"""P10A marketplace inbox schema invariants."""

import sqlite3

from flask import Flask
import pytest

from migrations.migrate_add_marketplace_inbox import apply_migration
from models import db


def _prerequisites(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
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


def test_marketplace_inbox_migration_is_idempotent_and_constrained():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _prerequisites(connection)
        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)
        connection.executescript("""
            INSERT INTO users(id) VALUES (1);
            INSERT INTO sellers(id) VALUES (1), (2);
            INSERT INTO marketplaces(id, code) VALUES (10, 'ozon');
            INSERT INTO seller_marketplace_accounts(id, seller_id, marketplace_id)
                VALUES (100, 1, 10), (200, 2, 10);
            INSERT INTO marketplace_listings(id, seller_id, marketplace_id, account_id)
                VALUES (1000, 1, 10, 100), (2000, 2, 10, 200);
            INSERT INTO marketplace_inbox_syncs (
                id, seller_id, marketplace_id, account_id, source_kind,
                period_start, period_end, status, current_status,
                request_fingerprint
            ) VALUES (
                500, 1, 10, 100, 'review', '2026-04-17', '2026-07-15',
                'running', 'NEW',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            );
            INSERT INTO marketplace_inbox_items (
                id, seller_id, marketplace_id, account_id, listing_id,
                last_sync_id, source_kind, external_id, external_sku,
                match_status, text, rating, provider_status, published_at,
                reply_eligible, source_endpoint, source_fingerprint, last_seen_at
            ) VALUES (
                700, 1, 10, 100, 1000, 500, 'review', 'review-1', '101',
                'matched', 'Synthetic', 5, 'NEW', '2026-07-15 10:00:00',
                1, '/v2/review/list',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                '2026-07-15 12:00:00'
            );
            INSERT INTO marketplace_reply_drafts (
                id, seller_id, marketplace_id, account_id, inbox_item_id,
                listing_id, created_by_user_id, status, generation_mode, text,
                source_fingerprint, facts_fingerprint, content_hash
            ) VALUES (
                900, 1, 10, 100, 700, 1000, 1, 'draft', 'template',
                'Safe local draft',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
            );
        """)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_inbox_syncs (
                    seller_id, marketplace_id, account_id, source_kind,
                    period_start, period_end, status, current_status,
                    request_fingerprint
                ) VALUES (
                    1, 10, 100, 'review', '2026-04-17', '2026-07-15',
                    'running', 'VIEWED',
                    'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
                )
            """)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_inbox_items (
                    seller_id, marketplace_id, account_id, source_kind,
                    external_id, external_sku, match_status, rating,
                    provider_status, published_at, reply_eligible,
                    source_endpoint, source_fingerprint, last_seen_at
                ) VALUES (
                    1, 10, 100, 'question', 'bad-question', '101',
                    'unmatched', 5, 'NEW', '2026-07-15 10:00:00', 1,
                    '/v1/question/list',
                    'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
                    '2026-07-15 12:00:00'
                )
            """)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO marketplace_reply_drafts (
                    seller_id, marketplace_id, account_id, inbox_item_id,
                    status, generation_mode, text, source_fingerprint,
                    facts_fingerprint, content_hash
                ) VALUES (
                    1, 10, 100, 700, 'draft', 'ai', 'Second active draft',
                    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                    'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                    'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
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
        "marketplace_inbox_syncs",
        "marketplace_inbox_items",
        "marketplace_reply_drafts",
    }.issubset(tables)
    assert violations == []


def test_marketplace_inbox_migration_rejects_shadowed_safety_index():
    connection = sqlite3.connect(":memory:")
    try:
        _prerequisites(connection)
        apply_migration(connection, verbose=False)
        connection.execute("DROP INDEX uq_marketplace_inbox_running_kind")
        connection.execute(
            "CREATE INDEX uq_marketplace_inbox_running_kind "
            "ON marketplace_inbox_syncs(account_id, source_kind)"
        )

        with pytest.raises(
            sqlite3.OperationalError,
            match="required partial unique index",
        ):
            apply_migration(connection, verbose=False)
    finally:
        connection.close()


def test_marketplace_inbox_migration_accepts_orm_created_schema():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        connection = db.engine.raw_connection()
        try:
            assert apply_migration(connection, verbose=False) == 0
        finally:
            connection.close()
            db.drop_all()
