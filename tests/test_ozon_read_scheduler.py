"""Durable attempts, fairness and restart recovery with synthetic reads only."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.fernet import Fernet
from flask import Flask
import pytest

from models import Marketplace, MarketplaceReadSchedule, Seller, SellerMarketplaceAccount, User, db
from services import ozon_read_scheduler as scheduler
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_operation_locks import _try_operation_lock, try_account_operation_lock
from services.ozon_api_client import OzonAPIError, OzonRateLimitError, OzonSellerAPIClient


NOW = datetime(2026, 9, 24, 23, 30)
DOMAINS = ('analytics', 'fulfillment', 'finance')


@pytest.fixture
def scope(monkeypatch):
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode('ascii'))
    app = Flask(__name__)
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://',
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User(username='scheduled', email='scheduled@test.local', password_hash='synthetic')
        seller = Seller(user=user, company_name='Synthetic')
        market = Marketplace(code='ozon', name='Ozon', adapter_code='ozon', is_active=True)
        db.session.add_all([seller, market])
        db.session.commit()
        account_ids = []

        def add_account(**values):
            account = SellerMarketplaceAccount(
                seller_id=seller.id, marketplace_id=market.id,
                external_account_id=f'synthetic-{len(account_ids)}',
                label='Synthetic', is_active=True, connection_status='connected',
            )
            account.set_credentials({'api_key': 'synthetic-secret'})
            for key, value in values.items():
                setattr(account, key, value)
            db.session.add(account)
            db.session.commit()
            account_ids.append(account.id)
            return account.id

        add_account()
        # A forgotten stub must fail locally, never hit the real marketplace.
        monkeypatch.setattr(OzonSellerAPIClient, 'request', Mock(side_effect=AssertionError('unexpected API')))
        yield SimpleNamespace(add=add_account, ids=account_ids, seller_id=seller.id,
                              marketplace_id=market.id)
        db.session.remove()
        db.drop_all()


def run(domain='analytics', **kwargs):
    return scheduler.run_due_reads(domain=domain, limit=kwargs.pop('limit', 1),
                                  now=kwargs.pop('now', NOW), **kwargs)


def completed(**kwargs):
    # Lease writes must be committed before any provider/service I/O.
    assert not db.session.connection().connection.driver_connection.in_transaction
    assert kwargs['recover_abandoned'] is True
    return SimpleNamespace(status='completed', completed_at=kwargs['now'],
                           period_code=kwargs['period_code'], period_end=kwargs['today'])


@pytest.mark.parametrize('domain', DOMAINS)
def test_more_than_50_fresh_accounts_cannot_hide_due_account(scope, monkeypatch, domain):
    for _ in range(60):
        scope.add()
    target = scope.ids[-1]
    spec = scheduler._domain(domain)
    cache = Mock(side_effect=lambda **kw: None if kw['account_id'] == target else completed(
        recover_abandoned=True, now=NOW, today=NOW.date(), period_code='30d'))
    monkeypatch.setattr(spec.service, '_fresh_cached_sync' if domain == 'analytics' else '_fresh_completed', cache)
    sync = Mock(side_effect=completed)
    monkeypatch.setattr(spec.service, 'sync_account', sync)
    assert run(domain)['completed'] == 1
    assert sync.call_args.kwargs['account_id'] == target
    assert MarketplaceReadSchedule.query.count() == 61
    assert run(domain)['selected'] == 0


@pytest.mark.parametrize('domain', DOMAINS)
def test_preflight_failure_has_durable_attempt_and_does_not_starve_others(scope, monkeypatch, domain):
    broken = db.session.get(SellerMarketplaceAccount, scope.ids[0])
    broken._credentials_encrypted = 'invalid-encrypted-value'
    db.session.commit()
    healthy = scope.add()
    spec = scheduler._domain(domain)
    sync = Mock(side_effect=completed)
    monkeypatch.setattr(spec.service, 'sync_account', sync)
    assert run(domain)['failed'] == 1
    sync.assert_not_called()
    state = MarketplaceReadSchedule.query.filter_by(account_id=broken.id, domain=domain).one()
    assert state.last_attempt_at == NOW
    assert state.next_due_at >= NOW + timedelta(minutes=10)
    assert state.consecutive_failures == 1
    db.session.remove()  # A fresh session/process must still observe the pause.
    assert run(domain)['completed'] == 1
    assert sync.call_args.kwargs['account_id'] == healthy


@pytest.mark.parametrize('domain', DOMAINS)
def test_long_retry_after_survives_restart_and_due_filter_precedes_limit(scope, monkeypatch, domain):
    for _ in range(104):
        scope.add()
    scheduler._discover(domain, NOW)
    MarketplaceReadSchedule.query.update({'next_due_at': NOW + timedelta(days=1)})
    target = scope.ids[-1]
    state = MarketplaceReadSchedule.query.filter_by(account_id=target).one()
    state.next_due_at = NOW
    db.session.commit()
    spec = scheduler._domain(domain)
    sync = Mock(side_effect=OzonRateLimitError('synthetic', status_code=429, retry_after=5000, retriable=True))
    monkeypatch.setattr(spec.service, 'sync_account', sync)
    assert run(domain)['failed'] == 1
    assert sync.call_args.kwargs['account_id'] == target
    db.session.refresh(state)
    due = state.next_due_at
    assert due >= NOW + timedelta(seconds=5000)
    assert state.last_error_code == 'provider_rate_limited'
    db.session.remove()
    assert run(domain, now=NOW + timedelta(seconds=4999))['selected'] == 0
    sync.assert_called_once()
    sync.side_effect = completed
    assert run(domain, now=due)['completed'] == 1


def test_missing_accounts_are_discovered_beyond_enrollment_limit(scope, monkeypatch):
    for _ in range(scheduler.DISCOVERY_LIMIT + 3):
        scope.add()
    spec = scheduler._domain('analytics')
    monkeypatch.setattr(spec.service, 'sync_account', Mock(side_effect=completed))
    run()
    assert MarketplaceReadSchedule.query.count() == scheduler.DISCOVERY_LIMIT
    run()
    assert MarketplaceReadSchedule.query.count() == len(scope.ids)


def test_expired_disconnected_and_foreign_scope_never_read(scope, monkeypatch):
    scope.add(credential_expires_at=NOW)
    scope.add(connection_status='disconnected')
    scope.add(is_active=False)
    scheduler._discover('analytics', NOW)
    row = MarketplaceReadSchedule.query.one()
    # Even a corrupt persisted owner must not authorize a provider call.
    row.seller_id = 999
    db.session.commit()
    sync = Mock(side_effect=completed)
    monkeypatch.setattr(scheduler._domain('analytics').service, 'sync_account', sync)
    assert run()['selected'] == 0
    sync.assert_not_called()


def test_live_claim_and_account_mutation_lock_prevent_io(scope, monkeypatch):
    sync = Mock(side_effect=completed)
    monkeypatch.setattr(scheduler._domain('analytics').service, 'sync_account', sync)
    claim = _try_operation_lock('ozon-read-analytics', scope.ids[0])
    try:
        assert run()['selected'] == 0
    finally:
        claim.close()
    account_claim = try_account_operation_lock(scope.ids[0])
    try:
        assert run()['selected'] == 0
    finally:
        account_claim.close()
    sync.assert_not_called()
    assert run(now=NOW + timedelta(minutes=1))['completed'] == 1


def test_stale_lease_can_resume_but_old_owner_cannot_finish(scope):
    scheduler._discover('analytics', NOW)
    row = MarketplaceReadSchedule.query.one()
    old_token = scheduler._claim(row, NOW)
    assert old_token
    assert scheduler._claim(row, NOW) is None
    later = NOW + timedelta(seconds=scheduler.LEASE_SECONDS)
    new_token = scheduler._claim(row, later)
    assert new_token and new_token != old_token
    assert not scheduler._finish(row, old_token, later, status='idle')
    assert scheduler._finish(row, new_token, later, status='pending', next_due_at=later)
    db.session.refresh(row)
    assert row.lease_token is None


@pytest.mark.parametrize('domain', DOMAINS)
def test_worker_never_steals_a_live_domain_service_claim(scope, domain):
    spec = scheduler._domain(domain)
    claim = spec.service._try_claim(scope.ids[0])
    assert claim is not None
    try:
        assert run(domain)['failed'] == 1
        state = MarketplaceReadSchedule.query.one()
        assert state.last_error_code == 'account_busy'
        assert state.consecutive_failures == 0
        assert state.next_due_at >= NOW + timedelta(seconds=60)
        assert spec.model.query.count() == 0
    finally:
        spec.service._release_claim(claim)


def test_pool_cleanup_failure_cannot_leak_account_or_domain_lock(scope, monkeypatch):
    adapter = SimpleNamespace(close=Mock(side_effect=RuntimeError('synthetic cleanup failure')))
    monkeypatch.setattr(scheduler._domain('analytics').service, 'sync_account', Mock(side_effect=completed))
    assert run(adapter_factory=lambda _: adapter)['completed'] == 1
    account_claim = try_account_operation_lock(scope.ids[0])
    domain_claim = _try_operation_lock('ozon-read-analytics', scope.ids[0])
    assert account_claim is not None and domain_claim is not None
    account_claim.close()
    domain_claim.close()


def _empty_response(endpoint, payload):
    if endpoint == 'analytics_data':
        return {'result': {'data': [], 'totals': [0, 0]}}
    if endpoint in ('posting_fbs_list', 'posting_fbo_list'):
        return {'postings': [], 'has_next': False, 'cursor': ''}
    if endpoint == 'returns_list':
        return {'returns': [], 'has_next': False}
    if endpoint == 'returns_rfbs_list':
        return {'returns': [], 'last_id': 0}
    if endpoint == 'conditional_cancellation_list':
        return {'result': [], 'last_id': 0}
    if endpoint == 'finance_accrual_types':
        return {'accrual_types': []}
    if endpoint == 'finance_accrual_by_day':
        return {'accruals': [], 'last_id': None}
    raise AssertionError(endpoint)


@pytest.mark.parametrize('domain', DOMAINS)
def test_real_service_resumes_committed_page_after_429_and_midnight(scope, monkeypatch, domain):
    calls = []

    def read(client, endpoint, payload):
        assert not db.session.connection().connection.driver_connection.in_transaction
        calls.append((endpoint, payload))
        if len(calls) == 2:
            raise OzonRateLimitError('synthetic', status_code=429, retry_after=5000, retriable=True)
        return _empty_response(endpoint, payload)

    monkeypatch.setattr(OzonSellerAPIClient, 'request', read)
    spec = scheduler._domain(domain)
    assert run(domain)['failed'] == 1
    snapshot = spec.model.query.one()
    assert snapshot.status == 'running'
    assert snapshot.last_page_at == NOW
    initial_id, initial_phase = snapshot.id, snapshot.phase
    assert initial_phase not in ('product', 'types', 'fbs_postings')
    attempted_page = calls[1]
    row = MarketplaceReadSchedule.query.one()
    due = row.next_due_at
    assert due.date() > NOW.date()
    db.session.remove()
    assert run(domain, now=due)['selected'] == 1
    assert calls[2] == attempted_page
    assert spec.model.query.count() == 1
    snapshot = db.session.get(spec.model, initial_id)
    assert snapshot.status in ('running', 'completed')
    assert snapshot.period_end == NOW.date()
    assert snapshot.error_code is None
    assert snapshot.error_message is None


@pytest.mark.parametrize('domain', DOMAINS)
def test_malformed_read_terminates_attempt_instead_of_retrying_same_cursor(scope, monkeypatch, domain):
    monkeypatch.setattr(OzonSellerAPIClient, 'request', lambda *args: {'malformed': True})
    assert run(domain)['failed'] == 1
    assert scheduler._domain(domain).model.query.one().status == 'failed'
    assert MarketplaceReadSchedule.query.one().next_due_at >= NOW + timedelta(minutes=10)


def test_read_client_limits_physical_calls_deadline_and_rejects_writes(monkeypatch):
    credentials = MarketplaceCredentials(external_account_id='synthetic', api_key='synthetic')
    request = Mock(return_value={})
    monkeypatch.setattr(OzonSellerAPIClient, 'request', request)
    client = scheduler._ReadClient(credentials)
    try:
        assert client.timeout == (3.0, 6.0)
        assert client.read_retries == 0
        with pytest.raises(ValueError):
            client.request('product_import', {})
        for _ in range(scheduler.MAX_CALLS_PER_ACCOUNT):
            client.request('analytics_data', {})
        with pytest.raises(OzonAPIError, match='Read budget'):
            client.request('analytics_data', {})
        assert request.call_count == scheduler.MAX_CALLS_PER_ACCOUNT
        client.calls = 0
        monkeypatch.setattr(scheduler.time, 'monotonic', lambda: client.deadline)
        with pytest.raises(OzonAPIError, match='Read budget'):
            client.request('analytics_data', {})
        assert request.call_count == scheduler.MAX_CALLS_PER_ACCOUNT
    finally:
        client.session.close()
