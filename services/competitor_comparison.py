# -*- coding: utf-8 -*-
"""Seller-scoped cross-competitor comparison by canonical supplier identity.

The service is deliberately read-only.  It never guesses identity itself and
never calls WB, an image host or an LLM.  It consumes completed global
``CompetitorProductMatch`` rows, applies the current seller's local review,
then groups exact matches by ``SupplierProduct.id``.  Seller prices are joined
only through the audited SupplierProduct -> ImportedProduct -> Product chain.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any

from sqlalchemy import func

from models import (
    db,
    CompetitorGroup,
    CompetitorProduct,
    CompetitorProductMatch,
    ImportedProduct,
    Product,
    SellerCompetitorMatchReview,
    SupplierProduct,
)
from services.competitor_matching import (
    shared_exact_match_is_admissible,
    supplier_identity_card,
)


MAX_COMPARE_GROUPS = 100
MAX_COMPARE_OFFERS = 1000
MAX_COMPARE_PAGE_SIZE = 50
DEFAULT_COMPARE_PAGE_SIZE = 24
MAX_COMPARE_QUERY_LENGTH = 120
VALID_SCOPES = {'all', 'linked', 'missing'}
VALID_SORTS = {'coverage', 'gap', 'price', 'name'}


class CompetitorComparisonError(ValueError):
    """Safe validation/not-found error for the comparison read model."""


def _price(value: Any) -> int | float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number.is_integer():
        return int(number)
    return round(number, 2)


def _freshness_key(product: CompetitorProduct) -> tuple[datetime, int]:
    observed_at = (
        product.last_price_at
        or product.last_fetched_at
        or product.updated_at
        or product.created_at
        or datetime.min
    )
    return observed_at, int(product.id or 0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _group_card(group: CompetitorGroup, products_count: int) -> dict[str, Any]:
    wb_supplier_id = None
    if group.auto_source == 'seller' and group.auto_source_value:
        try:
            candidate = int(group.auto_source_value)
            wb_supplier_id = candidate if candidate > 0 else None
        except (TypeError, ValueError):
            wb_supplier_id = None
    return {
        'id': group.id,
        'name': group.name,
        'color': group.color or '#3B82F6',
        'wb_supplier_id': wb_supplier_id,
        'products_count': int(products_count or 0),
    }


def _own_products_by_supplier(
    seller_id: int,
    supplier_ids: set[int],
) -> dict[int, Product]:
    """Resolve one seller-owned Product per supplier id by exact FK only."""
    if not supplier_ids:
        return {}
    imported_rows = (
        ImportedProduct.query
        .filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.supplier_product_id.in_(supplier_ids),
            ImportedProduct.product_id.isnot(None),
        )
        .order_by(ImportedProduct.id.desc())
        .all()
    )
    product_ids = {
        int(row.product_id) for row in imported_rows if row.product_id
    }
    products = {
        row.id: row for row in Product.query.filter(
            Product.seller_id == seller_id,
            Product.id.in_(product_ids),
        ).all()
    } if product_ids else {}
    result: dict[int, Product] = {}
    for imported in imported_rows:
        supplier_id = imported.supplier_product_id
        own = products.get(imported.product_id)
        if supplier_id and own and supplier_id not in result:
            result[int(supplier_id)] = own
    return result


def _own_card(product: Product | None) -> dict[str, Any] | None:
    if not product:
        return None
    public_base = _price(product.wb_public_base_price)
    public_final = _price(product.wb_public_final_price)
    seller_base = _price(product.price)
    seller_discount = _price(product.discount_price)
    return {
        'id': product.id,
        'nm_id': product.nm_id,
        # Seller title is display-only.  It never participates in identity.
        'title': (product.title or '')[:500],
        # Compatibility/display fields from the seller account. In
        # particular ``discount_price`` is not called a buyer price.
        'price': seller_base,
        'discount_price': seller_discount,
        'seller_base_price': seller_base,
        'seller_discount_price': seller_discount,
        # Base price has the same semantics in seller/public APIs, so the
        # seller value is a safe fallback while the first public observation
        # is pending. Final price has no fallback: seller discountedPrice
        # excludes WB-funded storefront discounts.
        'base_price': public_base or seller_base,
        'final_price': public_final,
        'effective_price': public_final,
        'base_price_source': (
            'wb_public_card' if public_base is not None
            else 'seller_api_fallback' if seller_base is not None
            else None
        ),
        'final_price_source': (
            'wb_public_card' if public_final is not None else None
        ),
        'public_base_price': public_base,
        'public_final_price': public_final,
        'public_price_synced_at': _iso(product.wb_public_price_synced_at),
        'public_price_miss_count': int(
            product.wb_public_price_miss_count or 0),
        'quantity': product.quantity,
        'is_active': bool(product.is_active),
        'last_sync': _iso(product.last_sync),
        'identity_source': 'exact_imported_product_fk',
        'display_content_source': 'seller_product',
    }


def _offer_card(
    product: CompetitorProduct,
    group: CompetitorGroup,
    match: CompetitorProductMatch,
    *,
    effective_source: str,
    own_base_price: int | float | None,
    own_final_price: int | float | None,
) -> dict[str, Any]:
    base_price = _price(product.current_price)
    final_price = _price(product.current_sale_price)

    def delta_to_own(
        competitor_price: int | float | None,
        own_price: int | float | None,
    ) -> float | None:
        if competitor_price is None or own_price is None:
            return None
        return round(
            (float(competitor_price) - float(own_price))
            / float(own_price) * 100,
            1,
        )

    base_delta = delta_to_own(base_price, own_base_price)
    final_delta = delta_to_own(final_price, own_final_price)
    wb_supplier_id = product.wb_supplier_id
    if not wb_supplier_id and group.auto_source == 'seller':
        try:
            candidate = int(group.auto_source_value or 0)
            wb_supplier_id = candidate if candidate > 0 else None
        except (TypeError, ValueError):
            wb_supplier_id = None
    return {
        'id': product.id,
        'nm_id': product.nm_id,
        'title': (product.title or '')[:500],
        'brand': (product.brand or '')[:200],
        'image_url': product.image_url,
        'price': base_price,
        'sale_price': final_price,
        'base_price': base_price,
        'final_price': final_price,
        # Compatibility aliases remain the storefront/final lane.
        'effective_price': final_price,
        'delta_to_own_percent': final_delta,
        'base_delta_to_own_percent': base_delta,
        'final_delta_to_own_percent': final_delta,
        'rating': product.current_rating,
        'feedbacks_count': product.current_feedbacks_count,
        'total_stock': product.current_total_stock,
        'last_price_at': _iso(product.last_price_at),
        'last_fetched_at': _iso(product.last_fetched_at),
        'price_miss_count': int(product.price_miss_count or 0),
        'competitor': {
            'group_id': group.id,
            'group_name': group.name,
            'color': group.color or '#3B82F6',
            'wb_supplier_id': wb_supplier_id,
            'supplier_name': (product.supplier_name or '')[:200],
        },
        'match': {
            'id': match.id,
            'score': int(match.final_score or 0),
            'source': effective_source,
            'reviewed': effective_source == 'seller_confirmed',
            'scope': 'exact_same_only',
        },
    }


def _matches_search(row: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    values = []
    supplier = row.get('supplier') or {}
    own = row.get('own_product') or {}
    values.extend([
        supplier.get('title'), supplier.get('brand'),
        supplier.get('vendor_code'), supplier.get('external_id'),
        own.get('title'), own.get('nm_id'),
    ])
    for offer in row.get('offers') or []:
        competitor = offer.get('competitor') or {}
        values.extend([
            offer.get('title'), offer.get('brand'), offer.get('nm_id'),
            competitor.get('group_name'), competitor.get('supplier_name'),
            competitor.get('wb_supplier_id'),
        ])
    haystack = ' '.join(
        str(value) for value in values if value not in (None, '')
    ).casefold()
    return query in haystack


def _price_lane_metrics(
    offers: list[dict[str, Any]],
    *,
    offer_key: str,
    own_price: int | float | None,
) -> dict[str, Any]:
    prices = [
        float(offer[offer_key]) for offer in offers
        if offer.get(offer_key) is not None
    ]
    minimum = min(prices) if prices else None
    maximum = max(prices) if prices else None
    middle = median(prices) if prices else None
    own_position = None
    own_vs_min = None
    if own_price is not None and prices:
        own_position = 1 + sum(price < float(own_price) for price in prices)
        own_vs_min = round(
            (float(own_price) - minimum) / minimum * 100,
            1,
        )
    return {
        'own_price': _price(own_price),
        'priced_offer_count': len(prices),
        'min_competitor_price': _price(minimum),
        'median_competitor_price': _price(middle),
        'max_competitor_price': _price(maximum),
        'own_position': own_position,
        'total_with_own': (
            len(prices) + 1 if own_price is not None and prices else None
        ),
        'own_vs_min_percent': own_vs_min,
        'competitor_spread_percent': (
            round((maximum - minimum) / minimum * 100, 1)
            if minimum is not None and maximum is not None else None
        ),
    }


def _sort_rows(rows: list[dict[str, Any]], sort: str) -> None:
    title = lambda row: ((row.get('supplier') or {}).get('title') or '').casefold()
    lane = lambda row: row['metrics']['price_lanes']['final']
    if sort == 'name':
        rows.sort(key=lambda row: (title(row), row['supplier']['id']))
    elif sort == 'price':
        rows.sort(key=lambda row: (
            lane(row)['min_competitor_price'] is None,
            lane(row)['min_competitor_price'] or 0,
            title(row),
        ))
    elif sort == 'gap':
        rows.sort(key=lambda row: (
            lane(row)['own_vs_min_percent'] is None,
            -(lane(row)['own_vs_min_percent'] or 0),
            title(row),
        ))
    else:
        rows.sort(key=lambda row: (
            row.get('own_product') is None,
            -row['metrics']['competitor_count'],
            -row['metrics']['offer_count'],
            title(row),
        ))


def build_competitor_comparison(
    seller_id: int,
    *,
    group_id: int | None = None,
    query: str = '',
    scope: str = 'all',
    sort: str = 'coverage',
    page: int = 1,
    per_page: int = DEFAULT_COMPARE_PAGE_SIZE,
) -> dict[str, Any]:
    """Build a bounded all-vs-us or one-competitor-vs-us comparison."""
    if isinstance(group_id, bool) or (
        group_id is not None and (not isinstance(group_id, int) or group_id <= 0)
    ):
        raise CompetitorComparisonError('Некорректный конкурент')
    if scope not in VALID_SCOPES:
        raise CompetitorComparisonError('Некорректный фильтр карточек')
    if sort not in VALID_SORTS:
        raise CompetitorComparisonError('Некорректная сортировка')
    if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
        raise CompetitorComparisonError('Некорректная страница')
    if (
        isinstance(per_page, bool) or not isinstance(per_page, int)
        or per_page <= 0 or per_page > MAX_COMPARE_PAGE_SIZE
    ):
        raise CompetitorComparisonError(
            f'На странице может быть от 1 до {MAX_COMPARE_PAGE_SIZE} товаров',
        )
    query = (query or '').strip()
    if len(query) > MAX_COMPARE_QUERY_LENGTH:
        raise CompetitorComparisonError(
            f'Поиск — не больше {MAX_COMPARE_QUERY_LENGTH} символов',
        )
    normalized_query = query.casefold()

    groups = (
        CompetitorGroup.query
        .filter_by(seller_id=seller_id, is_active=True)
        .order_by(CompetitorGroup.name.asc(), CompetitorGroup.id.asc())
        .limit(MAX_COMPARE_GROUPS + 1)
        .all()
    )
    groups_truncated = len(groups) > MAX_COMPARE_GROUPS
    groups = groups[:MAX_COMPARE_GROUPS]
    selected_group = None
    if group_id is not None:
        selected_group = CompetitorGroup.query.filter_by(
            id=group_id, seller_id=seller_id, is_active=True,
        ).first()
        if not selected_group:
            raise CompetitorComparisonError('Конкурент не найден')
        if selected_group.id not in {group.id for group in groups}:
            groups.append(selected_group)

    group_counts = dict(db.session.query(
        CompetitorProduct.group_id,
        func.count(CompetitorProduct.id),
    ).join(
        CompetitorGroup, CompetitorGroup.id == CompetitorProduct.group_id,
    ).filter(
        CompetitorProduct.seller_id == seller_id,
        CompetitorProduct.is_active.is_(True),
        CompetitorGroup.seller_id == seller_id,
        CompetitorGroup.is_active.is_(True),
    ).group_by(CompetitorProduct.group_id).all())
    group_cards = [
        _group_card(group, group_counts.get(group.id, 0)) for group in groups
    ]

    product_query = CompetitorProduct.query.join(
        CompetitorGroup, CompetitorGroup.id == CompetitorProduct.group_id,
    ).filter(
        CompetitorProduct.seller_id == seller_id,
        CompetitorProduct.is_active.is_(True),
        CompetitorGroup.seller_id == seller_id,
        CompetitorGroup.is_active.is_(True),
    )
    if selected_group:
        product_query = product_query.filter(
            CompetitorProduct.group_id == selected_group.id,
        )
    else:
        product_query = product_query.filter(
            CompetitorProduct.group_id.in_([group.id for group in groups]),
        )
    products = product_query.order_by(
        CompetitorProduct.id.asc(),
    ).limit(MAX_COMPARE_OFFERS + 1).all()
    offers_truncated = len(products) > MAX_COMPARE_OFFERS
    products = products[:MAX_COMPARE_OFFERS]

    # A WB nmID is one offer even if it was accidentally added to two groups.
    # Keep the freshest seller-scoped observation to avoid double-counting.
    product_by_nm: dict[int, CompetitorProduct] = {}
    for product in products:
        nm_id = int(product.nm_id)
        current = product_by_nm.get(nm_id)
        if current is None or _freshness_key(product) > _freshness_key(current):
            product_by_nm[nm_id] = product
    products = list(product_by_nm.values())

    nm_ids = list(product_by_nm)
    matches = {
        int(row.nm_id): row for row in CompetitorProductMatch.query.filter(
            CompetitorProductMatch.nm_id.in_(nm_ids),
        ).all()
    } if nm_ids else {}
    match_ids = [row.id for row in matches.values()]
    reviews = {
        row.match_id: row for row in SellerCompetitorMatchReview.query.filter(
            SellerCompetitorMatchReview.seller_id == seller_id,
            SellerCompetitorMatchReview.match_id.in_(match_ids),
        ).all()
    } if match_ids else {}

    effective_rows: list[
        tuple[CompetitorProduct, CompetitorGroup, CompetitorProductMatch, int, str]
    ] = []
    unmatched_count = 0
    non_exact_count = 0
    for product in products:
        match = matches.get(int(product.nm_id))
        if not match or match.processing_status != 'completed':
            unmatched_count += 1
            continue
        review = reviews.get(match.id)
        if review:
            if (
                review.status != 'confirmed'
                or review.match_type != 'same'
                or not review.supplier_product_id
            ):
                non_exact_count += 1
                continue
            supplier_id = int(review.supplier_product_id)
            effective_source = 'seller_confirmed'
        elif shared_exact_match_is_admissible(match):
            supplier_id = int(match.suggested_supplier_product_id)
            effective_source = 'shared_suggestion'
        else:
            non_exact_count += 1
            continue
        group = next(
            (candidate for candidate in groups if candidate.id == product.group_id),
            None,
        )
        # For an all-groups query every active group is in the bounded group
        # list under normal limits.  Selected group is explicitly appended.
        if not group:
            continue
        effective_rows.append(
            (product, group, match, supplier_id, effective_source),
        )

    supplier_ids = {row[3] for row in effective_rows}
    suppliers = {
        row.id: row for row in SupplierProduct.query.filter(
            SupplierProduct.id.in_(supplier_ids),
        ).all()
    } if supplier_ids else {}
    own_products = _own_products_by_supplier(seller_id, supplier_ids)

    row_metadata: dict[int, list[tuple[CompetitorProduct, CompetitorGroup,
                                      CompetitorProductMatch, str]]] = defaultdict(list)
    for product, group, match, supplier_id, effective_source in effective_rows:
        if supplier_id not in suppliers:
            continue
        row_metadata[supplier_id].append(
            (product, group, match, effective_source),
        )

    rows: list[dict[str, Any]] = []
    for supplier_id, metadata in row_metadata.items():
        supplier = suppliers.get(supplier_id)
        supplier_card = supplier_identity_card(supplier)
        if not supplier_card:
            continue
        own = own_products.get(supplier_id)
        own_card = _own_card(own)
        own_base_price = own_card['base_price'] if own_card else None
        own_final_price = own_card['final_price'] if own_card else None
        offers = [
            _offer_card(
                product, group, match,
                effective_source=effective_source,
                own_base_price=own_base_price,
                own_final_price=own_final_price,
            )
            for product, group, match, effective_source in metadata
        ]
        offers.sort(key=lambda offer: (
            offer['final_price'] is None,
            offer['final_price'] or 0,
            offer['base_price'] is None,
            offer['base_price'] or 0,
            (offer['competitor']['group_name'] or '').casefold(),
            offer['nm_id'],
        ))
        base_lane = _price_lane_metrics(
            offers, offer_key='base_price', own_price=own_base_price,
        )
        final_lane = _price_lane_metrics(
            offers, offer_key='final_price', own_price=own_final_price,
        )
        competitor_keys = {
            ('wb', offer['competitor']['wb_supplier_id'])
            if offer['competitor']['wb_supplier_id']
            else ('group', offer['competitor']['group_id'])
            for offer in offers
        }
        rows.append({
            'supplier': supplier_card,
            'own_product': own_card,
            'offers': offers,
            'metrics': {
                'offer_count': len(offers),
                'competitor_count': len(competitor_keys),
                'price_lanes': {
                    'base': base_lane,
                    'final': final_lane,
                },
                # Compatibility aliases intentionally mean the final
                # storefront lane; new clients should use price_lanes.
                **final_lane,
            },
        })

    all_competitor_keys = {
        ('wb', offer['competitor']['wb_supplier_id'])
        if offer['competitor']['wb_supplier_id']
        else ('group', offer['competitor']['group_id'])
        for row in rows for offer in row['offers']
    }
    undercut_base = sum(
        1 for row in rows
        if row['metrics']['price_lanes']['base']['own_vs_min_percent'] is not None
        and row['metrics']['price_lanes']['base']['own_vs_min_percent'] > 0
    )
    undercut_final = sum(
        1 for row in rows
        if row['metrics']['price_lanes']['final']['own_vs_min_percent'] is not None
        and row['metrics']['price_lanes']['final']['own_vs_min_percent'] > 0
    )
    ours_lowest_final = sum(
        1 for row in rows
        if row['metrics']['price_lanes']['final']['own_vs_min_percent'] is not None
        and row['metrics']['price_lanes']['final']['own_vs_min_percent'] <= 0
    )
    summary = {
        'identities': len(rows),
        'with_own': sum(1 for row in rows if row['own_product']),
        'without_own': sum(1 for row in rows if not row['own_product']),
        'offers': sum(row['metrics']['offer_count'] for row in rows),
        'competitors': len(all_competitor_keys),
        'undercut': undercut_final,
        'undercut_base': undercut_base,
        'undercut_final': undercut_final,
        'ours_lowest_or_equal': ours_lowest_final,
        'ours_lowest_or_equal_final': ours_lowest_final,
        'awaiting_match': unmatched_count,
        'excluded_non_exact': non_exact_count,
    }

    filtered = [row for row in rows if _matches_search(row, normalized_query)]
    if scope == 'linked':
        filtered = [row for row in filtered if row['own_product']]
    elif scope == 'missing':
        filtered = [row for row in filtered if not row['own_product']]
    _sort_rows(filtered, sort)

    total = len(filtered)
    pages = max(1, math.ceil(total / per_page))
    page = min(page, pages)
    start = (page - 1) * per_page
    items = filtered[start:start + per_page]

    return {
        'items': items,
        'summary': summary,
        'groups': group_cards,
        'filters': {
            'group_id': selected_group.id if selected_group else None,
            'query': query,
            'scope': scope,
            'sort': sort,
        },
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'pages': pages,
            'has_previous': page > 1,
            'has_next': page < pages,
        },
        'bounds': {
            'offers_limit': MAX_COMPARE_OFFERS,
            'offers_truncated': offers_truncated,
            'groups_limit': MAX_COMPARE_GROUPS,
            'groups_truncated': groups_truncated,
        },
        'identity_scope': 'exact_same_only',
        'identity_source': 'supplier_observed_only',
        'own_link_source': 'exact_imported_product_fk',
        'price_contract': {
            'base': 'wb_public_basic',
            'final': 'wb_public_total_or_product',
            'own_final_requires_public_observation': True,
            'personal_discounts_excluded': True,
        },
        'generated_at': datetime.utcnow().isoformat(),
    }
