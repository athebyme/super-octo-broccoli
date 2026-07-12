# -*- coding: utf-8 -*-
"""Тесты потокобезопасного TTL-кеша (services/ttl_cache.py).

Требования: single-flight (нет stampede-гонок), префиксная инвалидация,
ошибки загрузчика не кешируются.
"""

import threading
import time
import unittest


class TestTTLCache(unittest.TestCase):
    def setUp(self):
        from services.ttl_cache import TTLCache
        self.cache = TTLCache()

    def test_caches_value_within_ttl(self):
        calls = []
        loader = lambda: calls.append(1) or 'value'
        self.assertEqual(self.cache.get_or_load('k', 60, loader), 'value')
        self.assertEqual(self.cache.get_or_load('k', 60, loader), 'value')
        self.assertEqual(len(calls), 1)

    def test_expires_after_ttl(self):
        calls = []
        loader = lambda: calls.append(1) or len(calls)
        self.assertEqual(self.cache.get_or_load('k', 0.05, loader), 1)
        time.sleep(0.08)
        self.assertEqual(self.cache.get_or_load('k', 0.05, loader), 2)

    def test_none_is_a_cacheable_value(self):
        calls = []
        loader = lambda: calls.append(1)
        self.assertIsNone(self.cache.get_or_load('k', 60, loader))
        self.assertIsNone(self.cache.get_or_load('k', 60, loader))
        self.assertEqual(len(calls), 1)

    def test_single_flight_no_stampede(self):
        calls = []

        def slow_loader():
            calls.append(1)
            time.sleep(0.1)
            return 'v'

        results = []
        threads = [threading.Thread(
            target=lambda: results.append(self.cache.get_or_load('k', 60, slow_loader)))
            for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, ['v'] * 8)
        self.assertEqual(len(calls), 1)  # загрузчик выполнился ровно один раз

    def test_invalidate_prefix(self):
        self.cache.get_or_load('a:1', 60, lambda: 'x')
        self.cache.get_or_load('a:2', 60, lambda: 'y')
        self.cache.get_or_load('b:1', 60, lambda: 'z')
        self.cache.invalidate('a:')
        calls = []
        self.assertEqual(self.cache.get_or_load('a:1', 60, lambda: calls.append(1) or 'x2'), 'x2')
        self.assertEqual(self.cache.get_or_load('b:1', 60, lambda: 'never'), 'z')

    def test_loader_exception_not_cached(self):
        calls = []

        def failing():
            calls.append(1)
            raise RuntimeError('boom')

        with self.assertRaises(RuntimeError):
            self.cache.get_or_load('k', 60, failing)
        with self.assertRaises(RuntimeError):
            self.cache.get_or_load('k', 60, failing)
        self.assertEqual(len(calls), 2)
        # после ошибок ключ остаётся рабочим
        self.assertEqual(self.cache.get_or_load('k', 60, lambda: 'ok'), 'ok')

    def test_clear(self):
        self.cache.get_or_load('k', 60, lambda: 'v')
        self.cache.clear()
        calls = []
        self.assertEqual(self.cache.get_or_load('k', 60, lambda: calls.append(1) or 'v2'), 'v2')


if __name__ == '__main__':
    unittest.main()
