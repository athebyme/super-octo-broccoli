# -*- coding: utf-8 -*-
"""Reviewed Ozon commercial writes are durable, exact and non-repeatable."""

from datetime import datetime, timedelta
import json
import unittest

from flask import Flask

from models import (
    Marketplace,
    MarketplaceCommercialProposal,
    MarketplaceListing,
    MarketplaceOperation,
    MarketplaceWarehouse,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_commercial import (
    MarketplaceCommercialConflict,
    MarketplaceCommercialNotFound,
    MarketplaceCommercialService,
    MarketplaceCommercialUpstreamError,
    MarketplaceCommercialValidationError,
)
from services.ozon_api_client import OzonAPIError, OzonAmbiguousWriteError


SYNTHETIC_CREDENTIALS = MarketplaceCredentials(
    external_account_id="synthetic-client",
    api_key="synthetic-key",
)


class SyntheticCommercialAdapter:
    capabilities = {
        "prices_read",
        "prices_write",
        "stocks_read",
        "stocks_write",
    }

    def __init__(self):
        self.price = "1000"
        self.old_price = "1500"
        self.min_price = "800"
        self.stock = 8
        self.price_writes = []
        self.stock_writes = []
        self.ambiguous_mode = None
        self.malformed_write_response = False
        self.read_failure = None
        self.post_write_read_failure = None

    def require_capability(self, capability):
        if capability not in self.capabilities:
            raise AssertionError(f"missing capability {capability}")

    def read_prices(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        failure = (
            self.post_write_read_failure
            if self.price_writes
            else self.read_failure
        )
        if failure == "api":
            raise OzonAPIError("synthetic provider failure")
        if failure == "programmer":
            raise AssertionError("synthetic programmer failure")
        return {
            "items": [{
                "offer_id": "offer-1",
                "product_id": 101,
                "price": {
                    "price": self.price,
                    "old_price": self.old_price,
                    "min_price": self.min_price,
                    "net_price": "600",
                    "currency_code": "RUB",
                    "auto_action_enabled": False,
                    "auto_add_to_ozon_actions_list_enabled": False,
                },
            }],
            "total": 1,
            "cursor": "",
        }

    def read_stocks_by_warehouse_fbs(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        return {
            "products": [{
                "sku": 9001,
                "offer_id": "offer-1",
                "product_id": 101,
                "warehouse_id": 7001,
                "warehouse_name": "Main FBS",
                "present": self.stock + 2,
                "reserved": 2,
                "free_stock": self.stock,
            }],
            "cursor": "",
            "has_next": False,
        }

    def update_prices(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.price_writes.append(payload)
        item = payload["prices"][0]
        if self.ambiguous_mode == "apply":
            self.price = item["price"]
            raise OzonAmbiguousWriteError(
                "synthetic ambiguous",
                code="synthetic_ambiguous",
                request_id="synthetic-request",
            )
        if self.ambiguous_mode == "no_apply":
            raise OzonAmbiguousWriteError(
                "synthetic ambiguous",
                code="synthetic_ambiguous",
            )
        self.price = item["price"]
        if self.malformed_write_response:
            return {"result": []}
        return {
            "result": [{
                "offer_id": item["offer_id"],
                "product_id": item["product_id"],
                "updated": True,
                "errors": [],
            }],
        }

    def update_stocks(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.stock_writes.append(payload)
        item = payload["stocks"][0]
        self.stock = item["stock"]
        return {
            "result": [{
                "offer_id": item["offer_id"],
                "product_id": item["product_id"],
                "warehouse_id": item["warehouse_id"],
                "updated": True,
                "errors": [],
            }],
        }


class MarketplaceCommercialServiceTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.user = User(
            username="commercial-owner",
            email="commercial-owner@test.local",
            is_active=True,
        )
        self.user.set_password("synthetic-password")
        self.seller = Seller(user=self.user, company_name="Commercial Owner")
        self.foreign_user = User(
            username="commercial-foreign",
            email="commercial-foreign@test.local",
            is_active=True,
        )
        self.foreign_user.set_password("synthetic-password")
        self.foreign_seller = Seller(
            user=self.foreign_user,
            company_name="Commercial Foreign",
        )
        self.marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add_all([self.seller, self.foreign_seller, self.marketplace])
        db.session.flush()
        self.account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="synthetic-client",
            label="Synthetic Ozon",
            is_active=True,
            connection_status="connected",
        )
        db.session.add(self.account)
        db.session.flush()
        self.listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            offer_id="offer-1",
            external_product_id="101",
            primary_sku="9001",
            normalized_status="active",
            is_available=True,
            price_summary_json=json.dumps({
                "available": True,
                "currency": "RUB",
                "values": {"price": "1000", "old_price": "1500"},
            }),
            sync_fingerprint="a" * 64,
        )
        db.session.add(self.listing)
        db.session.flush()
        self.warehouse = MarketplaceWarehouse(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            external_warehouse_id="7001",
            name="Main FBS",
            status="created",
            warehouse_type="ORDINARY",
            flags_json="{}",
            limits_json="{}",
            is_available=True,
            sync_fingerprint="b" * 64,
            last_seen_at=datetime.utcnow(),
            last_synced_at=datetime.utcnow(),
        )
        db.session.add(self.warehouse)
        db.session.commit()
        self.adapter = SyntheticCommercialAdapter()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def price_proposal(self, **overrides):
        values = {
            "seller_id": self.seller.id,
            "listing_id": self.listing.id,
            "price": "1100",
            "source": "user",
            "idempotency_key": "price-proposal-0001",
            "created_by_user_id": self.user.id,
            "adapter": self.adapter,
            "credentials": SYNTHETIC_CREDENTIALS,
            "now": datetime(2026, 7, 15, 12, 0, 0),
        }
        values.update(overrides)
        return MarketplaceCommercialService.create_price_proposal(**values)

    def approve(self, proposal, **overrides):
        values = {
            "seller_id": self.seller.id,
            "proposal_id": proposal.id,
            "expected_version": proposal.version,
            "reviewed_by_user_id": self.user.id,
            "adapter": self.adapter,
            "credentials": SYNTHETIC_CREDENTIALS,
            "now": datetime(2026, 7, 15, 12, 5, 0),
        }
        values.update(overrides)
        return MarketplaceCommercialService.approve_proposal(**values)

    def test_price_proposal_is_read_only_idempotent_and_requires_review(self):
        proposal = self.price_proposal()
        self.assertEqual(proposal.status, "pending_review")
        self.assertEqual(self.adapter.price_writes, [])
        self.assertEqual(
            json.loads(proposal.baseline_state_json)["price"],
            "1000",
        )
        duplicate = self.price_proposal()
        self.assertEqual(duplicate.id, proposal.id)
        self.assertEqual(MarketplaceCommercialProposal.query.count(), 1)

    def test_price_approval_commits_snapshot_before_exact_single_write(self):
        proposal = self.price_proposal()
        applied = self.approve(proposal)
        self.assertEqual(applied.status, "applied")
        self.assertEqual(len(self.adapter.price_writes), 1)
        self.assertEqual(self.adapter.price_writes[0], {
            "prices": [{
                "offer_id": "offer-1",
                "product_id": 101,
                "price": "1100",
                "currency_code": "RUB",
                "old_price": "1500",
            }],
        })
        operation = db.session.get(MarketplaceOperation, applied.operation_id)
        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(operation.attempt_count, 1)
        self.assertIsNotNone(operation.snapshot)
        self.assertEqual(operation.snapshot.snapshot_kind, "price")
        self.assertEqual(operation.snapshot.rollback_status, "available")
        self.assertEqual(
            json.loads(operation.snapshot.before_state_json)["price"],
            "1000",
        )
        self.assertEqual(
            json.loads(operation.snapshot.submitted_state_json)["price"],
            "1100",
        )

    def test_drift_before_approval_blocks_write(self):
        proposal = self.price_proposal()
        self.adapter.price = "1050"
        result = self.approve(proposal)
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.error_code, "commercial_baseline_drift")
        self.assertEqual(self.adapter.price_writes, [])
        self.assertIsNone(result.operation_id)

    def test_price_decrease_and_large_delta_need_explicit_guardrails(self):
        with self.assertRaises(MarketplaceCommercialValidationError):
            self.price_proposal(price="900", idempotency_key="decrease-0001")
        self.adapter.old_price = "3000"
        with self.assertRaises(MarketplaceCommercialValidationError):
            self.price_proposal(price="1600", idempotency_key="large-0001")
        self.adapter.old_price = "1500"
        proposal = self.price_proposal(
            price="900",
            idempotency_key="decrease-0002",
            allow_price_decrease=True,
            guardrail_note="Seller explicitly approved a controlled decrease",
        )
        self.assertEqual(proposal.status, "pending_review")

    def test_reject_is_terminal_and_never_writes(self):
        proposal = self.price_proposal()
        rejected = MarketplaceCommercialService.reject_proposal(
            seller_id=self.seller.id,
            proposal_id=proposal.id,
            expected_version=proposal.version,
            reviewed_by_user_id=self.user.id,
            note="Not needed",
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(self.adapter.price_writes, [])
        with self.assertRaises(MarketplaceCommercialConflict):
            self.approve(rejected)

    def test_stock_proposal_names_owned_warehouse_and_updates_free_stock(self):
        proposal = MarketplaceCommercialService.create_stock_proposal(
            seller_id=self.seller.id,
            listing_id=self.listing.id,
            warehouse_id=self.warehouse.id,
            stock=0,
            source="user",
            idempotency_key="stock-proposal-0001",
            created_by_user_id=self.user.id,
            adapter=self.adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(proposal.status, "pending_review")
        self.assertEqual(self.adapter.stock_writes, [])
        applied = self.approve(proposal)
        self.assertEqual(applied.status, "applied")
        self.assertEqual(self.adapter.stock_writes, [{
            "stocks": [{
                "offer_id": "offer-1",
                "product_id": 101,
                "warehouse_id": 7001,
                "stock": 0,
            }],
        }])

    def test_ambiguous_write_reconciles_without_retry(self):
        self.adapter.ambiguous_mode = "apply"
        proposal = self.price_proposal()
        result = self.approve(proposal)
        self.assertEqual(result.status, "applied")
        self.assertEqual(len(self.adapter.price_writes), 1)
        operation = db.session.get(MarketplaceOperation, result.operation_id)
        self.assertEqual(operation.attempt_count, 1)

        self.adapter = SyntheticCommercialAdapter()
        self.adapter.ambiguous_mode = "no_apply"
        second = self.price_proposal(
            idempotency_key="price-proposal-0002",
        )
        uncertain = self.approve(second)
        self.assertEqual(uncertain.status, "uncertain")
        operation = db.session.get(MarketplaceOperation, uncertain.operation_id)
        self.assertEqual(operation.status, "uncertain")
        self.assertEqual(len(self.adapter.price_writes), 1)
        MarketplaceCommercialService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=self.adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 12, 6, 0),
            allow_submission=False,
        )
        self.assertEqual(len(self.adapter.price_writes), 1)

    def test_rollback_is_a_second_reviewed_exact_restore_with_drift_gate(self):
        original_proposal = self.price_proposal()
        applied = self.approve(original_proposal)
        original_operation = db.session.get(
            MarketplaceOperation,
            applied.operation_id,
        )
        rollback = MarketplaceCommercialService.create_rollback_proposal(
            seller_id=self.seller.id,
            operation_id=original_operation.id,
            idempotency_key="price-rollback-0001",
            created_by_user_id=self.user.id,
            adapter=self.adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(rollback.status, "pending_review")
        self.assertEqual(rollback.source, "rollback")
        self.assertEqual(len(self.adapter.price_writes), 1)
        restored = self.approve(rollback)
        self.assertEqual(restored.status, "applied")
        self.assertEqual(self.adapter.price, "1000")
        self.assertEqual(len(self.adapter.price_writes), 2)
        db.session.refresh(original_operation)
        self.assertEqual(original_operation.snapshot.rollback_status, "succeeded")

        next_proposal = self.price_proposal(
            price="1100",
            idempotency_key="price-proposal-0003",
        )
        next_applied = self.approve(next_proposal)
        next_operation = db.session.get(
            MarketplaceOperation,
            next_applied.operation_id,
        )
        self.adapter.price = "1050"
        with self.assertRaises(MarketplaceCommercialConflict):
            MarketplaceCommercialService.create_rollback_proposal(
                seller_id=self.seller.id,
                operation_id=next_operation.id,
                idempotency_key="price-rollback-0002",
                created_by_user_id=self.user.id,
                adapter=self.adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        db.session.refresh(next_operation)
        self.assertEqual(next_operation.snapshot.rollback_status, "conflict")

    def test_malformed_success_can_be_proven_by_read_without_retry(self):
        self.adapter.malformed_write_response = True
        proposal = self.price_proposal()
        result = self.approve(proposal)
        self.assertEqual(result.status, "applied")
        self.assertEqual(len(self.adapter.price_writes), 1)

    def test_provider_read_error_is_typed_and_reconciliation_never_retries_write(self):
        self.adapter.read_failure = "api"
        with self.assertRaises(MarketplaceCommercialUpstreamError):
            self.price_proposal()
        self.assertEqual(self.adapter.price_writes, [])

        self.adapter.read_failure = None
        proposal = self.price_proposal(idempotency_key="price-read-failure-01")
        self.adapter.post_write_read_failure = "api"
        result = self.approve(proposal)
        self.assertEqual(result.status, "applying")
        operation = db.session.get(MarketplaceOperation, result.operation_id)
        self.assertEqual(operation.status, "polling")
        self.assertEqual(
            operation.error_code,
            "commercial_reconciliation_read_failed",
        )
        self.assertEqual(len(self.adapter.price_writes), 1)

        self.adapter.post_write_read_failure = None
        confirmed = MarketplaceCommercialService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=self.adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 12, 6, 0),
            allow_submission=False,
        )
        self.assertEqual(confirmed.status, "succeeded")
        self.assertEqual(len(self.adapter.price_writes), 1)

    def test_programmer_error_during_reconciliation_is_not_hidden(self):
        proposal = self.price_proposal(idempotency_key="price-programmer-error-01")
        self.adapter.post_write_read_failure = "programmer"
        with self.assertRaisesRegex(AssertionError, "programmer failure"):
            self.approve(proposal)
        self.assertEqual(len(self.adapter.price_writes), 1)
        operation = MarketplaceOperation.query.filter_by(
            seller_id=self.seller.id,
            operation_kind="price_update",
        ).one()
        self.assertEqual(operation.status, "submitted")
        self.assertEqual(operation.attempt_count, 1)

    def test_tenant_scope_hides_proposal_and_listing(self):
        proposal = self.price_proposal()
        with self.assertRaises(MarketplaceCommercialNotFound):
            MarketplaceCommercialService.get_proposal(
                seller_id=self.foreign_seller.id,
                proposal_id=proposal.id,
            )
        with self.assertRaises(MarketplaceCommercialNotFound):
            MarketplaceCommercialService.create_price_proposal(
                seller_id=self.foreign_seller.id,
                listing_id=self.listing.id,
                price="1100",
                source="user",
                created_by_user_id=self.foreign_user.id,
                adapter=self.adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )


if __name__ == "__main__":
    unittest.main()
