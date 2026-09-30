#!/usr/bin/env python3
"""Add versioned seller-authored common-content metadata to ImportedProduct."""

from __future__ import annotations

import os
import sqlite3
import sys


def _default_db_path() -> str:
    configured = os.environ.get("DATABASE_URL", "")
    if configured.startswith("sqlite:///", 0):
        return configured.removeprefix("sqlite:///")
    candidates = (
        "data/seller_platform.db",
        "/app/data/seller_platform.db",
        os.path.join(os.path.dirname(__file__), "..", "data", "seller_platform.db"),
    )
    return next((path for path in candidates if os.path.exists(path)), candidates[0])


def migrate(db_path: str | None = None) -> bool:
    path = db_path or _default_db_path()
    if not os.path.exists(path):
        print("Database file not found")
        return False

    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "PRAGMA table_info(imported_products)"
        ).fetchall()
        if not rows:
            raise sqlite3.OperationalError(
                "imported_products prerequisite table is missing"
            )
        columns = {row[1]: row for row in rows}
        connection.execute("BEGIN IMMEDIATE")
        if "content_overrides_json" not in columns:
            connection.execute(
                "ALTER TABLE imported_products "
                "ADD COLUMN content_overrides_json TEXT"
            )
        if "content_edit_version" not in columns:
            connection.execute(
                "ALTER TABLE imported_products "
                "ADD COLUMN content_edit_version INTEGER NOT NULL DEFAULT 1"
            )
        current = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(imported_products)"
            ).fetchall()
        }
        overrides = current["content_overrides_json"]
        version = current["content_edit_version"]
        if str(overrides[2]).upper() != "TEXT" or bool(overrides[3]):
            raise sqlite3.OperationalError(
                "content_overrides_json schema does not match the nullable TEXT contract"
            )
        if (
            str(version[2]).upper() not in {"INTEGER", "INT"}
            or not bool(version[3])
            or str(version[4]).strip("'\"() ") != "1"
        ):
            raise sqlite3.OperationalError(
                "content_edit_version schema does not match INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            "UPDATE imported_products SET content_edit_version = 1 "
            "WHERE content_edit_version IS NULL OR content_edit_version < 1"
        )
        connection.commit()
        print("ImportedProduct common-content columns verified")
        return True
    except Exception as exc:
        connection.rollback()
        print(f"ImportedProduct common-content migration failed: {exc}")
        return False
    finally:
        connection.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(0 if migrate(target) else 1)
