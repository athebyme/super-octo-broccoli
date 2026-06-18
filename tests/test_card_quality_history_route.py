# -*- coding: utf-8 -*-
"""Тест роута GET /api/card-quality/<int:product_id>/history (seller-scoped, newest-first)."""

import os
import unittest
from datetime import datetime, timedelta


class TestCardQualityHistoryRoute(unittest.TestCase):
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
        # share one in-memory DB.
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
        from models import User, Seller, Product, CardEditHistory

        # --- Seller 1 ---
        user1 = User(username='hist_seller1', email='hist_seller1@example.com', password_hash='x')
        cls.db.session.add(user1)
        cls.db.session.flush()
        seller1 = Seller(user_id=user1.id, company_name='ООО Тест1', wb_seller_id='hist-123')
        seller1.wb_api_key = 'test-api-key-1'
        cls.db.session.add(seller1)
        cls.db.session.flush()
        cls.user1_id = user1.id
        cls.seller1_id = seller1.id

        product1 = Product(seller_id=seller1.id, nm_id=101, vendor_code='H-A', is_active=True,
                           quality_score=80, nm_rating=7.0)
        cls.db.session.add(product1)
        cls.db.session.flush()
        cls.product1_id = product1.id

        # Two history rows for seller1/product1 — older first in DB but we expect newest first
        older_time = datetime(2025, 1, 1, 10, 0, 0)
        newer_time = datetime(2025, 1, 2, 10, 0, 0)

        h1 = CardEditHistory(
            product_id=product1.id,
            seller_id=seller1.id,
            action='update',
            changed_fields=['title', 'description'],
            snapshot_before={'title': 'Old title', 'description': 'Old desc'},
            snapshot_after={'title': 'New title', 'description': 'New desc'},
            wb_synced=True,
            wb_sync_status='success',
            user_comment=None,
            created_at=older_time,
        )
        h2 = CardEditHistory(
            product_id=product1.id,
            seller_id=seller1.id,
            action='update',
            changed_fields=['photos'],
            snapshot_before={'photos': []},
            snapshot_after={'photos': ['url1']},
            wb_synced=False,
            wb_sync_status='pending',
            user_comment='добавили фото',
            created_at=newer_time,
        )
        cls.db.session.add_all([h1, h2])

        # --- Seller 2 ---
        user2 = User(username='hist_seller2', email='hist_seller2@example.com', password_hash='x')
        cls.db.session.add(user2)
        cls.db.session.flush()
        seller2 = Seller(user_id=user2.id, company_name='ООО Тест2', wb_seller_id='hist-456')
        seller2.wb_api_key = 'test-api-key-2'
        cls.db.session.add(seller2)
        cls.db.session.flush()
        cls.user2_id = user2.id
        cls.seller2_id = seller2.id

        product2 = Product(seller_id=seller2.id, nm_id=202, vendor_code='H-B', is_active=True,
                           quality_score=60, nm_rating=8.0)
        cls.db.session.add(product2)
        cls.db.session.flush()
        cls.product2_id = product2.id

        h3 = CardEditHistory(
            product_id=product2.id,
            seller_id=seller2.id,
            action='update',
            changed_fields=['brand'],
            snapshot_before={'brand': 'OldBrand'},
            snapshot_after={'brand': 'NewBrand'},
            wb_synced=False,
            wb_sync_status='skipped',
            user_comment=None,
            created_at=datetime(2025, 1, 3, 10, 0, 0),
        )
        cls.db.session.add(h3)
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.drop_all()
        cls._engine.dispose()

    def _client_logged_in(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True
        return client

    def test_history_returns_items_newest_first(self):
        """Two history rows returned newest-first, with expected fields."""
        client = self._client_logged_in(self.user1_id)
        resp = client.get(f'/api/card-quality/{self.product1_id}/history')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        items = payload['items']
        self.assertEqual(len(items), 2)
        # newest first — explicit date checks on seeded rows
        self.assertEqual(items[0]['created_at'][:10], '2025-01-02')
        self.assertEqual(items[1]['created_at'][:10], '2025-01-01')
        # fields present
        for item in items:
            self.assertIn('created_at', item)
            self.assertIn('changed_fields', item)
            self.assertIn('action', item)
            self.assertIn('wb_synced', item)
            self.assertIn('wb_sync_status', item)
            self.assertIn('changes', item)
        # newest row has 'photos' changed
        self.assertEqual(items[0]['changed_fields'], ['photos'])
        self.assertEqual(items[0]['user_comment'], 'добавили фото')
        # older row has title+description
        self.assertEqual(items[1]['changed_fields'], ['title', 'description'])

    def test_history_seller_scoped(self):
        """Seller2 cannot access seller1's product history — returns 404."""
        client = self._client_logged_in(self.user2_id)
        resp = client.get(f'/api/card-quality/{self.product1_id}/history')
        self.assertEqual(resp.status_code, 404)
        payload = resp.get_json()
        self.assertFalse(payload['success'])

    def test_history_404_unknown_product(self):
        """Non-existent product_id returns 404."""
        client = self._client_logged_in(self.user1_id)
        resp = client.get('/api/card-quality/99999/history')
        self.assertEqual(resp.status_code, 404)
        payload = resp.get_json()
        self.assertFalse(payload['success'])


if __name__ == '__main__':
    unittest.main()
