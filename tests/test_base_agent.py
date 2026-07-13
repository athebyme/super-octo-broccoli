# -*- coding: utf-8 -*-
"""
Тесты для BaseAgent — core-модуль агентной системы.

Тестирует утилиты и внутреннюю логику без запуска LLM.
"""
import json
import threading
from types import SimpleNamespace

import pytest

import agents.base_agent as base_agent_module
from agents.base_agent import (
    _sanitize_error,
    _estimate_context_size,
    _summarize_old_messages,
    _extract_json,
    _build_usage,
    _merge_usage,
    _BoundedFailureTracker,
    BaseAgent,
    select_task_llm_profile,
)
from agents.llm import llm_retry
from agents.tools import ToolRegistry


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


class TestRuntimeLiveness:
    def test_liveness_continues_while_platform_heartbeat_is_blocked(self, monkeypatch):
        heartbeat_entered = threading.Event()
        release_heartbeat = threading.Event()
        second_touch = threading.Event()
        touches = []

        class BlockingPlatform:
            def heartbeat(self, status):
                heartbeat_entered.set()
                release_heartbeat.wait(timeout=1)

        def record_touch():
            touches.append(1)
            if len(touches) >= 2:
                second_touch.set()

        class RuntimeAgent(BaseAgent):
            def build_task_prompt(self, task):
                return ''

        agent = object.__new__(RuntimeAgent)
        agent.platform = BlockingPlatform()
        agent.config = SimpleNamespace(
            HEARTBEAT_INTERVAL=30,
            reload_remote_config=lambda: None,
        )
        agent._running = True
        agent._heartbeat_thread = None
        agent._liveness_thread = None
        agent._runtime_stop_event = threading.Event()
        monkeypatch.setattr(base_agent_module, 'LIVENESS_INTERVAL_SECONDS', 0.01)
        monkeypatch.setattr(base_agent_module, '_touch_liveness', record_touch)

        try:
            agent._start_heartbeat()
            assert heartbeat_entered.wait(timeout=0.5)
            assert second_touch.wait(timeout=0.5)
        finally:
            agent.stop()
            release_heartbeat.set()
            agent._stop_heartbeat()

        assert len(touches) >= 2


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


class _TestAgent(BaseAgent):
    agent_name = 'test-agent'
    system_prompt = 'test system'

    def build_task_prompt(self, task: dict) -> str:
        return 'test prompt'


class _RecordingPlatform:
    def __init__(self):
        self.actions = []

    def log_thinking(self, *args, **kwargs):
        self.actions.append(('thinking', args, kwargs))

    def log_action(self, *args, **kwargs):
        self.actions.append(('action', args, kwargs))

    def log_decision(self, *args, **kwargs):
        self.actions.append(('decision', args, kwargs))

    def update_progress(self, *args, **kwargs):
        self.actions.append(('progress', args, kwargs))


class _SingleResponseLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat_with_tools(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) > 1:
            raise AssertionError('token budget must prevent another LLM call')
        return self.response


def _make_bare_agent(response, token_budget=10):
    agent = object.__new__(_TestAgent)
    agent.config = SimpleNamespace(
        RUN_TOKEN_BUDGET=token_budget,
        MAX_TOKENS=4096,
        OBSERVATION_MAX_CHARS=1200,
    )
    agent.platform = _RecordingPlatform()
    agent.llm = _SingleResponseLLM(response)
    agent._step_namer = None
    agent._tools = ToolRegistry()
    return agent


class TestTokenBudget:
    def test_unlimited_budget_keeps_original_llm_call_shape(self):
        response = {
            'text': '{"completed": true}',
            'tool_calls': [],
            'stop_reason': 'end_turn',
            'usage': {'input_tokens': 7, 'output_tokens': 3},
        }
        agent = _make_bare_agent(response, token_budget=0)
        agent._tools.register(
            'unused', 'Keeps tool calling enabled',
            {'properties': {}, 'required': []},
            handler=lambda: {'ok': True},
        )

        agent._execute_react({'id': 'task-0'})

        assert 'max_tokens' not in agent.llm.calls[0]

    def test_task_scoped_override_limits_single_react(self):
        response = {
            'text': '{"completed": true}',
            'tool_calls': [],
            'stop_reason': 'end_turn',
            'usage': {'input_tokens': 4, 'output_tokens': 2},
        }
        agent = _make_bare_agent(response, token_budget=2000)
        agent._run_token_budget_override = 1000
        agent._tools.register(
            'unused', 'Keeps tool calling enabled',
            {'properties': {}, 'required': []},
            handler=lambda: {'ok': True},
        )

        agent._execute_react({'id': 'task-scoped-budget'})

        assert 0 < agent.llm.calls[0]['max_tokens'] < 1000

    def test_exhausted_budget_returns_partial_without_second_llm_call(self):
        tool_runs = []
        response = {
            'text': '{"processed": 1}',
            'tool_calls': [
                {'name': 'record', 'arguments': {'value': 1}, 'id': 'call-1'},
            ],
            'stop_reason': 'tool_use',
            'usage': {'input_tokens': 900, 'output_tokens': 100},
        }
        agent = _make_bare_agent(response, token_budget=1000)
        agent._tools.register(
            'record', 'Record a value',
            {
                'properties': {'value': {'type': 'integer'}},
                'required': ['value'],
            },
            handler=lambda value: tool_runs.append(value) or {'ok': True},
        )

        result = agent._execute_react({'id': 'task-1'})

        assert tool_runs == [1]
        assert len(agent.llm.calls) == 1
        assert 0 < agent.llm.calls[0]['max_tokens'] < 1000
        assert result['processed'] == 1
        assert result['status'] == 'partial'
        assert result['_usage'] == {
            'input_tokens': 900,
            'output_tokens': 100,
            'total_tokens': 1000,
            'api_requests': 1,
            'react_iterations': 1,
            'token_budget': 1000,
            'budget_exhausted': True,
        }

    def test_completed_answer_wins_when_final_call_exhausts_budget(self):
        response = {
            'text': '{"completed": true}',
            'tool_calls': [],
            'stop_reason': 'end_turn',
            'usage': {'input_tokens': 900, 'output_tokens': 100},
        }
        agent = _make_bare_agent(response, token_budget=1000)
        agent._tools.register(
            'unused', 'Keeps tool calling enabled',
            {'properties': {}, 'required': []},
            handler=lambda: {'ok': True},
        )

        result = agent._execute_react({'id': 'task-2'})

        assert result['completed'] is True
        assert result.get('status') != 'partial'
        assert result['_usage']['total_tokens'] == 1000

    def test_next_input_estimate_can_block_call_without_spending_tokens(self):
        response = {
            'text': '{"completed": true}',
            'tool_calls': [],
            'stop_reason': 'end_turn',
            'usage': {'input_tokens': 1, 'output_tokens': 1},
        }
        agent = _make_bare_agent(response, token_budget=100)
        agent._tools.register(
            'unused', 'Keeps tool calling enabled',
            {'properties': {}, 'required': []},
            handler=lambda: {'ok': True},
        )

        result = agent._execute_react({'id': 'task-input-reserve'})

        assert result['status'] == 'partial'
        assert result['_usage']['budget_exhausted'] is True
        assert agent.llm.calls == []

    def test_react_propagates_cache_and_cost_usage(self):
        response = {
            'text': '{"completed": true}',
            'tool_calls': [],
            'stop_reason': 'end_turn',
            'usage': {
                'input_tokens': 100,
                'output_tokens': 20,
                'cache_hit_tokens': 64,
                'cache_miss_tokens': 36,
                'estimated_cost_usd': 0.0001,
            },
        }
        agent = _make_bare_agent(response, token_budget=0)
        agent._tools.register(
            'unused', 'Keeps tool calling enabled',
            {'properties': {}, 'required': []},
            handler=lambda: {'ok': True},
        )

        result = agent._execute_react({'id': 'task-cache'})

        assert result['_usage']['cache_hit_tokens'] == 64
        assert result['_usage']['cache_miss_tokens'] == 36
        assert result['_usage']['cache_hit_rate'] == 0.64
        assert result['_usage']['estimated_cost_usd'] == 0.0001

    def test_react_retry_cannot_exceed_remaining_api_budget(self):
        class AlwaysFailingLLM:
            def __init__(self):
                self.calls = 0

            @llm_retry(max_retries=3, base_delay=0)
            def chat_with_tools(self, **kwargs):
                self.calls += 1
                raise ConnectionError('temporary provider failure')

        agent = _make_bare_agent({}, token_budget=0)
        agent.llm = AlwaysFailingLLM()
        agent._tools.register(
            'unused', 'Keeps tool calling enabled',
            {'properties': {}, 'required': []},
            handler=lambda: {'ok': True},
        )

        with pytest.raises(ConnectionError) as exc_info:
            agent._execute_react(
                {'id': 'task-api-retry-budget'}, api_budget_override=2,
            )

        assert agent.llm.calls == 2
        assert exc_info.value.llm_usage['api_requests'] == 2


class TestTokenOptimizations:
    def test_usage_is_attributed_to_each_requested_model(self):
        total = {}
        _merge_usage(total, {
            'model': 'deepseek-v4-pro', 'input_tokens': 100,
            'output_tokens': 20, 'api_requests': 1,
        })
        _merge_usage(total, {
            'model': 'deepseek-v4-flash', 'input_tokens': 40,
            'output_tokens': 10, 'api_requests': 2,
        })
        result = _build_usage(total)

        assert result['total_tokens'] == 170
        assert result['api_requests'] == 3
        assert result['models']['deepseek-v4-pro']['total_tokens'] == 120
        assert result['models']['deepseek-v4-flash']['api_requests'] == 2

    def test_direct_usage_keeps_requested_model(self):
        result = _build_usage({
            'model': 'deepseek-v4-flash', 'input_tokens': 8,
            'output_tokens': 2, 'api_requests': 1,
        })

        assert result['models'] == {
            'deepseek-v4-flash': {
                'input_tokens': 8, 'output_tokens': 2,
                'total_tokens': 10, 'api_requests': 1,
            },
        }

    def test_observation_length_is_configurable(self):
        agent = object.__new__(_TestAgent)
        agent.config = SimpleNamespace(OBSERVATION_MAX_CHARS=5)

        result = agent._format_tool_results([
            {'name': 'long_tool', 'result': 'abcdefghij'},
        ])

        assert result == '[Tool Result: long_tool]\nabcde\n... (обрезано)'

    def test_tool_allowlist_and_step_namer_opt_in(self, monkeypatch):
        registry = ToolRegistry()
        registry.register('allowed', 'Allowed', {'properties': {}, 'required': []},
                          handler=lambda: 'allowed')
        registry.register('hidden', 'Hidden', {'properties': {}, 'required': []},
                          handler=lambda: 'hidden')

        class Config:
            STEP_NAMER_ENABLED = 0

            @classmethod
            def validate(cls):
                return None

        class AllowlistedAgent(_TestAgent):
            tool_allowlist = ('allowed',)

        monkeypatch.setattr(base_agent_module, 'PlatformClient', lambda config: object())
        monkeypatch.setattr(base_agent_module, 'create_llm', lambda config: object())
        monkeypatch.setattr(
            base_agent_module, 'create_platform_tools', lambda platform: registry,
        )
        monkeypatch.setattr(
            base_agent_module,
            'create_step_namer_llm',
            lambda config: pytest.fail('step namer must be disabled by default'),
        )

        agent = AllowlistedAgent(Config())

        assert [tool['name'] for tool in agent._tools.get_tool_schemas()] == ['allowed']
        assert agent._step_namer is None
        assert [tool['name'] for tool in registry.get_tool_schemas()] == [
            'allowed', 'hidden',
        ]


class TestTaskModelPolicy:
    def test_safe_read_task_can_use_flash(self):
        profile = {
            'provider': 'deepseek', 'model': 'deepseek-v4-pro',
            'key': 'secret', 'single_model': False,
        }

        selected = select_task_llm_profile('analyze_reviews', profile)

        assert selected['provider'] == 'deepseek'
        assert selected['model'] == 'deepseek-v4-flash'
        assert selected['thinking'] is False
        assert selected['key'] == 'secret'

    @pytest.mark.parametrize('task_type', ['seo_single', 'fill_batch', 'optimize_prices'])
    def test_execution_tasks_use_flash(self, task_type):
        profile = {
            'provider': 'deepseek', 'model': 'deepseek-v4-pro',
            'key': 'secret', 'single_model': False,
        }

        assert select_task_llm_profile(task_type, profile)['model'] == 'deepseek-v4-flash'

    @pytest.mark.parametrize('task_type', ['plan_request', 'smart', 'custom', 'pipeline'])
    def test_orchestration_tasks_keep_primary(self, task_type):
        profile = {
            'provider': 'deepseek', 'model': 'deepseek-v4-pro',
            'key': 'secret', 'single_model': False,
        }

        assert select_task_llm_profile(task_type, profile) == profile

    def test_single_model_keeps_primary_for_safe_task(self):
        profile = {
            'provider': 'deepseek', 'model': 'custom-primary',
            'single_model': True,
        }

        assert select_task_llm_profile('quality_check', profile) == profile

    def test_agent_configures_task_model_without_logging_key(self, monkeypatch, caplog):
        profile = {
            'provider': 'deepseek', 'model': 'deepseek-v4-pro',
            'key': 'never-log-this-key', 'single_model': False,
        }
        selected_profiles = []
        selected_llm = object()
        agent = object.__new__(_TestAgent)
        agent.config = SimpleNamespace()
        agent._default_llm = object()
        agent.llm = agent._default_llm
        agent.platform = SimpleNamespace(
            get_task_ai_config=lambda task_id: profile,
        )
        monkeypatch.setattr(
            base_agent_module,
            'create_llm_from_profile',
            lambda selected, config: selected_profiles.append(selected) or selected_llm,
        )

        agent._configure_task_llm({
            'id': 'task-safe', 'task_type': 'product_insights',
        })

        assert agent.llm is selected_llm
        assert selected_profiles[0]['model'] == 'deepseek-v4-flash'
        assert 'never-log-this-key' not in caplog.text


class _BatchPlatform(_RecordingPlatform):
    def __init__(self, agent):
        super().__init__()
        self.agent = agent
        self.saves = []
        self.checkpoints = []

    def get_task_status(self, task_id):
        status = 'cancelled' if self.agent.cancelled else 'running'
        return {'task': {'id': task_id, 'status': status}}

    def log_error(self, *args, **kwargs):
        self.actions.append(('error', args, kwargs))

    def log_result(self, *args, **kwargs):
        self.actions.append(('result', args, kwargs))

    def update_checkpoint(self, task_id, checkpoint):
        self.checkpoints.append((task_id, checkpoint))
        return {'ok': True}

    def batch_update_imported_products(self, updates):
        self.saves.append(updates)
        if getattr(self, 'cancel_on_save', False):
            self.agent.cancelled = True
        return {
            'updated': len(updates), 'failed': 0,
            'results': [
                {'product_id': item['product_id'], 'status': 'updated'}
                for item in updates
            ],
        }


class _StructuredBatchAgent(_TestAgent):
    def _prefetch_for_structured_batch(self, product_ids):
        self.prefetch_calls += 1
        return [{'id': product_id, 'title': f'Product {product_id}'}
                for product_id in product_ids]

    def build_structured_prompt(self, products_data):
        return json.dumps(products_data)

    def batch_result_schema(self):
        return {'type': 'object'}

    def _postprocess_structured_results(self, results):
        self.postprocess_calls += 1
        return results

    def _map_structured_result_to_updates(self, results):
        return [
            {'product_id': item['product_id'], 'title': item.get('title', 'Updated')}
            for item in results
        ]

    def _prefetch_reference_data(self, products_data):
        return {}

    def _build_tool_batch_prompt(self, products_data, reference_data):
        return json.dumps(products_data)


class _StructuredResponseLLM:
    def __init__(self, agent, data=None, error=None, cancel_after=False):
        self.agent = agent
        self.data = data
        self.error = error
        self.cancel_after = cancel_after
        self.calls = 0
        self.call_kwargs = []

    def structured_output_with_usage(self, **kwargs):
        self.calls += 1
        self.call_kwargs.append(kwargs)
        if self.cancel_after:
            self.agent.cancelled = True
        if self.error:
            raise self.error
        data = self.data(kwargs) if callable(self.data) else self.data
        return {
            'data': data,
            'usage': {
                'model': 'deepseek-v4-flash',
                'input_tokens': 10, 'output_tokens': 2, 'api_requests': 1,
            },
        }


def _make_batch_agent(data=None, error=None, cancel_after=False):
    agent = object.__new__(_StructuredBatchAgent)
    agent.config = SimpleNamespace(
        RUN_API_BUDGET=24, RUN_TOKEN_BUDGET=0, MAX_TOKENS=4096,
        OBSERVATION_MAX_CHARS=1200,
    )
    agent.cancelled = False
    agent.prefetch_calls = 0
    agent.postprocess_calls = 0
    agent.platform = _BatchPlatform(agent)
    agent.llm = _StructuredResponseLLM(
        agent, data=data, error=error, cancel_after=cancel_after,
    )
    agent._tools = ToolRegistry()
    agent._step_namer = None
    return agent


class TestStructuredBatchCancellation:
    def test_cancelled_before_prefetch_does_no_work(self):
        agent = _make_batch_agent(data={'results': []})
        agent.cancelled = True

        result = agent._execute_structured_batch(
            {'id': 'batch-cancelled'}, [1, 2], chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'cancelled'
        assert agent.prefetch_calls == 0
        assert agent.llm.calls == 0
        assert agent.platform.saves == []

    def test_cancelled_after_llm_skips_postprocess_and_save_but_keeps_usage(self):
        agent = _make_batch_agent(
            data={'results': [{'product_id': 1, 'title': 'Updated'}]},
            cancel_after=True,
        )

        result = agent._execute_structured_batch(
            {'id': 'batch-inflight'}, [1], chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'cancelled'
        assert agent.llm.calls == 1
        assert agent.postprocess_calls == 0
        assert agent.platform.saves == []
        assert result['_usage']['total_tokens'] == 12
        assert result['_usage']['api_requests'] == 1
        assert agent.platform.checkpoints[-1][1]['usage']['total_tokens'] == 12

    def test_cancelled_after_save_does_not_start_next_chunk(self):
        agent = _make_batch_agent(
            data={'results': [{'product_id': 1, 'title': 'Updated'}]},
        )
        agent.platform.cancel_on_save = True

        result = agent._execute_structured_batch(
            {'id': 'batch-between-chunks'}, [1, 2],
            chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'cancelled'
        assert agent.llm.calls == 1
        assert len(agent.platform.saves) == 1

    def test_failed_structured_call_never_falls_back_to_react(self):
        agent = _make_batch_agent(error=RuntimeError('structured failed'))
        react_calls = []
        agent._execute_react = lambda *args, **kwargs: react_calls.append(1)

        result = agent._execute_structured_batch(
            {'id': 'batch-failed'}, [1], chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'failed'
        assert result['failed'] == 1
        assert react_calls == []
        assert agent.platform.saves == []

    def test_invalid_structured_result_never_falls_back_to_react(self):
        agent = _make_batch_agent(data={'unexpected': []})
        react_calls = []
        agent._execute_react = lambda *args, **kwargs: react_calls.append(1)

        result = agent._execute_structured_batch(
            {'id': 'batch-invalid'}, [1], chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'failed'
        assert result['_usage']['api_requests'] == 1
        assert react_calls == []
        assert agent.platform.saves == []

    def test_duplicate_selection_stops_before_prefetch_and_llm(self):
        agent = _make_batch_agent()

        result = agent._execute_structured_batch(
            {'id': 'batch-duplicate-selection'}, [1, 1],
            chunk_size=2, max_workers=1,
        )

        assert result['status'] == 'failed'
        assert result['failed'] == 2
        assert agent.prefetch_calls == 0
        assert agent.llm.calls == 0
        assert agent.platform.saves == []

    def test_incomplete_prefetch_stops_before_llm(self):
        agent = _make_batch_agent()
        agent._prefetch_for_structured_batch = lambda product_ids: [
            {'id': product_ids[0], 'title': 'Only one'},
        ]

        result = agent._execute_structured_batch(
            {'id': 'batch-incomplete-prefetch'}, [1, 2],
            chunk_size=2, max_workers=1,
        )

        assert result['status'] == 'failed'
        assert result['failed'] == 2
        assert agent.llm.calls == 0
        assert agent.platform.saves == []

    @pytest.mark.parametrize('results', [
        [{'product_id': 1, 'title': 'Updated'}],
        [
            {'product_id': 1, 'title': 'Updated'},
            {'product_id': 1, 'title': 'Duplicate'},
        ],
        [
            {'product_id': 1, 'title': 'Updated'},
            {'product_id': 999, 'title': 'Foreign'},
        ],
    ])
    def test_result_id_mismatch_blocks_chunk_before_postprocess(self, results):
        agent = _make_batch_agent(data={'results': results})

        result = agent._execute_structured_batch(
            {'id': 'batch-result-scope'}, [1, 2],
            chunk_size=2, max_workers=1,
        )

        assert result['status'] == 'failed'
        assert result['failed'] == 2
        assert agent.postprocess_calls == 0
        assert agent.platform.saves == []

    def test_mapper_updates_must_be_unique_subset_of_chunk(self):
        agent = _make_batch_agent(data={'results': [
            {'product_id': 1, 'title': 'Updated'},
            {'product_id': 2, 'title': 'Updated'},
        ]})
        agent._map_structured_result_to_updates = lambda results: [
            {'product_id': 1, 'title': 'First'},
            {'product_id': 1, 'title': 'Duplicate'},
        ]

        result = agent._execute_structured_batch(
            {'id': 'batch-mapper-scope'}, [1, 2],
            chunk_size=2, max_workers=1,
        )

        assert result['status'] == 'failed'
        assert result['failed'] == 2
        assert agent.platform.saves == []

    def test_unsaved_products_count_as_failed_without_api_error_rows(self):
        agent = _make_batch_agent(data={'results': [
            {'product_id': 1, 'title': 'Updated'},
            {'product_id': 2, 'title': 'Updated'},
        ]})
        agent.platform.batch_update_imported_products = lambda updates: {
            'updated': 0,
            'failed': 0,
            'results': [],
        }

        result = agent._execute_structured_batch(
            {'id': 'batch-zero-confirmed'}, [1, 2],
            chunk_size=2, max_workers=1,
        )

        assert result['status'] == 'failed'
        assert result['processed'] == 2
        assert result['saved'] == 0
        assert result['failed'] == 2

    def test_mapper_subset_marks_omitted_product_failed(self):
        agent = _make_batch_agent(data={'results': [
            {'product_id': 1, 'title': 'Updated'},
            {'product_id': 2, 'title': 'No update'},
        ]})
        agent._map_structured_result_to_updates = lambda results: [{
            'product_id': 1,
            'title': 'Updated',
        }]

        result = agent._execute_structured_batch(
            {'id': 'batch-mapper-subset'}, [1, 2],
            chunk_size=2, max_workers=1,
        )

        assert result['status'] == 'partial'
        assert result['saved'] == 1
        assert result['failed'] == 1

    def test_api_budget_accounts_for_deferred_products(self):
        def response_for_chunk(kwargs):
            products = json.loads(kwargs['prompt'])
            return {'results': [{
                'product_id': product['id'],
                'title': 'Updated',
            } for product in products]}

        agent = _make_batch_agent(data=response_for_chunk)
        agent._run_api_budget_override = 1

        result = agent._execute_structured_batch(
            {'id': 'batch-api-budget'}, [1, 2],
            chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'partial'
        assert result['processed'] == 1
        assert result['saved'] == 1
        assert result['failed'] == 1
        assert agent.llm.calls == 1

    def test_structured_retry_uses_only_chunk_api_allocation(self):
        agent = _make_batch_agent()
        agent._run_api_budget_override = 2

        class AlwaysFailingStructuredLLM:
            def __init__(self):
                self.calls = 0

            @llm_retry(max_retries=3, base_delay=0)
            def structured_output_with_usage(self, **kwargs):
                self.calls += 1
                raise TimeoutError('temporary provider failure')

        agent.llm = AlwaysFailingStructuredLLM()

        result = agent._execute_structured_batch(
            {'id': 'batch-api-retry-budget'}, [1],
            chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'failed'
        assert agent.llm.calls == 2
        assert result['_usage']['api_requests'] == 2

    def test_normal_run_token_budget_keeps_two_structured_chunks(self):
        def response_for_chunk(kwargs):
            products = json.loads(kwargs['prompt'])
            return {'results': [{
                'product_id': product['id'],
                'title': 'Updated',
            } for product in products]}

        agent = _make_batch_agent(data=response_for_chunk)
        agent._run_token_budget_override = 30_000

        result = agent._execute_structured_batch(
            {'id': 'batch-token-budget'}, [1, 2],
            chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'completed'
        max_tokens = [call['max_tokens'] for call in agent.llm.call_kwargs]
        assert len(max_tokens) == 2
        assert all(0 < value <= agent.config.MAX_TOKENS for value in max_tokens)

    def test_small_run_token_budget_defers_without_llm_call(self):
        agent = _make_batch_agent(data={'results': []})
        agent._run_token_budget_override = 100

        result = agent._execute_structured_batch(
            {'id': 'batch-small-token-budget'}, [1, 2],
            chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'partial'
        assert result['processed'] == 0
        assert result['saved'] == 0
        assert result['failed'] == 2
        assert agent.llm.calls == 0
        assert agent.platform.saves == []

    def test_actual_usage_exhaustion_stops_later_structured_chunks(self):
        agent = _make_batch_agent()
        agent._run_token_budget_override = 3000

        class ExhaustingStructuredLLM:
            def __init__(self):
                self.calls = 0

            def structured_output_with_usage(self, **kwargs):
                self.calls += 1
                products = json.loads(kwargs['prompt'])
                return {
                    'data': {'results': [{
                        'product_id': product['id'],
                        'title': 'Updated',
                    } for product in products]},
                    'usage': {
                        'input_tokens': 2800,
                        'output_tokens': 200,
                        'api_requests': 1,
                    },
                }

        agent.llm = ExhaustingStructuredLLM()

        result = agent._execute_structured_batch(
            {'id': 'batch-actual-token-usage'}, [1, 2, 3],
            chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'partial'
        assert result['processed'] == 1
        assert result['saved'] == 1
        assert result['failed'] == 2
        assert result['_usage']['budget_exhausted'] is True
        assert agent.llm.calls == 1

    def test_tool_batch_does_not_start_another_react_chunk_after_cancel(self):
        agent = _make_batch_agent()
        react_calls = []

        def execute_once(*args, **kwargs):
            react_calls.append(1)
            agent.cancelled = True
            return {
                'processed': 1, 'saved': 0, 'results': [],
                '_usage': {'input_tokens': 5, 'output_tokens': 1, 'api_requests': 1},
            }

        agent._execute_react = execute_once

        result = agent._execute_tool_batch(
            {'id': 'tool-batch-cancelled', 'seller_id': 1},
            [1, 2], chunk_size=1, max_workers=1,
        )

        assert result['status'] == 'cancelled'
        assert react_calls == [1]

    def test_react_discards_tool_calls_when_cancelled_during_llm(self):
        agent = _make_batch_agent()
        tool_runs = []
        agent._tools.register(
            'write', 'Write', {'properties': {}, 'required': []},
            handler=lambda: tool_runs.append(1) or {'ok': True},
        )

        class CancellingLLM:
            def chat_with_tools(self, **kwargs):
                agent.cancelled = True
                return {
                    'text': 'apply update',
                    'tool_calls': [
                        {'name': 'write', 'arguments': {}, 'id': 'call-1'},
                    ],
                    'stop_reason': 'tool_use',
                    'usage': {
                        'input_tokens': 6, 'output_tokens': 2, 'api_requests': 1,
                    },
                }

        agent.llm = CancellingLLM()

        result = agent._execute_react({'id': 'react-inflight'})

        assert result['status'] == 'cancelled'
        assert result['_usage']['total_tokens'] == 8
        assert tool_runs == []

    def test_failed_batch_result_marks_task_failed_instead_of_completed(self):
        failed = []
        completed = []

        class PollPlatform:
            def poll_tasks(self, limit):
                return [{'id': 'batch-task', 'task_type': 'seo_batch'}]

            def set_task_id(self, task_id):
                return None

            def start_task(self, task_id):
                return None

            def log_thinking(self, *args, **kwargs):
                return None

            def log_error(self, *args, **kwargs):
                return None

            def fail_task(self, task_id, error, result=None):
                failed.append((task_id, error, result))

            def complete_task(self, task_id, result):
                completed.append((task_id, result))

        agent = object.__new__(_StructuredBatchAgent)
        agent.platform = PollPlatform()
        agent._task_failures = _BoundedFailureTracker()
        agent._default_llm = object()
        agent.llm = agent._default_llm
        agent._configure_task_llm = lambda task: None
        agent.execute_task = lambda task: {
            'status': 'failed', 'message': 'Structured output invalid',
            '_usage': {'total_tokens': 12},
        }

        agent._poll_and_execute()

        assert completed == []
        assert failed[0][0] == 'batch-task'
        assert failed[0][2]['_usage']['total_tokens'] == 12
