"""Atomic legacy upgrade, preserving pending work and SQLite safety invariants."""
import sqlite3
from unittest.mock import patch

import pytest

from migrations import migrate_add_inbox_read_queue as migration
from migrations.migrate_add_marketplace_read_schedules import apply_migration as schedules
from migrations.migrate_add_marketplace_read_requests import apply_migration as requests
from tests.test_marketplace_read_schedules_migration import connection


def legacy(connection):
    schedules(connection, verbose=False)
    requests(connection, verbose=False)
    ddl = list(connection.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE '%marketplace_read_%' AND sql IS NOT NULL ORDER BY type DESC"))
    connection.execute('DROP TABLE marketplace_read_requests')
    connection.execute('DROP TABLE marketplace_read_schedules')
    for kind, name, sql in ddl:
        if kind == 'table':
            sql = sql.replace("'finance','reviews','questions'", "'finance'")
            sql = sql.replace("CHECK ((domain IN ('reviews','questions') AND period_code = '90d') OR\n               (domain IN ('analytics','fulfillment','finance') AND period_code IN ('7d','30d'))),", '')
            sql = sql.replace("CHECK (period_code IN ('7d','30d','90d'))", "CHECK (period_code IN ('7d','30d'))")
        connection.execute(sql)
    connection.execute("""INSERT INTO marketplace_read_schedules
        (id,seller_id,marketplace_id,account_id,domain,status,next_due_at,cooldown_until,
         consecutive_failures,last_error_code,lease_token,lease_expires_at,last_run_id)
        VALUES (27,1,1,1,'finance','waiting','2026-09-27','2026-09-27',3,'provider_rate_limited','claimed','2026-09-26',12)""")
    connection.execute("""INSERT INTO marketplace_read_requests
        (id,seller_id,marketplace_id,account_id,domain,period_code,period_start,period_end,credential_version,run_id,failure_count)
        VALUES (45,1,1,1,'finance','7d','2026-09-20','2026-09-26',2,12,3)""")
    connection.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name='marketplace_read_requests'")
    connection.execute("CREATE INDEX extra_inbox_migration_test ON marketplace_read_requests(failure_count) WHERE failure_count>0")
    connection.commit()


def snapshot(c):
    return {name: c.execute('SELECT * FROM '+name).fetchall() for name in migration.TABLES}


def test_atomic_upgrade_preserves_work_indexes_sequence_and_foreign_keys(connection):
    legacy(connection)
    before = snapshot(connection)
    assert migration.apply_migration(connection, verbose=False) == 2
    assert snapshot(connection) == before
    assert connection.execute('PRAGMA foreign_keys').fetchone() == (1,)
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    assert connection.execute("SELECT seq FROM sqlite_sequence WHERE name='marketplace_read_requests'").fetchone() == (1000,)
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='extra_inbox_migration_test'").fetchone()
    connection.execute("""INSERT INTO marketplace_read_requests
        (seller_id,marketplace_id,account_id,domain,period_code,period_start,period_end,credential_version)
        VALUES (1,1,1,'reviews','90d','2026-06-29','2026-09-26',2)""")
    assert connection.execute("SELECT id FROM marketplace_read_requests WHERE domain='reviews'").fetchone() == (1001,)
    connection.commit()
    assert migration.apply_migration(connection, verbose=False) == 0


@pytest.mark.parametrize('domain,period', [('finance','90d'),('reviews','30d'),('questions','7d'),('unknown','90d')])
def test_new_contract_rejects_crossed_kind_and_period(connection, domain, period):
    legacy(connection)
    migration.apply_migration(connection, verbose=False)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("""INSERT INTO marketplace_read_requests
            (seller_id,marketplace_id,account_id,domain,period_code,period_start,period_end,credential_version)
            VALUES (1,1,1,?,?, '2026-09-20','2026-09-26',2)""", (domain,period))


def test_second_table_failure_rolls_back_first_table_and_restores_fk_mode(connection):
    legacy(connection)
    before, schema = snapshot(connection), connection.execute('SELECT * FROM sqlite_master ORDER BY name').fetchall()
    original = migration._rebuild
    def fail(c, table):
        if table == migration.TABLES[1]:
            raise sqlite3.OperationalError('synthetic interruption')
        return original(c, table)
    with patch.object(migration, '_rebuild', fail), pytest.raises(sqlite3.OperationalError):
        migration.apply_migration(connection, verbose=False)
    assert snapshot(connection) == before
    assert connection.execute('SELECT * FROM sqlite_master ORDER BY name').fetchall() == schema
    assert connection.execute('PRAGMA foreign_keys').fetchone() == (1,)


@pytest.mark.parametrize('ddl', [
    'CREATE VIEW dependency AS SELECT id FROM marketplace_read_requests',
    'CREATE TABLE dependency(id INTEGER REFERENCES marketplace_read_requests(id))',
    'ALTER TABLE marketplace_read_requests ADD COLUMN unexpected TEXT',
    'CREATE TABLE marketplace_read_requests_inbox_upgrade(id INTEGER)',
])
def test_unknown_dependencies_fail_without_mutating_queue(connection, ddl):
    legacy(connection)
    connection.execute(ddl); connection.commit()
    before = snapshot(connection)
    with pytest.raises(sqlite3.OperationalError):
        migration.apply_migration(connection, verbose=False)
    assert snapshot(connection) == before
    assert 'reviews' not in connection.execute("SELECT sql FROM sqlite_master WHERE name='marketplace_read_schedules'").fetchone()[0]


def test_unrelated_legacy_fk_violation_preserved_but_managed_violation_rejected(connection):
    legacy(connection)
    connection.execute('PRAGMA foreign_keys=OFF')
    connection.executescript('CREATE TABLE old_orphans(parent INTEGER REFERENCES sellers(id)); INSERT INTO old_orphans VALUES(99);')
    before = connection.execute('PRAGMA foreign_key_check').fetchall()
    migration.apply_migration(connection, verbose=False)
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == before
    assert connection.execute('PRAGMA foreign_keys').fetchone() == (0,)
    connection.execute('UPDATE marketplace_read_schedules SET seller_id=99'); connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        migration.apply_migration(connection, verbose=False)


def test_fresh_model_schema_and_missing_prerequisite_or_outer_transaction(connection):
    with pytest.raises(sqlite3.OperationalError, match='prerequisite'):
        migration.apply_migration(connection, verbose=False)
    schedules(connection, verbose=False); requests(connection, verbose=False); connection.commit()
    assert migration.apply_migration(connection, verbose=False) == 0
    connection.execute('BEGIN')
    with pytest.raises(sqlite3.OperationalError, match='own transaction'):
        migration.apply_migration(connection, verbose=False)
    connection.rollback()
