# -*- coding: utf-8 -*-
"""Canonical product links are exact, tenant-scoped and fully audited."""

import json
import unittest

from flask import Flask

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    MarketplaceListingLinkEvent,
    Product,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_product_links import (
    MarketplaceProductLinkConflict,
    MarketplaceProductLinkNotFound,
    MarketplaceProductLinkService,
)


class MarketplaceProductLinkServiceTest(unittest.TestCase):
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
        self.seller1 = self._seller("link-one", "link1@test.local")
        self.seller2 = self._seller("link-two", "link2@test.local")
        self.wb = Marketplace(
            name="Wildberries",
            code="wb",
            adapter_code="wb",
            is_active=True,
        )
        self.ozon = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add_all([self.wb, self.ozon])
        db.session.flush()
        self.account1 = self._account(self.seller1.id, "account-one")
        self.account2 = self._account(self.seller2.id, "account-two")
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
            label=external_id,
            is_active=True,
            connection_status="connected",
        )
        db.session.add(account)
        db.session.flush()
        return account

    def _canonical(
        self,
        *,
        seller_id=None,
        offer="shared-offer",
        title="Одна общая карточка",
        with_wb=True,
    ):
        seller_id = seller_id or self.seller1.id
        wb_product = None
        if with_wb:
            wb_product = Product(
                seller_id=seller_id,
                nm_id=10_000 + ImportedProduct.query.count(),
                vendor_code=offer,
                title=title,
            )
            db.session.add(wb_product)
            db.session.flush()
        product = ImportedProduct(
            seller_id=seller_id,
            product_id=wb_product.id if wb_product else None,
            external_id=f"source-{ImportedProduct.query.count()}",
            external_vendor_code=offer,
            source_type="synthetic",
            title=title,
            ai_attributes=json.dumps({"material": "cotton"}),
        )
        db.session.add(product)
        db.session.commit()
        return product

    def _listing(
        self,
        *,
        seller_id=None,
        account=None,
        offer="shared-offer",
        external_product_id=None,
        title="Одна общая карточка",
        imported_product_id=None,
    ):
        seller_id = seller_id or self.seller1.id
        account = account or self.account1
        listing = MarketplaceListing(
            seller_id=seller_id,
            marketplace_id=self.ozon.id,
            account_id=account.id,
            offer_id=offer,
            external_product_id=(
                external_product_id or str(100 + MarketplaceListing.query.count())
            ),
            title=title,
            imported_product_id=imported_product_id,
            sync_fingerprint="a" * 64,
        )
        db.session.add(listing)
        db.session.commit()
        return listing

    def test_exact_offer_reuses_one_internal_card_and_ai_cache(self):
        canonical = self._canonical()
        listing = self._listing()
        result = MarketplaceProductLinkService.reconcile_objects(
            seller_id=self.seller1.id,
            listings=[listing],
            commit=True,
        )
        db.session.refresh(listing)

        self.assertEqual(result, {"linked": 1, "ambiguous": 0, "unmatched": 0})
        self.assertEqual(listing.imported_product_id, canonical.id)
        self.assertEqual(listing.canonical_link_status, "linked")
        self.assertEqual(listing.link_source, "exact_offer_identity")
        event = MarketplaceListingLinkEvent.query.one()
        self.assertEqual(event.action, "auto_link")
        self.assertEqual(event.imported_product_id, canonical.id)

        context = MarketplaceProductLinkService.context(
            seller_id=self.seller1.id,
            listing_id=listing.id,
        )
        self.assertEqual(context["canonical_product"]["id"], canonical.id)
        self.assertTrue(context["canonical_product"]["ai_cache_available"])
        self.assertEqual(
            context["canonical_product"]["ai_source"],
            "imported_product_cache",
        )

    def test_title_similarity_never_links_and_duplicate_exact_ids_are_ambiguous(self):
        self._canonical(offer="different-offer", title="Совпадающее название")
        title_only = self._listing(
            offer="no-identity-match",
            title="Совпадающее название",
        )
        unmatched = MarketplaceProductLinkService.reconcile_objects(
            seller_id=self.seller1.id,
            listings=[title_only],
            commit=True,
        )
        self.assertEqual(unmatched["unmatched"], 1)
        self.assertIsNone(title_only.imported_product_id)

        first = self._canonical(offer="ambiguous", with_wb=False)
        second = self._canonical(offer="ambiguous", with_wb=False)
        ambiguous = self._listing(offer="ambiguous")
        result = MarketplaceProductLinkService.reconcile_objects(
            seller_id=self.seller1.id,
            listings=[ambiguous],
            commit=True,
        )
        db.session.refresh(ambiguous)
        self.assertEqual(result["ambiguous"], 1)
        self.assertIsNone(ambiguous.imported_product_id)
        self.assertEqual(ambiguous.canonical_link_status, "ambiguous")
        evidence = json.loads(ambiguous.link_evidence_json)
        self.assertEqual(
            evidence["candidate_product_ids"],
            sorted([first.id, second.id]),
        )

    def test_manual_link_is_tenant_scoped_optimistic_unique_and_audited(self):
        canonical = self._canonical(offer="manual")
        listing = self._listing(offer="provider-offer")
        foreign = self._canonical(
            seller_id=self.seller2.id,
            offer="foreign",
        )
        with self.assertRaises(MarketplaceProductLinkNotFound):
            MarketplaceProductLinkService.link(
                seller_id=self.seller1.id,
                listing_id=listing.id,
                imported_product_id=foreign.id,
                expected_link_version=listing.link_version,
                actor_user_id=self.seller1.user.id,
            )
        with self.assertRaises(MarketplaceProductLinkConflict):
            MarketplaceProductLinkService.link(
                seller_id=self.seller1.id,
                listing_id=listing.id,
                imported_product_id=canonical.id,
                expected_link_version=listing.link_version + 1,
                actor_user_id=self.seller1.user.id,
            )

        linked = MarketplaceProductLinkService.link(
            seller_id=self.seller1.id,
            listing_id=listing.id,
            imported_product_id=canonical.id,
            expected_link_version=listing.link_version,
            actor_user_id=self.seller1.user.id,
        )
        self.assertEqual(linked.imported_product_id, canonical.id)
        self.assertEqual(linked.link_version, 2)

        duplicate = self._listing(
            offer="other-provider-offer",
            external_product_id="other-provider-product",
        )
        with self.assertRaises(MarketplaceProductLinkConflict):
            MarketplaceProductLinkService.link(
                seller_id=self.seller1.id,
                listing_id=duplicate.id,
                imported_product_id=canonical.id,
                expected_link_version=duplicate.link_version,
                actor_user_id=self.seller1.user.id,
            )

        unlinked = MarketplaceProductLinkService.unlink(
            seller_id=self.seller1.id,
            listing_id=listing.id,
            expected_link_version=linked.link_version,
            actor_user_id=self.seller1.user.id,
        )
        self.assertIsNone(unlinked.imported_product_id)
        self.assertEqual(unlinked.link_version, 3)
        self.assertEqual(
            [row.action for row in MarketplaceListingLinkEvent.query.order_by(
                MarketplaceListingLinkEvent.id
            ).all()],
            ["manual_link", "unlink"],
        )

    def test_one_canonical_card_cannot_link_two_listings_in_same_account(self):
        canonical = self._canonical(offer="first-identity")
        existing = self._listing(
            offer="already-linked",
            imported_product_id=canonical.id,
        )
        existing.link_status = "linked"
        existing.link_source = "bootstrap"
        canonical.external_id = "second-identity"
        db.session.commit()
        second = self._listing(
            offer="second-identity",
            external_product_id="second-external",
        )
        result = MarketplaceProductLinkService.reconcile_objects(
            seller_id=self.seller1.id,
            listings=[second],
            commit=True,
        )
        self.assertEqual(result["ambiguous"], 1)
        self.assertIsNone(second.imported_product_id)
        self.assertEqual(
            json.loads(second.link_evidence_json)["reason"],
            "canonical_product_already_linked_in_account",
        )


if __name__ == "__main__":
    unittest.main()
