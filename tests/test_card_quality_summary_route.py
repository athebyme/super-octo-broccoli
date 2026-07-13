# -*- coding: utf-8 -*-
"""Тест роута GET /api/card-quality/summary (seller-scoped JSON)."""

import os
import unittest


class TestCardQualitySummaryRoute(unittest.TestCase):
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
        user = User(username='seller1', email='seller1@example.com', password_hash='x')
        cls.db.session.add(user)
        cls.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Тест', wb_seller_id='123')
        seller.wb_api_key = 'test-api-key'
        cls.db.session.add(seller)
        cls.db.session.flush()
        cls.user_id = user.id
        cls.seller_id = seller.id
        # 2 хорошие (без причин), 1 с причиной по характеристикам, 1 с низким рейтингом
        cls.db.session.add_all([
            Product(seller_id=seller.id, nm_id=1, vendor_code='A', is_active=True,
                    quality_score=90, nm_rating=9.0),
            Product(seller_id=seller.id, nm_id=2, vendor_code='B', is_active=True,
                    quality_score=75, nm_rating=8.0),
            Product(seller_id=seller.id, nm_id=3, vendor_code='C', is_active=True,
                    quality_score=40, nm_rating=7.0, attention_reasons='weak_chars'),
            Product(seller_id=seller.id, nm_id=4, vendor_code='D', is_active=True,
                    quality_score=80, nm_rating=5.0, attention_reasons='low_rating'),
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

    def test_summary_returns_seller_scoped_data(self):
        client = self._client_logged_in()
        resp = client.get('/api/card-quality/summary')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        data = payload['data']
        self.assertEqual(data['total'], 4)
        self.assertEqual(data['need_attention'], 2)  # карточки с непустым attention_reasons
        self.assertIn('distribution', data)
        self.assertIn('avg_quality', data)

    def test_requires_login(self):
        resp = self.app.test_client().get('/api/card-quality/summary')
        self.assertIn(resp.status_code, (302, 401))


if __name__ == '__main__':
    unittest.main()
