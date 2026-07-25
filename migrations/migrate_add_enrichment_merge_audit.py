#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Add the bounded smart-enrichment decision receipt to card history.

Idempotent and fail-fast: the ORM reads this column on every history query, so
an existing database must be migrated before the web process starts.
"""
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


def migrate(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        tables = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "card_edit_history" not in tables:
            logger.info("card_edit_history отсутствует — миграция не требуется")
            return True

        columns = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(card_edit_history)")
        }
        if "merge_decisions" not in columns:
            cursor.execute(
                "ALTER TABLE card_edit_history ADD COLUMN merge_decisions TEXT"
            )
            conn.commit()
            logger.info("Добавлена card_edit_history.merge_decisions")
        else:
            logger.info("card_edit_history.merge_decisions уже существует")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH
    if not target.exists():
        logger.error("База не найдена: %s", target)
        sys.exit(1)
    sys.exit(0 if migrate(target) else 1)

