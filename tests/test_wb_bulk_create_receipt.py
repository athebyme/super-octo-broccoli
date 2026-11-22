"""Owner-scoped idempotency and exact-scope receipts for WB bulk create."""

import sys
import types
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import BackgroundJob, ImportedProduct, Seller, User, db
from routes.suppliers import register_supplier_routes


class WBBulkCreateReceiptTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='wb-bulk-create-route-tests',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_supplier_routes(self.app)

        from flask_wtf.csrf import generate_csrf

        @self.app.route('/_test/csrf')
        def csrf_token():
            return generate_csrf()

        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        user = User(
            username='wb-bulk-create-owner',
            email='wb-bulk-create-owner@test.local',
            password_hash='test-hash',
            is_active=True,
        )
        other_user = User(
            username='wb-bulk-create-other',
            email='wb-bulk-create-other@test.local',
            password_hash='test-hash',
            is_active=True,
        )
        db.session.add_all([user, other_user])
        db.session.flush()
        seller = Seller(user_id=user.id, company_name='WB create owner')
        seller.wb_api_key = 'synthetic-key'
        other_seller = Seller(user_id=other_user.id, company_name='Other owner')
        other_seller.wb_api_key = 'synthetic-key'
        db.session.add_all([seller, other_seller])
        db.session.flush()
        products = [
            ImportedProduct(seller_id=seller.id, title=f'Owned {index}')
            for index in range(2)
        ]
        foreign_product = ImportedProduct(
            seller_id=other_seller.id, title='Foreign product',
        )
        db.session.add_all([*products, foreign_product])
        db.session.commit()
        self.user_id = user.id
        self.seller_id = seller.id
        self.other_user_id = other_user.id
        self.product_ids = [product.id for product in products]
        self.foreign_product_id = foreign_product.id
        self.client = self._client(self.user_id)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _client(self, user_id, *, csrf_secret='test-csrf-secret'):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True
            sess['csrf_token'] = csrf_secret
        return client

    def _auth_patches(self, user_id):
        seller = Seller.query.filter_by(user_id=user_id).first()
        user = SimpleNamespace(
            id=user_id,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
            seller=seller,
        )
        return (
            patch('routes.suppliers.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
        )

    def _post(self, client, user_id, payload, *, headers=None):
        user_patch, login_patch = self._auth_patches(user_id)
        fake_app = types.ModuleType('seller_platform')
        fake_app.app = self.app
        with user_patch, login_patch, patch.dict(
            sys.modules, {'seller_platform': fake_app},
        ), patch('threading.Thread') as thread:
            thread.return_value.start.return_value = None
            response = client.post(
                '/my-products/push-to-wb-bulk',
                json=payload,
                headers=headers or {},
            )
        return response, thread

    def _get_result(self, client, user_id, request_key, *, query_string=None):
        user_patch, login_patch = self._auth_patches(user_id)
        with user_patch, login_patch:
            response = client.get(
                '/my-products/push-to-wb-bulk/result',
                query_string=query_string,
                headers={'X-WB-Create-Key': request_key},
            )
        return response

    @staticmethod
    def _run_thread_target(thread, *, cancel_before_work=False):
        db.session.remove()
        target = thread.call_args.kwargs['target']
        args = thread.call_args.kwargs['args']
        if cancel_before_work:
            target(*args)
            return

        class FakeImporter:
            def __init__(self, _seller):
                pass

            @staticmethod
            def batch_import_products_to_wb(products, progress_callback=None):
                if progress_callback:
                    progress_callback('preparing', {'items': []})
                return [
                    {
                        'product_id': product.id,
                        'title': product.title,
                        'status': 'success' if index == 0 else 'skipped',
                        'error': None if index == 0 else 'not ready',
                        'nm_id': None,
                        'warnings': [],
                    }
                    for index, product in enumerate(products)
                ]

        with patch(
            'services.wb_product_importer.WBProductImporter', FakeImporter,
        ), patch(
            'services.upload_readiness_validator.validate_product_upload_readiness',
            return_value={'is_ready': False, 'issues': []},
        ):
            target(*args)

    def test_keyed_start_replays_across_reauthenticated_sessions(self):
        request_key = 'a' * 32
        response, thread = self._post(
            self.client, self.user_id,
            {'product_ids': self.product_ids, 'request_key': request_key},
        )
        self.assertEqual(response.status_code, 202)
        first = response.get_json()
        self.assertEqual(first['product_ids'], self.product_ids)
        self.assertEqual(first['total'], len(self.product_ids))
        self.assertEqual(first['status'], 'pending')
        self.assertFalse(first['replayed'])
        self.assertFalse(first['worker_exited'])
        self.assertIsNone(first['result_counts'])
        self.assertEqual(thread.call_count, 1)
        thread.return_value.start.assert_called_once()

        job = BackgroundJob.query.filter_by(job_uid=first['job_uid']).one()
        self.assertNotIn(request_key, job.job_uid)
        self.assertNotIn(request_key, job.progress_data)
        self.assertNotIn(request_key, job.result_data)
        self._run_thread_target(thread)
        completed = self._get_result(self.client, self.user_id, request_key)
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()['status'], 'completed')
        self.assertTrue(completed.get_json()['worker_exited'])
        self.assertEqual(
            completed.get_json()['result_counts'],
            {'processed': 2, 'succeeded': 1, 'failed': 1},
        )

        renewed_client = self._client(self.user_id, csrf_secret='renewed-session-secret')
        replay, replay_thread = self._post(
            renewed_client, self.user_id,
            {'product_ids': self.product_ids, 'request_key': request_key},
        )
        self.assertEqual(replay.status_code, 200)
        second = replay.get_json()
        self.assertEqual(second['job_uid'], first['job_uid'])
        self.assertEqual(second['product_ids'], self.product_ids)
        self.assertTrue(second['replayed'])
        replay_thread.assert_not_called()
        self.assertEqual(BackgroundJob.query.count(), 1)

        readback = self._get_result(
            renewed_client, self.user_id, request_key,
        )
        self.assertEqual(readback.status_code, 200)
        self.assertEqual(readback.get_json()['job_uid'], first['job_uid'])
        self.assertTrue(readback.get_json()['replayed'])
        self.assertTrue(readback.get_json()['worker_exited'])
        self.assertIn('no-store', readback.headers['Cache-Control'])

        other_client = self._client(self.other_user_id)
        foreign_read = self._get_result(other_client, self.other_user_id, request_key)
        self.assertEqual(foreign_read.status_code, 404)
        self.assertEqual(
            foreign_read.get_json()['code'], 'wb_create_receipt_not_found',
        )

    def test_stale_unexited_managed_job_stays_unknown_and_blocks_same_set(self):
        request_key = '4' * 32
        response, _thread = self._post(
            self.client, self.user_id,
            {'product_ids': [self.product_ids[0]], 'request_key': request_key},
        )
        job_uid = response.get_json()['job_uid']
        db.session.remove()
        job = BackgroundJob.query.filter_by(job_uid=job_uid).one()
        job.updated_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()

        result = self._get_result(self.client, self.user_id, request_key)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()['status'], 'unknown')
        self.assertFalse(result.get_json()['worker_exited'])
        db.session.remove()
        self.assertEqual(
            BackgroundJob.query.filter_by(job_uid=job_uid).one().status,
            'pending',
        )

        denied, thread = self._post(
            self.client, self.user_id,
            {'product_ids': [self.product_ids[0]], 'request_key': '5' * 32},
        )
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.get_json()['code'], 'wb_create_overlap')
        self.assertFalse(denied.get_json()['accepted'])
        thread.assert_not_called()

    def test_key_scope_mismatch_is_conflict_and_query_keys_are_rejected(self):
        request_key = 'b' * 32
        initial, thread = self._post(
            self.client, self.user_id,
            {'product_ids': self.product_ids, 'request_key': request_key},
        )
        self.assertEqual(initial.status_code, 202)

        mismatch, mismatch_thread = self._post(
            self.client, self.user_id,
            {'product_ids': list(reversed(self.product_ids)), 'request_key': request_key},
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.get_json()['code'], 'wb_create_request_key_conflict')
        self.assertNotIn('accepted', mismatch.get_json())
        mismatch_thread.assert_not_called()

        query_read = self._get_result(
            self.client, self.user_id, request_key,
            query_string={'request_key': request_key},
        )
        self.assertEqual(query_read.status_code, 400)
        self.assertEqual(
            query_read.get_json()['code'], 'wb_create_request_key_invalid',
        )

        absent = self._get_result(self.client, self.user_id, '9' * 32)
        self.assertEqual(absent.status_code, 404)
        self.assertEqual(
            absent.get_json()['code'], 'wb_create_receipt_not_found',
        )

    def test_exact_ownership_and_intersecting_overlap_fail_closed(self):
        request_key = 'c' * 32
        foreign, foreign_thread = self._post(
            self.client, self.user_id,
            {'product_ids': [self.foreign_product_id], 'request_key': request_key},
        )
        self.assertEqual(foreign.status_code, 400)
        self.assertEqual(foreign.get_json()['code'], 'wb_create_scope_mismatch')
        self.assertFalse(foreign.get_json()['accepted'])
        foreign_thread.assert_not_called()
        self.assertEqual(BackgroundJob.query.count(), 0)

        first, first_thread = self._post(
            self.client, self.user_id,
            {'product_ids': [self.product_ids[0]], 'request_key': request_key},
        )
        self.assertEqual(first.status_code, 202)
        first_thread.return_value.start.assert_called_once()

        another_key, overlap_thread = self._post(
            self.client, self.user_id,
            {'product_ids': [self.product_ids[0]], 'request_key': 'd' * 32},
        )
        self.assertEqual(another_key.status_code, 409)
        self.assertEqual(another_key.get_json()['code'], 'wb_create_overlap')
        self.assertFalse(another_key.get_json()['accepted'])
        overlap_thread.assert_not_called()

        disjoint, disjoint_thread = self._post(
            self.client, self.user_id,
            {'product_ids': [self.product_ids[1]], 'request_key': 'e' * 32},
        )
        self.assertEqual(disjoint.status_code, 202)
        disjoint_thread.return_value.start.assert_called_once()
        self.assertEqual(BackgroundJob.query.count(), 2)

    def test_cancel_is_unknown_until_worker_exit_then_releases_overlap(self):
        request_key = 'f' * 32
        response, _thread = self._post(
            self.client, self.user_id,
            {'product_ids': [self.product_ids[0]], 'request_key': request_key},
        )
        job_uid = response.get_json()['job_uid']

        user_patch, login_patch = self._auth_patches(self.user_id)
        with user_patch, login_patch:
            cancelled = self.client.post(f'/api/jobs/{job_uid}/cancel')
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()['status'], 'unknown')
        self.assertFalse(cancelled.get_json()['worker_exited'])

        result = self._get_result(self.client, self.user_id, request_key)
        self.assertEqual(result.get_json()['status'], 'unknown')
        self.assertFalse(result.get_json()['worker_exited'])
        self.assertTrue(result.get_json()['cancel_requested'])

        blocked, blocked_thread = self._post(
            self.client, self.user_id,
            {'product_ids': [self.product_ids[0]], 'request_key': '1' * 32},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()['code'], 'wb_create_overlap')
        blocked_thread.assert_not_called()

        cancel_thread = _thread
        self._run_thread_target(cancel_thread, cancel_before_work=True)

        terminal = self._get_result(self.client, self.user_id, request_key)
        self.assertEqual(terminal.get_json()['status'], 'cancelled')
        self.assertTrue(terminal.get_json()['worker_exited'])
        self.assertIsNone(terminal.get_json()['result_counts'])

        retry, retry_thread = self._post(
            self.client, self.user_id,
            {'product_ids': [self.product_ids[0]], 'request_key': '2' * 32},
        )
        self.assertEqual(retry.status_code, 202)
        retry_thread.return_value.start.assert_called_once()

    def test_csrf_and_invalid_json_do_not_reserve_or_start(self):
        self.app.config['WTF_CSRF_ENABLED'] = True
        user_patch, login_patch = self._auth_patches(self.user_id)
        with user_patch, login_patch:
            missing_csrf = self.client.post(
                '/my-products/push-to-wb-bulk',
                json={'product_ids': self.product_ids, 'request_key': '3' * 32},
            )
        self.assertEqual(missing_csrf.status_code, 400)
        self.assertEqual(BackgroundJob.query.count(), 0)

        csrf_response = self.client.get('/_test/csrf')
        token = csrf_response.get_data(as_text=True)
        user_patch, login_patch = self._auth_patches(self.user_id)
        fake_app = types.ModuleType('seller_platform')
        fake_app.app = self.app
        with user_patch, login_patch, patch.dict(
            sys.modules, {'seller_platform': fake_app},
        ), patch('threading.Thread') as thread:
            malformed = self.client.post(
                '/my-products/push-to-wb-bulk',
                data='not-json',
                content_type='application/json',
                headers={'X-CSRFToken': token},
            )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(set(malformed.get_json()), {'success', 'error'})
        self.assertFalse(malformed.get_json()['success'])
        thread.assert_not_called()
        self.assertEqual(BackgroundJob.query.count(), 0)

        user_patch, login_patch = self._auth_patches(self.user_id)
        with user_patch, login_patch, patch.dict(
            sys.modules, {'seller_platform': fake_app},
        ), patch('threading.Thread') as valid_thread:
            valid_thread.return_value.start.return_value = None
            valid = self.client.post(
                '/my-products/push-to-wb-bulk',
                json={'product_ids': [self.product_ids[0]], 'request_key': '6' * 32},
                headers={'X-CSRFToken': token},
            )
        self.assertEqual(valid.status_code, 202)
        valid_thread.return_value.start.assert_called_once()
        self.assertEqual(BackgroundJob.query.count(), 1)

    def test_legacy_no_key_success_shape_is_unchanged(self):
        response, thread = self._post(
            self.client, self.user_id,
            {'product_ids': [self.product_ids[0]]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.get_json()), {'success', 'job_uid'})
        self.assertTrue(response.get_json()['success'])
        thread.return_value.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
