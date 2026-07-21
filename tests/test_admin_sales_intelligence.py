# -*- coding: utf-8 -*-
from datetime import date, datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask

from models import (
    AdminAuditLog,
    BestsellerImageRecommendation,
    ImportedProduct,
    Marketplace,
    MarketplaceAnalyticsSync,
    MarketplaceListing,
    MarketplaceMetricFact,
    MarketplaceQualityAssessment,
    Product,
    Seller,
    SellerMarketplaceAccount,
    User,
    WBSale,
    db,
)
from services.admin_sales_intelligence import (
    AdminSalesIntelligenceError,
    AdminSalesIntelligenceService,
    SalesDashboardFilters,
)


class AdminSalesIntelligenceTests(unittest.TestCase):
    NOW = datetime(2026, 7, 17, 12, 0, 0)

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self._seed()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _user_and_seller(self, username, *, admin=False, wb=False):
        user = User(
            username=username,
            email=f"{username}@test.local",
            is_admin=admin,
            is_active=True,
        )
        user.set_password("synthetic-password")
        seller = Seller(user=user, company_name=f"Компания {username}")
        if wb:
            seller._wb_api_key_encrypted = "encrypted-wb-key"
        db.session.add(seller)
        db.session.flush()
        return user, seller

    def _seed(self):
        self.admin_user, self.admin_seller = self._user_and_seller(
            "admin", admin=True,
        )
        self.seller_user, self.seller = self._user_and_seller("seller", wb=True)
        self.foreign_user, self.foreign_seller = self._user_and_seller("foreign")

        self.wb_product = Product(
            seller_id=self.seller.id,
            nm_id=10101,
            title="Сильный товар WB",
            vendor_code="WB-101",
            quality_score=61,
            photos_json=json.dumps(["https://img.test/wb.jpg"]),
        )
        db.session.add(self.wb_product)
        db.session.flush()
        self.wb_imported = ImportedProduct(
            seller_id=self.seller.id,
            product_id=self.wb_product.id,
            external_id="source-wb",
            title=self.wb_product.title,
            photo_urls=json.dumps(["https://img.test/wb.jpg"]),
            import_status="imported",
        )
        db.session.add(self.wb_imported)
        for srid, price, is_return in (
            ("sale-1", 100, False),
            ("sale-2", 120, False),
            ("return-1", 100, True),
        ):
            db.session.add(WBSale(
                seller_id=self.seller.id,
                srid=srid,
                sale_id=("R-" if is_return else "S-") + srid,
                nm_id=self.wb_product.nm_id,
                date=self.NOW,
                last_change_date=self.NOW,
                finished_price=price,
                price_with_disc=price,
                is_return=is_return,
            ))

        marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add(marketplace)
        db.session.flush()
        self.ozon_account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=marketplace.id,
            external_account_id="client-1",
            label="Основной Ozon",
            is_active=True,
            connection_status="connected",
        )
        db.session.add(self.ozon_account)
        db.session.flush()
        self.ozon_imported = ImportedProduct(
            seller_id=self.seller.id,
            external_id="source-ozon",
            title="Сильный товар Ozon",
            photo_urls=json.dumps([
                "https://img.test/ozon-1.jpg",
                "https://img.test/ozon-2.jpg",
            ]),
            import_status="validated",
        )
        db.session.add(self.ozon_imported)
        db.session.flush()
        self.ozon_listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=marketplace.id,
            account_id=self.ozon_account.id,
            imported_product_id=self.ozon_imported.id,
            offer_id="OZ-202",
            external_product_id="202",
            title=self.ozon_imported.title,
            normalized_status="active",
            is_available=True,
            is_archived=False,
            link_status="linked",
            sync_fingerprint="a" * 64,
        )
        db.session.add(self.ozon_listing)
        db.session.flush()
        sync = MarketplaceAnalyticsSync(
            seller_id=self.seller.id,
            marketplace_id=marketplace.id,
            account_id=self.ozon_account.id,
            period_code="30d",
            period_start=date(2026, 6, 18),
            period_end=date(2026, 7, 17),
            status="completed",
            phase="completed",
            request_fingerprint="b" * 64,
            completed_at=self.NOW,
        )
        db.session.add(sync)
        db.session.flush()
        for code, provider, value, unit, definition in (
            ("ordered_revenue_rub", "revenue", 900, "rub", "ozon.analytics.v1/revenue"),
            ("ordered_units", "ordered_units", 5, "count", "ozon.analytics.v1/ordered_units"),
            ("returned_units", "returns", 1, "count", "ozon.analytics.v1/returns"),
        ):
            db.session.add(MarketplaceMetricFact(
                sync_id=sync.id,
                seller_id=self.seller.id,
                marketplace_id=marketplace.id,
                account_id=self.ozon_account.id,
                listing_id=self.ozon_listing.id,
                dimension_kind="listing",
                dimension_id="sku-202",
                metric_code=code,
                provider_metric=provider,
                metric_value=value,
                unit=unit,
                definition_code=definition,
                cross_marketplace_comparable=False,
                observed_at=self.NOW,
            ))
        db.session.add(MarketplaceQualityAssessment(
            seller_id=self.seller.id,
            marketplace_id=marketplace.id,
            account_id=self.ozon_account.id,
            listing_id=self.ozon_listing.id,
            status="scored",
            severity="warning",
            score=67,
            impact=10,
            listing_fingerprint=self.ozon_listing.sync_fingerprint,
        ))
        db.session.commit()

    def test_dashboard_keeps_marketplace_definitions_separate(self):
        result = AdminSalesIntelligenceService.dashboard(
            SalesDashboardFilters(),
            include_ozon=True,
            now=self.NOW,
        )

        self.assertEqual({item["marketplace_code"] for item in result["items"]}, {"wb", "ozon"})
        self.assertFalse(result["comparison"]["cross_marketplace_financial_rollup"])
        self.assertEqual(result["comparison"]["rank_scope"], "marketplace")
        self.assertEqual(len(result["summaries"]), 2)
        wb = next(item for item in result["items"] if item["marketplace_code"] == "wb")
        ozon = next(item for item in result["items"] if item["marketplace_code"] == "ozon")
        self.assertEqual(wb["units"], 1)
        self.assertEqual(wb["revenue_rub"], 120)
        self.assertEqual(ozon["units"], 5)
        self.assertEqual(ozon["revenue_rub"], 900)
        self.assertNotEqual(
            wb["metric_definitions"]["revenue"]["code"],
            ozon["metric_definitions"]["revenue"]["code"],
        )
        self.assertTrue(wb["recommendable"])
        self.assertTrue(ozon["recommendable"])
        self.assertEqual(wb["rank_scope"], "marketplace")

    def test_admin_handoff_is_local_bounded_and_seller_scoped(self):
        filters = SalesDashboardFilters()
        dashboard = AdminSalesIntelligenceService.dashboard(
            filters,
            include_ozon=True,
            now=self.NOW,
        )
        keys = [item["scope_key"] for item in dashboard["items"]]
        with patch("services.image_lab_service.launch_experiments") as launch:
            result = AdminSalesIntelligenceService.recommend(
                filters=filters,
                row_keys=keys,
                admin_user_id=self.admin_user.id,
                include_ozon=True,
                remote_addr="127.0.0.1",
                now=self.NOW,
            )
        launch.assert_not_called()
        self.assertEqual(result["created"], 2)
        self.assertEqual(BestsellerImageRecommendation.query.count(), 2)
        self.assertEqual(AdminAuditLog.query.filter_by(
            action="recommend_bestseller_images",
        ).count(), 1)
        seller_items = AdminSalesIntelligenceService.seller_recommendations(
            seller_id=self.seller.id,
        )
        self.assertEqual(len(seller_items), 2)
        self.assertTrue(all(item["target_ready"] for item in seller_items))

        with self.assertRaises(AdminSalesIntelligenceError):
            AdminSalesIntelligenceService.review_recommendation(
                seller_id=self.foreign_seller.id,
                recommendation_id=result["recommendation_ids"][0],
                user_id=self.foreign_user.id,
                status="completed",
            )
        reviewed = AdminSalesIntelligenceService.review_recommendation(
            seller_id=self.seller.id,
            recommendation_id=result["recommendation_ids"][0],
            user_id=self.seller_user.id,
            status="completed",
            now=self.NOW,
        )
        self.assertEqual(reviewed.status, "completed")

    def test_exact_wb_gallery_indices_replace_false_zero_photo_count(self):
        self.wb_imported.photo_urls = None
        self.wb_product.photos_json = json.dumps([1, 2, 3])
        db.session.commit()
        filters = SalesDashboardFilters(marketplace="wb")

        dashboard = AdminSalesIntelligenceService.dashboard(
            filters,
            include_ozon=False,
            now=self.NOW,
        )

        self.assertEqual(len(dashboard["items"]), 1)
        row = dashboard["items"][0]
        self.assertEqual(row["photo_count"], 3)
        self.assertEqual(row["photo_source"], "wb_gallery")
        self.assertTrue(row["recommendable"])

        outcome = AdminSalesIntelligenceService.recommend(
            filters=filters,
            row_keys=[row["scope_key"]],
            admin_user_id=self.admin_user.id,
            include_ozon=False,
            now=self.NOW,
        )
        self.assertEqual(outcome["created"], 1)
        recommendation = AdminSalesIntelligenceService.seller_recommendations(
            seller_id=self.seller.id,
        )[0]
        self.assertEqual(recommendation["photo_count"], 3)
        self.assertTrue(recommendation["target_ready"])

    def test_ungrounded_or_oversized_selection_is_rejected(self):
        with self.assertRaises(AdminSalesIntelligenceError):
            AdminSalesIntelligenceService.recommend(
                filters=SalesDashboardFilters(),
                row_keys=["ozon:account:999:listing:999"],
                admin_user_id=self.admin_user.id,
                include_ozon=True,
                now=self.NOW,
            )
        with self.assertRaises(AdminSalesIntelligenceError):
            AdminSalesIntelligenceService.recommend(
                filters=SalesDashboardFilters(),
                row_keys=[f"wb:product:{value}" for value in range(1, 52)],
                admin_user_id=self.admin_user.id,
                include_ozon=True,
                now=self.NOW,
            )


if __name__ == "__main__":
    unittest.main()
