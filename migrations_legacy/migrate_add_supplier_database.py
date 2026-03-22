#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: Добавление единой базы товаров поставщика

Создаёт таблицы:
- suppliers — поставщики
- supplier_products — товары поставщиков (централизованная база)
- seller_suppliers — связь продавцов с поставщиками (M2M)

Модифицирует:
- imported_products — добавляет supplier_product_id, supplier_id

Мигрирует данные:
- Создаёт Supplier из существующих AutoImportSettings (sexoptovik)
- Связывает продавцов с поставщиком
"""
import os
import sys
import sqlite3
import json
import logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = BASE_DIR / 'data'
DEFAULT_DB_PATH = DATA_ROOT / 'seller_platform.db'


def get_db_path():
    """Получить путь к базе данных"""
    database_url = os.environ.get('DATABASE_URL')
    if database_url and database_url.startswith('sqlite:///'):
        return database_url.replace('sqlite:///', '')
    return str(DEFAULT_DB_PATH)


def run_migration():
    db_path = get_db_path()
    logger.info(f"Миграция базы данных: {db_path}")

    if not os.path.exists(db_path):
        logger.warning(f"База данных не найдена: {db_path}. Миграция будет применена при следующем запуске приложения.")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # ============================================================
        # 1. Создание таблицы suppliers
        # ============================================================
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='suppliers'")
        if not cursor.fetchone():
            logger.info("Создание таблицы suppliers...")
            cursor.execute("""
                CREATE TABLE suppliers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(200) NOT NULL,
                    code VARCHAR(50) NOT NULL UNIQUE,
                    description TEXT,
                    website VARCHAR(500),
                    logo_url VARCHAR(500),

                    csv_source_url VARCHAR(500),
                    csv_delimiter VARCHAR(5) DEFAULT ';',
                    csv_encoding VARCHAR(20) DEFAULT 'cp1251',
                    api_endpoint VARCHAR(500),

                    auth_login VARCHAR(200),
                    auth_password VARCHAR(500),

                    ai_enabled BOOLEAN DEFAULT 0 NOT NULL,
                    ai_provider VARCHAR(50) DEFAULT 'openai',
                    ai_api_key VARCHAR(500),
                    ai_api_base_url VARCHAR(500),
                    ai_model VARCHAR(100) DEFAULT 'gpt-4o-mini',
                    ai_temperature FLOAT DEFAULT 0.3,
                    ai_max_tokens INTEGER DEFAULT 2000,
                    ai_timeout INTEGER DEFAULT 60,
                    ai_client_id VARCHAR(500),
                    ai_client_secret VARCHAR(500),
                    ai_category_instruction TEXT,
                    ai_size_instruction TEXT,
                    ai_seo_title_instruction TEXT,
                    ai_keywords_instruction TEXT,
                    ai_description_instruction TEXT,
                    ai_analysis_instruction TEXT,

                    resize_images BOOLEAN DEFAULT 1 NOT NULL,
                    image_target_size INTEGER DEFAULT 1200,
                    image_background_color VARCHAR(20) DEFAULT 'white',

                    default_markup_percent FLOAT,

                    is_active BOOLEAN DEFAULT 1 NOT NULL,

                    total_products INTEGER DEFAULT 0,
                    last_sync_at DATETIME,
                    last_sync_status VARCHAR(50),
                    last_sync_error TEXT,
                    last_sync_duration FLOAT,

                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    created_by_user_id INTEGER REFERENCES users(id)
                )
            """)
            cursor.execute("CREATE INDEX idx_suppliers_code ON suppliers(code)")
            cursor.execute("CREATE INDEX idx_suppliers_active ON suppliers(is_active)")
            logger.info("Таблица suppliers создана")
        else:
            logger.info("Таблица suppliers уже существует")

        # ============================================================
        # 2. Создание таблицы supplier_products
        # ============================================================
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='supplier_products'")
        if not cursor.fetchone():
            logger.info("Создание таблицы supplier_products...")
            cursor.execute("""
                CREATE TABLE supplier_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),

                    external_id VARCHAR(200),
                    vendor_code VARCHAR(200),
                    barcode VARCHAR(200),

                    title VARCHAR(500),
                    description TEXT,
                    brand VARCHAR(200),
                    category VARCHAR(200),
                    all_categories TEXT,

                    wb_category_name VARCHAR(200),
                    wb_subject_id INTEGER,
                    wb_subject_name VARCHAR(200),
                    category_confidence FLOAT DEFAULT 0.0,

                    supplier_price FLOAT,
                    supplier_quantity INTEGER,
                    currency VARCHAR(10) DEFAULT 'RUB',

                    characteristics_json TEXT,
                    sizes_json TEXT,
                    colors_json TEXT,
                    materials_json TEXT,
                    dimensions_json TEXT,
                    gender VARCHAR(50),
                    country VARCHAR(100),
                    season VARCHAR(50),
                    age_group VARCHAR(50),

                    photo_urls_json TEXT,
                    processed_photos_json TEXT,
                    video_url VARCHAR(500),

                    ai_seo_title VARCHAR(500),
                    ai_description TEXT,
                    ai_keywords_json TEXT,
                    ai_bullets_json TEXT,
                    ai_rich_content_json TEXT,
                    ai_analysis_json TEXT,
                    ai_validated BOOLEAN DEFAULT 0,
                    ai_validated_at DATETIME,
                    ai_validation_score FLOAT,
                    content_hash VARCHAR(64),

                    original_data_json TEXT,

                    status VARCHAR(50) DEFAULT 'draft',
                    validation_errors_json TEXT,

                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,

                    UNIQUE(supplier_id, external_id)
                )
            """)
            cursor.execute("CREATE INDEX idx_supplier_products_supplier ON supplier_products(supplier_id)")
            cursor.execute("CREATE INDEX idx_supplier_products_external ON supplier_products(external_id)")
            cursor.execute("CREATE INDEX idx_supplier_products_status ON supplier_products(supplier_id, status)")
            cursor.execute("CREATE INDEX idx_supplier_products_category ON supplier_products(supplier_id, wb_subject_id)")
            cursor.execute("CREATE INDEX idx_supplier_products_brand ON supplier_products(supplier_id, brand)")
            logger.info("Таблица supplier_products создана")
        else:
            logger.info("Таблица supplier_products уже существует")

        # ============================================================
        # 3. Создание таблицы seller_suppliers
        # ============================================================
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='seller_suppliers'")
        if not cursor.fetchone():
            logger.info("Создание таблицы seller_suppliers...")
            cursor.execute("""
                CREATE TABLE seller_suppliers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id),
                    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),

                    is_active BOOLEAN DEFAULT 1 NOT NULL,
                    supplier_code VARCHAR(50),
                    vendor_code_pattern VARCHAR(200) DEFAULT 'id-{product_id}-{supplier_code}',

                    custom_markup_percent FLOAT,

                    products_imported INTEGER DEFAULT 0,
                    last_import_at DATETIME,

                    connected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,

                    UNIQUE(seller_id, supplier_id)
                )
            """)
            cursor.execute("CREATE INDEX idx_seller_suppliers_seller ON seller_suppliers(seller_id)")
            cursor.execute("CREATE INDEX idx_seller_suppliers_supplier ON seller_suppliers(supplier_id)")
            cursor.execute("CREATE INDEX idx_seller_suppliers_active ON seller_suppliers(seller_id, is_active)")
            logger.info("Таблица seller_suppliers создана")
        else:
            logger.info("Таблица seller_suppliers уже существует")

        # ============================================================
        # 4. Добавление колонок в imported_products
        # ============================================================
        cursor.execute("PRAGMA table_info(imported_products)")
        existing_columns = {row['name'] for row in cursor.fetchall()}

        if 'supplier_product_id' not in existing_columns:
            logger.info("Добавление колонки supplier_product_id в imported_products...")
            cursor.execute("ALTER TABLE imported_products ADD COLUMN supplier_product_id INTEGER REFERENCES supplier_products(id)")
            cursor.execute("CREATE INDEX idx_imported_supplier_product ON imported_products(supplier_product_id)")
            logger.info("Колонка supplier_product_id добавлена")
        else:
            logger.info("Колонка supplier_product_id уже существует")

        if 'supplier_id' not in existing_columns:
            logger.info("Добавление колонки supplier_id в imported_products...")
            cursor.execute("ALTER TABLE imported_products ADD COLUMN supplier_id INTEGER REFERENCES suppliers(id)")
            cursor.execute("CREATE INDEX idx_imported_supplier ON imported_products(supplier_id)")
            logger.info("Колонка supplier_id добавлена")
        else:
            logger.info("Колонка supplier_id уже существует")

        # ============================================================
        # 5. Миграция существующих данных
        # ============================================================
        logger.info("Проверка существующих данных для миграции...")

        # Проверяем, есть ли уже поставщик sexoptovik
        cursor.execute("SELECT id FROM suppliers WHERE code = 'sexoptovik'")
        existing_supplier = cursor.fetchone()

        if not existing_supplier:
            # Проверяем, есть ли настройки автоимпорта с sexoptovik
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auto_import_settings'")
            if cursor.fetchone():
                cursor.execute("""
                    SELECT s.id as seller_id, ais.*
                    FROM auto_import_settings ais
                    JOIN sellers s ON s.id = ais.seller_id
                    WHERE ais.csv_source_type = 'sexoptovik' OR ais.csv_source_url LIKE '%sexoptovik%'
                    LIMIT 1
                """)
                auto_import = cursor.fetchone()

                if auto_import:
                    logger.info("Найдены настройки sexoptovik — создаём поставщика...")

                    # Берём AI настройки из первой найденной записи
                    ai_provider = auto_import['ai_provider'] if 'ai_provider' in auto_import.keys() else 'openai'
                    ai_model = auto_import['ai_model'] if 'ai_model' in auto_import.keys() else 'gpt-4o-mini'
                    csv_url = auto_import['csv_source_url'] if 'csv_source_url' in auto_import.keys() else None
                    csv_delim = auto_import['csv_delimiter'] if 'csv_delimiter' in auto_import.keys() else ';'
                    auth_login = auto_import['sexoptovik_login'] if 'sexoptovik_login' in auto_import.keys() else None
                    auth_pwd = auto_import['sexoptovik_password'] if 'sexoptovik_password' in auto_import.keys() else None

                    # Получаем admin user id
                    cursor.execute("SELECT id FROM users WHERE is_admin = 1 LIMIT 1")
                    admin_row = cursor.fetchone()
                    admin_id = admin_row['id'] if admin_row else None

                    cursor.execute("""
                        INSERT INTO suppliers (name, code, description, website,
                            csv_source_url, csv_delimiter, csv_encoding,
                            auth_login, auth_password,
                            ai_enabled, ai_provider, ai_model,
                            resize_images, image_target_size, image_background_color,
                            is_active, total_products,
                            created_at, created_by_user_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1200, 'white', 1, 0, ?, ?)
                    """, (
                        'Sexoptovik', 'sexoptovik',
                        'Поставщик товаров для взрослых (sexoptovik.ru)',
                        'http://sexoptovik.ru',
                        csv_url, csv_delim, 'cp1251',
                        auth_login, auth_pwd,
                        1 if auto_import['ai_enabled'] else 0 if 'ai_enabled' in auto_import.keys() else 0,
                        ai_provider, ai_model,
                        datetime.utcnow().isoformat(),
                        admin_id
                    ))
                    supplier_id = cursor.lastrowid
                    logger.info(f"Создан поставщик Sexoptovik (id={supplier_id})")

                    # Связываем всех продавцов с настройками sexoptovik
                    cursor.execute("""
                        SELECT seller_id, supplier_code, vendor_code_pattern
                        FROM auto_import_settings
                        WHERE csv_source_type = 'sexoptovik' OR csv_source_url LIKE '%sexoptovik%'
                    """)
                    sellers_to_connect = cursor.fetchall()

                    for seller_row in sellers_to_connect:
                        cursor.execute("""
                            INSERT OR IGNORE INTO seller_suppliers
                                (seller_id, supplier_id, is_active, supplier_code, vendor_code_pattern, connected_at)
                            VALUES (?, ?, 1, ?, ?, ?)
                        """, (
                            seller_row['seller_id'],
                            supplier_id,
                            seller_row['supplier_code'],
                            seller_row['vendor_code_pattern'],
                            datetime.utcnow().isoformat()
                        ))
                        logger.info(f"  Подключён продавец seller_id={seller_row['seller_id']}")

                    # Обновляем supplier_id в imported_products
                    cursor.execute("""
                        UPDATE imported_products
                        SET supplier_id = ?
                        WHERE source_type = 'sexoptovik'
                    """, (supplier_id,))
                    updated_count = cursor.rowcount
                    logger.info(f"  Обновлено {updated_count} imported_products с supplier_id")

                    # Обновляем total_products
                    cursor.execute("SELECT COUNT(*) as cnt FROM imported_products WHERE supplier_id = ?", (supplier_id,))
                    cnt = cursor.fetchone()['cnt']
                    cursor.execute("UPDATE suppliers SET total_products = ? WHERE id = ?", (cnt, supplier_id))
                else:
                    logger.info("Настройки sexoptovik не найдены — пропускаем миграцию данных")
            else:
                logger.info("Таблица auto_import_settings не существует — пропускаем миграцию данных")
        else:
            logger.info(f"Поставщик sexoptovik уже существует (id={existing_supplier['id']})")

        conn.commit()
        logger.info("Миграция завершена успешно!")

    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка миграции: {e}")
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    run_migration()
