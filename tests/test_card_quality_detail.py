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

    def test_includes_photos_list_for_thumbnail(self):
        product = types.SimpleNamespace(
            id=2, nm_id=200, vendor_code='SKU-2', title='Товар',
            photos_json=json.dumps(['https://x/1.jpg', 'https://x/2.jpg']),
            characteristics_json=json.dumps({}),
            sizes_json=json.dumps([]),
            description='', brand='', price=0, subject_id=None,
            nm_rating=None, wb_feedback_rating=None,
            nm_rating_checked_at=None,
        )
        d = card_quality_detail(product)
        self.assertEqual(d['photos'], ['https://x/1.jpg', 'https://x/2.jpg'])

    def test_photos_empty_when_none(self):
        product = types.SimpleNamespace(
            id=3, nm_id=300, vendor_code='SKU-3', title='Товар',
            photos_json=None, characteristics_json=None, sizes_json=None,
            description='', brand='', price=0, subject_id=None,
            nm_rating=None, wb_feedback_rating=None, nm_rating_checked_at=None,
        )
        d = card_quality_detail(product)
        self.assertEqual(d['photos'], [])
