#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: Добавление поставщика sex-opt.ru (Andrey)

Создаёт запись Supplier для sex-opt.ru с csv_column_mapping для header-based CSV.
Это ОТДЕЛЬНЫЙ поставщик от sexoptovik — формат CSV с заголовками, коды вида 0T-00000877.
Идемпотентная — безопасно запускать повторно.
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

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_ROOT = BASE_DIR / 'data'
DEFAULT_DB_PATH = DATA_ROOT / 'seller_platform.db'

SUPPLIER_CODE = 'andrey'
SUPPLIER_NAME = 'Андрей (sex-opt.ru)'

SEXOPT_CSV_COLUMN_MAPPING = {
    # Идентификация
    "external_id": {"column": "code", "type": "string"},
    "vendor_code": {"column": "article", "type": "string"},
    "title": {"column": "title", "type": "string"},

    # Бренд и категории
    "brand": {"column": "brand_title", "type": "string"},
    "categories": {"column": "category_title", "type": "list", "separator": "/"},

    # Описание и страна
    "description": {"column": "description", "type": "string"},
    "country": {"column": "country", "type": "string"},

    # Цены
    "supplier_price": {"column": "price", "type": "number"},
    "recommended_retail_price": {"column": "retail_price", "type": "number"},

    # Характеристики товара
    "colors": {"column": "color", "type": "list", "separator": ","},
    "materials": {"column": "material", "type": "list", "separator": ","},
    "sizes_raw": {"column": "size", "type": "string"},
    "barcodes": {"column": "barcodes", "type": "list", "separator": ","},

    # Остатки по складам (суммируются)
    "supplier_quantity": {
        "columns": ["msk", "spb", "tmn", "rst", "nsk", "ast"],
        "type": "stock_sum"
    },

    # Фото (прямые URL из нескольких колонок)
    "photo_urls": {
        "columns": ["image", "image1", "image2"],
        "type": "photo_urls"
    },

    # Физические характеристики
    "characteristics": {
        "columns": {
            "length": "Длина, см",
            "width": "Ширина, см",
            "weight": "Вес, кг",
            "battery": "Тип батареек",
            "waterproof": "Водонепроницаемость"
        },
        "type": "characteristics"
    },

    # Габариты упаковки
    "dimensions": {
        "columns": {
            "width_packed": "Ширина упаковки, см",
            "height_packed": "Высота упаковки, см",
            "length_packed": "Длина упаковки, см",
            "weight_packed": "Вес упаковки, кг"
        },
        "type": "characteristics"
    },
}


def get_db_path():
    if len(sys.argv) > 1:
        return sys.argv[1]
    database_url = os.environ.get('DATABASE_URL')
    if database_url and database_url.startswith('sqlite:///'):
        return database_url.replace('sqlite:///', '')
    return str(DEFAULT_DB_PATH)


def run_migration():
    db_path = get_db_path()
    logger.info(f"Миграция: добавление поставщика {SUPPLIER_NAME} | БД: {db_path}")

    if not os.path.exists(db_path):
        logger.warning(f"База данных не найдена: {db_path}. Пропускаем.")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # Проверяем что таблица suppliers существует
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='suppliers'")
        if not cursor.fetchone():
            logger.info("Таблица suppliers не существует — пропускаем")
            return

        # Добавляем колонки если отсутствуют (для старых БД)
        cursor.execute("PRAGMA table_info(suppliers)")
        columns = [col['name'] for col in cursor.fetchall()]

        if 'csv_has_header' not in columns:
            logger.info("Добавляю колонку csv_has_header...")
            cursor.execute("ALTER TABLE suppliers ADD COLUMN csv_has_header BOOLEAN DEFAULT 0")

        if 'csv_column_mapping' not in columns:
            logger.info("Добавляю колонку csv_column_mapping...")
            cursor.execute("ALTER TABLE suppliers ADD COLUMN csv_column_mapping TEXT")

        if 'price_file_url' not in columns:
            logger.info("Добавляю колонку price_file_url...")
            cursor.execute("ALTER TABLE suppliers ADD COLUMN price_file_url VARCHAR(500)")

        if 'price_file_inf_url' not in columns:
            logger.info("Добавляю колонку price_file_inf_url...")
            cursor.execute("ALTER TABLE suppliers ADD COLUMN price_file_inf_url VARCHAR(500)")

        if 'price_file_delimiter' not in columns:
            logger.info("Добавляю колонку price_file_delimiter...")
            cursor.execute("ALTER TABLE suppliers ADD COLUMN price_file_delimiter VARCHAR(5) DEFAULT ';'")

        if 'price_file_encoding' not in columns:
            logger.info("Добавляю колонку price_file_encoding...")
            cursor.execute("ALTER TABLE suppliers ADD COLUMN price_file_encoding VARCHAR(20) DEFAULT 'cp1251'")

        # Проверяем, есть ли уже поставщик
        cursor.execute("SELECT id FROM suppliers WHERE code = ?", (SUPPLIER_CODE,))
        existing = cursor.fetchone()

        csv_source_url = (
            "https://old.sex-opt.ru/catalogue/db_export/"
            "?type=csv"
            "&user=romantiki25@yandex.ru"
            "&hash=d1482b6450a8e8a59cddf7921dac1d65547770d4ee576dfb07e2cb1d15c11ef6"
            "&columns_separator=%3B"
            "&encoding=utf-8"
        )
        column_mapping_json = json.dumps(SEXOPT_CSV_COLUMN_MAPPING, ensure_ascii=False)

        if existing:
            supplier_id = existing['id']
            logger.info(f"Поставщик '{SUPPLIER_CODE}' уже существует (id={supplier_id}). Обновляю конфигурацию...")
            cursor.execute("""
                UPDATE suppliers SET
                    name = ?,
                    description = 'Оптовый поставщик товаров (sex-opt.ru). CSV с заголовками, коды вида 0T-00000877.',
                    website = 'https://old.sex-opt.ru',
                    csv_source_url = ?,
                    csv_delimiter = ';',
                    csv_encoding = 'utf-8',
                    csv_has_header = 1,
                    csv_column_mapping = ?,
                    resize_images = 1,
                    image_target_size = 1200,
                    image_background_color = 'white',
                    updated_at = ?
                WHERE id = ?
            """, (SUPPLIER_NAME, csv_source_url, column_mapping_json,
                  datetime.utcnow().isoformat(), supplier_id))
            logger.info(f"Конфигурация обновлена")
        else:
            # Получаем admin user id
            cursor.execute("SELECT id FROM users WHERE is_admin = 1 LIMIT 1")
            admin_row = cursor.fetchone()
            admin_id = admin_row['id'] if admin_row else None

            cursor.execute("""
                INSERT INTO suppliers (
                    name, code, description, website,
                    csv_source_url, csv_delimiter, csv_encoding, csv_has_header, csv_column_mapping,
                    resize_images, image_target_size, image_background_color,
                    ai_enabled,
                    is_active, auto_sync_prices, total_products,
                    created_at, created_by_user_id
                ) VALUES (
                    ?, ?,
                    'Оптовый поставщик товаров (sex-opt.ru). CSV с заголовками, коды вида 0T-00000877.',
                    'https://old.sex-opt.ru',
                    ?, ';', 'utf-8', 1, ?,
                    1, 1200, 'white',
                    0,
                    1, 0, 0,
                    ?, ?
                )
            """, (SUPPLIER_NAME, SUPPLIER_CODE, csv_source_url, column_mapping_json,
                  datetime.utcnow().isoformat(), admin_id))
            supplier_id = cursor.lastrowid
            logger.info(f"Создан поставщик {SUPPLIER_NAME} (id={supplier_id})")

        conn.commit()
        logger.info(f"Миграция завершена (supplier_id={supplier_id})")

    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка миграции: {e}")
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    run_migration()
