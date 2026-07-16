# -*- coding: utf-8 -*-
"""P7 auto-publish migration preserves WB history and isolates accounts."""

import sqlite3

import pytest

from migrations.migrate_add_marketplace_auto_publish import migrate


def _legacy_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE seller_marketplace_accounts (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id)
        );
        CREATE TABLE imported_products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id)
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id)
        );
        CREATE TABLE marketplace_product_drafts (id INTEGER PRIMARY KEY);
        CREATE TABLE marketplace_operations (id INTEGER PRIMARY KEY);
        CREATE TABLE marketplace_listings (id INTEGER PRIMARY KEY);
        CREATE TABLE auto_publish_settings (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL UNIQUE,
            is_enabled BOOLEAN NOT NULL DEFAULT 0,
            marketplace_code VARCHAR(50) NOT NULL DEFAULT 'wb',
            check_interval_minutes INTEGER NOT NULL DEFAULT 30,
            last_run_at DATETIME,
            next_run_at DATETIME,
            batch_size INTEGER NOT NULL DEFAULT 10,
            max_daily_publishes INTEGER NOT NULL DEFAULT 100,
            daily_published_count INTEGER NOT NULL DEFAULT 0,
            daily_count_reset_at DATETIME,
            validation_mode VARCHAR(20) NOT NULL DEFAULT 'strict',
            max_retries_per_product INTEGER NOT NULL DEFAULT 3,
            retry_delay_minutes INTEGER NOT NULL DEFAULT 60,
            failure_threshold INTEGER NOT NULL DEFAULT 5,
            is_paused BOOLEAN NOT NULL DEFAULT 0,
            paused_reason TEXT,
            paused_at DATETIME,
            supplier_ids_json TEXT,
            notify_on_success BOOLEAN NOT NULL DEFAULT 0,
            notify_on_failure BOOLEAN NOT NULL DEFAULT 1,
            notify_on_pause BOOLEAN NOT NULL DEFAULT 1,
            run_lock_token VARCHAR(64),
            created_at DATETIME NOT NULL,
            updated_at DATETIME
        );
        CREATE TABLE auto_publish_runs (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            run_uid VARCHAR(36) NOT NULL UNIQUE,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            triggered_by VARCHAR(20),
            started_at DATETIME,
            completed_at DATETIME,
            duration_seconds REAL,
            total_candidates INTEGER DEFAULT 0,
            total_validated INTEGER DEFAULT 0,
            total_published INTEGER DEFAULT 0,
            total_failed INTEGER DEFAULT 0,
            total_skipped INTEGER DEFAULT 0,
            error_summary TEXT,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE auto_publish_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES auto_publish_runs(id),
            imported_product_id INTEGER NOT NULL REFERENCES imported_products(id),
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            step VARCHAR(30),
            status VARCHAR(20),
            wb_nm_id INTEGER,
            product_id INTEGER REFERENCES products(id),
            error_message TEXT,
            error_step VARCHAR(30),
            error_history_json TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at DATETIME,
            validation_result_json TEXT,
            started_at DATETIME,
            completed_at DATETIME,
            created_at DATETIME NOT NULL
        );
        INSERT INTO sellers(id) VALUES (1);
        INSERT INTO seller_marketplace_accounts(id, seller_id) VALUES (100, 1), (101, 1);
        INSERT INTO imported_products(id, seller_id) VALUES (10, 1);
        INSERT INTO products(id, seller_id) VALUES (20, 1);
        INSERT INTO auto_publish_settings (
            id, seller_id, is_enabled, marketplace_code,
            daily_published_count, created_at
        ) VALUES (7, 1, 1, 'wb', 4, '2026-07-15T00:00:00');
        INSERT INTO auto_publish_runs (
            id, seller_id, run_uid, status, total_published, created_at
        ) VALUES (8, 1, 'legacy-run-000000000000000000000001', 'completed', 1,
                  '2026-07-15T00:01:00');
        INSERT INTO auto_publish_items (
            id, run_id, imported_product_id, seller_id, step, status,
            wb_nm_id, product_id, error_history_json, created_at
        ) VALUES (9, 8, 10, 1, 'completed', 'completed', 123456, 20, '[]',
                  '2026-07-15T00:02:00');
    """)
    connection.commit()


def test_marketplace_auto_publish_migration_preserves_and_isolates(tmp_path):
    database = tmp_path / "auto-publish.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _legacy_schema(connection)
    finally:
        connection.close()

    migrate(str(database))
    migrate(str(database))

    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        settings = connection.execute("""
            SELECT id, seller_id, marketplace_code, account_id,
                   daily_published_count, run_lock_token
            FROM auto_publish_settings WHERE id=7
        """).fetchone()
        run = connection.execute("""
            SELECT settings_id, marketplace_code, account_id, total_published
            FROM auto_publish_runs WHERE id=8
        """).fetchone()
        item = connection.execute("""
            SELECT marketplace_code, account_id, wb_nm_id, product_id,
                   draft_id, operation_id, listing_id
            FROM auto_publish_items WHERE id=9
        """).fetchone()

        connection.execute("""
            INSERT INTO auto_publish_settings (
                seller_id, account_id, marketplace_code, created_at
            ) VALUES (1, 100, 'ozon', CURRENT_TIMESTAMP)
        """)
        connection.execute("""
            INSERT INTO auto_publish_settings (
                seller_id, account_id, marketplace_code, created_at
            ) VALUES (1, 101, 'ozon', CURRENT_TIMESTAMP)
        """)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO auto_publish_settings (
                    seller_id, account_id, marketplace_code, created_at
                ) VALUES (1, 100, 'ozon', CURRENT_TIMESTAMP)
            """)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO auto_publish_settings (
                    seller_id, account_id, marketplace_code, created_at
                ) VALUES (1, NULL, 'ozon', CURRENT_TIMESTAMP)
            """)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO auto_publish_settings (
                    seller_id, account_id, marketplace_code, created_at
                ) VALUES (1, NULL, 'wb', CURRENT_TIMESTAMP)
            """)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert settings == (7, 1, "wb", None, 4, None)
    assert run == (7, "wb", None, 1)
    assert item == ("wb", None, 123456, 20, None, None, None)
    assert violations == []


def test_migration_preserves_unrelated_legacy_fk_violation(tmp_path):
    database = tmp_path / "auto-publish-with-legacy-orphan.db"
    connection = sqlite3.connect(database)
    try:
        _legacy_schema(connection)
        connection.executescript("""
            CREATE TABLE legacy_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE legacy_child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER REFERENCES legacy_parent(id)
            );
            INSERT INTO legacy_child(id, parent_id) VALUES (1, 404);
        """)
        connection.commit()
    finally:
        connection.close()

    migrate(str(database))
    migrate(str(database))

    connection = sqlite3.connect(database)
    try:
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(auto_publish_settings)"
            ).fetchall()
        }
    finally:
        connection.close()

    assert violations == [("legacy_child", 1, "legacy_parent", 0)]
    assert {"account_id", "marketplace_code"} <= columns
