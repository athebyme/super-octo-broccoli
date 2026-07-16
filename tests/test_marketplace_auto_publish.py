# -*- coding: utf-8 -*-
"""Account-scoped Ozon draft provisioning, quotas and circuit breaker."""

from datetime import datetime, timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask

from models import (
    AutoPublishItem,
    AutoPublishRun,
    AutoPublishSettings,
    ImportedProduct,
    Marketplace,
    MarketplaceProductDraft,
    Notification,
    Seller,
    SellerMarketplaceAccount,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from services.marketplace_auto_publish import (
    MarketplaceAutoPublishError,
    MarketplaceDraftProvisioner,
    OzonAutoPublishService,
)
from services.auto_publish_service import reset_stuck_auto_publish
from services.supplier_service import SupplierService


class MarketplaceAutoPublishTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
            MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=True,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.seller = self._seller("auto-ozon", "auto-ozon@test.local")
        self.other_seller = self._seller("auto-other", "auto-other@test.local")
        self.marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        self.supplier = Supplier(name="Synthetic", code="synthetic-auto")
        db.session.add_all([self.marketplace, self.supplier])
        db.session.flush()
        self.account1 = self._account(self.seller, "account-one")
        self.account2 = self._account(self.seller, "account-two")
        self.other_account = self._account(self.other_seller, "account-other")
        self.settings1 = self._settings(self.seller, self.account1)
        self.settings2 = self._settings(self.seller, self.account2)
        self.other_settings = self._settings(
            self.other_seller, self.other_account
        )
        db.session.add(SellerSupplier(
            seller_id=self.seller.id,
            supplier_id=self.supplier.id,
            is_active=True,
        ))
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

    def _account(self, seller, label):
        account = SellerMarketplaceAccount(
            seller_id=seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id=f"external-{label}",
            label=label,
            is_active=True,
            connection_status="connected",
            _credentials_encrypted="synthetic-encrypted-credential",
        )
        db.session.add(account)
        db.session.flush()
        return account

    @staticmethod
    def _settings(seller, account):
        settings = AutoPublishSettings(
            seller_id=seller.id,
            marketplace_code="ozon",
            account_id=account.id,
            is_enabled=True,
            batch_size=10,
            max_daily_publishes=100,
            max_retries_per_product=3,
            failure_threshold=3,
        )
        db.session.add(settings)
        db.session.flush()
        return settings

    def _product(self, suffix, *, seller=None):
        seller = seller or self.seller
        original = {
            "external_id": f"source-{suffix}",
            "vendor_code": f"offer-{suffix}",
            "title": f"Товар {suffix}",
            "description": "Наблюдаемое описание",
            "brand": "Наблюдаемый бренд",
            "category": "Исходная категория",
            "photo_urls": [f"https://img.test/{suffix}.jpg"],
            "barcodes": [f"460000000{suffix:03d}"],
            "dimensions": {
                "package_width_cm": 10,
                "package_height_cm": 5,
                "package_length_cm": 20,
                "package_weight_g": 300,
            },
        }
        product = ImportedProduct(
            seller_id=seller.id,
            supplier_id=self.supplier.id,
            source_type="synthetic-auto",
            external_id=f"source-{suffix}",
            external_vendor_code=f"offer-{suffix}",
            title=f"Товар {suffix}",
            description="Наблюдаемое описание",
            brand="Наблюдаемый бренд",
            category="Исходная категория",
            original_data=json.dumps(original, ensure_ascii=False),
            photo_urls=json.dumps(original["photo_urls"]),
            barcodes=json.dumps(original["barcodes"]),
            supplier_price=100,
            calculated_price=1000,
            calculated_price_before_discount=1200,
            import_status="pending",
        )
        db.session.add(product)
        db.session.commit()
        return product

    def test_provisioner_creates_one_draft_per_enabled_owned_account(self):
        # Pause stops provider writes, not deterministic local preparation.
        self.settings2.is_paused = True
        db.session.commit()
        product = self._product(1)

        first = MarketplaceDraftProvisioner.provision(
            seller_id=self.seller.id,
            imported_product_ids=[product.id],
        )
        second = MarketplaceDraftProvisioner.provision(
            seller_id=self.seller.id,
            imported_product_ids=[product.id],
        )

        drafts = MarketplaceProductDraft.query.filter_by(
            seller_id=self.seller.id,
            imported_product_id=product.id,
        ).order_by(MarketplaceProductDraft.account_id.asc()).all()
        self.assertEqual(first["created"], 2)
        self.assertEqual(first["failed"], 0)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["existing"], 2)
        self.assertEqual(
            [draft.account_id for draft in drafts],
            [self.account1.id, self.account2.id],
        )
        self.assertNotIn(self.other_account.id, {d.account_id for d in drafts})

        foreign = self._product(2, seller=self.other_seller)
        with self.assertRaises(MarketplaceAutoPublishError):
            MarketplaceDraftProvisioner.provision(
                seller_id=self.seller.id,
                imported_product_ids=[foreign.id],
            )

    def test_disabled_target_is_not_provisioned(self):
        self.settings2.is_enabled = False
        db.session.commit()
        product = self._product(3)

        result = MarketplaceDraftProvisioner.provision(
            seller_id=self.seller.id,
            imported_product_ids=[product.id],
        )

        self.assertEqual(result["targets"], 1)
        self.assertEqual(result["created"], 1)
        draft = MarketplaceProductDraft.query.filter_by(
            seller_id=self.seller.id,
            imported_product_id=product.id,
        ).one()
        self.assertEqual(draft.account_id, self.account1.id)

    def test_supplier_import_provisions_all_enabled_targets(self):
        supplier_product = SupplierProduct(
            supplier_id=self.supplier.id,
            external_id="supplier-source",
            vendor_code="supplier-offer",
            title="Товар поставщика",
            description="Описание поставщика",
            category="Исходная категория",
            supplier_price=250,
            supplier_quantity=4,
            photo_urls_json='["https://img.test/supplier.jpg"]',
            original_data_json=json.dumps({
                "external_id": "supplier-source",
                "vendor_code": "supplier-offer",
                "title": "Товар поставщика",
                "description": "Описание поставщика",
                "category": "Исходная категория",
                "photo_urls": ["https://img.test/supplier.jpg"],
            }, ensure_ascii=False),
        )
        db.session.add(supplier_product)
        db.session.commit()

        result = SupplierService.import_to_seller(
            self.seller.id,
            [supplier_product.id],
        )

        self.assertTrue(result.success)
        self.assertEqual(result.imported, 1)
        self.assertEqual(result.marketplace_drafts_created, 2)
        self.assertEqual(result.marketplace_draft_errors, 0)
        self.assertEqual(len(result.imported_product_ids), 1)
        self.assertGreater(result.imported_product_ids[0], 0)
        scopes = {
            draft.account_id
            for draft in MarketplaceProductDraft.query.filter_by(
                imported_product_id=result.imported_product_ids[0]
            ).all()
        }
        self.assertEqual(scopes, {self.account1.id, self.account2.id})

    def _prepare_three_drafts(self):
        products = [self._product(index) for index in (10, 11, 12)]
        provisioned = MarketplaceDraftProvisioner.provision(
            seller_id=self.seller.id,
            imported_product_ids=[product.id for product in products],
            account_ids=[self.account1.id],
        )
        self.assertEqual(provisioned["created"], 3)
        return products

    @staticmethod
    def _fake_validate(service, item, *, now):
        item.status = "processing"
        item.step = "ready_to_submit"
        item.draft_version = item.draft.version
        item.started_at = now
        db.session.commit()
        return item.draft

    def test_quota_tail_is_deferred_without_cross_account_counters(self):
        products = self._prepare_three_drafts()
        self.settings1.batch_size = 3
        db.session.commit()

        def fake_submit(service, item, *, now):
            item.status = "completed"
            item.step = "published"
            item.completed_at = now
            service.settings.daily_published_count += 1
            db.session.commit()
            return SimpleNamespace(attempt_count=1, status="succeeded")

        with patch.object(
            OzonAutoPublishService,
            "_refresh_and_validate",
            new=self._fake_validate,
        ), patch.object(
            OzonAutoPublishService,
            "_submit_item",
            new=fake_submit,
        ), patch(
            "services.marketplace_auto_publish."
            "MarketplacePublicationService.get_account_quota_capacity",
            return_value={"available": 1},
        ):
            run = OzonAutoPublishService(
                self.seller, self.settings1
            ).execute_run(triggered_by="manual", now=datetime.utcnow())

        self.assertEqual(run.account_id, self.account1.id)
        self.assertEqual(run.total_candidates, 3)
        self.assertEqual(run.total_published, 1)
        self.assertEqual(run.total_deferred, 2)
        self.assertEqual(run.status, "completed")
        self.assertEqual(self.settings1.daily_published_count, 1)
        self.assertEqual(self.settings2.daily_published_count, 0)
        self.assertFalse(
            AutoPublishRun.query.filter_by(
                settings_id=self.settings2.id
            ).first()
        )
        self.assertEqual(
            {product.import_status for product in products},
            {"pending"},
        )
        statuses = {
            item.status
            for item in AutoPublishItem.query.filter_by(run_id=run.id).all()
        }
        self.assertEqual(statuses, {"completed", "deferred"})

    def test_account_circuit_breaker_pauses_only_failing_scope(self):
        self._prepare_three_drafts()
        self.settings1.batch_size = 3
        self.settings1.failure_threshold = 1
        db.session.commit()

        def fake_submit(service, item, *, now):
            service._mark_failed(
                item,
                code="synthetic_rejection",
                message="Synthetic Ozon rejection",
                now=now,
            )
            db.session.commit()
            return None

        with patch.object(
            OzonAutoPublishService,
            "_refresh_and_validate",
            new=self._fake_validate,
        ), patch.object(
            OzonAutoPublishService,
            "_submit_item",
            new=fake_submit,
        ), patch(
            "services.marketplace_auto_publish."
            "MarketplacePublicationService.get_account_quota_capacity",
            return_value={"available": 3},
        ):
            run = OzonAutoPublishService(
                self.seller, self.settings1
            ).execute_run(triggered_by="manual", now=datetime.utcnow())

        self.assertTrue(self.settings1.is_paused)
        self.assertFalse(self.settings2.is_paused)
        self.assertEqual(run.status, "paused")
        self.assertEqual(run.total_failed, 1)
        self.assertEqual(run.total_deferred, 2)
        notices = Notification.query.filter_by(seller_id=self.seller.id).all()
        self.assertEqual(len(notices), 2)
        self.assertTrue(all(
            f'"account_id": {self.account1.id}' in notice.metadata_json
            for notice in notices
        ))
        OzonAutoPublishService(
            self.seller, self.settings1
        )._recalculate_run(run, now=datetime.utcnow())
        self.assertEqual(
            Notification.query.filter_by(seller_id=self.seller.id).count(),
            2,
        )

    def test_cancellation_wins_before_durable_submit_claim(self):
        product = self._product(20)
        MarketplaceDraftProvisioner.provision(
            seller_id=self.seller.id,
            imported_product_ids=[product.id],
            account_ids=[self.account1.id],
        )
        draft = MarketplaceProductDraft.query.filter_by(
            seller_id=self.seller.id,
            account_id=self.account1.id,
            imported_product_id=product.id,
        ).one()
        run = AutoPublishRun(
            settings_id=self.settings1.id,
            seller_id=self.seller.id,
            marketplace_code="ozon",
            account_id=self.account1.id,
            run_uid="synthetic-cancel-before-claim",
            status="cancelling",
            started_at=datetime.utcnow(),
        )
        db.session.add(run)
        db.session.flush()
        item = AutoPublishItem(
            run_id=run.id,
            imported_product_id=product.id,
            seller_id=self.seller.id,
            marketplace_code="ozon",
            account_id=self.account1.id,
            draft_id=draft.id,
            draft_version=draft.version,
            status="processing",
            step="ready_to_submit",
        )
        db.session.add(item)
        db.session.commit()

        service = OzonAutoPublishService(self.seller, self.settings1)
        with patch(
            "services.marketplace_auto_publish."
            "MarketplacePublicationService.start_publication"
        ) as start:
            operation = service._submit_item(item, now=datetime.utcnow())

        self.assertIsNone(operation)
        start.assert_not_called()
        self.assertEqual(item.status, "skipped")
        self.assertEqual(item.step, "cancelled_before_write")
        self.assertIsNone(item.idempotency_key)

    def test_separate_auto_publish_flag_blocks_direct_run(self):
        self.app.config["MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED"] = False
        with self.assertRaises(MarketplaceAutoPublishError):
            OzonAutoPublishService(
                self.seller, self.settings1
            ).execute_run(triggered_by="manual")
        self.assertIsNone(self.settings1._run_lock_token)
        self.assertEqual(
            AutoPublishRun.query.filter_by(settings_id=self.settings1.id).count(),
            0,
        )

    def test_only_latest_attempt_controls_retry_exhaustion_and_cooldown(self):
        product = self._product(21)
        MarketplaceDraftProvisioner.provision(
            seller_id=self.seller.id,
            imported_product_ids=[product.id],
            account_ids=[self.account1.id],
        )
        draft = MarketplaceProductDraft.query.filter_by(
            account_id=self.account1.id,
            imported_product_id=product.id,
        ).one()
        old_run = AutoPublishRun(
            settings_id=self.settings1.id,
            seller_id=self.seller.id,
            marketplace_code="ozon",
            account_id=self.account1.id,
            run_uid="synthetic-old-exhausted-attempt",
            status="failed",
        )
        latest_run = AutoPublishRun(
            settings_id=self.settings1.id,
            seller_id=self.seller.id,
            marketplace_code="ozon",
            account_id=self.account1.id,
            run_uid="synthetic-latest-manual-retry",
            status="failed",
        )
        db.session.add_all([old_run, latest_run])
        db.session.flush()
        db.session.add_all([
            AutoPublishItem(
                run_id=old_run.id,
                imported_product_id=product.id,
                seller_id=self.seller.id,
                marketplace_code="ozon",
                account_id=self.account1.id,
                draft_id=draft.id,
                status="failed",
                step="failed",
                retry_count=3,
                next_retry_at=datetime.utcnow() + timedelta(hours=1),
            ),
            AutoPublishItem(
                run_id=latest_run.id,
                imported_product_id=product.id,
                seller_id=self.seller.id,
                marketplace_code="ozon",
                account_id=self.account1.id,
                draft_id=draft.id,
                status="failed",
                step="failed",
                retry_count=0,
                next_retry_at=None,
            ),
        ])
        db.session.commit()

        candidates = OzonAutoPublishService(
            self.seller, self.settings1
        )._candidate_drafts(now=datetime.utcnow())

        self.assertIn(draft.id, {candidate.id for candidate in candidates})

    def test_restart_keeps_ozon_item_for_reconciliation(self):
        product = self._product(22)
        MarketplaceDraftProvisioner.provision(
            seller_id=self.seller.id,
            imported_product_ids=[product.id],
            account_ids=[self.account1.id],
        )
        draft = MarketplaceProductDraft.query.filter_by(
            account_id=self.account1.id,
            imported_product_id=product.id,
        ).one()
        run = AutoPublishRun(
            settings_id=self.settings1.id,
            seller_id=self.seller.id,
            marketplace_code="ozon",
            account_id=self.account1.id,
            run_uid="synthetic-restart-reconcile",
            status="running",
        )
        db.session.add(run)
        db.session.flush()
        item = AutoPublishItem(
            run_id=run.id,
            imported_product_id=product.id,
            seller_id=self.seller.id,
            marketplace_code="ozon",
            account_id=self.account1.id,
            draft_id=draft.id,
            draft_version=draft.version,
            idempotency_key="synthetic-restart-boundary-key",
            status="processing",
            step="submitting",
        )
        self.settings1._run_lock_token = "stale-restart-token"
        db.session.add(item)
        db.session.commit()

        reset_stuck_auto_publish(self.app)

        db.session.refresh(run)
        db.session.refresh(item)
        db.session.refresh(self.settings1)
        db.session.refresh(product)
        self.assertEqual(run.status, "waiting")
        self.assertEqual(item.status, "processing")
        self.assertEqual(item.idempotency_key, "synthetic-restart-boundary-key")
        self.assertIsNone(self.settings1._run_lock_token)
        self.assertEqual(product.import_status, "pending")


if __name__ == "__main__":
    unittest.main()
