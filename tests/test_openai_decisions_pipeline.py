"""Deterministic orchestration tests for the Decisions plan/cache/client pipeline."""

from datetime import datetime, timedelta, timezone

import pytest

from services.marketplace_decisions_preparation import (
    CategoryProductContext,
    CharacteristicAttributeContext,
    CharacteristicProductContext,
    DictionaryValueChoice,
    ExactCategoryTypeChoice,
    plan_category_phase,
    plan_characteristic_phase,
)
from services.openai_decisions import DecisionAnswer, DecisionOutcome
from services.openai_decisions_cache import OpenAIDecisionsCache
from services.openai_decisions_pipeline import OpenAIDecisionsPipeline


NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def category_context(key, *, name="Текстильный органайзер", choices=None):
    options = choices or (
        ExactCategoryTypeChoice(
            "ozon", "tree-r77", "10", "Хранение", "100", "Органайзеры",
        ),
        ExactCategoryTypeChoice(
            "ozon", "tree-r77", "11", "Текстиль", "110", "Текстильные сумки",
        ),
    )
    return CategoryProductContext(
        product_key=key,
        marketplace="ozon",
        reference_version="tree-r77",
        source_fields={"name": name, "description": "Складная модель"},
        unresolved=True,
        choices=options,
    )


def characteristic_context(
    key,
    *,
    schema_version="schema-r77-v1",
    schema_hash="a" * 64,
    name="Текстильный органайзер",
):
    attribute_id = "9042"
    choices = tuple(
        DictionaryValueChoice(
            marketplace="ozon",
            reference_version="tree-r77",
            type_id="700",
            schema_version=schema_version,
            schema_hash=schema_hash,
            attribute_id=attribute_id,
            dictionary_version=f"dict-{schema_version}",
            value_id=value_id,
            value=value,
        )
        for value_id, value in (("9001", "Для хранения"), ("9002", "Для поездок"))
    )
    return CharacteristicProductContext(
        product_key=key,
        marketplace="ozon",
        reference_version="tree-r77",
        current_type_id="700",
        current_type_name="Органайзеры",
        schema_version=schema_version,
        schema_hash=schema_hash,
        source_fields={"name": name, "description": "Складная модель"},
        attributes=(CharacteristicAttributeContext(
            attribute_id=attribute_id,
            attribute_name="Назначение",
            choices=choices,
        ),),
    )


class FakeClient:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def decide(self, *, input_json, questions):
        self.calls.append({
            "input_json": input_json,
            "questions": tuple(questions),
        })
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            return outcome(input_json, tuple(questions)) if callable(outcome) else outcome
        return answered_outcome(tuple(questions))


def answered_outcome(questions):
    return DecisionOutcome(
        "success",
        answers=tuple(
            DecisionAnswer(
                name=question.name,
                status="answered",
                choice=question.choices[0].value,
                confidence=0.94,
            )
            for question in questions
        ),
    )


def private_cache(tmp_path, *, max_calls_per_day=20):
    private = tmp_path / "private"
    private.mkdir(mode=0o700, parents=True)
    return OpenAIDecisionsCache(
        str(private / "decisions.sqlite3"),
        max_calls_per_day=max_calls_per_day,
    )


def test_same_facts_and_schema_use_cached_answers_without_another_call(tmp_path):
    client = FakeClient()
    pipeline = OpenAIDecisionsPipeline(private_cache(tmp_path), client)
    contexts = [category_context("seller-7-account-2-product-101")]

    first = pipeline.run_category_phase(contexts, now=NOW)
    second = pipeline.run_category_phase(contexts, now=NOW + timedelta(minutes=1))

    assert first.reservation_state == "ready"
    assert first.finish_state == "success"
    assert first.physical_request_started is True
    assert second.reservation_state == "cached"
    assert second.finish_state is None
    assert second.physical_request_started is False
    assert len(client.calls) == 1
    assert [item.status for item in first.results] == ["selected"]
    assert [item.status for item in second.results] == ["selected"]
    assert first.results[0].option == second.results[0].option


def test_mixed_cached_and_new_questions_send_only_the_new_product_context(tmp_path):
    client = FakeClient()
    pipeline = OpenAIDecisionsPipeline(private_cache(tmp_path), client)
    known = category_context("seller-7-account-2-product-201")
    new = category_context("seller-7-account-2-product-202")

    seeded = pipeline.run_category_phase([known], now=NOW)
    mixed_plan = plan_category_phase([known, new])
    mixed = pipeline.run_plan(mixed_plan, now=NOW + timedelta(minutes=1))

    assert seeded.results[0].status == "selected"
    assert mixed.reservation_state == "ready"
    assert len(client.calls) == 2
    call = client.calls[-1]
    assert [question.name for question in call["questions"]] == [
        mixed_plan.questions[1].name,
    ]
    expected_new_namespace = mixed_plan.input_json["products"][1]["namespace"]
    assert [product["namespace"] for product in call["input_json"]["products"]] == [
        expected_new_namespace,
    ]
    assert [item.status for item in mixed.results] == ["selected", "selected"]
    assert mixed.results[0].signature == mixed_plan.question_fingerprints[0]
    assert mixed.results[1].signature == mixed_plan.question_fingerprints[1]


def test_changed_source_facts_invalidate_only_that_product_question(tmp_path):
    client = FakeClient()
    pipeline = OpenAIDecisionsPipeline(private_cache(tmp_path), client)
    original_a = category_context("seller-7-account-2-product-301")
    unchanged_b = category_context("seller-7-account-2-product-302")
    pipeline.run_category_phase([original_a, unchanged_b], now=NOW)

    changed_a = category_context(
        "seller-7-account-2-product-301",
        name="Другой наблюдаемый товар",
    )
    changed_plan = plan_category_phase([changed_a, unchanged_b])
    result = pipeline.run_plan(changed_plan, now=NOW + timedelta(minutes=1))

    assert result.reservation_state == "ready"
    assert len(client.calls) == 2
    call = client.calls[-1]
    assert [question.name for question in call["questions"]] == [
        changed_plan.questions[0].name,
    ]
    assert [product["namespace"] for product in call["input_json"]["products"]] == [
        changed_plan.input_json["products"][0]["namespace"],
    ]
    assert [item.status for item in result.results] == ["selected", "selected"]
    assert changed_plan.question_fingerprints[0] != plan_category_phase(
        [original_a, unchanged_b],
    ).question_fingerprints[0]
    assert changed_plan.question_fingerprints[1] == plan_category_phase(
        [original_a, unchanged_b],
    ).question_fingerprints[1]


def test_changed_schema_invalidates_only_that_characteristic_question(tmp_path):
    client = FakeClient()
    pipeline = OpenAIDecisionsPipeline(private_cache(tmp_path), client)
    original_a = characteristic_context("seller-7-account-2-product-401")
    unchanged_b = characteristic_context("seller-7-account-2-product-402")
    original_plan = plan_characteristic_phase([original_a, unchanged_b])
    first = pipeline.run_plan(original_plan, now=NOW)
    assert [item.status for item in first.results] == ["selected", "selected"]

    changed_a = characteristic_context(
        "seller-7-account-2-product-401",
        schema_version="schema-r77-v2",
        schema_hash="b" * 64,
    )
    changed_plan = plan_characteristic_phase([changed_a, unchanged_b])
    result = pipeline.run_plan(changed_plan, now=NOW + timedelta(minutes=1))

    assert result.reservation_state == "ready"
    assert len(client.calls) == 2
    call = client.calls[-1]
    assert [question.name for question in call["questions"]] == [
        changed_plan.questions[0].name,
    ]
    assert [product["namespace"] for product in call["input_json"]["products"]] == [
        changed_plan.input_json["products"][0]["namespace"],
    ]
    assert [item.status for item in result.results] == ["selected", "selected"]
    assert changed_plan.question_fingerprints[0] != original_plan.question_fingerprints[0]
    assert changed_plan.question_fingerprints[1] == original_plan.question_fingerprints[1]


def test_partial_refusal_is_merged_and_cached_as_a_safe_abstention(tmp_path):
    def partial(_input_json, questions):
        return DecisionOutcome(
            "partial_refusal",
            answers=(
                DecisionAnswer(
                    questions[0].name,
                    "answered",
                    questions[0].choices[0].value,
                    0.95,
                ),
                DecisionAnswer(questions[1].name, "refused"),
            ),
        )

    client = FakeClient([partial])
    pipeline = OpenAIDecisionsPipeline(private_cache(tmp_path), client)
    contexts = [
        category_context("seller-7-account-2-product-501"),
        category_context("seller-7-account-2-product-502"),
    ]

    first = pipeline.run_category_phase(contexts, now=NOW)
    second = pipeline.run_category_phase(contexts, now=NOW + timedelta(minutes=1))

    assert first.finish_state == "partial_refusal"
    assert [item.status for item in first.results] == ["selected", "abstained"]
    assert first.results[1].reason == "model_refusal"
    assert second.reservation_state == "cached"
    assert [item.status for item in second.results] == ["selected", "abstained"]
    assert second.results[1].reason == "model_refusal"
    assert len(client.calls) == 1


def test_unknown_result_is_abstained_and_never_dispatched_again(tmp_path):
    client = FakeClient([DecisionOutcome("unknown_response", safe_code="transport_unknown")])
    pipeline = OpenAIDecisionsPipeline(private_cache(tmp_path), client)
    contexts = [category_context("seller-7-account-2-product-601")]

    first = pipeline.run_category_phase(contexts, now=NOW)
    second = pipeline.run_category_phase(contexts, now=NOW + timedelta(minutes=2))

    assert first.finish_state == "unknown"
    assert first.physical_request_started is True
    assert first.results[0].status == "abstained"
    assert first.results[0].reason == "unknown_response"
    assert second.reservation_state == "unknown"
    assert second.results[0].status == "abstained"
    assert second.results[0].reason == "unknown_response"
    assert second.physical_request_started is False
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("outcome", "cooldown"),
    [
        (DecisionOutcome("rate_limited", http_status=429), timedelta(minutes=5)),
        (DecisionOutcome("http_error", http_status=401), timedelta(hours=1)),
        (DecisionOutcome("http_error", http_status=403), timedelta(hours=1)),
    ],
)
def test_unknown_provider_failures_pause_other_fingerprints(outcome, cooldown, tmp_path):
    client = FakeClient([outcome])
    pipeline = OpenAIDecisionsPipeline(private_cache(tmp_path), client)
    failed_product = category_context("seller-7-account-2-product-provider-error")
    other_product = category_context("seller-7-account-2-product-after-provider-error")

    failed = pipeline.run_category_phase([failed_product], now=NOW)
    paused = pipeline.run_category_phase([other_product], now=NOW + timedelta(minutes=1))

    assert failed.finish_state == "unknown"
    assert failed.results[0].status == "abstained"
    assert failed.results[0].reason == "unknown_response"
    assert failed.retry_at == NOW + cooldown
    assert paused.reservation_state == "deferred"
    assert paused.physical_request_started is False
    assert paused.results[0].status == "abstained"
    assert paused.results[0].reason == "cache_deferred"
    assert len(client.calls) == 1


def test_busy_process_lock_releases_unstarted_claim_and_cached_hits_skip_lock(tmp_path):
    client = FakeClient()
    cache = private_cache(tmp_path)
    pipeline = OpenAIDecisionsPipeline(cache, client)
    cached_product = category_context("seller-7-account-2-product-cached-under-lock")
    seeded = pipeline.run_category_phase([cached_product], now=NOW)
    assert seeded.results[0].status == "selected"

    other_cache = OpenAIDecisionsCache(cache._path, max_calls_per_day=20)
    physical_lock = other_cache.acquire_physical_call_lock()
    assert physical_lock is not None
    cached = pipeline.run_category_phase(
        [cached_product], now=NOW + timedelta(minutes=1),
    )
    assert cached.reservation_state == "cached"
    assert cached.finish_state is None
    assert cached.physical_request_started is False
    assert len(client.calls) == 1

    new_product = category_context("seller-7-account-2-product-lock-contended")
    blocked = pipeline.run_category_phase(
        [new_product], now=NOW + timedelta(minutes=2),
    )
    assert blocked.reservation_state == "ready"
    assert blocked.finish_state == "released"
    assert blocked.physical_request_started is False
    assert blocked.safe_code == "provider_call_busy"
    assert blocked.results[0].status == "abstained"
    assert blocked.results[0].reason == "cache_busy"
    assert len(client.calls) == 1

    physical_lock.release()
    retried = pipeline.run_category_phase(
        [new_product], now=NOW + timedelta(minutes=2, seconds=1),
    )
    assert retried.finish_state == "success"
    assert retried.physical_request_started is True
    assert len(client.calls) == 2


def test_missing_configuration_releases_claim_without_false_success(tmp_path):
    client = FakeClient([DecisionOutcome("configuration_error", safe_code="missing_key")])
    pipeline = OpenAIDecisionsPipeline(private_cache(tmp_path), client)
    result = pipeline.run_category_phase(
        [category_context("seller-7-account-2-product-701")],
        now=NOW,
    )

    assert result.finish_state == "released"
    assert result.physical_request_started is False
    assert result.results[0].status == "abstained"
    assert result.results[0].reason == "configuration_error"
    assert len(client.calls) == 1


def test_deterministic_or_ineligible_plan_does_not_reserve_or_call_client(tmp_path):
    client = FakeClient()
    pipeline = OpenAIDecisionsPipeline(private_cache(tmp_path), client)
    plan = plan_category_phase([
        category_context(
            "seller-7-account-2-product-801",
            choices=(ExactCategoryTypeChoice(
                "ozon", "tree-r77", "10", "Хранение", "100", "Органайзеры",
            ),),
        ),
    ])

    result = pipeline.run_plan(plan, now=NOW)

    assert result.reservation_state == "not_required"
    assert result.physical_request_started is False
    assert result.results[0].status == "abstained"
    assert result.results[0].reason == "insufficient_exact_choices"
    assert client.calls == []
