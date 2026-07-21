# -*- coding: utf-8 -*-
"""Idempotent SQLite contract for bestseller Image Lab recommendations."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from migrations.migrate_add_bestseller_image_recommendations import migrate


class BestsellerImageRecommendationMigrationTests(unittest.TestCase):
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
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id)
                );
                CREATE TABLE seller_marketplace_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id)
                );
                CREATE TABLE marketplace_listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id),
                    account_id INTEGER REFERENCES seller_marketplace_accounts(id)
                );
            """)
            connection.commit()
            connection.close()

            migrate(str(database))
            migrate(str(database))

            connection = sqlite3.connect(database)
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(bestseller_image_recommendations)"
                    ).fetchall()
                }
                indexes = {
                    row[1] for row in connection.execute(
                        "PRAGMA index_list(bestseller_image_recommendations)"
                    ).fetchall()
                }
                connection.executescript("""
                    INSERT INTO sellers DEFAULT VALUES;
                    INSERT INTO users DEFAULT VALUES;
                    INSERT INTO imported_products(seller_id) VALUES (1);
                    INSERT INTO products(seller_id) VALUES (1);
                    INSERT INTO seller_marketplace_accounts(seller_id) VALUES (1);
                    INSERT INTO marketplace_listings(seller_id, account_id) VALUES (1, 1);
                """)
                connection.execute("""
                    INSERT INTO bestseller_image_recommendations (
                        seller_id, imported_product_id, marketplace_code,
                        account_id, marketplace_listing_id, scope_key,
                        period_code, opportunity_score, units, revenue_rub,
                        photo_count, recommended_by_user_id
                    ) VALUES (1, 1, 'ozon', 1, 1, 'ozon:account:1:listing:1',
                              '30d', 85, 5, 900, 2, 1)
                """)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("""
                        INSERT INTO bestseller_image_recommendations (
                            seller_id, marketplace_code, scope_key, period_code,
                            opportunity_score
                        ) VALUES (1, 'wb', 'ozon:account:1:listing:1', '30d', 80)
                    """)
                connection.rollback()
            finally:
                connection.close()

        self.assertTrue({
            "seller_id", "imported_product_id", "marketplace_code", "scope_key",
            "period_code", "status", "opportunity_score", "snapshot_json",
            "recommended_by_user_id", "reviewed_by_user_id",
        }.issubset(columns))
        self.assertIn("idx_bestseller_image_recommendation_seller_status", indexes)


if __name__ == "__main__":
    unittest.main()
