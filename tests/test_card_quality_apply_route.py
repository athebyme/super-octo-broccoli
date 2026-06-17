# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch, MagicMock


class ApplyRouteTest(unittest.TestCase):
    def setUp(self):
        import os
        os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-unit-tests')
        os.environ.setdefault('DISABLE_SECURE_COOKIE', '1')
        import seller_platform as app_module
        self.app = app_module.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def _login_ctx(self, has_key=True, product=None):
        seller = MagicMock()
        seller.id = 7
        seller.has_valid_api_key.return_value = has_key
        user = MagicMock()
        user.is_authenticated = True
        user.seller = seller
        return user, seller

    def test_apply_success(self):
        user, seller = self._login_ctx()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555

        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.WildberriesAPIClient') as MockWB, \
             patch('routes.card_quality.apply_card_updates') as mock_apply:
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_apply.return_value = {
                'success': True, 'fields_applied': ['title'],
                'old_quality': 40.0, 'new_quality': 62.0, 'wb_sync': True, 'error': None,
            }
            resp = self.client.post('/api/card-quality/101/apply',
                                    json={'updates': {'title': 'Новый заголовок'}})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data['success'])
            self.assertEqual(data['fields_applied'], ['title'])
            self.assertEqual(data['new_quality'], 62.0)
            self.assertEqual(data['old_quality'], 40.0)
            self.assertTrue(data['wb_sync'])
            # apply_card_updates получил только whitelisted поля
            called_updates = mock_apply.call_args.args[1] if mock_apply.call_args.args else mock_apply.call_args.kwargs['updates']
            self.assertIn('title', called_updates)

    def test_apply_rejects_when_no_api_key(self):
        user, seller = self._login_ctx(has_key=False)
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user):
            resp = self.client.post('/api/card-quality/101/apply',
                                    json={'updates': {'title': 'x'}})
            self.assertEqual(resp.status_code, 403)
            self.assertIn('ключ', resp.get_json().get('error', '').lower())

    def test_apply_returns_422_on_failure(self):
        user, seller = self._login_ctx()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555

        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.WildberriesAPIClient'), \
             patch('routes.card_quality.apply_card_updates') as mock_apply:
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_apply.return_value = {
                'success': False, 'fields_applied': [],
                'old_quality': 40.0, 'new_quality': 40.0, 'wb_sync': False, 'error': 'WB rejected',
            }
            resp = self.client.post('/api/card-quality/101/apply',
                                    json={'updates': {'title': 'Новый заголовок'}})
            self.assertEqual(resp.status_code, 422)
            data = resp.get_json()
            self.assertEqual(data['error'], 'WB rejected')

    def test_apply_returns_500_on_exception(self):
        user, seller = self._login_ctx()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555

        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.WildberriesAPIClient'), \
             patch('routes.card_quality.apply_card_updates') as mock_apply:
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_apply.side_effect = RuntimeError('boom')
            resp = self.client.post('/api/card-quality/101/apply',
                                    json={'updates': {'title': 'Новый заголовок'}})
            self.assertEqual(resp.status_code, 500)

    def test_apply_filters_unknown_fields(self):
        user, seller = self._login_ctx()
        product = MagicMock(); product.id = 101; product.nm_id = 555
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.WildberriesAPIClient'), \
             patch('routes.card_quality.apply_card_updates') as mock_apply:
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_apply.return_value = {'success': True, 'fields_applied': ['brand'],
                                       'old_quality': 1, 'new_quality': 2, 'wb_sync': True, 'error': None}
            self.client.post('/api/card-quality/101/apply',
                             json={'updates': {'brand': 'Nike', 'hacker': 'drop table'}})
            called_updates = mock_apply.call_args.args[1] if mock_apply.call_args.args else mock_apply.call_args.kwargs['updates']
            self.assertIn('brand', called_updates)
            self.assertNotIn('hacker', called_updates)

    def test_apply_returns_400_on_empty_updates(self):
        user, seller = self._login_ctx()
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct:
            product = MagicMock(); product.id = 101
            MockProduct.query.filter_by.return_value.first.return_value = product
            resp = self.client.post('/api/card-quality/101/apply',
                                    json={'updates': {}})
            self.assertEqual(resp.status_code, 400)

    def test_apply_returns_400_on_only_disallowed_fields(self):
        user, seller = self._login_ctx()
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct:
            product = MagicMock(); product.id = 101
            MockProduct.query.filter_by.return_value.first.return_value = product
            resp = self.client.post('/api/card-quality/101/apply',
                                    json={'updates': {'evil_field': 'val', 'another_bad': 'x'}})
            self.assertEqual(resp.status_code, 400)

    def test_apply_returns_404_when_product_not_found(self):
        user, seller = self._login_ctx()
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct:
            MockProduct.query.filter_by.return_value.first.return_value = None
            resp = self.client.post('/api/card-quality/999/apply',
                                    json={'updates': {'title': 'test'}})
            self.assertEqual(resp.status_code, 404)


if __name__ == '__main__':
    unittest.main()
