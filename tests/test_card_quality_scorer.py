# -*- coding: utf-8 -*-
"""Тесты детерминированного Quality Score."""

import json
import types
import unittest

from services.card_quality_scorer import (
    WEIGHTS, score_status, compute_card_quality, product_to_card_input,
)


def _perfect_card():
    return {
        'photos': ['u'] * 8,
        'characteristics': {f'k{i}': 'v' for i in range(10)},
        'title': 'x' * 40,
        'description': 'y' * 400,
        'brand': 'BrandX',
        'barcodes': ['1234567890123'],
        'price': 999,
        'subject_id': 5,
    }


def _empty_card():
    return {
        'photos': [], 'characteristics': {}, 'title': '', 'description': '',
        'brand': '', 'barcodes': [], 'price': 0, 'subject_id': None,
    }


class TestScoreStatus(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(score_status(90), 'excellent')
        self.assertEqual(score_status(85), 'excellent')
        self.assertEqual(score_status(70), 'good')
        self.assertEqual(score_status(69.9), 'average')
        self.assertEqual(score_status(50), 'average')
        self.assertEqual(score_status(30), 'poor')


class TestWeights(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(sum(WEIGHTS.values()), 100)


class TestComputeCardQuality(unittest.TestCase):
    def test_perfect_card_scores_100(self):
        result = compute_card_quality(_perfect_card())
        self.assertEqual(result['score'], 100.0)
        self.assertEqual(result['status'], 'excellent')
        self.assertEqual(result['recommendations'], [])

    def test_empty_card_scores_0_and_has_recommendations(self):
        result = compute_card_quality(_empty_card())
        self.assertEqual(result['score'], 0.0)
        self.assertEqual(result['status'], 'poor')
        self.assertTrue(len(result['recommendations']) >= 5)

    def test_photos_subscore_is_proportional(self):
        card = _perfect_card()
        card['photos'] = ['u', 'u', 'u']  # 3 photos
        dim = compute_card_quality(card)['dimensions']['photos']
        self.assertEqual(dim['score'], 37)        # 3 * 100 // 8
        self.assertEqual(dim['status'], 'warning')
        self.assertTrue(dim['hint'])

    def test_all_dimensions_present(self):
        dims = compute_card_quality(_perfect_card())['dimensions']
        self.assertEqual(set(dims.keys()), set(WEIGHTS.keys()))

    def test_recommendations_sorted_by_impact(self):
        # missing photos (weight 20) must rank above missing brand (weight 10)
        card = _perfect_card()
        card['photos'] = []
        card['brand'] = ''
        recs = compute_card_quality(card)['recommendations']
        joined = ' || '.join(recs)
        self.assertIn('фото', joined.lower())
        self.assertLess(joined.lower().index('фото'), joined.lower().index('бренд'))


class TestProductToCardInput(unittest.TestCase):
    def test_reads_product_attributes(self):
        product = types.SimpleNamespace(
            photos_json=json.dumps(['a', 'b']),
            characteristics_json=json.dumps({'Цвет': 'красный'}),
            sizes_json=json.dumps([{'skus': ['111', '222']}]),
            title='Товар', description='Описание', brand='Бренд',
            price=500, subject_id=64,
        )
        card = product_to_card_input(product)
        self.assertEqual(card['photos'], ['a', 'b'])
        self.assertEqual(card['characteristics'], {'Цвет': 'красный'})
        self.assertEqual(card['barcodes'], ['111', '222'])
        self.assertEqual(card['title'], 'Товар')
        self.assertEqual(card['subject_id'], 64)
