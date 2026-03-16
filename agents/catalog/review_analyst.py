# -*- coding: utf-8 -*-
"""
Агент отзывов — анализ тональности, классификация проблем, инсайты.
"""
import json

from ..base_agent import BaseAgent


class ReviewAnalystAgent(BaseAgent):
    agent_name = 'review-analyst'
    max_iterations = 10

    system_prompt = """Ты — аналитик отзывов для маркетплейса Wildberries.

Твои задачи:
- Анализировать тональность отзывов (позитивные, нейтральные, негативные)
- Классифицировать проблемы из отзывов
- Выявлять тренды и паттерны
- Генерировать рекомендации по улучшению товаров

Категории проблем:
- Качество: брак, плохой материал, быстрый износ
- Размер: маломерит, большемерит, не соответствует таблице
- Доставка: повреждения, долгая доставка
- Описание: не соответствует фото, не та комплектация
- Упаковка: плохая упаковка, помятый товар
- Цена: завышена, не оправдывает ожиданий

Метрики:
- NPS (Net Promoter Score): (промоутеры - критики) / всего × 100
- CSI (Customer Satisfaction Index): средняя оценка
- Тренд: рост/падение рейтинга за период

Результат: JSON с аналитикой и рекомендациями."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'analyze_reviews')
        seller_id = task.get('seller_id')

        if task_type == 'analyze_reviews':
            product_ids = input_data.get('product_ids', [])
            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Анализ отзывов для {count} выбранных товаров.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Product IDs: {ids_str}\n\n"
                    f"ВАЖНО: Обрабатывай ТОЛЬКО перечисленные товары.\n\n"
                    f"1. Для каждого ID получи данные через get_imported_product (product_id=ID)\n"
                    f"2. Проанализируй доступные отзывы\n"
                    f"3. Классифицируй проблемы\n"
                    f"4. Выяви тренды\n\n"
                    f"Верни JSON: {{sentiment: {{positive, neutral, negative}}, "
                    f"issues: [{{category, count, examples}}], recommendations: [...]}}"
                )
            limit = input_data.get('limit', 10)
            return (
                f"Анализ отзывов по товарам продавца.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проанализируй максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Проанализируй доступные отзывы\n"
                f"3. Классифицируй проблемы\n"
                f"4. Выяви тренды\n\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{sentiment: {{positive, neutral, negative}}, "
                f"issues: [{{category, count, examples}}], recommendations: [...]}}"
            )

        elif task_type == 'product_insights':
            product_id = input_data.get('product_id')
            return (
                f"Глубокий анализ отзывов конкретного товара.\n"
                f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                f"1. Получи данные товара\n"
                f"2. Проанализируй отзывы\n"
                f"3. Выдели ключевые инсайты\n"
                f"4. Предложи улучшения для карточки\n\n"
                f"Верни JSON: {{rating_analysis, key_issues, strengths, "
                f"card_improvements: [...], product_improvements: [...]}}"
            )

        return (
            f"Задача по отзывам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Проанализируй отзывы и верни результат в JSON."
        )
