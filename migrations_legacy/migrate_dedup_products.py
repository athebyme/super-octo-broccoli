#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: удаление дубликатов Product по (seller_id, nm_id)

Проблема: при импорте товара через WBProductImporter создавался Product,
а при общей синхронизации с WB создавался ещё один Product с тем же nm_id.
Результат — дубли карточек в разделе "Карточки".

Эта миграция:
1. Находит все дубликаты по (seller_id, nm_id)
2. Оставляет самую старую запись (обычно созданную при импорте)
3. Переносит ссылки ImportedProduct.product_id на оставшуюся запись
4. Удаляет лишние записи
5. Создаёт уникальный индекс для предотвращения дублей в будущем

Запуск:
    python migrations/migrate_dedup_products.py
"""

import sqlite3
import os
import sys


def find_database():
    """Находит базу данных"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)

    possible_paths = [
        '/app/data/seller_platform.db',
        '/app/seller_platform.db',
        '/app/instance/seller_platform.db',
        '/app/instance/app.db',
        '/app/app.db',
        '/app/data/app.db',
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
    print(f"\n{'='*60}")
    print(f"Дедупликация Product по (seller_id, nm_id)")
    print(f"База данных: {db_path}")
    print(f"{'='*60}\n")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Шаг 1: Найти дубликаты
    cursor.execute("""
        SELECT seller_id, nm_id, COUNT(*) as cnt, GROUP_CONCAT(id) as ids
        FROM products
        WHERE nm_id > 0
        GROUP BY seller_id, nm_id
        HAVING cnt > 1
    """)
    duplicates = cursor.fetchall()

    if not duplicates:
        print("Дубликатов не найдено.")
    else:
        print(f"Найдено {len(duplicates)} групп дубликатов:\n")

        total_deleted = 0

        for seller_id, nm_id, count, ids_str in duplicates:
            ids = [int(x) for x in ids_str.split(',')]
            keep_id = min(ids)  # оставляем самую старую запись
            delete_ids = [x for x in ids if x != keep_id]

            print(f"  seller_id={seller_id}, nm_id={nm_id}: {count} записей (IDs: {ids_str})")
            print(f"    Оставляем: ID={keep_id}, удаляем: {delete_ids}")

            # Переносим ссылки из imported_products на оставшуюся запись
            for del_id in delete_ids:
                cursor.execute(
                    "UPDATE imported_products SET product_id = ? WHERE product_id = ?",
                    (keep_id, del_id)
                )
                moved = cursor.rowcount
                if moved > 0:
                    print(f"    Перенесено {moved} ссылок ImportedProduct с Product ID={del_id} -> ID={keep_id}")

            # Удаляем дубли
            cursor.execute(
                f"DELETE FROM products WHERE id IN ({','.join('?' * len(delete_ids))})",
                delete_ids
            )
            deleted = cursor.rowcount
            total_deleted += deleted
            print(f"    Удалено: {deleted} дублей")

        conn.commit()
        print(f"\nИтого удалено: {total_deleted} дублей Product")

    # Шаг 2: Создаём уникальный индекс (если ещё нет)
    # Сначала удаляем старый неуникальный индекс
    try:
        cursor.execute("DROP INDEX IF EXISTS idx_seller_nm_id")
        print("\nУдалён старый индекс idx_seller_nm_id")
    except Exception as e:
        print(f"\nНе удалось удалить старый индекс: {e}")

    try:
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_seller_nm_id
            ON products (seller_id, nm_id)
            WHERE nm_id > 0
        """)
        conn.commit()
        print("Создан уникальный индекс uq_seller_nm_id на products(seller_id, nm_id) WHERE nm_id > 0")
    except sqlite3.OperationalError as e:
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            print(f"Ошибка создания уникального индекса (возможно есть оставшиеся дубли): {e}")
            print("Попробуйте запустить миграцию повторно")
        else:
            print(f"Ошибка создания индекса: {e}")

    conn.close()
    print(f"\n{'='*60}")
    print("Миграция завершена")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_path = find_database()

    if not db_path:
        print("База данных не найдена!")
        print("Укажите путь: python migrations/migrate_dedup_products.py /path/to/db.db")
        sys.exit(1)

    migrate(db_path)
