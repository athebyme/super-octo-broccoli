# -*- coding: utf-8 -*-
"""
Агент модерации — диагностика блокировок, проверка на стоп-слова, compliance.
"""
import json

from ..base_agent import BaseAgent


class CardDoctorAgent(BaseAgent):
    agent_name = 'card-doctor'
    max_iterations = 20
    use_fallback_llm = True  # модерация требует точных рассуждений → Claude

    system_prompt = """Ты — эксперт по модерации карточек Wildberries.

Твои задачи:
- Диагностировать причины блокировки/скрытия карточек
- Проверять на стоп-слова и нарушения правил WB
- Предлагать исправления для прохождения модерации
- Превентивный скан карточек перед публикацией

Знания о причинах блокировки:
- Стоп-слова: "лучший", "номер 1", "аналог", названия лекарств, медицинские заявления
- Неправильный бренд (не совпадает с регистрацией)
- Запрещённые категории товаров
- Некорректные фото (водяные знаки, контактные данные)
- Несоответствие описания товару
- Нарушение авторских прав
- Указание ссылок на другие площадки

Стоп-слова WB (частичный список):
- Медицина: "лечит", "исцеляет", "медицинский", "терапевтический"
- Маркетинг: "лучший", "единственный", "номер 1", "топ-1"
- Запрещённые: "аналог [бренда]", "реплика", "копия"
- Контакты: телефоны, email, ссылки, @username

Результат: JSON с диагностикой и рекомендациями."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = task.get('input_data', '{}')
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except (json.JSONDecodeError, ValueError):
                input_data = {}

        task_type = task.get('task_type', 'diagnose_single')
        seller_id = task.get('seller_id')

        if task_type == 'diagnose_single':
            product_id = input_data.get('product_id')
            return (
                f"Диагностика карточки WB.\n"
                f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                f"1. Получи данные товара через get_product\n"
                f"2. Проверь заголовок на стоп-слова\n"
                f"3. Проверь описание на нарушения\n"
                f"4. Проверь бренд и категорию\n"
                f"5. Оцени риск блокировки (0-10)\n\n"
                f"Верни JSON: {{risk_score, issues: [{{type, severity, text, suggestion}}], "
                f"clean: bool, recommendations: [...]}}"
            )

        elif task_type in ('diagnose_batch', 'preventive_scan'):
            product_ids = input_data.get('product_ids', [])
            limit = input_data.get('limit', 10)

            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Диагностика {count} выбранных карточек.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Product IDs: {ids_str}\n\n"
                    f"ВАЖНО: Обрабатывай ТОЛЬКО перечисленные товары.\n\n"
                    f"1. Для каждого ID получи данные через get_imported_product (product_id=ID)\n"
                    f"2. Проверь на стоп-слова и нарушения\n"
                    f"3. Подготовь отчёт о рисках\n\n"
                    f"Верни JSON: {{total, clean, at_risk, critical, issues: [...]}}"
                )

            return (
                f"Превентивный скан карточек.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проверь максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Проверь каждый товар из списка на стоп-слова и нарушения\n"
                f"3. Подготовь отчёт о рисках\n\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом и сразу анализируй.\n\n"
                f"Верни JSON: {{total, clean, at_risk, critical, issues: [...]}}"
            )

        return (
            f"Задача модерации.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Проведи диагностику и верни результат в JSON."
        )
