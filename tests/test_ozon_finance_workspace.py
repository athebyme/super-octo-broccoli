"""Finance previews, completed snapshots, exact child scope and query budgets."""
from datetime import datetime, date
import json

import pytest
from sqlalchemy import event

from models import (db, Marketplace, MarketplaceFinanceFact, MarketplaceFinanceFactItem,
                    MarketplaceFinanceComponent, MarketplaceFinanceSync, MarketplaceListing,
                    MarketplacePosting, SellerMarketplaceAccount)
from services.marketplace_finance import MarketplaceFinanceService
from tests import test_marketplace_finance_routes as route_fixture


@pytest.fixture
def workspace():
    fixture = route_fixture.MarketplaceFinanceRoutesTest()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def get(f, suffix='', detail=False):
    auth, login = f._auth()
    path = '/marketplaces/api/finance' + (f'/{f.fact_id}' if detail else '')
    with auth, login:
        return f.client.get(path + f'?account_id={f.own_account_id}&view=compact' + suffix)


def listing(f, *, foreign=False):
    account = db.session.get(SellerMarketplaceAccount, f.foreign_account_id if foreign else f.own_account_id)
    row = MarketplaceListing(seller_id=account.seller_id, marketplace_id=account.marketplace_id,
        account_id=account.id, offer_id='offer', external_product_id='100', primary_sku='500',
        title='Secret other store' if foreign else 'Own product', normalized_status='active',
        is_available=True, is_archived=False, sync_fingerprint='a'*64,
        media_json=json.dumps({'primary_image':'https://photos.test/foreign.jpg' if foreign else 'https://photos.test/own.jpg'}))
    db.session.add(row); db.session.flush()
    return row


def child(f, i, listing_id=None, *, foreign=False, fact_id=None):
    scope = {'seller_id':f.seller2_id if foreign else f.seller1_id,
             'account_id':f.foreign_account_id if foreign else f.own_account_id,
             'fact_id':fact_id or f.fact_id, 'listing_id':listing_id}
    db.session.add(MarketplaceFinanceFactItem(**scope, external_sku=f'sku-{i}', match_status='matched' if listing_id else 'unmatched'))
    db.session.add(MarketplaceFinanceComponent(**scope, component_key=f'key-{i}', component_kind='item_fee',
        external_type_id=9 if foreign else 7, type_name='Foreign fee' if foreign else 'Delivery',
        external_sku=f'sku-{i}', amount='-0.0500', currency='RUB', rollup_role='explanatory_only'))


def test_compact_previews_and_all_detail_pages_are_reachable(workspace):
    f=workspace
    with f.app.app_context():
        own=listing(f)
        for i in range(121):child(f,i,own.id)
        db.session.commit()
    data=get(f).get_json()['data']; fact=data['items'][0]
    assert len(fact['items'])==len(fact['components'])==3
    assert fact['items_count']==fact['components_count']==121
    assert fact['items_truncated'] and fact['components_truncated']
    assert fact['items'][0]['listing']['title']=='Own product'
    assert fact['items'][0]['offer_id']=='offer'
    detail=get(f,'&item_page=3&component_page=3&per_page=50',detail=True).get_json()['data']
    assert len(detail['items'])==len(detail['components'])==21
    assert detail['items'][-1]['external_sku']=='sku-120'
    assert detail['components_pagination']=={'page':3,'per_page':50,'pages':3,'total':121}
    # A repeated type matches the parent exactly once; fee rows are explanatory.
    typed=get(f,'&type_id=7').get_json()['data']
    assert typed['pagination']['total']==1 and typed['totals'][0]['net']=='-10.0000'


def test_foreign_children_listing_posting_and_search_cannot_escape_scope(workspace):
    f=workspace
    with f.app.app_context():
        secret=listing(f,foreign=True);child(f,1,secret.id);child(f,2,secret.id,foreign=True)
        p=MarketplacePosting(seller_id=f.seller2_id,marketplace_id=secret.marketplace_id,
            account_id=f.foreign_account_id,posting_number='secret-posting',fulfillment_kind='fbs',status='delivered',
            source_endpoint='/v4/posting/fbs/list',sync_fingerprint='b'*64,last_seen_at=datetime.utcnow())
        db.session.add(p);db.session.flush();db.session.get(MarketplaceFinanceFact,f.fact_id).posting_id=p.id;db.session.commit()
    for detail in [False,True]:
        data=get(f,detail=detail).get_json()['data'];fact=data if detail else data['items'][0]
        assert len(fact['items'])==len(fact['components'])==1
        assert fact['items'][0]['listing'] is None and fact['items'][0]['listing_id'] is None
        assert fact['items'][0]['title'] is None and fact['items'][0]['match_status']=='unavailable'
        assert fact['items'][0]['offer_id'] is None
        assert fact['posting_url'] is None and fact['posting_id'] is None
        assert 'Secret other store' not in json.dumps(data) and 'Foreign fee' not in json.dumps(data)
    assert get(f,'&search=sku-2').get_json()['data']['pagination']['total']==0
    assert get(f,'&type_id=9').get_json()['data']['pagination']['total']==0
    assert [t['external_type_id'] for t in get(f).get_json()['data']['type_counts']]==[7]


def test_literal_search_and_query_validation_even_without_snapshot(workspace):
    f=workspace
    assert get(f,'&search=%25').get_json()['data']['pagination']['total']==0
    assert get(f,'&search=_').get_json()['data']['pagination']['total']==0
    with f.app.app_context():
        db.session.get(MarketplaceFinanceFact,f.fact_id).unit_number='exact_100%'
        db.session.commit()
    assert get(f,'&search=%25').get_json()['data']['pagination']['total']==1
    for suffix in ['&account_id=1','&period=30d&period=7d','&seller_id=2','&view=wide',
                   '&type_id=0','&page=١','&per_page=101','&type_id=²','&page='+'9'*5000]:
        assert get(f,suffix).status_code==400,suffix[:80]
    for suffix in ['&item_page=0','&component_page=-1','&per_page=101','&extra=1']:
        assert get(f,suffix,detail=True).status_code==400
    with f.app.app_context():
        db.session.get(MarketplaceFinanceFact,f.fact_id).sync.status='failed';db.session.commit()
    for suffix in ['&category=invalid','&sign=invalid','&search='+'a'*201]:
        assert get(f,suffix).status_code==400
    assert get(f).get_json()['data']['snapshot_sync'] is None
    assert get(f,detail=True).status_code==404


def test_mismatched_marketplace_or_snapshot_scope_is_never_visible(workspace):
    f=workspace
    with f.app.app_context():
        wb=Marketplace(code='wb',name='WB',adapter_code='wb',is_active=True);db.session.add(wb);db.session.flush()
        fact=db.session.get(MarketplaceFinanceFact,f.fact_id);fact.marketplace_id=wb.id;db.session.commit()
    assert get(f).get_json()['data']['pagination']['total']==0
    assert get(f,detail=True).status_code==404
    with f.app.app_context():
        fact=db.session.get(MarketplaceFinanceFact,f.fact_id);fact.marketplace_id=fact.sync.marketplace_id;fact.sync.account_id=f.foreign_account_id;db.session.commit()
    assert get(f,detail=True).status_code==404


def test_preview_query_count_does_not_grow_per_fact(workspace):
    f=workspace
    with f.app.app_context():
        own=listing(f);original=db.session.get(MarketplaceFinanceFact,f.fact_id);child(f,1,own.id)
        for i in range(20):
            fact=MarketplaceFinanceFact(sync_id=original.sync_id,seller_id=original.seller_id,
                marketplace_id=original.marketplace_id,account_id=original.account_id,accrual_id=f'additional-{i}',
                fact_date=date.today(),accrued_category='ITEM',total_amount='0',currency='RUB',amount_sign='zero',
                source_fingerprint='f'*64,observed_at=datetime.utcnow())
            db.session.add(fact);db.session.flush();child(f,i+2,own.id,fact_id=fact.id)
        db.session.commit()
        counts=[]
        for size in [1,25]:
            statements=[]
            def observe(*args):statements.append(args[2])
            event.listen(db.engine,'before_cursor_execute',observe)
            try:
                result=MarketplaceFinanceService.list_facts(seller_id=f.seller1_id,account_id=f.own_account_id,per_page=size,compact=True)
            finally:event.remove(db.engine,'before_cursor_execute',observe)
            assert len(result['items'])==min(size,21)
            counts.append(len(statements))
        assert counts[1]<=counts[0]+1 and counts[1]<=20,counts


def test_pinned_pages_keep_snapshot_and_period_during_refresh_and_next_day(workspace):
    from datetime import timedelta
    f=workspace
    first=get(f,'&per_page=1').get_json()['data']
    snapshot_id=first['snapshot_sync']['id']
    anchor=first['coverage']['requested_end']
    with f.app.app_context():
        original=db.session.get(MarketplaceFinanceFact,f.fact_id)
        edge=MarketplaceFinanceFact(sync_id=snapshot_id,seller_id=original.seller_id,
            marketplace_id=original.marketplace_id,account_id=original.account_id,accrual_id='edge-day',
            fact_date=date.today()-timedelta(days=29),accrued_category='ITEM',total_amount='25',currency='RUB',
            amount_sign='positive',source_fingerprint='e'*64,observed_at=datetime.utcnow())
        db.session.add(edge)
        source=original.sync
        newer=MarketplaceFinanceSync(seller_id=source.seller_id,marketplace_id=source.marketplace_id,
            account_id=source.account_id,period_code='30d',period_start=source.period_start,
            period_end=source.period_end,status='completed',phase='completed',current_date=source.current_date,
            contract_version=source.contract_version,request_fingerprint=source.request_fingerprint,
            completed_at=datetime.utcnow()+timedelta(seconds=1))
        db.session.add(newer);db.session.commit();new_id=newer.id
        pinned=MarketplaceFinanceService.list_facts(seller_id=f.seller1_id,account_id=f.own_account_id,
            snapshot_id=snapshot_id,as_of=anchor,today=date.today()+timedelta(days=1),page=2,per_page=1)
        assert pinned['snapshot_sync']['id']==snapshot_id and pinned['pagination']['total']==2
        assert pinned['coverage']['requested_end']==anchor and pinned['totals'][0]['net']=='15.0000'
        assert pinned['items'][0]['accrual_id']=='edge-day'
    assert get(f).get_json()['data']['snapshot_sync']['id']==new_id
    pinned=get(f,f'&snapshot_id={snapshot_id}&as_of={anchor}&page=2&per_page=1').get_json()['data']
    assert pinned['snapshot_sync']['id']==snapshot_id and pinned['totals'][0]['net']=='15.0000'
    for suffix in ['&as_of='+anchor,'&snapshot_id=0','&snapshot_id=١',
                   f'&snapshot_id={snapshot_id}&as_of=0001-01-01',
                   f'&snapshot_id={snapshot_id}&as_of=2026-02-30',
                   f'&snapshot_id={snapshot_id}&as_of=9999-01-01']:
        assert get(f,suffix).status_code==400,suffix
    assert get(f,'&snapshot_id=999999').status_code==404
    with f.app.app_context():
        db.session.get(MarketplaceFinanceSync,snapshot_id).status='running';db.session.commit()
    assert get(f,f'&snapshot_id={snapshot_id}').status_code==404
    with f.app.app_context():
        snap=db.session.get(MarketplaceFinanceSync,snapshot_id);snap.status='completed';snap.account_id=f.foreign_account_id;db.session.commit()
    assert get(f,f'&snapshot_id={snapshot_id}').status_code==404
