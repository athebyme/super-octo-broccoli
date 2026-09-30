"""Observed analytics: one pinned scope, exact decimals and bounded product pages."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from unittest.mock import patch

import pytest
from sqlalchemy import event

from models import db, Marketplace, MarketplaceAnalyticsSync as Sync, MarketplaceMetricFact as Fact, MarketplaceListing as Listing
from services.marketplace_analytics import MarketplaceAnalyticsService as Analytics, MarketplaceAnalyticsNotFound, MarketplaceAnalyticsValidationError
from services.marketplace_analytics_workspace import get_workspace, REVENUE, UNITS
from services.ozon_analytics_contracts import REQUEST_METRIC_DEFINITIONS, request_fingerprint
from tests import test_marketplace_analytics as service_fixture
from tests import test_marketplace_insight_routes as route_fixture
from tests.test_marketplace_analytics import SyntheticAnalyticsAdapter, SYNTHETIC_CREDENTIALS


@pytest.fixture
def data():
    fixture = service_fixture.MarketplaceAnalyticsServiceTest();fixture.setUp()
    fixture.run = Analytics.sync_account(seller_id=fixture.seller.id, account_id=fixture.account.id,
        period_code='7d', force=True, max_pages=2, adapter=SyntheticAnalyticsAdapter(), credentials=SYNTHETIC_CREDENTIALS)
    fixture.listing.media_json=json.dumps({'primary_image':'https://example.test/own.jpg'})
    db.session.commit()
    yield fixture
    fixture.tearDown()


def read(f, **kwargs):
    return get_workspace(seller_id=f.seller.id,account_id=f.account.id,period_code=kwargs.pop('period_code','7d'),**kwargs)


def metric(f, sku, values=('0.0001','1.0000'), **kw):
    rows=[]
    for definition,value in zip(REQUEST_METRIC_DEFINITIONS,values):
        row=Fact(sync_id=f.run.id,seller_id=f.seller.id,marketplace_id=f.marketplace.id,account_id=f.account.id,
            listing_id=f.listing.id,dimension_kind='listing',dimension_id=str(sku),dimension_name='Наблюдённый товар',
            metric_code=definition.metric_code,provider_metric=definition.provider_metric,metric_value=Decimal(value),
            unit=definition.unit,definition_code=definition.definition_code,cross_marketplace_comparable=False,
            source_endpoint='/v1/analytics/data',observed_at=datetime.utcnow())
        for key,value in kw.items():setattr(row,key,value)
        db.session.add(row);rows.append(row)
    return rows


def newer(f, **overrides):
    columns={c.name:getattr(f.run,c.name) for c in Sync.__table__.columns if c.name not in ['id','created_at','updated_at']}
    columns.update(completed_at=datetime.utcnow()+timedelta(seconds=1),totals_json=json.dumps(Analytics._stored_totals({REVENUE:Decimal('7'),UNITS:Decimal('2')})))
    columns.update(overrides)
    row=Sync(**columns);db.session.add(row);db.session.commit();return row


def test_exact_money_days_and_current_owned_photo(data):
    result=read(data)
    assert result['scope']['account_id']==data.account.id
    assert result['snapshot']['id']==data.run.id and result['snapshot']['period_matches_request']
    assert result['totals']=={REVENUE:'1200.0000',UNITS:'4.0000','average_unit_rub':'300.00'}
    assert len(result['daily'])==7
    assert all(d[REVENUE] is None for d in result['daily'][:-1])
    assert result['daily'][-1][REVENUE]=='1200.0000'
    assert result['products'][0]['image']=='https://example.test/own.jpg'
    assert result['products'][0]['url'].startswith('/marketplaces/listings/view/')
    assert result['products'][0]['metrics'][UNITS]=='4.0000'
    assert {d['metric_code'] for d in result['definitions']}=={REVENUE,UNITS}


def test_pinned_snapshot_and_day_do_not_change_on_new_completed(data):
    first=read(data);pin={'snapshot_id':first['snapshot']['id'],'as_of':first['as_of']}
    next_sync=newer(data)
    assert read(data)['snapshot']['id']==next_sync.id
    fixed=read(data,**pin,today=date.today()+timedelta(days=1))
    assert fixed['totals']==first['totals'] and fixed['products']==first['products']
    assert fixed['requested_period']==first['requested_period']
    with pytest.raises(MarketplaceAnalyticsNotFound):read(data,snapshot_id=next_sync.id,period_code='30d')


def test_missing_zero_and_wrong_definition_are_not_interchangeable(data):
    data.run.totals_json=json.dumps(Analytics._stored_totals({REVENUE:Decimal('0'),UNITS:Decimal('0')}));db.session.commit()
    result=read(data);assert result['totals']=={REVENUE:'0',UNITS:'0','average_unit_rub':None}
    data.run.totals_json=json.dumps({REVENUE:{'value':'999','unit':'count','definition_code':'other','cross_marketplace_comparable':False}})
    f=Fact.query.filter_by(sync_id=data.run.id,dimension_kind='listing',metric_code=REVENUE).one();f.definition_code='unconfirmed'
    db.session.commit();result=read(data)
    assert result['totals'][REVENUE] is None and result['totals'][UNITS] is None
    assert result['products'][0]['metrics'][REVENUE] is None
    assert result['products'][0]['metrics'][UNITS]=='4.0000'


def test_foreign_facts_and_listing_metadata_are_never_exposed(data):
    foreign=Listing(seller_id=data.other_seller.id,marketplace_id=data.marketplace.id,account_id=data.other_account.id,
        offer_id='foreign-secret-offer',external_product_id='secret',title='Foreign Secret Title',
        media_json=json.dumps({'primary_image':'https://example.test/foreign-secret.jpg'}),sync_fingerprint='f'*64)
    db.session.add(foreign);db.session.flush()
    for f in Fact.query.filter_by(sync_id=data.run.id,dimension_kind='listing'):f.listing_id=foreign.id
    metric(data,'9999',seller_id=data.other_seller.id,account_id=data.other_account.id,listing_id=foreign.id)
    wrong_market=Marketplace(code='fixture-other',name='Other',adapter_code='none');db.session.add(wrong_market);db.session.flush()
    metric(data,'8888',marketplace_id=wrong_market.id)
    db.session.commit()
    result=read(data);assert result['pagination']['total']==1
    product=result['products'][0];assert product['listing_id'] is None and product['url'] is None and product['image'] is None
    assert not product['matched'] and product['offer_id'] is None
    assert 'foreign-secret' not in json.dumps(result) and 'Foreign Secret' not in json.dumps(result)
    # Existing JSON consumers have the same tenant boundary.
    legacy=Analytics.get_products(seller_id=data.seller.id,account_id=data.account.id,period_code='7d',sync_id=data.run.id)
    assert len(legacy['items'])==1 and legacy['items'][0]['listing_id'] is None
    assert 'foreign-secret' not in json.dumps(legacy)


def test_conflicting_link_and_literal_unicode_search(data):
    data.listing.title='Ёлка_100% Артикул';db.session.commit()
    assert read(data,search='ёлка_100%')['pagination']['total']==1
    assert read(data,search='_x')['pagination']['total']==0
    assert read(data,search='%')['pagination']['total']==1
    facts=metric(data,'9900');facts[0].listing_id=None;db.session.commit()
    product=next(r for r in read(data)['products'] if r['sku']=='9900')
    assert product['listing_id'] is None and not product['matched']
    assert read(data,search='ёлка')['pagination']['total']==1


def test_sql_pagination_is_stable_and_bounded(data):
    for n in range(56):metric(data,str(9000+n))
    db.session.commit()
    first=read(data,per_page=25,sort_by=UNITS,sort_dir='asc')
    second=read(data,per_page=25,page=2,sort_by=UNITS,sort_dir='asc')
    last=read(data,per_page=25,page=3,sort_by=UNITS,sort_dir='asc')
    assert first['pagination']=={'page':1,'per_page':25,'total':57,'pages':3}
    assert len(first['products'])==len(second['products'])==25 and len(last['products'])==7
    assert len({r['sku'] for r in first['products']+second['products']+last['products']})==57
    assert first['products'][0]['sku']=='9000' and last['products'][-1]['sku']=='1101'
    assert first['products'][0]['metrics'][REVENUE]=='0.0001'
    seller_id,account_id=data.seller.id,data.account.id
    calls=[]
    def query(*args):
        if args[2].lstrip().upper().startswith('SELECT'):calls.append(args[2])
    event.listen(db.engine,'before_cursor_execute',query)
    try:
        a=get_workspace(seller_id=seller_id,account_id=account_id,period_code='7d',per_page=1)
        first_count=len(calls);calls.clear()
        b=get_workspace(seller_id=seller_id,account_id=account_id,period_code='7d',per_page=100)
        assert len(b['products'])==57 and len(a['products'])==1
        assert len(calls)<=first_count+1 and len(calls)<=8
    finally:event.remove(db.engine,'before_cursor_execute',query)


def test_explicit_invalid_or_missing_snapshot_never_falls_back(data):
    bad=newer(data,status='failed')
    with pytest.raises(MarketplaceAnalyticsNotFound):read(data,snapshot_id=bad.id)
    with pytest.raises(MarketplaceAnalyticsNotFound):read(data,snapshot_id=99999)
    bad.status='completed';bad.seller_id=data.other_seller.id;bad.account_id=data.other_account.id;db.session.commit()
    with pytest.raises(MarketplaceAnalyticsNotFound):read(data,snapshot_id=bad.id)
    with pytest.raises(MarketplaceAnalyticsValidationError):read(data,as_of=date.today().isoformat())
    with pytest.raises(MarketplaceAnalyticsValidationError):read(data,snapshot_id=data.run.id,as_of=(date.today()+timedelta(days=1)).isoformat())
    data.run.request_fingerprint='x'*64;db.session.commit()
    with pytest.raises(MarketplaceAnalyticsValidationError):read(data,snapshot_id=data.run.id)


def test_old_period_is_shown_as_observed_not_intersection(data):
    # SKU aggregate belongs to the whole old period, not the overlap with today.
    result=read(data,today=date.today()+timedelta(days=3))
    assert result['status']=='stale' and not result['snapshot']['period_matches_request']
    assert result['totals'][REVENUE]=='1200.0000'
    assert result['snapshot']['period_end']!=result['requested_period']['end']
    assert len(result['daily'])==7


def test_invalid_values_sort_as_unknown_and_malformed_dates_fail_closed(data):
    metric(data,'9900',values=('10000000000000000','3'));db.session.commit()
    products=read(data,sort_dir='desc')['products']
    assert products[-1]['sku']=='9900' and products[-1]['metrics'][REVENUE] is None
    fact=Fact.query.filter_by(sync_id=data.run.id,dimension_kind='day',metric_code=REVENUE).one()
    fact.fact_date=data.run.period_end+timedelta(days=1);db.session.commit()
    with pytest.raises(MarketplaceAnalyticsValidationError):read(data)


def test_read_transaction_keeps_facts_during_concurrent_retention(data,tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    seller_id,account_id,snapshot_id=data.seller.id,data.account.id,data.run.id
    expected=read(data);db.session.remove();original_engine=db.engines[None]
    path=tmp_path/'analytics-read-race.db'
    with original_engine.connect() as source:
        target=sqlite3.connect(path);source.connection.driver_connection.backup(target)
        target.execute('PRAGMA journal_mode=WAL');target.close()
    engine=create_engine('sqlite:///'+str(path));db.engines[None]=engine;deleted=[]
    def prune(connection,cursor,statement,parameters,context,executemany):
        if (not deleted and statement.startswith('SELECT marketplace_analytics_syncs.id,')
                and connection.connection.driver_connection.in_transaction):
            with sqlite3.connect(path) as writer:
                writer.execute('DELETE FROM marketplace_metric_facts WHERE sync_id=?',(snapshot_id,))
                writer.execute('DELETE FROM marketplace_analytics_syncs WHERE id=?',(snapshot_id,))
            deleted.append(True)
    event.listen(engine,'after_cursor_execute',prune)
    try:
        result=get_workspace(seller_id=seller_id,account_id=account_id,period_code='7d',snapshot_id=snapshot_id)
        assert deleted and result['totals']==expected['totals']
        assert result['daily']==expected['daily'] and result['products']==expected['products']
        with sqlite3.connect(path) as reader:
            assert reader.execute('SELECT COUNT(*) FROM marketplace_metric_facts').fetchone()[0]==0
    finally:
        event.remove(engine,'after_cursor_execute',prune);db.session.remove()
        db.engines[None]=original_engine;engine.dispose()


def test_no_snapshot_returns_unknown_totals_and_empty_rows(data):
    result=get_workspace(seller_id=data.seller.id,account_id=data.account.id,period_code='30d')
    assert result['status']=='no_data' and result['snapshot'] is None
    assert all(v is None for v in result['totals'].values())
    assert result['products']==result['daily']==[]


def test_workspace_route_is_strict_and_seller_scoped():
    f=route_fixture.MarketplaceInsightRoutesTest();f.setUp()
    try:
        with f._auth()[0],f._auth()[1]:
            assert f.client.get(f'/marketplaces/api/analytics/workspace?account_id={f.own_account_id}').status_code==200
            assert f.client.get(f'/marketplaces/api/analytics/workspace?account_id={f.foreign_account_id}').status_code==404
            for suffix in ['&account_id=2','&page=01','&page=٢','&per_page=101','&sort_dir=bad','&as_of=2026-07-15','&snapshot_id=0','&view=other','&search='+('x'*201)]:
                assert f.client.get(f'/marketplaces/api/analytics/workspace?account_id={f.own_account_id}'+suffix).status_code==400,suffix
    finally:f.tearDown()
