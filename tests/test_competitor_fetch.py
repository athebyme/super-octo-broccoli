# -*- coding: utf-8 -*-
"""Fetch-слой конкурентов: парсинг, health, кэш, bounded-поведение."""
import unittest
from unittest.mock import MagicMock, patch

from services import competitor_fetch as cf


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    return r


SEARCH_PRODUCT = {
    'id': 872594650, 'name': 'Носки высокие набор', 'brand': 'OSMAN',
    'supplier': 'OSMAN', 'supplierId': 4116984,
    'reviewRating': 4.9, 'feedbacks': 1966, 'totalQuantity': 66,
    'sizes': [{'price': {'basic': 370000, 'product': 58900}}],
}


class ParserTest(unittest.TestCase):
    def test_parse_search_product_rubles(self):
        obs = cf.parse_price_observation(SEARCH_PRODUCT)
        self.assertEqual(obs['price'], 3700)       # копейки -> рубли
        self.assertEqual(obs['sale_price'], 589)
        self.assertEqual(obs['rating'], 4.9)
        self.assertEqual(obs['feedbacks_count'], 1966)
        self.assertEqual(obs['total_stock'], 66)

    def test_parse_legacy_priceu_format(self):
        obs = cf.parse_price_observation(
            {'priceU': 250000, 'salePriceU': 199000, 'sizes': []})
        self.assertEqual(obs['price'], 2500)
        self.assertEqual(obs['sale_price'], 1990)

    def test_final_price_prefers_total_and_pair_stays_on_one_size(self):
        obs = cf.parse_price_observation({
            'sizes': [
                {'price': {
                    'basic': 300000,
                    'product': 210000,
                    'total': 175000,
                }},
                {'price': {'basic': 900000, 'product': 100000}},
            ],
        })
        self.assertEqual(obs['price'], 3000)
        self.assertEqual(obs['sale_price'], 1750)

    def test_parse_full_product(self):
        with patch.object(cf, '_image_url', return_value='http://img/1.webp'):
            p = cf.parse_full_product(SEARCH_PRODUCT)
        self.assertEqual(p['nm_id'], 872594650)
        self.assertEqual(p['wb_supplier_id'], 4116984)
        self.assertEqual(p['sale_price'], 589)
        self.assertIn('image_url', p)


class HealthTest(unittest.TestCase):
    def test_cooldown_after_three_failures(self):
        h = cf.SourceHealthRegistry()
        self.assertTrue(h.allowed(cf.SOURCE_SEARCH))
        h.record_failure(cf.SOURCE_SEARCH)
        h.record_failure(cf.SOURCE_SEARCH)
        self.assertTrue(h.allowed(cf.SOURCE_SEARCH))
        h.record_failure(cf.SOURCE_SEARCH)
        self.assertFalse(h.allowed(cf.SOURCE_SEARCH))
        # другой источник не задет
        self.assertTrue(h.allowed(cf.SOURCE_CATALOG))

    def test_success_resets_counter(self):
        h = cf.SourceHealthRegistry()
        h.record_failure(cf.SOURCE_SEARCH)
        h.record_failure(cf.SOURCE_SEARCH)
        h.record_success(cf.SOURCE_SEARCH)
        h.record_failure(cf.SOURCE_SEARCH)
        h.record_failure(cf.SOURCE_SEARCH)
        self.assertTrue(h.allowed(cf.SOURCE_SEARCH))

    def test_cooldown_expires(self):
        h = cf.SourceHealthRegistry()
        for _ in range(3):
            h.record_failure(cf.SOURCE_CATALOG)
        with patch.object(cf.time, 'time', return_value=cf.time.time() + 601):
            self.assertTrue(h.allowed(cf.SOURCE_CATALOG))


class CacheTest(unittest.TestCase):
    def setUp(self):
        cf.clear_observation_cache()

    def tearDown(self):
        cf.clear_observation_cache()

    def test_roundtrip_and_ttl(self):
        cf.put_cached_observation(1, {'sale_price': 100})
        self.assertEqual(cf.get_cached_observation(1)['sale_price'], 100)
        with patch.object(cf.time, 'time', return_value=cf.time.time() + 301):
            self.assertIsNone(cf.get_cached_observation(1))

    def test_bounded_size(self):
        for i in range(cf.CACHE_MAX_ENTRIES + 100):
            cf.put_cached_observation(i, {'sale_price': i})
        # кэш не растёт бесконечно
        self.assertLessEqual(len(cf._observation_cache), cf.CACHE_MAX_ENTRIES)


class FetchServiceTest(unittest.TestCase):
    def _service(self, session):
        limiter = MagicMock()
        health = cf.SourceHealthRegistry()
        return cf.CompetitorFetchService(
            session=session, rate_limiter=limiter, health=health), limiter, health

    def test_basket_metadata_ok(self):
        session = MagicMock()
        session.get.side_effect = [
            _resp(200, {'imt_name': 'Вибратор', 'subj_name': 'Вибраторы',
                        'selling': {'brand_name': 'JOS', 'supplier_id': 332183,
                                    'is_adult': True}}),
            _resp(200, {'supplierName': 'MAGIC TOYS', 'supplierId': 332183}),
        ]
        svc, limiter, _ = self._service(session)
        with patch.object(cf, '_basket_base_url',
                          return_value='https://basket-05.wbbasket.ru/vol807/part80786/80786423'), \
             patch.object(cf, '_image_url', return_value='http://img/1.webp'):
            meta = svc.fetch_basket_metadata(80786423)
        self.assertEqual(meta['title'], 'Вибратор')
        self.assertEqual(meta['wb_supplier_id'], 332183)
        self.assertEqual(meta['supplier_name'], 'MAGIC TOYS')
        self.assertTrue(meta['is_adult'])
        # basket — тоже через глобальный limiter
        self.assertTrue(limiter.wait_if_needed.called)

    def test_basket_404_means_gone(self):
        session = MagicMock()
        session.get.return_value = _resp(404)
        svc, _, _ = self._service(session)
        with patch.object(cf, '_basket_base_url',
                          return_value='https://basket-05.wbbasket.ru/x'):
            self.assertEqual(svc.fetch_basket_metadata(80786423), 'gone')

    def test_search_products_single_page_raises_on_429(self):
        session = MagicMock()
        session.get.return_value = _resp(429)
        svc, _, health = self._service(session)
        with self.assertRaises(cf.WBRateLimitedError):
            svc.search_products('носки')
        # 429 зафиксирован в health
        self.assertEqual(
            health.snapshot()[cf.SOURCE_SEARCH]['consecutive_failures'], 1)
        # ровно один HTTP-вызов, никакой пагинации/sleep
        self.assertEqual(session.get.call_count, 1)

    def test_seller_preview_hard_caps_unexpected_large_page(self):
        products = [dict(SEARCH_PRODUCT, id=index + 1) for index in range(140)]
        session = MagicMock()
        session.get.return_value = _resp(200, {'data': {'products': products}})
        svc, _, _ = self._service(session)
        preview = svc.fetch_seller_catalog_page(4116984)
        self.assertEqual(len(preview), cf.INTERACTIVE_CATALOG_LIMIT)
        self.assertEqual(session.get.call_count, 1)
        url = session.get.call_args.args[0]
        params = session.get.call_args.kwargs['params']
        self.assertEqual(url, 'https://catalog.wb.ru/sellers/v4/catalog')
        self.assertEqual(params['supplier'], '4116984')
        self.assertEqual(params['page'], '1')
        self.assertEqual(params['spp'], '30')
        self.assertEqual(params['ab_testing'], 'false')
        self.assertNotIn('limit', params)

    def test_supplier_prices_collects_targets_and_stops(self):
        page1 = {'data': {'products': [
            dict(SEARCH_PRODUCT, id=111), dict(SEARCH_PRODUCT, id=222)]}}
        session = MagicMock()
        session.get.return_value = _resp(200, page1)
        svc, _, _ = self._service(session)
        found = svc.fetch_supplier_prices(4116984, {111, 222})
        self.assertEqual(set(found.keys()), {111, 222})
        # все цели найдены на первой странице — вторая не запрашивается
        self.assertEqual(session.get.call_count, 1)

    def test_exact_prices_use_one_bounded_card_v4_request(self):
        session = MagicMock()
        session.get.return_value = _resp(200, {'products': [
            dict(SEARCH_PRODUCT, id=111),
            dict(SEARCH_PRODUCT, id=222),
            dict(SEARCH_PRODUCT, id=999),
        ]})
        svc, _, _ = self._service(session)

        found = svc.fetch_exact_prices([111, 222, 111])

        self.assertEqual(set(found), {111, 222})
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(session.get.call_args.args[0], cf.CARD_URL)
        self.assertEqual(session.get.call_args.kwargs['params']['nm'], '111;222')

    def test_exact_prices_reject_invalid_or_oversized_batches(self):
        session = MagicMock()
        svc, _, _ = self._service(session)
        with self.assertRaises(ValueError):
            svc.fetch_exact_prices([True])
        with self.assertRaises(ValueError):
            svc.fetch_exact_prices(
                list(range(1, cf.EXACT_CARD_BATCH_LIMIT + 2)),
            )
        session.get.assert_not_called()

    def test_supplier_prices_429_stops_without_sleep(self):
        session = MagicMock()
        session.get.return_value = _resp(429)
        svc, _, health = self._service(session)
        found = svc.fetch_supplier_prices(4116984, {111})
        self.assertEqual(found, {})
        self.assertEqual(session.get.call_count, 1)

    def test_source_in_cooldown_skipped(self):
        session = MagicMock()
        svc, _, health = self._service(session)
        for _ in range(3):
            health.record_failure(cf.SOURCE_CATALOG)
        found = svc.fetch_supplier_prices(4116984, {111})
        self.assertEqual(found, {})
        session.get.assert_not_called()


class GlobalLimiterTest(unittest.TestCase):
    def test_env_clamped(self):
        with patch.dict(cf.os.environ, {'COMPETITOR_PUBLIC_RPM': '500'}):
            self.assertEqual(cf._resolve_public_rpm(), 60)
        with patch.dict(cf.os.environ, {'COMPETITOR_PUBLIC_RPM': '0'}):
            self.assertEqual(cf._resolve_public_rpm(), 1)
        with patch.dict(cf.os.environ, {'COMPETITOR_PUBLIC_RPM': 'мусор'}):
            self.assertEqual(cf._resolve_public_rpm(), 20)


if __name__ == '__main__':
    unittest.main()
