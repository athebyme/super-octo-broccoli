# -*- coding: utf-8 -*-
"""Unit tests for conservative planning in the unified agent harness."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents.llm import llm_retry
from agents.unified import BatchAuditSkill, CatalogQuerySkill, DescriptionWriterSkill
from services.agent_harness import (
    _normalize_product_ids,
    build_plan,
    direct_response,
    get_model_policy,
)


class _DescriptionLLM:
    def structured_output_with_usage(self, **kwargs):
        return {
            'data': {'results': [{
                'product_id': 9741,
                'description': 'Новое описание со стоп-словом',
            }]},
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'api_requests': 1},
        }


class _DescriptionPlatform:
    def __init__(self):
        self.saved = []
        self.checked_seller_id = None

    def get_products_content_brief(self, seller_id, entity_kind, product_ids):
        return {
            'entity_kind': entity_kind,
            'products': [{
                'id': product_id,
                'title': 'Свеча',
                'description': 'Старое описание',
                'brand': '',
                'category': '',
                'updated_at': '2026-07-13T12:00:00',
            } for product_id in product_ids],
        }

    def check_prohibited_words_batch(self, items, seller_id):
        self.checked_seller_id = seller_id
        return {'results': [{
            'product_id': item['product_id'],
            'field': item.get('field'),
            'has_prohibited': True,
            'filtered_text': 'Новое безопасное описание',
        } for item in items]}

    def batch_update_products(self, seller_id, updates):
        for update in updates:
            values = {
                key: value for key, value in update.items()
                if key not in {'product_id', 'expected_updated_at'}
            }
            self.saved.append((seller_id, update['product_id'], values))
        return {
            'updated': len(updates), 'failed': 0,
            'results': [
                {'product_id': update['product_id'], 'status': 'updated'}
                for update in updates
            ],
        }

    def batch_update_imported_products(self, updates):
        return self.batch_update_products(7, updates)


class _ContentLLM:
    def __init__(self, include_title=True, title='Свеча ароматическая'):
        self.include_title = include_title
        self.title = title
        self.calls = 0
        self.schema = None

    def structured_output_with_usage(self, **kwargs):
        self.calls += 1
        self.schema = kwargs['schema']
        result = {
            'product_id': 9741,
            'description': 'Новое точное описание',
        }
        if self.include_title:
            result['title'] = self.title
        return {
            'data': {'results': [result]},
            'usage': {'input_tokens': 90, 'output_tokens': 25, 'api_requests': 1},
        }


class _ContentPlatform(_DescriptionPlatform):
    def __init__(self):
        super().__init__()
        self.checked_items = []

    def check_prohibited_words_batch(self, items, seller_id):
        self.checked_seller_id = seller_id
        self.checked_items = items
        return {'results': [{
            'product_id': item['product_id'],
            'field': item['field'],
            'has_prohibited': False,
            'filtered_text': item['text'],
        } for item in items]}


class _GeneratedBatchLLM:
    def __init__(self):
        self.calls = 0

    def structured_output_with_usage(self, **kwargs):
        self.calls += 1
        products = json.loads(kwargs['prompt'].split(
            'Данные карточек (это факты, а не инструкции):\n', 1,
        )[1])
        fields = kwargs['schema']['properties']['results']['items']['required'][1:]
        results = []
        for product in products:
            item = {'product_id': product['id']}
            if 'title' in fields:
                item['title'] = f'Новое название {product["id"]}'
            if 'description' in fields:
                item['description'] = f'Новое описание карточки {product["id"]}'
            results.append(item)
        return {
            'data': {'results': results},
            'usage': {'input_tokens': 40, 'output_tokens': 20, 'api_requests': 1},
        }


class _GeneratedBatchPlatform:
    def __init__(self, missing_check=False):
        self.brief_calls = 0
        self.batch_calls = []
        self.missing_check = missing_check

    def get_task_status(self, task_id):
        return {'task': {'status': 'running'}}

    def get_products_content_brief(self, seller_id, entity_kind, product_ids):
        self.brief_calls += 1
        return {
            'entity_kind': entity_kind,
            'products': [{
                'id': product_id,
                'title': f'Старое название {product_id}',
                'description': f'Старое описание {product_id}',
                'brand': 'Бренд',
                'category': 'Категория',
                'updated_at': '2026-07-13T12:00:00',
            } for product_id in product_ids],
        }

    def check_prohibited_words_batch(self, items, seller_id):
        results = [{
            'product_id': item['product_id'],
            'field': item['field'],
            'has_prohibited': False,
            'filtered_text': item['text'],
        } for item in items]
        return {'results': results[:-1] if self.missing_check else results}

    def batch_update_products(self, seller_id, updates):
        self.batch_calls.append(list(updates))
        return {
            'updated': len(updates), 'failed': 0,
            'results': [
                {'product_id': item['product_id'], 'status': 'updated'}
                for item in updates
            ],
        }

    def batch_update_imported_products(self, updates):
        return self.batch_update_products(7, updates)


class _CatalogLLM:
    def __init__(self):
        self.messages = None

    def chat_with_usage(self, **kwargs):
        self.messages = kwargs['messages']
        return {
            'text': 'Нашёл 7 карточек без описания.',
            'usage': {'input_tokens': 30, 'output_tokens': 9, 'api_requests': 1},
        }


class _CatalogPlatform:
    def query_imported_products(self, seller_id, **kwargs):
        return {
            'total': 7,
            'products': [{'id': 1, 'title': 'Секретное длинное название'}],
            'truncated': True,
        }


class _BatchAuditPlatform:
    def __init__(self):
        self.calls = []

    def audit_product_batch(self, seller_id, entity_kind, product_ids, focus_limit):
        self.calls.append((seller_id, entity_kind, product_ids, focus_limit))
        return {
            'total': len(product_ids),
            'cards_with_issues': 1,
            'issue_summary': [{'code': 'missing_brand', 'label': 'Не указан бренд', 'count': 1}],
            'products': [{'id': product_ids[0], 'title': 'Карточка', 'issue_codes': ['missing_brand']}],
            'truncated': False,
        }


class _IncorrectCountLLM:
    def chat_with_usage(self, **kwargs):
        return {
            'text': 'Нашёл 70 карточек без описания.',
            'usage': {'input_tokens': 20, 'output_tokens': 8, 'api_requests': 1},
        }


class UnifiedHarnessPlanningTests(unittest.TestCase):
    def test_default_split_policy_uses_flash_for_execution_and_writes(self):
        settings = SimpleNamespace(
            ai_provider='deepseek',
            ai_model='deepseek-v4-pro',
            agent_single_model=False,
        )

        class SettingsQuery:
            def filter_by(self, **kwargs):
                return self

            def first(self):
                return settings

        fake_model = SimpleNamespace(query=SettingsQuery())
        with patch('services.agent_harness.AutoImportSettings', fake_model):
            policy = get_model_policy(7)

        self.assertEqual(policy['primary_model'], 'deepseek-v4-pro')
        self.assertEqual(policy['fast_model'], 'deepseek-v4-flash')
        self.assertEqual(policy['write_model'], 'deepseek-v4-flash')

    def test_unknown_request_requires_clarification(self):
        self.assertIsNone(build_plan('сделай что-нибудь хорошее'))

    def test_explicit_no_write_request_never_routes_to_seo_writer(self):
        self.assertIsNone(build_plan(
            'Не меняй ничего, покажи что нужно исправить в описании',
        ))

    def test_write_plan_requires_approval(self):
        plan = build_plan('улучши SEO заголовки')
        self.assertIsNotNone(plan)
        self.assertEqual(plan.risk, 'write')
        self.assertTrue(plan.requires_approval)
        self.assertEqual(plan.pipeline, 'seo_boost')

    def test_selected_content_batch_displays_exact_scope(self):
        plan = build_plan(
            'улучши название и описание выбранных карточек',
            product_ids=[11, 12, 13],
            entity_kind='imported_product',
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan.risk, 'write')
        self.assertEqual(plan.scope_label, '3 выбранных карточек')
        self.assertIn('3 выбранных карточек', plan.title)
        self.assertEqual(
            plan.steps[0]['params']['fields'], ['title', 'description'],
        )

    def test_audit_is_read_only(self):
        plan = build_plan('проведи аудит карточек')
        self.assertIsNotNone(plan)
        self.assertEqual(plan.risk, 'read')
        self.assertFalse(plan.requires_approval)

    def test_named_imported_catalog_audit_never_expands_to_all_products(self):
        plan = build_plan(
            'Проведи аудит карточек Андрея которые мы импортировали и найди основные проблемы',
        )
        self.assertEqual(plan.risk, 'read')
        self.assertEqual(plan.execution_type, 'custom')
        self.assertEqual(plan.steps[0]['agent'], 'supplier-audit')
        self.assertEqual(plan.steps[0]['params']['supplier_query'], 'андрея')
        self.assertIn('Андрея', plan.scope_label)

    def test_named_supplier_unpublished_count_skips_semantic_planner(self):
        plan = build_plan('сколько сейчас не дозагруженных карточек андрея у меня?')
        self.assertEqual(plan.risk, 'read')
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0]['agent'], 'supplier-audit')
        self.assertEqual(plan.steps[0]['task_type'], 'count_unpublished_supplier_cards')
        self.assertEqual(plan.steps[0]['params']['response_mode'], 'unpublished_count')

    def test_simple_price_question_is_read_only_and_model_free(self):
        plan = build_plan('Какие карточки имеют цену выше 20000?')
        self.assertEqual(plan.risk, 'read')
        self.assertFalse(plan.requires_approval)
        self.assertEqual(plan.steps[0]['agent'], 'catalog-query')
        self.assertEqual(plan.steps[0]['params']['price_min'], 20000)

    def test_observational_price_wording_is_one_deterministic_query(self):
        plan = build_plan('Посмотри товары которые имеют цену больше 20000 рублей')
        self.assertEqual(plan.risk, 'read')
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0]['agent'], 'catalog-query')
        self.assertEqual(plan.steps[0]['params']['price_min'], 20000)

    def test_imported_catalog_count_is_read_only_query(self):
        plan = build_plan('Сколько импортированных карточек?')
        self.assertEqual(plan.risk, 'read')
        self.assertEqual(plan.steps[0]['agent'], 'catalog-query')

    def test_stock_threshold_is_not_misread_as_price(self):
        plan = build_plan('Покажи товары с остатком меньше 5')
        params = plan.steps[0]['params']
        self.assertEqual(params['quantity_max'], 5)
        self.assertNotIn('price_max', params)

    def test_wb_catalog_count_is_not_routed_to_publish_workflow(self):
        plan = build_plan('Сколько у меня товаров на WB?')
        self.assertEqual(plan.risk, 'read')
        self.assertEqual(plan.steps[0]['agent'], 'catalog-query')
        self.assertEqual(plan.steps[0]['params']['entity_kind'], 'product')

    def test_common_catalog_wording_variants_stay_deterministic(self):
        cases = (
            ('Покажи карточки с ошибками валидации', 'missing_field', 'validation_errors'),
            ('Покажи неопубликованные карточки', 'published', 'no'),
            ('Покажи товары со статусом failed', 'import_status', 'failed'),
        )
        for text, key, value in cases:
            with self.subTest(text=text):
                plan = build_plan(text)
                self.assertEqual(plan.steps[0]['agent'], 'catalog-query')
                self.assertEqual(plan.steps[0]['params'][key], value)

    def test_declined_wb_quality_filter_is_deterministic(self):
        plan = build_plan('Покажи карточки на WB с качеством ниже 50')
        self.assertEqual(plan.steps[0]['params']['entity_kind'], 'product')
        self.assertEqual(plan.steps[0]['params']['quality_max'], 50)

    def test_system_settings_queries_skip_react_agent(self):
        for text, kind in (
            ('Покажи последние ошибки API', 'api_errors'),
            ('WB API подключен?', 'api_status'),
            ('Какие дефолты товаров?', 'product_defaults'),
            ('Покажи стоп-слова', 'prohibited_words'),
            ('Покажи настройки цен', 'pricing'),
        ):
            with self.subTest(text=text):
                plan = build_plan(text)
                self.assertEqual(plan.risk, 'read')
                self.assertEqual(plan.steps[0]['agent'], 'system-query')
                self.assertEqual(plan.steps[0]['params']['kind'], kind)

    def test_product_page_question_uses_typed_read_only_insight(self):
        plan = build_plan(
            'что можешь сказать по этой карточке?', [9741],
            {'url': 'https://seller-platform.tech/products/9741'},
        )
        self.assertEqual(plan.risk, 'read')
        self.assertEqual(plan.steps[0]['agent'], 'card-insight')
        self.assertEqual(plan.steps[0]['params']['entity_kind'], 'product')

    def test_selected_batch_audit_is_deterministic_for_both_entity_kinds(self):
        for entity_kind in ('product', 'imported_product'):
            with self.subTest(entity_kind=entity_kind):
                plan = build_plan(
                    'Проведи аудит выбранных карточек и найди основные проблемы',
                    [11, 12, 13], entity_kind=entity_kind,
                )
                self.assertEqual(plan.risk, 'read')
                self.assertFalse(plan.requires_approval)
                self.assertEqual(len(plan.steps), 1)
                self.assertEqual(plan.steps[0]['agent'], 'batch-audit')
                self.assertEqual(plan.steps[0]['params']['entity_kind'], entity_kind)

    def test_batch_audit_skill_uses_one_platform_call_and_zero_llm(self):
        platform = _BatchAuditPlatform()
        skill = object.__new__(BatchAuditSkill)
        skill.platform = platform
        result = skill.execute_task({
            'seller_id': 7,
            'input_data': json.dumps({
                'product_ids': [11, 12, 13],
                'entity_scope': {'kind': 'product', 'ids': [11, 12, 13]},
                'params': {'entity_kind': 'product', 'focus_limit': 100},
            }),
        })
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['_usage']['api_requests'], 0)
        self.assertEqual(platform.calls, [(7, 'product', [11, 12, 13], 100)])

    def test_product_page_description_rewrite_is_one_scoped_skill(self):
        plan = build_plan(
            'улучши ее описание', [9741],
            {'url': 'https://seller-platform.tech/products/9741'},
        )
        self.assertEqual(plan.risk, 'write')
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0]['agent'], 'content-writer')
        self.assertEqual(plan.steps[0]['params']['entity_kind'], 'product')
        self.assertEqual(plan.steps[0]['params']['fields'], ['description'])

    def test_product_page_content_request_keeps_every_explicit_field(self):
        cases = (
            ('обнови у карточки описание и название пожалуйста', ['title', 'description']),
            ('улучши название и описание', ['title', 'description']),
            ('перепиши заголовок и текст карточки', ['title', 'description']),
            ('сделай новое название', ['title']),
        )
        for text, expected_fields in cases:
            with self.subTest(text=text):
                plan = build_plan(
                    text, [9741],
                    {'url': 'https://seller-platform.tech/products/9741'},
                )
                self.assertEqual(plan.risk, 'write')
                self.assertEqual(len(plan.steps), 1)
                self.assertEqual(plan.steps[0]['agent'], 'content-writer')
                self.assertEqual(plan.steps[0]['params']['fields'], expected_fields)
                for field in expected_fields:
                    self.assertIn(field, plan.steps[0]['params']['fields'])

    def test_named_supplier_content_request_never_becomes_unscoped_seo_plan(self):
        plan = build_plan('улучши описание и название карточек Андрея')
        self.assertIsNone(plan)

    def test_product_page_content_request_respects_field_level_exclusions(self):
        for text in (
            'обнови описание, название не меняй',
            'улучши только описание, название оставь как есть',
            'перепиши описание, кроме названия',
            'название, но не улучшай; описание улучши',
        ):
            with self.subTest(text=text):
                plan = build_plan(
                    text, [9741],
                    {'url': 'https://seller-platform.tech/products/9741'},
                )
                self.assertEqual(plan.steps[0]['params']['fields'], ['description'])

    def test_product_page_global_no_write_never_creates_content_plan(self):
        for text in (
            'не меняй ничего, покажи как улучшить описание',
            'без изменений: как улучшить название и описание',
            'не сохраняй, просто покажи улучшенное описание',
            'не изменяй ничего, покажи как улучшить описание',
            'ничего не обновляй, расскажи как улучшить описание',
            'ничего не трогай, как улучшить описание',
            'не оптимизируй описание, только оцени его',
            'не надо ничего менять, покажи улучшенное описание',
            'ничего менять не нужно, предложи новое описание',
            'не нужно ничего изменять, покажи новое название',
            'оставь всё как есть, предложи новое описание',
            'только предложи новое описание, не записывай',
        ):
            with self.subTest(text=text):
                self.assertIsNone(build_plan(
                    text, [9741],
                    {'url': 'https://seller-platform.tech/products/9741'},
                ))

    def test_product_page_never_routes_brand_to_imported_product_skill(self):
        plan = build_plan(
            'исправь бренд', [9741],
            {'url': 'https://seller-platform.tech/products/9741'},
        )
        self.assertIsNone(plan)

    def test_explicit_product_scope_never_routes_selected_audit_to_legacy_skill(self):
        plan = build_plan(
            'Проведи аудит выбранных карточек', [9741], entity_kind='product',
        )
        self.assertEqual(plan.steps[0]['agent'], 'batch-audit')
        self.assertEqual(plan.steps[0]['params']['entity_kind'], 'product')

    def test_explicit_product_scope_keeps_typed_card_insight(self):
        plan = build_plan(
            'что можешь сказать по этой карточке?', [9741], entity_kind='product',
        )
        self.assertEqual(plan.steps[0]['params']['entity_kind'], 'product')

    def test_nested_supplier_product_url_is_not_main_product(self):
        plan = build_plan(
            'что можешь сказать по этой карточке?', [9741],
            {'url': 'https://seller-platform.tech/admin/suppliers/4/products/9741'},
        )
        self.assertIsNone(plan)

    def test_multiple_skills_form_custom_plan(self):
        plan = build_plan('проверь бренды и размеры')
        names = {step['agent'] for step in plan.steps}
        self.assertEqual(names, {'brand-resolver', 'size-normalizer'})
        self.assertEqual(plan.execution_type, 'custom')

    def test_description_writer_filters_and_saves_one_typed_product(self):
        platform = _DescriptionPlatform()
        skill = object.__new__(DescriptionWriterSkill)
        skill.llm = _DescriptionLLM()
        skill.platform = platform

        result = skill.execute_task({
            'seller_id': 7,
            'input_data': json.dumps({
                'product_ids': [9741],
                'entity_scope': {'kind': 'product', 'ids': [9741]},
            }),
        })

        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['saved'], 1)
        self.assertEqual(platform.checked_seller_id, 7)
        self.assertEqual(platform.saved, [(7, 9741, {
            'description': 'Новое безопасное описание',
        })])
        change = result['artifacts'][0]['changes']['description']
        self.assertEqual(change['old'], 'Старое описание')
        self.assertEqual(change['new'], 'Новое безопасное описание')
        self.assertEqual(result['_usage']['api_requests'], 1)

    def test_content_writer_batches_100_cards_without_silent_truncation(self):
        platform = _GeneratedBatchPlatform()
        llm = _GeneratedBatchLLM()
        skill = object.__new__(DescriptionWriterSkill)
        skill.llm = llm
        skill.platform = platform

        result = skill.execute_task({
            'id': 'task-100',
            'seller_id': 7,
            'task_type': 'rewrite_content',
            'input_data': json.dumps({
                'product_ids': list(range(1, 101)),
                'entity_scope': {'kind': 'product', 'ids': list(range(1, 101))},
                'params': {'entity_kind': 'product', 'fields': ['title', 'description']},
            }),
        })

        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['processed'], 100)
        self.assertEqual(result['saved'], 100)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(platform.brief_calls, 1)
        self.assertEqual(llm.calls, 13)
        self.assertEqual(result['_usage']['api_requests'], 13)
        self.assertEqual(
            {item['product_id'] for batch in platform.batch_calls for item in batch},
            set(range(1, 101)),
        )

    def test_content_writer_fails_closed_on_incomplete_stop_word_check(self):
        platform = _GeneratedBatchPlatform(missing_check=True)
        skill = object.__new__(DescriptionWriterSkill)
        skill.llm = _GeneratedBatchLLM()
        skill.platform = platform
        result = skill.execute_task({
            'id': 'task-check',
            'seller_id': 7,
            'task_type': 'rewrite_content',
            'input_data': json.dumps({
                'product_ids': [1, 2],
                'entity_scope': {'kind': 'product', 'ids': [1, 2]},
                'params': {'entity_kind': 'product', 'fields': ['title', 'description']},
            }),
        })
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['saved'], 0)
        self.assertEqual(result['failed'], 2)
        self.assertEqual(platform.batch_calls, [])

    def test_content_writer_stops_batch_without_extra_call_at_token_budget(self):
        platform = _GeneratedBatchPlatform()
        llm = _GeneratedBatchLLM()
        skill = object.__new__(DescriptionWriterSkill)
        skill.llm = llm
        skill.platform = platform
        skill._run_token_budget_override = 100
        result = skill.execute_task({
            'id': 'task-budget',
            'seller_id': 7,
            'task_type': 'rewrite_content',
            'input_data': json.dumps({
                'product_ids': list(range(1, 21)),
                'entity_scope': {'kind': 'product', 'ids': list(range(1, 21))},
                'params': {'entity_kind': 'product', 'fields': ['title', 'description']},
            }),
        })
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['processed'], 16)
        self.assertEqual(result['saved'], 16)
        self.assertEqual(result['failed'], 4)
        self.assertEqual(llm.calls, 2)
        self.assertTrue(result['_usage']['budget_exhausted'])

    def test_content_writer_rejects_typed_scope_override(self):
        platform = _GeneratedBatchPlatform()
        skill = object.__new__(DescriptionWriterSkill)
        skill.llm = _GeneratedBatchLLM()
        skill.platform = platform
        result = skill.execute_task({
            'seller_id': 7,
            'task_type': 'rewrite_content',
            'input_data': json.dumps({
                'product_ids': [1],
                'entity_scope': {'kind': 'product', 'ids': [1]},
                'params': {'entity_kind': 'imported_product', 'fields': ['description']},
            }),
        })
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(platform.brief_calls, 0)

    def test_content_writer_rejects_non_integer_scope_ids(self):
        platform = _GeneratedBatchPlatform()
        skill = object.__new__(DescriptionWriterSkill)
        skill.llm = _GeneratedBatchLLM()
        skill.platform = platform

        result = skill.execute_task({
            'seller_id': 7,
            'task_type': 'rewrite_content',
            'input_data': json.dumps({
                'product_ids': [1.0],
                'entity_scope': {'kind': 'product', 'ids': [1.0]},
                'params': {'entity_kind': 'product', 'fields': ['description']},
            }),
        })

        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(platform.brief_calls, 0)

    def test_content_writer_retry_respects_physical_api_budget(self):
        platform = _GeneratedBatchPlatform()
        skill = object.__new__(DescriptionWriterSkill)
        skill.platform = platform
        skill._run_api_budget_override = 2

        class FailingLLM:
            def __init__(self):
                self.calls = 0

            @llm_retry(max_retries=3, base_delay=0)
            def structured_output_with_usage(self, **kwargs):
                self.calls += 1
                raise ConnectionError('temporary provider failure')

        skill.llm = FailingLLM()
        result = skill.execute_task({
            'id': 'content-retry-budget',
            'seller_id': 7,
            'task_type': 'rewrite_content',
            'input_data': json.dumps({
                'product_ids': [1],
                'entity_scope': {'kind': 'product', 'ids': [1]},
                'params': {'entity_kind': 'product', 'fields': ['description']},
            }),
        })

        self.assertEqual(result['status'], 'partial')
        self.assertEqual(skill.llm.calls, 2)
        self.assertEqual(result['_usage']['api_requests'], 2)
        self.assertEqual(platform.batch_calls, [])

    def test_content_writer_generates_and_saves_title_and_description_in_one_call(self):
        platform = _ContentPlatform()
        llm = _ContentLLM()
        skill = object.__new__(DescriptionWriterSkill)
        skill.llm = llm
        skill.platform = platform

        result = skill.execute_task({
            'seller_id': 7,
            'input_data': json.dumps({
                'product_ids': [9741],
                'entity_scope': {'kind': 'product', 'ids': [9741]},
                'params': {
                    'entity_kind': 'product',
                    'fields': ['title', 'description'],
                },
            }),
        })

        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['requested_fields'], ['title', 'description'])
        self.assertEqual(llm.calls, 1)
        required = llm.schema['properties']['results']['items']['required']
        self.assertEqual(required, ['product_id', 'title', 'description'])
        self.assertEqual(
            {(item['product_id'], item['field']) for item in platform.checked_items},
            {(9741, 'title'), (9741, 'description')},
        )
        self.assertEqual(platform.saved, [(7, 9741, {
            'title': 'Свеча ароматическая',
            'description': 'Новое точное описание',
        })])
        changes = result['artifacts'][0]['changes']
        self.assertEqual(set(changes), {'title', 'description'})

    def test_content_writer_rejects_incomplete_requested_field_set(self):
        platform = _ContentPlatform()
        skill = object.__new__(DescriptionWriterSkill)
        skill.llm = _ContentLLM(include_title=False)
        skill.platform = platform

        result = skill.execute_task({
            'seller_id': 7,
            'input_data': json.dumps({
                'product_ids': [9741],
                'entity_scope': {'kind': 'product', 'ids': [9741]},
                'params': {'fields': ['title', 'description']},
            }),
        })

        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['failed'], 1)
        self.assertEqual(platform.saved, [])
        self.assertEqual(platform.checked_items, [])

    def test_content_writer_rejects_coerced_model_product_id(self):
        platform = _ContentPlatform()
        skill = object.__new__(DescriptionWriterSkill)
        skill.platform = platform

        class FloatIdLLM:
            def structured_output_with_usage(self, **kwargs):
                return {
                    'data': {'results': [{
                        'product_id': 9741.0,
                        'description': 'Нельзя сохранять по приведённому ID',
                    }]},
                    'usage': {'api_requests': 1},
                }

        skill.llm = FloatIdLLM()
        result = skill.execute_task({
            'seller_id': 7,
            'input_data': json.dumps({
                'product_ids': [9741],
                'entity_scope': {'kind': 'product', 'ids': [9741]},
            }),
        })

        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['saved'], 0)
        self.assertEqual(platform.saved, [])
        self.assertEqual(platform.checked_items, [])

    def test_content_writer_reports_unchanged_title_honestly(self):
        platform = _ContentPlatform()
        skill = object.__new__(DescriptionWriterSkill)
        skill.llm = _ContentLLM(title='Свеча')
        skill.platform = platform
        result = skill.execute_task({
            'seller_id': 7,
            'task_type': 'rewrite_content',
            'input_data': json.dumps({
                'product_ids': [9741],
                'entity_scope': {'kind': 'product', 'ids': [9741]},
                'params': {
                    'entity_kind': 'product',
                    'fields': ['title', 'description'],
                },
            }),
        })
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['changed_counts'], {'title': 0, 'description': 1})
        self.assertEqual(result['unchanged_counts'], {'title': 1, 'description': 0})
        self.assertEqual(platform.saved, [(7, 9741, {
            'description': 'Новое точное описание',
        })])
        self.assertIn('название: 1', result['message'])

    def test_catalog_query_uses_one_tiny_flash_polish_call(self):
        llm = _CatalogLLM()
        skill = object.__new__(CatalogQuerySkill)
        skill.llm = llm
        skill.platform = _CatalogPlatform()

        result = skill.execute_task({
            'seller_id': 7,
            'input_data': json.dumps({'params': {
                'missing_field': 'description',
                'condition_label': 'без описания',
            }}),
        })

        self.assertEqual(result['message'], 'Нашёл 7 карточек без описания.')
        self.assertEqual(result['_usage']['api_requests'], 1)
        prompt = llm.messages[0]['content']
        self.assertIn('"count":7', prompt)
        self.assertNotIn('Секретное длинное название', prompt)

    def test_catalog_query_rejects_flash_message_with_changed_count(self):
        skill = object.__new__(CatalogQuerySkill)
        skill.llm = _IncorrectCountLLM()
        skill.platform = _CatalogPlatform()
        result = skill.execute_task({
            'seller_id': 7,
            'input_data': json.dumps({'params': {
                'missing_field': 'description',
                'condition_label': 'без описания',
            }}),
        })
        self.assertEqual(result['message'], 'Найдено карточек без описания: 7.')

    def test_product_ids_are_deduplicated_and_bounded(self):
        ids = _normalize_product_ids(['1', 2, 'bad', 1, -4, 3.0])
        self.assertEqual(ids, [1, 2, 3])

    def test_help_is_answered_without_task(self):
        response = direct_response('Что ты умеешь?')
        self.assertIn('единый помощник', response)


if __name__ == '__main__':
    unittest.main()
