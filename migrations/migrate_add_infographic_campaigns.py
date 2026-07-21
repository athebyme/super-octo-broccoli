#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create durable seller-scoped bulk infographic campaigns."""

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


MANAGED_TABLES = {
    'infographic_campaigns',
    'infographic_campaign_items',
    'infographic_campaign_slides',
}


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
    }
    for table_name, columns in required.items():
        actual = _columns(connection, table_name)
        if not actual:
            raise sqlite3.OperationalError(
                f'infographic campaign prerequisite missing: {table_name}'
            )
        missing = columns - actual
        if missing:
            raise sqlite3.OperationalError(
                f'{table_name} is missing columns: {", ".join(sorted(missing))}'
            )


def _create_schema(connection: sqlite3.Connection) -> None:
    _require_prerequisites(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS infographic_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            name VARCHAR(160) NOT NULL,
            template_key VARCHAR(40) NOT NULL DEFAULT 'botanical',
            mode VARCHAR(24) NOT NULL DEFAULT 'catalog',
            status VARCHAR(24) NOT NULL DEFAULT 'queued',
            scope_json TEXT NOT NULL DEFAULT '{}',
            config_json TEXT NOT NULL DEFAULT '{}',
            total_items INTEGER NOT NULL DEFAULT 0,
            runnable_items INTEGER NOT NULL DEFAULT 0,
            completed_items INTEGER NOT NULL DEFAULT 0,
            failed_items INTEGER NOT NULL DEFAULT 0,
            approved_items INTEGER NOT NULL DEFAULT 0,
            total_slides INTEGER NOT NULL DEFAULT 0,
            completed_slides INTEGER NOT NULL DEFAULT 0,
            approved_slides INTEGER NOT NULL DEFAULT 0,
            estimated_cost_rub FLOAT NOT NULL DEFAULT 0.0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME,
            completed_at DATETIME,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_infographic_campaign_mode CHECK (mode IN ('catalog')),
            CONSTRAINT ck_infographic_campaign_status CHECK (
                status IN ('queued','running','review','approved','partial','cancelled')
            ),
            CONSTRAINT ck_infographic_campaign_counters CHECK (
                total_items >= 0 AND runnable_items >= 0
                AND completed_items >= 0 AND failed_items >= 0
                AND approved_items >= 0 AND total_slides >= 0
                AND completed_slides >= 0 AND approved_slides >= 0
                AND estimated_cost_rub >= 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS infographic_campaign_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL
                REFERENCES infographic_campaigns(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            imported_product_id INTEGER
                REFERENCES imported_products(id) ON DELETE SET NULL,
            product_title VARCHAR(500) NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'queued',
            source_fingerprint VARCHAR(64) NOT NULL,
            fact_pack_json TEXT NOT NULL DEFAULT '{}',
            content_json TEXT NOT NULL DEFAULT '{}',
            error_code VARCHAR(80),
            error_message VARCHAR(1000),
            total_slides INTEGER NOT NULL DEFAULT 0,
            completed_slides INTEGER NOT NULL DEFAULT 0,
            approved_slides INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME,
            completed_at DATETIME,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_infographic_campaign_product UNIQUE (
                campaign_id, imported_product_id
            ),
            CONSTRAINT ck_infographic_campaign_item_status CHECK (
                status IN (
                    'queued','running','ready','failed','blocked',
                    'cancelled','conflict'
                )
            ),
            CONSTRAINT ck_infographic_campaign_item_counters CHECK (
                total_slides >= 0 AND completed_slides >= 0
                AND approved_slides >= 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS infographic_campaign_slides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL
                REFERENCES infographic_campaigns(id) ON DELETE CASCADE,
            item_id INTEGER NOT NULL
                REFERENCES infographic_campaign_items(id) ON DELETE CASCADE,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            slide_type VARCHAR(40) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'completed',
            review_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            content_json TEXT NOT NULL DEFAULT '{}',
            quality_json TEXT NOT NULL DEFAULT '{}',
            artifact_path VARCHAR(500),
            artifact_sha256 VARCHAR(64),
            error_message VARCHAR(1000),
            reviewed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reviewed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_infographic_item_slide_position UNIQUE (item_id, position),
            CONSTRAINT ck_infographic_slide_position CHECK (position >= 1),
            CONSTRAINT ck_infographic_slide_status CHECK (
                status IN ('completed','failed')
            ),
            CONSTRAINT ck_infographic_slide_review_status CHECK (
                review_status IN ('pending','approved','rejected')
            )
        )
    ''')
    for statement in (
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaigns_seller_id '
        'ON infographic_campaigns(seller_id)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaigns_created_by_user_id '
        'ON infographic_campaigns(created_by_user_id)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaigns_status '
        'ON infographic_campaigns(status)',
        'CREATE INDEX IF NOT EXISTS idx_infographic_campaign_seller_created '
        'ON infographic_campaigns(seller_id, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_infographic_campaign_seller_status '
        'ON infographic_campaigns(seller_id, status, created_at)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaign_items_campaign_id '
        'ON infographic_campaign_items(campaign_id)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaign_items_seller_id '
        'ON infographic_campaign_items(seller_id)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaign_items_imported_product_id '
        'ON infographic_campaign_items(imported_product_id)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaign_items_status '
        'ON infographic_campaign_items(status)',
        'CREATE INDEX IF NOT EXISTS idx_infographic_item_campaign_status '
        'ON infographic_campaign_items(campaign_id, status, id)',
        'CREATE INDEX IF NOT EXISTS idx_infographic_item_seller_product '
        'ON infographic_campaign_items(seller_id, imported_product_id, created_at)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaign_slides_campaign_id '
        'ON infographic_campaign_slides(campaign_id)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaign_slides_item_id '
        'ON infographic_campaign_slides(item_id)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaign_slides_seller_id '
        'ON infographic_campaign_slides(seller_id)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaign_slides_status '
        'ON infographic_campaign_slides(status)',
        'CREATE INDEX IF NOT EXISTS ix_infographic_campaign_slides_review_status '
        'ON infographic_campaign_slides(review_status)',
        'CREATE INDEX IF NOT EXISTS idx_infographic_slide_campaign_review '
        'ON infographic_campaign_slides(campaign_id, review_status, position)',
        'CREATE INDEX IF NOT EXISTS idx_infographic_slide_item_position '
        'ON infographic_campaign_slides(item_id, position)',
    ):
        connection.execute(statement)


def _verify_schema(connection: sqlite3.Connection) -> None:
    expected = {
        'infographic_campaigns': {
            'id', 'seller_id', 'created_by_user_id', 'name', 'template_key',
            'mode', 'status', 'scope_json', 'config_json', 'total_items',
            'runnable_items', 'completed_items', 'failed_items',
            'approved_items', 'total_slides', 'completed_slides',
            'approved_slides', 'estimated_cost_rub', 'created_at',
            'started_at', 'completed_at', 'updated_at',
        },
        'infographic_campaign_items': {
            'id', 'campaign_id', 'seller_id', 'imported_product_id',
            'product_title', 'status', 'source_fingerprint', 'fact_pack_json',
            'content_json', 'error_code', 'error_message', 'total_slides',
            'completed_slides', 'approved_slides', 'created_at', 'started_at',
            'completed_at', 'updated_at',
        },
        'infographic_campaign_slides': {
            'id', 'campaign_id', 'item_id', 'seller_id', 'position',
            'slide_type', 'status', 'review_status', 'content_json',
            'quality_json', 'artifact_path', 'artifact_sha256',
            'error_message', 'reviewed_by_user_id', 'reviewed_at',
            'created_at', 'updated_at',
        },
    }
    for table_name, columns in expected.items():
        missing = columns - _columns(connection, table_name)
        if missing:
            raise sqlite3.OperationalError(
                f'{table_name} is missing columns: {", ".join(sorted(missing))}'
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
        label='Infographic campaign migration',
    )
    after = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    if verbose:
        print('infographic campaigns: OK')
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
