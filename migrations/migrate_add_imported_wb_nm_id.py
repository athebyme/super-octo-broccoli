# -*- coding: utf-8 -*-
"""
Миграция: колонка wb_nm_id в imported_products + backfill из products.nm_id.

Код (auto_import_manager, wb_product_importer, upload_readiness_validator,
шаблон истории загрузок) давно пишет и читает imported_products.wb_nm_id,
но колонки в схеме не было: присвоения молча терялись, SQL-фильтры падали
в перехваченный AttributeError — дубль-детект по баркодам не работал.

Backfill: для уже загруженных на WB товаров nmID восстанавливается из
связанного products.nm_id (по product_id).

Запуск:
    python migrations/migrate_add_imported_wb_nm_id.py [путь_к_БД]
"""
import os
import sqlite3
import sys


def get_db_path():
    paths = [
        'data/seller_platform.db',
        '../data/seller_platform.db',
        '/app/data/seller_platform.db',
        os.path.join(os.path.dirname(__file__), '..', 'data', 'seller_platform.db'),
    ]
    for path in paths:
        if os.path.exists(path):
            return path
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('sqlite:///'):
        db_path = db_url.replace('sqlite:///', '')
        if os.path.exists(db_path):
            return db_path
    return 'data/seller_platform.db'


def migrate(db_path=None):
    if db_path is None:
        db_path = get_db_path()
    print(f"Using database: {db_path}")

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute('PRAGMA table_info(imported_products)')
        existing_columns = [row[1] for row in cursor.fetchall()]
        if not existing_columns:
            print('Таблица imported_products не найдена — пропуск')
            return True

        if 'wb_nm_id' not in existing_columns:
            cursor.execute('ALTER TABLE imported_products ADD COLUMN wb_nm_id INTEGER')
            print('  + Добавлена колонка: wb_nm_id')

        cursor.execute(
            'CREATE INDEX IF NOT EXISTS ix_imported_products_wb_nm_id '
            'ON imported_products(wb_nm_id)'
        )

        # Backfill: nmID уже загруженных товаров из связанного Product
        cursor.execute(
            """
            UPDATE imported_products
            SET wb_nm_id = (
                SELECT nm_id FROM products
                WHERE products.id = imported_products.product_id
            )
            WHERE wb_nm_id IS NULL
              AND product_id IS NOT NULL
              AND import_status = 'imported'
            """
        )
        print(f'  ~ Backfill wb_nm_id из products.nm_id: {cursor.rowcount} строк')

        conn.commit()
        print('Миграция wb_nm_id завершена')
        return True
    except Exception as e:
        conn.rollback()
        print(f'Ошибка миграции: {e}')
        return False
    finally:
        conn.close()


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else None
    ok = migrate(path)
    sys.exit(0 if ok else 1)
