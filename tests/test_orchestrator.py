# -*- coding: utf-8 -*-
"""
Тесты для оркестратора — keyword matching и pipeline resolution.
"""
import pytest

from agents.catalog.orchestrator import (
    resolve_agents_from_text,
    PIPELINES,
    KEYWORD_AGENTS,
)


class TestResolveAgentsFromText:
    def test_seo_keywords(self):
        agents = resolve_agents_from_text('оптимизируй SEO заголовки')
        # Должен вернуть pipeline seo_boost
        agent_names = [a['agent'] for a in agents]
        assert 'seo-writer' in agent_names

    def test_category_keywords(self):
        agents = resolve_agents_from_text('определи категории товаров')
        agent_names = [a['agent'] for a in agents]
        assert 'category-mapper' in agent_names

    def test_brand_keywords(self):
        agents = resolve_agents_from_text('нормализуй бренды')
        agent_names = [a['agent'] for a in agents]
        assert 'brand-resolver' in agent_names

    def test_moderation_keywords(self):
        agents = resolve_agents_from_text('проверь на стоп-слова и модерацию')
        # 'провер' -> audit pipeline OR card-doctor
        agent_names = [a['agent'] for a in agents]
        assert 'card-doctor' in agent_names

    def test_import_returns_full_prepare(self):
        agents = resolve_agents_from_text('импортируй товары')
        # 'импорт' -> full_prepare pipeline
        assert len(agents) >= 4  # full_prepare has 4+ steps

    def test_unknown_text_returns_full_prepare(self):
        agents = resolve_agents_from_text('сделай что-нибудь полезное')
        # Unknown -> default full_prepare
        expected_steps = PIPELINES['full_prepare']['steps']
        assert agents == expected_steps

    def test_batch_uses_batch_types(self):
        agents = resolve_agents_from_text('нормализуй бренды', is_batch=True)
        brand_agent = next(a for a in agents if a['agent'] == 'brand-resolver')
        assert brand_agent['task_type'] == 'resolve_batch'

    def test_single_uses_single_types(self):
        agents = resolve_agents_from_text('нормализуй бренды', is_batch=False)
        brand_agent = next(a for a in agents if a['agent'] == 'brand-resolver')
        assert brand_agent['task_type'] == 'resolve_single'

    def test_no_duplicates(self):
        agents = resolve_agents_from_text('сео заголовок seo описание текст')
        agent_names = [a['agent'] for a in agents]
        assert len(agent_names) == len(set(agent_names))

    def test_multiple_agents_from_text(self):
        agents = resolve_agents_from_text('бренды и размеры')
        agent_names = {a['agent'] for a in agents}
        assert 'brand-resolver' in agent_names
        assert 'size-normalizer' in agent_names


class TestPipelines:
    def test_all_pipelines_have_steps(self):
        for name, pipeline in PIPELINES.items():
            assert 'steps' in pipeline, f"Pipeline {name} has no steps"
            assert len(pipeline['steps']) > 0, f"Pipeline {name} has empty steps"

    def test_all_pipeline_steps_have_agent(self):
        for name, pipeline in PIPELINES.items():
            for step in pipeline['steps']:
                assert 'agent' in step, f"Pipeline {name}: step missing 'agent'"
                assert 'task_type' in step, f"Pipeline {name}: step missing 'task_type'"

    def test_full_prepare_order(self):
        steps = PIPELINES['full_prepare']['steps']
        agents = [s['agent'] for s in steps]
        # Category must come before characteristics and SEO
        assert agents.index('category-mapper') < agents.index('characteristics-filler')
        assert agents.index('characteristics-filler') < agents.index('seo-writer')
        assert agents.index('seo-writer') < agents.index('card-doctor')
