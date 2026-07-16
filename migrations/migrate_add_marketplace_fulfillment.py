#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add account-scoped Ozon postings, returns and cancellation projections."""

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
    }
    missing = sorted(required - _objects(connection))
    if missing:
        raise sqlite3.OperationalError(
            "Marketplace fulfillment prerequisites are missing: "
            + ", ".join(missing)
        )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    _require_prerequisites(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_fulfillment_syncs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            period_code VARCHAR(10) NOT NULL,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            phase VARCHAR(40) NOT NULL DEFAULT 'fbs_postings',
            next_offset INTEGER NOT NULL DEFAULT 0,
            next_cursor VARCHAR(100) NOT NULL DEFAULT '0',
            page_count INTEGER NOT NULL DEFAULT 0,
            posting_count INTEGER NOT NULL DEFAULT 0,
            return_count INTEGER NOT NULL DEFAULT 0,
            cancellation_count INTEGER NOT NULL DEFAULT 0,
            matched_item_count INTEGER NOT NULL DEFAULT 0,
            unmatched_item_count INTEGER NOT NULL DEFAULT 0,
            contract_version VARCHAR(80) NOT NULL DEFAULT 'ozon-fulfillment-v1',
            request_fingerprint VARCHAR(64) NOT NULL,
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_page_at DATETIME,
            completed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_fulfillment_period_code CHECK (
                period_code IN ('7d','30d')
            ),
            CONSTRAINT ck_marketplace_fulfillment_status CHECK (
                status IN ('running','completed','failed','cancelled')
            ),
            CONSTRAINT ck_marketplace_fulfillment_phase CHECK (
                phase IN ('fbs_postings','fbo_postings','returns',
                    'rfbs_returns','conditional_cancellations','completed')
            ),
            CONSTRAINT ck_marketplace_fulfillment_period CHECK (
                period_start <= period_end
            ),
            CONSTRAINT ck_marketplace_fulfillment_counters CHECK (
                next_offset >= 0 AND page_count >= 0 AND posting_count >= 0
                AND return_count >= 0 AND cancellation_count >= 0
                AND matched_item_count >= 0 AND unmatched_item_count >= 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_postings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            last_sync_id INTEGER REFERENCES marketplace_fulfillment_syncs(id) ON DELETE SET NULL,
            posting_number VARCHAR(200) NOT NULL,
            external_order_id VARCHAR(200),
            external_order_number VARCHAR(200),
            fulfillment_kind VARCHAR(20) NOT NULL,
            status VARCHAR(120) NOT NULL,
            substatus VARCHAR(120),
            upstream_created_at DATETIME,
            shipment_at DATETIME,
            delivered_at DATETIME,
            cancelled_at DATETIME,
            cancellation_reason_code VARCHAR(100),
            cancellation_reason VARCHAR(500),
            source_endpoint VARCHAR(100) NOT NULL,
            sync_fingerprint VARCHAR(64) NOT NULL,
            last_seen_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_posting_account_number UNIQUE (
                account_id, posting_number
            ),
            CONSTRAINT ck_marketplace_posting_fulfillment_kind CHECK (
                fulfillment_kind IN ('fbo','fbs')
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_posting_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            posting_id INTEGER NOT NULL REFERENCES marketplace_postings(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            identity_key VARCHAR(64) NOT NULL,
            offer_id VARCHAR(200),
            external_sku VARCHAR(100),
            name VARCHAR(500),
            quantity INTEGER NOT NULL,
            unit_price NUMERIC(20, 4),
            currency VARCHAR(3),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_posting_item_identity UNIQUE (
                posting_id, identity_key
            ),
            CONSTRAINT ck_marketplace_posting_item_quantity CHECK (quantity > 0),
            CONSTRAINT ck_marketplace_posting_item_price CHECK (
                unit_price IS NULL OR unit_price >= 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_posting_status_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            posting_id INTEGER NOT NULL REFERENCES marketplace_postings(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            status VARCHAR(120) NOT NULL,
            substatus VARCHAR(120),
            event_fingerprint VARCHAR(64) NOT NULL,
            observed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_posting_status_event UNIQUE (
                posting_id, event_fingerprint
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_returns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            last_sync_id INTEGER REFERENCES marketplace_fulfillment_syncs(id) ON DELETE SET NULL,
            posting_id INTEGER REFERENCES marketplace_postings(id) ON DELETE SET NULL,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            source_kind VARCHAR(20) NOT NULL,
            external_return_id VARCHAR(100) NOT NULL,
            posting_number VARCHAR(200),
            external_order_id VARCHAR(200),
            fulfillment_kind VARCHAR(20) NOT NULL,
            status VARCHAR(120) NOT NULL,
            status_label VARCHAR(300),
            reason VARCHAR(500),
            upstream_created_at DATETIME,
            status_changed_at DATETIME,
            completed_at DATETIME,
            offer_id VARCHAR(200),
            external_sku VARCHAR(100),
            product_name VARCHAR(500),
            quantity INTEGER NOT NULL DEFAULT 1,
            unit_price NUMERIC(20, 4),
            currency VARCHAR(3),
            source_endpoint VARCHAR(100) NOT NULL,
            sync_fingerprint VARCHAR(64) NOT NULL,
            last_seen_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_return_account_source UNIQUE (
                account_id, source_kind, external_return_id
            ),
            CONSTRAINT ck_marketplace_return_source_kind CHECK (
                source_kind IN ('fbo_fbs','rfbs')
            ),
            CONSTRAINT ck_marketplace_return_fulfillment_kind CHECK (
                fulfillment_kind IN ('fbo','fbs','rfbs','unknown')
            ),
            CONSTRAINT ck_marketplace_return_quantity CHECK (quantity > 0),
            CONSTRAINT ck_marketplace_return_price CHECK (
                unit_price IS NULL OR unit_price >= 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_cancellations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            last_sync_id INTEGER REFERENCES marketplace_fulfillment_syncs(id) ON DELETE SET NULL,
            posting_id INTEGER REFERENCES marketplace_postings(id) ON DELETE SET NULL,
            source_kind VARCHAR(30) NOT NULL,
            external_cancellation_id VARCHAR(200) NOT NULL,
            posting_number VARCHAR(200) NOT NULL,
            status VARCHAR(120) NOT NULL,
            status_label VARCHAR(300),
            initiator VARCHAR(80),
            reason_code VARCHAR(100),
            reason VARCHAR(500),
            requested_at DATETIME,
            resolved_at DATETIME,
            source_endpoint VARCHAR(100) NOT NULL,
            sync_fingerprint VARCHAR(64) NOT NULL,
            last_seen_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_cancellation_account_source UNIQUE (
                account_id, source_kind, external_cancellation_id
            ),
            CONSTRAINT ck_marketplace_cancellation_source_kind CHECK (
                source_kind IN ('posting_fbo','posting_fbs','rfbs_conditional')
            )
        )
    ''')

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_fulfillment_syncs_seller_id ON marketplace_fulfillment_syncs(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_fulfillment_syncs_marketplace_id ON marketplace_fulfillment_syncs(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_fulfillment_syncs_account_id ON marketplace_fulfillment_syncs(account_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_fulfillment_running ON marketplace_fulfillment_syncs(account_id) WHERE status = 'running'",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_fulfillment_scope ON marketplace_fulfillment_syncs(seller_id, account_id, period_start, period_end, status)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_postings_seller_id ON marketplace_postings(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_postings_marketplace_id ON marketplace_postings(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_postings_account_id ON marketplace_postings(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_postings_last_sync_id ON marketplace_postings(last_sync_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_posting_scope_status ON marketplace_postings(seller_id, account_id, status, upstream_created_at)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_posting_items_posting_id ON marketplace_posting_items(posting_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_posting_items_seller_id ON marketplace_posting_items(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_posting_items_account_id ON marketplace_posting_items(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_posting_items_listing_id ON marketplace_posting_items(listing_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_posting_item_listing ON marketplace_posting_items(seller_id, account_id, listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_posting_status_events_posting_id ON marketplace_posting_status_events(posting_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_posting_status_events_seller_id ON marketplace_posting_status_events(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_posting_status_events_account_id ON marketplace_posting_status_events(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_posting_status_event_scope ON marketplace_posting_status_events(seller_id, account_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_returns_seller_id ON marketplace_returns(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_returns_marketplace_id ON marketplace_returns(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_returns_account_id ON marketplace_returns(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_returns_last_sync_id ON marketplace_returns(last_sync_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_returns_posting_id ON marketplace_returns(posting_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_returns_listing_id ON marketplace_returns(listing_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_return_scope_status ON marketplace_returns(seller_id, account_id, status, status_changed_at)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_return_listing ON marketplace_returns(seller_id, account_id, listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_cancellations_seller_id ON marketplace_cancellations(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_cancellations_marketplace_id ON marketplace_cancellations(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_cancellations_account_id ON marketplace_cancellations(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_cancellations_last_sync_id ON marketplace_cancellations(last_sync_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_cancellations_posting_id ON marketplace_cancellations(posting_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_cancellation_scope_status ON marketplace_cancellations(seller_id, account_id, status, requested_at)",
    )
    for statement in statements:
        connection.execute(statement)

    expected = {
        "marketplace_fulfillment_syncs": {
            "seller_id", "marketplace_id", "account_id", "period_code",
            "period_start", "period_end", "status", "phase", "next_offset",
            "next_cursor", "page_count", "contract_version", "request_fingerprint",
        },
        "marketplace_postings": {
            "seller_id", "marketplace_id", "account_id", "last_sync_id",
            "posting_number", "fulfillment_kind", "status", "source_endpoint",
            "sync_fingerprint", "last_seen_at",
        },
        "marketplace_posting_items": {
            "posting_id", "seller_id", "account_id", "listing_id",
            "identity_key", "offer_id", "external_sku", "quantity",
        },
        "marketplace_posting_status_events": {
            "posting_id", "seller_id", "account_id", "status",
            "event_fingerprint", "observed_at",
        },
        "marketplace_returns": {
            "seller_id", "marketplace_id", "account_id", "source_kind",
            "external_return_id", "posting_number", "listing_id", "status",
            "source_endpoint", "sync_fingerprint",
        },
        "marketplace_cancellations": {
            "seller_id", "marketplace_id", "account_id", "source_kind",
            "external_cancellation_id", "posting_number", "status",
            "source_endpoint", "sync_fingerprint",
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
        print("Marketplace fulfillment migration completed successfully!")
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
