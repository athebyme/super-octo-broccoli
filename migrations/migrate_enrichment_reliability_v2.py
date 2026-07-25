#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add restart-safe WB enrichment dispatch and reconciliation state.

The migration is intentionally idempotent and fail-fast: the ORM selects these
columns on normal page loads, so a partially migrated database must not start.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "seller_platform.db"


def _tables(cursor):
    return {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(cursor, table):
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}


def _add_columns(cursor, table, definitions):
    existing = _columns(cursor, table)
    for name, sql_type in definitions:
        if name in existing:
            continue
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        logger.info("Добавлена %s.%s", table, name)


def migrate(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        tables = _tables(cursor)

        if "card_edit_history" in tables:
            _add_columns(cursor, "card_edit_history", (
                ("wb_reconcile_due_at", "DATETIME"),
                ("wb_reconcile_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("wb_reconciled_at", "DATETIME"),
                ("wb_reconcile_code", "VARCHAR(64)"),
            ))
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS ix_card_edit_history_wb_reconcile_due_at "
                "ON card_edit_history(wb_reconcile_due_at)"
            )

        if "enrichment_jobs" in tables:
            _add_columns(cursor, "enrichment_jobs", (
                ("confirmed", "INTEGER DEFAULT 0"),
                ("conflicted", "INTEGER DEFAULT 0"),
                ("product_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("bulk_edit_id", "INTEGER REFERENCES bulk_edit_history(id)"),
                ("claim_token", "VARCHAR(64)"),
                ("claim_expires_at", "DATETIME"),
                ("heartbeat_at", "DATETIME"),
                ("current_product_id", "INTEGER"),
                ("current_item_started_at", "DATETIME"),
                ("last_error", "TEXT"),
            ))
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS ix_enrichment_jobs_bulk_edit_id "
                "ON enrichment_jobs(bulk_edit_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS ix_enrichment_jobs_claim_expires_at "
                "ON enrichment_jobs(claim_expires_at)"
            )

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH
    if not target.exists():
        logger.error("База не найдена: %s", target)
        sys.exit(1)
    sys.exit(0 if migrate(target) else 1)
