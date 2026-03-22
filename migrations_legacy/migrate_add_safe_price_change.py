#!/usr/bin/env python3
"""
Миграция БД: Добавление таблиц для безопасного изменения цен

Создает таблицы:
- safe_price_change_settings - настройки безопасности для каждого продавца
- price_change_batches - пакеты изменений цен
- price_change_items - отдельные элементы изменений

Запуск:
    python migrate_add_safe_price_change.py
"""

import sqlite3
import os
from datetime import datetime

# Путь к базе данных
def get_db_path():
    """Получить путь к базе данных"""
    # Сначала проверяем DATABASE_PATH
    db_path = os.environ.get('DATABASE_PATH')
    if db_path:
        return db_path
    # Затем DATABASE_URL
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('sqlite:///'):
        return db_url.replace('sqlite:///', '')
    # По умолчанию
    return 'data/seller_platform.db'

DB_PATH = get_db_path()


def get_connection():
    """Получить соединение с БД"""
    return sqlite3.connect(DB_PATH)


def table_exists(cursor, table_name: str) -> bool:
    """Проверить существует ли таблица"""
    cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name=?
    """, (table_name,))
    return cursor.fetchone() is not None


def migrate():
    """Выполнить миграцию"""
    conn = get_connection()
    cursor = conn.cursor()

    print("=" * 60)
    print("Миграция: Добавление таблиц для безопасного изменения цен")
    print("=" * 60)

    # 1. Создание таблицы safe_price_change_settings
    if not table_exists(cursor, 'safe_price_change_settings'):
        print("\n📦 Создание таблицы safe_price_change_settings...")
        cursor.execute("""
            CREATE TABLE safe_price_change_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL UNIQUE,

                -- Основные настройки
                is_enabled BOOLEAN DEFAULT 1 NOT NULL,
                safe_threshold_percent REAL DEFAULT 10.0 NOT NULL,
                warning_threshold_percent REAL DEFAULT 20.0 NOT NULL,
                mode VARCHAR(20) DEFAULT 'confirm' NOT NULL,

                -- Дополнительные настройки
                require_comment_for_dangerous BOOLEAN DEFAULT 1 NOT NULL,
                allow_bulk_dangerous BOOLEAN DEFAULT 0 NOT NULL,
                max_products_per_batch INTEGER DEFAULT 1000 NOT NULL,
                allow_unlimited_batch BOOLEAN DEFAULT 1 NOT NULL,

                -- Уведомления
                notify_on_dangerous BOOLEAN DEFAULT 1 NOT NULL,
                notify_email VARCHAR(200),

                -- Метаданные
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (seller_id) REFERENCES sellers(id)
            )
        """)
        cursor.execute("CREATE INDEX idx_safe_price_seller ON safe_price_change_settings(seller_id)")
        print("   ✅ Таблица safe_price_change_settings создана")
    else:
        print("   ⏭️  Таблица safe_price_change_settings уже существует")

    # 2. Создание таблицы price_change_batches
    if not table_exists(cursor, 'price_change_batches'):
        print("\n📦 Создание таблицы price_change_batches...")
        cursor.execute("""
            CREATE TABLE price_change_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,

                -- Описание операции
                name VARCHAR(200),
                description TEXT,
                change_type VARCHAR(50) NOT NULL,
                change_value REAL,
                change_formula VARCHAR(500),

                -- Статус
                status VARCHAR(30) DEFAULT 'draft' NOT NULL,

                -- Классификация безопасности
                has_safe_changes BOOLEAN DEFAULT 0,
                has_warning_changes BOOLEAN DEFAULT 0,
                has_dangerous_changes BOOLEAN DEFAULT 0,

                -- Статистика
                total_items INTEGER DEFAULT 0,
                safe_count INTEGER DEFAULT 0,
                warning_count INTEGER DEFAULT 0,
                dangerous_count INTEGER DEFAULT 0,
                applied_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,

                -- Подтверждение
                confirmed_at TIMESTAMP,
                confirmed_by_user_id INTEGER,
                confirmation_comment TEXT,

                -- Применение
                applied_at TIMESTAMP,
                wb_task_id VARCHAR(100),
                apply_errors TEXT,

                -- Откат
                reverted BOOLEAN DEFAULT 0,
                reverted_at TIMESTAMP,
                reverted_by_user_id INTEGER,
                revert_batch_id INTEGER,

                -- Метаданные
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (seller_id) REFERENCES sellers(id),
                FOREIGN KEY (confirmed_by_user_id) REFERENCES users(id),
                FOREIGN KEY (reverted_by_user_id) REFERENCES users(id),
                FOREIGN KEY (revert_batch_id) REFERENCES price_change_batches(id)
            )
        """)
        cursor.execute("CREATE INDEX idx_price_batch_seller_status ON price_change_batches(seller_id, status)")
        cursor.execute("CREATE INDEX idx_price_batch_seller_created ON price_change_batches(seller_id, created_at)")
        cursor.execute("CREATE INDEX idx_price_batch_status ON price_change_batches(status)")
        print("   ✅ Таблица price_change_batches создана")
    else:
        print("   ⏭️  Таблица price_change_batches уже существует")

    # 3. Создание таблицы price_change_items
    if not table_exists(cursor, 'price_change_items'):
        print("\n📦 Создание таблицы price_change_items...")
        cursor.execute("""
            CREATE TABLE price_change_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,

                -- Идентификация товара
                nm_id BIGINT NOT NULL,
                vendor_code VARCHAR(100),
                product_title VARCHAR(500),

                -- Текущие значения
                old_price DECIMAL(10, 2),
                old_discount INTEGER,
                old_discount_price DECIMAL(10, 2),

                -- Новые значения
                new_price DECIMAL(10, 2),
                new_discount INTEGER,
                new_discount_price DECIMAL(10, 2),

                -- Расчетные метрики
                price_change_amount DECIMAL(10, 2),
                price_change_percent REAL,

                -- Классификация безопасности
                safety_level VARCHAR(20) DEFAULT 'safe' NOT NULL,

                -- Статус элемента
                status VARCHAR(20) DEFAULT 'pending' NOT NULL,
                error_message TEXT,

                -- Результат от WB
                wb_applied_at TIMESTAMP,
                wb_status VARCHAR(50),

                -- Метаданные
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (batch_id) REFERENCES price_change_batches(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX idx_price_item_batch ON price_change_items(batch_id)")
        cursor.execute("CREATE INDEX idx_price_item_product ON price_change_items(product_id)")
        cursor.execute("CREATE INDEX idx_price_item_nm_id ON price_change_items(nm_id)")
        cursor.execute("CREATE INDEX idx_price_item_batch_safety ON price_change_items(batch_id, safety_level)")
        cursor.execute("CREATE INDEX idx_price_item_batch_status ON price_change_items(batch_id, status)")
        print("   ✅ Таблица price_change_items создана")
    else:
        print("   ⏭️  Таблица price_change_items уже существует")

    # Создание настроек по умолчанию для существующих продавцов
    print("\n📝 Создание настроек по умолчанию для продавцов...")
    if table_exists(cursor, 'sellers'):
        cursor.execute("""
            INSERT INTO safe_price_change_settings (
                seller_id, is_enabled, safe_threshold_percent,
                warning_threshold_percent, mode,
                require_comment_for_dangerous, allow_bulk_dangerous,
                max_products_per_batch, allow_unlimited_batch,
                notify_on_dangerous
            )
            SELECT id, 1, 10.0, 20.0, 'confirm', 1, 0, 1000, 1, 1
            FROM sellers
            WHERE id NOT IN (SELECT seller_id FROM safe_price_change_settings)
        """)
        created_settings = cursor.rowcount
        if created_settings > 0:
            print(f"   ✅ Создано настроек: {created_settings}")
        else:
            print("   ⏭️  Все продавцы уже имеют настройки")
    else:
        print("   ⏭️  Таблица sellers не существует (будет создана при запуске приложения)")

    # Сохранение изменений
    conn.commit()
    conn.close()

    print("\n" + "=" * 60)
    print("✅ Миграция успешно завершена!")
    print("=" * 60)


def rollback():
    """Откатить миграцию (удалить созданные таблицы)"""
    conn = get_connection()
    cursor = conn.cursor()

    print("⚠️  Откат миграции safe_price_change...")

    # Удаление в обратном порядке из-за foreign keys
    tables = ['price_change_items', 'price_change_batches', 'safe_price_change_settings']

    for table in tables:
        if table_exists(cursor, table):
            cursor.execute(f"DROP TABLE {table}")
            print(f"   🗑️  Таблица {table} удалена")
        else:
            print(f"   ⏭️  Таблица {table} не существует")

    conn.commit()
    conn.close()

    print("✅ Откат завершен")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--rollback':
        rollback()
    else:
        migrate()
