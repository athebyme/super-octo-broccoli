#!/usr/bin/env python3
"""Add separate storage for scoped Ozon dictionary search receipts."""

from __future__ import annotations

import argparse
from contextlib import closing
import sqlite3
from pathlib import Path

from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from services.ozon_brand_search_models import OzonBrandSearchEvidence


def migrate(database_path: str) -> int:
    """Install the ledger atomically while preserving existing reference rows."""
    uri = Path(database_path).resolve().as_uri() + "?mode=rw"
    table = OzonBrandSearchEvidence.__table__
    sqlite_dialect = dialect()
    expected = {table.name: ("table", str(CreateTable(table).compile(dialect=sqlite_dialect)))}
    expected.update({
        index.name: ("index", str(CreateIndex(index).compile(dialect=sqlite_dialect)))
        for index in sorted(table.indexes, key=lambda index: index.name)
    })

    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for parent in ("sellers", "seller_marketplace_accounts"):
            columns = {
                row[1] for row in connection.execute(f'PRAGMA table_info("{parent}")')
            }
            if "id" not in columns:
                raise sqlite3.OperationalError(
                    f"Ozon brand search evidence parent table {parent} is unavailable"
                )

        added = 0
        for name, (object_type, sql) in expected.items():
            existing = connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name = ?", (name,),
            ).fetchone()
            if existing is None:
                connection.execute(sql)
                added += 1
            elif existing[0] != object_type or (existing[1] or "").split() != sql.split():
                raise sqlite3.OperationalError("incompatible Ozon brand search evidence schema")

        actual_objects = dict(connection.execute(
            "SELECT name, type FROM sqlite_master WHERE tbl_name = ? AND sql IS NOT NULL",
            (table.name,),
        ))
        if actual_objects != {name: object_type for name, (object_type, _) in expected.items()}:
            raise sqlite3.OperationalError("unexpected Ozon brand search evidence schema objects")

    print(f"Ozon brand search evidence migration complete ({added} schema objects added)")
    return added


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database_path")
    migrate(parser.parse_args().database_path)
