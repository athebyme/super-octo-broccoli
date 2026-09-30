# -*- coding: utf-8 -*-
"""Safety and durability contracts for shared supplier-card enrichment."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flask import Flask

from models import (
    AIParsingAttempt,
    ImportedProduct,
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    Seller,
    Supplier,
    SupplierCatalogEnrichmentItem,
    SupplierCatalogEnrichmentRun,
    SupplierProduct,
    User,
    db,
)
from services.supplier_catalog_enrichment import (
    SupplierCatalogEnrichmentError,
    SupplierCatalogEnrichmentService,
    _evidence_is_grounded,
    _legacy_source_fingerprint,
    _product_source,
    _source_fingerprint,
)
from services.supplier_service import SupplierService
from services.ozon_draft_ai_transport import FlashOutcome


class _FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0
        self.prompts = []

    def chat_completion(self, *args, **kwargs):
        self.calls += 1
        self.prompts.append(args[0] if args else [])
        if not self.responses:
            raise AssertionError('Unexpected model call')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeAIService:
    def __init__(self, client):
        self.client = client
        self.config = SimpleNamespace(model='test-model')

    def close(self):
        return None


def _native_flash_service(client):
    service = _FakeAIService(client)
    service.config = SimpleNamespace(
        provider='deepseek', model='deepseek-flash',
        api_base_url='https://api.deepseek.com/v1', api_key='test-secret',
        max_retries=1, proxy_enabled=False,
        task_profile='supplier_parsing_flash',
    )
    return service


class SupplierCatalogEnrichmentTest(unittest.TestCase):
    ANAL_ID = 51001
    VIBRATOR_ID = 51002

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='supplier-catalog-enrichment',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        admin = User(
            username='catalog-admin', email='catalog-admin@test.local',
            is_admin=True, is_active=True,
        )
        admin.set_password('synthetic-password')
        supplier = Supplier(
            name='Test supplier', code='catalog-enrichment-test',
            ai_enabled=True,
        )
        marketplace = Marketplace(
            name='Wildberries', code='wb', is_active=True,
            categories_sync_status='success',
            categories_synced_at=self._now(),
            categories_version=7,
            categories_snapshot_hash='a' * 64,
            total_categories=2,
        )
        db.session.add_all([admin, supplier, marketplace])
        db.session.flush()
        self.admin_id = admin.id
        self.supplier_id = supplier.id
        self.marketplace_id = marketplace.id
        self.anal = MarketplaceCategory(
            marketplace_id=marketplace.id,
            subject_id=self.ANAL_ID,
            subject_name='Анальные пробки',
            parent_name='Товары для взрослых',
            is_leaf=True,
            is_enabled=True,
            is_available=True,
            characteristics_sync_status='success',
            characteristics_synced_at=self._now(),
            characteristics_schema_hash='b' * 64,
            characteristics_version=3,
            characteristics_count=1,
        )
        vibrator = MarketplaceCategory(
            marketplace_id=marketplace.id,
            subject_id=self.VIBRATOR_ID,
            subject_name='Вибраторы',
            parent_name='Товары для взрослых',
            is_leaf=True,
            is_enabled=True,
            is_available=True,
            characteristics_sync_status='success',
            characteristics_synced_at=self._now(),
        )
        db.session.add_all([self.anal, vibrator])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    @staticmethod
    def _now():
        from datetime import datetime
        return datetime.utcnow()

    def _product(self, **overrides):
        values = {
            'supplier_id': self.supplier_id,
            'external_id': f'p-{SupplierProduct.query.count() + 1}',
            'title': 'Анальная пробка из силикона',
            'description': 'Материал изделия: силикон.',
            'category': 'Товары для взрослых',
            'wb_category_name': 'Товары для взрослых',
            'content_revision': 1,
        }
        values.update(overrides)
        values.setdefault('original_data_json', json.dumps({
            'title': values['title'],
            'description': values['description'],
            'category': values['category'],
        }, ensure_ascii=False))
        product = SupplierProduct(**values)
        db.session.add(product)
        db.session.commit()
        return product

    def _run(self, product, responses=(), mode='category_only', batches=3):
        client = _FakeClient(list(responses))

        def fake_service(*args, **kwargs):
            return _FakeAIService(client)

        with patch.object(
            SupplierService, '_get_ai_service', side_effect=fake_service,
        ):
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[product.id],
                mode=mode,
            )
            SupplierCatalogEnrichmentService.process_run(
                run.id, batch_limit=batches,
            )
        return (
            db.session.get(SupplierCatalogEnrichmentRun, run.id),
            SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id).one(),
            client,
        )

    def test_source_evidence_ignores_ai_poisoned_normalized_columns(self):
        product = self._product()
        fingerprint = _source_fingerprint(product)
        product.title = 'Выдуманный ПВХ товар'
        product.description = 'Материал изделия: ПВХ.'
        product.characteristics_json = json.dumps([
            {'name': 'Материал изделия', 'value': 'ПВХ'},
        ], ensure_ascii=False)
        product.ai_parsed_data_json = json.dumps({'materials': {'material': 'ПВХ'}})
        db.session.commit()
        source = _product_source(product)
        self.assertEqual(source['title'], 'Анальная пробка из силикона')
        self.assertEqual(source['description'], 'Материал изделия: силикон.')
        self.assertNotIn('ПВХ', json.dumps(source, ensure_ascii=False))
        self.assertFalse(_evidence_is_grounded('ПВХ', source))
        self.assertEqual(_source_fingerprint(product), fingerprint)
        original = json.loads(product.original_data_json)
        original['description'] = 'Материал изделия: латекс.'
        product.original_data_json = json.dumps(original, ensure_ascii=False)
        self.assertNotEqual(_source_fingerprint(product), fingerprint)

    def test_model_prompt_uses_snapshot_not_poisoned_columns(self):
        product = self._product()
        product.title = 'ВЫДУМАННЫЙ-ПВХ-ТОВАР'
        product.description = 'Несуществующий материал ПВХ.'
        product.ai_parsed_data_json = '{"materials":"ПВХ"}'
        db.session.commit()
        _, _, client = self._run(
            product,
            responses=[self._response_for_product(product.id, self.ANAL_ID)],
        )
        self.assertEqual(client.calls, 1)
        prompt = json.dumps(client.prompts[0], ensure_ascii=False)
        self.assertIn('Анальная пробка из силикона', prompt)
        self.assertNotIn('ВЫДУМАННЫЙ-ПВХ-ТОВАР', prompt)
        self.assertNotIn('Несуществующий материал ПВХ', prompt)

    def test_existing_ai_category_is_not_deterministic_source_evidence(self):
        product = self._product(wb_subject_id=self.VIBRATOR_ID)
        product.title = 'Вибратор из ПВХ'
        db.session.commit()
        client = _FakeClient(['{"wrong":[]}'])

        def fake_service(*args, **kwargs):
            return _native_flash_service(client)

        with patch.object(SupplierService, '_get_ai_service', side_effect=fake_service), \
             patch('services.ozon_draft_ai_transport.flash_completion',
                   return_value=FlashOutcome('success', content={'wrong': []},
                                             http_status=200)) as physical:
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[product.id],
            )
            SupplierCatalogEnrichmentService.process_run(run.id)
        item = SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id).one()
        self.assertEqual(physical.call_count, 1)
        self.assertEqual(AIParsingAttempt.query.filter_by(run_uid=run.id).count(), 1)
        self.assertEqual(client.calls, 0)
        self.assertEqual(item.status, 'failed')
        self.assertEqual(item.error_code, 'invalid_model_response')
        self.assertEqual(product.wb_subject_id, self.VIBRATOR_ID)

    def test_missing_source_snapshot_needs_review_without_model_call(self):
        product = self._product(original_data_json=None)
        run, item, client = self._run(product)
        self.assertEqual(client.calls, 0)
        self.assertEqual(item.status, 'needs_review')
        self.assertEqual(item.error_code, 'source_snapshot_missing')
        self.assertEqual(run.status, 'partial')
        self.assertIsNone(product.ai_marketplace_json)

    def test_existing_luna_channels_survive_flash_run_without_model_call(self):
        protected = json.dumps({
            'Материал изделия': ['Силикон'],
            '_meta': {'channels': {'ozon': {'schema_hash': 'a' * 64}}},
        }, ensure_ascii=False)
        product = self._product(ai_marketplace_json=protected)
        client = _FakeClient([])

        def fake_service(*args, **kwargs):
            service = _FakeAIService(client)
            service.config.model = 'deepseek-flash'
            return service

        with patch.object(SupplierService, '_get_ai_service', side_effect=fake_service):
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[product.id],
            )
            SupplierCatalogEnrichmentService.process_run(run.id)
        item = SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id).one()
        self.assertEqual(client.calls, 0)
        self.assertEqual(item.status, 'needs_review')
        self.assertEqual(item.error_code, 'existing_content_requires_review')
        self.assertEqual(run.status, 'partial')
        self.assertEqual(product.ai_marketplace_json, protected)
        with self.assertRaises(SupplierCatalogEnrichmentError) as raised:
            SupplierCatalogEnrichmentService.apply_review_category(
                item_id=item.id,
                supplier_id=self.supplier_id,
                subject_id=self.ANAL_ID,
            )
        self.assertEqual(raised.exception.code, 'existing_content_requires_review')
        self.assertEqual(product.ai_marketplace_json, protected)

    def test_luna_channel_added_after_run_claim_is_not_overwritten(self):
        product = self._product()
        client = _FakeClient([])

        def fake_service(*args, **kwargs):
            service = _FakeAIService(client)
            service.config.model = 'deepseek-flash'
            return service

        with patch.object(SupplierService, '_get_ai_service', side_effect=fake_service):
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[product.id],
            )
            protected = json.dumps({
                '_meta': {'channels': {'ozon': {'schema_hash': 'b' * 64}}},
            }, ensure_ascii=False)
            product.ai_marketplace_json = protected
            db.session.commit()
            SupplierCatalogEnrichmentService.process_run(run.id)
        item = SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id).one()
        self.assertEqual(client.calls, 0)
        self.assertEqual(item.status, 'needs_review')
        self.assertEqual(item.error_code, 'existing_content_requires_review')
        self.assertEqual(product.ai_marketplace_json, protected)

    def test_flash_invalid_response_is_not_retried_by_durable_run(self):
        product = self._product()
        client = _FakeClient(['{"wrong":[]}'])

        def fake_service(*args, **kwargs):
            return _native_flash_service(client)

        with patch.object(SupplierService, '_get_ai_service', side_effect=fake_service), \
             patch('services.ozon_draft_ai_transport.flash_completion',
                   return_value=FlashOutcome('success', content={'wrong': []},
                                             http_status=200)) as physical:
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[product.id],
            )
            SupplierCatalogEnrichmentService.process_run(run.id, batch_limit=3)
            SupplierCatalogEnrichmentService.process_run(run.id, batch_limit=3)
        item = SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id).one()
        self.assertEqual(physical.call_count, 1)
        self.assertEqual(AIParsingAttempt.query.filter_by(run_uid=run.id).count(), 1)
        self.assertEqual(client.calls, 0)
        self.assertEqual(item.status, 'failed')
        self.assertEqual(item.error_code, 'invalid_model_response')

    def test_flash_429_keeps_admin_chunk_pending_until_durable_due(self):
        product = self._product()
        client = _FakeClient([])

        with patch.object(SupplierService, '_get_ai_service',
                          side_effect=lambda *a, **k: _native_flash_service(client)), \
             patch('services.ozon_draft_ai_transport.flash_completion',
                   return_value=FlashOutcome('rate_limited', http_status=429,
                                             retry_after_seconds=3600)) as physical:
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id, admin_user_id=self.admin_id,
                product_ids=[product.id],
            )
            SupplierCatalogEnrichmentService.process_run(run.id, batch_limit=3)
            SupplierCatalogEnrichmentService.process_run(run.id, batch_limit=3)
        item = SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id).one()
        attempt = AIParsingAttempt.query.filter_by(run_uid=run.id).one()
        self.assertEqual(physical.call_count, 1)
        self.assertEqual(client.calls, 0)
        self.assertEqual(attempt.status, 'rate_limited')
        self.assertGreater(attempt.retry_due_at, datetime.utcnow() + timedelta(minutes=50))
        self.assertEqual(item.status, 'pending')

    def test_flash_unknown_response_is_terminal_with_one_physical_claim(self):
        product = self._product()
        client = _FakeClient([])

        with patch.object(SupplierService, '_get_ai_service',
                          side_effect=lambda *a, **k: _native_flash_service(client)), \
             patch('services.ozon_draft_ai_transport.flash_completion',
                   return_value=FlashOutcome('unknown_response',
                                             safe_code='ai_response_unknown')) as physical:
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id, admin_user_id=self.admin_id,
                product_ids=[product.id],
            )
            SupplierCatalogEnrichmentService.process_run(run.id, batch_limit=3)
            SupplierCatalogEnrichmentService.process_run(run.id, batch_limit=3)
        item = SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id).one()
        attempt = AIParsingAttempt.query.filter_by(run_uid=run.id).one()
        self.assertEqual(physical.call_count, 1)
        self.assertEqual(attempt.status, 'unknown_response')
        self.assertEqual(item.status, 'needs_review')
        self.assertEqual(item.error_code, 'unknown_response')

    def test_stale_flash_running_item_is_unknown_not_automatically_replayed(self):
        product = self._product()
        client = _FakeClient([])

        def fake_service(*args, **kwargs):
            service = _FakeAIService(client)
            service.config.model = 'deepseek-flash'
            return service

        with patch.object(SupplierService, '_get_ai_service', side_effect=fake_service):
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[product.id],
            )
            item = SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id).one()
            SupplierCatalogEnrichmentItem.query.filter_by(id=item.id).update({
                'status': 'running',
                'updated_at': datetime.utcnow() - timedelta(minutes=11),
            }, synchronize_session=False)
            run.status = 'running'
            db.session.commit()
            SupplierCatalogEnrichmentService.process_run(run.id)
            SupplierCatalogEnrichmentService.process_run(run.id)

        db.session.refresh(item)
        db.session.refresh(run)
        self.assertEqual(client.calls, 0)
        self.assertEqual(item.status, 'needs_review')
        self.assertEqual(item.error_code, 'unknown_response')
        self.assertEqual(run.status, 'partial')
        self.assertIsNone(product.wb_subject_id)

    def test_nonfinite_source_number_is_excluded_from_model_evidence(self):
        product = self._product(original_data_json=json.dumps({
            'title': 'Анальная пробка',
            'characteristics': {'weight': float('nan')},
        }))
        source = _product_source(product)
        self.assertNotIn('NaN', json.dumps(source, ensure_ascii=False))

    def test_historical_normalized_hash_still_allows_guarded_rollback(self):
        product = self._product(wb_subject_id=self.ANAL_ID)
        run, item, client = self._run(product)
        self.assertEqual(item.status, 'applied')
        self.assertEqual(client.calls, 0)
        run.selection_json = '{}'
        item.source_fingerprint = _legacy_source_fingerprint(product)
        db.session.commit()
        SupplierCatalogEnrichmentService.rollback_item(
            item_id=item.id, supplier_id=self.supplier_id,
        )
        self.assertEqual(item.status, 'rolled_back')

    def test_historical_pending_run_fails_closed_before_model_call(self):
        product = self._product()
        client = _FakeClient([])

        def fake_service(*args, **kwargs):
            return _FakeAIService(client)

        with patch.object(SupplierService, '_get_ai_service', side_effect=fake_service):
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[product.id],
            )
            run.selection_json = '{}'
            db.session.commit()
            SupplierCatalogEnrichmentService.process_run(run.id)
        item = SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id).one()
        self.assertEqual(client.calls, 0)
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.error_code, 'legacy_review_required')
        self.assertEqual(item.status, 'failed')

    @staticmethod
    def _category_response(subject_id, confidence=0.98, evidence='Анальная пробка'):
        return json.dumps({
            'results': [{
                'product_id': 1,
                'subject_id': subject_id,
                'confidence': confidence,
                'reasoning': 'Тип товара прямо указан в названии.',
                'evidence': evidence,
                'lookup_query': None,
            }],
        }, ensure_ascii=False)

    def _response_for_product(
        self, product_id, subject_id, confidence=0.98,
        evidence='Анальная пробка',
    ):
        return json.dumps({
            'results': [{
                'product_id': product_id,
                'subject_id': subject_id,
                'confidence': confidence,
                'reasoning': 'Тип товара прямо указан в названии.',
                'evidence': evidence,
                'lookup_query': None,
            }],
        }, ensure_ascii=False)

    def test_existing_leaf_id_canonicalizes_parent_name_without_model(self):
        product = self._product(wb_subject_id=self.ANAL_ID)
        run, item, client = self._run(product)

        db.session.refresh(product)
        self.assertEqual(run.status, 'completed')
        self.assertEqual(item.status, 'applied')
        self.assertEqual(product.wb_category_name, 'Анальные пробки')
        self.assertEqual(product.wb_subject_name, 'Анальные пробки')
        self.assertEqual(product.content_revision, 2)
        self.assertEqual(client.calls, 0)

    def test_model_cannot_inject_category_outside_tool_candidates(self):
        product = self._product()
        invalid = self._response_for_product(product.id, 999999)
        client = _FakeClient([invalid, invalid, invalid])

        def fake_service(*args, **kwargs):
            return _FakeAIService(client)

        with patch.object(
            SupplierService, '_get_ai_service', side_effect=fake_service,
        ):
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[product.id],
            )
            for _ in range(3):
                SupplierCatalogEnrichmentService.process_run(
                    run.id, batch_limit=1,
                )

        db.session.refresh(product)
        item = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run.id,
        ).one()
        self.assertEqual(item.status, 'failed')
        self.assertEqual(item.error_code, 'invalid_model_response')
        self.assertIsNone(product.wb_subject_id)
        self.assertEqual(product.wb_category_name, 'Товары для взрослых')

    def test_grounded_high_confidence_leaf_is_applied(self):
        product = self._product()
        response = self._response_for_product(product.id, self.ANAL_ID)
        run, item, client = self._run(product, [response])

        db.session.refresh(product)
        self.assertEqual(run.status, 'completed')
        self.assertEqual(item.status, 'applied')
        self.assertEqual(product.wb_subject_id, self.ANAL_ID)
        self.assertEqual(product.wb_category_name, 'Анальные пробки')
        self.assertNotEqual(product.wb_category_name, 'Товары для взрослых')
        self.assertEqual(client.calls, 1)

    def test_tied_lexical_candidates_require_admin_review(self):
        db.session.add(MarketplaceCategory(
            marketplace_id=self.marketplace_id,
            subject_id=51003,
            subject_name='Пробки анальные',
            parent_name='Товары для взрослых',
            is_leaf=True,
            is_enabled=True,
            is_available=True,
        ))
        db.session.commit()
        product = self._product()
        response = self._response_for_product(product.id, self.ANAL_ID)
        run, item, _ = self._run(product, [response])

        db.session.refresh(product)
        self.assertEqual(run.status, 'partial')
        self.assertEqual(item.status, 'needs_review')
        self.assertEqual(item.error_code, 'low_confidence')
        self.assertIsNone(product.wb_subject_id)

    def test_transient_model_failure_is_bounded_and_never_writes(self):
        product = self._product()
        run, item, client = self._run(
            product,
            [RuntimeError('synthetic provider body')],
            batches=1,
        )

        db.session.refresh(product)
        self.assertEqual(run.status, 'running')
        self.assertEqual(item.status, 'pending')
        self.assertEqual(item.error_code, 'model_call_failed')
        self.assertNotIn('synthetic provider body', item.error_message)
        self.assertIsNone(product.wb_subject_id)
        self.assertEqual(client.calls, 1)

    def test_model_failure_does_not_retry_deterministic_rows(self):
        deterministic = self._product(wb_subject_id=self.ANAL_ID)
        unresolved = self._product(
            title='Вибратор силиконовый',
            external_id='mixed-unresolved',
        )
        client = _FakeClient([RuntimeError('synthetic provider body')])

        def fake_service(*args, **kwargs):
            return _FakeAIService(client)

        with patch.object(
            SupplierService, '_get_ai_service', side_effect=fake_service,
        ):
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[deterministic.id, unresolved.id],
            )
            SupplierCatalogEnrichmentService.process_run(
                run.id, batch_limit=1,
            )

        items = {
            item.supplier_product_id: item
            for item in SupplierCatalogEnrichmentItem.query.filter_by(
                run_id=run.id,
            ).all()
        }
        db.session.refresh(deterministic)
        db.session.refresh(unresolved)
        self.assertEqual(items[deterministic.id].status, 'applied')
        self.assertIsNone(items[deterministic.id].error_code)
        self.assertEqual(deterministic.wb_category_name, 'Анальные пробки')
        self.assertEqual(items[unresolved.id].status, 'pending')
        self.assertEqual(items[unresolved.id].error_code, 'model_call_failed')
        self.assertIsNone(unresolved.wb_subject_id)
        self.assertEqual(client.calls, 1)

    def test_cancel_keeps_applied_category_rollbackable(self):
        product = self._product(wb_subject_id=self.ANAL_ID)
        run, item, client = self._run(
            product,
            mode='category_and_characteristics',
            batches=1,
        )
        self.assertEqual(item.status, 'pending')
        self.assertTrue(item.category_changed)

        self.assertTrue(SupplierCatalogEnrichmentService.request_cancel(
            run.id, self.supplier_id,
        ))
        SupplierCatalogEnrichmentService.process_run(run.id, batch_limit=1)
        db.session.refresh(run)
        db.session.refresh(item)
        self.assertEqual(run.status, 'cancelled')
        self.assertEqual(item.status, 'applied')
        self.assertIsNotNone(item.after_json)
        self.assertEqual(client.calls, 0)

        SupplierCatalogEnrichmentService.rollback_item(
            item_id=item.id,
            supplier_id=self.supplier_id,
        )
        db.session.refresh(product)
        self.assertEqual(product.wb_category_name, 'Товары для взрослых')

    def test_between_phase_card_edit_is_not_absorbed_into_rollback(self):
        product = self._product(wb_subject_id=self.ANAL_ID)
        run, item, _ = self._run(
            product,
            mode='category_and_characteristics',
            batches=1,
        )
        category_checkpoint = item.after_json
        self.assertTrue(category_checkpoint)

        db.session.add(MarketplaceCategoryCharacteristic(
            marketplace_id=self.marketplace_id,
            category_id=self.anal.id,
            charc_id=8010,
            name='Материал изделия',
            charc_type=1,
            max_count=1,
            required=False,
            dictionary_json=json.dumps(['Силикон'], ensure_ascii=False),
            dictionary_source='wb_schema',
            dictionary_synced_at=self._now(),
            dictionary_version=1,
            is_enabled=True,
            is_available=True,
        ))
        product.marketplace_fields_json = json.dumps(
            {'Ручная правка': 'Не перезаписывать'}, ensure_ascii=False,
        )
        product.content_revision = int(product.content_revision or 1) + 1
        db.session.commit()

        client = _FakeClient([])
        with patch.object(
            SupplierService,
            '_get_ai_service',
            return_value=_FakeAIService(client),
        ):
            SupplierCatalogEnrichmentService.process_run(
                run.id, batch_limit=1,
            )

        db.session.refresh(run)
        db.session.refresh(item)
        db.session.refresh(product)
        self.assertEqual(run.status, 'partial')
        self.assertEqual(item.status, 'applied')
        self.assertEqual(item.error_code, 'card_changed')
        self.assertEqual(item.after_json, category_checkpoint)
        self.assertEqual(client.calls, 0)

        with self.assertRaises(SupplierCatalogEnrichmentError) as error:
            SupplierCatalogEnrichmentService.rollback_item(
                item_id=item.id, supplier_id=self.supplier_id,
            )
        self.assertEqual(error.exception.code, 'rollback_conflict')
        self.assertEqual(product.get_marketplace_fields(), {
            'Ручная правка': 'Не перезаписывать',
        })

    def test_low_confidence_proposal_does_not_write_until_admin_review(self):
        product = self._product()
        response = self._response_for_product(
            product.id, self.ANAL_ID, confidence=0.70,
        )
        run, item, _ = self._run(product, [response])

        db.session.refresh(product)
        self.assertEqual(run.status, 'partial')
        self.assertEqual(item.status, 'needs_review')
        self.assertIsNone(product.wb_subject_id)

        product.marketplace_fields_json = json.dumps(
            {'Ручное поле': 'Сохранить'}, ensure_ascii=False,
        )
        product.marketplace_validation_status = 'manual_reviewed'
        product.content_revision = int(product.content_revision or 1) + 1
        db.session.commit()

        SupplierCatalogEnrichmentService.apply_review_category(
            item_id=item.id,
            supplier_id=self.supplier_id,
            subject_id=self.ANAL_ID,
        )
        db.session.refresh(product)
        db.session.refresh(item)
        self.assertEqual(product.wb_subject_id, self.ANAL_ID)
        self.assertEqual(item.status, 'applied')

        SupplierCatalogEnrichmentService.rollback_item(
            item_id=item.id, supplier_id=self.supplier_id,
        )
        db.session.refresh(product)
        self.assertIsNone(product.wb_subject_id)
        self.assertEqual(item.status, 'rolled_back')
        self.assertEqual(
            product.get_marketplace_fields(), {'Ручное поле': 'Сохранить'},
        )
        self.assertEqual(
            product.marketplace_validation_status, 'manual_reviewed',
        )

    def test_old_review_cannot_mutate_during_new_active_run(self):
        old_product = self._product()
        response = self._response_for_product(
            old_product.id, self.ANAL_ID, confidence=0.70,
        )
        old_run, old_item, _ = self._run(
            old_product,
            [response],
            mode='category_and_characteristics',
        )
        self.assertEqual(old_run.status, 'partial')
        self.assertEqual(old_item.status, 'needs_review')

        new_product = self._product(
            external_id='new-active-run-product',
            wb_subject_id=self.ANAL_ID,
        )
        with patch.object(
            SupplierService,
            '_get_ai_service',
            return_value=_FakeAIService(_FakeClient([])),
        ):
            new_run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[new_product.id],
            )

        with self.assertRaises(SupplierCatalogEnrichmentError) as error:
            SupplierCatalogEnrichmentService.apply_review_category(
                item_id=old_item.id,
                supplier_id=self.supplier_id,
                subject_id=self.ANAL_ID,
            )
        self.assertEqual(error.exception.code, 'run_already_active')
        self.assertIsNone(old_product.wb_subject_id)
        self.assertEqual(new_run.status, 'pending')

    def test_characteristics_require_grounded_value_and_exact_dictionary(self):
        self.anal.characteristics_count = 1
        characteristic = MarketplaceCategoryCharacteristic(
            marketplace_id=self.marketplace_id,
            category_id=self.anal.id,
            charc_id=8001,
            name='Материал изделия',
            charc_type=1,
            max_count=1,
            required=False,
            dictionary_json=json.dumps(['Силикон'], ensure_ascii=False),
            dictionary_source='wb_schema',
            dictionary_synced_at=self._now(),
            dictionary_version=1,
            is_enabled=True,
            is_available=True,
        )
        db.session.add(characteristic)
        product = self._product(
            wb_subject_id=self.ANAL_ID,
            wb_subject_name='Анальные пробки',
            wb_category_name='Анальные пробки',
        )
        db.session.commit()
        response = json.dumps({
            'results': [{
                'product_id': product.id,
                'fields': {
                    'Материал изделия': {
                        'value': ['Силикон'],
                        'evidence': 'Материал изделия: силикон',
                    },
                },
            }],
        }, ensure_ascii=False)
        run, item, client = self._run(
            product, [response], mode='category_and_characteristics', batches=3,
        )

        db.session.refresh(product)
        self.assertEqual(run.status, 'completed')
        self.assertEqual(item.status, 'applied')
        self.assertTrue(item.characteristics_changed)
        self.assertEqual(
            product.get_ai_marketplace_data()['Материал изделия'], ['Силикон'],
        )
        self.assertEqual(
            product.get_marketplace_fields()['Материал изделия'], 'Силикон',
        )
        self.assertEqual(client.calls, 1)

    def test_characteristic_rejects_each_value_missing_from_evidence(self):
        self.anal.characteristics_count = 1
        characteristic = MarketplaceCategoryCharacteristic(
            marketplace_id=self.marketplace_id,
            category_id=self.anal.id,
            charc_id=8002,
            name='Материал изделия',
            charc_type=1,
            max_count=2,
            required=False,
            dictionary_json=json.dumps(
                ['Силикон', 'Пластик'], ensure_ascii=False,
            ),
            dictionary_source='wb_schema',
            dictionary_synced_at=self._now(),
            dictionary_version=1,
            is_enabled=True,
            is_available=True,
        )
        db.session.add(characteristic)
        product = self._product(
            wb_subject_id=self.ANAL_ID,
            wb_subject_name='Анальные пробки',
            wb_category_name='Анальные пробки',
        )
        db.session.commit()
        response = json.dumps({
            'results': [{
                'product_id': product.id,
                'fields': {
                    'Материал изделия': {
                        'value': ['Силикон', 'Пластик'],
                        'evidence': 'Материал изделия: силикон',
                    },
                },
            }],
        }, ensure_ascii=False)
        _run, item, _ = self._run(
            product, [response], mode='category_and_characteristics', batches=3,
        )

        db.session.refresh(product)
        self.assertNotIn(
            'Материал изделия', product.get_ai_marketplace_data(),
        )
        self.assertFalse(item.characteristics_changed)

    def test_seller_sees_revision_and_syncs_shared_characteristics(self):
        product = self._product(
            wb_subject_id=self.ANAL_ID,
            wb_subject_name='Анальные пробки',
            wb_category_name='Анальные пробки',
            ai_marketplace_json=json.dumps({
                'Материал изделия': ['Силикон'],
                '_meta': {'source': 'supplier_catalog_enrichment'},
            }, ensure_ascii=False),
            content_revision=4,
        )
        user = User(
            username='catalog-seller', email='catalog-seller@test.local',
            is_active=True,
        )
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Catalog seller')
        db.session.add(seller)
        db.session.flush()
        imported = ImportedProduct(
            seller_id=seller.id,
            supplier_id=self.supplier_id,
            supplier_product_id=product.id,
            external_id=product.external_id,
            title='Old title',
            supplier_content_revision=1,
        )
        db.session.add(imported)
        db.session.commit()

        result = SupplierService.update_seller_products(
            seller.id, [product.id],
        )
        db.session.refresh(imported)
        self.assertEqual(result.imported, 1)
        self.assertEqual(imported.supplier_content_revision, 4)
        self.assertEqual(imported.wb_subject_id, self.ANAL_ID)
        characteristics = json.loads(imported.characteristics)
        self.assertIn({
            'name': 'Материал изделия', 'value': ['Силикон'],
        }, characteristics)

        product.wb_subject_id = None
        product.wb_subject_name = None
        product.wb_category_name = 'Товары для взрослых'
        product.category_confidence = None
        product.ai_marketplace_json = None
        product.characteristics_json = None
        product.content_revision = 5
        db.session.commit()

        stale_page = SupplierService.get_available_products_for_seller(
            seller.id,
            self.supplier_id,
            page=1,
            per_page=20,
            show_imported=False,
            updates_only=True,
        )
        self.assertEqual([row.id for row in stale_page.items], [product.id])

        SupplierService.update_seller_products(seller.id, [product.id])
        db.session.refresh(imported)
        self.assertEqual(imported.supplier_content_revision, 5)
        self.assertIsNone(imported.wb_subject_id)
        self.assertEqual(imported.mapped_wb_category, 'Товары для взрослых')
        self.assertIsNone(imported.characteristics)

    def test_seller_sync_ignores_legacy_marketplace_container_keys(self):
        product = self._product(
            characteristics_json=json.dumps([
                {'name': 'Материал', 'value': 'Силикон'},
            ], ensure_ascii=False),
            ai_marketplace_json=json.dumps({
                'title': 'Legacy title',
                'characteristics': {'Цвет': 'Красный'},
                'package_dimensions': {'length': 10},
            }, ensure_ascii=False),
            content_revision=2,
        )
        user = User(
            username='legacy-container-seller',
            email='legacy-container@test.local',
            is_active=True,
        )
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Legacy container seller')
        db.session.add(seller)
        db.session.flush()
        imported = ImportedProduct(
            seller_id=seller.id,
            supplier_id=self.supplier_id,
            supplier_product_id=product.id,
            external_id=product.external_id,
            title=product.title,
            supplier_content_revision=1,
        )
        db.session.add(imported)
        db.session.commit()

        SupplierService.update_seller_products(seller.id, [product.id])
        db.session.refresh(imported)
        self.assertEqual(json.loads(imported.characteristics), [
            {'name': 'Материал', 'value': 'Силикон'},
        ])

    def test_stale_reference_blocks_before_any_model_call(self):
        marketplace = db.session.get(Marketplace, self.marketplace_id)
        from datetime import timedelta
        marketplace.categories_synced_at = self._now() - timedelta(hours=49)
        db.session.commit()
        product = self._product()
        client = _FakeClient([])
        with patch.object(
            SupplierService,
            '_get_ai_service',
            return_value=_FakeAIService(client),
        ):
            with self.assertRaises(SupplierCatalogEnrichmentError) as error:
                SupplierCatalogEnrichmentService.create_run(
                    supplier_id=self.supplier_id,
                    admin_user_id=self.admin_id,
                    product_ids=[product.id],
                )
        self.assertEqual(error.exception.code, 'wb_reference_unavailable')
        self.assertEqual(client.calls, 0)
