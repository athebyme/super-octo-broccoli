# -*- coding: utf-8 -*-
"""Тесты роутов раздела «Обновление карточек» (supplier-updates)."""

import unittest
from unittest.mock import patch, MagicMock


class SupplierUpdatesRouteTest(unittest.TestCase):
    def setUp(self):
        import os
        os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-unit-tests')
        os.environ.setdefault('DISABLE_SECURE_COOKIE', '1')
        import seller_platform as app_module
        self.app = app_module.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def _login_ctx(self, has_key=True):
        seller = MagicMock()
        seller.id = 7
        seller.has_valid_api_key.return_value = has_key
        user = MagicMock()
        user.is_authenticated = True
        user.seller = seller
        return user, seller

    def _patches(self, user):
        return [
            patch('routes.supplier_updates.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
        ]

    def test_start_rejects_without_api_key(self):
        user, _ = self._login_ctx(has_key=False)
        with patch('routes.supplier_updates.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user):
            resp = self.client.post('/api/supplier-updates/photos/start',
                                    json={'product_ids': [1]})
        self.assertEqual(resp.status_code, 403)
        self.assertIn('ключ', resp.get_json()['error'].lower())

    def test_start_409_when_job_active(self):
        user, _ = self._login_ctx()
        active = MagicMock()
        active.job_uid = 'busy-job'
        with patch('routes.supplier_updates.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.supplier_updates.BackgroundJob') as MockJob:
            MockJob.query.filter_by.return_value.filter.return_value.first.return_value = active
            resp = self.client.post('/api/supplier-updates/photos/start',
                                    json={'product_ids': [1]})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['job_uid'], 'busy-job')

    def test_start_filters_foreign_ids_and_launches_thread(self):
        user, _ = self._login_ctx()
        own = MagicMock()
        own.id = 11
        with patch('routes.supplier_updates.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.supplier_updates.BackgroundJob') as MockJob, \
             patch('routes.supplier_updates.Product') as MockProduct, \
             patch('routes.supplier_updates.db'), \
             patch('routes.supplier_updates.threading.Thread') as MockThread:
            MockJob.query.filter_by.return_value.filter.return_value.first.return_value = None
            MockProduct.query.filter.return_value.all.return_value = [own]
            resp = self.client.post('/api/supplier-updates/photos/start',
                                    json={'product_ids': [11, 999]})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['total'], 1)
        MockThread.assert_called_once()
        # product_ids, переданные в поток, — только свои
        args = MockThread.call_args.kwargs.get('args') or MockThread.call_args.args
        self.assertEqual(args[3], [11])

    def test_start_400_when_nothing_to_do(self):
        user, _ = self._login_ctx()
        with patch('routes.supplier_updates.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.supplier_updates.BackgroundJob') as MockJob, \
             patch('routes.supplier_updates.Product') as MockProduct:
            MockJob.query.filter_by.return_value.filter.return_value.first.return_value = None
            MockProduct.query.filter.return_value.all.return_value = []
            resp = self.client.post('/api/supplier-updates/photos/start',
                                    json={'product_ids': [999]})
        self.assertEqual(resp.status_code, 400)

    def test_start_select_all_expands_filter(self):
        user, _ = self._login_ctx()
        with patch('routes.supplier_updates.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.supplier_updates.BackgroundJob') as MockJob, \
             patch('routes.supplier_updates.expand_filter_to_ids',
                   return_value=[1, 2, 3]) as mock_expand, \
             patch('routes.supplier_updates.db'), \
             patch('routes.supplier_updates.threading.Thread'):
            MockJob.query.filter_by.return_value.filter.return_value.first.return_value = None
            resp = self.client.post('/api/supplier-updates/photos/start',
                                    json={'select_all': True, 'supplier_id': 2,
                                          'only_new': True, 'search': ''})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['total'], 3)
        mock_expand.assert_called_once_with(7, supplier_id=2, only_new=True, search='')

    def test_status_scoped_to_seller(self):
        user, _ = self._login_ctx()
        with patch('routes.supplier_updates.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.supplier_updates.BackgroundJob') as MockJob:
            MockJob.query.filter_by.return_value.first.return_value = None
            resp = self.client.get('/api/supplier-updates/jobs/xyz/status')
            self.assertEqual(resp.status_code, 404)
            MockJob.query.filter_by.assert_called_with(job_uid='xyz', seller_id=7)

    def test_cancel_active_job(self):
        user, _ = self._login_ctx()
        job = MagicMock()
        job.status = 'running'
        with patch('routes.supplier_updates.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.supplier_updates.BackgroundJob') as MockJob, \
             patch('routes.supplier_updates.db'):
            MockJob.query.filter_by.return_value.first.return_value = job
            resp = self.client.post('/api/supplier-updates/jobs/xyz/cancel')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(job.status, 'cancelled')


if __name__ == '__main__':
    unittest.main()
