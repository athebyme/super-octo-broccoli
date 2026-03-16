# -*- coding: utf-8 -*-
"""
Тесты для LLM-слоя — retry-декоратор и JSON-парсинг.
"""
import json
import pytest

from agents.llm import llm_retry, _extract_json_from_text


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
