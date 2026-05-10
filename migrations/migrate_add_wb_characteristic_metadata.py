#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Add WB characteristic metadata columns introduced in the current Content API:
hasFilter and isVariable.
"""
import sqlite3
import sys
from pathlib import Path


def _db_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    root = Path(__file__).resolve().parent.parent
    for candidate in (root / "data" / "seller_platform.db", root / "seller_platform.db"):
        if candidate.exists():
            return str(candidate)
    return str(root / "data" / "seller_platform.db")


def _columns(cursor, table_name: str) -> set:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='marketplace_category_characteristics'"
        )
        if not cursor.fetchone():
            print("marketplace_category_characteristics table does not exist, skipping")
            return

        existing = _columns(cursor, "marketplace_category_characteristics")
        additions = [
            ("has_filter", "BOOLEAN DEFAULT 0"),
            ("is_variable", "BOOLEAN DEFAULT 0"),
        ]
        for name, ddl in additions:
            if name not in existing:
                cursor.execute(
                    f"ALTER TABLE marketplace_category_characteristics ADD COLUMN {name} {ddl}"
                )
                print(f"Added marketplace_category_characteristics.{name}")
            else:
                print(f"Column marketplace_category_characteristics.{name} already exists")

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate(_db_path())
