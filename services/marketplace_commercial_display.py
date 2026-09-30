"""Local Ozon commercial presentation. Display facts never authorize a write."""

from sqlalchemy.orm import joinedload

from models import MarketplaceWarehouse, MarketplaceWarehouseStock
from services.marketplace_commercial import MarketplaceCommercialService


def product_summary(listing):
    if listing is None:
        return None
    image = listing.primary_image_url()
    return {
        "id": listing.id, "title": listing.title or listing.offer_id,
        "offer_id": listing.offer_id, "account_id": listing.account_id,
        "primary_image": image if isinstance(image, str) and image.startswith("https://") else None,
    }


def commercial_context(*, seller_id, listing_id):
    """Bounded local facts for the form; proposal creation observes Ozon again."""
    listing = MarketplaceCommercialService._owned_listing(
        seller_id=seller_id, listing_id=listing_id,
    )
    warehouses = MarketplaceWarehouse.query.filter_by(
        seller_id=seller_id, account_id=listing.account_id,
        marketplace_id=listing.marketplace_id, is_available=True,
    ).order_by(MarketplaceWarehouse.name, MarketplaceWarehouse.id).limit(101).all()
    ids = [row.id for row in warehouses[:100]]
    stocks = MarketplaceWarehouseStock.query.options(
        joinedload(MarketplaceWarehouseStock.warehouse),
    ).filter_by(
        seller_id=seller_id, listing_id=listing.id, account_id=listing.account_id,
        marketplace_id=listing.marketplace_id, offer_id=listing.offer_id,
        external_product_id=listing.external_product_id, is_available=True,
    ).filter(MarketplaceWarehouseStock.warehouse_id.in_(ids)).limit(100).all()
    price = listing._json_value(listing.price_summary_json, {})
    values = price.get("values") if price.get("available") is not False else None
    return {
        "product": product_summary(listing),
        "account_label": listing.account.label,
        # Ozon seller base price. Never substitute marketing/buyer prices here.
        "price": values if isinstance(values, dict) else {},
        "currency": price.get("currency"),
        "prices_synced_at": listing.prices_synced_at.isoformat() if listing.prices_synced_at else None,
        "warehouses": [row.to_public_dict() for row in warehouses[:100]],
        "warehouses_truncated": len(warehouses) > 100,
        "stocks": [row.to_public_dict() for row in stocks],
    }
