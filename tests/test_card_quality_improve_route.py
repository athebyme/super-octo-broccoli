# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch, MagicMock


def _user(has_key=True):
    seller = MagicMock()
    seller.id = 7
    seller.has_valid_api_key.return_value = has_key
    u = MagicMock()
    u.is_authenticated = True
    u.seller = seller
    return u


class ImproveProposalRouteTest(unittest.TestCase):
    def setUp(self):
        import os
        os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-unit-tests')
        os.environ.setdefault('DISABLE_SECURE_COOKIE', '1')
        import seller_platform as app_module
        self.app = app_module.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def test_improve_returns_weak_dims_and_supplier_diff_without_task_ids(self):
        user = _user()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail') as mock_detail, \
             patch('routes.card_quality.collect_weak_dimensions', return_value=['photos', 'description']), \
             patch('routes.card_quality.get_enrichment_service') as mock_es:
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_detail.return_value = {'dimensions': {}}
            svc = MagicMock()
            svc.find_supplier_data.return_value = MagicMock()
            svc.build_preview.return_value = {'title': {'has_change': True}}
            mock_es.return_value = svc

            resp = self.client.post('/api/card-quality/101/improve', json={})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['weak_dims'], ['photos', 'description'])
            self.assertIsNotNone(data['supplier_diff'])
            self.assertNotIn('task_ids', data)
            self.assertEqual(set(data.keys()), {'success', 'weak_dims', 'supplier_diff'})

    def test_improve_without_supplier_data_returns_none_diff(self):
        user = _user()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail', return_value={'dimensions': {}}), \
             patch('routes.card_quality.collect_weak_dimensions', return_value=[]), \
             patch('routes.card_quality.get_enrichment_service') as mock_es:
            MockProduct.query.filter_by.return_value.first.return_value = product
            svc = MagicMock()
            svc.find_supplier_data.return_value = None
            mock_es.return_value = svc

            resp = self.client.post('/api/card-quality/101/improve', json={})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['weak_dims'], [])
            self.assertIsNone(data['supplier_diff'])
            self.assertNotIn('task_ids', data)

    def test_ai_analyze_endpoint_removed(self):
        """Legacy /ai-analyze endpoint удалён вместе со спец-агентами — маршрут больше не существует."""
        resp = self.client.post('/api/card-quality/101/ai-analyze', json={})
        self.assertEqual(resp.status_code, 404)

    def test_proposal_without_body_returns_standard_photos_proposal(self):
        user = _user()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        product.photos_json = '["own1"]'
        product.subject_id = 10
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.build_proposal_from_tasks') as mock_build, \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.get_standard_media', return_value={'photos': ['std1', 'std2']}), \
             patch('routes.card_quality.get_min_photos', return_value=5), \
             patch('routes.card_quality.compose_card_photo_urls',
                   return_value=['own1', 'std1', 'std2']):
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_build.return_value = {}
            mock_es.return_value.find_supplier_data.return_value = None

            # Запрос без тела вообще (task_ids больше не читаются из body)
            resp = self.client.post('/api/card-quality/101/proposal')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['proposal']['photos']['source'], 'standard-photos')
            self.assertEqual(data['proposal']['photos']['proposed'], ['own1', 'std1', 'std2'])
            # build_proposal_from_tasks вызван с пустым task_results
            self.assertEqual(mock_build.call_args.args[1], [])

    def test_proposal_ignores_task_ids_in_body(self):
        """task_ids в body больше не читаются — build_proposal_from_tasks всегда получает []."""
        user = _user()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        product.photos_json = None
        product.subject_id = 10
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.build_proposal_from_tasks') as mock_build, \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.get_standard_media', return_value={}), \
             patch('routes.card_quality.compose_card_photo_urls', return_value=[]):
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_build.return_value = {}
            mock_es.return_value.find_supplier_data.return_value = None

            resp = self.client.post('/api/card-quality/101/proposal',
                                    json={'task_ids': {'card-doctor': 'tid-999'}})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(mock_build.call_args.args[1], [])

    def test_improve_returns_403_without_api_key(self):
        user = _user(has_key=False)
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user):
            resp = self.client.post('/api/card-quality/101/improve', json={})
            self.assertEqual(resp.status_code, 403)

    def test_improve_returns_404_when_product_not_found(self):
        user = _user()
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct:
            MockProduct.query.filter_by.return_value.first.return_value = None
            resp = self.client.post('/api/card-quality/999/improve', json={})
            self.assertEqual(resp.status_code, 404)

    def test_proposal_returns_403_without_api_key(self):
        user = _user(has_key=False)
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user):
            resp = self.client.post('/api/card-quality/101/proposal', json={})
            self.assertEqual(resp.status_code, 403)


if __name__ == '__main__':
    unittest.main()
