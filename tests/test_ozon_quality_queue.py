"""Queue lifecycle and HTTP read-only proof, using only synthetic local facts."""
from datetime import datetime, timedelta
from unittest.mock import patch

from models import BackgroundJob, MarketplaceListing, MarketplaceQualityAssessment, db
from services.marketplace_quality import MarketplaceQualityService
from services.ozon_quality_queue import enqueue_quality, latest_quality_job, run_quality_tick
from services.marketplace_operation_locks import _try_operation_lock
from tests.test_marketplace_insight_routes import MarketplaceInsightRoutesTest


class OzonQualityQueueTest(MarketplaceInsightRoutesTest):
    # Parent tests also verify compatibility of the legacy read routes.
    def _enqueue(self, **kw):
        return enqueue_quality(seller_id=self.seller1_id, account_id=self.own_account_id, **kw)

    def _extra(self, count):
        original=db.session.get(MarketplaceListing,self.own_listing_id)
        result=[]
        for index in range(count):
            row=MarketplaceListing(seller_id=self.seller1_id,marketplace_id=original.marketplace_id,
                account_id=self.own_account_id,offer_id=f'extra-{index}',external_product_id=f'extra-product-{index}',normalized_status='active',
                is_available=True,is_archived=False,sync_fingerprint='c'*64)
            db.session.add(row);db.session.flush();result.append(row.id)
        db.session.commit()
        return result

    def test_enqueue_is_deduplicated_and_only_creates_local_job(self):
        with self.app.app_context(), patch.object(MarketplaceQualityService,'recompute_account') as calc:
            first=self._enqueue();second=self._enqueue()
            self.assertEqual(first['job_uid'],second['job_uid'])
            self.assertEqual(first['status'],'pending')
            self.assertEqual(BackgroundJob.query.count(),1)
            self.assertEqual(MarketplaceQualityAssessment.query.count(),1)
            self.assertNotIn('upper_id',str(first));calc.assert_not_called()
            job=BackgroundJob.query.one()
            self.assertEqual(job.to_dict()['progress'],{})

    def test_keyset_does_not_skip_after_archive_or_include_later_insert(self):
        with self.app.app_context():
            ids=self._extra(4);first=self._enqueue()
            db.session.get(MarketplaceListing,ids[0]).is_archived=True
            original=db.session.get(MarketplaceListing,self.own_listing_id)
            row=MarketplaceListing(seller_id=self.seller1_id,marketplace_id=original.marketplace_id,
                account_id=self.own_account_id,offer_id='arrived-later',external_product_id='late-product',normalized_status='active',
                is_available=True,is_archived=False,sync_fingerprint='d'*64)
            db.session.add(row);db.session.commit();new_id=row.id
        with patch('services.ozon_quality_queue.BATCH_SIZE',2):
            a=run_quality_tick(self.app);b=run_quality_tick(self.app)
        self.assertEqual(a,{'processed':2,'completed':False})
        self.assertEqual(b,{'processed':2,'completed':True})
        with self.app.app_context():
            job=latest_quality_job(seller_id=self.seller1_id,account_id=self.own_account_id)
            self.assertEqual(job['processed'],4);self.assertEqual(job['initial_total'],5)
            self.assertEqual(job['status'],'completed')
            self.assertIsNone(MarketplaceQualityAssessment.query.filter_by(listing_id=new_id).first())
            self.assertIsNone(MarketplaceQualityAssessment.query.filter_by(listing_id=self.foreign_listing_id).first())

    def test_restart_after_score_commit_repeats_only_local_batch(self):
        with self.app.app_context():self._enqueue()
        original=MarketplaceQualityService.recompute_account
        def crash(**kwargs):
            original(**kwargs)
            raise SystemExit('synthetic process exit after scoring commit')
        with patch.object(MarketplaceQualityService,'recompute_account',side_effect=crash):
            with self.assertRaises(SystemExit):run_quality_tick(self.app)
        with self.app.app_context():
            job=BackgroundJob.query.one();self.assertEqual(job.status,'running')
            self.assertEqual(job.get_progress()['after_id'],0)
            self.assertEqual(MarketplaceQualityAssessment.query.count(),1)
        self.assertEqual(run_quality_tick(self.app),{'processed':1,'completed':True})
        with self.app.app_context():
            self.assertEqual(BackgroundJob.query.one().processed,1)
            self.assertEqual(MarketplaceQualityAssessment.query.count(),1)

    def test_live_job_lock_is_not_stolen_and_status_get_does_not_write(self):
        with self.app.app_context():self._enqueue()
        lock=_try_operation_lock('ozon-quality-job',self.own_account_id)
        self.assertIsNotNone(lock)
        try:
            with patch.object(MarketplaceQualityService,'recompute_account') as calc:
                self.assertEqual(run_quality_tick(self.app),{'processed':0});calc.assert_not_called()
            with self.app.app_context():
                with patch.object(db.session,'commit') as commit:
                    value=latest_quality_job(seller_id=self.seller1_id,account_id=self.own_account_id)
                    self.assertTrue(value['active']);commit.assert_not_called()
        finally:lock.close()

    def test_all_quality_writers_share_calculation_lock(self):
        with self.app.app_context():self._enqueue()
        lock=_try_operation_lock('ozon-quality',self.own_account_id)
        try:self.assertEqual(run_quality_tick(self.app),{'processed':0,'busy':True})
        finally:lock.close()
        self.assertTrue(run_quality_tick(self.app)['completed'])

    def test_expired_read_is_observation_and_new_request_can_replace(self):
        now=datetime.utcnow()
        with self.app.app_context():
            first=self._enqueue(now=now-timedelta(days=2))
            observed=latest_quality_job(seller_id=self.seller1_id,account_id=self.own_account_id,now=now)
            self.assertEqual(observed['code'],'expired');self.assertFalse(observed['active'])
            self.assertEqual(BackgroundJob.query.one().status,'pending')
            second=self._enqueue(now=now)
            self.assertNotEqual(first['job_uid'],second['job_uid'])
            self.assertEqual(BackgroundJob.query.order_by(BackgroundJob.id).first().status,'failed')

    def test_worker_expiry_and_feature_flag(self):
        with self.app.app_context():self._enqueue(now=datetime.utcnow()-timedelta(days=2))
        self.app.config['MARKETPLACE_OZON_ENABLED']=False
        self.assertTrue(run_quality_tick(self.app)['disabled'])
        with self.app.app_context():self.assertEqual(BackgroundJob.query.one().status,'pending')
        self.app.config['MARKETPLACE_OZON_ENABLED']=True
        self.assertTrue(run_quality_tick(self.app)['failed'])
        with self.app.app_context():self.assertEqual(BackgroundJob.query.one().get_result()['code'],'expired')

    def test_failed_local_calculation_is_not_reported_as_completed(self):
        with self.app.app_context():self._enqueue()
        with patch.object(MarketplaceQualityService,'recompute_account',side_effect=ValueError('private-sensitive-test-value')):
            self.assertTrue(run_quality_tick(self.app)['failed'])
        with self.app.app_context():
            status=latest_quality_job(seller_id=self.seller1_id,account_id=self.own_account_id)
            self.assertEqual(status['status'],'failed');self.assertNotIn('private-sensitive',str(status))

    def test_refresh_http_scope_and_strict_payload(self):
        user,login=self._auth()
        with user,login:
            base=f'/marketplaces/api/quality/refresh?account_id={self.own_account_id}'
            missing=self.client.get(base);self.assertIsNone(missing.get_json()['job'])
            for path in [base+'&account_id=2',base+'&other=1','/marketplaces/api/quality/refresh?account_id=١']:
                self.assertEqual(self.client.get(path).status_code,400)
            foreign=self.client.get(f'/marketplaces/api/quality/refresh?account_id={self.foreign_account_id}')
            self.assertEqual(foreign.status_code,404)
            self.assertEqual(self.client.post(base,json={'listing_ids':[self.own_listing_id]}).status_code,400)
            self.assertEqual(self.client.post(base,json={'account_id':self.foreign_account_id}).status_code,400)
            with patch.object(MarketplaceQualityService,'recompute_account') as calc:
                response=self.client.post(base,json={})
                self.assertEqual(response.status_code,202);calc.assert_not_called()
            self.assertIn('no-store',response.headers['Cache-Control'])
            self.assertEqual(self.client.get(base).get_json()['job']['job_uid'],response.get_json()['job']['job_uid'])

    def test_refresh_requires_csrf_before_creating_a_job(self):
        self.app.config['WTF_CSRF_ENABLED']=True
        user,login=self._auth()
        with user,login:
            response=self.client.post(f'/marketplaces/api/quality/refresh?account_id={self.own_account_id}',json={})
            self.assertEqual(response.status_code,400)
        with self.app.app_context():self.assertEqual(BackgroundJob.query.count(),0)

    def test_legacy_quality_gets_never_calculate_or_commit(self):
        user,login=self._auth()
        with user,login,patch.object(MarketplaceQualityService,'recompute_account') as calc:
            detail=self.client.get(f'/marketplaces/api/quality/{self.own_listing_id}?account_id={self.own_account_id}')
            self.assertEqual(detail.status_code,200)
            with self.app.app_context():
                MarketplaceQualityAssessment.query.delete();db.session.commit()
            result=self.client.get(f'/marketplaces/api/quality?account_id={self.own_account_id}')
            self.assertEqual(result.status_code,200);self.assertEqual(result.get_json()['items'],[])
            calc.assert_not_called()
        with self.app.app_context():self.assertEqual(MarketplaceQualityAssessment.query.count(),0)
