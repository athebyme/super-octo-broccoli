#!/usr/bin/env python3
"""Add separate storage for opt-in supplier catalog schedules."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from services.supplier_catalog_schedule_models import (
    SupplierCatalogSchedule,
    SupplierCatalogScheduledRun,
)


_SCHEDULE_REVISION_TRIGGER = "tr_supplier_catalog_schedule_revision_increases"
_SCHEDULE_REVISION_TRIGGER_SQL = """
CREATE TRIGGER tr_supplier_catalog_schedule_revision_increases
BEFORE UPDATE OF revision ON supplier_catalog_schedules
FOR EACH ROW
WHEN NEW.revision <= OLD.revision
BEGIN
    SELECT RAISE(ABORT, 'supplier catalog schedule revision must increase');
END
""".strip()


def _schema_objects():
    sqlite_dialect = dialect()
    for table in (SupplierCatalogSchedule.__table__, SupplierCatalogScheduledRun.__table__):
        yield "table", table.name, str(CreateTable(table).compile(dialect=sqlite_dialect)).strip()
        for index in sorted(table.indexes, key=lambda item: item.name):
            yield "index", index.name, str(CreateIndex(index).compile(dialect=sqlite_dialect)).strip()
    yield "trigger", _SCHEDULE_REVISION_TRIGGER, _SCHEDULE_REVISION_TRIGGER_SQL


def _require_tables(connection: sqlite3.Connection) -> None:
    required = {
        "suppliers": {"id"},
        "users": {"id"},
        "supplier_catalog_refresh_runs": {"id"},
    }
    for table, required_columns in required.items():
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            raise sqlite3.OperationalError(
                f"supplier catalog schedule parent table {table} is unavailable"
            )
        columns = {
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if not required_columns.issubset(columns):
            raise sqlite3.OperationalError(
                f"supplier catalog schedule parent table {table} is incompatible"
            )


def migrate(database_path: str) -> int:
    """Create and verify only the two additive schedule tables and index."""
    uri = Path(database_path).resolve().as_uri() + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_tables(connection)

        added = 0
        for kind, name, expected_sql in _schema_objects():
            actual = connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = ?", (name,),
            ).fetchone()
            if actual is None:
                connection.execute(expected_sql)
                added += 1
            elif (
                actual[0] != kind
                or not actual[1]
                or actual[1].split() != expected_sql.split()
            ):
                raise sqlite3.OperationalError(
                    "incompatible supplier catalog schedule schema"
                )

        # An extra object with one of the managed table names is already
        # rejected above. Also fail closed on extra triggers: they can change
        # schedule revision semantics without appearing in model metadata.
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name IN ('supplier_catalog_schedules', "
                "'supplier_catalog_scheduled_runs')"
            )
        }
        if triggers != {_SCHEDULE_REVISION_TRIGGER}:
            raise sqlite3.OperationalError(
                "incompatible supplier catalog schedule triggers"
            )

        expected_indexes = {
            table.name: {index.name for index in table.indexes}
            for table in (
                SupplierCatalogSchedule.__table__,
                SupplierCatalogScheduledRun.__table__,
            )
        }
        for table_name, expected_names in expected_indexes.items():
            actual_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name=? AND name NOT LIKE 'sqlite_autoindex_%'",
                    (table_name,),
                )
            }
            if actual_names != expected_names:
                raise sqlite3.OperationalError(
                    "incompatible supplier catalog schedule indexes"
                )

        connection.commit()
        print(f"Supplier catalog schedule migration complete ({added} schema objects added)")
        return added
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database_path")
    args = parser.parse_args()
    migrate(args.database_path)


if __name__ == "__main__":
    main()
