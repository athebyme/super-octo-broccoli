#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add marketplace adapter metadata and seller-scoped encrypted accounts."""

import json
import os
import sqlite3
import sys


OZON_ENDPOINT_VERSIONS = {
    "roles": "/v1/roles",
    "seller_info": "/v1/seller/info",
    "product_operation_limits": "/v4/product/info/limit",
    "product_list": "/v3/product/list",
    "product_info_list": "/v3/product/info/list",
    "product_attributes": "/v4/product/info/attributes",
    "description_category_tree": "/v1/description-category/tree",
    "description_category_attributes": "/v1/description-category/attribute",
    "description_category_attribute_values": (
        "/v1/description-category/attribute/values"
    ),
    "description_category_attribute_values_search": (
        "/v1/description-category/attribute/values/search"
    ),
    "product_import": "/v3/product/import",
    "product_import_status": "/v1/product/import/info",
    "product_prices": "/v5/product/info/prices",
    "product_prices_update": "/v1/product/import/prices",
    "product_stocks": "/v4/product/info/stocks",
    "product_stocks_update": "/v2/products/stocks",
    "warehouses": "/v2/warehouse/list",
    "finance_accrual_by_day": "/v1/finance/accrual/by-day",
    "finance_compensation": "/v1/finance/compensation",
    "finance_decompensation": "/v1/finance/decompensation",
}

WB_ENDPOINT_VERSIONS = {
    "category_tree": "/content/v2/object/all",
    "category_attributes": "/content/v2/object/charcs/{subject_id}",
    "catalog_write": "/content/v2/cards/upload",
}


def _columns(connection: sqlite3.Connection, table_name: str) -> set:
    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def _ensure_marketplaces_table(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL,
            code VARCHAR(50) NOT NULL UNIQUE,
            logo_url VARCHAR(500),
            is_active BOOLEAN DEFAULT 1,
            api_base_url VARCHAR(500),
            api_key VARCHAR(500),
            api_version VARCHAR(20),
            adapter_code VARCHAR(50),
            capability_versions_json TEXT,
            categories_synced_at DATETIME,
            categories_sync_status VARCHAR(50),
            categories_sync_error TEXT,
            categories_version INTEGER NOT NULL DEFAULT 0,
            total_categories INTEGER DEFAULT 0,
            total_characteristics INTEGER DEFAULT 0,
            directories_synced_at DATETIME,
            directories_sync_status VARCHAR(50),
            directories_sync_error TEXT,
            directories_version INTEGER NOT NULL DEFAULT 0,
            brands_synced_at DATETIME,
            brands_sync_status VARCHAR(50),
            brands_sync_error TEXT,
            brands_version INTEGER NOT NULL DEFAULT 0,
            brands_sync_checkpoint TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    existing = _columns(connection, "marketplaces")
    if "adapter_code" not in existing:
        connection.execute(
            "ALTER TABLE marketplaces ADD COLUMN adapter_code VARCHAR(50)"
        )
    if "capability_versions_json" not in existing:
        connection.execute(
            "ALTER TABLE marketplaces ADD COLUMN capability_versions_json TEXT"
        )


def _seed_marketplaces(connection: sqlite3.Connection) -> None:
    connection.execute(
        '''
        INSERT OR IGNORE INTO marketplaces (
            name, code, is_active, api_base_url, api_version, adapter_code,
            capability_versions_json, categories_version, directories_version,
            brands_version, total_categories, total_characteristics,
            created_at, updated_at
        ) VALUES (?, ?, 1, ?, ?, ?, ?, 0, 0, 0, 0, 0,
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ''',
        (
            "Wildberries",
            "wb",
            "https://content-api.wildberries.ru",
            "v2",
            "wb",
            json.dumps(WB_ENDPOINT_VERSIONS, sort_keys=True),
        ),
    )
    connection.execute(
        '''
        INSERT OR IGNORE INTO marketplaces (
            name, code, is_active, api_base_url, api_version, adapter_code,
            capability_versions_json, categories_version, directories_version,
            brands_version, total_categories, total_characteristics,
            created_at, updated_at
        ) VALUES (?, ?, 1, ?, NULL, ?, ?, 0, 0, 0, 0, 0,
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ''',
        (
            "Ozon",
            "ozon",
            "https://api-seller.ozon.ru",
            "ozon",
            json.dumps(OZON_ENDPOINT_VERSIONS, sort_keys=True),
        ),
    )
    connection.execute(
        '''
        UPDATE marketplaces
        SET adapter_code = COALESCE(NULLIF(adapter_code, ''), code)
        WHERE code IN ('wb', 'ozon')
        ''',
    )
    connection.execute(
        '''
        UPDATE marketplaces
        SET api_base_url = COALESCE(NULLIF(api_base_url, ''), ?),
            capability_versions_json = ?
        WHERE code = 'ozon'
        ''',
        (
            "https://api-seller.ozon.ru",
            json.dumps(OZON_ENDPOINT_VERSIONS, sort_keys=True),
        ),
    )
    connection.execute(
        '''
        UPDATE marketplaces
        SET capability_versions_json = COALESCE(
            NULLIF(capability_versions_json, ''), ?
        )
        WHERE code = 'wb'
        ''',
        (json.dumps(WB_ENDPOINT_VERSIONS, sort_keys=True),),
    )


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    before = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
    }
    _ensure_marketplaces_table(connection)
    _seed_marketplaces(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS seller_marketplace_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            external_account_id VARCHAR(200) NOT NULL,
            label VARCHAR(120) NOT NULL,
            credentials_encrypted TEXT,
            credential_version INTEGER NOT NULL DEFAULT 1,
            credentials_updated_at DATETIME,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            is_default BOOLEAN NOT NULL DEFAULT 0,
            settings_json TEXT NOT NULL DEFAULT '{}',
            connection_status VARCHAR(30) NOT NULL DEFAULT 'unchecked',
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            roles_json TEXT NOT NULL DEFAULT '[]',
            credential_expires_at DATETIME,
            connection_checked_at DATETIME,
            provider_request_id VARCHAR(200),
            last_error_code VARCHAR(100),
            last_error_message VARCHAR(1000),
            version INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_seller_marketplace_external_account UNIQUE (
                seller_id, marketplace_id, external_account_id
            ),
            CONSTRAINT ck_seller_marketplace_connection_status CHECK (
                connection_status IN (
                    'unchecked', 'connected', 'invalid', 'limited',
                    'error', 'disconnected'
                )
            )
        )
    ''')
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_seller_marketplace_accounts_seller_id "
        "ON seller_marketplace_accounts(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_seller_marketplace_accounts_marketplace_id "
        "ON seller_marketplace_accounts(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS idx_seller_mp_account_active "
        "ON seller_marketplace_accounts(seller_id, marketplace_id, is_active)",
        "CREATE INDEX IF NOT EXISTS idx_seller_mp_account_default "
        "ON seller_marketplace_accounts(seller_id, marketplace_id, is_default)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_seller_mp_account_one_default "
        "ON seller_marketplace_accounts(seller_id, marketplace_id) "
        "WHERE is_default = 1",
    ):
        connection.execute(statement)

    after = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
    }
    if verbose:
        print("Marketplace accounts migration completed successfully!")
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
