# -*- coding: utf-8 -*-
"""WB-shaped admin actions must not accept an Ozon marketplace definition."""

from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flask import Flask
from flask_login import LoginManager

from models import Marketplace, db
from routes.marketplaces import register_marketplaces_routes


class AdminMarketplaceAdapterGuardTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="admin-marketplace-guard",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        register_marketplaces_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            marketplace = Marketplace(
                name="Ozon",
                code="ozon",
                adapter_code="ozon",
                is_active=True,
            )
            db.session.add(marketplace)
            db.session.commit()
            self.marketplace_id = marketplace.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    @staticmethod
    def _admin():
        return SimpleNamespace(
            id=1,
            is_authenticated=True,
            is_active=True,
            is_admin=True,
            seller=None,
        )

    def test_ozon_cannot_store_key_in_legacy_global_wb_field(self):
        user = self._admin()
        with patch("routes.marketplaces.current_user", user), patch(
            "flask_login.utils._get_user", return_value=user
        ):
            response = self.client.post(
                f"/admin/marketplaces/{self.marketplace_id}/settings",
                data={"api_key": "must-not-be-stored"},
            )
        self.assertEqual(response.status_code, 409)
        with self.app.app_context():
            marketplace = Marketplace.query.filter_by(
                id=self.marketplace_id
            ).one()
            self.assertIsNone(marketplace._api_key_encrypted)

    def test_ozon_cannot_enter_wb_reference_sync(self):
        user = self._admin()
        with patch("routes.marketplaces.current_user", user), patch(
            "flask_login.utils._get_user", return_value=user
        ), patch(
            "routes.marketplaces.MarketplaceService.sync_categories"
        ) as sync:
            response = self.client.post(
                f"/admin/marketplaces/{self.marketplace_id}/sync_categories"
            )
        self.assertEqual(response.status_code, 409)
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
