"""Helpers for history snapshots based on the exact WB wire state."""

from copy import deepcopy
from typing import Any, Dict, Mapping


def overlay_snapshot_with_wb_card(
    local_snapshot: Mapping[str, Any],
    wb_card: Mapping[str, Any],
) -> Dict[str, Any]:
    """Overlay WB-owned fields with values from a fetched/prepared full card.

    Local ORM data can lag behind WB. History used for conflict-aware rollback
    must therefore store the value that was actually read from and sent to WB.
    """
    snapshot = deepcopy(dict(local_snapshot or {}))
    if not isinstance(wb_card, Mapping):
        return snapshot

    scalar_fields = {
        'nmID': 'nm_id',
        'vendorCode': 'vendor_code',
        'title': 'title',
        'brand': 'brand',
        'description': 'description',
    }
    for wb_name, snapshot_name in scalar_fields.items():
        if wb_name in wb_card:
            snapshot[snapshot_name] = deepcopy(wb_card[wb_name])

    for field in ('characteristics', 'dimensions'):
        if field in wb_card:
            snapshot[field] = deepcopy(wb_card[field])
    return snapshot
