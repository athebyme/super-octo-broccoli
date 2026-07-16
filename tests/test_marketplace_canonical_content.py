# -*- coding: utf-8 -*-
"""Reviewed Ozon observations update only canonical common content."""

from datetime import datetime, timedelta
from unittest.mock import patch
import unittest

from flask import Flask

from models import (
    AgentChangeSnapshot,
    ImportedProduct,
    Marketplace,
    MarketplaceCanonicalContentProposal,
    MarketplaceListing,
    Product,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_canonical_content import (
    MarketplaceCanonicalContentConflict,
    MarketplaceCanonicalContentNotFound,
    MarketplaceCanonicalContentService,
    MarketplaceCanonicalContentValidationError,
)
from services.marketplace_accounts import MarketplaceAccountService


class MarketplaceCanonicalContentServiceTest(unittest.TestCase):
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

        self.user, self.seller = self._seller(
            "canonical-owner",
            "canonical-owner@test.local",
        )
        self.foreign_user, self.foreign_seller = self._seller(
            "canonical-foreign",
            "canonical-foreign@test.local",
        )
        self.ozon = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add(self.ozon)
        db.session.flush()
        self.account = self._account(self.seller.id, "canonical-account")
        self.foreign_account = self._account(
            self.foreign_seller.id,
            "foreign-account",
        )

        self.wb_product = Product(
            seller_id=self.seller.id,
            nm_id=991001,
            vendor_code="shared-offer",
            title="WB projection title",
            description="WB projection description",
        )
        db.session.add(self.wb_product)
        db.session.flush()
        self.product = ImportedProduct(
            seller_id=self.seller.id,
            product_id=self.wb_product.id,
            external_id="canonical-source",
            external_vendor_code="shared-offer",
            source_type="synthetic",
            title="Canonical title",
            description="Canonical description",
            category="Canonical WB category",
            ai_attributes='{"cached": true}',
        )
        db.session.add(self.product)
        db.session.flush()
        observed_at = datetime.utcnow()
        self.listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.ozon.id,
            account_id=self.account.id,
            imported_product_id=self.product.id,
            offer_id="shared-offer",
            external_product_id="501001",
            title="Ozon observed title",
            description="Ozon observed description",
            external_category_id="17027470",
            external_type_id="91461",
            attributes_json='[{"id": 85, "values": [{"dictionary_value_id": 7}]}]',
            price_summary_json='{"values": {"price": "1990"}}',
            stock_summary_json='{"present": 4}',
            media_json='{"images": ["https://provider.invalid/image.jpg"]}',
            normalized_status="active",
            is_available=True,
            is_archived=False,
            link_status="linked",
            info_synced_at=observed_at,
            attributes_synced_at=observed_at,
            sync_fingerprint="a" * 64,
        )
        db.session.add(self.listing)

        self.foreign_product = ImportedProduct(
            seller_id=self.foreign_seller.id,
            external_id="foreign-source",
            external_vendor_code="foreign-offer",
            source_type="synthetic",
            title="Foreign canonical",
        )
        db.session.add(self.foreign_product)
        db.session.flush()
        self.foreign_listing = MarketplaceListing(
            seller_id=self.foreign_seller.id,
            marketplace_id=self.ozon.id,
            account_id=self.foreign_account.id,
            imported_product_id=self.foreign_product.id,
            offer_id="foreign-offer",
            external_product_id="501002",
            title="Foreign Ozon",
            description="Foreign description",
            normalized_status="active",
            is_available=True,
            link_status="linked",
            info_synced_at=observed_at,
            attributes_synced_at=observed_at,
            sync_fingerprint="b" * 64,
        )
        db.session.add(self.foreign_listing)
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
        db.session.flush()
        return user, seller

    def _account(self, seller_id, external_account_id):
        account = SellerMarketplaceAccount(
            seller_id=seller_id,
            marketplace_id=self.ozon.id,
            external_account_id=external_account_id,
            label=external_account_id,
            is_active=True,
            connection_status="connected",
        )
        db.session.add(account)
        db.session.flush()
        return account

    def _create(self, fields=None):
        return MarketplaceCanonicalContentService.create_proposal(
            seller_id=self.seller.id,
            listing_id=self.listing.id,
            created_by_user_id=self.user.id,
            fields=fields,
        )

    def test_comparison_contains_only_common_content_and_no_side_effects(self):
        comparison = MarketplaceCanonicalContentService.comparison(
            seller_id=self.seller.id,
            listing_id=self.listing.id,
        )

        self.assertEqual(
            [row["field"] for row in comparison["fields"]],
            ["title", "description"],
        )
        self.assertEqual(
            comparison["differing_fields"],
            ["title", "description"],
        )
        self.assertTrue(comparison["source_fresh"])
        self.assertTrue(comparison["proposal_allowed"])
        self.assertFalse(comparison["side_effects"]["provider_calls"])
        self.assertIn(
            "attribute_and_dictionary_value_ids",
            comparison["excluded_scopes"],
        )
        self.assertEqual(self.product.category, "Canonical WB category")

    def test_apply_is_reviewed_local_only_snapshotted_and_reversible(self):
        proposal = self._create()
        self.assertEqual(proposal.status, "pending_review")
        self.assertEqual(self.product.title, "Canonical title")

        applied = MarketplaceCanonicalContentService.apply_proposal(
            seller_id=self.seller.id,
            proposal_id=proposal.id,
            expected_version=proposal.version,
            reviewed_by_user_id=self.user.id,
            note="checked synthetic diff",
        )
        db.session.refresh(self.product)
        db.session.refresh(self.wb_product)
        db.session.refresh(self.listing)
        self.assertEqual(applied.status, "applied")
        self.assertEqual(self.product.title, "Ozon observed title")
        self.assertEqual(self.product.description, "Ozon observed description")
        self.assertEqual(self.product.category, "Canonical WB category")
        self.assertEqual(self.wb_product.title, "WB projection title")
        self.assertEqual(self.listing.title, "Ozon observed title")
        snapshot = AgentChangeSnapshot.query.one()
        self.assertEqual(snapshot.agent_id, "ozon-canonical-review")
        self.assertFalse(snapshot.is_rolled_back)

        rolled_back = MarketplaceCanonicalContentService.rollback_proposal(
            seller_id=self.seller.id,
            proposal_id=applied.id,
            expected_version=applied.version,
            rolled_back_by_user_id=self.user.id,
        )
        db.session.refresh(self.product)
        db.session.refresh(snapshot)
        self.assertEqual(rolled_back.status, "rolled_back")
        self.assertEqual(self.product.title, "Canonical title")
        self.assertEqual(self.product.description, "Canonical description")
        self.assertTrue(snapshot.is_rolled_back)

    def test_canonical_or_ozon_drift_turns_pending_proposal_into_conflict(self):
        canonical_proposal = self._create(fields=["title"])
        self.product.title = "Newer canonical edit"
        db.session.commit()
        with self.assertRaises(MarketplaceCanonicalContentConflict):
            MarketplaceCanonicalContentService.apply_proposal(
                seller_id=self.seller.id,
                proposal_id=canonical_proposal.id,
                expected_version=canonical_proposal.version,
                reviewed_by_user_id=self.user.id,
            )
        db.session.expire_all()
        stored = db.session.get(
            MarketplaceCanonicalContentProposal,
            canonical_proposal.id,
        )
        self.assertEqual(stored.status, "conflict")
        self.assertEqual(self.product.title, "Newer canonical edit")
        self.assertEqual(AgentChangeSnapshot.query.count(), 0)

        self.product.title = "Canonical title"
        self.listing.title = "Ozon observed title v2"
        self.listing.info_synced_at = datetime.utcnow()
        self.listing.attributes_synced_at = datetime.utcnow()
        db.session.commit()
        source_proposal = self._create(fields=["title"])
        self.listing.title = "Ozon observed title v3"
        self.listing.info_synced_at = datetime.utcnow()
        self.listing.attributes_synced_at = datetime.utcnow()
        db.session.commit()
        with self.assertRaises(MarketplaceCanonicalContentConflict):
            MarketplaceCanonicalContentService.apply_proposal(
                seller_id=self.seller.id,
                proposal_id=source_proposal.id,
                expected_version=source_proposal.version,
                reviewed_by_user_id=self.user.id,
            )
        db.session.expire_all()
        self.assertEqual(
            db.session.get(
                MarketplaceCanonicalContentProposal,
                source_proposal.id,
            ).status,
            "conflict",
        )
        self.assertEqual(self.product.title, "Canonical title")

    def test_stale_or_incomplete_observation_and_unsupported_fields_fail_closed(self):
        self.listing.info_synced_at = datetime.utcnow() - timedelta(hours=49)
        db.session.commit()
        with self.assertRaises(MarketplaceCanonicalContentValidationError):
            self._create()

        self.listing.info_synced_at = datetime.utcnow()
        self.listing.attributes_synced_at = datetime.utcnow()
        db.session.commit()
        for fields in (
            ["external_category_id"],
            ["title", "title"],
            "title",
            [True],
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(
                    MarketplaceCanonicalContentValidationError
                ):
                    self._create(fields=fields)

    def test_tenant_scope_and_actor_scope_are_exact(self):
        with self.assertRaises(MarketplaceCanonicalContentNotFound):
            MarketplaceCanonicalContentService.comparison(
                seller_id=self.seller.id,
                listing_id=self.foreign_listing.id,
            )
        with self.assertRaises(MarketplaceCanonicalContentNotFound):
            MarketplaceCanonicalContentService.create_proposal(
                seller_id=self.seller.id,
                listing_id=self.listing.id,
                created_by_user_id=self.foreign_user.id,
            )

        foreign_proposal = MarketplaceCanonicalContentService.create_proposal(
            seller_id=self.foreign_seller.id,
            listing_id=self.foreign_listing.id,
            created_by_user_id=self.foreign_user.id,
        )
        with self.assertRaises(MarketplaceCanonicalContentNotFound):
            MarketplaceCanonicalContentService.apply_proposal(
                seller_id=self.seller.id,
                proposal_id=foreign_proposal.id,
                expected_version=foreign_proposal.version,
                reviewed_by_user_id=self.user.id,
            )

    def test_duplicate_is_idempotent_and_changed_source_supersedes_pending(self):
        first = self._create(fields=["title"])
        duplicate = self._create(fields=["title"])
        self.assertEqual(duplicate.id, first.id)

        self.listing.title = "Another Ozon observation"
        self.listing.info_synced_at = datetime.utcnow()
        self.listing.attributes_synced_at = datetime.utcnow()
        db.session.commit()
        replacement = self._create(fields=["title"])
        self.assertNotEqual(replacement.id, first.id)
        db.session.expire_all()
        self.assertEqual(
            db.session.get(
                MarketplaceCanonicalContentProposal,
                first.id,
            ).status,
            "conflict",
        )
        self.assertEqual(replacement.status, "pending_review")

    def test_reject_never_mutates_canonical_and_corrupt_contract_never_applies(self):
        rejected = self._create(fields=["description"])
        rejected = MarketplaceCanonicalContentService.reject_proposal(
            seller_id=self.seller.id,
            proposal_id=rejected.id,
            expected_version=rejected.version,
            reviewed_by_user_id=self.user.id,
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(self.product.description, "Canonical description")

        corrupt = self._create(fields=["title"])
        corrupt.contract_version = "unknown-contract"
        db.session.commit()
        with self.assertRaises(MarketplaceCanonicalContentConflict):
            MarketplaceCanonicalContentService.apply_proposal(
                seller_id=self.seller.id,
                proposal_id=corrupt.id,
                expected_version=corrupt.version,
                reviewed_by_user_id=self.user.id,
            )
        db.session.expire_all()
        self.assertEqual(
            db.session.get(
                MarketplaceCanonicalContentProposal,
                corrupt.id,
            ).status,
            "conflict",
        )
        self.assertEqual(self.product.title, "Canonical title")

    def test_rollback_blocks_when_newer_canonical_edit_exists(self):
        proposal = self._create(fields=["title"])
        applied = MarketplaceCanonicalContentService.apply_proposal(
            seller_id=self.seller.id,
            proposal_id=proposal.id,
            expected_version=proposal.version,
            reviewed_by_user_id=self.user.id,
        )
        self.product.title = "Human edit after apply"
        db.session.commit()
        with self.assertRaises(MarketplaceCanonicalContentConflict):
            MarketplaceCanonicalContentService.rollback_proposal(
                seller_id=self.seller.id,
                proposal_id=applied.id,
                expected_version=applied.version,
                rolled_back_by_user_id=self.user.id,
            )
        db.session.expire_all()
        self.assertEqual(self.product.title, "Human edit after apply")
        stored = db.session.get(MarketplaceCanonicalContentProposal, applied.id)
        self.assertEqual(stored.status, "applied")
        self.assertEqual(stored.error_code, "canonical_rollback_drift")
        self.assertFalse(stored.snapshot.is_rolled_back)

    def test_atomic_apply_and_rollback_races_fail_without_partial_state(self):
        proposal = self._create(fields=["title"])
        with patch.object(
            MarketplaceCanonicalContentService,
            "_conditional_product_update",
            return_value=False,
        ), self.assertRaises(MarketplaceCanonicalContentConflict):
            MarketplaceCanonicalContentService.apply_proposal(
                seller_id=self.seller.id,
                proposal_id=proposal.id,
                expected_version=proposal.version,
                reviewed_by_user_id=self.user.id,
            )
        db.session.expire_all()
        raced_apply = db.session.get(
            MarketplaceCanonicalContentProposal,
            proposal.id,
        )
        self.assertEqual(raced_apply.status, "conflict")
        self.assertEqual(raced_apply.error_code, "canonical_apply_race")
        self.assertEqual(self.product.title, "Canonical title")
        self.assertEqual(AgentChangeSnapshot.query.count(), 0)

        replacement = self._create(fields=["title"])
        applied = MarketplaceCanonicalContentService.apply_proposal(
            seller_id=self.seller.id,
            proposal_id=replacement.id,
            expected_version=replacement.version,
            reviewed_by_user_id=self.user.id,
        )
        with patch.object(
            MarketplaceCanonicalContentService,
            "_conditional_product_update",
            return_value=False,
        ), self.assertRaises(MarketplaceCanonicalContentConflict):
            MarketplaceCanonicalContentService.rollback_proposal(
                seller_id=self.seller.id,
                proposal_id=applied.id,
                expected_version=applied.version,
                rolled_back_by_user_id=self.user.id,
            )
        db.session.expire_all()
        raced_rollback = db.session.get(
            MarketplaceCanonicalContentProposal,
            applied.id,
        )
        self.assertEqual(raced_rollback.status, "applied")
        self.assertEqual(raced_rollback.error_code, "canonical_rollback_race")
        self.assertEqual(self.product.title, "Ozon observed title")
        self.assertFalse(raced_rollback.snapshot.is_rolled_back)

    def test_account_lock_blocks_create_and_apply_without_mutation(self):
        with patch(
            "services.marketplace_canonical_content.try_account_operation_lock",
            return_value=None,
        ), self.assertRaises(MarketplaceCanonicalContentConflict):
            self._create(fields=["title"])
        self.assertEqual(MarketplaceCanonicalContentProposal.query.count(), 0)

        proposal = self._create(fields=["title"])
        with patch(
            "services.marketplace_canonical_content.try_account_operation_lock",
            return_value=None,
        ), self.assertRaises(MarketplaceCanonicalContentConflict):
            MarketplaceCanonicalContentService.apply_proposal(
                seller_id=self.seller.id,
                proposal_id=proposal.id,
                expected_version=proposal.version,
                reviewed_by_user_id=self.user.id,
            )
        db.session.expire_all()
        self.assertEqual(
            db.session.get(
                MarketplaceCanonicalContentProposal,
                proposal.id,
            ).status,
            "pending_review",
        )
        self.assertEqual(self.product.title, "Canonical title")

    def test_disconnect_conflicts_pending_but_preserves_applied_rollback(self):
        pending = self._create(fields=["description"])
        MarketplaceAccountService.disconnect(
            seller_id=self.seller.id,
            account_id=self.account.id,
        )
        db.session.expire_all()
        stored_pending = db.session.get(
            MarketplaceCanonicalContentProposal,
            pending.id,
        )
        self.assertEqual(stored_pending.status, "conflict")
        self.assertEqual(
            stored_pending.error_code,
            "account_disconnected_before_review",
        )
        listed = MarketplaceCanonicalContentService.list_for_listing(
            seller_id=self.seller.id,
            listing_id=self.listing.id,
        )
        self.assertEqual([item.id for item in listed], [pending.id])

        self.account.is_active = True
        self.account.connection_status = "connected"
        db.session.commit()
        proposal = self._create(fields=["title"])
        applied = MarketplaceCanonicalContentService.apply_proposal(
            seller_id=self.seller.id,
            proposal_id=proposal.id,
            expected_version=proposal.version,
            reviewed_by_user_id=self.user.id,
        )
        MarketplaceAccountService.disconnect(
            seller_id=self.seller.id,
            account_id=self.account.id,
        )
        rolled_back = MarketplaceCanonicalContentService.rollback_proposal(
            seller_id=self.seller.id,
            proposal_id=applied.id,
            expected_version=applied.version,
            rolled_back_by_user_id=self.user.id,
        )
        db.session.expire_all()
        self.assertEqual(rolled_back.status, "rolled_back")
        self.assertEqual(self.product.title, "Canonical title")


if __name__ == "__main__":
    unittest.main()
