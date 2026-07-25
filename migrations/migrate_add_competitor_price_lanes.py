#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persist public WB price pairs for seller-owned comparison cards.

The seller prices API exposes the seller's discounted price, while the public
storefront may additionally apply a WB-funded discount.  The comparison read
model therefore needs a separate public ``basic``/``total-or-product`` pair.
This migration is additive, idempotent and performs no network calls.
"""

import logging
import sqlite3
import sys
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / 'data' / 'seller_platform.db'


def _columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f'PRAGMA table_info({table_name})'
        ).fetchall()
    }


def migrate(db_path: str | Path) -> bool:
    connection = sqlite3.connect(str(db_path))
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if 'products' not in tables:
            raise sqlite3.OperationalError(
                'competitor price lanes prerequisite missing: products',
            )

        columns = _columns(connection, 'products')
        added = []
        for name, declaration in (
            ('wb_public_base_price', 'NUMERIC(10, 2)'),
            ('wb_public_final_price', 'NUMERIC(10, 2)'),
            ('wb_public_price_synced_at', 'DATETIME'),
            (
                'wb_public_price_miss_count',
                'INTEGER NOT NULL DEFAULT 0',
            ),
        ):
            if name in columns:
                continue
            connection.execute(
                f'ALTER TABLE products ADD COLUMN {name} {declaration}'
            )
            columns.add(name)
            added.append(name)

        connection.execute(
            'UPDATE products SET wb_public_price_miss_count = 0 '
            'WHERE wb_public_price_miss_count IS NULL'
        )
        # On the first rollout, do not leave new storefront values empty for
        # up to a full configured interval. The existing singleton scheduler
        # will pick enabled sellers on its next minute tick.
        if added and 'competitor_monitor_settings' in tables:
            settings_columns = _columns(
                connection, 'competitor_monitor_settings',
            )
            if {'is_enabled', 'next_sync_due_at'} <= settings_columns:
                connection.execute(
                    'UPDATE competitor_monitor_settings '
                    'SET next_sync_due_at = CURRENT_TIMESTAMP '
                    'WHERE is_enabled = 1'
                )

        connection.commit()
        logger.info(
            'competitor price lanes: added %s',
            added or 'nothing (already applied)',
        )
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    db_path = (
        Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH
    )
    if not db_path.exists():
        logger.error('DB not found: %s', db_path)
        return 1
    return 0 if migrate(db_path) else 1


if __name__ == '__main__':
    sys.exit(main())
