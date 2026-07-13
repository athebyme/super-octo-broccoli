# -*- coding: utf-8 -*-
"""Тесты фильтров списка импортированных товаров (/my-products)."""

import os
import unittest


class TestMyProductsFilters(unittest.TestCase):
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
        # StaticPool ensures all sessions/connections (including request contexts)
        # share one in-memory DB. Don't keep app context alive between requests —
        # that would cause g._login_user to bleed across requests.
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
        from models import User, Seller, Supplier, ImportedProduct
        user = User(username='seller1', email='seller1@example.com', password_hash='x')
        cls.db.session.add(user)
        cls.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Тест', wb_seller_id='123')
        seller.wb_api_key = 'test-api-key'
        cls.db.session.add(seller)
        cls.db.session.flush()
        cls.user_id = user.id

        other_user = User(username='seller2', email='seller2@example.com', password_hash='x')
        cls.db.session.add(other_user)
        cls.db.session.flush()
        other_seller = Seller(user_id=other_user.id, company_name='ООО Чужой', wb_seller_id='456')
        cls.db.session.add(other_seller)
        cls.db.session.flush()

        sup_a = Supplier(name='Alpha Supplier', code='alpha')
        sup_b = Supplier(name='Beta Supplier', code='beta')
        cls.db.session.add_all([sup_a, sup_b])
        cls.db.session.flush()
        cls.sup_a_id = sup_a.id
        cls.sup_b_id = sup_b.id

        cls.db.session.add_all([
            ImportedProduct(
                seller_id=seller.id, supplier_id=sup_a.id, title='PROD-ALPHA-STOCK',
                brand='BrandX', photo_urls='["http://x/1.jpg"]',
                supplier_quantity=5, supplier_price=100.0,
                mapped_wb_category='Игрушки', import_status='pending',
            ),
            ImportedProduct(
                seller_id=seller.id, supplier_id=sup_b.id, title='PROD-BETA-NOPHOTO',
                brand='BrandY', photo_urls='[]',
                supplier_quantity=0, supplier_price=500.0,
                import_status='pending',
            ),
            ImportedProduct(
                seller_id=seller.id, supplier_id=None, title='PROD-NOSUP',
                brand=None, photo_urls=None,
                supplier_quantity=None, supplier_price=None,
                import_status='pending',
            ),
            ImportedProduct(
                seller_id=other_seller.id, supplier_id=sup_a.id, title='PROD-OTHER-SELLER',
                brand='BrandX', import_status='pending',
            ),
        ])
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.drop_all()
        cls._engine.dispose()

    def _client(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)
            sess['_fresh'] = True
        return client

    def _get(self, qs=''):
        resp = self._client().get('/my-products' + qs)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_no_filters_shows_all_own_products(self):
        html = self._get()
        self.assertIn('PROD-ALPHA-STOCK', html)
        self.assertIn('PROD-BETA-NOPHOTO', html)
        self.assertIn('PROD-NOSUP', html)
        self.assertNotIn('PROD-OTHER-SELLER', html)

    def test_supplier_filter(self):
        html = self._get(f'?supplier={self.sup_a_id}')
        self.assertIn('PROD-ALPHA-STOCK', html)
        self.assertNotIn('PROD-BETA-NOPHOTO', html)
        self.assertNotIn('PROD-NOSUP', html)

    def test_supplier_none_filter(self):
        html = self._get('?supplier=none')
        self.assertIn('PROD-NOSUP', html)
        self.assertNotIn('PROD-ALPHA-STOCK', html)
        self.assertNotIn('PROD-BETA-NOPHOTO', html)

    def test_supplier_filter_is_tenant_scoped(self):
        html = self._get(f'?supplier={self.sup_a_id}')
        self.assertNotIn('PROD-OTHER-SELLER', html)

    def test_brand_filter(self):
        html = self._get('?brand=BrandX')
        self.assertIn('PROD-ALPHA-STOCK', html)
        self.assertNotIn('PROD-BETA-NOPHOTO', html)
        self.assertNotIn('PROD-NOSUP', html)

    def test_brand_none_filter(self):
        html = self._get('?brand=none')
        self.assertIn('PROD-NOSUP', html)
        self.assertNotIn('PROD-ALPHA-STOCK', html)

    def test_no_photos_filter(self):
        html = self._get('?has_photos=no')
        self.assertIn('PROD-BETA-NOPHOTO', html)
        self.assertIn('PROD-NOSUP', html)
        self.assertNotIn('PROD-ALPHA-STOCK', html)

    def test_has_photos_filter(self):
        html = self._get('?has_photos=yes')
        self.assertIn('PROD-ALPHA-STOCK', html)
        self.assertNotIn('PROD-BETA-NOPHOTO', html)
        self.assertNotIn('PROD-NOSUP', html)

    def test_stock_filter(self):
        html = self._get('?stock=in_stock')
        self.assertIn('PROD-ALPHA-STOCK', html)
        self.assertNotIn('PROD-BETA-NOPHOTO', html)
        self.assertNotIn('PROD-NOSUP', html)

    def test_price_range_filter(self):
        html = self._get('?price_min=200')
        self.assertIn('PROD-BETA-NOPHOTO', html)
        self.assertNotIn('PROD-ALPHA-STOCK', html)
        self.assertNotIn('PROD-NOSUP', html)

    def test_wb_category_filter(self):
        html = self._get('?wb_category=Игрушки')
        self.assertIn('PROD-ALPHA-STOCK', html)
        self.assertNotIn('PROD-BETA-NOPHOTO', html)

    def test_invalid_price_is_ignored(self):
        html = self._get('?price_min=abc&price_max=xyz')
        self.assertIn('PROD-ALPHA-STOCK', html)
        self.assertIn('PROD-BETA-NOPHOTO', html)
        self.assertIn('PROD-NOSUP', html)


if __name__ == '__main__':
    unittest.main()
