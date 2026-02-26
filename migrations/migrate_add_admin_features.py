#!/usr/bin/env python3
"""
Миграция для добавления функций администрирования

Добавляет:
- Таблицу user_activity для логирования активности пользователей
- Таблицу admin_audit_log для логирования действий администраторов
- Таблицу system_settings для глобальных настроек
- Поля blocked_at, blocked_by_user_id, notes в таблицу users
"""

import os
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from seller_platform import app, db
from models import UserActivity, AdminAuditLog, SystemSettings


def migrate():
    """Выполнить миграцию"""
    with app.app_context():
        print("Начало миграции: добавление функций администрирования")

        try:
            # Проверяем существование таблиц
            inspector = db.inspect(db.engine)
            existing_tables = inspector.get_table_names()

            tables_to_create = []
            if 'user_activity' not in existing_tables:
                tables_to_create.append('user_activity')
            if 'admin_audit_log' not in existing_tables:
                tables_to_create.append('admin_audit_log')
            if 'system_settings' not in existing_tables:
                tables_to_create.append('system_settings')

            if tables_to_create:
                print(f"Создание таблиц: {', '.join(tables_to_create)}")

                # Создаем новые таблицы
                UserActivity.__table__.create(db.engine, checkfirst=True)
                AdminAuditLog.__table__.create(db.engine, checkfirst=True)
                SystemSettings.__table__.create(db.engine, checkfirst=True)

                print("✓ Новые таблицы созданы")
            else:
                print("✓ Все таблицы уже существуют")

            # Добавляем колонки в таблицу users
            print("\nДобавление колонок в таблицу users...")
            users_columns = {col['name'] for col in inspector.get_columns('users')}

            columns_added = []
            if 'blocked_at' not in users_columns:
                db.session.execute(db.text('ALTER TABLE users ADD COLUMN blocked_at DATETIME'))
                columns_added.append('blocked_at')

            if 'blocked_by_user_id' not in users_columns:
                db.session.execute(db.text('ALTER TABLE users ADD COLUMN blocked_by_user_id INTEGER'))
                db.session.execute(db.text('CREATE INDEX idx_users_blocked_by ON users(blocked_by_user_id)'))
                columns_added.append('blocked_by_user_id')

            if 'notes' not in users_columns:
                db.session.execute(db.text('ALTER TABLE users ADD COLUMN notes TEXT'))
                columns_added.append('notes')

            if columns_added:
                db.session.commit()
                print(f"✓ Добавлены колонки: {', '.join(columns_added)}")
            else:
                print("✓ Все колонки уже существуют")

            # Добавляем дефолтные системные настройки
            print("\nДобавление дефолтных системных настроек...")
            default_settings = [
                {
                    'key': 'global_sync_enabled',
                    'value': 'true',
                    'value_type': 'bool',
                    'description': 'Разрешить автоматическую синхронизацию для всех продавцов'
                },
                {
                    'key': 'max_sync_interval_minutes',
                    'value': '1440',
                    'value_type': 'int',
                    'description': 'Максимальный интервал автосинхронизации (минуты)'
                },
                {
                    'key': 'min_sync_interval_minutes',
                    'value': '5',
                    'value_type': 'int',
                    'description': 'Минимальный интервал автосинхронизации (минуты)'
                },
                {
                    'key': 'api_rate_limit_per_minute',
                    'value': '100',
                    'value_type': 'int',
                    'description': 'Лимит API запросов в минуту на продавца'
                },
                {
                    'key': 'cleanup_logs_days',
                    'value': '90',
                    'value_type': 'int',
                    'description': 'Хранить логи активности (дни)'
                }
            ]

            settings_added = 0
            for setting_data in default_settings:
                existing = SystemSettings.query.filter_by(key=setting_data['key']).first()
                if not existing:
                    setting = SystemSettings(**setting_data)
                    db.session.add(setting)
                    settings_added += 1

            if settings_added > 0:
                db.session.commit()
                print(f"✓ Добавлено {settings_added} настроек по умолчанию")
            else:
                print("✓ Все настройки по умолчанию уже существуют")

            print("\n✅ Миграция завершена успешно!")
            print("\nДобавлено:")
            print("  - Таблица user_activity: логирование активности пользователей")
            print("  - Таблица admin_audit_log: логирование действий администраторов")
            print("  - Таблица system_settings: глобальные настройки системы")
            print("  - Поля в users: blocked_at, blocked_by_user_id, notes")
            print("  - Системные настройки по умолчанию")

            return True

        except Exception as e:
            print(f"✗ Ошибка при миграции: {e}")
            import traceback
            traceback.print_exc()
            db.session.rollback()
            return False


if __name__ == '__main__':
    success = migrate()
    sys.exit(0 if success else 1)
