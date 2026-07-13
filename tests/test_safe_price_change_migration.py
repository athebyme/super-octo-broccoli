# -*- coding: utf-8 -*-
"""Regression tests for the idempotent safe-price migration."""
import sqlite3

from migrations import migrate_add_safe_price_change


def test_backfill_sets_created_at_for_sqlalchemy_created_table(tmp_path):
    db_path = tmp_path / 'platform.db'
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        INSERT INTO sellers (id) VALUES (1);
        CREATE TABLE safe_price_change_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL UNIQUE,
            is_enabled BOOLEAN NOT NULL,
            safe_threshold_percent REAL NOT NULL,
            warning_threshold_percent REAL NOT NULL,
            mode VARCHAR(20) NOT NULL,
            require_comment_for_dangerous BOOLEAN NOT NULL,
            allow_bulk_dangerous BOOLEAN NOT NULL,
            max_products_per_batch INTEGER NOT NULL,
            allow_unlimited_batch BOOLEAN NOT NULL,
            notify_on_dangerous BOOLEAN NOT NULL,
            notify_email VARCHAR(200),
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP
        );
        """
    )
    connection.commit()
    connection.close()

    original_path = migrate_add_safe_price_change.DB_PATH
    migrate_add_safe_price_change.DB_PATH = str(db_path)
    try:
        migrate_add_safe_price_change.migrate()
        migrate_add_safe_price_change.migrate()
    finally:
        migrate_add_safe_price_change.DB_PATH = original_path

    connection = sqlite3.connect(db_path)
    row = connection.execute(
        'SELECT seller_id, created_at, updated_at FROM safe_price_change_settings'
    ).fetchone()
    count = connection.execute(
        'SELECT COUNT(*) FROM safe_price_change_settings'
    ).fetchone()[0]
    connection.close()

    assert row[0] == 1
    assert row[1]
    assert row[2]
    assert count == 1
