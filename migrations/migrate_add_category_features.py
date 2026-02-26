#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Миграция: добавление функционала для ручного исправления категорий
- Добавляет поля category_confidence и all_categories в imported_products
- Создает таблицу product_category_corrections
"""
from seller_platform import app, db
from sqlalchemy import inspect

def migrate():
    """Выполняет миграцию"""
    with app.app_context():
        print("🔄 Добавление функционала для исправления категорий...")

        try:
            inspector = inspect(db.engine)

            # 1. Добавляем поля в imported_products
            print("\n1️⃣ Добавление полей в imported_products...")
            columns = [col['name'] for col in inspector.get_columns('imported_products')]

            with db.engine.connect() as conn:
                if 'category_confidence' not in columns:
                    conn.execute(db.text(
                        "ALTER TABLE imported_products ADD COLUMN category_confidence REAL DEFAULT 0.0"
                    ))
                    conn.commit()
                    print("   ✅ Поле category_confidence добавлено")
                else:
                    print("   ℹ️  Поле category_confidence уже существует")

                if 'all_categories' not in columns:
                    conn.execute(db.text(
                        "ALTER TABLE imported_products ADD COLUMN all_categories TEXT"
                    ))
                    conn.commit()
                    print("   ✅ Поле all_categories добавлено")
                else:
                    print("   ℹ️  Поле all_categories уже существует")

            # 2. Создаем таблицу product_category_corrections
            print("\n2️⃣ Создание таблицы product_category_corrections...")

            if 'product_category_corrections' not in inspector.get_table_names():
                with db.engine.connect() as conn:
                    conn.execute(db.text("""
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
                    """))
                    conn.commit()
                    print("   ✅ Таблица product_category_corrections создана")

                    # Создаем индексы
                    conn.execute(db.text(
                        "CREATE INDEX idx_correction_external ON product_category_corrections(external_id, source_type)"
                    ))
                    conn.execute(db.text(
                        "CREATE INDEX idx_correction_category ON product_category_corrections(original_category, source_type)"
                    ))
                    conn.execute(db.text(
                        "CREATE INDEX idx_correction_product ON product_category_corrections(imported_product_id)"
                    ))
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
            raise

if __name__ == '__main__':
    migrate()
