#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: создание таблиц контент-фабрики

Таблицы:
- content_factories — конвейеры генерации контента
- social_accounts — подключённые аккаунты соцсетей
- content_templates — шаблоны для генерации
- content_items — единицы сгенерированного контента
- content_plans — контент-планы (календарь)

Запуск:
    python migrations/migrate_add_content_factory.py
"""

import sqlite3
import os
import sys


def find_database():
    """Находит базу данных в разных возможных путях"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)

    possible_paths = [
        '/app/data/seller_platform.db',
        '/app/seller_platform.db',
        '/app/instance/seller_platform.db',
        os.path.join(parent_dir, 'seller_platform.db'),
        os.path.join(parent_dir, 'data', 'seller_platform.db'),
        os.path.join(parent_dir, 'instance', 'seller_platform.db'),
        os.path.join(parent_dir, 'instance', 'app.db'),
        os.path.join(parent_dir, 'app.db'),
        os.path.join(parent_dir, 'data', 'app.db'),
        'seller_platform.db',
        'data/seller_platform.db',
        'instance/app.db',
        'app.db',
        'data/app.db',
    ]

    for path in possible_paths:
        if os.path.exists(path):
            return path

    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('sqlite:///'):
        db_path = db_url.replace('sqlite:///', '')
        if os.path.exists(db_path):
            return db_path

    return None


def migrate(db_path):
    """Создаёт таблицы контент-фабрики"""

    print(f"\n{'='*60}")
    print(f"📂 База данных: {db_path}")
    print(f"   Миграция: Content Factory")
    print(f"{'='*60}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    total_created = 0

    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row[0] for row in cursor.fetchall()}

        # ============================================================
        # 1. social_accounts (создаём первой, т.к. на неё ссылается content_factories)
        # ============================================================
        print("\n📋 Таблица: social_accounts")
        print("-" * 40)

        if 'social_accounts' not in existing_tables:
            cursor.execute("""
                CREATE TABLE social_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id),
                    platform VARCHAR(20) NOT NULL,
                    account_name VARCHAR(200),
                    account_id VARCHAR(200),
                    credentials TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    connected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_used_at DATETIME,
                    last_error TEXT
                )
            """)
            cursor.execute("CREATE INDEX idx_sa_seller ON social_accounts(seller_id)")
            cursor.execute("CREATE INDEX idx_sa_platform ON social_accounts(seller_id, platform)")
            print("  ✅ Таблица social_accounts создана")
            total_created += 1
        else:
            print("  ⏭️  Таблица уже существует")

        # ============================================================
        # 2. content_factories
        # ============================================================
        print("\n📋 Таблица: content_factories")
        print("-" * 40)

        if 'content_factories' not in existing_tables:
            cursor.execute("""
                CREATE TABLE content_factories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id),
                    name VARCHAR(200) NOT NULL,
                    description TEXT,
                    platform VARCHAR(20) NOT NULL,
                    content_types_json TEXT DEFAULT '[]',
                    tone VARCHAR(20) DEFAULT 'casual',
                    style_guidelines TEXT,
                    language VARCHAR(10) DEFAULT 'ru',
                    product_selection_mode VARCHAR(20) DEFAULT 'manual',
                    product_selection_rules_json TEXT DEFAULT '{}',
                    ai_provider VARCHAR(20) DEFAULT 'openai',
                    schedule_cron VARCHAR(100),
                    auto_approve BOOLEAN DEFAULT 0,
                    default_social_account_id INTEGER REFERENCES social_accounts(id),
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX idx_cf_seller ON content_factories(seller_id)")
            cursor.execute("CREATE INDEX idx_cf_platform ON content_factories(seller_id, platform)")
            print("  ✅ Таблица content_factories создана")
            total_created += 1
        else:
            print("  ⏭️  Таблица уже существует")

        # ============================================================
        # 3. content_templates
        # ============================================================
        print("\n📋 Таблица: content_templates")
        print("-" * 40)

        if 'content_templates' not in existing_tables:
            cursor.execute("""
                CREATE TABLE content_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER REFERENCES sellers(id),
                    platform VARCHAR(20) NOT NULL,
                    content_type VARCHAR(30) NOT NULL,
                    name VARCHAR(200) NOT NULL,
                    system_prompt TEXT NOT NULL,
                    user_prompt_template TEXT NOT NULL,
                    example_output TEXT,
                    hashtag_strategy TEXT,
                    is_system BOOLEAN DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX idx_ct_seller ON content_templates(seller_id)")
            cursor.execute("CREATE INDEX idx_ct_platform_type ON content_templates(platform, content_type)")
            print("  ✅ Таблица content_templates создана")
            total_created += 1
        else:
            print("  ⏭️  Таблица уже существует")

        # ============================================================
        # 4. content_items
        # ============================================================
        print("\n📋 Таблица: content_items")
        print("-" * 40)

        if 'content_items' not in existing_tables:
            cursor.execute("""
                CREATE TABLE content_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    factory_id INTEGER NOT NULL REFERENCES content_factories(id),
                    seller_id INTEGER NOT NULL REFERENCES sellers(id),
                    template_id INTEGER REFERENCES content_templates(id),
                    platform VARCHAR(20) NOT NULL,
                    content_type VARCHAR(30) NOT NULL,
                    product_ids_json TEXT DEFAULT '[]',
                    title VARCHAR(500),
                    body_text TEXT NOT NULL,
                    hashtags_json TEXT DEFAULT '[]',
                    media_urls_json TEXT DEFAULT '[]',
                    platform_specific_json TEXT DEFAULT '{}',
                    status VARCHAR(20) DEFAULT 'draft',
                    scheduled_at DATETIME,
                    published_at DATETIME,
                    social_account_id INTEGER REFERENCES social_accounts(id),
                    external_post_id VARCHAR(200),
                    external_post_url VARCHAR(500),
                    ai_provider VARCHAR(20),
                    ai_model VARCHAR(50),
                    tokens_used INTEGER,
                    generation_time_ms INTEGER,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX idx_ci_factory ON content_items(factory_id)")
            cursor.execute("CREATE INDEX idx_ci_seller ON content_items(seller_id)")
            cursor.execute("CREATE INDEX idx_ci_factory_status ON content_items(factory_id, status)")
            cursor.execute("CREATE INDEX idx_ci_scheduled ON content_items(status, scheduled_at)")
            cursor.execute("CREATE INDEX idx_ci_platform ON content_items(seller_id, platform)")
            print("  ✅ Таблица content_items создана")
            total_created += 1
        else:
            print("  ⏭️  Таблица уже существует")

        # ============================================================
        # 5. content_plans
        # ============================================================
        print("\n📋 Таблица: content_plans")
        print("-" * 40)

        if 'content_plans' not in existing_tables:
            cursor.execute("""
                CREATE TABLE content_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    factory_id INTEGER NOT NULL REFERENCES content_factories(id),
                    seller_id INTEGER NOT NULL REFERENCES sellers(id),
                    name VARCHAR(200) NOT NULL,
                    date_from DATE NOT NULL,
                    date_to DATE NOT NULL,
                    slots_json TEXT DEFAULT '[]',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX idx_cp_factory ON content_plans(factory_id)")
            cursor.execute("CREATE INDEX idx_cp_seller ON content_plans(seller_id)")
            print("  ✅ Таблица content_plans создана")
            total_created += 1
        else:
            print("  ⏭️  Таблица уже существует")

        # ============================================================
        # Вставка системных шаблонов контента
        # ============================================================
        print("\n📋 Системные шаблоны контента")
        print("-" * 40)

        cursor.execute("SELECT COUNT(*) FROM content_templates WHERE is_system = 1")
        existing_system = cursor.fetchone()[0]

        if existing_system == 0:
            system_templates = [
                # Telegram
                ('telegram', 'promo_post', 'Промо-пост Telegram',
                 'Ты — опытный SMM-менеджер для маркетплейсов. Пишешь продающие посты для Telegram-каналов. Стиль: лаконичный, с эмодзи, призывом к действию. Максимум 1000 символов.',
                 'Напиши продающий пост для Telegram о товаре:\n\nНазвание: {product_name}\nЦена: {price} руб.\nБренд: {brand}\nКатегория: {category}\nОписание: {description}\n\nТребования:\n- Цепляющий заголовок\n- 2-3 ключевых преимущества\n- Цена и ссылка на WB\n- 3-5 хештегов\n- Призыв к действию'),

                ('telegram', 'review', 'Обзор товара Telegram',
                 'Ты — блогер-обзорщик товаров с Wildberries. Пишешь честные, информативные обзоры для Telegram. Стиль: дружелюбный, экспертный, с личным опытом.',
                 'Напиши обзор товара для Telegram-канала:\n\nНазвание: {product_name}\nЦена: {price} руб.\nБренд: {brand}\nКатегория: {category}\nОписание: {description}\nХарактеристики: {characteristics}\n\nТребования:\n- Заголовок с оценкой (из 10)\n- Плюсы и минусы\n- Для кого подойдёт\n- Итоговая рекомендация\n- 3-5 хештегов'),

                ('telegram', 'carousel', 'Подборка товаров Telegram',
                 'Ты — SMM-менеджер, составляешь тематические подборки товаров для Telegram-канала. Стиль: структурированный, с нумерацией, краткий.',
                 'Составь подборку товаров для Telegram-канала:\n\nТема подборки: {collection_theme}\nТовары:\n{products_list}\n\nТребования:\n- Цепляющий заголовок подборки\n- Краткое описание каждого товара (2-3 предложения)\n- Цена каждого товара\n- Общий призыв к действию в конце\n- 5-7 хештегов'),

                # VK
                ('vk', 'promo_post', 'Промо-пост ВКонтакте',
                 'Ты — SMM-менеджер для сообщества ВКонтакте. Пишешь посты для продвижения товаров с Wildberries. Стиль: дружелюбный, подробный, с эмодзи. Максимум 2000 символов.',
                 'Напиши пост для ВКонтакте о товаре:\n\nНазвание: {product_name}\nЦена: {price} руб.\nБренд: {brand}\nКатегория: {category}\nОписание: {description}\n\nТребования:\n- Привлекающий внимание заголовок\n- Подробное описание преимуществ\n- Цена и ссылка\n- 5-10 хештегов\n- Призыв к действию'),

                ('vk', 'review', 'Обзор товара ВКонтакте',
                 'Ты — блогер-обзорщик товаров. Пишешь развёрнутые обзоры для сообщества ВКонтакте. Стиль: экспертный, структурированный, с фото-описанием.',
                 'Напиши подробный обзор товара для ВКонтакте:\n\nНазвание: {product_name}\nЦена: {price} руб.\nБренд: {brand}\nКатегория: {category}\nОписание: {description}\nХарактеристики: {characteristics}\n\nТребования:\n- Заголовок-интрига\n- Первое впечатление\n- Подробный разбор характеристик\n- Плюсы и минусы списком\n- Сравнение с аналогами (если возможно)\n- Итоговый вердикт\n- 5-10 хештегов'),

                # Instagram
                ('instagram', 'promo_post', 'Промо-пост Instagram',
                 'Ты — Instagram-маркетолог. Пишешь продающие описания к постам. Стиль: стильный, с эмодзи, визуально структурированный. Максимум 2200 символов.',
                 'Напиши описание к Instagram-посту о товаре:\n\nНазвание: {product_name}\nЦена: {price} руб.\nБренд: {brand}\nКатегория: {category}\nОписание: {description}\n\nТребования:\n- Первая строка — хук (привлечь внимание)\n- Описание преимуществ с эмодзи\n- Цена\n- Призыв к действию\n- 15-25 хештегов (в конце поста)'),

                ('instagram', 'story_script', 'Stories-скрипт Instagram',
                 'Ты — Instagram-маркетолог. Создаёшь сценарии для Stories. Формат: набор слайдов с текстом и указаниями по визуалу.',
                 'Создай сценарий Instagram Stories для товара:\n\nНазвание: {product_name}\nЦена: {price} руб.\nБренд: {brand}\nОписание: {description}\n\nТребования:\n- 5-7 слайдов\n- Для каждого слайда: текст на экране + описание визуала\n- Первый слайд — интрига/вопрос\n- Последний — призыв к действию с ссылкой\n- Используй стикеры: опрос, вопрос, отсчёт'),

                # TikTok
                ('tiktok', 'story_script', 'Скрипт видео TikTok',
                 'Ты — TikTok-маркетолог. Пишешь сценарии коротких видео для продвижения товаров. Стиль: динамичный, трендовый, с хуком в первые 3 секунды.',
                 'Напиши сценарий TikTok-видео о товаре:\n\nНазвание: {product_name}\nЦена: {price} руб.\nБренд: {brand}\nКатегория: {category}\nОписание: {description}\n\nТребования:\n- Хронометраж 30-60 секунд\n- Хук в первые 3 секунды\n- Сценарий по секундам\n- Рекомендации по музыке/звуку\n- Призыв к действию\n- 5-10 хештегов (включая трендовые)'),

                ('tiktok', 'promo_post', 'Промо-описание TikTok',
                 'Ты — TikTok-маркетолог. Пишешь описания к видео. Стиль: молодёжный, с трендовыми хештегами, максимум 300 символов.',
                 'Напиши описание к TikTok-видео о товаре:\n\nНазвание: {product_name}\nЦена: {price} руб.\nБренд: {brand}\nОписание: {description}\n\nТребования:\n- Короткий цепляющий текст (до 150 символов)\n- 5-10 хештегов (микс трендовых и тематических)\n- Призыв к действию'),

                # YouTube
                ('youtube', 'review', 'Обзор товара YouTube',
                 'Ты — YouTube-блогер, делаешь обзоры товаров с маркетплейсов. Пишешь сценарии видео и описания. Стиль: экспертный, структурированный.',
                 'Напиши сценарий YouTube-обзора товара:\n\nНазвание: {product_name}\nЦена: {price} руб.\nБренд: {brand}\nКатегория: {category}\nОписание: {description}\nХарактеристики: {characteristics}\n\nТребования:\n- Заголовок видео (до 60 символов)\n- Описание видео (для поисковой выдачи)\n- Таймкоды сценария\n- Вступление (30 сек)\n- Распаковка и первые впечатления\n- Подробный обзор\n- Плюсы и минусы\n- Итог и рекомендация\n- Теги для YouTube (10-15 тегов)'),

                ('youtube', 'story_script', 'YouTube Shorts скрипт',
                 'Ты — YouTube-блогер. Создаёшь сценарии для YouTube Shorts (до 60 секунд). Стиль: динамичный, с хуком.',
                 'Напиши сценарий YouTube Shorts о товаре:\n\nНазвание: {product_name}\nЦена: {price} руб.\nБренд: {brand}\nОписание: {description}\n\nТребования:\n- Хронометраж до 60 секунд\n- Хук в первые 2-3 секунды\n- Сценарий по секундам\n- Заголовок (до 100 символов)\n- Описание\n- 5-10 хештегов'),
            ]

            for platform, content_type, name, system_prompt, user_prompt in system_templates:
                cursor.execute("""
                    INSERT INTO content_templates
                    (seller_id, platform, content_type, name, system_prompt, user_prompt_template, is_system, is_active)
                    VALUES (NULL, ?, ?, ?, ?, ?, 1, 1)
                """, (platform, content_type, name, system_prompt, user_prompt))

            print(f"  ✅ Вставлено {len(system_templates)} системных шаблонов")
        else:
            print(f"  ⏭️  Системные шаблоны уже существуют ({existing_system} шт.)")

        conn.commit()

        print(f"\n{'='*60}")
        print(f"✅ Миграция Content Factory завершена!")
        print(f"   Создано таблиц: {total_created}")
        print(f"{'='*60}\n")

        return True

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Ошибка миграции: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()


def main():
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
        if not os.path.exists(db_path):
            print(f"❌ Файл базы данных не найден: {db_path}")
            sys.exit(1)
    else:
        db_path = find_database()
        if not db_path:
            print("❌ База данных не найдена!")
            print("\nУкажите путь явно:")
            print("  python migrations/migrate_add_content_factory.py /path/to/database.db")
            sys.exit(1)

    success = migrate(db_path)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
