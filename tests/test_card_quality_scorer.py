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
        'photos': ['u'] * 10,
        'characteristics': {f'Характеристика {i}': 'знач' for i in range(10)},
        'available_charcs': [
            {'name': f'Характеристика {i}', 'required': i < 3} for i in range(10)
        ],
        'title': 'Платье летнее женское хлопковое миди с поясом',
        'description': (
            'Лёгкое летнее платье из натурального хлопка свободного кроя. '
            'Дышащая ткань подходит для жаркой погоды, приталенный силуэт '
            'подчёркивает фигуру. Длина миди, съёмный пояс в комплекте. '
            'Машинная стирка при тридцати градусах, материал не мнётся и '
            'сохраняет цвет после многочисленных стирок. Подходит для '
            'офиса, прогулок и отпуска. Карманы по бокам, скрытая молния '
            'на спине, подкладка из вискозы. Размерная сетка соответствует '
            'российским стандартам, при сомнениях берите размер больше. '
            'Производство Россия, сертификат соответствия имеется. '
            'В комплекте чехол для хранения и инструкция по уходу за тканью. '
            'Модель хорошо сочетается с босоножками и лёгким жакетом в '
            'прохладную погоду.'
        ),
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
        self.assertEqual(dim['score'], 30)        # min(3 * 10, 40)
        self.assertEqual(dim['status'], 'warning')
        self.assertTrue(dim['hint'])

    def test_all_dimensions_present(self):
        dims = compute_card_quality(_perfect_card())['dimensions']
        self.assertEqual(set(dims.keys()), set(WEIGHTS.keys()))

    def test_recommendations_sorted_by_impact(self):
        # missing photos (weight 20) must rank above missing brand (weight 8)
        card = _perfect_card()
        card['photos'] = []
        card['brand'] = ''
        recs = compute_card_quality(card)['recommendations']
        joined = ' || '.join(recs)
        self.assertIn('фото', joined.lower())
        self.assertLess(joined.lower().index('фото'), joined.lower().index('бренд'))


class TestPhotosV2(unittest.TestCase):
    def test_zero_photos_is_error(self):
        card = _perfect_card(); card['photos'] = []
        d = compute_card_quality(card)['dimensions']['photos']
        self.assertEqual(d['score'], 0); self.assertEqual(d['status'], 'error')

    def test_under_5_photos_capped_at_40(self):
        card = _perfect_card(); card['photos'] = ['u'] * 4
        d = compute_card_quality(card)['dimensions']['photos']
        self.assertEqual(d['score'], 40); self.assertEqual(d['status'], 'warning')

    def test_10_photos_is_100(self):
        card = _perfect_card(); card['photos'] = ['u'] * 10
        self.assertEqual(compute_card_quality(card)['dimensions']['photos']['score'], 100)


class TestDescriptionV2(unittest.TestCase):
    def test_length_scale_600(self):
        card = _perfect_card()
        self.assertEqual(compute_card_quality(card)['dimensions']['description']['score'], 100)

    def test_duplicate_penalty(self):
        card = _perfect_card(); card['description_dup'] = True
        d = compute_card_quality(card)['dimensions']['description']
        self.assertEqual(d['score'], 40)  # 100 * 0.4
        self.assertEqual(d['status'], 'warning')

    def test_low_unique_words_penalty(self):
        card = _perfect_card()
        card['description'] = ('слово другое ' * 60)[:650]  # длина 600+, но 2 уникальных слова
        d = compute_card_quality(card)['dimensions']['description']
        self.assertEqual(d['score'], 60)  # 100 * 0.6
        self.assertEqual(d['status'], 'warning')


class TestTitleV2(unittest.TestCase):
    def test_good_title_100(self):
        self.assertEqual(compute_card_quality(_perfect_card())['dimensions']['title']['score'], 100)

    def test_few_significant_words_penalty(self):
        card = _perfect_card(); card['title'] = 'Ааааааааааааа ббббббббббббб'  # 2 слова, длина 27
        d = compute_card_quality(card)['dimensions']['title']
        self.assertEqual(d['score'], 70); self.assertEqual(d['status'], 'warning')

    def test_word_spam_penalty(self):
        card = _perfect_card(); card['title'] = 'платье платье платье красное летнее'
        d = compute_card_quality(card)['dimensions']['title']
        self.assertEqual(d['score'], 80)  # 100 - 20 за повтор ≥3
        self.assertEqual(d['status'], 'warning')

    def test_caps_penalty(self):
        card = _perfect_card(); card['title'] = 'ПЛАТЬЕ ЛЕТНЕЕ ЖЕНСКОЕ ХЛОПКОВОЕ МИДИ'
        d = compute_card_quality(card)['dimensions']['title']
        self.assertEqual(d['score'], 80); self.assertEqual(d['status'], 'warning')

    def test_over_60_still_50(self):
        card = _perfect_card(); card['title'] = 'х' * 61
        self.assertEqual(compute_card_quality(card)['dimensions']['title']['score'], 50)


class TestCharacteristicsV2(unittest.TestCase):
    def _card(self, chars, available):
        card = _perfect_card()
        card['characteristics'] = chars
        card['available_charcs'] = available
        return card

    def test_full_fill_is_100(self):
        av = [{'name': 'Цвет', 'required': True}, {'name': 'Состав', 'required': False}]
        d = compute_card_quality(self._card({'Цвет': 'красный', 'Состав': 'хлопок'}, av))
        self.assertEqual(d['dimensions']['characteristics']['score'], 100)

    def test_required_weighted_x3(self):
        av = [{'name': 'Цвет', 'required': True}, {'name': 'Состав', 'required': False}]
        # заполнен только optional: 1 / (3+1) = 25
        d = compute_card_quality(self._card({'Состав': 'хлопок'}, av))
        self.assertEqual(d['dimensions']['characteristics']['score'], 25)

    def test_name_match_case_insensitive(self):
        av = [{'name': 'Цвет', 'required': False}]
        d = compute_card_quality(self._card({'цвет ': 'красный'}, av))
        self.assertEqual(d['dimensions']['characteristics']['score'], 100)

    def test_zero_filled_is_error(self):
        av = [{'name': 'Цвет', 'required': True}]
        d = compute_card_quality(self._card({}, av))['dimensions']['characteristics']
        self.assertEqual(d['score'], 0); self.assertEqual(d['status'], 'error')

    def test_fallback_without_config_capped_70(self):
        d = compute_card_quality(self._card({f'k{i}': 'v' for i in range(10)}, None))
        self.assertEqual(d['dimensions']['characteristics']['score'], 70)

    def test_list_format_characteristics(self):
        av = [{'name': 'Цвет', 'required': False}]
        chars = [{'name': 'Цвет', 'value': 'красный'}]
        d = compute_card_quality(self._card(chars, av))
        self.assertEqual(d['dimensions']['characteristics']['score'], 100)


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
