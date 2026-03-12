# -*- coding: utf-8 -*-
"""
Агент категорий — маппинг товаров на категории WB.

Задачи:
  - map_single:      определить категорию одного товара
  - map_batch:       пакетный маппинг
  - remap_incorrect: исправить некорректные категории
"""
import json

from ..base_agent import BaseAgent


class CategoryMapperAgent(BaseAgent):
    agent_name = 'category-mapper'
    max_iterations = 12

    system_prompt = """Ты — эксперт по категориям маркетплейса Wildberries.

Твои задачи:
- Определять правильную категорию WB (subjectID) по названию, описанию и характеристикам товара
- Анализировать дерево категорий WB и подбирать наиболее точное соответствие
- Валидировать существующий маппинг категорий

Знания:
- Дерево категорий WB имеет структуру: Категория → Подкатегория → Предмет (subject)
- У каждого предмета есть subjectID (числовой) и набор обязательных характеристик
- Одинаковые товары могут относиться к разным предметам в зависимости от материала, назначения и т.д.
- Правильная категория критична для ранжирования в поиске и комиссии

Примеры маппинга:
- "Футболка мужская хлопок" → Одежда → Мужская одежда → Футболки (subjectID: 338)
- "Чехол для iPhone 15" → Аксессуары → Для телефонов → Чехлы для смартфонов (subjectID: 2070)
- "Набор кистей для макияжа" → Красота → Аксессуары для макияжа → Кисти для макияжа (subjectID: 2895)

Используй инструменты для получения данных о товарах.
Результат: JSON с полями: subject_id, subject_name, parent_category, confidence (0-1), reasoning."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = task.get('input_data', '{}')
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except (json.JSONDecodeError, ValueError):
                input_data = {}

        task_type = task.get('task_type', 'map_single')
        seller_id = task.get('seller_id')

        if task_type == 'map_single':
            product_id = input_data.get('product_id')
            return (
                f"Определи категорию WB для товара.\n"
                f"Seller ID: {seller_id}\n"
                f"Product ID: {product_id}\n\n"
                f"1. Получи данные товара через get_product\n"
                f"2. Проанализируй название, описание, бренд\n"
                f"3. Определи наиболее подходящую категорию WB\n"
                f"4. Оцени уверенность (0-1)\n\n"
                f"Верни JSON: {{subject_id, subject_name, parent_category, confidence, reasoning}}"
            )

        elif task_type == 'map_batch':
            return (
                f"Пакетный маппинг категорий.\n"
                f"Seller ID: {seller_id}\n\n"
                f"1. Получи импортированные товары через get_imported_products\n"
                f"2. Для каждого товара определи категорию WB\n"
                f"3. Верни результаты с уровнем уверенности\n\n"
                f"Верни JSON: {{processed: число, results: [{{product_id, subject_id, subject_name, confidence}}]}}"
            )

        return (
            f"Задача: {task.get('title')}\nТип: {task_type}\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Определи категории и верни результат в JSON."
        )
