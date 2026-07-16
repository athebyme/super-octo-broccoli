#!/usr/bin/env python3
"""Add audited links from marketplace listings to canonical seller products.

``ImportedProduct`` remains the single seller-owned content/AI source.  The
migration only adds relationship metadata and an append-only audit journal; it
does not copy content, call a marketplace or invoke an LLM.
"""

import os
import sqlite3
import sys
from typing import Dict, Set


def _objects(connection: sqlite3.Connection) -> Set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
        if row[0]
    }


def _columns(connection: sqlite3.Connection, table_name: str) -> Set[str]:
    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def _ensure_prerequisites(connection: sqlite3.Connection) -> None:
    required: Dict[str, Set[str]] = {
        "sellers": {"id"},
        "users": {"id"},
        "marketplaces": {"id"},
        "seller_marketplace_accounts": {
            "id", "seller_id", "marketplace_id",
        },
        "imported_products": {"id", "seller_id", "product_id"},
        "marketplace_listings": {
            "id", "seller_id", "marketplace_id", "account_id",
            "legacy_product_id", "imported_product_id", "created_at",
            "updated_at",
        },
    }
    for table_name, required_columns in required.items():
        actual = _columns(connection, table_name)
        if not actual:
            raise sqlite3.OperationalError(
                f"marketplace product link prerequisite missing: {table_name}"
            )
        missing = required_columns - actual
        if missing:
            raise sqlite3.OperationalError(
                f"{table_name} is missing columns: "
                + ", ".join(sorted(missing))
            )


def _add_column(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    column_name: str,
    declaration: str,
) -> bool:
    if column_name in _columns(connection, table_name):
        return False
    connection.execute(
        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
    )
    return True


def _ensure_partial_unique_index(connection: sqlite3.Connection) -> None:
    index_name = "uq_marketplace_listing_account_canonical"
    metadata = next(
        (
            row
            for row in connection.execute(
                "PRAGMA index_list(marketplace_listings)"
            ).fetchall()
            if row[1] == index_name
        ),
        None,
    )
    columns = tuple(
        row[2]
        for row in connection.execute(
            f"PRAGMA index_info({index_name})"
        ).fetchall()
    )
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    normalized_sql = " ".join(
        str(sql_row[0] if sql_row else "").lower().split()
    )
    predicate = (
        "where account_id is not null and imported_product_id is not null"
    )
    if (
        metadata is None
        or not bool(metadata[2])
        or len(metadata) < 5
        or not bool(metadata[4])
        or columns != ("account_id", "imported_product_id")
        or predicate not in normalized_sql
    ):
        raise sqlite3.OperationalError(
            f"{index_name} is not the required partial unique index"
        )


def _ensure_schema(connection: sqlite3.Connection) -> int:
    _ensure_prerequisites(connection)
    added = 0
    for column_name, declaration in (
        ("link_status", "VARCHAR(20) NOT NULL DEFAULT 'unlinked'"),
        ("link_source", "VARCHAR(40)"),
        ("link_evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("link_version", "INTEGER NOT NULL DEFAULT 1"),
        ("linked_at", "DATETIME"),
        (
            "linked_by_user_id",
            "INTEGER REFERENCES users(id) ON DELETE SET NULL",
        ),
    ):
        added += int(_add_column(
            connection,
            table_name="marketplace_listings",
            column_name=column_name,
            declaration=declaration,
        ))

    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_listing_link_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL
                REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER
                REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            listing_id INTEGER NOT NULL
                REFERENCES marketplace_listings(id) ON DELETE CASCADE,
            previous_imported_product_id INTEGER,
            imported_product_id INTEGER,
            action VARCHAR(20) NOT NULL,
            source VARCHAR(40) NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            link_version INTEGER NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_listing_link_event_action CHECK (
                action IN (
                    'auto_link','manual_link','unlink','ambiguous','bootstrap'
                )
            ),
            CONSTRAINT ck_marketplace_listing_link_event_version CHECK (
                link_version >= 1
            ),
            CONSTRAINT uq_marketplace_listing_link_event_version UNIQUE (
                listing_id, link_version
            )
        )
    ''')

    duplicate = connection.execute('''
        SELECT 1
        FROM marketplace_listings
        WHERE account_id IS NOT NULL AND imported_product_id IS NOT NULL
        GROUP BY account_id, imported_product_id
        HAVING COUNT(*) > 1
        LIMIT 1
    ''').fetchone()
    if duplicate is not None:
        raise sqlite3.OperationalError(
            "duplicate marketplace listing canonical links require manual review"
        )

    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listings_linked_by_user_id "
        "ON marketplace_listings(linked_by_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_listing_link_state "
        "ON marketplace_listings(seller_id, marketplace_id, account_id, "
        "link_status, imported_product_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_marketplace_listing_account_canonical "
        "ON marketplace_listings(account_id, imported_product_id) "
        "WHERE account_id IS NOT NULL AND imported_product_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_link_events_seller_id "
        "ON marketplace_listing_link_events(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_link_events_marketplace_id "
        "ON marketplace_listing_link_events(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_link_events_account_id "
        "ON marketplace_listing_link_events(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_link_events_listing_id "
        "ON marketplace_listing_link_events(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_link_events_actor_user_id "
        "ON marketplace_listing_link_events(actor_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_listing_link_event_scope "
        "ON marketplace_listing_link_events(seller_id, listing_id, created_at)",
    ):
        connection.execute(statement)
    _ensure_partial_unique_index(connection)

    # Existing relationships came either from the WB compatibility backfill or
    # a successful Ozon publication.  Mark them linked without inventing match
    # confidence or reparsing content.
    connection.execute('''
        UPDATE marketplace_listings
        SET link_status = 'linked',
            link_source = COALESCE(
                link_source,
                CASE
                    WHEN legacy_product_id IS NOT NULL THEN 'wb_backfill'
                    ELSE 'existing_publication'
                END
            ),
            link_evidence_json = COALESCE(NULLIF(link_evidence_json, ''), '{}'),
            link_version = CASE
                WHEN link_version IS NULL OR link_version < 1 THEN 1
                ELSE link_version
            END,
            linked_at = COALESCE(linked_at, updated_at, created_at, CURRENT_TIMESTAMP)
        WHERE imported_product_id IS NOT NULL
    ''')
    connection.execute('''
        UPDATE marketplace_listings
        SET link_status = CASE
                WHEN link_status = 'ambiguous' THEN 'ambiguous'
                ELSE 'unlinked'
            END,
            link_version = CASE
                WHEN link_version IS NULL OR link_version < 1 THEN 1
                ELSE link_version
            END,
            link_evidence_json = COALESCE(NULLIF(link_evidence_json, ''), '{}')
        WHERE imported_product_id IS NULL
    ''')
    connection.execute('''
        INSERT OR IGNORE INTO marketplace_listing_link_events (
            seller_id, marketplace_id, account_id, listing_id,
            previous_imported_product_id, imported_product_id,
            action, source, evidence_json, actor_user_id,
            link_version, created_at
        )
        SELECT
            seller_id, marketplace_id, account_id, id,
            NULL, imported_product_id,
            'bootstrap', COALESCE(link_source, 'existing_publication'),
            '{}', NULL, link_version,
            COALESCE(linked_at, updated_at, created_at, CURRENT_TIMESTAMP)
        FROM marketplace_listings
        WHERE imported_product_id IS NOT NULL
    ''')

    required_listing_columns = {
        "link_status", "link_source", "link_evidence_json", "link_version",
        "linked_at", "linked_by_user_id",
    }
    missing = required_listing_columns - _columns(
        connection,
        "marketplace_listings",
    )
    if missing:
        raise sqlite3.OperationalError(
            "marketplace_listings is missing link columns: "
            + ", ".join(sorted(missing))
        )
    event_columns = {
        "id", "seller_id", "marketplace_id", "account_id", "listing_id",
        "previous_imported_product_id", "imported_product_id", "action",
        "source", "evidence_json", "actor_user_id", "link_version",
        "created_at",
    }
    missing = event_columns - _columns(
        connection,
        "marketplace_listing_link_events",
    )
    if missing:
        raise sqlite3.OperationalError(
            "marketplace_listing_link_events is missing columns: "
            + ", ".join(sorted(missing))
        )
    return added


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    before = _objects(connection)
    added_columns = _ensure_schema(connection)
    after = _objects(connection)
    if verbose:
        print("Marketplace product links migration completed successfully!")
    return added_columns + len(after - before)


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
