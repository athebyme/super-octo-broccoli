#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Скрипт для получения полного списка категорий WB для взрослых товаров
"""
import requests
import json

def fetch_wb_adult_categories():
    """Получает список всех категорий для взрослых товаров из WB API"""

    url = "https://content-api.wildberries.ru/content/v2/object/all"

    params = {
        'parentID': 5038,  # Товары для взрослых
        'locale': 'ru',
        'limit': 1000,
        'offset': 0
    }

    print(f"🔄 Запрос к WB API: {url}")
    print(f"   Параметры: {params}")

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        data = response.json()

        if data.get('error'):
            print(f"❌ Ошибка API: {data.get('errorText')}")
            return None

        categories = data.get('data', [])
        print(f"\n✅ Получено категорий: {len(categories)}")

        # Сортируем по ID для удобства
        categories_sorted = sorted(categories, key=lambda x: x['subjectID'])

        print("\n📋 Список всех категорий WB для взрослых (parentID=5038):")
        print("="*80)

        wb_dict = {}
        for cat in categories_sorted:
            subject_id = cat['subjectID']
            subject_name = cat['subjectName']
            wb_dict[subject_id] = subject_name
            print(f"  {subject_id}: \"{subject_name}\"")

        # Сохраняем в файл
        with open('wb_categories_full.json', 'w', encoding='utf-8') as f:
            json.dump(categories_sorted, f, ensure_ascii=False, indent=2)

        print(f"\n💾 Данные сохранены в wb_categories_full.json")

        # Сохраняем Python dict для копирования
        with open('wb_categories_dict.txt', 'w', encoding='utf-8') as f:
            f.write("WB_ADULT_CATEGORIES = {\n")
            for subject_id, subject_name in wb_dict.items():
                f.write(f"    {subject_id}: \"{subject_name}\",\n")
            f.write("}\n")

        print(f"💾 Python dict сохранен в wb_categories_dict.txt")

        return wb_dict

    except requests.exceptions.RequestException as e:
        print(f"❌ Ошибка запроса: {e}")
        return None
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return None


if __name__ == '__main__':
    categories = fetch_wb_adult_categories()

    if categories:
        print(f"\n✅ Всего категорий: {len(categories)}")

        # Сравниваем с текущим маппингом
        try:
            from services.wb_categories_mapping import WB_ADULT_CATEGORIES

            current_ids = set(WB_ADULT_CATEGORIES.keys())
            api_ids = set(categories.keys())

            missing = api_ids - current_ids
            extra = current_ids - api_ids

            if missing:
                print(f"\n⚠️  Отсутствуют в маппинге ({len(missing)}):")
                for cat_id in sorted(missing):
                    print(f"  {cat_id}: {categories[cat_id]}")

            if extra:
                print(f"\n⚠️  Есть в маппинге, но нет в API ({len(extra)}):")
                for cat_id in sorted(extra):
                    print(f"  {cat_id}: {WB_ADULT_CATEGORIES[cat_id]}")

            if not missing and not extra:
                print("\n✅ Маппинг полностью совпадает с API!")

        except ImportError:
            print("\n⚠️  Не удалось импортировать wb_categories_mapping для сравнения")
