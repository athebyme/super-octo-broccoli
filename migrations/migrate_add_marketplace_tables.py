#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: добавление таблиц и полей для интеграции маркетплейсов
"""
import os
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import db
from seller_platform import app

def run_migration():
    """Применяет миграцию"""
    print("Применение миграции: добавление таблиц маркетплейсов...")

    with app.app_context():
        # Создаем новые таблицы
        db.create_all()

        print("✓ Таблицы проверены/созданы:")
        print("  - marketplaces")
        print("  - marketplace_categories")
        print("  - marketplace_category_characteristics")
        print("  - marketplace_directories")
        print("  - marketplace_connections")
        print("  - marketplace_sync_jobs")

        # Добавляем новые колонки в supplier_products
        try:
            db.session.execute(db.text("ALTER TABLE supplier_products ADD COLUMN marketplace_fields_json TEXT"))
            print("✓ Добавлена колонка marketplace_fields_json в supplier_products")
        except Exception as e:
            if "duplicate column name" in str(e).lower():
                print("⏭️ Колонка marketplace_fields_json уже существует")
            else:
                print(f"⚠️ Ошибка при добавлении marketplace_fields_json: {e}")

        try:
            db.session.execute(db.text("ALTER TABLE supplier_products ADD COLUMN marketplace_validation_status VARCHAR(50)"))
            print("✓ Добавлена колонка marketplace_validation_status в supplier_products")
        except Exception as e:
            if "duplicate column name" in str(e).lower():
                print("⏭️ Колонка marketplace_validation_status уже существует")
            else:
                print(f"⚠️ Ошибка при добавлении marketplace_validation_status: {e}")

        try:
            db.session.execute(db.text("ALTER TABLE supplier_products ADD COLUMN marketplace_fill_pct FLOAT"))
            print("✓ Добавлена колонка marketplace_fill_pct в supplier_products")
        except Exception as e:
            if "duplicate column name" in str(e).lower():
                print("⏭️ Колонка marketplace_fill_pct уже существует")
            else:
                print(f"⚠️ Ошибка при добавлении marketplace_fill_pct: {e}")

        db.session.commit()
        print("\nМиграция успешно завершена!")

if __name__ == '__main__':
    run_migration()
