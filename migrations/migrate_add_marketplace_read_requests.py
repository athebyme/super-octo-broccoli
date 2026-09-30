#!/usr/bin/env python3
"""Add bounded exact-period manual requests; do not alter existing snapshots."""
import os
import sqlite3
import sys


def apply_migration(connection, *, verbose=True):
    for table, required in {
        'sellers': {'id'}, 'marketplaces': {'id'},
        'seller_marketplace_accounts': {'id', 'seller_id', 'marketplace_id'},
        'marketplace_read_schedules': {'account_id', 'domain', 'next_due_at', 'cooldown_until'},
    }.items():
        if not required <= {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}:
            raise sqlite3.OperationalError(f'Read request prerequisites missing: {table}')
    before = set(connection.execute('SELECT type,name FROM sqlite_master'))
    connection.execute('''CREATE TABLE IF NOT EXISTS marketplace_read_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
        marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
        account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
        domain VARCHAR(20) NOT NULL,
        period_code VARCHAR(3) NOT NULL,
        period_start DATE NOT NULL,
        period_end DATE NOT NULL,
        force BOOLEAN NOT NULL DEFAULT 0,
        credential_version INTEGER NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        run_id INTEGER,
        failure_count INTEGER NOT NULL DEFAULT 0,
        error_code VARCHAR(100),
        requested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at DATETIME,
        CHECK (domain IN ('analytics','fulfillment','finance','reviews','questions')),
        CHECK (period_code IN ('7d','30d','90d')),
        CHECK ((domain IN ('reviews','questions') AND period_code = '90d') OR
               (domain IN ('analytics','fulfillment','finance') AND period_code IN ('7d','30d'))),
        CHECK (status IN ('pending','running','completed','failed')),
        CHECK (period_start <= period_end AND failure_count >= 0)
    )''')
    connection.execute('''CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_read_request_active
        ON marketplace_read_requests(account_id,domain,period_code) WHERE status IN ('pending','running')''')
    connection.execute('''CREATE INDEX IF NOT EXISTS idx_marketplace_read_request_scope
        ON marketplace_read_requests(seller_id,marketplace_id,account_id,domain,id)''')
    expected = {'id', 'seller_id', 'marketplace_id', 'account_id', 'domain', 'period_code',
                'period_start', 'period_end', 'force', 'credential_version', 'status', 'run_id',
                'failure_count', 'error_code', 'requested_at', 'updated_at', 'completed_at'}
    if not expected <= {row[1] for row in connection.execute('PRAGMA table_info(marketplace_read_requests)')}:
        raise sqlite3.OperationalError('Read request schema is incomplete')
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='marketplace_read_requests'"
    ).fetchone()[0]
    compact_table = ''.join(table_sql.lower().split()).replace('"', '')
    if not any(value in compact_table for value in (
        "check(domainin('analytics','fulfillment','finance'))",
        "check(domainin('analytics','fulfillment','finance','reviews','questions'))",
    )):
        raise sqlite3.OperationalError('Read queue domain check is incompatible')
    if not any(value in compact_table for value in (
        "check(period_codein('7d','30d'))", "check(period_codein('7d','30d','90d'))",
    )):
        raise sqlite3.OperationalError('Read queue period check is incompatible')
    for clause in (
        "check(statusin('pending','running','completed','failed'))",
        'check(period_start<=period_endandfailure_count>=0)',
    ):
        if clause not in compact_table:
            raise sqlite3.OperationalError('Read request checks are incomplete')
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='uq_marketplace_read_request_active'").fetchone()[0]
    compact = ''.join(sql.lower().split()).replace('"', '')
    if 'uniqueindex' not in compact or "(account_id,domain,period_code)wherestatusin('pending','running')" not in compact:
        raise sqlite3.OperationalError('Read request active dedup index is incompatible')
    if tuple(row[2] for row in connection.execute('PRAGMA index_info(idx_marketplace_read_request_scope)')) != (
        'seller_id', 'marketplace_id', 'account_id', 'domain', 'id',
    ):
        raise sqlite3.OperationalError('Read request scope index is incompatible')
    if verbose:
        print('Marketplace read requests migration completed successfully')
    return len(set(connection.execute('SELECT type,name FROM sqlite_master')) - before)


def migrate(db_path):
    with sqlite3.connect(db_path) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        apply_migration(connection)


if __name__ == '__main__':
    migrate(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DATABASE_PATH', 'data/seller_platform.db'))
