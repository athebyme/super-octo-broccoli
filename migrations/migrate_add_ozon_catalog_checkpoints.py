#!/usr/bin/env python3
"""Add private, bounded within-page checkpoints for Ozon catalog reads."""

import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety
else:
    from _foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety


CHECKPOINTS = 'marketplace_catalog_page_checkpoints'
ITEMS = 'marketplace_catalog_page_items'
RUNS = 'marketplace_catalog_syncs'


def _columns(connection, table):
    return {row[1]: row for row in connection.execute(f'PRAGMA table_info({table})')}


def _normalized(sql):
    return ''.join(sql.lower().split()).replace('"', '')


def _require_index(connection, table, name, fields, *, unique=False):
    indexes = {row[1]: row for row in connection.execute(f'PRAGMA index_list({table})')}
    index = indexes.get(name)
    if index is None or bool(index[2]) != unique or bool(index[4]):
        raise sqlite3.OperationalError('Ozon catalog checkpoint index incompatible: ' + name)
    actual = tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{name}")'))
    if actual != fields:
        raise sqlite3.OperationalError('Ozon catalog checkpoint index columns incompatible: ' + name)


def _require_unique(connection, table, fields):
    for row in connection.execute(f'PRAGMA index_list({table})'):
        if row[2] and not row[4]:
            actual = tuple(part[2] for part in connection.execute(f'PRAGMA index_info("{row[1]}")'))
            if actual == fields:
                return
    raise sqlite3.OperationalError('Ozon catalog checkpoint unique constraint missing: ' + table)


def _require_table(connection, table, fields, checks, foreign_keys, uniques):
    actual = _columns(connection, table)
    if not fields.keys() <= actual.keys() or actual['id'][5] != 1:
        raise sqlite3.OperationalError('Ozon catalog checkpoint columns missing: ' + table)
    for name, (kind, required) in fields.items():
        row = actual[name]
        if row[2].upper() != kind or (name != 'id' and bool(row[3]) != required):
            raise sqlite3.OperationalError('Ozon catalog checkpoint column incompatible: ' + table + '.' + name)
    ddl_row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    ddl = _normalized(ddl_row[0] if ddl_row else '')
    for check in checks:
        if 'check(' + _normalized(check) + ')' not in ddl:
            raise sqlite3.OperationalError('Ozon catalog checkpoint CHECK missing: ' + table)
    found_fks = {(row[3], row[2], row[4], row[6]) for row in connection.execute(f'PRAGMA foreign_key_list({table})')}
    if not {(column, target, 'id', rule) for column, target, rule in foreign_keys} <= found_fks:
        raise sqlite3.OperationalError('Ozon catalog checkpoint FK incompatible: ' + table)
    for fields_tuple in uniques:
        _require_unique(connection, table, fields_tuple)


def apply_migration(connection, *, verbose=True):
    prerequisites = {
        'sellers': {'id'},
        'marketplaces': {'id'},
        'seller_marketplace_accounts': {'id'},
        RUNS: {'id', 'seller_id', 'marketplace_id', 'account_id', 'cursor', 'phase'},
    }
    for table, columns in prerequisites.items():
        if not columns <= _columns(connection, table).keys():
            raise sqlite3.OperationalError('Ozon catalog checkpoint prerequisite missing: ' + table)
    baseline = foreign_key_snapshot(connection)
    before = set(connection.execute('SELECT type,name FROM sqlite_master'))
    connection.execute('SAVEPOINT ozon_catalog_checkpoints_ddl')
    try:
        run_columns = _columns(connection, RUNS)
        if 'credential_fingerprint' not in run_columns:
            connection.execute(f'ALTER TABLE {RUNS} ADD COLUMN credential_fingerprint VARCHAR(64)')
        column = _columns(connection, RUNS)['credential_fingerprint']
        if column[2].upper() != 'VARCHAR(64)' or column[3]:
            raise sqlite3.OperationalError('Ozon catalog run credential fingerprint incompatible')

        checkpoint_fields = {
            'id': ('INTEGER', False), 'run_id': ('INTEGER', True),
            'seller_id': ('INTEGER', True), 'marketplace_id': ('INTEGER', True),
            'account_id': ('INTEGER', True), 'credential_fingerprint': ('VARCHAR(64)', True),
            'phase': ('VARCHAR(20)', True), 'visibility': ('VARCHAR(30)', True),
            'start_cursor': ('VARCHAR(1000)', True), 'next_cursor': ('VARCHAR(1000)', True),
            'list_total': ('INTEGER', False), 'page_limit': ('INTEGER', True),
            'generation': ('INTEGER', True), 'ttl_restarts': ('INTEGER', True),
            'base_items_json': ('TEXT', True), 'base_hash': ('VARCHAR(64)', False),
            'domain': ('VARCHAR(20)', True), 'domain_cursor': ('VARCHAR(1000)', True),
            'domain_total': ('INTEGER', False), 'domain_seen_count': ('INTEGER', True),
            'domain_page_count': ('INTEGER', True), 'staged_bytes': ('INTEGER', True),
            'list_observed_at': ('DATETIME', False), 'domain_observed_at_json': ('TEXT', True),
            'started_at': ('DATETIME', True),
            'updated_at': ('DATETIME', True),
        }
        checkpoint_fks = [
            ('run_id', RUNS, 'CASCADE'), ('seller_id', 'sellers', 'CASCADE'),
            ('marketplace_id', 'marketplaces', 'NO ACTION'),
            ('account_id', 'seller_marketplace_accounts', 'CASCADE'),
        ]
        checkpoint_checks = [
            "phase IN ('active','archived')",
            "domain IN ('list','info','attributes','prices','stocks','apply')",
            'page_limit BETWEEN 1 AND 1000',
            'generation BETWEEN 0 AND 16 AND ttl_restarts BETWEEN 0 AND 8',
            'domain_seen_count >= 0 AND domain_page_count >= 0 AND staged_bytes >= 0',
            'list_total IS NULL OR list_total >= 0',
            'domain_total IS NULL OR domain_total >= 0',
        ]
        connection.execute(f'''
            CREATE TABLE IF NOT EXISTS {CHECKPOINTS} (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL UNIQUE REFERENCES {RUNS}(id) ON DELETE CASCADE,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
                account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
                credential_fingerprint VARCHAR(64) NOT NULL,
                phase VARCHAR(20) NOT NULL,
                visibility VARCHAR(30) NOT NULL,
                start_cursor VARCHAR(1000) NOT NULL DEFAULT '',
                next_cursor VARCHAR(1000) NOT NULL DEFAULT '',
                list_total INTEGER,
                page_limit INTEGER NOT NULL DEFAULT 1000,
                generation INTEGER NOT NULL DEFAULT 0,
                ttl_restarts INTEGER NOT NULL DEFAULT 0,
                base_items_json TEXT NOT NULL DEFAULT '[]',
                base_hash VARCHAR(64),
                domain VARCHAR(20) NOT NULL DEFAULT 'list',
                domain_cursor VARCHAR(1000) NOT NULL DEFAULT '',
                domain_total INTEGER,
                domain_seen_count INTEGER NOT NULL DEFAULT 0,
                domain_page_count INTEGER NOT NULL DEFAULT 0,
                staged_bytes INTEGER NOT NULL DEFAULT 0,
                list_observed_at DATETIME,
                domain_observed_at_json TEXT NOT NULL DEFAULT '{{}}',
                started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                {', '.join('CHECK (' + check + ')' for check in checkpoint_checks)}
            )
        ''')
        _require_table(connection, CHECKPOINTS, checkpoint_fields, checkpoint_checks, checkpoint_fks, [('run_id',)])

        item_fields = {
            'id': ('INTEGER', False), 'checkpoint_id': ('INTEGER', True),
            'domain': ('VARCHAR(20)', True), 'product_id': ('VARCHAR(100)', True),
            'item_json': ('TEXT', True), 'byte_count': ('INTEGER', True),
            'observed_at': ('DATETIME', True),
        }
        item_checks = ["domain IN ('info','attributes','prices','stocks')", 'byte_count > 0']
        connection.execute(f'''
            CREATE TABLE IF NOT EXISTS {ITEMS} (
                id INTEGER PRIMARY KEY,
                checkpoint_id INTEGER NOT NULL REFERENCES {CHECKPOINTS}(id) ON DELETE CASCADE,
                domain VARCHAR(20) NOT NULL,
                product_id VARCHAR(100) NOT NULL,
                item_json TEXT NOT NULL,
                byte_count INTEGER NOT NULL,
                observed_at DATETIME NOT NULL,
                UNIQUE (checkpoint_id, domain, product_id),
                {', '.join('CHECK (' + check + ')' for check in item_checks)}
            )
        ''')
        _require_table(connection, ITEMS, item_fields, item_checks,
                       [('checkpoint_id', CHECKPOINTS, 'CASCADE')],
                       [('checkpoint_id', 'domain', 'product_id')])

        indexes = {
            'idx_catalog_checkpoint_expiry': (CHECKPOINTS, ('started_at', 'id')),
            'idx_catalog_checkpoint_account': (CHECKPOINTS, ('seller_id', 'account_id', 'run_id')),
            'idx_catalog_page_item_domain': (ITEMS, ('checkpoint_id', 'domain', 'product_id')),
        }
        for name, (table, fields) in indexes.items():
            connection.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({",".join(fields)})')
            _require_index(connection, table, name, fields)
        assert_foreign_key_safety(connection, baseline=baseline,
                                  managed_tables={CHECKPOINTS, ITEMS},
                                  label='Ozon catalog checkpoints migration')
        connection.execute('RELEASE SAVEPOINT ozon_catalog_checkpoints_ddl')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT ozon_catalog_checkpoints_ddl')
        connection.execute('RELEASE SAVEPOINT ozon_catalog_checkpoints_ddl')
        raise
    if verbose:
        print('Ozon catalog checkpoints migration completed successfully')
    return len(set(connection.execute('SELECT type,name FROM sqlite_master')) - before)


def migrate(db_path):
    if not os.path.isfile(db_path):
        raise sqlite3.OperationalError('Ozon catalog checkpoint database does not exist')
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
