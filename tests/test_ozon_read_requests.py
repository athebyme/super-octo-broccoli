from datetime import timedelta
from unittest.mock import Mock
from types import SimpleNamespace
from flask import current_app
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

import pytest

from models import MarketplaceReadRequest, MarketplaceReadSchedule, SellerMarketplaceAccount, db
from services import ozon_read_scheduler as scheduler
from services.ozon_read_requests import enqueue_read, read_status
from services.ozon_api_client import OzonSellerAPIClient, OzonRateLimitError
from tests.test_ozon_read_scheduler import scope, NOW, DOMAINS, _empty_response


def enqueue(scope, domain='analytics', period='7d', **kwargs):
    return enqueue_read(seller_id=scope.seller_id, account_id=scope.ids[0], domain=domain,
                        period_code=period, force=True, now=kwargs.get('now', NOW))


def status(scope, domain='analytics', period='7d', **kwargs):
    return read_status(seller_id=scope.seller_id, account_id=scope.ids[0], domain=domain,
                       period_code=period, now=kwargs.get('now', NOW))


@pytest.mark.parametrize('domain', DOMAINS)
def test_enqueue_deduplicates_two_tabs_and_status_has_no_provider_io(scope, domain):
    first = enqueue(scope, domain)
    assert first['status'] == 'pending' and first['active']
    assert first['period_start'] == (NOW.date() - timedelta(days=6)).isoformat()
    assert first['period_end'] == NOW.date().isoformat()
    assert enqueue(scope, domain)['id'] == first['id']
    other = enqueue(scope, domain, '30d')
    assert other['id'] != first['id']
    assert MarketplaceReadRequest.query.count() == 2
    assert MarketplaceReadSchedule.query.count() == 1
    before = db.session.get(MarketplaceReadRequest, first['id']).updated_at
    assert status(scope, domain)['id'] == first['id']
    assert db.session.get(MarketplaceReadRequest, first['id']).updated_at == before
    OzonSellerAPIClient.request.assert_not_called()
    assert not {'credential_version', 'lease_token', 'api_key', 'cursor'} & first.keys()


@pytest.mark.parametrize('domain', DOMAINS)
def test_exact_seven_day_request_finishes_in_background_after_restart(scope, monkeypatch, domain):
    calls = []
    def read(client, endpoint, payload):
        assert not db.session.connection().connection.driver_connection.in_transaction
        calls.append((endpoint, payload))
        return _empty_response(endpoint, payload)
    monkeypatch.setattr(OzonSellerAPIClient, 'request', read)
    request = enqueue(scope, domain)
    for _ in range(8):
        db.session.remove()
        schedule = MarketplaceReadSchedule.query.one()
        scheduler.run_requested_reads(now=schedule.next_due_at)
        state = status(scope, domain)
        if state['status'] == 'completed':
            break
    assert state['id'] == request['id'] and state['status'] == 'completed'
    snapshot = db.session.get(scheduler._domain(domain).model, state['snapshot_id'])
    assert snapshot.period_code == '7d'
    assert snapshot.period_start == NOW.date() - timedelta(days=6)
    assert snapshot.period_end == NOW.date()
    assert snapshot.status == 'completed'
    assert calls


@pytest.mark.parametrize('domain', DOMAINS)
def test_manual_clicks_never_bypass_provider_cooldown(scope, monkeypatch, domain):
    read = Mock(side_effect=OzonRateLimitError('synthetic', status_code=429, retry_after=5000, retriable=True))
    monkeypatch.setattr(OzonSellerAPIClient, 'request', read)
    request = enqueue(scope, domain)
    assert scheduler.run_requested_reads(now=NOW)['failed'] == 1
    due = MarketplaceReadSchedule.query.one().next_due_at
    assert due >= NOW + timedelta(seconds=5000)
    duplicate = enqueue(scope, domain, now=NOW + timedelta(seconds=1))
    assert duplicate['id'] == request['id'] and duplicate['status'] == 'waiting'
    enqueue(scope, domain, '30d', now=NOW + timedelta(seconds=2))
    assert MarketplaceReadSchedule.query.one().next_due_at == due
    assert scheduler.run_requested_reads(now=NOW + timedelta(seconds=4999))['selected'] == 0
    read.assert_called_once()


def test_manual_request_arriving_during_automatic_read_is_not_lost(scope, monkeypatch):
    queued = []
    def read(client, endpoint, payload):
        if not queued:
            queued.append(enqueue(scope))
        return _empty_response(endpoint, payload)
    monkeypatch.setattr(OzonSellerAPIClient, 'request', read)
    assert scheduler.run_due_reads(domain='analytics', limit=1, now=NOW)['completed'] == 1
    schedule = MarketplaceReadSchedule.query.one()
    # The next tick is bounded from completion, not the start of provider I/O.
    # CPU contention must not turn this correctness check into a 1s benchmark.
    assert schedule.updated_at <= schedule.next_due_at <= schedule.updated_at + timedelta(seconds=60)
    assert status(scope)['status'] == 'pending'
    assert scheduler.run_requested_reads(now=schedule.next_due_at)['completed'] == 1
    assert status(scope)['status'] == 'completed'
    snapshots = scheduler._domain('analytics').model.query.all()
    assert {row.period_code for row in snapshots} == {'7d', '30d'}


def test_new_request_between_completion_and_schedule_finish_is_preserved(scope, monkeypatch):
    monkeypatch.setattr(OzonSellerAPIClient, 'request', lambda _, endpoint, payload: _empty_response(endpoint, payload))
    first = enqueue(scope)
    original = scheduler._success
    next_request = []
    def intercept(*args, **kwargs):
        next_request.append(enqueue(scope, now=NOW + timedelta(seconds=1)))
        return original(*args, **kwargs)
    monkeypatch.setattr(scheduler, '_success', intercept)
    assert scheduler.run_requested_reads(now=NOW)['completed'] == 1
    assert next_request[0]['id'] != first['id']
    assert db.session.get(MarketplaceReadRequest, first['id']).status == 'completed'
    assert status(scope)['status'] == 'pending'
    schedule = MarketplaceReadSchedule.query.one()
    assert schedule.updated_at <= schedule.next_due_at <= schedule.updated_at + timedelta(seconds=60)


@pytest.mark.parametrize('change', ['credentials', 'expired'])
def test_obsolete_request_does_not_call_provider(scope, change):
    enqueue(scope)
    if change == 'credentials':
        account = db.session.get(SellerMarketplaceAccount, scope.ids[0])
        account.credential_version += 1
        db.session.commit()
        now = NOW
    else:
        now = NOW + timedelta(days=2)
    assert status(scope, now=now)['status'] == 'paused'
    assert scheduler.run_requested_reads(now=now)['selected'] == 0
    assert MarketplaceReadRequest.query.one().status == 'failed'
    OzonSellerAPIClient.request.assert_not_called()


def test_malformed_response_fails_request_without_replacing_snapshot(scope, monkeypatch):
    monkeypatch.setattr(OzonSellerAPIClient, 'request', lambda *_: {'malformed': True})
    enqueue(scope)
    assert scheduler.run_requested_reads(now=NOW)['failed'] == 1
    state = status(scope)
    assert state['status'] == 'failed' and not state['active']
    assert state['snapshot_id'] is None


def test_duplicate_pending_click_cannot_accelerate_next_page(scope, monkeypatch):
    monkeypatch.setattr(OzonSellerAPIClient, 'request', lambda _, endpoint, payload: _empty_response(endpoint, payload))
    first = enqueue(scope, 'finance')
    scheduler.run_requested_reads(now=NOW)
    due = MarketplaceReadSchedule.query.one().next_due_at
    assert due >= NOW + timedelta(seconds=60)
    assert enqueue(scope, 'finance', now=NOW + timedelta(seconds=1))['id'] == first['id']
    assert MarketplaceReadSchedule.query.one().next_due_at == due


def test_failure_limit_stops_manual_polling_and_preserves_cooldown(scope, monkeypatch):
    monkeypatch.setattr(OzonSellerAPIClient, 'request', Mock(side_effect=OzonRateLimitError(
        'synthetic', status_code=429, retry_after=5000, retriable=True)))
    enqueue(scope)
    item = MarketplaceReadRequest.query.one()
    item.failure_count = 7
    db.session.commit()
    scheduler.run_requested_reads(now=NOW)
    state = status(scope)
    assert state['status'] == 'failed' and state['error_code'] == 'retry_exhausted'
    assert MarketplaceReadSchedule.query.one().cooldown_until >= NOW + timedelta(seconds=5000)


def test_read_request_history_is_bounded(scope):
    for _ in range(23):
        result = enqueue(scope)
        db.session.get(MarketplaceReadRequest, result['id']).status = 'completed'
        db.session.commit()
    enqueue(scope)
    assert MarketplaceReadRequest.query.filter_by(status='completed').count() == 20


@pytest.mark.parametrize('domain', DOMAINS)
def test_real_http_enqueue_status_authorization_and_csrf(scope, monkeypatch, domain):
    from models import Seller, User
    from routes.marketplace_insights import register_marketplace_insight_routes
    from routes.marketplace_finance import register_marketplace_finance_routes
    from routes.marketplace_fulfillment import register_marketplace_fulfillment_routes
    app = current_app._get_current_object()
    app.config.update(SECRET_KEY='synthetic', MARKETPLACE_OZON_ENABLED=True, WTF_CSRF_ENABLED=False)
    LoginManager(app)
    CSRFProtect(app)
    {'analytics': register_marketplace_insight_routes, 'finance': register_marketplace_finance_routes,
     'fulfillment': register_marketplace_fulfillment_routes}[domain](app)
    user = SimpleNamespace(id=1, is_authenticated=True, seller=SimpleNamespace(id=scope.seller_id))
    module = 'marketplace_insights' if domain == 'analytics' else 'marketplace_' + domain
    monkeypatch.setattr('routes.' + module + '.current_user', user)
    monkeypatch.setattr('flask_login.utils._get_user', lambda: user)
    foreign = Seller(company_name='Foreign', user=User(username='foreign', email='foreign@test.local', password_hash='synthetic'))
    db.session.add(foreign)
    db.session.commit()
    other = scope.add(seller_id=foreign.id)
    client = app.test_client()
    base = '/marketplaces/api/' + domain + '/sync'
    uri = base + '?account_id=' + str(scope.ids[0])
    assert client.get(uri + '&period=7d').get_json()['data']['status'] == 'idle'
    assert MarketplaceReadRequest.query.count() == 0
    response = client.post(uri, json={'period': '7d', 'force': True})
    assert response.status_code == 202
    request_id = response.get_json()['data']['id']
    assert client.post(uri, json={'period': '7d', 'force': True}).get_json()['data']['id'] == request_id
    assert client.get(uri + '&period=7d').get_json()['data']['id'] == request_id
    assert client.get(base + '?account_id=' + str(other)).status_code == 404
    assert client.post(base + '?account_id=' + str(other), json={'period': '7d'}).status_code == 404
    assert client.post(uri, json={'period': '7d', 'account_id': other}).status_code == 400
    assert client.get(uri + '&period=90d').status_code == 400
    app.config['WTF_CSRF_ENABLED'] = True
    assert client.post(uri, json={'period': '7d'}).status_code == 400
    app.config['MARKETPLACE_OZON_ENABLED'] = False
    assert client.get(uri).status_code == 404
    OzonSellerAPIClient.request.assert_not_called()
