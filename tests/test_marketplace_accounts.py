# -*- coding: utf-8 -*-
"""Encrypted seller marketplace accounts and tenant-scoped routes."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import os
import unittest

from cryptography.fernet import Fernet
from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    BackgroundJob,
    ImportedProduct,
    Marketplace,
    MarketplaceCanonicalContentProposal,
    MarketplaceListing,
    MarketplaceOperation,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_accounts import register_marketplace_account_routes
from services.marketplace_accounts import (
    MarketplaceAccountConflict,
    MarketplaceAccountConfigurationError,
    MarketplaceAccountNotFound,
    MarketplaceAccountService,
    MarketplaceAccountValidationError,
)
from services.marketplace_adapters.types import ConnectionCheck


class MarketplaceAccountsTest(unittest.TestCase):
    def setUp(self):
        self.previous_encryption_key = os.environ.get("ENCRYPTION_KEY")
        os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")

        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-account-tests",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_account_routes(self.app)
        self.app.add_url_rule(
            "/api-settings",
            endpoint="api_settings",
            view_func=lambda: "api settings",
        )
        self.client = self.app.test_client()

        with self.app.app_context():
            db.create_all()
            self.seller1_id = self._create_seller("seller-one", "one@example.test")
            self.seller2_id = self._create_seller("seller-two", "two@example.test")
            ozon = Marketplace(
                name="Ozon",
                code="ozon",
                adapter_code="ozon",
                api_base_url="https://api-seller.ozon.ru",
                is_active=True,
            )
            wb = Marketplace(
                name="Wildberries",
                code="wb",
                adapter_code="wb",
                is_active=True,
            )
            db.session.add_all([ozon, wb])
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        if self.previous_encryption_key is None:
            os.environ.pop("ENCRYPTION_KEY", None)
        else:
            os.environ["ENCRYPTION_KEY"] = self.previous_encryption_key

    @staticmethod
    def _create_seller(username, email):
        user = User(
            username=username,
            email=email,
            is_active=True,
            is_admin=False,
        )
        user.set_password("synthetic-password")
        seller = Seller(user=user, company_name=username)
        db.session.add(seller)
        db.session.commit()
        return seller.id

    @staticmethod
    def _user(seller_id=None):
        seller = SimpleNamespace(id=seller_id) if seller_id is not None else None
        return SimpleNamespace(
            id=100,
            seller=seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _login_patches(self, seller_id):
        user = self._user(seller_id)
        if seller_id is not None:
            with self.app.app_context():
                user.id = db.session.get(Seller, seller_id).user_id
        return (
            patch("routes.marketplace_accounts.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def _save(self, seller_id=None, **overrides):
        values = {
            "seller_id": seller_id or self.seller1_id,
            "external_account_id": "123456",
            "label": "Основной Ozon",
            "api_key": "synthetic-ozon-key",
            "is_default": False,
        }
        values.update(overrides)
        return MarketplaceAccountService.save_ozon_account(**values)

    @staticmethod
    def _operation(account, *, status, attempt_count=0):
        operation = MarketplaceOperation(
            seller_id=account.seller_id,
            marketplace_id=account.marketplace_id,
            account_id=account.id,
            operation_kind="product_import",
            status=status,
            idempotency_key=f"disconnect-{status}-{attempt_count}",
            request_fingerprint="a" * 64,
            contract_version="ozon-product-import-v3-2026-07-10",
            request_summary_json="{}",
            quota_snapshot_json="{}",
            quota_reserved=1,
            attempt_count=attempt_count,
            provider_request_ids_json="[]",
            item_results_json="[]",
        )
        db.session.add(operation)
        db.session.commit()
        return operation

    def test_credentials_are_encrypted_and_absent_from_public_contract(self):
        with self.app.app_context():
            account = self._save()
            raw = account._credentials_encrypted
            public_json = json.dumps(account.to_public_dict(), ensure_ascii=False)
            self.assertNotEqual(raw, "synthetic-ozon-key")
            self.assertNotIn("synthetic-ozon-key", raw)
            self.assertNotIn("synthetic-ozon-key", public_json)
            self.assertNotIn("synthetic-ozon-key", repr(account))
            self.assertEqual(account.get_credentials()["api_key"], "synthetic-ozon-key")
            self.assertTrue(account.is_default)

    def test_default_vat_is_validated_and_exposed_without_other_settings(self):
        with self.app.app_context():
            account = self._save(default_vat="0.22")
            account.settings_json = json.dumps({
                "default_vat": "0.22",
                "internal_note": "must-not-leak",
            })
            db.session.commit()

            public = account.to_public_dict()
            self.assertEqual(public["settings"], {"default_vat": "0.22"})
            self.assertNotIn("must-not-leak", json.dumps(public))

            updated = MarketplaceAccountService.save_ozon_account(
                seller_id=self.seller1_id,
                account_id=account.id,
                external_account_id=account.external_account_id,
                label=account.label,
                api_key=None,
                default_vat="0.1",
            )
            self.assertEqual(updated.public_settings["default_vat"], "0.1")

            with self.assertRaises(MarketplaceAccountValidationError):
                MarketplaceAccountService.save_ozon_account(
                    seller_id=self.seller1_id,
                    account_id=account.id,
                    external_account_id=account.external_account_id,
                    label=account.label,
                    api_key=None,
                    default_vat="18%",
                )

    def test_new_credentials_fail_closed_without_encryption_key(self):
        with self.app.app_context():
            os.environ.pop("ENCRYPTION_KEY", None)
            with self.assertRaises(MarketplaceAccountConfigurationError):
                self._save()
            self.assertEqual(SellerMarketplaceAccount.query.count(), 0)

    def test_account_lookup_and_mutations_require_account_plus_seller(self):
        with self.app.app_context():
            account = self._save()
            with self.assertRaises(MarketplaceAccountNotFound):
                MarketplaceAccountService.get_owned_account(
                    seller_id=self.seller2_id,
                    account_id=account.id,
                )
            with self.assertRaises(MarketplaceAccountNotFound):
                MarketplaceAccountService.disconnect(
                    seller_id=self.seller2_id,
                    account_id=account.id,
                )
            self.assertTrue(account.has_credentials)

    def test_connection_check_redacts_adapter_error_before_persisting(self):
        with self.app.app_context():
            account = self._save(api_key="never-persist-this-key")
            adapter = MagicMock()
            adapter.check_connection.return_value = ConnectionCheck(
                ok=False,
                status="error",
                external_account_id="123456",
                error_code="synthetic_error",
                error_message="provider echoed never-persist-this-key",
            )
            registry = MagicMock()
            registry.get.return_value = adapter
            checked, result = MarketplaceAccountService.check_connection(
                seller_id=self.seller1_id,
                account_id=account.id,
                registry=registry,
            )
            self.assertFalse(result.ok)
            self.assertEqual(checked.connection_status, "error")
            self.assertNotIn("never-persist-this-key", checked.last_error_message)
            self.assertIn("[redacted]", checked.last_error_message)
            adapter.check_connection.assert_called_once()

    def test_untrusted_adapter_exception_is_not_persisted(self):
        with self.app.app_context():
            account = self._save(api_key="exception-secret-key")
            adapter = MagicMock()
            adapter.check_connection.side_effect = RuntimeError(
                "provider echoed exception-secret-key"
            )
            registry = MagicMock()
            registry.get.return_value = adapter
            checked, result = MarketplaceAccountService.check_connection(
                seller_id=self.seller1_id,
                account_id=account.id,
                registry=registry,
            )
            self.assertFalse(result.ok)
            self.assertNotIn("exception-secret-key", checked.last_error_message)
            self.assertEqual(
                checked.last_error_code,
                "adapter_connection_check_failed",
            )

    def test_default_is_scoped_to_seller_and_marketplace(self):
        with self.app.app_context():
            first = self._save(external_account_id="1")
            second = self._save(external_account_id="2")
            self.assertTrue(first.is_default)
            self.assertFalse(second.is_default)
            MarketplaceAccountService.set_default(
                seller_id=self.seller1_id,
                account_id=second.id,
            )
            db.session.refresh(first)
            self.assertFalse(first.is_default)
            self.assertTrue(second.is_default)

    def test_disconnect_removes_secret_and_promotes_replacement(self):
        with self.app.app_context():
            first = self._save(external_account_id="1")
            second = self._save(external_account_id="2")
            MarketplaceAccountService.disconnect(
                seller_id=self.seller1_id,
                account_id=first.id,
            )
            db.session.refresh(second)
            self.assertFalse(first.has_credentials)
            self.assertEqual(first.connection_status, "disconnected")
            self.assertTrue(second.is_default)

    def test_disconnect_cancels_only_never_submitted_queue(self):
        with self.app.app_context():
            account = self._save()
            operation = self._operation(
                account,
                status="queued",
                attempt_count=0,
            )

            MarketplaceAccountService.disconnect(
                seller_id=self.seller1_id,
                account_id=account.id,
            )

            db.session.refresh(account)
            db.session.refresh(operation)
            self.assertFalse(account.has_credentials)
            self.assertEqual(operation.status, "cancelled")
            self.assertEqual(
                operation.error_code,
                "account_disconnected_before_submission",
            )
            self.assertEqual(operation.quota_reserved, 0)
            self.assertIsNotNone(operation.completed_at)

    def test_pending_canonical_diff_blocks_identity_edit_and_conflicts_on_disconnect(self):
        with self.app.app_context():
            account = self._save()
            product = ImportedProduct(
                seller_id=self.seller1_id,
                external_id="account-lifecycle-source",
                external_vendor_code="account-lifecycle-offer",
                source_type="synthetic",
                title="Canonical title",
            )
            db.session.add(product)
            db.session.flush()
            listing = MarketplaceListing(
                seller_id=self.seller1_id,
                marketplace_id=account.marketplace_id,
                account_id=account.id,
                imported_product_id=product.id,
                offer_id="account-lifecycle-offer",
                external_product_id="801001",
                title="Ozon title",
                normalized_status="active",
                link_status="linked",
                sync_fingerprint="e" * 64,
            )
            db.session.add(listing)
            db.session.flush()
            proposal = MarketplaceCanonicalContentProposal(
                seller_id=self.seller1_id,
                marketplace_id=account.marketplace_id,
                account_id=account.id,
                listing_id=listing.id,
                imported_product_id=product.id,
                created_by_user_id=db.session.get(
                    Seller,
                    self.seller1_id,
                ).user_id,
                status="pending_review",
                fields_json='["title"]',
                baseline_state_json='{"title":"Canonical title"}',
                proposed_state_json='{"title":"Ozon title"}',
                baseline_fingerprint="f" * 64,
                source_fingerprint="1" * 64,
                source_observed_at=datetime.utcnow(),
            )
            db.session.add(proposal)
            db.session.commit()

            with self.assertRaises(MarketplaceAccountConflict):
                MarketplaceAccountService.save_ozon_account(
                    seller_id=self.seller1_id,
                    account_id=account.id,
                    external_account_id="different-client-id",
                    label=account.label,
                    api_key=None,
                )

            MarketplaceAccountService.disconnect(
                seller_id=self.seller1_id,
                account_id=account.id,
            )
            db.session.refresh(proposal)
            self.assertEqual(proposal.status, "conflict")
            self.assertEqual(
                proposal.error_code,
                "account_disconnected_before_review",
            )

    def test_disconnect_preserves_credentials_needed_for_reconciliation(self):
        for status, attempt_count in (
            ("uncertain", 1),
            ("submitted", 1),
            ("queued", 1),
        ):
            with self.subTest(status=status):
                with self.app.app_context():
                    account = self._save(
                        external_account_id=f"blocking-{status}",
                    )
                    operation = self._operation(
                        account,
                        status=status,
                        attempt_count=attempt_count,
                    )

                    with self.assertRaises(MarketplaceAccountConflict):
                        MarketplaceAccountService.disconnect(
                            seller_id=self.seller1_id,
                            account_id=account.id,
                        )

                    db.session.refresh(account)
                    db.session.refresh(operation)
                    self.assertTrue(account.has_credentials)
                    self.assertTrue(account.is_active)
                    self.assertEqual(operation.status, status)

    def test_pending_write_blocks_settings_but_allows_read_only_connection_check(self):
        with self.app.app_context():
            account = self._save()
            operation = self._operation(
                account,
                status="submitted",
                attempt_count=1,
            )
            registry = MagicMock()
            registry.get.return_value.check_connection.return_value = ConnectionCheck(
                ok=True, status='connected', external_account_id=account.external_account_id,
                capabilities=frozenset({'catalog_read'}), roles=(),
            )

            with self.assertRaises(MarketplaceAccountConflict):
                MarketplaceAccountService.save_ozon_account(
                    seller_id=self.seller1_id,
                    account_id=account.id,
                    external_account_id=account.external_account_id,
                    label="Изменённое имя",
                    api_key=None,
                )
            MarketplaceAccountService.check_connection(
                seller_id=self.seller1_id, account_id=account.id, registry=registry,
            )

            db.session.refresh(account)
            db.session.refresh(operation)
            self.assertEqual(account.label, "Основной Ozon")
            self.assertTrue(account.has_credentials)
            self.assertEqual(operation.status, "submitted")
            registry.get.return_value.check_connection.assert_called_once()

    def test_key_recovery_keeps_unknown_outcome_and_settings_and_checks_scope(self):
        with self.app.app_context():
            account = self._save(default_vat='0.22')
            account.credential_expires_at = datetime.utcnow() - timedelta(days=1)
            operation = self._operation(account, status='uncertain', attempt_count=1)
            db.session.commit()
            version = account.version
            with patch('requests.sessions.Session.request', side_effect=AssertionError('No HTTP')):
                updated = MarketplaceAccountService.rotate_ozon_key(
                    seller_id=self.seller1_id, account_id=account.id,
                    external_account_id=account.external_account_id, api_key='recovery-synthetic-key', expected_version=account.version)
            self.assertEqual(updated.get_credentials()['api_key'], 'recovery-synthetic-key')
            self.assertEqual(updated.version, version + 1)
            self.assertEqual(updated.label, 'Основной Ozon')
            self.assertEqual(updated.public_settings['default_vat'], '0.22')
            self.assertEqual(updated.connection_status, 'unchecked')
            self.assertEqual(updated.capabilities_json, '[]')
            self.assertIsNone(updated.credential_expires_at)
            db.session.refresh(operation)
            self.assertEqual((operation.status, operation.attempt_count), ('uncertain', 1))
            for seller_id, client_id, expected in [
                (self.seller2_id, account.external_account_id, MarketplaceAccountNotFound),
                (self.seller1_id, 'different-client', MarketplaceAccountConflict),
            ]:
                with self.assertRaises(expected):
                    MarketplaceAccountService.rotate_ozon_key(seller_id=seller_id, account_id=account.id,
                        external_account_id=client_id, api_key='must-not-save', expected_version=account.version)
            with patch('services.marketplace_accounts.try_account_operation_lock', return_value=None):
                with self.assertRaises(MarketplaceAccountConflict):
                    MarketplaceAccountService.rotate_ozon_key(seller_id=self.seller1_id, account_id=account.id,
                        external_account_id=account.external_account_id, api_key='must-not-save', expected_version=account.version)
            db.session.refresh(account)
            self.assertEqual(account.get_credentials()['api_key'], 'recovery-synthetic-key')

    def test_json_create_and_list_never_echo_api_key(self):
        user_patch, login_patch = self._login_patches(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.post(
                "/marketplaces/accounts/ozon",
                json={
                    "client_id": "777",
                    "label": "Кабинет 777",
                    "api_key": "route-secret-key",
                    "is_default": True,
                    "default_vat": "0.22",
                },
            )
            listed = self.client.get(
                "/marketplaces/accounts/api",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn("route-secret-key", response.get_data(as_text=True))
        self.assertNotIn("route-secret-key", listed.get_data(as_text=True))
        self.assertEqual(len(listed.get_json()["accounts"]), 1)
        self.assertEqual(
            listed.get_json()["accounts"][0]["settings"]["default_vat"],
            "0.22",
        )

    def test_html_index_marks_only_nonexpired_connected_vat_account_ready(self):
        with self.app.app_context():
            ready = self._save(default_vat="0.22")
            ready.connection_status = "connected"
            ready.credential_expires_at = datetime.utcnow() + timedelta(days=1)
            db.session.commit()
            expired = self._save(
                external_account_id="expired-client",
                label="Истёкший ключ",
                default_vat="0.22",
            )
            expired.connection_status = "connected"
            expired.credential_expires_at = datetime.utcnow() - timedelta(
                seconds=1,
            )
            db.session.commit()
            ready_id = ready.id
            expired_id = expired.id

        self.app.config["MARKETPLACE_OZON_PUBLICATION_ENABLED"] = True
        user_patch, login_patch = self._login_patches(self.seller1_id)
        with user_patch, login_patch, patch(
            "routes.marketplace_accounts.render_template",
            return_value="accounts",
        ) as render:
            response = self.client.get("/marketplaces/accounts/")

        self.assertEqual(response.status_code, 200)
        ready_ids = render.call_args.kwargs["upload_ready_account_ids"]
        self.assertIn(ready_id, ready_ids)
        self.assertNotIn(expired_id, ready_ids)

    def test_html_create_can_return_to_shared_api_settings(self):
        user_patch, login_patch = self._login_patches(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.post(
                "/marketplaces/accounts/ozon",
                data={
                    "client_id": "778",
                    "label": "Кабинет из API settings",
                    "api_key": "html-route-secret-key",
                    "is_default": "1",
                    "return_to": "api_settings",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/api-settings"))
        self.assertNotIn("html-route-secret-key", response.get_data(as_text=True))

    def test_foreign_route_mutations_are_not_found_and_do_not_call_adapter(self):
        with self.app.app_context():
            account_id = self._save().id
        user_patch, login_patch = self._login_patches(self.seller2_id)
        with user_patch, login_patch, patch(
            "services.marketplace_accounts.get_marketplace_registry"
        ) as registry:
            checked = self.client.post(
                f"/marketplaces/accounts/{account_id}/check",
                json={},
            )
            disconnected = self.client.post(
                f"/marketplaces/accounts/{account_id}/disconnect",
                json={},
            )
        self.assertEqual(checked.status_code, 404)
        self.assertEqual(disconnected.status_code, 404)
        registry.assert_not_called()
        with self.app.app_context():
            account = SellerMarketplaceAccount.query.filter_by(id=account_id).one()
            self.assertTrue(account.has_credentials)

    def test_non_seller_is_denied_before_query(self):
        user = self._user()
        with patch("routes.marketplace_accounts.current_user", user), patch(
            "flask_login.utils._get_user", return_value=user
        ), patch.object(MarketplaceAccountService, "list_accounts") as listed:
            response = self.client.get("/marketplaces/accounts/api")
        self.assertEqual(response.status_code, 403)
        listed.assert_not_called()

    def test_feature_flag_blocks_new_connection_but_allows_disconnect(self):
        with self.app.app_context():
            account = self._save()
            account_id, version = account.id, account.version
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._login_patches(self.seller1_id)
        with user_patch, login_patch:
            create = self.client.post(
                "/marketplaces/accounts/ozon",
                json={
                    "client_id": "999",
                    "label": "Blocked",
                    "api_key": "blocked-secret",
                    "is_default": False,
                },
            )
            disconnect = self.client.post(
                f"/marketplaces/accounts/{account_id}/disconnect",
                json={"expected_version": version},
            )
        self.assertEqual(create.status_code, 404)
        self.assertEqual(disconnect.status_code, 200)

    def test_write_routes_are_csrf_protected_when_enabled(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        user_patch, login_patch = self._login_patches(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.post(
                "/marketplaces/accounts/ozon",
                data={
                    "client_id": "888",
                    "label": "No CSRF",
                    "api_key": "not-saved",
                },
            )
        self.assertEqual(response.status_code, 400)

    def test_one_action_connection_queues_without_http_and_without_vat(self):
        user_patch, login_patch = self._login_patches(self.seller1_id)
        with user_patch, login_patch, patch(
            'services.ozon_account_sync._BoundedAdapter', side_effect=AssertionError('HTTP in request'),
        ) as adapter:
            response = self.client.post('/marketplaces/accounts/ozon/connect', json={
                'client_id': '321', 'api_key': 'new-connection-secret',
            })
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload['account']['connection_status'], 'unchecked')
        self.assertEqual(payload['job']['status'], 'pending')
        self.assertEqual(payload['job']['phase'], 'check')
        self.assertNotIn('new-connection-secret', response.get_data(as_text=True))
        self.assertNotIn('credential_fingerprint', response.get_data(as_text=True))
        adapter.assert_not_called()
        with self.app.app_context():
            self.assertEqual(BackgroundJob.query.count(), 1)

    def test_existing_account_identity_is_immutable_even_without_operations(self):
        with self.app.app_context():
            account = self._save()
            original_key = account._credentials_encrypted
            with self.assertRaises(MarketplaceAccountConflict):
                self._save(account_id=account.id, external_account_id='different-shop', api_key='replacement')
            db.session.refresh(account)
            self.assertEqual(account.external_account_id, '123456')
            self.assertEqual(account._credentials_encrypted, original_key)
            self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_disconnected_account_can_reconnect_without_duplicate_or_lost_history(self):
        with self.app.app_context():
            account = self._save()
            account_id = account.id
            MarketplaceAccountService.disconnect(seller_id=self.seller1_id, account_id=account_id)
            expected_version = account.version
        user_patch, login_patch = self._login_patches(self.seller1_id)
        with user_patch, login_patch:
            result = self.client.post(f'/marketplaces/accounts/{account_id}/reconnect', json={
                'client_id': '123456', 'api_key': 'reconnect-synthetic-secret', 'expected_version': expected_version,
            })
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.get_json()['account']['id'], account_id)
        self.assertTrue(result.get_json()['account']['is_active'])
        self.assertEqual(result.get_json()['job']['phase'], 'check')
        self.assertNotIn('reconnect-synthetic-secret', result.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(SellerMarketplaceAccount.query.count(), 1)

    def test_onboarding_is_strict_tenant_scoped_and_status_get_is_read_only(self):
        with self.app.app_context():
            account_id = self._save().id
        user_patch, login_patch = self._login_patches(self.seller1_id)
        with user_patch, login_patch:
            queued = self.client.post(f'/marketplaces/accounts/{account_id}/connect', json={})
            duplicate = self.client.post(f'/marketplaces/accounts/{account_id}/connect', json={})
            status = self.client.get(f'/marketplaces/accounts/{account_id}/setup', headers={'Accept': 'application/json'})
            smuggled = self.client.post(f'/marketplaces/accounts/{account_id}/connect', json={'seller_id': self.seller1_id})
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(duplicate.get_json()['job']['job_uid'], queued.get_json()['job']['job_uid'])
        self.assertEqual(status.get_json()['job']['status'], 'pending')
        self.assertNotIn('credential_fingerprint', status.get_data(as_text=True))
        self.assertEqual(smuggled.status_code, 400)
        user_patch, login_patch = self._login_patches(self.seller2_id)
        with user_patch, login_patch:
            foreign_post = self.client.post(f'/marketplaces/accounts/{account_id}/connect', json={})
            foreign_get = self.client.get(f'/marketplaces/accounts/{account_id}/setup', headers={'Accept': 'application/json'})
        self.assertEqual(foreign_post.status_code, 404)
        self.assertEqual(foreign_get.status_code, 404)
        with self.app.app_context():
            job = BackgroundJob.query.one()
            self.assertEqual(job.status, 'pending')
            self.assertIsNone(SellerMarketplaceAccount.query.get(account_id).connection_checked_at)

    def test_onboarding_duplicate_flag_csrf_and_scope_validation(self):
        user_patch, login_patch = self._login_patches(self.seller1_id)
        payload = {'client_id': '321', 'api_key': 'duplicate-connection-secret'}
        with user_patch, login_patch:
            first = self.client.post('/marketplaces/accounts/ozon/connect', json=payload)
            duplicate = self.client.post('/marketplaces/accounts/ozon/connect', json=payload)
            smuggled = self.client.post('/marketplaces/accounts/ozon/connect', json=dict(payload, seller_id=self.seller2_id))
            self.app.config['MARKETPLACE_OZON_ENABLED'] = False
            disabled = self.client.post('/marketplaces/accounts/ozon/connect', json=payload)
            self.app.config['MARKETPLACE_OZON_ENABLED'] = True
            self.app.config['WTF_CSRF_ENABLED'] = True
            csrf = self.client.post('/marketplaces/accounts/ozon/connect', json=payload)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(smuggled.status_code, 400)
        self.assertEqual(disabled.status_code, 404)
        self.assertEqual(csrf.status_code, 400)
        with self.app.app_context():
            self.assertEqual(SellerMarketplaceAccount.query.count(), 1)
            self.assertEqual(BackgroundJob.query.count(), 1)

    def test_connection_version_drift_is_rejected_before_provider(self):
        with self.app.app_context():
            account = self._save()
            registry = MagicMock()
            with self.assertRaises(MarketplaceAccountConflict):
                MarketplaceAccountService.check_connection(
                    seller_id=self.seller1_id, account_id=account.id,
                    expected_version=account.version + 1, registry=registry,
                )
            registry.get.assert_not_called()


    def test_existing_key_cannot_bypass_reviewed_rotation_through_settings_route(self):
        with self.app.app_context():
            account = self._save()
            identity, version, encrypted = account.id, account.version, account._credentials_encrypted
        user_patch, login_patch = self._login_patches(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.post(f'/marketplaces/accounts/{identity}', json={
                'client_id':'123456', 'label':'Attempted settings replacement',
                'api_key':'must-not-bypass-review', 'expected_version':version,
            })
        self.assertEqual(response.status_code,400)
        self.assertNotIn('must-not-bypass-review',response.get_data(as_text=True))
        with self.app.app_context():
            account = db.session.get(SellerMarketplaceAccount,identity)
            self.assertEqual((account.version,account._credentials_encrypted),(version,encrypted))

    def test_reconnect_requires_reviewed_version_before_save_or_enqueue(self):
        with self.app.app_context():
            account = self._save()
            identity, version, encrypted = account.id, account.version, account._credentials_encrypted
        user_patch, login_patch = self._login_patches(self.seller1_id)
        with user_patch, login_patch, patch('routes.marketplace_accounts.enqueue_account_sync') as enqueue:
            for supplied, status in [(None,400),(True,400),(0,400),(version+1,409)]:
                payload={'client_id':'123456','api_key':'must-not-save-stale'}
                if supplied is not None:payload['expected_version']=supplied
                response=self.client.post(f'/marketplaces/accounts/{identity}/reconnect',json=payload)
                self.assertEqual(response.status_code,status)
                self.assertNotIn('must-not-save-stale',response.get_data(as_text=True))
            enqueue.assert_not_called()
        with self.app.app_context():
            account=db.session.get(SellerMarketplaceAccount,identity)
            self.assertEqual((account.version,account._credentials_encrypted),(version,encrypted))

    def test_reconnect_stale_hidden_label_gets_reviewable_version_conflict(self):
        with self.app.app_context():
            account=self._save()
            identity, viewed, old_label, encrypted=account.id,account.version,account.label,account._credentials_encrypted
            account.label='Название после изменения';account.version+=1;db.session.commit()
        user_patch,login_patch=self._login_patches(self.seller1_id)
        with user_patch,login_patch,patch('routes.marketplace_accounts.enqueue_account_sync') as enqueue:
            response=self.client.post(f'/marketplaces/accounts/{identity}/reconnect',json={
                'client_id':'123456','api_key':'must-not-save-stale','label':old_label,'expected_version':viewed})
            self.assertEqual(response.status_code,409)
            self.assertEqual(response.get_json()['code'],'marketplace_account_version_conflict')
            enqueue.assert_not_called()
        with self.app.app_context():
            account=db.session.get(SellerMarketplaceAccount,identity)
            self.assertEqual((account.version,account._credentials_encrypted),(viewed+1,encrypted))


if __name__ == "__main__":
    unittest.main()
