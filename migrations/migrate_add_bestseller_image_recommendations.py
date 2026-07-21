#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create durable admin-to-seller bestseller image recommendations."""

import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import assert_foreign_key_safety, foreign_key_snapshot
else:
    from _foreign_key_safety import (  # type: ignore[no-redef]
        assert_foreign_key_safety,
        foreign_key_snapshot,
    )


MANAGED_TABLES = {'bestseller_image_recommendations'}


def _columns(connection: sqlite3.Connection, table_name: str):
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_info({table_name})').fetchall()
    }


def _require_prerequisites(connection: sqlite3.Connection) -> None:
    required = {
        'sellers': {'id'},
        'users': {'id'},
        'imported_products': {'id', 'seller_id'},
        'products': {'id', 'seller_id'},
        'seller_marketplace_accounts': {'id', 'seller_id'},
        'marketplace_listings': {'id', 'seller_id', 'account_id'},
    }
    for table_name, columns in required.items():
        actual = _columns(connection, table_name)
        if not actual:
            raise sqlite3.OperationalError(
                f'bestseller recommendation prerequisite missing: {table_name}'
            )
        missing = columns - actual
        if missing:
            raise sqlite3.OperationalError(
                f'{table_name} is missing columns: {", ".join(sorted(missing))}'
            )


def _create_schema(connection: sqlite3.Connection) -> None:
    _require_prerequisites(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS bestseller_image_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            imported_product_id INTEGER
                REFERENCES imported_products(id) ON DELETE SET NULL,
            marketplace_code VARCHAR(20) NOT NULL,
            account_id INTEGER
                REFERENCES seller_marketplace_accounts(id) ON DELETE SET NULL,
            marketplace_listing_id INTEGER
                REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            legacy_product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            scope_key VARCHAR(220) NOT NULL,
            period_code VARCHAR(10) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'recommended',
            opportunity_score FLOAT NOT NULL,
            units FLOAT NOT NULL DEFAULT 0,
            revenue_rub FLOAT NOT NULL DEFAULT 0,
            photo_count INTEGER NOT NULL DEFAULT 0,
            quality_score FLOAT,
            source_observed_at DATETIME,
            metric_definitions_json TEXT NOT NULL DEFAULT '{}',
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            recommended_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reviewed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reviewed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_bestseller_image_recommendation_scope
                UNIQUE (seller_id, scope_key),
            CONSTRAINT ck_bestseller_image_recommendation_marketplace
                CHECK (marketplace_code IN ('wb','ozon')),
            CONSTRAINT ck_bestseller_image_recommendation_period
                CHECK (period_code IN ('7d','30d')),
            CONSTRAINT ck_bestseller_image_recommendation_status
                CHECK (status IN ('recommended','dismissed','completed')),
            CONSTRAINT ck_bestseller_image_recommendation_metrics CHECK (
                opportunity_score >= 0 AND opportunity_score <= 100
                AND units >= 0 AND revenue_rub >= 0 AND photo_count >= 0
            ),
            CONSTRAINT ck_bestseller_image_recommendation_quality CHECK (
                quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 100)
            )
        )
    ''')
    for statement in (
        'CREATE INDEX IF NOT EXISTS ix_bestseller_image_recommendations_seller_id '
        'ON bestseller_image_recommendations(seller_id)',
        'CREATE INDEX IF NOT EXISTS ix_bestseller_image_recommendations_imported_product_id '
        'ON bestseller_image_recommendations(imported_product_id)',
        'CREATE INDEX IF NOT EXISTS ix_bestseller_image_recommendations_marketplace_code '
        'ON bestseller_image_recommendations(marketplace_code)',
        'CREATE INDEX IF NOT EXISTS ix_bestseller_image_recommendations_account_id '
        'ON bestseller_image_recommendations(account_id)',
        'CREATE INDEX IF NOT EXISTS ix_bestseller_image_recommendations_marketplace_listing_id '
        'ON bestseller_image_recommendations(marketplace_listing_id)',
        'CREATE INDEX IF NOT EXISTS ix_bestseller_image_recommendations_legacy_product_id '
        'ON bestseller_image_recommendations(legacy_product_id)',
        'CREATE INDEX IF NOT EXISTS ix_bestseller_image_recommendations_status '
        'ON bestseller_image_recommendations(status)',
        'CREATE INDEX IF NOT EXISTS ix_bestseller_image_recommendations_recommended_by_user_id '
        'ON bestseller_image_recommendations(recommended_by_user_id)',
        'CREATE INDEX IF NOT EXISTS ix_bestseller_image_recommendations_reviewed_by_user_id '
        'ON bestseller_image_recommendations(reviewed_by_user_id)',
        'CREATE INDEX IF NOT EXISTS idx_bestseller_image_recommendation_seller_status '
        'ON bestseller_image_recommendations(seller_id, status, opportunity_score)',
        'CREATE INDEX IF NOT EXISTS idx_bestseller_image_recommendation_admin_status '
        'ON bestseller_image_recommendations(status, marketplace_code, updated_at)',
    ):
        connection.execute(statement)


def _verify_schema(connection: sqlite3.Connection) -> None:
    expected = {
        'id', 'seller_id', 'imported_product_id', 'marketplace_code',
        'account_id', 'marketplace_listing_id', 'legacy_product_id', 'scope_key',
        'period_code', 'status', 'opportunity_score', 'units', 'revenue_rub',
        'photo_count', 'quality_score', 'source_observed_at',
        'metric_definitions_json', 'snapshot_json', 'recommended_by_user_id',
        'reviewed_by_user_id', 'reviewed_at', 'created_at', 'updated_at',
    }
    missing = expected - _columns(connection, 'bestseller_image_recommendations')
    if missing:
        raise sqlite3.OperationalError(
            'bestseller_image_recommendations is missing columns: '
            + ', '.join(sorted(missing))
        )


def apply_migration(connection: sqlite3.Connection, *, verbose: bool = True) -> int:
    baseline = foreign_key_snapshot(connection)
    before = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    _create_schema(connection)
    _verify_schema(connection)
    assert_foreign_key_safety(
        connection,
        baseline=baseline,
        managed_tables=MANAGED_TABLES,
        label='Bestseller image recommendation migration',
    )
    after = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    if verbose:
        print('bestseller image recommendations: OK')
    return len(after - before)


def migrate(db_path: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        apply_migration(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get('DATABASE_PATH', 'data/seller_platform.db')
    )
    migrate(path)
