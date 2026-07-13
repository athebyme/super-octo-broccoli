# -*- coding: utf-8 -*-
"""Security and seller-context tests for the internal agent API."""
import unittest
from datetime import datetime, timedelta

from flask import Flask
from werkzeug.security import generate_password_hash

from models import (
    db, APILog, AgentChangeSnapshot, AgentConversation, AgentMessage, AgentTask,
    AutoImportSettings, ProductDefaults, CardEditHistory, ImportedProduct,
    Product, Seller, SellerSupplier,
    ServiceAgent, Supplier, User,
)
from routes.internal_api import internal_api_bp
from agents.platform_client import PlatformClient
from services.agent_harness import conversation_payload, rollback_task_tree, snapshot_count


class InternalAgentSecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.app.register_blueprint(internal_api_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user1 = User(
            username='seller-1', email='seller1@example.com', password_hash='x',
        )
        user2 = User(
            username='seller-2', email='seller2@example.com', password_hash='x',
        )
        db.session.add_all([user1, user2])
        db.session.flush()
        self.seller1 = Seller(user_id=user1.id, company_name='Seller One')
        self.seller1.wb_api_key = 'wb-secret-key'
        self.seller2 = Seller(user_id=user2.id, company_name='Seller Two')
        db.session.add_all([self.seller1, self.seller2])
        db.session.flush()

        self.agent1 = ServiceAgent(
            id='agent-1', name='agent-one', display_name='Agent One',
            api_key_hash=generate_password_hash('key-1'),
        )
        self.agent2 = ServiceAgent(
            id='agent-2', name='agent-two', display_name='Agent Two',
            api_key_hash=generate_password_hash('key-2'),
        )
        self.target = ServiceAgent(
            id='agent-target', name='seo-writer', display_name='SEO Writer',
            api_key_hash=generate_password_hash('target-key'),
        )
        db.session.add_all([self.agent1, self.agent2, self.target])
        db.session.flush()

        self.task1 = AgentTask(
            id='task-1', agent_id=self.agent1.id, seller_id=self.seller1.id,
            task_type='seo_single', title='Owned task', status='running',
        )
        self.task2 = AgentTask(
            id='task-2', agent_id=self.agent2.id, seller_id=self.seller2.id,
            task_type='seo_single', title='Foreign task', status='running',
        )
        db.session.add_all([self.task1, self.task2])
        db.session.add(AutoImportSettings(
            seller_id=self.seller1.id,
            ai_provider='deepseek',
            ai_api_key='seller-secret-ai-key',
            ai_api_base_url='https://api.deepseek.com/v1',
            ai_model='deepseek-v4-pro',
            agent_single_model=False,
        ))
        db.session.add(ProductDefaults(
            seller_id=self.seller1.id, rule_type='global',
            length_cm=10, width_cm=5, min_photos=4,
            default_characteristics='{"country":"RU"}',
        ))
        db.session.add(APILog(
            seller_id=self.seller1.id,
            endpoint='/api/v1/test?token=endpoint-secret', method='GET',
            status_code=401, success=False,
            request_body='{"api_key":"must-not-leak"}',
            response_body='{"token":"must-not-leak"}',
            error_message=(
                'Authorization: Bearer secret-token failed; '
                'api_key=second-secret'
            ),
        ))
        self.supplier = Supplier(name='Андрей (sex-opt.ru)', code='andrey')
        db.session.add(self.supplier)
        db.session.flush()
        db.session.add(SellerSupplier(
            seller_id=self.seller1.id, supplier_id=self.supplier.id, is_active=True,
        ))
        db.session.add_all([
            ImportedProduct(
                seller_id=self.seller1.id, supplier_id=self.supplier.id,
                title='Полная карточка', wb_subject_id=1, category_confidence=0.9,
                brand='Brand', description='x' * 150, photo_urls='["photo.jpg"]',
                characteristics='{"color":"red"}', supplier_price=100,
                supplier_quantity=5,
            ),
            ImportedProduct(
                seller_id=self.seller1.id, supplier_id=self.supplier.id,
                title='Проблемная карточка', description='', photo_urls='[]',
                characteristics='{}', supplier_quantity=0,
            ),
            ImportedProduct(
                seller_id=self.seller2.id, supplier_id=self.supplier.id,
                title='Чужая проблемная карточка', description='', photo_urls='[]',
                characteristics='{}', supplier_quantity=0,
            ),
        ])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @property
    def auth(self):
        return {'X-Agent-Id': 'agent-1', 'X-Agent-Key': 'key-1'}

    def task_headers(self, task_id='task-1'):
        return {**self.auth, 'X-Task-Id': task_id}

    def test_task_endpoints_hide_foreign_tasks(self):
        owned = self.client.get('/internal/v1/tasks/task-1', headers=self.auth)
        foreign = self.client.get('/internal/v1/tasks/task-2', headers=self.auth)
        foreign_step = self.client.get(
            '/internal/v1/tasks/task-2/steps', headers=self.auth,
        )

        self.assertEqual(owned.status_code, 200)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign_step.status_code, 404)

        mutations = (
            ('/internal/v1/tasks/task-2/start', {}),
            ('/internal/v1/tasks/task-2/progress', {'completed_steps': 1}),
            ('/internal/v1/tasks/task-2/complete', {'result': {}}),
            ('/internal/v1/tasks/task-2/fail', {'error': 'forbidden'}),
            ('/internal/v1/tasks/task-2/steps', {'title': 'forbidden'}),
        )
        for path, payload in mutations:
            with self.subTest(path=path):
                response = self.client.post(path, headers=self.auth, json=payload)
                self.assertEqual(response.status_code, 404)

    def test_task_mutations_return_compact_status_without_large_blobs(self):
        self.task1.input_data = '{"product_ids":[' + ','.join(['1'] * 500) + ']}'
        self.task1.checkpoint_json = '{"large":"' + ('x' * 5000) + '"}'
        self.task1.result_data = '{"large":"' + ('y' * 5000) + '"}'
        db.session.commit()

        response = self.client.post(
            '/internal/v1/tasks/task-1/progress',
            headers=self.auth,
            json={'completed_steps': 2, 'total_steps': 4, 'current_step_label': 'Проверка'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()['task']
        self.assertEqual(payload['id'], 'task-1')
        self.assertEqual(payload['progress_percent'], 50)
        self.assertNotIn('input_data', payload)
        self.assertNotIn('checkpoint', payload)
        self.assertNotIn('result', payload)
        self.assertLess(len(response.data), 1000)

    def test_seller_context_requires_owned_matching_task(self):
        path = f'/internal/v1/sellers/{self.seller1.id}'
        no_task = self.client.get(path, headers=self.auth)
        foreign_task = self.client.get(path, headers=self.task_headers('task-2'))
        owned = self.client.get(path, headers=self.task_headers())

        self.assertEqual(no_task.status_code, 403)
        self.assertEqual(foreign_task.status_code, 403)
        self.assertEqual(owned.status_code, 200)

    def test_task_ai_config_is_owned_and_contains_selected_profile(self):
        response = self.client.get(
            '/internal/v1/tasks/task-1/ai-config', headers=self.auth,
        )
        foreign = self.client.get(
            '/internal/v1/tasks/task-2/ai-config', headers=self.auth,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(foreign.status_code, 404)
        profile = response.get_json()['ai_config']
        self.assertEqual(profile, {
            'provider': 'deepseek',
            'model': 'deepseek-v4-pro',
            'key': 'seller-secret-ai-key',
            'base_url': 'https://api.deepseek.com/v1',
            'single_model': False,
        })

    def test_subtask_requires_owned_parent_and_same_seller(self):
        payload = {
            'agent_name': 'seo-writer', 'task_type': 'seo_single',
            'seller_id': self.seller1.id, 'title': 'Child',
            'parent_task_id': 'task-1', 'input_data': {'seller_id': self.seller1.id},
        }
        missing_header = self.client.post(
            '/internal/v1/tasks/create', headers=self.auth, json=payload,
        )
        wrong_seller = self.client.post(
            '/internal/v1/tasks/create', headers=self.task_headers(),
            json={**payload, 'seller_id': self.seller2.id},
        )
        created = self.client.post(
            '/internal/v1/tasks/create', headers=self.task_headers(), json=payload,
        )

        self.assertEqual(missing_header.status_code, 403)
        self.assertEqual(wrong_seller.status_code, 403)
        self.assertEqual(created.status_code, 200)
        child = created.get_json()['task']
        self.assertEqual(child['parent_task_id'], 'task-1')
        direct = self.client.get(
            f"/internal/v1/tasks/{child['id']}", headers=self.auth,
        )
        via_parent = self.client.get(
            f"/internal/v1/tasks/task-1/subtasks/{child['id']}", headers=self.auth,
        )
        self.assertEqual(direct.status_code, 404)
        self.assertEqual(via_parent.status_code, 200)

    def test_system_context_is_sanitized(self):
        base = f'/internal/v1/sellers/{self.seller1.id}'
        status = self.client.get(
            f'{base}/api-connection-status', headers=self.task_headers(),
        ).get_json()['connection']
        defaults = self.client.get(
            f'{base}/product-defaults', headers=self.task_headers(),
        ).get_json()['defaults']
        logs = self.client.get(
            f'{base}/api-logs?limit=500', headers=self.task_headers(),
        ).get_json()['logs']

        self.assertTrue(status['has_key'])
        self.assertEqual(status['mask'], '****')
        self.assertEqual(set(status), {'has_key', 'mask', 'status'})
        self.assertNotIn('wb-secret-key', str(status))
        self.assertEqual(defaults[0]['dimensions']['length'], 10)
        self.assertNotIn('global_media', defaults[0])
        self.assertNotIn('request_body', logs[0])
        self.assertNotIn('response_body', logs[0])
        self.assertNotIn('secret-token', str(logs[0]))
        self.assertNotIn('second-secret', str(logs[0]))
        self.assertNotIn('endpoint-secret', str(logs[0]))

    def test_named_supplier_audit_is_scoped_and_aggregated(self):
        resolve = self.client.get(
            f'/internal/v1/sellers/{self.seller1.id}/suppliers/resolve?q=андрея',
            headers=self.task_headers(),
        )
        self.assertEqual(resolve.status_code, 200)
        self.assertEqual(resolve.get_json()['suppliers'][0]['id'], self.supplier.id)

        response = self.client.get(
            f'/internal/v1/sellers/{self.seller1.id}/suppliers/'
            f'{self.supplier.id}/imported-audit?focus_limit=10',
            headers=self.task_headers(),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['total'], 2)
        self.assertEqual(payload['cards_with_issues'], 1)
        issue_codes = {item['code'] for item in payload['issue_summary']}
        self.assertIn('missing_category', issue_codes)
        self.assertIn('missing_photos', issue_codes)
        self.assertEqual(len(payload['focus_product_ids']), 1)

    def test_imported_catalog_filters_are_deterministic(self):
        missing = self.client.get(
            f'/internal/v1/sellers/{self.seller1.id}/imported-products/query'
            '?missing_field=description&limit=20',
            headers=self.task_headers(),
        )
        out_of_stock = self.client.get(
            f'/internal/v1/sellers/{self.seller1.id}/imported-products/query'
            '?stock_state=out_of_stock&limit=20',
            headers=self.task_headers(),
        )
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.get_json()['total'], 1)
        self.assertEqual(out_of_stock.get_json()['total'], 1)

        limited = self.client.get(
            f'/internal/v1/sellers/{self.seller1.id}/imported-products/query?limit=1',
            headers=self.task_headers(),
        ).get_json()
        self.assertEqual(limited['total'], 2)
        self.assertEqual(len(limited['products']), 1)
        self.assertTrue(limited['truncated'])

        invalid = self.client.get(
            f'/internal/v1/sellers/{self.seller1.id}/imported-products/query'
            '?missing_field=secret_field',
            headers=self.task_headers(),
        )
        self.assertEqual(invalid.status_code, 400)

    def test_selected_batch_audit_and_content_brief_are_typed_and_tenant_scoped(self):
        owned_imported = ImportedProduct.query.filter_by(
            seller_id=self.seller1.id, title='Проблемная карточка',
        ).one()
        foreign_imported = ImportedProduct.query.filter_by(seller_id=self.seller2.id).one()
        owned_product = Product(
            seller_id=self.seller1.id, nm_id=445566,
            title='Основная карточка', description='Описание WB',
        )
        db.session.add(owned_product)
        db.session.commit()

        audit = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/products/audit-batch',
            headers=self.task_headers(),
            json={
                'entity_kind': 'imported_product',
                'product_ids': [owned_imported.id],
                'focus_limit': 100,
            },
        )
        self.assertEqual(audit.status_code, 200)
        payload = audit.get_json()
        self.assertEqual(payload['total'], 1)
        self.assertEqual(payload['cards_with_issues'], 1)
        self.assertEqual(payload['products'][0]['id'], owned_imported.id)

        brief = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/products/content-brief',
            headers=self.task_headers(),
            json={
                'entity_kind': 'imported_product',
                'product_ids': [owned_imported.id],
            },
        )
        self.assertEqual(brief.status_code, 200)
        self.assertEqual(brief.get_json()['products'][0]['id'], owned_imported.id)
        self.assertIsNotNone(brief.get_json()['products'][0]['updated_at'])

        product_brief = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/products/content-brief',
            headers=self.task_headers(),
            json={
                'entity_kind': 'product',
                'product_ids': [owned_product.id],
            },
        )
        self.assertEqual(product_brief.status_code, 200)
        self.assertEqual(product_brief.get_json()['products'][0]['id'], owned_product.id)
        self.assertIsNotNone(product_brief.get_json()['products'][0]['updated_at'])

        mixed_scope = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/products/audit-batch',
            headers=self.task_headers(),
            json={
                'entity_kind': 'imported_product',
                'product_ids': [owned_imported.id, foreign_imported.id],
            },
        )
        self.assertEqual(mixed_scope.status_code, 409)
        self.assertNotIn('products', mixed_scope.get_json())

    def test_product_batch_update_is_scoped_audited_and_reports_unchanged(self):
        changed = Product(
            seller_id=self.seller1.id, nm_id=551001,
            title='До', description='Описание до',
        )
        unchanged = Product(
            seller_id=self.seller1.id, nm_id=551002,
            title='Без изменений', description='Стабильно',
        )
        db.session.add_all([changed, unchanged])
        db.session.commit()
        changed_version = changed.updated_at.isoformat()
        unchanged_version = unchanged.updated_at.isoformat()

        response = self.client.patch(
            f'/internal/v1/sellers/{self.seller1.id}/products/batch',
            headers=self.task_headers(),
            json={'updates': [
                {
                    'product_id': changed.id,
                    'title': 'После',
                    'description': 'Описание после',
                    'expected_updated_at': changed_version,
                },
                {
                    'product_id': unchanged.id,
                    'title': 'Без изменений',
                    'expected_updated_at': unchanged_version,
                },
            ]},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['updated'], 1)
        self.assertEqual(payload['unchanged'], 1)
        self.assertEqual(payload['failed'], 0)
        self.assertEqual(
            [item['status'] for item in payload['results']],
            ['updated', 'unchanged'],
        )
        db.session.refresh(changed)
        self.assertEqual(changed.title, 'После')
        self.assertEqual(changed.description, 'Описание после')
        history = CardEditHistory.query.filter_by(product_id=changed.id).one()
        self.assertEqual(history.changed_fields, ['title', 'description'])
        self.assertEqual(history.snapshot_before, {
            'title': 'До', 'description': 'Описание до',
        })
        self.assertEqual(history.snapshot_after, {
            'title': 'После', 'description': 'Описание после',
        })
        self.assertEqual(history.user_comment, 'agent_task:task-1')
        self.assertEqual(CardEditHistory.query.filter_by(product_id=unchanged.id).count(), 0)
        self.assertEqual(snapshot_count(self.task1.id), 1)

        self.task1.status = 'completed'
        db.session.commit()
        rollback = rollback_task_tree(self.task1.id, self.seller1.id)
        db.session.refresh(changed)
        self.assertEqual(rollback['snapshots'], 1)
        self.assertEqual(changed.title, 'До')
        self.assertEqual(changed.description, 'Описание до')

    def test_product_batch_rejects_conflicts_protected_fields_and_foreign_rows(self):
        stale = Product(
            seller_id=self.seller1.id, nm_id=552001,
            title='Ручная версия', description='Не менять',
        )
        protected = Product(
            seller_id=self.seller1.id, nm_id=552002,
            title='Цена защищена', price=100, quantity=3,
        )
        foreign = Product(
            seller_id=self.seller2.id, nm_id=552003,
            title='Чужая карточка',
        )
        db.session.add_all([stale, protected, foreign])
        db.session.commit()

        response = self.client.patch(
            f'/internal/v1/sellers/{self.seller1.id}/products/batch',
            headers=self.task_headers(),
            json={'updates': [
                {
                    'product_id': stale.id,
                    'description': 'Перезаписать',
                    'expected_updated_at': '2000-01-01T00:00:00',
                },
                {
                    'product_id': protected.id,
                    'title': 'Не применять вместе с ценой',
                    'price': 200,
                },
                {'product_id': foreign.id, 'title': 'Вторжение'},
            ]},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['updated'], 0)
        self.assertEqual(payload['failed'], 3)
        self.assertTrue(payload['results'][0]['conflict'])
        self.assertTrue(payload['results'][1]['requires_manual_review'])
        self.assertEqual(payload['results'][2]['error'], 'Product not found')
        db.session.refresh(stale)
        db.session.refresh(protected)
        db.session.refresh(foreign)
        self.assertEqual(stale.description, 'Не менять')
        self.assertEqual(protected.title, 'Цена защищена')
        self.assertEqual(float(protected.price), 100.0)
        self.assertEqual(foreign.title, 'Чужая карточка')
        self.assertEqual(CardEditHistory.query.filter(
            CardEditHistory.product_id.in_([stale.id, protected.id, foreign.id]),
        ).count(), 0)

        wrong_seller = self.client.patch(
            f'/internal/v1/sellers/{self.seller2.id}/products/batch',
            headers=self.task_headers(),
            json={'updates': [{'product_id': foreign.id, 'title': 'Нет'}]},
        )
        self.assertEqual(wrong_seller.status_code, 403)

        duplicate = self.client.patch(
            f'/internal/v1/sellers/{self.seller1.id}/products/batch',
            headers=self.task_headers(),
            json={'updates': [
                {'product_id': stale.id, 'title': 'A'},
                {'product_id': stale.id, 'title': 'B'},
            ]},
        )
        self.assertEqual(duplicate.status_code, 400)

        oversized = self.client.patch(
            f'/internal/v1/sellers/{self.seller1.id}/products/batch',
            headers=self.task_headers(),
            json={'updates': [
                {'product_id': index + 1, 'title': 'X'} for index in range(51)
            ]},
        )
        self.assertEqual(oversized.status_code, 400)

        invalid_id = self.client.patch(
            f'/internal/v1/sellers/{self.seller1.id}/products/batch',
            headers=self.task_headers(),
            json={'updates': [{'product_id': -1, 'title': 'X'}]},
        )
        self.assertEqual(invalid_id.status_code, 400)

    def test_typed_selection_endpoints_reject_oversized_or_duplicate_ids(self):
        paths = (
            '/products/content-brief',
            '/products/audit-batch',
        )
        for suffix in paths:
            with self.subTest(suffix=suffix, case='oversized'):
                response = self.client.post(
                    f'/internal/v1/sellers/{self.seller1.id}{suffix}',
                    headers=self.task_headers(),
                    json={
                        'entity_kind': 'product',
                        'product_ids': list(range(1, 202)),
                    },
                )
                self.assertEqual(response.status_code, 400)
            with self.subTest(suffix=suffix, case='duplicate'):
                response = self.client.post(
                    f'/internal/v1/sellers/{self.seller1.id}{suffix}',
                    headers=self.task_headers(),
                    json={
                        'entity_kind': 'product',
                        'product_ids': [1, 1],
                    },
                )
                self.assertEqual(response.status_code, 400)

    def test_imported_batch_rejects_stale_expected_updated_at(self):
        product = ImportedProduct.query.filter_by(
            seller_id=self.seller1.id, title='Полная карточка',
        ).one()
        brief = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/products/content-brief',
            headers=self.task_headers(),
            json={
                'entity_kind': 'imported_product',
                'product_ids': [product.id],
            },
        ).get_json()['products'][0]
        product.description = 'Ручное изменение после brief'
        product.updated_at = datetime.utcnow() + timedelta(seconds=1)
        db.session.commit()

        response = self.client.patch(
            '/internal/v1/imported-products/batch',
            headers=self.task_headers(),
            json={'updates': [{
                'product_id': product.id,
                'description': 'Ответ модели на старых данных',
                'expected_updated_at': brief['updated_at'],
            }]},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['updated'], 0)
        self.assertEqual(payload['failed'], 1)
        self.assertTrue(payload['results'][0]['conflict'])
        db.session.refresh(product)
        self.assertEqual(product.description, 'Ручное изменение после brief')
        self.assertEqual(AgentChangeSnapshot.query.filter_by(
            imported_product_id=product.id,
        ).count(), 0)

        current_version = product.updated_at.isoformat()
        success = self.client.patch(
            '/internal/v1/imported-products/batch',
            headers=self.task_headers(),
            json={'updates': [{
                'product_id': product.id,
                'description': 'Изменение на актуальной версии',
                'expected_updated_at': current_version,
            }]},
        )
        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.get_json()['results'][0]['status'], 'updated')
        db.session.refresh(product)
        self.assertEqual(product.description, 'Изменение на актуальной версии')
        snapshot = AgentChangeSnapshot.query.filter_by(
            imported_product_id=product.id,
        ).one()
        self.assertNotIn('expected_updated_at', snapshot.new_values)

    def test_platform_client_batch_helpers_never_clip_items(self):
        client = object.__new__(PlatformClient)
        calls = []

        def fake_request(method, path, **kwargs):
            payload = kwargs.get('json') or {}
            calls.append((method, path, payload))
            if path.endswith('/products/batch'):
                items = payload['updates']
                return {
                    'updated': len(items), 'unchanged': 0, 'failed': 0,
                    'results': [
                        {'product_id': item['product_id'], 'status': 'updated'}
                        for item in items
                    ],
                }
            if path == '/prohibited-words/check-batch':
                return {
                    'results': [
                        {'product_id': item['product_id'], 'field': item.get('field')}
                        for item in payload['items']
                    ],
                }
            return {'products': [], 'count': len(payload.get('product_ids') or [])}

        client._request = fake_request
        updates = [{'product_id': index, 'title': f'Title {index}'} for index in range(1, 52)]
        saved = client.batch_update_products(self.seller1.id, updates)
        self.assertEqual(saved['updated'], 51)
        write_calls = [call for call in calls if call[1].endswith('/products/batch')]
        self.assertEqual([len(call[2]['updates']) for call in write_calls], [50, 1])

        checks = [
            {'product_id': index, 'field': 'title', 'text': f'Text {index}'}
            for index in range(1, 52)
        ]
        checked = client.check_prohibited_words_batch(checks, self.seller1.id)
        self.assertEqual(checked['count'], 51)
        check_calls = [call for call in calls if call[1] == '/prohibited-words/check-batch']
        self.assertEqual([len(call[2]['items']) for call in check_calls], [50, 1])

        ids = list(range(1, 201))
        client.get_products_content_brief(self.seller1.id, 'product', ids)
        client.audit_product_batch(self.seller1.id, 'product', ids)
        selection_calls = [
            call for call in calls if call[1].endswith(('content-brief', 'audit-batch'))
        ]
        self.assertEqual([len(call[2]['product_ids']) for call in selection_calls], [200, 200])
        with self.assertRaises(ValueError):
            client.get_products_content_brief(
                self.seller1.id, 'product', list(range(1, 202)),
            )

    def test_prohibited_words_batch_is_one_tenant_scoped_request(self):
        response = self.client.post(
            '/internal/v1/prohibited-words/check-batch',
            headers=self.task_headers(),
            json={
                'seller_id': self.seller1.id,
                'items': [
                    {'product_id': 1, 'field': 'title', 'text': 'Clean text'},
                    {'product_id': 2, 'field': 'description', 'text': 'Fuck text'},
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['count'], 2)
        self.assertFalse(payload['results'][0]['has_prohibited'])
        self.assertTrue(payload['results'][1]['has_prohibited'])
        self.assertEqual(payload['results'][0]['field'], 'title')
        self.assertEqual(payload['results'][1]['field'], 'description')
        self.assertNotIn('fuck', payload['results'][1]['filtered_text'].lower())

        foreign = self.client.post(
            '/internal/v1/prohibited-words/check-batch',
            headers=self.task_headers(),
            json={
                'seller_id': self.seller2.id,
                'items': [{'product_id': 1, 'text': 'Text'}],
            },
        )
        self.assertEqual(foreign.status_code, 403)

    def test_wb_catalog_query_is_typed_and_seller_scoped(self):
        db.session.add_all([
            Product(seller_id=self.seller1.id, nm_id=111, title='Active', is_active=True),
            Product(seller_id=self.seller1.id, nm_id=222, title='Inactive', is_active=False),
            Product(seller_id=self.seller2.id, nm_id=333, title='Foreign', is_active=True),
        ])
        db.session.commit()
        response = self.client.get(
            f'/internal/v1/sellers/{self.seller1.id}/products/query?active=yes',
            headers=self.task_headers(),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['total'], 1)
        self.assertEqual(payload['products'][0]['title'], 'Active')

    def test_conversation_payload_returns_latest_messages_after_limit(self):
        conversation = AgentConversation(
            id='conversation-latest', seller_id=self.seller1.id,
            user_id=self.seller1.user_id, title='Long chat',
        )
        db.session.add(conversation)
        start = datetime(2026, 1, 1)
        db.session.add_all([
            AgentMessage(
                id=f'message-{index:03d}', conversation_id=conversation.id,
                role='user', content=f'Message {index}',
                created_at=start + timedelta(seconds=index),
                updated_at=start + timedelta(seconds=index),
            )
            for index in range(105)
        ])
        db.session.commit()

        messages = conversation_payload(conversation, message_limit=100)['messages']
        self.assertEqual(len(messages), 100)
        self.assertEqual(messages[0]['content'], 'Message 5')
        self.assertEqual(messages[-1]['content'], 'Message 104')

    def test_product_write_creates_agent_task_rollback_snapshot(self):
        product = Product(
            seller_id=self.seller1.id, nm_id=123456,
            title='Original', description='Before',
            characteristics_json='{"Материал":"Воск"}',
            sizes_json='[{"name":"one size"}]',
            photos_json='["one.jpg","two.jpg"]',
        )
        db.session.add(product)
        db.session.commit()

        response = self.client.patch(
            f'/internal/v1/sellers/{self.seller1.id}/products/{product.id}',
            headers=self.task_headers(), json={'description': 'After'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['changed'])
        self.assertEqual(response.get_json()['product']['photos_count'], 2)
        self.assertEqual(
            response.get_json()['product']['characteristics'], {'Материал': 'Воск'},
        )
        history = CardEditHistory.query.filter_by(product_id=product.id).one()
        self.assertEqual(history.snapshot_before, {'description': 'Before'})
        self.assertEqual(history.snapshot_after, {'description': 'After'})
        self.assertEqual(history.user_comment, 'agent_task:task-1')
        self.assertEqual(snapshot_count(self.task1.id), 1)

        self.task1.status = 'completed'
        db.session.commit()
        result = rollback_task_tree(self.task1.id, self.seller1.id)

        db.session.refresh(product)
        db.session.refresh(history)
        self.assertEqual(result['snapshots'], 1)
        self.assertEqual(product.description, 'Before')
        self.assertTrue(history.reverted)
        self.assertEqual(snapshot_count(self.task1.id), 0)

    def test_product_price_and_stock_require_manual_review(self):
        product = Product(
            seller_id=self.seller1.id, nm_id=987654,
            title='Protected', price=100, quantity=4,
        )
        db.session.add(product)
        db.session.commit()

        response = self.client.patch(
            f'/internal/v1/sellers/{self.seller1.id}/products/{product.id}',
            headers=self.task_headers(), json={'price': 200, 'quantity': 8},
        )

        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertTrue(payload['requires_manual_review'])
        self.assertEqual(payload['protected_fields'], ['price', 'quantity'])
        db.session.refresh(product)
        self.assertEqual(float(product.price), 100.0)
        self.assertEqual(product.quantity, 4)
        self.assertEqual(CardEditHistory.query.filter_by(product_id=product.id).count(), 0)


if __name__ == '__main__':
    unittest.main()
