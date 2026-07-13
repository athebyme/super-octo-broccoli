# -*- coding: utf-8 -*-
"""Тест роута GET /api/card-quality/list (фильтры reason/bucket, сортировка impact)."""

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
        from models import User, Seller, Product

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

        # 3 own products, impacts 5 / 20 / 40, different reasons, different buckets
        cls.db.session.add_all([
            Product(seller_id=seller.id, nm_id=101, vendor_code='LOW', is_active=True,
                    quality_score=80, nm_rating=9.0,
                    attention_reasons='low_rating', quality_impact=5),
            Product(seller_id=seller.id, nm_id=102, vendor_code='MID', is_active=True,
                    quality_score=60, nm_rating=7.0,
                    attention_reasons='weak_chars', quality_impact=20),
            Product(seller_id=seller.id, nm_id=103, vendor_code='HIGH', is_active=True,
                    quality_score=30, nm_rating=5.0,
                    attention_reasons='few_photos,weak_chars', quality_impact=40),
            # Other seller's product — must never appear in own seller's results
            Product(seller_id=other_seller.id, nm_id=999, vendor_code='OTHER', is_active=True,
                    quality_score=10, nm_rating=3.0,
                    attention_reasons='few_photos', quality_impact=99),
        ])
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.drop_all()
        cls._engine.dispose()

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


if __name__ == '__main__':
    unittest.main()
