# -*- coding: utf-8 -*-
"""Тесты normalize_photo_urls: индексы WB → CDN-URL, http-URL — как есть."""
import unittest

from services.wb_media import normalize_photo_urls


class TestNormalizePhotoUrls(unittest.TestCase):
    def test_indices_become_cdn_urls(self):
        urls = normalize_photo_urls(103, [1, 2])
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(u.startswith('https://basket-') for u in urls))
        self.assertTrue(urls[0].endswith('/1.webp'))
        self.assertIn('/103/', urls[0])

    def test_strings_pass_through_mixed(self):
        urls = normalize_photo_urls(103, ['https://cdn.example.com/a.jpg', 2, '/media/standard/1/x.jpg'])
        self.assertEqual(urls[0], 'https://cdn.example.com/a.jpg')
        self.assertTrue(urls[1].endswith('/2.webp'))
        self.assertEqual(urls[2], '/media/standard/1/x.jpg')

    def test_non_url_types_dropped(self):
        self.assertEqual(normalize_photo_urls(103, [None, {}, True, '  ']), [])

    def test_empty_and_missing_nm_id(self):
        self.assertEqual(normalize_photo_urls(103, []), [])
        self.assertEqual(normalize_photo_urls(None, [1, 2]), [])
