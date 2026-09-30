"""Local lifecycle rules for legacy WB projections; never marketplace writes."""

from sqlalchemy import select, update

from models import Product, db


def deactivate_unconfirmed_products(seller_id: int, *, limit: int = 500) -> int:
    """Hide invalid nmID placeholders while preserving every FK and history.

    Failed legacy imports may have audit records and canonical links attached.
    Deleting their Product rows is neither a safe cleanup nor proof of a WB
    deletion. The caller owns commit/rollback of this bounded local update.
    """
    if isinstance(seller_id, bool) or not isinstance(seller_id, int) or seller_id <= 0:
        raise ValueError('seller_id must be a positive integer')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError('limit must be between 1 and 500')
    targets = select(Product.id).where(
        Product.seller_id == seller_id,
        Product.nm_id <= 0,
        Product.is_active.is_(True),
    ).order_by(Product.id).limit(limit)
    result = db.session.execute(
        update(Product).where(
            Product.seller_id == seller_id,
            Product.id.in_(targets),
        ).values(is_active=False).execution_options(synchronize_session=False)
    )
    return result.rowcount
