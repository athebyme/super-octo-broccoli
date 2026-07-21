#!/usr/bin/env python3
"""Add exact marketplace brand identity to category-scoped brand links.

The migration is SQLite-only, idempotent, and deliberately does not backfill
existing rows: a legacy MarketplaceBrand ID is not proof of the provider ID in
every category. A fresh category sweep or exact live validation fills the value.
"""

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


COLUMN_NAME = "marketplace_external_brand_id"
INDEX_NAME = "idx_bcl_category_external_brand_id"
MANAGED_TABLES = frozenset({"brand_category_links"})


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def apply_migration(connection: sqlite3.Connection, *, verbose: bool = True) -> int:
    """Apply the migration to an open connection and return added columns."""
    cursor = connection.cursor()
    if not _table_exists(cursor, "brand_category_links"):
        if verbose:
            print("  -- brand_category_links: table does not exist, skipped")
        return 0

    baseline_violations = foreign_key_snapshot(connection)
    columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(brand_category_links)")
    }
    added = 0
    if COLUMN_NAME not in columns:
        cursor.execute(
            "ALTER TABLE brand_category_links "
            f"ADD COLUMN {COLUMN_NAME} INTEGER"
        )
        added = 1
        if verbose:
            print(f"  ++ brand_category_links.{COLUMN_NAME}")

    cursor.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
        f"ON brand_category_links(category_id, {COLUMN_NAME})"
    )
    assert_foreign_key_safety(
        connection,
        baseline=baseline_violations,
        managed_tables=MANAGED_TABLES,
        label="Brand category external ID migration",
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
        print("Brand category external ID migration: database not found")
        return 1
    try:
        added = migrate(database_path)
    except Exception as exc:
        print(f"Brand category external ID migration failed: {exc}")
        return 1
    print(f"Brand category external ID migration complete; added={added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
