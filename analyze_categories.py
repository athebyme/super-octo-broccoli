#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Анализ всех уникальных категорий из CSV файлов sexoptovik
"""
import csv
import sys
from collections import defaultdict

def analyze_csv_categories(csv_file):
    """Анализирует категории в CSV файле"""
    categories = set()
    category_chains = set()  # Полные цепочки категорий
    category_counts = defaultdict(int)

    try:
        with open(csv_file, 'r', encoding='cp1251') as f:
            reader = csv.reader(f, delimiter=';')

            for row in reader:
                if len(row) < 17:
                    continue

                # Колонка 2: категория (может быть цепочка через >)
                category = row[2].strip()

                if not category or category == 'Категория':
                    continue

                # Сохраняем полную цепочку
                category_chains.add(category)
                category_counts[category] += 1

                # Разбиваем на части
                parts = [p.strip() for p in category.split('>')]
                for part in parts:
                    if part:
                        categories.add(part)

    except Exception as e:
        print(f"Ошибка при чтении {csv_file}: {e}")
        return set(), set(), {}

    return categories, category_chains, category_counts


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_categories.py <csv_file1> [csv_file2] ...")
        sys.exit(1)

    all_categories = set()
    all_chains = set()
    all_counts = defaultdict(int)

    for csv_file in sys.argv[1:]:
        print(f"\nАнализ файла: {csv_file}")
        cats, chains, counts = analyze_csv_categories(csv_file)
        all_categories.update(cats)
        all_chains.update(chains)
        for k, v in counts.items():
            all_counts[k] += v
        print(f"  Найдено категорий: {len(cats)}")
        print(f"  Найдено цепочек: {len(chains)}")

    print("\n" + "="*80)
    print(f"ВСЕГО УНИКАЛЬНЫХ КАТЕГОРИЙ: {len(all_categories)}")
    print(f"ВСЕГО УНИКАЛЬНЫХ ЦЕПОЧЕК: {len(all_chains)}")
    print("="*80)

    print("\n📊 ВСЕ УНИКАЛЬНЫЕ КАТЕГОРИИ (отсортировано):")
    print("-" * 80)
    for cat in sorted(all_categories):
        print(f"  - {cat}")

    print("\n\n📊 ВСЕ УНИКАЛЬНЫЕ ЦЕПОЧКИ КАТЕГОРИЙ (топ-50 по частоте):")
    print("-" * 80)
    sorted_chains = sorted(all_counts.items(), key=lambda x: x[1], reverse=True)
    for chain, count in sorted_chains[:50]:
        print(f"  [{count:4d}] {chain}")

    # Сохраняем в файл
    with open('category_analysis.txt', 'w', encoding='utf-8') as f:
        f.write("ВСЕ УНИКАЛЬНЫЕ КАТЕГОРИИ:\n")
        f.write("="*80 + "\n")
        for cat in sorted(all_categories):
            f.write(f"{cat}\n")

        f.write("\n\nВСЕ УНИКАЛЬНЫЕ ЦЕПОЧКИ (с частотой):\n")
        f.write("="*80 + "\n")
        for chain, count in sorted_chains:
            f.write(f"[{count:4d}] {chain}\n")

    print("\n\n✅ Результаты сохранены в category_analysis.txt")


if __name__ == '__main__':
    main()
