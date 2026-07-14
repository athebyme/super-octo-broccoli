"""Idempotent ORM seed for local/new marketplace installations."""

import json

from models import Marketplace, db
from services.marketplace_adapters import get_marketplace_registry
from services.ozon_api_client import OZON_API_BASE_URL


MARKETPLACE_DEFINITIONS = {
    "wb": {
        "name": "Wildberries",
        "api_base_url": "https://content-api.wildberries.ru",
        "api_version": "v2",
    },
    "ozon": {
        "name": "Ozon",
        "api_base_url": OZON_API_BASE_URL,
        "api_version": None,
    },
}


def ensure_marketplace_definitions(*, commit: bool = True) -> dict:
    """Create missing static provider definitions without touching secrets."""
    registry = get_marketplace_registry()
    created = 0
    updated = 0
    for code, definition in MARKETPLACE_DEFINITIONS.items():
        marketplace = Marketplace.query.filter_by(code=code).first()
        if marketplace is None:
            marketplace = Marketplace(
                code=code,
                name=definition["name"],
                api_base_url=definition["api_base_url"],
                api_version=definition["api_version"],
                adapter_code=code,
                is_active=True,
            )
            db.session.add(marketplace)
            created += 1
        changed = False
        if not marketplace.adapter_code:
            marketplace.adapter_code = code
            changed = True
        if not marketplace.api_base_url:
            marketplace.api_base_url = definition["api_base_url"]
            changed = True
        if not marketplace.capability_versions_json:
            marketplace.capability_versions_json = json.dumps(
                registry.get(code).endpoint_versions,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            changed = True
        if changed and marketplace.id is not None:
            updated += 1
    if commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    return {"created": created, "updated": updated}
