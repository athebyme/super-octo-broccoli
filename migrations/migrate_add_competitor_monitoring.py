#!/usr/bin/env python3
"""
Миграция БД: Добавление таблиц для мониторинга конкурентов

Создает таблицы:
- competitor_monitor_settings - настройки мониторинга для каждого продавца
- competitor_groups - именованные группы конкурентов
- competitor_products - отслеживаемые товары конкурентов
- competitor_price_snapshots - снимки цен (delta storage)
- competitor_alerts - уведомления об изменениях

Запуск:
    python migrate_add_competitor_monitoring.py
"""

import sqlite3
import os
from datetime import datetime


def get_db_path():
    """Получить путь к базе данных"""
    db_path = os.environ.get('DATABASE_PATH')
    if db_path:
        return db_path
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('sqlite:///'):
        return db_url.replace('sqlite:///', '')
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
    print("Миграция: Добавление таблиц для мониторинга конкурентов")
    print("=" * 60)

    # 1. competitor_monitor_settings
    if not table_exists(cursor, 'competitor_monitor_settings'):
        print("\nСоздание таблицы competitor_monitor_settings...")
        cursor.execute("""
            CREATE TABLE competitor_monitor_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL UNIQUE,
                is_enabled BOOLEAN DEFAULT 0,
                is_running BOOLEAN DEFAULT 0,
                price_change_alert_percent REAL DEFAULT 5.0,
                requests_per_minute INTEGER DEFAULT 60,
                max_products INTEGER DEFAULT 100000,
                pause_between_cycles_seconds INTEGER DEFAULT 0,
                last_sync_at DATETIME,
                last_sync_status VARCHAR(50) DEFAULT 'never',
                last_sync_error TEXT,
                last_full_cycle_duration REAL,
                total_products_monitored INTEGER DEFAULT 0,
                total_cycles_completed INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (seller_id) REFERENCES sellers(id)
            )
        """)
        cursor.execute("CREATE INDEX idx_cms_seller ON competitor_monitor_settings(seller_id)")
        print("  Таблица competitor_monitor_settings создана")
    else:
        print("  Таблица competitor_monitor_settings уже существует")

    # 2. competitor_groups
    if not table_exists(cursor, 'competitor_groups'):
        print("\nСоздание таблицы competitor_groups...")
        cursor.execute("""
            CREATE TABLE competitor_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                name VARCHAR(200) NOT NULL,
                description TEXT,
                color VARCHAR(7) DEFAULT '#3B82F6',
                own_product_id INTEGER,
                auto_source VARCHAR(20) DEFAULT 'manual',
                auto_source_value VARCHAR(200),
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (seller_id) REFERENCES sellers(id),
                FOREIGN KEY (own_product_id) REFERENCES products(id)
            )
        """)
        cursor.execute("CREATE INDEX idx_cg_seller ON competitor_groups(seller_id)")
        print("  Таблица competitor_groups создана")
    else:
        print("  Таблица competitor_groups уже существует")

    # 3. competitor_products
    if not table_exists(cursor, 'competitor_products'):
        print("\nСоздание таблицы competitor_products...")
        cursor.execute("""
            CREATE TABLE competitor_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                nm_id BIGINT NOT NULL,
                title VARCHAR(500),
                brand VARCHAR(200),
                supplier_name VARCHAR(200),
                wb_supplier_id BIGINT,
                image_url VARCHAR(500),
                current_price INTEGER,
                current_sale_price INTEGER,
                current_rating REAL,
                current_feedbacks_count INTEGER,
                current_total_stock INTEGER,
                priority INTEGER DEFAULT 2,
                is_active BOOLEAN DEFAULT 1,
                last_fetched_at DATETIME,
                fetch_error_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (seller_id) REFERENCES sellers(id),
                FOREIGN KEY (group_id) REFERENCES competitor_groups(id),
                UNIQUE (seller_id, nm_id, group_id)
            )
        """)
        cursor.execute("CREATE INDEX idx_cp_seller ON competitor_products(seller_id)")
        cursor.execute("CREATE INDEX idx_cp_group ON competitor_products(group_id)")
        cursor.execute("CREATE INDEX idx_cp_nm_id ON competitor_products(nm_id)")
        cursor.execute("CREATE INDEX idx_cp_seller_active_priority ON competitor_products(seller_id, is_active, priority)")
        print("  Таблица competitor_products создана")
    else:
        print("  Таблица competitor_products уже существует")

    # 4. competitor_price_snapshots
    if not table_exists(cursor, 'competitor_price_snapshots'):
        print("\nСоздание таблицы competitor_price_snapshots...")
        cursor.execute("""
            CREATE TABLE competitor_price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                seller_id INTEGER NOT NULL,
                price INTEGER,
                sale_price INTEGER,
                rating REAL,
                feedbacks_count INTEGER,
                total_stock INTEGER,
                price_change_percent REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES competitor_products(id) ON DELETE CASCADE,
                FOREIGN KEY (seller_id) REFERENCES sellers(id)
            )
        """)
        cursor.execute("CREATE INDEX idx_cps_product_created ON competitor_price_snapshots(product_id, created_at)")
        cursor.execute("CREATE INDEX idx_cps_seller_created ON competitor_price_snapshots(seller_id, created_at)")
        cursor.execute("CREATE INDEX idx_cps_created ON competitor_price_snapshots(created_at)")
        print("  Таблица competitor_price_snapshots создана")
    else:
        print("  Таблица competitor_price_snapshots уже существует")

    # 5. competitor_alerts
    if not table_exists(cursor, 'competitor_alerts'):
        print("\nСоздание таблицы competitor_alerts...")
        cursor.execute("""
            CREATE TABLE competitor_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                product_id INTEGER,
                group_id INTEGER,
                alert_type VARCHAR(30) NOT NULL,
                severity VARCHAR(10) DEFAULT 'info',
                old_value REAL,
                new_value REAL,
                change_percent REAL,
                message TEXT,
                is_read BOOLEAN DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (seller_id) REFERENCES sellers(id),
                FOREIGN KEY (product_id) REFERENCES competitor_products(id) ON DELETE CASCADE,
                FOREIGN KEY (group_id) REFERENCES competitor_groups(id) ON DELETE SET NULL
            )
        """)
        cursor.execute("CREATE INDEX idx_ca_seller ON competitor_alerts(seller_id)")
        cursor.execute("CREATE INDEX idx_ca_seller_read ON competitor_alerts(seller_id, is_read)")
        cursor.execute("CREATE INDEX idx_ca_created ON competitor_alerts(created_at)")
        print("  Таблица competitor_alerts создана")
    else:
        print("  Таблица competitor_alerts уже существует")

    conn.commit()
    conn.close()

    print("\n" + "=" * 60)
    print("Миграция завершена успешно!")
    print("=" * 60)


def rollback():
    """Откат миграции"""
    conn = get_connection()
    cursor = conn.cursor()

    tables = [
        'competitor_alerts',
        'competitor_price_snapshots',
        'competitor_products',
        'competitor_groups',
        'competitor_monitor_settings',
    ]

    print("Откат миграции: удаление таблиц мониторинга конкурентов")
    for table in tables:
        if table_exists(cursor, table):
            cursor.execute(f"DROP TABLE {table}")
            print(f"  Удалена таблица {table}")

    conn.commit()
    conn.close()
    print("Откат завершён")


if __name__ == '__main__':
    import sys
    if '--rollback' in sys.argv:
        rollback()
    else:
        migrate()
