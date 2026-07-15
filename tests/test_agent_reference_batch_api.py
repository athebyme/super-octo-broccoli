# -*- coding: utf-8 -*-
import json
import unittest
from collections import Counter
from datetime import datetime, timedelta

from flask import Flask
from sqlalchemy import event
from werkzeug.security import generate_password_hash

from models import (
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    MarketplaceDirectory,
    ServiceAgent,
    db,
)
from routes.internal_api import internal_api_bp


class AgentReferenceBatchApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.app.register_blueprint(internal_api_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.agent = ServiceAgent(
            id='reference-batch-agent',
            name='reference-batch-agent',
            display_name='Reference Batch Agent',
            api_key_hash=generate_password_hash('reference-batch-key'),
        )
        self.marketplace = Marketplace(
            name='Wildberries',
            code='wb',
            is_active=True,
            categories_synced_at=datetime.utcnow(),
            categories_sync_status='success',
            total_categories=4,
        )
        db.session.add_all([self.agent, self.marketplace])
        db.session.flush()

        self.enabled = self._add_category(
            1001, 'Футболки', 'Одежда', enabled=True,
        )
        self.disabled = self._add_category(
            1002, 'Туфли', 'Обувь', enabled=False,
        )
        self.stale = self._add_category(
            1003,
            'Устаревшая схема',
            'Тест',
            enabled=True,
            synced_at=datetime.utcnow() - timedelta(hours=49),
        )
        self.unavailable = self._add_category(
            1004, 'Снятая категория', 'Тест', enabled=True,
            available=False,
        )

        self._add_characteristic(
            self.enabled, 2001, 'Цвет товара', required=True,
        )
        self._add_characteristic(
            self.enabled, 2002, 'Длина', charc_type=4,
        )
        self._add_characteristic(
            self.disabled, 2101, 'Страна производства', required=True,
        )
        self._add_characteristic(
            self.stale, 2201, 'Длина', charc_type=4,
        )
        db.session.add_all([
            MarketplaceDirectory(
                marketplace_id=self.marketplace.id,
                directory_type='colors',
                data_json=json.dumps([
                    {'name': 'Красный'},
                    {'name': 'Синий'},
                ], ensure_ascii=False),
                synced_at=datetime.utcnow(),
                sync_status='success',
                items_count=2,
            ),
            MarketplaceDirectory(
                marketplace_id=self.marketplace.id,
                directory_type='countries',
                data_json=json.dumps([
                    {'name': 'Россия'},
                    {'name': 'Китай'},
                ], ensure_ascii=False),
                synced_at=datetime.utcnow(),
                sync_status='success',
                items_count=2,
            ),
        ])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @property
    def auth(self):
        return {
            'X-Agent-Id': self.agent.id,
            'X-Agent-Key': 'reference-batch-key',
        }

    def _add_category(
        self,
        subject_id,
        subject_name,
        parent_name,
        *,
        enabled,
        available=True,
        synced_at=None,
    ):
        category = MarketplaceCategory(
            marketplace_id=self.marketplace.id,
            subject_id=subject_id,
            subject_name=subject_name,
            parent_name=parent_name,
            is_leaf=True,
            is_enabled=enabled,
            is_available=available,
            last_seen_at=datetime.utcnow(),
            characteristics_synced_at=synced_at or datetime.utcnow(),
            characteristics_sync_status='success',
            characteristics_count=1,
        )
        db.session.add(category)
        db.session.flush()
        return category

    def _add_characteristic(
        self,
        category,
        charc_id,
        name,
        *,
        charc_type=1,
        required=False,
    ):
        characteristic = MarketplaceCategoryCharacteristic(
            marketplace_id=self.marketplace.id,
            category_id=category.id,
            charc_id=charc_id,
            name=name,
            charc_type=charc_type,
            required=required,
            max_count=1,
            is_enabled=True,
            is_available=True,
            last_seen_at=datetime.utcnow(),
        )
        db.session.add(characteristic)
        return characteristic

    def test_search_batch_matches_single_contract_and_disabled_fallback(self):
        queries = ['одежда', 'обувь']
        batch_response = self.client.post(
            '/internal/v1/categories/search-batch',
            headers=self.auth,
            json={'queries': queries, 'limit': 10},
        )
        self.assertEqual(batch_response.status_code, 200)
        batch = batch_response.get_json()
        self.assertEqual(batch['count'], len(queries))
        self.assertEqual(
            [item['query'] for item in batch['results']], queries,
        )

        for query, item in zip(queries, batch['results']):
            single = self.client.get(
                '/internal/v1/categories/search',
                headers=self.auth,
                query_string={'q': query, 'limit': 10},
            ).get_json()
            comparable = dict(item)
            comparable.pop('query')
            self.assertEqual(comparable, single)

        disabled = batch['results'][1]
        self.assertEqual(disabled['categories'][0]['subject_id'], 1002)
        self.assertFalse(disabled['categories'][0]['is_enabled'])
        self.assertIn('warning', disabled)

    def test_unusable_global_category_snapshot_skips_category_select(self):
        original_synced_at = self.marketplace.categories_synced_at
        original_status = self.marketplace.categories_sync_status
        cases = (
            (
                datetime.utcnow() - timedelta(hours=49),
                'success',
                'stale_cache',
            ),
            (datetime.utcnow(), 'failed', 'sync_not_successful'),
        )
        for synced_at, sync_status, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                self.marketplace.categories_synced_at = synced_at
                self.marketplace.categories_sync_status = sync_status
                db.session.commit()
                category_selects = []

                def record(
                    connection, cursor, statement, parameters, context,
                    executemany,
                ):
                    normalized = ' '.join(statement.casefold().split())
                    if (
                        normalized.startswith('select')
                        and 'from marketplace_categories' in normalized
                    ):
                        category_selects.append(normalized)

                event.listen(db.engine, 'before_cursor_execute', record)
                try:
                    response = self.client.post(
                        '/internal/v1/categories/search-batch',
                        headers=self.auth,
                        json={'queries': ['одежда', 'обувь']},
                    )
                finally:
                    event.remove(
                        db.engine, 'before_cursor_execute', record,
                    )

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload['count'], 2)
                self.assertEqual(category_selects, [])
                for item in payload['results']:
                    self.assertEqual(item['categories'], [])
                    self.assertEqual(item['count'], 0)
                    self.assertFalse(item['reference_status']['usable'])
                    self.assertEqual(
                        item['reference_status']['reason'], expected_reason,
                    )

        self.marketplace.categories_synced_at = original_synced_at
        self.marketplace.categories_sync_status = original_status
        db.session.commit()

    def test_characteristics_batch_is_exact_and_fail_closed_per_item(self):
        subject_ids = [1001, 999999, 1003, 1004, 1002]
        response = self.client.post(
            '/internal/v1/categories/characteristics-batch',
            headers=self.auth,
            json={'subject_ids': subject_ids},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['count'], len(subject_ids))
        self.assertEqual(
            [item['subject_id'] for item in payload['results']], subject_ids,
        )

        fresh, missing, stale, unavailable, disabled = payload['results']
        self.assertTrue(fresh['reference_status']['usable'])
        self.assertEqual(fresh['count'], 2)
        self.assertFalse(missing['reference_status']['usable'])
        self.assertEqual(missing['reference_status']['reason'], 'not_found')
        self.assertEqual(missing['characteristics'], [])
        self.assertFalse(stale['reference_status']['usable'])
        self.assertEqual(stale['reference_status']['reason'], 'stale_cache')
        self.assertEqual(stale['characteristics'], [])
        self.assertFalse(unavailable['reference_status']['usable'])
        self.assertEqual(
            unavailable['reference_status']['reason'],
            'upstream_unavailable',
        )
        self.assertFalse(disabled['reference_status']['usable'])
        self.assertEqual(
            disabled['reference_status']['reason'], 'category_disabled',
        )
        self.assertEqual(disabled['characteristics'], [])

        single = self.client.get(
            '/internal/v1/categories/1001/characteristics',
            headers=self.auth,
        ).get_json()
        self.assertEqual(fresh, single)

    def test_characteristics_preflight_uses_global_category_freshness(self):
        self.marketplace.categories_synced_at = (
            datetime.utcnow() - timedelta(hours=49)
        )
        db.session.commit()

        response = self.client.post(
            '/internal/v1/categories/characteristics-batch',
            headers=self.auth,
            json={'subject_ids': [self.enabled.subject_id]},
        )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()['results'][0]
        self.assertFalse(result['reference_status']['usable'])
        self.assertEqual(result['reference_status']['reason'], 'stale_cache')
        self.assertEqual(result['characteristics'], [])
        self.assertIn(
            'category_reference_status', result['reference_status'],
        )

    def test_characteristics_required_only_matches_single_contract(self):
        subject_ids = [1001, 1002]
        response = self.client.post(
            '/internal/v1/categories/characteristics-batch',
            headers=self.auth,
            json={
                'subject_ids': subject_ids,
                'required_only': True,
            },
        )
        self.assertEqual(response.status_code, 200)
        results = response.get_json()['results']
        self.assertEqual(
            [item['subject_id'] for item in results], subject_ids,
        )

        for subject_id, item in zip(subject_ids, results):
            single = self.client.get(
                f'/internal/v1/categories/{subject_id}/characteristics',
                headers=self.auth,
                query_string={'required_only': 'true'},
            ).get_json()
            self.assertEqual(item, single)
        self.assertEqual(results[0]['count'], 1)
        self.assertTrue(results[0]['characteristics'][0]['required'])
        self.assertEqual(results[1]['count'], 0)
        self.assertEqual(
            results[1]['reference_status']['reason'], 'category_disabled',
        )

    def test_characteristic_value_search_returns_only_canonical_global_values(self):
        response = self.client.post(
            '/internal/v1/categories/characteristic-values/search-batch',
            headers=self.auth,
            json={'queries': [{
                'subject_id': 1001,
                'charc_id': 2001,
                'query': 'крас',
            }]},
        )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()['results'][0]
        self.assertTrue(result['usable'])
        self.assertTrue(result['constrained'])
        self.assertEqual(result['source'], 'colors')
        self.assertEqual(result['values'], ['Красный'])
        self.assertEqual(result['query'], 'крас')

        duplicate = self.client.post(
            '/internal/v1/categories/characteristic-values/search-batch',
            headers=self.auth,
            json={'queries': [
                {'subject_id': 1001, 'charc_id': 2001, 'query': 'крас'},
                {'subject_id': 1001, 'charc_id': 2001, 'query': 'КРАС'},
            ]},
        )
        invalid_id = self.client.post(
            '/internal/v1/categories/characteristic-values/search-batch',
            headers=self.auth,
            json={'queries': [{
                'subject_id': True, 'charc_id': 2001, 'query': 'крас',
            }]},
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(invalid_id.status_code, 400)

    def test_batch_inputs_are_strict_and_authenticated(self):
        endpoints = (
            ('/internal/v1/categories/search-batch', {'queries': ['одежда']}),
            (
                '/internal/v1/categories/characteristics-batch',
                {'subject_ids': [1001]},
            ),
        )
        for endpoint, body in endpoints:
            with self.subTest(endpoint=endpoint, case='auth'):
                self.assertEqual(
                    self.client.post(endpoint, json=body).status_code, 401,
                )

        invalid_search = (
            None,
            {},
            {'queries': []},
            {'queries': ['x']},
            {'queries': ['одежда', 'ОДЕЖДА']},
            {'queries': [123]},
            {'queries': ['ok'], 'limit': True},
            {'queries': ['ok'], 'limit': 51},
            {'queries': [f'q{index}' for index in range(201)]},
        )
        for body in invalid_search:
            with self.subTest(endpoint='search', body=str(body)[:80]):
                response = self.client.post(
                    '/internal/v1/categories/search-batch',
                    headers=self.auth,
                    json=body,
                )
                self.assertEqual(response.status_code, 400)

        invalid_schemas = (
            None,
            {},
            {'subject_ids': []},
            {'subject_ids': [1001, 1001]},
            {'subject_ids': [True]},
            {'subject_ids': ['1001']},
            {'subject_ids': [0]},
            {'subject_ids': [1001], 'required_only': 'true'},
            {'subject_ids': list(range(1, 202))},
        )
        for body in invalid_schemas:
            with self.subTest(endpoint='schema', body=str(body)[:80]):
                response = self.client.post(
                    '/internal/v1/categories/characteristics-batch',
                    headers=self.auth,
                    json=body,
                )
                self.assertEqual(response.status_code, 400)

    def test_reference_batch_query_count_is_constant(self):
        extra_ids = []
        for offset in range(12):
            subject_id = 1100 + offset
            category = self._add_category(
                subject_id,
                f'Футболки {offset}',
                f'Группа {offset}',
                enabled=True,
            )
            self._add_characteristic(
                category, 3000 + offset * 2, 'Цвет', required=True,
            )
            self._add_characteristic(
                category,
                3001 + offset * 2,
                'Страна производства',
                required=True,
            )
            category.characteristics_count = 2
            extra_ids.append(subject_id)
        self.marketplace.total_categories += len(extra_ids)
        db.session.commit()

        def measured(endpoint, body):
            statements = Counter()

            def record(
                connection, cursor, statement, parameters, context,
                executemany,
            ):
                normalized = ' '.join(statement.casefold().split())
                if not normalized.startswith('select'):
                    return
                for table in (
                    'marketplaces',
                    'marketplace_categories',
                    'marketplace_category_characteristics',
                    'marketplace_directories',
                ):
                    if f'from {table}' in normalized:
                        statements[table] += 1

            event.listen(db.engine, 'before_cursor_execute', record)
            try:
                response = self.client.post(
                    endpoint, headers=self.auth, json=body,
                )
            finally:
                event.remove(db.engine, 'before_cursor_execute', record)
            self.assertEqual(response.status_code, 200)
            return statements

        one_search = measured(
            '/internal/v1/categories/search-batch',
            {'queries': ['группа 0']},
        )
        many_search = measured(
            '/internal/v1/categories/search-batch',
            {'queries': [f'группа {index}' for index in range(12)]},
        )
        self.assertEqual(one_search, many_search)
        self.assertEqual(one_search['marketplaces'], 1)
        self.assertEqual(one_search['marketplace_categories'], 1)

        one_schema = measured(
            '/internal/v1/categories/characteristics-batch',
            {'subject_ids': extra_ids[:1]},
        )
        many_schemas = measured(
            '/internal/v1/categories/characteristics-batch',
            {'subject_ids': extra_ids},
        )
        self.assertEqual(one_schema, many_schemas)
        self.assertEqual(one_schema['marketplaces'], 1)
        self.assertEqual(one_schema['marketplace_categories'], 1)
        self.assertEqual(
            one_schema['marketplace_category_characteristics'], 1,
        )
        self.assertEqual(one_schema['marketplace_directories'], 1)


if __name__ == '__main__':
    unittest.main()
