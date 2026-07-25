#!/usr/bin/env python3
"""Add explicit Ozon full-state attribute-removal intent to drafts.

The column is additive and defaults to an empty list.  Existing drafts keep
the preserve-live behavior until a seller explicitly reviews cleanup in the
mass editor.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


TABLE = "marketplace_product_drafts"
COLUMN = "attribute_removals_json"


def migrate(db_path: str) -> None:
    path = Path(db_path)
    if not path.exists():
        raise RuntimeError(f"Database does not exist: {path}")

    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,),
        ).fetchone()
        if table is None:
            raise RuntimeError(
                f"Required table {TABLE} is missing; run the base draft "
                "migration first"
            )
        columns = {
            row[1]
            for row in connection.execute(
                f"PRAGMA table_info({TABLE})"
            ).fetchall()
        }
        if COLUMN not in columns:
            connection.execute(
                f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        connection.commit()
        print("Marketplace draft attribute removals migration completed!")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: migrate_add_marketplace_draft_attribute_removals.py "
            "/path/to/seller_platform.db"
        )
    migrate(sys.argv[1])
