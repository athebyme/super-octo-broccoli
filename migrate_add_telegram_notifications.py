#!/usr/bin/env python3
"""
Миграция для добавления функционала Telegram уведомлений

Добавляет таблицы:
- telegram_settings: настройки Telegram уведомлений для продавца
- telegram_notification_log: лог отправленных уведомлений
"""

import os
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from seller_platform import app, db
from models import TelegramSettings, TelegramNotificationLog


def migrate():
    """Выполнить миграцию"""
    with app.app_context():
        print("Начало миграции: добавление функционала Telegram уведомлений")

        # Создаем таблицы
        try:
            # Проверяем существование таблиц
            inspector = db.inspect(db.engine)
            existing_tables = inspector.get_table_names()

            tables_to_create = []
            if 'telegram_settings' not in existing_tables:
                tables_to_create.append('telegram_settings')
            if 'telegram_notification_log' not in existing_tables:
                tables_to_create.append('telegram_notification_log')

            if tables_to_create:
                print(f"Создание таблиц: {', '.join(tables_to_create)}")

                # Создаем только новые таблицы
                TelegramSettings.__table__.create(db.engine, checkfirst=True)
                TelegramNotificationLog.__table__.create(db.engine, checkfirst=True)

                print("✓ Таблицы успешно созданы")
            else:
                print("✓ Все таблицы уже существуют")

            print("\nМиграция завершена успешно!")
            print("\nНовые таблицы:")
            print("  - telegram_settings: настройки Telegram уведомлений для продавца")
            print("  - telegram_notification_log: лог отправленных уведомлений")
            print("\nНовые возможности:")
            print("  ✓ Настройка Telegram бота для уведомлений")
            print("  ✓ Уведомления о низких остатках")
            print("  ✓ Уведомления об изменениях цен")
            print("  ✓ Уведомления об ошибках синхронизации")
            print("  ✓ Уведомления о завершении импорта и массовых операций")
            print("  ✓ Ежедневная сводка по товарам")

            return True

        except Exception as e:
            print(f"✗ Ошибка при создании таблиц: {e}")
            import traceback
            traceback.print_exc()
            return False


if __name__ == '__main__':
    success = migrate()
    sys.exit(0 if success else 1)
