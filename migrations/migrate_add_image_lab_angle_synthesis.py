#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add a per-job target view for research-only novel-view synthesis."""

import os
import sqlite3
import sys

try:
    from .migrate_add_image_lab_reference_watermark import migrate as migrate_reference
except ImportError:  # direct script execution
    from migrate_add_image_lab_reference_watermark import migrate as migrate_reference


def migrate(db_path: str) -> None:
    migrate_reference(db_path)
    connection = sqlite3.connect(db_path)
    try:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(image_generation_experiments)"
            ).fetchall()
        }
        if "requested_view" not in columns:
            connection.execute(
                "ALTER TABLE image_generation_experiments "
                "ADD COLUMN requested_view VARCHAR(32)"
            )
        connection.commit()
        print("image_generation_experiments angle synthesis: OK")
    finally:
        connection.close()


if __name__ == "__main__":
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("DATABASE_PATH", "data/seller_platform.db")
    )
    migrate(path)
