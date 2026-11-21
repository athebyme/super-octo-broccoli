"""Add the standalone durable supplier catalog refresh queue."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from services.supplier_catalog_refresh_models import SupplierCatalogRefreshRun


def _schema_objects():
    table = SupplierCatalogRefreshRun.__table__
    sqlite_dialect = dialect()
    yield "table", table.name, str(CreateTable(table).compile(dialect=sqlite_dialect)).strip()
    for index in sorted(table.indexes, key=lambda item: item.name):
        yield "index", index.name, str(CreateIndex(index).compile(dialect=sqlite_dialect)).strip()


def migrate(database_path: str) -> int:
    """Create only the queue and its indexes. Existing application rows are untouched."""
    uri = Path(database_path).resolve().as_uri() + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parents = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('suppliers', 'users')"
        ).fetchall()
        if {row[0] for row in parents} != {"suppliers", "users"}:
            raise sqlite3.OperationalError("supplier catalog refresh parent tables are unavailable")

        added = 0
        for kind, name, expected_sql in _schema_objects():
            actual = connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = ?", (name,),
            ).fetchone()
            if actual is None:
                connection.execute(expected_sql)
                added += 1
            elif actual[0] != kind or not actual[1] or actual[1].split() != expected_sql.split():
                raise sqlite3.OperationalError("incompatible supplier catalog refresh schema")
        connection.commit()
        print(f"Supplier catalog refresh queue migration complete ({added} schema objects added)")
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
