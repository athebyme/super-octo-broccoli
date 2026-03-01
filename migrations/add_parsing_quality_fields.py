# -*- coding: utf-8 -*-
"""
Миграция: Добавление полей для улучшения качества парсинга.

Новые поля:
- Supplier: csv_column_mapping (JSON), csv_has_header (Boolean)
- SupplierProduct: parsing_confidence (Float), normalization_applied (Boolean)
- ParsingLog: новая таблица для метрик парсинга
"""
import sqlite3
import logging
import os

logger = logging.getLogger(__name__)


def run_migration(db_path: str = None):
    """Запустить миграцию."""
    if db_path is None:
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'seller_platform.db')

    if not os.path.exists(db_path):
        logger.warning(f"Database not found at {db_path}, skipping migration")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    migrations = [
        # Supplier: конфигурируемый маппинг колонок
        ("ALTER TABLE suppliers ADD COLUMN csv_column_mapping TEXT", "suppliers.csv_column_mapping"),
        ("ALTER TABLE suppliers ADD COLUMN csv_has_header BOOLEAN DEFAULT 0", "suppliers.csv_has_header"),

        # SupplierProduct: качество парсинга
        ("ALTER TABLE supplier_products ADD COLUMN parsing_confidence REAL", "supplier_products.parsing_confidence"),
        ("ALTER TABLE supplier_products ADD COLUMN normalization_applied BOOLEAN DEFAULT 0", "supplier_products.normalization_applied"),

        # ParsingLog: таблица метрик
        ("""
        CREATE TABLE IF NOT EXISTS parsing_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
            event_type VARCHAR(50) NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            total_products INTEGER DEFAULT 0,
            processed_successfully INTEGER DEFAULT 0,
            errors_count INTEGER DEFAULT 0,
            duration_seconds REAL,
            field_fill_rates TEXT,
            ai_tokens_used INTEGER,
            ai_cache_hits INTEGER DEFAULT 0,
            ai_cache_misses INTEGER DEFAULT 0,
            errors_json TEXT,
            normalization_stats TEXT
        )
        """, "parsing_logs table"),

        # Индекс для быстрого поиска логов
        ("""
        CREATE INDEX IF NOT EXISTS idx_parsing_logs_supplier
        ON parsing_logs(supplier_id, created_at DESC)
        """, "idx_parsing_logs_supplier"),

        # Индекс для быстрого поиска товаров по confidence
        ("""
        CREATE INDEX IF NOT EXISTS idx_supplier_product_confidence
        ON supplier_products(supplier_id, parsing_confidence)
        """, "idx_supplier_product_confidence"),
    ]

    for sql, description in migrations:
        try:
            cursor.execute(sql)
            logger.info(f"Migration applied: {description}")
        except sqlite3.OperationalError as e:
            if 'duplicate column name' in str(e).lower() or 'already exists' in str(e).lower():
                logger.debug(f"Already exists: {description}")
            else:
                logger.error(f"Migration error ({description}): {e}")

    conn.commit()
    conn.close()
    logger.info("Parsing quality migration complete")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_migration()
