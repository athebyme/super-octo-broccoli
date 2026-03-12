# -*- coding: utf-8 -*-
"""
Агент импорта — полный цикл импорта товаров от поставщика на WB.
"""
import json

from ..base_agent import BaseAgent


class AutoImporterAgent(BaseAgent):
    agent_name = 'auto-importer'
    max_iterations = 15

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

Результат: JSON с обработанными товарами и статусом готовности."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = task.get('input_data', '{}')
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except (json.JSONDecodeError, ValueError):
                input_data = {}

        task_type = task.get('task_type', 'import_batch')
        seller_id = task.get('seller_id')

        if task_type == 'import_batch':
            return (
                f"Импорт пакета товаров от поставщика.\n"
                f"Seller ID: {seller_id}\n\n"
                f"1. Получи импортированные товары через get_imported_products\n"
                f"2. Для каждого товара:\n"
                f"   a. Определи категорию WB\n"
                f"   b. Сгенерируй SEO-заголовок\n"
                f"   c. Сгенерируй описание\n"
                f"   d. Заполни характеристики\n"
                f"   e. Проверь на стоп-слова\n"
                f"3. Подготовь отчёт о готовности\n\n"
                f"Верни JSON: {{total, ready, needs_review, "
                f"products: [{{id, title, category, status, issues}}]}}"
            )

        elif task_type == 'import_single':
            product_id = input_data.get('product_id')
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

        return (
            f"Задача импорта.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Выполни импорт и верни результат в JSON."
        )
