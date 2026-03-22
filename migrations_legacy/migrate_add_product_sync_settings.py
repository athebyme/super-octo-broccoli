#!/usr/bin/env python3
"""
Миграция для добавления настроек автоматической синхронизации товаров

Добавляет таблицу:
- product_sync_settings: настройки автосинхронизации для продавца
"""

import os
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from seller_platform import app, db
from models import ProductSyncSettings


def migrate():
    """Выполнить миграцию"""
    with app.app_context():
        print("Начало миграции: добавление настроек автоматической синхронизации товаров")

        # Создаем таблицу
        try:
            # Проверяем существование таблицы
            inspector = db.inspect(db.engine)
            existing_tables = inspector.get_table_names()

            if 'product_sync_settings' not in existing_tables:
                print("Создание таблицы: product_sync_settings")

                # Создаем таблицу
                ProductSyncSettings.__table__.create(db.engine, checkfirst=True)

                print("✓ Таблица product_sync_settings успешно создана")
            else:
                print("✓ Таблица product_sync_settings уже существует")

            print("\nМиграция завершена успешно!")
            print("\nНовая таблица:")
            print("  - product_sync_settings: настройки автоматической синхронизации товаров")

            return True

        except Exception as e:
            print(f"✗ Ошибка при создании таблицы: {e}")
            import traceback
            traceback.print_exc()
            return False


if __name__ == '__main__':
    success = migrate()
    sys.exit(0 if success else 1)
