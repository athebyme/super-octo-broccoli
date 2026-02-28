#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: Добавление AI парсера данных поставщиков

Добавляет:
- suppliers: description_file_url, description_file_delimiter, description_file_encoding,
  last_description_sync_at, last_description_sync_status, ai_parsing_instruction
- supplier_products: ai_parsed_data_json, ai_parsed_at, ai_marketplace_json, description_source
"""
import os
import sys
import sqlite3
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


def column_exists(cursor, table_name, column_name):
    """Проверяет существование столбца в таблице"""
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    return column_name in columns


def run_migration():
    db_path = get_db_path()
    logger.info(f"Миграция AI парсера: {db_path}")

    if not os.path.exists(db_path):
        logger.warning(f"База данных не найдена: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # ============================================================
        # Таблица suppliers: новые поля для описаний и AI парсинга
        # ============================================================
        supplier_columns = {
            'description_file_url': "VARCHAR(500)",
            'description_file_delimiter': "VARCHAR(5) DEFAULT ';'",
            'description_file_encoding': "VARCHAR(20) DEFAULT 'cp1251'",
            'last_description_sync_at': "DATETIME",
            'last_description_sync_status': "VARCHAR(50)",
            'ai_parsing_instruction': "TEXT",
        }

        for col_name, col_type in supplier_columns.items():
            if not column_exists(cursor, 'suppliers', col_name):
                cursor.execute(f"ALTER TABLE suppliers ADD COLUMN {col_name} {col_type}")
                logger.info(f"  + suppliers.{col_name}")
            else:
                logger.info(f"  = suppliers.{col_name} (уже есть)")

        # ============================================================
        # Таблица supplier_products: поля AI парсинга
        # ============================================================
        product_columns = {
            'ai_parsed_data_json': "TEXT",
            'ai_parsed_at': "DATETIME",
            'ai_marketplace_json': "TEXT",
            'description_source': "VARCHAR(50)",
        }

        for col_name, col_type in product_columns.items():
            if not column_exists(cursor, 'supplier_products', col_name):
                cursor.execute(f"ALTER TABLE supplier_products ADD COLUMN {col_name} {col_type}")
                logger.info(f"  + supplier_products.{col_name}")
            else:
                logger.info(f"  = supplier_products.{col_name} (уже есть)")

        conn.commit()
        logger.info("Миграция AI парсера завершена успешно")

    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка миграции: {e}")
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    run_migration()
