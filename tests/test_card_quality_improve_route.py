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

    def test_improve_agents_offline_returns_supplier_diff(self):
        user = _user()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail') as mock_detail, \
             patch('routes.card_quality.collect_weak_dimensions', return_value=['photos', 'description']), \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.agent_service') as mock_as:
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_detail.return_value = {'dimensions': {}}
            svc = MagicMock()
            svc.find_supplier_data.return_value = MagicMock()
            svc.build_preview.return_value = {'title': {'has_change': True}}
            mock_es.return_value = svc
            # агенты офлайн
            mock_as.get_agent_by_name.return_value = MagicMock(status='offline')

            resp = self.client.post('/api/card-quality/101/improve', json={})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['weak_dims'], ['photos', 'description'])
            self.assertIsNotNone(data['supplier_diff'])
            self.assertEqual(data['task_ids'], {})

    def test_improve_enqueues_online_agents(self):
        user = _user()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail', return_value={'dimensions': {}}), \
             patch('routes.card_quality.collect_weak_dimensions', return_value=[]), \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.agent_service') as mock_as:
            MockProduct.query.filter_by.return_value.first.return_value = product
            svc = MagicMock()
            svc.find_supplier_data.return_value = None
            mock_es.return_value = svc
            online = MagicMock()
            online.status = 'online'
            online.id = 'aid'
            mock_as.get_agent_by_name.return_value = online
            task = MagicMock()
            task.id = 'tid-1'
            mock_as.create_task.return_value = task

            resp = self.client.post('/api/card-quality/101/improve', json={})
            data = resp.get_json()
            self.assertIn('photo-optimizer', data['task_ids'])
            self.assertIn('card-doctor', data['task_ids'])
            self.assertIsNone(data['supplier_diff'])

    def test_proposal_maps_completed_tasks(self):
        user = _user()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        completed = MagicMock()
        completed.status = 'completed'
        completed.get_result.return_value = {'recommended_order': [2, 0, 1]}
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.AgentTask') as MockTask, \
             patch('routes.card_quality.build_proposal_from_tasks') as mock_build, \
             patch('routes.card_quality.get_enrichment_service') as mock_es:
            MockProduct.query.filter_by.return_value.first.return_value = product
            MockTask.query.filter_by.return_value.first.return_value = completed
            mock_es.return_value.find_supplier_data.return_value = None
            mock_build.return_value = {'photos': {'proposed': []}}
            resp = self.client.post('/api/card-quality/101/proposal',
                                    json={'task_ids': {'photo-optimizer': 'tid-1'}})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn('photos', data['proposal'])
            # build_proposal_from_tasks получил список {agent, result}
            passed = mock_build.call_args.args[1]
            self.assertEqual(passed[0]['agent'], 'photo-optimizer')
            self.assertEqual(passed[0]['result'], {'recommended_order': [2, 0, 1]})

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
            resp = self.client.post('/api/card-quality/101/proposal',
                                    json={'task_ids': {}})
            self.assertEqual(resp.status_code, 403)

    def test_proposal_skips_incomplete_tasks(self):
        user = _user()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555
        incomplete = MagicMock()
        incomplete.status = 'running'
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.AgentTask') as MockTask, \
             patch('routes.card_quality.build_proposal_from_tasks') as mock_build, \
             patch('routes.card_quality.get_enrichment_service') as mock_es:
            MockProduct.query.filter_by.return_value.first.return_value = product
            MockTask.query.filter_by.return_value.first.return_value = incomplete
            mock_es.return_value.find_supplier_data.return_value = None
            mock_build.return_value = {}
            resp = self.client.post('/api/card-quality/101/proposal',
                                    json={'task_ids': {'card-doctor': 'tid-2'}})
            self.assertEqual(resp.status_code, 200)
            # Незавершённая задача не попала в task_results
            passed = mock_build.call_args.args[1]
            self.assertEqual(passed, [])


    def test_improve_enqueues_generative_agents_for_weak_dims(self):
        """Генеративные агенты ставятся в очередь в propose-mode для слабых измерений."""
        user = _user()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555

        # Счётчик для уникальных id задач
        call_counter = {'n': 0}

        def make_task(*args, **kwargs):
            call_counter['n'] += 1
            t = MagicMock()
            t.id = f'tid-{call_counter["n"]}'
            return t

        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail', return_value={'dimensions': {}}), \
             patch('routes.card_quality.collect_weak_dimensions',
                   return_value=['title', 'brand', 'category']), \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.agent_service') as mock_as:

            MockProduct.query.filter_by.return_value.first.return_value = product
            svc = MagicMock()
            svc.find_supplier_data.return_value = None
            mock_es.return_value = svc

            online = MagicMock()
            online.status = 'online'
            online.id = 'agent-id'
            mock_as.get_agent_by_name.return_value = online
            mock_as.create_task.side_effect = make_task

            resp = self.client.post('/api/card-quality/101/improve', json={})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            task_ids = data['task_ids']

            # Диагностические агенты всегда присутствуют (если online)
            self.assertIn('photo-optimizer', task_ids)
            self.assertIn('card-doctor', task_ids)

            # Генеративные агенты для слабых измерений
            self.assertIn('seo-writer', task_ids)       # title слабый
            self.assertIn('brand-resolver', task_ids)   # brand слабый
            self.assertIn('category-mapper', task_ids)  # category слабый

            # characteristics не слабый → characteristics-filler не запускается
            self.assertNotIn('characteristics-filler', task_ids)

            # seo-writer вызван с mode='propose'
            seo_calls = [
                c for c in mock_as.create_task.call_args_list
                if c.kwargs.get('agent_id') == 'agent-id'
                and c.kwargs.get('task_type') == 'seo_single'
            ]
            self.assertEqual(len(seo_calls), 1)
            self.assertEqual(seo_calls[0].kwargs['input_data']['mode'], 'propose')

            # seo-writer вызван ровно один раз (title и description не дублируют)
            all_types = [c.kwargs.get('task_type') for c in mock_as.create_task.call_args_list]
            self.assertEqual(all_types.count('seo_single'), 1)


if __name__ == '__main__':
    unittest.main()
