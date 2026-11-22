# -*- coding: utf-8 -*-
"""Unified listing HTTP APIs preserve seller/account and feature boundaries."""

from datetime import datetime
from pathlib import Path
import json
import re
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import unittest

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceCanonicalContentProposal,
    MarketplaceCatalogSync,
    MarketplaceListing,
    Product,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_listings import register_marketplace_listing_routes
import routes.marketplace_listings as marketplace_listing_routes
from routes.marketplace_drafts import register_marketplace_draft_routes
from routes.ozon_bulk_uploads import register_ozon_bulk_upload_routes
from services.ozon_existing_update_batch import OzonExistingUpdateBatch
from services.marketplace_listings import MarketplaceListingService
from services.marketplace_drafts import (
    MarketplaceDraftNotFound,
    MarketplaceDraftService,
)


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
        from routes.marketplace_accounts import register_marketplace_account_routes
        register_marketplace_account_routes(self.app)
        register_marketplace_draft_routes(self.app)
        register_ozon_bulk_upload_routes(self.app)
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
            "templates/marketplace_listings_beta.html",
            "templates/marketplace_listings.html",
            "templates/partials/marketplace_catalog_products.html",
            "templates/marketplace_listing_beta_detail.html",
            "templates/partials/listing_workspace_beta_header.html",
            "templates/partials/listing_workspace_classic_header.html",
            "templates/partials/preparation_workspace_journey.html",
            "templates/partials/seller_workspace_nav.html",
            "templates/partials/seller_workspace_utility.html",
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

    def test_beta_page_renders_for_seller(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.get("/marketplaces/listings/beta")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "marketplace-catalog-app",
            response.get_data(as_text=True),
        )

    def test_primary_catalog_is_vue_with_compatible_alias_and_no_js_fallback(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            primary = self.client.get('/marketplaces/listings/?marketplace=ozon')
            alias = self.client.get('/marketplaces/listings/beta?marketplace=ozon')
            classic = self.client.get('/marketplaces/listings/classic?marketplace=ozon')
        for response in (primary, alias, classic):
            self.assertEqual(response.status_code, 200)
        html = primary.get_data(as_text=True)
        self.assertIn('marketplace-catalog-app', html)
        self.assertIn('vendor/vue-3.4.38.global.prod.js', html)
        self.assertNotIn('cdn.jsdelivr.net/npm/vue', html)
        self.assertNotIn('Классическая версия', html)
        self.assertIn('"classic": "/marketplaces/listings/classic"', html)
        self.assertIn('"base": "/marketplaces/listings/"', html)
        self.assertIn('marketplace-catalog-app', alias.get_data(as_text=True))
        self.assertIn('catalog-products', classic.get_data(as_text=True))
        self.assertNotIn('Foreign Ozon title', html)

    def test_beta_page_requires_seller_profile(self):
        user_patch, login_patch = self._auth(None)
        with user_patch, login_patch:
            response = self.client.get("/marketplaces/listings/beta")
        self.assertEqual(response.status_code, 403)

    def test_update_draft_context_returns_exact_ready_contract(self):
        expected = {
            "listing_id": self.own_id,
            "link_version": 1,
            "link_status": "linked",
            "link_source": "exact_source_identity",
            "link_evidence_sha256": "a" * 64,
            "linked_at": "2026-11-21T10:00:00",
            "offer_id": "own-offer",
            "external_product_id": "101",
            "external_category_id": "10",
            "external_type_id": "20",
            "source_fact_hash": "b" * 64,
            "baseline_fingerprint": "c" * 64,
            "baseline_contract_version": "test-contract-v1",
        }
        service_context = {
            "status": "ready",
            "account_id": self.account1_id,
            "account_label": "Ozon One",
            "imported_product_id": self.own_source_id,
            "expected_existing_listing": expected,
        }
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService,
            "existing_listing_update_draft_context",
            return_value=service_context,
        ) as context:
            response = self.client.get(
                f"/marketplaces/listings/{self.own_id}/update-draft-context",
                headers={"Accept": "application/json"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.get_json(), {
            "success": True,
            "status": "ready",
            "account_id": self.account1_id,
            "account_label": "Ozon One",
            "imported_product_id": self.own_source_id,
            "expected_existing_listing": expected,
        })
        context.assert_called_once_with(
            seller_id=self.seller1_id,
            listing_id=self.own_id,
        )

    def test_update_draft_context_existing_uses_canonical_draft_detail_url(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService,
            "existing_listing_update_draft_context",
            return_value={
                "status": "existing",
                "account_id": self.account1_id,
                "account_label": "Ozon One",
                "imported_product_id": self.own_source_id,
                "draft_id": 987,
            },
        ):
            response = self.client.get(
                f"/marketplaces/listings/{self.own_id}/update-draft-context",
                headers={"Accept": "application/json"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "success": True,
            "status": "existing",
            "account_id": self.account1_id,
            "account_label": "Ozon One",
            "imported_product_id": self.own_source_id,
            "draft_url": "/marketplaces/drafts/987",
        })

    def test_update_draft_context_is_seller_scoped_and_requires_exact_link(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            foreign = self.client.get(
                f"/marketplaces/listings/{self.foreign_id}/update-draft-context",
                headers={"Accept": "application/json"},
            )
            unlinked = self.client.get(
                f"/marketplaces/listings/{self.own_id}/update-draft-context",
                headers={"Accept": "application/json"},
            )

        self.assertEqual(foreign.status_code, 404)
        self.assertFalse(foreign.get_json()["success"])
        self.assertIn("не найден", foreign.get_json()["error"])
        self.assertEqual(unlinked.status_code, 409)
        self.assertFalse(unlinked.get_json()["success"])
        self.assertIn("не привязана", unlinked.get_json()["error"])

    def test_update_draft_context_requires_feature_flag_and_seller_profile(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService,
            "existing_listing_update_draft_context",
        ) as context:
            disabled = self.client.get(
                f"/marketplaces/listings/{self.own_id}/update-draft-context",
                headers={"Accept": "application/json"},
            )
        context.assert_not_called()
        self.assertEqual(disabled.status_code, 404)
        self.assertEqual(disabled.get_json()["code"], "ozon_feature_disabled")

        self.app.config["MARKETPLACE_OZON_ENABLED"] = True
        user_patch, login_patch = self._auth(None)
        with user_patch, login_patch:
            no_seller = self.client.get(
                f"/marketplaces/listings/{self.own_id}/update-draft-context",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(no_seller.status_code, 403)
        self.assertEqual(no_seller.get_json()["code"], "seller_profile_required")

    def _existing_update_preview_document(self, listing_ids=None):
        listing_ids = listing_ids or [self.own_id]
        return {
            "account_id": self.account1_id,
            "account_label": "Ozon One",
            "listing_ids": listing_ids,
            "rows": [{
                "listing_id": listing_id,
                "title": f"Listing {listing_id}",
                "offer_id": f"offer-{listing_id}",
                "status": "ready",
            } for listing_id in listing_ids],
            "digest": "a" * 64,
        }

    def _existing_update_token(self, *, account_id=None, listing_ids=None):
        with self.app.test_request_context():
            return marketplace_listing_routes._issue_existing_update_token(
                user_id=self.seller1_id,
                seller_id=self.seller1_id,
                account_id=account_id or self.account1_id,
                listing_ids=listing_ids or [self.own_id],
                digest="a" * 64,
                return_to=(
                    "/marketplaces/listings/?marketplace=ozon"
                    f"&account_id={account_id or self.account1_id}"
                ),
            )

    def test_existing_update_preview_is_exact_local_scope_signed_and_private(self):
        requested_ids = [self.own_id]
        return_to = (
            "/marketplaces/listings/beta?marketplace=ozon"
            f"&account_id={self.account1_id}&search=coat&page=2"
        )
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch.object(
            OzonExistingUpdateBatch,
            "preview",
            return_value=self._existing_update_preview_document(requested_ids),
        ) as preview:
            response = self.client.post(
                "/marketplaces/listings/update-drafts/preview",
                json={
                    "account_id": self.account1_id,
                    "listing_ids": requested_ids,
                    "return_to": return_to,
                },
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        result = response.get_json()["preview"]
        self.assertEqual(result["listing_ids"], requested_ids)
        self.assertEqual([row["listing_id"] for row in result["rows"]], requested_ids)
        self.assertEqual(result["rows"][0]["status"], "ready")
        self.assertNotIn("digest", result)
        self.assertTrue(result["token"])
        preview.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.account1_id,
            listing_ids=requested_ids,
        )
        with self.app.test_request_context():
            payload = marketplace_listing_routes._existing_update_serializer().loads(
                result["token"],
                max_age=marketplace_listing_routes._EXISTING_UPDATE_TOKEN_MAX_AGE,
            )
        self.assertEqual(payload["user_id"], self.seller1_id)
        self.assertEqual(payload["seller_id"], self.seller1_id)
        self.assertEqual(payload["account_id"], self.account1_id)
        self.assertEqual(payload["listing_ids"], requested_ids)
        self.assertEqual(payload["digest"], "a" * 64)
        self.assertEqual(payload["return_to"], return_to)

    def test_existing_update_apply_links_only_successful_exact_draft_ids(self):
        rows = [
            {"listing_id": self.own_id, "title": "Ready", "offer_id": "own-offer",
             "status": "created", "draft_id": 701},
            {"listing_id": self.own_id + 1, "title": "Existing", "offer_id": "offer-2",
             "status": "existing", "draft_id": 702},
            {"listing_id": self.own_id + 2, "title": "Blocked", "offer_id": "offer-3",
             "status": "blocked", "message": "Нужно перечитать"},
        ]
        report = {
            "account_id": self.account1_id,
            "account_label": "Ozon One",
            "listing_ids": [self.own_id, self.own_id + 1, self.own_id + 2],
            "rows": rows,
        }
        token = self._existing_update_token(
            listing_ids=report["listing_ids"],
        )
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch.object(
            OzonExistingUpdateBatch, "apply", return_value=report,
        ) as apply:
            response = self.client.post(
                "/marketplaces/listings/update-drafts/apply",
                json={"token": token, "confirm_local": True},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        result = response.get_json()["report"]
        self.assertEqual([row["status"] for row in result["rows"]],
                         ["created", "existing", "blocked"])
        self.assertEqual(result["editor_url"].split("?", 1)[0],
                         "/marketplaces/ozon/uploads/selection/repair")
        query = parse_qs(urlsplit(result["editor_url"]).query)
        self.assertEqual(query["account_id"], [str(self.account1_id)])
        self.assertEqual(query["draft_ids"], ["701,702"])
        self.assertEqual(query["return_to"], [
            "/marketplaces/listings/?marketplace=ozon"
            f"&account_id={self.account1_id}"
        ])
        apply.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.account1_id,
            listing_ids=report["listing_ids"],
            expected_digest="a" * 64,
        )

    def test_existing_update_apply_all_blocked_has_no_editor_link(self):
        listing_ids = [self.own_id]
        token = self._existing_update_token(listing_ids=listing_ids)
        report = {
            "account_id": self.account1_id,
            "account_label": "Ozon One",
            "listing_ids": listing_ids,
            "rows": [{"listing_id": self.own_id, "status": "blocked",
                      "title": "Own", "offer_id": "own-offer"}],
        }
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch.object(
            OzonExistingUpdateBatch, "apply", return_value=report,
        ):
            response = self.client.post(
                "/marketplaces/listings/update-drafts/apply",
                json={"token": token, "confirm_local": True},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIsNone(response.json["report"]["editor_url"])

    def test_existing_update_preview_rejects_bad_ids_shop_url_and_unknown_fields(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        wrong_shop_return = (
            "/marketplaces/listings/?marketplace=ozon"
            f"&account_id={self.account2_id}"
        )
        external_return = "https://example.invalid/marketplaces/listings/?marketplace=ozon"
        bad_requests = [
            {"account_id": self.account1_id, "listing_ids": [self.own_id, self.own_id]},
            {"account_id": self.account1_id, "listing_ids": [True]},
            {"account_id": self.account1_id, "listing_ids": ["1"]},
            {"account_id": self.account1_id, "listing_ids": [self.own_id], "return_to": wrong_shop_return},
            {"account_id": self.account1_id, "listing_ids": [self.own_id], "return_to": external_return},
            {"account_id": self.account1_id, "listing_ids": [self.own_id], "unexpected": 1},
        ]
        with user_patch, login_patch, patch.object(
            OzonExistingUpdateBatch,
            "preview",
            side_effect=AssertionError("invalid scope must not reach local preview"),
        ) as preview:
            for body in bad_requests:
                with self.subTest(body=body):
                    response = self.client.post(
                        "/marketplaces/listings/update-drafts/preview",
                        json=body,
                    )
                    self.assertEqual(response.status_code, 400, response.get_json())
                    self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        preview.assert_not_called()

    def test_existing_update_preview_cross_seller_listing_fails_closed(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch.object(
            OzonExistingUpdateBatch,
            "preview",
            side_effect=MarketplaceDraftNotFound("Листинг не найден"),
        ) as preview:
            response = self.client.post(
                "/marketplaces/listings/update-drafts/preview",
                json={"account_id": self.account1_id, "listing_ids": [self.foreign_id]},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["code"], "marketplace_draft_not_found")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        preview.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.account1_id,
            listing_ids=[self.foreign_id],
        )

    def test_existing_update_apply_token_is_user_bound_and_expires(self):
        token = self._existing_update_token()
        user_patch, login_patch = self._auth(self.seller2_id)
        with user_patch, login_patch, patch.object(
            OzonExistingUpdateBatch, "apply",
            side_effect=AssertionError("wrong seller must not apply"),
        ) as apply:
            wrong_seller = self.client.post(
                "/marketplaces/listings/update-drafts/apply",
                json={"token": token, "confirm_local": True},
            )
        self.assertEqual(wrong_seller.status_code, 409)
        self.assertEqual(wrong_seller.json["code"], "existing_update_review_invalid")
        apply.assert_not_called()

        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch.object(
            marketplace_listing_routes,
            "_EXISTING_UPDATE_TOKEN_MAX_AGE",
            -1,
        ), patch.object(
            OzonExistingUpdateBatch, "apply",
            side_effect=AssertionError("expired token must not apply"),
        ) as apply:
            expired = self.client.post(
                "/marketplaces/listings/update-drafts/apply",
                json={"token": token, "confirm_local": True},
            )
        self.assertEqual(expired.status_code, 409)
        self.assertEqual(expired.json["code"], "existing_update_review_invalid")
        apply.assert_not_called()

    def test_existing_update_posts_keep_global_csrf_protection(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch.object(
            OzonExistingUpdateBatch,
            "preview",
            side_effect=AssertionError("CSRF rejection must happen before preview"),
        ) as preview:
            response = self.client.post(
                "/marketplaces/listings/update-drafts/preview",
                json={"account_id": self.account1_id, "listing_ids": [self.own_id]},
            )
        self.assertEqual(response.status_code, 400)
        preview.assert_not_called()

    def test_catalog_bootstrap_and_classic_setup_keep_last_completed_snapshot(self):
        with self.app.app_context():
            marketplace_id = db.session.get(
                Marketplace,
                db.session.get(SellerMarketplaceAccount, self.account1_id).marketplace_id,
            ).id
            second_account = SellerMarketplaceAccount(
                seller_id=self.seller1_id,
                marketplace_id=marketplace_id,
                external_account_id="route-one-second-client",
                label="Ozon One Second",
                is_active=True,
                connection_status="connected",
            )
            db.session.add(second_account)
            db.session.flush()
            def sync(account_id, status, completed_at=None):
                return MarketplaceCatalogSync(
                    seller_id=self.seller1_id,
                    marketplace_id=marketplace_id,
                    account_id=account_id,
                    status=status,
                    phase="completed" if status == "completed" else "active",
                    completed_at=completed_at,
                )

            db.session.add_all([
                sync(self.account1_id, "completed", datetime(2026, 11, 18, 9, 10, 11)),
                sync(self.account1_id, "running"),
                sync(second_account.id, "completed", datetime(2026, 11, 17, 8, 9, 10)),
                sync(second_account.id, "failed"),
            ])
            db.session.commit()
            second_account_id = second_account.id
            expected = {
                self.account1_id: "2026-11-18T09:10:11Z",
                second_account_id: "2026-11-17T08:09:10Z",
            }
            sync_count = MarketplaceCatalogSync.query.count()

        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            beta = self.client.get("/marketplaces/listings/")
        self.assertEqual(beta.status_code, 200)
        match = re.search(
            r'<script type="application/json" id="mcat-bootstrap">(.*?)</script>',
            beta.get_data(as_text=True),
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        bootstrap = json.loads(match.group(1))
        payload_by_id = {account["id"]: account for account in bootstrap["accounts"]}
        self.assertEqual(payload_by_id[self.account1_id]["last_completed_at"], expected[self.account1_id])
        self.assertEqual(payload_by_id[second_account_id]["last_completed_at"], expected[second_account_id])
        self.assertEqual(payload_by_id[self.account1_id]["last_sync"]["status"], "running")
        self.assertEqual(payload_by_id[second_account_id]["last_sync"]["status"], "failed")

        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch(
            "routes.marketplace_listings.render_template",
            return_value="classic",
        ) as render:
            classic = self.client.get("/marketplaces/listings/classic?marketplace=ozon")
        self.assertEqual(classic.status_code, 200)
        context = render.call_args.kwargs
        self.assertEqual(context["catalog_completed_at"], expected)
        self.assertEqual(context["setup_config"]["catalog_completed_at"], expected)
        with self.app.app_context():
            self.assertEqual(MarketplaceCatalogSync.query.count(), sync_count)

    def test_list_api_exposes_bounded_primary_image(self):
        with self.app.app_context():
            listing = db.session.get(MarketplaceListing, self.own_id)
            listing.media_json = (
                '{"primary_image": "https://cdn.test/img-1.jpg",'
                ' "images": ["https://cdn.test/img-2.jpg"]}'
            )
            db.session.commit()
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.get(
                "/marketplaces/listings/api",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 200)
        items = {item["id"]: item for item in response.get_json()["items"]}
        self.assertEqual(
            items[self.own_id]["primary_image"],
            "https://cdn.test/img-1.jpg",
        )

    def test_list_api_expands_wb_photo_slots_into_cdn_preview(self):
        with self.app.app_context():
            wb = Marketplace(
                name="Wildberries",
                code="wb",
                adapter_code="wb",
                is_active=True,
            )
            db.session.add(wb)
            db.session.flush()
            product = Product(
                seller_id=self.seller1_id,
                nm_id=123456789,
                photos_json="[1, 2]",
            )
            db.session.add(product)
            db.session.flush()
            listing = MarketplaceListing(
                seller_id=self.seller1_id,
                marketplace_id=wb.id,
                legacy_product_id=product.id,
                offer_id="wb-offer",
                external_product_id="wb-123",
                title="WB projection",
                normalized_status="active",
                sync_fingerprint="c" * 64,
            )
            db.session.add(listing)
            db.session.commit()
            listing_id = listing.id
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.get(
                "/marketplaces/listings/api?marketplace=wb",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 200)
        items = {item["id"]: item for item in response.get_json()["items"]}
        row = items[listing_id]
        self.assertIn("wbbasket.ru", row["primary_image"])
        self.assertTrue(row["primary_image"].endswith("/1.webp"))
        self.assertTrue(row["hover_image"].endswith("/2.webp"))

    def test_groups_api_merges_channels_of_one_canonical_product(self):
        with self.app.app_context():
            wb = Marketplace(
                name="Wildberries",
                code="wb",
                adapter_code="wb",
                is_active=True,
            )
            db.session.add(wb)
            db.session.flush()
            wb_product = Product(
                seller_id=self.seller1_id,
                nm_id=555001,
                photos_json="[1]",
            )
            db.session.add(wb_product)
            db.session.flush()
            wb_listing = MarketplaceListing(
                seller_id=self.seller1_id,
                marketplace_id=wb.id,
                legacy_product_id=wb_product.id,
                imported_product_id=self.own_source_id,
                offer_id="wb-group-offer",
                external_product_id="wb-group-1",
                title="WB side",
                normalized_status="active",
                sync_fingerprint="d" * 64,
            )
            own = db.session.get(MarketplaceListing, self.own_id)
            own.imported_product_id = self.own_source_id
            db.session.add(wb_listing)
            db.session.commit()
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.get(
                "/marketplaces/listings/api/groups",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["pagination"]["total"], 1)
        group = payload["items"][0]
        self.assertEqual(group["key"], f"p{self.own_source_id}")
        self.assertEqual(group["listing_count"], 2)
        codes = [row["marketplace_code"] for row in group["listings"]]
        self.assertEqual(codes, ["wb", "ozon"])

    def test_beta_detail_renders_channel_members_and_is_tenant_scoped(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            own = self.client.get(f"/marketplaces/listings/beta/{self.own_id}")
            canonical = self.client.get(f"/marketplaces/listings/view/{self.own_id}")
            canonical_foreign = self.client.get(f"/marketplaces/listings/view/{self.foreign_id}")
            foreign = self.client.get(
                f"/marketplaces/listings/beta/{self.foreign_id}",
            )
        self.assertEqual(own.status_code, 200)
        self.assertEqual(canonical.status_code, 200)
        self.assertEqual(canonical_foreign.status_code, 404)
        self.assertIn(
            "marketplace-detail-app",
            own.get_data(as_text=True),
        )
        self.assertEqual(foreign.status_code, 404)

    def test_facets_api_counts_statuses_and_channels_in_one_call(self):
        with self.app.app_context():
            listing = db.session.get(MarketplaceListing, self.own_id)
            listing.normalized_status = "error"
            db.session.commit()
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.get(
                "/marketplaces/listings/api/facets",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["statuses"]["error"], 1)
        self.assertEqual(payload["statuses"]["all"], 1)
        self.assertEqual(payload["channels"]["ozon"], 1)
        self.assertEqual(
            payload["channels"][f"ozon:{self.account1_id}"],
            1,
        )
        # Чужой продавец в счётчики не попадает
        self.assertEqual(payload["channels"]["all"], 1)

    def test_search_matches_cyrillic_regardless_of_case(self):
        with self.app.app_context():
            listing = db.session.get(MarketplaceListing, self.own_id)
            listing.title = "Свеча ароматическая"
            db.session.commit()
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            lower = self.client.get(
                "/marketplaces/listings/api?search=свеча",
                headers={"Accept": "application/json"},
            )
            upper = self.client.get(
                "/marketplaces/listings/api?search=СВЕЧА",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(lower.get_json()["pagination"]["total"], 1)
        self.assertEqual(upper.get_json()["pagination"]["total"], 1)

    def test_beta_detail_json_bootstrap_includes_members_and_gallery(self):
        with self.app.app_context():
            listing = db.session.get(MarketplaceListing, self.own_id)
            listing.media_json = (
                '{"primary_image": "https://cdn.test/a.jpg",'
                ' "images": ["https://cdn.test/b.jpg"]}'
            )
            db.session.commit()
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.get(
                f"/marketplaces/listings/beta/{self.own_id}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["listing"]["id"], self.own_id)
        self.assertEqual(
            payload["gallery"],
            ["https://cdn.test/a.jpg", "https://cdn.test/b.jpg"],
        )
        self.assertEqual(
            [row["id"] for row in payload["members"]],
            [self.own_id],
        )
        self.assertIn("attribute_names", payload)

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
        job = {'job_uid': 'synthetic-job', 'status': 'pending', 'message': 'Загружаем в фоне'}
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch, patch(
            'routes.marketplace_listings.enqueue_account_sync', return_value=job,
        ) as enqueue, patch.object(MarketplaceListingService, 'sync_ozon_account') as sync:
            loose = self.client.post(
                f"/marketplaces/listings/accounts/{self.account1_id}/sync",
                json={"max_pages": "1", "force_restart": "false"},
            )
            valid = self.client.post(
                f"/marketplaces/listings/accounts/{self.account1_id}/sync",
                json={"max_pages": 1, "force_restart": False},
            )
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(valid.status_code, 202)
        self.assertEqual(valid.get_json()['job']['status'], 'pending')
        self.assertIn('/setup', valid.get_json()['status_url'])
        sync.assert_not_called()
        enqueue.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.account1_id,
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
