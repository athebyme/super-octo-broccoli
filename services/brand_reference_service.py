# -*- coding: utf-8 -*-
"""Typed, fail-closed access to the cached WB brand reference."""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from models import (
    Brand,
    BrandAlias,
    BrandCategoryLink,
    Marketplace,
    MarketplaceBrand,
    MarketplaceCategory,
    db,
)
from services.brand_engine import normalize_for_comparison


REFERENCE_MAX_AGE = timedelta(hours=48)
MAX_BATCH_SIZE = 100


def _reference_status(
    source: str,
    synced_at,
    sync_status: str | None = None,
    error: str | None = None,
    *,
    available: bool = True,
    has_data: bool = True,
) -> dict[str, Any]:
    normalized_status = str(sync_status or '').strip().lower()
    if not normalized_status:
        normalized_status = 'success' if synced_at else 'never_synced'

    stale = True
    if synced_at:
        value = synced_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        stale = datetime.now(timezone.utc) - value > REFERENCE_MAX_AGE

    reason = None
    if not available:
        reason = 'upstream_unavailable'
    elif normalized_status != 'success':
        reason = 'sync_not_successful'
    elif stale:
        reason = 'stale_cache'
    elif not has_data:
        reason = 'empty_cache'

    return {
        'source': source,
        'sync_status': normalized_status,
        'synced_at': synced_at.isoformat() if synced_at else None,
        'stale': stale,
        'available': bool(available),
        'usable': reason is None,
        'reason': reason,
        'error': str(error)[:240] if error else None,
        'max_age_hours': int(REFERENCE_MAX_AGE.total_seconds() // 3600),
    }


def _global_brand_status(
    marketplace: Marketplace | None,
    *,
    has_verified_binding: bool,
) -> dict[str, Any]:
    status = _reference_status(
        'wb_brands',
        marketplace.brands_synced_at if marketplace else None,
        marketplace.brands_sync_status if marketplace else None,
        getattr(marketplace, 'brands_sync_error', None) if marketplace else None,
        available=bool(marketplace and marketplace.is_active),
        has_data=bool(
            marketplace
            and (marketplace.brands_version or 0) > 0
            and has_verified_binding
        ),
    )
    status['version'] = int(marketplace.brands_version or 0) if marketplace else 0
    status['scope'] = 'global'
    return status


def _category_status(
    marketplace: Marketplace | None,
    category_id: int | None,
    category: MarketplaceCategory | None,
    global_status: Mapping[str, Any],
    category_verified_at=None,
) -> dict[str, Any]:
    if not category_id:
        status = dict(global_status)
        status.update({
            'usable': False,
            'reason': 'category_scope_required',
            'error': 'category_id is required for WB brand validation',
            'scope': 'category',
            'category_id': category_id,
        })
        return status

    category_catalog = _reference_status(
        'wb_categories',
        marketplace.categories_synced_at if marketplace else None,
        marketplace.categories_sync_status if marketplace else None,
        getattr(marketplace, 'categories_sync_error', None) if marketplace else None,
        available=bool(
            marketplace
            and marketplace.is_active
            and category
            and category.is_enabled
            and category.is_available
            and category.is_leaf
        ),
        has_data=bool(category),
    )
    if not category_catalog['usable']:
        status = dict(category_catalog)
        status.update({
            'source': 'wb_brands',
            'version': int(marketplace.brands_version or 0) if marketplace else 0,
            'scope': 'category',
            'category_id': category_id,
            'category_reference_status': category_catalog,
        })
        return status

    if category_verified_at:
        scoped_status = _reference_status(
            'wb_brands',
            category_verified_at,
            'success',
            None,
            available=bool(marketplace and marketplace.is_active),
            has_data=True,
        )
        status = (
            scoped_status
            if scoped_status['usable'] or not global_status.get('usable')
            else dict(global_status)
        )
    else:
        status = dict(global_status)
    status.update({
        'version': int(marketplace.brands_version or 0) if marketplace else 0,
        'scope': 'category',
        'category_id': category_id,
    })
    return status


def parse_positive_integer(raw_value: Any, field_name: str) -> int:
    """Parse an integer without bool/float truncation or loose coercion."""
    if isinstance(raw_value, bool):
        raise ValueError(f'{field_name} must be a positive integer')
    if isinstance(raw_value, int):
        value = raw_value
    elif isinstance(raw_value, str) and re.fullmatch(
        r'[1-9][0-9]*', raw_value.strip(),
    ):
        value = int(raw_value.strip())
    else:
        raise ValueError(f'{field_name} must be a positive integer')
    if value <= 0:
        raise ValueError(f'{field_name} must be a positive integer')
    return value


def _positive_category_ids(values: Iterable[Any]) -> list[int]:
    category_ids = []
    seen = set()
    for raw_value in values:
        category_id = parse_positive_integer(raw_value, 'category_id')
        if category_id not in seen:
            category_ids.append(category_id)
            seen.add(category_id)
    if len(category_ids) > MAX_BATCH_SIZE:
        raise ValueError(f'Maximum {MAX_BATCH_SIZE} category IDs')
    if not category_ids:
        raise ValueError('category_ids must contain 1..100 entries')
    return category_ids


def _load_reference_context(
    category_ids: list[int], *, include_category_evidence: bool,
) -> dict[str, Any]:
    marketplace = Marketplace.query.filter_by(code='wb').first()
    marketplace_id = marketplace.id if marketplace else -1
    has_verified_binding = bool(db.session.query(
        MarketplaceBrand.id,
    ).join(
        Brand,
        Brand.id == MarketplaceBrand.brand_id,
    ).filter(
        MarketplaceBrand.marketplace_id == marketplace_id,
        MarketplaceBrand.status == 'verified',
        MarketplaceBrand.is_available.is_(True),
        Brand.status == 'verified',
    ).first()) if marketplace else False
    global_status = _global_brand_status(
        marketplace,
        has_verified_binding=has_verified_binding,
    )

    categories = MarketplaceCategory.query.filter(
        MarketplaceCategory.marketplace_id == marketplace_id,
        MarketplaceCategory.subject_id.in_(category_ids),
    ).all() if category_ids else []
    categories_by_id = {int(item.subject_id): item for item in categories}

    # A recent category link is durable evidence for that category while a
    # bounded global sweep is still partial. This query is independent of the
    # number of requested categories.
    verified_rows = []
    if category_ids and include_category_evidence:
        verified_rows = db.session.query(
            BrandCategoryLink.category_id,
            db.func.max(BrandCategoryLink.verified_at),
        ).join(
            MarketplaceBrand,
            MarketplaceBrand.id == BrandCategoryLink.marketplace_brand_id,
        ).join(
            Brand,
            Brand.id == MarketplaceBrand.brand_id,
        ).filter(
            MarketplaceBrand.marketplace_id == marketplace_id,
            MarketplaceBrand.status == 'verified',
            MarketplaceBrand.is_available.is_(True),
            Brand.status == 'verified',
            BrandCategoryLink.category_id.in_(category_ids),
        ).group_by(BrandCategoryLink.category_id).all()
    verified_by_category = {
        int(category_id): verified_at
        for category_id, verified_at in verified_rows
    }

    results = [
        {
            'category_id': category_id,
            'reference_status': _category_status(
                marketplace,
                category_id,
                categories_by_id.get(category_id),
                global_status,
                verified_by_category.get(category_id),
            ),
        }
        for category_id in category_ids
    ]
    return {
        'marketplace': marketplace,
        'marketplace_id': marketplace_id,
        'reference_status': global_status,
        'results': results,
        'count': len(category_ids),
    }


def preflight_brand_categories(category_ids: Iterable[Any]) -> dict[str, Any]:
    """Return freshness/availability for category scopes using bounded SQL."""
    category_ids = _positive_category_ids(category_ids)
    context = _load_reference_context(
        category_ids, include_category_evidence=True,
    )
    return {
        'reference_status': context['reference_status'],
        'results': context['results'],
        'count': context['count'],
    }


def resolve_exact_brand_categories(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Resolve exact brand/category pairs with a constant number of queries.

    Each item must contain a caller-owned unique ``request_id``, a brand, and a
    positive category_id. Fuzzy matches are deliberately outside this write
    safety contract.
    """
    if not isinstance(items, list) or not items or len(items) > MAX_BATCH_SIZE:
        raise ValueError(f'items must contain 1..{MAX_BATCH_SIZE} entries')

    prepared = []
    request_ids = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError('Each item must be an object')
        request_id = item.get('request_id')
        if request_id is None or request_id in request_ids:
            raise ValueError('request_id values must be present and unique')
        request_ids.add(request_id)
        brand_name = str(item.get('brand') or '').strip()
        if len(brand_name) < 2 or len(brand_name) > 200:
            raise ValueError('brand must contain 2..200 chars')
        raw_category_id = item.get('category_id')
        category_id = None
        if raw_category_id not in (None, ''):
            category_id = parse_positive_integer(raw_category_id, 'category_id')
        prepared.append({
            'request_id': request_id,
            'brand': brand_name,
            'normalized': normalize_for_comparison(brand_name),
            'category_id': category_id,
        })

    category_ids = {
        item['category_id'] for item in prepared if item['category_id']
    }
    preflight = _load_reference_context(
        sorted(category_ids), include_category_evidence=False,
    )
    marketplace = preflight['marketplace']
    marketplace_id = preflight['marketplace_id']
    global_status = preflight['reference_status']
    category_statuses = {
        item['category_id']: item['reference_status']
        for item in preflight['results']
    }

    normalized_names = {item['normalized'] for item in prepared}
    alias_rows = db.session.query(
        BrandAlias.alias_normalized,
        Brand.id,
        Brand.name,
    ).join(
        Brand,
        Brand.id == BrandAlias.brand_id,
    ).filter(
        BrandAlias.alias_normalized.in_(normalized_names),
        BrandAlias.is_active.is_(True),
        Brand.status == 'verified',
    ).all()
    brands_by_name = {
        alias_normalized: {'id': brand_id, 'name': brand_name}
        for alias_normalized, brand_id, brand_name in alias_rows
    }
    brand_ids = {item['id'] for item in brands_by_name.values()}
    bindings = MarketplaceBrand.query.filter(
        MarketplaceBrand.marketplace_id == marketplace_id,
        MarketplaceBrand.brand_id.in_(brand_ids),
        MarketplaceBrand.status == 'verified',
        MarketplaceBrand.is_available.is_(True),
    ).all() if brand_ids else []
    bindings_by_brand_id = {binding.brand_id: binding for binding in bindings}
    binding_ids = {binding.id for binding in bindings}
    links = BrandCategoryLink.query.filter(
        BrandCategoryLink.marketplace_brand_id.in_(binding_ids),
        BrandCategoryLink.category_id.in_(category_ids),
    ).all() if binding_ids and category_ids else []
    links_by_key = {
        (link.marketplace_brand_id, int(link.category_id)): link
        for link in links
    }

    results = []
    for item in prepared:
        category_id = item['category_id']
        brand = brands_by_name.get(item['normalized'])
        binding = bindings_by_brand_id.get(brand['id']) if brand else None
        link = links_by_key.get((binding.id, category_id)) if (
            binding and category_id
        ) else None
        reference_status = dict(
            category_statuses.get(category_id) or global_status
        )
        if not category_id:
            reference_status.update({
                'usable': False,
                'reason': 'category_scope_required',
                'error': 'category_id is required for WB brand validation',
                'scope': 'category',
                'category_id': None,
            })
        elif link:
            # Exact pair evidence is stronger than a stale/partial global sweep,
            # but never stronger than an unavailable/stale category catalog.
            category_reference = reference_status.get('category_reference_status')
            if not category_reference or category_reference.get('usable'):
                pair_status = _reference_status(
                    'wb_brands',
                    link.verified_at,
                    'success',
                    None,
                    available=bool(marketplace and marketplace.is_active),
                    has_data=True,
                )
                pair_status.update({
                    'version': int(marketplace.brands_version or 0)
                    if marketplace else 0,
                    'scope': 'category',
                    'category_id': category_id,
                })
                reference_status = pair_status

        result = {
            'request_id': item['request_id'],
            'status': 'unavailable',
            'brand_name': None,
            'confidence': 0.0,
            'suggestions': [],
            'reference_status': reference_status,
        }
        if reference_status.get('usable'):
            if brand and binding:
                result.update({
                    'status': 'found',
                    'brand_name': brand['name'],
                    'marketplace_brand_name': binding.marketplace_brand_name,
                    'marketplace_brand_id': (
                        link.marketplace_external_brand_id if link else None
                    ),
                    'confidence': 1.0,
                    'category_available': link.is_available if link else None,
                })
            else:
                result['status'] = 'not_found'
        results.append(result)
    return results
