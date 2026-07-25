# -*- coding: utf-8 -*-
"""Сигналы главной страницы: что требует внимания продавца.

Дашборд обязан показывать не «сколько всего карточек», а что мешает продавать
прямо сейчас — и только по своему продавцу.
"""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class DashboardAttentionTest(unittest.TestCase):
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
        from models import ImportedProduct, Product, Seller, User
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.session.remove()
        self.db.drop_all()
        self.db.create_all()
        self.client = self.app.test_client()

        user = User(username='dash-owner', email='dash@test.local', is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Главная')
        seller.wb_api_key = 'synthetic-key'
        other_user = User(username='dash-alien', email='alien2@test.local', is_active=True)
        other_user.set_password('synthetic-password')
        other = Seller(user=other_user, company_name='Чужой')
        self.db.session.add_all([seller, other])
        self.db.session.flush()

        # Товар со спросом, но без заказов — главный сигнал
        self.db.session.add(Product(
            seller_id=seller.id, nm_id=1, title='Смотрят', is_active=True,
            wb_views_30d=500, wb_orders_30d=0,
        ))
        # Чужой такой же товар не должен попасть в счётчик
        self.db.session.add(Product(
            seller_id=other.id, nm_id=2, title='Чужой', is_active=True,
            wb_views_30d=900, wb_orders_30d=0,
        ))
        # Карточка без фото
        self.db.session.add(ImportedProduct(
            seller_id=seller.id, external_id='no-photo', source_type='synthetic',
            title='Без фото', import_status='pending', photo_urls='[]',
        ))
        # Готова к публикации
        self.db.session.add(ImportedProduct(
            seller_id=seller.id, external_id='ready', source_type='synthetic',
            title='Готов', import_status='validated',
            photo_urls=json.dumps(['https://cdn.test/a.jpg']),
        ))
        self.db.session.commit()

        self.seller_id = seller.id
        self.user = SimpleNamespace(
            id=user.id, username=user.username, seller=seller,
            is_authenticated=True, is_active=True, is_admin=False,
        )

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _auth(self):
        return patch('flask_login.utils._get_user', return_value=self.user)

    def test_signals_are_actionable_and_tenant_scoped(self):
        with self._auth():
            response = self.client.get('/api/dashboard/attention')
        self.assertEqual(response.status_code, 200)
        signals = {s['key']: s for s in response.get_json()['signals']}

        self.assertEqual(signals['losing_sales']['count'], 1)  # чужой не попал
        self.assertEqual(signals['no_photos']['count'], 1)
        self.assertEqual(signals['ready_to_publish']['count'], 1)
        # У каждого сигнала есть куда пойти и что нажать
        for signal in signals.values():
            self.assertTrue(signal['href'])
            self.assertTrue(signal['action'])
            self.assertTrue(signal['text'])

    def test_dangerous_signals_come_first(self):
        with self._auth():
            response = self.client.get('/api/dashboard/attention')
        tones = [s['tone'] for s in response.get_json()['signals']]
        order = {'danger': 0, 'warn': 1, 'info': 2, 'ok': 3}
        self.assertEqual(tones, sorted(tones, key=lambda t: order[t]))

    def test_beta_dashboard_renders(self):
        with self._auth():
            response = self.client.get('/dashboard/beta')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('dashboard-app', body)
        self.assertIn('sh-vue-kit.js', body)


if __name__ == '__main__':
    unittest.main()
