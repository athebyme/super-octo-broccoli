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
    SellerSupplier,
    SellerMarketplaceAccount,
    Supplier,
    SupplierProduct,
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
        self.sexoptovik = Supplier(
            name="Sexoptovik",
            code="sexoptovik",
        )
        self.andrey = Supplier(
            name="Андрей",
            code="andrey",
        )
        db.session.add_all([self.sexoptovik, self.andrey])
        db.session.flush()
        db.session.add_all([
            SellerSupplier(
                seller_id=self.seller1.id,
                supplier_id=self.sexoptovik.id,
                is_active=True,
            ),
            SellerSupplier(
                seller_id=self.seller1.id,
                supplier_id=self.andrey.id,
                is_active=True,
            ),
        ])
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

    def _supplier_product(
        self,
        *,
        supplier=None,
        external_id="7725",
        vendor_code=None,
        title="Исходная карточка",
    ):
        product = SupplierProduct(
            supplier_id=(supplier or self.sexoptovik).id,
            external_id=external_id,
            vendor_code=vendor_code,
            title=title,
            description="Наблюдённое описание поставщика",
            status="ready",
            original_data_json=json.dumps({
                "title": title,
                "description": "Наблюдённое описание поставщика",
            }),
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

        self.assertEqual(result, {
            "linked": 1,
            "materialized": 0,
            "wb_attached": 0,
            "ambiguous": 0,
            "unmatched": 0,
            "busy": 0,
        })
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

    def test_sexoptovik_suffixes_link_ozon_canonical_and_wb(self):
        source = self._supplier_product(external_id="7725")
        wb_product = Product(
            seller_id=self.seller1.id,
            nm_id=101_437_892,
            vendor_code="id-7725-1366",
            title="WB карточка 7725",
        )
        db.session.add(wb_product)
        db.session.flush()
        canonical = ImportedProduct(
            seller_id=self.seller1.id,
            supplier_id=self.sexoptovik.id,
            supplier_product_id=source.id,
            product_id=wb_product.id,
            external_id="7725",
            source_type="sexoptovik",
            title=source.title,
            import_status="imported",
            wb_nm_id=wb_product.nm_id,
        )
        db.session.add(canonical)
        db.session.commit()
        listing = self._listing(offer="id-7725-1364")

        result = MarketplaceProductLinkService.reconcile_objects(
            seller_id=self.seller1.id,
            listings=[listing],
            commit=True,
        )
        db.session.refresh(listing)

        self.assertEqual(result["linked"], 1)
        self.assertEqual(listing.imported_product_id, canonical.id)
        self.assertEqual(listing.link_source, "exact_source_identity")
        evidence = json.loads(listing.link_evidence_json)
        self.assertEqual(evidence["wb_product_id"], wb_product.id)
        self.assertEqual(evidence["supplier_product_id"], source.id)
        self.assertEqual(
            evidence["parsed_identities"][0]["value"],
            "7725",
        )

    def test_selected_product_preflight_resolves_existing_ozon_suffix(self):
        canonical = ImportedProduct(
            seller_id=self.seller1.id,
            supplier_id=self.sexoptovik.id,
            source_type="sexoptovik",
            external_id="7725",
            external_vendor_code="id-7725-1366",
            title="Карточка 7725",
        )
        db.session.add(canonical)
        db.session.commit()
        listing = self._listing(offer="id-7725-1364")

        result = MarketplaceProductLinkService.reconcile_account_products(
            seller_id=self.seller1.id,
            account_id=self.account1.id,
            products=[canonical],
        )

        self.assertEqual(result["linked"], 1)
        self.assertEqual(
            result["resolved_listing_ids"],
            {canonical.id: listing.id},
        )
        self.assertEqual(result["blocked"], {})
        self.assertNotIn("unresolved_seller_unlink_listing_ids", result)
        db.session.refresh(listing)
        self.assertEqual(listing.imported_product_id, canonical.id)

    def test_private_preflight_reports_only_exact_seller_unlink_siblings(self):
        canonical = self._canonical(
            offer="known-main-offer",
            with_wb=False,
        )
        selected = self._listing(
            offer=canonical.external_vendor_code,
            imported_product_id=canonical.id,
        )
        selected.link_status = "linked"
        selected.link_source = "exact_offer_identity"
        db.session.commit()
        sibling = self._listing(offer=canonical.external_id)
        sibling.link_source = "seller_unlink"
        db.session.commit()

        result = MarketplaceProductLinkService._reconcile_account_products_impl(
            seller_id=self.seller1.id,
            account_id=self.account1.id,
            products=[canonical],
            commit=False,
            guard_seller_unlinked_siblings=True,
        )

        self.assertEqual(result["linked"], 0)
        self.assertEqual(
            result["resolved_listing_ids"],
            {canonical.id: selected.id},
        )
        self.assertEqual(
            result["unresolved_seller_unlink_listing_ids"],
            {canonical.id: [sibling.id]},
        )
        self.assertFalse(db.session.new or db.session.dirty or db.session.deleted)
        self.assertEqual(MarketplaceListingLinkEvent.query.count(), 0)

    def test_private_selected_preflight_does_not_commit_staged_link_or_event(self):
        canonical = self._canonical(offer="caller-owned-no-commit")
        listing = self._listing(offer="caller-owned-no-commit")

        result = MarketplaceProductLinkService._reconcile_account_products_impl(
            seller_id=self.seller1.id,
            account_id=self.account1.id,
            products=[canonical],
            commit=False,
        )

        self.assertEqual(result["linked"], 1)
        self.assertEqual(
            result["resolved_listing_ids"],
            {canonical.id: listing.id},
        )
        self.assertEqual(listing.imported_product_id, canonical.id)
        db.session.rollback()
        db.session.refresh(listing)
        self.assertIsNone(listing.imported_product_id)
        self.assertEqual(listing.canonical_link_status, "unlinked")
        self.assertEqual(MarketplaceListingLinkEvent.query.count(), 0)

    def test_selected_product_preflight_blocks_explicitly_unlinked_offer(self):
        canonical = ImportedProduct(
            seller_id=self.seller1.id,
            supplier_id=self.sexoptovik.id,
            source_type="sexoptovik",
            external_id="7726",
            external_vendor_code="id-7726-1366",
            title="Карточка 7726",
        )
        db.session.add(canonical)
        db.session.commit()
        listing = self._listing(offer="id-7726-1364")
        listing.link_source = "seller_unlink"
        db.session.commit()

        result = MarketplaceProductLinkService.reconcile_account_products(
            seller_id=self.seller1.id,
            account_id=self.account1.id,
            products=[canonical],
        )

        self.assertEqual(result["linked"], 0)
        self.assertEqual(
            result["blocked"][canonical.id]["code"],
            "existing_ozon_listing_link_unresolved",
        )
        self.assertEqual(
            result["blocked"][canonical.id]["listing_ids"],
            [listing.id],
        )

    def test_missing_canonical_is_materialized_from_unique_supplier_and_wb(self):
        source = self._supplier_product(
            external_id="88001",
            title="Полная карточка из фида",
        )
        wb_product = Product(
            seller_id=self.seller1.id,
            nm_id=880_010,
            vendor_code="id-88001-1366",
            title="Существующая WB карточка",
        )
        db.session.add(wb_product)
        db.session.commit()
        listing = self._listing(offer="id-88001-1364")

        reconciled = MarketplaceProductLinkService.reconcile_listing(
            seller_id=self.seller1.id,
            listing_id=listing.id,
        )

        self.assertIsNotNone(reconciled.imported_product_id)
        canonical = db.session.get(
            ImportedProduct,
            reconciled.imported_product_id,
        )
        self.assertEqual(canonical.supplier_product_id, source.id)
        self.assertEqual(canonical.product_id, wb_product.id)
        self.assertEqual(canonical.wb_nm_id, wb_product.nm_id)
        self.assertEqual(canonical.import_status, "imported")
        self.assertEqual(canonical.title, source.title)
        self.assertEqual(
            ImportedProduct.query.filter_by(
                seller_id=self.seller1.id,
                product_id=wb_product.id,
            ).count(),
            1,
        )

        # Reconciliation is idempotent; it cannot create a second canonical.
        repeated = MarketplaceProductLinkService.reconcile_listing(
            seller_id=self.seller1.id,
            listing_id=listing.id,
        )
        self.assertEqual(repeated.imported_product_id, canonical.id)
        self.assertEqual(
            ImportedProduct.query.filter_by(
                seller_id=self.seller1.id,
                product_id=wb_product.id,
            ).count(),
            1,
        )

    def test_materialization_requires_active_seller_supplier_scope(self):
        self._supplier_product(external_id="88002")
        wb_product = Product(
            seller_id=self.seller2.id,
            nm_id=880_020,
            vendor_code="id-88002-1366",
            title="WB другого seller",
        )
        db.session.add(wb_product)
        db.session.commit()
        listing = self._listing(
            seller_id=self.seller2.id,
            account=self.account2,
            offer="id-88002-1364",
        )

        reconciled = MarketplaceProductLinkService.reconcile_listing(
            seller_id=self.seller2.id,
            listing_id=listing.id,
        )

        self.assertIsNone(reconciled.imported_product_id)
        self.assertEqual(
            ImportedProduct.query.filter_by(
                seller_id=self.seller2.id,
                product_id=wb_product.id,
            ).count(),
            0,
        )

    def test_andrey_serial_links_case_and_historical_prefix_variants(self):
        source = self._supplier_product(
            supplier=self.andrey,
            external_id="УТ-00000584",
        )
        wb_product = Product(
            seller_id=self.seller1.id,
            nm_id=584_001,
            vendor_code="id-00000584-1366Z1C1A",
            title="WB Андрей",
        )
        db.session.add(wb_product)
        db.session.flush()
        canonical = ImportedProduct(
            seller_id=self.seller1.id,
            supplier_id=self.andrey.id,
            supplier_product_id=source.id,
            product_id=wb_product.id,
            external_id="УТ-00000584",
            source_type="andrey",
            title=source.title,
            import_status="imported",
        )
        db.session.add(canonical)
        db.session.commit()
        listing = self._listing(offer="1366Z1C1Ayt-00000584")

        result = MarketplaceProductLinkService.reconcile_objects(
            seller_id=self.seller1.id,
            listings=[listing],
            commit=True,
        )

        self.assertEqual(result["linked"], 1)
        self.assertEqual(listing.imported_product_id, canonical.id)
        evidence = json.loads(listing.link_evidence_json)
        self.assertTrue(any(
            item["kind"] == "serial" and item["value"] == "584"
            for item in evidence["parsed_identities"]
        ))

    def test_duplicate_source_copies_resolve_only_by_unique_exact_wb_fk(self):
        source = self._supplier_product(external_id="99001")
        exact_wb = Product(
            seller_id=self.seller1.id,
            nm_id=990_010,
            vendor_code="id-99001-1366",
            title="Точный WB",
        )
        other_wb = Product(
            seller_id=self.seller1.id,
            nm_id=990_011,
            vendor_code="manual-other-code",
            title="Другой WB",
        )
        db.session.add_all([exact_wb, other_wb])
        db.session.flush()
        exact = ImportedProduct(
            seller_id=self.seller1.id,
            supplier_product_id=source.id,
            supplier_id=self.sexoptovik.id,
            product_id=exact_wb.id,
            external_id="99001",
            source_type="sexoptovik",
            title="Точный",
        )
        duplicate = ImportedProduct(
            seller_id=self.seller1.id,
            supplier_product_id=source.id,
            supplier_id=self.sexoptovik.id,
            product_id=other_wb.id,
            external_id="99001",
            source_type="sexoptovik",
            title="Legacy дубль",
        )
        db.session.add_all([exact, duplicate])
        db.session.commit()
        listing = self._listing(offer="id-99001-1364")

        result = MarketplaceProductLinkService.reconcile_objects(
            seller_id=self.seller1.id,
            listings=[listing],
            commit=True,
        )

        self.assertEqual(result["linked"], 1)
        self.assertEqual(listing.imported_product_id, exact.id)

    def test_conflicting_existing_wb_relationship_is_never_overwritten(self):
        source = self._supplier_product(external_id="99101")
        exact_wb = Product(
            seller_id=self.seller1.id,
            nm_id=991_010,
            vendor_code="id-99101-1366",
            title="Точный WB",
        )
        wrong_wb = Product(
            seller_id=self.seller1.id,
            nm_id=991_011,
            vendor_code="manual-wrong",
            title="Связанный ранее WB",
        )
        db.session.add_all([exact_wb, wrong_wb])
        db.session.flush()
        canonical = ImportedProduct(
            seller_id=self.seller1.id,
            supplier_product_id=source.id,
            supplier_id=self.sexoptovik.id,
            product_id=wrong_wb.id,
            external_id="99101",
            source_type="sexoptovik",
            title="Конфликт",
        )
        db.session.add(canonical)
        db.session.commit()
        listing = self._listing(offer="id-99101-1364")

        result = MarketplaceProductLinkService.reconcile_objects(
            seller_id=self.seller1.id,
            listings=[listing],
            commit=True,
        )

        self.assertEqual(result["ambiguous"], 1)
        self.assertIsNone(listing.imported_product_id)
        self.assertEqual(canonical.product_id, wrong_wb.id)
        self.assertEqual(
            json.loads(listing.link_evidence_json)["reason"],
            "canonical_wb_source_identity_conflict",
        )

    def test_automatic_reconcile_respects_explicit_seller_unlink(self):
        canonical = self._canonical(offer="seller-choice")
        listing = self._listing(offer="seller-choice")
        listing.link_source = "seller_unlink"
        db.session.commit()

        automatic = MarketplaceProductLinkService.reconcile_objects(
            seller_id=self.seller1.id,
            listings=[listing],
            commit=True,
        )
        self.assertEqual(automatic["linked"], 0)
        self.assertIsNone(listing.imported_product_id)

        explicit = MarketplaceProductLinkService.reconcile_listing(
            seller_id=self.seller1.id,
            listing_id=listing.id,
        )
        self.assertEqual(explicit.imported_product_id, canonical.id)

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
