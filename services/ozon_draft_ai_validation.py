"""Source-only, reference-sealed suggestions for seller-owned Ozon drafts.

This module performs local reads and pure validation. It neither calls a model
nor writes a draft. The coordinator owns admission, transport and audit commit.
``literal_source`` certifies literal presence in the sealed source and a
compatible named source field when one is supplied; it does not certify the
product claim's semantic truth. Every result remains a seller-reviewed proposal.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any

from models import (
    MarketplaceAttributeDefinition, MarketplaceAttributeValue,
    MarketplaceProductDraft, OzonDraftCompletionSuggestion, SupplierProduct, db,
)
from scripts.validate_luna_parsing import (
    ContractError, evidence_quotes, value_grounded_in_source,
)
from services.marketplace_drafts import (
    MarketplaceDraftError, MarketplaceDraftService,
    MarketplaceDraftValidationError,
)
from services.ozon_reference_service import OzonReferenceService
from services.ozon_brand_policy import match_forbidden_ozon_brand


SOURCE_KEYS = frozenset({
    'title', 'description', 'brand', 'category', 'all_categories',
    'characteristics', 'materials', 'colors', 'sizes_raw', 'sizes',
})
TEXT_KEYS = frozenset({'title', 'description', 'brand', 'category', 'sizes_raw'})
LIST_KEYS = frozenset({'all_categories', 'materials', 'colors', 'sizes'})
HIGH_RISK = re.compile(
    r'страна|сертифик|декларац|маркиров|возраст|упаков|габарит|вес|масса|'
    r'объ[её]м|состав|штрих|баркод|гарант|производител|длина|ширина|высота|'
    r'диаметр|размер|country|certif|compliance|composition|ingredient|'
    r'packag|dimension|weight|volume|barcode|age|length|width|height|diameter|size', re.I,
)
_URL = re.compile(r'''(?i)(?<!\w)https?://[^\s<>"']+''')
_EMAIL = re.compile(r'(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}')
_CREDENTIAL = re.compile(
    r'''(?i)\b(?:bearer\s+[^\s,;]+|'''
    r'''(?:api[ _-]?(?:key|token)|access[ _-]?token|secret|password|пароль|ключ\s*api)'''
    r'''\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+))''',
)
_PHONE_CANDIDATE = re.compile(r'(?<!\w)\+?\d[\d ()-]{8,30}\d(?!\w)')
_LONG_DIGITS = re.compile(r'(?<!\d)\d{8,}(?!\d)')
SOURCE_MAX_BYTES = 128 * 1024
FACTS_MAX_BYTES = 16 * 1024
SCHEMA_MAX_BYTES = 32 * 1024
MAX_DEFINITIONS = 200
MAX_DICTIONARY_VALUES = 40
MAX_DICTIONARY_TERMS = 300
MAX_SUGGESTIONS = 40
ENGINE_OWNED_ATTRIBUTE_IDS = frozenset({
    MarketplaceDraftService.OZON_DESCRIPTION_ATTRIBUTE_ID,
    MarketplaceDraftService.OZON_TYPE_ATTRIBUTE_ID,
    MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID,
    MarketplaceDraftService.OZON_GROUP_ATTRIBUTE_ID,
    MarketplaceDraftService.OZON_ADULT_ATTRIBUTE_ID,
    MarketplaceDraftService.OZON_HASHTAG_ATTRIBUTE_ID,
})


class OzonDraftAIValidationError(ValueError):
    """Only application-owned codes/messages cross the seller API boundary."""

    status_code = 409

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _fail(code: str, message: str) -> None:
    raise OzonDraftAIValidationError(code, message)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('duplicate_source_key', 'Исходный снимок содержит повторяющиеся ключи.')
        result[key] = value
    return result


def _nonfinite(_value):
    _fail('nonfinite_source', 'В исходном снимке есть некорректное число.')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        _nonfinite(value)
    return number


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode('utf-8')).hexdigest()


def _source_document(raw: str | None, *, missing: bool = False) -> dict | None:
    if raw in (None, ''):
        return None if missing else _fail(
            'source_snapshot_missing', 'Нет исходного снимка поставщика для подсказок.',
        )
    try:
        size = len(raw.encode('utf-8')) if isinstance(raw, str) else SOURCE_MAX_BYTES + 1
    except UnicodeError:
        _fail('source_snapshot_invalid', 'Исходный снимок повреждён; проверьте данные вручную.')
    if size > SOURCE_MAX_BYTES:
        _fail('source_snapshot_too_large', 'Исходный снимок слишком велик для AI-подсказок.')
    try:
        value = json.loads(raw, object_pairs_hook=_object_pairs,
                           parse_constant=_nonfinite, parse_float=_finite_float)
    except OzonDraftAIValidationError:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _fail('source_snapshot_invalid', 'Исходный снимок повреждён; проверьте данные вручную.')
    if not isinstance(value, dict):
        _fail('source_snapshot_invalid', 'Исходный снимок должен быть объектом.')
    return value


def _source_scalar(value: Any, maximum: int = 3000) -> str | None:
    if not isinstance(value, str):
        return None
    value = ' '.join(value.split())
    # Whitelisting a free-text field does not whitelist credentials, signed
    # media links, contacts or long numeric identifiers embedded inside it.
    value = _URL.sub(' ', value)
    value = _EMAIL.sub(' ', value)
    value = _CREDENTIAL.sub(' ', value)
    value = _LONG_DIGITS.sub(' ', value)
    value = _PHONE_CANDIDATE.sub(
        lambda match: ' ' if 10 <= sum(ch.isdigit() for ch in match.group()) <= 15
        else match.group(), value,
    )
    value = ' '.join(value.split())
    return value[:maximum] if value else None


def _source_list(value: Any) -> list:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:30]:
        text = _source_scalar(item, 300)
        if text:
            result.append(text)
    return result


def _characteristics(value: Any) -> list:
    rows = []
    if isinstance(value, dict):
        value = [{'name': key, 'value': item} for key, item in list(value.items())[:50]]
    if not isinstance(value, list):
        return rows
    for item in value[:50]:
        if not isinstance(item, dict):
            continue
        name = _source_scalar(item.get('name') or item.get('key'), 120)
        if not name or (HIGH_RISK.search(name) and _source_field_name(name) != 'sizes'):
            continue
        raw = item.get('value')
        if isinstance(raw, list):
            values = _source_list(raw)
            if values:
                rows.append({'name': name, 'value': values})
        else:
            text = _source_scalar(raw, 500)
            if text:
                rows.append({'name': name, 'value': text})
    return rows[:30]


def _source_facts(snapshot: dict) -> dict:
    facts = {}
    for key in sorted(SOURCE_KEYS):
        value = snapshot.get(key)
        if key in TEXT_KEYS:
            clean = _source_scalar(value, 3000 if key == 'description' else 700)
        elif key == 'characteristics':
            clean = _characteristics(value)
        else:
            clean = _source_list(value)
        if clean:
            facts[key] = clean
    if not facts:
        _fail('source_facts_missing', 'В исходном снимке нет пригодных фактов для подсказок.')
    try:
        facts_size = len(_dump(facts).encode('utf-8'))
    except (TypeError, ValueError, UnicodeError):
        _fail('source_snapshot_invalid', 'Исходный снимок повреждён; проверьте данные вручную.')
    if facts_size > FACTS_MAX_BYTES:
        _fail('source_facts_too_large', 'Исходные факты слишком велики для AI-подсказок.')
    return facts


def _literal_dictionary_terms(facts: dict) -> list[str]:
    """Index-backed exact display shortlist, never a fuzzy model mapping."""
    strings = []
    for row in facts.get('characteristics', []):
        value = row.get('value') if isinstance(row, dict) else None
        strings.extend(value if isinstance(value, list) else [value])
    for key in ('colors', 'materials', 'sizes', 'sizes_raw', 'brand',
                'title', 'description', 'category', 'all_categories'):
        value = facts.get(key)
        strings.extend(value if isinstance(value, list) else [value])
    result, seen = [], set()
    for source in strings:
        if not isinstance(source, str):
            continue
        normalized = OzonReferenceService.normalize_value(source)
        words = re.findall(r'[\wё-]+', normalized)
        candidates = [normalized] if 0 < len(normalized) <= 100 else []
        for length in (1, 2, 3):
            candidates.extend(' '.join(words[offset:offset + length])
                              for offset in range(max(0, len(words) - length + 1)))
        for value in candidates:
            if value and value not in seen and len(value) <= 100:
                seen.add(value)
                result.append(value)
                if len(result) >= MAX_DICTIONARY_TERMS:
                    return result
    return result


def _filled_documents(draft: MarketplaceProductDraft) -> tuple[list, list]:
    try:
        documents, _baseline = MarketplaceDraftService.publication_documents(draft)
    except MarketplaceDraftError:
        _fail('draft_state_unavailable', 'Текущее содержимое карточки требует ручной проверки.')
    attributes = documents.get('attributes')
    groups = documents.get('complex_attributes')
    if not isinstance(attributes, list) or not isinstance(groups, list):
        _fail('draft_attributes_invalid', 'Сохранённые атрибуты карточки повреждены.')
    return attributes, groups


def _filled_slots(attributes: list, groups: list) -> list[dict]:
    result = []
    for item in attributes:
        if isinstance(item, dict) and item.get('values'):
            result.append({'attribute_id': item.get('attribute_id'),
                           'complex_id': item.get('complex_id', '0')})
    for group in groups:
        if not isinstance(group, dict):
            continue
        for item in group.get('attributes', []):
            if isinstance(item, dict) and item.get('values'):
                result.append({'attribute_id': item.get('attribute_id'),
                               'complex_id': item.get('complex_id', '0')})
    return sorted(result, key=lambda row: (str(row['attribute_id']), str(row['complex_id'])))


def _eligible(field: MarketplaceAttributeDefinition) -> bool:
    # The first lane suggests only literal text or exact dictionary values.
    # Physical measurements, certification and other regulated fields remain
    # manual even if a model quotes a number from the feed.
    if (not field.is_enabled or not field.is_available
            or field.external_attribute_id in ENGINE_OWNED_ATTRIBUTE_IDS
            or field.attribute_complex_id):
        return False
    # An explicitly observed apparel/product size is in the first-lane source
    # contract. Keep package dimensions and other measurement fields manual.
    if HIGH_RISK.search(field.name) and _source_field_name(field.name) != 'sizes':
        return False
    if MarketplaceDraftService._normalized_text(field.data_type) != 'string':
        return False
    return bool(re.fullmatch(r'[1-9][0-9]*', field.external_attribute_id or ''))


_SOURCE_FIELD_ALIASES = {
    'colors': frozenset({'цвет', 'цвет товара', 'цвет изделия', 'основной цвет'}),
    'materials': frozenset({'материал', 'материал товара', 'материал изделия'}),
    'sizes': frozenset({'размер', 'размер товара', 'размер изделия', 'размеры'}),
    'brand': frozenset({'бренд', 'торговая марка'}),
}


def _source_field_name(value: str) -> str:
    """Collapse only reviewed source/target label aliases, never value aliases."""
    normalized = OzonReferenceService.normalize_value(value)
    for canonical, aliases in _SOURCE_FIELD_ALIASES.items():
        if normalized in aliases:
            return canonical
    return normalized


def _effective_max_value_count(field: MarketplaceAttributeDefinition) -> int:
    """Expose the same bounded cardinality enforced by validate_result."""
    if not field.is_collection:
        return 1
    configured = field.max_value_count
    if type(configured) is int and configured > 0:
        return min(configured, MarketplaceDraftService.MAX_ATTRIBUTE_VALUES)
    return MarketplaceDraftService.MAX_ATTRIBUTE_VALUES


def _evidence_field_binding(path: str, target_name: str, facts: dict) -> bool | None:
    """Bind explicitly labeled facts to a target field.

    ``None`` means prose without a source field label. Such literal evidence is
    still a suggestion for mandatory seller review, not semantic proof.
    """
    tokens = [token.replace('~1', '/').replace('~0', '~') for token in path[1:].split('/')]
    root = tokens[0]
    target = _source_field_name(target_name)
    if root == 'characteristics':
        if (len(tokens) not in (3, 4) or not tokens[1].isdigit()
                or tokens[2] != 'value'):
            return False
        rows = facts.get('characteristics', [])
        index = int(tokens[1])
        if index >= len(rows) or not isinstance(rows[index], dict):
            return False
        source_name = rows[index].get('name')
        return isinstance(source_name, str) and _source_field_name(source_name) == target
    if root in ('colors', 'materials', 'sizes', 'sizes_raw', 'brand'):
        canonical = 'sizes' if root == 'sizes_raw' else root
        return target == canonical
    # A title, description or category has no field label. Literal membership
    # can be offered for human review, but must not override an explicit label.
    return None


class OzonDraftAIValidation:
    @classmethod
    def capture(cls, draft: MarketplaceProductDraft) -> dict:
        if not isinstance(draft, MarketplaceProductDraft) or not draft.id:
            _fail('draft_required', 'Нужен сохранённый черновик Ozon.')
        if (not draft.account or draft.account.seller_id != draft.seller_id
                or draft.account.id != draft.account_id
                or not draft.account.is_active
                or not draft.marketplace or draft.marketplace.code != 'ozon'):
            _fail('draft_scope_mismatch', 'Черновик и кабинет продавца не совпадают.')
        imported = draft.imported_product
        if not imported or imported.id != draft.imported_product_id or imported.seller_id != draft.seller_id:
            _fail('source_scope_mismatch', 'Исходный товар больше не принадлежит продавцу.')
        source_kind = 'imported'
        source_product_id = None
        snapshot = _source_document(imported.original_data, missing=True)
        if snapshot is None:
            if not imported.supplier_product_id:
                _fail('source_snapshot_missing', 'Нет исходного снимка поставщика для подсказок.')
            source = db.session.get(SupplierProduct, imported.supplier_product_id)
            if source is None or source.id != imported.supplier_product_id:
                _fail('supplier_source_missing', 'Точный исходный товар поставщика не найден.')
            snapshot = _source_document(source.original_data_json)
            source_kind = 'supplier'
            source_product_id = source.id
        facts = _source_facts(snapshot)
        dictionary_terms = _literal_dictionary_terms(facts)
        product_type = draft.product_type
        if product_type is None or product_type.id != draft.product_type_id:
            _fail('product_type_required', 'Выберите точную категорию и тип Ozon.')
        if (not product_type.category
                or product_type.marketplace_id != draft.marketplace_id
                or not product_type.is_seller_selectable
                or draft.external_category_id != product_type.category.external_category_id
                or draft.external_type_id != product_type.external_type_id):
            _fail('product_type_identity_mismatch', 'Выбранный тип Ozon изменился.')
        if (not OzonReferenceService.tree_is_fresh(product_type.marketplace)
                or not OzonReferenceService.reference_is_fresh(product_type)):
            _fail('schema_stale', 'Схема Ozon устарела; обновите справочник и повторите.')
        attributes, groups = _filled_documents(draft)
        filled = _filled_slots(attributes, groups)
        filled_keys = {(str(row['attribute_id']), str(row['complex_id'])) for row in filled}
        definitions = MarketplaceAttributeDefinition.query.filter_by(
            product_type_id=product_type.id, is_available=True,
        ).order_by(MarketplaceAttributeDefinition.id).limit(MAX_DEFINITIONS + 1).all()
        if len(definitions) > MAX_DEFINITIONS:
            _fail('schema_context_too_large', 'Схема типа слишком велика для одной AI-подсказки.')
        effective_definitions = [{
            'attribute_id': field.external_attribute_id,
            'complex_id': field.attribute_complex_id or '0',
            'name': field.name,
            'data_type': field.data_type,
            'required': bool(field.is_required),
            'enabled': bool(field.is_enabled),
            'available': bool(field.is_available),
            'dictionary_id': field.dictionary_id,
            'max_value_count': field.max_value_count,
            'collection': bool(field.is_collection),
            'complex_collection': bool(field.complex_is_collection),
        } for field in definitions]
        schema_fields, dictionary_seal, issues = [], [], []
        for field in definitions:
            complex_id = field.attribute_complex_id or '0'
            if field.attribute_complex_id and (
                field.external_attribute_id, complex_id
            ) not in filled_keys:
                issues.append('complex_group_requires_manual_review')
            if not _eligible(field):
                continue
            values = []
            if field.dictionary_id:
                if not OzonReferenceService.dictionary_is_fresh(field):
                    issues.append('dictionary_stale')
                    continue
                restrictions = set(field.restriction_value_ids)
                dictionary_seal.append({
                    'attribute_id': field.external_attribute_id,
                    'snapshot_hash': field.values_snapshot_hash,
                    'version': field.values_version,
                    'restriction_value_ids': sorted(restrictions),
                })
                if (field.external_attribute_id, complex_id) in filled_keys:
                    continue
                rows = MarketplaceAttributeValue.query.filter_by(
                    product_type_id=product_type.id, attribute_id=field.id,
                    is_available=True,
                ).filter(
                    MarketplaceAttributeValue.value_normalized.in_(dictionary_terms),
                ).order_by(MarketplaceAttributeValue.id).limit(MAX_DICTIONARY_VALUES + 1).all()
                if len(rows) > MAX_DICTIONARY_VALUES:
                    issues.append('dictionary_shortlist_too_large')
                    continue
                values = [
                    {'dictionary_value_id': row.external_value_id, 'value': row.value}
                    for row in rows if not restrictions or row.external_value_id in restrictions
                ]
                if not values:
                    issues.append('dictionary_no_eligible_values')
                    continue
            elif (field.external_attribute_id, complex_id) in filled_keys:
                continue
            schema_fields.append({
                'attribute_id': field.external_attribute_id,
                'complex_id': complex_id,
                'name': field.name[:200],
                'data_type': 'string',
                'required': bool(field.is_required),
                'max_value_count': _effective_max_value_count(field),
                'collection': bool(field.is_collection),
                'complex_collection': bool(field.complex_is_collection),
                'dictionary_id': field.dictionary_id,
                'dictionary_values': values,
            })
        if not schema_fields:
            _fail('no_eligible_missing_attributes',
                  'Для этого черновика нет безопасных незаполненных атрибутов; заполните вручную.')
        schema = {
            'external_category_id': product_type.category.external_category_id,
            'external_type_id': product_type.external_type_id,
            'attributes': schema_fields,
        }
        try:
            schema_size = len(_dump(schema).encode('utf-8'))
        except (TypeError, ValueError, UnicodeError):
            _fail('schema_context_invalid', 'Схема типа повреждена; обновите справочник.')
        if schema_size > SCHEMA_MAX_BYTES:
            _fail('schema_context_too_large', 'Схема типа слишком велика для одной AI-подсказки.')
        source_identity = {
            'source_kind': source_kind, 'source_product_id': source_product_id,
            'imported_product_id': imported.id, 'facts': facts,
        }
        return {
            'draft_id': draft.id, 'seller_id': draft.seller_id,
            'account_id': draft.account_id, 'imported_product_id': imported.id,
            'product_type_id': product_type.id,
            'expected_draft_version': draft.version,
            'source_kind': source_kind, 'source_product_id': source_product_id,
            'source_hash': _hash(source_identity),
            'type_schema_hash': _hash({
                'marketplace_id': draft.marketplace_id,
                'category_id': product_type.category_id,
                'external_category_id': product_type.category.external_category_id,
                'external_type_id': product_type.external_type_id,
                'attributes_schema_hash': product_type.attributes_schema_hash,
                'effective_definitions': effective_definitions,
            }),
            'dictionary_hash': _hash(dictionary_seal),
            'filled_slots_hash': _hash({'attributes': attributes,
                                        'complex_attributes': groups}),
            'source_facts': facts, 'schema': schema,
            'missing_slots': [{'attribute_id': row['attribute_id'],
                               'complex_id': row['complex_id']} for row in schema_fields],
            'filled_slots': filled,
            'reference_issues': sorted(set(issues)),
        }

    @staticmethod
    def filled_slots_hash(draft: MarketplaceProductDraft) -> str:
        """Recompute only the current value seal after an atomic partial apply."""
        attributes, groups = _filled_documents(draft)
        return _hash({'attributes': attributes, 'complex_attributes': groups})

    @classmethod
    def check_seal(cls, draft: MarketplaceProductDraft, item) -> dict:
        if (draft.id != item.draft_id or draft.seller_id != item.seller_id
                or draft.account_id != item.account_id
                or draft.imported_product_id != item.imported_product_id
                or draft.product_type_id != item.product_type_id):
            _fail('draft_identity_changed', 'Идентичность черновика изменилась.')
        accepted_versions = [row[0] for row in db.session.query(
            OzonDraftCompletionSuggestion.applied_draft_version,
        ).filter(
            OzonDraftCompletionSuggestion.item_id == item.id,
            OzonDraftCompletionSuggestion.status == 'accepted',
            OzonDraftCompletionSuggestion.applied_draft_version.isnot(None),
        ).all()]
        expected_version = max(accepted_versions, default=item.expected_draft_version)
        if draft.version != expected_version:
            _fail('draft_version_changed', 'Черновик изменился; требуется новый просмотр.')
        captured = cls.capture(draft)
        for name in ('source_kind', 'source_product_id', 'source_hash',
                     'type_schema_hash', 'dictionary_hash'):
            if captured[name] != getattr(item, name):
                _fail('source_or_schema_changed', 'Источник или схема изменились; подсказки устарели.')
        expected_filled = item.reviewed_filled_slots_hash or item.filled_slots_hash
        if captured['filled_slots_hash'] != expected_filled:
            _fail('filled_slots_changed', 'Заполненные поля изменились; требуется новый просмотр.')
        return captured

    @classmethod
    def validate_result(cls, sealed_context: dict, raw: Any) -> dict:
        if isinstance(raw, str):
            if len(raw.encode('utf-8')) > 256 * 1024:
                _fail('model_response_too_large', 'Ответ модели слишком велик.')
            try:
                raw = json.loads(raw, object_pairs_hook=_object_pairs,
                                 parse_constant=_nonfinite)
            except OzonDraftAIValidationError:
                raise
            except (TypeError, ValueError, RecursionError, UnicodeError):
                _fail('invalid_model_response', 'Ответ модели не является корректным JSON.')
        if (not isinstance(raw, dict) or set(raw) != {'draft_id', 'suggestions'}
                or raw.get('draft_id') != sealed_context['draft_id']
                or not isinstance(raw.get('suggestions'), list)
                or len(raw['suggestions']) > MAX_SUGGESTIONS):
            _fail('invalid_model_response', 'Ответ модели не соответствует составу черновика.')
        by_slot = {(row['attribute_id'], row['complex_id']): row
                   for row in sealed_context['schema']['attributes']}
        suggestions, rejections, seen = [], [], set()
        for row in raw['suggestions']:
            if not isinstance(row, dict) or set(row) != {
                'attribute_id', 'complex_id', 'group_ordinal', 'values',
                'evidence', 'provenance_code',
            }:
                rejections.append('invalid_suggestion_shape')
                continue
            if (not isinstance(row['attribute_id'], str)
                    or not isinstance(row['complex_id'], str)
                    or type(row['group_ordinal']) is not int):
                rejections.append('invalid_suggestion_identity')
                continue
            identity = (row['attribute_id'], row['complex_id'], row['group_ordinal'])
            if identity in seen:
                _fail('duplicate_suggestion_slot', 'Модель повторила один и тот же атрибут.')
            seen.add(identity)
            if row['complex_id'] != '0':
                rejections.append('complex_group_requires_manual_review')
                continue
            definition = by_slot.get((row['attribute_id'], row['complex_id']))
            if definition is None or row['provenance_code'] != 'literal_source':
                rejections.append('attribute_outside_sealed_schema')
                continue
            ordinal = row['group_ordinal']
            if (type(ordinal) is not int or ordinal < 0 or ordinal > 50
                    or (row['complex_id'] == '0' and ordinal != 0)
                    or (row['complex_id'] != '0' and ordinal == 0)):
                rejections.append('invalid_complex_group')
                continue
            values = row['values']
            cap = definition['max_value_count'] or MarketplaceDraftService.MAX_ATTRIBUTE_VALUES
            if not isinstance(values, list) or not 1 <= len(values) <= min(cap, MarketplaceDraftService.MAX_ATTRIBUTE_VALUES):
                rejections.append('attribute_values_limit')
                continue
            if not definition['collection'] and len(values) > 1:
                rejections.append('attribute_not_collection')
                continue
            try:
                evidence_quotes(row['evidence'], sealed_context['source_facts'])
            except ContractError as exc:
                rejections.append(str(exc))
                continue
            bindings = [
                _evidence_field_binding(entry['path'], definition['name'],
                                        sealed_context['source_facts'])
                for entry in row['evidence']
            ]
            if False in bindings:
                rejections.append('source_field_mismatch')
                continue
            # If the model supplied named evidence, each proposed value must
            # be present in that correctly bound evidence. Additional prose
            # cannot launder an unrelated value into a named source field.
            grounded_evidence = [entry for entry, binding in zip(row['evidence'], bindings)
                                 if binding is True] if True in bindings else row['evidence']
            canonical_values = []
            allowed = {value['dictionary_value_id']: value['value']
                       for value in definition['dictionary_values']}
            for value in values:
                if (not isinstance(value, dict) or set(value) not in
                        ({'value'}, {'value', 'dictionary_value_id'})
                        or not isinstance(value.get('value'), str)
                        or not 1 <= len(value['value'].strip()) <= 1000):
                    rejections.append('invalid_attribute_value')
                    break
                display = value['value']
                if (_source_field_name(definition['name']) == 'brand'
                        and match_forbidden_ozon_brand(display)):
                    rejections.append('forbidden_brand')
                    break
                if definition['dictionary_id']:
                    external_id = value.get('dictionary_value_id')
                    if not isinstance(external_id, str) or allowed.get(external_id) != display:
                        rejections.append('dictionary_value_not_exact')
                        break
                elif 'dictionary_value_id' in value:
                    rejections.append('unexpected_dictionary_value_id')
                    break
                if not value_grounded_in_source(display, grounded_evidence,
                                                sealed_context['source_facts']):
                    rejections.append('value_not_grounded')
                    break
                canonical_values.append(deepcopy(value))
            else:
                suggestions.append({
                    'attribute_id': row['attribute_id'],
                    'complex_id': row['complex_id'],
                    'group_ordinal': ordinal,
                    'values': canonical_values,
                    'evidence': deepcopy(row['evidence']),
                    'provenance_code': 'literal_source',
                })
        return {'suggestions': suggestions, 'rejections': rejections[:40]}

    @classmethod
    def merge_selected(cls, draft: MarketplaceProductDraft,
                       suggestions: list[dict]) -> dict:
        if not isinstance(suggestions, list) or not suggestions:
            _fail('selection_required', 'Выберите хотя бы одну подсказку.')
        current_attributes, current_groups = _filled_documents(draft)
        attributes = deepcopy(current_attributes)
        groups = deepcopy(current_groups)
        occupied = {(row['attribute_id'], row['complex_id'])
                    for row in _filled_slots(attributes, groups)}
        seen = set()
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                _fail('invalid_suggestion', 'Подсказка повреждена.')
            attr = suggestion.get('attribute_id')
            complex_id = suggestion.get('complex_id', '0')
            ordinal = suggestion.get('group_ordinal', 0)
            if not isinstance(attr, str) or not isinstance(complex_id, str) or type(ordinal) is not int:
                _fail('invalid_suggestion', 'Идентичность подсказки повреждена.')
            identity = (attr, complex_id, ordinal)
            if identity in seen or (attr, complex_id) in occupied:
                _fail('attribute_already_filled', 'Поле уже заполнено или повторено.')
            seen.add(identity)
            if complex_id != '0':
                _fail('complex_group_requires_manual_review',
                      'Связанную группу характеристик заполните вручную.')
            native = {'attribute_id': attr, 'complex_id': complex_id,
                      'values': deepcopy(suggestion.get('values'))}
            if ordinal != 0:
                _fail('invalid_complex_group', 'Complex-группа повреждена.')
            attributes.append(native)
        try:
            attributes = MarketplaceDraftService._normalize_attribute_items(
                attributes, 'attributes',
            )
            groups = MarketplaceDraftService._normalize_complex_attributes(groups)
        except MarketplaceDraftValidationError:
            _fail('native_shape_invalid', 'Предложение не проходит формат Ozon.')
        baseline_errors, merged_errors = [], []
        MarketplaceDraftService._validate_attributes(
            product_type=draft.product_type,
            attributes=current_attributes, complex_groups=current_groups,
            errors=baseline_errors,
        )
        MarketplaceDraftService._validate_attributes(
            product_type=draft.product_type,
            attributes=attributes, complex_groups=groups,
            errors=merged_errors,
        )
        baseline = {(row['code'], row['field']) for row in baseline_errors}
        new_errors = [row for row in merged_errors
                      if (row['code'], row['field']) not in baseline]
        if new_errors:
            _fail('native_validation_failed', 'Подсказка не проходит текущую схему Ozon.')
        return {'attributes': attributes, 'complex_attributes': groups}
