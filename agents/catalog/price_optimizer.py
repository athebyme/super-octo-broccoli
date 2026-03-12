# -*- coding: utf-8 -*-
"""
Агент цен — оптимизация ценообразования, unit-экономика, анализ маржинальности.
"""
import json

from ..base_agent import BaseAgent


class PriceOptimizerAgent(BaseAgent):
    agent_name = 'price-optimizer'
    max_iterations = 10
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
        input_data = task.get('input_data', '{}')
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except (json.JSONDecodeError, ValueError):
                input_data = {}

        task_type = task.get('task_type', 'optimize_prices')
        seller_id = task.get('seller_id')

        if task_type == 'optimize_prices':
            return (
                f"Оптимизируй цены товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n\n"
                f"1. Получи товары через get_products\n"
                f"2. Рассчитай unit-экономику для каждого\n"
                f"3. Определи товары с отрицательной/низкой маржой\n"
                f"4. Предложи оптимальные цены\n\n"
                f"Верни JSON: {{products: [{{product_id, current_price, suggested_price, margin_pct, reasoning}}]}}"
            )

        elif task_type == 'margin_audit':
            return (
                f"Аудит маржинальности.\n"
                f"Seller ID: {seller_id}\n\n"
                f"1. Получи все товары\n"
                f"2. Рассчитай маржу по каждому\n"
                f"3. Выдели проблемные (маржа < 20%)\n"
                f"4. Подготовь отчёт\n\n"
                f"Верни JSON: {{total, avg_margin, problematic: [...], recommendations: [...]}}"
            )

        return (
            f"Задача по ценам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Оптимизируй ценообразование и верни результат в JSON."
        )
