# -*- coding: utf-8 -*-
"""
Агент импорта — полный цикл импорта товаров от поставщика на WB.
"""
import json

from ..base_agent import BaseAgent


class AutoImporterAgent(BaseAgent):
    agent_name = 'auto-importer'
    max_iterations = 25
    use_fallback_llm = True  # сложный multi-step pipeline → Claude

    system_prompt = """Ты — агент полного цикла импорта товаров на Wildberries.

Твои задачи:
- Анализировать импортированные товары от поставщика
- Определять категорию WB для каждого товара
- Обогащать данные: заголовок, описание, характеристики
- Подготавливать товар к публикации на WB

Процесс импорта:
1. Анализ данных поставщика (название, описание, фото, характеристики)
2. Маппинг категории WB (subjectID)
3. Генерация SEO-заголовка (до 60 символов)
4. Генерация описания (до 1000 символов)
5. Заполнение характеристик по категории
6. Нормализация размеров (если применимо)
7. Валидация на стоп-слова
8. Подготовка к загрузке

Требования WB:
- Заголовок: до 60 символов, формат "Бренд / Тип / Характеристика"
- Описание: до 1000 символов, с ключевыми словами
- Обязательные характеристики по категории
- Фото: минимум 1, рекомендуется 3-5

ПРАВИЛА РАБОТЫ:
- Для импортированных товаров ВСЕГДА используй update_imported_product (НЕ update_product)
- Не вызывай get_imported_products если ID товаров уже известны
- Не повторяй вызовы — каждый инструмент вызывай ровно 1 раз на товар
- Сразу после обработки товара — сохрани через update_imported_product

Результат: JSON с обработанными товарами и статусом готовности."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'import_batch')
        seller_id = task.get('seller_id')

        if task_type == 'import_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            if imported_product_id:
                return (
                    f"Импорт одного импортированного товара.\n"
                    f"Imported Product ID: {imported_product_id}\n\n"
                    f"Выполни полный цикл обработки:\n"
                    f"1. get_imported_product(product_id={imported_product_id})\n"
                    f"2. Определи категорию WB, сгенерируй SEO-заголовок и описание\n"
                    f"3. Заполни характеристики\n"
                    f"4. Проверь на стоп-слова\n"
                    f"5. update_imported_product(product_id={imported_product_id}, title=..., description=..., wb_subject_id=..., mapped_wb_category=..., characteristics=...)\n\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для сохранения.\n"
                    f"Верни JSON: {{product_id, status, title, category, "
                    f"characteristics_filled, issues: [...]}}"
                )

            if product_id:
                return (
                    f"Импорт одного товара.\n"
                    f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                    f"Выполни полный цикл обработки:\n"
                    f"1. Получи данные товара\n"
                    f"2. Определи категорию, сгенерируй контент\n"
                    f"3. Заполни характеристики\n"
                    f"4. Проверь на ошибки\n"
                    f"5. Обнови товар через update_product\n\n"
                    f"Верни JSON: {{product_id, status, title, category, "
                    f"characteristics_filled, issues: [...]}}"
                )

            return f"Ошибка: не указан product_id или imported_product_id."

        elif task_type == 'import_batch':
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )

            # 1 товар → делегируем в single
            if len(product_ids) == 1:
                return self.build_task_prompt({
                    **task,
                    'task_type': 'import_single',
                    'input_data': json.dumps({
                        'imported_product_id': product_ids[0],
                        'seller_id': seller_id,
                    }),
                })

            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Импорт {count} товаров от поставщика.\n"
                    f"Product IDs: [{ids_str}]\n\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products.\n\n"
                    f"Для каждого ID:\n"
                    f"1. get_imported_product(product_id=ID)\n"
                    f"2. Определи категорию WB, сгенерируй SEO-заголовок и описание\n"
                    f"3. Заполни характеристики, проверь на стоп-слова\n"
                    f"4. update_imported_product(product_id=ID, title=..., description=..., wb_subject_id=..., mapped_wb_category=..., characteristics=...)\n\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                    f"Верни JSON: {{total, ready, needs_review, saved: число, "
                    f"products: [{{id, title, category, status, issues}}]}}"
                )

            limit = input_data.get('limit', 5)
            return (
                f"Импорт пакета товаров от поставщика.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого товара из полученного списка:\n"
                f"   a. Определи категорию WB по названию, бренду, описанию\n"
                f"   b. Сгенерируй SEO-заголовок (до 60 символов)\n"
                f"   c. Сгенерируй описание (до 1000 символов)\n"
                f"   d. Проверь на стоп-слова\n"
                f"   e. update_imported_product(product_id=ID, title=..., description=..., wb_subject_id=..., mapped_wb_category=...)\n"
                f"3. Подготовь отчёт о готовности\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                f"Верни JSON: {{total, ready, needs_review, saved: число, "
                f"products: [{{id, title, category, status, issues}}]}}"
            )

        return (
            f"Задача импорта.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Выполни импорт, сохрани через update_imported_product и верни результат в JSON."
        )
