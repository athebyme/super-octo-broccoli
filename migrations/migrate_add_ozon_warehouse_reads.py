#!/usr/bin/env python3
"""Add private durable Ozon warehouse/FBS read jobs and bounded staging."""

import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety
else:
    from _foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety


JOBS = 'marketplace_warehouse_read_jobs'
ITEMS = 'marketplace_warehouse_read_items'
ACTIVE = "status IN ('queued','running','waiting_provider')"
REQUIRED = {
    JOBS: {'id', 'seller_id', 'marketplace_id', 'account_id', 'kind', 'listing_id',
           'external_account_id', 'offer_id', 'external_product_id', 'credential_fingerprint',
           'status', 'next_due_at', 'cooldown_until', 'lease_token', 'lease_expires_at',
           'last_attempt_at', 'failure_count', 'page_count', 'next_cursor',
           'seen_cursor_hashes_json', 'staged_count', 'staged_bytes', 'error_code',
           'warehouse_sync_id', 'last_completed_at', 'requested_at', 'completed_at',
           'created_at', 'updated_at'},
    ITEMS: {'id', 'job_id', 'external_warehouse_id', 'item_kind', 'normalized_json',
            'normalized_bytes', 'fingerprint', 'observed_at'},
}
CHECKS = {
    JOBS: [
        "kind IN ('warehouses','fbs_stock')",
        "(kind = 'warehouses' AND listing_id IS NULL AND offer_id IS NULL AND external_product_id IS NULL) OR (kind = 'fbs_stock' AND listing_id IS NOT NULL AND offer_id IS NOT NULL AND external_product_id IS NOT NULL)",
        "status IN ('queued','running','waiting_provider','waiting_access','completed','failed','cancelled')",
        'failure_count >= 0 AND page_count >= 0 AND staged_count >= 0 AND staged_bytes >= 0',
    ],
    ITEMS: ["item_kind IN ('warehouses','fbs_stock') AND normalized_bytes >= 0"],
}
FOREIGN_KEYS = {
    JOBS: {'seller_id': ('sellers', 'CASCADE'), 'marketplace_id': ('marketplaces', 'NO ACTION'),
           'account_id': ('seller_marketplace_accounts', 'CASCADE'),
           'listing_id': ('marketplace_listings', 'CASCADE'),
           'warehouse_sync_id': ('marketplace_warehouse_syncs', 'SET NULL')},
    ITEMS: {'job_id': (JOBS, 'CASCADE')},
}
INDEXES = {
    'uq_marketplace_warehouse_read_active_account': (JOBS, ('account_id', 'kind'), True,
        "kind = 'warehouses' AND " + ACTIVE),
    'uq_marketplace_warehouse_read_active_listing': (JOBS, ('account_id', 'listing_id', 'kind'), True,
        "kind = 'fbs_stock' AND " + ACTIVE),
    'idx_marketplace_warehouse_read_due': (JOBS, ('status', 'next_due_at', 'last_attempt_at', 'id'), False, None),
    'idx_marketplace_warehouse_read_scope': (JOBS, ('seller_id', 'account_id', 'kind', 'listing_id', 'id'), False, None),
    'uq_marketplace_warehouse_read_item': (ITEMS, ('job_id', 'external_warehouse_id'), True, None),
    'idx_marketplace_warehouse_read_item_job': (ITEMS, ('job_id', 'id'), False, None),
}


def _normalize(text):
    return ''.join(str(text or '').lower().split()).replace('"', '')


def _table_sql(connection, name):
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row[0] if row else ''


def _validate(connection):
    for name, required in REQUIRED.items():
        info = {row[1]: row for row in connection.execute(f'PRAGMA table_info({name})')}
        if not required <= info.keys() or not info['id'][5]:
            raise sqlite3.OperationalError('Warehouse read migration columns incompatible: ' + name)
        ddl = _normalize(_table_sql(connection, name))
        if any('check(' + _normalize(check) + ')' not in ddl for check in CHECKS[name]):
            raise sqlite3.OperationalError('Warehouse read migration CHECK incompatible: ' + name)
        fks = {(r[3], r[2], r[4], r[6]) for r in connection.execute(f'PRAGMA foreign_key_list({name})')}
        if not {(field, target, 'id', action) for field, (target, action) in FOREIGN_KEYS[name].items()} <= fks:
            raise sqlite3.OperationalError('Warehouse read migration FK incompatible: ' + name)
    for name, (table, columns, unique, where) in INDEXES.items():
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        if row is None:
            raise sqlite3.OperationalError('Warehouse read migration index missing: ' + name)
        indexes = {r[1]: r for r in connection.execute(f'PRAGMA index_list({table})')}
        if name not in indexes or bool(indexes[name][2]) != unique:
            raise sqlite3.OperationalError('Warehouse read migration index incompatible: ' + name)
        actual = tuple(r[2] for r in connection.execute(f'PRAGMA index_info({name})'))
        if actual != columns or (where is not None and _normalize(where) not in _normalize(row[0])):
            raise sqlite3.OperationalError('Warehouse read migration index scope incompatible: ' + name)


def apply_migration(connection, *, verbose=True):
    for prerequisite in ('sellers', 'marketplaces', 'seller_marketplace_accounts',
                         'marketplace_listings', 'marketplace_warehouse_syncs'):
        if 'id' not in {r[1] for r in connection.execute(f'PRAGMA table_info({prerequisite})')}:
            raise sqlite3.OperationalError('Warehouse read prerequisite missing: ' + prerequisite)
    baseline = foreign_key_snapshot(connection)
    before = set(connection.execute('SELECT type,name FROM sqlite_master'))
    connection.execute('SAVEPOINT ozon_warehouse_read_ddl')
    try:
        connection.execute('''CREATE TABLE IF NOT EXISTS marketplace_warehouse_read_jobs (
            id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            kind VARCHAR(20) NOT NULL,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE CASCADE,
            external_account_id VARCHAR(200) NOT NULL,
            offer_id VARCHAR(200), external_product_id VARCHAR(100),
            credential_fingerprint VARCHAR(64) NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'queued',
            next_due_at DATETIME NOT NULL, cooldown_until DATETIME,
            lease_token VARCHAR(64), lease_expires_at DATETIME, last_attempt_at DATETIME,
            failure_count INTEGER NOT NULL DEFAULT 0, page_count INTEGER NOT NULL DEFAULT 0,
            next_cursor TEXT, seen_cursor_hashes_json TEXT NOT NULL DEFAULT '[]',
            staged_count INTEGER NOT NULL DEFAULT 0, staged_bytes INTEGER NOT NULL DEFAULT 0,
            error_code VARCHAR(100),
            warehouse_sync_id INTEGER REFERENCES marketplace_warehouse_syncs(id) ON DELETE SET NULL,
            last_completed_at DATETIME, requested_at DATETIME NOT NULL,
            completed_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            CONSTRAINT ck_marketplace_warehouse_read_kind CHECK (kind IN ('warehouses','fbs_stock')),
            CONSTRAINT ck_marketplace_warehouse_read_scope CHECK (
                (kind = 'warehouses' AND listing_id IS NULL AND offer_id IS NULL AND external_product_id IS NULL)
                OR (kind = 'fbs_stock' AND listing_id IS NOT NULL AND offer_id IS NOT NULL AND external_product_id IS NOT NULL)
            ),
            CONSTRAINT ck_marketplace_warehouse_read_status CHECK (
                status IN ('queued','running','waiting_provider','waiting_access','completed','failed','cancelled')
            ),
            CONSTRAINT ck_marketplace_warehouse_read_counts CHECK (
                failure_count >= 0 AND page_count >= 0 AND staged_count >= 0 AND staged_bytes >= 0
            )
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS marketplace_warehouse_read_items (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES marketplace_warehouse_read_jobs(id) ON DELETE CASCADE,
            external_warehouse_id VARCHAR(100) NOT NULL,
            item_kind VARCHAR(20) NOT NULL,
            normalized_json TEXT NOT NULL,
            normalized_bytes INTEGER NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            observed_at DATETIME NOT NULL,
            CONSTRAINT ck_marketplace_warehouse_read_item CHECK (
                item_kind IN ('warehouses','fbs_stock') AND normalized_bytes >= 0
            )
        )''')
        for name, (table, columns, unique, where) in INDEXES.items():
            statement = 'CREATE ' + ('UNIQUE ' if unique else '') + f'INDEX IF NOT EXISTS {name} ON {table} (' + ','.join(columns) + ')'
            if where is not None:
                statement += ' WHERE ' + where
            connection.execute(statement)
        _validate(connection)
        assert_foreign_key_safety(connection, baseline=baseline, managed_tables={JOBS, ITEMS},
                                  label='Ozon warehouse read migration')
        connection.execute('RELEASE SAVEPOINT ozon_warehouse_read_ddl')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT ozon_warehouse_read_ddl')
        connection.execute('RELEASE SAVEPOINT ozon_warehouse_read_ddl')
        raise
    if verbose:
        print('Ozon warehouse read migration completed successfully')
    return len(set(connection.execute('SELECT type,name FROM sqlite_master')) - before)


def migrate(db_path):
    if not os.path.isfile(db_path):
        raise sqlite3.OperationalError('Warehouse read database does not exist')
    connection = sqlite3.connect(db_path)
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN IMMEDIATE')
        apply_migration(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    migrate(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DATABASE_PATH', 'data/seller_platform.db'))
