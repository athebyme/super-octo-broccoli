"""Identity-only quarantine preview from committed operation documents.

No ORM, provider calls, live listing fallback, title matching or mutable drafts.
An unknown identity widens the *proposed* scope to the account. The caller must
present that scope and obtain an explicit reviewed confirmation before saving it.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import unicodedata


MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
PRODUCT_KINDS = {'product_import', 'product_update', 'product_update_rollback'}
COMMERCIAL_KINDS = {'price_update', 'price_rollback', 'stock_update', 'stock_rollback'}
REASONS = {
    'immutable_target_verified': 'Артикул подтверждён сохранёнными данными отправки.',
    'identity_unknown': 'Не удалось подтвердить товар по сохранённой отправке. Остановка затронет весь магазин.',
    'identity_conflict': 'Сохранённые идентификаторы расходятся. Безопасно выделить один товар нельзя.',
    'document_invalid': 'Сохранённые данные отправки неполны или повреждены. Точная область товара неизвестна.',
    'unsupported_kind': 'Для этого вида операции точная область товара пока не подтверждена.',
}


class _Unknown(ValueError):
    def __init__(self, reason='document_invalid'):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class QuarantineScope:
    kind: str
    offer_id: str | None
    product_id: str | None
    reason_code: str

    def document(self):
        return {**asdict(self), 'explanation': REASONS[self.reason_code],
                'all_product_writes': True, 'includes_all_warehouses': True}

    def review_token(self, *, seller_id, marketplace_id, account_id, origin_type, origin_id, version):
        if origin_type not in {'operation', 'media_operation'}:
            raise ValueError('invalid_quarantine_origin_type')
        values = [seller_id, marketplace_id, account_id, origin_id, version]
        if any(type(value) is not int or not 0 < value <= 2**63-1 for value in values):
            raise ValueError('invalid_quarantine_review_context')
        # This is an optimistic review token, not an authorization signature.
        # Caller recomputes server-owned scope; no client identity is accepted.
        payload = [1, origin_type, values, asdict(self)]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()


def offer_id(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 200 or value != value.strip()
            or any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in value)):
        raise _Unknown('identity_unknown')
    return value


def product_id(value):
    if type(value) is int:
        if value <= 0 or value >= 10**100:
            raise _Unknown('identity_unknown')
        return str(value)
    if (not isinstance(value, str) or not 0 < len(value) <= 100 or not value.isascii()
            or not value.isdecimal() or int(value) <= 0):
        raise _Unknown('identity_unknown')
    return str(int(value))


def _object(raw):
    def invalid_constant(_value):
        raise _Unknown()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _Unknown()
            result[key] = value
        return result
    if not isinstance(raw, str) or len(raw) > MAX_DOCUMENT_BYTES:
        raise _Unknown()
    try:
        if len(raw.encode('utf-8')) > MAX_DOCUMENT_BYTES:
            raise _Unknown()
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise _Unknown() from None
    if not isinstance(value, dict):
        raise _Unknown()
    return value


def _single_item(payload):
    if not isinstance(payload, dict):
        raise _Unknown()
    items = payload.get('items')
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise _Unknown()
    return items[0]


def _agreement(values, normalize, *, required=True):
    if not values:
        if required:
            raise _Unknown('identity_unknown')
        return None
    result = {normalize(value) for value in values}
    if len(result) != 1:
        raise _Unknown('identity_conflict')
    return result.pop()


def operation_scope(*, kind, summary_json, submitted_json, before_json):
    """Read only known immutable identity locations for current Ozon kinds."""
    try:
        if kind not in PRODUCT_KINDS | COMMERCIAL_KINDS | {'product_import_rollback'}:
            raise _Unknown('unsupported_kind')
        summary, submitted, before = map(_object, (summary_json, submitted_json, before_json))
        offers = [summary.get('offer_id')]
        products = []
        if kind in PRODUCT_KINDS:
            item = _single_item(submitted)
            offers.append(item.get('offer_id'))
            if kind == 'product_import':
                # Current create boundary commits observed absence before HTTP.
                if before.get('exists') is not False or before.get('items') != []:
                    raise _Unknown()
                offers.append(before.get('offer_id'))
            else:
                identity = before.get('identity')
                if not isinstance(identity, dict):
                    raise _Unknown()
                offers.append(identity.get('offer_id'))
                products.extend([identity.get('product_id'), summary.get('external_product_id')])
                before_item = _single_item(before.get('payload', {}))
                offers.append(before_item.get('offer_id'))
            if 'product_id' in item:
                products.append(item['product_id'])
        elif kind == 'product_import_rollback':
            identity = before.get('identity')
            targets = submitted.get('product_id')
            if not isinstance(identity, dict) or not isinstance(targets, list) or len(targets) != 1:
                raise _Unknown()
            offers.append(identity.get('offer_id'))
            products.extend([identity.get('product_id'), summary.get('external_product_id'), targets[0]])
        else:
            for document in (before, submitted):
                offers.append(document.get('offer_id'))
                products.append(document.get('product_id'))
            # Stored commercial summaries include another exact before/proposed
            # pair. If present, it must agree; warehouse_id here is a local FK,
            # while the state holds a provider warehouse ID. Never compare them.
            for field in ('before', 'proposed'):
                if field in summary:
                    document = summary[field]
                    if not isinstance(document, dict):
                        raise _Unknown()
                    offers.append(document.get('offer_id'))
                    products.append(document.get('product_id'))
        return QuarantineScope('product', _agreement(offers, offer_id),
            _agreement(products, product_id, required=kind != 'product_import'), 'immutable_target_verified')
    except _Unknown as error:
        return QuarantineScope('account', None, None, error.reason)


def media_scope(*, account_id, external_item_id, target_json):
    """Preview historical Ozon media identities; does not enable media writes."""
    try:
        target = _object(target_json)
        if (type(account_id) is not int or account_id <= 0 or type(target.get('account_id')) is not int
                or target['account_id'] != account_id or target.get('marketplace_code') != 'ozon'
                or target.get('entity_kind') != 'marketplace_listing'):
            raise _Unknown('identity_conflict')
        return QuarantineScope('product', offer_id(target.get('offer_id')),
            _agreement([external_item_id, target.get('external_product_id')], product_id),
            'immutable_target_verified')
    except _Unknown as error:
        return QuarantineScope('account', None, None, error.reason)


def incoming_scope(*, kind, summary_json, submitted_json):
    """A never-attempted write has no live-before snapshot yet.

    Its committed desired identity is enough to *match* an existing fence.
    Unknown identity cannot prove exclusion and must match every account fence.
    This weaker parser must never be used to place a product quarantine.
    """
    try:
        summary, submitted = map(_object, (summary_json, submitted_json))
        offers, products = [summary.get('offer_id')], []
        if kind in PRODUCT_KINDS:
            item = _single_item(submitted)
            offers.append(item.get('offer_id'))
            if kind != 'product_import':
                products.append(summary.get('external_product_id'))
            if 'product_id' in item:
                products.append(item['product_id'])
        elif kind == 'product_import_rollback':
            targets = submitted.get('product_id')
            if not isinstance(targets, list) or len(targets) != 1:
                raise _Unknown()
            products.extend([summary.get('external_product_id'), targets[0]])
        elif kind in COMMERCIAL_KINDS:
            offers.append(submitted.get('offer_id'))
            products.append(submitted.get('product_id'))
            for key in ('before', 'proposed'):
                document = summary.get(key)
                if not isinstance(document, dict):
                    raise _Unknown()
                offers.append(document.get('offer_id'))
                products.append(document.get('product_id'))
        else:
            raise _Unknown('unsupported_kind')
        return QuarantineScope('product', _agreement(offers, offer_id),
            _agreement(products, product_id, required=kind != 'product_import'), 'immutable_target_verified')
    except _Unknown as error:
        return QuarantineScope('account', None, None, error.reason)
