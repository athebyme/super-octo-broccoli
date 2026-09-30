#!/usr/bin/env python3
"""Add durable Ozon read scheduling without scanning or changing snapshots."""

import os
import sqlite3
import sys


def apply_migration(connection: sqlite3.Connection, *, verbose: bool = True) -> int:
    for table, required in {
        'sellers': {'id'},
        'marketplaces': {'id'},
        'seller_marketplace_accounts': {'id', 'seller_id', 'marketplace_id'},
    }.items():
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        if not required <= columns:
            raise sqlite3.OperationalError(f'Read schedule prerequisites missing: {table}')
    before = set(connection.execute("SELECT type,name FROM sqlite_master"))
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_read_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            domain VARCHAR(20) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            next_due_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            cooldown_until DATETIME,
            last_attempt_at DATETIME,
            last_success_at DATETIME,
            last_run_id INTEGER,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error_code VARCHAR(100),
            lease_token VARCHAR(64),
            lease_expires_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_read_schedule UNIQUE (account_id, domain),
            CONSTRAINT ck_marketplace_read_schedule_domain CHECK (
                domain IN ('analytics','fulfillment','finance','reviews','questions')
            ),
            CONSTRAINT ck_marketplace_read_schedule_status CHECK (
                status IN ('pending','running','waiting','idle','failed')
            ),
            CONSTRAINT ck_marketplace_read_schedule_failures CHECK (consecutive_failures >= 0),
            CONSTRAINT ck_marketplace_read_schedule_lease CHECK (
                (lease_token IS NULL) = (lease_expires_at IS NULL)
            )
        )
    ''')
    connection.execute('''CREATE INDEX IF NOT EXISTS idx_marketplace_read_schedule_due
        ON marketplace_read_schedules(domain, next_due_at, id)''')
    connection.execute('''CREATE INDEX IF NOT EXISTS idx_marketplace_read_schedule_scope
        ON marketplace_read_schedules(seller_id, marketplace_id, account_id)''')
    required = {
        'id', 'seller_id', 'marketplace_id', 'account_id', 'domain', 'status',
        'next_due_at', 'cooldown_until', 'last_attempt_at', 'last_success_at',
        'last_run_id', 'consecutive_failures', 'last_error_code', 'lease_token',
        'lease_expires_at', 'created_at', 'updated_at',
    }
    actual = {row[1] for row in connection.execute('PRAGMA table_info(marketplace_read_schedules)')}
    if not required <= actual:
        raise sqlite3.OperationalError('Marketplace read schedule schema is incomplete')
    unique_scopes = []
    for row in connection.execute('PRAGMA index_list(marketplace_read_schedules)'):
        if row[2] and not row[4]:
            # Index names come from SQLite metadata, never a user request.
            name = row[1].replace('"', '""')
            unique_scopes.append(tuple(item[2] for item in connection.execute(f'PRAGMA index_info("{name}")')))
    if ('account_id', 'domain') not in unique_scopes:
        raise sqlite3.OperationalError('Marketplace read schedule unique scope is missing')
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='marketplace_read_schedules'"
    ).fetchone()[0]
    compact_sql = ''.join(table_sql.lower().split()).replace('"', '')
    if not any(value in compact_sql for value in (
        "check(domainin('analytics','fulfillment','finance'))",
        "check(domainin('analytics','fulfillment','finance','reviews','questions'))",
    )):
        raise sqlite3.OperationalError('Read queue domain check is incompatible')
    for clause in (
        "check(statusin('pending','running','waiting','idle','failed'))",
        'check(consecutive_failures>=0)',
        'check((lease_tokenisnull)=(lease_expires_atisnull))',
    ):
        if clause not in compact_sql:
            raise sqlite3.OperationalError('Marketplace read schedule checks are incomplete')
    for name, columns in {
        'idx_marketplace_read_schedule_due': ('domain', 'next_due_at', 'id'),
        'idx_marketplace_read_schedule_scope': ('seller_id', 'marketplace_id', 'account_id'),
    }.items():
        actual = tuple(row[2] for row in connection.execute(f'PRAGMA index_info({name})'))
        if actual != columns:
            raise sqlite3.OperationalError('Marketplace read schedule index is incompatible')
    if verbose:
        print('Marketplace read schedules migration completed successfully')
    return len(set(connection.execute("SELECT type,name FROM sqlite_master")) - before)


def migrate(db_path: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        apply_migration(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    migrate(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DATABASE_PATH', 'data/seller_platform.db'))
