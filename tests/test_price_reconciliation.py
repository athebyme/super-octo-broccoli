# -*- coding: utf-8 -*-
"""Отправка цены и её применение — разные события.

`POST /api/v2/upload/task` лишь ставит пачку в очередь Wildberries. Раньше
платформа считала это применением: писала новую цену в Product.price и
показывала её продавцу как факт. Правило теперь: локальная цена меняется
только после того, как та же цена наблюдается на площадке.
"""

import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch


class _PriceTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        os.environ.setdefault('SKIP_SCHEDULER', '1')
        import sqlalchemy as _sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa
        from models import db
        cls.app = seller_platform.app
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SQLALCHEMY_ENGINE_OPTIONS={},
            WTF_CSRF_ENABLED=False,
            TESTING=True,
        )
        cls._engine = _sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls._engine}
        cls.db = db

    def setUp(self):
        from models import (
            PriceChangeBatch, PriceChangeItem, Product, Seller, User,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.session.remove()
        self.db.drop_all()
        self.db.create_all()
        self.client = self.app.test_client()

        user = User(username='rec-owner', email='rec@test.local', is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Сверка')
        seller.wb_api_key = 'synthetic-key'
        self.db.session.add(seller)
        self.db.session.flush()

        self.product = Product(
            seller_id=seller.id,
            nm_id=770101,
            title='Свеча',
            is_active=True,
            price=Decimal('1000'),
        )
        self.db.session.add(self.product)
        self.db.session.flush()

        batch = PriceChangeBatch(
            seller_id=seller.id,
            name='Переоценка',
            change_type='percent',
            change_value=20,
            status='submitted',
            total_items=1,
            applied_at=datetime.utcnow(),
        )
        self.db.session.add(batch)
        self.db.session.flush()

        item = PriceChangeItem(
            batch_id=batch.id,
            product_id=self.product.id,
            nm_id=self.product.nm_id,
            old_price=Decimal('1000'),
            new_price=Decimal('1200'),
            status='submitted',
            wb_status='queued',
        )
        self.db.session.add(item)
        self.db.session.commit()

        self.seller = seller
        self.batch_id = batch.id
        self.item_id = item.id
        self.product_id = self.product.id

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    @staticmethod
    def _client_with_price(price):
        """Клиент WB, отвечающий заданной фактической ценой товара."""
        def get_goods_prices(**kwargs):
            if price is None:
                return {'data': {'listGoods': []}}
            return {'data': {'listGoods': [{
                'nmID': kwargs.get('filter_nm_id'),
                'sizes': [{'sizeID': 0, 'price': price}],
            }]}}
        return SimpleNamespace(get_goods_prices=get_goods_prices, close=lambda: None)


class PriceReconciliationTest(_PriceTestBase):
    def _reconcile(self, actual_price, now=None):
        from models import PriceChangeBatch
        from services.price_reconciliation import PriceReconciliationService

        batch = self.db.session.get(PriceChangeBatch, self.batch_id)
        return PriceReconciliationService.reconcile_batch(
            batch=batch,
            api_client=self._client_with_price(actual_price),
            now=now,
        )

    def test_confirmed_price_updates_local_card(self):
        from models import PriceChangeBatch, PriceChangeItem, Product

        outcome = self._reconcile(1200)
        self.assertEqual(outcome.confirmed, 1)
        item = self.db.session.get(PriceChangeItem, self.item_id)
        self.assertEqual(item.status, 'applied')
        self.assertEqual(item.wb_status, 'confirmed')
        product = self.db.session.get(Product, self.product_id)
        self.assertEqual(Decimal(product.price), Decimal('1200'))
        batch = self.db.session.get(PriceChangeBatch, self.batch_id)
        self.assertEqual(batch.status, 'applied')

    def test_price_not_yet_applied_keeps_waiting_and_does_not_touch_card(self):
        from models import PriceChangeItem, Product

        outcome = self._reconcile(1000)  # на площадке всё ещё старая цена
        self.assertEqual(outcome.still_waiting, 1)
        self.assertEqual(outcome.confirmed, 0)
        item = self.db.session.get(PriceChangeItem, self.item_id)
        self.assertEqual(item.status, 'submitted')
        product = self.db.session.get(Product, self.product_id)
        # Локальная цена не выдумывается, пока площадка не подтвердила
        self.assertEqual(Decimal(product.price), Decimal('1000'))

    def test_after_deadline_unapplied_price_becomes_failed(self):
        from models import PriceChangeItem, Product

        later = datetime.utcnow() + timedelta(hours=25)
        outcome = self._reconcile(1000, now=later)
        self.assertEqual(outcome.rejected, 1)
        item = self.db.session.get(PriceChangeItem, self.item_id)
        self.assertEqual(item.status, 'failed')
        self.assertIn('другая цена', item.error_message.lower())
        product = self.db.session.get(Product, self.product_id)
        self.assertEqual(Decimal(product.price), Decimal('1000'))

    def test_missing_marketplace_answer_is_not_treated_as_success(self):
        from models import PriceChangeItem

        outcome = self._reconcile(None)
        self.assertEqual(outcome.confirmed, 0)
        self.assertEqual(outcome.still_waiting, 1)
        item = self.db.session.get(PriceChangeItem, self.item_id)
        self.assertEqual(item.status, 'submitted')

    def test_rounding_tolerance_confirms_close_price(self):
        # WB округляет до рубля — расхождение в рубль не считается отказом
        outcome = self._reconcile(1201)
        self.assertEqual(outcome.confirmed, 1)


class PriceReconciliationRouteTest(_PriceTestBase):
    def setUp(self):
        super().setUp()
        self.user = SimpleNamespace(
            id=self.seller.user_id,
            username='rec-owner',
            seller=self.seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def test_manual_reconcile_reports_result(self):
        with patch('flask_login.utils._get_user', return_value=self.user), \
                patch('routes.safe_prices.get_current_seller', return_value=self.seller), \
                patch('routes.safe_prices.current_user', self.user), \
                patch(
                    'routes.safe_prices.WildberriesAPIClient',
                    return_value=self._client_with_price(1200),
                ):
            response = self.client.post(
                f'/prices/batch/{self.batch_id}/reconcile'
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['confirmed'], 1)
        self.assertEqual(payload['status'], 'applied')
        self.assertIn('подтверждено', payload['message'].lower())


if __name__ == '__main__':
    unittest.main()
