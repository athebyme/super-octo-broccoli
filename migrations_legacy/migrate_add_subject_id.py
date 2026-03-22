#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция для добавления subject_id в products

Добавляет:
- subject_id в products - ID предмета для получения характеристик из WB API
"""

import sqlite3
import sys
from pathlib import Path


def migrate_add_subject_id(db_path: str):
    """Добавить поле subject_id в таблицу products"""

    print(f"🔄 Запуск миграции: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Проверяем существующие колонки в products
        cursor.execute("PRAGMA table_info(products)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        print(f"📋 Существующие колонки в products: {len(existing_columns)}")

        # Добавляем subject_id если его нет
        if 'subject_id' not in existing_columns:
            print("  ➕ Добавление колонки: subject_id")
            cursor.execute("ALTER TABLE products ADD COLUMN subject_id INTEGER")
            print("  ✓ Колонка subject_id добавлена")
        else:
            print("  ✓ Колонка subject_id уже существует")

        conn.commit()
        print("✅ Миграция успешно завершена!")
        return True

    except Exception as e:
        print(f"❌ Ошибка миграции: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    # По умолчанию используем путь из аргументов или стандартный
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/seller_platform.db"

    if not Path(db_path).exists():
        print(f"⚠️  База данных не найдена: {db_path}")
        print("   Создайте базу или укажите правильный путь")
        sys.exit(1)

    success = migrate_add_subject_id(db_path)
    sys.exit(0 if success else 1)
