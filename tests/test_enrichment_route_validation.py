# -*- coding: utf-8 -*-
"""Route safety: invalid WB characteristics block later photo side effects."""

import os
import unittest
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

    def test_invalid_characteristics_block_selective_photo_upload(self):
        seller = MagicMock()
        seller.id = 7
        seller.wb_api_key = 'test-key'
        seller.has_valid_api_key.return_value = True
        user = MagicMock(is_authenticated=True, seller=seller)
        product = SimpleNamespace(id=11, seller_id=7)
        imported = SimpleNamespace(id=22)
        service = MagicMock()
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
            product_model.query.get_or_404.return_value = product
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

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('словаре WB', response.get_json()['error'])
        service.apply_selective_photos.assert_not_called()


if __name__ == '__main__':
    unittest.main()
