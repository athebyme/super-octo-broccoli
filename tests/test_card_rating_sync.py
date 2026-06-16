# -*- coding: utf-8 -*-
"""Тест парсинга ответа sales-funnel в карту рейтингов."""

import unittest

from services.product_sync_scheduler import parse_sales_funnel_ratings


class TestParseSalesFunnelRatings(unittest.TestCase):
    def test_extracts_ratings_keyed_by_nm_id(self):
        resp = {'data': {'products': [
            {'product': {'nmId': 105146863, 'productRating': 8, 'feedbackRating': 4.8}},
            {'product': {'nmId': 100142591, 'productRating': 6, 'feedbackRating': 0}},
        ]}}
        result = parse_sales_funnel_ratings(resp)
        self.assertEqual(result[105146863], {'product_rating': 8, 'feedback_rating': 4.8})
        self.assertEqual(result[100142591], {'product_rating': 6, 'feedback_rating': 0})

    def test_handles_missing_and_malformed(self):
        self.assertEqual(parse_sales_funnel_ratings({}), {})
        self.assertEqual(parse_sales_funnel_ratings({'data': {}}), {})
        self.assertEqual(parse_sales_funnel_ratings({'data': {'products': [{}]}}), {})

    def test_supports_nmID_alias(self):
        resp = {'data': {'products': [{'product': {'nmID': 42, 'productRating': 9.3, 'feedbackRating': 5}}]}}
        self.assertEqual(parse_sales_funnel_ratings(resp), {42: {'product_rating': 9.3, 'feedback_rating': 5}})
