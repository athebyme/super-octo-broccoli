# -*- coding: utf-8 -*-
"""
Миграция: Добавление полей external_id_pattern и default_vendor_code_pattern
в таблицу suppliers для настраиваемого формирования артикулов по поставщику.
"""
import sqlite3
import logging
import os

logger = logging.getLogger(__name__)


def run_migration(db_path: str = None):
    """Запустить миграцию."""
    if db_path is None:
        base_dir = os.path.dirname(os.path.dirname(__file__))
        candidates = [
            os.path.join(base_dir, 'data', 'seller_platform.db'),
            os.path.join(base_dir, 'instance', 'seller_platform.db'),
            os.environ.get('DATABASE_URL', '').replace('sqlite:////', '/').replace('sqlite:///', ''),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                db_path = path
                break

    if not db_path or not os.path.exists(db_path):
        logger.warning("БД не найдена, пропускаем миграцию add_supplier_vendor_code_fields")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='suppliers'")
        if not cursor.fetchone():
            logger.info("Таблица suppliers не существует, пропускаем")
            return False

        cursor.execute("PRAGMA table_info(suppliers)")
        columns = {row[1] for row in cursor.fetchall()}

        added = []
        if 'external_id_pattern' not in columns:
            cursor.execute("ALTER TABLE suppliers ADD COLUMN external_id_pattern VARCHAR(300)")
            added.append('external_id_pattern')

        if 'default_vendor_code_pattern' not in columns:
            cursor.execute("ALTER TABLE suppliers ADD COLUMN default_vendor_code_pattern VARCHAR(200)")
            added.append('default_vendor_code_pattern')

        if added:
            conn.commit()
            logger.info(f"Добавлены поля в suppliers: {', '.join(added)}")
        else:
            logger.info("Поля external_id_pattern, default_vendor_code_pattern уже существуют")

        return True
    except Exception as e:
        logger.error(f"Ошибка миграции: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_migration()
