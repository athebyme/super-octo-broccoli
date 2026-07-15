#!/usr/bin/env python3
"""Add marketplace-neutral customer inbox and local reply drafts.

The migration is additive and idempotent. It never reads marketplace
credentials or calls a provider.
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
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _ensure_partial_unique_index(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    index_name: str,
    columns: tuple,
    predicate: str,
) -> None:
    rows = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    metadata = next((row for row in rows if row[1] == index_name), None)
    actual_columns = tuple(
        row[2]
        for row in connection.execute(
            f"PRAGMA index_info({index_name})"
        ).fetchall()
    )
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    normalized_sql = " ".join(str(sql_row[0] if sql_row else "").lower().split())
    if (
        metadata is None
        or not bool(metadata[2])
        or len(metadata) < 5
        or not bool(metadata[4])
        or actual_columns != columns
        or f"where {predicate}" not in normalized_sql
    ):
        raise sqlite3.OperationalError(
            f"{index_name} is not the required partial unique index"
        )


def _ensure_prerequisites(connection: sqlite3.Connection) -> None:
    required: Dict[str, Set[str]] = {
        "sellers": {"id"},
        "users": {"id"},
        "marketplaces": {"id"},
        "seller_marketplace_accounts": {"id", "seller_id", "marketplace_id"},
        "marketplace_listings": {"id", "seller_id", "marketplace_id", "account_id"},
    }
    for table_name, columns in required.items():
        actual = _columns(connection, table_name)
        if not actual:
            raise sqlite3.OperationalError(
                f"marketplace inbox prerequisite table missing: {table_name}"
            )
        missing = columns - actual
        if missing:
            raise sqlite3.OperationalError(
                f"{table_name} is missing columns: " + ", ".join(sorted(missing))
            )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    _ensure_prerequisites(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_inbox_syncs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            source_kind VARCHAR(20) NOT NULL,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            current_status VARCHAR(20) NOT NULL DEFAULT 'NEW',
            next_cursor VARCHAR(200),
            page_count INTEGER NOT NULL DEFAULT 0,
            seen_count INTEGER NOT NULL DEFAULT 0,
            created_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            matched_count INTEGER NOT NULL DEFAULT 0,
            unmatched_count INTEGER NOT NULL DEFAULT 0,
            ambiguous_count INTEGER NOT NULL DEFAULT 0,
            contract_version VARCHAR(80) NOT NULL DEFAULT 'ozon-inbox-status-v1',
            request_fingerprint VARCHAR(64) NOT NULL,
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_page_at DATETIME,
            completed_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_inbox_sync_kind CHECK (
                source_kind IN ('review','question')
            ),
            CONSTRAINT ck_marketplace_inbox_sync_status CHECK (
                status IN ('running','completed','failed','cancelled')
            ),
            CONSTRAINT ck_marketplace_inbox_sync_current_status CHECK (
                current_status IN ('NEW','VIEWED','PROCESSED')
            ),
            CONSTRAINT ck_marketplace_inbox_sync_period CHECK (
                period_start <= period_end
            ),
            CONSTRAINT ck_marketplace_inbox_sync_counters CHECK (
                page_count >= 0 AND seen_count >= 0 AND created_count >= 0
                AND updated_count >= 0 AND matched_count >= 0
                AND unmatched_count >= 0 AND ambiguous_count >= 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_inbox_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            last_sync_id INTEGER REFERENCES marketplace_inbox_syncs(id) ON DELETE SET NULL,
            source_kind VARCHAR(20) NOT NULL,
            external_id VARCHAR(200) NOT NULL,
            external_sku VARCHAR(100) NOT NULL,
            match_status VARCHAR(20) NOT NULL,
            text TEXT,
            rating INTEGER,
            provider_status VARCHAR(20) NOT NULL,
            order_status VARCHAR(100),
            published_at DATETIME NOT NULL,
            is_rating_participant BOOLEAN,
            comments_count INTEGER NOT NULL DEFAULT 0,
            photos_count INTEGER NOT NULL DEFAULT 0,
            videos_count INTEGER NOT NULL DEFAULT 0,
            answers_count INTEGER NOT NULL DEFAULT 0,
            reply_eligible BOOLEAN NOT NULL DEFAULT 0,
            source_endpoint VARCHAR(100) NOT NULL,
            source_fingerprint VARCHAR(64) NOT NULL,
            last_seen_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_marketplace_inbox_item_external UNIQUE (
                account_id, source_kind, external_id
            ),
            CONSTRAINT ck_marketplace_inbox_item_kind CHECK (
                source_kind IN ('review','question')
            ),
            CONSTRAINT ck_marketplace_inbox_item_status CHECK (
                provider_status IN ('NEW','VIEWED','PROCESSED')
            ),
            CONSTRAINT ck_marketplace_inbox_item_match CHECK (
                match_status IN ('matched','unmatched','ambiguous')
            ),
            CONSTRAINT ck_marketplace_inbox_item_rating CHECK (
                (source_kind = 'review' AND rating BETWEEN 1 AND 5)
                OR (source_kind = 'question' AND rating IS NULL)
            ),
            CONSTRAINT ck_marketplace_inbox_item_counts CHECK (
                comments_count >= 0 AND photos_count >= 0 AND videos_count >= 0
                AND answers_count >= 0
            )
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_reply_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            inbox_item_id INTEGER NOT NULL REFERENCES marketplace_inbox_items(id) ON DELETE CASCADE,
            listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
            created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'draft',
            generation_mode VARCHAR(20) NOT NULL,
            text TEXT NOT NULL,
            source_fingerprint VARCHAR(64) NOT NULL,
            facts_fingerprint VARCHAR(64) NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            model_name VARCHAR(200),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_marketplace_reply_draft_status CHECK (
                status IN ('draft','superseded')
            ),
            CONSTRAINT ck_marketplace_reply_draft_mode CHECK (
                generation_mode IN ('ai','template')
            )
        )
    ''')

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_marketplace_inbox_syncs_seller_id ON marketplace_inbox_syncs(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_inbox_syncs_marketplace_id ON marketplace_inbox_syncs(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_inbox_syncs_account_id ON marketplace_inbox_syncs(account_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_inbox_running_kind ON marketplace_inbox_syncs(account_id, source_kind) WHERE status = 'running'",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_inbox_sync_scope ON marketplace_inbox_syncs(seller_id, account_id, source_kind, status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_inbox_items_seller_id ON marketplace_inbox_items(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_inbox_items_marketplace_id ON marketplace_inbox_items(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_inbox_items_account_id ON marketplace_inbox_items(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_inbox_items_listing_id ON marketplace_inbox_items(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_inbox_items_last_sync_id ON marketplace_inbox_items(last_sync_id)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_inbox_item_scope ON marketplace_inbox_items(seller_id, account_id, source_kind, provider_status, published_at)",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_inbox_item_listing ON marketplace_inbox_items(seller_id, account_id, listing_id, published_at)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_reply_drafts_seller_id ON marketplace_reply_drafts(seller_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_reply_drafts_marketplace_id ON marketplace_reply_drafts(marketplace_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_reply_drafts_account_id ON marketplace_reply_drafts(account_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_reply_drafts_inbox_item_id ON marketplace_reply_drafts(inbox_item_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_reply_drafts_listing_id ON marketplace_reply_drafts(listing_id)",
        "CREATE INDEX IF NOT EXISTS ix_marketplace_reply_drafts_created_by_user_id ON marketplace_reply_drafts(created_by_user_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_reply_active_item ON marketplace_reply_drafts(inbox_item_id) WHERE status = 'draft'",
        "CREATE INDEX IF NOT EXISTS idx_marketplace_reply_draft_scope ON marketplace_reply_drafts(seller_id, account_id, status, created_at)",
    )
    for statement in statements:
        connection.execute(statement)

    _ensure_partial_unique_index(
        connection,
        table_name="marketplace_inbox_syncs",
        index_name="uq_marketplace_inbox_running_kind",
        columns=("account_id", "source_kind"),
        predicate="status = 'running'",
    )
    _ensure_partial_unique_index(
        connection,
        table_name="marketplace_reply_drafts",
        index_name="uq_marketplace_reply_active_item",
        columns=("inbox_item_id",),
        predicate="status = 'draft'",
    )

    expected = {
        "marketplace_inbox_syncs": {
            "id", "seller_id", "marketplace_id", "account_id", "source_kind",
            "period_start", "period_end", "status", "current_status",
            "next_cursor", "page_count", "seen_count", "created_count",
            "updated_count", "matched_count", "unmatched_count",
            "ambiguous_count", "contract_version", "request_fingerprint",
            "error_code", "error_message", "started_at", "last_page_at",
            "completed_at", "created_at", "updated_at",
        },
        "marketplace_inbox_items": {
            "id", "seller_id", "marketplace_id", "account_id", "listing_id",
            "last_sync_id", "source_kind", "external_id", "external_sku",
            "match_status", "text", "rating", "provider_status",
            "order_status", "published_at", "is_rating_participant",
            "comments_count", "photos_count", "videos_count", "answers_count",
            "reply_eligible", "source_endpoint", "source_fingerprint",
            "last_seen_at", "created_at", "updated_at",
        },
        "marketplace_reply_drafts": {
            "id", "seller_id", "marketplace_id", "account_id", "inbox_item_id",
            "listing_id", "created_by_user_id", "status", "generation_mode",
            "text", "source_fingerprint", "facts_fingerprint", "content_hash",
            "model_name", "created_at", "updated_at",
        },
    }
    for table_name, required_columns in expected.items():
        missing = required_columns - _columns(connection, table_name)
        if missing:
            raise sqlite3.OperationalError(
                f"{table_name} is missing columns: " + ", ".join(sorted(missing))
            )


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    before = _objects(connection)
    _ensure_schema(connection)
    after = _objects(connection)
    if verbose:
        print("Marketplace inbox migration completed successfully!")
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
