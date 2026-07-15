# -*- coding: utf-8 -*-
"""Scheduler refreshes Ozon references only behind the dark-launch flag."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
import os
import unittest

from cryptography.fernet import Fernet
from flask import Flask

from models import (
    Marketplace,
    MarketplaceReferenceAccount,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.product_sync_scheduler import (
    poll_ozon_commercial_operations,
    poll_ozon_marketplace_operations,
    sync_marketplace_characteristics,
    sync_marketplaces,
    sync_ozon_analytics_accounts,
    sync_ozon_fulfillment_accounts,
)


class OzonReferenceSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.previous_encryption_key = os.environ.get("ENCRYPTION_KEY")
        os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        with self.app.app_context():
            db.create_all()
            marketplace = Marketplace(
                name="Ozon",
                code="ozon",
                adapter_code="ozon",
                is_active=True,
            )
            db.session.add(marketplace)
            db.session.flush()
            reference = MarketplaceReferenceAccount(
                marketplace_id=marketplace.id,
                external_account_id="123",
                connection_status="connected",
            )
            reference.set_credentials({"api_key": "scheduler-secret"})
            db.session.add(reference)
            user = User(
                username="ozon-scheduler",
                email="ozon-scheduler@test.local",
                is_active=True,
            )
            user.set_password("synthetic-password")
            seller = Seller(user=user, company_name="Ozon scheduler")
            db.session.add(seller)
            db.session.flush()
            account = SellerMarketplaceAccount(
                seller_id=seller.id,
                marketplace_id=marketplace.id,
                external_account_id="seller-123",
                label="Synthetic Ozon",
                is_active=True,
                connection_status="connected",
            )
            db.session.add(account)
            db.session.commit()
            self.marketplace_id = marketplace.id
            self.seller_id = seller.id
            self.account_id = account.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        if self.previous_encryption_key is None:
            os.environ.pop("ENCRYPTION_KEY", None)
        else:
            os.environ["ENCRYPTION_KEY"] = self.previous_encryption_key

    def test_tree_and_schema_refresh_use_bounded_ozon_services(self):
        with patch(
            "services.ozon_reference_service.OzonReferenceService.sync_tree",
            return_value={"success": True},
        ) as tree:
            sync_marketplaces(self.app)
        tree.assert_called_once_with(self.marketplace_id)

        with patch(
            "services.ozon_reference_service."
            "OzonReferenceService.sync_stale_enabled_types",
            return_value={
                "success": True,
                "failed": 0,
                "dictionaries_failed": 0,
            },
        ) as schemas:
            sync_marketplace_characteristics(self.app, limit=17)
        schemas.assert_called_once_with(self.marketplace_id, limit=17)

    def test_dark_launch_flag_blocks_scheduled_ozon_calls(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        with patch(
            "services.ozon_reference_service.OzonReferenceService.sync_tree"
        ) as tree, patch(
            "services.ozon_reference_service."
            "OzonReferenceService.sync_stale_enabled_types"
        ) as schemas:
            sync_marketplaces(self.app)
            sync_marketplace_characteristics(self.app)
        tree.assert_not_called()
        schemas.assert_not_called()

    def test_operation_reconciliation_survives_flag_disable_without_queued_write(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        self.app.config["MARKETPLACE_OZON_PUBLICATION_ENABLED"] = False
        with patch(
            "services.marketplace_publications."
            "MarketplacePublicationService.poll_due_operations",
            return_value={
                "selected": 1,
                "processed": 1,
                "busy": 0,
                "failed": 0,
            },
        ) as poll:
            result = poll_ozon_marketplace_operations(self.app, limit=7)
        self.assertEqual(result["processed"], 1)
        poll.assert_called_once_with(limit=7, allow_submission=False)

        self.app.config["MARKETPLACE_OZON_ENABLED"] = True
        self.app.config["MARKETPLACE_OZON_PUBLICATION_ENABLED"] = True
        with patch(
            "services.marketplace_publications."
            "MarketplacePublicationService.poll_due_operations",
            return_value={
                "selected": 0,
                "processed": 0,
                "busy": 0,
                "failed": 0,
            },
        ) as poll:
            poll_ozon_marketplace_operations(self.app)
        poll.assert_called_once_with(limit=20, allow_submission=True)

    def test_commercial_reconciliation_has_independent_dark_write_flag(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        self.app.config["MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED"] = False
        with patch(
            "services.marketplace_commercial."
            "MarketplaceCommercialService.poll_due_operations",
            return_value={
                "selected": 1,
                "processed": 1,
                "busy": 0,
                "failed": 0,
            },
        ) as poll:
            result = poll_ozon_commercial_operations(self.app, limit=7)
        self.assertEqual(result["processed"], 1)
        poll.assert_called_once_with(limit=7, allow_submission=False)

        self.app.config["MARKETPLACE_OZON_ENABLED"] = True
        self.app.config["MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED"] = True
        with patch(
            "services.marketplace_commercial."
            "MarketplaceCommercialService.poll_due_operations",
            return_value={
                "selected": 0,
                "processed": 0,
                "busy": 0,
                "failed": 0,
            },
        ) as poll:
            poll_ozon_commercial_operations(self.app)
        poll.assert_called_once_with(limit=20, allow_submission=True)

    def test_analytics_scheduler_is_bounded_read_only_and_refreshes_quality(self):
        completed_at = datetime.utcnow()
        run = SimpleNamespace(status="completed", completed_at=completed_at)
        with patch(
            "services.marketplace_analytics."
            "MarketplaceAnalyticsService._running_run",
            return_value=None,
        ), patch(
            "services.marketplace_analytics."
            "MarketplaceAnalyticsService._fresh_cached_sync",
            return_value=None,
        ), patch(
            "services.marketplace_analytics."
            "MarketplaceAnalyticsService.sync_account",
            return_value=run,
        ) as sync, patch(
            "services.marketplace_quality."
            "MarketplaceQualityService.recompute_account",
            return_value={"processed": 0},
        ) as quality:
            result = sync_ozon_analytics_accounts(self.app, limit=1)

        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["completed"], 1)
        sync.assert_called_once()
        sync_kwargs = sync.call_args.kwargs
        self.assertEqual(sync_kwargs["seller_id"], self.seller_id)
        self.assertEqual(sync_kwargs["account_id"], self.account_id)
        self.assertEqual(sync_kwargs["period_code"], "30d")
        self.assertFalse(sync_kwargs["force"])
        self.assertEqual(sync_kwargs["max_pages"], 2)
        quality.assert_called_once()

    def test_analytics_scheduler_flag_blocks_read_calls(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        with patch(
            "services.marketplace_analytics."
            "MarketplaceAnalyticsService.sync_account",
        ) as sync:
            result = sync_ozon_analytics_accounts(self.app)
        self.assertEqual(result["selected"], 0)
        sync.assert_not_called()

    def test_fulfillment_scheduler_is_bounded_and_read_only(self):
        run = SimpleNamespace(status="completed")
        with patch(
            "services.marketplace_fulfillment."
            "MarketplaceFulfillmentService._latest_completed",
            return_value=None,
        ), patch(
            "services.marketplace_fulfillment."
            "MarketplaceFulfillmentService.sync_account",
            return_value=run,
        ) as sync:
            result = sync_ozon_fulfillment_accounts(self.app, limit=1)

        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["completed"], 1)
        sync.assert_called_once()
        kwargs = sync.call_args.kwargs
        self.assertEqual(kwargs["seller_id"], self.seller_id)
        self.assertEqual(kwargs["account_id"], self.account_id)
        self.assertEqual(kwargs["period_code"], "30d")
        self.assertFalse(kwargs["force"])
        self.assertEqual(kwargs["max_pages"], 5)

    def test_fulfillment_scheduler_flag_blocks_read_calls(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        with patch(
            "services.marketplace_fulfillment."
            "MarketplaceFulfillmentService.sync_account",
        ) as sync:
            result = sync_ozon_fulfillment_accounts(self.app)
        self.assertEqual(result["selected"], 0)
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
