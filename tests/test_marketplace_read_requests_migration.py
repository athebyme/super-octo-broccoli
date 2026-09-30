import sqlite3

import pytest

from migrations.migrate_add_marketplace_read_requests import apply_migration
from migrations.migrate_add_marketplace_read_schedules import apply_migration as add_schedule
from tests.test_marketplace_read_schedules_migration import connection


def test_requests_migration_preserves_queue_and_unique_active_period(connection):
    add_schedule(connection, verbose=False)
    assert apply_migration(connection, verbose=False) > 0
    values = (1, 1, 1, 'analytics', '7d', '2026-09-18', '2026-09-24', 1)
    sql = '''INSERT INTO marketplace_read_requests
        (seller_id,marketplace_id,account_id,domain,period_code,period_start,period_end,credential_version)
        VALUES (?,?,?,?,?,?,?,?)'''
    connection.execute(sql, values)
    before = connection.execute('SELECT * FROM marketplace_read_requests').fetchall()
    assert apply_migration(connection, verbose=False) == 0
    assert connection.execute('SELECT * FROM marketplace_read_requests').fetchall() == before
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, values)
    connection.execute('UPDATE marketplace_read_requests SET status="completed"')
    connection.execute(sql, values)
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_requests_model_schema_and_additive_migration_agree(connection):
    from sqlalchemy.dialects.sqlite import dialect
    from sqlalchemy.schema import CreateTable
    from models import MarketplaceReadRequest
    add_schedule(connection, verbose=False)
    connection.execute(str(CreateTable(MarketplaceReadRequest.__table__).compile(dialect=dialect())))
    assert apply_migration(connection, verbose=False) == 2
    assert apply_migration(connection, verbose=False) == 0


def test_requests_partial_schema_fails_without_deleting_rows(connection):
    add_schedule(connection, verbose=False)
    connection.executescript('CREATE TABLE marketplace_read_requests(id INTEGER PRIMARY KEY);'
                             'INSERT INTO marketplace_read_requests VALUES(7);')
    with pytest.raises(sqlite3.OperationalError):
        apply_migration(connection, verbose=False)
    assert connection.execute('SELECT * FROM marketplace_read_requests').fetchall() == [(7,)]
