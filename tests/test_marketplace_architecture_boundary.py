# -*- coding: utf-8 -*-
"""Architecture guard: Ozon HTTP calls live only in its typed transport."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ozon_base_url_is_not_called_outside_typed_transport():
    offenders = []
    allowed = ROOT / "services" / "ozon_api_client.py"
    for folder in (ROOT / "routes", ROOT / "services", ROOT / "agents"):
        for path in folder.rglob("*.py"):
            if path == allowed:
                continue
            if "api-seller.ozon.ru" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
