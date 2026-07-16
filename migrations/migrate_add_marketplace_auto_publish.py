#!/usr/bin/env python3
"""Make auto-publish state marketplace/account scoped without losing WB history.

Legacy installations have exactly one ``auto_publish_settings`` row per seller
and WB-specific run/item rows.  P7 keeps those rows as the explicit ``wb``
scope and adds an independent scope for every Ozon operational account.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, Iterable


SETTINGS_COLUMNS = {
    "id", "seller_id", "account_id", "is_enabled", "marketplace_code",
    "check_interval_minutes", "last_run_at", "next_run_at", "batch_size",
    "max_daily_publishes", "daily_published_count", "daily_count_reset_at",
    "validation_mode", "max_retries_per_product", "retry_delay_minutes",
    "failure_threshold", "is_paused", "paused_reason", "paused_at",
    "supplier_ids_json", "notify_on_success", "notify_on_failure",
    "notify_on_pause", "run_lock_token", "created_at", "updated_at",
}
RUN_COLUMNS = {
    "id", "settings_id", "seller_id", "marketplace_code", "account_id",
    "run_uid", "status", "triggered_by", "started_at", "completed_at",
    "duration_seconds", "total_candidates", "total_validated",
    "total_published", "total_failed", "total_skipped", "total_deferred",
    "error_summary", "created_at",
}
ITEM_COLUMNS = {
    "id", "run_id", "imported_product_id", "seller_id",
    "marketplace_code", "account_id", "draft_id", "operation_id",
    "listing_id", "draft_version", "idempotency_key", "step", "status",
    "wb_nm_id", "product_id",
    "error_message", "error_step", "error_history_json", "retry_count",
    "next_retry_at", "validation_result_json", "started_at",
    "completed_at", "created_at",
}


def _db_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url.startswith("sqlite:///"):
        return database_url.replace("sqlite:///", "", 1)
    root = Path(__file__).resolve().parent.parent
    for candidate in (
        root / "data" / "seller_platform.db",
        root / "seller_platform.db",
    ):
        if candidate.exists():
            return str(candidate)
    return str(root / "data" / "seller_platform.db")


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    return cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    if not _table_exists(cursor, table):
        return set()
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}


def _rows(cursor: sqlite3.Cursor, table: str) -> list[Dict[str, Any]]:
    if not _table_exists(cursor, table):
        return []
    names = [row[1] for row in cursor.execute(f"PRAGMA table_info({table})")]
    return [dict(zip(names, row)) for row in cursor.execute(f"SELECT * FROM {table}")]


def _value(row: Dict[str, Any], key: str, default: Any = None) -> Any:
    value = row.get(key, default)
    return default if value is None and default is not None else value


def _legacy_seller_unique(cursor: sqlite3.Cursor) -> bool:
    if not _table_exists(cursor, "auto_publish_settings"):
        return False
    indexes = cursor.execute("PRAGMA index_list(auto_publish_settings)").fetchall()
    for index in indexes:
        # seq, name, unique, origin, partial
        if not index[2] or index[4]:
            continue
        fields = [
            item[2]
            for item in cursor.execute(f"PRAGMA index_info({index[1]})")
        ]
        if fields == ["seller_id"]:
            return True
    return False


def _create_schema(cursor: sqlite3.Cursor) -> None:
    cursor.execute("""
        CREATE TABLE auto_publish_settings (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            account_id INTEGER REFERENCES seller_marketplace_accounts(id),
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
            created_at DATETIME NOT NULL,
            updated_at DATETIME,
            CONSTRAINT ck_auto_publish_settings_scope CHECK (
                (marketplace_code = 'wb' AND account_id IS NULL) OR
                (marketplace_code = 'ozon' AND account_id IS NOT NULL)
            ),
            CONSTRAINT uq_auto_publish_settings_account UNIQUE(account_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE auto_publish_runs (
            id INTEGER PRIMARY KEY,
            settings_id INTEGER NOT NULL REFERENCES auto_publish_settings(id),
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            marketplace_code VARCHAR(50) NOT NULL DEFAULT 'wb',
            account_id INTEGER REFERENCES seller_marketplace_accounts(id),
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
            total_deferred INTEGER DEFAULT 0,
            error_summary TEXT,
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_auto_publish_run_scope CHECK (
                (marketplace_code = 'wb' AND account_id IS NULL) OR
                (marketplace_code = 'ozon' AND account_id IS NOT NULL)
            )
        )
    """)
    cursor.execute("""
        CREATE TABLE auto_publish_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES auto_publish_runs(id),
            imported_product_id INTEGER NOT NULL REFERENCES imported_products(id),
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            marketplace_code VARCHAR(50) NOT NULL DEFAULT 'wb',
            account_id INTEGER REFERENCES seller_marketplace_accounts(id),
            draft_id INTEGER REFERENCES marketplace_product_drafts(id),
            operation_id INTEGER REFERENCES marketplace_operations(id),
            listing_id INTEGER REFERENCES marketplace_listings(id),
            draft_version INTEGER,
            idempotency_key VARCHAR(128),
            step VARCHAR(30) DEFAULT 'queued',
            status VARCHAR(20) DEFAULT 'pending',
            wb_nm_id INTEGER,
            product_id INTEGER REFERENCES products(id),
            error_message TEXT,
            error_step VARCHAR(30),
            error_history_json TEXT DEFAULT '[]',
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at DATETIME,
            validation_result_json TEXT,
            started_at DATETIME,
            completed_at DATETIME,
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_auto_publish_item_scope CHECK (
                (marketplace_code = 'wb' AND account_id IS NULL) OR
                (marketplace_code = 'ozon' AND account_id IS NOT NULL)
            )
        )
    """)


def _create_indexes(cursor: sqlite3.Cursor) -> None:
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_aps_seller ON auto_publish_settings(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_auto_publish_settings_account_id ON auto_publish_settings(account_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_auto_publish_settings_wb_seller ON auto_publish_settings(seller_id) WHERE marketplace_code = 'wb' AND account_id IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_apr_seller_status ON auto_publish_runs(seller_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_apr_settings_status ON auto_publish_runs(settings_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_apr_account_status ON auto_publish_runs(account_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_apr_created ON auto_publish_runs(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_api_run_status ON auto_publish_items(run_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_api_seller_status ON auto_publish_items(seller_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_api_account_status ON auto_publish_items(account_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_api_retry ON auto_publish_items(status, next_retry_at)",
        "CREATE INDEX IF NOT EXISTS idx_api_imported_product ON auto_publish_items(imported_product_id)",
        "CREATE INDEX IF NOT EXISTS ix_auto_publish_items_draft_id ON auto_publish_items(draft_id)",
        "CREATE INDEX IF NOT EXISTS ix_auto_publish_items_listing_id ON auto_publish_items(listing_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_api_operation ON auto_publish_items(operation_id) WHERE operation_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_api_account_idempotency ON auto_publish_items(account_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
    )
    for statement in statements:
        cursor.execute(statement)


def _insert(cursor: sqlite3.Cursor, table: str, row: Dict[str, Any]) -> None:
    columns = list(row)
    placeholders = ",".join("?" for _ in columns)
    cursor.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
        tuple(row[column] for column in columns),
    )


def _migrate_rows(
    cursor: sqlite3.Cursor,
    settings_rows: Iterable[Dict[str, Any]],
    run_rows: Iterable[Dict[str, Any]],
    item_rows: Iterable[Dict[str, Any]],
) -> None:
    now = datetime.utcnow().isoformat()
    settings_by_seller: Dict[int, int] = {}
    used_setting_ids: set[int] = set()
    for source in settings_rows:
        seller_id = int(source["seller_id"])
        if seller_id in settings_by_seller:
            raise RuntimeError(
                f"Legacy auto-publish has duplicate seller scope {seller_id}"
            )
        setting_id = int(source["id"])
        settings_by_seller[seller_id] = setting_id
        used_setting_ids.add(setting_id)
        _insert(cursor, "auto_publish_settings", {
            "id": setting_id,
            "seller_id": seller_id,
            "account_id": None,
            "is_enabled": int(bool(_value(source, "is_enabled", 0))),
            "marketplace_code": "wb",
            "check_interval_minutes": _value(source, "check_interval_minutes", 30),
            "last_run_at": source.get("last_run_at"),
            "next_run_at": source.get("next_run_at"),
            "batch_size": _value(source, "batch_size", 10),
            "max_daily_publishes": _value(source, "max_daily_publishes", 100),
            "daily_published_count": _value(source, "daily_published_count", 0),
            "daily_count_reset_at": source.get("daily_count_reset_at"),
            "validation_mode": _value(source, "validation_mode", "strict"),
            "max_retries_per_product": _value(source, "max_retries_per_product", 3),
            "retry_delay_minutes": _value(source, "retry_delay_minutes", 60),
            "failure_threshold": _value(source, "failure_threshold", 5),
            "is_paused": int(bool(_value(source, "is_paused", 0))),
            "paused_reason": source.get("paused_reason"),
            "paused_at": source.get("paused_at"),
            "supplier_ids_json": source.get("supplier_ids_json"),
            "notify_on_success": int(bool(_value(source, "notify_on_success", 0))),
            "notify_on_failure": int(bool(_value(source, "notify_on_failure", 1))),
            "notify_on_pause": int(bool(_value(source, "notify_on_pause", 1))),
            "run_lock_token": None,
            "created_at": _value(source, "created_at", now),
            "updated_at": source.get("updated_at"),
        })

    seller_ids = {
        int(row["seller_id"])
        for row in run_rows
        if row.get("seller_id") is not None
    }
    missing_sellers = sorted(seller_ids - set(settings_by_seller))
    next_id = max(used_setting_ids, default=0) + 1
    for seller_id in missing_sellers:
        seller_exists = cursor.execute(
            "SELECT 1 FROM sellers WHERE id=?", (seller_id,)
        ).fetchone()
        if seller_exists is None:
            raise RuntimeError(
                f"Auto-publish run references missing seller {seller_id}"
            )
        settings_by_seller[seller_id] = next_id
        _insert(cursor, "auto_publish_settings", {
            "id": next_id,
            "seller_id": seller_id,
            "account_id": None,
            "is_enabled": 0,
            "marketplace_code": "wb",
            "check_interval_minutes": 30,
            "batch_size": 10,
            "max_daily_publishes": 100,
            "daily_published_count": 0,
            "validation_mode": "strict",
            "max_retries_per_product": 3,
            "retry_delay_minutes": 60,
            "failure_threshold": 5,
            "is_paused": 0,
            "notify_on_success": 0,
            "notify_on_failure": 1,
            "notify_on_pause": 1,
            "created_at": now,
        })
        next_id += 1

    run_scope: Dict[int, tuple[int, int]] = {}
    for source in run_rows:
        run_id = int(source["id"])
        seller_id = int(source["seller_id"])
        settings_id = settings_by_seller[seller_id]
        run_scope[run_id] = (seller_id, settings_id)
        _insert(cursor, "auto_publish_runs", {
            "id": run_id,
            "settings_id": settings_id,
            "seller_id": seller_id,
            "marketplace_code": "wb",
            "account_id": None,
            "run_uid": source["run_uid"],
            "status": _value(source, "status", "pending"),
            "triggered_by": _value(source, "triggered_by", "scheduler"),
            "started_at": source.get("started_at"),
            "completed_at": source.get("completed_at"),
            "duration_seconds": source.get("duration_seconds"),
            "total_candidates": _value(source, "total_candidates", 0),
            "total_validated": _value(source, "total_validated", 0),
            "total_published": _value(source, "total_published", 0),
            "total_failed": _value(source, "total_failed", 0),
            "total_skipped": _value(source, "total_skipped", 0),
            "total_deferred": _value(source, "total_deferred", 0),
            "error_summary": source.get("error_summary"),
            "created_at": _value(source, "created_at", now),
        })

    for source in item_rows:
        run_id = int(source["run_id"])
        if run_id not in run_scope:
            raise RuntimeError(
                f"Auto-publish item references missing run {run_id}"
            )
        seller_id, _ = run_scope[run_id]
        if int(source["seller_id"]) != seller_id:
            raise RuntimeError(
                f"Auto-publish item {source['id']} crosses seller scope"
            )
        _insert(cursor, "auto_publish_items", {
            "id": source["id"],
            "run_id": run_id,
            "imported_product_id": source["imported_product_id"],
            "seller_id": seller_id,
            "marketplace_code": "wb",
            "account_id": None,
            "draft_id": None,
            "operation_id": None,
            "listing_id": None,
            "draft_version": None,
            "idempotency_key": None,
            "step": _value(source, "step", "queued"),
            "status": _value(source, "status", "pending"),
            "wb_nm_id": source.get("wb_nm_id"),
            "product_id": source.get("product_id"),
            "error_message": source.get("error_message"),
            "error_step": source.get("error_step"),
            "error_history_json": _value(source, "error_history_json", "[]"),
            "retry_count": _value(source, "retry_count", 0),
            "next_retry_at": source.get("next_retry_at"),
            "validation_result_json": source.get("validation_result_json"),
            "started_at": source.get("started_at"),
            "completed_at": source.get("completed_at"),
            "created_at": _value(source, "created_at", now),
        })


def migrate(db_path: str) -> None:
    connection = sqlite3.connect(db_path)
    cursor = connection.cursor()
    try:
        complete = (
            SETTINGS_COLUMNS <= _columns(cursor, "auto_publish_settings")
            and RUN_COLUMNS <= _columns(cursor, "auto_publish_runs")
            and ITEM_COLUMNS <= _columns(cursor, "auto_publish_items")
            and not _legacy_seller_unique(cursor)
        )
        if complete:
            _create_indexes(cursor)
            connection.commit()
            print("Marketplace-scoped auto-publish schema is already current")
            return

        settings_rows = _rows(cursor, "auto_publish_settings")
        run_rows = _rows(cursor, "auto_publish_runs")
        item_rows = _rows(cursor, "auto_publish_items")
        for backup in (
            "auto_publish_items_p7_legacy",
            "auto_publish_runs_p7_legacy",
            "auto_publish_settings_p7_legacy",
        ):
            if _table_exists(cursor, backup):
                raise RuntimeError(
                    f"Found unfinished marketplace auto-publish migration table {backup}"
                )

        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for table in (
            "auto_publish_items",
            "auto_publish_runs",
            "auto_publish_settings",
        ):
            if _table_exists(cursor, table):
                cursor.execute(f"ALTER TABLE {table} RENAME TO {table}_p7_legacy")

        _create_schema(cursor)
        _migrate_rows(cursor, settings_rows, run_rows, item_rows)

        for table in (
            "auto_publish_items_p7_legacy",
            "auto_publish_runs_p7_legacy",
            "auto_publish_settings_p7_legacy",
        ):
            if _table_exists(cursor, table):
                cursor.execute(f"DROP TABLE {table}")
        _create_indexes(cursor)
        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                "Marketplace auto-publish migration left foreign-key violations"
            )
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        print(
            "Marketplace-scoped auto-publish schema is ready: "
            f"settings={len(settings_rows)}, runs={len(run_rows)}, "
            f"items={len(item_rows)}"
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    migrate(_db_path())
