# -*- coding: utf-8 -*-
"""Тесты поиска, catalog-фильтров и сортировки card-quality API."""

import os
import unittest


class TestCardQualityListFilters(unittest.TestCase):
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
        cls.app.config['SECRET_KEY'] = 'test-secret-key-for-unit-tests'
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
        from models import User, Seller, Product, ImportedProduct, Supplier

        # Own seller — needs a valid WB API key for api_card_quality_list to pass
        # the has_valid_api_key() guard.
        user = User(username='seller1', email='seller1@example.com', password_hash='x')
        cls.db.session.add(user)
        cls.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Тест', wb_seller_id='123')
        seller.wb_api_key = 'test-api-key'
        cls.db.session.add(seller)
        cls.db.session.flush()
        cls.user_id = user.id
        cls.seller_id = seller.id

        # Other seller — must never leak into own seller's list/candidates
        other_user = User(username='seller2', email='seller2@example.com', password_hash='x')
        cls.db.session.add(other_user)
        cls.db.session.flush()
        other_seller = Seller(user_id=other_user.id, company_name='ООО Чужой', wb_seller_id='456')
        other_seller.wb_api_key = 'other-api-key'
        cls.db.session.add(other_seller)
        cls.db.session.flush()

        supplier_alpha = Supplier(name='Alpha Supplier', code='cq-alpha')
        supplier_beta = Supplier(name='Beta Supplier', code='cq-beta')
        supplier_foreign = Supplier(name='Foreign Supplier', code='cq-foreign')
        cls.db.session.add_all([supplier_alpha, supplier_beta, supplier_foreign])
        cls.db.session.flush()
        cls.supplier_alpha_id = supplier_alpha.id
        cls.supplier_beta_id = supplier_beta.id
        cls.supplier_foreign_id = supplier_foreign.id

        # 3 own products, impacts 5 / 20 / 40, different reasons, different buckets
        product_low = Product(
            seller_id=seller.id, nm_id=101, vendor_code='LOW', title='Льняная рубашка',
            brand='Nord', object_name='Рубашки', is_active=True,
            quality_score=80, nm_rating=9.0,
            photos_json='["https://cdn.example.com/photo1.jpg"]',
            attention_reasons='low_rating', quality_impact=5,
        )
        product_mid = Product(
            seller_id=seller.id, nm_id=102, vendor_code='MID', title='Брюки прямые',
            brand='Line', object_name='Брюки', is_active=True,
            quality_score=60, nm_rating=7.0,
            attention_reasons='weak_chars', quality_impact=20,
        )
        product_high = Product(
            seller_id=seller.id, nm_id=103, vendor_code='HIGH', title='Кардиган тёплый',
            brand='Nord', object_name='Кардиганы', is_active=True,
            quality_score=30, nm_rating=5.0,
            photos_json='[1, 2, 3]',
            attention_reasons='few_photos,weak_chars', quality_impact=40,
        )
        foreign_product = Product(
            seller_id=other_seller.id, nm_id=999, vendor_code='OTHER',
            title='Кардиган чужой', brand='Foreign', object_name='Чужая категория',
            is_active=True, quality_score=10, nm_rating=3.0,
            attention_reasons='few_photos', quality_impact=99,
        )
        cls.db.session.add_all([
            product_low, product_mid, product_high, foreign_product,
        ])
        cls.db.session.flush()
        cls.db.session.add_all([
            ImportedProduct(
                seller_id=seller.id, product_id=product_low.id,
                supplier_id=supplier_alpha.id, import_status='imported',
            ),
            ImportedProduct(
                seller_id=seller.id, product_id=product_mid.id,
                supplier_id=supplier_beta.id, import_status='imported',
            ),
            ImportedProduct(
                seller_id=other_seller.id, product_id=foreign_product.id,
                supplier_id=supplier_foreign.id, import_status='imported',
            ),
        ])
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.drop_all()
        cls._engine.dispose()

    def setUp(self):
        # Общий процессный TTL-кэш сводки не должен переносить данные между тестами
        from services.ttl_cache import cache
        cache.invalidate('cq-summary')
        cache.invalidate('cq-filters')

    def _client_logged_in(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)
            sess['_fresh'] = True
        return client

    def _nm_ids(self, payload):
        return {item['nm_id'] for item in payload['items']}

    def test_reason_filter_only_matching_and_tenant_scoped(self):
        client = self._client_logged_in()
        resp = client.get('/api/card-quality/list?reason=few_photos')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        nm_ids = self._nm_ids(payload)
        # Only own product with 'few_photos' in attention_reasons
        self.assertEqual(nm_ids, {103})
        # Other seller's card (nm_id=999, also has few_photos) must never leak
        self.assertNotIn(999, nm_ids)

    def test_bucket_poor_filters_quality_score_below_50(self):
        client = self._client_logged_in()
        resp = client.get('/api/card-quality/list?bucket=poor')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        nm_ids = self._nm_ids(payload)
        # Only nm_id=103 has quality_score < 50 (30)
        self.assertEqual(nm_ids, {103})

    def test_search_matches_title_vendor_code_and_nm_id(self):
        client = self._client_logged_in()

        by_title = client.get('/api/card-quality/list?search=Кардиган').get_json()
        self.assertEqual(self._nm_ids(by_title), {103})

        by_vendor_code = client.get('/api/card-quality/list?search=mid').get_json()
        self.assertEqual(self._nm_ids(by_vendor_code), {102})

        by_nm_id = client.get('/api/card-quality/list?search=101').get_json()
        self.assertEqual(self._nm_ids(by_nm_id), {101})

    def test_search_stays_tenant_scoped_and_combines_with_filters(self):
        client = self._client_logged_in()

        foreign = client.get('/api/card-quality/list?search=чужой').get_json()
        self.assertEqual(self._nm_ids(foreign), set())

        combined = client.get(
            '/api/card-quality/list?search=Кардиган&reason=weak_chars&bucket=poor'
        ).get_json()
        self.assertEqual(self._nm_ids(combined), {103})

    def test_category_brand_and_supplier_filters(self):
        client = self._client_logged_in()
        cases = [
            ({'category': 'Кардиганы'}, {103}),
            ({'brand': 'Nord'}, {101, 103}),
            ({'supplier_id': self.supplier_alpha_id}, {101}),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                payload = client.get(
                    '/api/card-quality/list', query_string=query,
                ).get_json()
                self.assertEqual(self._nm_ids(payload), expected)

    def test_catalog_filters_combine_and_foreign_supplier_returns_nothing(self):
        client = self._client_logged_in()
        combined = client.get('/api/card-quality/list', query_string={
            'category': 'Рубашки',
            'brand': 'Nord',
            'supplier_id': self.supplier_alpha_id,
            'bucket': 'good',
        }).get_json()
        self.assertEqual(self._nm_ids(combined), {101})

        foreign = client.get('/api/card-quality/list', query_string={
            'supplier_id': self.supplier_foreign_id,
        }).get_json()
        self.assertEqual(self._nm_ids(foreign), set())

    def test_filter_options_are_counted_and_tenant_scoped(self):
        client = self._client_logged_in()
        resp = client.get('/api/card-quality/filters')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']

        categories = {item['value']: item['count'] for item in data['categories']}
        self.assertEqual(categories, {'Брюки': 1, 'Кардиганы': 1, 'Рубашки': 1})

        brands = {item['value']: item['count'] for item in data['brands']}
        self.assertEqual(brands, {'Line': 1, 'Nord': 2})

        suppliers = {item['label']: item['count'] for item in data['suppliers']}
        self.assertEqual(suppliers, {'Alpha Supplier': 1, 'Beta Supplier': 1})
        self.assertNotIn('Foreign Supplier', suppliers)

    def test_page_renders_search_and_quality_controls(self):
        client = self._client_logged_in()
        resp = client.get('/card-quality')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="cq-search"', html)
        self.assertIn('id="cq-category-filter"', html)
        self.assertIn('id="cq-brand-filter"', html)
        self.assertIn('id="cq-supplier-filter"', html)
        self.assertIn('id="cq-quality-filter"', html)

    def test_sort_impact_default_orders_desc_by_quality_impact(self):
        client = self._client_logged_in()
        resp = client.get('/api/card-quality/list?sort=impact')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        items = payload['items']
        self.assertGreaterEqual(len(items), 3)
        # First item must be the max quality_impact among own seller's cards (40 -> nm_id 103)
        self.assertEqual(items[0]['nm_id'], 103)

    def test_unknown_reason_is_ignored_not_500(self):
        client = self._client_logged_in()
        resp = client.get('/api/card-quality/list?reason=not_a_real_reason')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        nm_ids = self._nm_ids(payload)
        # Filter not applied -> all 3 own cards returned (still tenant-scoped)
        self.assertEqual(nm_ids, {101, 102, 103})

    def test_requires_login(self):
        resp = self.app.test_client().get('/api/card-quality/list')
        self.assertIn(resp.status_code, (302, 401))

    def test_first_photo_url_built_from_wb_indices_and_passthrough(self):
        client = self._client_logged_in()
        resp = client.get('/api/card-quality/list')
        self.assertEqual(resp.status_code, 200)
        by_nm = {item['nm_id']: item for item in resp.get_json()['items']}
        # photos_json с WB-индексами [1,2,3] → CDN-URL с nm_id и первым индексом
        url_103 = by_nm[103]['first_photo_url']
        self.assertIn('/103/', url_103)
        self.assertTrue(url_103.endswith('/1.webp'))
        # photos_json с готовым http-URL → возвращается как есть
        self.assertEqual(by_nm[101]['first_photo_url'], 'https://cdn.example.com/photo1.jpg')
        # без фото → пустая строка
        self.assertEqual(by_nm[102]['first_photo_url'], '')

    def test_summary_cached_copy_not_mutated_by_filtered_total(self):
        from services.ttl_cache import cache
        client = self._client_logged_in()
        # Первый запрос с фильтром кладёт сводку в кэш; total в ответе — фильтрованный
        resp1 = client.get('/api/card-quality/list?reason=few_photos')
        self.assertEqual(resp1.get_json()['summary']['total'], 1)
        # Кэшированный объект не должен унаследовать фильтрованный total
        cached = cache.get_or_load(f'cq-summary:{self.seller_id}', 60, lambda: {'total': -1})
        self.assertNotEqual(cached.get('total'), 1)


if __name__ == '__main__':
    unittest.main()
