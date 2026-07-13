# -*- coding: utf-8 -*-
"""Тест фильтра «Только слабые карточки» в products_list."""

import os
import unittest


class TestProductsQualityFilter(unittest.TestCase):
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
        from models import User, Seller, Product
        user = User(username='seller1', email='seller1@example.com', password_hash='x')
        cls.db.session.add(user)
        cls.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Тест', wb_seller_id='123')
        seller.wb_api_key = 'test-api-key'
        cls.db.session.add(seller)
        cls.db.session.flush()
        cls.user_id = user.id
        cls.db.session.add_all([
            # v2 "weak" definition: non-empty attention_reasons (see
            # services/card_quality_scorer.compute_attention). A high raw
            # quality_score/nm_rating with NO attention reasons must NOT be
            # treated as weak, and an empty-string attention_reasons (legacy
            # default) must behave the same as NULL.
            Product(seller_id=seller.id, nm_id=1, vendor_code='STRONG-1', is_active=True,
                    quality_score=90, nm_rating=9.0, attention_reasons=None),
            Product(seller_id=seller.id, nm_id=2, vendor_code='WEAK-QUALITY', is_active=True,
                    quality_score=40, nm_rating=8.0, attention_reasons='weak_chars,weak_description'),
            Product(seller_id=seller.id, nm_id=3, vendor_code='WEAK-RATING', is_active=True,
                    quality_score=80, nm_rating=5.0, attention_reasons='low_rating'),
            Product(seller_id=seller.id, nm_id=4, vendor_code='STRONG-EMPTY-REASONS', is_active=True,
                    quality_score=95, nm_rating=9.5, attention_reasons=''),
            # Discriminates v1 vs v2: low score/rating but NO attention
            # reasons — the removed v1 definition (`quality_score < 50 |
            # nm_rating < 6`) would wrongly flag this as weak.
            Product(seller_id=seller.id, nm_id=5, vendor_code='LOW-SCORE-NO-REASONS', is_active=True,
                    quality_score=10, nm_rating=2.0, attention_reasons=None),
            # Discriminates v1 vs v2: high score/rating but HAS attention
            # reasons (e.g. behavioral signal) — v1 would wrongly skip this.
            Product(seller_id=seller.id, nm_id=6, vendor_code='HIGH-SCORE-HAS-REASONS', is_active=True,
                    quality_score=95, nm_rating=9.5, attention_reasons='no_views'),
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

    def test_weak_filter_returns_only_weak(self):
        client = self._client()
        resp = client.get('/products?quality_weak=1')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('WEAK-QUALITY', html)
        self.assertIn('WEAK-RATING', html)
        self.assertNotIn('STRONG-1', html)
        # High score/rating alone is not "weak" in v2 — only attention_reasons is.
        self.assertNotIn('STRONG-EMPTY-REASONS', html)
        self.assertNotIn('LOW-SCORE-NO-REASONS', html)
        self.assertIn('HIGH-SCORE-HAS-REASONS', html)

    def test_no_filter_returns_all(self):
        client = self._client()
        resp = client.get('/products')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('WEAK-QUALITY', html)
        self.assertIn('STRONG-1', html)
        self.assertIn('STRONG-EMPTY-REASONS', html)


if __name__ == '__main__':
    unittest.main()
