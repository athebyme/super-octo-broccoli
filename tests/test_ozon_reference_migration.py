# -*- coding: utf-8 -*-
"""Idempotency and identity constraints for Ozon reference truth."""

import sqlite3

from migrations.migrate_add_ozon_references import apply_migration


def test_ozon_reference_migration_is_idempotent_and_pair_scoped():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE sellers (id INTEGER PRIMARY KEY)")
        apply_migration(connection, verbose=False)
        apply_migration(connection, verbose=False)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        marketplace_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(marketplaces)"
            ).fetchall()
        }
        type_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(marketplace_product_types)"
            ).fetchall()
        }
        attribute_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(marketplace_attribute_definitions)"
            ).fetchall()
        }

        marketplace_id = connection.execute(
            "SELECT id FROM marketplaces WHERE code='ozon'"
        ).fetchone()[0]
        connection.execute(
            '''
            INSERT INTO marketplace_taxonomy_categories (
                marketplace_id, external_category_id, name, full_path
            ) VALUES (?, '10', 'Category A', 'Category A')
            ''',
            (marketplace_id,),
        )
        category_a = connection.execute(
            "SELECT id FROM marketplace_taxonomy_categories "
            "WHERE external_category_id='10'"
        ).fetchone()[0]
        connection.execute(
            '''
            INSERT INTO marketplace_taxonomy_categories (
                marketplace_id, external_category_id, name, full_path
            ) VALUES (?, '11', 'Category B', 'Category B')
            ''',
            (marketplace_id,),
        )
        category_b = connection.execute(
            "SELECT id FROM marketplace_taxonomy_categories "
            "WHERE external_category_id='11'"
        ).fetchone()[0]
        # The same type_id may exist under another description category; the
        # domain identity is the exact pair, never type_id alone.
        connection.execute(
            '''
            INSERT INTO marketplace_product_types (
                marketplace_id, category_id, external_type_id, name
            ) VALUES (?, ?, '777', 'Type A')
            ''',
            (marketplace_id, category_a),
        )
        connection.execute(
            '''
            INSERT INTO marketplace_product_types (
                marketplace_id, category_id, external_type_id, name
            ) VALUES (?, ?, '777', 'Type B')
            ''',
            (marketplace_id, category_b),
        )
        pair_count = connection.execute(
            "SELECT COUNT(*) FROM marketplace_product_types "
            "WHERE external_type_id='777'"
        ).fetchone()[0]
    finally:
        connection.close()

    assert {
        "marketplace_reference_accounts",
        "marketplace_taxonomy_categories",
        "marketplace_product_types",
        "marketplace_attribute_definitions",
        "marketplace_attribute_values",
    }.issubset(tables)
    assert "categories_snapshot_hash" in marketplace_columns
    assert "total_product_types" in marketplace_columns
    assert {"category_id", "external_type_id", "attributes_schema_hash"}.issubset(
        type_columns
    )
    assert {
        "external_attribute_id",
        "attribute_complex_id",
        "category_dependent",
        "values_snapshot_hash",
        "restriction_value_ids_json",
    }.issubset(attribute_columns)
    assert pair_count == 2
