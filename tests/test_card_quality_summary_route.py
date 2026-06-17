# -*- coding: utf-8 -*-
"""Тест роута GET /api/card-quality/summary (seller-scoped JSON)."""

import os
import tempfile
import unittest


class TestCardQualitySummaryRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._db_fd, cls._db_path = tempfile.mkstemp(suffix='.db')
        os.close(cls._db_fd)
        os.environ['DATABASE_URL'] = 'sqlite:///' + cls._db_path
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        os.environ['SECRET_KEY'] = 'test-secret-key-for-unit-tests'
        import seller_platform  # noqa
        cls.app = seller_platform.app
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + cls._db_path
        cls.app.config['SECRET_KEY'] = 'test-secret-key-for-unit-tests'
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['TESTING'] = True
        from models import db
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            cls._seed()

    @classmethod
    def _seed(cls):
        from models import User, Seller, Product
        user = User(username='seller1', email='seller1@example.com', password_hash='x')
        cls.db.session.add(user)
        cls.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Тест', wb_seller_id='123')
        seller.wb_api_key = 'test-api-key'
        cls.db.session.add(seller)
        cls.db.session.flush()
        cls.user_id = user.id
        cls.seller_id = seller.id
        # 2 хорошие, 1 слабая по quality, 1 слабая по nm_rating
        cls.db.session.add_all([
            Product(seller_id=seller.id, nm_id=1, vendor_code='A', is_active=True,
                    quality_score=90, nm_rating=9.0),
            Product(seller_id=seller.id, nm_id=2, vendor_code='B', is_active=True,
                    quality_score=75, nm_rating=8.0),
            Product(seller_id=seller.id, nm_id=3, vendor_code='C', is_active=True,
                    quality_score=40, nm_rating=7.0),
            Product(seller_id=seller.id, nm_id=4, vendor_code='D', is_active=True,
                    quality_score=80, nm_rating=5.0),
        ])
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.drop_all()
        os.remove(cls._db_path)

    def _client_logged_in(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)
            sess['_fresh'] = True
        return client

    def test_summary_returns_seller_scoped_data(self):
        client = self._client_logged_in()
        resp = client.get('/api/card-quality/summary')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        data = payload['data']
        self.assertEqual(data['total'], 4)
        self.assertEqual(data['need_attention'], 2)  # quality<50 ИЛИ nm_rating<6
        self.assertIn('distribution', data)
        self.assertIn('avg_quality', data)

    def test_requires_login(self):
        resp = self.app.test_client().get('/api/card-quality/summary')
        self.assertIn(resp.status_code, (302, 401))


if __name__ == '__main__':
    unittest.main()
