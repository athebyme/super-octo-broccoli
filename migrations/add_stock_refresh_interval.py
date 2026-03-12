"""
Миграция: добавление колонки stock_refresh_interval в таблицу sellers

Колонка хранит настраиваемый интервал обновления остатков (в минутах, от 5 до 60).
По умолчанию 30 мин — совпадает с частотой обновления данных на стороне WB.

Запуск:
    python migrations/add_stock_refresh_interval.py
"""

import sqlite3
import os


def get_db_path():
    paths = [
        'data/seller_platform.db',
        '../data/seller_platform.db',
        '/app/data/seller_platform.db',
        'instance/app.db',
        '../instance/app.db',
        os.path.join(os.path.dirname(__file__), '..', 'data', 'seller_platform.db'),
        os.path.join(os.path.dirname(__file__), '..', 'instance', 'app.db'),
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

    cursor.execute('PRAGMA table_info(sellers)')
    existing_columns = [row[1] for row in cursor.fetchall()]

    added = 0
    col_name = 'stock_refresh_interval'
    if col_name not in existing_columns:
        try:
            cursor.execute(f'ALTER TABLE sellers ADD COLUMN {col_name} INTEGER DEFAULT 30')
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
    if len(sys.argv) > 1:
        migrate(sys.argv[1])
    else:
        migrate()
