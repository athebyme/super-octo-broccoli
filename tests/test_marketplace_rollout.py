# -*- coding: utf-8 -*-
"""P11 bounded WB projection, parity metrics and guarded read cutover."""

from datetime import datetime, timedelta
import unittest

from flask import Flask
from sqlalchemy.exc import IntegrityError

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    MarketplaceListingLinkEvent,
    MarketplaceProjectionRun,
    Product,
    Seller,
    User,
    db,
)
from services.marketplace_rollout import (
    MarketplaceRolloutBusy,
    MarketplaceRolloutService,
    MarketplaceRolloutValidationError,
)


class MarketplaceRolloutServiceTest(unittest.TestCase):
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
        user = User(
            username="rollout-seller",
            email="rollout@test.local",
            is_active=True,
        )
        user.set_password("synthetic-password")
        seller = Seller(user=user, company_name="Rollout Seller")
        wb = Marketplace(
            name="Wildberries",
            code="wb",
            adapter_code="wb",
            is_active=True,
        )
        db.session.add_all([seller, wb])
        db.session.commit()
        self.seller_id = seller.id
        self.wb_id = wb.id
        self.source_at = datetime(2026, 7, 1, 10, 0, 0)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _products(self, count):
        rows = []
        for index in range(1, count + 1):
            rows.append(Product(
                seller_id=self.seller_id,
                nm_id=800_000 + index,
                imt_id=900_000 + index,
                vendor_code=f"offer-{index}",
                title=f"Карточка {index}",
                description=f"Описание {index}",
                subject_id=100 + (index % 3),
                price="1000.00",
                discount_price="900.00",
                quantity=index % 11,
                photos_json='["https://example.test/photo.jpg"]',
                characteristics_json='[{"id":1,"value":"Тест"}]',
                dimensions_json='{"length":10,"width":20,"height":30}',
                is_active=index % 7 != 0,
                created_at=self.source_at,
                updated_at=self.source_at,
                last_sync=self.source_at,
            ))
        db.session.add_all(rows)
        db.session.commit()
        return rows

    def _finish_backfill(self, *, now, limit=200, force=False):
        run = MarketplaceRolloutService.run_backfill_batch(
            seller_id=self.seller_id,
            limit=limit,
            force_full=force,
            now=now,
        )
        while run is not None and run.status == "running":
            run = MarketplaceRolloutService.run_backfill_batch(
                seller_id=self.seller_id,
                limit=limit,
                now=now,
            )
        return run

    def _finish_parity(self, *, now, limit=200, force=False):
        run = MarketplaceRolloutService.run_parity_batch(
            seller_id=self.seller_id,
            limit=limit,
            force_full=force,
            now=now,
        )
        while run is not None and run.status == "running":
            run = MarketplaceRolloutService.run_parity_batch(
                seller_id=self.seller_id,
                limit=limit,
                now=now,
            )
        return run

    def test_empty_catalog_is_an_exact_noop_cutover(self):
        state = MarketplaceRolloutService.readiness(
            seller_id=self.seller_id,
        )
        query, query_state = MarketplaceRolloutService.wb_product_query(
            seller_id=self.seller_id,
            common_read_requested=True,
        )
        self.assertTrue(state["cutover_ready"])
        self.assertEqual(state["blockers"], [])
        self.assertEqual(query_state["read_mode"], "marketplace_listing")
        self.assertEqual(query.count(), 0)

    def test_205_rows_are_resumable_in_200_row_batches_and_gate_common_read(self):
        self._products(205)
        backfill_at = self.source_at + timedelta(days=1)

        first = MarketplaceRolloutService.run_backfill_batch(
            seller_id=self.seller_id,
            limit=200,
            now=backfill_at,
        )
        self.assertEqual(first.status, "running")
        self.assertEqual(first.scanned_count, 200)
        self.assertEqual(first.inserted_count, 200)
        self.assertEqual(MarketplaceListing.query.count(), 200)

        completed = MarketplaceRolloutService.run_backfill_batch(
            seller_id=self.seller_id,
            limit=200,
            now=backfill_at,
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.scanned_count, 205)
        self.assertEqual(completed.inserted_count, 205)
        self.assertEqual(MarketplaceListing.query.count(), 205)

        parity_at = backfill_at + timedelta(minutes=1)
        shadow_first = MarketplaceRolloutService.run_parity_batch(
            seller_id=self.seller_id,
            limit=200,
            now=parity_at,
        )
        self.assertEqual(shadow_first.status, "running")
        self.assertEqual(shadow_first.matched_count, 200)
        shadow_done = MarketplaceRolloutService.run_parity_batch(
            seller_id=self.seller_id,
            limit=200,
            now=parity_at,
        )
        self.assertEqual(shadow_done.status, "completed")
        self.assertEqual(shadow_done.matched_count, 205)
        self.assertEqual(shadow_done.missing_count, 0)
        self.assertEqual(shadow_done.mismatched_count, 0)
        self.assertEqual(shadow_done.to_public_dict()["parity_ratio"], 1.0)

        query, state = MarketplaceRolloutService.wb_product_query(
            seller_id=self.seller_id,
            common_read_requested=True,
        )
        self.assertEqual(state["read_mode"], "marketplace_listing")
        self.assertTrue(state["cutover_ready"])
        self.assertEqual(query.count(), 205)

    def test_scheduler_tick_is_bounded_and_does_not_report_idle_runs_as_work(self):
        self._products(205)
        first_at = self.source_at + timedelta(days=1)

        first = MarketplaceRolloutService.maintenance_tick(
            seller_limit=1,
            batch_size=200,
            now=first_at,
        )
        second = MarketplaceRolloutService.maintenance_tick(
            seller_limit=1,
            batch_size=200,
            now=first_at + timedelta(minutes=1),
        )
        third = MarketplaceRolloutService.maintenance_tick(
            seller_limit=1,
            batch_size=200,
            now=first_at + timedelta(minutes=2),
        )
        idle = MarketplaceRolloutService.maintenance_tick(
            seller_limit=1,
            batch_size=200,
            now=first_at + timedelta(minutes=3),
        )

        self.assertEqual(first["backfill_batches"], 1)
        self.assertEqual(first["parity_batches"], 0)
        self.assertEqual(second["backfill_batches"], 1)
        self.assertEqual(second["parity_batches"], 1)
        self.assertEqual(third["backfill_batches"], 0)
        self.assertEqual(third["parity_batches"], 1)
        self.assertEqual(idle["backfill_batches"], 0)
        self.assertEqual(idle["parity_batches"], 0)

    def test_source_or_projection_drift_blocks_cutover_until_repaired(self):
        products = self._products(3)
        backfill_at = self.source_at + timedelta(days=1)
        self._finish_backfill(now=backfill_at)
        self._finish_parity(now=backfill_at + timedelta(minutes=1))

        product = products[0]
        product.title = "Новое legacy название"
        product.updated_at = backfill_at + timedelta(minutes=2)
        db.session.commit()
        query, stale = MarketplaceRolloutService.wb_product_query(
            seller_id=self.seller_id,
            common_read_requested=True,
        )
        self.assertEqual(stale["read_mode"], "legacy_fallback")
        self.assertFalse(stale["cutover_ready"])
        self.assertIn("wb_source_changed_after_sweep", stale["blockers"])
        self.assertEqual(query.count(), 3)

        repaired_at = backfill_at + timedelta(minutes=3)
        repaired = self._finish_backfill(now=repaired_at)
        self.assertEqual(repaired.updated_count, 1)
        parity = self._finish_parity(now=repaired_at + timedelta(minutes=1))
        self.assertEqual(parity.mismatched_count, 0)
        self.assertTrue(
            MarketplaceRolloutService.readiness(
                seller_id=self.seller_id,
            )["cutover_ready"]
        )

        listing = MarketplaceListing.query.filter_by(
            legacy_product_id=products[1].id,
        ).one()
        listing.title = "Внешний локальный drift"
        db.session.commit()
        drift = self._finish_parity(
            now=repaired_at + timedelta(minutes=2),
            force=True,
        )
        self.assertEqual(drift.mismatched_count, 1)
        self.assertEqual(drift.mismatch_fields_json, '{"title":1}')
        sample = drift.to_public_dict()["mismatch_sample"]
        self.assertEqual(sample[0]["fields"], ["title"])
        self.assertNotIn("Внешний локальный drift", str(sample))
        fallback = MarketplaceRolloutService.wb_product_query(
            seller_id=self.seller_id,
            common_read_requested=True,
        )[1]
        self.assertEqual(fallback["read_mode"], "legacy_fallback")

    def test_direct_canonical_fk_is_linked_once_with_append_only_audit(self):
        product = self._products(1)[0]
        imported = ImportedProduct(
            seller_id=self.seller_id,
            product_id=product.id,
            external_id="canonical-1",
            external_vendor_code="offer-1",
            source_type="synthetic",
            title="Общая карточка",
            created_at=self.source_at,
            updated_at=self.source_at,
        )
        db.session.add(imported)
        db.session.commit()

        run = self._finish_backfill(now=self.source_at + timedelta(days=1))
        listing = MarketplaceListing.query.one()
        event = MarketplaceListingLinkEvent.query.one()
        self.assertEqual(run.status, "completed")
        self.assertEqual(listing.imported_product_id, imported.id)
        self.assertEqual(listing.canonical_link_status, "linked")
        self.assertEqual(event.action, "bootstrap")
        self.assertEqual(event.source, "wb_backfill")

        self._finish_backfill(
            now=self.source_at + timedelta(days=2),
            force=True,
        )
        self.assertEqual(MarketplaceListingLinkEvent.query.count(), 1)

        imported.product_id = None
        imported.updated_at = self.source_at + timedelta(days=3)
        db.session.commit()
        unlinked = MarketplaceRolloutService.readiness(
            seller_id=self.seller_id,
        )
        self.assertFalse(unlinked["cutover_ready"])
        self.assertEqual(
            unlinked["first_link_mismatch_product_id"],
            product.id,
        )
        self.assertIn("wb_canonical_link_mismatch", unlinked["blockers"])

    def test_common_read_execution_gate_falls_back_for_a_concurrent_new_product(self):
        self._products(2)
        backfill_at = self.source_at + timedelta(days=1)
        self._finish_backfill(now=backfill_at)
        self._finish_parity(now=backfill_at + timedelta(minutes=1))

        query, state = MarketplaceRolloutService.wb_product_query(
            seller_id=self.seller_id,
            common_read_requested=True,
        )
        self.assertEqual(state["read_mode"], "marketplace_listing")

        db.session.add(Product(
            seller_id=self.seller_id,
            nm_id=999_999,
            vendor_code="concurrent-offer",
            title="Создана после readiness",
            is_active=True,
            created_at=backfill_at + timedelta(minutes=2),
            updated_at=backfill_at + timedelta(minutes=2),
        ))
        db.session.commit()

        # The SQL-level global missing-row guard switches this execution to
        # the complete legacy membership instead of hiding the new card.
        self.assertEqual(query.count(), 3)

    def test_batch_limits_and_database_active_run_constraint_fail_closed(self):
        self._products(1)
        with self.assertRaises(MarketplaceRolloutValidationError):
            MarketplaceRolloutService.run_backfill_batch(
                seller_id=self.seller_id,
                limit=201,
            )
        first = MarketplaceProjectionRun(
            seller_id=self.seller_id,
            marketplace_id=self.wb_id,
            run_kind="wb_backfill",
            status="running",
            target_product_id=1,
        )
        second = MarketplaceProjectionRun(
            seller_id=self.seller_id,
            marketplace_id=self.wb_id,
            run_kind="wb_backfill",
            status="pending",
            target_product_id=1,
        )
        db.session.add(first)
        db.session.commit()
        db.session.add(second)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_pause_resume_is_durable_and_refuses_an_owned_batch_lease(self):
        run = MarketplaceProjectionRun(
            seller_id=self.seller_id,
            marketplace_id=self.wb_id,
            run_kind="wb_backfill",
            status="pending",
            target_product_id=10,
        )
        db.session.add(run)
        db.session.commit()

        paused = MarketplaceRolloutService.pause_run(run_id=run.id)
        self.assertEqual(paused.status, "paused")
        resumed = MarketplaceRolloutService.resume_run(run_id=run.id)
        self.assertEqual(resumed.status, "running")

        resumed.lease_owner = "synthetic-owned-batch"
        resumed.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
        db.session.commit()
        with self.assertRaises(MarketplaceRolloutBusy):
            MarketplaceRolloutService.pause_run(run_id=run.id)


if __name__ == "__main__":
    unittest.main()
