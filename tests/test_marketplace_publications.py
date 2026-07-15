# -*- coding: utf-8 -*-
"""Ozon product import is strict, durable, idempotent and tenant-scoped."""

from datetime import datetime, timedelta
from copy import deepcopy
import json
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceListing,
    MarketplaceOperation,
    MarketplaceProductDraft,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_drafts import (
    MarketplaceDraftConflict,
    MarketplaceDraftService,
)
from services.marketplace_publications import (
    MarketplacePublicationNotFound,
    MarketplacePublicationService,
)
from services.ozon_api_client import (
    OzonAPIError,
    OzonAmbiguousWriteError,
)
from services.ozon_product_import import (
    OzonProductImportContract,
    OzonProductImportPayloadError,
    OzonProductImportProtocolError,
)
from services.ozon_product_state import OzonProductStateContract


SYNTHETIC_CREDENTIALS = MarketplaceCredentials(
    external_account_id="synthetic-client",
    api_key="synthetic-key",
)


class SyntheticPublicationAdapter:
    capabilities = {"catalog_read", "catalog_write"}

    def __init__(
        self,
        *,
        offer_exists=False,
        ambiguous=False,
        preflight_error=False,
        status="imported",
    ):
        self.offer_exists = offer_exists
        self.ambiguous = ambiguous
        self.preflight_error = preflight_error
        self.status = status
        self.list_calls = []
        self.submitted_payloads = []
        self.status_calls = []

    def require_capability(self, capability):
        if capability not in self.capabilities:
            raise AssertionError(f"missing capability {capability}")

    def list_products(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.list_calls.append(payload)
        if self.preflight_error:
            raise OzonAPIError(
                "synthetic read outage",
                code="synthetic_read_outage",
                request_id="synthetic-request",
            )
        visibility = payload["filter"]["visibility"]
        items = []
        if self.offer_exists and visibility == "ALL":
            items = [{
                "product_id": 987654,
                "offer_id": payload["filter"]["offer_id"][0],
                "archived": False,
                "has_fbo_stocks": False,
                "has_fbs_stocks": False,
            }]
        return {
            "result": {
                "items": items,
                "total": len(items),
                "last_id": "",
            }
        }

    def get_operation_limits(self, credentials):
        assert credentials == SYNTHETIC_CREDENTIALS
        return {
            "operation_limits": [{
                "operation": "product_create",
                "limit": 100,
                "usage": 4,
                "remaining": 96,
                "reset_at": "2026-07-16T00:00:00Z",
            }]
        }

    def submit_products(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.submitted_payloads.append(payload)
        if self.ambiguous:
            self.offer_exists = True
            raise OzonAmbiguousWriteError(
                "synthetic ambiguous write",
                code="synthetic_ambiguous_write",
                request_id="synthetic-write-request",
            )
        return {"result": {"task_id": 456}}

    def get_submission(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.status_calls.append(payload)
        errors = []
        product_id = 987654 if self.status == "imported" else 0
        if self.status in {"failed", "skipped"}:
            errors = [{
                "code": "SYNTHETIC_REJECT",
                "message": "Synthetic item rejection",
            }]
        return {
            "result": {
                "items": [{
                    "offer_id": "safe-offer",
                    "product_id": product_id,
                    "status": self.status,
                    "errors": errors,
                }],
                "total": 1,
            }
        }


class SyntheticFullStateAdapter(SyntheticPublicationAdapter):
    """Synthetic exact-state adapter for update/archive state machines."""

    def __init__(self, live_payload=None, *, create_mode=False):
        super().__init__(offer_exists=not create_mode)
        self.live_payload = deepcopy(live_payload)
        self.archived = False
        self.pending_payload = None
        self.archive_calls = []
        self.full_read_calls = []

    def _item(self):
        if not self.live_payload:
            raise AssertionError("synthetic live payload is unavailable")
        return self.live_payload["items"][0]

    def list_products(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.list_calls.append(payload)
        visibility = payload["filter"]["visibility"]
        visible = self.live_payload is not None and (
            (visibility == "ALL" and not self.archived)
            or (visibility == "ARCHIVED" and self.archived)
        )
        items = []
        if visible:
            items = [{
                "product_id": 987654,
                "offer_id": self._item()["offer_id"],
                "archived": self.archived,
                "has_fbo_stocks": False,
                "has_fbs_stocks": False,
            }]
        return {
            "result": {"items": items, "total": len(items), "last_id": ""}
        }

    def get_operation_limits(self, credentials):
        assert credentials == SYNTHETIC_CREDENTIALS
        return {
            "operation_limits": [{
                "operation": "product_update" if self.live_payload else "product_create",
                "limit": 100,
                "usage": 1,
                "remaining": 99,
            }]
        }

    def submit_products(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.submitted_payloads.append(deepcopy(payload))
        self.pending_payload = deepcopy(payload)
        return {"result": {"task_id": 456}}

    def get_submission(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.status_calls.append(payload)
        if self.pending_payload is not None:
            self.live_payload = self.pending_payload
            self.pending_payload = None
            self.offer_exists = True
            self.archived = False
        return {
            "result": {
                "items": [{
                    "offer_id": self._item()["offer_id"],
                    "product_id": 987654,
                    "status": "imported",
                    "errors": [],
                }],
                "total": 1,
            }
        }

    def get_products(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.full_read_calls.append(("info", deepcopy(payload)))
        item = self._item()
        media = self._media(item)
        return {"items": [{
            "id": 987654,
            "offer_id": item["offer_id"],
            "name": item["name"],
            "description": self._description(item),
            "description_category_id": item["description_category_id"],
            "type_id": item["type_id"],
            "is_archived": self.archived,
            "barcodes": [item["barcode"]] if item.get("barcode") else [],
            "primary_image": media["primary_image"],
            "images": [media["primary_image"], *media["images"]],
            "statuses": {},
            "visibility_details": {},
            "errors": [],
        }]}

    def get_product_attributes(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.full_read_calls.append(("attributes", deepcopy(payload)))
        item = self._item()
        return {
            "result": [{
                "id": 987654,
                "offer_id": item["offer_id"],
                "name": item["name"],
                "description_category_id": item["description_category_id"],
                "type_id": item["type_id"],
                "attributes": deepcopy(item["attributes"]),
                "complex_attributes": deepcopy(item["complex_attributes"]),
                "width": item["width"],
                "height": item["height"],
                "depth": item["depth"],
                "dimension_unit": item["dimension_unit"],
                "weight": item["weight"],
                "weight_unit": item["weight_unit"],
                "barcodes": [item["barcode"]] if item.get("barcode") else [],
                "images": [],
                "sku": 7654321,
            }],
            "total": 1,
            "last_id": "",
        }

    def read_prices(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.full_read_calls.append(("prices", deepcopy(payload)))
        item = self._item()
        price = {
            "price": item["price"],
            "currency_code": item["currency_code"],
            "vat": item["vat"],
        }
        if item.get("old_price"):
            price["old_price"] = item["old_price"]
        return {
            "items": [{
                "product_id": 987654,
                "offer_id": item["offer_id"],
                "price": price,
            }],
            "total": 1,
            "cursor": "",
        }

    def get_product_pictures(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.full_read_calls.append(("pictures", deepcopy(payload)))
        media = self._media(self._item())
        return {"items": [{
            "product_id": 987654,
            "primary_photo": [media["primary_image"]],
            "photo": media["images"],
            "color_photo": [media["color_image"]] if media.get("color_image") else [],
            "errors": [],
        }]}

    def archive_products(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.archive_calls.append(deepcopy(payload))
        self.archived = True
        return {"result": True}

    @staticmethod
    def _media(item):
        images = list(item.get("images", []))
        primary = item.get("primary_image")
        if not primary:
            primary = images.pop(0)
        result = {"primary_image": primary, "images": images}
        if item.get("color_image"):
            result["color_image"] = item["color_image"]
        return result

    @staticmethod
    def _description(item):
        for attribute in item.get("attributes", []):
            if attribute.get("id") == 4191 and attribute.get("values"):
                return attribute["values"][0].get("value")
        return None

class OzonPublicationFixture:
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
        now = datetime.utcnow()
        self.user = User(
            username="ozon-publication",
            email="ozon-publication@test.local",
            is_active=True,
        )
        self.user.set_password("synthetic-password")
        self.seller = Seller(user=self.user, company_name="Publication Seller")
        self.foreign_user = User(
            username="ozon-publication-foreign",
            email="ozon-publication-foreign@test.local",
            is_active=True,
        )
        self.foreign_user.set_password("synthetic-password")
        self.foreign_seller = Seller(
            user=self.foreign_user,
            company_name="Foreign Seller",
        )
        self.marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
            categories_synced_at=now,
            categories_snapshot_hash="tree-hash",
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
        category = MarketplaceTaxonomyCategory(
            marketplace_id=self.marketplace.id,
            external_category_id="10",
            name="Категория",
            full_path="Категория",
            is_available=True,
            last_seen_at=now,
        )
        db.session.add_all([self.account, category])
        db.session.flush()
        self.product_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=category.id,
            external_type_id="777",
            name="Тип",
            is_available=True,
            is_enabled=True,
            attributes_synced_at=now,
            attributes_sync_status="success",
            attributes_schema_hash="schema-hash",
            attributes_version=3,
            attributes_count=1,
            required_attributes_count=0,
        )
        self.source = ImportedProduct(
            seller_id=self.seller.id,
            external_id="source-1",
            external_vendor_code="safe-offer",
            source_type="synthetic",
            title="Безопасный товар",
            description="Наблюдаемое описание",
            category="Категория",
        )
        db.session.add_all([self.product_type, self.source])
        db.session.flush()
        description = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="4191",
            name="Аннотация",
            data_type="String",
            is_required=False,
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=now,
        )
        self.draft = MarketplaceProductDraft(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            imported_product_id=self.source.id,
            product_type_id=self.product_type.id,
            offer_id="safe-offer",
            external_category_id="10",
            external_type_id="777",
            status="ready",
            source_fact_hash="a" * 64,
            source_facts_json="{}",
            provenance_json="{}",
            content_json=json.dumps({
                "name": "Безопасный товар",
                "description": "Наблюдаемое описание",
            }, ensure_ascii=False),
            attributes_json="[]",
            complex_attributes_json="[]",
            media_json=json.dumps({
                "images": ["https://img.test/product.jpg"],
            }),
            dimensions_json=json.dumps({
                "width": "200",
                "height": "30",
                "depth": "300",
                "dimension_unit": "MILLIMETERS",
                "weight": "250",
                "weight_unit": "GRAMS",
            }),
            barcodes_json=json.dumps(["4600000000001"]),
            commercial_json=json.dumps({
                "price": "1000",
                "old_price": "1200",
                "vat": "0.22",
                "currency_code": "RUB",
            }),
            schema_version=3,
            schema_hash="schema-hash",
            validation_status="valid",
            validation_result_json=json.dumps({
                "publishable": True,
                "errors": [],
                "warnings": [],
            }),
            validated_at=now,
        )
        db.session.add_all([description, self.draft])
        db.session.commit()
        self.expected_version = self.draft.version

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def validation_result(self):
        return {
            "publishable": True,
            "errors": [],
            "warnings": [],
            "schema": {
                "hash": "schema-hash",
                "version": 3,
            },
        }

    def start(self, adapter, *, key="publication-key-0001"):
        with patch.object(
            MarketplaceDraftService,
            "_build_validation_result",
            return_value=self.validation_result(),
        ):
            return MarketplacePublicationService.start_publication(
                seller_id=self.seller.id,
                draft_id=self.draft.id,
                expected_version=self.expected_version,
                idempotency_key=key,
                created_by_user_id=self.user.id,
                adapter=adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )

    def desired_payload(self):
        return OzonProductImportContract.build_payload(self.draft)

    def prior_payload(self):
        payload = deepcopy(self.desired_payload())
        item = payload["items"][0]
        item["name"] = "Предыдущее название"
        item["price"] = "900"
        item["old_price"] = "1100"
        item["images"] = ["https://img.test/prior.jpg"]
        for attribute in item["attributes"]:
            if attribute["id"] == 4191:
                attribute["values"] = [{"value": "Предыдущее описание"}]
        return payload

    def attach_listing(self, payload=None):
        payload = payload or self.prior_payload()
        item = payload["items"][0]
        listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            imported_product_id=self.source.id,
            product_type_id=self.product_type.id,
            offer_id=item["offer_id"],
            external_product_id="987654",
            title=item["name"],
            normalized_status="active",
            is_available=True,
            is_archived=False,
            stock_summary_json=json.dumps({"preserve": True}),
            sync_fingerprint="b" * 64,
        )
        db.session.add(listing)
        db.session.flush()
        self.draft.published_listing_id = listing.id
        db.session.commit()
        self.expected_version = self.draft.version
        return listing

    def start_update(self, adapter, *, key="product-update-key-0001"):
        with patch.object(
            MarketplaceDraftService,
            "_build_validation_result",
            return_value=self.validation_result(),
        ):
            return MarketplacePublicationService.start_update(
                seller_id=self.seller.id,
                draft_id=self.draft.id,
                expected_version=self.expected_version,
                idempotency_key=key,
                created_by_user_id=self.user.id,
                adapter=adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )


class OzonProductImportContractTest(OzonPublicationFixture, unittest.TestCase):
    def test_builder_is_whitelist_only_and_maps_description_attribute(self):
        payload = OzonProductImportContract.build_payload(self.draft)
        self.assertEqual(set(payload), {"items"})
        item = payload["items"][0]
        self.assertEqual(item["offer_id"], "safe-offer")
        self.assertEqual(item["description_category_id"], 10)
        self.assertEqual(item["type_id"], 777)
        self.assertEqual(item["dimension_unit"], "mm")
        self.assertEqual(item["weight_unit"], "g")
        self.assertEqual(item["barcode"], "4600000000001")
        self.assertNotIn("description", item)
        self.assertNotIn("images360", item)
        description = next(
            value for value in item["attributes"]
            if value["id"] == 4191
        )
        self.assertEqual(description, {
            "id": 4191,
            "complex_id": 0,
            "values": [{"value": "Наблюдаемое описание"}],
        })

    def test_builder_rejects_long_offer_fractional_physical_and_extra_barcode(self):
        self.draft.offer_id = "x" * 51
        with self.assertRaises(OzonProductImportPayloadError):
            OzonProductImportContract.build_payload(self.draft)
        self.draft.offer_id = "safe-offer"
        dimensions = json.loads(self.draft.dimensions_json)
        dimensions["weight"] = "250.5"
        self.draft.dimensions_json = json.dumps(dimensions)
        with self.assertRaises(OzonProductImportPayloadError):
            OzonProductImportContract.build_payload(self.draft)
        dimensions["weight"] = "250"
        self.draft.dimensions_json = json.dumps(dimensions)
        self.draft.barcodes_json = json.dumps(["1", "2"])
        with self.assertRaises(OzonProductImportPayloadError):
            OzonProductImportContract.build_payload(self.draft)

    def test_status_exact_set_and_skipped_fail_closed(self):
        skipped = OzonProductImportContract.normalize_status({
            "result": {
                "items": [{
                    "offer_id": "safe-offer",
                    "product_id": 0,
                    "status": "skipped",
                    "errors": [{"code": "SKIPPED"}],
                }],
                "total": 1,
            }
        }, expected_offer_ids=["safe-offer"])
        self.assertEqual(skipped["aggregate_status"], "failed")
        with self.assertRaises(OzonProductImportProtocolError):
            OzonProductImportContract.normalize_status({
                "result": {
                    "items": [{
                        "offer_id": "foreign-offer",
                        "product_id": 1,
                        "status": "imported",
                        "errors": [],
                    }],
                    "total": 1,
                }
            }, expected_offer_ids=["safe-offer"])

    def test_quota_prefers_new_contract_and_malformed_new_shape_does_not_fallback(self):
        normalized = OzonProductImportContract.normalize_quota({
            "operation_limits": [{
                "operation": "product_create",
                "limit": 100,
                "usage": 10,
                "remaining": 90,
            }],
            "daily_create": {"limit": 999, "usage": 0},
        })
        self.assertEqual(normalized["source"], "operation_limits")
        self.assertEqual(normalized["remaining"], 90)
        split = OzonProductImportContract.normalize_quota({
            "operation_limits": [
                {
                    "operation": "product_create",
                    "limit": 100,
                    "usage": 10,
                    "remaining": 90,
                },
                {
                    "operation": "product_update",
                    "limit": 100,
                    "usage": 100,
                    "remaining": 0,
                },
            ],
        }, mode="create")
        self.assertEqual(split["remaining"], 90)
        self.assertEqual(
            [item["name"] for item in split["entries"]],
            ["product_create"],
        )
        with self.assertRaises(OzonProductImportProtocolError):
            OzonProductImportContract.normalize_quota({
                "operation_limits": {"unexpected": True},
                "daily_create": {"limit": 999, "usage": 0},
            })


class MarketplacePublicationServiceTest(OzonPublicationFixture, unittest.TestCase):
    def test_submit_poll_finalize_and_idempotent_retry(self):
        adapter = SyntheticPublicationAdapter()
        operation = self.start(adapter)
        self.assertEqual(operation.status, "submitted")
        self.assertEqual(operation.attempt_count, 1)
        self.assertEqual(operation.external_task_id, "456")
        self.assertEqual(len(adapter.submitted_payloads), 1)
        before = json.loads(operation.snapshot.before_state_json)
        self.assertIs(before["exists"], False)

        completed = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.quota_reserved, 0)
        listing = MarketplaceListing.query.filter_by(
            account_id=self.account.id,
            offer_id="safe-offer",
        ).one()
        self.assertEqual(listing.external_product_id, "987654")
        self.assertEqual(listing.imported_product_id, self.source.id)
        db.session.refresh(self.draft)
        self.assertEqual(self.draft.status, "published")
        self.assertEqual(self.draft.published_listing_id, listing.id)
        self.assertEqual(completed.snapshot.rollback_status, "available")

        repeated = self.start(adapter)
        self.assertEqual(repeated.id, completed.id)
        self.assertEqual(len(adapter.submitted_payloads), 1)

    def test_existing_offer_fails_before_quota_and_write(self):
        adapter = SyntheticPublicationAdapter(offer_exists=True)
        operation = self.start(adapter, key="publication-key-existing")
        self.assertEqual(operation.status, "failed")
        self.assertEqual(operation.error_code, "offer_exists_upstream")
        self.assertEqual(operation.attempt_count, 0)
        self.assertEqual(adapter.submitted_payloads, [])

    def test_ambiguous_write_reconciles_live_without_retry(self):
        adapter = SyntheticPublicationAdapter(ambiguous=True)
        operation = self.start(adapter, key="publication-key-ambiguous")
        self.assertEqual(operation.status, "uncertain")
        self.assertEqual(operation.attempt_count, 1)
        self.assertEqual(len(adapter.submitted_payloads), 1)

    def test_manual_uncertain_resolution_releases_only_local_quota(self):
        adapter = SyntheticPublicationAdapter(ambiguous=True)
        operation = self.start(adapter, key="publication-key-manual-resolution")
        self.assertEqual(operation.status, "uncertain")
        self.assertEqual(operation.quota_reserved, 1)

        resolved = MarketplacePublicationService.resolve_uncertain(
            seller_id=self.seller.id,
            operation_id=operation.id,
            expected_version=operation.version,
            reason="Проверено вручную в кабинете; результат пока неясен",
            resolved_by_user_id=self.user.id,
        )
        self.assertEqual(resolved.status, "uncertain")
        self.assertEqual(resolved.quota_reserved, 0)
        self.assertIsNone(resolved.next_poll_at)
        self.assertEqual(resolved.error_code, "manual_uncertain_resolution")
        summary = json.loads(resolved.request_summary_json)
        self.assertEqual(
            summary["manual_resolution"]["upstream_outcome"],
            "still_uncertain",
        )
        self.assertEqual(len(adapter.submitted_payloads), 1)

        completed = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.reconcile_count, 1)
        self.assertEqual(len(adapter.submitted_payloads), 1)

    def test_prewrite_outage_stays_queued_and_rollout_flag_prevents_submit(self):
        adapter = SyntheticPublicationAdapter(preflight_error=True)
        operation = self.start(adapter, key="publication-key-prewrite")
        self.assertEqual(operation.status, "queued")
        self.assertEqual(operation.attempt_count, 0)
        adapter.preflight_error = False

        still_queued = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            allow_submission=False,
        )
        self.assertEqual(still_queued.status, "queued")
        self.assertEqual(adapter.submitted_payloads, [])

        submitted = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            allow_submission=True,
        )
        self.assertEqual(submitted.status, "submitted")
        self.assertEqual(len(adapter.submitted_payloads), 1)

    def test_task_poll_outage_stops_automatic_retries_after_deadline(self):
        adapter = SyntheticPublicationAdapter()
        operation = self.start(adapter, key="publication-key-poll-deadline")
        adapter.get_submission = MagicMock(side_effect=OzonAPIError(
            "synthetic status outage",
            code="synthetic_status_outage",
            request_id="synthetic-status-request",
        ))

        stopped = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=operation.deadline_at + timedelta(seconds=1),
        )

        self.assertEqual(stopped.status, "uncertain")
        self.assertEqual(
            stopped.error_code,
            "ozon_task_poll_deadline_exceeded",
        )
        self.assertIsNone(stopped.next_poll_at)
        self.assertEqual(stopped.poll_count, 1)

    def test_active_operation_blocks_draft_mutation_and_foreign_read(self):
        operation = self.start(
            SyntheticPublicationAdapter(status="pending"),
            key="publication-key-active",
        )
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.seller.id,
                draft_id=self.draft.id,
                expected_version=self.draft.version,
                patch={"offer_id": "changed-offer"},
            )
        with self.assertRaises(MarketplacePublicationNotFound):
            MarketplacePublicationService.get_operation(
                seller_id=self.foreign_seller.id,
                operation_id=operation.id,
            )

    def test_public_serializer_never_exposes_payload_idempotency_or_secret(self):
        operation = self.start(
            SyntheticPublicationAdapter(),
            key="publication-key-public",
        )
        document = operation.to_public_dict(detail=True)
        encoded = json.dumps(document, ensure_ascii=False)
        self.assertNotIn("synthetic-key", encoded)
        self.assertNotIn("idempotency_key", document)
        self.assertNotIn("submitted_state", encoded)
        self.assertNotIn("api_key", encoded.lower())
        self.assertEqual(
            MarketplaceOperation.query.filter_by(seller_id=self.seller.id).count(),
            1,
        )

    def test_full_state_update_and_exact_prior_state_rollback(self):
        prior = self.prior_payload()
        listing = self.attach_listing(prior)
        adapter = SyntheticFullStateAdapter(prior)

        operation = self.start_update(adapter)
        self.assertEqual(operation.operation_kind, "product_update")
        self.assertEqual(operation.status, "submitted")
        self.assertEqual(operation.snapshot.snapshot_kind, "product_update")
        self.assertEqual(
            operation.snapshot.before_fingerprint,
            OzonProductStateContract.fingerprint(prior),
        )
        self.assertEqual(operation.attempt_count, 1)
        self.assertEqual(len(adapter.submitted_payloads), 1)

        completed = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.snapshot.rollback_status, "available")
        self.assertEqual(
            completed.snapshot.confirmed_fingerprint,
            OzonProductStateContract.fingerprint(self.desired_payload()),
        )
        db.session.refresh(listing)
        self.assertEqual(json.loads(listing.stock_summary_json), {"preserve": True})
        self.assertEqual(listing.title, "Безопасный товар")

        rollback = MarketplacePublicationService.start_update_rollback(
            seller_id=self.seller.id,
            operation_id=completed.id,
            expected_version=completed.version,
            idempotency_key="product-update-rollback-key-0001",
            created_by_user_id=self.user.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(rollback.operation_kind, "product_update_rollback")
        self.assertEqual(rollback.status, "submitted")
        db.session.refresh(completed.snapshot)
        self.assertEqual(completed.snapshot.rollback_status, "pending")

        restored = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=rollback.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(restored.status, "succeeded")
        self.assertEqual(
            OzonProductStateContract.fingerprint(adapter.live_payload),
            OzonProductStateContract.fingerprint(prior),
        )
        db.session.refresh(completed.snapshot)
        self.assertEqual(completed.snapshot.rollback_status, "succeeded")
        self.assertEqual(len(adapter.submitted_payloads), 2)

    def test_update_rollback_drift_fails_before_second_write(self):
        prior = self.prior_payload()
        self.attach_listing(prior)
        adapter = SyntheticFullStateAdapter(prior)
        completed = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=self.start_update(
                adapter,
                key="product-update-key-drift",
            ).id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        drifted = deepcopy(adapter.live_payload)
        drifted["items"][0]["name"] = "Внешнее изменение"
        adapter.live_payload = drifted

        rollback = MarketplacePublicationService.start_update_rollback(
            seller_id=self.seller.id,
            operation_id=completed.id,
            expected_version=completed.version,
            idempotency_key="product-update-rollback-key-drift",
            created_by_user_id=self.user.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(rollback.status, "failed")
        self.assertEqual(rollback.error_code, "update_before_state_drift")
        self.assertEqual(len(adapter.submitted_payloads), 1)
        db.session.refresh(completed.snapshot)
        self.assertEqual(completed.snapshot.rollback_status, "conflict")

    def test_create_compensation_archives_only_unchanged_created_listing(self):
        desired = self.desired_payload()
        adapter = SyntheticFullStateAdapter(create_mode=True)
        created = self.start(adapter, key="publication-key-archive")
        completed = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=created.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.snapshot.rollback_status, "available")
        self.assertEqual(
            OzonProductStateContract.fingerprint(adapter.live_payload),
            OzonProductStateContract.fingerprint(desired),
        )

        archived = MarketplacePublicationService.start_create_rollback(
            seller_id=self.seller.id,
            operation_id=completed.id,
            expected_version=completed.version,
            idempotency_key="product-create-archive-key-0001",
            created_by_user_id=self.user.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(archived.status, "succeeded")
        self.assertEqual(archived.operation_kind, "product_import_rollback")
        self.assertEqual(adapter.archive_calls, [{"product_id": [987654]}])
        db.session.refresh(completed.snapshot)
        self.assertEqual(completed.snapshot.rollback_status, "succeeded")
        listing = db.session.get(MarketplaceListing, completed.listing_id)
        self.assertTrue(listing.is_archived)
        db.session.refresh(self.draft)
        self.assertEqual(self.draft.status, "archived")


if __name__ == "__main__":
    unittest.main()
