#!/usr/bin/env python3
"""Add non-destructive freshness/version metadata for marketplace references.

The migration is intentionally SQLite-only, idempotent, and does not import the
Flask application (which would otherwise start scheduler side effects).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


TABLE_COLUMNS = {
    "marketplaces": [
        ("categories_sync_error", "TEXT"),
        ("categories_version", "INTEGER NOT NULL DEFAULT 0"),
        ("directories_sync_status", "VARCHAR(50)"),
        ("directories_sync_error", "TEXT"),
        ("directories_version", "INTEGER NOT NULL DEFAULT 0"),
        ("brands_synced_at", "DATETIME"),
        ("brands_sync_status", "VARCHAR(50)"),
        ("brands_sync_error", "TEXT"),
        ("brands_version", "INTEGER NOT NULL DEFAULT 0"),
        ("brands_sync_checkpoint", "TEXT"),
    ],
    "marketplace_categories": [
        ("is_available", "BOOLEAN NOT NULL DEFAULT 1"),
        ("last_seen_at", "DATETIME"),
        ("characteristics_sync_status", "VARCHAR(50)"),
        ("characteristics_sync_error", "TEXT"),
        ("characteristics_schema_hash", "VARCHAR(64)"),
        ("characteristics_version", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "marketplace_category_characteristics": [
        ("ai_instruction_source", "VARCHAR(20) NOT NULL DEFAULT 'legacy'"),
        ("is_available", "BOOLEAN NOT NULL DEFAULT 1"),
        ("last_seen_at", "DATETIME"),
    ],
    "marketplace_directories": [
        ("sync_status", "VARCHAR(50)"),
        ("sync_error", "TEXT"),
        ("data_hash", "VARCHAR(64)"),
        ("version", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "marketplace_brands": [
        ("is_available", "BOOLEAN NOT NULL DEFAULT 0"),
        ("last_seen_at", "DATETIME"),
    ],
}


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _columns(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})")}


def apply_migration(connection: sqlite3.Connection, *, verbose: bool = True) -> int:
    """Apply the migration to an open connection and return added column count."""
    cursor = connection.cursor()
    added = 0
    brand_availability_added = False

    for table_name, definitions in TABLE_COLUMNS.items():
        if not _table_exists(cursor, table_name):
            if verbose:
                print(f"  -- {table_name}: table does not exist, skipped")
            continue
        existing = _columns(cursor, table_name)
        for column_name, column_type in definitions:
            if column_name in existing:
                continue
            cursor.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            )
            existing.add(column_name)
            added += 1
            if table_name == "marketplace_brands" and column_name == "is_available":
                brand_availability_added = True
            if verbose:
                print(f"  ++ {table_name}.{column_name}")

    # Existing rows remain usable, but are distinguishable from a new verified
    # upstream snapshot by their version/hash fields. Nothing is deleted.
    if _table_exists(cursor, "marketplaces"):
        cursor.execute(
            """
            UPDATE marketplaces
            SET categories_version = CASE
                    WHEN COALESCE(categories_version, 0) = 0
                         AND COALESCE(total_categories, 0) > 0 THEN 1
                    ELSE COALESCE(categories_version, 0)
                END,
                directories_sync_status = COALESCE(
                    directories_sync_status,
                    CASE WHEN directories_synced_at IS NOT NULL THEN 'success' END
                ),
                directories_version = CASE
                    WHEN COALESCE(directories_version, 0) = 0
                         AND directories_synced_at IS NOT NULL THEN 1
                    ELSE COALESCE(directories_version, 0)
                END,
                brands_version = COALESCE(brands_version, 0)
            """
        )

    if _table_exists(cursor, "marketplace_categories"):
        cursor.execute(
            """
            UPDATE marketplace_categories
            SET is_available = COALESCE(is_available, 1),
                last_seen_at = COALESCE(last_seen_at, updated_at, created_at),
                characteristics_sync_status = COALESCE(
                    characteristics_sync_status,
                    CASE WHEN characteristics_synced_at IS NOT NULL THEN 'success' END
                ),
                characteristics_version = CASE
                    WHEN COALESCE(characteristics_version, 0) = 0
                         AND characteristics_synced_at IS NOT NULL THEN 1
                    ELSE COALESCE(characteristics_version, 0)
                END
            """
        )

    if _table_exists(cursor, "marketplace_category_characteristics"):
        cursor.execute(
            """
            UPDATE marketplace_category_characteristics
            SET is_available = COALESCE(is_available, 1),
                last_seen_at = COALESCE(last_seen_at, updated_at, created_at),
                ai_instruction_source = CASE
                    WHEN ai_instruction_source IS NULL OR ai_instruction_source = ''
                    THEN CASE WHEN ai_instruction IS NULL OR ai_instruction = ''
                              THEN 'generated' ELSE 'legacy' END
                    ELSE ai_instruction_source
                END
            """
        )

    if _table_exists(cursor, "marketplace_directories"):
        cursor.execute(
            """
            UPDATE marketplace_directories
            SET sync_status = COALESCE(
                    sync_status,
                    CASE WHEN synced_at IS NOT NULL THEN 'success' END
                ),
                version = CASE
                    WHEN COALESCE(version, 0) = 0 AND synced_at IS NOT NULL THEN 1
                    ELSE COALESCE(version, 0)
                END
            """
        )
        if "directory_type" in _columns(cursor, "marketplace_directories"):
            cursor.execute(
                """
                UPDATE marketplace_directories
                SET sync_status = 'unsupported_global_scope',
                    sync_error = 'WB TNVED requires a typed subjectID'
                WHERE lower(directory_type) = 'tnved'
                """
            )

    if _table_exists(cursor, "marketplace_brands"):
        brand_columns = _columns(cursor, "marketplace_brands")
        if "status" in brand_columns:
            verified_value = "1" if brand_availability_added else "COALESCE(is_available, 0)"
            cursor.execute(
                f"""
                UPDATE marketplace_brands
                SET is_available = CASE
                        WHEN LOWER(COALESCE(status, '')) = 'verified'
                        THEN {verified_value}
                        ELSE 0
                    END,
                    last_seen_at = COALESCE(last_seen_at, verified_at, updated_at, created_at)
                """
            )
        else:
            cursor.execute(
                """
                UPDATE marketplace_brands
                SET is_available = 0,
                    last_seen_at = COALESCE(last_seen_at, verified_at, updated_at, created_at)
                """
            )

    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_mp_category_available ON marketplace_categories(marketplace_id, is_available)",
        "CREATE INDEX IF NOT EXISTS idx_mp_category_schema_sync ON marketplace_categories(marketplace_id, is_enabled, is_available, characteristics_synced_at)",
        "CREATE INDEX IF NOT EXISTS idx_mp_charc_available ON marketplace_category_characteristics(category_id, is_available)",
        "CREATE INDEX IF NOT EXISTS idx_mp_brand_available ON marketplace_brands(marketplace_id, is_available)",
    ):
        table_name = statement.split(" ON ", 1)[1].split("(", 1)[0]
        if _table_exists(cursor, table_name):
            cursor.execute(statement)

    return added


def _find_database() -> str | None:
    candidates = [
        os.environ.get("DATABASE_PATH"),
        "/app/data/seller_platform.db",
        "data/seller_platform.db",
        "seller_platform.db",
    ]
    return next((path for path in candidates if path and Path(path).exists()), None)


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else _find_database()
    if not db_path or not Path(db_path).exists():
        print("Marketplace reference freshness migration: database not found")
        return 1

    connection = sqlite3.connect(db_path)
    try:
        added = apply_migration(connection)
        connection.commit()
        print(f"Marketplace reference freshness migration complete; added={added}")
        return 0
    except Exception as exc:
        connection.rollback()
        print(f"Marketplace reference freshness migration failed: {exc}")
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
