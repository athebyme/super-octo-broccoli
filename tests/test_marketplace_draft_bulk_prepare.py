# -*- coding: utf-8 -*-
"""Bulk-prepare drafts endpoint: tenant scope, strict ids, flag, counting."""

from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceProductDraft,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_drafts import register_marketplace_draft_routes
from services.marketplace_drafts import MarketplaceDraftService


class MarketplaceDraftBulkPrepareTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-draft-bulk",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_draft_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.seller1_id, self.user1_id = self._seller(
                "bulk-one", "bulk-one@test.local"
            )
            self.seller2_id, self.user2_id = self._seller(
                "bulk-two", "bulk-two@test.local"
            )
            ozon = Marketplace(
                name="Ozon",
                code="ozon",
                adapter_code="ozon",
                is_active=True,
            )
            db.session.add(ozon)
            db.session.flush()
            self.marketplace_id = ozon.id
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
            with_draft = ImportedProduct(
                seller_id=self.seller1_id,
                external_id="with-draft",
                external_vendor_code="with-draft-offer",
                title="Existing draft source",
            )
            without_draft = ImportedProduct(
                seller_id=self.seller1_id,
                external_id="without-draft",
                external_vendor_code="without-draft-offer",
                title="Missing draft source",
            )
            foreign_source = ImportedProduct(
                seller_id=self.seller2_id,
                external_id="foreign-source",
                external_vendor_code="foreign-offer",
                title="Foreign source",
            )
            db.session.add_all(
                [account1, account2, with_draft, without_draft, foreign_source]
            )
            db.session.flush()
            existing = MarketplaceProductDraft(
                seller_id=self.seller1_id,
                marketplace_id=ozon.id,
                account_id=account1.id,
                imported_product_id=with_draft.id,
                offer_id="with-draft-offer",
                status="needs_category",
                source_fact_hash="a" * 64,
            )
            db.session.add(existing)
            db.session.commit()
            self.account1_id = account1.id
            self.account2_id = account2.id
            self.with_draft_id = with_draft.id
            self.without_draft_id = without_draft.id
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
        return seller.id, user.id

    @staticmethod
    def _user(seller_id=None, user_id=10):
        seller = SimpleNamespace(id=seller_id) if seller_id else None
        return SimpleNamespace(
            id=user_id,
            seller=seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _auth(self, seller_id, user_id=10):
        user = self._user(seller_id, user_id)
        return (
            patch("routes.marketplace_drafts.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def _post(self, payload):
        return self.client.post(
            "/marketplaces/drafts/bulk-prepare",
            json=payload,
            headers={"Accept": "application/json"},
        )

    def _draft_count(self):
        with self.app.app_context():
            return MarketplaceProductDraft.query.count()

    def test_creates_missing_and_counts_existing(self):
        created_drafts = []

        def fake_create_draft(**kwargs):
            self.assertEqual(kwargs["seller_id"], self.seller1_id)
            self.assertEqual(kwargs["account_id"], self.account1_id)
            draft = MarketplaceProductDraft(
                seller_id=kwargs["seller_id"],
                marketplace_id=self.marketplace_id,
                account_id=kwargs["account_id"],
                imported_product_id=kwargs["imported_product_id"],
                offer_id=f"offer-{kwargs['imported_product_id']}",
                status="draft",
                source_fact_hash="c" * 64,
            )
            db.session.add(draft)
            db.session.commit()
            created_drafts.append(draft.id)
            return SimpleNamespace(id=draft.id, version=1)

        validate_calls = []

        def fake_validate(**kwargs):
            validate_calls.append(kwargs["draft_id"])
            return SimpleNamespace(id=kwargs["draft_id"], version=2)

        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService, "create_draft", side_effect=fake_create_draft
        ), patch.object(
            MarketplaceDraftService, "validate_draft", side_effect=fake_validate
        ):
            response = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": [
                    self.with_draft_id,
                    self.without_draft_id,
                ],
                "validate": True,
            })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["created"], 1)
        self.assertEqual(body["existing"], 1)
        self.assertEqual(body["failed"], 0)
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(created_drafts), 1)
        self.assertEqual(validate_calls, created_drafts)
        self.assertEqual(self._draft_count(), 2)

    def test_foreign_products_are_rejected_whole_request(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService, "create_draft"
        ) as create_mock:
            response = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": [
                    self.without_draft_id,
                    self.foreign_source_id,
                ],
            })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])
        create_mock.assert_not_called()
        self.assertEqual(self._draft_count(), 1)

    def test_foreign_account_is_rejected(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService, "create_draft"
        ) as create_mock:
            response = self._post({
                "account_id": self.account2_id,
                "imported_product_ids": [self.without_draft_id],
            })
        # Чужой кабинет = «не найден» в tenant scope сервиса
        self.assertEqual(response.status_code, 404)
        create_mock.assert_not_called()
        self.assertEqual(self._draft_count(), 1)

    def test_feature_flag_off_blocks_endpoint(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            response = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": [self.without_draft_id],
            })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json()["code"], "ozon_feature_disabled"
        )
        self.assertEqual(self._draft_count(), 1)

    def test_service_is_fail_closed_when_flag_off(self):
        """Kill-switch гейтит и прямые вызовы сервиса в обход route."""
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        from services.marketplace_drafts import (
            MarketplaceDraftValidationError,
        )

        with self.app.test_request_context("/"):
            with self.assertRaises(MarketplaceDraftValidationError):
                MarketplaceDraftService.bulk_prepare(
                    seller_id=self.seller1_id,
                    account_id=self.account1_id,
                    imported_product_ids=[self.without_draft_id],
                )
        self.assertEqual(self._draft_count(), 1)

    def test_non_seller_is_denied(self):
        user_patch, login_patch = self._auth(None, self.user1_id)
        with user_patch, login_patch:
            response = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": [self.without_draft_id],
            })
        self.assertEqual(response.status_code, 403)

    def test_strict_ids_reject_duplicates_bool_and_overflow(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService, "create_draft"
        ) as create_mock:
            duplicate = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": [
                    self.without_draft_id,
                    self.without_draft_id,
                ],
            })
            boolean = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": [True],
            })
            overflow = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": list(range(1, 202)),
            })
            empty = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": [],
            })
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(boolean.status_code, 400)
        self.assertEqual(overflow.status_code, 400)
        self.assertEqual(empty.status_code, 400)
        create_mock.assert_not_called()
        self.assertEqual(self._draft_count(), 1)

    def test_validate_crash_does_not_double_count_item(self):
        def fake_create_draft(**kwargs):
            return SimpleNamespace(id=999, version=1)

        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService, "create_draft", side_effect=fake_create_draft
        ), patch.object(
            MarketplaceDraftService,
            "validate_draft",
            side_effect=RuntimeError("unexpected validation crash"),
        ):
            response = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": [self.without_draft_id],
                "validate": True,
            })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["created"], 1)
        self.assertEqual(body["failed"], 0)
        self.assertEqual(
            body["created"] + body["existing"] + body["failed"], body["total"]
        )

    def test_inactive_account_is_rejected_fast(self):
        with self.app.app_context():
            account = db.session.get(
                SellerMarketplaceAccount, self.account1_id
            )
            account.is_active = False
            db.session.commit()
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService, "create_draft"
        ) as create_mock:
            response = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": [self.without_draft_id],
            })
        self.assertEqual(response.status_code, 400)
        create_mock.assert_not_called()

    def test_single_item_failure_does_not_block_others(self):
        calls = []

        def flaky_create_draft(**kwargs):
            calls.append(kwargs["imported_product_id"])
            from services.marketplace_drafts import MarketplaceDraftError

            raise MarketplaceDraftError("synthetic failure")

        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService, "create_draft", side_effect=flaky_create_draft
        ):
            response = self._post({
                "account_id": self.account1_id,
                "imported_product_ids": [
                    self.with_draft_id,
                    self.without_draft_id,
                ],
            })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["created"], 0)
        self.assertEqual(body["existing"], 1)
        self.assertEqual(body["failed"], 1)
        self.assertEqual(calls, [self.without_draft_id])


if __name__ == "__main__":
    unittest.main()
