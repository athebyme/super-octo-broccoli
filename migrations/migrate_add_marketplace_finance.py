#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add immutable account-scoped Ozon finance snapshots and facts."""

import os
import sqlite3
import sys


def _objects(connection: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }


def _columns(connection: sqlite3.Connection, table_name: str) -> set:
    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def _require_prerequisites(connection: sqlite3.Connection) -> None:
    required = {
        "sellers",
        "marketplaces",
        "seller_marketplace_accounts",
        "marketplace_listings",
        "marketplace_postings",
    }
    missing = sorted(required - _objects(connection))
    if missing:
        raise sqlite3.OperationalError(
            "Marketplace finance prerequisites are missing: "
            + ", ".join(missing)
        )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    _require_prerequisites(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_finance_syncs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            period_code VARCHAR(10) NOT NULL,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            phase VARCHAR(20) NOT NULL DEFAULT 'types',
            current_date DATE NOT NULL,
            next_cursor VARCHAR(100),
            page_count INTEGER NOT NULL DEFAULT 0,
            fact_count INTEGER NOT NULL DEFAULT 0,
            item_count INTEGER NOT NULL DEFAULT 0,
            component_count INTEGER NOT NULL DEFAULT 0,
            matched_item_count INTEGER NOT NULL DEFAULT 0,
            unmatched_item_count INTEGER NOT NULL DEFAULT 0,
            ambiguous_item_count INTEGER NOT NULL DEFAULT 0,
            contract_version VARCHAR(80) NOT NULL DEFAULT 'ozon-finance-accrual-v1',
            request_fingerprint VARCHAR(64) NOT NULL,
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_page_at DATETIME,
            completed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_finance_period_code CHECK (
                period_code IN ('7d','30d')
            ),
            CONSTRAINT ck_marketplace_finance_status CHECK (
                status IN ('running','completed','failed','cancelled')
            ),
            CONSTRAINT ck_marketplace_finance_phase CHECK (
                phase IN ('types','accruals','completed')
            ),
            CONSTRAINT ck_marketplace_finance_period CHECK (
                period_start <= period_end
                AND current_date >= period_start
                AND current_date <= period_end
            ),
            CONSTRAINT ck_marketplace_finance_counters CHECK (
                page_count >= 0 AND fact_count >= 0 AND item_count >= 0
                AND component_count >= 0 AND matched_item_count >= 0
                AND unmatched_item_count >= 0 AND ambiguous_item_count >= 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_finance_accrual_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            last_sync_id INTEGER REFERENCES marketplace_finance_syncs(id) ON DELETE SET NULL,
            external_type_id INTEGER NOT NULL,
            name VARCHAR(300) NOT NULL,
            description VARCHAR(2000),
            source_endpoint VARCHAR(100) NOT NULL DEFAULT '/v1/finance/accrual/types',
            last_seen_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_finance_type_account_external UNIQUE (
                account_id, external_type_id
            ),
            CONSTRAINT ck_marketplace_finance_type_positive CHECK (
                external_type_id > 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_finance_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_id INTEGER NOT NULL REFERENCES marketplace_finance_syncs(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            posting_id INTEGER REFERENCES marketplace_postings(id) ON DELETE SET NULL,
            accrual_id VARCHAR(100) NOT NULL,
            fact_date DATE NOT NULL,
            unit_number VARCHAR(200),
            accrued_category VARCHAR(25) NOT NULL,
            total_amount NUMERIC(20, 4) NOT NULL,
            currency VARCHAR(3) NOT NULL,
            amount_sign VARCHAR(10) NOT NULL,
            definition_code VARCHAR(120) NOT NULL DEFAULT 'ozon-accrual-total-amount-v1',
            source_endpoint VARCHAR(100) NOT NULL DEFAULT '/v1/finance/accrual/by-day',
            contract_version VARCHAR(80) NOT NULL DEFAULT 'ozon-finance-accrual-v1',
            source_fingerprint VARCHAR(64) NOT NULL,
            observed_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_finance_fact_sync_accrual UNIQUE (
                sync_id, accrual_id
            ),
            CONSTRAINT ck_marketplace_finance_fact_category CHECK (
                accrued_category IN ('UNSPECIFIED','POSTING','ITEM','NON_ITEM')
            ),
            CONSTRAINT ck_marketplace_finance_fact_sign CHECK (
                amount_sign IN ('positive','negative','zero')
            ),
            CONSTRAINT ck_marketplace_finance_fact_amount_sign CHECK (
                (total_amount > 0 AND amount_sign = 'positive')
                OR (total_amount < 0 AND amount_sign = 'negative')
                OR (total_amount = 0 AND amount_sign = 'zero')
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_finance_fact_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_id INTEGER NOT NULL REFERENCES marketplace_finance_facts(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            external_sku VARCHAR(100) NOT NULL,
            match_status VARCHAR(20) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_finance_fact_item_sku UNIQUE (
                fact_id, external_sku
            ),
            CONSTRAINT ck_marketplace_finance_fact_item_match CHECK (
                match_status IN ('matched','unmatched','ambiguous')
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_finance_components (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_id INTEGER NOT NULL REFERENCES marketplace_finance_facts(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            component_key VARCHAR(64) NOT NULL,
            component_kind VARCHAR(30) NOT NULL,
            external_type_id INTEGER NOT NULL,
            type_name VARCHAR(300),
            external_sku VARCHAR(100),
            amount NUMERIC(20, 4) NOT NULL,
            currency VARCHAR(3) NOT NULL,
            rollup_role VARCHAR(30) NOT NULL DEFAULT 'explanatory_only',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_finance_component_key UNIQUE (
                fact_id, component_key
            ),
            CONSTRAINT ck_marketplace_finance_component_kind CHECK (
                component_kind IN ('item_fee','non_item_fee','delivery_service')
            ),
            CONSTRAINT ck_marketplace_finance_component_type CHECK (
                external_type_id > 0
            ),
            CONSTRAINT ck_marketplace_finance_component_rollup CHECK (
                rollup_role = 'explanatory_only'
            )
        )
    ''')

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_syncs_seller_id ON marketplace_finance_syncs(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_syncs_marketplace_id ON marketplace_finance_syncs(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_syncs_account_id ON marketplace_finance_syncs(account_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_finance_running ON marketplace_finance_syncs(account_id) WHERE status = 'running'",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_finance_sync_scope ON marketplace_finance_syncs(seller_id, account_id, period_start, period_end, status)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_accrual_types_seller_id ON marketplace_finance_accrual_types(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_accrual_types_marketplace_id ON marketplace_finance_accrual_types(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_accrual_types_account_id ON marketplace_finance_accrual_types(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_accrual_types_last_sync_id ON marketplace_finance_accrual_types(last_sync_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_finance_type_scope ON marketplace_finance_accrual_types(seller_id, account_id, name)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_facts_sync_id ON marketplace_finance_facts(sync_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_facts_seller_id ON marketplace_finance_facts(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_facts_marketplace_id ON marketplace_finance_facts(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_facts_account_id ON marketplace_finance_facts(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_facts_posting_id ON marketplace_finance_facts(posting_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_finance_fact_scope_date ON marketplace_finance_facts(seller_id, account_id, fact_date, currency)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_finance_fact_unit ON marketplace_finance_facts(seller_id, account_id, unit_number)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_fact_items_fact_id ON marketplace_finance_fact_items(fact_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_fact_items_seller_id ON marketplace_finance_fact_items(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_fact_items_account_id ON marketplace_finance_fact_items(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_fact_items_listing_id ON marketplace_finance_fact_items(listing_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_finance_fact_item_listing ON marketplace_finance_fact_items(seller_id, account_id, listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_components_fact_id ON marketplace_finance_components(fact_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_components_seller_id ON marketplace_finance_components(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_components_account_id ON marketplace_finance_components(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_finance_components_listing_id ON marketplace_finance_components(listing_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_finance_component_type ON marketplace_finance_components(seller_id, account_id, external_type_id)",
    )
    for statement in statements:
        connection.execute(statement)

    expected = {
        "marketplace_finance_syncs": {
            "seller_id", "marketplace_id", "account_id", "period_code",
            "period_start", "period_end", "status", "phase", "current_date",
            "next_cursor", "request_fingerprint", "contract_version",
        },
        "marketplace_finance_accrual_types": {
            "seller_id", "marketplace_id", "account_id", "last_sync_id",
            "external_type_id", "name", "source_endpoint", "last_seen_at",
        },
        "marketplace_finance_facts": {
            "sync_id", "seller_id", "marketplace_id", "account_id",
            "posting_id", "accrual_id", "fact_date", "accrued_category",
            "total_amount", "currency", "amount_sign", "source_fingerprint",
        },
        "marketplace_finance_fact_items": {
            "fact_id", "seller_id", "account_id", "listing_id",
            "external_sku", "match_status",
        },
        "marketplace_finance_components": {
            "fact_id", "seller_id", "account_id", "listing_id",
            "component_key", "component_kind", "external_type_id",
            "amount", "currency", "rollup_role",
        },
    }
    for table_name, required_columns in expected.items():
        missing = required_columns - _columns(connection, table_name)
        if missing:
            raise sqlite3.OperationalError(
                f"{table_name} is missing columns: " + ", ".join(sorted(missing))
            )


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    before = _objects(connection)
    _ensure_schema(connection)
    after = _objects(connection)
    if verbose:
        print("Marketplace finance migration completed successfully!")
    return len(after - before)


def migrate(db_path: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        apply_migration(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    database = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("DATABASE_PATH", "data/seller_platform.db")
    )
    migrate(database)
