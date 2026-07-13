# -*- coding: utf-8 -*-
"""Тест идемпотентной миграции card-quality v2."""
import sqlite3
import unittest
import tempfile
import os

from migrations.migrate_add_card_quality_v2 import migrate


class TestCardQualityV2Migration(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, quality_score FLOAT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_adds_columns_and_table(self):
        self.assertTrue(migrate(self.db_path))
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
        self.assertTrue({'wb_views_30d', 'wb_orders_30d', 'wb_cart_conv', 'wb_order_conv',
                         'wb_buyout_rate', 'funnel_checked_at', 'attention_reasons',
                         'quality_impact'} <= cols)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('wb_subject_charcs_cache', tables)
        conn.close()

    def test_idempotent(self):
        self.assertTrue(migrate(self.db_path))
        self.assertTrue(migrate(self.db_path))  # повторный запуск не падает
