# -*- coding: utf-8 -*-
"""
Конфиденс-скоринг качества парсинга товаров.

Рассчитывает оценку от 0.0 до 1.0 для каждого товара, показывающую
насколько полно и качественно заполнены данные.
Используется для приоритизации ручной проверки.
"""
import json
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ParsingConfidenceScorer:
    """
    Скоринг качества парсинга товара.

    Формула:
    - Базовый балл 1.0
    - Штрафы за отсутствие обязательных полей
    - Штрафы за fuzzy-matched значения
    - Штрафы за невалидные данные
    - Бонус за полноту заполнения
    """

    # Веса обязательных полей
    REQUIRED_FIELD_WEIGHTS = {
        'title': 0.15,
        'brand': 0.10,
        'category': 0.10,
        'vendor_code': 0.05,
        'photo_urls': 0.15,
        'barcodes': 0.05,
    }

    # Веса опциональных полей
    OPTIONAL_FIELD_WEIGHTS = {
        'description': 0.08,
        'colors': 0.05,
        'materials': 0.05,
        'sizes_raw': 0.04,
        'country': 0.03,
        'gender': 0.03,
    }

    @classmethod
    def score_product(cls, product_data: dict) -> float:
        """
        Рассчитать confidence score для товара.

        Args:
            product_data: dict из парсера CSV (до/после нормализации)

        Returns:
            float 0.0 - 1.0
        """
        score = 0.0

        # Обязательные поля — наличие и качество
        for field, weight in cls.REQUIRED_FIELD_WEIGHTS.items():
            value = product_data.get(field)
            field_score = cls._score_field(field, value)
            score += weight * field_score

        # Опциональные поля — бонус за наличие
        for field, weight in cls.OPTIONAL_FIELD_WEIGHTS.items():
            value = product_data.get(field)
            field_score = cls._score_field(field, value)
            score += weight * field_score

        # Дополнительный бонус за полноту (все поля заполнены)
        filled_count = sum(
            1 for f in list(cls.REQUIRED_FIELD_WEIGHTS) + list(cls.OPTIONAL_FIELD_WEIGHTS)
            if cls._has_value(product_data.get(f))
        )
        total_fields = len(cls.REQUIRED_FIELD_WEIGHTS) + len(cls.OPTIONAL_FIELD_WEIGHTS)
        completeness_bonus = 0.12 * (filled_count / total_fields)
        score += completeness_bonus

        return round(max(0.0, min(1.0, score)), 3)

    @classmethod
    def score_supplier_product(cls, product) -> float:
        """
        Рассчитать confidence для SupplierProduct (модель БД).
        """
        data = {
            'title': product.title,
            'brand': product.brand,
            'category': product.category,
            'vendor_code': product.vendor_code,
            'description': product.description,
            'country': product.country,
            'gender': product.gender,
        }

        # JSON поля
        try:
            data['photo_urls'] = json.loads(product.photo_urls_json) if product.photo_urls_json else []
        except Exception:
            data['photo_urls'] = []
        try:
            data['colors'] = json.loads(product.colors_json) if product.colors_json else []
        except Exception:
            data['colors'] = []
        try:
            data['materials'] = json.loads(product.materials_json) if product.materials_json else []
        except Exception:
            data['materials'] = []
        try:
            sizes_data = json.loads(product.sizes_json) if product.sizes_json else {}
            data['sizes_raw'] = sizes_data.get('raw', '')
        except Exception:
            data['sizes_raw'] = ''

        data['barcodes'] = [product.barcode] if product.barcode else []

        # AI-данные добавляют бонус
        base_score = cls.score_product(data)

        # Бонус за AI-парсинг
        ai_bonus = 0.0
        if product.ai_parsed_data_json:
            try:
                ai_data = json.loads(product.ai_parsed_data_json)
                meta = ai_data.get('_meta', {})
                filled = meta.get('filled_fields', 0)
                total = meta.get('total_fields', 1)
                missing_req = len(meta.get('missing_required', []))
                issues = len(meta.get('validation_issues', []))

                if filled > 0:
                    ai_bonus += 0.05 * min(1.0, filled / max(total, 1))
                ai_bonus -= 0.03 * missing_req
                ai_bonus -= 0.01 * issues
            except Exception:
                pass

        # Штраф за низкий confidence маппинга категории
        if product.category_confidence is not None and product.category_confidence < 0.7:
            ai_bonus -= 0.05

        # Бонус за marketplace валидацию
        if product.marketplace_validation_status == 'valid':
            ai_bonus += 0.05
        elif product.marketplace_validation_status == 'partial':
            ai_bonus += 0.02

        return round(max(0.0, min(1.0, base_score + ai_bonus)), 3)

    @classmethod
    def _score_field(cls, field: str, value) -> float:
        """Оценить качество конкретного поля."""
        if not cls._has_value(value):
            return 0.0

        if field == 'title':
            title = str(value)
            if len(title) < 5:
                return 0.3
            if len(title) < 15:
                return 0.6
            if len(title) > 150:
                return 0.7  # Слишком длинный
            return 1.0

        if field == 'photo_urls':
            if isinstance(value, list):
                if len(value) == 0:
                    return 0.0
                if len(value) == 1:
                    return 0.5
                if len(value) >= 3:
                    return 1.0
                return 0.7
            return 0.0

        if field == 'barcodes':
            if isinstance(value, list) and value:
                barcode = str(value[0])
                if len(barcode) == 13:
                    return 1.0  # EAN-13
                if len(barcode) >= 8:
                    return 0.8
                return 0.5
            return 0.0

        if field == 'description':
            desc = str(value)
            if len(desc) < 20:
                return 0.3
            if len(desc) < 100:
                return 0.6
            return 1.0

        # Для остальных полей: есть значение = 1.0
        return 1.0

    @staticmethod
    def _has_value(value) -> bool:
        """Проверить, есть ли значение."""
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict)):
            return bool(value)
        return True

    @classmethod
    def get_quality_distribution(cls, supplier_id: int) -> Dict[str, int]:
        """
        Распределение товаров по уровням качества для поставщика.

        Returns:
            {"high": N, "medium": N, "low": N, "critical": N}
        """
        from models import SupplierProduct

        products = SupplierProduct.query.filter_by(
            supplier_id=supplier_id
        ).all()

        distribution = {'high': 0, 'medium': 0, 'low': 0, 'critical': 0}
        for product in products:
            confidence = product.parsing_confidence
            if confidence is None:
                confidence = cls.score_supplier_product(product)
            if confidence >= 0.8:
                distribution['high'] += 1
            elif confidence >= 0.6:
                distribution['medium'] += 1
            elif confidence >= 0.4:
                distribution['low'] += 1
            else:
                distribution['critical'] += 1

        return distribution
