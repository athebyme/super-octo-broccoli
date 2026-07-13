# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import Mock

from agents.catalog.brand_resolver import BrandResolverAgent
from agents.platform_client import PlatformClient


class BrandReferenceContractTestCase(unittest.TestCase):
    def test_brand_resolver_blocks_stale_category_before_any_llm_call(self):
        for entity_kind in ('imported', 'product'):
            with self.subTest(entity_kind=entity_kind):
                platform = Mock()
                platform.preflight_brand_categories.return_value = {
                    'reference_status': {
                        'source': 'wb_brands',
                        'usable': False,
                        'reason': 'stale_cache',
                    },
                    'results': [{
                        'category_id': 101,
                        'reference_status': {
                            'source': 'wb_brands',
                            'usable': False,
                            'stale': True,
                            'reason': 'stale_cache',
                        },
                    }],
                }
                platform.get_imported_products_brief.return_value = [{
                    'id': 1, 'wb_subject_id': 101, 'title': 'Test',
                }]
                platform.get_product.return_value = {
                    'id': 1, 'subject_id': 101, 'title': 'Test',
                }
                resolver = BrandResolverAgent.__new__(BrandResolverAgent)
                resolver.platform = platform
                resolver.llm = Mock()

                input_data = (
                    {'imported_product_id': 1}
                    if entity_kind == 'imported'
                    else {'product_id': 1}
                )
                result = resolver.execute_task({
                    'id': 'task',
                    'seller_id': 7,
                    'task_type': 'resolve_single',
                    'input_data': json.dumps(input_data),
                })

                self.assertEqual(result['status'], 'needs_clarification')
                self.assertTrue(result['reference_data_blocked'])
                self.assertEqual(result['_usage'].get('api_requests', 0), 0)
                resolver.llm.chat.assert_not_called()
                resolver.llm.structured_output.assert_not_called()

    def test_brand_preflight_covers_products_after_first_brief_page(self):
        platform = Mock()
        platform.get_imported_products_brief.side_effect = lambda ids: [
            {
                'id': product_id,
                'wb_subject_id': 102 if product_id == 51 else 101,
                'title': f'Test {product_id}',
            }
            for product_id in ids
        ]
        platform.preflight_brand_categories.return_value = {
            'reference_status': {'source': 'wb_brands', 'usable': True},
            'results': [
                {
                    'category_id': 101,
                    'reference_status': {'source': 'wb_brands', 'usable': True},
                },
                {
                    'category_id': 102,
                    'reference_status': {
                        'source': 'wb_brands', 'usable': False,
                        'stale': True, 'reason': 'stale_cache',
                    },
                },
            ],
        }
        resolver = BrandResolverAgent.__new__(BrandResolverAgent)
        resolver.platform = platform
        resolver.llm = Mock()
        product_ids = list(range(1, 52))

        result = resolver.execute_task({
            'id': 'task',
            'seller_id': 7,
            'task_type': 'resolve_batch',
            'input_data': json.dumps({'imported_product_ids': product_ids}),
        })

        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(
            platform.get_imported_products_brief.call_args_list[0].args[0],
            list(range(1, 51)),
        )
        self.assertEqual(
            platform.get_imported_products_brief.call_args_list[1].args[0],
            [51],
        )
        platform.preflight_brand_categories.assert_called_once_with([101, 102])
        resolver.llm.structured_output.assert_not_called()

    def test_missing_selected_product_stops_before_preflight_and_llm(self):
        platform = Mock()
        platform.get_imported_products_brief.return_value = []
        resolver = BrandResolverAgent.__new__(BrandResolverAgent)
        resolver.platform = platform
        resolver.llm = Mock()

        result = resolver.execute_task({
            'id': 'task',
            'seller_id': 7,
            'task_type': 'resolve_single',
            'input_data': json.dumps({'imported_product_id': 999}),
        })

        self.assertEqual(result['status'], 'needs_clarification')
        self.assertTrue(result['selection_data_blocked'])
        self.assertFalse(result['reference_data_blocked'])
        self.assertEqual(result['_usage'].get('api_requests', 0), 0)
        platform.preflight_brand_categories.assert_not_called()
        resolver.llm.chat.assert_not_called()
        resolver.llm.structured_output.assert_not_called()

    def test_platform_client_validates_typed_brand_preflight(self):
        platform = PlatformClient.__new__(PlatformClient)
        platform._request = Mock(return_value={
            'reference_status': {'source': 'wb_brands', 'usable': True},
            'results': [{
                'category_id': 101,
                'reference_status': {'source': 'wb_brands', 'usable': True},
            }],
            'count': 1,
        })

        result = platform.preflight_brand_categories([101])

        self.assertEqual(result['count'], 1)
        platform._request.assert_called_once_with(
            'POST', '/brands/preflight', json={'category_ids': [101]},
        )
        with self.assertRaisesRegex(ValueError, '1..100'):
            platform.preflight_brand_categories([])

    def test_platform_client_unwraps_brand_result_and_batch_uses_canonical_name(self):
        platform = PlatformClient.__new__(PlatformClient)
        single_payload = {
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
        }
        batch_payload = {
            'results': [{
                'product_id': 1,
                'status': 'found',
                'brand_name': 'Lelo',
                'marketplace_brand_name': 'LELO',
                'marketplace_brand_id': 42,
                'confidence': 1.0,
                'category_available': True,
                'reference_status': {
                    'source': 'wb_brands',
                    'usable': True,
                    'stale': False,
                },
            }],
            'count': 1,
        }
        platform._request = Mock(side_effect=[single_payload, batch_payload])
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
        self.assertEqual(platform._request.call_args_list[0].args, (
            'GET', '/brands/validate',
        ))
        self.assertEqual(platform._request.call_args_list[1].args, (
            'POST', '/brands/validate-batch',
        ))
        self.assertEqual(platform._request.call_args_list[1].kwargs['json'], {
            'items': [{
                'product_id': 1,
                'brand': 'lelo',
                'category_id': 101,
            }],
        })

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
        platform.validate_brands.assert_not_called()

    def test_no_brand_sentinel_uses_the_same_exact_category_validation(self):
        platform = Mock()
        platform.validate_brands.return_value = [{
            'product_id': 1,
            'status': 'not_found',
            'reference_status': {'usable': True},
        }]
        resolver = BrandResolverAgent.__new__(BrandResolverAgent)
        resolver.platform = platform
        resolver._structured_subject_by_product = {1: 101}

        results = resolver._postprocess_structured_results([
            {'product_id': 1, 'brand': 'Нет бренда'},
        ])

        platform.validate_brands.assert_called_once_with([{
            'product_id': 1,
            'brand': 'Нет бренда',
            'category_id': 101,
        }])
        self.assertIsNone(results[0]['brand'])
        self.assertEqual(
            results[0]['error'], 'brand_not_registered_for_category',
        )

    def test_structured_brand_results_reject_duplicate_missing_and_foreign_ids(self):
        invalid_results = {
            'duplicate': [
                {'product_id': 1, 'brand': 'Brand A'},
                {'product_id': 1, 'brand': 'Brand A'},
            ],
            'missing': [
                {'product_id': 1, 'brand': 'Brand A'},
            ],
            'foreign': [
                {'product_id': 1, 'brand': 'Brand A'},
                {'product_id': 3, 'brand': 'Brand C'},
            ],
        }
        for case, results in invalid_results.items():
            with self.subTest(case=case):
                platform = Mock()
                resolver = BrandResolverAgent.__new__(BrandResolverAgent)
                resolver.platform = platform
                resolver._structured_subject_by_product = {1: 101, 2: 102}

                with self.assertRaisesRegex(
                    ValueError, 'do not match the current chunk',
                ):
                    resolver._postprocess_structured_results(results)

                platform.validate_brands.assert_not_called()

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
