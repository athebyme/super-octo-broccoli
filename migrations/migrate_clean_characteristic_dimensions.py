#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: чистка габаритов упаковки из характеристик общего каталога.

Исторические синки записали в supplier_products.characteristics_json
dimension-образные имена («Длина/Ширина/Высота упаковки, см»,
«Вес упаковки, кг»): в WB это отдельный объект габаритов карточки, а не
характеристики, и строгий schema-валидатор честно блокировал такие патчи
целиком. Скрипт переносит эти записи в dimensions_json (под исходными
именами колонок фида; существующие ключи dimensions_json не перетираются)
и убирает их из characteristics_json.

Staging-копии ImportedProduct.characteristics не трогаются: runtime-фильтр
(partition_supplier_characteristic_input) разводит их на лету, а следующее
«Обновить карточки» копирует уже чистые данные из каталога.

Идемпотентная — безопасно запускать повторно. Повреждённый JSON строки
пропускается без изменений.
"""
import json
import logging
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / 'data' / 'seller_platform.db'

BATCH_SIZE = 500


def _extract(raw_chars: dict):
    """(clean_chars, moved_by_original_name) через общий детектор имён."""
    sys.path.insert(0, str(BASE_DIR))
    from services.wb_content_payload import extract_characteristics

    extraction = extract_characteristics(raw_chars)
    moved = {
        name: raw_chars[name]
        for name in extraction.dropped
        if name in raw_chars
    }
    return extraction.values, moved


def migrate(db_path):
    db_path = str(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        tables = {
            row[0]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if 'supplier_products' not in tables:
            logger.info('supplier_products отсутствует — миграция не требуется')
            return True

        rows = cur.execute(
            """
            SELECT id, characteristics_json, dimensions_json
            FROM supplier_products
            WHERE characteristics_json IS NOT NULL
              AND characteristics_json != ''
              AND (
                    characteristics_json LIKE '%упаков%'
                 OR characteristics_json LIKE '%packed%'
                 OR characteristics_json LIKE '%package%'
              )
            """
        ).fetchall()

        cleaned = 0
        skipped_invalid = 0
        pending = []
        for row in rows:
            try:
                raw_chars = json.loads(row['characteristics_json'])
            except (TypeError, ValueError):
                skipped_invalid += 1
                continue
            if not isinstance(raw_chars, dict):
                continue

            clean_chars, moved = _extract(raw_chars)
            if not moved:
                continue

            dims = {}
            if row['dimensions_json']:
                try:
                    parsed_dims = json.loads(row['dimensions_json'])
                    if isinstance(parsed_dims, dict):
                        dims = parsed_dims
                except (TypeError, ValueError):
                    dims = {}
            # Существующие явные габариты сильнее перенесённых.
            for name, value in moved.items():
                dims.setdefault(name, value)

            pending.append((
                json.dumps(clean_chars, ensure_ascii=False) if clean_chars else None,
                json.dumps(dims, ensure_ascii=False) if dims else None,
                row['id'],
            ))
            cleaned += 1

            if len(pending) >= BATCH_SIZE:
                cur.executemany(
                    'UPDATE supplier_products'
                    ' SET characteristics_json = ?, dimensions_json = ?'
                    ' WHERE id = ?',
                    pending,
                )
                conn.commit()
                pending = []

        if pending:
            cur.executemany(
                'UPDATE supplier_products'
                ' SET characteristics_json = ?, dimensions_json = ?'
                ' WHERE id = ?',
                pending,
            )
            conn.commit()

        logger.info(
            'Габариты из характеристик перенесены: строк изменено %s, '
            'повреждённый JSON пропущен: %s',
            cleaned, skipped_invalid,
        )
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    if not Path(target).exists():
        logger.error('База не найдена: %s', target)
        sys.exit(1)
    ok = migrate(target)
    sys.exit(0 if ok else 1)
