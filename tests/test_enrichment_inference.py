# -*- coding: utf-8 -*-
"""Режим characteristics_inference: безопасные предположения через review."""

import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from models import (
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    MarketplaceDirectory,
    Supplier,
    SupplierCatalogEnrichmentItem,
    SupplierCatalogEnrichmentRun,
    SupplierProduct,
    User,
    db,
)
from services.supplier_catalog_enrichment import (
    MODE_INFERENCE,
    SupplierCatalogEnrichmentError,
    SupplierCatalogEnrichmentService,
)
from services.supplier_service import SupplierService


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.prompts = []

    def chat_completion(self, messages, **kwargs):
        self.calls += 1
        self.prompts.append(messages)
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


class InferenceModeTest(unittest.TestCase):
    SUBJECT_ID = 71001

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='inference-test',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        admin = User(
            username='inference-admin', email='inference-admin@test.local',
            is_admin=True, is_active=True,
        )
        admin.set_password('synthetic-password')
        supplier = Supplier(
            name='Inference supplier', code='inference-test', ai_enabled=True,
        )
        marketplace = Marketplace(
            name='Wildberries', code='wb', is_active=True,
            categories_sync_status='success',
            categories_synced_at=datetime.utcnow(),
            categories_version=7,
            categories_snapshot_hash='a' * 64,
            total_categories=1,
        )
        db.session.add_all([admin, supplier, marketplace])
        db.session.flush()
        self.admin_id = admin.id
        self.supplier_id = supplier.id
        self.marketplace_id = marketplace.id

        category = MarketplaceCategory(
            marketplace_id=marketplace.id,
            subject_id=self.SUBJECT_ID,
            subject_name='Вибраторы',
            parent_name='Товары для взрослых',
            is_leaf=True,
            is_enabled=True,
            is_available=True,
            characteristics_sync_status='success',
            characteristics_synced_at=datetime.utcnow(),
            characteristics_schema_hash='b' * 64,
            characteristics_version=3,
            characteristics_count=2,
        )
        db.session.add(category)
        db.session.flush()
        self.category = category
        # Словарное поле — кандидат на предположения
        db.session.add(MarketplaceCategoryCharacteristic(
            marketplace_id=marketplace.id,
            category_id=category.id,
            charc_id=9101,
            name='Пол',
            charc_type=1,
            max_count=1,
            required=False,
            dictionary_json=json.dumps(
                ['Женский', 'Мужской', 'Унисекс'], ensure_ascii=False,
            ),
            dictionary_source='wb_schema',
            dictionary_synced_at=datetime.utcnow(),
            dictionary_version=1,
            is_enabled=True,
            is_available=True,
        ))
        # Второе словарное поле
        db.session.add(MarketplaceCategoryCharacteristic(
            marketplace_id=marketplace.id,
            category_id=category.id,
            charc_id=9102,
            name='Материал изделия',
            charc_type=1,
            max_count=1,
            required=False,
            dictionary_json=json.dumps(['Силикон'], ensure_ascii=False),
            dictionary_source='wb_schema',
            dictionary_synced_at=datetime.utcnow(),
            dictionary_version=1,
            is_enabled=True,
            is_available=True,
        ))
        # «Пол» валидируется через глобальный справочник kinds
        db.session.add(MarketplaceDirectory(
            marketplace_id=marketplace.id,
            directory_type='kinds',
            data_json=json.dumps(
                ['Женский', 'Мужской', 'Унисекс'], ensure_ascii=False,
            ),
            synced_at=datetime.utcnow(),
            sync_status='success',
            items_count=3,
            version=1,
        ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _product(self, **overrides):
        values = {
            'supplier_id': self.supplier_id,
            'external_id': f'inf-{SupplierProduct.query.count() + 1}',
            'title': 'Вибратор классический',
            'description': 'Описание без явных фактов о поле.',
            'category': 'Вибраторы',
            'wb_category_name': 'Вибраторы',
            'wb_subject_id': self.SUBJECT_ID,
            'wb_subject_name': 'Вибраторы',
            'content_revision': 1,
        }
        values.update(overrides)
        product = SupplierProduct(**values)
        db.session.add(product)
        db.session.commit()
        return product

    def _run(self, products, responses=(), batches=3):
        client = _FakeClient(responses)

        def fake_service(*args, **kwargs):
            return _FakeAIService(client)

        with patch.object(
            SupplierService, '_get_ai_service', side_effect=fake_service,
        ):
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[p.id for p in products],
                mode=MODE_INFERENCE,
            )
            SupplierCatalogEnrichmentService.process_run(
                run.id, batch_limit=batches,
            )
        return db.session.get(SupplierCatalogEnrichmentRun, run.id), client

    @staticmethod
    def _response(product_id, suggestions):
        return json.dumps({
            'results': [{
                'product_id': product_id,
                'suggestions': suggestions,
            }],
        }, ensure_ascii=False)

    def test_fully_filled_product_is_unchanged_without_llm(self):
        product = self._product(
            ai_marketplace_json=json.dumps({
                'Пол': ['Унисекс'],
                'Материал изделия': ['Силикон'],
                '_meta': {'source': 'supplier_catalog_enrichment'},
            }, ensure_ascii=False),
        )
        run, client = self._run([product])
        item = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run.id,
        ).one()
        self.assertEqual(item.status, 'unchanged')
        self.assertEqual(client.calls, 0)
        self.assertEqual(run.status, 'completed')

    def test_prompt_contains_filled_and_only_open_fields(self):
        product = self._product(
            characteristics_json=json.dumps(
                [{'name': 'Материал изделия', 'value': 'Силикон'}],
                ensure_ascii=False,
            ),
        )
        response = self._response(product.id, [{
            'name': 'Пол', 'value': ['Унисекс'],
            'rationale': 'Игрушка без гендерной специфики',
            'confidence': 0.8,
        }])
        run, client = self._run([product], [response])
        self.assertEqual(client.calls, 1)
        blob = ' '.join(m['content'] for m in client.prompts[0])
        self.assertIn('"filled"', blob)
        self.assertIn('Материал изделия', blob)
        # Заполненное поле не предлагается как открытое
        self.assertNotIn('"name":"Материал изделия"', blob.replace(' ', ''))
        self.assertIn('Пол', blob)

    def test_valid_suggestion_goes_to_review_without_touching_product(self):
        product = self._product()
        response = self._response(product.id, [{
            'name': 'Пол', 'value': ['Унисекс'],
            'rationale': 'Типовая игрушка', 'confidence': 0.9,
        }])
        run, client = self._run([product], [response])
        item = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run.id,
        ).one()
        self.assertEqual(item.status, 'needs_review')
        suggestions = json.loads(item.inference_json)
        self.assertEqual(suggestions[0]['name'], 'Пол')
        self.assertEqual(suggestions[0]['value'], ['Унисекс'])
        db.session.refresh(product)
        self.assertIsNone(product.ai_marketplace_json)
        self.assertEqual(product.content_revision, 1)
        self.assertEqual(run.status, 'partial')

    def test_invalid_suggestions_are_dropped(self):
        product = self._product(
            ai_marketplace_json=json.dumps({
                'Материал изделия': ['Силикон'],
                '_meta': {'source': 'supplier_catalog_enrichment'},
            }, ensure_ascii=False),
        )
        response = self._response(product.id, [
            # Уже заполнено — отбрасывается
            {'name': 'Материал изделия', 'value': ['Силикон'],
             'rationale': 'x', 'confidence': 0.9},
            # Значения нет в словаре — отбрасывается на канонизации
            {'name': 'Пол', 'value': ['Марсианский'],
             'rationale': 'x', 'confidence': 0.9},
        ])
        run, client = self._run([product], [response])
        item = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run.id,
        ).one()
        self.assertEqual(item.status, 'unchanged')
        self.assertIsNone(item.inference_json)

    def test_apply_selection_writes_canonical_value_with_snapshot(self):
        product = self._product()
        response = self._response(product.id, [{
            'name': 'Пол', 'value': ['Унисекс'],
            'rationale': 'Типовая игрушка', 'confidence': 0.9,
        }])
        run, client = self._run([product], [response])
        item = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run.id,
        ).one()

        result = SupplierCatalogEnrichmentService.apply_inference_selection(
            run_id=run.id,
            item_id=item.id,
            supplier_id=self.supplier_id,
            admin_user_id=self.admin_id,
            field_names=['Пол'],
        )
        self.assertEqual(result['applied'], ['Пол'])
        db.session.refresh(product)
        data = json.loads(product.ai_marketplace_json)
        self.assertEqual(data['Пол'], ['Унисекс'])
        self.assertEqual(
            data['_meta']['evidence']['Пол'], 'inference: approved by admin',
        )
        self.assertEqual(product.content_revision, 2)
        db.session.refresh(item)
        self.assertEqual(item.status, 'applied')
        self.assertTrue(item.characteristics_changed)
        self.assertTrue(item.before_json)
        self.assertTrue(item.after_json)

    def test_apply_selection_rejects_unknown_field(self):
        product = self._product()
        response = self._response(product.id, [{
            'name': 'Пол', 'value': ['Унисекс'],
            'rationale': 'x', 'confidence': 0.9,
        }])
        run, client = self._run([product], [response])
        item = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run.id,
        ).one()
        with self.assertRaises(SupplierCatalogEnrichmentError):
            SupplierCatalogEnrichmentService.apply_inference_selection(
                run_id=run.id,
                item_id=item.id,
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                field_names=['Материал изделия'],
            )

    def test_apply_selection_conflicts_on_source_drift(self):
        product = self._product()
        response = self._response(product.id, [{
            'name': 'Пол', 'value': ['Унисекс'],
            'rationale': 'x', 'confidence': 0.9,
        }])
        run, client = self._run([product], [response])
        item = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run.id,
        ).one()
        product.title = 'Совсем другой товар'
        db.session.commit()
        with self.assertRaises(SupplierCatalogEnrichmentError) as ctx:
            SupplierCatalogEnrichmentService.apply_inference_selection(
                run_id=run.id,
                item_id=item.id,
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                field_names=['Пол'],
            )
        self.assertEqual(ctx.exception.code, 'source_changed')
        db.session.refresh(product)
        self.assertIsNone(product.ai_marketplace_json)


if __name__ == '__main__':
    unittest.main()
