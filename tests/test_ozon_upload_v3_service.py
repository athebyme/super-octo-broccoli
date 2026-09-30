"""Durable upload acceptance, local preparation, and exact operation ownership."""

from datetime import datetime, timedelta
import json
from unittest.mock import patch
import unittest

from models import (
    BackgroundJob, ImportedProduct, MarketplaceListingSnapshot, MarketplaceOperation,
    OzonBulkUploadItem, OzonBulkUploadRun, db,
)
from services.marketplace_drafts import MarketplaceDraftService
from services.marketplace_product_links import MarketplaceProductLinkService
from services.marketplace_publications import MarketplacePublicationService
from services.ozon_bulk_upload import (
    OzonBulkUploadConflict, OzonBulkUploadService,
)
from services.ozon_upload_queue import (
    ItemAdvanceResult, OzonUploadLeaseLost, OzonUploadQueueService,
)
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

    def _reviewed(self, key="r" * 24):
        return OzonBulkUploadService.accept_reviewed_publish(
            seller_id=self.seller.id, account_id=self.account.id,
            draft_ids=[self.draft.id],
            expected_versions={str(self.draft.id): self.draft.version},
            request_key=key, created_by_user_id=self.user.id,
        )

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
        ), patch.object(
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

        def wait_one(*, run_id, item_id, lease_token, now=None):
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

        def finish_one(*, run_id, item_id, lease_token, now=None):
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
