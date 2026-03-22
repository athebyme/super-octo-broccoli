"""
Миграция базы данных для добавления новых полей и таблиц
Выполняется автоматически при запуске приложения
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
        # === Миграция 1: Добавление полей в таблицу sellers ===
        print("📝 Проверка таблицы sellers...")

        # Проверяем существует ли таблица sellers
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='sellers'
        """)

        if not cursor.fetchone():
            print("  ⚠️  Таблица sellers не существует, пропускаем миграцию")
            print("  💡 Таблица будет создана автоматически через db.create_all()")
        else:
            # Получить список существующих колонок
            cursor.execute("PRAGMA table_info(sellers)")
            existing_columns = {row[1] for row in cursor.fetchall()}

            # Список новых колонок для добавления
            new_columns = {
                'api_last_sync': 'DATETIME',
                'api_sync_status': 'VARCHAR(50)',
            }

            for column_name, column_type in new_columns.items():
                if column_name not in existing_columns:
                    print(f"  ➕ Добавление колонки: {column_name}")
                    cursor.execute(f"ALTER TABLE sellers ADD COLUMN {column_name} {column_type}")
                    conn.commit()
                else:
                    print(f"  ✓ Колонка {column_name} уже существует")

        # === Миграция 2: Создание таблицы products ===
        print("📝 Проверка таблицы products...")
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='products'
        """)

        if not cursor.fetchone():
            print("  ➕ Создание таблицы products")
            cursor.execute("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    nm_id BIGINT NOT NULL,
                    imt_id BIGINT,
                    vendor_code VARCHAR(100),
                    title VARCHAR(500),
                    brand VARCHAR(200),
                    object_name VARCHAR(200),
                    supplier_vendor_code VARCHAR(100),
                    price NUMERIC(10, 2),
                    discount_price NUMERIC(10, 2),
                    quantity INTEGER DEFAULT 0,
                    photos_json TEXT,
                    video_url VARCHAR(500),
                    sizes_json TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_sync DATETIME,
                    FOREIGN KEY (seller_id) REFERENCES sellers(id)
                )
            """)

            # Создание индексов
            cursor.execute("CREATE INDEX idx_products_seller_id ON products(seller_id)")
            cursor.execute("CREATE INDEX idx_products_nm_id ON products(nm_id)")
            cursor.execute("CREATE INDEX idx_products_vendor_code ON products(vendor_code)")
            cursor.execute("CREATE INDEX idx_seller_nm_id ON products(seller_id, nm_id)")
            cursor.execute("CREATE INDEX idx_seller_vendor_code ON products(seller_id, vendor_code)")
            cursor.execute("CREATE INDEX idx_seller_active ON products(seller_id, is_active)")

            conn.commit()
            print("  ✓ Таблица products создана с индексами")
        else:
            print("  ✓ Таблица products уже существует")

        # === Миграция 3: Создание таблицы api_logs ===
        print("📝 Проверка таблицы api_logs...")
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='api_logs'
        """)

        if not cursor.fetchone():
            print("  ➕ Создание таблицы api_logs")
            cursor.execute("""
                CREATE TABLE api_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    endpoint VARCHAR(200) NOT NULL,
                    method VARCHAR(10) NOT NULL,
                    status_code INTEGER,
                    response_time FLOAT,
                    success BOOLEAN DEFAULT 1,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (seller_id) REFERENCES sellers(id)
                )
            """)

            # Создание индексов
            cursor.execute("CREATE INDEX idx_api_logs_seller_id ON api_logs(seller_id)")
            cursor.execute("CREATE INDEX idx_api_logs_created_at ON api_logs(created_at)")
            cursor.execute("CREATE INDEX idx_seller_created ON api_logs(seller_id, created_at)")

            conn.commit()
            print("  ✓ Таблица api_logs создана с индексами")
        else:
            print("  ✓ Таблица api_logs уже существует")

        # === Миграция 4: Создание таблицы seller_reports (если нужна) ===
        print("📝 Проверка таблицы seller_reports...")
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='seller_reports'
        """)

        if not cursor.fetchone():
            print("  ➕ Создание таблицы seller_reports")
            cursor.execute("""
                CREATE TABLE seller_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    statistics_path VARCHAR(500) NOT NULL,
                    price_path VARCHAR(500) NOT NULL,
                    processed_path VARCHAR(500) NOT NULL,
                    selected_columns TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (seller_id) REFERENCES sellers(id)
                )
            """)

            cursor.execute("CREATE INDEX idx_seller_reports_seller_id ON seller_reports(seller_id)")

            conn.commit()
            print("  ✓ Таблица seller_reports создана")
        else:
            print("  ✓ Таблица seller_reports уже существует")

        print("✅ Миграция успешно завершена!")
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

    parser = argparse.ArgumentParser(description='Миграция базы данных')
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
        sys.exit(0)
    except Exception as e:
        print(f"💥 Миграция не удалась: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
