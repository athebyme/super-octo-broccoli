#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create marketplace-neutral reviewed media publication operations."""

import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import assert_foreign_key_safety, foreign_key_snapshot
else:
    from _foreign_key_safety import (  # type: ignore[no-redef]
        assert_foreign_key_safety,
        foreign_key_snapshot,
    )


MANAGED_TABLES = {
    'marketplace_media_publications',
    'marketplace_media_operations',
    'marketplace_media_operation_slides',
}


def _columns(connection: sqlite3.Connection, table_name: str):
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_info({table_name})').fetchall()
    }


def _require_prerequisites(connection: sqlite3.Connection) -> None:
    required = {
        'sellers': {'id'},
        'users': {'id'},
        'products': {'id', 'seller_id', 'nm_id'},
        'imported_products': {'id', 'seller_id', 'product_id'},
        'infographic_campaigns': {'id', 'seller_id'},
        'infographic_campaign_items': {'id', 'campaign_id', 'seller_id'},
        'infographic_campaign_slides': {
            'id', 'item_id', 'seller_id', 'artifact_sha256', 'review_status',
        },
        'seller_marketplace_accounts': {'id', 'seller_id', 'marketplace_id'},
        'marketplace_listings': {
            'id', 'seller_id', 'account_id', 'legacy_product_id',
        },
    }
    for table_name, columns in required.items():
        actual = _columns(connection, table_name)
        if not actual:
            raise sqlite3.OperationalError(
                f'marketplace media prerequisite missing: {table_name}'
            )
        missing = columns - actual
        if missing:
            raise sqlite3.OperationalError(
                f'{table_name} is missing columns: {", ".join(sorted(missing))}'
            )


def _create_schema(connection: sqlite3.Connection) -> None:
    _require_prerequisites(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_media_publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            campaign_id INTEGER
                REFERENCES infographic_campaigns(id) ON DELETE SET NULL,
            created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            confirmed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            marketplace_code VARCHAR(32) NOT NULL,
            account_id INTEGER
                REFERENCES seller_marketplace_accounts(id) ON DELETE SET NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'draft',
            placement_policy VARCHAR(32) NOT NULL DEFAULT 'prepend_approved',
            overflow_policy VARCHAR(32) NOT NULL DEFAULT 'trim_current_tail',
            scope_json TEXT NOT NULL DEFAULT '{}',
            constraints_json TEXT NOT NULL DEFAULT '{}',
            total_items INTEGER NOT NULL DEFAULT 0,
            ready_items INTEGER NOT NULL DEFAULT 0,
            blocked_items INTEGER NOT NULL DEFAULT 0,
            queued_items INTEGER NOT NULL DEFAULT 0,
            succeeded_items INTEGER NOT NULL DEFAULT 0,
            failed_items INTEGER NOT NULL DEFAULT 0,
            uncertain_items INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            confirmed_at DATETIME,
            completed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_media_publication_status CHECK (
                status IN (
                    'draft','ready','queued','running','succeeded',
                    'partial','cancelled'
                )
            ),
            CONSTRAINT ck_marketplace_media_placement_policy CHECK (
                placement_policy IN ('prepend_approved')
            ),
            CONSTRAINT ck_marketplace_media_overflow_policy CHECK (
                overflow_policy IN ('trim_current_tail')
            ),
            CONSTRAINT ck_marketplace_media_publication_counters CHECK (
                total_items >= 0 AND ready_items >= 0 AND blocked_items >= 0
                AND queued_items >= 0 AND succeeded_items >= 0
                AND failed_items >= 0 AND uncertain_items >= 0
                AND version >= 1
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_media_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_id INTEGER NOT NULL
                REFERENCES marketplace_media_publications(id) ON DELETE CASCADE,
            rollback_of_operation_id INTEGER
                REFERENCES marketplace_media_operations(id) ON DELETE SET NULL,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            confirmed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            infographic_item_id INTEGER
                REFERENCES infographic_campaign_items(id) ON DELETE SET NULL,
            imported_product_id INTEGER
                REFERENCES imported_products(id) ON DELETE SET NULL,
            marketplace_code VARCHAR(32) NOT NULL,
            account_id INTEGER
                REFERENCES seller_marketplace_accounts(id) ON DELETE SET NULL,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            legacy_product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            external_item_id VARCHAR(200) NOT NULL,
            operation_kind VARCHAR(20) NOT NULL DEFAULT 'publish',
            status VARCHAR(24) NOT NULL DEFAULT 'ready',
            placement_policy VARCHAR(32) NOT NULL DEFAULT 'prepend_approved',
            target_json TEXT NOT NULL DEFAULT '{}',
            source_snapshot_json TEXT NOT NULL DEFAULT '{}',
            baseline_media_json TEXT NOT NULL DEFAULT '[]',
            proposed_media_json TEXT NOT NULL DEFAULT '[]',
            dropped_media_json TEXT NOT NULL DEFAULT '[]',
            confirmed_media_json TEXT NOT NULL DEFAULT '[]',
            baseline_fingerprint VARCHAR(64) NOT NULL,
            proposed_fingerprint VARCHAR(64) NOT NULL,
            confirmed_fingerprint VARCHAR(64),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            reconcile_count INTEGER NOT NULL DEFAULT 0,
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            public_assets_expires_at DATETIME,
            submitted_at DATETIME,
            last_reconciled_at DATETIME,
            next_reconcile_at DATETIME,
            deadline_at DATETIME,
            completed_at DATETIME,
            version INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_media_operation_kind CHECK (
                operation_kind IN ('publish','rollback')
            ),
            CONSTRAINT ck_marketplace_media_operation_status CHECK (
                status IN (
                    'ready','blocked','queued','preflighting','submitting',
                    'reconciling','succeeded','uncertain','failed','conflict',
                    'cancelled'
                )
            ),
            CONSTRAINT ck_marketplace_media_operation_placement CHECK (
                placement_policy IN ('prepend_approved','restore_snapshot')
            ),
            CONSTRAINT ck_marketplace_media_operation_counters CHECK (
                attempt_count >= 0 AND attempt_count <= 1
                AND reconcile_count >= 0 AND version >= 1
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_media_operation_slides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id INTEGER NOT NULL
                REFERENCES marketplace_media_operations(id) ON DELETE CASCADE,
            slide_id INTEGER NOT NULL
                REFERENCES infographic_campaign_slides(id) ON DELETE RESTRICT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            artifact_sha256 VARCHAR(64) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_media_operation_slide UNIQUE (
                operation_id, slide_id
            ),
            CONSTRAINT uq_marketplace_media_operation_slide_position UNIQUE (
                operation_id, position
            ),
            CONSTRAINT ck_marketplace_media_operation_slide_position CHECK (
                position >= 1
            )
        )
    ''')
    for statement in (
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_publications_seller_id '
        'ON marketplace_media_publications(seller_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_publications_campaign_id '
        'ON marketplace_media_publications(campaign_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_publications_created_by_user_id '
        'ON marketplace_media_publications(created_by_user_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_publications_confirmed_by_user_id '
        'ON marketplace_media_publications(confirmed_by_user_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_publications_marketplace_code '
        'ON marketplace_media_publications(marketplace_code)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_publications_account_id '
        'ON marketplace_media_publications(account_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_publications_status '
        'ON marketplace_media_publications(status)',
        'CREATE INDEX IF NOT EXISTS idx_marketplace_media_publication_seller_created '
        'ON marketplace_media_publications(seller_id, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_marketplace_media_publication_scope '
        'ON marketplace_media_publications('
        'seller_id, marketplace_code, account_id, status)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_publication_id '
        'ON marketplace_media_operations(publication_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_rollback_of_operation_id '
        'ON marketplace_media_operations(rollback_of_operation_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_seller_id '
        'ON marketplace_media_operations(seller_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_created_by_user_id '
        'ON marketplace_media_operations(created_by_user_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_confirmed_by_user_id '
        'ON marketplace_media_operations(confirmed_by_user_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_infographic_item_id '
        'ON marketplace_media_operations(infographic_item_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_imported_product_id '
        'ON marketplace_media_operations(imported_product_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_marketplace_code '
        'ON marketplace_media_operations(marketplace_code)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_account_id '
        'ON marketplace_media_operations(account_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_listing_id '
        'ON marketplace_media_operations(listing_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_legacy_product_id '
        'ON marketplace_media_operations(legacy_product_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operations_status '
        'ON marketplace_media_operations(status)',
        'CREATE INDEX IF NOT EXISTS idx_marketplace_media_operation_due '
        'ON marketplace_media_operations(status, next_reconcile_at, id)',
        'CREATE INDEX IF NOT EXISTS idx_marketplace_media_operation_seller_status '
        'ON marketplace_media_operations('
        'seller_id, marketplace_code, status, updated_at)',
        'CREATE UNIQUE INDEX IF NOT EXISTS '
        'uq_marketplace_media_publication_publish_item '
        'ON marketplace_media_operations(publication_id, infographic_item_id) '
        "WHERE operation_kind = 'publish'",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_media_active_wb_target "
        "ON marketplace_media_operations(legacy_product_id) "
        "WHERE marketplace_code = 'wb' AND legacy_product_id IS NOT NULL "
        "AND status IN ('queued','preflighting','submitting','reconciling','uncertain')",
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_media_active_listing_target '
        'ON marketplace_media_operations(account_id, listing_id) '
        'WHERE account_id IS NOT NULL AND listing_id IS NOT NULL '
        "AND status IN ('queued','preflighting','submitting','reconciling','uncertain')",
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operation_slides_operation_id '
        'ON marketplace_media_operation_slides(operation_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operation_slides_slide_id '
        'ON marketplace_media_operation_slides(slide_id)',
        'CREATE INDEX IF NOT EXISTS ix_marketplace_media_operation_slides_seller_id '
        'ON marketplace_media_operation_slides(seller_id)',
        'CREATE INDEX IF NOT EXISTS idx_marketplace_media_operation_slide_order '
        'ON marketplace_media_operation_slides(operation_id, position)',
    ):
        connection.execute(statement)


def _verify_schema(connection: sqlite3.Connection) -> None:
    expected = {
        'marketplace_media_publications': {
            'id', 'seller_id', 'campaign_id', 'created_by_user_id',
            'confirmed_by_user_id', 'marketplace_code', 'account_id', 'status',
            'placement_policy', 'overflow_policy', 'scope_json',
            'constraints_json', 'total_items', 'ready_items', 'blocked_items',
            'queued_items', 'succeeded_items', 'failed_items',
            'uncertain_items', 'version', 'confirmed_at', 'completed_at',
            'created_at', 'updated_at',
        },
        'marketplace_media_operations': {
            'id', 'publication_id', 'rollback_of_operation_id', 'seller_id',
            'created_by_user_id', 'confirmed_by_user_id',
            'infographic_item_id', 'imported_product_id', 'marketplace_code',
            'account_id', 'listing_id', 'legacy_product_id', 'external_item_id',
            'operation_kind', 'status', 'placement_policy', 'target_json',
            'source_snapshot_json', 'baseline_media_json',
            'proposed_media_json', 'dropped_media_json',
            'confirmed_media_json', 'baseline_fingerprint',
            'proposed_fingerprint', 'confirmed_fingerprint', 'attempt_count',
            'reconcile_count', 'error_code', 'error_message',
            'public_assets_expires_at', 'submitted_at', 'last_reconciled_at',
            'next_reconcile_at', 'deadline_at', 'completed_at', 'version',
            'created_at', 'updated_at',
        },
        'marketplace_media_operation_slides': {
            'id', 'operation_id', 'slide_id', 'seller_id', 'position',
            'artifact_sha256', 'created_at',
        },
    }
    for table_name, columns in expected.items():
        missing = columns - _columns(connection, table_name)
        if missing:
            raise sqlite3.OperationalError(
                f'{table_name} is missing columns: {", ".join(sorted(missing))}'
            )


def apply_migration(connection: sqlite3.Connection, *, verbose: bool = True) -> int:
    baseline = foreign_key_snapshot(connection)
    before = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    _create_schema(connection)
    _verify_schema(connection)
    assert_foreign_key_safety(
        connection,
        baseline=baseline,
        managed_tables=MANAGED_TABLES,
        label='Marketplace media publication migration',
    )
    after = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    if verbose:
        print('marketplace media publications: OK')
    return len(after - before)


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
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get('DATABASE_PATH', 'data/seller_platform.db')
    )
    migrate(path)
