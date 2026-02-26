#!/usr/bin/env python3
"""
Миграция для добавления таблиц заблокированных и скрытых карточек

Добавляет таблицы:
- blocked_cards: заблокированные карточки товаров WB
- shadowed_cards: карточки, скрытые из каталога WB
- blocked_cards_sync_settings: настройки синхронизации
"""

import os
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from seller_platform import app, db
from models import BlockedCard, ShadowedCard, BlockedCardsSyncSettings


def migrate():
    """Выполнить миграцию"""
    with app.app_context():
        print("Начало миграции: добавление таблиц заблокированных/скрытых карточек")

        try:
            inspector = db.inspect(db.engine)
            existing_tables = inspector.get_table_names()

            tables_to_create = []
            if 'blocked_cards' not in existing_tables:
                tables_to_create.append('blocked_cards')
            if 'shadowed_cards' not in existing_tables:
                tables_to_create.append('shadowed_cards')
            if 'blocked_cards_sync_settings' not in existing_tables:
                tables_to_create.append('blocked_cards_sync_settings')

            if tables_to_create:
                print(f"Создание таблиц: {', '.join(tables_to_create)}")

                BlockedCard.__table__.create(db.engine, checkfirst=True)
                ShadowedCard.__table__.create(db.engine, checkfirst=True)
                BlockedCardsSyncSettings.__table__.create(db.engine, checkfirst=True)

                print("✓ Таблицы успешно созданы")
            else:
                print("✓ Все таблицы уже существуют")

            print("\nМиграция завершена успешно!")
            print("\nТаблицы:")
            print("  - blocked_cards: заблокированные карточки товаров WB")
            print("  - shadowed_cards: карточки, скрытые из каталога WB")
            print("  - blocked_cards_sync_settings: настройки синхронизации")

            return True

        except Exception as e:
            print(f"✗ Ошибка при создании таблиц: {e}")
            import traceback
            traceback.print_exc()
            return False


if __name__ == '__main__':
    success = migrate()
    sys.exit(0 if success else 1)
