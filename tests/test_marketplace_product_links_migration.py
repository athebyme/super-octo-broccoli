# -*- coding: utf-8 -*-
"""Canonical marketplace link migration is additive and idempotent."""

import sqlite3

from migrations.migrate_add_marketplace_listings import (
    apply_migration as apply_listings,
)
from migrations.migrate_add_marketplace_product_links import apply_migration
from migrations.migrate_add_ozon_references import (
    apply_migration as apply_references,
)


def _base_schema(connection):
    connection.executescript('''
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            nm_id INTEGER NOT NULL,
            vendor_code TEXT,
            title TEXT,
            created_at DATETIME,
            updated_at DATETIME
        );
        CREATE TABLE imported_products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            product_id INTEGER REFERENCES products(id)
        );
    ''')


def test_link_migration_backfills_existing_relationship_and_is_idempotent():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        _base_schema(connection)
        apply_references(connection, verbose=False)
        connection.executescript('''
            INSERT INTO users(id) VALUES (1);
            INSERT INTO sellers(id) VALUES (1);
            INSERT INTO products(
                id, seller_id, nm_id, vendor_code, title, created_at, updated_at
            ) VALUES (
                7, 1, 7007, 'same-offer', 'Shared',
                '2026-07-01 10:00:00', '2026-07-02 10:00:00'
            );
            INSERT INTO imported_products(id, seller_id, product_id)
            VALUES (9, 1, 7);
        ''')
        apply_listings(connection, verbose=False)

        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)
        row = connection.execute('''
            SELECT imported_product_id, link_status, link_source,
                   link_version, linked_at, link_evidence_json
            FROM marketplace_listings
        ''').fetchone()
        events = [tuple(item) for item in connection.execute('''
            SELECT action, source, imported_product_id, link_version
            FROM marketplace_listing_link_events
        ''').fetchall()]
        columns = {
            item[1]
            for item in connection.execute(
                "PRAGMA table_info(marketplace_listings)"
            ).fetchall()
        }
        indexes = {
            item[1]
            for item in connection.execute(
                "PRAGMA index_list(marketplace_listings)"
            ).fetchall()
        }
    finally:
        connection.close()

    assert first > 0
    assert second == 0
    assert row[0:4] == (9, "linked", "wb_backfill", 1)
    assert row[4] == "2026-07-02 10:00:00"
    assert row[5] == "{}"
    assert events == [("bootstrap", "wb_backfill", 9, 1)]
    assert {
        "link_status", "link_source", "link_evidence_json", "link_version",
        "linked_at", "linked_by_user_id",
    }.issubset(columns)
    assert "uq_marketplace_listing_account_canonical" in indexes


def test_link_migration_fails_fast_without_required_listing_schema():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE sellers (id INTEGER PRIMARY KEY)")
        failed = False
        try:
            apply_migration(connection, verbose=False)
        except sqlite3.OperationalError:
            failed = True
    finally:
        connection.close()
    assert failed


def test_link_migration_fails_fast_on_duplicate_account_canonical_links():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        _base_schema(connection)
        apply_references(connection, verbose=False)
        connection.executescript('''
            INSERT INTO users(id) VALUES (1);
            INSERT INTO sellers(id) VALUES (1);
            INSERT INTO imported_products(id, seller_id, product_id)
            VALUES (9, 1, NULL);
        ''')
        apply_listings(connection, verbose=False)
        marketplace_id = connection.execute(
            "SELECT id FROM marketplaces WHERE code='ozon'"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO seller_marketplace_accounts (
                id, seller_id, marketplace_id, external_account_id, label
            ) VALUES (5, 1, ?, 'synthetic', 'Synthetic')
        ''', (marketplace_id,))
        connection.execute('''
            INSERT INTO marketplace_listings (
                seller_id, marketplace_id, account_id, imported_product_id,
                offer_id, external_product_id, sync_fingerprint
            ) VALUES
                (1, ?, 5, 9, 'offer-a', '100', ?),
                (1, ?, 5, 9, 'offer-b', '200', ?)
        ''', (marketplace_id, "a" * 64, marketplace_id, "b" * 64))
        failed = False
        try:
            apply_migration(connection, verbose=False)
        except sqlite3.OperationalError:
            failed = True
    finally:
        connection.close()
    assert failed
