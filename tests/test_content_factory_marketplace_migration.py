# -*- coding: utf-8 -*-
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migrations.migrate_add_content_factory_marketplace_scope import migrate


class ContentFactoryMarketplaceMigrationTests(unittest.TestCase):
    def test_migration_is_idempotent_and_preserves_legacy_default(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "platform.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE sellers (id INTEGER PRIMARY KEY AUTOINCREMENT);
                CREATE TABLE seller_marketplace_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT
                );
                CREATE TABLE content_factories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    name TEXT NOT NULL
                );
                CREATE TABLE content_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    factory_id INTEGER NOT NULL
                );
                INSERT INTO sellers DEFAULT VALUES;
                INSERT INTO content_factories (seller_id, name)
                VALUES (1, 'Legacy factory');
                INSERT INTO content_items (factory_id) VALUES (1);
                """
            )
            connection.commit()
            connection.close()

            migrate(str(database))
            migrate(str(database))

            connection = sqlite3.connect(database)
            try:
                factory_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(content_factories)"
                    ).fetchall()
                }
                item_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(content_items)"
                    ).fetchall()
                }
                indexes = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA index_list(content_factories)"
                    ).fetchall()
                }
                legacy = connection.execute(
                    "SELECT catalog_source, marketplace_account_id "
                    "FROM content_factories WHERE id=1"
                ).fetchone()
                refs = connection.execute(
                    "SELECT entity_refs_json FROM content_items WHERE id=1"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertTrue(
            {"catalog_source", "marketplace_account_id"}.issubset(factory_columns)
        )
        self.assertIn("entity_refs_json", item_columns)
        self.assertIn("ix_content_factories_marketplace_account_id", indexes)
        self.assertEqual(legacy, ("legacy_wb", None))
        self.assertEqual(refs, "[]")


if __name__ == "__main__":
    unittest.main()
