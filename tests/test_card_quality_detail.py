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

    def test_uses_persisted_v2_score_and_breakdown_when_available(self):
        # product.quality_score/quality_breakdown_json are the persistent v2
        # values that list/filter/bucket queries rely on (see
        # card_quality_scorer.recompute_and_persist). card_quality_detail must
        # surface the SAME numbers, not a live re-score built without the
        # category-schema context (which caps the characteristics dimension
        # at 70 and would silently disagree with the rest of the UI).
        persisted_breakdown = {
            'characteristics': {'score': 20, 'status': 'warning', 'weight': 25,
                                 'hint': 'Заполните характеристики категории'},
            'photos': {'score': 100, 'status': 'ok', 'weight': 20, 'hint': ''},
            'description': {'score': 10, 'status': 'error', 'weight': 20,
                             'hint': 'Добавьте описание товара'},
            'title': {'score': 100, 'status': 'ok', 'weight': 15, 'hint': ''},
            'brand': {'score': 0, 'status': 'warning', 'weight': 8, 'hint': 'Не указан бренд'},
            'barcodes': {'score': 100, 'status': 'ok', 'weight': 6, 'hint': ''},
            'price': {'score': 100, 'status': 'ok', 'weight': 3, 'hint': ''},
            'category': {'score': 100, 'status': 'ok', 'weight': 3, 'hint': ''},
        }
        product = types.SimpleNamespace(
            id=4, nm_id=400, vendor_code='SKU-4', title='Товар',
            photos_json=json.dumps(['a']),
            characteristics_json=json.dumps({'Цвет': 'к'}),
            sizes_json=json.dumps([]),
            description='', brand='', price=999, subject_id=64,
            nm_rating=None, wb_feedback_rating=None, nm_rating_checked_at=None,
            quality_score=28.0,
            quality_breakdown_json=json.dumps(persisted_breakdown, ensure_ascii=False),
        )
        d = card_quality_detail(product)
        self.assertEqual(d['quality_score'], 28.0)
        self.assertEqual(d['dimensions'], persisted_breakdown)
        # impact = weight * (100 - score) desc: description 20*90=1800,
        # characteristics 25*80=2000, brand 8*100=800 -> characteristics first.
        self.assertEqual(d['recommendations'][0], 'Заполните характеристики категории')
        self.assertEqual(d['recommendations'][1], 'Добавьте описание товара')
        self.assertEqual(d['recommendations'][2], 'Не указан бренд')

    def test_falls_back_to_live_score_when_not_persisted(self):
        # quality_score is None (never computed/persisted) -> old live behavior.
        product = types.SimpleNamespace(
            id=5, nm_id=500, vendor_code='SKU-5', title='Товар' * 3,
            photos_json=json.dumps(['a', 'b', 'c', 'd', 'e']),
            characteristics_json=json.dumps({'Цвет': 'к', 'Размер': 'M'}),
            sizes_json=json.dumps([{'skus': ['111']}]),
            description='d' * 300, brand='Бренд', price=999, subject_id=64,
            nm_rating=8.0, wb_feedback_rating=4.8, nm_rating_checked_at=None,
            quality_score=None, quality_breakdown_json=None,
        )
        d = card_quality_detail(product)
        from services.card_quality_scorer import product_to_card_input, compute_card_quality
        expected = compute_card_quality(product_to_card_input(product))
        self.assertEqual(d['quality_score'], expected['score'])
        self.assertEqual(d['dimensions'], expected['dimensions'])
        self.assertEqual(d['recommendations'], expected['recommendations'])

    def test_ignores_corrupt_persisted_breakdown(self):
        # quality_score is set but breakdown JSON is corrupt/empty -> live path,
        # not a crash and not a bare persisted score without dimensions.
        product = types.SimpleNamespace(
            id=6, nm_id=600, vendor_code='SKU-6', title='Товар',
            photos_json=json.dumps(['a']), characteristics_json=json.dumps({}),
            sizes_json=json.dumps([]),
            description='', brand='', price=0, subject_id=None,
            nm_rating=None, wb_feedback_rating=None, nm_rating_checked_at=None,
            quality_score=28.0, quality_breakdown_json='{not json',
        )
        d = card_quality_detail(product)
        from services.card_quality_scorer import product_to_card_input, compute_card_quality
        expected = compute_card_quality(product_to_card_input(product))
        self.assertEqual(d['quality_score'], expected['score'])
        self.assertEqual(d['dimensions'], expected['dimensions'])
