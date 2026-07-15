# -*- coding: utf-8 -*-
"""Тесты JSON-эндпоинта поиска товаров для командной палитры (/api/products/search).

Проверяют: успешный поиск, tenant-изоляцию (чужие товары не видны),
короткий запрос (empty), поиск по nmID.
"""

import os
import unittest


class TestProductsSearchApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import sqlalchemy as _sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa
        from models import db
        cls.app = seller_platform.app
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        cls.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        cls.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['TESTING'] = True
        cls._engine = _sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls._engine}
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            cls._seed()

    @classmethod
    def _seed(cls):
        from models import User, Seller, Product
        # Продавец 1
        u1 = User(username='seller1', email='s1@example.com', password_hash='x')
        cls.db.session.add(u1); cls.db.session.flush()
        s1 = Seller(user_id=u1.id, company_name='ООО Один', wb_seller_id='111')
        s1.wb_api_key = 'k1'
        cls.db.session.add(s1); cls.db.session.flush()
        # Продавец 2
        u2 = User(username='seller2', email='s2@example.com', password_hash='x')
        cls.db.session.add(u2); cls.db.session.flush()
        s2 = Seller(user_id=u2.id, company_name='ООО Два', wb_seller_id='222')
        s2.wb_api_key = 'k2'
        cls.db.session.add(s2); cls.db.session.flush()
        cls.user1_id = u1.id
        cls.user2_id = u2.id
        cls.db.session.add_all([
            Product(seller_id=s1.id, nm_id=1001, vendor_code='ALPHA-1', title='Куртка зимняя', brand='Марка', is_active=True),
            Product(seller_id=s1.id, nm_id=1002, vendor_code='ALPHA-2', title='Штаны', brand='Марка', is_active=True),
            # У продавца 2 — товар с тем же словом «Куртка», не должен видеться продавцу 1
            Product(seller_id=s2.id, nm_id=2001, vendor_code='BETA-1', title='Куртка чужая', brand='Другая', is_active=True),
        ])
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.drop_all()
        cls._engine.dispose()

    def _client(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True
        return client

    def test_search_returns_matching(self):
        client = self._client(self.user1_id)
        resp = client.get('/api/products/search?q=ALPHA')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        codes = {i['vendor_code'] for i in data['items']}
        self.assertEqual(codes, {'ALPHA-1', 'ALPHA-2'})
        # url ведёт на карточку
        self.assertTrue(all(i['url'].startswith('/products/') for i in data['items']))

    def test_tenant_isolation(self):
        client = self._client(self.user1_id)
        resp = client.get('/api/products/search?q=Куртка')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        codes = {i['vendor_code'] for i in data['items']}
        # Своя «Куртка зимняя» видна, чужая «Куртка чужая» — нет
        self.assertIn('ALPHA-1', codes)
        self.assertNotIn('BETA-1', codes)

    def test_search_by_nm_id(self):
        client = self._client(self.user1_id)
        resp = client.get('/api/products/search?q=1002')
        self.assertEqual(resp.status_code, 200)
        codes = {i['vendor_code'] for i in resp.get_json()['items']}
        self.assertEqual(codes, {'ALPHA-2'})

    def test_short_query_returns_empty(self):
        client = self._client(self.user1_id)
        resp = client.get('/api/products/search?q=a')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['items'], [])

    def test_seller2_sees_only_own(self):
        client = self._client(self.user2_id)
        resp = client.get('/api/products/search?q=Куртка')
        codes = {i['vendor_code'] for i in resp.get_json()['items']}
        self.assertEqual(codes, {'BETA-1'})


if __name__ == '__main__':
    unittest.main()
