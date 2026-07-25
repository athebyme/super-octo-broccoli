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

WB_TITLE_HARD_ISSUE_CODES = frozenset({
    'title_too_long',
    'title_brand',
    'title_repetition',
    'title_promo',
})

_TITLE_WORD_RE = re.compile(r'[а-яёa-z0-9]+', re.IGNORECASE)
_TITLE_REPEAT_STOP_WORDS = frozenset({
    'для', 'без', 'при', 'под', 'над', 'или', 'как', 'это', 'его', 'её',
    'ее', 'из', 'на', 'по', 'до', 'от', 'со', 'во', 'за', 'and', 'for',
    'the', 'with', 'from',
})
_TITLE_PROMO_WORDS = frozenset({
    'топ', 'хит', 'бестселлер', 'новинка', 'акция', 'распродажа',
})
_TITLE_PROMO_PREFIXES = (
    'чарующ', 'идеальн', 'роскошн', 'шикарн', 'эксклюзивн', 'уникальн',
    'премиальн',
)

_CONTENT_FIELD_ALIASES = {
    'title': ('назван', 'заголов', 'наименован'),
    'description': ('описани', 'текст карточ'),
}

_NEGATED_ACTION = (
    r'(?:не\s+(?:надо\s+)?(?:меняй|изменяй|обновляй|переписывай|трогай|'
    r'улучшай|оптимизируй|исправляй))'
)


def _title_words(value: str) -> list[str]:
    return [
        word.casefold()
        for word in _TITLE_WORD_RE.findall(str(value or ''))
    ]


def _contains_word_phrase(text_words: list[str], phrase_words: list[str]) -> bool:
    if not phrase_words or len(phrase_words) > len(text_words):
        return False
    width = len(phrase_words)
    return any(
        text_words[index:index + width] == phrase_words
        for index in range(len(text_words) - width + 1)
    )


def analyze_wb_title(title: str, brand: str = '') -> list[dict[str, object]]:
    """Return deterministic WB-index defects that must block a generated title.

    Semantic synonym/detail checks still require the content writer.  This
    helper owns the objective subset that Python can prove: the 60-character
    limit, an exact brand phrase, repeated meaningful words and promotional
    filler.
    """
    raw_title = str(title or '').strip()
    if not raw_title:
        return [{
            'code': 'title_missing',
            'message': 'Нет наименования товара',
        }]

    issues: list[dict[str, object]] = []
    title_words = _title_words(raw_title)

    if len(raw_title) > CONTENT_FIELD_LIMITS['title']:
        issues.append({
            'code': 'title_too_long',
            'message': (
                'Сократите наименование до 60 символов и перенесите '
                'подробности в описание'
            ),
        })

    brand_value = str(brand or '').strip()
    brand_words = _title_words(brand_value)
    if brand_words and _contains_word_phrase(title_words, brand_words):
        issues.append({
            'code': 'title_brand',
            'message': (
                f'Удалите бренд «{brand_value[:80]}» из наименования; '
                'он должен быть только в поле «Бренд»'
            ),
        })

    repeat_counts: dict[str, int] = {}
    for word in title_words:
        if (
            len(word) < 3
            or word.isdigit()
            or word in _TITLE_REPEAT_STOP_WORDS
        ):
            continue
        repeat_counts[word] = repeat_counts.get(word, 0) + 1
    repeated = sorted(
        word for word, count in repeat_counts.items() if count >= 2
    )
    if repeated:
        issues.append({
            'code': 'title_repetition',
            'message': (
                'Удалите повторяющиеся слова из наименования: '
                + ', '.join(f'«{word}»' for word in repeated[:5])
            ),
            'words': repeated[:5],
        })

    promo_words = sorted({
        word for word in title_words
        if (
            word in _TITLE_PROMO_WORDS
            or any(word.startswith(prefix) for prefix in _TITLE_PROMO_PREFIXES)
        )
    })
    if re.search(r'(?iu)(?:^|\W)№\s*1(?:\W|$)', raw_title):
        promo_words.append('№1')
    if promo_words:
        promo_words = list(dict.fromkeys(promo_words))
        issues.append({
            'code': 'title_promo',
            'message': (
                'Удалите рекламные и лишние слова из наименования: '
                + ', '.join(f'«{word}»' for word in promo_words[:5])
                + '; подтверждённые детали перенесите в описание'
            ),
            'words': promo_words[:5],
        })

    return issues


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
