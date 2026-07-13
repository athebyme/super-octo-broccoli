# -*- coding: utf-8 -*-
"""Focused usage accounting tests for the unified semantic planner."""
from agents.unified import UnifiedSellerAgent


class _PlannerLLM:
    def structured_output_with_usage(self, **kwargs):
        return {
            'data': {
                'title': 'Проверка системы',
                'summary': 'Read-only audit',
                'risk': 'read',
                'confidence': 0.9,
                'scope_label': 'Настройки',
                'steps': [{
                    'skill': 'system-context',
                    'label': 'Проверить настройки',
                    'params': {},
                }],
            },
            'usage': {
                'input_tokens': 200,
                'output_tokens': 40,
                'cache_hit_tokens': 128,
                'cache_miss_tokens': 72,
                'estimated_cost_usd': 0.0002,
            },
        }


class _BroadContentPlannerLLM:
    def structured_output_with_usage(self, **kwargs):
        return {
            'data': {
                'title': 'Контент карточки',
                'summary': 'Rewrite content',
                'risk': 'write',
                'confidence': 0.9,
                'scope_label': 'Карточка #10',
                'steps': [{
                    'skill': 'content-writer',
                    'label': 'Название и описание',
                    'params': {'fields': ['title', 'description']},
                }],
            },
            'usage': {'input_tokens': 20, 'output_tokens': 10, 'api_requests': 1},
        }


def test_semantic_planner_returns_cache_usage():
    agent = object.__new__(UnifiedSellerAgent)
    agent.llm = _PlannerLLM()
    agent.system_prompt = UnifiedSellerAgent.system_prompt

    result = agent._plan_request(
        {'id': 'plan-1'},
        {'text': 'Проверь настройки API', 'product_ids': []},
    )

    assert result['status'] == 'completed'
    assert result['_usage']['input_tokens'] == 200
    assert result['_usage']['cache_hit_tokens'] == 128
    assert result['_usage']['cache_hit_rate'] == 0.64
    assert result['_usage']['estimated_cost_usd'] == 0.0002


def test_semantic_planner_cannot_broaden_explicit_content_fields():
    agent = object.__new__(UnifiedSellerAgent)
    agent.llm = _BroadContentPlannerLLM()
    agent.system_prompt = UnifiedSellerAgent.system_prompt
    result = agent._plan_request(
        {'id': 'plan-content'},
        {
            'text': 'Улучши только описание, название не меняй',
            'product_ids': [10],
            'entity_scope': {'kind': 'product', 'ids': [10]},
        },
    )
    assert result['status'] == 'completed'
    assert result['steps'][0]['params']['fields'] == ['description']
