#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: добавление таблиц для автоимпорта товаров
"""
from pathlib import Path
from models import db, AutoImportSettings, ImportedProduct, CategoryMapping
from seller_platform import app

def run_migration():
    """Применяет миграцию"""
    print("Применение миграции: добавление таблиц автоимпорта...")

    with app.app_context():
        # Создаем таблицы
        db.create_all()

        print("✓ Таблицы созданы:")
        print("  - auto_import_settings")
        print("  - imported_products")
        print("  - category_mappings")

        # Добавляем предустановленные маппинги категорий
        print("\nДобавление предустановленных маппингов категорий...")

        predefined_mappings = [
            ('Вибраторы', 'sexoptovik', 'Вибраторы', 5994, 'Вибраторы', 0, True, 1.0),
            ('Фаллоимитаторы', 'sexoptovik', 'Фаллоимитаторы', 5995, 'Фаллоимитаторы', 0, True, 1.0),
            ('Белье эротическое', 'sexoptovik', 'Белье', 3, 'Белье', 0, True, 0.8),
            ('Белье', 'sexoptovik', 'Белье', 3, 'Белье', 0, True, 0.9),
            ('Костюмы эротические', 'sexoptovik', 'Эротические костюмы', 6007, 'Эротические костюмы', 0, True, 1.0),
            ('Игрушки для взрослых', 'sexoptovik', 'Игрушки для взрослых', 5993, 'Игрушки для взрослых', 0, True, 0.9),
            ('Массажеры', 'sexoptovik', 'Массажеры', 469, 'Массажеры', 0, True, 0.7),
            ('Лубриканты', 'sexoptovik', 'Лубриканты', 6003, 'Лубриканты', 0, True, 1.0),
            ('Смазки', 'sexoptovik', 'Лубриканты', 6003, 'Лубриканты', 0, True, 1.0),
            ('Наручники', 'sexoptovik', 'Наручники и фиксаторы', 5998, 'Наручники и фиксаторы', 0, True, 1.0),
            ('Маски', 'sexoptovik', 'Маски и повязки', 6000, 'Маски и повязки', 0, True, 0.8),
            ('Стимуляторы', 'sexoptovik', 'Стимуляторы', 5996, 'Стимуляторы', 0, True, 0.9),
            ('Анальные игрушки', 'sexoptovik', 'Анальные игрушки', 5997, 'Анальные игрушки', 0, True, 1.0),
            ('Вакуумные помпы', 'sexoptovik', 'Вакуумные помпы', 5999, 'Вакуумные помпы', 0, True, 1.0),
            ('Кольца', 'sexoptovik', 'Эрекционные кольца', 6001, 'Эрекционные кольца', 0, True, 0.9),
        ]

        added = 0
        for mapping_data in predefined_mappings:
            source_cat, source_type, wb_cat, wb_subj_id, wb_subj_name, priority, is_auto, confidence = mapping_data

            # Проверяем, не существует ли уже
            existing = CategoryMapping.query.filter_by(
                source_category=source_cat,
                source_type=source_type,
                wb_subject_id=wb_subj_id
            ).first()

            if not existing:
                mapping = CategoryMapping(
                    source_category=source_cat,
                    source_type=source_type,
                    wb_category_name=wb_cat,
                    wb_subject_id=wb_subj_id,
                    wb_subject_name=wb_subj_name,
                    priority=priority,
                    is_auto_mapped=is_auto,
                    confidence_score=confidence
                )
                db.session.add(mapping)
                added += 1

        db.session.commit()
        print(f"✓ Добавлено {added} маппингов категорий")

        print("\nМиграция успешно завершена!")
        print("\nДля использования автоимпорта:")
        print("1. Создайте настройки автоимпорта для продавца в ЛК")
        print("2. Укажите URL CSV файла и код поставщика")
        print("3. Запустите импорт вручную или настройте автоматический импорт")

if __name__ == '__main__':
    run_migration()
