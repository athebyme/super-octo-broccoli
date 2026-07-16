"""P9 Ozon fulfillment schema invariants."""

import sqlite3

import pytest

from migrations.migrate_add_marketplace_fulfillment import apply_migration


def _prerequisites(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE marketplaces (id INTEGER PRIMARY KEY, code VARCHAR(50));
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
    """)


def test_marketplace_fulfillment_migration_is_idempotent_and_constrained():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _prerequisites(connection)
        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)
        connection.executescript("""
            INSERT INTO sellers(id) VALUES (1), (2);
            INSERT INTO marketplaces(id, code) VALUES (10, 'ozon');
            INSERT INTO seller_marketplace_accounts(id, seller_id, marketplace_id)
                VALUES (100, 1, 10), (200, 2, 10);
            INSERT INTO marketplace_listings(id, seller_id, marketplace_id, account_id)
                VALUES (1000, 1, 10, 100), (2000, 2, 10, 200);
            INSERT INTO marketplace_fulfillment_syncs (
                id, seller_id, marketplace_id, account_id, period_code,
                period_start, period_end, request_fingerprint
            ) VALUES (
                500, 1, 10, 100, '30d', '2026-06-16', '2026-07-15',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            );
            INSERT INTO marketplace_postings (
                id, seller_id, marketplace_id, account_id, last_sync_id,
                posting_number, fulfillment_kind, status, source_endpoint,
                sync_fingerprint, last_seen_at
            ) VALUES (
                700, 1, 10, 100, 500, '100-1-1', 'fbs', 'delivered',
                '/v4/posting/fbs/list',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                '2026-07-15 12:00:00'
            );
            INSERT INTO marketplace_posting_items (
                posting_id, seller_id, account_id, listing_id, identity_key,
                offer_id, external_sku, quantity
            ) VALUES (
                700, 1, 100, 1000,
                'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                'offer-1', '123', 1
            );
        """)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_fulfillment_syncs (
                    seller_id, marketplace_id, account_id, period_code,
                    period_start, period_end, request_fingerprint
                ) VALUES (
                    1, 10, 100, '7d', '2026-07-09', '2026-07-15',
                    'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
                )
            """)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_postings (
                    seller_id, marketplace_id, account_id, posting_number,
                    fulfillment_kind, status, source_endpoint,
                    sync_fingerprint, last_seen_at
                ) VALUES (
                    1, 10, 100, 'bad-kind', 'rfbs', 'new', '/bad',
                    'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                    '2026-07-15 12:00:00'
                )
            """)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_posting_items (
                    posting_id, seller_id, account_id, identity_key, quantity
                ) VALUES (
                    999999, 1, 100,
                    'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
                    1
                )
            """)

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert first > 0
    assert second == 0
    assert {
        "marketplace_fulfillment_syncs",
        "marketplace_postings",
        "marketplace_posting_items",
        "marketplace_posting_status_events",
        "marketplace_returns",
        "marketplace_cancellations",
    }.issubset(tables)
    assert violations == []
