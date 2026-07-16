#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add Ozon warehouses and reviewed price/stock mutation persistence."""

import os
import sqlite3
import sys


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


def _table_sql(connection: sqlite3.Connection, table_name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return str(row[0] or "") if row else ""


def _require_prerequisites(connection: sqlite3.Connection) -> None:
    required = {
        "users",
        "sellers",
        "marketplaces",
        "seller_marketplace_accounts",
        "marketplace_product_drafts",
        "marketplace_listings",
        "marketplace_operations",
        "marketplace_listing_snapshots",
    }
    missing = sorted(required - _schema_objects(connection))
    if missing:
        raise sqlite3.OperationalError(
            "Marketplace commercial migration prerequisites are missing: "
            + ", ".join(missing)
        )


def _create_operations(connection: sqlite3.Connection) -> None:
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


def _create_snapshots(connection: sqlite3.Connection) -> None:
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
                snapshot_kind IN ('product_import','price','stock')
            ),
            CONSTRAINT ck_marketplace_listing_snapshot_rollback_status CHECK (
                rollback_status IN (
                    'not_requested','unavailable','available','pending',
                    'succeeded','failed','conflict'
                )
            )
        )
    ''')


def _operation_indexes(connection: sqlite3.Connection) -> None:
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
        "ON marketplace_operations(draft_id) WHERE draft_id IS NOT NULL "
        "AND status IN ('queued','submitting','submitted','polling','uncertain')",
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


def _expand_operation_contracts(connection: sqlite3.Connection) -> bool:
    operation_sql = _table_sql(connection, "marketplace_operations")
    snapshot_sql = _table_sql(connection, "marketplace_listing_snapshots")
    if "'price_update'" in operation_sql and "'price'" in snapshot_sql:
        _operation_indexes(connection)
        return False
    leftovers = {
        "_marketplace_operations_before_commercial",
        "_marketplace_snapshots_before_commercial",
    }.intersection(_schema_objects(connection))
    if leftovers:
        raise sqlite3.OperationalError(
            "Incomplete marketplace commercial migration requires manual recovery"
        )
    if _columns(connection, "marketplace_operations") != set(OPERATION_COLUMNS):
        raise sqlite3.OperationalError(
            "Marketplace operation columns differ from the expected P5a schema"
        )
    if _columns(connection, "marketplace_listing_snapshots") != set(SNAPSHOT_COLUMNS):
        raise sqlite3.OperationalError(
            "Marketplace snapshot columns differ from the expected P5a schema"
        )

    # ``db.create_all()`` runs before file migrations in the Docker entrypoint
    # and can therefore create the new child table while the existing parent
    # still has the P5a CHECK constraint. It cannot contain a valid commercial
    # operation yet. Drop only that provably empty table so SQLite does not
    # rewrite its FK to the temporary parent during ALTER TABLE RENAME.
    if "marketplace_commercial_proposals" in _schema_objects(connection):
        proposal_count = connection.execute(
            "SELECT COUNT(*) FROM marketplace_commercial_proposals"
        ).fetchone()[0]
        if proposal_count:
            raise sqlite3.OperationalError(
                "Commercial proposals exist before operation contract expansion"
            )
        connection.execute("DROP TABLE marketplace_commercial_proposals")

    operation_count = connection.execute(
        "SELECT COUNT(*) FROM marketplace_operations"
    ).fetchone()[0]
    snapshot_count = connection.execute(
        "SELECT COUNT(*) FROM marketplace_listing_snapshots"
    ).fetchone()[0]
    connection.execute(
        "ALTER TABLE marketplace_listing_snapshots "
        "RENAME TO _marketplace_snapshots_before_commercial"
    )
    connection.execute(
        "ALTER TABLE marketplace_operations "
        "RENAME TO _marketplace_operations_before_commercial"
    )
    _create_operations(connection)
    operation_columns = ", ".join(OPERATION_COLUMNS)
    connection.execute(
        f"INSERT INTO marketplace_operations ({operation_columns}) "
        f"SELECT {operation_columns} FROM _marketplace_operations_before_commercial"
    )
    _create_snapshots(connection)
    snapshot_columns = ", ".join(SNAPSHOT_COLUMNS)
    connection.execute(
        f"INSERT INTO marketplace_listing_snapshots ({snapshot_columns}) "
        f"SELECT {snapshot_columns} FROM _marketplace_snapshots_before_commercial"
    )
    if connection.execute(
        "SELECT COUNT(*) FROM marketplace_operations"
    ).fetchone()[0] != operation_count:
        raise sqlite3.OperationalError("Marketplace operation rebuild lost rows")
    if connection.execute(
        "SELECT COUNT(*) FROM marketplace_listing_snapshots"
    ).fetchone()[0] != snapshot_count:
        raise sqlite3.OperationalError("Marketplace snapshot rebuild lost rows")
    connection.execute("DROP TABLE _marketplace_snapshots_before_commercial")
    connection.execute("DROP TABLE _marketplace_operations_before_commercial")
    _operation_indexes(connection)
    return True


def _create_commercial_tables(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_warehouse_syncs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            page_count INTEGER NOT NULL DEFAULT 0,
            seen_count INTEGER NOT NULL DEFAULT 0,
            created_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            unavailable_count INTEGER NOT NULL DEFAULT 0,
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_warehouse_sync_status CHECK (
                status IN ('running','completed','failed')
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_warehouses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            external_warehouse_id VARCHAR(100) NOT NULL,
            name VARCHAR(500) NOT NULL,
            status VARCHAR(100),
            warehouse_type VARCHAR(100),
            carriage_label_type VARCHAR(100),
            flags_json TEXT NOT NULL DEFAULT '{}',
            limits_json TEXT NOT NULL DEFAULT '{}',
            is_available BOOLEAN NOT NULL DEFAULT 1,
            sync_fingerprint VARCHAR(64) NOT NULL,
            last_seen_at DATETIME NOT NULL,
            last_synced_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_warehouse_account_external UNIQUE (
                account_id, external_warehouse_id
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_warehouse_stocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            listing_id INTEGER NOT NULL REFERENCES marketplace_listings(id) ON DELETE CASCADE,
            warehouse_id INTEGER NOT NULL REFERENCES marketplace_warehouses(id) ON DELETE CASCADE,
            offer_id VARCHAR(200) NOT NULL,
            external_product_id VARCHAR(100) NOT NULL,
            sku VARCHAR(100) NOT NULL,
            present INTEGER NOT NULL,
            reserved INTEGER NOT NULL,
            free_stock INTEGER NOT NULL,
            is_available BOOLEAN NOT NULL DEFAULT 1,
            sync_fingerprint VARCHAR(64) NOT NULL,
            observed_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_stock_listing_warehouse UNIQUE (
                account_id, listing_id, warehouse_id
            ),
            CONSTRAINT ck_marketplace_warehouse_stock_nonnegative CHECK (
                present >= 0 AND reserved >= 0 AND free_stock >= 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_commercial_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            listing_id INTEGER NOT NULL REFERENCES marketplace_listings(id) ON DELETE CASCADE,
            warehouse_id INTEGER REFERENCES marketplace_warehouses(id) ON DELETE SET NULL,
            operation_id INTEGER UNIQUE REFERENCES marketplace_operations(id) ON DELETE SET NULL,
            rollback_of_operation_id INTEGER REFERENCES marketplace_operations(id) ON DELETE SET NULL,
            agent_task_id VARCHAR(36) REFERENCES agent_tasks(id) ON DELETE SET NULL,
            created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reviewed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            proposal_kind VARCHAR(20) NOT NULL,
            source VARCHAR(20) NOT NULL DEFAULT 'user',
            status VARCHAR(30) NOT NULL DEFAULT 'pending_review',
            idempotency_key VARCHAR(128) NOT NULL,
            request_fingerprint VARCHAR(64) NOT NULL,
            contract_version VARCHAR(80) NOT NULL,
            baseline_fingerprint VARCHAR(64) NOT NULL,
            proposed_fingerprint VARCHAR(64) NOT NULL,
            baseline_state_json TEXT NOT NULL,
            proposed_state_json TEXT NOT NULL,
            guardrails_json TEXT NOT NULL DEFAULT '{}',
            review_note VARCHAR(1000),
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            version INTEGER NOT NULL DEFAULT 1,
            reviewed_at DATETIME,
            applied_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_commercial_proposal_idempotency UNIQUE (
                account_id, proposal_kind, idempotency_key
            ),
            CONSTRAINT ck_marketplace_commercial_proposal_kind CHECK (
                proposal_kind IN ('price','stock')
            ),
            CONSTRAINT ck_marketplace_commercial_proposal_source CHECK (
                source IN ('user','agent','system','rollback')
            ),
            CONSTRAINT ck_marketplace_commercial_proposal_status CHECK (
                status IN (
                    'pending_review','approved','rejected','applying','applied',
                    'failed','conflict','uncertain','cancelled'
                )
            ),
            CONSTRAINT ck_marketplace_commercial_proposal_scope CHECK (
                (proposal_kind = 'price' AND warehouse_id IS NULL) OR
                (proposal_kind = 'stock' AND warehouse_id IS NOT NULL)
            )
        )
    ''')

    statements = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_warehouse_sync_running "
        "ON marketplace_warehouse_syncs(account_id) WHERE status='running'",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_warehouse_sync_scope "
        "ON marketplace_warehouse_syncs(seller_id, account_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_warehouse_scope "
        "ON marketplace_warehouses(seller_id, account_id, is_available)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_stock_scope "
        "ON marketplace_warehouse_stocks("
        "seller_id, account_id, listing_id, is_available)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_commercial_proposal_scope "
        "ON marketplace_commercial_proposals("
        "seller_id, account_id, status, created_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_commercial_proposal_active "
        "ON marketplace_commercial_proposals("
        "account_id, listing_id, proposal_kind, COALESCE(warehouse_id, 0)) "
        "WHERE status IN ('pending_review','approved','applying','uncertain')",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouse_syncs_seller_id "
        "ON marketplace_warehouse_syncs(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouse_syncs_marketplace_id "
        "ON marketplace_warehouse_syncs(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouse_syncs_account_id "
        "ON marketplace_warehouse_syncs(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouses_seller_id "
        "ON marketplace_warehouses(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouses_marketplace_id "
        "ON marketplace_warehouses(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouses_account_id "
        "ON marketplace_warehouses(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouse_stocks_seller_id "
        "ON marketplace_warehouse_stocks(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouse_stocks_marketplace_id "
        "ON marketplace_warehouse_stocks(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouse_stocks_account_id "
        "ON marketplace_warehouse_stocks(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouse_stocks_listing_id "
        "ON marketplace_warehouse_stocks(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_warehouse_stocks_warehouse_id "
        "ON marketplace_warehouse_stocks(warehouse_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_commercial_proposals_seller_id "
        "ON marketplace_commercial_proposals(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_commercial_proposals_marketplace_id "
        "ON marketplace_commercial_proposals(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_commercial_proposals_account_id "
        "ON marketplace_commercial_proposals(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_commercial_proposals_listing_id "
        "ON marketplace_commercial_proposals(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_commercial_proposals_warehouse_id "
        "ON marketplace_commercial_proposals(warehouse_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_commercial_proposals_operation_id "
        "ON marketplace_commercial_proposals(operation_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_commercial_proposals_rollback_operation_id "
        "ON marketplace_commercial_proposals(rollback_of_operation_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_commercial_proposals_agent_task_id "
        "ON marketplace_commercial_proposals(agent_task_id)",
    )
    for statement in statements:
        connection.execute(statement)


def _verify_schema(connection: sqlite3.Connection) -> None:
    expected = {
        "marketplace_warehouse_syncs": {
            "seller_id", "account_id", "status", "page_count", "seen_count",
        },
        "marketplace_warehouses": {
            "seller_id", "account_id", "external_warehouse_id",
            "sync_fingerprint", "is_available",
        },
        "marketplace_warehouse_stocks": {
            "seller_id", "account_id", "listing_id", "warehouse_id",
            "free_stock", "sync_fingerprint",
        },
        "marketplace_commercial_proposals": {
            "seller_id", "account_id", "listing_id", "proposal_kind",
            "status", "baseline_state_json", "proposed_state_json",
            "operation_id", "version",
        },
    }
    for table_name, columns in expected.items():
        missing = columns - _columns(connection, table_name)
        if missing:
            raise sqlite3.OperationalError(
                f"{table_name} is missing columns: {', '.join(sorted(missing))}"
            )
    if "'price_update'" not in _table_sql(connection, "marketplace_operations"):
        raise sqlite3.OperationalError("Marketplace operation contract was not expanded")
    if "'price'" not in _table_sql(connection, "marketplace_listing_snapshots"):
        raise sqlite3.OperationalError("Marketplace snapshot contract was not expanded")


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    before = _schema_objects(connection)
    _require_prerequisites(connection)
    contract_expanded = _expand_operation_contracts(connection)
    _create_commercial_tables(connection)
    _verify_schema(connection)
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            "Marketplace commercial migration produced foreign-key violations"
        )
    after = _schema_objects(connection)
    if verbose:
        print("Marketplace commercial schema migration completed successfully!")
    return max(len(after - before), int(contract_expanded))


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
