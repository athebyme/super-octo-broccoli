import json
from datetime import datetime,timedelta
from unittest.mock import patch
from sqlalchemy import event
from models import db,MarketplaceListing,MarketplaceQualityAssessment,MarketplaceAnalyticsSync
from services.marketplace_quality import MarketplaceQualityService,MarketplaceQualityNotFound
from services.marketplace_quality_workspace import workspace,detail,QualityReadLimit
from services.marketplace_analytics import MarketplaceAnalyticsService
from services.ozon_analytics_contracts import request_fingerprint
from tests import test_marketplace_insight_routes as fixtures


class OzonQualityWorkspaceTest(fixtures.MarketplaceInsightRoutesTest):
    def _list(self,**kwargs):return workspace(seller_id=self.seller1_id,account_id=self.own_account_id,**kwargs)
    def _detail(self,**kwargs):return detail(seller_id=self.seller1_id,account_id=self.own_account_id,listing_id=self.own_listing_id,**kwargs)

    def test_unassessed_cards_are_visible_without_orm_or_provider_writes(self):
        with self.app.app_context():
            MarketplaceQualityAssessment.query.delete();db.session.commit()
            with patch.object(MarketplaceQualityService,'recompute_account') as calc,patch.object(db.session,'commit') as commit:
                value=self._list();row=self._detail()
                self.assertEqual(value['summary']['total'],1);self.assertEqual(value['summary']['unassessed'],1)
                self.assertIsNone(value['summary']['average_saved_score'])
                self.assertEqual(row['state'],'unassessed');self.assertIsNone(row['score'])
                calc.assert_not_called();commit.assert_not_called()
            self.assertEqual(MarketplaceQualityAssessment.query.count(),0)

    def test_observed_zero_and_changed_and_old_are_distinct(self):
        with self.app.app_context():
            a=MarketplaceQualityAssessment.query.one();a.score=0;db.session.commit()
            value=self._list();self.assertEqual(value['summary']['average_saved_score'],0)
            self.assertEqual(value['items'][0]['score'],0);self.assertEqual(value['items'][0]['state'],'observed')
            a.evaluated_at=datetime.utcnow()-timedelta(days=2);db.session.commit()
            self.assertEqual(self._detail()['state'],'outdated')
            a.listing_fingerprint='z'*64;db.session.commit()
            self.assertEqual(self._detail()['state'],'changed');self.assertEqual(self._list(state='observed')['items'],[])

    def test_unicode_search_literal_wildcards_and_reason_json_whitespace(self):
        with self.app.app_context():
            row=db.session.get(MarketplaceListing,self.own_listing_id);row.title='ЧЁРНЫЙ товар 100%_test'
            a=MarketplaceQualityAssessment.query.one();a.reasons_json=json.dumps([{'code':'ozon_few_media'},'not-an-object',{'code':'ozon_few_media'}],indent=2)
            db.session.commit()
            self.assertEqual(self._list(search='чёрный')['pagination']['total'],1)
            self.assertEqual(self._list(search='%_')['pagination']['total'],1)
            self.assertEqual(self._list(search='100xx')['pagination']['total'],0)
            value=self._list(reason='ozon_few_media');self.assertEqual(value['pagination']['total'],1)
            self.assertEqual(value['summary']['reasons'][0]['count'],1)

    def test_foreign_assessment_and_listing_do_not_leak(self):
        with self.app.app_context():
            row=MarketplaceQualityAssessment.query.one();row.account_id=self.foreign_account_id;row.reasons_json='[{"code":"ozon_few_media"}]';db.session.commit()
            value=self._list();self.assertEqual(value['summary']['assessed'],0);self.assertEqual(value['summary']['reasons'],[])
            self.assertIsNone(value['items'][0]['assessment_id'])
            with self.assertRaises(MarketplaceQualityNotFound):detail(seller_id=self.seller1_id,account_id=self.own_account_id,listing_id=self.foreign_listing_id)
            with self.assertRaises(MarketplaceQualityNotFound):workspace(seller_id=self.seller1_id,account_id=self.foreign_account_id)

    def test_metrics_are_scoped_and_current_supported_observations(self):
        with self.app.app_context():
            a=MarketplaceQualityAssessment.query.one();a.metrics_json=json.dumps({'values':{'ordered_units':0,'views':123}})
            db.session.commit();self.assertTrue(all(r['value'] is None for r in self._detail()['metrics']))
            now=datetime.utcnow();snap=MarketplaceAnalyticsSync(seller_id=self.seller1_id,account_id=self.own_account_id,marketplace_id=a.marketplace_id,
                period_code='30d',period_start=(now-timedelta(days=29)).date(),period_end=now.date(),status='completed',phase='completed',request_fingerprint=request_fingerprint(period_start=(now-timedelta(days=29)).date(),period_end=now.date()),
                contract_version=MarketplaceAnalyticsService.CONTRACT_VERSION,completed_at=now)
            db.session.add(snap);db.session.flush();a.analytics_sync_id=snap.id;db.session.commit()
            metrics={r['code']:r['value'] for r in self._detail()['metrics']}
            self.assertEqual(metrics['ordered_units'],0);self.assertIsNone(metrics['views'])
            snap.account_id=self.foreign_account_id;db.session.commit()
            self.assertIsNone(self._detail()['analytics_snapshot'])
            self.assertTrue(all(r['value'] is None for r in self._detail()['metrics']))

    def test_photo_metadata_and_safe_links_do_not_request_images(self):
        with self.app.app_context():
            row=db.session.get(MarketplaceListing,self.own_listing_id);row.media_json=json.dumps({'primary_image':'https://img.test/a.jpg'});db.session.commit()
            item=self._list()['items'][0];self.assertEqual(item['image'],'https://img.test/a.jpg')
            self.assertEqual(item['url'],f'/marketplaces/listings/view/{self.own_listing_id}?account_id={self.own_account_id}')
            row.media_json='{"primary_image":"https://secret:password@img.test/a.jpg"}';db.session.commit()
            self.assertIsNone(self._detail()['image'])

    def test_budgets_and_connection_cleanup(self):
        with self.app.app_context():
            with patch('services.marketplace_quality_workspace.MAX_TEXT_BYTES',1):
                with self.assertRaises(QualityReadLimit):self._list()
            with patch('services.marketplace_quality_workspace.READ_SECONDS',-1):
                with self.assertRaises(QualityReadLimit):self._list()
            self.assertEqual(self._list()['pagination']['total'],1)
            with db.engine.connect() as c:self.assertGreater(c.exec_driver_sql('PRAGMA busy_timeout').scalar(),200)

    def test_fixed_query_count_and_summary_is_not_page_sum(self):
        with self.app.app_context():
            own=db.session.get(MarketplaceListing,self.own_listing_id)
            for i in range(28):db.session.add(MarketplaceListing(seller_id=self.seller1_id,account_id=self.own_account_id,marketplace_id=own.marketplace_id,
                offer_id=f'offer-{i}',external_product_id=f'product-{i}',normalized_status='active',is_available=True,is_archived=False,sync_fingerprint='d'*64))
            db.session.commit();statements=[]
            def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
            event.listen(db.engine,'before_cursor_execute',capture)
            try:value=self._list(page=2,per_page=25)
            finally:event.remove(db.engine,'before_cursor_execute',capture)
            self.assertEqual(len(value['items']),4);self.assertEqual(value['summary']['total'],29)
            self.assertEqual(value['summary']['unassessed'],28);self.assertLessEqual(len(statements),7)

    def test_workspace_http_scope_strict_query_and_no_store(self):
        user,login=self._auth()
        with user,login:
            base=f'/marketplaces/api/quality/workspace?account_id={self.own_account_id}'
            response=self.client.get(base);self.assertEqual(response.status_code,200);self.assertIn('private',response.headers['Cache-Control'])
            self.assertEqual(response.get_json()['data']['scope'],{'account_id':self.own_account_id,'marketplace_code':'ozon'})
            for extra in ['&page=0','&page=١','&foo=1','&account_id=2','&state=failed','&sort_by=unknown']:
                self.assertEqual(self.client.get(base+extra).status_code,400,extra)
            self.assertEqual(self.client.post(base,json={}).status_code,405)
