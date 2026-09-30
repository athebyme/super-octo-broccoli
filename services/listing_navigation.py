"""Safe return links for the three existing marketplace listing catalog routes.

This allowlist is intentionally narrower than ``is_safe_local_path``: a return
link from a listing workspace may point only at a known catalog endpoint and
may carry only the catalog's existing filters and pagination fields.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit


LISTING_CATALOG_PATHS = frozenset({
    "/marketplaces/listings/",
    "/marketplaces/listings/beta",
    "/marketplaces/listings/classic",
})
LISTING_CATALOG_DEFAULT = "/marketplaces/listings/"
LISTING_RETURN_QUERY_KEYS = (
    "marketplace",
    "account_id",
    "status",
    "link_status",
    "include_unavailable",
    "search",
    "page",
    "per_page",
)

_NORMALIZED_STATUSES = frozenset({
    "active", "moderation", "creating", "error", "archived", "inactive", "unknown",
})
_LINK_STATUSES = frozenset({"linked", "unlinked", "ambiguous"})
_BOOLEAN_VALUES = {
    "1": "1", "true": "1", "on": "1", "yes": "1",
    "0": "0", "false": "0", "off": "0", "no": "0",
}
_MAX_SAFE_JS_INTEGER = 9_007_199_254_740_991
_MAX_SAFE_CATALOG_PAGE = 90_071_992_547_409
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_ENCODED_PATH_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_POSITIVE_INTEGER = re.compile(r"[0-9]+\Z")


def _positive_integer_text(value: str, maximum: int) -> Optional[str]:
    if not _POSITIVE_INTEGER.fullmatch(value):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if parsed < 1 or parsed > maximum:
        return None
    return str(parsed)


def _validate_query_value(key: str, value: str) -> bool:
    if _CONTROL.search(value) or (key != "search" and "\\" in value):
        return False
    if key == "marketplace":
        return not value or value.strip().lower() in {"wb", "ozon"}
    if key == "account_id":
        return not value or _positive_integer_text(value, _MAX_SAFE_JS_INTEGER) is not None
    if key == "status":
        return not value or value in _NORMALIZED_STATUSES
    if key == "link_status":
        return not value or value in _LINK_STATUSES
    if key == "include_unavailable":
        return not value or value.strip().lower() in _BOOLEAN_VALUES
    if key == "search":
        return len(value) <= 200
    if key == "page":
        return not value or _positive_integer_text(value, _MAX_SAFE_CATALOG_PAGE) is not None
    if key == "per_page":
        if not value:
            return True
        normalized = _positive_integer_text(value, 100)
        return normalized is not None
    return False


def validate_listing_return_url(value: Any) -> Optional[str]:
    """Return a canonical safe listing catalog URL, or ``None``.

    A URL is accepted only when its path exactly matches one of the three
    existing listing catalog routes. Encoded path separators are rejected;
    encoded slashes inside the ``search`` value remain data and are safely
    re-encoded when the accepted URL is returned.
    """
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if value != value.strip() or _CONTROL.search(value) or "\\" in value:
        return None
    if _BAD_ESCAPE.search(value):
        return None
    try:
        split = urlsplit(value)
    except ValueError:
        return None
    if split.scheme or split.netloc or split.fragment:
        return None
    if split.path not in LISTING_CATALOG_PATHS or _ENCODED_PATH_SEPARATOR.search(split.path):
        return None

    try:
        pairs = parse_qsl(
            split.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=len(LISTING_RETURN_QUERY_KEYS),
        ) if split.query else []
    except (UnicodeDecodeError, ValueError):
        return None

    seen = set()
    normalized_pairs = []
    for key, query_value in pairs:
        if key not in LISTING_RETURN_QUERY_KEYS or key in seen:
            return None
        seen.add(key)
        if not _validate_query_value(key, query_value):
            return None
        if key == "marketplace" and query_value:
            query_value = query_value.strip().lower()
        elif key == "account_id" and query_value:
            query_value = _positive_integer_text(query_value, _MAX_SAFE_JS_INTEGER)
        elif key == "include_unavailable" and query_value:
            query_value = _BOOLEAN_VALUES[query_value.strip().lower()]
        elif key == "page" and query_value:
            query_value = _positive_integer_text(query_value, _MAX_SAFE_CATALOG_PAGE)
        elif key == "per_page" and query_value:
            query_value = _positive_integer_text(query_value, 100)
        normalized_pairs.append((key, query_value))

    # Canonical encoding removes parser ambiguities while preserving each
    # supported filter value, including blank and explicit false values.
    return split.path + ("?" + urlencode(normalized_pairs, doseq=True) if pairs else "")


def build_listing_catalog_url(
    path: str = LISTING_CATALOG_DEFAULT,
    filters: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build a catalog URL from the existing listing filter structure."""
    if path not in LISTING_CATALOG_PATHS:
        path = LISTING_CATALOG_DEFAULT
    filters = filters or {}
    aliases = {
        "marketplace": ("marketplace", "marketplace_code"),
        "account_id": ("account_id",),
        "status": ("status", "normalized_status"),
        "link_status": ("link_status",),
        "include_unavailable": ("include_unavailable",),
        "search": ("search",),
        "page": ("page",),
        "per_page": ("per_page",),
    }
    pairs = []
    for query_key in LISTING_RETURN_QUERY_KEYS:
        value = None
        for source_key in aliases[query_key]:
            if source_key in filters:
                value = filters[source_key]
                break
        if value is None or value == "":
            continue
        if query_key == "include_unavailable":
            if not isinstance(value, bool):
                value = str(value).lower() in _BOOLEAN_VALUES and _BOOLEAN_VALUES[str(value).lower()] == "1"
            if not value:
                continue
            value = "1"
        else:
            value = str(value)
        if not _validate_query_value(query_key, value):
            continue
        pairs.append((query_key, value))
    raw = path + ("?" + urlencode(pairs) if pairs else "")
    return validate_listing_return_url(raw) or LISTING_CATALOG_DEFAULT


def listing_return_url(
    values: Iterable[str],
    *,
    marketplace_code: Optional[str] = None,
    account_id: Optional[int] = None,
) -> str:
    """Validate a request's return_to values and provide a safe direct fallback."""
    values = list(values)
    if len(values) == 1:
        safe = validate_listing_return_url(values[0])
        if safe:
            return safe
    fallback = {}
    if marketplace_code in {"wb", "ozon"}:
        fallback["marketplace"] = marketplace_code
    if marketplace_code == "ozon" and account_id:
        fallback["account_id"] = account_id
    return build_listing_catalog_url(filters=fallback)


def current_listing_catalog_url(
    path: str,
    raw_query: str,
    *,
    fallback_filters: Optional[Mapping[str, Any]] = None,
) -> str:
    """Keep a current catalog URL only if its route and full query are allowed."""
    raw = path + ("?" + raw_query if raw_query else "")
    safe = validate_listing_return_url(raw)
    if safe:
        return safe
    fallback_path = path if path in LISTING_CATALOG_PATHS else LISTING_CATALOG_DEFAULT
    return build_listing_catalog_url(fallback_path, fallback_filters)
