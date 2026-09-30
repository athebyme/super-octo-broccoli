"""Current WB Analytics stock read; identity/quantity are observed, never guessed."""

ENDPOINT = '/api/analytics/v1/stocks-report/wb-warehouses'
PAGE_SIZE = 5000
MAX_BATCH_ROWS = 25000


class WBStockContractError(ValueError):
    pass


def _integer(value, name, *, minimum=0, maximum=2**63 - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WBStockContractError(f'Invalid WB stocks {name}')
    return value


def stock_request(nm_ids, *, offset=0, limit=PAGE_SIZE):
    if not isinstance(nm_ids, list) or not 1 <= len(nm_ids) <= 100:
        raise WBStockContractError('WB stocks requires 1..100 exact nmIds')
    ids = [_integer(value, 'nmId', minimum=1) for value in nm_ids]
    if len(set(ids)) != len(ids):
        raise WBStockContractError('Duplicate WB stock request nmId')
    return {
        'nmIds': ids, 'chrtIds': [],
        'offset': _integer(offset, 'offset', maximum=MAX_BATCH_ROWS),
        'limit': _integer(limit, 'limit', minimum=1, maximum=PAGE_SIZE),
    }


def normalize_stock_page(body, *, nm_ids, limit=PAGE_SIZE):
    data = body.get('data') if isinstance(body, dict) else None
    items = data.get('items') if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) > limit:
        raise WBStockContractError('Invalid WB stocks page')
    observed, seen, previous_nm = [], set(), 0
    for item in items:
        if not isinstance(item, dict):
            raise WBStockContractError('Invalid WB stocks row')
        row = {name: _integer(item.get(name), name, minimum=1) for name in ('nmId', 'chrtId', 'warehouseId')}
        if row['nmId'] not in nm_ids or row['nmId'] < previous_nm:
            raise WBStockContractError('WB stock identity or order mismatch')
        previous_nm = row['nmId']
        identity = tuple(row[name] for name in ('nmId', 'chrtId', 'warehouseId'))
        if identity in seen:
            raise WBStockContractError('Duplicate WB stock identity')
        seen.add(identity)
        for name in ('quantity', 'inWayToClient', 'inWayFromClient'):
            row[name] = _integer(item.get(name), name, maximum=2**31 - 1)
        name = item.get('warehouseName')
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise WBStockContractError('Invalid WB warehouse name')
        row['warehouseName'] = name.strip()
        observed.append(row)
    return observed


def combine_stock_pages(previous, current):
    if len(previous) + len(current) > MAX_BATCH_ROWS:
        raise WBStockContractError('WB stock batch exceeds safe row budget')
    if previous and current and previous[-1]['nmId'] > current[0]['nmId']:
        raise WBStockContractError('WB stock pagination order changed')
    seen = {(row['nmId'], row['chrtId'], row['warehouseId']) for row in previous}
    if any((row['nmId'], row['chrtId'], row['warehouseId']) in seen for row in current):
        raise WBStockContractError('WB stock pagination repeated an identity')
    return previous + current
