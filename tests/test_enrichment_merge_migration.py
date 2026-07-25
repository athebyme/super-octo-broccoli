# -*- coding: utf-8 -*-
"""Idempotence test for smart-enrichment audit migration."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migrations.migrate_add_enrichment_merge_audit import migrate
from migrations.migrate_enrichment_reliability_v2 import (
    migrate as migrate_reliability_v2,
)


class EnrichmentMergeMigrationTestCase(unittest.TestCase):
    def test_adds_merge_decisions_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'test.db'
            conn = sqlite3.connect(db_path)
            conn.execute(
                'CREATE TABLE card_edit_history '
                '(id INTEGER PRIMARY KEY, changed_fields TEXT)'
            )
            conn.commit()
            conn.close()

            self.assertTrue(migrate(db_path))
            self.assertTrue(migrate(db_path))

            conn = sqlite3.connect(db_path)
            columns = {
                row[1]
                for row in conn.execute('PRAGMA table_info(card_edit_history)')
            }
            conn.close()
            self.assertIn('merge_decisions', columns)

    def test_adds_restart_and_reconciliation_columns_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'test.db'
            conn = sqlite3.connect(db_path)
            conn.execute(
                'CREATE TABLE bulk_edit_history (id INTEGER PRIMARY KEY)'
            )
            conn.execute(
                'CREATE TABLE card_edit_history '
                '(id INTEGER PRIMARY KEY, changed_fields TEXT)'
            )
            conn.execute(
                'CREATE TABLE enrichment_jobs '
                '(id TEXT PRIMARY KEY, status TEXT)'
            )
            conn.commit()
            conn.close()

            self.assertTrue(migrate_reliability_v2(db_path))
            self.assertTrue(migrate_reliability_v2(db_path))

            conn = sqlite3.connect(db_path)
            history_columns = {
                row[1] for row in conn.execute(
                    'PRAGMA table_info(card_edit_history)'
                )
            }
            job_columns = {
                row[1] for row in conn.execute(
                    'PRAGMA table_info(enrichment_jobs)'
                )
            }
            indexes = {
                row[1] for row in conn.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE type = 'index'"
                )
            }
            conn.close()

            self.assertTrue({
                'wb_reconcile_due_at', 'wb_reconcile_attempts',
                'wb_reconciled_at', 'wb_reconcile_code',
            }.issubset(history_columns))
            self.assertTrue({
                'product_ids_json', 'bulk_edit_id', 'claim_token',
                'claim_expires_at', 'current_product_id',
                'current_item_started_at', 'confirmed', 'conflicted',
            }.issubset(job_columns))
            self.assertIn(
                'ix_card_edit_history_wb_reconcile_due_at', indexes,
            )


if __name__ == '__main__':
    unittest.main()
