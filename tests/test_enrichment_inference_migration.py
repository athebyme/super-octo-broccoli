# -*- coding: utf-8 -*-
"""Миграция режима inference: rebuild CHECK mode + items.inference_json."""

import os
import sqlite3
import tempfile
import unittest

from migrations.migrate_add_enrichment_inference import run_migration


OLD_RUNS_DDL = """
CREATE TABLE supplier_catalog_enrichment_runs (
    id VARCHAR(36) NOT NULL,
    supplier_id INTEGER NOT NULL,
    admin_user_id INTEGER NOT NULL,
    mode VARCHAR(40) NOT NULL,
    status VARCHAR(24) NOT NULL,
    selection_json TEXT NOT NULL,
    reference_snapshot_json TEXT,
    model_used VARCHAR(100),
    total INTEGER NOT NULL,
    processed INTEGER NOT NULL,
    applied INTEGER NOT NULL,
    unchanged INTEGER NOT NULL,
    needs_review INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    cancelled INTEGER NOT NULL,
    llm_calls INTEGER NOT NULL,
    llm_call_limit INTEGER NOT NULL,
    current_label VARCHAR(300),
    error_code VARCHAR(80),
    error_message TEXT,
    heartbeat_at DATETIME,
    started_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_supplier_catalog_enrichment_run_mode
        CHECK (mode IN ('category_only', 'category_and_characteristics')),
    CONSTRAINT ck_supplier_catalog_enrichment_run_status
        CHECK (status IN ('pending', 'running', 'cancelling', 'cancelled',
                          'completed', 'partial', 'failed')),
    FOREIGN KEY(supplier_id) REFERENCES suppliers (id),
    FOREIGN KEY(admin_user_id) REFERENCES users (id)
)
"""

PARENT_DDL = """
CREATE TABLE suppliers (id INTEGER PRIMARY KEY, code TEXT);
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
INSERT INTO suppliers (id, code) VALUES (1, 'sup');
INSERT INTO users (id, username) VALUES (1, 'admin');
"""

OLD_ITEMS_DDL = """
CREATE TABLE supplier_catalog_enrichment_items (
    id INTEGER PRIMARY KEY,
    run_id VARCHAR(36) NOT NULL,
    supplier_product_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    phase VARCHAR(24) NOT NULL DEFAULT 'category',
    status VARCHAR(24) NOT NULL DEFAULT 'pending'
)
"""


class EnrichmentInferenceMigrationTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        con = sqlite3.connect(self.db_path)
        con.executescript(PARENT_DDL)
        con.executescript(OLD_RUNS_DDL)
        con.executescript(OLD_ITEMS_DDL)
        con.execute(
            "INSERT INTO supplier_catalog_enrichment_runs "
            "(id, supplier_id, admin_user_id, mode, status, selection_json, "
            "total, processed, applied, unchanged, needs_review, failed, "
            "cancelled, llm_calls, llm_call_limit, created_at, updated_at) "
            "VALUES ('r1', 1, 1, 'category_only', 'completed', '{}', "
            "1, 1, 1, 0, 0, 0, 0, 2, 20, '2026-01-01', '2026-01-01')"
        )
        con.execute(
            "INSERT INTO supplier_catalog_enrichment_runs "
            "(id, supplier_id, admin_user_id, mode, status, selection_json, "
            "total, processed, applied, unchanged, needs_review, failed, "
            "cancelled, llm_calls, llm_call_limit, created_at, updated_at) "
            "VALUES ('r2', 1, 1, 'category_and_characteristics', 'partial', "
            "'{}', 2, 2, 1, 0, 1, 0, 0, 4, 20, '2026-01-02', '2026-01-02')"
        )
        con.commit()
        con.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _migrate(self):
        import sys
        argv = sys.argv
        sys.argv = ['migrate', self.db_path]
        try:
            run_migration()
        finally:
            sys.argv = argv

    def test_migration_is_idempotent_and_preserves_rows(self):
        self._migrate()
        self._migrate()

        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute(
            "SELECT id, mode FROM supplier_catalog_enrichment_runs ORDER BY id"
        )
        self.assertEqual(cur.fetchall(), [
            ('r1', 'category_only'),
            ('r2', 'category_and_characteristics'),
        ])

        # Новый mode принимается
        cur.execute(
            "INSERT INTO supplier_catalog_enrichment_runs "
            "(id, supplier_id, admin_user_id, mode, status, selection_json, "
            "total, processed, applied, unchanged, needs_review, failed, "
            "cancelled, llm_calls, llm_call_limit, created_at, updated_at) "
            "VALUES ('r3', 1, 1, 'characteristics_inference', 'pending', "
            "'{}', 3, 0, 0, 0, 0, 0, 0, 0, 20, '2026-01-03', '2026-01-03')"
        )
        # Мусорный mode по-прежнему отклоняется
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                "INSERT INTO supplier_catalog_enrichment_runs "
                "(id, supplier_id, admin_user_id, mode, status, "
                "selection_json, total, processed, applied, unchanged, "
                "needs_review, failed, cancelled, llm_calls, llm_call_limit, "
                "created_at, updated_at) "
                "VALUES ('bad', 1, 1, 'garbage', 'pending', '{}', "
                "1, 0, 0, 0, 0, 0, 0, 0, 20, '2026-01-04', '2026-01-04')"
            )

        cur.execute('PRAGMA table_info(supplier_catalog_enrichment_items)')
        self.assertIn(
            'inference_json', {row[1] for row in cur.fetchall()},
        )
        # partial unique активного scope пересоздан
        cur.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name='uq_supplier_catalog_enrichment_active_supplier'"
        )
        self.assertEqual(cur.fetchone()[0], 1)
        con.close()


if __name__ == '__main__':
    unittest.main()
