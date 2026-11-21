"""Exact seller-owned product handoff into the legacy WB price workflow."""

import os
from pathlib import Path
import shutil
import subprocess
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event


ROOT = Path(__file__).resolve().parents[1]


class SafePriceHandoffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        os.environ.setdefault('SKIP_SCHEDULER', '1')
        import sqlalchemy as sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa: F401
        from models import db

        cls.app = seller_platform.app
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SQLALCHEMY_ENGINE_OPTIONS={},
            WTF_CSRF_ENABLED=False,
            TESTING=True,
        )
        cls._engine = sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls._engine}
        cls.db = db

    def setUp(self):
        from models import Product, SafePriceChangeSettings, Seller, User

        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.session.remove()
        self.db.drop_all()
        self.db.create_all()

        owner_user = User(
            username='price-handoff-owner',
            email='price-handoff-owner@example.test',
            is_active=True,
        )
        owner_user.set_password('synthetic-password')
        self.seller = Seller(user=owner_user, company_name='Price handoff')
        foreign_user = User(
            username='price-handoff-other',
            email='price-handoff-other@example.test',
            is_active=True,
        )
        foreign_user.set_password('synthetic-password')
        foreign_seller = Seller(user=foreign_user, company_name='Other seller')
        self.db.session.add_all([self.seller, foreign_seller])
        self.db.session.flush()

        self.first = Product(
            seller_id=self.seller.id,
            nm_id=770101,
            vendor_code='local-101',
            title='First local product',
            is_active=True,
            price=Decimal('1000'),
        )
        self.second = Product(
            seller_id=self.seller.id,
            nm_id=770102,
            vendor_code='local-102',
            title='Second local product',
            is_active=True,
            price=Decimal('2000'),
        )
        self.inactive = Product(
            seller_id=self.seller.id,
            nm_id=770103,
            vendor_code='local-103',
            title='Inactive local product',
            is_active=False,
            price=Decimal('3000'),
        )
        self.no_wb_id = Product(
            seller_id=self.seller.id,
            nm_id=0,
            vendor_code='local-105',
            title='Product without a WB card ID',
            is_active=True,
            price=Decimal('5000'),
        )
        self.foreign = Product(
            seller_id=foreign_seller.id,
            nm_id=770104,
            vendor_code='foreign-104',
            title='Foreign product',
            is_active=True,
            price=Decimal('4000'),
        )
        self.db.session.add_all([
            self.first, self.second, self.inactive, self.no_wb_id, self.foreign,
        ])
        self.db.session.commit()

        self.owner = SimpleNamespace(
            id=owner_user.id,
            username=owner_user.username,
            seller=self.seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )
        self.client = self.app.test_client()
        self.settings_model = SafePriceChangeSettings

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _request_as_owner(self, method, path, **kwargs):
        with patch('flask_login.utils._get_user', return_value=self.owner), \
                patch('routes.safe_prices.current_user', self.owner), \
                patch('routes.safe_prices.get_current_seller', return_value=self.seller):
            return getattr(self.client, method)(path, **kwargs)

    def _watch_db_writes(self):
        writes = []
        commits = []

        def on_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            first_word = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ''
            if first_word in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                writes.append(statement)

        def on_commit(_session):
            commits.append(True)

        engine = self.db.engine
        session_class = self.db.session.session_factory.class_
        event.listen(engine, 'before_cursor_execute', on_sql)
        event.listen(session_class, 'after_commit', on_commit)
        return engine, session_class, on_sql, on_commit, writes, commits

    def _stop_watching_db_writes(self, watch):
        engine, session_class, on_sql, on_commit, _writes, _commits = watch
        event.remove(engine, 'before_cursor_execute', on_sql)
        event.remove(session_class, 'after_commit', on_commit)

    def test_invalid_handoff_stops_before_price_settings_or_fallback(self):
        too_many_ids = ','.join(str(value) for value in range(1, 202))
        cases = (
            (f'/prices/change?product_ids={self.first.id},,{self.second.id}', 400),
            (f'/prices/change?product_ids={self.first.id},{self.first.id}', 400),
            (f'/prices/change?product_ids={too_many_ids}', 400),
            ('/prices/change?product_ids=9007199254740992', 400),
            ('/prices/change?product_ids=1&product_ids=2', 400),
            (f'/prices/change?product_ids={self.inactive.id}', 400),
            (f'/prices/change?product_ids={self.no_wb_id.id}', 400),
            (f'/prices/change?product_ids={self.foreign.id}', 404),
        )
        with patch(
            'routes.safe_prices.get_or_create_settings',
            side_effect=AssertionError('invalid handoff must not create settings'),
        ), patch('routes.safe_prices.WildberriesAPIClient', side_effect=AssertionError):
            for path, expected_status in cases:
                with self.subTest(path=path):
                    response = self._request_as_owner('get', path)
                    self.assertEqual(response.status_code, expected_status)
                    self.assertIn('Вернуться'.encode('utf-8'), response.data)
                    self.assertIn(b'/my-products/beta', response.data)
        self.assertEqual(
            self.settings_model.query.filter_by(seller_id=self.seller.id).count(), 0,
        )

    def test_valid_handoff_page_and_product_read_are_exact_and_account_scoped(self):
        ids = [self.second.id, self.first.id]
        self.assertEqual(self.settings_model.query.count(), 0)
        watch = self._watch_db_writes()
        try:
            direct = self._request_as_owner('get', '/prices/change')
            exact = self._request_as_owner(
                'get', f'/prices/change?product_ids={ids[0]},{ids[1]}',
            )
            products = self._request_as_owner(
                'get', f'/prices/api/products?product_ids={ids[0]},{ids[1]}',
            )
        finally:
            self._stop_watching_db_writes(watch)

        writes, commits = watch[-2:]
        self.assertEqual(writes, [])
        self.assertEqual(commits, [])
        self.assertEqual(self.settings_model.query.count(), 0)

        self.assertEqual(direct.status_code, 200)
        self.assertIn('Синхронизировать с WB'.encode(), direct.data)
        self.assertIn(b'safeThreshold: 10.0', direct.data)
        self.assertIn(b'warningThreshold: 20.0', direct.data)

        self.assertEqual(exact.status_code, 200)
        self.assertIn('Выбор закреплён: 2 товаров из «Мои товары»'.encode(), exact.data)
        self.assertIn(f'priceChangeApp(true, [{ids[0]}, {ids[1]}]'.encode(), exact.data)
        self.assertIn(b'data-price-workspace-return', exact.data)
        self.assertIn('Вернуться к товарам'.encode(), exact.data)
        self.assertIn(b'common-product-content.js?v=20261121-price-result-return-r41', exact.data)
        self.assertNotIn('Синхронизировать с WB'.encode(), exact.data)
        self.assertNotIn('Сначала синхронизируйте цены'.encode(), exact.data)
        self.assertIn('if (this.selectionHandoff || this.syncingWb) return;'.encode(), exact.data)
        self.assertIn(b'safeThreshold: 10.0', exact.data)
        self.assertIn(b'warningThreshold: 20.0', exact.data)

        self.assertEqual(products.status_code, 200)
        payload = products.get_json()
        self.assertTrue(payload['exact_selection'])
        self.assertEqual([row['id'] for row in payload['products']], ids)
        self.assertEqual(payload['total'], 2)

    def test_preview_exact_flag_rechecks_owned_active_ids(self):
        response = self._request_as_owner(
            'post', '/prices/api/preview',
            json={
                'exact_selection': True,
                'product_ids': [self.second.id, self.first.id],
                'change_type': 'set',
                'change_value': 2100,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['exact_selection'])
        self.assertEqual(
            [row['product_id'] for row in payload['items']],
            [self.second.id, self.first.id],
        )

        with patch(
            'routes.safe_prices.get_or_create_settings',
            side_effect=AssertionError('stale exact selection must stop first'),
        ):
            response = self._request_as_owner(
                'post', '/prices/api/preview',
                json={
                    'exact_selection': True,
                    'product_ids': [self.foreign.id],
                    'change_type': 'set',
                    'change_value': 2100,
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'exact_selection_changed')

    def test_exact_json_mode_requires_a_boolean_flag(self):
        with patch(
            'routes.safe_prices.get_or_create_settings',
            side_effect=AssertionError('invalid exact JSON must stop before settings'),
        ):
            preview = self._request_as_owner(
                'post', '/prices/api/preview',
                json={'exact_selection': 'true', 'product_ids': [self.first.id]},
            )
            batch = self._request_as_owner(
                'post', '/prices/create-batch',
                json={'exact_selection': 'true', 'product_ids': [self.first.id]},
            )
        self.assertEqual(preview.status_code, 400)
        self.assertEqual(batch.status_code, 400)

    def test_create_exact_batch_contains_only_selected_ids(self):
        response = self._request_as_owner(
            'post', '/prices/create-batch',
            json={
                'exact_selection': True,
                'product_ids': [self.second.id],
                'change_type': 'set',
                'change_value': 2100,
                'name': 'Selected product',
                'description': '',
                'price_source': 'local',
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(
            payload['redirect'], f"/prices/batch/{payload['batch_id']}",
        )
        from models import PriceChangeBatch
        batch = self.db.session.get(PriceChangeBatch, payload['batch_id'])
        self.assertEqual([item.product_id for item in batch.items], [self.second.id])

    def _review_request(self, product_ids, **overrides):
        payload = {
            'reviewed_write': True,
            'product_ids': product_ids,
            'change_type': 'set',
            'change_value': 2100,
            'formula': '',
            'price_source': 'local',
            'exact_selection': False,
        }
        payload.update(overrides)
        return payload

    def test_reviewed_preview_is_read_only_authoritative_and_saves_only_changed_rows(self):
        from models import PriceChangeBatch, PriceChangeItem

        watch = self._watch_db_writes()
        try:
            preview = self._request_as_owner(
                'post', '/prices/api/preview',
                json=self._review_request(
                    [self.first.id, self.second.id],
                    exact_selection=True,
                    change_value=2000,
                ),
            )
        finally:
            self._stop_watching_db_writes(watch)

        writes, commits = watch[-2:]
        self.assertEqual(writes, [])
        self.assertEqual(commits, [])
        self.assertEqual(preview.status_code, 200)
        reviewed = preview.get_json()
        self.assertTrue(reviewed['reviewed'])
        self.assertEqual(reviewed['stats']['selected_count'], 2)
        self.assertEqual(reviewed['stats']['changed_count'], 1)
        self.assertEqual(reviewed['stats']['no_op_count'], 1)
        self.assertEqual(reviewed['stats']['total'], 1)
        self.assertTrue(reviewed['can_create'])
        self.assertTrue(reviewed['review_token'])
        self.assertEqual(
            [row['status'] for row in reviewed['items']],
            ['changed', 'no_op'],
        )
        self.assertEqual(reviewed['items'][0]['old_price'], 1000)
        self.assertEqual(reviewed['items'][0]['new_price'], 2000)

        create_payload = self._review_request(
            [self.first.id, self.second.id],
            exact_selection=True,
            change_value=2000,
            review_token=reviewed['review_token'],
            name='Reviewed subset',
        )
        with patch('routes.safe_prices.WildberriesAPIClient', side_effect=AssertionError):
            response = self._request_as_owner(
                'post', '/prices/create-batch', json=create_payload,
            )
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result['reviewed_selection'], 'wb_reviewed_selection_v1')
        self.assertEqual(result['selected_count'], 2)
        self.assertEqual(result['changed_count'], 1)
        self.assertEqual(result['stats']['total'], 1)
        batch = self.db.session.get(PriceChangeBatch, result['batch_id'])
        self.assertEqual(batch.total_items, 1)
        self.assertEqual(
            [item.product_id for item in PriceChangeItem.query.filter_by(batch_id=batch.id)],
            [self.first.id],
        )

    def test_reviewed_price_states_keep_zero_distinct_from_unknown_and_scope_fallback(self):
        from datetime import datetime

        self.first.wb_price = Decimal('0')
        self.first.wb_price_synced_at = datetime(2026, 11, 21, 10, 30)
        self.second.wb_price = None
        self.second.wb_price_synced_at = None
        self.db.session.commit()

        catalog = self._request_as_owner(
            'get',
            f'/prices/api/products?product_ids={self.first.id},{self.second.id}',
        )
        self.assertEqual(catalog.status_code, 200)
        catalog_rows = catalog.get_json()['products']
        self.assertEqual(catalog_rows[0]['wb_price'], 0)
        self.assertIsNone(catalog_rows[1]['wb_price'])

        ordinary = self._request_as_owner(
            'post', '/prices/api/preview',
            json=self._review_request(
                [self.first.id, self.second.id],
                price_source='marketplace',
                change_value=100,
            ),
        )
        self.assertEqual(ordinary.status_code, 200)
        rows = ordinary.get_json()['items']
        self.assertEqual(rows[0]['source_kind'], 'wb_seller_price')
        self.assertEqual(rows[0]['old_price'], 0)
        self.assertEqual(rows[0]['new_price'], 100)
        self.assertEqual(rows[0]['status'], 'changed')
        self.assertEqual(rows[0]['source_freshness_at'], '2026-11-21T10:30:00')
        self.assertIsNone(rows[1]['old_price'])
        self.assertEqual(rows[1]['status'], 'skipped')

        exact = self._request_as_owner(
            'post', '/prices/api/preview',
            json=self._review_request(
                [self.second.id],
                exact_selection=True,
                price_source='marketplace',
                change_value=100,
            ),
        )
        self.assertEqual(exact.status_code, 200)
        exact_row = exact.get_json()['items'][0]
        self.assertEqual(exact_row['source_kind'], 'local_fallback')
        self.assertEqual(exact_row['old_price'], 2000)
        self.assertEqual(exact_row['status'], 'changed')

    def test_reviewed_preview_classifies_noop_skipped_invalid_and_server_formula_errors(self):
        from models import Product

        no_price = Product(
            seller_id=self.seller.id,
            nm_id=770106,
            vendor_code='unknown-price',
            title='No local price',
            is_active=True,
            price=None,
        )
        self.db.session.add(no_price)
        self.db.session.commit()

        response = self._request_as_owner(
            'post', '/prices/api/preview',
            json=self._review_request(
                [self.first.id, self.second.id, no_price.id],
                change_type='fixed',
                change_value=0,
            ),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            [row['status'] for row in payload['items']],
            ['no_op', 'no_op', 'skipped'],
        )
        self.assertEqual(payload['stats']['changed_count'], 0)
        self.assertFalse(payload['can_create'])

        invalid = self._request_as_owner(
            'post', '/prices/api/preview',
            json=self._review_request([self.first.id], change_value=0, change_type='set'),
        )
        self.assertEqual(invalid.status_code, 200)
        invalid_row = invalid.get_json()['items'][0]
        self.assertEqual(invalid_row['status'], 'invalid')
        self.assertEqual(invalid_row['new_price'], 0)

        bad_formula = self._request_as_owner(
            'post', '/prices/api/preview',
            json=self._review_request(
                [self.first.id], change_type='formula', change_value=None,
                formula='P ** 11',
            ),
        )
        self.assertEqual(bad_formula.status_code, 200)
        self.assertEqual(bad_formula.get_json()['items'][0]['status'], 'invalid')

    def test_review_token_rejects_parameter_and_source_drift_before_batch_insert(self):
        from models import PriceChangeBatch

        request = self._review_request([self.first.id], change_value=1200)
        preview = self._request_as_owner('post', '/prices/api/preview', json=request)
        token = preview.get_json()['review_token']

        changed_input = dict(request, change_value=1300, review_token=token)
        changed = self._request_as_owner(
            'post', '/prices/create-batch', json=changed_input,
        )
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(PriceChangeBatch.query.count(), 0)

        tampered_token = token[:-1] + ('A' if token[-1] != 'A' else 'B')
        rejected = self._request_as_owner(
            'post', '/prices/create-batch',
            json=dict(request, review_token=tampered_token),
        )
        self.assertEqual(rejected.status_code, 409)

        self.first.price = Decimal('1001')
        self.db.session.commit()
        drifted = self._request_as_owner(
            'post', '/prices/create-batch',
            json=dict(request, review_token=token),
        )
        self.assertEqual(drifted.status_code, 409)
        self.assertEqual(drifted.get_json()['code'], 'review_stale')
        self.assertEqual(PriceChangeBatch.query.count(), 0)

    def test_review_token_binds_order_owner_settings_and_expiry(self):
        from models import PriceChangeBatch, SafePriceChangeSettings

        ordered_request = self._review_request(
            [self.first.id, self.second.id],
            exact_selection=True,
            change_value=2500,
        )
        preview = self._request_as_owner(
            'post', '/prices/api/preview', json=ordered_request,
        )
        token = preview.get_json()['review_token']

        reordered = self._request_as_owner(
            'post', '/prices/create-batch',
            json=dict(
                ordered_request,
                product_ids=[self.second.id, self.first.id],
                review_token=token,
            ),
        )
        self.assertEqual(reordered.status_code, 409)

        with patch('flask_login.utils._get_user', return_value=self.owner), \
                patch('routes.safe_prices.current_user', SimpleNamespace(
                    id=self.owner.id + 99,
                    seller=self.seller,
                    is_authenticated=True,
                )), \
                patch('routes.safe_prices.get_current_seller', return_value=self.seller):
            wrong_owner = self.client.post(
                '/prices/create-batch',
                json=dict(ordered_request, review_token=token),
            )
        self.assertEqual(wrong_owner.status_code, 409)

        settings = SafePriceChangeSettings(
            seller_id=self.seller.id,
            safe_threshold_percent=5,
            warning_threshold_percent=10,
            mode='confirm',
            allow_unlimited_batch=True,
            max_products_per_batch=1000,
        )
        self.db.session.add(settings)
        self.db.session.commit()
        settings_drift = self._request_as_owner(
            'post', '/prices/create-batch',
            json=dict(ordered_request, review_token=token),
        )
        self.assertEqual(settings_drift.status_code, 409)
        self.assertEqual(settings_drift.get_json()['code'], 'review_stale')

        fresh = self._request_as_owner(
            'post', '/prices/api/preview', json=self._review_request(
                [self.first.id], change_value=1200,
            ),
        ).get_json()['review_token']
        with patch('routes.safe_prices.PRICE_REVIEW_TOKEN_MAX_AGE_SECONDS', -1):
            expired = self._request_as_owner(
                'post', '/prices/create-batch',
                json=self._review_request(
                    [self.first.id], change_value=1200, review_token=fresh,
                ),
            )
        self.assertEqual(expired.status_code, 409)
        self.assertIn('устарел', expired.get_json()['error'])
        self.assertEqual(PriceChangeBatch.query.count(), 0)

    def test_reviewed_guardrail_and_configured_selection_limit_block_save(self):
        from models import PriceChangeBatch, SafePriceChangeSettings

        settings = SafePriceChangeSettings(
            seller_id=self.seller.id,
            mode='block',
            safe_threshold_percent=10,
            warning_threshold_percent=20,
            allow_unlimited_batch=False,
            max_products_per_batch=1,
        )
        self.db.session.add(settings)
        self.db.session.commit()

        limited = self._request_as_owner(
            'post', '/prices/api/preview',
            json=self._review_request(
                [self.first.id, self.second.id], change_type='fixed', change_value=1,
            ),
        )
        self.assertEqual(limited.status_code, 400)
        self.assertIn('Превышен лимит', limited.get_json()['error'])

        dangerous_preview = self._request_as_owner(
            'post', '/prices/api/preview',
            json=self._review_request(
                [self.first.id], change_type='set', change_value=2000,
            ),
        )
        self.assertEqual(dangerous_preview.status_code, 200)
        reviewed = dangerous_preview.get_json()
        self.assertTrue(reviewed['guardrail_blocked'])
        self.assertFalse(reviewed['can_create'])
        blocked = self._request_as_owner(
            'post', '/prices/create-batch',
            json=self._review_request(
                [self.first.id], change_type='set', change_value=2000,
                review_token=reviewed['review_token'],
            ),
        )
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.get_json()['code'], 'price_change_blocked')
        self.assertEqual(PriceChangeBatch.query.count(), 0)

    def test_reviewed_lane_preserves_all_selected_ids_above_legacy_handoff_cap(self):
        from models import Product

        products = [Product(
            seller_id=self.seller.id,
            nm_id=780000 + index,
            vendor_code=f'bulk-{index}',
            title=f'Bulk {index}',
            is_active=True,
            price=Decimal('100'),
        ) for index in range(605)]
        self.db.session.add_all(products)
        self.db.session.commit()
        ids = [product.id for product in products]

        response = self._request_as_owner(
            'post', '/prices/api/preview',
            json=self._review_request(ids, change_type='fixed', change_value=1),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['stats']['selected_count'], 605)
        self.assertEqual(payload['stats']['changed_count'], 605)
        self.assertEqual([row['product_id'] for row in payload['items']], ids)
        self.assertLess(len(payload['review_token']), 4096)

    def test_page_requires_review_and_contains_no_browser_formula_evaluator(self):
        response = self._request_as_owner('get', '/prices/change')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Рассчитать и проверить изменения'.encode(), response.data)
        self.assertIn(b'canCreateReviewedBatch()', response.data)
        self.assertIn(b'reviewed_write: true', response.data)
        self.assertIn('Сохранить проверенные изменения'.encode(), response.data)
        self.assertNotIn(b'eval(', response.data)
        self.assertNotIn(b'calculateNewPrice', response.data)

    def _pending_review_batch(self, name):
        from models import PriceChangeBatch, PriceChangeItem, SafePriceChangeSettings

        settings = SafePriceChangeSettings.query.filter_by(
            seller_id=self.seller.id,
        ).first()
        if settings is None:
            settings = SafePriceChangeSettings(
                seller_id=self.seller.id,
                mode='confirm',
                require_comment_for_dangerous=False,
            )
            self.db.session.add(settings)
        else:
            settings.mode = 'confirm'
            settings.require_comment_for_dangerous = False
        batch = PriceChangeBatch(
            seller_id=self.seller.id,
            name=name,
            change_type='set',
            change_value=2000,
            status='pending_review',
            has_dangerous_changes=True,
            total_items=1,
            dangerous_count=1,
        )
        self.db.session.add(batch)
        self.db.session.flush()
        self.db.session.add(PriceChangeItem(
            batch_id=batch.id,
            product_id=self.first.id,
            nm_id=self.first.nm_id,
            vendor_code=self.first.vendor_code,
            product_title=self.first.title,
            old_price=1000,
            new_price=2000,
            price_change_amount=1000,
            price_change_percent=100,
            safety_level='dangerous',
        ))
        self.db.session.commit()
        return batch

    def test_confirm_and_reject_relay_only_the_navigation_marker(self):
        from models import PriceChangeBatch

        nonce = '0123456789abcdef0123456789abcdef'
        confirm_batch = self._pending_review_batch('Confirm route result')
        confirm_page = self._request_as_owner(
            'get', f'/prices/batch/{confirm_batch.id}/confirm',
        )
        self.assertEqual(confirm_page.status_code, 200)
        self.assertIn(b'data-price-result-confirm-form', confirm_page.data)
        self.assertIn(b'name="action" value="reject" formnovalidate', confirm_page.data)
        self.assertNotIn(b'name="action" value="confirm" formnovalidate', confirm_page.data)
        self.assertIn(b'priceResultWorkspaceReturnHref', confirm_page.data)
        self.assertIn(b'20261121-price-result-return-r41', confirm_page.data)

        confirmed = self._request_as_owner(
            'post', f'/prices/batch/{confirm_batch.id}/confirm',
            data={'action': 'confirm', 'workspace_return': nonce},
        )
        self.assertEqual(confirmed.status_code, 302)
        self.assertEqual(
            confirmed.location,
            f'/prices/batch/{confirm_batch.id}#workspace-return={nonce}',
        )
        self.assertEqual(
            self.db.session.get(PriceChangeBatch, confirm_batch.id).status,
            'confirmed',
        )
        confirmed_detail = self._request_as_owner(
            'get', f'/prices/batch/{confirm_batch.id}',
        )
        self.assertEqual(confirmed_detail.status_code, 200)
        self.assertIn(b'data-price-workspace-return', confirmed_detail.data)

        reject_batch = self._pending_review_batch('Reject route result')
        rejected = self._request_as_owner(
            'post', f'/prices/batch/{reject_batch.id}/confirm',
            data={'action': 'reject', 'workspace_return': nonce},
        )
        self.assertEqual(rejected.status_code, 302)
        self.assertEqual(
            rejected.location,
            f'/prices/batch/{reject_batch.id}#workspace-return={nonce}',
        )
        self.assertEqual(
            self.db.session.get(PriceChangeBatch, reject_batch.id).status,
            'cancelled',
        )
        rejected_detail = self._request_as_owner(
            'get', f'/prices/batch/{reject_batch.id}',
        )
        self.assertEqual(rejected_detail.status_code, 200)
        self.assertIn(b'data-price-workspace-return', rejected_detail.data)

    def test_legacy_reject_redirect_does_not_accept_a_malformed_marker(self):
        batch = self._pending_review_batch('Legacy direct result')
        rejected = self._request_as_owner(
            'post', f'/prices/batch/{batch.id}/confirm',
            data={'action': 'reject', 'workspace_return': 'not-a-nonce'},
        )
        self.assertEqual(rejected.status_code, 302)
        self.assertEqual(rejected.location, '/prices/')

    def test_client_binds_only_the_exact_canonical_committed_batch_result(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node.js is required for the route binding contract')
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const match = html.match(/<script>\s*function priceChangeApp[\s\S]*?<\/script>/);
assert.ok(match, 'priceChangeApp script is present');
const source = match[0].replace(/^<script>|<\/script>$/g, '')
  .replace(/\{\{[\s\S]*?\}\}/g, '0');
let received;
const hash = '#workspace-return=0123456789abcdef0123456789abcdef';
const context = {
  URL,
  URLSearchParams,
  window: {
    location: {hash, origin:'https://seller.test'},
    SellerHubCommonContent: {
      bindPriceWorkspaceResult(value, payload) {
        received = {value, payload};
        return value;
      },
    },
  },
};
vm.createContext(context);
vm.runInContext(source + ';globalThis.__priceChangeApp = priceChangeApp;', context);
const app = context.__priceChangeApp(true, [7001, 7002], {
  owner:'7:9', workspaceUrl:'/my-products/beta',
});
app.selectedIds = [7001, 7002];
const data = {
  success:true, batch_id:91, status:'pending_review', stats:{total:2},
  redirect:'/prices/batch/91/confirm',
};
const exact = app.exactBatchResult(data);
assert.equal(exact.href, '/prices/batch/91/confirm');
assert.equal(exact.canonicalRedirect, true);
assert.equal(app.bindExactBatchReturn(data, exact.canonicalRedirect), hash);
assert.equal(received.value, hash);
assert.deepEqual(JSON.parse(JSON.stringify(received.payload)), {
  owner:'7:9',
  priceTarget:{channel:'wb', ids:[7001, 7002], accountId:null},
  priceResult:{channel:'wb', batchId:91},
});

for (const redirect of [
  'https://outside.test/prices/batch/91/confirm',
  '//outside.test/prices/batch/91/confirm',
  '/prices/batch/92/confirm',
  '/prices/batch/91/confirm?next=/outside',
  '/prices/batch/91/confirm#other',
]) {
  const changed = {...data, redirect};
  const candidate = app.exactBatchResult(changed);
  assert.equal(candidate.href, '/prices/batch/91/confirm');
  assert.equal(candidate.canonicalRedirect, false);
  assert.equal(app.bindExactBatchReturn(changed, candidate.canonicalRedirect), '');
}
assert.equal(app.exactBatchResult({...data, status:'applied'}), null);
assert.equal(app.exactBatchResult({...data, stats:{total:1}}), null);
const reviewedSubset = {
  ...data,
  reviewed_write:true,
  reviewed_selection:'wb_reviewed_selection_v1',
  selected_count:2,
  changed_count:1,
  stats:{total:1},
};
assert.equal(app.exactBatchResult(reviewedSubset).href, '/prices/batch/91/confirm');
assert.equal(app.exactBatchResult({...reviewedSubset, selected_count:1}), null);
assert.equal(app.exactBatchResult({...reviewedSubset, changed_count:3}), null);
app.selectedIds = [7002, 7001];
assert.equal(app.exactBatchResult(data), null);

const normal = context.__priceChangeApp(false, []);
normal.selectedIds = [8001, 8002];
normal.changeValue = 10;
normal.preview = {review_token:'signed', can_create:true, stats:{changed_count:2}};
normal.previewInputKey = normal.reviewInputKey();
assert.equal(normal.canCreateReviewedBatch(), true);
normal.changeValue = 11;
assert.equal(normal.reviewIsCurrent(), false);
assert.equal(normal.canCreateReviewedBatch(), false);
normal.invalidateReview();
assert.equal(normal.preview, null);
assert.equal(normal.previewInputKey, '');
"""
        result = subprocess.run(
            [node, '-e', script, str(ROOT / 'templates/prices_change.html')],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_drift_before_calculation_returns_409_without_a_batch(self):
        import routes.safe_prices as safe_prices
        from models import PriceChangeBatch, Product

        resolve = safe_prices._resolve_exact_active_products
        calls = 0

        def deactivate_between_checks(seller_id, product_ids):
            nonlocal calls
            calls += 1
            if calls == 2:
                product = self.db.session.get(Product, self.second.id)
                product.is_active = False
                self.db.session.flush()
            return resolve(seller_id, product_ids)

        with patch(
            'routes.safe_prices._resolve_exact_active_products',
            side_effect=deactivate_between_checks,
        ):
            response = self._request_as_owner(
                'post', '/prices/create-batch',
                json={
                    'exact_selection': True,
                    'product_ids': [self.second.id],
                    'change_type': 'set',
                    'change_value': 2100,
                    'name': 'Drifted selection',
                    'description': '',
                    'price_source': 'local',
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(PriceChangeBatch.query.filter_by(seller_id=self.seller.id).count(), 0)

    def test_drift_during_calculation_returns_409_before_batch_insertion(self):
        import routes.safe_prices as safe_prices
        from models import PriceChangeBatch, Product

        calculate = safe_prices.calculate_price_changes

        def calculate_then_deactivate(*args, **kwargs):
            result = calculate(*args, **kwargs)
            product = self.db.session.get(Product, self.second.id)
            product.is_active = False
            self.db.session.flush()
            return result

        with patch(
            'routes.safe_prices.calculate_price_changes',
            side_effect=calculate_then_deactivate,
        ):
            response = self._request_as_owner(
                'post', '/prices/create-batch',
                json={
                    'exact_selection': True,
                    'product_ids': [self.second.id],
                    'change_type': 'set',
                    'change_value': 2100,
                    'name': 'Drifted during calculation',
                    'description': '',
                    'price_source': 'local',
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(PriceChangeBatch.query.filter_by(seller_id=self.seller.id).count(), 0)


if __name__ == '__main__':
    unittest.main()
