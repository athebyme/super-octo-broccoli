# -*- coding: utf-8 -*-
"""
SmartProductParser — умный парсинг товаров от поставщика.

Алгоритмический pipeline (без AI), выполняющий:
1. Brand Resolution — через BrandEngine (exact/fuzzy/alias → resolved_brand_id)
2. Category Enhancement — smart-маппинг на WB с учётом бренда и описания
3. Characteristics Extraction — извлечение размеров, материалов, цветов из title
4. WB-ready data preparation — формирование данных для импорта в маркетплейс
5. Quality Scoring — оценка готовности товара к импорту

Pipeline вызывается:
- При sync_from_csv (массово, для каждого товара)
- При ручном парсинге конкретных товаров через UI
- При smart-импорте к продавцу (обогащение перед копированием)
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# RESULT DATACLASS
# ============================================================================

@dataclass
class SmartParseResult:
    """Результат умного парсинга одного товара."""
    success: bool = True

    # Brand resolution
    brand_resolved: bool = False
    brand_id: Optional[int] = None
    brand_canonical: Optional[str] = None
    brand_confidence: float = 0.0
    brand_status: str = ''  # exact, confident, uncertain, unresolved

    # Category
    category_mapped: bool = False
    wb_subject_id: Optional[int] = None
    wb_subject_name: Optional[str] = None
    category_confidence: float = 0.0

    # Extracted characteristics
    extracted_colors: List[str] = field(default_factory=list)
    extracted_materials: List[str] = field(default_factory=list)
    extracted_gender: Optional[str] = None
    extracted_dimensions: Dict = field(default_factory=dict)
    extracted_sizes: List[str] = field(default_factory=list)

    # Quality
    readiness_score: float = 0.0  # 0-100, готовность к импорту
    missing_fields: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'success': self.success,
            'brand': {
                'resolved': self.brand_resolved,
                'brand_id': self.brand_id,
                'canonical': self.brand_canonical,
                'confidence': self.brand_confidence,
                'status': self.brand_status,
            },
            'category': {
                'mapped': self.category_mapped,
                'wb_subject_id': self.wb_subject_id,
                'wb_subject_name': self.wb_subject_name,
                'confidence': self.category_confidence,
            },
            'extracted': {
                'colors': self.extracted_colors,
                'materials': self.extracted_materials,
                'gender': self.extracted_gender,
                'dimensions': self.extracted_dimensions,
                'sizes': self.extracted_sizes,
            },
            'readiness_score': self.readiness_score,
            'missing_fields': self.missing_fields,
            'warnings': self.warnings,
        }


@dataclass
class BulkSmartParseResult:
    """Результат массового парсинга."""
    total: int = 0
    processed: int = 0
    brand_resolved_count: int = 0
    category_mapped_count: int = 0
    avg_readiness_score: float = 0.0
    errors: int = 0
    error_messages: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            'total': self.total,
            'processed': self.processed,
            'brand_resolved_count': self.brand_resolved_count,
            'category_mapped_count': self.category_mapped_count,
            'avg_readiness_score': round(self.avg_readiness_score, 1),
            'errors': self.errors,
            'error_messages': self.error_messages[:20],
            'duration_seconds': round(self.duration_seconds, 2),
        }


# ============================================================================
# EXTRACTION PATTERNS
# ============================================================================

# Размеры из title/description (длина, диаметр, объём и т.д.)
_DIM_PATTERNS = [
    # "длина 18 см", "длина: 18см", "дл. 18 см"
    (r'(?:длина|дл\.?)\s*[:=]?\s*(\d+[.,]?\d*)\s*(?:см|мм|cm|mm)', 'length_cm'),
    # "диаметр 3.5 см"
    (r'(?:диаметр|диам\.?|Ø)\s*[:=]?\s*(\d+[.,]?\d*)\s*(?:см|мм|cm|mm)', 'diameter_cm'),
    # "ширина 5 см"
    (r'(?:ширина|шир\.?)\s*[:=]?\s*(\d+[.,]?\d*)\s*(?:см|мм|cm|mm)', 'width_cm'),
    # "высота 10 см"
    (r'(?:высота|выс\.?)\s*[:=]?\s*(\d+[.,]?\d*)\s*(?:см|мм|cm|mm)', 'height_cm'),
    # "вес 250 г", "вес: 0.3 кг"
    (r'(?:вес|масса)\s*[:=]?\s*(\d+[.,]?\d*)\s*(?:г|гр|g)', 'weight_g'),
    (r'(?:вес|масса)\s*[:=]?\s*(\d+[.,]?\d*)\s*(?:кг|kg)', 'weight_kg'),
    # "объём 100 мл"
    (r'(?:объ[её]м|объ\.?)\s*[:=]?\s*(\d+[.,]?\d*)\s*(?:мл|ml)', 'volume_ml'),
    # "рабочая длина 15 см"
    (r'(?:рабочая\s+длина|раб\.?\s*дл\.?)\s*[:=]?\s*(\d+[.,]?\d*)\s*(?:см|мм)', 'working_length_cm'),
]
_DIM_COMPILED = [(re.compile(p, re.IGNORECASE), key) for p, key in _DIM_PATTERNS]

# Размеры одежды
_CLOTHING_SIZES = {
    'xxs', 'xs', 's', 'm', 'l', 'xl', 'xxl', 'xxxl', '2xl', '3xl', '4xl', '5xl',
    'one size', 'os', 'uni', 'универсальный',
}
_RU_SIZES = re.compile(r'\b(4[02468]|5[02468]|6[02468])\b')
_LETTER_SIZES = re.compile(r'\b(XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|5XL)\b', re.IGNORECASE)

# Пол из title
_GENDER_PATTERNS = [
    (re.compile(r'\b(?:для\s+(?:него|мужчин|парней)|мужской|мужск\.?|man|men|for\s+him)\b', re.IGNORECASE), 'Мужской'),
    (re.compile(r'\b(?:для\s+(?:неё|нее|женщин|девушек)|женский|женск\.?|woman|women|for\s+her)\b', re.IGNORECASE), 'Женский'),
    (re.compile(r'\b(?:унисекс|unisex|для\s+(?:пар|двоих))\b', re.IGNORECASE), 'Унисекс'),
]

# Материалы из title/description
_MATERIAL_PATTERNS = [
    re.compile(r'\b(силикон(?:овый|овая)?)\b', re.IGNORECASE),
    re.compile(r'\b(латекс(?:ный|ная)?)\b', re.IGNORECASE),
    re.compile(r'\b(ABS[\s-]?пластик)\b', re.IGNORECASE),
    re.compile(r'\b(TPE|TPR|ТПЕ|ТПР)\b', re.IGNORECASE),
    re.compile(r'\b(поликарбонат)\b', re.IGNORECASE),
    re.compile(r'\b(нержавеющая\s+сталь|металл(?:ический)?)\b', re.IGNORECASE),
    re.compile(r'\b(стекло|стеклянн(?:ый|ая))\b', re.IGNORECASE),
    re.compile(r'\b(кожа|кожан(?:ый|ая)|PU\s*кожа|эко[\s-]?кожа)\b', re.IGNORECASE),
    re.compile(r'\b(нейлон(?:овый|овая)?)\b', re.IGNORECASE),
    re.compile(r'\b(хлопок|хлопков(?:ый|ая)|cotton)\b', re.IGNORECASE),
    re.compile(r'\b(полиэстер|polyester)\b', re.IGNORECASE),
    re.compile(r'\b(спандекс|эластан|elastane)\b', re.IGNORECASE),
    re.compile(r'\b(кибер[\s-]?кожа|cyberskin)\b', re.IGNORECASE),
    re.compile(r'\b(water[\s-]?based|на\s+водной\s+основе)\b', re.IGNORECASE),
]

# Нормализация извлечённых материалов
_MATERIAL_CANONICAL = {
    'силикон': 'Силикон', 'силиконовый': 'Силикон', 'силиконовая': 'Силикон',
    'латекс': 'Латекс', 'латексный': 'Латекс', 'латексная': 'Латекс',
    'abs-пластик': 'ABS-пластик', 'abs пластик': 'ABS-пластик',
    'tpe': 'TPE', 'тпе': 'TPE', 'tpr': 'TPR', 'тпр': 'TPR',
    'поликарбонат': 'Поликарбонат',
    'нержавеющая сталь': 'Нержавеющая сталь', 'металл': 'Металл', 'металлический': 'Металл',
    'стекло': 'Стекло', 'стеклянный': 'Стекло', 'стеклянная': 'Стекло',
    'кожа': 'Кожа', 'кожаный': 'Кожа', 'кожаная': 'Кожа',
    'pu кожа': 'PU-кожа', 'эко-кожа': 'Эко-кожа', 'эко кожа': 'Эко-кожа',
    'нейлон': 'Нейлон', 'нейлоновый': 'Нейлон', 'нейлоновая': 'Нейлон',
    'хлопок': 'Хлопок', 'хлопковый': 'Хлопок', 'хлопковая': 'Хлопок', 'cotton': 'Хлопок',
    'полиэстер': 'Полиэстер', 'polyester': 'Полиэстер',
    'спандекс': 'Спандекс', 'эластан': 'Спандекс', 'elastane': 'Спандекс',
    'кибер-кожа': 'Кибер-кожа', 'кибер кожа': 'Кибер-кожа', 'cyberskin': 'Кибер-кожа',
}


# ============================================================================
# SMART PRODUCT PARSER
# ============================================================================

class SmartProductParser:
    """
    Умный парсер товаров — алгоритмическое обогащение данных.

    Pipeline:
    1. Brand Resolution (BrandEngine)
    2. Category Mapping (SmartCategoryMapper)
    3. Characteristics Extraction (regex-based)
    4. Readiness Scoring (заполненность полей для WB)

    Не требует AI — работает только на алгоритмах и справочниках.
    """

    def __init__(self, supplier_id: int = None, marketplace_id: int = None):
        self._supplier_id = supplier_id
        self._marketplace_id = marketplace_id
        self._brand_engine = None
        self._brand_engine_loaded = False

    def _get_brand_engine(self):
        """Lazy init BrandEngine."""
        if not self._brand_engine_loaded:
            try:
                from services.brand_engine import get_brand_engine
                self._brand_engine = get_brand_engine()
            except Exception as e:
                logger.debug(f"BrandEngine not available: {e}")
                self._brand_engine = None
            self._brand_engine_loaded = True
        return self._brand_engine

    # ------------------------------------------------------------------
    # Main API
    # ------------------------------------------------------------------

    def parse_product(self, product_data: dict,
                      existing_product=None) -> SmartParseResult:
        """
        Умный парсинг одного товара.

        Args:
            product_data: dict с полями (title, brand, category, description,
                          colors, materials, sizes_raw, gender, country, ...)
            existing_product: SupplierProduct из БД (для обновления, если есть)

        Returns:
            SmartParseResult
        """
        result = SmartParseResult()

        title = product_data.get('title', '')
        description = product_data.get('description', '')
        text_combined = f"{title} {description}".strip()

        # --- Step 1: Brand Resolution ---
        raw_brand = product_data.get('brand', '')
        self._resolve_brand(result, raw_brand, title)

        # --- Step 2: Extract characteristics from text ---
        self._extract_dimensions(result, text_combined)
        self._extract_materials(result, text_combined, product_data.get('materials', []))
        self._extract_gender(result, text_combined, product_data.get('gender', ''))
        self._extract_sizes(result, text_combined, product_data.get('sizes_raw', ''))
        self._extract_colors_from_title(result, title, product_data.get('colors', []))

        # --- Step 3: Category mapping ---
        self._map_category(result, product_data)

        # --- Step 4: Readiness scoring ---
        self._compute_readiness(result, product_data)

        return result

    def apply_to_supplier_product(self, product, result: SmartParseResult) -> None:
        """
        Применить результаты SmartParse к SupplierProduct в БД.

        Обновляет только те поля, которые были пустые или получили
        более качественные данные (higher confidence).
        """
        # Brand
        if result.brand_resolved and result.brand_id:
            product.resolved_brand_id = result.brand_id
            if result.brand_canonical and (
                not product.brand or result.brand_confidence >= 0.9
            ):
                product.brand = result.brand_canonical

        # Category
        if result.category_mapped and result.wb_subject_id:
            # Обновляем только если нет категории или новая уверенность выше
            current_conf = product.category_confidence or 0.0
            if not product.wb_subject_id or result.category_confidence > current_conf:
                product.wb_subject_id = result.wb_subject_id
                product.wb_subject_name = result.wb_subject_name
                product.wb_category_name = result.wb_subject_name
                product.category_confidence = result.category_confidence

        # Gender
        if result.extracted_gender and not product.gender:
            product.gender = result.extracted_gender

        # Dimensions
        if result.extracted_dimensions and not product.dimensions_json:
            product.dimensions_json = json.dumps(result.extracted_dimensions, ensure_ascii=False)

        # Colors — дополняем, не перезаписываем
        if result.extracted_colors:
            existing_colors = []
            if product.colors_json:
                try:
                    existing_colors = json.loads(product.colors_json)
                except Exception:
                    pass
            if not existing_colors:
                product.colors_json = json.dumps(result.extracted_colors, ensure_ascii=False)

        # Materials — дополняем
        if result.extracted_materials:
            existing_mats = []
            if product.materials_json:
                try:
                    existing_mats = json.loads(product.materials_json)
                except Exception:
                    pass
            if not existing_mats:
                product.materials_json = json.dumps(result.extracted_materials, ensure_ascii=False)

        # Sizes
        if result.extracted_sizes:
            existing_sizes = {}
            if product.sizes_json:
                try:
                    existing_sizes = json.loads(product.sizes_json)
                except Exception:
                    pass
            if not existing_sizes.get('simple_sizes'):
                existing_sizes['simple_sizes'] = result.extracted_sizes
                product.sizes_json = json.dumps(existing_sizes, ensure_ascii=False)

        # Parsing confidence — обновляем readiness score
        product.parsing_confidence = result.readiness_score / 100.0
        product.normalization_applied = True

    def parse_and_apply_single(self, product_id: int) -> dict:
        """
        Парсит один SupplierProduct по ID и применяет результаты.

        Returns:
            dict: {success, result, error}
        """
        from models import db, SupplierProduct

        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        product_data = product.get_all_data_for_parsing()
        result = self.parse_product(product_data, existing_product=product)
        self.apply_to_supplier_product(product, result)

        product.updated_at = datetime.utcnow()
        db.session.commit()

        return {
            'success': True,
            'result': result.to_dict(),
            'product_id': product_id,
        }

    def parse_and_apply_bulk(self, product_ids: List[int],
                             batch_size: int = 100) -> BulkSmartParseResult:
        """
        Массовый умный парсинг SupplierProduct.

        Args:
            product_ids: IDs товаров
            batch_size: размер батча для коммита

        Returns:
            BulkSmartParseResult
        """
        from models import db, SupplierProduct

        bulk_result = BulkSmartParseResult(total=len(product_ids))
        start_time = time.time()
        readiness_sum = 0.0

        for i, pid in enumerate(product_ids):
            try:
                product = SupplierProduct.query.get(pid)
                if not product:
                    bulk_result.errors += 1
                    bulk_result.error_messages.append(f"ID {pid}: не найден")
                    continue

                product_data = product.get_all_data_for_parsing()
                result = self.parse_product(product_data, existing_product=product)
                self.apply_to_supplier_product(product, result)
                product.updated_at = datetime.utcnow()

                bulk_result.processed += 1
                readiness_sum += result.readiness_score

                if result.brand_resolved:
                    bulk_result.brand_resolved_count += 1
                if result.category_mapped:
                    bulk_result.category_mapped_count += 1

                # Batch commit
                if (i + 1) % batch_size == 0:
                    db.session.flush()

            except Exception as e:
                bulk_result.errors += 1
                bulk_result.error_messages.append(f"ID {pid}: {str(e)[:100]}")
                if len(bulk_result.error_messages) > 50:
                    bulk_result.error_messages.append("...и другие ошибки")
                    break

        db.session.commit()
        bulk_result.duration_seconds = time.time() - start_time

        if bulk_result.processed > 0:
            bulk_result.avg_readiness_score = readiness_sum / bulk_result.processed

        logger.info(
            f"SmartParse bulk: {bulk_result.processed}/{bulk_result.total} "
            f"(brands={bulk_result.brand_resolved_count}, "
            f"cats={bulk_result.category_mapped_count}, "
            f"avg_score={bulk_result.avg_readiness_score:.1f}) "
            f"in {bulk_result.duration_seconds:.1f}s"
        )

        return bulk_result

    def smart_import_to_seller(self, seller_id: int,
                               supplier_product_ids: List[int]) -> dict:
        """
        Умный импорт товаров к продавцу с предварительным парсингом.

        1. Выполняет SmartParse для каждого товара
        2. Применяет результаты к SupplierProduct
        3. Импортирует к продавцу через SupplierService.import_to_seller
        4. Применяет обогащённые данные к ImportedProduct

        Returns:
            dict: {success, parse_result, import_result, error}
        """
        from services.supplier_service import SupplierService

        # Step 1: Smart parse all products
        parse_result = self.parse_and_apply_bulk(supplier_product_ids)

        # Step 2: Import to seller
        import_result = SupplierService.import_to_seller(seller_id, supplier_product_ids)

        # Step 3: Post-import enrichment — resolve brands on ImportedProducts
        if import_result.success and import_result.imported > 0:
            try:
                self._enrich_imported_products(seller_id, supplier_product_ids)
            except Exception as e:
                logger.warning(f"Post-import enrichment error: {e}")

        return {
            'success': import_result.success,
            'parse_result': parse_result.to_dict(),
            'import_result': {
                'imported': import_result.imported,
                'skipped': import_result.skipped,
                'errors': import_result.errors,
                'error_messages': import_result.error_messages[:10],
            },
        }

    # ------------------------------------------------------------------
    # Step 1: Brand Resolution
    # ------------------------------------------------------------------

    def _resolve_brand(self, result: SmartParseResult, raw_brand: str, title: str):
        """Resolve brand через BrandEngine."""
        engine = self._get_brand_engine()
        if not engine:
            if raw_brand:
                result.brand_canonical = raw_brand
                result.brand_status = 'unresolved'
            return

        # Пробуем raw_brand
        brand_to_resolve = raw_brand.strip() if raw_brand else ''

        # Если бренд пустой — пробуем извлечь из title
        if not brand_to_resolve:
            brand_to_resolve = self._extract_brand_from_title(title)

        if not brand_to_resolve:
            result.brand_status = 'unresolved'
            return

        try:
            resolution = engine.resolve(
                brand_to_resolve,
                marketplace_id=self._marketplace_id,
            )

            result.brand_status = resolution.status
            result.brand_confidence = resolution.confidence

            if resolution.status in ('exact', 'confident'):
                result.brand_resolved = True
                result.brand_id = resolution.brand_id
                result.brand_canonical = resolution.canonical_name
            elif resolution.status == 'uncertain' and resolution.suggestions:
                # Берём лучший suggestion если confidence > 0.7
                if resolution.confidence >= 0.7:
                    result.brand_resolved = True
                    result.brand_id = resolution.brand_id
                    result.brand_canonical = resolution.canonical_name
                else:
                    result.brand_canonical = raw_brand
                    result.warnings.append(
                        f"Бренд '{raw_brand}' не определён точно. "
                        f"Варианты: {', '.join(s.get('name', '') for s in resolution.suggestions[:3])}"
                    )
            else:
                result.brand_canonical = raw_brand
                if raw_brand:
                    result.warnings.append(f"Бренд '{raw_brand}' не найден в реестре")

        except Exception as e:
            logger.debug(f"Brand resolution error: {e}")
            result.brand_canonical = raw_brand
            result.brand_status = 'unresolved'

    def _extract_brand_from_title(self, title: str) -> str:
        """Пытается извлечь бренд из начала заголовка."""
        if not title:
            return ''

        # Паттерн: первое слово в title до первого пробела — часто бренд
        # Но только если оно начинается с заглавной и не является обычным словом
        _skip_words = {
            'набор', 'комплект', 'подарочный', 'интимный', 'эротический',
            'стимулятор', 'вибратор', 'массажёр', 'массажер', 'крем', 'гель',
            'смазка', 'лубрикант', 'презерватив', 'костюм', 'бельё', 'белье',
            'кольцо', 'насадка', 'помпа', 'анальная', 'вагинальный', 'мастурбатор',
            'мини', 'большой', 'маленький', 'новый', 'классический', 'секс',
        }

        words = title.split()
        if not words:
            return ''

        first = words[0].strip('",\'«»()[]')
        if first and first[0].isupper() and first.lower() not in _skip_words and len(first) >= 2:
            # Проверяем — это может быть бренд? (2-20 символов, не число)
            if 2 <= len(first) <= 20 and not first.isdigit():
                return first

        return ''

    # ------------------------------------------------------------------
    # Step 2: Extract characteristics
    # ------------------------------------------------------------------

    def _extract_dimensions(self, result: SmartParseResult, text: str):
        """Извлекает физические размеры из текста."""
        if not text:
            return

        dims = {}
        for pattern, key in _DIM_COMPILED:
            match = pattern.search(text)
            if match:
                try:
                    value = float(match.group(1).replace(',', '.'))
                    # Конвертация мм → см если нужно
                    unit_part = match.group(0).lower()
                    if key == 'weight_kg':
                        dims['weight_g'] = value * 1000
                    elif 'мм' in unit_part or 'mm' in unit_part:
                        dims[key] = round(value / 10, 1)
                    else:
                        dims[key] = value
                except (ValueError, IndexError):
                    continue

        if dims:
            result.extracted_dimensions = dims

    def _extract_materials(self, result: SmartParseResult, text: str,
                          existing_materials: list):
        """Извлекает материалы из текста."""
        if existing_materials:
            result.extracted_materials = existing_materials
            return

        if not text:
            return

        found = set()
        for pattern in _MATERIAL_PATTERNS:
            for match in pattern.finditer(text):
                raw = match.group(1).strip().lower()
                canonical = _MATERIAL_CANONICAL.get(raw, raw.capitalize())
                if canonical not in found:
                    found.add(canonical)

        if found:
            result.extracted_materials = list(found)

    def _extract_gender(self, result: SmartParseResult, text: str,
                       existing_gender: str):
        """Определяет пол из текста."""
        if existing_gender and existing_gender.strip():
            result.extracted_gender = existing_gender.strip()
            return

        if not text:
            return

        for pattern, gender in _GENDER_PATTERNS:
            if pattern.search(text):
                result.extracted_gender = gender
                return

    def _extract_sizes(self, result: SmartParseResult, text: str,
                      sizes_raw: str):
        """Извлекает размеры из текста."""
        sizes = set()

        # Из sizes_raw (от поставщика)
        if sizes_raw:
            # Буквенные размеры
            for m in _LETTER_SIZES.finditer(sizes_raw):
                sizes.add(m.group(1).upper())
            # Числовые размеры
            for m in _RU_SIZES.finditer(sizes_raw):
                sizes.add(m.group(1))

        # Из текста
        if text and not sizes:
            for m in _LETTER_SIZES.finditer(text):
                sizes.add(m.group(1).upper())
            # Числовые только если рядом слово "размер"
            size_context = re.search(
                r'(?:размер|size)\s*[:=]?\s*([\d,\s/\-]+)',
                text, re.IGNORECASE
            )
            if size_context:
                for m in _RU_SIZES.finditer(size_context.group(1)):
                    sizes.add(m.group(1))

        if sizes:
            result.extracted_sizes = sorted(sizes)

    def _extract_colors_from_title(self, result: SmartParseResult, title: str,
                                   existing_colors: list):
        """Извлекает цвета из title если в CSV их не было."""
        if existing_colors:
            result.extracted_colors = existing_colors
            return

        if not title:
            return

        try:
            from services.data_normalizer import COLOR_CANONICAL
        except ImportError:
            return

        found = []
        title_lower = title.lower()
        for key, canonical in COLOR_CANONICAL.items():
            if key in title_lower and canonical not in found:
                found.append(canonical)

        if found:
            result.extracted_colors = found[:5]  # Не более 5 цветов

    # ------------------------------------------------------------------
    # Step 3: Category mapping
    # ------------------------------------------------------------------

    def _map_category(self, result: SmartParseResult, product_data: dict):
        """Маппинг категории на WB через SmartCategoryMapper."""
        try:
            from services.smart_category_mapper import SmartCategoryMapper

            all_cats = product_data.get('all_categories', [])
            subj_id, subj_name, conf = SmartCategoryMapper.map_category(
                csv_category=product_data.get('category', ''),
                product_title=product_data.get('title', ''),
                brand=result.brand_canonical or product_data.get('brand', ''),
                description=product_data.get('description', ''),
                all_categories=all_cats,
                external_id=product_data.get('external_id', ''),
                source_type='sexoptovik',
            )

            if subj_id:
                result.category_mapped = True
                result.wb_subject_id = subj_id
                result.wb_subject_name = subj_name
                result.category_confidence = conf

        except Exception as e:
            logger.debug(f"Category mapping error: {e}")

    # ------------------------------------------------------------------
    # Step 4: Readiness scoring
    # ------------------------------------------------------------------

    def _compute_readiness(self, result: SmartParseResult, product_data: dict):
        """
        Оценка готовности товара к импорту на WB.

        Шкала 0-100:
        - Title (обязательно) — 15
        - Brand (обязательно) — 15
        - Category WB (обязательно) — 15
        - Photos (обязательно) — 10
        - Price (обязательно) — 10
        - Color — 5
        - Materials — 5
        - Gender — 5
        - Description — 5
        - Dimensions — 5
        - Barcode — 5
        - Sizes — 5
        """
        score = 0.0
        missing = []

        # Title — 15 points
        title = product_data.get('title', '')
        if title and len(title) >= 5:
            score += 15
        else:
            missing.append('title')

        # Brand — 15 points
        if result.brand_resolved:
            score += 15
        elif result.brand_canonical:
            score += 8  # Есть бренд, но не в реестре
        else:
            missing.append('brand')

        # Category — 15 points
        if result.category_mapped:
            score += 15 * min(result.category_confidence, 1.0)
        else:
            missing.append('wb_category')

        # Photos — 10 points
        photos = product_data.get('photo_urls', [])
        if isinstance(photos, str):
            try:
                photos = json.loads(photos)
            except Exception:
                photos = []
        if photos:
            score += min(10, len(photos) * 2.5)
        else:
            missing.append('photos')

        # Price — 10 points
        price = product_data.get('supplier_price') or product_data.get('price')
        if price and float(price) > 0:
            score += 10
        else:
            missing.append('price')

        # Color — 5 points
        colors = result.extracted_colors or product_data.get('colors', [])
        if colors:
            score += 5
        else:
            missing.append('color')

        # Materials — 5 points
        materials = result.extracted_materials or product_data.get('materials', [])
        if materials:
            score += 5
        else:
            missing.append('materials')

        # Gender — 5 points
        gender = result.extracted_gender or product_data.get('gender', '')
        if gender:
            score += 5
        else:
            missing.append('gender')

        # Description — 5 points
        description = product_data.get('description', '')
        if description and len(description) >= 20:
            score += 5
        else:
            missing.append('description')

        # Dimensions — 5 points
        if result.extracted_dimensions:
            score += 5
        else:
            missing.append('dimensions')

        # Barcode — 5 points
        barcodes = product_data.get('barcodes', [])
        barcode = product_data.get('barcode', '')
        if barcodes or barcode:
            score += 5
        else:
            missing.append('barcode')

        # Sizes — extra 5 points if applicable
        # (не все товары имеют размеры, потому бонусные)
        if result.extracted_sizes:
            # Не штрафуем за отсутствие, но бонусим за наличие
            # (Добавляем поверх 100 — потом clamp)
            pass

        result.readiness_score = min(score, 100.0)
        result.missing_fields = missing

    # ------------------------------------------------------------------
    # Post-import enrichment
    # ------------------------------------------------------------------

    def _enrich_imported_products(self, seller_id: int,
                                  supplier_product_ids: List[int]):
        """
        Обогащает ImportedProducts после импорта.
        Копирует resolved_brand_id и brand_status.
        """
        from models import db, ImportedProduct, SupplierProduct

        imported = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.supplier_product_id.in_(supplier_product_ids),
        ).all()

        for imp in imported:
            sp = imp.supplier_product
            if not sp:
                continue

            # Brand
            if sp.resolved_brand_id:
                imp.resolved_brand_id = sp.resolved_brand_id
                imp.brand_status = 'exact'
            elif sp.brand:
                imp.brand_status = 'unresolved'

        db.session.commit()


# ============================================================================
# CHARACTERISTICS VALIDATOR — валидация и коррекция AI-извлечённых данных
# ============================================================================

@dataclass
class CharacteristicValidation:
    """Результат валидации одной характеристики."""
    name: str
    original_value: str
    corrected_value: Optional[str] = None
    status: str = 'valid'  # valid, corrected, invalid, missing_required
    error: str = ''
    source: str = ''  # csv, ai, extracted


@dataclass
class CharacteristicsValidationResult:
    """Результат валидации всех характеристик товара."""
    is_valid: bool = True
    total_checked: int = 0
    valid_count: int = 0
    corrected_count: int = 0
    invalid_count: int = 0
    missing_required: List[str] = field(default_factory=list)
    validations: List[CharacteristicValidation] = field(default_factory=list)
    readiness_pct: float = 0.0  # % заполненных обязательных характеристик

    def to_dict(self) -> dict:
        return {
            'is_valid': self.is_valid,
            'total_checked': self.total_checked,
            'valid_count': self.valid_count,
            'corrected_count': self.corrected_count,
            'invalid_count': self.invalid_count,
            'missing_required': self.missing_required,
            'readiness_pct': round(self.readiness_pct, 1),
            'validations': [
                {
                    'name': v.name,
                    'original_value': v.original_value,
                    'corrected_value': v.corrected_value,
                    'status': v.status,
                    'error': v.error,
                }
                for v in self.validations
                if v.status != 'valid'  # Показываем только проблемные
            ],
        }


class CharacteristicsValidator:
    """
    Валидатор характеристик товара — проверяет и корректирует AI-извлечённые данные.

    Работает по принципу:
    1. Загружает допустимые характеристики для категории WB (MarketplaceCategoryCharacteristic)
    2. Проверяет каждое значение:
       - Если есть dictionary (справочник) — проверяет вхождение, ищет ближайшее
       - Если числовое — проверяет диапазон
       - Если строковое — проверяет длину
    3. Для обязательных полей без значения — помечает как missing
    4. Автокоррекция:
       - Fuzzy match по справочнику (SequenceMatcher)
       - Нормализация единиц измерения
       - Trim/clean значений

    Сценарии использования:
    - После AI парсинга — проверить что AI не нагалюцинировал
    - Перед импортом в WB — убедиться что все required заполнены корректно
    - На UI — показать продавцу что нужно поправить
    """

    # Fuzzy match threshold для справочных значений
    FUZZY_THRESHOLD = 0.82

    # Допустимые диапазоны для числовых характеристик (санитарная проверка)
    _NUMERIC_RANGES = {
        'Длина': (0.1, 500),       # см
        'Ширина': (0.1, 200),
        'Высота': (0.1, 300),
        'Диаметр': (0.1, 50),
        'Вес': (0.1, 50000),       # г
        'Объем': (0.1, 10000),     # мл
        'Рабочая длина': (0.1, 100),
    }

    @staticmethod
    def validate_product(product_id: int,
                         auto_correct: bool = True) -> CharacteristicsValidationResult:
        """
        Валидация характеристик SupplierProduct.

        Args:
            product_id: ID товара
            auto_correct: Автоматически корректировать ошибки

        Returns:
            CharacteristicsValidationResult
        """
        from models import (
            db, SupplierProduct, MarketplaceCategory,
            MarketplaceCategoryCharacteristic
        )

        result = CharacteristicsValidationResult()

        product = SupplierProduct.query.get(product_id)
        if not product:
            result.is_valid = False
            return result

        # Загружаем AI-данные
        ai_data = product.get_ai_parsed_data()
        mp_data = product.get_ai_marketplace_data()

        # Нужен subject_id для загрузки допустимых характеристик
        subject_id = product.wb_subject_id
        if not subject_id:
            result = CharacteristicsValidator._validate_basic_fields(
                product, ai_data, mp_data
            )
            return result

        # Загружаем характеристики категории
        category = MarketplaceCategory.query.filter_by(
            subject_id=subject_id
        ).first()
        if not category:
            result = CharacteristicsValidator._validate_basic_fields(
                product, ai_data, mp_data
            )
            return result

        charcs = MarketplaceCategoryCharacteristic.query.filter_by(
            category_id=category.id,
            is_enabled=True,
        ).all()

        if not charcs:
            result = CharacteristicsValidator._validate_basic_fields(
                product, ai_data, mp_data
            )
            return result

        # Собираем текущие значения характеристик
        current_values = mp_data.get('characteristics', {}) if mp_data else {}

        required_total = 0
        required_filled = 0
        corrections_to_apply = {}

        for charc in charcs:
            result.total_checked += 1
            val = current_values.get(charc.name, '')

            validation = CharacteristicValidation(
                name=charc.name,
                original_value=str(val) if val else '',
            )

            # Проверяем обязательные
            if charc.required:
                required_total += 1
                if not val:
                    validation.status = 'missing_required'
                    validation.error = 'Обязательное поле не заполнено'
                    result.missing_required.append(charc.name)
                    result.validations.append(validation)
                    continue
                else:
                    required_filled += 1

            if not val:
                result.valid_count += 1
                continue

            # Валидация по типу
            if charc.charc_type == 4:
                # Числовое значение
                validated = CharacteristicsValidator._validate_numeric(
                    charc.name, str(val), charc.unit_name
                )
                validation.status = validated['status']
                validation.error = validated.get('error', '')
                if validated.get('corrected'):
                    validation.corrected_value = validated['corrected']
                    if auto_correct:
                        corrections_to_apply[charc.name] = validated['corrected']
            else:
                # Строковое / справочное значение
                dictionary = CharacteristicsValidator._load_dictionary(charc)
                if dictionary:
                    validated = CharacteristicsValidator._validate_against_dictionary(
                        str(val), dictionary
                    )
                    validation.status = validated['status']
                    validation.error = validated.get('error', '')
                    if validated.get('corrected'):
                        validation.corrected_value = validated['corrected']
                        if auto_correct:
                            corrections_to_apply[charc.name] = validated['corrected']
                else:
                    if len(str(val)) > 500:
                        validation.status = 'invalid'
                        validation.error = f'Значение слишком длинное ({len(str(val))} символов)'
                    else:
                        validation.status = 'valid'

            if validation.status == 'valid':
                result.valid_count += 1
            elif validation.status == 'corrected':
                result.corrected_count += 1
            elif validation.status in ('invalid', 'missing_required'):
                result.invalid_count += 1

            result.validations.append(validation)

        # Readiness %
        if required_total > 0:
            result.readiness_pct = (required_filled / required_total) * 100
        else:
            result.readiness_pct = 100.0

        result.is_valid = (
            result.invalid_count == 0 and len(result.missing_required) == 0
        )

        # Применяем коррекции
        if auto_correct and corrections_to_apply and mp_data:
            chars = mp_data.get('characteristics', {})
            chars.update(corrections_to_apply)
            mp_data['characteristics'] = chars
            product.ai_marketplace_json = json.dumps(mp_data, ensure_ascii=False)
            db.session.commit()
            logger.info(
                f"CharacteristicsValidator: product {product_id} — "
                f"corrected {len(corrections_to_apply)} values"
            )

        return result

    @staticmethod
    def validate_bulk(product_ids: List[int],
                      auto_correct: bool = True) -> dict:
        """
        Массовая валидация характеристик.

        Returns:
            dict: {total, valid, with_errors, corrections_applied, details}
        """
        total = len(product_ids)
        valid = 0
        with_errors = 0
        corrections_applied = 0
        details = []

        for pid in product_ids:
            try:
                result = CharacteristicsValidator.validate_product(
                    pid, auto_correct=auto_correct
                )
                if result.is_valid:
                    valid += 1
                else:
                    with_errors += 1
                corrections_applied += result.corrected_count

                if not result.is_valid:
                    details.append({
                        'product_id': pid,
                        'missing_required': result.missing_required,
                        'invalid_count': result.invalid_count,
                        'corrected_count': result.corrected_count,
                    })

            except Exception as e:
                with_errors += 1
                details.append({
                    'product_id': pid,
                    'error': str(e)[:100],
                })

        return {
            'total': total,
            'valid': valid,
            'with_errors': with_errors,
            'corrections_applied': corrections_applied,
            'details': details[:50],
        }

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_basic_fields(product, ai_data: dict,
                               mp_data: dict) -> CharacteristicsValidationResult:
        """Базовая валидация без справочника категории."""
        result = CharacteristicsValidationResult()

        basic_checks = [
            ('title', product.title, 'Название'),
            ('brand', product.brand, 'Бренд'),
        ]

        for field_name, value, display_name in basic_checks:
            result.total_checked += 1
            v = CharacteristicValidation(
                name=display_name, original_value=str(value or '')
            )
            if not value or not str(value).strip():
                v.status = 'missing_required'
                v.error = 'Обязательное поле'
                result.missing_required.append(display_name)
                result.invalid_count += 1
            else:
                v.status = 'valid'
                result.valid_count += 1
            result.validations.append(v)

        # Числовые характеристики — проверка адекватности
        if mp_data and mp_data.get('dimensions'):
            dims = mp_data['dimensions']
            for dim_key in ('length', 'width', 'height'):
                val = dims.get(dim_key)
                if val is not None:
                    result.total_checked += 1
                    v = CharacteristicValidation(
                        name=dim_key, original_value=str(val)
                    )
                    try:
                        fval = float(val)
                        if fval <= 0 or fval > 500:
                            v.status = 'invalid'
                            v.error = f'Вне допустимого диапазона: {fval}'
                            result.invalid_count += 1
                        else:
                            v.status = 'valid'
                            result.valid_count += 1
                    except (ValueError, TypeError):
                        v.status = 'invalid'
                        v.error = f'Не числовое значение: {val}'
                        result.invalid_count += 1
                    result.validations.append(v)

        required_fields = 2
        filled = result.valid_count
        result.readiness_pct = (
            (filled / required_fields * 100) if required_fields else 100.0
        )
        result.is_valid = (
            result.invalid_count == 0 and len(result.missing_required) == 0
        )
        return result

    @staticmethod
    def _validate_numeric(name: str, value: str,
                         unit_name: str = None) -> dict:
        """Валидация числового значения."""
        try:
            clean = str(value).replace(',', '.').replace(' ', '').strip()
            clean = re.sub(
                r'(см|мм|г|кг|мл|л|шт)\.?$', '', clean, flags=re.IGNORECASE
            ).strip()
            fval = float(clean)
        except (ValueError, TypeError):
            return {
                'status': 'invalid',
                'error': f'Не удалось преобразовать в число: "{value}"',
            }

        range_tuple = CharacteristicsValidator._NUMERIC_RANGES.get(name)
        if range_tuple:
            min_val, max_val = range_tuple
            if fval < min_val:
                return {
                    'status': 'invalid',
                    'error': f'Значение {fval} меньше минимума {min_val}',
                }
            if fval > max_val:
                # Возможно AI дал в мм, а нужно см
                if unit_name and unit_name in ('см', 'cm') and fval > max_val:
                    corrected = round(fval / 10, 1)
                    if min_val <= corrected <= max_val:
                        return {
                            'status': 'corrected',
                            'corrected': str(corrected),
                            'error': (
                                f'Значение {fval} скорректировано '
                                f'(мм→см): {corrected}'
                            ),
                        }
                return {
                    'status': 'invalid',
                    'error': f'Значение {fval} больше максимума {max_val}',
                }

        return {'status': 'valid'}

    @staticmethod
    def _validate_against_dictionary(value: str,
                                     dictionary: List[str]) -> dict:
        """Валидация строкового значения по справочнику."""
        if not dictionary:
            return {'status': 'valid'}

        value_clean = value.strip()
        value_lower = value_clean.lower()

        # Exact match (case-insensitive)
        dict_lower_map = {d.lower(): d for d in dictionary}
        if value_lower in dict_lower_map:
            if value_clean != dict_lower_map[value_lower]:
                return {
                    'status': 'corrected',
                    'corrected': dict_lower_map[value_lower],
                    'error': (
                        f'Исправлен регистр: '
                        f'"{value_clean}" → "{dict_lower_map[value_lower]}"'
                    ),
                }
            return {'status': 'valid'}

        # Fuzzy match
        from difflib import SequenceMatcher
        best_match = None
        best_ratio = 0.0

        for dict_val in dictionary:
            ratio = SequenceMatcher(
                None, value_lower, dict_val.lower()
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = dict_val

        if best_ratio >= CharacteristicsValidator.FUZZY_THRESHOLD:
            return {
                'status': 'corrected',
                'corrected': best_match,
                'error': (
                    f'Автозамена: "{value_clean}" → "{best_match}" '
                    f'(совпадение {best_ratio:.0%})'
                ),
            }

        return {
            'status': 'invalid',
            'error': (
                f'Значение "{value_clean}" не найдено в справочнике. '
                f'Ближайшее: "{best_match}" ({best_ratio:.0%})'
                if best_match
                else f'Значение "{value_clean}" не найдено в справочнике'
            ),
        }

    @staticmethod
    def _load_dictionary(charc) -> Optional[List[str]]:
        """Загружает справочник допустимых значений."""
        if not charc.dictionary_json:
            return None
        try:
            raw = json.loads(charc.dictionary_json)
            if isinstance(raw, list):
                values = []
                for item in raw:
                    if isinstance(item, dict):
                        values.append(item.get('value', ''))
                    elif isinstance(item, str):
                        values.append(item)
                return [v for v in values if v]
        except Exception:
            pass
        return None
