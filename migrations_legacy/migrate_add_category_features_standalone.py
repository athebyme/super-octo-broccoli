#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Миграция: добавление функционала для ручного исправления категорий
Standalone версия - не требует импорта seller_platform
"""
import sqlite3
import os

def migrate():
    """Выполняет миграцию"""
    # Определяем путь к базе данных
    db_path = os.environ.get('DATABASE_URL', 'sqlite:///./data/seller_platform.db')

    # Извлекаем путь к файлу из sqlite URL
    if db_path.startswith('sqlite:///'):
        db_file = db_path.replace('sqlite:///', '')
    else:
        print(f"❌ Неподдерживаемый DATABASE_URL: {db_path}")
        return

    if not os.path.exists(db_file):
        print(f"❌ База данных не найдена: {db_file}")
        return

    print(f"🔄 Подключение к базе данных: {db_file}")

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    try:
        print("\n🔄 Добавление функционала для исправления категорий...")

        # 1. Добавляем поля в imported_products
        print("\n1️⃣ Добавление полей в imported_products...")

        # Получаем список существующих колонок
        cursor.execute("PRAGMA table_info(imported_products)")
        columns = [row[1] for row in cursor.fetchall()]

        if 'category_confidence' not in columns:
            cursor.execute("""
                ALTER TABLE imported_products
                ADD COLUMN category_confidence REAL DEFAULT 0.0
            """)
            conn.commit()
            print("   ✅ Поле category_confidence добавлено")
        else:
            print("   ℹ️  Поле category_confidence уже существует")

        if 'all_categories' not in columns:
            cursor.execute("""
                ALTER TABLE imported_products
                ADD COLUMN all_categories TEXT
            """)
            conn.commit()
            print("   ✅ Поле all_categories добавлено")
        else:
            print("   ℹ️  Поле all_categories уже существует")

        # 2. Создаем таблицу product_category_corrections
        print("\n2️⃣ Создание таблицы product_category_corrections...")

        # Проверяем, существует ли таблица
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='product_category_corrections'
        """)

        if not cursor.fetchone():
            cursor.execute("""
                CREATE TABLE product_category_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    imported_product_id INTEGER,
                    external_id VARCHAR(200),
                    source_type VARCHAR(50) DEFAULT 'sexoptovik',
                    product_title VARCHAR(500),
                    original_category VARCHAR(200),
                    corrected_wb_subject_id INTEGER NOT NULL,
                    corrected_wb_subject_name VARCHAR(200),
                    corrected_by_user_id INTEGER,
                    correction_reason TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (imported_product_id) REFERENCES imported_products(id),
                    FOREIGN KEY (corrected_by_user_id) REFERENCES users(id)
                )
            """)
            conn.commit()
            print("   ✅ Таблица product_category_corrections создана")

            # Создаем индексы
            cursor.execute("""
                CREATE INDEX idx_correction_external
                ON product_category_corrections(external_id, source_type)
            """)
            cursor.execute("""
                CREATE INDEX idx_correction_category
                ON product_category_corrections(original_category, source_type)
            """)
            cursor.execute("""
                CREATE INDEX idx_correction_product
                ON product_category_corrections(imported_product_id)
            """)
            conn.commit()
            print("   ✅ Индексы созданы")
        else:
            print("   ℹ️  Таблица product_category_corrections уже существует")

        print("\n✅ Миграция завершена успешно!")
        print("\n📝 Добавлены:")
        print("   - imported_products.category_confidence (REAL)")
        print("   - imported_products.all_categories (TEXT)")
        print("   - product_category_corrections (TABLE)")

    except Exception as e:
        print(f"\n❌ Ошибка миграции: {e}")
        import traceback
        traceback.print_exc()
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == '__main__':
    migrate()
