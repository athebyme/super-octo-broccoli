"""Real key rotation invalidates manual intent without erasing read history."""
from datetime import timedelta
import sqlite3

import pytest

from models import MarketplaceReadRequest, MarketplaceReadSchedule, SellerMarketplaceAccount, db
from services.marketplace_accounts import MarketplaceAccountService
from services.marketplace_credential_identity import ozon_credential_fingerprint
from services.ozon_read_requests import enqueue_read, read_status
from services import ozon_read_scheduler as scheduler
from services.ozon_api_client import OzonSellerAPIClient
from migrations.migrate_add_marketplace_read_credential_identity import apply_migration
from migrations.migrate_add_marketplace_read_requests import apply_migration as add_requests
from migrations.migrate_add_marketplace_read_schedules import apply_migration as add_schedule
from tests.test_marketplace_read_schedules_migration import connection
from tests.test_ozon_read_scheduler import scope, NOW, _empty_response


def _enqueue(scope):
    return enqueue_read(seller_id=scope.seller_id, account_id=scope.ids[0],
                        domain='analytics', period_code='7d', force=True, now=NOW)


def _status(scope):
    return read_status(seller_id=scope.seller_id, account_id=scope.ids[0],
                       domain='analytics', period_code='7d', now=NOW)


def test_real_same_client_key_rotation_stops_old_intent_with_unchanged_format(scope):
    queued = _enqueue(scope)
    account = db.session.get(SellerMarketplaceAccount, scope.ids[0])
    original_format = account.credential_version
    original_marker = ozon_credential_fingerprint(account)
    MarketplaceAccountService.rotate_ozon_key(
        seller_id=scope.seller_id, account_id=account.id,
        external_account_id=account.external_account_id, api_key='synthetic-replacement',
        expected_version=account.version,
    )
    assert account.credential_version == original_format
    assert ozon_credential_fingerprint(account) != original_marker
    # Emulate the separately completed roles check; no provider check in this test.
    account.connection_status = 'connected'
    db.session.commit()
    assert _status(scope)['error_code'] == 'credentials_changed'
    assert scheduler.run_requested_reads(now=NOW)['selected'] == 0
    old = db.session.get(MarketplaceReadRequest, queued['id'])
    assert old.status == 'failed' and old.error_code == 'credentials_changed'
    OzonSellerAPIClient.request.assert_not_called()
    replacement = _enqueue(scope)
    assert replacement['id'] != queued['id'] and replacement['active']
    assert 'credential_fingerprint' not in replacement


def test_settings_revision_does_not_cancel_same_key_intent(scope, monkeypatch):
    queued = _enqueue(scope)
    account = db.session.get(SellerMarketplaceAccount, scope.ids[0])
    before = ozon_credential_fingerprint(account)
    account.label = 'Renamed synthetic cabinet'
    account.version += 1
    db.session.commit()
    assert ozon_credential_fingerprint(account) == before
    monkeypatch.setattr(OzonSellerAPIClient, 'request',
                        lambda _, endpoint, payload: _empty_response(endpoint, payload))
    assert scheduler.run_requested_reads(now=NOW)['completed'] == 1
    assert _status(scope)['id'] == queued['id']
    assert _status(scope)['status'] == 'completed'


def test_legacy_intent_is_not_stamped_with_current_key_and_new_click_keeps_cooldown(scope):
    first = _enqueue(scope)
    item = db.session.get(MarketplaceReadRequest, first['id'])
    item.credential_fingerprint = None
    schedule = MarketplaceReadSchedule.query.one()
    due = NOW + timedelta(hours=3)
    schedule.next_due_at = schedule.cooldown_until = due
    schedule.last_error_code = 'provider_rate_limited'
    db.session.commit()
    assert _status(scope)['error_code'] == 'credentials_unverified'
    assert not _status(scope)['active']
    replacement = _enqueue(scope)
    assert replacement['id'] != first['id']
    db.session.refresh(item)
    db.session.refresh(schedule)
    assert item.status == 'failed' and item.credential_fingerprint is None
    assert item.error_code == 'credentials_unverified'
    assert schedule.next_due_at == schedule.cooldown_until == due
    assert scheduler.run_requested_reads(now=NOW)['selected'] == 0
    OzonSellerAPIClient.request.assert_not_called()


def test_identity_migration_is_additive_and_never_backfills_legacy_intents(connection):
    add_schedule(connection, verbose=False)
    add_requests(connection, verbose=False)
    connection.execute('''INSERT INTO marketplace_read_requests
        (seller_id,marketplace_id,account_id,domain,period_code,period_start,period_end,credential_version)
        VALUES (1,1,1,'analytics','7d','2026-09-18','2026-09-24',1)''')
    columns = [row[1] for row in connection.execute('PRAGMA table_info(marketplace_read_requests)')]
    before = connection.execute('SELECT * FROM marketplace_read_requests').fetchall()
    assert apply_migration(connection, verbose=False) == 1
    assert connection.execute('SELECT '+','.join(columns)+' FROM marketplace_read_requests').fetchall() == before
    assert connection.execute('SELECT credential_fingerprint FROM marketplace_read_requests').fetchall() == [(None,)]
    assert apply_migration(connection, verbose=False) == 0
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_identity_migration_accepts_current_model_schema(connection):
    from sqlalchemy.dialects.sqlite import dialect
    from sqlalchemy.schema import CreateTable
    connection.execute(str(CreateTable(MarketplaceReadRequest.__table__).compile(dialect=dialect())))
    assert apply_migration(connection, verbose=False) == 0


def test_identity_migration_rejects_unknown_column_shape(connection):
    add_schedule(connection, verbose=False)
    add_requests(connection, verbose=False)
    connection.execute('ALTER TABLE marketplace_read_requests ADD COLUMN credential_fingerprint INTEGER')
    with pytest.raises(sqlite3.OperationalError, match='incompatible'):
        apply_migration(connection, verbose=False)
