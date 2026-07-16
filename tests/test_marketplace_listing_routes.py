# -*- coding: utf-8 -*-
"""Unified listing HTTP APIs preserve seller/account and feature boundaries."""

from datetime import datetime
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceCanonicalContentProposal,
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
        self._register_template_stubs()
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
            own_source = ImportedProduct(
                seller_id=self.seller1_id,
                external_id="route-source-own",
                external_vendor_code="route-internal-own",
                source_type="synthetic",
                title="Own internal product",
                ai_attributes='{"cached": true}',
            )
            foreign_source = ImportedProduct(
                seller_id=self.seller2_id,
                external_id="route-source-foreign",
                external_vendor_code="route-internal-foreign",
                source_type="synthetic",
                title="Foreign internal product",
            )
            db.session.add_all([own_source, foreign_source])
            db.session.commit()
            self.account1_id = account1.id
            self.account2_id = account2.id
            self.own_id = own.id
            self.foreign_id = foreign.id
            self.own_source_id = own_source.id
            self.foreign_source_id = foreign_source.id

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
        seller = (
            SimpleNamespace(id=seller_id, company_name="Synthetic seller")
            if seller_id else None
        )
        return SimpleNamespace(
            id=seller_id or 10,
            username="synthetic-user",
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

    def _register_template_stubs(self):
        root = Path(__file__).resolve().parents[1]
        endpoint_names = set()
        for relative_path in (
            "templates/base.html",
            "templates/marketplace_listing_detail.html",
        ):
            endpoint_names.update(re.findall(
                r"url_for\(['\"]([^'\"]+)",
                (root / relative_path).read_text(encoding="utf-8"),
            ))

        def stub():
            return ""

        for index, endpoint in enumerate(sorted(endpoint_names)):
            if endpoint not in self.app.view_functions:
                self.app.add_url_rule(
                    f"/__template_stub/{index}",
                    endpoint=endpoint,
                    view_func=stub,
                )

    def _link_fresh(self, *, foreign=False):
        seller_id = self.seller2_id if foreign else self.seller1_id
        listing_id = self.foreign_id if foreign else self.own_id
        source_id = self.foreign_source_id if foreign else self.own_source_id
        listing = MarketplaceListing.query.filter_by(
            id=listing_id,
            seller_id=seller_id,
        ).one()
        listing.imported_product_id = source_id
        listing.link_status = "linked"
        listing.title = "Foreign Ozon title" if foreign else "Own Ozon title"
        listing.description = (
            "Foreign Ozon description"
            if foreign else "Own Ozon description"
        )
        listing.info_synced_at = datetime.utcnow()
        listing.attributes_synced_at = datetime.utcnow()
        db.session.commit()
        return listing

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

    def test_manual_product_link_is_strict_tenant_scoped_and_filterable(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            foreign = self.client.post(
                f"/marketplaces/listings/{self.own_id}/link",
                json={
                    "imported_product_id": self.foreign_source_id,
                    "expected_link_version": 1,
                },
            )
            loose = self.client.post(
                f"/marketplaces/listings/{self.own_id}/link",
                json={
                    "imported_product_id": str(self.own_source_id),
                    "expected_link_version": "1",
                },
            )
            linked = self.client.post(
                f"/marketplaces/listings/{self.own_id}/link",
                json={
                    "imported_product_id": self.own_source_id,
                    "expected_link_version": 1,
                },
            )
            filtered = self.client.get(
                "/marketplaces/listings/api?marketplace=ozon&link_status=linked",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(linked.status_code, 200)
        self.assertEqual(
            linked.get_json()["listing"]["imported_product_id"],
            self.own_source_id,
        )
        self.assertEqual(
            [item["id"] for item in filtered.get_json()["items"]],
            [self.own_id],
        )

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

    def test_reviewed_ozon_content_route_is_local_strict_and_reversible(self):
        with self.app.app_context():
            self._link_fresh()
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            created = self.client.post(
                f"/marketplaces/listings/{self.own_id}/canonical-content-proposals",
                json={"fields": ["title", "description"]},
            )
            proposal_data = created.get_json()["proposal"]
            loose = self.client.post(
                "/marketplaces/listings/canonical-content-proposals/"
                f"{proposal_data['id']}/apply",
                json={
                    "expected_version": str(proposal_data["version"]),
                    "confirm_apply": True,
                },
            )
            unconfirmed = self.client.post(
                "/marketplaces/listings/canonical-content-proposals/"
                f"{proposal_data['id']}/apply",
                json={
                    "expected_version": proposal_data["version"],
                    "confirm_apply": False,
                },
            )
            applied = self.client.post(
                "/marketplaces/listings/canonical-content-proposals/"
                f"{proposal_data['id']}/apply",
                json={
                    "expected_version": proposal_data["version"],
                    "confirm_apply": True,
                },
            )
            applied_data = applied.get_json()["proposal"]
            rolled_back = self.client.post(
                "/marketplaces/listings/canonical-content-proposals/"
                f"{proposal_data['id']}/rollback",
                json={
                    "expected_version": applied_data["version"],
                    "confirm_rollback": True,
                },
            )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(unconfirmed.status_code, 400)
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(rolled_back.status_code, 200)
        self.assertEqual(
            rolled_back.get_json()["proposal"]["status"],
            "rolled_back",
        )
        with self.app.app_context():
            source = db.session.get(ImportedProduct, self.own_source_id)
            self.assertEqual(source.title, "Own internal product")

    def test_reverse_content_routes_do_not_cross_tenant_or_feature_flag(self):
        with self.app.app_context():
            self._link_fresh(foreign=True)
            foreign_proposal = MarketplaceCanonicalContentProposal(
                seller_id=self.seller2_id,
                marketplace_id=Marketplace.query.filter_by(code="ozon").one().id,
                account_id=self.account2_id,
                listing_id=self.foreign_id,
                imported_product_id=self.foreign_source_id,
                created_by_user_id=User.query.join(Seller).filter(
                    Seller.id == self.seller2_id
                ).one().id,
                status="pending_review",
                fields_json='["title"]',
                baseline_state_json='{"title":"Foreign internal product"}',
                proposed_state_json='{"title":"Foreign Ozon title"}',
                baseline_fingerprint="c" * 64,
                source_fingerprint="d" * 64,
                source_observed_at=datetime.utcnow(),
            )
            db.session.add(foreign_proposal)
            db.session.commit()
            foreign_proposal_id = foreign_proposal.id

        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            denied = self.client.post(
                "/marketplaces/listings/canonical-content-proposals/"
                f"{foreign_proposal_id}/apply",
                json={"expected_version": 1, "confirm_apply": True},
            )
        self.assertEqual(denied.status_code, 404)

        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        with self.app.app_context():
            self._link_fresh()
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch(
            "routes.marketplace_listings."
            "MarketplaceCanonicalContentService.create_proposal"
        ) as create:
            disabled = self.client.post(
                f"/marketplaces/listings/{self.own_id}/canonical-content-proposals",
                json={"fields": ["title"]},
            )
        self.assertEqual(disabled.status_code, 404)
        create.assert_not_called()

    def test_applied_reverse_history_remains_visible_after_disconnect_and_unlink(self):
        with self.app.app_context():
            self._link_fresh()
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            created = self.client.post(
                f"/marketplaces/listings/{self.own_id}/canonical-content-proposals",
                json={"fields": ["title"]},
            ).get_json()["proposal"]
            applied = self.client.post(
                "/marketplaces/listings/canonical-content-proposals/"
                f"{created['id']}/apply",
                json={
                    "expected_version": created["version"],
                    "confirm_apply": True,
                },
            )
        self.assertEqual(applied.status_code, 200)

        with self.app.app_context():
            account = db.session.get(
                SellerMarketplaceAccount,
                self.account1_id,
            )
            account.is_active = False
            account.connection_status = "disconnected"
            listing = db.session.get(MarketplaceListing, self.own_id)
            listing.imported_product_id = None
            listing.link_status = "unlinked"
            db.session.commit()

        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            detail = self.client.get(
                f"/marketplaces/listings/{self.own_id}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(detail.status_code, 200)
        payload = detail.get_json()
        self.assertEqual(
            payload["canonical_content"]["blocked_reasons"],
            ["canonical_link_unavailable"],
        )
        self.assertEqual(
            [item["id"] for item in payload["canonical_content_proposals"]],
            [created["id"]],
        )

    def test_reverse_diff_review_ui_renders_with_pending_proposal(self):
        with self.app.app_context():
            self._link_fresh()
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            created = self.client.post(
                f"/marketplaces/listings/{self.own_id}/canonical-content-proposals",
                json={"fields": ["title", "description"]},
            )
            rendered = self.client.get(
                f"/marketplaces/listings/{self.own_id}",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(rendered.status_code, 200)
        html = rendered.get_data(as_text=True)
        self.assertIn("Ozon → общая карточка", html)
        self.assertIn("Применить к master", html)
        self.assertIn("Категории, характеристики, ID справочников", html)
        self.assertNotIn("synthetic-key", html)

    def test_feature_disable_blocks_new_apply_but_keeps_reject_and_rollback(self):
        with self.app.app_context():
            self._link_fresh()
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            pending = self.client.post(
                f"/marketplaces/listings/{self.own_id}/canonical-content-proposals",
                json={"fields": ["title"]},
            ).get_json()["proposal"]
            self.app.config["MARKETPLACE_OZON_ENABLED"] = False
            blocked_apply = self.client.post(
                "/marketplaces/listings/canonical-content-proposals/"
                f"{pending['id']}/apply",
                json={
                    "expected_version": pending["version"],
                    "confirm_apply": True,
                },
            )
            rejected = self.client.post(
                "/marketplaces/listings/canonical-content-proposals/"
                f"{pending['id']}/reject",
                json={"expected_version": pending["version"]},
            )

            self.app.config["MARKETPLACE_OZON_ENABLED"] = True
            second = self.client.post(
                f"/marketplaces/listings/{self.own_id}/canonical-content-proposals",
                json={"fields": ["title"]},
            ).get_json()["proposal"]
            applied = self.client.post(
                "/marketplaces/listings/canonical-content-proposals/"
                f"{second['id']}/apply",
                json={
                    "expected_version": second["version"],
                    "confirm_apply": True,
                },
            ).get_json()["proposal"]
            self.app.config["MARKETPLACE_OZON_ENABLED"] = False
            rolled_back = self.client.post(
                "/marketplaces/listings/canonical-content-proposals/"
                f"{second['id']}/rollback",
                json={
                    "expected_version": applied["version"],
                    "confirm_rollback": True,
                },
            )
        self.app.config["MARKETPLACE_OZON_ENABLED"] = True
        self.assertEqual(blocked_apply.status_code, 404)
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rolled_back.status_code, 200)
        self.assertEqual(
            rolled_back.get_json()["proposal"]["status"],
            "rolled_back",
        )
