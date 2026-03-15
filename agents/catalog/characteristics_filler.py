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

ПРАВИЛА РАБОТЫ:
- Для импортированных товаров ВСЕГДА используй update_imported_product (НЕ update_product)
- Не вызывай get_imported_products если ID товаров уже известны
- Не повторяй вызовы — каждый инструмент вызывай ровно 1 раз на товар
- Сразу после заполнения характеристик — сохрани через update_imported_product

Результат: JSON со списком заполненных характеристик."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'fill_single')
        seller_id = task.get('seller_id')

        if task_type == 'fill_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            if imported_product_id:
                return (
                    f"Заполни характеристики импортированного товара.\n"
                    f"Imported Product ID: {imported_product_id}\n\n"
                    f"1. get_imported_product(product_id={imported_product_id})\n"
                    f"2. Определи категорию и обязательные характеристики\n"
                    f"3. Извлеки данные из названия и описания\n"
                    f"4. update_imported_product(product_id={imported_product_id}, characteristics=...)\n\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для сохранения.\n"
                    f"Верни JSON: {{characteristics: {{key: value, ...}}, "
                    f"filled_count, missing: [...], confidence}}"
                )

            if product_id:
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

            return f"Ошибка: не указан product_id или imported_product_id."

        elif task_type == 'fill_batch':
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )

            # 1 товар → делегируем в single
            if len(product_ids) == 1:
                return self.build_task_prompt({
                    **task,
                    'task_type': 'fill_single',
                    'input_data': json.dumps({
                        'imported_product_id': product_ids[0],
                        'seller_id': seller_id,
                    }),
                })

            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Заполни характеристики для {count} выбранных товаров.\n"
                    f"Product IDs: [{ids_str}]\n\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products.\n\n"
                    f"Для каждого ID:\n"
                    f"1. get_imported_product(product_id=ID)\n"
                    f"2. Определи обязательные характеристики для категории\n"
                    f"3. update_imported_product(product_id=ID, characteristics=...)\n\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                    f"Верни JSON: {{processed: число, saved: число, results: [{{product_id, filled_count, missing: [...]}}]}}"
                )

            limit = input_data.get('limit', 10)
            return (
                f"Пакетное заполнение характеристик.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого товара определи и заполни характеристики\n"
                f"3. Для каждого: update_imported_product(product_id=ID, characteristics=...)\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
            )

        elif task_type == 'validate_existing':
            limit = input_data.get('limit', 10)
            return (
                f"Валидация характеристик товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проверь максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Проверь заполненность обязательных характеристик\n"
                f"3. Найди ошибки и пустые поля\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n\n"
                f"Верни JSON: {{total, valid, issues: [{{product_id, missing: [...], errors: [...]}}]}}"
            )

        return (
            f"Задача по характеристикам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Заполни характеристики, сохрани через update_imported_product и верни результат в JSON."
        )
