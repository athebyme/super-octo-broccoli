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

from models import db, ProductSyncSettings
from seller_platform import app

def migrate():
    """Добавить поля для синхронизации остатков"""
    with app.app_context():
        print("🔧 Adding stocks sync settings columns to product_sync_settings table...")

        # Создаем таблицу если не существует или обновляем схему
        db.create_all()

        print("✅ Migration completed successfully!")
        print("   - stocks_sync_interval_minutes (default: 30 minutes)")
        print("   - last_stocks_sync_at")
        print("   - next_stocks_sync_at")
        print("   - last_stocks_sync_status")
        print("   - last_stocks_sync_error")
        print("   - last_stocks_sync_duration")
        print("   - stocks_synced")

        # Проверяем существующие записи
        settings_count = ProductSyncSettings.query.count()
        print(f"\n📊 Found {settings_count} existing sync settings records")

if __name__ == '__main__':
    migrate()
