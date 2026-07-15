# -*- coding: utf-8 -*-
"""Commercial routes keep tenant, review, type and feature-flag boundaries."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    Marketplace,
    MarketplaceCommercialProposal,
    MarketplaceListing,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_commercial import register_marketplace_commercial_routes
from services.marketplace_commercial import MarketplaceCommercialService
from services.marketplace_warehouses import MarketplaceWarehouseService


class MarketplaceCommercialRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-commercial-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_commercial_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.seller1_id, self.user1_id = self._seller(
                "commercial-route-one",
                "commercial-route-one@test.local",
            )
            self.seller2_id, self.user2_id = self._seller(
                "commercial-route-two",
                "commercial-route-two@test.local",
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
                external_account_id="synthetic-client-one",
                label="Ozon One",
                is_active=True,
                connection_status="connected",
            )
            account2 = SellerMarketplaceAccount(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                external_account_id="synthetic-client-two",
                label="Ozon Two",
                is_active=True,
                connection_status="connected",
            )
            db.session.add_all([account1, account2])
            db.session.flush()
            listing1 = self._listing(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=account1.id,
                offer_id="own-commercial-offer",
                product_id="101",
            )
            listing2 = self._listing(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                account_id=account2.id,
                offer_id="foreign-commercial-secret",
                product_id="202",
            )
            db.session.add_all([listing1, listing2])
            db.session.flush()
            own = self._proposal(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=account1.id,
                listing_id=listing1.id,
                user_id=self.user1_id,
                idempotency_key="own-route-idempotency-secret",
                offer_id=listing1.offer_id,
                product_id=listing1.external_product_id,
            )
            foreign = self._proposal(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                account_id=account2.id,
                listing_id=listing2.id,
                user_id=self.user2_id,
                idempotency_key="foreign-route-idempotency-secret",
                offer_id=listing2.offer_id,
                product_id=listing2.external_product_id,
            )
            db.session.add_all([own, foreign])
            db.session.commit()
            self.account1_id = account1.id
            self.listing1_id = listing1.id
            self.own_proposal_id = own.id
            self.foreign_proposal_id = foreign.id

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
    def _listing(*, seller_id, marketplace_id, account_id, offer_id, product_id):
        return MarketplaceListing(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            account_id=account_id,
            offer_id=offer_id,
            external_product_id=product_id,
            normalized_status="active",
            is_available=True,
            price_summary_json="{}",
            stock_summary_json="{}",
            sync_fingerprint="a" * 64,
        )

    @staticmethod
    def _proposal(
        *,
        seller_id,
        marketplace_id,
        account_id,
        listing_id,
        user_id,
        idempotency_key,
        offer_id,
        product_id,
    ):
        before = {
            "kind": "price",
            "offer_id": offer_id,
            "product_id": product_id,
            "price": "1000",
            "old_price": "1500",
            "min_price": "800",
            "currency_code": "RUB",
        }
        proposed = dict(before, price="1100")
        return MarketplaceCommercialProposal(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            account_id=account_id,
            listing_id=listing_id,
            created_by_user_id=user_id,
            proposal_kind="price",
            source="user",
            status="pending_review",
            idempotency_key=idempotency_key,
            request_fingerprint="b" * 64,
            contract_version="synthetic-v1",
            baseline_fingerprint="c" * 64,
            proposed_fingerprint="d" * 64,
            baseline_state_json=json.dumps(before),
            proposed_state_json=json.dumps(proposed),
            guardrails_json="{}",
        )

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
            patch("routes.marketplace_commercial.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def _own_proposal(self):
        return db.session.get(
            MarketplaceCommercialProposal,
            self.own_proposal_id,
        )

    def test_detail_is_tenant_scoped_and_omits_internal_idempotency(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            own = self.client.get(
                f"/marketplaces/commercial/api/{self.own_proposal_id}",
            )
            foreign = self.client.get(
                f"/marketplaces/commercial/api/{self.foreign_proposal_id}",
            )
        self.assertEqual(own.status_code, 200)
        self.assertEqual(foreign.status_code, 404)
        encoded = json.dumps(own.get_json(), ensure_ascii=False)
        self.assertNotIn("idempotency", encoded)
        self.assertNotIn("foreign-commercial-secret", encoded)
        self.assertNotIn("foreign-route-idempotency-secret", foreign.get_data(as_text=True))

    def test_price_json_is_strict_and_uses_authenticated_author(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with self.app.app_context():
            proposal = self._own_proposal()
            with user_patch, login_patch, patch.object(
                MarketplaceCommercialService,
                "create_price_proposal",
                return_value=proposal,
            ) as create:
                loose = self.client.post(
                    f"/marketplaces/commercial/listings/{self.listing1_id}/price-proposals",
                    json={"price": 1100.0},
                )
                unknown = self.client.post(
                    f"/marketplaces/commercial/listings/{self.listing1_id}/price-proposals",
                    json={"price": "1100", "seller_id": self.seller2_id},
                )
                valid = self.client.post(
                    f"/marketplaces/commercial/listings/{self.listing1_id}/price-proposals",
                    json={
                        "price": "1100",
                        "idempotency_key": "route-price-create-01",
                    },
                )
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(valid.status_code, 201)
        create.assert_called_once_with(
            seller_id=self.seller1_id,
            listing_id=self.listing1_id,
            price="1100",
            source="user",
            idempotency_key="route-price-create-01",
            created_by_user_id=self.user1_id,
            allow_price_decrease=False,
            allow_large_change=False,
            guardrail_note=None,
        )

    def test_stock_json_rejects_loose_zero_but_accepts_exact_integer_zero(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with self.app.app_context():
            proposal = self._own_proposal()
            with user_patch, login_patch, patch.object(
                MarketplaceCommercialService,
                "create_stock_proposal",
                return_value=proposal,
            ) as create:
                loose = self.client.post(
                    f"/marketplaces/commercial/listings/{self.listing1_id}/stock-proposals",
                    json={"warehouse_id": 7, "stock": "0"},
                )
                valid = self.client.post(
                    f"/marketplaces/commercial/listings/{self.listing1_id}/stock-proposals",
                    json={"warehouse_id": 7, "stock": 0},
                )
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(valid.status_code, 201)
        create.assert_called_once_with(
            seller_id=self.seller1_id,
            listing_id=self.listing1_id,
            warehouse_id=7,
            stock=0,
            source="user",
            idempotency_key=None,
            created_by_user_id=self.user1_id,
        )

    def test_approve_requires_separate_flag_and_exact_version(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with self.app.app_context():
            proposal = self._own_proposal()
            with user_patch, login_patch, patch.object(
                MarketplaceCommercialService,
                "approve_proposal",
                return_value=proposal,
            ) as approve:
                disabled = self.client.post(
                    f"/marketplaces/commercial/{self.own_proposal_id}/approve",
                    json={
                        "expected_version": proposal.version,
                        "confirm_write": True,
                    },
                )
                self.app.config["MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED"] = True
                unconfirmed = self.client.post(
                    f"/marketplaces/commercial/{self.own_proposal_id}/approve",
                    json={"expected_version": proposal.version},
                )
                loose = self.client.post(
                    f"/marketplaces/commercial/{self.own_proposal_id}/approve",
                    json={
                        "expected_version": str(proposal.version),
                        "confirm_write": True,
                    },
                )
                valid = self.client.post(
                    f"/marketplaces/commercial/{self.own_proposal_id}/approve",
                    json={
                        "expected_version": proposal.version,
                        "confirm_write": True,
                    },
                )
        self.assertEqual(disabled.status_code, 404)
        self.assertEqual(unconfirmed.status_code, 400)
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(valid.status_code, 200)
        approve.assert_called_once_with(
            seller_id=self.seller1_id,
            proposal_id=self.own_proposal_id,
            expected_version=proposal.version,
            reviewed_by_user_id=self.user1_id,
        )

    def test_batch_approve_is_flagged_and_preserves_exact_typed_set(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with self.app.app_context():
            proposal = self._own_proposal()
            items = [{
                "proposal_id": proposal.id,
                "expected_version": proposal.version,
            }]
            with user_patch, login_patch, patch.object(
                MarketplaceCommercialService,
                "approve_proposals",
                return_value=[proposal],
            ) as approve:
                disabled = self.client.post(
                    "/marketplaces/commercial/batch-approve",
                    json={"items": items, "confirm_write": True},
                )
                self.app.config["MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED"] = True
                unconfirmed = self.client.post(
                    "/marketplaces/commercial/batch-approve",
                    json={"items": items},
                )
                unknown = self.client.post(
                    "/marketplaces/commercial/batch-approve",
                    json={
                        "items": items,
                        "confirm_write": True,
                        "seller_id": self.seller2_id,
                    },
                )
                valid = self.client.post(
                    "/marketplaces/commercial/batch-approve",
                    json={"items": items, "confirm_write": True},
                )
        self.assertEqual(disabled.status_code, 404)
        self.assertEqual(unconfirmed.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(valid.status_code, 200)
        approve.assert_called_once_with(
            seller_id=self.seller1_id,
            items=items,
            reviewed_by_user_id=self.user1_id,
        )

    def test_batch_approve_rejects_loose_ids_before_account_access(self):
        self.app.config["MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED"] = True
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            string_id = self.client.post(
                "/marketplaces/commercial/batch-approve",
                json={"items": [{
                    "proposal_id": str(self.own_proposal_id),
                    "expected_version": 1,
                }], "confirm_write": True},
            )
            boolean_version = self.client.post(
                "/marketplaces/commercial/batch-approve",
                json={"items": [{
                    "proposal_id": self.own_proposal_id,
                    "expected_version": True,
                }], "confirm_write": True},
            )
            duplicate = self.client.post(
                "/marketplaces/commercial/batch-approve",
                json={"items": [
                    {"proposal_id": self.own_proposal_id, "expected_version": 1},
                    {"proposal_id": self.own_proposal_id, "expected_version": 1},
                ], "confirm_write": True},
            )
        self.assertEqual(string_id.status_code, 400)
        self.assertEqual(boolean_version.status_code, 400)
        self.assertEqual(duplicate.status_code, 400)

    def test_warehouse_sync_accepts_only_empty_object(self):
        result = SimpleNamespace(to_public_dict=lambda: {"status": "completed"})
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceWarehouseService,
            "sync_warehouses",
            return_value=result,
        ) as sync:
            unknown = self.client.post(
                f"/marketplaces/commercial/accounts/{self.account1_id}/warehouses/sync",
                json={"seller_id": self.seller2_id},
            )
            valid = self.client.post(
                f"/marketplaces/commercial/accounts/{self.account1_id}/warehouses/sync",
                json={},
            )
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(valid.status_code, 200)
        sync.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.account1_id,
        )

    def test_unexpected_exception_is_redacted(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceCommercialService,
            "create_price_proposal",
            side_effect=RuntimeError("synthetic-private-provider-message"),
        ):
            response = self.client.post(
                f"/marketplaces/commercial/listings/{self.listing1_id}/price-proposals",
                json={"price": "1100"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(
            "synthetic-private-provider-message",
            response.get_data(as_text=True),
        )


if __name__ == "__main__":
    unittest.main()
