#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: мониторинг конкурентов v2 (scheduler вместо тредов).

Добавляет:
- competitor_monitor_settings: sync_interval_minutes (30..1440, default 60),
  next_sync_due_at (NULL = due сейчас), discount_alert_pp;
- competitor_products: metadata_synced_at, is_adult, price_miss_count,
  last_price_at (backfill из last_fetched_at, где цена наблюдалась);
- competitor_groups: import_requested (заявка на импорт каталога продавца).

Удаляет мёртвые v1-колонки pause_between_cycles_seconds и
requests_per_minute (только при SQLite >= 3.35 с поддержкой DROP COLUMN;
на старом SQLite колонки остаются и просто игнорируются ORM).
is_running сохраняется: v2 показывает через него живой статус синка.
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
        tables = {row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        added = []

        if 'competitor_monitor_settings' in tables:
            cols = {r[1] for r in cur.execute(
                'PRAGMA table_info(competitor_monitor_settings)')}
            if 'sync_interval_minutes' not in cols:
                cur.execute("ALTER TABLE competitor_monitor_settings "
                            "ADD COLUMN sync_interval_minutes INTEGER DEFAULT 60")
                cur.execute("UPDATE competitor_monitor_settings "
                            "SET sync_interval_minutes = 60 "
                            "WHERE sync_interval_minutes IS NULL")
                added.append('settings.sync_interval_minutes')
            if 'next_sync_due_at' not in cols:
                cur.execute("ALTER TABLE competitor_monitor_settings "
                            "ADD COLUMN next_sync_due_at DATETIME")
                added.append('settings.next_sync_due_at')
            if 'discount_alert_pp' not in cols:
                cur.execute("ALTER TABLE competitor_monitor_settings "
                            "ADD COLUMN discount_alert_pp FLOAT DEFAULT 5.0")
                cur.execute("UPDATE competitor_monitor_settings "
                            "SET discount_alert_pp = 5.0 "
                            "WHERE discount_alert_pp IS NULL")
                added.append('settings.discount_alert_pp')

        if 'competitor_monitor_settings' in tables:
            supports_drop = tuple(
                int(x) for x in sqlite3.sqlite_version.split('.')[:2]
            ) >= (3, 35)
            if supports_drop:
                cols = {r[1] for r in cur.execute(
                    'PRAGMA table_info(competitor_monitor_settings)')}
                for dead in ('pause_between_cycles_seconds',
                             'requests_per_minute'):
                    if dead in cols:
                        cur.execute('ALTER TABLE competitor_monitor_settings '
                                    f'DROP COLUMN {dead}')
                        added.append(f'settings.-{dead}')

        if 'competitor_products' in tables:
            cols = {r[1] for r in cur.execute(
                'PRAGMA table_info(competitor_products)')}
            if 'metadata_synced_at' not in cols:
                cur.execute("ALTER TABLE competitor_products "
                            "ADD COLUMN metadata_synced_at DATETIME")
                added.append('products.metadata_synced_at')
            if 'is_adult' not in cols:
                cur.execute("ALTER TABLE competitor_products "
                            "ADD COLUMN is_adult BOOLEAN")
                added.append('products.is_adult')
            if 'price_miss_count' not in cols:
                cur.execute("ALTER TABLE competitor_products "
                            "ADD COLUMN price_miss_count INTEGER DEFAULT 0")
                cur.execute("UPDATE competitor_products "
                            "SET price_miss_count = 0 "
                            "WHERE price_miss_count IS NULL")
                added.append('products.price_miss_count')
            if 'last_price_at' not in cols:
                cur.execute("ALTER TABLE competitor_products "
                            "ADD COLUMN last_price_at DATETIME")
                cur.execute("UPDATE competitor_products "
                            "SET last_price_at = last_fetched_at "
                            "WHERE current_sale_price IS NOT NULL "
                            "   OR current_price IS NOT NULL")
                added.append('products.last_price_at')

        if 'competitor_groups' in tables:
            cols = {r[1] for r in cur.execute(
                'PRAGMA table_info(competitor_groups)')}
            if 'import_requested' not in cols:
                cur.execute("ALTER TABLE competitor_groups "
                            "ADD COLUMN import_requested BOOLEAN DEFAULT 0")
                cur.execute("UPDATE competitor_groups "
                            "SET import_requested = 0 "
                            "WHERE import_requested IS NULL")
                added.append('groups.import_requested')

        conn.commit()
        logger.info('competitor v2: добавлено %s', added or 'ничего (уже применено)')
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_DB_PATH)
    if not Path(db_path).exists():
        logger.error('БД не найдена: %s', db_path)
        return 1
    return 0 if migrate(db_path) else 1


if __name__ == '__main__':
    sys.exit(main())
