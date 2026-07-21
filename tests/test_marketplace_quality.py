from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import unittest

from flask import Flask

from models import (
    Marketplace,
    MarketplaceAnalyticsSync,
    MarketplaceAttributeDefinition,
    MarketplaceListing,
    MarketplaceMetricFact,
    MarketplaceProductType,
    MarketplaceQualityAssessment,
    MarketplaceTaxonomyCategory,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_quality import (
    MarketplaceQualityNotFound,
    MarketplaceQualityService,
    MarketplaceQualityValidationError,
)
from services.marketplace_analytics import MarketplaceAnalyticsService
from services.ozon_analytics_contracts import METRIC_BY_CODE, request_fingerprint


class MarketplaceQualityServiceTest(unittest.TestCase):
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
        self.now = datetime(2026, 7, 15, 12, 0, 0)

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
            categories_synced_at=self.now,
            categories_snapshot_hash="c" * 64,
        )
        db.session.add_all([
            user, self.seller, other_user, self.other_seller, self.marketplace,
        ])
        db.session.flush()
        self.account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="client-1",
            label="Ozon One",
            connection_status="connected",
            is_active=True,
            is_default=True,
        )
        self.other_account = SellerMarketplaceAccount(
            seller_id=self.other_seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="client-2",
            label="Ozon Two",
            connection_status="connected",
            is_active=True,
            is_default=True,
        )
        category = MarketplaceTaxonomyCategory(
            marketplace_id=self.marketplace.id,
            external_category_id="10",
            name="Категория",
            full_path="Категория",
            is_available=True,
            last_seen_at=self.now,
        )
        db.session.add_all([self.account, self.other_account, category])
        db.session.flush()
        self.product_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=category.id,
            external_type_id="777",
            name="Тип",
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
            attributes_synced_at=self.now,
            attributes_sync_status="success",
            attributes_schema_hash="s" * 64,
            attributes_count=2,
            required_attributes_count=1,
        )
        db.session.add(self.product_type)
        db.session.flush()
        db.session.add_all([
            MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                external_attribute_id="1",
                name="Обязательное",
                data_type="String",
                is_required=True,
                is_filterable=False,
                is_available=True,
                is_enabled=True,
            ),
            MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                external_attribute_id="2",
                name="Фильтр",
                data_type="String",
                is_required=False,
                is_filterable=True,
                is_available=True,
                is_enabled=True,
            ),
        ])
        self.listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            product_type_id=self.product_type.id,
            offer_id="offer-1",
            external_product_id="101",
            primary_sku="1101",
            external_category_id="10",
            external_type_id="777",
            title="Полезный тестовый товар Ozon",
            description="Подробное описание товара. " * 30,
            normalized_status="active",
            is_available=True,
            is_archived=False,
            attributes_json=json.dumps([{
                "id": "1",
                "complex_id": None,
                "values": [{"dictionary_value_id": None, "value": "Есть"}],
            }]),
            complex_attributes_json="[]",
            media_json=json.dumps({
                "primary_image": "https://img.test/1.jpg",
                "images": [
                    "https://img.test/1.jpg",
                    "https://img.test/2.jpg",
                    "https://img.test/3.jpg",
                    "https://img.test/4.jpg",
                    "https://img.test/5.jpg",
                ],
            }),
            barcodes_json='["4600000000001"]',
            price_summary_json=json.dumps({"available": True}),
            moderation_errors_json="[]",
            attributes_synced_at=self.now,
            sync_fingerprint="a" * 64,
        )
        db.session.add(self.listing)
        db.session.flush()
        self.analytics = MarketplaceAnalyticsSync(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            period_code="30d",
            period_start=date(2026, 6, 16),
            period_end=date(2026, 7, 15),
            status="completed",
            phase="completed",
            request_fingerprint=request_fingerprint(
                period_start=date(2026, 6, 16),
                period_end=date(2026, 7, 15),
            ),
            contract_version=MarketplaceAnalyticsService.CONTRACT_VERSION,
            metrics_json="[]",
            totals_json="{}",
            started_at=self.now,
            completed_at=self.now,
        )
        db.session.add(self.analytics)
        db.session.flush()
        for code, value in {
            "views": 10,
            "ordered_units": 0,
            "cart_conversion_percent": 2,
            "cancelled_units": 0,
            "delivered_units": 0,
            "returned_units": 0,
        }.items():
            definition = METRIC_BY_CODE[code]
            db.session.add(MarketplaceMetricFact(
                sync_id=self.analytics.id,
                seller_id=self.seller.id,
                marketplace_id=self.marketplace.id,
                account_id=self.account.id,
                listing_id=self.listing.id,
                dimension_kind="listing",
                dimension_id="1101",
                metric_code=definition.metric_code,
                provider_metric=definition.provider_metric,
                metric_value=Decimal(value),
                unit=definition.unit,
                definition_code=definition.definition_code,
                cross_marketplace_comparable=False,
            ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_fresh_schema_scores_required_and_filterable_separately(self):
        result = MarketplaceQualityService.recompute_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            listing_ids=[self.listing.id],
            limit=1,
            now=self.now,
        )
        self.assertEqual(result["scored"], 1)
        assessment = MarketplaceQualityAssessment.query.one()
        public = assessment.to_public_dict()
        self.assertEqual(public["entity_kind"], "marketplace_listing")
        self.assertEqual(public["account_id"], self.account.id)
        self.assertEqual(public["status"], "scored")
        codes = {item["code"] for item in public["reasons"]}
        self.assertIn("ozon_missing_filterable_attribute", codes)
        self.assertNotIn("ozon_missing_required_attribute", codes)
        self.assertIn("ozon_low_views", codes)
        self.assertFalse(public["metrics"]["cross_marketplace_comparable"])
        self.assertEqual(public["analytics_sync_id"], self.analytics.id)

    def test_stale_schema_never_returns_guessed_score(self):
        self.marketplace.categories_synced_at = self.now - timedelta(hours=49)
        db.session.commit()
        MarketplaceQualityService.recompute_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            listing_ids=[self.listing.id],
            limit=1,
            now=self.now,
        )
        assessment = MarketplaceQualityAssessment.query.one()
        self.assertEqual(assessment.status, "schema_stale")
        self.assertIsNone(assessment.score)
        reasons = json.loads(assessment.reasons_json)
        self.assertEqual(reasons[0]["code"], "ozon_schema_stale")

    def test_stale_listing_snapshot_never_returns_guessed_score(self):
        self.listing.attributes_synced_at = self.now - timedelta(hours=49)
        db.session.commit()
        MarketplaceQualityService.recompute_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            listing_ids=[self.listing.id],
            limit=1,
            now=self.now,
        )
        assessment = MarketplaceQualityAssessment.query.one()
        self.assertEqual(assessment.status, "unscorable")
        self.assertIsNone(assessment.score)
        reasons = json.loads(assessment.reasons_json)
        self.assertEqual(reasons[0]["code"], "ozon_listing_snapshot_stale")

    def test_stale_analytics_is_not_used_as_current_performance_signal(self):
        self.analytics.completed_at = self.now - timedelta(hours=5)
        db.session.commit()
        MarketplaceQualityService.recompute_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            listing_ids=[self.listing.id],
            limit=1,
            now=self.now,
        )
        assessment = MarketplaceQualityAssessment.query.one()
        reasons = json.loads(assessment.reasons_json)
        codes = {item["code"] for item in reasons}
        self.assertIn("ozon_no_analytics_signal", codes)
        self.assertNotIn("ozon_low_views", codes)
        self.assertIsNone(assessment.analytics_sync_id)

    def test_core_analytics_does_not_infer_deprecated_metrics_as_zero(self):
        MarketplaceMetricFact.query.filter(
            MarketplaceMetricFact.sync_id == self.analytics.id,
            MarketplaceMetricFact.metric_code != "ordered_units",
        ).delete(synchronize_session=False)
        db.session.commit()

        MarketplaceQualityService.recompute_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            listing_ids=[self.listing.id],
            limit=1,
            now=self.now,
        )
        assessment = MarketplaceQualityAssessment.query.one()
        codes = {item["code"] for item in json.loads(assessment.reasons_json)}
        self.assertNotIn("ozon_low_views", codes)
        self.assertNotIn("ozon_high_cancellation_rate", codes)
        self.assertNotIn("ozon_high_return_rate", codes)
        self.assertEqual(assessment.analytics_sync_id, self.analytics.id)

    def test_explicit_listing_ids_cannot_be_silently_paginated(self):
        with self.assertRaises(MarketplaceQualityValidationError):
            MarketplaceQualityService.recompute_account(
                seller_id=self.seller.id,
                account_id=self.account.id,
                listing_ids=[self.listing.id],
                limit=1,
                offset=1,
                now=self.now,
            )

    def test_exact_set_rejects_foreign_listing_without_partial_assessment(self):
        foreign = MarketplaceListing(
            seller_id=self.other_seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.other_account.id,
            offer_id="foreign",
            external_product_id="999",
            normalized_status="active",
            is_available=True,
            is_archived=False,
            sync_fingerprint="z" * 64,
        )
        db.session.add(foreign)
        db.session.commit()
        with self.assertRaises(MarketplaceQualityNotFound):
            MarketplaceQualityService.recompute_account(
                seller_id=self.seller.id,
                account_id=self.account.id,
                listing_ids=[self.listing.id, foreign.id],
                limit=2,
                now=self.now,
            )
        self.assertEqual(MarketplaceQualityAssessment.query.count(), 0)

    def test_list_keeps_marketplace_account_entity_scope(self):
        MarketplaceQualityService.recompute_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            listing_ids=[self.listing.id],
            limit=1,
            now=self.now,
        )
        data = MarketplaceQualityService.list_assessments(
            seller_id=self.seller.id,
            account_id=self.account.id,
            reason="ozon_missing_filterable_attribute",
        )
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["listing_id"], self.listing.id)
        self.assertEqual(data["summary"]["entity_kind"], "marketplace_listing")
        self.assertEqual(data["summary"]["account_id"], self.account.id)


if __name__ == "__main__":
    unittest.main()
