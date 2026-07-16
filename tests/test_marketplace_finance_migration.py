"""P9B Ozon finance schema invariants."""

import sqlite3

import pytest

from migrations.migrate_add_marketplace_finance import apply_migration


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
        CREATE TABLE marketplace_postings (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER REFERENCES seller_marketplace_accounts(id)
        );
    """)


def test_marketplace_finance_migration_is_idempotent_and_constrained():
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
            INSERT INTO marketplace_postings(id, seller_id, marketplace_id, account_id)
                VALUES (3000, 1, 10, 100);
            INSERT INTO marketplace_finance_syncs (
                id, seller_id, marketplace_id, account_id, period_code,
                period_start, period_end, status, phase, current_date,
                request_fingerprint
            ) VALUES (
                500, 1, 10, 100, '7d', '2026-07-09', '2026-07-15',
                'running', 'accruals', '2026-07-15',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            );
            INSERT INTO marketplace_finance_facts (
                id, sync_id, seller_id, marketplace_id, account_id, posting_id,
                accrual_id, fact_date, accrued_category, total_amount, currency,
                amount_sign, source_fingerprint, observed_at
            ) VALUES (
                700, 500, 1, 10, 100, 3000, 'accrual-1', '2026-07-15',
                'POSTING', -10.25, 'RUB', 'negative',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                '2026-07-15 12:00:00'
            );
            INSERT INTO marketplace_finance_fact_items (
                fact_id, seller_id, account_id, listing_id, external_sku,
                match_status
            ) VALUES (700, 1, 100, 1000, '101', 'matched');
            INSERT INTO marketplace_finance_components (
                fact_id, seller_id, account_id, listing_id, component_key,
                component_kind, external_type_id, external_sku, amount, currency
            ) VALUES (
                700, 1, 100, 1000,
                'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                'delivery_service', 7, '101', -5, 'RUB'
            );
        """)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_finance_syncs (
                    seller_id, marketplace_id, account_id, period_code,
                    period_start, period_end, current_date, request_fingerprint
                ) VALUES (
                    1, 10, 100, '30d', '2026-06-16', '2026-07-15',
                    '2026-06-16',
                    'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
                )
            """)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_finance_facts (
                    sync_id, seller_id, marketplace_id, account_id, accrual_id,
                    fact_date, accrued_category, total_amount, currency,
                    amount_sign, source_fingerprint, observed_at
                ) VALUES (
                    500, 1, 10, 100, 'bad-sign', '2026-07-15', 'ITEM',
                    -1, 'RUB', 'positive',
                    'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                    '2026-07-15 12:00:00'
                )
            """)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_finance_components (
                    fact_id, seller_id, account_id, component_key,
                    component_kind, external_type_id, amount, currency
                ) VALUES (
                    700, 1, 100,
                    'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
                    'commission_snapshot', 7, -1, 'RUB'
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
        "marketplace_finance_syncs",
        "marketplace_finance_accrual_types",
        "marketplace_finance_facts",
        "marketplace_finance_fact_items",
        "marketplace_finance_components",
    }.issubset(tables)
    assert violations == []
