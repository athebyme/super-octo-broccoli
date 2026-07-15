#!/usr/bin/env python3
"""Add provenance/freshness metadata for WB characteristic dictionaries.

SQLite-only, idempotent and safe for existing installations. Historical
non-empty dictionaries are treated as explicit admin policy because older
versions did not persist enough information to claim an upstream WB source.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path


TABLE = "marketplace_category_characteristics"
COLUMNS = (
    ("dictionary_source", "VARCHAR(30) NOT NULL DEFAULT 'none'"),
    ("dictionary_synced_at", "DATETIME"),
    ("dictionary_hash", "VARCHAR(64)"),
    ("dictionary_version", "INTEGER NOT NULL DEFAULT 0"),
    ("has_filter", "BOOLEAN DEFAULT 0"),
    ("is_variable", "BOOLEAN DEFAULT 0"),
)


def _table_exists(cursor: sqlite3.Cursor) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,),
    )
    return cursor.fetchone() is not None


def _columns(cursor: sqlite3.Cursor) -> set[str]:
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({TABLE})")}


def _dictionary_state(raw: object) -> tuple[bool, str | None]:
    """Return whether legacy JSON has values and its runtime-compatible hash."""
    text = str(raw or '').strip()
    if not text:
        return False, None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        # Preserve damaged legacy data as an explicit admin policy so the
        # validator fails closed and the admin can repair it.
        return True, hashlib.sha256(text.encode('utf-8')).hexdigest()
    candidates = parsed.get('data') if isinstance(parsed, dict) else parsed
    has_values = bool(isinstance(candidates, list) and candidates)
    if not has_values:
        return False, None
    stable = json.dumps(
        parsed, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    )
    return True, hashlib.sha256(stable.encode('utf-8')).hexdigest()


def apply_migration(connection: sqlite3.Connection, *, verbose: bool = True) -> int:
    cursor = connection.cursor()
    if not _table_exists(cursor):
        if verbose:
            print(f"  -- {TABLE}: table does not exist, skipped")
        return 0

    existing = _columns(cursor)
    added = 0
    for name, ddl in COLUMNS:
        if name in existing:
            continue
        cursor.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}")
        existing.add(name)
        added += 1
        if verbose:
            print(f"  ++ {TABLE}.{name}")

    rows = cursor.execute(
        f"SELECT id, dictionary_json, dictionary_source, dictionary_hash, "
        f"dictionary_version, dictionary_synced_at, updated_at, created_at FROM {TABLE}"
    ).fetchall()
    for row in rows:
        (
            row_id, dictionary_json, source, data_hash, version,
            synced_at, updated_at, created_at,
        ) = row
        has_values, normalized_hash = _dictionary_state(dictionary_json)
        normalized_source = str(source or '').strip()
        if normalized_source not in {'none', 'admin', 'wb_schema', 'wb_directory'}:
            normalized_source = 'admin' if has_values else 'none'
        elif has_values and normalized_source == 'none':
            normalized_source = 'admin'
        elif not has_values:
            normalized_source = 'none'
        normalized_version = max(int(version or 0), 1 if has_values else 0)
        normalized_synced_at = (
            synced_at or updated_at or created_at
        ) if has_values else None
        cursor.execute(
            f"""
            UPDATE {TABLE}
            SET dictionary_source = ?, dictionary_hash = ?,
                dictionary_version = ?, dictionary_synced_at = ?
            WHERE id = ?
            """,
            (
                normalized_source, normalized_hash, normalized_version,
                normalized_synced_at, row_id,
            ),
        )

    cursor.execute(
        f"CREATE INDEX IF NOT EXISTS idx_mp_charc_dictionary_source "
        f"ON {TABLE}(category_id, dictionary_source)"
    )
    return added


def _find_database() -> str | None:
    candidates = (
        os.environ.get('DATABASE_PATH'),
        '/app/data/seller_platform.db',
        'data/seller_platform.db',
        'seller_platform.db',
    )
    return next((path for path in candidates if path and Path(path).exists()), None)


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else _find_database()
    if not db_path or not Path(db_path).exists():
        print('WB dictionary provenance migration: database not found')
        return 1
    connection = sqlite3.connect(db_path)
    try:
        added = apply_migration(connection)
        connection.commit()
        print(f'WB dictionary provenance migration complete; added={added}')
        return 0
    except Exception as exc:
        connection.rollback()
        print(f'WB dictionary provenance migration failed: {exc}')
        return 1
    finally:
        connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
