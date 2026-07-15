# -*- coding: utf-8 -*-
import json
from types import SimpleNamespace

import pytest

from agents.catalog.category_mapper import CategoryMapperAgent
from agents.catalog.characteristics_filler import CharacteristicsFillerAgent
from agents.platform_client import PlatformClient
from agents.tools import ToolRegistry


_WRITE_TOOL_NAMES = frozenset({
    'update_product', 'batch_update_products',
    'update_imported_product', 'batch_update_imported_products',
})


class _BatchPlatform:
    def __init__(self):
        self.saves = []
        self.checkpoints = []

    def get_task_status(self, task_id):
        return {'task': {'id': task_id, 'status': 'running'}}

    def log_thinking(self, *args, **kwargs):
        return None

    def update_progress(self, *args, **kwargs):
        return None

    def update_checkpoint(self, task_id, checkpoint):
        self.checkpoints.append((task_id, checkpoint))
        return {'ok': True}

    def batch_update_imported_products(self, updates):
        self.saves.append(updates)
        return {
            'updated': len(updates),
            'failed': 0,
            'results': [
                {'product_id': item['product_id'], 'status': 'updated'}
                for item in updates
            ],
        }


def _tool_batch_agent(agent_class, products, result_factory, reference_data):
    agent = object.__new__(agent_class)
    agent.config = SimpleNamespace(
        RUN_API_BUDGET=24,
        RUN_TOKEN_BUDGET=30000,
        MAX_TOKENS=4096,
    )
    agent.platform = _BatchPlatform()
    agent._step_namer = None
    agent._tools = ToolRegistry()
    reference_names = (
        'search_wb_categories', 'get_category_characteristics',
        'search_characteristic_values',
    )
    for name in reference_names + tuple(_WRITE_TOOL_NAMES):
        if name in reference_names and name not in agent_class.tool_allowlist:
            continue
        agent._tools.register(
            name=name,
            description=name,
            parameters={'properties': {}, 'required': []},
            handler=lambda: {'ok': True},
        )

    product_map = {item['id']: item for item in products}
    agent._prefetch_for_structured_batch = lambda product_ids: [
        product_map[product_id] for product_id in product_ids
    ]
    agent._prefetch_reference_data = lambda products_data: reference_data
    observed = {
        'tool_names': [], 'prompts': [],
        'api_budgets': [], 'token_budgets': [],
    }

    def execute_react(
        task, *, tools_override=None, max_iterations_override=None,
        api_budget_override=None, token_budget_override=None,
    ):
        observed['tool_names'].append({
            item['name'] for item in tools_override.get_tool_schemas()
        })
        observed['prompts'].append(task['_prefetched_prompt'])
        observed['api_budgets'].append(api_budget_override)
        observed['token_budgets'].append(token_budget_override)
        chunk_ids = json.loads(task['input_data'])['product_ids']
        return {
            'results': result_factory(chunk_ids),
            '_usage': {
                'input_tokens': 20,
                'output_tokens': 5,
                'api_requests': 1,
            },
        }

    agent._execute_react = execute_react
    return agent, observed


def test_category_tool_batch_uses_python_owned_single_write_per_chunk():
    products = [
        {'id': product_id, 'title': f'Товар {product_id}', 'category': 'Обувь'}
        for product_id in (1, 2, 3)
    ]
    agent, observed = _tool_batch_agent(
        CategoryMapperAgent,
        products,
        lambda ids: [
            {
                'product_id': product_id,
                'subject_id': 1000 + product_id,
                'subject_name': 'Туфли',
                'confidence': 0.9,
                'reasoning': 'Совпадает тип товара',
            }
            for product_id in ids
        ],
        {
            'cached_category_searches': {
                'Обувь': {
                    'categories': [{'subject_name': 'CURRENT_SEARCH_MARKER'}],
                },
                'Игрушки': {
                    'categories': [{'subject_name': 'LEAK_SEARCH_MARKER'}],
                },
            },
        },
    )

    result = agent._execute_tool_batch(
        {'id': 'category-batch', 'seller_id': 7, 'task_type': 'map_batch'},
        [1, 2, 3],
        chunk_size=2,
        max_workers=1,
    )

    assert result['status'] == 'completed'
    assert result['processed'] == 3
    assert result['saved'] == 3
    assert result['failed'] == 0
    assert len(agent.platform.saves) == 2
    assert [len(updates) for updates in agent.platform.saves] == [2, 1]
    assert agent.platform.saves[0][0] == {
        'product_id': 1,
        'wb_subject_id': 1001,
        'mapped_wb_category': 'Туфли',
        'category_confidence': 0.9,
    }
    assert all(not (_WRITE_TOOL_NAMES & names) for names in observed['tool_names'])
    assert all(
        'НЕ вызывай update_imported_product или batch_update_imported_products'
        in prompt
        and '"results"' in prompt
        and 'CURRENT_SEARCH_MARKER' in prompt
        and 'LEAK_SEARCH_MARKER' not in prompt
        for prompt in observed['prompts']
    )
    assert observed['api_budgets'] == [4, 4]
    assert sum(observed['token_budgets']) <= agent.config.RUN_TOKEN_BUDGET


def test_characteristics_tool_batch_serializes_patch_and_writes_once():
    products = [
        {
            'id': 11,
            'title': 'Черные туфли',
            'description': 'Цвет черный',
            'wb_subject_id': 500,
        },
        {
            'id': 12,
            'title': 'Красные туфли',
            'description': 'Цвет красный',
            'wb_subject_id': 500,
        },
    ]
    colors = {11: 'Черный', 12: 'Красный'}
    agent, observed = _tool_batch_agent(
        CharacteristicsFillerAgent,
        products,
        lambda ids: [
            {
                'product_id': product_id,
                'characteristics': {'Цвет товара': colors[product_id]},
                'filled_count': 1,
                'missing': [],
                'confidence': 0.95,
            }
            for product_id in ids
        ],
        {
            'chars_by_subject': {
                500: [{'id': 1, 'name': 'Цвет товара', 'type': 'Строка'}],
                999: [{'id': 2, 'name': 'LEAK_SCHEMA_MARKER', 'type': 'Строка'}],
            },
        },
    )

    result = agent._execute_tool_batch(
        {'id': 'chars-batch', 'seller_id': 7, 'task_type': 'fill_batch'},
        [11, 12],
        chunk_size=15,
        max_workers=1,
    )

    assert result['status'] == 'completed'
    assert result['saved'] == 2
    assert len(agent.platform.saves) == 1
    updates = agent.platform.saves[0]
    assert all(isinstance(item['characteristics'], str) for item in updates)
    assert json.loads(updates[0]['characteristics']) == {
        'Цвет товара': 'Черный',
    }
    assert observed['tool_names'][0] == {'search_characteristic_values'}
    assert '"results"' in observed['prompts'][0]
    assert 'LEAK_SCHEMA_MARKER' not in observed['prompts'][0]
    assert observed['api_budgets'] == [4]
    assert observed['token_budgets'] == [agent.config.RUN_TOKEN_BUDGET]


def test_large_tool_batch_schedules_only_functionally_budgeted_prefix():
    products = [
        {'id': product_id, 'title': f'Товар {product_id}', 'category': 'Обувь'}
        for product_id in range(1, 201)
    ]
    agent, observed = _tool_batch_agent(
        CategoryMapperAgent,
        products,
        lambda ids: [{
            'product_id': product_id,
            'subject_id': 1000,
            'subject_name': 'Туфли',
            'confidence': 0.9,
            'reasoning': 'Совпадает тип товара',
        } for product_id in ids],
        {
            'cached_category_searches': {
                'Обувь': {'categories': [{'subject_name': 'Туфли'}]},
            },
        },
    )

    result = agent._execute_tool_batch(
        {'id': 'large-category-batch', 'seller_id': 7, 'task_type': 'map_batch'},
        list(range(1, 201)),
        chunk_size=15,
        max_workers=1,
    )

    assert result['status'] == 'partial'
    assert result['processed'] == 75
    assert result['saved'] == 75
    assert result['failed'] == 125
    assert len(observed['token_budgets']) == 5
    assert observed['token_budgets'] == [6000] * 5
    assert len(agent.platform.saves) == 5


class _NoCategoryPlatform:
    def __init__(self):
        self.schema_calls = 0

    def get_imported_product(self, product_id):
        return {'product': {'id': product_id, 'title': 'Товар без категории'}}

    def get_category_characteristics(self, subject_id, required_only=False):
        self.schema_calls += 1
        raise AssertionError('Schema must not be requested without wb_subject_id')


class _FailIfCalledLlm:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, **kwargs):
        self.calls += 1
        raise AssertionError('LLM must not run without wb_subject_id')

    def chat(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError('LLM must not run without wb_subject_id')


def test_single_characteristics_without_subject_stops_before_llm():
    agent = object.__new__(CharacteristicsFillerAgent)
    agent.platform = _NoCategoryPlatform()
    agent.llm = _FailIfCalledLlm()
    agent.config = SimpleNamespace(
        RUN_API_BUDGET=24,
        RUN_TOKEN_BUDGET=30000,
        MAX_TOKENS=4096,
    )
    agent._tools = ToolRegistry()
    agent._step_namer = None

    result = agent.execute_task({
        'id': 'missing-subject',
        'task_type': 'fill_single',
        'input_data': json.dumps({'imported_product_id': 41}),
    })

    assert result['status'] == 'needs_clarification'
    assert result['reference_data_blocked'] is True
    assert result['failed'] == 1
    assert result['reference_status']['reason'] == 'category_required'
    assert result['_usage'].get('api_requests', 0) == 0
    assert result['_usage'].get('total_tokens', 0) == 0
    assert agent.llm.calls == 0
    assert agent.platform.schema_calls == 0


def test_catalog_mappers_reject_cross_chunk_product_ids():
    mapper = object.__new__(CategoryMapperAgent)

    with pytest.raises(ValueError, match='outside chunk'):
        mapper._map_tool_batch_result_to_updates(
            {
                'results': [{
                    'product_id': 999,
                    'subject_id': 1,
                    'subject_name': 'Категория',
                    'confidence': 1.0,
                }],
            },
            [{'id': 1}],
        )


def test_tool_batch_missing_prefetch_row_fails_before_llm_or_write():
    agent, observed = _tool_batch_agent(
        CategoryMapperAgent,
        [{'id': 1, 'title': 'Товар 1', 'category': 'Обувь'}],
        lambda ids: [],
        {'cached_category_searches': {}},
    )
    agent._prefetch_for_structured_batch = lambda product_ids: [
        {'id': 1, 'title': 'Товар 1', 'category': 'Обувь'},
    ]

    result = agent._execute_tool_batch(
        {'id': 'missing-prefetch', 'seller_id': 7, 'task_type': 'map_batch'},
        [1, 2],
        chunk_size=15,
        max_workers=1,
    )

    assert result['status'] == 'failed'
    assert result['processed'] == 0
    assert result['saved'] == 0
    assert result['failed'] == 2
    assert result['errors'][0]['missing_product_ids'] == [2]
    assert observed['prompts'] == []
    assert agent.platform.saves == []


def test_tool_batch_duplicate_selection_fails_before_prefetch_and_llm():
    agent, observed = _tool_batch_agent(
        CategoryMapperAgent,
        [{'id': 1, 'title': 'Товар 1', 'category': 'Обувь'}],
        lambda ids: [],
        {'cached_category_searches': {}},
    )
    prefetch_calls = []
    agent._prefetch_for_structured_batch = (
        lambda product_ids: prefetch_calls.append(product_ids) or []
    )

    result = agent._execute_tool_batch(
        {'id': 'duplicate-selection', 'seller_id': 7, 'task_type': 'map_batch'},
        [1, 1],
        chunk_size=15,
        max_workers=1,
    )

    assert result['status'] == 'failed'
    assert result['failed'] == 2
    assert 'Повторяющийся product_id' in result['message']
    assert prefetch_calls == []
    assert observed['prompts'] == []
    assert agent.platform.saves == []


def test_reference_tool_chunk_has_four_request_hard_cap():
    agent, observed = _tool_batch_agent(
        CategoryMapperAgent,
        [{'id': 1, 'title': 'Туфли', 'category': 'Обувь'}],
        lambda ids: [{
            'product_id': 1,
            'subject_id': 1001,
            'subject_name': 'Туфли',
            'confidence': 0.9,
        }],
        {'cached_category_searches': {'Обувь': {'categories': []}}},
    )

    result = agent._execute_tool_batch(
        {'id': 'hard-cap', 'seller_id': 7, 'task_type': 'map_batch'},
        [1],
        chunk_size=15,
        max_workers=1,
    )

    assert result['status'] == 'completed'
    assert observed['api_budgets'] == [4]


def test_global_budget_smaller_than_safe_chunk_starts_no_llm_call():
    agent, observed = _tool_batch_agent(
        CategoryMapperAgent,
        [{'id': 1, 'title': 'Туфли', 'category': 'Обувь'}],
        lambda ids: [],
        {'cached_category_searches': {'Обувь': {'categories': []}}},
    )
    agent.config.RUN_API_BUDGET = 1

    result = agent._execute_tool_batch(
        {'id': 'too-small-budget', 'seller_id': 7, 'task_type': 'map_batch'},
        [1],
        chunk_size=15,
        max_workers=1,
    )

    assert result['status'] == 'partial'
    assert result['failed'] == 1
    assert observed['api_budgets'] == []
    assert agent.platform.saves == []


class _BulkCategoryPlatform:
    def __init__(self):
        self.calls = []

    def search_categories_batch(self, queries, limit=20):
        self.calls.append((list(queries), limit))
        status = {'usable': True, 'available': True, 'stale': False}
        results = [
            {
                'query': query,
                'categories': [{
                    'subject_id': index + 1,
                    'subject_name': f'WB {query}',
                }],
                'count': 1,
                'reference_status': status,
            }
            for index, query in enumerate(queries)
        ]
        return {
            'results': results,
            'count': len(results),
            'reference_status': status,
        }

    def search_categories(self, query, limit=20):
        raise AssertionError('Batch prefetch must not use single search')


class _BulkCharacteristicsPlatform:
    def __init__(self):
        self.calls = []

    def get_category_characteristics_batch(
        self, subject_ids, required_only=False,
    ):
        self.calls.append((list(subject_ids), required_only))
        return {
            'results': [
                {
                    'subject_id': subject_id,
                    'characteristics': [{
                        'charc_id': subject_id,
                        'name': f'Поле {subject_id}',
                    }],
                    'count': 1,
                    'reference_status': {
                        'usable': True,
                        'available': True,
                        'stale': False,
                    },
                }
                for subject_id in subject_ids
            ],
            'count': len(subject_ids),
        }

    def get_category_characteristics(self, subject_id, required_only=False):
        raise AssertionError('Batch prefetch must not use single schema calls')


def test_category_prefetch_coalesces_ten_casefolded_scopes_into_one_call():
    platform = _BulkCategoryPlatform()
    agent = object.__new__(CategoryMapperAgent)
    agent.platform = platform
    products = [
        {'id': index + 1, 'category': f'Категория {index}', 'title': 'Товар'}
        for index in range(10)
    ]
    products.append({
        'id': 11,
        'category': 'категория 0',
        'title': 'Дубликат регистра',
    })

    result = agent._prefetch_reference_data(products)

    assert len(platform.calls) == 1
    queries, limit = platform.calls[0]
    assert len(queries) == 10
    assert queries[0] == 'Категория 0'
    assert 'категория 0' not in queries
    assert limit == 10
    assert len(result['cached_category_searches']) == 10


def test_characteristics_prefetch_coalesces_ten_scopes_into_one_call():
    platform = _BulkCharacteristicsPlatform()
    agent = object.__new__(CharacteristicsFillerAgent)
    agent.platform = platform
    products = [
        {'id': index + 1, 'wb_subject_id': 1000 + index, 'title': 'Товар'}
        for index in range(10)
    ]

    result = agent._prefetch_reference_data(products)

    assert platform.calls == [([1000 + index for index in range(10)], False)]
    assert len(result['chars_by_subject']) == 10


def test_platform_client_rejects_reordered_category_batch_response():
    client = object.__new__(PlatformClient)

    def request(method, path, **kwargs):
        queries = kwargs['json']['queries']
        status = {'usable': True}
        results = [
            {
                'query': query,
                'categories': [],
                'count': 0,
                'reference_status': status,
            }
            for query in reversed(queries)
        ]
        return {
            'results': results,
            'count': len(results),
            'reference_status': status,
        }

    client._request = request

    with pytest.raises(ValueError, match='request order'):
        client.search_categories_batch(['Обувь', 'Одежда'], limit=10)


def test_platform_client_rejects_reordered_schema_batch_response():
    client = object.__new__(PlatformClient)

    def request(method, path, **kwargs):
        subject_ids = kwargs['json']['subject_ids']
        results = [
            {
                'subject_id': subject_id,
                'characteristics': [],
                'count': 0,
                'reference_status': {'usable': False},
            }
            for subject_id in reversed(subject_ids)
        ]
        return {'results': results, 'count': len(results)}

    client._request = request

    with pytest.raises(ValueError, match='request order'):
        client.get_category_characteristics_batch([1001, 1002])


def test_platform_client_rejects_reordered_characteristic_value_search():
    client = object.__new__(PlatformClient)

    def request(method, path, **kwargs):
        queries = kwargs['json']['queries']
        return {
            'count': len(queries),
            'results': [{**item, 'values': []} for item in reversed(queries)],
        }

    client._request = request
    with pytest.raises(ValueError, match='order mismatch'):
        client.search_characteristic_values_batch([
            {'subject_id': 1001, 'charc_id': 1, 'query': 'крас'},
            {'subject_id': 1002, 'charc_id': 2, 'query': 'син'},
        ])
