#!/usr/bin/env python3
"""Add a local expiry dedup journal; preserve credentials, catalog and history."""
import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety
else:
    from _foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety

TABLE = 'marketplace_credential_notices'
COLUMNS = {'account_id', 'seller_id', 'marketplace_id', 'credential_version',
           'expires_at', 'highest_stage', 'notified_at'}


def apply_migration(connection, *, verbose=True):
    for table, columns in {'sellers': {'id'}, 'marketplaces': {'id'},
            'seller_marketplace_accounts': {'id', 'seller_id', 'marketplace_id', 'is_active', 'credential_expires_at'}}.items():
        if not columns <= {r[1] for r in connection.execute(f'PRAGMA table_info({table})')}:
            raise sqlite3.OperationalError('Credential notice prerequisites missing: '+table)
    baseline = foreign_key_snapshot(connection)
    before = set(connection.execute('SELECT type,name FROM sqlite_master'))
    connection.execute('SAVEPOINT credential_notices_ddl')
    try:
        connection.execute('''CREATE TABLE IF NOT EXISTS marketplace_credential_notices (
            account_id INTEGER PRIMARY KEY REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            credential_version INTEGER NOT NULL,
            expires_at DATETIME NOT NULL,
            highest_stage INTEGER NOT NULL,
            notified_at DATETIME NOT NULL,
            CHECK (credential_version > 0 AND highest_stage BETWEEN 1 AND 4)
        )''')
        connection.execute('''CREATE INDEX IF NOT EXISTS idx_marketplace_credential_notice_scope
            ON marketplace_credential_notices(seller_id,marketplace_id,account_id)''')
        connection.execute('''CREATE INDEX IF NOT EXISTS idx_marketplace_account_expiry
            ON seller_marketplace_accounts(marketplace_id,is_active,credential_expires_at,id)''')
        info = {r[1]: r for r in connection.execute(f'PRAGMA table_info({TABLE})')}
        if not COLUMNS <= info.keys() or not info['account_id'][5] or any(not info[c][3] for c in COLUMNS - {'account_id'}):
            raise sqlite3.OperationalError('Credential notice schema is incompatible')
        fks = {(r[3], r[2], r[4], r[6]) for r in connection.execute(f'PRAGMA foreign_key_list({TABLE})')}
        if not {('account_id','seller_marketplace_accounts','id','CASCADE'),
                ('seller_id','sellers','id','CASCADE'), ('marketplace_id','marketplaces','id','NO ACTION')} <= fks:
            raise sqlite3.OperationalError('Credential notice foreign keys are incompatible')
        ddl = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
        if 'check(credential_version>0andhighest_stagebetween1and4)' not in ''.join(ddl.lower().split()).replace('"',''):
            raise sqlite3.OperationalError('Credential notice constraints are incompatible')
        for name, columns in {'idx_marketplace_credential_notice_scope': ('seller_id','marketplace_id','account_id'),
                'idx_marketplace_account_expiry': ('marketplace_id','is_active','credential_expires_at','id')}.items():
            if tuple(r[2] for r in connection.execute(f'PRAGMA index_info({name})')) != columns:
                raise sqlite3.OperationalError('Credential notice index is incompatible')
        assert_foreign_key_safety(connection, baseline=baseline,
            managed_tables={TABLE, 'seller_marketplace_accounts'}, label='Credential notice migration')
        connection.execute('RELEASE SAVEPOINT credential_notices_ddl')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT credential_notices_ddl')
        connection.execute('RELEASE SAVEPOINT credential_notices_ddl')
        raise
    if verbose:
        print('Marketplace credential notice migration completed successfully')
    return len(set(connection.execute('SELECT type,name FROM sqlite_master')) - before)


def migrate(db_path):
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
    migrate(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DATABASE_PATH','data/seller_platform.db'))
