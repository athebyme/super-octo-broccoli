#!/usr/bin/env python3
"""Add exact official dictionary review; never rewrite active reference values."""
import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety
else:
    from _foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety

TABLE = 'ozon_reference_value_reviews'
COLUMNS = {
    'attribute_id', 'product_type_id', 'version', 'status', 'baseline_hash',
    'baseline_version', 'schema_hash', 'scope_hash', 'candidate_hash', 'candidate_json',
    'payload_bytes', 'previous_count', 'candidate_count', 'observed_at', 'expires_at',
    'approved_by', 'approved_at', 'applied_at',
}


def apply_migration(connection, *, verbose=True):
    for table in ('marketplace_attribute_definitions', 'marketplace_product_types', 'users'):
        if 'id' not in {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}:
            raise sqlite3.OperationalError(f'Ozon reference review prerequisite missing: {table}')
    baseline = foreign_key_snapshot(connection)
    before = set(connection.execute('SELECT type,name FROM sqlite_master'))
    connection.execute('''CREATE TABLE IF NOT EXISTS ozon_reference_value_reviews (
        attribute_id INTEGER PRIMARY KEY REFERENCES marketplace_attribute_definitions(id),
        product_type_id INTEGER NOT NULL REFERENCES marketplace_product_types(id),
        version INTEGER NOT NULL DEFAULT 1,
        status VARCHAR(20) NOT NULL DEFAULT 'pending_review',
        baseline_hash VARCHAR(64) NOT NULL,
        baseline_version INTEGER NOT NULL,
        schema_hash VARCHAR(64) NOT NULL,
        scope_hash VARCHAR(64) NOT NULL,
        candidate_hash VARCHAR(64) NOT NULL,
        candidate_json TEXT,
        payload_bytes INTEGER NOT NULL DEFAULT 0,
        previous_count INTEGER NOT NULL,
        candidate_count INTEGER NOT NULL,
        observed_at DATETIME NOT NULL,
        expires_at DATETIME NOT NULL,
        approved_by INTEGER REFERENCES users(id),
        approved_at DATETIME,
        applied_at DATETIME,
        CHECK (status IN ('pending_review','approved','applied','stale')),
        CHECK (version > 0 AND payload_bytes >= 0 AND previous_count >= 0 AND candidate_count >= 0)
    )''')
    connection.execute('''CREATE INDEX IF NOT EXISTS idx_ozon_reference_review_status
        ON ozon_reference_value_reviews(status,product_type_id)''')
    info = {row[1]: row for row in connection.execute(f'PRAGMA table_info({TABLE})')}
    if not COLUMNS <= info.keys() or not info['attribute_id'][5]:
        raise sqlite3.OperationalError('Ozon reference review schema is incomplete')
    nullable = {'attribute_id', 'candidate_json', 'approved_by', 'approved_at', 'applied_at'}
    if any(not info[column][3] for column in COLUMNS - nullable):
        raise sqlite3.OperationalError('Ozon reference review nullability is incompatible')
    fks = {(row[3], row[2], row[4]) for row in connection.execute(f'PRAGMA foreign_key_list({TABLE})')}
    if not {('attribute_id','marketplace_attribute_definitions','id'),
            ('product_type_id','marketplace_product_types','id'), ('approved_by','users','id')} <= fks:
        raise sqlite3.OperationalError('Ozon reference review foreign keys are incomplete')
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
    compact = ''.join(sql.lower().split()).replace('"', '')
    for clause in ("check(statusin('pending_review','approved','applied','stale'))",
                   'check(version>0andpayload_bytes>=0andprevious_count>=0andcandidate_count>=0)'):
        if clause not in compact:
            raise sqlite3.OperationalError('Ozon reference review checks are incomplete')
    if tuple(row[2] for row in connection.execute('PRAGMA index_info(idx_ozon_reference_review_status)')) != ('status', 'product_type_id'):
        raise sqlite3.OperationalError('Ozon reference review status index is incompatible')
    assert_foreign_key_safety(connection, baseline=baseline, managed_tables={TABLE}, label='Ozon reference review migration')
    if verbose:
        print('Ozon reference reviews migration completed successfully')
    return len(set(connection.execute('SELECT type,name FROM sqlite_master')) - before)


def migrate(db_path):
    with sqlite3.connect(db_path) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        apply_migration(connection)


if __name__ == '__main__':
    migrate(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DATABASE_PATH', 'data/seller_platform.db'))
