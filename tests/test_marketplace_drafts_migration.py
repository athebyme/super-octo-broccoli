# -*- coding: utf-8 -*-
"""Marketplace draft migration is additive, repeatable and constrained."""

import sqlite3

from migrations.migrate_add_marketplace_drafts import apply_migration
from migrations.migrate_add_marketplace_listings import (
    apply_migration as apply_listings,
)
from migrations.migrate_add_ozon_references import (
    apply_migration as apply_references,
)


def _base_schema(connection):
    connection.executescript('''
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE suppliers (id INTEGER PRIMARY KEY);
        CREATE TABLE supplier_products (
            id INTEGER PRIMARY KEY,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id)
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            nm_id INTEGER NOT NULL
        );
        CREATE TABLE imported_products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            supplier_product_id INTEGER REFERENCES supplier_products(id),
            supplier_id INTEGER REFERENCES suppliers(id),
            product_id INTEGER REFERENCES products(id)
        );
    ''')


def test_marketplace_draft_migration_is_idempotent_and_enforces_scope():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        _base_schema(connection)
        apply_references(connection, verbose=False)
        apply_listings(connection, verbose=False)
        apply_migration(connection, verbose=False)
        apply_migration(connection, verbose=False)

        draft_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(marketplace_product_drafts)"
            ).fetchall()
        }
        mapping_indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(marketplace_category_mappings)"
            ).fetchall()
        }
        connection.executescript('''
            INSERT INTO users(id) VALUES (1);
            INSERT INTO sellers(id) VALUES (1);
            INSERT INTO suppliers(id) VALUES (1);
            INSERT INTO supplier_products(id, supplier_id) VALUES (1, 1);
            INSERT INTO imported_products(
                id, seller_id, supplier_product_id, supplier_id
            ) VALUES (1, 1, 1, 1), (2, 1, 1, 1);
        ''')
        marketplace_id = connection.execute(
            "SELECT id FROM marketplaces WHERE code='ozon'"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO marketplace_taxonomy_categories (
                marketplace_id, external_category_id, name, full_path
            ) VALUES (?, '10', 'Category', 'Category')
        ''', (marketplace_id,))
        category_id = connection.execute(
            "SELECT id FROM marketplace_taxonomy_categories"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO marketplace_product_types (
                marketplace_id, category_id, external_type_id, name,
                is_available, is_enabled
            ) VALUES (?, ?, '777', 'Type', 1, 1)
        ''', (marketplace_id, category_id))
        product_type_id = connection.execute(
            "SELECT id FROM marketplace_product_types"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO seller_marketplace_accounts (
                seller_id, marketplace_id, external_account_id, label
            ) VALUES (1, ?, 'client', 'Client')
        ''', (marketplace_id,))
        account_id = connection.execute(
            "SELECT id FROM seller_marketplace_accounts"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO marketplace_category_mappings (
                seller_id, marketplace_id, supplier_id, product_type_id,
                scope_key, source_type, source_category,
                source_category_normalized, external_category_id,
                external_type_id
            ) VALUES (1, ?, 1, ?, 'supplier:1', 'synthetic',
                      'Category', 'category', '10', '777')
        ''', (marketplace_id, product_type_id))
        mapping_id = connection.execute(
            "SELECT id FROM marketplace_category_mappings"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO marketplace_product_drafts (
                seller_id, marketplace_id, account_id, imported_product_id,
                supplier_product_id, product_type_id, category_mapping_id,
                offer_id, external_category_id, external_type_id,
                source_fact_hash
            ) VALUES (1, ?, ?, 1, 1, ?, ?, 'offer-1', '10', '777', ?)
        ''', (
            marketplace_id,
            account_id,
            product_type_id,
            mapping_id,
            "a" * 64,
        ))

        duplicate_offer_failed = False
        try:
            connection.execute('''
                INSERT INTO marketplace_product_drafts (
                    seller_id, marketplace_id, account_id,
                    imported_product_id, offer_id, source_fact_hash
                ) VALUES (1, ?, ?, 2, 'offer-1', ?)
            ''', (marketplace_id, account_id, "b" * 64))
        except sqlite3.IntegrityError:
            duplicate_offer_failed = True

        invalid_status_failed = False
        try:
            connection.execute('''
                UPDATE marketplace_product_drafts
                SET validation_status='maybe'
                WHERE imported_product_id=1
            ''')
        except sqlite3.IntegrityError:
            invalid_status_failed = True
    finally:
        connection.close()

    assert {
        "seller_id",
        "marketplace_id",
        "account_id",
        "imported_product_id",
        "source_facts_json",
        "provenance_json",
        "attributes_json",
        "complex_attributes_json",
        "schema_hash",
        "validation_result_json",
        "version",
    }.issubset(draft_columns)
    assert "idx_marketplace_category_mapping_lookup" in mapping_indexes
    assert duplicate_offer_failed
    assert invalid_status_failed
