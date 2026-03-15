# -*- coding: utf-8 -*-
"""
Агент характеристик — заполнение обязательных и рекомендованных характеристик карточки WB.
"""
import json

from ..base_agent import BaseAgent


class CharacteristicsFillerAgent(BaseAgent):
    agent_name = 'characteristics-filler'
    max_iterations = 20

    system_prompt = """Ты — эксперт по характеристикам карточек Wildberries.

Твои задачи:
- Заполнять обязательные характеристики карточки WB
- Извлекать данные из описания поставщика
- Подбирать значения из словарей WB
- Валидировать заполненные характеристики

Знания:
- Каждая категория WB имеет свой набор обязательных и рекомендованных характеристик
- Многие характеристики — справочные (выбор из списка), а не свободные
- Незаполненные обязательные характеристики блокируют публикацию карточки
- Чем больше характеристик заполнено, тем лучше ранжирование

Типичные характеристики:
- Состав (%, материалы)
- Цвет (из справочника WB, ~150 значений)
- Страна производства
- Пол, возрастная группа
- Сезон, комплектация
- Тип упаковки, количество в упаковке

Результат: JSON со списком заполненных характеристик."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'fill_single')
        seller_id = task.get('seller_id')

        if task_type == 'fill_single':
            product_id = input_data.get('product_id')
            return (
                f"Заполни характеристики карточки WB.\n"
                f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                f"1. Получи данные товара через get_product\n"
                f"2. Определи категорию и обязательные характеристики\n"
                f"3. Извлеки данные из названия и описания\n"
                f"4. Заполни максимум характеристик\n\n"
                f"Верни JSON: {{characteristics: {{key: value, ...}}, "
                f"filled_count, missing: [...], confidence}}"
            )

        elif task_type == 'fill_batch':
            product_ids = input_data.get('product_ids', [])
            limit = input_data.get('limit', 10)

            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Заполни характеристики для {count} выбранных товаров.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Product IDs: {ids_str}\n\n"
                    f"ВАЖНО: Обрабатывай ТОЛЬКО перечисленные товары.\n\n"
                    f"1. Для каждого ID получи данные через get_imported_product (product_id=ID)\n"
                    f"2. Определи обязательные характеристики для категории\n"
                    f"3. Заполни характеристики и обнови через update_product\n\n"
                    f"Верни JSON: {{processed: число, results: [{{product_id, filled_count, missing: [...]}}]}}"
                )

            return (
                f"Пакетное заполнение характеристик.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Для каждого товара определи и заполни характеристики\n"
                f"3. Обнови через update_product\n\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{processed: число, results: [...]}}"
            )

        elif task_type == 'validate_existing':
            limit = input_data.get('limit', 10)
            return (
                f"Валидация характеристик товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проверь максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Проверь заполненность обязательных характеристик\n"
                f"3. Найди ошибки и пустые поля\n\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{total, valid, issues: [{{product_id, missing: [...], errors: [...]}}]}}"
            )

        return (
            f"Задача по характеристикам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Заполни характеристики и верни результат в JSON."
        )
