# -*- coding: utf-8 -*-
"""Durable, seller-scoped bulk infographic campaigns.

The catalog mode is intentionally local-first: copy is frozen from a bounded
fact pack, the product RGB never goes through a language model, and rendering
falls back to a deterministic empty template.  Generation and WB publication
remain separate actions; this module has no marketplace write capability.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, or_

from models import (
    InfographicCampaign,
    InfographicCampaignItem,
    InfographicCampaignSlide,
    ImportedProduct,
    db,
)
from services.infographic_content import build_fact_pack, build_fact_safe_rich_content

logger = logging.getLogger(__name__)

MAX_EXACT_PRODUCTS = 200
MAX_REVIEW_OBJECTS = 500
MIN_SLIDES = 2
MAX_SLIDES = 8
ACTIVE_ITEM_STATUSES = frozenset({'queued', 'running'})
TERMINAL_ITEM_STATUSES = frozenset({
    'ready', 'failed', 'blocked', 'cancelled', 'conflict',
})

TEMPLATES: Dict[str, Dict[str, Any]] = {
    'botanical': {
        'label': 'Ботанический',
        'description': 'Светлый каталог, зелёный акцент и тёплая кнопка.',
        'palette': ['#173f2a', '#e0a52b', '#edf4e8', '#ffffff'],
        'mood': 'botanical_catalog',
    },
    'studio': {
        'label': 'Светлая студия',
        'description': 'Нейтральная светлая подача для большинства категорий.',
        'palette': ['#20242a', '#7a8797', '#f2f3f5', '#ffffff'],
        'mood': 'clean_studio',
    },
    'contrast': {
        'label': 'Контрастный',
        'description': 'Тёмная обложка и заметные информационные акценты.',
        'palette': ['#171717', '#d9783d', '#eee9e2', '#ffffff'],
        'mood': 'contrast_catalog',
    },
}

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='infographic-campaign')
_submitted_lock = threading.Lock()
_submitted_item_ids: set[int] = set()
INLINE_MAX_SUBMITTED = 8
STALE_RUNNING_MINUTES = 30
_recovery_lock = threading.Lock()
_last_inline_recovery: Dict[int, float] = {}


class InfographicCampaignError(ValueError):
    """Bounded public validation or campaign-state error."""

    def __init__(self, message: str, *, code: str = 'invalid_campaign'):
        super().__init__(message)
        self.code = code


class _TemplateOnlyImageService:
    """Force the existing safe renderer down its deterministic background path."""

    @staticmethod
    def generate_background(_scene_key: str):
        return False, None, 'Массовый каталог использует локальный шаблон'


def _json_load(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _strict_positive_ids(values: Any, *, limit: int, field: str) -> List[int]:
    if not isinstance(values, list) or not values:
        raise InfographicCampaignError(f'{field} должен быть непустым массивом')
    if len(values) > limit:
        raise InfographicCampaignError(
            f'За один запуск можно выбрать не более {limit} товаров',
            code='selection_too_large',
        )
    result: List[int] = []
    seen = set()
    for index, raw in enumerate(values):
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise InfographicCampaignError(
                f'{field}[{index}] должен быть positive integer',
            )
        if raw in seen:
            raise InfographicCampaignError(f'Повторяющийся ID товара: {raw}')
        seen.add(raw)
        result.append(raw)
    return result


def parse_form_product_ids(values: Iterable[str]) -> List[int]:
    """Strict parser for repeated HTML form fields; no loose ``int`` coercion."""
    raw_values = list(values)
    if not raw_values:
        raise InfographicCampaignError('Не выбраны товары')
    parsed: List[int] = []
    for index, raw in enumerate(raw_values):
        clean = str(raw or '').strip()
        if not clean.isascii() or not clean.isdigit() or clean.startswith('0'):
            raise InfographicCampaignError(
                f'product_ids[{index}] должен быть positive integer',
            )
        parsed.append(int(clean))
    return _strict_positive_ids(
        parsed, limit=MAX_EXACT_PRODUCTS, field='product_ids',
    )


def _photo_entries(product: ImportedProduct) -> List[Any]:
    for raw in (product.photo_urls, product.processed_photos):
        values = _json_load(raw, [])
        if isinstance(values, list) and values:
            return values[:10]
    return []


def _load_original_photo_bytes(product: ImportedProduct) -> bytes:
    """Use the canonical Image Lab candidate/cache contract for slide input."""
    from services.image_lab_service import fetch_original_product_bytes

    return fetch_original_product_bytes(product, photo_index=0)


def _characteristics(product: ImportedProduct) -> Dict[str, Any]:
    raw = _json_load(product.characteristics, {})
    result: Dict[str, Any] = {}
    if isinstance(raw, dict):
        result.update({
            str(key).strip(): value
            for key, value in raw.items()
            if str(key).strip() and not str(key).startswith('_')
        })
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get('name') or item.get('title')
            value = item.get('value')
            if isinstance(name, str) and name.strip() and value not in (None, '', [], {}):
                result[name.strip()] = value
    for label, value in (
        ('Страна производства', product.country),
        ('Пол', product.gender),
        ('Цвет', _json_load(product.colors, product.colors or '')),
        ('Материал', _json_load(product.materials, product.materials or '')),
    ):
        if value not in (None, '', [], {}) and label not in result:
            result[label] = value
    return result


def _source_state(product: ImportedProduct) -> Dict[str, Any]:
    return {
        'product_id': product.id,
        'title': product.title or '',
        'category': product.mapped_wb_category or product.category or '',
        'brand': product.brand or '',
        'characteristics': _characteristics(product),
        'photos': _photo_entries(product),
        'supplier_product_id': product.supplier_product_id,
        'updated_at': product.updated_at.isoformat() if product.updated_at else None,
    }


def _fact_pack(product: ImportedProduct) -> Dict[str, Any]:
    return build_fact_pack(
        title=product.title or '',
        category=product.mapped_wb_category or product.category or '',
        brand=product.brand or '',
        characteristics=_characteristics(product),
        source_prefix='imported_product',
    )


def _content_for_product(
    product: ImportedProduct,
    *,
    template_key: str,
    slide_limit: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    pack = _fact_pack(product)
    content = build_fact_safe_rich_content(pack, max_slides=slide_limit)
    if len(content.get('slides', [])) < MIN_SLIDES:
        raise ValueError(
            'Недостаточно подтверждённых фактов: нужен минимум второй слайд',
        )
    template = TEMPLATES[template_key]
    content['design_recommendations'] = {
        'color_palette': list(template['palette']),
        'font_style': 'modern',
        'overall_mood': template['mood'],
        'template_key': template_key,
        'template_version': 2,
    }
    return pack, content


def preview_products(seller_id: int, product_ids: Sequence[int]) -> List[Dict[str, Any]]:
    ids = _strict_positive_ids(
        product_ids, limit=MAX_EXACT_PRODUCTS, field='product_ids',
    )
    products = ImportedProduct.query.filter(
        ImportedProduct.seller_id == seller_id,
        ImportedProduct.id.in_(ids),
    ).all()
    by_id = {product.id: product for product in products}
    if set(by_id) != set(ids):
        raise InfographicCampaignError(
            'Часть товаров не найдена в каталоге продавца', code='scope_mismatch',
        )
    result = []
    for product_id in ids:
        product = by_id[product_id]
        photos = _photo_entries(product)
        facts = _fact_pack(product).get('facts', []) if product.title else []
        reasons = []
        if not product.title:
            reasons.append('нет названия')
        if not photos:
            reasons.append('нет исходного фото')
        if product.title and photos:
            try:
                _content_for_product(
                    product, template_key='botanical', slide_limit=6,
                )
            except (TypeError, ValueError):
                reasons.append('мало подтверждённых фактов для второго слайда')
        result.append({
            'id': product.id,
            'title': product.title or f'Товар #{product.id}',
            'category': product.mapped_wb_category or product.category or '',
            'photo_count': len(photos),
            'fact_count': len(facts),
            'ready': not reasons,
            'reasons': reasons,
        })
    return result


def create_campaign(
    *,
    seller_id: int,
    user_id: int,
    product_ids: Sequence[int],
    template_key: str = 'botanical',
    slide_limit: int = 6,
    name: str = '',
) -> InfographicCampaign:
    ids = _strict_positive_ids(
        product_ids, limit=MAX_EXACT_PRODUCTS, field='product_ids',
    )
    if template_key not in TEMPLATES:
        raise InfographicCampaignError('Неизвестный шаблон инфографики')
    if isinstance(slide_limit, bool) or not isinstance(slide_limit, int):
        raise InfographicCampaignError('slide_limit должен быть integer')
    if slide_limit < MIN_SLIDES or slide_limit > MAX_SLIDES:
        raise InfographicCampaignError(
            f'slide_limit должен быть от {MIN_SLIDES} до {MAX_SLIDES}',
        )
    clean_name = ' '.join(str(name or '').split()).strip()[:160]

    products = ImportedProduct.query.filter(
        ImportedProduct.seller_id == seller_id,
        ImportedProduct.id.in_(ids),
    ).all()
    by_id = {product.id: product for product in products}
    if set(by_id) != set(ids):
        raise InfographicCampaignError(
            'Часть товаров не найдена в каталоге продавца', code='scope_mismatch',
        )

    now = datetime.utcnow()
    campaign = InfographicCampaign(
        seller_id=seller_id,
        created_by_user_id=user_id,
        name=clean_name or f'Инфографика · {now:%d.%m.%Y %H:%M}',
        template_key=template_key,
        mode='catalog',
        status='queued',
        scope_json=_canonical_json({'kind': 'imported_product', 'ids': ids}),
        config_json=_canonical_json({
            'slide_limit': slide_limit,
            'publication_mode': 'none',
            'template_version': 2,
        }),
        total_items=len(ids),
        estimated_cost_rub=0.0,
    )
    db.session.add(campaign)
    db.session.flush()

    runnable = 0
    for product_id in ids:
        product = by_id[product_id]
        source = _source_state(product)
        source_fingerprint = _sha256_json(source)
        status = 'queued'
        error_code = None
        error_message = None
        pack: Dict[str, Any] = {}
        content: Dict[str, Any] = {}
        if not product.title:
            status, error_code = 'blocked', 'missing_title'
            error_message = 'Для инфографики требуется название товара'
        elif not source['photos']:
            status, error_code = 'blocked', 'missing_photo'
            error_message = 'Нет доступного исходного фото'
        else:
            try:
                pack, content = _content_for_product(
                    product, template_key=template_key, slide_limit=slide_limit,
                )
            except (TypeError, ValueError) as exc:
                status, error_code = 'blocked', 'insufficient_facts'
                error_message = str(exc)[:1000]
        if status == 'queued':
            runnable += 1
        item = InfographicCampaignItem(
            campaign_id=campaign.id,
            seller_id=seller_id,
            imported_product_id=product.id,
            product_title=(product.title or f'Товар #{product.id}')[:500],
            status=status,
            source_fingerprint=source_fingerprint,
            fact_pack_json=_canonical_json(pack),
            content_json=_canonical_json(content),
            error_code=error_code,
            error_message=error_message,
            total_slides=len(content.get('slides', [])) if content else 0,
            completed_at=now if status == 'blocked' else None,
        )
        db.session.add(item)

    campaign.runnable_items = runnable
    if runnable == 0:
        campaign.status = 'partial'
        campaign.failed_items = len(ids)
        campaign.completed_at = now
    else:
        refresh_campaign(campaign, commit=False)
    db.session.commit()
    return campaign


def _artifact_root() -> Path:
    return Path(os.environ.get('IMAGE_LAB_DATA_DIR', 'data/image_lab')).resolve()


def _write_artifact(item: InfographicCampaignItem, position: int, data: bytes) -> str:
    relative = (
        Path(str(item.seller_id)) / 'campaigns' / str(item.campaign_id)
        / str(item.id) / f'{position:02d}.png'
    )
    target = _artifact_root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.png.tmp')
    temporary.write_bytes(data)
    temporary.replace(target)
    return str(relative)


def slide_artifact_path(slide: InfographicCampaignSlide) -> Optional[Path]:
    if not slide.artifact_path:
        return None
    root = _artifact_root()
    candidate = (root / slide.artifact_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _refresh_item_review_count(item: InfographicCampaignItem) -> None:
    item.completed_slides = item.slides.filter_by(status='completed').count()
    item.approved_slides = item.slides.filter_by(
        status='completed', review_status='approved',
    ).count()


def refresh_campaign(campaign: InfographicCampaign, *, commit: bool = True) -> None:
    rows = campaign.items.with_entities(
        InfographicCampaignItem.status, func.count(InfographicCampaignItem.id),
    ).group_by(InfographicCampaignItem.status).all()
    counts = {status: int(count) for status, count in rows}
    partially_rendered = campaign.items.filter(
        InfographicCampaignItem.status == 'ready',
        InfographicCampaignItem.error_code.isnot(None),
    ).count()
    campaign.completed_items = max(0, counts.get('ready', 0) - partially_rendered)
    campaign.failed_items = partially_rendered + sum(counts.get(status, 0) for status in (
        'failed', 'blocked', 'conflict',
    ))
    campaign.total_slides = campaign.items.with_entities(
        func.coalesce(func.sum(InfographicCampaignItem.total_slides), 0),
    ).scalar() or 0
    campaign.completed_slides = campaign.items.with_entities(
        func.coalesce(func.sum(InfographicCampaignItem.completed_slides), 0),
    ).scalar() or 0
    campaign.approved_slides = campaign.items.with_entities(
        func.coalesce(func.sum(InfographicCampaignItem.approved_slides), 0),
    ).scalar() or 0
    campaign.approved_items = campaign.items.filter(
        InfographicCampaignItem.status == 'ready',
        InfographicCampaignItem.error_code.is_(None),
        InfographicCampaignItem.completed_slides > 0,
        InfographicCampaignItem.approved_slides
        == InfographicCampaignItem.completed_slides,
    ).count()

    if campaign.status != 'cancelled':
        active = counts.get('queued', 0) + counts.get('running', 0)
        pending_reviews = InfographicCampaignSlide.query.filter_by(
            campaign_id=campaign.id,
            seller_id=campaign.seller_id,
            status='completed',
            review_status='pending',
        ).count()
        rejected_reviews = InfographicCampaignSlide.query.filter_by(
            campaign_id=campaign.id,
            seller_id=campaign.seller_id,
            status='completed',
            review_status='rejected',
        ).count()
        if active:
            campaign.status = 'running' if counts.get('running', 0) else 'queued'
            campaign.started_at = campaign.started_at or datetime.utcnow()
        elif pending_reviews:
            campaign.status = 'review'
            campaign.completed_at = campaign.completed_at or datetime.utcnow()
        elif campaign.approved_slides:
            campaign.status = (
                'approved'
                if not campaign.failed_items
                and not rejected_reviews
                and campaign.approved_slides == campaign.completed_slides
                else 'partial'
            )
            campaign.completed_at = campaign.completed_at or datetime.utcnow()
        else:
            campaign.status = 'partial'
            campaign.completed_at = campaign.completed_at or datetime.utcnow()
    if commit:
        db.session.commit()


def _finish_item_failure(
    item_id: int,
    *,
    status: str,
    code: str,
    message: str,
) -> bool:
    item = db.session.get(InfographicCampaignItem, item_id)
    if not item:
        return False
    campaign_id = item.campaign_id
    updated = InfographicCampaignItem.query.filter_by(
        id=item_id, status='running',
    ).update({
        'status': status,
        'error_code': code[:80],
        'error_message': message[:1000],
        'completed_at': datetime.utcnow(),
    }, synchronize_session=False)
    if updated != 1:
        db.session.rollback()
        return False
    db.session.expire_all()
    campaign = db.session.get(InfographicCampaign, campaign_id)
    if campaign:
        refresh_campaign(campaign, commit=False)
    db.session.commit()
    return True


def render_item(app, item_id: int) -> bool:
    """Atomically claim and render one item; errors become bounded durable state."""
    with app.app_context():
        claimed = InfographicCampaignItem.query.filter_by(
            id=item_id, status='queued',
        ).update({
            'status': 'running',
            'started_at': datetime.utcnow(),
            'error_code': None,
            'error_message': None,
        }, synchronize_session=False)
        db.session.commit()
        if claimed != 1:
            return False
        item = db.session.get(InfographicCampaignItem, item_id)
        campaign = InfographicCampaign.query.filter_by(
            id=item.campaign_id, seller_id=item.seller_id,
        ).first()
        if not campaign or campaign.status == 'cancelled':
            _finish_item_failure(
                item_id, status='cancelled', code='campaign_cancelled',
                message='Кампания отменена до начала рендера',
            )
            return False
        product = ImportedProduct.query.filter_by(
            id=item.imported_product_id, seller_id=item.seller_id,
        ).first()
        if not product:
            _finish_item_failure(
                item_id, status='conflict', code='product_unavailable',
                message='Товар удалён или больше не принадлежит продавцу',
            )
            return False
        if _sha256_json(_source_state(product)) != item.source_fingerprint:
            _finish_item_failure(
                item_id, status='conflict', code='source_drift',
                message='Карточка или исходные фото изменились после создания кампании',
            )
            return False

        content = _json_load(item.content_json, {})
        photos = _photo_entries(product)
        try:
            from services.infographic_renderer import render_hybrid_slides

            source_photo_bytes = _load_original_photo_bytes(product)

            results = render_hybrid_slides(
                rich_content=content,
                image_service=_TemplateOnlyImageService(),
                product_photos=photos,
                product_title=product.title or '',
                supplier_product_id=product.supplier_product_id,
                source_photo_bytes=source_photo_bytes,
                max_slides=MAX_SLIDES,
            )
        except Exception as exc:  # noqa: BLE001 - durable worker boundary
            logger.exception('Infographic campaign render failed item=%s', item_id)
            _finish_item_failure(
                item_id, status='failed', code='renderer_failed',
                message=str(exc),
            )
            return False

        db.session.rollback()
        item = db.session.get(InfographicCampaignItem, item_id)
        campaign = db.session.get(InfographicCampaign, item.campaign_id) if item else None
        if not item or not campaign:
            return False
        if campaign.status == 'cancelled':
            item.status = 'cancelled'
            item.error_code = 'campaign_cancelled'
            item.error_message = 'Кампания отменена во время рендера'
            item.completed_at = datetime.utcnow()
            db.session.commit()
            return False

        product = ImportedProduct.query.filter_by(
            id=item.imported_product_id, seller_id=item.seller_id,
        ).first()
        if (
            not product
            or _sha256_json(_source_state(product)) != item.source_fingerprint
        ):
            _finish_item_failure(
                item_id, status='conflict', code='source_drift_after_render',
                message='Карточка изменилась во время рендера; результат отброшен',
            )
            return False

        active_campaign = db.session.query(InfographicCampaign.id).filter(
            InfographicCampaign.id == item.campaign_id,
            InfographicCampaign.seller_id == item.seller_id,
            InfographicCampaign.status.in_(['queued', 'running']),
        ).exists()
        finalization_claim = InfographicCampaignItem.query.filter(
            InfographicCampaignItem.id == item.id,
            InfographicCampaignItem.status == 'running',
            active_campaign,
        ).update({'status': 'running'}, synchronize_session=False)
        db.session.flush()
        if finalization_claim != 1:
            db.session.rollback()
            _finish_item_failure(
                item_id, status='cancelled', code='campaign_cancelled',
                message='Кампания отменена до сохранения результата',
            )
            return False

        item.slides.delete(synchronize_session=False)
        db.session.flush()
        successful = 0
        failed = 0
        content_slides = {
            int(slide.get('number', index + 1)): slide
            for index, slide in enumerate(content.get('slides', []))
            if isinstance(slide, dict)
        }
        for index, result in enumerate(results):
            position = max(1, int(result.get('slide_number') or index + 1))
            image_bytes = result.get('image_bytes')
            quality = result.get('quality') or {}
            quality_rejected = (
                isinstance(quality, dict) and quality.get('status') == 'rejected'
            )
            success = bool(
                result.get('success') and image_bytes and not quality_rejected
            )
            artifact_path = None
            artifact_hash = None
            if success:
                artifact_path = _write_artifact(item, position, image_bytes)
                artifact_hash = hashlib.sha256(image_bytes).hexdigest()
                successful += 1
            else:
                failed += 1
            slide = InfographicCampaignSlide(
                campaign_id=item.campaign_id,
                item_id=item.id,
                seller_id=item.seller_id,
                position=position,
                slide_type=str(result.get('slide_type') or 'unknown')[:40],
                status='completed' if success else 'failed',
                review_status='pending' if success else 'rejected',
                content_json=_canonical_json(content_slides.get(position, {})),
                quality_json=_canonical_json(quality),
                artifact_path=artifact_path,
                artifact_sha256=artifact_hash,
                error_message=(
                    str(result.get('error') or '')[:1000]
                    or ('Слайд отклонён quality gate' if quality_rejected else None)
                ),
            )
            db.session.add(slide)

        item.total_slides = max(len(results), len(content_slides))
        item.completed_slides = successful
        item.approved_slides = 0
        item.completed_at = datetime.utcnow()
        if successful:
            item.status = 'ready'
            if failed:
                item.error_code = 'partial_render'
                item.error_message = f'Не удалось собрать слайдов: {failed}'
            else:
                item.error_code = None
                item.error_message = None
        else:
            item.status = 'failed'
            item.error_code = 'no_rendered_slides'
            item.error_message = (
                str(results[0].get('error') or 'Рендер не создал ни одного слайда')[:1000]
                if results else 'Рендер не создал ни одного слайда'
            )
        db.session.flush()
        refresh_campaign(campaign, commit=False)
        db.session.commit()
        return successful > 0


def _run_submitted(app, item_id: int) -> None:
    try:
        _render_item_safely(app, item_id)
    finally:
        with _submitted_lock:
            _submitted_item_ids.discard(item_id)


def _render_item_safely(app, item_id: int) -> bool:
    """Keep an unexpected persistence failure from killing the durable worker."""
    try:
        return render_item(app, item_id)
    except Exception as exc:  # noqa: BLE001 - last worker boundary
        logger.exception(
            'Unexpected infographic campaign failure item=%s', item_id,
        )
        with app.app_context():
            db.session.rollback()
            _finish_item_failure(
                item_id,
                status='failed',
                code='worker_failed',
                message=str(exc) or 'Неожиданная ошибка worker',
            )
        return False


def recover_stale_items(
    app,
    *,
    campaign_id: Optional[int] = None,
    limit: int = 20,
) -> int:
    """Turn interrupted running rows into explicit retryable failures."""
    bounded = max(1, min(int(limit), 100))
    cutoff = datetime.utcnow() - timedelta(minutes=STALE_RUNNING_MINUTES)
    with app.app_context():
        query = db.session.query(InfographicCampaignItem.id).join(
            InfographicCampaign,
            InfographicCampaign.id == InfographicCampaignItem.campaign_id,
        ).filter(
            InfographicCampaignItem.status == 'running',
            InfographicCampaign.status.in_(['queued', 'running']),
            or_(
                InfographicCampaignItem.started_at.is_(None),
                InfographicCampaignItem.started_at <= cutoff,
            ),
        )
        if campaign_id is not None:
            query = query.filter(
                InfographicCampaignItem.campaign_id == campaign_id,
            )
        item_ids = [
            item_id for (item_id,) in query.order_by(
                InfographicCampaignItem.id.asc(),
            ).limit(bounded).all()
        ]
        recovered = 0
        for item_id in item_ids:
            if _finish_item_failure(
                item_id,
                status='failed',
                code='worker_interrupted',
                message='Рендер не завершился после перезапуска worker; можно повторить',
            ):
                recovered += 1
    return recovered


def launch_campaign(app, campaign_id: int) -> int:
    """Offer queued rows to the bounded inline executor without duplicate submits."""
    if os.environ.get('IMAGE_LAB_INLINE_WORKER', '1') != '1':
        return 0
    now = time.monotonic()
    with _recovery_lock:
        should_recover = now - _last_inline_recovery.get(campaign_id, 0) >= 60
        if should_recover:
            _last_inline_recovery[campaign_id] = now
            if len(_last_inline_recovery) > 1000:
                oldest = min(_last_inline_recovery, key=_last_inline_recovery.get)
                _last_inline_recovery.pop(oldest, None)
    if should_recover:
        recover_stale_items(app, campaign_id=campaign_id)
    with app.app_context():
        item_ids = [
            item_id for (item_id,) in db.session.query(InfographicCampaignItem.id).filter(
                InfographicCampaignItem.campaign_id == campaign_id,
                InfographicCampaignItem.status == 'queued',
            ).order_by(InfographicCampaignItem.id.asc()).limit(
                MAX_EXACT_PRODUCTS,
            ).all()
        ]
    reserved: List[int] = []
    with _submitted_lock:
        capacity = max(0, INLINE_MAX_SUBMITTED - len(_submitted_item_ids))
        for item_id in item_ids:
            if not capacity:
                break
            if item_id in _submitted_item_ids:
                continue
            _submitted_item_ids.add(item_id)
            reserved.append(item_id)
            capacity -= 1
    for item_id in reserved:
        _executor.submit(_run_submitted, app, item_id)
    return len(reserved)


def process_pending_once(app, *, limit: int = 2) -> int:
    """Bounded durable worker entrypoint used by the Image Lab runner."""
    bounded = max(1, min(int(limit), 20))
    with app.app_context():
        ids = [
            item_id for (item_id,) in db.session.query(InfographicCampaignItem.id).join(
                InfographicCampaign,
                InfographicCampaign.id == InfographicCampaignItem.campaign_id,
            ).filter(
                InfographicCampaignItem.status == 'queued',
                InfographicCampaign.status != 'cancelled',
            ).order_by(InfographicCampaignItem.id.asc()).limit(bounded).all()
        ]
    for item_id in ids:
        _render_item_safely(app, item_id)
    return len(ids)


def review_slides(
    campaign: InfographicCampaign,
    *,
    seller_id: int,
    user_id: int,
    action: str,
    slide_ids: Optional[Sequence[int]] = None,
    item_ids: Optional[Sequence[int]] = None,
) -> int:
    if campaign.seller_id != seller_id:
        raise InfographicCampaignError('Кампания не найдена', code='scope_mismatch')
    if action not in {'approve', 'reject'}:
        raise InfographicCampaignError('Неизвестное решение review')
    if bool(slide_ids) == bool(item_ids):
        raise InfographicCampaignError('Передайте ровно один scope: slide_ids или item_ids')

    query = InfographicCampaignSlide.query.filter_by(
        campaign_id=campaign.id, seller_id=seller_id, status='completed',
    )
    expected: List[int]
    if slide_ids:
        expected = _strict_positive_ids(
            slide_ids, limit=MAX_REVIEW_OBJECTS, field='slide_ids',
        )
        rows = query.filter(InfographicCampaignSlide.id.in_(expected)).all()
        if {row.id for row in rows} != set(expected):
            raise InfographicCampaignError(
                'Часть слайдов вне области кампании', code='scope_mismatch',
            )
    else:
        expected = _strict_positive_ids(
            item_ids, limit=MAX_EXACT_PRODUCTS, field='item_ids',
        )
        items = InfographicCampaignItem.query.filter(
            InfographicCampaignItem.campaign_id == campaign.id,
            InfographicCampaignItem.seller_id == seller_id,
            InfographicCampaignItem.id.in_(expected),
        ).all()
        if {item.id for item in items} != set(expected):
            raise InfographicCampaignError(
                'Часть товаров вне области кампании', code='scope_mismatch',
            )
        rows = query.filter(InfographicCampaignSlide.item_id.in_(expected)).all()
        if not rows:
            raise InfographicCampaignError('В выбранных товарах нет готовых слайдов')

    now = datetime.utcnow()
    decision = 'approved' if action == 'approve' else 'rejected'
    if action == 'approve':
        rejected_by_gate = next((
            row for row in rows
            if _json_load(row.quality_json, {}).get('status') == 'rejected'
        ), None)
        if rejected_by_gate is not None:
            raise InfographicCampaignError(
                f'Слайд #{rejected_by_gate.id} отклонён quality gate и не может быть одобрен',
                code='quality_rejected',
            )
    for row in rows:
        row.review_status = decision
        row.reviewed_by_user_id = user_id
        row.reviewed_at = now

    affected_items = {row.item_id for row in rows}
    db.session.flush()
    for item in InfographicCampaignItem.query.filter(
        InfographicCampaignItem.id.in_(affected_items),
        InfographicCampaignItem.seller_id == seller_id,
    ).all():
        _refresh_item_review_count(item)
    db.session.flush()
    refresh_campaign(campaign, commit=False)
    db.session.commit()
    return len(rows)


def cancel_campaign(campaign: InfographicCampaign, *, seller_id: int) -> int:
    if campaign.seller_id != seller_id:
        raise InfographicCampaignError('Кампания не найдена', code='scope_mismatch')
    cancelled_at = datetime.utcnow()
    claimed = InfographicCampaign.query.filter(
        InfographicCampaign.id == campaign.id,
        InfographicCampaign.seller_id == seller_id,
        InfographicCampaign.status.in_(['queued', 'running']),
    ).update({
        'status': 'cancelled',
        'completed_at': cancelled_at,
    }, synchronize_session=False)
    if claimed != 1:
        db.session.rollback()
        current = InfographicCampaign.query.filter_by(
            id=campaign.id, seller_id=seller_id,
        ).first()
        if current and current.status == 'cancelled':
            return 0
        raise InfographicCampaignError('Кампанию на этой стадии уже нельзя отменить')
    cancelled = campaign.items.filter(
        InfographicCampaignItem.status.in_(['queued', 'running']),
    ).update({
        'status': 'cancelled',
        'error_code': 'campaign_cancelled',
        'error_message': 'Кампания отменена пользователем',
        'completed_at': cancelled_at,
    }, synchronize_session=False)
    db.session.commit()
    return int(cancelled or 0)


def retry_items(
    campaign: InfographicCampaign,
    *,
    seller_id: int,
    item_ids: Sequence[int],
) -> int:
    if campaign.seller_id != seller_id or campaign.status == 'cancelled':
        raise InfographicCampaignError('Кампания недоступна', code='scope_mismatch')
    ids = _strict_positive_ids(
        item_ids, limit=MAX_EXACT_PRODUCTS, field='item_ids',
    )
    items = InfographicCampaignItem.query.filter(
        InfographicCampaignItem.campaign_id == campaign.id,
        InfographicCampaignItem.seller_id == seller_id,
        InfographicCampaignItem.id.in_(ids),
    ).all()
    if {item.id for item in items} != set(ids):
        raise InfographicCampaignError('Часть товаров вне области кампании')
    reset = 0
    config = _json_load(campaign.config_json, {})
    slide_limit = int(config.get('slide_limit') or 6)
    for item in items:
        if item.status not in {'failed', 'blocked', 'conflict'}:
            continue
        product = ImportedProduct.query.filter_by(
            id=item.imported_product_id, seller_id=seller_id,
        ).first()
        if not product or not product.title or not _photo_entries(product):
            continue
        try:
            pack, content = _content_for_product(
                product,
                template_key=campaign.template_key,
                slide_limit=slide_limit,
            )
        except (TypeError, ValueError):
            continue
        item.source_fingerprint = _sha256_json(_source_state(product))
        item.fact_pack_json = _canonical_json(pack)
        item.content_json = _canonical_json(content)
        item.status = 'queued'
        item.error_code = None
        item.error_message = None
        item.total_slides = len(content.get('slides', []))
        item.completed_slides = 0
        item.approved_slides = 0
        item.started_at = None
        item.completed_at = None
        item.slides.delete(synchronize_session=False)
        reset += 1
    db.session.flush()
    refresh_campaign(campaign, commit=False)
    db.session.commit()
    return reset


def template_options() -> List[Dict[str, Any]]:
    return [
        {'key': key, **value}
        for key, value in TEMPLATES.items()
    ]


def campaign_summary(campaign: InfographicCampaign) -> Dict[str, Any]:
    return {
        'id': campaign.id,
        'name': campaign.name,
        'template_key': campaign.template_key,
        'template_label': TEMPLATES.get(campaign.template_key, {}).get(
            'label', campaign.template_key,
        ),
        'mode': campaign.mode,
        'status': campaign.status,
        'total_items': campaign.total_items,
        'runnable_items': campaign.runnable_items,
        'completed_items': campaign.completed_items,
        'failed_items': campaign.failed_items,
        'approved_items': campaign.approved_items,
        'total_slides': campaign.total_slides,
        'completed_slides': campaign.completed_slides,
        'approved_slides': campaign.approved_slides,
        'estimated_cost_rub': float(campaign.estimated_cost_rub or 0),
        'created_at': campaign.created_at.isoformat() if campaign.created_at else None,
        'started_at': campaign.started_at.isoformat() if campaign.started_at else None,
        'completed_at': campaign.completed_at.isoformat() if campaign.completed_at else None,
        'url': f'/image-lab/campaigns/{campaign.id}',
    }


def item_summary(item: InfographicCampaignItem, *, include_slides: bool = True) -> Dict[str, Any]:
    result = {
        'id': item.id,
        'product_id': item.imported_product_id,
        'title': item.product_title,
        'status': item.status,
        'error_code': item.error_code,
        'error': item.error_message or '',
        'total_slides': item.total_slides,
        'completed_slides': item.completed_slides,
        'approved_slides': item.approved_slides,
    }
    if include_slides:
        result['slides'] = [
            {
                'id': slide.id,
                'position': slide.position,
                'slide_type': slide.slide_type,
                'status': slide.status,
                'review_status': slide.review_status,
                'quality': _json_load(slide.quality_json, {}),
                'has_artifact': bool(slide.artifact_path),
                'image_url': (
                    f'/image-lab/campaigns/{item.campaign_id}/slides/{slide.id}/image'
                    if slide.artifact_path else None
                ),
                'error': slide.error_message or '',
            }
            for slide in item.slides.order_by(
                InfographicCampaignSlide.position.asc(),
            ).all()
        ]
    return result


def campaign_item_page(
    campaign: InfographicCampaign,
    *,
    page: int = 1,
    per_page: int = 40,
) -> Dict[str, Any]:
    bounded_page = max(1, int(page))
    bounded_per_page = max(1, min(int(per_page), 100))
    query = campaign.items.order_by(InfographicCampaignItem.id.asc())
    total = query.count()
    items = query.offset((bounded_page - 1) * bounded_per_page).limit(
        bounded_per_page,
    ).all()
    item_ids = [item.id for item in items]
    slides_by_item: Dict[int, List[InfographicCampaignSlide]] = {
        item_id: [] for item_id in item_ids
    }
    if item_ids:
        for slide in InfographicCampaignSlide.query.filter(
            InfographicCampaignSlide.seller_id == campaign.seller_id,
            InfographicCampaignSlide.campaign_id == campaign.id,
            InfographicCampaignSlide.item_id.in_(item_ids),
        ).order_by(
            InfographicCampaignSlide.item_id.asc(),
            InfographicCampaignSlide.position.asc(),
        ).all():
            slides_by_item.setdefault(slide.item_id, []).append(slide)

    rows = []
    for item in items:
        row = item_summary(item, include_slides=False)
        row['slides'] = [{
            'id': slide.id,
            'position': slide.position,
            'slide_type': slide.slide_type,
            'status': slide.status,
            'review_status': slide.review_status,
            'quality': _json_load(slide.quality_json, {}),
            'has_artifact': bool(slide.artifact_path),
            'image_url': (
                f'/image-lab/campaigns/{campaign.id}/slides/{slide.id}/image'
                if slide.artifact_path else None
            ),
            'error': slide.error_message or '',
        } for slide in slides_by_item.get(item.id, [])]
        rows.append(row)
    pages = max(1, (total + bounded_per_page - 1) // bounded_per_page)
    return {
        'items': rows,
        'page': bounded_page,
        'per_page': bounded_per_page,
        'pages': pages,
        'total': total,
    }
