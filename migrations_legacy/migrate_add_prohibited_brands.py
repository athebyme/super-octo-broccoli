#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: создание таблицы prohibited_brands для запрещённых брендов по маркетплейсам.

Запуск:
    python migrations/migrate_add_prohibited_brands.py
"""

import sqlite3
import os
import sys


def find_database():
    """Находит базу данных"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)

    possible_paths = [
        '/app/data/seller_platform.db',
        '/app/seller_platform.db',
        '/app/instance/seller_platform.db',
        '/app/instance/app.db',
        os.path.join(parent_dir, 'seller_platform.db'),
        os.path.join(parent_dir, 'data', 'seller_platform.db'),
        os.path.join(parent_dir, 'instance', 'seller_platform.db'),
        os.path.join(parent_dir, 'instance', 'app.db'),
        os.path.join(parent_dir, 'app.db'),
        os.path.join(parent_dir, 'data', 'app.db'),
        'seller_platform.db',
        'data/seller_platform.db',
        'instance/app.db',
        'app.db',
    ]

    for path in possible_paths:
        if os.path.exists(path):
            return path

    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('sqlite:///'):
        db_path = db_url.replace('sqlite:///', '')
        if os.path.exists(db_path):
            return db_path

    return None


def migrate(db_path):
    """Создаёт таблицу prohibited_brands"""
    print(f"\n{'='*60}")
    print(f"Миграция: prohibited_brands")
    print(f"БД: {db_path}")
    print(f"{'='*60}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Проверяем, существует ли таблица
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prohibited_brands'")
        if cursor.fetchone():
            print("  Таблица prohibited_brands уже существует")
            return True

        cursor.execute("""
            CREATE TABLE prohibited_brands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_name VARCHAR(200) NOT NULL,
                brand_name_normalized VARCHAR(200) NOT NULL,
                marketplace VARCHAR(50) NOT NULL,
                reason TEXT,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_prohibited_brand_mp UNIQUE (brand_name_normalized, marketplace)
            )
        """)
        print("  Таблица prohibited_brands создана")

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_prohibited_brand_normalized
            ON prohibited_brands (brand_name_normalized)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_prohibited_brand_active
            ON prohibited_brands (marketplace, is_active)
        """)
        print("  Индексы созданы")

        conn.commit()
        print("  Миграция завершена успешно")
        return True

    except Exception as e:
        conn.rollback()
        print(f"  Ошибка миграции: {e}")
        return False
    finally:
        conn.close()


def main():
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_path = find_database()

    if not db_path:
        print("БД не найдена")
        sys.exit(1)

    success = migrate(db_path)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
