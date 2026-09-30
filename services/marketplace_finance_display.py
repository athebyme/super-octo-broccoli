"""Bounded local finance presentation; no provider or image I/O."""
from sqlalchemy import func

from models import MarketplaceFinanceFactItem, MarketplaceFinanceComponent, db
from services.marketplace_fulfillment_display import listing_previews, related_postings


def _children(model, *, account, ids):
    return model.query.filter(
        model.fact_id.in_(ids), model.seller_id == account.seller_id,
        model.account_id == account.id,
    )


def _document(row):
    if isinstance(row, MarketplaceFinanceFactItem):
        # Never call the ORM relationship serializer before authorizing its FK.
        return {'id': row.id, 'listing_id': row.listing_id,
                'external_sku': row.external_sku, 'match_status': row.match_status}
    return {'id': row.id, **row.to_public_dict()}


def _decorate(lines, *, account):
    listing_previews(lines, account=account)
    for line in lines:
        if 'match_status' in line:
            line['title'] = line['listing']['title'] if line['listing'] else None
            line['offer_id'] = line['listing']['offer_id'] if line['listing'] else None
            if not line['listing'] and line['match_status'] == 'matched':
                line['match_status'] = 'unavailable'


def _postings(documents, facts, *, account):
    related_postings(documents, facts, account=account)
    for item in documents:
        if not item['posting_url']:
            item['posting_id'] = None


def fact_previews(facts, *, account, compact=False):
    documents = {f.id: f.to_public_dict() for f in facts}
    if not documents:
        return []
    for model, field in [(MarketplaceFinanceFactItem, 'items'), (MarketplaceFinanceComponent, 'components')]:
        query = _children(model, account=account, ids=list(documents))
        counts = dict(query.with_entities(model.fact_id, func.count(model.id)).group_by(model.fact_id).all())
        for fact_id, item in documents.items():
            item[field] = []
            item[field + '_count'] = counts.get(fact_id, 0)
        if compact:
            ranks = query.with_entities(model.id.label('child_id'), func.row_number().over(
                partition_by=model.fact_id, order_by=model.id,
            ).label('rank')).subquery()
            rows = model.query.join(ranks, ranks.c.child_id == model.id).filter(ranks.c.rank <= 3).order_by(model.id).all()
        else:
            rows = query.order_by(model.id).all()
        lines = []
        for row in rows:
            line = _document(row)
            lines.append(line)
            documents[row.fact_id][field].append(line)
        _decorate(lines, account=account)
        for item in documents.values():
            item[field + '_truncated'] = item[field + '_count'] > len(item[field])
    result = list(documents.values())
    _postings(result, facts, account=account)
    return result


def fact_detail(fact, *, account, item_page=1, component_page=1, per_page=50):
    data = fact.to_public_dict()
    for model, field, page in [(MarketplaceFinanceFactItem, 'items', item_page),
                               (MarketplaceFinanceComponent, 'components', component_page)]:
        result = _children(model, account=account, ids=[fact.id]).order_by(model.id).paginate(
            page=page, per_page=per_page, error_out=False,
        )
        data[field] = [_document(row) for row in result.items]
        _decorate(data[field], account=account)
        data[field + '_pagination'] = {'page': page, 'per_page': per_page, 'pages': result.pages, 'total': result.total}
    _postings([data], [fact], account=account)
    return data
