#!/usr/bin/env python3
"""Add durable automatic-publish health state to social accounts."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

try:
    from _foreign_key_safety import (
        assert_foreign_key_safety,
        foreign_key_snapshot,
    )
except ImportError:
    from migrations._foreign_key_safety import (
        assert_foreign_key_safety,
        foreign_key_snapshot,
    )


TABLE_NAME = "social_accounts"
MANAGED_TABLES = frozenset({TABLE_NAME})
COLUMNS = (
    ("last_error_code", "VARCHAR(80)"),
    ("last_error_at", "DATETIME"),
    ("automatic_publish_blocked_at", "DATETIME"),
)
INDEX_NAME = "idx_social_account_auto_publish_health"


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def apply_migration(connection: sqlite3.Connection, *, verbose: bool = True) -> int:
    cursor = connection.cursor()
    if not _table_exists(cursor, TABLE_NAME):
        if verbose:
            print(f"  -- {TABLE_NAME}: table does not exist, skipped")
        return 0

    baseline_violations = foreign_key_snapshot(connection)
    existing = {
        row[1] for row in cursor.execute(f"PRAGMA table_info({TABLE_NAME})")
    }
    added = 0
    for column_name, column_type in COLUMNS:
        if column_name in existing:
            continue
        cursor.execute(
            f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column_name} {column_type}"
        )
        existing.add(column_name)
        added += 1
        if verbose:
            print(f"  ++ {TABLE_NAME}.{column_name}")

    cursor.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
        f"ON {TABLE_NAME}(is_active, automatic_publish_blocked_at)"
    )
    assert_foreign_key_safety(
        connection,
        baseline=baseline_violations,
        managed_tables=MANAGED_TABLES,
        label="Social account publish health migration",
    )
    return added


def migrate(database_path: str) -> int:
    connection = sqlite3.connect(database_path)
    try:
        added = apply_migration(connection)
        connection.commit()
        return added
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _find_database() -> str | None:
    candidates = [
        os.environ.get("DATABASE_PATH"),
        "/app/data/seller_platform.db",
        "data/seller_platform.db",
        "seller_platform.db",
    ]
    return next((path for path in candidates if path and Path(path).exists()), None)


def main() -> int:
    database_path = sys.argv[1] if len(sys.argv) > 1 else _find_database()
    if not database_path or not Path(database_path).exists():
        print("Social account publish health migration: database not found")
        return 1
    try:
        added = migrate(database_path)
    except Exception as exc:
        print(f"Social account publish health migration failed: {exc}")
        return 1
    print(f"Social account publish health migration complete; added={added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
