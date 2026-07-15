#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add reviewed Ozon observation -> canonical content proposals."""

import os
import sqlite3
import sys
from typing import Dict, Set


TABLE_NAME = "marketplace_canonical_content_proposals"


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
        "marketplaces": {"id", "code"},
        "seller_marketplace_accounts": {
            "id", "seller_id", "marketplace_id",
        },
        "marketplace_listings": {
            "id", "seller_id", "marketplace_id", "account_id",
            "imported_product_id",
        },
        "imported_products": {"id", "seller_id"},
        "agent_change_snapshots": {"id", "imported_product_id"},
    }
    for table_name, required_columns in required.items():
        actual = _columns(connection, table_name)
        if not actual:
            raise sqlite3.OperationalError(
                f"canonical content prerequisite missing: {table_name}"
            )
        missing = required_columns - actual
        if missing:
            raise sqlite3.OperationalError(
                f"{table_name} is missing columns: "
                + ", ".join(sorted(missing))
            )


def _ensure_partial_unique_index(connection: sqlite3.Connection) -> None:
    index_name = "uq_marketplace_canonical_content_pending"
    metadata = next(
        (
            row
            for row in connection.execute(
                f"PRAGMA index_list({TABLE_NAME})"
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
    if (
        metadata is None
        or not bool(metadata[2])
        or len(metadata) < 5
        or not bool(metadata[4])
        or columns != ("seller_id", "listing_id")
        or "where status = 'pending_review'" not in normalized_sql
    ):
        raise sqlite3.OperationalError(
            f"{index_name} is not the required partial unique index"
        )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    _ensure_prerequisites(connection)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL
                REFERENCES sellers(id) ON DELETE CASCADE,
            marketplace_id INTEGER NOT NULL
                REFERENCES marketplaces(id),
            account_id INTEGER NOT NULL
                REFERENCES seller_marketplace_accounts(id) ON DELETE CASCADE,
            listing_id INTEGER NOT NULL
                REFERENCES marketplace_listings(id) ON DELETE CASCADE,
            imported_product_id INTEGER NOT NULL
                REFERENCES imported_products(id) ON DELETE CASCADE,
            snapshot_id INTEGER UNIQUE
                REFERENCES agent_change_snapshots(id) ON DELETE SET NULL,
            created_by_user_id INTEGER
                REFERENCES users(id) ON DELETE SET NULL,
            reviewed_by_user_id INTEGER
                REFERENCES users(id) ON DELETE SET NULL,
            rolled_back_by_user_id INTEGER
                REFERENCES users(id) ON DELETE SET NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'pending_review'
                CONSTRAINT ck_marketplace_canonical_content_status CHECK (
                    status IN (
                        'pending_review', 'applied', 'rejected', 'conflict',
                        'rolled_back'
                    )
                ),
            fields_json TEXT NOT NULL,
            baseline_state_json TEXT NOT NULL,
            proposed_state_json TEXT NOT NULL,
            baseline_fingerprint VARCHAR(64) NOT NULL,
            source_fingerprint VARCHAR(64) NOT NULL,
            contract_version VARCHAR(80) NOT NULL
                DEFAULT 'ozon-canonical-common-content-v1',
            source_observed_at DATETIME NOT NULL,
            review_note VARCHAR(1000),
            error_code VARCHAR(100),
            error_message VARCHAR(1000),
            version INTEGER NOT NULL DEFAULT 1
                CONSTRAINT ck_marketplace_canonical_content_version CHECK (
                    version >= 1
                ),
            reviewed_at DATETIME,
            applied_at DATETIME,
            rolled_back_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_marketplace_canonical_content_scope "
        f"ON {TABLE_NAME} (seller_id, account_id, listing_id, created_at)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_marketplace_canonical_content_pending "
        f"ON {TABLE_NAME} (seller_id, listing_id) "
        "WHERE status = 'pending_review'"
    )

    required_columns = {
        "id", "seller_id", "marketplace_id", "account_id", "listing_id",
        "imported_product_id", "snapshot_id", "created_by_user_id",
        "reviewed_by_user_id", "rolled_back_by_user_id", "status",
        "fields_json", "baseline_state_json", "proposed_state_json",
        "baseline_fingerprint", "source_fingerprint", "contract_version",
        "source_observed_at", "review_note", "error_code", "error_message",
        "version", "reviewed_at", "applied_at", "rolled_back_at",
        "created_at", "updated_at",
    }
    missing = required_columns - _columns(connection, TABLE_NAME)
    if missing:
        raise sqlite3.OperationalError(
            f"{TABLE_NAME} is missing columns: "
            + ", ".join(sorted(missing))
        )
    _ensure_partial_unique_index(connection)


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    before = _objects(connection)
    _ensure_schema(connection)
    after = _objects(connection)
    if verbose:
        print("Marketplace canonical content proposals migration completed!")
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
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("DATABASE_PATH", "data/seller_platform.db")
    )
    migrate(path)
