# -*- coding: utf-8 -*-
"""P8 marketplace quality/analytics schema and account isolation constraints."""

import sqlite3

import pytest

from migrations.migrate_add_marketplace_quality_analytics import apply_migration


def _prerequisite_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE sellers (
            id INTEGER PRIMARY KEY
        );
        CREATE TABLE marketplaces (
            id INTEGER PRIMARY KEY,
            code VARCHAR(50) NOT NULL UNIQUE
        );
        CREATE TABLE seller_marketplace_accounts (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id)
        );
        CREATE TABLE marketplace_listings (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER REFERENCES seller_marketplace_accounts(id)
        );
        CREATE TABLE marketplace_attribute_definitions (
            id INTEGER PRIMARY KEY,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id)
        );
    """)


def test_quality_analytics_migration_is_idempotent_and_account_scoped():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _prerequisite_schema(connection)

        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        attribute_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(marketplace_attribute_definitions)"
            ).fetchall()
        }

        connection.executescript("""
            INSERT INTO sellers(id) VALUES (1), (2);
            INSERT INTO marketplaces(id, code) VALUES (10, 'ozon');
            INSERT INTO seller_marketplace_accounts(id, seller_id, marketplace_id)
                VALUES (100, 1, 10), (200, 2, 10);
            INSERT INTO marketplace_listings(id, seller_id, marketplace_id, account_id)
                VALUES (1000, 1, 10, 100), (2000, 2, 10, 200);
            INSERT INTO marketplace_analytics_syncs (
                id, seller_id, marketplace_id, account_id, period_code,
                period_start, period_end, request_fingerprint
            ) VALUES (
                500, 1, 10, 100, '30d', '2026-06-16', '2026-07-15',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            );
        """)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_analytics_syncs (
                    seller_id, marketplace_id, account_id, period_code,
                    period_start, period_end, request_fingerprint
                ) VALUES (
                    1, 10, 100, '30d', '2026-06-16', '2026-07-15',
                    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
                )
            """)

        connection.executescript("""
            INSERT INTO marketplace_metric_facts (
                sync_id, seller_id, marketplace_id, account_id, listing_id,
                dimension_kind, dimension_id, metric_code, provider_metric,
                metric_value, unit, definition_code
            ) VALUES (
                500, 1, 10, 100, 1000, 'listing', 'sku:777',
                'ordered_units', 'ordered_units', 3, 'count',
                'ozon.analytics.v1/ordered_units'
            );
            INSERT INTO marketplace_quality_assessments (
                seller_id, marketplace_id, account_id, listing_id,
                analytics_sync_id, status, severity, score, impact,
                listing_fingerprint
            ) VALUES (
                1, 10, 100, 1000, 500, 'scored', 'good', 82.5, 17.5,
                'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
            );
        """)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_quality_assessments (
                    seller_id, marketplace_id, account_id, listing_id,
                    status, severity, score, impact, listing_fingerprint
                ) VALUES (
                    1, 10, 100, 1000, 'scored', 'excellent', 101, 0,
                    'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
                )
            """)

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert first > 0
    assert second == 0
    assert {
        "marketplace_analytics_syncs",
        "marketplace_metric_facts",
        "marketplace_quality_assessments",
    }.issubset(tables)
    assert "is_filterable" in attribute_columns
    assert violations == []
