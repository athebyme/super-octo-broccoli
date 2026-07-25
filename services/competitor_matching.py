# -*- coding: utf-8 -*-
"""Shared WB competitor -> SupplierProduct identity matching.

The identity boundary is intentionally strict:

* marketplace facts come from the public, observed ``CompetitorProduct`` row;
* candidate facts come only from the central ``SupplierProduct`` feed fields;
* seller ``Product`` and copied/edited ``ImportedProduct`` content are never
  read by the scorer or sent to the LLM;
* a completed fingerprint is shared by every seller, while seller decisions
  and seller prices remain tenant-scoped.

External image/LLM calls happen only from the singleton scheduler tick.  HTTP
helpers below only enqueue work or read/write local reviewed state.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any

from PIL import Image, ImageOps
from sqlalchemy import and_, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from models import (
    BackgroundJob,
    CompetitorGroup,
    CompetitorMatchEvent,
    CompetitorProduct,
    CompetitorProductMatch,
    ImportedProduct,
    Product,
    SellerCompetitorMatchReview,
    SupplierProduct,
    db,
)


logger = logging.getLogger(__name__)

ALGORITHM_VERSION = 'supplier-observed-v2'
MAX_GROUP_PRODUCTS = 300
MAX_CANDIDATES = 3
MAX_INDEX_CANDIDATES = 500
MAX_FACT_CHARS = 600
MAX_CHARACTERISTICS = 40
DEFAULT_INDEX_TTL_SECONDS = 600
DEFAULT_RECHECK_DAYS = 30
DEFAULT_ITEMS_PER_TICK = 3
DEFAULT_TICK_SECONDS = 45
DEFAULT_MAX_LLM_CALLS_PER_JOB = 300
DEFAULT_MAX_PHOTOS_PER_SIDE = 3
DEFAULT_IMAGE_MAX_BYTES = 8 * 1024 * 1024
CLAIM_MINUTES = 10
WAIT_OBSERVATION_ATTEMPTS = 6

_TOKEN_RE = re.compile(r'[a-zа-яё0-9]+', re.IGNORECASE)
_SPACE_RE = re.compile(r'\s+')
_STOPWORDS = {
    'а', 'без', 'в', 'во', 'для', 'до', 'и', 'из', 'или', 'к', 'на', 'не',
    'от', 'по', 'под', 'при', 'с', 'со', 'у', 'шт', 'штук', 'товар',
    'набор', 'универсальный', 'новый', 'модель', 'цвет', 'размер', 'the',
    'with', 'for', 'of', 'and', 'in', 'pcs',
}
_UNIT_ALIASES = {
    'миллилитр': 'мл', 'миллилитров': 'мл', 'ml': 'мл',
    'литр': 'л', 'литра': 'л', 'литров': 'л', 'l': 'л',
    'грамм': 'г', 'грамма': 'г', 'граммов': 'г', 'gr': 'г', 'g': 'г',
    'килограмм': 'кг', 'килограмма': 'кг', 'килограммов': 'кг', 'kg': 'кг',
    'сантиметр': 'см', 'сантиметра': 'см', 'сантиметров': 'см', 'cm': 'см',
    'миллиметр': 'мм', 'миллиметра': 'мм', 'миллиметров': 'мм', 'mm': 'мм',
}

_supplier_index_lock = threading.Lock()
_supplier_index_cache: dict[str, Any] = {'loaded_at': 0.0, 'value': None}
_image_cache_lock = threading.Lock()
_image_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_DCT_COS: list[list[float]] | None = None


class CompetitorMatchingError(RuntimeError):
    """Safe domain error suitable for a bounded API response."""


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _json_load(raw: Any, fallback: Any) -> Any:
    if raw is None:
        return fallback
    if isinstance(raw, (list, dict)):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    return value


def _json_dump(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    )


def _bounded_text(value: Any, limit: int = MAX_FACT_CHARS) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    return _SPACE_RE.sub(' ', str(value)).strip()[:limit]


def _normalize_text(value: Any) -> str:
    text = _bounded_text(value, 4000).casefold().replace('ё', 'е')
    tokens = []
    for token in _TOKEN_RE.findall(text):
        tokens.append(_UNIT_ALIASES.get(token, token))
    return ' '.join(tokens)


def _tokens(value: Any) -> set[str]:
    return {
        token for token in _normalize_text(value).split()
        if len(token) > 1 and token not in _STOPWORDS
    }


def _number_units(value: Any) -> set[str]:
    normalized = _normalize_text(value)
    parts = normalized.split()
    result = set()
    for index, part in enumerate(parts):
        if not re.fullmatch(r'\d+(?:[.,]\d+)?', part):
            continue
        number = part.replace(',', '.')
        unit = parts[index + 1] if index + 1 < len(parts) else ''
        unit = _UNIT_ALIASES.get(unit, unit)
        result.add(number + (unit if unit in {'мл', 'л', 'г', 'кг', 'см', 'мм'} else ''))
    return result


def _bounded_values(raw: Any, limit: int = 20) -> list[str]:
    value = _json_load(raw, raw)
    if isinstance(value, dict):
        iterable = list(value.values())
    elif isinstance(value, list):
        iterable = value
    elif value:
        iterable = [value]
    else:
        iterable = []
    result = []
    for item in iterable:
        if isinstance(item, dict):
            item = item.get('value') or item.get('name') or ''
        text = _bounded_text(item, 120)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _bounded_characteristics(raw: Any) -> list[dict[str, str]]:
    value = _json_load(raw, [])
    if isinstance(value, dict):
        value = [{'name': key, 'value': item} for key, item in value.items()]
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _bounded_text(
            item.get('name') or item.get('key') or item.get('title'), 100,
        )
        raw_value = item.get('value')
        if isinstance(raw_value, list):
            raw_value = ', '.join(
                _bounded_text(part, 60) for part in raw_value[:8]
                if _bounded_text(part, 60)
            )
        value_text = _bounded_text(raw_value, 180)
        key = name.casefold()
        if not name or not value_text or key in seen:
            continue
        seen.add(key)
        result.append({'name': name, 'value': value_text})
        if len(result) >= MAX_CHARACTERISTICS:
            break
    return result


def _merge_characteristics(*sources: Any) -> list[dict[str, str]]:
    """Merge only explicitly supplied observed collections, preserving order."""
    result = []
    seen = set()
    for source in sources:
        for item in _bounded_characteristics(source):
            key = item['name'].casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
            if len(result) >= MAX_CHARACTERISTICS:
                return result
    return result


def _supplier_photo_urls(raw: Any, limit: int = 6) -> list[str]:
    """Extract original feed URLs; processed/AI media is never inspected."""
    value = _json_load(raw, [])
    if not isinstance(value, list):
        value = [value]
    result = []
    for item in value:
        candidates: list[Any]
        if isinstance(item, str):
            candidates = [item]
        elif isinstance(item, dict):
            preferred = [
                url for key, url in item.items()
                if str(key).casefold() not in {'blur', 'thumb', 'thumbnail'}
            ]
            candidates = preferred or list(item.values())
        else:
            candidates = []
        for candidate in candidates:
            url = _bounded_text(candidate, 1000)
            if not url.startswith(('http://', 'https://')) or url in result:
                continue
            result.append(url)
            break
        if len(result) >= limit:
            break
    return result


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json_dump(value).encode('utf-8')).hexdigest()


def _supplier_fact_pack(row: Any) -> dict[str, Any]:
    """Whitelist supplier-observed fields and explicitly omit every ai_* field."""
    original = _json_load(row.original_data_json, {})
    if not isinstance(original, dict):
        original = {}

    has_observed_snapshot = bool(original)

    def observed(key: str, normalized: Any) -> Any:
        # ``original_data_json`` is refreshed from the feed and is the first
        # and exclusive choice once present.  Falling back per-field would let
        # an edited/AI-normalized value fill a key omitted by the real feed.
        # Whole-row fallback exists only for legacy rows that predate observed
        # source snapshots.
        if has_observed_snapshot:
            return original.get(key)
        return normalized

    if has_observed_snapshot:
        characteristics = _merge_characteristics(
            original.get('characteristics'),
            original.get('extracted_characteristics'),
            original.get('_extra_characteristics'),
        )
        dimensions = _merge_characteristics(
            original.get('dimensions'), original.get('_extra_dimensions'),
        )
    else:
        characteristics = _bounded_characteristics(row.characteristics_json)
        dimensions = _bounded_characteristics(row.dimensions_json)
    facts = {
        'supplier_product_id': int(row.id),
        'external_id': _bounded_text(observed('external_id', row.external_id), 120),
        'vendor_code': _bounded_text(observed('vendor_code', row.vendor_code), 120),
        'additional_vendor_code': _bounded_text(
            observed('additional_vendor_code', row.additional_vendor_code), 120),
        'barcodes': _bounded_values(observed(
            'barcodes', row.barcodes_json or row.barcode,
        ), 12),
        'title': _bounded_text(observed('title', row.title), 500),
        'description': _bounded_text(
            observed('description', row.description), MAX_FACT_CHARS),
        'brand': _bounded_text(observed('brand', row.brand), 160),
        'category': _bounded_text(observed('category', row.category), 240),
        'category_path': _bounded_values(observed(
            'all_categories', row.all_categories,
        ), 12),
        'characteristics': characteristics,
        'sizes': _bounded_values(observed('sizes_raw', row.sizes_json), 20),
        'colors': _bounded_values(observed('colors', row.colors_json), 20),
        'materials': _bounded_values(observed('materials', row.materials_json), 20),
        'dimensions': dimensions,
        'gender': _bounded_text(observed('gender', row.gender), 50),
        'country': _bounded_text(observed('country', row.country), 100),
        'season': _bounded_text(observed('season', row.season), 50),
        'age_group': _bounded_text(observed('age_group', row.age_group), 50),
        'photos': _supplier_photo_urls(observed(
            'photo_urls', row.photo_urls_json,
        )),
        'source_mode': (
            'original_data_json' if has_observed_snapshot
            else 'legacy_normalized_supplier_fields'
        ),
    }
    return facts


def _facts_text(facts: dict[str, Any]) -> str:
    characteristic_text = ' '.join(
        f"{item.get('name', '')} {item.get('value', '')}"
        for item in facts.get('characteristics', [])
    )
    dimension_text = ' '.join(
        f"{item.get('name', '')} {item.get('value', '')}"
        for item in facts.get('dimensions', [])
    )
    values = [
        facts.get('title'), facts.get('brand'), facts.get('category'),
        ' '.join(facts.get('category_path') or []),
        characteristic_text, dimension_text,
        ' '.join(facts.get('sizes') or []),
        ' '.join(facts.get('colors') or []),
        ' '.join(facts.get('materials') or []),
        facts.get('gender'), facts.get('country'), facts.get('season'),
        facts.get('age_group'), facts.get('vendor_code'),
        facts.get('additional_vendor_code'),
    ]
    return ' '.join(_bounded_text(value, 1000) for value in values if value)


def _build_supplier_index() -> dict[str, Any]:
    columns = (
        SupplierProduct.id,
        SupplierProduct.external_id,
        SupplierProduct.vendor_code,
        SupplierProduct.additional_vendor_code,
        SupplierProduct.barcode,
        SupplierProduct.barcodes_json,
        SupplierProduct.title,
        SupplierProduct.description,
        SupplierProduct.brand,
        SupplierProduct.category,
        SupplierProduct.all_categories,
        SupplierProduct.characteristics_json,
        SupplierProduct.sizes_json,
        SupplierProduct.colors_json,
        SupplierProduct.materials_json,
        SupplierProduct.dimensions_json,
        SupplierProduct.gender,
        SupplierProduct.country,
        SupplierProduct.season,
        SupplierProduct.age_group,
        SupplierProduct.photo_urls_json,
        SupplierProduct.original_data_json,
    )
    records = []
    postings: dict[str, list[int]] = defaultdict(list)
    document_frequency: Counter[str] = Counter()
    for row in db.session.query(*columns).yield_per(1000):
        facts = _supplier_fact_pack(row)
        all_tokens = _tokens(_facts_text(facts))
        title_tokens = _tokens(facts['title'])
        record = {
            'id': int(row.id),
            'facts': facts,
            'fingerprint': _fingerprint(facts),
            'tokens': all_tokens,
            'title_tokens': title_tokens,
            'numbers': _number_units(_facts_text(facts)),
        }
        index = len(records)
        records.append(record)
        for token in all_tokens:
            postings[token].append(index)
            document_frequency[token] += 1
    return {
        'records': records,
        'postings': dict(postings),
        'df': dict(document_frequency),
        'count': len(records),
    }


def _get_supplier_index() -> dict[str, Any]:
    ttl = _env_int(
        'COMPETITOR_MATCH_INDEX_TTL_SECONDS',
        DEFAULT_INDEX_TTL_SECONDS, 60, 3600,
    )
    now = time.monotonic()
    cached = _supplier_index_cache.get('value')
    if cached is not None and now - _supplier_index_cache['loaded_at'] < ttl:
        return cached
    with _supplier_index_lock:
        now = time.monotonic()
        cached = _supplier_index_cache.get('value')
        if cached is not None and now - _supplier_index_cache['loaded_at'] < ttl:
            return cached
        value = _build_supplier_index()
        _supplier_index_cache['value'] = value
        _supplier_index_cache['loaded_at'] = now
        return value


def invalidate_supplier_match_index() -> None:
    """Test/ingest hook; TTL remains the normal invalidation path."""
    with _supplier_index_lock:
        _supplier_index_cache['value'] = None
        _supplier_index_cache['loaded_at'] = 0.0


def _marketplace_fact_pack(nm_id: int) -> dict[str, Any] | None:
    rows = (
        CompetitorProduct.query
        .filter(CompetitorProduct.nm_id == nm_id)
        .order_by(
            CompetitorProduct.metadata_synced_at.desc().nullslast(),
            CompetitorProduct.last_fetched_at.desc().nullslast(),
            CompetitorProduct.updated_at.desc(),
        )
        .limit(8).all()
    )
    if not rows:
        return None

    def first(name: str) -> Any:
        for row in rows:
            value = getattr(row, name, None)
            if value not in (None, ''):
                return value
        return None

    characteristics = []
    for row in rows:
        candidate = _bounded_characteristics(row.characteristics_json)
        if len(candidate) > len(characteristics):
            characteristics = candidate
    image_url = _bounded_text(first('image_url'), 1000)
    photo_count = first('photo_count')
    if isinstance(photo_count, bool) or not isinstance(photo_count, int):
        photo_count = 1 if image_url else 0
    photo_count = max(0, min(photo_count, 30))
    photos = []
    if image_url.startswith(('http://', 'https://')):
        photos.append(image_url)
        for index in range(2, min(photo_count, 6) + 1):
            if re.search(r'/1\.(?:webp|jpg|jpeg|png)(?:\?.*)?$', image_url, re.I):
                photos.append(re.sub(
                    r'/1(\.(?:webp|jpg|jpeg|png)(?:\?.*)?)$',
                    f'/{index}\\1', image_url, flags=re.I,
                ))
    facts = {
        'nm_id': int(nm_id),
        'title': _bounded_text(first('title'), 500),
        'brand': _bounded_text(first('brand'), 160),
        'subject_id': first('subject_id'),
        'subject_name': _bounded_text(first('subject_name'), 200),
        'marketplace_seller': _bounded_text(first('supplier_name'), 160),
        'marketplace_seller_id': first('wb_supplier_id'),
        'characteristics': characteristics,
        'photo_count': photo_count,
        'photos': photos,
    }
    if not facts['title'] and not characteristics:
        return None
    return facts


def _score_candidate(
    marketplace: dict[str, Any], candidate: dict[str, Any],
) -> dict[str, Any]:
    supplier = candidate['facts']
    market_title = _normalize_text(marketplace.get('title'))
    supplier_title = _normalize_text(supplier.get('title'))
    market_title_tokens = _tokens(market_title)
    supplier_title_tokens = candidate['title_tokens']
    intersection = market_title_tokens & supplier_title_tokens
    union = market_title_tokens | supplier_title_tokens
    jaccard = len(intersection) / len(union) if union else 0.0
    coverage = (
        len(intersection) / min(len(market_title_tokens), len(supplier_title_tokens))
        if market_title_tokens and supplier_title_tokens else 0.0
    )
    sequence = SequenceMatcher(None, market_title, supplier_title).ratio()

    market_brand = _normalize_text(marketplace.get('brand'))
    supplier_brand = _normalize_text(supplier.get('brand'))
    brand_exact = bool(market_brand and supplier_brand and market_brand == supplier_brand)
    brand_overlap = bool(_tokens(market_brand) & _tokens(supplier_brand))
    brand_conflict = bool(market_brand and supplier_brand and not brand_overlap)

    market_category = _tokens(marketplace.get('subject_name'))
    supplier_category = _tokens(
        ' '.join([supplier.get('category', '')]
                 + list(supplier.get('category_path') or [])),
    )
    category_overlap = (
        len(market_category & supplier_category) /
        max(1, min(len(market_category), len(supplier_category)))
    )

    market_all_text = ' '.join([
        marketplace.get('title') or '', marketplace.get('brand') or '',
        marketplace.get('subject_name') or '',
        ' '.join(
            f"{item.get('name', '')} {item.get('value', '')}"
            for item in marketplace.get('characteristics') or []
        ),
    ])
    market_numbers = _number_units(market_all_text)
    supplier_numbers = candidate['numbers']
    number_overlap = market_numbers & supplier_numbers
    numeric_conflict = bool(
        market_numbers and supplier_numbers and not number_overlap
    )

    score = (
        44 * coverage + 22 * sequence + 14 * jaccard
        + (14 if brand_exact else 6 if brand_overlap else 0)
        + 6 * category_overlap
    )
    if numeric_conflict:
        score -= 16
    elif number_overlap:
        score += min(8, len(number_overlap) * 4)
    if brand_conflict:
        score -= 10
    score = max(0, min(100, round(score)))
    return {
        'supplier_product_id': candidate['id'],
        'text_score': score,
        'source_fingerprint': candidate['fingerprint'],
        'evidence': {
            'title_overlap': sorted(intersection)[:12],
            'title_coverage': round(coverage, 3),
            'title_similarity': round(sequence, 3),
            'brand_exact': brand_exact,
            'brand_conflict': brand_conflict,
            'category_overlap': round(category_overlap, 3),
            'number_overlap': sorted(number_overlap)[:10],
            'numeric_conflict': numeric_conflict,
        },
        '_record': candidate,
    }


def shortlist_supplier_candidates(
    marketplace: dict[str, Any], limit: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    index = _get_supplier_index()
    query_text = ' '.join([
        marketplace.get('title') or '', marketplace.get('brand') or '',
        marketplace.get('subject_name') or '',
        ' '.join(
            f"{item.get('name', '')} {item.get('value', '')}"
            for item in marketplace.get('characteristics') or []
        ),
    ])
    query_tokens = _tokens(query_text)
    weighted: defaultdict[int, float] = defaultdict(float)
    count = max(1, index['count'])
    ranked_tokens = sorted(
        query_tokens,
        key=lambda token: index['df'].get(token, count + 1),
    )[:16]
    for token in ranked_tokens:
        postings = index['postings'].get(token, [])
        if len(postings) > 5000:
            continue
        weight = math.log((count + 1) / (len(postings) + 1)) + 1.0
        for record_index in postings:
            weighted[record_index] += weight
    if not weighted:
        return []
    candidate_indexes = [
        item[0] for item in sorted(
            weighted.items(), key=lambda pair: (-pair[1], pair[0]),
        )[:MAX_INDEX_CANDIDATES]
    ]
    scored = [
        _score_candidate(marketplace, index['records'][record_index])
        for record_index in candidate_indexes
    ]
    scored.sort(key=lambda item: (-item['text_score'], item['supplier_product_id']))
    return scored[:max(1, min(MAX_CANDIDATES, limit))]


def _dct_cos() -> list[list[float]]:
    global _DCT_COS
    if _DCT_COS is None:
        _DCT_COS = [
            [math.cos((2 * x + 1) * u * math.pi / 64) for x in range(32)]
            for u in range(8)
        ]
    return _DCT_COS


def _image_fingerprint(data: bytes) -> dict[str, Any]:
    with Image.open(BytesIO(data)) as opened:
        image = ImageOps.exif_transpose(opened).convert('RGB')
        width, height = image.size
        normalized = image.resize((128, 128), Image.Resampling.LANCZOS)
        pixel_sha = hashlib.sha256(normalized.tobytes()).hexdigest()

        gray9 = image.convert('L').resize((9, 8), Image.Resampling.LANCZOS)
        values9 = list(gray9.get_flattened_data())
        dhash = 0
        for y in range(8):
            for x in range(8):
                dhash = (dhash << 1) | int(
                    values9[y * 9 + x] > values9[y * 9 + x + 1]
                )

        gray32 = image.convert('L').resize((32, 32), Image.Resampling.LANCZOS)
        pixels = list(gray32.get_flattened_data())
        cosine = _dct_cos()
        coefficients = []
        for u in range(8):
            for v in range(8):
                total = 0.0
                for y in range(32):
                    cy = cosine[v][y]
                    row_offset = y * 32
                    for x in range(32):
                        total += pixels[row_offset + x] * cosine[u][x] * cy
                coefficients.append(total)
        tail = sorted(coefficients[1:])
        median = tail[len(tail) // 2]
        phash = 0
        for coefficient in coefficients:
            phash = (phash << 1) | int(coefficient > median)
        return {
            'pixel_sha': pixel_sha,
            'dhash': dhash,
            'phash': phash,
            'width': width,
            'height': height,
        }


def _download_image_fingerprint(url: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _image_cache_lock:
        cached = _image_cache.get(url)
    if cached and now - cached[0] < 3600:
        return cached[1]
    result = None
    try:
        # Reuse the hardened downloader: DNS/private-IP checks are repeated on
        # every redirect and response bytes are capped.
        from services.image_lab_service import download_public_image
        data = download_public_image(
            url,
            max_bytes=_env_int(
                'COMPETITOR_MATCH_IMAGE_MAX_BYTES',
                DEFAULT_IMAGE_MAX_BYTES, 256 * 1024, 20 * 1024 * 1024,
            ),
            timeout=(4.0, 10.0),
        )
        result = _image_fingerprint(data)
    except Exception as error:  # no provider body/URL is persisted
        logger.info(
            'Competitor match image unavailable (%s)',
            type(error).__name__,
        )
    with _image_cache_lock:
        if len(_image_cache) >= 2000:
            oldest = min(_image_cache, key=lambda key: _image_cache[key][0])
            _image_cache.pop(oldest, None)
        _image_cache[url] = (now, result)
    return result


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _image_similarity(
    marketplace_urls: list[str], supplier_urls: list[str],
) -> tuple[int | None, dict[str, Any]]:
    cap = _env_int(
        'COMPETITOR_MATCH_MAX_PHOTOS_PER_SIDE',
        DEFAULT_MAX_PHOTOS_PER_SIDE, 1, 5,
    )
    marketplace_fps = [
        fp for fp in (
            _download_image_fingerprint(url) for url in marketplace_urls[:cap]
        ) if fp
    ]
    supplier_fps = [
        fp for fp in (
            _download_image_fingerprint(url) for url in supplier_urls[:cap]
        ) if fp
    ]
    if not marketplace_fps or not supplier_fps:
        return None, {
            'status': 'unavailable',
            'marketplace_images': len(marketplace_fps),
            'supplier_images': len(supplier_fps),
            'compared_pairs': 0,
            'strong_pair': False,
        }
    best_score = 0
    best_distances = None
    pairs = 0
    for left in marketplace_fps:
        for right in supplier_fps:
            pairs += 1
            if left['pixel_sha'] == right['pixel_sha']:
                score, distances = 100, {'phash': 0, 'dhash': 0}
            else:
                p_distance = _hamming(left['phash'], right['phash'])
                d_distance = _hamming(left['dhash'], right['dhash'])
                if p_distance <= 4 and d_distance <= 6:
                    score = 97
                elif p_distance <= 8 and d_distance <= 10:
                    score = 90
                elif p_distance <= 12 and d_distance <= 15:
                    score = 78
                elif p_distance <= 18 and d_distance <= 22:
                    score = 58
                else:
                    score = max(0, round(45 - (p_distance + d_distance) / 2))
                distances = {'phash': p_distance, 'dhash': d_distance}
            if score > best_score:
                best_score = score
                best_distances = distances
    return best_score, {
        'status': 'matched' if best_score >= 88 else 'different',
        'marketplace_images': len(marketplace_fps),
        'supplier_images': len(supplier_fps),
        'compared_pairs': pairs,
        'strong_pair': best_score >= 88,
        'best_distances': best_distances,
    }


def _deterministic_score(text_score: int, image_score: int | None) -> int:
    if image_score is None:
        return max(0, min(100, round(text_score * 0.86)))
    if image_score >= 88:
        return max(0, min(100, round(text_score * 0.62 + image_score * 0.38)))
    if image_score >= 58:
        return max(0, min(100, round(text_score * 0.75 + image_score * 0.25)))
    return max(0, min(100, round(text_score * 0.72 + image_score * 0.12)))


def _exact_same_gate(
    candidate: dict[str, Any] | None,
    llm_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Admit a shared ``same`` only with strong, non-conflicting evidence.

    A classifier verdict cannot turn a generic product-type overlap into an
    exact identity.  Seller confirmation remains the explicit escape hatch
    for true matches whose public/source evidence is incomplete.
    """
    if not candidate:
        return {
            'admitted': False,
            'route': None,
            'reasons': ['no_candidate'],
        }
    evidence = candidate.get('evidence') or {}
    image_evidence = candidate.get('image_evidence') or {}
    llm_conflicts = [
        item for item in ((llm_result or {}).get('conflicts') or []) if item
    ]
    text_score = int(candidate.get('text_score') or 0)
    coverage = float(evidence.get('title_coverage') or 0)
    similarity = float(evidence.get('title_similarity') or 0)
    strong_photo = bool(image_evidence.get('strong_pair'))
    brand_exact = bool(evidence.get('brand_exact'))
    brand_conflict = bool(evidence.get('brand_conflict'))
    numeric_conflict = bool(evidence.get('numeric_conflict'))

    photo_route = (
        strong_photo
        and text_score >= 65
        and coverage >= 0.5
    )
    text_route = (
        text_score >= 94
        and coverage >= 0.9
        and similarity >= 0.88
        and (brand_exact or text_score >= 98)
    )
    reasons = []
    if brand_conflict:
        reasons.append('brand_conflict')
    if numeric_conflict:
        reasons.append('numeric_conflict')
    if llm_conflicts:
        reasons.append('llm_reported_conflicts')
    if not photo_route and not text_route:
        reasons.append('no_strong_identity_route')
    admitted = not reasons
    return {
        'admitted': admitted,
        'route': (
            'strong_photo' if admitted and photo_route
            else 'near_exact_text' if admitted and text_route
            else None
        ),
        'reasons': reasons,
        'text_score': text_score,
        'title_coverage': round(coverage, 3),
        'title_similarity': round(similarity, 3),
        'strong_photo': strong_photo,
        'brand_exact': brand_exact,
        'llm_conflict_count': min(len(llm_conflicts), 8),
    }


def shared_exact_match_is_admissible(
    match: CompetitorProductMatch | None,
) -> bool:
    """Fail-closed read boundary for unreviewed shared exact matches.

    New rows carry the v2 gate result. Legacy rows are accepted only when
    both text and a strong perceptual-photo signal were already high; all
    other legacy ``same`` rows require seller confirmation.
    """
    if (
        not match
        or match.processing_status != 'completed'
        or match.predicted_match_type != 'same'
        or not match.suggested_supplier_product_id
    ):
        return False
    evidence = _json_load(match.evidence_json, {})
    gate = (
        evidence.get('exact_same_gate')
        if isinstance(evidence, dict) else None
    )
    if isinstance(gate, dict) and isinstance(gate.get('admitted'), bool):
        return gate['admitted']
    return bool(
        int(match.final_score or 0) >= 88
        and int(match.text_score or 0) >= 70
        and match.image_score is not None
        and int(match.image_score) >= 88
    )


_LLM_SYSTEM = """Ты проверяешь идентичность товарных карточек.
Слева только наблюдаемые публичные факты маркетплейса, справа только исходные
данные фида поставщика. Нельзя домысливать свойства, выбирать ID вне списка или
считать похожий тип товара той же моделью. same = тот же товар/модель/вариант;
analog = функционально близкий, но не тот же товар; different = конфликт;
uncertain = данных недостаточно. Результат модели — лишь один сигнал: итоговую
уверенность вычисляет Python. Совпадение бренда, категории и общего типа товара
само по себе недостаточно для same. Если фото отмечены как different, название
лишь частично совпадает либо отсутствует доказательство конкретной модели и
варианта, выбирай uncertain/analog, а не same. Фото уже сравнены
детерминированно по содержимому."""

_LLM_SCHEMA = {
    'type': 'object',
    'required': [
        'supplier_product_id', 'verdict', 'matched_facts', 'conflicts',
        'reason',
    ],
    'properties': {
        'supplier_product_id': {'type': ['integer', 'null']},
        'verdict': {
            'type': 'string',
            'enum': ['same', 'analog', 'different', 'uncertain'],
        },
        'matched_facts': {
            'type': 'array', 'maxItems': 8, 'items': {'type': 'string'},
        },
        'conflicts': {
            'type': 'array', 'maxItems': 8, 'items': {'type': 'string'},
        },
        'reason': {'type': 'string', 'maxLength': 500},
    },
    'additionalProperties': False,
}


def _prompt_fact_pack(facts: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in facts.items()
        if key != 'photos' and value not in ('', [], None)
    } | {'photo_count': len(facts.get('photos') or [])}


def _safe_usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key in ('input_tokens', 'output_tokens', 'cache_read_tokens',
                'cache_creation_tokens', 'api_requests'):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    return result


def _run_llm(
    marketplace: dict[str, Any], candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    from agents.llm import create_llm_from_profile, llm_retry_attempt_limit
    from services.llm_config import get_central_llm_config

    central = get_central_llm_config()
    if not central:
        return {'status': 'unavailable', 'called': False, 'model': None}
    profile = {
        'provider': central.get('provider'),
        'key': central.get('api_key'),
        'model': central.get('model'),
        'base_url': central.get('base_url'),
        # This is a short classifier, not an orchestration task.  DeepSeek's
        # thinking mode can consume the entire output budget before emitting
        # JSON, so disable it explicitly just like other execution helpers.
        'thinking': False,
    }
    model_label = '/'.join(filter(None, [
        _bounded_text(profile.get('provider'), 40),
        _bounded_text(profile.get('model'), 120),
    ]))[:160]
    payload = {
        'marketplace': _prompt_fact_pack(marketplace),
        'supplier_candidates': [
            {
                'supplier_product_id': candidate['supplier_product_id'],
                'facts': _prompt_fact_pack(candidate['_record']['facts']),
                'text_score': candidate['text_score'],
                'photo_evidence': candidate.get('image_evidence'),
            }
            for candidate in candidates
        ],
    }
    try:
        llm = create_llm_from_profile(profile)
        with llm_retry_attempt_limit(1):
            response = llm.structured_output_with_usage(
                system=_LLM_SYSTEM,
                prompt=_json_dump(payload),
                schema=_LLM_SCHEMA,
                max_tokens=650,
            )
        data = response.get('data') if isinstance(response, dict) else None
        if not isinstance(data, dict):
            raise ValueError('malformed_result')
        allowed_ids = {
            candidate['supplier_product_id'] for candidate in candidates
        }
        candidate_id = data.get('supplier_product_id')
        if candidate_id is not None and (
            isinstance(candidate_id, bool) or not isinstance(candidate_id, int)
            or candidate_id not in allowed_ids
        ):
            raise ValueError('candidate_out_of_scope')
        verdict = data.get('verdict')
        if verdict not in {'same', 'analog', 'different', 'uncertain'}:
            raise ValueError('invalid_verdict')
        if verdict in {'same', 'analog'} and candidate_id is None:
            raise ValueError('missing_candidate')
        return {
            'status': 'completed',
            'called': True,
            'model': model_label,
            'supplier_product_id': candidate_id,
            'verdict': verdict,
            'matched_facts': [
                _bounded_text(item, 160)
                for item in (data.get('matched_facts') or [])[:8]
                if _bounded_text(item, 160)
            ],
            'conflicts': [
                _bounded_text(item, 160)
                for item in (data.get('conflicts') or [])[:8]
                if _bounded_text(item, 160)
            ],
            'reason': _bounded_text(data.get('reason'), 500),
            'usage': _safe_usage(response.get('usage')),
        }
    except Exception as error:
        usage = _safe_usage(getattr(error, 'llm_usage', {}))
        safe_error = 'provider_' + type(error).__name__.casefold()[:40]
        if isinstance(error, ValueError):
            message = str(error)
            allowed = {
                'malformed_result', 'candidate_out_of_scope',
                'invalid_verdict', 'missing_candidate',
            }
            if message in allowed:
                safe_error = message
            elif message.startswith('Cannot extract JSON from LLM response'):
                safe_error = 'invalid_json'
            else:
                safe_error = 'invalid_result'
        logger.warning(
            'Competitor match LLM failed (%s)', safe_error,
        )
        return {
            'status': 'failed',
            'called': True,
            'model': model_label,
            'error_code': ('llm_' + safe_error)[:64],
            'usage': usage,
        }


def _provider_identity() -> str:
    try:
        from services.llm_config import get_central_llm_config
        config = get_central_llm_config() or {}
        return '/'.join(filter(None, [
            _bounded_text(config.get('provider'), 40),
            _bounded_text(config.get('model'), 120),
        ])) or 'unavailable'
    except Exception:
        return 'unavailable'


def _cache_is_fresh(row: CompetitorProductMatch, evaluation_fingerprint: str) -> bool:
    if (
        row.processing_status != 'completed'
        or row.evaluation_fingerprint != evaluation_fingerprint
        or not row.evaluated_at
    ):
        return False
    days = _env_int(
        'COMPETITOR_MATCH_RECHECK_DAYS', DEFAULT_RECHECK_DAYS, 1, 365,
    )
    return row.evaluated_at >= datetime.utcnow() - timedelta(days=days)


def ensure_global_match_rows(nm_ids: list[int]) -> None:
    if not nm_ids:
        return
    # Two seller HTTP workers may enqueue the same global nmID concurrently.
    # INSERT .. ON CONFLICT is the only write here and keeps that race
    # idempotent without turning seller requests into provider work.
    statement = sqlite_insert(CompetitorProductMatch.__table__).values([
        {
            'nm_id': nm_id,
            'processing_status': 'queued',
            'algorithm_version': ALGORITHM_VERSION,
            'llm_status': 'pending',
            'marketplace_facts_json': '{}',
            'text_score': 0,
            'deterministic_score': 0,
            'final_score': 0,
            'candidates_json': '[]',
            'evidence_json': '{}',
            'llm_usage_json': '{}',
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow(),
        }
        for nm_id in nm_ids
    ]).on_conflict_do_nothing(index_elements=['nm_id'])
    db.session.execute(statement)


def process_global_match(nm_id: int, *, allow_llm: bool = True) -> dict[str, Any]:
    """Evaluate one shared nmID. Must only be called by background runtime."""
    marketplace = _marketplace_fact_pack(nm_id)
    row = CompetitorProductMatch.query.filter_by(nm_id=nm_id).first()
    if not row:
        row = CompetitorProductMatch(
            nm_id=nm_id, processing_status='queued',
            algorithm_version=ALGORITHM_VERSION, llm_status='pending',
        )
        db.session.add(row)
        db.session.flush()
    if marketplace is None:
        row.processing_status = 'queued'
        row.claim_token = None
        row.claim_expires_at = None
        db.session.commit()
        return {'status': 'waiting_observation', 'nm_id': nm_id,
                'llm_called': False, 'cache_hit': False}

    candidates = shortlist_supplier_candidates(marketplace)
    source_fingerprint = _fingerprint(marketplace)
    candidate_fingerprints = [
        [candidate['supplier_product_id'], candidate['source_fingerprint']]
        for candidate in candidates
    ]
    evaluation_fingerprint = _fingerprint({
        'algorithm': ALGORITHM_VERSION,
        'marketplace': source_fingerprint,
        'candidates': candidate_fingerprints,
        'llm': _provider_identity(),
    })
    if _cache_is_fresh(row, evaluation_fingerprint):
        return {'status': 'cached', 'nm_id': nm_id, 'llm_called': False,
                'cache_hit': True, 'match_id': row.id}

    now = datetime.utcnow()
    recheck_days = _env_int(
        'COMPETITOR_MATCH_RECHECK_DAYS', DEFAULT_RECHECK_DAYS, 1, 365,
    )
    fresh_cutoff = now - timedelta(days=recheck_days)
    claim = uuid.uuid4().hex
    # Compare-and-set is essential here: two seller workers can discover the
    # same uncached nmID concurrently.  SQLite serializes the UPDATE and only
    # one worker may acquire the unexpired global claim before image/LLM I/O.
    claimed = (
        CompetitorProductMatch.query
        .filter(
            CompetitorProductMatch.id == row.id,
            or_(
                CompetitorProductMatch.processing_status != 'processing',
                CompetitorProductMatch.claim_token.is_(None),
                CompetitorProductMatch.claim_expires_at.is_(None),
                CompetitorProductMatch.claim_expires_at <= now,
            ),
            # The pre-claim cache check can race a worker that is just about
            # to commit.  Re-check the same freshness predicate inside the
            # atomic UPDATE so a freshly completed fingerprint cannot be
            # claimed and sent to the LLM again.
            or_(
                CompetitorProductMatch.processing_status != 'completed',
                CompetitorProductMatch.evaluation_fingerprint.is_(None),
                CompetitorProductMatch.evaluation_fingerprint
                != evaluation_fingerprint,
                CompetitorProductMatch.evaluated_at.is_(None),
                CompetitorProductMatch.evaluated_at < fresh_cutoff,
            ),
        )
        .update({
            CompetitorProductMatch.processing_status: 'processing',
            CompetitorProductMatch.claim_token: claim,
            CompetitorProductMatch.claim_expires_at: (
                now + timedelta(minutes=CLAIM_MINUTES)
            ),
            CompetitorProductMatch.algorithm_version: ALGORITHM_VERSION,
        }, synchronize_session=False)
    )
    db.session.commit()
    if claimed != 1:
        current = db.session.get(CompetitorProductMatch, row.id)
        if current and _cache_is_fresh(current, evaluation_fingerprint):
            return {
                'status': 'cached', 'nm_id': nm_id,
                'llm_called': False, 'cache_hit': True,
                'match_id': current.id,
            }
        return {'status': 'shared_in_progress', 'nm_id': nm_id,
                'llm_called': False, 'cache_hit': False, 'match_id': row.id}

    try:
        for candidate in candidates:
            image_score, image_evidence = _image_similarity(
                marketplace.get('photos') or [],
                candidate['_record']['facts'].get('photos') or [],
            )
            candidate['image_score'] = image_score
            candidate['image_evidence'] = image_evidence
            candidate['deterministic_score'] = _deterministic_score(
                candidate['text_score'], image_score,
            )

        plausible = bool(candidates and candidates[0]['text_score'] >= 25)
        if allow_llm and plausible:
            llm_result = _run_llm(marketplace, candidates)
        elif plausible:
            llm_result = {
                'status': 'skipped', 'called': False, 'model': None,
                'error_code': 'job_llm_budget',
            }
        else:
            llm_result = {'status': 'skipped', 'called': False, 'model': None}

        chosen = candidates[0] if candidates else None
        verdict = None
        if llm_result.get('status') == 'completed':
            verdict = llm_result.get('verdict')
            chosen_id = llm_result.get('supplier_product_id')
            if chosen_id is not None:
                chosen = next(
                    (item for item in candidates
                     if item['supplier_product_id'] == chosen_id),
                    chosen,
                )

        suggested_id = None
        match_type = 'uncertain'
        final_score = chosen['deterministic_score'] if chosen else 0
        exact_same_gate = _exact_same_gate(chosen, llm_result)
        if chosen:
            numeric_conflict = bool(
                chosen.get('evidence', {}).get('numeric_conflict')
            )
            if verdict == 'different':
                match_type = 'different'
                final_score = min(final_score, 28)
            elif verdict == 'same':
                final_score = min(100, final_score + 10)
                suggested_id = chosen['supplier_product_id']
                if exact_same_gate['admitted']:
                    match_type = 'same'
                else:
                    # Preserve the candidate for explicit seller review, but
                    # never expose it as an exact shared identity.
                    match_type = 'uncertain'
                    final_score = min(final_score, 64)
            elif verdict == 'analog':
                match_type = 'analog'
                final_score = min(76, max(45, final_score + 2))
                suggested_id = chosen['supplier_product_id']
            elif verdict == 'uncertain':
                match_type = 'uncertain'
                final_score = max(0, final_score - 5)
                if final_score >= 58:
                    suggested_id = chosen['supplier_product_id']
            elif (
                final_score >= 72
                and exact_same_gate['admitted']
            ):
                match_type = 'same'
                suggested_id = chosen['supplier_product_id']
            elif final_score >= 58 and not numeric_conflict:
                match_type = 'analog'
                suggested_id = chosen['supplier_product_id']

        stored_candidates = []
        for candidate in candidates:
            stored_candidates.append({
                'supplier_product_id': candidate['supplier_product_id'],
                'text_score': candidate['text_score'],
                'image_score': candidate.get('image_score'),
                'deterministic_score': candidate.get('deterministic_score', 0),
                'source_fingerprint': candidate['source_fingerprint'],
                'evidence': candidate['evidence'],
                'image_evidence': candidate.get('image_evidence', {}),
            })
        evidence = {
            'source_scope': 'supplier_observed_only',
            'selected_candidate_id': (
                chosen['supplier_product_id'] if chosen else None
            ),
            'exact_same_gate': exact_same_gate,
            'llm_matched_facts': llm_result.get('matched_facts', []),
            'llm_conflicts': llm_result.get('conflicts', []),
        }

        current = db.session.get(CompetitorProductMatch, row.id)
        if not current or current.claim_token != claim:
            db.session.rollback()
            return {'status': 'claim_lost', 'nm_id': nm_id,
                    'llm_called': bool(llm_result.get('called')),
                    'cache_hit': False}
        current.suggested_supplier_product_id = suggested_id
        current.processing_status = 'completed'
        current.predicted_match_type = match_type
        current.marketplace_facts_json = _json_dump(marketplace)
        current.source_fingerprint = source_fingerprint
        current.text_score = chosen['text_score'] if chosen else 0
        current.image_score = chosen.get('image_score') if chosen else None
        current.deterministic_score = (
            chosen.get('deterministic_score', 0) if chosen else 0
        )
        current.final_score = max(0, min(100, int(final_score)))
        current.candidates_json = _json_dump(stored_candidates)
        current.evidence_json = _json_dump(evidence)
        current.algorithm_version = ALGORITHM_VERSION
        current.evaluation_fingerprint = evaluation_fingerprint
        current.evaluated_at = datetime.utcnow()
        current.llm_status = llm_result.get('status', 'failed')
        current.llm_verdict = llm_result.get('verdict')
        current.llm_reason = llm_result.get('reason')
        current.llm_model = llm_result.get('model')
        current.llm_usage_json = _json_dump(llm_result.get('usage', {}))
        current.llm_error_code = llm_result.get('error_code')
        current.llm_evaluated_at = (
            datetime.utcnow() if llm_result.get('called') else None
        )
        current.claim_token = None
        current.claim_expires_at = None
        db.session.commit()
        return {
            'status': 'completed', 'nm_id': nm_id,
            'llm_called': bool(llm_result.get('called')),
            'cache_hit': False, 'match_id': current.id,
            'suggested_supplier_product_id': suggested_id,
            'final_score': current.final_score,
        }
    except Exception:
        db.session.rollback()
        failed = CompetitorProductMatch.query.filter_by(id=row.id).first()
        if failed and failed.claim_token == claim:
            failed.processing_status = 'failed'
            failed.claim_token = None
            failed.claim_expires_at = None
            failed.llm_error_code = 'matching_internal_error'
            db.session.commit()
        raise


def queue_group_matching(seller_id: int, group_id: int) -> BackgroundJob:
    group = CompetitorGroup.query.filter_by(
        id=group_id, seller_id=seller_id, is_active=True,
    ).first()
    if not group:
        raise CompetitorMatchingError('Группа не найдена')
    nm_ids = [
        int(value[0]) for value in (
            db.session.query(CompetitorProduct.nm_id)
            .filter_by(seller_id=seller_id, group_id=group_id, is_active=True)
            .distinct().order_by(CompetitorProduct.nm_id).limit(
                MAX_GROUP_PRODUCTS + 1,
            ).all()
        )
    ]
    if not nm_ids:
        raise CompetitorMatchingError('В группе пока нет товаров')
    if len(nm_ids) > MAX_GROUP_PRODUCTS:
        raise CompetitorMatchingError(
            f'За один запуск можно проверить до {MAX_GROUP_PRODUCTS} товаров',
        )

    active = (
        BackgroundJob.query
        .filter(
            BackgroundJob.seller_id == seller_id,
            BackgroundJob.job_type == 'competitor_matching',
            BackgroundJob.status.in_(['pending', 'running']),
        )
        .order_by(BackgroundJob.created_at.desc()).limit(20).all()
    )
    for job in active:
        if job.get_progress().get('group_id') == group_id:
            return job

    ensure_global_match_rows(nm_ids)
    job = BackgroundJob(
        job_uid=uuid.uuid4().hex,
        seller_id=seller_id,
        job_type='competitor_matching',
        status='pending',
        total=len(nm_ids),
        processed=0,
        succeeded=0,
        failed_count=0,
    )
    job.set_progress({
        'group_id': group_id,
        'remaining_nm_ids': nm_ids,
        'results': [],
        'attempts': {},
        'llm_calls': 0,
        'cache_hits': 0,
    })
    db.session.add(job)
    db.session.commit()
    return job


def run_competitor_matching_tick(flask_app) -> dict[str, int]:
    """Process one bounded seller job from the singleton scheduler."""
    summary = {'jobs': 0, 'processed': 0, 'llm_calls': 0, 'cache_hits': 0}
    with flask_app.app_context():
        job = (
            BackgroundJob.query
            .filter(
                BackgroundJob.job_type == 'competitor_matching',
                BackgroundJob.status.in_(['pending', 'running']),
            )
            .order_by(BackgroundJob.created_at.asc()).first()
        )
        if not job:
            return summary
        summary['jobs'] = 1
        job.status = 'running'
        progress = job.get_progress()
        remaining = [
            value for value in progress.get('remaining_nm_ids', [])
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        ][:MAX_GROUP_PRODUCTS]
        results = progress.get('results')
        if not isinstance(results, list):
            results = []
        attempts = progress.get('attempts')
        if not isinstance(attempts, dict):
            attempts = {}
        llm_calls = int(progress.get('llm_calls') or 0)
        cache_hits = int(progress.get('cache_hits') or 0)
        db.session.commit()

        item_cap = _env_int(
            'COMPETITOR_MATCH_ITEMS_PER_TICK', DEFAULT_ITEMS_PER_TICK, 1, 10,
        )
        wall_seconds = _env_int(
            'COMPETITOR_MATCH_TICK_SECONDS', DEFAULT_TICK_SECONDS, 10, 55,
        )
        llm_cap = _env_int(
            'COMPETITOR_MATCH_LLM_MAX_PER_JOB',
            DEFAULT_MAX_LLM_CALLS_PER_JOB, 0, MAX_GROUP_PRODUCTS,
        )
        started = time.monotonic()
        handled = 0
        while remaining and handled < item_cap:
            if handled and time.monotonic() - started >= wall_seconds:
                break
            nm_id = remaining.pop(0)
            handled += 1
            try:
                outcome = process_global_match(
                    nm_id, allow_llm=llm_calls < llm_cap,
                )
            except Exception as error:
                logger.exception(
                    'Competitor matching failed for nmID %s (%s)',
                    nm_id, type(error).__name__,
                )
                outcome = {
                    'status': 'failed', 'nm_id': nm_id,
                    'llm_called': False, 'cache_hit': False,
                }
            if outcome.get('llm_called'):
                llm_calls += 1
                summary['llm_calls'] += 1
            if outcome.get('cache_hit'):
                cache_hits += 1
                summary['cache_hits'] += 1

            status = outcome.get('status')
            if status in {'waiting_observation', 'shared_in_progress'}:
                key = str(nm_id)
                attempts[key] = int(attempts.get(key) or 0) + 1
                if attempts[key] < WAIT_OBSERVATION_ATTEMPTS:
                    remaining.append(nm_id)
                    continue
                status = 'failed'
                outcome['status'] = status
                outcome['error_code'] = 'marketplace_facts_unavailable'

            job = BackgroundJob.query.filter_by(id=job.id).first()
            if not job:
                return summary
            job.processed += 1
            summary['processed'] += 1
            if status in {'completed', 'cached'}:
                job.succeeded += 1
            else:
                job.failed_count += 1
            results.append({
                key: value for key, value in outcome.items()
                if key in {
                    'status', 'nm_id', 'match_id',
                    'suggested_supplier_product_id', 'final_score',
                    'error_code',
                }
            })
            results = results[-MAX_GROUP_PRODUCTS:]
            progress.update({
                'remaining_nm_ids': remaining,
                'results': results,
                'attempts': attempts,
                'llm_calls': llm_calls,
                'cache_hits': cache_hits,
            })
            job.set_progress(progress)
            db.session.commit()

        job = BackgroundJob.query.filter_by(id=job.id).first()
        if job:
            progress.update({
                'remaining_nm_ids': remaining,
                'results': results,
                'attempts': attempts,
                'llm_calls': llm_calls,
                'cache_hits': cache_hits,
            })
            job.set_progress(progress)
            if not remaining:
                job.status = 'completed'
                job.set_result({
                    'group_id': progress.get('group_id'),
                    'matched': job.succeeded,
                    'failed': job.failed_count,
                    'llm_calls': llm_calls,
                    'cache_hits': cache_hits,
                    'shared_cache': True,
                    'source_scope': 'supplier_observed_only',
                })
            db.session.commit()
    return summary


def _supplier_card(product: SupplierProduct | None) -> dict[str, Any] | None:
    if not product:
        return None
    facts = _supplier_fact_pack(product)
    photos = list(facts.get('photos') or [])[:2]
    return {
        'id': product.id,
        'title': facts['title'],
        'brand': facts['brand'],
        'category': facts['category'],
        'vendor_code': facts['vendor_code'],
        'external_id': facts['external_id'],
        'photo_url': photos[0] if photos else None,
        'source': 'supplier_feed',
        'source_mode': facts['source_mode'],
    }


def supplier_identity_card(
    product: SupplierProduct | None,
) -> dict[str, Any] | None:
    """Public, source-boundary-safe supplier identity serializer.

    Cross-competitor read models must render the same observed supplier facts
    as matching itself.  Keeping this wrapper here prevents a comparison UI
    from accidentally falling back to seller-edited or AI-normalized fields.
    """
    return _supplier_card(product)


def _score_label(score: int, match_type: str | None) -> str:
    if match_type == 'different':
        return 'Не совпадает'
    if match_type == 'analog':
        return 'Возможный аналог'
    if score >= 88:
        return 'Почти точное совпадение'
    if score >= 72:
        return 'Высокая вероятность'
    if score >= 55:
        return 'Нужно проверить'
    return 'Мало данных'


def _match_type_label(value: str | None) -> str:
    return {
        'same': 'Тот же товар',
        'analog': 'Возможный аналог',
        'different': 'Не совпадает',
        'uncertain': 'Нужно проверить',
    }.get(value, 'Не определено')


def _llm_status_label(status: str | None, verdict: str | None) -> str:
    if verdict:
        return _match_type_label(verdict)
    return {
        'pending': 'ожидает проверки',
        'completed': 'проверено',
        'cached': 'из общего кэша',
        'unavailable': 'недоступна',
        'failed': 'не ответила',
        'skipped': 'не потребовалась',
    }.get(status, 'нет данных')


def serialize_group_matches(seller_id: int, group_id: int) -> dict[str, Any]:
    group = CompetitorGroup.query.filter_by(
        id=group_id, seller_id=seller_id,
    ).first()
    if not group:
        raise CompetitorMatchingError('Группа не найдена')
    competitors = (
        CompetitorProduct.query
        .filter_by(seller_id=seller_id, group_id=group_id, is_active=True)
        .order_by(CompetitorProduct.id.asc()).limit(MAX_GROUP_PRODUCTS).all()
    )
    nm_ids = [int(product.nm_id) for product in competitors]
    matches = {
        int(row.nm_id): row for row in CompetitorProductMatch.query.filter(
            CompetitorProductMatch.nm_id.in_(nm_ids)
        ).all()
    } if nm_ids else {}
    match_ids = [row.id for row in matches.values()]
    reviews = {
        row.match_id: row for row in SellerCompetitorMatchReview.query.filter(
            SellerCompetitorMatchReview.seller_id == seller_id,
            SellerCompetitorMatchReview.match_id.in_(match_ids),
        ).all()
    } if match_ids else {}

    supplier_ids = set()
    parsed_candidates: dict[int, list[dict[str, Any]]] = {}
    for match in matches.values():
        if match.suggested_supplier_product_id:
            supplier_ids.add(match.suggested_supplier_product_id)
        candidate_rows = _json_load(match.candidates_json, [])
        if not isinstance(candidate_rows, list):
            candidate_rows = []
        parsed_candidates[match.id] = candidate_rows[:MAX_CANDIDATES]
        for candidate in candidate_rows[:MAX_CANDIDATES]:
            if isinstance(candidate, dict) and isinstance(
                candidate.get('supplier_product_id'), int,
            ):
                supplier_ids.add(candidate['supplier_product_id'])
        review = reviews.get(match.id)
        if review and review.supplier_product_id:
            supplier_ids.add(review.supplier_product_id)
    suppliers = {
        row.id: row for row in SupplierProduct.query.filter(
            SupplierProduct.id.in_(supplier_ids)
        ).all()
    } if supplier_ids else {}

    imported_by_supplier: dict[int, ImportedProduct] = {}
    if supplier_ids:
        imported = (
            ImportedProduct.query
            .filter(
                ImportedProduct.seller_id == seller_id,
                ImportedProduct.supplier_product_id.in_(supplier_ids),
                ImportedProduct.product_id.isnot(None),
            )
            .order_by(ImportedProduct.id.desc()).all()
        )
        product_ids = [row.product_id for row in imported if row.product_id]
        own_products = {
            row.id: row for row in Product.query.filter(
                Product.seller_id == seller_id,
                Product.id.in_(product_ids),
            ).all()
        } if product_ids else {}
        for imported_row in imported:
            if (
                imported_row.supplier_product_id not in imported_by_supplier
                and imported_row.product_id in own_products
            ):
                imported_by_supplier[imported_row.supplier_product_id] = imported_row
    else:
        own_products = {}

    items = []
    for competitor in competitors:
        match = matches.get(int(competitor.nm_id))
        if not match:
            items.append({
                'competitor': competitor.to_dict(),
                'match': None,
                'effective_supplier': None,
                'own_product': None,
                'review': None,
            })
            continue
        review = reviews.get(match.id)
        effective_id = match.suggested_supplier_product_id
        effective_source = 'shared_suggestion'
        if review and review.status == 'confirmed':
            effective_id = review.supplier_product_id
            effective_source = 'seller_confirmed'
        elif review and review.status == 'rejected':
            effective_id = None
            effective_source = 'seller_rejected'
        supplier = suppliers.get(effective_id)
        imported_row = imported_by_supplier.get(effective_id)
        own = own_products.get(imported_row.product_id) if imported_row else None
        competitor_price = competitor.current_sale_price or competitor.current_price
        own_price = (
            float(own.discount_price or own.price)
            if own and (own.discount_price or own.price) else None
        )
        gap = None
        if own_price and competitor_price:
            gap = round((own_price - competitor_price) / competitor_price * 100, 1)
        candidates = []
        for candidate in parsed_candidates.get(match.id, []):
            supplier_id = candidate.get('supplier_product_id')
            if supplier_id not in suppliers:
                continue
            candidates.append({
                **{key: candidate.get(key) for key in (
                    'text_score', 'image_score', 'deterministic_score',
                    'evidence', 'image_evidence',
                )},
                'supplier': _supplier_card(suppliers[supplier_id]),
            })
        items.append({
            'competitor': competitor.to_dict(),
            'match': {
                'id': match.id,
                'status': match.processing_status,
                'match_type': match.predicted_match_type,
                'match_type_label': _match_type_label(
                    match.predicted_match_type),
                'score': match.final_score,
                'score_label': _score_label(
                    match.final_score, match.predicted_match_type,
                ),
                'text_score': match.text_score,
                'image_score': match.image_score,
                'llm_status': match.llm_status,
                'llm_verdict': match.llm_verdict,
                'llm_label': _llm_status_label(
                    match.llm_status, match.llm_verdict),
                'llm_reason': match.llm_reason,
                'evaluated_at': (
                    match.evaluated_at.isoformat() if match.evaluated_at else None
                ),
                'shared': True,
                'source_scope': 'supplier_observed_only',
                'candidates': candidates,
            },
            'effective_supplier': _supplier_card(supplier),
            'effective_source': effective_source,
            'own_product': ({
                'id': own.id,
                'nm_id': own.nm_id,
                'title': _bounded_text(own.title, 500),
                'price': float(own.price) if own.price is not None else None,
                'discount_price': (
                    float(own.discount_price)
                    if own.discount_price is not None else None
                ),
                'effective_price': own_price,
                'price_gap_percent': gap,
                'identity_source': 'exact_imported_product_fk',
            } if own else None),
            'review': ({
                'status': review.status,
                'match_type': review.match_type,
                'supplier_product_id': review.supplier_product_id,
                'reviewed_at': (
                    review.reviewed_at.isoformat() if review.reviewed_at else None
                ),
            } if review else None),
        })
    active_job = None
    for candidate_job in (
        BackgroundJob.query
        .filter(
            BackgroundJob.seller_id == seller_id,
            BackgroundJob.job_type == 'competitor_matching',
            BackgroundJob.status.in_(['pending', 'running']),
        )
        .order_by(BackgroundJob.created_at.desc()).limit(20).all()
    ):
        if candidate_job.get_progress().get('group_id') == group_id:
            active_job = candidate_job.to_dict()
            break
    return {
        'group_id': group_id,
        'items': items,
        'active_job': active_job,
        'shared_cache': True,
        'source_scope': 'supplier_observed_only',
        'limit': MAX_GROUP_PRODUCTS,
    }


def review_match(
    seller_id: int,
    actor_user_id: int | None,
    match_id: int,
    *,
    action: str,
    supplier_product_id: int | None = None,
    match_type: str | None = None,
) -> SellerCompetitorMatchReview | None:
    match = CompetitorProductMatch.query.filter_by(id=match_id).first()
    if not match:
        raise CompetitorMatchingError('Сопоставление не найдено')
    owns_target = db.session.query(CompetitorProduct.id).filter(
        CompetitorProduct.seller_id == seller_id,
        CompetitorProduct.nm_id == match.nm_id,
        CompetitorProduct.is_active.is_(True),
    ).first()
    if not owns_target:
        raise CompetitorMatchingError('Сопоставление не найдено')
    review = SellerCompetitorMatchReview.query.filter_by(
        seller_id=seller_id, match_id=match_id,
    ).first()
    previous_id = review.supplier_product_id if review else None

    if action == 'reset':
        if review:
            db.session.delete(review)
        db.session.add(CompetitorMatchEvent(
            seller_id=seller_id, match_id=match_id,
            previous_supplier_product_id=previous_id,
            supplier_product_id=None, action='reset', match_type=None,
            actor_user_id=actor_user_id,
            evidence_json=_json_dump({'scope': 'seller_local'}),
        ))
        db.session.commit()
        return None

    if action not in {'confirm', 'reject'}:
        raise CompetitorMatchingError('Некорректное действие')
    if action == 'confirm':
        if (
            isinstance(supplier_product_id, bool)
            or not isinstance(supplier_product_id, int)
            or supplier_product_id <= 0
        ):
            raise CompetitorMatchingError('Выберите товар поставщика')
        if not SupplierProduct.query.filter_by(id=supplier_product_id).first():
            raise CompetitorMatchingError('Товар поставщика не найден')
        if match_type not in {'same', 'analog'}:
            raise CompetitorMatchingError('Укажите тип совпадения')
    else:
        supplier_product_id = None
        match_type = None

    if not review:
        review = SellerCompetitorMatchReview(
            seller_id=seller_id, match_id=match_id,
        )
        db.session.add(review)
    review.status = 'confirmed' if action == 'confirm' else 'rejected'
    review.supplier_product_id = supplier_product_id
    review.match_type = match_type
    review.actor_user_id = actor_user_id
    review.reviewed_at = datetime.utcnow()
    db.session.add(CompetitorMatchEvent(
        seller_id=seller_id, match_id=match_id,
        previous_supplier_product_id=previous_id,
        supplier_product_id=supplier_product_id,
        action=action, match_type=match_type,
        actor_user_id=actor_user_id,
        evidence_json=_json_dump({
            'scope': 'seller_local',
            'shared_suggestion_id': match.suggested_supplier_product_id,
        }),
    ))
    db.session.commit()
    return review


def search_supplier_products(query: str, limit: int = 20) -> list[dict[str, Any]]:
    query = _bounded_text(query, 120)
    if len(query) < 2:
        return []
    escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    pattern = f'%{escaped}%'
    has_raw = and_(
        SupplierProduct.original_data_json.isnot(None),
        SupplierProduct.original_data_json.notin_(('', '{}')),
    )
    # Search the same source boundary that is rendered.  Normalized columns
    # remain searchable only for legacy rows with no observed snapshot.
    rows = SupplierProduct.query.filter(or_(
        and_(
            has_raw,
            SupplierProduct.original_data_json.ilike(pattern, escape='\\'),
        ),
        and_(
            ~has_raw,
            or_(
                SupplierProduct.title.ilike(pattern, escape='\\'),
                SupplierProduct.brand.ilike(pattern, escape='\\'),
                SupplierProduct.vendor_code.ilike(pattern, escape='\\'),
                SupplierProduct.external_id.ilike(pattern, escape='\\'),
            ),
        ),
    )).order_by(SupplierProduct.id.asc()).limit(
        max(1, min(30, limit)),
    ).all()
    return [_supplier_card(row) for row in rows]
