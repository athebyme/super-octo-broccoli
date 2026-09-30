"""Explicit offline capacity probe; run by path, outside routine pytest collection.

One synthetic 1000-item Ozon page carries a near-64 MiB streamed attributes
response. Measure insert and repeat-update apply paths, including one exact
offer link. This fixture is representative, not a universal provider bound.
No network or live seller state is used.
"""

import json
import resource
import time
import requests
from sqlalchemy import event

from models import ImportedProduct, MarketplaceCatalogPageCheckpoint, MarketplaceListing, db
from services.marketplace_listings import MarketplaceListingService
from services.ozon_read_response import bound_read_responses
from tests.test_ozon_catalog_checkpoints import PagingCatalog
from tests.test_ozon_account_sync import connected, setup
from tests.test_marketplace_listing_service import SYNTHETIC_CREDENTIALS

class Raw:
    def __init__(self, body):
        self.body = body
    def stream(self, amount, decode_content=True):
        for offset in range(0, len(self.body), amount):
            yield self.body[offset:offset + amount]
    def close(self):
        pass
    def release_conn(self):
        pass

class BigCatalog(PagingCatalog):
    def __init__(self):
        super().__init__([1000])
        self.raw_bytes = 0
    def get_product_attributes(self, credentials, payload):
        self._attempt('attributes', 0)
        items = []
        for n in range(1, 1001):
            values = [{'value': f'{n:04d}-{i}-' + 'x' * 7993} for i in range(7)]
            items.append({'id': n, 'offer_id': f'offer-{n}', 'attributes': [
                {'id': 7777, 'values': values}],
                'ignored_provider_field': f'{n:04d}' + 'y' * 8996})
        body = json.dumps({'result': items, 'total': 1000, 'last_id': ''},
                          separators=(',', ':')).encode()
        self.raw_bytes = len(body)
        response = requests.Response()
        response.status_code = 200
        response.headers['Content-Length'] = str(len(body))
        response.raw = Raw(body)
        class Session:
            def request(self, *args, **kwargs):
                return response
        session = Session()
        bound_read_responses(session, maximum_bytes=64 * 1024 * 1024)
        return session.request('POST', 'synthetic').json()

def test_large_bounded_catalog_page(setup, monkeypatch):
    connected(setup)
    db.session.add(ImportedProduct(
        seller_id=setup.seller_id, external_id='source-1',
        external_vendor_code='offer-1', source_type='synthetic',
        title='Linked synthetic item', ai_attributes='{}',
    ))
    db.session.commit()
    adapter = BigCatalog()
    metrics = []
    current = {}
    in_apply = [False]
    original = MarketplaceListingService._apply_catalog_page.__func__
    def before_sql(conn, cursor, statement, parameters, context, executemany):
        if in_apply[0] and 'first_dml' not in current and statement.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')):
            current['first_dml'] = time.perf_counter()
    event.listen(db.engine, 'before_cursor_execute', before_sql)
    def measured(cls, **kwargs):
        current.clear()
        in_apply[0] = True
        current['apply_start'] = time.perf_counter()
        try:
            return original(cls, **kwargs)
        finally:
            current['apply_end'] = time.perf_counter()
            metrics.append(current.copy())
            in_apply[0] = False
    monkeypatch.setattr(MarketplaceListingService, '_apply_catalog_page', classmethod(measured))
    run = MarketplaceListingService.sync_ozon_account(
        seller_id=setup.seller_id, account_id=setup.account_id,
        adapter=adapter, credentials=SYNTHETIC_CREDENTIALS, max_pages=1)
    assert run.page_count == 1 and MarketplaceListing.query.count() == 1000
    adapter.tick_calls = 0
    updated = MarketplaceListingService.sync_ozon_account(
        seller_id=setup.seller_id, account_id=setup.account_id,
        adapter=adapter, credentials=SYNTHETIC_CREDENTIALS,
        max_pages=1, force_restart=True)
    assert updated.page_count == 1 and updated.updated_count == 1000
    assert MarketplaceListing.query.count() == 1000
    assert MarketplaceListing.query.filter_by(offer_id='offer-1').one().link_source == 'exact_offer_identity'
    stage_remaining = MarketplaceCatalogPageCheckpoint.query.count()
    assert stage_remaining == 0
    event.remove(db.engine, 'before_cursor_execute', before_sql)
    print('CATALOG_CAPACITY_METRICS ' + json.dumps({
        'raw_response_bytes': adapter.raw_bytes,
        'response_cap_bytes': 64 * 1024 * 1024,
        'items': 1000,
        'stage_remaining': stage_remaining,
        'insert_apply_wall_seconds': round(metrics[0]['apply_end']-metrics[0]['apply_start'], 3),
        'insert_first_dml_to_return_seconds': round(metrics[0]['apply_end']-metrics[0]['first_dml'], 3),
        'update_apply_wall_seconds': round(metrics[1]['apply_end']-metrics[1]['apply_start'], 3),
        'update_first_dml_to_return_seconds': round(metrics[1]['apply_end']-metrics[1]['first_dml'], 3),
        'maxrss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'calls': dict(adapter.calls),
    }, sort_keys=True))
