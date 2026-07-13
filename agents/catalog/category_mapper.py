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

from ..base_agent import BaseAgent, _build_usage
from ..platform_client import (
    ReferenceDataUnavailableError,
    require_usable_reference,
)

logger = logging.getLogger(__name__)


class CategoryMapperAgent(BaseAgent):
    agent_name = 'category-mapper'
    max_iterations = 18
    tool_allowlist = (
        'get_product', 'update_product', 'get_imported_products',
        'get_imported_product', 'update_imported_product',
        'batch_update_imported_products', 'search_wb_categories',
    )

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
- Для одиночной ImportedProduct-карточки сохрани через update_imported_product.
  В tool-assisted batch не вызывай update tools: верни typed results, их одним
  batch-запросом проверит и сохранит Python harness
- ЗАПРЕЩЕНО выдумывать категории — ТОЛЬКО из результатов search_wb_categories
- ЗАПРЕЩЕНО ставить явно неподходящую категорию! "Насадки для вибраторов" НЕ подходит для анальной пробки/втулки.
  Если найдена только неподходящая категория — НЕ используй её. Лучше вернуть ошибку, чем записать неверную.
- Если search_wb_categories вернул warning о disabled-категориях и is_enabled=false —
  это значит нужная категория существует в WB, но не включена в системе.
  НЕ записывай disabled-категорию. Верни в результате: {"error": "category_disabled",
  "subject_id": ..., "subject_name": ..., "message": "Категория найдена, но не включена. Включите в разделе Маркетплейсы → Категории."}
- Если reference_status.usable=false, не пытайся подобрать категорию по памяти и не
  вызывай update tools. Останови задачу и сообщи, что нужна синхронизация с WB.
- confidence: 1.0 = точное совпадение, 0.8-0.9 = очень похоже, 0.5-0.7 = приблизительно.
  НЕ ставь confidence выше 0.5 если категория не соответствует типу товара.

Результат: JSON с полями: subject_id, subject_name, parent_name, confidence, reasoning."""

    def execute_task(self, task: dict) -> dict:
        """Batch: tool-assisted с предзагрузкой и кэшированием категорий. Single: ReAct."""
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'map_single')
        try:
            if task_type in ('map_batch',):
                product_ids = (
                    input_data.get('product_ids')
                    or input_data.get('imported_product_ids')
                    or []
                )
                if len(product_ids) > 1:
                    return self._execute_tool_batch(
                        task, product_ids, chunk_size=15, max_workers=2,
                    )
            # One local SQL-backed call prevents spending any LLM tokens when
            # the shared category snapshot is stale or failed.
            self._search_categories_fresh('товары', limit=1)
            return self._execute_react(task)
        except ReferenceDataUnavailableError as exc:
            total = len(input_data.get('product_ids') or input_data.get('imported_product_ids') or [])
            return self._reference_blocked_result(exc, total)

    @staticmethod
    def _reference_blocked_result(exc, total=0):
        return {
            'status': 'needs_clarification',
            'partial': True,
            'reference_data_blocked': True,
            'processed': 0,
            'saved': 0,
            'failed': total,
            'message': str(exc),
            'reference_status': exc.reference_status,
            '_usage': _build_usage({}, mode='reference_preflight'),
        }

    def _search_categories_fresh(self, query, limit=10):
        response = self.platform.search_categories(query, limit=limit)
        return require_usable_reference(response, 'wb_categories')

    @staticmethod
    def _product_category_query(product: dict) -> str | None:
        supplier_category = str(product.get('category') or '')
        query = supplier_category.split('>')[0].strip()
        if len(query) >= 2:
            return query
        query = ' '.join(str(product.get('title') or '').split()[:2]).strip()
        return query if len(query) >= 2 else None

    def _prefetch_reference_data(self, products_data: list[dict]) -> dict:
        """Load every unique supplier scope with one typed internal request."""
        queries = []
        seen_queries = set()
        for product in products_data:
            query = self._product_category_query(product)
            if not query:
                continue
            normalized = query.casefold()
            if normalized in seen_queries:
                continue
            seen_queries.add(normalized)
            queries.append(query)
        if not queries:
            queries = ['товары']

        try:
            payload = self.platform.search_categories_batch(queries, limit=10)
        except ReferenceDataUnavailableError:
            raise
        except Exception as exc:
            logger.warning('Failed to prefetch category batch: %s', exc)
            raise ReferenceDataUnavailableError(
                'wb_categories',
                {
                    'warning': 'Не удалось загрузить актуальные категории WB.',
                    'reference_status': {
                        'source': 'wb_categories',
                        'usable': False,
                        'available': False,
                        'stale': False,
                        'reason': 'request_failed',
                    },
                },
            ) from exc

        results = payload.get('results') if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != len(queries):
            raise ReferenceDataUnavailableError('wb_categories', payload)

        cached_searches = {}
        for expected_query, result in zip(queries, results):
            if (
                not isinstance(result, dict)
                or result.get('query') != expected_query
            ):
                raise ReferenceDataUnavailableError('wb_categories', payload)
            require_usable_reference(result, 'wb_categories')
            cached_searches[expected_query] = {
                'categories': result.get('categories', []),
                **(
                    {'warning': result['warning']}
                    if result.get('warning') else {}
                ),
            }

        return {'cached_category_searches': cached_searches}

    def _build_tool_batch_prompt(
        self, products_data: list[dict], reference_data: dict,
    ) -> str:
        """Промпт с данными товаров и кэшированными результатами поиска категорий."""
        products_json = json.dumps(
            products_data, ensure_ascii=False, separators=(',', ':'),
        )

        # Кэшированные результаты только для supplier scopes текущего чанка.
        # Не переносим все task-level searches в каждый prompt.
        cached = reference_data.get('cached_category_searches', {})
        cached_by_normalized = {
            query.casefold(): (query, results)
            for query, results in cached.items()
        }
        relevant_queries = []
        seen_queries = set()
        for product in products_data:
            query = self._product_category_query(product)
            normalized = query.casefold() if query else ''
            if not normalized or normalized in seen_queries:
                continue
            seen_queries.add(normalized)
            relevant_queries.append(query)
        cache_parts = []
        for query in relevant_queries:
            cached_item = cached_by_normalized.get(query.casefold())
            if not cached_item:
                continue
            cached_query, results = cached_item
            results_json = json.dumps(
                results, ensure_ascii=False, separators=(',', ':'),
            )
            cache_parts.append(f'Запрос "{cached_query}":\n{results_json}')

        cache_text = '\n\n'.join(cache_parts) if cache_parts else 'Нет предзагруженных результатов.'

        return (
            f"Пакетный маппинг категорий для {len(products_data)} товаров.\n\n"
            f"=== ДАННЫЕ ТОВАРОВ (уже загружены) ===\n{products_json}\n\n"
            f"=== ПРЕДЗАГРУЖЕННЫЕ РЕЗУЛЬТАТЫ ПОИСКА КАТЕГОРИЙ ===\n{cache_text}\n\n"
            f"НЕ вызывай get_imported_product — данные уже выше.\n\n"
            f"Алгоритм:\n"
            f"1. Для каждого товара посмотри предзагруженные результаты поиска выше\n"
            f"2. Если подходящая категория найдена — используй её\n"
            f"3. Если НЕ найдена — вызови search_wb_categories с уточнённым запросом\n"
            f"4. Для каждого товара подготовь typed result: product_id, subject_id, "
            f"subject_name, confidence и reasoning\n\n"
            f"mapped_wb_category = subject_name (конечная), НЕ parent_name (раздел).\n"
            f"НЕ вызывай update_imported_product или batch_update_imported_products. "
            f"Сохранение выполнит Python harness ровно одним batch-вызовом после "
            f"проверки результата.\n\n"
            f"Верни ОДИН финальный JSON без markdown: "
            f"{{\"results\":[{{\"product_id\":ID,\"subject_id\":123,"
            f"\"subject_name\":\"Категория\",\"confidence\":0.9,"
            f"\"reasoning\":\"кратко\"}}]}}. "
            f"Если категория не найдена, верни для товара product_id и error."
        )

    def _map_tool_batch_result_to_updates(
        self, chunk_result: dict, products_data: list[dict],
    ) -> list[dict]:
        """Convert only typed, confident category results into batch updates."""
        results = chunk_result.get('results')
        if not isinstance(results, list):
            raise ValueError('Category batch result must contain results array')

        allowed_ids = {item.get('id') for item in products_data}
        seen = set()
        updates = []
        for item in results:
            if not isinstance(item, dict):
                raise ValueError('Category batch result item must be an object')
            product_id = item.get('product_id')
            if (
                not isinstance(product_id, int)
                or isinstance(product_id, bool)
                or product_id not in allowed_ids
            ):
                raise ValueError('Category result references product outside chunk')
            if product_id in seen:
                raise ValueError('Category result contains duplicate product_id')
            seen.add(product_id)
            if item.get('error'):
                continue

            subject_id = item.get('subject_id')
            subject_name = item.get('subject_name')
            confidence = item.get('confidence')
            if (
                not isinstance(subject_id, int)
                or isinstance(subject_id, bool)
                or subject_id <= 0
                or not isinstance(subject_name, str)
                or not subject_name.strip()
                or isinstance(confidence, bool)
            ):
                continue
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                continue
            if not 0.5 <= confidence <= 1.0:
                continue
            updates.append({
                'product_id': product_id,
                'wb_subject_id': subject_id,
                'mapped_wb_category': subject_name.strip(),
                'category_confidence': confidence,
            })
        return updates

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
                # Режим предложения (propose): определяем категорию, но НЕ сохраняем
                if input_data.get('mode') == 'propose':
                    return (
                        f"Определи категорию WB для товара (РЕЖИМ ПРЕДЛОЖЕНИЯ).\n"
                        f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                        f"1. get_product(seller_id={seller_id}, product_id={product_id})\n"
                        f"2. search_wb_categories(query=<ключевое слово из названия>)\n"
                        f"3. Выбери наиболее подходящую КОНЕЧНУЮ категорию (subject_name)\n"
                        f"ЗАПРЕЩЕНО вызывать update_product или update_imported_product — НИЧЕГО не сохраняй.\n\n"
                        f"Верни ТОЛЬКО JSON: {{subject_id, subject_name, confidence: 0..1}}"
                    )
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
