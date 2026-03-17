# -*- coding: utf-8 -*-
"""
Инструменты (tools) для агентов.

Определяет функции, которые LLM может вызывать через function calling.
Каждый инструмент — обёртка над Internal API платформы.
"""
import json
import logging
from typing import Callable

logger = logging.getLogger(__name__)


# ── Реестр инструментов ────────────────────────────────────────────

class ToolRegistry:
    """Хранит инструменты и выполняет вызовы."""

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}

    def register(self, name: str, description: str,
                 parameters: dict, handler: Callable):
        """Регистрирует инструмент."""
        self._tools[name] = {
            'name': name,
            'description': description,
            'input_schema': {
                'type': 'object',
                'properties': parameters.get('properties', {}),
                'required': parameters.get('required', []),
            },
        }
        self._handlers[name] = handler

    def merge(self, other: 'ToolRegistry'):
        """Объединяет инструменты из другого реестра в текущий."""
        self._tools.update(other._tools)
        self._handlers.update(other._handlers)

    def remove(self, name: str):
        """Удаляет инструмент из реестра."""
        self._tools.pop(name, None)
        self._handlers.pop(name, None)

    def get_tool_schemas(self) -> list[dict]:
        """Возвращает схемы всех инструментов для LLM."""
        return list(self._tools.values())

    def execute(self, name: str, arguments: dict) -> str:
        """Выполняет инструмент по имени с валидацией аргументов."""
        handler = self._handlers.get(name)
        if not handler:
            return json.dumps({'error': f'Unknown tool: {name}'})

        # Валидация аргументов по схеме
        schema = self._tools.get(name, {}).get('input_schema', {})
        required = schema.get('required', [])
        properties = schema.get('properties', {})

        # Проверяем обязательные аргументы
        missing = [f for f in required if f not in arguments]
        if missing:
            return json.dumps({
                'error': f'Missing required arguments: {", ".join(missing)}',
                'tool': name,
            })

        # Фильтруем неизвестные аргументы (защита от LLM-галлюцинаций)
        if properties:
            filtered_args = {k: v for k, v in arguments.items() if k in properties}
        else:
            filtered_args = arguments

        # Приведение типов для числовых полей
        for arg_name, arg_value in list(filtered_args.items()):
            prop_schema = properties.get(arg_name, {})
            expected_type = prop_schema.get('type')
            if expected_type == 'integer' and isinstance(arg_value, (str, float)):
                try:
                    filtered_args[arg_name] = int(arg_value)
                except (ValueError, TypeError):
                    pass
            elif expected_type == 'string' and not isinstance(arg_value, str):
                filtered_args[arg_name] = str(arg_value)

        try:
            result = handler(**filtered_args)
            if isinstance(result, dict) or isinstance(result, list):
                return json.dumps(result, ensure_ascii=False, indent=2)
            return str(result)
        except TypeError as e:
            # Логируем с деталями для дебага
            logger.error(f"Tool {name} type error: {e} (args: {list(filtered_args.keys())})")
            return json.dumps({'error': f'Invalid arguments for {name}: {e}'})
        except Exception as e:
            logger.error(f"Tool {name} error: {e}")
            return json.dumps({'error': str(e)})


# ── Стандартные инструменты платформы ──────────────────────────────

def create_platform_tools(platform_client) -> ToolRegistry:
    """Создаёт набор инструментов для работы с платформой."""
    registry = ToolRegistry()

    # ── Товары ─────────────────────────────────────────────────

    registry.register(
        name='get_products',
        description='Получить список товаров продавца. Возвращает массив товаров с id, title, brand, category, status. Максимум 20 товаров за запрос.',
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
                'page': {'type': 'integer', 'description': 'Номер страницы (default: 1)'},
                'per_page': {'type': 'integer', 'description': 'Товаров на странице (default: 20, max: 20)'},
                'status': {'type': 'string', 'description': 'Фильтр по статусу WB'},
            },
            'required': ['seller_id'],
        },
        handler=lambda seller_id, page=1, per_page=20, status=None:
            platform_client.list_products(seller_id, page, min(int(per_page), 20), status),
    )

    registry.register(
        name='get_product',
        description='Получить детальную информацию о конкретном товаре по ID.',
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
                'product_id': {'type': 'integer', 'description': 'ID товара'},
            },
            'required': ['seller_id', 'product_id'],
        },
        handler=lambda seller_id, product_id:
            platform_client.get_product(seller_id, product_id),
    )

    registry.register(
        name='update_product',
        description='Обновить данные товара: заголовок, описание, бренд, категорию WB, SEO-заголовок и др.',
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
                'product_id': {'type': 'integer', 'description': 'ID товара'},
                'title': {'type': 'string', 'description': 'Новый заголовок'},
                'description': {'type': 'string', 'description': 'Новое описание'},
                'brand': {'type': 'string', 'description': 'Новый бренд'},
                'wb_category_id': {'type': 'integer', 'description': 'ID категории WB (subjectID)'},
                'wb_category_name': {'type': 'string', 'description': 'Название категории WB'},
                'ai_seo_title': {'type': 'string', 'description': 'SEO-оптимизированный заголовок'},
                'tags': {'type': 'string', 'description': 'Теги через запятую'},
            },
            'required': ['seller_id', 'product_id'],
        },
        handler=lambda seller_id, product_id, **updates:
            platform_client.update_product(seller_id, product_id, updates),
    )

    registry.register(
        name='get_imported_products',
        description='Получить товары от поставщика (ещё не обработанные). Максимум 20 товаров за запрос.',
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
                'page': {'type': 'integer', 'description': 'Номер страницы (default: 1)'},
                'per_page': {'type': 'integer', 'description': 'Товаров на странице (default: 20, max: 20)'},
            },
            'required': ['seller_id'],
        },
        handler=lambda seller_id, page=1, per_page=20:
            platform_client.list_imported_products(seller_id, page, min(int(per_page), 20)),
    )

    registry.register(
        name='get_imported_product',
        description='Получить детальную информацию об одном импортированном товаре по ID.',
        parameters={
            'properties': {
                'product_id': {'type': 'integer', 'description': 'ID импортированного товара'},
            },
            'required': ['product_id'],
        },
        handler=lambda product_id: platform_client.get_imported_product(product_id),
    )

    registry.register(
        name='update_imported_product',
        description=(
            'Обновить данные импортированного товара: категорию WB, бренд, характеристики и др. '
            'ЗАЩИТА ЦЕН: платформа запрещает установку цены ниже закупочной + минимальная наценка (по умолчанию 20%).'
        ),
        parameters={
            'properties': {
                'product_id': {'type': 'integer', 'description': 'ID импортированного товара'},
                'mapped_wb_category': {'type': 'string', 'description': 'Название конечной (leaf) категории WB (subject_name из search_wb_categories)'},
                'wb_subject_id': {'type': 'integer', 'description': 'ID конечной категории WB (subject_id из search_wb_categories)'},
                'category_confidence': {'type': 'number', 'description': 'Уверенность в выборе категории (0.0-1.0)'},
                'brand': {'type': 'string', 'description': 'Бренд'},
                'title': {'type': 'string', 'description': 'Заголовок товара'},
                'description': {'type': 'string', 'description': 'Описание товара'},
                'characteristics': {'type': 'string', 'description': 'JSON характеристик'},
                'sizes': {'type': 'string', 'description': 'JSON размеров'},
                'gender': {'type': 'string', 'description': 'Пол (мужской/женский/унисекс)'},
                'country': {'type': 'string', 'description': 'Страна производства'},
                'calculated_price': {'type': 'number', 'description': 'Рассчитанная цена (защита: не ниже закупка + min_profit%)'},
                'calculated_discount_price': {'type': 'number', 'description': 'Цена со скидкой SPP (защита: не ниже закупка + min_profit%)'},
                'calculated_price_before_discount': {'type': 'number', 'description': 'Цена до скидки (защита: не ниже закупка + min_profit%)'},
            },
            'required': ['product_id'],
        },
        handler=lambda product_id, **updates:
            platform_client.update_imported_product(product_id, updates),
    )

    # ── Справочник категорий WB ──────────────────────────────────

    registry.register(
        name='search_wb_categories',
        description=(
            'Поиск КОНЕЧНЫХ (leaf) категорий WB по названию из локального справочника. '
            'Возвращает ТОЛЬКО leaf-категории, которые WB API реально принимает. '
            'subject_id и subject_name — это конечная категория для карточки. '
            'parent_name — родительский раздел (для информации, НЕ для записи в карточку). '
            'Ищет и по subject_name, и по parent_name — запрос "Товары для взрослых" вернёт '
            'все конечные категории этого раздела.'
        ),
        parameters={
            'properties': {
                'query': {'type': 'string', 'description': 'Поисковый запрос (название товара или категории, минимум 2 символа)'},
                'limit': {'type': 'integer', 'description': 'Макс. количество результатов (по умолчанию 10)'},
            },
            'required': ['query'],
        },
        handler=lambda query, limit=10:
            platform_client.search_categories(query, min(int(limit), 20)),
    )

    # ── Продавец ───────────────────────────────────────────────

    registry.register(
        name='get_seller_info',
        description='Получить информацию о продавце: название компании, наличие API-ключа WB.',
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
            },
            'required': ['seller_id'],
        },
        handler=lambda seller_id: platform_client.get_seller(seller_id),
    )

    # ── Характеристики категории ────────────────────────────────

    registry.register(
        name='get_category_characteristics',
        description=(
            'Получить ВСЕ характеристики категории WB по subject_id. '
            'Возвращает: название, тип (Число/Строка), единицу измерения, допустимые значения из словаря. '
            'Всегда возвращает полный список — заполняй МАКСИМУМ характеристик из описания товара.'
        ),
        parameters={
            'properties': {
                'subject_id': {'type': 'integer', 'description': 'ID категории WB (subjectID)'},
            },
            'required': ['subject_id'],
        },
        handler=lambda subject_id:
            platform_client.get_category_characteristics(int(subject_id), False),
    )

    # ── Справочники WB (цвета, страны, сезоны) ─────────────────

    registry.register(
        name='get_directory',
        description=(
            'Получить справочник WB: colors (цвета), countries (страны), kinds (пол), seasons (сезоны). '
            'Используй для заполнения характеристик значениями из реального справочника WB. '
            'Можно фильтровать по подстроке.'
        ),
        parameters={
            'properties': {
                'directory_type': {
                    'type': 'string',
                    'description': 'Тип справочника: colors, countries, kinds, seasons',
                },
                'query': {'type': 'string', 'description': 'Поисковый запрос для фильтрации (опционально)'},
                'limit': {'type': 'integer', 'description': 'Максимум записей (default: 50)'},
            },
            'required': ['directory_type'],
        },
        handler=lambda directory_type, query=None, limit=50:
            platform_client.get_directory(directory_type, query, min(int(limit), 200)),
    )

    # ── Запрещённые слова ───────────────────────────────────────

    registry.register(
        name='get_prohibited_words',
        description=(
            'Получить список запрещённых слов WB (стоп-слова). '
            'Возвращает слова и их безопасные замены. Используй для проверки '
            'и очистки заголовков и описаний перед публикацией.'
        ),
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца для персональных стоп-слов (опционально)'},
                'query': {'type': 'string', 'description': 'Поиск по конкретному слову (опционально)'},
            },
            'required': [],
        },
        handler=lambda seller_id=None, query=None:
            platform_client.get_prohibited_words(
                int(seller_id) if seller_id else None, query
            ),
    )

    registry.register(
        name='check_text_prohibited',
        description=(
            'Проверить текст на запрещённые слова WB. Возвращает найденные стоп-слова '
            'и очищенный текст с заменами. Используй для проверки заголовков и описаний.'
        ),
        parameters={
            'properties': {
                'text': {'type': 'string', 'description': 'Текст для проверки'},
                'seller_id': {'type': 'integer', 'description': 'ID продавца (опционально)'},
            },
            'required': ['text'],
        },
        handler=lambda text, seller_id=None:
            platform_client.check_prohibited_words(
                text, int(seller_id) if seller_id else None
            ),
    )

    # ── Валидация бренда ────────────────────────────────────────

    registry.register(
        name='validate_brand',
        description=(
            'Проверить бренд по реестру WB в конкретной категории. '
            'category_id (wb_subject_id товара) ОБЯЗАТЕЛЕН — без него результат бессмысленен, '
            'т.к. бренд может быть в WB глобально, но недоступен в категории товара. '
            'Возвращает: найден ли бренд, каноническое написание, похожие варианты.'
        ),
        parameters={
            'properties': {
                'brand_name': {'type': 'string', 'description': 'Название бренда для проверки'},
                'category_id': {'type': 'integer', 'description': 'wb_subject_id категории товара (ОБЯЗАТЕЛЕН)'},
            },
            'required': ['brand_name', 'category_id'],
        },
        handler=lambda brand_name, category_id=None:
            ({'error': 'category_id (wb_subject_id) обязателен! Возьми его из данных товара (поле wb_subject_id).'}
             if not category_id else
             platform_client.validate_brand(brand_name, int(category_id))),
    )

    # ── Настройки ценообразования ───────────────────────────────

    registry.register(
        name='get_pricing_settings',
        description=(
            'Получить настройки ценообразования продавца: комиссию WB (%), налоговый коэффициент, '
            'стоимость логистики, упаковки, хранения, таблицу наценок. '
            'ОБЯЗАТЕЛЬНО используй для расчёта unit-экономики вместо стандартных значений.'
        ),
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
            },
            'required': ['seller_id'],
        },
        handler=lambda seller_id:
            platform_client.get_pricing_settings(int(seller_id)),
    )

    return registry


def create_orchestrator_tools(platform_client) -> ToolRegistry:
    """Создаёт инструменты для агента-оркестратора."""
    registry = ToolRegistry()

    registry.register(
        name='create_subtask',
        description='Создать подзадачу для специализированного агента. Возвращает task_id созданной задачи.',
        parameters={
            'properties': {
                'agent_name': {'type': 'string', 'description': 'Имя агента (category-mapper, seo-writer, и т.д.)'},
                'task_type': {'type': 'string', 'description': 'Тип задачи (map_batch, seo_batch, и т.д.)'},
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
                'title': {'type': 'string', 'description': 'Название подзадачи'},
                'input_data': {'type': 'string', 'description': 'JSON строка с входными данными (product_ids, limit и т.д.)'},
                'parent_task_id': {'type': 'string', 'description': 'ID родительской задачи (для связи подзадач)'},
            },
            'required': ['agent_name', 'task_type', 'seller_id', 'title'],
        },
        handler=lambda agent_name, task_type, seller_id, title, input_data='{}', parent_task_id=None:
            platform_client.create_subtask(
                agent_name=agent_name,
                task_type=task_type,
                seller_id=int(seller_id),
                title=title,
                input_data=json.loads(input_data) if isinstance(input_data, str) else input_data,
                parent_task_id=parent_task_id,
            ),
    )

    registry.register(
        name='get_subtask_status',
        description='Проверить статус подзадачи. Возвращает task с полями status, completed_steps, result.',
        parameters={
            'properties': {
                'task_id': {'type': 'string', 'description': 'ID задачи'},
            },
            'required': ['task_id'],
        },
        handler=lambda task_id: platform_client.get_task_status(task_id),
    )

    registry.register(
        name='get_subtask_result',
        description='Получить результат завершённой подзадачи.',
        parameters={
            'properties': {
                'task_id': {'type': 'string', 'description': 'ID задачи'},
            },
            'required': ['task_id'],
        },
        handler=lambda task_id: platform_client.get_task_status(task_id),
    )

    return registry
