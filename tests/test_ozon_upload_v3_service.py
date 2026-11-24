"""Durable upload acceptance, local preparation, and exact operation ownership."""

from datetime import datetime, timedelta
import json
import time
from unittest.mock import patch
import unittest

from models import (
    BackgroundJob, ImportedProduct, MarketplaceAttributeDefinition,
    MarketplaceAttributeValue, MarketplaceCategoryMapping,
    MarketplaceListingSnapshot, MarketplaceOperation, OzonBulkUploadItem,
    OzonBulkUploadRun, SellerSupplier, Supplier, SupplierProduct, db,
)
from services.marketplace_drafts import MarketplaceDraftService
from services.marketplace_product_links import MarketplaceProductLinkService
from services.marketplace_publications import MarketplacePublicationService
from services.ozon_bulk_upload import (
    OzonBulkUploadConflict, OzonBulkUploadService,
    OzonBulkUploadValidationError,
)
from services.ozon_upload_queue import (
    ItemAdvanceResult, OzonUploadLeaseLost, OzonUploadQueueService,
)
from services.ozon_upload_review import OzonUploadReviewService
from services.ozon_product_import import OzonProductImportContract
from tests.test_marketplace_publications import OzonPublicationFixture


class OzonUploadV3ServiceTest(OzonPublicationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
        )
        self.account._credentials_encrypted = "synthetic-encrypted-value"
        self.account.settings_json = '{"default_vat":"0.22"}'
        db.session.commit()

    def _source(self, key="s" * 24):
        return OzonBulkUploadService.accept_source_prepare(
            seller_id=self.seller.id, account_id=self.account.id,
            imported_product_ids=[self.source.id], request_key=key,
            created_by_user_id=self.user.id,
        )

    def _retry_parent(self, phases, *, key):
        products = [self.source]
        for index in range(1, len(phases)):
            product = ImportedProduct(
                seller_id=self.seller.id,
                external_id=f"retry-source-{index}-{key[:4]}",
                external_vendor_code=f"retry-offer-{index}-{key[:4]}",
                source_type="synthetic",
                title=f"Товар для повтора {index}",
                description="Наблюдаемое описание",
                category="Категория",
            )
            db.session.add(product)
            products.append(product)
        db.session.flush()

        prepared_drafts = {}
        for product, phase in zip(products, phases):
            if phase != "prepared" or product.id == self.source.id:
                continue
            draft = MarketplaceDraftService.create_draft(
                seller_id=self.seller.id,
                account_id=self.account.id,
                imported_product_id=product.id,
                product_type_id=self.product_type.id,
            )
            prepared_drafts[product.id] = draft

        accepted = OzonBulkUploadService.accept_source_prepare(
            seller_id=self.seller.id,
            account_id=self.account.id,
            imported_product_ids=[product.id for product in products],
            request_key=key,
            created_by_user_id=self.user.id,
        )
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).one()
        items = OzonBulkUploadItem.query.filter_by(run_id=run.id).order_by(
            OzonBulkUploadItem.ordinal.asc(),
        ).all()
        run.state = "completed"
        run.next_due_at = None
        for item, phase, product in zip(items, phases, products):
            if phase == "prepared":
                draft = (
                    prepared_drafts.get(product.id)
                    or MarketplaceDraftService.get_draft(
                        seller_id=self.seller.id,
                        draft_id=self.draft.id,
                    )
                )
                item.draft_id = draft.id
                item.prepared_version = draft.version
            item.phase = phase
            item.next_due_at = None
        db.session.commit()
        return accepted, run, items, products

    def _reviewed(self, key="r" * 24):
        return OzonBulkUploadService.accept_reviewed_publish(
            seller_id=self.seller.id, account_id=self.account.id,
            draft_ids=[self.draft.id],
            expected_versions={str(self.draft.id): self.draft.version},
            request_key=key, created_by_user_id=self.user.id,
        )

    def _linked_reviewed_operation(self):
        accepted = self._reviewed()
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).one()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).one()
        claim = OzonUploadQueueService.claim_due_run(
            now=datetime.utcnow(), exclude_run_ids=set(),
        )
        with patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ):
            operation = MarketplacePublicationService.enqueue_reviewed_upload_item(
                seller_id=self.seller.id, account_id=self.account.id,
                draft_id=self.draft.id, expected_version=self.draft.version,
                run_item_id=item.id, lease_token=claim[1],
                created_by_user_id=self.user.id, now=datetime.utcnow(),
            )
        db.session.refresh(item)
        return accepted, run, item, operation, operation.snapshot

    def test_review_recovery_accepts_terminal_failure_without_physical_attempt_only(self):
        accepted, _run, item, operation, _snapshot = self._linked_reviewed_operation()
        operation.status = "failed"
        operation.error_code = "upload_local_preflight_failed"
        operation.attempt_count = 0
        operation.poll_count = 0
        operation.external_task_id = None
        operation.submitted_at = None
        operation.last_polled_at = None
        operation.completed_at = datetime.utcnow()
        db.session.commit()

        document = OzonBulkUploadService.public_document(accepted.job, detail=True)
        self.assertEqual(document["summary"]["active"], 0)
        self.assertEqual(document["review_recovery_draft_ids"], [self.draft.id])

        # An external task identity contradicts the no-attempt branch and must
        # fail closed even when attempt_count remains zero.
        operation.external_task_id = "unexpected-task"
        db.session.commit()
        document = OzonBulkUploadService.public_document(accepted.job, detail=True)
        self.assertEqual(document["review_recovery_draft_ids"], [])

        operation.external_task_id = None
        operation.error_code = "product_update_no_change"
        operation.operation_kind = "product_update"
        db.session.commit()
        document = OzonBulkUploadService.public_document(accepted.job, detail=True)
        self.assertEqual(item.phase, "operation_linked")
        self.assertEqual(document["items"][0]["status"], "already_current")
        self.assertEqual(document["review_recovery_draft_ids"], [])

    def test_review_recovery_requires_exact_settled_task_readback_proof(self):
        accepted, _run, _item, operation, snapshot = self._linked_reviewed_operation()
        now = datetime.utcnow()
        result = {
            "total": 1,
            "aggregate_status": "failed",
            "items": [{
                "offer_id": self.draft.offer_id,
                "product_id": "12345",
                "status": "failed",
                "errors": [],
            }],
        }
        operation.status = "failed"
        operation.error_code = "ozon_import_failed"
        operation.attempt_count = 1
        operation.poll_count = 1
        operation.external_task_id = "synthetic-task-1"
        operation.submitted_at = now - timedelta(seconds=10)
        operation.last_polled_at = now
        operation.completed_at = now
        operation.item_results_json = json.dumps(result["items"])
        snapshot.confirmed_state_json = json.dumps({
            "source": "task_status", "result": result,
            "confirmed_at": now.isoformat(),
        })
        snapshot.confirmed_fingerprint = OzonProductImportContract.fingerprint(result)
        db.session.commit()

        document = OzonBulkUploadService.public_document(accepted.job, detail=True)
        self.assertEqual(document["review_recovery_draft_ids"], [self.draft.id])

        snapshot.confirmed_fingerprint = "0" * 64
        db.session.commit()
        document = OzonBulkUploadService.public_document(accepted.job, detail=True)
        self.assertEqual(document["review_recovery_draft_ids"], [])

        snapshot.confirmed_fingerprint = OzonProductImportContract.fingerprint(result)
        operation.status = "partial"
        db.session.commit()
        document = OzonBulkUploadService.public_document(accepted.job, detail=True)
        self.assertEqual(document["review_recovery_draft_ids"], [])

    def test_source_acceptance_is_local_even_when_publication_disabled(self):
        self.app.config["MARKETPLACE_OZON_PUBLICATION_ENABLED"] = False
        with patch.object(
            MarketplacePublicationService, "_account_adapter_credentials",
            side_effect=AssertionError("provider credentials consulted"),
        ):
            accepted = self._source()
            replay = self._source()
        self.assertFalse(accepted.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(accepted.job.id, replay.job.id)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        self.assertEqual(OzonBulkUploadRun.query.count(), 1)
        self.assertEqual(OzonBulkUploadItem.query.count(), 1)
        document = OzonBulkUploadService.public_document(accepted.job, detail=True)
        self.assertEqual(document["mode"], "source_prepare")
        self.assertEqual(document["items"][0]["phase"], "pending")
        self.assertFalse(document["source_prepare_complete"])

    def test_source_prepare_completion_requires_durable_terminal_and_settled_items(self):
        accepted = self._source(key="source-completion-proof-0001")
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).one()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).one()

        # Terminal-looking item rows alone do not prove the coordinator's
        # durable terminal commit.
        item.phase = "needs_input"
        item.next_due_at = None
        db.session.commit()
        document = OzonBulkUploadService.public_document(accepted.job, detail=True)
        self.assertEqual(document["summary"]["active"], 0)
        self.assertFalse(document["source_prepare_complete"])

        run.state = "completed"
        run.next_due_at = None
        db.session.commit()
        document = OzonBulkUploadService.public_document(accepted.job, detail=True)
        self.assertTrue(document["source_prepare_complete"])

        # A stale or inconsistent completed run with active items remains
        # explicitly incomplete until all item work is settled.
        item.phase = "preparing"
        db.session.commit()
        document = OzonBulkUploadService.public_document(accepted.job, detail=True)
        self.assertGreater(document["summary"]["active"], 0)
        self.assertFalse(document["source_prepare_complete"])

    def test_source_retry_selects_exact_terminal_local_problems_and_replays(self):
        accepted, parent, items, products = self._retry_parent(
            ["needs_input", "failed_local", "excluded", "prepared"],
            key="prepare-retry-parent-001",
        )
        parent_document = OzonBulkUploadService.public_document(
            accepted.job, detail=True,
        )
        self.assertEqual(parent_document["prepare_retry_count"], 2)
        request_key = OzonBulkUploadService.prepare_retry_request_key_for_document(
            seller_id=self.seller.id,
            document=parent_document,
        )
        self.assertRegex(request_key, r"^prepare-retry-v1-[0-9a-f]{64}$")

        first = OzonBulkUploadService.retry_run(
            seller_id=self.seller.id,
            job_uid=accepted.job.job_uid,
            request_key=request_key,
            created_by_user_id=self.user.id,
            canonical_scope=True,
        )
        child = OzonBulkUploadRun.query.filter_by(job_id=first.job.id).one()
        child_items = OzonBulkUploadItem.query.filter_by(
            run_id=child.id,
        ).order_by(OzonBulkUploadItem.ordinal.asc()).all()
        self.assertEqual(child.mode, "source_prepare")
        self.assertEqual(child.parent_prepare_run_id, parent.id)
        self.assertEqual(
            [item.imported_product_id for item in child_items],
            [products[0].id, products[1].id],
        )
        self.assertEqual(MarketplaceOperation.query.count(), 0)

        replay = OzonBulkUploadService.retry_run(
            seller_id=self.seller.id,
            job_uid=accepted.job.job_uid,
            request_key=request_key,
            created_by_user_id=self.user.id,
            canonical_scope=True,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.job.id, first.job.id)
        self.assertEqual(OzonBulkUploadRun.query.count(), 2)
        child_document = OzonBulkUploadService.public_document(
            replay.job, detail=True,
        )
        self.assertEqual(child_document["prepare_retry_count"], 0)
        self.assertEqual(
            child_document["previous_prepare_job_uid"],
            accepted.job.job_uid,
        )

    def test_source_retry_digest_denies_same_count_scope_drift_and_replays_exact_child(self):
        accepted, parent, items, products = self._retry_parent(
            ["needs_input", "prepared"],
            key="prepare-retry-drift-scope-001",
        )
        parent_document = OzonBulkUploadService.public_document(
            accepted.job, detail=True,
        )
        self.assertEqual(parent_document["prepare_retry_count"], 1)
        stale_key = OzonBulkUploadService.prepare_retry_request_key_for_document(
            seller_id=self.seller.id,
            document=parent_document,
        )

        # Keep the count at one but replace the exact eligible product.
        items[0].draft_id = self.draft.id
        items[0].prepared_version = self.draft.version
        items[0].phase = "prepared"
        items[1].phase = "needs_input"
        db.session.commit()
        changed_document = OzonBulkUploadService.public_document(
            accepted.job, detail=True,
        )
        self.assertEqual(changed_document["prepare_retry_count"], 1)
        changed_key = OzonBulkUploadService.prepare_retry_request_key_for_document(
            seller_id=self.seller.id,
            document=changed_document,
        )
        self.assertNotEqual(stale_key, changed_key)
        with self.assertRaises(OzonBulkUploadConflict) as changed:
            OzonBulkUploadService.retry_run(
                seller_id=self.seller.id,
                job_uid=accepted.job.job_uid,
                request_key=stale_key,
                created_by_user_id=self.user.id,
                canonical_scope=True,
            )
        self.assertEqual(
            changed.exception.code,
            "ozon_prepare_retry_scope_changed",
        )
        self.assertEqual(OzonBulkUploadRun.query.count(), 1)

        # The scope shown by a fresh GET accepts exactly the new eligible row.
        first = OzonBulkUploadService.retry_run(
            seller_id=self.seller.id,
            job_uid=accepted.job.job_uid,
            request_key=changed_key,
            created_by_user_id=self.user.id,
            canonical_scope=True,
        )
        child = OzonBulkUploadRun.query.filter_by(job_id=first.job.id).one()
        child_items = OzonBulkUploadItem.query.filter_by(run_id=child.id).all()
        self.assertEqual(
            [item.imported_product_id for item in child_items],
            [products[1].id],
        )

        # A second POST with that key replays its exact accepted child even if
        # the parent scope later changes again.
        items[0].phase = "needs_input"
        items[0].draft_id = None
        items[0].prepared_version = None
        items[1].phase = "prepared"
        items[1].draft_id = self.draft.id
        items[1].prepared_version = self.draft.version
        db.session.commit()
        replay = OzonBulkUploadService.retry_run(
            seller_id=self.seller.id,
            job_uid=accepted.job.job_uid,
            request_key=changed_key,
            created_by_user_id=self.user.id,
            canonical_scope=True,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.job.id, first.job.id)
        self.assertEqual(OzonBulkUploadRun.query.count(), 2)
        replayed_ids = [item.imported_product_id for item in
                        OzonBulkUploadItem.query.filter_by(run_id=child.id).all()]
        self.assertEqual(replayed_ids, [products[1].id])

    def test_source_retry_requires_completed_source_run_and_nonempty_problem_scope(self):
        active = self._source(key="prepare-retry-active-scope-01")
        with self.assertRaises(OzonBulkUploadConflict):
            OzonBulkUploadService.retry_run(
                seller_id=self.seller.id,
                job_uid=active.job.job_uid,
                request_key="legacy-active-retry-key-001",
            )

        completed, _run, _items, _products = self._retry_parent(
            ["prepared"],
            key="prepare-retry-no-target-01",
        )
        with self.assertRaises(OzonBulkUploadValidationError):
            OzonBulkUploadService.retry_run(
                seller_id=self.seller.id,
                job_uid=completed.job.job_uid,
                request_key="legacy-empty-retry-key-001",
            )

        reviewed = self._reviewed(key="prepare-retry-reviewed-01")
        with self.assertRaises(OzonBulkUploadConflict) as wrong_mode:
            OzonBulkUploadService.retry_run(
                seller_id=self.seller.id,
                job_uid=reviewed.job.job_uid,
                request_key="legacy-reviewed-retry-001",
            )
        self.assertEqual(wrong_mode.exception.code, "draft_review_required")

    def test_request_key_conflict_and_account_scoped_readback(self):
        self._source()
        with self.assertRaises(OzonBulkUploadConflict) as caught:
            self._reviewed(key="s" * 24)
        self.assertEqual(caught.exception.code, "upload_request_key_conflict")
        self.assertEqual(
            OzonBulkUploadService.find_by_request_key(
                seller_id=self.seller.id, account_id=self.account.id,
                request_key="s" * 24,
            ).id,
            BackgroundJob.query.first().id,
        )

    def test_local_prepare_commits_draft_and_never_enqueues(self):
        accepted = self._source()
        with patch.object(
            MarketplaceDraftService, "reconcile_observed_category_mappings",
            return_value={"success": True, "code": "observed_category_mapping_reconciled"},
        ), patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ), patch.object(
            MarketplacePublicationService, "enqueue_reviewed_upload_item",
            side_effect=AssertionError("source preparation enqueued a write"),
        ):
            result = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(result["processed_items"], 1)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        self.assertIsNotNone(run.mapping_preflight_at)
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).first()
        self.assertIn(item.phase, {"prepared", "needs_input", "waiting_reference"})
        self.assertIsNone(item.operation_id)

    def _versioned_supplier_pair(self, supplier, *, copied_revision, suffix):
        external_id = f"revision-source-{suffix}"
        seller_offer = f"seller-owned-offer-{suffix}"
        source = ImportedProduct(
            seller_id=self.seller.id,
            supplier_id=supplier.id,
            supplier_content_revision=copied_revision,
            external_id=external_id,
            external_vendor_code=seller_offer,
            source_type=supplier.code,
            title=f"Seller-owned title {suffix}",
            description=f"Seller-owned description {suffix}",
            category="Категория",
            original_data="",
        )
        markers = {
            "title": f"CURRENT_SHARED_TITLE_{suffix}",
            "description": f"CURRENT_SHARED_DESCRIPTION_{suffix}",
            "brand": f"CURRENT_SHARED_BRAND_{suffix}",
            "category": f"CURRENT_SHARED_CATEGORY_{suffix}",
            "country": f"CURRENT_SHARED_COUNTRY_{suffix}",
            "color": f"CURRENT_SHARED_COLOR_{suffix}",
            "characteristic_name": f"CURRENT_SHARED_CHARACTERISTIC_{suffix}",
            "characteristic_value": f"CURRENT_SHARED_VALUE_{suffix}",
        }
        snapshot = {
            "external_id": external_id,
            "vendor_code": seller_offer,
            "title": markers["title"],
            "description": markers["description"],
            "brand": markers["brand"],
            "category": markers["category"],
            "country": markers["country"],
            "colors": [markers["color"]],
            "characteristics": {
                markers["characteristic_name"]: markers["characteristic_value"],
            },
            "observed_supplier_fields_version": 1,
            "observed_supplier_fields": {
                "colors": [markers["color"]],
                "country": markers["country"],
            },
        }
        supplier_product = SupplierProduct(
            supplier_id=supplier.id,
            external_id=external_id,
            vendor_code=seller_offer,
            title=markers["title"],
            description=markers["description"],
            brand=markers["brand"],
            category=markers["category"],
            country=markers["country"],
            colors_json=json.dumps([markers["color"]], ensure_ascii=False),
            characteristics_json=json.dumps([{
                "name": markers["characteristic_name"],
                "value": markers["characteristic_value"],
            }], ensure_ascii=False),
            original_data_json=json.dumps(snapshot, ensure_ascii=False),
            content_revision=8,
        )
        db.session.add_all([source, supplier_product])
        db.session.flush()
        source.supplier_product_id = supplier_product.id
        return source, supplier_product, markers

    def _active_supplier_category_mapping(self, source, supplier):
        identity = MarketplaceDraftService._mapping_identities(source)[-1]
        mapping = MarketplaceCategoryMapping(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            supplier_id=supplier.id,
            product_type_id=self.product_type.id,
            scope_key=identity["scope_key"],
            source_type=identity["source_type"],
            source_category=identity["source_category"],
            source_category_normalized=identity["source_category_normalized"],
            external_category_id=self.product_type.category.external_category_id,
            external_type_id=self.product_type.external_type_id,
            mapping_source="manual",
            mapping_status="active",
            confidence=1.0,
            evidence_json="{}",
        )
        db.session.add(mapping)

    def test_source_prepare_does_not_hydrate_unversioned_or_stale_supplier_copy(self):
        supplier = Supplier(
            name="Revision Evidence Supplier",
            code="revision-evidence-test",
        )
        db.session.add(supplier)
        db.session.flush()
        db.session.add(SellerSupplier(
            seller_id=self.seller.id,
            supplier_id=supplier.id,
            is_active=True,
        ))
        unversioned, _unversioned_source, unversioned_markers = (
            self._versioned_supplier_pair(
                supplier, copied_revision=0, suffix="zero",
            )
        )
        stale, _stale_source, stale_markers = self._versioned_supplier_pair(
            supplier, copied_revision=7, suffix="stale",
        )
        self._active_supplier_category_mapping(unversioned, supplier)
        db.session.commit()

        accepted = OzonBulkUploadService.accept_source_prepare(
            seller_id=self.seller.id,
            account_id=self.account.id,
            imported_product_ids=[unversioned.id, stale.id],
            request_key="stale-copy-source-prepare-01",
            created_by_user_id=self.user.id,
        )
        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            side_effect=AssertionError("local preparation attempted a model call"),
        ) as decisions_pipeline, patch.object(
            MarketplacePublicationService,
            "_account_adapter_credentials",
            side_effect=AssertionError("local preparation consulted provider credentials"),
        ) as provider_credentials, patch.object(
            MarketplacePublicationService,
            "enqueue_reviewed_upload_item",
            side_effect=AssertionError("source preparation enqueued a provider write"),
        ) as provider_enqueue:
            result = OzonBulkUploadService.run_due_preparation()

        self.assertEqual(result["processed_items"], 2)
        decisions_pipeline.assert_not_called()
        provider_credentials.assert_not_called()
        provider_enqueue.assert_not_called()

        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).one()
        items = OzonBulkUploadItem.query.filter_by(run_id=run.id).order_by(
            OzonBulkUploadItem.ordinal.asc(),
        ).all()
        self.assertEqual(len(items), 2)
        self.assertEqual({item.imported_product_id for item in items}, {
            unversioned.id, stale.id,
        })
        source_by_id = {unversioned.id: unversioned, stale.id: stale}
        markers_by_id = {
            unversioned.id: unversioned_markers,
            stale.id: stale_markers,
        }
        for item in items:
            source = source_by_id[item.imported_product_id]
            markers = markers_by_id[item.imported_product_id]
            self.assertIn(
                item.phase,
                {"prepared", "needs_input", "waiting_reference"},
                item.error_code,
            )
            self.assertIsNotNone(item.draft_id)
            self.assertIsNone(item.operation_id)
            draft = MarketplaceDraftService.get_draft(
                seller_id=self.seller.id,
                draft_id=item.draft_id,
            )
            self.assertEqual(draft.imported_product_id, source.id)
            self.assertEqual(draft.seller_id, self.seller.id)
            self.assertEqual(draft.account_id, self.account.id)
            self.assertEqual(json.loads(draft.content_json), {
                "name": source.title,
                "description": source.description,
            })
            self.assertEqual(
                json.loads(draft.source_facts_json)["facts"]["identity"]["title"],
                source.title,
            )
            persisted_material = "\n".join((
                draft.source_facts_json,
                draft.provenance_json,
                draft.content_json,
                draft.attributes_json,
                draft.complex_attributes_json,
                draft.media_json,
                draft.dimensions_json,
                draft.barcodes_json,
            ))
            for marker in markers.values():
                self.assertNotIn(marker, persisted_material)
            self.assertNotIn("colors", json.loads(draft.source_facts_json)["facts"].get(
                "attributes", {},
            ))

        db.session.expire_all()
        persisted_sources = ImportedProduct.query.filter(
            ImportedProduct.id.in_([unversioned.id, stale.id]),
        ).all()
        self.assertEqual({source.supplier_content_revision for source in persisted_sources}, {0, 7})
        self.assertEqual({source.original_data for source in persisted_sources}, {""})
        self.assertTrue(all(source.colors is None for source in persisted_sources))
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_current_supplier_copy_revision_allows_explicit_snapshot_recovery(self):
        from types import SimpleNamespace

        from services.ozon_bulk_repair import OzonBulkRepairService
        from services.supplier_service import hydrate_missing_imported_observed_snapshot

        supplier = Supplier(
            name="Revision Evidence Supplier",
            code="revision-evidence-current-test",
        )
        db.session.add(supplier)
        db.session.flush()
        source, _supplier_product, markers = self._versioned_supplier_pair(
            supplier, copied_revision=8, suffix="current",
        )
        db.session.commit()

        self.assertTrue(hydrate_missing_imported_observed_snapshot(source))
        db.session.commit()
        db.session.refresh(source)
        copied = json.loads(source.original_data)
        self.assertEqual(copied["colors"], [markers["color"]])
        self.assertEqual(copied["country"], markers["country"])
        self.assertEqual(copied["title"], markers["title"])
        self.assertEqual(source.supplier_content_revision, 8)
        evidence = OzonBulkRepairService._source_supplier_evidence(
            SimpleNamespace(
                imported_product=source,
                imported_product_id=source.id,
                seller_id=self.seller.id,
            )
        )
        self.assertEqual(evidence["status"], "confirmed")
        self.assertEqual(evidence["fields"], {
            "colors": [markers["color"]],
            "country": markers["country"],
        })

    def test_reviewed_ready_local_draft_stops_at_prepared_phase(self):
        accepted = self._source(key="p" * 24)
        with patch.object(
            MarketplaceDraftService, "reconcile_observed_category_mappings",
            return_value={"success": True, "code": "observed_category_mapping_reconciled"},
        ), patch.object(
            MarketplaceProductLinkService, "reconcile_account_products",
            return_value={"blocked": {}, "resolved_listing_ids": {}},
        ), patch("services.supplier_service.hydrate_missing_imported_observed_snapshot"), patch.object(
            MarketplaceDraftService, "create_draft", return_value=self.draft,
        ), patch.object(
            MarketplaceDraftService, "rebase_source_defaults", return_value=self.draft,
        ), patch.object(
            MarketplaceDraftService, "apply_reference_defaults", return_value=self.draft,
        ), patch.object(
            MarketplaceDraftService, "apply_account_defaults", return_value=self.draft,
        ), patch.object(
            MarketplaceDraftService, "validate_draft", return_value=self.draft,
        ) as validate_draft, patch.object(
            MarketplacePublicationService, "enqueue_reviewed_upload_item",
            side_effect=AssertionError("local preparation submitted a write"),
        ):
            result = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(result["prepared"], 1)
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).first()
        self.assertEqual(item.phase, "prepared")
        self.assertEqual(item.draft_id, self.draft.id)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        validate_draft.assert_called_once()
        self.assertIs(validate_draft.call_args.kwargs["stage_automatic_cleanup"], True)

    def test_normal_preparation_stages_and_reviews_exact_4543_cleanup(self):
        self._assert_preparation_reviews_exact_4543_cleanup(local_row=True)

    def test_normal_preparation_stages_and_reviews_inherited_4543_cleanup(self):
        self._assert_preparation_reviews_exact_4543_cleanup(local_row=False)

    def _assert_preparation_reviews_exact_4543_cleanup(self, *, local_row):
        now = datetime.utcnow()
        values = [(str(4543000 + index), f"Назначение {index}") for index in range(1, 6)]
        definition = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="4543",
            name="Назначение товара 18+",
            data_type="String",
            is_required=False,
            max_value_count=4,
            is_collection=True,
            dictionary_id="dictionary-4543",
            is_available=True,
            is_enabled=True,
            last_seen_at=now,
            values_synced_at=now,
            values_sync_status="success",
            values_snapshot_hash="values-4543",
            values_version=1,
            values_count=len(values),
        )
        db.session.add(definition)
        db.session.flush()
        for value_id, label in values:
            db.session.add(MarketplaceAttributeValue(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                attribute_id=definition.id,
                external_value_id=value_id,
                value=label,
                value_normalized=label.casefold(),
                is_available=True,
                last_seen_at=now,
            ))
        payload = self.prior_payload()
        payload["items"][0]["attributes"].append({
            "id": "4543",
            "complex_id": None,
            "values": [{
                "dictionary_value_id": value_id,
                "value": label,
            } for value_id, label in values],
        })
        listing = self.attach_listing(payload)
        before_listing_attributes = listing.attributes_json
        try:
            MarketplaceDraftService._listing_projection_state(listing)
        except Exception as exc:
            self.fail(f"synthetic listing projection failed: {exc!r}; cause={exc.__cause__!r}")
        self.product_type.attributes_count += 1
        draft_attributes = [{
            "attribute_id": "4543",
            "complex_id": "0",
            "values": [{
                "dictionary_value_id": value_id,
                "value": label,
            } for value_id, label in values],
        }] if local_row else []
        self.draft.attributes_json = json.dumps(draft_attributes, ensure_ascii=False)
        db.session.commit()

        accepted = self._source(key="a" * 24)
        with patch.object(
            MarketplaceDraftService, "reconcile_observed_category_mappings",
            return_value={"success": True, "code": "observed_category_mapping_reconciled"},
        ), patch(
            "services.supplier_service.hydrate_missing_imported_observed_snapshot",
            return_value=False,
        ):
            result = OzonBulkUploadService.run_due_preparation()

        self.assertEqual(result["processed_items"], 1)
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).one()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).one()
        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id,
            draft_id=item.draft_id,
        )
        self.assertEqual(item.phase, "prepared", f"{item.error_code}: {item.error_message}")
        self.assertEqual(item.prepared_version, draft.version)
        self.assertEqual(json.loads(draft.attributes_json), [])
        self.assertEqual(json.loads(draft.attribute_removals_json), [{
            "attribute_id": "4543",
            "complex_id": "0",
        }])
        summary = json.loads(draft.validation_result_json)["automatic_attribute_cleanup"]
        self.assertEqual(summary["attribute_name"], "Назначение товара 18+")
        self.assertEqual(summary["before_count"], 5)
        self.assertEqual(summary["max_value_count"], 4)
        self.assertEqual(summary["action"], "omit_entire_attribute")
        self.assertEqual(listing.attributes_json, before_listing_attributes)
        self.assertEqual(MarketplaceOperation.query.count(), 0)

        review = OzonUploadReviewService.document(
            seller_id=self.seller.id,
            account_id=self.account.id,
            draft_ids=[draft.id],
        )
        row = review["items"][0]
        self.assertEqual(row["version"], item.prepared_version)
        self.assertEqual(row["automatic_attribute_cleanup"], summary)

        # A confirmation tied to this review cannot enqueue after any version change.
        reviewed_version = draft.version
        draft.version += 1
        db.session.commit()
        with self.assertRaises(OzonBulkUploadConflict):
            OzonBulkUploadService.accept_reviewed_publish(
                seller_id=self.seller.id,
                account_id=self.account.id,
                draft_ids=[draft.id],
                expected_versions={str(draft.id): reviewed_version},
                request_key="stale-cleanup-confirm-0123456789",
                created_by_user_id=self.user.id,
            )
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        self.assertEqual(
            OzonBulkUploadRun.query.filter_by(mode="reviewed_drafts").count(),
            0,
        )

    def test_reviewed_enqueue_links_operation_and_snapshot_atomically(self):
        accepted = self._reviewed()
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).first()
        claim = OzonUploadQueueService.claim_due_run(
            now=datetime.utcnow(), exclude_run_ids=set(),
        )
        self.assertEqual(claim[0], run.id)
        with patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ):
            operation = MarketplacePublicationService.enqueue_reviewed_upload_item(
                seller_id=self.seller.id, account_id=self.account.id,
                draft_id=self.draft.id, expected_version=self.draft.version,
                run_item_id=item.id, lease_token=claim[1],
                created_by_user_id=self.user.id, now=datetime.utcnow(),
            )
        self.assertEqual(operation.status, "queued")
        self.assertEqual(MarketplaceOperation.query.count(), 1)
        self.assertEqual(MarketplaceListingSnapshot.query.count(), 1)
        db.session.expire_all()
        linked = OzonBulkUploadItem.query.filter_by(id=item.id).first()
        self.assertEqual(linked.operation_id, operation.id)
        self.assertEqual(linked.phase, "operation_linked")

    def test_stale_lease_cannot_link_operation(self):
        accepted = self._reviewed()
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).first()
        run.lease_token = "x" * 32
        run.lease_until = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        with patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ):
            with self.assertRaises(Exception):
                MarketplacePublicationService.enqueue_reviewed_upload_item(
                    seller_id=self.seller.id, account_id=self.account.id,
                    draft_id=self.draft.id, expected_version=self.draft.version,
                    run_item_id=item.id, lease_token="x" * 32,
                    created_by_user_id=self.user.id, now=datetime.utcnow(),
                )
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        self.assertEqual(MarketplaceListingSnapshot.query.count(), 0)

    def test_reviewed_version_drift_never_creates_operation(self):
        accepted = self._reviewed()
        self.draft.version += 1
        db.session.commit()
        result = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(result["needs_input"], 1)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).first()
        self.assertEqual(item.phase, "needs_input")

    def test_publication_flag_disable_after_acceptance_defers_enqueue(self):
        accepted = self._reviewed(key="f" * 24)
        self.app.config["MARKETPLACE_OZON_PUBLICATION_ENABLED"] = False
        result = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(result["waiting"], 1)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).first()
        self.assertEqual(item.phase, "reviewed")
        self.assertGreater(item.next_due_at, datetime.utcnow())

    def test_exception_after_operation_flush_rolls_back_item_and_operation(self):
        accepted = self._reviewed()
        original = MarketplacePublicationService._create_operation

        def interrupted(**kwargs):
            original(**kwargs)
            raise RuntimeError("simulated process interruption before item link")

        with patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ), patch.object(
            MarketplacePublicationService, "_create_operation",
            side_effect=interrupted,
        ):
            result = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        self.assertEqual(MarketplaceListingSnapshot.query.count(), 0)
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).first()
        self.assertIsNone(item.operation_id)
        self.assertEqual(item.phase, "reviewed")

    def test_unversioned_payload_drift_before_writer_claim_fails_closed(self):
        accepted = self._reviewed()
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).first()
        claim = OzonUploadQueueService.claim_due_run(
            now=datetime.utcnow(), exclude_run_ids=set(),
        )
        original = MarketplacePublicationService._publication_payload
        calls = 0

        def drift(**kwargs):
            nonlocal calls
            calls += 1
            result = original(**kwargs)
            if calls == 1:
                content = json.loads(self.draft.content_json)
                content["name"] = "Изменено без новой версии"
                self.draft.content_json = json.dumps(content, ensure_ascii=False)
                db.session.commit()
            return result

        with patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ), patch.object(
            MarketplacePublicationService, "_publication_payload",
            side_effect=drift,
        ):
            with self.assertRaises(Exception):
                MarketplacePublicationService.enqueue_reviewed_upload_item(
                    seller_id=self.seller.id, account_id=self.account.id,
                    draft_id=self.draft.id, expected_version=self.draft.version,
                    run_item_id=item.id, lease_token=claim[1],
                    created_by_user_id=self.user.id, now=datetime.utcnow(),
                )
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        self.assertEqual(MarketplaceListingSnapshot.query.count(), 0)
        db.session.expire_all()
        self.assertIsNone(db.session.get(OzonBulkUploadItem, item.id).operation_id)

    def test_expired_lease_is_fenced_and_new_claim_can_resume(self):
        accepted = self._source()
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).first()
        first = OzonUploadQueueService.claim_due_run(
            now=datetime.utcnow(), exclude_run_ids=set(),
        )
        self.assertEqual(first[0], run.id)
        self.assertIsNone(OzonUploadQueueService.claim_due_run(
            now=datetime.utcnow(), exclude_run_ids=set(),
        ))
        run.lease_until = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        second = OzonUploadQueueService.claim_due_run(
            now=datetime.utcnow(), exclude_run_ids=set(),
        )
        self.assertEqual(second[0], run.id)
        self.assertNotEqual(first[1], second[1])
        with self.assertRaises(OzonUploadLeaseLost):
            OzonUploadQueueService.transition_item(
                run_id=run.id, item_id=item.id, lease_token=first[1],
                now=datetime.utcnow(), phase="needs_input",
            )
        OzonUploadQueueService.transition_item(
            run_id=run.id, item_id=item.id, lease_token=second[1],
            now=datetime.utcnow(), phase="needs_input",
        )
        self.assertEqual(db.session.get(OzonBulkUploadItem, item.id).phase, "needs_input")

    def test_twenty_waiting_runs_do_not_hide_run_twenty_one(self):
        runs = []
        for number in range(21):
            accepted = self._source(key=f"k{number:023d}")
            run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
            run.mapping_preflight_at = datetime.utcnow()
            runs.append(run.id)
        db.session.commit()

        def wait_one(
            *, run_id, item_id, lease_token, now=None,
            deadline_monotonic=None,
        ):
            self.assertIs(type(deadline_monotonic), float)
            self.assertGreater(deadline_monotonic, time.monotonic())
            due = datetime.utcnow() + timedelta(minutes=5)
            OzonUploadQueueService.transition_item(
                run_id=run_id, item_id=item_id, lease_token=lease_token,
                now=datetime.utcnow(), phase="waiting_reference",
                next_due_at=due,
            )
            return ItemAdvanceResult(True, "waiting_reference", "waiting", next_due_at=due)

        with patch.object(OzonUploadQueueService, "advance_item", side_effect=wait_one):
            first = OzonBulkUploadService.run_due_preparation(
                run_limit=20, item_limit=20,
            )
            second = OzonBulkUploadService.run_due_preparation(
                run_limit=20, item_limit=20,
            )
        self.assertEqual(first["processed_items"], 20)
        self.assertEqual(second["processed_items"], 1)
        last = OzonBulkUploadItem.query.filter_by(run_id=runs[-1]).first()
        self.assertEqual(last.phase, "waiting_reference")

    def test_twenty_large_ready_runs_do_not_hide_run_twenty_one(self):
        second_product = ImportedProduct(
            seller_id=self.seller.id, external_id="second-ready-source",
            external_vendor_code="second-ready-offer", source_type="synthetic",
            title="Second local product", category="Категория",
        )
        db.session.add(second_product)
        db.session.commit()
        runs = []
        for number in range(21):
            accepted = OzonBulkUploadService.accept_source_prepare(
                seller_id=self.seller.id, account_id=self.account.id,
                imported_product_ids=(
                    [self.source.id, second_product.id]
                    if number < 20 else [self.source.id]
                ),
                request_key=f"q{number:023d}",
            )
            run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
            run.mapping_preflight_at = datetime.utcnow()
            runs.append(run.id)
        db.session.commit()

        def finish_one(
            *, run_id, item_id, lease_token, now=None,
            deadline_monotonic=None,
        ):
            self.assertIs(type(deadline_monotonic), float)
            self.assertGreater(deadline_monotonic, time.monotonic())
            OzonUploadQueueService.transition_item(
                run_id=run_id, item_id=item_id, lease_token=lease_token,
                now=datetime.utcnow(), phase="needs_input",
            )
            return ItemAdvanceResult(True, "needs_input", "needs_input")

        with patch.object(OzonUploadQueueService, "advance_item", side_effect=finish_one):
            first = OzonBulkUploadService.run_due_preparation(
                run_limit=20, item_limit=20,
            )
            second = OzonBulkUploadService.run_due_preparation(
                run_limit=1, item_limit=1,
            )
        self.assertEqual(first["processed_items"], 20)
        self.assertEqual(second["processed_items"], 1)
        self.assertEqual(
            OzonBulkUploadItem.query.filter_by(
                run_id=runs[-1], phase="needs_input",
            ).count(), 1,
        )

    def test_exact_two_hundred_acceptance_is_bounded_and_local(self):
        products = [self.source]
        for number in range(1, 200):
            product = ImportedProduct(
                seller_id=self.seller.id,
                external_id=f"bulk-{number}",
                external_vendor_code=f"bulk-offer-{number}",
                source_type="synthetic", title=f"Local product {number}",
                category="Категория",
            )
            db.session.add(product)
            products.append(product)
        db.session.commit()
        with patch.object(
            MarketplacePublicationService, "enqueue_reviewed_upload_item",
            side_effect=AssertionError("source acceptance created operation"),
        ):
            accepted = OzonBulkUploadService.accept_source_prepare(
                seller_id=self.seller.id, account_id=self.account.id,
                imported_product_ids=[product.id for product in products],
                request_key="z" * 24,
                created_by_user_id=self.user.id,
            )
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        self.assertEqual(OzonBulkUploadItem.query.filter_by(run_id=run.id).count(), 200)
        self.assertLess(len(accepted.job.progress_data.encode("utf-8")), 512 * 1024)
        self.assertEqual(MarketplaceOperation.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
