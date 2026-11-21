"""Exact seller-owned product handoff into the legacy WB price workflow."""

import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event


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
        self.assertIn(b'common-product-content.js?v=20261121-price-workspace-return-r39', exact.data)
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
        from models import PriceChangeBatch
        batch = self.db.session.get(PriceChangeBatch, payload['batch_id'])
        self.assertEqual([item.product_id for item in batch.items], [self.second.id])

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
