"""HTTP scope and local-only boundaries for the Ozon inbox."""

from datetime import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    Marketplace,
    MarketplaceInboxItem,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_inbox import register_marketplace_inbox_routes
from services.marketplace_inbox import MarketplaceInboxService


class MarketplaceInboxRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-inbox-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_inbox_routes(self.app)
        self.app.url_build_error_handlers.append(
            lambda _error, _endpoint, _values: "#"
        )
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.seller1_id, self.user1_id = self._seller(
                "inbox-one", "inbox-one@test.local",
            )
            self.seller2_id, self.user2_id = self._seller(
                "inbox-two", "inbox-two@test.local",
            )
            marketplace = Marketplace(
                code="ozon",
                name="Ozon",
                adapter_code="ozon",
                is_active=True,
            )
            db.session.add(marketplace)
            db.session.flush()
            own = SellerMarketplaceAccount(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                external_account_id="own-inbox",
                label="Own Inbox",
                is_active=True,
                is_default=True,
                connection_status="connected",
                capabilities_json=json.dumps(["reviews_read", "questions_read"]),
            )
            foreign = SellerMarketplaceAccount(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                external_account_id="foreign-inbox",
                label="Foreign Secret Cabinet",
                is_active=True,
                is_default=True,
                connection_status="connected",
                capabilities_json=json.dumps(["reviews_read", "questions_read"]),
            )
            db.session.add_all([own, foreign])
            db.session.flush()
            own_item = self._item(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=own.id,
                external_id="own-review",
                text="Own review text",
            )
            foreign_item = self._item(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                account_id=foreign.id,
                external_id="foreign-secret-review",
                text="Foreign secret review text",
            )
            db.session.add_all([own_item, foreign_item])
            db.session.commit()
            self.own_account_id = own.id
            self.foreign_account_id = foreign.id
            self.own_item_id = own_item.id

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
    def _item(*, seller_id, marketplace_id, account_id, external_id, text):
        return MarketplaceInboxItem(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            account_id=account_id,
            source_kind="review",
            external_id=external_id,
            external_sku="101",
            match_status="unmatched",
            text=text,
            rating=5,
            provider_status="NEW",
            published_at=datetime.utcnow(),
            comments_count=0,
            photos_count=0,
            videos_count=0,
            answers_count=0,
            reply_eligible=True,
            source_endpoint="/v2/review/list",
            source_fingerprint="a" * 64,
            last_seen_at=datetime.utcnow(),
        )

    @staticmethod
    def _user(seller_id, user_id):
        return SimpleNamespace(
            id=user_id,
            username="inbox-one",
            seller=SimpleNamespace(id=seller_id, company_name="Inbox One"),
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _auth(self):
        user = self._user(self.seller1_id, self.user1_id)
        return (
            patch("routes.marketplace_inbox.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_read_route_is_exact_account_scoped_and_does_not_leak_foreign_rows(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            own = self.client.get(
                f"/marketplaces/api/reviews?account_id={self.own_account_id}",
            )
            foreign = self.client.get(
                f"/marketplaces/api/reviews?account_id={self.foreign_account_id}",
            )

        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.get_json()["data"]["items"][0]["external_id"], "own-review")
        self.assertFalse(
            own.get_json()["data"]["capability"]["provider_send_enabled"]
        )
        self.assertIn("account_ready", own.get_json()["data"]["capability"])
        self.assertEqual(foreign.status_code, 404)
        encoded = json.dumps(foreign.get_json(), ensure_ascii=False)
        self.assertNotIn("Foreign Secret Cabinet", encoded)
        self.assertNotIn("foreign-secret-review", encoded)

    def test_page_renders_explicit_read_only_and_ai_privacy_boundaries(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.get(
                f"/marketplaces/reviews?account_id={self.own_account_id}",
            )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Ничего не отправляется в Ozon", html)
        self.assertIn("настроенному AI-провайдеру", html)
        self.assertNotIn("Foreign Secret Cabinet", html)

    def test_scope_smuggling_duplicate_query_and_loose_types_fail_before_service(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch.object(
            MarketplaceInboxService,
            "sync_kind",
        ) as sync:
            smuggled = self.client.post(
                f"/marketplaces/api/reviews/sync?account_id={self.own_account_id}",
                json={
                    "source_kind": "review",
                    "force": False,
                    "account_id": self.foreign_account_id,
                },
            )
            duplicate = self.client.post(
                "/marketplaces/api/reviews/sync"
                f"?account_id={self.own_account_id}"
                f"&account_id={self.foreign_account_id}",
                json={"source_kind": "review", "force": False},
            )
            duplicate_kind = self.client.get(
                "/marketplaces/api/reviews"
                f"?account_id={self.own_account_id}"
                "&source_kind=review&source_kind=question",
            )
            unknown_query = self.client.post(
                f"/marketplaces/api/reviews/sync?account_id={self.own_account_id}"
                "&seller_id=999",
                json={"source_kind": "review", "force": False},
            )
            noncanonical_account = self.client.post(
                "/marketplaces/api/reviews/sync?account_id=%D9%A1",
                json={"source_kind": "review", "force": False},
            )
            loose_bool = self.client.post(
                f"/marketplaces/api/reviews/sync?account_id={self.own_account_id}",
                json={"source_kind": "review", "force": "false"},
            )
            loose_pages = self.client.post(
                f"/marketplaces/api/reviews/sync?account_id={self.own_account_id}",
                json={"source_kind": "review", "force": False, "max_pages": 1.5},
            )

        self.assertEqual(smuggled.status_code, 400)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(duplicate_kind.status_code, 400)
        self.assertEqual(unknown_query.status_code, 400)
        self.assertEqual(noncanonical_account.status_code, 400)
        self.assertEqual(loose_bool.status_code, 400)
        self.assertEqual(loose_pages.status_code, 400)
        sync.assert_not_called()

    def test_sync_passes_only_authenticated_query_scope(self):
        run = SimpleNamespace(to_public_dict=lambda: {
            "id": 900,
            "account_id": self.own_account_id,
            "source_kind": "question",
            "status": "running",
        })
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch.object(
            MarketplaceInboxService,
            "sync_kind",
            return_value=run,
        ) as sync:
            response = self.client.post(
                f"/marketplaces/api/reviews/sync?account_id={self.own_account_id}",
                json={"source_kind": "question", "force": True, "max_pages": 4},
            )

        self.assertEqual(response.status_code, 200)
        sync.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.own_account_id,
            source_kind="question",
            force=True,
            max_pages=4,
        )

    def test_draft_endpoint_is_local_service_only_and_uses_authenticated_user(self):
        draft = SimpleNamespace(to_public_dict=lambda: {
            "id": 901,
            "status": "draft",
            "generation_mode": "ai",
            "text": "Local draft",
        })
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch.object(
            MarketplaceInboxService,
            "create_reply_draft",
            return_value=draft,
        ) as create:
            response = self.client.post(
                f"/marketplaces/api/reviews/{self.own_item_id}/draft"
                f"?account_id={self.own_account_id}",
                json={"generation_mode": "ai"},
            )

        self.assertEqual(response.status_code, 200)
        create.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.own_account_id,
            item_id=self.own_item_id,
            generation_mode="ai",
            created_by_user_id=self.user1_id,
        )

    def test_feature_flag_blocks_inbox_api(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.get(
                f"/marketplaces/api/reviews?account_id={self.own_account_id}",
            )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
