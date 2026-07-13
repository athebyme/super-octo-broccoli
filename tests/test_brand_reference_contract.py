# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock

from agents.catalog.brand_resolver import BrandResolverAgent
from agents.platform_client import PlatformClient


class BrandReferenceContractTestCase(unittest.TestCase):
    def test_platform_client_unwraps_brand_result_and_batch_uses_canonical_name(self):
        platform = PlatformClient.__new__(PlatformClient)
        platform._request = Mock(return_value={
            'result': {
                'status': 'found',
                'brand_name': 'Lelo',
                'marketplace_brand_name': 'LELO',
                'marketplace_brand_id': 42,
                'confidence': 1.0,
                'category_available': True,
            },
            'reference_status': {
                'source': 'wb_brands',
                'usable': True,
                'stale': False,
            },
        })
        resolver = BrandResolverAgent.__new__(BrandResolverAgent)
        resolver.platform = platform
        resolver._structured_subject_by_product = {1: 101}

        check = platform.validate_brand('lelo')
        results = resolver._postprocess_structured_results([
            {'product_id': 1, 'brand': 'lelo'},
        ])

        self.assertEqual(check['status'], 'found')
        self.assertNotIn('result', check)
        self.assertEqual(results, [{'product_id': 1, 'brand': 'LELO'}])
        platform._request.assert_called_with(
            'GET', '/brands/validate',
            params={'brand': 'lelo', 'category_id': 101},
        )

    def test_batch_brand_validation_skips_write_without_category_scope(self):
        platform = Mock()
        resolver = BrandResolverAgent.__new__(BrandResolverAgent)
        resolver.platform = platform
        resolver._structured_subject_by_product = {1: None}

        results = resolver._postprocess_structured_results([
            {'product_id': 1, 'brand': 'LELO'},
        ])

        self.assertIsNone(results[0]['brand'])
        self.assertEqual(results[0]['error'], 'category_scope_required')
        platform.validate_brand.assert_not_called()

    def test_platform_client_rejects_missing_brand_result_envelope(self):
        platform = PlatformClient.__new__(PlatformClient)
        platform._request = Mock(return_value={'status': 'found'})

        with self.assertRaisesRegex(ValueError, 'Invalid brand validation response'):
            platform.validate_brand('LELO')

    def test_platform_client_blocks_stale_brand_reference(self):
        platform = PlatformClient.__new__(PlatformClient)
        platform._request = Mock(return_value={
            'result': {
                'status': 'unavailable',
                'brand_name': None,
            },
            'reference_status': {
                'source': 'wb_brands',
                'usable': False,
                'reason': 'stale_cache',
            },
            'warning': 'Справочник брендов WB устарел',
        })

        with self.assertRaisesRegex(RuntimeError, 'устарел'):
            platform.validate_brand('LELO', category_id=101)


if __name__ == '__main__':
    unittest.main()
