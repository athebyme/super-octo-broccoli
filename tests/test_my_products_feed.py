# -*- coding: utf-8 -*-
"""Лента «Моих товаров»: контекст для решения и tenant scope.

Экран массовых операций должен давать ответ «стоит ли выбирать эту строку»:
качество, спрос и фактические цены канала. И не должен показывать чужое.
"""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class MyProductsFeedTest(unittest.TestCase):
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
            ImportedProduct, Product, Seller, Supplier, SupplierProduct, User,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        # Движок общий на класс (StaticPool), поэтому чистим схему явно перед
        # каждым тестом: иначе строки предыдущего случая ломают уникальность.
        self.db.session.remove()
        self.db.drop_all()
        self.db.create_all()
        self.client = self.app.test_client()

        user = User(username='feed-owner', email='feed@test.local', is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Лента')
        other_user = User(username='feed-alien', email='alien@test.local', is_active=True)
        other_user.set_password('synthetic-password')
        other_seller = Seller(user=other_user, company_name='Чужой')
        supplier = Supplier(name='Опт', code='opt')
        self.db.session.add_all([seller, other_seller, supplier])
        self.db.session.flush()

        wb_product = Product(
            seller_id=seller.id,
            nm_id=880001,
            title='Свеча',
            is_active=True,
            quality_score=41,
            quality_impact=52.0,
            attention_reasons='few_photos,weak_chars',
            wb_views_30d=310,
            wb_orders_30d=2,
            wb_price=1500,
            wb_discounted_price=1290,
            quantity=6,
            photos_json=json.dumps([1, 2]),
        )
        self.db.session.add(wb_product)
        self.db.session.flush()

        source = SupplierProduct(
            supplier_id=supplier.id,
            external_id='sp-1',
            title='Свеча',
            content_revision=5,
        )
        self.db.session.add(source)
        self.db.session.flush()

        published = ImportedProduct(
            seller_id=seller.id,
            supplier_id=supplier.id,
            supplier_product_id=source.id,
            supplier_content_revision=3,
            product_id=wb_product.id,
            external_id='imp-1',
            external_vendor_code='id-7725-1',
            source_type='synthetic',
            title='Свеча ароматическая',
            import_status='imported',
            supplier_price=700,
            supplier_quantity=12,
            photo_urls=json.dumps(['https://cdn.test/a.jpg']),
        )
        draft_row = ImportedProduct(
            seller_id=seller.id,
            external_id='imp-2',
            source_type='synthetic',
            title='Плед',
            import_status='validated',
        )
        foreign = ImportedProduct(
            seller_id=other_seller.id,
            external_id='imp-alien',
            source_type='synthetic',
            title='Чужой товар',
            import_status='validated',
        )
        self.db.session.add_all([published, draft_row, foreign])
        self.db.session.commit()

        self.seller_id = seller.id
        self.published_id = published.id
        self.draft_id = draft_row.id
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

    def test_feed_carries_quality_demand_and_channel_facts(self):
        with self._auth():
            response = self.client.get(
                '/api/my-products',
                headers={'Accept': 'application/json'},
            )
        self.assertEqual(response.status_code, 200)
        items = {item['id']: item for item in response.get_json()['items']}
        published = items[self.published_id]
        self.assertEqual(published['wb']['quality_score'], 41)
        self.assertIn('few_photos', published['wb']['attention_reasons'])
        self.assertEqual(published['wb']['views_30d'], 310)
        self.assertEqual(published['wb']['orders_30d'], 2)
        self.assertEqual(published['wb']['price'], 1290.0)
        self.assertEqual(published['supplier_price'], 700.0)
        self.assertTrue(published['has_supplier_update'])
        # Непубликованная строка не выдумывает канальных фактов
        self.assertIsNone(items[self.draft_id]['wb'])

    def test_feed_is_tenant_scoped(self):
        with self._auth():
            response = self.client.get(
                '/api/my-products',
                headers={'Accept': 'application/json'},
            )
        titles = [item['title'] for item in response.get_json()['items']]
        self.assertNotIn('Чужой товар', titles)

    def test_search_matches_cyrillic_in_any_case(self):
        with self._auth():
            lower = self.client.get('/api/my-products?search=свеча')
            upper = self.client.get('/api/my-products?search=СВЕЧА')
        self.assertEqual(len(lower.get_json()['items']), 1)
        self.assertEqual(len(upper.get_json()['items']), 1)

    def test_facets_count_statuses_and_supplier_updates(self):
        with self._auth():
            response = self.client.get('/api/my-products/facets')
        statuses = response.get_json()['statuses']
        self.assertEqual(statuses['all'], 2)
        self.assertEqual(statuses['imported'], 1)
        self.assertEqual(statuses['validated'], 1)
        self.assertEqual(statuses['supplier_updates'], 1)

    def test_per_page_is_bounded(self):
        with self._auth():
            response = self.client.get('/api/my-products?per_page=500')
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()


class MyProductsBetaPageTest(MyProductsFeedTest):
    """Страница массовых операций рендерится и знает про каналы продавца."""

    def test_beta_page_renders_with_bootstrap(self):
        with self._auth():
            response = self.client.get('/my-products/beta')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('my-products-app', body)
        self.assertIn('sh-vue-kit.js', body)
        self.assertIn('mp-bootstrap', body)

    def test_classic_page_links_to_beta(self):
        with self._auth():
            response = self.client.get('/my-products')
        self.assertEqual(response.status_code, 200)
        self.assertIn('/my-products/beta', response.get_data(as_text=True))
