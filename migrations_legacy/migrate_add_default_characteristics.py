#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: добавление колонки default_characteristics в product_defaults
"""
import sqlite3
import os


def run_migration():
    """Применяет миграцию"""
    print("Применение миграции: добавление default_characteristics в product_defaults...")

    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'seller_platform.db')
    if not os.path.exists(db_path):
        # Попробуем альтернативный путь
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'seller_platform.db')

    if not os.path.exists(db_path):
        print("  БД не найдена, пропускаем (будет создана при первом запуске)")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Проверяем, существует ли колонка
    cursor.execute("PRAGMA table_info(product_defaults)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'default_characteristics' not in columns:
        cursor.execute("ALTER TABLE product_defaults ADD COLUMN default_characteristics TEXT")
        conn.commit()
        print("  - default_characteristics: добавлена")
    else:
        print("  - default_characteristics: уже существует")

    conn.close()
    print("Миграция завершена.")


if __name__ == '__main__':
    run_migration()
