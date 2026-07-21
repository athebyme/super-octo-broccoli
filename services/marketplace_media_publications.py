# -*- coding: utf-8 -*-
"""Reviewed, durable and marketplace-neutral infographic publication.

WB media writes are single-attempt and always followed by live gallery
reconciliation.  Every existing WB image is cached before the write so an
exact rollback never depends on mutable WB CDN positions.  Ozon targets share
the same account/listing contract, but direct media writes remain disabled
until they are delegated to Ozon's existing full-state publication operation.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from sqlalchemy import and_, case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from models import (
    InfographicCampaign,
    InfographicCampaignItem,
    InfographicCampaignSlide,
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    MarketplaceMediaOperation,
    MarketplaceMediaOperationSlide,
    MarketplaceMediaPublication,
    Product,
    Seller,
    SellerMarketplaceAccount,
    db,
)
from services.infographic_campaigns import slide_artifact_path
from services.marketplace_adapters.types import MarketplaceCredentials
from services.marketplace_listing_media import MarketplaceListingMediaService
from services.marketplace_operation_locks import (
    release_marketplace_operation_lock,
    try_account_operation_lock,
    try_wb_seller_media_lock,
)
from services.marketplace_media_channels import (
    MarketplaceMediaChannelError,
    WbMediaTarget,
    get_media_channel_registry,
)
from services.wb_api_client import (
    WBAPIException,
    WBTransportUncertainException,
)


logger = logging.getLogger(__name__)

CONTRACT_VERSION = 1
MAX_PUBLICATION_ITEMS = 200
MAX_IMAGES = 30
PUBLIC_ASSET_TTL_HOURS = 24
RECONCILE_DEADLINE_MINUTES = 30
RECONCILE_DELAY_SECONDS = 8
UNCERTAIN_RECONCILE_SECONDS = 300
STALE_BOUNDARY_MINUTES = 10
VISUAL_HASH_DISTANCE = 10
MAX_REMOTE_IMAGE_BYTES = 32 * 1024 * 1024
ACTIVE_OPERATION_STATUSES = frozenset({
    'queued', 'preflighting', 'submitting', 'reconciling', 'uncertain',
})
TERMINAL_OPERATION_STATUSES = frozenset({
    'succeeded', 'failed', 'conflict', 'cancelled',
})

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='media-publication')
_submitted_lock = threading.Lock()
_submitted_operation_ids: set[int] = set()
INLINE_MAX_SUBMITTED = 8


class MarketplaceMediaPublicationError(ValueError):
    """Bounded validation/state error safe for seller-facing routes."""

    def __init__(self, message: str, *, code: str = 'media_publication_error'):
        super().__init__(message)
        self.code = code


def _json_load(value: Any, fallback: Any) -> Any:
    if isinstance(value, type(fallback)):
        return value
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _utc_epoch(value: datetime) -> int:
    """Convert the project's naive-UTC timestamps without host-TZ drift."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return int(value.timestamp())


def _strict_ids(
    values: Any,
    *,
    field: str,
    allow_empty: bool = False,
) -> List[int]:
    if values is None and allow_empty:
        return []
    if not isinstance(values, list):
        raise MarketplaceMediaPublicationError(f'{field} должен быть массивом')
    if not values and not allow_empty:
        raise MarketplaceMediaPublicationError(f'{field} не должен быть пустым')
    if len(values) > MAX_PUBLICATION_ITEMS:
        raise MarketplaceMediaPublicationError(
            f'За один запуск можно выбрать не более {MAX_PUBLICATION_ITEMS} товаров',
            code='selection_too_large',
        )
    result = []
    seen = set()
    for index, value in enumerate(values):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise MarketplaceMediaPublicationError(
                f'{field}[{index}] должен быть positive integer',
            )
        if value in seen:
            raise MarketplaceMediaPublicationError(
                f'{field} содержит повторяющийся ID {value}',
            )
        seen.add(value)
        result.append(value)
    return result


def _public_base_status(config: Mapping[str, Any]) -> Dict[str, Any]:
    raw = str(config.get('PUBLIC_BASE_URL') or '').strip().rstrip('/')
    if not raw:
        return {
            'ready': False,
            'code': 'public_base_url_missing',
            'message': 'Задайте PUBLIC_BASE_URL для передачи файлов маркетплейсу',
        }
    parsed = urlparse(raw)
    hostname = (parsed.hostname or '').strip().lower()
    if (
        parsed.scheme != 'https'
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return {
            'ready': False,
            'code': 'public_base_url_invalid',
            'message': 'PUBLIC_BASE_URL должен быть публичным HTTPS-адресом',
        }
    if hostname == 'localhost' or hostname.endswith('.local'):
        return {
            'ready': False,
            'code': 'public_base_url_not_public',
            'message': 'Маркетплейс не сможет скачать файлы с локального адреса',
        }
    try:
        address = ipaddress.ip_address(hostname.strip('[]'))
    except ValueError:
        address = None
    if address and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    ):
        return {
            'ready': False,
            'code': 'public_base_url_not_public',
            'message': 'PUBLIC_BASE_URL должен быть доступен из интернета',
        }
    return {'ready': True, 'base_url': raw, 'code': None, 'message': ''}


def public_base_status(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Public wrapper used by the UI and readiness tests."""
    return dict(_public_base_status(config))


def _selected_campaign_items(
    campaign: InfographicCampaign,
    *,
    seller_id: int,
    item_ids: Optional[Sequence[int]],
) -> List[InfographicCampaignItem]:
    if campaign.seller_id != seller_id:
        raise MarketplaceMediaPublicationError(
            'Кампания не найдена', code='scope_mismatch',
        )
    requested = _strict_ids(
        list(item_ids) if item_ids is not None else None,
        field='item_ids',
        allow_empty=item_ids is None,
    )
    query = InfographicCampaignItem.query.options(
        joinedload(InfographicCampaignItem.imported_product),
    ).filter_by(campaign_id=campaign.id, seller_id=seller_id)
    if requested:
        query = query.filter(InfographicCampaignItem.id.in_(requested))
    items = query.order_by(InfographicCampaignItem.id.asc()).all()
    if requested and {item.id for item in items} != set(requested):
        raise MarketplaceMediaPublicationError(
            'Часть товаров не входит в кампанию', code='scope_mismatch',
        )
    if not items:
        raise MarketplaceMediaPublicationError('В кампании нет выбранных товаров')
    if len(items) > MAX_PUBLICATION_ITEMS:
        raise MarketplaceMediaPublicationError(
            f'За один запуск можно выбрать не более {MAX_PUBLICATION_ITEMS} товаров',
            code='selection_too_large',
        )
    if any(
        item.imported_product is None
        or item.imported_product.seller_id != seller_id
        for item in items
    ):
        raise MarketplaceMediaPublicationError(
            'Источник одной из строк кампании больше не принадлежит продавцу',
            code='source_scope_mismatch',
        )
    return items


def _approved_slides(
    item_ids: Sequence[int],
    *,
    seller_id: int,
    campaign_id: int,
) -> Dict[int, List[InfographicCampaignSlide]]:
    result = {item_id: [] for item_id in item_ids}
    if not item_ids:
        return result
    rows = InfographicCampaignSlide.query.filter(
        InfographicCampaignSlide.item_id.in_(item_ids),
        InfographicCampaignSlide.seller_id == seller_id,
        InfographicCampaignSlide.campaign_id == campaign_id,
        InfographicCampaignSlide.status == 'completed',
        InfographicCampaignSlide.review_status == 'approved',
    ).order_by(
        InfographicCampaignSlide.item_id.asc(),
        InfographicCampaignSlide.position.asc(),
    ).all()
    for slide in rows:
        result.setdefault(slide.item_id, []).append(slide)
    return result


def _slide_snapshot(slide: InfographicCampaignSlide) -> Dict[str, Any]:
    path = slide_artifact_path(slide)
    if (
        path is None
        or not slide.artifact_sha256
        or len(slide.artifact_sha256) != 64
    ):
        raise MarketplaceMediaPublicationError(
            f'Артефакт слайда #{slide.id} недоступен',
            code='slide_artifact_missing',
        )
    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual_hash, slide.artifact_sha256):
        raise MarketplaceMediaPublicationError(
            f'Артефакт слайда #{slide.id} изменился',
            code='slide_artifact_drift',
        )
    return {
        'kind': 'generated',
        'slide_id': slide.id,
        'slide_position': slide.position,
        'sha256': slide.artifact_sha256,
        'storage_kind': 'image_lab',
        'local_path': slide.artifact_path,
        'mime_type': 'image/png',
    }


def _gallery_fingerprint(entries: Sequence[Mapping[str, Any]], video_url=None) -> str:
    return _fingerprint({
        'photos': [
            str(
                entry.get('source_url')
                or entry.get('observation_token')
                or ''
            )
            for entry in entries
        ],
        'video': str(video_url or ''),
    })


def _proposal_fingerprint(entries: Sequence[Mapping[str, Any]]) -> str:
    identities = []
    for entry in entries:
        if entry.get('kind') == 'generated':
            identities.append({
                'kind': 'generated',
                'slide_id': entry.get('slide_id'),
                'sha256': entry.get('sha256'),
            })
        else:
            identities.append({
                'kind': str(entry.get('kind') or 'current'),
                'baseline_position': entry.get('baseline_position'),
                'source_url': entry.get('source_url'),
                'sha256': entry.get('sha256'),
            })
    return _fingerprint(identities)


def _target_title(item: InfographicCampaignItem) -> str:
    return (item.product_title or f'Товар #{item.id}')[:500]


def _new_operation(
    publication: MarketplaceMediaPublication,
    item: InfographicCampaignItem,
    slides: Sequence[InfographicCampaignSlide],
    *,
    user_id: int,
    marketplace_code: str,
    account_id: Optional[int],
    listing_id: Optional[int],
    legacy_product_id: Optional[int],
    external_item_id: str,
    target: Mapping[str, Any],
    baseline: Sequence[Mapping[str, Any]],
    proposed: Sequence[Mapping[str, Any]],
    dropped: Sequence[Mapping[str, Any]],
    status: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> MarketplaceMediaOperation:
    operation = MarketplaceMediaOperation(
        publication_id=publication.id,
        seller_id=publication.seller_id,
        created_by_user_id=user_id,
        infographic_item_id=item.id,
        imported_product_id=item.imported_product_id,
        marketplace_code=marketplace_code,
        account_id=account_id,
        listing_id=listing_id,
        legacy_product_id=legacy_product_id,
        external_item_id=str(external_item_id)[:200],
        operation_kind='publish',
        status=status,
        placement_policy='prepend_approved',
        target_json=_canonical_json(dict(target)),
        source_snapshot_json=_canonical_json({
            'contract_version': CONTRACT_VERSION,
            'campaign_id': publication.campaign_id,
            'infographic_item_id': item.id,
            'title': _target_title(item),
            'slides': [
                {
                    'slide_id': slide.id,
                    'position': slide.position,
                    'artifact_sha256': slide.artifact_sha256,
                }
                for slide in slides
            ],
        }),
        baseline_media_json=_canonical_json(list(baseline)),
        proposed_media_json=_canonical_json(list(proposed)),
        dropped_media_json=_canonical_json(list(dropped)),
        baseline_fingerprint=_gallery_fingerprint(baseline),
        proposed_fingerprint=_proposal_fingerprint(proposed),
        error_code=error_code,
        error_message=(str(error_message or '')[:1000] or None),
    )
    db.session.add(operation)
    db.session.flush()
    for index, slide in enumerate(slides, start=1):
        db.session.add(MarketplaceMediaOperationSlide(
            operation_id=operation.id,
            slide_id=slide.id,
            seller_id=publication.seller_id,
            position=index,
            artifact_sha256=str(slide.artifact_sha256),
        ))
    return operation


def _wb_prepare(
    publication: MarketplaceMediaPublication,
    items: Sequence[InfographicCampaignItem],
    slides_by_item: Mapping[int, Sequence[InfographicCampaignSlide]],
    *,
    seller: Seller,
    user_id: int,
    channel=None,
) -> None:
    channel = channel or get_media_channel_registry().get('wb')
    if not seller.wb_api_key:
        raise MarketplaceMediaPublicationError(
            'Подключите API-ключ Wildberries', code='wb_credentials_missing',
        )
    products_by_id = {
        product.id: product
        for product in Product.query.filter(
            Product.seller_id == seller.id,
            Product.id.in_([
                item.imported_product.product_id
                for item in items
                if item.imported_product is not None
                and item.imported_product.product_id is not None
            ]),
        ).all()
    }
    valid_targets = []
    item_targets: Dict[int, Tuple[Product, WbMediaTarget]] = {}
    for item in items:
        product_id = (
            item.imported_product.product_id
            if item.imported_product is not None else None
        )
        product = products_by_id.get(product_id)
        if product is not None and product.is_active and product.nm_id:
            target = WbMediaTarget(int(product.nm_id), product.vendor_code or '')
            item_targets[item.id] = (product, target)
            valid_targets.append(target)

    galleries = channel.read_galleries(
        MarketplaceCredentials(
            api_key=seller.wb_api_key,
            external_account_id=str(seller.wb_seller_id or seller.id),
        ),
        valid_targets,
        audit_seller_id=seller.id,
    ) if valid_targets else {}

    for item in items:
        slides = list(slides_by_item.get(item.id, ()))
        resolved = item_targets.get(item.id)
        error_code = None
        error_message = None
        if not slides:
            error_code = 'approved_slides_missing'
            error_message = 'Для товара нет одобренных слайдов'
        elif len(slides) > channel.constraints.max_images:
            error_code = 'approved_slides_limit_exceeded'
            error_message = 'Одобренных слайдов больше лимита галереи WB'
        elif resolved is None:
            error_code = 'wb_exact_link_missing'
            error_message = 'Нет точной связи с активной карточкой WB'

        product, target = resolved if resolved else (None, None)
        gallery = galleries.get(target.nm_id) if target else None
        if error_code is None and gallery is None:
            error_code = 'wb_card_not_found'
            error_message = 'Карточка не найдена в подключённом кабинете WB'
        if error_code is None and gallery.video_url:
            error_code = 'wb_video_preservation_unsupported'
            error_message = (
                'В карточке есть видео; текущий безопасный поток не умеет '
                'гарантированно сохранить его при media/save'
            )

        baseline = list(gallery.to_state()) if gallery else []
        generated = []
        if error_code is None:
            try:
                generated = [_slide_snapshot(slide) for slide in slides]
            except MarketplaceMediaPublicationError as exc:
                error_code = exc.code
                error_message = str(exc)
        keep_count = max(0, channel.constraints.max_images - len(generated))
        retained = [
            {
                'kind': 'current',
                'baseline_position': index,
                **dict(entry),
            }
            for index, entry in enumerate(baseline[:keep_count], start=1)
        ]
        dropped = [
            {
                'kind': 'current',
                'baseline_position': index,
                **dict(entry),
            }
            for index, entry in enumerate(baseline[keep_count:], start=keep_count + 1)
        ]
        proposed = generated + retained
        target_state = {
            'contract_version': CONTRACT_VERSION,
            'entity_kind': 'legacy_wb_product',
            'marketplace_code': 'wb',
            'account_id': None,
            'listing_id': None,
            'legacy_product_id': product.id if product else None,
            'imported_product_id': item.imported_product_id,
            'nm_id': target.nm_id if target else None,
            'vendor_code': target.vendor_code if target else '',
        }
        _new_operation(
            publication,
            item,
            slides,
            user_id=user_id,
            marketplace_code='wb',
            account_id=None,
            listing_id=None,
            legacy_product_id=product.id if product else None,
            external_item_id=str(target.nm_id if target else f'unresolved-{item.id}'),
            target=target_state,
            baseline=baseline,
            proposed=proposed,
            dropped=dropped,
            status='blocked' if error_code else 'ready',
            error_code=error_code,
            error_message=error_message,
        )


def _ozon_prepare(
    publication: MarketplaceMediaPublication,
    items: Sequence[InfographicCampaignItem],
    slides_by_item: Mapping[int, Sequence[InfographicCampaignSlide]],
    *,
    seller: Seller,
    user_id: int,
    account_id: Optional[int],
) -> None:
    if not isinstance(account_id, int) or isinstance(account_id, bool) or account_id <= 0:
        raise MarketplaceMediaPublicationError(
            'Для Ozon нужен точный account_id', code='ozon_account_required',
        )
    account = SellerMarketplaceAccount.query.join(
        Marketplace, Marketplace.id == SellerMarketplaceAccount.marketplace_id,
    ).filter(
        SellerMarketplaceAccount.id == account_id,
        SellerMarketplaceAccount.seller_id == seller.id,
        SellerMarketplaceAccount.is_active.is_(True),
        Marketplace.code == 'ozon',
    ).first()
    if account is None:
        raise MarketplaceMediaPublicationError(
            'Активный кабинет Ozon не найден', code='ozon_account_scope_mismatch',
        )
    publication.account_id = account.id
    imported_ids = [item.imported_product_id for item in items if item.imported_product_id]
    listings = MarketplaceListing.query.join(
        Marketplace, Marketplace.id == MarketplaceListing.marketplace_id,
    ).filter(
        MarketplaceListing.seller_id == seller.id,
        MarketplaceListing.account_id == account.id,
        MarketplaceListing.imported_product_id.in_(imported_ids or [-1]),
        MarketplaceListing.is_available.is_(True),
        MarketplaceListing.is_archived.is_(False),
        Marketplace.code == 'ozon',
    ).all()
    by_product = {
        listing.imported_product_id: listing
        for listing in listings
        if listing.canonical_link_status == 'linked'
    }
    for item in items:
        slides = list(slides_by_item.get(item.id, ()))
        listing = by_product.get(item.imported_product_id)
        error_code = 'ozon_media_full_state_required'
        error_message = (
            'Цель Ozon зафиксирована, но публикация изображений будет доступна '
            'после подключения к full-state product operation'
        )
        if not slides:
            error_code = 'approved_slides_missing'
            error_message = 'Для товара нет одобренных слайдов'
        elif listing is None:
            error_code = 'ozon_exact_listing_missing'
            error_message = 'Нет точного связанного листинга в выбранном кабинете Ozon'
        generated = []
        if slides:
            try:
                generated = [_slide_snapshot(slide) for slide in slides]
            except MarketplaceMediaPublicationError as exc:
                error_code = exc.code
                error_message = str(exc)
        observed_media = (
            MarketplaceListingMediaService.observed_main_image_summary(listing)
            if listing is not None else {
                'main_image_count': 0,
                'main_image_fingerprint': _fingerprint([]),
                'available_main_slots': MAX_IMAGES,
            }
        )
        observed_count = int(observed_media['main_image_count'])
        observed_fingerprint = str(observed_media['main_image_fingerprint'])
        # Ozon is a typed reserve only.  Keep enough information to show the
        # planned count/order, but never turn observed provider CDN URLs into
        # a second media master or disclose them through publication JSON.
        baseline = [
            {
                'kind': 'observed',
                'baseline_position': position,
                'observation_token': (
                    f'ozon:{observed_fingerprint}:{position}'
                ),
            }
            for position in range(1, observed_count + 1)
        ]
        keep_count = max(0, MAX_IMAGES - len(generated))
        retained = [
            {
                'kind': 'current', 'baseline_position': index,
                **dict(entry),
            }
            for index, entry in enumerate(baseline[:keep_count], start=1)
        ]
        dropped = [
            {
                'kind': 'current', 'baseline_position': index,
                **dict(entry),
            }
            for index, entry in enumerate(baseline[keep_count:], start=keep_count + 1)
        ]
        target = {
            'contract_version': CONTRACT_VERSION,
            'entity_kind': 'marketplace_listing',
            'marketplace_code': 'ozon',
            'account_id': account.id,
            'listing_id': listing.id if listing else None,
            'legacy_product_id': None,
            'imported_product_id': item.imported_product_id,
            'external_product_id': listing.external_product_id if listing else None,
            'offer_id': listing.offer_id if listing else None,
            'observed_media': observed_media,
        }
        _new_operation(
            publication,
            item,
            slides,
            user_id=user_id,
            marketplace_code='ozon',
            account_id=account.id,
            listing_id=listing.id if listing else None,
            legacy_product_id=None,
            external_item_id=(
                listing.external_product_id if listing else f'unresolved-{item.id}'
            ),
            target=target,
            baseline=baseline,
            proposed=generated + retained,
            dropped=dropped,
            status='blocked',
            error_code=error_code,
            error_message=error_message,
        )


def prepare_publication(
    campaign: InfographicCampaign,
    *,
    seller_id: int,
    user_id: int,
    marketplace_code: str,
    account_id: Optional[int] = None,
    item_ids: Optional[Sequence[int]] = None,
    channel=None,
) -> MarketplaceMediaPublication:
    """Freeze an exact ordered preview without a marketplace write."""
    marketplace_code = str(marketplace_code or '').strip().lower()
    registry = get_media_channel_registry()
    try:
        constraints = registry.get(marketplace_code).constraints
    except MarketplaceMediaChannelError as exc:
        raise MarketplaceMediaPublicationError(str(exc), code=exc.code) from exc
    seller = db.session.get(Seller, seller_id)
    if seller is None:
        raise MarketplaceMediaPublicationError(
            'Кабинет продавца не найден', code='seller_missing',
        )
    items = _selected_campaign_items(
        campaign, seller_id=seller_id, item_ids=item_ids,
    )
    slides_by_item = _approved_slides(
        [item.id for item in items],
        seller_id=seller_id,
        campaign_id=campaign.id,
    )
    publication = MarketplaceMediaPublication(
        seller_id=seller_id,
        campaign_id=campaign.id,
        created_by_user_id=user_id,
        marketplace_code=marketplace_code,
        account_id=account_id if marketplace_code == 'ozon' else None,
        status='draft',
        placement_policy='prepend_approved',
        overflow_policy='trim_current_tail',
        scope_json=_canonical_json({
            'entity_kind': 'infographic_campaign_item',
            'ids': [item.id for item in items],
        }),
        constraints_json=_canonical_json(constraints.to_public_dict()),
        total_items=len(items),
    )
    db.session.add(publication)
    db.session.flush()
    try:
        if marketplace_code == 'wb':
            _wb_prepare(
                publication,
                items,
                slides_by_item,
                seller=seller,
                user_id=user_id,
                channel=channel,
            )
        elif marketplace_code == 'ozon':
            _ozon_prepare(
                publication,
                items,
                slides_by_item,
                seller=seller,
                user_id=user_id,
                account_id=account_id,
            )
        else:  # registry and service switch must move together
            raise MarketplaceMediaPublicationError(
                'Media-контур маркетплейса ещё не реализован',
                code='media_marketplace_not_implemented',
            )
        db.session.flush()
        refresh_publication(publication, commit=False)
        db.session.commit()
        return publication
    except MarketplaceMediaChannelError as exc:
        db.session.rollback()
        raise MarketplaceMediaPublicationError(str(exc), code=exc.code) from exc
    except Exception:
        db.session.rollback()
        raise


def channel_readiness(
    campaign: InfographicCampaign,
    *,
    seller_id: int,
    config: Mapping[str, Any],
) -> Dict[str, Any]:
    """Local-only channel readiness for the publication wizard."""
    if campaign.seller_id != seller_id:
        raise MarketplaceMediaPublicationError(
            'Кампания не найдена', code='scope_mismatch',
        )
    items = campaign.items.order_by(InfographicCampaignItem.id.asc()).all()
    item_ids = [item.id for item in items]
    slide_map = _approved_slides(
        item_ids,
        seller_id=seller_id,
        campaign_id=campaign.id,
    )
    approved_item_ids = [item.id for item in items if slide_map.get(item.id)]
    imported_ids = [
        item.imported_product_id
        for item in items
        if item.id in approved_item_ids and item.imported_product_id
    ]
    wb_linked = 0
    if imported_ids:
        wb_linked = ImportedProduct.query.join(
            Product, Product.id == ImportedProduct.product_id,
        ).filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.id.in_(imported_ids),
            Product.seller_id == seller_id,
            Product.is_active.is_(True),
            Product.nm_id.isnot(None),
        ).count()

    ozon_accounts: Dict[int, Dict[str, Any]] = {}
    for offset in range(0, len(imported_ids), 150):
        targets = MarketplaceListingMediaService.targets_for_products(
            seller_id=seller_id,
            imported_product_ids=imported_ids[offset:offset + 150],
        )
        for product_targets in targets.values():
            for target in product_targets:
                account = ozon_accounts.setdefault(target['account_id'], {
                    'account_id': target['account_id'],
                    'label': target['account_label'],
                    'linked_items': 0,
                    '_products': set(),
                })
                product_id = target['imported_product_id']
                if product_id not in account['_products']:
                    account['_products'].add(product_id)
                    account['linked_items'] += 1
    ozon_rows = []
    for account in ozon_accounts.values():
        account.pop('_products', None)
        account['publication_supported'] = False
        account['reason'] = 'Ожидает подключения к Ozon full-state operation'
        ozon_rows.append(account)
    ozon_rows.sort(key=lambda row: (str(row['label']).casefold(), row['account_id']))
    return {
        'approved_items': len(approved_item_ids),
        'approved_item_ids': approved_item_ids,
        'public_host': _public_base_status(config),
        'channels': {
            'wb': {
                **get_media_channel_registry().get('wb').constraints.to_public_dict(),
                'linked_items': int(wb_linked),
            },
            'ozon': {
                **get_media_channel_registry().get('ozon').constraints.to_public_dict(),
                'accounts': ozon_rows,
                'feature_enabled': bool(config.get('MARKETPLACE_OZON_ENABLED')),
            },
        },
    }


def refresh_publication(
    publication: MarketplaceMediaPublication,
    *,
    commit: bool = True,
) -> None:
    """Recalculate batch state from independent initial publish operations."""
    rows = publication.operations.filter_by(operation_kind='publish').with_entities(
        MarketplaceMediaOperation.status,
        func.count(MarketplaceMediaOperation.id),
    ).group_by(MarketplaceMediaOperation.status).all()
    counts = {status: int(count or 0) for status, count in rows}
    publication.total_items = sum(counts.values())
    publication.ready_items = counts.get('ready', 0)
    publication.blocked_items = counts.get('blocked', 0)
    publication.queued_items = sum(
        counts.get(status, 0)
        for status in ('queued', 'preflighting', 'submitting', 'reconciling')
    )
    publication.succeeded_items = counts.get('succeeded', 0)
    publication.failed_items = sum(
        counts.get(status, 0)
        for status in ('failed', 'conflict', 'cancelled')
    )
    publication.uncertain_items = counts.get('uncertain', 0)

    active_count = sum(counts.get(status, 0) for status in ACTIVE_OPERATION_STATUSES)
    if publication.status == 'cancelled' and active_count == 0:
        pass
    elif publication.confirmed_at is None:
        publication.status = 'ready' if publication.ready_items else 'draft'
        publication.completed_at = None
    elif active_count:
        publication.status = (
            'queued'
            if counts.get('queued', 0) == active_count
            else 'running'
        )
        publication.completed_at = None
    else:
        publishable = publication.total_items - publication.blocked_items
        if publishable > 0 and publication.succeeded_items == publishable:
            publication.status = (
                'succeeded' if publication.blocked_items == 0 else 'partial'
            )
        else:
            publication.status = 'partial'
        publication.completed_at = publication.completed_at or datetime.utcnow()
    if commit:
        db.session.commit()


def _safe_preview_entry(
    operation: MarketplaceMediaOperation,
    entry: Mapping[str, Any],
    position: int,
) -> Dict[str, Any]:
    kind = str(entry.get('kind') or 'current')
    result = {
        'position': position,
        'kind': kind,
        'sha256': str(entry.get('sha256') or '')[:64] or None,
        'visual_hash': str(entry.get('visual_hash') or '')[:16] or None,
    }
    if kind == 'generated':
        slide_id = entry.get('slide_id')
        result.update({
            'slide_id': slide_id,
            'slide_position': entry.get('slide_position'),
            'preview_url': (
                f'/image-lab/campaigns/{operation.publication.campaign_id}'
                f'/slides/{slide_id}/image'
                if slide_id and operation.publication.campaign_id else None
            ),
        })
    else:
        result.update({
            'baseline_position': entry.get('baseline_position'),
            'preview_url': entry.get('source_url'),
        })
    return result


def operation_summary(
    operation: MarketplaceMediaOperation,
    *,
    detail: bool = False,
) -> Dict[str, Any]:
    source = _json_load(operation.source_snapshot_json, {})
    target = _json_load(operation.target_json, {})
    proposed = _json_load(operation.proposed_media_json, [])
    dropped = _json_load(operation.dropped_media_json, [])
    rollback_rows = operation.rollback_operations.order_by(
        MarketplaceMediaOperation.id.desc(),
    ).all() if operation.operation_kind == 'publish' else []
    rollback = rollback_rows[0] if rollback_rows else None
    rollback_blocks_new = any(
        row.status in ACTIVE_OPERATION_STATUSES or row.status == 'succeeded'
        for row in rollback_rows
    )
    result = {
        'id': operation.id,
        'publication_id': operation.publication_id,
        'rollback_of_operation_id': operation.rollback_of_operation_id,
        'operation_kind': operation.operation_kind,
        'status': operation.status,
        'marketplace_code': operation.marketplace_code,
        'account_id': operation.account_id,
        'listing_id': operation.listing_id,
        'legacy_product_id': operation.legacy_product_id,
        'imported_product_id': operation.imported_product_id,
        'infographic_item_id': operation.infographic_item_id,
        'external_item_id': operation.external_item_id,
        'title': source.get('title') or (
            operation.infographic_item.product_title
            if operation.infographic_item else f'Операция #{operation.id}'
        ),
        'placement_policy': operation.placement_policy,
        'current_count': len(_json_load(operation.baseline_media_json, [])),
        'proposed_count': len(proposed),
        'generated_count': sum(
            1 for entry in proposed if entry.get('kind') == 'generated'
        ),
        'dropped_count': len(dropped),
        'attempt_count': operation.attempt_count,
        'reconcile_count': operation.reconcile_count,
        'error_code': operation.error_code,
        'error': operation.error_message or '',
        'version': operation.version,
        'submitted_at': (
            operation.submitted_at.isoformat() if operation.submitted_at else None
        ),
        'completed_at': (
            operation.completed_at.isoformat() if operation.completed_at else None
        ),
        'rollback': (
            {
                'id': rollback.id,
                'status': rollback.status,
                'error_code': rollback.error_code,
                'error': rollback.error_message or '',
            }
            if rollback else None
        ),
        'can_rollback': bool(
            operation.operation_kind == 'publish'
            and operation.status == 'succeeded'
            and not rollback_blocks_new
            and _json_load(operation.baseline_media_json, [])
        ),
        'target': {
            key: target.get(key)
            for key in (
                'entity_kind', 'marketplace_code', 'account_id', 'listing_id',
                'legacy_product_id', 'imported_product_id', 'nm_id',
                'observed_media',
            )
        },
    }
    if detail:
        result.update({
            'proposed_media': [
                _safe_preview_entry(operation, entry, index)
                for index, entry in enumerate(proposed, start=1)
            ],
            'dropped_media': [
                {
                    'position': index,
                    'baseline_position': entry.get('baseline_position'),
                    'preview_url': entry.get('source_url'),
                }
                for index, entry in enumerate(dropped, start=1)
            ],
            'baseline_fingerprint': operation.baseline_fingerprint,
            'proposed_fingerprint': operation.proposed_fingerprint,
            'confirmed_fingerprint': operation.confirmed_fingerprint,
        })
    return result


def publication_summary(
    publication: MarketplaceMediaPublication,
    *,
    detail: bool = False,
) -> Dict[str, Any]:
    data = {
        'id': publication.id,
        'campaign_id': publication.campaign_id,
        'marketplace_code': publication.marketplace_code,
        'account_id': publication.account_id,
        'account_label': publication.account.label if publication.account else None,
        'status': publication.status,
        'placement_policy': publication.placement_policy,
        'overflow_policy': publication.overflow_policy,
        'total_items': publication.total_items,
        'ready_items': publication.ready_items,
        'blocked_items': publication.blocked_items,
        'queued_items': publication.queued_items,
        'succeeded_items': publication.succeeded_items,
        'failed_items': publication.failed_items,
        'uncertain_items': publication.uncertain_items,
        'version': publication.version,
        'confirmed_at': (
            publication.confirmed_at.isoformat() if publication.confirmed_at else None
        ),
        'completed_at': (
            publication.completed_at.isoformat() if publication.completed_at else None
        ),
        'created_at': (
            publication.created_at.isoformat() if publication.created_at else None
        ),
        'url': f'/image-lab/publications/{publication.id}',
    }
    if detail:
        data.update({
            'constraints': _json_load(publication.constraints_json, {}),
            'operations': [
                operation_summary(operation, detail=True)
                for operation in publication.operations.filter_by(
                    operation_kind='publish',
                ).order_by(MarketplaceMediaOperation.id.asc()).all()
            ],
            'rollback_operations': [
                operation_summary(operation, detail=True)
                for operation in publication.operations.filter_by(
                    operation_kind='rollback',
                ).order_by(MarketplaceMediaOperation.id.asc()).all()
            ],
        })
    return data


def list_publications(
    *,
    seller_id: int,
    campaign_id: Optional[int] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    bounded = max(1, min(int(limit), 100))
    query = MarketplaceMediaPublication.query.filter_by(seller_id=seller_id)
    if campaign_id is not None:
        query = query.filter_by(campaign_id=campaign_id)
    return [
        publication_summary(row)
        for row in query.order_by(
            MarketplaceMediaPublication.created_at.desc(),
            MarketplaceMediaPublication.id.desc(),
        ).limit(bounded).all()
    ]


def confirm_publication(
    publication: MarketplaceMediaPublication,
    *,
    seller_id: int,
    user_id: int,
    expected_version: int,
    confirm_exact_order: bool,
    config: Mapping[str, Any],
) -> int:
    if publication.seller_id != seller_id:
        raise MarketplaceMediaPublicationError(
            'Публикация не найдена', code='scope_mismatch',
        )
    if (
        not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version <= 0
    ):
        raise MarketplaceMediaPublicationError('expected_version должен быть positive integer')
    if not confirm_exact_order:
        raise MarketplaceMediaPublicationError(
            'Подтвердите показанный порядок фотографий', code='confirmation_required',
        )
    if publication.version != expected_version or publication.confirmed_at is not None:
        raise MarketplaceMediaPublicationError(
            'Предпросмотр изменился; обновите страницу', code='version_conflict',
        )
    channel = get_media_channel_registry().get(publication.marketplace_code)
    if not channel.constraints.publication_supported:
        raise MarketplaceMediaPublicationError(
            'Для этого канала provider write ещё не подключён',
            code='channel_publication_not_supported',
        )
    host_status = _public_base_status(config)
    if not host_status['ready']:
        raise MarketplaceMediaPublicationError(
            host_status['message'], code=host_status['code'],
        )
    ready = publication.operations.filter_by(
        operation_kind='publish', status='ready',
    ).all()
    if not ready:
        raise MarketplaceMediaPublicationError(
            'Нет готовых карточек для публикации', code='nothing_to_publish',
        )

    for operation in ready:
        active_query = MarketplaceMediaOperation.query.filter(
            MarketplaceMediaOperation.id != operation.id,
            MarketplaceMediaOperation.status.in_(ACTIVE_OPERATION_STATUSES),
        )
        if operation.marketplace_code == 'wb':
            active_query = active_query.filter(
                MarketplaceMediaOperation.marketplace_code == 'wb',
                MarketplaceMediaOperation.legacy_product_id == operation.legacy_product_id,
            )
        else:
            active_query = active_query.filter(
                MarketplaceMediaOperation.account_id == operation.account_id,
                MarketplaceMediaOperation.listing_id == operation.listing_id,
            )
        if active_query.first() is not None:
            raise MarketplaceMediaPublicationError(
                f'Для карточки {operation.external_item_id} уже идёт media-операция',
                code='target_operation_active',
            )

    now = datetime.utcnow()
    for operation in ready:
        operation.status = 'queued'
        operation.confirmed_by_user_id = user_id
        operation.error_code = None
        operation.error_message = None
    publication.confirmed_by_user_id = user_id
    publication.confirmed_at = now
    publication.status = 'queued'
    try:
        db.session.flush()
        refresh_publication(publication, commit=False)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise MarketplaceMediaPublicationError(
            'Для одной из карточек уже началась другая media-операция',
            code='target_operation_active',
        ) from exc
    return len(ready)


def cancel_publication(
    publication: MarketplaceMediaPublication,
    *,
    seller_id: int,
) -> int:
    if publication.seller_id != seller_id:
        raise MarketplaceMediaPublicationError(
            'Публикация не найдена', code='scope_mismatch',
        )
    cancelled = publication.operations.filter(
        MarketplaceMediaOperation.operation_kind == 'publish',
        MarketplaceMediaOperation.attempt_count == 0,
        MarketplaceMediaOperation.status.in_(['ready', 'queued', 'preflighting']),
    ).update({
        'status': 'cancelled',
        'error_code': 'cancelled_by_user',
        'error_message': 'Операция отменена до provider write',
        'completed_at': datetime.utcnow(),
    }, synchronize_session=False)
    db.session.flush()
    active = publication.operations.filter(
        MarketplaceMediaOperation.status.in_(ACTIVE_OPERATION_STATUSES),
    ).count()
    if active == 0:
        publication.status = 'cancelled'
        publication.completed_at = datetime.utcnow()
    else:
        refresh_publication(publication, commit=False)
    db.session.commit()
    return int(cancelled or 0)


def _media_root() -> Path:
    configured = os.environ.get('MEDIA_PUBLICATION_DATA_DIR', '').strip()
    if configured:
        return Path(configured).resolve()
    image_root = Path(os.environ.get('IMAGE_LAB_DATA_DIR', 'data/image_lab')).resolve()
    return (image_root / 'media_publications').resolve()


def _storage_path(entry: Mapping[str, Any]) -> Optional[Path]:
    local_path = entry.get('local_path')
    if not isinstance(local_path, str) or not local_path:
        return None
    storage_kind = entry.get('storage_kind')
    if storage_kind == 'image_lab':
        root = Path(os.environ.get('IMAGE_LAB_DATA_DIR', 'data/image_lab')).resolve()
    elif storage_kind == 'media_publication':
        root = _media_root()
    else:
        return None
    candidate = (root / local_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _difference_hash(image: Image.Image) -> str:
    grayscale = image.convert('L').resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(
        grayscale.get_flattened_data()
        if hasattr(grayscale, 'get_flattened_data')
        else grayscale.getdata()
    )
    value = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            value = (value << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return f'{value:016x}'


def _local_visual_state(path: Path) -> Dict[str, Any]:
    data = path.read_bytes()
    if not data or len(data) > MAX_REMOTE_IMAGE_BYTES:
        raise MarketplaceMediaPublicationError(
            'Файл изображения пуст или превышает 32 МБ',
            code='media_asset_invalid',
        )
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('animated image')
            visual_hash = _difference_hash(image)
            width, height = image.size
    except (OSError, ValueError) as exc:
        raise MarketplaceMediaPublicationError(
            'Файл не является статическим изображением',
            code='media_asset_invalid',
        ) from exc
    if width < 700 or height < 900:
        raise MarketplaceMediaPublicationError(
            f'Изображение {width}×{height} меньше минимума WB 700×900',
            code='media_asset_resolution_too_small',
        )
    return {
        'sha256': hashlib.sha256(data).hexdigest(),
        'visual_hash': visual_hash,
        'width': width,
        'height': height,
    }


def _allowed_wb_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2_000:
        return False
    parsed = urlparse(value)
    hostname = (parsed.hostname or '').lower()
    return bool(
        parsed.scheme == 'https'
        and hostname
        and not parsed.username
        and not parsed.password
        and any(
            hostname == suffix or hostname.endswith('.' + suffix)
            for suffix in ('wbbasket.ru', 'wildberries.ru', 'wb.ru')
        )
    )


def _read_remote_image(url: str) -> bytes:
    if not _allowed_wb_url(url):
        raise MarketplaceMediaPublicationError(
            'WB вернул неподдерживаемый media host',
            code='wb_media_host_invalid',
        )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=2, pool_connections=2, pool_maxsize=2)
    session.mount('https://', adapter)
    try:
        response = session.get(
            url,
            timeout=(5, 25),
            allow_redirects=False,
            stream=True,
            headers={'Accept': 'image/*'},
        )
        if response.status_code != 200:
            raise MarketplaceMediaPublicationError(
                'Не удалось скачать текущее изображение WB',
                code='wb_media_download_failed',
            )
        content_length = response.headers.get('Content-Length')
        if content_length:
            try:
                if int(content_length) > MAX_REMOTE_IMAGE_BYTES:
                    raise MarketplaceMediaPublicationError(
                        'Текущее изображение WB превышает 32 МБ',
                        code='wb_media_too_large',
                    )
            except ValueError:
                pass
        chunks = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_REMOTE_IMAGE_BYTES:
                raise MarketplaceMediaPublicationError(
                    'Текущее изображение WB превышает 32 МБ',
                    code='wb_media_too_large',
                )
            chunks.append(chunk)
        data = b''.join(chunks)
        if not data:
            raise MarketplaceMediaPublicationError(
                'WB вернул пустое изображение', code='wb_media_download_failed',
            )
        return data
    except requests.RequestException as exc:
        raise MarketplaceMediaPublicationError(
            'Не удалось скачать текущее изображение WB',
            code='wb_media_download_failed',
        ) from exc
    finally:
        session.close()


def _normalize_remote_to_cache(
    url: str,
    target: Path,
) -> Dict[str, Any]:
    data = _read_remote_image(url)
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('animated image')
            width, height = image.size
            if width < 1 or height < 1:
                raise ValueError('empty dimensions')
            visual_hash = _difference_hash(image)
            image_format = str(image.format or '').upper()
    except (OSError, ValueError) as exc:
        raise MarketplaceMediaPublicationError(
            'Текущее медиа WB не является статическим изображением',
            code='wb_media_invalid_image',
        ) from exc
    mime_type = {
        'JPEG': 'image/jpeg',
        'PNG': 'image/png',
        'WEBP': 'image/webp',
        'BMP': 'image/bmp',
        'GIF': 'image/gif',
    }.get(image_format)
    if mime_type is None:
        raise MarketplaceMediaPublicationError(
            'Формат текущего изображения WB не поддерживается',
            code='wb_media_invalid_image',
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass
    temporary = target.with_suffix('.asset.tmp')
    temporary.write_bytes(data)
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(target)
    return {
        'sha256': hashlib.sha256(data).hexdigest(),
        'visual_hash': visual_hash,
        'width': width,
        'height': height,
        'mime_type': mime_type,
    }


def _remote_visual_hash(url: str) -> str:
    data = _read_remote_image(url)
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            return _difference_hash(image)
    except OSError as exc:
        raise MarketplaceMediaPublicationError(
            'WB вернул некорректное изображение',
            code='wb_media_invalid_image',
        ) from exc


def _visual_distance(left: str, right: str) -> int:
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except (TypeError, ValueError):
        return 64


def _prepare_initial_assets(
    operation: MarketplaceMediaOperation,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    baseline = [dict(entry) for entry in _json_load(operation.baseline_media_json, [])]
    proposed = [dict(entry) for entry in _json_load(operation.proposed_media_json, [])]
    dropped = [dict(entry) for entry in _json_load(operation.dropped_media_json, [])]
    cache_root = _media_root()
    relative_root = Path(str(operation.seller_id)) / str(operation.id) / 'baseline'
    cached_by_position = {}
    for index, entry in enumerate(baseline, start=1):
        relative = relative_root / f'{index:02d}.asset'
        target = cache_root / relative
        state = _normalize_remote_to_cache(str(entry.get('source_url') or ''), target)
        entry.update({
            **state,
            'storage_kind': 'media_publication',
            'local_path': str(relative),
        })
        cached_by_position[index] = entry

    associations = operation.operation_slides.order_by(
        MarketplaceMediaOperationSlide.position.asc(),
    ).all()
    generated = [entry for entry in proposed if entry.get('kind') == 'generated']
    if len(generated) != len(associations):
        raise MarketplaceMediaPublicationError(
            'Набор одобренных слайдов изменился', code='slide_membership_drift',
        )
    association_by_slide = {row.slide_id: row for row in associations}
    for entry in generated:
        slide_id = entry.get('slide_id')
        association = association_by_slide.get(slide_id)
        slide = db.session.get(InfographicCampaignSlide, slide_id)
        if (
            association is None
            or slide is None
            or slide.seller_id != operation.seller_id
            or slide.review_status != 'approved'
            or slide.status != 'completed'
            or slide.artifact_sha256 != association.artifact_sha256
            or entry.get('sha256') != association.artifact_sha256
        ):
            raise MarketplaceMediaPublicationError(
                'Одобренный слайд изменился после предпросмотра',
                code='approved_slide_drift',
            )
        path = _storage_path(entry)
        if path is None:
            raise MarketplaceMediaPublicationError(
                'Одобренный артефакт недоступен', code='slide_artifact_missing',
            )
        state = _local_visual_state(path)
        if not hmac.compare_digest(state['sha256'], association.artifact_sha256):
            raise MarketplaceMediaPublicationError(
                'Одобренный артефакт изменился', code='slide_artifact_drift',
            )
        entry.update(state)

    for collection in (proposed, dropped):
        for entry in collection:
            if entry.get('kind') == 'generated':
                continue
            baseline_position = entry.get('baseline_position')
            cached = cached_by_position.get(baseline_position)
            if cached is None:
                raise MarketplaceMediaPublicationError(
                    'Снимок текущей галереи повреждён',
                    code='baseline_snapshot_invalid',
                )
            entry.update({
                key: cached[key]
                for key in (
                    'sha256', 'visual_hash', 'width', 'height', 'mime_type',
                    'storage_kind', 'local_path',
                )
            })
    return baseline, proposed, dropped


def _verify_local_proposed(
    operation: MarketplaceMediaOperation,
) -> List[Dict[str, Any]]:
    proposed = [dict(entry) for entry in _json_load(operation.proposed_media_json, [])]
    if not proposed:
        raise MarketplaceMediaPublicationError(
            'Нельзя отправить пустую галерею', code='empty_gallery',
        )
    for entry in proposed:
        path = _storage_path(entry)
        if path is None:
            raise MarketplaceMediaPublicationError(
                'Локальный снимок для публикации недоступен',
                code='media_asset_missing',
            )
        state = _local_visual_state(path)
        expected_sha = str(entry.get('sha256') or '')
        if not expected_sha or not hmac.compare_digest(state['sha256'], expected_sha):
            raise MarketplaceMediaPublicationError(
                'Локальный снимок для публикации изменился',
                code='media_asset_drift',
            )
        entry.update(state)
    return proposed


def _try_operation_lock(operation: MarketplaceMediaOperation):
    if operation.marketplace_code == 'wb':
        return ('wb', try_wb_seller_media_lock(operation.seller_id))
    if operation.marketplace_code == 'ozon' and operation.account_id:
        return ('account', try_account_operation_lock(operation.account_id))
    return ('unsupported', None)


def _release_operation_lock(claim) -> None:
    if not claim:
        return
    _kind, handle = claim
    if handle is None:
        return
    release_marketplace_operation_lock(handle)


def _wb_runtime(operation: MarketplaceMediaOperation):
    if operation.marketplace_code != 'wb' or operation.account_id is not None:
        raise MarketplaceMediaPublicationError(
            'Provider write для этого канала не подключён',
            code='channel_publication_not_supported',
        )
    seller = Seller.query.filter_by(id=operation.seller_id).first()
    product = Product.query.filter_by(
        id=operation.legacy_product_id,
        seller_id=operation.seller_id,
    ).first()
    target_state = _json_load(operation.target_json, {})
    if (
        seller is None
        or product is None
        or not product.is_active
        or not product.nm_id
        or int(product.nm_id) != target_state.get('nm_id')
        or str(product.nm_id) != operation.external_item_id
    ):
        raise MarketplaceMediaPublicationError(
            'Точная связь с карточкой WB изменилась', code='target_drift',
        )
    if not seller.wb_api_key:
        raise MarketplaceMediaPublicationError(
            'API-ключ WB недоступен', code='wb_credentials_missing',
        )
    credentials = MarketplaceCredentials(
        api_key=seller.wb_api_key,
        external_account_id=str(seller.wb_seller_id or seller.id),
    )
    target = WbMediaTarget(int(product.nm_id), product.vendor_code or '')
    return seller, product, credentials, target


def _asset_signature(
    secret_key: str,
    operation_id: int,
    position: int,
    expires: int,
    sha256: str,
) -> str:
    message = f'media:{operation_id}:{position}:{expires}:{sha256}'
    return hmac.new(
        secret_key.encode('utf-8'), message.encode('utf-8'), hashlib.sha256,
    ).hexdigest()[:32]


def _public_asset_urls(
    operation: MarketplaceMediaOperation,
    *,
    config: Mapping[str, Any],
) -> List[str]:
    host = _public_base_status(config)
    if not host['ready']:
        raise MarketplaceMediaPublicationError(host['message'], code=host['code'])
    if not operation.public_assets_expires_at:
        raise MarketplaceMediaPublicationError(
            'Срок публичных ссылок не зафиксирован', code='asset_expiry_missing',
        )
    expires = _utc_epoch(operation.public_assets_expires_at)
    secret = str(config.get('SECRET_KEY') or '')
    if len(secret) < 16:
        raise MarketplaceMediaPublicationError(
            'SECRET_KEY не подходит для подписанных media URL',
            code='secret_key_invalid',
        )
    proposed = _json_load(operation.proposed_media_json, [])
    result = []
    for position, entry in enumerate(proposed, start=1):
        sha256 = str(entry.get('sha256') or '')
        signature = _asset_signature(
            secret, operation.id, position, expires, sha256,
        )
        result.append(
            f"{host['base_url']}/media-publications/assets/"
            f'{operation.id}/{position}/{expires}/{signature}.img'
        )
    return result


def resolve_public_asset(
    *,
    operation_id: int,
    position: int,
    expires: int,
    signature: str,
    secret_key: str,
    now: Optional[datetime] = None,
) -> Tuple[Path, str]:
    """Resolve one provider-fetchable asset without tenant data in the URL."""
    now = now or datetime.utcnow()
    if position <= 0 or expires <= _utc_epoch(now):
        raise MarketplaceMediaPublicationError(
            'Публичная media-ссылка истекла', code='asset_url_expired',
        )
    operation = db.session.get(MarketplaceMediaOperation, operation_id)
    if (
        operation is None
        or operation.status not in {
            'submitting', 'reconciling', 'uncertain', 'succeeded',
        }
        or operation.public_assets_expires_at is None
        or expires != _utc_epoch(operation.public_assets_expires_at)
    ):
        raise MarketplaceMediaPublicationError(
            'Публичный media-asset недоступен', code='asset_unavailable',
        )
    proposed = _json_load(operation.proposed_media_json, [])
    if position > len(proposed):
        raise MarketplaceMediaPublicationError(
            'Публичный media-asset не найден', code='asset_missing',
        )
    entry = proposed[position - 1]
    sha256 = str(entry.get('sha256') or '')
    expected = _asset_signature(
        secret_key, operation.id, position, expires, sha256,
    )
    if not hmac.compare_digest(str(signature or ''), expected):
        raise MarketplaceMediaPublicationError(
            'Неверная подпись media-asset', code='asset_signature_invalid',
        )
    path = _storage_path(entry)
    if path is None:
        raise MarketplaceMediaPublicationError(
            'Публичный media-asset не найден', code='asset_missing',
        )
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual, sha256):
        raise MarketplaceMediaPublicationError(
            'Публичный media-asset изменился', code='asset_drift',
        )
    return path, str(entry.get('mime_type') or 'application/octet-stream')


def _mark_operation(
    operation_id: int,
    *,
    status: str,
    code: Optional[str] = None,
    message: Optional[str] = None,
    next_reconcile_at: Optional[datetime] = None,
    completed: bool = False,
) -> None:
    operation = db.session.get(MarketplaceMediaOperation, operation_id)
    if operation is None:
        return
    operation.status = status
    operation.error_code = code
    operation.error_message = (str(message or '')[:1000] or None)
    operation.next_reconcile_at = next_reconcile_at
    if completed:
        operation.completed_at = operation.completed_at or datetime.utcnow()
    db.session.flush()
    refresh_publication(operation.publication, commit=False)
    db.session.commit()


def _live_gallery_state(gallery) -> List[Dict[str, Any]]:
    return [dict(entry) for entry in gallery.to_state()]


def _exact_live_matches(
    gallery,
    expected: Sequence[Mapping[str, Any]],
) -> bool:
    live_urls = [entry.get('source_url') for entry in gallery.to_state()]
    expected_urls = [entry.get('source_url') for entry in expected]
    return not gallery.video_url and live_urls == expected_urls


def _preflight_and_submit(
    operation_id: int,
    *,
    config: Mapping[str, Any],
    channel=None,
) -> bool:
    operation = db.session.get(MarketplaceMediaOperation, operation_id)
    if operation is None or operation.status != 'queued' or operation.attempt_count != 0:
        return False
    claimed = MarketplaceMediaOperation.query.filter_by(
        id=operation.id,
        status='queued',
        attempt_count=0,
    ).update({
        'status': 'preflighting',
        'error_code': None,
        'error_message': None,
    }, synchronize_session=False)
    if claimed != 1:
        db.session.rollback()
        return False
    db.session.commit()
    operation = db.session.get(MarketplaceMediaOperation, operation_id)
    try:
        channel = channel or get_media_channel_registry().get(operation.marketplace_code)
        if not channel.constraints.publication_supported:
            raise MarketplaceMediaPublicationError(
                'Provider write для канала не подключён',
                code='channel_publication_not_supported',
            )
        seller, product, credentials, target = _wb_runtime(operation)
        live = channel.read_gallery(
            credentials, target, audit_seller_id=seller.id,
        )
        expected_baseline = _json_load(operation.baseline_media_json, [])
        if not _exact_live_matches(live, expected_baseline):
            raise MarketplaceMediaPublicationError(
                'Галерея WB изменилась после предпросмотра',
                code='baseline_drift',
            )
        if operation.operation_kind == 'publish':
            if live.video_url:
                raise MarketplaceMediaPublicationError(
                    'В карточке появилось видео; безопасная публикация остановлена',
                    code='wb_video_preservation_unsupported',
                )
            baseline, proposed, dropped = _prepare_initial_assets(operation)
        else:
            baseline = [dict(entry) for entry in expected_baseline]
            proposed = _verify_local_proposed(operation)
            dropped = []

        # The provider gallery may drift while images are being cached.  Repeat
        # the exact read immediately before the durable single-attempt boundary.
        live_again = channel.read_gallery(
            credentials, target, audit_seller_id=seller.id,
        )
        if not _exact_live_matches(live_again, expected_baseline):
            raise MarketplaceMediaPublicationError(
                'Галерея WB изменилась во время подготовки',
                code='baseline_drift',
            )
        now = datetime.utcnow()
        expires_at = now + timedelta(hours=PUBLIC_ASSET_TTL_HOURS)
        deadline = now + timedelta(minutes=RECONCILE_DEADLINE_MINUTES)
        values = {
            'status': 'submitting',
            'attempt_count': 1,
            'baseline_media_json': _canonical_json(baseline),
            'proposed_media_json': _canonical_json(proposed),
            'dropped_media_json': _canonical_json(dropped),
            'public_assets_expires_at': expires_at,
            'submitted_at': now,
            'deadline_at': deadline,
            'next_reconcile_at': None,
            'error_code': None,
            'error_message': None,
        }
        boundary = MarketplaceMediaOperation.query.filter_by(
            id=operation.id,
            status='preflighting',
            attempt_count=0,
        ).update(values, synchronize_session=False)
        if boundary != 1:
            db.session.rollback()
            return False
        db.session.commit()

        operation = db.session.get(MarketplaceMediaOperation, operation_id)
        public_urls = _public_asset_urls(operation, config=config)
        try:
            channel.submit_gallery_once(
                credentials,
                target,
                public_urls,
                audit_seller_id=seller.id,
            )
        except WBTransportUncertainException as exc:
            if exc.request_may_have_been_applied:
                _mark_operation(
                    operation.id,
                    status='uncertain',
                    code='wb_write_ambiguous',
                    message='Результат записи WB неизвестен; выполняется сверка',
                    next_reconcile_at=datetime.utcnow(),
                )
            else:
                _mark_operation(
                    operation.id,
                    status='failed',
                    code='wb_write_not_sent',
                    message='WB недоступен до отправки запроса',
                    completed=True,
                )
            return False
        except WBAPIException:
            _mark_operation(
                operation.id,
                status='failed',
                code='wb_write_rejected',
                message='WB отклонил media-запрос',
                completed=True,
            )
            return False
        except Exception:  # noqa: BLE001 - boundary is already crossed
            logger.exception('Unexpected WB media write result operation=%s', operation.id)
            _mark_operation(
                operation.id,
                status='uncertain',
                code='wb_write_ambiguous',
                message='Результат записи WB неизвестен; выполняется сверка',
                next_reconcile_at=datetime.utcnow(),
            )
            return False

        _mark_operation(
            operation.id,
            status='reconciling',
            code='wb_processing',
            message='WB принял запрос; проверяем фактический порядок',
            next_reconcile_at=datetime.utcnow() + timedelta(
                seconds=RECONCILE_DELAY_SECONDS,
            ),
        )
        return True
    except MarketplaceMediaPublicationError as exc:
        _mark_operation(
            operation_id,
            status='conflict' if exc.code in {
                'baseline_drift', 'target_drift', 'approved_slide_drift',
                'slide_membership_drift', 'slide_artifact_drift',
                'media_asset_drift',
            } else 'failed',
            code=exc.code,
            message=str(exc),
            completed=True,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - durable pre-write boundary
        logger.exception('Media preflight failed operation=%s', operation_id)
        current = db.session.get(MarketplaceMediaOperation, operation_id)
        if current and current.attempt_count == 1:
            _mark_operation(
                operation_id,
                status='uncertain',
                code='worker_failed_after_boundary',
                message='Worker прервался после границы отправки; выполняется сверка',
                next_reconcile_at=datetime.utcnow(),
            )
        else:
            _mark_operation(
                operation_id,
                status='failed',
                code='preflight_failed',
                message='Не удалось безопасно подготовить media-операцию',
                completed=True,
            )
        return False


def _reconcile_operation(
    operation_id: int,
    *,
    channel=None,
) -> bool:
    operation = db.session.get(MarketplaceMediaOperation, operation_id)
    if (
        operation is None
        or operation.status not in {'reconciling', 'uncertain'}
        or operation.attempt_count != 1
    ):
        return False
    now = datetime.utcnow()
    try:
        channel = channel or get_media_channel_registry().get(operation.marketplace_code)
        seller, product, credentials, target = _wb_runtime(operation)
        gallery = channel.read_gallery(
            credentials, target, audit_seller_id=seller.id,
        )
        live_entries = _live_gallery_state(gallery)
        expected = _json_load(operation.proposed_media_json, [])
        baseline = _json_load(operation.baseline_media_json, [])
        operation.reconcile_count += 1
        operation.last_reconciled_at = now

        if len(live_entries) == len(expected) and not gallery.video_url:
            for entry in live_entries:
                entry['visual_hash'] = _remote_visual_hash(
                    str(entry.get('fingerprint_url') or ''),
                )
            matches = all(
                _visual_distance(
                    str(live.get('visual_hash') or ''),
                    str(wanted.get('visual_hash') or ''),
                ) <= VISUAL_HASH_DISTANCE
                for live, wanted in zip(live_entries, expected)
            )
            if matches:
                operation.confirmed_media_json = _canonical_json(live_entries)
                operation.confirmed_fingerprint = _fingerprint([
                    {
                        'source_url': entry.get('source_url'),
                        'visual_hash': entry.get('visual_hash'),
                    }
                    for entry in live_entries
                ])
                operation.status = 'succeeded'
                operation.error_code = None
                operation.error_message = None
                operation.next_reconcile_at = None
                operation.completed_at = now
                if product and product.seller_id == operation.seller_id:
                    product.photos_json = _canonical_json([
                        entry.get('source_url') for entry in live_entries
                    ])
                db.session.flush()
                refresh_publication(operation.publication, commit=False)
                db.session.commit()
                return True

        deadline_passed = bool(operation.deadline_at and now >= operation.deadline_at)
        still_baseline = _exact_live_matches(gallery, baseline)
        if deadline_passed and still_baseline:
            operation.status = 'failed'
            operation.error_code = 'wb_media_not_applied'
            operation.error_message = (
                'WB не применил новый порядок до окончания окна сверки'
            )
            operation.next_reconcile_at = None
            operation.completed_at = now
        elif deadline_passed:
            operation.status = 'uncertain'
            operation.error_code = 'wb_media_state_unrecognized'
            operation.error_message = (
                'Галерея WB не совпадает ни со снимком до, ни с подтверждённым порядком'
            )
            operation.next_reconcile_at = now + timedelta(
                seconds=UNCERTAIN_RECONCILE_SECONDS,
            )
        else:
            operation.status = 'reconciling'
            operation.error_code = 'wb_processing'
            operation.error_message = 'WB ещё обрабатывает новый порядок фотографий'
            operation.next_reconcile_at = now + timedelta(
                seconds=RECONCILE_DELAY_SECONDS,
            )
        db.session.flush()
        refresh_publication(operation.publication, commit=False)
        db.session.commit()
        return False
    except Exception:  # noqa: BLE001 - reconciliation must remain durable
        logger.exception('Media reconciliation failed operation=%s', operation_id)
        db.session.rollback()
        operation = db.session.get(MarketplaceMediaOperation, operation_id)
        if operation is None:
            return False
        operation.reconcile_count += 1
        operation.last_reconciled_at = now
        operation.status = 'uncertain' if (
            operation.deadline_at and now >= operation.deadline_at
        ) else operation.status
        operation.error_code = 'wb_reconcile_unavailable'
        operation.error_message = 'Не удалось прочитать фактическую галерею WB'
        operation.next_reconcile_at = now + timedelta(
            seconds=(
                UNCERTAIN_RECONCILE_SECONDS
                if operation.status == 'uncertain'
                else RECONCILE_DELAY_SECONDS
            ),
        )
        db.session.flush()
        refresh_publication(operation.publication, commit=False)
        db.session.commit()
        return False


def process_operation(
    app,
    operation_id: int,
    *,
    channel=None,
) -> bool:
    with app.app_context():
        operation = db.session.get(MarketplaceMediaOperation, operation_id)
        if operation is None:
            return False
        claim = _try_operation_lock(operation)
        if not claim or claim[1] is None:
            return False
        try:
            if operation.status == 'queued':
                return _preflight_and_submit(
                    operation.id, config=app.config, channel=channel,
                )
            if operation.status in {'reconciling', 'uncertain'}:
                return _reconcile_operation(operation.id, channel=channel)
            return False
        finally:
            _release_operation_lock(claim)


def _run_submitted(app, operation_id: int) -> None:
    try:
        process_operation(app, operation_id)
    finally:
        with _submitted_lock:
            _submitted_operation_ids.discard(operation_id)


def launch_publication(app, publication_id: int) -> int:
    """Submit a bounded number of queued operations to the inline worker."""
    enabled = str(
        app.config.get(
            'MEDIA_PUBLICATION_INLINE_WORKER',
            os.environ.get(
                'MEDIA_PUBLICATION_INLINE_WORKER',
                os.environ.get('IMAGE_LAB_INLINE_WORKER', '1'),
            ),
        )
    ).strip().lower() in {'1', 'true', 'yes', 'on'}
    if not enabled:
        return 0
    with app.app_context():
        ids = [
            row[0]
            for row in db.session.query(MarketplaceMediaOperation.id).filter_by(
                publication_id=publication_id,
                status='queued',
            ).order_by(MarketplaceMediaOperation.id.asc()).limit(
                INLINE_MAX_SUBMITTED,
            ).all()
        ]
    submitted = 0
    with _submitted_lock:
        available = max(0, INLINE_MAX_SUBMITTED - len(_submitted_operation_ids))
        for operation_id in ids[:available]:
            if operation_id in _submitted_operation_ids:
                continue
            _submitted_operation_ids.add(operation_id)
            _executor.submit(_run_submitted, app, operation_id)
            submitted += 1
    return submitted


def process_pending_once(app, *, limit: int = 4) -> int:
    bounded = max(1, min(int(limit), 20))
    now = datetime.utcnow()
    with app.app_context():
        ids = [
            row[0]
            for row in db.session.query(MarketplaceMediaOperation.id).filter(
                or_(
                    MarketplaceMediaOperation.status == 'queued',
                    and_(
                        MarketplaceMediaOperation.status.in_([
                            'reconciling', 'uncertain',
                        ]),
                        or_(
                            MarketplaceMediaOperation.next_reconcile_at.is_(None),
                            MarketplaceMediaOperation.next_reconcile_at <= now,
                        ),
                    ),
                )
            ).order_by(
                case(
                    (
                        MarketplaceMediaOperation.status.in_([
                            'reconciling', 'uncertain',
                        ]),
                        0,
                    ),
                    else_=1,
                ),
                MarketplaceMediaOperation.id.asc(),
            ).limit(bounded).all()
        ]
    processed = 0
    for operation_id in ids:
        process_operation(app, operation_id)
        processed += 1
    return processed


def recover_stale_operations(app, *, limit: int = 20) -> int:
    bounded = max(1, min(int(limit), 100))
    cutoff = datetime.utcnow() - timedelta(minutes=STALE_BOUNDARY_MINUTES)
    recovered = 0
    with app.app_context():
        rows = MarketplaceMediaOperation.query.filter(
            MarketplaceMediaOperation.updated_at < cutoff,
            MarketplaceMediaOperation.status.in_(['preflighting', 'submitting']),
        ).order_by(MarketplaceMediaOperation.updated_at.asc()).limit(bounded).all()
        touched_publications = set()
        for operation in rows:
            touched_publications.add(operation.publication_id)
            if operation.status == 'preflighting' and operation.attempt_count == 0:
                operation.status = 'queued'
                operation.error_code = 'preflight_recovered'
                operation.error_message = 'Worker перезапущен до provider write'
            else:
                operation.status = 'uncertain'
                operation.error_code = 'write_boundary_recovered'
                operation.error_message = (
                    'Worker перезапущен после границы отправки; выполняется сверка'
                )
                operation.next_reconcile_at = datetime.utcnow()
            recovered += 1
        db.session.flush()
        for publication_id in touched_publications:
            publication = db.session.get(MarketplaceMediaPublication, publication_id)
            if publication:
                refresh_publication(publication, commit=False)
        db.session.commit()
    return recovered


def create_rollbacks(
    publication: MarketplaceMediaPublication,
    *,
    seller_id: int,
    user_id: int,
    operation_ids: Sequence[int],
    confirm_exact_state: bool,
    channel=None,
) -> List[MarketplaceMediaOperation]:
    """Create separately audited rollback writes after a fresh drift preflight."""
    if publication.seller_id != seller_id:
        raise MarketplaceMediaPublicationError(
            'Публикация не найдена', code='scope_mismatch',
        )
    if not confirm_exact_state:
        raise MarketplaceMediaPublicationError(
            'Подтвердите восстановление исходной галереи',
            code='confirmation_required',
        )
    ids = _strict_ids(list(operation_ids), field='operation_ids')
    originals = publication.operations.filter(
        MarketplaceMediaOperation.id.in_(ids),
        MarketplaceMediaOperation.operation_kind == 'publish',
        MarketplaceMediaOperation.status == 'succeeded',
        MarketplaceMediaOperation.marketplace_code == 'wb',
    ).order_by(MarketplaceMediaOperation.id.asc()).all()
    if {operation.id for operation in originals} != set(ids):
        raise MarketplaceMediaPublicationError(
            'Часть операций нельзя откатить', code='rollback_scope_mismatch',
        )
    channel = channel or get_media_channel_registry().get('wb')
    seller = db.session.get(Seller, seller_id)
    if seller is None or not seller.wb_api_key:
        raise MarketplaceMediaPublicationError(
            'API-ключ WB недоступен', code='wb_credentials_missing',
        )
    credentials = MarketplaceCredentials(
        api_key=seller.wb_api_key,
        external_account_id=str(seller.wb_seller_id or seller.id),
    )
    targets = []
    target_by_operation = {}
    for original in originals:
        existing = original.rollback_operations.filter(
            MarketplaceMediaOperation.status.in_([
                'queued', 'preflighting', 'submitting', 'reconciling',
                'uncertain', 'succeeded',
            ]),
        ).first()
        if existing is not None:
            raise MarketplaceMediaPublicationError(
                f'Откат для {original.external_item_id} уже создан',
                code='rollback_already_exists',
            )
        _seller, _product, _credentials, target = _wb_runtime(original)
        targets.append(target)
        target_by_operation[original.id] = target
    galleries = channel.read_galleries(
        credentials, targets, audit_seller_id=seller_id,
    )

    created = []
    now = datetime.utcnow()
    for original in originals:
        target = target_by_operation[original.id]
        live = galleries.get(target.nm_id)
        confirmed = _json_load(original.confirmed_media_json, [])
        if live is None or not _exact_live_matches(live, confirmed):
            raise MarketplaceMediaPublicationError(
                f'Галерея {original.external_item_id} изменилась после публикации',
                code='rollback_live_drift',
            )
        restore = []
        for position, entry in enumerate(
            _json_load(original.baseline_media_json, []), start=1,
        ):
            restored = dict(entry)
            restored['kind'] = 'restore'
            restored['baseline_position'] = position
            path = _storage_path(restored)
            if path is None or not restored.get('sha256') or not restored.get('visual_hash'):
                raise MarketplaceMediaPublicationError(
                    f'Исходный снимок {original.external_item_id} недоступен',
                    code='rollback_snapshot_missing',
                )
            restore.append(restored)
        if not restore:
            raise MarketplaceMediaPublicationError(
                'Безопасное удаление всей галереи через этот поток не поддерживается',
                code='rollback_empty_gallery_unsupported',
            )
        rollback = MarketplaceMediaOperation(
            publication_id=publication.id,
            rollback_of_operation_id=original.id,
            seller_id=seller_id,
            created_by_user_id=user_id,
            confirmed_by_user_id=user_id,
            infographic_item_id=original.infographic_item_id,
            imported_product_id=original.imported_product_id,
            marketplace_code='wb',
            account_id=None,
            listing_id=None,
            legacy_product_id=original.legacy_product_id,
            external_item_id=original.external_item_id,
            operation_kind='rollback',
            status='queued',
            placement_policy='restore_snapshot',
            target_json=original.target_json,
            source_snapshot_json=_canonical_json({
                'contract_version': CONTRACT_VERSION,
                'rollback_of_operation_id': original.id,
                'confirmed_fingerprint': original.confirmed_fingerprint,
                'restore_fingerprint': original.baseline_fingerprint,
            }),
            baseline_media_json=_canonical_json(confirmed),
            proposed_media_json=_canonical_json(restore),
            dropped_media_json='[]',
            baseline_fingerprint=_gallery_fingerprint(confirmed),
            proposed_fingerprint=_proposal_fingerprint(restore),
            submitted_at=None,
            error_code=None,
            error_message=None,
            created_at=now,
        )
        db.session.add(rollback)
        created.append(rollback)
    try:
        db.session.flush()
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise MarketplaceMediaPublicationError(
            'Для одной из карточек уже идёт media-операция',
            code='target_operation_active',
        ) from exc
    return created
