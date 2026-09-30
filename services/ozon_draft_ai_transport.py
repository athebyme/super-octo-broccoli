"""One bounded native DeepSeek Flash parsing attempt, without ORM or retries.

The caller must reserve a durable AIParsingAttempt before invoking this module.
No response body or prompt is logged or persisted here. A network exception is
uncertain because the provider may have processed the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import socket
import threading
from typing import Any, Mapping, Optional, Sequence

import requests


NATIVE_BASE_URL = "https://api.deepseek.com/v1"
NATIVE_MODEL = "deepseek-flash"
MAX_REQUEST_BYTES = 64 * 1024
MAX_ADMIN_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_TOKENS = 8000
MAX_HTTP_SECONDS = 60
MAX_SQLITE_INTEGER = (1 << 63) - 1
_PHYSICAL_BULKHEAD = threading.BoundedSemaphore(3)
_PERMIT_FACTORY_TOKEN = object()


class FlashConfigurationError(ValueError):
    """A local parsing profile is unsafe or incomplete; no HTTP was attempted."""


class FlashPermit:
    """One process-local physical slot, transferred exactly once to a reader.

    The reserving coordinator may release an unused permit. After transfer,
    only the transport reader may release it, including after a parent timeout.
    """

    def __init__(self, token):
        if token is not _PERMIT_FACTORY_TOKEN:
            raise ValueError("invalid_flash_permit")
        self._state = "held"
        self._lock = threading.Lock()

    def release(self) -> bool:
        """Release before transport ownership; a transferred slot stays held."""
        with self._lock:
            if self._state != "held":
                return False
            self._state = "released"
            _PHYSICAL_BULKHEAD.release()
            return True

    def _transfer(self) -> None:
        with self._lock:
            if self._state != "held":
                raise ValueError("invalid_flash_permit")
            self._state = "transferred"

    def _release_from_reader(self) -> None:
        with self._lock:
            if self._state != "transferred":
                return
            self._state = "released"
            _PHYSICAL_BULKHEAD.release()


def try_acquire_flash_permit() -> Optional[FlashPermit]:
    """Admit physical capacity before a coordinator reserves durable budget."""
    if not _PHYSICAL_BULKHEAD.acquire(blocking=False):
        return None
    return FlashPermit(_PERMIT_FACTORY_TOKEN)


@dataclass(frozen=True)
class FlashUsage:
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cache_hit_tokens: Optional[int] = None
    cache_miss_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None


@dataclass(frozen=True)
class FlashOutcome:
    # success, rate_limited, http_error, invalid_response, unknown_response
    kind: str
    content: Optional[Any] = None
    http_status: Optional[int] = None
    retry_after_seconds: Optional[float] = None
    usage: FlashUsage = FlashUsage()
    safe_code: Optional[str] = None


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite_json_number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_json_number")
    return number


def _strict_json(raw: bytes | str) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_no_duplicate_keys,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )


def _count(value: Any) -> Optional[int]:
    return value if type(value) is int and 0 <= value <= MAX_SQLITE_INTEGER else None


def _usage(payload: Any) -> FlashUsage:
    if not isinstance(payload, dict):
        return FlashUsage()
    details = payload.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    completion_details = payload.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    return FlashUsage(
        prompt_tokens=_count(payload.get("prompt_tokens")),
        completion_tokens=_count(payload.get("completion_tokens")),
        cache_hit_tokens=_count(payload.get("prompt_cache_hit_tokens"))
        if "prompt_cache_hit_tokens" in payload
        else _count(details.get("cached_tokens")),
        cache_miss_tokens=_count(payload.get("prompt_cache_miss_tokens")),
        reasoning_tokens=_count(completion_details.get("reasoning_tokens")),
    )


def parse_retry_after(value: Optional[str], *, now: Optional[datetime] = None) -> float:
    """Return the full observed Retry-After delay; malformed/missing means 60s."""
    if not value or len(value) > 128:
        return 60.0
    value = value.strip()
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except ValueError:
        pass
    try:
        due = parsedate_to_datetime(value)
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (due - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 60.0


def _validate_profile(config: Any) -> str:
    provider = getattr(config, "provider", None)
    provider = getattr(provider, "value", provider)
    key = getattr(config, "api_key", None)
    if (
        provider != "deepseek"
        or getattr(config, "model", None) != NATIVE_MODEL
        or getattr(config, "api_base_url", None) != NATIVE_BASE_URL
        or type(key) is not str
        or not key.strip()
        or type(getattr(config, "max_retries", None)) is not int
        or config.max_retries != 1
        or getattr(config, "proxy_enabled", None) is not False
        or getattr(config, "task_profile", None) not in (
            "supplier_parsing_flash", "seller_draft_completion_flash",
        )
        or len(key) > 1024
        or any(ord(character) < 33 or ord(character) == 127 for character in key)
    ):
        raise FlashConfigurationError("native_flash_profile_required")
    return key


def _request_payload(messages: Sequence[Mapping[str, str]], max_output_tokens: int,
                     request_limit: int) -> bytes:
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise FlashConfigurationError("output_token_limit")
    if not isinstance(messages, (tuple, list)) or not 1 <= len(messages) <= 12:
        raise FlashConfigurationError("invalid_messages")
    normalized = []
    for message in messages:
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise FlashConfigurationError("invalid_messages")
        role, content = message["role"], message["content"]
        if role not in ("system", "user") or type(content) is not str or not content:
            raise FlashConfigurationError("invalid_messages")
        normalized.append({"role": role, "content": content})
    payload = {
        "model": NATIVE_MODEL,
        "messages": normalized,
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": max_output_tokens,
    }
    try:
        raw = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise FlashConfigurationError("invalid_request_encoding") from None
    if len(raw) > request_limit:
        raise FlashConfigurationError("request_too_large")
    return raw


def validate_flash_request(config: Any, messages: Sequence[Mapping[str, str]],
                           *, max_output_tokens: int = MAX_OUTPUT_TOKENS) -> None:
    """Fail local profile/size checks before the durable physical reservation."""
    _validate_profile(config)
    request_limit = (MAX_ADMIN_REQUEST_BYTES
                     if config.task_profile == "supplier_parsing_flash"
                     else MAX_REQUEST_BYTES)
    _request_payload(messages, max_output_tokens, request_limit)


def _flash_completion_sync(
    config: Any,
    messages: Sequence[Mapping[str, str]],
    *,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    session: Optional[requests.Session] = None,
    now: Optional[datetime] = None,
    _abort: Optional[threading.Event] = None,
    _handles: Optional[dict] = None,
) -> FlashOutcome:
    """Perform exactly one physical request after the caller's durable claim.

    The returned content is parsed JSON, never raw provider text. The caller
    must treat ``unknown_response`` as consumed budget and never auto-replay it.
    """
    key = _validate_profile(config)
    request_limit = (MAX_ADMIN_REQUEST_BYTES
                     if config.task_profile == "supplier_parsing_flash"
                     else MAX_REQUEST_BYTES)
    raw_request = _request_payload(messages, max_output_tokens, request_limit)
    if isinstance(session, requests.Session):
        raise FlashConfigurationError("external_requests_session_not_allowed")
    owned_session = session is None
    client = session if session is not None else requests.Session()
    # Environment HTTP(S)_PROXY and netrc must not change this native endpoint.
    client.trust_env = False
    client.proxies = {}
    if _handles is not None:
        _handles["client"] = client
    response = None
    try:
        if _abort is not None and _abort.is_set():
            return FlashOutcome("unknown_response", safe_code="ai_total_timeout")
        response = client.post(
            NATIVE_BASE_URL + "/chat/completions",
            data=raw_request,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=(5, MAX_HTTP_SECONDS - 5),
            allow_redirects=False,
            stream=True,
        )
        if _handles is not None:
            _handles["response"] = response
        if _abort is not None and _abort.is_set():
            return FlashOutcome("unknown_response", safe_code="ai_total_timeout")
        status = response.status_code
        if status == 429:
            return FlashOutcome(
                "rate_limited", http_status=429,
                retry_after_seconds=parse_retry_after(response.headers.get("Retry-After"), now=now),
                safe_code="ai_rate_limited",
            )
        if status != 200:
            return FlashOutcome("http_error", http_status=status, safe_code="ai_provider_http_error")
        chunks = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if _abort is not None and _abort.is_set():
                return FlashOutcome("unknown_response", safe_code="ai_total_timeout")
            if not chunk:
                continue
            if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                return FlashOutcome("invalid_response", http_status=200, safe_code="ai_response_too_large")
            chunks.extend(chunk)
        observed_usage = FlashUsage()
        try:
            envelope = _strict_json(bytes(chunks))
            if not isinstance(envelope, dict):
                raise ValueError("response_envelope")
            observed_usage = _usage(envelope.get("usage"))
            choices = envelope.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("response_choices")
            choice = choices[0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError("response_message")
            if choice.get("finish_reason") != "stop":
                raise ValueError("response_incomplete")
            content = choice["message"].get("content")
            if type(content) is not str or len(content.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise ValueError("response_content")
            parsed = _strict_json(content)
            if not isinstance(parsed, (dict, list)):
                raise ValueError("response_content_shape")
            return FlashOutcome("success", content=parsed, http_status=200, usage=observed_usage)
        except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
            return FlashOutcome("invalid_response", http_status=200, safe_code="ai_invalid_json",
                                usage=observed_usage)
    except requests.RequestException:
        return FlashOutcome("unknown_response", safe_code="ai_response_unknown")
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        if owned_session:
            try:
                client.close()
            except Exception:
                pass


def flash_completion(
    config: Any,
    messages: Sequence[Mapping[str, str]],
    *,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    session: Optional[requests.Session] = None,
    now: Optional[datetime] = None,
    permit: Optional[FlashPermit] = None,
    _total_timeout_seconds: float = MAX_HTTP_SECONDS,
) -> FlashOutcome:
    """Return within the caller deadline even if a peer dribbles response bytes.

    A timed-out request is uncertain: the reader may take longer to exit. Its
    process-local bulkhead slot is released only by that reader, and the shared
    ledger keeps a 120-second reservation fence. No late reader can commit an
    outcome or cause an automatic replay.
    """
    if permit is not None and type(permit) is not FlashPermit:
        raise ValueError("invalid_flash_permit")
    # Release a pre-admitted slot if local validation prevents any HTTP work.
    try:
        validate_flash_request(config, messages, max_output_tokens=max_output_tokens)
        if (type(_total_timeout_seconds) not in (int, float)
                or not 0 < _total_timeout_seconds <= MAX_HTTP_SECONDS):
            raise ValueError("invalid_total_timeout")
    except Exception:
        if permit is not None:
            permit.release()
        raise
    if permit is None:
        permit = try_acquire_flash_permit()
        if permit is None:
            return FlashOutcome("unknown_response", safe_code="ai_local_capacity_unknown")
    permit._transfer()
    result = []
    done = threading.Event()
    abort = threading.Event()
    handles = {}

    def _run():
        try:
            result.append(_flash_completion_sync(
                config, messages, max_output_tokens=max_output_tokens,
                session=session, now=now, _abort=abort, _handles=handles,
            ))
        except Exception:
            result.append(FlashOutcome("unknown_response", safe_code="ai_response_unknown"))
        finally:
            permit._release_from_reader()
            done.set()

    try:
        worker = threading.Thread(target=_run, name="flash-one-attempt", daemon=True)
        worker.start()
    except Exception:
        permit._release_from_reader()
        return FlashOutcome("unknown_response", safe_code="ai_response_unknown")
    if not done.wait(_total_timeout_seconds):
        # A response may be complete at the same instant; err toward unknown.
        abort.set()
        response = handles.get("response")
        raw = getattr(response, "raw", None)
        connection = getattr(raw, "_connection", None)
        candidate = getattr(connection, "sock", None)
        if candidate is None:
            fp = getattr(raw, "_fp", None)
            nested = getattr(getattr(fp, "fp", None), "raw", None)
            candidate = getattr(nested, "_sock", None)
        if isinstance(candidate, socket.socket):
            try:
                candidate.setblocking(False)
                candidate.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return FlashOutcome("unknown_response", safe_code="ai_total_timeout")
    return result[0]
