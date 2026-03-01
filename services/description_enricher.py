# -*- coding: utf-8 -*-
"""
Извлечение характеристик из описания товара.

Парсит описание товара (часто содержит параметры в свободном текстовом виде)
и извлекает структурированные данные:
- Размеры (длина, диаметр, ширина)
- Материал
- Цвет
- Страна производства
- Батарейки / зарядка
- Вес
"""
import re
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class DescriptionEnricher:
    """
    Извлечение структурированных характеристик из текстового описания товара.

    Работает после парсинга CSV — если поля пустые, пробуем заполнить из описания.
    """

    # Паттерны для извлечения размеров (в мм или см)
    SIZE_PATTERNS = [
        # "Длина 15 см", "длина: 150 мм"
        (r'длин[аы]\s*[:\-–]?\s*(\d+[\.,]?\d*)\s*(см|мм|m|cm)', 'length'),
        # "Диаметр 3.5 см"
        (r'диаметр\s*[:\-–]?\s*(\d+[\.,]?\d*)\s*(см|мм|m|cm)', 'diameter'),
        # "Ширина 5 см"
        (r'ширин[аы]\s*[:\-–]?\s*(\d+[\.,]?\d*)\s*(см|мм|m|cm)', 'width'),
        # "Высота 20 см"
        (r'высот[аы]\s*[:\-–]?\s*(\d+[\.,]?\d*)\s*(см|мм|m|cm)', 'height'),
        # "Рабочая длина 12 см"
        (r'рабоч[а-я]*\s+длин[аы]\s*[:\-–]?\s*(\d+[\.,]?\d*)\s*(см|мм)', 'working_length'),
        # "Вес 150 г", "вес: 0.3 кг"
        (r'вес\s*[:\-–]?\s*(\d+[\.,]?\d*)\s*(г|гр|кг|kg|g)', 'weight'),
    ]

    # Паттерны для извлечения материала
    MATERIAL_PATTERNS = [
        r'материал\s*[:\-–]?\s*([а-яёa-z\s,/]+?)(?:\.|,\s*[а-яёА-ЯЁ]|\n|$)',
        r'изготовлен[а-я]*\s+из\s+([а-яёa-z\s,/]+?)(?:\.|,\s*[а-яёА-ЯЁ]|\n|$)',
    ]

    # Паттерны для извлечения цвета
    COLOR_PATTERNS = [
        r'цвет\s*[:\-–]?\s*([а-яёa-z\s,/]+?)(?:\.|,\s*[а-яёА-ЯЁ]|\n|$)',
    ]

    # Паттерны для страны
    COUNTRY_PATTERNS = [
        r'(?:страна|производство|сделано|made\s+in)\s*[:\-–]?\s*([а-яёa-z\s]+?)(?:\.|,|\n|$)',
    ]

    # Паттерны для типа питания
    POWER_PATTERNS = [
        r'((?:батарейк|элемент питания|аккумулятор|usb|зарядк|перезаряж)[а-яё]*(?:\s+[а-яёa-z0-9\s,]+)?)',
        r'питание\s*[:\-–]?\s*([а-яёa-z0-9\s,/]+?)(?:\.|;|\n|$)',
    ]

    @classmethod
    def enrich_from_description(cls, product_data: dict) -> dict:
        """
        Обогатить данные товара из описания.

        Заполняет только пустые поля — не перезаписывает существующие.

        Args:
            product_data: dict с данными товара (из парсера CSV)

        Returns:
            Обновлённый dict с дополненными данными
        """
        description = product_data.get('description', '')
        if not description or len(description) < 10:
            return product_data

        result = dict(product_data)
        enriched_fields = []

        # Размеры
        dimensions = cls._extract_dimensions(description)
        if dimensions and not result.get('dimensions'):
            result['dimensions'] = dimensions
            enriched_fields.append('dimensions')

        # Материалы
        if not result.get('materials') or result.get('materials') == []:
            materials = cls._extract_materials(description)
            if materials:
                result['materials'] = materials
                enriched_fields.append('materials')

        # Цвета
        if not result.get('colors') or result.get('colors') == []:
            colors = cls._extract_colors(description)
            if colors:
                result['colors'] = colors
                enriched_fields.append('colors')

        # Страна
        if not result.get('country'):
            country = cls._extract_country(description)
            if country:
                result['country'] = country
                enriched_fields.append('country')

        # Питание (сохраняем в характеристики)
        power = cls._extract_power_type(description)
        if power:
            chars = result.get('extracted_characteristics', {})
            if not chars.get('power_type'):
                chars['power_type'] = power
                result['extracted_characteristics'] = chars
                enriched_fields.append('power_type')

        if enriched_fields:
            logger.debug(
                f"Enriched product {result.get('external_id', '?')}: "
                f"{', '.join(enriched_fields)}"
            )

        return result

    @classmethod
    def _extract_dimensions(cls, text: str) -> Optional[Dict[str, str]]:
        """Извлечь размеры из текста."""
        text_lower = text.lower()
        dimensions = {}

        for pattern, key in cls.SIZE_PATTERNS:
            match = re.search(pattern, text_lower, re.IGNORECASE)
            if match:
                value = match.group(1).replace(',', '.')
                unit = match.group(2).lower()
                # Нормализуем в мм
                try:
                    num_val = float(value)
                    if unit in ('см', 'cm'):
                        num_val *= 10
                    elif unit in ('m',):
                        num_val *= 1000
                    dimensions[key] = f"{num_val:.1f} мм"
                except ValueError:
                    dimensions[key] = f"{value} {unit}"

        return dimensions if dimensions else None

    @classmethod
    def _extract_materials(cls, text: str) -> List[str]:
        """Извлечь материалы из текста."""
        text_lower = text.lower()
        materials = []

        for pattern in cls.MATERIAL_PATTERNS:
            match = re.search(pattern, text_lower, re.IGNORECASE)
            if match:
                raw = match.group(1).strip()
                # Разделяем по запятой и /
                parts = re.split(r'[,/]', raw)
                for part in parts:
                    part = part.strip()
                    if part and len(part) > 2 and len(part) < 50:
                        # Первая буква заглавная
                        materials.append(part[0].upper() + part[1:])
                break  # Берём только первое совпадение

        return materials

    @classmethod
    def _extract_colors(cls, text: str) -> List[str]:
        """Извлечь цвета из текста."""
        text_lower = text.lower()
        colors = []

        for pattern in cls.COLOR_PATTERNS:
            match = re.search(pattern, text_lower, re.IGNORECASE)
            if match:
                raw = match.group(1).strip()
                parts = re.split(r'[,/]', raw)
                for part in parts:
                    part = part.strip()
                    if part and len(part) > 2 and len(part) < 30:
                        colors.append(part)
                break

        return colors

    @classmethod
    def _extract_country(cls, text: str) -> Optional[str]:
        """Извлечь страну производства из текста."""
        text_lower = text.lower()

        for pattern in cls.COUNTRY_PATTERNS:
            match = re.search(pattern, text_lower, re.IGNORECASE)
            if match:
                country = match.group(1).strip()
                if len(country) > 2 and len(country) < 30:
                    return country[0].upper() + country[1:]

        # Прямые упоминания стран
        known_countries = {
            'китай': 'Китай', 'china': 'Китай',
            'россия': 'Россия', 'russia': 'Россия',
            'германия': 'Германия', 'germany': 'Германия',
            'сша': 'США', 'usa': 'США',
            'япония': 'Япония', 'japan': 'Япония',
            'южная корея': 'Южная Корея', 'south korea': 'Южная Корея',
            'канада': 'Канада', 'canada': 'Канада',
            'австралия': 'Австралия',
            'швеция': 'Швеция', 'sweden': 'Швеция',
        }
        for key, value in known_countries.items():
            if key in text_lower:
                return value

        return None

    @classmethod
    def _extract_power_type(cls, text: str) -> Optional[str]:
        """Извлечь тип питания из текста."""
        text_lower = text.lower()

        for pattern in cls.POWER_PATTERNS:
            match = re.search(pattern, text_lower, re.IGNORECASE)
            if match:
                raw = match.group(1).strip()
                if len(raw) > 3 and len(raw) < 100:
                    return raw[0].upper() + raw[1:]

        # Специфические паттерны
        if 'usb' in text_lower and ('заряд' in text_lower or 'recharg' in text_lower):
            return 'USB зарядка'
        if re.search(r'батарейк[а-я]*\s+[a-z]+\d*', text_lower):
            match = re.search(r'батарейк[а-я]*\s+([a-z]+\d*)', text_lower)
            if match:
                return f'Батарейки {match.group(1).upper()}'

        return None

    @classmethod
    def enrich_product_list(cls, products: List[dict]) -> List[dict]:
        """Обогатить список товаров из описаний."""
        enriched = []
        enriched_count = 0
        for product in products:
            original_keys = set(k for k, v in product.items() if v)
            result = cls.enrich_from_description(product)
            new_keys = set(k for k, v in result.items() if v) - original_keys
            if new_keys:
                enriched_count += 1
            enriched.append(result)

        logger.info(
            f"Description enrichment: {enriched_count}/{len(products)} "
            f"products enriched"
        )
        return enriched
