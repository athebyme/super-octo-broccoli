# -*- coding: utf-8 -*-
"""Operational dashboard is useful, tenant-scoped and secret-free."""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import re
import unittest

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    Marketplace,
    MarketplaceCatalogSync,
    MarketplaceProjectionRun,
    Product,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_readiness import register_marketplace_readiness_routes
from services.marketplace_readiness import MarketplaceReadinessService
from services.marketplace_rollout import MarketplaceRolloutService


class MarketplaceReadinessTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-readiness",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
            MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=False,
            MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
            MARKETPLACE_WB_PROJECTION_ENABLED=True,
            MARKETPLACE_WB_DUAL_READ_ENABLED=True,
            MARKETPLACE_WB_COMMON_READ_ENABLED=False,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_readiness_routes(self.app)
        self._register_template_stubs()
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.seller1_id = self._seller("ready-one", "ready1@test.local")
        self.seller2_id = self._seller("ready-two", "ready2@test.local")
        wb = Marketplace(
            name="Wildberries",
            code="wb",
            adapter_code="wb",
            is_active=True,
        )
        ozon = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
            categories_sync_status="success",
            categories_synced_at=datetime(2026, 7, 15, 10, 0, 0),
            categories_version=4,
            categories_snapshot_hash="synthetic-reference-snapshot",
            total_categories=100,
            total_product_types=250,
        )
        db.session.add_all([wb, ozon])
        db.session.flush()
        account1 = SellerMarketplaceAccount(
            seller_id=self.seller1_id,
            marketplace_id=ozon.id,
            external_account_id="must-not-leak-client-id",
            label="Pilot Ozon",
            connection_status="connected",
            is_active=True,
        )
        account1._credentials_encrypted = "must-not-leak-ciphertext"
        wb_account = SellerMarketplaceAccount(
            seller_id=self.seller1_id,
            marketplace_id=wb.id,
            external_account_id="wb-only-account",
            label="Must not enter Ozon aggregates",
            connection_status="connected",
            is_active=True,
        )
        account2 = SellerMarketplaceAccount(
            seller_id=self.seller2_id,
            marketplace_id=ozon.id,
            external_account_id="foreign-client-id",
            label="Foreign Ozon",
            connection_status="invalid",
            is_active=True,
        )
        account2._credentials_encrypted = "foreign-ciphertext"
        product = Product(
            seller_id=self.seller1_id,
            nm_id=123456,
            vendor_code="ready-offer",
            title="Ready product",
            is_active=True,
            created_at=datetime(2026, 7, 1),
            updated_at=datetime(2026, 7, 1),
        )
        foreign_product = Product(
            seller_id=self.seller2_id,
            nm_id=654321,
            vendor_code="foreign-offer",
            title="Foreign product",
            is_active=True,
            created_at=datetime(2026, 7, 1),
            updated_at=datetime(2026, 7, 1),
        )
        db.session.add_all([
            account1,
            account2,
            wb_account,
            product,
            foreign_product,
        ])
        db.session.flush()
        db.session.add(MarketplaceCatalogSync(
            seller_id=self.seller1_id,
            marketplace_id=wb.id,
            account_id=wb_account.id,
            status="failed",
        ))
        db.session.commit()
        self.account1_id = account1.id
        self.account2_id = account2.id
        self.ready_at = datetime(2026, 7, 16, 10, 0, 0)
        MarketplaceRolloutService.run_backfill_batch(
            seller_id=self.seller1_id,
            now=self.ready_at,
        )
        MarketplaceRolloutService.run_parity_batch(
            seller_id=self.seller1_id,
            now=self.ready_at + timedelta(minutes=1),
        )

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
        return seller.id

    @staticmethod
    def _user(seller_id):
        return SimpleNamespace(
            id=seller_id,
            username="synthetic-user",
            seller=SimpleNamespace(id=seller_id),
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _auth(self, seller_id):
        user = self._user(seller_id)
        return (
            patch("routes.marketplace_readiness.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def _register_template_stubs(self):
        root = Path(__file__).resolve().parents[1]
        endpoint_names = set()
        for relative_path in (
            "templates/base.html",
            "templates/marketplace_readiness.html",
        ):
            endpoint_names.update(re.findall(
                r"url_for\(['\"]([^'\"]+)",
                (root / relative_path).read_text(encoding="utf-8"),
            ))

        def stub():
            return ""

        for index, endpoint in enumerate(sorted(endpoint_names)):
            if endpoint not in self.app.view_functions:
                self.app.add_url_rule(
                    f"/__template_stub/{index}",
                    endpoint=endpoint,
                    view_func=stub,
                )

    def test_readiness_is_green_and_never_serializes_credentials_or_external_id(self):
        document = MarketplaceReadinessService.build(
            seller_id=self.seller1_id,
            config=self.app.config,
            now=self.ready_at + timedelta(minutes=2),
        )
        serialized = json.dumps(document, ensure_ascii=False)
        self.assertTrue(document["stage_ready"])
        self.assertTrue(document["publication_ready"])
        self.assertEqual(document["ozon"]["accounts"]["connected"], 1)
        self.assertEqual(document["ozon"]["accounts"]["items"][0]["label"], "Pilot Ozon")
        self.assertEqual(document["ozon"]["syncs"]["catalog"]["total"], 0)
        self.assertNotIn("must-not-leak-ciphertext", serialized)
        self.assertNotIn("must-not-leak-client-id", serialized)
        self.assertNotIn("Must not enter Ozon aggregates", serialized)
        self.assertNotIn("foreign", serialized.lower())

    def test_expired_credential_and_uncut_foreign_seller_are_explicit_blockers(self):
        account = db.session.get(SellerMarketplaceAccount, self.account1_id)
        account.credential_expires_at = self.ready_at - timedelta(seconds=1)
        db.session.commit()
        expired = MarketplaceReadinessService.build(
            seller_id=self.seller1_id,
            config=self.app.config,
            now=self.ready_at,
        )
        foreign = MarketplaceReadinessService.build(
            seller_id=self.seller2_id,
            config=self.app.config,
            now=self.ready_at,
        )
        self.assertIn("ozon_credentials_expired", expired["blockers"])
        self.assertFalse(expired["stage_ready"])
        self.assertIn("wb_backfill_not_completed", foreign["blockers"])
        self.assertEqual(foreign["ozon"]["accounts"]["connected"], 0)

    def test_missing_credential_and_stale_reference_fail_closed(self):
        account = db.session.get(SellerMarketplaceAccount, self.account1_id)
        account._credentials_encrypted = None
        ozon = Marketplace.query.filter_by(code="ozon").one()
        ozon.categories_synced_at = self.ready_at - timedelta(hours=49)
        db.session.commit()

        document = MarketplaceReadinessService.build(
            seller_id=self.seller1_id,
            config=self.app.config,
            now=self.ready_at,
        )

        self.assertFalse(document["stage_ready"])
        self.assertFalse(document["ozon"]["reference"]["fresh"])
        self.assertIn("ozon_credentials_missing", document["blockers"])
        self.assertIn("ozon_reference_not_ready", document["blockers"])

    def test_json_route_is_tenant_scoped_and_local_actions_are_strict(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.get(
                "/marketplaces/readiness/",
                headers={"Accept": "application/json"},
            )
            loose = self.client.post(
                "/marketplaces/readiness/projection/backfill",
                json={"force_full": "true"},
            )
            self.app.config["MARKETPLACE_WB_DUAL_READ_ENABLED"] = False
            dual_disabled = self.client.post(
                "/marketplaces/readiness/projection/parity",
                json={},
            )
            self.app.config["MARKETPLACE_WB_DUAL_READ_ENABLED"] = True
            foreign_run = MarketplaceProjectionRun.query.filter_by(
                seller_id=self.seller2_id,
            ).first()
            if foreign_run is None:
                foreign_run = MarketplaceProjectionRun(
                    seller_id=self.seller2_id,
                    marketplace_id=Marketplace.query.filter_by(code="wb").one().id,
                    run_kind="wb_backfill",
                    status="paused",
                    target_product_id=1,
                )
                db.session.add(foreign_run)
                db.session.commit()
            denied = self.client.post(
                f"/marketplaces/readiness/projection/runs/{foreign_run.id}/resume",
                json={},
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()["readiness"]
        self.assertEqual(body["seller_id"], self.seller1_id)
        self.assertEqual(body["ozon"]["accounts"]["total"], 1)
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(dual_disabled.status_code, 409)
        self.assertEqual(
            dual_disabled.get_json()["code"],
            "wb_dual_read_disabled",
        )
        self.assertEqual(denied.status_code, 404)

    def test_html_dashboard_renders_operational_states_without_secret_values(self):
        user_patch, login_patch = self._auth(self.seller1_id)
        with user_patch, login_patch:
            response = self.client.get("/marketplaces/readiness/")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Готовность маркетплейсов", html)
        self.assertIn("Pilot Ozon", html)
        self.assertIn("данные совпадают", html)
        self.assertNotIn("must-not-leak-ciphertext", html)
        self.assertNotIn("must-not-leak-client-id", html)


if __name__ == "__main__":
    unittest.main()
