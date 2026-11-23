"""Synthetic tests for the bounded, source-scoped Ozon brand search."""

from datetime import datetime, timedelta
import json
import os
from unittest import TestCase
from unittest.mock import patch

from cryptography.fernet import Fernet

from models import (
    BackgroundJob,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceListing,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    db,
)
from services.marketplace_drafts import MarketplaceDraftService
from services.ozon_api_client import OzonAPIError, OzonRateLimitError
from services.ozon_brand_search import OzonBrandSearch
from services.ozon_brand_search_models import OzonBrandSearchEvidence
from services.ozon_reference_service import OzonReferenceService
from services.ozon_upload_queue import OzonUploadQueueService
from tests.test_marketplace_publications import OzonPublicationFixture


class _FakeSearchClient:
    def __init__(self, response=None, error=None, before_return=None):
        self.response = response
        self.error = error
        self.before_return = before_return
        self.calls = []

    def search_description_category_attribute_values(self, payload):
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        if self.before_return is not None:
            self.before_return()
        return self.response


class OzonBrandSearchTest(OzonPublicationFixture, TestCase):
    def setUp(self):
        self._encryption_patch = patch.dict(
            os.environ,
            {"ENCRYPTION_KEY": Fernet.generate_key().decode("ascii")},
        )
        self._encryption_patch.start()
        super().setUp()
        now = datetime.utcnow()
        self.account.external_account_id = "123456"
        self.account.set_credentials({"api_key": "synthetic-key"})
        self.account.connection_status = "connected"
        self.marketplace.categories_synced_at = now
        self.marketplace.categories_snapshot_hash = "1" * 64
        self.product_type.attributes_synced_at = now
        self.product_type.attributes_schema_hash = "2" * 64
        self.product_type.attributes_version = 3
        self.product_type.attributes_count = 1
        self.product_type.required_attributes_count = 1

        self.definition = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="31",
            name="Бренд",
            data_type="String",
            is_required=True,
            dictionary_id="100",
            max_value_count=1,
            is_collection=False,
            is_available=True,
            is_enabled=True,
            values_synced_at=now,
            values_sync_status="success",
            values_snapshot_hash="3" * 64,
            values_version=2,
            values_count=0,
            restriction_value_ids_json="[]",
        )
        db.session.add(self.definition)
        db.session.flush()

        self.supplier = Supplier(name="Synthetic supplier", code="synthetic")
        db.session.add(self.supplier)
        db.session.flush()
        db.session.add(SellerSupplier(
            seller_id=self.seller.id,
            supplier_id=self.supplier.id,
            is_active=True,
        ))
        raw = {
            "brand": "Acme",
            "characteristics": [{"name": "Бренд", "value": "Acme"}],
        }
        self.supplier_product = SupplierProduct(
            supplier_id=self.supplier.id,
            external_id=self.source.external_id,
            content_revision=7,
            title=self.source.title,
            brand="Acme",
            characteristics_json=json.dumps(raw["characteristics"], ensure_ascii=False),
            original_data_json=json.dumps(raw, ensure_ascii=False),
        )
        db.session.add(self.supplier_product)
        db.session.flush()
        self.source.supplier_product_id = self.supplier_product.id
        self.source.supplier_id = self.supplier.id
        self.source.source_type = self.supplier.code
        self.source.supplier_content_revision = 7
        self.source.brand = "Acme"
        self.source.original_data = json.dumps(raw, ensure_ascii=False)
        facts, provenance, fact_hash = MarketplaceDraftService._fact_snapshot(
            self.source,
        )
        self.draft.source_facts_json = MarketplaceDraftService._canonical_json(
            facts, dict,
        )
        self.draft.provenance_json = MarketplaceDraftService._canonical_json(
            provenance, dict,
        )
        self.draft.source_fact_hash = fact_hash
        self.draft.attributes_json = "[]"

        token = "a" * 32
        self.job = BackgroundJob(
            job_uid="brand-search-job",
            seller_id=self.seller.id,
            job_type="ozon_bulk_upload",
            status="running",
            total=1,
        )
        db.session.add(self.job)
        db.session.flush()
        self.run = OzonBulkUploadRun(
            job_id=self.job.id,
            seller_id=self.seller.id,
            account_id=self.account.id,
            mode="source_prepare",
            request_key_hash="4" * 64,
            request_fingerprint="5" * 64,
            created_by_user_id=self.user.id,
            state="active",
            next_due_at=now,
            lease_token=token,
            lease_until=now + timedelta(minutes=2),
        )
        db.session.add(self.run)
        db.session.flush()
        self.item = OzonBulkUploadItem(
            run_id=self.run.id,
            ordinal=1,
            imported_product_id=self.source.id,
            draft_id=self.draft.id,
            prepared_version=self.draft.version,
            phase="preparing",
            next_due_at=None,
            title_snapshot=self.source.title,
            error_code=OzonUploadQueueService.QUEUE_CREATED_DRAFT_CODE,
        )
        db.session.add(self.item)
        db.session.commit()
        self.token = token

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            self._encryption_patch.stop()

    def _prepare(self, client, *, confirmed=True, now=None):
        return OzonBrandSearch.prepare_source_brand(
            run=self.run,
            item=self.item,
            account=self.account,
            product=self.source,
            draft=self.draft,
            lease_token=self.token,
            confirmed_queue_created_draft=confirmed,
            deadline_monotonic=None,
            now=now or datetime.utcnow(),
            client_factory=lambda credentials, **kwargs: client,
        )

    def _store_brand_cell(self, *, value_id="101", value="Acme"):
        self.draft.attributes_json = json.dumps([{
            "attribute_id": "31",
            "complex_id": "0",
            "values": [{
                "dictionary_value_id": value_id,
                "value": value,
            }],
        }])
        db.session.commit()

    def _draft_state(self):
        db.session.refresh(self.draft)
        return (
            self.draft.version,
            self.draft.created_at,
            self.draft.updated_at,
            self.draft.attributes_json,
        )

    @staticmethod
    def _brand_cells(draft):
        return [
            row for row in json.loads(draft.attributes_json or "[]")
            if row.get("attribute_id") in {"31", "85"}
        ]

    def test_exact_single_id_is_staged_and_search_read_is_local_only(self):
        client = _FakeSearchClient({
            "result": [
                {"id": 101, "value": "Acme", "info": "discarded"},
                {"id": 102, "value": "Acme Pro", "picture": "discarded"},
            ],
        })
        self.assertTrue(self._prepare(client))
        self.assertEqual(client.calls, [{
            "attribute_id": 31,
            "description_category_id": 10,
            "type_id": 777,
            "limit": 100,
            "value": "Acme",
        }])
        db.session.refresh(self.draft)
        self.assertEqual(self._brand_cells(self.draft), [{
            "attribute_id": "31",
            "complex_id": "0",
            "values": [{"dictionary_value_id": "101", "value": "Acme"}],
        }])
        self.assertEqual(
            OzonBrandSearch.value_choices(self.draft, self.definition),
            [{"dictionary_value_id": "101", "value": "Acme"}],
        )
        self.assertEqual(len(client.calls), 1)
        provenance = json.loads(self.draft.provenance_json)
        self.assertEqual(
            provenance["ozon_brand_search.31"]["dictionary_value_id"],
            "101",
        )

    def test_distinct_exact_provider_ids_remain_picker_choices(self):
        client = _FakeSearchClient({
            "result": [
                {"id": 101, "value": "Acme"},
                {"id": 102, "value": "ACME"},
                {"id": 103, "value": "Acme Pro"},
            ],
        })
        self.assertFalse(self._prepare(client))
        db.session.refresh(self.draft)
        self.assertEqual(self._brand_cells(self.draft), [])
        self.assertEqual(
            OzonBrandSearch.value_choices(self.draft, self.definition),
            [
                {"dictionary_value_id": "101", "value": "Acme"},
                {"dictionary_value_id": "102", "value": "ACME"},
            ],
        )
        evidence = OzonBrandSearchEvidence.query.one()
        self.assertTrue(evidence.complete_result)
        self.assertNotIn("info", evidence.choices_json)

    def test_allowlist_does_not_resolve_provider_ambiguity_for_auto_stage(self):
        self.definition.restriction_value_ids_json = json.dumps(["101"])
        db.session.commit()
        client = _FakeSearchClient({
            "result": [
                {"id": 101, "value": "Acme"},
                {"id": 102, "value": "ACME"},
            ],
        })
        self.assertFalse(self._prepare(client))
        db.session.refresh(self.draft)
        self.assertEqual(self._brand_cells(self.draft), [])
        self.assertEqual(
            OzonBrandSearch.value_choices(self.draft, self.definition),
            [{"dictionary_value_id": "101", "value": "Acme"}],
        )

    def test_cohort_scope_ignores_other_product_facts(self):
        first_scope = OzonBrandSearch._definition_scope(
            draft=self.draft,
            definition=self.definition,
        )
        second_raw = {
            "brand": "Acme",
            "characteristics": [{"name": "brand", "value": "Acme"}],
        }
        second_product = type(self.source)(
            seller_id=self.seller.id,
            external_id="source-2",
            external_vendor_code="offer-source-2",
            source_type=self.supplier.code,
            title="Different source title",
            brand="Acme",
            supplier_id=self.supplier.id,
            supplier_product_id=None,
            supplier_content_revision=7,
            original_data=json.dumps(second_raw, ensure_ascii=False),
        )
        db.session.add(second_product)
        db.session.flush()
        second_supplier_product = SupplierProduct(
            supplier_id=self.supplier.id,
            external_id=second_product.external_id,
            content_revision=7,
            brand="Acme",
            characteristics_json=json.dumps(
                second_raw["characteristics"], ensure_ascii=False,
            ),
            original_data_json=json.dumps(second_raw, ensure_ascii=False),
        )
        db.session.add(second_supplier_product)
        db.session.flush()
        second_product.supplier_product_id = second_supplier_product.id
        second_facts, second_provenance, second_hash = (
            MarketplaceDraftService._fact_snapshot(second_product)
        )
        second_draft = type(self.draft)(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            imported_product_id=second_product.id,
            product_type_id=self.product_type.id,
            offer_id="offer-source-2",
            external_category_id="10",
            external_type_id="777",
            status="draft",
            source_fact_hash=second_hash,
            source_facts_json=MarketplaceDraftService._canonical_json(
                second_facts, dict,
            ),
            provenance_json=MarketplaceDraftService._canonical_json(
                second_provenance, dict,
            ),
            content_json="{}",
            attributes_json="[]",
            complex_attributes_json="[]",
            media_json="{}",
            dimensions_json="{}",
            barcodes_json="[]",
            commercial_json="{}",
        )
        db.session.add(second_draft)
        db.session.commit()
        second_scope = OzonBrandSearch._definition_scope(
            draft=second_draft,
            definition=self.definition,
        )
        self.assertNotEqual(self.draft.source_fact_hash, second_hash)
        self.assertEqual(first_scope["scope_hash"], second_scope["scope_hash"])

    def test_expired_selected_choice_can_refresh_without_overwrite(self):
        client = _FakeSearchClient({"result": [{"id": 101, "value": "Acme"}]})
        self.assertTrue(self._prepare(client))
        db.session.refresh(self.draft)
        self.draft.attributes_json = json.dumps([{
            "attribute_id": "31",
            "complex_id": "0",
            "values": [{"dictionary_value_id": "101", "value": "Acme"}],
        }])
        db.session.commit()
        evidence = OzonBrandSearchEvidence.query.one()
        now = datetime.utcnow()
        evidence.reserved_at = now - timedelta(hours=13)
        evidence.finished_at = now - timedelta(hours=13) + timedelta(seconds=1)
        evidence.expires_at = now - timedelta(seconds=1)
        db.session.commit()

        renewal = _FakeSearchClient({"result": [{"id": 101, "value": "Acme"}]})
        self.assertFalse(self._prepare(renewal, confirmed=False))
        self.assertEqual(len(renewal.calls), 1)
        db.session.refresh(self.draft)
        self.assertEqual(self._brand_cells(self.draft)[0]["values"], [
            {"dictionary_value_id": "101", "value": "Acme"},
        ])
        self.assertEqual(
            OzonBrandSearch.value_choices(self.draft, self.definition),
            [{"dictionary_value_id": "101", "value": "Acme"}],
        )

    def test_search_only_cell_reverifies_once_per_fresh_reference_scope(self):
        first = _FakeSearchClient({"result": [{"id": 101, "value": "Acme"}]})
        self.assertTrue(self._prepare(first))
        original_scope = OzonBrandSearch._definition_scope(
            self.draft, self.definition,
        )
        original_state = self._draft_state()
        self.assertEqual(self._brand_cells(self.draft)[0]["values"], [{
            "value": "Acme",
            "dictionary_value_id": "101",
        }])

        # A fresh dictionary and credential generation must not make a saved
        # search-only value impossible to validate. Verify it in the new scope
        # without rewriting the user's cell or draft timestamps/version.
        self.definition.values_version += 1
        self.definition.values_snapshot_hash = "4" * 64
        self.account.credential_version += 1
        db.session.commit()
        dictionary_scope = OzonBrandSearch._definition_scope(
            self.draft, self.definition,
        )
        self.assertNotEqual(
            dictionary_scope["scope_hash"], original_scope["scope_hash"],
        )
        renewed = _FakeSearchClient({"result": [{"id": 101, "value": "Acme"}]})
        self.assertFalse(self._prepare(renewed, confirmed=False))
        self.assertEqual(len(renewed.calls), 1)
        self.assertEqual(self._draft_state(), original_state)
        self.assertEqual(
            OzonBrandSearch.value_choices(self.draft, self.definition),
            [{"dictionary_value_id": "101", "value": "Acme"}],
        )
        renewed_validation = MarketplaceDraftService._build_validation_result(
            self.draft,
        )
        self.assertFalse(any(
            error.get("code") == "dictionary_value_out_of_scope"
            for error in renewed_validation["errors"]
        ))
        proof = OzonBrandSearchEvidence.query.filter_by(
            scope_hash=dictionary_scope["scope_hash"],
        ).one()
        self.assertEqual(proof.status, "completed")

        # The same fresh scope uses its cached proof and makes no second read.
        cached = _FakeSearchClient({"result": [{"id": 101, "value": "Acme"}]})
        self.assertFalse(self._prepare(cached, confirmed=False))
        self.assertEqual(cached.calls, [])
        self.assertEqual(self._draft_state(), original_state)

        # A separate schema generation is a separate bounded proof scope.
        self.product_type.attributes_version += 1
        self.product_type.attributes_schema_hash = "5" * 64
        db.session.commit()
        schema_scope = OzonBrandSearch._definition_scope(
            self.draft, self.definition,
        )
        self.assertNotEqual(
            schema_scope["scope_hash"], dictionary_scope["scope_hash"],
        )
        schema_renewed = _FakeSearchClient({
            "result": [{"id": 101, "value": "Acme"}],
        })
        self.assertFalse(self._prepare(schema_renewed, confirmed=False))
        self.assertEqual(len(schema_renewed.calls), 1)
        self.assertEqual(self._draft_state(), original_state)
        self.assertEqual(
            OzonBrandSearch.value_choices(self.draft, self.definition),
            [{"dictionary_value_id": "101", "value": "Acme"}],
        )

    def test_search_only_verify_unknown_result_stays_fenced_in_same_scope(self):
        self._store_brand_cell()
        original_state = self._draft_state()
        unknown = _FakeSearchClient(error=OzonAPIError("unknown"))
        self.assertFalse(self._prepare(unknown, confirmed=False))
        self.assertEqual(len(unknown.calls), 1)
        evidence = OzonBrandSearchEvidence.query.one()
        self.assertEqual(evidence.status, "unavailable")
        self.assertEqual(self._draft_state(), original_state)

        later = _FakeSearchClient({"result": [{"id": 101, "value": "Acme"}]})
        self.assertFalse(self._prepare(later, confirmed=False))
        self.assertEqual(later.calls, [])
        self.assertEqual(OzonBrandSearchEvidence.query.one().status, "unavailable")
        self.assertEqual(self._draft_state(), original_state)

    def test_verify_only_rejects_unrelated_denied_and_local_dictionary_cells(self):
        cases = (
            ("unrelated", "101", "Other", "[]", None),
            ("noncanonical id", "00101", "Acme", "[]", None),
            ("restricted id", "101", "Acme", '["102"]', None),
            ("local exact", "101", "Acme", "[]", (True, "Acme")),
            ("local inactive", "101", "Acme", "[]", (False, "Acme")),
            ("local label changed", "101", "Acme", "[]", (True, "Acme official")),
        )
        for label, value_id, value, restriction, local in cases:
            with self.subTest(case=label):
                self.definition.restriction_value_ids_json = restriction
                self._store_brand_cell(value_id=value_id, value=value)
                local_row = None
                if local is not None:
                    available, official_value = local
                    local_row = MarketplaceAttributeValue(
                        marketplace_id=self.marketplace.id,
                        product_type_id=self.product_type.id,
                        attribute_id=self.definition.id,
                        external_value_id=value_id,
                        value=official_value,
                        value_normalized=OzonReferenceService.normalize_value(
                            official_value,
                        ),
                        is_available=available,
                    )
                    db.session.add(local_row)
                    db.session.commit()
                else:
                    db.session.commit()
                original_state = self._draft_state()
                client = _FakeSearchClient({
                    "result": [{"id": 101, "value": "Acme"}],
                })

                self.assertFalse(self._prepare(client, confirmed=False))
                self.assertEqual(client.calls, [])
                self.assertEqual(OzonBrandSearchEvidence.query.count(), 0)
                self.assertEqual(self._draft_state(), original_state)

                if local_row is not None:
                    db.session.delete(local_row)
                    db.session.commit()

    def test_linked_listing_hides_search_choices(self):
        client = _FakeSearchClient({"result": [{"id": 101, "value": "Acme"}]})
        self.assertTrue(self._prepare(client))
        listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            imported_product_id=self.source.id,
            offer_id=self.draft.offer_id,
            external_product_id="9001",
            sync_fingerprint="6" * 64,
            normalized_status="active",
        )
        db.session.add(listing)
        db.session.commit()
        self.assertEqual(
            OzonBrandSearch.value_choices(self.draft, self.definition),
            [],
        )

    def test_local_rate_gate_restores_expired_receipt_for_safe_renewal(self):
        original = _FakeSearchClient({
            "result": [
                {"id": 101, "value": "Acme"},
                {"id": 102, "value": "ACME"},
            ],
        })
        self.assertFalse(self._prepare(original))
        evidence = OzonBrandSearchEvidence.query.one()
        old_choices = evidence.choices_json
        now = datetime.utcnow()
        evidence.reserved_at = now - timedelta(hours=13)
        evidence.finished_at = now - timedelta(hours=13) + timedelta(seconds=1)
        evidence.expires_at = now - timedelta(seconds=1)
        self.draft.attributes_json = json.dumps([{
            "attribute_id": "31",
            "complex_id": "0",
            "values": [{"dictionary_value_id": "101", "value": "Acme"}],
        }])
        db.session.commit()

        locally_limited = _FakeSearchClient(error=OzonRateLimitError(
            "local budget",
            code="ozon_local_rate_limited",
        ))
        self.assertFalse(self._prepare(locally_limited, confirmed=False))
        db.session.refresh(evidence)
        self.assertEqual(evidence.status, "completed")
        self.assertEqual(evidence.choices_json, old_choices)
        self.assertEqual(evidence.complete_result, True)
        self.assertEqual(len(locally_limited.calls), 1)

    def test_draft_version_change_during_http_never_auto_stages(self):
        def seller_edit():
            MarketplaceDraftService.update_draft(
                seller_id=self.seller.id,
                draft_id=self.draft.id,
                expected_version=self.draft.version,
                patch={"content": {"name": "Seller edited title"}},
            )

        client = _FakeSearchClient(
            {"result": [{"id": 101, "value": "Acme"}]},
            before_return=seller_edit,
        )
        self.assertFalse(self._prepare(client))
        db.session.refresh(self.draft)
        self.assertEqual(self._brand_cells(self.draft), [])
        self.assertEqual(
            json.loads(self.draft.content_json)["name"],
            "Seller edited title",
        )

    def test_current_supplier_revision_drift_blocks_search(self):
        self.supplier_product.content_revision = 8
        db.session.commit()
        client = _FakeSearchClient({"result": [{"id": 101, "value": "Acme"}]})
        self.assertFalse(self._prepare(client))
        self.assertEqual(client.calls, [])
        self.assertEqual(OzonBrandSearchEvidence.query.count(), 0)

    def test_other_product_offer_collision_blocks_search(self):
        from models import ImportedProduct
        other = ImportedProduct(
            seller_id=self.seller.id,
            external_id="other-source",
            external_vendor_code="other-offer",
            source_type=self.supplier.code,
            title="Other source",
        )
        db.session.add(other)
        db.session.flush()
        db.session.add(MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            imported_product_id=other.id,
            offer_id=self.draft.offer_id,
            external_product_id="9002",
            sync_fingerprint="7" * 64,
            normalized_status="active",
        ))
        db.session.commit()
        client = _FakeSearchClient({"result": [{"id": 101, "value": "Acme"}]})
        self.assertFalse(self._prepare(client))
        self.assertEqual(client.calls, [])

    def test_unavailable_scope_does_not_reissue(self):
        client = _FakeSearchClient(error=OzonAPIError("unknown"))
        self.assertFalse(self._prepare(client))
        self.assertEqual(len(client.calls), 1)
        second = _FakeSearchClient({"result": [{"id": 101, "value": "Acme"}]})
        self.assertFalse(self._prepare(second))
        self.assertEqual(second.calls, [])
        self.assertEqual(
            OzonBrandSearchEvidence.query.one().status,
            "unavailable",
        )
