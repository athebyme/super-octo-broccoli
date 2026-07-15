# -*- coding: utf-8 -*-
"""Warehouse and per-listing stock reads are complete and tenant-scoped."""

from datetime import datetime
import unittest

from flask import Flask

from models import (
    Marketplace,
    MarketplaceListing,
    MarketplaceWarehouse,
    MarketplaceWarehouseStock,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_warehouses import (
    MarketplaceWarehouseConflict,
    MarketplaceWarehouseNotFound,
    MarketplaceWarehouseService,
    MarketplaceWarehouseSyncError,
)


SYNTHETIC_CREDENTIALS = MarketplaceCredentials(
    external_account_id="synthetic-client",
    api_key="synthetic-key",
)


class SyntheticWarehouseAdapter:
    capabilities = {"warehouses_read", "stocks_read"}

    def __init__(self):
        self.warehouse_pages = {
            "": {
                "warehouses": [{
                    "warehouse_id": 7001,
                    "name": "Main FBS",
                    "status": "created",
                    "warehouse_type": "ORDINARY",
                    "phone": "must-not-persist",
                    "is_rfbs": False,
                    "has_postings_limit": True,
                    "postings_limit": 100,
                }],
                "cursor": "second",
                "has_next": True,
            },
            "second": {
                "warehouses": [{
                    "warehouse_id": 7002,
                    "name": "Reserve rFBS",
                    "status": "created",
                    "warehouse_type": "ORDINARY",
                    "is_rfbs": True,
                }],
                "cursor": "",
                "has_next": False,
            },
        }
        self.stock_pages = {
            "": {
                "products": [{
                    "sku": 9001,
                    "offer_id": "offer-1",
                    "product_id": 101,
                    "warehouse_id": 7001,
                    "warehouse_name": "Main FBS",
                    "present": 11,
                    "reserved": 3,
                    "free_stock": 8,
                }],
                "cursor": "stock-second",
                "has_next": True,
            },
            "stock-second": {
                "products": [{
                    "sku": 9001,
                    "offer_id": "offer-1",
                    "product_id": 101,
                    "warehouse_id": 7002,
                    "warehouse_name": "Reserve rFBS",
                    "present": 4,
                    "reserved": 1,
                    "free_stock": 3,
                }],
                "cursor": "",
                "has_next": False,
            },
        }
        self.warehouse_calls = []
        self.stock_calls = []

    def require_capability(self, capability):
        if capability not in self.capabilities:
            raise AssertionError(f"missing capability {capability}")

    def read_warehouses(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.warehouse_calls.append(payload)
        return self.warehouse_pages[payload["cursor"]]

    def read_stocks_by_warehouse_fbs(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.stock_calls.append(payload)
        return self.stock_pages[payload["cursor"]]


class MarketplaceWarehouseServiceTest(unittest.TestCase):
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
            username="warehouse-owner",
            email="warehouse-owner@test.local",
            is_active=True,
        )
        self.user.set_password("synthetic-password")
        self.seller = Seller(user=self.user, company_name="Warehouse Owner")
        self.foreign_user = User(
            username="warehouse-foreign",
            email="warehouse-foreign@test.local",
            is_active=True,
        )
        self.foreign_user.set_password("synthetic-password")
        self.foreign_seller = Seller(
            user=self.foreign_user,
            company_name="Foreign Warehouse Owner",
        )
        self.marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add_all([
            self.seller,
            self.foreign_seller,
            self.marketplace,
        ])
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
            sync_fingerprint="a" * 64,
        )
        db.session.add(self.listing)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def sync(self, adapter):
        return MarketplaceWarehouseService.sync_warehouses(
            seller_id=self.seller.id,
            account_id=self.account.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 12, 0, 0),
        )

    def test_complete_paginated_sync_persists_only_operational_fields(self):
        adapter = SyntheticWarehouseAdapter()
        run = self.sync(adapter)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.page_count, 2)
        self.assertEqual(run.seen_count, 2)
        rows = MarketplaceWarehouseService.list_warehouses(
            seller_id=self.seller.id,
            account_id=self.account.id,
        )
        self.assertEqual([row.external_warehouse_id for row in rows], ["7001", "7002"])
        public = rows[0].to_public_dict()
        self.assertNotIn("phone", public)
        self.assertNotIn("address", public)
        self.assertEqual(public["limits"]["postings_limit"], 100)
        self.assertEqual(adapter.warehouse_calls, [
            {"limit": 100, "cursor": ""},
            {"limit": 100, "cursor": "second"},
        ])

    def test_failed_full_snapshot_keeps_last_good_rows(self):
        adapter = SyntheticWarehouseAdapter()
        self.sync(adapter)
        fingerprints = {
            row.external_warehouse_id: row.sync_fingerprint
            for row in MarketplaceWarehouse.query.all()
        }
        adapter.warehouse_pages["second"] = {
            "warehouses": [adapter.warehouse_pages[""]["warehouses"][0]],
            "cursor": "",
            "has_next": False,
        }
        with self.assertRaises(MarketplaceWarehouseSyncError):
            self.sync(adapter)
        rows = MarketplaceWarehouse.query.all()
        self.assertEqual(
            {row.external_warehouse_id: row.sync_fingerprint for row in rows},
            fingerprints,
        )
        self.assertTrue(all(row.is_available for row in rows))

    def test_complete_snapshot_marks_missing_warehouse_unavailable(self):
        adapter = SyntheticWarehouseAdapter()
        self.sync(adapter)
        adapter.warehouse_pages[""] = {
            "warehouses": [adapter.warehouse_pages[""]["warehouses"][0]],
            "cursor": "",
            "has_next": False,
        }
        run = self.sync(adapter)
        self.assertEqual(run.unavailable_count, 1)
        missing = MarketplaceWarehouse.query.filter_by(
            external_warehouse_id="7002"
        ).one()
        self.assertFalse(missing.is_available)

    def test_stock_refresh_requires_known_owned_warehouse_and_exact_listing(self):
        adapter = SyntheticWarehouseAdapter()
        with self.assertRaises(MarketplaceWarehouseConflict):
            MarketplaceWarehouseService.refresh_listing_stocks(
                seller_id=self.seller.id,
                listing_id=self.listing.id,
                adapter=adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        self.assertEqual(MarketplaceWarehouseStock.query.count(), 0)

        self.sync(adapter)
        rows = MarketplaceWarehouseService.refresh_listing_stocks(
            seller_id=self.seller.id,
            listing_id=self.listing.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 13, 0, 0),
        )
        self.assertEqual([row.free_stock for row in rows], [8, 3])
        self.assertEqual(adapter.stock_calls[-2:], [
            {"limit": 100, "cursor": "", "offer_id": ["offer-1"]},
            {"limit": 100, "cursor": "stock-second", "offer_id": ["offer-1"]},
        ])

        adapter.stock_pages[""]["products"][0]["offer_id"] = "foreign-offer"
        with self.assertRaises(MarketplaceWarehouseSyncError):
            MarketplaceWarehouseService.refresh_listing_stocks(
                seller_id=self.seller.id,
                listing_id=self.listing.id,
                adapter=adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        self.assertEqual(
            MarketplaceWarehouseStock.query.filter_by(
                listing_id=self.listing.id,
                warehouse_id=rows[0].warehouse_id,
            ).one().free_stock,
            8,
        )

    def test_tenant_scope_hides_listing_and_account(self):
        adapter = SyntheticWarehouseAdapter()
        with self.assertRaises(MarketplaceWarehouseNotFound):
            MarketplaceWarehouseService.refresh_listing_stocks(
                seller_id=self.foreign_seller.id,
                listing_id=self.listing.id,
                adapter=adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        with self.assertRaises(MarketplaceWarehouseNotFound):
            MarketplaceWarehouseService.list_warehouses(
                seller_id=self.foreign_seller.id,
                account_id=self.account.id,
            )


if __name__ == "__main__":
    unittest.main()
