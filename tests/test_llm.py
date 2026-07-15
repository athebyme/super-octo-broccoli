# -*- coding: utf-8 -*-
"""
Тесты для LLM-слоя — retry-декоратор и JSON-парсинг.
"""
import json
from types import SimpleNamespace

import pytest

import agents.llm as llm_module
from agents.llm import (
    BaseLLM, llm_retry, llm_retry_attempt_limit,
    _extract_json_from_text, create_llm_from_profile,
    _extract_openai_usage, _safe_base_url_for_log, DeepSeekLLM, OpenAICompatLLM,
    OpenRouterLLM, LLMProviderError,
)


# ── _extract_json_from_text ────────────────────────────────────────

class TestExtractJsonFromText:
    def test_pure_json(self):
        result = _extract_json_from_text('{"key": "value"}')
        assert result == {'key': 'value'}

    def test_json_in_code_block(self):
        result = _extract_json_from_text('```json\n{"key": "value"}\n```')
        assert result == {'key': 'value'}

    def test_json_in_bare_code_block(self):
        result = _extract_json_from_text('```\n{"key": "value"}\n```')
        assert result == {'key': 'value'}

    def test_json_with_surrounding_text(self):
        text = 'Here is result: {"status": "ok"} done.'
        result = _extract_json_from_text(text)
        assert result == {'status': 'ok'}

    def test_nested_json(self):
        text = '{"outer": {"inner": {"deep": 1}}}'
        result = _extract_json_from_text(text)
        assert result['outer']['inner']['deep'] == 1

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            _extract_json_from_text('no json here at all')

    def test_whitespace_handling(self):
        result = _extract_json_from_text('  \n  {"a": 1}  \n  ')
        assert result == {'a': 1}


# ── llm_retry ──────────────────────────────────────────────────────

class TestLLMRetry:
    def test_success_no_retry(self):
        call_count = 0

        @llm_retry(max_retries=3, base_delay=0.01)
        def success():
            nonlocal call_count
            call_count += 1
            return 'ok'

        assert success() == 'ok'
        assert call_count == 1

    def test_retries_on_connection_error(self):
        call_count = 0

        @llm_retry(max_retries=2, base_delay=0.01)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError('network error')
            return 'ok'

        assert flaky() == 'ok'
        assert call_count == 3

    def test_success_usage_counts_every_physical_attempt(self):
        call_count = 0

        @llm_retry(max_retries=2, base_delay=0)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError('network error')
            return {
                'text': 'ok',
                'usage': {'input_tokens': 11, 'api_requests': 1},
            }

        result = flaky()

        assert result['usage'] == {
            'input_tokens': 11,
            'api_requests': 3,
        }

    def test_no_retry_on_non_retryable(self):
        @llm_retry(max_retries=3, base_delay=0.01)
        def bad():
            raise ValueError('bad input')

        with pytest.raises(ValueError):
            bad()

    def test_exhausted_retries_attaches_usage_without_final_sleep(
            self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(llm_module.time, 'sleep', sleeps.append)

        @llm_retry(max_retries=1, base_delay=2)
        def always_fail():
            raise ConnectionError('nope')

        with pytest.raises(ConnectionError) as exc_info:
            always_fail()

        assert exc_info.value.llm_usage == {'api_requests': 2}
        assert sleeps == [2]

    def test_attempt_limit_caps_retries_and_is_request_local(self):
        call_count = 0

        @llm_retry(max_retries=3, base_delay=0)
        def succeeds_on_third_call():
            nonlocal call_count
            call_count += 1
            if call_count % 3:
                raise TimeoutError('retry me')
            return {'usage': {}}

        with llm_retry_attempt_limit(2):
            with pytest.raises(TimeoutError) as exc_info:
                succeeds_on_third_call()

        assert call_count == 2
        assert exc_info.value.llm_usage == {'api_requests': 2}

        # The exhausted limit must not leak into the next logical request.
        call_count = 0
        result = succeeds_on_third_call()
        assert result['usage']['api_requests'] == 3

    @pytest.mark.parametrize('value', [0, -1, True, 1.5])
    def test_attempt_limit_requires_positive_integer(self, value):
        with pytest.raises(ValueError):
            with llm_retry_attempt_limit(value):
                pass


class TestOpenRouterProxy:
    def test_explicit_socks_proxy_is_passed_only_as_http_client(
            self, monkeypatch):
        import httpx
        import openai

        captured = {}
        marker = object()

        def fake_http_client(**kwargs):
            captured['httpx'] = kwargs
            return marker

        def fake_openai(**kwargs):
            captured['openai'] = kwargs
            return SimpleNamespace()

        monkeypatch.setattr(httpx, 'Client', fake_http_client)
        monkeypatch.setattr(openai, 'OpenAI', fake_openai)
        cfg = SimpleNamespace(
            OPENROUTER_API_KEY='test-secret',
            OPENROUTER_MODEL='google/gemini-2.5-flash',
            AI_PROXY='socks5://proxy.local:1080',
        )

        OpenRouterLLM(cfg)

        assert captured['httpx']['proxy'] == 'socks5://proxy.local:1080'
        assert captured['openai']['http_client'] is marker
        assert 'proxy.local' not in captured['openai']['default_headers'].values()

    @pytest.mark.parametrize('proxy', [
        'ftp://proxy.local:21', 'socks5://', 'not-a-url',
    ])
    def test_invalid_proxy_is_rejected_before_client_creation(self, proxy):
        cfg = SimpleNamespace(
            OPENROUTER_API_KEY='test-secret',
            OPENROUTER_MODEL='google/gemini-2.5-flash',
            AI_PROXY=proxy,
        )

        with pytest.raises(LLMProviderError, match='AI_PROXY'):
            OpenRouterLLM(cfg)


class TestCompatibilityUsage:
    class PlainProvider(BaseLLM):
        def __init__(self, failures=0, text='ok'):
            self.failures = failures
            self.text = text
            self.calls = 0

        @llm_retry(max_retries=3, base_delay=0)
        def chat(self, system, messages, temperature=None, max_tokens=None):
            self.calls += 1
            if self.calls <= self.failures:
                raise ConnectionError('temporary')
            return self.text

        def chat_with_tools(self, system, messages, tools,
                            temperature=None, max_tokens=None):
            raise NotImplementedError

        def structured_output(self, system, prompt, schema,
                              max_tokens=None):
            text = self.chat(
                system,
                [{'role': 'user', 'content': prompt}],
                max_tokens=max_tokens,
            )
            return _extract_json_from_text(text)

    def test_plain_chat_wrapper_captures_retry_attempts(self):
        provider = self.PlainProvider(failures=2)

        result = provider.chat_with_usage('system', [])

        assert result == {
            'text': 'ok',
            'usage': {'api_requests': 3},
        }

    def test_structured_parse_error_keeps_nested_chat_attempts(self):
        provider = self.PlainProvider(failures=1, text='not json')

        with pytest.raises(ValueError) as exc_info:
            provider.structured_output_with_usage(
                'system', 'prompt', {'type': 'object'},
            )

        assert provider.calls == 2
        assert exc_info.value.llm_usage == {'api_requests': 2}


class TestTaskLLMProfile:
    def test_base_url_log_value_removes_credentials_and_query(self):
        assert _safe_base_url_for_log(
            'https://user:secret@example.com/v1?api_key=secret#fragment'
        ) == 'https://example.com/v1'

    def test_deepseek_profile_overrides_model_and_key(self, monkeypatch):
        captured = {}

        def fake_deepseek(config):
            captured.update({
                'key': config.DEEPSEEK_API_KEY,
                'url': config.DEEPSEEK_BASE_URL,
                'model': config.DEEPSEEK_MODEL,
            })
            return captured

        base = SimpleNamespace(
            DEEPSEEK_API_KEY='central-key',
            DEEPSEEK_BASE_URL='https://central.example/v1',
        )
        monkeypatch.setattr(llm_module, 'DeepSeekLLM', fake_deepseek)

        result = create_llm_from_profile({
            'provider': 'deepseek', 'key': 'seller-key',
            'base_url': 'https://seller.example/v1', 'model': 'deepseek-v4-pro',
        }, base)

        assert result == {
            'key': 'seller-key',
            'url': 'https://seller.example/v1',
            'model': 'deepseek-v4-pro',
        }

    def test_default_profile_is_deepseek_v4_pro(self, monkeypatch):
        monkeypatch.setattr(
            llm_module, 'DeepSeekLLM', lambda config: config.DEEPSEEK_MODEL,
        )
        base = SimpleNamespace(
            DEEPSEEK_API_KEY='central-key',
            DEEPSEEK_BASE_URL='https://api.deepseek.com/v1',
        )

        assert create_llm_from_profile({}, base) == 'deepseek-v4-pro'


class TestPromptCacheUsage:
    def test_deepseek_execution_can_disable_thinking(self):
        provider = object.__new__(DeepSeekLLM)
        provider.thinking = False

        assert provider._thinking_request_kwargs() == {
            'extra_body': {'thinking': {'type': 'disabled'}},
        }

        provider.thinking = None
        assert provider._thinking_request_kwargs() == {}

    def test_deepseek_cache_metrics_and_estimated_cost(self):
        usage = SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=100,
            prompt_cache_hit_tokens=800,
            prompt_cache_miss_tokens=200,
        )

        result = _extract_openai_usage(usage, 'deepseek-v4-flash')

        assert result['cache_hit_tokens'] == 800
        assert result['cache_miss_tokens'] == 200
        assert result['model'] == 'deepseek-v4-flash'
        assert result['cache_hit'] is True
        assert result['cache_hit_rate'] == 0.8
        assert result['estimated_cost_usd'] == pytest.approx(0.00005824)

    def test_openai_cached_tokens_shape_is_supported(self):
        usage = {
            'prompt_tokens': 120,
            'completion_tokens': 20,
            'prompt_tokens_details': {'cached_tokens': 90},
            'completion_tokens_details': {'reasoning_tokens': 7},
            'cost': 0.0042,
        }

        result = _extract_openai_usage(usage, 'some-proxy-model')

        assert result['cache_hit_tokens'] == 90
        assert result['cache_miss_tokens'] == 30
        assert result['reasoning_tokens'] == 7
        assert result['cost_usd'] == 0.0042

    def test_structured_output_keeps_schema_in_stable_prefix(self):
        calls = []

        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content='{"result":"ok"}'),
                    )],
                    usage=SimpleNamespace(
                        prompt_tokens=100,
                        completion_tokens=10,
                        prompt_cache_hit_tokens=64,
                        prompt_cache_miss_tokens=36,
                    ),
                )

        provider = object.__new__(OpenAICompatLLM)
        provider.model = 'deepseek-v4-pro'
        provider.base_url = 'https://api.deepseek.com'
        provider.cfg = SimpleNamespace(TEMPERATURE=0.1, MAX_TOKENS=256)
        provider.client = SimpleNamespace(
            chat=SimpleNamespace(completions=Completions()),
        )
        schema = {
            'type': 'object',
            'properties': {'result': {'type': 'string'}},
            'required': ['result'],
        }

        first = provider.structured_output_with_usage(
            'stable system', 'dynamic request one', schema, max_tokens=17,
        )
        provider.structured_output_with_usage(
            'stable system', 'dynamic request two', schema,
        )

        assert first['data'] == {'result': 'ok'}
        assert first['usage']['cache_hit_tokens'] == 64
        assert calls[0]['max_tokens'] == 17
        assert calls[0]['messages'][0] == calls[1]['messages'][0]
        assert calls[0]['messages'][1]['content'] == 'dynamic request one'
        assert calls[1]['messages'][1]['content'] == 'dynamic request two'
        assert 'dynamic request' not in calls[0]['messages'][0]['content']

    def test_multimodal_structured_output_keeps_reference_out_of_system_prefix(self):
        calls = []

        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content='{"scene":"cyan studio"}'),
                    )],
                    usage=SimpleNamespace(prompt_tokens=80, completion_tokens=12),
                )

        provider = object.__new__(OpenAICompatLLM)
        provider.model = 'google/gemini-2.5-flash'
        provider.base_url = 'https://openrouter.ai/api/v1'
        provider.cfg = SimpleNamespace(TEMPERATURE=0.1, MAX_TOKENS=256)
        provider.client = SimpleNamespace(
            chat=SimpleNamespace(completions=Completions()),
        )
        schema = {
            'type': 'object',
            'properties': {'scene': {'type': 'string'}},
            'required': ['scene'],
        }
        reference = 'https://cdn.example.test/reference.webp'

        result = provider.structured_output_multimodal_with_usage(
            'stable system', 'inspect style only', schema,
            image_urls=[reference], max_tokens=64,
        )

        assert result['data'] == {'scene': 'cyan studio'}
        assert result['usage']['api_requests'] == 1
        assert reference not in calls[0]['messages'][0]['content']
        content = calls[0]['messages'][1]['content']
        assert content[0] == {'type': 'text', 'text': 'inspect style only'}
        assert content[1] == {
            'type': 'image_url', 'image_url': {'url': reference},
        }
