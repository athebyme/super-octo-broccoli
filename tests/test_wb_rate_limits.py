# -*- coding: utf-8 -*-
"""Тесты соответствия лимитам WB API.

Официальные лимиты (dev.wildberries.ru):
- Контент, общий пул: 100 запросов/мин на аккаунт продавца (media/save — в общем пуле);
- cards/update, cards/upload, cards/upload/add: ОТДЕЛЬНО по 10 запросов/мин каждый;
- при 429 WB шлёт заголовок X-Ratelimit-Retry (секунды до повтора).
"""

import unittest
from unittest.mock import MagicMock

from services.wb_api_client import WildberriesAPIClient, WBRateLimitException


class TestPerEndpointLimiters(unittest.TestCase):
    def setUp(self):
        self.client = WildberriesAPIClient("token")

    def test_cards_update_has_dedicated_10_per_minute_limiter(self):
        limiter = self.client._limiter_for_endpoint('/content/v2/cards/update')
        self.assertIsNotNone(limiter)
        self.assertEqual(limiter.max_requests, 10)

    def test_cards_upload_and_upload_add_have_separate_limiters(self):
        upload = self.client._limiter_for_endpoint('/content/v2/cards/upload')
        upload_add = self.client._limiter_for_endpoint('/content/v2/cards/upload/add')
        self.assertIsNotNone(upload)
        self.assertIsNotNone(upload_add)
        self.assertEqual(upload.max_requests, 10)
        self.assertEqual(upload_add.max_requests, 10)
        self.assertIsNot(upload, upload_add)  # раздельные вёдра — лимиты независимы

    def test_media_save_uses_only_global_pool(self):
        self.assertIsNone(self.client._limiter_for_endpoint('/content/v3/media/save'))

    def test_same_endpoint_returns_same_limiter_instance(self):
        a = self.client._limiter_for_endpoint('/content/v2/cards/update')
        b = self.client._limiter_for_endpoint('/content/v2/cards/update')
        self.assertIs(a, b)


class TestRateLimit429RetryAfter(unittest.TestCase):
    def _make_429_response(self, headers):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = headers
        resp.text = 'too many requests'
        return resp

    def test_429_exception_carries_retry_after_from_header(self):
        client = WildberriesAPIClient("token")
        client.session = MagicMock()
        client.session.request.return_value = self._make_429_response(
            {'X-Ratelimit-Retry': '42'})

        with self.assertRaises(WBRateLimitException) as ctx:
            client._make_request('POST', 'content', '/content/v3/media/save', json={})
        self.assertEqual(ctx.exception.retry_after, 42)

    def test_429_without_header_defaults_retry_after_none(self):
        client = WildberriesAPIClient("token")
        client.session = MagicMock()
        client.session.request.return_value = self._make_429_response({})

        with self.assertRaises(WBRateLimitException) as ctx:
            client._make_request('POST', 'content', '/content/v3/media/save', json={})
        self.assertIsNone(ctx.exception.retry_after)


if __name__ == '__main__':
    unittest.main()
