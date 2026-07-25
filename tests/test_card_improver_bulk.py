# -*- coding: utf-8 -*-
"""Regression: the legacy batch helper is a provider-write-free shim."""

import unittest

from services.card_improver import (
    LEGACY_WRITE_DISABLED,
    apply_card_updates_bulk,
)


class LegacyBulkWriteDisabledTest(unittest.TestCase):
    def test_every_row_fails_closed_without_batch_or_media_calls(self):
        class Product:
            def __init__(self, product_id, title):
                self.id = product_id
                self.title = title
                self.quality_score = 20.0

        class Client:
            def __getattr__(self, name):
                raise AssertionError(
                    f'legacy bulk shim must not access WB client: {name}'
                )

        first = Product(1, 'One')
        second = Product(2, 'Two')
        results = apply_card_updates_bulk(
            [
                (first, {'title': 'Replacement'}),
                (second, {'photos': ['replacement.jpg']}),
            ],
            object(),
            Client(),
        )

        self.assertEqual(set(results), {1, 2})
        self.assertTrue(all(not row['success'] for row in results.values()))
        self.assertTrue(all(
            row['error'] == LEGACY_WRITE_DISABLED
            for row in results.values()
        ))
        self.assertEqual(first.title, 'One')
        self.assertEqual(second.title, 'Two')


if __name__ == '__main__':
    unittest.main()
