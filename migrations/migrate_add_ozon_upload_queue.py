#!/usr/bin/env python3
"""Add an empty, durable Ozon card preparation/review queue.

Historical bulk jobs are deliberately untouched. Runtime adoption of a legacy
job needs exact seller/account/draft/operation proof and lives outside DDL.
"""

import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety
else:
    from _foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety


RUNS = 'ozon_bulk_upload_runs'
ITEMS = 'ozon_bulk_upload_items'

FIELDS = {
    RUNS: {
        'id': ('INTEGER', False),
        'job_id': ('INTEGER', True),
        'seller_id': ('INTEGER', True),
        'account_id': ('INTEGER', True),
        'mode': ('VARCHAR(24)', True),
        'request_key_hash': ('CHAR(64)', True),
        'request_fingerprint': ('CHAR(64)', True),
        'created_by_user_id': ('INTEGER', False),
        'parent_prepare_run_id': ('INTEGER', False),
        'state': ('VARCHAR(12)', True),
        'next_due_at': ('DATETIME', False),
        'last_attempt_at': ('DATETIME', False),
        'mapping_preflight_at': ('DATETIME', False),
        'lease_token': ('CHAR(32)', False),
        'lease_until': ('DATETIME', False),
        'created_at': ('DATETIME', True),
        'updated_at': ('DATETIME', True),
    },
    ITEMS: {
        'id': ('INTEGER', False),
        'run_id': ('INTEGER', True),
        'ordinal': ('INTEGER', True),
        'imported_product_id': ('INTEGER', True),
        'reviewed_draft_id': ('INTEGER', False),
        'reviewed_version': ('INTEGER', False),
        'draft_id': ('INTEGER', False),
        'prepared_version': ('INTEGER', False),
        'operation_id': ('INTEGER', False),
        'phase': ('VARCHAR(40)', True),
        'next_due_at': ('DATETIME', False),
        'reference_wait_started_at': ('DATETIME', False),
        'local_failure_count': ('INTEGER', True),
        'title_snapshot': ('VARCHAR(300)', True),
        'offer_id_snapshot': ('VARCHAR(200)', False),
        'error_code': ('VARCHAR(100)', False),
        'error_message': ('VARCHAR(700)', False),
        'created_at': ('DATETIME', True),
        'updated_at': ('DATETIME', True),
    },
}

CHECKS = {
    RUNS: [
        "mode IN ('source_prepare','reviewed_drafts')",
        "state IN ('active','completed')",
        '(lease_token IS NULL) = (lease_until IS NULL)',
        "(state = 'active') = (next_due_at IS NOT NULL)",
    ],
    ITEMS: [
        'ordinal > 0',
        "phase IN ('pending','preparing','waiting_reference','needs_input',"
        "'prepared','reviewed','operation_linked',"
        "'needs_manual_reconciliation','failed_local','excluded')",
        'local_failure_count BETWEEN 0 AND 3',
        '(reviewed_draft_id IS NULL AND reviewed_version IS NULL) OR '
        '(reviewed_draft_id IS NOT NULL AND reviewed_version IS NOT NULL '
        'AND reviewed_draft_id > 0 AND reviewed_version > 0)',
        "phase != 'operation_linked' OR operation_id IS NOT NULL",
        "phase != 'prepared' OR (draft_id IS NOT NULL AND prepared_version IS NOT NULL)",
        "phase != 'reviewed' OR (reviewed_draft_id IS NOT NULL AND reviewed_version IS NOT NULL)",
    ],
}

FOREIGN_KEYS = {
    RUNS: {
        'job_id': ('background_jobs', 'RESTRICT'),
        'seller_id': ('sellers', 'RESTRICT'),
        'account_id': ('seller_marketplace_accounts', 'RESTRICT'),
        'created_by_user_id': ('users', 'SET NULL'),
        'parent_prepare_run_id': (RUNS, 'RESTRICT'),
    },
    ITEMS: {
        'run_id': (RUNS, 'RESTRICT'),
        'operation_id': ('marketplace_operations', 'RESTRICT'),
    },
}

UNIQUES = {
    RUNS: [('job_id',), ('seller_id', 'request_key_hash')],
    ITEMS: [('run_id', 'ordinal'), ('run_id', 'imported_product_id'),
            ('operation_id',)],
}

INDEXES = {
    'idx_ozon_bulk_run_due': (RUNS, ('state', 'next_due_at', 'last_attempt_at', 'id')),
    'idx_ozon_bulk_run_history': (
        RUNS, ('seller_id', 'account_id', 'created_at', 'id'),
    ),
    'idx_ozon_bulk_item_due': (
        ITEMS, ('run_id', 'phase', 'next_due_at', 'ordinal'),
    ),
}


def _normalized(value):
    return ''.join(value.lower().split()).replace('"', '')


def _columns(connection, table):
    return {row[1]: row for row in connection.execute(f'PRAGMA table_info({table})')}


def _require_table(connection, table):
    actual = _columns(connection, table)
    if not FIELDS[table].keys() <= actual.keys() or actual['id'][5] != 1:
        raise sqlite3.OperationalError('Ozon upload queue columns missing: ' + table)
    for name, (kind, required) in FIELDS[table].items():
        row = actual[name]
        if row[2].upper() != kind or (name != 'id' and bool(row[3]) != required):
            raise sqlite3.OperationalError(
                'Ozon upload queue column incompatible: ' + table + '.' + name
            )
    defaults = {'state': "'active'"} if table == RUNS else {'local_failure_count': '0'}
    for name, expected in defaults.items():
        if _normalized(str(actual[name][4] or '')) != _normalized(expected):
            raise sqlite3.OperationalError(
                'Ozon upload queue default incompatible: ' + table + '.' + name
            )
    ddl_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone()
    ddl = _normalized(ddl_row[0] if ddl_row else '')
    if any('check(' + _normalized(check) + ')' not in ddl for check in CHECKS[table]):
        raise sqlite3.OperationalError('Ozon upload queue CHECK incompatible: ' + table)
    found_fks = {
        (row[3], row[2], row[4], row[6])
        for row in connection.execute(f'PRAGMA foreign_key_list({table})')
    }
    required_fks = {
        (name, target, 'id', rule)
        for name, (target, rule) in FOREIGN_KEYS[table].items()
    }
    if not required_fks <= found_fks:
        raise sqlite3.OperationalError('Ozon upload queue FK incompatible: ' + table)
    found_uniques = {
        tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{index[1]}")'))
        for index in connection.execute(f'PRAGMA index_list({table})')
        if index[2] and not index[4]
    }
    if any(fields not in found_uniques for fields in UNIQUES[table]):
        raise sqlite3.OperationalError('Ozon upload queue UNIQUE incompatible: ' + table)


def _require_index(connection, name, table, columns):
    indexes = {row[1]: row for row in connection.execute(f'PRAGMA index_list({table})')}
    entry = indexes.get(name)
    if entry is None or entry[2] or entry[4]:
        raise sqlite3.OperationalError('Ozon upload queue index incompatible: ' + name)
    actual = tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{name}")'))
    if actual != columns:
        raise sqlite3.OperationalError('Ozon upload queue index incompatible: ' + name)


def apply_migration(connection, *, verbose=True):
    for table in ('background_jobs', 'sellers', 'seller_marketplace_accounts',
                  'users', 'marketplace_operations'):
        if 'id' not in _columns(connection, table):
            raise sqlite3.OperationalError('Ozon upload queue prerequisite missing: ' + table)
    baseline = foreign_key_snapshot(connection)
    before = set(connection.execute('SELECT type,name FROM sqlite_master'))
    connection.execute('SAVEPOINT ozon_upload_queue_ddl')
    try:
        connection.execute(f'''
            CREATE TABLE IF NOT EXISTS {RUNS} (
                id INTEGER PRIMARY KEY,
                job_id INTEGER NOT NULL UNIQUE REFERENCES background_jobs(id) ON DELETE RESTRICT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE RESTRICT,
                account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE RESTRICT,
                mode VARCHAR(24) NOT NULL,
                request_key_hash CHAR(64) NOT NULL,
                request_fingerprint CHAR(64) NOT NULL,
                created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                parent_prepare_run_id INTEGER REFERENCES {RUNS}(id) ON DELETE RESTRICT,
                state VARCHAR(12) NOT NULL DEFAULT 'active',
                next_due_at DATETIME,
                last_attempt_at DATETIME,
                mapping_preflight_at DATETIME,
                lease_token CHAR(32),
                lease_until DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE (seller_id,request_key_hash),
                {', '.join('CHECK (' + check + ')' for check in CHECKS[RUNS])}
            )
        ''')
        _require_table(connection, RUNS)
        connection.execute(f'''
            CREATE TABLE IF NOT EXISTS {ITEMS} (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES {RUNS}(id) ON DELETE RESTRICT,
                ordinal INTEGER NOT NULL,
                imported_product_id INTEGER NOT NULL,
                reviewed_draft_id INTEGER,
                reviewed_version INTEGER,
                draft_id INTEGER,
                prepared_version INTEGER,
                operation_id INTEGER UNIQUE REFERENCES marketplace_operations(id) ON DELETE RESTRICT,
                phase VARCHAR(40) NOT NULL,
                next_due_at DATETIME,
                reference_wait_started_at DATETIME,
                local_failure_count INTEGER NOT NULL DEFAULT 0,
                title_snapshot VARCHAR(300) NOT NULL,
                offer_id_snapshot VARCHAR(200),
                error_code VARCHAR(100),
                error_message VARCHAR(700),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE (run_id,ordinal),
                UNIQUE (run_id,imported_product_id),
                {', '.join('CHECK (' + check + ')' for check in CHECKS[ITEMS])}
            )
        ''')
        _require_table(connection, ITEMS)
        for name, (table, columns) in INDEXES.items():
            connection.execute(
                f'CREATE INDEX IF NOT EXISTS {name} ON {table} ('
                + ','.join(columns) + ')'
            )
            _require_index(connection, name, table, columns)
        assert_foreign_key_safety(
            connection, baseline=baseline, managed_tables={RUNS, ITEMS},
            label='Ozon upload queue migration',
        )
        connection.execute('RELEASE SAVEPOINT ozon_upload_queue_ddl')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT ozon_upload_queue_ddl')
        connection.execute('RELEASE SAVEPOINT ozon_upload_queue_ddl')
        raise
    if verbose:
        print('Ozon upload queue migration completed successfully')
    return len(set(connection.execute('SELECT type,name FROM sqlite_master')) - before)


def migrate(db_path):
    if not os.path.isfile(db_path):
        raise sqlite3.OperationalError('Ozon upload queue database does not exist')
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
