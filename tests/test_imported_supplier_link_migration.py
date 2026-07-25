# -*- coding: utf-8 -*-
"""Idempotent exact-provenance backfill for legacy seller imports."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migrations.migrate_backfill_imported_supplier_links import migrate


class ImportedSupplierLinkMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / 'links.db'
        connection = sqlite3.connect(self.db_path)
        connection.executescript('''
            PRAGMA foreign_keys = ON;
            CREATE TABLE supplier_products (
                id INTEGER PRIMARY KEY,
                supplier_id INTEGER NOT NULL,
                external_id VARCHAR(200),
                UNIQUE (supplier_id, external_id)
            );
            CREATE TABLE imported_products (
                id INTEGER PRIMARY KEY,
                supplier_id INTEGER,
                external_id VARCHAR(200),
                supplier_product_id INTEGER
                    REFERENCES supplier_products(id)
            );
            INSERT INTO supplier_products(id, supplier_id, external_id) VALUES
                (10, 1, 'same-key'),
                (11, 2, 'same-key'),
                (12, 1, 'other-key');
            INSERT INTO imported_products(
                id, supplier_id, external_id, supplier_product_id
            ) VALUES
                (1, 1, 'same-key', NULL),
                (2, 2, 'same-key', NULL),
                (3, 1, 'missing', NULL),
                (4, 1, 'other-key', 10),
                (5, NULL, 'same-key', NULL),
                (6, 1, '', NULL);
        ''')
        connection.commit()
        connection.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_backfills_only_exact_null_links_and_is_idempotent(self):
        first = migrate(self.db_path, batch_size=1)
        self.assertEqual(first, {'linked': 2, 'batches': 2, 'remaining': 0})

        connection = sqlite3.connect(self.db_path)
        rows = dict(connection.execute(
            'SELECT id, supplier_product_id FROM imported_products ORDER BY id'
        ).fetchall())
        violations = connection.execute('PRAGMA foreign_key_check').fetchall()
        connection.close()
        self.assertEqual(rows[1], 10)
        self.assertEqual(rows[2], 11)
        self.assertIsNone(rows[3])
        # Existing non-NULL values are never rewritten, even when legacy data
        # is inconsistent with the current source key.
        self.assertEqual(rows[4], 10)
        self.assertIsNone(rows[5])
        self.assertIsNone(rows[6])
        self.assertEqual(violations, [])

        second = migrate(self.db_path, batch_size=2)
        self.assertEqual(second, {'linked': 0, 'batches': 0, 'remaining': 0})

    def test_rejects_invalid_batch_size_and_missing_schema(self):
        with self.assertRaises(ValueError):
            migrate(self.db_path, batch_size=0)
        missing = Path(self.tempdir.name) / 'missing.db'
        sqlite3.connect(missing).close()
        with self.assertRaises(sqlite3.OperationalError):
            migrate(missing)


if __name__ == '__main__':
    unittest.main()
