# -*- coding: utf-8 -*-
"""Strict Ozon listing normalization and complete-sweep reconciliation."""

from datetime import datetime
import unittest

from flask import Flask

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceCatalogSync,
    MarketplaceListing,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_listings import (
    MarketplaceCatalogProtocolError,
    MarketplaceListingNotFound,
    MarketplaceListingService,
)


SYNTHETIC_CREDENTIALS = MarketplaceCredentials(
    external_account_id="synthetic-client",
    api_key="synthetic-key",
)


class SyntheticCatalogAdapter:
    capabilities = {"catalog_read"}

    def __init__(self, *, malformed_archived=False):
        self.malformed_archived = malformed_archived
        self.list_payloads = []
        self.enrichment_ids = []

    def require_capability(self, capability):
        if capability not in self.capabilities:
            raise AssertionError("missing capability")

    def list_products(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.list_payloads.append(payload)
        visibility = payload["filter"]["visibility"]
        if visibility == "ALL":
            return {
                "result": {
                    "items": [{
                        "product_id": 101,
                        "offer_id": "offer-active",
                        "has_fbo_stocks": True,
                        "has_fbs_stocks": False,
                        "archived": False,
                    }],
                    "total": 1,
                    "last_id": "active-end",
                }
            }
        if self.malformed_archived:
            return {"result": {"items": [], "total": 1, "last_id": ""}}
        return {
            "result": {
                "items": [{
                    "product_id": "202",
                    "offer_id": "offer-archived",
                    "has_fbo_stocks": False,
                    "has_fbs_stocks": False,
                    "archived": True,
                }],
                "total": 1,
                "last_id": "archived-end",
            }
        }

    @staticmethod
    def _ids(payload):
        if "product_id" in payload:
            return [str(item) for item in payload["product_id"]]
        return [str(item) for item in payload["filter"]["product_id"]]

    def get_products(self, credentials, payload):
        ids = self._ids(payload)
        self.enrichment_ids.append(("info", ids))
        items = []
        for product_id in ids:
            archived = product_id == "202"
            items.append({
                "id": int(product_id),
                "offer_id": (
                    "offer-archived" if archived else "offer-active"
                ),
                "name": "Архивный товар" if archived else "Активный товар",
                "description_category_id": 10,
                "type_id": 777,
                "is_archived": archived,
                "barcodes": [f"barcode-{product_id}"],
                "images": [f"https://img.test/{product_id}.jpg"],
                "statuses": {
                    "is_created": True,
                    "status": "processed",
                    "status_failed": "",
                },
                "visibility_details": {
                    "active_product": not archived,
                    "has_price": not archived,
                    "has_stock": not archived,
                    "reasons": [],
                },
                "errors": [],
                "created_at": "2026-07-01T10:00:00Z",
                "updated_at": "2026-07-15T10:00:00Z",
                "sources": [{"sku": int(product_id) + 1000}],
            })
        return {"items": items}

    def get_product_attributes(self, credentials, payload):
        ids = self._ids(payload)
        self.enrichment_ids.append(("attributes", ids))
        return {
            "result": [{
                "id": int(product_id),
                "offer_id": (
                    "offer-archived" if product_id == "202" else "offer-active"
                ),
                "name": "Товар",
                "description_category_id": 10,
                "type_id": 777,
                "sku": int(product_id) + 1000,
                "attributes": [{
                    "id": 31,
                    "complex_id": 0,
                    "values": [{
                        "dictionary_value_id": 9001,
                        "value": "Бренд",
                    }],
                }],
                "complex_attributes": [],
                "depth": 10,
                "height": 20,
                "width": 30,
                "weight": 500,
                "dimension_unit": "mm",
                "weight_unit": "g",
                "barcodes": [f"barcode-{product_id}"],
                "images": [f"https://img.test/{product_id}.jpg"],
            } for product_id in ids],
            "total": len(ids),
            "last_id": "",
        }

    def read_prices(self, credentials, payload):
        ids = self._ids(payload)
        self.enrichment_ids.append(("prices", ids))
        return {
            "items": [{
                "product_id": int(product_id),
                "offer_id": (
                    "offer-archived" if product_id == "202" else "offer-active"
                ),
                "price": {
                    "auto_action_enabled": False,
                    "auto_add_to_ozon_actions_list_enabled": False,
                    "currency_code": "RUB",
                    "marketing_seller_price": "1200.00",
                    "old_price": "1500.00",
                    "min_price": "900.00",
                    "net_price": "700.00",
                    "price": "1200.00",
                    "retail_price": "1200.00",
                    "vat": "0.20",
                },
                "commissions": [],
                "marketing_actions": {},
            } for product_id in ids],
            "total": len(ids),
            "cursor": "",
        }

    def read_stocks(self, credentials, payload):
        ids = self._ids(payload)
        self.enrichment_ids.append(("stocks", ids))
        return {
            "items": [{
                "product_id": int(product_id),
                "offer_id": (
                    "offer-archived" if product_id == "202" else "offer-active"
                ),
                "stocks": [{
                    "type": "fbo" if product_id == "101" else "fbs",
                    "present": 4 if product_id == "101" else 0,
                    "reserved": 1 if product_id == "101" else 0,
                    "warehouse_ids": [500],
                }],
            } for product_id in ids],
            "total": len(ids),
            "cursor": "",
        }


class RepeatingCursorCatalogAdapter(SyntheticCatalogAdapter):
    def __init__(self):
        super().__init__()
        self.active_calls = 0

    def list_products(self, credentials, payload):
        if payload["filter"]["visibility"] != "ALL":
            return super().list_products(credentials, payload)
        self.list_payloads.append(payload)
        self.active_calls += 1
        return {
            "result": {
                "items": [{
                    "product_id": 101,
                    "offer_id": "offer-active",
                    "has_fbo_stocks": True,
                    "has_fbs_stocks": False,
                    "archived": False,
                }],
                "total": 2,
                "last_id": (
                    "next-page" if self.active_calls == 1 else "end"
                ),
            }
        }


class MarketplaceListingServiceTest(unittest.TestCase):
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
        self.seller1 = self._seller("seller-one", "one@listing.test")
        self.seller2 = self._seller("seller-two", "two@listing.test")
        self.ozon = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add(self.ozon)
        db.session.flush()
        category = MarketplaceTaxonomyCategory(
            marketplace_id=self.ozon.id,
            external_category_id="10",
            name="Категория",
            full_path="Категория",
            is_available=True,
        )
        db.session.add(category)
        db.session.flush()
        product_type = MarketplaceProductType(
            marketplace_id=self.ozon.id,
            category_id=category.id,
            external_type_id="777",
            name="Тип",
            is_available=True,
        )
        db.session.add(product_type)
        db.session.flush()
        self.account1 = SellerMarketplaceAccount(
            seller_id=self.seller1.id,
            marketplace_id=self.ozon.id,
            external_account_id="synthetic-client",
            label="Ozon 1",
            is_active=True,
            connection_status="connected",
        )
        self.account2 = SellerMarketplaceAccount(
            seller_id=self.seller2.id,
            marketplace_id=self.ozon.id,
            external_account_id="other-client",
            label="Ozon 2",
            is_active=True,
            connection_status="connected",
        )
        db.session.add_all([self.account1, self.account2])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    @staticmethod
    def _seller(username, email):
        user = User(username=username, email=email, is_active=True)
        user.set_password("synthetic-password")
        seller = Seller(user=user, company_name=username)
        db.session.add(seller)
        db.session.commit()
        return seller

    def _stale_listing(self):
        listing = MarketplaceListing(
            seller_id=self.seller1.id,
            marketplace_id=self.ozon.id,
            account_id=self.account1.id,
            offer_id="old-offer",
            external_product_id="999",
            normalized_status="active",
            is_available=True,
            sync_fingerprint="a" * 64,
            created_at=datetime(2026, 7, 1),
        )
        db.session.add(listing)
        db.session.commit()
        return listing

    def test_strict_product_list_normalizer_rejects_loose_and_duplicate_data(self):
        valid = MarketplaceListingService.normalize_product_list_page({
            "result": {
                "items": [{
                    "product_id": 1,
                    "offer_id": "offer",
                    "archived": False,
                    "has_fbo_stocks": False,
                    "has_fbs_stocks": True,
                }],
                "total": 1,
                "last_id": "end",
            }
        })
        self.assertEqual(valid["items"][0]["product_id"], "1")
        with self.assertRaises(MarketplaceCatalogProtocolError):
            MarketplaceListingService.normalize_product_list_page({
                "result": {
                    "items": [{
                        "product_id": 1,
                        "offer_id": "offer",
                        "archived": 0,
                    }],
                    "total": 1,
                    "last_id": "",
                }
            })
        with self.assertRaises(MarketplaceCatalogProtocolError):
            MarketplaceListingService.normalize_product_list_page({
                "result": {
                    "items": [
                        {"product_id": 1, "offer_id": "same"},
                        {"product_id": 2, "offer_id": "same"},
                    ],
                    "total": 2,
                    "last_id": "",
                }
            })

    def test_current_product_info_failed_stage_is_a_bounded_string(self):
        def response(
            status_failed,
            moderate_status="",
            primary_image=None,
            sku=0,
        ):
            return {
                "items": [{
                    "id": 101,
                    "offer_id": "offer-active",
                    "primary_image": (
                        ["https://img.test/primary.jpg"]
                        if primary_image is None else primary_image
                    ),
                    "sku": sku,
                    "statuses": {
                        "is_created": True,
                        "status": "new",
                        "status_failed": status_failed,
                        "moderate_status": moderate_status,
                    },
                }],
            }

        healthy = MarketplaceListingService.normalize_product_info(
            response("")
        )["101"]
        self.assertEqual(healthy["statuses"]["status_failed"], "")
        self.assertNotIn("sku", healthy["identifiers"])
        self.assertEqual(
            healthy["media"]["primary_image"],
            "https://img.test/primary.jpg",
        )

        failed = MarketplaceListingService.normalize_product_info(
            response("imported", "declined")
        )["101"]
        self.assertEqual(failed["statuses"]["status_failed"], "imported")
        normalized, _, _ = MarketplaceListingService._normalize_status(
            archived=False,
            statuses=failed["statuses"],
            visibility={},
            errors=[],
        )
        self.assertEqual(normalized, "error")

        identified = MarketplaceListingService.normalize_product_info(
            response("", sku=7654321)
        )["101"]
        self.assertEqual(identified["identifiers"]["sku"], "7654321")

        with self.assertRaisesRegex(
            MarketplaceCatalogProtocolError,
            "status_failed must be a string",
        ):
            MarketplaceListingService.normalize_product_info(response(False))
        with self.assertRaisesRegex(
            MarketplaceCatalogProtocolError,
            "primary_image must be a bounded list",
        ):
            MarketplaceListingService.normalize_product_info(
                response("", primary_image="https://img.test/legacy.jpg")
            )
        with self.assertRaisesRegex(
            MarketplaceCatalogProtocolError,
            "sku must be positive",
        ):
            MarketplaceListingService.normalize_product_info(
                response("", sku=-1)
            )

    def test_current_product_attributes_accepts_bounded_long_text(self):
        def response(value, *, complex_attributes=None):
            return {
                "result": [{
                    "id": 101,
                    "offer_id": "offer-active",
                    "attributes": [{
                        "id": 4191,
                        "complex_id": 0,
                        "values": [{"value": value}],
                    }],
                    "complex_attributes": (
                        [] if complex_attributes is None else complex_attributes
                    ),
                    "barcodes": [],
                    "images": [],
                }],
                "total": 1,
                "last_id": "",
            }

        live_sized_value = "x" * 5976
        page = MarketplaceListingService.normalize_product_attributes_page(
            response(live_sized_value)
        )
        self.assertEqual(
            page["items"]["101"]["attributes"][0]["values"][0]["value"],
            live_sized_value,
        )

        blank_page = MarketplaceListingService.normalize_product_attributes_page(
            response(" \r\n\t ")
        )
        self.assertIsNone(
            blank_page["items"]["101"]["attributes"][0]["values"][0]["value"]
        )

        empty_complex_page = (
            MarketplaceListingService.normalize_product_attributes_page(
                response("value", complex_attributes=[{}])
            )
        )
        self.assertEqual(
            empty_complex_page["items"]["101"]["complex_attributes"],
            [{"attributes": []}],
        )

        with self.assertRaisesRegex(
            MarketplaceCatalogProtocolError,
            "exceeds 10000 characters",
        ):
            MarketplaceListingService.normalize_product_attributes_page(
                response("x" * 10001)
            )

    def test_current_v5_price_fields_are_kept_and_deprecated_fields_are_dropped(self):
        page = MarketplaceListingService.normalize_prices_page({
            "items": [{
                "product_id": 101,
                "offer_id": "offer-active",
                "price": {
                    "auto_action_enabled": False,
                    "auto_add_to_ozon_actions_list_enabled": True,
                    "currency_code": "RUB",
                    "marketing_seller_price": "1200.00",
                    "min_price": "900.00",
                    "net_price": "700.00",
                    "old_price": "1500.00",
                    "price": "1200.00",
                    "retail_price": "1200.00",
                    "vat": "0.20",
                    "premium_price": "1.00",
                    "recommended_price": "2.00",
                },
                "commissions": {},
                "marketing_actions": {},
            }],
            "total": 1,
            "cursor": "",
        })
        summary = page["items"]["101"]["summary"]
        self.assertEqual(summary["currency"], "RUB")
        self.assertEqual(summary["values"]["price"], "1200.00")
        self.assertEqual(summary["values"]["net_price"], "700.00")
        self.assertNotIn("premium_price", summary["values"])
        self.assertNotIn("recommended_price", summary["values"])

    def test_paused_run_resumes_and_only_complete_sweep_marks_missing(self):
        stale = self._stale_listing()
        adapter = SyntheticCatalogAdapter()
        first = MarketplaceListingService.sync_ozon_account(
            seller_id=self.seller1.id,
            account_id=self.account1.id,
            max_pages=1,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 10, 0, 0),
        )
        self.assertEqual(first.status, "paused")
        self.assertEqual(first.phase, "archived")
        db.session.refresh(stale)
        self.assertTrue(stale.is_available)
        self.assertEqual(MarketplaceListing.query.count(), 2)

        second = MarketplaceListingService.sync_ozon_account(
            seller_id=self.seller1.id,
            account_id=self.account1.id,
            max_pages=1,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.phase, "completed")
        self.assertEqual(second.seen_count, 2)
        self.assertEqual(second.missing_count, 1)
        db.session.refresh(stale)
        self.assertFalse(stale.is_available)
        self.assertEqual(stale.visibility, "missing_from_complete_sweep")

        active = MarketplaceListing.query.filter_by(
            account_id=self.account1.id,
            offer_id="offer-active",
        ).one()
        archived = MarketplaceListing.query.filter_by(
            account_id=self.account1.id,
            offer_id="offer-archived",
        ).one()
        self.assertEqual(active.external_product_id, "101")
        self.assertEqual(active.primary_sku, "1101")
        self.assertEqual(active.product_type.external_type_id, "777")
        self.assertEqual(active.normalized_status, "active")
        self.assertEqual(active.to_public_dict()["stock_summary"]["present"], 4)
        self.assertEqual(archived.normalized_status, "archived")
        self.assertTrue(archived.is_available)
        self.assertTrue(archived.is_archived)
        self.assertEqual(
            [payload["filter"]["visibility"] for payload in adapter.list_payloads],
            ["ALL", "ARCHIVED"],
        )

    def test_catalog_page_links_one_exact_internal_offer_without_ai(self):
        canonical = ImportedProduct(
            seller_id=self.seller1.id,
            external_id="source-active",
            external_vendor_code="offer-active",
            source_type="synthetic",
            title="Общая карточка",
            ai_attributes='{"cached": true}',
        )
        db.session.add(canonical)
        db.session.commit()

        run = MarketplaceListingService.sync_ozon_account(
            seller_id=self.seller1.id,
            account_id=self.account1.id,
            max_pages=1,
            adapter=SyntheticCatalogAdapter(),
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(run.status, "paused")
        listing = MarketplaceListing.query.filter_by(
            account_id=self.account1.id,
            offer_id="offer-active",
        ).one()
        self.assertEqual(listing.imported_product_id, canonical.id)
        self.assertEqual(listing.link_source, "exact_offer_identity")
        self.assertEqual(listing.canonical_link_status, "linked")

    def test_failed_archived_phase_preserves_unseen_listing_and_resume_cursor(self):
        stale = self._stale_listing()
        adapter = SyntheticCatalogAdapter(malformed_archived=True)
        first = MarketplaceListingService.sync_ozon_account(
            seller_id=self.seller1.id,
            account_id=self.account1.id,
            max_pages=1,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        with self.assertRaises(MarketplaceCatalogProtocolError):
            MarketplaceListingService.sync_ozon_account(
                seller_id=self.seller1.id,
                account_id=self.account1.id,
                max_pages=1,
                adapter=adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        failed = db.session.get(MarketplaceCatalogSync, first.id)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.phase, "archived")
        self.assertEqual(failed.cursor, "")
        db.session.refresh(stale)
        self.assertTrue(stale.is_available)

    def test_duplicate_identity_across_cursor_pages_fails_before_finalizer(self):
        adapter = RepeatingCursorCatalogAdapter()
        with self.assertRaises(MarketplaceCatalogProtocolError):
            MarketplaceListingService.sync_ozon_account(
                seller_id=self.seller1.id,
                account_id=self.account1.id,
                max_pages=2,
                adapter=adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        run = MarketplaceCatalogSync.query.one()
        listing = MarketplaceListing.query.one()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.phase, "active")
        self.assertEqual(run.phase_seen_count, 1)
        self.assertEqual(listing.last_catalog_sync_phase, "active")
        self.assertTrue(listing.is_available)

    def test_queries_require_seller_scope_and_validate_account_ownership(self):
        own = MarketplaceListing(
            seller_id=self.seller1.id,
            marketplace_id=self.ozon.id,
            account_id=self.account1.id,
            offer_id="own",
            external_product_id="1",
            sync_fingerprint="b" * 64,
        )
        foreign = MarketplaceListing(
            seller_id=self.seller2.id,
            marketplace_id=self.ozon.id,
            account_id=self.account2.id,
            offer_id="foreign",
            external_product_id="2",
            sync_fingerprint="c" * 64,
        )
        db.session.add_all([own, foreign])
        db.session.commit()
        page = MarketplaceListingService.list_listings(
            seller_id=self.seller1.id,
            marketplace_code="ozon",
        )
        self.assertEqual([item.id for item in page.items], [own.id])
        with self.assertRaises(MarketplaceListingNotFound):
            MarketplaceListingService.get_listing(
                seller_id=self.seller1.id,
                listing_id=foreign.id,
            )
        with self.assertRaises(MarketplaceListingNotFound):
            MarketplaceListingService.list_listings(
                seller_id=self.seller1.id,
                account_id=self.account2.id,
            )
