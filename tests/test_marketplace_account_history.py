"""Account-local settings, atomic audit and reviewed default selection."""
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from models import db, MarketplaceAccountEvent, Seller, SellerMarketplaceAccount
from services.marketplace_account_history import history_page
from services.marketplace_accounts import (MarketplaceAccountService as Accounts,
    MarketplaceAccountConflict, MarketplaceAccountValidationError,
    MarketplaceAccountVersionConflict, MarketplaceAccountConfigurationError)
from services.marketplace_operation_locks import try_account_operation_lock
from tests import test_marketplace_accounts as fixtures


class AccountHistoryTest(unittest.TestCase):
    setUp = fixtures.MarketplaceAccountsTest.setUp
    tearDown = fixtures.MarketplaceAccountsTest.tearDown
    _create_seller = staticmethod(fixtures.MarketplaceAccountsTest._create_seller)
    _user = staticmethod(fixtures.MarketplaceAccountsTest._user)
    _operation = staticmethod(fixtures.MarketplaceAccountsTest._operation)
    _save = fixtures.MarketplaceAccountsTest._save
    _login_patches = fixtures.MarketplaceAccountsTest._login_patches

    def actor(self, seller_id=None):
        return db.session.get(Seller, seller_id or self.seller1_id).user_id

    def settings(self, account, **overrides):
        args = dict(seller_id=account.seller_id, account_id=account.id,
                    external_account_id=account.external_account_id,
                    expected_version=account.version, label='Обновлённый магазин',
                    default_vat='0.22', actor_user_id=self.actor())
        args.update(overrides)
        return Accounts.save_settings(**args)

    def events(self, account):
        return MarketplaceAccountEvent.query.filter_by(account_id=account.id).order_by(MarketplaceAccountEvent.id).all()

    def test_settings_preserve_key_provider_state_and_uncertain_operation(self):
        with self.app.app_context():
            account = self._save(default_vat='0.2')
            operation = self._operation(account, status='uncertain', attempt_count=1)
            account.connection_status = 'connected'
            account.credential_expires_at = datetime(2030, 1, 1)
            account.capabilities_json = '{"product_write":true}'
            db.session.commit()
            fields = ('_credentials_encrypted', 'credential_version', 'credential_expires_at',
                      'capabilities_json', 'connection_checked_at', 'connection_status', 'is_active', 'is_default')
            before = {key: getattr(account, key) for key in fields}
            op_before = (operation.status, operation.attempt_count, operation.request_summary_json)
            with patch.object(SellerMarketplaceAccount, 'get_credentials', side_effect=AssertionError('decrypt forbidden')):
                self.settings(account)
            self.assertEqual(before, {key: getattr(account, key) for key in fields})
            self.assertEqual(op_before, (operation.status, operation.attempt_count, operation.request_summary_json))
            event = self.events(account)[-1]
            self.assertEqual(event.action, 'settings_changed')
            self.assertEqual(event.actor_user_id, self.actor())
            self.assertEqual(set(json.loads(event.changes_json)), {'label', 'default_vat'})
            self.assertEqual(event.account_version_after, event.account_version_before + 1)
            self.assertEqual(event.credential_version_after, event.credential_version_before)

    def test_disconnected_settings_do_not_reactivate_and_empty_vat_is_unknown(self):
        with self.app.app_context():
            account = self._save(default_vat='0.2')
            Accounts.disconnect(seller_id=account.seller_id, account_id=account.id)
            self.settings(account, default_vat='')
            self.assertFalse(account.is_active)
            self.assertFalse(account.has_credentials)
            self.assertFalse(account.is_default)
            self.assertIsNone(account.public_settings['default_vat'])

    def test_noop_creates_no_version_or_history(self):
        with self.app.app_context():
            account = self._save(default_vat='0.2')
            before = (account.version, len(self.events(account)))
            self.settings(account, label=account.label, default_vat=None)
            self.assertEqual(before, (account.version, len(self.events(account))))

    def test_stale_settings_and_lost_response_cannot_double_save(self):
        with self.app.app_context():
            account = self._save()
            viewed = account.version
            self.settings(account)
            with self.assertRaises(MarketplaceAccountVersionConflict):
                self.settings(account, expected_version=viewed)
            self.assertEqual(len(self.events(account)), 2)

    def test_physical_lock_blocks_settings_without_event(self):
        with self.app.app_context():
            account = self._save()
            claim = try_account_operation_lock(account.id)
            self.assertIsNotNone(claim)
            try:
                with self.assertRaises(MarketplaceAccountConflict):
                    self.settings(account)
            finally:
                claim.close()
            self.assertEqual(len(self.events(account)), 1)

    def test_foreign_actor_cannot_write_audit_or_account(self):
        with self.app.app_context():
            account = self._save()
            original = account.label
            with self.assertRaises(MarketplaceAccountValidationError):
                self.settings(account, actor_user_id=self.actor(self.seller2_id))
            self.assertEqual(account.label, original)
            self.assertEqual(len(self.events(account)), 1)

    def test_audit_failure_rolls_back_account(self):
        with self.app.app_context():
            account = self._save()
            before = (account.label, account.version, account.settings_json)
            with patch('services.marketplace_accounts.append_event', side_effect=RuntimeError('audit unavailable')):
                with self.assertRaises(RuntimeError):
                    self.settings(account)
            self.assertEqual(before, (account.label, account.version, account.settings_json))
            self.assertEqual(len(self.events(account)), 1)

    def test_dirty_session_is_rejected_without_discarding_callers_changes(self):
        with self.app.app_context():
            account = self._save()
            account_id, version = account.id, account.version
            account.label = 'Незавершённое изменение'
            with self.assertRaises(MarketplaceAccountConflict):
                Accounts.save_settings(seller_id=self.seller1_id, account_id=account_id,
                    external_account_id='123456', label='Другое', expected_version=version)
            self.assertIn(account, db.session.dirty)
            self.assertEqual(account.label, 'Незавершённое изменение')
            db.session.flush()
            with self.assertRaises(MarketplaceAccountConflict):
                Accounts.save_ozon_account(seller_id=self.seller1_id, external_account_id='987654',
                    label='Ещё магазин', api_key='synthetic-second-key')
            db.session.commit()
            self.assertEqual(db.session.get(SellerMarketplaceAccount, account.id).label, 'Незавершённое изменение')

    def test_invalid_versions_settings_and_malformed_saved_settings_fail_closed(self):
        with self.app.app_context():
            account = self._save()
            for value in (True, False, 0, -1, '1', 1.2, None):
                with self.subTest(value=value), self.assertRaises(MarketplaceAccountValidationError):
                    self.settings(account, expected_version=value)
            for value in ('0.99', False, 0, {}, []):
                with self.subTest(vat=value), self.assertRaises(MarketplaceAccountValidationError):
                    self.settings(account, default_vat=value)
            account.settings_json = '[]'
            db.session.commit()
            with self.assertRaises(MarketplaceAccountConfigurationError):
                self.settings(account)
            self.assertEqual(len(self.events(account)), 1)

    def test_default_switch_versions_both_accounts_and_requires_observed_context(self):
        with self.app.app_context():
            first = self._save()
            second = self._save(external_account_id='222222')
            viewed = {'id': first.id, 'version': first.version}
            versions = (first.version, second.version)
            Accounts.set_default(seller_id=self.seller1_id, account_id=second.id,
                expected_version=second.version, expected_default=viewed, actor_user_id=self.actor())
            self.assertFalse(first.is_default)
            self.assertTrue(second.is_default)
            self.assertEqual((first.version, second.version), (versions[0]+1, versions[1]+1))
            self.assertEqual(self.events(first)[-1].action, 'default_changed')
            self.assertEqual(self.events(second)[-1].actor_user_id, self.actor())
            with self.assertRaises(MarketplaceAccountVersionConflict):
                Accounts.set_default(seller_id=self.seller1_id, account_id=first.id,
                    expected_version=first.version, expected_default=viewed)
            self.assertTrue(second.is_default)

    def test_default_group_claim_busy_and_atomic_audit_failure(self):
        with self.app.app_context():
            first = self._save()
            second = self._save(external_account_id='222222')
            claim = try_account_operation_lock(first.id)
            try:
                with self.assertRaises(MarketplaceAccountConflict):
                    Accounts.set_default(seller_id=self.seller1_id, account_id=second.id)
            finally:
                claim.close()
            from services.marketplace_account_history import append_event
            def fail_second(account, *args):
                if account.id == second.id:
                    raise RuntimeError('second event failed')
                return append_event(account, *args)
            with patch('services.marketplace_accounts.append_event', side_effect=fail_second):
                with self.assertRaises(RuntimeError):
                    Accounts.set_default(seller_id=self.seller1_id, account_id=second.id)
            self.assertTrue(first.is_default)
            self.assertFalse(second.is_default)
            self.assertEqual(len(self.events(first)), 1)
            self.assertEqual(len(self.events(second)), 1)

    def test_disconnect_versions_replacement_and_refuses_uncertain_or_stale(self):
        with self.app.app_context():
            first = self._save()
            second = self._save(external_account_id='222222')
            with self.assertRaises(MarketplaceAccountVersionConflict):
                Accounts.disconnect(seller_id=self.seller1_id, account_id=first.id, expected_version=first.version+1)
            previous = second.version
            Accounts.disconnect(seller_id=self.seller1_id, account_id=first.id,
                expected_version=first.version, actor_user_id=self.actor())
            self.assertEqual(second.version, previous+1)
            self.assertTrue(second.is_default)
            self.assertEqual(self.events(first)[-1].action, 'disconnected')
            self._operation(second, status='uncertain', attempt_count=1)
            count = len(self.events(second))
            with self.assertRaises(MarketplaceAccountConflict):
                Accounts.disconnect(seller_id=self.seller1_id, account_id=second.id, expected_version=second.version)
            self.assertTrue(second.has_credentials)
            self.assertEqual(len(self.events(second)), count)

    def test_key_event_does_not_store_credential_or_claim_access(self):
        with self.app.app_context():
            account = self._save()
            Accounts.rotate_ozon_key(seller_id=self.seller1_id, account_id=account.id,
                external_account_id=account.external_account_id, api_key='synthetic-rotated-secret',
                expected_version=account.version, actor_user_id=self.actor())
            event = self.events(account)[-1]
            self.assertEqual(event.action, 'key_replaced')
            # This is the encryption envelope format, not a rotation counter.
            self.assertEqual(event.credential_version_after, event.credential_version_before)
            self.assertEqual(event.account_version_after, event.account_version_before+1)
            data = history_page(seller_id=self.seller1_id, account_id=account.id, viewer_user_id=self.actor())
            rendered = json.dumps(data, ensure_ascii=False)
            for forbidden in ('synthetic', 'encrypted', 'fingerprint', 'credential_version', 'actor_user_id'):
                self.assertNotIn(forbidden, rendered)
            self.assertEqual(data['items'][0]['actor'], 'Вы')
            self.assertIn('проверяется отдельно', data['items'][0]['hint'])

    def test_history_keyset_and_poisoned_changes_are_bounded_and_read_only(self):
        with self.app.app_context():
            account = self._save()
            for number in range(34):
                self.settings(account, label=f'Магазин {number}')
            event = self.events(account)[-1]
            event.changes_json = json.dumps({'api_key': 'never-show-this', 'label': {'before': 'a'*10000, 'after':'x'}})
            db.session.commit()
            before = (account.version, len(self.events(account)))
            page = history_page(seller_id=self.seller1_id, account_id=account.id, viewer_user_id=self.actor())
            self.assertEqual(len(page['items']), 30)
            self.assertEqual(page['items'][0]['changes'], [])
            later = history_page(seller_id=self.seller1_id, account_id=account.id,
                viewer_user_id=self.actor(), before_id=page['next_before_id'])
            self.assertEqual(len(later['items']), 5)
            self.assertIsNone(later['next_before_id'])
            self.assertFalse({x['id'] for x in page['items']} & {x['id'] for x in later['items']})
            self.assertEqual(before, (account.version, len(self.events(account))))
            self.assertEqual(later['items'][-1]['actor'], 'Инициатор не записан')

    def test_routes_validate_scope_actor_version_unknown_fields_and_history_cursor(self):
        with self.app.app_context():
            account = self._save()
            account_id, version = account.id, account.version
            foreign = self._save(seller_id=self.seller2_id)
            foreign_id = foreign.id
        p1, p2 = self._login_patches(self.seller1_id)
        with p1, p2:
            payload = dict(label='Из формы', client_id='123456', default_vat='0.22', expected_version=version)
            path = f'/marketplaces/accounts/{account_id}'
            for changes in ({'api_key':'forbidden'}, {'actor_user_id':999}, {'expected_version':True}, {'expected_version':str(version)}):
                response = self.client.post(path, json={**payload, **changes})
                self.assertEqual(response.status_code, 400)
            response = self.client.post(path, json=payload)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(self.client.post(path, json=payload).status_code, 409)
            response = self.client.get(path+'/history')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(response.get_json()['items'][0]['actor'], 'Вы')
            for suffix in ('?seller_id=2', '?before_id=-1', '?before_id=true', '?before_id=1&before_id=2'):
                self.assertEqual(self.client.get(path+'/history'+suffix, headers={'Accept':'application/json'}).status_code, 400)
            self.assertEqual(self.client.get(f'/marketplaces/accounts/{foreign_id}/history', headers={'Accept':'application/json'}).status_code, 404)
            self.assertEqual(self.client.post(path+'/default', json={}).status_code, 400)
            self.assertEqual(self.client.post(path+'/disconnect', json={}).status_code, 400)
        with self.app.app_context():
            self.assertEqual(MarketplaceAccountEvent.query.filter_by(account_id=account_id).count(), 2)
