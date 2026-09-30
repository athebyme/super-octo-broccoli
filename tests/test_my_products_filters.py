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
        cls.app.config['MARKETPLACE_OZON_ENABLED'] = True
        cls.app.config['MARKETPLACE_OZON_PUBLICATION_ENABLED'] = True
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
        from models import (
            ImportedProduct,
            Marketplace,
            MarketplaceListing,
            Seller,
            SellerMarketplaceAccount,
            Supplier,
            User,
        )
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

        linked_product = ImportedProduct(
                seller_id=seller.id, supplier_id=sup_a.id, title='PROD-ALPHA-STOCK',
                brand='BrandX', photo_urls='["http://x/1.jpg"]',
                supplier_quantity=5, supplier_price=100.0,
                mapped_wb_category='Игрушки', import_status='pending',
            )
        cls.db.session.add_all([
            linked_product,
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
        ozon = Marketplace(
            name='Ozon',
            code='ozon',
            adapter_code='ozon',
            is_active=True,
        )
        cls.db.session.add(ozon)
        cls.db.session.flush()
        account = SellerMarketplaceAccount(
            seller_id=seller.id,
            marketplace_id=ozon.id,
            external_account_id='my-products-ozon',
            label='Основной Ozon',
            is_active=True,
            is_default=True,
            connection_status='connected',
            _credentials_encrypted='synthetic-encrypted',
            settings_json='{"default_vat":"0.22"}',
        )
        cls.db.session.add(account)
        cls.db.session.flush()
        cls.db.session.add(MarketplaceListing(
            seller_id=seller.id,
            marketplace_id=ozon.id,
            account_id=account.id,
            imported_product_id=linked_product.id,
            offer_id='id-7725-1364',
            external_product_id='7725001',
            normalized_status='active',
            link_status='linked',
            link_source='exact_source_identity',
            sync_fingerprint='a' * 64,
        ))
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

    def test_linked_ozon_listing_is_visible_and_uses_update_action(self):
        html = self._get()
        self.assertIn('На Ozon', html)
        self.assertIn('Подготовить Ozon', html)
        self.assertIn('Отправка в Ozon подтверждается отдельно после проверки.', html)
        self.assertIn('action="/marketplaces/ozon/uploads/"', html)

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

    def test_explicit_ozon_account_keeps_published_wb_sources_and_never_switches_target(self):
        from models import ImportedProduct, SellerMarketplaceAccount
        with self.app.app_context():
            original = SellerMarketplaceAccount.query.filter_by(external_account_id='my-products-ozon').one()
            account = SellerMarketplaceAccount(seller_id=original.seller_id, marketplace_id=original.marketplace_id,
                external_account_id='my-products-second', label='Выбранный магазин B', is_active=True,
                connection_status='connected', _credentials_encrypted='synthetic-second-key', settings_json='{}')
            source = ImportedProduct(seller_id=original.seller_id, title='PUBLISHED-WB-FOR-OZON', import_status='imported')
            self.db.session.add_all([account, source])
            self.db.session.commit()
            account_id, source_id = account.id, source.id
        try:
            html = self._get('?account_id=' + str(account_id))
            source_position = html.index('PUBLISHED-WB-FOR-OZON')
            source_row = html[html.rfind('<tr', 0, source_position):html.find('</tr>', source_position)]
            self.assertIn('Выбранный магазин B', html)
            self.assertIn('account_id=' + str(account_id), html)
            self.assertIn('Подготовить Ozon', source_row)
            self.assertIn('action="/marketplaces/ozon/uploads/"', source_row)
            self.assertIn('name="account_id" value="' + str(account_id) + '"', source_row)
            self.assertIn('name="imported_product_ids" value="' + str(source_id) + '"', source_row)
            self.assertIn('name="confirm_prepare" value="1"', source_row)
            self.assertIn('name="request_key"', source_row)
        finally:
            with self.app.app_context():
                self.db.session.delete(self.db.session.get(ImportedProduct, source_id))
                self.db.session.delete(self.db.session.get(SellerMarketplaceAccount, account_id))
                self.db.session.commit()


if __name__ == '__main__':
    unittest.main()
