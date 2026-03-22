#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: Добавление реестра брендов (Brand Registry) с поддержкой мультимаркетплейса

Создаёт таблицы:
- brands — глобальный реестр брендов (без привязки к маркетплейсу)
- brand_aliases — маппинг вариантов написания
- marketplace_brands — привязка бренда к маркетплейсу (имя, ID, статус на площадке)
- brand_category_links — допустимость бренда в категориях конкретного маркетплейса

Модифицирует:
- imported_products — добавляет resolved_brand_id, brand_status
- supplier_products — добавляет resolved_brand_id

Сеет начальные данные:
- Импорт из BRAND_CANONICAL dict
- Создание MarketplaceBrand для WB
- Извлечение уникальных брендов из supplier_products
"""
import os
import sys
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_ROOT = BASE_DIR / 'data'
DEFAULT_DB_PATH = DATA_ROOT / 'seller_platform.db'


def get_db_path():
    """Получить путь к базе данных"""
    database_url = os.environ.get('DATABASE_URL')
    if database_url and database_url.startswith('sqlite:///'):
        return database_url.replace('sqlite:///', '')
    return str(DEFAULT_DB_PATH)


# Каноническое написание брендов из data_normalizer.py
BRAND_CANONICAL = {
    'lelo': 'LELO',
    'satisfyer': 'Satisfyer',
    'womanizer': 'Womanizer',
    'we-vibe': 'We-Vibe',
    'we vibe': 'We-Vibe',
    'fun factory': 'Fun Factory',
    'funfactory': 'Fun Factory',
    'baile': 'Baile',
    'toyfa': 'TOYFA',
    'sexus': 'Sexus',
    'bior toys': 'Bior Toys',
    'biortoys': 'Bior Toys',
    'fantasy': 'Fantasy',
    'pipedream': 'Pipedream',
    'doc johnson': 'Doc Johnson',
    'california exotic': 'California Exotic',
    'calexotics': 'CalExotics',
    'evolved': 'Evolved',
    'svakom': 'SVAKOM',
    'lovense': 'Lovense',
    'je joue': 'Je Joue',
    'hot': 'HOT',
    'system jo': 'System JO',
    'swiss navy': 'Swiss Navy',
}


def normalize_brand_name(name):
    """Нормализация имени бренда: lowercase, без лишних пробелов"""
    if not name:
        return ''
    return ' '.join(name.lower().strip().split())


def run_migration():
    db_path = get_db_path()
    logger.info(f"Миграция Brand Registry (мультимаркетплейс): {db_path}")

    if not os.path.exists(db_path):
        logger.warning(f"База данных не найдена: {db_path}. Миграция будет применена при создании БД.")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # ============================================================
        # 1. Создание таблицы brands (глобальный реестр)
        # ============================================================
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='brands'")
        if not cursor.fetchone():
            logger.info("Создание таблицы brands...")
            cursor.execute('''
                CREATE TABLE brands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(200) NOT NULL,
                    name_normalized VARCHAR(200) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    country VARCHAR(100),
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_brand_name_normalized UNIQUE (name_normalized)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_brand_status ON brands(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_brand_name_normalized ON brands(name_normalized)')
            logger.info("Таблица brands создана")
        else:
            logger.info("Таблица brands уже существует")
            # Миграция существующей таблицы: удаляем wb_brand_id и verified_at если есть
            # (данные мигрируют в marketplace_brands)
            cursor.execute("PRAGMA table_info(brands)")
            brand_columns = [row['name'] for row in cursor.fetchall()]
            if 'wb_brand_id' in brand_columns:
                logger.info("Существующая таблица brands содержит wb_brand_id — данные будут мигрированы в marketplace_brands")

        # ============================================================
        # 2. Создание таблицы brand_aliases
        # ============================================================
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='brand_aliases'")
        if not cursor.fetchone():
            logger.info("Создание таблицы brand_aliases...")
            cursor.execute('''
                CREATE TABLE brand_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                    alias VARCHAR(200) NOT NULL,
                    alias_normalized VARCHAR(200) NOT NULL,
                    source VARCHAR(30) NOT NULL DEFAULT 'manual',
                    confidence REAL DEFAULT 1.0,
                    supplier_id INTEGER REFERENCES suppliers(id),
                    is_active BOOLEAN DEFAULT 1 NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    CONSTRAINT uq_brand_alias_normalized UNIQUE (alias_normalized)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_alias_normalized ON brand_aliases(alias_normalized)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_alias_brand ON brand_aliases(brand_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_alias_source ON brand_aliases(source)')
            logger.info("Таблица brand_aliases создана")
        else:
            logger.info("Таблица brand_aliases уже существует")

        # ============================================================
        # 3. Создание таблицы marketplace_brands
        # ============================================================
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='marketplace_brands'")
        if not cursor.fetchone():
            logger.info("Создание таблицы marketplace_brands...")
            cursor.execute('''
                CREATE TABLE marketplace_brands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                    marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
                    marketplace_brand_name VARCHAR(200) NOT NULL,
                    marketplace_brand_id INTEGER,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    verified_at TIMESTAMP,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_brand_marketplace UNIQUE (brand_id, marketplace_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mpb_brand ON marketplace_brands(brand_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mpb_marketplace ON marketplace_brands(marketplace_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mpb_status ON marketplace_brands(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mpb_mp_brand_id ON marketplace_brands(marketplace_brand_id)')
            logger.info("Таблица marketplace_brands создана")
        else:
            logger.info("Таблица marketplace_brands уже существует")

        # ============================================================
        # 4. Создание/обновление таблицы brand_category_links
        # ============================================================
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='brand_category_links'")
        existing_bcl = cursor.fetchone()
        if not existing_bcl:
            logger.info("Создание таблицы brand_category_links...")
            cursor.execute('''
                CREATE TABLE brand_category_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    marketplace_brand_id INTEGER NOT NULL REFERENCES marketplace_brands(id) ON DELETE CASCADE,
                    category_id INTEGER NOT NULL,
                    category_name VARCHAR(200),
                    is_available BOOLEAN DEFAULT 1 NOT NULL,
                    verified_at TIMESTAMP,
                    CONSTRAINT uq_mp_brand_category UNIQUE (marketplace_brand_id, category_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_bcl_mp_brand ON brand_category_links(marketplace_brand_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_bcl_category ON brand_category_links(category_id)')
            logger.info("Таблица brand_category_links создана")
        else:
            # Проверяем — старая схема (brand_id + wb_subject_id) или новая (marketplace_brand_id + category_id)
            cursor.execute("PRAGMA table_info(brand_category_links)")
            bcl_columns = [row['name'] for row in cursor.fetchall()]
            if 'marketplace_brand_id' not in bcl_columns:
                logger.info("Пересоздание brand_category_links под новую схему (marketplace_brand_id)...")
                cursor.execute('DROP TABLE IF EXISTS brand_category_links')
                cursor.execute('''
                    CREATE TABLE brand_category_links (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        marketplace_brand_id INTEGER NOT NULL REFERENCES marketplace_brands(id) ON DELETE CASCADE,
                        category_id INTEGER NOT NULL,
                        category_name VARCHAR(200),
                        is_available BOOLEAN DEFAULT 1 NOT NULL,
                        verified_at TIMESTAMP,
                        CONSTRAINT uq_mp_brand_category UNIQUE (marketplace_brand_id, category_id)
                    )
                ''')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_bcl_mp_brand ON brand_category_links(marketplace_brand_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_bcl_category ON brand_category_links(category_id)')
                logger.info("brand_category_links пересоздана")
            else:
                logger.info("Таблица brand_category_links уже в новой схеме")

        # ============================================================
        # 5. Добавление resolved_brand_id в imported_products
        # ============================================================
        cursor.execute("PRAGMA table_info(imported_products)")
        columns = [row['name'] for row in cursor.fetchall()]

        if 'resolved_brand_id' not in columns:
            logger.info("Добавление resolved_brand_id в imported_products...")
            cursor.execute('ALTER TABLE imported_products ADD COLUMN resolved_brand_id INTEGER REFERENCES brands(id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ip_resolved_brand ON imported_products(resolved_brand_id)')

        if 'brand_status' not in columns:
            logger.info("Добавление brand_status в imported_products...")
            cursor.execute('ALTER TABLE imported_products ADD COLUMN brand_status VARCHAR(20)')

        # ============================================================
        # 6. Добавление resolved_brand_id в supplier_products
        # ============================================================
        cursor.execute("PRAGMA table_info(supplier_products)")
        columns = [row['name'] for row in cursor.fetchall()]

        if 'resolved_brand_id' not in columns:
            logger.info("Добавление resolved_brand_id в supplier_products...")
            cursor.execute('ALTER TABLE supplier_products ADD COLUMN resolved_brand_id INTEGER REFERENCES brands(id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_sp_resolved_brand ON supplier_products(resolved_brand_id)')

        # ============================================================
        # 7. Seed данных из BRAND_CANONICAL
        # ============================================================
        cursor.execute("SELECT COUNT(*) as cnt FROM brands")
        brand_count = cursor.fetchone()['cnt']

        if brand_count == 0:
            logger.info("Сеем начальные данные из BRAND_CANONICAL...")
            now = datetime.utcnow().isoformat()

            # Находим маркетплейс WB
            cursor.execute("SELECT id FROM marketplaces WHERE code = 'wb'")
            wb_row = cursor.fetchone()
            wb_marketplace_id = wb_row['id'] if wb_row else None
            if not wb_marketplace_id:
                logger.warning("Маркетплейс WB не найден — MarketplaceBrand записи не будут созданы")

            # Собираем уникальные канонические бренды
            canonical_brands = {}
            for alias_lower, canonical_name in BRAND_CANONICAL.items():
                name_norm = normalize_brand_name(canonical_name)
                if name_norm not in canonical_brands:
                    canonical_brands[name_norm] = canonical_name

            # Создаём бренды
            brand_id_map = {}  # name_normalized -> brand_id
            for name_norm, canonical_name in canonical_brands.items():
                cursor.execute(
                    'INSERT INTO brands (name, name_normalized, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                    (canonical_name, name_norm, 'verified', now, now)
                )
                brand_id = cursor.lastrowid
                brand_id_map[name_norm] = brand_id

                # Создаём MarketplaceBrand для WB
                if wb_marketplace_id:
                    cursor.execute(
                        'INSERT INTO marketplace_brands (brand_id, marketplace_id, marketplace_brand_name, status, verified_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (brand_id, wb_marketplace_id, canonical_name, 'verified', now, now, now)
                    )

                logger.info(f"  Бренд: {canonical_name} (id={brand_id})")

            # Создаём алиасы
            for alias_lower, canonical_name in BRAND_CANONICAL.items():
                name_norm = normalize_brand_name(canonical_name)
                brand_id = brand_id_map.get(name_norm)
                if not brand_id:
                    continue

                alias_norm = normalize_brand_name(alias_lower)

                cursor.execute('SELECT id FROM brand_aliases WHERE alias_normalized = ?', (alias_norm,))
                if not cursor.fetchone():
                    cursor.execute(
                        'INSERT INTO brand_aliases (brand_id, alias, alias_normalized, source, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                        (brand_id, alias_lower, alias_norm, 'manual', 1.0, now)
                    )

                canon_norm = normalize_brand_name(canonical_name)
                cursor.execute('SELECT id FROM brand_aliases WHERE alias_normalized = ?', (canon_norm,))
                if not cursor.fetchone():
                    cursor.execute(
                        'INSERT INTO brand_aliases (brand_id, alias, alias_normalized, source, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                        (brand_id, canonical_name, canon_norm, 'manual', 1.0, now)
                    )

            logger.info(f"Создано {len(canonical_brands)} брендов из BRAND_CANONICAL")

            # ============================================================
            # 8. Извлечение уникальных брендов из supplier_products
            # ============================================================
            logger.info("Извлечение уникальных брендов из supplier_products...")
            cursor.execute('''
                SELECT DISTINCT brand, supplier_id
                FROM supplier_products
                WHERE brand IS NOT NULL AND brand != ''
            ''')
            supplier_brands = cursor.fetchall()

            new_brands_count = 0
            new_aliases_count = 0

            for row in supplier_brands:
                raw_brand = row['brand']
                supplier_id = row['supplier_id']
                brand_norm = normalize_brand_name(raw_brand)

                if not brand_norm:
                    continue

                cursor.execute('SELECT ba.brand_id FROM brand_aliases ba WHERE ba.alias_normalized = ?', (brand_norm,))
                existing = cursor.fetchone()
                if existing:
                    continue

                cursor.execute('SELECT id FROM brands WHERE name_normalized = ?', (brand_norm,))
                brand_row = cursor.fetchone()

                if brand_row:
                    brand_id = brand_row['id']
                else:
                    cursor.execute(
                        'INSERT INTO brands (name, name_normalized, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                        (raw_brand.strip(), brand_norm, 'pending', now, now)
                    )
                    brand_id = cursor.lastrowid
                    new_brands_count += 1

                cursor.execute(
                    'INSERT INTO brand_aliases (brand_id, alias, alias_normalized, source, confidence, supplier_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (brand_id, raw_brand.strip(), brand_norm, 'supplier_csv', 0.8, supplier_id, now)
                )
                new_aliases_count += 1

            logger.info(f"Из supplier_products: {new_brands_count} новых брендов, {new_aliases_count} новых алиасов")

        # ============================================================
        # 9. Миграция из старой схемы: wb_brand_id -> marketplace_brands
        # ============================================================
        cursor.execute("PRAGMA table_info(brands)")
        brand_cols = [row['name'] for row in cursor.fetchall()]
        if 'wb_brand_id' in brand_cols:
            logger.info("Миграция wb_brand_id из brands в marketplace_brands...")
            cursor.execute("SELECT id FROM marketplaces WHERE code = 'wb'")
            wb_row = cursor.fetchone()
            if wb_row:
                wb_mp_id = wb_row['id']
                cursor.execute("SELECT id, name, wb_brand_id, verified_at FROM brands WHERE wb_brand_id IS NOT NULL")
                for row in cursor.fetchall():
                    cursor.execute("SELECT id FROM marketplace_brands WHERE brand_id = ? AND marketplace_id = ?",
                                   (row['id'], wb_mp_id))
                    if not cursor.fetchone():
                        cursor.execute(
                            'INSERT INTO marketplace_brands (brand_id, marketplace_id, marketplace_brand_name, marketplace_brand_id, status, verified_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                            (row['id'], wb_mp_id, row['name'], row['wb_brand_id'],
                             'verified', row['verified_at'], datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
                        )
                logger.info("wb_brand_id мигрирован в marketplace_brands")

        conn.commit()
        logger.info("Миграция Brand Registry (мультимаркетплейс) завершена!")

    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка миграции: {e}")
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    run_migration()
