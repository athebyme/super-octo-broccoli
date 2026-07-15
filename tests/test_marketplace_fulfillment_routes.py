import json
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    Marketplace,
    MarketplaceCancellation,
    MarketplacePosting,
    MarketplaceReturn,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_fulfillment import register_marketplace_fulfillment_routes
from services.marketplace_fulfillment import MarketplaceFulfillmentService


class MarketplaceFulfillmentRoutesTest(unittest.TestCase):
    def setUp(self):
        observed_at = datetime.utcnow()
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-fulfillment-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_fulfillment_routes(self.app)
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
            own = SellerMarketplaceAccount(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                external_account_id="own-client",
                label="Own Ozon",
                is_active=True,
                connection_status="connected",
            )
            foreign = SellerMarketplaceAccount(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                external_account_id="foreign-client",
                label="Foreign Secret Label",
                is_active=True,
                connection_status="connected",
            )
            db.session.add_all([own, foreign])
            db.session.flush()
            posting = MarketplacePosting(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=own.id,
                posting_number="own-posting",
                fulfillment_kind="fbs",
                status="delivered",
                source_endpoint="/v4/posting/fbs/list",
                sync_fingerprint="a" * 64,
                last_seen_at=observed_at,
            )
            foreign_posting = MarketplacePosting(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                account_id=foreign.id,
                posting_number="foreign-secret-posting",
                fulfillment_kind="fbo",
                status="delivered",
                source_endpoint="/v3/posting/fbo/list",
                sync_fingerprint="b" * 64,
                last_seen_at=observed_at,
            )
            db.session.add_all([posting, foreign_posting])
            db.session.flush()
            db.session.add_all([
                MarketplaceReturn(
                    seller_id=self.seller1_id,
                    marketplace_id=marketplace.id,
                    account_id=own.id,
                    posting_id=posting.id,
                    source_kind="fbo_fbs",
                    external_return_id="ret-1",
                    posting_number="own-posting",
                    fulfillment_kind="fbs",
                    status="MovingToSeller",
                    quantity=1,
                    source_endpoint="/v1/returns/list",
                    sync_fingerprint="c" * 64,
                    last_seen_at=observed_at,
                ),
                MarketplaceCancellation(
                    seller_id=self.seller1_id,
                    marketplace_id=marketplace.id,
                    account_id=own.id,
                    posting_id=posting.id,
                    source_kind="posting_fbs",
                    external_cancellation_id="cancel-1",
                    posting_number="own-posting",
                    status="cancelled",
                    source_endpoint="/v4/posting/fbs/list",
                    sync_fingerprint="d" * 64,
                    last_seen_at=observed_at,
                ),
            ])
            db.session.commit()
            self.own_account_id = own.id
            self.foreign_account_id = foreign.id
            self.own_posting_id = posting.id

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
            patch("routes.marketplace_fulfillment.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_all_read_routes_are_exact_account_scoped(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            orders = self.client.get(
                f"/marketplaces/api/orders?account_id={self.own_account_id}",
            )
            returns = self.client.get(
                f"/marketplaces/api/returns?account_id={self.own_account_id}",
            )
            cancellations = self.client.get(
                f"/marketplaces/api/cancellations?account_id={self.own_account_id}",
            )
            detail = self.client.get(
                f"/marketplaces/api/orders/{self.own_posting_id}"
                f"?account_id={self.own_account_id}",
            )
            foreign = self.client.get(
                f"/marketplaces/api/orders?account_id={self.foreign_account_id}",
            )
        self.assertEqual(orders.status_code, 200)
        self.assertEqual(returns.status_code, 200)
        self.assertEqual(cancellations.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            orders.get_json()["data"]["items"][0]["posting_number"],
            "own-posting",
        )
        self.assertEqual(foreign.status_code, 404)
        encoded = json.dumps(foreign.get_json(), ensure_ascii=False)
        self.assertNotIn("Foreign Secret Label", encoded)
        self.assertNotIn("foreign-secret-posting", encoded)

    def test_scope_smuggling_and_loose_types_fail_before_service(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch.object(
            MarketplaceFulfillmentService,
            "sync_account",
        ) as sync:
            smuggled = self.client.post(
                f"/marketplaces/api/fulfillment/sync?account_id={self.own_account_id}",
                json={
                    "period": "30d",
                    "force": False,
                    "account_id": self.foreign_account_id,
                },
            )
            loose_bool = self.client.post(
                f"/marketplaces/api/fulfillment/sync?account_id={self.own_account_id}",
                json={"period": "30d", "force": "false"},
            )
            loose_pages = self.client.post(
                f"/marketplaces/api/fulfillment/sync?account_id={self.own_account_id}",
                json={"period": "30d", "force": False, "max_pages": 1.5},
            )
        self.assertEqual(smuggled.status_code, 400)
        self.assertEqual(loose_bool.status_code, 400)
        self.assertEqual(loose_pages.status_code, 400)
        sync.assert_not_called()

    def test_sync_passes_only_authenticated_query_scope(self):
        run = SimpleNamespace(to_public_dict=lambda: {
            "id": 900,
            "account_id": self.own_account_id,
            "status": "completed",
        })
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch.object(
            MarketplaceFulfillmentService,
            "sync_account",
            return_value=run,
        ) as sync:
            response = self.client.post(
                f"/marketplaces/api/fulfillment/sync?account_id={self.own_account_id}",
                json={"period": "7d", "force": True, "max_pages": 4},
            )
        self.assertEqual(response.status_code, 200)
        sync.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.own_account_id,
            period_code="7d",
            force=True,
            max_pages=4,
        )

    def test_feature_flag_blocks_api(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.get(
                f"/marketplaces/api/orders?account_id={self.own_account_id}",
            )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
