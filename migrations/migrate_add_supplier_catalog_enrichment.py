#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable admin enrichment runs and supplier-content revisions."""

import argparse
import os
import sqlite3

if __package__:
    from ._foreign_key_safety import (
        assert_foreign_key_safety,
        foreign_key_snapshot,
    )
else:
    from _foreign_key_safety import (  # type: ignore[no-redef]
        assert_foreign_key_safety,
        foreign_key_snapshot,
    )


MANAGED_TABLES = {
    'supplier_catalog_enrichment_runs',
    'supplier_catalog_enrichment_items',
}


def _default_db_path():
    database_url = os.environ.get('DATABASE_URL', '')
    if database_url.startswith('sqlite:///'):
        return database_url.replace('sqlite:///', '', 1)
    return os.environ.get('DATABASE_PATH', '/app/data/seller_platform.db')


def _columns(cursor, table):
    cursor.execute(f'PRAGMA table_info({table})')
    return {row[1] for row in cursor.fetchall()}


def _table_exists(cursor, table):
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    )
    return cursor.fetchone() is not None


def migrate(db_path):
    """Apply the migration idempotently to one SQLite database."""
    if not db_path or not os.path.exists(db_path):
        raise FileNotFoundError(f'Database not found: {db_path}')

    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA foreign_keys=ON')
    cursor = conn.cursor()
    try:
        baseline = foreign_key_snapshot(conn)
        if not _table_exists(cursor, 'supplier_products'):
            raise RuntimeError('supplier_products table is missing')
        if not _table_exists(cursor, 'imported_products'):
            raise RuntimeError('imported_products table is missing')
        if not _table_exists(cursor, 'suppliers'):
            raise RuntimeError('suppliers table is missing')
        if not _table_exists(cursor, 'users'):
            raise RuntimeError('users table is missing')

        if 'content_revision' not in _columns(cursor, 'supplier_products'):
            cursor.execute(
                'ALTER TABLE supplier_products ADD COLUMN '
                'content_revision INTEGER NOT NULL DEFAULT 1'
            )
        cursor.execute(
            'UPDATE supplier_products SET content_revision=1 '
            'WHERE content_revision IS NULL OR content_revision < 1'
        )

        imported_columns = _columns(cursor, 'imported_products')
        added_imported_revision = 'supplier_content_revision' not in imported_columns
        if added_imported_revision:
            cursor.execute(
                'ALTER TABLE imported_products ADD COLUMN '
                'supplier_content_revision INTEGER NOT NULL DEFAULT 0'
            )
            # Existing copies predate revision tracking and are treated as
            # synchronized at migration time; only later enrichment is shown.
            cursor.execute(
                '''
                UPDATE imported_products
                   SET supplier_content_revision = COALESCE((
                       SELECT content_revision
                         FROM supplier_products
                        WHERE supplier_products.id =
                              imported_products.supplier_product_id
                   ), 0)
                 WHERE supplier_product_id IS NOT NULL
                '''
            )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS supplier_catalog_enrichment_runs (
                id VARCHAR(36) PRIMARY KEY,
                supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                admin_user_id INTEGER NOT NULL REFERENCES users(id),
                mode VARCHAR(40) NOT NULL DEFAULT 'category_only'
                    CHECK (mode IN (
                        'category_only', 'category_and_characteristics'
                    )),
                status VARCHAR(24) NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending', 'running', 'cancelling', 'cancelled',
                        'completed', 'partial', 'failed'
                    )),
                selection_json TEXT NOT NULL DEFAULT '{}',
                reference_snapshot_json TEXT,
                model_used VARCHAR(100),
                total INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                applied INTEGER NOT NULL DEFAULT 0,
                unchanged INTEGER NOT NULL DEFAULT 0,
                needs_review INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                cancelled INTEGER NOT NULL DEFAULT 0,
                llm_calls INTEGER NOT NULL DEFAULT 0,
                llm_call_limit INTEGER NOT NULL DEFAULT 1600,
                current_label VARCHAR(300),
                error_code VARCHAR(80),
                error_message TEXT,
                heartbeat_at DATETIME,
                started_at DATETIME,
                completed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS supplier_catalog_enrichment_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id VARCHAR(36) NOT NULL REFERENCES
                    supplier_catalog_enrichment_runs(id) ON DELETE CASCADE,
                supplier_product_id INTEGER NOT NULL REFERENCES
                    supplier_products(id),
                ordinal INTEGER NOT NULL,
                phase VARCHAR(24) NOT NULL DEFAULT 'category'
                    CHECK (phase IN ('category', 'characteristics', 'done')),
                status VARCHAR(24) NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending', 'running', 'applied', 'unchanged',
                        'needs_review', 'failed', 'cancelled', 'rolled_back',
                        'rollback_conflict'
                    )),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                source_fingerprint VARCHAR(64) NOT NULL,
                proposed_subject_id INTEGER,
                proposed_subject_name VARCHAR(300),
                confidence FLOAT,
                reasoning TEXT,
                evidence TEXT,
                before_json TEXT,
                after_json TEXT,
                reference_json TEXT,
                error_code VARCHAR(80),
                error_message TEXT,
                category_changed BOOLEAN NOT NULL DEFAULT 0,
                characteristics_changed BOOLEAN NOT NULL DEFAULT 0,
                applied_revision INTEGER,
                started_at DATETIME,
                completed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_supplier_catalog_enrichment_run_product
                    UNIQUE (run_id, supplier_product_id)
            )
            '''
        )
        if 'attempt_count' not in _columns(
            cursor, 'supplier_catalog_enrichment_items'
        ):
            cursor.execute(
                'ALTER TABLE supplier_catalog_enrichment_items ADD COLUMN '
                'attempt_count INTEGER NOT NULL DEFAULT 0'
            )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS '
            'idx_supplier_catalog_enrichment_run_supplier '
            'ON supplier_catalog_enrichment_runs(supplier_id)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS '
            'idx_supplier_catalog_enrichment_run_status '
            'ON supplier_catalog_enrichment_runs(status)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS '
            'idx_supplier_catalog_enrichment_run_active '
            'ON supplier_catalog_enrichment_runs(supplier_id, status, created_at)'
        )
        cursor.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS '
            'uq_supplier_catalog_enrichment_active_supplier '
            'ON supplier_catalog_enrichment_runs(supplier_id) '
            "WHERE status IN ('pending','running','cancelling')"
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS '
            'idx_supplier_catalog_enrichment_item_run '
            'ON supplier_catalog_enrichment_items(run_id)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS '
            'idx_supplier_catalog_enrichment_item_product '
            'ON supplier_catalog_enrichment_items(supplier_product_id)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS '
            'idx_supplier_catalog_enrichment_item_queue '
            'ON supplier_catalog_enrichment_items('
            'run_id, status, phase, ordinal)'
        )
        expected_columns = {
            'supplier_catalog_enrichment_runs': {
                'id', 'supplier_id', 'admin_user_id', 'mode', 'status',
                'selection_json', 'reference_snapshot_json', 'model_used',
                'total', 'processed', 'applied', 'unchanged', 'needs_review',
                'failed', 'cancelled', 'llm_calls', 'llm_call_limit',
                'current_label', 'error_code', 'error_message', 'heartbeat_at',
                'started_at', 'completed_at', 'created_at', 'updated_at',
            },
            'supplier_catalog_enrichment_items': {
                'id', 'run_id', 'supplier_product_id', 'ordinal', 'phase',
                'status', 'attempt_count', 'source_fingerprint',
                'proposed_subject_id', 'proposed_subject_name', 'confidence',
                'reasoning', 'evidence', 'before_json', 'after_json',
                'reference_json', 'error_code', 'error_message',
                'category_changed', 'characteristics_changed',
                'applied_revision', 'started_at', 'completed_at',
                'created_at', 'updated_at',
            },
        }
        for table, expected in expected_columns.items():
            missing = expected - _columns(cursor, table)
            if missing:
                raise RuntimeError(
                    f'{table} is missing columns: {", ".join(sorted(missing))}'
                )
        assert_foreign_key_safety(
            conn,
            baseline=baseline,
            managed_tables=MANAGED_TABLES,
            label='Supplier catalog enrichment migration',
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('db_path', nargs='?', default=_default_db_path())
    args = parser.parse_args()
    migrate(args.db_path)
    print('Supplier catalog enrichment migration complete')


if __name__ == '__main__':
    main()
