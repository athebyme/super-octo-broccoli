"""Pure, deterministic planning and decoding for marketplace Decisions batches."""

from dataclasses import replace

import pytest

from services.openai_decisions import DecisionAnswer, DecisionOutcome
from services.marketplace_decisions_preparation import (
    MAX_PRODUCTS,
    CategoryProductContext,
    CharacteristicAttributeContext,
    CharacteristicProductContext,
    CharacteristicOption,
    DecisionPreparationError,
    DictionaryValueChoice,
    ExactCategoryTypeChoice,
    decode_decision_plan,
    decode_decision_plan_answers,
    plan_category_phase,
    plan_characteristic_phase,
)


REF = "ozon-tree-2026-11-23"


def category_choice(category_id, category_name, type_id, type_name, *, ref=REF):
    return ExactCategoryTypeChoice(
        "ozon", ref, category_id, category_name, type_id, type_name,
    )


def category_context(key, *, source=None, choices=None, unresolved=True):
    return CategoryProductContext(
        product_key=key,
        marketplace="ozon",
        reference_version=REF,
        source_fields=source or {"name": "Текстильный органайзер", "price": "999"},
        unresolved=unresolved,
        choices=choices if choices is not None else (
            category_choice("10", "Хранение", "100", "Органайзеры"),
            category_choice("11", "Текстиль", "110", "Сумки и органайзеры"),
        ),
    )


def dictionary_choice(attribute_id, value_id, value, *, type_id="77", ref=REF,
                      schema_version="v3", schema_hash="schema-77",
                      dictionary_version="dict-10"):
    return DictionaryValueChoice(
        marketplace="ozon",
        reference_version=ref,
        type_id=type_id,
        schema_version=schema_version,
        schema_hash=schema_hash,
        attribute_id=attribute_id,
        dictionary_version=dictionary_version,
        value_id=value_id,
        value=value,
    )


def characteristic_context(key, attributes, *, type_id="77", type_name="Органайзеры",
                           schema_version="v3", schema_hash="schema-77", source=None):
    return CharacteristicProductContext(
        product_key=key,
        marketplace="ozon",
        reference_version=REF,
        current_type_id=type_id,
        current_type_name=type_name,
        schema_version=schema_version,
        schema_hash=schema_hash,
        source_fields=source or {"name": "Текстильный органайзер", "description": "Складной"},
        attributes=attributes,
    )


def answer_for(plan, question_index=0, choice_index=0, *, confidence=0.93):
    question = plan.questions[question_index]
    choice = question.choices[choice_index].value
    return DecisionOutcome(
        "success",
        answers=(DecisionAnswer(question.name, "answered", choice, confidence),),
    )


def test_category_phase_batches_exact_pairs_and_sends_only_whitelisted_observations():
    unresolved = category_context(1001, source={
        "name": "Товар с наблюдаемым названием",
        "description": "Фактическое описание",
        "price": "999",
        "tnved": "123456",
        "seller_email": "seller@example.test",
    })
    already_resolved = category_context(1002, unresolved=False)
    plan = plan_category_phase([unresolved, already_resolved])

    assert plan.phase == "category"
    assert len(plan.questions) == 1
    question = plan.questions[0]
    assert question.name.endswith(".category")
    assert len(question.choices) == 3
    payload = plan.input_json
    assert payload["phase"] == "category"
    assert payload["products"] == [{
        "namespace": question.name.removesuffix(".category"),
        "marketplace": "ozon",
        "observed_source_fields": {
            "name": "Товар с наблюдаемым названием",
            "description": "Фактическое описание",
        },
    }]
    assert "1001" not in repr(payload)
    assert "price" not in repr(payload) and "seller_email" not in repr(payload)
    assert all(len(value) == 64 for value in plan.question_fingerprints)
    assert "Товар с наблюдаемым названием" not in plan.question_fingerprints[0]
    assert "Товар с наблюдаемым названием" not in repr(plan)

    results = decode_decision_plan(plan, answer_for(plan, choice_index=1))
    assert results[0].status == "selected"
    assert results[0].option.category_id == "11"
    assert results[0].option.type_id == "110"
    assert results[0].option.reference_version == REF
    assert results[1].status == "skipped"
    assert results[1].reason == "already_resolved"


def test_per_product_signatures_and_question_names_survive_batch_reordering():
    first = category_context("seller41-account7-product1001")
    second = category_context("seller41-account7-product1002")
    solo = plan_category_phase([first])
    together = plan_category_phase([second, first])

    assert solo.questions[0].name == together.questions[1].name
    assert solo.questions[0].choices == together.questions[1].choices
    assert solo.question_fingerprints[0] == together.question_fingerprints[1]
    assert solo.request_signature != together.request_signature


def test_each_actual_question_binds_to_only_its_products_observed_fields():
    category_plan = plan_category_phase([
        category_context("pink-product", source={"name": "pink organizer"}),
        category_context("black-product", source={"name": "black organizer"}),
    ])
    category_namespaces = [
        product["namespace"] for product in category_plan.input_json["products"]
    ]
    assert len(category_plan.questions) == 2
    for index, question in enumerate(category_plan.questions):
        namespace = category_namespaces[index]
        other_namespace = category_namespaces[1 - index]
        assert f"namespace «{namespace}»" in question.instructions
        assert "observed_source_fields" in question.instructions
        assert other_namespace not in question.instructions

    def color_context(product_key, product_name):
        attribute = CharacteristicAttributeContext("10", "Цвет", choices=(
            dictionary_choice("10", "5001", "Розовый"),
            dictionary_choice("10", "5002", "Черный"),
        ))
        return characteristic_context(
            product_key, [attribute], source={"name": product_name},
        )

    characteristic_plan = plan_characteristic_phase([
        color_context("pink-product", "pink organizer"),
        color_context("black-product", "black organizer"),
    ])
    characteristic_namespaces = [
        product["namespace"] for product in characteristic_plan.input_json["products"]
    ]
    assert len(characteristic_plan.questions) == 2
    for index, question in enumerate(characteristic_plan.questions):
        namespace = characteristic_namespaces[index]
        other_namespace = characteristic_namespaces[1 - index]
        assert f"namespace «{namespace}»" in question.instructions
        assert "observed_source_fields" in question.instructions
        assert other_namespace not in question.instructions


def test_category_phase_abstains_for_candidates_from_another_reference_or_single_option():
    mismatch = category_context("p1", choices=(
        category_choice("10", "A", "100", "Type A", ref="stale-tree"),
        category_choice("11", "B", "110", "Type B"),
    ))
    single = category_context("p2", choices=(
        category_choice("10", "A", "100", "Type A"),
    ))
    plan = plan_category_phase([mismatch, single])

    assert plan.questions == ()
    results = decode_decision_plan(plan, None)
    assert [(item.status, item.reason) for item in results] == [
        ("abstained", "choice_scope_mismatch"),
        ("abstained", "insufficient_exact_choices"),
    ]


def test_characteristic_phase_skips_manual_existing_literal_and_regulatory_fields():
    attributes = [
        CharacteristicAttributeContext("1", "Цвет", existing_value=True),
        CharacteristicAttributeContext("2", "Материал", manual_value=True),
        CharacteristicAttributeContext("3", "Состав", deterministic_literal="100% хлопок"),
        CharacteristicAttributeContext(
            "22232", "ТН ВЭД", choices=(dictionary_choice("22232", "1", "код 1"),
                                       dictionary_choice("22232", "2", "код 2")),
        ),
        CharacteristicAttributeContext("10", "Тип ткани", choices=(
            dictionary_choice("10", "5001", "Хлопок"),
            dictionary_choice("10", "5002", "Полиэстер"),
        )),
        CharacteristicAttributeContext("11", "Другой тип", choices=(
            dictionary_choice("11", "6001", "Значение A", type_id="88"),
            dictionary_choice("11", "6002", "Значение B", type_id="88"),
        )),
        CharacteristicAttributeContext("13", "Назначение товара 18+", choices=(
            dictionary_choice("13", "8001", "Взрослый"),
            dictionary_choice("13", "8002", "Для всех"),
        )),
        CharacteristicAttributeContext("14", "Фактура", choices=(
            dictionary_choice("14", "9001", "Гладкая", dictionary_version="dict-a"),
            dictionary_choice("14", "9002", "Рельефная", dictionary_version="dict-b"),
        )),
    ]
    no_type = characteristic_context("product-no-type", [
        CharacteristicAttributeContext("12", "Размер", choices=(
            dictionary_choice("12", "7001", "Малый"),
            dictionary_choice("12", "7002", "Большой"),
        )),
    ], type_id=None, type_name=None, schema_version=None, schema_hash=None)
    plan = plan_characteristic_phase([
        characteristic_context("product-1", attributes), no_type,
    ])

    assert len(plan.questions) == 1
    assert plan.questions[0].name.startswith("p") and ".a" in plan.questions[0].name
    assert len(plan.input_json["products"]) == 1
    assert plan.input_json["products"][0]["exact_current_type"] == "Органайзеры"
    results = decode_decision_plan(plan, answer_for(plan, choice_index=1))
    assert [(row.status, row.reason) for row in results] == [
        ("skipped", "existing_value"),
        ("skipped", "manual_value"),
        ("selected", "deterministic_literal_match"),
        ("abstained", "regulatory_value_not_inferred"),
        ("selected", "exact_choice"),
        ("abstained", "dictionary_scope_mismatch"),
        ("abstained", "regulatory_value_not_inferred"),
        ("abstained", "dictionary_scope_mismatch"),
        ("abstained", "current_type_schema_required"),
    ]
    literal = results[2].option
    assert isinstance(literal, CharacteristicOption)
    assert literal.kind == "literal" and literal.value == "100% хлопок"
    dictionary = results[4].option
    assert isinstance(dictionary, CharacteristicOption)
    assert (dictionary.type_id, dictionary.schema_hash, dictionary.dictionary_value_id) == (
        "77", "schema-77", "5002",
    )


@pytest.mark.parametrize(("answer", "reason"), [
    (DecisionAnswer("placeholder", "refused"), "model_refusal"),
])
def test_refusal_low_confidence_and_model_abstention_fail_closed(answer, reason):
    plan = plan_category_phase([category_context("p-refusal")])
    named_refusal = replace(answer, name=plan.questions[0].name)
    refused = decode_decision_plan(plan, DecisionOutcome("refused", answers=(named_refusal,)))
    assert refused[0].status == "abstained" and refused[0].reason == reason

    low_confidence = decode_decision_plan(
        plan, answer_for(plan, confidence=0.79),
    )
    assert low_confidence[0].status == "abstained"
    assert low_confidence[0].reason == "insufficient_confidence"

    question = plan.questions[0]
    abstain = question.choices[-1].value
    explicit_abstention = decode_decision_plan(plan, DecisionOutcome(
        "success", answers=(DecisionAnswer(question.name, "answered", abstain, 0.99),),
    ))
    assert explicit_abstention[0].status == "abstained"
    assert explicit_abstention[0].reason == "model_abstention"


def test_malformed_batch_response_abstains_all_pending_without_losing_precomputed_results():
    contexts = [
        category_context("pending-1"),
        category_context("resolved-1", unresolved=False),
    ]
    plan = plan_category_phase(contexts)
    mismatched = DecisionOutcome("success", answers=(
        DecisionAnswer("wrong-name", "answered", "unknown", 0.99),
    ))
    results = decode_decision_plan(plan, mismatched)
    assert results[0].status == "abstained" and results[0].reason == "response_mismatch"
    assert results[1].status == "skipped" and results[1].reason == "already_resolved"


def test_product_and_question_limits_are_fail_closed():
    contexts = [category_context(f"product-{index}") for index in range(MAX_PRODUCTS + 1)]
    with pytest.raises(DecisionPreparationError) as caught:
        plan_category_phase(contexts)
    assert caught.value.code == "invalid_product_count"

    with pytest.raises(DecisionPreparationError) as caught:
        plan_characteristic_phase([characteristic_context("same", []), characteristic_context("same", [])])
    assert caught.value.code == "duplicate_or_invalid_product_key"


def test_partial_cache_answers_decode_by_independent_fingerprint():
    first_attribute = CharacteristicAttributeContext("10", "Тип ткани", choices=(
            dictionary_choice("10", "5001", "Хлопок"),
            dictionary_choice("10", "5002", "Полиэстер"),
        ))
    second_attribute = CharacteristicAttributeContext("11", "Цвет", choices=(
            dictionary_choice("11", "6001", "Синий"),
            dictionary_choice("11", "6002", "Красный"),
        ))
    plan = plan_characteristic_phase([
        characteristic_context("seller41-account7-product1", [first_attribute],
                               source={"name": "first observed product"}),
        characteristic_context("seller41-account7-product2", [second_attribute],
                               source={"name": "second observed product"}),
    ])
    fingerprint_by_name = {
        question.name: fingerprint
        for question, fingerprint in zip(plan.questions, plan.question_fingerprints)
    }
    first = plan.questions[0]
    cached = DecisionAnswer(first.name, "answered", first.choices[1].value, 0.91)
    subset_payload = plan.input_json_for([fingerprint_by_name[first.name]])
    assert len(subset_payload["products"]) == 1
    assert subset_payload["products"][0]["observed_source_fields"]["name"] == "first observed product"

    results = decode_decision_plan_answers(
        plan,
        {fingerprint_by_name[first.name]: cached},
        missing_reasons={fingerprint_by_name[plan.questions[1].name]: "cache_busy"},
    )
    assert results[0].status == "selected"
    assert results[0].option.dictionary_value_id == "5002"
    assert results[1].status == "abstained" and results[1].reason == "cache_busy"
