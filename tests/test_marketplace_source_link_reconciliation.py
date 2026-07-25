"""Existing Ozon/WB links are repaired by a bounded durable local cursor."""

from datetime import datetime, timedelta
import unittest

from flask import Flask

from models import (
    BackgroundJob,
    Marketplace,
    MarketplaceListing,
    Product,
    Seller,
    SellerMarketplaceAccount,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from services.marketplace_source_link_reconciliation import (
    MarketplaceSourceLinkReconciliation,
)


class MarketplaceSourceLinkReconciliationTest(unittest.TestCase):
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
            username="source-link",
            email="source-link@test.local",
            is_active=True,
        )
        user.set_password("synthetic-password")
        self.seller = Seller(user=user, company_name="Source link")
        self.ozon = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        self.supplier = Supplier(
            name="Sexoptovik",
            code="sexoptovik",
        )
        db.session.add_all([self.seller, self.ozon, self.supplier])
        db.session.flush()
        self.account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=self.ozon.id,
            external_account_id="source-link-account",
            label="Source link account",
            is_active=True,
            connection_status="connected",
        )
        db.session.add_all([
            self.account,
            SellerSupplier(
                seller_id=self.seller.id,
                supplier_id=self.supplier.id,
                is_active=True,
            ),
        ])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _triple_without_canonical(self, source_id: int):
        source = SupplierProduct(
            supplier_id=self.supplier.id,
            external_id=str(source_id),
            title=f"Source {source_id}",
            status="ready",
        )
        wb = Product(
            seller_id=self.seller.id,
            nm_id=1_000_000 + source_id,
            vendor_code=f"id-{source_id}-1366",
            title=f"WB {source_id}",
        )
        listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.ozon.id,
            account_id=self.account.id,
            offer_id=f"id-{source_id}-1364",
            external_product_id=str(2_000_000 + source_id),
            title=f"Ozon {source_id}",
            sync_fingerprint=f"{source_id:064d}"[-64:],
        )
        db.session.add_all([source, wb, listing])
        db.session.commit()
        return listing

    def test_keyset_cursor_materializes_and_links_across_restart(self):
        listings = [
            self._triple_without_canonical(source_id)
            for source_id in (7725, 7726, 7727)
        ]
        explicitly_unlinked = self._triple_without_canonical(7728)
        explicitly_unlinked.link_source = "seller_unlink"
        db.session.commit()
        now = datetime(2026, 7, 24, 12, 0, 0)

        created = MarketplaceSourceLinkReconciliation.ensure_jobs(
            account_limit=3,
            now=now,
        )
        self.assertEqual(created, 1)
        job = BackgroundJob.query.filter_by(
            job_type=MarketplaceSourceLinkReconciliation.JOB_TYPE,
        ).one()
        self.assertEqual(job.total, 3)

        first = MarketplaceSourceLinkReconciliation.process_job_batch(
            job=job,
            batch_size=2,
            now=now,
        )
        self.assertEqual(first["linked"], 2)
        self.assertEqual(first["materialized"], 2)
        db.session.expire_all()
        job = db.session.get(BackgroundJob, job.id)
        first_progress = MarketplaceSourceLinkReconciliation._document(job)
        self.assertEqual(
            first_progress["cursor_listing_id"],
            listings[1].id,
        )
        self.assertEqual(job.status, "pending")

        # A new process can resume from the durable listing keyset.
        second = MarketplaceSourceLinkReconciliation.process_job_batch(
            job=job,
            batch_size=2,
            now=now + timedelta(minutes=1),
        )
        self.assertEqual(second["linked"], 1)
        self.assertEqual(second["materialized"], 1)
        job = db.session.get(BackgroundJob, job.id)
        completed = MarketplaceSourceLinkReconciliation.process_job_batch(
            job=job,
            batch_size=2,
            now=now + timedelta(minutes=2),
        )
        self.assertTrue(completed["completed"])

        db.session.expire_all()
        for listing in listings:
            current = db.session.get(MarketplaceListing, listing.id)
            self.assertIsNotNone(current.imported_product_id)
            self.assertEqual(current.link_source, "exact_source_identity")
            self.assertIsNotNone(current.imported_product.product_id)
        self.assertIsNone(
            db.session.get(
                MarketplaceListing,
                explicitly_unlinked.id,
            ).imported_product_id
        )
        job = db.session.get(BackgroundJob, job.id)
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.succeeded, 3)
        self.assertEqual(job.get_result()["materialized"], 3)

    def test_active_or_recent_completed_scope_is_not_duplicated(self):
        self._triple_without_canonical(88001)
        now = datetime(2026, 7, 24, 12, 0, 0)
        self.assertEqual(
            MarketplaceSourceLinkReconciliation.ensure_jobs(now=now),
            1,
        )
        self.assertEqual(
            MarketplaceSourceLinkReconciliation.ensure_jobs(now=now),
            0,
        )
        job = BackgroundJob.query.filter_by(
            job_type=MarketplaceSourceLinkReconciliation.JOB_TYPE,
        ).one()
        MarketplaceSourceLinkReconciliation.process_job_batch(
            job=job,
            now=now,
        )
        job = db.session.get(BackgroundJob, job.id)
        MarketplaceSourceLinkReconciliation.process_job_batch(
            job=job,
            now=now + timedelta(minutes=1),
        )
        self.assertEqual(
            MarketplaceSourceLinkReconciliation.ensure_jobs(
                now=now + timedelta(hours=1),
            ),
            0,
        )

    def test_failed_scope_has_bounded_retry_cooldown(self):
        self._triple_without_canonical(88002)
        now = datetime(2026, 7, 24, 12, 0, 0)
        self.assertEqual(
            MarketplaceSourceLinkReconciliation.ensure_jobs(now=now),
            1,
        )
        job = BackgroundJob.query.filter_by(
            job_type=MarketplaceSourceLinkReconciliation.JOB_TYPE,
        ).one()
        job.status = "failed"
        job.updated_at = now
        db.session.commit()

        self.assertEqual(
            MarketplaceSourceLinkReconciliation.ensure_jobs(
                now=now + timedelta(minutes=9),
            ),
            0,
        )
        self.assertEqual(
            MarketplaceSourceLinkReconciliation.ensure_jobs(
                now=now + timedelta(minutes=11),
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
