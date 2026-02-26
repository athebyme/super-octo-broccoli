#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция для добавления характеристик и складов

Добавляет:
- characteristics_json в products - для хранения характеристик товара
- description в products - описание товара
- dimensions_json в products - габариты (длина, ширина, высота)
- таблицу product_stocks - остатки по складам
"""

import sqlite3
import sys
from pathlib import Path


def migrate_add_characteristics(db_path: str):
    """Добавить поля характеристик и таблицу остатков"""

    print(f"🔄 Запуск миграции: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Проверяем существующие колонки в products
        cursor.execute("PRAGMA table_info(products)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        print(f"📋 Существующие колонки в products: {len(existing_columns)}")

        # Добавляем новые колонки если их нет
        new_columns = {
            'characteristics_json': 'TEXT',
            'description': 'TEXT',
            'dimensions_json': 'TEXT',
        }

        for column_name, column_type in new_columns.items():
            if column_name not in existing_columns:
                print(f"  ➕ Добавление колонки: {column_name}")
                cursor.execute(f"ALTER TABLE products ADD COLUMN {column_name} {column_type}")
            else:
                print(f"  ✓ Колонка {column_name} уже существует")

        # Создаём таблицу product_stocks если не существует
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='product_stocks'
        """)

        if not cursor.fetchone():
            print("  ➕ Создание таблицы product_stocks")
            cursor.execute("""
                CREATE TABLE product_stocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    warehouse_id INTEGER,
                    warehouse_name VARCHAR(200),
                    quantity INTEGER DEFAULT 0,
                    quantity_full INTEGER DEFAULT 0,
                    in_way_to_client INTEGER DEFAULT 0,
                    in_way_from_client INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
                )
            """)

            # Создаём индексы
            cursor.execute("""
                CREATE INDEX idx_product_stocks_product_id
                ON product_stocks(product_id)
            """)
            cursor.execute("""
                CREATE INDEX idx_product_stocks_warehouse_id
                ON product_stocks(warehouse_id)
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX idx_product_stocks_unique
                ON product_stocks(product_id, warehouse_id)
            """)
            print("  ✓ Таблица product_stocks создана с индексами")
        else:
            print("  ✓ Таблица product_stocks уже существует")

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

    success = migrate_add_characteristics(db_path)
    sys.exit(0 if success else 1)
