# -*- coding: utf-8 -*-
"""Regression: card_improver cannot perform destructive gallery replace."""

import unittest

from services.card_improver import apply_card_updates


class LegacyPhotoWriteDisabledTest(unittest.TestCase):
    def test_media_save_method_is_never_called(self):
        class Product:
            id = 1
            quality_score = 10
            photos_json = '["manual-1.jpg", "manual-2.jpg"]'

        class Client:
            calls = 0

            def upload_photos_by_url(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError('media/save must not be called')

        product = Product()
        client = Client()
        result = apply_card_updates(
            product, {'photos': ['new.jpg']}, object(), client,
        )

        self.assertFalse(result['success'])
        self.assertEqual(client.calls, 0)
        self.assertEqual(
            product.photos_json,
            '["manual-1.jpg", "manual-2.jpg"]',
        )


if __name__ == '__main__':
    unittest.main()
