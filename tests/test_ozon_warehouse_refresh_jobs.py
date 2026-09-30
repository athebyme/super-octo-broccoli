"""Durable Ozon warehouse reads preserve last-good state and never write Ozon."""

from datetime import datetime, timedelta
import sqlite3

from cryptography.fernet import Fernet
from flask import Flask
import pytest

from models import (
    Marketplace, MarketplaceListing, MarketplaceWarehouse,
    MarketplaceWarehouseReadItem, MarketplaceWarehouseReadJob,
    MarketplaceWarehouseStock, Seller, SellerMarketplaceAccount, User, db,
)
from migrations.migrate_add_ozon_warehouse_reads import apply_migration
from services.ozon_api_client import OzonRateLimitError
from services import ozon_warehouse_reads as reads
from services.ozon_read_scheduler import run_requested_reads
from services.marketplace_operation_locks import try_account_operation_lock


T = datetime(2026, 9, 26, 12, 0, 0)


def warehouse_page(identity='7001', *, cursor='', has_next=False):
    return {'warehouses': [{'warehouse_id': identity, 'name': 'Склад ' + identity,
                            'status': 'created', 'warehouse_type': 'ORDINARY'}],
            'cursor': cursor, 'has_next': has_next}


def stock_page(*, products=None, cursor='', has_next=False):
    if products is None:
        products = [{'sku': 9001, 'offer_id': 'offer-1', 'product_id': 101,
                     'warehouse_id': 7001, 'warehouse_name': 'Склад 7001',
                     'present': 11, 'reserved': 3, 'free_stock': 8}]
    return {'products': products, 'cursor': cursor, 'has_next': has_next}


class FakeAdapter:
    def __init__(self, warehouse_pages=None, stock_pages=None, failure=None):
        self.warehouse_pages = warehouse_pages or {'': warehouse_page()}
        self.stock_pages = stock_pages or {'': stock_page()}
        self.failure = failure
        self.calls = []

    def read_warehouses(self, credentials, payload):
        assert not db.session.connection().connection.driver_connection.in_transaction
        self.calls.append(('warehouses', credentials.external_account_id, payload['cursor']))
        if self.failure:
            raise self.failure
        return self.warehouse_pages[payload['cursor']]

    def read_stocks_by_warehouse_fbs(self, credentials, payload):
        assert not db.session.connection().connection.driver_connection.in_transaction
        self.calls.append(('fbs_stock', credentials.external_account_id, payload['cursor']))
        if self.failure:
            raise self.failure
        return self.stock_pages[payload['cursor']]

    def close(self):
        pass


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    app = Flask(__name__)
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://',
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User(username='warehouse-read-owner', email='warehouse-read-owner@test.local', is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Warehouse Read Owner')
        foreign_user = User(username='warehouse-read-other', email='warehouse-read-other@test.local', is_active=True)
        foreign_user.set_password('synthetic-password')
        foreign_seller = Seller(user=foreign_user, company_name='Warehouse Read Other')
        marketplace = Marketplace(name='Ozon', code='ozon', adapter_code='ozon', is_active=True)
        db.session.add_all([seller, foreign_seller, marketplace])
        db.session.flush()
        account = SellerMarketplaceAccount(seller_id=seller.id, marketplace_id=marketplace.id,
                                           external_account_id='synthetic-client', label='Ozon',
                                           is_active=True, connection_status='connected')
        account.set_credentials({'api_key': 'synthetic-secret'})
        db.session.add(account)
        db.session.flush()
        listing = MarketplaceListing(seller_id=seller.id, marketplace_id=marketplace.id,
                                     account_id=account.id, offer_id='offer-1',
                                     external_product_id='101', primary_sku='9001',
                                     normalized_status='active', is_available=True,
                                     sync_fingerprint='a' * 64)
        db.session.add(listing)
        db.session.commit()
        yield {'seller': seller.id, 'foreign': foreign_seller.id,
               'account': account.id, 'listing': listing.id}
        db.session.remove()
        db.drop_all()


def enqueue(setup, kind='warehouses', now=T):
    args = {'seller_id': setup['seller'], 'kind': kind, 'now': now}
    args['account_id' if kind == 'warehouses' else 'listing_id'] = setup['account' if kind == 'warehouses' else 'listing']
    return reads.enqueue_refresh(**args)


def advance(job_id, fake, when):
    return reads.run_read_step(job_id, now=when, adapter_factory=lambda credentials: fake)


def test_enqueue_deduplicates_and_two_pages_apply_atomically(setup):
    fake = FakeAdapter({'': warehouse_page('7001', cursor='next', has_next=True),
                        'next': warehouse_page('7002')})
    first = enqueue(setup)
    assert enqueue(setup)['id'] == first['id']
    assert reads.due_candidate(now=T)['id'] == first['id']
    assert fake.calls == []
    assert advance(first['id'], fake, T + timedelta(seconds=10))['outcome'] == 'running'
    assert MarketplaceWarehouse.query.count() == 0
    assert MarketplaceWarehouseReadItem.query.count() == 1
    result = advance(first['id'], fake, T + timedelta(seconds=20))
    assert result == {'selected': 1, 'outcome': 'completed'}
    assert {r.external_warehouse_id for r in MarketplaceWarehouse.query.all()} == {'7001', '7002'}
    assert MarketplaceWarehouseReadItem.query.count() == 0
    assert reads.status_for_scope(seller_id=setup['seller'], kind='warehouses', account_id=setup['account'])['snapshot_id']
    assert fake.calls == [('warehouses', 'synthetic-client', ''), ('warehouses', 'synthetic-client', 'next')]
    assert MarketplaceWarehouse.query.filter_by(external_warehouse_id='7001').one().last_seen_at < MarketplaceWarehouse.query.filter_by(external_warehouse_id='7002').one().last_seen_at


def test_busy_account_claim_skips_slot_and_next_ready_account_runs(setup):
    first = enqueue(setup)
    original = db.session.get(SellerMarketplaceAccount, setup['account'])
    second_account = SellerMarketplaceAccount(
        seller_id=setup['seller'], marketplace_id=original.marketplace_id,
        external_account_id='synthetic-second-client', label='Second Ozon',
        is_active=True, connection_status='connected',
    )
    second_account.set_credentials({'api_key': 'synthetic-second-secret'})
    db.session.add(second_account)
    db.session.commit()
    second = reads.enqueue_refresh(seller_id=setup['seller'], kind='warehouses',
                                   account_id=second_account.id, now=T + timedelta(seconds=1))
    fake = FakeAdapter()
    claim = try_account_operation_lock(setup['account'])
    assert claim is not None
    try:
        result = run_requested_reads(
            limit=1, now=T + timedelta(seconds=2),
            warehouse_adapter_factory=lambda credentials: fake,
        )
    finally:
        claim.close()
    assert result['selected'] == 1 and result['completed'] == 1
    assert fake.calls == [('warehouses', 'synthetic-second-client', '')]
    busy = db.session.get(MarketplaceWarehouseReadJob, first['id'])
    assert busy.status == 'waiting_provider' and busy.error_code == 'account_busy'
    assert busy.next_due_at >= T + timedelta(seconds=62)
    assert db.session.get(MarketplaceWarehouseReadJob, second['id']).status == 'completed'


def test_crash_after_final_checkpoint_resumes_without_provider_call(setup):
    job = enqueue(setup)
    token = reads._claim(job['id'], T + timedelta(seconds=1))
    page = reads.OzonWarehouseContract.normalize_page(warehouse_page())
    assert reads._checkpoint(job['id'], token, page, 'warehouses', T + timedelta(seconds=2)) == (True, True)
    row = db.session.get(MarketplaceWarehouseReadJob, job['id'])
    row.lease_expires_at = T + timedelta(seconds=3)
    row.next_due_at = T + timedelta(seconds=3)
    db.session.commit()
    fake = FakeAdapter()
    assert advance(job['id'], fake, T + timedelta(seconds=4))['outcome'] == 'completed'
    assert fake.calls == []
    assert MarketplaceWarehouse.query.count() == 1
    assert MarketplaceWarehouseReadItem.query.count() == 0


def test_duplicate_second_page_fails_and_cleans_staging_without_projection(setup):
    job = enqueue(setup)
    fake = FakeAdapter({'': warehouse_page('7001', cursor='next', has_next=True),
                        'next': warehouse_page('7001')})
    advance(job['id'], fake, T + timedelta(seconds=1))
    outcome = advance(job['id'], fake, T + timedelta(seconds=2))
    assert outcome['outcome'] == 'failed'
    assert MarketplaceWarehouse.query.count() == 0
    assert MarketplaceWarehouseReadItem.query.count() == 0
    assert reads.status_for_job(seller_id=setup['seller'], job_id=job['id'])['code'] == 'invalid_snapshot'


def test_same_client_key_rotation_stops_old_cursor_before_read(setup):
    job = enqueue(setup)
    fake = FakeAdapter({'': warehouse_page('7001', cursor='next', has_next=True),
                        'next': warehouse_page('7002')})
    advance(job['id'], fake, T + timedelta(seconds=1))
    account = db.session.get(SellerMarketplaceAccount, setup['account'])
    account.set_credentials({'api_key': 'replacement-secret'})
    db.session.commit()
    result = advance(job['id'], fake, T + timedelta(seconds=2))
    assert result['outcome'] == 'failed'
    assert reads.status_for_job(seller_id=setup['seller'], job_id=job['id'])['code'] == 'credentials_changed'
    assert len(fake.calls) == 1
    assert MarketplaceWarehouseReadItem.query.count() == 0


def test_full_retry_after_and_expired_job_cooldown_are_durable(setup):
    job = enqueue(setup)
    fake = FakeAdapter(failure=OzonRateLimitError('synthetic', code='ozon_rate_limited',
                                                   status_code=429, retry_after=120, retriable=True))
    assert advance(job['id'], fake, T + timedelta(seconds=1))['outcome'] == 'unavailable'
    row = db.session.get(MarketplaceWarehouseReadJob, job['id'])
    assert row.next_due_at >= T + timedelta(seconds=121)
    assert advance(job['id'], fake, T + timedelta(seconds=60))['selected'] == 0
    assert len(fake.calls) == 1
    assert enqueue(setup)['id'] == job['id']
    later = T + timedelta(seconds=122)
    fake.failure = OzonRateLimitError('synthetic', code='ozon_rate_limited',
                                      status_code=429, retry_after=3 * 86400, retriable=True)
    assert advance(job['id'], fake, later)['outcome'] == 'failed'
    assert reads.status_for_job(seller_id=setup['seller'], job_id=job['id'])['code'] == 'provider_cooldown_exceeds_job_age'
    with pytest.raises(reads.WarehouseReadCooldown) as error:
        enqueue(setup, now=later + timedelta(seconds=1))
    assert error.value.next_attempt_at is not None


def test_fbs_empty_complete_snapshot_marks_only_exact_listing_unavailable(setup):
    warehouse = enqueue(setup)
    advance(warehouse['id'], FakeAdapter(), T + timedelta(seconds=1))
    first = enqueue(setup, 'fbs_stock', T + timedelta(seconds=2))
    assert advance(first['id'], FakeAdapter(), T + timedelta(seconds=3))['outcome'] == 'completed'
    stock = MarketplaceWarehouseStock.query.one()
    assert stock.is_available and stock.free_stock == 8
    second = enqueue(setup, 'fbs_stock', T + timedelta(seconds=4))
    assert advance(second['id'], FakeAdapter(stock_pages={'': stock_page(products=[])}), T + timedelta(seconds=5))['outcome'] == 'completed'
    db.session.refresh(stock)
    assert not stock.is_available
    assert stock.observed_at >= T + timedelta(seconds=5)
    assert MarketplaceWarehouseReadItem.query.count() == 0


def test_unknown_fbs_warehouse_keeps_last_good_stock(setup):
    warehouse = enqueue(setup)
    advance(warehouse['id'], FakeAdapter(), T + timedelta(seconds=1))
    first = enqueue(setup, 'fbs_stock', T + timedelta(seconds=2))
    advance(first['id'], FakeAdapter(), T + timedelta(seconds=3))
    previous = MarketplaceWarehouseStock.query.one()
    old_fingerprint, old_time = previous.sync_fingerprint, previous.observed_at
    wrong = stock_page(products=[{'sku': 9001, 'offer_id': 'offer-1', 'product_id': 101,
                                  'warehouse_id': 7002, 'warehouse_name': 'Unknown',
                                  'present': 5, 'reserved': 1, 'free_stock': 4}])
    second = enqueue(setup, 'fbs_stock', T + timedelta(seconds=4))
    outcome = advance(second['id'], FakeAdapter(stock_pages={'': wrong}), T + timedelta(seconds=5))
    assert outcome['outcome'] == 'failed'
    assert reads.status_for_job(seller_id=setup['seller'], job_id=second['id'])['code'] == 'unknown_warehouse'
    db.session.refresh(previous)
    assert previous.is_available and previous.sync_fingerprint == old_fingerprint and previous.observed_at == old_time
    assert MarketplaceWarehouseReadItem.query.count() == 0


def test_cancel_during_provider_read_prevents_late_checkpoint(setup):
    job = enqueue(setup)

    class CancellingAdapter(FakeAdapter):
        def read_warehouses(self, credentials, payload):
            result = super().read_warehouses(credentials, payload)
            reads.cancel_refresh(seller_id=setup['seller'], job_id=job['id'], now=T + timedelta(seconds=2))
            return result

    fake = CancellingAdapter()
    assert advance(job['id'], fake, T + timedelta(seconds=1))['selected'] == 0
    assert reads.status_for_job(seller_id=setup['seller'], job_id=job['id'])['status'] == 'cancelled'
    assert MarketplaceWarehouseReadItem.query.count() == 0
    assert MarketplaceWarehouse.query.count() == 0


def test_stage_caps_fail_before_partial_apply(setup):
    job = enqueue(setup)
    token = reads._claim(job['id'], T + timedelta(seconds=1))
    row = db.session.get(MarketplaceWarehouseReadJob, job['id'])
    row.staged_count = reads.MAX_ROWS
    row.staged_bytes = reads.MAX_STAGED_BYTES
    db.session.commit()
    page = reads.OzonWarehouseContract.normalize_page(warehouse_page())
    with pytest.raises(reads._PageFailure) as error:
        reads._checkpoint(job['id'], token, page, 'warehouses', T + timedelta(seconds=2))
    assert error.value.code == 'response_too_large'
    assert MarketplaceWarehouse.query.count() == 0
    db.session.rollback()


def test_cancel_and_tenant_scope_invalidate_stale_worker(setup):
    job = enqueue(setup)
    token = reads._claim(job['id'], T + timedelta(seconds=1))
    with pytest.raises(reads.WarehouseReadNotFound):
        reads.status_for_job(seller_id=setup['foreign'], job_id=job['id'])
    assert reads.cancel_refresh(seller_id=setup['seller'], job_id=job['id'], now=T + timedelta(seconds=2))['status'] == 'cancelled'
    page = reads.OzonWarehouseContract.normalize_page(warehouse_page())
    assert reads._checkpoint(job['id'], token, page, 'warehouses', T + timedelta(seconds=3)) == (False, False)
    assert MarketplaceWarehouseReadItem.query.count() == 0
    assert MarketplaceWarehouse.query.count() == 0


def test_eight_long_paused_stages_release_private_capacity_for_ninth_ready_account(setup):
    marketplace_id = db.session.get(SellerMarketplaceAccount, setup['account']).marketplace_id
    account_ids = [setup['account']]
    for index in range(7):
        account = SellerMarketplaceAccount(
            seller_id=setup['seller'], marketplace_id=marketplace_id,
            external_account_id=f'synthetic-extra-{index}', label=f'Extra {index}',
            is_active=True, connection_status='connected')
        account.set_credentials({'api_key': f'synthetic-extra-key-{index}'})
        db.session.add(account)
        db.session.flush()
        account_ids.append(account.id)
    db.session.commit()
    paused = []
    for index, account_id in enumerate(account_ids):
        job = reads.enqueue_refresh(seller_id=setup['seller'], kind='warehouses',
                                    account_id=account_id, now=T)
        fake = FakeAdapter({'': warehouse_page(str(7100 + index), cursor='next', has_next=True)})
        assert advance(job['id'], fake, T + timedelta(seconds=index + 1))['outcome'] == 'running'
        fake.failure = OzonRateLimitError('synthetic', code='ozon_rate_limited', status_code=429,
                                          retry_after=3600 + index * 60, retriable=True)
        assert advance(job['id'], fake, T + timedelta(seconds=index + 10))['outcome'] == 'unavailable'
        row = db.session.get(MarketplaceWarehouseReadJob, job['id'])
        paused.append((row.id, row.next_due_at, row.failure_count, len(fake.calls)))
    assert MarketplaceWarehouseReadItem.query.count() == 8
    ninth = SellerMarketplaceAccount(
        seller_id=setup['seller'], marketplace_id=marketplace_id,
        external_account_id='synthetic-ninth', label='Ninth',
        is_active=True, connection_status='connected')
    ninth.set_credentials({'api_key': 'synthetic-ninth-key'})
    db.session.add(ninth)
    db.session.commit()
    job = reads.enqueue_refresh(seller_id=setup['seller'], kind='warehouses',
                                account_id=ninth.id, now=T + timedelta(seconds=20))
    fake = FakeAdapter({'': warehouse_page('7999')})
    assert advance(job['id'], fake, T + timedelta(seconds=21))['outcome'] == 'completed'
    assert fake.calls == [('warehouses', 'synthetic-ninth', '')]
    assert MarketplaceWarehouse.query.filter_by(account_id=ninth.id, external_warehouse_id='7999').count() == 1
    assert MarketplaceWarehouseReadItem.query.count() == 7
    reset = []
    for job_id, due, failures, prior_calls in paused:
        row = db.session.get(MarketplaceWarehouseReadJob, job_id)
        assert row.next_due_at == due and row.failure_count == failures
        assert row.status == 'waiting_provider'
        if row.staged_count == 0:
            reset.append(job_id)
            assert row.page_count == 0 and row.next_cursor is None
            assert advance(job_id, FakeAdapter(), T + timedelta(minutes=1))['selected'] == 0
    assert len(reset) == 1


def test_additive_migration_idempotent_with_partial_scope_indexes():
    connection = sqlite3.connect(':memory:')
    connection.execute('PRAGMA foreign_keys=ON')
    for name in ('sellers', 'marketplaces', 'seller_marketplace_accounts',
                 'marketplace_listings', 'marketplace_warehouse_syncs'):
        connection.execute(f'CREATE TABLE {name} (id INTEGER PRIMARY KEY)')
    assert apply_migration(connection, verbose=False) > 0
    assert apply_migration(connection, verbose=False) == 0
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert 'uq_marketplace_warehouse_read_active_account' in names
    assert 'uq_marketplace_warehouse_read_active_listing' in names
    assert 'observed_at' in {row[1] for row in connection.execute('PRAGMA table_info(marketplace_warehouse_read_items)')}
    connection.close()
