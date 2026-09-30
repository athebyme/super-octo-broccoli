"""An admin approves an exact local observation; only another full read applies it."""
from datetime import datetime, timedelta
import sqlite3
import unittest
from unittest.mock import patch

from models import db, User, Seller, MarketplaceCategoryMapping, MarketplaceAttributeDefinition, MarketplaceAttributeValue, OzonReferenceValueReview, AdminAuditLog
from services.ozon_reference_service import OzonReferenceService as References, OzonReferenceValidationError
from services.ozon_reference_reviews import OzonReferenceReviewService as Reviews
from tests import test_ozon_reference_service as fixtures
from tests.test_ozon_reference_service import SyntheticOzonAdapter, SYNTHETIC_CREDENTIALS, _attribute
from migrations.migrate_add_ozon_reference_reviews import apply_migration


class OzonReferenceReviewsTest(unittest.TestCase):
    _tree_response = staticmethod(fixtures.OzonReferenceServiceTest._tree_response)
    _sync_tree = fixtures.OzonReferenceServiceTest._sync_tree
    _create_type_with_schema = fixtures.OzonReferenceServiceTest._create_type_with_schema

    def setUp(self):
        fixtures.OzonReferenceServiceTest.setUp(self)
        product_type = self._create_type_with_schema()
        References.sync_attributes(product_type.id, adapter=SyntheticOzonAdapter(attributes={
            'result': [_attribute(31, 'Размер', required=True, dictionary_id=99)]}), credentials=SYNTHETIC_CREDENTIALS)
        self.attribute = MarketplaceAttributeDefinition.query.filter_by(product_type_id=product_type.id).one()
        self.admin = User(username='admin', email='admin@example.test', password_hash='unused', is_admin=True)
        db.session.add(self.admin)
        db.session.commit()
        self.assertTrue(self.sync(40)['success'])
        self.baseline = (self.attribute.values_snapshot_hash, self.attribute.values_version, self.attribute.values_synced_at)

    def tearDown(self):
        fixtures.OzonReferenceServiceTest.tearDown(self)

    def sync(self, count, **options):
        pages = [{'result':[{'id': i, 'value':f'Размер {i}'} for i in range(1, count + 1)], 'has_next':False}]
        adapter = options.pop('adapter', SyntheticOzonAdapter(value_pages=pages))
        return References.sync_attribute_values(self.attribute.id, adapter=adapter, credentials=SYNTHETIC_CREDENTIALS, **options)

    def stage(self):
        self.assertFalse(self.sync(10)['success'])
        review = db.session.get(OzonReferenceValueReview, self.attribute.id)
        self.assertEqual(review.status, 'pending_review')
        return review

    def payload(self, review):
        return {'version':review.version, 'candidate_hash':review.candidate_hash, 'confirm':True}

    def assert_baseline(self):
        self.assertEqual((self.attribute.values_snapshot_hash, self.attribute.values_version, self.attribute.values_synced_at), self.baseline)
        self.assertEqual(MarketplaceAttributeValue.query.filter_by(attribute_id=self.attribute.id, is_available=True).count(), 40)

    def test_exact_approval_requires_second_complete_read_and_atomic_audit(self):
        review = self.stage()
        self.assert_baseline()
        self.assertFalse(References.dictionary_is_fresh(self.attribute))
        self.assertEqual(Reviews.preview(self.attribute)['review']['total'], 30)
        self.assertEqual(Reviews.preview(self.attribute, mode='current')['review']['total'], 10)
        with patch('services.ozon_reference_service.OzonReferenceService._adapter_credentials', side_effect=AssertionError('HTTP approval must not call provider')):
            result = Reviews.approve(self.attribute, self.payload(review), self.admin.id)
            self.assertEqual(result['status'], 'approved')
            Reviews.approve(self.attribute, self.payload(review), self.admin.id)
        self.assertEqual(AdminAuditLog.query.count(), 1)
        self.assert_baseline()
        self.assertFalse(References.dictionary_is_fresh(self.attribute))
        self.assertTrue(self.sync(10)['success'])
        self.assertEqual(self.attribute.values_version, self.baseline[1] + 1)
        self.assertTrue(References.dictionary_is_fresh(self.attribute))
        self.assertEqual(review.status, 'applied')
        self.assertIsNone(review.candidate_json)
        self.assertEqual(review.payload_bytes, 0)
        self.assertEqual(AdminAuditLog.query.count(), 2)
        self.assertEqual(MarketplaceAttributeValue.query.filter_by(attribute_id=self.attribute.id, is_available=False).count(), 30)

    def test_different_second_response_revokes_approval_without_cache_changes(self):
        review = self.stage()
        payload = self.payload(review)
        Reviews.approve(self.attribute, payload, self.admin.id)
        self.assertFalse(self.sync(9)['success'])
        self.assert_baseline()
        self.assertEqual(review.version, payload['version'] + 1)
        self.assertEqual(review.status, 'pending_review')
        self.assertIsNone(review.approved_by)
        with self.assertRaises(OzonReferenceValidationError):
            Reviews.approve(self.attribute, payload, self.admin.id)

    def test_expired_and_revoked_actor_cannot_apply(self):
        review = self.stage()
        Reviews.approve(self.attribute, self.payload(review), self.admin.id)
        self.admin.is_admin = False
        db.session.commit()
        self.assertFalse(self.sync(10)['success'])
        self.assert_baseline()
        self.assertEqual(review.status, 'pending_review')
        self.admin.is_admin = True
        db.session.commit()
        Reviews.approve(self.attribute, self.payload(review), self.admin.id)
        self.assertFalse(self.sync(10, now=review.expires_at + timedelta(seconds=1))['success'])
        self.assert_baseline()
        self.assertEqual(review.status, 'pending_review')

    def test_old_schema_and_invalid_confirmation_are_rejected(self):
        review = self.stage()
        for bad in (None, {}, {**self.payload(review), 'confirm':1}, {**self.payload(review), 'version':True},
                    {**self.payload(review), 'candidate_hash':'wrong'}):
            with self.subTest(payload=bad), self.assertRaises(OzonReferenceValidationError):
                Reviews.approve(self.attribute, bad, self.admin.id)
        self.attribute.product_type.attributes_schema_hash = 'new-schema'
        db.session.commit()
        with self.assertRaises(OzonReferenceValidationError):
            Reviews.approve(self.attribute, self.payload(review), self.admin.id)
        self.assertFalse(Reviews.preview(self.attribute)['review']['can_approve'])
        self.assert_baseline()

    def test_schema_change_during_provider_read_preserves_cache(self):
        def mutate(*args):
            self.attribute.product_type.attributes_schema_hash = 'changed-during-io'
            db.session.commit()
            return {'result':[{'id':1, 'value':'small'}], 'has_next':False}
        adapter = SyntheticOzonAdapter()
        adapter.fetch_attribute_values = mutate
        result = self.sync(1, adapter=adapter)
        self.assertFalse(result['success'])
        self.assertIn('scope changed', result['error'])
        self.assert_baseline()
        self.assertIsNone(db.session.get(OzonReferenceValueReview, self.attribute.id))

    def test_incomplete_empty_and_oversized_candidate_never_becomes_approvable(self):
        self.assertFalse(self.sync(0)['success'])
        adapter = SyntheticOzonAdapter(value_pages=[{'result':[{'id':1,'value':'one'}]}])
        self.assertFalse(self.sync(1, adapter=adapter)['success'])
        with patch.object(Reviews, 'MAX_CANDIDATE_BYTES', 1):
            self.assertFalse(self.sync(10)['success'])
        self.assertIsNone(db.session.get(OzonReferenceValueReview, self.attribute.id))
        self.assert_baseline()

    def test_candidate_does_not_repeat_until_approval_then_worker_handles_admin_demand(self):
        review = self.stage()
        self.attribute.values_synced_at = datetime.utcnow() - timedelta(hours=25)
        db.session.commit()
        with patch.object(References, '_adapter_credentials', side_effect=AssertionError('no provider before approval')):
            self.assertEqual(References.sync_demanded_types(self.marketplace_id)['selected'], 0)
        Reviews.approve(self.attribute, self.payload(review), self.admin.id)
        adapter = SyntheticOzonAdapter(value_pages=[{'result':[{'id':i,'value':f'Размер {i}'} for i in range(1,11)],'has_next':False}])
        with patch.object(References, '_adapter_credentials', return_value=(adapter, SYNTHETIC_CREDENTIALS)):
            result = References.sync_demanded_types(self.marketplace_id)
        self.assertEqual(result['dictionaries_synced'], 1, result)
        self.assertEqual(review.status, 'applied')

    def test_existing_seller_demand_skips_unchanged_pending_review(self):
        self.stage()
        seller = Seller(user=self.admin, company_name='Reference QA')
        db.session.add(seller)
        db.session.flush()
        product_type = self.attribute.product_type
        db.session.add(MarketplaceCategoryMapping(
            seller_id=seller.id, marketplace_id=self.marketplace_id,
            product_type_id=product_type.id, scope_key='source:qa', source_type='synthetic',
            source_category='QA', source_category_normalized='qa',
            external_category_id=product_type.category.external_category_id,
            external_type_id=product_type.external_type_id, mapping_source='manual', mapping_status='active', confidence=1.0,
        ))
        self.attribute.updated_at = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
        with patch.object(References, '_adapter_credentials', side_effect=AssertionError('candidate awaits review')):
            result = References.sync_demanded_types(self.marketplace_id)
        self.assertEqual(result['selected'], 0)

    def test_recovered_full_dictionary_clears_pending_candidate(self):
        review = self.stage()
        self.assertTrue(self.sync(40)['success'])
        self.assertTrue(References.dictionary_is_fresh(self.attribute))
        self.assertEqual(review.status, 'stale')
        self.assertIsNone(review.candidate_json)

    def test_migration_accepts_orm_schema_and_is_idempotent(self):
        connection = db.engine.raw_connection()
        self.assertEqual(apply_migration(connection, verbose=False), 0)
        self.assertEqual(apply_migration(connection, verbose=False), 0)
        connection.close()


class ReferenceReviewMigrationTest(unittest.TestCase):
    def test_additive_fresh_schema_and_managed_orphan_rejection(self):
        connection = sqlite3.connect(':memory:')
        for table in ('users', 'marketplace_product_types', 'marketplace_attribute_definitions'):
            connection.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
        self.assertGreater(apply_migration(connection, verbose=False), 0)
        self.assertEqual(apply_migration(connection, verbose=False), 0)
        connection.execute('''INSERT INTO ozon_reference_value_reviews (
            attribute_id,product_type_id,version,status,baseline_hash,baseline_version,
            schema_hash,scope_hash,candidate_hash,payload_bytes,previous_count,candidate_count,observed_at,expires_at)
            VALUES (1,1,1,'pending_review','hash',1,'schema','scope','candidate',0,40,10,'2026-09-24','2026-09-25')''')
        with self.assertRaises(sqlite3.IntegrityError):
            apply_migration(connection, verbose=False)
        connection.close()
