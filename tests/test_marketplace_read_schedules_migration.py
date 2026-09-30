import sqlite3

import pytest

from migrations.migrate_add_marketplace_read_schedules import apply_migration


@pytest.fixture
def connection():
    connection = sqlite3.connect(':memory:')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.executescript('''
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE marketplaces (id INTEGER PRIMARY KEY);
        CREATE TABLE seller_marketplace_accounts (
            id INTEGER PRIMARY KEY, seller_id INTEGER, marketplace_id INTEGER
        );
        INSERT INTO sellers VALUES (1);
        INSERT INTO marketplaces VALUES (1);
        INSERT INTO seller_marketplace_accounts VALUES (1,1,1);
    ''')
    yield connection
    connection.close()


def test_additive_migration_preserves_attempt_and_provider_cooldown_on_repeat(connection):
    assert apply_migration(connection, verbose=False) > 0
    connection.execute('''
        INSERT INTO marketplace_read_schedules
        (seller_id, marketplace_id, account_id, domain, status, next_due_at,
         cooldown_until, consecutive_failures, last_error_code)
        VALUES (1,1,1,'finance','waiting','2026-09-25','2026-09-25',3,'provider_rate_limited')
    ''')
    before = connection.execute('SELECT * FROM marketplace_read_schedules').fetchall()
    assert apply_migration(connection, verbose=False) == 0
    assert connection.execute('SELECT * FROM marketplace_read_schedules').fetchall() == before
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute('''INSERT INTO marketplace_read_schedules
            (seller_id,marketplace_id,account_id,domain) VALUES (1,1,1,'finance')''')


@pytest.mark.parametrize('extra_columns,values', [
    ('', "1,1,1,'unknown'"),
    (',status', "1,1,1,'analytics','unknown'"),
    (',consecutive_failures', "1,1,1,'analytics',-1"),
    (',lease_token', "1,1,1,'analytics','incomplete-lease'"),
    ('', "1,1,999,'analytics'"),
])
def test_schema_rejects_invalid_schedule_state(connection, extra_columns, values):
    apply_migration(connection, verbose=False)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f'''INSERT INTO marketplace_read_schedules
            (seller_id,marketplace_id,account_id,domain{extra_columns}) VALUES ({values})''')


def test_incomplete_existing_schema_fails_without_rebuilding_or_dropping_data(connection):
    connection.execute('CREATE TABLE marketplace_read_schedules (id INTEGER PRIMARY KEY)')
    connection.execute('INSERT INTO marketplace_read_schedules VALUES (23)')
    with pytest.raises(sqlite3.OperationalError):
        apply_migration(connection, verbose=False)
    assert connection.execute('SELECT * FROM marketplace_read_schedules').fetchall() == [(23,)]


def test_missing_prerequisites_fail_before_creating_schedule_table():
    with sqlite3.connect(':memory:') as connection:
        with pytest.raises(sqlite3.OperationalError, match='prerequisites'):
            apply_migration(connection, verbose=False)
        assert connection.execute('SELECT name FROM sqlite_master').fetchall() == []


def test_model_created_schema_passes_the_same_migration_checks(connection):
    from sqlalchemy.dialects.sqlite import dialect
    from sqlalchemy.schema import CreateTable
    from models import MarketplaceReadSchedule

    connection.execute(str(CreateTable(MarketplaceReadSchedule.__table__).compile(dialect=dialect())))
    assert apply_migration(connection, verbose=False) == 2  # The two indexes.
    assert apply_migration(connection, verbose=False) == 0
