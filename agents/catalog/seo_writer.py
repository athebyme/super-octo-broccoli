# -*- coding: utf-8 -*-
"""
Агент SEO — генерация SEO-оптимизированных заголовков и описаний.

Задачи:
  - seo_single:    оптимизировать одну карточку
  - seo_batch:     оптимизировать пакет карточек
  - rewrite_titles: переписать заголовки
"""
import json

from ..base_agent import BaseAgent
from ..tools import ToolRegistry


class SEOWriterAgent(BaseAgent):
    agent_name = 'seo-writer'
    max_iterations = 20

    system_prompt = """Ты — SEO-эксперт для маркетплейса Wildberries (WB).

Твои задачи:
- Генерировать SEO-оптимизированные заголовки карточек (до 60 символов)
- Писать описания с ключевыми словами (до 1000 символов)
- Анализировать и улучшать существующий контент

Правила WB для заголовков:
- Максимум 60 символов
- Формат: "Бренд / Тип товара / Ключевая характеристика"
- Без спецсимволов кроме / и ,
- Без слов "лучший", "номер 1", "хит продаж"
- Без указания цены и скидок

Правила WB для описаний:
- Максимум 1000 символов
- Естественное вхождение ключевых слов
- Преимущества товара, материал, назначение
- Без контактов, ссылок, запрещённых слов

Используй инструменты для получения данных о товарах, затем генерируй оптимизированный контент.
Финальный результат отдай в JSON с полями: title, description, keywords, changes_summary."""

    def get_tools(self) -> ToolRegistry:
        """SEO-специфичные инструменты обрабатываются внутри LLM через промпт."""
        return None

    def build_task_prompt(self, task: dict) -> str:
        input_data = task.get('input_data', '{}')
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except (json.JSONDecodeError, ValueError):
                input_data = {}

        task_type = task.get('task_type', 'seo_single')
        seller_id = task.get('seller_id')

        if task_type == 'seo_single':
            product_id = input_data.get('product_id')
            return (
                f"Оптимизируй SEO для товара.\n"
                f"Seller ID: {seller_id}\n"
                f"Product ID: {product_id}\n\n"
                f"1. Получи данные товара через get_product\n"
                f"2. Проанализируй текущий заголовок и описание\n"
                f"3. Сгенерируй оптимизированный заголовок (до 60 символов)\n"
                f"4. Сгенерируй SEO-описание (до 1000 символов)\n"
                f"5. Обнови товар через update_product\n\n"
                f"Верни JSON: {{title, description, keywords: [...], changes_summary}}"
            )

        elif task_type == 'seo_batch':
            product_ids = input_data.get('product_ids', [])
            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"SEO-оптимизация {count} выбранных товаров.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Product IDs: {ids_str}\n\n"
                    f"ВАЖНО: Обрабатывай ТОЛЬКО перечисленные товары.\n\n"
                    f"1. Для каждого ID получи данные через get_imported_product (product_id=ID)\n"
                    f"2. Сгенерируй оптимизированный заголовок и описание\n"
                    f"3. Обнови каждый товар через update_product\n\n"
                    f"Верни JSON: {{processed: число, results: [...]}}"
                )
            limit = input_data.get('limit', 10)
            return (
                f"SEO-оптимизация пакета товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Для каждого товара сгенерируй оптимизированный заголовок и описание\n"
                f"3. Обнови каждый товар через update_product\n\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{processed: число, results: [...]}}"
            )

        elif task_type == 'rewrite_titles':
            limit = input_data.get('limit', 10)
            return (
                f"Перепиши заголовки товаров по правилам WB.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. Загрузи ОДНУ страницу: get_products(seller_id={seller_id}, page=1, per_page={limit})\n"
                f"2. Проанализируй заголовки на соответствие правилам WB\n"
                f"3. Перепиши несоответствующие заголовки\n"
                f"4. Обнови через update_product\n\n"
                f"ВАЖНО: НЕ листай страницы. Загрузи товары ОДНИМ вызовом.\n\n"
                f"Верни JSON: {{rewritten: число, skipped: число, details: [...]}}"
            )

        return (
            f"Задача: {task.get('title')}\nТип: {task_type}\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Выполни SEO-оптимизацию и верни результат в JSON."
        )
