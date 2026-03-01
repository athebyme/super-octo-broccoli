# -*- coding: utf-8 -*-
"""
Движок автокоррекции данных товаров.

Конфигурируемые правила для автоматического исправления типовых ошибок:
- Определение бренда из title если brand пустой
- Заполнение страны из бренда
- Очистка мусорных данных
- Автозаполнение пола по категории
"""
import re
import logging
from typing import Dict, List, Callable, Optional

logger = logging.getLogger(__name__)


class CorrectionRule:
    """Одно правило автокоррекции."""

    def __init__(
        self,
        name: str,
        condition: Callable[[dict], bool],
        action: Callable[[dict], dict],
        priority: int = 0,
        description: str = '',
    ):
        self.name = name
        self.condition = condition
        self.action = action
        self.priority = priority
        self.description = description

    def apply(self, data: dict) -> dict:
        if self.condition(data):
            return self.action(data)
        return data


class AutoCorrectionEngine:
    """
    Движок автокоррекции с конфигурируемыми правилами.

    Правила применяются в порядке приоритета (от высокого к низкому).
    Каждое правило имеет condition (когда применять) и action (что делать).
    """

    def __init__(self):
        self.rules: List[CorrectionRule] = []
        self._register_default_rules()

    def _register_default_rules(self):
        """Регистрация стандартных правил коррекции."""

        # --- Извлечение бренда из title ---
        self.add_rule(CorrectionRule(
            name='brand_from_title',
            description='Извлечь бренд из названия если brand пустой',
            priority=100,
            condition=lambda d: not d.get('brand') and bool(d.get('title')),
            action=self._extract_brand_from_title,
        ))

        # --- Страна по бренду ---
        self.add_rule(CorrectionRule(
            name='country_from_brand',
            description='Определить страну по бренду',
            priority=90,
            condition=lambda d: not d.get('country') and bool(d.get('brand')),
            action=self._set_country_from_brand,
        ))

        # --- Пол по категории ---
        self.add_rule(CorrectionRule(
            name='gender_from_category',
            description='Определить пол по категории',
            priority=80,
            condition=lambda d: not d.get('gender') and bool(d.get('category')),
            action=self._set_gender_from_category,
        ))

        # --- Очистка title от артикула поставщика ---
        self.add_rule(CorrectionRule(
            name='clean_title_vendor_code',
            description='Убрать артикул поставщика из начала title',
            priority=70,
            condition=lambda d: bool(d.get('title')) and bool(d.get('vendor_code')),
            action=self._clean_title_vendor_code,
        ))

        # --- Очистка мусорных значений ---
        self.add_rule(CorrectionRule(
            name='clean_garbage_values',
            description='Удалить мусорные значения (-, n/a, нет данных)',
            priority=60,
            condition=lambda d: True,
            action=self._clean_garbage_values,
        ))

    def add_rule(self, rule: CorrectionRule):
        """Добавить правило и пересортировать по приоритету."""
        self.rules.append(rule)
        self.rules.sort(key=lambda r: r.priority, reverse=True)

    def apply_all(self, data: dict) -> dict:
        """Применить все правила к данным товара."""
        result = dict(data)
        applied = []

        for rule in self.rules:
            try:
                new_result = rule.apply(result)
                if new_result is not result:
                    result = new_result
                    applied.append(rule.name)
            except Exception as e:
                logger.debug(f"Rule '{rule.name}' error: {e}")

        if applied:
            logger.debug(
                f"Auto-correction applied for {data.get('external_id', '?')}: "
                f"{', '.join(applied)}"
            )

        return result

    def apply_to_list(self, products: List[dict]) -> List[dict]:
        """Применить правила к списку товаров."""
        corrected = []
        corrections_count = 0

        for product in products:
            original = product
            result = self.apply_all(product)
            if result is not original:
                corrections_count += 1
            corrected.append(result)

        logger.info(
            f"Auto-correction: {corrections_count}/{len(products)} "
            f"products corrected"
        )
        return corrected

    # ------------------------------------------------------------------
    # Встроенные действия
    # ------------------------------------------------------------------

    # Бренды, которые можно извлечь из title
    KNOWN_BRANDS_IN_TITLE = [
        'LELO', 'Satisfyer', 'Womanizer', 'We-Vibe', 'Fun Factory',
        'Baile', 'TOYFA', 'Sexus', 'Bior Toys', 'Pipedream',
        'Doc Johnson', 'California Exotic', 'CalExotics', 'Evolved',
        'SVAKOM', 'Lovense', 'Je Joue', 'HOT', 'System JO',
        'Swiss Navy', 'Fantasy', 'Tenga', 'Fleshlight', 'Bad Dragon',
        'Fifty Shades', 'Durex', 'Contex',
    ]

    @classmethod
    def _extract_brand_from_title(cls, data: dict) -> dict:
        """Извлечь бренд из title."""
        title = data.get('title', '')
        if not title:
            return data

        title_lower = title.lower()
        for brand in cls.KNOWN_BRANDS_IN_TITLE:
            if brand.lower() in title_lower:
                result = dict(data)
                result['brand'] = brand
                return result

        return data

    # Маппинг бренд → страна
    BRAND_COUNTRY_MAP = {
        'lelo': 'Швеция',
        'satisfyer': 'Германия',
        'womanizer': 'Германия',
        'we-vibe': 'Канада',
        'fun factory': 'Германия',
        'baile': 'Китай',
        'toyfa': 'Россия',
        'sexus': 'Россия',
        'bior toys': 'Россия',
        'pipedream': 'США',
        'doc johnson': 'США',
        'california exotic': 'США',
        'calexotics': 'США',
        'evolved': 'США',
        'svakom': 'Китай',
        'lovense': 'Гонконг',
        'je joue': 'Великобритания',
        'hot': 'Австрия',
        'system jo': 'США',
        'swiss navy': 'США',
        'tenga': 'Япония',
        'fleshlight': 'США',
        'durex': 'Великобритания',
        'contex': 'Великобритания',
    }

    @classmethod
    def _set_country_from_brand(cls, data: dict) -> dict:
        """Определить страну по бренду."""
        brand = data.get('brand', '')
        if not brand:
            return data

        country = cls.BRAND_COUNTRY_MAP.get(brand.lower())
        if country:
            result = dict(data)
            result['country'] = country
            return result

        return data

    # Категории → пол
    FEMALE_CATEGORIES = {
        'вагинальные шарики', 'вагинальные тренажеры', 'вибротрусики',
        'клиторальные стимуляторы', 'вакуумно-волновые стимуляторы',
        'женские стимуляторы', 'бюстгальтеры', 'пеньюары',
    }

    MALE_CATEGORIES = {
        'мастурбаторы', 'мастурбаторы мужские', 'насадки на мастурбатор',
        'эрекционные кольца', 'массажеры простаты', 'насадки на член',
        'увеличители члена', 'фаллопротезы',
    }

    @classmethod
    def _set_gender_from_category(cls, data: dict) -> dict:
        """Определить пол по категории."""
        category = data.get('category', '')
        if not category:
            return data

        cat_lower = category.lower()
        result = dict(data)

        for female_cat in cls.FEMALE_CATEGORIES:
            if female_cat in cat_lower:
                result['gender'] = 'Женский'
                return result

        for male_cat in cls.MALE_CATEGORIES:
            if male_cat in cat_lower:
                result['gender'] = 'Мужской'
                return result

        return data

    @staticmethod
    def _clean_title_vendor_code(data: dict) -> dict:
        """Убрать дублирование артикула в начале title."""
        title = data.get('title', '')
        vendor_code = data.get('vendor_code', '')

        if not title or not vendor_code or len(vendor_code) < 3:
            return data

        # Если title начинается с vendor_code
        if title.lower().startswith(vendor_code.lower()):
            cleaned = title[len(vendor_code):].lstrip(' -_/|,;.')
            if cleaned and len(cleaned) > 5:
                result = dict(data)
                result['title'] = cleaned[0].upper() + cleaned[1:]
                return result

        return data

    # Мусорные значения, которые нужно очистить
    GARBAGE_VALUES = {
        '-', '--', 'n/a', 'na', 'н/д', 'нет', 'нет данных',
        'не указано', 'не указан', 'не определено', '0', 'null',
        'none', 'undefined', '.', '..', '...', '—',
    }

    @classmethod
    def _clean_garbage_values(cls, data: dict) -> dict:
        """Удалить мусорные значения из строковых полей."""
        fields_to_clean = (
            'brand', 'country', 'gender', 'description', 'category',
        )
        changed = False
        result = dict(data)

        for field in fields_to_clean:
            value = result.get(field)
            if isinstance(value, str) and value.strip().lower() in cls.GARBAGE_VALUES:
                result[field] = ''
                changed = True

        return result if changed else data


# Синглтон для использования в пайплайне
_default_engine = None


def get_default_engine() -> AutoCorrectionEngine:
    """Получить дефолтный движок автокоррекции."""
    global _default_engine
    if _default_engine is None:
        _default_engine = AutoCorrectionEngine()
    return _default_engine
