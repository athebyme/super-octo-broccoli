#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: добавление таблицы product_defaults для дефолтных габаритов/веса и глобального медиа
"""
from models import db, ProductDefaults
from seller_platform import app


def run_migration():
    """Применяет миграцию"""
    print("Применение миграции: добавление таблицы product_defaults...")

    with app.app_context():
        db.create_all()
        print("  - product_defaults: создана")

    print("Миграция завершена.")


if __name__ == '__main__':
    run_migration()
