# -*- coding: utf-8 -*-
"""
Агент брендов — распознавание, нормализация, валидация брендов.
"""
import json

from ..base_agent import BaseAgent


class BrandResolverAgent(BaseAgent):
    agent_name = 'brand-resolver'
    max_iterations = 20

    system_prompt = """Ты — эксперт по брендам на маркетплейсе Wildberries.

Твои задачи:
- Распознавать бренды из названий товаров поставщика
- Нормализовать написание (транслитерация, исправление ошибок)
- Проверять наличие бренда в реестре WB
- Подбирать корректное написание для карточки

Знания:
- WB имеет справочник брендов с точным написанием
- Неправильный бренд = блокировка карточки
- Частые ошибки: кириллица vs латиница, регистр, пробелы
- Некоторые бренды запрещены (контрафакт, фармацевтика)
- Если бренд неизвестен — можно указать "Нет бренда"

Примеры нормализации:
- "найк" → "Nike"
- "ADIDAS ORIGINALS" → "adidas Originals"
- "Самсунг" → "Samsung"
- "Без бренда" → "Нет бренда"

Результат: JSON с нормализованными брендами."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = task.get('input_data', '{}')
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except (json.JSONDecodeError, ValueError):
                input_data = {}

        task_type = task.get('task_type', 'resolve_single')
        seller_id = task.get('seller_id')

        if task_type == 'resolve_single':
            product_id = input_data.get('product_id')
            return (
                f"Определи и нормализуй бренд товара.\n"
                f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                f"1. Получи данные товара через get_product\n"
                f"2. Определи бренд из названия/описания\n"
                f"3. Нормализуй написание\n"
                f"4. Обнови товар через update_product\n\n"
                f"Верни JSON: {{original_brand, normalized_brand, "
                f"confidence, wb_registered: bool}}"
            )

        elif task_type == 'resolve_batch':
            product_ids = input_data.get('product_ids', [])
            limit = input_data.get('limit', 10)

            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Пакетная нормализация брендов для {count} товаров.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Product IDs: {ids_str}\n\n"
                    f"ВАЖНО: Обрабатывай ТОЛЬКО перечисленные товары.\n\n"
                    f"1. Для каждого ID получи данные через get_imported_product (product_id=ID)\n"
                    f"2. Нормализуй бренд\n"
                    f"3. Обнови каждый товар через update_product\n\n"
                    f"Верни JSON: {{total, updated, skipped, "
                    f"results: [{{product_id, original, normalized}}]}}"
                )

            return (
                f"Пакетная нормализация брендов.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Для каждого товара нормализуй бренд\n"
                f"3. Обнови товары с исправленными брендами через update_product\n\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{total, updated, skipped, "
                f"results: [{{product_id, original, normalized}}]}}"
            )

        elif task_type == 'audit_brands':
            limit = input_data.get('limit', 10)
            return (
                f"Аудит брендов.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проверь максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Проверь бренды на корректность\n"
                f"3. Найди потенциальные проблемы\n\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{total, correct, issues: [{{product_id, brand, issue}}]}}"
            )

        return (
            f"Задача по брендам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Нормализуй бренды и верни результат в JSON."
        )
