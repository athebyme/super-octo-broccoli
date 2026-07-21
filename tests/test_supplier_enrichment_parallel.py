# -*- coding: utf-8 -*-
"""Параллельный LLM-путь admin-обогащения характеристик.

SUPPLIER_ENRICHMENT_LLM_CONCURRENCY управляет только числом одновременных
HTTP-вызовов: содержимое чанков, llm-бюджет и fail-closed поведение обязаны
совпадать с последовательным путём.
"""

import json
import threading
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from models import (
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    Supplier,
    SupplierCatalogEnrichmentItem,
    SupplierCatalogEnrichmentRun,
    SupplierProduct,
    User,
    db,
)
from services.supplier_catalog_enrichment import SupplierCatalogEnrichmentService
from services.supplier_service import SupplierService


class _RoutingFakeClient:
    """Потокобезопасный фейк: ответ выбирается по product_id в prompt.

    Опциональный barrier доказывает фактическую одновременность вызовов:
    последовательный путь не смог бы собрать обе стороны рандеву."""

    def __init__(self, responses_by_product_id, barrier=None):
        self.responses = dict(responses_by_product_id)
        self.barrier = barrier
        self.calls = 0
        self.max_parallel = 0
        self._active = 0
        self._lock = threading.Lock()

    def chat_completion(self, messages, **kwargs):
        with self._lock:
            self.calls += 1
            self._active += 1
            self.max_parallel = max(self.max_parallel, self._active)
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        try:
            blob = ' '.join(str(m.get('content', '')) for m in messages)
            for product_id, response in self.responses.items():
                # Матчим entry из payload (сортировка ключей: product_id,source),
                # а не пример формата "product_id":1,"fields" из инструкции.
                if f'"product_id":{product_id},"source"' in blob:
                    return response
            raise AssertionError('Не найден ответ для prompt')
        finally:
            with self._lock:
                self._active -= 1


class _FakeAIService:
    def __init__(self, client):
        self.client = client
        self.config = SimpleNamespace(model='test-model')

    def close(self):
        return None


class ParallelCharacteristicsTest(unittest.TestCase):
    ANAL_ID = 61001
    VIBRATOR_ID = 61002

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='parallel-enrichment',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        admin = User(
            username='parallel-admin', email='parallel-admin@test.local',
            is_admin=True, is_active=True,
        )
        admin.set_password('synthetic-password')
        supplier = Supplier(
            name='Parallel supplier', code='parallel-enrichment-test',
            ai_enabled=True,
        )
        marketplace = Marketplace(
            name='Wildberries', code='wb', is_active=True,
            categories_sync_status='success',
            categories_synced_at=datetime.utcnow(),
            categories_version=7,
            categories_snapshot_hash='a' * 64,
            total_categories=2,
        )
        db.session.add_all([admin, supplier, marketplace])
        db.session.flush()
        self.admin_id = admin.id
        self.supplier_id = supplier.id
        self.marketplace_id = marketplace.id

        for subject_id, subject_name, charc_id in (
            (self.ANAL_ID, 'Анальные пробки', 9001),
            (self.VIBRATOR_ID, 'Вибраторы', 9002),
        ):
            category = MarketplaceCategory(
                marketplace_id=marketplace.id,
                subject_id=subject_id,
                subject_name=subject_name,
                parent_name='Товары для взрослых',
                is_leaf=True,
                is_enabled=True,
                is_available=True,
                characteristics_sync_status='success',
                characteristics_synced_at=datetime.utcnow(),
                characteristics_schema_hash='b' * 64,
                characteristics_version=3,
                characteristics_count=1,
            )
            db.session.add(category)
            db.session.flush()
            db.session.add(MarketplaceCategoryCharacteristic(
                marketplace_id=marketplace.id,
                category_id=category.id,
                charc_id=charc_id,
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
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _product(self, subject_id, subject_name, external_id):
        product = SupplierProduct(
            supplier_id=self.supplier_id,
            external_id=external_id,
            title=f'{subject_name} из силикона',
            description='Материал изделия: силикон.',
            category=subject_name,
            wb_category_name=subject_name,
            wb_subject_id=subject_id,
            wb_subject_name=subject_name,
            content_revision=1,
        )
        db.session.add(product)
        db.session.commit()
        return product

    @staticmethod
    def _response(product_id):
        return json.dumps({
            'results': [{
                'product_id': product_id,
                'fields': {
                    'Материал изделия': {
                        'value': ['Силикон'],
                        'evidence': 'Материал изделия: силикон',
                    },
                },
            }],
        }, ensure_ascii=False)

    def _run_parallel(self, products, client, concurrency='2'):
        def fake_service(*args, **kwargs):
            return _FakeAIService(client)

        with patch.object(
            SupplierService, '_get_ai_service', side_effect=fake_service,
        ), patch.dict(
            'os.environ',
            {'SUPPLIER_ENRICHMENT_LLM_CONCURRENCY': concurrency},
        ):
            run = SupplierCatalogEnrichmentRun.query.filter_by(
                supplier_id=self.supplier_id,
            ).first()
            if not run:
                run = SupplierCatalogEnrichmentService.create_run(
                    supplier_id=self.supplier_id,
                    admin_user_id=self.admin_id,
                    product_ids=[p.id for p in products],
                    mode='category_and_characteristics',
                )
            SupplierCatalogEnrichmentService.process_run(run.id, batch_limit=5)
        return db.session.get(SupplierCatalogEnrichmentRun, run.id)

    def test_two_subject_chunks_processed_in_parallel(self):
        p1 = self._product(self.ANAL_ID, 'Анальные пробки', 'par-1')
        p2 = self._product(self.VIBRATOR_ID, 'Вибраторы', 'par-2')
        client = _RoutingFakeClient(
            {
                p1.id: self._response(p1.id),
                p2.id: self._response(p2.id),
            },
            barrier=threading.Barrier(2),
        )
        run = self._run_parallel([p1, p2], client)

        self.assertEqual(run.status, 'completed')
        items = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run.id,
        ).all()
        self.assertEqual(
            {item.status for item in items}, {'applied'},
        )
        self.assertTrue(all(item.characteristics_changed for item in items))
        # Два чанка = два model-вызова, выполненных одновременно
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.max_parallel, 2)
        self.assertEqual(run.llm_calls, 2)
        db.session.refresh(p1)
        db.session.refresh(p2)
        for product in (p1, p2):
            self.assertEqual(
                product.get_ai_marketplace_data()['Материал изделия'],
                ['Силикон'],
            )

    def test_budget_exhaustion_fails_closed_without_stray_calls(self):
        p1 = self._product(self.ANAL_ID, 'Анальные пробки', 'bud-1')
        p2 = self._product(self.VIBRATOR_ID, 'Вибраторы', 'bud-2')
        client = _RoutingFakeClient({
            p1.id: self._response(p1.id),
            p2.id: self._response(p2.id),
        })

        def fake_service(*args, **kwargs):
            return _FakeAIService(client)

        with patch.object(
            SupplierService, '_get_ai_service', side_effect=fake_service,
        ), patch.dict(
            'os.environ', {'SUPPLIER_ENRICHMENT_LLM_CONCURRENCY': '2'},
        ):
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id,
                admin_user_id=self.admin_id,
                product_ids=[p1.id, p2.id],
                mode='category_and_characteristics',
            )
            # Оставляем бюджет ровно на один вызов: вторая резервация
            # обязана fail-closed завершить run без выполнения вызовов.
            run.llm_calls = run.llm_call_limit - 1
            db.session.commit()
            SupplierCatalogEnrichmentService.process_run(run.id, batch_limit=5)

        run = db.session.get(SupplierCatalogEnrichmentRun, run.id)
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.error_code, 'llm_budget_exhausted')
        # Зарезервированный, но не выполненный вызов не уходит в сеть
        self.assertEqual(client.calls, 0)
        items = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run.id,
        ).all()
        # Категорийный этап уже применён, поэтому checkpoint-семантика
        # оставляет items applied с честным кодом остановки характеристик.
        self.assertEqual({item.status for item in items}, {'applied'})
        self.assertEqual(
            {item.error_code for item in items}, {'llm_budget_exhausted'},
        )


if __name__ == '__main__':
    unittest.main()
