# -*- coding: utf-8 -*-
"""Focused contracts for the bounded semantic fallback and plan projection."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from agents.unified import CatalogQuerySkill, KnowledgeQuerySkill, UnifiedSellerAgent
from services.agent_harness import (
    build_plan, direct_response, _needs_semantic_planner, sync_run_message,
)


class _PlannerLLM:
    def __init__(self, data):
        self.data = data
        self.kwargs = None

    def structured_output_with_usage(self, **kwargs):
        self.kwargs = kwargs
        return {
            'data': self.data,
            'usage': {'input_tokens': 220, 'output_tokens': 45, 'api_requests': 1},
        }


def _plan_with(llm, input_data):
    agent = object.__new__(UnifiedSellerAgent)
    agent.llm = llm
    agent.system_prompt = UnifiedSellerAgent.system_prompt
    return agent._plan_request({'id': 'planning-task'}, input_data)


def _planned_step(skill, params=None, risk='read', confidence=0.9):
    return {
        'title': 'Узкий запрос',
        'summary': 'Проверить только запрошенное условие.',
        'risk': risk,
        'confidence': confidence,
        'scope_label': 'Карточки по условию',
        'clarification_question': '',
        'steps': [{
            'skill': skill,
            'label': 'Проверить условие',
            'params': params or {},
        }],
    }


def test_every_short_unresolved_goal_uses_semantic_fallback():
    assert _needs_semantic_planner('покажи просевшие карточки')
    assert _needs_semantic_planner('жив ли коннект с WB?')
    assert not _needs_semantic_planner('   ')
    assert build_plan('покажи просевшие карточки') is None
    assert build_plan('покажи просевшие карточки на WB') is None
    assert build_plan('покажи карточки со слабыми описаниями') is None
    assert build_plan('какие категории требуют внимания?') is None
    assert build_plan('покажи все карточки') is not None
    assert build_plan('сколько у меня товаров на WB?') is not None
    assert direct_response('помощь с убыточными товарами') is None
    assert direct_response('привет!') is not None


def test_explicit_knowledge_question_is_deterministic_read_plan():
    plan = build_plan('Что сказано в правилах WB про внешние ссылки?')
    assert plan is not None
    assert plan.risk == 'read'
    assert plan.steps == [{
        'agent': 'knowledge-query', 'task_type': 'answer_knowledge',
        'label': 'Поиск в проверенных инструкциях',
        'params': {'query': 'Что сказано в правилах WB про внешние ссылки?'},
    }]


def test_semantic_knowledge_query_is_bounded_and_cited():
    llm = _PlannerLLM(_planned_step('knowledge-query', {
        'query': 'правила WB про внешние ссылки',
        'seller_id': 999,
    }))
    planned = _plan_with(llm, {
        'text': 'разрешены ли внешние ссылки в описании',
        'product_ids': [],
        'entity_scope': {'kind': 'imported_product', 'ids': []},
    })
    assert planned['status'] == 'completed'
    assert planned['risk'] == 'read'
    assert planned['steps'][0]['params'] == {'query': 'правила WB про внешние ссылки'}

    class _KnowledgePlatform:
        def search_knowledge(self, seller_id, query, limit=6, max_chars=6000):
            assert seller_id == 7
            assert limit == 6
            assert max_chars == 6000
            return {
                'has_results': True,
                'context': '[K1] Правила\nИсточник: https://example.test/rule\nСсылки запрещены.',
                'citations': [{
                    'citation_id': 'K1', 'title': 'Правила', 'version': '1',
                    'source_uri': 'https://example.test/rule', 'heading': 'Описание',
                    'source_key': 'rule',
                }],
                'hits': [{'citation_id': 'K1', 'snippet': 'Ссылки запрещены.'}],
                'retrieval': {'mode': 'fts_prefix_trigram'},
            }

    class _KnowledgeLLM:
        def structured_output_with_usage(self, **kwargs):
            assert kwargs['max_tokens'] == 700
            assert '<retrieved_context>' in kwargs['prompt']
            return {
                'data': {
                    'answer': 'Внешние ссылки запрещены.',
                    'citation_ids': ['K1'],
                    'insufficient_context': False,
                },
                'usage': {'input_tokens': 100, 'output_tokens': 20, 'api_requests': 1},
            }

    skill = object.__new__(KnowledgeQuerySkill)
    skill.platform = _KnowledgePlatform()
    skill.llm = _KnowledgeLLM()
    skill.system_prompt = KnowledgeQuerySkill.system_prompt
    skill._run_token_budget_override = 10000
    result = skill.execute_task({
        'seller_id': 7,
        'input_data': json.dumps({'params': planned['steps'][0]['params']}),
    })
    assert 'Внешние ссылки запрещены.' in result['message']
    assert '[K1]' in result['message']
    assert result['_usage']['mode'] == 'hybrid_rag_flash'


def test_semantic_catalog_query_is_bounded_typed_and_one_model_call():
    llm = _PlannerLLM(_planned_step('catalog-query', {
        'entity_kind': 'product',
        'quality_max': 55,
        'stock_state': 'out_of_stock',
        'condition_label': 'просевшие и закончившиеся на WB',
        'limit': 999,
        'seller_id': 999,
        'raw_sql': 'drop table products',
    }, risk='write'))
    result = _plan_with(llm, {
        'text': 'покажи просевшие карточки, которые закончились',
        'product_ids': [],
        'entity_scope': {'kind': 'imported_product', 'ids': []},
        'dialog_context': [{'role': 'user', 'content': 'Речь про карточки на WB'}],
    })

    assert result['status'] == 'completed'
    assert result['risk'] == 'read'
    params = result['steps'][0]['params']
    assert params == {
        'entity_kind': 'product',
        'limit': 200,
        'condition_label': 'просевшие и закончившиеся на WB',
        'polish': False,
        'stock_state': 'out_of_stock',
        'quality_max': 55.0,
    }
    assert llm.kwargs['max_tokens'] == 1200
    assert 'Речь про карточки на WB' in llm.kwargs['prompt']
    assert len(llm.kwargs['prompt']) < 8000
    assert 'drop table products' not in str(params)

    class _NoSecondCall:
        def chat_with_usage(self, **kwargs):
            raise AssertionError('semantic SQL execution must not call the LLM again')

    class _CatalogPlatform:
        def query_products(self, seller_id, **kwargs):
            return {'total': 3, 'products': [{'id': 1}], 'truncated': True}

    skill = object.__new__(CatalogQuerySkill)
    skill.llm = _NoSecondCall()
    skill.platform = _CatalogPlatform()
    executed = skill.execute_task({
        'seller_id': 7,
        'input_data': json.dumps({'params': params}),
    })
    assert executed['message'].endswith(': 3.')
    assert executed['_usage']['api_requests'] == 0


def test_semantic_planner_cannot_cross_product_entity_boundary():
    llm = _PlannerLLM(_planned_step('seo-writer'))
    result = _plan_with(llm, {
        'text': 'сделай эту карточку заметнее',
        'product_ids': [41],
        'entity_scope': {'kind': 'product', 'ids': [41]},
    })
    assert result['status'] == 'needs_clarification'
    assert result['_usage']['api_requests'] == 1


def test_semantic_planner_enforces_global_no_write_constraint():
    llm = _PlannerLLM(_planned_step('content-writer', {
        'fields': ['title', 'description'],
    }, risk='read'))
    result = _plan_with(llm, {
        'text': 'ничего не меняй, только предложи новое описание',
        'product_ids': [41],
        'entity_scope': {'kind': 'product', 'ids': [41]},
        'allow_writes': False,
    })
    assert result['status'] == 'needs_clarification'


def test_named_write_scope_never_expands_to_the_whole_catalog():
    assert build_plan('исправь бренды карточек Андрея') is None

    unsafe = _PlannerLLM(_planned_step('brand-resolver', risk='write'))
    rejected = _plan_with(unsafe, {
        'text': 'исправь бренды карточек Андрея',
        'product_ids': [],
        'entity_scope': {'kind': 'imported_product', 'ids': []},
        'named_scope_hint': 'андрея',
    })
    assert rejected['status'] == 'needs_clarification'

    planned = _planned_step('supplier-audit', {'supplier_query': 'другое имя'})
    planned['steps'].append({
        'skill': 'brand-resolver', 'label': 'Исправить бренды', 'params': {},
    })
    safe = _plan_with(_PlannerLLM(planned), {
        'text': 'исправь бренды карточек Андрея',
        'product_ids': [],
        'entity_scope': {'kind': 'imported_product', 'ids': []},
        'named_scope_hint': 'андрея',
    })
    assert safe['status'] == 'completed'
    assert safe['risk'] == 'write'
    assert safe['steps'][0]['params']['supplier_query'] == 'андрея'


def test_semantic_write_requires_selected_or_explicit_global_scope():
    planned = _planned_step('brand-resolver', risk='write')
    scoped_input = {
        'text': 'приведи бренды в порядок',
        'product_ids': [],
        'entity_scope': {'kind': 'imported_product', 'ids': []},
    }
    rejected = _plan_with(_PlannerLLM(planned), scoped_input)
    assert rejected['status'] == 'needs_clarification'
    assert 'всему каталогу' in rejected['clarification_question']

    accepted = _plan_with(_PlannerLLM(planned), {
        **scoped_input,
        'text': 'приведи бренды в порядок во всём каталоге',
        'allow_global_write': True,
    })
    assert accepted['status'] == 'completed'
    assert accepted['risk'] == 'write'


class _ProjectedMessage:
    def __init__(self, result):
        self.task = SimpleNamespace(
            id='planning-task', status='completed', progress_percent=100,
            current_step_label='План готов', duration_seconds=1,
            error_message=None, get_result=lambda: result,
        )
        self.kind = 'run'
        self.content = 'Анализирую цель.'
        self.task_id = 'planning-task'
        self.metadata = {
            'phase': 'planning', 'product_ids': [], 'request_text': 'проверь API',
            'model_policy': {}, 'entity_scope': {'kind': 'imported_product', 'ids': []},
        }
        self.conversation = object()
        self.updated_at = None

    def get_metadata(self):
        return dict(self.metadata)

    def set_metadata(self, value):
        self.metadata = dict(value)


def test_semantic_read_plan_auto_starts_but_write_plan_waits_for_confirmation():
    base = {
        'status': 'completed', 'title': 'Проверка', 'summary': 'Проверить API.',
        'confidence': 0.9, 'scope_label': 'Настройки',
        'steps': [{
            'agent': 'system-query', 'task_type': 'read_system_setting',
            'label': 'Проверить API', 'params': {'kind': 'api_status'},
        }],
        '_usage': {'api_requests': 1, 'total_tokens': 265},
    }
    read_message = _ProjectedMessage({**base, 'risk': 'read'})
    with patch('services.agent_harness.snapshot_count', return_value=0), patch(
        'services.agent_harness._create_run_from_plan', return_value=object(),
    ) as create_run:
        assert sync_run_message(read_message)
    create_run.assert_called_once_with(read_message.conversation, read_message)
    assert read_message.metadata['requires_approval'] is False
    assert read_message.metadata['auto_started'] is True
    assert read_message.metadata['planning_usage']['api_requests'] == 1

    write_message = _ProjectedMessage({**base, 'risk': 'write'})
    with patch('services.agent_harness.snapshot_count', return_value=0), patch(
        'services.agent_harness._create_run_from_plan', return_value=object(),
    ) as create_run:
        assert sync_run_message(write_message)
    create_run.assert_not_called()
    assert write_message.metadata['requires_approval'] is True
    assert write_message.metadata['status'] == 'pending_approval'
