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

КРИТИЧЕСКИЕ ПРАВИЛА:
- Для определения допустимых размеров в категории используй get_category_characteristics(subject_id=...)
  чтобы узнать формат и допустимые значения размеров
- Для импортированных товаров ВСЕГДА используй update_imported_product (НЕ update_product)
- Не повторяй вызовы — каждый инструмент вызывай ровно 1 раз на товар

Таблица конвертации одежды (женская):
  XS=40-42, S=42-44, M=44-46, L=46-48, XL=48-50, XXL=50-52

Таблица конвертации обуви:
  EU 36=RU 35=23см, EU 37=RU 36=23.5см, EU 38=RU 37=24см...

Результат: JSON с нормализованными размерами."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'normalize_single')
        seller_id = task.get('seller_id')

        if task_type == 'normalize_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            target_id = imported_product_id or product_id
            get_cmd = f"get_imported_product(product_id={target_id})" if imported_product_id else f"get_product(seller_id={seller_id}, product_id={product_id})"

            return (
                f"Нормализуй размеры товара для WB.\n"
                f"{'Imported Product' if imported_product_id else 'Product'} ID: {target_id}\n\n"
                f"Шаги:\n"
                f"1. {get_cmd} — получи данные товара\n"
                f"2. get_category_characteristics(subject_id=<wb_subject_id>) — узнай допустимые характеристики размеров\n"
                f"3. Проанализируй текущие размеры/габариты\n"
                f"4. Нормализуй в формат WB согласно характеристикам категории\n"
                f"5. update_imported_product(product_id={target_id}, sizes=<JSON размеров>)\n\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для сохранения.\n"
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
