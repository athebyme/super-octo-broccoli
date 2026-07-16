# -*- coding: utf-8 -*-
"""Exact-set commercial batch approval keeps per-listing durable evidence."""

from datetime import datetime
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
    MarketplaceCommercialService,
    MarketplaceCommercialValidationError,
)
from services.ozon_api_client import OzonAmbiguousWriteError


SYNTHETIC_CREDENTIALS = MarketplaceCredentials(
    external_account_id="synthetic-batch-client",
    api_key="synthetic-batch-key",
)


class SyntheticBatchAdapter:
    capabilities = {
        "prices_read",
        "prices_write",
        "stocks_read",
        "stocks_write",
    }

    def __init__(self):
        self.products = {
            "101": {"offer_id": "batch-offer-1", "price": "1000", "old": "1600"},
            "202": {"offer_id": "batch-offer-2", "price": "2000", "old": "3200"},
        }
        self.stocks = {
            ("batch-offer-1", "7001"): 8,
            ("batch-offer-2", "7002"): 5,
        }
        self.price_writes = []
        self.stock_writes = []
        self.price_reads = []
        self.stock_reads = []
        self.rejected_offers = set()
        self.ambiguous_apply = False
        self.malformed_after_apply = False

    def require_capability(self, capability):
        if capability not in self.capabilities:
            raise AssertionError(f"missing capability {capability}")

    def read_prices(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.price_reads.append(payload)
        product_ids = payload["filter"]["product_id"]
        items = []
        for product_id in product_ids:
            state = self.products[str(product_id)]
            items.append({
                "offer_id": state["offer_id"],
                "product_id": int(product_id),
                "price": {
                    "price": state["price"],
                    "old_price": state["old"],
                    "min_price": "1",
                    "net_price": state["price"],
                    "currency_code": "RUB",
                    "auto_action_enabled": False,
                    "auto_add_to_ozon_actions_list_enabled": False,
                },
            })
        return {"items": items, "total": len(items), "cursor": ""}

    def update_prices(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.price_writes.append(payload)
        results = []
        for item in payload["prices"]:
            if item["offer_id"] in self.rejected_offers:
                results.append({
                    "offer_id": item["offer_id"],
                    "product_id": item["product_id"],
                    "updated": False,
                    "errors": [{"code": "rejected", "message": "synthetic"}],
                })
                continue
            self.products[str(item["product_id"])]["price"] = item["price"]
            results.append({
                "offer_id": item["offer_id"],
                "product_id": item["product_id"],
                "updated": True,
                "errors": [],
            })
        if self.ambiguous_apply:
            raise OzonAmbiguousWriteError(
                "synthetic ambiguous batch",
                code="synthetic_batch_ambiguous",
                request_id="synthetic-batch-request",
            )
        if self.malformed_after_apply:
            return {"result": results[:-1]}
        return {"result": results}

    def read_stocks_by_warehouse_fbs(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.stock_reads.append(payload)
        offers = set(payload["offer_id"])
        products = []
        for (offer_id, warehouse_id), stock in self.stocks.items():
            if offer_id not in offers:
                continue
            product_id = 101 if offer_id.endswith("1") else 202
            products.append({
                "sku": product_id + 9000,
                "offer_id": offer_id,
                "product_id": product_id,
                "warehouse_id": int(warehouse_id),
                "warehouse_name": f"Warehouse {warehouse_id}",
                "present": stock + 1,
                "reserved": 1,
                "free_stock": stock,
            })
        return {"products": products, "cursor": "", "has_next": False}

    def update_stocks(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.stock_writes.append(payload)
        results = []
        for item in payload["stocks"]:
            identity = (item["offer_id"], str(item["warehouse_id"]))
            self.stocks[identity] = item["stock"]
            results.append({
                "offer_id": item["offer_id"],
                "product_id": item["product_id"],
                "warehouse_id": item["warehouse_id"],
                "updated": True,
                "errors": [],
            })
        return {"result": results}


class MarketplaceCommercialBatchTest(unittest.TestCase):
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
            username="commercial-batch-owner",
            email="commercial-batch-owner@test.local",
            is_active=True,
        )
        self.user.set_password("synthetic-password")
        self.seller = Seller(user=self.user, company_name="Commercial Batch")
        self.marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add_all([self.seller, self.marketplace])
        db.session.flush()
        self.account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="synthetic-batch-client",
            label="Synthetic Batch Ozon",
            is_active=True,
            connection_status="connected",
        )
        db.session.add(self.account)
        db.session.flush()
        self.listings = []
        self.warehouses = []
        for index, (offer_id, product_id, warehouse_id) in enumerate((
            ("batch-offer-1", "101", "7001"),
            ("batch-offer-2", "202", "7002"),
        ), start=1):
            listing = MarketplaceListing(
                seller_id=self.seller.id,
                marketplace_id=self.marketplace.id,
                account_id=self.account.id,
                offer_id=offer_id,
                external_product_id=product_id,
                primary_sku=str(9000 + int(product_id)),
                normalized_status="active",
                is_available=True,
                price_summary_json="{}",
                stock_summary_json="{}",
                sync_fingerprint=str(index) * 64,
            )
            warehouse = MarketplaceWarehouse(
                seller_id=self.seller.id,
                marketplace_id=self.marketplace.id,
                account_id=self.account.id,
                external_warehouse_id=warehouse_id,
                name=f"Warehouse {warehouse_id}",
                flags_json="{}",
                limits_json="{}",
                is_available=True,
                sync_fingerprint=str(index + 2) * 64,
                last_seen_at=datetime.utcnow(),
                last_synced_at=datetime.utcnow(),
            )
            db.session.add_all([listing, warehouse])
            self.listings.append(listing)
            self.warehouses.append(warehouse)
        db.session.commit()
        self.adapter = SyntheticBatchAdapter()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def price_proposals(self):
        proposals = []
        for index, (listing, price) in enumerate(
            zip(self.listings, ("1100", "2100")),
            start=1,
        ):
            proposals.append(MarketplaceCommercialService.create_price_proposal(
                seller_id=self.seller.id,
                listing_id=listing.id,
                price=price,
                source="user",
                idempotency_key=f"batch-price-proposal-{index:02d}",
                created_by_user_id=self.user.id,
                adapter=self.adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            ))
        return proposals

    def stock_proposals(self):
        proposals = []
        for index, (listing, warehouse, stock) in enumerate(
            zip(self.listings, self.warehouses, (0, 7)),
            start=1,
        ):
            proposals.append(MarketplaceCommercialService.create_stock_proposal(
                seller_id=self.seller.id,
                listing_id=listing.id,
                warehouse_id=warehouse.id,
                stock=stock,
                source="user",
                idempotency_key=f"batch-stock-proposal-{index:02d}",
                created_by_user_id=self.user.id,
                adapter=self.adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            ))
        return proposals

    def approve(self, proposals):
        return MarketplaceCommercialService.approve_proposals(
            seller_id=self.seller.id,
            items=[{
                "proposal_id": proposal.id,
                "expected_version": proposal.version,
            } for proposal in proposals],
            reviewed_by_user_id=self.user.id,
            adapter=self.adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 14, 0, 0),
        )

    def test_two_prices_use_one_write_and_two_confirmed_snapshots(self):
        proposals = self.price_proposals()
        results = self.approve(proposals)
        self.assertEqual([item.status for item in results], ["applied", "applied"])
        self.assertEqual(len(self.adapter.price_writes), 1)
        self.assertEqual(len(self.adapter.price_writes[0]["prices"]), 2)
        self.assertEqual(
            [len(item["filter"]["product_id"]) for item in self.adapter.price_reads],
            [1, 1, 2, 2],
        )
        operations = MarketplaceOperation.query.order_by(MarketplaceOperation.id).all()
        self.assertEqual(len(operations), 2)
        for operation in operations:
            self.assertEqual(operation.status, "succeeded")
            self.assertEqual(operation.attempt_count, 1)
            self.assertIsNotNone(operation.snapshot.confirmed_fingerprint)
            self.assertEqual(operation.snapshot.rollback_status, "available")

    def test_partial_result_is_accounted_per_item_without_second_write(self):
        proposals = self.price_proposals()
        self.adapter.rejected_offers.add("batch-offer-2")
        results = self.approve(proposals)
        self.assertEqual([item.status for item in results], ["applied", "failed"])
        self.assertEqual(len(self.adapter.price_writes), 1)
        operations = MarketplaceOperation.query.order_by(MarketplaceOperation.id).all()
        self.assertEqual([item.status for item in operations], ["succeeded", "failed"])
        self.assertEqual(operations[1].snapshot.rollback_status, "unavailable")

    def test_preflight_drift_blocks_entire_write_and_marks_only_drifted_item(self):
        proposals = self.price_proposals()
        self.adapter.products["101"]["price"] = "1050"
        with self.assertRaises(MarketplaceCommercialConflict):
            self.approve(proposals)
        self.assertEqual(self.adapter.price_writes, [])
        db.session.expire_all()
        states = [
            db.session.get(MarketplaceCommercialProposal, proposal.id).status
            for proposal in proposals
        ]
        self.assertEqual(states, ["conflict", "pending_review"])
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_ambiguous_batch_reconciles_without_retry(self):
        proposals = self.price_proposals()
        self.adapter.ambiguous_apply = True
        results = self.approve(proposals)
        self.assertEqual([item.status for item in results], ["applied", "applied"])
        self.assertEqual(len(self.adapter.price_writes), 1)

    def test_malformed_batch_reconciles_without_retry(self):
        proposals = self.price_proposals()
        self.adapter.malformed_after_apply = True
        results = self.approve(proposals)
        self.assertEqual([item.status for item in results], ["applied", "applied"])
        self.assertEqual(len(self.adapter.price_writes), 1)

    def test_stock_batch_names_each_exact_owned_warehouse(self):
        proposals = self.stock_proposals()
        results = self.approve(proposals)
        self.assertEqual([item.status for item in results], ["applied", "applied"])
        self.assertEqual(len(self.adapter.stock_writes), 1)
        self.assertEqual(
            [len(item["offer_id"]) for item in self.adapter.stock_reads],
            [1, 1, 2, 2],
        )
        self.assertEqual(
            {item["warehouse_id"] for item in self.adapter.stock_writes[0]["stocks"]},
            {7001, 7002},
        )
        self.assertEqual(self.adapter.stocks[("batch-offer-1", "7001")], 0)
        self.assertEqual(self.adapter.stocks[("batch-offer-2", "7002")], 7)

    def test_batch_input_rejects_duplicates_and_mixed_kinds_before_write(self):
        prices = self.price_proposals()
        duplicate = [{
            "proposal_id": prices[0].id,
            "expected_version": prices[0].version,
        }] * 2
        with self.assertRaises(MarketplaceCommercialValidationError):
            MarketplaceCommercialService.approve_proposals(
                seller_id=self.seller.id,
                items=duplicate,
                reviewed_by_user_id=self.user.id,
                adapter=self.adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        stock = MarketplaceCommercialService.create_stock_proposal(
            seller_id=self.seller.id,
            listing_id=self.listings[0].id,
            warehouse_id=self.warehouses[0].id,
            stock=0,
            source="user",
            idempotency_key="batch-mixed-stock-01",
            created_by_user_id=self.user.id,
            adapter=self.adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        with self.assertRaises(MarketplaceCommercialValidationError):
            self.approve([prices[0], stock])
        self.assertEqual(self.adapter.price_writes, [])
        self.assertEqual(self.adapter.stock_writes, [])


if __name__ == "__main__":
    unittest.main()
