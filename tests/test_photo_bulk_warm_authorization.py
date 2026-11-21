"""Authorization boundary for the explicit full supplier-photo warm."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from flask_wtf.csrf import CSRFProtect

from routes.photos import register_photo_routes


class PhotoBulkWarmAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='photo-bulk-warm-test',
            WTF_CSRF_ENABLED=False,
        )
        CSRFProtect(self.app)
        register_photo_routes(self.app)
        self.client = self.app.test_client()

    @staticmethod
    def _user(*, admin):
        return SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_admin=admin,
        )

    def test_seller_cannot_start_full_warm_or_probe_supplier(self):
        user = self._user(admin=False)
        supplier_model = SimpleNamespace(query=SimpleNamespace(
            get=MagicMock(),
        ))
        with patch('flask_login.utils._get_user', return_value=user), \
             patch('models.Supplier', supplier_model), \
             patch(
                 'services.photo_cache.bulk_download_supplier_photos',
             ) as warm:
            response = self.client.post('/api/photos/download-all/17')

        self.assertEqual(response.status_code, 403)
        supplier_model.query.get.assert_not_called()
        warm.assert_not_called()

    def test_admin_warms_only_the_exact_existing_supplier_and_keeps_payload(self):
        user = self._user(admin=True)
        supplier = SimpleNamespace(id=17)
        supplier_model = SimpleNamespace(query=SimpleNamespace(
            get=MagicMock(return_value=supplier),
        ))
        result = {
            'total_photos': 6,
            'already_cached': 2,
            'queued': 4,
            'errors': [],
        }
        with patch('flask_login.utils._get_user', return_value=user), \
             patch('models.Supplier', supplier_model), \
             patch(
                 'services.photo_cache.bulk_download_supplier_photos',
                 return_value=result,
             ) as warm:
            response = self.client.post('/api/photos/download-all/17')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'success': True, **result})
        supplier_model.query.get.assert_called_once_with(17)
        warm.assert_called_once_with(supplier.id)

    def test_admin_gets_json_not_found_without_starting_warm(self):
        user = self._user(admin=True)
        supplier_model = SimpleNamespace(query=SimpleNamespace(
            get=MagicMock(return_value=None),
        ))
        with patch('flask_login.utils._get_user', return_value=user), \
             patch('models.Supplier', supplier_model), \
             patch(
                 'services.photo_cache.bulk_download_supplier_photos',
             ) as warm:
            response = self.client.post('/api/photos/download-all/999')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {
            'success': False,
            'error': 'supplier_not_found',
        })
        supplier_model.query.get.assert_called_once_with(999)
        warm.assert_not_called()

    def test_full_warm_is_post_only(self):
        with patch(
            'services.photo_cache.bulk_download_supplier_photos',
        ) as warm:
            response = self.client.get('/api/photos/download-all/17')

        self.assertEqual(response.status_code, 405)
        warm.assert_not_called()

    def test_full_warm_retains_global_csrf_protection(self):
        self.app.config['WTF_CSRF_ENABLED'] = True
        user = self._user(admin=True)
        with patch('flask_login.utils._get_user', return_value=user), \
             patch(
                 'services.photo_cache.bulk_download_supplier_photos',
             ) as warm:
            response = self.client.post('/api/photos/download-all/17')

        self.assertEqual(response.status_code, 400)
        warm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
