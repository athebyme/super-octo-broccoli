from datetime import date, datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    Marketplace,
    MarketplaceFinanceFact,
    MarketplaceFinanceSync,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_finance import register_marketplace_finance_routes
from services.marketplace_finance import MarketplaceFinanceService


class MarketplaceFinanceRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-finance-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_finance_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.seller1_id, self.user1_id = self._seller("fin-one", "fin-one@test.local")
            self.seller2_id, self.user2_id = self._seller("fin-two", "fin-two@test.local")
            marketplace = Marketplace(
                code="ozon",
                name="Ozon",
                adapter_code="ozon",
                is_active=True,
            )
            db.session.add(marketplace)
            db.session.flush()
            own = SellerMarketplaceAccount(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                external_account_id="own-finance",
                label="Own Finance",
                is_active=True,
                is_default=True,
                connection_status="connected",
            )
            foreign = SellerMarketplaceAccount(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                external_account_id="foreign-finance",
                label="Foreign Finance Secret Label",
                is_active=True,
                is_default=True,
                connection_status="connected",
            )
            db.session.add_all([own, foreign])
            db.session.flush()
            sync = MarketplaceFinanceSync(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=own.id,
                period_code="30d",
                period_start=date.today().replace(day=1),
                period_end=date.today(),
                status="completed",
                phase="completed",
                current_date=date.today(),
                contract_version=MarketplaceFinanceService.CONTRACT_VERSION,
                request_fingerprint=MarketplaceFinanceService._run_fingerprint(
                    date.today().replace(day=1),
                    date.today(),
                ),
                completed_at=datetime.utcnow(),
            )
            # Make the snapshot cover the exact rolling 30-day route period.
            from datetime import timedelta
            sync.period_start = date.today() - timedelta(days=29)
            sync.request_fingerprint = MarketplaceFinanceService._run_fingerprint(
                sync.period_start,
                sync.period_end,
            )
            db.session.add(sync)
            db.session.flush()
            fact = MarketplaceFinanceFact(
                sync_id=sync.id,
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=own.id,
                accrual_id="own-accrual",
                fact_date=date.today(),
                unit_number="own-posting",
                accrued_category="NON_ITEM",
                total_amount=-10,
                currency="RUB",
                amount_sign="negative",
                definition_code=MarketplaceFinanceService.DEFINITION_CODE,
                source_endpoint="/v1/finance/accrual/by-day",
                contract_version=MarketplaceFinanceService.CONTRACT_VERSION,
                source_fingerprint="a" * 64,
                observed_at=datetime.utcnow(),
            )
            db.session.add(fact)
            db.session.commit()
            self.own_account_id = own.id
            self.foreign_account_id = foreign.id
            self.fact_id = fact.id

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
            patch("routes.marketplace_finance.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_read_routes_are_exact_account_scoped(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            listing = self.client.get(
                f"/marketplaces/api/finance?account_id={self.own_account_id}",
            )
            detail = self.client.get(
                f"/marketplaces/api/finance/{self.fact_id}"
                f"?account_id={self.own_account_id}",
            )
            foreign = self.client.get(
                f"/marketplaces/api/finance?account_id={self.foreign_account_id}",
            )
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            listing.get_json()["data"]["items"][0]["accrual_id"],
            "own-accrual",
        )
        self.assertEqual(foreign.status_code, 404)
        encoded = json.dumps(foreign.get_json(), ensure_ascii=False)
        self.assertNotIn("Foreign Finance Secret Label", encoded)

    def test_scope_smuggling_and_loose_types_fail_before_service(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch.object(
            MarketplaceFinanceService,
            "sync_account",
        ) as sync:
            smuggled = self.client.post(
                f"/marketplaces/api/finance/sync?account_id={self.own_account_id}",
                json={
                    "period": "30d",
                    "force": False,
                    "account_id": self.foreign_account_id,
                },
            )
            loose_bool = self.client.post(
                f"/marketplaces/api/finance/sync?account_id={self.own_account_id}",
                json={"period": "30d", "force": "false"},
            )
            loose_pages = self.client.post(
                f"/marketplaces/api/finance/sync?account_id={self.own_account_id}",
                json={"period": "30d", "force": False, "max_pages": 1.5},
            )
            loose_type = self.client.get(
                f"/marketplaces/api/finance?account_id={self.own_account_id}"
                "&type_id=1.5",
            )
        self.assertEqual(smuggled.status_code, 400)
        self.assertEqual(loose_bool.status_code, 400)
        self.assertEqual(loose_pages.status_code, 400)
        self.assertEqual(loose_type.status_code, 400)
        sync.assert_not_called()

    def test_sync_passes_only_authenticated_query_scope(self):
        run = SimpleNamespace(to_public_dict=lambda: {
            "id": 900,
            "account_id": self.own_account_id,
            "status": "running",
        })
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch.object(
            MarketplaceFinanceService,
            "sync_account",
            return_value=run,
        ) as sync:
            response = self.client.post(
                f"/marketplaces/api/finance/sync?account_id={self.own_account_id}",
                json={"period": "7d", "force": True, "max_pages": 4},
            )
        self.assertEqual(response.status_code, 200)
        sync.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.own_account_id,
            period_code="7d",
            force=True,
            max_pages=4,
        )

    def test_feature_flag_blocks_api(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.get(
                f"/marketplaces/api/finance?account_id={self.own_account_id}",
            )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
