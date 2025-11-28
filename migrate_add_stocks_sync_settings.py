#!/usr/bin/env python3
"""
Миграция: добавление отдельных настроек синхронизации остатков

Добавляет поля для независимой синхронизации остатков:
- stocks_sync_interval_minutes
- last_stocks_sync_at
- next_stocks_sync_at
- last_stocks_sync_status
- last_stocks_sync_error
- last_stocks_sync_duration
- stocks_synced
"""

from models import db
from seller_platform import app

def migrate():
    """Добавить поля для синхронизации остатков"""
    with app.app_context():
        print("🔧 Migrating product_sync_settings table...")

        # Сначала создаем все таблицы если они не существуют
        print("   📋 Creating tables if they don't exist...")
        db.create_all()
        print("   ✅ Tables created/verified")

        # Для SQLite используем прямой SQL для добавления колонок
        connection = db.engine.raw_connection()
        cursor = connection.cursor()

        # Проверяем существует ли таблица
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='product_sync_settings'")
        if not cursor.fetchone():
            print("   ⚠️  Table product_sync_settings doesn't exist, creating it...")
            connection.commit()
            cursor.close()
            connection.close()
            db.create_all()
            print("   ✅ Table created via db.create_all()")
            print("\n✅ Migration completed successfully (table was created with all fields)")
            return

        # Список колонок для добавления
        columns_to_add = [
            ("stocks_sync_interval_minutes", "INTEGER DEFAULT 30 NOT NULL"),
            ("last_stocks_sync_at", "DATETIME"),
            ("next_stocks_sync_at", "DATETIME"),
            ("last_stocks_sync_status", "VARCHAR(50)"),
            ("last_stocks_sync_error", "TEXT"),
            ("last_stocks_sync_duration", "FLOAT"),
            ("stocks_synced", "INTEGER DEFAULT 0"),
        ]

        print(f"   🔄 Adding {len(columns_to_add)} new columns...")
        for column_name, column_type in columns_to_add:
            try:
                cursor.execute(f"ALTER TABLE product_sync_settings ADD COLUMN {column_name} {column_type}")
                print(f"      ✅ Added column: {column_name}")
            except Exception as e:
                if "duplicate column name" in str(e).lower():
                    print(f"      ⏭️  Column {column_name} already exists, skipping")
                else:
                    print(f"      ❌ Error adding {column_name}: {e}")

        connection.commit()
        cursor.close()
        connection.close()

        print("\n✅ Migration completed successfully!")

if __name__ == '__main__':
    migrate()
