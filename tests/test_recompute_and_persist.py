# -*- coding: utf-8 -*-
"""Тест recompute_and_persist: персист Quality Score + снимок CardRatingHistory."""

import json
import os
import unittest
from datetime import datetime


class TestRecomputeAndPersist(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        from flask import Flask
        from models import db
        cls.app = Flask(__name__)
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        cls.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        cls.app.config['SECRET_KEY'] = 'test'
        cls.app.config['WTF_CSRF_ENABLED'] = False
        db.init_app(cls.app)
        cls.db = db
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.db.drop_all()
        cls.ctx.pop()

    _seller_counter = 0

    def _make_seller(self):
        from models import User, Seller
        TestRecomputeAndPersist._seller_counter += 1
        n = TestRecomputeAndPersist._seller_counter
        user = User(username=f'u{n}', email=f'u{n}@example.com', password_hash='x')
        self.db.session.add(user)
        self.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Тест')
        self.db.session.add(seller)
        self.db.session.flush()
        return seller

    def test_persists_score_breakdown_and_history(self):
        from models import Product, CardRatingHistory
        from services.card_quality_scorer import recompute_and_persist

        seller = self._make_seller()
        product = Product(
            seller_id=seller.id, nm_id=105146863, vendor_code='SKU-1', title='Хороший товар детальный',
            photos_json=json.dumps(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']),
            characteristics_json=json.dumps({'Цвет': 'к', 'Размер': 'M', 'Состав': 'хлопок'}),
            sizes_json=json.dumps([{'skus': ['111']}]),
            description='d' * 400, brand='Бренд', price=999, subject_id=64,
            nm_rating=8.0, wb_feedback_rating=4.8,
        )
        self.db.session.add(product)
        self.db.session.flush()

        result = recompute_and_persist(product, capture_history=True)

        self.assertIsInstance(result, dict)
        self.assertIn('score', result)
        self.assertIsNotNone(product.quality_score)
        self.assertEqual(product.quality_score, result['score'])
        breakdown = json.loads(product.quality_breakdown_json)
        self.assertIn('photos', breakdown)
        self.assertIn('characteristics', breakdown)
        self.assertIsInstance(product.quality_checked_at, datetime)

        rows = CardRatingHistory.query.filter_by(product_id=product.id).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].seller_id, seller.id)
        self.assertEqual(rows[0].nm_id, 105146863)
        self.assertEqual(rows[0].quality_score, result['score'])
        self.assertEqual(rows[0].wb_product_rating, 8.0)
        self.assertEqual(rows[0].wb_feedback_rating, 4.8)

    def test_capture_history_false_skips_snapshot(self):
        from models import Product, CardRatingHistory
        from services.card_quality_scorer import recompute_and_persist

        seller = self._make_seller()
        product = Product(seller_id=seller.id, nm_id=100142591, vendor_code='SKU-2', title='Товар')
        self.db.session.add(product)
        self.db.session.flush()

        recompute_and_persist(product, capture_history=False)

        self.assertIsNotNone(product.quality_score)
        self.assertIsNotNone(product.quality_breakdown_json)
        self.assertIsNotNone(product.quality_checked_at)
        rows = CardRatingHistory.query.filter_by(product_id=product.id).all()
        self.assertEqual(len(rows), 0)


if __name__ == '__main__':
    unittest.main()
