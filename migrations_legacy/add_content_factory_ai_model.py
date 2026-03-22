#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: добавление поля ai_model в content_factories

Позволяет выбирать конкретную модель AI для каждой фабрики контента.

Запуск:
    python migrations/add_content_factory_ai_model.py
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
        os.path.join(parent_dir, 'data', 'seller_platform.db'),
        os.path.join(parent_dir, 'seller_platform.db'),
        os.path.join(parent_dir, 'instance', 'seller_platform.db'),
    ]

    db_url = os.environ.get('DATABASE_URL', '')
    if 'sqlite' in db_url:
        path = db_url.replace('sqlite:////', '/').replace('sqlite:///', '')
        possible_paths.insert(0, path)

    for path in possible_paths:
        if os.path.exists(path):
            return path
    return None


def migrate():
    db_path = find_database()
    if not db_path:
        print("Database not found!")
        sys.exit(1)

    print(f"Using database: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if column already exists
    cursor.execute("PRAGMA table_info(content_factories)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'ai_model' in columns:
        print("Column ai_model already exists in content_factories, skipping")
    else:
        cursor.execute("ALTER TABLE content_factories ADD COLUMN ai_model VARCHAR(100)")
        print("Added ai_model column to content_factories")

    conn.commit()
    conn.close()
    print("Migration complete!")


if __name__ == '__main__':
    migrate()
