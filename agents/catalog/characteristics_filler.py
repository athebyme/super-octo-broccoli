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

Твоя задача — заполнить характеристики товара СТРОГО по схеме WB.

ФОРМАТ ХАРАКТЕРИСТИК WB:
Характеристики хранятся как JSON-словарь, где ключ = ТОЧНОЕ название характеристики из WB,
значение = строка или массив строк.
Пример:
{
  "Цвет": "черный",
  "Страна производства": "Китай",
  "Материал изделия": "силикон",
  "Пол": "Женский",
  "Комплектация": "1 шт",
  "Длина": 15
}

КРИТИЧЕСКИЕ ПРАВИЛА:
1. ОБЯЗАТЕЛЬНО вызови get_category_characteristics(subject_id=...) — узнай РЕАЛЬНЫЕ характеристики категории
2. Используй ТОЛЬКО названия характеристик из результата get_category_characteristics
3. Если у характеристики есть dictionary (справочник) — используй ТОЛЬКО значения из справочника
4. Для цвета/страны/пола: get_directory(directory_type='colors'|'countries'|'kinds') — возьми значение из справочника WB
5. Числовые характеристики (charc_type=4) = число. Строковые (charc_type=1) = строка.
6. Если характеристика required=true — ОБЯЗАТЕЛЬНО заполни
7. Извлекай данные из описания/названия товара, сопоставляй с допустимыми значениями
8. Для импортированных товаров ВСЕГДА используй update_imported_product
9. НЕ выдумывай значения — если данных нет, пропускай необязательные

АЛГОРИТМ:
1. Получить данные товара
2. get_category_characteristics(subject_id=<wb_subject_id>) — ОБЯЗАТЕЛЬНО
3. Для справочных значений — get_directory(...)
4. Заполнить словарь {имя_характеристики: значение}
5. update_imported_product(product_id=..., characteristics=<JSON словарь>)

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
                    f"Шаги:\n"
                    f"1. get_imported_product(product_id={imported_product_id}) — получи данные товара\n"
                    f"2. get_category_characteristics(subject_id=<wb_subject_id из товара>) — ОБЯЗАТЕЛЬНО узнай характеристики категории\n"
                    f"   Из ответа ты получишь: name, type (charc_type), required, dictionary, unit_name\n"
                    f"3. Для цвета: get_directory(directory_type='colors') — справочник WB\n"
                    f"4. Для страны: get_directory(directory_type='countries') — справочник WB\n"
                    f"5. Извлеки данные из описания товара. Сопоставь с допустимыми значениями из dictionary.\n"
                    f"6. Собери словарь характеристик:\n"
                    f"   - Ключи = ТОЧНЫЕ названия из get_category_characteristics (поле name)\n"
                    f"   - Значения = строки из dictionary или числа для charc_type=4\n"
                    f"   - Заполни ВСЕ required характеристики\n"
                    f"7. update_imported_product(product_id={imported_product_id}, characteristics=<JSON строка словаря>)\n\n"
                    f"ПРИМЕР characteristics для передачи в update_imported_product:\n"
                    f'{{"Цвет": "черный", "Страна производства": "Китай", "Пол": "Женский", "Длина": 15}}\n\n'
                    f"ЗАПРЕЩЕНО выдумывать характеристики — используй ТОЛЬКО данные из get_category_characteristics.\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для сохранения.\n"
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
