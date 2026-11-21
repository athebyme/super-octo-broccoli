# -*- coding: utf-8 -*-
"""
Сервисный слой для работы с базой товаров поставщиков

Предоставляет бизнес-логику для:
- CRUD поставщиков
- Синхронизация каталога из CSV
- Импорт товаров к продавцу
- Обновление товаров продавца из базы поставщика
- AI-валидация и обогащение на уровне поставщика
"""
import csv
import codecs
import copy
import json
import hashlib
import hmac
import logging
import math
import os
import re
import time
import threading
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import requests
from sqlalchemy import text
from sqlalchemy.orm import joinedload, selectinload

from models import (
    db, Supplier, SupplierProduct, SellerSupplier,
    ImportedProduct, Seller, CategoryMapping,
    Notification, log_admin_action
)
from services.pricing_engine import extract_supplier_product_id
from services.smart_product_parser import (
    SmartProductParser,
    advance_supplier_content_revisions,
    supplier_material_input_fingerprint,
    supplier_product_material_fingerprint,
)

logger = logging.getLogger(__name__)

# Символы, запрещённые WB в описаниях и заголовках
_WB_FORBIDDEN_CHARS = re.compile(r'[™®©℠℗⁺]')


def _sanitize_wb_text(text: str) -> str:
    """Удаляет запрещённые WB символы (™, ®, © и т.д.) из текста."""
    if not text:
        return text
    return _WB_FORBIDDEN_CHARS.sub('', text).strip()


def _commit_with_retry(session, max_retries: int = 5, base_delay: float = 0.3):
    """Коммит с retry для SQLite (database is locked).

    При 'locked'/'busy' ошибках — rollback и повтор (данные остаются в сессии).
    При невосстановимой ошибке сессии (prepared state) — remove() и raise
    (данные потеряны, но сессия очищена для следующих операций).
    """
    for attempt in range(1, max_retries + 1):
        try:
            session.commit()
            return
        except Exception as e:
            err_str = str(e).lower()
            if 'locked' in err_str or 'busy' in err_str:
                if attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(f"SQLite locked, retry {attempt}/{max_retries} in {delay:.1f}s")
                    try:
                        session.rollback()
                    except Exception:
                        # Сессия сломана — remove() и прекращаем ретраи (данные потеряны)
                        session.remove()
                        raise
                    time.sleep(delay)
                    continue
            # Не-locked ошибка или исчерпаны ретраи — cleanup и raise
            try:
                session.rollback()
            except Exception:
                session.remove()
            raise


def _get_marketplace_categories_block(supplier_id: int) -> str:
    """
    Получить текстовый блок включённых категорий маркетплейса для AI промпта.
    Если у поставщика нет активных подключений к маркетплейсам — возвращает ''.
    """
    try:
        from models import MarketplaceConnection
        from services.marketplace_service import MarketplaceService

        conn = MarketplaceConnection.query.filter_by(
            supplier_id=supplier_id,
            is_active=True
        ).first()
        if not conn:
            return ""

        return MarketplaceService.get_enabled_categories_for_prompt(conn.marketplace_id)
    except Exception as e:
        logger.debug(f"Could not load marketplace categories for supplier {supplier_id}: {e}")
        return ""


# ============================================================================
# RESULT DATACLASSES
# ============================================================================

@dataclass
class SyncResult:
    """Результат синхронизации каталога"""
    success: bool = True
    total_in_csv: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    error_messages: list = field(default_factory=list)
    duration_seconds: float = 0.0
    seller_rows_updated: int = 0
    busy: bool = False
    conflicting_source_rows: int = 0
    catalog_error_code: Optional[str] = None
    price_followup_status: Optional[str] = None
    price_followup_conflicts: Optional[int] = None


def _supplier_catalog_config_snapshot(supplier) -> dict:
    """Return the canonical, effective config used by catalog and price reads."""
    created_at = getattr(supplier, 'created_at', None)
    return {
        'supplier_id': getattr(supplier, 'id', None),
        'created_at': created_at.isoformat() if isinstance(created_at, datetime) else None,
        'code': getattr(supplier, 'code', None),
        'csv_source_url': getattr(supplier, 'csv_source_url', None),
        'csv_delimiter': getattr(supplier, 'csv_delimiter', None) or ';',
        'csv_encoding': getattr(supplier, 'csv_encoding', None) or 'cp1251',
        'csv_column_mapping': copy.deepcopy(
            getattr(supplier, 'csv_column_mapping', None),
        ),
        'csv_has_header': bool(getattr(supplier, 'csv_has_header', False)),
        'is_active': bool(getattr(supplier, 'is_active', False)),
        'price_file_url': getattr(supplier, 'price_file_url', None),
        'price_file_encoding': (
            getattr(supplier, 'price_file_encoding', None) or 'cp1251'
        ),
        'price_file_delimiter': (
            getattr(supplier, 'price_file_delimiter', None) or ';'
        ),
    }


def supplier_catalog_config_digest(supplier) -> str:
    """Hash the effective supplier source config without retaining its values."""
    canonical = json.dumps(
        _supplier_catalog_config_snapshot(supplier),
        sort_keys=True,
        ensure_ascii=False,
        separators=(',', ':'),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


@dataclass
class ImportResult:
    """Результат импорта товаров к продавцу"""
    success: bool = True
    total_requested: int = 0
    imported: int = 0
    skipped: int = 0
    errors: int = 0
    error_messages: list = field(default_factory=list)
    imported_product_ids: list = field(default_factory=list)
    item_results: list = field(default_factory=list)
    marketplace_drafts_created: int = 0
    marketplace_drafts_existing: int = 0
    marketplace_draft_errors: int = 0


@dataclass
class BulkActionResult:
    """Результат массовой операции"""
    success: bool = True
    total: int = 0
    processed: int = 0
    errors: int = 0
    error_messages: list = field(default_factory=list)


class SupplierRefreshReviewStale(ValueError):
    """The selected supplier refresh no longer matches its reviewed state."""


SUPPLIER_REFRESH_REVIEW_PRODUCT_FIELDS = (
    "title", "description", "photo_urls", "characteristics",
    "content_overrides_json", "category", "all_categories",
    "mapped_wb_category", "wb_subject_id", "category_confidence",
    "brand", "resolved_brand_id", "brand_status", "country", "gender",
    "colors", "sizes", "materials", "supplier_price", "supplier_quantity",
    "recommended_retail_price", "barcodes", "original_data",
    "processed_photos", "ai_attributes", "ai_seo_title", "ai_keywords",
    "ai_bullets", "supplier_content_revision",
)
SUPPLIER_REFRESH_REVIEW_SOURCE_FIELDS = (
    "id", "supplier_id", "external_id", "created_at", "updated_at",
    "content_revision",
    "title", "description", "description_source", "ai_description",
    "brand", "resolved_brand_id", "category", "all_categories",
    "wb_category_name", "wb_subject_id", "category_confidence", "country",
    "gender", "colors_json", "sizes_json", "materials_json",
    "characteristics_json", "ai_marketplace_json", "supplier_price",
    "supplier_quantity", "recommended_retail_price", "barcodes_json",
    "barcode", "photo_urls_json", "processed_photos_json",
    "dimensions_json", "original_data_json", "ai_seo_title",
    "ai_keywords_json", "ai_bullets_json",
)


def _supplier_refresh_review_scalar(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return f"nonfinite:{value}"
    return value


def _supplier_refresh_review_values(instance, fields):
    return {
        field: _supplier_refresh_review_scalar(getattr(instance, field, None))
        for field in fields
    }


def _supplier_refresh_digest(payload):
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _supplier_catalog_refresh_review_hasher(context):
    """Start a streamed digest for a supplier-scoped refresh review."""
    canonical = json.dumps(
        context,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    hasher = hashlib.sha256()
    hasher.update(b"supplier-catalog-refresh-review-v1\0")
    hasher.update(len(canonical).to_bytes(8, "big"))
    hasher.update(canonical)
    return hasher


def _supplier_catalog_refresh_review_add_row(hasher, row_id, row_digest):
    """Add one complete row-review digest without retaining all rows."""
    canonical = json.dumps(
        [row_id, row_digest],
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    hasher.update(len(canonical).to_bytes(8, "big"))
    hasher.update(canonical)


def _supplier_catalog_refresh_review_finish(hasher, row_count):
    hasher.update(b"\0rows\0")
    hasher.update(int(row_count).to_bytes(8, "big"))
    return hasher.hexdigest()


def _supplier_refresh_source_matches(imported_product):
    source = getattr(imported_product, "supplier_product", None)
    return bool(
        source is not None
        and source.id == imported_product.supplier_product_id
        and imported_product.supplier_id is not None
        and source.supplier_id == imported_product.supplier_id
    )


def supplier_refresh_product_label(imported_product):
    title = imported_product.title.strip() if isinstance(imported_product.title, str) else ""
    vendor_code = imported_product.external_vendor_code or imported_product.external_id or ""
    vendor_code = vendor_code.strip() if isinstance(vendor_code, str) else ""
    if title and vendor_code and title != vendor_code:
        label = f"{title} · {vendor_code}"
    else:
        label = title or vendor_code or "Товар из выбранного списка"
    return label[:2_048]


def supplier_refresh_preview_label(imported_product):
    return supplier_refresh_product_label(imported_product)[:256]


def _supplier_refresh_clone(imported_product):
    """Copy only supplier-mutated values into an unattached projection row."""
    clone = ImportedProduct()
    fields = set(SUPPLIER_REFRESH_REVIEW_PRODUCT_FIELDS) | {
        "seller_id", "supplier_id", "supplier_product_id", "external_id",
        "source_type", "content_edit_version",
    }
    for field in fields:
        setattr(clone, field, copy.deepcopy(getattr(imported_product, field, None)))
    # The common-content writer guard applies to persisted rows. This clone is
    # deliberately transient, so projection cannot register or write a row.
    clone.id = None
    return clone


def _supplier_refresh_common_value(imported_product, field):
    columns = {
        "title": "title",
        "description": "description",
        "photos": "photo_urls",
        "characteristics": "characteristics",
    }
    value = getattr(imported_product, columns[field], None)
    if field in {"photos", "characteristics"} and isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, RecursionError):
            return value
    return value


def _supplier_refresh_safe_text(value, *, limit=96):
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list, tuple)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Decimal):
        value = format(value, "f")
    elif isinstance(value, float):
        value = format(value, ".15g")
    elif not isinstance(value, str):
        value = str(value)
    value = " ".join(value.split())
    return value[:limit] if value else None


def _supplier_refresh_preview_changes(imported_product, projected):
    """Return value fields plus null/null group markers; never expose raw JSON."""
    changes = []

    def add_value(field, before, after, *, limit=96):
        if before != after:
            changes.append({
                "field": field,
                "before": _supplier_refresh_safe_text(before, limit=limit),
                "after": _supplier_refresh_safe_text(after, limit=limit),
            })

    def add_group(field, before, after):
        if before != after:
            changes.append({"field": field, "before": None, "after": None})

    add_value(
        "title",
        _supplier_refresh_common_value(imported_product, "title"),
        _supplier_refresh_common_value(projected, "title"),
        limit=120,
    )
    add_group(
        "description",
        _supplier_refresh_common_value(imported_product, "description"),
        _supplier_refresh_common_value(projected, "description"),
    )
    add_group(
        "photos",
        (
            _supplier_refresh_common_value(imported_product, "photos"),
            imported_product.processed_photos,
        ),
        (
            _supplier_refresh_common_value(projected, "photos"),
            projected.processed_photos,
        ),
    )
    add_group(
        "characteristics",
        _supplier_refresh_common_value(imported_product, "characteristics"),
        _supplier_refresh_common_value(projected, "characteristics"),
    )
    add_group(
        "category",
        (
            imported_product.category, imported_product.all_categories,
            imported_product.mapped_wb_category, imported_product.wb_subject_id,
            imported_product.category_confidence,
        ),
        (
            projected.category, projected.all_categories,
            projected.mapped_wb_category, projected.wb_subject_id,
            projected.category_confidence,
        ),
    )
    add_value(
        "purchase_price",
        imported_product.supplier_price,
        projected.supplier_price,
        limit=32,
    )
    add_value(
        "supplier_quantity",
        imported_product.supplier_quantity,
        projected.supplier_quantity,
        limit=32,
    )

    detail_fields = (
        "brand", "resolved_brand_id", "brand_status", "country", "gender",
        "colors", "sizes", "materials", "barcodes", "recommended_retail_price",
        "ai_attributes", "ai_seo_title", "ai_keywords", "ai_bullets",
    )
    add_group(
        "product_details",
        _supplier_refresh_review_values(imported_product, detail_fields),
        _supplier_refresh_review_values(projected, detail_fields),
    )
    add_group(
        "source_snapshot",
        (
            imported_product.original_data,
            imported_product.content_overrides_json,
            imported_product.supplier_content_revision,
        ),
        (
            projected.original_data,
            projected.content_overrides_json,
            projected.supplier_content_revision,
        ),
    )

    if not changes and (
        _supplier_refresh_review_values(
            imported_product, SUPPLIER_REFRESH_REVIEW_PRODUCT_FIELDS,
        )
        != _supplier_refresh_review_values(
            projected, SUPPLIER_REFRESH_REVIEW_PRODUCT_FIELDS,
        )
    ):
        changes.append({"field": "product_details", "before": None, "after": None})
    return changes


# ============================================================================
# CSV PARSER (для supplier_products)
# ============================================================================

class SupplierCSVParser:
    """
    Парсер CSV для различных поставщиков.

    Поддерживает:
    - Конфигурируемый маппинг колонок (через csv_column_mapping в Supplier)
    - Legacy-формат sexoptovik (hardcoded, для обратной совместимости)
    - Generic парсинг (заголовки в первой строке)
    - Автоматическую нормализацию данных через DataNormalizer
    - Предвалидацию CSV через CSVPreValidator
    """

    def __init__(self, supplier: Supplier):
        self.supplier = supplier
        self.delimiter = supplier.csv_delimiter or ';'
        self.encoding = supplier.csv_encoding or 'cp1251'

    def fetch_csv(self) -> Optional[str]:
        """Скачать и строго декодировать CSV по URL."""
        raw_bytes = self.fetch_csv_raw()
        if raw_bytes is None:
            return None
        try:
            return raw_bytes.decode(self.encoding, errors='strict')
        except (LookupError, UnicodeDecodeError):
            logger.error("Invalid catalog CSV encoding for supplier %s", self.supplier.code)
            return None

    def fetch_csv_raw(self) -> Optional[bytes]:
        """Получить ограниченный публичный CSV через общий pinned TLS transport."""
        if not self.supplier.csv_source_url:
            logger.error(f"Supplier {self.supplier.code}: CSV URL не задан")
            return None
        try:
            from services.supplier_price_feed import read_supplier_csv_bytes

            return read_supplier_csv_bytes(self.supplier.csv_source_url)
        except Exception as e:
            # Transport exceptions carry a short enum code, never the URL/body.
            logger.error(
                "Catalog CSV read failed for supplier %s (%s)",
                self.supplier.code,
                getattr(e, 'code', 'network_error'),
            )
            return None

    def preflight_raw(self, raw_bytes: bytes):
        """Parse the exact downloaded body and reject incomplete catalog rows.

        Returns ``(products, PreValidationResult)``. ``products`` is empty
        whenever validation fails, so preview and application share the same
        decoding, row identity checks, and parser behavior.
        """
        from services.csv_pre_validator import CSVPreValidator, PreValidationResult

        validation = PreValidationResult()
        if not isinstance(raw_bytes, bytes) or not raw_bytes:
            validation.is_valid = False
            validation.errors.append("CSV файл пуст")
            return [], validation

        try:
            codec = codecs.lookup(self.encoding)
            csv_content = raw_bytes.decode(codec.name, errors='strict')
        except (LookupError, UnicodeDecodeError):
            validation.is_valid = False
            validation.errors.append("Не удалось декодировать CSV в настроенной кодировке")
            return [], validation

        try:
            validation = CSVPreValidator.validate_raw(
                raw_bytes,
                expected_delimiter=self.supplier.csv_delimiter,
                expected_encoding=self.supplier.csv_encoding,
                column_mapping=self.supplier.csv_column_mapping,
            )
        except Exception:
            # The detailed catalog checks below are authoritative. Keep
            # preview usable if only the optional diagnostics fail.
            validation = PreValidationResult()

        try:
            expected_ids = self._catalog_feed_identities(csv_content)
            if not expected_ids:
                raise ValueError("CSV не содержит товаров с ID и названием")

            # Apply the established normalizer/enrichers only after the source
            # rows have a complete identity set.
            products = self.parse_and_normalize(csv_content)
            from services.data_normalizer import DataNormalizer

            normalized_expected_ids = [
                DataNormalizer.clean_string(external_id)
                for external_id in expected_ids
            ]
            actual_ids = [
                str(product.get('external_id') or '').strip()
                for product in products
            ]
            if (
                len(actual_ids) != len(normalized_expected_ids)
                or any(not external_id for external_id in actual_ids)
                or len(set(actual_ids)) != len(actual_ids)
                or set(actual_ids) != set(normalized_expected_ids)
                or any(not str(product.get('title') or '').strip() for product in products)
            ):
                raise ValueError("Парсер не сохранил полный набор ID из CSV")
        except (csv.Error, UnicodeError, ValueError, TypeError) as exc:
            validation.is_valid = False
            message = str(exc)
            if message not in validation.errors:
                validation.errors.append(message[:200])
            return [], validation
        except Exception:
            validation.is_valid = False
            validation.errors.append("Не удалось полностью разобрать CSV каталог")
            return [], validation

        # CSVPreValidator has a generic two-line heuristic, which is not a
        # completeness contract for a headerless single-row fixture. The
        # strict source identity checks above are the authoritative result.
        validation.is_valid = True
        validation.errors = []
        return products, validation

    def _catalog_feed_identities(self, csv_content: str) -> List[str]:
        """Validate every substantive row and return its exact source ID."""
        if not isinstance(self.delimiter, str) or len(self.delimiter) != 1:
            raise ValueError("Неверно настроен разделитель CSV")

        reader = csv.reader(
            StringIO(csv_content, newline=''),
            delimiter=self.delimiter,
            quotechar='"',
            strict=True,
        )
        rows = list(reader)
        data_rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not data_rows:
            return []

        if (
            self.supplier.code == 'sexoptovik'
            and not self.supplier.csv_column_mapping
        ):
            identities = []
            numeric_ids = set()
            for row in data_rows:
                if len(row) != 18:
                    raise ValueError("Некорректная структура строки каталога sexoptovik")
                external_id = row[0].strip()
                title = row[2].strip()
                if not re.fullmatch(r'[0-9]+', external_id, flags=re.ASCII):
                    raise ValueError("Некорректный ID товара в каталоге sexoptovik")
                numeric_id = int(external_id)
                if numeric_id <= 0 or numeric_id > (1 << 63) - 1:
                    raise ValueError("Некорректный ID товара в каталоге sexoptovik")
                if external_id != str(numeric_id):
                    raise ValueError("Неканонический ID товара в каталоге sexoptovik")
                if numeric_id in numeric_ids:
                    raise ValueError("В каталоге sexoptovik найдены повторяющиеся ID")
                if not title:
                    raise ValueError("В каталоге sexoptovik найдена строка без названия")
                numeric_ids.add(numeric_id)
                identities.append(external_id)
            return identities

        mapping = self.supplier.csv_column_mapping
        if isinstance(mapping, dict) and mapping:
            has_header = bool(getattr(self.supplier, 'csv_has_header', False))
            uses_header_names = any(
                isinstance(config.get('column'), str)
                or isinstance(config.get('columns'), (list, dict))
                for config in mapping.values()
                if isinstance(config, dict)
            )
            header_index = {}
            if has_header:
                header = data_rows.pop(0) if data_rows else []
                if not header:
                    raise ValueError("В CSV отсутствует строка заголовков")
                header_index = {
                    value.strip().strip('"'): index
                    for index, value in enumerate(header)
                }

            def mapped_index(field_name):
                config = mapping.get(field_name)
                if not isinstance(config, dict):
                    return None
                column = config.get('column')
                if isinstance(column, str):
                    return header_index.get(column) if has_header and uses_header_names else None
                return column if isinstance(column, int) and column >= 0 else None

            id_index = mapped_index('external_id')
            title_index = mapped_index('title')
            if id_index is None or title_index is None:
                raise ValueError("В маппинге CSV не заданы колонки ID и названия")

            identities = []
            seen = set()
            for row in data_rows:
                if id_index >= len(row) or title_index >= len(row):
                    raise ValueError("В строке каталога отсутствует ID или название")
                external_id = row[id_index].strip()
                title = row[title_index].strip()
                if not external_id or not title:
                    raise ValueError("В строке каталога отсутствует ID или название")
                if external_id in seen:
                    raise ValueError("В каталоге найдены повторяющиеся ID")
                seen.add(external_id)
                identities.append(external_id)
            return identities

        # Generic DictReader uses these exact legacy aliases and generates a
        # row-number identity only when the source has no ID column at all.
        header, *records = data_rows
        id_names = ('id', 'ID', 'id товара', 'Артикул')
        title_names = ('title', 'Название', 'name', 'наименование')
        id_name = next((name for name in id_names if name in header), None)
        title_name = next((name for name in title_names if name in header), None)
        if title_name is None:
            raise ValueError("В CSV не найдена колонка названия товара")

        identities = []
        seen = set()
        row_number = 0
        for row in records:
            if not any(cell.strip() for cell in row):
                continue
            row_number += 1
            title = row[header.index(title_name)].strip() if header.index(title_name) < len(row) else ''
            if not title:
                raise ValueError("В строке каталога отсутствует название")
            if id_name is None:
                external_id = str(row_number)
            else:
                id_index = header.index(id_name)
                external_id = row[id_index].strip() if id_index < len(row) else ''
                if not external_id:
                    raise ValueError("В строке каталога отсутствует ID")
            if external_id in seen:
                raise ValueError("В каталоге найдены повторяющиеся ID")
            seen.add(external_id)
            identities.append(external_id)
        return identities

    def parse(self, csv_content: str) -> List[Dict]:
        """
        Парсит CSV и возвращает список товаров.

        Приоритет выбора стратегии:
        1. Конфигурируемый маппинг (csv_column_mapping)
        2. Legacy-формат по коду поставщика (sexoptovik)
        3. Generic парсинг (заголовки в первой строке)
        """
        # Конфигурируемый маппинг — универсальный парсинг
        if self.supplier.csv_column_mapping:
            return self._parse_with_mapping(csv_content)

        # Legacy — hardcoded форматы
        if self.supplier.code == 'sexoptovik':
            return self._parse_sexoptovik(csv_content)

        # Generic — DictReader
        return self._parse_generic(csv_content)

    def parse_and_normalize(self, csv_content: str) -> List[Dict]:
        """
        Парсит CSV и автоматически нормализует данные.
        Рекомендуемый метод для новых интеграций.

        Pipeline: parse → normalize → enrich → auto-correct
        """
        from services.data_normalizer import DataNormalizer

        products = self.parse(csv_content)
        normalized = DataNormalizer.normalize_product_list(products)

        # Обогащение из описаний (заполняет пустые поля из текста описания)
        try:
            from services.description_enricher import DescriptionEnricher
            normalized = DescriptionEnricher.enrich_product_list(normalized)
        except Exception as e:
            logger.debug(f"Description enrichment skipped: {e}")

        # Автокоррекция (бренд из title, страна из бренда, очистка мусора)
        try:
            from services.auto_correction_rules import get_default_engine
            engine = get_default_engine()
            normalized = engine.apply_to_list(normalized)
        except Exception as e:
            logger.debug(f"Auto-correction skipped: {e}")

        return normalized

    # ------------------------------------------------------------------
    # Конфигурируемый маппинг колонок
    # ------------------------------------------------------------------

    def _parse_with_mapping(self, csv_content: str) -> List[Dict]:
        """
        Парсинг CSV с конфигурируемым маппингом колонок.

        csv_column_mapping формат:
        {
            "external_id": {"column": 0, "type": "string"},          # по индексу
            "external_id": {"column": "code", "type": "string"},     # по имени заголовка (csv_has_header=True)
            "vendor_code": {"column": 1, "type": "string"},
            "title": {"column": 2, "type": "string"},
            "categories": {"column": 3, "type": "list", "separator": "#"},
            "brand": {"column": 4, "type": "string"},
            "country": {"column": 5, "type": "string"},
            "gender": {"column": 8, "type": "string"},
            "colors": {"column": 9, "type": "list", "separator": ","},
            "sizes_raw": {"column": 10, "type": "string"},
            "photo_codes": {"column": 13, "type": "list", "separator": ","},
            "barcodes": {"column": 14, "type": "list", "separator": "#"},
            "materials": {"column": 15, "type": "list", "separator": ","},
            "description": {"column": 16, "type": "string"},
            "price": {"column": 7, "type": "number"},
            # Составные типы (несколько колонок):
            "supplier_quantity": {"columns": ["msk", "spb"], "type": "stock_sum"},
            "photo_urls": {"columns": ["image", "image1"], "type": "photo_urls"},
            "characteristics": {"columns": {"length": "Длина"}, "type": "characteristics"}
        }
        """
        mapping = self.supplier.csv_column_mapping
        if not mapping or not isinstance(mapping, dict):
            logger.error(f"Invalid csv_column_mapping for {self.supplier.code}")
            return self._parse_generic(csv_content)

        products = []
        reader = csv.reader(StringIO(csv_content), delimiter=self.delimiter, quotechar='"')

        has_header = getattr(self.supplier, 'csv_has_header', False)
        header_index = {}  # имя заголовка → индекс колонки

        # Резолвим маппинг: строковые имена колонок → числовые индексы
        resolved_mapping = {}
        uses_header_names = any(
            isinstance(m.get('column'), str) or isinstance(m.get('columns'), (list, dict))
            for m in mapping.values() if isinstance(m, dict)
        )

        if has_header and uses_header_names:
            # Читаем первую строку как заголовки
            try:
                header_row = next(reader)
                header_index = {
                    col.strip().strip('"'): idx for idx, col in enumerate(header_row)
                }
                logger.debug(f"CSV headers ({self.supplier.code}): {list(header_index.keys())[:20]}...")
            except StopIteration:
                logger.error(f"CSV is empty for {self.supplier.code}")
                return []

            # Резолвим каждое поле маппинга
            for field_name, config in mapping.items():
                if not isinstance(config, dict):
                    continue
                resolved_mapping[field_name] = self._resolve_mapping_config(
                    config, header_index
                )
        else:
            resolved_mapping = mapping

        # Определяем минимальное количество колонок для валидации строк
        max_col = 0
        for m in resolved_mapping.values():
            if not isinstance(m, dict):
                continue
            col = m.get('column')
            if isinstance(col, int):
                max_col = max(max_col, col)
            cols = m.get('columns')
            if isinstance(cols, list):
                for c in cols:
                    if isinstance(c, int):
                        max_col = max(max_col, c)
            elif isinstance(cols, dict):
                for c in cols.keys():
                    if isinstance(c, int):
                        max_col = max(max_col, c)

        # Несмаппленные колонки заголовка: сохраняются в product['raw_extra'],
        # чтобы данные фида не терялись безвозвратно (opt-in флагом маппинга).
        unmapped_columns = {}
        if has_header and header_index and mapping.get('_include_unmapped'):
            mapped_idx = set()
            for m in resolved_mapping.values():
                if not isinstance(m, dict):
                    continue
                col = m.get('column')
                if isinstance(col, int) and col >= 0:
                    mapped_idx.add(col)
                cols = m.get('columns')
                if isinstance(cols, list):
                    mapped_idx.update(c for c in cols if isinstance(c, int))
                elif isinstance(cols, dict):
                    mapped_idx.update(c for c in cols.keys() if isinstance(c, int))
            unmapped_columns = {
                idx: name for name, idx in header_index.items()
                if idx not in mapped_idx
            }

        for row_num, row in enumerate(reader, 1):
            try:
                # Пропускаем заголовок если header ещё не был прочитан выше
                if has_header and not uses_header_names and row_num == 1:
                    continue

                if len(row) <= max_col:
                    # Missing trailing optional columns are empty cells, not
                    # a reason to drop an otherwise complete ID/title row.
                    row.extend([''] * (max_col + 1 - len(row)))

                product = self._extract_fields_by_mapping(
                    row, resolved_mapping, unmapped_columns
                )
                if not product:
                    continue

                # Фото — специальная обработка (legacy photo_codes)
                if 'photo_codes' in resolved_mapping and product.get('_photo_codes'):
                    product['photo_urls'] = self._build_photo_urls(
                        product.get('external_id', ''),
                        product['_photo_codes']
                    )
                    del product['_photo_codes']

                products.append(product)

            except Exception as e:
                logger.error(f"Mapping-парсинг строка {row_num}: {e}")
                continue

        logger.info(
            f"Mapping-парсинг: {len(products)} товаров из CSV ({self.supplier.code})"
        )
        return products

    def _resolve_mapping_config(self, config: dict, header_index: dict) -> dict:
        """Резолвит строковые имена колонок в числовые индексы."""
        resolved = dict(config)

        # Одиночная колонка: "column": "code" → "column": 0
        col = config.get('column')
        if isinstance(col, str):
            idx = header_index.get(col)
            if idx is not None:
                resolved['column'] = idx
            else:
                logger.warning(f"Header '{col}' not found in CSV, skipping field")
                resolved['column'] = -1  # будет пропущен

        # Множественные колонки (list): ["msk", "spb"] → [8, 34]
        cols = config.get('columns')
        if isinstance(cols, list):
            resolved['columns'] = []
            for c in cols:
                if isinstance(c, str):
                    idx = header_index.get(c)
                    if idx is not None:
                        resolved['columns'].append(idx)
                    else:
                        logger.warning(f"Header '{c}' not found in CSV")
                else:
                    resolved['columns'].append(c)

        # Множественные колонки (dict): {"length": "Длина"} → {18: "Длина"}
        if isinstance(cols, dict):
            resolved_cols = {}
            for col_key, label in cols.items():
                if isinstance(col_key, str):
                    idx = header_index.get(col_key)
                    if idx is not None:
                        resolved_cols[idx] = label
                    else:
                        logger.warning(f"Header '{col_key}' not found in CSV")
                else:
                    resolved_cols[col_key] = label
            resolved['columns'] = resolved_cols

        # Авто-обнаружение колонок по префиксу (например, все image, image1..imageN)
        prefix = config.get('columns_prefix')
        if isinstance(prefix, str) and prefix:
            discovered = discover_columns_by_prefix(header_index, prefix)
            existing = resolved.get('columns')
            merged = list(existing) if isinstance(existing, list) else []
            for idx in discovered:
                if idx not in merged:
                    merged.append(idx)
            resolved['columns'] = merged

        return resolved

    def _extract_fields_by_mapping(self, row: list, mapping: dict,
                                   unmapped_columns: Optional[Dict[int, str]] = None) -> Optional[Dict]:
        """Извлечь поля из строки CSV по маппингу."""
        product = {}

        for field_name, config in mapping.items():
            if not isinstance(config, dict):
                continue

            field_type = config.get('type', 'string')
            separator = config.get('separator', ',')

            # list из нескольких колонок: значения объединяются по порядку
            # колонок с дедупликацией (первая колонка — приоритетная)
            if field_type == 'list' and isinstance(config.get('columns'), list):
                merged = []
                for col_idx in config['columns']:
                    if isinstance(col_idx, int) and 0 <= col_idx < len(row):
                        cell = row[col_idx].strip()
                        if not cell:
                            continue
                        for part in cell.split(separator):
                            value = part.strip()
                            if value and value not in merged:
                                merged.append(value)
                if field_name == 'categories':
                    product['all_categories'] = merged
                    product['category'] = merged[0] if merged else ''
                else:
                    product[field_name] = merged
                continue

            # --- Составные типы (несколько колонок) ---
            if field_type == 'stock_sum':
                # Суммирование остатков по нескольким складам
                columns = config.get('columns', [])
                total = 0
                for col_idx in columns:
                    if isinstance(col_idx, int) and col_idx < len(row):
                        raw = row[col_idx].strip()
                        if raw:
                            try:
                                total += int(float(raw.replace(',', '.').replace(' ', '')))
                            except (ValueError, TypeError):
                                pass
                product[field_name] = total
                continue

            if field_type == 'photo_urls':
                # Сборка фото из нескольких колонок (прямые URL).
                # Если в колонке несколько URL через разделитель — разбиваем.
                # Колонки могут дублировать друг друга (sex-opt: image/image1/image2
                # повторяют первые URL колонки images) — дедуп с сохранением порядка.
                columns = config.get('columns', [])
                sep = config.get('separator')
                photos = []
                seen_urls = set()
                for col_idx in columns:
                    if isinstance(col_idx, int) and 0 <= col_idx < len(row):
                        cell = row[col_idx].strip()
                        if not cell:
                            continue
                        parts = cell.split(sep) if sep else [cell]
                        for part in parts:
                            url = part.strip()
                            if url and url.startswith('http') and url not in seen_urls:
                                seen_urls.add(url)
                                photos.append({'original': url})
                product['photo_urls'] = photos
                continue

            if field_type == 'characteristics':
                # Сборка характеристик из отдельных колонок
                columns = config.get('columns', {})
                target = '_extra_dimensions' if field_name in ('dimensions', 'package_dimensions') else '_extra_characteristics'
                chars = product.get(target, [])
                if isinstance(columns, dict):
                    for col_idx, char_name in columns.items():
                        col_idx = int(col_idx) if isinstance(col_idx, str) and col_idx.isdigit() else col_idx
                        if isinstance(col_idx, int) and col_idx < len(row):
                            val = row[col_idx].strip()
                            if val:
                                chars.append({'name': char_name, 'value': val})
                product[target] = chars
                continue

            # --- Одиночные колонки ---
            col_idx = config.get('column', 0)

            if col_idx == -1 or not isinstance(col_idx, int):
                continue

            if col_idx >= len(row):
                continue

            raw_value = row[col_idx].strip()

            if field_type == 'list':
                values = [v.strip() for v in raw_value.split(separator) if v.strip()]
                if field_name == 'categories':
                    product['all_categories'] = values
                    product['category'] = values[0] if values else ''
                elif field_name == 'photo_codes':
                    product['_photo_codes'] = values
                else:
                    product[field_name] = values
            elif field_type == 'number':
                try:
                    product[field_name] = float(raw_value.replace(',', '.').replace(' ', ''))
                except (ValueError, TypeError):
                    product[field_name] = None
            else:
                product[field_name] = raw_value

        # Финализация характеристик
        if product.get('_extra_characteristics'):
            existing = []
            if product.get('characteristics_json'):
                try:
                    existing = json.loads(product['characteristics_json'])
                except (json.JSONDecodeError, TypeError):
                    pass
            existing.extend(product['_extra_characteristics'])
            product['characteristics'] = existing
            del product['_extra_characteristics']

        if product.get('_extra_dimensions'):
            # dict {имя: значение} — так габариты читаются и WB dimension
            # builder-ом, и marketplace fact pack (Ozon) как observed факт
            product['dimensions'] = {
                item['name']: item['value']
                for item in product['_extra_dimensions']
            }
            del product['_extra_dimensions']

        # Несмаппленные колонки фида — bounded passthrough в original_data
        if unmapped_columns:
            raw_extra = {}
            for col_idx, col_name in unmapped_columns.items():
                if 0 <= col_idx < len(row):
                    value = row[col_idx].strip()
                    if value:
                        raw_extra[col_name] = value
            if raw_extra:
                product['raw_extra'] = raw_extra

        # Валидация минимальных полей
        if not product.get('external_id') or not product.get('title'):
            return None

        # Заполняем дефолтные значения для отсутствующих полей
        defaults = {
            'vendor_code': '', 'category': '', 'all_categories': [],
            'brand': '', 'country': '', 'gender': '',
            'colors': [], 'sizes_raw': '', 'photo_urls': [],
            'barcodes': [], 'materials': [], 'description': '',
        }
        for key, default in defaults.items():
            if key not in product:
                product[key] = default

        return product

    def _build_photo_urls(self, external_id: str, photo_codes: List[str]) -> List[Dict]:
        """Формирует URL фото (универсальная версия для конфигурируемого маппинга)."""
        if not photo_codes or not external_id:
            return []

        # Для sexoptovik формата — сохраняем совместимость
        if self.supplier.code == 'sexoptovik':
            return self._parse_sexoptovik_photos(external_id, ','.join(photo_codes))

        # Для других поставщиков — фото-коды могут быть URL
        photos = []
        for code in photo_codes:
            if code.startswith('http'):
                photos.append({'original': code})
            else:
                photos.append({'code': code})
        return photos

    # ------------------------------------------------------------------
    # Legacy: sexoptovik
    # ------------------------------------------------------------------

    def _parse_sexoptovik(self, csv_content: str) -> List[Dict]:
        """Парсинг формата sexoptovik (legacy, hardcoded)"""
        products = []
        reader = csv.reader(StringIO(csv_content), delimiter=self.delimiter, quotechar='"')

        for row_num, row in enumerate(reader, 1):
            try:
                if len(row) < 15:
                    continue

                external_id = row[0].strip() if len(row) > 0 else ''
                vendor_code = row[1].strip() if len(row) > 1 else ''
                title = row[2].strip() if len(row) > 2 else ''

                if not external_id or not title:
                    continue

                # Категории (через #)
                categories_raw = row[3].strip() if len(row) > 3 else ''
                categories = [c.strip() for c in categories_raw.split('#') if c.strip()]
                main_category = categories[0] if categories else ''

                brand = row[4].strip() if len(row) > 4 else ''
                country = row[5].strip() if len(row) > 5 else ''
                general_category = row[6].strip() if len(row) > 6 else ''
                gender = row[8].strip() if len(row) > 8 else ''

                # Цвета
                colors_raw = row[9].strip() if len(row) > 9 else ''
                colors = [c.strip() for c in colors_raw.split(',') if c.strip()]

                # Размеры
                sizes_raw = row[10].strip() if len(row) > 10 else ''

                # Фото
                photo_codes_raw = row[13].strip() if len(row) > 13 else ''
                photo_urls = self._parse_sexoptovik_photos(external_id, photo_codes_raw)

                # Баркоды
                barcodes_raw = row[14].strip() if len(row) > 14 else ''
                barcodes = [b.strip() for b in barcodes_raw.split('#') if b.strip()]

                # Материалы
                materials_raw = row[15].strip() if len(row) > 15 else ''
                materials = [m.strip() for m in materials_raw.split(',') if m.strip()]

                products.append({
                    'external_id': external_id,
                    'vendor_code': vendor_code,
                    'title': title,
                    'category': main_category,
                    'all_categories': categories,
                    'brand': brand,
                    'country': country,
                    'gender': gender,
                    'colors': colors,
                    'sizes_raw': sizes_raw,
                    'photo_urls': photo_urls,
                    'barcodes': barcodes,
                    'materials': materials,
                    'description': '',
                })

            except Exception as e:
                logger.error(f"Ошибка парсинга строки {row_num}: {e}")
                continue

        logger.info(f"Распарсено {len(products)} товаров из CSV ({self.supplier.code})")
        return products

    def _parse_sexoptovik_photos(self, product_id: str, photo_codes: str) -> List[Dict]:
        """Формирует URL фотографий для sexoptovik"""
        if not photo_codes or not product_id:
            return []

        photos = []
        extracted = extract_supplier_product_id(product_id)
        numeric_id = str(extracted) if extracted else product_id

        if ',' in photo_codes:
            photo_nums = [p.strip() for p in photo_codes.split(',') if p.strip()]
        else:
            photo_nums = [p.strip() for p in photo_codes.split() if p.strip()]

        for num in photo_nums:
            photos.append({
                'sexoptovik': f"https://sexoptovik.ru/admin/_project/user_images/prods_res/{numeric_id}/{numeric_id}_{num}_1200.jpg",
                'blur': f"https://x-story.ru/mp/_project/img_sx0_1200/{numeric_id}_{num}_1200.jpg",
                'original': f"https://x-story.ru/mp/_project/img_sx_1200/{numeric_id}_{num}_1200.jpg",
            })

        return photos

    # ------------------------------------------------------------------
    # Generic
    # ------------------------------------------------------------------

    def _parse_generic(self, csv_content: str) -> List[Dict]:
        """Generic парсинг CSV (заголовки в первой строке)"""
        products = []
        reader = csv.DictReader(StringIO(csv_content), delimiter=self.delimiter)

        for row_num, row in enumerate(reader, 1):
            try:
                # Пытаемся найти поля по типовым названиям
                external_id = (row.get('id') or row.get('ID') or
                               row.get('id товара') or row.get('Артикул') or
                               str(row_num))
                title = (row.get('title') or row.get('Название') or
                         row.get('name') or row.get('наименование') or '')
                brand = row.get('brand') or row.get('Бренд') or ''
                category = row.get('category') or row.get('Категория') or ''
                price_str = (row.get('price') or row.get('Цена') or
                             row.get('цена') or row.get('цена, руб.') or '0')

                try:
                    price = float(str(price_str).replace(',', '.').replace(' ', ''))
                except (ValueError, TypeError):
                    price = None

                if not title:
                    continue

                products.append({
                    'external_id': str(external_id).strip(),
                    'vendor_code': row.get('vendor_code') or row.get('Артикул поставщика') or '',
                    'title': title.strip(),
                    'category': category.strip(),
                    'all_categories': [category.strip()] if category else [],
                    'brand': brand.strip(),
                    'country': row.get('country') or row.get('Страна') or '',
                    'gender': '',
                    'colors': [],
                    'sizes_raw': '',
                    'photo_urls': [],
                    'barcodes': [],
                    'materials': [],
                    'description': row.get('description') or row.get('Описание') or '',
                    'supplier_price': price,
                })

            except Exception as e:
                logger.error(f"Generic парсинг строка {row_num}: {e}")
                continue

        logger.info(f"Generic парсинг: {len(products)} товаров")
        return products


def discover_columns_by_prefix(header_index: dict, prefix: str) -> list:
    """Найти индексы колонок, заголовки которых матчат ^<prefix>\\d*$.

    Возвращает индексы в порядке: голый префикс (image) первым, затем
    числовые суффиксы по возрастанию (image1, image2, ..., image10).
    """
    pattern = re.compile(rf'^{re.escape(prefix)}(\d*)$')
    matched = []
    for name, idx in header_index.items():
        m = pattern.match(name)
        if m:
            suffix = m.group(1)
            order = int(suffix) if suffix else -1  # голый префикс — первым
            matched.append((order, idx))
    matched.sort(key=lambda pair: pair[0])
    return [idx for _, idx in matched]


@contextmanager
def _supplier_catalog_sync_lock(supplier_id: int):
    """Cross-process nonblocking fence for one supplier's catalog refresh."""
    try:
        import fcntl
    except ImportError:
        # A refresh without the process-shared fence would permit duplicate
        # provider reads and concurrent writes to the same source catalog.
        yield False
        return

    lock_path = os.path.join(
        '/tmp', f'supplier-catalog-sync-{int(supplier_id)}.lock',
    )
    try:
        handle = open(lock_path, 'a+b')
    except OSError:
        logger.error("Could not open supplier catalog lock for supplier %s", supplier_id)
        yield False
        return

    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError):
            pass
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


# ============================================================================
# SUPPLIER SERVICE
# ============================================================================

class SupplierService:
    """Основной сервисный класс для работы с поставщиками"""

    supplier_catalog_config_digest = staticmethod(supplier_catalog_config_digest)

    # -----------------------------------------------------------------------
    # CRUD поставщиков
    # -----------------------------------------------------------------------

    @staticmethod
    def create_supplier(data: dict, created_by_user_id: int = None) -> Supplier:
        """Создать нового поставщика"""
        supplier = Supplier(
            name=data['name'],
            code=data['code'],
            description=data.get('description'),
            website=data.get('website'),
            csv_source_url=data.get('csv_source_url'),
            csv_delimiter=data.get('csv_delimiter', ';'),
            csv_encoding=data.get('csv_encoding', 'cp1251'),
            price_file_url=data.get('price_file_url'),
            price_file_inf_url=data.get('price_file_inf_url'),
            price_file_delimiter=data.get('price_file_delimiter', ';'),
            price_file_encoding=data.get('price_file_encoding', 'cp1251'),
            auto_sync_prices=data.get('auto_sync_prices', False),
            auto_sync_interval_minutes=data.get('auto_sync_interval_minutes', 60),
            auth_login=data.get('auth_login'),
            ai_enabled=data.get('ai_enabled', False),
            ai_provider=data.get('ai_provider', 'openai'),
            ai_api_base_url=data.get('ai_api_base_url'),
            ai_model=data.get('ai_model', 'gpt-4o-mini'),
            ai_temperature=data.get('ai_temperature', 0.3),
            ai_max_tokens=data.get('ai_max_tokens', 2000),
            ai_timeout=data.get('ai_timeout', 60),
            resize_images=data.get('resize_images', True),
            image_target_size=data.get('image_target_size', 1200),
            image_background_color=data.get('image_background_color', 'white'),
            default_markup_percent=data.get('default_markup_percent'),
            is_active=data.get('is_active', True),
            created_by_user_id=created_by_user_id,
        )

        # Зашифрованные поля через setter
        if data.get('auth_password'):
            supplier.auth_password = data['auth_password']
        if data.get('ai_api_key'):
            supplier.ai_api_key = data['ai_api_key']

        # AI кастомные инструкции
        for field_name in ('ai_category_instruction', 'ai_size_instruction',
                           'ai_seo_title_instruction', 'ai_keywords_instruction',
                           'ai_description_instruction', 'ai_analysis_instruction'):
            if data.get(field_name):
                setattr(supplier, field_name, data[field_name])

        db.session.add(supplier)
        db.session.commit()
        logger.info(f"Создан поставщик: {supplier.code} (id={supplier.id})")
        return supplier

    @staticmethod
    def update_supplier(supplier_id: int, data: dict) -> Optional[Supplier]:
        """Обновить данные поставщика"""
        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return None

        # Обычные поля
        simple_fields = [
            'name', 'description', 'website', 'csv_source_url', 'csv_delimiter',
            'csv_encoding', 'csv_column_mapping', 'csv_has_header',
            'api_endpoint', 'auth_login', 'ai_enabled', 'ai_provider',
            'ai_api_base_url', 'ai_model', 'ai_temperature', 'ai_max_tokens',
            'ai_timeout', 'ai_client_id', 'ai_client_secret',
            'resize_images', 'image_target_size', 'image_background_color',
            'default_markup_percent', 'is_active',
            'price_file_url', 'price_file_inf_url', 'price_file_delimiter',
            'price_file_encoding', 'auto_sync_prices', 'auto_sync_interval_minutes',
            'ai_proxy_enabled',
            'ai_category_instruction', 'ai_size_instruction',
            'ai_seo_title_instruction', 'ai_keywords_instruction',
            'ai_description_instruction', 'ai_analysis_instruction',
            'ai_parsing_instruction',
            'description_file_url', 'description_file_delimiter', 'description_file_encoding',
            'external_id_pattern', 'default_vendor_code_pattern',
        ]
        for f in simple_fields:
            if f in data:
                setattr(supplier, f, data[f])

        # Зашифрованные поля
        if 'auth_password' in data and data['auth_password']:
            supplier.auth_password = data['auth_password']
        if 'ai_api_key' in data and data['ai_api_key']:
            supplier.ai_api_key = data['ai_api_key']

        db.session.commit()
        logger.info(f"Обновлён поставщик: {supplier.code} (id={supplier.id})")
        return supplier

    @staticmethod
    def delete_supplier(supplier_id: int) -> bool:
        """Удалить поставщика (и все его товары)"""
        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return False

        db.session.delete(supplier)
        db.session.commit()
        logger.info(f"Удалён поставщик: {supplier.code} (id={supplier_id})")
        return True

    @staticmethod
    def get_supplier(supplier_id: int) -> Optional[Supplier]:
        """Получить поставщика по ID"""
        return Supplier.query.get(supplier_id)

    @staticmethod
    def get_supplier_by_code(code: str) -> Optional[Supplier]:
        """Получить поставщика по коду"""
        return Supplier.query.filter_by(code=code).first()

    @staticmethod
    def list_suppliers(active_only: bool = False) -> List[Supplier]:
        """Список всех поставщиков"""
        q = Supplier.query
        if active_only:
            q = q.filter_by(is_active=True)
        return q.order_by(Supplier.name).all()

    # -----------------------------------------------------------------------
    # Синхронизация каталога из CSV
    # -----------------------------------------------------------------------

    @staticmethod
    def sync_from_csv(supplier_id: int, price_data: Dict[str, float] = None) -> SyncResult:
        """Safely refresh one complete catalog, then run local postprocessing."""
        start_time = time.time()
        with _supplier_catalog_sync_lock(supplier_id) as acquired:
            if not acquired:
                return SyncResult(
                    success=False,
                    busy=True,
                    error_messages=["Обновление каталога этого поставщика уже выполняется"],
                    price_followup_status='skipped',
                )
            return SupplierService._sync_from_csv_locked(
                supplier_id, price_data=price_data, start_time=start_time,
            )

    @staticmethod
    def _sync_from_csv_locked(
        supplier_id: int,
        *,
        price_data: Dict[str, float] = None,
        start_time: float,
        expected_config_digest: Optional[str] = None,
        raw_checkpoint=None,
    ) -> SyncResult:
        result = SyncResult()
        supplier = (
            Supplier.query.filter_by(id=supplier_id)
            .populate_existing()
            .first()
        )
        if not supplier:
            result.success = False
            result.error_messages.append("Поставщик не найден")
            result.catalog_error_code = 'supplier_not_found'
            result.price_followup_status = 'skipped'
            return result

        config = _supplier_catalog_config_snapshot(supplier)
        config_digest = supplier_catalog_config_digest(supplier)
        if (
            expected_config_digest is not None
            and expected_config_digest != config_digest
        ):
            result.success = False
            result.catalog_error_code = 'source_changed'
            result.price_followup_status = 'skipped'
            result.error_messages.append("Настройки источника изменились до начала чтения каталога")
            return result

        parser_supplier = SimpleNamespace(**copy.deepcopy(config))
        supplier.last_sync_status = 'running'
        supplier.last_sync_error = None
        db.session.commit()

        def mark_failed(
            message: str,
            *,
            code: str,
            error_count: int = 1,
        ) -> SyncResult:
            db.session.rollback()
            result.success = False
            result.duration_seconds = time.time() - start_time
            result.errors = error_count
            result.added = 0
            result.updated = 0
            result.error_messages = [message]
            result.catalog_error_code = code
            result.price_followup_status = 'skipped'
            try:
                current = db.session.get(Supplier, supplier_id)
                if current:
                    current.last_sync_status = 'failed'
                    current.last_sync_error = message[:500]
                    db.session.commit()
            except Exception:
                db.session.rollback()
                logger.error("Could not persist failed catalog status for supplier %s", supplier_id)
            return result

        # Re-read after committing running and close that read transaction before
        # HTTP so the admitted config remains current at the start of the read.
        try:
            db.session.rollback()
            live_supplier = (
                Supplier.query.filter_by(id=supplier_id)
                .populate_existing()
                .first()
            )
            current_digest = (
                supplier_catalog_config_digest(live_supplier)
                if live_supplier else None
            )
            config_still_current = (
                current_digest == config_digest
                and (
                    expected_config_digest is None
                    or current_digest == expected_config_digest
                )
            )
            db.session.rollback()
        except Exception:
            db.session.rollback()
            return mark_failed(
                "Не удалось повторно проверить настройки источника",
                code='catalog_config_check_failed',
            )
        if not config_still_current:
            return mark_failed(
                "Настройки источника изменились до начала чтения каталога",
                code='source_changed',
            )

        parser = SupplierCSVParser(parser_supplier)
        raw_bytes = parser.fetch_csv_raw()
        if raw_bytes is None:
            return mark_failed(
                "Не удалось скачать CSV каталога",
                code='catalog_fetch_failed',
            )

        parsed_products, pre_result = parser.preflight_raw(raw_bytes)
        if not pre_result.is_valid:
            logger.warning(
                "Catalog preflight rejected supplier %s (%s error(s))",
                config['code'], len(pre_result.errors),
            )
            message = (
                pre_result.errors[0]
                if pre_result.errors else "CSV каталога не прошёл полную проверку"
            )
            return mark_failed(message, code='catalog_validation_failed')
        result.total_in_csv = len(parsed_products)
        if not parsed_products:
            return mark_failed(
                "CSV каталог не содержит товаров",
                code='catalog_empty',
            )

        # Admin settings can change while the body is being read. Revalidate
        # the exact source/parser configuration before opening the apply phase.
        try:
            if db.session.new or db.session.dirty or db.session.deleted:
                raise RuntimeError("catalog_apply_requires_clean_session")
            if db.engine.dialect.name == 'sqlite':
                connection = db.session.connection().connection.driver_connection
                if getattr(connection, 'in_transaction', False):
                    raise RuntimeError("catalog_apply_requires_fresh_transaction")
                # Take the writer reservation before the supplier re-read so a
                # config update cannot race between comparison and raw apply.
                db.session.execute(text('BEGIN IMMEDIATE'))
            live_supplier = (
                Supplier.query.filter_by(id=supplier_id)
                .populate_existing()
                .first()
            )
            current_digest = (
                supplier_catalog_config_digest(live_supplier)
                if live_supplier else None
            )
            if current_digest != config_digest:
                return mark_failed(
                    "Настройки источника изменились во время чтения каталога",
                    code='source_changed',
                )
        except Exception:
            return mark_failed(
                "Не удалось повторно проверить настройки источника",
                code='catalog_config_check_failed',
            )

        try:
            existing_map = {
                product.external_id: product
                for product in SupplierProduct.query.filter_by(
                    supplier_id=supplier_id,
                ).all()
                if product.external_id
            }

            # Keep the raw source baseline and final material baseline through
            # the native SmartParser post-ingest pass. Raw facts and revisions
            # commit together before optional local enrichment starts.
            revision_baselines = {}
            raw_input_baselines = {}
            raw_revision_targets = {}
            products_to_parse = []
            products_to_parse_object_ids = set()

            def enqueue_for_parse(product):
                if product is None:
                    return
                object_id = id(product)
                if object_id not in products_to_parse_object_ids:
                    products_to_parse_object_ids.add(object_id)
                    products_to_parse.append(product)

            def advance_for_raw_feed_change(product, baseline):
                if product is None or baseline is None:
                    return
                baseline_revision, previous_input_fingerprint = baseline
                current_input_fingerprint = supplier_material_input_fingerprint(
                    product.original_data_json
                )
                if current_input_fingerprint != previous_input_fingerprint:
                    product_id = product.id
                    if product_id is not None:
                        raw_revision_targets[product_id] = max(
                            raw_revision_targets.get(product_id, 0),
                            baseline_revision + 1,
                        )

            for index, product_data in enumerate(parsed_products, start=1):
                external_id = product_data['external_id']
                existing = existing_map.get(external_id)
                if existing:
                    if existing.id not in revision_baselines:
                        revision = int(existing.content_revision or 1)
                        raw_input_baselines[existing.id] = (
                            revision,
                            supplier_material_input_fingerprint(
                                existing.original_data_json
                            ),
                        )
                        revision_baselines[existing.id] = (
                            revision,
                            supplier_product_material_fingerprint(existing),
                        )
                    baseline = raw_input_baselines[existing.id]
                    _update_supplier_product(existing, product_data, price_data)
                    advance_for_raw_feed_change(existing, baseline)
                    product = existing
                    result.updated += 1
                else:
                    product = _create_supplier_product(
                        supplier_id, product_data, price_data,
                    )
                    db.session.add(product)
                    result.added += 1
                enqueue_for_parse(product)
                if index % 100 == 0:
                    db.session.flush()

            # Resolve IDs before commit so the follow-on parser does not cause
            # an accidental SELECT for every expired ORM instance.
            db.session.flush()
            product_ids_to_parse = []
            seen_product_ids = set()
            for product in products_to_parse:
                if product.id is not None and product.id not in seen_product_ids:
                    seen_product_ids.add(product.id)
                    product_ids_to_parse.append(product.id)

            advance_supplier_content_revisions(
                raw_revision_targets,
                products=products_to_parse,
            )
            result.duration_seconds = time.time() - start_time
            live_supplier.total_products = SupplierProduct.query.filter_by(
                supplier_id=supplier_id,
            ).count()
            raw_at = datetime.utcnow()
            live_supplier.last_sync_at = raw_at
            live_supplier.last_sync_status = 'success'
            live_supplier.last_sync_error = None
            live_supplier.last_sync_duration = result.duration_seconds
            # Every raw row, revision, and freshness timestamp is one commit.
            if raw_checkpoint is not None:
                try:
                    raw_checkpoint(result, raw_at)
                except Exception as exc:
                    logger.error(
                        "Catalog raw checkpoint failed for supplier %s (%s)",
                        config['code'], type(exc).__name__,
                    )
                    return mark_failed(
                        "Не удалось сохранить checkpoint синхронизации каталога",
                        code='raw_checkpoint_failed',
                    )
            db.session.commit()
        except Exception as exc:
            logger.error(
                "Atomic catalog apply failed for supplier %s (%s)",
                config['code'], type(exc).__name__,
            )
            return mark_failed(
                "Не удалось применить полный CSV каталог",
                code='catalog_apply_failed',
            )

        # Optional post-processing starts only after raw catalog facts have a
        # durable success timestamp. A failure here never rolls those facts back.
        try:
            smart_parser = SmartProductParser(supplier_id=supplier_id)
            if product_ids_to_parse:
                smart_result = smart_parser.parse_and_apply_bulk(
                    product_ids_to_parse,
                    revision_baselines=revision_baselines,
                )
                logger.info(
                    "SmartParse %s: brands=%s cats=%s avg_score=%.1f",
                    config['code'],
                    smart_result.brand_resolved_count,
                    smart_result.category_mapped_count,
                    smart_result.avg_readiness_score,
                )
                if smart_result.errors:
                    result.errors += smart_result.errors
                    result.error_messages.append(
                        "Каталог обновлён, но часть карточек не прошла локальный Smart Parser"
                    )
        except Exception as exc:
            db.session.rollback()
            result.errors += 1
            result.error_messages.append(
                "Каталог обновлён; локальная постобработка Smart Parser не завершена"
            )
            logger.warning(
                "SmartParser postprocessing failed for supplier %s (%s)",
                config['code'], type(exc).__name__,
            )

        result.duration_seconds = time.time() - start_time
        try:
            from models import ParsingLog

            all_products = SupplierProduct.query.filter_by(
                supplier_id=supplier_id,
            ).limit(500).all()
            field_fill = {}
            if all_products:
                for field_name in (
                    'title', 'brand', 'category', 'vendor_code', 'country',
                    'gender', 'barcode', 'description',
                ):
                    filled = sum(
                        1 for product in all_products
                        if getattr(product, field_name, None)
                    )
                    field_fill[field_name] = round(filled / len(all_products), 3)
                for field_name in ('colors_json', 'materials_json', 'photo_urls_json'):
                    filled = sum(
                        1 for product in all_products
                        if getattr(product, field_name, None)
                        and getattr(product, field_name) != '[]'
                    )
                    field_fill[field_name.replace('_json', '')] = round(
                        filled / len(all_products), 3,
                    )

            db.session.add(ParsingLog(
                supplier_id=supplier_id,
                event_type='sync',
                total_products=result.total_in_csv,
                processed_successfully=result.added + result.updated,
                errors_count=result.errors,
                duration_seconds=result.duration_seconds,
                field_fill_rates=field_fill,
                errors_json=result.error_messages[:20] if result.error_messages else None,
            ))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.debug(
                "Could not save catalog parsing metrics for supplier %s (%s)",
                config['code'], type(exc).__name__,
            )

        # Preserve the established bounded price/stock follow-on lane.
        price_url = config['price_file_url']
        if isinstance(price_url, str) and price_url.strip():
            try:
                price_result = SupplierService.sync_prices_and_stock(
                    supplier_id,
                    force=True,
                    expected_config={
                        'supplier_created_at': config['created_at'],
                        'price_file_url': price_url,
                        'encoding': config['price_file_encoding'],
                        'delimiter': config['price_file_delimiter'],
                    },
                )
                code = (
                    price_result.error_messages[0]
                    if price_result.error_messages else None
                )
                if price_result.success:
                    conflicts = price_result.conflicting_source_rows
                    result.price_followup_conflicts = (
                        conflicts if type(conflicts) is int and conflicts >= 0 else None
                    )
                    result.price_followup_status = (
                        'partial' if result.price_followup_conflicts else 'success'
                    )
                    logger.info(
                        "Supplier price follow-up %s: updated=%s",
                        config['code'], price_result.updated,
                    )
                elif price_result.busy or code in ('sync_busy', 'database_busy'):
                    result.price_followup_status = 'busy'
                elif code in ('source_changed', 'configuration_changed'):
                    result.price_followup_status = 'source_changed'
                else:
                    result.price_followup_status = 'failed'
            except Exception as exc:
                result.price_followup_status = 'failed'
                logger.warning(
                    "Supplier price follow-up failed for %s (%s)",
                    config['code'], type(exc).__name__,
                )
        else:
            result.price_followup_status = 'not_configured'

        logger.info(
            "Catalog sync %s: added=%s updated=%s rows=%s duration=%.1fs",
            config['code'], result.added, result.updated,
            result.total_in_csv, result.duration_seconds,
        )
        return result

    # -----------------------------------------------------------------------
    # Синхронизация цен и остатков
    # -----------------------------------------------------------------------

    @staticmethod
    def sync_prices_and_stock(
        supplier_id: int,
        force: bool = False,
        *,
        expected_config: Optional[dict] = None,
    ) -> SyncResult:
        """Read and apply one complete supplier price feed.

        The legacy force flag remains accepted. Each admitted check now reads
        the actual CSV so its per-row price observations can be refreshed.
        """
        from services.supplier_price_sync import sync_supplier_price_feed

        return sync_supplier_price_feed(
            supplier_id,
            expected_config=expected_config,
        )

    @staticmethod
    def get_price_stock_stats(supplier_id: int) -> dict:
        """Статистика по ценам и остаткам товаров поставщика"""
        base = SupplierProduct.query.filter_by(supplier_id=supplier_id)

        in_stock = base.filter(
            SupplierProduct.supplier_quantity.isnot(None),
            SupplierProduct.supplier_quantity > 0
        ).count()

        out_of_stock = base.filter(
            db.or_(
                SupplierProduct.supplier_quantity.is_(None),
                SupplierProduct.supplier_quantity == 0
            )
        ).count()

        with_price = base.filter(
            SupplierProduct.supplier_price.isnot(None),
            SupplierProduct.supplier_price > 0
        ).count()

        price_stats = db.session.query(
            db.func.min(SupplierProduct.supplier_price),
            db.func.max(SupplierProduct.supplier_price),
            db.func.avg(SupplierProduct.supplier_price),
            db.func.sum(SupplierProduct.supplier_quantity),
        ).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.supplier_price.isnot(None),
            SupplierProduct.supplier_price > 0
        ).first()

        with_rrp = base.filter(
            SupplierProduct.recommended_retail_price.isnot(None),
            SupplierProduct.recommended_retail_price > 0
        ).count()

        price_changed = base.filter(
            SupplierProduct.previous_price.isnot(None)
        ).count()

        return {
            'in_stock': in_stock,
            'out_of_stock': out_of_stock,
            'with_price': with_price,
            'with_rrp': with_rrp,
            'price_changed': price_changed,
            'min_price': round(price_stats[0], 2) if price_stats[0] else 0,
            'max_price': round(price_stats[1], 2) if price_stats[1] else 0,
            'avg_price': round(price_stats[2], 2) if price_stats[2] else 0,
            'total_stock': int(price_stats[3]) if price_stats[3] else 0,
        }

    # -----------------------------------------------------------------------
    # Управление товарами
    # -----------------------------------------------------------------------

    @staticmethod
    def get_products(supplier_id: int, page: int = 1, per_page: int = 50,
                     search: str = None, status: str = None,
                     category: str = None, brand: str = None,
                     ai_validated: bool = None, has_photos: bool = None,
                     stock_status: str = None,
                     sort_by: str = 'created_at', sort_dir: str = 'desc'):
        """Получить товары поставщика с фильтрацией и пагинацией"""
        q = SupplierProduct.query.filter_by(supplier_id=supplier_id)

        # Фильтры
        if stock_status == 'in_stock':
            q = q.filter(SupplierProduct.supplier_quantity.isnot(None),
                         SupplierProduct.supplier_quantity > 0)
        elif stock_status == 'out_of_stock':
            q = q.filter(db.or_(
                SupplierProduct.supplier_quantity.is_(None),
                SupplierProduct.supplier_quantity == 0))
        if search:
            search_term = f"%{search}%"
            q = q.filter(
                db.or_(
                    SupplierProduct.title.ilike(search_term),
                    SupplierProduct.external_id.ilike(search_term),
                    SupplierProduct.vendor_code.ilike(search_term),
                    SupplierProduct.brand.ilike(search_term),
                )
            )
        if status:
            q = q.filter(SupplierProduct.status == status)
        if category:
            q = q.filter(SupplierProduct.category.ilike(f"%{category}%"))
        if brand:
            q = q.filter(SupplierProduct.brand == brand)
        if ai_validated is not None:
            q = q.filter(SupplierProduct.ai_validated == ai_validated)
        if has_photos is True:
            q = q.filter(SupplierProduct.photo_urls_json.isnot(None))
            q = q.filter(SupplierProduct.photo_urls_json != '[]')
        elif has_photos is False:
            q = q.filter(
                db.or_(
                    SupplierProduct.photo_urls_json.is_(None),
                    SupplierProduct.photo_urls_json == '[]'
                )
            )

        # Сортировка
        sort_column = getattr(SupplierProduct, sort_by, SupplierProduct.created_at)
        if sort_dir == 'asc':
            q = q.order_by(sort_column.asc())
        else:
            q = q.order_by(sort_column.desc())

        return q.paginate(page=page, per_page=per_page, error_out=False)

    @staticmethod
    def get_product(product_id: int) -> Optional[SupplierProduct]:
        """Получить конкретный товар"""
        return SupplierProduct.query.get(product_id)

    @staticmethod
    def get_product_detail_fact_groups(product: SupplierProduct) -> dict:
        """Build bounded, provenance-separated facts for seller catalog UI."""
        observed = []
        suggested = []
        observed_names = set()
        suggested_names = set()

        def display_value(value) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, bool):
                return 'Да' if value else 'Нет'
            if isinstance(value, (str, int, float)):
                text = re.sub(r'\s+', ' ', str(value)).strip()
                return text[:1000] if text else None
            if isinstance(value, (list, tuple)):
                parts = []
                for entry in value:
                    if not isinstance(entry, (str, int, float, bool)):
                        continue
                    text = display_value(entry)
                    if text and text not in parts:
                        parts.append(text)
                    if len(parts) >= 20:
                        break
                joined = ', '.join(parts)
                return joined[:1000] if joined else None
            return None

        def add_fact(target, seen_names, name, value):
            label = re.sub(r'\s+', ' ', str(name or '')).strip()[:160]
            normalized_name = label.casefold()
            rendered_value = display_value(value)
            if (
                not label
                or label.startswith('_')
                or normalized_name in {'parsing_meta', 'meta'}
                or normalized_name in seen_names
                or not rendered_value
                or len(target) >= 80
            ):
                return
            seen_names.add(normalized_name)
            target.append({'name': label, 'value': rendered_value})

        for item in product.get_characteristics():
            if isinstance(item, dict):
                add_fact(
                    observed,
                    observed_names,
                    item.get('name'),
                    item.get('value'),
                )

        try:
            dimensions = (
                json.loads(product.dimensions_json)
                if isinstance(product.dimensions_json, str)
                else product.dimensions_json
            )
        except (TypeError, ValueError):
            dimensions = {}
        if isinstance(dimensions, dict):
            for name, value in dimensions.items():
                add_fact(observed, observed_names, name, value)

        ai_parsed = product.get_ai_parsed_data()
        if isinstance(ai_parsed, dict):
            for name, value in ai_parsed.items():
                add_fact(suggested, suggested_names, name, value)

        ai_marketplace = product.get_ai_marketplace_data()
        marketplace_characteristics = (
            ai_marketplace.get('characteristics')
            if isinstance(ai_marketplace, dict)
            else None
        )
        if isinstance(marketplace_characteristics, dict):
            for name, value in marketplace_characteristics.items():
                add_fact(suggested, suggested_names, name, value)

        marketplace_results = []
        meta = ai_marketplace.get('_meta', {}) if isinstance(ai_marketplace, dict) else {}
        if isinstance(meta, dict) and meta.get('workflow') == 'codex_luna_v2':
            from services.supplier_luna_enrichment import source_matches
            current_source = source_matches(product, meta)
            channels = meta.get('channels', {})
            for code, label in (('wb', 'Wildberries'), ('ozon', 'Ozon')):
                channel = channels.get(code) if isinstance(channels, dict) else None
                if not isinstance(channel, dict):
                    continue
                confirmed, inferred, approved = [], [], []
                for key, target in (('fields', confirmed), ('inferences', inferred),
                                    ('approved_inferences', approved)):
                    names = set()
                    for fact in (channel.get(key) or [])[:80]:
                        if isinstance(fact, dict):
                            add_fact(target, names, fact.get('name'), fact.get('value'))
                marketplace_results.append({
                    'code': code, 'label': label,
                    'category': str((channel.get('target') or {}).get('name') or '')[:300],
                    'confirmed': confirmed, 'inferred': inferred, 'approved': approved,
                    'missing_required_count': len(channel.get('missing_required') or []),
                    'source_current': current_source,
                })

        return {
            'observed': observed,
            'suggested': suggested,
            'observed_count': len(observed),
            'suggested_count': len(suggested),
            'marketplace_results': marketplace_results,
        }

    @staticmethod
    def update_product(product_id: int, data: dict) -> Optional[SupplierProduct]:
        """Обновить товар поставщика"""
        product = SupplierProduct.query.get(product_id)
        if not product:
            return None

        updatable = [
            'title', 'description', 'brand', 'category', 'vendor_code',
            'wb_category_name', 'wb_subject_id', 'wb_subject_name',
            'supplier_price', 'supplier_quantity', 'gender', 'country',
            'season', 'age_group', 'status',
        ]
        for f in updatable:
            if f in data:
                setattr(product, f, data[f])

        # JSON поля
        json_fields = [
            'characteristics_json', 'sizes_json', 'colors_json',
            'materials_json', 'dimensions_json', 'photo_urls_json',
            'ai_keywords_json', 'ai_bullets_json',
        ]
        for f in json_fields:
            if f in data:
                val = data[f]
                if isinstance(val, (list, dict)):
                    setattr(product, f, json.dumps(val, ensure_ascii=False))
                else:
                    setattr(product, f, val)

        # Текстовые AI поля
        if 'ai_seo_title' in data:
            product.ai_seo_title = data['ai_seo_title']
        if 'ai_description' in data:
            product.ai_description = data['ai_description']

        db.session.commit()
        return product

    @staticmethod
    def delete_products(product_ids: List[int]) -> int:
        """Удалить товары по ID"""
        count = SupplierProduct.query.filter(SupplierProduct.id.in_(product_ids)).delete(
            synchronize_session=False
        )
        db.session.commit()
        return count

    @staticmethod
    def get_product_stats(supplier_id: int) -> dict:
        """Статистика по товарам поставщика (один агрегатный запрос)"""
        def _status_count(status: str):
            return db.func.sum(db.case(
                (SupplierProduct.status == status, 1), else_=0,
            ))

        row = db.session.query(
            db.func.count(SupplierProduct.id),
            _status_count('draft'),
            _status_count('validated'),
            _status_count('ready'),
            _status_count('archived'),
            db.func.sum(db.case(
                (SupplierProduct.ai_validated.is_(True), 1), else_=0,
            )),
            db.func.sum(db.case(
                (db.and_(
                    SupplierProduct.photo_urls_json.isnot(None),
                    SupplierProduct.photo_urls_json != '[]',
                ), 1), else_=0,
            )),
            # distinct игнорирует NULL, nullif('') приравнивает пустую строку
            db.func.count(db.distinct(
                db.func.nullif(SupplierProduct.brand, ''))),
            db.func.count(db.distinct(
                db.func.nullif(SupplierProduct.category, ''))),
        ).filter(SupplierProduct.supplier_id == supplier_id).one()

        return {
            'total': int(row[0] or 0),
            'draft': int(row[1] or 0),
            'validated': int(row[2] or 0),
            'ready': int(row[3] or 0),
            'archived': int(row[4] or 0),
            'ai_validated': int(row[5] or 0),
            'with_photos': int(row[6] or 0),
            'brands': int(row[7] or 0),
            'categories': int(row[8] or 0),
        }

    # -----------------------------------------------------------------------
    # Подключение продавцов
    # -----------------------------------------------------------------------

    @staticmethod
    def connect_seller(seller_id: int, supplier_id: int,
                       supplier_code: str = None,
                       vendor_code_pattern: str = None) -> SellerSupplier:
        """Подключить продавца к поставщику"""
        existing = SellerSupplier.query.filter_by(
            seller_id=seller_id, supplier_id=supplier_id
        ).first()

        if existing:
            existing.is_active = True
            if supplier_code:
                existing.supplier_code = supplier_code
            if vendor_code_pattern:
                existing.vendor_code_pattern = vendor_code_pattern
            db.session.commit()
            return existing

        # Если шаблон не передан — берём default_vendor_code_pattern поставщика
        if not vendor_code_pattern:
            supplier = Supplier.query.get(supplier_id)
            if supplier and supplier.default_vendor_code_pattern:
                vendor_code_pattern = supplier.default_vendor_code_pattern

        conn = SellerSupplier(
            seller_id=seller_id,
            supplier_id=supplier_id,
            supplier_code=supplier_code,
            vendor_code_pattern=vendor_code_pattern or 'id-{product_id}-{supplier_code}',
        )
        db.session.add(conn)
        db.session.commit()
        logger.info(f"Продавец {seller_id} подключён к поставщику {supplier_id}")
        return conn

    @staticmethod
    def disconnect_seller(seller_id: int, supplier_id: int) -> bool:
        """Отключить продавца от поставщика"""
        conn = SellerSupplier.query.filter_by(
            seller_id=seller_id, supplier_id=supplier_id
        ).first()
        if not conn:
            return False
        conn.is_active = False
        db.session.commit()
        logger.info(f"Продавец {seller_id} отключён от поставщика {supplier_id}")
        return True

    @staticmethod
    def get_seller_suppliers(seller_id: int, active_only: bool = True) -> List[SellerSupplier]:
        """Получить список поставщиков продавца"""
        q = SellerSupplier.query.filter_by(seller_id=seller_id)
        if active_only:
            q = q.filter_by(is_active=True)
        return q.all()

    @staticmethod
    def get_supplier_sellers(supplier_id: int, active_only: bool = True) -> List[SellerSupplier]:
        """Получить список продавцов поставщика"""
        q = SellerSupplier.query.filter_by(supplier_id=supplier_id)
        if active_only:
            q = q.filter_by(is_active=True)
        return q.all()

    # -----------------------------------------------------------------------
    # Импорт товаров к продавцу
    # -----------------------------------------------------------------------

    @staticmethod
    def build_imported_product_from_supplier(
        seller_id: int,
        supplier_product: SupplierProduct,
    ) -> ImportedProduct:
        """Build, but do not persist, one exact seller-owned source copy.

        Marketplace identity reconciliation reuses this constructor when an
        already observed WB card and an Ozon listing both point to the same
        unique supplier row.  Keeping construction here prevents that local
        recovery path from inventing a second, poorer canonical snapshot.
        """
        if (
            not isinstance(seller_id, int)
            or isinstance(seller_id, bool)
            or seller_id <= 0
        ):
            raise ValueError("seller_id должен быть положительным целым числом")
        if not isinstance(supplier_product, SupplierProduct):
            raise ValueError("supplier_product должен быть SupplierProduct")
        return _copy_to_imported_product(seller_id, supplier_product)

    @staticmethod
    def import_to_seller(
        seller_id: int,
        supplier_product_ids: List[int],
        draft_account_ids: Optional[List[int]] = None,
    ) -> ImportResult:
        """
        Импортировать товары из базы поставщика к продавцу.
        Копирует данные в ImportedProduct и сохраняет связь с SupplierProduct.

        draft_account_ids — явно выбранные пользователем Ozon-кабинеты
        (чекбоксы «Каналы» в каталоге): для них дополнительно готовятся
        локальные черновики независимо от настроек авто-публикации.
        """
        result = ImportResult(total_requested=len(supplier_product_ids))

        seller = db.session.get(Seller, seller_id)
        if not seller:
            result.success = False
            result.error_messages.append("Продавец не найден")
            return result

        # Получаем товары поставщика (supplier нужен каждой копии — eager,
        # чтобы _copy_to_imported_product не делал SELECT на каждый товар)
        supplier_products = SupplierProduct.query.options(
            joinedload(SupplierProduct.supplier)
        ).filter(
            SupplierProduct.id.in_(supplier_product_ids)
        ).all()

        if not supplier_products:
            result.error_messages.append("Товары не найдены")
            return result

        # Проверяем, что продавец подключён к поставщику
        supplier_ids = set(sp.supplier_id for sp in supplier_products)
        for sup_id in supplier_ids:
            conn = SellerSupplier.query.filter_by(
                seller_id=seller_id, supplier_id=sup_id, is_active=True
            ).first()
            if not conn:
                result.success = False
                result.error_messages.append(f"Продавец не подключён к поставщику id={sup_id}")
                return result

        # Проверяем дубликаты
        existing_sp_ids = set()
        existing_imports = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.supplier_product_id.in_(supplier_product_ids)
        ).all()
        for imp in existing_imports:
            existing_sp_ids.add(imp.supplier_product_id)

        draft_source_ids = [
            imp.id for imp in existing_imports if imp.id is not None
        ]

        # Импортируем
        for sp in supplier_products:
            try:
                if sp.id in existing_sp_ids:
                    result.skipped += 1
                    continue

                imported = SupplierService.build_imported_product_from_supplier(
                    seller_id,
                    sp,
                )
                # Per-item savepoint keeps one malformed supplier row from
                # poisoning the whole exact seller import transaction. The
                # flush is required before local marketplace drafts can use
                # the durable ImportedProduct ID.
                with db.session.begin_nested():
                    db.session.add(imported)
                    db.session.flush()
                result.imported += 1
                result.imported_product_ids.append(imported.id)
                draft_source_ids.append(imported.id)

            except Exception as e:
                result.errors += 1
                result.error_messages.append(f"Товар {sp.external_id}: {str(e)[:100]}")

        # Обновляем статистику подключения
        db.session.flush()
        for sup_id in supplier_ids:
            conn = SellerSupplier.query.filter_by(
                seller_id=seller_id, supplier_id=sup_id
            ).first()
            if conn:
                conn.products_imported = ImportedProduct.query.filter_by(
                    seller_id=seller_id, supplier_id=sup_id
                ).count()
                conn.last_import_at = datetime.utcnow()

        db.session.commit()

        # ImportedProduct остаётся WB-compatible source draft, а каждый
        # включённый Ozon account получает отдельную локальную проекцию. Этот
        # шаг не вызывает provider/LLM и не меняет результат supplier import.
        if draft_source_ids:
            try:
                from services.marketplace_auto_publish import (
                    MarketplaceDraftProvisioner,
                )

                provisioned = MarketplaceDraftProvisioner.provision_in_chunks(
                    seller_id=seller_id,
                    imported_product_ids=sorted(set(draft_source_ids)),
                )
                result.marketplace_drafts_created = provisioned["created"]
                result.marketplace_drafts_existing = provisioned["existing"]
                result.marketplace_draft_errors = provisioned["failed"]
                if provisioned["failed"]:
                    result.error_messages.append(
                        "Не удалось подготовить часть Ozon-черновиков: "
                        f"{provisioned['failed']}"
                    )
            except Exception as exc:
                db.session.rollback()
                result.marketplace_draft_errors = len(draft_source_ids)
                result.error_messages.append(
                    "Supplier import завершён, но Ozon-черновики требуют повторной подготовки"
                )
                logger.warning(
                    "Не удалось provision Ozon drafts для seller %s (%s)",
                    seller_id,
                    type(exc).__name__,
                )

        # Явно выбранные пользователем Ozon-кабинеты — дополнительно к
        # авто-провижинингу выше и независимо от AutoPublishSettings.
        # Детерминированный локальный путь без provider/LLM; ошибка одного
        # кабинета не откатывает импорт и не блокирует остальные кабинеты.
        if draft_source_ids and draft_account_ids:
            from services.marketplace_drafts import (
                MarketplaceDraftError,
                MarketplaceDraftService,
            )

            source_ids = sorted(set(draft_source_ids))
            chunk_size = MarketplaceDraftService.BULK_PREPARE_MAX_PRODUCTS
            for account_id in dict.fromkeys(draft_account_ids):
                for offset in range(0, len(source_ids), chunk_size):
                    chunk = source_ids[offset:offset + chunk_size]
                    try:
                        prepared = MarketplaceDraftService.bulk_prepare(
                            seller_id=seller_id,
                            account_id=account_id,
                            imported_product_ids=chunk,
                            validate=False,
                        )
                        result.marketplace_drafts_created += prepared["created"]
                        result.marketplace_drafts_existing += prepared["existing"]
                        result.marketplace_draft_errors += prepared["failed"]
                    except MarketplaceDraftError as exc:
                        db.session.rollback()
                        result.marketplace_draft_errors += len(chunk)
                        result.error_messages.append(
                            f"Ozon-кабинет {account_id}: {str(exc)[:100]}"
                        )
                    except Exception as exc:
                        db.session.rollback()
                        result.marketplace_draft_errors += len(chunk)
                        result.error_messages.append(
                            "Ozon-черновики выбранного кабинета требуют повторной подготовки"
                        )
                        logger.warning(
                            "bulk_prepare для кабинета %s seller %s упал (%s)",
                            account_id,
                            seller_id,
                            type(exc).__name__,
                        )

        # Проверяем товары без фотографий и уведомляем продавца
        if result.imported > 0:
            try:
                _notify_products_without_photos(seller_id, result.imported_product_ids)
            except Exception as e:
                logger.warning(f"Ошибка при проверке фотографий: {e}")

        logger.info(
            f"Импорт к продавцу {seller_id}: "
            f"+{result.imported} / skip={result.skipped} / err={result.errors}"
        )
        return result

    @staticmethod
    def _begin_supplier_update_transaction() -> None:
        """Acquire SQLite's writer lock before exact revalidation/savepoints."""
        db.session.rollback()
        try:
            connection = db.session.connection()
            if connection.dialect.name == "sqlite":
                raw_connection = connection.connection.driver_connection
                if getattr(raw_connection, "in_transaction", False):
                    raise RuntimeError("supplier_update_transaction_not_fresh")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def _validate_imported_product_ids(seller_id, imported_product_ids):
        if (
            isinstance(seller_id, bool)
            or not isinstance(seller_id, int)
            or seller_id <= 0
        ):
            raise ValueError("Некорректный продавец")
        if not isinstance(imported_product_ids, list):
            raise ValueError("Ожидается точный список карточек")
        if len(imported_product_ids) > 200:
            raise ValueError("За один раз можно выбрать не более 200 карточек")
        if any(
            isinstance(product_id, bool)
            or not isinstance(product_id, int)
            or product_id <= 0
            or product_id > (1 << 63) - 1
            for product_id in imported_product_ids
        ):
            raise ValueError("Список карточек содержит некорректный ID")
        if len(set(imported_product_ids)) != len(imported_product_ids):
            raise ValueError("Список карточек содержит повторяющиеся ID")

    @staticmethod
    def _ordered_imported_products(seller_id, imported_product_ids):
        rows = ImportedProduct.query.options(
            selectinload(ImportedProduct.supplier_product)
        ).filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.id.in_(imported_product_ids),
        ).all()
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(imported_product_ids):
            raise ValueError("Часть выбранных карточек недоступна")
        return [by_id[product_id] for product_id in imported_product_ids]

    @staticmethod
    def _supplier_refresh_preview_for_rows(
        seller_id, imported_product_ids, rows, *, include_items=True,
    ):
        items = []
        digest_rows = []
        eligible = 0
        changed = 0
        skipped = 0
        for imported_product in rows:
            source = imported_product.supplier_product
            linked_source = source if _supplier_refresh_source_matches(imported_product) else None
            product_identity = {
                "id": imported_product.id,
                "seller_id": imported_product.seller_id,
                "product_id": imported_product.product_id,
                "supplier_id": imported_product.supplier_id,
                "supplier_product_id": imported_product.supplier_product_id,
                "external_id": imported_product.external_id,
                "external_vendor_code": imported_product.external_vendor_code,
                "source_type": imported_product.source_type,
                "import_status": imported_product.import_status,
                "created_at": _supplier_refresh_review_scalar(imported_product.created_at),
                "updated_at": _supplier_refresh_review_scalar(imported_product.updated_at),
                "content_edit_version": imported_product.content_edit_version,
            }
            product_before = _supplier_refresh_review_values(
                imported_product, SUPPLIER_REFRESH_REVIEW_PRODUCT_FIELDS,
            )
            source_identity = None
            if source is not None:
                source_identity = {
                    "id": source.id,
                    "supplier_id": source.supplier_id,
                    "external_id": source.external_id,
                    "vendor_code": source.vendor_code,
                }
            base = {
                "product_identity": product_identity,
                "product_before": product_before,
                "source_identity": source_identity,
            }
            if linked_source is None:
                skipped += 1
                if include_items:
                    items.append({
                        "product_id": imported_product.id,
                        "label": supplier_refresh_preview_label(imported_product),
                        "state": "unlinked",
                        "changes": [],
                    })
                digest_rows.append({
                    **base,
                    "state": "unlinked",
                    "source": None,
                    "projected": None,
                })
                continue

            eligible += 1
            projected = _supplier_refresh_clone(imported_product)
            try:
                _update_imported_from_supplier(projected, linked_source)
            except Exception:
                raise ValueError("Не удалось проверить данные выбранных карточек") from None
            projected_values = _supplier_refresh_review_values(
                projected, SUPPLIER_REFRESH_REVIEW_PRODUCT_FIELDS,
            )
            row_changed = product_before != projected_values
            if row_changed:
                changed += 1
            if include_items:
                try:
                    display_changes = _supplier_refresh_preview_changes(
                        imported_product, projected,
                    )
                except Exception:
                    raise ValueError("Не удалось проверить данные выбранных карточек") from None
                if row_changed and not display_changes:
                    display_changes = [{
                        "field": "product_details",
                        "before": None,
                        "after": None,
                    }]
                items.append({
                    "product_id": imported_product.id,
                    "label": supplier_refresh_preview_label(imported_product),
                    "state": "changed" if row_changed else "unchanged",
                    "changes": display_changes,
                })
            digest_rows.append({
                **base,
                "state": "linked",
                "source": _supplier_refresh_review_values(
                    linked_source, SUPPLIER_REFRESH_REVIEW_SOURCE_FIELDS,
                ),
                "projected": projected_values,
            })

        digest = _supplier_refresh_digest({
            "v": 1,
            "seller_id": seller_id,
            "ids": imported_product_ids,
            "rows": digest_rows,
        })
        return {
            "requested": len(imported_product_ids),
            "eligible": eligible,
            "changed": changed,
            "skipped": skipped,
            "items": items,
            "review_digest": digest,
        }

    @staticmethod
    def preview_imported_products(seller_id, imported_product_ids):
        """Read-only exact supplier refresh preview and private review digest."""
        SupplierService._validate_imported_product_ids(seller_id, imported_product_ids)
        if not imported_product_ids:
            raise ValueError("Не выбраны товары")
        rows = SupplierService._ordered_imported_products(
            seller_id, imported_product_ids,
        )
        return SupplierService._supplier_refresh_preview_for_rows(
            seller_id, imported_product_ids, rows,
        )

    @staticmethod
    def _supplier_catalog_refresh_connection_values(connection):
        """Include access identity and settings that can invalidate a review."""
        return {
            "id": connection.id,
            "seller_id": connection.seller_id,
            "supplier_id": connection.supplier_id,
            "is_active": bool(connection.is_active),
            "supplier_code": connection.supplier_code,
            "vendor_code_pattern": connection.vendor_code_pattern,
            "custom_markup_percent": _supplier_refresh_review_scalar(
                connection.custom_markup_percent,
            ),
            "connected_at": _supplier_refresh_review_scalar(connection.connected_at),
            "updated_at": _supplier_refresh_review_scalar(connection.updated_at),
        }

    @staticmethod
    def preview_supplier_products(
        seller_id: int,
        supplier_id: int,
        supplier_product_ids: Optional[List[int]] = None,
        *,
        details_limit: int = 50,
    ) -> dict:
        """Preview a source-ID selection or all local copies for one supplier.

        ``None`` is the explicit all-owned-imports scope. A list means the
        exact ordered SupplierProduct IDs selected in the supplier catalog;
        every seller-owned ImportedProduct copy linked to those source rows is
        reviewed. The digest streams the full scope independently of the
        bounded ``items`` sample so a route can sign it without returning a
        large response body.
        """
        if (
            isinstance(seller_id, bool)
            or not isinstance(seller_id, int)
            or seller_id <= 0
        ):
            raise ValueError("Некорректный продавец")
        if (
            isinstance(supplier_id, bool)
            or not isinstance(supplier_id, int)
            or supplier_id <= 0
        ):
            raise ValueError("Некорректный поставщик")
        if (
            isinstance(details_limit, bool)
            or not isinstance(details_limit, int)
            or details_limit < 0
            or details_limit > 200
        ):
            raise ValueError("Некорректный размер списка изменений")

        if supplier_product_ids is not None:
            if not isinstance(supplier_product_ids, list):
                raise ValueError("Ожидается точный список товаров поставщика")
            if len(supplier_product_ids) > 200:
                raise ValueError("За один раз можно выбрать не более 200 товаров")
            if any(
                isinstance(product_id, bool)
                or not isinstance(product_id, int)
                or product_id <= 0
                or product_id > (1 << 63) - 1
                for product_id in supplier_product_ids
            ):
                raise ValueError("Список товаров поставщика содержит некорректный ID")
            if len(set(supplier_product_ids)) != len(supplier_product_ids):
                raise ValueError("Список товаров поставщика содержит повторы")
            if not supplier_product_ids:
                raise ValueError("Не выбраны товары")

        connection = SellerSupplier.query.filter_by(
            seller_id=seller_id,
            supplier_id=supplier_id,
            is_active=True,
        ).first()
        if connection is None:
            raise ValueError("Нет активного доступа к поставщику")

        if supplier_product_ids is None:
            rows_query = ImportedProduct.query.options(
                selectinload(ImportedProduct.supplier_product)
            ).filter_by(
                seller_id=seller_id,
                supplier_id=supplier_id,
            ).order_by(ImportedProduct.id)
            scope = {"kind": "all"}
            source_requested = None
        else:
            source_rows = SupplierProduct.query.filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.id.in_(supplier_product_ids),
            ).all()
            if {row.id for row in source_rows} != set(supplier_product_ids):
                raise ValueError("Часть товаров не относится к этому поставщику")
            rows_query = ImportedProduct.query.options(
                selectinload(ImportedProduct.supplier_product)
            ).filter(
                ImportedProduct.seller_id == seller_id,
                ImportedProduct.supplier_product_id.in_(supplier_product_ids),
            ).order_by(ImportedProduct.id)
            scope = {
                "kind": "source_ids",
                "supplier_product_ids": list(supplier_product_ids),
            }
            source_requested = len(supplier_product_ids)

        digest = _supplier_catalog_refresh_review_hasher({
            "v": 1,
            "seller_id": seller_id,
            "supplier_id": supplier_id,
            "scope": scope,
            "connection": SupplierService._supplier_catalog_refresh_connection_values(
                connection,
            ),
        })
        requested = 0
        eligible = 0
        changed = 0
        skipped = 0
        items = []
        found_source_ids = set()

        for imported_product in rows_query.yield_per(100):
            if imported_product.supplier_product_id is not None:
                found_source_ids.add(imported_product.supplier_product_id)
            source = imported_product.supplier_product
            if (
                source is not None
                and source.supplier_id != supplier_id
            ):
                raise ValueError("Связь карточки с поставщиком не совпадает")
            if supplier_product_ids is not None and (
                imported_product.supplier_id != supplier_id
                or source is None
                or source.supplier_id != supplier_id
            ):
                raise ValueError("Связь карточки с поставщиком не совпадает")

            include_item = len(items) < details_limit
            try:
                row_review = SupplierService._supplier_refresh_preview_for_rows(
                    seller_id,
                    [imported_product.id],
                    [imported_product],
                    include_items=include_item,
                )
            except ValueError:
                raise
            except Exception:
                raise ValueError("Не удалось проверить данные выбранных карточек") from None

            requested += row_review["requested"]
            eligible += row_review["eligible"]
            changed += row_review["changed"]
            skipped += row_review["skipped"]
            if include_item:
                items.extend(row_review["items"])
            _supplier_catalog_refresh_review_add_row(
                digest,
                imported_product.id,
                row_review["review_digest"],
            )

        if supplier_product_ids is not None:
            if not found_source_ids:
                raise ValueError("Выбранные товары не импортированы в ваши карточки")
            if found_source_ids != set(supplier_product_ids):
                raise ValueError(
                    "Часть выбранных товаров ещё не импортирована в ваши карточки"
                )

        review_digest = _supplier_catalog_refresh_review_finish(digest, requested)
        return {
            "scope": scope["kind"],
            "supplier_id": supplier_id,
            "source_requested": source_requested,
            "requested": requested,
            "eligible": eligible,
            "changed": changed,
            "skipped": skipped,
            "items": items,
            "items_truncated": requested > len(items),
            "details_limit": details_limit,
            "review_digest": review_digest,
        }

    @staticmethod
    def _apply_supplier_updates(
        imported_products: List[ImportedProduct], *, require_matching_supplier=False,
    ) -> ImportResult:
        """Обновить exact rows с savepoint на строку."""
        result = ImportResult(total_requested=len(imported_products))

        for imp in imported_products:
            if not imp.supplier_product_id or not imp.supplier_product or (
                require_matching_supplier and not _supplier_refresh_source_matches(imp)
            ):
                result.skipped += 1
                result.item_results.append({
                    "product_id": imp.id,
                    "status": "skipped",
                    "reason": "no_supplier_link",
                })
                continue

            try:
                with db.session.begin_nested():
                    _update_imported_from_supplier(imp, imp.supplier_product)
                result.imported += 1
                result.item_results.append({
                    "product_id": imp.id,
                    "status": "updated",
                    "reason": None,
                })
            except Exception as exc:
                result.errors += 1
                result.error_messages.append("Не удалось обновить одну карточку")
                result.item_results.append({
                    "product_id": imp.id,
                    "status": "error",
                    "reason": "update_failed",
                })
                logger.warning(
                    "Supplier refresh row failed for ImportedProduct %s (%s)",
                    imp.id,
                    type(exc).__name__,
                )

        try:
            db.session.commit()
        except Exception:
            # This method owns the batch commit boundary. A failed commit must
            # release any writer lock and discard its uncommitted row updates.
            db.session.rollback()
            raise

        logger.info(
            "Обновление карточек из поставщика: requested=%s updated=%s "
            "skipped=%s errors=%s",
            result.total_requested,
            result.imported,
            result.skipped,
            result.errors,
        )
        return result

    @staticmethod
    def update_imported_products(
        seller_id: int,
        imported_product_ids: List[int],
        *,
        expected_review_digest: Optional[str] = None,
    ) -> ImportResult:
        """Apply an exact seller-owned refresh, optionally under reviewed state."""
        SupplierService._validate_imported_product_ids(
            seller_id, imported_product_ids,
        )
        if not imported_product_ids:
            return ImportResult()
        if expected_review_digest is not None and (
            not isinstance(expected_review_digest, str)
            or len(expected_review_digest) != 64
            or any(char not in "0123456789abcdef" for char in expected_review_digest)
        ):
            raise ValueError("Проверка обновления недействительна")

        SupplierService._begin_supplier_update_transaction()
        try:
            try:
                ordered_rows = SupplierService._ordered_imported_products(
                    seller_id, imported_product_ids,
                )
            except ValueError:
                if expected_review_digest is not None:
                    raise SupplierRefreshReviewStale(
                        "Выбранные данные изменились после просмотра"
                    ) from None
                raise
            if expected_review_digest is not None:
                current_review = SupplierService._supplier_refresh_preview_for_rows(
                    seller_id, imported_product_ids, ordered_rows,
                )
                if not hmac.compare_digest(
                    current_review["review_digest"], expected_review_digest,
                ):
                    raise SupplierRefreshReviewStale(
                        "Выбранные данные изменились после просмотра"
                    )
            return SupplierService._apply_supplier_updates(
                ordered_rows,
                require_matching_supplier=expected_review_digest is not None,
            )
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def update_supplier_products(
        seller_id: int,
        supplier_id: int,
        supplier_product_ids: Optional[List[int]] = None,
        *,
        expected_review_digest: Optional[str] = None,
    ) -> ImportResult:
        """Обновить выбранные товары или explicit all одного поставщика.

        ``None`` зарезервирован для явного режима «весь каталог» и всегда
        ограничен текущими seller + active supplier. Пустой список означает
        ноль выбранных товаров. When a review digest is supplied, the exact
        source scope, active connection, all mapped local rows and their
        projections are recomputed under the writer transaction before apply.
        """
        if (
            isinstance(supplier_id, bool)
            or not isinstance(supplier_id, int)
            or supplier_id <= 0
        ):
            raise ValueError("Некорректный поставщик")
        if (
            isinstance(seller_id, bool)
            or not isinstance(seller_id, int)
            or seller_id <= 0
        ):
            raise ValueError("Некорректный продавец")
        reviewed = expected_review_digest is not None
        if reviewed and (
            not isinstance(expected_review_digest, str)
            or len(expected_review_digest) != 64
            or any(char not in "0123456789abcdef" for char in expected_review_digest)
        ):
            raise ValueError("Проверка обновления недействительна")
        if supplier_product_ids is not None:
            if not isinstance(supplier_product_ids, list):
                raise ValueError("Ожидается точный список товаров поставщика")
            if len(supplier_product_ids) > 200:
                raise ValueError("За один раз можно выбрать не более 200 товаров")
            if any(
                isinstance(product_id, bool)
                or not isinstance(product_id, int)
                or product_id <= 0
                for product_id in supplier_product_ids
            ):
                raise ValueError("Список товаров поставщика содержит некорректный ID")
            if len(set(supplier_product_ids)) != len(supplier_product_ids):
                raise ValueError("Список товаров поставщика содержит повторы")
            if not supplier_product_ids:
                if reviewed:
                    raise ValueError("Не выбраны товары")
                return ImportResult()

        SupplierService._begin_supplier_update_transaction()
        try:
            active_connection = SellerSupplier.query.filter_by(
                seller_id=seller_id,
                supplier_id=supplier_id,
                is_active=True,
            ).first()
            if not active_connection:
                if reviewed:
                    raise SupplierRefreshReviewStale(
                        "Доступ к поставщику изменился после просмотра"
                    )
                raise ValueError("Нет активного доступа к поставщику")

            if supplier_product_ids is None:
                imported_rows = ImportedProduct.query.options(
                    selectinload(ImportedProduct.supplier_product)
                ).filter_by(
                    seller_id=seller_id,
                    supplier_id=supplier_id,
                ).order_by(ImportedProduct.id).all()
                if any(
                    row.supplier_product is not None
                    and row.supplier_product.supplier_id != supplier_id
                    for row in imported_rows
                ):
                    if reviewed:
                        raise SupplierRefreshReviewStale(
                            "Связь с поставщиком изменилась после просмотра"
                        )
                    raise ValueError("Связь карточки с поставщиком не совпадает")
                if reviewed:
                    current_review = SupplierService.preview_supplier_products(
                        seller_id,
                        supplier_id,
                        None,
                        details_limit=0,
                    )
                    if not hmac.compare_digest(
                        current_review["review_digest"], expected_review_digest,
                    ):
                        raise SupplierRefreshReviewStale(
                            "Выбранные данные изменились после просмотра"
                        )
                return SupplierService._apply_supplier_updates(imported_rows)

            source_rows = SupplierProduct.query.filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.id.in_(supplier_product_ids),
            ).all()
            if {row.id for row in source_rows} != set(supplier_product_ids):
                if reviewed:
                    raise SupplierRefreshReviewStale(
                        "Выбранные товары поставщика изменились после просмотра"
                    )
                raise ValueError("Часть товаров не относится к этому поставщику")

            imported_rows = ImportedProduct.query.options(
                selectinload(ImportedProduct.supplier_product)
            ).filter(
                ImportedProduct.seller_id == seller_id,
                ImportedProduct.supplier_product_id.in_(supplier_product_ids),
            ).order_by(ImportedProduct.id).all()
            if not imported_rows:
                if reviewed:
                    raise SupplierRefreshReviewStale(
                        "Выбранные карточки изменились после просмотра"
                    )
                raise ValueError("Выбранные товары не импортированы в ваши карточки")
            if any(row.supplier_id != supplier_id for row in imported_rows):
                if reviewed:
                    raise SupplierRefreshReviewStale(
                        "Связь с поставщиком изменилась после просмотра"
                    )
                raise ValueError("Связь карточки с поставщиком не совпадает")
            if any(
                row.supplier_product is None
                or row.supplier_product.supplier_id != supplier_id
                for row in imported_rows
            ):
                if reviewed:
                    raise SupplierRefreshReviewStale(
                        "Связь с поставщиком изменилась после просмотра"
                    )
                raise ValueError("Связь карточки с поставщиком не совпадает")
            found_source_ids = {
                row.supplier_product_id for row in imported_rows
            }
            if found_source_ids != set(supplier_product_ids):
                if reviewed:
                    raise SupplierRefreshReviewStale(
                        "Связанные карточки изменились после просмотра"
                    )
                raise ValueError(
                    "Часть выбранных товаров ещё не импортирована в ваши карточки"
                )
            if len(imported_rows) > 200 and not reviewed:
                raise ValueError("Слишком много локальных копий для выбранного набора")

            if reviewed:
                current_review = SupplierService.preview_supplier_products(
                    seller_id,
                    supplier_id,
                    supplier_product_ids,
                    details_limit=0,
                )
                if not hmac.compare_digest(
                    current_review["review_digest"], expected_review_digest,
                ):
                    raise SupplierRefreshReviewStale(
                        "Выбранные данные изменились после просмотра"
                    )

            return SupplierService._apply_supplier_updates(imported_rows)
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def update_seller_products(seller_id: int,
                               supplier_product_ids: List[int] = None) -> ImportResult:
        """
        Обновить данные у продавца из базы поставщика.
        Обновляет ImportedProduct по связанному SupplierProduct.
        """
        if supplier_product_ids is not None:
            # None is the long-standing explicit service-level "all" mode.
            # An empty list is an exact empty selection and must not widen it.
            if not supplier_product_ids:
                return ImportResult()

        SupplierService._begin_supplier_update_transaction()
        try:
            q = ImportedProduct.query.options(
                selectinload(ImportedProduct.supplier_product)
            ).filter(
                ImportedProduct.seller_id == seller_id,
                ImportedProduct.supplier_product_id.isnot(None)
            )
            if supplier_product_ids is not None:
                q = q.filter(
                    ImportedProduct.supplier_product_id.in_(supplier_product_ids)
                )
            imported_products = q.all()
            return SupplierService._apply_supplier_updates(imported_products)
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def get_available_products_for_seller(seller_id: int, supplier_id: int,
                                          page: int = 1, per_page: int = 50,
                                          search: str = None,
                                          show_imported: bool = False,
                                          stock_status: str = None,
                                          wb_filter: str = None,
                                          ai_filter: str = None,
                                          updates_only: bool = False,
                                          wb_existing_sp_ids: set = None):
        """Товары поставщика, доступные для импорта продавцу

        stock_status: 'in_stock' | 'out_of_stock' | None (все)
        wb_filter: 'on_wb' | 'not_on_wb' | None (все)
        ai_filter: 'ai_parsed' | 'not_parsed' | None (все)
        wb_existing_sp_ids: pre-computed set of supplier_product IDs already on WB
                           (includes both ImportedProduct links and vendor_code matches)
        """
        q = SupplierProduct.query.filter_by(supplier_id=supplier_id)
        q = q.filter(SupplierProduct.status.in_(['draft', 'validated', 'ready']))

        if updates_only:
            stale_supplier_copy_exists = db.session.query(
                ImportedProduct.id
            ).filter(
                ImportedProduct.seller_id == seller_id,
                ImportedProduct.supplier_product_id == SupplierProduct.id,
                ImportedProduct.supplier_content_revision
                < SupplierProduct.content_revision,
            ).exists()
            q = q.filter(stale_supplier_copy_exists)

        # Фильтр по статусу на WB (uses pre-computed set including vendor_code matches)
        if wb_filter in ('on_wb', 'not_on_wb'):
            if wb_existing_sp_ids:
                if wb_filter == 'on_wb':
                    q = q.filter(SupplierProduct.id.in_(wb_existing_sp_ids))
                else:
                    q = q.filter(~SupplierProduct.id.in_(wb_existing_sp_ids))
            else:
                # Fallback if no pre-computed set
                wb_sp_subq = db.session.query(ImportedProduct.supplier_product_id).filter(
                    ImportedProduct.seller_id == seller_id,
                    ImportedProduct.supplier_product_id.isnot(None),
                    ImportedProduct.product_id.isnot(None)
                ).subquery()
                if wb_filter == 'on_wb':
                    q = q.filter(SupplierProduct.id.in_(wb_sp_subq))
                else:
                    q = q.filter(~SupplierProduct.id.in_(wb_sp_subq))
        elif not show_imported and not updates_only:
            # Исключаем уже импортированные
            imported_sp_ids = db.session.query(ImportedProduct.supplier_product_id).filter(
                ImportedProduct.seller_id == seller_id,
                ImportedProduct.supplier_product_id.isnot(None)
            ).subquery()
            q = q.filter(~SupplierProduct.id.in_(imported_sp_ids))

        # Фильтр по наличию
        if stock_status == 'in_stock':
            q = q.filter(SupplierProduct.supplier_quantity.isnot(None),
                         SupplierProduct.supplier_quantity > 0)
        elif stock_status == 'out_of_stock':
            q = q.filter(db.or_(
                SupplierProduct.supplier_quantity.is_(None),
                SupplierProduct.supplier_quantity == 0))

        # Фильтр по AI-парсингу
        if ai_filter == 'ai_parsed':
            q = q.filter(SupplierProduct.ai_parsed_at.isnot(None))
        elif ai_filter == 'not_parsed':
            q = q.filter(SupplierProduct.ai_parsed_at.is_(None))

        if search:
            search_term = f"%{search}%"
            q = q.filter(
                db.or_(
                    SupplierProduct.title.ilike(search_term),
                    SupplierProduct.external_id.ilike(search_term),
                    SupplierProduct.brand.ilike(search_term),
                    SupplierProduct.vendor_code.ilike(search_term),
                    SupplierProduct.barcode.ilike(search_term),
                    SupplierProduct.additional_vendor_code.ilike(search_term),
                    SupplierProduct.category.ilike(search_term),
                    SupplierProduct.wb_category_name.ilike(search_term),
                    SupplierProduct.wb_subject_name.ilike(search_term),
                )
            )

        # Сначала товары в наличии и с заполненными характеристиками,
        # внутри групп — по бренду и названию
        in_stock = db.case(
            (SupplierProduct.supplier_quantity > 0, 1), else_=0,
        )
        enriched = db.case(
            (SupplierProduct.ai_marketplace_json.isnot(None), 1), else_=0,
        )
        return q.order_by(
            in_stock.desc(),
            enriched.desc(),
            db.nullslast(SupplierProduct.brand.asc()),
            SupplierProduct.title.asc(),
        ).paginate(
            page=page, per_page=per_page, error_out=False
        )

    # ===================================================================
    # AI ОПЕРАЦИИ НА УРОВНЕ ПОСТАВЩИКА
    # ===================================================================

    @staticmethod
    def _get_ai_service(supplier: Supplier, model_override: str = None,
                        task_profile: str = None):
        """Создать AIService из настроек поставщика"""
        from services.ai_service import AIConfig, AIService as AISvc
        if task_profile == 'supplier_parsing_flash':
            config = AIConfig.for_supplier_parsing(supplier, model_override)
        elif task_profile is None:
            config = AIConfig.from_settings(supplier)
        else:
            raise ValueError('Неизвестный профиль AI-задачи.')
        if not config:
            return None
        if model_override and task_profile is None:
            config.model = model_override
        return AISvc(config)

    @staticmethod
    def ai_validate_product(product_id: int) -> dict:
        """
        AI валидация одного товара поставщика.
        Запускает analyze_card и обновляет ai_validation_score, ai_validated.

        Returns:
            dict с ключами: success, score, issues, recommendations, error
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен для этого поставщика'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис (проверьте API ключ)'}

        photos = product.get_photos() if hasattr(product, 'get_photos') else []

        success, result, error = ai_svc.analyze_card(
            title=product.title or '',
            description=product.description or '',
            category=product.wb_category_name or product.category or '',
            photos_count=len(photos),
            price=product.supplier_price or 0
        )

        if success and result:
            score = result.get('score', 0)
            product.ai_analysis_json = json.dumps(result, ensure_ascii=False)
            product.ai_validation_score = score
            product.ai_validated = True
            product.ai_validated_at = datetime.utcnow()
            if score >= 70:
                product.status = 'validated'
            db.session.commit()
            return {
                'success': True,
                'score': score,
                'issues': result.get('issues', []),
                'recommendations': result.get('recommendations', []),
                'strengths': result.get('strengths', []),
            }

        return {'success': False, 'error': error or 'Ошибка AI анализа'}

    @staticmethod
    def ai_validate_bulk(supplier_id: int, product_ids: List[int]) -> dict:
        """
        AI валидация нескольких товаров.

        Returns:
            dict: {success, validated, errors, results: [{product_id, score, error}]}
        """
        supplier = Supplier.query.get(supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен', 'validated': 0, 'errors': 0, 'results': []}

        results = []
        validated = 0
        errors = 0

        for pid in product_ids:
            res = SupplierService.ai_validate_product(pid)
            results.append({
                'product_id': pid,
                'score': res.get('score'),
                'error': res.get('error'),
                'success': res.get('success', False),
            })
            if res.get('success'):
                validated += 1
            else:
                errors += 1

        return {
            'success': True,
            'validated': validated,
            'errors': errors,
            'results': results,
        }

    @staticmethod
    def ai_generate_seo(product_id: int) -> dict:
        """
        AI генерация SEO заголовка для товара.

        Returns:
            dict: {success, title, keywords_used, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис'}

        success, result, error = ai_svc.generate_seo_title(
            title=product.title or '',
            category=product.wb_category_name or product.category or '',
            brand=product.brand or '',
            description=product.description or ''
        )

        if success and result:
            title = _sanitize_wb_text(result.get('title', ''))
            product.ai_seo_title = title
            product.updated_at = datetime.utcnow()
            db.session.commit()
            return {'success': True, 'title': title, 'keywords_used': result.get('keywords_used', [])}

        return {'success': False, 'error': error or 'Ошибка AI'}

    @staticmethod
    def ai_generate_description(product_id: int) -> dict:
        """
        AI генерация описания для товара.

        Returns:
            dict: {success, description, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис'}

        success, result, error = ai_svc.enhance_description(
            title=product.title or '',
            description=product.description or '',
            category=product.wb_category_name or product.category or ''
        )

        if success and result:
            desc = _sanitize_wb_text(result.get('description', ''))
            product.ai_description = desc
            product.updated_at = datetime.utcnow()
            db.session.commit()
            return {'success': True, 'description': desc}

        return {'success': False, 'error': error or 'Ошибка AI'}

    @staticmethod
    def ai_generate_keywords(product_id: int) -> dict:
        """
        AI генерация ключевых слов для товара.

        Returns:
            dict: {success, keywords, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис'}

        success, result, error = ai_svc.generate_keywords(
            title=product.title or '',
            category=product.wb_category_name or product.category or '',
            description=product.description or ''
        )

        if success and result:
            product.ai_keywords_json = json.dumps(result.get('keywords', []), ensure_ascii=False)
            product.updated_at = datetime.utcnow()
            db.session.commit()
            return {'success': True, 'keywords': result.get('keywords', [])}

        return {'success': False, 'error': error or 'Ошибка AI'}

    @staticmethod
    def ai_analyze_product(product_id: int) -> dict:
        """
        Комплексный AI анализ товара (без изменения статуса валидации).

        Returns:
            dict: {success, score, issues, recommendations, strengths, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис'}

        photos = product.get_photos() if hasattr(product, 'get_photos') else []

        success, result, error = ai_svc.analyze_card(
            title=product.title or '',
            description=product.description or '',
            category=product.wb_category_name or product.category or '',
            photos_count=len(photos),
            price=product.supplier_price or 0
        )

        if success and result:
            product.ai_analysis_json = json.dumps(result, ensure_ascii=False)
            product.updated_at = datetime.utcnow()
            db.session.commit()
            return {
                'success': True,
                'score': result.get('score', 0),
                'issues': result.get('issues', []),
                'recommendations': result.get('recommendations', []),
                'strengths': result.get('strengths', []),
            }

        return {'success': False, 'error': error or 'Ошибка AI'}

    @staticmethod
    def ai_full_enrich(product_id: int) -> dict:
        """
        Полное AI обогащение товара: SEO + описание + ключевые слова + анализ.

        Returns:
            dict: {success, seo_title, description, keywords, score, errors}
        """
        errors = []
        result_data = {}

        seo_res = SupplierService.ai_generate_seo(product_id)
        if seo_res.get('success'):
            result_data['seo_title'] = seo_res.get('title', '')
        else:
            errors.append(f"SEO: {seo_res.get('error', '?')}")

        desc_res = SupplierService.ai_generate_description(product_id)
        if desc_res.get('success'):
            result_data['description'] = desc_res.get('description', '')
        else:
            errors.append(f"Описание: {desc_res.get('error', '?')}")

        kw_res = SupplierService.ai_generate_keywords(product_id)
        if kw_res.get('success'):
            result_data['keywords'] = kw_res.get('keywords', [])
        else:
            errors.append(f"Ключевые слова: {kw_res.get('error', '?')}")

        val_res = SupplierService.ai_validate_product(product_id)
        if val_res.get('success'):
            result_data['score'] = val_res.get('score', 0)
        else:
            errors.append(f"Анализ: {val_res.get('error', '?')}")

        return {
            'success': len(errors) == 0,
            'errors': errors,
            **result_data,
        }

    # ===================================================================
    # AI RICH КОНТЕНТ (инфографика)
    # ===================================================================

    @staticmethod
    def ai_generate_rich_content(product_id: int) -> dict:
        """
        Fact-safe сборка Rich-контента для товара.

        Метод сохраняет legacy-имя API, но production copy не генерируется
        моделью: на слайды попадают только дословные поля товара с provenance.

        Returns:
            dict: {success, data, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        # Получаем характеристики
        characteristics = {}
        if product.characteristics_json:
            try:
                characteristics = json.loads(product.characteristics_json) if isinstance(product.characteristics_json, str) else product.characteristics_json
            except (TypeError, ValueError):
                characteristics = {}
        if isinstance(characteristics, list):
            mapped = {}
            for item in characteristics:
                if not isinstance(item, dict):
                    continue
                name = item.get('name') or item.get('title')
                value = item.get('value')
                if isinstance(name, str) and name.strip() and value is not None:
                    mapped[name.strip()] = value
            characteristics = mapped
        elif not isinstance(characteristics, dict):
            characteristics = {}

        try:
            from services.infographic_content import (
                build_fact_pack,
                build_fact_safe_rich_content,
            )

            fact_pack = build_fact_pack(
                title=product.title or '',
                category=product.wb_category_name or product.category or '',
                brand=product.brand or '',
                characteristics=characteristics,
            )
            result = build_fact_safe_rich_content(fact_pack)
            product.ai_rich_content_json = json.dumps(result, ensure_ascii=False)
            product.updated_at = datetime.utcnow()
            db.session.commit()
            return {
                'success': True,
                'data': result,
                'generator': 'fact_safe_v1',
            }
        except (TypeError, ValueError) as exc:
            db.session.rollback()
            return {'success': False, 'error': str(exc)}

    @staticmethod
    def ai_render_infographic(product_id: int, slide_index=None) -> dict:
        """Compatibility wrapper over the production-safe hybrid renderer."""
        result = SupplierService.ai_render_hybrid_infographic(product_id)
        if slide_index is not None and result.get('results'):
            selected = [
                item for item in result['results']
                if item.get('slide_number') == slide_index + 1
            ]
            result['results'] = selected
            result['total_slides'] = len(selected)
            result['successful'] = sum(1 for item in selected if item.get('success'))
            result['failed'] = len(selected) - result['successful']
        return result

    @staticmethod
    def ai_render_infographic_preview(product_id: int, slide_index: int = 0, preview_width: int = 720) -> dict:
        """Return a resized preview produced by the safe hybrid path."""
        import base64 as b64module
        import io as io_module

        result = SupplierService.ai_render_hybrid_infographic(product_id)
        if not result.get('success'):
            return result
        matches = [
            item for item in result.get('results', [])
            if item.get('slide_number') == slide_index + 1
        ]
        if not matches or not matches[0].get('image_base64'):
            error = matches[0].get('error') if matches else f'Слайд {slide_index} не найден'
            return {'success': False, 'error': error}
        try:
            from PIL import Image

            width = max(240, min(int(preview_width), 900))
            image = Image.open(io_module.BytesIO(
                b64module.b64decode(matches[0]['image_base64']))).convert('RGB')
            height = round(image.height * width / image.width)
            image = image.resize((width, height), Image.Resampling.LANCZOS)
            output = io_module.BytesIO()
            image.save(output, format='JPEG', quality=82)
            return {
                'success': True,
                'slide_index': slide_index,
                'slide_type': matches[0].get('slide_type', 'unknown'),
                'preview_base64': b64module.b64encode(output.getvalue()).decode('ascii'),
                'renderer': matches[0].get('renderer', 'hybrid'),
                'publishable': matches[0].get('publishable', False),
                'quality': matches[0].get('quality', {}),
            }
        except (TypeError, ValueError, OSError) as exc:
            return {'success': False, 'error': str(exc)}

    @staticmethod
    def ai_render_hybrid_infographic(product_id: int) -> dict:
        """
        Гибридный рендер инфографики: AI-фон + Playwright текст/фото.
        Требует настроенный image_gen провайдер на поставщике.

        Returns:
            dict: {success, total_slides, successful, results, error}
        """
        import base64 as b64module
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        if not product.ai_rich_content_json:
            return {'success': False, 'error': 'Сначала сгенерируйте Rich-контент'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier:
            return {'success': False, 'error': 'Поставщик не найден'}

        # Получаем image service. Без ключа renderer использует безопасный
        # детерминированный фон; foreground и текст всё равно не уходят в модель.
        from services.image_generation_service import create_image_service
        img_service = create_image_service(supplier)

        try:
            rich_content = json.loads(product.ai_rich_content_json)

            product_photos = []
            if product.photo_urls_json:
                try:
                    product_photos = json.loads(product.photo_urls_json) if isinstance(product.photo_urls_json, str) else product.photo_urls_json
                except Exception:
                    pass

            from services.infographic_renderer import render_hybrid_slides

            results = render_hybrid_slides(
                rich_content=rich_content,
                image_service=img_service,
                product_photos=product_photos,
                product_title=product.title or '',
                supplier_product_id=product_id
            )

            output = []
            for r in results:
                item = {
                    'slide_number': r.get('slide_number', 0),
                    'slide_type': r.get('slide_type', 'unknown'),
                    'success': r['success'],
                    'error': r.get('error', ''),
                    'renderer': r.get('renderer', 'hybrid'),
                    'has_ai_bg': r.get('has_ai_bg', False),
                    'publishable': r.get('publishable', False),
                    'quality': r.get('quality', {}),
                    'background_note': r.get('background_note', ''),
                }
                if r['success'] and r.get('image_bytes'):
                    item['image_base64'] = b64module.b64encode(r['image_bytes']).decode('utf-8')
                    item['image_size'] = r.get('image_size', len(r['image_bytes']))
                output.append(item)

            successful = sum(1 for r in results if r['success'])
            return {
                'success': True,
                'total_slides': len(rich_content.get('slides', [])),
                'successful': successful,
                'failed': len(rich_content.get('slides', [])) - successful,
                'results': output,
                'renderer': 'hybrid'
            }

        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Hybrid render error: {e}")
            return {'success': False, 'error': str(e)}

    # ===================================================================
    # СИНХРОНИЗАЦИЯ ОПИСАНИЙ ИЗ ВНЕШНЕГО CSV
    # ===================================================================

    @staticmethod
    def sync_descriptions(supplier_id: int) -> dict:
        """
        Синхронизация описаний товаров из отдельного CSV файла поставщика.

        Формат CSV: id;описание (или с заголовками)
        Привязка по external_id (числовой ID).

        Returns:
            dict: {success, updated, not_found, errors, error_messages}
        """
        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return {'success': False, 'error': 'Поставщик не найден'}
        if not supplier.description_file_url:
            return {'success': False, 'error': 'URL файла описаний не задан'}

        delimiter = supplier.description_file_delimiter or ';'
        encoding = supplier.description_file_encoding or 'cp1251'

        try:
            resp = requests.get(supplier.description_file_url, timeout=60)
            resp.raise_for_status()
            content = resp.content.decode(encoding, errors='replace')
        except Exception as e:
            supplier.last_description_sync_status = 'failed'
            db.session.commit()
            return {'success': False, 'error': f'Ошибка загрузки: {e}'}

        updated = 0
        not_found = 0
        errors = 0
        error_messages = []

        reader = csv.reader(StringIO(content), delimiter=delimiter, quotechar='"')
        for row_num, row in enumerate(reader, 1):
            try:
                if len(row) < 2:
                    continue

                product_id_raw = row[0].strip()
                description = row[1].strip() if len(row) > 1 else ''

                if not product_id_raw or not description:
                    continue

                # Ищем товар по external_id или числовому ID
                product = SupplierProduct.query.filter(
                    SupplierProduct.supplier_id == supplier_id,
                    db.or_(
                        SupplierProduct.external_id == product_id_raw,
                        SupplierProduct.external_id.like('%' + str(product_id_raw).replace('%', '').replace('_', '') + '%')
                    )
                ).first()

                if not product:
                    not_found += 1
                    continue

                product.description = description
                product.description_source = 'csv'
                product.updated_at = datetime.utcnow()
                updated += 1

                if updated % 100 == 0:
                    db.session.flush()

            except Exception as e:
                errors += 1
                if len(error_messages) < 10:
                    error_messages.append(f"Строка {row_num}: {e}")

        supplier.last_description_sync_at = datetime.utcnow()
        supplier.last_description_sync_status = 'success' if errors == 0 else 'partial'
        db.session.commit()

        return {
            'success': True,
            'updated': updated,
            'not_found': not_found,
            'errors': errors,
            'error_messages': error_messages,
        }

    # ===================================================================
    # AI ПОЛНЫЙ ПАРСИНГ ТОВАРА
    # ===================================================================

    @staticmethod
    def ai_full_parse(product_id: int) -> dict:
        """
        Полный AI парсинг товара — извлекает ВСЕ возможные характеристики.

        Собирает все данные товара, отправляет в AI, получает структурированный
        JSON со всеми характеристиками, сохраняет результат.

        Returns:
            dict: {success, parsed_data, marketplace_data, fill_percentage, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен для этого поставщика'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис'}

        # Собираем все данные товара
        product_data = product.get_all_data_for_parsing()

        # Включённые категории маркетплейса для AI
        mp_categories = _get_marketplace_categories_block(supplier.id)

        success, result, error = ai_svc.full_product_parse(
            product_data, marketplace_categories_block=mp_categories
        )

        if success and result:
            # Сохраняем полный результат парсинга
            product.ai_parsed_data_json = json.dumps(result, ensure_ascii=False)
            product.ai_parsed_at = datetime.utcnow()
            product.ai_model_used = ai_svc.config.model
            product.ai_fill_pct = result.get('parsing_meta', {}).get('fill_percentage', 0)

            # Формируем данные для маркетплейса (WB)
            marketplace_data = _build_marketplace_data(product, result)
            product.ai_marketplace_json = json.dumps(marketplace_data, ensure_ascii=False)

            # Обновляем поля товара из результатов парсинга если они были пустые
            _apply_parsed_data_to_product(product, result)

            # Верификация категории (отдельный этап — до marketplace parse)
            _verify_and_fix_category(product, result, ai_svc)

            # Интеграция с MarketplaceAwareParsingTask
            _run_marketplace_aware_parse(product, result, ai_svc)

            product.updated_at = datetime.utcnow()
            db.session.commit()

            fill_pct = result.get('parsing_meta', {}).get('fill_percentage', 0)
            card_pct = _calc_card_completeness_pct(product)

            # Валидация и автокоррекция AI-характеристик
            validation_result = {}
            try:
                from services.smart_product_parser import CharacteristicsValidator
                val = CharacteristicsValidator.validate_product(
                    product_id, auto_correct=True
                )
                validation_result = val.to_dict()
            except Exception as ve:
                logger.debug(f"Characteristics validation skipped: {ve}")

            return {
                'success': True,
                'parsed_data': result,
                'marketplace_data': marketplace_data,
                'fill_percentage': fill_pct,
                'card_completeness': card_pct,
                'validation': validation_result,
            }

        return {'success': False, 'error': error or 'Ошибка AI парсинга'}

    # ===================================================================
    # ФОНОВЫЙ AI ПАРСИНГ
    # ===================================================================

    @staticmethod
    def start_ai_parse_job(supplier_id: int, product_ids: List[int],
                           admin_user_id: int = None,
                           max_workers: int = 8,
                           model_override: str = None) -> dict:
        """
        Запускает фоновый AI парсинг товаров.

        Returns:
            dict: {job_id, total} или {error}
        """
        from models import AIParseJob

        supplier = Supplier.query.get(supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'error': 'AI не включен для этого поставщика'}

        if not product_ids:
            return {'error': 'Не выбраны товары'}

        job_id = str(uuid.uuid4())
        job = AIParseJob(
            id=job_id,
            supplier_id=supplier_id,
            admin_user_id=admin_user_id,
            job_type='parse' if len(product_ids) > 1 else 'parse_single',
            status='pending',
            total=len(product_ids),
            processed=0,
            succeeded=0,
            failed=0,
            results=json.dumps([]),
        )
        db.session.add(job)
        db.session.commit()

        effective_workers = max(1, min(max_workers, 16, len(product_ids)))

        thread = threading.Thread(
            target=SupplierService._run_ai_parse_job,
            args=(job_id, supplier_id, product_ids, effective_workers, model_override),
            daemon=True,
            name=f'AIParse-{job_id[:8]}'
        )
        thread.start()

        logger.info(
            f"[AI Parse] Job {job_id} started: {len(product_ids)} products, "
            f"{effective_workers} workers, supplier={supplier_id}"
        )
        return {'job_id': job_id, 'total': len(product_ids)}

    @staticmethod
    def _notify_sellers_new_cards(supplier_id: int, succeeded_count: int):
        """
        Уведомить всех продавцов, подключённых к поставщику,
        о новых обработанных карточках.

        Проверяет, какие карточки ещё не импортированы продавцом,
        и создаёт уведомление только если есть новые.
        """
        try:
            supplier = Supplier.query.get(supplier_id)
            if not supplier:
                return

            # Получаем всех активных продавцов этого поставщика
            connections = SellerSupplier.query.filter_by(
                supplier_id=supplier_id,
                is_active=True
            ).all()

            if not connections:
                return

            # Общее количество товаров поставщика
            total_supplier_products = SupplierProduct.query.filter_by(
                supplier_id=supplier_id
            ).count()

            for conn in connections:
                try:
                    # Сколько товаров уже импортировано у этого продавца
                    imported_count = ImportedProduct.query.filter_by(
                        seller_id=conn.seller_id,
                        supplier_id=supplier_id
                    ).count()

                    # Сколько новых (не импортированных) товаров
                    new_count = total_supplier_products - imported_count
                    if new_count <= 0:
                        continue

                    # Создаём уведомление
                    n = Notification(
                        seller_id=conn.seller_id,
                        category='info',
                        title=f'Доступно {new_count} новых карточек',
                        message=(
                            f'У поставщика «{supplier.name}» обработано {succeeded_count} карточек. '
                            f'Доступно {new_count} новых товаров для импорта.'
                        ),
                        link=f'/supplier-catalog/{supplier_id}/products',
                        metadata_json=json.dumps({
                            'type': 'new_supplier_cards',
                            'supplier_id': supplier_id,
                            'supplier_name': supplier.name,
                            'new_count': new_count,
                            'succeeded_count': succeeded_count,
                        }, ensure_ascii=False),
                    )
                    db.session.add(n)
                except Exception as e:
                    logger.warning(f"Failed to create notification for seller {conn.seller_id}: {e}")

            _commit_with_retry(db.session)
            logger.info(
                f"[AI Parse] Notifications sent to {len(connections)} sellers "
                f"for supplier {supplier_id}"
            )
        except Exception as e:
            logger.error(f"[AI Parse] Error sending notifications: {e}")
            db.session.rollback()

    @staticmethod
    def _run_ai_parse_job(job_id: str, supplier_id: int, product_ids: List[int],
                          max_workers: int = 4, model_override: str = None):
        """
        Фоновый поток AI парсинга с параллельной обработкой.

        Использует ThreadPoolExecutor для одновременной отправки нескольких
        AI-запросов. Каждый воркер получает свой AIService (свою HTTP-сессию).
        Прогресс обновляется thread-safe через Lock.
        """
        from seller_platform import app as flask_app
        from models import AIParseJob

        with flask_app.app_context():
            job = AIParseJob.query.get(job_id)
            if not job:
                logger.error(f"[AI Parse] Job {job_id} not found")
                return

            job.status = 'running'
            job.heartbeat_at = datetime.utcnow()
            db.session.commit()

            supplier = Supplier.query.get(supplier_id)
            if not supplier or not supplier.ai_enabled:
                job.status = 'failed'
                job.error_message = 'AI не включен'
                db.session.commit()
                return

            # Проверяем что AI сервис создаётся
            test_svc = SupplierService._get_ai_service(supplier, model_override=model_override)
            if not test_svc:
                job.status = 'failed'
                job.error_message = 'Не удалось создать AI сервис'
                db.session.commit()
                return

            # Сохраняем название модели в задаче
            try:
                job.model_used = test_svc.config.model
                db.session.commit()
            except Exception:
                pass  # колонка может отсутствовать до миграции

            # Для одного товара — без пула
            if len(product_ids) == 1:
                SupplierService._parse_single_in_job(
                    job_id, supplier_id, product_ids[0], test_svc
                )
                return

            # Resolve marketplace categories once for all workers
            mp_categories = _get_marketplace_categories_block(supplier_id)

            # Сохраняем AIConfig ДО запуска пула — это plain dataclass,
            # не ORM-объект, безопасен для использования из любого потока.
            # Каждый воркер создаст свой AIService с отдельной HTTP-сессией.
            ai_config = test_svc.config

            # --- Параллельный режим ---
            effective_workers = min(max_workers, len(product_ids))
            logger.info(
                f"[AI Parse] Job {job_id}: {len(product_ids)} products, "
                f"{effective_workers} parallel workers"
            )

            # Thread-safe счётчики и результаты
            lock = threading.Lock()
            counters = {'processed': 0, 'succeeded': 0, 'failed': 0}
            results = []
            cancelled = threading.Event()
            job_done = threading.Event()  # Сигнал завершения для heartbeat-потока

            def _heartbeat_loop():
                """Фоновый поток: обновляет heartbeat каждые 30 секунд,
                пока AI-воркеры работают. Предотвращает ложное срабатывание
                stale-детекции при долгих AI-запросах (DeepSeek, two-pass)."""
                with flask_app.app_context():
                    while not job_done.wait(timeout=30):
                        try:
                            job_hb = AIParseJob.query.get(job_id)
                            if not job_hb or job_hb.status not in ('pending', 'running'):
                                break
                            job_hb.heartbeat_at = datetime.utcnow()
                            with lock:
                                job_hb.processed = counters['processed']
                                job_hb.succeeded = counters['succeeded']
                                job_hb.failed = counters['failed']
                            _commit_with_retry(db.session)
                        except Exception:
                            try:
                                db.session.rollback()
                            except Exception:
                                pass
                        finally:
                            # Освобождаем соединение между heartbeat-ами (30 сек простоя)
                            try:
                                db.session.remove()
                            except Exception:
                                pass

            # Запускаем фоновый heartbeat-поток
            heartbeat_thread = threading.Thread(
                target=_heartbeat_loop,
                daemon=True,
                name=f'AIParse-HB-{job_id[:8]}'
            )
            heartbeat_thread.start()

            def _update_job_progress(current_title=None):
                """Обновляет прогресс в БД (вызывается под lock из main thread)."""
                try:
                    job_ref = AIParseJob.query.get(job_id)
                    if not job_ref:
                        return
                    job_ref.processed = counters['processed']
                    job_ref.succeeded = counters['succeeded']
                    job_ref.failed = counters['failed']
                    job_ref.current_product_title = current_title
                    job_ref.heartbeat_at = datetime.utcnow()
                    job_ref.results = json.dumps(results[-100:], ensure_ascii=False)
                    _commit_with_retry(db.session)
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass
                finally:
                    # Освобождаем соединение сразу после обновления прогресса
                    try:
                        db.session.remove()
                    except Exception:
                        pass

            def _check_cancelled():
                """Проверяет не отменена ли задача."""
                try:
                    db.session.expire_all()
                    j = AIParseJob.query.get(job_id)
                    if j and j.status == 'cancelled':
                        cancelled.set()
                        return True
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass
                finally:
                    try:
                        db.session.remove()
                    except Exception:
                        pass
                return False

            def _parse_one(pid: int) -> dict:
                """
                Парсит один товар. Запускается в воркер-потоке.

                ВАЖНО: DB-соединение освобождается (session.remove()) перед долгим
                AI-вызовом и заново берётся из пула для сохранения результатов.
                Это предотвращает исчерпание QueuePool при параллельных воркерах.
                """
                if cancelled.is_set():
                    return {'product_id': pid, 'status': 'cancelled'}

                with flask_app.app_context():
                    title = ''
                    try:
                        # --- Фаза 1: читаем данные из БД и СРАЗУ освобождаем соединение ---
                        product_data = None
                        try:
                            product = SupplierProduct.query.get(pid)
                            if not product or product.supplier_id != supplier_id:
                                return {
                                    'product_id': pid, 'title': '',
                                    'status': 'error', 'error': 'Товар не найден',
                                }
                            title = (product.title or '')[:80]
                            product_data = product.get_all_data_for_parsing()
                        finally:
                            # Освобождаем соединение ДО долгого AI-вызова
                            try:
                                db.session.remove()
                            except Exception:
                                pass

                        # --- Фаза 2: AI-вызов (долгая операция, БЕЗ DB-соединения) ---
                        # Создаём AIService из заранее подготовленного config (plain dataclass),
                        # а не из ORM-объекта supplier, который отвязан от сессии после remove().
                        from services.ai_service import AIService as AISvc
                        worker_svc = AISvc(ai_config)

                        success, result, error = worker_svc.full_product_parse(
                            product_data, marketplace_categories_block=mp_categories
                        )

                        if not success or not result:
                            return {
                                'product_id': pid, 'title': title,
                                'status': 'error', 'error': error or 'Ошибка AI',
                            }

                        # --- Фаза 3: сохраняем результат (заново берём соединение из пула) ---
                        try:
                            product = SupplierProduct.query.get(pid)
                            if not product:
                                return {
                                    'product_id': pid, 'title': title,
                                    'status': 'error', 'error': 'Товар удалён во время парсинга',
                                }

                            with db.session.no_autoflush:
                                product.ai_parsed_data_json = json.dumps(result, ensure_ascii=False)
                                product.ai_parsed_at = datetime.utcnow()
                                product.ai_model_used = worker_svc.config.model
                                product.ai_fill_pct = result.get('parsing_meta', {}).get('fill_percentage', 0)

                                marketplace_data = _build_marketplace_data(product, result)
                                product.ai_marketplace_json = json.dumps(marketplace_data, ensure_ascii=False)

                                _apply_parsed_data_to_product(product, result)
                                _verify_and_fix_category(product, result, worker_svc)
                                _run_marketplace_aware_parse(product, result, worker_svc)

                                product.updated_at = datetime.utcnow()

                            _commit_with_retry(db.session)
                        finally:
                            try:
                                db.session.remove()
                            except Exception:
                                pass

                        # --- Фаза 4: валидация (отдельное короткое соединение) ---
                        validation_info = {}
                        try:
                            from services.smart_product_parser import CharacteristicsValidator
                            val_result = CharacteristicsValidator.validate_product(
                                pid, auto_correct=True
                            )
                            validation_info = {
                                'chars_valid': val_result.is_valid,
                                'chars_corrected': val_result.corrected_count,
                                'chars_invalid': val_result.invalid_count,
                            }
                        except Exception as ve:
                            logger.debug(f"Characteristics validation skipped: {ve}")
                        finally:
                            try:
                                db.session.remove()
                            except Exception:
                                pass

                        fill_pct = result.get('parsing_meta', {}).get('fill_percentage', 0)
                        try:
                            product = SupplierProduct.query.get(pid)
                            card_pct = _calc_card_completeness_pct(product) if product else 0
                        except Exception:
                            card_pct = 0
                        finally:
                            try:
                                db.session.remove()
                            except Exception:
                                pass

                        return {
                            'product_id': pid, 'title': title,
                            'status': 'success', 'fill_pct': fill_pct,
                            'card_pct': card_pct,
                            **validation_info,
                        }
                    except Exception as e:
                        try:
                            db.session.rollback()
                        except Exception:
                            pass
                        logger.error(f"[AI Parse] Worker error pid={pid}: {e}")
                        return {
                            'product_id': pid, 'title': title,
                            'status': 'error', 'error': str(e)[:200],
                        }
                    finally:
                        try:
                            db.session.remove()
                        except Exception:
                            pass

            # Запускаем пул
            with ThreadPoolExecutor(max_workers=effective_workers,
                                    thread_name_prefix='AIParse') as pool:
                futures = {}
                for pid in product_ids:
                    if cancelled.is_set():
                        break
                    fut = pool.submit(_parse_one, pid)
                    futures[fut] = pid

                # Проверяем отмену периодически
                check_interval = max(1, effective_workers)
                done_count = 0

                for fut in as_completed(futures):
                    try:
                        res = fut.result()
                    except Exception as fut_err:
                        pid = futures.get(fut, 0)
                        logger.error(f"[AI Parse] Unhandled worker error pid={pid}: {fut_err}")
                        res = {
                            'product_id': pid, 'title': '',
                            'status': 'error', 'error': str(fut_err)[:200],
                        }
                    done_count += 1

                    with lock:
                        counters['processed'] += 1
                        if res.get('status') == 'success':
                            counters['succeeded'] += 1
                        elif res.get('status') == 'cancelled':
                            pass  # не считаем
                        else:
                            counters['failed'] += 1
                        results.append(res)

                    # Обновляем прогресс в БД каждые N завершений
                    # + heartbeat обновляется каждый раз для обнаружения stale jobs
                    if done_count % check_interval == 0 or done_count == len(product_ids):
                        current_title = res.get('title') if res.get('status') != 'success' else None
                        _update_job_progress(current_title)

                        # Проверяем отмену
                        if done_count % (check_interval * 2) == 0:
                            _check_cancelled()
                            if cancelled.is_set():
                                pool.shutdown(wait=False, cancel_futures=True)
                                break
                    # Heartbeat обновляется фоновым потоком (_heartbeat_loop)
                    # каждые 30 секунд, независимо от прогресса AI-запросов

            # Останавливаем heartbeat-поток
            job_done.set()
            heartbeat_thread.join(timeout=5)

            # Завершение задачи
            try:
                job_final = AIParseJob.query.get(job_id)
                if job_final:
                    if job_final.status != 'cancelled':
                        job_final.status = 'done'
                    job_final.processed = counters['processed']
                    job_final.succeeded = counters['succeeded']
                    job_final.failed = counters['failed']
                    job_final.current_product_title = None
                    job_final.heartbeat_at = datetime.utcnow()
                    job_final.results = json.dumps(results[-100:], ensure_ascii=False)
                    job_final.updated_at = datetime.utcnow()
                    _commit_with_retry(db.session)
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
            finally:
                try:
                    db.session.remove()
                except Exception:
                    pass

            logger.info(
                f"[AI Parse] Job {job_id} done: "
                f"{counters['succeeded']} ok, {counters['failed']} fail "
                f"out of {len(product_ids)} ({effective_workers} workers)"
            )

            # Уведомляем продавцов о новых обработанных карточках
            if counters['succeeded'] > 0:
                try:
                    SupplierService._notify_sellers_new_cards(
                        supplier_id, counters['succeeded']
                    )
                except Exception as e:
                    logger.warning(f"[AI Parse] Notification error: {e}")

    @staticmethod
    def _parse_single_in_job(job_id: str, supplier_id: int, product_id: int, ai_svc):
        """Парсит один товар в рамках job (без пула)."""
        from models import AIParseJob

        job = AIParseJob.query.get(job_id)
        if not job:
            return

        product = SupplierProduct.query.get(product_id)
        if not product or product.supplier_id != supplier_id:
            job.status = 'done'
            job.processed = 1
            job.failed = 1
            job.results = json.dumps([{
                'product_id': product_id, 'status': 'error',
                'error': 'Товар не найден',
            }], ensure_ascii=False)
            db.session.commit()
            return

        job.current_product_title = (product.title or 'Без названия')[:200]
        job.heartbeat_at = datetime.utcnow()
        product_title_short = (product.title or '')[:80]
        db.session.commit()

        try:
            product_data = product.get_all_data_for_parsing()
            mp_categories = _get_marketplace_categories_block(supplier_id)

            # Освобождаем соединение перед долгим AI-вызовом
            try:
                db.session.remove()
            except Exception:
                pass

            success, result, error = ai_svc.full_product_parse(
                product_data, marketplace_categories_block=mp_categories
            )

            if success and result:
                # Заново берём product из БД
                product = SupplierProduct.query.get(product_id)
                if not product:
                    job = AIParseJob.query.get(job_id)
                    if job:
                        job.status = 'done'
                        job.processed = 1
                        job.failed = 1
                        job.results = json.dumps([{
                            'product_id': product_id, 'status': 'error',
                            'error': 'Товар удалён во время парсинга',
                        }], ensure_ascii=False)
                        job.current_product_title = None
                        job.updated_at = datetime.utcnow()
                        _commit_with_retry(db.session)
                    return

                with db.session.no_autoflush:
                    product.ai_parsed_data_json = json.dumps(result, ensure_ascii=False)
                    product.ai_parsed_at = datetime.utcnow()
                    product.ai_model_used = ai_svc.config.model
                    product.ai_fill_pct = result.get('parsing_meta', {}).get('fill_percentage', 0)
                    marketplace_data = _build_marketplace_data(product, result)
                    product.ai_marketplace_json = json.dumps(marketplace_data, ensure_ascii=False)
                    _apply_parsed_data_to_product(product, result)
                    _verify_and_fix_category(product, result, ai_svc)
                    _run_marketplace_aware_parse(product, result, ai_svc)
                    product.updated_at = datetime.utcnow()

                fill_pct = result.get('parsing_meta', {}).get('fill_percentage', 0)
                card_pct = _calc_card_completeness_pct(product)

                job = AIParseJob.query.get(job_id)
                if job:
                    job.status = 'done'
                    job.processed = 1
                    job.succeeded = 1
                    job.results = json.dumps([{
                        'product_id': product_id,
                        'title': product_title_short,
                        'status': 'success',
                        'fill_pct': fill_pct,
                        'card_pct': card_pct,
                    }], ensure_ascii=False)
            else:
                job = AIParseJob.query.get(job_id)
                if job:
                    job.status = 'done'
                    job.processed = 1
                    job.failed = 1
                    job.results = json.dumps([{
                        'product_id': product_id,
                        'title': product_title_short,
                        'status': 'error',
                        'error': error or 'Ошибка AI',
                    }], ensure_ascii=False)
        except Exception as e:
            logger.error(f"[AI Parse] Single parse error {product_id}: {e}")
            try:
                db.session.rollback()
            except Exception:
                db.session.remove()
            job = AIParseJob.query.get(job_id)
            if job:
                job.status = 'done'
                job.processed = 1
                job.failed = 1
                job.results = json.dumps([{
                    'product_id': product_id,
                    'status': 'error',
                    'error': str(e)[:200],
                }], ensure_ascii=False)

        try:
            job = AIParseJob.query.get(job_id)
            if job:
                job.current_product_title = None
                job.updated_at = datetime.utcnow()
                _commit_with_retry(db.session)
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass

        # Уведомляем продавцов (аналогично batch-парсингу)
        if job.succeeded and job.succeeded > 0:
            try:
                SupplierService._notify_sellers_new_cards(
                    supplier_id, job.succeeded
                )
            except Exception as e:
                logger.warning(f"[AI Parse] Single parse notification error: {e}")

    @staticmethod
    def start_description_sync_job(supplier_id: int, admin_user_id: int = None) -> dict:
        """
        Запускает фоновую синхронизацию описаний.

        Returns:
            dict: {job_id} или {error}
        """
        from models import AIParseJob

        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return {'error': 'Поставщик не найден'}
        if not supplier.description_file_url:
            return {'error': 'URL файла описаний не задан'}

        job_id = str(uuid.uuid4())
        job = AIParseJob(
            id=job_id,
            supplier_id=supplier_id,
            admin_user_id=admin_user_id,
            job_type='sync_descriptions',
            status='pending',
            total=0,
            processed=0,
            succeeded=0,
            failed=0,
            results=json.dumps([]),
        )
        db.session.add(job)
        db.session.commit()

        thread = threading.Thread(
            target=SupplierService._run_description_sync_job,
            args=(job_id, supplier_id),
            daemon=True,
            name=f'DescSync-{job_id[:8]}'
        )
        thread.start()

        return {'job_id': job_id}

    @staticmethod
    def _run_description_sync_job(job_id: str, supplier_id: int):
        """Фоновый поток синхронизации описаний."""
        from seller_platform import app as flask_app
        from models import AIParseJob

        with flask_app.app_context():
            job = AIParseJob.query.get(job_id)
            if not job:
                return

            job.status = 'running'
            job.current_product_title = 'Загрузка CSV...'
            db.session.commit()

            result = SupplierService.sync_descriptions(supplier_id)

            job.status = 'done' if result.get('success') else 'failed'
            job.succeeded = result.get('updated', 0)
            job.failed = result.get('errors', 0)
            job.processed = job.succeeded + job.failed + result.get('not_found', 0)
            job.total = job.processed
            job.error_message = result.get('error')
            job.current_product_title = None
            job.results = json.dumps([{
                'updated': result.get('updated', 0),
                'not_found': result.get('not_found', 0),
                'errors': result.get('errors', 0),
                'error_messages': result.get('error_messages', []),
            }], ensure_ascii=False)
            job.updated_at = datetime.utcnow()
            db.session.commit()

            logger.info(f"[Desc Sync] Job {job_id} done: {result}")

    @staticmethod
    def get_ai_parse_job(job_id: str) -> Optional[dict]:
        """Получить статус фоновой задачи AI парсинга."""
        from models import AIParseJob

        job = AIParseJob.query.get(job_id)
        if not job:
            return None

        results = []
        if job.results:
            try:
                results = json.loads(job.results)
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            'job_id': job.id,
            'job_type': job.job_type,
            'status': job.status,
            'total': job.total,
            'processed': job.processed,
            'succeeded': job.succeeded,
            'failed': job.failed,
            'current_product': job.current_product_title,
            'model_used': getattr(job, 'model_used', None),
            'error_message': job.error_message,
            'progress_pct': round(job.processed / job.total * 100) if job.total else 0,
            'results': results[-50:],
            'created_at': job.created_at.isoformat() if job.created_at else None,
            'updated_at': job.updated_at.isoformat() if job.updated_at else None,
            'heartbeat_at': job.heartbeat_at.isoformat() if getattr(job, 'heartbeat_at', None) else None,
            'is_stale': job.is_stale if hasattr(job, 'is_stale') else False,
        }

    @staticmethod
    def cancel_ai_parse_job(job_id: str) -> bool:
        """Отменить фоновую задачу AI парсинга."""
        from models import AIParseJob

        job = AIParseJob.query.get(job_id)
        if not job or job.status not in ('pending', 'running'):
            return False
        job.status = 'cancelled'
        db.session.commit()
        return True

    @staticmethod
    def get_active_ai_parse_jobs(supplier_id: int) -> List[dict]:
        """Получить активные задачи AI парсинга для поставщика.

        Автоматически обнаруживает и помечает зависшие задачи (stale jobs),
        у которых heartbeat не обновлялся дольше STALE_TIMEOUT_SECONDS.
        """
        from models import AIParseJob

        jobs = AIParseJob.query.filter(
            AIParseJob.supplier_id == supplier_id,
            AIParseJob.status.in_(['pending', 'running'])
        ).order_by(AIParseJob.created_at.desc()).all()

        # Проверяем stale jobs и помечаем их как failed
        active_jobs = []
        for j in jobs:
            if j.is_stale:
                logger.warning(
                    f"[AI Parse] Stale job detected: {j.id}, "
                    f"status={j.status}, heartbeat_at={j.heartbeat_at}, "
                    f"processed={j.processed}/{j.total}"
                )
                j.status = 'failed'
                j.error_message = (
                    f'Задача зависла (воркер не отвечает). '
                    f'Обработано {j.processed} из {j.total} товаров '
                    f'({j.succeeded} успешно, {j.failed} с ошибкой). '
                    f'Токены за необработанные товары могли быть списаны.'
                )
                j.updated_at = datetime.utcnow()
                try:
                    _commit_with_retry(db.session)
                except Exception:
                    db.session.rollback()
                # Не добавляем в active_jobs — она больше не активна
            else:
                data = SupplierService.get_ai_parse_job(j.id)
                if data:
                    active_jobs.append(data)

        return active_jobs

    @staticmethod
    def get_recent_ai_parse_jobs(supplier_id: int, limit: int = 10) -> List[dict]:
        """Получить последние задачи AI парсинга."""
        from models import AIParseJob

        jobs = AIParseJob.query.filter_by(supplier_id=supplier_id)\
            .order_by(AIParseJob.created_at.desc()).limit(limit).all()

        return [r for r in (SupplierService.get_ai_parse_job(j.id) for j in jobs) if r]

    @staticmethod
    def get_product_raw_json(product_id: int) -> dict:
        """
        Возвращает полный JSON дамп товара для анализа.

        Returns:
            dict: Все данные товара включая AI парсинг
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'error': 'Товар не найден'}

        data = product.to_dict(include_ai=True)
        # Добавляем полные данные
        data['all_data_for_parsing'] = product.get_all_data_for_parsing()
        data['ai_parsed_data'] = product.get_ai_parsed_data()
        data['ai_marketplace_data'] = product.get_ai_marketplace_data()

        # Оригинальные данные
        try:
            data['original_data'] = json.loads(product.original_data_json) if product.original_data_json else {}
        except Exception:
            data['original_data'] = {}

        # JSON поля развёрнутые
        try:
            data['colors'] = json.loads(product.colors_json) if product.colors_json else []
        except Exception:
            data['colors'] = []
        try:
            data['materials'] = json.loads(product.materials_json) if product.materials_json else []
        except Exception:
            data['materials'] = []
        try:
            data['sizes'] = json.loads(product.sizes_json) if product.sizes_json else {}
        except Exception:
            data['sizes'] = {}
        try:
            data['dimensions'] = json.loads(product.dimensions_json) if product.dimensions_json else {}
        except Exception:
            data['dimensions'] = {}
        try:
            data['characteristics'] = json.loads(product.characteristics_json) if product.characteristics_json else []
        except Exception:
            data['characteristics'] = []

        return data


# ============================================================================
# CARD COMPLETENESS CALCULATOR
# ============================================================================

def _calc_card_completeness_pct(product: SupplierProduct) -> int:
    """
    Быстрый расчёт заполненности карточки (0-100%).
    Считает основные группы полей: основные, цены, характеристики, AI, маркетплейс, медиа.
    """
    import json as _json

    def _has(val):
        if val is None:
            return False
        if isinstance(val, str):
            val = val.strip()
            if not val or val in ('[]', '{}', '""'):
                return False
            if val.startswith('[') or val.startswith('{'):
                try:
                    return bool(_json.loads(val))
                except Exception:
                    pass
            return True
        if isinstance(val, (int, float)):
            return True
        return bool(val)

    fields = [
        # Основные (6)
        product.title, product.description, product.brand,
        product.category, product.vendor_code, product.barcode,
        # Цены и остатки (4)
        product.supplier_price, product.supplier_quantity,
        product.recommended_retail_price, product.supplier_status,
        # Характеристики (9)
        product.gender, product.country, product.season, product.age_group,
        product.colors_json, product.materials_json, product.sizes_json,
        product.dimensions_json, product.characteristics_json,
        # AI данные (5)
        product.ai_seo_title, product.ai_description,
        product.ai_keywords_json, product.ai_bullets_json, product.ai_parsed_data_json,
        # Маркетплейс (3) + валидация
        product.wb_category_name, product.wb_subject_id, product.marketplace_fields_json,
    ]

    filled = sum(1 for f in fields if _has(f))
    # +1 для валидации маркетплейса
    total = len(fields) + 1
    if getattr(product, 'marketplace_validation_status', None) == 'valid':
        filled += 1

    # Медиа (2): фотографии + видео
    total += 2
    if hasattr(product, 'get_photos') and product.get_photos():
        filled += 1
    if _has(getattr(product, 'video_url', None)):
        filled += 1

    return round(filled / total * 100) if total else 0


# ============================================================================
# MARKETPLACE DATA BUILDER
# ============================================================================

def _build_marketplace_data(product: SupplierProduct, parsed: dict) -> dict:
    """
    Формирует данные в формате маркетплейса WB из AI парсинга.

    Returns:
        dict с данными готовыми для загрузки в WB
    """
    mp = parsed.get('marketplace_ready', {})
    brand_info = parsed.get('brand_info', {})
    physical = parsed.get('physical', {})
    pkg = parsed.get('package', {})
    materials = parsed.get('materials', {})
    color = parsed.get('color', {})
    sizing = parsed.get('sizing', {})
    audience = parsed.get('audience', {})
    seasonality = parsed.get('seasonality', {})
    contents = parsed.get('contents', {})
    identity = parsed.get('product_identity', {})
    origin = parsed.get('origin', {})

    # Габариты товара
    dims_length = physical.get('length_cm')
    dims_width = physical.get('width_cm')
    dims_height = physical.get('height_cm')
    dims_weight_kg = round(physical.get('weight_g', 0) / 1000, 2) if physical.get('weight_g') else None

    # Только явно извлечённые габариты упаковки. Подстановка типичных размеров
    # или веса создаёт ложный physical fact и запрещена для marketplace drafts.
    pkg_length = pkg.get('package_length_cm')
    pkg_width = pkg.get('package_width_cm')
    pkg_height = pkg.get('package_height_cm')
    pkg_weight_kg = round(pkg.get('package_weight_g', 0) / 1000, 2) if pkg.get('package_weight_g') else None

    wb_data = {
        'title': mp.get('wb_title_suggestion') or product.ai_seo_title or product.title or '',
        'description': mp.get('wb_description_short') or product.ai_description or product.description or '',
        'brand': brand_info.get('brand_normalized') or brand_info.get('brand') or product.brand or '',
        'vendor_code': product.vendor_code or '',
        'barcode': product.barcode or '',

        # Категория
        'category': identity.get('wb_category') or product.wb_category_name or product.category or '',
        'subject': identity.get('wb_subject') or product.wb_subject_name or '',
        'subject_id': product.wb_subject_id,

        # Цена
        'price': product.supplier_price,

        # Цвет
        'color': color.get('wb_color') or color.get('primary_color') or '',

        # Размеры
        'sizes': sizing.get('available_sizes', []),
        'ru_sizes': sizing.get('ru_sizes', []),

        # Габариты для WB
        'dimensions': {
            'length': dims_length,
            'width': dims_width,
            'height': dims_height,
            'weight_kg': dims_weight_kg,
        },
        'package_dimensions': {
            'length': pkg_length,
            'width': pkg_width,
            'height': pkg_height,
            'weight_kg': pkg_weight_kg,
        },

        # Характеристики
        'characteristics': {
            'Бренд': brand_info.get('brand_normalized') or brand_info.get('brand') or product.brand or '',
            'Цвет': color.get('wb_color') or '',
            'Пол': audience.get('gender') or product.gender or '',
            'Страна производства': origin.get('country_of_origin') or product.country or '',
            'Материал': materials.get('primary_material') or '',
            'Состав': materials.get('composition') or '',
            'Сезон': seasonality.get('season') or product.season or '',
            'Комплектация': ', '.join(contents.get('package_contents', [])) if contents.get('package_contents') else '',
        },

        # SEO
        'keywords': mp.get('search_keywords', []),
        'bullet_points': mp.get('bullet_points', []),

        # Доп. данные
        'photos_count': len(product.get_photos()),
    }

    # Удаляем пустые характеристики
    wb_data['characteristics'] = {k: v for k, v in wb_data['characteristics'].items() if v}

    # Добавляем доп характеристики из физических данных
    # Используем как общие, так и категорийно-специфичные имена,
    # чтобы маппинг работал для любых категорий WB
    if physical.get('diameter_cm') or physical.get('max_diameter_cm'):
        diameter = physical.get('max_diameter_cm') or physical.get('diameter_cm')
        wb_data['characteristics']['Диаметр'] = diameter
        wb_data['characteristics']['Максимальный диаметр'] = diameter
    if physical.get('volume_ml'):
        wb_data['characteristics']['Объем'] = physical['volume_ml']
    if physical.get('working_length_cm'):
        wb_data['characteristics']['Рабочая длина'] = physical['working_length_cm']
    if physical.get('length_cm'):
        wb_data['characteristics']['Длина'] = physical['length_cm']
    if physical.get('width_cm'):
        wb_data['characteristics']['Ширина'] = physical['width_cm']
        wb_data['characteristics']['Ширина предмета'] = physical['width_cm']
    if physical.get('weight_g'):
        wb_data['characteristics']['Вес товара без упаковки'] = physical['weight_g']
        wb_data['characteristics']['Вес'] = physical['weight_g']

    # Функциональность / особенности
    functionality = parsed.get('functionality', {})
    if functionality.get('special_features'):
        features = functionality['special_features']
        wb_data['characteristics']['Особенности'] = '; '.join(features) if isinstance(features, list) else features

    # Комплектация из contents (если не уже заполнена)
    contents = parsed.get('contents', {})
    if not wb_data['characteristics'].get('Комплектация') and contents.get('package_contents'):
        wb_data['characteristics']['Комплектация'] = ', '.join(contents['package_contents'])

    return wb_data


def _apply_parsed_data_to_product(product: SupplierProduct, parsed: dict) -> None:
    """Обновляет пустые поля товара из результатов AI парсинга"""
    brand_info = parsed.get('brand_info', {})
    audience = parsed.get('audience', {})
    origin = parsed.get('origin', {})
    seasonality = parsed.get('seasonality', {})
    color = parsed.get('color', {})
    materials = parsed.get('materials', {})
    physical = parsed.get('physical', {})

    # Обновляем только пустые поля
    if not product.brand and brand_info.get('brand'):
        product.brand = brand_info['brand']

    if not product.gender and audience.get('gender'):
        gender_map = {'male': 'Мужской', 'female': 'Женский', 'unisex': 'Унисекс'}
        product.gender = gender_map.get(audience['gender'], audience['gender'])

    if not product.country and origin.get('country_of_origin'):
        product.country = origin['country_of_origin']

    if not product.season and seasonality.get('season'):
        season_map = {'all_season': 'Всесезонный', 'summer': 'Лето', 'winter': 'Зима', 'demi': 'Демисезон'}
        product.season = season_map.get(seasonality['season'], seasonality['season'])

    if not product.age_group and audience.get('age_group'):
        age_map = {'adult': 'Взрослый', 'teen': 'Подросток', 'child': 'Детский', 'baby': 'Малыш'}
        product.age_group = age_map.get(audience['age_group'], audience['age_group'])

    # Обновляем JSON поля если пустые
    if not product.colors_json and color.get('primary_color'):
        colors = [color['primary_color']]
        if color.get('secondary_colors'):
            colors.extend(color['secondary_colors'])
        product.colors_json = json.dumps(colors, ensure_ascii=False)

    if not product.materials_json and materials.get('materials_list'):
        product.materials_json = json.dumps(materials['materials_list'], ensure_ascii=False)

    if not product.dimensions_json and any(v for v in physical.values() if v is not None):
        product.dimensions_json = json.dumps(physical, ensure_ascii=False)

    # Маркетплейс-ready данные
    mp = parsed.get('marketplace_ready', {})
    if not product.ai_seo_title and mp.get('wb_title_suggestion'):
        product.ai_seo_title = _sanitize_wb_text(mp['wb_title_suggestion'][:60])

    if not product.ai_description and mp.get('wb_description_short'):
        product.ai_description = _sanitize_wb_text(mp['wb_description_short'])

    if not product.ai_keywords_json and mp.get('search_keywords'):
        product.ai_keywords_json = json.dumps(mp['search_keywords'], ensure_ascii=False)

    if not product.ai_bullets_json and mp.get('bullet_points'):
        product.ai_bullets_json = json.dumps(mp['bullet_points'], ensure_ascii=False)

    # Собираем characteristics_json из AI-результатов если пуст
    if not product.characteristics_json or product.characteristics_json in ('{}', '[]', ''):
        chars = {}
        if product.brand:
            chars['Бренд'] = product.brand
        if color.get('wb_color') or color.get('primary_color'):
            chars['Цвет'] = color.get('wb_color') or color.get('primary_color')
        if materials.get('primary_material'):
            chars['Материал'] = materials['primary_material']
        if materials.get('composition'):
            chars['Состав'] = materials['composition']
        if product.gender:
            chars['Пол'] = product.gender
        if product.country:
            chars['Страна производства'] = product.country
        if product.season:
            chars['Сезон'] = product.season
        if physical.get('diameter_cm') or physical.get('max_diameter_cm'):
            diameter = str(physical.get('max_diameter_cm') or physical.get('diameter_cm'))
            chars['Диаметр'] = diameter
            chars['Максимальный диаметр'] = diameter
        if physical.get('volume_ml'):
            chars['Объем'] = str(physical['volume_ml'])
        if physical.get('working_length_cm'):
            chars['Рабочая длина'] = str(physical['working_length_cm'])
        if physical.get('length_cm'):
            chars['Длина'] = str(physical['length_cm'])
        if physical.get('width_cm'):
            chars['Ширина'] = str(physical['width_cm'])
            chars['Ширина предмета'] = str(physical['width_cm'])
        if physical.get('weight_g'):
            chars['Вес товара без упаковки'] = str(physical['weight_g'])
        # Функциональность / особенности
        functionality = parsed.get('functionality', {})
        if functionality.get('special_features'):
            features = functionality['special_features']
            chars['Особенности'] = '; '.join(features) if isinstance(features, list) else str(features)
        pkg_contents = parsed.get('contents', {})
        if pkg_contents.get('package_contents'):
            chars['Комплектация'] = ', '.join(pkg_contents['package_contents'])
        if chars:
            product.characteristics_json = json.dumps(chars, ensure_ascii=False)


def _verify_and_fix_category(product: SupplierProduct, parsed_data: dict, ai_svc) -> None:
    """
    Пост-проверка категории после AI парсинга.

    Использует ВСЕ категории из БД (MarketplaceCategory) вместо хардкода.
    Многоуровневый подход:
      1. Точное совпадение wb_subject из AI-ответа с БД
      2. Fuzzy-match по имени категории
      3. AI detect_category с полным списком из БД
      4. SmartCategoryMapper как фоллбэк
    """
    from models import MarketplaceCategory, MarketplaceConnection, Marketplace
    from difflib import SequenceMatcher

    identity = parsed_data.get('product_identity', {})
    ai_wb_subject = (identity.get('wb_subject') or '').strip()
    ai_wb_category = (identity.get('wb_category') or '').strip()

    # Загружаем все включённые категории из БД
    wb_marketplace = Marketplace.query.filter_by(code='wb').first()
    if not wb_marketplace:
        return

    db_categories = MarketplaceCategory.query.filter_by(
        marketplace_id=wb_marketplace.id,
        is_enabled=True,
    ).all()
    if not db_categories:
        return

    # Словарь {subject_id: subject_name} из БД
    db_cat_map = {c.subject_id: c.subject_name for c in db_categories}
    # Обратный словарь {name_lower: subject_id}
    name_to_id = {c.subject_name.lower(): c.subject_id for c in db_categories}

    current_id = product.wb_subject_id
    current_confidence = getattr(product, 'category_confidence', 0) or 0

    best_id = None
    best_name = None
    best_confidence = 0.0
    method = None

    # --- 1. Если текущая категория уже есть в БД и confidence высок — оставляем ---
    if current_id and current_id in db_cat_map and current_confidence >= 0.9:
        logger.debug(
            f"[CategoryVerify] Product {product.id}: keeping current "
            f"category {current_id} (confidence={current_confidence})"
        )
        return

    # --- 2. Точное совпадение AI wb_subject с БД ---
    if ai_wb_subject:
        ai_lower = ai_wb_subject.lower()
        if ai_lower in name_to_id:
            best_id = name_to_id[ai_lower]
            best_name = db_cat_map[best_id]
            best_confidence = 0.92
            method = 'exact_match'

    # --- 3. Fuzzy-match AI wb_subject по БД ---
    if not best_id and ai_wb_subject:
        ai_lower = ai_wb_subject.lower()
        top_ratio = 0.0
        for cat in db_categories:
            ratio = SequenceMatcher(None, ai_lower, cat.subject_name.lower()).ratio()
            if ratio > top_ratio:
                top_ratio = ratio
                if ratio >= 0.78:
                    best_id = cat.subject_id
                    best_name = cat.subject_name
                    best_confidence = round(ratio * 0.9, 3)
                    method = 'fuzzy_match'

    # --- 4. Fuzzy-match по ai_wb_category (может быть более общим) ---
    if not best_id and ai_wb_category:
        ai_lower = ai_wb_category.lower()
        top_ratio = 0.0
        for cat in db_categories:
            ratio = SequenceMatcher(None, ai_lower, cat.subject_name.lower()).ratio()
            if ratio > top_ratio:
                top_ratio = ratio
                if ratio >= 0.78:
                    best_id = cat.subject_id
                    best_name = cat.subject_name
                    best_confidence = round(ratio * 0.85, 3)
                    method = 'fuzzy_wb_category'

    # --- 5. AI detect_category с полным списком из БД ---
    if not best_id or best_confidence < 0.8:
        try:
            # Подставляем в AI-сервис категории из БД
            original_categories = getattr(ai_svc, '_categories', None)
            ai_svc.set_categories(db_cat_map)

            from services.wb_categories_mapping import get_keyword_hints
            hints = get_keyword_hints(
                product_title=product.title or '',
                csv_category=product.category or '',
                description=(product.description or '')[:300],
            )

            cat_id, cat_name, cat_conf, cat_reason = ai_svc.detect_category(
                product_title=product.title or '',
                source_category=product.category or '',
                all_categories=[
                    c for c in [
                        identity.get('wb_category'),
                        identity.get('product_type'),
                        identity.get('product_subtype'),
                        product.category,
                    ] if c
                ],
                brand=product.brand or '',
                description=(product.description or '')[:500],
                keyword_hints=hints,
            )

            # Возвращаем оригинальные категории
            if original_categories is not None:
                ai_svc.set_categories(original_categories)

            if cat_id and cat_id in db_cat_map:
                # AI дал результат лучше текущего?
                if cat_conf > best_confidence:
                    best_id = cat_id
                    best_name = cat_name
                    best_confidence = cat_conf
                    method = 'ai_detect'
        except Exception as e:
            logger.warning(f"[CategoryVerify] AI detect_category failed for {product.id}: {e}")

    # --- 6. SmartCategoryMapper как фоллбэк ---
    if not best_id or best_confidence < 0.6:
        try:
            from services.smart_category_mapper import SmartCategoryMapper
            sm_id, sm_name, sm_conf = SmartCategoryMapper.map_category(
                csv_category=product.category or '',
                product_title=product.title or '',
                brand=product.brand or '',
                description=(product.description or '')[:300],
                all_categories=None,
                source_type='sexoptovik',
            )
            if sm_id and sm_id in db_cat_map and sm_conf > best_confidence:
                best_id = sm_id
                best_name = db_cat_map.get(sm_id, sm_name)
                best_confidence = sm_conf
                method = 'smart_mapper'
        except Exception as e:
            logger.debug(f"[CategoryVerify] SmartCategoryMapper fallback failed: {e}")

    # --- Применяем результат ---
    if best_id and (best_confidence > current_confidence or current_id not in db_cat_map):
        product.wb_subject_id = best_id
        product.wb_subject_name = best_name
        product.wb_category_name = best_name
        product.category_confidence = best_confidence
        logger.info(
            f"[CategoryVerify] Product {product.id}: "
            f"category updated → {best_name} ({best_id}), "
            f"confidence={best_confidence}, method={method}"
        )
    elif best_id:
        logger.debug(
            f"[CategoryVerify] Product {product.id}: "
            f"keeping {current_id} (current_conf={current_confidence} >= new {best_confidence})"
        )


def _run_marketplace_aware_parse(product: SupplierProduct, parsed_data: dict, ai_svc) -> None:
    """Запускает MarketplaceAwareParsingTask если удалось определить категорию WB."""
    from models import MarketplaceCategory, MarketplaceCategoryCharacteristic, MarketplaceConnection, Marketplace
    from services.marketplace_ai_parser import MarketplaceAwareParsingTask
    from services.marketplace_validator import MarketplaceValidator

    # 1. Пытаемся определить subject_id
    subject_id = product.wb_subject_id
    if not subject_id:
        wb_subject_name = parsed_data.get('product_identity', {}).get('wb_subject')
        if wb_subject_name:
            # Ищем категорию по имени
            cat = MarketplaceCategory.query.filter(MarketplaceCategory.subject_name.ilike(wb_subject_name)).first()
            if cat:
                subject_id = cat.subject_id
                product.wb_subject_id = subject_id
                product.wb_subject_name = cat.subject_name

    # Если не нашли, ищем категорию по умолчанию у поставщика
    if not subject_id:
        conn = MarketplaceConnection.query.filter_by(supplier_id=product.supplier_id, is_active=True).first()
        if conn and conn.auto_map_categories and conn.default_category_id:
            cat = MarketplaceCategory.query.get(conn.default_category_id)
            if cat:
                subject_id = cat.subject_id
                product.wb_subject_id = subject_id
                product.wb_subject_name = cat.subject_name
                
    if not subject_id:
        logger.info(f"Skipping MarketplaceAwareParsingTask for {product.id} - no subject_id found.")
        return
        
    # Ищем характеристики для этой категории
    wb_marketplace = Marketplace.query.filter_by(code='wb').first()
    if not wb_marketplace:
        return
        
    cat = MarketplaceCategory.query.filter_by(marketplace_id=wb_marketplace.id, subject_id=subject_id).first()
    if not cat:
        return
        
    characteristics = MarketplaceCategoryCharacteristic.query.filter_by(category_id=cat.id, is_enabled=True).all()
    if not characteristics:
        logger.info(f"No characteristics found for category {cat.id} ({cat.subject_name}), skipping MarketplaceAwareParsingTask")
        return

    # Запускаем таску с двухпроходным парсингом и кэшированием
    task = MarketplaceAwareParsingTask(
        client=ai_svc.client,
        characteristics=characteristics,
        custom_instruction=ai_svc.config.custom_parsing_instruction,
        category_id=subject_id,
    )

    product_info = {
        'title': product.title or product.ai_seo_title,
        'description': product.description or product.ai_description,
        'brand': product.brand,
        'category': product.wb_category_name or product.category,
        'wb_category': product.wb_subject_name,
    }

    # Передаём уже извлечённые данные для лучшего маппинга
    physical = parsed_data.get('physical', {})
    color = parsed_data.get('color', {})
    materials = parsed_data.get('materials', {})
    contents = parsed_data.get('contents', {})
    functionality = parsed_data.get('functionality', {})
    audience = parsed_data.get('audience', {})

    if physical:
        product_info['dimensions'] = physical
    if color:
        colors = [color.get('primary_color', '')]
        if color.get('secondary_colors'):
            colors.extend(color['secondary_colors'])
        product_info['colors'] = [c for c in colors if c]
    if materials.get('materials_list'):
        product_info['materials'] = materials['materials_list']

    # Собираем уже распарсенные характеристики, чтобы AI их переиспользовал
    pre_chars = {}
    if physical.get('diameter_cm') or physical.get('max_diameter_cm'):
        pre_chars['диаметр'] = physical.get('max_diameter_cm') or physical.get('diameter_cm')
    if physical.get('working_length_cm'):
        pre_chars['рабочая длина'] = physical['working_length_cm']
    if physical.get('length_cm'):
        pre_chars['длина'] = physical['length_cm']
    if physical.get('width_cm'):
        pre_chars['ширина'] = physical['width_cm']
    if physical.get('weight_g'):
        pre_chars['вес'] = physical['weight_g']
    if functionality.get('special_features'):
        pre_chars['особенности'] = functionality['special_features']
    if contents.get('package_contents'):
        pre_chars['комплектация'] = ', '.join(contents['package_contents'])
    if audience.get('gender'):
        pre_chars['пол'] = audience['gender']
    if pre_chars:
        product_info['characteristics'] = pre_chars

    success, result, error = task.execute_two_pass(
        product=product, product_info=product_info, original_data=parsed_data
    )

    if success and result:
        # Убираем метаданные из результата перед сохранением
        clean_fields = {k: v for k, v in result.items() if k != '_meta'}

        # Post-fill: досыпаем значения из physical/functionality для полей,
        # которые AI не заполнил во втором проходе, но данные уже есть.
        # Маппинг generic → category-specific через fuzzy-match по имени характеристики.
        _post_fill_marketplace_fields(clean_fields, parsed_data, characteristics)

        # Сохраняем плоские поля для формы (массивы → строки через ";")
        flat_fields = {}
        for k, v in clean_fields.items():
            if isinstance(v, list):
                flat_fields[k] = '; '.join(str(x) for x in v) if v else ''
            elif v is not None:
                flat_fields[k] = v
        product.marketplace_fields_json = json.dumps(flat_fields, ensure_ascii=False)

        # Мержим AI-specific поля в ai_marketplace_json для валидатора
        existing_mp = product.get_ai_marketplace_data()
        existing_mp.update(clean_fields)
        product.ai_marketplace_json = json.dumps(existing_mp, ensure_ascii=False)

        # Валидируем (обновит marketplace_fields_json, validation_status, fill_pct)
        validation_result = MarketplaceValidator.validate_product_for_marketplace(product, wb_marketplace.id)

        logger.info(f"Marketplace aware parse success for {product.id}, status: {validation_result.get('validation_status')}")
    else:
        # Даже если AI второй проход не сработал — пробуем заполнить из physical данных
        _post_fill_marketplace_fields_fallback(product, parsed_data, characteristics)
        logger.error(f"Marketplace aware parse failed for {product.id}: {error}")


# ============================================================================
# POST-FILL: физические данные → WB-характеристики
# ============================================================================

# Маппинг ключевых слов в именах WB характеристик на physical/functionality поля.
# Ключ — подстрока (lowercase) в имени WB характеристики.
# Значение — (секция parsed_data, поле, [альтернативные поля]).
_PHYSICAL_TO_WB_KEYWORDS = [
    # Диаметр
    (['диаметр'], 'physical', ['max_diameter_cm', 'diameter_cm']),
    # Длина
    (['общая длина', 'длина'], 'physical', ['working_length_cm', 'length_cm']),
    # Рабочая длина (более специфичная)
    (['рабочая длина'], 'physical', ['working_length_cm']),
    # Ширина
    (['ширина'], 'physical', ['width_cm']),
    # Вес
    (['вес'], 'physical', ['weight_g']),
    # Объём
    (['объем', 'объём'], 'physical', ['volume_ml']),
    # Глубина
    (['глубина'], 'physical', ['depth_cm']),
    # Толщина
    (['толщина'], 'physical', ['thickness_cm']),
    # Особенности
    (['особенности', 'особенность'], 'functionality', ['special_features']),
]


def _post_fill_marketplace_fields(
    fields: dict,
    parsed_data: dict,
    characteristics: list,
) -> None:
    """
    Досыпает значения в marketplace fields из уже распарсенных физических данных.

    Если AI во втором проходе не заполнил поле (например "Диаметр секс игрушки (см)"),
    но в parsed_data.physical.diameter_cm есть значение — подставляем его.

    Работает по fuzzy-match ключевых слов в именах WB характеристик.
    """
    for charc in characteristics:
        if not charc.is_enabled:
            continue
        name = charc.name
        # Уже заполнено AI
        if name in fields and fields[name] not in (None, '', [], '[]'):
            continue

        name_lower = name.lower()

        for keywords, section_key, field_names in _PHYSICAL_TO_WB_KEYWORDS:
            if not any(kw in name_lower for kw in keywords):
                continue

            section = parsed_data.get(section_key, {})
            value = None
            for fn in field_names:
                value = section.get(fn)
                if value is not None:
                    break

            if value is None:
                continue

            # Тип 4 (NUMBER) — нужно число
            if charc.charc_type == 4:
                if isinstance(value, (int, float)):
                    fields[name] = value
                    logger.debug(f"[PostFill] {name} = {value} (from {section_key}.{field_names})")
                elif isinstance(value, str):
                    match = re.search(r'([\d]+[.,]?[\d]*)', value.replace(',', '.'))
                    if match:
                        num = float(match.group(1))
                        fields[name] = int(num) if num == int(num) else num
            # Тип 1 (STRING ARRAY) — массив строк
            elif charc.charc_type == 1:
                if isinstance(value, list):
                    str_values = [str(v) for v in value if v]
                    if str_values:
                        fields[name] = str_values
                elif isinstance(value, str) and value:
                    fields[name] = [value]
            else:
                # Другие типы — строка
                if isinstance(value, list):
                    fields[name] = '; '.join(str(v) for v in value if v)
                elif value:
                    fields[name] = value

            break  # Одно совпадение на характеристику


def _post_fill_marketplace_fields_fallback(
    product: SupplierProduct,
    parsed_data: dict,
    characteristics: list,
) -> None:
    """
    Fallback: если _run_marketplace_aware_parse полностью провалился,
    заполняем marketplace_fields_json напрямую из physical данных.
    """
    try:
        existing = {}
        if product.marketplace_fields_json:
            try:
                existing = json.loads(product.marketplace_fields_json)
            except (json.JSONDecodeError, TypeError):
                pass

        _post_fill_marketplace_fields(existing, parsed_data, characteristics)

        if existing:
            # Flatten lists to strings for form
            flat = {}
            for k, v in existing.items():
                if isinstance(v, list):
                    flat[k] = '; '.join(str(x) for x in v) if v else ''
                elif v is not None:
                    flat[k] = v
            product.marketplace_fields_json = json.dumps(flat, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"[PostFill fallback] Error: {e}")


# ============================================================================
# PRIVATE HELPERS
# ============================================================================

def _create_supplier_product(supplier_id: int, data: dict,
                             price_data: Dict[str, float] = None) -> SupplierProduct:
    """Создать SupplierProduct из парсерных данных"""
    sp = SupplierProduct(supplier_id=supplier_id)
    _update_supplier_product(sp, data, price_data)
    return sp


def _update_supplier_product(sp: SupplierProduct, data: dict,
                             price_data: Dict[str, float] = None) -> None:
    """Обновить SupplierProduct из парсерных данных"""
    sp.external_id = data.get('external_id', sp.external_id)
    sp.vendor_code = data.get('vendor_code', sp.vendor_code)
    sp.title = data.get('title', sp.title)
    sp.description = data.get('description', sp.description)
    sp.brand = data.get('brand', sp.brand)
    sp.category = data.get('category', sp.category)
    sp.country = data.get('country', sp.country)
    sp.gender = data.get('gender', sp.gender)

    # JSON поля
    if 'all_categories' in data:
        sp.all_categories = json.dumps(data['all_categories'], ensure_ascii=False)
    if 'colors' in data:
        sp.colors_json = json.dumps(data['colors'], ensure_ascii=False)
    if 'materials' in data:
        sp.materials_json = json.dumps(data['materials'], ensure_ascii=False)
    if 'barcodes' in data:
        sp.barcode = data['barcodes'][0] if data['barcodes'] else sp.barcode
        if data['barcodes']:
            sp.barcodes_json = json.dumps(data['barcodes'], ensure_ascii=False)
    if 'photo_urls' in data:
        sp.photo_urls_json = json.dumps(data['photo_urls'], ensure_ascii=False)
    if 'sizes_raw' in data and data['sizes_raw']:
        sp.sizes_json = json.dumps({'raw': data['sizes_raw']}, ensure_ascii=False)

    # Видео поставщика (прямая/embed ссылка)
    video_url = data.get('video_url')
    if isinstance(video_url, str) and video_url.startswith('http'):
        sp.video_url = video_url[:500]

    # Наблюдённые данные поставщика: освежаются каждым sync, включая
    # raw_extra с несмаппленными колонками фида. Источник observed-фактов
    # для marketplace fact pack (Ozon) и локального отката.
    sp.original_data_json = json.dumps(data, ensure_ascii=False, default=str)

    # Цена из price_data (если есть)
    if price_data and sp.external_id:
        ext_id = sp.external_id
        # Пробуем найти цену по числовому ID
        numeric_id = extract_supplier_product_id(ext_id)
        if numeric_id is not None:
            price = price_data.get(numeric_id) or price_data.get(str(numeric_id)) or price_data.get(ext_id)
            if price:
                sp.supplier_price = price

    if 'supplier_price' in data and data['supplier_price']:
        sp.supplier_price = data['supplier_price']

    # Остатки из CSV (суммированные или прямые)
    if 'supplier_quantity' in data and data['supplier_quantity'] is not None:
        sp.supplier_quantity = data['supplier_quantity']

    # РРЦ из CSV; fallback-колонка (у andrey — retail_price_minsk) заполняет
    # РРЦ только когда основная колонка пуста
    if data.get('recommended_retail_price'):
        sp.recommended_retail_price = data['recommended_retail_price']
    elif data.get('recommended_retail_price_fallback'):
        sp.recommended_retail_price = data['recommended_retail_price_fallback']

    # Характеристики из CSV (составной тип characteristics).
    # Свежий парс перестраивает JSON целиком (условная перезапись оставляла
    # однажды загрязнённые данные навсегда), а dimension-образные имена
    # («Ширина упаковки, см» и т.п.) уходят в dimensions_json: в WB это
    # отдельный объект габаритов карточки, а не характеристики.
    extracted_dims_from_chars = {}
    if 'characteristics' in data:
        raw_chars = data['characteristics'] or {}
        if isinstance(raw_chars, dict):
            from services.wb_content_payload import extract_characteristics
            extraction = extract_characteristics(raw_chars)
            clean_chars = extraction.values
            # dimensions_json хранит наблюдённый dict «имя: значение» фида,
            # поэтому переносим под исходными именами колонок.
            extracted_dims_from_chars = {
                name: raw_chars[name]
                for name in extraction.dropped
                if name in raw_chars
            }
        else:
            clean_chars = raw_chars
        sp.characteristics_json = (
            json.dumps(clean_chars, ensure_ascii=False) if clean_chars else None
        )

    # Габариты упаковки из CSV (составной тип dimensions); явная колонка
    # габаритов сильнее значений, извлечённых из характеристик.
    if ('dimensions' in data and data['dimensions']) or extracted_dims_from_chars:
        dims = dict(extracted_dims_from_chars)
        explicit_dims = data.get('dimensions')
        if isinstance(explicit_dims, dict):
            dims.update(explicit_dims)
        sp.dimensions_json = json.dumps(dims, ensure_ascii=False)

    # Контент хеш для отслеживания изменений
    content_str = f"{sp.title}|{sp.brand}|{sp.category}|{sp.supplier_price}"
    sp.content_hash = hashlib.md5(content_str.encode()).hexdigest()

    # WB категория: SmartCategoryMapper с контекстным анализом
    try:
        from services.smart_category_mapper import SmartCategoryMapper
        all_cats = data.get('all_categories', [])
        subj_id, subj_name, cat_conf = SmartCategoryMapper.map_category(
            csv_category=data.get('category', ''),
            product_title=data.get('title', ''),
            brand=data.get('brand', ''),
            description=data.get('description', ''),
            all_categories=all_cats,
            external_id=data.get('external_id', ''),
            source_type=sp.supplier.code if sp.supplier else 'unknown',
        )
        sp.wb_subject_id = subj_id
        sp.wb_subject_name = subj_name
        sp.wb_category_name = subj_name
        sp.category_confidence = cat_conf

        # Валидация: убеждаемся что категория существует в БД маркетплейса
        if subj_id and cat_conf < 0.9:
            try:
                from models import MarketplaceCategory, Marketplace
                wb_mp = Marketplace.query.filter_by(code='wb').first()
                if wb_mp:
                    db_cat = MarketplaceCategory.query.filter_by(
                        marketplace_id=wb_mp.id,
                        subject_id=subj_id,
                        is_enabled=True,
                    ).first()
                    if not db_cat:
                        # Категория не найдена в БД — ищем ближайшую по имени
                        from difflib import SequenceMatcher
                        if subj_name:
                            best_match = None
                            best_ratio = 0.0
                            for c in MarketplaceCategory.query.filter_by(
                                marketplace_id=wb_mp.id, is_enabled=True
                            ).all():
                                ratio = SequenceMatcher(
                                    None, subj_name.lower(), c.subject_name.lower()
                                ).ratio()
                                if ratio > best_ratio and ratio >= 0.7:
                                    best_ratio = ratio
                                    best_match = c
                            if best_match:
                                sp.wb_subject_id = best_match.subject_id
                                sp.wb_subject_name = best_match.subject_name
                                sp.wb_category_name = best_match.subject_name
                                sp.category_confidence = round(cat_conf * best_ratio, 3)
                                logger.debug(
                                    f"Import category fix: {subj_name} → "
                                    f"{best_match.subject_name} (ratio={best_ratio:.2f})"
                                )
            except Exception as e:
                logger.debug(f"Category DB validation during import failed: {e}")
    except Exception as e:
        logger.debug(f"SmartCategoryMapper failed: {e}")

    # Расчёт confidence score
    try:
        from services.parsing_confidence import ParsingConfidenceScorer
        sp.parsing_confidence = ParsingConfidenceScorer.score_product(data)
        sp.normalization_applied = True
    except Exception:
        pass


def _supplier_characteristics_payload(sp: SupplierProduct) -> Optional[str]:
    """Merge source characteristics with exact WB fields from the shared card."""
    raw = []
    if sp.characteristics_json:
        try:
            parsed = json.loads(sp.characteristics_json)
            if isinstance(parsed, dict):
                raw = [
                    {'name': str(name), 'value': value}
                    for name, value in parsed.items()
                    if value not in (None, '', [])
                ]
            elif isinstance(parsed, list):
                raw = [item for item in parsed if isinstance(item, dict)]
        except (TypeError, ValueError):
            raw = []

    marketplace_fields = {}
    if sp.ai_marketplace_json:
        try:
            parsed = json.loads(sp.ai_marketplace_json)
            meta = parsed.get('_meta') if isinstance(parsed, dict) else None
            if (
                isinstance(parsed, dict)
                and isinstance(meta, dict)
                and meta.get('source') == 'supplier_catalog_enrichment'
            ):
                marketplace_fields = {
                    str(name): value for name, value in parsed.items()
                    if not str(name).startswith('_')
                    and value not in (None, '', [])
                }
                if meta.get('workflow') == 'codex_luna_v2':
                    from services.supplier_luna_enrichment import source_matches
                    if not source_matches(sp, meta):
                        marketplace_fields = {}
        except (TypeError, ValueError):
            marketplace_fields = {}

    positions = {
        str(item.get('name')).strip().casefold(): index
        for index, item in enumerate(raw)
        if item.get('name')
    }
    for name, value in marketplace_fields.items():
        key = name.strip().casefold()
        normalized = {'name': name, 'value': value}
        if key in positions:
            raw[positions[key]] = normalized
        else:
            positions[key] = len(raw)
            raw.append(normalized)
    return json.dumps(raw, ensure_ascii=False) if raw else None


def _supplier_barcodes_payload(sp: SupplierProduct) -> Optional[str]:
    """Полный список штрихкодов поставщика как JSON list.

    Fallback — единичный legacy sp.barcode."""
    if sp.barcodes_json:
        try:
            parsed = json.loads(sp.barcodes_json)
            if isinstance(parsed, list) and parsed:
                return json.dumps(parsed, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    if sp.barcode:
        return json.dumps([sp.barcode], ensure_ascii=False)
    return None


def _supplier_observed_snapshot(sp: SupplierProduct) -> Optional[str]:
    """Return the complete observed supplier snapshot used by channel drafts.

    Older feed integrations stored normalized photos, dimensions and
    characteristics in their dedicated columns but left those fields out of
    ``original_data_json``.  Marketplace fact packs intentionally read the
    seller's copied source snapshot, so that omission made otherwise complete
    products look empty to Ozon.  Merge only missing observed fields here;
    never copy AI/marketplace suggestions and never replace a value already
    present in the raw source snapshot.
    """

    try:
        original = json.loads(sp.original_data_json or "{}")
    except (TypeError, ValueError):
        original = {}
    if not isinstance(original, dict):
        original = {}

    def parsed_json(raw_value, expected_type):
        if isinstance(raw_value, expected_type):
            return raw_value
        try:
            value = json.loads(raw_value or "")
        except (TypeError, ValueError):
            return expected_type()
        return value if isinstance(value, expected_type) else expected_type()

    characteristics = parsed_json(sp.characteristics_json, dict)
    if not characteristics:
        characteristics = parsed_json(sp.characteristics_json, list)
    additions = {
        "photo_urls": parsed_json(sp.photo_urls_json, list),
        "dimensions": parsed_json(sp.dimensions_json, dict),
        "characteristics": characteristics,
        "barcodes": parsed_json(sp.barcodes_json, list),
    }
    if not additions["barcodes"] and sp.barcode:
        additions["barcodes"] = [sp.barcode]
    if sp.recommended_retail_price is not None:
        additions["recommended_retail_price"] = sp.recommended_retail_price

    for key, value in additions.items():
        if original.get(key) in (None, "", [], {}) and value not in (
            None,
            "",
            [],
            {},
        ):
            original[key] = value
    if not original:
        return None
    return json.dumps(
        original,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def hydrate_missing_imported_observed_snapshot(
    imp: ImportedProduct,
) -> bool:
    """Fill only absent source facts from the exact linked supplier row.

    This is used by an explicitly confirmed Ozon synchronization to repair
    legacy ImportedProduct snapshots that predate full photo/dimension/
    characteristic copying.  Existing snapshot values and seller-owned card
    fields are never replaced, and ``supplier_content_revision`` is deliberately
    left unchanged because this is not a full supplier-card refresh.
    """
    if (
        not isinstance(imp, ImportedProduct)
        or imp.supplier_product_id is None
        or imp.supplier_product is None
        or imp.supplier_product.id != imp.supplier_product_id
    ):
        return False
    raw_supplier = _supplier_observed_snapshot(imp.supplier_product)
    if not raw_supplier:
        return False
    try:
        supplier_snapshot = json.loads(raw_supplier)
    except (TypeError, ValueError):
        return False
    if not isinstance(supplier_snapshot, dict):
        return False
    try:
        current = json.loads(imp.original_data or "{}")
    except (TypeError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}

    changed = False
    for key, value in supplier_snapshot.items():
        if (
            current.get(key) in (None, "", [], {})
            and value not in (None, "", [], {})
        ):
            current[key] = value
            changed = True
    if not changed:
        return False
    imp.original_data = json.dumps(
        current,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return True


def _copy_to_imported_product(seller_id: int, sp: SupplierProduct) -> ImportedProduct:
    """Копировать данные из SupplierProduct в новый ImportedProduct"""
    imp = ImportedProduct(
        seller_id=seller_id,
        supplier_product_id=sp.id,
        supplier_id=sp.supplier_id,
        supplier_content_revision=int(sp.content_revision or 1),
        external_id=sp.external_id,
        external_vendor_code=sp.vendor_code,
        source_type=sp.supplier.code if sp.supplier else 'unknown',
        title=sp.title,
        description=sp.description or sp.ai_description or '',
        brand=sp.brand,
        category=sp.category,
        all_categories=sp.all_categories,
        mapped_wb_category=sp.wb_category_name,
        wb_subject_id=sp.wb_subject_id,
        category_confidence=sp.category_confidence or 0.0,
        country=sp.country,
        gender=sp.gender,
        colors=sp.colors_json,
        sizes=sp.sizes_json,
        materials=sp.materials_json,
        photo_urls=sp.photo_urls_json,
        processed_photos=sp.processed_photos_json,
        barcodes=_supplier_barcodes_payload(sp),
        characteristics=_supplier_characteristics_payload(sp),
        supplier_price=sp.supplier_price,
        recommended_retail_price=sp.recommended_retail_price,
        supplier_quantity=sp.supplier_quantity,
        import_status='pending',
        original_data=_supplier_observed_snapshot(sp),
    )

    # Копируем resolved brand
    if sp.resolved_brand_id:
        imp.resolved_brand_id = sp.resolved_brand_id
        imp.brand_status = 'exact'
    elif sp.brand:
        imp.brand_status = 'unresolved'

    # Копируем AI данные если есть
    if sp.ai_seo_title:
        imp.ai_seo_title = sp.ai_seo_title
    if sp.ai_keywords_json:
        imp.ai_keywords = sp.ai_keywords_json
    if sp.ai_bullets_json:
        imp.ai_bullets = sp.ai_bullets_json
    if sp.ai_rich_content_json:
        imp.ai_rich_content = sp.ai_rich_content_json
    if sp.ai_analysis_json:
        imp.ai_analysis = sp.ai_analysis_json
        imp.ai_analysis_at = sp.ai_validated_at

    return imp


def _update_imported_from_supplier(imp: ImportedProduct, sp: SupplierProduct) -> None:
    """Обновить ImportedProduct из SupplierProduct (sync)"""
    from services.common_product_content import (
        content_writer_guard_snapshot,
        guard_content_writer,
        refresh_from_source,
    )
    expected_common_state = None
    if imp.id is not None:
        expected_common_state = content_writer_guard_snapshot(imp)
        guard_content_writer(imp, expected=expected_common_state)
    category_changed = imp.wb_subject_id != sp.wb_subject_id
    imp.brand = sp.brand or imp.brand
    if sp.resolved_brand_id and not imp.resolved_brand_id:
        imp.resolved_brand_id = sp.resolved_brand_id
        imp.brand_status = 'exact'
    imp.category = sp.category or imp.category
    imp.all_categories = sp.all_categories or imp.all_categories
    imp.mapped_wb_category = sp.wb_category_name
    imp.wb_subject_id = sp.wb_subject_id
    imp.category_confidence = sp.category_confidence or 0.0
    imp.country = sp.country or imp.country
    imp.gender = sp.gender or imp.gender
    imp.colors = sp.colors_json or imp.colors
    imp.sizes = sp.sizes_json or imp.sizes
    imp.materials = sp.materials_json or imp.materials
    shared_characteristics = _supplier_characteristics_payload(sp)
    if category_changed:
        imp.ai_attributes = None
    imp.supplier_price = sp.supplier_price if sp.supplier_price is not None else imp.supplier_price
    imp.supplier_quantity = sp.supplier_quantity if sp.supplier_quantity is not None else imp.supplier_quantity
    if sp.recommended_retail_price is not None:
        imp.recommended_retail_price = sp.recommended_retail_price
    barcodes_payload = _supplier_barcodes_payload(sp)
    if barcodes_payload:
        imp.barcodes = barcodes_payload
    # Свежие наблюдённые данные поставщика (raw_extra, габариты, РРЦ и т.д.)
    observed_snapshot = _supplier_observed_snapshot(sp)
    if observed_snapshot:
        imp.original_data = observed_snapshot

    # Common-content manual values stay seller-owned while the supplier's
    # observed snapshot and inherited baseline continue to refresh.
    try:
        source_snapshot = json.loads(observed_snapshot or imp.original_data or "{}")
    except (TypeError, ValueError, RecursionError):
        source_snapshot = {}
    if not isinstance(source_snapshot, dict):
        source_snapshot = {}
    source_values = {}
    provenance = {}
    if sp.title is not None:
        source_values["title"] = sp.title
        provenance["title"] = "source"
    elif source_snapshot.get("title") not in (None, "") or "title" in source_snapshot:
        source_values["title"] = source_snapshot.get("title")
        provenance["title"] = "source"
    if sp.description not in (None, ""):
        source_values["description"] = sp.description
        description_source = str(sp.description_source or "").strip().casefold()
        if description_source in {"ai", "ai_generated", "generated"}:
            provenance["description"] = "ai_suggestion"
        elif description_source in {"manual", "csv"}:
            provenance["description"] = "supplier_enrichment"
        else:
            provenance["description"] = "source"
    elif sp.ai_description not in (None, ""):
        source_values["description"] = sp.ai_description
        provenance["description"] = "ai_suggestion"
    elif source_snapshot.get("description") not in (None, ""):
        source_values["description"] = source_snapshot["description"]
        provenance["description"] = "source"

    if sp.photo_urls_json not in (None, ""):
        source_values["photos"] = sp.photo_urls_json
        provenance["photos"] = "source"
    elif source_snapshot.get("photo_urls") not in (None, "", [], {}):
        source_values["photos"] = source_snapshot["photo_urls"]
        provenance["photos"] = "source"

    # The previous supplier sync intentionally cleared characteristics when
    # the shared payload disappeared (for example, after a reviewed category
    # rollback). Keep that behavior while recording a null source baseline;
    # never fall back to ImportedProduct's possibly manual effective value.
    source_values["characteristics"] = shared_characteristics
    characteristics_origin = "source"
    if shared_characteristics not in (None, ""):
        try:
            marketplace_data = json.loads(sp.ai_marketplace_json or "{}")
        except (TypeError, ValueError, RecursionError):
            marketplace_data = {}
        meta = marketplace_data.get("_meta") if isinstance(marketplace_data, dict) else None
        characteristics_origin = (
            "supplier_enrichment"
            if isinstance(meta, dict)
            and meta.get("source") == "supplier_catalog_enrichment"
            else "source"
        )
    provenance["characteristics"] = characteristics_origin
    refresh_from_source(
        imp,
        source_values,
        provenance=provenance,
        observed_fields={"photos", "characteristics"},
    )

    if sp.processed_photos_json:
        imp.processed_photos = sp.processed_photos_json

    # AI данные — только если продавец не генерировал свои
    if sp.ai_seo_title and not imp.ai_seo_title:
        imp.ai_seo_title = sp.ai_seo_title
    if sp.ai_keywords_json and not imp.ai_keywords:
        imp.ai_keywords = sp.ai_keywords_json
    if sp.ai_bullets_json and not imp.ai_bullets:
        imp.ai_bullets = sp.ai_bullets_json

    imp.supplier_content_revision = int(sp.content_revision or 1)
    imp.updated_at = datetime.utcnow()


def _notify_products_without_photos(seller_id: int, product_ids: List[int]) -> None:
    """Проверить импортированные товары на наличие фотографий и уведомить продавца.

    Дедупликация: не чаще 1 раза в час для одного продавца.
    """
    if not product_ids:
        return

    # Дедупликация — не спамим
    from models import Notification
    from datetime import timedelta
    recent_cutoff = datetime.utcnow() - timedelta(hours=1)
    already_sent = Notification.query.filter(
        Notification.seller_id == seller_id,
        Notification.title.like('%без фотографий%'),
        Notification.created_at >= recent_cutoff,
    ).first()
    if already_sent:
        return

    products = ImportedProduct.query.filter(
        ImportedProduct.id.in_(product_ids),
        ImportedProduct.seller_id == seller_id,
    ).all()

    no_photo_products = []
    for p in products:
        has_photos = False
        if p.photo_urls:
            try:
                urls = json.loads(p.photo_urls) if isinstance(p.photo_urls, str) else p.photo_urls
                if isinstance(urls, list) and len(urls) > 0:
                    has_photos = True
            except (json.JSONDecodeError, TypeError):
                pass
        if not has_photos:
            no_photo_products.append(p)

    if not no_photo_products:
        return

    count = len(no_photo_products)
    examples = [p.title or p.external_id or f"ID {p.id}" for p in no_photo_products[:5]]
    examples_text = ", ".join(f'"{e}"' for e in examples)
    if count > 5:
        examples_text += f" и ещё {count - 5}"

    from seller_platform import create_notification
    create_notification(
        seller_id=seller_id,
        category='warning',
        title=f'{count} товар(ов) без фотографий',
        message=(
            f'После импорта обнаружены товары без фото: {examples_text}. '
            f'Товары без фотографий не могут быть загружены на Wildberries. '
            f'Перейдите в «Мои товары» чтобы загрузить фотографии.'
        ),
        link='/my-products?status=pending',
        metadata={'no_photo_product_ids': [p.id for p in no_photo_products]},
    )
