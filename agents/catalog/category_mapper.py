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
После определения категории ОБЯЗАТЕЛЬНО сохрани её через update_product.
Результат: JSON с полями: subject_id, subject_name, parent_category, confidence (0-1), reasoning."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
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
                    f"4. Сохрани: update_product(seller_id={seller_id}, product_id={imported_product_id}, wb_category_id=..., wb_category_name=...)\n\n"
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
                f"4. Сохрани: update_product(seller_id={seller_id}, product_id={product_id}, wb_category_id=..., wb_category_name=...)\n\n"
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
            products_data = input_data.get('products_data', [])
            limit = input_data.get('limit', 10)

            # Лучший вариант: данные уже переданы — 0 вызовов API на загрузку
            if products_data:
                products_json = json.dumps(products_data[:20], ensure_ascii=False, indent=2)
                return (
                    f"Пакетный маппинг категорий. Данные товаров уже загружены.\n"
                    f"Seller ID: {seller_id}\n\n"
                    f"Товары:\n{products_json}\n\n"
                    f"Для каждого товара:\n"
                    f"1. По названию, описанию, бренду определи категорию WB\n"
                    f"2. Сохрани: update_product(seller_id={seller_id}, product_id=ID, wb_category_id=..., wb_category_name=...)\n\n"
                    f"НЕ ВЫЗЫВАЙ get_imported_products и get_product — данные уже есть выше.\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_product для каждого товара.\n\n"
                    f"Верни JSON: {{processed: число, saved: число, results: [{{product_id, subject_id, subject_name, confidence}}]}}"
                )

            # Есть конкретные IDs — загружать по одному
            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Пакетный маппинг категорий для {count} импортированных товаров.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Product IDs: {ids_str}\n\n"
                    f"ВАЖНО: Обрабатывай ТОЛЬКО перечисленные выше товары.\n"
                    f"НЕ загружай список всех товаров продавца.\n\n"
                    f"Для каждого ID:\n"
                    f"1. Получи данные через get_imported_product (product_id=ID)\n"
                    f"2. По названию, описанию, бренду определи категорию WB\n"
                    f"3. Сохрани: update_product(seller_id={seller_id}, product_id=ID, wb_category_id=..., wb_category_name=...)\n\n"
                    f"ВАЖНО: После определения категории ОБЯЗАТЕЛЬНО вызови update_product для сохранения.\n\n"
                    f"Верни JSON: {{processed: число, saved: число, results: [{{product_id, subject_id, subject_name, confidence}}]}}"
                )

            # Нет ни данных, ни IDs — загружаем одну страницу
            return (
                f"Пакетный маппинг категорий.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_imported_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Для каждого товара определи категорию WB по названию, описанию, бренду\n"
                f"3. Для каждого сохрани: update_product(seller_id={seller_id}, product_id=ID, wb_category_id=..., wb_category_name=...)\n\n"
                f"ВАЖНО:\n"
                f"- Загрузи товары ОДНИМ вызовом. НЕ листай страницы.\n"
                f"- ОБЯЗАТЕЛЬНО вызови update_product для каждого товара.\n"
                f"- Данных из списка достаточно для маппинга.\n\n"
                f"Верни JSON: {{processed: число, saved: число, results: [{{product_id, subject_id, subject_name, confidence}}]}}"
            )

        return (
            f"Задача: {task.get('title')}\nТип: {task_type}\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Определи категории, сохрани через update_product и верни результат в JSON."
        )
