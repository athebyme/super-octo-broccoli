# -*- coding: utf-8 -*-
"""WB preview exposes only seller-owned Ozon preparation context."""

from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flask import Flask
from flask_login import LoginManager

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceProductDraft,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.suppliers import register_supplier_routes


class SellerOzonPreparationUiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="seller-ozon-preparation-ui",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        register_supplier_routes(self.app)
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        own_user = User(
            username="ozon-preview-own",
            email="ozon-preview-own@test.local",
            is_active=True,
        )
        own_user.set_password("synthetic-password")
        own_seller = Seller(user=own_user, company_name="Own")
        foreign_user = User(
            username="ozon-preview-foreign",
            email="ozon-preview-foreign@test.local",
            is_active=True,
        )
        foreign_user.set_password("synthetic-password")
        foreign_seller = Seller(user=foreign_user, company_name="Foreign")
        ozon = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add_all([own_seller, foreign_seller, ozon])
        db.session.flush()
        own_account = SellerMarketplaceAccount(
            seller_id=own_seller.id,
            marketplace_id=ozon.id,
            external_account_id="own-client",
            label="Own Ozon",
            is_active=True,
            is_default=True,
            connection_status="connected",
        )
        foreign_account = SellerMarketplaceAccount(
            seller_id=foreign_seller.id,
            marketplace_id=ozon.id,
            external_account_id="foreign-client",
            label="Foreign Ozon secret",
            is_active=True,
            is_default=True,
            connection_status="connected",
        )
        own_product = ImportedProduct(
            seller_id=own_seller.id,
            external_id="own-product",
            title="Own product",
            description="Own description",
            category="Own category",
        )
        foreign_product = ImportedProduct(
            seller_id=foreign_seller.id,
            external_id="foreign-product",
            title="Foreign product secret",
            description="Foreign description",
            category="Foreign category",
        )
        db.session.add_all([
            own_account,
            foreign_account,
            own_product,
            foreign_product,
        ])
        db.session.flush()
        own_draft = MarketplaceProductDraft(
            seller_id=own_seller.id,
            marketplace_id=ozon.id,
            account_id=own_account.id,
            imported_product_id=own_product.id,
            offer_id="own-offer",
            status="needs_category",
            source_fact_hash="a" * 64,
        )
        foreign_draft = MarketplaceProductDraft(
            seller_id=foreign_seller.id,
            marketplace_id=ozon.id,
            account_id=foreign_account.id,
            imported_product_id=foreign_product.id,
            offer_id="foreign-offer",
            status="needs_category",
            source_fact_hash="b" * 64,
        )
        db.session.add_all([own_draft, foreign_draft])
        db.session.commit()

        self.own_seller = own_seller
        self.own_user_id = own_user.id
        self.own_account_id = own_account.id
        self.own_product_id = own_product.id
        self.own_draft_id = own_draft.id
        self.foreign_product_id = foreign_product.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _auth(self):
        user = SimpleNamespace(
            id=self.own_user_id,
            seller=self.own_seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )
        return (
            patch("routes.suppliers.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_preview_context_is_tenant_scoped_and_feature_gated(self):
        captured = {}

        def render(_template, **context):
            captured.clear()
            captured.update(context)
            return "ok"

        importer = SimpleNamespace(
            build_wb_card_preview=lambda _product: {
                "is_ready": True,
                "issues": [],
            }
        )
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "services.wb_product_importer.WBProductImporter",
            return_value=importer,
        ), patch("routes.suppliers.render_template", side_effect=render):
            response = self.client.get(
                f"/my-products/{self.own_product_id}/wb-preview"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [account.id for account in captured["ozon_accounts"]],
            [self.own_account_id],
        )
        self.assertEqual(
            captured["ozon_drafts_by_account"][self.own_account_id].id,
            self.own_draft_id,
        )
        self.assertNotIn("Foreign Ozon secret", response.get_data(as_text=True))

        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "services.wb_product_importer.WBProductImporter",
            return_value=importer,
        ), patch("routes.suppliers.render_template", side_effect=render):
            denied = self.client.get(
                f"/my-products/{self.foreign_product_id}/wb-preview"
            )
        self.assertEqual(denied.status_code, 404)

        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "services.wb_product_importer.WBProductImporter",
            return_value=importer,
        ), patch("routes.suppliers.render_template", side_effect=render):
            disabled = self.client.get(
                f"/my-products/{self.own_product_id}/wb-preview"
            )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(captured["ozon_enabled"])
        self.assertEqual(captured["ozon_accounts"], [])
        self.assertEqual(captured["ozon_drafts_by_account"], {})


if __name__ == "__main__":
    unittest.main()
