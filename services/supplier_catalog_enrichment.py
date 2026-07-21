# -*- coding: utf-8 -*-
"""Safe durable bulk enrichment of shared supplier product cards.

The model never writes and never supplies free-form marketplace identities.
Python owns reference lookup, exact-set validation and the final DB commit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import (
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    Supplier,
    SupplierCatalogEnrichmentItem,
    SupplierCatalogEnrichmentRun,
    SupplierProduct,
    db,
)

logger = logging.getLogger(__name__)

REFERENCE_MAX_AGE_HOURS = 48
MAX_SELECTION = 10_000
MAX_CHARACTERISTIC_SELECTION = 5_000
CATEGORY_BATCH_SIZE = 20
CHARACTERISTIC_BATCH_SIZE = 6
MAX_BATCHES_PER_TICK = 3
MAX_ITEM_ATTEMPTS = 3
MAX_LLM_CALLS = 1_600
# Отдельный режим предположений: модель видит schema + source + заполненное
# и предлагает значения незаполненных словарных полей. Только needs_review.
MODE_INFERENCE = 'characteristics_inference'
AUTO_APPLY_CONFIDENCE = 0.92
AUTO_APPLY_SOURCE_MATCH = 0.90
ACTIVE_RUN_STATUSES = ('pending', 'running', 'cancelling')


def _llm_concurrency() -> int:
    """Число параллельных LLM-вызовов внутри одного processing batch.

    Управляет только числом одновременных HTTP-вызовов; размер и содержимое
    каждого чанка не меняются, и каждый вызов по-прежнему резервируется в
    llm-бюджете run-а по одному. Default 1 — последовательный путь.
    """
    raw = os.environ.get('SUPPLIER_ENRICHMENT_LLM_CONCURRENCY', '1')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(value, 8))
TERMINAL_ITEM_STATUSES = (
    'applied', 'unchanged', 'needs_review', 'failed', 'cancelled',
    'rolled_back', 'rollback_conflict',
)

_RU_ENDINGS = (
    'иями', 'ями', 'ами', 'ого', 'ему', 'ому', 'ыми', 'ими', 'ая', 'яя',
    'ое', 'ее', 'ые', 'ие', 'ый', 'ий', 'ой', 'ов', 'ев', 'ам', 'ям',
    'ах', 'ях', 'ом', 'ем', 'у', 'ю', 'а', 'я', 'ы', 'и', 'о', 'е',
)
_STOPWORDS = {
    'для', 'или', 'это', 'товар', 'товары', 'набор', 'комплект', 'шт',
    'цвет', 'размер', 'новый', 'женский', 'мужской', 'универсальный',
}
_SENSITIVE_SOURCE_KEYS = re.compile(
    r'(?:token|secret|password|api.?key|credential|cookie|authorization)',
    re.IGNORECASE,
)


class SupplierCatalogEnrichmentError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _json_dump(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        default=str,
    )


def _json_load(value: Any, fallback: Any) -> Any:
    if value in (None, ''):
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _hash(value: Any) -> str:
    return hashlib.sha256(_json_dump(value).encode('utf-8')).hexdigest()


def _normalized(value: Any) -> str:
    text = str(value or '').casefold().replace('ё', 'е')
    text = re.sub(r'[^\w\s]+', ' ', text, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', text).strip()


def _stem(word: str) -> str:
    if len(word) <= 3:
        return word
    for ending in _RU_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[:-len(ending)]
    return word


def _tokens(value: Any) -> set[str]:
    return {
        _stem(word) for word in _normalized(value).split()
        if len(word) >= 3 and word not in _STOPWORDS
    }


def _bounded_text(value: Any, limit: int) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _public_failure(label: str, exc: Exception) -> str:
    """Bounded error without provider bodies, prompts, credentials or facts."""
    return f'{label} ({type(exc).__name__}). Состояние строки сохранено безопасно.'


def _safe_source_value(value: Any, depth: int = 0) -> Any:
    """Bound supplier-owned source facts before they enter a prompt."""
    if depth > 3:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value, 700)
    if isinstance(value, list):
        return [
            cleaned for cleaned in (
                _safe_source_value(item, depth + 1) for item in value[:30]
            ) if cleaned not in (None, '', [], {})
        ]
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:40]:
            key = _bounded_text(key, 100)
            if not key or _SENSITIVE_SOURCE_KEYS.search(key):
                continue
            cleaned = _safe_source_value(item, depth + 1)
            if cleaned not in (None, '', [], {}):
                result[key] = cleaned
        return result
    return _bounded_text(value, 300)


def _product_source(product: SupplierProduct) -> Dict[str, Any]:
    source = {
        'product_id': int(product.id),
        'title': _bounded_text(product.title, 500),
        'description': _bounded_text(product.description, 3000),
        'brand': _bounded_text(product.brand, 200),
        'supplier_category': _bounded_text(product.category, 500),
        'all_categories': _safe_source_value(
            _json_load(product.all_categories, []),
        ),
        'characteristics': _safe_source_value(
            _json_load(product.characteristics_json, []),
        ),
        'colors': _safe_source_value(_json_load(product.colors_json, [])),
        'materials': _safe_source_value(
            _json_load(product.materials_json, []),
        ),
        'sizes': _safe_source_value(_json_load(product.sizes_json, {})),
        'dimensions': _safe_source_value(
            _json_load(product.dimensions_json, {}),
        ),
        'gender': _bounded_text(product.gender, 50),
        'country': _bounded_text(product.country, 100),
        'season': _bounded_text(product.season, 50),
        'age_group': _bounded_text(product.age_group, 50),
    }
    return {
        key: value for key, value in source.items()
        if value not in (None, '', [], {})
    }


def _source_fingerprint(product: SupplierProduct) -> str:
    return _hash(_product_source(product))


def _source_blob(source: Dict[str, Any]) -> str:
    return _normalized(_json_dump(source))


def _evidence_is_grounded(evidence: Any, source: Dict[str, Any]) -> bool:
    evidence_norm = _normalized(evidence)
    if not evidence_norm:
        return False
    if len(evidence_norm) < 3 and not evidence_norm.isdigit():
        return False
    return evidence_norm in _source_blob(source)


def _characteristic_value_is_grounded(
    value: Any, evidence: Any, source: Dict[str, Any],
) -> bool:
    """Require every proposed scalar, not only its field quote, in source."""
    if not _evidence_is_grounded(evidence, source):
        return False
    values = value if isinstance(value, list) else [value]
    if not values:
        return False
    evidence_blob = _normalized(evidence)
    source_blob = _source_blob(source)
    for scalar in values:
        if isinstance(scalar, bool) or not isinstance(
            scalar, (str, int, float)
        ):
            return False
        normalized = _normalized(scalar)
        if not normalized or (
            normalized not in evidence_blob and normalized not in source_blob
        ):
            return False
    return True


def _enrichment_state(product: SupplierProduct) -> Dict[str, Any]:
    return {
        'wb_subject_id': product.wb_subject_id,
        'wb_subject_name': product.wb_subject_name,
        'wb_category_name': product.wb_category_name,
        'category_confidence': product.category_confidence,
        'ai_marketplace_json': _json_load(product.ai_marketplace_json, {}),
        'marketplace_fields_json': _json_load(
            product.marketplace_fields_json, {},
        ),
        'marketplace_validation_status': product.marketplace_validation_status,
        'marketplace_fill_pct': product.marketplace_fill_pct,
        'content_revision': int(product.content_revision or 1),
    }


def _target_fingerprint(product: SupplierProduct) -> str:
    state = _enrichment_state(product)
    state.pop('content_revision', None)
    return _hash(state)


def _extract_json_object(response: Any) -> Dict[str, Any]:
    if not isinstance(response, str) or not response.strip():
        raise ValueError('Модель вернула пустой ответ')
    text = response.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        match = re.search(r'\{[\s\S]*\}', text)
        if not match:
            raise ValueError('Ответ модели не содержит JSON-объект')
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError('Ответ модели должен быть JSON-объектом')
    return data


class SupplierCatalogEnrichmentService:
    """Admin-only orchestration; routes own authentication and audit logs."""

    @staticmethod
    def reference_status() -> Dict[str, Any]:
        marketplace = Marketplace.query.filter_by(code='wb').first()
        leaf_count = 0
        enabled_leaf_count = 0
        if marketplace:
            base = MarketplaceCategory.query.filter(
                MarketplaceCategory.marketplace_id == marketplace.id,
                MarketplaceCategory.is_leaf.is_(True),
                MarketplaceCategory.is_available.is_(True),
            )
            leaf_count = base.count()
            enabled_leaf_count = base.filter(
                MarketplaceCategory.is_enabled.is_(True),
            ).count()
        fresh = bool(
            marketplace
            and marketplace.categories_synced_at
            and marketplace.categories_synced_at
            >= datetime.utcnow() - timedelta(hours=REFERENCE_MAX_AGE_HOURS)
        )
        usable = bool(
            marketplace
            and marketplace.is_active
            and marketplace.categories_sync_status == 'success'
            and fresh
            and enabled_leaf_count > 0
        )
        reason = None
        if not marketplace:
            reason = 'marketplace_not_found'
        elif not marketplace.is_active:
            reason = 'marketplace_inactive'
        elif marketplace.categories_sync_status != 'success':
            reason = 'sync_not_successful'
        elif not fresh:
            reason = 'reference_stale'
        elif not enabled_leaf_count:
            reason = 'no_enabled_leaf_categories'
        return {
            'usable': usable,
            'reason': reason,
            'marketplace_id': marketplace.id if marketplace else None,
            'synced_at': (
                marketplace.categories_synced_at.isoformat()
                if marketplace and marketplace.categories_synced_at else None
            ),
            'version': int(marketplace.categories_version or 0)
            if marketplace else 0,
            'snapshot_hash': marketplace.categories_snapshot_hash
            if marketplace else None,
            'leaf_count': leaf_count,
            'enabled_leaf_count': enabled_leaf_count,
            'max_age_hours': REFERENCE_MAX_AGE_HOURS,
        }

    @classmethod
    def require_reference(cls) -> Dict[str, Any]:
        status = cls.reference_status()
        if not status['usable']:
            raise SupplierCatalogEnrichmentError(
                'wb_reference_unavailable',
                'Справочник конечных категорий WB устарел или недоступен. '
                'Сначала синхронизируйте его в «Маркетплейсы → Категории».',
            )
        return status

    @staticmethod
    def selection_query(
        supplier_id: int, filters: Optional[Dict[str, Any]] = None,
    ):
        filters = filters or {}
        query = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
        )
        search = _bounded_text(filters.get('search'), 200)
        if search:
            term = f'%{search}%'
            query = query.filter(db.or_(
                SupplierProduct.title.ilike(term),
                SupplierProduct.external_id.ilike(term),
                SupplierProduct.vendor_code.ilike(term),
                SupplierProduct.brand.ilike(term),
                SupplierProduct.category.ilike(term),
                SupplierProduct.wb_category_name.ilike(term),
            ))
        wb_category = _bounded_text(filters.get('wb_category'), 300)
        if wb_category == '__missing__':
            query = query.filter(db.or_(
                SupplierProduct.wb_category_name.is_(None),
                SupplierProduct.wb_category_name == '',
                SupplierProduct.wb_subject_id.is_(None),
            ))
        elif wb_category:
            query = query.filter(
                SupplierProduct.wb_category_name == wb_category,
            )
        source_category = _bounded_text(filters.get('source_category'), 300)
        if source_category:
            query = query.filter(
                SupplierProduct.category == source_category,
            )
        stock_status = filters.get('stock_status')
        if stock_status == 'in_stock':
            query = query.filter(SupplierProduct.supplier_quantity > 0)
        elif stock_status == 'out_of_stock':
            query = query.filter(db.or_(
                SupplierProduct.supplier_quantity.is_(None),
                SupplierProduct.supplier_quantity <= 0,
            ))
        mapping_state = filters.get('mapping_state')
        if mapping_state in ('invalid', 'valid'):
            marketplace = Marketplace.query.filter_by(code='wb').first()
            valid_ids = db.session.query(MarketplaceCategory.subject_id).filter(
                MarketplaceCategory.marketplace_id == (
                    marketplace.id if marketplace else -1
                ),
                MarketplaceCategory.is_leaf.is_(True),
                MarketplaceCategory.is_enabled.is_(True),
                MarketplaceCategory.is_available.is_(True),
            )
            if mapping_state == 'valid':
                query = query.filter(SupplierProduct.wb_subject_id.in_(valid_ids))
            else:
                query = query.filter(db.or_(
                    SupplierProduct.wb_subject_id.is_(None),
                    ~SupplierProduct.wb_subject_id.in_(valid_ids),
                ))
        return query

    @staticmethod
    def _validate_ids(
        supplier_id: int, product_ids: Sequence[int], mode: str,
    ) -> List[int]:
        if not isinstance(product_ids, (list, tuple)) or not product_ids:
            raise SupplierCatalogEnrichmentError(
                'empty_selection', 'Выберите хотя бы один товар.',
            )
        limit = (
            MAX_CHARACTERISTIC_SELECTION
            if mode in ('category_and_characteristics', MODE_INFERENCE)
            else MAX_SELECTION
        )
        if len(product_ids) > limit:
            raise SupplierCatalogEnrichmentError(
                'selection_too_large',
                f'За один запуск можно выбрать не более {limit} товаров.',
            )
        prepared = []
        seen = set()
        for raw in product_ids:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
                raise SupplierCatalogEnrichmentError(
                    'invalid_product_ids',
                    'ID товаров должны быть положительными целыми числами.',
                )
            if raw in seen:
                raise SupplierCatalogEnrichmentError(
                    'duplicate_product_ids', 'В выборе есть повторяющиеся товары.',
                )
            seen.add(raw)
            prepared.append(raw)
        owned = {
            row[0] for row in db.session.query(SupplierProduct.id).filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.id.in_(prepared),
            ).all()
        }
        if owned != set(prepared):
            raise SupplierCatalogEnrichmentError(
                'product_scope_mismatch',
                'Часть товаров не принадлежит выбранному поставщику.',
            )
        return prepared

    @classmethod
    def create_run(
        cls,
        *,
        supplier_id: int,
        admin_user_id: int,
        product_ids: Sequence[int],
        mode: str = 'category_only',
        selection: Optional[Dict[str, Any]] = None,
        model_override: Optional[str] = None,
    ) -> SupplierCatalogEnrichmentRun:
        if mode not in (
            'category_only', 'category_and_characteristics', MODE_INFERENCE,
        ):
            raise SupplierCatalogEnrichmentError(
                'invalid_mode', 'Неизвестный режим обогащения.',
            )
        supplier = db.session.get(Supplier, supplier_id)
        if not supplier:
            raise SupplierCatalogEnrichmentError(
                'supplier_not_found', 'Поставщик не найден.',
            )
        if not supplier.ai_enabled:
            raise SupplierCatalogEnrichmentError(
                'supplier_ai_disabled',
                'Сначала включите AI в настройках поставщика.',
            )
        active = SupplierCatalogEnrichmentRun.query.filter(
            SupplierCatalogEnrichmentRun.supplier_id == supplier_id,
            SupplierCatalogEnrichmentRun.status.in_(ACTIVE_RUN_STATUSES),
        ).order_by(SupplierCatalogEnrichmentRun.created_at.desc()).first()
        if active:
            raise SupplierCatalogEnrichmentError(
                'run_already_active',
                'Для этого поставщика уже выполняется массовое обогащение.',
            )
        reference = cls.require_reference()
        prepared_ids = cls._validate_ids(supplier_id, product_ids, mode)

        from services.supplier_service import SupplierService
        ai_service = SupplierService._get_ai_service(
            supplier, model_override=model_override,
        )
        if not ai_service:
            raise SupplierCatalogEnrichmentError(
                'ai_not_configured',
                'AI-профиль поставщика не настроен или не содержит API-ключ.',
            )
        model_used = ai_service.config.model
        ai_service.close()

        if mode == MODE_INFERENCE:
            estimated_calls = math.ceil(
                len(prepared_ids) / CHARACTERISTIC_BATCH_SIZE
            )
        else:
            estimated_calls = math.ceil(
                len(prepared_ids) / CATEGORY_BATCH_SIZE
            ) * 2
            if mode == 'category_and_characteristics':
                estimated_calls += math.ceil(
                    len(prepared_ids) / CHARACTERISTIC_BATCH_SIZE
                )
        call_limit = min(MAX_LLM_CALLS, max(20, estimated_calls + 10))
        run = SupplierCatalogEnrichmentRun(
            id=str(uuid.uuid4()),
            supplier_id=supplier_id,
            admin_user_id=admin_user_id,
            mode=mode,
            status='pending',
            selection_json=_json_dump(_safe_source_value(selection or {})),
            reference_snapshot_json=_json_dump(reference),
            model_used=model_used,
            total=len(prepared_ids),
            llm_call_limit=call_limit,
            heartbeat_at=datetime.utcnow(),
        )
        db.session.add(run)

        products = {
            product.id: product for product in SupplierProduct.query.filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.id.in_(prepared_ids),
            ).all()
        }
        db.session.add_all([
            SupplierCatalogEnrichmentItem(
                run_id=run.id,
                supplier_product_id=product_id,
                ordinal=ordinal,
                source_fingerprint=_source_fingerprint(products[product_id]),
                # Inference не имеет категорийной фазы: items сразу в
                # characteristics (CHECK phase не расширяется)
                phase=(
                    'characteristics' if mode == MODE_INFERENCE
                    else 'category'
                ),
            )
            for ordinal, product_id in enumerate(prepared_ids, start=1)
        ])
        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            concurrent = SupplierCatalogEnrichmentRun.query.filter(
                SupplierCatalogEnrichmentRun.supplier_id == supplier_id,
                SupplierCatalogEnrichmentRun.status.in_(ACTIVE_RUN_STATUSES),
            ).first()
            if concurrent:
                raise SupplierCatalogEnrichmentError(
                    'run_already_active',
                    'Для этого поставщика уже выполняется массовое обогащение.',
                ) from exc
            raise SupplierCatalogEnrichmentError(
                'run_create_conflict',
                'Не удалось безопасно создать запуск; повторите попытку.',
            ) from exc
        return run

    @staticmethod
    def serialize_run(run: SupplierCatalogEnrichmentRun) -> Dict[str, Any]:
        progress = round((run.processed or 0) * 100 / run.total) \
            if run.total else 0
        return {
            'id': run.id,
            'supplier_id': run.supplier_id,
            'mode': run.mode,
            'status': run.status,
            'total': run.total,
            'processed': run.processed,
            'applied': run.applied,
            'unchanged': run.unchanged,
            'needs_review': run.needs_review,
            'failed': run.failed,
            'cancelled': run.cancelled,
            'progress': progress,
            'llm_calls': run.llm_calls,
            'llm_call_limit': run.llm_call_limit,
            'model_used': run.model_used,
            'current_label': run.current_label,
            'error_code': run.error_code,
            'error_message': run.error_message,
            'heartbeat_at': run.heartbeat_at.isoformat()
            if run.heartbeat_at else None,
            'created_at': run.created_at.isoformat() if run.created_at else None,
            'completed_at': run.completed_at.isoformat()
            if run.completed_at else None,
        }

    @staticmethod
    def _load_leaf_categories(marketplace_id: int) -> List[Dict[str, Any]]:
        rows = MarketplaceCategory.query.filter(
            MarketplaceCategory.marketplace_id == marketplace_id,
            MarketplaceCategory.is_leaf.is_(True),
            MarketplaceCategory.is_enabled.is_(True),
            MarketplaceCategory.is_available.is_(True),
        ).order_by(MarketplaceCategory.subject_name.asc()).all()
        return [{
            'subject_id': int(row.subject_id),
            'subject_name': row.subject_name or '',
            'parent_name': row.parent_name or '',
            '_subject_norm': _normalized(row.subject_name),
            '_parent_norm': _normalized(row.parent_name),
            '_subject_tokens': _tokens(row.subject_name),
        } for row in rows]

    @staticmethod
    def _exact_source_category(
        source: Dict[str, Any], categories: Sequence[Dict[str, Any]],
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        labels = []
        supplier_category = source.get('supplier_category')
        if supplier_category:
            labels.extend(
                part.strip() for part in re.split(r'[>/|]', supplier_category)
                if part.strip()
            )
        all_categories = source.get('all_categories')
        if isinstance(all_categories, list):
            labels.extend(str(value).strip() for value in all_categories if value)
        by_name: Dict[str, List[Dict[str, Any]]] = {}
        for category in categories:
            by_name.setdefault(category['_subject_norm'], []).append(category)
        for label in reversed(labels):
            matches = by_name.get(_normalized(label), [])
            if len(matches) == 1:
                return matches[0], label
        return None

    @staticmethod
    def _category_source_match(
        category: Dict[str, Any], source: Dict[str, Any],
    ) -> float:
        title = _normalized(source.get('title'))
        supplier_category = _normalized(source.get('supplier_category'))
        all_categories = _normalized(source.get('all_categories'))
        source_text = ' '.join((title, supplier_category, all_categories))
        subject = category['_subject_norm']
        if subject and subject in source_text:
            return 1.0
        subject_tokens = category['_subject_tokens']
        source_tokens = _tokens(source_text)
        if not subject_tokens:
            return 0.0
        overlap = len(subject_tokens & source_tokens) / len(subject_tokens)
        if overlap:
            return min(0.9, 0.25 + overlap * 0.65)
        return 0.0

    @classmethod
    def _rank_candidates(
        cls,
        source: Dict[str, Any],
        categories: Sequence[Dict[str, Any]],
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        supplier_category = _normalized(source.get('supplier_category'))
        all_categories = _normalized(source.get('all_categories'))
        ranked = []
        for category in categories:
            source_match = cls._category_source_match(category, source)
            parent_match = bool(
                category['_parent_norm']
                and category['_parent_norm'] in (
                    supplier_category + ' ' + all_categories
                )
            )
            if source_match <= 0 and not parent_match:
                continue
            score = source_match + (0.06 if parent_match else 0)
            ranked.append((
                -score,
                len(category['subject_name']),
                category['subject_name'],
                category,
                source_match,
            ))
        ranked.sort(key=lambda item: item[:3])
        return [{
            'subject_id': item[3]['subject_id'],
            'subject_name': item[3]['subject_name'],
            'parent_name': item[3]['parent_name'],
            'source_match': round(item[4], 3),
        } for item in ranked[:limit]]

    @staticmethod
    def _search_categories(
        query: str,
        categories: Sequence[Dict[str, Any]],
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        query = _bounded_text(query, 120)
        normalized = _normalized(query)
        terms = {_stem(word) for word in normalized.split() if len(word) >= 2}
        if not normalized or not terms:
            return []
        ranked = []
        for category in categories:
            combined = category['_subject_norm'] + ' ' + category['_parent_norm']
            combined_stems = {_stem(word) for word in combined.split()}
            if not terms <= combined_stems and not all(
                term in combined for term in terms
            ):
                continue
            if normalized == category['_subject_norm']:
                rank = 0
            elif normalized in category['_subject_norm']:
                rank = 1
            elif normalized in category['_parent_norm']:
                rank = 2
            else:
                rank = 3
            ranked.append((rank, len(category['subject_name']), category))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]['subject_name']))
        return [{
            'subject_id': item[2]['subject_id'],
            'subject_name': item[2]['subject_name'],
            'parent_name': item[2]['parent_name'],
            'source_match': 0.0,
        } for item in ranked[:limit]]

    @classmethod
    def search_reference_categories(
        cls, query: str, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Admin review lookup with the same fresh leaf-only boundary."""
        reference = cls.require_reference()
        categories = cls._load_leaf_categories(reference['marketplace_id'])
        results = cls._search_categories(
            query, categories, limit=max(1, min(int(limit), 50)),
        )
        return [{
            key: item[key]
            for key in ('subject_id', 'subject_name', 'parent_name')
        } for item in results]

    @staticmethod
    def _category_prompt(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
        system = (
            'Ты классифицируешь товары по конечным предметам Wildberries. '
            'CANDIDATES — результаты типизированного read-only поиска по свежему '
            'справочнику WB; использовать можно ТОЛЬКО subject_id из CANDIDATES. '
            'parent_name — раздел, его нельзя выбирать или записывать. Если ни '
            'один вариант не подходит, верни subject_id=null и короткий '
            'lookup_query (1–4 слова) для ещё одного вызова справочника. Не '
            'угадывай. evidence — точная короткая цитата из SOURCE, которая '
            'подтверждает тип товара. Верни ровно один result на каждый '
            'product_id, без дублей и пропусков, только JSON.'
        )
        payload = [{
            'product_id': entry['product_id'],
            'source': entry['source'],
            'candidates': entry['candidates'],
        } for entry in entries]
        user = (
            'Верни {"results":[{"product_id":1,"subject_id":123|null,'
            '"confidence":0.0,"reasoning":"кратко","evidence":"точная '
            'цитата","lookup_query":"..."|null}]} для данных:\n'
            + _json_dump(payload)
        )
        return [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ]

    @staticmethod
    def _validate_category_response(
        response: str,
        entries: Sequence[Dict[str, Any]],
    ) -> Dict[int, Dict[str, Any]]:
        data = _extract_json_object(response)
        if set(data) != {'results'} or not isinstance(data['results'], list):
            raise ValueError('Category response must contain only results array')
        expected = {entry['product_id'] for entry in entries}
        candidates = {
            entry['product_id']: {
                item['subject_id'] for item in entry['candidates']
            } for entry in entries
        }
        result = {}
        allowed_keys = {
            'product_id', 'subject_id', 'confidence', 'reasoning', 'evidence',
            'lookup_query',
        }
        for item in data['results']:
            if not isinstance(item, dict) or set(item) - allowed_keys:
                raise ValueError('Invalid category result shape')
            product_id = item.get('product_id')
            if (
                isinstance(product_id, bool)
                or not isinstance(product_id, int)
                or product_id not in expected
                or product_id in result
            ):
                raise ValueError('Foreign, invalid or duplicate product_id')
            subject_id = item.get('subject_id')
            if subject_id is not None and (
                isinstance(subject_id, bool)
                or not isinstance(subject_id, int)
                or subject_id not in candidates[product_id]
            ):
                raise ValueError('subject_id is outside typed candidates')
            raw_confidence = item.get('confidence', 0)
            if isinstance(raw_confidence, bool) or not isinstance(
                raw_confidence, (int, float)
            ):
                raise ValueError('confidence must be numeric')
            confidence = float(raw_confidence)
            if not 0 <= confidence <= 1:
                raise ValueError('confidence must be from 0 to 1')
            for key in ('reasoning', 'evidence', 'lookup_query'):
                if item.get(key) is not None and not isinstance(item[key], str):
                    raise ValueError(f'{key} must be a string or null')
            result[product_id] = {
                **item,
                'confidence': confidence,
                'reasoning': _bounded_text(item.get('reasoning'), 1000),
                'evidence': _bounded_text(item.get('evidence'), 500),
                'lookup_query': _bounded_text(item.get('lookup_query'), 120),
            }
        if set(result) != expected:
            raise ValueError('Category response is not an exact product set')
        return result

    @staticmethod
    def _same_reference(
        original: Dict[str, Any], current: Dict[str, Any],
    ) -> bool:
        if not current.get('usable'):
            return False
        if original.get('version') != current.get('version'):
            return False
        original_hash = original.get('snapshot_hash')
        current_hash = current.get('snapshot_hash')
        return not (original_hash or current_hash) or original_hash == current_hash

    @staticmethod
    @contextmanager
    def _supplier_lock(supplier_id: int):
        try:
            import fcntl
        except ImportError:
            yield True
            return
        lock_dir = os.environ.get('SUPPLIER_ENRICHMENT_LOCK_DIR', '/tmp')
        path = os.path.join(
            lock_dir, f'supplier-catalog-enrichment-{int(supplier_id)}.lock',
        )
        handle = open(path, 'a+', encoding='utf-8')
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

    @classmethod
    def _finish_unfinished_items(
        cls,
        run_id: str,
        *,
        terminal_status: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Close pending work without hiding changes committed in an earlier phase."""
        if terminal_status not in ('failed', 'cancelled'):
            raise ValueError('Unsupported terminal status')
        items = SupplierCatalogEnrichmentItem.query.filter(
            SupplierCatalogEnrichmentItem.run_id == run_id,
            SupplierCatalogEnrichmentItem.status.in_(('pending', 'running')),
        ).all()
        now = datetime.utcnow()
        for item in items:
            product = item.supplier_product
            already_changed = bool(
                product
                and (item.category_changed or item.characteristics_changed)
            )
            if already_changed:
                item.status = 'applied'
                if not item.after_json:
                    item.after_json = _json_dump(_enrichment_state(product))
            else:
                item.status = terminal_status
            item.phase = 'done'
            if error_code is not None:
                item.error_code = error_code
            if error_message is not None:
                item.error_message = _bounded_text(error_message, 1200)
            item.completed_at = now
        db.session.commit()

    @classmethod
    def _reserve_llm_call(cls, run_id: str) -> bool:
        run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
        if not run or run.status not in ('pending', 'running'):
            return False
        if run.llm_calls >= run.llm_call_limit:
            run.status = 'failed'
            run.error_code = 'llm_budget_exhausted'
            run.error_message = (
                'Лимит model-вызовов исчерпан; необработанные товары не изменены.'
            )
            run.completed_at = datetime.utcnow()
            cls._finish_unfinished_items(
                run_id,
                terminal_status='failed',
                error_code='llm_budget_exhausted',
                error_message=run.error_message,
            )
            cls._refresh_counters(run_id)
            return False
        run.llm_calls += 1
        run.heartbeat_at = datetime.utcnow()
        db.session.commit()
        return True

    @classmethod
    def _mark_batch_error(
        cls,
        run_id: str,
        item_ids: Sequence[int],
        code: str,
        message: str,
    ) -> None:
        items = SupplierCatalogEnrichmentItem.query.filter(
            SupplierCatalogEnrichmentItem.run_id == run_id,
            SupplierCatalogEnrichmentItem.id.in_(item_ids),
        ).all()
        now = datetime.utcnow()
        for item in items:
            item.error_code = code
            item.error_message = _bounded_text(message, 1200)
            if item.attempt_count >= MAX_ITEM_ATTEMPTS:
                product = item.supplier_product
                if product and (
                    item.category_changed or item.characteristics_changed
                ):
                    item.status = 'applied'
                    if not item.after_json:
                        item.after_json = _json_dump(_enrichment_state(product))
                else:
                    item.status = 'failed'
                item.phase = 'done'
                item.completed_at = now
            else:
                item.status = 'pending'
                item.completed_at = None
        db.session.commit()

    @classmethod
    def _apply_category_results(
        cls,
        *,
        run_id: str,
        result_by_id: Dict[int, Dict[str, Any]],
        source_by_id: Dict[int, Dict[str, Any]],
        target_fingerprints: Dict[int, str],
        candidates_by_id: Dict[int, Dict[int, Dict[str, Any]]],
        deterministic_ids: Optional[set[int]] = None,
    ) -> None:
        deterministic_ids = deterministic_ids or set()
        run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
        reference = cls.require_reference()
        original_reference = _json_load(run.reference_snapshot_json, {})
        if not cls._same_reference(original_reference, reference):
            raise SupplierCatalogEnrichmentError(
                'wb_reference_changed',
                'Снимок категорий WB изменился во время запуска.',
            )
        marketplace_id = reference['marketplace_id']
        items = SupplierCatalogEnrichmentItem.query.filter(
            SupplierCatalogEnrichmentItem.run_id == run_id,
            SupplierCatalogEnrichmentItem.supplier_product_id.in_(
                list(result_by_id)
            ),
        ).all()
        item_by_product = {item.supplier_product_id: item for item in items}
        products = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == run.supplier_id,
            SupplierProduct.id.in_(list(result_by_id)),
        ).all()
        now = datetime.utcnow()

        for product in products:
            item = item_by_product[product.id]
            result = result_by_id[product.id]
            source = source_by_id[product.id]
            item.proposed_subject_id = result.get('subject_id')
            item.confidence = result.get('confidence')
            item.reasoning = result.get('reasoning')
            item.evidence = result.get('evidence')

            if _source_fingerprint(product) != item.source_fingerprint:
                item.status = 'needs_review'
                item.phase = 'done'
                item.error_code = 'source_changed'
                item.error_message = (
                    'Исходные данные товара изменились после создания запуска.'
                )
                item.completed_at = now
                continue
            if _target_fingerprint(product) != target_fingerprints[product.id]:
                item.status = 'needs_review'
                item.phase = 'done'
                item.error_code = 'card_changed'
                item.error_message = (
                    'Карточку изменили параллельно; автоматическая запись отменена.'
                )
                item.completed_at = now
                continue

            subject_id = result.get('subject_id')
            candidate = candidates_by_id.get(product.id, {}).get(subject_id)
            category = None
            if subject_id:
                category = MarketplaceCategory.query.filter_by(
                    marketplace_id=marketplace_id,
                    subject_id=subject_id,
                    is_leaf=True,
                    is_enabled=True,
                    is_available=True,
                ).first()
            if not subject_id or not candidate or not category:
                item.status = 'needs_review'
                item.phase = 'done'
                item.error_code = 'category_not_resolved'
                item.error_message = (
                    'Надёжная конечная категория не определена; карточка не изменена.'
                )
                item.completed_at = now
                continue

            item.proposed_subject_name = category.subject_name
            grounded = _evidence_is_grounded(result.get('evidence'), source)
            deterministic = product.id in deterministic_ids
            candidate_score = float(candidate.get('source_match') or 0)
            competing_scores = [
                float(other.get('source_match') or 0)
                for other_subject_id, other in candidates_by_id.get(
                    product.id, {}
                ).items()
                if other_subject_id != subject_id
            ]
            lexical_unambiguous = not any(
                score >= candidate_score for score in competing_scores
            )
            safe_to_apply = deterministic or (
                result['confidence'] >= AUTO_APPLY_CONFIDENCE
                and grounded
                and candidate_score >= AUTO_APPLY_SOURCE_MATCH
                and lexical_unambiguous
            )
            item.reference_json = _json_dump({
                'subject_id': category.subject_id,
                'subject_name': category.subject_name,
                'parent_name': category.parent_name,
                'categories_version': reference['version'],
                'categories_snapshot_hash': reference['snapshot_hash'],
                'deterministic': deterministic,
                'evidence_grounded': grounded,
                'source_match': candidate_score,
                'lexical_unambiguous': lexical_unambiguous,
            })
            if not safe_to_apply:
                item.status = 'needs_review'
                item.phase = 'done'
                item.error_code = 'low_confidence'
                item.error_message = (
                    'Предложение сохранено для проверки, но не применено: '
                    'недостаточно подтверждения в исходных данных.'
                )
                item.completed_at = now
                continue

            old_subject_id = product.wb_subject_id
            before = _enrichment_state(product)
            if not item.before_json:
                item.before_json = _json_dump(before)
            product.wb_subject_id = int(category.subject_id)
            product.wb_subject_name = category.subject_name
            product.wb_category_name = category.subject_name
            product.category_confidence = float(result['confidence'])
            if old_subject_id != product.wb_subject_id:
                # Values validated for another category must never cross the
                # category boundary automatically.
                product.ai_marketplace_json = None
                product.marketplace_fields_json = None
                product.marketplace_validation_status = None
                product.marketplace_fill_pct = None
            changed = before != _enrichment_state(product)
            if changed:
                product.content_revision = int(product.content_revision or 1) + 1
                item.category_changed = True
                item.applied_revision = product.content_revision

            item.error_code = None
            item.error_message = None
            if run.mode == 'category_and_characteristics':
                item.phase = 'characteristics'
                item.status = 'pending'
                # Phase checkpoint: later cancellation/failure must preserve
                # this exact applied state, and concurrent edits must conflict
                # instead of becoming part of the run's rollback snapshot.
                item.after_json = _json_dump(_enrichment_state(product))
            else:
                item.phase = 'done'
                item.status = 'applied' if changed else 'unchanged'
                item.after_json = _json_dump(_enrichment_state(product))
                item.completed_at = now
        db.session.commit()

    @classmethod
    def _process_category_batch(cls, run_id: str, ai_service) -> bool:
        run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
        items = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run_id, status='pending', phase='category',
        ).order_by(SupplierCatalogEnrichmentItem.ordinal).limit(
            CATEGORY_BATCH_SIZE
        ).all()
        if not items:
            return False
        reference = cls.require_reference()
        original_reference = _json_load(run.reference_snapshot_json, {})
        if not cls._same_reference(original_reference, reference):
            raise SupplierCatalogEnrichmentError(
                'wb_reference_changed',
                'Снимок категорий WB изменился; запустите обработку заново.',
            )
        categories = cls._load_leaf_categories(reference['marketplace_id'])
        by_subject = {category['subject_id']: category for category in categories}
        item_id_by_product = {
            item.supplier_product_id: item.id for item in items
        }
        product_ids = [item.supplier_product_id for item in items]
        products = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == run.supplier_id,
            SupplierProduct.id.in_(product_ids),
        ).all()
        product_by_id = {product.id: product for product in products}
        now = datetime.utcnow()
        source_by_id = {}
        target_fingerprints = {}
        candidates_by_id: Dict[int, Dict[int, Dict[str, Any]]] = {}
        results: Dict[int, Dict[str, Any]] = {}
        deterministic_ids = set()
        model_entries = []

        for item in items:
            product = product_by_id.get(item.supplier_product_id)
            if not product:
                item.status = 'failed'
                item.phase = 'done'
                item.error_code = 'product_not_found'
                item.error_message = 'Товар больше не существует.'
                item.completed_at = now
                continue
            item.status = 'running'
            item.attempt_count += 1
            item.started_at = item.started_at or now
            source = _product_source(product)
            source_by_id[product.id] = source
            target_fingerprints[product.id] = _target_fingerprint(product)
            if not item.before_json:
                item.before_json = _json_dump(_enrichment_state(product))

            current = by_subject.get(product.wb_subject_id)
            if current:
                candidate = {
                    'subject_id': current['subject_id'],
                    'subject_name': current['subject_name'],
                    'parent_name': current['parent_name'],
                    'source_match': 1.0,
                }
                candidates_by_id[product.id] = {current['subject_id']: candidate}
                results[product.id] = {
                    'product_id': product.id,
                    'subject_id': current['subject_id'],
                    'confidence': 1.0,
                    'reasoning': 'Существующий subject_id подтверждён свежим leaf-справочником.',
                    'evidence': source.get('title') or source.get('supplier_category') or 'ID',
                }
                deterministic_ids.add(product.id)
                continue

            exact = cls._exact_source_category(source, categories)
            if exact:
                category, evidence = exact
                candidate = {
                    'subject_id': category['subject_id'],
                    'subject_name': category['subject_name'],
                    'parent_name': category['parent_name'],
                    'source_match': 1.0,
                }
                candidates_by_id[product.id] = {category['subject_id']: candidate}
                results[product.id] = {
                    'product_id': product.id,
                    'subject_id': category['subject_id'],
                    'confidence': 1.0,
                    'reasoning': 'Точное совпадение категории источника с предметом WB.',
                    'evidence': evidence,
                }
                deterministic_ids.add(product.id)
                continue

            candidates = cls._rank_candidates(source, categories)
            candidates_by_id[product.id] = {
                candidate['subject_id']: candidate for candidate in candidates
            }
            model_entries.append({
                'product_id': product.id,
                'source': source,
                'candidates': candidates,
            })

        run.current_label = (
            f'Категории: товары {items[0].ordinal}–{items[-1].ordinal}'
        )
        run.heartbeat_at = now
        db.session.commit()

        def apply_ready(
            ready_results: Dict[int, Dict[str, Any]],
            ready_deterministic_ids: Optional[set[int]] = None,
        ) -> bool:
            if not ready_results:
                return True
            try:
                cls._apply_category_results(
                    run_id=run_id,
                    result_by_id=ready_results,
                    source_by_id=source_by_id,
                    target_fingerprints=target_fingerprints,
                    candidates_by_id=candidates_by_id,
                    deterministic_ids=ready_deterministic_ids,
                )
            except SupplierCatalogEnrichmentError:
                raise
            except Exception as exc:
                db.session.rollback()
                cls._mark_batch_error(
                    run_id,
                    [item_id_by_product[product_id]
                     for product_id in ready_results],
                    'category_apply_failed',
                    _public_failure('Категория не сохранена', exc),
                )
                return False
            return True

        # Rows already grounded by an exact leaf ID or exact source label do
        # not depend on the model. Commit them before any fallible provider
        # call so an unrelated unresolved row cannot make them retry or fail.
        if results:
            if not apply_ready(results, deterministic_ids):
                return True
            results = {}

        if model_entries:
            model_item_ids = [
                item_id_by_product[entry['product_id']]
                for entry in model_entries
            ]
            if not cls._reserve_llm_call(run_id):
                return True
            try:
                response = ai_service.client.chat_completion(
                    cls._category_prompt(model_entries),
                    temperature=0.0,
                    max_tokens=7000,
                    response_format={'type': 'json_object'},
                )
            except Exception as exc:
                cls._mark_batch_error(
                    run_id, model_item_ids, 'model_call_failed',
                    _public_failure('Вызов модели не выполнен', exc),
                )
                return True
            try:
                model_results = cls._validate_category_response(
                    response, model_entries,
                )
            except Exception as exc:
                cls._mark_batch_error(
                    run_id, model_item_ids, 'invalid_model_response', str(exc),
                )
                return True

            lookup_entries = []
            for entry in model_entries:
                result = model_results[entry['product_id']]
                if result.get('subject_id') is None and result.get('lookup_query'):
                    looked_up = cls._search_categories(
                        result['lookup_query'], categories,
                    )
                    # source_match is always recomputed from actual source;
                    # the lookup query itself cannot authorize auto-apply.
                    for candidate in looked_up:
                        raw_category = by_subject[candidate['subject_id']]
                        candidate['source_match'] = cls._category_source_match(
                            raw_category, entry['source'],
                        )
                    entry = {**entry, 'candidates': looked_up}
                    candidates_by_id[entry['product_id']].update({
                        candidate['subject_id']: candidate
                        for candidate in looked_up
                    })
                    lookup_entries.append(entry)
                else:
                    results[entry['product_id']] = result

            # First-pass decisions are independent from rows that requested a
            # second reference lookup, so journal them before that call.
            if results:
                if not apply_ready(results):
                    return True
                results = {}

            if lookup_entries:
                lookup_item_ids = [
                    item_id_by_product[entry['product_id']]
                    for entry in lookup_entries
                ]
                if not cls._reserve_llm_call(run_id):
                    return True
                try:
                    second_response = ai_service.client.chat_completion(
                        cls._category_prompt(lookup_entries),
                        temperature=0.0,
                        max_tokens=5000,
                        response_format={'type': 'json_object'},
                    )
                except Exception as exc:
                    cls._mark_batch_error(
                        run_id, lookup_item_ids, 'model_call_failed',
                        _public_failure('Вызов модели не выполнен', exc),
                    )
                    return True
                try:
                    results = cls._validate_category_response(
                        second_response, lookup_entries,
                    )
                except Exception as exc:
                    cls._mark_batch_error(
                        run_id, lookup_item_ids, 'invalid_model_response',
                        str(exc),
                    )
                    return True
                if not apply_ready(results):
                    return True
        return True

    @staticmethod
    def _characteristic_schema(
        marketplace: Marketplace,
        category: MarketplaceCategory,
    ) -> Tuple[List[MarketplaceCategoryCharacteristic], List[Dict[str, Any]]]:
        from services.marketplace_validator import (
            get_wb_characteristic_constraint,
        )
        if (
            not category.is_leaf or not category.is_enabled
            or not category.is_available
            or category.characteristics_sync_status != 'success'
            or not category.characteristics_synced_at
            or category.characteristics_synced_at
            < datetime.utcnow() - timedelta(hours=REFERENCE_MAX_AGE_HOURS)
        ):
            raise SupplierCatalogEnrichmentError(
                'wb_schema_unavailable',
                f'Схема «{category.subject_name}» устарела или недоступна.',
            )
        characteristics = MarketplaceCategoryCharacteristic.query.filter(
            MarketplaceCategoryCharacteristic.category_id == category.id,
            MarketplaceCategoryCharacteristic.marketplace_id == marketplace.id,
            MarketplaceCategoryCharacteristic.is_available.is_(True),
            db.or_(
                MarketplaceCategoryCharacteristic.is_enabled.is_(True),
                MarketplaceCategoryCharacteristic.required.is_(True),
            ),
        ).order_by(
            MarketplaceCategoryCharacteristic.display_order,
            MarketplaceCategoryCharacteristic.charc_id,
        ).all()
        if not characteristics:
            raise SupplierCatalogEnrichmentError(
                'wb_schema_unavailable',
                f'Для «{category.subject_name}» нет доступной схемы характеристик.',
            )
        cache: Dict[str, Any] = {}
        schema = []
        for charc in characteristics:
            if charc.charc_type == 0:
                continue
            constraint = get_wb_characteristic_constraint(
                marketplace, charc, cache, values_limit=40,
            )
            if not constraint.get('usable'):
                issue = constraint.get('issue') or {}
                raise SupplierCatalogEnrichmentError(
                    'wb_dictionary_unavailable',
                    issue.get('message')
                    or f'Справочник «{charc.name}» недоступен.',
                )
            schema.append({
                'name': charc.name,
                'type': 'number' if charc.charc_type == 4 else 'string_array',
                'unit': charc.unit_name,
                'required': bool(charc.required),
                'max_count': int(charc.max_count or 0),
                'allowed_values': constraint.get('values') or [],
                'allowed_values_truncated': bool(constraint.get('truncated')),
                'instruction': _bounded_text(charc.ai_instruction, 300),
            })
        return characteristics, schema

    @staticmethod
    def _characteristics_prompt(
        category: MarketplaceCategory,
        schema: Sequence[Dict[str, Any]],
        entries: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        system = (
            'Ты извлекаешь характеристики товара для одной категории WB. '
            'Заполняй ТОЛЬКО явно указанные факты из SOURCE: не делай выводы '
            'по типу товара и не оценивай размеры, материал, страну, '
            'комплектацию или количество. Ключ поля должен точно присутствовать '
            'в SCHEMA. Для allowed_values используй только точное значение из '
            'списка; если список усечён, можно вернуть точную цитату источника — '
            'Python всё равно сверит её с полным справочником. Для каждого '
            'значения обязательно верни evidence — точную цитату из SOURCE. '
            'Неизвестные поля пропусти. Верни ровно один result на каждый '
            'product_id, только JSON.'
        )
        payload = {
            'category': {
                'subject_id': category.subject_id,
                'subject_name': category.subject_name,
            },
            'schema': schema,
            'products': [{
                'product_id': entry['product_id'],
                'source': entry['source'],
            } for entry in entries],
        }
        user = (
            'Формат: {"results":[{"product_id":1,"fields":'
            '{"Точное имя":{"value":["значение"],"evidence":"цитата"}}}]}.'
            '\nДанные:\n' + _json_dump(payload)
        )
        return [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ]

    @staticmethod
    def _validate_characteristics_response(
        response: str,
        entries: Sequence[Dict[str, Any]],
        schema_names: set[str],
    ) -> Dict[int, Dict[str, Dict[str, Any]]]:
        data = _extract_json_object(response)
        if set(data) != {'results'} or not isinstance(data['results'], list):
            raise ValueError('Characteristic response must contain results only')
        expected = {entry['product_id'] for entry in entries}
        result = {}
        for item in data['results']:
            if not isinstance(item, dict) or set(item) != {'product_id', 'fields'}:
                raise ValueError('Invalid characteristic result shape')
            product_id = item.get('product_id')
            if (
                isinstance(product_id, bool)
                or not isinstance(product_id, int)
                or product_id not in expected
                or product_id in result
            ):
                raise ValueError('Foreign, invalid or duplicate product_id')
            fields = item.get('fields')
            if not isinstance(fields, dict) or set(fields) - schema_names:
                raise ValueError('Characteristic key is outside current schema')
            prepared = {}
            for name, field in fields.items():
                if not isinstance(field, dict) or set(field) != {'value', 'evidence'}:
                    raise ValueError('Each field requires value and evidence')
                if not isinstance(field.get('evidence'), str):
                    raise ValueError('Characteristic evidence must be a string')
                prepared[name] = {
                    'value': field.get('value'),
                    'evidence': _bounded_text(field['evidence'], 500),
                }
            result[product_id] = prepared
        if set(result) != expected:
            raise ValueError('Characteristic response is not an exact product set')
        return result

    @staticmethod
    def _mark_item_drift(item, code: str, message: str) -> None:
        if item.category_changed or item.characteristics_changed:
            item.status = 'applied'
        else:
            item.status = 'needs_review'
        item.phase = 'done'
        item.error_code = code
        item.error_message = message
        item.completed_at = datetime.utcnow()

    @classmethod
    def _collect_characteristic_chunk(
        cls, run_id: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Подготовить один чанк характеристик до LLM-вызова.

        Содержимое чанка идентично последовательному пути: до 6 товаров
        одного subject, полная schema и bounded source. Возвращает
        (did_work, ctx); ctx не None только когда items помечены running и
        prompt готов. LLM-бюджет здесь ещё не резервируется.
        """
        first = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run_id, status='pending', phase='characteristics',
        ).order_by(SupplierCatalogEnrichmentItem.ordinal).first()
        if not first:
            return False, None
        run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
        reference = cls.require_reference()
        if not cls._same_reference(
            _json_load(run.reference_snapshot_json, {}), reference,
        ):
            raise SupplierCatalogEnrichmentError(
                'wb_reference_changed',
                'Снимок категорий WB изменился; запустите обработку заново.',
            )
        first_product = db.session.get(SupplierProduct, first.supplier_product_id)
        if not first_product or not first_product.wb_subject_id:
            first.status = 'failed'
            first.phase = 'done'
            first.error_code = 'category_missing'
            first.error_message = 'Нет подтверждённой конечной категории WB.'
            first.completed_at = datetime.utcnow()
            db.session.commit()
            return True, None
        subject_id = int(first_product.wb_subject_id)
        candidate_items = SupplierCatalogEnrichmentItem.query.join(
            SupplierProduct,
            SupplierProduct.id
            == SupplierCatalogEnrichmentItem.supplier_product_id,
        ).filter(
            SupplierCatalogEnrichmentItem.run_id == run_id,
            SupplierCatalogEnrichmentItem.status == 'pending',
            SupplierCatalogEnrichmentItem.phase == 'characteristics',
            SupplierProduct.wb_subject_id == subject_id,
        ).order_by(SupplierCatalogEnrichmentItem.ordinal).limit(
            CHARACTERISTIC_BATCH_SIZE
        ).all()
        item_ids = [item.id for item in candidate_items]
        product_ids = [item.supplier_product_id for item in candidate_items]
        products = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == run.supplier_id,
            SupplierProduct.id.in_(product_ids),
        ).all()
        product_by_id = {product.id: product for product in products}
        marketplace = Marketplace.query.filter_by(code='wb').first()
        category = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace.id if marketplace else -1,
            subject_id=subject_id,
        ).first()
        if not marketplace or not category:
            cls._mark_batch_error(
                run_id, item_ids, 'wb_schema_unavailable',
                'Категория WB или её схема не найдена.',
            )
            return True, None
        try:
            characteristics, schema = cls._characteristic_schema(
                marketplace, category,
            )
        except SupplierCatalogEnrichmentError as exc:
            for item in candidate_items:
                item.attempt_count += 1
            db.session.commit()
            cls._mark_batch_error(run_id, item_ids, exc.code, str(exc))
            return True, None

        now = datetime.utcnow()
        entries = []
        target_fingerprints = {}
        active_item_ids = []

        for item in candidate_items:
            product = product_by_id.get(item.supplier_product_id)
            if not product:
                item.status = 'failed'
                item.phase = 'done'
                item.error_code = 'product_not_found'
                item.error_message = 'Товар больше не существует.'
                item.completed_at = now
                continue
            if _source_fingerprint(product) != item.source_fingerprint:
                cls._mark_item_drift(
                    item,
                    'source_changed',
                    'Исходные данные изменились после категорийного этапа.',
                )
                continue
            category_checkpoint = _json_load(item.after_json, None)
            if (
                category_checkpoint
                and _enrichment_state(product) != category_checkpoint
            ):
                cls._mark_item_drift(
                    item,
                    'card_changed',
                    'Карточку изменили после категорийного этапа.',
                )
                continue
            item.status = 'running'
            item.attempt_count += 1
            item.started_at = item.started_at or now
            entries.append({
                'product_id': product.id,
                'source': _product_source(product),
            })
            active_item_ids.append(item.id)
            target_fingerprints[product.id] = _target_fingerprint(product)
        run.current_label = f'Характеристики: {category.subject_name}'
        run.heartbeat_at = now
        db.session.commit()

        if not entries:
            return True, None
        return True, {
            'run_id': run_id,
            'subject_id': subject_id,
            'marketplace': marketplace,
            'category': category,
            'characteristics': characteristics,
            'schema': schema,
            'entries': entries,
            'active_item_ids': active_item_ids,
            'candidate_items': candidate_items,
            'target_fingerprints': target_fingerprints,
            'prompt': cls._characteristics_prompt(category, schema, entries),
        }

    @classmethod
    def _apply_characteristic_response(
        cls, ctx: Dict[str, Any], validation_client, response: Any,
    ) -> None:
        """Применить ответ модели к чанку: валидация, grounding и commit.

        Выполняется только в главном потоке (ORM). `response` может быть
        Exception — тогда чанк fail-closed помечается model_call_failed.
        """
        run_id = ctx['run_id']
        subject_id = ctx['subject_id']
        marketplace = ctx['marketplace']
        category = ctx['category']
        characteristics = ctx['characteristics']
        schema = ctx['schema']
        entries = ctx['entries']
        active_item_ids = ctx['active_item_ids']
        candidate_items = ctx['candidate_items']
        target_fingerprints = ctx['target_fingerprints']
        run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
        if isinstance(response, Exception):
            cls._mark_batch_error(
                run_id, active_item_ids, 'model_call_failed',
                _public_failure('Вызов модели не выполнен', response),
            )
            return
        try:
            raw_results = cls._validate_characteristics_response(
                response, entries, {item['name'] for item in schema},
            )
        except Exception as exc:
            cls._mark_batch_error(
                run_id, active_item_ids, 'invalid_model_response', str(exc),
            )
            return

        from services.marketplace_ai_parser import MarketplaceAwareParsingTask
        from services.marketplace_validator import MarketplaceValidator
        validator = MarketplaceAwareParsingTask(
            validation_client, characteristics, category_id=subject_id,
        )
        item_by_product = {
            item.supplier_product_id: item for item in candidate_items
        }
        try:
            for entry in entries:
                product_id = entry['product_id']
                product = db.session.get(SupplierProduct, product_id)
                item = item_by_product[product_id]
                if _source_fingerprint(product) != item.source_fingerprint:
                    cls._mark_item_drift(
                        item,
                        'source_changed',
                        'Исходные данные изменились во время model-вызова.',
                    )
                    continue
                category_checkpoint = _json_load(item.after_json, None)
                if (
                    category_checkpoint
                    and _enrichment_state(product) != category_checkpoint
                ):
                    cls._mark_item_drift(
                        item,
                        'card_changed',
                        'Карточку изменили во время model-вызова.',
                    )
                    continue
                if _target_fingerprint(product) != target_fingerprints[product_id]:
                    cls._mark_item_drift(
                        item, 'card_changed', 'Карточку изменили параллельно.',
                    )
                    continue

                existing = _json_load(product.ai_marketplace_json, {})
                existing.pop('_meta', None)
                existing_validated = validator.parse_response(_json_dump(existing)) or {}
                existing_validated.pop('_meta', None)
                grounded_values = {}
                evidence_map = {}
                for name, field in raw_results[product_id].items():
                    if _characteristic_value_is_grounded(
                        field['value'], field['evidence'], entry['source'],
                    ):
                        grounded_values[name] = field['value']
                        evidence_map[name] = field['evidence']
                new_validated = validator.parse_response(
                    _json_dump(grounded_values)
                ) or {}
                meta = new_validated.pop('_meta', {})
                merged = {**existing_validated, **new_validated}
                content_changed = merged != existing_validated
                before = _enrichment_state(product)
                if not item.before_json:
                    item.before_json = _json_dump(before)
                # Metadata from another run must not create a fake content
                # change when canonical field values stayed identical.
                if content_changed:
                    product.ai_marketplace_json = _json_dump({
                        **merged,
                        '_meta': {
                            'source': 'supplier_catalog_enrichment',
                            'model': run.model_used,
                            'evidence': evidence_map,
                            'validation_issues': meta.get(
                                'validation_issues', []
                            ),
                        },
                    })
                product.ai_model_used = run.model_used
                validation = MarketplaceValidator.validate_product_for_marketplace(
                    product, marketplace.id,
                )
                product.ai_fill_pct = validation.get('fill_percentage', 0)
                if content_changed:
                    product.content_revision = int(product.content_revision or 1) + 1
                    item.characteristics_changed = True
                    item.applied_revision = product.content_revision
                item.reference_json = _json_dump({
                    **_json_load(item.reference_json, {}),
                    'characteristics_schema_hash': category.characteristics_schema_hash,
                    'characteristics_version': category.characteristics_version,
                    'characteristics_synced_at': (
                        category.characteristics_synced_at.isoformat()
                        if category.characteristics_synced_at else None
                    ),
                })
                item.phase = 'done'
                item.status = (
                    'applied'
                    if item.category_changed or item.characteristics_changed
                    else 'unchanged'
                )
                item.error_code = None
                item.error_message = None
                item.after_json = _json_dump(_enrichment_state(product))
                item.completed_at = datetime.utcnow()
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            cls._mark_batch_error(
                run_id, active_item_ids, 'characteristics_apply_failed',
                _public_failure('Характеристики не сохранены', exc),
            )

    @classmethod
    def _process_characteristic_batch(cls, run_id: str, ai_service) -> bool:
        did_work, ctx = cls._collect_characteristic_chunk(run_id)
        if not ctx:
            return did_work
        if not cls._reserve_llm_call(run_id):
            return True
        try:
            response = ai_service.client.chat_completion(
                ctx['prompt'],
                temperature=0.0,
                max_tokens=8000,
                response_format={'type': 'json_object'},
            )
        except Exception as exc:
            response = exc
        cls._apply_characteristic_response(ctx, ai_service.client, response)
        return True

    @classmethod
    def _process_characteristic_batches_parallel(
        cls, run_id: str, supplier, ai_service, concurrency: int,
        collect_fn=None, apply_fn=None,
    ) -> bool:
        """До `concurrency` чанков за один batch (характеристики/inference).

        Содержимое каждого чанка идентично последовательному пути; растёт
        только число одновременных HTTP-вызовов. ORM живёт в главном потоке:
        worker-поток выполняет ровно один chat_completion через собственный
        AIService instance (общий instance не потокобезопасен из-за
        last_error/session state). Каждый вызов резервируется в llm-бюджете
        run-а по одному; применение результатов строго последовательное,
        SQLite получает одного писателя.
        """
        from concurrent.futures import ThreadPoolExecutor

        collect_fn = collect_fn or cls._collect_characteristic_chunk
        apply_fn = apply_fn or cls._apply_characteristic_response

        did_any = False
        ctxs = []
        # Bounded: не больше 2×concurrency попыток collect за batch, чтобы
        # tick не стал безлимитным на runs с массовыми drift/failed items.
        for _ in range(max(1, concurrency) * 2):
            if len(ctxs) >= max(1, concurrency):
                break
            did_work, ctx = collect_fn(run_id)
            if ctx is not None:
                did_any = True
                ctxs.append(ctx)
                continue
            if did_work:
                did_any = True
                continue
            break
        if not ctxs:
            return did_any
        ready = []
        for ctx in ctxs:
            if not cls._reserve_llm_call(run_id):
                # Бюджет исчерпан: _reserve_llm_call уже fail-closed завершил
                # run и все его незавершённые items.
                return True
            ready.append(ctx)

        from services.supplier_service import SupplierService
        run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
        services = []
        for _ in ready:
            svc = SupplierService._get_ai_service(
                supplier, model_override=run.model_used,
            )
            if not svc:
                break
            services.append(svc)

        def call(svc, ctx):
            try:
                return svc.client.chat_completion(
                    ctx['prompt'],
                    temperature=0.0,
                    max_tokens=8000,
                    response_format={'type': 'json_object'},
                )
            except Exception as exc:
                return exc

        try:
            if len(services) == len(ready) and len(ready) > 1:
                with ThreadPoolExecutor(max_workers=len(ready)) as pool:
                    futures = [
                        pool.submit(call, svc, ctx)
                        for svc, ctx in zip(services, ready)
                    ]
                    responses = [future.result() for future in futures]
            else:
                shared = services[0] if services else ai_service
                responses = [call(shared, ctx) for ctx in ready]
        finally:
            for svc in services:
                try:
                    svc.close()
                except Exception:
                    pass

        for ctx, response in zip(ready, responses):
            apply_fn(ctx, ai_service.client, response)
        return True

    # ------------------------------------------------------------------
    # Inference-режим: предложения незаполненных словарных характеристик
    # ------------------------------------------------------------------

    @staticmethod
    def _filled_characteristic_names(product: SupplierProduct) -> set:
        """Casefold-имена уже заполненных характеристик товара.

        Учитывает факты фида (characteristics_json) и применённые
        enrichment-значения (ai_marketplace_json без _meta)."""
        names = set()
        raw = _json_load(product.characteristics_json, [])
        if isinstance(raw, dict):
            names.update(str(k) for k in raw.keys())
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get('name'):
                    names.add(str(item['name']))
        marketplace_values = _json_load(product.ai_marketplace_json, {})
        if isinstance(marketplace_values, dict):
            names.update(
                str(k) for k in marketplace_values.keys()
                if not str(k).startswith('_')
            )
        return {name.strip().casefold() for name in names if name.strip()}

    @classmethod
    def _collect_inference_chunk(
        cls, run_id: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Чанк inference: до 6 товаров одного subject, prompt с filled и
        только незаполненными словарными полями. LLM-бюджет не резервируется.
        """
        first = SupplierCatalogEnrichmentItem.query.filter_by(
            run_id=run_id, status='pending', phase='characteristics',
        ).order_by(SupplierCatalogEnrichmentItem.ordinal).first()
        if not first:
            return False, None
        run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
        reference = cls.require_reference()
        if not cls._same_reference(
            _json_load(run.reference_snapshot_json, {}), reference,
        ):
            raise SupplierCatalogEnrichmentError(
                'wb_reference_changed',
                'Снимок категорий WB изменился; запустите обработку заново.',
            )
        first_product = db.session.get(SupplierProduct, first.supplier_product_id)
        if not first_product or not first_product.wb_subject_id:
            first.status = 'failed'
            first.phase = 'done'
            first.error_code = 'category_missing'
            first.error_message = 'Нет подтверждённой конечной категории WB.'
            first.completed_at = datetime.utcnow()
            db.session.commit()
            return True, None
        subject_id = int(first_product.wb_subject_id)
        candidate_items = SupplierCatalogEnrichmentItem.query.join(
            SupplierProduct,
            SupplierProduct.id
            == SupplierCatalogEnrichmentItem.supplier_product_id,
        ).filter(
            SupplierCatalogEnrichmentItem.run_id == run_id,
            SupplierCatalogEnrichmentItem.status == 'pending',
            SupplierCatalogEnrichmentItem.phase == 'characteristics',
            SupplierProduct.wb_subject_id == subject_id,
        ).order_by(SupplierCatalogEnrichmentItem.ordinal).limit(
            CHARACTERISTIC_BATCH_SIZE
        ).all()
        item_ids = [item.id for item in candidate_items]
        product_ids = [item.supplier_product_id for item in candidate_items]
        products = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == run.supplier_id,
            SupplierProduct.id.in_(product_ids),
        ).all()
        product_by_id = {product.id: product for product in products}
        marketplace = Marketplace.query.filter_by(code='wb').first()
        category = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace.id if marketplace else -1,
            subject_id=subject_id,
        ).first()
        if not marketplace or not category:
            cls._mark_batch_error(
                run_id, item_ids, 'wb_schema_unavailable',
                'Категория WB или её схема не найдена.',
            )
            return True, None
        try:
            characteristics, schema = cls._characteristic_schema(
                marketplace, category,
            )
        except SupplierCatalogEnrichmentError as exc:
            for item in candidate_items:
                item.attempt_count += 1
            db.session.commit()
            cls._mark_batch_error(run_id, item_ids, exc.code, str(exc))
            return True, None

        # Только словарные поля: непустой полный allowed_values и не number.
        dictionary_fields = [
            field for field in schema
            if field.get('allowed_values') and field.get('type') != 'number'
        ]

        now = datetime.utcnow()
        entries = []
        target_fingerprints = {}
        active_item_ids = []
        open_names_by_product: Dict[int, set] = {}

        for item in candidate_items:
            product = product_by_id.get(item.supplier_product_id)
            if not product:
                item.status = 'failed'
                item.phase = 'done'
                item.error_code = 'product_not_found'
                item.error_message = 'Товар больше не существует.'
                item.completed_at = now
                continue
            if _source_fingerprint(product) != item.source_fingerprint:
                cls._mark_item_drift(
                    item,
                    'source_changed',
                    'Исходные данные изменились после создания запуска.',
                )
                continue
            filled_names = cls._filled_characteristic_names(product)
            open_fields = [
                field for field in dictionary_fields
                if field['name'].strip().casefold() not in filled_names
            ]
            if not open_fields:
                item.status = 'unchanged'
                item.phase = 'done'
                item.error_code = None
                item.error_message = None
                item.completed_at = now
                continue
            filled_map = {}
            raw = _json_load(product.characteristics_json, [])
            if isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, dict) and entry.get('name'):
                        filled_map[str(entry['name'])] = _bounded_text(
                            entry.get('value'), 200,
                        )
            elif isinstance(raw, dict):
                for name, value in raw.items():
                    filled_map[str(name)] = _bounded_text(value, 200)
            marketplace_values = _json_load(product.ai_marketplace_json, {})
            if isinstance(marketplace_values, dict):
                for name, value in marketplace_values.items():
                    if not str(name).startswith('_'):
                        filled_map[str(name)] = _bounded_text(value, 200)

            item.status = 'running'
            item.attempt_count += 1
            item.started_at = item.started_at or now
            entries.append({
                'product_id': product.id,
                'source': _product_source(product),
                'filled': filled_map,
                'open_fields': [{
                    'name': field['name'],
                    'allowed_values': field['allowed_values'],
                    'allowed_values_truncated': field['allowed_values_truncated'],
                    'max_count': field['max_count'],
                } for field in open_fields],
            })
            active_item_ids.append(item.id)
            open_names_by_product[product.id] = {
                field['name'] for field in open_fields
            }
            target_fingerprints[product.id] = _target_fingerprint(product)
        run.current_label = f'Предположения: {category.subject_name}'
        run.heartbeat_at = now
        db.session.commit()

        if not entries:
            return True, None
        return True, {
            'run_id': run_id,
            'subject_id': subject_id,
            'marketplace': marketplace,
            'category': category,
            'characteristics': characteristics,
            'schema': schema,
            'entries': entries,
            'active_item_ids': active_item_ids,
            'candidate_items': candidate_items,
            'target_fingerprints': target_fingerprints,
            'open_names_by_product': open_names_by_product,
            'prompt': cls._inference_prompt(category, entries),
        }

    @staticmethod
    def _inference_prompt(
        category: MarketplaceCategory,
        entries: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        system = (
            'Ты предлагаешь ВЕРОЯТНЫЕ значения незаполненных словарных '
            'характеристик товара для одной категории WB. Это предположения, '
            'а не факты: они попадут на ручное ревью и никогда не применяются '
            'автоматически. Используй FILLED и SOURCE как контекст. Для '
            'каждого предложения выбирай значение СТРОГО из allowed_values '
            'нужного поля из списка OPEN_FIELDS этого товара; другие поля не '
            'предлагай. Верни короткое rationale (до 200 символов) и '
            'confidence 0..1. Если не уверен — пропусти поле. Физические '
            'величины (вес, размеры) не предлагай никогда. Верни ровно один '
            'result на каждый product_id, только JSON.'
        )
        payload = {
            'category': {
                'subject_id': category.subject_id,
                'subject_name': category.subject_name,
            },
            'products': [{
                'product_id': entry['product_id'],
                'source': entry['source'],
                'filled': entry['filled'],
                'open_fields': entry['open_fields'],
            } for entry in entries],
        }
        user = (
            'Формат: {"results":[{"product_id":1,"suggestions":'
            '[{"name":"Точное имя","value":["значение"],'
            '"rationale":"почему","confidence":0.8}]}]}.'
            '\nДанные:\n' + _json_dump(payload)
        )
        return [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ]

    @staticmethod
    def _validate_inference_response(
        response: Any,
        entries: Sequence[Dict[str, Any]],
        open_names_by_product: Dict[int, set],
    ) -> Dict[int, List[Dict[str, Any]]]:
        data = _extract_json_object(response)
        results = data.get('results')
        if not isinstance(results, list):
            raise ValueError('Response must contain results list')
        expected = {entry['product_id'] for entry in entries}
        parsed: Dict[int, List[Dict[str, Any]]] = {}
        for item in results:
            if not isinstance(item, dict) or set(item) != {
                'product_id', 'suggestions',
            }:
                raise ValueError('Invalid result object')
            product_id = item.get('product_id')
            if (
                isinstance(product_id, bool)
                or not isinstance(product_id, int)
                or product_id not in expected
                or product_id in parsed
            ):
                raise ValueError('Foreign, invalid or duplicate product_id')
            suggestions = item.get('suggestions')
            if not isinstance(suggestions, list):
                raise ValueError('suggestions must be a list')
            open_names = open_names_by_product.get(product_id, set())
            cleaned = []
            seen_names = set()
            for suggestion in suggestions:
                if not isinstance(suggestion, dict):
                    continue
                name = suggestion.get('name')
                value = suggestion.get('value')
                confidence = suggestion.get('confidence')
                if not isinstance(name, str) or name not in open_names:
                    continue
                if name in seen_names:
                    continue
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, list) or not all(
                    isinstance(v, str) and v.strip() for v in value
                ):
                    continue
                if isinstance(confidence, bool) or not isinstance(
                    confidence, (int, float),
                ) or not 0 <= float(confidence) <= 1:
                    continue
                seen_names.add(name)
                cleaned.append({
                    'name': name,
                    'value': [v.strip() for v in value],
                    'rationale': _bounded_text(
                        suggestion.get('rationale'), 200,
                    ),
                    'confidence': round(float(confidence), 3),
                })
            parsed[product_id] = cleaned
        missing = expected - set(parsed)
        if missing:
            raise ValueError('Response is missing product results')
        return parsed

    @classmethod
    def _apply_inference_response(
        cls, ctx: Dict[str, Any], validation_client, response: Any,
    ) -> None:
        """Сохранить предложения в item.inference_json (needs_review).

        Карточка товара НЕ меняется: применение — только отдельным
        подтверждением админа через apply_inference_selection."""
        run_id = ctx['run_id']
        subject_id = ctx['subject_id']
        characteristics = ctx['characteristics']
        entries = ctx['entries']
        active_item_ids = ctx['active_item_ids']
        candidate_items = ctx['candidate_items']
        target_fingerprints = ctx['target_fingerprints']
        open_names_by_product = ctx['open_names_by_product']
        if isinstance(response, Exception):
            cls._mark_batch_error(
                run_id, active_item_ids, 'model_call_failed',
                _public_failure('Вызов модели не выполнен', response),
            )
            return
        try:
            raw_results = cls._validate_inference_response(
                response, entries, open_names_by_product,
            )
        except Exception as exc:
            cls._mark_batch_error(
                run_id, active_item_ids, 'invalid_model_response', str(exc),
            )
            return

        from services.marketplace_ai_parser import MarketplaceAwareParsingTask
        validator = MarketplaceAwareParsingTask(
            validation_client, characteristics, category_id=subject_id,
        )
        item_by_product = {
            item.supplier_product_id: item for item in candidate_items
        }
        try:
            for entry in entries:
                product_id = entry['product_id']
                product = db.session.get(SupplierProduct, product_id)
                item = item_by_product[product_id]
                if _source_fingerprint(product) != item.source_fingerprint:
                    cls._mark_item_drift(
                        item,
                        'source_changed',
                        'Исходные данные изменились во время model-вызова.',
                    )
                    continue
                if _target_fingerprint(product) != target_fingerprints[product_id]:
                    cls._mark_item_drift(
                        item, 'card_changed', 'Карточку изменили параллельно.',
                    )
                    continue

                canonical = []
                for suggestion in raw_results[product_id]:
                    validated = validator.parse_response(_json_dump({
                        suggestion['name']: suggestion['value'],
                    })) or {}
                    validated.pop('_meta', None)
                    value = validated.get(suggestion['name'])
                    if value in (None, '', []):
                        continue
                    canonical.append({
                        'name': suggestion['name'],
                        'value': value,
                        'rationale': suggestion['rationale'],
                        'confidence': suggestion['confidence'],
                    })

                item.phase = 'done'
                item.error_code = None
                item.error_message = None
                item.completed_at = datetime.utcnow()
                if canonical:
                    canonical.sort(
                        key=lambda s: s['confidence'], reverse=True,
                    )
                    item.inference_json = _json_dump(canonical)
                    item.status = 'needs_review'
                else:
                    item.status = 'unchanged'
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            cls._mark_batch_error(
                run_id, active_item_ids, 'inference_apply_failed',
                _public_failure('Предложения не сохранены', exc),
            )

    @classmethod
    def apply_inference_selection(
        cls,
        run_id: str,
        item_id: int,
        supplier_id: int,
        admin_user_id: int,
        field_names: Sequence[str],
    ) -> Dict[str, Any]:
        """Применить выбранные админом inference-предложения к товару.

        Повторно проверяет scope, отсутствие другого active run, свежесть
        схемы/словарей, drift source и заново канонизирует каждое значение.
        """
        run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
        if not run or run.supplier_id != supplier_id:
            raise SupplierCatalogEnrichmentError(
                'run_not_found', 'Запуск не найден.',
            )
        if run.mode != MODE_INFERENCE:
            raise SupplierCatalogEnrichmentError(
                'invalid_mode', 'Этот запуск не является inference-режимом.',
            )
        other_active = SupplierCatalogEnrichmentRun.query.filter(
            SupplierCatalogEnrichmentRun.supplier_id == supplier_id,
            SupplierCatalogEnrichmentRun.status.in_(ACTIVE_RUN_STATUSES),
            SupplierCatalogEnrichmentRun.id != run_id,
        ).first()
        if other_active:
            raise SupplierCatalogEnrichmentError(
                'run_already_active',
                'Для этого поставщика выполняется другое обогащение.',
            )
        item = db.session.get(SupplierCatalogEnrichmentItem, item_id)
        if not item or item.run_id != run_id:
            raise SupplierCatalogEnrichmentError(
                'item_not_found', 'Строка запуска не найдена.',
            )
        if item.status != 'needs_review' or not item.inference_json:
            raise SupplierCatalogEnrichmentError(
                'item_not_reviewable', 'Для строки нет предложений на ревью.',
            )
        selected = [
            str(name).strip() for name in (field_names or [])
            if str(name).strip()
        ]
        if not selected:
            raise SupplierCatalogEnrichmentError(
                'empty_selection', 'Выберите хотя бы одно предложение.',
            )
        if len(selected) > 100 or any(len(name) > 100 for name in selected):
            raise SupplierCatalogEnrichmentError(
                'invalid_selection', 'Слишком длинный список предложений.',
            )
        suggestions = {
            s['name']: s for s in _json_load(item.inference_json, [])
            if isinstance(s, dict) and s.get('name')
        }
        unknown = [name for name in selected if name not in suggestions]
        if unknown:
            raise SupplierCatalogEnrichmentError(
                'invalid_selection',
                'Выбраны предложения, которых нет в этой строке.',
            )
        product = db.session.get(SupplierProduct, item.supplier_product_id)
        if not product or product.supplier_id != supplier_id:
            raise SupplierCatalogEnrichmentError(
                'product_not_found', 'Товар не найден.',
            )
        if _source_fingerprint(product) != item.source_fingerprint:
            item.error_code = 'source_changed'
            item.error_message = (
                'Исходные данные изменились; предложения устарели.'
            )
            db.session.commit()
            raise SupplierCatalogEnrichmentError(
                'source_changed',
                'Исходные данные товара изменились; предложения устарели.',
            )
        cls.require_reference()
        marketplace = Marketplace.query.filter_by(code='wb').first()
        category = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace.id if marketplace else -1,
            subject_id=product.wb_subject_id,
        ).first()
        if not marketplace or not category:
            raise SupplierCatalogEnrichmentError(
                'wb_schema_unavailable', 'Схема категории WB недоступна.',
            )
        characteristics, schema = cls._characteristic_schema(
            marketplace, category,
        )
        schema_names = {field['name'] for field in schema}
        from services.marketplace_ai_parser import MarketplaceAwareParsingTask
        from services.marketplace_validator import MarketplaceValidator
        validator = MarketplaceAwareParsingTask(
            None, characteristics, category_id=int(product.wb_subject_id),
        )

        payload = {}
        for name in selected:
            if name not in schema_names:
                raise SupplierCatalogEnrichmentError(
                    'schema_changed',
                    f'Поле «{name}» больше не входит в схему категории.',
                )
            payload[name] = suggestions[name]['value']
        validated = validator.parse_response(_json_dump(payload)) or {}
        meta = validated.pop('_meta', {})
        applied_names = [
            name for name in selected
            if validated.get(name) not in (None, '', [])
        ]
        if not applied_names:
            raise SupplierCatalogEnrichmentError(
                'validation_failed',
                'Ни одно выбранное значение не прошло словарную проверку.',
            )

        existing = _json_load(product.ai_marketplace_json, {})
        existing_meta = existing.pop('_meta', {}) if isinstance(existing, dict) else {}
        if not isinstance(existing, dict):
            existing = {}
        before = _enrichment_state(product)
        evidence_map = existing_meta.get('evidence') or {}
        for name in applied_names:
            existing[name] = validated[name]
            evidence_map[name] = 'inference: approved by admin'
        product.ai_marketplace_json = _json_dump({
            **existing,
            '_meta': {
                'source': 'supplier_catalog_enrichment',
                'model': run.model_used,
                'evidence': evidence_map,
                'validation_issues': meta.get('validation_issues', []),
            },
        })
        product.ai_model_used = run.model_used
        validation = MarketplaceValidator.validate_product_for_marketplace(
            product, marketplace.id,
        )
        product.ai_fill_pct = validation.get('fill_percentage', 0)
        product.content_revision = int(product.content_revision or 1) + 1

        if not item.before_json:
            item.before_json = _json_dump(before)
        item.after_json = _json_dump(_enrichment_state(product))
        item.characteristics_changed = True
        item.applied_revision = product.content_revision
        item.status = 'applied'
        item.error_code = None
        item.error_message = None
        item.completed_at = datetime.utcnow()
        db.session.commit()
        cls._refresh_counters(run_id)
        return {
            'applied': applied_names,
            'skipped': [n for n in selected if n not in applied_names],
        }

    @classmethod
    def _process_inference_batch(cls, run_id: str, ai_service) -> bool:
        did_work, ctx = cls._collect_inference_chunk(run_id)
        if not ctx:
            return did_work
        if not cls._reserve_llm_call(run_id):
            return True
        try:
            response = ai_service.client.chat_completion(
                ctx['prompt'],
                temperature=0.0,
                max_tokens=8000,
                response_format={'type': 'json_object'},
            )
        except Exception as exc:
            response = exc
        cls._apply_inference_response(ctx, ai_service.client, response)
        return True

    @classmethod
    def _refresh_counters(cls, run_id: str) -> SupplierCatalogEnrichmentRun:
        run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
        counts = dict(db.session.query(
            SupplierCatalogEnrichmentItem.status,
            func.count(SupplierCatalogEnrichmentItem.id),
        ).filter(
            SupplierCatalogEnrichmentItem.run_id == run_id,
        ).group_by(SupplierCatalogEnrichmentItem.status).all())
        run.applied = int(counts.get('applied', 0))
        run.unchanged = int(counts.get('unchanged', 0))
        run.needs_review = int(counts.get('needs_review', 0))
        run.failed = int(counts.get('failed', 0))
        run.cancelled = int(counts.get('cancelled', 0))
        run.processed = sum(int(counts.get(status, 0)) for status in TERMINAL_ITEM_STATUSES)
        run.heartbeat_at = datetime.utcnow()
        db.session.commit()
        return run

    @classmethod
    def _finalize_if_done(cls, run_id: str) -> bool:
        run = cls._refresh_counters(run_id)
        unfinished = SupplierCatalogEnrichmentItem.query.filter(
            SupplierCatalogEnrichmentItem.run_id == run_id,
            SupplierCatalogEnrichmentItem.status.in_(('pending', 'running')),
        ).count()
        if unfinished:
            return False
        applied_with_error = SupplierCatalogEnrichmentItem.query.filter(
            SupplierCatalogEnrichmentItem.run_id == run_id,
            SupplierCatalogEnrichmentItem.status == 'applied',
            SupplierCatalogEnrichmentItem.error_code.isnot(None),
        ).count()
        if run.status == 'cancelling' or run.cancelled:
            run.status = 'cancelled'
        elif run.error_code:
            run.status = 'failed'
        elif run.failed or run.needs_review or applied_with_error:
            run.status = 'partial'
        else:
            run.status = 'completed'
        run.current_label = None
        run.completed_at = datetime.utcnow()
        db.session.commit()
        return True

    @classmethod
    def _cancel_pending(cls, run_id: str) -> None:
        cls._finish_unfinished_items(
            run_id, terminal_status='cancelled',
        )
        cls._finalize_if_done(run_id)

    @classmethod
    def process_run(
        cls, run_id: str, batch_limit: int = MAX_BATCHES_PER_TICK,
    ) -> Optional[Dict[str, Any]]:
        run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
        if not run or run.status not in ACTIVE_RUN_STATUSES:
            return cls.serialize_run(run) if run else None
        with cls._supplier_lock(run.supplier_id) as acquired:
            if not acquired:
                return cls.serialize_run(run)
            run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
            if run.status == 'cancelling':
                cls._cancel_pending(run_id)
                return cls.serialize_run(
                    db.session.get(SupplierCatalogEnrichmentRun, run_id)
                )

            stale_before = datetime.utcnow() - timedelta(minutes=10)
            SupplierCatalogEnrichmentItem.query.filter(
                SupplierCatalogEnrichmentItem.run_id == run_id,
                SupplierCatalogEnrichmentItem.status == 'running',
                SupplierCatalogEnrichmentItem.updated_at < stale_before,
            ).update({'status': 'pending'}, synchronize_session=False)
            run.status = 'running'
            run.started_at = run.started_at or datetime.utcnow()
            run.heartbeat_at = datetime.utcnow()
            db.session.commit()

            supplier = db.session.get(Supplier, run.supplier_id)
            from services.supplier_service import SupplierService
            ai_service = SupplierService._get_ai_service(
                supplier, model_override=run.model_used,
            )
            if not ai_service:
                run.status = 'failed'
                run.error_code = 'ai_not_configured'
                run.error_message = 'AI-профиль поставщика недоступен.'
                run.completed_at = datetime.utcnow()
                cls._finish_unfinished_items(
                    run_id,
                    terminal_status='failed',
                    error_code=run.error_code,
                    error_message=run.error_message,
                )
                cls._refresh_counters(run_id)
                return cls.serialize_run(run)

            try:
                for _ in range(max(1, min(int(batch_limit), 10))):
                    run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
                    if run.status == 'cancelling':
                        cls._cancel_pending(run_id)
                        break
                    if run.status not in ('pending', 'running'):
                        break
                    did_work = cls._process_category_batch(run_id, ai_service)
                    if not did_work:
                        concurrency = _llm_concurrency()
                        if run.mode == MODE_INFERENCE:
                            collect_fn = cls._collect_inference_chunk
                            apply_fn = cls._apply_inference_response
                            sequential = cls._process_inference_batch
                        else:
                            collect_fn = cls._collect_characteristic_chunk
                            apply_fn = cls._apply_characteristic_response
                            sequential = cls._process_characteristic_batch
                        if concurrency > 1:
                            did_work = cls._process_characteristic_batches_parallel(
                                run_id, supplier, ai_service, concurrency,
                                collect_fn=collect_fn, apply_fn=apply_fn,
                            )
                        else:
                            did_work = sequential(run_id, ai_service)
                    cls._refresh_counters(run_id)
                    if not did_work or cls._finalize_if_done(run_id):
                        break
            except SupplierCatalogEnrichmentError as exc:
                db.session.rollback()
                run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
                run.status = 'failed'
                run.error_code = exc.code
                run.error_message = str(exc)
                run.completed_at = datetime.utcnow()
                cls._finish_unfinished_items(
                    run_id,
                    terminal_status='failed',
                    error_code=exc.code,
                    error_message=str(exc),
                )
                cls._refresh_counters(run_id)
            except Exception as exc:
                db.session.rollback()
                logger.error(
                    'Supplier catalog enrichment run failed: %s',
                    type(exc).__name__,
                )
                run = db.session.get(SupplierCatalogEnrichmentRun, run_id)
                run.status = 'failed'
                run.error_code = 'unexpected_error'
                run.error_message = (
                    'Неожиданная ошибка обработки; оставшиеся товары не '
                    f'изменены ({type(exc).__name__}).'
                )
                run.completed_at = datetime.utcnow()
                cls._finish_unfinished_items(
                    run_id,
                    terminal_status='failed',
                    error_code=run.error_code,
                    error_message=run.error_message,
                )
                cls._refresh_counters(run_id)
            finally:
                ai_service.close()
            return cls.serialize_run(
                db.session.get(SupplierCatalogEnrichmentRun, run_id)
            )

    @classmethod
    def process_due_runs(cls, limit: int = 2) -> List[Dict[str, Any]]:
        run_ids = [row[0] for row in db.session.query(
            SupplierCatalogEnrichmentRun.id,
        ).filter(
            SupplierCatalogEnrichmentRun.status.in_(ACTIVE_RUN_STATUSES),
        ).order_by(
            SupplierCatalogEnrichmentRun.created_at.asc(),
        ).limit(max(1, min(int(limit), 5))).all()]
        results = []
        for run_id in run_ids:
            result = cls.process_run(run_id)
            if result:
                results.append(result)
        return results

    @classmethod
    def kick(cls, flask_app, run_id: str) -> None:
        """Start one bounded batch now; scheduler remains the durable fallback."""
        def target():
            with flask_app.app_context():
                try:
                    cls.process_run(run_id, batch_limit=1)
                finally:
                    db.session.remove()

        threading.Thread(
            target=target,
            daemon=True,
            name=f'supplier-enrichment-{run_id[:8]}',
        ).start()

    @classmethod
    def request_cancel(cls, run_id: str, supplier_id: int) -> bool:
        run = SupplierCatalogEnrichmentRun.query.filter_by(
            id=run_id, supplier_id=supplier_id,
        ).first()
        if not run or run.status not in ACTIVE_RUN_STATUSES:
            return False
        run.status = 'cancelling'
        run.heartbeat_at = datetime.utcnow()
        db.session.commit()
        return True

    @classmethod
    def apply_review_category(
        cls,
        *,
        item_id: int,
        supplier_id: int,
        subject_id: int,
    ) -> SupplierCatalogEnrichmentItem:
        if (
            isinstance(subject_id, bool)
            or not isinstance(subject_id, int)
            or subject_id <= 0
        ):
            raise SupplierCatalogEnrichmentError(
                'invalid_subject_id', 'Нужен положительный subject_id.',
            )
        reference = cls.require_reference()
        item = SupplierCatalogEnrichmentItem.query.join(
            SupplierCatalogEnrichmentRun,
            SupplierCatalogEnrichmentRun.id
            == SupplierCatalogEnrichmentItem.run_id,
        ).filter(
            SupplierCatalogEnrichmentItem.id == item_id,
            SupplierCatalogEnrichmentRun.supplier_id == supplier_id,
        ).first()
        if not item or item.status != 'needs_review':
            raise SupplierCatalogEnrichmentError(
                'item_not_reviewable', 'Элемент не найден или уже обработан.',
            )
        other_active = SupplierCatalogEnrichmentRun.query.filter(
            SupplierCatalogEnrichmentRun.supplier_id == supplier_id,
            SupplierCatalogEnrichmentRun.id != item.run_id,
            SupplierCatalogEnrichmentRun.status.in_(ACTIVE_RUN_STATUSES),
        ).first()
        if other_active:
            raise SupplierCatalogEnrichmentError(
                'run_already_active',
                'Сначала завершите текущий запуск этого поставщика.',
            )
        category = MarketplaceCategory.query.filter_by(
            marketplace_id=reference['marketplace_id'],
            subject_id=subject_id,
            is_leaf=True,
            is_enabled=True,
            is_available=True,
        ).first()
        if not category:
            raise SupplierCatalogEnrichmentError(
                'category_not_allowed',
                'Можно выбрать только включённый конечный предмет из свежего WB-справочника.',
            )
        product = item.supplier_product
        before = _enrichment_state(product)
        # Manual review is a new explicit decision. Its rollback baseline must
        # be the current card, not a potentially stale snapshot from the
        # original automatic attempt.
        item.before_json = _json_dump(before)
        item.after_json = None
        item.category_changed = False
        item.characteristics_changed = False
        item.applied_revision = None
        item.source_fingerprint = _source_fingerprint(product)
        old_subject_id = product.wb_subject_id
        product.wb_subject_id = category.subject_id
        product.wb_subject_name = category.subject_name
        product.wb_category_name = category.subject_name
        product.category_confidence = 1.0
        if old_subject_id != category.subject_id:
            product.ai_marketplace_json = None
            product.marketplace_fields_json = None
            product.marketplace_validation_status = None
            product.marketplace_fill_pct = None
        changed = before != _enrichment_state(product)
        if changed:
            product.content_revision = int(product.content_revision or 1) + 1
            item.category_changed = True
            item.applied_revision = product.content_revision
        item.proposed_subject_id = category.subject_id
        item.proposed_subject_name = category.subject_name
        item.confidence = 1.0
        item.reasoning = 'Категория подтверждена администратором.'
        item.error_code = None
        item.error_message = None
        if item.run.mode == 'category_and_characteristics':
            item.phase = 'characteristics'
            item.status = 'pending'
            item.after_json = _json_dump(_enrichment_state(product))
            item.completed_at = None
            item.run.status = 'pending'
            item.run.completed_at = None
            item.run.reference_snapshot_json = _json_dump(reference)
        else:
            item.phase = 'done'
            item.status = 'applied' if changed else 'unchanged'
            item.after_json = _json_dump(_enrichment_state(product))
            item.completed_at = datetime.utcnow()
        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            concurrent = SupplierCatalogEnrichmentRun.query.filter(
                SupplierCatalogEnrichmentRun.supplier_id == supplier_id,
                SupplierCatalogEnrichmentRun.id != item.run_id,
                SupplierCatalogEnrichmentRun.status.in_(ACTIVE_RUN_STATUSES),
            ).first()
            if concurrent:
                raise SupplierCatalogEnrichmentError(
                    'run_already_active',
                    'Сначала завершите текущий запуск этого поставщика.',
                ) from exc
            raise
        cls._finalize_if_done(item.run_id)
        return item

    @classmethod
    def rollback_item(
        cls, *, item_id: int, supplier_id: int,
    ) -> SupplierCatalogEnrichmentItem:
        item = SupplierCatalogEnrichmentItem.query.join(
            SupplierCatalogEnrichmentRun,
            SupplierCatalogEnrichmentRun.id
            == SupplierCatalogEnrichmentItem.run_id,
        ).filter(
            SupplierCatalogEnrichmentItem.id == item_id,
            SupplierCatalogEnrichmentRun.supplier_id == supplier_id,
        ).first()
        if not item or item.status != 'applied':
            raise SupplierCatalogEnrichmentError(
                'item_not_rollbackable', 'Изменение не найдено или уже отменено.',
            )
        other_active = SupplierCatalogEnrichmentRun.query.filter(
            SupplierCatalogEnrichmentRun.supplier_id == supplier_id,
            SupplierCatalogEnrichmentRun.id != item.run_id,
            SupplierCatalogEnrichmentRun.status.in_(ACTIVE_RUN_STATUSES),
        ).first()
        if other_active:
            raise SupplierCatalogEnrichmentError(
                'run_already_active',
                'Сначала завершите текущий запуск этого поставщика.',
            )
        before = _json_load(item.before_json, None)
        after = _json_load(item.after_json, None)
        product = item.supplier_product
        if not before or not after:
            raise SupplierCatalogEnrichmentError(
                'rollback_snapshot_missing', 'Нет полного снимка для отката.',
            )
        if _enrichment_state(product) != after:
            item.status = 'rollback_conflict'
            item.error_code = 'rollback_conflict'
            item.error_message = (
                'После запуска карточку уже изменили; автоматический откат запрещён.'
            )
            db.session.commit()
            raise SupplierCatalogEnrichmentError(
                'rollback_conflict', item.error_message,
            )
        product.wb_subject_id = before.get('wb_subject_id')
        product.wb_subject_name = before.get('wb_subject_name')
        product.wb_category_name = before.get('wb_category_name')
        product.category_confidence = before.get('category_confidence')
        product.ai_marketplace_json = _json_dump(
            before.get('ai_marketplace_json') or {},
        ) if before.get('ai_marketplace_json') else None
        product.marketplace_fields_json = _json_dump(
            before.get('marketplace_fields_json') or {},
        ) if before.get('marketplace_fields_json') else None
        product.marketplace_validation_status = before.get(
            'marketplace_validation_status'
        )
        product.marketplace_fill_pct = before.get('marketplace_fill_pct')
        product.content_revision = int(product.content_revision or 1) + 1
        item.status = 'rolled_back'
        item.error_code = None
        item.error_message = None
        item.completed_at = datetime.utcnow()
        db.session.commit()
        cls._refresh_counters(item.run_id)
        return item
