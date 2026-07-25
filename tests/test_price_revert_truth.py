# -*- coding: utf-8 -*-
"""Откат цен не имеет права врать о том, что реально откачено.

Прод-инцидент (батч #105): WB подтвердил откат 706 позиций из 8706, а локальные
цены были переписаны у всех 8706 — продавец видел цену, которой на площадке нет.
Правило: локальная цена меняется только для позиций, подтверждённых площадкой.
"""

import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch


class PriceRevertTruthTest(unittest.TestCase):
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

        user = User(username='price-owner', email='price@test.local', is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Цены')
        seller.wb_api_key = 'synthetic-key'
        self.db.session.add(seller)
        self.db.session.flush()

        # Два товара: у обоих цена уже изменена на новую
        self.products = []
        for index in range(2):
            product = Product(
                seller_id=seller.id,
                nm_id=990001 + index,
                title=f'Товар {index}',
                is_active=True,
                price=Decimal('1500'),
            )
            self.db.session.add(product)
            self.products.append(product)
        self.db.session.flush()

        batch = PriceChangeBatch(
            seller_id=seller.id,
            name='Исходный батч',
            change_type='percent',
            change_value=50,
            status='applied',
            total_items=2,
            applied_count=2,
        )
        self.db.session.add(batch)
        self.db.session.flush()

        for product in self.products:
            item = PriceChangeItem(
                batch_id=batch.id,
                product_id=product.id,
                nm_id=product.nm_id,
                product_title=product.title,
                old_price=Decimal('1000'),
                new_price=Decimal('1500'),
                status='applied',
            )
            self.db.session.add(item)
        self.db.session.commit()

        self.seller_id = seller.id
        self.batch_id = batch.id
        self.user = SimpleNamespace(
            id=user.id,
            username=user.username,
            seller=seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _revert_with_partial_wb_result(self, rejected_nm_id):
        """WB принимает откат одной позиции и отклоняет вторую."""
        fake_client = SimpleNamespace(
            upload_prices_batch=lambda *a, **kw: {
                'success': 1,
                'failed': 1,
                'errors': [{
                    'nm_ids': [rejected_nm_id],
                    'error': 'Цена вне допустимого диапазона',
                }],
            },
            close=lambda: None,
        )
        with patch('flask_login.utils._get_user', return_value=self.user), \
                patch('routes.safe_prices.get_current_seller', return_value=self.user.seller), \
                patch('routes.safe_prices.current_user', self.user), \
                patch('routes.safe_prices.WildberriesAPIClient', return_value=fake_client):
            response = self.client.post(f'/prices/batch/{self.batch_id}/revert')
            if response.status_code != 200:
                raise AssertionError(
                    f'revert вернул {response.status_code}: {response.get_data(as_text=True)[:300]}'
                )
            return response

    def test_rejected_position_keeps_its_marketplace_price(self):
        from models import PriceChangeBatch, Product

        rejected = self.products[1]
        rejected_nm_id = rejected.nm_id
        accepted_id = self.products[0].id
        rejected_id = rejected.id

        response = self._revert_with_partial_wb_result(rejected_nm_id)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['reverted'], 1)
        self.assertEqual(payload['rejected'], 1)

        accepted_product = self.db.session.get(Product, accepted_id)
        rejected_product = self.db.session.get(Product, rejected_id)
        # Подтверждённая позиция вернулась к старой цене
        self.assertEqual(Decimal(accepted_product.price), Decimal('1000'))
        # Отклонённая осталась с той ценой, которая реально стоит на площадке
        self.assertEqual(Decimal(rejected_product.price), Decimal('1500'))

        original = self.db.session.get(PriceChangeBatch, self.batch_id)
        # Батч не считается полностью откаченным, пока часть позиций не вернулась
        self.assertFalse(original.reverted)
        revert_batch = self.db.session.get(
            PriceChangeBatch, original.revert_batch_id
        )
        self.assertEqual(revert_batch.status, 'partially_applied')

    def test_revert_items_get_their_own_status(self):
        from models import PriceChangeBatch, PriceChangeItem

        self._revert_with_partial_wb_result(self.products[1].nm_id)
        original = self.db.session.get(PriceChangeBatch, self.batch_id)
        items = PriceChangeItem.query.filter_by(
            batch_id=original.revert_batch_id
        ).all()
        statuses = sorted(item.status for item in items)
        # Раньше позиции отката навсегда оставались в 'pending'
        self.assertEqual(statuses, ['applied', 'failed'])
        failed = [item for item in items if item.status == 'failed'][0]
        self.assertIn('диапазон', failed.error_message.lower())


if __name__ == '__main__':
    unittest.main()


class PriceRetryFailedTest(PriceRevertTruthTest):
    """Упавшие позиции можно доотправить, не пересобирая весь запуск."""

    def _make_partially_applied_batch(self):
        """Запуск, где одна позиция применилась, а вторая упала."""
        from models import PriceChangeBatch, PriceChangeItem
        from decimal import Decimal as D

        batch = PriceChangeBatch(
            seller_id=self.seller_id,
            name='Летняя переоценка',
            change_type='percent',
            change_value=20,
            status='partially_applied',
            total_items=2,
            applied_count=1,
            failed_count=1,
        )
        self.db.session.add(batch)
        self.db.session.flush()
        ok_item = PriceChangeItem(
            batch_id=batch.id,
            product_id=self.products[0].id,
            nm_id=self.products[0].nm_id,
            old_price=D('1000'),
            new_price=D('1200'),
            status='applied',
        )
        failed_item = PriceChangeItem(
            batch_id=batch.id,
            product_id=self.products[1].id,
            nm_id=self.products[1].nm_id,
            old_price=D('1000'),
            new_price=D('1300'),
            status='failed',
            error_message='API Error 400',
        )
        self.db.session.add_all([ok_item, failed_item])
        self.db.session.commit()
        return batch.id

    def _retry(self, batch_id):
        with patch('flask_login.utils._get_user', return_value=self.user), \
                patch('routes.safe_prices.get_current_seller', return_value=self.user.seller), \
                patch('routes.safe_prices.current_user', self.user):
            return self.client.post(f'/prices/batch/{batch_id}/retry-failed')

    def test_retry_takes_only_failed_positions(self):
        from models import PriceChangeBatch, PriceChangeItem
        from decimal import Decimal as D

        batch_id = self._make_partially_applied_batch()
        response = self._retry(batch_id)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['items'], 1)

        retry_batch = self.db.session.get(PriceChangeBatch, payload['batch_id'])
        self.assertEqual(retry_batch.status, 'draft')
        items = PriceChangeItem.query.filter_by(batch_id=retry_batch.id).all()
        self.assertEqual(len(items), 1)
        # Цель сохранена, а точка отсчёта взята из текущей цены товара
        self.assertEqual(D(items[0].new_price), D('1300'))
        self.assertEqual(D(items[0].old_price), D(self.products[1].price))

    def test_retry_without_failures_is_rejected(self):
        from models import PriceChangeBatch

        batch = self.db.session.get(PriceChangeBatch, self.batch_id)
        self.assertEqual(batch.status, 'applied')
        response = self._retry(self.batch_id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('ошибк', response.get_json()['error'].lower())
