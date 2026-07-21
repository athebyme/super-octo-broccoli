#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Одноразовая чистка мусора competitor_price_snapshots (v1 накопил ~10M строк
на 2 товара: fetch-miss писался как «изменение на None» каждый цикл).

Удаляет чанками: (1) all-NULL снимки, (2) подряд идущие дубликаты per product.
Бюджет времени на прогон — 60с: недочищенный хвост доберёт регулярная
чанковая компакция scheduler-джоба. Идемпотентная. Место на диске вернёт
только последующий ручной VACUUM (не выполняется здесь: БД многогигабайтная).
"""
import logging
import sqlite3
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / 'data' / 'seller_platform.db'
CHUNK = 20_000


def migrate(db_path, max_seconds=60):
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA busy_timeout = 30000')
    started = time.time()
    deleted_null = 0
    deleted_dup = 0
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'competitor_price_snapshots' not in tables:
            logger.info('Таблица снимков отсутствует — чистка не требуется')
            return True

        def out_of_time():
            return (time.time() - started) > max_seconds

        while not out_of_time():
            cur = conn.execute("""
                DELETE FROM competitor_price_snapshots WHERE id IN (
                    SELECT id FROM competitor_price_snapshots
                    WHERE price IS NULL AND sale_price IS NULL
                      AND total_stock IS NULL AND rating IS NULL
                    LIMIT ?)""", (CHUNK,))
            conn.commit()
            deleted_null += cur.rowcount
            if cur.rowcount < CHUNK:
                break

        while not out_of_time():
            cur = conn.execute("""
                DELETE FROM competitor_price_snapshots WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               price IS LAG(price) OVER w
                               AND sale_price IS LAG(sale_price) OVER w
                               AND total_stock IS LAG(total_stock) OVER w
                               AND rating IS LAG(rating) OVER w AS is_dup
                        FROM competitor_price_snapshots
                        WINDOW w AS (PARTITION BY product_id
                                     ORDER BY created_at, id)
                    ) WHERE is_dup LIMIT ?)""", (CHUNK,))
            conn.commit()
            deleted_dup += cur.rowcount
            if cur.rowcount < CHUNK:
                break

        logger.info('Чистка снимков: удалено %s all-NULL, %s дублей '
                    '(бюджет %sс, потрачено %.1fс)',
                    deleted_null, deleted_dup, max_seconds,
                    time.time() - started)
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_DB_PATH)
    if not Path(db_path).exists():
        logger.error('БД не найдена: %s', db_path)
        return 1
    return 0 if migrate(db_path) else 1


if __name__ == '__main__':
    sys.exit(main())
