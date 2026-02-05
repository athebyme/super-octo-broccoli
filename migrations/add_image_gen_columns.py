#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: Добавление колонок для генерации изображений

Добавляет в таблицу auto_import_settings:
- image_gen_enabled: включена ли генерация картинок
- image_gen_provider: провайдер (together_flux рекомендуется!)
- openai_api_key: API ключ OpenAI
- replicate_api_key: API ключ Replicate
- together_api_key: API ключ Together AI (рекомендуется - $5 бесплатно!)
- image_gen_width: ширина изображения
- image_gen_height: высота изображения
- openai_image_quality: качество DALL-E (standard/hd)
- openai_image_style: стиль DALL-E (vivid/natural)

Запуск:
    python migrations/add_image_gen_columns.py
"""

import sqlite3
import os
import sys

def find_database():
    """Находит базу данных в разных возможных путях"""
    # Возможные пути к базе данных
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)

    possible_paths = [
        # Docker пути - seller_platform.db (data первый - там основная база!)
        '/app/data/seller_platform.db',
        '/app/seller_platform.db',
        '/app/instance/seller_platform.db',
        # Docker пути - app.db
        '/app/instance/app.db',
        '/app/app.db',
        '/app/data/app.db',
        # Локальные пути относительно скрипта
        os.path.join(parent_dir, 'seller_platform.db'),
        os.path.join(parent_dir, 'data', 'seller_platform.db'),
        os.path.join(parent_dir, 'instance', 'seller_platform.db'),
        os.path.join(parent_dir, 'instance', 'app.db'),
        os.path.join(parent_dir, 'app.db'),
        os.path.join(parent_dir, 'data', 'app.db'),
        # Текущая директория
        'seller_platform.db',
        'data/seller_platform.db',
        'instance/app.db',
        'app.db',
        'data/app.db',
    ]

    for path in possible_paths:
        if os.path.exists(path):
            return path

    return None


def get_existing_columns(cursor, table_name):
    """Получает список существующих колонок в таблице"""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def migrate():
    """Выполняет миграцию"""
    DB_PATH = find_database()

    if not DB_PATH:
        print("❌ База данных не найдена!")
        print("   Проверьте, что файл app.db существует в одном из путей:")
        print("   - /app/instance/app.db (Docker)")
        print("   - ./instance/app.db")
        print("   - ./app.db")
        sys.exit(1)

    print(f"📂 База данных: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Проверяем существующие колонки
        existing = get_existing_columns(cursor, 'auto_import_settings')
        print(f"📋 Существующие колонки: {len(existing)}")

        # Колонки для добавления
        new_columns = [
            ("image_gen_enabled", "BOOLEAN DEFAULT 0 NOT NULL"),
            ("image_gen_provider", "VARCHAR(50) DEFAULT 'together_flux'"),
            ("openai_api_key", "VARCHAR(500)"),
            ("replicate_api_key", "VARCHAR(500)"),
            ("together_api_key", "VARCHAR(500)"),  # Together AI - рекомендуется!
            ("image_gen_width", "INTEGER DEFAULT 1440"),
            ("image_gen_height", "INTEGER DEFAULT 810"),
            ("openai_image_quality", "VARCHAR(20) DEFAULT 'standard'"),
            ("openai_image_style", "VARCHAR(20) DEFAULT 'vivid'"),
        ]

        added = 0
        for col_name, col_type in new_columns:
            if col_name not in existing:
                try:
                    sql = f"ALTER TABLE auto_import_settings ADD COLUMN {col_name} {col_type}"
                    cursor.execute(sql)
                    print(f"✅ Добавлена колонка: {col_name}")
                    added += 1
                except sqlite3.OperationalError as e:
                    if "duplicate column" in str(e).lower():
                        print(f"⏭️ Колонка уже существует: {col_name}")
                    else:
                        raise
            else:
                print(f"⏭️ Колонка уже существует: {col_name}")

        conn.commit()
        print(f"\n✅ Миграция завершена. Добавлено колонок: {added}")

    except Exception as e:
        conn.rollback()
        print(f"❌ Ошибка миграции: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == '__main__':
    migrate()
