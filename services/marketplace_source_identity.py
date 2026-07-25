"""Deterministic supplier identity encoded in marketplace offer/vendor codes.

The supported suppliers historically used different seller suffixes and
channel prefixes for the same source item.  This module removes only those
known wrappers.  It never compares titles, descriptions, barcodes or other
fuzzy evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Iterable, Optional, Tuple


MAX_IDENTITY_CHARS = 200
KNOWN_SOURCE_CODES = frozenset({"sexoptovik", "andrey"})

_ANDREY_WRAPPED = re.compile(
    r"^id-(?P<serial>[0-9]+)-[0-9]+[zw][0-9]+c[0-9]+a$",
    re.IGNORECASE,
)
_ANDREY_LANE = re.compile(
    r"^[0-9]+[zw][0-9]+c[0-9]+a(?P<external>.+)$",
    re.IGNORECASE,
)
_SEXOPTOVIK_LANE = re.compile(
    r"^[0-9]+z[0-9]+c[0-9]+(?P<lane>s)(?P<external>[0-9]+)$",
    re.IGNORECASE,
)
_VENDOR_LANE = re.compile(
    r"^[0-9]+z[0-9]+c[0-9]+v(?P<vendor>.+)$",
    re.IGNORECASE,
)
_SEXOPTOVIK_WRAPPED = re.compile(
    r"^id-(?P<external>[0-9]+)-[0-9]+$",
    re.IGNORECASE,
)
_TRAILING_SERIAL = re.compile(r"(?P<serial>[0-9]+)$")


@dataclass(frozen=True, order=True)
class SourceIdentityKey:
    """One exact Python-owned source identity.

    ``source_code='*'`` is intentionally limited to exact supplier vendor-code
    values.  It does not mean an unrestricted source or a fuzzy match.
    """

    source_code: str
    kind: str
    value: str

    def to_evidence(self) -> dict:
        return {
            "source_code": self.source_code,
            "kind": self.kind,
            "value": self.value,
        }


@dataclass(frozen=True)
class ParsedSourceIdentity:
    key: SourceIdentityKey
    scheme: str

    def to_evidence(self) -> dict:
        return {
            **self.key.to_evidence(),
            "scheme": self.scheme,
        }


def normalize_identity_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    if (
        not normalized
        or len(normalized) > MAX_IDENTITY_CHARS
        or any(
            unicodedata.category(character) in {"Cc", "Cf"}
            for character in normalized
        )
    ):
        return None
    return normalized


def _fold(value: object) -> Optional[str]:
    normalized = normalize_identity_text(value)
    return normalized.casefold() if normalized else None


def _positive_ascii_integer(value: object) -> Optional[str]:
    normalized = normalize_identity_text(value)
    if not normalized or not normalized.isascii() or not normalized.isdigit():
        return None
    canonical = normalized.lstrip("0") or "0"
    return canonical if canonical != "0" else None


def _andrey_serial(value: object) -> Optional[str]:
    normalized = normalize_identity_text(value)
    match = _TRAILING_SERIAL.search(normalized or "")
    return _positive_ascii_integer(match.group("serial")) if match else None


def _deduplicate(
    values: Iterable[ParsedSourceIdentity],
) -> Tuple[ParsedSourceIdentity, ...]:
    result = {}
    for parsed in values:
        result.setdefault(parsed.key, parsed)
    return tuple(result.values())


def parse_encoded_source_identities(
    value: object,
) -> Tuple[ParsedSourceIdentity, ...]:
    """Parse only known, anchored historical marketplace code formats."""
    normalized = normalize_identity_text(value)
    if not normalized:
        return ()

    match = _ANDREY_WRAPPED.fullmatch(normalized)
    if match:
        serial = _positive_ascii_integer(match.group("serial"))
        return (
            ParsedSourceIdentity(
                SourceIdentityKey("andrey", "serial", serial),
                "andrey_wrapped_serial",
            ),
        ) if serial else ()

    match = _ANDREY_LANE.fullmatch(normalized)
    if match:
        external = normalize_identity_text(match.group("external"))
        folded = _fold(external)
        serial = _andrey_serial(external)
        parsed = []
        if folded:
            parsed.append(ParsedSourceIdentity(
                SourceIdentityKey("andrey", "external_id", folded),
                "andrey_lane_external",
            ))
        if serial:
            parsed.append(ParsedSourceIdentity(
                SourceIdentityKey("andrey", "serial", serial),
                "andrey_lane_serial",
            ))
        return _deduplicate(parsed)

    match = _SEXOPTOVIK_LANE.fullmatch(normalized)
    if match:
        external = _positive_ascii_integer(match.group("external"))
        return (
            ParsedSourceIdentity(
                SourceIdentityKey("sexoptovik", "external_id", external),
                f"sexoptovik_lane_{match.group('lane').casefold()}",
            ),
        ) if external else ()

    match = _VENDOR_LANE.fullmatch(normalized)
    if match:
        vendor = _fold(match.group("vendor"))
        return (
            ParsedSourceIdentity(
                SourceIdentityKey("*", "vendor_code", vendor),
                "exact_vendor_lane_v",
            ),
        ) if vendor else ()

    match = _SEXOPTOVIK_WRAPPED.fullmatch(normalized)
    if match:
        external = _positive_ascii_integer(match.group("external"))
        return (
            ParsedSourceIdentity(
                SourceIdentityKey("sexoptovik", "external_id", external),
                "sexoptovik_wrapped_id",
            ),
        ) if external else ()

    return ()


def source_record_identities(
    *,
    source_code: object,
    external_id: object = None,
    vendor_codes: Iterable[object] = (),
) -> frozenset[SourceIdentityKey]:
    """Build exact keys from one supplier/import source record."""
    normalized_source = _fold(source_code)
    if normalized_source not in KNOWN_SOURCE_CODES:
        return frozenset()

    keys = set()
    if normalized_source == "sexoptovik":
        canonical = _positive_ascii_integer(external_id)
        if canonical:
            keys.add(SourceIdentityKey(
                "sexoptovik",
                "external_id",
                canonical,
            ))
    elif normalized_source == "andrey":
        folded = _fold(external_id)
        serial = _andrey_serial(external_id)
        if folded:
            keys.add(SourceIdentityKey("andrey", "external_id", folded))
        if serial:
            keys.add(SourceIdentityKey("andrey", "serial", serial))

    for raw_vendor in vendor_codes:
        vendor = _fold(raw_vendor)
        if vendor:
            keys.add(SourceIdentityKey("*", "vendor_code", vendor))
            keys.add(SourceIdentityKey(
                normalized_source,
                "vendor_code",
                vendor,
            ))
    return frozenset(keys)


def encoded_record_identities(
    *values: object,
) -> frozenset[SourceIdentityKey]:
    """Build keys from channel-side encoded vendor/offer values."""
    return frozenset(
        parsed.key
        for value in values
        for parsed in parse_encoded_source_identities(value)
    )
