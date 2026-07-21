"""Durable social account publish-health migration tests."""

import sqlite3

from flask import Flask

from migrations.migrate_add_social_account_publish_health import apply_migration
from models import db


def test_social_account_publish_health_migration_is_idempotent():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript("""
            CREATE TABLE social_accounts (
                id INTEGER PRIMARY KEY,
                seller_id INTEGER NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                last_error TEXT
            );
            INSERT INTO social_accounts (
                id, seller_id, is_active, last_error
            ) VALUES (1, 10, 1, 'legacy error');
        """)

        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)
        row = connection.execute(
            "SELECT last_error, last_error_code, last_error_at, "
            "automatic_publish_blocked_at FROM social_accounts WHERE id = 1"
        ).fetchone()
        index_columns = [
            item[2] for item in connection.execute(
                "PRAGMA index_info(idx_social_account_auto_publish_health)"
            ).fetchall()
        ]
    finally:
        connection.close()

    assert first == 3
    assert second == 0
    assert row == ("legacy error", None, None, None)
    assert index_columns == ["is_active", "automatic_publish_blocked_at"]


def test_social_account_publish_health_migration_skips_missing_table():
    connection = sqlite3.connect(":memory:")
    try:
        assert apply_migration(connection, verbose=False) == 0
    finally:
        connection.close()


def test_social_account_publish_health_migration_accepts_orm_schema():
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
