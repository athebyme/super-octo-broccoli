# -*- coding: utf-8 -*-
"""Shared WB/Ozon API settings entry point."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import os
import unittest


class ApiSettingsMarketplaceTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("SECRET_KEY", "api-settings-tests")
        os.environ.setdefault("DISABLE_SECURE_COOKIE", "1")
        import seller_platform

        self.module = seller_platform
        self.app = seller_platform.app
        self._previous_config = {
            key: self.app.config.get(key)
            for key in (
                "TESTING",
                "WTF_CSRF_ENABLED",
                "MARKETPLACE_OZON_ENABLED",
                "MARKETPLACE_OZON_PUBLICATION_ENABLED",
                "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED",
            )
        }
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=False,
            MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config.update(self._previous_config)

    @staticmethod
    def _user():
        return SimpleNamespace(
            id=17,
            seller=SimpleNamespace(id=71),
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def test_get_loads_only_owned_ozon_accounts_into_public_context(self):
        user = self._user()
        account = MagicMock()
        account.to_public_dict.return_value = {
            "id": 9,
            "marketplace_code": "ozon",
            "external_account_id": "synthetic-client",
            "label": "Основной Ozon",
            "has_credentials": True,
            "connection_status": "unchecked",
        }

        with patch.object(self.module, "current_user", user), patch(
            "flask_login.utils._get_user", return_value=user
        ), patch(
            "services.marketplace_accounts.MarketplaceAccountService.list_accounts",
            return_value=[account],
        ) as list_accounts, patch.object(
            self.module, "render_template", return_value="rendered"
        ) as rendered:
            response = self.client.get("/api-settings")

        self.assertEqual(response.status_code, 200)
        list_accounts.assert_called_once_with(
            seller_id=71,
            marketplace_code="ozon",
        )
        _, context = rendered.call_args
        self.assertEqual(context["ozon_accounts"], [account.to_public_dict.return_value])
        self.assertTrue(context["ozon_enabled"])
        self.assertFalse(context["ozon_publication_enabled"])
        self.assertFalse(context["ozon_commercial_writes_enabled"])
        self.assertNotIn("credentials_encrypted", str(context))

    def test_template_has_direct_ozon_create_and_read_only_check_forms(self):
        self.app.jinja_env.get_template("api_settings.html")
        template = (
            Path(__file__).parents[1] / "templates" / "api_settings.html"
        ).read_text(encoding="utf-8")

        self.assertIn("marketplace_accounts.create_ozon", template)
        self.assertIn("marketplace_accounts.check", template)
        self.assertIn('name="return_to" value="api_settings"', template)
        self.assertIn('name="csrf_token"', template)
        self.assertIn("Публикация карточек и изменение цен или остатков", template)
        self.assertNotIn("credentials_encrypted", template)


if __name__ == "__main__":
    unittest.main()
