#!/usr/bin/env python3
"""
Миграция для добавления полей синхронизации цен/остатков

Supplier:
- price_file_url, price_file_inf_url, price_file_delimiter, price_file_encoding
- last_price_sync_at, last_price_sync_status, last_price_sync_error, last_price_file_hash
- auto_sync_prices, auto_sync_interval_minutes

SupplierProduct:
- recommended_retail_price, supplier_status, additional_vendor_code
- last_price_sync_at, price_changed_at, previous_price

Usage:
  python migrations/migrate_add_price_stock_sync.py                         # auto-detect via Flask app
  python migrations/migrate_add_price_stock_sync.py /path/to/db.db          # explicit path
  python migrations/migrate_add_price_stock_sync.py --db-path /path/to/db   # explicit path
"""

import sys
import sqlite3
from pathlib import Path


def get_db_path():
    """Получить путь к БД из аргументов или через Flask"""
    # Аргумент командной строки
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--db-path' and i < len(sys.argv) - 1:
            return sys.argv[i + 1]
        if not arg.startswith('-') and arg.endswith('.db'):
            return arg

    # Через Flask app
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from seller_platform import app
        with app.app_context():
            db_url = app.config['SQLALCHEMY_DATABASE_URI']
            if db_url.startswith('sqlite:///'):
                return db_url.replace('sqlite:///', '')
    except Exception:
        pass

    # Стандартный путь
    default = Path(__file__).parent.parent / 'data' / 'seller_platform.db'
    if default.exists():
        return str(default)

    return None


def migrate(db_path: str = None):
    """Выполнить миграцию"""
    if not db_path:
        db_path = get_db_path()

    if not db_path:
        print("Не удалось определить путь к БД")
        return False

    if not Path(db_path).exists():
        print(f"База данных не найдена: {db_path}")
        return False

    print(f"Начало миграции: добавление полей синхронизации цен/остатков ({db_path})")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # --- suppliers ---
        cursor.execute("PRAGMA table_info(suppliers)")
        columns = {row[1] for row in cursor.fetchall()}

        supplier_new_columns = [
            ("price_file_url", "VARCHAR(500)"),
            ("price_file_inf_url", "VARCHAR(500)"),
            ("price_file_delimiter", "VARCHAR(5) DEFAULT ';'"),
            ("price_file_encoding", "VARCHAR(20) DEFAULT 'cp1251'"),
            ("last_price_sync_at", "DATETIME"),
            ("last_price_sync_status", "VARCHAR(50)"),
            ("last_price_sync_error", "TEXT"),
            ("last_price_file_hash", "VARCHAR(64)"),
            ("auto_sync_prices", "BOOLEAN DEFAULT 0"),
            ("auto_sync_interval_minutes", "INTEGER DEFAULT 60"),
        ]

        for col_name, col_type in supplier_new_columns:
            if col_name not in columns:
                print(f"  + suppliers.{col_name}")
                cursor.execute(f"ALTER TABLE suppliers ADD COLUMN {col_name} {col_type}")
            else:
                print(f"  . suppliers.{col_name} already exists")

        conn.commit()

        # --- supplier_products ---
        cursor.execute("PRAGMA table_info(supplier_products)")
        columns = {row[1] for row in cursor.fetchall()}

        product_new_columns = [
            ("recommended_retail_price", "FLOAT"),
            ("supplier_status", "VARCHAR(50)"),
            ("additional_vendor_code", "VARCHAR(200)"),
            ("last_price_sync_at", "DATETIME"),
            ("price_changed_at", "DATETIME"),
            ("previous_price", "FLOAT"),
        ]

        for col_name, col_type in product_new_columns:
            if col_name not in columns:
                print(f"  + supplier_products.{col_name}")
                cursor.execute(f"ALTER TABLE supplier_products ADD COLUMN {col_name} {col_type}")
            else:
                print(f"  . supplier_products.{col_name} already exists")

        conn.commit()

        print("\nМиграция завершена успешно!")
        return True

    except Exception as e:
        print(f"Ошибка при миграции: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()


if __name__ == '__main__':
    success = migrate()
    sys.exit(0 if success else 1)
