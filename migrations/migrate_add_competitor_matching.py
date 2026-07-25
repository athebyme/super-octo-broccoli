#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add shared competitor-to-supplier matching and seller review state.

The migration is additive and local-only.  It adds bounded observed WB facts,
one global computation row per nmID, tenant-owned review overrides and an
append-only audit journal.  It never calls WB, image hosts or an LLM.
"""

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
    'competitor_product_matches',
    'seller_competitor_match_reviews',
    'competitor_match_events',
}


def _columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(
            f'PRAGMA table_info({table_name})'
        ).fetchall()
    }


def _unique_index_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for row in connection.execute(
        f'PRAGMA index_list({table_name})'
    ).fetchall():
        if not row[2]:
            continue
        index_name = str(row[1]).replace('"', '""')
        columns = tuple(
            item[2]
            for item in connection.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()
        )
        result.add(columns)
    return result


def _foreign_keys(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[tuple[str, str, str, str]]:
    return {
        (str(row[3]), str(row[2]), str(row[4]), str(row[6]).upper())
        for row in connection.execute(
            f'PRAGMA foreign_key_list({table_name})'
        ).fetchall()
    }


def _require_prerequisites(connection: sqlite3.Connection) -> None:
    required = {
        'users': {'id'},
        'sellers': {'id'},
        'supplier_products': {'id'},
        'competitor_products': {'id', 'nm_id'},
        'background_jobs': {
            'job_uid', 'seller_id', 'job_type', 'status', 'progress_data',
        },
    }
    for table_name, expected in required.items():
        actual = _columns(connection, table_name)
        if not actual:
            raise sqlite3.OperationalError(
                f'competitor matching prerequisite missing: {table_name}'
            )
        missing = expected - actual
        if missing:
            raise sqlite3.OperationalError(
                f'{table_name} is missing columns: '
                + ', '.join(sorted(missing))
            )


def _add_competitor_fact_columns(connection: sqlite3.Connection) -> int:
    columns = _columns(connection, 'competitor_products')
    added = 0
    for name, declaration in (
        ('subject_id', 'INTEGER'),
        ('subject_name', 'VARCHAR(200)'),
        ('photo_count', 'INTEGER'),
        ('characteristics_json', 'TEXT'),
    ):
        if name in columns:
            continue
        connection.execute(
            f'ALTER TABLE competitor_products ADD COLUMN {name} {declaration}'
        )
        columns.add(name)
        added += 1
    return added


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE IF NOT EXISTS competitor_product_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nm_id BIGINT NOT NULL,
            suggested_supplier_product_id INTEGER
                REFERENCES supplier_products(id) ON DELETE SET NULL,
            processing_status VARCHAR(20) NOT NULL DEFAULT 'queued',
            predicted_match_type VARCHAR(20),
            marketplace_facts_json TEXT NOT NULL DEFAULT '{}',
            source_fingerprint VARCHAR(64),
            text_score INTEGER NOT NULL DEFAULT 0,
            image_score INTEGER,
            deterministic_score INTEGER NOT NULL DEFAULT 0,
            final_score INTEGER NOT NULL DEFAULT 0,
            candidates_json TEXT NOT NULL DEFAULT '[]',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            algorithm_version VARCHAR(40) NOT NULL,
            evaluation_fingerprint VARCHAR(64),
            evaluated_at DATETIME,
            claim_token VARCHAR(64),
            claim_expires_at DATETIME,
            llm_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            llm_verdict VARCHAR(20),
            llm_reason VARCHAR(500),
            llm_model VARCHAR(160),
            llm_usage_json TEXT NOT NULL DEFAULT '{}',
            llm_error_code VARCHAR(64),
            llm_evaluated_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_competitor_product_match_nm UNIQUE (nm_id),
            CONSTRAINT ck_competitor_match_processing_status CHECK (
                processing_status IN ('queued','processing','completed','failed')
            ),
            CONSTRAINT ck_competitor_match_llm_status CHECK (
                llm_status IN (
                    'pending','completed','cached','unavailable','failed','skipped'
                )
            ),
            CONSTRAINT ck_competitor_match_predicted_type CHECK (
                predicted_match_type IS NULL OR predicted_match_type IN (
                    'same','analog','different','uncertain'
                )
            ),
            CONSTRAINT ck_competitor_match_scores CHECK (
                text_score >= 0 AND text_score <= 100
                AND (image_score IS NULL OR (
                    image_score >= 0 AND image_score <= 100
                ))
                AND deterministic_score >= 0 AND deterministic_score <= 100
                AND final_score >= 0 AND final_score <= 100
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS seller_competitor_match_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL
                REFERENCES sellers(id) ON DELETE CASCADE,
            match_id INTEGER NOT NULL
                REFERENCES competitor_product_matches(id) ON DELETE CASCADE,
            supplier_product_id INTEGER
                REFERENCES supplier_products(id) ON DELETE SET NULL,
            status VARCHAR(20) NOT NULL,
            match_type VARCHAR(20),
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reviewed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_seller_competitor_match_review
                UNIQUE (seller_id, match_id),
            CONSTRAINT ck_seller_competitor_match_review_status CHECK (
                status IN ('confirmed','rejected')
            ),
            CONSTRAINT ck_seller_competitor_match_review_type CHECK (
                match_type IS NULL OR match_type IN ('same','analog')
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS competitor_match_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL
                REFERENCES sellers(id) ON DELETE CASCADE,
            match_id INTEGER NOT NULL
                REFERENCES competitor_product_matches(id) ON DELETE CASCADE,
            previous_supplier_product_id INTEGER
                REFERENCES supplier_products(id) ON DELETE SET NULL,
            supplier_product_id INTEGER
                REFERENCES supplier_products(id) ON DELETE SET NULL,
            action VARCHAR(20) NOT NULL,
            match_type VARCHAR(20),
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_competitor_match_event_action CHECK (
                action IN ('confirm','reject','reset')
            ),
            CONSTRAINT ck_competitor_match_event_type CHECK (
                match_type IS NULL OR match_type IN ('same','analog')
            )
        )
    ''')


def _create_indexes(connection: sqlite3.Connection) -> None:
    for statement in (
        'CREATE UNIQUE INDEX IF NOT EXISTS '
        'ix_competitor_product_matches_nm_id '
        'ON competitor_product_matches(nm_id)',
        'CREATE INDEX IF NOT EXISTS '
        'ix_competitor_product_matches_suggested_supplier_product_id '
        'ON competitor_product_matches(suggested_supplier_product_id)',
        'CREATE INDEX IF NOT EXISTS '
        'ix_competitor_product_matches_source_fingerprint '
        'ON competitor_product_matches(source_fingerprint)',
        'CREATE INDEX IF NOT EXISTS '
        'ix_competitor_product_matches_evaluation_fingerprint '
        'ON competitor_product_matches(evaluation_fingerprint)',
        'CREATE INDEX IF NOT EXISTS '
        'ix_competitor_product_matches_claim_token '
        'ON competitor_product_matches(claim_token)',
        'CREATE INDEX IF NOT EXISTS '
        'ix_competitor_product_matches_claim_expires_at '
        'ON competitor_product_matches(claim_expires_at)',
        'CREATE INDEX IF NOT EXISTS idx_competitor_match_state '
        'ON competitor_product_matches(processing_status,updated_at)',
        'CREATE INDEX IF NOT EXISTS idx_competitor_match_suggested '
        'ON competitor_product_matches(suggested_supplier_product_id)',
        'CREATE INDEX IF NOT EXISTS '
        'ix_seller_competitor_match_reviews_seller_id '
        'ON seller_competitor_match_reviews(seller_id)',
        'CREATE INDEX IF NOT EXISTS '
        'ix_seller_competitor_match_reviews_match_id '
        'ON seller_competitor_match_reviews(match_id)',
        'CREATE INDEX IF NOT EXISTS '
        'ix_seller_competitor_match_reviews_supplier_product_id '
        'ON seller_competitor_match_reviews(supplier_product_id)',
        'CREATE INDEX IF NOT EXISTS '
        'ix_seller_competitor_match_reviews_actor_user_id '
        'ON seller_competitor_match_reviews(actor_user_id)',
        'CREATE INDEX IF NOT EXISTS idx_seller_competitor_review_scope '
        'ON seller_competitor_match_reviews(seller_id,match_id)',
        'CREATE INDEX IF NOT EXISTS ix_competitor_match_events_seller_id '
        'ON competitor_match_events(seller_id)',
        'CREATE INDEX IF NOT EXISTS ix_competitor_match_events_match_id '
        'ON competitor_match_events(match_id)',
        'CREATE INDEX IF NOT EXISTS ix_competitor_match_events_actor_user_id '
        'ON competitor_match_events(actor_user_id)',
        'CREATE INDEX IF NOT EXISTS idx_competitor_match_event_scope '
        'ON competitor_match_events(seller_id,match_id,created_at)',
    ):
        connection.execute(statement)


def _verify(connection: sqlite3.Connection) -> None:
    expected_match = {
        'id', 'nm_id', 'suggested_supplier_product_id', 'processing_status',
        'predicted_match_type', 'marketplace_facts_json',
        'source_fingerprint', 'text_score', 'image_score',
        'deterministic_score', 'final_score', 'candidates_json',
        'evidence_json', 'algorithm_version', 'evaluation_fingerprint',
        'evaluated_at', 'claim_token', 'claim_expires_at', 'llm_status',
        'llm_verdict', 'llm_reason', 'llm_model', 'llm_usage_json',
        'llm_error_code', 'llm_evaluated_at', 'created_at', 'updated_at',
    }
    missing = expected_match - _columns(
        connection, 'competitor_product_matches')
    if missing:
        raise sqlite3.OperationalError(
            'competitor_product_matches has an incompatible schema; missing: '
            + ', '.join(sorted(missing))
        )
    expected_review = {
        'id', 'seller_id', 'match_id', 'supplier_product_id', 'status',
        'match_type', 'actor_user_id', 'reviewed_at', 'created_at',
        'updated_at',
    }
    missing_review = expected_review - _columns(
        connection, 'seller_competitor_match_reviews')
    if missing_review:
        raise sqlite3.OperationalError(
            'seller_competitor_match_reviews is missing columns: '
            + ', '.join(sorted(missing_review))
        )
    expected_event = {
        'id', 'seller_id', 'match_id', 'previous_supplier_product_id',
        'supplier_product_id', 'action', 'match_type', 'actor_user_id',
        'evidence_json', 'created_at',
    }
    missing_event = expected_event - _columns(
        connection, 'competitor_match_events')
    if missing_event:
        raise sqlite3.OperationalError(
            'competitor_match_events is missing columns: '
            + ', '.join(sorted(missing_event))
        )
    expected_product = {
        'subject_id', 'subject_name', 'photo_count', 'characteristics_json',
    }
    missing_product = expected_product - _columns(
        connection, 'competitor_products')
    if missing_product:
        raise sqlite3.OperationalError(
            'competitor_products is missing matching facts: '
            + ', '.join(sorted(missing_product))
        )

    unique_requirements = {
        'competitor_product_matches': ('nm_id',),
        'seller_competitor_match_reviews': ('seller_id', 'match_id'),
    }
    for table_name, columns in unique_requirements.items():
        if columns not in _unique_index_columns(connection, table_name):
            raise sqlite3.OperationalError(
                f'{table_name} is missing required unique key '
                + ','.join(columns)
            )

    foreign_key_requirements = {
        'competitor_product_matches': {
            ('suggested_supplier_product_id', 'supplier_products', 'id',
             'SET NULL'),
        },
        'seller_competitor_match_reviews': {
            ('seller_id', 'sellers', 'id', 'CASCADE'),
            ('match_id', 'competitor_product_matches', 'id', 'CASCADE'),
            ('supplier_product_id', 'supplier_products', 'id', 'SET NULL'),
            ('actor_user_id', 'users', 'id', 'SET NULL'),
        },
        'competitor_match_events': {
            ('seller_id', 'sellers', 'id', 'CASCADE'),
            ('match_id', 'competitor_product_matches', 'id', 'CASCADE'),
            ('previous_supplier_product_id', 'supplier_products', 'id',
             'SET NULL'),
            ('supplier_product_id', 'supplier_products', 'id', 'SET NULL'),
            ('actor_user_id', 'users', 'id', 'SET NULL'),
        },
    }
    for table_name, expected in foreign_key_requirements.items():
        missing_foreign_keys = expected - _foreign_keys(connection, table_name)
        if missing_foreign_keys:
            raise sqlite3.OperationalError(
                f'{table_name} has incompatible foreign keys: '
                + repr(sorted(missing_foreign_keys))
            )


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    baseline = foreign_key_snapshot(connection)
    before = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    _require_prerequisites(connection)
    added_columns = _add_competitor_fact_columns(connection)
    _create_tables(connection)
    _create_indexes(connection)
    _verify(connection)
    assert_foreign_key_safety(
        connection,
        baseline=baseline,
        managed_tables=MANAGED_TABLES,
        label='Competitor matching migration',
    )
    after = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    if verbose:
        print('competitor matching: OK')
    return len(after - before) + added_columns


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
