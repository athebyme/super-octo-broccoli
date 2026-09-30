#!/usr/bin/env python3
"""Add the nullable, single-use review key for WB bulk edits."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


TABLE = "bulk_edit_history"
COLUMN = "review_key"
INDEX = "uq_bulk_edit_history_review_key"


def _table_exists(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone() is not None


def _verify_column(connection: sqlite3.Connection) -> bool:
    columns = {
        row[1]: row
        for row in connection.execute(f"PRAGMA table_info({TABLE})").fetchall()
    }
    column = columns.get(COLUMN)
    if column is None:
        connection.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(64)")
        return True
    declared_type = "".join(str(column[2]).upper().split())
    if declared_type != "VARCHAR(64)" or column[3] != 0:
        raise sqlite3.OperationalError(
            f"Incompatible {TABLE}.{COLUMN} schema; expected nullable VARCHAR(64)"
        )
    return False


def _index_definition(connection: sqlite3.Connection):
    return connection.execute(
        "SELECT type, tbl_name, sql FROM sqlite_master WHERE name=?", (INDEX,)
    ).fetchone()


def _verify_index(connection: sqlite3.Connection) -> bool:
    definition = _index_definition(connection)
    if definition is not None and (definition[0] != "index" or definition[1] != TABLE):
        raise sqlite3.OperationalError(f"Incompatible object named {INDEX}")

    if definition is None:
        connection.execute(
            f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} ({COLUMN})"
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
    if indexed_columns != (COLUMN,):
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
        _verify_column(connection)
        _verify_index(connection)

        # Verify the final contract, including nullable legacy rows and unique keys.
        column = next(
            (row for row in connection.execute(f"PRAGMA table_info({TABLE})") if row[1] == COLUMN),
            None,
        )
        if (
            column is None
            or "".join(str(column[2]).upper().split()) != "VARCHAR(64)"
            or column[3] != 0
        ):
            raise sqlite3.OperationalError(
                f"Final {TABLE}.{COLUMN} schema verification failed"
            )

        added = len(set(connection.execute("SELECT type, name FROM sqlite_master")) - before)
        if verbose:
            print(f"WB bulk review key migration complete; schema_objects_added={added}")
        return added
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise


def migrate(database_path: str) -> int:
    connection = sqlite3.connect(database_path)
    try:
        # sqlite3's legacy transaction mode does not automatically wrap DDL.
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
        print("WB bulk review key migration: database not found")
        return 1
    try:
        migrate(database_path)
    except Exception as exc:
        print(f"WB bulk review key migration failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
