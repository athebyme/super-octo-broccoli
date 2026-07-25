# -*- coding: utf-8 -*-
"""Route safety: invalid WB characteristics block later photo side effects."""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class EnrichmentRouteValidationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-unit-tests')
        os.environ.setdefault('DISABLE_SECURE_COOKIE', '1')
        import seller_platform
        cls.app = seller_platform.app
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        self.http = self.app.test_client()

    def test_bulk_ids_are_strict_bounded_unique(self):
        from routes.enrichment import _bounded_unique_product_ids

        self.assertEqual(_bounded_unique_product_ids([3, 1]), [3, 1])
        with self.assertRaises(ValueError):
            _bounded_unique_product_ids([3, 1, 3])
        for invalid in ([True], ['1'], [0], [-1], '1'):
            with self.assertRaises(ValueError):
                _bounded_unique_product_ids(invalid)
        with self.assertRaises(ValueError):
            _bounded_unique_product_ids(list(range(1, 202)))

    def test_bulk_photo_confirmation_defaults_to_photos_only_refresh(self):
        template = (
            Path(__file__).resolve().parents[1]
            / 'templates'
            / 'products_enrich_bulk.html'
        ).read_text(encoding='utf-8')

        self.assertIn('photos: true', template)
        for field in (
            'title', 'description', 'characteristics', 'dimensions', 'brand'
        ):
            self.assertIn(f'{field}: false', template)
        self.assertIn("photoStrategy: 'smart_merge'", template)
        self.assertIn('Ручные фото и порядок сохраняются', template)
        self.assertIn('Подтвердить обновление фото', template)

    def test_invalid_characteristics_block_selective_photo_upload(self):
        seller = MagicMock()
        seller.id = 7
        seller.wb_api_key = 'test-key'
        seller.has_valid_api_key.return_value = True
        user = MagicMock(is_authenticated=True, seller=seller)
        product = SimpleNamespace(id=11, seller_id=7)
        imported = SimpleNamespace(id=22)
        service = MagicMock()
        service._photo_source.return_value = ([{'original': 'https://x/0.jpg'}], 'x', '22')
        service._photo_url.side_effect = lambda value: value.get('original')
        service.apply_enrichment.return_value = {
            'success': False,
            'fields_applied': [],
            'photos': {'skipped': True},
            'error': 'Материал отсутствует в словаре WB',
            'wb_sync': False,
        }

        with (
            patch('routes.enrichment.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.enrichment.Product') as product_model,
            patch('routes.enrichment.ImportedProduct') as imported_model,
            patch(
                'routes.enrichment.get_enrichment_service',
                return_value=service,
            ),
            patch(
                'services.wb_api_client.WildberriesAPIClient',
                return_value=MagicMock(),
            ),
        ):
            product_model.query.filter_by.return_value.first_or_404.return_value = product
            imported_model.query.filter_by.return_value.first.return_value = imported
            response = self.http.post(
                '/api/products/11/enrich/apply',
                json={
                    'fields': ['characteristics', 'photos'],
                    'photo_indices': [0],
                    'photo_strategy': 'append',
                    'supplier_id': 22,
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('словаре WB', response.get_json()['error'])
        self.assertEqual(
            service.apply_enrichment.call_args.args[2],
            ['characteristics'],
        )
        self.assertEqual(
            response.get_json()['photos']['reason'],
            'content_update_not_settled',
        )
        service.apply_selective_photos.assert_not_called()
        product_model.query.filter_by.assert_called_once_with(id=11, seller_id=7)
        imported_model.query.filter_by.assert_called_once_with(id=22, seller_id=7)

    def test_unknown_photo_strategy_is_rejected_before_supplier_resolution(self):
        seller = MagicMock()
        seller.id = 7
        seller.has_valid_api_key.return_value = True
        user = MagicMock(is_authenticated=True, seller=seller)
        product = SimpleNamespace(id=11, seller_id=7)

        with (
            patch('routes.enrichment.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.enrichment.Product') as product_model,
            patch(
                'routes.enrichment.get_enrichment_service',
            ) as service_factory,
        ):
            product_model.query.filter_by.return_value.first_or_404.return_value = product
            response = self.http.post(
                '/api/products/11/enrich/apply',
                json={
                    'fields': ['photos'],
                    'photo_strategy': 'destructive_replace',
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'Invalid photo_strategy')
        service_factory.assert_not_called()

    def test_selective_photo_lock_wait_is_reported_as_deferred_not_sent(self):
        seller = MagicMock()
        seller.id = 7
        seller.wb_api_key = 'test-key'
        seller.has_valid_api_key.return_value = True
        user = MagicMock(is_authenticated=True, seller=seller)
        product = SimpleNamespace(id=11, seller_id=7)
        imported = SimpleNamespace(id=22)
        service = MagicMock()
        service._photo_source.return_value = ([{'original': 'https://x/0.jpg'}], 'x', '22')
        service._photo_url.side_effect = lambda value: value.get('original')
        service.apply_enrichment.return_value = {
            'success': True,
            'fields_applied': [],
            'photos': {'skipped': True, 'reason': 'selective_mode'},
            'error': None,
            'wb_sync': False,
            'reconciliation_pending': False,
        }
        service.apply_selective_photos.return_value = {
            'success': False,
            'uploaded': 0,
            'skipped': True,
            'reason': 'media_operation_busy',
            'error': 'Другая операция с фото ещё выполняется',
            'deferred': True,
            'reconciliation_pending': True,
        }

        with (
            patch('routes.enrichment.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.enrichment.Product') as product_model,
            patch('routes.enrichment.ImportedProduct') as imported_model,
            patch(
                'routes.enrichment.get_enrichment_service',
                return_value=service,
            ),
            patch(
                'services.wb_api_client.WildberriesAPIClient',
                return_value=MagicMock(),
            ),
        ):
            product_model.query.filter_by.return_value.first_or_404.return_value = product
            imported_model.query.filter_by.return_value.first.return_value = imported
            response = self.http.post(
                '/api/products/11/enrich/apply',
                json={
                    'fields': ['photos'],
                    'photo_indices': [0],
                    'photo_strategy': 'smart_merge',
                    'supplier_id': 22,
                },
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 202)
        self.assertTrue(payload['deferred'])
        self.assertTrue(payload['reconciliation_pending'])
        self.assertFalse(payload['success'])
        self.assertEqual(payload['fields_pending'], ['photos'])
        service.apply_enrichment.assert_not_called()
        self.assertEqual(
            service.apply_selective_photos.call_args.kwargs[
                'expected_source_urls'
            ],
            ['https://x/0.jpg'],
        )

    def test_uncertain_selective_content_does_not_claim_photos_are_pending(self):
        seller = MagicMock()
        seller.id = 7
        seller.wb_api_key = 'test-key'
        seller.has_valid_api_key.return_value = True
        user = MagicMock(is_authenticated=True, seller=seller)
        product = SimpleNamespace(id=11, seller_id=7)
        imported = SimpleNamespace(id=22)
        service = MagicMock()
        service._photo_source.return_value = (
            [{'original': 'https://x/0.jpg'}], 'x', '22',
        )
        service._photo_url.side_effect = lambda value: value.get('original')
        service.apply_enrichment.return_value = {
            'success': False,
            'fields_applied': [],
            'error': 'Исход content write уточняется',
            'wb_sync': False,
            'reconciliation_pending': True,
            'fields_pending': ['title'],
            'deferred': False,
        }

        with (
            patch('routes.enrichment.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.enrichment.Product') as product_model,
            patch('routes.enrichment.ImportedProduct') as imported_model,
            patch(
                'routes.enrichment.get_enrichment_service',
                return_value=service,
            ),
            patch(
                'services.wb_api_client.WildberriesAPIClient',
                return_value=MagicMock(),
            ),
        ):
            product_model.query.filter_by.return_value.first_or_404.return_value = product
            imported_model.query.filter_by.return_value.first.return_value = imported
            response = self.http.post(
                '/api/products/11/enrich/apply',
                json={
                    'fields': ['title', 'photos'],
                    'photo_indices': [0],
                    'photo_strategy': 'smart_merge',
                    'supplier_id': 22,
                },
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload['fields_pending'], ['title'])
        self.assertEqual(
            payload['photos']['reason'], 'content_update_not_settled'
        )
        self.assertTrue(payload['photos']['definitely_not_sent'])
        self.assertEqual(
            service.apply_enrichment.call_args.args[2], ['title']
        )
        service.apply_selective_photos.assert_not_called()

    def test_invalid_selective_indices_are_rejected_before_content_write(self):
        seller = MagicMock()
        seller.id = 7
        seller.wb_api_key = 'test-key'
        seller.has_valid_api_key.return_value = True
        user = MagicMock(is_authenticated=True, seller=seller)
        product = SimpleNamespace(id=11, seller_id=7)
        imported = SimpleNamespace(id=22)
        service = MagicMock()

        with (
            patch('routes.enrichment.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.enrichment.Product') as product_model,
            patch('routes.enrichment.ImportedProduct') as imported_model,
            patch(
                'routes.enrichment.get_enrichment_service',
                return_value=service,
            ),
            patch(
                'services.wb_api_client.WildberriesAPIClient',
            ) as wb_client,
        ):
            product_model.query.filter_by.return_value.first_or_404.return_value = product
            imported_model.query.filter_by.return_value.first.return_value = imported
            response = self.http.post(
                '/api/products/11/enrich/apply',
                json={
                    'fields': ['title', 'photos'],
                    'photo_indices': [True],
                    'photo_strategy': 'smart_merge',
                    'supplier_id': 22,
                },
            )

        self.assertEqual(response.status_code, 400)
        service.apply_enrichment.assert_not_called()
        service.apply_selective_photos.assert_not_called()
        wb_client.assert_not_called()

    def test_out_of_range_selective_index_is_rejected_before_content_write(self):
        seller = MagicMock()
        seller.id = 7
        seller.wb_api_key = 'test-key'
        seller.has_valid_api_key.return_value = True
        user = MagicMock(is_authenticated=True, seller=seller)
        product = SimpleNamespace(id=11, seller_id=7)
        imported = SimpleNamespace(id=22)
        service = MagicMock()
        service._photo_source.return_value = (
            [{'original': 'https://x/0.jpg'}], 'x', '22',
        )

        with (
            patch('routes.enrichment.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.enrichment.Product') as product_model,
            patch('routes.enrichment.ImportedProduct') as imported_model,
            patch(
                'routes.enrichment.get_enrichment_service',
                return_value=service,
            ),
            patch(
                'services.wb_api_client.WildberriesAPIClient',
            ) as wb_client,
        ):
            product_model.query.filter_by.return_value.first_or_404.return_value = product
            imported_model.query.filter_by.return_value.first.return_value = imported
            response = self.http.post(
                '/api/products/11/enrich/apply',
                json={
                    'fields': ['title', 'photos'],
                    'photo_indices': [1],
                    'photo_strategy': 'smart_merge',
                    'supplier_id': 22,
                },
            )

        self.assertEqual(response.status_code, 400)
        service.apply_enrichment.assert_not_called()
        service.apply_selective_photos.assert_not_called()
        wb_client.assert_not_called()

    def test_cross_tenant_supplier_id_is_not_resolved(self):
        seller = MagicMock()
        seller.id = 7
        seller.wb_api_key = 'test-key'
        seller.has_valid_api_key.return_value = True
        user = MagicMock(is_authenticated=True, seller=seller)
        product = SimpleNamespace(id=11, seller_id=7)
        service = MagicMock()

        with (
            patch('routes.enrichment.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.enrichment.Product') as product_model,
            patch('routes.enrichment.ImportedProduct') as imported_model,
            patch(
                'routes.enrichment.get_enrichment_service',
                return_value=service,
            ),
            patch('services.wb_api_client.WildberriesAPIClient') as wb_client,
        ):
            product_model.query.filter_by.return_value.first_or_404.return_value = product
            imported_model.query.filter_by.return_value.first.return_value = None
            response = self.http.post(
                '/api/products/11/enrich/apply',
                json={
                    'fields': ['characteristics'],
                    'supplier_id': 22,
                },
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error'], 'Supplier data not found')
        imported_model.query.filter_by.assert_called_once_with(id=22, seller_id=7)
        service.apply_enrichment.assert_not_called()
        wb_client.assert_not_called()

    def test_legacy_reupload_photos_uses_preserve_live_service(self):
        seller = MagicMock()
        seller.id = 7
        seller.wb_api_key = 'test-key'
        seller.has_valid_api_key.return_value = True
        user = MagicMock(is_authenticated=True, seller=seller)
        imported = SimpleNamespace(
            id=22,
            seller_id=7,
            import_status='imported',
            product_id=11,
        )
        product = SimpleNamespace(id=11, seller_id=7, nm_id=5011)
        service = MagicMock()
        service.apply_enrichment.return_value = {
            'success': True,
            'fields_applied': ['photos'],
            'photos': {'uploaded': 1},
            'wb_sync': True,
            'reconciliation_pending': True,
            'deferred': False,
        }
        client = MagicMock()

        with (
            patch('routes.suppliers.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.suppliers.ImportedProduct') as imported_model,
            patch('routes.suppliers.Product') as product_model,
            patch(
                'services.supplier_enrichment.get_enrichment_service',
                return_value=service,
            ),
            patch(
                'services.wb_api_client.WildberriesAPIClient',
                return_value=client,
            ),
        ):
            imported_model.query.filter_by.return_value.first_or_404.return_value = imported
            product_model.query.filter_by.return_value.first.return_value = product
            response = self.http.post(
                '/my-products/22/wb-reupload-photos',
            )

        self.assertEqual(response.status_code, 202)
        imported_model.query.filter_by.assert_called_once_with(
            id=22, seller_id=7,
        )
        product_model.query.filter_by.assert_called_once_with(
            id=11, seller_id=7,
        )
        self.assertEqual(service.apply_enrichment.call_args.args[2:4], (
            ['photos'], 'smart_merge',
        ))
        client.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
