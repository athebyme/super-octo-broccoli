# -*- coding: utf-8 -*-
"""The legacy card improver must never bypass preserve-live enrichment."""

import unittest

from services.card_improver import (
    LEGACY_WRITE_DISABLED,
    apply_card_updates,
)


class _Product:
    id = 101
    quality_score = 42.0
    title = 'Live projection'
    brand = 'Manual brand'
    photos_json = '["manual.jpg"]'
    subject_id = 10


class _ExplodingClient:
    def __getattr__(self, name):
        raise AssertionError(f'legacy shim must not access WB client: {name}')


class LegacyCardImproverFailClosedTest(unittest.TestCase):
    def test_content_write_is_blocked_without_mutation(self):
        product = _Product()
        result = apply_card_updates(
            product,
            {'title': 'Candidate', 'description': 'Candidate description'},
            object(),
            _ExplodingClient(),
        )

        self.assertFalse(result['success'])
        self.assertFalse(result['wb_sync'])
        self.assertEqual(result['fields_applied'], [])
        self.assertEqual(result['error'], LEGACY_WRITE_DISABLED)
        self.assertEqual(product.title, 'Live projection')
        self.assertEqual(result['old_quality'], 42.0)
        self.assertEqual(result['new_quality'], 42.0)

    def test_photo_and_subject_write_are_blocked_without_mutation(self):
        product = _Product()
        result = apply_card_updates(
            product,
            {'photos': ['replacement.jpg'], 'subject_id': 99},
            object(),
            _ExplodingClient(),
        )

        self.assertFalse(result['success'])
        self.assertEqual(product.photos_json, '["manual.jpg"]')
        self.assertEqual(product.subject_id, 10)

    def test_unknown_or_empty_payload_is_still_fail_closed(self):
        result = apply_card_updates(
            _Product(), {'unknown': 'value'}, object(), _ExplodingClient(),
        )
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], LEGACY_WRITE_DISABLED)


if __name__ == '__main__':
    unittest.main()
