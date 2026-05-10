# -*- coding: utf-8 -*-
"""Small regression tests for auto-publish flow helpers."""

import unittest

from services.auto_publish_service import _is_retryable_error


class TestAutoPublishServiceHelpers(unittest.TestCase):
    def test_retryable_infrastructure_errors(self):
        self.assertTrue(_is_retryable_error("Timeout while uploading card"))
        self.assertTrue(_is_retryable_error("WB API 503 service unavailable"))
        self.assertTrue(_is_retryable_error("429 too many requests"))

    def test_non_retryable_payload_errors(self):
        self.assertFalse(_is_retryable_error("bad request: missing brand"))
        self.assertFalse(_is_retryable_error("API ключ WB не настроен"))
        self.assertFalse(_is_retryable_error("Бренд запрещён"))


if __name__ == "__main__":
    unittest.main()
