#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: полный приём фида поставщика andrey (sex-opt.ru).

1. supplier_products.barcodes_json TEXT — полный список штрихкодов из фида
   (раньше сохранялся только первый в barcode).
2. imported_products.recommended_retail_price FLOAT — РРЦ поставщика,
   раньше терялась при импорте продавцу.
3. Обновляет suppliers.csv_column_mapping для code='andrey':
   - категории из category_new_title (заполнена 100%) + старая category_title;
   - РРЦ-fallback из retail_price_minsk (фактическая розничная цена, 99.8%);
   - video → video_url, полный список barcodes, склад kdr в сумме остатков;
   - производитель как характеристика;
   - _include_unmapped: несмаппленные колонки фида сохраняются
     в original_data_json.raw_extra и больше не теряются.

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
DEFAULT_DB_PATH = BASE_DIR / 'data' / 'seller_platform.db'

SUPPLIER_CODE = 'andrey'

ANDREY_CSV_COLUMN_MAPPING = {
    # Идентификация
    "external_id": {"column": "code", "type": "string"},
    "vendor_code": {"column": "article", "type": "string"},
    "title": {"column": "title", "type": "string"},

    # Бренд и категории. Новое дерево категорий заполнено на 100%,
    # старое — на 19%; list с несколькими колонками объединяет цепочки,
    # первая колонка идёт первой (category = её первый элемент).
    "brand": {"column": "brand_title", "type": "string"},
    "categories": {"columns": ["category_new_title", "category_title"],
                   "type": "list", "separator": "/"},

    # Описание и страна
    "description": {"column": "description", "type": "string"},
    "country": {"column": "country", "type": "string"},

    # Цены. retail_price заполнена у ~11%, retail_price_minsk — у ~99.8%
    # и по перекрытию совпадает с РРЦ (медиана ×1.1); используется как fallback.
    "supplier_price": {"column": "price", "type": "number"},
    "recommended_retail_price": {"column": "retail_price", "type": "number"},
    "recommended_retail_price_fallback": {"column": "retail_price_minsk", "type": "number"},

    # Видео (embed-ссылка поставщика)
    "video_url": {"column": "video", "type": "string"},

    # Характеристики товара
    "colors": {"column": "color", "type": "list", "separator": ","},
    "materials": {"column": "material", "type": "list", "separator": ","},
    "sizes_raw": {"column": "size", "type": "string"},
    "barcodes": {"column": "barcodes", "type": "list", "separator": ","},

    # Остатки по складам (суммируются)
    "supplier_quantity": {
        "columns": ["msk", "spb", "tmn", "rst", "nsk", "ast", "kdr"],
        "type": "stock_sum"
    },

    # Фото (прямые URL). image/image1/image2 содержат максимум 3 фото,
    # полный список лежит в колонке images через запятую (до 28 URL) —
    # дубликаты между колонками парсер отбрасывает.
    "photo_urls": {
        "columns": ["image", "image1", "image2", "images"],
        "columns_prefix": "image",
        "separator": ",",
        "type": "photo_urls"
    },

    # Физические характеристики
    "characteristics": {
        "columns": {
            "length": "Длина, см",
            "width": "Ширина, см",
            "weight": "Вес, кг",
            "battery": "Тип батареек",
            "waterproof": "Водонепроницаемость",
            "manufacturer": "Производитель"
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

    # Несмаппленные колонки фида (manufacturer/marked/start_price/url/
    # modification_code/group_title и др.) сохраняются в raw_extra
    "_include_unmapped": True,
}


def get_db_path():
    if len(sys.argv) > 1:
        return sys.argv[1]
    database_url = os.environ.get('DATABASE_URL')
    if database_url and database_url.startswith('sqlite:///'):
        return database_url.replace('sqlite:///', '')
    return str(DEFAULT_DB_PATH)


def _add_column_if_missing(cursor, table: str, column: str, ddl_type: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column in existing:
        return False
    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
    logger.info(f"Добавлена колонка {table}.{column}")
    return True


def run_migration():
    db_path = get_db_path()
    logger.info(f"Миграция: полный приём фида andrey | БД: {db_path}")

    if not os.path.exists(db_path):
        logger.warning(f"База данных не найдена: {db_path}. Пропускаем.")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # Колонки (только если таблицы существуют)
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('supplier_products', 'imported_products', 'suppliers')"
        )
        tables = {row['name'] for row in cursor.fetchall()}

        if 'supplier_products' in tables:
            _add_column_if_missing(cursor, 'supplier_products', 'barcodes_json', 'TEXT')
        if 'imported_products' in tables:
            _add_column_if_missing(cursor, 'imported_products', 'recommended_retail_price', 'FLOAT')

        # Обновление маппинга поставщика andrey
        if 'suppliers' in tables:
            cursor.execute("SELECT id FROM suppliers WHERE code = ?", (SUPPLIER_CODE,))
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "UPDATE suppliers SET csv_column_mapping = ?, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(ANDREY_CSV_COLUMN_MAPPING, ensure_ascii=False),
                        datetime.utcnow().isoformat(),
                        row['id'],
                    ),
                )
                logger.info(f"Обновлён csv_column_mapping поставщика '{SUPPLIER_CODE}' (id={row['id']})")
            else:
                logger.info(f"Поставщик '{SUPPLIER_CODE}' не найден — маппинг не обновлялся")

        conn.commit()
        logger.info("Миграция завершена")
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка миграции: {e}")
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    run_migration()
