"""
Миграция для добавления колонок AI инструкций и расширения AIHistory

Добавляет:
1. Дополнительные AI инструкции в auto_import_settings
2. Расширенные поля в ai_history для полного логирования

Запуск:
    python migrations/add_ai_instructions_columns.py

Или через Flask shell:
    flask shell
    >>> exec(open('migrations/add_ai_instructions_columns.py').read())
"""

import sqlite3
import os


def get_db_path():
    """Получить путь к базе данных"""
    paths = [
        'data/seller_platform.db',
        'seller_platform.db',
        '../data/seller_platform.db',
        '/app/data/seller_platform.db',
        'instance/app.db',
        '../instance/app.db',
        'app.db',
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

    try:
        from seller_platform import app
        db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        if db_uri.startswith('sqlite:///'):
            return db_uri.replace('sqlite:///', '')
    except:
        pass

    return 'data/seller_platform.db'


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

    # =====================================
    # Миграция auto_import_settings
    # =====================================
    print("\n=== Migrating auto_import_settings ===")
    cursor.execute('PRAGMA table_info(auto_import_settings)')
    existing_columns = [row[1] for row in cursor.fetchall()]
    print(f'Existing columns in auto_import_settings: {len(existing_columns)}')

    # Новые колонки AI инструкций
    new_settings_columns = [
        ('ai_seo_title_instruction', 'TEXT'),
        ('ai_keywords_instruction', 'TEXT'),
        ('ai_bullets_instruction', 'TEXT'),
        ('ai_description_instruction', 'TEXT'),
        ('ai_rich_content_instruction', 'TEXT'),
        ('ai_analysis_instruction', 'TEXT'),
    ]

    added = 0
    for col_name, col_type in new_settings_columns:
        if col_name not in existing_columns:
            try:
                cursor.execute(f'ALTER TABLE auto_import_settings ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')
        else:
            print(f'  = Column {col_name} already exists')

    print(f'Added {added} columns to auto_import_settings')

    # =====================================
    # Миграция ai_history
    # =====================================
    print("\n=== Migrating ai_history ===")
    cursor.execute('PRAGMA table_info(ai_history)')
    ai_history_columns = [row[1] for row in cursor.fetchall()]
    print(f'Existing columns in ai_history: {len(ai_history_columns)}')

    # Новые колонки для расширенного логирования
    new_history_columns = [
        ('ai_provider', 'VARCHAR(50)'),
        ('ai_model', 'VARCHAR(100)'),
        ('system_prompt', 'TEXT'),
        ('user_prompt', 'TEXT'),
        ('raw_response', 'TEXT'),
        ('tokens_prompt', 'INTEGER DEFAULT 0'),
        ('tokens_completion', 'INTEGER DEFAULT 0'),
        ('response_time_ms', 'INTEGER DEFAULT 0'),
        ('source_module', 'VARCHAR(100)'),
    ]

    history_added = 0
    for col_name, col_type in new_history_columns:
        if col_name not in ai_history_columns:
            try:
                cursor.execute(f'ALTER TABLE ai_history ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                history_added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')
        else:
            print(f'  = Column {col_name} already exists')

    print(f'Added {history_added} columns to ai_history')

    # Добавляем индекс на created_at если его нет
    try:
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_history_created ON ai_history(created_at)')
        print('  + Created index idx_ai_history_created')
    except sqlite3.OperationalError as e:
        print(f'  ! Index creation skipped: {e}')

    conn.commit()
    conn.close()

    print(f'\nMigration completed!')
    print(f'  - auto_import_settings: {added} new columns')
    print(f'  - ai_history: {history_added} new columns')
    return True


def run_migration_with_path(db_path):
    """Запускает миграцию с указанным путем к БД"""
    print(f"Using provided path: {db_path}")

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # auto_import_settings
    print("\n=== Migrating auto_import_settings ===")
    cursor.execute('PRAGMA table_info(auto_import_settings)')
    existing_columns = [row[1] for row in cursor.fetchall()]

    new_settings_columns = [
        ('ai_seo_title_instruction', 'TEXT'),
        ('ai_keywords_instruction', 'TEXT'),
        ('ai_bullets_instruction', 'TEXT'),
        ('ai_description_instruction', 'TEXT'),
        ('ai_rich_content_instruction', 'TEXT'),
        ('ai_analysis_instruction', 'TEXT'),
    ]

    added = 0
    for col_name, col_type in new_settings_columns:
        if col_name not in existing_columns:
            try:
                cursor.execute(f'ALTER TABLE auto_import_settings ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')

    # ai_history
    print("\n=== Migrating ai_history ===")
    cursor.execute('PRAGMA table_info(ai_history)')
    ai_history_columns = [row[1] for row in cursor.fetchall()]

    new_history_columns = [
        ('ai_provider', 'VARCHAR(50)'),
        ('ai_model', 'VARCHAR(100)'),
        ('system_prompt', 'TEXT'),
        ('user_prompt', 'TEXT'),
        ('raw_response', 'TEXT'),
        ('tokens_prompt', 'INTEGER DEFAULT 0'),
        ('tokens_completion', 'INTEGER DEFAULT 0'),
        ('response_time_ms', 'INTEGER DEFAULT 0'),
        ('source_module', 'VARCHAR(100)'),
    ]

    history_added = 0
    for col_name, col_type in new_history_columns:
        if col_name not in ai_history_columns:
            try:
                cursor.execute(f'ALTER TABLE ai_history ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                history_added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')

    try:
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_history_created ON ai_history(created_at)')
    except:
        pass

    conn.commit()
    conn.close()
    print(f'\nMigration completed! Settings: {added}, History: {history_added}')
    return True


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1:
        db_path = sys.argv[1]
        success = run_migration_with_path(db_path)
        sys.exit(0 if success else 1)
    else:
        migrate()
