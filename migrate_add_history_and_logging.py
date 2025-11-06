"""
Миграция базы данных для добавления:
- Полей request_body и response_body в api_logs
- Таблицы card_edit_history для отслеживания изменений
- Таблицы product_stocks для остатков по складам
- Дополнительных полей в products
"""
import sqlite3
import sys
from pathlib import Path


def migrate_database(db_path: str):
    """Применить миграции к базе данных"""
    print(f"🔄 Запуск миграции базы данных: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # === Миграция 1: Добавление полей в api_logs для полного логирования ===
        print("📝 Проверка таблицы api_logs...")

        cursor.execute("PRAGMA table_info(api_logs)")
        api_logs_columns = {row[1] for row in cursor.fetchall()}

        new_api_logs_columns = {
            'request_body': 'TEXT',
            'response_body': 'TEXT',
        }

        for column_name, column_type in new_api_logs_columns.items():
            if column_name not in api_logs_columns:
                print(f"  ➕ Добавление колонки api_logs.{column_name}")
                cursor.execute(f"ALTER TABLE api_logs ADD COLUMN {column_name} {column_type}")
                conn.commit()
            else:
                print(f"  ✓ Колонка api_logs.{column_name} уже существует")

        # === Миграция 2: Добавление полей в products ===
        print("📝 Проверка таблицы products...")

        cursor.execute("PRAGMA table_info(products)")
        products_columns = {row[1] for row in cursor.fetchall()}

        new_products_columns = {
            'characteristics_json': 'TEXT',
            'description': 'TEXT',
            'dimensions_json': 'TEXT',
        }

        for column_name, column_type in new_products_columns.items():
            if column_name not in products_columns:
                print(f"  ➕ Добавление колонки products.{column_name}")
                cursor.execute(f"ALTER TABLE products ADD COLUMN {column_name} {column_type}")
                conn.commit()
            else:
                print(f"  ✓ Колонка products.{column_name} уже существует")

        # === Миграция 3: Создание таблицы card_edit_history ===
        print("📝 Проверка таблицы card_edit_history...")
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='card_edit_history'
        """)

        if not cursor.fetchone():
            print("  ➕ Создание таблицы card_edit_history")
            cursor.execute("""
                CREATE TABLE card_edit_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    seller_id INTEGER NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    changed_fields TEXT,
                    snapshot_before TEXT,
                    snapshot_after TEXT,
                    wb_synced BOOLEAN DEFAULT 0,
                    wb_sync_status VARCHAR(50),
                    wb_error_message TEXT,
                    reverted BOOLEAN DEFAULT 0,
                    reverted_at DATETIME,
                    reverted_by_history_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    user_comment TEXT,
                    FOREIGN KEY (product_id) REFERENCES products(id),
                    FOREIGN KEY (seller_id) REFERENCES sellers(id),
                    FOREIGN KEY (reverted_by_history_id) REFERENCES card_edit_history(id)
                )
            """)

            # Создание индексов
            cursor.execute("CREATE INDEX idx_card_edit_history_product_id ON card_edit_history(product_id)")
            cursor.execute("CREATE INDEX idx_card_edit_history_seller_id ON card_edit_history(seller_id)")
            cursor.execute("CREATE INDEX idx_card_edit_history_created_at ON card_edit_history(created_at)")

            conn.commit()
            print("  ✓ Таблица card_edit_history создана с индексами")
        else:
            print("  ✓ Таблица card_edit_history уже существует")

        # === Миграция 4: Создание таблицы product_stocks ===
        print("📝 Проверка таблицы product_stocks...")
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='product_stocks'
        """)

        if not cursor.fetchone():
            print("  ➕ Создание таблицы product_stocks")
            cursor.execute("""
                CREATE TABLE product_stocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    warehouse_id INTEGER,
                    warehouse_name VARCHAR(200),
                    quantity INTEGER DEFAULT 0,
                    quantity_full INTEGER DEFAULT 0,
                    in_way_to_client INTEGER DEFAULT 0,
                    in_way_from_client INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
                    UNIQUE(product_id, warehouse_id)
                )
            """)

            # Создание индексов
            cursor.execute("CREATE INDEX idx_product_stocks_product_id ON product_stocks(product_id)")
            cursor.execute("CREATE INDEX idx_product_stocks_warehouse_id ON product_stocks(warehouse_id)")

            conn.commit()
            print("  ✓ Таблица product_stocks создана с индексами")
        else:
            print("  ✓ Таблица product_stocks уже существует")

        print("✅ Миграция успешно завершена!")
        print("\n📋 Добавлено:")
        print("  • Поля request_body и response_body в api_logs для полного логирования")
        print("  • Таблица card_edit_history для отслеживания изменений с функцией отката")
        print("  • Таблица product_stocks для остатков по складам")
        print("  • Поля characteristics_json, description, dimensions_json в products")

        return True

    except Exception as e:
        print(f"❌ Ошибка миграции: {e}", file=sys.stderr)
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    """Точка входа для CLI"""
    import argparse

    parser = argparse.ArgumentParser(description='Миграция базы данных - добавление истории изменений и логирования')
    parser.add_argument(
        '--db-path',
        default='data/seller_platform.db',
        help='Путь к файлу базы данных (по умолчанию: data/seller_platform.db)'
    )

    args = parser.parse_args()

    # Создать директорию если не существует
    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        migrate_database(str(db_path))
        print("\n💡 Теперь вы можете:")
        print("  • Просматривать полные логи API запросов с телами запросов и ответов")
        print("  • Отслеживать все изменения карточек товаров")
        print("  • Откатывать изменения к предыдущему состоянию")
        print("  • Экспортировать товары в CSV с правильной кодировкой (UTF-8 с BOM)")
        sys.exit(0)
    except Exception as e:
        print(f"💥 Миграция не удалась: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
