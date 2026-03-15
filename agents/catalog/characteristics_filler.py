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
1. Данные товара и схема характеристик категории УЖЕ ЗАГРУЖЕНЫ в промпте — НЕ вызывай get_imported_product и get_category_characteristics
2. Заполни МАКСИМУМ характеристик из описания/названия товара — не только required
3. Извлекай: материал, цвет, страну, пол, комплектацию, размеры, вес, объём, количество в упаковке и любые другие данные
4. Для характеристик с dictionary — используй значения из словаря
5. Числовые (type="Число") = число. Строковые (type="Строка") = строка
6. Ключи = ТОЧНЫЕ названия характеристик (поле name)

ЗАПРЕЩЕНО:
- Выдумывать значения — если данных нет, пропускай
- Оставлять characteristics пустым если в описании есть хоть какие-то данные

ОБЯЗАТЕЛЬНО вызови update_imported_product с заполненными характеристиками.

Результат: JSON с полями: characteristics, filled_count, missing, confidence."""

    def execute_task(self, task: dict) -> dict:
        """Автоматически разбивает большие батчи на чанки."""
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'fill_single')
        if task_type in ('fill_batch',):
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )
            if len(product_ids) > self.max_batch_size:
                return self._run_chunked_batch(task, product_ids)
        return self._execute_react(task)

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'fill_single')
        seller_id = task.get('seller_id')

        if task_type == 'fill_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            if imported_product_id:
                return self._build_single_prompt(imported_product_id)

            if product_id:
                return (
                    f"Заполни характеристики карточки WB.\n"
                    f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                    f"1. get_product(seller_id={seller_id}, product_id={product_id})\n"
                    f"2. get_category_characteristics(subject_id=<wb_subject_id>)\n"
                    f"3. Заполни характеристики и обнови через update_product\n\n"
                    f"Верни JSON: {{characteristics: {{...}}, filled_count, missing, confidence}}"
                )

            return f"Ошибка: не указан product_id или imported_product_id."

        elif task_type == 'fill_batch':
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )

            # 1 товар → делегируем в single с предзагрузкой
            if len(product_ids) == 1:
                return self._build_single_prompt(product_ids[0])

            if product_ids:
                return self._build_batch_prompt(product_ids)

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

    def _build_single_prompt(self, imported_product_id: int) -> str:
        """Строит промпт для одного товара с предзагрузкой данных и характеристик категории."""
        # Предзагружаем данные товара
        product_data = self._prefetch_product(imported_product_id)
        if not product_data:
            return (
                f"Заполни характеристики импортированного товара.\n"
                f"Imported Product ID: {imported_product_id}\n\n"
                f"1. get_imported_product(product_id={imported_product_id})\n"
                f"2. get_category_characteristics(subject_id=<wb_subject_id>)\n"
                f"3. update_imported_product(product_id={imported_product_id}, characteristics=<JSON>)\n\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product!\n"
                f"Верни JSON: {{characteristics: {{...}}, filled_count: N, missing: [...], confidence: 0-1}}"
            )

        subject_id = product_data.get('wb_subject_id')
        chars_schema = self._prefetch_category_chars(subject_id) if subject_id else None

        product_text = json.dumps(product_data, ensure_ascii=False, indent=2)

        if chars_schema:
            chars_text = json.dumps(chars_schema, ensure_ascii=False, indent=2)
            return (
                f"Заполни характеристики импортированного товара.\n"
                f"Imported Product ID: {imported_product_id}\n\n"
                f"=== ДАННЫЕ ТОВАРА (уже загружены) ===\n{product_text}\n\n"
                f"=== СХЕМА ХАРАКТЕРИСТИК КАТЕГОРИИ (уже загружены) ===\n{chars_text}\n\n"
                f"ВСЕ ДАННЫЕ УЖЕ ЗАГРУЖЕНЫ. НЕ вызывай get_imported_product и get_category_characteristics.\n\n"
                f"ТВОЯ ЕДИНСТВЕННАЯ ЗАДАЧА:\n"
                f"1. Проанализируй КАЖДУЮ характеристику из схемы и извлеки значение из описания/названия товара\n"
                f"2. Вызови update_imported_product(product_id={imported_product_id}, characteristics=<JSON словарь>)\n\n"
                f"Что извлекать:\n"
                f"- Материал → из описания (\"силикон\", \"пластик\", \"хлопок\")\n"
                f"- Цвет → из описания или по контексту\n"
                f"- Страна → из поля country товара или описания\n"
                f"- Пол → из описания (\"для женщин\" → \"Женский\")\n"
                f"- Комплектация → из описания (\"набор\", \"1 шт\")\n"
                f"- Размеры (длина, ширина, диаметр) → из описания, в числах\n"
                f"- ВСЕ остальные характеристики — ищи данные в описании\n\n"
                f"Для характеристик с dictionary — используй ТОЛЬКО значения из словаря.\n"
                f"Числовые (type=\"Число\") = число. Строковые (type=\"Строка\") = строка.\n"
                f"Ключи = ТОЧНЫЕ названия из схемы (поле name).\n\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product — без него характеристики НЕ сохранятся!\n"
                f"Верни JSON: {{characteristics: {{...}}, filled_count: N, missing: [...], confidence: 0-1}}"
            )

        # Нет subject_id или не удалось загрузить характеристики
        return (
            f"Заполни характеристики импортированного товара.\n"
            f"Imported Product ID: {imported_product_id}\n\n"
            f"=== ДАННЫЕ ТОВАРА (уже загружены) ===\n{product_text}\n\n"
            f"НЕ вызывай get_imported_product — данные уже выше.\n\n"
            f"Шаги:\n"
            f"1. get_category_characteristics(subject_id={subject_id or '<wb_subject_id>'}) — получи схему характеристик\n"
            f"2. Заполни МАКСИМУМ характеристик из описания/названия\n"
            f"3. update_imported_product(product_id={imported_product_id}, characteristics=<JSON словарь>)\n\n"
            f"ОБЯЗАТЕЛЬНО вызови update_imported_product!\n"
            f"Верни JSON: {{characteristics: {{...}}, filled_count: N, missing: [...], confidence: 0-1}}"
        )

    def _build_batch_prompt(self, product_ids: list) -> str:
        """Строит промпт для пакетной обработки с предзагрузкой."""
        count = len(product_ids)
        products_brief = self._prefetch_products_brief(product_ids)

        if not products_brief:
            ids_str = ', '.join(str(i) for i in product_ids[:20])
            return (
                f"Заполни характеристики для {count} товаров.\n"
                f"Product IDs: [{ids_str}]\n\n"
                f"Для каждого ID:\n"
                f"1. get_imported_product(product_id=ID)\n"
                f"2. get_category_characteristics(subject_id=<wb_subject_id>)\n"
                f"3. update_imported_product(product_id=ID, characteristics=<JSON>)\n\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                f"Верни JSON: {{processed: число, saved: число, results: [{{product_id, filled_count, missing: [...]}}]}}"
            )

        # Предзагружаем характеристики для каждой уникальной категории
        subject_ids = set()
        for p in products_brief:
            sid = p.get('wb_subject_id')
            if sid:
                subject_ids.add(sid)

        chars_by_subject = {}
        for sid in subject_ids:
            chars = self._prefetch_category_chars(sid)
            if chars:
                chars_by_subject[sid] = chars

        products_text = json.dumps(products_brief, ensure_ascii=False, indent=2)

        parts = [
            f"Заполни характеристики для {count} товаров.\n",
            f"=== ДАННЫЕ ТОВАРОВ (уже загружены) ===\n{products_text}\n",
        ]

        if chars_by_subject:
            parts.append("=== СХЕМЫ ХАРАКТЕРИСТИК ПО КАТЕГОРИЯМ ===")
            for sid, chars in chars_by_subject.items():
                chars_text = json.dumps(chars, ensure_ascii=False, indent=2)
                parts.append(f"subject_id={sid}:\n{chars_text}")
            parts.append("")

        parts.append(
            "ВСЕ ДАННЫЕ УЖЕ ЗАГРУЖЕНЫ. НЕ вызывай get_imported_product и get_category_characteristics.\n\n"
            "Для КАЖДОГО товара:\n"
            "1. Найди его категорию (wb_subject_id) → возьми схему характеристик\n"
            "2. Извлеки значения из описания/названия\n"
            "3. Вызови update_imported_product(product_id=ID, characteristics=<JSON словарь>)\n\n"
            f"Формат characteristics: {{\"Цвет\": \"черный\", \"Страна производства\": \"Китай\"}}\n"
            "ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара!\n\n"
            f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
        )

        return '\n'.join(parts)

    def _prefetch_product(self, product_id: int) -> dict:
        """Предзагрузка полных данных одного товара."""
        try:
            data = self.platform.get_imported_product(product_id)
            return data.get('product', data) if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Failed to prefetch product {product_id}: {e}")
            return {}

    def _prefetch_category_chars(self, subject_id: int) -> list:
        """Предзагрузка характеристик категории."""
        if not subject_id:
            return []
        try:
            data = self.platform.get_category_characteristics(subject_id, False)
            return data.get('characteristics', data) if isinstance(data, dict) else data
        except Exception as e:
            logger.warning(f"Failed to prefetch chars for subject {subject_id}: {e}")
            return []

    def _prefetch_products_brief(self, product_ids: list) -> list:
        """Предзагрузка кратких данных товаров для встраивания в промпт."""
        try:
            return self.platform.get_imported_products_brief(product_ids)
        except Exception as e:
            logger.warning(f"Failed to prefetch products brief: {e}")
            return []
