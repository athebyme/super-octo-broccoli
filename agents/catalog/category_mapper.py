# -*- coding: utf-8 -*-
"""
Агент категорий — маппинг товаров на категории WB.

Задачи:
  - map_single:      определить категорию одного товара
  - map_batch:       пакетный маппинг
  - remap_incorrect: исправить некорректные категории
"""
import json
import logging

from ..base_agent import BaseAgent

logger = logging.getLogger(__name__)


class CategoryMapperAgent(BaseAgent):
    agent_name = 'category-mapper'
    max_iterations = 18

    system_prompt = """Ты — эксперт по категориям маркетплейса Wildberries.

Задача: определить правильную КОНЕЧНУЮ категорию WB (subjectID) через search_wb_categories.

СТРАТЕГИЯ ПОИСКА (в порядке приоритета):
1. Сначала ищи по словам из поля category поставщика (например category="Вакуумные помпы > ..." → ищи "Вакуумные помпы")
2. Если не нашёл — ищи по типу товара из названия (1-2 ключевых слова)
3. Попробуй синонимы: "пробка" → "втулка" → "стимулятор" → "игрушка"
4. Попробуй родительский раздел: "Товары для взрослых", "Бытовая техника" и т.п. — это покажет ВСЕ leaf-категории раздела
5. Максимум 5 попыток поиска на товар!

ПРАВИЛА:
- subject_name = конечная категория для карточки. parent_name = раздел (НЕ записывай в карточку)
- mapped_wb_category = subject_name, wb_subject_id = subject_id
- ОБЯЗАТЕЛЬНО вызови update_imported_product для сохранения
- ЗАПРЕЩЕНО выдумывать категории — ТОЛЬКО из результатов search_wb_categories
- ЗАПРЕЩЕНО ставить явно неподходящую категорию! "Насадки для вибраторов" НЕ подходит для анальной пробки/втулки.
  Если найдена только неподходящая категория — НЕ используй её. Лучше вернуть ошибку, чем записать неверную.
- Если search_wb_categories вернул warning о disabled-категориях и is_enabled=false —
  это значит нужная категория существует в WB, но не включена в системе.
  НЕ записывай disabled-категорию. Верни в результате: {"error": "category_disabled",
  "subject_id": ..., "subject_name": ..., "message": "Категория найдена, но не включена. Включите в разделе Маркетплейсы → Категории."}
- confidence: 1.0 = точное совпадение, 0.8-0.9 = очень похоже, 0.5-0.7 = приблизительно.
  НЕ ставь confidence выше 0.5 если категория не соответствует типу товара.

Результат: JSON с полями: subject_id, subject_name, parent_name, confidence, reasoning."""

    def execute_task(self, task: dict) -> dict:
        """Автоматически разбивает большие батчи на чанки."""
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'map_single')
        if task_type in ('map_batch',):
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
        task_type = task.get('task_type', 'map_single')
        seller_id = task.get('seller_id')

        if task_type == 'map_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            if imported_product_id:
                return self._build_single_prompt(imported_product_id)

            if product_id:
                return (
                    f"Определи категорию WB для товара.\n"
                    f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                    f"Шаги:\n"
                    f"1. get_product(seller_id={seller_id}, product_id={product_id})\n"
                    f"2. search_wb_categories(query=<ключевое слово из названия>)\n"
                    f"3. Выбери наиболее подходящую КОНЕЧНУЮ категорию (subject_name)\n"
                    f"4. update_product(seller_id={seller_id}, product_id={product_id}, wb_category_id=<subject_id>, wb_category_name=<subject_name>)\n\n"
                    f"ЗАПРЕЩЕНО выдумывать категории — используй ТОЛЬКО результаты search_wb_categories.\n"
                    f"mapped_wb_category = subject_name (конечная), НЕ parent_name (раздел).\n"
                    f"Верни JSON: {{subject_id, subject_name, parent_name, confidence, reasoning}}"
                )

            return f"Ошибка: не указан product_id или imported_product_id."

        elif task_type == 'map_batch':
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )
            products_data = input_data.get('products_data', [])
            limit = input_data.get('limit', 10)

            # 1 товар → делегируем в single с предзагрузкой
            if len(product_ids) == 1 and not products_data:
                return self._build_single_prompt(product_ids[0])

            # Данные уже переданы
            if products_data:
                products_json = json.dumps(products_data[:20], ensure_ascii=False, indent=2)
                return (
                    f"Пакетный маппинг категорий. Данные уже загружены.\n\n"
                    f"Товары:\n{products_json}\n\n"
                    f"Для каждого товара:\n"
                    f"1. search_wb_categories(query=<ключевое слово из названия>) — найди категорию\n"
                    f"2. update_imported_product(product_id=ID, wb_subject_id=<subject_id>, mapped_wb_category=<subject_name>, category_confidence=<0.0-1.0>)\n\n"
                    f"ЗАПРЕЩЕНО выдумывать категории — используй ТОЛЬКО результаты search_wb_categories.\n"
                    f"mapped_wb_category = subject_name (конечная категория), НЕ parent_name (раздел).\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products — данные уже есть выше.\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                    f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
                )

            # Конкретные IDs — предзагружаем данные в промпт (экономия токенов)
            if product_ids:
                count = len(product_ids)
                products_brief = self._prefetch_products_brief(product_ids)
                if products_brief:
                    products_text = json.dumps(products_brief, ensure_ascii=False, indent=2)
                    return (
                        f"Пакетный маппинг категорий для {count} товаров.\n"
                        f"Данные товаров уже загружены:\n{products_text}\n\n"
                        f"НЕ вызывай get_imported_product — данные уже выше.\n\n"
                        f"Алгоритм:\n"
                        f"1. Сгруппируй товары по полю category поставщика\n"
                        f"2. Для каждой группы: search_wb_categories — ищи по словам из category поставщика (до знака '>'), потом по названию\n"
                        f"3. Для каждого товара: update_imported_product(product_id=ID, wb_subject_id=<subject_id>, mapped_wb_category=<subject_name>, category_confidence=<0.0-1.0>)\n\n"
                        f"mapped_wb_category = subject_name (конечная), НЕ parent_name (раздел).\n"
                        f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                        f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
                    )

                # Fallback если предзагрузка не удалась
                ids_str = ', '.join(str(i) for i in product_ids[:20])
                return (
                    f"Пакетный маппинг категорий для {count} товаров.\n"
                    f"Product IDs: [{ids_str}]\n\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products.\n\n"
                    f"Для каждого ID:\n"
                    f"1. get_imported_product(product_id=ID)\n"
                    f"2. search_wb_categories(query=<ключевое слово из названия>) — найди категорию\n"
                    f"3. update_imported_product(product_id=ID, wb_subject_id=<subject_id>, mapped_wb_category=<subject_name>, category_confidence=<0.0-1.0>)\n\n"
                    f"ЗАПРЕЩЕНО выдумывать категории — используй ТОЛЬКО результаты search_wb_categories.\n"
                    f"mapped_wb_category = subject_name (конечная категория), НЕ parent_name (раздел).\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                    f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
                )

            # Без IDs — загружаем страницу
            return (
                f"Пакетный маппинг категорий.\n"
                f"Seller ID: {seller_id}, лимит: {limit}\n\n"
                f"Шаги:\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого товара:\n"
                f"   a. search_wb_categories(query=<ключевое слово>) — найди категорию\n"
                f"   b. update_imported_product(product_id=ID, wb_subject_id=<subject_id>, mapped_wb_category=<subject_name>, category_confidence=<0.0-1.0>)\n\n"
                f"ЗАПРЕЩЕНО выдумывать категории — используй ТОЛЬКО результаты search_wb_categories.\n"
                f"mapped_wb_category = subject_name (конечная категория), НЕ parent_name (раздел).\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                f"Верни JSON: {{processed: число, saved: число, results: [...]}}"
            )

        return (
            f"Задача: {task.get('title')}\nТип: {task_type}\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Определи категории через search_wb_categories, сохрани через update_imported_product и верни результат."
        )

    def _build_single_prompt(self, imported_product_id: int) -> str:
        """Строит промпт для одного товара с предзагрузкой данных."""
        product_data = self._prefetch_product(imported_product_id)

        if product_data:
            # Извлекаем подсказку для поиска из категории поставщика
            supplier_category = product_data.get('category', '')
            title = product_data.get('title', '')
            search_hints = []
            if supplier_category:
                # Берём первую часть категории поставщика (до ">")
                main_cat = supplier_category.split('>')[0].strip()
                if main_cat:
                    search_hints.append(main_cat)
            if title:
                # Первые 2-3 слова из названия
                words = title.split()[:3]
                search_hints.append(' '.join(words))

            product_text = json.dumps(product_data, ensure_ascii=False, indent=2)
            hints_text = ', '.join(f'"{h}"' for h in search_hints) if search_hints else '"ключевое слово из названия"'

            return (
                f"Определи категорию WB для импортированного товара.\n"
                f"Imported Product ID: {imported_product_id}\n\n"
                f"=== ДАННЫЕ ТОВАРА (уже загружены) ===\n{product_text}\n\n"
                f"НЕ вызывай get_imported_product — данные уже выше.\n\n"
                f"Шаги:\n"
                f"1. search_wb_categories(query=...) — НАЧНИ с: {hints_text}\n"
                f"2. Если 0 результатов — попробуй синоним, более общее слово, или родительский раздел (макс 5 попыток)\n"
                f"   Пример: если не нашёл 'анальная пробка' — ищи 'пробка', 'Товары для взрослых'\n"
                f"3. ПРОВЕРЬ что найденная категория СООТВЕТСТВУЕТ типу товара!\n"
                f"   Если не соответствует — НЕ используй её, ищи дальше или верни ошибку\n"
                f"4. update_imported_product(product_id={imported_product_id}, wb_subject_id=<subject_id>, mapped_wb_category=<subject_name>, category_confidence=<0.0-1.0>)\n\n"
                f"mapped_wb_category = subject_name (конечная), НЕ parent_name (раздел).\n"
                f"Если search_wb_categories вернул is_enabled=false — категория НЕ включена, НЕ записывай её.\n"
                f"Верни ошибку: {{\"error\": \"category_disabled\", \"subject_id\": ..., \"subject_name\": ..., \"message\": \"Включите категорию\"}}\n\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product (только если нашёл подходящую включённую категорию)!\n"
                f"Верни JSON: {{subject_id, subject_name, parent_name, confidence, reasoning}}"
            )

        # Не удалось предзагрузить — fallback
        return (
            f"Определи категорию WB для импортированного товара.\n"
            f"Imported Product ID: {imported_product_id}\n\n"
            f"1. get_imported_product(product_id={imported_product_id})\n"
            f"2. search_wb_categories — ищи сначала по словам из category поставщика, потом по названию\n"
            f"3. update_imported_product(product_id={imported_product_id}, wb_subject_id=..., mapped_wb_category=..., category_confidence=...)\n\n"
            f"mapped_wb_category = subject_name (конечная), НЕ parent_name (раздел).\n"
            f"ОБЯЗАТЕЛЬНО вызови update_imported_product!\n"
            f"Верни JSON: {{subject_id, subject_name, parent_name, confidence, reasoning}}"
        )

    def _prefetch_product(self, product_id: int) -> dict:
        """Предзагрузка данных одного товара."""
        try:
            data = self.platform.get_imported_product(product_id)
            return data.get('product', data) if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Failed to prefetch product {product_id}: {e}")
            return {}

    def _prefetch_products_brief(self, product_ids: list) -> list:
        """Предзагрузка кратких данных товаров для встраивания в промпт.

        Возвращает только id, title, brand, category — минимум для маппинга.
        Экономит ~80% токенов по сравнению с N вызовами get_imported_product.
        """
        try:
            return self.platform.get_imported_products_brief(product_ids)
        except Exception as e:
            logger.warning(f"Failed to prefetch products brief: {e}")
            return []
