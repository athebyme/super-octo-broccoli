# -*- coding: utf-8 -*-
"""Тесты авто-обнаружения фото-колонок по префиксу и резолва маппинга."""

import unittest

from services.supplier_service import discover_columns_by_prefix, SupplierCSVParser


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


class TestResolveMappingConfigPrefix(unittest.TestCase):
    def setUp(self):
        # _resolve_mapping_config does not use any attribute of self except being
        # a bound method; pass a bare object as self to avoid building a Supplier.
        self.resolve = SupplierCSVParser._resolve_mapping_config.__get__(object())

    def test_columns_prefix_discovers_all_image_columns(self):
        header_index = {'image': 5, 'image1': 6, 'image2': 7, 'image3': 8}
        cfg = {'type': 'photo_urls', 'columns_prefix': 'image'}
        resolved = self.resolve(cfg, header_index)
        self.assertEqual(resolved['columns'], [5, 6, 7, 8])

    def test_explicit_columns_still_work_without_prefix(self):
        header_index = {'image': 5, 'image1': 6, 'image2': 7}
        cfg = {'type': 'photo_urls', 'columns': ['image', 'image1', 'image2']}
        resolved = self.resolve(cfg, header_index)
        self.assertEqual(resolved['columns'], [5, 6, 7])

    def test_explicit_columns_merge_with_prefix_no_duplicates(self):
        header_index = {'image': 5, 'image1': 6, 'image2': 7, 'image3': 8}
        cfg = {'type': 'photo_urls', 'columns': ['image'], 'columns_prefix': 'image'}
        resolved = self.resolve(cfg, header_index)
        self.assertEqual(resolved['columns'], [5, 6, 7, 8])


class TestExtractPhotoUrls(unittest.TestCase):
    def setUp(self):
        self.extract = SupplierCSVParser._extract_fields_by_mapping.__get__(object())

    def test_collects_all_mapped_columns(self):
        row = ['EXT-1', 'Product Title', '', '', '', 'http://a/1.jpg', 'http://a/2.jpg', 'http://a/3.jpg', 'http://a/4.jpg']
        mapping = {
            'external_id': {'type': 'string', 'column': 0},
            'title': {'type': 'string', 'column': 1},
            'photo_urls': {'type': 'photo_urls', 'columns': [5, 6, 7, 8]},
        }
        product = self.extract(row, mapping)
        self.assertEqual(product['photo_urls'],
                         [{'original': 'http://a/1.jpg'}, {'original': 'http://a/2.jpg'},
                          {'original': 'http://a/3.jpg'}, {'original': 'http://a/4.jpg'}])

    def test_single_column_with_separator_splits(self):
        row = ['EXT-1', 'Product Title', 'http://a/1.jpg; http://a/2.jpg ; http://a/3.jpg']
        mapping = {
            'external_id': {'type': 'string', 'column': 0},
            'title': {'type': 'string', 'column': 1},
            'photo_urls': {'type': 'photo_urls', 'columns': [2], 'separator': ';'},
        }
        product = self.extract(row, mapping)
        self.assertEqual(product['photo_urls'],
                         [{'original': 'http://a/1.jpg'}, {'original': 'http://a/2.jpg'},
                          {'original': 'http://a/3.jpg'}])

    def test_skips_blank_and_non_http(self):
        row = ['EXT-1', 'Product Title', 'http://a/1.jpg', '', 'not-a-url', 'http://a/2.jpg']
        mapping = {
            'external_id': {'type': 'string', 'column': 0},
            'title': {'type': 'string', 'column': 1},
            'photo_urls': {'type': 'photo_urls', 'columns': [2, 3, 4, 5]},
        }
        product = self.extract(row, mapping)
        self.assertEqual(product['photo_urls'],
                         [{'original': 'http://a/1.jpg'}, {'original': 'http://a/2.jpg'}])

    def test_dedup_across_columns_preserves_order(self):
        # sex-opt.ru: image/image1/image2 дублируют первые URL колонки images
        row = ['EXT-1', 'Product Title',
               'http://a/1.jpg', 'http://a/2.jpg',
               'http://a/1.jpg,http://a/2.jpg,http://a/3.jpg,http://a/4.jpg']
        mapping = {
            'external_id': {'type': 'string', 'column': 0},
            'title': {'type': 'string', 'column': 1},
            'photo_urls': {'type': 'photo_urls', 'columns': [2, 3, 4], 'separator': ','},
        }
        product = self.extract(row, mapping)
        self.assertEqual(product['photo_urls'],
                         [{'original': 'http://a/1.jpg'}, {'original': 'http://a/2.jpg'},
                          {'original': 'http://a/3.jpg'}, {'original': 'http://a/4.jpg'}])


class TestSexoptMappingConfig(unittest.TestCase):
    """Маппинг Андрея должен читать колонку images (все фото через запятую)."""

    def test_photo_urls_includes_images_column_with_comma_separator(self):
        from migrations.migrate_add_sexopt_supplier import SEXOPT_CSV_COLUMN_MAPPING
        cfg = SEXOPT_CSV_COLUMN_MAPPING['photo_urls']
        self.assertIn('images', cfg['columns'])
        self.assertEqual(cfg.get('separator'), ',')
