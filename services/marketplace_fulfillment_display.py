"""Bounded local presentation of authorized Ozon fulfillment observations.

The caller owns seller/account authorization. No provider, image or LLM I/O.
Listing previews require the exact already stored FK in the same account.
"""

from sqlalchemy import func
from urllib.parse import urlsplit

from models import (
    MarketplaceListing, MarketplacePosting, MarketplacePostingItem, MarketplacePostingStatusEvent,
    MarketplaceFulfillmentSync, db,
)


def listing_previews(documents, *, account):
    ids = {row.get('listing_id') for row in documents if type(row.get('listing_id')) is int}
    listings = MarketplaceListing.query.filter(
        MarketplaceListing.id.in_(ids),
        MarketplaceListing.seller_id == account.seller_id,
        MarketplaceListing.marketplace_id == account.marketplace_id,
        MarketplaceListing.account_id == account.id,
    ).all() if ids else []
    previews = {}
    for row in listings:
        # Only Ozon media from this exact listing; never legacy WB fallback.
        media = row._json_value(row.media_json, {})
        media = media if isinstance(media, dict) else {}
        image = media.get('primary_image')
        if not image:
            images = media.get('images')
            image = next((s for s in images if isinstance(s, str)), None) if isinstance(images, list) else None
        try:
            url = urlsplit(image) if isinstance(image, str) and len(image) <= 2000 else None
            valid = bool(url and url.scheme in {'http', 'https'} and url.hostname and not url.username and not url.password)
        except ValueError:
            valid = False
        previews[row.id] = {
            'id': row.id, 'title': row.title, 'offer_id': row.offer_id,
            'image': image if valid else None,
            'url': f'/marketplaces/listings/view/{row.id}?account_id={account.id}',
        }
    for row in documents:
        row['listing'] = previews.get(row.get('listing_id'))
        if row['listing'] is None:
            row['listing_id'] = None


def posting_previews(postings, *, account):
    """At most three product lines per posting, using a fixed query count."""
    ids = [p.id for p in postings]
    documents = {p.id: p.to_public_dict() for p in postings}
    if not ids:
        return []
    scope = (
        MarketplacePostingItem.seller_id == account.seller_id,
        MarketplacePostingItem.account_id == account.id,
        MarketplacePostingItem.posting_id.in_(ids),
    )
    counts = db.session.query(
        MarketplacePostingItem.posting_id, func.count(MarketplacePostingItem.id),
        func.sum(MarketplacePostingItem.quantity),
    ).filter(*scope).group_by(MarketplacePostingItem.posting_id).all()
    for row in documents.values():
        row.update(items=[], item_count=0, quantity=0)
    for posting_id, count, quantity in counts:
        documents[posting_id].update(item_count=count, quantity=quantity)
    ranked = db.session.query(
        MarketplacePostingItem.id.label('item_id'),
        func.row_number().over(
            partition_by=MarketplacePostingItem.posting_id,
            order_by=MarketplacePostingItem.id,
        ).label('rank'),
    ).filter(*scope).subquery()
    rows = MarketplacePostingItem.query.join(
        ranked, ranked.c.item_id == MarketplacePostingItem.id,
    ).filter(ranked.c.rank <= 3).order_by(MarketplacePostingItem.id).all()
    lines = []
    for row in rows:
        line = row.to_public_dict()
        lines.append(line)
        documents[row.posting_id]['items'].append(line)
    listing_previews(lines, account=account)
    for row in documents.values():
        row['items_truncated'] = row['item_count'] > len(row['items'])
    return list(documents.values())


def posting_detail(posting, *, account, page=1, per_page=50):
    """All lines remain reachable via bounded pages; history is observed only."""
    data = posting.to_public_dict()
    result = MarketplacePostingItem.query.filter_by(
        posting_id=posting.id, seller_id=account.seller_id, account_id=account.id,
    ).order_by(MarketplacePostingItem.id).paginate(page=page, per_page=per_page, error_out=False)
    data['items'] = [row.to_public_dict() for row in result.items]
    listing_previews(data['items'], account=account)
    data['item_pagination'] = {'page': page, 'per_page': per_page, 'pages': result.pages, 'total': result.total}
    events = MarketplacePostingStatusEvent.query.filter_by(
        posting_id=posting.id, seller_id=account.seller_id, account_id=account.id,
    ).order_by(MarketplacePostingStatusEvent.observed_at.desc(), MarketplacePostingStatusEvent.id.desc()).limit(101).all()
    data['status_history'] = [row.to_public_dict() for row in events[:100]]
    data['history_truncated'] = len(events) > 100
    return data


def sync_context(*, account, period_code):
    query = MarketplaceFulfillmentSync.query.filter_by(
        seller_id=account.seller_id, marketplace_id=account.marketplace_id,
        account_id=account.id, period_code=period_code,
    )
    completed = query.filter_by(status='completed').order_by(
        MarketplaceFulfillmentSync.completed_at.desc(), MarketplaceFulfillmentSync.id.desc(),
    ).first()
    latest = query.order_by(MarketplaceFulfillmentSync.id.desc()).first()
    return {
        'last_completed_sync': completed.to_public_dict() if completed else None,
        'sync': latest.to_public_dict() if latest else None,
    }


def related_postings(documents, rows, *, account):
    """An old order can open from a recent return even outside list date filters."""
    ids = {row.posting_id for row in rows if row.posting_id is not None}
    owned = {row.id for row in MarketplacePosting.query.with_entities(MarketplacePosting.id).filter(
        MarketplacePosting.id.in_(ids), MarketplacePosting.seller_id == account.seller_id,
        MarketplacePosting.account_id == account.id, MarketplacePosting.marketplace_id == account.marketplace_id,
    ).all()} if ids else set()
    for document, row in zip(documents, rows):
        document['posting_url'] = (
            f'/marketplaces/orders?account_id={account.id}&posting_id={row.posting_id}'
            if row.posting_id in owned else None
        )
