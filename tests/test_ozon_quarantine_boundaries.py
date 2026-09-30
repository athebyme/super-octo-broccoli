"""Provider-spy checks for every currently active Ozon write boundary."""

from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from models import MarketplaceOperation, MarketplaceWriteQuarantine, db
from services.marketplace_commercial import (
    MarketplaceCommercialConflict, MarketplaceCommercialService,
    QuarantinedCommercialConflict,
)
from services.marketplace_publications import MarketplacePublicationService

# Existing fixture modules live in a flat tests directory without a package.
sys.path.insert(0, str(Path(__file__).parent))
from test_marketplace_publications import (
    OzonPublicationFixture, SyntheticPublicationAdapter,
    SyntheticFullStateAdapter, SYNTHETIC_CREDENTIALS as PUBLICATION_CREDENTIALS,
)
import test_marketplace_commercial as commercial_fixture
import test_marketplace_commercial_batch as batch_fixture

COMMERCIAL_CREDENTIALS = commercial_fixture.SYNTHETIC_CREDENTIALS


def place_fixture_hold(fixture, *, offer_id=None, product_id=None, account_wide=False,
                       origin=None):
    """Insert a persisted operator decision; service tests own decision validation."""
    if origin is None:
        origin = MarketplaceOperation(
            seller_id=fixture.seller.id, marketplace_id=fixture.marketplace.id,
            account_id=fixture.account.id, operation_kind="price_update",
            status="uncertain", idempotency_key=f"hold-origin-{datetime.utcnow().timestamp()}",
            request_fingerprint="a" * 64, contract_version="test",
            request_summary_json="{}", attempt_count=1,
        )
        db.session.add(origin)
        db.session.flush()
    hold = MarketplaceWriteQuarantine(
        seller_id=fixture.seller.id, marketplace_id=fixture.marketplace.id,
        account_id=fixture.account.id, operation_id=origin.id,
        scope_kind="account" if account_wide else "product",
        offer_id=None if account_wide else offer_id,
        product_id=None if account_wide else product_id,
        scope_reason="identity_unknown" if account_wide else "immutable_target_verified",
        reviewed_scope_token="a" * 64, status="active",
    )
    db.session.add(hold)
    db.session.commit()
    return hold


class PublicationBoundaryTest(OzonPublicationFixture, unittest.TestCase):
    def test_bulk_enqueue_explains_hold_without_creating_operation(self):
        from services.marketplace_drafts import MarketplaceDraftService
        place_fixture_hold(self, offer_id="safe-offer")
        with patch.object(MarketplaceDraftService, "_build_validation_result",
                          return_value=self.validation_result()):
            result = MarketplacePublicationService.enqueue_bulk_publications(
                seller_id=self.seller.id, account_id=self.account.id,
                draft_ids=[self.draft.id], created_by_user_id=self.user.id)
        self.assertEqual(result["queued"], [])
        self.assertEqual(result["skipped"][0]["draft_id"], self.draft.id)
        self.assertIn("остановлены", result["skipped"][0]["reason"])
        self.assertEqual(MarketplaceOperation.query.count(), 1)  # hold origin only

    def test_bulk_update_enqueue_uses_product_identity_to_explain_hold(self):
        from services.marketplace_drafts import MarketplaceDraftService
        self.attach_listing()
        place_fixture_hold(self, offer_id="previous-offer", product_id="987654")
        with patch.object(MarketplaceDraftService, "_build_validation_result",
                          return_value=self.validation_result()):
            result = MarketplacePublicationService.enqueue_bulk_updates(
                seller_id=self.seller.id, account_id=self.account.id,
                draft_ids=[self.draft.id], created_by_user_id=self.user.id)
        self.assertEqual(result["queued"], [])
        self.assertIn("остановлены", result["skipped"][0]["reason"])

    def test_import_and_queued_dispatch_stop_before_media_or_provider_read(self):
        place_fixture_hold(self, offer_id="safe-offer")
        adapter = SyntheticPublicationAdapter()
        operation = self.start(adapter)
        self.assertEqual((operation.status, operation.error_code, operation.attempt_count),
                         ("failed", "ozon_write_quarantined", 0))
        self.assertEqual(adapter.list_calls, [])
        self.assertEqual(adapter.submitted_payloads, [])

        # A previously queued job is checked again when scheduler dispatches it.
        other = self._queued_import("queued-held-import-0001")
        result = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id, operation_id=other.id,
            adapter=adapter, credentials=PUBLICATION_CREDENTIALS)
        self.assertEqual((result.status, result.error_code, result.attempt_count),
                         ("failed", "ozon_write_quarantined", 0))
        self.assertEqual(adapter.list_calls, [])
        self.assertEqual(adapter.submitted_payloads, [])

    def _queued_import(self, key):
        from unittest.mock import patch
        from services.marketplace_drafts import MarketplaceDraftService
        with patch.object(MarketplaceDraftService, "_build_validation_result",
                          return_value=self.validation_result()):
            draft, payload, _ = MarketplacePublicationService._publication_payload(
                seller_id=self.seller.id, draft_id=self.draft.id,
                expected_version=self.expected_version)
        return MarketplacePublicationService._create_operation(
            draft=draft, payload=payload, idempotency_key=key,
            created_by_user_id=self.user.id, now=datetime.utcnow())

    def test_full_update_is_fenced_by_provider_product_id_after_offer_rename(self):
        self.attach_listing()
        place_fixture_hold(self, offer_id="old-offer", product_id="987654")
        adapter = SyntheticFullStateAdapter(self.prior_payload())
        operation = self.start_update(adapter)
        self.assertEqual((operation.status, operation.error_code, operation.attempt_count),
                         ("failed", "ozon_write_quarantined", 0))
        self.assertEqual(adapter.submitted_payloads, [])
        self.assertEqual(adapter.list_calls, [])

    def test_update_rollback_and_archive_compensation_stop_without_second_write(self):
        prior = self.prior_payload()
        self.attach_listing(prior)
        adapter = SyntheticFullStateAdapter(prior)
        completed = self.start_update(adapter)
        completed = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id, operation_id=completed.id,
            adapter=adapter, credentials=PUBLICATION_CREDENTIALS)
        self.assertEqual(completed.status, "succeeded")
        writes_before = len(adapter.submitted_payloads)
        place_fixture_hold(self, offer_id="safe-offer", product_id="987654")
        rollback = MarketplacePublicationService.start_update_rollback(
            seller_id=self.seller.id, operation_id=completed.id,
            expected_version=completed.version,
            idempotency_key="held-update-rollback-0001",
            created_by_user_id=self.user.id, adapter=adapter,
            credentials=PUBLICATION_CREDENTIALS)
        self.assertEqual((rollback.status, rollback.error_code, rollback.attempt_count),
                         ("failed", "ozon_write_quarantined", 0))
        self.assertEqual(len(adapter.submitted_payloads), writes_before)

    def test_archive_compensation_has_zero_archive_attempts(self):
        adapter = SyntheticFullStateAdapter(create_mode=True)
        created = self.start(adapter, key="held-archive-source-0001")
        completed = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id, operation_id=created.id,
            adapter=adapter, credentials=PUBLICATION_CREDENTIALS)
        self.assertEqual(completed.status, "succeeded")
        place_fixture_hold(self, offer_id="safe-offer", product_id="987654")
        archived = MarketplacePublicationService.start_create_rollback(
            seller_id=self.seller.id, operation_id=completed.id,
            expected_version=completed.version,
            idempotency_key="held-archive-compensation-0001",
            created_by_user_id=self.user.id, adapter=adapter,
            credentials=PUBLICATION_CREDENTIALS)
        self.assertEqual((archived.status, archived.error_code, archived.attempt_count),
                         ("failed", "ozon_write_quarantined", 0))
        self.assertEqual(adapter.archive_calls, [])

    def test_attempted_manual_read_does_not_restart_due_polling(self):
        adapter = SyntheticPublicationAdapter(ambiguous=True)
        operation = self.start(adapter)
        self.assertEqual(operation.status, "uncertain")
        place_fixture_hold(self, offer_id="safe-offer", origin=operation)
        operation.next_poll_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        due = MarketplacePublicationService.poll_due_operations(limit=1)
        self.assertEqual(due["selected"], 0)
        operation.next_poll_at = None
        db.session.commit()
        before = len(adapter.submitted_payloads)
        MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id, operation_id=operation.id,
            adapter=adapter, credentials=PUBLICATION_CREDENTIALS)
        db.session.refresh(operation)
        self.assertEqual(operation.next_poll_at, None)
        self.assertEqual(operation.attempt_count, 1)
        self.assertEqual(len(adapter.submitted_payloads), before)


class CommercialBoundaryTest(unittest.TestCase):
    setUp = commercial_fixture.MarketplaceCommercialServiceTest.setUp
    tearDown = commercial_fixture.MarketplaceCommercialServiceTest.tearDown
    price_proposal = commercial_fixture.MarketplaceCommercialServiceTest.price_proposal
    approve = commercial_fixture.MarketplaceCommercialServiceTest.approve

    def test_price_and_stock_approval_conflict_before_operation_or_provider_read(self):
        for kind in ("price", "stock"):
            proposal = (self.price_proposal(idempotency_key=f"hold-price-{kind}-0001")
                        if kind == "price" else MarketplaceCommercialService.create_stock_proposal(
                            seller_id=self.seller.id, listing_id=self.listing.id,
                            warehouse_id=self.warehouse.id, stock=7, source="user",
                            idempotency_key="hold-stock-0001",
                            created_by_user_id=self.user.id, adapter=self.adapter,
                            credentials=COMMERCIAL_CREDENTIALS))
            if kind == "price":
                place_fixture_hold(self, offer_id="offer-1", product_id="101")
            with patch.object(self.adapter, "read_prices", wraps=self.adapter.read_prices) as price_read, \
                 patch.object(self.adapter, "read_stocks_by_warehouse_fbs",
                              wraps=self.adapter.read_stocks_by_warehouse_fbs) as stock_read:
                with self.assertRaises(QuarantinedCommercialConflict) as error:
                    self.approve(proposal)
                price_read.assert_not_called()
                stock_read.assert_not_called()
            self.assertEqual(error.exception.code, "ozon_write_quarantined")
            self.assertEqual(error.exception.write_quarantine["scope"], "product")
            self.assertIsNone(proposal.operation_id)
        self.assertEqual(self.adapter.price_writes, [])
        self.assertEqual(self.adapter.stock_writes, [])

    def test_queued_price_and_stock_fail_at_dispatch_with_zero_attempts(self):
        for kind in ("price", "stock"):
            proposal = (self.price_proposal(idempotency_key="queued-price-held-0001")
                        if kind == "price" else MarketplaceCommercialService.create_stock_proposal(
                            seller_id=self.seller.id, listing_id=self.listing.id,
                            warehouse_id=self.warehouse.id, stock=7, source="user",
                            idempotency_key="queued-stock-held-0001",
                            created_by_user_id=self.user.id, adapter=self.adapter,
                            credentials=COMMERCIAL_CREDENTIALS))
            operation = MarketplaceCommercialService._create_operation(
                proposal=proposal, reviewer_id=self.user.id, now=datetime.utcnow())
            if kind == "price":
                place_fixture_hold(self, offer_id="offer-1", product_id="101")
                self.account.connection_status = "error"
                db.session.commit()
            result = MarketplaceCommercialService.poll_operation(
                seller_id=self.seller.id, operation_id=operation.id,
                adapter=self.adapter, credentials=COMMERCIAL_CREDENTIALS,
                allow_submission=True)
            self.assertEqual((result.status, result.error_code, result.attempt_count),
                             ("failed", "ozon_write_quarantined", 0))
            if kind == "price":
                self.account.connection_status = "connected"
                db.session.commit()
        self.assertEqual(self.adapter.price_writes, [])
        self.assertEqual(self.adapter.stock_writes, [])

    def test_attempted_commercial_read_remains_manual_and_stays_off_due_queue(self):
        self.adapter.ambiguous_mode = "no_apply"
        proposal = self.price_proposal(idempotency_key="held-attempted-price-0001")
        attempted = self.approve(proposal)
        operation = db.session.get(MarketplaceOperation, attempted.operation_id)
        self.assertEqual((operation.status, operation.attempt_count), ("uncertain", 1))
        place_fixture_hold(self, offer_id="offer-1", product_id="101", origin=operation)
        operation.next_poll_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        due = MarketplaceCommercialService.poll_due_operations(limit=1)
        self.assertEqual(due["selected"], 0)
        operation.next_poll_at = None
        db.session.commit()
        writes_before = len(self.adapter.price_writes)
        with patch.object(self.adapter, "read_prices", wraps=self.adapter.read_prices) as read:
            MarketplaceCommercialService.poll_operation(
                seller_id=self.seller.id, operation_id=operation.id,
                adapter=self.adapter, credentials=COMMERCIAL_CREDENTIALS,
                now=datetime(2026, 7, 15, 12, 6, 0))
            read.assert_called()
        db.session.refresh(operation)
        self.assertEqual(operation.next_poll_at, None)
        self.assertEqual(operation.attempt_count, 1)
        self.assertEqual(len(self.adapter.price_writes), writes_before)

    def test_price_rollback_proposal_cannot_send_a_second_write(self):
        original = self.approve(self.price_proposal(
            idempotency_key="rollback-source-price-0001"))
        rollback = MarketplaceCommercialService.create_rollback_proposal(
            seller_id=self.seller.id, operation_id=original.operation_id,
            idempotency_key="held-price-rollback-0001",
            created_by_user_id=self.user.id, adapter=self.adapter,
            credentials=COMMERCIAL_CREDENTIALS)
        place_fixture_hold(self, offer_id="offer-1", product_id="101")
        writes_before = len(self.adapter.price_writes)
        with patch.object(self.adapter, "read_prices", wraps=self.adapter.read_prices) as read:
            with self.assertRaises(MarketplaceCommercialConflict):
                self.approve(rollback)
            read.assert_not_called()
        self.assertIsNone(rollback.operation_id)
        operation = MarketplaceCommercialService._create_operation(
            proposal=rollback, reviewer_id=self.user.id, now=datetime.utcnow())
        result = MarketplaceCommercialService.poll_operation(
            seller_id=self.seller.id, operation_id=operation.id,
            adapter=self.adapter, credentials=COMMERCIAL_CREDENTIALS,
            allow_submission=True)
        self.assertEqual((result.status, result.error_code, result.attempt_count),
                         ("failed", "ozon_write_quarantined", 0))
        self.assertEqual(len(self.adapter.price_writes), writes_before)

    def test_stock_rollback_dispatch_cannot_send_a_second_write(self):
        original_proposal = MarketplaceCommercialService.create_stock_proposal(
            seller_id=self.seller.id, listing_id=self.listing.id,
            warehouse_id=self.warehouse.id, stock=7, source="user",
            idempotency_key="stock-rollback-source-0001",
            created_by_user_id=self.user.id, adapter=self.adapter,
            credentials=COMMERCIAL_CREDENTIALS)
        original = self.approve(original_proposal)
        rollback = MarketplaceCommercialService.create_rollback_proposal(
            seller_id=self.seller.id, operation_id=original.operation_id,
            idempotency_key="held-stock-rollback-0001",
            created_by_user_id=self.user.id, adapter=self.adapter,
            credentials=COMMERCIAL_CREDENTIALS)
        operation = MarketplaceCommercialService._create_operation(
            proposal=rollback, reviewer_id=self.user.id, now=datetime.utcnow())
        place_fixture_hold(self, offer_id="offer-1", product_id="101")
        writes_before = len(self.adapter.stock_writes)
        result = MarketplaceCommercialService.poll_operation(
            seller_id=self.seller.id, operation_id=operation.id,
            adapter=self.adapter, credentials=COMMERCIAL_CREDENTIALS,
            allow_submission=True)
        self.assertEqual((result.status, result.error_code, result.attempt_count),
                         ("failed", "ozon_write_quarantined", 0))
        self.assertEqual(len(self.adapter.stock_writes), writes_before)


class CommercialBatchBoundaryTest(unittest.TestCase):
    setUp = batch_fixture.MarketplaceCommercialBatchTest.setUp
    tearDown = batch_fixture.MarketplaceCommercialBatchTest.tearDown
    price_proposals = batch_fixture.MarketplaceCommercialBatchTest.price_proposals
    stock_proposals = batch_fixture.MarketplaceCommercialBatchTest.stock_proposals
    approve = batch_fixture.MarketplaceCommercialBatchTest.approve

    def test_one_held_member_prevents_entire_frozen_batch_and_early_approval(self):
        proposals = self.price_proposals()
        place_fixture_hold(self, offer_id="batch-offer-2", product_id="202")
        before = len(self.adapter.price_reads)
        with self.assertRaises(MarketplaceCommercialConflict):
            self.approve(proposals)
        self.assertEqual(len(self.adapter.price_reads), before)
        self.assertEqual(self.adapter.price_writes, [])
        self.assertTrue(all(proposal.operation_id is None for proposal in proposals))

        operations = [MarketplaceCommercialService._build_operation(
            proposal=proposal, reviewer_id=self.user.id, now=datetime.utcnow())
            for proposal in proposals]
        db.session.commit()
        MarketplaceCommercialService._submit_batch_locked(
            operations=operations, proposals=proposals, adapter=self.adapter,
            credentials=COMMERCIAL_CREDENTIALS, now=datetime.utcnow())
        self.assertEqual(self.adapter.price_writes, [])
        self.assertTrue(all(op.status == "failed" and op.attempt_count == 0
                            and op.error_code == "ozon_write_quarantined"
                            for op in operations))

    def test_one_held_stock_prevents_entire_frozen_stock_batch(self):
        proposals = self.stock_proposals()
        operations = [MarketplaceCommercialService._build_operation(
            proposal=proposal, reviewer_id=self.user.id, now=datetime.utcnow())
            for proposal in proposals]
        db.session.commit()
        place_fixture_hold(self, offer_id="batch-offer-2", product_id="202")
        MarketplaceCommercialService._submit_batch_locked(
            operations=operations, proposals=proposals, adapter=self.adapter,
            credentials=COMMERCIAL_CREDENTIALS, now=datetime.utcnow())
        self.assertEqual(self.adapter.stock_writes, [])
        self.assertTrue(all(op.status == "failed" and op.attempt_count == 0
                            for op in operations))
