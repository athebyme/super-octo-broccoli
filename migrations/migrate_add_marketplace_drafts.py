#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add seller-scoped marketplace category mappings and product drafts."""

import os
import sqlite3
import sys


def _schema_objects(connection: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }


def _columns(connection: sqlite3.Connection, table_name: str) -> set:
    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def _require_prerequisites(connection: sqlite3.Connection) -> None:
    required = {
        "sellers",
        "users",
        "suppliers",
        "supplier_products",
        "imported_products",
        "marketplaces",
        "seller_marketplace_accounts",
        "marketplace_product_types",
        "marketplace_listings",
    }
    missing = sorted(required - _schema_objects(connection))
    if missing:
        raise sqlite3.OperationalError(
            "Marketplace draft migration prerequisites are missing: "
            + ", ".join(missing)
        )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    _require_prerequisites(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_category_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id) ON DELETE CASCADE,
            supplier_id INTEGER REFERENCES suppliers(id) ON DELETE CASCADE,
            product_type_id INTEGER NOT NULL REFERENCES marketplace_product_types(id) ON DELETE CASCADE,
            scope_key VARCHAR(220) NOT NULL,
            source_type VARCHAR(80) NOT NULL,
            source_category VARCHAR(500) NOT NULL,
            source_category_normalized VARCHAR(500) NOT NULL,
            external_category_id VARCHAR(100) NOT NULL,
            external_type_id VARCHAR(100) NOT NULL,
            mapping_source VARCHAR(30) NOT NULL DEFAULT 'manual',
            mapping_status VARCHAR(20) NOT NULL DEFAULT 'active',
            confidence FLOAT NOT NULL DEFAULT 1.0,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            corrected_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_category_mapping_scope UNIQUE (
                seller_id, marketplace_id, scope_key,
                source_category_normalized
            ),
            CONSTRAINT ck_marketplace_category_mapping_source CHECK (
                mapping_source IN ('manual','deterministic','ai')
            ),
            CONSTRAINT ck_marketplace_category_mapping_status CHECK (
                mapping_status IN ('suggested','active','rejected','stale')
            ),
            CONSTRAINT ck_marketplace_category_mapping_confidence CHECK (
                confidence >= 0 AND confidence <= 1
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_product_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            imported_product_id INTEGER NOT NULL REFERENCES imported_products(id) ON DELETE CASCADE,
            supplier_product_id INTEGER REFERENCES supplier_products(id) ON DELETE SET NULL,
            product_type_id INTEGER REFERENCES marketplace_product_types(id) ON DELETE SET NULL,
            category_mapping_id INTEGER REFERENCES marketplace_category_mappings(id) ON DELETE SET NULL,
            published_listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            offer_id VARCHAR(200) NOT NULL,
            external_category_id VARCHAR(100),
            external_type_id VARCHAR(100),
            status VARCHAR(30) NOT NULL DEFAULT 'needs_category',
            source_fact_hash VARCHAR(64) NOT NULL,
            source_facts_json TEXT NOT NULL DEFAULT '{}',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            content_json TEXT NOT NULL DEFAULT '{}',
            attributes_json TEXT NOT NULL DEFAULT '[]',
            complex_attributes_json TEXT NOT NULL DEFAULT '[]',
            media_json TEXT NOT NULL DEFAULT '{}',
            dimensions_json TEXT NOT NULL DEFAULT '{}',
            barcodes_json TEXT NOT NULL DEFAULT '[]',
            commercial_json TEXT NOT NULL DEFAULT '{}',
            schema_version INTEGER,
            schema_hash VARCHAR(64),
            validation_status VARCHAR(30) NOT NULL DEFAULT 'never_validated',
            validation_result_json TEXT NOT NULL DEFAULT '{}',
            validated_at DATETIME,
            version INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_draft_account_imported UNIQUE (
                account_id, imported_product_id
            ),
            CONSTRAINT uq_marketplace_draft_account_offer UNIQUE (
                account_id, offer_id
            ),
            CONSTRAINT ck_marketplace_product_draft_status CHECK (
                status IN (
                    'needs_category','draft','blocked','ready',
                    'published','archived'
                )
            ),
            CONSTRAINT ck_marketplace_product_draft_validation_status CHECK (
                validation_status IN (
                    'never_validated','invalid','valid','stale'
                )
            )
        )
    ''')

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_category_mappings_seller_id "
        "ON marketplace_category_mappings(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_category_mappings_marketplace_id "
        "ON marketplace_category_mappings(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_category_mappings_supplier_id "
        "ON marketplace_category_mappings(supplier_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_category_mappings_product_type_id "
        "ON marketplace_category_mappings(product_type_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_category_mapping_lookup "
        "ON marketplace_category_mappings("
        "seller_id, marketplace_id, scope_key, source_category_normalized, "
        "mapping_status)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_product_drafts_seller_id "
        "ON marketplace_product_drafts(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_product_drafts_marketplace_id "
        "ON marketplace_product_drafts(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_product_drafts_account_id "
        "ON marketplace_product_drafts(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_product_drafts_imported_product_id "
        "ON marketplace_product_drafts(imported_product_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_product_drafts_supplier_product_id "
        "ON marketplace_product_drafts(supplier_product_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_product_drafts_product_type_id "
        "ON marketplace_product_drafts(product_type_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_product_drafts_category_mapping_id "
        "ON marketplace_product_drafts(category_mapping_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_product_drafts_published_listing_id "
        "ON marketplace_product_drafts(published_listing_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_draft_seller_status "
        "ON marketplace_product_drafts("
        "seller_id, marketplace_id, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_draft_account_type "
        "ON marketplace_product_drafts(account_id, product_type_id)",
    )
    for statement in statements:
        connection.execute(statement)

    expected = {
        "marketplace_category_mappings": {
            "seller_id",
            "marketplace_id",
            "scope_key",
            "source_category_normalized",
            "product_type_id",
            "mapping_status",
        },
        "marketplace_product_drafts": {
            "seller_id",
            "marketplace_id",
            "account_id",
            "imported_product_id",
            "offer_id",
            "source_fact_hash",
            "attributes_json",
            "validation_status",
            "version",
        },
    }
    for table_name, columns in expected.items():
        missing = columns - _columns(connection, table_name)
        if missing:
            raise sqlite3.OperationalError(
                f"{table_name} is missing columns: {', '.join(sorted(missing))}"
            )


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    before = _schema_objects(connection)
    _ensure_schema(connection)
    after = _schema_objects(connection)
    if verbose:
        print("Marketplace draft schema migration completed successfully!")
    return len(after - before)


def migrate(db_path: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
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
