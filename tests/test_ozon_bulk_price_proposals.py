# -*- coding: utf-8 -*-
"""Exact-set bulk price preview and atomic local proposal save."""

from datetime import datetime
import os
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    Marketplace,
    MarketplaceCommercialProposal,
    MarketplaceListing,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_commercial import register_marketplace_commercial_routes
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_commercial import (
    MarketplaceCommercialBusy,
    MarketplaceCommercialConflict,
    MarketplaceCommercialNotFound,
    MarketplaceCommercialService,
    MarketplaceCommercialValidationError,
)


SYNTHETIC_CREDENTIALS = MarketplaceCredentials(
    external_account_id="bulk-synthetic-client",
    api_key="synthetic-only",
)


class BulkPriceAdapter:
    capabilities = {"prices_read", "prices_write"}

    def __init__(self):
        self.read_calls = []
        self.write_calls = []
        self.price_rows = {
            101: ("bulk-offer-1", "1000", "1500"),
            102: ("bulk-offer-2", "1200", "2000"),
        }

    def require_capability(self, capability):
        if capability not in self.capabilities:
            raise AssertionError("unexpected capability")

    def read_prices(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.read_calls.append(payload)
        requested = set(payload["filter"]["product_id"])
        items = []
        for product_id in requested:
            row = self.price_rows.get(product_id)
            if row is None:
                continue
            offer_id, amount, old_price = row
            items.append({
                "offer_id": offer_id,
                "product_id": product_id,
                "price": {
                    "price": amount,
                    "old_price": old_price,
                    "min_price": "0",
                    "currency_code": "RUB",
                },
            })
        return {"items": items, "total_items": len(items), "cursor": ""}

    def update_prices(self, *args, **kwargs):
        self.write_calls.append((args, kwargs))
        raise AssertionError("proposal preparation must not write to Ozon")


class OzonBulkPriceProposalTest(unittest.TestCase):
    def setUp(self):
        self._temporary_db = tempfile.TemporaryDirectory(
            prefix="seller-hub-bulk-price-test-"
        )
        self.database_path = os.path.join(self._temporary_db.name, "test.sqlite")
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="bulk-price-test-secret",
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{self.database_path}",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_commercial_routes(self.app)
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        self.user = User(
            username="bulk-price-owner",
            email="bulk-price-owner@test.local",
            is_active=True,
        )
        self.user.set_password("synthetic-password")
        self.seller = Seller(user=self.user, company_name="Bulk Price Owner")
        self.other_user = User(
            username="bulk-price-other",
            email="bulk-price-other@test.local",
            is_active=True,
        )
        self.other_user.set_password("synthetic-password")
        self.other_seller = Seller(
            user=self.other_user,
            company_name="Bulk Price Other",
        )
        self.marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add_all([self.seller, self.other_seller, self.marketplace])
        db.session.flush()
        self.account = self._account(self.seller.id, "owner-client")
        self.other_account = self._account(self.other_seller.id, "other-client")
        db.session.add_all([self.account, self.other_account])
        db.session.flush()
        self.listings = [
            self._listing(self.seller.id, self.account.id, 101, "bulk-offer-1"),
            self._listing(self.seller.id, self.account.id, 102, "bulk-offer-2"),
        ]
        self.other_listing = self._listing(
            self.other_seller.id,
            self.other_account.id,
            201,
            "other-offer",
        )
        db.session.add_all([*self.listings, self.other_listing])
        db.session.commit()
        self.adapter = BulkPriceAdapter()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.context.pop()
        self._temporary_db.cleanup()

    def _account(self, seller_id, external_id):
        return SellerMarketplaceAccount(
            seller_id=seller_id,
            marketplace_id=self.marketplace.id,
            external_account_id=external_id,
            label="Synthetic Ozon",
            is_active=True,
            connection_status="connected",
        )

    def _listing(self, seller_id, account_id, product_id, offer_id):
        return MarketplaceListing(
            seller_id=seller_id,
            marketplace_id=self.marketplace.id,
            account_id=account_id,
            offer_id=offer_id,
            external_product_id=str(product_id),
            title=f"Synthetic item {product_id}",
            normalized_status="active",
            is_available=True,
            is_archived=False,
            sync_fingerprint=("a" if seller_id == self.seller.id else "b") * 64,
            price_summary_json="{}",
            stock_summary_json="{}",
        )

    def _preview(self, **overrides):
        values = {
            "seller_id": self.seller.id,
            "account_id": self.account.id,
            "listing_ids": [row.id for row in self.listings],
            "mode": "set",
            "value": "1300",
            "created_by_user_id": self.user.id,
            "adapter": self.adapter,
            "credentials": SYNTHETIC_CREDENTIALS,
            "now": datetime(2026, 11, 20, 12, 0, 0),
        }
        values.update(overrides)
        return MarketplaceCommercialService.preview_bulk_price_proposals(**values)

    def _auth(self):
        fake = SimpleNamespace(
            id=self.user.id,
            seller=SimpleNamespace(id=self.seller.id),
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )
        return (
            patch("routes.marketplace_commercial.current_user", fake),
            patch("flask_login.utils._get_user", return_value=fake),
        )

    def test_preview_reads_exact_set_once_and_confirm_is_atomic_local_only(self):
        preview = self._preview()
        self.assertEqual(preview["selected_count"], 2)
        self.assertEqual(preview["eligible_count"], 2)
        self.assertEqual([item["status"] for item in preview["items"]], ["eligible", "eligible"])
        self.assertEqual(len(self.adapter.read_calls), 1)
        self.assertEqual(self.adapter.write_calls, [])
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 0)
        self.assertEqual(preview["items"][0]["proposed_price"], "1300")

        result = MarketplaceCommercialService.confirm_bulk_price_proposals(
            seller_id=self.seller.id,
            created_by_user_id=self.user.id,
            preview_token=preview["preview_token"],
        )
        self.assertEqual(result["created_count"], 2)
        self.assertEqual(result["saved_count"], 2)
        self.assertFalse(result["replayed"])
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 2)
        self.assertEqual(len(self.adapter.read_calls), 1)
        self.assertEqual(self.adapter.write_calls, [])
        self.assertTrue(all(
            item["proposal"].status == "pending_review"
            for item in result["items"]
        ))

    def test_100_listing_bulk_preview_confirm_and_replay_stay_exact_and_bounded(self):
        listings = [
            self._listing(
                self.seller.id,
                self.account.id,
                product_id,
                f"mass-offer-{product_id}",
            )
            for product_id in range(1001, 1101)
        ]
        db.session.add_all(listings)
        db.session.commit()
        listing_ids = [row.id for row in listings]
        product_ids = [int(row.external_product_id) for row in listings]
        self.adapter.price_rows.update({
            product_id: (row.offer_id, "1000", "1500")
            for row, product_id in zip(listings, product_ids)
        })

        preview = self._preview(listing_ids=listing_ids)
        self.assertEqual(preview["selected_count"], 100)
        self.assertEqual(preview["eligible_count"], 100)
        self.assertEqual(preview["noop_count"], 0)
        self.assertEqual(preview["blocked_count"], 0)
        self.assertEqual(
            [item["listing_id"] for item in preview["items"]],
            listing_ids,
        )
        self.assertEqual(len(self.adapter.read_calls), 1)
        self.assertEqual(
            set(self.adapter.read_calls[0]["filter"]["product_id"]),
            set(product_ids),
        )
        self.assertEqual(self.adapter.read_calls[0]["limit"], 100)
        self.assertEqual(self.adapter.write_calls, [])
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 0)

        saved = MarketplaceCommercialService.confirm_bulk_price_proposals(
            seller_id=self.seller.id,
            created_by_user_id=self.user.id,
            preview_token=preview["preview_token"],
        )
        first_ids = {item["proposal"].id for item in saved["items"]}
        self.assertEqual(saved["created_count"], 100)
        self.assertEqual(saved["saved_count"], 100)
        self.assertFalse(saved["replayed"])
        self.assertEqual(len(first_ids), 100)
        self.assertEqual(
            {item["listing_id"] for item in saved["items"]},
            set(listing_ids),
        )
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 100)
        self.assertEqual(len(self.adapter.read_calls), 1)
        self.assertEqual(self.adapter.write_calls, [])

        replay = MarketplaceCommercialService.confirm_bulk_price_proposals(
            seller_id=self.seller.id,
            created_by_user_id=self.user.id,
            preview_token=preview["preview_token"],
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["created_count"], 0)
        self.assertEqual(replay["saved_count"], 100)
        self.assertEqual(
            {item["proposal"].id for item in replay["items"]},
            first_ids,
        )
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 100)
        self.assertEqual(len(self.adapter.read_calls), 1)
        self.assertEqual(self.adapter.write_calls, [])

        with self.assertRaises(MarketplaceCommercialValidationError):
            self._preview(listing_ids=listing_ids + [max(listing_ids) + 1])
        self.assertEqual(len(self.adapter.read_calls), 1)
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 100)

    def test_percent_is_signed_delta_with_two_rounding_stages_and_existing_guards(self):
        preview = self._preview(mode="percent", value="2.25")
        self.assertEqual(
            [item["price_review"]["requested_price"] for item in preview["items"]],
            ["1022.5", "1227"],
        )
        self.assertEqual(
            [item["proposed_price"] for item in preview["items"]],
            ["1023", "1227"],
        )
        self.assertEqual(preview["eligible_count"], 2)

        decrease = self._preview(mode="percent", value="-10")
        self.assertEqual(decrease["eligible_count"], 0)
        self.assertEqual(decrease["blocked_count"], 2)
        self.assertTrue(all(
            item["reason_code"] == "price_decrease_requires_review"
            for item in decrease["items"]
        ))
        reviewed_decrease = self._preview(
            mode="percent",
            value="-10",
            allow_price_decrease=True,
            guardrail_note="Reviewed seller price decrease",
        )
        self.assertEqual(reviewed_decrease["eligible_count"], 2)
        self.assertTrue(all(
            item["guardrails"]["allow_price_decrease"]
            for item in reviewed_decrease["items"]
        ))
        missing_note = self._preview(
            mode="percent",
            value="-10",
            allow_price_decrease=True,
            guardrail_note=None,
        )
        self.assertEqual(missing_note["blocked_count"], 2)
        self.assertTrue(all(
            item["reason_code"] == "override_note_required"
            for item in missing_note["items"]
        ))
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 0)

    def test_confirm_replay_precedes_current_listing_and_proposal_state_checks(self):
        preview = self._preview()
        first = MarketplaceCommercialService.confirm_bulk_price_proposals(
            seller_id=self.seller.id,
            created_by_user_id=self.user.id,
            preview_token=preview["preview_token"],
        )
        first["items"][0]["proposal"].status = "approved"
        self.listings[1].is_archived = True
        db.session.commit()

        replay = MarketplaceCommercialService.confirm_bulk_price_proposals(
            seller_id=self.seller.id,
            created_by_user_id=self.user.id,
            preview_token=preview["preview_token"],
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["created_count"], 0)
        self.assertEqual(replay["saved_count"], 2)
        self.assertEqual(replay["items"][0]["status"], "approved")
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 2)
        self.assertEqual(self.adapter.write_calls, [])

    def test_existing_active_row_is_blocked_while_exact_eligible_subset_saves(self):
        existing = MarketplaceCommercialService.create_price_proposal(
            seller_id=self.seller.id,
            listing_id=self.listings[0].id,
            price="1100",
            source="user",
            idempotency_key="existing-price-0001",
            created_by_user_id=self.user.id,
            adapter=self.adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(existing.status, "pending_review")
        self.adapter.read_calls = []

        preview = self._preview()
        self.assertEqual(preview["eligible_count"], 1)
        self.assertEqual(preview["blocked_count"], 1)
        self.assertEqual(preview["items"][0]["reason_code"], "active_price_proposal")
        saved = MarketplaceCommercialService.confirm_bulk_price_proposals(
            seller_id=self.seller.id,
            created_by_user_id=self.user.id,
            preview_token=preview["preview_token"],
        )
        self.assertEqual(saved["created_count"], 1)
        self.assertEqual(saved["saved_count"], 1)
        self.assertEqual([item["listing_id"] for item in saved["items"]], [self.listings[1].id])
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 2)
        self.assertEqual(len(self.adapter.read_calls), 1)
        self.assertEqual(self.adapter.write_calls, [])

    def test_stale_state_and_database_commit_failure_leave_no_partial_proposals(self):
        stale = self._preview()
        self.listings[0].sync_fingerprint = "c" * 64
        db.session.commit()
        with self.assertRaises(MarketplaceCommercialConflict):
            MarketplaceCommercialService.confirm_bulk_price_proposals(
                seller_id=self.seller.id,
                created_by_user_id=self.user.id,
                preview_token=stale["preview_token"],
            )
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 0)

        fresh = self._preview()
        with patch.object(db.session, "commit", side_effect=RuntimeError("synthetic commit failure")):
            with self.assertRaises(RuntimeError):
                MarketplaceCommercialService.confirm_bulk_price_proposals(
                    seller_id=self.seller.id,
                    created_by_user_id=self.user.id,
                    preview_token=fresh["preview_token"],
                )
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 0)

    def test_confirm_holds_sqlite_writer_lock_through_final_revalidation(self):
        preview = self._preview()
        original_scope = MarketplaceCommercialService._bulk_price_scope
        blocked_writer = []

        def scope_with_competing_writer(**kwargs):
            result = original_scope(**kwargs)
            observer = sqlite3.connect(
                self.database_path,
                timeout=0,
                isolation_level=None,
            )
            try:
                try:
                    observer.execute(
                        "UPDATE marketplace_listings "
                        "SET sync_fingerprint = ? WHERE id = ?",
                        ("f" * 64, self.listings[0].id),
                    )
                except sqlite3.OperationalError as exc:
                    blocked_writer.append("locked" in str(exc).lower())
                else:
                    blocked_writer.append(False)
            finally:
                observer.close()
            return result

        with patch.object(
            MarketplaceCommercialService,
            "_bulk_price_scope",
            side_effect=scope_with_competing_writer,
        ):
            result = MarketplaceCommercialService.confirm_bulk_price_proposals(
                seller_id=self.seller.id,
                created_by_user_id=self.user.id,
                preview_token=preview["preview_token"],
            )

        self.assertEqual(blocked_writer, [True])
        self.assertEqual(result["created_count"], 2)
        self.assertEqual(self.listings[0].sync_fingerprint, "a" * 64)
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 2)

    def test_confirm_fails_closed_when_sqlite_writer_lock_is_busy(self):
        preview = self._preview()
        blocker = sqlite3.connect(
            self.database_path,
            timeout=0,
            isolation_level=None,
        )
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(MarketplaceCommercialBusy):
                MarketplaceCommercialService.confirm_bulk_price_proposals(
                    seller_id=self.seller.id,
                    created_by_user_id=self.user.id,
                    preview_token=preview["preview_token"],
                )
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 0)

    def test_confirm_rechecks_preview_expiry_after_lock_acquisition(self):
        preview = self._preview()
        original_claims = MarketplaceCommercialService._bulk_claims
        calls = []

        def claims_expiring_after_lock(token, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise MarketplaceCommercialValidationError(
                    "Сравнение цены истекло"
                )
            return original_claims(token, **kwargs)

        with patch.object(
            MarketplaceCommercialService,
            "_bulk_claims",
            side_effect=claims_expiring_after_lock,
        ):
            with self.assertRaises(MarketplaceCommercialValidationError):
                MarketplaceCommercialService.confirm_bulk_price_proposals(
                    seller_id=self.seller.id,
                    created_by_user_id=self.user.id,
                    preview_token=preview["preview_token"],
                )
        self.assertEqual(len(calls), 2)
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 0)

    def test_invalid_mixed_duplicate_and_archived_sets_fail_before_provider_read(self):
        with self.assertRaises(MarketplaceCommercialValidationError):
            self._preview(listing_ids=[self.listings[0].id, self.listings[0].id])
        self.assertEqual(self.adapter.read_calls, [])
        with self.assertRaises(MarketplaceCommercialNotFound):
            self._preview(listing_ids=[self.listings[0].id, self.other_listing.id])
        self.assertEqual(self.adapter.read_calls, [])
        self.listings[0].is_archived = True
        db.session.commit()
        with self.assertRaises(MarketplaceCommercialConflict):
            self._preview(listing_ids=[self.listings[0].id])
        self.assertEqual(self.adapter.read_calls, [])

    def test_tampered_token_wrong_user_and_review_handoff_are_exact(self):
        preview = self._preview(listing_ids=[self.listings[0].id])
        token = preview["preview_token"]
        unsigned, signature = token.rsplit(".", 1)
        tampered = unsigned + "." + ("a" if signature[0] != "a" else "b") + signature[1:]
        with self.assertRaises(MarketplaceCommercialValidationError):
            MarketplaceCommercialService.confirm_bulk_price_proposals(
                seller_id=self.seller.id,
                created_by_user_id=self.user.id,
                preview_token=tampered,
            )
        with self.assertRaises(MarketplaceCommercialNotFound):
            MarketplaceCommercialService.confirm_bulk_price_proposals(
                seller_id=self.seller.id,
                created_by_user_id=self.other_user.id,
                preview_token=token,
            )
        result = MarketplaceCommercialService.confirm_bulk_price_proposals(
            seller_id=self.seller.id,
            created_by_user_id=self.user.id,
            preview_token=token,
        )
        proposal_id = result["items"][0]["proposal"].id
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            saved = self.client.post(
                "/marketplaces/commercial/price-proposals/bulk-confirm",
                json={"preview_token": token, "confirm_save": True},
            )
            response = self.client.get(
                "/marketplaces/commercial/?"
                f"account_id={self.account.id}&proposal_kind=price&"
                f"status=pending_review&proposal_ids={proposal_id}",
                headers={"Accept": "application/json"},
            )
            foreign_scope = self.client.get(
                "/marketplaces/commercial/?"
                f"account_id={self.account.id}&proposal_kind=price&"
                f"status=pending_review&proposal_ids={proposal_id},{999999}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.get_json()["replayed"])
        self.assertEqual(saved.get_json()["saved_count"], 1)
        self.assertIn(f"proposal_ids={proposal_id}", saved.get_json()["review_url"])
        self.assertNotIn("idempotency_key", saved.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["exact_selection"])
        self.assertEqual([item["id"] for item in response.get_json()["items"]], [proposal_id])
        self.assertEqual(foreign_scope.status_code, 404)
        self.assertNotIn("999999", foreign_scope.get_data(as_text=True))

    def test_bulk_listing_bootstrap_is_local_compact_and_exact(self):
        user_patch, login_patch = self._auth()
        query = ",".join(str(item.id) for item in self.listings)
        with user_patch, login_patch:
            response = self.client.get(
                "/marketplaces/commercial/?"
                f"account_id={self.account.id}&bulk_listing_ids={query}",
                headers={"Accept": "application/json"},
            )
            mixed = self.client.get(
                "/marketplaces/commercial/?"
                f"account_id={self.account.id}&bulk_listing_ids={self.listings[0].id},{self.other_listing.id}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(set(data), {"account_id", "items"})
        self.assertEqual(data["account_id"], self.account.id)
        self.assertEqual([item["id"] for item in data["items"]], [row.id for row in self.listings])
        self.assertEqual(
            set(data["items"][0]),
            {"id", "offer_id", "title", "account_id", "is_available", "is_archived"},
        )
        self.assertEqual(mixed.status_code, 404)
        self.listings[0].is_archived = True
        db.session.commit()
        with user_patch, login_patch:
            archived = self.client.get(
                "/marketplaces/commercial/?"
                f"account_id={self.account.id}&bulk_listing_ids={self.listings[0].id}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(archived.status_code, 409)
        self.listings[0].is_archived = False
        self.listings[0].is_available = False
        db.session.commit()
        with user_patch, login_patch:
            unavailable = self.client.get(
                "/marketplaces/commercial/?"
                f"account_id={self.account.id}&bulk_listing_ids={self.listings[0].id}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(unavailable.status_code, 409)
        self.listings[0].is_available = True
        self.account.is_active = False
        db.session.commit()
        with user_patch, login_patch:
            inactive_account = self.client.get(
                "/marketplaces/commercial/?"
                f"account_id={self.account.id}&bulk_listing_ids={self.listings[0].id}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(inactive_account.status_code, 409)
        self.assertEqual(self.adapter.read_calls, [])

    def test_expired_preview_token_is_rejected_without_creating_proposals(self):
        preview = self._preview()
        with patch.object(MarketplaceCommercialService, "BULK_PRICE_PREVIEW_MAX_AGE", -1):
            with self.assertRaises(MarketplaceCommercialConflict):
                MarketplaceCommercialService.confirm_bulk_price_proposals(
                    seller_id=self.seller.id,
                    created_by_user_id=self.user.id,
                    preview_token=preview["preview_token"],
                )
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 0)

    def test_confirm_route_saves_exact_document_without_exposing_internal_key(self):
        preview = self._preview(listing_ids=[self.listings[0].id])
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.post(
                "/marketplaces/commercial/price-proposals/bulk-confirm",
                json={
                    "preview_token": preview["preview_token"],
                    "confirm_save": True,
                },
            )
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertEqual(data["selected_count"], 1)
        self.assertEqual(data["created_count"], 1)
        self.assertEqual(data["saved_count"], 1)
        self.assertFalse(data["replayed"])
        self.assertEqual(len(data["items"]), 1)
        proposal = data["items"][0]["proposal"]
        self.assertEqual(proposal["status"], "pending_review")
        self.assertEqual(proposal["listing_id"], self.listings[0].id)
        self.assertIn("proposal_ids=", data["review_url"])
        encoded = response.get_data(as_text=True)
        self.assertNotIn("idempotency_key", encoded)
        self.assertNotIn("api_key", encoded)
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 1)


if __name__ == "__main__":
    unittest.main()
