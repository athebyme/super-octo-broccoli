# -*- coding: utf-8 -*-
"""Marketplace listing schema and idempotent WB compatibility backfill."""

import json
import sqlite3

from migrations.migrate_add_marketplace_accounts import (
    apply_migration as apply_accounts,
)
from migrations.migrate_add_marketplace_listings import apply_migration
from migrations.migrate_add_ozon_references import (
    apply_migration as apply_references,
)


def _legacy_schema(connection):
    connection.execute("CREATE TABLE sellers (id INTEGER PRIMARY KEY)")
    connection.execute('''
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL,
            nm_id INTEGER NOT NULL,
            imt_id INTEGER,
            vendor_code TEXT,
            title TEXT,
            description TEXT,
            subject_id INTEGER,
            is_active BOOLEAN,
            photos_json TEXT,
            characteristics_json TEXT,
            dimensions_json TEXT,
            price NUMERIC,
            discount_price NUMERIC,
            quantity INTEGER,
            last_sync DATETIME,
            created_at DATETIME,
            updated_at DATETIME
        )
    ''')
    connection.execute('''
        CREATE TABLE imported_products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL,
            product_id INTEGER
        )
    ''')
    apply_accounts(connection, verbose=False)
    apply_references(connection, verbose=False)


def test_listing_migration_is_idempotent_and_backfills_wb_without_fake_account():
    connection = sqlite3.connect(":memory:")
    try:
        _legacy_schema(connection)
        connection.execute("INSERT INTO sellers(id) VALUES (1)")
        connection.execute('''
            INSERT INTO products (
                id, seller_id, nm_id, imt_id, vendor_code, title, description,
                subject_id, is_active, photos_json, characteristics_json,
                dimensions_json, price, discount_price, quantity,
                last_sync, created_at, updated_at
            ) VALUES (
                7, 1, 1234567890123, 987, 'seller-offer', 'Товар WB',
                'Описание', 42, 1, '["https://img.test/1.jpg"]',
                '[{"id": 1, "value": "x"}]', '{"width": 10}',
                1500.50, 1200.25, 3,
                '2026-07-15 10:00:00', '2026-07-01 09:00:00',
                '2026-07-15 10:00:00'
            )
        ''')
        connection.execute(
            "INSERT INTO imported_products(id, seller_id, product_id) VALUES (9, 1, 7)"
        )

        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)
        rows = connection.execute('''
            SELECT account_id, legacy_product_id, imported_product_id, offer_id,
                   external_product_id, normalized_status, identifiers_json,
                   media_json, price_summary_json, stock_summary_json,
                   sync_fingerprint
            FROM marketplace_listings
        ''').fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(marketplace_catalog_syncs)"
            ).fetchall()
        }
    finally:
        connection.close()

    assert first > 0
    assert second == 0
    assert len(rows) == 1
    row = rows[0]
    assert row[0] is None
    assert row[1:6] == (
        7,
        9,
        "seller-offer",
        "1234567890123",
        "active",
    )
    assert json.loads(row[6]) == {
        "imt_id": "987",
        "nm_id": "1234567890123",
    }
    assert json.loads(row[7])["photos"] == ["https://img.test/1.jpg"]
    assert json.loads(row[8])["currency"] == "RUB"
    assert json.loads(row[9])["present"] == 3
    assert len(row[10]) == 64
    assert {"marketplace_catalog_syncs", "marketplace_listings"}.issubset(tables)
    assert "uq_marketplace_catalog_sync_running" in indexes


def test_wb_backfill_has_stable_offer_fallback_and_bounds_invalid_legacy_json():
    connection = sqlite3.connect(":memory:")
    try:
        _legacy_schema(connection)
        connection.execute("INSERT INTO sellers(id) VALUES (1)")
        connection.execute('''
            INSERT INTO products (
                id, seller_id, nm_id, vendor_code, is_active, photos_json,
                characteristics_json, dimensions_json
            ) VALUES (1, 1, 55, '', 0, 'broken', '{}', '[]')
        ''')
        apply_migration(connection, verbose=False)
        row = connection.execute('''
            SELECT offer_id, normalized_status, media_json, attributes_json,
                   dimensions_json
            FROM marketplace_listings
        ''').fetchone()
    finally:
        connection.close()

    assert row[0] == "wb-nm-55"
    assert row[1] == "inactive"
    assert json.loads(row[2]) == {"photos": []}
    assert json.loads(row[3]) == []
    assert json.loads(row[4]) == {}


def test_startup_backfill_is_bounded_and_resumes_by_missing_keyset():
    connection = sqlite3.connect(":memory:")
    try:
        _legacy_schema(connection)
        connection.execute("INSERT INTO sellers(id) VALUES (1)")
        connection.executemany(
            "INSERT INTO products(id, seller_id, nm_id, is_active) "
            "VALUES (?, 1, ?, 1)",
            [(index, 900_000 + index) for index in range(1, 206)],
        )
        first = apply_migration(
            connection,
            verbose=False,
            backfill_limit=200,
        )
        first_count = connection.execute(
            "SELECT COUNT(*) FROM marketplace_listings"
        ).fetchone()[0]
        second = apply_migration(
            connection,
            verbose=False,
            backfill_limit=200,
        )
        second_count = connection.execute(
            "SELECT COUNT(*) FROM marketplace_listings"
        ).fetchone()[0]
        third = apply_migration(
            connection,
            verbose=False,
            backfill_limit=200,
        )
    finally:
        connection.close()

    assert first > 0
    assert first_count == 200
    assert second == 5
    assert second_count == 205
    assert third == 0


def test_direct_migration_keeps_the_deployed_full_backfill_default():
    connection = sqlite3.connect(":memory:")
    try:
        _legacy_schema(connection)
        connection.execute("INSERT INTO sellers(id) VALUES (1)")
        connection.executemany(
            "INSERT INTO products(id, seller_id, nm_id, is_active) "
            "VALUES (?, 1, ?, 1)",
            [(index, 910_000 + index) for index in range(1, 206)],
        )
        apply_migration(connection, verbose=False)
        count = connection.execute(
            "SELECT COUNT(*) FROM marketplace_listings"
        ).fetchone()[0]
    finally:
        connection.close()

    assert count == 205
