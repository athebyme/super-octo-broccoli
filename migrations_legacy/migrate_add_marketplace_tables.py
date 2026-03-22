#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: добавление таблиц и полей для интеграции маркетплейсов.

Безопасна для повторного запуска (идемпотентна).
Работает с SQLite: каждый ALTER TABLE в отдельном try/except,
потому что SQLite не поддерживает IF NOT EXISTS для колонок.
"""
import os
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import db
from seller_platform import app


def column_exists(table_name: str, column_name: str) -> bool:
    """Проверяет существование колонки в таблице (SQLite-совместимо)."""
    try:
        result = db.session.execute(db.text(f"PRAGMA table_info({table_name})"))
        columns = [row[1] for row in result]
        return column_name in columns
    except Exception:
        return False


def add_column_safe(table_name: str, column_name: str, column_type: str):
    """Добавляет колонку, если она не существует."""
    if column_exists(table_name, column_name):
        print(f"  -- {table_name}.{column_name} уже существует")
        return False

    try:
        db.session.execute(db.text(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
        ))
        print(f"  ++ {table_name}.{column_name} добавлена ({column_type})")
        return True
    except Exception as e:
        print(f"  !! {table_name}.{column_name} ошибка: {e}")
        return False


def run_migration():
    """Применяет миграцию"""
    print("=" * 60)
    print("Миграция: таблицы и поля для интеграции маркетплейсов")
    print("=" * 60)

    with app.app_context():
        # 1. Создаем новые таблицы (db.create_all безопасен — не трогает существующие)
        print("\n[1/2] Создание таблиц...")
        db.create_all()

        tables_to_check = [
            'marketplaces',
            'marketplace_categories',
            'marketplace_category_characteristics',
            'marketplace_directories',
            'marketplace_connections',
            'marketplace_sync_jobs',
        ]
        for t in tables_to_check:
            try:
                db.session.execute(db.text(f"SELECT 1 FROM {t} LIMIT 1"))
                print(f"  OK {t}")
            except Exception:
                print(f"  !! {t} НЕ СОЗДАНА")

        # 2. Добавляем колонки в supplier_products
        print("\n[2/2] Колонки в supplier_products...")
        add_column_safe('supplier_products', 'marketplace_fields_json', 'TEXT')
        add_column_safe('supplier_products', 'marketplace_validation_status', 'VARCHAR(50)')
        add_column_safe('supplier_products', 'marketplace_fill_pct', 'FLOAT')

        db.session.commit()
        print("\n" + "=" * 60)
        print("Миграция завершена успешно!")
        print("=" * 60)


if __name__ == '__main__':
    run_migration()
