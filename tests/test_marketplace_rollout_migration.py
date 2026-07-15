# -*- coding: utf-8 -*-
"""P11 rollout schema is additive, repeatable and never scans Product data."""

import sqlite3

from migrations.migrate_add_marketplace_accounts import (
    apply_migration as apply_accounts,
)
from migrations.migrate_add_marketplace_listings import (
    apply_migration as apply_listings,
)
from migrations.migrate_add_marketplace_rollout import apply_migration
from migrations.migrate_add_ozon_references import (
    apply_migration as apply_references,
)


def _schema(connection):
    connection.execute("CREATE TABLE sellers (id INTEGER PRIMARY KEY)")
    connection.execute('''
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL,
            nm_id INTEGER NOT NULL
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
    apply_listings(connection, verbose=False, backfill_limit=0)


def test_rollout_migration_is_idempotent_and_adds_active_run_constraint():
    connection = sqlite3.connect(":memory:")
    try:
        _schema(connection)
        connection.execute("INSERT INTO sellers(id) VALUES (1)")
        wb_id = connection.execute(
            "SELECT id FROM marketplaces WHERE code='wb'"
        ).fetchone()[0]
        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(marketplace_projection_runs)"
            ).fetchall()
        }
        connection.execute('''
            INSERT INTO marketplace_projection_runs (
                seller_id, marketplace_id, run_kind, status,
                target_product_id
            ) VALUES (?, ?, 'wb_backfill', 'running', 10)
        ''', (1, wb_id))
        try:
            connection.execute('''
                INSERT INTO marketplace_projection_runs (
                    seller_id, marketplace_id, run_kind, status,
                    target_product_id
                ) VALUES (?, ?, 'wb_backfill', 'pending', 10)
            ''', (1, wb_id))
        except sqlite3.IntegrityError:
            duplicate_blocked = True
        else:
            duplicate_blocked = False
    finally:
        connection.close()

    assert first > 0
    assert second == 0
    assert duplicate_blocked
    assert {
        "run_kind", "cursor_product_id", "target_product_id",
        "lease_owner", "lease_expires_at", "mismatch_fields_json",
        "mismatch_sample_json",
    }.issubset(columns)
