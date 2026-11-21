#!/usr/bin/env python3
"""Add durable idempotency keys and receipts for reviewed WB price batches."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


TABLE = "price_change_batches"
KEY_COLUMN = "review_key_hash"
RECEIPT_COLUMN = "review_receipt_json"
INDEX = "uq_price_change_batches_review_key_hash"


def _table_exists(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone() is not None


def _verify_column(connection: sqlite3.Connection, name: str, expected_type: str) -> bool:
    columns = {
        row[1]: row
        for row in connection.execute(f"PRAGMA table_info({TABLE})").fetchall()
    }
    column = columns.get(name)
    if column is None:
        connection.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {expected_type}")
        return True
    declared_type = "".join(str(column[2]).upper().split())
    if declared_type != expected_type or column[3] != 0:
        raise sqlite3.OperationalError(
            f"Incompatible {TABLE}.{name} schema; expected nullable {expected_type}"
        )
    return False


def _verify_index(connection: sqlite3.Connection) -> bool:
    definition = connection.execute(
        "SELECT type, tbl_name, sql FROM sqlite_master WHERE name=?", (INDEX,)
    ).fetchone()
    if definition is not None and (definition[0] != "index" or definition[1] != TABLE):
        raise sqlite3.OperationalError(f"Incompatible object named {INDEX}")
    if definition is None:
        connection.execute(
            f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} ({KEY_COLUMN})"
        )

    index_rows = connection.execute(f"PRAGMA index_list({TABLE})").fetchall()
    index_row = next((row for row in index_rows if row[1] == INDEX), None)
    if index_row is None:
        raise sqlite3.OperationalError(f"Missing required unique index {INDEX}")
    # SQLite reports: seq, name, unique, origin, partial.
    if index_row[2] != 1 or (len(index_row) > 4 and index_row[4] != 0):
        raise sqlite3.OperationalError(f"Incompatible unique index {INDEX}")
    indexed_columns = tuple(
        row[2]
        for row in connection.execute(f"PRAGMA index_info({INDEX})").fetchall()
    )
    if indexed_columns != (KEY_COLUMN,):
        raise sqlite3.OperationalError(f"Incompatible columns in index {INDEX}")
    return definition is None


def apply_migration(connection: sqlite3.Connection, *, verbose: bool = True) -> int:
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        if not _table_exists(connection):
            raise sqlite3.OperationalError(f"Required table {TABLE} is missing")

        before = set(connection.execute("SELECT type, name FROM sqlite_master"))
        _verify_column(connection, KEY_COLUMN, "VARCHAR(64)")
        _verify_column(connection, RECEIPT_COLUMN, "TEXT")
        _verify_index(connection)

        columns = {
            row[1]: row
            for row in connection.execute(f"PRAGMA table_info({TABLE})").fetchall()
        }
        expected = {
            KEY_COLUMN: "VARCHAR(64)",
            RECEIPT_COLUMN: "TEXT",
        }
        for name, expected_type in expected.items():
            column = columns.get(name)
            if (
                column is None
                or "".join(str(column[2]).upper().split()) != expected_type
                or column[3] != 0
            ):
                raise sqlite3.OperationalError(
                    f"Final {TABLE}.{name} schema verification failed"
                )

        added = len(set(connection.execute("SELECT type, name FROM sqlite_master")) - before)
        if verbose:
            print(
                "WB price review receipt migration complete; "
                f"schema_objects_added={added}"
            )
        return added
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise


def migrate(database_path: str) -> int:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
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
        print("WB price review receipt migration: database not found")
        return 1
    try:
        migrate(database_path)
    except Exception as exc:
        print(f"WB price review receipt migration failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
