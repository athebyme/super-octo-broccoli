#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Expand durable marketplace journals for full-state Ozon product updates."""

import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import (
        assert_foreign_key_safety,
        foreign_key_snapshot,
    )
else:
    from _foreign_key_safety import (  # type: ignore[no-redef]
        assert_foreign_key_safety,
        foreign_key_snapshot,
    )


OPERATION_COLUMNS = (
    "id", "seller_id", "marketplace_id", "account_id", "draft_id",
    "listing_id", "parent_operation_id", "created_by_user_id",
    "operation_kind", "status", "idempotency_key", "request_fingerprint",
    "contract_version", "draft_version", "request_summary_json",
    "quota_snapshot_json", "quota_reserved", "attempt_count", "poll_count",
    "reconcile_count", "external_task_id", "provider_request_ids_json",
    "item_results_json", "error_code", "error_message", "submitted_at",
    "last_polled_at", "next_poll_at", "deadline_at", "completed_at",
    "version", "created_at", "updated_at",
)
SNAPSHOT_COLUMNS = (
    "id", "seller_id", "marketplace_id", "account_id", "operation_id",
    "draft_id", "listing_id", "rollback_operation_id", "snapshot_kind",
    "source_fingerprint", "before_fingerprint", "submitted_fingerprint",
    "confirmed_fingerprint", "before_state_json", "submitted_state_json",
    "confirmed_state_json", "rollback_state_json", "rollback_status",
    "rollback_error_code", "rollback_error_message", "created_at",
    "updated_at",
)
MANAGED_TABLES = {
    "marketplace_operations",
    "marketplace_listing_snapshots",
}


def _objects(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }


def _columns(connection, table_name):
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _table_sql(connection, table_name):
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return str(row[0] or "") if row else ""


def _create_operations(connection):
    connection.execute('''
        CREATE TABLE marketplace_operations (
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
                operation_kind IN (
                    'product_import','product_import_rollback',
                    'product_update','product_update_rollback',
                    'price_update','stock_update',
                    'price_rollback','stock_rollback'
                )
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


def _create_snapshots(connection):
    connection.execute('''
        CREATE TABLE marketplace_listing_snapshots (
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
                snapshot_kind IN ('product_import','product_update','price','stock')
            ),
            CONSTRAINT ck_marketplace_listing_snapshot_rollback_status CHECK (
                rollback_status IN (
                    'not_requested','unavailable','available','pending',
                    'succeeded','failed','conflict'
                )
            )
        )
    ''')


def _indexes(connection):
    statements = (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_seller_id ON marketplace_operations(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_marketplace_id ON marketplace_operations(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_account_id ON marketplace_operations(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_draft_id ON marketplace_operations(draft_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_listing_id ON marketplace_operations(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_operations_parent_operation_id ON marketplace_operations(parent_operation_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_operation_due ON marketplace_operations(status, next_poll_at)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_operation_seller_status ON marketplace_operations(seller_id, marketplace_id, status, updated_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_operation_active_draft ON marketplace_operations(draft_id) WHERE draft_id IS NOT NULL AND status IN ('queued','submitting','submitted','polling','uncertain')",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_seller_id ON marketplace_listing_snapshots(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_marketplace_id ON marketplace_listing_snapshots(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_account_id ON marketplace_listing_snapshots(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_operation_id ON marketplace_listing_snapshots(operation_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_draft_id ON marketplace_listing_snapshots(draft_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_listing_id ON marketplace_listing_snapshots(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_snapshots_rollback_operation_id ON marketplace_listing_snapshots(rollback_operation_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_snapshot_seller_listing ON marketplace_listing_snapshots(seller_id, listing_id, created_at)",
    )
    for statement in statements:
        connection.execute(statement)


def _expand(connection):
    required = {
        "marketplace_operations", "marketplace_listing_snapshots",
        "marketplace_commercial_proposals",
    }
    missing = sorted(required - _objects(connection))
    if missing:
        raise sqlite3.OperationalError(
            "Marketplace product update prerequisites are missing: "
            + ", ".join(missing)
        )
    operation_sql = _table_sql(connection, "marketplace_operations")
    snapshot_sql = _table_sql(connection, "marketplace_listing_snapshots")
    if "'product_update'" in operation_sql and "'product_update'" in snapshot_sql:
        _indexes(connection)
        return False
    if _columns(connection, "marketplace_operations") != set(OPERATION_COLUMNS):
        raise sqlite3.OperationalError(
            "Marketplace operation columns differ from the expected P6 schema"
        )
    if _columns(connection, "marketplace_listing_snapshots") != set(SNAPSHOT_COLUMNS):
        raise sqlite3.OperationalError(
            "Marketplace snapshot columns differ from the expected P6 schema"
        )
    leftovers = {
        "_marketplace_operations_before_product_updates",
        "_marketplace_snapshots_before_product_updates",
    }.intersection(_objects(connection))
    if leftovers:
        raise sqlite3.OperationalError(
            "Incomplete marketplace product update migration requires recovery"
        )

    operation_count = connection.execute(
        "SELECT COUNT(*) FROM marketplace_operations"
    ).fetchone()[0]
    snapshot_count = connection.execute(
        "SELECT COUNT(*) FROM marketplace_listing_snapshots"
    ).fetchone()[0]
    connection.execute("PRAGMA legacy_alter_table=ON")
    connection.execute(
        "ALTER TABLE marketplace_listing_snapshots "
        "RENAME TO _marketplace_snapshots_before_product_updates"
    )
    connection.execute(
        "ALTER TABLE marketplace_operations "
        "RENAME TO _marketplace_operations_before_product_updates"
    )
    _create_operations(connection)
    operation_columns = ", ".join(OPERATION_COLUMNS)
    connection.execute(
        f"INSERT INTO marketplace_operations ({operation_columns}) "
        f"SELECT {operation_columns} "
        "FROM _marketplace_operations_before_product_updates"
    )
    _create_snapshots(connection)
    snapshot_columns = ", ".join(SNAPSHOT_COLUMNS)
    connection.execute(
        f"INSERT INTO marketplace_listing_snapshots ({snapshot_columns}) "
        f"SELECT {snapshot_columns} "
        "FROM _marketplace_snapshots_before_product_updates"
    )
    if connection.execute(
        "SELECT COUNT(*) FROM marketplace_operations"
    ).fetchone()[0] != operation_count:
        raise sqlite3.OperationalError("Marketplace operation rebuild lost rows")
    if connection.execute(
        "SELECT COUNT(*) FROM marketplace_listing_snapshots"
    ).fetchone()[0] != snapshot_count:
        raise sqlite3.OperationalError("Marketplace snapshot rebuild lost rows")
    connection.execute("DROP TABLE _marketplace_snapshots_before_product_updates")
    connection.execute("DROP TABLE _marketplace_operations_before_product_updates")
    connection.execute("PRAGMA legacy_alter_table=OFF")
    _indexes(connection)
    return True


def apply_migration(connection, *, verbose=True):
    baseline_violations = foreign_key_snapshot(connection)
    before = _objects(connection)
    changed = _expand(connection)
    if "'product_update'" not in _table_sql(connection, "marketplace_operations"):
        raise sqlite3.OperationalError("Product update operation contract is missing")
    if "'product_update'" not in _table_sql(connection, "marketplace_listing_snapshots"):
        raise sqlite3.OperationalError("Product update snapshot contract is missing")
    assert_foreign_key_safety(
        connection,
        baseline=baseline_violations,
        managed_tables=MANAGED_TABLES,
        label="Marketplace product update migration",
    )
    if verbose:
        print("Marketplace product update migration completed successfully!")
    return int(changed) + len(_objects(connection) - before)


def migrate(db_path):
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        apply_migration(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    database_path = sys.argv[1] if len(sys.argv) > 1 else "data/seller_platform.db"
    migrate(database_path)
