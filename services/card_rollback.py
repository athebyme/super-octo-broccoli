"""Conflict-aware WB card rollback helpers."""

import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Set, Tuple

from services.marketplace_validator import build_wb_characteristic_patch
from services.wb_validators import (
    WBValidationError,
    clean_characteristics_for_update,
    prepare_card_for_characteristic_rollback,
    prepare_card_for_update,
)


def _characteristics_by_id(raw: Any) -> Dict[int, Dict[str, Any]]:
    if not isinstance(raw, list):
        raise WBValidationError(
            'Снимок характеристик имеет неподдерживаемый формат'
        )
    result = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise WBValidationError(
                f'Характеристика #{index + 1} в снимке должна быть объектом'
            )
        if 'value' not in item:
            raise WBValidationError(
                f'Характеристика #{index + 1} в снимке не содержит value'
            )
        try:
            if isinstance(item.get('id'), bool):
                raise ValueError
            charc_id = int(item.get('id'))
        except (TypeError, ValueError):
            raise WBValidationError(
                f'Характеристика #{index + 1} в снимке не содержит числовой id'
            )
        if charc_id in result:
            raise WBValidationError(
                f'Характеристика id={charc_id} продублирована в снимке'
            )
        result[charc_id] = item
    return result


def _characteristic_state(
    mapped: Mapping[int, Mapping[str, Any]],
    charc_id: int,
) -> Tuple[Any, ...]:
    if charc_id not in mapped:
        return ('absent',)
    value = mapped[charc_id].get('value')
    def normalize(candidate):
        if isinstance(candidate, str):
            normalized = candidate.strip().casefold()
            if re.fullmatch(r'-?\d+(?:[.,]\d+)?', normalized):
                try:
                    return ('number', Decimal(normalized.replace(',', '.')).normalize())
                except InvalidOperation:
                    pass
            return ('text', normalized)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return ('number', Decimal(str(candidate)).normalize())
        return candidate

    if isinstance(value, list):
        wire_value = tuple(normalize(item) for item in value)
    else:
        # WB может вернуть ту же характеристику как scalar, хотя снимок
        # хранит одноэлементный list (и наоборот). Сравниваем их канонически.
        wire_value = (normalize(value),)
    return ('present', wire_value)


def _canonical_nested(value: Any) -> Any:
    """Normalize JSON-like dimension values for conflict comparison."""
    if isinstance(value, Mapping):
        return tuple(sorted(
            (str(key), _canonical_nested(item))
            for key, item in value.items()
        ))
    if isinstance(value, list):
        return tuple(_canonical_nested(item) for item in value)
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r'-?\d+(?:[.,]\d+)?', stripped):
            try:
                return ('number', Decimal(
                    stripped.replace(',', '.')).normalize())
            except InvalidOperation:
                pass
        return ('text', stripped)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ('number', Decimal(str(value)).normalize())
    return value


def _canonical_wire_dimensions(value: Any) -> Any:
    if not isinstance(value, Mapping):
        raise WBValidationError('Снимок dimensions должен быть объектом')
    from services.wb_content_payload import normalize_dimensions
    return _canonical_nested(normalize_dimensions(defaults=value))


def classify_wb_card_history_state(
    live_card: Mapping[str, Any],
    snapshot_before: Mapping[str, Any],
    snapshot_after: Mapping[str, Any],
    changed_fields: Iterable[str],
) -> str:
    """Classify fresh WB values as history before/after/conflict.

    Characteristics compare only IDs actually changed by the history row, so
    an unrelated live WB edit does not hide whether this specific update landed.
    """
    if not all(isinstance(value, Mapping) for value in (
        live_card, snapshot_before, snapshot_after,
    )):
        raise WBValidationError('Нельзя сверить WB-карточку с историей')

    before_matches = True
    after_matches = True
    for field in changed_fields or []:
        if field not in snapshot_before or field not in snapshot_after:
            raise WBValidationError(
                f'В истории нет before/after для поля {field}'
            )
        if field == 'vendor_code':
            live_state = live_card.get('vendorCode')
            before_state = snapshot_before[field]
            after_state = snapshot_after[field]
        elif field in ('title', 'description', 'brand'):
            live_state = live_card.get(field)
            before_state = snapshot_before[field]
            after_state = snapshot_after[field]
        elif field == 'dimensions':
            live_state = _canonical_wire_dimensions(
                live_card.get('dimensions') or {})
            before_state = _canonical_wire_dimensions(
                snapshot_before[field] or {})
            after_state = _canonical_wire_dimensions(
                snapshot_after[field] or {})
        elif field == 'characteristics':
            live_by_id = _characteristics_by_id(
                live_card.get('characteristics') or [])
            before_by_id = _characteristics_by_id(snapshot_before[field])
            after_by_id = _characteristics_by_id(snapshot_after[field])
            changed_ids = {
                charc_id
                for charc_id in set(before_by_id) | set(after_by_id)
                if _characteristic_state(before_by_id, charc_id)
                != _characteristic_state(after_by_id, charc_id)
            }
            before_matches = before_matches and all(
                _characteristic_state(live_by_id, charc_id)
                == _characteristic_state(before_by_id, charc_id)
                for charc_id in changed_ids
            )
            after_matches = after_matches and all(
                _characteristic_state(live_by_id, charc_id)
                == _characteristic_state(after_by_id, charc_id)
                for charc_id in changed_ids
            )
            continue
        else:
            raise WBValidationError(
                f'Поле {field} не поддерживает WB reconciliation'
            )

        before_matches = before_matches and live_state == before_state
        after_matches = after_matches and live_state == after_state

    if after_matches and not before_matches:
        return 'after'
    if before_matches and not after_matches:
        return 'before'
    if before_matches and after_matches:
        return 'unchanged'
    return 'conflict'


def prepare_wb_card_history_rollback(
    current_card: Dict[str, Any],
    snapshot_before: Mapping[str, Any],
    snapshot_after: Mapping[str, Any],
    changed_fields: Iterable[str],
    subject_id: int,
    validation_cache: Dict[str, Any] = None,
    acceptable_snapshots: Iterable[Mapping[str, Any]] = None,
) -> Tuple[Dict[str, Any], Set[str]]:
    """Restore one history row over a fresh/current prepared WB card.

    The function compares only fields touched by the history row. A concurrent
    change to any of them aborts the whole row; unrelated live fields survive.
    """
    if not isinstance(current_card, dict):
        raise WBValidationError('Свежая WB-карточка не получена')
    if not isinstance(snapshot_before, Mapping) or not isinstance(
        snapshot_after, Mapping
    ):
        raise WBValidationError('В истории отсутствует before/after snapshot')
    group_snapshots = list(acceptable_snapshots or [])
    if any(not isinstance(item, Mapping) for item in group_snapshots):
        raise WBValidationError('Групповой snapshot должен быть объектом')

    def accepted_field_values(field):
        return [
            item[field]
            for item in group_snapshots
            if field in item
        ]

    restore_updates = {}
    restored_characteristics = None
    removed_characteristic_ids: List[int] = []
    restored_dimensions = None
    reverted_fields: Set[str] = set()
    conflict_fields = []

    for field in changed_fields or []:
        if field not in snapshot_before or field not in snapshot_after:
            raise WBValidationError(
                f'В истории нет полного before/after для поля {field}'
            )

        if field == 'vendor_code':
            accepted_values = [
                snapshot_after[field], snapshot_before[field],
                *accepted_field_values(field),
            ]
            if current_card.get('vendorCode') not in accepted_values:
                conflict_fields.append(field)
            restore_updates['vendorCode'] = snapshot_before[field]
        elif field in ('title', 'description', 'brand'):
            accepted_values = [
                snapshot_after[field], snapshot_before[field],
                *accepted_field_values(field),
            ]
            if current_card.get(field) not in accepted_values:
                conflict_fields.append(field)
            restore_updates[field] = snapshot_before[field]
        elif field == 'characteristics':
            before_by_id = _characteristics_by_id(snapshot_before[field])
            after_by_id = _characteristics_by_id(snapshot_after[field])
            live_by_id = _characteristics_by_id(
                current_card.get('characteristics') or [])
            changed_ids = {
                charc_id
                for charc_id in set(before_by_id) | set(after_by_id)
                if _characteristic_state(before_by_id, charc_id)
                != _characteristic_state(after_by_id, charc_id)
            }
            for charc_id in changed_ids:
                live_state = _characteristic_state(live_by_id, charc_id)
                accepted_states = [
                    _characteristic_state(after_by_id, charc_id),
                    _characteristic_state(before_by_id, charc_id),
                ]
                for group_snapshot in group_snapshots:
                    if 'characteristics' not in group_snapshot:
                        continue
                    accepted_states.append(_characteristic_state(
                        _characteristics_by_id(
                            group_snapshot['characteristics']),
                        charc_id,
                    ))
                if live_state not in accepted_states:
                    conflict_fields.append(f'characteristics[{charc_id}]')

            if changed_ids:
                restore_values = [
                    before_by_id[charc_id]
                    for charc_id in sorted(changed_ids)
                    if charc_id in before_by_id
                ]
                restored_characteristics = build_wb_characteristic_patch(
                    subject_id,
                    restore_values,
                    validation_cache=validation_cache,
                )
                restored_characteristics = clean_characteristics_for_update(
                    restored_characteristics)
                removed_characteristic_ids = sorted(
                    changed_ids - set(before_by_id))
        elif field == 'dimensions':
            live_dimensions = current_card.get('dimensions') or {}
            live_state = _canonical_wire_dimensions(live_dimensions)
            accepted_states = [
                _canonical_wire_dimensions(snapshot_after[field] or {}),
                _canonical_wire_dimensions(snapshot_before[field] or {}),
            ]
            accepted_states.extend(
                _canonical_wire_dimensions(value or {})
                for value in accepted_field_values(field)
            )
            if live_state not in accepted_states:
                conflict_fields.append(field)
            restored_dimensions = deepcopy(snapshot_before[field] or {})
        else:
            raise WBValidationError(
                f'Поле {field} не поддерживает безопасный WB rollback'
            )
        reverted_fields.add(field)

    if not reverted_fields:
        raise WBValidationError('Нет поддерживаемых полей для отката')
    if conflict_fields:
        raise WBValidationError(
            'WB-карточка изменилась после операции; конфликт: '
            + ', '.join(conflict_fields)
        )

    prepared = prepare_card_for_update(current_card, restore_updates)
    if restored_dimensions is not None:
        if restored_dimensions:
            prepared['dimensions'] = restored_dimensions
        else:
            prepared.pop('dimensions', None)
        # Batch boundary normalizes dimensions as well. Normalize here so the
        # prepared payload, local mirror and actual wire payload stay identical.
        from services.wb_content_payload import normalize_update_card_payload
        prepared = normalize_update_card_payload(prepared)
    if restored_characteristics is not None or removed_characteristic_ids:
        prepared = prepare_card_for_characteristic_rollback(
            prepared,
            restored_characteristics or [],
            removed_characteristic_ids,
        )
    return prepared, reverted_fields


def apply_prepared_rollback_to_product(
    product,
    prepared_card: Mapping[str, Any],
    reverted_fields: Iterable[str],
) -> None:
    """Mirror the exact successfully sent WB values into local Product."""
    fields = set(reverted_fields)
    if 'vendor_code' in fields:
        product.vendor_code = prepared_card.get('vendorCode')
    if 'title' in fields:
        product.title = prepared_card.get('title')
    if 'description' in fields:
        product.description = prepared_card.get('description')
    if 'brand' in fields:
        product.brand = prepared_card.get('brand')
    if 'characteristics' in fields:
        product.characteristics_json = json.dumps(
            prepared_card.get('characteristics') or [],
            ensure_ascii=False,
        )
    if 'dimensions' in fields:
        product.dimensions_json = json.dumps(
            prepared_card.get('dimensions') or {},
            ensure_ascii=False,
        )
