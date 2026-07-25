# -*- coding: utf-8 -*-
"""Страница «Что чинить в карточках» (beta) и коммерческий контекст в API.

Очередь качества бесполезна без ответа на вопрос «стоит ли вкладываться»:
список обязан отдавать просмотры, заказы, цену и остаток, а страница —
рендериться и без подключённого WB (с честным объяснением вместо пустоты).
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class CardQualityBetaPageTest(unittest.TestCase):
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
        from models import Product, Seller, User
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.create_all()
        self.client = self.app.test_client()

        user = User(username='cq-owner', email='cq@test.local', is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Качество')
        seller.wb_api_key = 'synthetic-key'
        self.db.session.add(seller)
        self.db.session.flush()

        product = Product(
            seller_id=seller.id,
            nm_id=770001,
            vendor_code='cq-1',
            title='Свеча ароматическая',
            is_active=True,
            quality_score=34,
            quality_impact=48.2,
            attention_reasons='few_photos,weak_chars',
            price=1290,
            quantity=7,
            wb_views_30d=424,
            wb_orders_30d=0,
            wb_order_conv=0.0,
        )
        self.db.session.add(product)
        self.db.session.commit()

        self.seller = seller
        self.product_id = product.id
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

    def _auth(self):
        return patch('flask_login.utils._get_user', return_value=self.user)

    def test_beta_page_renders_for_connected_seller(self):
        with self._auth():
            response = self.client.get('/card-quality/beta')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('card-quality-app', body)
        self.assertIn('sh-vue-kit.js', body)

    def test_list_api_exposes_demand_and_commercial_context(self):
        with self._auth():
            response = self.client.get(
                '/api/card-quality/list',
                headers={'Accept': 'application/json'},
            )
        self.assertEqual(response.status_code, 200)
        item = response.get_json()['items'][0]
        self.assertEqual(item['views_30d'], 424)
        self.assertEqual(item['orders_30d'], 0)
        self.assertEqual(item['quantity'], 7)
        self.assertEqual(item['price'], 1290.0)
        self.assertIn('few_photos', item['attention_reasons'])

    def test_beta_page_without_wb_key_still_renders_explanation(self):
        self.seller.wb_api_key = None
        self.db.session.commit()
        with self._auth():
            response = self.client.get('/card-quality/beta')
        self.assertEqual(response.status_code, 200)
        self.assertIn('card-quality-app', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
