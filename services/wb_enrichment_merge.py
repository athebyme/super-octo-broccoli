# -*- coding: utf-8 -*-
"""Pure merge policy and bounded image matching for WB enrichment.

Supplier enrichment is intentionally conservative:

* an existing WB characteristic is replaced only when the candidate contains
  strictly more normalized information and retains every semantic token of
  the live value;
* a missing/empty characteristic may be filled;
* title/description use the same conservative superset rule, brand identity is
  only filled when absent, and existing non-empty dimensions are immutable;
* the live WB gallery is never replaced by enrichment.  Supplier images are
  perceptually matched and only missing images are appended after the live
  gallery.

The helpers in this module do not read ORM objects and never perform provider
writes.  They return bounded decision receipts which the caller persists next
to the exact before/after card snapshots.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse

from PIL import Image, ImageOps


MERGE_POLICY_VERSION = "wb-enrichment-preserve-v4"
PHOTO_MATCH_THRESHOLD = 88
MAX_DECISION_PREVIEW_CHARS = 300
MAX_REMOTE_IMAGE_BYTES = 8 * 1024 * 1024
WB_MEDIA_HOST_SUFFIXES = (".wbbasket.ru", ".wildberries.ru", ".wb.ru")

_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)$")
_NUMERIC_FACT_RE = re.compile(
    r"(?<![\w])(?:[+-]?\d+(?:[.,]\d+)?|[+-]?[.,]\d+)(?![\w])",
    re.UNICODE,
)
_NEGATION_TOKENS = frozenset({
    "не", "без", "нет", "кроме", "no", "not", "without",
})
_NEGATION_PREFIXES = (
    "отсутств", "исключ", "запрещ", "невозмож", "недоступ",
    "absent", "exclude", "except", "forbidden", "unavailable", "cannot",
)


class WBEnrichmentMergeError(ValueError):
    """A safe merge plan cannot be built from the observed live state."""


def _normalized_scalar(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return "" if value is None else str(value).casefold()
    if isinstance(value, (int, float)):
        return str(value)
    return _SPACE_RE.sub(" ", str(value)).strip().casefold()


def canonical_characteristic_value(value: Any) -> tuple[str, ...]:
    """Return a stable, order-preserving semantic form for comparison."""
    raw_items: Iterable[Any]
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    elif isinstance(value, Mapping):
        raw_items = (
            f"{key}: {json.dumps(item, ensure_ascii=False, sort_keys=True)}"
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    else:
        raw_items = (value,)

    normalized = []
    seen = set()
    for item in raw_items:
        text = _normalized_scalar(item)
        if text and text not in seen:
            seen.add(text)
            normalized.append(text)
    return tuple(normalized)


def characteristic_completeness(value: Any) -> int:
    """A deterministic information-length score used by the strict policy."""
    normalized = canonical_characteristic_value(value)
    return sum(len(item) for item in normalized) + max(0, len(normalized) - 1)


def _semantic_tokens(value: Any) -> frozenset[str]:
    """Return conservative Unicode word evidence for superset checks."""
    return frozenset(
        token
        for item in canonical_characteristic_value(value)
        for token in _TOKEN_RE.findall(item)
        if token
    )


def _is_numeric_value(value: Any) -> bool:
    """Numbers are atomic facts: a longer spelling is not more complete."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    canonical = canonical_characteristic_value(value)
    return bool(canonical) and all(_NUMERIC_RE.fullmatch(item) for item in canonical)


def _contains_numeric_fact(value: Any) -> bool:
    """Treat a number with units as an atomic fact too.

    A longer phrase must not turn ``10 см`` into ``10 см, 20 см`` merely
    because it retains the first token.  Numeric marketplace facts need an
    explicit reviewed edit; enrichment only fills them when the live field is
    absent/empty.
    """
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    return any(
        _NUMERIC_FACT_RE.search(item)
        for item in canonical_characteristic_value(value)
    )


def _strictly_more_complete_superset(existing: Any, candidate: Any) -> bool:
    """Whether candidate adds information without dropping live evidence."""
    if (
        _is_numeric_value(existing)
        or _is_numeric_value(candidate)
        or _contains_numeric_fact(existing)
        or _contains_numeric_fact(candidate)
    ):
        return False
    existing_tokens = _semantic_tokens(existing)
    candidate_tokens = _semantic_tokens(candidate)
    if not existing_tokens or not existing_tokens.issubset(candidate_tokens):
        return False
    if candidate_tokens == existing_tokens:
        return False
    # A syntactic superset can still reverse the meaning ("красный" →
    # "не красный"). Newly introduced negation is therefore never treated as
    # more complete evidence.
    def negation_markers(tokens: frozenset[str]) -> frozenset[str]:
        return frozenset(
            token for token in tokens
            if token in _NEGATION_TOKENS
            or any(token.startswith(prefix) for prefix in _NEGATION_PREFIXES)
        )

    if negation_markers(candidate_tokens) - negation_markers(existing_tokens):
        return False
    return characteristic_completeness(candidate) > characteristic_completeness(
        existing
    )


def _audit_value(value: Any) -> dict[str, Any]:
    canonical = canonical_characteristic_value(value)
    rendered = ", ".join(canonical)
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "preview": rendered[:MAX_DECISION_PREVIEW_CHARS],
        "preview_truncated": len(rendered) > MAX_DECISION_PREVIEW_CHARS,
        "fingerprint": hashlib.sha256(encoded).hexdigest(),
        "completeness": characteristic_completeness(value),
    }


def plan_scalar_field_merge(
    field: str,
    existing: Any,
    candidate: Any,
) -> dict[str, Any]:
    """Plan one enrichment text field without replacing richer manual data."""
    if field not in {"title", "description", "brand"}:
        raise WBEnrichmentMergeError(f"Поле {field} не поддерживает smart merge")

    existing_audit = _audit_value(existing)
    candidate_audit = _audit_value(candidate)
    same = canonical_characteristic_value(existing) == (
        canonical_characteristic_value(candidate)
    )
    contains_existing = _semantic_tokens(existing).issubset(
        _semantic_tokens(candidate)
    )

    if candidate_audit["completeness"] <= 0:
        decision = "skipped_empty"
        accepted = False
    elif existing_audit["completeness"] <= 0:
        decision = "filled_missing"
        accepted = True
    elif same:
        decision = "unchanged"
        accepted = False
    elif field == "brand":
        # Brand is identity, not prose. A longer different brand must never win.
        decision = "preserved_existing"
        accepted = False
    elif _strictly_more_complete_superset(existing, candidate):
        decision = "replaced_more_complete"
        accepted = True
    else:
        decision = "preserved_existing"
        accepted = False

    return {
        "decision": decision,
        "accepted": accepted,
        "existing": existing_audit,
        "candidate": candidate_audit,
        "existing_length": existing_audit["completeness"],
        "candidate_length": candidate_audit["completeness"],
        "semantic_contains_existing": bool(contains_existing),
    }


def plan_dimensions_merge(existing: Any, candidate: Any) -> dict[str, Any]:
    """Fill only missing dimension keys; never reinterpret an existing fact."""
    if existing is None:
        existing = {}
    if not isinstance(existing, Mapping):
        raise WBEnrichmentMergeError("Свежие dimensions WB должны быть объектом")
    if not isinstance(candidate, Mapping):
        raise WBEnrichmentMergeError("Dimensions поставщика должны быть объектом")

    accepted_patch: dict[str, Any] = {}
    items = []
    counts = {
        "filled_missing": 0,
        "preserved_existing": 0,
        "unchanged": 0,
        "skipped_empty": 0,
    }

    def usable_dimension_value(key: str, value: Any) -> bool:
        if key not in {"length", "width", "height", "weightBrutto"}:
            return _audit_value(value)["completeness"] > 0
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            return False
        return math.isfinite(float(value)) and float(value) > 0

    for raw_key, incoming in candidate.items():
        key = str(raw_key)
        incoming_audit = _audit_value(incoming)
        present = raw_key in existing or key in existing
        current = existing.get(raw_key, existing.get(key))
        current_audit = _audit_value(current)
        incoming_usable = usable_dimension_value(key, incoming)
        current_usable = usable_dimension_value(key, current)
        if not incoming_usable:
            decision = "skipped_empty"
        elif not present or not current_usable:
            decision = "filled_missing"
            accepted_patch[key] = incoming
        elif canonical_characteristic_value(current) == (
            canonical_characteristic_value(incoming)
        ):
            decision = "unchanged"
        else:
            decision = "preserved_existing"
        counts[decision] += 1
        items.append({
            "key": key[:100],
            "decision": decision,
            "existing": current_audit,
            "candidate": incoming_audit,
        })
    return {
        "accepted_patch": accepted_patch,
        "counts": counts,
        "items": items,
    }


def plan_characteristic_merge(
    existing: Any,
    candidate_patch: Any,
) -> dict[str, Any]:
    """Select only new or strictly more complete characteristic values.

    ``existing`` is the fresh full WB array and ``candidate_patch`` is the
    already schema/dictionary-validated supplier patch.  No characteristic is
    removed by this policy.
    """
    if not isinstance(existing, list):
        raise WBEnrichmentMergeError(
            "Свежая WB-карточка не содержит массив characteristics"
        )
    if not isinstance(candidate_patch, list):
        raise WBEnrichmentMergeError(
            "Патч характеристик поставщика должен быть массивом"
        )

    existing_by_id: dict[int, Mapping[str, Any]] = {}
    for index, item in enumerate(existing):
        if not isinstance(item, Mapping):
            raise WBEnrichmentMergeError(
                f"Характеристика WB #{index + 1} должна быть объектом"
            )
        charc_id = item.get("id")
        if (
            not isinstance(charc_id, int)
            or isinstance(charc_id, bool)
            or charc_id <= 0
        ):
            raise WBEnrichmentMergeError(
                f"Характеристика WB #{index + 1} не содержит "
                "typed positive integer id"
            )
        if charc_id in existing_by_id:
            raise WBEnrichmentMergeError(
                f"Характеристика WB id={charc_id} продублирована"
            )
        existing_by_id[charc_id] = item

    accepted = []
    decisions = []
    seen_candidate_ids = set()
    counts = {
        "added": 0,
        "replaced_more_complete": 0,
        "preserved_existing": 0,
        "unchanged": 0,
        "skipped_empty": 0,
    }

    for index, item in enumerate(candidate_patch):
        if not isinstance(item, Mapping) or "value" not in item:
            raise WBEnrichmentMergeError(
                f"Характеристика поставщика #{index + 1} некорректна"
            )
        charc_id = item.get("id")
        if (
            not isinstance(charc_id, int)
            or isinstance(charc_id, bool)
            or charc_id <= 0
        ):
            raise WBEnrichmentMergeError(
                f"Характеристика поставщика #{index + 1} не содержит typed id"
            )
        if charc_id in seen_candidate_ids:
            raise WBEnrichmentMergeError(
                f"Характеристика поставщика id={charc_id} продублирована"
            )
        seen_candidate_ids.add(charc_id)

        incoming_value = item.get("value")
        incoming_audit = _audit_value(incoming_value)
        current = existing_by_id.get(charc_id)
        current_value = current.get("value") if current is not None else None
        current_audit = _audit_value(current_value)
        name = (
            (current or {}).get("name")
            or item.get("name")
            or f"id {charc_id}"
        )

        if incoming_audit["completeness"] <= 0:
            decision = "skipped_empty"
        elif current is None or current_audit["completeness"] <= 0:
            decision = "added"
            accepted.append({"id": charc_id, "value": incoming_value})
        elif canonical_characteristic_value(current_value) == (
            canonical_characteristic_value(incoming_value)
        ):
            decision = "unchanged"
        elif _strictly_more_complete_superset(current_value, incoming_value):
            decision = "replaced_more_complete"
            accepted.append({"id": charc_id, "value": incoming_value})
        else:
            decision = "preserved_existing"

        counts[decision] += 1
        decisions.append({
            "id": charc_id,
            "name": str(name)[:200],
            "decision": decision,
            "existing": current_audit,
            "candidate": incoming_audit,
            "semantic_contains_existing": _semantic_tokens(
                current_value
            ).issubset(_semantic_tokens(incoming_value)),
        })

    return {
        "policy": MERGE_POLICY_VERSION,
        "accepted_patch": accepted,
        "counts": counts,
        "items": decisions,
    }


def _image_fingerprint(data: bytes) -> dict[str, Any]:
    if not data:
        raise WBEnrichmentMergeError("Пустые байты изображения")
    try:
        with Image.open(BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            width, height = image.size
            if width < 32 or height < 32:
                raise WBEnrichmentMergeError("Изображение слишком маленькое")
            normalized = image.resize((128, 128), Image.Resampling.LANCZOS)
            pixel_sha = hashlib.sha256(normalized.tobytes()).hexdigest()

            gray9 = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            values9 = list(gray9.get_flattened_data())
            dhash = 0
            for y in range(8):
                for x in range(8):
                    dhash = (dhash << 1) | int(
                        values9[y * 9 + x] > values9[y * 9 + x + 1]
                    )

            gray8 = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
            values8 = list(gray8.get_flattened_data())
            average = sum(values8) / len(values8)
            ahash = 0
            for value in values8:
                ahash = (ahash << 1) | int(value > average)
    except WBEnrichmentMergeError:
        raise
    except Exception as exc:
        raise WBEnrichmentMergeError("Изображение не декодируется") from exc
    return {
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "pixel_sha": pixel_sha,
        "dhash": dhash,
        "ahash": ahash,
        "width": width,
        "height": height,
    }


def fingerprint_local_photo(path: Any) -> dict[str, Any]:
    photo_path = Path(path)
    # Read at most one byte beyond the policy limit.  A supplier/cache file is
    # external input and must not be allowed to allocate unbounded memory just
    # because it has a local path.
    with photo_path.open("rb") as source:
        data = source.read(MAX_REMOTE_IMAGE_BYTES + 1)
    if len(data) > MAX_REMOTE_IMAGE_BYTES:
        raise WBEnrichmentMergeError("Локальное фото превышает лимит сравнения")
    return _image_fingerprint(data)


def fingerprint_remote_photo(url: str) -> dict[str, Any]:
    # Reuse the repository's redirect-aware SSRF-safe bounded downloader.
    from services.image_lab_service import download_public_image

    return _image_fingerprint(download_public_image(
        url,
        max_bytes=MAX_REMOTE_IMAGE_BYTES,
        timeout=(3.0, 8.0),
    ))


def photo_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    if left.get("pixel_sha") == right.get("pixel_sha"):
        return 100
    try:
        d_distance = (int(left["dhash"]) ^ int(right["dhash"])).bit_count()
        a_distance = (int(left["ahash"]) ^ int(right["ahash"])).bit_count()
    except (KeyError, TypeError, ValueError):
        return 0
    if d_distance <= 4 and a_distance <= 5:
        return 97
    if d_distance <= 8 and a_distance <= 9:
        return 90
    if d_distance <= 12 and a_distance <= 14:
        return 78
    return max(0, round(55 - (d_distance + a_distance) * 1.5))


def _wb_https_photo_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 2_000:
        return None
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    if not any(
        hostname == suffix[1:] or hostname.endswith(suffix)
        for suffix in WB_MEDIA_HOST_SUFFIXES
    ):
        return None
    return value


def live_wb_photo_urls(card: Mapping[str, Any]) -> list[str]:
    """Extract one trusted thumbnail URL per exact live gallery position."""
    photos = card.get("photos") if isinstance(card, Mapping) else None
    if not isinstance(photos, list):
        raise WBEnrichmentMergeError("WB не вернул полный массив live-фото")
    result = []
    for index, item in enumerate(photos):
        if isinstance(item, Mapping):
            raw_url = (
                # Supplier photo cache normalizes every candidate to a
                # 1200x1200 canvas.  WB's portrait ``tm``/``big`` variants
                # distort that canvas enough to create false perceptual-hash
                # conflicts after an otherwise successful append.  The
                # provider's square rendition preserves the approved pixels
                # for matching and delayed reconciliation.
                item.get("square")
                or item.get("c516x688")
                or item.get("big")
                or item.get("c246x328")
                or item.get("tm")
            )
        else:
            raw_url = item
        url = _wb_https_photo_url(raw_url)
        if not url:
            raise WBEnrichmentMergeError(
                f"WB вернул некорректный URL live-фото #{index + 1}"
            )
        result.append(url)
    return result


def live_wb_photo_match_urls(card: Mapping[str, Any]) -> list[str]:
    """Return one trusted comparison URL per exact live gallery position.

    WB's ``tm``/portrait renditions can crop a square source heavily enough
    that a perceptual hash no longer recognises the same image.  The square
    rendition preserves the source composition much better, so matching uses
    it when available while snapshots continue to store the canonical bounded
    URL returned by :func:`live_wb_photo_urls`.
    """
    canonical_urls = live_wb_photo_urls(card)
    photos = card.get("photos")
    result = []
    for index, canonical_url in enumerate(canonical_urls):
        item = photos[index] if isinstance(photos, list) else None
        square_url = (
            _wb_https_photo_url(item.get("square"))
            if isinstance(item, Mapping) else None
        )
        result.append(square_url or canonical_url)
    return result


def live_wb_photo_legacy_urls(card: Mapping[str, Any]) -> list[str]:
    """Return the portrait-first URL used by v3 photo receipts.

    This exists only for read compatibility with durable receipts created
    before square-first comparison.  New planning must use
    :func:`live_wb_photo_match_urls`.
    """
    photos = card.get("photos") if isinstance(card, Mapping) else None
    if not isinstance(photos, list):
        raise WBEnrichmentMergeError("WB не вернул полный массив live-фото")
    result = []
    for index, item in enumerate(photos):
        if isinstance(item, Mapping):
            raw_url = (
                item.get("tm")
                or item.get("c246x328")
                or item.get("big")
                or item.get("c516x688")
                or item.get("square")
            )
        else:
            raw_url = item
        url = _wb_https_photo_url(raw_url)
        if not url:
            raise WBEnrichmentMergeError(
                f"WB вернул некорректный legacy URL live-фото #{index + 1}"
            )
        result.append(url)
    return result


def plan_photo_merge(
    existing_fingerprints: Sequence[Optional[Mapping[str, Any]]],
    candidate_fingerprints: Sequence[Optional[Mapping[str, Any]]],
    *,
    max_images: int,
) -> dict[str, Any]:
    """Plan append-only photo changes without ever replacing live positions."""
    if max_images <= 0 or len(existing_fingerprints) > max_images:
        raise WBEnrichmentMergeError("Live-галерея превышает допустимый лимит")

    accepted_indices = []
    accepted_fingerprints: list[Mapping[str, Any]] = []
    items = []
    live_failures = sum(fp is None for fp in existing_fingerprints)
    counts = {
        "already_present": 0,
        "append": 0,
        "duplicate_candidate": 0,
        "skipped_capacity": 0,
        "skipped_match_unavailable": 0,
        "skipped_unreadable": 0,
    }

    for candidate_index, candidate in enumerate(candidate_fingerprints):
        if candidate is None:
            decision = "skipped_unreadable"
            best_score = None
            matched_position = None
        else:
            best_score = -1
            matched_position = None
            match_kind = None
            for existing_index, existing in enumerate(existing_fingerprints):
                if existing is None:
                    continue
                score = photo_similarity(existing, candidate)
                if score > best_score:
                    best_score = score
                    matched_position = existing_index + 1
                    match_kind = "already_present"
            for accepted_offset, accepted in enumerate(accepted_fingerprints):
                score = photo_similarity(accepted, candidate)
                if score > best_score:
                    best_score = score
                    matched_position = (
                        len(existing_fingerprints) + accepted_offset + 1
                    )
                    match_kind = "duplicate_candidate"

            if best_score >= PHOTO_MATCH_THRESHOLD:
                decision = str(match_kind)
            elif len(existing_fingerprints) + len(accepted_indices) >= max_images:
                decision = "skipped_capacity"
                matched_position = None
            elif live_failures:
                # A missing live fingerprint means we cannot prove that this
                # source photo is absent.  Fail closed instead of introducing
                # a duplicate into a manually curated gallery.
                decision = "skipped_match_unavailable"
                matched_position = None
            else:
                decision = "append"
                matched_position = len(existing_fingerprints) + len(accepted_indices) + 1
                accepted_indices.append(candidate_index)
                accepted_fingerprints.append(candidate)

        counts[decision] += 1
        item = {
            "candidate_index": candidate_index,
            "decision": decision,
            "target_or_match_position": matched_position,
            "similarity": best_score if best_score is not None and best_score >= 0 else None,
            "candidate_fingerprint": (
                candidate.get("pixel_sha") if candidate is not None else None
            ),
        }
        items.append(item)

    return {
        "policy": MERGE_POLICY_VERSION,
        "mode": "preserve_live_append_missing",
        "live_count": len(existing_fingerprints),
        "candidate_count": len(candidate_fingerprints),
        "live_fingerprint_failures": live_failures,
        "matching_status": "complete" if live_failures == 0 else "blocked",
        "append_indices": accepted_indices,
        "counts": counts,
        "items": items,
    }
