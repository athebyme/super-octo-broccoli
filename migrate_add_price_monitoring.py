#!/usr/bin/env python3
"""
Миграция для добавления функционала мониторинга цен

Добавляет таблицы:
- price_monitor_settings: настройки мониторинга для продавца
- price_history: история изменений цен и остатков
- suspicious_price_changes: подозрительные изменения цен
"""

import os
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from seller_platform import app, db
from models import PriceMonitorSettings, PriceHistory, SuspiciousPriceChange


def migrate():
    """Выполнить миграцию"""
    with app.app_context():
        print("Начало миграции: добавление функционала мониторинга цен")

        # Создаем таблицы
        try:
            # Проверяем существование таблиц
            inspector = db.inspect(db.engine)
            existing_tables = inspector.get_table_names()

            tables_to_create = []
            if 'price_monitor_settings' not in existing_tables:
                tables_to_create.append('price_monitor_settings')
            if 'price_history' not in existing_tables:
                tables_to_create.append('price_history')
            if 'suspicious_price_changes' not in existing_tables:
                tables_to_create.append('suspicious_price_changes')

            if tables_to_create:
                print(f"Создание таблиц: {', '.join(tables_to_create)}")

                # Создаем только новые таблицы
                PriceMonitorSettings.__table__.create(db.engine, checkfirst=True)
                PriceHistory.__table__.create(db.engine, checkfirst=True)
                SuspiciousPriceChange.__table__.create(db.engine, checkfirst=True)

                print("✓ Таблицы успешно созданы")
            else:
                print("✓ Все таблицы уже существуют")

            print("\nМиграция завершена успешно!")
            print("\nНовые таблицы:")
            print("  - price_monitor_settings: настройки мониторинга для продавца")
            print("  - price_history: история изменений цен и остатков")
            print("  - suspicious_price_changes: подозрительные изменения цен")

            return True

        except Exception as e:
            print(f"✗ Ошибка при создании таблиц: {e}")
            import traceback
            traceback.print_exc()
            return False


if __name__ == '__main__':
    success = migrate()
    sys.exit(0 if success else 1)
