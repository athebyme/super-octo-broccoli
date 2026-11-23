"""Configuration-only tests for isolated OpenAI Decisions runtime wiring."""

import pytest

from services import openai_decisions_runtime as runtime


class RuntimeFactories:
    def __init__(self):
        self.cache_calls = []
        self.client_calls = []
        self.pipeline_calls = []
        self.cache = object()
        self.client = object()
        self.pipeline = object()

    def install(self, monkeypatch):
        def make_cache(path, *, max_calls_per_day):
            self.cache_calls.append((path, max_calls_per_day))
            return self.cache

        def make_client(*, api_key, proxy_url):
            self.client_calls.append((api_key, proxy_url))
            return self.client

        def make_pipeline(*, cache, client):
            self.pipeline_calls.append((cache, client))
            return self.pipeline

        monkeypatch.setattr(runtime, "OpenAIDecisionsCache", make_cache)
        monkeypatch.setattr(runtime, "OpenAIDecisionsClient", make_client)
        monkeypatch.setattr(runtime, "OpenAIDecisionsPipeline", make_pipeline)


def configured(**overrides):
    values = {
        "OPENAI_DECISIONS_ENABLED": "true",
        "OPENAI_DECISIONS_API_KEY": "decisions-test-key",
        "AI_PROXY": "socks5h://proxy.example:1080",
    }
    values.update(overrides)
    return values


def test_disabled_or_absent_configuration_skips_before_any_factory(monkeypatch, caplog):
    factories = RuntimeFactories()
    factories.install(monkeypatch)

    assert runtime.create_openai_decisions_pipeline(environ={}) is None
    assert runtime.create_openai_decisions_pipeline(environ={
        "OPENAI_DECISIONS_ENABLED": "false",
        "OPENAI_DECISIONS_API_KEY": "not-used",
        "AI_PROXY": "http://proxy.example:3128",
    }) is None
    assert runtime.create_openai_decisions_pipeline(environ={
        "OPENAI_DECISIONS_ENABLED": "unexpected",
        "OPENAI_DECISIONS_API_KEY": "not-used",
        "AI_PROXY": "http://proxy.example:3128",
    }) is None
    assert factories.cache_calls == []
    assert factories.client_calls == []
    assert factories.pipeline_calls == []
    assert caplog.text == ""


@pytest.mark.parametrize("missing", [
    {"OPENAI_DECISIONS_API_KEY": ""},
    {"OPENAI_DECISIONS_API_KEY": None},
    {"AI_PROXY": ""},
    {"AI_PROXY": None},
])
def test_enabled_but_missing_credentials_skips_without_cache_or_client(monkeypatch, missing):
    factories = RuntimeFactories()
    factories.install(monkeypatch)

    assert runtime.create_openai_decisions_pipeline(
        environ=configured(**missing),
    ) is None
    assert factories.cache_calls == []
    assert factories.client_calls == []
    assert factories.pipeline_calls == []


def test_native_openai_chat_key_is_not_a_decisions_credential(monkeypatch):
    factories = RuntimeFactories()
    factories.install(monkeypatch)
    environment = {
        "OPENAI_DECISIONS_ENABLED": "true",
        "OPENAI_API_KEY": "chat-key-must-not-be-used",
        "AI_PROXY": "http://proxy.example:3128",
    }

    assert runtime.create_openai_decisions_pipeline(environ=environment) is None
    assert factories.cache_calls == []
    assert factories.client_calls == []


@pytest.mark.parametrize("limit", ["", "0", "1001", "1e2", " 100", "１２"])
def test_invalid_daily_budget_fails_closed_before_object_construction(monkeypatch, limit):
    factories = RuntimeFactories()
    factories.install(monkeypatch)

    assert runtime.create_openai_decisions_pipeline(
        environ=configured(OPENAI_DECISIONS_MAX_CALLS_PER_DAY=limit),
    ) is None
    assert factories.cache_calls == []
    assert factories.client_calls == []


@pytest.mark.parametrize("proxy", [
    "",
    "ftp://proxy.example:21",
    "https://proxy.example:99999",
    "http://proxy.example/path#fragment",
    "http://proxy.example:3128\n",
])
def test_invalid_existing_proxy_fails_closed_before_object_construction(monkeypatch, proxy):
    factories = RuntimeFactories()
    factories.install(monkeypatch)

    assert runtime.create_openai_decisions_pipeline(
        environ=configured(AI_PROXY=proxy),
    ) is None
    assert factories.cache_calls == []
    assert factories.client_calls == []


def test_valid_runtime_uses_exact_existing_proxy_private_volume_path_and_default_budget(monkeypatch):
    factories = RuntimeFactories()
    factories.install(monkeypatch)

    result = runtime.create_openai_decisions_pipeline(environ=configured())

    assert result is factories.pipeline
    assert factories.cache_calls == [(runtime.OPENAI_DECISIONS_CACHE_PATH, 100)]
    assert factories.client_calls == [
        ("decisions-test-key", "socks5h://proxy.example:1080"),
    ]
    assert factories.pipeline_calls == [(factories.cache, factories.client)]
    assert runtime.OPENAI_DECISIONS_CACHE_PATH.startswith("/app/data/")


def test_valid_explicit_daily_limit_is_bounded_and_passed_to_durable_cache(monkeypatch):
    factories = RuntimeFactories()
    factories.install(monkeypatch)

    result = runtime.create_openai_decisions_pipeline(
        environ=configured(OPENAI_DECISIONS_MAX_CALLS_PER_DAY="250"),
    )

    assert result is factories.pipeline
    assert factories.cache_calls == [(runtime.OPENAI_DECISIONS_CACHE_PATH, 250)]


def test_invalid_construction_fails_silently(monkeypatch, caplog):
    def raise_without_detail(*_args, **_kwargs):
        raise RuntimeError("must-not-be-logged")

    monkeypatch.setattr(runtime, "OpenAIDecisionsCache", raise_without_detail)
    assert runtime.create_openai_decisions_pipeline(environ=configured()) is None
    assert "must-not-be-logged" not in caplog.text
