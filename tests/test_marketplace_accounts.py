# -*- coding: utf-8 -*-
"""Encrypted seller marketplace accounts and tenant-scoped routes."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import os
import unittest

from cryptography.fernet import Fernet
from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import Marketplace, Seller, SellerMarketplaceAccount, User, db
from routes.marketplace_accounts import register_marketplace_account_routes
from services.marketplace_accounts import (
    MarketplaceAccountConfigurationError,
    MarketplaceAccountNotFound,
    MarketplaceAccountService,
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
            account_id = self._save().id
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
                json={},
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


if __name__ == "__main__":
    unittest.main()
