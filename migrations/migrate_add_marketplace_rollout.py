#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add durable bounded WB projection/parity rollout runs.

This migration is additive and repeatable.  It intentionally does not scan or
rewrite ``products``; the runtime worker owns all large-catalog data movement.
"""

import os
import sqlite3
import sys


def _objects(connection: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }


def _tables(connection: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(connection: sqlite3.Connection, table_name: str) -> set:
    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    prerequisites = {
        "sellers",
        "marketplaces",
        "products",
        "marketplace_listings",
    }
    missing = sorted(prerequisites - _tables(connection))
    if missing:
        raise sqlite3.OperationalError(
            "Marketplace rollout prerequisites are missing: "
            + ", ".join(missing)
        )

    before = _objects(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_projection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL
                REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            run_kind VARCHAR(30) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            cursor_product_id INTEGER NOT NULL DEFAULT 0,
            target_product_id INTEGER NOT NULL DEFAULT 0,
            scanned_count INTEGER NOT NULL DEFAULT 0,
            inserted_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            unchanged_count INTEGER NOT NULL DEFAULT 0,
            matched_count INTEGER NOT NULL DEFAULT 0,
            missing_count INTEGER NOT NULL DEFAULT 0,
            mismatched_count INTEGER NOT NULL DEFAULT 0,
            mismatch_fields_json TEXT NOT NULL DEFAULT '{}',
            mismatch_sample_json TEXT NOT NULL DEFAULT '[]',
            lease_owner VARCHAR(64),
            lease_expires_at DATETIME,
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            heartbeat_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_projection_run_kind CHECK (
                run_kind IN ('wb_backfill','wb_parity')
            ),
            CONSTRAINT ck_marketplace_projection_run_status CHECK (
                status IN ('pending','running','paused','completed','failed')
            ),
            CONSTRAINT ck_marketplace_projection_run_cursor CHECK (
                cursor_product_id >= 0 AND target_product_id >= 0
            ),
            CONSTRAINT ck_marketplace_projection_run_counts CHECK (
                scanned_count >= 0 AND inserted_count >= 0
                AND updated_count >= 0 AND unchanged_count >= 0
                AND matched_count >= 0 AND missing_count >= 0
                AND mismatched_count >= 0
            )
        )
    ''')
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_projection_runs_seller_id "
        "ON marketplace_projection_runs(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_projection_runs_marketplace_id "
        "ON marketplace_projection_runs(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_projection_run_scope "
        "ON marketplace_projection_runs("
        "seller_id, marketplace_id, run_kind, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_projection_run_status "
        "ON marketplace_projection_runs(status, heartbeat_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_marketplace_projection_run_active "
        "ON marketplace_projection_runs(seller_id, marketplace_id, run_kind) "
        "WHERE status IN ('pending','running','paused')",
    ):
        connection.execute(statement)

    required = {
        "id", "seller_id", "marketplace_id", "run_kind", "status",
        "cursor_product_id", "target_product_id", "scanned_count",
        "inserted_count", "updated_count", "unchanged_count",
        "matched_count", "missing_count", "mismatched_count",
        "mismatch_fields_json", "mismatch_sample_json", "lease_owner",
        "lease_expires_at", "error_code", "error_message", "started_at",
        "heartbeat_at", "completed_at", "created_at", "updated_at",
    }
    missing = sorted(required - _columns(
        connection,
        "marketplace_projection_runs",
    ))
    if missing:
        raise sqlite3.OperationalError(
            "marketplace_projection_runs is incomplete: "
            + ", ".join(missing)
        )

    after = _objects(connection)
    if verbose:
        print("Marketplace rollout migration completed successfully")
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
