from datetime import date, datetime
import json
import unittest
from unittest.mock import patch

from flask import Flask

from models import (
    Marketplace,
    MarketplaceFinanceComponent,
    MarketplaceFinanceFact,
    MarketplaceFinanceFactItem,
    MarketplaceFinanceSync,
    MarketplaceListing,
    MarketplacePosting,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_finance import (
    MarketplaceFinanceNotFound,
    MarketplaceFinanceProtocolError,
    MarketplaceFinanceService,
)


SYNTHETIC_CREDENTIALS = MarketplaceCredentials(
    external_account_id="synthetic-finance-client",
    api_key="synthetic-finance-key",
)


class SyntheticFinanceAdapter:
    capabilities = {"finance_read"}

    def __init__(self, *, malformed_date=None):
        self.malformed_date = malformed_date
        self.calls = []

    def require_capability(self, capability):
        if capability not in self.capabilities:
            raise AssertionError("missing capability")

    def read_finance_accrual_types(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        assert payload == {}
        self.calls.append(("types", payload))
        return {
            "accrual_types": [{
                "id": 7,
                "name": "Synthetic service fee",
                "description": "Synthetic only",
            }],
        }

    def read_finance_accrual_by_day(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        day = payload["date"]
        self.calls.append((day, payload))
        if self.malformed_date == day:
            return {
                "accruals": [{
                    "type_id": 999,
                    "date": day,
                    "accrued_category": "POSTING",
                    "total_amount": {"amount": "1", "currency": "RUB"},
                }],
                "last_id": None,
            }
        day_number = int(day.replace("-", ""))
        if day == "2026-07-10":
            amount, currency = "-40", "RUB"
        elif day == "2026-07-11":
            amount, currency = "10", "USD"
        else:
            amount, currency = "100", "RUB"
        return {
            "accruals": [{
                "accrual_id": day_number,
                "date": day,
                "unit_number": "100-1-1",
                "accrued_category": "POSTING",
                "total_amount": {"amount": amount, "currency": currency},
                "posting": {
                    "products": [{
                        "sku": 101,
                        "commission": {
                            "sale_amount": {"amount": amount, "currency": currency},
                        },
                        "delivery": {
                            "services": [{
                                "type_id": 7,
                                "accrued": {"amount": "-5", "currency": currency},
                            }],
                        },
                    }],
                },
                "item_fees": None,
                "non_item_fee": None,
                "buyer": {"phone": "must never persist"},
            }],
            "last_id": None,
        }


class MarketplaceFinanceServiceTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user = User(username="finance", email="finance@example.test")
        user.set_password("synthetic-password")
        self.seller = Seller(user=user, company_name="Finance Seller")
        other_user = User(username="finance-other", email="other-finance@example.test")
        other_user.set_password("synthetic-password")
        self.other_seller = Seller(user=other_user, company_name="Other")
        self.marketplace = Marketplace(
            code="ozon",
            name="Ozon",
            is_active=True,
            adapter_code="ozon",
        )
        db.session.add_all([
            user,
            self.seller,
            other_user,
            self.other_seller,
            self.marketplace,
        ])
        db.session.flush()
        self.account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="synthetic-finance-client",
            label="Finance Ozon",
            is_active=True,
            is_default=True,
            connection_status="connected",
        )
        self.other_account = SellerMarketplaceAccount(
            seller_id=self.other_seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="other-finance-client",
            label="Other Ozon",
            is_active=True,
            is_default=True,
            connection_status="connected",
        )
        db.session.add_all([self.account, self.other_account])
        db.session.flush()
        self.listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            offer_id="offer-finance",
            external_product_id="product-finance",
            primary_sku="101",
            identifiers_json=json.dumps({"sku": "101"}),
            title="Synthetic finance product",
            normalized_status="active",
            is_available=True,
            is_archived=False,
            sync_fingerprint="a" * 64,
        )
        self.posting = MarketplacePosting(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            posting_number="100-1-1",
            fulfillment_kind="fbs",
            status="delivered",
            source_endpoint="/v4/posting/fbs/list",
            sync_fingerprint="b" * 64,
            last_seen_at=datetime(2026, 7, 15, 10, 0, 0),
        )
        db.session.add_all([self.listing, self.posting])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _sync(self, adapter=None, **kwargs):
        return MarketplaceFinanceService.sync_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code=kwargs.pop("period_code", "7d"),
            force=kwargs.pop("force", True),
            max_pages=kwargs.pop("max_pages", 10),
            adapter=adapter or SyntheticFinanceAdapter(),
            credentials=SYNTHETIC_CREDENTIALS,
            now=kwargs.pop("now", datetime(2026, 7, 15, 12, 0, 0)),
            today=kwargs.pop("today", date(2026, 7, 15)),
            **kwargs,
        )

    def test_completed_snapshot_persists_exact_safe_facts(self):
        adapter = SyntheticFinanceAdapter()
        run = self._sync(adapter)

        self.assertEqual(run.status, "completed")
        self.assertEqual(run.phase, "completed")
        self.assertEqual(run.page_count, 8)
        self.assertEqual(run.fact_count, 7)
        self.assertEqual(run.item_count, 7)
        self.assertEqual(run.component_count, 7)
        self.assertEqual(run.matched_item_count, 7)
        self.assertEqual(MarketplaceFinanceFact.query.count(), 7)
        self.assertEqual(MarketplaceFinanceFactItem.query.count(), 7)
        self.assertEqual(MarketplaceFinanceComponent.query.count(), 7)

        fact = MarketplaceFinanceFact.query.filter_by(
            fact_date=date(2026, 7, 10)
        ).one()
        self.assertEqual(str(fact.total_amount), "-40.0000")
        self.assertEqual(fact.amount_sign, "negative")
        self.assertEqual(fact.posting_id, self.posting.id)
        self.assertEqual(fact.items.one().listing_id, self.listing.id)
        component = fact.components.one()
        self.assertEqual(component.type_name, "Synthetic service fee")
        self.assertEqual(component.rollup_role, "explanatory_only")

        columns = {
            column.name
            for model in (
                MarketplaceFinanceFact,
                MarketplaceFinanceFactItem,
                MarketplaceFinanceComponent,
            )
            for column in model.__table__.columns
        }
        for forbidden in (
            "buyer", "phone", "address", "raw_response", "raw_payload",
            "financial_data", "commission_json",
        ):
            self.assertNotIn(forbidden, columns)

    def test_snapshot_resumes_by_current_day(self):
        adapter = SyntheticFinanceAdapter()
        first = self._sync(adapter, max_pages=3)
        self.assertEqual(first.status, "running")
        self.assertEqual(first.phase, "accruals")
        self.assertEqual(first.current_date, date(2026, 7, 11))
        resumed = self._sync(
            adapter,
            force=False,
            max_pages=5,
            now=datetime(2026, 7, 15, 12, 1, 0),
        )
        self.assertEqual(resumed.id, first.id)
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(len(adapter.calls), 8)

    def test_type_dictionary_is_loaded_once_per_bounded_sync_call(self):
        adapter = SyntheticFinanceAdapter()
        with patch.object(
            MarketplaceFinanceService,
            "_type_names",
            wraps=MarketplaceFinanceService._type_names,
        ) as type_names:
            self._sync(adapter)
        self.assertEqual(type_names.call_count, 1)

    def test_calendar_rollover_keeps_overlapping_last_good_visible(self):
        completed = self._sync(SyntheticFinanceAdapter())
        partial = self._sync(
            SyntheticFinanceAdapter(),
            max_pages=1,
            now=datetime(2026, 7, 16, 9, 0, 0),
            today=date(2026, 7, 16),
        )
        self.assertEqual(partial.status, "running")

        result = MarketplaceFinanceService.list_facts(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="7d",
            today=date(2026, 7, 16),
        )

        self.assertEqual(result["snapshot_sync"]["id"], completed.id)
        self.assertEqual(result["sync"]["id"], partial.id)
        self.assertEqual(result["pagination"]["total"], 6)
        self.assertEqual(result["coverage"]["snapshot_end"], "2026-07-15")
        self.assertEqual(result["coverage"]["requested_end"], "2026-07-16")
        self.assertFalse(result["coverage"]["complete"])

    def test_failed_partial_snapshot_never_replaces_last_good(self):
        completed = self._sync(SyntheticFinanceAdapter())
        partial = self._sync(
            SyntheticFinanceAdapter(malformed_date="2026-07-10"),
            max_pages=2,
            now=datetime(2026, 7, 15, 13, 0, 0),
        )
        self.assertEqual(partial.status, "running")
        with self.assertRaises(MarketplaceFinanceProtocolError):
            self._sync(
                SyntheticFinanceAdapter(malformed_date="2026-07-10"),
                force=False,
                max_pages=1,
                now=datetime(2026, 7, 15, 13, 1, 0),
            )
        failed = MarketplaceFinanceSync.query.filter_by(status="failed").one()
        self.assertNotEqual(failed.id, completed.id)
        result = MarketplaceFinanceService.list_facts(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="7d",
            today=date(2026, 7, 15),
        )
        self.assertEqual(result["snapshot_sync"]["id"], completed.id)
        self.assertEqual(result["sync"]["status"], "failed")
        self.assertEqual(result["pagination"]["total"], 7)

    def test_totals_never_mix_currency_or_fee_components(self):
        self._sync(SyntheticFinanceAdapter())
        result = MarketplaceFinanceService.list_facts(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="7d",
            today=date(2026, 7, 15),
        )
        totals = {item["currency"]: item for item in result["totals"]}
        self.assertEqual(totals["RUB"]["positive"], "500.0000")
        self.assertEqual(totals["RUB"]["negative"], "-40.0000")
        self.assertEqual(totals["RUB"]["net"], "460.0000")
        self.assertEqual(totals["USD"]["net"], "10.0000")
        self.assertEqual(result["definitions"]["component_rollup"], "forbidden")
        self.assertFalse(result["definitions"]["profit"])

    def test_ambiguous_sku_is_visible_but_never_mislinked(self):
        duplicate = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            offer_id="duplicate-offer",
            external_product_id="duplicate-product",
            primary_sku="101",
            identifiers_json="{}",
            title="Duplicate SKU",
            normalized_status="active",
            is_available=True,
            is_archived=False,
            sync_fingerprint="c" * 64,
        )
        db.session.add(duplicate)
        db.session.commit()
        run = self._sync(SyntheticFinanceAdapter())
        self.assertEqual(run.ambiguous_item_count, 7)
        item = MarketplaceFinanceFactItem.query.first()
        self.assertEqual(item.match_status, "ambiguous")
        self.assertIsNone(item.listing_id)

    def test_read_models_enforce_tenant_and_completed_scope(self):
        run = self._sync(SyntheticFinanceAdapter(), max_pages=2)
        self.assertEqual(run.status, "running")
        fact = MarketplaceFinanceFact.query.first()
        with self.assertRaises(MarketplaceFinanceNotFound):
            MarketplaceFinanceService.get_fact(
                seller_id=self.seller.id,
                account_id=self.account.id,
                fact_id=fact.id,
            )
        with self.assertRaises(MarketplaceFinanceNotFound):
            MarketplaceFinanceService.list_facts(
                seller_id=self.other_seller.id,
                account_id=self.account.id,
            )


if __name__ == "__main__":
    unittest.main()
