from datetime import timedelta
import json
from unittest.mock import Mock

import pytest

from models import (db, SellerMarketplaceAccount, MarketplaceCatalogSync, MarketplaceReadSchedule,
                    MarketplaceReadRequest, BackgroundJob, MarketplaceOperation)
from services.ozon_account_health import observe_account,HealthNotFound,DOMAIN_NAMES
from services import ozon_read_scheduler as worker
from services.ozon_read_requests import enqueue_read
from services.ozon_api_client import OzonSellerAPIClient
from tests.test_ozon_read_scheduler import scope,NOW,_empty_response


def health(scope,**kwargs):
    return observe_account(seller_id=scope.seller_id,account_id=scope.ids[0],
        config={'MARKETPLACE_OZON_ENABLED':True},now=kwargs.pop('now',NOW),
        scheduler_state=kwargs.pop('scheduler_state',{'state':'healthy','age_seconds':1,'checked_at':1}),**kwargs)


def domain(result,name):return next(row for row in result['domains'] if row['domain']==name)


def test_empty_is_unknown_not_green_and_no_credentials_decryption_or_http(scope,monkeypatch):
    decrypt=Mock(side_effect=AssertionError('health must not decrypt'))
    monkeypatch.setattr(SellerMarketplaceAccount,'get_credentials',decrypt)
    result=health(scope)
    assert result['scope']=={'marketplace':'ozon','account_id':scope.ids[0]}
    assert len(result['domains'])==6 and result['needs_attention']
    assert {row['freshness'] for row in result['domains']}=={'unknown'}
    assert domain(result,'reviews')['activity']=='access_unconfirmed'
    assert result['publication_permission_evaluated'] is False
    assert 'synthetic-secret' not in json.dumps(result)
    decrypt.assert_not_called();OzonSellerAPIClient.request.assert_not_called()


def test_completed_catalog_freshness_is_separate_from_partial_attempt(scope):
    run=MarketplaceCatalogSync(seller_id=scope.seller_id,marketplace_id=scope.marketplace_id,account_id=scope.ids[0],
        status='completed',phase='completed',completed_at=NOW-timedelta(minutes=10),started_at=NOW-timedelta(minutes=12),page_count=9)
    db.session.add(run);db.session.commit();identity=run.id
    partial=MarketplaceCatalogSync(seller_id=scope.seller_id,marketplace_id=scope.marketplace_id,account_id=scope.ids[0],status='running',started_at=NOW)
    db.session.add(partial);db.session.commit()
    row=domain(health(scope),'catalog');assert row['freshness']=='fresh' and row['activity']=='running'
    assert row['last_complete']['id']==identity and row['last_complete']['pages']==9
    assert domain(health(scope,now=NOW+timedelta(hours=1)),'catalog')['freshness']=='stale'


@pytest.mark.parametrize('name',['analytics','fulfillment','finance','reviews','questions'])
def test_real_domain_contract_and_window_are_required_for_freshness(scope,monkeypatch,name):
    account=db.session.get(SellerMarketplaceAccount,scope.ids[0]);account.capabilities_json='["reviews_read","questions_read"]';db.session.commit()
    def read(client,endpoint,payload):
        if endpoint in ('review_list','question_list'):return {'reviews' if endpoint=='review_list' else 'questions':[],'has_next':False,'last_id':None}
        return _empty_response(endpoint,payload)
    monkeypatch.setattr(OzonSellerAPIClient,'request',read)
    for _ in range(10):
        schedule=MarketplaceReadSchedule.query.filter_by(domain=name).first()
        result=worker.run_due_reads(domain=name,limit=1,now=schedule.next_due_at if schedule else NOW)
        if result['completed']:break
    row=domain(health(scope,now=NOW+timedelta(minutes=12)),name)
    assert row['freshness']=='fresh',row
    model=worker._domain(name).model;run=model.query.one()
    run.request_fingerprint='0'*64;db.session.commit()
    assert domain(health(scope,now=NOW+timedelta(minutes=12)),name)['freshness']=='unknown'


def test_manual_seven_day_snapshot_does_not_certify_regular_thirty_days(scope,monkeypatch):
    monkeypatch.setattr(OzonSellerAPIClient,'request',lambda self,e,p:_empty_response(e,p))
    enqueue_read(seller_id=scope.seller_id,account_id=scope.ids[0],domain='analytics',period_code='7d',now=NOW)
    assert worker.run_requested_reads(now=NOW)['completed']==1
    assert domain(health(scope),'analytics')['freshness']=='unknown'


def test_cooldown_queued_intent_and_revoked_credentials_remain_distinct(scope):
    enqueue_read(seller_id=scope.seller_id,account_id=scope.ids[0],domain='finance',period_code='30d',now=NOW)
    schedule=MarketplaceReadSchedule.query.one();schedule.cooldown_until=NOW+timedelta(hours=3);schedule.next_due_at=schedule.cooldown_until;schedule.last_error_code='provider_rate_limited';db.session.commit()
    row=domain(health(scope),'finance');assert row['activity']=='waiting' and row['freshness']=='unknown'
    assert row['due_lag_seconds'] is None and row['queue']['active']
    account=db.session.get(SellerMarketplaceAccount,scope.ids[0]);account.credential_version+=1;db.session.commit()
    assert domain(health(scope),'finance')['queue']['status']=='paused'
    assert MarketplaceReadRequest.query.one().status=='pending'  # Observing cannot retire it.


def test_stale_scheduler_does_not_change_data_or_operation_permission(scope):
    result=health(scope,scheduler_state={'state':'stale','age_seconds':120,'checked_at':1})
    assert result['scheduler']['state']=='stale' and result['needs_attention']
    assert result['publication_permission_evaluated'] is False


@pytest.mark.parametrize('change,expected', [('credential_expires_at','expired'),('is_active','disabled'),('connection_status','connection_unconfirmed')])
def test_account_states_do_not_promise_access(scope,change,expected):
    account=db.session.get(SellerMarketplaceAccount,scope.ids[0]);setattr(account,change,{'credential_expires_at':NOW,'is_active':False,'connection_status':'error'}[change]);db.session.commit()
    assert health(scope)['account']['state']==expected
    assert all(d['activity']=='account_unavailable' for d in health(scope)['domains'])


def test_catalog_pending_is_exact_account_and_only_allowlisted_public_metadata(scope):
    foreign=scope.add()
    for aid in (foreign,scope.ids[0]):
        row=BackgroundJob(seller_id=scope.seller_id,job_type='ozon_account_sync',job_uid=f'oc:{aid}:synthetic',status='pending',
            result_data=json.dumps({'account_id':aid,'code':'provider-secret-error-body','next_retry_at':'bad','credential_fingerprint':'never-public'}))
        db.session.add(row)
    db.session.commit()
    result=health(scope);row=domain(result,'catalog')
    assert row['queue']['active'] and row['activity']=='pending'
    assert 'never-public' not in json.dumps(result) and 'provider-secret' not in json.dumps(result)


def test_foreign_scope_and_invalid_identity_are_not_found(scope):
    for seller,account in [(scope.seller_id+1,scope.ids[0]),(scope.seller_id,True),(scope.seller_id,0)]:
        with pytest.raises(HealthNotFound):
            observe_account(seller_id=seller,account_id=account,config={},now=NOW)
    OzonSellerAPIClient.request.assert_not_called()


def test_catalog_denial_before_snapshot_is_visible_without_reading_private_progress(scope):
    row=BackgroundJob(seller_id=scope.seller_id,job_type='ozon_account_sync',job_uid=f'oc:{scope.ids[0]}:synthetic',status='failed',
        result_data=json.dumps({'account_id':scope.ids[0],'code':'ozon_catalog_access_denied'}),
        progress_data=json.dumps({'credential_fingerprint':'private-value','raw_error':'private-error'}))
    db.session.add(row);db.session.commit()
    result=health(scope);catalog=domain(result,'catalog')
    assert catalog['activity']=='access_denied' and catalog['freshness']=='unknown'
    assert 'private-value' not in json.dumps(result) and 'private-error' not in json.dumps(result)


def test_uncertain_history_is_bounded_exact_scope_and_observation_does_not_retry(scope):
    foreign=scope.add()
    for index in range(28):
        db.session.add(MarketplaceOperation(seller_id=scope.seller_id,marketplace_id=scope.marketplace_id,
            account_id=foreign if index==0 else scope.ids[0],operation_kind='price_update',status='uncertain',
            idempotency_key=f'synthetic-{index}',request_fingerprint='a'*64,contract_version='synthetic',
            attempt_count=1,created_at=NOW-timedelta(hours=3),updated_at=NOW-timedelta(hours=2)))
    db.session.commit()
    result=health(scope)
    assert len(result['operations'])==25 and result['operations_truncated']
    assert all(row['check_state']=='stopped' and row['attempts']==1 for row in result['operations'])
    assert all(db.session.get(MarketplaceOperation,row['id']).account_id==scope.ids[0] for row in result['operations'])
    assert MarketplaceOperation.query.count()==28
    OzonSellerAPIClient.request.assert_not_called()


def test_deadline_removes_progress_handler_and_restores_connection_timeout(scope,monkeypatch):
    import services.ozon_account_health as service
    from sqlalchemy import text
    engine=db.engine
    with engine.connect() as c:before=c.exec_driver_sql('PRAGMA busy_timeout').scalar()
    monkeypatch.setattr(service,'READ_SECONDS',-1)
    with pytest.raises(service.HealthError):
        with service._reader(engine) as c:
            c.execute(text('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT SUM(x) FROM n'))
    with engine.connect() as c:
        assert c.exec_driver_sql('PRAGMA busy_timeout').scalar()==before
        assert c.execute(text('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT MAX(x) FROM n')).scalar()==10000


def test_http_scope_auth_private_headers_and_refresh_is_get_only(scope,monkeypatch):
    from flask import current_app
    from flask_login import LoginManager
    from types import SimpleNamespace
    from routes.ozon_account_health import register_ozon_account_health_routes
    app=current_app._get_current_object();app.config.update(SECRET_KEY='synthetic',MARKETPLACE_OZON_ENABLED=True)
    LoginManager(app);register_ozon_account_health_routes(app)
    user=SimpleNamespace(is_authenticated=True,seller=SimpleNamespace(id=scope.seller_id))
    monkeypatch.setattr('routes.ozon_account_health.current_user',user);monkeypatch.setattr('flask_login.utils._get_user',lambda:user)
    client=app.test_client();url='/marketplaces/api/status?account_id='+str(scope.ids[0])
    response=client.get(url);assert response.status_code==200 and response.headers['Cache-Control']=='private, no-store'
    assert response.json['data']['scope']['account_id']==scope.ids[0]
    assert client.post(url).status_code==405
    for suffix in ['&seller_id=9','&account_id=2']:
        assert client.get(url+suffix).status_code==400
    for value in ['0','01','true','١','1.0']:
        assert client.get('/marketplaces/api/status?account_id='+value).status_code==400
    user.seller.id+=99
    assert client.get(url).status_code==404
    user.is_authenticated=False
    assert client.get(url,headers={'Accept':'application/json'}).status_code in (401,302)
    OzonSellerAPIClient.request.assert_not_called()


def test_cli_missing_path_is_not_created_and_never_prints_path(tmp_path):
    import subprocess
    import sys
    path=tmp_path/'missing.db'
    result=subprocess.run([sys.executable,'scripts/check_ozon_health.py','--database',str(path),
        '--seller-id','1','--account-id','1'],text=True,capture_output=True,timeout=15)
    assert result.returncode==2 and json.loads(result.stdout)['status']=='unavailable'
    assert not path.exists() and str(path) not in result.stdout+result.stderr
