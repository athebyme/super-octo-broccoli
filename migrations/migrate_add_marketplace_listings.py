#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add the durable marketplace listing projection and catalog sync journal.

The migration is additive and repeatable.  Its historical direct-call contract
still performs the complete idempotent WB compatibility backfill.  P11 startup
paths explicitly pass a 200-row limit so deploy latency is never proportional
to catalog size; the durable runtime worker resumes the remaining keyset.
"""

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, Iterable, Optional


MAX_JSON_BYTES = 262_144
MAX_DESCRIPTION_CHARS = 100_000
STARTUP_BACKFILL_LIMIT = 200


def _tables(connection: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(connection: sqlite3.Connection, table_name: str) -> set:
    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def _canonical_json(value: Any, fallback: Any) -> str:
    if not isinstance(value, type(fallback)):
        value = fallback
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        encoded = json.dumps(
            fallback,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return encoded


def _legacy_json(raw: Any, fallback: Any) -> str:
    if raw in (None, ""):
        return _canonical_json(fallback, fallback)
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        value = fallback
    return _canonical_json(value, fallback)


def _text(value: Any, maximum: int) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized[:maximum] if normalized else None


def _fingerprint(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _ensure_schema(connection: sqlite3.Connection) -> None:
    required = {
        "sellers",
        "marketplaces",
        "seller_marketplace_accounts",
        "products",
        "imported_products",
        "marketplace_product_types",
    }
    missing = sorted(required - _tables(connection))
    if missing:
        raise RuntimeError(
            "Marketplace listing migration prerequisites are missing: "
            + ", ".join(missing)
        )

    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_catalog_syncs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL
                REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            phase VARCHAR(20) NOT NULL DEFAULT 'active',
            visibility VARCHAR(30) NOT NULL DEFAULT 'ALL',
            cursor VARCHAR(1000) NOT NULL DEFAULT '',
            phase_seen_count INTEGER NOT NULL DEFAULT 0,
            phase_expected_total INTEGER,
            page_count INTEGER NOT NULL DEFAULT 0,
            seen_count INTEGER NOT NULL DEFAULT 0,
            created_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            missing_count INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            heartbeat_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_catalog_sync_status CHECK (
                status IN ('running','paused','completed','failed')
            ),
            CONSTRAINT ck_marketplace_catalog_sync_phase CHECK (
                phase IN ('active','archived','finalize','completed')
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER
                REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            legacy_product_id INTEGER UNIQUE
                REFERENCES products(id) ON DELETE CASCADE,
            imported_product_id INTEGER
                REFERENCES imported_products(id) ON DELETE SET NULL,
            product_type_id INTEGER
                REFERENCES marketplace_product_types(id) ON DELETE SET NULL,
            last_catalog_sync_id INTEGER
                REFERENCES marketplace_catalog_syncs(id) ON DELETE SET NULL,
            last_catalog_sync_phase VARCHAR(20),
            offer_id VARCHAR(200) NOT NULL,
            external_product_id VARCHAR(100) NOT NULL,
            primary_sku VARCHAR(100),
            identifiers_json TEXT NOT NULL DEFAULT '{}',
            external_category_id VARCHAR(100),
            external_type_id VARCHAR(100),
            title VARCHAR(500),
            description TEXT,
            normalized_status VARCHAR(30) NOT NULL DEFAULT 'unknown',
            provider_status VARCHAR(200),
            visibility VARCHAR(100),
            is_archived BOOLEAN NOT NULL DEFAULT 0,
            is_available BOOLEAN NOT NULL DEFAULT 1,
            has_fbo_stocks BOOLEAN NOT NULL DEFAULT 0,
            has_fbs_stocks BOOLEAN NOT NULL DEFAULT 0,
            statuses_json TEXT NOT NULL DEFAULT '{}',
            moderation_errors_json TEXT NOT NULL DEFAULT '[]',
            attributes_json TEXT NOT NULL DEFAULT '[]',
            complex_attributes_json TEXT NOT NULL DEFAULT '[]',
            media_json TEXT NOT NULL DEFAULT '{}',
            dimensions_json TEXT NOT NULL DEFAULT '{}',
            barcodes_json TEXT NOT NULL DEFAULT '[]',
            price_summary_json TEXT NOT NULL DEFAULT '{}',
            stock_summary_json TEXT NOT NULL DEFAULT '{}',
            upstream_created_at DATETIME,
            upstream_updated_at DATETIME,
            list_synced_at DATETIME,
            info_synced_at DATETIME,
            attributes_synced_at DATETIME,
            prices_synced_at DATETIME,
            stocks_synced_at DATETIME,
            last_seen_at DATETIME,
            sync_fingerprint VARCHAR(64) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_listing_account_offer
                UNIQUE (account_id, offer_id),
            CONSTRAINT uq_marketplace_listing_account_product
                UNIQUE (account_id, external_product_id),
            CONSTRAINT ck_marketplace_listing_normalized_status CHECK (
                normalized_status IN (
                    'active','moderation','creating','error',
                    'archived','inactive','unknown'
                )
            )
        )
    ''')

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_catalog_syncs_seller_id "
        "ON marketplace_catalog_syncs(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_catalog_syncs_marketplace_id "
        "ON marketplace_catalog_syncs(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_catalog_syncs_account_id "
        "ON marketplace_catalog_syncs(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_catalog_sync_account_status "
        "ON marketplace_catalog_syncs(seller_id, account_id, status, updated_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_catalog_sync_running "
        "ON marketplace_catalog_syncs(account_id) WHERE status = 'running'",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listings_seller_id "
        "ON marketplace_listings(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listings_marketplace_id "
        "ON marketplace_listings(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listings_account_id "
        "ON marketplace_listings(account_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_marketplace_listings_legacy_product_id "
        "ON marketplace_listings(legacy_product_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listings_imported_product_id "
        "ON marketplace_listings(imported_product_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listings_product_type_id "
        "ON marketplace_listings(product_type_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listings_last_catalog_sync_id "
        "ON marketplace_listings(last_catalog_sync_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_listing_seller_scope "
        "ON marketplace_listings(seller_id, marketplace_id, is_available)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_listing_account_status "
        "ON marketplace_listings(account_id, normalized_status, is_available)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_listing_category_type "
        "ON marketplace_listings("
        "marketplace_id, external_category_id, external_type_id)",
    )
    for statement in statements:
        connection.execute(statement)

    # Additive repair for databases that ran an earlier development build of
    # this still-idempotent migration before phase-level duplicate detection.
    listing_columns = _columns(connection, "marketplace_listings")
    if "last_catalog_sync_phase" not in listing_columns:
        connection.execute(
            "ALTER TABLE marketplace_listings "
            "ADD COLUMN last_catalog_sync_phase VARCHAR(20)"
        )

    expected_sync_columns = {
        "id", "seller_id", "marketplace_id", "account_id", "status",
        "phase", "visibility", "cursor", "phase_seen_count",
        "phase_expected_total", "page_count", "seen_count", "created_count",
        "updated_count", "missing_count", "warning_count", "error_code",
        "error_message", "started_at", "heartbeat_at", "completed_at",
        "created_at", "updated_at",
    }
    expected_listing_columns = {
        "id", "seller_id", "marketplace_id", "account_id",
        "legacy_product_id", "imported_product_id", "product_type_id",
        "last_catalog_sync_id", "last_catalog_sync_phase", "offer_id",
        "external_product_id",
        "primary_sku", "identifiers_json", "external_category_id",
        "external_type_id", "title", "description", "normalized_status",
        "provider_status", "visibility", "is_archived", "is_available",
        "has_fbo_stocks", "has_fbs_stocks", "statuses_json",
        "moderation_errors_json", "attributes_json",
        "complex_attributes_json", "media_json", "dimensions_json",
        "barcodes_json", "price_summary_json", "stock_summary_json",
        "upstream_created_at", "upstream_updated_at", "list_synced_at",
        "info_synced_at", "attributes_synced_at", "prices_synced_at",
        "stocks_synced_at", "last_seen_at", "sync_fingerprint",
        "created_at", "updated_at",
    }
    missing_sync = expected_sync_columns - _columns(
        connection, "marketplace_catalog_syncs"
    )
    missing_listing = expected_listing_columns - _columns(
        connection, "marketplace_listings"
    )
    if missing_sync or missing_listing:
        raise RuntimeError(
            "Marketplace listing schema is incomplete: "
            f"sync={sorted(missing_sync)}, listings={sorted(missing_listing)}"
        )


def _select_products(
    connection: sqlite3.Connection,
    *,
    limit: Optional[int],
) -> Iterable[sqlite3.Row]:
    product_columns = _columns(connection, "products")
    optional = (
        "imt_id", "vendor_code", "title", "description", "subject_id",
        "is_active", "photos_json", "characteristics_json", "dimensions_json",
        "price", "discount_price", "quantity", "last_sync", "created_at",
        "updated_at",
    )
    select_columns = ["id", "seller_id", "nm_id"] + [
        name for name in optional if name in product_columns
    ]
    connection.row_factory = sqlite3.Row
    if limit is None:
        # Preserve the deployed migration's original direct-call contract.
        return connection.execute(
            "SELECT " + ", ".join(select_columns) + " FROM products ORDER BY id"
        ).fetchall()
    return connection.execute(
        "SELECT "
        + ", ".join(f"p.{name} AS {name}" for name in select_columns)
        + " FROM products AS p "
        "LEFT JOIN marketplace_listings AS ml "
        "ON ml.legacy_product_id = p.id "
        "WHERE ml.id IS NULL ORDER BY p.id LIMIT ?",
        (limit,),
    ).fetchall()


def _backfill_wb(
    connection: sqlite3.Connection,
    *,
    limit: Optional[int],
) -> int:
    if limit is not None and (
        not isinstance(limit, int) or isinstance(limit, bool) or limit < 0
    ):
        raise ValueError("backfill limit must be a non-negative integer")
    if limit == 0:
        return 0
    marketplace = connection.execute(
        "SELECT id FROM marketplaces WHERE code = 'wb' LIMIT 1"
    ).fetchone()
    if marketplace is None:
        raise RuntimeError("WB marketplace definition is missing")
    wb_marketplace_id = int(marketplace[0])

    product_rows = list(_select_products(connection, limit=limit))
    product_ids = [int(row["id"]) for row in product_rows]

    imported_by_product: Dict[int, int] = {}
    if product_ids and "product_id" in _columns(connection, "imported_products"):
        if limit is None:
            imported_rows = connection.execute('''
                SELECT product_id, MAX(id)
                FROM imported_products
                WHERE product_id IS NOT NULL
                GROUP BY product_id
            ''').fetchall()
        else:
            placeholders = ",".join("?" for _ in product_ids)
            imported_rows = connection.execute(f'''
                SELECT product_id, MAX(id)
                FROM imported_products
                WHERE product_id IN ({placeholders})
                GROUP BY product_id
            ''', product_ids).fetchall()
        imported_by_product = {
            int(row[0]): int(row[1])
            for row in imported_rows
        }

    inserted = 0
    for row in product_rows:
        data = dict(row)
        product_id = int(data["id"])
        if connection.execute(
            "SELECT 1 FROM marketplace_listings WHERE legacy_product_id = ?",
            (product_id,),
        ).fetchone():
            continue

        nm_id = str(data["nm_id"])
        offer_id = _text(data.get("vendor_code"), 200) or f"wb-nm-{nm_id}"
        is_active = bool(data.get("is_active", True))
        identifiers = {"nm_id": nm_id}
        if data.get("imt_id") is not None:
            identifiers["imt_id"] = str(data["imt_id"])
        identifiers_json = _canonical_json(identifiers, {})
        media_json = _canonical_json(
            {"photos": json.loads(_legacy_json(data.get("photos_json"), []))},
            {},
        )
        attributes_json = _legacy_json(
            data.get("characteristics_json"), []
        )
        dimensions_json = _legacy_json(data.get("dimensions_json"), {})
        price_summary = {
            "available": data.get("price") is not None,
            "currency": "RUB",
            "price": (
                str(data["price"]) if data.get("price") is not None else None
            ),
            "discount_price": (
                str(data["discount_price"])
                if data.get("discount_price") is not None else None
            ),
            "source": "legacy_wb_projection",
        }
        stock_summary = {
            "available": True,
            "present": int(data.get("quantity") or 0),
            "source": "legacy_wb_projection",
        }
        snapshot = {
            "offer_id": offer_id,
            "external_product_id": nm_id,
            "title": _text(data.get("title"), 500),
            "category_id": (
                str(data["subject_id"])
                if data.get("subject_id") is not None else None
            ),
            "status": "active" if is_active else "inactive",
            "identifiers": identifiers,
            "media": json.loads(media_json),
            "attributes": json.loads(attributes_json),
            "dimensions": json.loads(dimensions_json),
            "price": price_summary,
            "stock": stock_summary,
        }
        fingerprint = _fingerprint(snapshot)
        last_seen_at = (
            data.get("last_sync")
            or data.get("updated_at")
            or data.get("created_at")
        )
        cursor = connection.execute('''
            INSERT OR IGNORE INTO marketplace_listings (
                seller_id, marketplace_id, account_id, legacy_product_id,
                imported_product_id, product_type_id, last_catalog_sync_id,
                last_catalog_sync_phase,
                offer_id, external_product_id, primary_sku, identifiers_json,
                external_category_id, external_type_id, title, description,
                normalized_status, provider_status, visibility, is_archived,
                is_available, has_fbo_stocks, has_fbs_stocks, statuses_json,
                moderation_errors_json, attributes_json,
                complex_attributes_json, media_json, dimensions_json,
                barcodes_json, price_summary_json, stock_summary_json,
                upstream_created_at, upstream_updated_at, list_synced_at,
                info_synced_at, attributes_synced_at, prices_synced_at,
                stocks_synced_at, last_seen_at, sync_fingerprint,
                created_at, updated_at
            ) VALUES (
                :seller_id, :marketplace_id, NULL, :legacy_product_id,
                :imported_product_id, NULL, NULL, NULL,
                :offer_id, :external_product_id, NULL, :identifiers_json,
                :external_category_id, NULL, :title, :description,
                :normalized_status, 'legacy_wb_projection', :visibility, 0,
                1, 0, 0, '{}', '[]', :attributes_json, '[]', :media_json,
                :dimensions_json, '[]', :price_summary_json,
                :stock_summary_json, NULL, NULL, :list_synced_at, NULL, NULL,
                NULL, NULL, :last_seen_at, :sync_fingerprint,
                COALESCE(:created_at, CURRENT_TIMESTAMP),
                COALESCE(:updated_at, CURRENT_TIMESTAMP)
            )
        ''', {
            "seller_id": int(data["seller_id"]),
            "marketplace_id": wb_marketplace_id,
            "legacy_product_id": product_id,
            "imported_product_id": imported_by_product.get(product_id),
            "offer_id": offer_id,
            "external_product_id": nm_id,
            "identifiers_json": identifiers_json,
            "external_category_id": (
                str(data["subject_id"])
                if data.get("subject_id") is not None else None
            ),
            "title": _text(data.get("title"), 500),
            "description": _text(
                data.get("description"), MAX_DESCRIPTION_CHARS
            ),
            "normalized_status": "active" if is_active else "inactive",
            "visibility": "active" if is_active else "inactive",
            "attributes_json": attributes_json,
            "media_json": media_json,
            "dimensions_json": dimensions_json,
            "price_summary_json": _canonical_json(price_summary, {}),
            "stock_summary_json": _canonical_json(stock_summary, {}),
            "list_synced_at": data.get("last_sync"),
            "last_seen_at": last_seen_at,
            "sync_fingerprint": fingerprint,
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
        })
        inserted += max(int(cursor.rowcount or 0), 0)
    return inserted


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
    backfill_limit: Optional[int] = None,
) -> int:
    before = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    _ensure_schema(connection)
    inserted = _backfill_wb(connection, limit=backfill_limit)
    after = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    if verbose:
        if backfill_limit is None:
            print(
                "Marketplace listing migration completed successfully "
                f"(WB backfill inserted {inserted})"
            )
        else:
            print(
                "Marketplace listing migration completed successfully "
                f"(bounded WB backfill inserted {inserted}, "
                f"limit {backfill_limit})"
            )
    return len(after - before) + inserted


def migrate(
    db_path: str,
    *,
    backfill_limit: Optional[int] = None,
) -> None:
    connection = sqlite3.connect(db_path)
    try:
        apply_migration(connection, backfill_limit=backfill_limit)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "database",
        nargs="?",
        default=os.environ.get("DATABASE_PATH", "data/seller_platform.db"),
    )
    parser.add_argument("--backfill-limit", type=int)
    arguments = parser.parse_args()
    migrate(arguments.database, backfill_limit=arguments.backfill_limit)
