#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add an exact marketplace-listing target to Image Lab experiments."""

import os
import sqlite3
import sys

try:
    from .migrate_add_image_lab_angle_synthesis import migrate as migrate_angles
except ImportError:  # direct script execution
    from migrate_add_image_lab_angle_synthesis import migrate as migrate_angles


def migrate(db_path: str) -> None:
    migrate_angles(db_path)
    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "marketplace_listings" not in tables:
            raise RuntimeError(
                "marketplace_listings must exist before Image Lab marketplace targets"
            )
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(image_generation_experiments)"
            ).fetchall()
        }
        if "marketplace_listing_id" not in columns:
            connection.execute(
                "ALTER TABLE image_generation_experiments "
                "ADD COLUMN marketplace_listing_id INTEGER "
                "REFERENCES marketplace_listings(id) ON DELETE SET NULL"
            )
        if "target_context_json" not in columns:
            connection.execute(
                "ALTER TABLE image_generation_experiments "
                "ADD COLUMN target_context_json TEXT"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_image_exp_seller_listing "
            "ON image_generation_experiments "
            "(seller_id, marketplace_listing_id, created_at)"
        )
        connection.commit()
        print("image_generation_experiments marketplace target: OK")
    finally:
        connection.close()


if __name__ == "__main__":
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("DATABASE_PATH", "data/seller_platform.db")
    )
    migrate(path)
