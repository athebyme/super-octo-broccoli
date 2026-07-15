# -*- coding: utf-8 -*-
"""Auto-publish HTTP APIs keep exact marketplace/account tenant scope."""

from datetime import datetime
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    AutoPublishItem,
    AutoPublishRun,
    AutoPublishSettings,
    ImportedProduct,
    Marketplace,
    Seller,
    SellerMarketplaceAccount,
    SellerSupplier,
    Supplier,
    User,
    db,
)
from routes.auto_publish import register_auto_publish_routes
from services.marketplace_auto_publish import OzonAutoPublishService


class AutoPublishMarketplaceRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="auto-publish-marketplace-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
            MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_auto_publish_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.seller1_id, self.user1_id = self._seller(
                "auto-route-one", "auto-route-one@test.local"
            )
            self.seller2_id, self.user2_id = self._seller(
                "auto-route-two", "auto-route-two@test.local"
            )
            marketplace = Marketplace(
                name="Ozon",
                code="ozon",
                adapter_code="ozon",
                is_active=True,
            )
            supplier1 = Supplier(name="Supplier One", code="auto-route-s1")
            supplier2 = Supplier(name="Supplier Two", code="auto-route-s2")
            db.session.add_all([marketplace, supplier1, supplier2])
            db.session.flush()
            self.supplier1_id = supplier1.id
            self.supplier2_id = supplier2.id
            db.session.add_all([
                SellerSupplier(
                    seller_id=self.seller1_id,
                    supplier_id=supplier1.id,
                    is_active=True,
                ),
                SellerSupplier(
                    seller_id=self.seller2_id,
                    supplier_id=supplier2.id,
                    is_active=True,
                ),
            ])
            account1 = self._account(
                self.seller1_id, marketplace.id, "Own One"
            )
            account2 = self._account(
                self.seller1_id, marketplace.id, "Own Two"
            )
            foreign = self._account(
                self.seller2_id, marketplace.id, "Foreign Secret"
            )
            db.session.flush()
            self.account1_id = account1.id
            self.account2_id = account2.id
            self.foreign_account_id = foreign.id
            settings1 = self._settings(self.seller1_id, account1.id)
            settings2 = self._settings(self.seller1_id, account2.id)
            foreign_settings = self._settings(self.seller2_id, foreign.id)
            product1 = ImportedProduct(
                seller_id=self.seller1_id,
                supplier_id=supplier1.id,
                external_id="auto-route-product-one",
                title="Own product",
                import_status="pending",
            )
            product2 = ImportedProduct(
                seller_id=self.seller1_id,
                supplier_id=supplier1.id,
                external_id="auto-route-product-two",
                title="Own product two",
                import_status="pending",
            )
            db.session.add_all([product1, product2])
            db.session.flush()
            self.product1_id = product1.id
            self.product2_id = product2.id
            run1 = self._run(settings1, self.seller1_id, account1.id)
            run2 = self._run(settings2, self.seller1_id, account2.id)
            foreign_run = self._run(
                foreign_settings, self.seller2_id, foreign.id
            )
            db.session.flush()
            self.settings1_id = settings1.id
            self.settings2_id = settings2.id
            self.run1_id = run1.id
            self.run2_id = run2.id
            self.foreign_run_id = foreign_run.id
            db.session.add(AutoPublishItem(
                run_id=run1.id,
                imported_product_id=product1.id,
                seller_id=self.seller1_id,
                marketplace_code="ozon",
                account_id=account1.id,
                status="pending",
                step="queued",
            ))
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    @staticmethod
    def _seller(username, email):
        user = User(username=username, email=email, is_active=True)
        user.set_password("synthetic-password")
        seller = Seller(user=user, company_name=username)
        db.session.add(seller)
        db.session.commit()
        return seller.id, user.id

    @staticmethod
    def _account(seller_id, marketplace_id, label):
        account = SellerMarketplaceAccount(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            external_account_id=f"client-{uuid.uuid4()}",
            label=label,
            is_active=True,
            connection_status="connected",
            _credentials_encrypted="synthetic-encrypted-credential",
        )
        db.session.add(account)
        return account

    @staticmethod
    def _settings(seller_id, account_id):
        settings = AutoPublishSettings(
            seller_id=seller_id,
            marketplace_code="ozon",
            account_id=account_id,
            is_enabled=True,
            validation_mode="strict",
        )
        db.session.add(settings)
        return settings

    @staticmethod
    def _run(settings, seller_id, account_id):
        run = AutoPublishRun(
            settings=settings,
            seller_id=seller_id,
            marketplace_code="ozon",
            account_id=account_id,
            run_uid=str(uuid.uuid4()),
            status="running",
            started_at=datetime.utcnow(),
        )
        db.session.add(run)
        return run

    @staticmethod
    def _user(seller_id, user_id):
        return SimpleNamespace(
            id=user_id,
            seller=SimpleNamespace(id=seller_id),
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _auth(self):
        user = self._user(self.seller1_id, self.user1_id)
        return (
            patch("routes.auto_publish.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_account_selector_isolates_settings_runs_and_foreign_scope(self):
        user_patch, login_patch = self._auth()
        scope1 = f"?marketplace=ozon&account_id={self.account1_id}"
        scope2 = f"?marketplace=ozon&account_id={self.account2_id}"
        foreign_scope = (
            f"?marketplace=ozon&account_id={self.foreign_account_id}"
        )
        with user_patch, login_patch:
            settings = self.client.get(
                "/api/auto-publish/settings" + scope1
            )
            runs = self.client.get("/api/auto-publish/runs" + scope1)
            hidden = self.client.get(
                f"/api/auto-publish/runs/{self.run1_id}" + scope2
            )
            foreign = self.client.get(
                "/api/auto-publish/settings" + foreign_scope
            )
            foreign_run = self.client.get(
                f"/api/auto-publish/runs/{self.foreign_run_id}" + scope1
            )

        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.get_json()["account_id"], self.account1_id)
        self.assertEqual(
            [row["id"] for row in runs.get_json()["runs"]],
            [self.run1_id],
        )
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign_run.status_code, 404)
        self.assertNotIn("Foreign Secret", foreign.get_data(as_text=True))

    def test_settings_contract_rejects_scope_smuggling_and_loose_values(self):
        with self.app.app_context():
            run = db.session.get(AutoPublishRun, self.run1_id)
            run.status = "completed"
            db.session.commit()
        user_patch, login_patch = self._auth()
        scope = f"?marketplace=ozon&account_id={self.account1_id}"
        with user_patch, login_patch:
            smuggled = self.client.post(
                "/api/auto-publish/settings" + scope,
                json={"account_id": self.account2_id, "batch_size": 5},
            )
            loose_number = self.client.post(
                "/api/auto-publish/settings" + scope,
                json={"batch_size": 2.5},
            )
            lenient = self.client.post(
                "/api/auto-publish/settings" + scope,
                json={"validation_mode": "lenient"},
            )
            duplicate_suppliers = self.client.post(
                "/api/auto-publish/settings" + scope,
                json={"supplier_ids": [self.supplier1_id, self.supplier1_id]},
            )
            foreign_supplier = self.client.post(
                "/api/auto-publish/settings" + scope,
                json={"supplier_ids": [self.supplier2_id]},
            )
            valid = self.client.post(
                "/api/auto-publish/settings" + scope,
                json={
                    "batch_size": 7,
                    "validation_mode": "strict",
                    "supplier_ids": [self.supplier1_id],
                    "notify_on_failure": False,
                },
            )

        self.assertEqual(smuggled.status_code, 400)
        self.assertEqual(loose_number.status_code, 400)
        self.assertEqual(lenient.status_code, 400)
        self.assertEqual(duplicate_suppliers.status_code, 400)
        self.assertEqual(foreign_supplier.status_code, 404)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.get_json()["settings"]["batch_size"], 7)
        self.assertFalse(valid.get_json()["settings"]["notify_on_failure"])

    def test_active_run_blocks_configuration_drift(self):
        user_patch, login_patch = self._auth()
        scope = f"?marketplace=ozon&account_id={self.account1_id}"
        with user_patch, login_patch:
            response = self.client.post(
                "/api/auto-publish/settings" + scope,
                json={"batch_size": 17},
            )

        self.assertEqual(response.status_code, 409)
        with self.app.app_context():
            settings = db.session.get(AutoPublishSettings, self.settings1_id)
            self.assertEqual(settings.batch_size, 10)

    def test_cancel_stops_unclaimed_writes_and_reconciles_claimed_boundary(self):
        with self.app.app_context():
            run = db.session.get(AutoPublishRun, self.run1_id)
            db.session.add_all([
                AutoPublishItem(
                    run_id=run.id,
                    imported_product_id=self.product2_id,
                    seller_id=self.seller1_id,
                    marketplace_code="ozon",
                    account_id=self.account1_id,
                    status="processing",
                    step="ready_to_submit",
                ),
                AutoPublishItem(
                    run_id=run.id,
                    imported_product_id=self.product2_id,
                    seller_id=self.seller1_id,
                    marketplace_code="ozon",
                    account_id=self.account1_id,
                    status="processing",
                    step="submitting",
                    idempotency_key="auto-route-claimed-boundary",
                ),
            ])
            db.session.commit()

        user_patch, login_patch = self._auth()
        scope = f"?marketplace=ozon&account_id={self.account1_id}"
        with user_patch, login_patch:
            response = self.client.post(
                f"/api/auto-publish/runs/{self.run1_id}/cancel" + scope
            )
            items_response = self.client.get(
                f"/api/auto-publish/runs/{self.run1_id}/items" + scope
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(items_response.status_code, 200)
        self.assertNotIn(
            "auto-route-claimed-boundary",
            items_response.get_data(as_text=True),
        )
        with self.app.app_context():
            run = db.session.get(AutoPublishRun, self.run1_id)
            items = AutoPublishItem.query.filter_by(run_id=run.id).order_by(
                AutoPublishItem.id.asc()
            ).all()
            self.assertEqual(run.status, "cancelling")
            self.assertEqual(
                [item.status for item in items],
                ["skipped", "skipped", "processing"],
            )
            seller = db.session.get(Seller, self.seller1_id)
            settings = db.session.get(AutoPublishSettings, self.settings1_id)
            OzonAutoPublishService(seller, settings)._reconcile_run(
                run,
                now=datetime.utcnow(),
            )
            self.assertEqual(run.status, "cancelled")
            self.assertTrue(all(item.status == "skipped" for item in items))
            other_run = db.session.get(AutoPublishRun, self.run2_id)
            self.assertEqual(other_run.status, "running")

    def test_separate_auto_publish_flag_blocks_enable(self):
        with self.app.app_context():
            settings = db.session.get(AutoPublishSettings, self.settings1_id)
            settings.is_enabled = False
            db.session.commit()
        self.app.config["MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED"] = False
        user_patch, login_patch = self._auth()
        scope = f"?marketplace=ozon&account_id={self.account1_id}"
        with user_patch, login_patch:
            response = self.client.post(
                "/api/auto-publish/toggle" + scope
            )

        self.assertEqual(response.status_code, 409)
        with self.app.app_context():
            settings = db.session.get(AutoPublishSettings, self.settings1_id)
            self.assertFalse(settings.is_enabled)


if __name__ == "__main__":
    unittest.main()
