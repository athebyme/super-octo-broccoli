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
- Искать категорию ТОЛЬКО через инструмент search_wb_categories

КРИТИЧЕСКИЕ ПРАВИЛА:
- ЗАПРЕЩЕНО выдумывать категории! ОБЯЗАТЕЛЬНО используй search_wb_categories для поиска
- Ищи по ключевым словам из названия товара (например "лубрикант", "футболка")
- Если первый поиск не дал результатов — попробуй синонимы или более общий запрос
- Используй ТОЛЬКО subject_id из результатов search_wb_categories
- Для импортированных товаров ВСЕГДА используй update_imported_product (НЕ update_product)
- Не вызывай get_imported_products если ID товаров уже известны
- Не повторяй вызовы — каждый инструмент вызывай ровно 1 раз на товар
- Сразу после определения категории — сохрани через update_imported_product

Результат: JSON с полями: subject_id, subject_name, parent_category, confidence (0-1), reasoning.

БЕЗОПАСНОСТЬ: Ты НЕ имеешь доступа к API-ключам, паролям или конфиденциальным данным продавцов."""

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
                    f"Imported Product ID: {imported_product_id}\n\n"
                    f"Шаги:\n"
                    f"1. get_imported_product(product_id={imported_product_id})\n"
                    f"2. search_wb_categories(query=<ключевое слово из названия товара>)\n"
                    f"3. Выбери наиболее подходящую категорию из результатов поиска\n"
                    f"4. update_imported_product(product_id={imported_product_id}, wb_subject_id=<subject_id из поиска>, mapped_wb_category=<subject_name>)\n\n"
                    f"ЗАПРЕЩЕНО выдумывать категории — используй ТОЛЬКО результаты search_wb_categories.\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для сохранения.\n"
                    f"Верни JSON: {{subject_id, subject_name, confidence, reasoning}}"
                )

            if product_id:
                return (
                    f"Определи категорию WB для товара.\n"
                    f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                    f"Шаги:\n"
                    f"1. get_product(seller_id={seller_id}, product_id={product_id})\n"
                    f"2. search_wb_categories(query=<ключевое слово из названия>)\n"
                    f"3. Выбери наиболее подходящую категорию из результатов\n"
                    f"4. update_product(seller_id={seller_id}, product_id={product_id}, wb_category_id=<subject_id>, wb_category_name=<subject_name>)\n\n"
                    f"ЗАПРЕЩЕНО выдумывать категории — используй ТОЛЬКО результаты search_wb_categories.\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_product для сохранения.\n"
                    f"Верни JSON: {{subject_id, subject_name, confidence, reasoning}}"
                )

            return f"Ошибка: не указан product_id или imported_product_id."

        elif task_type == 'map_batch':
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )
            products_data = input_data.get('products_data', [])
            limit = input_data.get('limit', 10)

            # 1 товар → делегируем в single
            if len(product_ids) == 1 and not products_data:
                return self.build_task_prompt({
                    **task,
                    'task_type': 'map_single',
                    'input_data': json.dumps({
                        'imported_product_id': product_ids[0],
                        'seller_id': seller_id,
                    }),
                })

            # Данные уже переданы
            if products_data:
                products_json = json.dumps(products_data[:20], ensure_ascii=False, indent=2)
                return (
                    f"Пакетный маппинг категорий. Данные уже загружены.\n\n"
                    f"Товары:\n{products_json}\n\n"
                    f"Для каждого товара:\n"
                    f"1. search_wb_categories(query=<ключевое слово из названия>) — найди категорию\n"
                    f"2. update_imported_product(product_id=ID, wb_subject_id=<subject_id из поиска>, mapped_wb_category=<subject_name>)\n\n"
                    f"ЗАПРЕЩЕНО выдумывать категории — используй ТОЛЬКО результаты search_wb_categories.\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products — данные уже есть выше.\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                    f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
                )

            # Конкретные IDs
            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Пакетный маппинг категорий для {count} товаров.\n"
                    f"Product IDs: [{ids_str}]\n\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products.\n\n"
                    f"Для каждого ID:\n"
                    f"1. get_imported_product(product_id=ID)\n"
                    f"2. search_wb_categories(query=<ключевое слово из названия>) — найди категорию\n"
                    f"3. update_imported_product(product_id=ID, wb_subject_id=<subject_id из поиска>, mapped_wb_category=<subject_name>)\n\n"
                    f"ЗАПРЕЩЕНО выдумывать категории — используй ТОЛЬКО результаты search_wb_categories.\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                    f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
                )

            # Без IDs — загружаем страницу
            return (
                f"Пакетный маппинг категорий.\n"
                f"Seller ID: {seller_id}, лимит: {limit}\n\n"
                f"Шаги:\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого товара:\n"
                f"   a. search_wb_categories(query=<ключевое слово>) — найди категорию\n"
                f"   b. update_imported_product(product_id=ID, wb_subject_id=<subject_id из поиска>, mapped_wb_category=<subject_name>)\n\n"
                f"ЗАПРЕЩЕНО выдумывать категории — используй ТОЛЬКО результаты search_wb_categories.\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
            )

        return (
            f"Задача: {task.get('title')}\nТип: {task_type}\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Определи категории через search_wb_categories, сохрани через update_imported_product и верни результат."
        )
