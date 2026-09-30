"""The WB bulk review key is optional for legacy rows and unique when present."""

import sqlite3

import pytest

from migrations.migrate_add_wb_bulk_review_key import apply_migration


def _legacy_connection():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE bulk_edit_history (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL,
            operation_type VARCHAR(50) NOT NULL
        );
        INSERT INTO bulk_edit_history (id, seller_id, operation_type)
        VALUES (9, 22, 'update_characteristic');
    """)
    return connection


def test_review_key_migration_is_additive_idempotent_and_unique():
    connection = _legacy_connection()
    try:
        assert apply_migration(connection, verbose=False) == 1
        assert apply_migration(connection, verbose=False) == 0
        assert connection.execute(
            "SELECT id, seller_id, operation_type, review_key FROM bulk_edit_history"
        ).fetchall() == [(9, 22, "update_characteristic", None)]

        connection.execute(
            "INSERT INTO bulk_edit_history (id, seller_id, operation_type, review_key) "
            "VALUES (10, 22, 'update_characteristic', NULL)"
        )
        review_key = "a" * 64
        connection.execute(
            "INSERT INTO bulk_edit_history (id, seller_id, operation_type, review_key) "
            "VALUES (?, 22, 'update_characteristic', ?)", (11, review_key)
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO bulk_edit_history (id, seller_id, operation_type, review_key) "
                "VALUES (?, 22, 'update_characteristic', ?)", (12, review_key)
            )
        index = connection.execute(
            "PRAGMA index_info(uq_bulk_edit_history_review_key)"
        ).fetchall()
        assert [row[2] for row in index] == ["review_key"]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "column_sql",
    [
        "review_key TEXT",
        "review_key VARCHAR(64) NOT NULL DEFAULT ''",
    ],
)
def test_review_key_migration_rejects_incompatible_existing_column(column_sql):
    connection = _legacy_connection()
    try:
        connection.execute(f"ALTER TABLE bulk_edit_history ADD COLUMN {column_sql}")
        with pytest.raises(sqlite3.OperationalError, match="Incompatible"):
            apply_migration(connection, verbose=False)
        assert connection.execute(
            "SELECT id FROM bulk_edit_history"
        ).fetchall() == [(9,)]
    finally:
        connection.close()


def test_review_key_migration_rejects_wrong_index_with_expected_name():
    connection = _legacy_connection()
    try:
        connection.execute(
            "ALTER TABLE bulk_edit_history ADD COLUMN review_key VARCHAR(64)"
        )
        connection.execute(
            "CREATE INDEX uq_bulk_edit_history_review_key "
            "ON bulk_edit_history(review_key)"
        )
        with pytest.raises(sqlite3.OperationalError, match="Incompatible unique index"):
            apply_migration(connection, verbose=False)
    finally:
        connection.close()


def test_review_key_migration_requires_history_table():
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError, match="Required table"):
            apply_migration(connection, verbose=False)
    finally:
        connection.close()


def test_review_key_migration_accepts_current_orm_schema():
    from flask import Flask

    from models import db

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
