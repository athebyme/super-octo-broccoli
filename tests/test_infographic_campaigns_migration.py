# -*- coding: utf-8 -*-
"""Idempotent SQLite contract for durable infographic campaigns."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from migrations.migrate_add_infographic_campaigns import migrate


class InfographicCampaignMigrationTests(unittest.TestCase):
    def test_migration_is_idempotent_and_preserves_fk_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "platform.db"
            connection = sqlite3.connect(database)
            connection.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE sellers (id INTEGER PRIMARY KEY AUTOINCREMENT);
                CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT);
                CREATE TABLE imported_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id)
                );
            """)
            connection.commit()
            connection.close()

            migrate(str(database))
            migrate(str(database))

            connection = sqlite3.connect(database)
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                item_columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(infographic_campaign_items)"
                    ).fetchall()
                }
                slide_indexes = {
                    row[1] for row in connection.execute(
                        "PRAGMA index_list(infographic_campaign_slides)"
                    ).fetchall()
                }

                connection.execute("INSERT INTO sellers DEFAULT VALUES")
                connection.execute("INSERT INTO users DEFAULT VALUES")
                connection.execute(
                    "INSERT INTO imported_products(seller_id) VALUES (1)"
                )
                connection.execute("""
                    INSERT INTO infographic_campaigns (
                        seller_id, created_by_user_id, name
                    ) VALUES (1, 1, 'Test campaign')
                """)
                connection.execute("""
                    INSERT INTO infographic_campaign_items (
                        campaign_id, seller_id, imported_product_id,
                        product_title, source_fingerprint
                    ) VALUES (1, 1, 1, 'Test product', ?)
                """, ("a" * 64,))
                connection.execute("""
                    INSERT INTO infographic_campaign_slides (
                        campaign_id, item_id, seller_id, position, slide_type
                    ) VALUES (1, 1, 1, 1, 'hero')
                """)
                connection.execute("DELETE FROM imported_products WHERE id=1")
                detached = connection.execute(
                    "SELECT imported_product_id FROM infographic_campaign_items WHERE id=1"
                ).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("""
                        INSERT INTO infographic_campaign_slides (
                            campaign_id, item_id, seller_id, position, slide_type
                        ) VALUES (1, 1, 1, 0, 'invalid')
                    """)
                connection.rollback()
            finally:
                connection.close()

        self.assertTrue({
            "infographic_campaigns",
            "infographic_campaign_items",
            "infographic_campaign_slides",
        }.issubset(tables))
        self.assertTrue({
            "campaign_id", "seller_id", "imported_product_id",
            "source_fingerprint", "fact_pack_json", "content_json",
        }.issubset(item_columns))
        self.assertIn("idx_infographic_slide_campaign_review", slide_indexes)
        self.assertIsNone(detached)


if __name__ == "__main__":
    unittest.main()
