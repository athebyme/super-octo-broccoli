# -*- coding: utf-8 -*-
"""
Агент характеристик — заполнение обязательных и рекомендованных характеристик карточки WB.
"""
import json

from ..base_agent import BaseAgent


class CharacteristicsFillerAgent(BaseAgent):
    agent_name = 'characteristics-filler'
    max_iterations = 12

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
        input_data = task.get('input_data', '{}')
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except (json.JSONDecodeError, ValueError):
                input_data = {}

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

        elif task_type == 'validate_existing':
            return (
                f"Валидация характеристик товаров.\n"
                f"Seller ID: {seller_id}\n\n"
                f"1. Получи товары через get_products\n"
                f"2. Проверь заполненность обязательных характеристик\n"
                f"3. Найди ошибки и пустые поля\n\n"
                f"Верни JSON: {{total, valid, issues: [{{product_id, missing: [...], errors: [...]}}]}}"
            )

        return (
            f"Задача по характеристикам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Заполни характеристики и верни результат в JSON."
        )
