# -*- coding: utf-8 -*-
"""Тест фильтра «Только слабые карточки» в products_list."""

import os
import tempfile
import unittest


class TestProductsQualityFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._db_fd, cls._db_path = tempfile.mkstemp(suffix='.db')
        os.close(cls._db_fd)
        os.environ['DATABASE_URL'] = 'sqlite:///' + cls._db_path
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import seller_platform  # noqa
        cls.app = seller_platform.app
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + cls._db_path
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['TESTING'] = True
        from models import db
        cls.db = db
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
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
            Product(seller_id=seller.id, nm_id=1, vendor_code='STRONG-1', is_active=True,
                    quality_score=90, nm_rating=9.0),
            Product(seller_id=seller.id, nm_id=2, vendor_code='WEAK-QUALITY', is_active=True,
                    quality_score=40, nm_rating=8.0),
            Product(seller_id=seller.id, nm_id=3, vendor_code='WEAK-RATING', is_active=True,
                    quality_score=80, nm_rating=5.0),
        ])
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.db.drop_all()
        cls.ctx.pop()
        os.remove(cls._db_path)

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

    def test_no_filter_returns_all(self):
        client = self._client()
        resp = client.get('/products')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('WEAK-QUALITY', html)
        self.assertIn('STRONG-1', html)


if __name__ == '__main__':
    unittest.main()
