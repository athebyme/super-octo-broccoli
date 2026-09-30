"""No-network checks for the seller's one-attempt native Flash boundary."""

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
import threading
import time

import pytest
import requests

from services.ozon_draft_ai_transport import (
    FlashConfigurationError, flash_completion, parse_retry_after,
    try_acquire_flash_permit,
)


class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.trust_env = True

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def profile(**overrides):
    values = dict(provider="deepseek", model="deepseek-flash",
                  api_base_url="https://api.deepseek.com/v1", api_key="secret",
                  max_retries=1, proxy_enabled=False,
                  task_profile="seller_draft_completion_flash")
    values.update(overrides)
    return SimpleNamespace(**values)


MESSAGES = [{"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "Observed red cotton."}]


def success_body(content='{"items":[]}'):
    return json.dumps({
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 21, "completion_tokens": 8,
                  "prompt_cache_hit_tokens": 12, "prompt_cache_miss_tokens": 9},
    }).encode()


def test_one_native_request_with_bounded_payload_and_nullable_usage():
    response = FakeResponse(200, success_body())
    session = FakeSession(response)
    result = flash_completion(profile(), MESSAGES, session=session)
    assert result.kind == "success" and result.content == {"items": []}
    assert result.usage.prompt_tokens == 21
    assert result.usage.cache_hit_tokens == 12
    assert result.usage.reasoning_tokens is None
    assert len(session.calls) == 1 and response.closed
    url, options = session.calls[0]
    assert url == "https://api.deepseek.com/v1/chat/completions"
    assert options["allow_redirects"] is False and options["stream"] is True
    assert session.trust_env is False
    assert len(options["data"]) <= 65536
    payload = json.loads(options["data"])
    assert payload["model"] == "deepseek-flash"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 8000


def test_provider_usage_beyond_sqlite_integer_is_unknown():
    body = json.loads(success_body())
    body["usage"]["prompt_tokens"] = 1 << 80
    result = flash_completion(profile(), MESSAGES,
                              session=FakeSession(FakeResponse(200, json.dumps(body).encode())))
    assert result.kind == "success"
    assert result.usage.prompt_tokens is None


def test_known_429_preserves_full_retry_after_and_no_provider_body():
    response = FakeResponse(429, b"secret provider body", {"Retry-After": "7200"})
    result = flash_completion(profile(), MESSAGES, session=FakeSession(response))
    assert result.kind == "rate_limited"
    assert result.retry_after_seconds == 7200
    assert result.content is None and result.usage.prompt_tokens is None
    assert parse_retry_after("Sun, 27 Sep 2026 01:00:00 GMT",
                             now=datetime(2026, 9, 27, tzinfo=timezone.utc)) == 3600


def test_timeout_is_uncertain_and_never_retried():
    session = FakeSession(error=requests.Timeout("raw secret"))
    result = flash_completion(profile(), MESSAGES, session=session)
    assert result.kind == "unknown_response"
    assert result.safe_code == "ai_response_unknown"
    assert len(session.calls) == 1


@pytest.mark.parametrize("content", [
    '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', '```json\n{}\n```',
])
def test_non_strict_model_json_is_terminal(content):
    result = flash_completion(profile(), MESSAGES,
                              session=FakeSession(FakeResponse(200, success_body(content))))
    assert result.kind == "invalid_response"
    assert result.usage.prompt_tokens == 21
    assert result.usage.completion_tokens == 8
    assert result.usage.cache_hit_tokens == 12


def test_truncated_output_keeps_reported_usage_without_retry_or_partial_content():
    body = json.loads(success_body('{"items":['))
    body['choices'][0]['finish_reason'] = 'length'
    session = FakeSession(FakeResponse(200, json.dumps(body).encode()))
    result = flash_completion(profile(), MESSAGES, session=session)
    assert result.kind == 'invalid_response' and result.content is None
    assert result.usage.prompt_tokens == 21
    assert result.usage.completion_tokens == 8
    assert result.usage.cache_miss_tokens == 9
    assert len(session.calls) == 1


def test_profile_and_size_reject_before_http():
    session = FakeSession(FakeResponse(200, success_body()))
    with pytest.raises(FlashConfigurationError):
        flash_completion(profile(provider="openrouter"), MESSAGES, session=session)
    with pytest.raises(FlashConfigurationError):
        flash_completion(profile(), [{"role": "user", "content": "x" * 65536}], session=session)
    with pytest.raises(FlashConfigurationError):
        flash_completion(profile(), MESSAGES, max_output_tokens=8001, session=session)
    assert session.calls == []


def test_pre_admitted_permit_releases_on_local_validation_and_cannot_reuse():
    permit = try_acquire_flash_permit()
    assert permit is not None
    with pytest.raises(FlashConfigurationError):
        flash_completion(profile(provider="openrouter"), MESSAGES,
                         permit=permit, session=FakeSession())
    assert permit.release() is False
    with pytest.raises(ValueError, match="invalid_flash_permit"):
        flash_completion(profile(), MESSAGES, permit=permit,
                         session=FakeSession(FakeResponse(200, success_body())))
    acquired = [try_acquire_flash_permit() for _ in range(3)]
    assert all(acquired)
    assert try_acquire_flash_permit() is None
    for slot in acquired:
        assert slot.release() is True
        assert slot.release() is False


def test_pre_admitted_permit_stays_with_timed_out_reader():
    gate = threading.Event()

    class HeldResponse(FakeResponse):
        def iter_content(self, chunk_size):
            gate.wait(timeout=1)
            yield b"{}"

    response = HeldResponse(200)
    permit = try_acquire_flash_permit()
    assert permit is not None
    result = flash_completion(profile(), MESSAGES, permit=permit,
                              session=FakeSession(response),
                              _total_timeout_seconds=0.04)
    assert result.safe_code == "ai_total_timeout"
    assert permit.release() is False
    peers = [try_acquire_flash_permit() for _ in range(2)]
    assert all(peers)
    assert try_acquire_flash_permit() is None
    gate.set()
    resumed = None
    for _ in range(50):
        if response.closed:
            resumed = try_acquire_flash_permit()
            if resumed is not None:
                break
        time.sleep(0.01)
    assert response.closed
    assert resumed is not None
    assert resumed.release() is True
    for slot in peers:
        assert slot.release() is True


def test_pre_admitted_permit_releases_if_reader_cannot_start(monkeypatch):
    permit = try_acquire_flash_permit()
    assert permit is not None
    monkeypatch.setattr(threading.Thread, "start", lambda _self: (_ for _ in ()).throw(RuntimeError("no thread")))
    result = flash_completion(profile(), MESSAGES, permit=permit,
                              session=FakeSession())
    assert result.safe_code == "ai_response_unknown"
    assert permit.release() is False
    acquired = [try_acquire_flash_permit() for _ in range(3)]
    assert all(acquired)
    for slot in acquired:
        slot.release()


def test_response_size_is_bounded_during_streaming():
    response = FakeResponse(200, b"x" * (4 * 1024 * 1024 + 1))
    result = flash_completion(profile(), MESSAGES, session=FakeSession(response))
    assert result.kind == "invalid_response"
    assert result.safe_code == "ai_response_too_large"


def test_slow_stream_cannot_extend_total_deadline():
    class SlowResponse(FakeResponse):
        def iter_content(self, chunk_size):
            while True:
                time.sleep(0.02)
                yield b" "

    response = SlowResponse(200)
    started = time.monotonic()
    result = flash_completion(profile(), MESSAGES, session=FakeSession(response),
                              _total_timeout_seconds=0.04)
    assert result.kind == "unknown_response"
    assert time.monotonic() - started < 0.2
    for _ in range(20):
        if response.closed:
            break
        time.sleep(0.01)
    assert response.closed


def test_timed_out_readers_keep_process_capacity_until_exit():
    gate = threading.Event()
    responses = []

    class HeldResponse(FakeResponse):
        def iter_content(self, chunk_size):
            gate.wait(timeout=1)
            yield b"{}"

    def start_one(_number):
        response = HeldResponse(200)
        responses.append(response)
        return flash_completion(profile(), MESSAGES,
                                session=FakeSession(response),
                                _total_timeout_seconds=0.04)

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(start_one, range(3)))
    assert all(outcome.safe_code == "ai_total_timeout" for outcome in outcomes)
    fourth = FakeSession(HeldResponse(200))
    result = flash_completion(profile(), MESSAGES, session=fourth,
                              _total_timeout_seconds=0.04)
    assert result.safe_code == "ai_local_capacity_unknown"
    assert fourth.calls == []
    gate.set()
    for _ in range(50):
        if all(response.closed for response in responses):
            break
        time.sleep(0.01)
    assert all(response.closed for response in responses)


def test_deadline_parent_does_not_block_on_slow_close():
    class SlowCloseResponse(FakeResponse):
        def iter_content(self, chunk_size):
            while True:
                time.sleep(0.02)
                yield b" "

        def close(self):
            time.sleep(0.25)
            super().close()

    response = SlowCloseResponse(200)
    started = time.monotonic()
    result = flash_completion(profile(), MESSAGES, session=FakeSession(response),
                              _total_timeout_seconds=0.04)
    assert result.kind == "unknown_response"
    assert time.monotonic() - started < 0.15
