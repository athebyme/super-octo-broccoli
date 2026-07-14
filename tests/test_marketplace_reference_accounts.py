# -*- coding: utf-8 -*-
"""Admin Ozon reference credentials stay encrypted and provider-isolated."""

from datetime import datetime
from unittest.mock import MagicMock
import json
import os
import unittest

from cryptography.fernet import Fernet
from flask import Flask

from models import Marketplace, MarketplaceReferenceAccount, db
from services.marketplace_adapters.types import ConnectionCheck
from services.marketplace_reference_accounts import (
    MarketplaceReferenceAccountConfigurationError,
    MarketplaceReferenceAccountNotFound,
    MarketplaceReferenceAccountService,
)


class MarketplaceReferenceAccountServiceTest(unittest.TestCase):
    def setUp(self):
        self.previous_encryption_key = os.environ.get("ENCRYPTION_KEY")
        os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        ozon = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        wb = Marketplace(name="Wildberries", code="wb", is_active=True)
        db.session.add_all([ozon, wb])
        db.session.commit()
        self.ozon_id = ozon.id
        self.wb_id = wb.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        if self.previous_encryption_key is None:
            os.environ.pop("ENCRYPTION_KEY", None)
        else:
            os.environ["ENCRYPTION_KEY"] = self.previous_encryption_key

    def test_reference_secret_is_encrypted_and_never_public(self):
        account = MarketplaceReferenceAccountService.save(
            marketplace_id=self.ozon_id,
            external_account_id="123456",
            api_key="reference-secret",
        )
        public = json.dumps(account.to_public_dict(), ensure_ascii=False)
        self.assertNotIn("reference-secret", account._credentials_encrypted)
        self.assertNotIn("reference-secret", public)
        self.assertNotIn("reference-secret", repr(account))
        self.assertEqual(account.get_credentials()["api_key"], "reference-secret")

    def test_reference_account_is_ozon_only_and_one_per_marketplace(self):
        with self.assertRaises(MarketplaceReferenceAccountNotFound):
            MarketplaceReferenceAccountService.save(
                marketplace_id=self.wb_id,
                external_account_id="wb-id",
                api_key="not-used",
            )
        first = MarketplaceReferenceAccountService.save(
            marketplace_id=self.ozon_id,
            external_account_id="1",
            api_key="first-key",
        )
        second = MarketplaceReferenceAccountService.save(
            marketplace_id=self.ozon_id,
            external_account_id="2",
            api_key="second-key",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(MarketplaceReferenceAccount.query.count(), 1)
        self.assertEqual(second.external_account_id, "2")
        self.assertEqual(second.connection_status, "unchecked")

    def test_missing_encryption_key_fails_closed_without_row(self):
        os.environ.pop("ENCRYPTION_KEY", None)
        with self.assertRaises(MarketplaceReferenceAccountConfigurationError):
            MarketplaceReferenceAccountService.save(
                marketplace_id=self.ozon_id,
                external_account_id="1",
                api_key="must-not-be-plaintext",
            )
        self.assertEqual(MarketplaceReferenceAccount.query.count(), 0)

    def test_check_persists_only_bounded_metadata_and_redacts_secret(self):
        account = MarketplaceReferenceAccountService.save(
            marketplace_id=self.ozon_id,
            external_account_id="123",
            api_key="reference-secret",
        )
        expires_at = datetime(2026, 12, 31, 0, 0, 0)
        adapter = MagicMock()
        adapter.check_connection.return_value = ConnectionCheck(
            ok=False,
            status="error",
            external_account_id="123",
            roles=("catalog", "catalog"),
            expires_at=expires_at,
            provider_request_id="request-1",
            error_code="synthetic",
            error_message="provider echoed reference-secret",
        )
        registry = MagicMock()
        registry.get.return_value = adapter
        checked, result = MarketplaceReferenceAccountService.check(
            marketplace_id=self.ozon_id,
            registry=registry,
            now=datetime(2026, 7, 15, 12, 0, 0),
        )
        self.assertFalse(result.ok)
        self.assertEqual(checked.connection_status, "error")
        self.assertEqual(json.loads(checked.roles_json), ["catalog"])
        self.assertEqual(checked.credential_expires_at, expires_at)
        self.assertNotIn("reference-secret", checked.last_error_message)
        self.assertIn("[redacted]", checked.last_error_message)
        adapter.check_connection.assert_called_once()

    def test_disconnect_removes_secret_and_connection_metadata(self):
        account = MarketplaceReferenceAccountService.save(
            marketplace_id=self.ozon_id,
            external_account_id="123",
            api_key="reference-secret",
        )
        account.connection_status = "connected"
        account.last_error_code = "old"
        db.session.commit()
        disconnected = MarketplaceReferenceAccountService.disconnect(
            marketplace_id=self.ozon_id,
        )
        self.assertFalse(disconnected.has_credentials)
        self.assertEqual(disconnected.connection_status, "unchecked")
        self.assertIsNone(disconnected.last_error_code)


if __name__ == "__main__":
    unittest.main()
