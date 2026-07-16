# -*- coding: utf-8 -*-
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migrations.migrate_add_image_generation_lab import migrate
from migrations.migrate_add_image_lab_reference_watermark import migrate as migrate_reference
from migrations.migrate_add_image_lab_angle_synthesis import migrate as migrate_angles
from migrations.migrate_add_image_lab_marketplace_target import (
    migrate as migrate_marketplace_target,
)


class ImageLabMigrationTests(unittest.TestCase):
    def test_migration_is_idempotent_and_creates_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "platform.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE sellers (id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
            connection.execute(
                "CREATE TABLE imported_products (id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
            connection.commit()
            connection.close()
            migrate(str(database))
            migrate(str(database))
            migrate_reference(str(database))
            migrate_reference(str(database))
            migrate_angles(str(database))
            migrate_angles(str(database))
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE marketplace_listings "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
            connection.commit()
            connection.close()
            migrate_marketplace_target(str(database))
            migrate_marketplace_target(str(database))
            connection = sqlite3.connect(database)
            try:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(image_generation_experiments)"
                    ).fetchall()
                }
                indexes = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA index_list(image_generation_experiments)"
                    ).fetchall()
                }
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("INSERT INTO sellers DEFAULT VALUES")
                connection.execute("INSERT INTO imported_products DEFAULT VALUES")
                connection.execute(
                    """
                    INSERT INTO image_generation_experiments (
                        seller_id, imported_product_id, backend, model, prompt,
                        prompt_sha256, status, estimated_cost_rub, created_at
                    ) VALUES (1, 1, 'gpu', 'qwen', 'background', ?, 'queued', 1, ?)
                    """,
                    ("a" * 64, "2026-07-14T00:00:00"),
                )
                connection.execute("DELETE FROM imported_products WHERE id=1")
                detached_product_id = connection.execute(
                    "SELECT imported_product_id FROM image_generation_experiments"
                ).fetchone()[0]
            finally:
                connection.close()
        self.assertTrue({
            "seller_id", "imported_product_id", "prompt_sha256", "status",
            "source_path", "background_path", "final_path", "quality_json",
            "rating", "created_at", "composition_mode",
            "source_photo_indices_json",
            "generation_strategy", "source_photo_roles_json",
            "primary_photo_index", "reference_path", "watermark_path",
            "watermark_json",
            "overlay_json",
            "requested_view",
            "marketplace_listing_id", "target_context_json",
        }.issubset(columns))
        self.assertIn("idx_image_exp_seller_created", indexes)
        self.assertIn("idx_image_exp_seller_status", indexes)
        self.assertIn("idx_image_exp_seller_listing", indexes)
        self.assertIsNone(detached_product_id)


if __name__ == "__main__":
    unittest.main()
