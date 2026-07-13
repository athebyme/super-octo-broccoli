# -*- coding: utf-8 -*-
"""Тесты соответствия лимитам WB API.

Официальные лимиты (dev.wildberries.ru), наши бюджеты — с запасом НИЖЕ:
- Контент, общий пул: 100/мин (media/save тут) → у нас 80/мин;
- Контент дополнительно: интервал 600 мс, burst 5 → у нас общий bucket 5/3с;
- cards/update, cards/upload, cards/upload/add: по 10/мин каждый → у нас по 8/мин;
- Бренды: 1 запрос/с с burst 5 → у нас строгий общий bucket 1/с;
- Цены и скидки (discounts): 10 запросов / 6 секунд → у нас 8/6с;
- Маркетплейс (остатки): 300/мин, 4XX считается за 10 → у нас 240/мин;
- при 429 WB шлёт X-Ratelimit-Retry (секунды до повтора).

Лимитеры категорий/эндпоинтов ОБЩИЕ для всех инстансов клиента с одним
токеном — параллельные джобы (цены/остатки vs фото) делят один бюджет.
"""

import unittest
from unittest.mock import MagicMock

from services.brand_cache import BrandCache
from services.wb_api_client import WildberriesAPIClient, WBRateLimitException


class TestPerEndpointLimiters(unittest.TestCase):
    def setUp(self):
        self.client = WildberriesAPIClient("token")

    def test_cards_update_has_dedicated_limiter_below_wb_budget(self):
        limiter = self.client._limiter_for_endpoint('/content/v2/cards/update')
        self.assertIsNotNone(limiter)
        self.assertEqual(limiter.max_requests, 8)  # WB даёт 10/мин

    def test_brands_endpoint_uses_its_current_one_request_per_second_limit(self):
        limiter = self.client._limiter_for_endpoint('/api/content/v1/brands')
        self.assertIsNotNone(limiter)
        self.assertEqual(limiter.max_requests, 1)
        self.assertEqual(limiter.time_window, 1)

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

    def test_content_has_shared_short_window_burst_limiter(self):
        limiter = self.client._limiter_for_content_burst('content')
        other = WildberriesAPIClient("token")._limiter_for_content_burst('content')
        self.assertEqual(limiter.max_requests, 5)
        self.assertEqual(limiter.time_window, 3)
        self.assertIs(limiter, other)
        self.assertIsNone(self.client._limiter_for_content_burst('statistics'))

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

    def test_429_accepts_standard_retry_after_header(self):
        client = WildberriesAPIClient("token")
        client.session = MagicMock()
        client.session.request.return_value = self._make_429_response(
            {'Retry-After': '7'})

        with self.assertRaises(WBRateLimitException) as ctx:
            client._make_request('POST', 'statistics', '/test', json={})
        self.assertEqual(ctx.exception.retry_after, 7)


class TestBrandCursorPagination(unittest.TestCase):
    @staticmethod
    def _response(payload):
        response = MagicMock()
        response.url = 'https://content-api.wildberries.ru/api/content/v1/brands'
        response.status_code = 200
        response.text = '{}'
        response.json.return_value = payload
        return response

    def test_fetch_all_brands_uses_cursor_pages_without_pattern_fanout(self):
        client = WildberriesAPIClient('brand-token')
        client._make_request = MagicMock(side_effect=[
            self._response({
                'brands': [{'id': 1, 'name': 'One'}],
                'next': 11,
                'total': 2,
            }),
            self._response({
                'brands': [{'id': 2, 'name': 'Two'}],
                'next': 0,
                'total': 2,
            }),
            self._response({
                'brands': [{'id': 3, 'name': 'Three'}],
                'next': None,
                'total': 1,
            }),
        ])

        result = client.fetch_all_brands([101, 202], max_requests=10)

        self.assertTrue(result['complete'])
        self.assertEqual(result['requests'], 3)
        self.assertEqual(result['categories_completed'], 2)
        self.assertEqual(result['completed_subject_ids'], [101, 202])
        self.assertEqual(
            {item['id'] for item in result['subject_brands'][101]},
            {1, 2},
        )
        self.assertEqual(
            {item['id'] for item in result['subject_brands'][202]},
            {3},
        )
        self.assertEqual({item['id'] for item in result['data']}, {1, 2, 3})
        calls = client._make_request.call_args_list
        self.assertEqual(calls[0].kwargs['params'], {'subjectId': 101})
        self.assertEqual(calls[1].kwargs['params'], {
            'subjectId': 101,
            'next': 11,
        })
        self.assertEqual(calls[2].kwargs['params'], {'subjectId': 202})
        self.assertNotIn('pattern', calls[0].kwargs['params'])
        self.assertNotIn('top', calls[0].kwargs['params'])

    def test_fetch_all_brands_budget_exhaustion_is_honest_partial(self):
        client = WildberriesAPIClient('brand-budget-token')
        client._make_request = MagicMock(return_value=self._response({
            'brands': [{'id': 1, 'name': 'One'}],
            'next': 11,
            'total': 2,
        }))

        result = client.fetch_all_brands([101], max_requests=1)

        self.assertFalse(result['complete'])
        self.assertEqual(result['requests'], 1)
        self.assertEqual(result['categories_completed'], 0)
        self.assertEqual(result['completed_subject_ids'], [])
        self.assertEqual(result['subject_brands'], {})
        self.assertEqual(result['errors'][0]['code'], 'request_budget_exhausted')
        self.assertEqual(client._make_request.call_count, 1)

    def test_single_subject_can_use_more_than_legacy_25_page_cap(self):
        client = WildberriesAPIClient('brand-many-pages-token')
        pages = []
        for index in range(26):
            pages.append(self._response({
                'brands': [{'id': index + 1, 'name': f'Brand {index + 1}'}],
                'next': index + 1 if index < 25 else 0,
                'total': 26,
            }))
        client._make_request = MagicMock(side_effect=pages)

        result = client.fetch_all_brands([303])

        self.assertTrue(result['complete'])
        self.assertEqual(result['requests'], 26)
        self.assertEqual(result['completed_subject_ids'], [303])
        self.assertEqual(len(result['subject_brands'][303]), 26)

    def test_complete_brand_snapshot_skips_entry_with_empty_name(self):
        client = WildberriesAPIClient('brand-empty-name-token')
        client._make_request = MagicMock(return_value=self._response({
            'brands': [
                {'id': 1, 'name': 'One'},
                {'id': 2, 'name': ''},
            ],
            'next': None,
            'total': 2,
        }))

        result = client.fetch_all_brands([404])

        self.assertTrue(result['complete'])
        self.assertEqual(result['completed_subject_ids'], [404])
        self.assertEqual(result['subject_brands'][404], [
            {'id': 1, 'name': 'One'},
        ])
        self.assertEqual(result['warnings'], [{
            'subject_id': 404,
            'code': 'brands_excluded_invalid_name',
            'count': 1,
        }])

    def test_brand_snapshot_rejects_duplicate_or_invalid_ids(self):
        payloads = (
            {
                'brands': [
                    {'id': 1, 'name': 'One'},
                    {'id': 1, 'name': 'Duplicate'},
                ],
                'next': None,
                'total': 2,
            },
            {
                'brands': [{'id': None, 'name': 'Invalid'}],
                'next': None,
                'total': 1,
            },
            {
                'brands': [{'id': '3', 'name': 'String ID'}],
                'next': None,
                'total': 1,
            },
            {
                'brands': [{'id': True, 'name': 'Boolean ID'}],
                'next': None,
                'total': 1,
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                client = WildberriesAPIClient('brand-invalid-id-token')
                client._make_request = MagicMock(
                    return_value=self._response(payload),
                )

                result = client.fetch_all_brands([505])

                self.assertFalse(result['complete'])
                self.assertEqual(result['completed_subject_ids'], [])
                self.assertEqual(result['subject_brands'], {})
                self.assertEqual(result['errors'][0]['code'], 'page_error')

    def test_brand_snapshot_rejects_duplicate_id_between_cursor_pages(self):
        client = WildberriesAPIClient('brand-cross-page-duplicate-token')
        client._make_request = MagicMock(side_effect=[
            self._response({
                'brands': [{'id': 1, 'name': 'One'}],
                'next': 11,
                'total': 2,
            }),
            self._response({
                'brands': [{'id': 1, 'name': 'Duplicate'}],
                'next': None,
                'total': 2,
            }),
        ])

        result = client.fetch_all_brands([606])

        self.assertFalse(result['complete'])
        self.assertEqual(result['completed_subject_ids'], [])
        self.assertEqual(result['subject_brands'], {})
        self.assertEqual(result['errors'][0]['code'], 'page_error')

    def test_brand_validation_requires_typed_category_without_api_call(self):
        client = WildberriesAPIClient('brand-validation-token')
        client._make_request = MagicMock()

        result = client.validate_brand('LELO')

        self.assertFalse(result['valid'])
        self.assertEqual(result['error'], 'category_scope_required')
        client._make_request.assert_not_called()

    def test_legacy_global_brand_cache_fails_without_erasing_last_good_data(self):
        cache = object.__new__(BrandCache)
        cache.is_syncing = False
        cache.sync_error = None
        cache.brands = {1: 'Last Good'}
        cache.brands_lower = {'last good': 1}
        wb_client = MagicMock()

        result = cache.sync_brands(wb_client)

        self.assertFalse(result)
        self.assertIn('category_scope_required', cache.sync_error)
        self.assertEqual(cache.brands, {1: 'Last Good'})
        wb_client.assert_not_called()

    def test_brand_validation_filters_complete_category_snapshot_locally(self):
        client = WildberriesAPIClient('brand-validation-token')
        client.get_brands_by_subject = MagicMock(return_value={
            'data': [
                {'id': 1, 'name': 'LELO'},
                {'id': 2, 'name': 'Other Brand'},
            ],
            'complete': True,
            'errors': [],
        })

        result = client.validate_brand('Le lo', subject_id=101)

        self.assertTrue(result['valid'])
        self.assertEqual(result['exact_match']['id'], 1)
        self.assertTrue(result['complete'])
        client.get_brands_by_subject.assert_called_once_with(101)

    def test_brand_search_without_category_fails_before_api_call(self):
        client = WildberriesAPIClient('brand-search-token')
        client._make_request = MagicMock()

        with self.assertRaisesRegex(ValueError, 'subject_id is required'):
            client.search_brands('LELO')

        client._make_request.assert_not_called()

if __name__ == '__main__':
    unittest.main()
