# -*- coding: utf-8 -*-
"""
Тесты для BaseAgent — core-модуль агентной системы.

Тестирует утилиты и внутреннюю логику без запуска LLM.
"""
import json
import pytest

from agents.base_agent import (
    _sanitize_error,
    _estimate_context_size,
    _summarize_old_messages,
    _extract_json,
    _BoundedFailureTracker,
    BaseAgent,
)


# ── _sanitize_error ────────────────────────────────────────────────

class TestSanitizeError:
    def test_empty_string(self):
        assert _sanitize_error('') == 'Неизвестная ошибка'

    def test_none(self):
        assert _sanitize_error(None) == 'Неизвестная ошибка'

    def test_normal_error(self):
        assert _sanitize_error('Connection refused') == 'Connection refused'

    def test_html_error(self):
        result = _sanitize_error('<!DOCTYPE html><html>Error 404</html>')
        assert 'HTML' in result
        assert 'CLOUDRU_BASE_URL' in result

    def test_long_error_truncated(self):
        long_msg = 'x' * 1000
        result = _sanitize_error(long_msg)
        assert len(result) <= 504  # 500 + '...'
        assert result.endswith('...')


# ── _estimate_context_size ─────────────────────────────────────────

class TestEstimateContextSize:
    def test_empty(self):
        assert _estimate_context_size([]) == 0

    def test_with_content(self):
        messages = [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'world'},
        ]
        assert _estimate_context_size(messages) == 10

    def test_missing_content(self):
        messages = [{'role': 'user'}]
        assert _estimate_context_size(messages) == 0


# ── _summarize_old_messages ────────────────────────────────────────

class TestSummarizeOldMessages:
    def test_short_list_unchanged(self):
        msgs = [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}]
        assert _summarize_old_messages(msgs) == msgs

    def test_four_messages_unchanged(self):
        msgs = [
            {'role': 'user', 'content': 'task'},
            {'role': 'assistant', 'content': 'ok'},
            {'role': 'user', 'content': 'result'},
            {'role': 'assistant', 'content': 'done'},
        ]
        assert _summarize_old_messages(msgs) == msgs

    def test_five_messages_compressed(self):
        msgs = [
            {'role': 'user', 'content': 'task prompt'},
            {'role': 'assistant', 'content': '[Tool Call: get_product(1)]'},
            {'role': 'user', 'content': '[Tool Result: get_product] ok'},
            {'role': 'assistant', 'content': 'thinking...'},
            {'role': 'user', 'content': 'final result'},
        ]
        result = _summarize_old_messages(msgs)
        assert len(result) == 4  # first + summary + last 2
        assert result[0] == msgs[0]  # first preserved
        assert 'Контекст сжат' in result[1]['content']
        assert result[-2:] == msgs[-2:]  # tail preserved

    def test_tool_names_extracted(self):
        msgs = [
            {'role': 'user', 'content': 'task'},
            {'role': 'assistant', 'content': '[Tool Call: get_product(1)]'},
            {'role': 'user', 'content': '[Tool Result: get_product] ok'},
            {'role': 'assistant', 'content': '[Tool Call: update_product(1)]'},
            {'role': 'user', 'content': '[Tool Result: update_product] ok'},
            {'role': 'assistant', 'content': 'done'},
            {'role': 'user', 'content': 'final'},
        ]
        result = _summarize_old_messages(msgs)
        summary = result[1]['content']
        assert 'get_product' in summary
        assert 'update_product' in summary


# ── _extract_json ──────────────────────────────────────────────────

class TestExtractJson:
    def test_pure_json(self):
        result = _extract_json('{"a": 1, "b": 2}')
        assert result == {'a': 1, 'b': 2}

    def test_json_in_code_block(self):
        text = '```json\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {'key': 'value'}

    def test_json_in_bare_code_block(self):
        text = '```\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {'key': 'value'}

    def test_json_embedded_in_text(self):
        text = 'Here is the result:\n{"status": "ok", "count": 5}\nDone!'
        result = _extract_json(text)
        assert result == {'status': 'ok', 'count': 5}

    def test_nested_json(self):
        text = '{"outer": {"inner": [1, 2, 3]}}'
        result = _extract_json(text)
        assert result == {'outer': {'inner': [1, 2, 3]}}

    def test_no_json_returns_message(self):
        result = _extract_json('Just some text without JSON')
        assert 'message' in result
        assert 'Just some text' in result['message']

    def test_empty_returns_default(self):
        result = _extract_json('')
        assert result == {'message': 'Задача выполнена'}

    def test_none_returns_default(self):
        result = _extract_json(None)
        assert result == {'message': 'Задача выполнена'}

    def test_json_with_text_around(self):
        text = 'Результат обработки:\n```json\n{"processed": 5, "errors": 0}\n```\nЗадача выполнена.'
        result = _extract_json(text)
        assert result == {'processed': 5, 'errors': 0}


# ── _BoundedFailureTracker ─────────────────────────────────────────

class TestBoundedFailureTracker:
    def test_increment(self):
        tracker = _BoundedFailureTracker(maxsize=10)
        assert tracker.increment('task_1') == 1
        assert tracker.increment('task_1') == 2
        assert tracker.increment('task_1') == 3

    def test_get_default(self):
        tracker = _BoundedFailureTracker()
        assert tracker.get('nonexistent', 0) == 0

    def test_pop(self):
        tracker = _BoundedFailureTracker()
        tracker.increment('task_1')
        tracker.pop('task_1', None)
        assert tracker.get('task_1', 0) == 0

    def test_bounded_size(self):
        tracker = _BoundedFailureTracker(maxsize=3)
        tracker.increment('a')
        tracker.increment('b')
        tracker.increment('c')
        tracker.increment('d')  # should evict 'a'
        assert 'a' not in tracker
        assert len(tracker) == 3

    def test_lru_order(self):
        tracker = _BoundedFailureTracker(maxsize=3)
        tracker.increment('a')
        tracker.increment('b')
        tracker.increment('c')
        tracker.increment('a')  # moves 'a' to end
        tracker.increment('d')  # should evict 'b' (oldest)
        assert 'b' not in tracker
        assert 'a' in tracker
        assert tracker['a'] == 2


# ── BaseAgent.parse_input_data ─────────────────────────────────────

class TestParseInputData:
    def test_json_string(self):
        task = {'input_data': '{"product_ids": [1, 2]}'}
        result = BaseAgent.parse_input_data(task)
        assert result == {'product_ids': [1, 2]}

    def test_already_dict(self):
        task = {'input_data': {'key': 'value'}}
        result = BaseAgent.parse_input_data(task)
        assert result == {'key': 'value'}

    def test_empty_string(self):
        task = {'input_data': ''}
        result = BaseAgent.parse_input_data(task)
        assert result == {}

    def test_invalid_json(self):
        task = {'input_data': 'not json'}
        result = BaseAgent.parse_input_data(task)
        assert result == {}

    def test_missing_key(self):
        task = {}
        result = BaseAgent.parse_input_data(task)
        assert result == {}

    def test_none_value(self):
        task = {'input_data': None}
        result = BaseAgent.parse_input_data(task)
        assert result == {}
