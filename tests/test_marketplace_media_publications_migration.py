# -*- coding: utf-8 -*-
"""Idempotent SQLite schema for marketplace-neutral media operations."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from migrations.migrate_add_marketplace_media_publications import migrate


class MarketplaceMediaPublicationMigrationTests(unittest.TestCase):
    def test_migration_is_idempotent_and_enforces_active_target(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "platform.db"
            connection = sqlite3.connect(database)
            connection.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE sellers (id INTEGER PRIMARY KEY AUTOINCREMENT);
                CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT);
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    nm_id INTEGER NOT NULL
                );
                CREATE TABLE imported_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    product_id INTEGER
                );
                CREATE TABLE infographic_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL
                );
                CREATE TABLE infographic_campaign_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    seller_id INTEGER NOT NULL
                );
                CREATE TABLE infographic_campaign_slides (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    seller_id INTEGER NOT NULL,
                    artifact_sha256 TEXT,
                    review_status TEXT
                );
                CREATE TABLE seller_marketplace_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    marketplace_id INTEGER NOT NULL
                );
                CREATE TABLE marketplace_listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    account_id INTEGER,
                    legacy_product_id INTEGER
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
                indexes = {
                    row[1] for row in connection.execute(
                        "PRAGMA index_list(marketplace_media_operations)"
                    ).fetchall()
                }
                connection.execute("INSERT INTO sellers DEFAULT VALUES")
                connection.execute("INSERT INTO users DEFAULT VALUES")
                connection.execute("INSERT INTO products(seller_id,nm_id) VALUES (1,1001)")
                connection.execute("INSERT INTO infographic_campaigns(seller_id) VALUES (1)")
                connection.execute("""
                    INSERT INTO marketplace_media_publications (
                        seller_id, campaign_id, marketplace_code
                    ) VALUES (1, 1, 'wb')
                """)
                base_values = (
                    1, 1, 1, "wb", "1001", "queued", "a" * 64, "b" * 64,
                )
                connection.execute("""
                    INSERT INTO marketplace_media_operations (
                        publication_id, seller_id, legacy_product_id,
                        marketplace_code, external_item_id, status, baseline_fingerprint,
                        proposed_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, base_values)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("""
                        INSERT INTO marketplace_media_operations (
                            publication_id, seller_id, legacy_product_id,
                            marketplace_code, external_item_id, status, baseline_fingerprint,
                            proposed_fingerprint
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, base_values)
                connection.rollback()
            finally:
                connection.close()

        self.assertTrue({
            "marketplace_media_publications",
            "marketplace_media_operations",
            "marketplace_media_operation_slides",
        }.issubset(tables))
        self.assertIn("uq_marketplace_media_active_wb_target", indexes)
        self.assertIn("uq_marketplace_media_publication_publish_item", indexes)


if __name__ == "__main__":
    unittest.main()
