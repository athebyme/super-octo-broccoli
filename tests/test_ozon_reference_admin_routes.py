# -*- coding: utf-8 -*-
"""Admin Ozon reference routes enforce feature and provider boundaries."""

from types import SimpleNamespace
from unittest.mock import patch
import os
import unittest

from cryptography.fernet import Fernet
from flask import Flask
from flask_login import LoginManager

from models import (
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceProductType,
    MarketplaceReferenceAccount,
    MarketplaceTaxonomyCategory,
    db,
)
from routes.marketplaces import register_marketplaces_routes
from services.marketplace_adapters.types import ConnectionCheck


class OzonReferenceAdminRoutesTest(unittest.TestCase):
    def setUp(self):
        self.previous_encryption_key = os.environ.get("ENCRYPTION_KEY")
        os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="ozon-reference-admin-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        self.app.add_url_rule('/dashboard', 'dashboard', lambda: 'dashboard')
        register_marketplaces_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            ozon = Marketplace(
                name="Ozon",
                code="ozon",
                adapter_code="ozon",
                is_active=True,
            )
            wb = Marketplace(name="Wildberries", code="wb", is_active=True)
            db.session.add_all([ozon, wb])
            db.session.flush()
            category = MarketplaceTaxonomyCategory(
                marketplace_id=ozon.id,
                external_category_id="10",
                name="Категория",
                full_path="Категория",
                is_available=True,
            )
            db.session.add(category)
            db.session.flush()
            product_type = MarketplaceProductType(
                marketplace_id=ozon.id,
                category_id=category.id,
                external_type_id="777",
                name="Тип",
                is_available=True,
            )
            db.session.add(product_type)
            db.session.flush()
            attribute = MarketplaceAttributeDefinition(
                marketplace_id=ozon.id,
                product_type_id=product_type.id,
                external_attribute_id="31",
                name="Бренд",
                data_type="String",
                is_required=True,
                is_enabled=True,
                is_available=True,
            )
            db.session.add(attribute)
            db.session.commit()
            self.ozon_id = ozon.id
            self.wb_id = wb.id
            self.product_type_id = product_type.id
            self.attribute_id = attribute.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        if self.previous_encryption_key is None:
            os.environ.pop("ENCRYPTION_KEY", None)
        else:
            os.environ["ENCRYPTION_KEY"] = self.previous_encryption_key

    @staticmethod
    def _user(*, admin=True):
        return SimpleNamespace(
            id=1,
            is_authenticated=True,
            is_active=True,
            is_admin=admin,
            seller=None,
        )

    def _auth(self, *, admin=True):
        user = self._user(admin=admin)
        return (
            patch("routes.marketplaces.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_reference_save_is_encrypted_and_feature_gated(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.post(
                f"/admin/marketplaces/{self.ozon_id}/ozon/reference-account",
                data={
                    "external_account_id": "123",
                    "api_key": "route-reference-secret",
                },
            )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            account = MarketplaceReferenceAccount.query.one()
            self.assertNotIn(
                "route-reference-secret",
                account._credentials_encrypted,
            )

        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.marketplaces.MarketplaceReferenceAccountService.save"
        ) as save:
            blocked = self.client.post(
                f"/admin/marketplaces/{self.ozon_id}/ozon/reference-account",
                data={"external_account_id": "456", "api_key": "new"},
            )
        self.assertEqual(blocked.status_code, 404)
        save.assert_not_called()

    def test_reference_disconnect_remains_available_when_flag_is_off(self):
        with self.app.app_context():
            account = MarketplaceReferenceAccount(
                marketplace_id=self.ozon_id,
                external_account_id="123",
            )
            account.set_credentials({"api_key": "remove-me"})
            db.session.add(account)
            db.session.commit()
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.post(
                f"/admin/marketplaces/{self.ozon_id}/ozon/reference-account/disconnect"
            )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertFalse(MarketplaceReferenceAccount.query.one().has_credentials)

    def test_check_flashes_persisted_redacted_error_not_adapter_raw_error(self):
        checked = SimpleNamespace(last_error_message="provider echoed [redacted]")
        result = ConnectionCheck(
            ok=False,
            status="error",
            external_account_id="123",
            error_message="provider echoed route-reference-secret",
        )
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.marketplaces.MarketplaceReferenceAccountService.check",
            return_value=(checked, result),
        ):
            response = self.client.post(
                f"/admin/marketplaces/{self.ozon_id}/ozon/reference-account/check"
            )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            flashes = session.get("_flashes", [])
        messages = " ".join(message for _category, message in flashes)
        self.assertIn("[redacted]", messages)
        self.assertNotIn("route-reference-secret", messages)

    def test_tree_sync_is_ozon_only_and_calls_typed_service(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.marketplaces.OzonReferenceService.sync_tree",
            return_value={
                "success": True,
                "available_categories": 1,
                "available_types": 1,
            },
        ) as sync:
            response = self.client.post(
                f"/admin/marketplaces/{self.ozon_id}/ozon/sync-tree"
            )
            wrong_provider = self.client.post(
                f"/admin/marketplaces/{self.wb_id}/ozon/sync-tree"
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(wrong_provider.status_code, 404)
        sync.assert_called_once_with(self.ozon_id)

    def test_category_browser_is_bounded_and_pair_scoped(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.marketplaces.render_template",
            return_value="rendered",
        ) as render:
            response = self.client.get(
                f"/admin/marketplaces/{self.ozon_id}/ozon/categories?search=Тип&page=1"
            )
        self.assertEqual(response.status_code, 200)
        context = render.call_args.kwargs
        self.assertEqual(context["total"], 1)
        self.assertEqual(len(context["product_types"]), 1)
        self.assertEqual(context["product_types"][0].external_type_id, "777")
        self.assertEqual(context["per_page"], 100)

    def test_toggle_and_attribute_update_reject_loose_json(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.marketplaces.OzonReferenceService.set_product_type_enabled"
        ) as toggle:
            loose_toggle = self.client.post(
                f"/admin/marketplaces/ozon/types/{self.product_type_id}/toggle",
                json={"is_enabled": "true"},
            )
        self.assertEqual(loose_toggle.status_code, 400)
        toggle.assert_not_called()

        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.marketplaces.OzonReferenceService.update_attribute_configuration"
        ) as update:
            mixed = self.client.post(
                f"/admin/marketplaces/ozon/attributes/{self.attribute_id}/update",
                json={"is_enabled": True, "ai_instruction": "x"},
            )
        self.assertEqual(mixed.status_code, 400)
        update.assert_not_called()

    def test_required_attribute_cannot_be_disabled(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.post(
                f"/admin/marketplaces/ozon/attributes/{self.attribute_id}/update",
                json={"is_enabled": False},
            )
        self.assertEqual(response.status_code, 409)
        with self.app.app_context():
            attribute = db.session.get(
                MarketplaceAttributeDefinition,
                self.attribute_id,
            )
            self.assertTrue(attribute.is_enabled)

    def test_non_admin_is_denied_before_reference_service(self):
        user_patch, login_patch = self._auth(admin=False)
        with user_patch, login_patch, patch(
            "routes.marketplaces.OzonReferenceService.sync_tree"
        ) as sync:
            response = self.client.post(
                f"/admin/marketplaces/{self.ozon_id}/ozon/sync-tree"
            )
        self.assertEqual(response.status_code, 302)
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
