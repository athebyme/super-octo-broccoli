#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: колонки WB-ревизии на products.

Добавляет `products.wb_audit_json` (bounded JSON последней live-сверки с WB:
существование карточки, фото, расхождения характеристик) и
`products.wb_audited_at`. Данные наполняет только runtime-сервис
`services/wb_card_audit.py`; backfill не требуется.

Идемпотентная — безопасно запускать повторно.
"""
import logging
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / 'data' / 'seller_platform.db'


def migrate(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        tables = {
            row[0]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if 'products' not in tables:
            logger.info('products отсутствует — миграция не требуется')
            return True

        columns = {
            row[1] for row in cur.execute('PRAGMA table_info(products)')
        }
        added = []
        if 'wb_audit_json' not in columns:
            cur.execute('ALTER TABLE products ADD COLUMN wb_audit_json TEXT')
            added.append('wb_audit_json')
        if 'wb_audited_at' not in columns:
            cur.execute('ALTER TABLE products ADD COLUMN wb_audited_at DATETIME')
            added.append('wb_audited_at')
        conn.commit()
        logger.info('WB card audit: добавлены колонки %s',
                    added or 'нет (уже существуют)')
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    if not Path(target).exists():
        logger.error('База не найдена: %s', target)
        sys.exit(1)
    ok = migrate(target)
    sys.exit(0 if ok else 1)
