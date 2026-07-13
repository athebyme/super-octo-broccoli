# -*- coding: utf-8 -*-
"""Тесты парсера sales-funnel: рейтинги + метрики воронки."""
import unittest

from services.product_sync_scheduler import parse_sales_funnel_metrics


def _resp(products):
    return {'data': {'products': products}}


class TestParseSalesFunnelMetrics(unittest.TestCase):
    def test_full_item(self):
        resp = _resp([{
            'product': {'nmId': 42, 'productRating': 9.3, 'feedbackRating': 5},
            'statistics': {'selectedPeriod': {
                'openCardCount': 1200, 'ordersCount': 40,
                'conversions': {'addToCartPercent': 6.5,
                                'cartToOrderPercent': 40.0,
                                'buyoutsPercent': 55.0},
            }},
        }])
        out = parse_sales_funnel_metrics(resp)
        self.assertEqual(out[42], {
            'product_rating': 9.3, 'feedback_rating': 5,
            'views': 1200, 'orders': 40,
            'cart_conv': 6.5, 'order_conv': 40.0, 'buyout_rate': 55.0,
        })

    def test_missing_statistics(self):
        out = parse_sales_funnel_metrics(_resp([
            {'product': {'nmId': 7, 'productRating': 8.0, 'feedbackRating': 4.5}},
        ]))
        self.assertEqual(out[7]['product_rating'], 8.0)
        self.assertIsNone(out[7]['views'])
        self.assertIsNone(out[7]['cart_conv'])

    def test_empty_and_garbage(self):
        self.assertEqual(parse_sales_funnel_metrics({}), {})
        self.assertEqual(parse_sales_funnel_metrics({'data': {}}), {})
        self.assertEqual(parse_sales_funnel_metrics(_resp([{}, 'мусор', {'product': {}}])), {})

    def test_nmid_alt_key(self):
        out = parse_sales_funnel_metrics(_resp([{'product': {'nmID': 11}}]))
        self.assertIn(11, out)
