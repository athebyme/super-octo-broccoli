# -*- coding: utf-8 -*-
"""Small regression tests for auto-publish flow helpers."""

import unittest

from services.auto_publish_service import _is_retryable_error
from services.wb_api_client import normalize_cards_error_list


class TestAutoPublishServiceHelpers(unittest.TestCase):
    def test_retryable_infrastructure_errors(self):
        self.assertTrue(_is_retryable_error("Timeout while uploading card"))
        self.assertTrue(_is_retryable_error("WB API 503 service unavailable"))
        self.assertTrue(_is_retryable_error("429 too many requests"))

    def test_non_retryable_payload_errors(self):
        self.assertFalse(_is_retryable_error("bad request: missing brand"))
        self.assertFalse(_is_retryable_error("API ключ WB не настроен"))
        self.assertFalse(_is_retryable_error("Бренд запрещён"))


class TestNormalizeCardsErrorList(unittest.TestCase):
    def test_real_v2_format_errors_dict(self):
        """Актуальный формат WB: errors — dict {vendorCode: [messages]}."""
        raw = [{
            'batchUUID': 'a3c4c774',
            'vendorCodes': ['1366Z1C1AУТ-00005295'],
            'errors': {'1366Z1C1AУТ-00005295': ['Недопустимое значение цвета "мягкий"']},
            'updatedAt': '2026-07-12T21:58:05Z',
        }]
        by_nm, by_vendor = normalize_cards_error_list(raw)
        self.assertEqual(by_nm, {})
        self.assertEqual(
            by_vendor,
            {'1366Z1C1AУТ-00005295': ['Недопустимое значение цвета "мягкий"']},
        )

    def test_legacy_format_errors_list(self):
        raw = [{
            'object': 'Вибраторы', 'nmID': 123456,
            'vendorCode': 'VC-1', 'errors': ['Баркод уже используется'],
        }]
        by_nm, by_vendor = normalize_cards_error_list(raw)
        self.assertEqual(by_nm, {123456: ['Баркод уже используется']})
        self.assertEqual(by_vendor, {'VC-1': ['Баркод уже используется']})

    def test_garbage_entries_ignored(self):
        by_nm, by_vendor = normalize_cards_error_list(
            [None, 'str', {}, {'errors': None}, {'errors': []}]
        )
        self.assertEqual(by_nm, {})
        self.assertEqual(by_vendor, {})

    def test_empty_input(self):
        self.assertEqual(normalize_cards_error_list(None), ({}, {}))
        self.assertEqual(normalize_cards_error_list([]), ({}, {}))


if __name__ == "__main__":
    unittest.main()
