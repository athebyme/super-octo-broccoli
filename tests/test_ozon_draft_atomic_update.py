"""Local suggestion review must commit draft and audit together."""
from sqlalchemy import event, text
import unittest

from models import BackgroundJob, db
from services.marketplace_drafts import MarketplaceDraftService, MarketplaceDraftConflict
from tests.test_marketplace_publications import OzonPublicationFixture


class OzonDraftAtomicUpdateTest(OzonPublicationFixture, unittest.TestCase):
    def test_uncommitted_mutation_rolls_back_with_review_audit(self):
        version = self.draft.version
        original = self.draft.attributes_json
        commits = []
        session = db.session()
        event.listen(session, 'after_commit', lambda session: commits.append(True))
        session.execute(text('BEGIN IMMEDIATE'))
        updated = MarketplaceDraftService._update_draft_core(
            seller_id=self.seller.id, draft_id=self.draft.id,
            expected_version=version, patch={'attributes': []}, commit=False,
        )
        self.assertEqual(updated.version, version + 1)
        self.assertEqual(updated.validation_status, 'stale')
        session.add(BackgroundJob(job_uid='synthetic-ai-review-audit',
            seller_id=self.seller.id, job_type='synthetic_review', status='completed'))
        session.flush()
        self.assertEqual(commits, [])
        session.rollback()
        session.expire_all()
        self.assertEqual(self.draft.version, version)
        self.assertEqual(self.draft.attributes_json, original)
        self.assertIsNone(BackgroundJob.query.filter_by(job_uid='synthetic-ai-review-audit').first())

    def test_caller_commit_keeps_version_and_audit_and_stale_version_fails(self):
        version = self.draft.version
        session = db.session()
        session.execute(text('BEGIN IMMEDIATE'))
        updated = MarketplaceDraftService._update_draft_core(
            seller_id=self.seller.id, draft_id=self.draft.id,
            expected_version=version, patch={'attributes': []}, commit=False,
        )
        session.add(BackgroundJob(job_uid='synthetic-ai-review-success',
            seller_id=self.seller.id, job_type='synthetic_review', status='completed'))
        session.commit()
        self.assertEqual(updated.version, version + 1)
        self.assertIsNotNone(BackgroundJob.query.filter_by(job_uid='synthetic-ai-review-success').first())
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService._update_draft_core(
                seller_id=self.seller.id, draft_id=self.draft.id,
                expected_version=version, patch={'attributes': []}, commit=False,
            )
        session.rollback()
