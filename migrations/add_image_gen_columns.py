#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: Добавление колонок для генерации изображений

Добавляет в таблицу auto_import_settings:
- image_gen_enabled: включена ли генерация картинок
- image_gen_provider: провайдер (openai_dalle, flux_pro, sdxl)
- openai_api_key: API ключ OpenAI
- replicate_api_key: API ключ Replicate
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

# Путь к базе данных
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'app.db')

# Альтернативный путь если instance нет
if not os.path.exists(DB_PATH):
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'app.db')


def get_existing_columns(cursor, table_name):
    """Получает список существующих колонок в таблице"""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def migrate():
    """Выполняет миграцию"""
    if not os.path.exists(DB_PATH):
        print(f"❌ База данных не найдена: {DB_PATH}")
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
            ("image_gen_provider", "VARCHAR(50) DEFAULT 'openai_dalle'"),
            ("openai_api_key", "VARCHAR(500)"),
            ("replicate_api_key", "VARCHAR(500)"),
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
