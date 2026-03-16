#!/usr/bin/env python3
"""
Скрипт добавления поставщика sex-opt.ru (old.sex-opt.ru) в БД.

Использование:
    python scripts/add_sexopt_supplier.py

Создаёт запись Supplier с полным csv_column_mapping для CSV-выгрузки sex-opt.ru.
CSV скачивается по постоянной ссылке с авторизацией через URL-параметры (user+hash).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from models import db, Supplier


SEXOPT_SUPPLIER_DATA = {
    "name": "sex-opt.ru",
    "code": "sexopt",
    "description": "Оптовый поставщик товаров для секс-шопов (old.sex-opt.ru). "
                   "CSV-выгрузка с авторизацией через URL-параметры.",
    "website": "https://old.sex-opt.ru",

    "csv_source_url": (
        "https://old.sex-opt.ru/catalogue/db_export/"
        "?type=csv"
        "&user=romantiki25@yandex.ru"
        "&hash=d1482b6450a8e8a59cddf7921dac1d65547770d4ee576dfb07e2cb1d15c11ef6"
        "&columns_separator=%3B"
        "&encoding=utf-8"
    ),
    "csv_delimiter": ";",
    "csv_encoding": "utf-8",
    "csv_has_header": True,

    "csv_column_mapping": {
        # Идентификация
        "external_id": {"column": "code", "type": "string"},
        "vendor_code": {"column": "article", "type": "string"},
        "title": {"column": "title", "type": "string"},

        # Бренд и категории
        "brand": {"column": "brand_title", "type": "string"},
        "categories": {"column": "category_title", "type": "list", "separator": "/"},

        # Описание и страна
        "description": {"column": "description", "type": "string"},
        "country": {"column": "country", "type": "string"},

        # Цены
        "price": {"column": "price", "type": "number"},
        "recommended_retail_price": {"column": "retail_price", "type": "number"},

        # Характеристики товара
        "colors": {"column": "color", "type": "list", "separator": ","},
        "materials": {"column": "material", "type": "list", "separator": ","},
        "sizes_raw": {"column": "size", "type": "string"},
        "barcodes": {"column": "barcodes", "type": "list", "separator": ","},

        # Остатки по складам (суммируются)
        "supplier_quantity": {
            "columns": ["msk", "spb", "tmn", "rst", "nsk", "ast"],
            "type": "stock_sum"
        },

        # Фото (прямые URL из нескольких колонок)
        "photo_urls": {
            "columns": ["image", "image1", "image2"],
            "type": "photo_urls"
        },

        # Физические характеристики
        "characteristics": {
            "columns": {
                "length": "Длина, см",
                "width": "Ширина, см",
                "weight": "Вес, кг",
                "battery": "Тип батареек",
                "waterproof": "Водонепроницаемость"
            },
            "type": "characteristics"
        },

        # Габариты упаковки
        "dimensions": {
            "columns": {
                "width_packed": "Ширина упаковки, см",
                "height_packed": "Высота упаковки, см",
                "length_packed": "Длина упаковки, см",
                "weight_packed": "Вес упаковки, кг"
            },
            "type": "characteristics"
        },
    },

    "is_active": True,
    "auto_sync_prices": False,
    "resize_images": True,
    "image_target_size": 1200,
    "image_background_color": "white",
}


def main():
    app = create_app()
    with app.app_context():
        existing = Supplier.query.filter_by(code='sexopt').first()
        if existing:
            print(f"Поставщик 'sexopt' уже существует (id={existing.id}). "
                  f"Обновляю конфигурацию...")
            for key, value in SEXOPT_SUPPLIER_DATA.items():
                if key != 'code':
                    setattr(existing, key, value)
            db.session.commit()
            print(f"Обновлено: {existing}")
            return existing.id

        from services.supplier_service import SupplierService
        supplier = SupplierService.create_supplier(SEXOPT_SUPPLIER_DATA)
        print(f"Создан поставщик: {supplier} (id={supplier.id})")
        print(f"CSV URL: {supplier.csv_source_url[:80]}...")
        print(f"Mapping fields: {list(supplier.csv_column_mapping.keys())}")
        print(f"\nДля синхронизации каталога:")
        print(f"  SupplierService.sync_from_csv({supplier.id})")
        return supplier.id


if __name__ == '__main__':
    supplier_id = main()
    print(f"\nSupplier ID: {supplier_id}")
