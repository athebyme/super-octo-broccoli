"""Both durable queues share slots without bypassing cooldowns or busy accounts."""
from datetime import timedelta

from models import MarketplaceReadSchedule, MarketplaceWarehouseReadJob, db
from services import ozon_read_scheduler as scheduler
from services import ozon_warehouse_reads as warehouses
from services.ozon_api_client import OzonSellerAPIClient
from services.ozon_read_requests import enqueue_read
from tests.test_ozon_read_scheduler import scope, NOW, _empty_response


def _period(scope, account_id, at=NOW):
    return enqueue_read(seller_id=scope.seller_id, account_id=account_id,
                        domain='analytics', period_code='7d', force=True, now=at)


def _warehouse(scope, account_id, at=NOW):
    return warehouses.enqueue_refresh(seller_id=scope.seller_id,
                                     account_id=account_id, kind='warehouses', now=at)


def test_period_and_warehouse_same_account_cannot_spend_two_budgets_in_one_tick(scope, monkeypatch):
    calls = []
    def provider(client, endpoint, payload):
        assert not db.session.connection().connection.driver_connection.in_transaction
        calls.append(endpoint)
        if endpoint == 'warehouses':
            return {'warehouses': [], 'cursor': '', 'has_next': False}
        return _empty_response(endpoint, payload)
    monkeypatch.setattr(OzonSellerAPIClient, 'request', provider)
    _period(scope, scope.ids[0])
    warehouse = _warehouse(scope, scope.ids[0])
    assert scheduler.run_requested_reads(limit=2, now=NOW)['selected'] == 1
    assert 'warehouses' not in calls
    assert db.session.get(MarketplaceWarehouseReadJob, warehouse['id']).status == 'queued'
    calls.clear()
    assert scheduler.run_requested_reads(limit=2, now=NOW + timedelta(seconds=10))['completed'] == 1
    assert calls == ['warehouses']


def test_older_warehouse_is_served_before_new_period_request(scope, monkeypatch):
    _warehouse(scope, scope.ids[0], NOW - timedelta(minutes=2))
    other = scope.add()
    _period(scope, other)
    seen = []
    monkeypatch.setattr(warehouses, 'run_read_step', lambda identity, **_: (
        seen.append(('warehouse', identity)) or {'selected': 1, 'outcome': 'running'}))
    monkeypatch.setattr(scheduler, '_advance', lambda row, *_, **__: (
        seen.append(('period', row.account_id)) or 'completed'))
    assert scheduler.run_requested_reads(now=NOW)['selected'] == 1
    assert len(seen) == 1 and seen[0][0] == 'warehouse'


def test_skipped_account_cannot_hide_another_account_in_either_lane(scope, monkeypatch):
    busy = scope.ids[0]
    warehouse = _warehouse(scope, busy, NOW - timedelta(minutes=2))
    _period(scope, busy, NOW - timedelta(minutes=1))
    healthy = scope.add()
    _period(scope, healthy)
    seen = []
    def skip(identity, **_):
        seen.append(('warehouse', identity))
        return {'selected': 0, 'outcome': 'skipped'}
    def advance(row, *_, **__):
        seen.append(('period', row.account_id))
        return 'completed'
    monkeypatch.setattr(warehouses, 'run_read_step', skip)
    monkeypatch.setattr(scheduler, '_advance', advance)
    assert scheduler.run_requested_reads(now=NOW)['completed'] == 1
    assert seen == [('warehouse', warehouse['id']), ('period', healthy)]


def test_more_than_candidate_limit_future_warehouse_jobs_do_not_hide_ready_account(scope, monkeypatch):
    for index in range(scheduler.CANDIDATE_LIMIT + 1):
        account_id = scope.ids[0] if index == 0 else scope.add()
        item = _warehouse(scope, account_id)
        row = db.session.get(MarketplaceWarehouseReadJob, item['id'])
        row.cooldown_until = row.next_due_at = NOW + timedelta(days=3)
        db.session.commit()
    ready = scope.add()
    item = _warehouse(scope, ready)
    seen = []
    monkeypatch.setattr(warehouses, 'run_read_step', lambda identity, **_: (
        seen.append(identity) or {'selected': 1, 'outcome': 'completed'}))
    assert scheduler.run_requested_reads(now=NOW)['completed'] == 1
    assert seen == [item['id']]
    OzonSellerAPIClient.request.assert_not_called()


def test_no_due_warehouse_bypasses_shared_slot_limit_for_periods(scope, monkeypatch):
    warehouse = _warehouse(scope, scope.ids[0], NOW - timedelta(minutes=2))
    other = scope.add()
    _period(scope, other, NOW - timedelta(minutes=1))
    row = db.session.get(MarketplaceWarehouseReadJob, warehouse['id'])
    row.lease_token = 'another-worker'
    row.lease_expires_at = NOW + timedelta(seconds=120)
    db.session.commit()
    seen = []
    monkeypatch.setattr(warehouses, 'run_read_step', lambda *_, **__: (_ for _ in ()).throw(
        AssertionError('Live warehouse lease selected')))
    monkeypatch.setattr(scheduler, '_advance', lambda row, *_, **__: (
        seen.append(row.account_id) or 'running'))
    assert scheduler.run_requested_reads(now=NOW)['running'] == 1
    assert seen == [other]
