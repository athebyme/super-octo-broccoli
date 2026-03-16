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
- Проверять на стоп-слова через check_text_prohibited (реальная база данных стоп-слов)
- Валидировать бренд через validate_brand (реальный реестр WB)
- Предлагать исправления для прохождения модерации
- Превентивный скан карточек перед публикацией

КРИТИЧЕСКИЕ ПРАВИЛА:
- ОБЯЗАТЕЛЬНО проверяй заголовок и описание через check_text_prohibited — это реальная база стоп-слов WB
- ОБЯЗАТЕЛЬНО проверяй бренд через validate_brand — это реальный реестр брендов WB
- НЕ полагайся на свой встроенный список стоп-слов — используй check_text_prohibited
- Для импортированных товаров используй get_imported_product (НЕ get_product)

Алгоритм диагностики:
1. Получить данные товара
2. check_text_prohibited(text=<заголовок>) — проверить заголовок
3. check_text_prohibited(text=<описание>) — проверить описание
4. validate_brand(brand_name=<бренд>, category_id=<wb_subject_id>) — проверить бренд
5. Оценить риск блокировки на основе РЕАЛЬНЫХ данных проверок
6. Предложить исправления

Результат: JSON с диагностикой и рекомендациями."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'diagnose_single')
        seller_id = task.get('seller_id')

        if task_type == 'diagnose_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            target_id = imported_product_id or product_id
            get_cmd = f"get_imported_product(product_id={target_id})" if imported_product_id else f"get_product(seller_id={seller_id}, product_id={product_id})"

            return (
                f"Диагностика карточки WB.\n"
                f"{'Imported Product' if imported_product_id else 'Product'} ID: {target_id}\n\n"
                f"Шаги:\n"
                f"1. {get_cmd} — получи данные товара\n"
                f"2. check_text_prohibited(text=<заголовок>) — ОБЯЗАТЕЛЬНО проверь заголовок\n"
                f"3. check_text_prohibited(text=<описание>) — ОБЯЗАТЕЛЬНО проверь описание\n"
                f"4. validate_brand(brand_name=<бренд>, category_id=<wb_subject_id>) — ОБЯЗАТЕЛЬНО проверь бренд\n"
                f"5. Оцени риск блокировки (0-10) на основе РЕАЛЬНЫХ данных проверок\n\n"
                f"ЗАПРЕЩЕНО полагаться на свой встроенный список стоп-слов.\n"
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
