# -*- coding: utf-8 -*-
"""
Нормализация данных товаров от поставщиков.

Очистка, стандартизация и валидация данных на этапе парсинга CSV.
Работает до AI-обработки — чистые данные на входе = лучший результат на выходе.
"""
import html
import re
import unicodedata
import logging
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)


# ============================================================================
# СЛОВАРИ НОРМАЛИЗАЦИИ
# ============================================================================

# Каноническое написание брендов (lowercase → правильное)
BRAND_CANONICAL = {
    'lelo': 'LELO',
    'satisfyer': 'Satisfyer',
    'womanizer': 'Womanizer',
    'we-vibe': 'We-Vibe',
    'we vibe': 'We-Vibe',
    'fun factory': 'Fun Factory',
    'funfactory': 'Fun Factory',
    'baile': 'Baile',
    'toyfa': 'TOYFA',
    'sexus': 'Sexus',
    'bior toys': 'Bior Toys',
    'biortoys': 'Bior Toys',
    'fantasy': 'Fantasy',
    'pipedream': 'Pipedream',
    'doc johnson': 'Doc Johnson',
    'california exotic': 'California Exotic',
    'calexotics': 'CalExotics',
    'evolved': 'Evolved',
    'svakom': 'SVAKOM',
    'lovense': 'Lovense',
    'je joue': 'Je Joue',
    'hot': 'HOT',
    'system jo': 'System JO',
    'swiss navy': 'Swiss Navy',
}

# Нормализация цветов → каноническое название для WB
COLOR_CANONICAL = {
    # Чёрный
    'черный': 'черный',
    'чёрный': 'черный',
    'черн.': 'черный',
    'black': 'черный',
    'чёрн.': 'черный',
    # Белый
    'белый': 'белый',
    'white': 'белый',
    'бел.': 'белый',
    # Красный
    'красный': 'красный',
    'red': 'красный',
    'красн.': 'красный',
    # Розовый
    'розовый': 'розовый',
    'pink': 'розовый',
    'роз.': 'розовый',
    # Фиолетовый
    'фиолетовый': 'фиолетовый',
    'purple': 'фиолетовый',
    'фиол.': 'фиолетовый',
    # Синий
    'синий': 'синий',
    'blue': 'синий',
    'голубой': 'голубой',
    # Зелёный
    'зеленый': 'зеленый',
    'зелёный': 'зеленый',
    'green': 'зеленый',
    # Жёлтый
    'желтый': 'желтый',
    'жёлтый': 'желтый',
    'yellow': 'желтый',
    # Бежевый
    'бежевый': 'бежевый',
    'беж.': 'бежевый',
    'beige': 'бежевый',
    'телесный': 'бежевый',
    # Серый
    'серый': 'серый',
    'grey': 'серый',
    'gray': 'серый',
    # Прозрачный
    'прозрачный': 'прозрачный',
    'transparent': 'прозрачный',
    'прозр.': 'прозрачный',
    # Золотой
    'золотой': 'золотой',
    'gold': 'золотой',
    'золот.': 'золотой',
    # Серебряный
    'серебряный': 'серебряный',
    'silver': 'серебряный',
    'серебр.': 'серебряный',
    # Коричневый
    'коричневый': 'коричневый',
    'brown': 'коричневый',
    'коричн.': 'коричневый',
    # Оранжевый
    'оранжевый': 'оранжевый',
    'orange': 'оранжевый',
    # Мультиколор
    'мультиколор': 'мультиколор',
    'multicolor': 'мультиколор',
    'разноцветный': 'мультиколор',
}


class DataNormalizer:
    """
    Нормализация данных товаров из CSV поставщика.

    Применяется ДО сохранения в БД:
    - Очистка строк (пробелы, unicode, HTML)
    - Нормализация брендов к каноническому написанию
    - Нормализация цветов к словарю WB
    - Валидация и нормализация баркодов (EAN-13/EAN-8)
    - Нормализация заголовков
    """

    @staticmethod
    def normalize_product(data: dict) -> dict:
        """
        Нормализовать все поля товара.
        Принимает и возвращает dict из парсера CSV.
        """
        result = dict(data)

        # Строковые поля — базовая очистка
        for field in ('external_id', 'vendor_code', 'title', 'description',
                      'category', 'brand', 'country', 'gender'):
            if field in result and isinstance(result[field], str):
                result[field] = DataNormalizer.clean_string(result[field])

        # Title — расширенная очистка
        if result.get('title'):
            result['title'] = DataNormalizer.normalize_title(
                result['title'], result.get('vendor_code', '')
            )

        # Brand — каноническое написание
        if result.get('brand'):
            result['brand'] = DataNormalizer.normalize_brand(result['brand'])

        # Цвета
        if result.get('colors') and isinstance(result['colors'], list):
            result['colors'] = DataNormalizer.normalize_colors(result['colors'])

        # Баркоды
        if result.get('barcodes') and isinstance(result['barcodes'], list):
            result['barcodes'] = DataNormalizer.normalize_barcodes(result['barcodes'])

        # Материалы
        if result.get('materials') and isinstance(result['materials'], list):
            result['materials'] = DataNormalizer.normalize_materials(result['materials'])

        # Категории
        if result.get('all_categories') and isinstance(result['all_categories'], list):
            result['all_categories'] = [
                DataNormalizer.clean_string(c)
                for c in result['all_categories'] if c and c.strip()
            ]
        if result.get('category'):
            result['category'] = DataNormalizer.clean_string(result['category'])

        return result

    # ------------------------------------------------------------------
    # Базовая очистка строк
    # ------------------------------------------------------------------

    @staticmethod
    def clean_string(s: str) -> str:
        """Базовая очистка строки: unicode, HTML, пробелы."""
        if not s:
            return ''

        # Нормализация unicode (NFC — каноническая декомпозиция + композиция)
        s = unicodedata.normalize('NFC', s)

        # Декодирование HTML entities (&amp; → &, &#39; → ')
        s = html.unescape(s)

        # Убираем непечатаемые символы (кроме пробелов и переносов)
        s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)

        # Замена non-breaking space и прочих пробелов на обычный пробел
        s = re.sub(r'[\xa0\u2000-\u200b\u202f\u205f\u3000]', ' ', s)

        # Убираем двойные+ пробелы
        s = re.sub(r' {2,}', ' ', s)

        return s.strip()

    # ------------------------------------------------------------------
    # Title
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_title(title: str, vendor_code: str = '') -> str:
        """
        Нормализация заголовка товара:
        - Убрать дублирование артикула в начале/конце
        - Убрать двойные кавычки вокруг названия
        - Первая буква — верхний регистр
        """
        if not title:
            return ''

        # Убрать дублирование артикула в названии
        if vendor_code and len(vendor_code) > 2:
            # Убираем артикул из начала: "АРТ-123 Название" → "Название"
            pattern_start = re.compile(
                r'^' + re.escape(vendor_code) + r'[\s\-_/|,;.]+', re.IGNORECASE
            )
            title = pattern_start.sub('', title)
            # Убираем из конца: "Название АРТ-123" → "Название"
            pattern_end = re.compile(
                r'[\s\-_/|,;.]+' + re.escape(vendor_code) + r'$', re.IGNORECASE
            )
            title = pattern_end.sub('', title)

        # Убрать обрамляющие кавычки
        if len(title) > 2:
            for q_open, q_close in [('"', '"'), ('«', '»'), ('"', '"'), ("'", "'")]:
                if title.startswith(q_open) and title.endswith(q_close):
                    title = title[1:-1].strip()
                    break

        # Первая буква — заглавная
        if title and title[0].islower():
            title = title[0].upper() + title[1:]

        return title.strip()

    # ------------------------------------------------------------------
    # Brand
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_brand(brand: str) -> str:
        """Нормализация бренда к каноническому написанию."""
        if not brand:
            return ''

        brand = brand.strip()
        canonical = BRAND_CANONICAL.get(brand.lower())
        if canonical:
            return canonical

        # Если не нашли в словаре — оставляем как есть, но чистим
        return brand

    # ------------------------------------------------------------------
    # Colors
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_colors(colors: List[str]) -> List[str]:
        """
        Нормализация списка цветов:
        - Маппинг к каноническим названиям (для WB)
        - Удаление дубликатов
        """
        if not colors:
            return []

        normalized = []
        seen = set()
        for color in colors:
            color = color.strip()
            if not color:
                continue
            canonical = COLOR_CANONICAL.get(color.lower(), color)
            if canonical.lower() not in seen:
                seen.add(canonical.lower())
                normalized.append(canonical)

        return normalized

    # ------------------------------------------------------------------
    # Barcodes
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_barcodes(barcodes: List[str]) -> List[str]:
        """
        Нормализация и валидация баркодов:
        - Убираем нечисловые символы
        - Валидируем EAN-13/EAN-8 контрольную цифру
        - Отбрасываем невалидные
        """
        if not barcodes:
            return []

        valid = []
        for barcode in barcodes:
            normalized = DataNormalizer.normalize_single_barcode(barcode)
            if normalized:
                valid.append(normalized)

        return valid

    @staticmethod
    def normalize_single_barcode(barcode: str) -> Optional[str]:
        """Нормализовать и валидировать один баркод."""
        if not barcode:
            return None

        # Убираем всё кроме цифр
        digits = re.sub(r'\D', '', barcode.strip())

        if not digits:
            return None

        # EAN-13 (13 цифр) или EAN-8 (8 цифр)
        if len(digits) == 13:
            if DataNormalizer._validate_ean13(digits):
                return digits
            logger.debug(f"Invalid EAN-13 checksum: {digits}")
            return digits  # Возвращаем всё равно — поставщик мог ошибиться, но данные ценны
        elif len(digits) == 8:
            if DataNormalizer._validate_ean8(digits):
                return digits
            logger.debug(f"Invalid EAN-8 checksum: {digits}")
            return digits
        elif len(digits) == 12:
            # UPC-A — тоже валидный формат
            return digits
        elif 7 <= len(digits) <= 14:
            # Нестандартная длина но похоже на баркод
            return digits

        # Слишком короткий или длинный — скорее мусор
        logger.debug(f"Barcode rejected (len={len(digits)}): {barcode}")
        return None

    @staticmethod
    def _validate_ean13(digits: str) -> bool:
        """Валидация контрольной цифры EAN-13."""
        if len(digits) != 13:
            return False
        total = 0
        for i, d in enumerate(digits[:12]):
            weight = 1 if i % 2 == 0 else 3
            total += int(d) * weight
        check = (10 - (total % 10)) % 10
        return check == int(digits[12])

    @staticmethod
    def _validate_ean8(digits: str) -> bool:
        """Валидация контрольной цифры EAN-8."""
        if len(digits) != 8:
            return False
        total = 0
        for i, d in enumerate(digits[:7]):
            weight = 3 if i % 2 == 0 else 1
            total += int(d) * weight
        check = (10 - (total % 10)) % 10
        return check == int(digits[7])

    # ------------------------------------------------------------------
    # Materials
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_materials(materials: List[str]) -> List[str]:
        """
        Нормализация материалов:
        - Очистка строк
        - Удаление дублей
        - Первая буква — заглавная
        """
        if not materials:
            return []

        normalized = []
        seen = set()
        for mat in materials:
            mat = DataNormalizer.clean_string(mat)
            if not mat:
                continue
            # Первая буква заглавная
            mat = mat[0].upper() + mat[1:] if mat else mat
            if mat.lower() not in seen:
                seen.add(mat.lower())
                normalized.append(mat)

        return normalized

    # ------------------------------------------------------------------
    # Batch-нормализация
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_product_list(products: List[dict]) -> List[dict]:
        """Нормализовать список товаров (результат парсинга CSV)."""
        normalized = []
        stats = {'total': len(products), 'normalized': 0, 'errors': 0}

        for product in products:
            try:
                normalized.append(DataNormalizer.normalize_product(product))
                stats['normalized'] += 1
            except Exception as e:
                logger.error(
                    f"Normalization error for product "
                    f"{product.get('external_id', '?')}: {e}"
                )
                normalized.append(product)  # Оставляем ненормализованным
                stats['errors'] += 1

        logger.info(
            f"Normalization complete: {stats['normalized']}/{stats['total']} ok, "
            f"{stats['errors']} errors"
        )
        return normalized
