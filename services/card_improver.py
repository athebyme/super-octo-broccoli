# -*- coding: utf-8 -*-
"""Read-only proposal helpers for the legacy card-quality screen.

Provider writes that historically lived here were unsafe full replacements:
they trusted browser/agent values, used ``cards/update`` or ``media/save``
directly and treated HTTP acceptance as success.  Seller-facing supplier and
photo actions now go through :mod:`services.supplier_enrichment`, which owns
fresh live merge decisions, durable receipts and read-only reconciliation.

The two ``apply_*`` functions remain as fail-closed compatibility shims so an
old import cannot silently restore the destructive path.
"""

import json
import logging
from typing import Any, Dict, List


logger = logging.getLogger('card_improver')

ALLOWED_FIELDS = {
    'title', 'brand', 'description', 'characteristics', 'dimensions',
    'subject_id', 'photos',
}

_GENERATIVE_FIELD_MAP: Dict[str, List] = {
    'seo-writer': [('title', 'title'), ('description', 'description')],
    'brand-resolver': [('brand', 'brand')],
    'category-mapper': [('subject_id', 'category')],
    'characteristics-filler': [('characteristics', 'characteristics')],
}

LEGACY_WRITE_DISABLED = (
    'legacy_card_improver_write_disabled: используйте preserve-live '
    'supplier enrichment'
)


def apply_card_updates(
    product,
    updates: Dict[str, Any],
    seller,
    wb_client,
    source: str = 'card-quality',
) -> Dict[str, Any]:
    """Fail closed instead of issuing an unjournaled WB full replacement."""
    old_quality = getattr(product, 'quality_score', None)
    logger.warning(
        '[Improve/%s] blocked legacy provider write for product=%s',
        source,
        getattr(product, 'id', None),
    )
    return {
        'success': False,
        'fields_applied': [],
        'old_quality': old_quality,
        'new_quality': old_quality,
        'wb_sync': False,
        'error': LEGACY_WRITE_DISABLED,
    }


def apply_card_updates_bulk(
    items: List,
    seller,
    wb_client,
    source: str = 'bulk',
) -> Dict[int, Dict[str, Any]]:
    """Fail closed per row; retained only for old callers during rollout."""
    results: Dict[int, Dict[str, Any]] = {}
    for product, _updates in items or []:
        results[getattr(product, 'id', None)] = apply_card_updates(
            product,
            {},
            seller,
            wb_client,
            source=source,
        )
    return results


def collect_weak_dimensions(detail: Dict[str, Any]) -> List[str]:
    """Return warning/error dimensions ordered by descending impact."""
    dims = (detail or {}).get('dimensions') or {}
    weak = []
    for name, data in dims.items():
        if data.get('status') in ('warning', 'error'):
            impact = data.get('weight', 0) * (100 - data.get('score', 0))
            weak.append((impact, name))
    weak.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in weak]


def build_proposal_from_tasks(
    product,
    task_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Map completed agent results to a review-only proposal."""
    proposal: Dict[str, Any] = {}

    try:
        current_photos = json.loads(
            getattr(product, 'photos_json', None) or '[]'
        )
    except (json.JSONDecodeError, TypeError):
        current_photos = []
    if not isinstance(current_photos, list):
        current_photos = []

    for entry in task_results or []:
        agent = entry.get('agent')
        result = entry.get('result') or {}

        if agent == 'photo-optimizer':
            order = result.get('recommended_order')
            if isinstance(order, list) and current_photos:
                seen = set()
                valid = []
                for index in order:
                    if (
                        isinstance(index, int)
                        and not isinstance(index, bool)
                        and 0 <= index < len(current_photos)
                        and index not in seen
                    ):
                        seen.add(index)
                        valid.append(index)
                valid.extend(
                    index for index in range(len(current_photos))
                    if index not in seen
                )
                reordered = [current_photos[index] for index in valid]
                if reordered != current_photos:
                    proposal['photos'] = {
                        'current': current_photos,
                        'proposed': reordered,
                        'dimension': 'photos',
                        'source': 'photo-optimizer',
                    }

        if agent in _GENERATIVE_FIELD_MAP:
            confidence = result.get('confidence', 1.0)
            if (
                isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and confidence >= 0.7
            ):
                for field, dimension in _GENERATIVE_FIELD_MAP[agent]:
                    proposed = result.get(field)
                    current = (
                        getattr(product, field, None)
                        if field != 'characteristics' else None
                    )
                    if proposed in (None, '', [], {}):
                        continue
                    if field != 'characteristics' and proposed == current:
                        continue
                    proposal[field] = {
                        'current': current,
                        'proposed': proposed,
                        'dimension': dimension,
                        'source': agent,
                    }

    return proposal
