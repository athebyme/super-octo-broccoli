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
    max_iterations = 25

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
            imported_product_id = input_data.get('imported_product_id')

            if imported_product_id:
                return (
                    f"Определи категорию WB для импортированного товара.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Imported Product ID: {imported_product_id}\n\n"
                    f"1. Получи данные товара через get_imported_product (product_id={imported_product_id})\n"
                    f"2. Проанализируй название, описание, бренд, характеристики\n"
                    f"3. Определи наиболее подходящую категорию WB (предмет/subject)\n"
                    f"4. Оцени уверенность (0-1)\n"
                    f"5. Сохрани категорию: update_product(product_id={imported_product_id}, data={{\"wb_category_id\": subject_id}})\n\n"
                    f"ВАЖНО: ОБЯЗАТЕЛЬНО вызови update_product для сохранения категории.\n\n"
                    f"Верни JSON: {{subject_id, subject_name, parent_category, confidence, reasoning}}"
                )

            return (
                f"Определи категорию WB для товара.\n"
                f"Seller ID: {seller_id}\n"
                f"Product ID: {product_id}\n\n"
                f"1. Получи данные товара через get_product\n"
                f"2. Проанализируй название, описание, бренд\n"
                f"3. Определи наиболее подходящую категорию WB\n"
                f"4. Оцени уверенность (0-1)\n"
                f"5. Сохрани категорию: update_product(product_id={product_id}, data={{\"wb_category_id\": subject_id}})\n\n"
                f"ВАЖНО: ОБЯЗАТЕЛЬНО вызови update_product для сохранения категории.\n\n"
                f"Верни JSON: {{subject_id, subject_name, parent_category, confidence, reasoning}}"
            )

        elif task_type == 'map_batch':
            # Поддержка обоих ключей: product_ids (из UI) и imported_product_ids (legacy)
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )
            limit = input_data.get('limit', 10)

            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Пакетный маппинг категорий для {count} импортированных товаров.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Product IDs: {ids_str}\n"
                    f"Всего товаров: {count}\n\n"
                    f"ВАЖНО: Обрабатывай ТОЛЬКО перечисленные выше товары.\n"
                    f"НЕ загружай список всех товаров продавца.\n\n"
                    f"Для каждого ID:\n"
                    f"1. Получи данные через get_imported_product (product_id=ID)\n"
                    f"2. По названию, описанию, бренду определи категорию WB\n"
                    f"3. Сохрани категорию: update_product(product_id=ID, data={{\"wb_category_id\": subject_id}})\n\n"
                    f"ВАЖНО: После определения категории ОБЯЗАТЕЛЬНО вызови update_product для сохранения.\n\n"
                    f"Верни JSON: {{processed: число, saved: число, results: [{{product_id, subject_id, subject_name, confidence}}]}}"
                )

            return (
                f"Пакетный маппинг категорий.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_imported_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Для каждого товара определи категорию WB по названию, описанию, бренду\n"
                f"3. Для каждого товара сохрани категорию: update_product(product_id=ID, data={{\"wb_category_id\": subject_id}})\n\n"
                f"ВАЖНО:\n"
                f"- Загрузи товары ОДНИМ вызовом. НЕ листай страницы.\n"
                f"- После определения категории ОБЯЗАТЕЛЬНО вызови update_product для сохранения.\n"
                f"- Не загружай каждый товар отдельно — данных из списка достаточно.\n\n"
                f"Верни JSON: {{processed: число, saved: число, results: [{{product_id, subject_id, subject_name, confidence}}]}}"
            )

        return (
            f"Задача: {task.get('title')}\nТип: {task_type}\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Определи категории и верни результат в JSON."
        )
