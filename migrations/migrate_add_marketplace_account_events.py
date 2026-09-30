#!/usr/bin/env python3
"""Add a credential-free account history without inventing old events."""
import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety
else:
    from _foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety

TABLE = 'marketplace_account_events'
ACTIONS = "action IN ('connected','key_replaced','settings_changed','default_changed','disconnected')"
VERSIONS = 'account_version_before >= 0 AND account_version_after > account_version_before AND credential_version_before >= 0 AND credential_version_after >= credential_version_before'
COLUMNS = {'id', 'account_id', 'seller_id', 'marketplace_id', 'actor_user_id', 'action',
           'account_version_before', 'account_version_after', 'credential_version_before',
           'credential_version_after', 'changes_json', 'created_at'}


def apply_migration(connection, *, verbose=True):
    for table in ('seller_marketplace_accounts', 'sellers', 'marketplaces', 'users'):
        if 'id' not in {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}:
            raise sqlite3.OperationalError('Account history prerequisite missing: ' + table)
    baseline = foreign_key_snapshot(connection)
    before = set(connection.execute('SELECT type,name FROM sqlite_master'))
    connection.execute('SAVEPOINT account_history_ddl')
    try:
        connection.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            action VARCHAR(30) NOT NULL,
            account_version_before INTEGER NOT NULL,
            account_version_after INTEGER NOT NULL,
            credential_version_before INTEGER NOT NULL,
            credential_version_after INTEGER NOT NULL,
            changes_json TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            CONSTRAINT uq_marketplace_account_event_version UNIQUE(account_id,account_version_after),
            CHECK ({ACTIONS}), CHECK ({VERSIONS})
        )''')
        connection.execute('''CREATE INDEX IF NOT EXISTS idx_marketplace_account_event_scope
            ON marketplace_account_events(seller_id,marketplace_id,account_id,id)''')
        info = {row[1]: row for row in connection.execute(f'PRAGMA table_info({TABLE})')}
        if not COLUMNS <= info.keys() or not info['id'][5] or any(not info[c][3] for c in COLUMNS - {'id', 'actor_user_id'}):
            raise sqlite3.OperationalError('Account history schema is incompatible')
        fks = {(r[3], r[2], r[4], r[6]) for r in connection.execute(f'PRAGMA foreign_key_list({TABLE})')}
        if not {('account_id', 'seller_marketplace_accounts', 'id', 'CASCADE'),
                ('seller_id', 'sellers', 'id', 'CASCADE'), ('marketplace_id', 'marketplaces', 'id', 'NO ACTION'),
                ('actor_user_id', 'users', 'id', 'SET NULL')} <= fks:
            raise sqlite3.OperationalError('Account history foreign keys are incompatible')
        normalize = lambda text: ''.join(text.lower().split()).replace('"', '')
        ddl = normalize(connection.execute("SELECT sql FROM sqlite_master WHERE name=? AND type='table'", (TABLE,)).fetchone()[0])
        if any('check(' + normalize(check) + ')' not in ddl for check in (ACTIONS, VERSIONS)):
            raise sqlite3.OperationalError('Account history constraints are incompatible')
        unique = [tuple(r[2] for r in connection.execute(f'PRAGMA index_info("{row[1]}")'))
                  for row in connection.execute(f'PRAGMA index_list({TABLE})') if row[2] and not row[4]]
        if ('account_id', 'account_version_after') not in unique:
            raise sqlite3.OperationalError('Account history version uniqueness missing')
        if tuple(r[2] for r in connection.execute('PRAGMA index_info(idx_marketplace_account_event_scope)')) != ('seller_id', 'marketplace_id', 'account_id', 'id'):
            raise sqlite3.OperationalError('Account history scope index is incompatible')
        assert_foreign_key_safety(connection, baseline=baseline, managed_tables={TABLE}, label='Account history migration')
        connection.execute('RELEASE SAVEPOINT account_history_ddl')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT account_history_ddl')
        connection.execute('RELEASE SAVEPOINT account_history_ddl')
        raise
    if verbose:
        print('Marketplace account history migration completed successfully')
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
    migrate(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DATABASE_PATH', 'data/seller_platform.db'))
