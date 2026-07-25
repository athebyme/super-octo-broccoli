# -*- coding: utf-8 -*-
"""Card-quality writes must route through preserve-live supplier enrichment."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class ApplyRouteTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-unit-tests')
        os.environ.setdefault('DISABLE_SECURE_COOKIE', '1')
        import seller_platform as app_module

        self.app = app_module.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    @staticmethod
    def _user(has_key=True):
        seller = MagicMock()
        seller.id = 7
        seller.wb_api_key = 'key'
        seller.has_valid_api_key.return_value = has_key
        user = MagicMock()
        user.is_authenticated = True
        user.seller = seller
        return user, seller

    @staticmethod
    def _product():
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        product.quality_score = 40.0
        return product

    def test_apply_uses_only_field_names_and_preserve_live_service(self):
        user, seller = self._user()
        product = self._product()
        imported = object()
        service = MagicMock()
        service.find_supplier_data.return_value = imported
        service.apply_enrichment.return_value = {
            'success': True,
            'fields_applied': ['title'],
            'fields_pending': ['title'],
            'wb_sync': True,
            'wb_confirmed': False,
            'reconciliation_pending': True,
            'error': None,
        }
        wb_client = MagicMock()

        with (
            patch('routes.card_quality.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.card_quality.Product') as product_model,
            patch(
                'routes.card_quality.get_enrichment_service',
                return_value=service,
            ),
            patch(
                'routes.card_quality.WildberriesAPIClient',
                return_value=wb_client,
            ),
        ):
            product_model.query.filter_by.return_value.first.return_value = product
            response = self.client.post(
                '/api/card-quality/101/apply',
                json={'updates': {'title': 'browser value is not trusted'}},
            )

        self.assertEqual(response.status_code, 202)
        data = response.get_json()
        self.assertTrue(data['reconciliation_pending'])
        service.apply_enrichment.assert_called_once_with(
            product,
            imported,
            ['title'],
            'smart_merge',
            seller,
            wb_client,
        )
        wb_client.close.assert_called_once_with()

    def test_apply_accepts_explicit_fields_contract(self):
        user, seller = self._user()
        product = self._product()
        service = MagicMock()
        service.find_supplier_data.return_value = object()
        service.apply_enrichment.return_value = {
            'success': True,
            'fields_applied': [],
            'wb_sync': False,
            'wb_confirmed': False,
            'reconciliation_pending': False,
            'error': None,
        }

        with (
            patch('routes.card_quality.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.card_quality.Product') as product_model,
            patch(
                'routes.card_quality.get_enrichment_service',
                return_value=service,
            ),
            patch('routes.card_quality.WildberriesAPIClient'),
        ):
            product_model.query.filter_by.return_value.first.return_value = product
            response = self.client.post(
                '/api/card-quality/101/apply',
                json={'fields': ['description', 'photos']},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            service.apply_enrichment.call_args.args[2],
            ['description', 'photos'],
        )

    def test_unknown_field_fails_closed(self):
        user, _seller = self._user()
        product = self._product()
        service = MagicMock()

        with (
            patch('routes.card_quality.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.card_quality.Product') as product_model,
            patch(
                'routes.card_quality.get_enrichment_service',
                return_value=service,
            ),
        ):
            product_model.query.filter_by.return_value.first.return_value = product
            response = self.client.post(
                '/api/card-quality/101/apply',
                json={'updates': {'title': 'x', 'unknown': 'y'}},
            )

        self.assertEqual(response.status_code, 400)
        service.find_supplier_data.assert_not_called()

    def test_missing_supplier_source_blocks_before_client(self):
        user, _seller = self._user()
        product = self._product()
        service = MagicMock()
        service.find_supplier_data.return_value = None

        with (
            patch('routes.card_quality.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.card_quality.Product') as product_model,
            patch(
                'routes.card_quality.get_enrichment_service',
                return_value=service,
            ),
            patch('routes.card_quality.WildberriesAPIClient') as client_factory,
        ):
            product_model.query.filter_by.return_value.first.return_value = product
            response = self.client.post(
                '/api/card-quality/101/apply',
                json={'fields': ['title']},
            )

        self.assertEqual(response.status_code, 409)
        client_factory.assert_not_called()

    def test_service_failure_returns_422_and_closes_client(self):
        user, _seller = self._user()
        product = self._product()
        service = MagicMock()
        service.find_supplier_data.return_value = object()
        service.apply_enrichment.return_value = {
            'success': False,
            'fields_applied': [],
            'wb_sync': False,
            'reconciliation_pending': False,
            'error': 'live drift',
        }
        wb_client = MagicMock()

        with (
            patch('routes.card_quality.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.card_quality.Product') as product_model,
            patch(
                'routes.card_quality.get_enrichment_service',
                return_value=service,
            ),
            patch(
                'routes.card_quality.WildberriesAPIClient',
                return_value=wb_client,
            ),
        ):
            product_model.query.filter_by.return_value.first.return_value = product
            response = self.client.post(
                '/api/card-quality/101/apply',
                json={'fields': ['title']},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()['error'], 'live drift')
        wb_client.close.assert_called_once_with()

    def test_auth_empty_and_not_found_contracts(self):
        user, _seller = self._user(has_key=False)
        with (
            patch('routes.card_quality.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
        ):
            response = self.client.post(
                '/api/card-quality/101/apply', json={'fields': ['title']},
            )
        self.assertEqual(response.status_code, 403)

        user, _seller = self._user()
        with (
            patch('routes.card_quality.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.card_quality.Product') as product_model,
        ):
            product_model.query.filter_by.return_value.first.return_value = None
            response = self.client.post(
                '/api/card-quality/999/apply', json={'fields': ['title']},
            )
        self.assertEqual(response.status_code, 404)

    def test_bulk_review_preserves_fields_per_product(self):
        user, seller = self._user()
        service = MagicMock()
        service.start_bulk_enrichment.return_value = '12345678-job'

        with (
            patch('routes.card_quality.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.card_quality.Product') as product_model,
            patch(
                'routes.card_quality.get_enrichment_service',
                return_value=service,
            ),
        ):
            product_model.query.filter.return_value.all.return_value = [
                SimpleNamespace(id=101), SimpleNamespace(id=202),
            ]
            response = self.client.post(
                '/card-quality/bulk-improve',
                data={
                    'action': 'confirm',
                    'apply_101_title': 'on',
                    'apply_202_description': 'on',
                },
            )

        self.assertEqual(response.status_code, 302)
        service.start_bulk_enrichment.assert_called_once_with(
            [101, 202],
            ['title', 'description'],
            'smart_merge',
            seller,
            fields_by_product={101: ['title'], 202: ['description']},
        )

    def test_standard_photo_bulk_uses_durable_enrichment(self):
        user, seller = self._user()
        service = MagicMock()
        service.start_bulk_enrichment.return_value = '12345678-job'

        with (
            patch('routes.card_quality.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
            patch('routes.card_quality.Product') as product_model,
            patch(
                'routes.card_quality.get_enrichment_service',
                return_value=service,
            ),
        ):
            product_model.query.filter.return_value.all.return_value = [
                SimpleNamespace(id=101),
            ]
            response = self.client.post(
                '/card-quality/standard-photos-bulk/apply',
                data={'action': 'confirm', 'product_101': 'on'},
            )

        self.assertEqual(response.status_code, 302)
        service.start_bulk_enrichment.assert_called_once_with(
            [101], ['photos'], 'smart_merge', seller,
        )


if __name__ == '__main__':
    unittest.main()
