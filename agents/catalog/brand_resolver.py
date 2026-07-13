# -*- coding: utf-8 -*-
"""
Агент брендов — распознавание, нормализация, валидация брендов.
"""
import json
import logging
import threading

from ..base_agent import BaseAgent, _build_usage
from ..platform_client import ReferenceDataUnavailableError

logger = logging.getLogger(__name__)


class BrandResolverAgent(BaseAgent):
    agent_name = 'brand-resolver'
    max_iterations = 12
    tool_allowlist = (
        'get_product', 'update_product', 'get_imported_products',
        'get_imported_product', 'update_imported_product',
        'batch_update_imported_products', 'validate_brand',
    )
    # Brand resolver НЕ должен сам искать категории — это задача category-mapper
    excluded_tools = ('search_wb_categories',)

    system_prompt = """Ты — эксперт по брендам на маркетплейсе Wildberries.

Твои задачи:
- Распознавать бренды из названий товаров поставщика
- Нормализовать написание (транслитерация, исправление ошибок)
- Проверять наличие бренда в реестре WB через validate_brand
- Подбирать корректное написание для карточки

КРИТИЧЕСКИЕ ПРАВИЛА:
- ЗАПРЕЩЕНО угадывать написание бренда! ОБЯЗАТЕЛЬНО используй validate_brand(brand_name=...)
  чтобы проверить бренд по реальному реестру WB
- ОБЯЗАТЕЛЬНО передавай category_id=<wb_subject_id> в validate_brand!
  Бренд может быть зарегистрирован в WB, но НЕДОСТУПЕН в конкретной категории товара.
  Без category_id проверка бессмысленна — WB отклонит карточку.
- validate_brand вернёт: точное совпадение, каноническое написание или похожие варианты
- Если validate_brand вернул category_available=true → бренд подтверждён в категории
- Если validate_brand вернул category_available=false → бренд НЕДОСТУПЕН в этой категории
- Если validate_brand вернул category_available=null → нет данных по категории (бренд найден в реестре, но не проверен по категории)
- Если бренд не найден — не записывай значение. Строка "Нет бренда" допустима
  только если validate_brand подтвердил её как exact category-scoped WB binding
- Если у товара есть wb_subject_id — ОБЯЗАТЕЛЬНО передавай category_id в validate_brand
- Если wb_subject_id пуст — НЕ изменяй бренд; верни category_scope_required для ручной проверки
- Для импортированных товаров ВСЕГДА используй update_imported_product (НЕ update_product)
- Не вызывай get_imported_products если ID товаров уже известны
- Не повторяй вызовы — каждый инструмент вызывай ровно 1 раз на товар
- Сразу после нормализации бренда — сохрани через update_imported_product

Алгоритм работы:
1. Получить товар (get_imported_product)
2. Извлечь бренд из названия/описания
3. validate_brand(brand_name=<бренд>, category_id=<wb_subject_id>) — ОБЯЗАТЕЛЬНО
4. Использовать каноническое написание из результата validate_brand
5. Сохранить (update_imported_product)

Результат: JSON с нормализованными брендами."""

    def execute_task(self, task: dict) -> dict:
        """Use the guarded structured path for imported single and batch writes."""
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'resolve_single')
        product_ids = (
            input_data.get('product_ids')
            or input_data.get('imported_product_ids')
            or []
        )
        try:
            self._preflight_brand_reference(task, input_data, product_ids)
            if task_type in ('resolve_batch',) and product_ids:
                return self._execute_structured_batch(
                    task, product_ids, chunk_size=30, max_workers=3,
                )
            if task_type == 'resolve_single' and input_data.get('imported_product_id'):
                return self._execute_structured_batch(
                    task, [input_data['imported_product_id']],
                    chunk_size=1, max_workers=1,
                )
            return self._execute_react(task)
        except ReferenceDataUnavailableError as exc:
            total = len(product_ids) or int(bool(
                input_data.get('product_id')
                or input_data.get('imported_product_id')
            ))
            return self._reference_blocked_result(exc, total)

    @staticmethod
    def _reference_blocked_result(exc, total=0):
        is_reference = exc.resource == 'wb_brands'
        return {
            'status': 'needs_clarification',
            'partial': True,
            'reference_data_blocked': is_reference,
            'selection_data_blocked': not is_reference,
            'processed': 0,
            'saved': 0,
            'failed': total,
            'message': str(exc),
            'reference_status': exc.reference_status,
            '_usage': _build_usage({}, mode='reference_preflight'),
        }

    @staticmethod
    def _missing_category_error():
        return ReferenceDataUnavailableError('wb_brands', {
            'warning': 'У товара нет категории WB. Сначала определите категорию.',
            'reference_status': {
                'source': 'wb_brands',
                'usable': False,
                'available': False,
                'stale': False,
                'reason': 'category_scope_required',
            },
        })

    @staticmethod
    def _missing_product_error(product_ids=None):
        product_ids = list(product_ids or [])
        suffix = (
            f' ID: {", ".join(map(str, product_ids[:10]))}.'
            if product_ids else ''
        )
        return ReferenceDataUnavailableError('selected_products', {
            'warning': (
                'Выбранная карточка не найдена или недоступна в текущей '
                f'области продавца.{suffix}'
            ),
            'reference_status': {
                'source': 'selected_products',
                'usable': False,
                'available': False,
                'stale': False,
                'reason': 'selected_product_unavailable',
            },
        })

    def _preflight_brand_reference(self, task, input_data, product_ids):
        """Load typed category scopes and stop before spending LLM tokens."""
        products = []
        imported_product_id = input_data.get('imported_product_id')
        product_id = input_data.get('product_id')
        if product_ids:
            products = []
            try:
                for index in range(0, len(product_ids), 50):
                    products.extend(self.platform.get_imported_products_brief(
                        product_ids[index:index + 50],
                    ))
            except Exception as exc:
                raise self._missing_product_error(product_ids) from exc
            self._preflight_products_by_id = {
                item.get('id'): item for item in products
            }
            loaded_ids = set(self._preflight_products_by_id)
            missing_ids = [
                value for value in product_ids if value not in loaded_ids
            ]
            if missing_ids:
                raise self._missing_product_error(missing_ids)
        elif imported_product_id:
            try:
                products = self.platform.get_imported_products_brief(
                    [imported_product_id],
                )
            except Exception as exc:
                raise self._missing_product_error([imported_product_id]) from exc
            self._preflight_products_by_id = {
                item.get('id'): item for item in products
            }
            if imported_product_id not in self._preflight_products_by_id:
                raise self._missing_product_error([imported_product_id])
        elif product_id:
            try:
                product = self.platform.get_product(
                    int(task.get('seller_id') or input_data.get('seller_id') or 0),
                    product_id,
                )
            except Exception as exc:
                raise self._missing_product_error([product_id]) from exc
            if product:
                self._preflight_main_product = {
                    'id': product.get('id'),
                    'title': str(product.get('title') or '')[:300],
                    'description': str(product.get('description') or '')[:800],
                    'brand': str(product.get('brand') or '')[:200],
                    'subject_id': product.get('subject_id'),
                }
                products = [product]
            else:
                raise self._missing_product_error([product_id])
        else:
            seller_id = int(task.get('seller_id') or input_data.get('seller_id') or 0)
            if not seller_id:
                raise self._missing_product_error()
            limit = min(max(int(input_data.get('limit') or 10), 1), 100)
            try:
                page = self.platform.list_imported_products(
                    seller_id, page=1, per_page=limit,
                )
            except Exception as exc:
                raise self._missing_product_error() from exc
            products = page.get('products') or []
            if not products:
                raise self._missing_product_error()

        subject_ids = {
            item.get('wb_subject_id') or item.get('subject_id')
            for item in products
        }
        if None in subject_ids or '' in subject_ids or 0 in subject_ids:
            raise self._missing_category_error()
        category_ids = sorted({int(value) for value in subject_ids})
        payloads = []
        for index in range(0, len(category_ids), 100):
            payloads.append(self.platform.preflight_brand_categories(
                category_ids[index:index + 100],
            ))
        statuses = []
        for payload in payloads:
            scoped = [
                item.get('reference_status') or {}
                for item in payload.get('results') or []
            ]
            statuses.extend(scoped or [payload.get('reference_status') or {}])
        blocked = next(
            (status for status in statuses if status.get('usable') is not True),
            None,
        )
        if blocked:
            raise ReferenceDataUnavailableError('wb_brands', {
                'warning': (
                    'Справочник брендов WB для выбранной категории '
                    'недоступен или устарел. Дождитесь синхронизации.'
                ),
                'reference_status': blocked,
            })

    # ── Structured Batch hooks ─────────────────────────────────

    def _prefetch_for_structured_batch(self, product_ids: list[int]) -> list[dict]:
        cached = getattr(self, '_preflight_products_by_id', {})
        products = [cached[product_id] for product_id in product_ids if product_id in cached]
        if len(products) != len(product_ids):
            products = super()._prefetch_for_structured_batch(product_ids)
        self._structured_subject_by_product = {
            product.get('id'): product.get('wb_subject_id')
            for product in products
        }
        # Structured chunks may run concurrently. Keep each chunk's expected
        # IDs in thread-local state so one worker cannot validate another one.
        self._structured_chunk_context = threading.local()
        return products

    def build_structured_prompt(self, products_data: list[dict]) -> str:
        context = getattr(self, '_structured_chunk_context', None)
        if context is None:
            context = threading.local()
            self._structured_chunk_context = context
        context.subject_by_product = {
            product.get('id'): product.get('wb_subject_id')
            for product in products_data
        }
        products_json = json.dumps(products_data, ensure_ascii=False, indent=2)
        return (
            f"Нормализуй бренды для {len(products_data)} товаров Wildberries.\n\n"
            f"=== ТОВАРЫ ===\n{products_json}\n\n"
            f"Для каждого товара:\n"
            f"1. Определи бренд из полей brand, title\n"
            f"2. Нормализуй написание (регистр, транслитерация)\n"
            f"3. Если бренд невозможно определить — верни пустую строку; "
            f"не подставляй \"Нет бренда\" без проверки реестра\n\n"
            f"Верни JSON: {{\"results\": [{{\"product_id\": ID, \"brand\": \"Каноническое название\"}}]}}"
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
                            'brand': {'type': 'string'},
                        },
                        'required': ['product_id', 'brand'],
                    },
                },
            },
            'required': ['results'],
        }

    def _postprocess_structured_results(self, results: list[dict]) -> list[dict]:
        """Validate all inferred brands with one typed internal API request."""
        context = getattr(self, '_structured_chunk_context', None)
        subject_by_product = (
            getattr(context, 'subject_by_product', None) if context else None
        ) or getattr(self, '_structured_subject_by_product', {})
        expected_ids = set(subject_by_product)
        returned_ids = []
        for item in results:
            if not isinstance(item, dict):
                raise ValueError('Brand result entries must be objects')
            product_id = item.get('product_id')
            if isinstance(product_id, bool) or not isinstance(product_id, int):
                raise ValueError('Brand result product_id must be an integer')
            returned_ids.append(product_id)
        returned_set = set(returned_ids)
        if (
            len(returned_ids) != len(returned_set)
            or returned_set != expected_ids
        ):
            missing = sorted(expected_ids - returned_set)
            foreign = sorted(returned_set - expected_ids)
            raise ValueError(
                'Brand result IDs do not match the current chunk: '
                f'missing={missing[:10]}, foreign={foreign[:10]}, '
                f'duplicates={len(returned_ids) - len(returned_set)}'
            )

        validation_items = []
        pending_by_product = {}
        for item in results:
            brand = str(item.get('brand') or '').strip()
            if not brand:
                item['brand'] = None
                item['error'] = 'brand_not_inferred'
                continue
            product_id = item.get('product_id')
            category_id = subject_by_product.get(product_id)
            if not category_id:
                item['brand'] = None
                item['error'] = 'category_scope_required'
                continue
            validation_items.append({
                'product_id': product_id,
                'brand': brand,
                'category_id': category_id,
            })
            pending_by_product[product_id] = item

        if not validation_items:
            return results
        try:
            checks = self.platform.validate_brands(validation_items)
        except Exception as error:
            logger.warning('Batch brand validation failed: %s', error)
            for item in pending_by_product.values():
                item['brand'] = None
                item['error'] = 'brand_reference_unavailable'
            return results

        checks_by_product = {check.get('product_id'): check for check in checks}
        for product_id, item in pending_by_product.items():
            check = checks_by_product.get(product_id) or {}
            status = check.get('status')
            canonical_name = (
                check.get('marketplace_brand_name')
                or check.get('brand_name')
            )
            if (
                status == 'found'
                and check.get('category_available') is True
                and canonical_name
            ):
                item['brand'] = canonical_name
            elif status == 'found':
                item['brand'] = None
                item['error'] = 'brand_not_verified_for_category'
            elif status in ('not_found', 'suggestions'):
                item['brand'] = None
                item['error'] = 'brand_not_registered_for_category'
            else:
                item['brand'] = None
                item['error'] = 'brand_reference_unavailable'
        return results

    def _map_structured_result_to_updates(self, results: list[dict]) -> list[dict]:
        updates = []
        for item in results:
            pid = item.get('product_id')
            brand = item.get('brand')
            if pid and brand:
                updates.append({'product_id': pid, 'brand': brand})
        return updates

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'resolve_single')
        seller_id = task.get('seller_id')

        if task_type == 'resolve_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            if imported_product_id:
                return (
                    f"Определи и нормализуй бренд импортированного товара.\n"
                    f"Imported Product ID: {imported_product_id}\n\n"
                    f"Шаги:\n"
                    f"1. get_imported_product(product_id={imported_product_id})\n"
                    f"2. Извлеки бренд из названия/описания товара\n"
                    f"3. validate_brand(brand_name=<бренд>, category_id=<wb_subject_id из данных товара>) — "
                    f"ОБЯЗАТЕЛЬНО передай category_id! Без него WB отклонит карточку\n"
                    f"4. Используй каноническое написание из результата validate_brand\n"
                    f"5. update_imported_product(product_id={imported_product_id}, brand=<каноническое написание>)\n\n"
                    f"ЗАПРЕЩЕНО угадывать бренд — используй ТОЛЬКО результат validate_brand.\n"
                    f"Если у товара есть wb_subject_id — передай category_id в validate_brand.\n"
                    f"Если wb_subject_id пуст — не изменяй бренд и верни category_scope_required.\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для сохранения.\n\n"
                    f"Верни JSON: {{original_brand, normalized_brand, "
                    f"confidence, wb_registered: bool, category_available: bool|null}}"
                )

            if product_id:
                product_data = getattr(self, '_preflight_main_product', {})
                product_text = json.dumps(
                    product_data, ensure_ascii=False, separators=(',', ':'),
                )
                # Режим предложения (propose): определяем бренд, но НЕ сохраняем
                if input_data.get('mode') == 'propose':
                    return (
                        f"Определи бренд товара (РЕЖИМ ПРЕДЛОЖЕНИЯ).\n"
                        f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                        f"Данные товара уже загружены: {product_text}\n\n"
                        f"1. Определи и нормализуй бренд из названия/описания\n"
                        f"2. Проверь точную пару через validate_brand(brand_name=<бренд>, "
                        f"category_id=<subject_id товара>)\n"
                        f"3. Предложи только marketplace_brand_name при category_available=true\n"
                        f"Не вызывай get_product: данные уже переданы выше.\n"
                        f"ЗАПРЕЩЕНО вызывать update_product или update_imported_product — НИЧЕГО не сохраняй.\n\n"
                        f"Верни ТОЛЬКО JSON: {{brand, confidence: 0..1}}"
                    )
                return (
                    f"Определи и нормализуй бренд товара.\n"
                    f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                    f"Данные товара уже загружены: {product_text}\n\n"
                    f"1. Определи бренд из названия/описания\n"
                    f"2. Вызови validate_brand(brand_name=<бренд>, "
                    f"category_id=<subject_id товара>)\n"
                    f"3. При status=found и category_available=true используй "
                    f"marketplace_brand_name\n"
                    f"4. Обнови товар через update_product; иначе ничего не записывай\n"
                    f"Не вызывай get_product: данные уже переданы выше.\n\n"
                    f"Верни JSON: {{original_brand, normalized_brand, "
                    f"confidence, wb_registered: bool}}"
                )

            return f"Ошибка: не указан product_id или imported_product_id."

        elif task_type == 'resolve_batch':
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )

            # 1 товар → делегируем в single
            if len(product_ids) == 1:
                return self.build_task_prompt({
                    **task,
                    'task_type': 'resolve_single',
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
                        f"Пакетная нормализация брендов для {count} товаров.\n"
                        f"Данные товаров уже загружены:\n{products_text}\n\n"
                        f"ОПТИМИЗАЦИЯ: данные уже загружены выше. ЗАПРЕЩЕНО вызывать get_imported_product.\n\n"
                        f"Для каждого товара:\n"
                        f"1. validate_brand(brand_name=<бренд из данных>, category_id=<wb_subject_id из данных товара>) — "
                        f"ОБЯЗАТЕЛЬНО передай category_id! Бренд может быть в WB, но недоступен в категории\n"
                        f"2. update_imported_product(product_id=ID, brand=<каноническое написание из validate_brand>)\n\n"
                        f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                        f"Если у товара есть wb_subject_id — передай category_id в validate_brand.\n"
                        f"Если wb_subject_id пуст — не изменяй бренд и верни category_scope_required.\n\n"
                        f"Верни JSON: {{total, updated, skipped, saved: число, "
                        f"results: [{{product_id, original, normalized, category_available}}]}}"
                    )

                ids_str = ', '.join(str(i) for i in product_ids[:20])
                return (
                    f"Пакетная нормализация брендов для {count} товаров.\n"
                    f"Product IDs: [{ids_str}]\n\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products.\n\n"
                    f"Для каждого ID:\n"
                    f"1. get_imported_product(product_id=ID)\n"
                    f"2. validate_brand(brand_name=<бренд>, category_id=<wb_subject_id из данных товара>) — "
                    f"ОБЯЗАТЕЛЬНО с category_id!\n"
                    f"3. update_imported_product(product_id=ID, brand=...)\n\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                    f"Верни JSON: {{total, updated, skipped, saved: число, "
                    f"results: [{{product_id, original, normalized}}]}}"
                )

            limit = input_data.get('limit', 10)
            return (
                f"Пакетная нормализация брендов.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого товара нормализуй бренд\n"
                f"3. Для каждого: update_imported_product(product_id=ID, brand=...)\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                f"Верни JSON: {{total, updated, skipped, saved: число, "
                f"results: [{{product_id, original, normalized}}]}}"
            )

        elif task_type == 'audit_brands':
            limit = input_data.get('limit', 10)
            return (
                f"Аудит брендов.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проверь максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Проверь бренды на корректность\n"
                f"3. Найди потенциальные проблемы\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n\n"
                f"Верни JSON: {{total, correct, issues: [{{product_id, brand, issue}}]}}"
            )

        return (
            f"Задача по брендам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Нормализуй бренды, сохрани через update_imported_product и верни результат в JSON."
        )

    def _prefetch_products_brief(self, product_ids: list) -> list:
        """Предзагрузка кратких данных товаров для встраивания в промпт."""
        try:
            return self.platform.get_imported_products_brief(product_ids)
        except Exception as e:
            logger.warning(f"Failed to prefetch products brief: {e}")
            return []
