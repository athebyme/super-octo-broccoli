"""Exact seller/listing fences for durable Ozon catalog operations.

Commercial price and stock operations intentionally do not participate. This
module performs local reads only; callers retain their existing account locks
and decide whether a found operation blocks their workflow.
"""

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from models import (
    Marketplace,
    MarketplaceListing,
    MarketplaceOperation,
    SellerMarketplaceAccount,
)


CATALOG_OPERATION_KINDS = frozenset({
    "product_import",
    "product_import_rollback",
    "product_update",
    "product_update_rollback",
})
ACTIVE_CATALOG_STATUSES = frozenset({
    "queued",
    "submitting",
    "submitted",
    "polling",
    "uncertain",
})


def _exact_positive_int(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def _scoped_listing_query(*, seller_id, marketplace_id, account_id,
                          listing_id):
    if not all(_exact_positive_int(value) for value in (
        seller_id, marketplace_id, account_id, listing_id,
    )):
        return None
    return MarketplaceOperation.query.options(
        joinedload(MarketplaceOperation.marketplace),
        joinedload(MarketplaceOperation.account),
    ).join(
        Marketplace,
        Marketplace.id == MarketplaceOperation.marketplace_id,
    ).join(
        SellerMarketplaceAccount,
        SellerMarketplaceAccount.id == MarketplaceOperation.account_id,
    ).join(
        MarketplaceListing,
        MarketplaceListing.id == MarketplaceOperation.listing_id,
    ).filter(
        MarketplaceOperation.seller_id == seller_id,
        MarketplaceOperation.marketplace_id == marketplace_id,
        MarketplaceOperation.account_id == account_id,
        MarketplaceOperation.listing_id == listing_id,
        MarketplaceOperation.operation_kind.in_(CATALOG_OPERATION_KINDS),
        MarketplaceOperation.status.in_(ACTIVE_CATALOG_STATUSES),
        Marketplace.code == "ozon",
        SellerMarketplaceAccount.seller_id == seller_id,
        SellerMarketplaceAccount.marketplace_id == marketplace_id,
        MarketplaceListing.seller_id == seller_id,
        MarketplaceListing.marketplace_id == marketplace_id,
        MarketplaceListing.account_id == account_id,
    )


def active_catalog_listing_operation(*, seller_id, marketplace_id,
                                     account_id, listing_id,
                                     exclude_operation_id=None):
    """Return the oldest exact-tenant active catalog operation for a listing."""
    query = _scoped_listing_query(
        seller_id=seller_id,
        marketplace_id=marketplace_id,
        account_id=account_id,
        listing_id=listing_id,
    )
    if query is None:
        return None
    if exclude_operation_id is not None:
        if not _exact_positive_int(exclude_operation_id):
            return None
        query = query.filter(MarketplaceOperation.id != exclude_operation_id)
    return query.order_by(MarketplaceOperation.id.asc()).first()


def queued_catalog_listing_blocker(operation):
    """Find an operation that must stop this queued write before any provider I/O.

    An older, never-attempted queued operation wins a same-listing queue tie.
    Newer queued operations do not block it, avoiding mutual deadlock. Any
    attempted or otherwise in-flight/uncertain operation blocks immediately.
    """
    if (
        operation is None
        or operation.operation_kind not in CATALOG_OPERATION_KINDS
        or operation.status != "queued"
        or not _exact_positive_int(operation.id)
        or not _exact_positive_int(operation.listing_id)
    ):
        return None
    query = _scoped_listing_query(
        seller_id=operation.seller_id,
        marketplace_id=operation.marketplace_id,
        account_id=operation.account_id,
        listing_id=operation.listing_id,
    )
    if query is None:
        return None
    return query.filter(
        MarketplaceOperation.id != operation.id,
        or_(
            MarketplaceOperation.status != "queued",
            MarketplaceOperation.attempt_count > 0,
            MarketplaceOperation.id < operation.id,
        ),
    ).order_by(MarketplaceOperation.id.asc()).first()
