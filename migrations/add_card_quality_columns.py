# -*- coding: utf-8 -*-
"""
Миграция: колонки качества карточки в таблицу products.

Добавляет: wb_feedback_rating (оценка по отзывам WB, 0-5),
nm_rating_checked_at (когда обновлён WB-рейтинг),
quality_score (наш Quality Score 0-100),
quality_breakdown_json (разбивка по измерениям),
quality_checked_at (когда пересчитан Quality Score).

Запуск:
    python migrations/add_card_quality_columns.py
    docker exec seller-platform python /app/migrations/add_card_quality_columns.py
"""

import sqlite3
import os


COLUMNS = [
    ('wb_feedback_rating', 'REAL'),
    ('nm_rating_checked_at', 'DATETIME'),
    ('quality_score', 'REAL'),
    ('quality_breakdown_json', 'TEXT'),
    ('quality_checked_at', 'DATETIME'),
]


def get_db_path():
    paths = [
        'data/seller_platform.db',
        '/app/data/seller_platform.db',
        os.path.join(os.path.dirname(__file__), '..', 'data', 'seller_platform.db'),
    ]
    for path in paths:
        if os.path.exists(path):
            print(f"Found database at: {path}")
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
    cursor.execute('PRAGMA table_info(products)')
    existing = [row[1] for row in cursor.fetchall()]

    added = 0
    for col_name, col_type in COLUMNS:
        if col_name not in existing:
            try:
                cursor.execute(f'ALTER TABLE products ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')
        else:
            print(f'  = Column {col_name} already exists')

    conn.commit()
    conn.close()
    print(f'\nMigration completed! Added {added} new columns.')
    return True


if __name__ == '__main__':
    import sys
    migrate(sys.argv[1] if len(sys.argv) > 1 else None)
