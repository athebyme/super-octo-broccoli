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

КРИТИЧЕСКИЕ ПРАВИЛА:
- ЗАПРЕЩЕНО использовать стандартные/выдуманные коэффициенты!
- ОБЯЗАТЕЛЬНО получи реальные настройки через get_pricing_settings(seller_id=...)
- Используй РЕАЛЬНЫЕ данные: комиссию WB, логистику, налоги, таблицу наценок из настроек продавца
- Закупочная цена (supplier_price) есть в данных товара — используй её

Формула unit-экономики (используй коэффициенты из get_pricing_settings):
  R = Закупка × tax_rate + logistics_cost + storage_cost + packaging_cost
  S = max(delivery_min, min(R × delivery_pct/100, delivery_max))
  Z = R + acquiring_cost + extra_cost + S + Наценка(из price_ranges)
  Y = Z × inflated_multiplier (цена до скидки)
  X = Z - SPP (цена со скидкой)

ЗАЩИТА ЦЕН (enforcement на уровне API):
- Платформа ЗАПРЕЩАЕТ установку цены ниже закупочной (supplier_price)
- Платформа ЗАПРЕЩАЕТ установку цены ниже порога: supplier_price × (1 + min_profit/100)
- min_profit берётся из настроек продавца (по умолчанию 20%)
- Если попытаться установить цену ниже порога — API вернёт ошибку 400
- Всегда проверяй расчёты перед сохранением

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
                    f"Шаги:\n"
                    f"1. get_pricing_settings(seller_id={seller_id}) — ОБЯЗАТЕЛЬНО получи реальные коэффициенты\n"
                    f"2. Для каждого ID: get_imported_product(product_id=ID) — получи данные и supplier_price\n"
                    f"3. Рассчитай unit-экономику по РЕАЛЬНОЙ формуле из настроек\n"
                    f"4. Предложи оптимальные цены\n\n"
                    f"ЗАПРЕЩЕНО использовать стандартные коэффициенты — бери ТОЛЬКО из get_pricing_settings.\n"
                    f"Верни JSON: {{products: [{{product_id, supplier_price, calculated_price, margin_pct, reasoning}}]}}"
                )

            return (
                f"Оптимизируй цены товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"Шаги:\n"
                f"1. get_pricing_settings(seller_id={seller_id}) — ОБЯЗАТЕЛЬНО получи реальные коэффициенты\n"
                f"2. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"3. Рассчитай unit-экономику по РЕАЛЬНОЙ формуле из настроек\n"
                f"4. Определи товары с отрицательной/низкой маржой\n"
                f"5. Предложи оптимальные цены\n\n"
                f"ЗАПРЕЩЕНО использовать стандартные коэффициенты — бери ТОЛЬКО из get_pricing_settings.\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{products: [{{product_id, supplier_price, calculated_price, margin_pct, reasoning}}]}}"
            )

        elif task_type == 'margin_audit':
            limit = input_data.get('limit', 10)
            return (
                f"Аудит маржинальности.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проверь максимум {limit} товаров.\n\n"
                f"Шаги:\n"
                f"1. get_pricing_settings(seller_id={seller_id}) — ОБЯЗАТЕЛЬНО получи реальные коэффициенты\n"
                f"2. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"3. Рассчитай маржу по каждому товару используя РЕАЛЬНЫЕ коэффициенты\n"
                f"4. Выдели проблемные (маржа < min_profit из настроек)\n"
                f"5. Подготовь отчёт\n\n"
                f"ЗАПРЕЩЕНО использовать стандартные коэффициенты — бери ТОЛЬКО из get_pricing_settings.\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{total, avg_margin, problematic: [...], recommendations: [...]}}"
            )

        return (
            f"Задача по ценам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Оптимизируй ценообразование и верни результат в JSON."
        )
