"""Exact category-scoped marketplace brand identity migration tests."""

import sqlite3

from flask import Flask
import pytest

from migrations.migrate_add_brand_category_external_id import apply_migration
from models import db


def test_brand_category_external_id_migration_is_idempotent_without_backfill():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript("""
            CREATE TABLE brand_category_links (
                id INTEGER PRIMARY KEY,
                marketplace_brand_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                category_name VARCHAR(200),
                is_available BOOLEAN NOT NULL DEFAULT 1,
                verified_at DATETIME,
                UNIQUE (marketplace_brand_id, category_id)
            );
            INSERT INTO brand_category_links (
                id, marketplace_brand_id, category_id, is_available
            ) VALUES (1, 10, 100, 1);
        """)

        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)
        row = connection.execute(
            "SELECT marketplace_external_brand_id "
            "FROM brand_category_links WHERE id = 1"
        ).fetchone()
        index_columns = [
            item[2] for item in connection.execute(
                "PRAGMA index_info(idx_bcl_category_external_brand_id)"
            ).fetchall()
        ]
    finally:
        connection.close()

    assert first == 1
    assert second == 0
    assert row == (None,)
    assert index_columns == ["category_id", "marketplace_external_brand_id"]


def test_brand_category_external_id_migration_skips_missing_table():
    connection = sqlite3.connect(":memory:")
    try:
        assert apply_migration(connection, verbose=False) == 0
    finally:
        connection.close()


def test_brand_category_external_id_migration_rejects_managed_fk_orphan():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript("""
            CREATE TABLE marketplace_brands (id INTEGER PRIMARY KEY);
            CREATE TABLE brand_category_links (
                id INTEGER PRIMARY KEY,
                marketplace_brand_id INTEGER NOT NULL
                    REFERENCES marketplace_brands(id),
                category_id INTEGER NOT NULL
            );
            INSERT INTO brand_category_links (
                id, marketplace_brand_id, category_id
            ) VALUES (1, 999, 100);
        """)
        with pytest.raises(sqlite3.IntegrityError, match="foreign-key safety"):
            apply_migration(connection, verbose=False)
    finally:
        connection.close()


def test_brand_category_external_id_migration_accepts_orm_schema():
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
