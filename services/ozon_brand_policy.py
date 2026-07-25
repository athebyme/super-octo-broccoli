"""Static Ozon brand restrictions used by every product-card write flow.

The provider restriction list is intentionally exact and deterministic.  Brand
comparison is case/spacing/punctuation tolerant, but it does not use fuzzy or
substring matching: an unrelated seller brand must never be blocked because it
merely contains a short restricted token such as ``ON`` or ``HOT``.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable, Optional


OZON_FORBIDDEN_BRAND_NAMES = (
    "ART STYLE",
    "B-VIBE",
    "BLACKRED",
    "BRAZZERS",
    "CANDY BOY",
    "CANDY GIRL",
    "EGZO",
    "EROLANTA",
    "EROMANTICA",
    "EROTIST",
    "ESKA",
    "FLESHNASH",
    "FLOVETTA",
    "FORTE LOVE POWER",
    "GANZO",
    "GLOSSY",
    "GVIBE",
    "HOT",
    "HOT PRODUCTION",
    "INDEEP",
    "JOS",
    "JUJU",
    "JULEJU",
    "L'EROINA",
    "LE FRIVOLE",
    "LE WAND",
    "LELO",
    "LOVENSE",
    "MAXUS",
    "MEGA GLIDE",
    "MIA-MIA",
    "MIOOCCHI",
    "MOJO",
    "MOY TOY",
    "MY.SIZE",
    "MiNiMi",
    "Natural Instinct",
    "ON",
    "ORION",
    "PORN HUB TOY",
    "PRE PARFUMER",
    "PRIVATE",
    "QUEEN FAIR",
    "Qvibry",
    "REBELTS",
    "ROMP by WOW Tech",
    "RUF",
    "SEXUS",
    "SEXY LIFE",
    "SHIATSU",
    "SPRING",
    "STIMUL 8",
    "SVAKOM",
    "SVAKOM DESIGN USA LIMITED",
    "SWISS NAVY",
    "TIME HEAT",
    "TOM OF FINLAND",
    "TOREX",
    "TOYFA",
    "VIAMAX",
    "VITALIS",
    "WANAME",
    "WE-VIBE",
    "WINYI",
    "WOMANIZER",
    "YOU2TOYS",
    "YOVEE",
    "ЁSKA",
    "ЛАС ИГРАС",
    "Молот Тора",
    "ПИКАНТНЫЕ ШТУЧКИ",
    "РИА ПАНДА",
    "Товары без упаковки",
    "ФЛЕШНАШ",
    "Штучки-дрючки",
    "ЭЛИВЕРТОРГ",
)


def normalize_ozon_brand(value: Any) -> Optional[str]:
    """Return the exact-match key without broad/fuzzy brand semantics."""
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("ё", "е")
    tokens = []
    current = []
    for character in normalized:
        if character.isalnum():
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return " ".join(tokens) or None


_CANONICAL_BY_KEY = {
    normalize_ozon_brand(name): name
    for name in OZON_FORBIDDEN_BRAND_NAMES
}
if (
    None in _CANONICAL_BY_KEY
    or len(_CANONICAL_BY_KEY) != len(OZON_FORBIDDEN_BRAND_NAMES)
):
    raise RuntimeError("Ozon forbidden brand policy contains duplicate keys")

OZON_FORBIDDEN_BRAND_KEYS = frozenset(_CANONICAL_BY_KEY)


def match_forbidden_ozon_brand(value: Any) -> Optional[str]:
    """Return the canonical restricted name for one exact normalized value."""
    key = normalize_ozon_brand(value)
    return _CANONICAL_BY_KEY.get(key)


def first_forbidden_ozon_brand(
    values: Iterable[Any],
    *,
    maximum: int = 100,
) -> Optional[str]:
    """Find the first restricted brand in a bounded candidate collection."""
    if (
        not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum <= 0
    ):
        raise ValueError("maximum must be a positive integer")
    for index, value in enumerate(values):
        if index >= maximum:
            break
        matched = match_forbidden_ozon_brand(value)
        if matched is not None:
            return matched
    return None
