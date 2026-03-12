# -*- coding: utf-8 -*-
"""
Агент брендов — распознавание, нормализация, валидация брендов.
"""
import json

from ..base_agent import BaseAgent


class BrandResolverAgent(BaseAgent):
    agent_name = 'brand-resolver'
    max_iterations = 10

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
            return (
                f"Пакетная нормализация брендов.\n"
                f"Seller ID: {seller_id}\n\n"
                f"1. Получи товары через get_products\n"
                f"2. Для каждого нормализуй бренд\n"
                f"3. Обнови товары с исправленными брендами\n\n"
                f"Верни JSON: {{total, updated, skipped, "
                f"results: [{{product_id, original, normalized}}]}}"
            )

        elif task_type == 'audit_brands':
            return (
                f"Аудит брендов: проверь все товары на корректность.\n"
                f"Seller ID: {seller_id}\n\n"
                f"1. Получи товары\n"
                f"2. Проверь бренды на корректность\n"
                f"3. Найди потенциальные проблемы\n\n"
                f"Верни JSON: {{total, correct, issues: [{{product_id, brand, issue}}]}}"
            )

        return (
            f"Задача по брендам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Нормализуй бренды и верни результат в JSON."
        )
