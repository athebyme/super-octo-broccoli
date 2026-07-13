# -*- coding: utf-8 -*-
"""
Агент характеристик — заполнение обязательных и рекомендованных характеристик карточки WB.
"""
import json
import logging

from ..base_agent import BaseAgent, _build_usage
from ..platform_client import (
    ReferenceDataUnavailableError,
    require_usable_reference,
)

logger = logging.getLogger(__name__)


class CharacteristicsFillerAgent(BaseAgent):
    agent_name = 'characteristics-filler'
    max_iterations = 15
    # Category schemas are prefetched and validated once before chunking.
    tool_batch_excluded_tools = ('get_category_characteristics',)
    tool_allowlist = (
        'get_product', 'update_product', 'get_imported_products',
        'get_imported_product', 'update_imported_product',
        'batch_update_imported_products', 'get_category_characteristics',
    )

    system_prompt = """Ты — эксперт по характеристикам карточек Wildberries.

Твоя задача — заполнить МАКСИМУМ характеристик товара по схеме WB.

ФОРМАТ: JSON-словарь {имя_характеристики: значение}
Пример: {"Цвет": "черный", "Страна производства": "Китай", "Материал изделия": "силикон", "Пол": "Женский", "Комплектация": "1 шт", "Длина": 15}

ПРАВИЛА:
1. Данные товара и схема характеристик категории УЖЕ ЗАГРУЖЕНЫ в промпте — НЕ вызывай get_imported_product и get_category_characteristics
2. Заполни МАКСИМУМ характеристик из описания/названия товара — не только required
3. Извлекай: материал, цвет, страну, пол, комплектацию, размеры, вес, объём, количество в упаковке и любые другие данные
4. Для constraint.constrained=true используй ТОЛЬКО канонические constraint.values
5. constraint.values может быть ограниченной выборкой (truncated=true). Если точного значения в ней нет — пропусти поле
6. Если constraint.usable=false у необязательного поля — пропусти его. Обязательная непроверяемая схема блокируется до LLM
7. Числовые (type="Число") = число. Строковые (type="Строка") = строка
8. Ключи = ТОЧНЫЕ названия характеристик (поле name)

ЗАПРЕЩЕНО:
- Выдумывать значения — если данных нет, пропускай
- Оставлять characteristics пустым если в описании есть хоть какие-то данные
- Использовать схему, если reference_status.usable=false, stale=true или available=false.
  В этом случае не вызывай update tools: останови задачу и запроси синхронизацию с WB

Для одиночной ImportedProduct вызови update_imported_product, для основной
Product-карточки — update_product. В tool-assisted batch и режиме предложения
не вызывай update tools: Python harness сам проверяет и сохраняет typed results.

Результат: JSON с полями: characteristics, filled_count, missing, confidence."""

    def execute_task(self, task: dict) -> dict:
        """Batch: tool-assisted с предзагрузкой характеристик категорий. Single: ReAct."""
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'fill_single')
        try:
            if task_type in ('fill_batch',):
                product_ids = (
                    input_data.get('product_ids')
                    or input_data.get('imported_product_ids')
                    or []
                )
                if len(product_ids) > 1:
                    return self._execute_tool_batch(
                        task, product_ids, chunk_size=15, max_workers=2,
                    )
            return self._execute_react(task)
        except ReferenceDataUnavailableError as exc:
            selected_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )
            total = len(selected_ids)
            if not total and (
                input_data.get('product_id')
                or input_data.get('imported_product_id')
            ):
                total = 1
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

    @staticmethod
    def _missing_category_error(subject_id=None):
        payload = {
            'warning': 'У товара нет доступной категории WB. Сначала определите категорию.',
            'reference_status': {
                'source': f'wb_category_characteristics:{subject_id or "unknown"}',
                'usable': False,
                'available': False,
                'stale': False,
                'reason': 'category_required',
            },
        }
        return ReferenceDataUnavailableError('wb_category_characteristics', payload)

    def _prefetch_reference_data(self, products_data: list[dict]) -> dict:
        """Load all unique category schemas with one typed internal request."""
        if any(not p.get('wb_subject_id') for p in products_data):
            raise self._missing_category_error()

        subject_ids = []
        seen_subject_ids = set()
        for product in products_data:
            subject_id = product.get('wb_subject_id')
            if subject_id in seen_subject_ids:
                continue
            seen_subject_ids.add(subject_id)
            subject_ids.append(subject_id)
        try:
            payload = self.platform.get_category_characteristics_batch(
                subject_ids, False,
            )
        except ReferenceDataUnavailableError:
            raise
        except Exception as exc:
            logger.warning('Failed to prefetch characteristic batch: %s', exc)
            raise ReferenceDataUnavailableError(
                'wb_category_characteristics',
                {
                    'warning': 'Не удалось загрузить текущие схемы характеристик WB.',
                    'reference_status': {
                        'source': 'wb_category_characteristics',
                        'usable': False,
                        'available': False,
                        'stale': False,
                        'reason': 'request_failed',
                    },
                },
            ) from exc

        results = payload.get('results') if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != len(subject_ids):
            raise ReferenceDataUnavailableError(
                'wb_category_characteristics', payload,
            )
        chars_by_subject = {}
        for expected_id, result in zip(subject_ids, results):
            if (
                not isinstance(result, dict)
                or result.get('subject_id') != expected_id
            ):
                raise ReferenceDataUnavailableError(
                    f'wb_category_characteristics:{expected_id}', payload,
                )
            chars_by_subject[expected_id] = self._characteristics_from_payload(
                result, expected_id,
            )

        return {'chars_by_subject': chars_by_subject}

    def _build_tool_batch_prompt(
        self, products_data: list[dict], reference_data: dict,
    ) -> str:
        """Промпт с данными товаров и предзагруженными характеристиками."""
        products_json = json.dumps(
            products_data, ensure_ascii=False, separators=(',', ':'),
        )

        chars_by_subject = reference_data.get('chars_by_subject', {})
        relevant_subject_ids = {
            product.get('wb_subject_id') for product in products_data
            if product.get('wb_subject_id')
        }
        chars_parts = []
        for sid in sorted(relevant_subject_ids):
            chars = chars_by_subject.get(sid)
            if not isinstance(chars, list):
                continue
            chars_json = json.dumps(
                chars, ensure_ascii=False, separators=(',', ':'),
            )
            chars_parts.append(f'subject_id={sid}:\n{chars_json}')

        chars_text = '\n\n'.join(chars_parts) if chars_parts else 'Нет предзагруженных характеристик.'

        return (
            f"Заполни характеристики для {len(products_data)} товаров.\n\n"
            f"=== ДАННЫЕ ТОВАРОВ (уже загружены) ===\n{products_json}\n\n"
            f"=== СХЕМЫ ХАРАКТЕРИСТИК ПО КАТЕГОРИЯМ (уже загружены) ===\n{chars_text}\n\n"
            f"ВСЕ ДАННЫЕ УЖЕ ЗАГРУЖЕНЫ. НЕ вызывай get_imported_product и get_category_characteristics.\n\n"
            f"Для КАЖДОГО товара:\n"
            f"1. Найди его wb_subject_id → возьми схему характеристик выше\n"
            f"2. Извлеки значения из title/description/brand/category\n"
            f"3. Подготовь typed result с product_id и characteristics\n\n"
            f"Формат characteristics: {{\"Цвет\": \"черный\", \"Страна производства\": \"Китай\"}}\n"
            f"Ключи = ТОЧНЫЕ названия из схемы (поле name).\n"
            f"НЕ вызывай update_imported_product или batch_update_imported_products. "
            f"Сохранение выполнит Python harness ровно одним batch-вызовом после "
            f"проверки результата.\n\n"
            f"Верни ОДИН финальный JSON без markdown: "
            f"{{\"results\":[{{\"product_id\":ID,\"characteristics\":"
            f"{{\"Цвет\":\"Черный\"}},\"filled_count\":1,"
            f"\"missing\":[],\"confidence\":0.9}}]}}. "
            f"Если достоверных значений нет, верни product_id и error."
        )

    def _map_tool_batch_result_to_updates(
        self, chunk_result: dict, products_data: list[dict],
    ) -> list[dict]:
        """Serialize validated characteristic dictionaries for one batch write."""
        results = chunk_result.get('results')
        if not isinstance(results, list):
            raise ValueError('Characteristics batch result must contain results array')

        allowed_ids = {item.get('id') for item in products_data}
        seen = set()
        updates = []
        for item in results:
            if not isinstance(item, dict):
                raise ValueError('Characteristics result item must be an object')
            product_id = item.get('product_id')
            if (
                not isinstance(product_id, int)
                or isinstance(product_id, bool)
                or product_id not in allowed_ids
            ):
                raise ValueError(
                    'Characteristics result references product outside chunk',
                )
            if product_id in seen:
                raise ValueError(
                    'Characteristics result contains duplicate product_id',
                )
            seen.add(product_id)
            if item.get('error'):
                continue

            characteristics = item.get('characteristics')
            if (
                not isinstance(characteristics, dict)
                or not characteristics
                or any(
                    not isinstance(name, str) or not name.strip()
                    for name in characteristics
                )
            ):
                continue
            updates.append({
                'product_id': product_id,
                'characteristics': json.dumps(
                    characteristics,
                    ensure_ascii=False,
                    separators=(',', ':'),
                ),
            })
        return updates

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
                return self._build_product_prompt(
                    seller_id,
                    product_id,
                    propose=input_data.get('mode') == 'propose',
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
        if not subject_id:
            raise self._missing_category_error(subject_id)
        chars_schema = self._prefetch_category_chars(subject_id)

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
                f"Для constraint.constrained=true используй ТОЛЬКО constraint.values.\n"
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

    def _build_product_prompt(
        self, seller_id: int, product_id: int, *, propose: bool = False,
    ) -> str:
        """Prefetch the main card and WB schema before the first LLM call."""
        try:
            product_data = self.platform.get_product(seller_id, product_id)
        except Exception as exc:
            logger.warning('Failed to prefetch product %s: %s', product_id, exc)
            return (
                f"Не удалось загрузить карточку Product ID {product_id}. "
                "Верни JSON: {\"error\": \"product_unavailable\"}."
            )

        if not isinstance(product_data, dict) or not product_data:
            return (
                f"Карточка Product ID {product_id} не найдена. "
                "Верни JSON: {\"error\": \"product_not_found\"}."
            )

        subject_id = (
            product_data.get('subject_id')
            or product_data.get('wb_subject_id')
            or product_data.get('wb_category_id')
        )
        if not subject_id:
            raise self._missing_category_error()
        chars_schema = self._prefetch_category_chars(subject_id)
        product_text = json.dumps(
            product_data, ensure_ascii=False, separators=(',', ':'),
        )
        chars_text = json.dumps(
            chars_schema, ensure_ascii=False, separators=(',', ':'),
        )

        action = (
            "Не вызывай update_product или update_imported_product; только предложи patch."
            if propose else
            f"Вызови update_product(seller_id={seller_id}, product_id={product_id}, "
            "characteristics=<JSON-словарь>) ровно один раз."
        )
        return (
            f"{'Предложи' if propose else 'Заполни'} характеристики карточки WB.\n"
            f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
            f"=== ДАННЫЕ ТОВАРА ===\n{product_text}\n\n"
            f"=== АКТУАЛЬНАЯ СХЕМА WB subject_id={subject_id} ===\n{chars_text}\n\n"
            "Товар и схема уже загружены: не вызывай get_product и "
            "get_category_characteristics. Не выдумывай отсутствующие факты. "
            "Ключи patch должны точно совпадать с name из схемы; при "
            "constraint.constrained=true используй только constraint.values. "
            "Если список truncated и точного значения нет, пропусти поле.\n"
            f"{action}\n"
            "Верни JSON: {characteristics: {...}, filled_count: N, "
            "missing: [...], confidence: 0..1}."
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
            raise self._missing_category_error(subject_id)
        try:
            data = self.platform.get_category_characteristics(subject_id, False)
            return self._characteristics_from_payload(data, subject_id)
        except ReferenceDataUnavailableError:
            raise
        except Exception as e:
            logger.warning(f"Failed to prefetch chars for subject {subject_id}: {e}")
            raise ReferenceDataUnavailableError(
                f'wb_category_characteristics:{subject_id}',
                {
                    'warning': 'Не удалось загрузить текущую схему характеристик WB.',
                    'reference_status': {
                        'source': f'wb_category_characteristics:{subject_id}',
                        'usable': False,
                        'available': False,
                        'stale': False,
                        'reason': 'request_failed',
                    },
                },
            ) from e

    @staticmethod
    def _characteristics_from_payload(data: dict, subject_id: int) -> list:
        require_usable_reference(
            data, f'wb_category_characteristics:{subject_id}',
        )
        characteristics = data.get('characteristics')
        if not isinstance(characteristics, list) or not characteristics:
            raise ReferenceDataUnavailableError(
                f'wb_category_characteristics:{subject_id}', data,
            )
        return characteristics

    def _prefetch_products_brief(self, product_ids: list) -> list:
        """Предзагрузка кратких данных товаров для встраивания в промпт."""
        try:
            return self.platform.get_imported_products_brief(product_ids)
        except Exception as e:
            logger.warning(f"Failed to prefetch products brief: {e}")
            return []
