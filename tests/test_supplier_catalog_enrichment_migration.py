# -*- coding: utf-8 -*-
"""Idempotent SQLite migration for supplier catalog enrichment."""

import os
import sqlite3
import tempfile
import unittest

from migrations.migrate_add_supplier_catalog_enrichment import migrate


class SupplierCatalogEnrichmentMigrationTest(unittest.TestCase):
    def test_migration_is_idempotent_and_backfills_current_revision(self):
        handle, path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        try:
            conn = sqlite3.connect(path)
            conn.executescript('''
                CREATE TABLE users (id INTEGER PRIMARY KEY);
                CREATE TABLE suppliers (id INTEGER PRIMARY KEY);
                CREATE TABLE supplier_products (
                    id INTEGER PRIMARY KEY,
                    supplier_id INTEGER NOT NULL,
                    external_id TEXT
                );
                CREATE TABLE imported_products (
                    id INTEGER PRIMARY KEY,
                    supplier_product_id INTEGER
                );
                INSERT INTO users(id) VALUES (1);
                INSERT INTO suppliers(id) VALUES (1);
                INSERT INTO supplier_products(id, supplier_id, external_id)
                VALUES (10, 1, 'p-10');
                INSERT INTO imported_products(id, supplier_product_id)
                VALUES (20, 10);
            ''')
            conn.commit()
            conn.close()

            migrate(path)
            migrate(path)

            conn = sqlite3.connect(path)
            product_columns = {
                row[1] for row in conn.execute(
                    'PRAGMA table_info(supplier_products)'
                )
            }
            imported_columns = {
                row[1] for row in conn.execute(
                    'PRAGMA table_info(imported_products)'
                )
            }
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            revision = conn.execute(
                'SELECT supplier_content_revision FROM imported_products '
                'WHERE id=20'
            ).fetchone()[0]
            conn.close()

            self.assertIn('content_revision', product_columns)
            self.assertIn('supplier_content_revision', imported_columns)
            self.assertEqual(revision, 1)
            self.assertIn('supplier_catalog_enrichment_runs', tables)
            self.assertIn('supplier_catalog_enrichment_items', tables)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
