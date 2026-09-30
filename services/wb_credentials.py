"""Bounded, unverified expiry hints, never authentication/permission evidence."""

import base64
import binascii
from datetime import datetime, timezone
import json
import math
import time


def _payload(api_key):
    if not isinstance(api_key, str) or len(api_key) > 16384:
        return None
    parts = api_key.strip().split('.')
    if len(parts) != 3 or not all(parts):
        return None
    try:
        payload = json.loads(base64.b64decode(
            parts[1] + '=' * (-len(parts[1]) % 4), altchars=b'-_', validate=True,
        ))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, TypeError, binascii.Error, OverflowError, RecursionError):
        return {}


def token_expiry_hint(api_key: str):
    """Unverified future expiry never proves identity or permission."""
    try:
        expiry = (_payload(api_key) or {}).get('exp')
        if (
            isinstance(expiry, bool) or not isinstance(expiry, (int, float))
            or not math.isfinite(expiry) or not 0 <= expiry < 253402300800
        ):
            return None
        return expiry
    except (ValueError, TypeError, binascii.Error, OverflowError, RecursionError):
        return None


def token_needs_stock_upgrade(api_key: str) -> bool:
    """Known Base/Test token types cannot use the new Analytics stock method."""
    kind = (_payload(api_key) or {}).get('acc')
    return type(kind) is int and kind in (1, 2)


def token_is_expired(api_key: str, *, now=None) -> bool:
    expiry = token_expiry_hint(api_key)
    return expiry is not None and expiry <= (time.time() if now is None else now)


def token_public_status(api_key: str, *, now=None) -> dict:
    expiry = token_expiry_hint(api_key)
    return {
        'configured': bool(api_key),
        'expired': token_is_expired(api_key, now=now),
        'expires_at': (
            datetime.fromtimestamp(expiry, timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
            if expiry is not None else None
        ),
    }
