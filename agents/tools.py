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
