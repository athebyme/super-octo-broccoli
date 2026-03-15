# -*- coding: utf-8 -*-
"""
Агент характеристик — заполнение обязательных и рекомендованных характеристик карточки WB.
"""
import json
import logging

from ..base_agent import BaseAgent

logger = logging.getLogger(__name__)


class CharacteristicsFillerAgent(BaseAgent):
    agent_name = 'characteristics-filler'
    max_iterations = 15

    system_prompt = """Ты — эксперт по характеристикам карточек Wildberries.

Твоя задача — заполнить МАКСИМУМ характеристик товара по схеме WB.

ФОРМАТ: JSON-словарь {имя_характеристики: значение}
Пример: {"Цвет": "черный", "Страна производства": "Китай", "Материал изделия": "силикон", "Пол": "Женский", "Комплектация": "1 шт", "Длина": 15}

ПРАВИЛА:
1. Вызови get_category_characteristics(subject_id=...) БЕЗ required_only — нужны ВСЕ характеристики
2. ЗАПРЕЩЕНО вызывать get_category_characteristics повторно — один вызов даёт полный список
3. Заполни МАКСИМУМ характеристик из описания/названия товара — не только required
4. Извлекай: материал, цвет, страну, пол, комплектацию, размеры, вес, объём, количество в упаковке и любые другие данные
5. Для характеристик с dictionary — используй значения из словаря
6. Числовые (type="Число") = число. Строковые (type="Строка") = строка
7. Ключи = ТОЧНЫЕ названия из get_category_characteristics (поле name)
8. Для импортированных товаров используй update_imported_product

ЗАПРЕЩЕНО:
- Вызывать get_category_characteristics более 1 раза на товар/категорию
- Вызывать get_category_characteristics с required_only=true (нужны ВСЕ характеристики)
- Выдумывать значения — если данных нет, пропускай
- Оставлять characteristics пустым если в описании есть хоть какие-то данные

Результат: JSON с полями: characteristics, filled_count, missing, confidence."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'fill_single')
        seller_id = task.get('seller_id')

        if task_type == 'fill_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            if imported_product_id:
                return (
                    f"Заполни характеристики импортированного товара.\n"
                    f"Imported Product ID: {imported_product_id}\n\n"
                    f"Шаги (ровно 3 вызова инструментов, НЕ больше):\n"
                    f"1. get_imported_product(product_id={imported_product_id}) — получи данные товара\n"
                    f"2. get_category_characteristics(subject_id=<wb_subject_id>) — получи ВСЕ характеристики категории (БЕЗ required_only!)\n"
                    f"3. Проанализируй КАЖДУЮ характеристику из списка и извлеки значение из описания/названия товара:\n"
                    f"   - Материал → из описания (\"силикон\", \"пластик\", \"хлопок\")\n"
                    f"   - Цвет → из описания или по контексту\n"
                    f"   - Страна → из поля country товара или описания\n"
                    f"   - Пол → из описания (\"для женщин\" → \"Женский\")\n"
                    f"   - Комплектация → из описания (\"набор\", \"1 шт\")\n"
                    f"   - Размеры (длина, ширина, диаметр) → из описания, в числах\n"
                    f"   - ВСЕ остальные характеристики — ищи данные в описании\n"
                    f"4. update_imported_product(product_id={imported_product_id}, characteristics=<JSON словарь>)\n\n"
                    f"ЗАПРЕЩЕНО вызывать get_category_characteristics повторно или с required_only.\n"
                    f"Заполняй МАКСИМУМ характеристик — не только required, но и все для которых есть данные.\n"
                    f"Верни JSON: {{characteristics: {{...}}, filled_count: N, missing: [...], confidence: 0-1}}"
                )

            if product_id:
                return (
                    f"Заполни характеристики карточки WB.\n"
                    f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                    f"1. get_product(seller_id={seller_id}, product_id={product_id})\n"
                    f"2. get_category_characteristics(subject_id=<wb_subject_id>) — ОБЯЗАТЕЛЬНО\n"
                    f"3. get_directory для цвета/страны при необходимости\n"
                    f"4. Заполни характеристики и обнови через update_product\n\n"
                    f"Верни JSON: {{characteristics: {{...}}, filled_count, missing, confidence}}"
                )

            return f"Ошибка: не указан product_id или imported_product_id."

        elif task_type == 'fill_batch':
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )

            # 1 товар → делегируем в single
            if len(product_ids) == 1:
                return self.build_task_prompt({
                    **task,
                    'task_type': 'fill_single',
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
                        f"Заполни характеристики для {count} товаров.\n"
                        f"Данные товаров уже загружены:\n{products_text}\n\n"
                        f"ОПТИМИЗАЦИЯ: данные уже загружены выше. ЗАПРЕЩЕНО вызывать get_imported_product.\n\n"
                        f"Алгоритм:\n"
                        f"1. Сгруппируй товары по категории (wb_subject_id)\n"
                        f"2. Для каждой категории: get_category_characteristics(subject_id=...) — ОДИН раз на категорию\n"
                        f"3. Для справочных значений: get_directory(...) — ОДИН раз на тип справочника\n"
                        f"4. Для каждого товара заполни характеристики из описания/названия\n"
                        f"5. update_imported_product(product_id=ID, characteristics=<JSON словарь>) — для КАЖДОГО товара\n\n"
                        f"Формат characteristics: {{\"Цвет\": \"черный\", \"Страна производства\": \"Китай\"}}\n"
                        f"Ключи = ТОЧНЫЕ названия из get_category_characteristics.\n"
                        f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                        f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
                    )

                ids_str = ', '.join(str(i) for i in product_ids[:20])
                return (
                    f"Заполни характеристики для {count} выбранных товаров.\n"
                    f"Product IDs: [{ids_str}]\n\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products.\n\n"
                    f"Для каждого ID:\n"
                    f"1. get_imported_product(product_id=ID)\n"
                    f"2. get_category_characteristics(subject_id=<wb_subject_id>) — ОБЯЗАТЕЛЬНО\n"
                    f"3. Заполни характеристики и update_imported_product(product_id=ID, characteristics=...)\n\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                    f"Верни JSON: {{processed: число, saved: число, results: [{{product_id, filled_count, missing: [...]}}]}}"
                )

            limit = input_data.get('limit', 10)
            return (
                f"Пакетное заполнение характеристик.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого товара:\n"
                f"   a. get_category_characteristics(subject_id=<wb_subject_id>) — схема характеристик\n"
                f"   b. Заполни характеристики из описания/названия\n"
                f"   c. update_imported_product(product_id=ID, characteristics=<JSON словарь>)\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
            )

        elif task_type == 'validate_existing':
            limit = input_data.get('limit', 10)
            return (
                f"Валидация характеристик товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проверь максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого: get_category_characteristics(subject_id=...) — сравни с характеристиками товара\n"
                f"3. Найди отсутствующие required и ошибочные значения\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n\n"
                f"Верни JSON: {{total, valid, issues: [{{product_id, missing: [...], errors: [...]}}]}}"
            )

        return (
            f"Задача по характеристикам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Заполни характеристики, сохрани через update_imported_product и верни результат в JSON."
        )

    def _prefetch_products_brief(self, product_ids: list) -> list:
        """Предзагрузка кратких данных товаров для встраивания в промпт."""
        try:
            return self.platform.get_imported_products_brief(product_ids)
        except Exception as e:
            logger.warning(f"Failed to prefetch products brief: {e}")
            return []
