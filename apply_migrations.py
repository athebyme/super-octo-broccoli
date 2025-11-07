#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автоматическое применение всех миграций к базе данных
"""

import os
import sys
import sqlite3
from pathlib import Path


def get_db_path():
    """Получить путь к базе данных из переменной окружения или по умолчанию"""
    # Проверяем переменную окружения DATABASE_URL
    db_url = os.environ.get('DATABASE_URL', '')

    if db_url.startswith('sqlite:///'):
        # Извлекаем путь из sqlite URL
        db_path = db_url.replace('sqlite:///', '')
        return db_path

    # По умолчанию
    return 'data/seller_platform.db'


def check_column_exists(db_path: str, table: str, column: str) -> bool:
    """Проверить существует ли колонка в таблице"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cursor.fetchall()}

    conn.close()
    return column in columns


def apply_migration_subject_id(db_path: str) -> bool:
    """Применить миграцию для добавления subject_id"""
    print("\n🔍 Проверка миграции: subject_id")

    if check_column_exists(db_path, 'products', 'subject_id'):
        print("  ✓ Колонка subject_id уже существует")
        return True

    print("  ➕ Добавление колонки subject_id...")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("ALTER TABLE products ADD COLUMN subject_id INTEGER")
        conn.commit()
        print("  ✅ Колонка subject_id добавлена")
        return True
    except Exception as e:
        print(f"  ❌ Ошибка: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def main():
    """Главная функция"""
    print("╔══════════════════════════════════════╗")
    print("║  Применение миграций к БД            ║")
    print("╚══════════════════════════════════════╝")

    db_path = get_db_path()
    print(f"\n📂 Путь к БД: {db_path}")

    if not Path(db_path).exists():
        print(f"\n⚠️  База данных не найдена: {db_path}")
        print("   Создайте базу используя init_platform.py или Flask CLI")
        sys.exit(1)

    print("\n🚀 Начинаем применение миграций...")

    success = True

    # Применяем миграцию subject_id
    if not apply_migration_subject_id(db_path):
        success = False

    print("\n" + "="*50)
    if success:
        print("✅ Все миграции успешно применены!")
    else:
        print("❌ Некоторые миграции не применены")
        sys.exit(1)

    print("="*50)


if __name__ == "__main__":
    main()
