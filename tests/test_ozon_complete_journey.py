"""Synthetic Ozon lifecycle checks with real routes, services and workers.

Only the AI transport outcome and marketplace adapter are synthetic. The local
queue, draft review, publication task, readback and tenant boundaries are real.
"""
from concurrent.futures import Future
from datetime import datetime
from io import BytesIO
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from flask import g
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from PIL import Image

from models import (
    AIParsingAttempt,
    ImportedProduct,
    MarketplaceAttributeDefinition,
    MarketplaceOperation,
    MarketplaceProductDraft,
    OzonDraftCompletionRun,
    OzonDraftCompletionSuggestion,
    OzonBulkUploadItem,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_operations import register_marketplace_operation_routes
from routes.marketplace_drafts import register_marketplace_draft_routes
from routes.ozon_bulk_uploads import register_ozon_bulk_upload_routes
from routes.ozon_draft_ai import register_ozon_draft_ai_routes
from services.marketplace_drafts import MarketplaceDraftService
from services.marketplace_image_assets import _store_jpeg, public_asset_url
from services.marketplace_publications import MarketplacePublicationService
from services.ozon_bulk_upload import OzonBulkUploadService
from services.ozon_draft_ai_transport import FlashOutcome, FlashUsage
from services import ozon_write_quarantine
from services import ozon_draft_ai_worker
from tests.test_marketplace_publications import (
    OzonPublicationFixture,
    SyntheticFullStateAdapter,
    SYNTHETIC_CREDENTIALS,
)


class ImmediateExecutor:
    """Complete the real worker's submitted boundary without a model call."""

    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except Exception as exc:  # pragma: no cover - reported by worker state
            future.set_exception(exc)
        return future


class SyntheticOzonProvider(SyntheticFullStateAdapter):
    """Provider-boundary fixture; all caller-side services remain real."""

    capabilities = {"catalog_read", "catalog_write"}

    def __init__(self, *, ambiguous=False):
        super().__init__(create_mode=True)
        self.ambiguous = ambiguous
        self.physical_writes = 0

    def require_capability(self, capability):
        if capability not in self.capabilities:
            raise AssertionError(f"unexpected capability: {capability}")

    def submit_products(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.physical_writes += 1
        self.submitted_payloads.append(json.loads(json.dumps(payload)))
        if self.ambiguous:
            # Provider accepted the product but the response was lost.
            self.live_payload = json.loads(json.dumps(payload))
            self.offer_exists = True
            from services.ozon_api_client import OzonAmbiguousWriteError
            raise OzonAmbiguousWriteError(
                "synthetic lost response",
                code="synthetic_ambiguous_write",
                request_id="synthetic-request",
            )
        return super().submit_products(credentials, payload)


class OzonCompleteJourneyTest(OzonPublicationFixture, unittest.TestCase):
    branch_counts = {
        "success": {"ai_calls": 0, "provider_writes": 0, "readbacks": 0},
        "invalid": {"ai_calls": 0, "provider_writes": 0},
        "repair": {"ai_calls": 0, "human_repairs": 0, "repreparations": 0},
        "clean_new_source": {
            "new_sources": 0,
            "prepared_drafts": 0,
            "category_selections": 0,
            "human_packaging_vat": 0,
            "fresh_validations": 0,
            "provider_writes": 0,
            "full_readbacks": 0,
        },
        "unknown": {
            "provider_writes": 0,
            "automatic_retries": 0,
            "quarantines": 0,
            "safe_reconciliations": 0,
        },
    }

    def setUp(self):
        self._encryption_env = patch.dict(
            os.environ,
            {"ENCRYPTION_KEY": Fernet.generate_key().decode("ascii")},
        )
        self._encryption_env.start()
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory(prefix="ozon-journey-")
        self.addCleanup(self.temporary.cleanup)
        self.app.config.update(
            SECRET_KEY="synthetic-ozon-journey-secret",
            ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"),
            PUBLIC_BASE_URL="https://seller.test",
            MARKETPLACE_IMAGE_ASSET_DIR=self.temporary.name,
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
            MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=False,
            MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
            WTF_CSRF_ENABLED=True,
            SESSION_COOKIE_SECURE=False,
            OZON_RATE_LIMIT_DIR=os.path.join(self.temporary.name, "limits"),
        )
        login = LoginManager(self.app)
        login.user_loader(lambda identifier: db.session.get(User, int(identifier)))
        CSRFProtect(self.app)
        register_ozon_draft_ai_routes(self.app)
        register_marketplace_draft_routes(self.app)
        register_ozon_bulk_upload_routes(self.app)
        register_marketplace_operation_routes(self.app)
        self.client = self.app.test_client()
        self._login(self.user.id)
        self.account.set_credentials({
            "client_id": "synthetic-client",
            "api_key": "synthetic-api-key",
        })
        self.account.settings_json = json.dumps({"default_vat": "0.22"})
        self._install_local_photo()
        self.source.original_data = json.dumps({
            "title": "Безопасный товар янтарного цвета",
            "description": "Цвет: янтарный. Поверхность: гладкая.",
        }, ensure_ascii=False)
        self._add_ai_attributes()
        db.session.commit()
        self.expected_version = self.draft.version
        profile = type("Profile", (), {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "api_key": "synthetic-ai-key",
            "api_base_url": "https://api.deepseek.com/v1",
            "task_profile": "seller_draft_completion_flash",
            "max_retries": 1,
            "proxy_enabled": False,
        })()
        self._profile_patches = [
            patch(name, return_value=profile)
            for name in (
                "services.ozon_draft_ai_completion.resolve_config",
                "services.ozon_draft_ai_worker.resolve_config",
            )
        ]
        for mocked in self._profile_patches:
            mocked.start()
            self.addCleanup(mocked.stop)
        ozon_draft_ai_worker._inflight.clear()
        self.addCleanup(ozon_draft_ai_worker._inflight.clear)
        self.addCleanup(self._encryption_env.stop)

    def tearDown(self):
        super().tearDown()

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        g.pop("_login_user", None)

    def _csrf(self):
        response = self.client.get(
            f"/marketplaces/api/drafts/{self.draft.id}/ai-suggestions"
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["csrf_token"]

    def _post(self, path, body, *, csrf=None):
        return self.client.post(
            path,
            json=body,
            headers={"X-CSRFToken": csrf or self._csrf()},
        )

    def _install_local_photo(self):
        output = BytesIO()
        Image.new("RGB", (800, 800), (180, 130, 40)).save(
            output, format="JPEG", quality=90,
        )
        digest = _store_jpeg(output.getvalue(), config=self.app.config)
        self.photo_url = public_asset_url(
            config=self.app.config,
            secret_key=self.app.config["SECRET_KEY"],
            digest=digest,
        )
        self.source.photo_urls = json.dumps([self.photo_url])
        self.draft.media_json = json.dumps({
            "primary_image": self.photo_url,
            "images": [],
        })

    def _add_ai_attributes(self):
        now = datetime.utcnow()
        for external_id, name in (("909001", "Цвет"), ("909002", "Фактура")):
            db.session.add(MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                external_attribute_id=external_id,
                name=name,
                data_type="String",
                is_required=False,
                is_collection=False,
                max_value_count=1,
                is_available=True,
                is_enabled=True,
                last_seen_at=now,
            ))
        self.product_type.attributes_count += 2

    def _run_source_prepare(self):
        response = self._post("/marketplaces/ozon/uploads/", {
            "account_id": self.account.id,
            "imported_product_ids": [self.source.id],
            "confirm_prepare": True,
            "request_key": "journey-source-prepare-0000000001",
        })
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job_uid = response.get_json()["run"]["job_uid"]
        stats = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(stats["prepared"], 1, stats)
        document = self.client.get(
            f"/marketplaces/ozon/uploads/api/{job_uid}"
        )
        self.assertEqual(document.status_code, 200, document.get_data(as_text=True))
        self.assertEqual(document.get_json()["run"]["items"][0]["status"], "prepared")
        return job_uid

    def _run_ai(self, *, values=None, key="journey-ai-request-0000000001"):
        draft = db.session.get(MarketplaceProductDraft, self.draft.id)
        payload = {
            "account_id": self.account.id,
            "draft_ids": [draft.id],
            "expected_versions": {str(draft.id): draft.version},
            "confirm_generate": True,
            "request_key": key,
        }
        created = self._post("/marketplaces/api/drafts/ai-completions", payload)
        self.assertEqual(created.status_code, 202, created.get_data(as_text=True))
        run = created.get_json()["run"]
        suggestions = []
        for attribute_id, name, value in values or (
            ("909001", "Цвет", "янтарный"),
            ("909002", "Фактура", "гладкая"),
        ):
            suggestions.append({
                "attribute_id": attribute_id,
                "complex_id": "0",
                "group_ordinal": 0,
                "values": [{"value": value}],
                "evidence": [{"path": "/description", "quote": value}],
                "provenance_code": "literal_source",
            })
        outcome = FlashOutcome(
            "success",
            content={"items": [{"draft_id": draft.id, "suggestions": suggestions}]},
            http_status=200,
            usage=FlashUsage(prompt_tokens=100, completion_tokens=20),
        )
        with patch.object(
            ozon_draft_ai_worker, "flash_completion", return_value=outcome,
        ) as model:
            first = ozon_draft_ai_worker.tick(executor=ImmediateExecutor())
            second = ozon_draft_ai_worker.tick(executor=ImmediateExecutor())
        if first["started_calls"] != 1:
            from models import OzonDraftCompletionItem
            item_state = OzonDraftCompletionItem.query.order_by(
                OzonDraftCompletionItem.id.desc()
            ).first()
            self.fail(
                "AI worker did not admit the synthetic call: "
                f"first={first}; second={second}; "
                f"run={OzonDraftCompletionRun.query.order_by(OzonDraftCompletionRun.id.desc()).first().status}; "
                f"item={None if item_state is None else (item_state.status, item_state.safe_code)}"
            )
        self.assertEqual(second["completed_calls"], 1)
        model.assert_called_once()
        readback = self.client.get(
            f"/marketplaces/api/drafts/ai-completions/{run['job_uid']}"
        )
        self.assertEqual(readback.status_code, 200, readback.get_data(as_text=True))
        return run, readback.get_json()["run"]

    def test_source_ai_selective_review_upload_task_and_full_readback(self):
        source_job_uid = self._run_source_prepare()
        draft = db.session.get(MarketplaceProductDraft, self.draft.id)
        prepared_version = draft.version

        ai_run, ai_document = self._run_ai()
        self.assertEqual(ai_document["status"], "completed")
        review_page = self.client.get(
            f"/marketplaces/api/drafts/{draft.id}/ai-suggestions"
        )
        self.assertEqual(review_page.status_code, 200)
        suggestions = review_page.get_json()["suggestions"]
        self.assertEqual(len(suggestions), 2)
        color = next(row for row in suggestions if row["attribute_id"] == "909001")
        texture = next(row for row in suggestions if row["attribute_id"] == "909002")
        before_ai_review = review_page.get_json()["version"]
        applied = self._post(
            f"/marketplaces/api/drafts/{draft.id}/ai-suggestions/apply",
            {
                "suggestion_ids": [color["id"]],
                "expected_version": before_ai_review,
                "review_token": review_page.get_json()["review_token"],
                "request_key": "journey-ai-apply-0000000001",
            },
            csrf=review_page.get_json()["csrf_token"],
        )
        self.assertEqual(applied.status_code, 200, applied.get_data(as_text=True))
        self.assertEqual(
            applied.get_json()["review"]["version_after"], before_ai_review + 1,
        )
        reopened = self.client.get(
            f"/marketplaces/api/drafts/{draft.id}/ai-suggestions"
        ).get_json()
        self.assertEqual(reopened["version"], before_ai_review + 1)
        self.assertEqual(
            next(row for row in reopened["suggestions"] if row["id"] == color["id"])["status"],
            "accepted",
        )
        self.assertEqual(
            next(row for row in reopened["suggestions"] if row["id"] == texture["id"])["status"],
            "proposed",
        )
        draft = db.session.get(MarketplaceProductDraft, self.draft.id)
        self.assertGreater(draft.version, prepared_version)
        self.assertEqual(draft.account_id, self.account.id)
        validation = self._post(
            f"/marketplaces/drafts/{draft.id}/validate",
            {"expected_version": draft.version},
            csrf=reopened["csrf_token"],
        )
        self.assertEqual(validation.status_code, 200, validation.get_data(as_text=True))
        self.assertTrue(validation.get_json()["draft"]["validation"]["publishable"])
        draft = db.session.get(MarketplaceProductDraft, draft.id)

        reviewed = self._post("/marketplaces/ozon/uploads/from-drafts", {
            "account_id": self.account.id,
            "draft_ids": [draft.id],
            "confirm_write": True,
            "expected_versions": {str(draft.id): draft.version},
            "request_key": "journey-reviewed-upload-0000001",
            "parent_prepare_job_uid": source_job_uid,
        })
        self.assertEqual(reviewed.status_code, 202, reviewed.get_data(as_text=True))
        upload_uid = reviewed.get_json()["run"]["job_uid"]
        queued = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(queued["enqueued"], 1, queued)
        upload_item = OzonBulkUploadItem.query.filter_by(
            reviewed_draft_id=draft.id,
        ).one()
        operation = db.session.get(MarketplaceOperation, upload_item.operation_id)
        self.assertIsNotNone(operation)
        self.assertEqual(operation.status, "queued")
        self.assertEqual(operation.account_id, self.account.id)
        self.assertEqual(operation.draft_id, draft.id)

        provider = SyntheticOzonProvider()
        submitted = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=provider,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(submitted.status, "submitted")
        self.assertEqual(provider.physical_writes, 1)
        completed = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=provider,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(provider.physical_writes, 1)
        self.assertEqual(len(provider.full_read_calls), 4)
        self.assertEqual(provider.submitted_payloads[0]["items"][0]["primary_image"], self.photo_url)
        self.assertEqual(db.session.get(MarketplaceProductDraft, draft.id).status, "published")
        result = self.client.get(f"/marketplaces/ozon/uploads/api/{upload_uid}")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["run"]["items"][0]["status"], "succeeded")
        self.branch_counts["success"].update({
            "ai_calls": 1,
            "provider_writes": provider.physical_writes,
            "readbacks": len(provider.full_read_calls),
        })

        self._login(self.foreign_user.id)
        denied = self.client.get(
            f"/marketplaces/api/drafts/{draft.id}/ai-suggestions"
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(provider.physical_writes, 1)
        self.assertEqual(OzonDraftCompletionRun.query.count(), 1)
        self.assertGreater(AIParsingAttempt.query.count(), 0)

    def test_ready_explicit_vat_draft_can_be_selected_without_account_default_vat(self):
        self.account.settings_json = "{}"
        db.session.commit()

        with patch("routes.marketplace_drafts.render_template", return_value="ok") as render:
            response = self.client.get(
                f"/marketplaces/drafts/?account_id={self.account.id}"
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        template_context = render.call_args.kwargs
        account = next(
            row for row in template_context["account_options"]
            if row["id"] == self.account.id
        )
        draft = next(
            row for row in template_context["draft_cards"]
            if row["id"] == self.draft.id
        )

        self.assertIsNone(self.account.public_settings.get("default_vat"))
        self.assertTrue(account["can_publish"])
        self.assertEqual(draft["status"], "ready")
        self.assertTrue(draft["validation_summary"]["publishable"])

        commercial = json.loads(self.draft.commercial_json)
        commercial.pop("vat")
        self.draft.commercial_json = json.dumps(commercial)
        missing_per_draft_vat = MarketplaceDraftService._build_validation_result(
            self.draft
        )
        self.assertFalse(missing_per_draft_vat["publishable"])

    def test_invalid_ai_evidence_leaves_draft_unchanged_for_human_repair(self):
        self._run_source_prepare()
        draft = db.session.get(MarketplaceProductDraft, self.draft.id)
        version_before = draft.version
        _, run = self._run_ai(values=[
            ("909001", "Цвет", "голубой"),
        ], key="journey-invalid-ai-request-01")
        self.assertEqual(run["status"], "completed")
        # The validator rejects an unsupported value; no AI value is applied.
        proposals = OzonDraftCompletionSuggestion.query.all()
        self.assertEqual(len(proposals), 0)
        self.assertEqual(db.session.get(MarketplaceProductDraft, draft.id).version, version_before)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        self.assertEqual(AIParsingAttempt.query.count(), 1)
        self.branch_counts["invalid"]["ai_calls"] = 1

    def test_clean_new_source_creates_incomplete_draft_until_human_fills_packaging_and_vat(self):
        source = ImportedProduct(
            seller_id=self.seller.id,
            external_id="clean-new-source-1",
            external_vendor_code="clean-new-offer",
            source_type="synthetic",
            title="Новая карточка из общей карточки",
            description="Только исходное описание; упаковку продавец проверяет сам.",
            category="Категория",
            original_data=json.dumps({
                "title": "Новая карточка из общей карточки",
                "description": "Только исходное описание; упаковку продавец проверяет сам.",
            }, ensure_ascii=False),
            photo_urls=json.dumps([self.photo_url]),
        )
        db.session.add(source)
        db.session.commit()
        response = self._post("/marketplaces/ozon/uploads/", {
            "account_id": self.account.id,
            "imported_product_ids": [source.id],
            "confirm_prepare": True,
            "request_key": "clean-new-source-create-0001",
        })
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job_uid = response.get_json()["run"]["job_uid"]
        stats = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(stats["needs_input"], 1, stats)
        draft = MarketplaceProductDraft.query.filter_by(
            seller_id=self.seller.id,
            account_id=self.account.id,
            imported_product_id=source.id,
        ).one()
        self.assertNotEqual(draft.id, self.draft.id)
        self.assertNotEqual(draft.status, "ready")
        initial = self.client.get(f"/marketplaces/ozon/uploads/api/{job_uid}")
        self.assertEqual(initial.status_code, 200, initial.get_data(as_text=True))
        self.assertEqual(initial.get_json()["run"]["items"][0]["draft_id"], draft.id)
        self.assertEqual(initial.get_json()["run"]["items"][0]["status"], "needs_input")

        initial_version = draft.version
        impact = self.client.get(
            f"/marketplaces/drafts/{draft.id}/category-impact"
            f"?expected_version={initial_version}"
            f"&target_product_type_id={self.product_type.id}"
            "&save_mapping=false&page=1"
        )
        self.assertEqual(impact.status_code, 200, impact.get_data(as_text=True))
        impact_document = impact.get_json()
        category = self._post(
            f"/marketplaces/drafts/{draft.id}",
            {
                "expected_version": initial_version,
                "patch": {
                    "product_type_id": self.product_type.id,
                    "save_mapping": False,
                },
                "category_review_token": impact_document["review_token"],
            },
            csrf=self._csrf(),
        )
        self.assertEqual(category.status_code, 200, category.get_data(as_text=True))
        draft = db.session.get(MarketplaceProductDraft, draft.id)
        initial_version = draft.version
        content = json.loads(draft.content_json or "{}")
        content.update({
            "name": "Новая карточка из общей карточки",
            "description": "Только исходное описание; упаковку продавец проверяет сам.",
        })
        saved = self._post(f"/marketplaces/drafts/{draft.id}", {
            "expected_version": initial_version,
            "patch": {
                "content": content,
                "media": {"primary_image": self.photo_url, "images": []},
                "dimensions": {
                    "width": "200", "height": "30", "depth": "300",
                    "dimension_unit": "MILLIMETERS", "weight": "250",
                    "weight_unit": "GRAMS",
                },
                "barcodes": ["4600000000001"],
                "commercial": {
                    "price": "1000", "old_price": "1200",
                    "vat": "0.22", "currency_code": "RUB",
                },
            },
        })
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertGreater(saved.get_json()["draft"]["version"], initial_version)
        self.branch_counts["clean_new_source"].update({
            "new_sources": 1,
            "prepared_drafts": 1,
            "category_selections": 1,
            "human_packaging_vat": 1,
        })
        validated = self._post(
            f"/marketplaces/drafts/{draft.id}/validate",
            {"expected_version": saved.get_json()["draft"]["version"]},
        )
        self.assertEqual(validated.status_code, 200, validated.get_data(as_text=True))
        self.assertTrue(
            validated.get_json()["draft"]["validation"]["publishable"],
            validated.get_json()["draft"]["validation"],
        )
        self.branch_counts["clean_new_source"]["fresh_validations"] = 1
        draft = db.session.get(MarketplaceProductDraft, draft.id)
        reviewed = self._post("/marketplaces/ozon/uploads/from-drafts", {
            "account_id": self.account.id,
            "draft_ids": [draft.id],
            "confirm_write": True,
            "expected_versions": {str(draft.id): draft.version},
            "request_key": "clean-new-source-reviewed-0001",
            "parent_prepare_job_uid": job_uid,
        })
        self.assertEqual(reviewed.status_code, 202, reviewed.get_data(as_text=True))
        upload_uid = reviewed.get_json()["run"]["job_uid"]
        queued = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(queued["enqueued"], 1, queued)
        upload_item = OzonBulkUploadItem.query.filter_by(
            reviewed_draft_id=draft.id,
        ).one()
        operation = db.session.get(MarketplaceOperation, upload_item.operation_id)
        self.assertEqual(operation.account_id, self.account.id)
        self.assertEqual(operation.draft_id, draft.id)
        self.assertEqual(operation.operation_kind, "product_import")

        provider = SyntheticOzonProvider()
        submitted = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=provider,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(submitted.status, "submitted")
        self.assertEqual(provider.physical_writes, 1)
        self.assertEqual(
            provider.submitted_payloads[0]["items"][0]["offer_id"],
            draft.offer_id,
        )
        completed = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=provider,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(provider.physical_writes, 1)
        self.assertEqual(len(provider.full_read_calls), 4)
        result = self.client.get(f"/marketplaces/ozon/uploads/api/{upload_uid}")
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        self.assertEqual(result.get_json()["run"]["items"][0]["status"], "succeeded")
        self.branch_counts["clean_new_source"].update({
            "provider_writes": provider.physical_writes,
            "full_readbacks": len(provider.full_read_calls),
        })

    def test_declined_suggestion_human_repair_and_source_reprepare(self):
        color = MarketplaceAttributeDefinition.query.filter_by(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="909001",
        ).one()
        color.is_required = True
        self.product_type.required_attributes_count += 1
        db.session.commit()

        accepted = self._post("/marketplaces/ozon/uploads/", {
            "account_id": self.account.id,
            "imported_product_ids": [self.source.id],
            "confirm_prepare": True,
            "request_key": "journey-repair-initial-000001",
        })
        self.assertEqual(accepted.status_code, 202, accepted.get_data(as_text=True))
        first_uid = accepted.get_json()["run"]["job_uid"]
        first_stats = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(first_stats["needs_input"], 1, first_stats)
        draft = db.session.get(MarketplaceProductDraft, self.draft.id)
        self.assertNotEqual(draft.status, "ready")

        _, ai_run = self._run_ai(values=[("909001", "Цвет", "янтарный")],
                                 key="journey-repair-ai-request-0001")
        self.assertEqual(ai_run["status"], "completed")
        self.branch_counts["repair"]["ai_calls"] = 1
        document = self.client.get(
            f"/marketplaces/api/drafts/{draft.id}/ai-suggestions"
        ).get_json()
        suggestion = document["suggestions"][0]
        declined = self._post(
            f"/marketplaces/api/drafts/{draft.id}/ai-suggestions/reject",
            {
                "suggestion_ids": [suggestion["id"]],
                "expected_version": document["version"],
                "review_token": document["review_token"],
                "request_key": "journey-repair-decline-000001",
            },
            csrf=document["csrf_token"],
        )
        self.assertEqual(declined.status_code, 200, declined.get_data(as_text=True))
        self.assertIsNone(declined.get_json()["review"]["version_after"])
        self.assertEqual(db.session.get(MarketplaceProductDraft, draft.id).version,
                         document["version"])

        draft = db.session.get(MarketplaceProductDraft, draft.id)
        attributes = json.loads(draft.attributes_json)
        attributes.append({
            "attribute_id": "909001",
            "complex_id": "0",
            "values": [{"value": "янтарный"}],
        })
        edited = MarketplaceDraftService.update_draft(
            seller_id=self.seller.id,
            draft_id=draft.id,
            expected_version=draft.version,
            patch={"attributes": attributes},
            corrected_by_user_id=self.user.id,
        )
        self.branch_counts["repair"]["human_repairs"] = 1
        validation = self._post(
            f"/marketplaces/drafts/{edited.id}/validate",
            {"expected_version": edited.version},
            csrf=document["csrf_token"],
        )
        self.assertEqual(validation.status_code, 200, validation.get_data(as_text=True))
        self.assertTrue(validation.get_json()["draft"]["validation"]["publishable"])

        reprepared = self._post("/marketplaces/ozon/uploads/", {
            "account_id": self.account.id,
            "imported_product_ids": [self.source.id],
            "confirm_prepare": True,
            "request_key": "journey-repair-reprepare-00001",
        })
        self.assertEqual(reprepared.status_code, 202, reprepared.get_data(as_text=True))
        second_uid = reprepared.get_json()["run"]["job_uid"]
        self.assertNotEqual(first_uid, second_uid)
        second_stats = OzonBulkUploadService.run_due_preparation()
        self.assertEqual(second_stats["prepared"], 1, second_stats)
        self.branch_counts["repair"]["repreparations"] = 1

    def test_unknown_write_is_quarantined_then_reconciled_without_retry(self):
        provider = SyntheticOzonProvider(ambiguous=True)
        operation = self.start(
            provider,
            key="journey-unknown-write-00001",
        )
        self.assertEqual(operation.status, "uncertain")
        self.assertEqual(operation.attempt_count, 1)
        self.assertEqual(provider.physical_writes, 1)

        same_request = self.start(
            provider,
            key="journey-unknown-write-00001",
        )
        self.assertEqual(same_request.id, operation.id)
        self.assertEqual(provider.physical_writes, 1)

        preview = ozon_write_quarantine.preview(
            seller_id=self.seller.id,
            origin_id=operation.id,
            viewer_user_id=self.user.id,
        )
        hold_id = ozon_write_quarantine.place(
            seller_id=self.seller.id,
            origin_id=operation.id,
            expected_version=operation.version,
            scope_token=preview["scope_token"],
            reason="Synthetic unknown write; stop new submissions",
            actor_user_id=self.user.id,
            confirm_scope=True,
        )
        self.assertGreater(hold_id, 0)
        held = ozon_write_quarantine.preview(
            seller_id=self.seller.id,
            origin_id=operation.id,
            viewer_user_id=self.user.id,
        )
        self.assertEqual(held["hold"]["status"], "active")

        reconciled = MarketplacePublicationService.poll_operation(
            seller_id=self.seller.id,
            operation_id=operation.id,
            adapter=provider,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertEqual(reconciled.status, "succeeded")
        self.assertEqual(reconciled.attempt_count, 1)
        self.assertEqual(provider.physical_writes, 1)
        self.assertEqual(len(provider.full_read_calls), 4)
        release_review = ozon_write_quarantine.preview(
            seller_id=self.seller.id,
            origin_id=operation.id,
            viewer_user_id=self.user.id,
        )
        self.assertTrue(release_review["can_release"])
        ozon_write_quarantine.update_decision(
            seller_id=self.seller.id,
            origin_id=operation.id,
            expected_version=release_review["hold"]["version"],
            expected_operation_version=reconciled.version,
            action="released",
            reason="Readback proves the original write outcome",
            actor_user_id=self.user.id,
            confirm_release=True,
        )
        final = ozon_write_quarantine.preview(
            seller_id=self.seller.id,
            origin_id=operation.id,
            viewer_user_id=self.user.id,
        )
        self.assertEqual(final["hold"]["status"], "released")
        self.assertEqual(final["outcome"], "succeeded")
        self.branch_counts["unknown"].update({
            "provider_writes": provider.physical_writes,
            "automatic_retries": provider.physical_writes - 1,
            "quarantines": 1,
            "safe_reconciliations": 1,
        })


if __name__ == "__main__":
    unittest.main()
