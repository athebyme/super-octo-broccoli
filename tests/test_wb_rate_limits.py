# -*- coding: utf-8 -*-
"""Тесты соответствия лимитам WB API.

Официальные лимиты (dev.wildberries.ru), наши бюджеты — с запасом НИЖЕ:
- Контент, общий пул: 100/мин (media/save тут) → у нас 80/мин;
- cards/update, cards/upload, cards/upload/add: по 10/мин каждый → у нас по 8/мин;
- Цены и скидки (discounts): 10 запросов / 6 секунд → у нас 8/6с;
- Маркетплейс (остатки): 300/мин, 4XX считается за 10 → у нас 240/мин;
- при 429 WB шлёт X-Ratelimit-Retry (секунды до повтора).

Лимитеры категорий/эндпоинтов ОБЩИЕ для всех инстансов клиента с одним
токеном — параллельные джобы (цены/остатки vs фото) делят один бюджет.
"""

import unittest
from unittest.mock import MagicMock

from services.wb_api_client import WildberriesAPIClient, WBRateLimitException


class TestPerEndpointLimiters(unittest.TestCase):
    def setUp(self):
        self.client = WildberriesAPIClient("token")

    def test_cards_update_has_dedicated_limiter_below_wb_budget(self):
        limiter = self.client._limiter_for_endpoint('/content/v2/cards/update')
        self.assertIsNotNone(limiter)
        self.assertEqual(limiter.max_requests, 8)  # WB даёт 10/мин

    def test_cards_upload_and_upload_add_have_separate_limiters(self):
        upload = self.client._limiter_for_endpoint('/content/v2/cards/upload')
        upload_add = self.client._limiter_for_endpoint('/content/v2/cards/upload/add')
        self.assertIsNotNone(upload)
        self.assertIsNotNone(upload_add)
        self.assertEqual(upload.max_requests, 8)
        self.assertEqual(upload_add.max_requests, 8)
        self.assertIsNot(upload, upload_add)  # раздельные вёдра — лимиты независимы

    def test_media_save_has_no_endpoint_bucket(self):
        self.assertIsNone(self.client._limiter_for_endpoint('/content/v3/media/save'))

    def test_same_endpoint_returns_same_limiter_instance(self):
        a = self.client._limiter_for_endpoint('/content/v2/cards/update')
        b = self.client._limiter_for_endpoint('/content/v2/cards/update')
        self.assertIs(a, b)


class TestCategoryLimiters(unittest.TestCase):
    def setUp(self):
        self.client = WildberriesAPIClient("token")

    def test_discounts_prices_budget_below_10_per_6s(self):
        limiter = self.client._limiter_for_category('discounts')
        self.assertIsNotNone(limiter)
        self.assertEqual(limiter.max_requests, 8)
        self.assertEqual(limiter.time_window, 6)

    def test_marketplace_stocks_budget_below_300_per_minute(self):
        limiter = self.client._limiter_for_category('marketplace')
        self.assertIsNotNone(limiter)
        self.assertEqual(limiter.max_requests, 240)
        self.assertEqual(limiter.time_window, 60)

    def test_content_budget_below_100_per_minute(self):
        limiter = self.client._limiter_for_category('content')
        self.assertIsNotNone(limiter)
        self.assertEqual(limiter.max_requests, 80)
        self.assertEqual(limiter.time_window, 60)

    def test_statistics_has_no_category_bucket(self):
        self.assertIsNone(self.client._limiter_for_category('statistics'))

    def test_limiters_shared_across_instances_with_same_token(self):
        """Параллельные джобы (цены/остатки и фото) с одним токеном делят бюджет."""
        other = WildberriesAPIClient("token")
        self.assertIs(self.client._limiter_for_category('discounts'),
                      other._limiter_for_category('discounts'))
        self.assertIs(self.client._limiter_for_endpoint('/content/v2/cards/update'),
                      other._limiter_for_endpoint('/content/v2/cards/update'))

    def test_limiters_independent_for_different_tokens(self):
        """Разные продавцы (токены) имеют независимые бюджеты WB."""
        other = WildberriesAPIClient("another-token")
        self.assertIsNot(self.client._limiter_for_category('discounts'),
                         other._limiter_for_category('discounts'))


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
