#!/usr/bin/env python3
"""Add admin-owned Ozon compliance default tables.

``OzonComplianceDefault`` stores one signed-off TN VED (HS) code decision per
Ozon product type, ``OzonMarkingRegistryVersion``/``OzonMarkingRule`` store a
versioned normative list of marking-required TN VED code prefixes.  This
migration is purely additive: no backfill is performed, so existing drafts
are left untouched rather than silently assigned a default.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "seller_platform.db"


CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS ozon_compliance_defaults (
        id INTEGER PRIMARY KEY,
        marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
        product_type_id INTEGER NOT NULL
            REFERENCES marketplace_product_types(id),
        tnved_code VARCHAR(20) NOT NULL,
        tnved_display VARCHAR(500),
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        decided_by_user_id INTEGER NOT NULL REFERENCES users(id),
        decided_at DATETIME NOT NULL,
        rationale TEXT NOT NULL,
        dictionary_version INTEGER,
        dictionary_hash VARCHAR(64),
        version INTEGER NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL,
        updated_at DATETIME,
        CHECK (status IN ('active', 'retired'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ozon_marking_registry_versions (
        id INTEGER PRIMARY KEY,
        label VARCHAR(200) NOT NULL,
        is_complete BOOLEAN NOT NULL DEFAULT 0,
        declared_by_user_id INTEGER NOT NULL REFERENCES users(id),
        declared_at DATETIME NOT NULL,
        rule_count INTEGER NOT NULL DEFAULT 0,
        checksum VARCHAR(64),
        status VARCHAR(20) NOT NULL DEFAULT 'superseded',
        created_at DATETIME NOT NULL,
        updated_at DATETIME,
        CHECK (status IN ('active', 'superseded'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ozon_marking_rules (
        id INTEGER PRIMARY KEY,
        registry_version_id INTEGER NOT NULL
            REFERENCES ozon_marking_registry_versions(id),
        code_prefix VARCHAR(20) NOT NULL,
        normative_ref VARCHAR(300),
        valid_from DATE,
        note TEXT,
        created_at DATETIME NOT NULL,
        UNIQUE (registry_version_id, code_prefix)
    )
    """,
)

INDEX_STATEMENTS = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_ozon_compliance_default_active
    ON ozon_compliance_defaults (marketplace_id, product_type_id)
    WHERE status = 'active'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_ozon_marking_registry_active
    ON ozon_marking_registry_versions (status)
    WHERE status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_ozon_marking_rules_prefix
    ON ozon_marking_rules (code_prefix)
    """,
)

MANAGED_TABLES = {
    "ozon_compliance_defaults",
    "ozon_marking_registry_versions",
    "ozon_marking_rules",
}


def apply_migration(db_path) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        baseline = {
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        }
        for statement in CREATE_STATEMENTS:
            connection.execute(statement)
        for statement in INDEX_STATEMENTS:
            connection.execute(statement)

        violations = {
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        }
        new_violations = violations - baseline
        relevant = [
            row for row in new_violations
            if str(row[0]) in MANAGED_TABLES or str(row[2]) in MANAGED_TABLES
        ]
        if relevant:
            connection.rollback()
            raise RuntimeError(
                f"Миграция создала нарушения внешних ключей: {relevant}"
            )
        connection.commit()
        logger.info("Compliance-таблицы Ozon готовы")
    finally:
        connection.close()


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    apply_migration(db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
