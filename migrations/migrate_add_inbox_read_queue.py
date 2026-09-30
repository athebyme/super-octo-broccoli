#!/usr/bin/env python3
"""Extend read-queue CHECK contracts atomically; preserve IDs, leases and cooldowns.

Standalone connection/transaction owner. SQLite's create/copy/drop/rename
procedure is used, never writable_schema or a rename of the original table.
"""
import os
import re
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety
    from .migrate_add_marketplace_read_schedules import apply_migration as verify_schedules
    from .migrate_add_marketplace_read_requests import apply_migration as verify_requests
else:
    from _foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety
    from migrate_add_marketplace_read_schedules import apply_migration as verify_schedules
    from migrate_add_marketplace_read_requests import apply_migration as verify_requests

TABLES = ('marketplace_read_schedules', 'marketplace_read_requests')
MAX_ROWS = 100_000
DOMAINS = "domain IN ('analytics','fulfillment','finance','reviews','questions')"
PERIODS = "period_code IN ('7d','30d','90d')"
PAIR = "(domain IN ('reviews','questions') AND period_code = '90d') OR (domain IN ('analytics','fulfillment','finance') AND period_code IN ('7d','30d'))"
COLUMNS = {
    TABLES[0]: {'id','seller_id','marketplace_id','account_id','domain','status','next_due_at','cooldown_until','last_attempt_at','last_success_at','last_run_id','consecutive_failures','last_error_code','lease_token','lease_expires_at','created_at','updated_at'},
    TABLES[1]: {'id','seller_id','marketplace_id','account_id','domain','period_code','period_start','period_end','force','credential_version','status','run_id','failure_count','error_code','requested_at','updated_at','completed_at'},
}


def _compact(sql):
    return ''.join(sql.lower().split()).replace('"', '')


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def _sql(connection, table):
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if row is None:
        raise sqlite3.OperationalError('Read queue prerequisite missing: '+table)
    return row[0]


def _current(table, sql):
    return _compact('CHECK ('+DOMAINS+')') in _compact(sql) and (
        table == TABLES[0] or (_compact('CHECK ('+PERIODS+')') in _compact(sql)
                              and _compact('CHECK ('+PAIR+')') in _compact(sql)))


def _preflight(connection, table):
    columns = list(connection.execute(f'PRAGMA table_xinfo({_quote(table)})'))
    if {row[1] for row in columns} != COLUMNS[table] or any(row[6] for row in columns):
        raise sqlite3.OperationalError('Unexpected read queue columns; review required: '+table)
    rows = connection.execute(f'SELECT count(*) FROM {_quote(table)}').fetchone()[0]
    if rows > MAX_ROWS:
        raise sqlite3.OperationalError('Read queue exceeds 100000 rows; review migration capacity')
    for kind, name, sql in connection.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('trigger','view')"):
        if re.search(r'\b'+re.escape(table)+r'\b', sql or '', flags=re.I):
            raise sqlite3.OperationalError('Read queue has an unexpected trigger/view dependency')
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        for fk in connection.execute(f'PRAGMA foreign_key_list({_quote(name)})'):
            if fk[2] == table:
                raise sqlite3.OperationalError('Read queue has an unexpected inbound foreign key')
    return [row[1] for row in columns]


def _rebuild(connection, table):
    sql = _sql(connection, table)
    if _current(table, sql):
        return False
    columns = _preflight(connection, table)
    temporary = table + '_inbox_upgrade'
    if connection.execute('SELECT 1 FROM sqlite_master WHERE name=?', (temporary,)).fetchone():
        raise sqlite3.OperationalError('Read queue temporary name is occupied')
    indexes = [row[0] for row in connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL ORDER BY name", (table,))]
    sequence = None
    if 'autoincrement' in sql.lower():
        row = connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone()
        sequence = row[0] if row else 0
    sql = re.sub(r"\bdomain\s+IN\s*\(\s*'analytics'\s*,\s*'fulfillment'\s*,\s*'finance'\s*\)", DOMAINS, sql, flags=re.I)
    if table == TABLES[1]:
        # Only replace the standalone period constraint, never the domain-period pair.
        sql = re.sub(r"CHECK\s*\(\s*period_code\s+IN\s*\(\s*'7d'\s*,\s*'30d'\s*\)\s*\)", 'CHECK ('+PERIODS+')', sql, flags=re.I)
        if _compact('CHECK ('+PAIR+')') not in _compact(sql):
            end = sql.rfind(')')
            sql = sql[:end] + ', CHECK ('+PAIR+')' + sql[end:]
    if not _current(table, sql):
        raise sqlite3.OperationalError('Unrecognized read queue CHECK contract')
    ddl = 'CREATE TABLE '+_quote(temporary)+' ('+sql.split('(', 1)[1]
    connection.execute(ddl)
    names = ','.join(_quote(name) for name in columns)
    connection.execute(f'INSERT INTO {_quote(temporary)} ({names}) SELECT {names} FROM {_quote(table)}')
    # Compare both directions while both copies exist; no Python copy of queue data.
    for left, right in ((table, temporary), (temporary, table)):
        mismatch = connection.execute(f'SELECT {names} FROM {_quote(left)} EXCEPT SELECT {names} FROM {_quote(right)} LIMIT 1').fetchone()
        if mismatch is not None:
            raise sqlite3.IntegrityError('Read queue row changed during migration')
    connection.execute(f'DROP TABLE {_quote(table)}')
    connection.execute(f'ALTER TABLE {_quote(temporary)} RENAME TO {_quote(table)}')
    for index in indexes:
        connection.execute(index)
    if sequence is not None:
        current = connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone()
        if current:
            connection.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?', (sequence, table))
        else:
            connection.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', (table, sequence))
    return True


def apply_migration(connection, *, verbose=True):
    if connection.in_transaction:
        raise sqlite3.OperationalError('Inbox read queue migration requires its own transaction')
    enabled = connection.execute('PRAGMA foreign_keys').fetchone()[0]
    connection.execute('PRAGMA foreign_keys=OFF')
    try:
        connection.execute('BEGIN IMMEDIATE')
        for table in TABLES:
            _sql(connection, table)  # Upstream migrations must already exist.
        baseline = foreign_key_snapshot(connection)
        assert_foreign_key_safety(connection, baseline=baseline, managed_tables=TABLES, label='Inbox read queue')
        # These validators admit exactly the supported legacy or current enums.
        verify_schedules(connection, verbose=False)
        verify_requests(connection, verbose=False)
        changed = sum(_rebuild(connection, table) for table in TABLES)
        verify_schedules(connection, verbose=False)
        verify_requests(connection, verbose=False)
        if not all(_current(table, _sql(connection, table)) for table in TABLES):
            raise sqlite3.OperationalError('Inbox read queue contract is incomplete')
        assert_foreign_key_safety(connection, baseline=baseline, managed_tables=TABLES, label='Inbox read queue')
        connection.commit()
        if verbose:
            print(f'Inbox read queue migration complete; rebuilt={changed}')
        return changed
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute('PRAGMA foreign_keys='+('ON' if enabled else 'OFF'))


def migrate(db_path):
    # mode=rw prevents a mistyped path from creating an empty database.
    from pathlib import Path
    connection = sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=rw', uri=True)
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        apply_migration(connection)
    finally:
        connection.close()


if __name__ == '__main__':
    migrate(sys.argv[1] if len(sys.argv)>1 else os.environ.get('DATABASE_PATH','data/seller_platform.db'))
