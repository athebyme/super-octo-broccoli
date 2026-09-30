"""Bounded canonical source selection, not marketplace identity inference."""

from sqlalchemy import case, func, or_

from models import ImportedProduct, db


def search_sources(*, seller_id, query='', limit=20):
    if type(seller_id) is not int or seller_id <= 0:
        raise ValueError('Некорректный продавец')
    if not isinstance(query, str) or len(query) > 100:
        raise ValueError('Поисковый запрос должен быть не длиннее 100 символов')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Лимит поиска должен быть от 1 до 20')
    query = query.strip()
    columns = (ImportedProduct.id, ImportedProduct.title, ImportedProduct.external_id, ImportedProduct.external_vendor_code)
    rows = db.session.query(*columns).filter(ImportedProduct.seller_id == seller_id)
    if query:
        normalized = query.casefold()
        if db.engine.dialect.name == 'sqlite':
            # SQLite lower()/NOCASE are ASCII-only. A separate function avoids
            # changing equality/identity semantics anywhere else in the app.
            connection = db.session.connection().connection.driver_connection
            connection.create_function('sh_source_casefold', 1, lambda value: str(value or '').casefold(), deterministic=True)
            lower = func.sh_source_casefold
        else:
            lower = func.lower
        expressions = [lower(column).contains(normalized, autoescape=True) for column in columns[1:]]
        exact = [lower(column) == normalized for column in columns[2:]]
        if query.isascii() and query.isdecimal() and len(query) <= 18 and int(query) > 0:
            exact.append(ImportedProduct.id == int(query))
            expressions.append(ImportedProduct.id == int(query))
        rows = rows.filter(or_(*expressions)).order_by(case((or_(*exact), 0), else_=1))
    found = rows.order_by(ImportedProduct.id.desc()).limit(limit + 1).all()
    return {
        'items': [{
            'id': row.id, 'title': (row.title or 'Без названия')[:300],
            'external_id': (row.external_id or '')[:200],
            'vendor_code': (row.external_vendor_code or '')[:200],
        } for row in found[:limit]],
        'has_more': len(found) > limit,
    }
