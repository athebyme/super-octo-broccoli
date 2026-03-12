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

    def get_tool_schemas(self) -> list[dict]:
        """Возвращает схемы всех инструментов для LLM."""
        return list(self._tools.values())

    def execute(self, name: str, arguments: dict) -> str:
        """Выполняет инструмент по имени."""
        handler = self._handlers.get(name)
        if not handler:
            return json.dumps({'error': f'Unknown tool: {name}'})
        try:
            result = handler(**arguments)
            if isinstance(result, dict) or isinstance(result, list):
                return json.dumps(result, ensure_ascii=False, indent=2)
            return str(result)
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
        description='Получить список товаров продавца. Возвращает массив товаров с id, title, brand, category, status.',
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
                'page': {'type': 'integer', 'description': 'Номер страницы (default: 1)'},
                'per_page': {'type': 'integer', 'description': 'Товаров на странице (default: 50)'},
                'status': {'type': 'string', 'description': 'Фильтр по статусу WB'},
            },
            'required': ['seller_id'],
        },
        handler=lambda seller_id, page=1, per_page=50, status=None:
            platform_client.list_products(seller_id, page, per_page, status),
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
        description='Обновить данные товара (title, description, brand, характеристики, SEO-заголовок).',
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
                'product_id': {'type': 'integer', 'description': 'ID товара'},
                'title': {'type': 'string', 'description': 'Новый заголовок'},
                'description': {'type': 'string', 'description': 'Новое описание'},
                'brand': {'type': 'string', 'description': 'Новый бренд'},
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
        description='Получить товары, импортированные от поставщика (ещё не обработанные).',
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
                'page': {'type': 'integer', 'description': 'Номер страницы'},
                'per_page': {'type': 'integer', 'description': 'Товаров на странице'},
            },
            'required': ['seller_id'],
        },
        handler=lambda seller_id, page=1, per_page=50:
            platform_client.list_imported_products(seller_id, page, per_page),
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


# ── Кастомные инструменты для специализированных агентов ───────────

def create_seo_tools() -> list[dict]:
    """Дополнительные tool-определения для SEO-агента (чисто LLM-based)."""
    return [
        {
            'name': 'analyze_keywords',
            'description': 'Анализирует ключевые слова товара и конкурентов. Выделяет высокочастотные и низкоконкурентные фразы.',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'product_title': {'type': 'string', 'description': 'Текущий заголовок товара'},
                    'product_category': {'type': 'string', 'description': 'Категория товара'},
                    'product_description': {'type': 'string', 'description': 'Описание товара'},
                },
                'required': ['product_title'],
            },
        },
        {
            'name': 'generate_seo_title',
            'description': 'Генерирует SEO-оптимизированный заголовок для WB. Учитывает ограничение 60 символов и ключевые слова.',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'product_title': {'type': 'string'},
                    'keywords': {'type': 'array', 'items': {'type': 'string'}},
                    'brand': {'type': 'string'},
                    'category': {'type': 'string'},
                },
                'required': ['product_title'],
            },
        },
        {
            'name': 'generate_seo_description',
            'description': 'Генерирует SEO-описание для карточки WB. До 1000 символов с ключевыми словами.',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'product_title': {'type': 'string'},
                    'current_description': {'type': 'string'},
                    'keywords': {'type': 'array', 'items': {'type': 'string'}},
                    'characteristics': {'type': 'string'},
                },
                'required': ['product_title'],
            },
        },
    ]


def create_category_tools() -> list[dict]:
    """Дополнительные tool-определения для агента категорий."""
    return [
        {
            'name': 'analyze_product_for_category',
            'description': 'Анализирует товар и определяет наиболее подходящую категорию WB (subjectID).',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string', 'description': 'Название товара'},
                    'description': {'type': 'string', 'description': 'Описание'},
                    'brand': {'type': 'string', 'description': 'Бренд'},
                    'characteristics': {'type': 'string', 'description': 'Характеристики JSON'},
                },
                'required': ['title'],
            },
        },
        {
            'name': 'validate_category_mapping',
            'description': 'Проверяет корректность маппинга категории. Подтверждает или предлагает альтернативу.',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'product_title': {'type': 'string'},
                    'current_category': {'type': 'string'},
                    'suggested_category': {'type': 'string'},
                },
                'required': ['product_title', 'current_category'],
            },
        },
    ]


def create_price_tools() -> list[dict]:
    """Дополнительные tool-определения для агента цен."""
    return [
        {
            'name': 'calculate_unit_economics',
            'description': 'Рассчитывает unit-экономику товара: себестоимость, комиссия WB, логистика, маржа.',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'purchase_price': {'type': 'number', 'description': 'Закупочная цена'},
                    'selling_price': {'type': 'number', 'description': 'Цена продажи'},
                    'category_commission': {'type': 'number', 'description': 'Комиссия категории %'},
                    'logistics_cost': {'type': 'number', 'description': 'Стоимость логистики'},
                    'weight_kg': {'type': 'number', 'description': 'Вес, кг'},
                },
                'required': ['purchase_price', 'selling_price'],
            },
        },
        {
            'name': 'suggest_optimal_price',
            'description': 'Рекомендует оптимальную цену с учётом маржинальности и конкуренции.',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'purchase_price': {'type': 'number'},
                    'current_price': {'type': 'number'},
                    'target_margin': {'type': 'number', 'description': 'Целевая маржа %'},
                    'category': {'type': 'string'},
                },
                'required': ['purchase_price', 'current_price'],
            },
        },
    ]
