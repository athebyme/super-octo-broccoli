import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    Marketplace,
    MarketplaceListing,
    MarketplaceQualityAssessment,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_insights import register_marketplace_insight_routes
from services.marketplace_analytics import MarketplaceAnalyticsService


class MarketplaceInsightRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-insight-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_insight_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.seller1_id, self.user1_id = self._seller("one", "one@test.local")
            self.seller2_id, self.user2_id = self._seller("two", "two@test.local")
            marketplace = Marketplace(
                code="ozon", name="Ozon", adapter_code="ozon", is_active=True,
            )
            db.session.add(marketplace)
            db.session.flush()
            own_account = SellerMarketplaceAccount(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                external_account_id="own-client",
                label="Own Ozon",
                is_active=True,
                connection_status="connected",
            )
            foreign_account = SellerMarketplaceAccount(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                external_account_id="foreign-client",
                label="Foreign Secret Label",
                is_active=True,
                connection_status="connected",
            )
            db.session.add_all([own_account, foreign_account])
            db.session.flush()
            own_listing = MarketplaceListing(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=own_account.id,
                offer_id="own-offer",
                external_product_id="101",
                normalized_status="active",
                is_available=True,
                is_archived=False,
                sync_fingerprint="a" * 64,
            )
            foreign_listing = MarketplaceListing(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                account_id=foreign_account.id,
                offer_id="foreign-secret-offer",
                external_product_id="202",
                normalized_status="active",
                is_available=True,
                is_archived=False,
                sync_fingerprint="b" * 64,
            )
            db.session.add_all([own_listing, foreign_listing])
            db.session.flush()
            db.session.add(MarketplaceQualityAssessment(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=own_account.id,
                listing_id=own_listing.id,
                status="scored",
                severity="good",
                score=80,
                impact=5,
                listing_fingerprint=own_listing.sync_fingerprint,
                reasons_json="[]",
                breakdown_json="{}",
                metrics_json="{}",
            ))
            db.session.commit()
            self.own_account_id = own_account.id
            self.foreign_account_id = foreign_account.id
            self.own_listing_id = own_listing.id
            self.foreign_listing_id = foreign_listing.id

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
        return seller.id, user.id

    @staticmethod
    def _user(seller_id, user_id):
        return SimpleNamespace(
            id=user_id,
            seller=SimpleNamespace(id=seller_id),
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _auth(self):
        user = self._user(self.seller1_id, self.user1_id)
        return (
            patch("routes.marketplace_insights.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_quality_list_and_detail_are_exact_account_scoped(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            own = self.client.get(
                f"/marketplaces/api/quality?account_id={self.own_account_id}",
            )
            foreign = self.client.get(
                f"/marketplaces/api/quality?account_id={self.foreign_account_id}",
            )
            foreign_detail = self.client.get(
                f"/marketplaces/api/quality/{self.foreign_listing_id}"
                f"?account_id={self.own_account_id}",
            )
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.get_json()["items"][0]["entity_kind"], "marketplace_listing")
        self.assertEqual(own.get_json()["items"][0]["account_id"], self.own_account_id)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign_detail.status_code, 404)
        encoded = json.dumps(foreign.get_json(), ensure_ascii=False)
        self.assertNotIn("Foreign Secret Label", encoded)
        self.assertNotIn("foreign-secret-offer", foreign_detail.get_data(as_text=True))

    def test_scope_smuggling_and_loose_types_are_rejected_before_sync(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch.object(
            MarketplaceAnalyticsService,
            "sync_account",
        ) as sync:
            smuggled = self.client.post(
                f"/marketplaces/api/analytics/sync?account_id={self.own_account_id}",
                json={"period": "7d", "force": False, "account_id": self.foreign_account_id},
            )
            loose = self.client.post(
                f"/marketplaces/api/analytics/sync?account_id={self.own_account_id}",
                json={"period": "7d", "force": "false"},
            )
            quality_smuggled = self.client.post(
                f"/marketplaces/api/quality/recompute?account_id={self.own_account_id}",
                json={"listing_ids": [self.own_listing_id], "marketplace": "wb"},
            )
        self.assertEqual(smuggled.status_code, 400)
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(quality_smuggled.status_code, 400)
        sync.assert_not_called()

    def test_foreign_analytics_account_denied_before_any_provider_call(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.get(
                "/marketplaces/api/analytics/summary"
                f"?account_id={self.foreign_account_id}&period=30d",
            )
            products = self.client.get(
                "/marketplaces/api/analytics/products"
                f"?account_id={self.foreign_account_id}&period=30d",
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(products.status_code, 404)
        self.assertNotIn("Foreign Secret Label", response.get_data(as_text=True))

    def test_analytics_routes_pass_only_authenticated_query_scope(self):
        run = SimpleNamespace(
            to_public_dict=lambda: {
                "id": 900,
                "account_id": self.own_account_id,
                "status": "completed",
            },
        )
        summary_data = {
            "account_id": self.own_account_id,
            "period": "7d",
            "comparison": {"cross_marketplace_comparable": False},
        }
        products_data = {
            "account_id": self.own_account_id,
            "items": [],
            "total": 0,
        }
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch.object(
            MarketplaceAnalyticsService,
            "get_summary",
            return_value=summary_data,
        ) as summary, patch.object(
            MarketplaceAnalyticsService,
            "get_products",
            return_value=products_data,
        ) as products, patch.object(
            MarketplaceAnalyticsService,
            "sync_account",
            return_value=run,
        ) as sync:
            summary_response = self.client.get(
                "/marketplaces/api/analytics/summary"
                f"?account_id={self.own_account_id}&period=7d",
            )
            products_response = self.client.get(
                "/marketplaces/api/analytics/products"
                f"?account_id={self.own_account_id}&period=7d&page=2&per_page=10",
            )
            sync_response = self.client.post(
                "/marketplaces/api/analytics/sync"
                f"?account_id={self.own_account_id}",
                json={"period": "7d", "force": True},
            )

        self.assertEqual(summary_response.status_code, 200)
        self.assertFalse(
            summary_response.get_json()["data"]["comparison"]
            ["cross_marketplace_comparable"],
        )
        self.assertEqual(products_response.status_code, 200)
        self.assertEqual(sync_response.status_code, 200)
        summary.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.own_account_id,
            period_code="7d",
        )
        products.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.own_account_id,
            period_code="7d",
            sort_by="ordered_revenue_rub",
            sort_dir="desc",
            search="",
            page=2,
            per_page=10,
        )
        sync.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.own_account_id,
            period_code="7d",
            force=True,
            max_pages=2,
        )

    def test_master_flag_closes_all_new_endpoints(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            quality = self.client.get(
                f"/marketplaces/api/quality?account_id={self.own_account_id}",
            )
            analytics = self.client.get(
                "/marketplaces/api/analytics/summary"
                f"?account_id={self.own_account_id}&period=30d",
            )
        self.assertEqual(quality.status_code, 404)
        self.assertEqual(analytics.status_code, 404)


if __name__ == "__main__":
    unittest.main()
