"""Exact listing fences for orphaned and queued Ozon catalog operations."""

from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from models import (
    MarketplaceListing,
    MarketplaceListingSnapshot,
    MarketplaceOperation,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_draft_editor import MarketplaceDraftEditor
from services.marketplace_drafts import MarketplaceDraftConflict, MarketplaceDraftService
from services.marketplace_publications import (
    MarketplacePublicationConflict,
    MarketplacePublicationService,
)
from services.ozon_bulk_upload import OzonBulkUploadService
from services.ozon_upload_queue import OzonUploadQueueService
from services.ozon_upload_review import OzonUploadReviewService
from services.ozon_catalog_operation_fence import (
    active_catalog_listing_operation,
    queued_catalog_listing_blocker,
)
import services.marketplace_publications as publication_module
from tests.test_marketplace_draft_editor import run_node
from tests.test_marketplace_publications import (
    OzonPublicationFixture,
    SYNTHETIC_CREDENTIALS,
    SyntheticFullStateAdapter,
)


class OzonCatalogOperationFenceTest(OzonPublicationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
        )

    def validation_result(self):
        result = super().validation_result()
        result.setdefault("validated_at", datetime.utcnow().isoformat())
        return result

    def _operation(self, *, listing_id, status="uncertain", kind="product_update",
                   seller_id=None, marketplace_id=None, account_id=None,
                   draft_id=None, parent_operation_id=None, attempt_count=1,
                   key="listing-fence-op-0001"):
        row = MarketplaceOperation(
            seller_id=seller_id if seller_id is not None else self.seller.id,
            marketplace_id=(marketplace_id if marketplace_id is not None
                            else self.marketplace.id),
            account_id=account_id if account_id is not None else self.account.id,
            draft_id=draft_id,
            listing_id=listing_id,
            parent_operation_id=parent_operation_id,
            operation_kind=kind,
            status=status,
            idempotency_key=key,
            request_fingerprint="f" * 64,
            contract_version="synthetic-fence-v1",
            request_summary_json=json.dumps({"offer_id": "safe-offer"}),
            quota_snapshot_json="{}",
            provider_request_ids_json="[]",
            item_results_json="[]",
            attempt_count=attempt_count,
            next_poll_at=(datetime.utcnow() + timedelta(minutes=1)
                          if status in {"queued", "submitted", "polling"} else None),
        )
        db.session.add(row)
        db.session.commit()
        return row

    def _parent_with_available_rollback(self, listing):
        parent = self._operation(
            listing_id=listing.id,
            status="succeeded",
            kind="product_import",
            draft_id=None,
            attempt_count=1,
            key="listing-fence-parent-success-0001",
        )
        snapshot = MarketplaceListingSnapshot(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            operation_id=parent.id,
            listing_id=listing.id,
            snapshot_kind="product_import",
            source_fingerprint="a" * 64,
            submitted_fingerprint="b" * 64,
            before_state_json="{}",
            submitted_state_json="{}",
            confirmed_state_json="{}",
            rollback_state_json=json.dumps({
                "product_id": "987654",
                "offer_id": listing.offer_id,
                "expected_live_fingerprint": "b" * 64,
            }),
            rollback_status="available",
        )
        db.session.add(snapshot)
        db.session.commit()
        return parent, snapshot

    def _linked_listing(self):
        listing = self.attach_listing(self.prior_payload())
        return listing

    def _reviewed_orphan(self, listing):
        operation = self._operation(
            listing_id=listing.id,
            status="uncertain",
            draft_id=None,
            attempt_count=1,
        )
        return operation

    def test_orphan_uncertain_blocks_editor_review_edit_and_v3_enqueue(self):
        listing = self._linked_listing()
        orphan = self._reviewed_orphan(listing)

        with patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ):
            editor = MarketplaceDraftEditor.document(
                seller_id=self.seller.id, draft_id=self.draft.id,
            )
            self.assertEqual(editor["active_operation_id"], orphan.id)
            self.assertEqual(
                next(op for op in editor["operations"] if op["id"] == orphan.id)["status"],
                "uncertain",
            )
            reviewed = OzonUploadReviewService.document(
                seller_id=self.seller.id,
                account_id=self.account.id,
                draft_ids=[self.draft.id],
            )
        item = reviewed["items"][0]
        self.assertFalse(item["selectable"])
        self.assertEqual(item["active_operation_id"], orphan.id)
        self.assertIn("already_in_progress", {error["code"] for error in item["errors"]})

        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.seller.id,
                draft_id=self.draft.id,
                expected_version=self.draft.version,
                patch={"offer_id": "changed-offer"},
            )

        # The same backend DTO field drives the editor's existing send lock.
        run_node(f"""
page.data.active_operation_id={orphan.id};
page.data.operations=[{{id:{orphan.id},status:'uncertain',next_poll_at:null}}];
assert.equal(page.activeOperationNotice.label,'Нужна сверка');
assert.equal(page.locked,true);assert.equal(page.canPublish,false);
""")

        self.account._credentials_encrypted = "synthetic-encrypted-value"
        self.account.settings_json = '{"default_vat":"0.22"}'
        db.session.commit()
        accepted = OzonBulkUploadService.accept_reviewed_publish(
            seller_id=self.seller.id,
            account_id=self.account.id,
            draft_ids=[self.draft.id],
            expected_versions={str(self.draft.id): self.draft.version},
            request_key="catalog-fence-review-0001",
            created_by_user_id=self.user.id,
        )
        upload_run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        queued_item = OzonBulkUploadItem.query.filter_by(run_id=upload_run.id).first()
        claim = OzonUploadQueueService.claim_due_run(
            now=datetime.utcnow(), exclude_run_ids=set(),
        )
        self.assertEqual(claim[0], upload_run.id)
        before_ops = MarketplaceOperation.query.count()
        before_snapshots = MarketplaceListingSnapshot.query.count()
        with patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ):
            with self.assertRaises(MarketplacePublicationConflict) as caught:
                MarketplacePublicationService.enqueue_reviewed_upload_item(
                    seller_id=self.seller.id,
                    account_id=self.account.id,
                    draft_id=self.draft.id,
                    expected_version=self.draft.version,
                    run_item_id=queued_item.id,
                    lease_token=claim[1],
                    created_by_user_id=self.user.id,
                    now=datetime.utcnow(),
                )
        self.assertEqual(caught.exception.code, "already_in_progress")
        self.assertEqual(MarketplaceOperation.query.count(), before_ops)
        self.assertEqual(
            MarketplaceListingSnapshot.query.count(),
            before_snapshots,
        )
        db.session.expire_all()
        self.assertIsNone(db.session.get(type(queued_item), queued_item.id).operation_id)

    def test_v3_listing_fence_rechecks_after_account_writer_lock(self):
        listing = self._linked_listing()
        self.account._credentials_encrypted = "synthetic-encrypted-value"
        self.account.settings_json = '{"default_vat":"0.22"}'
        db.session.commit()
        accepted = OzonBulkUploadService.accept_reviewed_publish(
            seller_id=self.seller.id,
            account_id=self.account.id,
            draft_ids=[self.draft.id],
            expected_versions={str(self.draft.id): self.draft.version},
            request_key="catalog-fence-race-00001",
            created_by_user_id=self.user.id,
        )
        upload_run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).first()
        queued_item = OzonBulkUploadItem.query.filter_by(run_id=upload_run.id).first()
        claim = OzonUploadQueueService.claim_due_run(
            now=datetime.utcnow(), exclude_run_ids=set(),
        )
        original = publication_module.active_catalog_listing_operation
        calls = 0

        def appear_after_preflight(**scope):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.assertIsNone(original(**scope))
                self._operation(
                    listing_id=listing.id,
                    status="uncertain",
                    draft_id=None,
                    attempt_count=1,
                    key="catalog-fence-race-orphan-0001",
                )
                return None
            return original(**scope)

        before_snapshots = MarketplaceListingSnapshot.query.count()
        with patch(
            "services.marketplace_publications.active_catalog_listing_operation",
            side_effect=appear_after_preflight,
        ), patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ):
            with self.assertRaises(MarketplacePublicationConflict) as caught:
                MarketplacePublicationService.enqueue_reviewed_upload_item(
                    seller_id=self.seller.id,
                    account_id=self.account.id,
                    draft_id=self.draft.id,
                    expected_version=self.draft.version,
                    run_item_id=queued_item.id,
                    lease_token=claim[1],
                    created_by_user_id=self.user.id,
                    now=datetime.utcnow(),
                )
        self.assertEqual(caught.exception.code, "already_in_progress")
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(MarketplaceOperation.query.count(), 1)
        self.assertEqual(MarketplaceListingSnapshot.query.count(), before_snapshots)
        db.session.expire_all()
        self.assertIsNone(db.session.get(OzonBulkUploadItem, queued_item.id).operation_id)

    def test_new_update_rejected_but_exact_idempotent_replay_is_preserved(self):
        listing = self._linked_listing()
        orphan = self._reviewed_orphan(listing)
        before_ops = MarketplaceOperation.query.count()

        adapter = SyntheticFullStateAdapter(self.prior_payload())
        with patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ), patch.object(
            MarketplacePublicationService, "_submit",
            side_effect=AssertionError("must reject before any provider read/write"),
        ):
            with self.assertRaises(MarketplacePublicationConflict):
                MarketplacePublicationService.start_update(
                    seller_id=self.seller.id,
                    draft_id=self.draft.id,
                    expected_version=self.expected_version,
                    idempotency_key="listing-fence-new-update-0001",
                    created_by_user_id=self.user.id,
                    adapter=adapter,
                    credentials=SYNTHETIC_CREDENTIALS,
                )
        self.assertEqual(MarketplaceOperation.query.count(), before_ops)

        # Create an older idempotent operation before the orphan. Its exact
        # replay must still resolve before the broader listing fence.
        db.session.delete(orphan)
        db.session.commit()
        first_adapter = SyntheticFullStateAdapter(self.prior_payload())
        with patch.object(
            MarketplaceDraftService, "_build_validation_result",
            side_effect=lambda draft: self.validation_result(),
        ), patch.object(
            MarketplacePublicationService, "_submit",
            side_effect=lambda operation, **kwargs: operation,
        ):
            original = MarketplacePublicationService.start_update(
                seller_id=self.seller.id,
                draft_id=self.draft.id,
                expected_version=self.expected_version,
                idempotency_key="listing-fence-replay-0001",
                created_by_user_id=self.user.id,
                adapter=first_adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        other = self._operation(
            listing_id=listing.id,
            status="uncertain",
            draft_id=None,
            attempt_count=1,
            key="listing-fence-other-op-0001",
        )
        with patch.object(
            MarketplacePublicationService, "_submit",
            side_effect=AssertionError("idempotent replay must not resubmit"),
        ):
            replay = MarketplacePublicationService.start_update(
                seller_id=self.seller.id,
                draft_id=self.draft.id,
                expected_version=self.expected_version,
                idempotency_key="listing-fence-replay-0001",
                created_by_user_id=self.user.id,
                adapter=first_adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        self.assertEqual(replay.id, original.id)
        self.assertNotEqual(replay.id, other.id)

    def test_physical_update_and_archive_fences_stop_before_provider_or_attempt(self):
        listing = self._linked_listing()
        self._reviewed_orphan(listing)
        now = datetime.utcnow()
        update = self._operation(
            listing_id=listing.id,
            status="queued",
            kind="product_update",
            draft_id=None,
            attempt_count=0,
            key="listing-fence-queued-update-0001",
        )
        archive = self._operation(
            listing_id=listing.id,
            status="queued",
            kind="product_import_rollback",
            draft_id=None,
            attempt_count=0,
            key="listing-fence-queued-archive-0001",
        )
        direct_update = self._operation(
            listing_id=listing.id,
            status="queued",
            kind="product_update",
            draft_id=None,
            attempt_count=0,
            key="listing-fence-direct-update-0001",
        )
        direct_result = MarketplacePublicationService._submit(
            direct_update,
            adapter=object(),
            credentials=SYNTHETIC_CREDENTIALS,
            now=now,
        )
        self.assertEqual(direct_result.status, "failed")
        self.assertEqual(direct_result.error_code, "listing_operation_in_progress")
        self.assertEqual(direct_result.attempt_count, 0)
        with patch.object(
            MarketplacePublicationService, "_account_adapter_credentials",
            side_effect=AssertionError("listing fence must run before provider resolution"),
        ):
            result = MarketplacePublicationService.poll_operation(
                seller_id=self.seller.id,
                operation_id=update.id,
                now=now,
                allow_submission=True,
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "listing_operation_in_progress")
        self.assertEqual(result.attempt_count, 0)
        self.assertIn("сверьте", result.error_message.lower())

        direct = MarketplacePublicationService._submit_archive(
            archive,
            adapter=object(),
            credentials=SYNTHETIC_CREDENTIALS,
            now=now,
        )
        self.assertEqual(direct.status, "failed")
        self.assertEqual(direct.error_code, "listing_operation_in_progress")
        self.assertEqual(direct.attempt_count, 0)
        self.assertEqual(MarketplaceListingSnapshot.query.count(), 0)

        attempted = self._operation(
            listing_id=listing.id,
            status="queued",
            kind="product_update",
            draft_id=None,
            attempt_count=1,
            key="listing-fence-attempted-0001",
        )
        self.assertFalse(MarketplacePublicationService._stop_conflicting_listing_write(
            attempted, now=now,
        ))
        db.session.refresh(attempted)
        self.assertEqual(attempted.status, "queued")
        self.assertEqual(attempted.attempt_count, 1)

    def test_archive_creation_keeps_parent_available_and_legacy_queue_stays_failed(self):
        listing = self._linked_listing()
        blocker = self._reviewed_orphan(listing)
        parent, snapshot = self._parent_with_available_rollback(listing)
        before = MarketplaceOperation.query.count()
        with self.assertRaises(MarketplacePublicationConflict):
            MarketplacePublicationService._create_archive_operation(
                parent=parent,
                idempotency_key="listing-fence-archive-create-0001",
                created_by_user_id=self.user.id,
                now=datetime.utcnow(),
            )
        self.assertEqual(MarketplaceOperation.query.count(), before)
        db.session.refresh(snapshot)
        self.assertEqual(snapshot.rollback_status, "available")
        self.assertEqual(blocker.status, "uncertain")

        # A pre-existing queued rollback from before this fence cannot be
        # resubmitted after another operation became active. It is stopped
        # honestly and is not made available for blind automatic replay.
        legacy = self._operation(
            listing_id=listing.id,
            status="queued",
            kind="product_import_rollback",
            parent_operation_id=parent.id,
            draft_id=None,
            attempt_count=0,
            key="listing-fence-legacy-archive-0001",
        )
        result = MarketplacePublicationService._submit_archive(
            legacy,
            adapter=object(),
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime.utcnow(),
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "listing_operation_in_progress")
        self.assertEqual(result.attempt_count, 0)
        db.session.refresh(snapshot)
        self.assertEqual(snapshot.rollback_status, "failed")
        self.assertEqual(snapshot.rollback_error_code, "listing_operation_in_progress")

    def test_queued_order_and_exact_scope_exclude_foreign_terminal_and_commercial_ops(self):
        listing = self._linked_listing()
        older = self._operation(
            listing_id=listing.id, status="queued", draft_id=None,
            attempt_count=0, key="listing-fence-older-queued-0001",
        )
        newer = self._operation(
            listing_id=listing.id, status="queued", draft_id=None,
            attempt_count=0, key="listing-fence-newer-queued-0001",
        )
        self.assertIsNone(queued_catalog_listing_blocker(older))
        self.assertEqual(queued_catalog_listing_blocker(newer).id, older.id)

        self.assertEqual(
            active_catalog_listing_operation(
                seller_id=self.seller.id,
                marketplace_id=self.marketplace.id,
                account_id=self.account.id,
                listing_id=listing.id,
            ).id,
            older.id,
        )
        db.session.rollback()
        # Once the catalog operations are terminal, exact listing scope is clear.
        older.status = "failed"
        newer.status = "failed"
        db.session.commit()
        self.assertIsNone(active_catalog_listing_operation(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            listing_id=listing.id,
        ))

        commercial = self._operation(
            listing_id=listing.id, status="uncertain", kind="price_update",
            draft_id=None, attempt_count=1, key="listing-fence-price-0001",
        )
        self.assertIsNone(active_catalog_listing_operation(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            listing_id=listing.id,
        ))
        self.assertEqual(commercial.operation_kind, "price_update")

        foreign_account = SellerMarketplaceAccount(
            seller_id=self.foreign_seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="foreign-synthetic-client",
            label="Foreign Synthetic Ozon",
            is_active=True,
            connection_status="connected",
        )
        other_listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            offer_id="other-safe-offer",
            external_product_id="654321",
            sync_fingerprint="c" * 64,
        )
        db.session.add_all([foreign_account, other_listing])
        db.session.flush()
        db.session.add_all([
            MarketplaceOperation(
                seller_id=self.foreign_seller.id,
                marketplace_id=self.marketplace.id,
                account_id=foreign_account.id,
                listing_id=listing.id,
                operation_kind="product_update",
                status="uncertain",
                idempotency_key="foreign-owner-operation-0001",
                request_fingerprint="e" * 64,
                contract_version="synthetic-fence-v1",
                request_summary_json="{}",
            ),
            MarketplaceOperation(
                seller_id=self.seller.id,
                marketplace_id=self.marketplace.id,
                account_id=foreign_account.id,
                listing_id=listing.id,
                operation_kind="product_update",
                status="uncertain",
                idempotency_key="foreign-account-operation-01",
                request_fingerprint="d" * 64,
                contract_version="synthetic-fence-v1",
                request_summary_json="{}",
            ),
            MarketplaceOperation(
                seller_id=self.seller.id,
                marketplace_id=self.marketplace.id,
                account_id=self.account.id,
                listing_id=other_listing.id,
                operation_kind="product_update",
                status="uncertain",
                idempotency_key="foreign-listing-operation-01",
                request_fingerprint="b" * 64,
                contract_version="synthetic-fence-v1",
                request_summary_json="{}",
            ),
        ])
        db.session.commit()
        self.assertIsNone(active_catalog_listing_operation(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            listing_id=listing.id,
        ))


if __name__ == "__main__":
    unittest.main()
