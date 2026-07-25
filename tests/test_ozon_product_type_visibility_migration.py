"""Ozon seller visibility migration is additive and repeatable."""

import sqlite3

from migrations.migrate_add_ozon_product_type_visibility import (
    apply_migration,
)


def test_visibility_migration_exposes_existing_types_without_enabling_preload():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.executescript(
            """
            CREATE TABLE marketplaces (
                id INTEGER PRIMARY KEY,
                code VARCHAR(50) NOT NULL UNIQUE
            );
            CREATE TABLE marketplace_taxonomy_categories (
                id INTEGER PRIMARY KEY,
                marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
                external_category_id VARCHAR(100) NOT NULL,
                name VARCHAR(500) NOT NULL,
                full_path VARCHAR(2000) NOT NULL
            );
            CREATE TABLE marketplace_product_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
                category_id INTEGER NOT NULL
                    REFERENCES marketplace_taxonomy_categories(id),
                external_type_id VARCHAR(100) NOT NULL,
                name VARCHAR(500) NOT NULL,
                is_disabled_upstream BOOLEAN NOT NULL DEFAULT 0,
                is_available BOOLEAN NOT NULL DEFAULT 1,
                is_enabled BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME
            );
            INSERT INTO marketplaces(id, code) VALUES (1, 'ozon');
            INSERT INTO marketplace_taxonomy_categories (
                id, marketplace_id, external_category_id, name, full_path
            ) VALUES (1, 1, '10', 'Category', 'Category');
            INSERT INTO marketplace_product_types (
                marketplace_id, category_id, external_type_id, name,
                is_available, is_enabled
            ) VALUES (1, 1, '777', 'Type', 1, 0);
            """
        )

        assert apply_migration(connection, verbose=False) == 1
        assert apply_migration(connection, verbose=False) == 0
        row = connection.execute(
            "SELECT is_enabled, is_seller_selectable "
            "FROM marketplace_product_types WHERE external_type_id='777'"
        ).fetchone()
        index_names = {
            item[1]
            for item in connection.execute(
                "PRAGMA index_list(marketplace_product_types)"
            ).fetchall()
        }
        violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
    finally:
        connection.close()

    assert row == (0, 1)
    assert "idx_marketplace_product_type_selectable" in index_names
    assert violations == []
