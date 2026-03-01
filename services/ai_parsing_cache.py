# -*- coding: utf-8 -*-
"""
Кэширование результатов AI-парсинга.

Позволяет избежать повторных AI-вызовов для неизменённых товаров.
Хэш вычисляется из входных данных товара — если данные не менялись,
результат берётся из кэша.
"""
import hashlib
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class AIParsingCache:
    """
    Кэш AI-парсинга на основе хэша входных данных товара.

    Работает через поля SupplierProduct:
    - content_hash: хэш входных данных
    - ai_parsed_data_json: кэшированный результат
    - ai_parsed_at: время парсинга
    """

    @staticmethod
    def compute_input_hash(product_data: dict) -> str:
        """
        Вычислить хэш входных данных для AI-парсинга.
        Хэш включает все данные, которые влияют на результат парсинга.
        """
        # Собираем только те поля, которые идут в промпт AI
        hash_fields = {
            'title': product_data.get('title', ''),
            'description': product_data.get('description', ''),
            'brand': product_data.get('brand', ''),
            'category': product_data.get('category', ''),
            'wb_category': product_data.get('wb_category', ''),
            'colors': product_data.get('colors', []),
            'materials': product_data.get('materials', []),
            'sizes': product_data.get('sizes', {}),
            'dimensions': product_data.get('dimensions', {}),
            'gender': product_data.get('gender', ''),
            'country': product_data.get('country', ''),
        }

        # Детерминированная сериализация (sort_keys)
        canonical = json.dumps(hash_fields, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]

    @staticmethod
    def is_cache_valid(product, product_data: dict) -> bool:
        """
        Проверить, актуален ли кэш AI-парсинга для товара.

        Args:
            product: SupplierProduct instance
            product_data: текущие данные товара (dict)

        Returns:
            True если кэш актуален, False если нужен повторный парсинг
        """
        # Нет кэшированных данных
        if not product.ai_parsed_data_json:
            return False

        # Нет хэша — данные парсились без кэширования
        if not product.content_hash:
            return False

        # Сравниваем хэши
        current_hash = AIParsingCache.compute_input_hash(product_data)
        return product.content_hash == current_hash

    @staticmethod
    def get_cached(product) -> Optional[Dict]:
        """
        Получить кэшированный результат AI-парсинга.

        Returns:
            dict с результатом парсинга или None если кэш невалиден
        """
        if not product.ai_parsed_data_json:
            return None

        try:
            return json.loads(product.ai_parsed_data_json)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def save_to_cache(product, product_data: dict, parsed_result: dict, model_used: str = '') -> None:
        """
        Сохранить результат AI-парсинга в кэш.

        Args:
            product: SupplierProduct instance
            product_data: входные данные (для хэша)
            parsed_result: результат парсинга
            model_used: название AI модели
        """
        product.content_hash = AIParsingCache.compute_input_hash(product_data)
        product.ai_parsed_data_json = json.dumps(parsed_result, ensure_ascii=False)
        product.ai_parsed_at = datetime.utcnow()
        if model_used:
            product.ai_model_used = model_used

    @staticmethod
    def invalidate(product) -> None:
        """Инвалидировать кэш для товара."""
        product.content_hash = None

    @staticmethod
    def get_cache_stats(supplier_id: int) -> Dict[str, Any]:
        """
        Статистика кэша AI-парсинга для поставщика.

        Returns:
            {
                'total_products': int,
                'cached': int,
                'not_cached': int,
                'cache_rate': float
            }
        """
        from models import SupplierProduct

        total = SupplierProduct.query.filter_by(supplier_id=supplier_id).count()
        cached = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.content_hash.isnot(None),
            SupplierProduct.ai_parsed_data_json.isnot(None),
        ).count()

        return {
            'total_products': total,
            'cached': cached,
            'not_cached': total - cached,
            'cache_rate': round(cached / total, 3) if total > 0 else 0.0,
        }
