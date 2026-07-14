# -*- coding: utf-8 -*-
"""Idempotent marketplace account schema and definition seeds."""

import json
import sqlite3

from migrations.migrate_add_marketplace_accounts import (
    OZON_ENDPOINT_VERSIONS,
    apply_migration,
)


def test_marketplace_account_migration_is_idempotent_and_seeds_current_ozon():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE sellers (id INTEGER PRIMARY KEY)")
        apply_migration(connection, verbose=False)
        apply_migration(connection, verbose=False)
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(seller_marketplace_accounts)"
            ).fetchall()
        }
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(seller_marketplace_accounts)"
            ).fetchall()
        }
        ozon = connection.execute(
            "SELECT adapter_code, api_base_url, capability_versions_json "
            "FROM marketplaces WHERE code='ozon'"
        ).fetchone()
        connection.execute("INSERT INTO sellers(id) VALUES (1)")
        marketplace_id = connection.execute(
            "SELECT id FROM marketplaces WHERE code='ozon'"
        ).fetchone()[0]
        connection.execute(
            '''
            INSERT INTO seller_marketplace_accounts (
                seller_id, marketplace_id, external_account_id, label
            ) VALUES (1, ?, '123', 'Test')
            ''',
            (marketplace_id,),
        )
        duplicate_failed = False
        try:
            connection.execute(
                '''
                INSERT INTO seller_marketplace_accounts (
                    seller_id, marketplace_id, external_account_id, label
                ) VALUES (1, ?, '123', 'Duplicate')
                ''',
                (marketplace_id,),
            )
        except sqlite3.IntegrityError:
            duplicate_failed = True
    finally:
        connection.close()

    assert {
        "seller_id",
        "marketplace_id",
        "external_account_id",
        "credentials_encrypted",
        "connection_status",
        "capabilities_json",
        "version",
        "created_at",
    }.issubset(columns)
    assert "idx_seller_mp_account_active" in indexes
    assert "idx_seller_mp_account_default" in indexes
    assert "uq_seller_mp_account_one_default" in indexes
    assert ozon[0] == "ozon"
    assert ozon[1] == "https://api-seller.ozon.ru"
    assert json.loads(ozon[2]) == OZON_ENDPOINT_VERSIONS
    assert duplicate_failed
