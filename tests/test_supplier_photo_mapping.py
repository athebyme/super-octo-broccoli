# -*- coding: utf-8 -*-
"""Тесты авто-обнаружения фото-колонок по префиксу и резолва маппинга."""

import unittest

from services.supplier_service import discover_columns_by_prefix


class TestDiscoverColumnsByPrefix(unittest.TestCase):
    def test_finds_all_image_columns_in_numeric_order(self):
        header_index = {
            'name': 0, 'price': 1,
            'image': 5, 'image1': 6, 'image2': 7, 'image10': 15, 'image3': 8,
        }
        result = discover_columns_by_prefix(header_index, 'image')
        # bare 'image' first, then numeric suffixes ascending (10 after 3, not after 1)
        self.assertEqual(result, [5, 6, 7, 8, 15])

    def test_ignores_non_matching_headers(self):
        header_index = {'image': 5, 'image_big': 6, 'thumbnail': 7, 'image2': 8}
        # 'image_big' has a non-digit suffix -> must NOT match
        self.assertEqual(discover_columns_by_prefix(header_index, 'image'), [5, 8])

    def test_returns_empty_when_no_match(self):
        self.assertEqual(discover_columns_by_prefix({'a': 0, 'b': 1}, 'image'), [])
