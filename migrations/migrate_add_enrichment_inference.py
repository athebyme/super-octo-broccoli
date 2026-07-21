#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: режим inference-предположений в admin-обогащении каталога.

1. Расширяет CHECK `supplier_catalog_enrichment_runs.mode` значением
   'characteristics_inference' (идемпотентный transactional rebuild —
   SQLite не умеет ALTER CHECK). Все существующие rows сохраняются.
2. Добавляет `supplier_catalog_enrichment_items.inference_json TEXT` —
   предложения модели [{name, value, rationale, confidence}].

Baseline `PRAGMA foreign_key_check` до DDL; после DDL отклоняются только
новые нарушения. Безопасно запускать повторно.
"""
import os
import sys
import sqlite3
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / 'data' / 'seller_platform.db'

NEW_MODE_CHECK = (
    "CHECK (mode IN ('category_only', 'category_and_characteristics', "
    "'characteristics_inference'))"
)

RUNS_DDL = """
CREATE TABLE supplier_catalog_enrichment_runs_new (
    id VARCHAR(36) NOT NULL,
    supplier_id INTEGER NOT NULL,
    admin_user_id INTEGER NOT NULL,
    mode VARCHAR(40) NOT NULL,
    status VARCHAR(24) NOT NULL,
    selection_json TEXT NOT NULL,
    reference_snapshot_json TEXT,
    model_used VARCHAR(100),
    total INTEGER NOT NULL,
    processed INTEGER NOT NULL,
    applied INTEGER NOT NULL,
    unchanged INTEGER NOT NULL,
    needs_review INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    cancelled INTEGER NOT NULL,
    llm_calls INTEGER NOT NULL,
    llm_call_limit INTEGER NOT NULL,
    current_label VARCHAR(300),
    error_code VARCHAR(80),
    error_message TEXT,
    heartbeat_at DATETIME,
    started_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_supplier_catalog_enrichment_run_mode
        CHECK (mode IN ('category_only', 'category_and_characteristics',
                        'characteristics_inference')),
    CONSTRAINT ck_supplier_catalog_enrichment_run_status
        CHECK (status IN ('pending', 'running', 'cancelling', 'cancelled',
                          'completed', 'partial', 'failed')),
    FOREIGN KEY(supplier_id) REFERENCES suppliers (id),
    FOREIGN KEY(admin_user_id) REFERENCES users (id)
)
"""

RUNS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_supplier_catalog_enrichment_runs_supplier_id "
    "ON supplier_catalog_enrichment_runs (supplier_id)",
    "CREATE INDEX IF NOT EXISTS idx_supplier_catalog_enrichment_run_active "
    "ON supplier_catalog_enrichment_runs (supplier_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_supplier_catalog_enrichment_runs_status "
    "ON supplier_catalog_enrichment_runs (status)",
    "CREATE INDEX IF NOT EXISTS ix_supplier_catalog_enrichment_runs_admin_user_id "
    "ON supplier_catalog_enrichment_runs (admin_user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_catalog_enrichment_active_supplier "
    "ON supplier_catalog_enrichment_runs (supplier_id) "
    "WHERE status IN ('pending','running','cancelling')",
    "CREATE INDEX IF NOT EXISTS idx_supplier_catalog_enrichment_run_supplier "
    "ON supplier_catalog_enrichment_runs(supplier_id)",
    "CREATE INDEX IF NOT EXISTS idx_supplier_catalog_enrichment_run_status "
    "ON supplier_catalog_enrichment_runs(status)",
)

RUNS_COLUMNS = (
    'id, supplier_id, admin_user_id, mode, status, selection_json, '
    'reference_snapshot_json, model_used, total, processed, applied, '
    'unchanged, needs_review, failed, cancelled, llm_calls, llm_call_limit, '
    'current_label, error_code, error_message, heartbeat_at, started_at, '
    'completed_at, created_at, updated_at'
)


def get_db_path():
    if len(sys.argv) > 1:
        return sys.argv[1]
    database_url = os.environ.get('DATABASE_URL')
    if database_url and database_url.startswith('sqlite:///'):
        return database_url.replace('sqlite:///', '')
    return str(DEFAULT_DB_PATH)


def _fk_violations(cursor, tables=None):
    cursor.execute('PRAGMA foreign_key_check')
    rows = cursor.fetchall()
    if tables is None:
        return set(map(tuple, rows))
    return {tuple(r) for r in rows if r[0] in tables or r[2] in tables}


def run_migration():
    db_path = get_db_path()
    logger.info(f"Миграция: enrichment inference mode | БД: {db_path}")
    if not os.path.exists(db_path):
        logger.warning(f"База данных не найдена: {db_path}. Пропускаем.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    managed = {'supplier_catalog_enrichment_runs', 'supplier_catalog_enrichment_items'}
    try:
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='supplier_catalog_enrichment_runs'"
        )
        row = cursor.fetchone()
        if not row:
            logger.info("Таблица runs не существует — пропускаем (создаст create_all)")
            return

        baseline = _fk_violations(cursor)

        if 'characteristics_inference' in (row[0] or ''):
            logger.info("CHECK mode уже расширен — rebuild не нужен")
        else:
            cursor.execute('BEGIN IMMEDIATE')
            cursor.execute(RUNS_DDL)
            cursor.execute(
                f"INSERT INTO supplier_catalog_enrichment_runs_new ({RUNS_COLUMNS}) "
                f"SELECT {RUNS_COLUMNS} FROM supplier_catalog_enrichment_runs"
            )
            cursor.execute('DROP TABLE supplier_catalog_enrichment_runs')
            cursor.execute(
                'ALTER TABLE supplier_catalog_enrichment_runs_new '
                'RENAME TO supplier_catalog_enrichment_runs'
            )
            for ddl in RUNS_INDEXES:
                cursor.execute(ddl)
            logger.info("Rebuild runs с расширенным CHECK mode выполнен")

        cursor.execute('PRAGMA table_info(supplier_catalog_enrichment_items)')
        item_columns = {r[1] for r in cursor.fetchall()}
        if item_columns and 'inference_json' not in item_columns:
            cursor.execute(
                'ALTER TABLE supplier_catalog_enrichment_items '
                'ADD COLUMN inference_json TEXT'
            )
            logger.info("Добавлена колонка items.inference_json")

        after = _fk_violations(cursor)
        new_violations = {
            v for v in after - baseline
        } | {
            v for v in after if (v[0] in managed or v[2] in managed)
        }
        if new_violations:
            raise RuntimeError(
                f'FK violations после миграции: {sorted(new_violations)[:5]}'
            )

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
