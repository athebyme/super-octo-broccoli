#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add typed marketplace listing scope to the legacy content factory."""

import os
import sqlite3
import sys


def migrate(db_path: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "content_factories",
            "content_items",
            "seller_marketplace_accounts",
        }
        missing = required - tables
        if missing:
            raise RuntimeError(
                "Content Factory marketplace scope prerequisites missing: "
                + ", ".join(sorted(missing))
            )

        factory_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(content_factories)"
            ).fetchall()
        }
        if "catalog_source" not in factory_columns:
            connection.execute(
                "ALTER TABLE content_factories "
                "ADD COLUMN catalog_source VARCHAR(30) NOT NULL DEFAULT 'legacy_wb'"
            )
        if "marketplace_account_id" not in factory_columns:
            connection.execute(
                "ALTER TABLE content_factories "
                "ADD COLUMN marketplace_account_id INTEGER "
                "REFERENCES seller_marketplace_accounts(id) ON DELETE SET NULL"
            )
        item_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(content_items)"
            ).fetchall()
        }
        if "entity_refs_json" not in item_columns:
            connection.execute(
                "ALTER TABLE content_items "
                "ADD COLUMN entity_refs_json TEXT NOT NULL DEFAULT '[]'"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_content_factories_marketplace_account_id "
            "ON content_factories (marketplace_account_id)"
        )
        connection.commit()
        print("content factory marketplace scope: OK")
    finally:
        connection.close()


if __name__ == "__main__":
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("DATABASE_PATH", "data/seller_platform.db")
    )
    migrate(path)
