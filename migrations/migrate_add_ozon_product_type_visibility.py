#!/usr/bin/env python3
"""Separate seller Ozon-type visibility from proactive schema refresh.

The original ``is_enabled`` flag controls the admin-owned refresh-ahead set.
Using it as seller visibility leaves a fresh installation with no selectable
Ozon types, while enabling every official type would schedule thousands of
unneeded schema calls.  This additive migration makes all official types
seller-selectable by default; runtime demand still decides which schemas are
fetched.
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


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def _foreign_key_violations(
    connection: sqlite3.Connection,
) -> set[tuple]:
    # The migration is additive and touches only this 9k-row table.  A global
    # ``PRAGMA foreign_key_check`` would scan the multi-gigabyte production DB
    # twice during startup without adding safety for this column/index change.
    return {
        tuple(row)
        for row in connection.execute(
            "PRAGMA foreign_key_check(marketplace_product_types)"
        ).fetchall()
    }


def apply_migration(
    connection: sqlite3.Connection,
    *,
    verbose: bool = True,
) -> int:
    """Apply the additive migration and return the number of added columns."""
    if "marketplace_product_types" not in _tables(connection):
        raise sqlite3.OperationalError(
            "Ozon product type visibility prerequisite missing: "
            "marketplace_product_types"
        )

    baseline_violations = _foreign_key_violations(connection)
    columns = _columns(connection, "marketplace_product_types")
    added = 0
    if "is_seller_selectable" not in columns:
        connection.execute(
            "ALTER TABLE marketplace_product_types "
            "ADD COLUMN is_seller_selectable "
            "BOOLEAN NOT NULL DEFAULT 1"
        )
        added = 1

    connection.execute(
        "UPDATE marketplace_product_types "
        "SET is_seller_selectable = 1 "
        "WHERE is_seller_selectable IS NULL"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_marketplace_product_type_selectable "
        "ON marketplace_product_types("
        "marketplace_id, is_seller_selectable, is_available)"
    )

    introduced = _foreign_key_violations(connection) - baseline_violations
    if introduced:
        raise sqlite3.IntegrityError(
            "Ozon product type visibility migration introduced foreign-key "
            f"violations: {sorted(introduced)!r}"
        )
    if verbose:
        print(
            "Ozon product type visibility migration complete; "
            f"added={added}"
        )
    return added


def migrate(db_path: str | Path) -> bool:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        apply_migration(connection)
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH
    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        return 1
    return 0 if migrate(db_path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
