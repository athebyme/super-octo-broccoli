"""Durable Ozon catalog page continuation with synthetic provider only."""

from collections import Counter
from datetime import datetime, timedelta
import sqlite3
from unittest.mock import patch

import pytest
import requests

from models import (
    BackgroundJob, MarketplaceCatalogPageCheckpoint, MarketplaceCatalogPageItem,
    MarketplaceCatalogSync, MarketplaceListing, db,
)
from migrations.migrate_add_ozon_catalog_checkpoints import apply_migration
from services.marketplace_accounts import MarketplaceAccountService
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_listings import MarketplaceListingService, MarketplaceListingValidationError
from services.marketplace_credential_identity import ozon_credential_fingerprint
from services.marketplace_operation_locks import try_account_operation_lock
from services.ozon_catalog_checkpoints import CatalogPageCheckpointService
from services.ozon_api_client import OzonAPIError
from services.ozon_account_sync import MAX_READ_RESPONSE_BYTES, _BudgetedClient
from services.ozon_read_response import OzonReadResponseTooLarge
from tests.test_ozon_account_sync import advance, connected, enqueue, setup
from tests.test_marketplace_listing_service import SYNTHETIC_CREDENTIALS, SyntheticCatalogAdapter


class PagingCatalog:
    capabilities = {'catalog_read'}

    def __init__(self, parts, *, total=1000, fail_once=None):
        assert sum(parts) == total
        self.parts = parts
        self.total = total
        self.fail_once = fail_once
        self.failed = False
        self.calls = Counter()
        self.tick_calls = 0
        self.list_payloads = []
        self.cursors = {'attributes': [], 'prices': [], 'stocks': []}

    def require_capability(self, capability):
        assert capability == 'catalog_read'

    def _attempt(self, domain, page_index=0):
        if self.tick_calls >= 12:
            raise OzonAPIError('local budget', code='ozon_read_budget', retriable=True)
        assert not db.session.connection().connection.driver_connection.in_transaction
        self.tick_calls += 1
        self.calls[domain] += 1
        if self.fail_once == (domain, page_index) and not self.failed:
            self.failed = True
            raise OzonAPIError('synthetic cooldown', code='ozon_rate_limited',
                               status_code=429, retry_after=120, retriable=True)

    @staticmethod
    def _ids(payload):
        return [str(value) for value in payload.get('product_id', payload.get('filter', {}).get('product_id', []))]

    def _page(self, domain, payload, item_builder):
        cursor_key = 'last_id' if domain == 'attributes' else 'cursor'
        index = int(payload[cursor_key] or '0')
        self._attempt(domain, index)
        self.cursors[domain].append(index)
        assert payload['limit'] == 1000
        assert len(self._ids(payload)) == self.total
        start = sum(self.parts[:index]) + 1
        stop = start + self.parts[index]
        items = [item_builder(number) for number in range(start, stop)]
        next_cursor = str(index + 1) if index + 1 < len(self.parts) else ''
        if domain == 'attributes':
            return {'result': items, 'total': self.total, 'last_id': next_cursor}
        return {'items': items, 'total': self.total, 'cursor': next_cursor}

    def list_products(self, credentials, payload):
        self._attempt('list')
        self.list_payloads.append(payload)
        assert payload['limit'] == 1000
        assert payload['last_id'] == ''
        if payload['filter']['visibility'] == 'ARCHIVED':
            return {'result': {'items': [], 'total': 0, 'last_id': ''}}
        return {'result': {'items': [
            {'product_id': number, 'offer_id': f'offer-{number}',
             'archived': False, 'has_fbo_stocks': False, 'has_fbs_stocks': False}
            for number in range(1, self.total + 1)
        ], 'total': self.total, 'last_id': 'active-end'}}

    def get_products(self, credentials, payload):
        self._attempt('info')
        assert len(self._ids(payload)) == self.total
        return {'items': [
            {'id': number, 'offer_id': f'offer-{number}', 'name': f'Product {number}',
             'created_at': '2026-01-01T00:00:00Z'}
            for number in range(1, self.total + 1)
        ]}

    def get_product_attributes(self, credentials, payload):
        return self._page('attributes', payload, lambda number: {
            'id': number, 'offer_id': f'offer-{number}', 'name': f'Product {number}',
            'attributes': [],
        })

    def read_prices(self, credentials, payload):
        return self._page('prices', payload, lambda number: {
            'product_id': number, 'offer_id': f'offer-{number}',
            'price': {'price': '10.00', 'currency_code': 'RUB'},
        })

    def read_stocks(self, credentials, payload):
        return self._page('stocks', payload, lambda number: {
            'product_id': number, 'offer_id': f'offer-{number}', 'stocks': [],
        })


def _tick(setup, job_id, adapter, *, now=None):
    adapter.tick_calls = 0
    assert advance(setup, job_id, adapter, **({'now': now} if now else {}))
    assert adapter.tick_calls <= 12
    return db.session.get(BackgroundJob, job_id)


@pytest.mark.parametrize('parts', ([75] * 12 + [100], [50] * 20))
def test_1000_item_page_continues_13_or_20_enrichment_pages_without_replay(setup, parts):
    connected(setup)
    job_id = enqueue(setup).id
    adapter = PagingCatalog(parts)
    for _ in range(9):
        job = _tick(setup, job_id, adapter)
        if job.status == 'completed':
            break
        assert job.status == 'pending'
        assert job.get_progress()['failures'] == 0
        if adapter.calls['stocks'] < len(parts):
            assert MarketplaceListing.query.count() == 0
            assert MarketplaceCatalogSync.query.one().cursor == ''
    assert job.status == 'completed'
    assert MarketplaceListing.query.count() == 1000
    first_listing = MarketplaceListing.query.filter_by(external_product_id='1').one()
    last_listing = MarketplaceListing.query.filter_by(external_product_id='1000').one()
    assert first_listing.prices_synced_at < last_listing.prices_synced_at
    assert first_listing.stocks_synced_at < last_listing.stocks_synced_at
    assert first_listing.list_synced_at <= first_listing.prices_synced_at
    assert MarketplaceCatalogPageCheckpoint.query.count() == 0
    assert MarketplaceCatalogPageItem.query.count() == 0
    assert adapter.calls == Counter(list=2, info=1, attributes=len(parts),
                                    prices=len(parts), stocks=len(parts))
    assert adapter.cursors['attributes'] == list(range(len(parts)))
    assert adapter.cursors['prices'] == list(range(len(parts)))
    assert adapter.cursors['stocks'] == list(range(len(parts)))
    assert [payload['filter']['visibility'] for payload in adapter.list_payloads] == ['ALL', 'ARCHIVED']
    run = MarketplaceCatalogSync.query.one()
    assert run.page_count == 2 and run.seen_count == 1000 and run.status == 'completed'
    assert run.credential_fingerprint and len(run.credential_fingerprint) == 64
    assert 'credential_fingerprint' not in str(run.to_public_dict())


def test_crash_after_stage_commit_resumes_exact_cursor_without_partial_apply(setup):
    connected(setup)
    job_id = enqueue(setup).id
    adapter = PagingCatalog([50] * 20)
    _tick(setup, job_id, adapter)
    checkpoint = MarketplaceCatalogPageCheckpoint.query.one()
    assert checkpoint.domain == 'attributes'
    assert checkpoint.domain_cursor != ''
    assert MarketplaceListing.query.count() == 0
    first_cursor = checkpoint.domain_cursor
    first_items = MarketplaceCatalogPageItem.query.count()
    assert first_items > 1000  # info and committed attribute pages
    db.session.remove()  # worker process/session died after a stage commit
    _tick(setup, job_id, adapter)
    assert adapter.calls['list'] == 1 and adapter.calls['info'] == 1
    resumed = MarketplaceCatalogPageCheckpoint.query.one()
    assert resumed.domain_cursor != first_cursor or resumed.domain != 'attributes'
    assert MarketplaceCatalogPageItem.query.count() > first_items
    assert MarketplaceListing.query.count() == 0


def test_429_preserves_stage_and_due_then_resumes_without_repeating_early_pages(setup):
    connected(setup)
    job_id = enqueue(setup).id
    adapter = PagingCatalog([50] * 20, fail_once=('attributes', 2))
    job = _tick(setup, job_id, adapter)
    assert job.status == 'pending'
    assert job.get_result()['code'] == 'ozon_rate_limited'
    due = datetime.fromisoformat(job.get_result()['next_retry_at'].removesuffix('Z'))
    checkpoint = MarketplaceCatalogPageCheckpoint.query.one()
    assert checkpoint.domain == 'attributes' and checkpoint.domain_cursor == '2'
    calls_before = adapter.calls.copy()
    assert not advance(setup, job_id, adapter, now=due - timedelta(seconds=1))
    assert adapter.calls == calls_before
    _tick(setup, job_id, adapter, now=due + timedelta(seconds=1))
    assert adapter.calls['list'] == 1 and adapter.calls['info'] == 1
    assert adapter.cursors['attributes'][:3] == [0, 1, 2]
    assert adapter.cursors['attributes'].count(2) == 1


def test_label_version_change_keeps_same_credential_stage_but_legacy_null_run_restarts(setup):
    connected(setup)
    job_id = enqueue(setup).id
    adapter = PagingCatalog([50] * 20)
    _tick(setup, job_id, adapter)
    old_run = MarketplaceCatalogSync.query.one()
    old_checkpoint_id = MarketplaceCatalogPageCheckpoint.query.one().id
    setup.account.label = 'Renamed without rotating key'
    setup.account.version += 1
    db.session.commit()
    _tick(setup, job_id, adapter)
    assert adapter.calls['list'] == 1
    assert MarketplaceCatalogPageCheckpoint.query.one().id == old_checkpoint_id

    # A historical unbound run cannot prove that its cursor belongs to the
    # current stored key, even if its account ID is unchanged.
    old_run.credential_fingerprint = None
    old_run.status = 'failed'
    old_run.cursor = 'prior-committed'
    db.session.commit()
    result = MarketplaceListingService.sync_ozon_account(
        seller_id=setup.seller_id, account_id=setup.account_id,
        adapter=PagingCatalog([1000]),
        credentials=SYNTHETIC_CREDENTIALS,
        max_pages=1,
    )
    assert result.id != old_run.id and result.cursor != old_run.cursor
    assert MarketplaceCatalogPageCheckpoint.query.filter_by(run_id=old_run.id).count() == 0


def test_injected_key_must_match_stored_account_before_any_catalog_read(setup):
    connected(setup)
    adapter = PagingCatalog([1000])
    with pytest.raises(MarketplaceListingValidationError):
        MarketplaceListingService.sync_ozon_account(
            seller_id=setup.seller_id, account_id=setup.account_id,
            adapter=adapter,
            credentials=MarketplaceCredentials(
                external_account_id=setup.account.external_account_id,
                api_key='a-different-synthetic-key',
            ),
            max_pages=1,
        )
    assert not adapter.calls
    assert MarketplaceCatalogSync.query.count() == 0


def test_account_claim_is_released_if_recheck_raises_before_catalog_file_claim(setup):
    connected(setup)
    adapter = PagingCatalog([1000])
    with patch('services.marketplace_listings.MarketplaceAccountService.get_owned_account',
               side_effect=[setup.account, RuntimeError('synthetic account reload failure')]):
        with pytest.raises(RuntimeError, match='synthetic account reload failure'):
            MarketplaceListingService.sync_ozon_account(
                seller_id=setup.seller_id, account_id=setup.account_id,
                adapter=adapter, credentials=SYNTHETIC_CREDENTIALS, max_pages=1,
            )
    claim = try_account_operation_lock(setup.account_id)
    assert claim is not None
    claim.close()
    assert not adapter.calls


def test_account_claim_is_released_if_catalog_file_claim_errors(setup):
    connected(setup)
    adapter = PagingCatalog([1000])
    with patch.object(MarketplaceListingService, '_try_claim', side_effect=OSError('synthetic lock error')):
        with pytest.raises(OSError, match='synthetic lock error'):
            MarketplaceListingService.sync_ozon_account(
                seller_id=setup.seller_id, account_id=setup.account_id,
                adapter=adapter, credentials=SYNTHETIC_CREDENTIALS, max_pages=1,
            )
    claim = try_account_operation_lock(setup.account_id)
    assert claim is not None
    claim.close()
    assert not adapter.calls


def test_oversized_success_downshifts_before_apply_and_restarts_only_current_page(setup):
    connected(setup)

    class OversizedOnce(SyntheticCatalogAdapter):
        def __init__(self):
            super().__init__()
            self.limits = []

        def list_products(self, credentials, payload):
            self.limits.append(payload['limit'])
            if len(self.limits) == 1:
                raise OzonReadResponseTooLarge()
            return super().list_products(credentials, payload)

    adapter = OversizedOnce()
    first = MarketplaceListingService.sync_ozon_account(
        seller_id=setup.seller_id, account_id=setup.account_id,
        adapter=adapter, credentials=SYNTHETIC_CREDENTIALS, max_pages=1,
    )
    assert first.status == 'paused' and first.page_count == 0
    assert MarketplaceListing.query.count() == 0
    checkpoint = MarketplaceCatalogPageCheckpoint.query.one()
    assert checkpoint.domain == 'list' and checkpoint.page_limit == 500
    assert checkpoint.generation == 1 and checkpoint.staged_bytes == 0

    second = MarketplaceListingService.sync_ozon_account(
        seller_id=setup.seller_id, account_id=setup.account_id,
        adapter=adapter, credentials=SYNTHETIC_CREDENTIALS, max_pages=1,
    )
    assert second.id == first.id and second.page_count == 1
    assert MarketplaceListing.query.count() == 1
    assert adapter.limits == [1000, 500]


def test_single_item_oversized_response_stops_without_partial_catalog_apply(setup):
    connected(setup)

    class AlwaysOversized(SyntheticCatalogAdapter):
        def __init__(self):
            super().__init__()
            self.limits = []

        def list_products(self, credentials, payload):
            self.limits.append(payload['limit'])
            raise OzonReadResponseTooLarge()

    adapter = AlwaysOversized()
    for _ in range(12):
        try:
            run = MarketplaceListingService.sync_ozon_account(
                seller_id=setup.seller_id, account_id=setup.account_id,
                adapter=adapter, credentials=SYNTHETIC_CREDENTIALS, max_pages=1,
            )
        except Exception as exc:
            assert getattr(exc, 'code', None) == 'ozon_catalog_protocol_error'
            break
        assert run.status == 'paused' and MarketplaceListing.query.count() == 0
    else:
        pytest.fail('One-item read overflow did not stop')
    assert adapter.limits == [1000, 500, 250, 125, 62, 31, 15, 7, 3, 1]
    assert MarketplaceListing.query.count() == 0
    assert MarketplaceCatalogPageCheckpoint.query.count() == 0


@pytest.mark.parametrize('status', (200, 429))
def test_budgeted_catalog_client_really_installs_response_bound_without_hiding_429(status):
    response = requests.Response()
    response.status_code = status
    response.headers['Content-Length'] = str(MAX_READ_RESPONSE_BYTES + 1)
    response.headers['Retry-After'] = '120'
    response.raw = type('Body', (), {
        'close': lambda self: None,
        'release_conn': lambda self: None,
    })()
    with patch('requests.Session.request', return_value=response) as physical:
        client = _BudgetedClient(SYNTHETIC_CREDENTIALS)
        client.rate_budget = None
        try:
            with pytest.raises(OzonAPIError) as failure:
                client.request('product_list', {'filter': {}, 'last_id': '', 'limit': 1000})
        finally:
            client.session.close()
    assert physical.call_count == 1 and physical.call_args.kwargs['stream'] is True
    if status == 200:
        assert isinstance(failure.value, OzonReadResponseTooLarge)
    else:
        assert failure.value.status_code == 429 and failure.value.retry_after == 120


def test_retry_after_starts_at_observed_failure_after_prior_calls(setup):
    connected(setup)
    job_id = enqueue(setup).id
    adapter = PagingCatalog([50] * 20, fail_once=('list', 0))
    started = datetime(2026, 9, 26, 12, 0, 0)
    with patch('services.ozon_account_sync.time.monotonic', side_effect=[100.0, 145.0]):
        assert advance(setup, job_id, adapter, now=started)
    due = datetime.fromisoformat(db.session.get(BackgroundJob, job_id).get_result()['next_retry_at'].removesuffix('Z'))
    assert due >= started + timedelta(seconds=45 + 120)


def test_eighth_ttl_expiration_stops_owner_job_without_losing_exhaustion_counter(setup):
    connected(setup)
    job_id = enqueue(setup).id
    adapter = PagingCatalog([50] * 20)
    _tick(setup, job_id, adapter)
    checkpoint = MarketplaceCatalogPageCheckpoint.query.one()
    checkpoint.ttl_restarts = 8
    checkpoint.generation = 8
    checkpoint.started_at = datetime.utcnow() - timedelta(hours=1)
    db.session.commit()
    calls_before = adapter.calls.copy()
    job = _tick(setup, job_id, adapter)
    assert job.status == 'failed' and job.get_result()['code'] == 'retry_exhausted'
    assert adapter.calls == calls_before
    checkpoint = MarketplaceCatalogPageCheckpoint.query.one()
    assert checkpoint.ttl_restarts == 8 and checkpoint.generation == 8
    assert checkpoint.domain == 'list' and checkpoint.staged_bytes == 0
    assert MarketplaceCatalogPageItem.query.count() == 0
    assert MarketplaceListing.query.count() == 0


def test_ninth_ready_account_waits_without_eviction_or_repeated_list_reads(setup):
    connected(setup)
    accounts = [setup.account]
    for number in range(2, 10):
        account = MarketplaceAccountService.save_ozon_account(
            seller_id=setup.seller_id, external_account_id=f'synthetic-{number}',
            label=f'Synthetic {number}', api_key=f'key-{number}',
        )
        account.connection_status = 'connected'
        db.session.commit()
        accounts.append(account)
    adapters = {}
    credentials = {}
    for account in accounts:
        adapter = PagingCatalog([1] * 13, total=13)
        adapters[account.id] = adapter
        credentials[account.id] = MarketplaceCredentials(
            external_account_id=account.external_account_id,
            api_key=account.get_credentials()['api_key'],
        )
        run = MarketplaceListingService.sync_ozon_account(
            seller_id=setup.seller_id, account_id=account.id,
            adapter=adapter, credentials=credentials[account.id], max_pages=1,
        )
        assert run.status == 'paused'
    ninth = accounts[-1]
    assert not adapters[ninth.id].calls
    assert MarketplaceCatalogPageCheckpoint.query.filter(
        MarketplaceCatalogPageCheckpoint.domain != 'list',
    ).count() == 8
    first = accounts[0]
    for _ in range(4):
        adapters[first.id].tick_calls = 0
        run = MarketplaceListingService.sync_ozon_account(
            seller_id=setup.seller_id, account_id=first.id,
            adapter=adapters[first.id], credentials=credentials[first.id], max_pages=1,
        )
        if run.page_count == 1:
            break
    assert run.page_count == 1
    assert adapters[first.id].calls['list'] == 1
    assert MarketplaceCatalogPageCheckpoint.query.filter_by(run_id=run.id).count() == 0
    resumed = MarketplaceListingService.sync_ozon_account(
        seller_id=setup.seller_id, account_id=ninth.id,
        adapter=adapters[ninth.id], credentials=credentials[ninth.id], max_pages=1,
    )
    assert resumed.status == 'paused'
    assert adapters[ninth.id].calls['list'] == 1
    assert MarketplaceCatalogPageCheckpoint.query.filter(
        MarketplaceCatalogPageCheckpoint.domain != 'list',
    ).count() == 8
    for account in accounts[1:-1]:
        assert adapters[account.id].calls['list'] == 1


@pytest.mark.parametrize('victim', ('expired', 'cooldown', 'terminal'))
def test_capacity_reclaims_occupied_stage_behind_sixteen_dormant_metadata_rows(setup, victim):
    now = datetime.utcnow()
    accounts = [setup.account]
    for number in range(2, 11):
        account = MarketplaceAccountService.save_ozon_account(
            seller_id=setup.seller_id, external_account_id=f'capacity-{number}',
            label=f'Capacity {number}', api_key=f'key-{number}',
        )
        accounts.append(account)

    def checkpoint(account, domain, started_at):
        fingerprint = ozon_credential_fingerprint(account)
        run = MarketplaceCatalogSync(
            seller_id=account.seller_id, marketplace_id=account.marketplace_id,
            account_id=account.id, status='paused', phase='active', visibility='ALL',
            credential_fingerprint=fingerprint, started_at=started_at,
        )
        db.session.add(run)
        db.session.flush()
        row = MarketplaceCatalogPageCheckpoint(
            run_id=run.id, seller_id=account.seller_id,
            marketplace_id=account.marketplace_id, account_id=account.id,
            credential_fingerprint=fingerprint, phase='active', visibility='ALL',
            domain=domain, started_at=started_at, updated_at=started_at,
            staged_bytes=100 if domain != 'list' else 0,
        )
        db.session.add(row)
        db.session.flush()
        return row

    # These rows precede every occupied slot in the old unfiltered LIMIT 16.
    for _ in range(16):
        checkpoint(accounts[0], 'list', now - timedelta(hours=2))
    occupied = [checkpoint(account, 'info', now - timedelta(minutes=40)
                           if victim == 'expired' and index == 0 else now)
                for index, account in enumerate(accounts[1:9])]
    first_id = occupied[0].id
    if victim in ('cooldown', 'terminal'):
        account = accounts[1]
        job = BackgroundJob(
            job_uid=f'oc:{account.id}:capacity-test', seller_id=account.seller_id,
            job_type='ozon_account_sync',
            status='pending' if victim == 'cooldown' else 'failed',
        )
        job.set_result({'next_retry_at': (now + timedelta(minutes=5)).isoformat() + 'Z'}
                       if victim == 'cooldown' else {})
        db.session.add(job)
    db.session.commit()

    assert CatalogPageCheckpointService._capacity_available(
        now=now, own_account_id=accounts[-1].id,
    )
    reclaimed = db.session.get(MarketplaceCatalogPageCheckpoint, first_id)
    assert reclaimed.domain == 'list' and reclaimed.staged_bytes == 0
    if victim == 'expired':
        assert reclaimed.ttl_restarts == 1 and reclaimed.generation == 1
    elif victim == 'cooldown':
        assert reclaimed.ttl_restarts == 0 and reclaimed.generation == 1
    else:
        assert reclaimed.ttl_restarts == 0 and reclaimed.generation == 0
    assert MarketplaceCatalogPageCheckpoint.query.filter(
        MarketplaceCatalogPageCheckpoint.domain != 'list',
    ).count() == 7


def _minimal_migration_connection():
    connection = sqlite3.connect(':memory:')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.executescript('''
        CREATE TABLE sellers(id INTEGER PRIMARY KEY);
        CREATE TABLE marketplaces(id INTEGER PRIMARY KEY);
        CREATE TABLE seller_marketplace_accounts(id INTEGER PRIMARY KEY);
        CREATE TABLE marketplace_catalog_syncs(
            id INTEGER PRIMARY KEY, seller_id INTEGER, marketplace_id INTEGER,
            account_id INTEGER, cursor VARCHAR(1000), phase VARCHAR(20));
        INSERT INTO sellers VALUES(1);
        INSERT INTO marketplaces VALUES(2);
        INSERT INTO seller_marketplace_accounts VALUES(3);
        INSERT INTO marketplace_catalog_syncs VALUES(4,1,2,3,'historical','active');
    ''')
    return connection


def test_migration_is_additive_repeated_and_preserves_historical_cursor():
    connection = _minimal_migration_connection()
    try:
        first = apply_migration(connection, verbose=False)
        connection.commit()
        second = apply_migration(connection, verbose=False)
        connection.commit()
        assert first > 0 and second == 0
        assert connection.execute('SELECT cursor, credential_fingerprint FROM marketplace_catalog_syncs WHERE id=4').fetchone() == ('historical', None)
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('''INSERT INTO marketplace_catalog_page_checkpoints
                (run_id,seller_id,marketplace_id,account_id,credential_fingerprint,phase,visibility,
                 start_cursor,next_cursor,page_limit,generation,ttl_restarts,base_items_json,domain,
                 domain_cursor,domain_seen_count,domain_page_count,staged_bytes,domain_observed_at_json,
                 started_at,updated_at)
                VALUES (4,1,2,3,'hash','active','ALL','','',0,0,0,'[]','list','',0,0,0,'{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)''')
        connection.rollback()
    finally:
        connection.close()


def test_migration_rejects_malformed_existing_table_and_rolls_back_column():
    connection = _minimal_migration_connection()
    try:
        connection.execute('CREATE TABLE marketplace_catalog_page_items(id INTEGER PRIMARY KEY)')
        connection.commit()
        with pytest.raises(sqlite3.OperationalError):
            apply_migration(connection, verbose=False)
        assert 'credential_fingerprint' not in [row[1] for row in connection.execute('PRAGMA table_info(marketplace_catalog_syncs)')]
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='marketplace_catalog_page_checkpoints'").fetchone()
    finally:
        connection.close()
