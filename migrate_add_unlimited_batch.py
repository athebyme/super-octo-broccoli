#!/usr/bin/env python3
"""
Миграция для добавления поля allow_unlimited_batch в настройки безопасности цен.

Запуск:
    python migrate_add_unlimited_batch.py
"""

import sqlite3
import os

def get_db_path():
    """Получить путь к базе данных"""
    return os.environ.get('DATABASE_PATH', 'data/seller_platform.db')

def migrate():
    """Добавить колонку allow_unlimited_batch"""
    db_path = get_db_path()

    if not os.path.exists(db_path):
        print(f"База данных не найдена: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Проверяем, существует ли уже колонка
        cursor.execute("PRAGMA table_info(safe_price_change_settings)")
        columns = [col[1] for col in cursor.fetchall()]

        if 'allow_unlimited_batch' in columns:
            print("Колонка allow_unlimited_batch уже существует")
        else:
            # Добавляем колонку
            cursor.execute("""
                ALTER TABLE safe_price_change_settings
                ADD COLUMN allow_unlimited_batch BOOLEAN DEFAULT 1 NOT NULL
            """)
            print("Добавлена колонка allow_unlimited_batch")

        # Также увеличим дефолтный лимит для существующих записей
        cursor.execute("""
            UPDATE safe_price_change_settings
            SET max_products_per_batch = 1000
            WHERE max_products_per_batch < 1000
        """)
        updated = cursor.rowcount
        if updated > 0:
            print(f"Обновлено {updated} записей: max_products_per_batch увеличен до 1000")

        conn.commit()
        print("Миграция успешно выполнена!")
        return True

    except Exception as e:
        print(f"Ошибка миграции: {e}")
        conn.rollback()
        return False

    finally:
        conn.close()

if __name__ == '__main__':
    migrate()
