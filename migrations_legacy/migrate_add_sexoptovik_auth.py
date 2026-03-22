#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Миграция: добавление полей авторизации sexoptovik в auto_import_settings
"""
from seller_platform import app, db
from models import AutoImportSettings

def migrate():
    """Выполняет миграцию"""
    with app.app_context():
        print("🔄 Добавление полей авторизации sexoptovik в auto_import_settings...")

        try:
            # Проверяем, существуют ли поля
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            columns = [col['name'] for col in inspector.get_columns('auto_import_settings')]

            fields_to_add = []
            if 'sexoptovik_login' not in columns:
                fields_to_add.append('sexoptovik_login')
            if 'sexoptovik_password' not in columns:
                fields_to_add.append('sexoptovik_password')

            if fields_to_add:
                with db.engine.connect() as conn:
                    for field in fields_to_add:
                        print(f"  Добавление поля {field}...")
                        conn.execute(db.text(
                            f"ALTER TABLE auto_import_settings ADD COLUMN {field} VARCHAR(200)"
                        ))
                    conn.commit()
                print(f"✅ Добавлены поля: {', '.join(fields_to_add)}")
            else:
                print("ℹ️  Все поля уже существуют")

            print("\n✅ Миграция завершена успешно!")
            print("\nТеперь можно:")
            print("1. Перейти в настройки автоимпорта")
            print("2. Заполнить логин и пароль от sexoptovik.ru")
            print("3. Фотографии будут загружаться автоматически с авторизацией")

        except Exception as e:
            print(f"\n❌ Ошибка миграции: {e}")
            db.session.rollback()
            raise

if __name__ == '__main__':
    migrate()
