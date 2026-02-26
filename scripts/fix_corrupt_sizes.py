#!/usr/bin/env python3
"""
Очистка битых данных в поле sizes.
Запуск: docker exec seller-platform python /app/fix_corrupt_sizes.py
"""
import sqlite3
import json
import os

DB_PATH = os.environ.get('DATABASE_PATH', '/app/data/seller_platform.db')
con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row
cur = con.cursor()

print("Проверяем поле sizes у всех товаров...")
cur.execute("SELECT id, seller_id, sizes FROM imported_products WHERE sizes IS NOT NULL")
rows = cur.fetchall()

to_clear = []
for row in rows:
    val = row['sizes']
    # Критерии "мусорного" значения:
    # 1. Не является валидным JSON И длиннее 300 символов (признак промпта)
    # 2. Начинается с типичных AI-промптов
    is_valid_json = False
    try:
        json.loads(val)
        is_valid_json = True
    except Exception:
        pass

    is_garbage = False
    if not is_valid_json:
        if len(val) > 300:
            is_garbage = True
        prompt_prefixes = ('Определи ', 'Ты эксперт', 'Твоя задача', 'НАЗВАНИЕ:', 'Определите ')
        if any(val.startswith(p) or ('\n' + p) in val[:100] for p in prompt_prefixes):
            is_garbage = True

    if is_garbage:
        to_clear.append((row['id'], row['seller_id'], val[:120]))

if not to_clear:
    print("✅ Битых значений не найдено — база чистая.")
else:
    print(f"\n❌ Найдено {len(to_clear)} товаров с мусором в sizes:\n")
    for pid, sid, sample in to_clear:
        print(f"  product_id={pid}  seller_id={sid}")
        print(f"  Значение (120 chars): {sample!r}")
        print()

    confirm = input("Очистить эти поля (установить NULL)? [y/N]: ").strip().lower()
    if confirm == 'y':
        for pid, sid, _ in to_clear:
            cur.execute("UPDATE imported_products SET sizes = NULL WHERE id = ?", (pid,))
        con.commit()
        print(f"✅ Очищено {len(to_clear)} записей.")
    else:
        print("Отменено.")

con.close()
