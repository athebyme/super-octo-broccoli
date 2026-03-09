#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Комплексная миграция: добавляет ВСЕ недостающие колонки

Этот скрипт объединяет все миграции и безопасно добавляет колонки,
которых нет в базе данных.

Запуск:
    python migrations/run_all_migrations.py

Или с указанием пути к БД:
    python migrations/run_all_migrations.py /path/to/database.db
"""

import sqlite3
import os
import sys


def find_database():
    """Находит базу данных в разных возможных путях"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)

    possible_paths = [
        # Docker пути - seller_platform.db (основная база!)
        '/app/data/seller_platform.db',
        '/app/seller_platform.db',
        '/app/instance/seller_platform.db',
        # Docker пути - app.db
        '/app/instance/app.db',
        '/app/app.db',
        '/app/data/app.db',
        # Локальные пути относительно скрипта
        os.path.join(parent_dir, 'seller_platform.db'),
        os.path.join(parent_dir, 'data', 'seller_platform.db'),
        os.path.join(parent_dir, 'instance', 'seller_platform.db'),
        os.path.join(parent_dir, 'instance', 'app.db'),
        os.path.join(parent_dir, 'app.db'),
        os.path.join(parent_dir, 'data', 'app.db'),
        # Текущая директория
        'seller_platform.db',
        'data/seller_platform.db',
        'instance/app.db',
        'app.db',
        'data/app.db',
    ]

    for path in possible_paths:
        if os.path.exists(path):
            return path

    # Проверяем переменную окружения
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('sqlite:///'):
        db_path = db_url.replace('sqlite:///', '')
        if os.path.exists(db_path):
            return db_path

    return None


def get_existing_columns(cursor, table_name):
    """Получает список существующих колонок в таблице"""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def add_column_if_missing(cursor, table_name, column_name, column_type, existing_columns):
    """Добавляет колонку если её нет"""
    if column_name not in existing_columns:
        try:
            sql = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            cursor.execute(sql)
            print(f"  ✅ Добавлена: {column_name}")
            return True
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                print(f"  ⏭️  Уже существует: {column_name}")
            else:
                print(f"  ❌ Ошибка: {column_name} - {e}")
            return False
    else:
        print(f"  ⏭️  Уже существует: {column_name}")
        return False


def migrate(db_path):
    """Выполняет все миграции"""

    print(f"\n{'='*60}")
    print(f"📂 База данных: {db_path}")
    print(f"{'='*60}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    total_added = 0

    try:
        # ============================================================
        # Миграция auto_import_settings
        # ============================================================
        print("\n📋 Таблица: auto_import_settings")
        print("-" * 40)

        existing = get_existing_columns(cursor, 'auto_import_settings')
        print(f"   Существующих колонок: {len(existing)}")

        # Все колонки для auto_import_settings
        settings_columns = [
            # Генерация изображений (из add_image_gen_columns.py)
            ("image_gen_enabled", "BOOLEAN DEFAULT 0 NOT NULL"),
            ("image_gen_provider", "VARCHAR(50) DEFAULT 'fluxapi'"),
            ("fluxapi_key", "VARCHAR(500)"),
            ("tensorart_app_id", "VARCHAR(500)"),
            ("tensorart_api_key", "VARCHAR(500)"),
            ("together_api_key", "VARCHAR(500)"),
            ("openai_api_key", "VARCHAR(500)"),
            ("replicate_api_key", "VARCHAR(500)"),
            ("image_gen_width", "INTEGER DEFAULT 1440"),
            ("image_gen_height", "INTEGER DEFAULT 810"),
            ("openai_image_quality", "VARCHAR(20) DEFAULT 'standard'"),
            ("openai_image_style", "VARCHAR(20) DEFAULT 'vivid'"),

            # AI инструкции основные (из add_ai_instructions_columns.py)
            ("ai_seo_title_instruction", "TEXT"),
            ("ai_keywords_instruction", "TEXT"),
            ("ai_bullets_instruction", "TEXT"),
            ("ai_description_instruction", "TEXT"),
            ("ai_rich_content_instruction", "TEXT"),
            ("ai_analysis_instruction", "TEXT"),

            # Расширенные AI инструкции (из add_advanced_ai_columns.py)
            ("ai_dimensions_instruction", "TEXT"),
            ("ai_clothing_sizes_instruction", "TEXT"),
            ("ai_brand_instruction", "TEXT"),
            ("ai_material_instruction", "TEXT"),
            ("ai_color_instruction", "TEXT"),
            ("ai_attributes_instruction", "TEXT"),

            # Cloud.ru OAuth2
            ("ai_client_id", "VARCHAR(500)"),
            ("ai_client_secret", "VARCHAR(500)"),

            # Дополнительные AI параметры
            ("ai_top_p", "FLOAT DEFAULT 0.95"),
            ("ai_presence_penalty", "FLOAT DEFAULT 0.0"),
            ("ai_frequency_penalty", "FLOAT DEFAULT 0.0"),
        ]

        for col_name, col_type in settings_columns:
            if add_column_if_missing(cursor, 'auto_import_settings', col_name, col_type, existing):
                total_added += 1

        # ============================================================
        # Миграция imported_products
        # ============================================================
        print("\n📋 Таблица: imported_products")
        print("-" * 40)

        existing = get_existing_columns(cursor, 'imported_products')
        print(f"   Существующих колонок: {len(existing)}")

        # Все колонки для imported_products
        products_columns = [
            # Расширенные AI поля (из add_advanced_ai_columns.py)
            ("ai_dimensions", "TEXT"),
            ("ai_clothing_sizes", "TEXT"),
            ("ai_detected_brand", "TEXT"),
            ("ai_materials", "TEXT"),
            ("ai_colors", "TEXT"),
            ("ai_attributes", "TEXT"),
            ("ai_gender", "VARCHAR(20)"),
            ("ai_age_group", "VARCHAR(20)"),
            ("ai_season", "VARCHAR(20)"),
            ("ai_country", "VARCHAR(100)"),
            # Оригинальные данные поставщика
            ("original_data", "TEXT"),
            # Brand registry
            ("resolved_brand_id", "INTEGER REFERENCES brands(id)"),
            ("brand_status", "VARCHAR(20)"),
        ]

        for col_name, col_type in products_columns:
            if add_column_if_missing(cursor, 'imported_products', col_name, col_type, existing):
                total_added += 1

        # Индексы для brand registry
        try:
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ip_resolved_brand ON imported_products(resolved_brand_id)')
        except sqlite3.OperationalError:
            pass

        # ============================================================
        # Миграция supplier_products (brand registry)
        # ============================================================
        print("\n📋 Таблица: supplier_products (brand registry)")
        print("-" * 40)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='supplier_products'")
        if cursor.fetchone():
            existing_sp = get_existing_columns(cursor, 'supplier_products')
            sp_columns = [
                ("resolved_brand_id", "INTEGER REFERENCES brands(id)"),
                # AI parser columns
                ("ai_parsed_data_json", "TEXT"),
                ("ai_parsed_at", "DATETIME"),
                ("ai_model_used", "VARCHAR(100)"),
                ("ai_marketplace_json", "TEXT"),
                ("description_source", "VARCHAR(50)"),
                # Marketplace columns
                ("marketplace_fields_json", "TEXT"),
                ("marketplace_validation_status", "VARCHAR(50)"),
                ("marketplace_fill_pct", "FLOAT"),
            ]
            for col_name, col_type in sp_columns:
                if add_column_if_missing(cursor, 'supplier_products', col_name, col_type, existing_sp):
                    total_added += 1
            try:
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_sp_resolved_brand ON supplier_products(resolved_brand_id)')
            except sqlite3.OperationalError:
                pass

        # ============================================================
        # Миграция suppliers (AI parser + description columns)
        # ============================================================
        print("\n📋 Таблица: suppliers (AI parser)")
        print("-" * 40)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='suppliers'")
        if cursor.fetchone():
            existing_sup = get_existing_columns(cursor, 'suppliers')
            sup_columns = [
                ("description_file_url", "VARCHAR(500)"),
                ("description_file_delimiter", "VARCHAR(5) DEFAULT ';'"),
                ("description_file_encoding", "VARCHAR(20) DEFAULT 'cp1251'"),
                ("last_description_sync_at", "DATETIME"),
                ("last_description_sync_status", "VARCHAR(50)"),
                ("ai_parsing_instruction", "TEXT"),
            ]
            for col_name, col_type in sup_columns:
                if add_column_if_missing(cursor, 'suppliers', col_name, col_type, existing_sup):
                    total_added += 1

        # ============================================================
        # Миграция ai_history
        # ============================================================
        print("\n📋 Таблица: ai_history")
        print("-" * 40)

        # Проверяем существует ли таблица
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ai_history'")
        if cursor.fetchone():
            existing = get_existing_columns(cursor, 'ai_history')
            print(f"   Существующих колонок: {len(existing)}")

            history_columns = [
                ("ai_provider", "VARCHAR(50)"),
                ("ai_model", "VARCHAR(100)"),
                ("system_prompt", "TEXT"),
                ("user_prompt", "TEXT"),
                ("raw_response", "TEXT"),
                ("tokens_prompt", "INTEGER DEFAULT 0"),
                ("tokens_completion", "INTEGER DEFAULT 0"),
                ("response_time_ms", "INTEGER DEFAULT 0"),
                ("source_module", "VARCHAR(100)"),
            ]

            for col_name, col_type in history_columns:
                if add_column_if_missing(cursor, 'ai_history', col_name, col_type, existing):
                    total_added += 1

            # Создаем индекс
            try:
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_history_created ON ai_history(created_at)')
                print("  ✅ Индекс idx_ai_history_created создан/существует")
            except sqlite3.OperationalError as e:
                print(f"  ⚠️  Индекс: {e}")
        else:
            print("   ⏭️  Таблица не существует (будет создана при первом использовании)")

        # ============================================================
        # Создание таблицы ai_parse_jobs (если не существует)
        # ============================================================
        print("\n📋 Таблица: ai_parse_jobs")
        print("-" * 40)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ai_parse_jobs'")
        if not cursor.fetchone():
            cursor.execute("""
                CREATE TABLE ai_parse_jobs (
                    id VARCHAR(36) PRIMARY KEY,
                    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                    admin_user_id INTEGER,
                    job_type VARCHAR(30) DEFAULT 'parse',
                    status VARCHAR(20) DEFAULT 'pending',
                    total INTEGER DEFAULT 0,
                    processed INTEGER DEFAULT 0,
                    succeeded INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    current_product_title VARCHAR(200),
                    model_used VARCHAR(100),
                    results TEXT,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_parse_jobs_supplier ON ai_parse_jobs(supplier_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_parse_jobs_status ON ai_parse_jobs(status)")
            print("  ✅ Таблица ai_parse_jobs создана")
            total_added += 1
        else:
            print("  ⏭️  Таблица уже существует")
            # Добавляем model_used если нет
            existing_ajc = get_existing_columns(cursor, 'ai_parse_jobs')
            if add_column_if_missing(cursor, 'ai_parse_jobs', 'model_used', 'VARCHAR(100)', existing_ajc):
                total_added += 1

        # ============================================================
        # Создание таблицы enrichment_jobs (если не существует)
        # ============================================================
        print("\n📋 Таблица: enrichment_jobs")
        print("-" * 40)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='enrichment_jobs'")
        if not cursor.fetchone():
            cursor.execute("""
                CREATE TABLE enrichment_jobs (
                    id VARCHAR(36) PRIMARY KEY,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id),
                    status VARCHAR(20) DEFAULT 'pending',
                    total INTEGER DEFAULT 0,
                    processed INTEGER DEFAULT 0,
                    succeeded INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    skipped INTEGER DEFAULT 0,
                    fields_config TEXT,
                    photo_strategy VARCHAR(20) DEFAULT 'replace',
                    results TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("  ✅ Таблица enrichment_jobs создана")
            total_added += 1
        else:
            print("  ⏭️  Таблица уже существует")

        # ============================================================
        # Создание/обновление таблицы product_defaults
        # ============================================================
        print("\n📋 Таблица: product_defaults")
        print("-" * 40)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='product_defaults'")
        if not cursor.fetchone():
            cursor.execute("""
                CREATE TABLE product_defaults (
                    id INTEGER PRIMARY KEY,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id),
                    rule_type VARCHAR(20) NOT NULL DEFAULT 'global',
                    wb_subject_id INTEGER,
                    wb_category_name VARCHAR(300),
                    length_cm FLOAT,
                    width_cm FLOAT,
                    height_cm FLOAT,
                    weight_kg FLOAT,
                    default_characteristics TEXT,
                    global_media TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(seller_id, rule_type, wb_subject_id)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_product_defaults_seller ON product_defaults(seller_id)")
            print("  ✅ Таблица product_defaults создана")
            total_added += 1
        else:
            print("  ⏭️  Таблица уже существует")
            existing_pdc = get_existing_columns(cursor, 'product_defaults')
            if add_column_if_missing(cursor, 'product_defaults', 'default_characteristics', 'TEXT', existing_pdc):
                total_added += 1

        # ============================================================
        # Дедупликация Product по (seller_id, nm_id)
        # и создание уникального индекса
        # ============================================================
        print("\n📋 Дедупликация products (seller_id, nm_id)")
        print("-" * 40)

        # Находим дубликаты
        cursor.execute("""
            SELECT seller_id, nm_id, COUNT(*) as cnt, GROUP_CONCAT(id) as ids
            FROM products
            WHERE nm_id > 0
            GROUP BY seller_id, nm_id
            HAVING cnt > 1
        """)
        duplicates = cursor.fetchall()

        if duplicates:
            dedup_deleted = 0
            for seller_id, nm_id, count, ids_str in duplicates:
                ids = [int(x) for x in ids_str.split(',')]
                keep_id = min(ids)
                delete_ids = [x for x in ids if x != keep_id]

                # Переносим ссылки ImportedProduct
                for del_id in delete_ids:
                    cursor.execute(
                        "UPDATE imported_products SET product_id = ? WHERE product_id = ?",
                        (keep_id, del_id)
                    )

                # Удаляем дубли
                cursor.execute(
                    f"DELETE FROM products WHERE id IN ({','.join('?' * len(delete_ids))})",
                    delete_ids
                )
                dedup_deleted += cursor.rowcount

            print(f"  ✅ Удалено {dedup_deleted} дублей Product")
            total_added += 1
        else:
            print("  ⏭️  Дубликатов нет")

        # Заменяем обычный индекс на уникальный
        try:
            cursor.execute("DROP INDEX IF EXISTS idx_seller_nm_id")
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_seller_nm_id
                ON products (seller_id, nm_id)
                WHERE nm_id > 0
            """)
            print("  ✅ Создан уникальный индекс uq_seller_nm_id")
        except sqlite3.OperationalError as e:
            print(f"  ⚠️  Индекс: {e}")

        # ============================================================
        # Коммит изменений
        # ============================================================
        conn.commit()

        print(f"\n{'='*60}")
        print(f"✅ Миграция завершена!")
        print(f"   Добавлено колонок: {total_added}")
        print(f"{'='*60}\n")

        return True

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Ошибка миграции: {e}")
        return False
    finally:
        conn.close()


def main():
    """Главная функция"""

    # Проверяем аргументы командной строки
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
        if not os.path.exists(db_path):
            print(f"❌ Файл базы данных не найден: {db_path}")
            sys.exit(1)
    else:
        db_path = find_database()
        if not db_path:
            print("❌ База данных не найдена!")
            print("\nПроверьте наличие файла в одном из путей:")
            print("  - /app/data/seller_platform.db (Docker)")
            print("  - ./data/seller_platform.db")
            print("  - ./instance/app.db")
            print("  - ./app.db")
            print("\nИли укажите путь явно:")
            print("  python migrations/run_all_migrations.py /path/to/database.db")
            sys.exit(1)

    success = migrate(db_path)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
