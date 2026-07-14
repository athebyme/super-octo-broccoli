# -*- coding: utf-8 -*-
"""Unified listing HTTP APIs preserve seller/account and feature boundaries."""

from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    Marketplace,
    MarketplaceListing,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_listings import register_marketplace_listing_routes
from services.marketplace_listings import MarketplaceListingService


class MarketplaceListingRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-listing-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_listing_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.seller1_id = self._seller("route-one", "route1@test.local")
            self.seller2_id = self._seller("route-two", "route2@test.local")
            ozon = Marketplace(
                name="Ozon",
                code="ozon",
                adapter_code="ozon",
                is_active=True,
            )
            db.session.add(ozon)
            db.session.flush()
            account1 = SellerMarketplaceAccount(
                seller_id=self.seller1_id,
                marketplace_id=ozon.id,
                external_account_id="client-one",
                label="Ozon One",
                is_active=True,
                connection_status="connected",
            )
            account2 = SellerMarketplaceAccount(
                seller_id=self.seller2_id,
                marketplace_id=ozon.id,
                external_account_id="client-two",
                label="Ozon Two",
                is_active=True,
                connection_status="connected",
            )
            db.session.add_all([account1, account2])
            db.session.flush()
            own = MarketplaceListing(
                seller_id=self.seller1_id,
                marketplace_id=ozon.id,
                account_id=account1.id,
                offer_id="own-offer",
                external_product_id="101",
                title="Own listing",
                normalized_status="active",
                sync_fingerprint="a" * 64,
            )
            foreign = MarketplaceListing(
                seller_id=self.seller2_id,
                marketplace_id=ozon.id,
                account_id=account2.id,
                offer_id="foreign-offer",
                external_product_id="202",
                title="Foreign listing",
                normalized_status="active",
                sync_fingerprint="b" * 64,
            )
            db.session.add_all([own, foreign])
            db.session.commit()
            self.account1_id = account1.id
            self.account2_id = account2.id
            self.own_id = own.id
            self.foreign_id = foreign.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    @staticmethod
    def _seller(username, email):
        user = User(username=username, email=email, is_active=True)
        user.set_password("synthetic-password")
        seller = Seller(user=user, company_name=username)
        db.session.add(seller)
        db.session.commit()
        return seller.id

    @staticmethod
    def _user(seller_id=None):
        seller = SimpleNamespace(id=seller_id) if seller_id else None
        return SimpleNamespace(
            id=10,
            seller=seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _auth(self, seller_id):
        user = self._user(seller_id)
        return (
            patch("routes.marketplace_listings.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_list_and_detail_are_tenant_scoped(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            listed = self.client.get(
                "/marketplaces/listings/api?marketplace=ozon",
                headers={"Accept": "application/json"},
            )
            own = self.client.get(
                f"/marketplaces/listings/{self.own_id}",
                headers={"Accept": "application/json"},
            )
            foreign = self.client.get(
                f"/marketplaces/listings/{self.foreign_id}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [item["id"] for item in listed.get_json()["items"]],
            [self.own_id],
        )
        self.assertEqual(own.status_code, 200)
        self.assertEqual(foreign.status_code, 404)
        self.assertNotIn("Foreign listing", foreign.get_data(as_text=True))

    def test_foreign_account_sync_is_denied_before_adapter_lookup(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch(
            "services.marketplace_listings.get_marketplace_registry"
        ) as registry:
            response = self.client.post(
                f"/marketplaces/listings/accounts/{self.account2_id}/sync",
                json={"max_pages": 1, "force_restart": False},
            )
        self.assertEqual(response.status_code, 404)
        registry.assert_not_called()

    def test_sync_json_is_strict_and_passes_authenticated_seller_scope(self):
        run = SimpleNamespace(
            status="paused",
            to_public_dict=lambda: {"id": 9, "status": "paused"},
        )
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceListingService,
            "sync_ozon_account",
            return_value=run,
        ) as sync:
            loose = self.client.post(
                f"/marketplaces/listings/accounts/{self.account1_id}/sync",
                json={"max_pages": "1", "force_restart": "false"},
            )
            valid = self.client.post(
                f"/marketplaces/listings/accounts/{self.account1_id}/sync",
                json={"max_pages": 1, "force_restart": False},
            )
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(valid.status_code, 200)
        sync.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.account1_id,
            max_pages=1,
            force_restart=False,
        )

    def test_feature_flag_and_non_seller_block_sync_before_service(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceListingService,
            "sync_ozon_account",
        ) as sync:
            disabled = self.client.post(
                f"/marketplaces/listings/accounts/{self.account1_id}/sync",
                json={},
            )
        self.assertEqual(disabled.status_code, 404)
        sync.assert_not_called()

        self.app.config["MARKETPLACE_OZON_ENABLED"] = True
        user = self._user()
        with patch("routes.marketplace_listings.current_user", user), patch(
            "flask_login.utils._get_user",
            return_value=user,
        ), patch.object(
            MarketplaceListingService,
            "sync_ozon_account",
        ) as sync:
            denied = self.client.post(
                f"/marketplaces/listings/accounts/{self.account1_id}/sync",
                json={},
            )
        self.assertEqual(denied.status_code, 403)
        sync.assert_not_called()

    def test_sync_route_is_csrf_protected_when_enabled(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.post(
                f"/marketplaces/listings/accounts/{self.account1_id}/sync",
                data={"max_pages": "1"},
            )
        self.assertEqual(response.status_code, 400)
