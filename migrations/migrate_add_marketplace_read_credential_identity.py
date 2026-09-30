#!/usr/bin/env python3
"""Add private ciphertext identity without guessing keys for existing intents."""
import os
import sqlite3
import sys


def apply_migration(connection, *, verbose=True):
    columns = {row[1]: row for row in connection.execute(
        'PRAGMA table_info(marketplace_read_requests)')}
    if not {'id', 'account_id', 'credential_version', 'status'} <= columns.keys():
        raise sqlite3.OperationalError('Read credential identity prerequisites missing')
    changed = 'credential_fingerprint' not in columns
    if changed:
        connection.execute(
            'ALTER TABLE marketplace_read_requests ADD COLUMN credential_fingerprint VARCHAR(64)')
    column = next(row for row in connection.execute(
        'PRAGMA table_info(marketplace_read_requests)') if row[1] == 'credential_fingerprint')
    if column[2].upper() != 'VARCHAR(64)' or column[3] or column[4] is not None:
        raise sqlite3.OperationalError('Read credential identity column is incompatible')
    if verbose:
        print('Marketplace read credential identity migration completed successfully')
    return int(changed)


def migrate(db_path):
    with sqlite3.connect(db_path) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        apply_migration(connection)


if __name__ == '__main__':
    migrate(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DATABASE_PATH', 'data/seller_platform.db'))
