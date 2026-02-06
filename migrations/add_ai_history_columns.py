"""
Миграция для добавления AI колонок в таблицу imported_products
и создания таблицы ai_history

Запуск:
    python migrations/add_ai_history_columns.py

Или через Flask shell:
    flask shell
    >>> exec(open('migrations/add_ai_history_columns.py').read())
"""

import sqlite3
import os


def get_db_path():
    """Получить путь к базе данных"""
    paths = [
        'data/seller_platform.db',
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

    # ============ Миграция imported_products ============
    print("\n=== Migrating imported_products table ===")

    cursor.execute('PRAGMA table_info(imported_products)')
    existing_columns = [row[1] for row in cursor.fetchall()]
    print(f'Existing columns in imported_products: {len(existing_columns)}')

    # Новые колонки для AI-кэширования
    new_columns = [
        ('ai_keywords', 'TEXT'),           # Ключевые слова (JSON)
        ('ai_bullets', 'TEXT'),            # Преимущества (JSON)
        ('ai_rich_content', 'TEXT'),       # Rich контент (JSON)
        ('ai_seo_title', 'VARCHAR(500)'),  # SEO заголовок
        ('ai_analysis', 'TEXT'),           # Анализ карточки (JSON)
        ('ai_analysis_at', 'DATETIME'),    # Когда был сделан анализ
        ('content_hash', 'VARCHAR(64)'),   # Хеш контента для отслеживания изменений
    ]

    added = 0
    for col_name, col_type in new_columns:
        if col_name not in existing_columns:
            try:
                cursor.execute(f'ALTER TABLE imported_products ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')
        else:
            print(f'  = Column {col_name} already exists')

    print(f'Added {added} columns to imported_products')

    # ============ Создание таблицы ai_history ============
    print("\n=== Creating ai_history table ===")

    # Проверяем, существует ли таблица
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ai_history'")
    if cursor.fetchone():
        print('  = Table ai_history already exists')
    else:
        create_table_sql = '''
        CREATE TABLE ai_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            imported_product_id INTEGER,
            action_type VARCHAR(50) NOT NULL,
            input_data TEXT,
            result_data TEXT,
            success BOOLEAN DEFAULT 1,
            error_message TEXT,
            tokens_used INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (seller_id) REFERENCES sellers(id),
            FOREIGN KEY (imported_product_id) REFERENCES imported_products(id)
        )
        '''
        try:
            cursor.execute(create_table_sql)
            print('  + Created table: ai_history')

            # Создаем индексы
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_history_seller_action ON ai_history(seller_id, action_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_history_product_created ON ai_history(imported_product_id, created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_history_created ON ai_history(created_at)')
            print('  + Created indexes for ai_history')
        except sqlite3.OperationalError as e:
            print(f'  ! Error creating ai_history: {e}')

    conn.commit()
    conn.close()

    print(f'\n✓ Migration completed successfully!')
    return True


def run_migration_with_path(db_path):
    """Запускает миграцию с указанным путем к БД"""
    print(f"Using provided path: {db_path}")

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # ============ Миграция imported_products ============
    print("\n=== Migrating imported_products table ===")

    cursor.execute('PRAGMA table_info(imported_products)')
    existing_columns = [row[1] for row in cursor.fetchall()]
    print(f'Existing columns: {len(existing_columns)}')

    new_columns = [
        ('ai_keywords', 'TEXT'),
        ('ai_bullets', 'TEXT'),
        ('ai_rich_content', 'TEXT'),
        ('ai_seo_title', 'VARCHAR(500)'),
        ('ai_analysis', 'TEXT'),
        ('ai_analysis_at', 'DATETIME'),
        ('content_hash', 'VARCHAR(64)'),
    ]

    added = 0
    for col_name, col_type in new_columns:
        if col_name not in existing_columns:
            try:
                cursor.execute(f'ALTER TABLE imported_products ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')
        else:
            print(f'  = Column {col_name} already exists')

    # ============ Создание таблицы ai_history ============
    print("\n=== Creating ai_history table ===")

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ai_history'")
    if cursor.fetchone():
        print('  = Table ai_history already exists')
    else:
        create_table_sql = '''
        CREATE TABLE ai_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            imported_product_id INTEGER,
            action_type VARCHAR(50) NOT NULL,
            input_data TEXT,
            result_data TEXT,
            success BOOLEAN DEFAULT 1,
            error_message TEXT,
            tokens_used INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (seller_id) REFERENCES sellers(id),
            FOREIGN KEY (imported_product_id) REFERENCES imported_products(id)
        )
        '''
        try:
            cursor.execute(create_table_sql)
            print('  + Created table: ai_history')

            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_history_seller_action ON ai_history(seller_id, action_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_history_product_created ON ai_history(imported_product_id, created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_history_created ON ai_history(created_at)')
            print('  + Created indexes for ai_history')
        except sqlite3.OperationalError as e:
            print(f'  ! Error creating ai_history: {e}')

    conn.commit()
    conn.close()
    print(f'\n✓ Migration completed!')
    return True


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1:
        db_path = sys.argv[1]
        success = run_migration_with_path(db_path)
        sys.exit(0 if success else 1)
    else:
        migrate()
