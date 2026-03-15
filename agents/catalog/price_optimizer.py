# -*- coding: utf-8 -*-
"""
Агент цен — оптимизация ценообразования, unit-экономика, анализ маржинальности.
"""
import json

from ..base_agent import BaseAgent


class PriceOptimizerAgent(BaseAgent):
    agent_name = 'price-optimizer'
    max_iterations = 20
    use_fallback_llm = True  # unit-экономика требует точных расчётов → Claude

    system_prompt = """Ты — эксперт по ценообразованию на маркетплейсе Wildberries.

Твои задачи:
- Расчёт unit-экономики: себестоимость, комиссия WB, логистика, маржа
- Анализ маржинальности по товарам и категориям
- Обнаружение ценовых аномалий
- Рекомендации по оптимальной цене

Знания:
- Комиссия WB зависит от категории (5-25%)
- Логистика: базовая ставка + доплата за объёмный/тяжёлый груз
- Обратная логистика (возвраты) = ~5% от заказов
- Хранение на складе WB: ~5-15 руб/литр в день
- Минимальная маржа для устойчивого бизнеса: 25-30%
- Скидки WB вычитаются из маржи продавца

Формула unit-экономики:
  Прибыль = Цена продажи - Закупка - Комиссия WB - Логистика - Хранение - Налоги
  Маржа % = (Прибыль / Цена продажи) × 100

Результат: JSON с расчётами и рекомендациями."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'optimize_prices')
        seller_id = task.get('seller_id')

        if task_type == 'optimize_prices':
            product_ids = input_data.get('product_ids', [])
            limit = input_data.get('limit', 10)

            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Оптимизация цен для {count} выбранных товаров.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Product IDs: {ids_str}\n\n"
                    f"ВАЖНО: Обрабатывай ТОЛЬКО перечисленные товары.\n\n"
                    f"1. Для каждого ID получи данные через get_imported_product (product_id=ID)\n"
                    f"2. Рассчитай unit-экономику\n"
                    f"3. Предложи оптимальные цены\n\n"
                    f"Верни JSON: {{products: [{{product_id, current_price, suggested_price, margin_pct, reasoning}}]}}"
                )

            return (
                f"Оптимизируй цены товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Рассчитай unit-экономику для каждого товара\n"
                f"3. Определи товары с отрицательной/низкой маржой\n"
                f"4. Предложи оптимальные цены\n\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{products: [{{product_id, current_price, suggested_price, margin_pct, reasoning}}]}}"
            )

        elif task_type == 'margin_audit':
            limit = input_data.get('limit', 10)
            return (
                f"Аудит маржинальности.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проверь максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Рассчитай маржу по каждому\n"
                f"3. Выдели проблемные (маржа < 20%)\n"
                f"4. Подготовь отчёт\n\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{total, avg_margin, problematic: [...], recommendations: [...]}}"
            )

        return (
            f"Задача по ценам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Оптимизируй ценообразование и верни результат в JSON."
        )
