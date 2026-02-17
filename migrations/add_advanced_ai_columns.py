"""
Миграция для добавления расширенных AI колонок

Добавляет:
1. Новые AI поля в imported_products для хранения результатов анализа
2. Новые AI инструкции в auto_import_settings

Запуск:
    python migrations/add_advanced_ai_columns.py

Или через Flask shell:
    flask shell
    >>> exec(open('migrations/add_advanced_ai_columns.py').read())
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
    # Миграция imported_products
    # =====================================
    print("\n=== Migrating imported_products ===")
    cursor.execute('PRAGMA table_info(imported_products)')
    existing_columns = [row[1] for row in cursor.fetchall()]
    print(f'Existing columns in imported_products: {len(existing_columns)}')

    # Новые колонки для расширенного AI анализа
    new_product_columns = [
        ('ai_dimensions', 'TEXT'),  # Габариты (JSON)
        ('ai_clothing_sizes', 'TEXT'),  # Размеры одежды (JSON)
        ('ai_detected_brand', 'TEXT'),  # Определенный AI бренд (JSON)
        ('ai_materials', 'TEXT'),  # Материалы и состав (JSON)
        ('ai_colors', 'TEXT'),  # Цвета товара (JSON)
        ('ai_attributes', 'TEXT'),  # Полный набор атрибутов (JSON)
        ('ai_gender', 'VARCHAR(20)'),  # Пол: male/female/unisex
        ('ai_age_group', 'VARCHAR(20)'),  # Возрастная группа
        ('ai_season', 'VARCHAR(20)'),  # Сезон
        ('ai_country', 'VARCHAR(100)'),  # Страна производства
    ]

    products_added = 0
    for col_name, col_type in new_product_columns:
        if col_name not in existing_columns:
            try:
                cursor.execute(f'ALTER TABLE imported_products ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                products_added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')
        else:
            print(f'  = Column {col_name} already exists')

    print(f'Added {products_added} columns to imported_products')

    # =====================================
    # Миграция auto_import_settings
    # =====================================
    print("\n=== Migrating auto_import_settings ===")
    cursor.execute('PRAGMA table_info(auto_import_settings)')
    settings_columns = [row[1] for row in cursor.fetchall()]
    print(f'Existing columns in auto_import_settings: {len(settings_columns)}')

    # Новые колонки AI инструкций
    new_settings_columns = [
        ('ai_dimensions_instruction', 'TEXT'),
        ('ai_clothing_sizes_instruction', 'TEXT'),
        ('ai_brand_instruction', 'TEXT'),
        ('ai_material_instruction', 'TEXT'),
        ('ai_color_instruction', 'TEXT'),
        ('ai_attributes_instruction', 'TEXT'),
    ]

    settings_added = 0
    for col_name, col_type in new_settings_columns:
        if col_name not in settings_columns:
            try:
                cursor.execute(f'ALTER TABLE auto_import_settings ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                settings_added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')
        else:
            print(f'  = Column {col_name} already exists')

    print(f'Added {settings_added} columns to auto_import_settings')

    conn.commit()
    conn.close()

    print(f'\nMigration completed!')
    print(f'  - imported_products: {products_added} new columns')
    print(f'  - auto_import_settings: {settings_added} new columns')
    return True


def run_migration_with_path(db_path):
    """Запускает миграцию с указанным путем к БД"""
    print(f"Using provided path: {db_path}")

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # imported_products
    print("\n=== Migrating imported_products ===")
    cursor.execute('PRAGMA table_info(imported_products)')
    existing_columns = [row[1] for row in cursor.fetchall()]

    new_product_columns = [
        ('ai_dimensions', 'TEXT'),
        ('ai_clothing_sizes', 'TEXT'),
        ('ai_detected_brand', 'TEXT'),
        ('ai_materials', 'TEXT'),
        ('ai_colors', 'TEXT'),
        ('ai_attributes', 'TEXT'),
        ('ai_gender', 'VARCHAR(20)'),
        ('ai_age_group', 'VARCHAR(20)'),
        ('ai_season', 'VARCHAR(20)'),
        ('ai_country', 'VARCHAR(100)'),
    ]

    products_added = 0
    for col_name, col_type in new_product_columns:
        if col_name not in existing_columns:
            try:
                cursor.execute(f'ALTER TABLE imported_products ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                products_added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')

    # auto_import_settings
    print("\n=== Migrating auto_import_settings ===")
    cursor.execute('PRAGMA table_info(auto_import_settings)')
    settings_columns = [row[1] for row in cursor.fetchall()]

    new_settings_columns = [
        ('ai_dimensions_instruction', 'TEXT'),
        ('ai_clothing_sizes_instruction', 'TEXT'),
        ('ai_brand_instruction', 'TEXT'),
        ('ai_material_instruction', 'TEXT'),
        ('ai_color_instruction', 'TEXT'),
        ('ai_attributes_instruction', 'TEXT'),
    ]

    settings_added = 0
    for col_name, col_type in new_settings_columns:
        if col_name not in settings_columns:
            try:
                cursor.execute(f'ALTER TABLE auto_import_settings ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                settings_added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')

    conn.commit()
    conn.close()
    print(f'\nMigration completed! Products: {products_added}, Settings: {settings_added}')
    return True


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1:
        db_path = sys.argv[1]
        success = run_migration_with_path(db_path)
        sys.exit(0 if success else 1)
    else:
        migrate()
