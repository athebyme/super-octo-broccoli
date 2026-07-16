#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add durable marketplace operation journal and listing snapshots."""

import os
import sqlite3
import sys


def _schema_objects(connection: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }


def _columns(connection: sqlite3.Connection, table_name: str) -> set:
    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def _require_prerequisites(connection: sqlite3.Connection) -> None:
    required = {
        "users",
        "sellers",
        "marketplaces",
        "seller_marketplace_accounts",
        "marketplace_product_drafts",
        "marketplace_listings",
    }
    missing = sorted(required - _schema_objects(connection))
    if missing:
        raise sqlite3.OperationalError(
            "Marketplace operation migration prerequisites are missing: "
            + ", ".join(missing)
        )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    _require_prerequisites(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            draft_id INTEGER REFERENCES marketplace_product_drafts(id) ON DELETE SET NULL,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            parent_operation_id INTEGER REFERENCES marketplace_operations(id) ON DELETE SET NULL,
            created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            operation_kind VARCHAR(50) NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'queued',
            idempotency_key VARCHAR(128) NOT NULL,
            request_fingerprint VARCHAR(64) NOT NULL,
            contract_version VARCHAR(80) NOT NULL,
            draft_version INTEGER,
            request_summary_json TEXT NOT NULL DEFAULT '{}',
            quota_snapshot_json TEXT NOT NULL DEFAULT '{}',
            quota_reserved INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            poll_count INTEGER NOT NULL DEFAULT 0,
            reconcile_count INTEGER NOT NULL DEFAULT 0,
            external_task_id VARCHAR(100),
            provider_request_ids_json TEXT NOT NULL DEFAULT '[]',
            item_results_json TEXT NOT NULL DEFAULT '[]',
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            submitted_at DATETIME,
            last_polled_at DATETIME,
            next_poll_at DATETIME,
            deadline_at DATETIME,
            completed_at DATETIME,
            version INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_operation_idempotency UNIQUE (
                account_id, operation_kind, idempotency_key
            ),
            CONSTRAINT ck_marketplace_operation_kind CHECK (
                operation_kind IN ('product_import','product_import_rollback')
            ),
            CONSTRAINT ck_marketplace_operation_status CHECK (
                status IN (
                    'queued','submitting','submitted','polling','succeeded',
                    'partial','failed','uncertain','cancelled'
                )
            ),
            CONSTRAINT ck_marketplace_operation_quota_reserved CHECK (
                quota_reserved >= 0 AND quota_reserved <= 100
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_listing_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            operation_id INTEGER NOT NULL UNIQUE REFERENCES marketplace_operations(id) ON DELETE CASCADE,
            draft_id INTEGER REFERENCES marketplace_product_drafts(id) ON DELETE SET NULL,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            rollback_operation_id INTEGER REFERENCES marketplace_operations(id) ON DELETE SET NULL,
            snapshot_kind VARCHAR(50) NOT NULL,
            source_fingerprint VARCHAR(64) NOT NULL,
            before_fingerprint VARCHAR(64),
            submitted_fingerprint VARCHAR(64) NOT NULL,
            confirmed_fingerprint VARCHAR(64),
            before_state_json TEXT NOT NULL DEFAULT '{}',
            submitted_state_json TEXT NOT NULL,
            confirmed_state_json TEXT NOT NULL DEFAULT '{}',
            rollback_state_json TEXT NOT NULL DEFAULT '{}',
            rollback_status VARCHAR(30) NOT NULL DEFAULT 'not_requested',
            rollback_error_code VARCHAR(100),
            rollback_error_message VARCHAR(1000),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_listing_snapshot_kind CHECK (
                snapshot_kind IN ('product_import')
            ),
            CONSTRAINT ck_marketplace_listing_snapshot_rollback_status CHECK (
                rollback_status IN (
                    'not_requested','unavailable','available','pending',
                    'succeeded','failed','conflict'
                )
            )
        )
    ''')

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_seller_id "
        "ON marketplace_operations(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_marketplace_id "
        "ON marketplace_operations(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_account_id "
        "ON marketplace_operations(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_draft_id "
        "ON marketplace_operations(draft_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_listing_id "
        "ON marketplace_operations(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_parent_operation_id "
        "ON marketplace_operations(parent_operation_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_operation_due "
        "ON marketplace_operations(status, next_poll_at)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_operation_seller_status "
        "ON marketplace_operations(seller_id, marketplace_id, status, updated_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_operation_active_draft "
        "ON marketplace_operations(draft_id) "
        "WHERE draft_id IS NOT NULL AND status IN ("
        "'queued','submitting','submitted','polling','uncertain')",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_seller_id "
        "ON marketplace_listing_snapshots(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_marketplace_id "
        "ON marketplace_listing_snapshots(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_account_id "
        "ON marketplace_listing_snapshots(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_operation_id "
        "ON marketplace_listing_snapshots(operation_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_draft_id "
        "ON marketplace_listing_snapshots(draft_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_listing_id "
        "ON marketplace_listing_snapshots(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_rollback_operation_id "
        "ON marketplace_listing_snapshots(rollback_operation_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_snapshot_seller_listing "
        "ON marketplace_listing_snapshots(seller_id, listing_id, created_at)",
    )
    for statement in statements:
        connection.execute(statement)

    expected = {
        "marketplace_operations": {
            "seller_id",
            "marketplace_id",
            "account_id",
            "draft_id",
            "operation_kind",
            "status",
            "idempotency_key",
            "request_fingerprint",
            "quota_snapshot_json",
            "external_task_id",
            "item_results_json",
            "version",
        },
        "marketplace_listing_snapshots": {
            "seller_id",
            "account_id",
            "operation_id",
            "submitted_state_json",
            "submitted_fingerprint",
            "rollback_status",
        },
    }
    for table_name, columns in expected.items():
        missing = columns - _columns(connection, table_name)
        if missing:
            raise sqlite3.OperationalError(
                f"{table_name} is missing columns: {', '.join(sorted(missing))}"
            )


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    before = _schema_objects(connection)
    _ensure_schema(connection)
    after = _schema_objects(connection)
    if verbose:
        print("Marketplace operation schema migration completed successfully!")
    return len(after - before)


def migrate(db_path: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        apply_migration(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    database = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("DATABASE_PATH", "data/seller_platform.db")
    )
    migrate(database)
