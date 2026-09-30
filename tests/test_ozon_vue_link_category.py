"""Focused local review contracts for the Vue linking and category journeys."""

import json
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import text

from models import ImportedProduct, MarketplaceProductDraft, Seller, db
from services.marketplace_drafts import MarketplaceDraftConflict, MarketplaceDraftService
from services.marketplace_product_links import MarketplaceProductLinkService
from tests import test_marketplace_drafts as draft_fixtures
from tests import test_marketplace_product_links as link_fixtures


class CategoryReviewServiceTest(unittest.TestCase):
    def setUp(self):
        self.fx = draft_fixtures.MarketplaceDraftServiceTest(methodName="runTest")
        self.fx.setUp()
        self.fx.app.secret_key = "synthetic-category-review-secret"
        self.product, self.draft = self.fx._ready_draft(external_id="review-source")
        self.target = self.fx._official_type("Другой тип", "Новая категория / Другой тип")
        self.draft.complex_attributes_json = json.dumps([{
            "attributes": [{"attribute_id": "700", "complex_id": "2", "values": [{"value": "ручное значение"}]}]
        }], ensure_ascii=False)
        self.draft.attribute_removals_json = json.dumps([{"attribute_id": "701", "complex_id": "0"}])
        db.session.commit()
        db.session.refresh(self.draft)
        self.user_id = db.session.get(Seller, self.fx.seller1_id).user_id

    def tearDown(self):
        self.fx.tearDown()

    def preview(self, **overrides):
        args = dict(
            seller_id=self.fx.seller1_id,
            draft_id=self.draft.id,
            expected_version=self.draft.version,
            target_product_type_id=self.target.id,
            save_mapping=False,
            actor_user_id=self.user_id,
        )
        args.update(overrides)
        return MarketplaceDraftService.category_impact(**args)

    def test_exact_impact_and_signed_single_draft_apply(self):
        preview = self.preview()
        kinds = {row["kind"] for row in preview["rows"]}
        self.assertIn("attribute", kinds)
        self.assertIn("complex_attribute", kinds)
        self.assertIn("attribute_removal", kinds)
        self.assertIn("category_mapping", kinds)
        self.assertEqual(preview["total"], len(preview["rows"]))
        self.assertTrue(any(
            "ручное значение" in json.dumps(row, ensure_ascii=False)
            for row in preview["rows"]
        ))
        patch = {"product_type_id": self.target.id, "save_mapping": False}
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.fx.seller1_id, draft_id=self.draft.id,
                expected_version=self.draft.version, patch=patch,
                corrected_by_user_id=self.user_id,
            )
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.fx.seller1_id, draft_id=self.draft.id,
                expected_version=self.draft.version, patch=patch,
                corrected_by_user_id=self.user_id + 1,
                category_review_token=preview["review_token"],
            )
        updated = MarketplaceDraftService.update_draft(
            seller_id=self.fx.seller1_id, draft_id=self.draft.id,
            expected_version=self.draft.version, patch=patch,
            corrected_by_user_id=self.user_id,
            category_review_token=preview["review_token"],
        )
        self.assertEqual(updated.product_type_id, self.target.id)
        self.assertEqual(json.loads(updated.complex_attributes_json), [])
        self.assertEqual(json.loads(updated.attribute_removals_json), [])
        self.assertIsNone(updated.category_mapping_id)
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.fx.seller1_id, draft_id=self.draft.id,
                expected_version=updated.version, patch={"product_type_id": self.fx.product_type.id},
                corrected_by_user_id=self.user_id,
                category_review_token=preview["review_token"],
            )

    def test_stale_revision_foreign_scope_and_pagination(self):
        preview = self.preview()
        self.assertEqual(preview["page"], 1)
        with self.assertRaises(MarketplaceDraftConflict):
            self.preview(expected_version=self.draft.version + 1)
        with self.assertRaises(Exception) as foreign:
            self.preview(seller_id=self.fx.seller2_id)
        self.assertEqual(foreign.exception.status_code, 404)
        self.draft.content_json = json.dumps({"name": "changed"})
        db.session.commit(); db.session.refresh(self.draft)
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.fx.seller1_id, draft_id=self.draft.id,
                expected_version=self.draft.version,
                patch={"product_type_id": self.target.id},
                corrected_by_user_id=self.user_id,
                category_review_token=preview["review_token"],
            )

    def test_mapping_created_from_null_is_reviewed_and_schema_drift_rejected(self):
        self.draft.category_mapping_id = None
        db.session.commit()
        preview = self.preview(save_mapping=True)
        mapping = [row for row in preview['rows'] if row['kind'] == 'category_mapping']
        self.assertEqual(len(mapping), 1)
        self.assertIsNone(mapping[0]['before'])
        self.assertEqual(mapping[0]['after'], {'new_mapping_requested': True})
        self.target.attributes_schema_hash = 'changed-schema-after-preview'
        db.session.commit()
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.fx.seller1_id, draft_id=self.draft.id,
                expected_version=self.draft.version,
                patch={'product_type_id': self.target.id, 'save_mapping': True},
                corrected_by_user_id=self.user_id,
                category_review_token=preview['review_token'],
            )

    def test_cached_type_is_refreshed_after_out_of_band_schema_change(self):
        preview = self.preview()
        old_hash = self.target.attributes_schema_hash
        raw = db.session.connection().connection.driver_connection
        raw.execute(
            'UPDATE marketplace_product_types SET attributes_schema_hash=? WHERE id=?',
            ('schema-changed-outside-orm', self.target.id),
        )
        raw.commit()
        self.assertEqual(self.target.attributes_schema_hash, old_hash)
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.fx.seller1_id, draft_id=self.draft.id,
                expected_version=self.draft.version,
                patch={'product_type_id': self.target.id},
                corrected_by_user_id=self.user_id,
                category_review_token=preview['review_token'],
            )
        self.assertEqual(self.target.attributes_schema_hash, 'schema-changed-outside-orm')
        self.assertEqual(db.session.get(MarketplaceProductDraft, self.draft.id).product_type_id,
                         self.fx.product_type.id)

    def test_expired_review_token_is_rejected(self):
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=1):
            old = self.preview()
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.fx.seller1_id, draft_id=self.draft.id,
                expected_version=self.draft.version,
                patch={'product_type_id': self.target.id},
                corrected_by_user_id=self.user_id,
                category_review_token=old['review_token'],
            )

    def test_dirty_session_is_not_rolled_back_by_reviewed_update(self):
        preview = self.preview()
        self.draft.offer_id = 'unsaved-caller-change'
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.fx.seller1_id, draft_id=self.draft.id,
                expected_version=self.draft.version,
                patch={'product_type_id': self.target.id},
                corrected_by_user_id=self.user_id,
                category_review_token=preview['review_token'],
            )
        self.assertIn(self.draft, db.session.dirty)
        self.assertEqual(self.draft.offer_id, 'unsaved-caller-change')
        db.session.rollback()

    def test_category_apply_holds_sqlite_write_lock_during_recompute(self):
        preview = self.preview()
        original = MarketplaceDraftService._category_change_plan.__func__
        observed = []
        def inspect_plan(cls, **kwargs):
            observed.append(db.session.connection().connection.driver_connection.in_transaction)
            return original(cls, **kwargs)
        with patch.object(MarketplaceDraftService, '_category_change_plan', classmethod(inspect_plan)):
            MarketplaceDraftService.update_draft(
                seller_id=self.fx.seller1_id, draft_id=self.draft.id,
                expected_version=self.draft.version,
                patch={'product_type_id': self.target.id},
                corrected_by_user_id=self.user_id,
                category_review_token=preview['review_token'],
            )
        self.assertEqual(observed, [True])

    def test_existing_sqlite_write_transaction_is_not_taken_over(self):
        preview = self.preview()
        db.session.execute(text('BEGIN IMMEDIATE'))
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.fx.seller1_id, draft_id=self.draft.id,
                expected_version=self.draft.version,
                patch={'product_type_id': self.target.id},
                corrected_by_user_id=self.user_id,
                category_review_token=preview['review_token'],
            )
        self.assertTrue(db.session.connection().connection.driver_connection.in_transaction)
        db.session.rollback()


class LinkDisplayServiceTest(unittest.TestCase):
    def setUp(self):
        self.fx = link_fixtures.MarketplaceProductLinkServiceTest(methodName="runTest")
        self.fx.setUp()

    def tearDown(self):
        self.fx.tearDown()

    def test_exact_old_imported_id_precedes_newer_substring_matches_within_limit(self):
        exact = self.fx._canonical(offer='old-exact', with_wb=False)
        term = str(exact.id)
        exact.title = f'Old exact ID {term}'
        exact.updated_at = datetime(2020, 1, 1)
        db.session.commit()
        recent = [
            self.fx._canonical(offer=f'decoy-{term}-{index}',
                               title=f'New substring {term} {index}', with_wb=False)
            for index in range(23)
        ]
        foreign = self.fx._canonical(
            seller_id=self.fx.seller2.id, offer=f'foreign-{term}',
            title=f'Foreign substring {term}', with_wb=False,
        )
        listing = self.fx._listing(offer='unrelated')

        # The previous updated_at-first page dropped this healthy exact ID.
        old_page = MarketplaceProductLinkService._candidate_query(
            seller_id=self.fx.seller1.id,
        ).filter(
            ImportedProduct.title.ilike(f'%{term}%'),
        ).order_by(
            ImportedProduct.updated_at.desc(), ImportedProduct.id.desc(),
        ).limit(20).all()
        self.assertEqual(len(old_page), 20)
        self.assertNotIn(exact.id, {row.id for row in old_page})

        found = MarketplaceProductLinkService.search_candidates(
            seller_id=self.fx.seller1.id, listing_id=listing.id,
            query=term, limit=20,
        )
        ids = [row['id'] for row in found]
        self.assertEqual(ids[0], exact.id)
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)
        self.assertTrue(set(ids[1:]).issubset({row.id for row in recent}))
        self.assertNotIn(foreign.id, ids)

    def test_candidate_photo_is_exact_owned_source_and_action_version(self):
        candidate = self.fx._canonical(offer="manual-choice", with_wb=True)
        candidate.photo_urls = json.dumps(["https://synthetic.invalid/photo.jpg"])
        listing = self.fx._listing(offer="different-offer")
        db.session.commit()
        found = MarketplaceProductLinkService.search_candidates(
            seller_id=self.fx.seller1.id, listing_id=listing.id, query="manual-choice", limit=20,
        )
        self.assertEqual([item["id"] for item in found], [candidate.id])
        self.assertEqual(
            found[0]["photo_preview_url"],
            f"/api/photos/imported-product/{candidate.id}/0?deferred=1",
        )
        initial = MarketplaceProductLinkService.context(
            seller_id=self.fx.seller1.id, listing_id=listing.id,
        )
        self.assertTrue(initial["actions"]["can_link"])
        MarketplaceProductLinkService.link(
            seller_id=self.fx.seller1.id, listing_id=listing.id,
            imported_product_id=candidate.id,
            expected_link_version=listing.link_version,
            actor_user_id=self.fx.seller1.user.id,
        )
        current = MarketplaceProductLinkService.context(
            seller_id=self.fx.seller1.id, listing_id=listing.id,
        )
        self.assertFalse(current["actions"]["can_link"])
        self.assertTrue(current["actions"]["can_unlink"])
        self.assertEqual(current["canonical_product"]["photo_preview_url"], found[0]["photo_preview_url"])

    def test_corrupt_foreign_wb_fk_is_excluded_and_cannot_link(self):
        candidate = self.fx._canonical(offer="foreign-wb", with_wb=False)
        foreign = self.fx._canonical(seller_id=self.fx.seller2.id, offer="foreign-source")
        candidate.product_id = foreign.product_id
        listing = self.fx._listing(offer="unrelated")
        db.session.commit()
        self.assertEqual(MarketplaceProductLinkService.search_candidates(
            seller_id=self.fx.seller1.id, listing_id=listing.id, query="foreign-wb", limit=20,
        ), [])
        with self.assertRaises(Exception):
            MarketplaceProductLinkService.link(
                seller_id=self.fx.seller1.id, listing_id=listing.id,
                imported_product_id=candidate.id,
                expected_link_version=listing.link_version,
                actor_user_id=self.fx.seller1.user.id,
            )

    def test_missing_product_fk_is_not_shown_as_valid_candidate(self):
        candidate = self.fx._canonical(offer='broken-fk', with_wb=False)
        candidate.product_id = 99999999
        listing = self.fx._listing(offer='unrelated')
        db.session.commit()
        self.assertEqual(MarketplaceProductLinkService.search_candidates(
            seller_id=self.fx.seller1.id, listing_id=listing.id,
            query='broken-fk', limit=20,
        ), [])
