# -*- coding: utf-8 -*-
"""Тест payload карточки для UI."""

import json
import types
import unittest
from datetime import datetime

from services.card_quality_scorer import card_quality_detail


class TestCardQualityDetail(unittest.TestCase):
    def test_combines_wb_rating_and_quality_score(self):
        product = types.SimpleNamespace(
            id=1, nm_id=105146863, vendor_code='SKU-1', title='Товар',
            photos_json=json.dumps(['a', 'b', 'c']),
            characteristics_json=json.dumps({'Цвет': 'к', 'Размер': 'M'}),
            sizes_json=json.dumps([{'skus': ['111']}]),
            description='d' * 300, brand='Бренд', price=999, subject_id=64,
            nm_rating=8.0, wb_feedback_rating=4.8,
            nm_rating_checked_at=datetime(2026, 6, 16, 12, 0, 0),
        )
        d = card_quality_detail(product)
        self.assertEqual(d['nm_id'], 105146863)
        self.assertEqual(d['wb_product_rating'], 8.0)
        self.assertEqual(d['wb_feedback_rating'], 4.8)
        self.assertEqual(d['nm_rating_checked_at'], '2026-06-16T12:00:00')
        self.assertIn('photos', d['dimensions'])
        self.assertIsInstance(d['quality_score'], float)
        self.assertIn(d['quality_status'], ('excellent', 'good', 'average', 'poor'))
