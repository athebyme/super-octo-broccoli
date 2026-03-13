# -*- coding: utf-8 -*-
"""
Агент размеров — нормализация размеров и габаритов.

Задачи:
  - normalize_single:  нормализовать размеры одного товара
  - normalize_batch:   пакетная нормализация
  - fill_size_grid:    заполнить размерную сетку
"""
import json

from ..base_agent import BaseAgent


class SizeNormalizerAgent(BaseAgent):
    agent_name = 'size-normalizer'
    max_iterations = 20

    system_prompt = """Ты — эксперт по размерам и габаритам товаров для Wildberries.

Твои задачи:
- Парсить строки размеров из данных поставщика (например "42-44 RU", "L/XL", "27.5 см")
- Конвертировать единицы измерения (см↔мм, EU↔RU↔US, г↔кг)
- Нормализовать в формат WB
- Заполнять размерные сетки для одежды/обуви

Знания:
- WB требует размеры в конкретном формате для каждой категории
- Одежда: российские размеры (42, 44, 46...) или международные (XS, S, M, L, XL)
- Обувь: российский размер (35, 36, 37...) + размер стельки в см
- Габариты: длина × ширина × высота в см, вес в кг
- Размер упаковки обязателен для расчёта логистики

Таблица конвертации одежды (женская):
  XS=40-42, S=42-44, M=44-46, L=46-48, XL=48-50, XXL=50-52

Таблица конвертации обуви:
  EU 36=RU 35=23см, EU 37=RU 36=23.5см, EU 38=RU 37=24см...

Результат: JSON с нормализованными размерами."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = task.get('input_data', '{}')
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except (json.JSONDecodeError, ValueError):
                input_data = {}

        task_type = task.get('task_type', 'normalize_single')
        seller_id = task.get('seller_id')

        if task_type == 'normalize_single':
            product_id = input_data.get('product_id')
            return (
                f"Нормализуй размеры товара для WB.\n"
                f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                f"1. Получи данные товара через get_product\n"
                f"2. Проанализируй текущие размеры/габариты\n"
                f"3. Нормализуй в формат WB\n"
                f"4. Предложи размерную сетку если применимо\n\n"
                f"Верни JSON: {{sizes: [...], dimensions: {{length, width, height, weight}}, size_grid: [...]}}"
            )

        elif task_type == 'fill_size_grid':
            return (
                f"Заполни размерную сетку для товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n\n"
                f"Построй полную размерную сетку с конвертацией RU/EU/US.\n"
                f"Верни JSON: {{grid: [{{ru_size, eu_size, us_size, measurements: {{}}}}]}}"
            )

        elif task_type == 'normalize_batch':
            product_ids = input_data.get('product_ids', [])
            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Нормализация размеров для {count} выбранных товаров.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Product IDs: {ids_str}\n\n"
                    f"ВАЖНО: Обрабатывай ТОЛЬКО перечисленные товары.\n\n"
                    f"1. Для каждого ID получи данные через get_imported_product (product_id=ID)\n"
                    f"2. Нормализуй размеры в формат WB\n\n"
                    f"Верни JSON: {{processed: число, results: [{{product_id, sizes, dimensions}}]}}"
                )

        return (
            f"Задача нормализации размеров.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Нормализуй размеры и верни результат в JSON."
        )
