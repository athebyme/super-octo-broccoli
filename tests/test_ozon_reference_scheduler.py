# -*- coding: utf-8 -*-
"""Scheduler refreshes Ozon references only behind the dark-launch flag."""

from unittest.mock import patch
import os
import unittest

from cryptography.fernet import Fernet
from flask import Flask

from models import Marketplace, MarketplaceReferenceAccount, db
from services.product_sync_scheduler import (
    poll_ozon_commercial_operations,
    poll_ozon_marketplace_operations,
    sync_marketplace_characteristics,
    sync_marketplaces,
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
            db.session.commit()
            self.marketplace_id = marketplace.id

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


if __name__ == "__main__":
    unittest.main()
