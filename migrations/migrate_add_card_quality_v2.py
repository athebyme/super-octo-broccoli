#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Миграция: Качество карточек v2.

Добавляет в products метрики воронки продаж WB, причины attention и impact;
создаёт таблицу wb_subject_charcs_cache (кэш конфигов характеристик категорий).
Идемпотентна.

Запуск:
    python migrations/migrate_add_card_quality_v2.py [путь_к_БД]
"""
import os
import sqlite3
import sys

PRODUCT_COLUMNS = [
    ('wb_views_30d', 'INTEGER'),
    ('wb_orders_30d', 'INTEGER'),
    ('wb_cart_conv', 'FLOAT'),
    ('wb_order_conv', 'FLOAT'),
    ('wb_buyout_rate', 'FLOAT'),
    ('funnel_checked_at', 'DATETIME'),
    ('attention_reasons', 'TEXT'),
    ('quality_impact', 'FLOAT'),
]


def get_db_path():
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        return sys.argv[1]
    db_path = os.environ.get('DATABASE_PATH')
    if db_path:
        return db_path
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('sqlite:///'):
        return db_url.replace('sqlite:///', '')
    for cand in ('/app/data/seller_platform.db', 'data/seller_platform.db'):
        if os.path.exists(cand):
            return cand
    return 'data/seller_platform.db'


def migrate(db_path=None) -> bool:
    db_path = db_path or get_db_path()
    if not os.path.exists(db_path):
        print(f"❌ База не найдена: {db_path}")
        return False
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(products)")
        existing = {row[1] for row in cur.fetchall()}
        for name, ctype in PRODUCT_COLUMNS:
            if name not in existing:
                cur.execute(f"ALTER TABLE products ADD COLUMN {name} {ctype}")
                print(f"  ✅ products.{name}")
            else:
                print(f"  ⏭️  products.{name} уже есть")
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='wb_subject_charcs_cache'")
        if not cur.fetchone():
            cur.execute("""
                CREATE TABLE wb_subject_charcs_cache (
                    subject_id INTEGER PRIMARY KEY,
                    charcs_json TEXT,
                    fetched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("  ✅ wb_subject_charcs_cache")
        else:
            print("  ⏭️  wb_subject_charcs_cache уже есть")
        conn.commit()
        print("✅ card-quality v2: миграция завершена")
        return True
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(0 if migrate() else 1)
