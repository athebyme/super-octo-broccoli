#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add marketplace-scoped quality assessments and analytics metric facts."""

import os
import sqlite3
import sys


def _schema_objects(connection: sqlite3.Connection) -> set:
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
        "marketplace_attribute_definitions",
    }
    missing = sorted(required - _schema_objects(connection))
    if missing:
        raise sqlite3.OperationalError(
            "Marketplace quality/analytics prerequisites are missing: "
            + ", ".join(missing)
        )


def _ensure_filterable_attribute(connection: sqlite3.Connection) -> None:
    if "is_filterable" not in _columns(
        connection,
        "marketplace_attribute_definitions",
    ):
        connection.execute(
            "ALTER TABLE marketplace_attribute_definitions "
            "ADD COLUMN is_filterable BOOLEAN NOT NULL DEFAULT 0"
        )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    _require_prerequisites(connection)
    _ensure_filterable_attribute(connection)

    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_analytics_syncs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            period_code VARCHAR(10) NOT NULL,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            phase VARCHAR(20) NOT NULL DEFAULT 'product',
            next_offset INTEGER NOT NULL DEFAULT 0,
            page_count INTEGER NOT NULL DEFAULT 0,
            row_count INTEGER NOT NULL DEFAULT 0,
            matched_rows INTEGER NOT NULL DEFAULT 0,
            unmatched_rows INTEGER NOT NULL DEFAULT 0,
            fact_count INTEGER NOT NULL DEFAULT 0,
            request_fingerprint VARCHAR(64) NOT NULL,
            contract_version VARCHAR(80) NOT NULL DEFAULT 'ozon-analytics-v1',
            metrics_json TEXT NOT NULL DEFAULT '[]',
            totals_json TEXT NOT NULL DEFAULT '{}',
            response_timestamp VARCHAR(80),
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_page_at DATETIME,
            completed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_analytics_period_code CHECK (
                period_code IN ('7d','30d')
            ),
            CONSTRAINT ck_marketplace_analytics_status CHECK (
                status IN ('running','completed','failed','cancelled')
            ),
            CONSTRAINT ck_marketplace_analytics_phase CHECK (
                phase IN ('product','day','completed')
            ),
            CONSTRAINT ck_marketplace_analytics_period CHECK (
                period_start <= period_end
            ),
            CONSTRAINT ck_marketplace_analytics_counters CHECK (
                next_offset >= 0 AND page_count >= 0 AND row_count >= 0
                AND matched_rows >= 0 AND unmatched_rows >= 0
                AND fact_count >= 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_metric_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_id INTEGER NOT NULL REFERENCES marketplace_analytics_syncs(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            dimension_kind VARCHAR(20) NOT NULL,
            dimension_id VARCHAR(100) NOT NULL,
            dimension_name VARCHAR(500),
            fact_date DATE,
            metric_code VARCHAR(60) NOT NULL,
            provider_metric VARCHAR(60) NOT NULL,
            metric_value NUMERIC(20, 4) NOT NULL,
            unit VARCHAR(20) NOT NULL,
            definition_code VARCHAR(120) NOT NULL,
            cross_marketplace_comparable BOOLEAN NOT NULL DEFAULT 0,
            source_endpoint VARCHAR(100) NOT NULL DEFAULT '/v1/analytics/data',
            observed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_metric_fact UNIQUE (
                sync_id, dimension_kind, dimension_id, metric_code
            ),
            CONSTRAINT ck_marketplace_metric_dimension_kind CHECK (
                dimension_kind IN ('listing','day')
            ),
            CONSTRAINT ck_marketplace_metric_dimension_date CHECK (
                (dimension_kind = 'listing' AND fact_date IS NULL)
                OR (dimension_kind = 'day' AND fact_date IS NOT NULL)
            ),
            CONSTRAINT ck_marketplace_metric_unit CHECK (
                unit IN ('count','rub','percent')
            ),
            CONSTRAINT ck_marketplace_metric_nonnegative CHECK (
                metric_value >= 0
            ),
            CONSTRAINT ck_marketplace_metric_comparable CHECK (
                cross_marketplace_comparable IN (0, 1)
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_quality_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            listing_id INTEGER NOT NULL REFERENCES marketplace_listings(id) ON DELETE CASCADE,
            analytics_sync_id INTEGER REFERENCES marketplace_analytics_syncs(id) ON DELETE SET NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'unscorable',
            severity VARCHAR(20) NOT NULL DEFAULT 'critical',
            score REAL,
            impact REAL NOT NULL DEFAULT 0,
            schema_hash VARCHAR(64),
            listing_fingerprint VARCHAR(64) NOT NULL,
            definition_version VARCHAR(80) NOT NULL DEFAULT 'marketplace-quality-v1',
            breakdown_json TEXT NOT NULL DEFAULT '{}',
            reasons_json TEXT NOT NULL DEFAULT '[]',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            evaluated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_quality_listing UNIQUE (listing_id),
            CONSTRAINT ck_marketplace_quality_status CHECK (
                status IN ('scored','schema_stale','unscorable')
            ),
            CONSTRAINT ck_marketplace_quality_severity CHECK (
                severity IN ('critical','warning','good','excellent')
            ),
            CONSTRAINT ck_marketplace_quality_score CHECK (
                score IS NULL OR (score >= 0 AND score <= 100)
            ),
            CONSTRAINT ck_marketplace_quality_impact CHECK (impact >= 0)
        )
    ''')

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_analytics_syncs_seller_id "
        "ON marketplace_analytics_syncs(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_analytics_syncs_marketplace_id "
        "ON marketplace_analytics_syncs(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_analytics_syncs_account_id "
        "ON marketplace_analytics_syncs(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_analytics_scope_period "
        "ON marketplace_analytics_syncs("
        "seller_id, account_id, period_start, period_end, status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_analytics_active_scope "
        "ON marketplace_analytics_syncs(account_id, period_code) "
        "WHERE status = 'running'",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_metric_facts_sync_id "
        "ON marketplace_metric_facts(sync_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_metric_facts_seller_id "
        "ON marketplace_metric_facts(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_metric_facts_marketplace_id "
        "ON marketplace_metric_facts(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_metric_facts_account_id "
        "ON marketplace_metric_facts(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_metric_facts_listing_id "
        "ON marketplace_metric_facts(listing_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_metric_listing "
        "ON marketplace_metric_facts("
        "seller_id, account_id, listing_id, metric_code)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_metric_day "
        "ON marketplace_metric_facts(sync_id, fact_date, metric_code)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_quality_assessments_seller_id "
        "ON marketplace_quality_assessments(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_quality_assessments_marketplace_id "
        "ON marketplace_quality_assessments(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_quality_assessments_account_id "
        "ON marketplace_quality_assessments(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_quality_assessments_listing_id "
        "ON marketplace_quality_assessments(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_quality_assessments_analytics_sync_id "
        "ON marketplace_quality_assessments(analytics_sync_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_quality_scope "
        "ON marketplace_quality_assessments("
        "seller_id, account_id, status, severity)",
    )
    for statement in statements:
        connection.execute(statement)

    expected = {
        "marketplace_analytics_syncs": {
            "seller_id", "marketplace_id", "account_id", "period_code",
            "period_start", "period_end", "status", "phase", "next_offset",
            "request_fingerprint", "metrics_json", "totals_json",
        },
        "marketplace_metric_facts": {
            "sync_id", "seller_id", "account_id", "listing_id",
            "dimension_kind", "dimension_id", "metric_code",
            "provider_metric", "metric_value", "definition_code",
        },
        "marketplace_quality_assessments": {
            "seller_id", "marketplace_id", "account_id", "listing_id",
            "analytics_sync_id", "status", "severity", "score", "impact",
            "schema_hash", "listing_fingerprint", "reasons_json",
        },
    }
    for table_name, required_columns in expected.items():
        missing = required_columns - _columns(connection, table_name)
        if missing:
            raise sqlite3.OperationalError(
                f"{table_name} is missing columns: "
                + ", ".join(sorted(missing))
            )


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    before = _schema_objects(connection)
    _ensure_schema(connection)
    after = _schema_objects(connection)
    if verbose:
        print("Marketplace quality/analytics migration completed successfully!")
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
