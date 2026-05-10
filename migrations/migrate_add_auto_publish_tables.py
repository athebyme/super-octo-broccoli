#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Create auto-publish tables and indexes.

The app can create tables via db.create_all() in some startup paths, but
production SQLite databases also use standalone migrations. Keep this script
idempotent so it is safe to run repeatedly.
"""
import os
import sqlite3
import sys
from pathlib import Path


def _db_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("sqlite:///"):
        return db_url.replace("sqlite:///", "")
    root = Path(__file__).resolve().parent.parent
    for candidate in (root / "data" / "seller_platform.db", root / "seller_platform.db"):
        if candidate.exists():
            return str(candidate)
    return str(root / "data" / "seller_platform.db")


def _columns(cursor, table_name: str) -> set:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def _add_column_if_missing(cursor, table: str, column: str, ddl: str) -> None:
    if column not in _columns(cursor, table):
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        print(f"Added {table}.{column}")


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS auto_publish_settings (
                id INTEGER PRIMARY KEY,
                seller_id INTEGER NOT NULL UNIQUE,
                is_enabled BOOLEAN NOT NULL DEFAULT 0,
                marketplace_code VARCHAR(50) NOT NULL DEFAULT 'wb',
                check_interval_minutes INTEGER NOT NULL DEFAULT 30,
                last_run_at DATETIME,
                next_run_at DATETIME,
                batch_size INTEGER NOT NULL DEFAULT 10,
                max_daily_publishes INTEGER NOT NULL DEFAULT 100,
                daily_published_count INTEGER NOT NULL DEFAULT 0,
                daily_count_reset_at DATETIME,
                validation_mode VARCHAR(20) NOT NULL DEFAULT 'strict',
                max_retries_per_product INTEGER NOT NULL DEFAULT 3,
                retry_delay_minutes INTEGER NOT NULL DEFAULT 60,
                failure_threshold INTEGER NOT NULL DEFAULT 5,
                is_paused BOOLEAN NOT NULL DEFAULT 0,
                paused_reason TEXT,
                paused_at DATETIME,
                supplier_ids_json TEXT,
                notify_on_success BOOLEAN NOT NULL DEFAULT 0,
                notify_on_failure BOOLEAN NOT NULL DEFAULT 1,
                notify_on_pause BOOLEAN NOT NULL DEFAULT 1,
                run_lock_token VARCHAR(64),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME,
                FOREIGN KEY(seller_id) REFERENCES sellers(id)
            )
        """)

        for column, ddl in (
            ("run_lock_token", "VARCHAR(64)"),
            ("daily_count_reset_at", "DATETIME"),
            ("notify_on_success", "BOOLEAN NOT NULL DEFAULT 0"),
            ("notify_on_failure", "BOOLEAN NOT NULL DEFAULT 1"),
            ("notify_on_pause", "BOOLEAN NOT NULL DEFAULT 1"),
        ):
            _add_column_if_missing(cursor, "auto_publish_settings", column, ddl)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS auto_publish_runs (
                id INTEGER PRIMARY KEY,
                seller_id INTEGER NOT NULL,
                run_uid VARCHAR(36) NOT NULL UNIQUE,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                triggered_by VARCHAR(20) DEFAULT 'scheduler',
                started_at DATETIME,
                completed_at DATETIME,
                duration_seconds REAL,
                total_candidates INTEGER DEFAULT 0,
                total_validated INTEGER DEFAULT 0,
                total_published INTEGER DEFAULT 0,
                total_failed INTEGER DEFAULT 0,
                total_skipped INTEGER DEFAULT 0,
                error_summary TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(seller_id) REFERENCES sellers(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS auto_publish_items (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                imported_product_id INTEGER NOT NULL,
                seller_id INTEGER NOT NULL,
                step VARCHAR(30) DEFAULT 'queued',
                status VARCHAR(20) DEFAULT 'pending',
                wb_nm_id INTEGER,
                product_id INTEGER,
                error_message TEXT,
                error_step VARCHAR(30),
                error_history_json TEXT DEFAULT '[]',
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at DATETIME,
                validation_result_json TEXT,
                started_at DATETIME,
                completed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES auto_publish_runs(id),
                FOREIGN KEY(imported_product_id) REFERENCES imported_products(id),
                FOREIGN KEY(seller_id) REFERENCES sellers(id),
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
        """)

        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_aps_seller ON auto_publish_settings(seller_id)",
            "CREATE INDEX IF NOT EXISTS idx_apr_seller_status ON auto_publish_runs(seller_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_apr_created ON auto_publish_runs(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_api_run_status ON auto_publish_items(run_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_api_seller_status ON auto_publish_items(seller_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_api_retry ON auto_publish_items(status, next_retry_at)",
            "CREATE INDEX IF NOT EXISTS idx_api_imported_product ON auto_publish_items(imported_product_id)",
        ]
        for ddl in indexes:
            cursor.execute(ddl)

        conn.commit()
        print("Auto-publish tables are ready")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate(_db_path())
