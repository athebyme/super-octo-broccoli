#!/usr/bin/env python3
"""Read-only, secret-safe live contract probe for one Ozon seller cabinet.

The script intentionally has no write mode and calls only endpoint manifest
entries classified as ``read``.  It prints response *shapes*, never response
values, product identities, credentials, or raw provider errors.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Dict, Iterable, Mapping, Optional

from services.marketplace_adapters.types import MarketplaceCredentials
from services.ozon_api_client import (
    OZON_ENDPOINTS,
    OzonAPIError,
    OzonSellerAPIClient,
)


DEFAULT_ENV_FILE = Path("/tmp/ozon_live.env")
ALLOWED_ENV_KEYS = frozenset({"OZON_LIVE_CLIENT_ID", "OZON_LIVE_API_KEY"})
BASE_READ_PROBES = (
    ("roles", {}),
    ("seller_info", {}),
    ("product_operation_limits", {}),
    (
        "product_list",
        {
            "filter": {
                "offer_id": [],
                "product_id": [],
                "visibility": "ALL",
            },
            "last_id": "",
            "limit": 1,
        },
    ),
    ("warehouses", {"cursor": "", "limit": 100}),
)


class ProbeConfigurationError(RuntimeError):
    pass


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ProbeConfigurationError("Ozon live env contains an invalid value")
    return value


def read_secret_env(path: Path) -> Dict[str, str]:
    """Read two exact keys from a regular owner-only file without shell eval."""
    try:
        file_stat = path.stat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(file_stat.st_mode):
        raise ProbeConfigurationError("Ozon live env must be a regular file")
    if file_stat.st_uid != os.getuid():
        raise ProbeConfigurationError("Ozon live env must belong to current user")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ProbeConfigurationError("Ozon live env permissions must be 0600")
    if file_stat.st_size > 8192:
        raise ProbeConfigurationError("Ozon live env is unexpectedly large")

    result: Dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, separator, raw_value = line.partition("=")
            key = key.strip()
            if not separator or key not in ALLOWED_ENV_KEYS:
                raise ProbeConfigurationError(
                    f"Unsupported entry in Ozon live env at line {line_number}"
                )
            if key in result:
                raise ProbeConfigurationError(
                    f"Duplicate entry in Ozon live env at line {line_number}"
                )
            result[key] = _unquote_env_value(raw_value)
    return result


def load_credentials(path: Path = DEFAULT_ENV_FILE) -> MarketplaceCredentials:
    file_values = read_secret_env(path)
    client_id = os.environ.get("OZON_LIVE_CLIENT_ID") or file_values.get(
        "OZON_LIVE_CLIENT_ID"
    )
    api_key = os.environ.get("OZON_LIVE_API_KEY") or file_values.get(
        "OZON_LIVE_API_KEY"
    )
    if not client_id or not api_key:
        raise ProbeConfigurationError(
            "Set OZON_LIVE_CLIENT_ID and OZON_LIVE_API_KEY in an owner-only env file"
        )
    return MarketplaceCredentials(
        external_account_id=client_id,
        api_key=api_key,
    )


def response_shape(value: Any, *, depth: int = 0) -> Any:
    """Return bounded structural metadata with no scalar provider values."""
    if depth >= 7:
        return {"type": "depth_limit"}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {
            "type": "array",
            "count": len(value),
            "sample_shapes": [
                response_shape(item, depth=depth + 1)
                for item in value[:2]
            ],
        }
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value)
        if len(keys) > 500:
            return {"type": "object", "field_count": len(keys), "too_large": True}
        return {
            "type": "object",
            "fields": {
                key: response_shape(value[key], depth=depth + 1)
                for key in keys
            },
        }
    return {"type": "unsupported"}


def _first_product_identity(response: Any) -> Dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    result = response.get("result")
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return {}
    item = items[0]
    product_id = item.get("product_id")
    if isinstance(product_id, bool) or not isinstance(product_id, (int, str)):
        product_id = None
    offer_id = item.get("offer_id")
    if not isinstance(offer_id, str) or not offer_id.strip():
        offer_id = None
    sku = item.get("sku")
    if isinstance(sku, bool) or not isinstance(sku, (int, str)):
        sku = None
    return {
        "product_id": product_id,
        "offer_id": offer_id,
        "sku": sku,
    }


def _product_read_probes(identity: Mapping[str, Any]) -> Iterable[tuple]:
    product_id = identity.get("product_id")
    if product_id is None:
        return ()
    common_filter = {
        "filter": {"product_id": [product_id], "visibility": "ALL"},
        "limit": 1,
    }
    probes = [
        ("product_info_list", {"product_id": [product_id]}),
        (
            "product_attributes",
            {**common_filter, "last_id": ""},
        ),
        ("product_prices", {**common_filter, "cursor": ""}),
        ("product_stocks", {**common_filter, "cursor": ""}),
        ("product_pictures_info", {"product_id": [product_id]}),
    ]
    sku = identity.get("sku")
    if sku is not None:
        # Both calls are read-only. Their exact July 2026 envelopes are what
        # this staging probe is intended to confirm before write support ships.
        probes.extend((
            ("product_stocks_by_warehouse_fbs", {"sku": [sku]}),
            ("product_stocks_by_warehouse_fbo", {"sku": [sku]}),
        ))
    return tuple(probes)


def run_probe(client: OzonSellerAPIClient) -> Dict[str, Any]:
    results = []
    identity: Dict[str, Any] = {}
    probes = list(BASE_READ_PROBES)
    position = 0
    while position < len(probes):
        endpoint_name, payload = probes[position]
        position += 1
        spec = OZON_ENDPOINTS.get(endpoint_name)
        if spec is None or spec.retry_class != "read":
            raise ProbeConfigurationError(
                f"Probe endpoint {endpoint_name!r} is not classified read-only"
            )
        try:
            response = client.request(endpoint_name, payload)
        except OzonAPIError as exc:
            results.append({
                "endpoint": endpoint_name,
                "path": spec.path,
                "ok": False,
                "status_code": exc.status_code,
                "error_code": exc.code,
                "provider_request_id": exc.request_id,
            })
            continue
        results.append({
            "endpoint": endpoint_name,
            "path": spec.path,
            "ok": True,
            "shape": response_shape(response),
        })
        if endpoint_name == "product_list":
            identity = _first_product_identity(response)
            probes.extend(_product_read_probes(identity))
    return {
        "mode": "read_only",
        "write_endpoint_count": 0,
        "product_sample_found": bool(identity.get("product_id")),
        "results": results,
    }


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe current Ozon read response shapes without exposing data",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="owner-only file with OZON_LIVE_CLIENT_ID/OZON_LIVE_API_KEY",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    try:
        credentials = load_credentials(args.env_file)
        client = OzonSellerAPIClient(credentials, read_retries=1)
        result = run_probe(client)
    except ProbeConfigurationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(item["ok"] for item in result["results"]) else 3


if __name__ == "__main__":
    sys.exit(main())
