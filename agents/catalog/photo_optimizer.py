# -*- coding: utf-8 -*-
"""
Агент фото — анализ, оптимизация и подготовка фотографий товаров для WB.

Задачи:
  - optimize_single:   оптимизировать фото одного товара
  - optimize_batch:    пакетная оптимизация фото
  - quality_check:     проверка качества фото
"""
import json

from ..base_agent import BaseAgent


class PhotoOptimizerAgent(BaseAgent):
    agent_name = 'photo-optimizer'
    max_iterations = 20

    system_prompt = """Ты — эксперт по фотографиям товаров для маркетплейса Wildberries.

Твои задачи:
- Анализировать качество фотографий товаров
- Проверять соответствие требованиям WB
- Определять порядок фото по релевантности
- Выявлять некачественные фото (размытые, тёмные, с водяными знаками)
- Давать рекомендации по улучшению визуального контента

Требования WB к фотографиям:
- Минимум 1 фото, рекомендуется 3-5
- Разрешение: минимум 900×1200 пикселей
- Белый или нейтральный фон
- Без водяных знаков, логотипов, текстовых наложений
- Без коллажей и рамок
- Товар должен занимать 60-80% площади кадра
- Формат: JPG/PNG

Правила сортировки:
- Первое фото — главное (фронтальный вид, на белом фоне)
- Далее: разные ракурсы, детали, упаковка
- Инфографика/lifestyle — в конце

ПРАВИЛА РАБОТЫ:
- Для импортированных товаров ВСЕГДА используй update_imported_product (НЕ update_product)
- Не вызывай get_imported_products если ID товаров уже известны
- Не повторяй вызовы — каждый инструмент вызывай ровно 1 раз на товар

Результат: JSON с анализом и рекомендациями по каждому фото."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'optimize_single')
        seller_id = task.get('seller_id')

        if task_type == 'optimize_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            if imported_product_id:
                return (
                    f"Анализ и оптимизация фото импортированного товара.\n"
                    f"Imported Product ID: {imported_product_id}\n\n"
                    f"1. get_imported_product(product_id={imported_product_id})\n"
                    f"2. Проанализируй фотографии товара\n"
                    f"3. Определи оптимальный порядок фото\n"
                    f"4. Выяви проблемы качества\n\n"
                    f"Верни JSON: {{total_photos, quality_score, issues: [...], "
                    f"recommended_order: [...], recommendations: [...]}}"
                )

            if product_id:
                return (
                    f"Анализ и оптимизация фото товара.\n"
                    f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                    f"1. Получи данные товара через get_product\n"
                    f"2. Проанализируй фотографии\n"
                    f"3. Определи оптимальный порядок\n"
                    f"4. Выяви проблемы качества\n\n"
                    f"Верни JSON: {{total_photos, quality_score, issues: [...], "
                    f"recommended_order: [...], recommendations: [...]}}"
                )

            return f"Ошибка: не указан product_id или imported_product_id."

        elif task_type == 'optimize_batch':
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )

            # 1 товар → делегируем в single
            if len(product_ids) == 1:
                return self.build_task_prompt({
                    **task,
                    'task_type': 'optimize_single',
                    'input_data': json.dumps({
                        'imported_product_id': product_ids[0],
                        'seller_id': seller_id,
                    }),
                })

            if product_ids:
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                count = len(product_ids)
                return (
                    f"Пакетный анализ фото для {count} товаров.\n"
                    f"Product IDs: [{ids_str}]\n\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products.\n\n"
                    f"Для каждого ID:\n"
                    f"1. get_imported_product(product_id=ID)\n"
                    f"2. Проанализируй фото на соответствие требованиям WB\n"
                    f"3. Определи оптимальный порядок\n\n"
                    f"Верни JSON: {{processed: число, results: [{{product_id, "
                    f"quality_score, issues, recommendations}}]}}"
                )

            limit = input_data.get('limit', 10)
            return (
                f"Пакетный анализ фото товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проанализируй максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого товара проанализируй фото\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n\n"
                f"Верни JSON: {{processed: число, results: [...]}}"
            )

        elif task_type == 'quality_check':
            limit = input_data.get('limit', 10)
            return (
                f"Проверка качества фото товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проверь максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого товара оцени качество фото\n"
                f"3. Выяви товары с проблемными фото\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n\n"
                f"Верни JSON: {{total, good_quality, needs_improvement, "
                f"issues: [{{product_id, photos_count, problems: [...]}}]}}"
            )

        return (
            f"Задача по фото.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Проанализируй фото и верни результат в JSON."
        )
