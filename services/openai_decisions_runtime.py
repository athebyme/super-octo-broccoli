"""Fail-closed runtime wiring for the isolated OpenAI Decisions service.

This factory is intentionally separate from native Flash and OpenAI Chat
configuration. It reads one dedicated Decisions key, reuses the existing
explicit AI_PROXY, and stores its private reservation ledger in seller data.
It does not log configuration values or perform network or database work.
"""

from collections.abc import Mapping
import os
from typing import Optional
from urllib.parse import urlsplit

from services.openai_decisions import OpenAIDecisionsClient
from services.openai_decisions_cache import OpenAIDecisionsCache
from services.openai_decisions_pipeline import OpenAIDecisionsPipeline


OPENAI_DECISIONS_ENABLED_ENV = "OPENAI_DECISIONS_ENABLED"
OPENAI_DECISIONS_API_KEY_ENV = "OPENAI_DECISIONS_API_KEY"
OPENAI_DECISIONS_DAILY_LIMIT_ENV = "OPENAI_DECISIONS_MAX_CALLS_PER_DAY"
OPENAI_DECISIONS_DEFAULT_DAILY_LIMIT = 100
OPENAI_DECISIONS_MAX_DAILY_LIMIT = 1000
OPENAI_DECISIONS_CACHE_PATH = "/app/data/openai_decisions/decisions.sqlite3"


def _is_enabled(environ: Mapping) -> bool:
    value = environ.get(OPENAI_DECISIONS_ENABLED_ENV)
    if type(value) is not str:
        return False
    return value.strip().lower() in {"1", "true"}


def _valid_api_key(value) -> bool:
    if type(value) is not str or not value or len(value) > 4096:
        return False
    try:
        value.encode("ascii")
    except UnicodeError:
        return False
    return not any(char.isspace() or ord(char) == 0x7f for char in value)


def _valid_proxy(value) -> bool:
    if type(value) is not str or not value or len(value) > 4096:
        return False
    if value != value.strip() or any(
        char.isspace() or ord(char) == 0x7f for char in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https", "socks5", "socks5h"}
            or not parsed.hostname
            or parsed.fragment
        ):
            return False
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return port is None or 1 <= port <= 65535


def _daily_limit(environ: Mapping) -> Optional[int]:
    raw = environ.get(
        OPENAI_DECISIONS_DAILY_LIMIT_ENV,
        str(OPENAI_DECISIONS_DEFAULT_DAILY_LIMIT),
    )
    if (
        type(raw) is not str
        or not raw
        or not raw.isascii()
        or not raw.isdecimal()
        or len(raw) > 4
    ):
        return None
    value = int(raw)
    if not 1 <= value <= OPENAI_DECISIONS_MAX_DAILY_LIMIT:
        return None
    return value


def create_openai_decisions_pipeline(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[OpenAIDecisionsPipeline]:
    """Build the Decisions pipeline only when its explicit config is complete.

    Disabled or incomplete configuration returns ``None`` before creating a
    cache/client object. The SQLite file itself is created lazily by the cache
    on the first valid planned request, inside the existing ``/app/data``
    named volume. All failures are silent and fail closed.
    """

    config = os.environ if environ is None else environ
    if not isinstance(config, Mapping) or not _is_enabled(config):
        return None

    api_key = config.get(OPENAI_DECISIONS_API_KEY_ENV)
    proxy_url = config.get("AI_PROXY")
    daily_limit = _daily_limit(config)
    if (
        not _valid_api_key(api_key)
        or not _valid_proxy(proxy_url)
        or daily_limit is None
    ):
        return None

    try:
        cache = OpenAIDecisionsCache(
            OPENAI_DECISIONS_CACHE_PATH,
            max_calls_per_day=daily_limit,
        )
        client = OpenAIDecisionsClient(
            api_key=api_key,
            proxy_url=proxy_url,
        )
        return OpenAIDecisionsPipeline(cache=cache, client=client)
    except Exception:
        # Constructor failures may carry a key or proxy URL in their message.
        # Do not log or return raw exception details.
        return None


__all__ = [
    "OPENAI_DECISIONS_CACHE_PATH",
    "OPENAI_DECISIONS_DEFAULT_DAILY_LIMIT",
    "OPENAI_DECISIONS_DAILY_LIMIT_ENV",
    "OPENAI_DECISIONS_ENABLED_ENV",
    "OPENAI_DECISIONS_API_KEY_ENV",
    "OPENAI_DECISIONS_MAX_DAILY_LIMIT",
    "create_openai_decisions_pipeline",
]
