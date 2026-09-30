"""Synthetic, network-free contract for the native supplier Flash lane."""

import io
import json
import unittest
from unittest.mock import patch

import requests

from services.ai_service import (
    AIClient, AIConfig, AIProfileError, AIProvider, AITask,
    DEEPSEEK_MODELS, OPENROUTER_MODELS,
)
from services.supplier_service import SupplierService


def _config(provider=AIProvider.DEEPSEEK, base='https://api.deepseek.com/v1'):
    return AIConfig(
        provider=provider,
        api_key='synthetic-key',
        api_base_url=base,
        model='deepseek-v4-pro',
    )


def _response(status, body):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode('utf-8')
    response._content_consumed = True
    response.raw = io.BytesIO()
    response.url = 'https://api.deepseek.com/v1/chat/completions'
    return response


class _InvalidTask(AITask):
    def get_system_prompt(self):
        return 'system'

    def build_user_prompt(self, **kwargs):
        return 'user'

    def parse_response(self, response):
        return None


class SupplierFlashProfileTest(unittest.TestCase):
    def test_legacy_supplier_ai_service_keeps_its_existing_model(self):
        with patch.object(AIConfig, 'from_settings', return_value=_config()):
            service = SupplierService._get_ai_service(
                object(), model_override='deepseek-v4-pro',
            )
        try:
            self.assertEqual(service.config.model, 'deepseek-v4-pro')
            self.assertEqual(service.config.max_retries, 3)
            self.assertEqual(service.config.task_profile, '')
        finally:
            service.close()

    def test_native_profile_ignores_global_pro_model_without_changing_it(self):
        original = _config()
        original.proxy_enabled = True
        original.timeout = 180
        with patch.object(AIConfig, 'from_settings', return_value=original):
            profile = AIConfig.for_supplier_parsing(object())
        self.assertIs(profile, original)
        self.assertEqual(profile.model, 'deepseek-flash')
        self.assertEqual(profile.max_retries, 1)
        self.assertEqual(profile.parse_retries, 1)
        self.assertFalse(profile.log_payloads)
        self.assertFalse(profile.proxy_enabled)
        self.assertEqual(profile.timeout, 60)
        self.assertEqual(profile.task_profile, 'supplier_parsing_flash')
        self.assertIn('deepseek-flash', DEEPSEEK_MODELS)
        self.assertNotIn('deepseek-flash', OPENROUTER_MODELS)

    def test_wrong_provider_base_and_override_fail_before_http(self):
        cases = (
            (_config(AIProvider.OPENROUTER, 'https://openrouter.ai/api/v1'), None,
             'native_deepseek_required'),
            (_config(base='https://api.deepseek.com.evil.test/v1'), None,
             'native_deepseek_base_required'),
            (_config(base='http://api.deepseek.com/v1'), None,
             'native_deepseek_base_required'),
            (_config(), 'deepseek-v4-pro', 'unsupported_model'),
        )
        for config, override, expected in cases:
            with self.subTest(expected=expected), patch.object(
                AIConfig, 'from_settings', return_value=config,
            ), self.assertRaises(AIProfileError) as raised:
                AIConfig.for_supplier_parsing(object(), override)
            self.assertEqual(raised.exception.code, expected)

    def test_one_post_disabled_thinking_and_exact_numeric_usage(self):
        with patch.object(AIConfig, 'from_settings', return_value=_config()):
            config = AIConfig.for_supplier_parsing(object())
        client = AIClient(config)
        self.assertFalse(client._session.trust_env)
        result = _response(200, {
            'choices': [{
                'message': {'content': '{"results":[]}'}, 'finish_reason': 'stop',
            }],
            'usage': {
                'prompt_tokens': 87, 'completion_tokens': 11,
                'total_tokens': 98, 'prompt_cache_hit_tokens': 60,
                'prompt_cache_miss_tokens': 27,
                'completion_tokens_details': {'reasoning_tokens': 0},
                'sneaky_body': 'must-not-retain',
            },
        })
        with patch.object(client._session, 'post', return_value=result) as post:
            answer = client.chat_completion([{'role': 'user', 'content': 'secret source'}])
        self.assertEqual(answer, '{"results":[]}')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['json']['model'], 'deepseek-flash')
        self.assertEqual(post.call_args.kwargs['json']['thinking'], {'type': 'disabled'})
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertTrue(post.call_args.kwargs['stream'])
        self.assertEqual(client.last_attempts, 1)
        self.assertEqual(client.last_usage, {
            'prompt_tokens': 87, 'completion_tokens': 11,
            'total_tokens': 98, 'prompt_cache_hit_tokens': 60,
            'prompt_cache_miss_tokens': 27, 'reasoning_tokens': 0,
        })

    def test_429_is_one_physical_attempt_without_sleep_or_body_leak(self):
        with patch.object(AIConfig, 'from_settings', return_value=_config()):
            config = AIConfig.for_supplier_parsing(object())
        client = AIClient(config)
        response = _response(429, {'message': 'private-body-marker'})
        response.headers['Retry-After'] = '120'
        with patch.object(client._session, 'post', return_value=response) as post, \
             patch('time.sleep') as sleep:
            answer = client.chat_completion([{'role': 'user', 'content': 'private-prompt'}])
        self.assertIsNone(answer)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(client.last_attempts, 1)
        self.assertIsNone(client.last_usage)
        self.assertNotIn('private-body-marker', client.last_error)

    def test_invalid_format_has_no_inner_retry_or_response_snippet(self):
        with patch.object(AIConfig, 'from_settings', return_value=_config()):
            config = AIConfig.for_supplier_parsing(object())
        client = AIClient(config)
        response = _response(200, {
            'choices': [{
                'message': {'content': 'private-output-marker'}, 'finish_reason': 'stop',
            }],
        })
        with patch.object(client._session, 'post', return_value=response) as post, \
             patch('time.sleep') as sleep:
            success, value, error = _InvalidTask(client).execute()
        self.assertFalse(success)
        self.assertIsNone(value)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertNotIn('private-output-marker', error)
        self.assertIsNone(client.last_usage)

    def test_307_redirect_is_rejected_without_following(self):
        with patch.object(AIConfig, 'from_settings', return_value=_config()):
            config = AIConfig.for_supplier_parsing(object())
        client = AIClient(config)
        response = _response(307, {'message': 'do not forward the key'})
        response.headers['Location'] = 'https://openrouter.ai/api/v1/chat/completions'
        with patch.object(client._session, 'post', return_value=response) as post:
            self.assertIsNone(client.chat_completion([{'role': 'user', 'content': 'x'}]))
        self.assertEqual(post.call_count, 1)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertEqual(client.last_attempts, 1)
        self.assertIn('redirect', client.last_error)

    def test_truncated_completion_is_not_applied_or_retried(self):
        with patch.object(AIConfig, 'from_settings', return_value=_config()):
            config = AIConfig.for_supplier_parsing(object())
        client = AIClient(config)
        response = _response(200, {
            'choices': [{
                'message': {'content': '{"results":[]}'}, 'finish_reason': 'length',
            }],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 20},
        })
        with patch.object(client._session, 'post', return_value=response) as post, \
             patch('time.sleep') as sleep:
            self.assertIsNone(client.chat_completion([{'role': 'user', 'content': 'x'}]))
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(client.last_finish_reason, 'length')
        self.assertEqual(client.last_usage['completion_tokens'], 20)
        self.assertIn('не завершён', client.last_error)

    def test_response_size_is_bounded_before_json(self):
        with patch.object(AIConfig, 'from_settings', return_value=_config()):
            config = AIConfig.for_supplier_parsing(object())
        client = AIClient(config)
        response = _response(200, {'choices': []})
        response.headers['Content-Length'] = str(4 * 1024 * 1024 + 1)
        with patch.object(client._session, 'post', return_value=response) as post:
            self.assertIsNone(client.chat_completion([{'role': 'user', 'content': 'x'}]))
        self.assertEqual(post.call_count, 1)
        self.assertIn('4 MiB', client.last_error)

        response2 = _response(200, {'choices': []})
        response2._content = b'x' * (4 * 1024 * 1024 + 1)
        with patch.object(client._session, 'post', return_value=response2) as post:
            self.assertIsNone(client.chat_completion([{'role': 'user', 'content': 'x'}]))
        self.assertEqual(post.call_count, 1)
        self.assertIn('4 MiB', client.last_error)

    def test_admin_prompt_and_output_are_bounded_before_http(self):
        with patch.object(AIConfig, 'from_settings', return_value=_config()):
            config = AIConfig.for_supplier_parsing(object())
        client = AIClient(config)
        with patch.object(client._session, 'post') as post:
            answer = client.chat_completion([
                {'role': 'user', 'content': 'x' * (256 * 1024)},
            ], max_tokens=16000)
        self.assertIsNone(answer)
        post.assert_not_called()
        self.assertEqual(client.last_attempts, 0)
        self.assertIn('256 KiB', client.last_error)

        response = _response(200, {
            'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}],
        })
        with patch.object(client._session, 'post', return_value=response) as post:
            self.assertEqual(client.chat_completion([
                {'role': 'user', 'content': 'bounded'},
            ], max_tokens=16000), '{}')
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 8000)

    def test_usage_rejects_untrusted_large_and_boolean_counters(self):
        self.assertEqual(AIClient._safe_usage({
            'prompt_tokens': True,
            'completion_tokens': 2**63,
            'total_tokens': 3,
            'prompt_cache_hit_tokens': -1,
            'prompt_cache_miss_tokens': 0,
            'completion_tokens_details': {'reasoning_tokens': 2**63},
        }), {'total_tokens': 3, 'prompt_cache_miss_tokens': 0})


if __name__ == '__main__':
    unittest.main()
