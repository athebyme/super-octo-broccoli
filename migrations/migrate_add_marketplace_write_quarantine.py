#!/usr/bin/env python3
"""Add explicit operator decisions without backfilling historical outcomes."""
import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety
else:
    from _foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety

Q = 'marketplace_write_quarantines'
E = 'marketplace_write_quarantine_events'
CHECKS = {
    Q: [
        '(operation_id IS NOT NULL AND media_operation_id IS NULL) OR (operation_id IS NULL AND media_operation_id IS NOT NULL)',
        "(scope_kind = 'account' AND offer_id IS NULL AND product_id IS NULL) OR (scope_kind = 'product' AND offer_id IS NOT NULL AND length(offer_id) BETWEEN 1 AND 200 AND (product_id IS NULL OR (length(product_id) BETWEEN 1 AND 100 AND product_id NOT GLOB '*[^0-9]*' AND product_id NOT LIKE '0%')))",
        "scope_reason IN ('immutable_target_verified','identity_unknown','identity_conflict','document_invalid','unsupported_kind') AND length(reviewed_scope_token) = 64",
        "(status = 'active' AND released_at IS NULL) OR (status = 'released' AND released_at IS NOT NULL)",
        'version > 0',
    ],
    E: ["action IN ('placed','note_added','released')",
        'quarantine_version > 0 AND operation_version > 0 AND length(reason) BETWEEN 10 AND 1000'],
}
FIELDS = {
    Q: {'id': ('INTEGER', False), 'seller_id': ('INTEGER', True), 'marketplace_id': ('INTEGER', True),
        'account_id': ('INTEGER', True), 'operation_id': ('INTEGER', False), 'media_operation_id': ('INTEGER', False),
        'scope_kind': ('VARCHAR(20)', True), 'offer_id': ('VARCHAR(200)', False), 'product_id': ('VARCHAR(100)', False),
        'scope_reason': ('VARCHAR(50)', True), 'reviewed_scope_token': ('VARCHAR(64)', True),
        'status': ('VARCHAR(20)', True), 'version': ('INTEGER', True), 'created_at': ('DATETIME', True),
        'updated_at': ('DATETIME', True), 'released_at': ('DATETIME', False)},
    E: {'id': ('INTEGER', False), 'quarantine_id': ('INTEGER', True), 'seller_id': ('INTEGER', True),
        'marketplace_id': ('INTEGER', True), 'account_id': ('INTEGER', True), 'actor_user_id': ('INTEGER', False),
        'action': ('VARCHAR(20)', True), 'reason': ('TEXT', True), 'quarantine_version': ('INTEGER', True),
        'operation_version': ('INTEGER', True), 'created_at': ('DATETIME', True)},
}
FKS = {
    Q: {'seller_id': ('sellers', 'CASCADE'), 'marketplace_id': ('marketplaces', 'NO ACTION'),
        'account_id': ('seller_marketplace_accounts', 'CASCADE'),
        'operation_id': ('marketplace_operations', 'RESTRICT'), 'media_operation_id': ('marketplace_media_operations', 'RESTRICT')},
    E: {'quarantine_id': (Q, 'CASCADE'), 'seller_id': ('sellers', 'CASCADE'),
        'marketplace_id': ('marketplaces', 'NO ACTION'), 'account_id': ('seller_marketplace_accounts', 'CASCADE'),
        'actor_user_id': ('users', 'SET NULL')},
}
UNIQUE = {Q: [('operation_id',), ('media_operation_id',)], E: [('quarantine_id', 'quarantine_version')]}
INDEXES = {
    'idx_write_quarantine_offer': (Q, ('seller_id', 'marketplace_id', 'account_id', 'status', 'offer_id')),
    'idx_write_quarantine_product': (Q, ('seller_id', 'marketplace_id', 'account_id', 'status', 'product_id')),
    'idx_write_quarantine_event_scope': (E, ('seller_id', 'marketplace_id', 'account_id', 'quarantine_id', 'id')),
}


def _normalize(text):
    return ''.join(text.lower().split()).replace('"', '')


def apply_migration(connection, *, verbose=True):
    for table in ('sellers', 'marketplaces', 'users', 'seller_marketplace_accounts',
                  'marketplace_operations', 'marketplace_media_operations'):
        if 'id' not in {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}:
            raise sqlite3.OperationalError('Write quarantine prerequisite missing: ' + table)
    baseline = foreign_key_snapshot(connection)
    before = set(connection.execute('SELECT type,name FROM sqlite_master'))
    connection.execute('SAVEPOINT write_quarantine_ddl')
    try:
        for table in (Q, E):
            fields = []
            for name, (kind, required) in FIELDS[table].items():
                part = f'{name} {kind}' + (' PRIMARY KEY' if name == 'id' else ' NOT NULL' if required else '')
                if name in FKS[table]:
                    target, ondelete = FKS[table][name]
                    part += f' REFERENCES {target}(id) ON DELETE {ondelete}'
                fields.append(part)
            fields += [f'CHECK ({check})' for check in CHECKS[table]]
            fields += ['UNIQUE (' + ','.join(keys) + ')' for keys in UNIQUE[table]]
            connection.execute(f'CREATE TABLE IF NOT EXISTS {table} (' + ','.join(fields) + ')')
            info = {row[1]: row for row in connection.execute(f'PRAGMA table_info({table})')}
            if (not FIELDS[table].keys() <= info.keys() or not info['id'][5] or
                    any(info[name][2].upper() != kind or (name != 'id' and bool(info[name][3]) != required)
                        for name, (kind, required) in FIELDS[table].items())):
                raise sqlite3.OperationalError('Write quarantine columns incompatible: ' + table)
            ddl = _normalize(connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0])
            if any('check(' + _normalize(check) + ')' not in ddl for check in CHECKS[table]):
                raise sqlite3.OperationalError('Write quarantine constraints incompatible: ' + table)
            fks = {(r[3], r[2], r[4], r[6]) for r in connection.execute(f'PRAGMA foreign_key_list({table})')}
            if not {(name, target, 'id', rule) for name, (target, rule) in FKS[table].items()} <= fks:
                raise sqlite3.OperationalError('Write quarantine foreign keys incompatible: ' + table)
            unique = [tuple(r[2] for r in connection.execute(f'PRAGMA index_info("{row[1]}")'))
                      for row in connection.execute(f'PRAGMA index_list({table})') if row[2] and not row[4]]
            if any(keys not in unique for keys in UNIQUE[table]):
                raise sqlite3.OperationalError('Write quarantine uniqueness missing: ' + table)
        for name, (table, fields) in INDEXES.items():
            connection.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table} (' + ','.join(fields) + ')')
            indexes = {r[1]: r for r in connection.execute(f'PRAGMA index_list({table})')}
            if (name not in indexes or indexes[name][2] or indexes[name][4] or
                    tuple(r[2] for r in connection.execute(f'PRAGMA index_info({name})')) != fields):
                raise sqlite3.OperationalError('Write quarantine index incompatible: ' + name)
        assert_foreign_key_safety(connection, baseline=baseline, managed_tables={Q, E}, label='Write quarantine migration')
        connection.execute('RELEASE SAVEPOINT write_quarantine_ddl')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT write_quarantine_ddl')
        connection.execute('RELEASE SAVEPOINT write_quarantine_ddl')
        raise
    if verbose:
        print('Marketplace write quarantine migration completed successfully')
    return len(set(connection.execute('SELECT type,name FROM sqlite_master')) - before)


def migrate(db_path):
    if not os.path.isfile(db_path):
        raise sqlite3.OperationalError('Write quarantine database does not exist')
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
