# -*- coding: utf-8 -*-
"""Security and seller-context tests for the internal agent API."""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from models import (
    db, APILog, AgentChangeSnapshot, AgentConversation, AgentMessage, AgentTask,
    AutoImportSettings, ProductDefaults, CardEditHistory, ImportedProduct,
    ImageGenerationExperiment, Product, Seller, SellerSupplier,
    ServiceAgent, Supplier, User,
)
from routes.internal_api import internal_api_bp
from agents.platform_client import PlatformClient
from services.agent_harness import (
    _compact_dialog_context,
    _conversation_memory,
    _latest_conversation_scope,
    _resolve_message_product_scope,
    conversation_payload,
    rollback_task_tree,
    snapshot_count,
    submit_turn,
)


class InternalAgentSecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
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

    def test_paid_image_generation_requires_approved_task_and_is_idempotent(self):
        source = ImportedProduct.query.filter_by(
            seller_id=self.seller1.id, title='Полная карточка',
        ).one()
        product = Product(
            seller_id=self.seller1.id,
            nm_id=907560659,
            title='Карточка для фотостудии',
        )
        db.session.add(product)
        db.session.flush()
        source.product_id = product.id
        source.wb_nm_id = product.nm_id
        self.task1.input_data = json.dumps({
            'risk': 'write',
            'steps': [{'agent': 'image-generator'}],
            'product_ids': [product.id],
            'entity_scope': {'kind': 'product', 'ids': [product.id]},
        })
        db.session.commit()
        base = f'/internal/v1/sellers/{self.seller1.id}/image-generation'

        brief = self.client.post(
            f'{base}/brief',
            headers=self.task_headers(),
            json={'entity_kind': 'product', 'product_id': product.id},
        )
        self.assertEqual(brief.status_code, 200)
        self.assertEqual(brief.get_json()['source_product_id'], source.id)
        self.assertEqual(brief.get_json()['generation'], {
            'backend': 'openrouter',
            'model': 'google/gemini-3.1-flash-lite-image',
            'strategy': 'native_scene',
            'resolution': '1K',
            'estimated_cost_usd': 0.04,
            'estimated_cost_rub': 3.3,
            'publishable': False,
            'review_required': True,
        })

        body = {
            'entity_kind': 'product',
            'product_id': product.id,
            'photo_index': 0,
            'scene_prompt': (
                'Saturated cyan gradient studio with a water splash on the left '
                'and a glossy white pedestal on the right'
            ),
            'prompt_model': 'google/gemini-2.5-flash',
        }
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'synthetic-test-key'}), \
                patch('services.image_lab_service.launch_experiments') as launch:
            created = self.client.post(
                f'{base}/experiments', headers=self.task_headers(), json=body,
            )
            replay = self.client.post(
                f'{base}/experiments', headers=self.task_headers(), json=body,
            )
            changed_replay = self.client.post(
                f'{base}/experiments', headers=self.task_headers(), json={
                    **body,
                    'scene_prompt': (
                        'Warm neutral studio with a stone pedestal and soft '
                        'directional daylight from the left'
                    ),
                },
            )

        self.assertEqual(created.status_code, 202)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(changed_replay.status_code, 200)
        experiment_id = created.get_json()['experiment']['id']
        self.assertEqual(replay.get_json()['experiment']['id'], experiment_id)
        self.assertEqual(changed_replay.get_json()['experiment']['id'], experiment_id)
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertFalse(changed_replay.get_json()['request_signature_matched'])
        self.assertEqual(ImageGenerationExperiment.query.filter_by(
            seller_id=self.seller1.id,
        ).count(), 1)
        experiment = db.session.get(ImageGenerationExperiment, experiment_id)
        self.assertEqual(experiment.backend, 'openrouter')
        self.assertEqual(experiment.model, 'google/gemini-3.1-flash-lite-image')
        self.assertEqual(experiment.generation_strategy, 'native_scene')
        self.assertEqual(float(experiment.estimated_cost_rub), 3.3)
        self.assertEqual(launch.call_count, 3)

        polled = self.client.get(
            f'{base}/experiments/{experiment_id}', headers=self.task_headers(),
        )
        self.assertEqual(polled.status_code, 200)
        outside = ImageGenerationExperiment(
            seller_id=self.seller1.id,
            imported_product_id=source.id,
            backend='aitunnel',
            model='gpt-image-2',
            generation_strategy='native_scene',
            composition_mode='single',
            source_photo_indices_json='[0]',
            source_photo_roles_json='{"0":"angle"}',
            primary_photo_index=0,
            prompt='unrelated',
            prompt_sha256='0' * 64,
            status='queued',
            estimated_cost_rub=1.53,
        )
        db.session.add(outside)
        db.session.commit()
        denied_poll = self.client.get(
            f'{base}/experiments/{outside.id}', headers=self.task_headers(),
        )
        self.assertEqual(denied_poll.status_code, 403)

        self.task1.input_data = json.dumps({
            'risk': 'read', 'steps': [{'agent': 'image-generator'}],
        })
        db.session.commit()
        denied_create = self.client.post(
            f'{base}/experiments', headers=self.task_headers(), json=body,
        )
        self.assertEqual(denied_create.status_code, 403)

    def test_paid_image_generation_backfills_exact_linked_wb_photos_atomically(self):
        source = ImportedProduct.query.filter_by(
            seller_id=self.seller1.id, title='Полная карточка',
        ).one()
        source.photo_urls = '[]'
        product = Product(
            seller_id=self.seller1.id,
            nm_id=100138374,
            title='Опубликованная карточка с WB-фото',
            photos_json='[1, 2, 3]',
        )
        db.session.add(product)
        db.session.flush()
        source.product_id = product.id
        source.wb_nm_id = product.nm_id
        self.task1.input_data = json.dumps({
            'risk': 'write',
            'steps': [{'agent': 'image-generator'}],
            'product_ids': [product.id],
            'entity_scope': {'kind': 'product', 'ids': [product.id]},
        })
        db.session.commit()
        base = f'/internal/v1/sellers/{self.seller1.id}/image-generation'
        normalized = [
            f'https://basket.example/{product.nm_id}/{index}.webp'
            for index in (1, 2, 3)
        ]

        with patch(
            'services.wb_media.normalize_photo_urls', return_value=normalized,
        ):
            brief = self.client.post(
                f'{base}/brief',
                headers=self.task_headers(),
                json={'entity_kind': 'product', 'product_id': product.id},
            )

        self.assertEqual(brief.status_code, 200)
        self.assertEqual(brief.get_json()['photo_count'], 3)
        self.assertEqual(json.loads(source.photo_urls), [])

        body = {
            'entity_kind': 'product',
            'product_id': product.id,
            'photo_index': 0,
            'scene_prompt': (
                'Cool tropical studio with clear water reflections and a clean '
                'white pedestal under soft directional daylight'
            ),
            'prompt_model': 'gemini-2.5-flash',
        }
        with patch(
            'services.wb_media.normalize_photo_urls', return_value=normalized,
        ), patch.dict(
            'os.environ', {'OPENROUTER_API_KEY': 'synthetic-test-key'},
        ), patch(
            'services.image_lab_service.launch_experiments',
        ) as launch:
            created = self.client.post(
                f'{base}/experiments', headers=self.task_headers(), json=body,
            )

        self.assertEqual(created.status_code, 202)
        db.session.refresh(source)
        self.assertEqual(json.loads(source.photo_urls), normalized)
        experiment_id = created.get_json()['experiment']['id']
        experiment = db.session.get(ImageGenerationExperiment, experiment_id)
        self.assertEqual(experiment.imported_product_id, source.id)
        launch.assert_called_once_with(self.app, [experiment_id])

    def test_paid_image_generation_checks_provider_balance_before_gemini(self):
        source = ImportedProduct.query.filter_by(
            seller_id=self.seller1.id, title='Полная карточка',
        ).one()
        product = Product(
            seller_id=self.seller1.id,
            nm_id=99112233,
            title='Карточка с проверкой баланса',
            photos_json='[1]',
        )
        db.session.add(product)
        db.session.flush()
        source.product_id = product.id
        source.wb_nm_id = product.nm_id
        self.task1.input_data = json.dumps({
            'risk': 'write',
            'steps': [{'agent': 'image-generator'}],
            'product_ids': [product.id],
            'entity_scope': {'kind': 'product', 'ids': [product.id]},
        })
        db.session.commit()

        with patch.dict(self.app.config, {'TESTING': False}), patch(
            'services.image_lab_service.openrouter_balance_usd',
            return_value=0.01,
        ):
            response = self.client.post(
                f'/internal/v1/sellers/{self.seller1.id}/image-generation/brief',
                headers=self.task_headers(),
                json={'entity_kind': 'product', 'product_id': product.id},
            )

        self.assertEqual(response.status_code, 409)
        error = response.get_json()['error']
        self.assertIn('$0.01', error)
        self.assertIn('$0.04', error)
        self.assertEqual(ImageGenerationExperiment.query.filter_by(
            seller_id=self.seller1.id,
        ).count(), 0)

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

    def test_imported_batch_rejects_invalid_or_duplicate_ids_before_write(self):
        product = ImportedProduct.query.filter_by(
            seller_id=self.seller1.id, title='Полная карточка',
        ).one()
        original_title = product.title
        snapshot_count = AgentChangeSnapshot.query.filter_by(
            imported_product_id=product.id,
        ).count()

        invalid_updates = (
            {'updates': {'product_id': product.id, 'title': 'Нельзя'}},
            {'updates': [None]},
            {'updates': [{'product_id': True, 'title': 'Нельзя'}]},
            {'updates': [{'product_id': 1.5, 'title': 'Нельзя'}]},
            {'updates': [
                {'product_id': product.id, 'title': 'Первое'},
                {'product_id': product.id, 'title': 'Второе'},
            ]},
        )
        for body in invalid_updates:
            with self.subTest(body=body):
                response = self.client.patch(
                    '/internal/v1/imported-products/batch',
                    headers=self.task_headers(),
                    json=body,
                )
                self.assertEqual(response.status_code, 400)

        db.session.refresh(product)
        self.assertEqual(product.title, original_title)
        self.assertEqual(AgentChangeSnapshot.query.filter_by(
            imported_product_id=product.id,
        ).count(), snapshot_count)

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

    def test_chat_keeps_typed_scope_and_run_outcome_as_durable_context(self):
        conversation = AgentConversation(
            id='conversation-context', seller_id=self.seller1.id,
            user_id=self.seller1.user_id, title='Context',
        )
        db.session.add(conversation)
        start = datetime(2026, 7, 15, 10, 0, 0)
        selected = AgentMessage(
            id='context-user', conversation_id=conversation.id,
            role='user', kind='text', content='Улучши эту карточку',
            created_at=start, updated_at=start,
        )
        selected.set_metadata({
            'scope_origin': 'request',
            'entity_scope': {'kind': 'product', 'ids': [13989]},
            'page_context': {'url': '/products/13989'},
        })
        run = AgentMessage(
            id='context-run', conversation_id=conversation.id,
            role='assistant', kind='run', content='Подготовлено предложение.',
            created_at=start + timedelta(seconds=1),
            updated_at=start + timedelta(seconds=1),
        )
        run.set_metadata({
            'status': 'completed',
            'result': {
                'status': 'completed',
                'message': 'Название и описание подготовлены.',
                'saved': 1,
                'results': [{
                    'skill': 'content-writer',
                    'status': 'completed',
                    'result': {
                        'message': 'Название и описание подготовлены.',
                        'requested_fields': ['title', 'description'],
                    },
                }],
            },
        })
        db.session.add_all([selected, run])
        db.session.commit()

        self.assertEqual(_latest_conversation_scope(conversation), {
            'kind': 'product',
            'ids': [13989],
            'page_context': {'url': '/products/13989'},
        })
        context = _compact_dialog_context(conversation)
        self.assertIn('Результат запуска: completed', context[-1]['content'])
        memory = _conversation_memory(conversation)
        self.assertEqual(
            memory['last_run']['requested_fields'], ['title', 'description'],
        )
        self.assertEqual(
            conversation_payload(conversation)['active_scope']['ids'], [13989],
        )

        followup = submit_turn(
            conversation, 'привет', [13989],
            entity_kind='product', scope_mode='selected',
        )
        self.assertEqual(
            followup['user_message'].get_metadata()['scope_origin'],
            'conversation',
        )

        explicit_global = submit_turn(
            conversation, 'привет', [], scope_mode='global',
        )
        self.assertEqual(
            explicit_global['user_message'].get_metadata()['entity_scope']['ids'],
            [],
        )
        self.assertEqual(
            explicit_global['user_message'].get_metadata()['scope_origin'],
            'global',
        )

        boundary = AgentMessage(
            id='context-global', conversation_id=conversation.id,
            role='user', kind='text', content='Покажи весь каталог',
            created_at=start + timedelta(seconds=2),
            updated_at=start + timedelta(seconds=2),
        )
        boundary.set_metadata({
            'scope_origin': 'global',
            'entity_scope': {'kind': 'imported_product', 'ids': []},
        })
        db.session.add(boundary)
        db.session.commit()
        self.assertIsNone(_latest_conversation_scope(conversation))

    def test_chat_grounds_wb_article_from_text_to_typed_product_scope(self):
        product = Product(
            seller_id=self.seller1.id, nm_id=68092554,
            vendor_code='id-5167-1277', title='Карточка WB',
        )
        conversation = AgentConversation(
            id='conversation-article', seller_id=self.seller1.id,
            user_id=self.seller1.user_id, title='Article grounding',
        )
        db.session.add_all([product, conversation])
        db.session.commit()

        text = (
            'Проверь карточку (1 шт.): артикул 68092554. '
            'Покажи основные проблемы.'
        )
        resolved = _resolve_message_product_scope(self.seller1.id, text)
        self.assertEqual(resolved['kind'], 'product')
        self.assertEqual(resolved['ids'], [product.id])
        self.assertEqual(resolved['references'][0]['matched_by'], 'nm_id')

        result = submit_turn(
            conversation, text, [], scope_mode='global',
        )
        metadata = result['user_message'].get_metadata()
        self.assertEqual(metadata['scope_origin'], 'message_reference')
        self.assertEqual(metadata['scope_mode'], 'selected')
        self.assertEqual(metadata['entity_scope'], {
            'kind': 'product', 'ids': [product.id],
        })
        self.assertEqual(result['assistant_message'].kind, 'plan')
        self.assertEqual(
            conversation_payload(conversation)['active_scope']['ids'],
            [product.id],
        )

    def test_chat_can_ground_explicit_any_card_for_image_approval_plan(self):
        source = ImportedProduct.query.filter_by(
            seller_id=self.seller1.id, title='Полная карточка',
        ).one()
        product = Product(
            seller_id=self.seller1.id,
            nm_id=907560659,
            title='Карточка со сценой',
            quality_impact=91,
        )
        conversation = AgentConversation(
            id='conversation-any-image', seller_id=self.seller1.id,
            user_id=self.seller1.user_id, title='Any image',
        )
        db.session.add_all([product, conversation])
        db.session.flush()
        source.product_id = product.id
        source.wb_nm_id = product.nm_id
        db.session.commit()

        result = submit_turn(
            conversation,
            'собери сцену для карточки любой',
            [],
            scope_mode='global',
        )

        user_metadata = result['user_message'].get_metadata()
        plan_metadata = result['assistant_message'].get_metadata()
        self.assertEqual(result['assistant_message'].kind, 'plan')
        self.assertEqual(user_metadata['scope_mode'], 'selected')
        self.assertEqual(user_metadata['entity_scope'], {
            'kind': 'product', 'ids': [product.id],
        })
        self.assertIn('выбран помощником', user_metadata['scope_label'])
        self.assertEqual(plan_metadata['status'], 'pending_approval')
        self.assertEqual(plan_metadata['product_ids'], [product.id])
        self.assertEqual(plan_metadata['steps'][0]['agent'], 'image-generator')
        self.assertIn('google/gemini-3.1-flash-lite-image', plan_metadata['summary'])
        self.assertIn('≈3,30 ₽', plan_metadata['summary'])

    def test_production_quality_prompt_with_nm_id_starts_typed_semantic_plan(self):
        product = Product(
            seller_id=self.seller1.id, nm_id=68092554,
            vendor_code='id-5167-1277', title='Карточка WB',
        )
        conversation = AgentConversation(
            id='conversation-production-article', seller_id=self.seller1.id,
            user_id=self.seller1.user_id, title='Production regression',
        )
        orchestrator = ServiceAgent(
            id='orchestrator-regression', name='orchestrator',
            display_name='Orchestrator', status='online',
            last_heartbeat=datetime.utcnow(),
        )
        db.session.add_all([product, conversation, orchestrator])
        db.session.commit()

        result = submit_turn(
            conversation,
            'Улучши карточки (1 шт.): артикулы 68092554. Основные '
            'проблемы: Мало фото, Слабые характеристики, Слабое описание, '
            'Слабый заголовок, Нет просмотров, Низкий рейтинг. Составь план '
            'исправления контента.',
            [], scope_mode='global',
        )

        self.assertEqual(result['assistant_message'].kind, 'run')
        planning_task = db.session.get(
            AgentTask, result['assistant_message'].task_id,
        )
        task_input = planning_task.get_input()
        self.assertEqual(planning_task.task_type, 'plan_request')
        self.assertEqual(task_input['product_ids'], [product.id])
        self.assertEqual(task_input['entity_scope'], {
            'kind': 'product', 'ids': [product.id],
        })
        self.assertEqual(task_input['scope_origin'], 'message_reference')
        self.assertNotIn('pipeline', task_input)

    def test_unknown_explicit_article_clarifies_without_global_plan(self):
        conversation = AgentConversation(
            id='conversation-unknown-article', seller_id=self.seller1.id,
            user_id=self.seller1.user_id, title='Unknown article',
        )
        foreign = Product(
            seller_id=self.seller2.id, nm_id=99999999,
            vendor_code='foreign', title='Чужая карточка',
        )
        db.session.add_all([conversation, foreign])
        db.session.commit()

        result = submit_turn(
            conversation,
            'Улучши карточку с артикулом 99999999',
            [], scope_mode='global',
        )

        self.assertEqual(result['assistant_message'].kind, 'clarification')
        self.assertIn('99999999', result['assistant_message'].content)
        self.assertEqual(
            result['assistant_message'].get_metadata()['reason'],
            'product_reference_not_found',
        )
        self.assertEqual(
            result['user_message'].get_metadata()['entity_scope']['ids'], [],
        )

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

    def test_local_rollback_never_overwrites_a_later_manual_edit(self):
        product = Product(
            seller_id=self.seller1.id, nm_id=123457,
            title='Original', description='Before',
        )
        db.session.add(product)
        db.session.commit()
        response = self.client.patch(
            f'/internal/v1/sellers/{self.seller1.id}/products/{product.id}',
            headers=self.task_headers(), json={'description': 'AI proposal'},
        )
        self.assertEqual(response.status_code, 200)

        product.description = 'Manual edit after AI'
        self.task1.status = 'completed'
        db.session.commit()
        result = rollback_task_tree(self.task1.id, self.seller1.id)

        history = CardEditHistory.query.filter_by(product_id=product.id).one()
        db.session.refresh(product)
        self.assertEqual(result['snapshots'], 0)
        self.assertEqual(result['conflicts'], 1)
        self.assertEqual(result['conflict_details'][0]['fields'], ['description'])
        self.assertEqual(product.description, 'Manual edit after AI')
        self.assertFalse(history.reverted)
        self.assertEqual(history.wb_sync_status, 'conflict')
        self.assertEqual(snapshot_count(self.task1.id), 0)

    def test_wb_content_publish_requires_exact_approved_product_scope_and_fields(self):
        product = Product(
            seller_id=self.seller1.id, nm_id=919191,
            title='Prepared', description='Prepared description',
        )
        conversation = AgentConversation(
            id='publish-conversation', seller_id=self.seller1.id,
            user_id=self.seller1.user_id, title='Publish',
        )
        db.session.add_all([product, conversation])
        db.session.flush()
        self.task1.input_data = json.dumps({
            'conversation_id': conversation.id,
            'product_ids': [product.id],
            'entity_scope': {'kind': 'product', 'ids': [product.id]},
            'steps': [{
                'agent': 'wb-content-publisher',
                'params': {'fields': ['title', 'description']},
            }],
        })
        db.session.commit()
        expected = {
            'published': 1, 'already_published': 0, 'failed': 0,
            'results': [{
                'product_id': product.id, 'status': 'published',
                'fields': ['title', 'description'],
            }],
        }
        with patch(
            'services.agent_wb_content.publish_confirmed_content_proposals',
            return_value=expected,
        ) as publish:
            response = self.client.post(
                f'/internal/v1/sellers/{self.seller1.id}/products/'
                'content-proposals/publish-batch',
                headers=self.task_headers(),
                json={
                    'product_ids': [product.id],
                    'fields': ['title', 'description'],
                },
            )

        self.assertEqual(response.status_code, 200)
        publish.assert_called_once_with(
            self.task1, self.seller1.id, [product.id], ['title', 'description'],
        )

        wrong_fields = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/products/'
            'content-proposals/publish-batch',
            headers=self.task_headers(),
            json={'product_ids': [product.id], 'fields': ['title']},
        )
        duplicate = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/products/'
            'content-proposals/publish-batch',
            headers=self.task_headers(),
            json={
                'product_ids': [product.id, product.id],
                'fields': ['title', 'description'],
            },
        )
        self.assertEqual(wrong_fields.status_code, 403)
        self.assertEqual(duplicate.status_code, 400)

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
