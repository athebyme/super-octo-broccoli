# -*- coding: utf-8 -*-
"""
Тесты для LLM-слоя — retry-декоратор и JSON-парсинг.
"""
import json
from types import SimpleNamespace

import pytest

import agents.llm as llm_module
from agents.llm import (
    llm_retry, _extract_json_from_text, create_llm_from_profile,
    _extract_openai_usage, _safe_base_url_for_log, DeepSeekLLM, OpenAICompatLLM,
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

    def test_no_retry_on_non_retryable(self):
        @llm_retry(max_retries=3, base_delay=0.01)
        def bad():
            raise ValueError('bad input')

        with pytest.raises(ValueError):
            bad()

    def test_exhausted_retries_raises(self):
        @llm_retry(max_retries=1, base_delay=0.01)
        def always_fail():
            raise ConnectionError('nope')

        with pytest.raises(ConnectionError):
            always_fail()


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
            'stable system', 'dynamic request one', schema,
        )
        provider.structured_output_with_usage(
            'stable system', 'dynamic request two', schema,
        )

        assert first['data'] == {'result': 'ok'}
        assert first['usage']['cache_hit_tokens'] == 64
        assert calls[0]['messages'][0] == calls[1]['messages'][0]
        assert calls[0]['messages'][1]['content'] == 'dynamic request one'
        assert calls[1]['messages'][1]['content'] == 'dynamic request two'
        assert 'dynamic request' not in calls[0]['messages'][0]['content']
