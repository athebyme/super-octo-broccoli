# -*- coding: utf-8 -*-
"""Marketplace operation routes enforce tenant, flag, type and CSRF scope."""

from datetime import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceListingSnapshot,
    MarketplaceOperation,
    MarketplaceProductDraft,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_operations import register_marketplace_operation_routes
from services.marketplace_publications import MarketplacePublicationService


class MarketplaceOperationRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-operation-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_operation_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.seller1_id, self.user1_id = self._seller(
                "operation-route-one",
                "operation-route-one@test.local",
            )
            self.seller2_id, self.user2_id = self._seller(
                "operation-route-two",
                "operation-route-two@test.local",
            )
            marketplace = Marketplace(
                name="Ozon",
                code="ozon",
                adapter_code="ozon",
                is_active=True,
            )
            db.session.add(marketplace)
            db.session.flush()
            account1 = SellerMarketplaceAccount(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                external_account_id="client-one",
                label="Ozon One",
                is_active=True,
                connection_status="connected",
            )
            account2 = SellerMarketplaceAccount(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                external_account_id="client-two",
                label="Ozon Two",
                is_active=True,
                connection_status="connected",
            )
            source1 = ImportedProduct(
                seller_id=self.seller1_id,
                external_id="route-source-one",
                title="Own source",
            )
            source2 = ImportedProduct(
                seller_id=self.seller2_id,
                external_id="route-source-two",
                title="Foreign source secret",
            )
            db.session.add_all([account1, account2, source1, source2])
            db.session.flush()
            draft1 = MarketplaceProductDraft(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=account1.id,
                imported_product_id=source1.id,
                offer_id="own-route-offer",
                status="ready",
                source_fact_hash="a" * 64,
                validation_status="valid",
            )
            draft2 = MarketplaceProductDraft(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                account_id=account2.id,
                imported_product_id=source2.id,
                offer_id="foreign-route-secret",
                status="ready",
                source_fact_hash="b" * 64,
                validation_status="valid",
            )
            db.session.add_all([draft1, draft2])
            db.session.flush()
            own_operation = self._operation(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=account1.id,
                draft=draft1,
                offer_id="own-route-offer",
                idempotency_key="route-key-own-0001",
            )
            foreign_operation = self._operation(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                account_id=account2.id,
                draft=draft2,
                offer_id="foreign-route-secret",
                idempotency_key="route-key-foreign-01",
            )
            db.session.commit()
            self.own_draft_id = draft1.id
            self.foreign_draft_id = draft2.id
            self.own_operation_id = own_operation.id
            self.foreign_operation_id = foreign_operation.id

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
    def _operation(
        *,
        seller_id,
        marketplace_id,
        account_id,
        draft,
        offer_id,
        idempotency_key,
    ):
        now = datetime.utcnow()
        summary = {
            "offer_id": offer_id,
            "attribute_count": 1,
            "image_count": 1,
        }
        operation = MarketplaceOperation(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            account_id=account_id,
            draft_id=draft.id,
            operation_kind="product_import",
            status="polling",
            idempotency_key=idempotency_key,
            request_fingerprint="c" * 64,
            contract_version="synthetic-contract",
            draft_version=draft.version,
            request_summary_json=json.dumps(summary),
            quota_snapshot_json="{}",
            provider_request_ids_json="[]",
            item_results_json="[]",
            external_task_id="123",
            next_poll_at=now,
            deadline_at=now,
        )
        db.session.add(operation)
        db.session.flush()
        snapshot = MarketplaceListingSnapshot(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            account_id=account_id,
            operation_id=operation.id,
            draft_id=draft.id,
            snapshot_kind="product_import",
            source_fingerprint=draft.source_fact_hash,
            submitted_fingerprint="c" * 64,
            submitted_state_json="{}",
            rollback_status="unavailable",
        )
        db.session.add(snapshot)
        return operation

    @staticmethod
    def _user(seller_id=None, user_id=10):
        seller = SimpleNamespace(id=seller_id) if seller_id else None
        return SimpleNamespace(
            id=user_id,
            seller=seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _auth(self, seller_id, user_id):
        user = self._user(seller_id, user_id)
        return (
            patch("routes.marketplace_operations.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_list_and_detail_hide_foreign_operations(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            listed = self.client.get(
                "/marketplaces/operations/",
                headers={"Accept": "application/json"},
            )
            own = self.client.get(
                f"/marketplaces/operations/api/{self.own_operation_id}",
            )
            foreign = self.client.get(
                f"/marketplaces/operations/api/{self.foreign_operation_id}",
            )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [item["id"] for item in listed.get_json()["items"]],
            [self.own_operation_id],
        )
        self.assertEqual(own.status_code, 200)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.content_type, "application/json")
        self.assertNotIn("foreign-route-secret", foreign.get_data(as_text=True))
        encoded = json.dumps(own.get_json(), ensure_ascii=False)
        self.assertNotIn("submitted_state_json", encoded)
        self.assertNotIn("idempotency_key", encoded)

    def test_publish_json_is_strict_and_uses_authenticated_author(self):
        operation = SimpleNamespace(
            id=99,
            is_terminal=False,
            snapshot=None,
            to_public_dict=lambda detail=False: {
                "id": 99,
                "status": "submitted",
            },
        )
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplacePublicationService,
            "start_publication",
            return_value=operation,
        ) as start:
            loose = self.client.post(
                f"/marketplaces/operations/drafts/{self.own_draft_id}/publish",
                json={
                    "expected_version": "1",
                    "idempotency_key": "route-publish-key-01",
                },
            )
            unknown = self.client.post(
                f"/marketplaces/operations/drafts/{self.own_draft_id}/publish",
                json={
                    "expected_version": 1,
                    "idempotency_key": "route-publish-key-01",
                    "seller_id": self.seller2_id,
                },
            )
            valid = self.client.post(
                f"/marketplaces/operations/drafts/{self.own_draft_id}/publish",
                json={
                    "expected_version": 1,
                    "idempotency_key": "route-publish-key-01",
                },
            )
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(valid.status_code, 202)
        start.assert_called_once_with(
            seller_id=self.seller1_id,
            draft_id=self.own_draft_id,
            expected_version=1,
            idempotency_key="route-publish-key-01",
            created_by_user_id=self.user1_id,
        )

    def test_publication_flag_blocks_new_write_but_poll_reconciles_only(self):
        self.app.config["MARKETPLACE_OZON_PUBLICATION_ENABLED"] = False
        operation = SimpleNamespace(
            id=self.own_operation_id,
            is_terminal=False,
            snapshot=None,
            to_public_dict=lambda detail=False: {
                "id": self.own_operation_id,
                "status": "polling",
            },
        )
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplacePublicationService,
            "start_publication",
        ) as start, patch.object(
            MarketplacePublicationService,
            "poll_operation",
            return_value=operation,
        ) as poll:
            disabled = self.client.post(
                f"/marketplaces/operations/drafts/{self.own_draft_id}/publish",
                json={
                    "expected_version": 1,
                    "idempotency_key": "route-disabled-key-01",
                },
            )
            reconciled = self.client.post(
                f"/marketplaces/operations/{self.own_operation_id}/poll",
                json={},
            )
        self.assertEqual(disabled.status_code, 404)
        self.assertEqual(reconciled.status_code, 200)
        start.assert_not_called()
        poll.assert_called_once_with(
            seller_id=self.seller1_id,
            operation_id=self.own_operation_id,
            allow_submission=False,
        )

    def test_non_seller_and_csrf_block_write(self):
        user = self._user()
        with patch("routes.marketplace_operations.current_user", user), patch(
            "flask_login.utils._get_user",
            return_value=user,
        ), patch.object(
            MarketplacePublicationService,
            "start_publication",
        ) as start:
            denied = self.client.post(
                f"/marketplaces/operations/drafts/{self.own_draft_id}/publish",
                json={
                    "expected_version": 1,
                    "idempotency_key": "route-non-seller-key",
                },
            )
        self.assertEqual(denied.status_code, 403)
        start.assert_not_called()

        self.app.config["WTF_CSRF_ENABLED"] = True
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplacePublicationService,
            "start_publication",
        ) as start:
            csrf = self.client.post(
                f"/marketplaces/operations/drafts/{self.own_draft_id}/publish",
                data={
                    "expected_version": "1",
                    "idempotency_key": "route-csrf-key-0001",
                },
            )
        self.assertEqual(csrf.status_code, 400)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
