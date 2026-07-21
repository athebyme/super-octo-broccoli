# -*- coding: utf-8 -*-
import json
import os
import unittest
from unittest import mock

from flask import Flask
from flask_login import LoginManager

from models import (
    ContentFactory,
    ContentItem,
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    Seller,
    SellerMarketplaceAccount,
    SocialAccount,
    User,
    db,
)
from routes.content_factory import register_content_factory_routes


class ContentFactoryMarketplaceRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(
            __name__,
            template_folder=os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "templates",
            ),
        )
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="synthetic-secret",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        login = LoginManager(self.app)
        login.user_loader(lambda user_id: db.session.get(User, int(user_id)))
        register_content_factory_routes(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        self.user1, self.seller1 = self._seller("route-one", "route-one@test.local")
        _, self.seller2 = self._seller("route-two", "route-two@test.local")
        marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add(marketplace)
        db.session.flush()
        self.account1 = self._account(self.seller1.id, marketplace.id, "one")
        self.account2 = self._account(self.seller2.id, marketplace.id, "two")
        canonical = ImportedProduct(
            seller_id=self.seller1.id,
            external_id="route-canonical",
            title="Общая карточка",
            photo_urls='["https://canonical.test/one.jpg"]',
        )
        db.session.add(canonical)
        db.session.flush()
        self.listing = MarketplaceListing(
            seller_id=self.seller1.id,
            marketplace_id=marketplace.id,
            account_id=self.account1.id,
            imported_product_id=canonical.id,
            offer_id="route-offer",
            external_product_id="5001",
            title="Observed listing",
            normalized_status="active",
            stock_summary_json='{"present": 3}',
            price_summary_json=(
                '{"values":{"marketing_seller_price":"999.00"},'
                '"currency":"RUB"}'
            ),
            media_json='{"primary_image":"https://ozon.test/observed.jpg"}',
            sync_fingerprint="5" * 64,
        )
        self.factory = ContentFactory(
            seller_id=self.seller1.id,
            name="Ozon route factory",
            platform="telegram",
            content_types_json='["promo_post"]',
            product_selection_mode="manual",
            catalog_source="marketplace_listing",
            marketplace_account_id=self.account1.id,
        )
        foreign_factory = ContentFactory(
            seller_id=self.seller2.id,
            name="Foreign factory",
            platform="telegram",
            content_types_json='["promo_post"]',
            catalog_source="marketplace_listing",
            marketplace_account_id=self.account2.id,
        )
        db.session.add_all([self.listing, self.factory, foreign_factory])
        db.session.commit()
        self.foreign_factory_id = foreign_factory.id
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.user1.id)
            session["_fresh"] = True

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
        return user, seller

    @staticmethod
    def _account(seller_id, marketplace_id, external_id):
        account = SellerMarketplaceAccount(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            external_account_id=external_id,
            label=f"Ozon {external_id}",
            is_active=True,
            connection_status="connected",
        )
        db.session.add(account)
        db.session.flush()
        return account

    def _ref(self, *, account_id=None):
        return {
            "entity_kind": "marketplace_listing",
            "id": self.listing.id,
            "marketplace_code": "ozon",
            "account_id": account_id or self.account1.id,
        }

    @mock.patch(
        "routes.photos.generate_public_photo_urls",
        return_value=["/photos/public/route-canonical.jpg"],
    )
    def test_selection_returns_typed_listing_scope_without_observed_media_url(
        self,
        _photos,
    ):
        response = self.client.post(
            f"/api/content-factory/{self.factory.id}/select-products",
            json={"count": 1},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        product = response.get_json()["products"][0]
        self.assertEqual(product["id"], self.listing.id)
        self.assertEqual(product["entity_ref"], self._ref())
        self.assertEqual(product["source_marketplace"], "ozon")
        self.assertNotIn("ozon.test", json.dumps(response.get_json()))

    @mock.patch("services.content_factory_service.ContentFactoryService.generate_and_save")
    def test_generate_rejects_id_overload_and_foreign_account_before_ai(self, generate):
        overloaded = self.client.post(
            f"/api/content-factory/{self.factory.id}/generate",
            json={
                "product_ids": [self.listing.id],
                "content_type": "promo_post",
            },
        )
        self.assertEqual(overloaded.status_code, 400, overloaded.get_json())
        foreign = self.client.post(
            f"/api/content-factory/{self.factory.id}/generate",
            json={
                "product_ids": [],
                "entity_refs": [self._ref(account_id=self.account2.id)],
                "content_type": "promo_post",
            },
        )
        self.assertEqual(foreign.status_code, 400, foreign.get_json())
        generate.assert_not_called()

    def test_foreign_factory_is_not_visible(self):
        response = self.client.post(
            f"/api/content-factory/{self.foreign_factory_id}/select-products",
            json={"count": 1},
        )
        self.assertEqual(response.status_code, 404)

    def test_feature_flag_blocks_ozon_factory_but_not_legacy_creation(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        blocked = self.client.post(
            f"/api/content-factory/{self.factory.id}/select-products",
            json={"count": 1},
        )
        self.assertEqual(blocked.status_code, 404)
        before = ContentFactory.query.filter_by(seller_id=self.seller1.id).count()
        created = self.client.post(
            "/content-factory/create",
            data={
                "name": "Legacy remains available",
                "platform": "telegram",
                "content_types": "promo_post",
                "catalog_source": "legacy_wb",
            },
        )
        self.assertEqual(created.status_code, 302)
        self.assertEqual(
            ContentFactory.query.filter_by(seller_id=self.seller1.id).count(),
            before + 1,
        )

    def test_create_rejects_foreign_marketplace_account(self):
        before = ContentFactory.query.filter_by(seller_id=self.seller1.id).count()
        response = self.client.post(
            "/content-factory/create",
            data={
                "name": "Cross tenant",
                "platform": "telegram",
                "content_types": "promo_post",
                "catalog_source": "marketplace_listing",
                "marketplace_account_id": str(self.account2.id),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            ContentFactory.query.filter_by(seller_id=self.seller1.id).count(),
            before,
        )

    def test_catalog_scope_is_immutable_after_first_content_item(self):
        item = ContentItem(
            factory_id=self.factory.id,
            seller_id=self.seller1.id,
            platform="telegram",
            content_type="promo_post",
            body_text="existing",
        )
        item.set_entity_refs([self._ref()])
        db.session.add(item)
        db.session.commit()
        response = self.client.post(
            f"/content-factory/{self.factory.id}/edit",
            data={
                "name": "Attempted scope change",
                "platform": "telegram",
                "content_types": "promo_post",
                "catalog_source": "legacy_wb",
            },
        )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.factory)
        self.assertEqual(self.factory.catalog_source, "marketplace_listing")
        self.assertEqual(self.factory.marketplace_account_id, self.account1.id)

    def test_publish_preflight_error_does_not_leave_item_claimed(self):
        item = ContentItem(
            factory_id=self.factory.id,
            seller_id=self.seller1.id,
            platform="telegram",
            content_type="promo_post",
            body_text="preflight must remain retryable",
            status="approved",
        )
        db.session.add(item)
        db.session.commit()

        response = self.client.post(
            f"/api/content-factory/items/{item.id}/publish",
            json={},
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        db.session.refresh(item)
        self.assertEqual(item.status, "approved")

    @mock.patch("requests.get")
    def test_vk_photo_diagnostic_cannot_use_foreign_social_credentials(self, get):
        response = mock.MagicMock()
        response.status_code = 404
        response.content = b""
        response.headers = {}
        get.return_value = response
        foreign_account = SocialAccount(
            seller_id=self.seller2.id,
            platform="vk",
            account_name="Foreign VK",
            account_id="99999",
            is_active=True,
        )
        foreign_account.set_credentials_dict({
            "access_token": "foreign-group-secret",
            "user_token": "foreign-user-secret",
            "group_id": "99999",
        })
        db.session.add(foreign_account)
        db.session.flush()
        item = ContentItem(
            factory_id=self.factory.id,
            seller_id=self.seller1.id,
            platform="vk",
            content_type="promo_post",
            body_text="diagnostic",
            media_urls_json='["https://example.test/photo.jpg"]',
        )
        db.session.add(item)
        db.session.commit()

        result = self.client.post(
            f"/api/content-factory/items/{item.id}/debug-photos",
            json={"social_account_id": foreign_account.id},
        )

        self.assertEqual(result.status_code, 200)
        payload = result.get_json()
        self.assertNotIn("vk_access_token_present", payload)
        self.assertNotIn("vk_user_token_present", payload)
        self.assertEqual(payload["steps"][-1]["step"], "vk_account")
        self.assertEqual(payload["steps"][-1]["status"], "FAIL")
        get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
