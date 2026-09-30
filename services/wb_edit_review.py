"""Local WB characteristic schema and deterministic edit preview helpers.

These helpers only read Seller Hub's synchronized admin reference cache and
seller-owned ``Product`` snapshots. They never create a WB client or perform
provider I/O. The existing WB client validators remain the final write gate.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime
from typing import Any, Mapping

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError

from models import (
    BulkEditHistory,
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    Product,
    db,
)
from services.marketplace_validator import (
    _coerce_number,
    _allowed_values_for_characteristic,
    _resolve_wb_schema,
    validate_wb_characteristics,
)
from services.product_selection import (
    MAX_BULK_PRODUCT_SELECTION,
    ProductSelectionError,
    parse_selected_product_ids,
)


WB_EDIT_REVIEW_MAX_AGE_SECONDS = 15 * 60
WB_EDIT_REVIEW_SALT = 'wb-edit-preview-v2'
_WB_REVIEW_KEY_RE = re.compile(r'^[0-9a-f]{64}$')


class WBEditReviewError(ValueError):
    """The local schema, input, preview, or review token is not safe to use."""


def parse_exact_wb_subject_id(value: Any) -> int:
    """Parse a WB subjectID without bool/float or lossy numeric coercion."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
        if parsed > 0:
            return parsed
    raise WBEditReviewError('WB subjectID должен быть положительным целым числом')


def _positive_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WBEditReviewError('Нужен положительный integer ID характеристики')
    return value


def _parse_current_characteristics(raw_json: Any) -> list[dict[str, Any]]:
    if raw_json in (None, ''):
        return []
    try:
        parsed = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except (TypeError, json.JSONDecodeError) as exc:
        raise WBEditReviewError('Локальный массив характеристик повреждён') from exc
    if not isinstance(parsed, list):
        raise WBEditReviewError('Локальные характеристики должны быть массивом')
    result = []
    seen = set()
    for item in parsed:
        if not isinstance(item, dict):
            raise WBEditReviewError('В локальном массиве есть неверная характеристика')
        raw_id = item.get('id')
        if isinstance(raw_id, bool):
            raise WBEditReviewError('ID локальной характеристики имеет неверный тип')
        if raw_id in (None, ''):
            # Preserve name-only legacy items for display; they cannot be
            # selected for a typed WB characteristic patch.
            result.append(dict(item))
            continue
        if isinstance(raw_id, int):
            charc_id = raw_id
        elif isinstance(raw_id, str) and raw_id.isascii() and raw_id.isdecimal():
            charc_id = int(raw_id)
        else:
            raise WBEditReviewError('ID локальной характеристики должен быть integer')
        if charc_id <= 0 or charc_id in seen:
            raise WBEditReviewError('ID локальной характеристики повторён или неверен')
        seen.add(charc_id)
        copy = dict(item)
        copy['id'] = charc_id
        result.append(copy)
    return result


def _values_for_form(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif value in (None, ''):
        values = []
    else:
        values = [value]
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


def _reference_status(subject_id: Any, validation_cache=None):
    base = validate_wb_characteristics(
        subject_id,
        [],
        validation_cache=validation_cache,
    )
    return bool(base.get('valid')), base


def _schema_revision(category, schema) -> dict[str, Any]:
    rows = [
        {
            'id': int(charc.charc_id),
            'type': int(charc.charc_type),
            'max_count': int(charc.max_count or 0),
            'required': bool(charc.required),
            'dictionary_hash': charc.dictionary_hash or '',
            'dictionary_version': int(charc.dictionary_version or 0),
        }
        for charc in schema or []
    ]
    payload = {
        'subject_id': int(category.subject_id),
        'version': int(category.characteristics_version or 0),
        'hash': category.characteristics_schema_hash or '',
        'rows': rows,
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8')
    payload['revision'] = hashlib.sha256(encoded).hexdigest()
    payload['synced_at'] = (
        category.characteristics_synced_at.isoformat()
        if category.characteristics_synced_at else None
    )
    return payload


def schema_for_subject(subject_id: Any, *, validation_cache=None) -> dict[str, Any]:
    """Return exact locally cached WB schema and effective form options."""
    try:
        subject_id = _positive_int(subject_id)
    except WBEditReviewError:
        return {
            'subject_id': None,
            'subject_name': None,
            'usable': False,
            'issues': [{'code': 'invalid_subject_id', 'message': 'subjectID не задан'}],
            'characteristics': [],
            'revision': None,
        }

    marketplace, category, schema = _resolve_wb_schema(
        subject_id,
        validation_cache=validation_cache,
    )
    reference_valid, reference = _reference_status(subject_id, validation_cache)
    if not marketplace or not category:
        return {
            'subject_id': subject_id,
            'subject_name': None,
            'usable': False,
            'issues': reference.get('issues') or [{
                'code': 'category_not_found',
                'message': 'Точная WB-категория не найдена в локальном справочнике',
            }],
            'characteristics': [],
            'revision': None,
        }

    if not reference_valid:
        # A stale cache may be useful as a diagnostic, but its characteristics
        # are no longer an editing contract. Keep the observed revision for a
        # fail-closed form guard and clear those fields from edit payloads.
        return {
            'subject_id': subject_id,
            'subject_name': category.subject_name,
            'parent_name': category.parent_name,
            'usable': False,
            'issues': list(reference.get('issues') or []),
            'characteristics': [],
            'revision': _schema_revision(category, schema),
        }

    characteristics = []
    for charc in sorted(schema or [], key=lambda item: (item.display_order or 0, item.charc_id)):
        try:
            allowed, source, issue = _allowed_values_for_characteristic(
                marketplace,
                charc,
                validation_cache,
            )
        except Exception:
            allowed, source, issue = None, None, {
                'code': 'schema_read_failed',
                'message': f'Не удалось прочитать правило «{charc.name}»',
            }
        field_usable = reference_valid and issue is None and charc.charc_type in (1, 4)
        characteristics.append({
            'id': int(charc.charc_id),
            'name': charc.name,
            'required': bool(charc.required),
            'charc_type': int(charc.charc_type),
            'max_count': int(charc.max_count or 0),
            'unit_name': charc.unit_name or '',
            'dictionary_values': list(allowed or []),
            'dictionary_source': source or 'free_text',
            'constrained': allowed is not None,
            'usable': field_usable,
            'issue': issue,
        })

    issues = list(reference.get('issues') or [])
    revision = _schema_revision(category, schema)
    effective_revision = hashlib.sha256(json.dumps({
        'base': revision['revision'],
        'effective_characteristics': [
            {
                'id': row['id'],
                'usable': row['usable'],
                'dictionary_source': row['dictionary_source'],
                'dictionary_values': row['dictionary_values'],
                'issue': row['issue'],
            }
            for row in characteristics
        ],
    }, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    revision['revision'] = effective_revision
    return {
        'subject_id': subject_id,
        'subject_name': category.subject_name,
        'parent_name': category.parent_name,
        'usable': bool(reference_valid and schema),
        'issues': issues,
        'characteristics': characteristics,
        'revision': revision,
    }


def product_characteristics_form(product: Product) -> dict[str, Any]:
    """Build the edit page model without a live WB API call."""
    current = _parse_current_characteristics(product.characteristics_json)
    by_id = {
        int(item['id']): item
        for item in current
        if isinstance(item.get('id'), int) and not isinstance(item.get('id'), bool)
    }
    schema = schema_for_subject(product.subject_id)
    fields = []
    for field in schema['characteristics']:
        present = by_id.get(field['id'])
        field = dict(field)
        field['present'] = present is not None
        field['current_values'] = _values_for_form(
            present.get('value') if present else None,
        )
        field['current_text'] = '\n'.join(field['current_values'])
        field['editable'] = bool(field['usable'])
        fields.append(field)
    known_ids = {field['id'] for field in fields}
    unknown_current = [
        {
            'id': item.get('id'),
            'name': item.get('name') or f"Характеристика {item.get('id') or ''}".strip(),
            'current_text': '\n'.join(_values_for_form(item.get('value'))),
        }
        for item in current
        if not isinstance(item.get('id'), int) or item['id'] not in known_ids
    ]
    return {
        'subject_id': schema['subject_id'],
        'subject_name': schema['subject_name'],
        'schema_usable': schema['usable'],
        'schema_issues': schema['issues'],
        'schema_revision': schema['revision'],
        'characteristic_fields': fields,
        'unknown_characteristics': unknown_current,
        'current_parse_ok': True,
    }


def prepare_single_product_characteristic_patch(product: Product, form: Any) -> list[dict[str, Any]]:
    """Validate changed editable fields for the exact locally cached subjectID."""
    form_model = product_characteristics_form(product)
    fields = {int(field['id']): field for field in form_model['characteristic_fields']}
    current = _characteristic_values_by_id(product.characteristics_json)
    submitted_names = set()
    changes = []
    for key in form.keys() if hasattr(form, 'keys') else []:
        if not isinstance(key, str) or not key.startswith('char_'):
            continue
        suffix = key[5:]
        if not suffix.isascii() or not suffix.isdecimal() or int(suffix) <= 0:
            raise WBEditReviewError('ID характеристики должен быть положительным целым числом')
        submitted_names.add(int(suffix))

    if not submitted_names:
        return []
    if not form_model['schema_usable']:
        raise WBEditReviewError('Точная локальная WB-схема устарела или недоступна')
    if submitted_names.difference(fields):
        raise WBEditReviewError('Форма содержит характеристику вне точной локальной схемы')
    submitted_revision = form.get('schema_revision', '') if hasattr(form, 'get') else ''
    if submitted_revision != form_model.get('schema_revision', {}).get('revision'):
        raise WBEditReviewError('Схема WB изменилась; обновите страницу и проверьте поля снова')

    for char_id, field in fields.items():
        if not field['usable']:
            raw_values = form.getlist(f'char_{char_id}') if hasattr(form, 'getlist') else []
            raw_one = form.get(f'char_{char_id}') if hasattr(form, 'get') else None
            if any(str(value).strip() for value in raw_values) or (raw_one is not None and str(raw_one).strip()):
                raise WBEditReviewError(f'«{field["name"]}»: это поле устарело или недоступно для записи')
            continue
        if char_id not in submitted_names:
            continue

        raw_values = form.getlist(f'char_{char_id}') if hasattr(form, 'getlist') else []
        raw_one = form.get(f'char_{char_id}') if hasattr(form, 'get') else None
        if field['charc_type'] == 1 and field['max_count'] > 1:
            if not field['dictionary_values']:
                value = [line.strip() for line in str(raw_one or '').splitlines() if line.strip()]
            elif raw_values:
                value = [str(item).strip() for item in raw_values if str(item).strip()]
            else:
                value = []
        else:
            value = str(raw_one or '').strip()
        if value in ('', []):
            continue
        candidate_values = _values_for_form(value)
        if candidate_values == current.get(char_id, []):
            continue
        changes.append({'id': char_id, 'value': value})

    if not changes:
        return []
    validation = validate_wb_characteristics(product.subject_id, changes)
    if not validation.get('valid'):
        details = '; '.join(item.get('message', '') for item in validation.get('issues', [])[:8])
        raise WBEditReviewError(details or 'Характеристики не прошли проверку по WB-схеме')
    schema_by_id = {int(field['id']): field for field in form_model['characteristic_fields']}
    normalized_changes = []
    for item in validation['normalized']:
        char_id = int(item['id'])
        field = schema_by_id[char_id]
        before = current.get(char_id, [])
        after_value = item['value']
        if field['charc_type'] == 4:
            previous_number = (
                _coerce_number(before[0], field.get('unit_name'))
                if len(before) == 1 else None
            )
            unchanged = previous_number is not None and previous_number == after_value
        elif field.get('dictionary_values'):
            allowed = {
                str(value).strip().casefold(): str(value).strip()
                for value in field['dictionary_values']
            }
            before_values = [allowed.get(value.casefold(), value) for value in before]
            after_values = _values_for_form(after_value)
            unchanged = [value.casefold() for value in before_values] == [
                value.casefold() for value in after_values
            ]
        else:
            unchanged = before == _values_for_form(after_value)
        if unchanged:
            continue
        normalized_changes.append({
            'id': char_id,
            'name': field['name'],
            'value': after_value,
        })
    return normalized_changes


def bulk_subject_groups(products: list[Product]) -> list[dict[str, Any]]:
    """Group selected cards by their exact stored WB subjectID."""
    grouped: dict[int | None, list[Product]] = {}
    for product in products:
        subject_id = product.subject_id
        if not isinstance(subject_id, int) or isinstance(subject_id, bool) or subject_id <= 0:
            subject_id = None
        grouped.setdefault(subject_id, []).append(product)

    result = []
    schema_cache: dict[int, dict[str, Any]] = {}
    for subject_id, rows in grouped.items():
        schema = schema_for_subject(subject_id) if subject_id else {
            'subject_name': None,
            'usable': False,
            'issues': [{'code': 'subject_id_missing', 'message': 'У карточки не задан exact subjectID'}],
            'characteristics': [],
            'revision': None,
        }
        if subject_id:
            schema_cache[subject_id] = schema
        display_names = sorted({str(row.object_name).strip() for row in rows if row.object_name})
        result.append({
            'subject_id': subject_id,
            'name': schema.get('subject_name') or ('Категория WB не подтверждена' if not subject_id else f'Предмет WB {subject_id}'),
            'object_names': display_names,
            'count': len(rows),
            'product_ids': [int(row.id) for row in rows],
            'usable': bool(schema.get('usable')),
            'issues': schema.get('issues') or [],
            'revision': schema.get('revision'),
        })
    return sorted(result, key=lambda group: (group['subject_id'] is None, group['subject_id'] or 0))


def bulk_characteristics_payload(products: list[Product]) -> dict[str, Any]:
    groups = bulk_subject_groups(products)
    by_subject = {}
    common = []
    for group in groups:
        subject_id = group.get('subject_id')
        if subject_id is None:
            continue
        schema = schema_for_subject(subject_id)
        by_subject[str(subject_id)] = [
            {
                **row,
                'values': [
                    {'id': value, 'value': value}
                    for value in row.get('dictionary_values', [])
                ],
            }
            for row in schema['characteristics']
        ]
    if len(by_subject) == 1:
        common = next(iter(by_subject.values()))
    elif by_subject:
        common_ids = set.intersection(*[
            {int(row['id']) for row in rows}
            for rows in by_subject.values()
        ])
        # IDs can coincide across WB subjects without describing the same
        # semantic field. Expose no cross-category common list; require an exact
        # subject choice in the editor.
        common = [] if len(by_subject) > 1 else [
            row for row in next(iter(by_subject.values())) if int(row['id']) in common_ids
        ]
    return {'groups': groups, 'by_subject': by_subject, 'common': common}


def _canonical_input_value(value: Any) -> Any:
    if isinstance(value, list):
        return [str(item).strip() for item in value if item is not None and str(item).strip()]
    if isinstance(value, str):
        return value.strip()
    return value


def parse_characteristic_changes(
    operation: str,
    *,
    changes: Any = None,
    char_id: Any = None,
    value: Any = None,
) -> list[dict[str, Any]]:
    if operation == 'update_characteristic':
        if isinstance(changes, str):
            try:
                changes = json.loads(changes)
            except json.JSONDecodeError as exc:
                raise WBEditReviewError('Список характеристик имеет неверный формат') from exc
        if not isinstance(changes, list) or not changes:
            raise WBEditReviewError('Добавьте хотя бы одну характеристику для изменения')
        if len(changes) > 50:
            raise WBEditReviewError('За один раз можно изменить не более 50 характеристик')
        result = []
        seen = set()
        for item in changes:
            if not isinstance(item, dict):
                raise WBEditReviewError('Изменение характеристики имеет неверный формат')
            raw_id = item.get('char_id')
            if isinstance(raw_id, str) and raw_id.isascii() and raw_id.isdecimal():
                parsed_id = int(raw_id)
            elif isinstance(raw_id, int) and not isinstance(raw_id, bool):
                parsed_id = raw_id
            else:
                raise WBEditReviewError('ID характеристики должен быть положительным числом')
            if parsed_id <= 0 or parsed_id in seen:
                raise WBEditReviewError('ID характеристики повторён или неверен')
            seen.add(parsed_id)
            result.append({'id': parsed_id, 'value': _canonical_input_value(item.get('value'))})
        return result
    if operation == 'add_characteristic':
        if isinstance(char_id, str) and char_id.isascii() and char_id.isdecimal():
            parsed_id = int(char_id)
        elif isinstance(char_id, int) and not isinstance(char_id, bool):
            parsed_id = char_id
        else:
            raise WBEditReviewError('Выберите характеристику')
        if parsed_id <= 0:
            raise WBEditReviewError('ID характеристики должен быть положительным числом')
        parsed_value = value
        if isinstance(parsed_value, str) and parsed_value.strip().startswith('['):
            try:
                parsed_value = json.loads(parsed_value)
            except json.JSONDecodeError as exc:
                raise WBEditReviewError('Список значений имеет неверный формат') from exc
            if not isinstance(parsed_value, list):
                raise WBEditReviewError('Список значений должен быть массивом')
        return [{'id': parsed_id, 'value': _canonical_input_value(parsed_value)}]
    raise WBEditReviewError('Эта операция не использует review характеристик')


def _characteristic_values_by_id(raw_json: Any) -> dict[int, list[str]]:
    rows = _parse_current_characteristics(raw_json)
    return {
        int(item['id']): _values_for_form(item.get('value'))
        for item in rows
        if isinstance(item.get('id'), int) and not isinstance(item.get('id'), bool)
    }


def product_content_fingerprint(product: Product) -> str:
    payload = {
        'id': int(product.id),
        'seller_id': int(product.seller_id),
        'nm_id': product.nm_id,
        'subject_id': product.subject_id,
        'object_name': product.object_name or '',
        'vendor_code': product.vendor_code or '',
        'title': product.title or '',
        'brand': product.brand or '',
        'description': product.description or '',
        'dimensions_json': product.dimensions_json or '',
        'sizes_json': product.sizes_json or '',
        'tags_json': product.tags_json or '',
        'is_active': bool(product.is_active),
        'updated_at': product.updated_at.isoformat() if product.updated_at else None,
        'characteristics_json': product.characteristics_json or '',
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def build_bulk_characteristic_preview(
    products: list[Product],
    *,
    operation: str,
    subject_id: Any,
    change_input: Any = None,
    char_id: Any = None,
    value: Any = None,
) -> dict[str, Any]:
    """Compute an apply preview from local seller facts and local WB schema."""
    if not isinstance(subject_id, int) or isinstance(subject_id, bool) or subject_id <= 0:
        raise WBEditReviewError('Выберите точный предмет WB по subjectID')
    changes = parse_characteristic_changes(
        operation,
        changes=change_input,
        char_id=char_id,
        value=value,
    )
    schema = schema_for_subject(subject_id)
    if not schema['usable']:
        details = '; '.join(item.get('message', '') for item in schema['issues'][:3])
        raise WBEditReviewError(details or 'Схема WB недоступна или устарела')
    validation = validate_wb_characteristics(subject_id, changes)
    if not validation.get('valid'):
        details = '; '.join(item.get('message', '') for item in validation.get('issues', [])[:8])
        raise WBEditReviewError(details or 'Характеристики не прошли проверку по WB-схеме')
    normalized_changes = validation['normalized']
    schema_by_id = {int(row['id']): row for row in schema['characteristics']}

    selected_count = len(products)
    eligible = 0
    diff = []
    skipped = []
    errors = []
    changed_product_ids = set()
    skipped_product_ids = set()
    error_product_ids = set()
    revisions = {str(subject_id): schema['revision']}
    local_fingerprints = {}
    for product in products:
        local_fingerprints[str(product.id)] = product_content_fingerprint(product)
        if product.subject_id != subject_id:
            skipped_product_ids.add(int(product.id))
            skipped.append({
                'product_id': int(product.id),
                'vendor_code': product.vendor_code or '',
                'reason': 'Карточка относится к другому subjectID',
            })
            continue
        if not product.nm_id or product.nm_id <= 0:
            skipped_product_ids.add(int(product.id))
            skipped.append({
                'product_id': int(product.id),
                'vendor_code': product.vendor_code or '',
                'reason': 'Карточка не привязана к WB nmID',
            })
            continue
        eligible += 1
        try:
            current = _characteristic_values_by_id(product.characteristics_json)
        except WBEditReviewError as exc:
            error_product_ids.add(int(product.id))
            errors.append({
                'product_id': int(product.id),
                'vendor_code': product.vendor_code or '',
                'reason': str(exc),
            })
            continue
        for change in normalized_changes:
            field = schema_by_id.get(int(change['id']))
            if not field or not field['usable']:
                error_product_ids.add(int(product.id))
                errors.append({
                    'product_id': int(product.id),
                    'vendor_code': product.vendor_code or '',
                    'reason': 'Поле отсутствует в свежей локальной схеме WB',
                })
                continue
            before = current.get(int(change['id']), [])
            after = _values_for_form(change['value'])

            if field['charc_type'] == 4:
                before_numbers = [
                    _coerce_number(item, field.get('unit_name'))
                    for item in before
                ]
                after_number = _coerce_number(
                    change['value'], field.get('unit_name'),
                )
                value_is_unchanged = (
                    len(before_numbers) == 1
                    and before_numbers[0] is not None
                    and before_numbers[0] == after_number
                )
            elif field.get('dictionary_values'):
                allowed = {
                    str(item).strip().casefold(): str(item).strip()
                    for item in field['dictionary_values']
                }
                before = [allowed.get(item.casefold(), item) for item in before]
                after = [allowed.get(item.casefold(), item) for item in after]
                value_is_unchanged = [item.casefold() for item in before] == [
                    item.casefold() for item in after
                ]
            else:
                value_is_unchanged = before == after
            if operation == 'add_characteristic' and before:
                skipped_product_ids.add(int(product.id))
                skipped.append({
                    'product_id': int(product.id),
                    'vendor_code': product.vendor_code or '',
                    'characteristic': field['name'],
                    'reason': 'Значение уже заполнено; режим «Заполнить отсутствующие» сохраняет его',
                })
                continue
            if value_is_unchanged:
                skipped_product_ids.add(int(product.id))
                skipped.append({
                    'product_id': int(product.id),
                    'vendor_code': product.vendor_code or '',
                    'characteristic': field['name'],
                    'reason': 'Значение уже совпадает',
                })
                continue
            diff.append({
                'product_id': int(product.id),
                'nm_id': int(product.nm_id),
                'vendor_code': product.vendor_code or '',
                'title': product.title or '',
                'subject_id': int(subject_id),
                'subject_name': schema['subject_name'] or f'Предмет WB {subject_id}',
                'characteristic_id': int(change['id']),
                'characteristic': field['name'],
                'before': before,
                'after': after,
            })
            changed_product_ids.add(int(product.id))

    skipped_only_product_ids = skipped_product_ids.difference(
        changed_product_ids, error_product_ids,
    )
    return {
        'channel': 'Wildberries',
        'marketplace_code': 'wb',
        'mode': 'fill_missing' if operation == 'add_characteristic' else 'replace_existing',
        'operation': operation,
        'subject_id': subject_id,
        'subject_name': schema['subject_name'] or f'Предмет WB {subject_id}',
        'selected_count': selected_count,
        'eligible_count': eligible,
        'changed_count': len(changed_product_ids),
        'diff_count': len(diff),
        'skipped_count': len(skipped_only_product_ids),
        'skipped_detail_count': len(skipped),
        'error_count': len(error_product_ids),
        'error_detail_count': len(errors),
        'diff': diff,
        'skipped': skipped,
        'errors': errors,
        'normalized_changes': normalized_changes,
        'schema_revisions': revisions,
        'local_fingerprints': local_fingerprints,
        'created_at': datetime.utcnow().isoformat(),
    }


def review_digest(preview: Mapping[str, Any], product_ids: list[int]) -> str:
    payload = {
        'seller_product_ids': [int(value) for value in product_ids],
        'operation': preview.get('operation'),
        'mode': preview.get('mode'),
        'subject_id': preview.get('subject_id'),
        'normalized_changes': preview.get('normalized_changes'),
        'local_fingerprints': preview.get('local_fingerprints'),
        'schema_revisions': preview.get('schema_revisions'),
        'diff': preview.get('diff'),
        'skipped': preview.get('skipped'),
        'errors': preview.get('errors'),
        'counts': [
            preview.get('selected_count'), preview.get('eligible_count'),
            preview.get('changed_count'), preview.get('skipped_count'),
            preview.get('error_count'),
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def issue_wb_edit_preview_token(
    *,
    secret_key: str,
    user_id: int,
    seller_id: int,
    selection_payload: Mapping[str, Any],
    preview: Mapping[str, Any],
) -> str:
    if preview.get('changed_count', 0) <= 0 or preview.get('error_count', 0) > 0:
        raise WBEditReviewError('Нет безопасных изменений для подтверждения')
    ids = parse_selected_product_ids(selection_payload.get('ids'))
    payload = {
        'v': 1,
        'user_id': int(user_id),
        'seller_id': int(seller_id),
        'marketplace': 'wb',
        'wb_account_id': selection_payload.get('wb_account_id'),
        'product_ids': ids,
        'selection_filter_fingerprint': selection_payload.get('filter_fingerprint'),
        'review_key': secrets.token_hex(32),
        'operation': preview.get('operation'),
        'subject_id': int(preview['subject_id']),
        'normalized_changes': preview['normalized_changes'],
        'local_fingerprints': preview['local_fingerprints'],
        'schema_revisions': preview['schema_revisions'],
        'digest': review_digest(preview, ids),
    }
    return URLSafeTimedSerializer(secret_key, salt=WB_EDIT_REVIEW_SALT).dumps(payload)


def load_wb_edit_preview_token(
    token: Any,
    *,
    secret_key: str,
    user_id: int,
    seller_id: int,
    max_age: int = WB_EDIT_REVIEW_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise WBEditReviewError('Подтверждение отсутствует или повреждено')
    serializer = URLSafeTimedSerializer(secret_key, salt=WB_EDIT_REVIEW_SALT)
    try:
        payload = serializer.loads(token, max_age=max_age)
    except SignatureExpired as exc:
        raise WBEditReviewError('Предпросмотр устарел; проверьте изменения снова') from exc
    except BadSignature as exc:
        raise WBEditReviewError('Подпись предпросмотра неверна') from exc
    if not isinstance(payload, dict) or (
        payload.get('v') != 1
        or payload.get('marketplace') != 'wb'
        or payload.get('user_id') != int(user_id)
        or payload.get('seller_id') != int(seller_id)
    ):
        raise WBEditReviewError('Предпросмотр относится к другому пользователю или продавцу')
    payload['product_ids'] = parse_selected_product_ids(payload.get('product_ids'))
    if not isinstance(payload.get('review_key'), str) or not _WB_REVIEW_KEY_RE.fullmatch(
        payload['review_key'],
    ):
        raise WBEditReviewError('Ключ однократного подтверждения отсутствует или повреждён')
    if not isinstance(payload.get('normalized_changes'), list) or not payload['normalized_changes']:
        raise WBEditReviewError('Предпросмотр не содержит изменений')
    return payload


def find_wb_bulk_review_claim(seller_id: int, review_key: str, *, session=None):
    """Find a seller-owned durable claim from a verified, signed preview nonce."""
    if not isinstance(review_key, str) or not _WB_REVIEW_KEY_RE.fullmatch(review_key):
        raise WBEditReviewError('Ключ однократного подтверждения повреждён')
    query = (session or db.session).query(BulkEditHistory)
    return query.filter_by(
        seller_id=int(seller_id), review_key=review_key,
    ).first()


def commit_wb_bulk_review_claim(history: BulkEditHistory, *, session=None):
    """Durably claim a review key before provider I/O, resolving unique races."""
    review_key = history.review_key
    if not isinstance(review_key, str) or not _WB_REVIEW_KEY_RE.fullmatch(review_key):
        raise WBEditReviewError('Ключ однократного подтверждения повреждён')
    active_session = session or db.session
    active_session.add(history)
    try:
        active_session.commit()
        return history, True
    except IntegrityError:
        active_session.rollback()
        existing = find_wb_bulk_review_claim(
            history.seller_id, review_key, session=active_session,
        )
        if existing is None:
            raise
        return existing, False


def validate_preview_against_current(
    payload: Mapping[str, Any],
    products: list[Product],
) -> dict[str, Any]:
    """Rebuild the local preview and reject any local/schema drift before I/O."""
    if [int(product.id) for product in products] != payload.get('product_ids'):
        raise WBEditReviewError('Точный набор товаров предпросмотра изменился')
    operation = payload.get('operation')
    normalized_changes = payload.get('normalized_changes')
    preview = build_bulk_characteristic_preview(
        products,
        operation=operation,
        subject_id=payload.get('subject_id'),
        change_input=[
            {'char_id': str(row['id']), 'value': row['value']}
            for row in normalized_changes
        ] if operation == 'update_characteristic' else None,
        char_id=(normalized_changes[0]['id'] if operation == 'add_characteristic' else None),
        value=(normalized_changes[0]['value'] if operation == 'add_characteristic' else None),
    )
    expected_digest = review_digest(preview, payload['product_ids'])
    if expected_digest != payload.get('digest'):
        raise WBEditReviewError('Карточка или схема WB изменилась после предпросмотра; проверьте снова')
    if payload.get('local_fingerprints') != preview.get('local_fingerprints'):
        raise WBEditReviewError('Локальные данные товара изменились после предпросмотра')
    if payload.get('schema_revisions') != preview.get('schema_revisions'):
        raise WBEditReviewError('Схема WB изменилась после предпросмотра')
    return preview
