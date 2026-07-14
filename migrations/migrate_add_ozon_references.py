#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create marketplace-neutral taxonomy/type/attribute reference tables."""

import os
import sqlite3
import sys

try:
    from .migrate_add_marketplace_accounts import apply_migration as apply_accounts
except ImportError:
    from migrate_add_marketplace_accounts import apply_migration as apply_accounts


def _columns(connection: sqlite3.Connection, table_name: str) -> set:
    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    apply_accounts(connection, verbose=False)
    before = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
    }
    marketplace_columns = _columns(connection, "marketplaces")
    if "categories_snapshot_hash" not in marketplace_columns:
        connection.execute(
            "ALTER TABLE marketplaces "
            "ADD COLUMN categories_snapshot_hash VARCHAR(64)"
        )
    if "total_product_types" not in marketplace_columns:
        connection.execute(
            "ALTER TABLE marketplaces "
            "ADD COLUMN total_product_types INTEGER DEFAULT 0"
        )

    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_reference_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marketplace_id INTEGER NOT NULL UNIQUE REFERENCES marketplaces(id),
            external_account_id VARCHAR(200) NOT NULL,
            credentials_encrypted TEXT,
            credential_version INTEGER NOT NULL DEFAULT 1,
            credentials_updated_at DATETIME,
            connection_status VARCHAR(30) NOT NULL DEFAULT 'unchecked',
            connection_checked_at DATETIME,
            credential_expires_at DATETIME,
            roles_json TEXT NOT NULL DEFAULT '[]',
            provider_request_id VARCHAR(200),
            last_error_code VARCHAR(100),
            last_error_message VARCHAR(1000),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_reference_connection_status CHECK (
                connection_status IN ('unchecked', 'connected', 'invalid', 'error')
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_taxonomy_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            external_category_id VARCHAR(100) NOT NULL,
            parent_id INTEGER REFERENCES marketplace_taxonomy_categories(id),
            name VARCHAR(500) NOT NULL,
            full_path VARCHAR(2000) NOT NULL,
            depth INTEGER NOT NULL DEFAULT 0,
            is_disabled_upstream BOOLEAN NOT NULL DEFAULT 0,
            is_available BOOLEAN NOT NULL DEFAULT 1,
            last_seen_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_taxonomy_category UNIQUE (
                marketplace_id, external_category_id
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_product_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            category_id INTEGER NOT NULL REFERENCES marketplace_taxonomy_categories(id),
            external_type_id VARCHAR(100) NOT NULL,
            name VARCHAR(500) NOT NULL,
            is_disabled_upstream BOOLEAN NOT NULL DEFAULT 0,
            is_available BOOLEAN NOT NULL DEFAULT 1,
            is_enabled BOOLEAN NOT NULL DEFAULT 0,
            last_seen_at DATETIME,
            attributes_synced_at DATETIME,
            attributes_sync_status VARCHAR(30),
            attributes_sync_error VARCHAR(1000),
            attributes_schema_hash VARCHAR(64),
            attributes_version INTEGER NOT NULL DEFAULT 0,
            attributes_count INTEGER NOT NULL DEFAULT 0,
            required_attributes_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_category_product_type UNIQUE (
                category_id, external_type_id
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_attribute_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            product_type_id INTEGER NOT NULL REFERENCES marketplace_product_types(id),
            external_attribute_id VARCHAR(100) NOT NULL,
            name VARCHAR(500) NOT NULL,
            description TEXT,
            data_type VARCHAR(100) NOT NULL,
            is_required BOOLEAN NOT NULL DEFAULT 0,
            dictionary_id VARCHAR(100),
            max_value_count INTEGER NOT NULL DEFAULT 0,
            attribute_complex_id VARCHAR(100),
            complex_is_collection BOOLEAN NOT NULL DEFAULT 0,
            is_collection BOOLEAN NOT NULL DEFAULT 0,
            category_dependent BOOLEAN NOT NULL DEFAULT 0,
            group_id VARCHAR(100),
            group_name VARCHAR(500),
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_enabled BOOLEAN NOT NULL DEFAULT 1,
            is_available BOOLEAN NOT NULL DEFAULT 1,
            last_seen_at DATETIME,
            ai_instruction TEXT,
            ai_instruction_source VARCHAR(20) NOT NULL DEFAULT 'generated',
            ai_example_value VARCHAR(500),
            restriction_value_ids_json TEXT,
            values_synced_at DATETIME,
            values_sync_status VARCHAR(30),
            values_sync_error VARCHAR(1000),
            values_snapshot_hash VARCHAR(64),
            values_version INTEGER NOT NULL DEFAULT 0,
            values_count INTEGER NOT NULL DEFAULT 0,
            values_sync_checkpoint VARCHAR(200),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_product_type_attribute UNIQUE (
                product_type_id, external_attribute_id
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_attribute_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            product_type_id INTEGER NOT NULL REFERENCES marketplace_product_types(id),
            attribute_id INTEGER NOT NULL REFERENCES marketplace_attribute_definitions(id),
            external_value_id VARCHAR(100) NOT NULL,
            value VARCHAR(1000) NOT NULL,
            value_normalized VARCHAR(1000) NOT NULL,
            info TEXT,
            picture_url VARCHAR(2000),
            is_available BOOLEAN NOT NULL DEFAULT 1,
            last_seen_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_attribute_value UNIQUE (
                attribute_id, external_value_id
            )
        )
    ''')

    # Existing P2 development databases may already have the table from an
    # earlier idempotent run. Additive repair keeps the migration repeatable.
    attribute_columns = _columns(connection, "marketplace_attribute_definitions")
    if "restriction_value_ids_json" not in attribute_columns:
        connection.execute(
            "ALTER TABLE marketplace_attribute_definitions "
            "ADD COLUMN restriction_value_ids_json TEXT"
        )

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_reference_accounts_marketplace_id "
        "ON marketplace_reference_accounts(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_taxonomy_categories_marketplace_id "
        "ON marketplace_taxonomy_categories(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_taxonomy_parent "
        "ON marketplace_taxonomy_categories(marketplace_id, parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_taxonomy_available "
        "ON marketplace_taxonomy_categories(marketplace_id, is_available)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_product_types_marketplace_id "
        "ON marketplace_product_types(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_product_types_category_id "
        "ON marketplace_product_types(category_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_product_type_enabled "
        "ON marketplace_product_types(marketplace_id, is_enabled, is_available)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_attribute_definitions_marketplace_id "
        "ON marketplace_attribute_definitions(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_attribute_definitions_product_type_id "
        "ON marketplace_attribute_definitions(product_type_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_attribute_required "
        "ON marketplace_attribute_definitions(product_type_id, is_required, is_available)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_attribute_dictionary "
        "ON marketplace_attribute_definitions(product_type_id, dictionary_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_attribute_values_marketplace_id "
        "ON marketplace_attribute_values(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_attribute_values_product_type_id "
        "ON marketplace_attribute_values(product_type_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_attribute_values_attribute_id "
        "ON marketplace_attribute_values(attribute_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_attribute_value_lookup "
        "ON marketplace_attribute_values(attribute_id, value_normalized, is_available)",
    )
    for statement in statements:
        connection.execute(statement)

    after = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
    }
    if verbose:
        print("Ozon reference schema migration completed successfully!")
    return len(after - before)


def migrate(db_path: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
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
