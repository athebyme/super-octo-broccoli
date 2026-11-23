"""Fully local coverage for source-backed Ozon color preparation."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from models import (
    ImportedProduct,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceCategoryMapping,
    MarketplaceListing,
    MarketplaceListingLinkEvent,
    MarketplaceProductType,
    MarketplaceOperation,
    MarketplaceProductDraft,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    Supplier,
    SupplierProduct,
    db,
)
from services.marketplace_decisions_preparation import decode_decision_plan
from services.marketplace_decisions_preparation import DecisionItemResult
from services.marketplace_decisions_preparation import plan_category_phase
from services.marketplace_decisions_preparation import plan_characteristic_phase
from services.marketplace_drafts import MarketplaceDraftService
from services.ozon_bulk_upload import OzonBulkUploadService
from services.ozon_reference_service import OzonReferenceService
from services.ozon_prepare_decisions import OzonPrepareDecisionsService
from services.ozon_upload_queue import OzonUploadQueueService
from services.openai_decisions import DecisionAnswer, DecisionChoice, DecisionOutcome
from services.openai_decisions_cache import OpenAIDecisionsCache
from services.openai_decisions_pipeline import OpenAIDecisionsPipeline
from services.supplier_color_observation_store import SupplierColorObservationStore
from services.supplier_service import supplier_catalog_config_digest
from tests.test_marketplace_publications import OzonPublicationFixture


class _FakeDecisionsPipeline:
    def __init__(self, *, confidence=0.99):
        self.cache = {}
        self.plans = []
        self.physical_batches = 0
        self.confidence = confidence

    def run_plan(self, plan):
        self.plans.append(plan)
        products = plan.input_json["products"]
        assert all(
            set(product["observed_source_fields"]) == {"color"}
            for product in products
        )
        assert all(
            "name" not in product and "description" not in product
            for product in products
        )
        missing = [
            fingerprint for fingerprint in plan.question_fingerprints
            if fingerprint not in self.cache
        ]
        if missing:
            self.physical_batches += 1
            answers = []
            for question in plan.questions:
                choice = next(
                    item for item in question.choices
                    if isinstance(item, DecisionChoice)
                    and item.description == "Pink"
                )
                answers.append(DecisionAnswer(
                    name=question.name,
                    status="answered",
                    choice=choice.value,
                    confidence=self.confidence,
                ))
            decoded = decode_decision_plan(
                plan,
                DecisionOutcome("success", answers=tuple(answers)),
            )
            self.cache.update({row.signature: row for row in decoded})
        return SimpleNamespace(
            phase="characteristic",
            results=tuple(self.cache[key] for key in plan.question_fingerprints),
        )


class _FakeAbstainingDecisionsPipeline:
    def __init__(self):
        self.plans = []

    def run_plan(self, plan):
        self.plans.append(plan)
        return SimpleNamespace(
            phase="characteristic",
            results=tuple(
                DecisionItemResult(
                    signature=fingerprint,
                    phase="characteristic",
                    status="abstained",
                    reason="insufficient_evidence",
                )
                for fingerprint in plan.question_fingerprints
            ),
        )


class _FakeCategoryPipeline:
    def __init__(self):
        self.calls = 0

    def run_category_phase(self, contexts):
        self.calls += 1
        plan = plan_category_phase(contexts)
        answers = []
        for question in plan.questions:
            selected = next(
                choice for choice in question.choices
                if isinstance(choice, DecisionChoice)
                and choice.description.endswith("тип товара: Тип")
            )
            answers.append(DecisionAnswer(
                name=question.name,
                status="answered",
                choice=selected.value,
                confidence=0.99,
            ))
        results = decode_decision_plan(
            plan,
            DecisionOutcome("success", answers=tuple(answers)),
        )
        return SimpleNamespace(
            results=results,
            physical_request_started=True,
            reservation_state="ready",
            finish_state="success",
        )


class _CooldownThenCategoryPipeline:
    def __init__(self, retry_at):
        self.retry_at = retry_at
        self.category_calls = 0
        self.selected = _FakeCategoryPipeline()
        self.colors = _FakeDecisionsPipeline()

    def run_category_phase(self, contexts):
        self.category_calls += 1
        if self.category_calls > 1:
            return self.selected.run_category_phase(contexts)
        plan = plan_category_phase(contexts)
        results = tuple(
            DecisionItemResult(
                signature=fingerprint,
                phase="category",
                status="abstained",
                reason="rate_limited",
            )
            for fingerprint in plan.question_fingerprints
        )
        return SimpleNamespace(
            results=results,
            physical_request_started=True,
            reservation_state="ready",
            finish_state="deferred",
            retry_at=self.retry_at,
        )

    def run_plan(self, plan):
        return self.colors.run_plan(plan)


class _FakeCategoryClient:
    def __init__(self):
        self.calls = []

    def decide(self, *, input_json, questions):
        questions = tuple(questions)
        self.calls.append({
            "input_json": input_json,
            "questions": questions,
        })
        answers = []
        for question in questions:
            selected = next(
                choice for choice in question.choices
                if isinstance(choice, DecisionChoice)
                and choice.description.endswith("тип товара: Тип")
            )
            answers.append(DecisionAnswer(
                name=question.name,
                status="answered",
                choice=selected.value,
                confidence=0.99,
            ))
        return DecisionOutcome("success", answers=tuple(answers))


class _FakeColorClient:
    def __init__(self):
        self.calls = []

    def decide(self, *, input_json, questions):
        questions = tuple(questions)
        self.calls.append((input_json, questions))
        answers = []
        for question in questions:
            choice = next(
                option for option in question.choices
                if isinstance(option, DecisionChoice)
                and option.description != "Недостаточно точных сведений для выбора"
            )
            answers.append(DecisionAnswer(
                name=question.name,
                status="answered",
                choice=choice.value,
                confidence=0.99,
            ))
        return DecisionOutcome("success", answers=tuple(answers))


class OzonPrepareDecisionsQueueTest(OzonPublicationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=False,
        )
        self.account.settings_json = '{"default_vat":"0.22"}'
        self.product_type.is_seller_selectable = True
        self._add_color_dictionary([
            ("101", "Pink"),
            ("102", "Deep pink"),
            ("103", "Черный"),
            ("104", "Красный"),
            ("105", "Серый"),
            ("106", "Розовый"),
        ])
        db.session.commit()

    def _add_color_dictionary(self, values):
        now = datetime.utcnow()
        definition = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="10096",
            name="Цвет товара",
            data_type="String",
            is_required=False,
            dictionary_id="1494",
            max_value_count=1,
            is_collection=False,
            is_available=True,
            is_enabled=True,
            last_seen_at=now,
            values_synced_at=now,
            values_sync_status="success",
            values_snapshot_hash="color-values-v1",
            values_version=1,
            values_count=len(values),
        )
        db.session.add(definition)
        db.session.flush()
        for external_id, value in values:
            db.session.add(MarketplaceAttributeValue(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                attribute_id=definition.id,
                external_value_id=external_id,
                value=value,
                value_normalized=OzonReferenceService.normalize_value(value),
                is_available=True,
                last_seen_at=now,
            ))
        self.color_definition = definition

    def _append_color_dictionary_values(self, values):
        now = datetime.utcnow()
        for external_id, value in values:
            db.session.add(MarketplaceAttributeValue(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                attribute_id=self.color_definition.id,
                external_value_id=external_id,
                value=value,
                value_normalized=OzonReferenceService.normalize_value(value),
                is_available=True,
                last_seen_at=now,
            ))
        self.color_definition.values_count += len(values)
        self.color_definition.values_version += 1
        self.color_definition.values_snapshot_hash = (
            f"color-values-v{self.color_definition.values_version}"
        )
        self.color_definition.values_synced_at = now
        db.session.flush()

    def test_shortlist_uses_hue_tokens_not_brightness_or_complexion_modifiers(self):
        self._append_color_dictionary_values([
            ("201", "Темно-синий"),
            ("202", "Темно-зеленый"),
            ("203", "Светло-синий"),
            ("204", "Светло-зеленый"),
            ("205", "Бордовый"),
            ("206", "Темно-бордовый"),
            ("207", "Пурпурный"),
            ("208", "Темно-пурпурный"),
        ])

        shortlist = OzonPrepareDecisionsService._shortlisted_dictionary_values
        self.assertEqual(
            shortlist(
                definition=self.color_definition,
                literal="темно-медовый",
            ),
            [],
        )
        self.assertEqual(
            shortlist(
                definition=self.color_definition,
                literal="темноволосый",
            ),
            [],
        )
        self.assertEqual(
            shortlist(
                definition=self.color_definition,
                literal="светлокожий",
            ),
            [],
        )
        self.assertEqual(
            OzonPrepareDecisionsService._tokens("темнопурпурный"),
            {"пурпурный"},
        )
        self.assertEqual(
            OzonPrepareDecisionsService._tokens("светло-розовый"),
            {"розовый"},
        )
        self.assertEqual(
            [row.value for row, _score in shortlist(
                definition=self.color_definition,
                literal="бордо",
            )],
            ["Бордовый", "Темно-бордовый"],
        )
        self.assertEqual(
            [row.value for row, _score in shortlist(
                definition=self.color_definition,
                literal="темнопурпурный",
            )],
            ["Пурпурный", "Темно-пурпурный"],
        )

    def _source_product(
        self, *, external_id, color, category_evidence=False,
        create_mapping=True,
    ):
        product = ImportedProduct.query.filter_by(id=self.source.id).first()
        if product is None or external_id != product.external_id:
            product = ImportedProduct(
                seller_id=self.seller.id,
                external_id=external_id,
                external_vendor_code=f"offer-{external_id}",
                source_type="synthetic",
                title="Тестовый источник цвета",
                description="Описание не передаётся модели",
                category="Категория",
            )
            db.session.add(product)
            db.session.flush()
        supplier = Supplier.query.filter_by(code="synthetic").first()
        if supplier is None:
            supplier = Supplier(name="Synthetic supplier", code="synthetic")
            db.session.add(supplier)
            db.session.flush()
        marker = {
            "observed_supplier_fields_version": 1,
            "observed_supplier_fields": {"colors": [color]},
            "colors": [color],
        }
        if category_evidence:
            marker.update({"title": product.title, "category": product.category})
        source_product = SupplierProduct(
            supplier_id=supplier.id,
            external_id=product.external_id,
            content_revision=7,
            title="Тестовый источник цвета",
            category="Категория",
            vendor_code=product.external_vendor_code,
            colors_json=json.dumps([color], ensure_ascii=False),
            original_data_json=json.dumps(marker, ensure_ascii=False),
        )
        db.session.add(source_product)
        db.session.flush()
        product.supplier_product_id = source_product.id
        product.supplier_id = supplier.id
        product.source_type = supplier.code
        product.supplier_content_revision = 7
        product.original_data = json.dumps(marker, ensure_ascii=False)
        product.colors = json.dumps([color], ensure_ascii=False)
        db.session.flush()
        if create_mapping:
            self._ensure_mapping(product)
        return product

    def _ensure_mapping(self, product):
        identity = MarketplaceDraftService._mapping_identities(product)[-1]
        existing = MarketplaceCategoryMapping.query.filter_by(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            scope_key=identity["scope_key"],
            source_category_normalized=identity["source_category_normalized"],
        ).first()
        if existing is not None:
            return existing
        mapping = MarketplaceCategoryMapping(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            supplier_id=identity["supplier_id"],
            product_type_id=self.product_type.id,
            scope_key=identity["scope_key"],
            source_type=identity["source_type"],
            source_category=identity["source_category"],
            source_category_normalized=identity["source_category_normalized"],
            external_category_id="10",
            external_type_id="777",
            mapping_source="manual",
            mapping_status="active",
            confidence=1.0,
            evidence_json="{}",
        )
        db.session.add(mapping)
        db.session.flush()
        return mapping

    def _accept_and_claim(self, products):
        accepted = OzonBulkUploadService.accept_source_prepare(
            seller_id=self.seller.id,
            account_id=self.account.id,
            imported_product_ids=[product.id for product in products],
            request_key="local-color-queue-test-0001",
            created_by_user_id=self.user.id,
        )
        run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).one()
        token = OzonUploadQueueService.claim_due_run(
            now=datetime.utcnow(), exclude_run_ids=set(),
        )[1]
        return run, token

    def _advance(
        self, run, token, product, *, now=None, deadline_seconds=35,
        validator=None,
    ):
        item = OzonBulkUploadItem.query.filter_by(
            run_id=run.id, imported_product_id=product.id,
        ).one()

        def ready_validation(*, seller_id, draft_id, expected_version,
                             stage_automatic_cleanup=False):
            draft = MarketplaceDraftService.get_draft(
                seller_id=seller_id, draft_id=draft_id,
            )
            draft.status = "ready"
            draft.validation_status = "valid"
            db.session.commit()
            return MarketplaceDraftService.get_draft(
                seller_id=seller_id, draft_id=draft_id,
            )

        validation = validator or ready_validation
        with patch.object(
            MarketplaceDraftService,
            "validate_draft",
            side_effect=validation,
        ):
            result = OzonUploadQueueService.advance_item(
                run_id=run.id,
                item_id=item.id,
                lease_token=token,
                now=now or datetime.utcnow(),
                deadline_monotonic=time.monotonic() + deadline_seconds,
            )
        db.session.refresh(item)
        return item, result

    @staticmethod
    def _color(draft):
        rows = json.loads(draft.attributes_json or "[]")
        return next(
            (row["values"] for row in rows
             if row.get("attribute_id") == "10096"),
            [],
        )

    def _prepare_using_catalog_color(self, *, withdraw_during_decision):
        db.session.delete(self.draft)
        product = self._source_product(external_id="catalog-proven-color", color="light pink")
        raw = json.dumps({"colors": ["light pink"]})
        product.original_data = raw
        source = product.supplier_product
        source.original_data_json = raw
        db.session.commit()
        supplier_id = source.supplier_id
        product_id = source.id
        scope = supplier_catalog_config_digest(source.supplier)
        observed_at = datetime.utcnow() - timedelta(hours=25)
        with TemporaryDirectory() as folder:
            store = SupplierColorObservationStore(Path(folder) / "catalog-colors.sqlite3")
            claim = store.reserve(supplier_id, scope, now=observed_at)
            self.assertTrue(claim)
            self.assertTrue(store.finish(supplier_id, claim, scope, {
                product_id: (
                    source.content_revision,
                    sha256(source.external_id.encode()).hexdigest(),
                    ("light pink",),
                ),
            }, observed_at=observed_at))
            run, token = self._accept_and_claim([product])
            pipeline = _FakeDecisionsPipeline()
            run_plan = pipeline.run_plan

            def decide(plan):
                result = run_plan(plan)
                if withdraw_during_decision:
                    now = datetime.utcnow()
                    replacement = store.reserve(supplier_id, scope, now=now)
                    self.assertTrue(replacement)
                    self.assertTrue(store.finish(supplier_id, replacement, scope, {}, observed_at=now))
                return result

            with patch(
                "services.supplier_color_observation_store.configured_supplier_color_observation_store",
                return_value=store,
            ), patch(
                "services.openai_decisions_runtime.create_openai_decisions_pipeline",
                return_value=pipeline,
            ), patch.object(pipeline, "run_plan", side_effect=decide):
                item, result = self._advance(run, token, product)
        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=item.draft_id,
        )
        self.assertEqual(result.outcome, "prepared")
        self.assertEqual(pipeline.physical_batches, 1)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        self.assertEqual(product.original_data, raw)
        self.assertEqual(source.original_data_json, raw)
        return self._color(draft)

    def test_normal_queue_uses_a_fresh_catalog_color_for_a_legacy_copy(self):
        self.assertEqual(self._prepare_using_catalog_color(withdraw_during_decision=False), [
            {"value": "Pink", "dictionary_value_id": "101"},
        ])

    def test_normal_queue_ignores_a_choice_if_catalog_proof_was_withdrawn(self):
        self.assertEqual(self._prepare_using_catalog_color(withdraw_during_decision=True), [])

    def test_queue_shortlists_hue_without_modifier_or_complexion_only_calls(self):
        db.session.delete(self.draft)
        self._append_color_dictionary_values([
            ("201", "Темно-синий"),
            ("202", "Темно-зеленый"),
            ("203", "Светло-синий"),
            ("204", "Светло-зеленый"),
            ("205", "Бордовый"),
            ("206", "Темно-бордовый"),
        ])
        products = [
            self._source_product(external_id="queue-light-pink", color="light pink"),
            self._source_product(external_id="queue-dark-honey", color="темно-медовый"),
            self._source_product(external_id="queue-dark-hair", color="темноволосый"),
            self._source_product(external_id="queue-light-skin", color="светлокожий"),
            self._source_product(
                external_id="queue-mixed-colors", color="чёрный с розовым",
            ),
        ]
        db.session.commit()
        run, token = self._accept_and_claim(products)
        pipeline = _FakeDecisionsPipeline()
        original_auto_map = MarketplaceDraftService._auto_map_attributes

        def without_default_color(**kwargs):
            attributes, report = original_auto_map(**kwargs)
            return [
                row for row in attributes
                if row.get("attribute_id") != "10096"
            ], report

        with patch.object(
            MarketplaceDraftService,
            "_auto_map_attributes",
            side_effect=without_default_color,
        ), patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ) as runtime_factory:
            results = [
                self._advance(run, token, product)
                for product in products
            ]

        self.assertTrue(all(result.outcome == "prepared" for _, result in results))
        self.assertEqual(len(pipeline.plans), 1)
        self.assertEqual(pipeline.physical_batches, 1)
        runtime_factory.assert_called_once()
        for (item, _result), product in zip(results, products):
            draft = MarketplaceDraftService.get_draft(
                seller_id=self.seller.id, draft_id=item.draft_id,
            )
            expected = (
                [{"value": "Pink", "dictionary_value_id": "101"}]
                if product is products[0] else []
            )
            self.assertEqual(self._color(draft), expected)
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_queue_preserves_explicit_abstention_for_ambiguous_shade(self):
        db.session.delete(self.draft)
        product = self._source_product(
            external_id="queue-abstained-light-pink", color="light pink",
        )
        db.session.commit()
        run, token = self._accept_and_claim([product])
        pipeline = _FakeAbstainingDecisionsPipeline()
        original_auto_map = MarketplaceDraftService._auto_map_attributes

        def without_default_color(**kwargs):
            attributes, report = original_auto_map(**kwargs)
            return [
                row for row in attributes
                if row.get("attribute_id") != "10096"
            ], report

        with patch.object(
            MarketplaceDraftService,
            "_auto_map_attributes",
            side_effect=without_default_color,
        ), patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ) as runtime_factory:
            item, result = self._advance(run, token, product)

        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=item.draft_id,
        )
        self.assertEqual(result.outcome, "prepared")
        self.assertEqual(len(pipeline.plans), 1)
        self.assertEqual(
            {
                choice.description
                for choice in pipeline.plans[0].questions[0].choices
                if isinstance(choice, DecisionChoice)
            },
            {
                "Pink",
                "Deep pink",
                "Недостаточно точных сведений для выбора",
            },
        )
        self.assertEqual(self._color(draft), [])
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        runtime_factory.assert_called_once()

    def test_one_queue_batch_warms_virtual_sibling_and_later_reuses_cache(self):
        # The fixture draft belongs to an older/manual path and is removed so
        # the first queue item is an eligible newly-created local draft.
        db.session.delete(self.draft)
        first = self._source_product(external_id="source-first", color="light pink")
        second = self._source_product(external_id="source-second", color="soft pink")
        db.session.commit()
        run, token = self._accept_and_claim([first, second])
        pipeline = _FakeDecisionsPipeline()

        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            first_item, first_result = self._advance(run, token, first)
            self.assertEqual(first_result.outcome, "prepared")
            first_draft = MarketplaceDraftService.get_draft(
                seller_id=self.seller.id,
                draft_id=first_item.draft_id,
            )
            self.assertEqual(
                self._color(first_draft),
                [{"value": "Pink", "dictionary_value_id": "101"}],
            )
            self.assertIsNone(MarketplaceProductDraft.query.filter_by(
                seller_id=self.seller.id,
                account_id=self.account.id,
                imported_product_id=second.id,
            ).first())

            second_item, second_result = self._advance(run, token, second)
            self.assertEqual(second_result.outcome, "prepared")
            second_draft = MarketplaceDraftService.get_draft(
                seller_id=self.seller.id,
                draft_id=second_item.draft_id,
            )
            self.assertEqual(
                self._color(second_draft),
                [{"value": "Pink", "dictionary_value_id": "101"}],
            )

        self.assertEqual(len(pipeline.plans), 2)
        self.assertEqual(len(pipeline.plans[0].input_json["products"]), 2)
        self.assertEqual(pipeline.physical_batches, 1)
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_known_compound_color_resolves_locally_without_decisions(self):
        db.session.delete(self.draft)
        products = [
            self._source_product(
                external_id="source-black", color="угольно-чёрный",
            ),
            self._source_product(
                external_id="source-pink", color="нежно-розовый",
            ),
        ]
        db.session.commit()
        run, token = self._accept_and_claim(products)
        pipeline = _FakeDecisionsPipeline()
        original_auto_map = MarketplaceDraftService._auto_map_attributes

        def no_default_color(**kwargs):
            attributes, report = original_auto_map(**kwargs)
            return [
                row for row in attributes
                if row.get("attribute_id") != "10096"
            ], report

        with patch.object(
            MarketplaceDraftService,
            "_auto_map_attributes",
            side_effect=no_default_color,
        ), patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ) as runtime_factory:
            results = [self._advance(run, token, product) for product in products]
        self.assertTrue(all(result.outcome == "prepared" for _, result in results))
        expected = (
            [{"value": "Черный", "dictionary_value_id": "103"}],
            [{"value": "Розовый", "dictionary_value_id": "106"}],
        )
        for (item, _result), expected_color in zip(results, expected):
            draft = MarketplaceDraftService.get_draft(
                seller_id=self.seller.id, draft_id=item.draft_id,
            )
            self.assertEqual(self._color(draft), expected_color)
        self.assertEqual(pipeline.physical_batches, 0)
        runtime_factory.assert_not_called()

    def test_uncertain_color_observations_never_fill_or_reach_decisions(self):
        db.session.delete(self.draft)
        products = [
            self._source_product(
                external_id=f"source-uncertain-{index}",
                color=color,
            )
            for index, color in enumerate((
                "серия", "не чёрный", "чёрный с розовым",
            ))
        ]
        db.session.commit()
        run, token = self._accept_and_claim(products)
        pipeline = _FakeDecisionsPipeline()
        original_auto_map = MarketplaceDraftService._auto_map_attributes
        original_observed_colors = MarketplaceDraftService._observed_colors

        def without_default_color(**kwargs):
            attributes, report = original_auto_map(**kwargs)
            return [
                row for row in attributes
                if row.get("attribute_id") != "10096"
            ], report

        def unsafe_legacy_colors(value):
            literal = value[0] if isinstance(value, list) and value else value
            if literal == "серия":
                return ["Серый"]
            if literal == "не чёрный":
                return ["Черный"]
            if literal == "чёрный с розовым":
                return ["Черный", "Розовый"]
            return original_observed_colors(value)

        with patch.object(
            MarketplaceDraftService,
            "_auto_map_attributes",
            side_effect=without_default_color,
        ), patch.object(
            MarketplaceDraftService,
            "_observed_colors",
            side_effect=unsafe_legacy_colors,
        ), patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ) as runtime_factory:
            results = [self._advance(run, token, product) for product in products]

        self.assertTrue(all(result.outcome == "prepared" for _, result in results))
        self.assertEqual(pipeline.plans, [])
        runtime_factory.assert_not_called()
        for item, _result in results:
            draft = MarketplaceDraftService.get_draft(
                seller_id=self.seller.id, draft_id=item.draft_id,
            )
            self.assertEqual(self._color(draft), [])

    def test_color_timestamp_refresh_reuses_cache_but_dictionary_change_does_not(self):
        product = self._source_product(
            external_id=self.source.external_id,
            color="light pink",
            create_mapping=False,
        )
        run, _token = self._accept_and_claim([product])
        item = OzonBulkUploadItem.query.filter_by(
            run_id=run.id, imported_product_id=product.id,
        ).one()

        def current_plan():
            candidate = OzonPrepareDecisionsService._candidate(
                run=run,
                item=item,
                product=product,
                draft=self.draft,
                product_type=self.product_type,
            )
            self.assertIsNotNone(candidate)
            return plan_characteristic_phase([candidate.context])

        initial_time = datetime.utcnow() - timedelta(seconds=30)
        self.marketplace.categories_synced_at = initial_time
        self.product_type.attributes_synced_at = initial_time
        self.color_definition.values_synced_at = initial_time
        db.session.commit()
        first_plan = current_plan()
        self.assertEqual(len(first_plan.question_fingerprints), 1)
        client = _FakeColorClient()

        with TemporaryDirectory() as private_dir:
            os.chmod(private_dir, 0o700)
            pipeline = OpenAIDecisionsPipeline(
                OpenAIDecisionsCache(
                    str(Path(private_dir) / "decisions.sqlite3"),
                    max_calls_per_day=10,
                ),
                client,
            )
            first = pipeline.run_plan(first_plan, now=initial_time)
            self.assertEqual(first.results[0].status, "selected")
            self.assertEqual(len(client.calls), 1)

            refreshed_time = datetime.utcnow()
            self.marketplace.categories_synced_at = refreshed_time
            self.marketplace.categories_snapshot_hash = "tree-refreshed-only"
            self.product_type.attributes_synced_at = refreshed_time
            self.color_definition.values_synced_at = refreshed_time
            db.session.commit()
            timestamp_refreshed_plan = current_plan()
            self.assertEqual(
                timestamp_refreshed_plan.question_fingerprints,
                first_plan.question_fingerprints,
            )
            timestamp_refreshed = pipeline.run_plan(
                timestamp_refreshed_plan,
                now=refreshed_time,
            )
            self.assertEqual(timestamp_refreshed.results[0].status, "selected")
            self.assertEqual(timestamp_refreshed.reservation_state, "cached")
            self.assertEqual(len(client.calls), 1)

            changed_value = MarketplaceAttributeValue.query.filter_by(
                attribute_id=self.color_definition.id,
                external_value_id="102",
            ).one()
            changed_value.value = "Soft pink"
            changed_value.value_normalized = OzonReferenceService.normalize_value(
                changed_value.value,
            )
            self.color_definition.values_version += 1
            self.color_definition.values_snapshot_hash = "color-values-v2"
            self.color_definition.values_synced_at = datetime.utcnow()
            db.session.commit()
            changed_dictionary_plan = current_plan()
            self.assertNotEqual(
                changed_dictionary_plan.question_fingerprints,
                first_plan.question_fingerprints,
            )
            changed_dictionary = pipeline.run_plan(
                changed_dictionary_plan,
                now=datetime.utcnow(),
            )
            self.assertEqual(changed_dictionary.results[0].status, "selected")
            self.assertEqual(len(client.calls), 2)

    def test_low_confidence_selection_is_never_applied(self):
        db.session.delete(self.draft)
        product = self._source_product(
            external_id="source-low-confidence", color="light pink",
        )
        db.session.commit()
        run, token = self._accept_and_claim([product])
        pipeline = _FakeDecisionsPipeline(confidence=0.94)
        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            item, result = self._advance(run, token, product)
        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=item.draft_id,
        )
        self.assertEqual(result.outcome, "prepared")
        self.assertEqual(self._color(draft), [])
        self.assertEqual(pipeline.physical_batches, 1)

    def test_source_queue_selects_exact_category_type_without_mapping_write(self):
        db.session.delete(self.draft)
        product = self._source_product(
            external_id="source-category",
            color="pink",
            category_evidence=True,
            create_mapping=False,
        )
        db.session.add(MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.product_type.category_id,
            external_type_id="778",
            name="Тип 2",
            is_available=True,
            is_disabled_upstream=False,
            is_seller_selectable=True,
        ))
        db.session.commit()
        run, token = self._accept_and_claim([product])
        pipeline = _FakeCategoryPipeline()
        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            item, result = self._advance(run, token, product)
        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=item.draft_id,
        )
        self.assertEqual(result.outcome, "prepared")
        self.assertEqual(draft.product_type_id, self.product_type.id)
        self.assertEqual(MarketplaceCategoryMapping.query.count(), 0)
        self.assertEqual(pipeline.calls, 1)
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_source_queue_applies_type_from_exact_linked_category_peers(self):
        db.session.delete(self.draft)
        source_category = "Локальные товары без узла Ozon"
        product = self._source_product(
            external_id="source-observed-category",
            color="pink",
            category_evidence=True,
            create_mapping=False,
        )
        source_snapshot = json.loads(product.original_data)
        source_snapshot["category"] = source_category
        product.category = source_category
        product.original_data = json.dumps(source_snapshot, ensure_ascii=False)
        product.supplier_product.category = source_category
        supplier_snapshot = json.loads(
            product.supplier_product.original_data_json
        )
        supplier_snapshot["category"] = source_category
        product.supplier_product.original_data_json = json.dumps(
            supplier_snapshot, ensure_ascii=False,
        )

        now = datetime.utcnow()
        category = self.product_type.category
        category.last_seen_at = now
        self.product_type.last_seen_at = now
        self.product_type.attributes_synced_at = now
        observed_types = [self.product_type]
        other_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=category.id,
            external_type_id="778",
            name="Тип 2",
            is_available=True,
            is_disabled_upstream=False,
            is_seller_selectable=True,
            last_seen_at=now,
            attributes_synced_at=now,
            attributes_sync_status="success",
            attributes_schema_hash="schema-hash-2",
            attributes_version=1,
        )
        db.session.add(other_type)
        db.session.flush()
        observed_types.append(other_type)

        peer_title = product.title
        for type_index, product_type in enumerate(observed_types, start=1):
            for peer_index in range(2):
                ordinal = (type_index - 1) * 2 + peer_index + 1
                external_id = f"linked-observed-peer-{ordinal}"
                vendor_code = f"linked-observed-offer-{ordinal}"
                source_raw = json.dumps({
                    "title": peer_title,
                    "category": source_category,
                    "vendor_code": vendor_code,
                }, ensure_ascii=False, sort_keys=True)
                supplier_product = SupplierProduct(
                    supplier_id=product.supplier_id,
                    external_id=external_id,
                    content_revision=3,
                    title=peer_title,
                    category=source_category,
                    vendor_code=vendor_code,
                    original_data_json=source_raw,
                )
                db.session.add(supplier_product)
                db.session.flush()
                peer = ImportedProduct(
                    seller_id=self.seller.id,
                    supplier_id=product.supplier_id,
                    supplier_product_id=supplier_product.id,
                    supplier_content_revision=3,
                    external_id=external_id,
                    external_vendor_code=vendor_code,
                    source_type=product.source_type,
                    title=peer_title,
                    category=source_category,
                    original_data=source_raw,
                    import_status="pending",
                )
                db.session.add(peer)
                db.session.flush()
                listing = MarketplaceListing(
                    seller_id=self.seller.id,
                    marketplace_id=self.marketplace.id,
                    account_id=self.account.id,
                    imported_product_id=peer.id,
                    product_type_id=product_type.id,
                    offer_id=vendor_code,
                    external_product_id=f"ozon-peer-{ordinal}",
                    external_category_id="10",
                    external_type_id=product_type.external_type_id,
                    title=peer_title,
                    normalized_status="active",
                    link_status="linked",
                    link_source="exact_source_identity",
                    link_evidence_json=source_raw,
                    link_version=1,
                    linked_at=now,
                    last_seen_at=now,
                    info_synced_at=now,
                    attributes_synced_at=now,
                    attributes_json="[]",
                    is_available=True,
                    is_archived=False,
                    sync_fingerprint=f"{ordinal:064x}",
                )
                db.session.add(listing)
                db.session.flush()
                db.session.add(MarketplaceListingLinkEvent(
                    seller_id=self.seller.id,
                    marketplace_id=self.marketplace.id,
                    account_id=self.account.id,
                    listing_id=listing.id,
                    imported_product_id=peer.id,
                    action="auto_link",
                    source="exact_source_identity",
                    evidence_json=source_raw,
                    link_version=1,
                    created_at=now,
                ))
        db.session.commit()

        run, token = self._accept_and_claim([product])
        pipeline = _FakeCategoryPipeline()
        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            item, result = self._advance(run, token, product)
        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=item.draft_id,
        )
        self.assertEqual(result.outcome, "prepared")
        self.assertEqual(draft.product_type_id, self.product_type.id)
        self.assertEqual(draft.external_category_id, "10")
        self.assertEqual(draft.external_type_id, "777")
        self.assertEqual(MarketplaceCategoryMapping.query.count(), 0)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        self.assertEqual(pipeline.calls, 1)

    def test_source_queue_batches_and_caches_two_exact_category_questions(self):
        db.session.delete(self.draft)
        first = self._source_product(
            external_id="source-category-first",
            color="pink",
            category_evidence=True,
            create_mapping=False,
        )
        second = self._source_product(
            external_id="source-category-second",
            color="pink",
            category_evidence=True,
            create_mapping=False,
        )
        db.session.add(MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.product_type.category_id,
            external_type_id="778",
            name="Тип 2",
            is_available=True,
            is_disabled_upstream=False,
            is_seller_selectable=True,
        ))
        db.session.commit()
        run, token = self._accept_and_claim([first, second])
        client = _FakeCategoryClient()

        with TemporaryDirectory() as private_dir:
            os.chmod(private_dir, 0o700)
            pipeline = OpenAIDecisionsPipeline(
                OpenAIDecisionsCache(
                    str(Path(private_dir) / "decisions.sqlite3"),
                    max_calls_per_day=10,
                ),
                client,
            )
            with patch(
                "services.openai_decisions_runtime.create_openai_decisions_pipeline",
                return_value=pipeline,
            ):
                first_item, first_result = self._advance(run, token, first)
                self.assertEqual(first_result.outcome, "prepared")
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(len(client.calls[0]["questions"]), 2)
                self.assertEqual(
                    len(client.calls[0]["input_json"]["products"]), 2,
                )
                first_draft = MarketplaceDraftService.get_draft(
                    seller_id=self.seller.id,
                    draft_id=first_item.draft_id,
                )
                self.assertEqual(first_draft.product_type_id, self.product_type.id)

                second_item, second_result = self._advance(run, token, second)
                self.assertEqual(second_result.outcome, "prepared")
                second_draft = MarketplaceDraftService.get_draft(
                    seller_id=self.seller.id,
                    draft_id=second_item.draft_id,
                )
                self.assertEqual(second_draft.product_type_id, self.product_type.id)
                self.assertEqual(len(client.calls), 1)

        self.assertEqual(MarketplaceCategoryMapping.query.count(), 0)
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_category_cooldown_continues_only_after_retry_at_and_selects_exact_type(self):
        db.session.delete(self.draft)
        product = self._source_product(
            external_id="source-category-cooldown",
            color="pink",
            category_evidence=True,
            create_mapping=False,
        )
        db.session.add(MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.product_type.category_id,
            external_type_id="778",
            name="Тип 2",
            is_available=True,
            is_disabled_upstream=False,
            is_seller_selectable=True,
        ))
        db.session.commit()
        run, token = self._accept_and_claim([product])
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=45)
        pipeline = _CooldownThenCategoryPipeline(retry_at)

        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            item, first = self._advance(run, token, product)
            self.assertEqual(first.outcome, "waiting")
            self.assertEqual(item.phase, "preparing")
            self.assertEqual(item.error_code, "ozon_decisions_waiting_1")
            self.assertEqual(item.local_failure_count, 0)
            due = item.next_due_at
            self.assertGreaterEqual(
                due,
                retry_at.astimezone(timezone.utc).replace(tzinfo=None),
            )
            draft = MarketplaceDraftService.get_draft(
                seller_id=self.seller.id, draft_id=item.draft_id,
            )
            self.assertIsNone(draft.product_type_id)

            before_due = OzonUploadQueueService.advance_item(
                run_id=run.id,
                item_id=item.id,
                lease_token=token,
                now=due - timedelta(seconds=1),
                deadline_monotonic=time.monotonic() + 35,
            )
            self.assertEqual(before_due.outcome, "skipped")
            self.assertEqual(pipeline.category_calls, 1)

            item, second = self._advance(
                run, token, product, now=due + timedelta(seconds=1),
            )

        self.assertEqual(second.outcome, "prepared")
        self.assertEqual(pipeline.category_calls, 2)
        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=item.draft_id,
        )
        self.assertEqual(draft.product_type_id, self.product_type.id)
        self.assertEqual(item.phase, "prepared")
        self.assertIsNone(item.error_code)

    def test_category_continuation_stops_after_six_hour_horizon(self):
        db.session.delete(self.draft)
        product = self._source_product(
            external_id="source-category-overdue",
            color="pink",
            category_evidence=True,
            create_mapping=False,
        )
        db.session.add(MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.product_type.category_id,
            external_type_id="778",
            name="Тип 2",
            is_available=True,
            is_disabled_upstream=False,
            is_seller_selectable=True,
        ))
        db.session.commit()
        run, token = self._accept_and_claim([product])
        pipeline = _CooldownThenCategoryPipeline(
            datetime.now(timezone.utc) + timedelta(seconds=30),
        )

        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            item, waiting = self._advance(run, token, product)
            self.assertEqual(waiting.outcome, "waiting")
            due = item.next_due_at
            run.created_at = datetime.utcnow() - timedelta(hours=7)
            db.session.commit()

            item, resumed = self._advance(
                run, token, product, now=due + timedelta(seconds=1),
            )

        self.assertEqual(resumed.outcome, "prepared")
        self.assertEqual(pipeline.category_calls, 1)
        self.assertEqual(item.phase, "prepared")
        self.assertIsNone(item.error_code)

    def test_manual_edit_breaks_category_continuation_lineage(self):
        db.session.delete(self.draft)
        product = self._source_product(
            external_id="source-category-manual-edit",
            color="pink",
            category_evidence=True,
            create_mapping=False,
        )
        db.session.add(MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.product_type.category_id,
            external_type_id="778",
            name="Тип 2",
            is_available=True,
            is_disabled_upstream=False,
            is_seller_selectable=True,
        ))
        db.session.commit()
        run, token = self._accept_and_claim([product])
        pipeline = _CooldownThenCategoryPipeline(
            datetime.now(timezone.utc) + timedelta(seconds=30),
        )

        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            item, waiting = self._advance(run, token, product)
            self.assertEqual(waiting.outcome, "waiting")
            draft = MarketplaceDraftService.get_draft(
                seller_id=self.seller.id, draft_id=item.draft_id,
            )
            old_version = draft.version
            edited = MarketplaceDraftService.update_draft(
                seller_id=self.seller.id,
                draft_id=draft.id,
                expected_version=old_version,
                patch={"content": {"name": "Ручное изменение"}},
            )
            self.assertGreater(edited.version, old_version)

            item, resumed = self._advance(
                run, token, product,
                now=item.next_due_at + timedelta(seconds=1),
            )

        current = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=edited.id,
        )
        self.assertEqual(resumed.outcome, "prepared")
        self.assertEqual(pipeline.category_calls, 1)
        self.assertEqual(
            json.loads(current.content_json)["name"], "Ручное изменение",
        )
        self.assertIsNone(current.product_type_id)
        self.assertIsNone(item.error_code)

    def test_waiting_reference_continues_color_only_for_untouched_queue_draft(self):
        db.session.delete(self.draft)
        product = self._source_product(
            external_id="source-color-reference-wait", color="light pink",
        )
        now = datetime.utcnow()
        stale = now - timedelta(days=5)
        self.marketplace.categories_synced_at = stale
        self.product_type.attributes_synced_at = stale
        self.color_definition.values_synced_at = stale
        db.session.commit()
        run, token = self._accept_and_claim([product])
        pipeline = _FakeDecisionsPipeline()
        validation_count = 0

        def wait_then_ready(*, seller_id, draft_id, expected_version,
                            stage_automatic_cleanup=False):
            nonlocal validation_count
            validation_count += 1
            draft = MarketplaceDraftService.get_draft(
                seller_id=seller_id, draft_id=draft_id,
            )
            if validation_count == 1:
                draft.status = "draft"
                draft.validation_status = "invalid"
                draft.validation_result_json = json.dumps({
                    "errors": [{
                        "code": "schema_stale",
                        "field": "attributes",
                        "message": "Справочник ещё обновляется",
                    }],
                }, ensure_ascii=False)
            else:
                draft.status = "ready"
                draft.validation_status = "valid"
                draft.validation_result_json = "{}"
            db.session.commit()
            return MarketplaceDraftService.get_draft(
                seller_id=seller_id, draft_id=draft_id,
            )

        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            item, first = self._advance(
                run, token, product, validator=wait_then_ready,
            )
            self.assertEqual(first.outcome, "waiting")
            self.assertEqual(item.phase, "waiting_reference")
            self.assertEqual(item.error_code, "ozon_source_prepare_created")
            self.assertEqual(pipeline.physical_batches, 0)
            draft = MarketplaceDraftService.get_draft(
                seller_id=self.seller.id, draft_id=item.draft_id,
            )
            self.assertEqual(self._color(draft), [])

            item.next_due_at = datetime.utcnow() - timedelta(seconds=1)
            self.marketplace.categories_synced_at = datetime.utcnow()
            self.product_type.attributes_synced_at = datetime.utcnow()
            self.color_definition.values_synced_at = datetime.utcnow()
            db.session.commit()
            item, resumed = self._advance(
                run, token, product, validator=wait_then_ready,
            )

        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=item.draft_id,
        )
        self.assertEqual(resumed.outcome, "prepared")
        self.assertEqual(validation_count, 2)
        self.assertEqual(pipeline.physical_batches, 1)
        self.assertEqual(
            self._color(draft),
            [{"value": "Pink", "dictionary_value_id": "101"}],
        )
        self.assertIsNone(item.error_code)

    def test_expired_reference_wait_drops_queue_creation_model_lineage(self):
        db.session.delete(self.draft)
        product = self._source_product(
            external_id="source-color-reference-expired", color="light pink",
        )
        stale = datetime.utcnow() - timedelta(days=5)
        self.marketplace.categories_synced_at = stale
        self.product_type.attributes_synced_at = stale
        self.color_definition.values_synced_at = stale
        db.session.commit()
        run, token = self._accept_and_claim([product])
        pipeline = _FakeDecisionsPipeline()
        validation_count = 0

        def wait_then_ready(*, seller_id, draft_id, expected_version,
                            stage_automatic_cleanup=False):
            nonlocal validation_count
            validation_count += 1
            draft = MarketplaceDraftService.get_draft(
                seller_id=seller_id, draft_id=draft_id,
            )
            if validation_count == 1:
                draft.status = "draft"
                draft.validation_status = "invalid"
                draft.validation_result_json = json.dumps({
                    "errors": [{
                        "code": "schema_stale",
                        "field": "attributes",
                        "message": "Справочник ещё обновляется",
                    }],
                }, ensure_ascii=False)
            else:
                draft.status = "ready"
                draft.validation_status = "valid"
                draft.validation_result_json = "{}"
            db.session.commit()
            return MarketplaceDraftService.get_draft(
                seller_id=seller_id, draft_id=draft_id,
            )

        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            item, first = self._advance(
                run, token, product, validator=wait_then_ready,
            )
            self.assertEqual(first.outcome, "waiting")
            self.assertEqual(item.error_code, "ozon_source_prepare_created")

            item.reference_wait_started_at = datetime.utcnow() - timedelta(hours=7)
            item.next_due_at = datetime.utcnow() - timedelta(seconds=1)
            self.marketplace.categories_synced_at = datetime.utcnow()
            self.product_type.attributes_synced_at = datetime.utcnow()
            self.color_definition.values_synced_at = datetime.utcnow()
            db.session.commit()
            item, resumed = self._advance(
                run, token, product, validator=wait_then_ready,
            )

        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=item.draft_id,
        )
        self.assertEqual(resumed.outcome, "prepared")
        self.assertEqual(validation_count, 2)
        self.assertEqual(pipeline.physical_batches, 0)
        self.assertEqual(pipeline.plans, [])
        self.assertEqual(self._color(draft), [])
        self.assertIsNone(item.error_code)

    def test_manual_draft_waiting_reference_has_no_creation_lineage_for_category(self):
        db.session.delete(self.draft)
        product = self._source_product(
            external_id="source-existing-untyped-draft",
            color="pink",
            category_evidence=True,
            create_mapping=False,
        )
        db.session.add(MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.product_type.category_id,
            external_type_id="778",
            name="Тип 2",
            is_available=True,
            is_disabled_upstream=False,
            is_seller_selectable=True,
        ))
        db.session.commit()
        existing_draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller.id,
            account_id=self.account.id,
            imported_product_id=product.id,
        )
        self.assertIsNone(existing_draft.product_type_id)
        run, token = self._accept_and_claim([product])
        item = OzonBulkUploadItem.query.filter_by(
            run_id=run.id, imported_product_id=product.id,
        ).one()
        item.draft_id = existing_draft.id
        item.prepared_version = existing_draft.version
        item.phase = "waiting_reference"
        item.next_due_at = datetime.utcnow() - timedelta(seconds=1)
        item.error_code = "ozon_reference_sync_pending"
        item.reference_wait_started_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
        pipeline = _FakeCategoryPipeline()

        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            item, result = self._advance(run, token, product)

        self.assertEqual(result.outcome, "prepared")
        self.assertEqual(item.phase, "prepared")
        self.assertIsNone(item.error_code)
        self.assertEqual(pipeline.calls, 0)
        self.assertIsNone(MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=existing_draft.id,
        ).product_type_id)

    def test_existing_manual_empty_draft_never_enters_decisions_or_gets_filled(self):
        product = self._source_product(
            external_id=self.source.external_id,
            color="light pink",
        )
        # Existing product is deliberately tied to an old editor draft with an
        # empty 10096 row; that empty value is seller intent, not a suggestion.
        self.draft.attributes_json = json.dumps([
            {"attribute_id": "10096", "complex_id": "0", "values": []},
        ])
        self.draft.product_type_id = self.product_type.id
        self.draft.external_category_id = "10"
        self.draft.external_type_id = "777"
        db.session.commit()
        run, token = self._accept_and_claim([product])
        pipeline = _FakeDecisionsPipeline()
        with patch(
            "services.openai_decisions_runtime.create_openai_decisions_pipeline",
            return_value=pipeline,
        ):
            item, _result = self._advance(run, token, product)
        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller.id, draft_id=item.draft_id,
        )
        self.assertEqual(self._color(draft), [])
        self.assertEqual(pipeline.plans, [])
        self.assertEqual(MarketplaceOperation.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
