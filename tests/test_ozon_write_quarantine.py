"""Local operator decisions: authorization, atomicity, scope and honest outcome."""
from datetime import datetime, timedelta
import json
import unittest
import uuid
from unittest.mock import patch

from models import (db, Seller, SellerMarketplaceAccount, MarketplaceOperation,
    MarketplaceListingSnapshot, MarketplaceWriteQuarantine as Hold,
    MarketplaceWriteQuarantineEvent as Event)
from services import ozon_write_quarantine as service
from services.ozon_quarantine_scope import QuarantineScope
from services.marketplace_operation_locks import try_account_operation_lock
from services.marketplace_commercial import MarketplaceCommercialService
from services.ozon_product_import import OzonProductImportContract
from services.ozon_product_state import OzonProductStateContract
from services.marketplace_publications import MarketplacePublicationService
from tests import test_marketplace_accounts as fixtures


class WriteQuarantineTest(unittest.TestCase):
    setUp = fixtures.MarketplaceAccountsTest.setUp
    tearDown = fixtures.MarketplaceAccountsTest.tearDown
    _create_seller = staticmethod(fixtures.MarketplaceAccountsTest._create_seller)
    _save = fixtures.MarketplaceAccountsTest._save

    def actor(self, seller_id=None):
        return db.session.get(Seller, seller_id or self.seller1_id).user_id

    def operation(self, account=None, *, kind='price_update', offer='EXACT-01', product='999', **overrides):
        account = account or self._save()
        before = {'kind': 'price', 'offer_id': offer, 'product_id': product, 'price': '1000',
            'old_price': '0', 'min_price': '0', 'currency_code': 'RUB',
            'auto_action_enabled': None, 'auto_add_to_ozon_actions_list_enabled': None}
        submitted = dict(before, price='1100')
        if kind.startswith('stock_'):
            before = {'kind': 'stock', 'offer_id': offer, 'product_id': product,
                'warehouse_id': '9001', 'sku': '1111', 'stock': 5}
            submitted = dict(before, stock=4)
        summary = {'offer_id': offer, 'before': before, 'proposed': submitted}
        if kind.startswith('product_'):
            summary = {'offer_id': offer, 'external_product_id': product}
            submitted = {'items': [{'offer_id': offer, 'name': 'Synthetic product', 'description_category_id': 100,
                'type_id': 200, 'attributes': [], 'complex_attributes': [], 'price': '1100',
                'currency_code': 'RUB', 'vat': '0.20', 'weight': 100, 'weight_unit': 'g',
                'width': 10, 'height': 20, 'depth': 30, 'dimension_unit': 'mm',
                'primary_image': 'https://example.com/product.jpg', 'images': []}]}
            before = {'exists': True, 'identity': {'offer_id': offer, 'product_id': product}, 'payload': submitted}
            if kind == 'product_import':
                before = {'offer_id': offer, 'exists': False, 'items': []}
            if kind == 'product_import_rollback':
                submitted = {'product_id': [product]}
        fingerprint = (OzonProductStateContract.fingerprint(submitted) if kind in {'product_update', 'product_update_rollback'}
            else OzonProductImportContract.fingerprint(submitted))
        values = dict(seller_id=account.seller_id, marketplace_id=account.marketplace_id, account_id=account.id,
            operation_kind=kind, status='uncertain', attempt_count=1, idempotency_key=uuid.uuid4().hex,
            contract_version='synthetic', request_fingerprint=fingerprint, request_summary_json=json.dumps(summary),
            quota_reserved=1, external_task_id='task-original', next_poll_at=datetime.utcnow(), version=1)
        values.update(overrides)
        operation = MarketplaceOperation(**values)
        db.session.add(operation)
        db.session.flush()
        db.session.add(MarketplaceListingSnapshot(operation_id=operation.id,
            seller_id=account.seller_id, marketplace_id=account.marketplace_id, account_id=account.id,
            snapshot_kind='price', source_fingerprint='a'*64, before_state_json=json.dumps(before),
            submitted_state_json=json.dumps(submitted), submitted_fingerprint=MarketplaceCommercialService._fingerprint(submitted)))
        db.session.commit()
        return operation

    def preview(self, operation, **overrides):
        values = dict(seller_id=operation.seller_id, origin_id=operation.id, viewer_user_id=self.actor())
        values.update(overrides)
        return service.preview(**values)

    def placement(self, operation):
        document = self.preview(operation)
        return dict(seller_id=operation.seller_id, origin_id=operation.id,
            expected_version=document['operation_version'], scope_token=document['scope_token'],
            reason='Проверяем неизвестный результат отдельно.', actor_user_id=self.actor(), confirm_scope=True)

    def place(self, operation, **overrides):
        values = self.placement(operation)
        values.update(overrides)
        return service.place(**values)

    def decision(self, operation, **overrides):
        doc = self.preview(operation)
        values = dict(seller_id=operation.seller_id, origin_id=operation.id,
            expected_version=doc['hold']['version'], expected_operation_version=doc['operation_version'],
            action='note_added', reason='Добавлено уточнение после проверки.', actor_user_id=self.actor())
        values.update(overrides)
        return service.update_decision(**values)

    def confirm(self, operation):
        snapshot = operation.snapshot
        state = json.loads(snapshot.submitted_state_json)
        operation.status = 'succeeded'
        operation.completed_at = datetime.utcnow() + timedelta(seconds=1)
        operation.next_poll_at = None
        operation.error_code = None
        if operation.operation_kind == 'product_import_rollback':
            state = {'offer_id': 'EXACT-01', 'product_id': '999', 'archived': True, 'confirmed_at': '2026-09-26'}
        elif operation.operation_kind.startswith('product_'):
            result = {'offer_id': 'EXACT-01', 'product_id': '999', 'status': 'imported', 'errors': []}
            state = MarketplacePublicationService._outgoing_to_listing_state(payload=state,
                item_result=result, now=operation.completed_at,
                source='task_status' if operation.operation_kind == 'product_import' else 'task_status_and_live_state')
            operation.item_results_json = json.dumps([result])
        snapshot.confirmed_state_json = json.dumps(state)
        snapshot.confirmed_fingerprint = (MarketplaceCommercialService._fingerprint(state)
            if not operation.operation_kind.startswith('product_') else OzonProductImportContract.fingerprint(state))
        if operation.operation_kind in {'product_update', 'product_update_rollback'}:
            snapshot.confirmed_fingerprint = operation.request_fingerprint
        db.session.commit()

    def test_place_preserves_outcome_identity_and_snapshot_without_credentials(self):
        with self.app.app_context():
            operation = self.operation()
            untouched = (operation.status, operation.attempt_count, operation.external_task_id,
                operation.request_summary_json, operation.snapshot.before_state_json, operation.snapshot.submitted_state_json)
            version = operation.version
            with patch.object(SellerMarketplaceAccount, 'get_credentials', side_effect=AssertionError('No credentials')):
                self.place(operation)
                doc = self.preview(operation)
            self.assertEqual(untouched, (operation.status, operation.attempt_count, operation.external_task_id,
                operation.request_summary_json, operation.snapshot.before_state_json, operation.snapshot.submitted_state_json))
            self.assertEqual(operation.version, version + 1)
            self.assertIsNone(operation.next_poll_at)
            self.assertEqual(operation.quota_reserved, 0)
            self.assertEqual(doc['outcome'], 'uncertain')
            self.assertFalse(doc['can_release'])
            self.assertEqual(doc['scope']['kind'], 'product')
            self.assertEqual(doc['events'][0]['action'], 'placed')
            self.assertNotIn('synthetic-ozon-key', json.dumps(doc))
            self.assertNotIn('request_summary', doc)

    def test_already_stopped_operation_still_advances_reviewed_version(self):
        with self.app.app_context():
            op = self.operation(next_poll_at=None, quota_reserved=0)
            viewed = op.version
            self.place(op)
            self.assertEqual(op.version, viewed + 1)

    def test_scope_fallback_requires_review_of_actual_wider_scope(self):
        with self.app.app_context():
            op = self.operation()
            old = self.placement(op)
            op.snapshot.before_state_json = '{}'
            db.session.commit()
            with self.assertRaises(service.QuarantineConflict):
                service.place(**old)
            doc = self.preview(op)
            self.assertEqual(doc['scope']['kind'], 'account')
            self.place(op)
            self.assertEqual(Hold.query.one().scope_kind, 'account')

    def test_foreign_actor_origin_type_and_versions_denied(self):
        with self.app.app_context():
            op = self.operation()
            base = self.placement(op)
            for fields in ({'seller_id': self.seller2_id}, {'origin_id': op.id + 999},
                    {'actor_user_id': self.actor(self.seller2_id)}, {'actor_user_id': None},
                    {'origin_type': 'future'}, {'expected_version': True}, {'expected_version': 1.0},
                    {'expected_version': 0}, {'expected_version': op.version + 1},
                    {'confirm_scope': 1}, {'scope_token': 'b'*64}, {'scope_token': []},
                    {'reason': 'short'}, {'reason': 'x'*1001}, {'reason': 'a'*10 + '\u202e'}):
                with self.subTest(fields=fields), self.assertRaises(service.QuarantineError):
                    service.place(**dict(base, **fields))
                self.assertEqual(Hold.query.count(), 0)
            self.assertEqual(Event.query.count(), 0)

    def test_inactive_actor_cannot_place(self):
        with self.app.app_context():
            op = self.operation()
            args = self.placement(op)
            op.account.seller.user.is_active = False
            db.session.commit()
            with self.assertRaises(service.QuarantineError):
                service.place(**args)

    def test_blocked_existing_session_cannot_place_note_or_release(self):
        with self.app.app_context():
            op = self.operation()
            args = self.placement(op)
            owner = op.account.seller.user
            owner.blocked_at = datetime.utcnow()
            db.session.commit()
            with self.assertRaises(service.QuarantineForbidden):
                service.place(**args)
            owner.blocked_at = None
            db.session.commit()
            self.place(op)
            self.confirm(op)
            doc = self.preview(op)
            args = dict(seller_id=op.seller_id, origin_id=op.id, expected_version=doc['hold']['version'],
                expected_operation_version=doc['operation_version'], actor_user_id=self.actor(),
                reason='Имеется действующее подтверждение.', confirm_release=True)
            owner.blocked_at = datetime.utcnow()
            db.session.commit()
            for action in ('note_added', 'released'):
                with self.subTest(action=action), self.assertRaises(service.QuarantineForbidden):
                    service.update_decision(**args, action=action)
            self.assertEqual(Event.query.count(), 1)

    def test_only_attempted_unknown_is_eligible(self):
        with self.app.app_context():
            op = self.operation()
            for status, attempt in [('queued', 0), ('uncertain', 0), ('partial', 1), ('failed', 1), ('succeeded', 1), ('polling', 1)]:
                op.status, op.attempt_count = status, attempt
                db.session.commit()
                self.assertFalse(self.preview(op)['can_place'])
                with self.subTest(status=status, attempt=attempt), self.assertRaises(service.QuarantineConflict):
                    self.place(op)

    def test_busy_account_does_not_create_a_decision(self):
        with self.app.app_context():
            op = self.operation()
            args = self.placement(op)
            claim = try_account_operation_lock(op.account_id)
            try:
                with self.assertRaises(service.QuarantineBusy):
                    service.place(**args)
            finally:
                claim.close()
            self.assertEqual(Hold.query.count(), 0)
            self.assertIsNotNone(op.next_poll_at)

    def test_lost_response_replay_and_stale_note_do_not_duplicate_events(self):
        with self.app.app_context():
            op = self.operation()
            args = self.placement(op)
            service.place(**args)
            with self.assertRaises(service.QuarantineConflict):
                service.place(**args)
            self.assertEqual(Event.query.count(), 1)
            doc = self.preview(op)
            self.decision(op)
            with self.assertRaises(service.QuarantineConflict):
                self.decision(op, expected_version=doc['hold']['version'])
            self.assertEqual(Event.query.count(), 2)

    def test_atomic_audit_failure_keeps_original_operation(self):
        with self.app.app_context():
            op = self.operation()
            viewed = op.version
            with patch.object(service, '_append', side_effect=RuntimeError('synthetic audit failure')):
                with self.assertRaises(RuntimeError):
                    self.place(op)
            self.assertEqual(Hold.query.count(), 0)
            self.assertEqual(Event.query.count(), 0)
            self.assertEqual(op.version, viewed)
            self.assertEqual(op.quota_reserved, 1)
            self.assertIsNotNone(op.next_poll_at)

    def test_dirty_and_flushed_caller_transaction_is_preserved(self):
        with self.app.app_context():
            op = self.operation()
            args = self.placement(op)
            for flush in (False, True):
                op.account.label = 'Незавершённое изменение'
                if flush:
                    db.session.flush()
                with self.subTest(flush=flush), self.assertRaises(service.QuarantineConflict):
                    service.place(**args)
                self.assertEqual(op.account.label, 'Незавершённое изменение')
                if flush:
                    self.assertTrue(db.session.connection().connection.driver_connection.in_transaction)
                db.session.rollback()
            self.assertEqual(Hold.query.count(), 0)

    def test_note_keeps_scope_and_outcome_and_is_bounded_paginated(self):
        with self.app.app_context():
            op = self.operation()
            self.place(op)
            hold = Hold.query.one()
            identity = (hold.scope_kind, hold.offer_id, hold.product_id, hold.reviewed_scope_token)
            for _ in range(32):
                self.decision(op)
            doc = self.preview(op)
            self.assertEqual(len(doc['events']), 30)
            last = self.preview(op, before_id=doc['next_before_id'])
            self.assertEqual(len(last['events']), 3)
            self.assertIsNone(last['next_before_id'])
            self.assertEqual(len({e['id'] for e in doc['events'] + last['events']}), 33)
            self.assertEqual(op.status, 'uncertain')
            self.assertEqual(identity, (hold.scope_kind, hold.offer_id, hold.product_id, hold.reviewed_scope_token))

    def test_newer_success_does_not_release_original_unknown(self):
        with self.app.app_context():
            op = self.operation()
            self.place(op)
            other = self.operation(op.account)
            self.confirm(other)
            self.assertFalse(self.preview(op)['can_release'])
            with self.assertRaises(service.QuarantineConflict):
                self.decision(op, action='released', confirm_release=True)
            self.assertEqual(Hold.query.one().status, 'active')

    def test_release_requires_original_proven_contract_for_each_kind(self):
        with self.app.app_context():
            account = self._save()
            for kind in ['price_update', 'price_rollback', 'stock_update', 'stock_rollback',
                         'product_import', 'product_update', 'product_update_rollback', 'product_import_rollback']:
                with self.subTest(kind=kind):
                    op = self.operation(account, kind=kind)
                    self.place(op)
                    self.confirm(op)
                    self.assertTrue(self.preview(op)['can_release'])
                    self.decision(op, action='released', confirm_release=True)
                    self.assertEqual(self.preview(op)['hold']['status'], 'released')
                    self.assertEqual(op.attempt_count, 1)
                    self.assertEqual(op.status, 'succeeded')
                    self.assertIsNone(op.next_poll_at)

    def test_status_alone_partial_and_foreign_snapshot_never_allow_release(self):
        with self.app.app_context():
            op = self.operation()
            self.place(op)
            op.status = 'succeeded'
            op.completed_at = datetime.utcnow() + timedelta(seconds=1)
            db.session.commit()
            self.assertFalse(self.preview(op)['can_release'])

            self.confirm(op)
            self.assertTrue(self.preview(op)['can_release'])
            for fields in ({'status': 'partial'}, {'error_code': 'still_unconfirmed'},
                    {'completed_at': datetime(2020, 1, 1)}, {'next_poll_at': datetime.utcnow()}):
                saved = {key: getattr(op, key) for key in fields}
                for key, value in fields.items():
                    setattr(op, key, value)
                db.session.commit()
                self.assertFalse(self.preview(op)['can_release'])
                for key, value in saved.items():
                    setattr(op, key, value)
                db.session.commit()
            op.snapshot.seller_id = self.seller2_id
            db.session.commit()
            self.assertFalse(self.preview(op)['can_release'])

    def test_corrupt_publication_proof_cannot_release_even_with_succeeded_status(self):
        with self.app.app_context():
            account = self._save()
            for kind in ('product_import', 'product_update', 'product_update_rollback'):
                op = self.operation(account, kind=kind)
                self.place(op)
                self.confirm(op)
                self.assertTrue(self.preview(op)['can_release'])
                snapshot = op.snapshot
                original = (snapshot.confirmed_state_json, snapshot.confirmed_fingerprint, op.item_results_json)
                state = json.loads(snapshot.confirmed_state_json)
                for state_change, fp, results in [
                    ({}, '0'*64, original[2]),
                    ({'dimensions': None}, original[1], original[2]),
                    ({'source': 'operator_checkbox'}, original[1], original[2]),
                    ({'confirmed_at': '2020-01-01T00:00:00'}, original[1], original[2]),
                    ({'external_product_id': '777'}, original[1], original[2]),
                    ({}, original[1], '[]'),
                    ({}, original[1], '{malformed'),
                ]:
                    with self.subTest(kind=kind, change=state_change, fp=fp, results=results):
                        snapshot.confirmed_state_json = json.dumps(dict(state, **state_change))
                        snapshot.confirmed_fingerprint = fp
                        op.item_results_json = results
                        db.session.commit()
                        self.assertFalse(self.preview(op)['can_release'])
                snapshot.confirmed_state_json, snapshot.confirmed_fingerprint, op.item_results_json = original
                db.session.commit()

    def test_task_failed_evidence_allows_release_without_claiming_success(self):
        with self.app.app_context():
            op = self.operation(kind='product_update')
            self.place(op)
            result = {'total': 1, 'aggregate_status': 'failed',
                'items': [{'offer_id': 'EXACT-01', 'status': 'failed', 'errors': [{'code': 'rejected'}]}]}
            op.status = 'failed'
            op.completed_at = datetime.utcnow() + timedelta(seconds=1)
            op.error_code = 'ozon_import_failed'
            op.snapshot.confirmed_state_json = json.dumps({'source': 'task_status', 'result': result, 'confirmed_at': '2026-09-26'})
            op.snapshot.confirmed_fingerprint = OzonProductImportContract.fingerprint(result)
            db.session.commit()
            self.assertTrue(self.preview(op)['can_release'])
            self.decision(op, action='released', confirm_release=True)
            self.assertEqual(op.status, 'failed')

    def test_release_review_and_confirmation_cannot_be_skipped(self):
        with self.app.app_context():
            op = self.operation()
            self.place(op)
            doc = self.preview(op)
            self.confirm(op)
            for fields in ({'confirm_release': False}, {'confirm_release': 1},
                    {'expected_operation_version': doc['operation_version']}, {'expected_version': 999}):
                with self.subTest(fields=fields), self.assertRaises(service.QuarantineError):
                    self.decision(op, **(dict(action='released', confirm_release=True) | fields))
            self.assertEqual(Hold.query.one().status, 'active')

    def test_match_offer_reuse_product_rename_and_independent_scope(self):
        with self.app.app_context():
            op = self.operation()
            self.place(op)
            base = dict(seller_id=op.seller_id, marketplace_id=op.marketplace_id, account_id=op.account_id)
            for offer, product, blocked in [('EXACT-01', '888', True), ('RENAMED', '999', True),
                    ('OTHER', '888', False), ('EXACT-01', None, True)]:
                with self.subTest(offer=offer, product=product):
                    scope = QuarantineScope('product', offer, product, 'immutable_target_verified')
                    self.assertEqual(service.matching_hold(**base, scope=scope) is not None, blocked)
            scope = QuarantineScope('account', None, None, 'identity_unknown')
            self.assertIsNotNone(service.matching_hold(**base, scope=scope))
            for field in ['seller_id', 'marketplace_id', 'account_id']:
                other = dict(base, **{field: base[field] + 999})
                self.assertIsNone(service.matching_hold(**other, scope=scope))

    def test_account_fallback_matches_any_product(self):
        with self.app.app_context():
            op = self.operation()
            op.snapshot.before_state_json = '{}'
            db.session.commit()
            self.place(op)
            query = dict(seller_id=op.seller_id, marketplace_id=op.marketplace_id, account_id=op.account_id,
                scope=QuarantineScope('product', 'UNRELATED', '777', 'immutable_target_verified'))
            self.assertIsNotNone(service.matching_hold(**query))

    def test_manual_reconciliation_stays_stopped_and_due_selection_excludes_origin(self):
        with self.app.app_context():
            op = self.operation()
            self.place(op)
            op.next_poll_at = datetime.utcnow()
            db.session.commit()  # Simulate read workflow committing before its caller returns.
            self.assertEqual(MarketplaceOperation.query.filter(service.automatic_reconciliation_allowed()).count(), 0)
            service.keep_reconciliation_stopped(op)
            self.assertIsNone(op.next_poll_at)
            self.assertIsNone(service.operation_hold(op))
            queued = self.operation(op.account, status='queued', attempt_count=0)
            self.assertEqual(MarketplaceOperation.query.filter(service.automatic_reconciliation_allowed()).count(), 1)
            self.assertIsNotNone(service.operation_hold(queued))
