# -*- coding: utf-8 -*-
"""Listing media targets stay exact, local-only and canonical-product based."""

import json
import unittest

from flask import Flask

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_listing_media import (
    MarketplaceListingMediaError,
    MarketplaceListingMediaService,
)


class MarketplaceListingMediaTests(unittest.TestCase):
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
        self.seller1 = self._seller("media-one", "media-one@test.local")
        self.seller2 = self._seller("media-two", "media-two@test.local")
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
        self.product1 = ImportedProduct(
            seller_id=self.seller1.id,
            external_id="canonical-one",
            title="Общая карточка",
            photo_urls='["https://canonical.test/one.jpg"]',
        )
        self.product2 = ImportedProduct(
            seller_id=self.seller2.id,
            external_id="canonical-two",
            title="Чужая карточка",
            photo_urls='["https://canonical.test/two.jpg"]',
        )
        db.session.add_all([self.product1, self.product2])
        db.session.flush()
        self.listing1 = self._listing(
            seller_id=self.seller1.id,
            account=self.account1,
            product=self.product1,
            external="101",
        )
        self.listing2 = self._listing(
            seller_id=self.seller2.id,
            account=self.account2,
            product=self.product2,
            external="202",
        )
        db.session.commit()

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

    def _listing(self, *, seller_id, account, product, external):
        listing = MarketplaceListing(
            seller_id=seller_id,
            marketplace_id=self.ozon.id,
            account_id=account.id,
            imported_product_id=product.id,
            offer_id=f"offer-{external}",
            external_product_id=external,
            title=product.title,
            normalized_status="active",
            media_json=json.dumps({
                "primary_image": f"https://ozon.test/{external}-main.jpg",
                "images": [
                    f"https://ozon.test/{external}-main.jpg",
                    f"https://ozon.test/{external}-side.jpg",
                ],
            }),
            sync_fingerprint="a" * 64,
        )
        db.session.add(listing)
        db.session.flush()
        return listing

    def test_target_exposes_constraints_without_media_urls(self):
        target = MarketplaceListingMediaService.resolve_target(
            seller_id=self.seller1.id,
            listing_id=self.listing1.id,
            expected_imported_product_id=self.product1.id,
            marketplace_code="ozon",
            account_id=self.account1.id,
        )
        self.assertEqual(target["observed_media"]["main_image_count"], 2)
        self.assertEqual(target["observed_media"]["available_main_slots"], 28)
        self.assertEqual(target["constraints"]["preferred_width"], 900)
        self.assertEqual(target["constraints"]["preferred_height"], 1200)
        self.assertFalse(target["constraints"]["local_artifact_attachable"])
        self.assertFalse(target["constraints"]["automatic_publication"])
        serialized = json.dumps(target)
        self.assertNotIn("ozon.test", serialized)
        self.assertEqual(target["source_policy"], "canonical_imported_product_only")

    def test_cross_tenant_and_wrong_canonical_product_fail_closed(self):
        with self.assertRaises(MarketplaceListingMediaError):
            MarketplaceListingMediaService.resolve_target(
                seller_id=self.seller1.id,
                listing_id=self.listing2.id,
            )
        with self.assertRaises(MarketplaceListingMediaError):
            MarketplaceListingMediaService.resolve_target(
                seller_id=self.seller1.id,
                listing_id=self.listing1.id,
                expected_imported_product_id=self.product2.id,
            )

    def test_unlinked_or_archived_listing_is_not_a_target(self):
        self.listing1.imported_product_id = None
        self.listing1.link_status = "unlinked"
        db.session.commit()
        with self.assertRaises(MarketplaceListingMediaError):
            MarketplaceListingMediaService.resolve_target(
                seller_id=self.seller1.id,
                listing_id=self.listing1.id,
            )

    def test_inactive_account_is_not_a_target(self):
        self.account1.is_active = False
        db.session.commit()
        with self.assertRaises(MarketplaceListingMediaError):
            MarketplaceListingMediaService.resolve_target(
                seller_id=self.seller1.id,
                listing_id=self.listing1.id,
            )

    def test_bulk_targets_are_bounded_to_seller_and_product(self):
        targets = MarketplaceListingMediaService.targets_for_products(
            seller_id=self.seller1.id,
            imported_product_ids=[self.product1.id],
        )
        self.assertEqual(
            [item["listing_id"] for item in targets[self.product1.id]],
            [self.listing1.id],
        )
        self.assertNotIn(
            self.listing2.id,
            [
                item["listing_id"]
                for product_targets in targets.values()
                for item in product_targets
            ],
        )


if __name__ == "__main__":
    unittest.main()
