# -*- coding: utf-8 -*-
"""Миграция v2 мониторинга конкурентов: идемпотентность и backfill."""
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _create_v1_schema(db_path):
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE competitor_monitor_settings (
            id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL,
            is_enabled BOOLEAN DEFAULT 0, is_running BOOLEAN DEFAULT 0,
            price_change_alert_percent FLOAT DEFAULT 5.0,
            requests_per_minute INTEGER DEFAULT 60,
            max_products INTEGER DEFAULT 100000,
            pause_between_cycles_seconds INTEGER DEFAULT 60,
            proxy_url VARCHAR(500),
            last_sync_at DATETIME, last_sync_status VARCHAR(50),
            last_sync_error TEXT, last_full_cycle_duration FLOAT,
            total_products_monitored INTEGER DEFAULT 0,
            total_cycles_completed INTEGER DEFAULT 0,
            created_at DATETIME, updated_at DATETIME
        );
        CREATE TABLE competitor_groups (
            id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL,
            name VARCHAR(200) NOT NULL, description TEXT, color VARCHAR(7),
            own_product_id INTEGER, auto_source VARCHAR(20),
            auto_source_value VARCHAR(200), is_active BOOLEAN DEFAULT 1,
            created_at DATETIME, updated_at DATETIME
        );
        CREATE TABLE competitor_products (
            id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL, nm_id BIGINT NOT NULL,
            title VARCHAR(500), brand VARCHAR(200), supplier_name VARCHAR(200),
            wb_supplier_id BIGINT, image_url VARCHAR(500),
            current_price INTEGER, current_sale_price INTEGER,
            current_rating FLOAT, current_feedbacks_count INTEGER,
            current_total_stock INTEGER, priority INTEGER DEFAULT 2,
            is_active BOOLEAN DEFAULT 1, last_fetched_at DATETIME,
            fetch_error_count INTEGER DEFAULT 0,
            created_at DATETIME, updated_at DATETIME
        );
        INSERT INTO competitor_monitor_settings
            (id, seller_id, is_enabled, pause_between_cycles_seconds)
            VALUES (1, 2, 1, 60), (2, 1, 0, 0);
        INSERT INTO competitor_products
            (id, seller_id, group_id, nm_id, current_sale_price, last_fetched_at)
            VALUES (1, 2, 1, 111, 9471, '2026-07-21 10:00:00'),
                   (2, 2, 1, 222, NULL, '2026-07-21 10:00:00');
    """)
    con.commit()
    con.close()


class CompetitorV2MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        _create_v1_schema(self.db_path)

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def _columns(self, table):
        con = sqlite3.connect(self.db_path)
        cols = {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
        con.close()
        return cols

    def test_adds_columns_and_backfills(self):
        from migrations.migrate_competitor_monitor_v2 import migrate
        self.assertTrue(migrate(self.db_path))
        self.assertIn('sync_interval_minutes', self._columns('competitor_monitor_settings'))
        self.assertIn('next_sync_due_at', self._columns('competitor_monitor_settings'))
        self.assertIn('discount_alert_pp', self._columns('competitor_monitor_settings'))
        self.assertIn('metadata_synced_at', self._columns('competitor_products'))
        self.assertIn('is_adult', self._columns('competitor_products'))
        self.assertIn('price_miss_count', self._columns('competitor_products'))
        self.assertIn('last_price_at', self._columns('competitor_products'))
        self.assertIn('import_requested', self._columns('competitor_groups'))
        # мёртвые v1-колонки выпилены (современный SQLite умеет DROP COLUMN)
        if tuple(int(x) for x in sqlite3.sqlite_version.split('.')[:2]) >= (3, 35):
            self.assertNotIn('pause_between_cycles_seconds',
                             self._columns('competitor_monitor_settings'))
            self.assertNotIn('requests_per_minute',
                             self._columns('competitor_monitor_settings'))
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            'SELECT seller_id, sync_interval_minutes FROM competitor_monitor_settings ORDER BY seller_id'
        ).fetchall()
        # backfill: всем существующим строкам interval 60
        self.assertEqual(rows, [(1, 60), (2, 60)])
        # last_price_at backfill только там, где цена есть
        lp = con.execute(
            'SELECT id, last_price_at FROM competitor_products ORDER BY id'
        ).fetchall()
        self.assertEqual(lp[0][1], '2026-07-21 10:00:00')
        self.assertIsNone(lp[1][1])
        con.close()

    def test_idempotent(self):
        from migrations.migrate_competitor_monitor_v2 import migrate
        self.assertTrue(migrate(self.db_path))
        self.assertTrue(migrate(self.db_path))  # повторный запуск не падает

    def test_missing_tables_is_noop(self):
        from migrations.migrate_competitor_monitor_v2 import migrate
        empty = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        empty.close()
        try:
            self.assertTrue(migrate(empty.name))
        finally:
            Path(empty.name).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
