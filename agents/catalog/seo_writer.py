# -*- coding: utf-8 -*-
"""
Агент SEO — генерация SEO-оптимизированных заголовков и описаний.

Задачи:
  - seo_single:    оптимизировать одну карточку
  - seo_batch:     оптимизировать пакет карточек
  - rewrite_titles: переписать заголовки
"""
import json
import logging

from ..base_agent import BaseAgent
from ..tools import ToolRegistry

logger = logging.getLogger(__name__)


class SEOWriterAgent(BaseAgent):
    agent_name = 'seo-writer'
    max_iterations = 12

    system_prompt = """SEO-эксперт для Wildberries.

Заголовок: до 60 символов, формат "Бренд / Тип / Характеристика", без спецсимволов и рекламных слов.
Описание: до 1000 символов, ключевые слова, преимущества, материал, без контактов/ссылок.

ПРАВИЛА:
- check_text_prohibited — проверь текст на стоп-слова ПЕРЕД сохранением
- update_imported_product — сохрани результат
- Каждый инструмент вызывай РОВНО 1 раз на товар
- НЕ повторяй вызовы

Результат: JSON {title, description, keywords, changes_summary}."""

    def get_tools(self) -> ToolRegistry:
        """SEO-специфичные инструменты обрабатываются внутри LLM через промпт."""
        return None

    def execute_task(self, task: dict) -> dict:
        """Batch: structured output (без tool calling). Single: ReAct."""
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'seo_single')
        if task_type in ('seo_batch',):
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )
            if len(product_ids) > 1:
                return self._execute_structured_batch(
                    task, product_ids, chunk_size=20, max_workers=3,
                )
        return self._execute_react(task)

    # ── Structured Batch hooks ─────────────────────────────────

    def _prefetch_for_structured_batch(self, product_ids: list[int]) -> list[dict]:
        """Загружаем brief с описаниями (нужны для генерации SEO)."""
        all_products = []
        for i in range(0, len(product_ids), 50):
            batch = product_ids[i:i + 50]
            try:
                products = self.platform.get_imported_products_brief(batch)
                all_products.extend(products)
            except Exception as e:
                logger.warning(f"Failed to prefetch SEO batch {i // 50}: {e}")
        return all_products

    def build_structured_prompt(self, products_data: list[dict]) -> str:
        products_json = json.dumps(products_data, ensure_ascii=False, indent=2)
        return (
            f"Сгенерируй SEO-оптимизированные заголовки и описания для {len(products_data)} товаров Wildberries.\n\n"
            f"=== ТОВАРЫ ===\n{products_json}\n\n"
            f"ПРАВИЛА:\n"
            f"- Заголовок: до 60 символов, формат «Бренд / Тип / Характеристика»\n"
            f"- Описание: до 1000 символов, ключевые слова, преимущества, материал\n"
            f"- Без спецсимволов, рекламных слов, контактов, ссылок\n"
            f"- Для каждого товара верни product_id, title, description, keywords\n\n"
            f"Верни JSON: {{\"results\": [{{\"product_id\": ID, \"title\": \"...\", "
            f"\"description\": \"...\", \"keywords\": [\"...\"]}}]}}"
        )

    def batch_result_schema(self) -> dict:
        return {
            'type': 'object',
            'properties': {
                'results': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'product_id': {'type': 'integer'},
                            'title': {'type': 'string'},
                            'description': {'type': 'string'},
                            'keywords': {'type': 'array', 'items': {'type': 'string'}},
                        },
                        'required': ['product_id', 'title', 'description'],
                    },
                },
            },
            'required': ['results'],
        }

    def _postprocess_structured_results(self, results: list[dict]) -> list[dict]:
        """Проверяем стоп-слова в Python (без LLM tool calling)."""
        for item in results:
            for field in ('title', 'description'):
                text = item.get(field, '')
                if not text:
                    continue
                try:
                    check = self.platform.check_prohibited_words(text)
                    if check.get('has_violations'):
                        filtered = check.get('filtered_text', text)
                        if filtered:
                            item[field] = filtered
                except Exception as e:
                    logger.warning(f"Prohibited words check failed: {e}")
        return results

    def _map_structured_result_to_updates(self, results: list[dict]) -> list[dict]:
        updates = []
        for item in results:
            pid = item.get('product_id')
            if not pid:
                continue
            update = {'product_id': pid}
            if item.get('title'):
                update['title'] = item['title']
            if item.get('description'):
                update['description'] = item['description']
            updates.append(update)
        return updates

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'seo_single')
        seller_id = task.get('seller_id')

        if task_type == 'seo_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            if imported_product_id:
                return (
                    f"Оптимизируй SEO для импортированного товара.\n"
                    f"Imported Product ID: {imported_product_id}\n\n"
                    f"Шаги:\n"
                    f"1. get_imported_product(product_id={imported_product_id})\n"
                    f"2. Проанализируй текущий заголовок и описание\n"
                    f"3. Сгенерируй оптимизированный заголовок (до 60 символов)\n"
                    f"4. Сгенерируй SEO-описание (до 1000 символов)\n"
                    f"5. check_text_prohibited(text=<заголовок>) — ОБЯЗАТЕЛЬНО проверь на стоп-слова\n"
                    f"6. check_text_prohibited(text=<описание>) — ОБЯЗАТЕЛЬНО проверь на стоп-слова\n"
                    f"7. Если найдены стоп-слова — используй filtered_text из ответа\n"
                    f"8. update_imported_product(product_id={imported_product_id}, title=..., description=...)\n\n"
                    f"ОБЯЗАТЕЛЬНО проверь тексты через check_text_prohibited перед сохранением.\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для сохранения.\n"
                    f"Верни JSON: {{title, description, keywords: [...], changes_summary}}"
                )

            if product_id:
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

            return f"Ошибка: не указан product_id или imported_product_id."

        elif task_type == 'seo_batch':
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )

            # 1 товар → делегируем в single
            if len(product_ids) == 1:
                return self.build_task_prompt({
                    **task,
                    'task_type': 'seo_single',
                    'input_data': json.dumps({
                        'imported_product_id': product_ids[0],
                        'seller_id': seller_id,
                    }),
                })

            if product_ids:
                count = len(product_ids)
                products_brief = self._prefetch_products_brief(product_ids)
                if products_brief:
                    products_text = json.dumps(products_brief, ensure_ascii=False, indent=2)
                    return (
                        f"SEO-оптимизация {count} товаров.\n"
                        f"Данные товаров уже загружены:\n{products_text}\n\n"
                        f"ОПТИМИЗАЦИЯ: данные уже загружены выше. ЗАПРЕЩЕНО вызывать get_imported_product.\n\n"
                        f"Для каждого товара:\n"
                        f"1. Сгенерируй оптимизированный заголовок (до 60 символов) и описание (до 1000 символов)\n"
                        f"2. check_text_prohibited(text=<заголовок>) — проверь на стоп-слова\n"
                        f"3. update_imported_product(product_id=ID, title=..., description=...)\n\n"
                        f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                        f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
                    )

                ids_str = ', '.join(str(i) for i in product_ids[:20])
                return (
                    f"SEO-оптимизация {count} выбранных товаров.\n"
                    f"Product IDs: [{ids_str}]\n\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products.\n\n"
                    f"Для каждого ID:\n"
                    f"1. get_imported_product(product_id=ID)\n"
                    f"2. Сгенерируй оптимизированный заголовок и описание\n"
                    f"3. update_imported_product(product_id=ID, title=..., description=...)\n\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                    f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
                )

            limit = input_data.get('limit', 10)
            return (
                f"SEO-оптимизация пакета товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого товара сгенерируй оптимизированный заголовок и описание\n"
                f"3. Для каждого: update_imported_product(product_id=ID, title=..., description=...)\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
            )

        elif task_type == 'rewrite_titles':
            limit = input_data.get('limit', 10)
            return (
                f"Перепиши заголовки товаров по правилам WB.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Проанализируй заголовки на соответствие правилам WB\n"
                f"3. Перепиши несоответствующие заголовки\n"
                f"4. Для каждого: update_imported_product(product_id=ID, title=...)\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                f"Верни JSON: {{rewritten: число, skipped: число, details: [...]}}"
            )

        return (
            f"Задача: {task.get('title')}\nТип: {task_type}\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Выполни SEO-оптимизацию, сохрани через update_imported_product и верни результат в JSON."
        )

    def _prefetch_products_brief(self, product_ids: list) -> list:
        """Предзагрузка кратких данных товаров для встраивания в промпт."""
        try:
            return self.platform.get_imported_products_brief(product_ids)
        except Exception as e:
            logger.warning(f"Failed to prefetch products brief: {e}")
            return []
