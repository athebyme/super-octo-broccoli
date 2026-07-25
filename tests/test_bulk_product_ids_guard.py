# -*- coding: utf-8 -*-
"""Массовые действия обязаны иметь границы запроса.

Инвариант проекта: bulk-эндпоинт принимает не более 200 уникальных
positive integer ID. До этого у отправки на WB, удаления и быстрой проверки
не было ни лимита, ни валидации типов — клиент мог прислать что угодно.
"""

import unittest

from routes.suppliers import _bulk_product_ids, MAX_BULK_PRODUCTS


class BulkProductIdsGuardTest(unittest.TestCase):
    def test_accepts_unique_positive_integers(self):
        self.assertEqual(_bulk_product_ids([3, 1, 2]), [3, 1, 2])

    def test_rejects_empty_and_non_list(self):
        for value in ([], None, 'abc', {}, 0):
            with self.assertRaises(ValueError):
                _bulk_product_ids(value)

    def test_rejects_over_limit(self):
        ids = list(range(1, MAX_BULK_PRODUCTS + 2))
        with self.assertRaises(ValueError) as ctx:
            _bulk_product_ids(ids)
        self.assertIn(str(MAX_BULK_PRODUCTS), str(ctx.exception))

    def test_rejects_duplicates(self):
        with self.assertRaises(ValueError):
            _bulk_product_ids([5, 5])

    def test_rejects_bool_float_and_string_ids(self):
        for value in (True, 1.5, '7', -3, 0):
            with self.assertRaises(ValueError):
                _bulk_product_ids([value])

    def test_custom_limit_is_enforced(self):
        with self.assertRaises(ValueError):
            _bulk_product_ids([1, 2, 3], limit=2)


if __name__ == '__main__':
    unittest.main()
