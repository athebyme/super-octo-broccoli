"""
Marketplace Validator
Обеспечивает валидацию данных товаров в соответствии со схемой API маркетплейса.
Проверяет типы, обязательность, maxCount, словарные ограничения.
"""
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, List, Optional, Mapping

from models import (
    Marketplace, MarketplaceCategory, MarketplaceCategoryCharacteristic,
    MarketplaceDirectory, SupplierProduct,
)

logger = logging.getLogger('marketplace_validator')

REFERENCE_MAX_AGE_HOURS = 48
EFFECTIVE_CONSTRAINT_VALUES_LIMIT = 40


class WBCharacteristicValidationError(ValueError):
    """Характеристики нельзя отправлять в WB по данным admin-cache."""

    def __init__(self, result: Dict[str, Any]):
        self.result = result
        issues = result.get('issues') or []
        details = '; '.join(issue.get('message', '') for issue in issues[:8])
        super().__init__(
            'Характеристики WB не прошли обязательную проверку по актуальной '
            'схеме и справочникам WB/админки: '
            f'{details or "неизвестная ошибка"}'
        )


_DIRECTORY_BY_CHARACTERISTIC = {
    'цвет': 'colors',
    'цвет товара': 'colors',
    'пол': 'kinds',
    'пол товара': 'kinds',
    'страна производства': 'countries',
    'сезон': 'seasons',
    'ставка ндс': 'vat',
}

def _strict_positive_integer(value: Any) -> int:
    """Return a positive JSON integer without bool/float/string coercion."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError('value must be a positive integer')
    return value


def _normalized_label(value: Any) -> str:
    text = str(value or '').strip().casefold().replace('ё', 'е')
    text = re.sub(r'[^\w\s]+', ' ', text, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', text).strip()


def _normalized_dictionary_value(value: Any) -> str:
    """Ключ для точного case-insensitive сравнения значения словаря."""
    return str(value or '').strip().casefold()


def _directory_type_for_characteristic(name: str) -> Optional[str]:
    normalized = _normalized_label(name)
    return _DIRECTORY_BY_CHARACTERISTIC.get(normalized)


def _is_fresh(synced_at: Optional[datetime]) -> bool:
    return bool(
        synced_at
        and synced_at >= datetime.utcnow() - timedelta(hours=REFERENCE_MAX_AGE_HOURS)
    )


def _reference_issues(
    marketplace: Marketplace,
    category: MarketplaceCategory,
) -> List[Dict[str, Any]]:
    issues = []
    if not marketplace.is_active:
        issues.append(_issue(
            'marketplace_inactive',
            'Маркетплейс WB отключён в админке',
        ))
    if marketplace.categories_sync_status != 'success' or not _is_fresh(
        marketplace.categories_synced_at
    ):
        issues.append(_issue(
            'category_reference_stale',
            'Справочник категорий WB устарел или синхронизирован с ошибкой',
        ))
    if not category.is_enabled:
        issues.append(_issue(
            'category_disabled',
            f'Категория «{category.subject_name or category.subject_id}» '
            'отключена в админке',
        ))
    if not category.is_leaf:
        issues.append(_issue(
            'category_not_leaf',
            f'Категория «{category.subject_name or category.subject_id}» '
            'не является конечным предметом WB',
        ))
    if not category.is_available:
        issues.append(_issue(
            'category_unavailable',
            f'Категория «{category.subject_name or category.subject_id}» '
            'больше недоступна в WB',
        ))
    if (
        category.characteristics_sync_status != 'success'
        or not _is_fresh(category.characteristics_synced_at)
    ):
        issues.append(_issue(
            'schema_stale',
            f'Схема характеристик категории '
            f'«{category.subject_name or category.subject_id}» устарела '
            'или синхронизирована с ошибкой',
        ))
    return issues


def _dictionary_values(raw: Any) -> List[str]:
    """Извлечь канонические значения из форматов WB admin-cache."""
    if not isinstance(raw, list):
        return []
    values = []
    for item in raw:
        if isinstance(item, dict):
            value = item.get('name') or item.get('value')
            if value is None and isinstance(item.get('id'), str):
                value = item['id']
        else:
            value = item
        value = str(value).strip() if value is not None else ''
        if value:
            values.append(value)
    return values


def _issue(code: str, message: str, **extra) -> Dict[str, Any]:
    result = {'code': code, 'message': message}
    result.update({key: value for key, value in extra.items() if value is not None})
    return result


def _resolve_wb_schema(
    subject_id: Any,
    marketplace_code: str = 'wb',
    validation_cache: Optional[Dict[str, Any]] = None,
):
    try:
        subject_id = _strict_positive_integer(subject_id)
    except ValueError:
        return None, None, None

    cache_key = (marketplace_code, subject_id)
    schema_cache = (
        validation_cache.setdefault('schemas', {})
        if validation_cache is not None else None
    )
    if schema_cache is not None and cache_key in schema_cache:
        return schema_cache[cache_key]

    marketplace = Marketplace.query.filter_by(code=marketplace_code).first()
    if not marketplace:
        resolved = (None, None, None)
        if schema_cache is not None:
            schema_cache[cache_key] = resolved
        return resolved
    category = MarketplaceCategory.query.filter_by(
        marketplace_id=marketplace.id,
        subject_id=subject_id,
    ).first()
    if not category:
        resolved = (marketplace, None, None)
        if schema_cache is not None:
            schema_cache[cache_key] = resolved
        return resolved
    available_characteristics = MarketplaceCategoryCharacteristic.query.filter_by(
        marketplace_id=marketplace.id,
        category_id=category.id,
        is_available=True,
    ).all()
    # Required characteristics cannot be disabled through current admin UI,
    # but old databases may still contain required=True/is_enabled=False.
    # Such a row must never disappear from create/removal validation.
    characteristics = [
        charc for charc in available_characteristics
        if charc.is_enabled or charc.required
    ]
    resolved = (marketplace, category, characteristics)
    if schema_cache is not None:
        schema_cache[cache_key] = resolved
    return resolved


def _allowed_values_for_characteristic(
    marketplace: Marketplace,
    characteristic: MarketplaceCategoryCharacteristic,
    validation_cache: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[List[str]], Optional[str], Optional[Dict[str, Any]]]:
    """Вернуть (values, source, issue); None values означает свободное поле."""
    cache_key = (marketplace.id, characteristic.id)
    allowed_cache = (
        validation_cache.setdefault('allowed_values', {})
        if validation_cache is not None else None
    )
    if allowed_cache is not None and cache_key in allowed_cache:
        return allowed_cache[cache_key]

    def resolved(values=None, source=None, issue=None):
        result = (values, source, issue)
        if allowed_cache is not None:
            allowed_cache[cache_key] = result
        return result

    normalized_name = _normalized_label(characteristic.name)
    directory_type = _directory_type_for_characteristic(characteristic.name)

    # Documented global WB directories are authoritative even when an old
    # installation still has a historical category-level list. Schema sync
    # clears those legacy lists, but validation must already be safe before the
    # next sweep. Category dictionaries remain authoritative for TNVED,
    # upstream schema dictionaries and deliberate admin policies.
    uses_global_directory = bool(directory_type)
    if characteristic.dictionary_json and not uses_global_directory:
        try:
            raw = json.loads(characteristic.dictionary_json)
        except (json.JSONDecodeError, TypeError):
            raw = None
        values = _dictionary_values(raw)
        if values:
            return resolved(values, 'category')
        if not isinstance(raw, list):
            return resolved(issue=_issue(
                'dictionary_not_synced',
                f'«{characteristic.name}»: словарь категории повреждён; '
                'синхронизируйте характеристики WB в админке',
                charc_id=characteristic.charc_id,
                name=characteristic.name,
            ))
        # Explicit [] falls through to a documented global directory or free
        # text. It is not evidence that WB requires a missing local allowlist.

    if normalized_name in ('тнвэд', 'тнвэд код', 'тн вэд', 'тн вэд код'):
        return resolved(issue=_issue(
            'category_scoped_directory_not_synced',
            f'«{characteristic.name}»: ТН ВЭД зависит от категории; '
            'используйте словарь характеристики конкретного предмета WB',
            charc_id=characteristic.charc_id,
            name=characteristic.name,
        ))
    if directory_type:
        directory_cache = (
            validation_cache.setdefault('directories', {})
            if validation_cache is not None else None
        )
        directory_key = (marketplace.id, directory_type)
        if directory_cache is not None and directory_key in directory_cache:
            directory = directory_cache[directory_key]
        else:
            directory = MarketplaceDirectory.query.filter_by(
                marketplace_id=marketplace.id,
                directory_type=directory_type,
            ).first()
            if directory_cache is not None:
                # Cache misses as well: an absent reference must not cause one
                # SELECT per characteristic in a request-sized batch.
                directory_cache[directory_key] = directory
        if (
            not directory
            or directory.sync_status != 'success'
            or not _is_fresh(directory.synced_at)
        ):
            return resolved(issue=_issue(
                'directory_stale',
                f'«{characteristic.name}»: справочник {directory_type} устарел '
                'или синхронизирован с ошибкой',
                charc_id=characteristic.charc_id,
                name=characteristic.name,
            ))
        try:
            raw = json.loads(directory.data_json) if directory and directory.data_json else None
        except (json.JSONDecodeError, TypeError):
            raw = None
        values = _dictionary_values(raw)
        if not values:
            return resolved(issue=_issue(
                'dictionary_not_synced',
                f'«{characteristic.name}»: справочник {directory_type} не синхронизирован '
                'в админке',
                charc_id=characteristic.charc_id,
                name=characteristic.name,
            ))
        return resolved(values, directory_type)

    return resolved()


def prime_wb_characteristic_directory_cache(
    marketplace: Marketplace,
    characteristics: List[MarketplaceCategoryCharacteristic],
    validation_cache: Optional[Dict[str, Any]] = None,
) -> None:
    """Load every global directory needed by a characteristic batch once."""
    if validation_cache is None or not marketplace or not characteristics:
        return

    directory_types = {
        directory_type
        for characteristic in characteristics
        for directory_type in [
            _directory_type_for_characteristic(characteristic.name),
        ]
        if directory_type
    }
    cache = validation_cache.setdefault('directories', {})
    missing_types = sorted(
        directory_type for directory_type in directory_types
        if (marketplace.id, directory_type) not in cache
    )
    if not missing_types:
        return

    rows = MarketplaceDirectory.query.filter(
        MarketplaceDirectory.marketplace_id == marketplace.id,
        MarketplaceDirectory.directory_type.in_(missing_types),
    ).all()
    by_type = {row.directory_type: row for row in rows}
    for directory_type in missing_types:
        # Missing rows are authoritative for this request and are cached as
        # None so validation remains fail-closed without another DB lookup.
        cache[(marketplace.id, directory_type)] = by_type.get(directory_type)


def get_wb_characteristic_constraint(
    marketplace: Marketplace,
    characteristic: MarketplaceCategoryCharacteristic,
    validation_cache: Optional[Dict[str, Any]] = None,
    values_limit: int = EFFECTIVE_CONSTRAINT_VALUES_LIMIT,
) -> Dict[str, Any]:
    """Return the compact effective constraint used by the write validator."""
    values_limit = max(1, min(int(values_limit), 100))
    allowed, source, issue = _allowed_values_for_characteristic(
        marketplace,
        characteristic,
        validation_cache,
    )

    normalized_name = _normalized_label(characteristic.name)
    directory_type = _directory_type_for_characteristic(characteristic.name)
    is_tnved = normalized_name in (
        'тнвэд', 'тнвэд код', 'тн вэд', 'тн вэд код',
    )
    # A TNVED allowlist is stored on the characteristic because the official
    # directory is subject-scoped. Expose its effective origin rather than the
    # generic storage label used by other category dictionaries.
    if is_tnved and allowed is not None:
        source = 'tnved'
    if source is None:
        if is_tnved:
            source = 'tnved'
        elif directory_type:
            source = directory_type
        elif characteristic.dictionary_json:
            source = 'category'
        else:
            source = 'free_text'

    dictionary_source = getattr(
        characteristic, 'dictionary_source', None,
    ) or ('admin' if characteristic.dictionary_json else 'none')
    dictionary_synced_at = getattr(
        characteristic, 'dictionary_synced_at', None,
    )
    dictionary_version = getattr(
        characteristic, 'dictionary_version', 0,
    ) or 0
    if directory_type:
        directory_key = (marketplace.id, directory_type)
        directory_cache = (
            validation_cache.get('directories', {})
            if validation_cache is not None else {}
        )
        directory = directory_cache.get(directory_key)
        if directory_key not in directory_cache:
            directory = MarketplaceDirectory.query.filter_by(
                marketplace_id=marketplace.id,
                directory_type=directory_type,
            ).first()
        # Global WB directories are the effective source even if this schema
        # row still carries a historical manual list that validation ignores.
        dictionary_source = 'wb_directory'
        dictionary_synced_at = directory.synced_at if directory else None
        dictionary_version = int(directory.version or 0) if directory else 0

    canonical_values = list(allowed or [])
    result = {
        'source': source,
        'constrained': source != 'free_text',
        'usable': issue is None,
        'count': len(canonical_values),
        'values': canonical_values[:values_limit],
        'truncated': len(canonical_values) > values_limit,
        'dictionary_source': dictionary_source,
        'dictionary_synced_at': (
            dictionary_synced_at.isoformat() if dictionary_synced_at else None
        ),
        'dictionary_version': dictionary_version,
    }
    if issue:
        result['issue'] = {
            key: issue[key]
            for key in ('code', 'message', 'charc_id', 'name')
            if issue.get(key) is not None
        }
    return result


def search_wb_characteristic_values(
    marketplace: Marketplace,
    characteristic: MarketplaceCategoryCharacteristic,
    query: str,
    *,
    validation_cache: Optional[Dict[str, Any]] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """Search one effective allowlist and return canonical values only.

    Search is deterministic and read-only. It may normalize punctuation for
    ranking, but the returned value is always the exact canonical string that
    the write validator will accept.
    """
    query = str(query or '').strip()
    if not query or len(query) > 120:
        raise ValueError('query must contain 1..120 characters')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError('limit must be an integer from 1 to 50')
    allowed, source, issue = _allowed_values_for_characteristic(
        marketplace, characteristic, validation_cache,
    )
    if issue:
        return {
            'usable': False,
            'constrained': True,
            'source': source,
            'count': 0,
            'values': [],
            'issue': issue,
        }
    if allowed is None:
        return {
            'usable': True,
            'constrained': False,
            'source': 'free_text',
            'count': 0,
            'values': [],
        }

    folded = query.casefold()
    normalized = _normalized_label(query)
    query_tokens = set(normalized.split())
    ranked = []
    for index, value in enumerate(allowed):
        value_folded = value.casefold()
        value_normalized = _normalized_label(value)
        value_tokens = set(value_normalized.split())
        if value_folded == folded:
            rank = 0
        elif value_normalized == normalized:
            rank = 1
        elif value_folded.startswith(folded) or value_normalized.startswith(normalized):
            rank = 2
        elif folded in value_folded or normalized in value_normalized:
            rank = 3
        elif query_tokens and query_tokens <= value_tokens:
            rank = 4
        else:
            continue
        ranked.append((rank, len(value), index, value))
    ranked.sort(key=lambda item: item[:3])
    values = [item[3] for item in ranked[:limit]]
    return {
        'usable': True,
        'constrained': True,
        'source': source,
        'count': len(values),
        'values': values,
        'truncated': len(ranked) > limit,
    }


def _coerce_number(value: Any, unit_name: Optional[str]) -> Optional[Any]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        if len(value) != 1:
            return None
        return _coerce_number(value[0], unit_name)
    if not isinstance(value, str):
        return None
    suffix = re.escape((unit_name or '').strip())
    suffix_pattern = rf'(?:\s*{suffix})?' if suffix else ''
    match = re.fullmatch(
        rf'\s*(-?\d+(?:[.,]\d+)?)\s*{suffix_pattern}\s*', value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    number = float(match.group(1).replace(',', '.'))
    return int(number) if number.is_integer() else number


def validate_wb_characteristics(
    subject_id: Any,
    characteristics: Any,
    marketplace_code: str = 'wb',
    validation_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Строго проверить WB characteristic patch по admin schema/dictionaries.

    В отличие от AI-валидаторов здесь нет fuzzy/substring автозамены: допустимо
    только точное case-insensitive совпадение с канонизацией регистра.
    """
    validation_cache = validation_cache if validation_cache is not None else {}
    marketplace, category, schema = _resolve_wb_schema(
        subject_id, marketplace_code, validation_cache)
    result = {
        'valid': False,
        'subject_id': subject_id,
        'normalized': [],
        'issues': [],
        'directories_used': [],
        'schema_synced_at': (
            category.characteristics_synced_at.isoformat()
            if category and category.characteristics_synced_at else None
        ),
    }

    if not marketplace:
        result['issues'].append(_issue(
            'marketplace_not_found',
            'Маркетплейс WB не настроен в админке',
        ))
        return result
    if not category:
        result['issues'].append(_issue(
            'category_not_found',
            f'Категория WB subject_id={subject_id} не найдена в админке',
        ))
        return result
    reference_issues = _reference_issues(marketplace, category)
    if reference_issues:
        result['issues'].extend(reference_issues)
        return result
    if not schema:
        result['issues'].append(_issue(
            'schema_not_synced',
            f'Характеристики категории «{category.subject_name or subject_id}» '
            'не синхронизированы в админке',
        ))
        return result
    if not isinstance(characteristics, list):
        result['issues'].append(_issue(
            'invalid_payload',
            'Характеристики WB должны быть массивом {id, value}',
        ))
        return result

    schema_by_id = {int(charc.charc_id): charc for charc in schema}
    seen_ids = set()
    for index, item in enumerate(characteristics):
        if not isinstance(item, dict):
            result['issues'].append(_issue(
                'invalid_payload',
                f'Характеристика #{index + 1} должна быть объектом',
            ))
            continue
        try:
            charc_id = _strict_positive_integer(item.get('id'))
        except ValueError:
            result['issues'].append(_issue(
                'invalid_payload',
                f'Характеристика #{index + 1}: отсутствует числовой id',
            ))
            continue
        charc = schema_by_id.get(charc_id)
        if not charc:
            result['issues'].append(_issue(
                'unknown_characteristic',
                f'Характеристика id={charc_id} отсутствует в WB-схеме категории',
                charc_id=charc_id,
            ))
            continue
        if charc_id in seen_ids:
            result['issues'].append(_issue(
                'duplicate_characteristic',
                f'«{charc.name}» передана более одного раза',
                charc_id=charc_id,
                name=charc.name,
            ))
            continue
        seen_ids.add(charc_id)

        value = item.get('value')
        if charc.charc_type == 4:
            normalized_value = _coerce_number(value, charc.unit_name)
            if normalized_value is None:
                result['issues'].append(_issue(
                    'type_mismatch',
                    f'«{charc.name}»: требуется число{f" ({charc.unit_name})" if charc.unit_name else ""}',
                    charc_id=charc_id,
                    name=charc.name,
                    value=value,
                ))
                continue
        elif charc.charc_type == 1:
            raw_values = value if isinstance(value, list) else [value]
            normalized_value = [
                str(raw).strip() for raw in raw_values
                if raw is not None and str(raw).strip()
            ]
            if not normalized_value:
                result['issues'].append(_issue(
                    'type_mismatch',
                    f'«{charc.name}»: требуется непустой массив строк',
                    charc_id=charc_id,
                    name=charc.name,
                    value=value,
                ))
                continue
            if charc.max_count and len(normalized_value) > charc.max_count:
                result['issues'].append(_issue(
                    'too_many_values',
                    f'«{charc.name}»: передано {len(normalized_value)}, максимум {charc.max_count}',
                    charc_id=charc_id,
                    name=charc.name,
                    value=value,
                ))
                continue

            allowed, source, dictionary_issue = _allowed_values_for_characteristic(
                marketplace, charc, validation_cache)
            if dictionary_issue:
                result['issues'].append(dictionary_issue)
                continue
            if allowed is not None:
                allowed_map = {
                    _normalized_dictionary_value(candidate): candidate
                    for candidate in allowed
                }
                canonical = []
                invalid = None
                for candidate in normalized_value:
                    matched = allowed_map.get(
                        _normalized_dictionary_value(candidate))
                    if matched is None:
                        invalid = candidate
                        break
                    canonical.append(matched)
                if invalid is not None:
                    result['issues'].append(_issue(
                        'value_not_allowed',
                        f'«{charc.name}»: значение «{invalid}» отсутствует в словаре WB',
                        charc_id=charc_id,
                        name=charc.name,
                        value=invalid,
                        allowed_sample=allowed[:10],
                    ))
                    continue
                normalized_value = canonical
                if source and source not in result['directories_used']:
                    result['directories_used'].append(source)
        elif charc.charc_type == 0:
            result['issues'].append(_issue(
                'unused_characteristic',
                f'«{charc.name}» отключена в текущей WB-схеме',
                charc_id=charc_id,
                name=charc.name,
            ))
            continue
        else:
            result['issues'].append(_issue(
                'unsupported_type',
                f'«{charc.name}»: неподдерживаемый WB-тип {charc.charc_type}',
                charc_id=charc_id,
                name=charc.name,
            ))
            continue

        result['normalized'].append({'id': charc_id, 'value': normalized_value})

    result['valid'] = not result['issues']
    return result


def build_wb_characteristic_patch(
    subject_id: Any,
    values: Any,
    marketplace_code: str = 'wb',
    validation_cache: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Смаппить supplier values (name/id formats) и строго провалидировать."""
    validation_cache = validation_cache if validation_cache is not None else {}
    if isinstance(values, list) and all(
        isinstance(item, dict) and item.get('id') is not None for item in values
    ):
        result = validate_wb_characteristics(
            subject_id, values, marketplace_code, validation_cache)
        if not result['valid']:
            raise WBCharacteristicValidationError(result)
        return result['normalized']

    marketplace, category, schema = _resolve_wb_schema(
        subject_id, marketplace_code, validation_cache)
    if not marketplace or not category or not schema:
        result = validate_wb_characteristics(
            subject_id, [], marketplace_code, validation_cache)
        raise WBCharacteristicValidationError(result)

    if isinstance(values, dict):
        named_items = list(values.items())
    elif isinstance(values, list):
        if any(not isinstance(item, dict) for item in values):
            raise WBCharacteristicValidationError({
                'valid': False,
                'subject_id': subject_id,
                'normalized': [],
                'issues': [_issue(
                    'invalid_payload',
                    'Каждая характеристика поставщика должна быть объектом',
                )],
            })
        named_items = [
            (item.get('name'), item.get('value'))
            for item in values
        ]
    else:
        result = {
            'valid': False, 'normalized': [],
            'issues': [_issue('invalid_payload', 'Неподдерживаемый формат характеристик поставщика')],
        }
        raise WBCharacteristicValidationError(result)

    by_name = {_normalized_label(charc.name): charc for charc in schema}
    patch = []
    mapping_issues = []
    for raw_name, value in named_items:
        normalized_name = _normalized_label(raw_name)
        charc = by_name.get(normalized_name)
        if not charc:
            mapping_issues.append(_issue(
                'unknown_characteristic',
                f'«{raw_name}» отсутствует в WB-схеме категории',
                name=raw_name,
                value=value,
            ))
            continue
        patch.append({'id': int(charc.charc_id), 'value': value})

    if mapping_issues:
        raise WBCharacteristicValidationError({
            'valid': False,
            'subject_id': subject_id,
            'normalized': [],
            'issues': mapping_issues,
        })

    result = validate_wb_characteristics(
        subject_id, patch, marketplace_code, validation_cache)
    if not result['valid']:
        raise WBCharacteristicValidationError(result)
    return result['normalized']


def validate_wb_full_card_dictionary_values(
    subject_id: Any,
    characteristics: Any,
    marketplace_code: str = 'wb',
    validation_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate dictionary-bound values already present in a fresh full card.

    WB may contain old/legacy characteristics that are no longer in the active
    admin schema. Those untouched unknown IDs remain authoritative. Every known
    characteristic backed by an explicit category/global dictionary (for
    example an admin allowlist or the official gender directory) must still
    pass the current reference before any full-card replacement is sent.
    """
    validation_cache = validation_cache if validation_cache is not None else {}
    marketplace, category, schema = _resolve_wb_schema(
        subject_id, marketplace_code, validation_cache)
    base = validate_wb_characteristics(
        subject_id, [], marketplace_code, validation_cache)
    if not base['valid']:
        return base
    if not isinstance(characteristics, list):
        return {
            **base,
            'valid': False,
            'issues': [_issue(
                'invalid_payload',
                'Полная WB-карточка должна содержать массив characteristics',
            )],
        }

    schema_by_id = {int(charc.charc_id): charc for charc in schema or []}
    constrained = []
    for item in characteristics:
        if not isinstance(item, dict):
            continue
        try:
            charc = schema_by_id.get(int(item.get('id')))
        except (TypeError, ValueError):
            charc = None
        if not charc:
            continue
        normalized_name = _normalized_label(charc.name)
        is_constrained = bool(
            charc.dictionary_json
            or _directory_type_for_characteristic(charc.name)
            or normalized_name in (
                'тнвэд', 'тнвэд код', 'тн вэд', 'тн вэд код',
            )
        )
        if is_constrained:
            constrained.append(item)

    if not constrained:
        return base
    return validate_wb_characteristics(
        subject_id,
        constrained,
        marketplace_code,
        validation_cache,
    )


def require_wb_characteristic_ids(
    subject_id: Any,
    characteristic_ids: Any,
    marketplace_code: str = 'wb',
    validation_cache: Optional[Dict[str, Any]] = None,
) -> List[int]:
    """Validate reference freshness and existence for value-less removals."""
    validation_cache = validation_cache if validation_cache is not None else {}
    base_result = validate_wb_characteristics(
        subject_id,
        [],
        marketplace_code,
        validation_cache,
    )
    if not base_result['valid']:
        raise WBCharacteristicValidationError(base_result)

    try:
        normalized_ids = sorted({
            _strict_positive_integer(value) for value in characteristic_ids
        })
    except (TypeError, ValueError) as exc:
        raise WBCharacteristicValidationError({
            'valid': False,
            'subject_id': subject_id,
            'normalized': [],
            'issues': [_issue(
                'invalid_payload',
                'ID удаляемых характеристик должны быть числовыми',
            )],
        }) from exc

    _marketplace, _category, schema = _resolve_wb_schema(
        subject_id,
        marketplace_code,
        validation_cache,
    )
    schema_by_id = {int(charc.charc_id): charc for charc in schema or []}
    available_ids = set(schema_by_id)
    from services.wb_content_payload import PACKED_WEIGHT_CHARC_IDS
    unknown_ids = [
        value for value in normalized_ids
        if value not in available_ids and value not in PACKED_WEIGHT_CHARC_IDS
    ]
    if unknown_ids:
        raise WBCharacteristicValidationError({
            'valid': False,
            'subject_id': subject_id,
            'normalized': [],
            'issues': [
                _issue(
                    'unknown_characteristic',
                    f'Характеристика id={value} отсутствует в WB-схеме категории',
                    charc_id=value,
                )
                for value in unknown_ids
            ],
        })
    required_ids = [
        value for value in normalized_ids
        if value in schema_by_id and schema_by_id[value].required
    ]
    if required_ids:
        raise WBCharacteristicValidationError({
            'valid': False,
            'subject_id': subject_id,
            'normalized': [],
            'issues': [
                _issue(
                    'required_characteristic_removal',
                    f'«{schema_by_id[value].name}»: обязательную характеристику '
                    'нельзя удалить при откате',
                    charc_id=value,
                    name=schema_by_id[value].name,
                )
                for value in required_ids
            ],
        })
    return normalized_ids


def build_wb_create_characteristics(
    subject_id: Any,
    values: Any,
    marketplace_code: str = 'wb',
    validation_cache: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Strict create validation, including admin-required characteristics."""
    validation_cache = validation_cache if validation_cache is not None else {}
    normalized = build_wb_characteristic_patch(
        subject_id,
        values,
        marketplace_code,
        validation_cache,
    )
    _marketplace, category, schema = _resolve_wb_schema(
        subject_id,
        marketplace_code,
        validation_cache,
    )
    provided_ids = {int(item['id']) for item in normalized}
    missing = [
        charc for charc in schema or []
        if charc.required and int(charc.charc_id) not in provided_ids
    ]
    if missing:
        raise WBCharacteristicValidationError({
            'valid': False,
            'subject_id': subject_id,
            'normalized': normalized,
            'issues': [
                _issue(
                    'required_characteristic_missing',
                    f'«{charc.name}»: обязательная характеристика не заполнена',
                    charc_id=charc.charc_id,
                    name=charc.name,
                )
                for charc in missing
            ],
            'schema_synced_at': (
                category.characteristics_synced_at.isoformat()
                if category and category.characteristics_synced_at else None
            ),
        })
    return normalized


_SUPPLIER_MATERIAL_CHARACTERISTIC_NAMES = (
    'материал изделия',
    'материал товара',
    'основной материал',
    'материал',
    'состав',
    'состав изделия',
    'состав материала',
)
_SUPPLIER_GENDER_CHARACTERISTIC_NAMES = ('пол', 'пол товара')


def build_wb_supplier_characteristic_patch(
    subject_id: Any,
    values: Any,
    *,
    materials: Any = None,
    gender: Any = None,
    marketplace_code: str = 'wb',
    validation_cache: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Собрать единый patch из реальных полей ImportedProduct.

    ``materials`` и ``gender`` хранятся отдельно от ``characteristics`` у
    поставщиков (в том числе у Андрея). Они сопоставляются только с точными
    semantic aliases текущей WB-схемы; fuzzy/substring matching запрещён.
    """
    validation_cache = validation_cache if validation_cache is not None else {}
    marketplace, category, schema = _resolve_wb_schema(
        subject_id, marketplace_code, validation_cache)
    if not marketplace or not category or not schema:
        result = validate_wb_characteristics(
            subject_id, [], marketplace_code, validation_cache)
        raise WBCharacteristicValidationError(result)

    if values in (None, ''):
        base_patch = []
    else:
        base_patch = build_wb_characteristic_patch(
            subject_id,
            values,
            marketplace_code,
            validation_cache,
        )

    by_name = {_normalized_label(charc.name): charc for charc in schema}
    combined = {int(item['id']): dict(item) for item in base_patch}
    issues = []

    def add_semantic_value(raw_value, aliases, label):
        if raw_value in (None, '', [], {}):
            return
        charc = next((by_name.get(alias) for alias in aliases if by_name.get(alias)), None)
        if not charc:
            issues.append(_issue(
                'semantic_characteristic_not_found',
                f'«{label}» поставщика не сопоставлен: в WB-схеме категории '
                'нет поддерживаемой точной характеристики',
                name=label,
                value=raw_value,
            ))
            return
        combined[int(charc.charc_id)] = {
            'id': int(charc.charc_id),
            'value': raw_value,
        }

    add_semantic_value(
        materials, _SUPPLIER_MATERIAL_CHARACTERISTIC_NAMES, 'Материал')
    add_semantic_value(gender, _SUPPLIER_GENDER_CHARACTERISTIC_NAMES, 'Пол')

    if issues:
        raise WBCharacteristicValidationError({
            'valid': False,
            'subject_id': subject_id,
            'normalized': [],
            'issues': issues,
        })

    result = validate_wb_characteristics(
        subject_id,
        list(combined.values()),
        marketplace_code,
        validation_cache,
    )
    if not result['valid']:
        raise WBCharacteristicValidationError(result)
    return result['normalized']


def merge_wb_characteristics(
    existing: Any,
    patch: Any,
    subject_id: Any = None,
    marketplace_code: str = 'wb',
    validation_cache: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Заменить значения по charc_id, не стирая остальные характеристики."""
    if isinstance(existing, Mapping):
        # Старые локальные карточки могли хранить {name: value}. Не теряем эти
        # поля при первом ID-based обновлении. Если известна категория, точно
        # связываем имена с charc_id, чтобы patch обновил поле без дубля.
        schema_by_name = {}
        if subject_id is not None:
            _, _, schema = _resolve_wb_schema(
                subject_id,
                marketplace_code,
                validation_cache,
            )
            schema_by_name = {
                _normalized_label(charc.name): charc for charc in (schema or [])
            }
        merged = []
        for name, value in existing.items():
            charc = schema_by_name.get(_normalized_label(name))
            if charc:
                merged.append({
                    'id': int(charc.charc_id),
                    'name': charc.name,
                    'value': value,
                })
            else:
                merged.append({'name': str(name), 'value': value})
    elif isinstance(existing, list):
        merged = [dict(item) for item in existing if isinstance(item, dict)]
    else:
        merged = []
    positions = {}
    for index, item in enumerate(merged):
        try:
            positions[int(item.get('id'))] = index
        except (TypeError, ValueError):
            continue
    for item in patch or []:
        if not isinstance(item, dict):
            continue
        try:
            charc_id = int(item.get('id'))
        except (TypeError, ValueError):
            continue
        if charc_id in positions:
            normalized = dict(merged[positions[charc_id]])
            normalized.update({'id': charc_id, 'value': item.get('value')})
            merged[positions[charc_id]] = normalized
        else:
            normalized = {'id': charc_id, 'value': item.get('value')}
            positions[charc_id] = len(merged)
            merged.append(normalized)
    return merged


class MarketplaceValidator:
    """Validates product data against marketplace characteristic schema"""

    @classmethod
    def validate_product_for_marketplace(
        cls, product: SupplierProduct, marketplace_id: int
    ) -> Dict[str, Any]:
        """
        Validates ALL marketplace-specific fields for a product based on mapped category.
        Stores validation results in product.marketplace_fields_json.
        """
        if not product.wb_subject_id:
            return {
                "valid": False, "status": "invalid",
                "errors": ["No marketplace category mapped"],
                "fill_pct": 0.0
            }

        # Get category + characteristics schema
        category = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace_id,
            subject_id=product.wb_subject_id
        ).first()

        if not category:
            return {
                "valid": False, "status": "invalid",
                "errors": ["Category not found in marketplace"],
                "fill_pct": 0.0
            }

        marketplace = Marketplace.query.get(marketplace_id)
        reference_issues = (
            _reference_issues(marketplace, category) if marketplace else [
                _issue('marketplace_not_found', 'Marketplace not found')
            ]
        )
        if reference_issues:
            return {
                "valid": False,
                "status": "invalid",
                "errors": [issue['message'] for issue in reference_issues],
                "reference_issues": reference_issues,
                "fill_pct": 0.0,
            }

        schema_charcs = MarketplaceCategoryCharacteristic.query.filter_by(
            category_id=category.id,
            is_enabled=True,
            is_available=True,
        ).all()

        if not schema_charcs:
            return {
                "valid": False, "status": "invalid",
                "errors": ["Schema not synced for this category"],
                "fill_pct": 0.0
            }

        # Get parsed data (from AI or manual)
        parsed_data = product.get_ai_parsed_data()
        # Also try marketplace-specific data
        mp_data = product.get_ai_marketplace_data()
        # Merge: marketplace data takes priority
        merged_data = {**parsed_data, **mp_data}

        validation_results = []
        errors = []
        total_enabled = len(schema_charcs)
        filled_count = 0
        required_filled = 0
        required_total = 0
        filterable_filled = 0
        filterable_total = 0

        for charc in schema_charcs:
            if charc.charc_type == 0:
                # Unused type — skip from counts
                total_enabled -= 1
                continue

            if charc.required:
                required_total += 1
            if getattr(charc, 'has_filter', False):
                filterable_total += 1

            value = merged_data.get(charc.name)
            is_valid, err_msg, coerced_val = cls.validate_single_characteristic(value, charc)

            has_value = (
                coerced_val is not None
                and coerced_val != ''
                and coerced_val != []
            )

            if is_valid and has_value:
                filled_count += 1
                if charc.required:
                    required_filled += 1
                if getattr(charc, 'has_filter', False):
                    filterable_filled += 1

            # Невалидное переданное значение является ошибкой независимо от
            # required: optional означает «можно не заполнять», а не «можно
            # отправить произвольный текст».
            if not is_valid and (
                charc.required or value not in (None, '', [])
            ):
                errors.append(f"{charc.name}: {err_msg}")

            validation_results.append({
                "id": charc.charc_id,
                "name": charc.name,
                "value": coerced_val,
                "type": charc.charc_type,
                "unit": charc.unit_name,
                "valid": is_valid,
                "error": err_msg,
                "required": charc.required,
                "has_filter": getattr(charc, 'has_filter', False),
                "is_variable": getattr(charc, 'is_variable', False),
                "has_value": has_value
            })

        # Fill percentage based on ALL enabled characteristics (not just required)
        fill_pct = (filled_count / total_enabled * 100) if total_enabled > 0 else 0.0

        # Status logic
        if errors:
            status = "invalid"
        elif required_total > 0 and required_filled < required_total:
            status = "partial"
        elif filled_count < total_enabled:
            status = "partial"
        else:
            status = "valid"

        result = {
            "subject_id": product.wb_subject_id,
            "category_name": category.subject_name,
            "characteristics": validation_results,
            "validation_status": status,
            "validation_errors": errors,
            "fill_percentage": round(fill_pct, 2),
            "stats": {
                "total_enabled": total_enabled,
                "filled": filled_count,
                "required_total": required_total,
                "required_filled": required_filled,
                "filterable_total": filterable_total,
                "filterable_filled": filterable_filled
            }
        }

        # Persist flat field-value map (displayable in admin form)
        flat_fields = {}
        for vr in validation_results:
            if not vr.get('valid'):
                continue
            val = vr.get('value')
            if val is None:
                continue
            if isinstance(val, list):
                flat_fields[vr['name']] = '; '.join(str(v) for v in val) if val else ''
            else:
                flat_fields[vr['name']] = val
        product.marketplace_fields_json = json.dumps(flat_fields, ensure_ascii=False)
        product.marketplace_validation_status = status
        product.marketplace_fill_pct = fill_pct

        return result

    @classmethod
    def validate_single_characteristic(
        cls, value: Any, charc: MarketplaceCategoryCharacteristic
    ) -> Tuple[bool, Optional[str], Any]:
        """
        Validate and optionally coerce a single characteristic value.
        Returns (is_valid, error_msg, coerced_value)
        """
        # Empty check
        if value is None or value == "" or value == []:
            if charc.required:
                return False, "Обязательное поле не заполнено", None
            return True, None, None

        # charcType=0: unused
        if charc.charc_type == 0:
            return True, None, None

        # charcType=4: number
        if charc.charc_type == 4:
            return cls._validate_number(value, charc)

        # charcType=1: string array
        if charc.charc_type == 1:
            return cls._validate_string_array(value, charc)

        return False, f"Неподдерживаемый WB-тип {charc.charc_type}", value

    @classmethod
    def _validate_number(
        cls, value: Any, charc: MarketplaceCategoryCharacteristic
    ) -> Tuple[bool, Optional[str], Any]:
        """Validate and coerce to number."""
        # Already a number
        if isinstance(value, (int, float)):
            return True, None, value

        # Array — extract first element
        if isinstance(value, list):
            if not value:
                if charc.required:
                    return False, "Пустой массив для числового поля", None
                return True, None, None
            return cls._validate_number(value[0], charc)

        # String — parse number
        if isinstance(value, str):
            cleaned = value.replace(',', '.').strip()
            # Remove units: "15 см" -> "15", "3.5 г" -> "3.5"
            match = re.search(r'([-]?[\d]+[.]?[\d]*)', cleaned)
            if match:
                try:
                    num = float(match.group(1))
                    return True, None, int(num) if num == int(num) else num
                except ValueError:
                    pass
            return False, f"Не удалось извлечь число из '{value}'", value

        return False, f"Неожиданный тип {type(value).__name__} для числового поля", value

    @classmethod
    def _validate_string_array(
        cls, value: Any, charc: MarketplaceCategoryCharacteristic
    ) -> Tuple[bool, Optional[str], Any]:
        """Validate and coerce to string array with dictionary matching."""
        # Normalize to array
        if isinstance(value, str):
            arr = [value]
        elif isinstance(value, list):
            arr = [str(v).strip() for v in value if v is not None and str(v).strip()]
        elif isinstance(value, (int, float)):
            arr = [str(value)]
        else:
            return False, f"Неожиданный тип {type(value).__name__}", value

        if not arr:
            if charc.required:
                return False, "Пустой массив для обязательного поля", []
            return True, None, []

        # Max count is a hard WB contract, not a lossy normalization rule.
        if charc.max_count > 0 and len(arr) > charc.max_count:
            return False, f"Передано {len(arr)} значений, максимум {charc.max_count}", arr

        # Используем тот же admin-cache контракт, что и apply-path: category
        # dictionary или типизированный глобальный справочник (например kinds).
        category = getattr(charc, 'category', None)
        marketplace = getattr(category, 'marketplace', None) if category else None
        allowed = None
        if marketplace:
            allowed, _, dictionary_issue = _allowed_values_for_characteristic(
                marketplace, charc)
            if dictionary_issue:
                return False, dictionary_issue['message'], arr
        elif charc.dictionary_json:
            try:
                allowed = _dictionary_values(json.loads(charc.dictionary_json))
            except (json.JSONDecodeError, TypeError):
                allowed = []
            if not allowed:
                return False, 'Словарь характеристики повреждён или пуст', arr

        if allowed is not None:
            allowed_map = {
                _normalized_dictionary_value(value): value
                for value in allowed
            }
            validated_arr = []
            for value in arr:
                canonical = allowed_map.get(
                    _normalized_dictionary_value(value))
                if canonical is None:
                    return (
                        False,
                        f"Значение '{value}' не найдено в справочнике",
                        arr,
                    )
                validated_arr.append(canonical)
            return True, None, validated_arr

        return True, None, arr

    @classmethod
    def validate_characteristic_live(
        cls, value: Any, charc_id: int, category_id: int
    ) -> Dict[str, Any]:
        """
        Live validation of a single characteristic value.
        Used by the frontend for instant feedback.
        Requires category_id because charc_id is not globally unique.
        """
        charc = MarketplaceCategoryCharacteristic.query.filter_by(
            category_id=category_id,
            charc_id=charc_id
        ).first()

        if not charc:
            return {"valid": False, "error": "Characteristic not found"}

        is_valid, err_msg, coerced = cls.validate_single_characteristic(value, charc)
        return {
            "valid": is_valid,
            "error": err_msg,
            "coerced_value": coerced,
            "charc_name": charc.name,
            "charc_type": charc.charc_type
        }
