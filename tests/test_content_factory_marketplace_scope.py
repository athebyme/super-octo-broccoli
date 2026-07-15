# -*- coding: utf-8 -*-
import json
import unittest
from unittest import mock

from flask import Flask

from models import (
    ContentFactory,
    ContentItem,
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    Product,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.content_factory_service import (
    ContentFactoryScopeError,
    ContentFactoryService,
    GenerationResult,
)


class ContentFactoryMarketplaceScopeTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="synthetic-secret",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        self.seller1 = self._seller("factory-one", "factory-one@test.local")
        self.seller2 = self._seller("factory-two", "factory-two@test.local")
        self.ozon = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add(self.ozon)
        db.session.flush()
        self.account1 = self._account(self.seller1.id, "one")
        self.account2 = self._account(self.seller2.id, "two")

        self.canonical = ImportedProduct(
            seller_id=self.seller1.id,
            external_id="canonical-one",
            external_vendor_code="vendor-one",
            title="Общий заголовок",
            brand="Общий бренд",
            category="Общая категория",
            description="Общее описание",
            photo_urls='["https://canonical.test/one.jpg"]',
        )
        self.zero_stock_product = ImportedProduct(
            seller_id=self.seller1.id,
            external_id="canonical-zero",
            title="Без остатка",
        )
        self.foreign_canonical = ImportedProduct(
            seller_id=self.seller2.id,
            external_id="canonical-foreign",
            title="Чужой товар",
        )
        db.session.add_all([
            self.canonical,
            self.zero_stock_product,
            self.foreign_canonical,
        ])
        db.session.flush()
        self.listing = self._listing(
            seller_id=self.seller1.id,
            account=self.account1,
            product=self.canonical,
            external="101",
            stock=7,
        )
        self.zero_stock_listing = self._listing(
            seller_id=self.seller1.id,
            account=self.account1,
            product=self.zero_stock_product,
            external="102",
            stock=0,
        )
        self.unlinked_listing = self._listing(
            seller_id=self.seller1.id,
            account=self.account1,
            product=None,
            external="103",
            stock=5,
        )
        self.foreign_listing = self._listing(
            seller_id=self.seller2.id,
            account=self.account2,
            product=self.foreign_canonical,
            external="201",
            stock=9,
        )
        self.factory = ContentFactory(
            seller_id=self.seller1.id,
            name="Ozon content",
            platform="telegram",
            content_types_json='["promo_post"]',
            product_selection_mode="manual",
            catalog_source="marketplace_listing",
            marketplace_account_id=self.account1.id,
        )
        self.legacy_factory = ContentFactory(
            seller_id=self.seller1.id,
            name="WB content",
            platform="telegram",
            content_types_json='["promo_post"]',
            product_selection_mode="manual",
            catalog_source="legacy_wb",
        )
        self.legacy_product = Product(
            seller_id=self.seller1.id,
            nm_id=7001,
            title="WB товар",
            quantity=1,
            is_active=True,
        )
        self.foreign_legacy_product = Product(
            seller_id=self.seller2.id,
            nm_id=7002,
            title="Чужой WB товар",
            quantity=1,
            is_active=True,
        )
        db.session.add_all([
            self.factory,
            self.legacy_factory,
            self.legacy_product,
            self.foreign_legacy_product,
        ])
        db.session.commit()
        self.service = ContentFactoryService()

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
        return seller

    def _account(self, seller_id, external_id):
        account = SellerMarketplaceAccount(
            seller_id=seller_id,
            marketplace_id=self.ozon.id,
            external_account_id=external_id,
            label=f"Ozon {external_id}",
            is_active=True,
            connection_status="connected",
        )
        db.session.add(account)
        db.session.flush()
        return account

    def _listing(self, *, seller_id, account, product, external, stock):
        listing = MarketplaceListing(
            seller_id=seller_id,
            marketplace_id=self.ozon.id,
            account_id=account.id,
            imported_product_id=product.id if product is not None else None,
            offer_id=f"offer-{external}",
            external_product_id=external,
            title=f"Observed {external}",
            normalized_status="active",
            price_summary_json=json.dumps({
                "values": {
                    "marketing_seller_price": "1250.00",
                    "old_price": "1500.00",
                },
                "currency": "RUB",
            }),
            stock_summary_json=json.dumps({"present": stock}),
            media_json=json.dumps({
                "primary_image": f"https://ozon.test/{external}.jpg",
            }),
            sync_fingerprint=external.rjust(64, "0"),
        )
        db.session.add(listing)
        db.session.flush()
        return listing

    def _ref(self, listing=None, account=None):
        listing = listing or self.listing
        account = account or self.account1
        return {
            "entity_kind": "marketplace_listing",
            "id": listing.id,
            "marketplace_code": "ozon",
            "account_id": account.id,
        }

    @mock.patch(
        "routes.photos.generate_public_photo_urls",
        return_value=["/photos/public/canonical-one.jpg"],
    )
    def test_selection_uses_canonical_facts_and_local_ozon_commercial_state(
        self,
        _photos,
    ):
        products = self.service.select_products(self.factory, limit=10)
        self.assertEqual([item["id"] for item in products], [self.listing.id])
        product = products[0]
        self.assertEqual(product["entity_ref"], self._ref())
        self.assertEqual(product["name"], "Общий заголовок")
        self.assertEqual(product["brand"], "Общий бренд")
        self.assertEqual(product["price"], 1250.0)
        self.assertEqual(product["photos"], ["/photos/public/canonical-one.jpg"])
        self.assertEqual(product["observed_media_count"], 1)
        self.assertEqual(product["product_url"], "")
        self.assertNotIn("ozon.test", json.dumps(product, ensure_ascii=False))

    def test_typed_refs_fail_closed_for_foreign_duplicate_and_zero_stock(self):
        invalid_sets = (
            [self._ref(self.foreign_listing, self.account2)],
            [self._ref(), self._ref()],
            [self._ref(self.zero_stock_listing)],
            [{**self._ref(), "id": True}],
        )
        for refs in invalid_sets:
            with self.subTest(refs=refs), self.assertRaises(ContentFactoryScopeError):
                self.service._collect_listing_data(refs, self.factory)

    def test_invalid_scope_finishes_before_ai_client_creation(self):
        with mock.patch.object(self.service, "_get_ai_client") as get_ai:
            result = self.service.generate_content(
                factory=self.factory,
                product_ids=[],
                content_type="promo_post",
                entity_refs=[self._ref(self.zero_stock_listing)],
            )
        self.assertFalse(result.success)
        self.assertIn("без остатка", result.error)
        get_ai.assert_not_called()

    def test_saved_item_keeps_typed_ref_and_never_overloads_product_ids(self):
        generated = GenerationResult(
            success=True,
            title="Пост",
            body_text="Точный локальный текст",
            hashtags=["товар"],
            media_urls=["/photos/public/canonical-one.jpg"],
            product_url="",
            wb_url=None,
            source_marketplace="ozon",
            entity_refs=[self._ref()],
            store_name="Магазин",
            product_names=["Общий заголовок"],
            quality_score=80,
            ai_provider="synthetic",
            ai_model="synthetic",
        )
        with mock.patch.object(
            self.service,
            "generate_content",
            return_value=generated,
        ):
            item, error = self.service.generate_and_save(
                factory=self.factory,
                product_ids=[],
                content_type="promo_post",
                entity_refs=[self._ref()],
            )
        self.assertIsNone(error)
        self.assertEqual(item.get_product_ids(), [])
        self.assertEqual(item.get_entity_refs(), [self._ref()])
        metadata = item.get_platform_specific()
        self.assertEqual(metadata["source_marketplace"], "ozon")
        self.assertEqual(metadata["product_url"], "")

    @mock.patch(
        "routes.photos.generate_public_photo_urls",
        return_value=["/photos/public/canonical-one.jpg"],
    )
    def test_media_fallback_uses_exact_linked_canonical_product(self, _photos):
        item = ContentItem(
            factory_id=self.factory.id,
            seller_id=self.seller1.id,
            platform="telegram",
            content_type="promo_post",
            body_text="body",
            media_urls_json="[]",
        )
        item.set_product_ids([])
        item.set_entity_refs([self._ref()])
        db.session.add(item)
        db.session.commit()
        self.assertEqual(
            item.get_media_urls(),
            ["/photos/public/canonical-one.jpg"],
        )

    def test_model_urls_are_removed_and_ozon_url_is_not_fabricated(self):
        body = self.service._ensure_product_url(
            "Товар https://www.wildberries.ru/fake\nКупить: https://ozon.ru/fake",
            "",
        )
        self.assertNotIn("http", body)
        self.assertNotIn("ozon.ru", body)
        known = "https://www.wildberries.ru/catalog/7001/detail.aspx"
        wb_body = self.service._ensure_product_url("Товар https://wrong.test", known)
        self.assertNotIn("wrong.test", wb_body)
        self.assertEqual(wb_body.count(known), 1)

    def test_legacy_product_path_remains_exact_and_tenant_scoped(self):
        with mock.patch.object(
            self.service,
            "_product_to_dict",
            side_effect=lambda product, validate_photos=False: {"id": product.id},
        ):
            own = self.service._collect_products_data(
                [self.legacy_product.id],
                self.seller1.id,
            )
            self.assertEqual(own, [{"id": self.legacy_product.id}])
            with self.assertRaises(ContentFactoryScopeError):
                self.service._collect_products_data(
                    [self.foreign_legacy_product.id],
                    self.seller1.id,
                )
            with self.assertRaises(ContentFactoryScopeError):
                self.service._collect_products_data([True], self.seller1.id)


if __name__ == "__main__":
    unittest.main()
