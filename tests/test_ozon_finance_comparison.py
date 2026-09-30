from datetime import datetime, timedelta
from decimal import Decimal
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from models import (db, SellerMarketplaceAccount, MarketplaceFinanceSync as Sync,
    MarketplaceFinanceFact as Fact, MarketplaceFinanceFactItem as Item, MarketplaceFinanceComponent as Component)
from services import marketplace_finance_comparison as service
from services.marketplace_finance import MarketplaceFinanceService as Finance, MarketplaceFinanceValidationError, MarketplaceFinanceNotFound
from tests.test_ozon_finance_workspace import workspace


def pair(f, *, shifted=0, foreign=False):
    old = db.session.get(Fact, f.fact_id).sync
    old.fact_count = 1
    old.completed_at = datetime.utcnow() - timedelta(hours=2)
    account = db.session.get(SellerMarketplaceAccount, f.foreign_account_id if foreign else f.own_account_id)
    run = Finance._create_run(account=account, period_code='30d', period_start=old.period_start + timedelta(days=shifted),
        period_end=old.period_end + timedelta(days=shifted), now=datetime.utcnow() - timedelta(minutes=2))
    run.status='completed';run.completed_at=datetime.utcnow()-timedelta(minutes=1);db.session.commit()
    return old, run


def add(run, identity, amount, *, currency='RUB', date=None):
    amount=Decimal(amount)
    row=Fact(sync_id=run.id,seller_id=run.seller_id,marketplace_id=run.marketplace_id,account_id=run.account_id,
        accrual_id=identity,fact_date=date or run.period_end,unit_number='own-posting',accrued_category='NON_ITEM',
        total_amount=amount,currency=currency,amount_sign='positive' if amount>0 else 'negative' if amount<0 else 'zero',
        definition_code=Finance.DEFINITION_CODE,contract_version=Finance.CONTRACT_VERSION,
        source_endpoint='/v1/finance/accrual/by-day',source_fingerprint='a'*64,observed_at=datetime.utcnow())
    db.session.add(row);run.fact_count+=1;db.session.flush();return row


def compare(f, old, new, **kwargs):
    db.session.commit()
    return service.compare(seller_id=f.seller1_id,account_id=f.own_account_id,older_id=old.id,newer_id=new.id,**kwargs)


def component(fact, key, amount='-0.1000', label='Delivery'):
    row=Component(fact_id=fact.id,seller_id=fact.seller_id,account_id=fact.account_id,component_key=key,
        component_kind='non_item_fee',external_type_id=int(key),type_name=label,amount=Decimal(amount),currency='RUB',rollup_role='explanatory_only')
    db.session.add(row);return row


def test_exact_decimal_totals_all_change_kinds_zero_and_currency_are_separate(workspace,monkeypatch):
    f=workspace
    decrypt=Mock(side_effect=AssertionError('must not decrypt'));monkeypatch.setattr(SellerMarketplaceAccount,'get_credentials',decrypt)
    provider=Mock(side_effect=AssertionError('must not call provider'));monkeypatch.setattr('services.ozon_api_client.OzonSellerAPIClient.request',provider)
    with f.app.app_context():
        old,new=pair(f);add(new,'own-accrual','-9.9999');add(old,'missing','0.0000');add(new,'usd','0.1001',currency='USD')
        result=compare(f,old,new)
        assert result['counts']=={'added':1,'missing':1,'changed':1,'unchanged':0}
        assert result['totals']==[
            {'currency':'RUB','older':'-10.0000','newer':'-9.9999','delta':'0.0001','older_count':2,'newer_count':1},
            {'currency':'USD','older':'0','newer':'0.1001','delta':'0.1001','older_count':0,'newer_count':1}]
        absent=next(r for r in result['items'] if r['kind']=='missing');assert absent['older']['amount']=='0.0000' and absent['newer'] is None
        assert result['observation_only'] and result['accounting_reconciliation'] is False
    decrypt.assert_not_called();provider.assert_not_called()


def test_mixed_currency_change_is_not_subtracted_as_one_money_value(workspace):
    f=workspace
    with f.app.app_context():
        old,new=pair(f);add(new,'own-accrual','-10',currency='USD');result=compare(f,old,new)
        assert result['items'][0]['fields']==['currency']
        assert [(r['currency'],r['delta']) for r in result['totals']]==[('RUB','10.0000'),('USD','-10.0000')]


def test_child_order_current_names_and_local_links_do_not_create_changes(workspace):
    f=workspace
    with f.app.app_context():
        old,new=pair(f);left=db.session.get(Fact,f.fact_id);right=add(new,'own-accrual','-10')
        for fact,keys,label in [(left,['1','2'],'Old label'),(right,['2','1'],'New label')]:
            for key in keys:component(fact,key,label=label)
            for sku in keys:db.session.add(Item(fact_id=fact.id,seller_id=fact.seller_id,account_id=fact.account_id,external_sku=sku,match_status='unmatched'))
        right.source_fingerprint='b'*64
        result=compare(f,old,new);assert result['counts']['unchanged']==1 and not result['items']
        row=Component.query.filter_by(fact_id=right.id,external_type_id=1).one();row.amount=Decimal('-0.2')
        result=compare(f,old,new);assert result['items'][0]['fields']==['components'];assert result['totals'][0]['delta']=='0.0000'


def test_rolling_window_edges_never_become_missing_or_added(workspace):
    f=workspace
    with f.app.app_context():
        old,new=pair(f,shifted=1);add(old,'old-edge','-1',date=old.period_start);add(new,'new-edge','-2');add(new,'own-accrual','-10',date=old.period_end)
        result=compare(f,old,new)
        assert result['period']=={'start':new.period_start.isoformat(),'end':old.period_end.isoformat(),'common_dates_only':True}
        assert result['counts']['unchanged']==1 and not result['items'];assert result['totals'][0]['older_count']==result['totals'][0]['newer_count']==1


@pytest.mark.parametrize('failure',['same','reverse','foreign','partial','pruned','no-overlap','fingerprint','contract','incomplete'])
def test_unconfirmed_pairs_fail_without_latest_fallback(workspace,failure):
    f=workspace
    with f.app.app_context():
        old,new=pair(f,shifted=31 if failure=='no-overlap' else 0,foreign=failure=='foreign');add(new,'own-accrual','-10')
        if failure=='partial':new.status='running'
        if failure=='fingerprint':new.request_fingerprint='x'*64
        if failure=='contract':new.contract_version='old-contract'
        if failure=='incomplete':new.fact_count+=1
        old_id,new_id=old.id,new.id
        if failure=='pruned':db.session.delete(new)
        db.session.commit()
        if failure=='same':new_id=old_id
        if failure=='reverse':old_id,new_id=new_id,old_id
        with pytest.raises((MarketplaceFinanceNotFound,MarketplaceFinanceValidationError)):
            service.compare(seller_id=f.seller1_id,account_id=f.own_account_id,older_id=old_id,newer_id=new_id)


def test_exact_history_scope_anchor_truncation_and_filter_pagination(workspace):
    f=workspace
    with f.app.app_context():
        old,new=pair(f);add(new,'own-accrual','-9')
        for i in range(55):add(new,f'new-{i:03d}','1')
        result=compare(f,old,new,kind='added',page=2,per_page=50)
        assert len(result['items'])==5 and result['counts']['changed']==1 and result['pagination']['total']==55
        assert result['totals'][0]['newer']=='46.0000' # Summary remains the whole common period.
        history=service.history(seller_id=f.seller1_id,account_id=f.own_account_id,anchor_id=old.id)
        assert len(history['items'])==2 and history['anchor']['id']==old.id and not history['truncated']
        with pytest.raises(MarketplaceFinanceNotFound):service.history(seller_id=f.seller2_id,account_id=f.own_account_id)


@pytest.mark.parametrize('budget,value',[('MAX_FACTS',0),('MAX_CHILDREN',0),('MAX_TEXT_BYTES',1),('READ_SECONDS',-1)])
def test_limits_fail_whole_comparison_and_restore_connection(workspace,monkeypatch,budget,value):
    f=workspace
    with f.app.app_context():
        old,new=pair(f);row=add(new,'own-accrual','-10');component(row,'1');db.session.commit()
        with db.engine.connect() as c:before=c.exec_driver_sql('PRAGMA busy_timeout').scalar()
        monkeypatch.setattr(service,budget,value)
        with pytest.raises(service.ComparisonLimit):compare(f,old,new)
        with db.engine.connect() as c:
            assert c.exec_driver_sql('PRAGMA busy_timeout').scalar()==before
            assert c.execute(text('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<5000) SELECT MAX(x) FROM n')).scalar()==5000


def test_routes_are_get_only_scoped_canonical_private_and_feature_gated(workspace):
    f=workspace
    with f.app.app_context():
        old,new=pair(f);add(new,'own-accrual','-10');db.session.commit();old_id,new_id=old.id,new.id
    auth,login=f._auth()
    with auth,login:
        prefix=f'/marketplaces/api/finance/changes?account_id={f.own_account_id}&older={old_id}&newer={new_id}'
        response=f.client.get(prefix);assert response.status_code==200 and response.cache_control.private and response.cache_control.no_store
        for suffix in ['&account_id=1','&seller_id=1','&page=01','&per_page=101','&kind=refund','&older=2']:
            assert f.client.get(prefix+suffix).status_code==400
        assert f.client.get(prefix.replace(f'account_id={f.own_account_id}',f'account_id={f.foreign_account_id}')).status_code==404
        assert f.client.post(prefix,json={}).status_code==405
        assert f.client.get(f'/marketplaces/api/finance/history?account_id={f.own_account_id}&anchor_id={old_id}').status_code==200
        f.app.config['MARKETPLACE_OZON_ENABLED']=False;assert f.client.get(prefix).status_code==404


def test_history_is_bounded_but_exact_anchor_stays_addressable(workspace,monkeypatch):
    f=workspace
    with f.app.app_context():
        old,new=pair(f);db.session.commit();monkeypatch.setattr(service,'HISTORY_LIMIT',1)
        result=service.history(seller_id=f.seller1_id,account_id=f.own_account_id,anchor_id=old.id)
        assert result['truncated'] and [r['id'] for r in result['items']]==[new.id] and result['anchor']['id']==old.id


def test_foreign_children_and_display_names_cannot_leak_into_comparison(workspace):
    f=workspace
    with f.app.app_context():
        old,new=pair(f);right=add(new,'own-accrual','-10');component(right,'1')
        foreign=Component(fact_id=right.id,seller_id=f.seller2_id,account_id=f.foreign_account_id,component_key='foreign',
            component_kind='non_item_fee',external_type_id=999,type_name='Foreign secret',amount=Decimal('-200'),currency='USD',rollup_role='explanatory_only')
        db.session.add(foreign);result=compare(f,old,new)
        assert result['items'][0]['newer']['components_count']==1
        assert result['totals'][0]['delta']=='0.0000'
        assert 'Foreign secret' not in json.dumps(result) and 'USD' not in json.dumps(result)


def test_retention_during_read_keeps_one_coherent_snapshot(workspace,tmp_path,monkeypatch):
    import sqlite3
    from sqlalchemy import create_engine
    f=workspace
    with f.app.app_context():
        old,new=pair(f);add(new,'own-accrual','-9');db.session.commit();old_id,new_id=old.id,new.id
        path=tmp_path/'comparison.sqlite';db.session.remove()
        raw=db.engine.raw_connection()
        with sqlite3.connect(path) as target:raw.driver_connection.backup(target)
        raw.close()
        with sqlite3.connect(path) as writer:writer.execute('PRAGMA journal_mode=WAL')
        engine=create_engine('sqlite:///'+str(path))
        original=service._facts;removed=[]
        def read_then_retain(connection,account,snapshot,budget):
            rows=original(connection,account,snapshot,budget)
            if not removed:
                with sqlite3.connect(path,timeout=.2) as writer:
                    writer.execute('DELETE FROM marketplace_finance_facts WHERE sync_id=?',(new_id,))
                    writer.execute('DELETE FROM marketplace_finance_syncs WHERE id=?',(new_id,))
                removed.append(new_id)
            return rows
        monkeypatch.setattr(service,'_facts',read_then_retain)
        try:
            result=service.compare(seller_id=f.seller1_id,account_id=f.own_account_id,older_id=old_id,newer_id=new_id,engine=engine)
            assert result['totals'][0]['delta']=='1.0000' and result['counts']['changed']==1
            with pytest.raises(MarketplaceFinanceNotFound):
                service.compare(seller_id=f.seller1_id,account_id=f.own_account_id,older_id=old_id,newer_id=new_id,engine=engine)
        finally:engine.dispose()
