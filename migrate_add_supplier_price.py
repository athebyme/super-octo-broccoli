#!/usr/bin/env python3
"""
Миграция для добавления колонок supplier_price в таблицы products и imported_products

Добавляет колонки:
- products.supplier_price: закупочная цена поставщика
- products.supplier_price_updated_at: дата обновления цены
- imported_products.supplier_price: закупочная цена поставщика
- imported_products.calculated_price: рассчитанная цена
- imported_products.calculated_discount_price: цена с SPP скидкой
- imported_products.calculated_price_before_discount: цена до скидки WB
"""

import os
import sys
import sqlite3
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from seller_platform import app


def migrate():
    """Выполнить миграцию"""
    with app.app_context():
        print("Начало миграции: добавление колонок supplier_price")

        db_url = app.config['SQLALCHEMY_DATABASE_URI']
        if db_url.startswith('sqlite:///'):
            db_path = db_url.replace('sqlite:///', '')
        else:
            print(f"❌ Неподдерживаемый тип БД: {db_url}")
            return False

        if not Path(db_path).exists():
            print(f"❌ База данных не найдена: {db_path}")
            return False

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        try:
            # --- products ---
            cursor.execute("PRAGMA table_info(products)")
            columns = {row[1] for row in cursor.fetchall()}

            if 'supplier_price' not in columns:
                print("  ➕ Добавление колонки supplier_price в products...")
                cursor.execute("ALTER TABLE products ADD COLUMN supplier_price FLOAT")
                cursor.execute("ALTER TABLE products ADD COLUMN supplier_price_updated_at DATETIME")
                conn.commit()
                print("  ✅ Колонки supplier_price добавлены в products")
            else:
                print("  ✓ Колонка supplier_price уже существует в products")

            # --- imported_products ---
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='imported_products'")
            if cursor.fetchone():
                cursor.execute("PRAGMA table_info(imported_products)")
                ip_columns = {row[1] for row in cursor.fetchall()}

                if 'supplier_price' not in ip_columns:
                    print("  ➕ Добавление колонок ценообразования в imported_products...")
                    cursor.execute("ALTER TABLE imported_products ADD COLUMN supplier_price FLOAT")
                    cursor.execute("ALTER TABLE imported_products ADD COLUMN calculated_price FLOAT")
                    cursor.execute("ALTER TABLE imported_products ADD COLUMN calculated_discount_price FLOAT")
                    cursor.execute("ALTER TABLE imported_products ADD COLUMN calculated_price_before_discount FLOAT")
                    conn.commit()
                    print("  ✅ Колонки ценообразования добавлены в imported_products")
                else:
                    print("  ✓ Колонки ценообразования уже существуют в imported_products")
            else:
                print("  ⚠️ Таблица imported_products не найдена, пропускаем")

            print("\nМиграция завершена успешно!")
            return True

        except Exception as e:
            print(f"✗ Ошибка при миграции: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            conn.close()


if __name__ == '__main__':
    success = migrate()
    sys.exit(0 if success else 1)
