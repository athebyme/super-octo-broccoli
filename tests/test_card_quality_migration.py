# -*- coding: utf-8 -*-
"""Тест идемпотентной миграции колонок качества карточки."""

import os
import sqlite3
import tempfile
import unittest

from migrations.add_card_quality_columns import migrate

NEW_COLS = ['wb_feedback_rating', 'nm_rating_checked_at',
            'quality_score', 'quality_breakdown_json', 'quality_checked_at']


class TestCardQualityMigration(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.execute('CREATE TABLE products (id INTEGER PRIMARY KEY, nm_rating REAL)')
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.path)

    def _cols(self):
        conn = sqlite3.connect(self.path)
        cols = [r[1] for r in conn.execute('PRAGMA table_info(products)').fetchall()]
        conn.close()
        return cols

    def test_adds_all_columns(self):
        migrate(self.path)
        cols = self._cols()
        for c in NEW_COLS:
            self.assertIn(c, cols)

    def test_idempotent_second_run_does_not_raise(self):
        migrate(self.path)
        migrate(self.path)  # must not raise
        self.assertIn('quality_score', self._cols())
