"""
Миграция для добавления AI колонок в таблицу auto_import_settings

Запуск:
    python migrations/add_ai_columns.py

Или через Flask shell:
    flask shell
    >>> exec(open('migrations/add_ai_columns.py').read())
"""

import sqlite3
import os

def get_db_path():
    """Получить путь к базе данных"""
    # Попробуем разные варианты
    paths = [
        'instance/app.db',
        '../instance/app.db',
        'app.db',
        os.path.join(os.path.dirname(__file__), '..', 'instance', 'app.db'),
    ]

    for path in paths:
        if os.path.exists(path):
            return path

    # Если не нашли, попробуем из конфига Flask
    try:
        from app import create_app
        app = create_app()
        db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        if db_uri.startswith('sqlite:///'):
            return db_uri.replace('sqlite:///', '')
    except:
        pass

    return 'instance/app.db'


def migrate():
    """Выполнить миграцию"""
    db_path = get_db_path()
    print(f"Using database: {db_path}")

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        print("Please specify correct path or run this script from the project root directory")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Получаем текущие колонки
    cursor.execute('PRAGMA table_info(auto_import_settings)')
    existing_columns = [row[1] for row in cursor.fetchall()]
    print(f'Existing columns: {len(existing_columns)}')

    # Новые колонки для добавления
    new_columns = [
        ('ai_enabled', 'BOOLEAN DEFAULT 0'),
        ('ai_provider', "VARCHAR(50) DEFAULT 'openai'"),
        ('ai_api_key', 'VARCHAR(500)'),
        ('ai_api_base_url', 'VARCHAR(500)'),
        ('ai_model', "VARCHAR(100) DEFAULT 'gpt-4o-mini'"),
        ('ai_temperature', 'FLOAT DEFAULT 0.3'),
        ('ai_max_tokens', 'INTEGER DEFAULT 2000'),
        ('ai_timeout', 'INTEGER DEFAULT 60'),
        ('ai_use_for_categories', 'BOOLEAN DEFAULT 1'),
        ('ai_use_for_sizes', 'BOOLEAN DEFAULT 1'),
        ('ai_category_confidence_threshold', 'FLOAT DEFAULT 0.7'),
        ('ai_top_p', 'FLOAT DEFAULT 0.95'),
        ('ai_presence_penalty', 'FLOAT DEFAULT 0.0'),
        ('ai_frequency_penalty', 'FLOAT DEFAULT 0.0'),
        ('ai_category_instruction', 'TEXT'),
        ('ai_size_instruction', 'TEXT'),
    ]

    added = 0
    for col_name, col_type in new_columns:
        if col_name not in existing_columns:
            try:
                cursor.execute(f'ALTER TABLE auto_import_settings ADD COLUMN {col_name} {col_type}')
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
    migrate()
