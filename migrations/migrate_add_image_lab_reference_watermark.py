#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add reference-guided generation and deterministic watermark audit fields."""

import os
import sqlite3
import sys

try:
    from .migrate_add_image_generation_lab import migrate as migrate_image_lab
except ImportError:  # direct script execution
    from migrate_add_image_generation_lab import migrate as migrate_image_lab


def migrate(db_path: str) -> None:
    migrate_image_lab(db_path)
    connection = sqlite3.connect(db_path)
    try:
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(image_generation_experiments)"
            ).fetchall()
        }
        additions = (
            (
                "generation_strategy",
                "VARCHAR(32) NOT NULL DEFAULT 'background_only'",
            ),
            ("source_photo_roles_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("primary_photo_index", "INTEGER"),
            ("reference_path", "VARCHAR(500)"),
            ("watermark_path", "VARCHAR(500)"),
            ("watermark_json", "TEXT"),
            ("overlay_json", "TEXT"),
        )
        for name, ddl in additions:
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE image_generation_experiments ADD COLUMN {name} {ddl}"
                )
        connection.commit()
        print("image_generation_experiments reference/watermark: OK")
    finally:
        connection.close()


if __name__ == "__main__":
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("DATABASE_PATH", "data/seller_platform.db")
    )
    migrate(path)
