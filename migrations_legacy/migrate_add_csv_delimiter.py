#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Миграция: добавление поля csv_delimiter в auto_import_settings
"""
from seller_platform import app, db
from models import AutoImportSettings

def migrate():
    """Выполняет миграцию"""
    with app.app_context():
        print("🔄 Добавление поля csv_delimiter в auto_import_settings...")

        try:
            # Проверяем, существует ли поле
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            columns = [col['name'] for col in inspector.get_columns('auto_import_settings')]

            if 'csv_delimiter' not in columns:
                # Добавляем колонку
                with db.engine.connect() as conn:
                    conn.execute(db.text(
                        "ALTER TABLE auto_import_settings ADD COLUMN csv_delimiter VARCHAR(5) DEFAULT ';'"
                    ))
                    conn.commit()
                print("✅ Поле csv_delimiter добавлено")

                # Устанавливаем значение по умолчанию для существующих записей
                AutoImportSettings.query.update({'csv_delimiter': ';'})
                db.session.commit()
                print("✅ Установлены значения по умолчанию")
            else:
                print("ℹ️  Поле csv_delimiter уже существует")

            print("\n✅ Миграция завершена успешно!")

        except Exception as e:
            print(f"\n❌ Ошибка миграции: {e}")
            db.session.rollback()
            raise

if __name__ == '__main__':
    migrate()
