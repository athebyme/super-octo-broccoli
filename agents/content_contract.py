# -*- coding: utf-8 -*-
"""Typed contract for seller-requested card content fields."""
from __future__ import annotations

import re
from collections.abc import Iterable


CONTENT_FIELD_LIMITS = {
    'title': 60,
    'description': 1000,
}

CONTENT_FIELD_LABELS = {
    'title': 'название',
    'description': 'описание',
}

_CONTENT_FIELD_ALIASES = {
    'title': ('назван', 'заголов', 'наименован'),
    'description': ('описани', 'текст карточ'),
}

_NEGATED_ACTION = (
    r'(?:не\s+(?:надо\s+)?(?:меняй|изменяй|обновляй|переписывай|трогай|'
    r'улучшай|оптимизируй|исправляй))'
)


def extract_explicit_content_fields(text: str) -> list[str]:
    """Return explicitly requested fields, excluding field-level negations."""
    normalized = str(text or '').lower()
    requested = []
    for field, aliases in _CONTENT_FIELD_ALIASES.items():
        matching_aliases = [alias for alias in aliases if alias in normalized]
        if not matching_aliases:
            continue
        alias_pattern = '(?:' + '|'.join(matching_aliases) + r')\w*'
        excluded = any(re.search(pattern, normalized) for pattern in (
            rf'{_NEGATED_ACTION}\s+(?:\w+\s+){{0,2}}{alias_pattern}',
            rf'{alias_pattern}\s+(?:\w+\s+){{0,2}}{_NEGATED_ACTION}',
            rf'{alias_pattern}[\s,;:\u2014-]+(?:но[\s,;:\u2014-]+)?{_NEGATED_ACTION}',
            rf'(?:кроме|за\s+исключением)\s+(?:\w+\s+){{0,2}}{alias_pattern}',
            rf'{alias_pattern}\s+(?:оставь|оставить)\s+(?:как\s+есть|без\s+изменений)',
            rf'{alias_pattern}\s+без\s+изменений',
            rf'без\s+изменени\w*\s+(?:\w+\s+){{0,2}}{alias_pattern}',
            rf'\bне\s+{alias_pattern}',
        ))
        if not excluded:
            requested.append(field)

    only_fields = []
    for field, aliases in _CONTENT_FIELD_ALIASES.items():
        alias_pattern = '(?:' + '|'.join(aliases) + r')\w*'
        if re.search(rf'\bтолько\s+(?:\w+\s+){{0,2}}{alias_pattern}', normalized):
            only_fields.append(field)
    return [field for field in requested if not only_fields or field in only_fields]


def normalize_content_fields(value, default: Iterable[str] = ()) -> list[str]:
    """Normalize an untrusted field mask while preserving canonical order."""
    if isinstance(value, str):
        requested = {value}
    elif isinstance(value, (list, tuple, set)):
        requested = {str(item) for item in value}
    else:
        requested = set(default)
    fields = [field for field in CONTENT_FIELD_LIMITS if field in requested]
    if fields:
        return fields
    default_set = {str(item) for item in default}
    return [field for field in CONTENT_FIELD_LIMITS if field in default_set]


def content_fields_label(fields: Iterable[str]) -> str:
    labels = [CONTENT_FIELD_LABELS[field] for field in fields if field in CONTENT_FIELD_LABELS]
    if len(labels) < 2:
        return labels[0] if labels else 'контент'
    return ' и '.join(labels)
