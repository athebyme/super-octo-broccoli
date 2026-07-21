from datetime import date, datetime
import json
import unittest

from flask import Flask

from models import (
    Marketplace,
    MarketplaceAnalyticsSync,
    MarketplaceListing,
    MarketplaceMetricFact,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_analytics import (
    MarketplaceAnalyticsConfigurationError,
    MarketplaceAnalyticsProtocolError,
    MarketplaceAnalyticsService,
)


SYNTHETIC_CREDENTIALS = MarketplaceCredentials(
    external_account_id="synthetic-client",
    api_key="synthetic-key",
)
METRICS = [1200, 4]


class SyntheticAnalyticsAdapter:
    capabilities = {"analytics_read"}

    def __init__(self, *, malformed=False):
        self.malformed = malformed
        self.payloads = []

    def require_capability(self, capability):
        if capability not in self.capabilities:
            raise AssertionError("missing capability")

    def read_analytics(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.payloads.append(payload)
        if self.malformed:
            return {"result": {"data": [], "totals": [1]}}
        dimension = payload["dimension"][0]
        dimension_id = "1101" if dimension == "sku" else payload["date_to"]
        return {
            "result": {
                "data": [{
                    "dimensions": [{
                        "id": dimension_id,
                        "name": "Тестовый товар" if dimension == "sku" else dimension_id,
                    }],
                    "metrics": list(METRICS),
                }],
                "totals": list(METRICS),
            },
            "timestamp": "2026-07-15T10:00:00Z",
        }


class MarketplaceAnalyticsServiceTest(unittest.TestCase):
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

        user = User(username="seller", email="seller@example.test")
        user.set_password("test-password")
        self.seller = Seller(user=user, company_name="Seller")
        other_user = User(username="other", email="other@example.test")
        other_user.set_password("test-password")
        self.other_seller = Seller(user=other_user, company_name="Other")
        self.marketplace = Marketplace(
            code="ozon",
            name="Ozon",
            is_active=True,
            adapter_code="ozon",
        )
        db.session.add_all([
            user, self.seller, other_user, self.other_seller, self.marketplace,
        ])
        db.session.flush()
        self.account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="synthetic-client",
            label="Основной Ozon",
            is_active=True,
            is_default=True,
            connection_status="connected",
        )
        self.other_account = SellerMarketplaceAccount(
            seller_id=self.other_seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="other-client",
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
            offer_id="offer-1",
            external_product_id="101",
            primary_sku="1101",
            identifiers_json=json.dumps({"sku": "1101", "sku_fbs": "2101"}),
            title="Тестовый товар",
            normalized_status="active",
            is_available=True,
            is_archived=False,
            sync_fingerprint="a" * 64,
        )
        db.session.add(self.listing)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_sync_persists_completed_definition_tagged_product_and_day_facts(self):
        adapter = SyntheticAnalyticsAdapter()
        run = MarketplaceAnalyticsService.sync_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="7d",
            force=True,
            max_pages=2,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 12, 0, 0),
            today=date(2026, 7, 15),
        )
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.phase, "completed")
        self.assertEqual(run.matched_rows, 1)
        self.assertEqual(run.unmatched_rows, 0)
        self.assertEqual(run.fact_count, 4)
        self.assertEqual(len(adapter.payloads), 2)
        self.assertEqual(adapter.payloads[0]["dimension"], ["sku"])
        self.assertEqual(adapter.payloads[1]["dimension"], ["day"])

        facts = MarketplaceMetricFact.query.filter_by(sync_id=run.id).all()
        self.assertEqual(len(facts), 4)
        self.assertTrue(all(not item.cross_marketplace_comparable for item in facts))
        self.assertTrue(all(
            item.definition_code.startswith("ozon.analytics.v1/")
            for item in facts
        ))
        product_fact = MarketplaceMetricFact.query.filter_by(
            sync_id=run.id,
            dimension_kind="listing",
            metric_code="ordered_units",
        ).one()
        self.assertEqual(product_fact.listing_id, self.listing.id)

        summary = MarketplaceAnalyticsService.get_summary(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="7d",
            sync_id=run.id,
            now=datetime(2026, 7, 15, 12, 5, 0),
        )
        self.assertEqual(summary["scope"]["account_id"], self.account.id)
        self.assertFalse(summary["scope"]["cross_marketplace_comparable"])
        self.assertEqual(summary["kpi"]["orders"], 4.0)
        self.assertEqual(summary["topProducts"][0]["listing_id"], self.listing.id)
        self.assertEqual(summary["topProducts"][0]["entity_kind"], "marketplace_listing")

    def test_failed_force_refresh_does_not_replace_last_completed_snapshot(self):
        good = SyntheticAnalyticsAdapter()
        completed = MarketplaceAnalyticsService.sync_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="7d",
            force=True,
            max_pages=2,
            adapter=good,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 12, 0, 0),
            today=date(2026, 7, 15),
        )
        with self.assertRaises(MarketplaceAnalyticsProtocolError):
            MarketplaceAnalyticsService.sync_account(
                seller_id=self.seller.id,
                account_id=self.account.id,
                period_code="7d",
                force=True,
                max_pages=1,
                adapter=SyntheticAnalyticsAdapter(malformed=True),
                credentials=SYNTHETIC_CREDENTIALS,
                now=datetime(2026, 7, 15, 13, 0, 0),
                today=date(2026, 7, 15),
            )
        latest = MarketplaceAnalyticsService.latest_completed_sync(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="7d",
        )
        self.assertEqual(latest.id, completed.id)
        failed = MarketplaceAnalyticsSync.query.filter_by(status="failed").one()
        self.assertNotEqual(failed.id, completed.id)
        self.assertEqual(failed.error_code, "ozon_analytics_protocol_error")

    def test_ambiguous_local_sku_fails_before_provider_read(self):
        duplicate = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            offer_id="offer-2",
            external_product_id="102",
            primary_sku="1101",
            identifiers_json="{}",
            normalized_status="active",
            is_available=True,
            is_archived=False,
            sync_fingerprint="b" * 64,
        )
        db.session.add(duplicate)
        db.session.commit()
        adapter = SyntheticAnalyticsAdapter()
        with self.assertRaises(MarketplaceAnalyticsConfigurationError):
            MarketplaceAnalyticsService.sync_account(
                seller_id=self.seller.id,
                account_id=self.account.id,
                period_code="30d",
                force=True,
                max_pages=1,
                adapter=adapter,
                credentials=SYNTHETIC_CREDENTIALS,
                now=datetime(2026, 7, 15, 12, 0, 0),
                today=date(2026, 7, 15),
            )
        self.assertEqual(adapter.payloads, [])

    def test_foreign_account_scope_is_never_read(self):
        with self.assertRaises(Exception):
            MarketplaceAnalyticsService.get_summary(
                seller_id=self.seller.id,
                account_id=self.other_account.id,
                period_code="30d",
            )
        with self.assertRaises(Exception):
            MarketplaceAnalyticsService.get_products(
                seller_id=self.seller.id,
                account_id=self.other_account.id,
                period_code="30d",
            )
        self.assertEqual(MarketplaceMetricFact.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
