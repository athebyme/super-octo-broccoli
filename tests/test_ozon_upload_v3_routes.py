"""Two-step upload HTTP consent, request recovery and read-only fresh review."""
import json
import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import event
from werkzeug.datastructures import MultiDict

from models import BackgroundJob, MarketplaceOperation, OzonBulkUploadItem, OzonBulkUploadRun, db
from routes.ozon_bulk_uploads import register_ozon_bulk_upload_routes
from services.ozon_bulk_upload import OzonBulkUploadService
from services.ozon_upload_review import OzonUploadReviewService
from tests.test_marketplace_publications import OzonPublicationFixture


class OzonUploadV3RoutesTest(OzonPublicationFixture, unittest.TestCase):
    key = "v3-exact-request-0123456789abcdef"

    def setUp(self):
        super().setUp()
        self.app.config.update(SECRET_KEY="v3-upload-fixture", WTF_CSRF_ENABLED=False,
                               MARKETPLACE_OZON_ENABLED=True,
                               MARKETPLACE_OZON_PUBLICATION_ENABLED=True)
        self.account._credentials_encrypted = "synthetic-encrypted-value"
        db.session.commit()
        register_ozon_bulk_upload_routes(self.app)
        self.client = self.app.test_client()
        self.user_view = SimpleNamespace(id=self.user.id,
            seller=SimpleNamespace(id=self.seller.id), is_authenticated=True, is_active=True)
        self.auth = patch("routes.ozon_bulk_uploads.current_user", self.user_view)
        self.login = patch("flask_login.utils._get_user", return_value=self.user_view)
        self.auth.start()
        self.login.start()
        self.addCleanup(self.auth.stop)
        self.addCleanup(self.login.stop)

    def source_payload(self, **changes):
        value = {"account_id": self.account.id, "imported_product_ids": [self.source.id],
                 "confirm_prepare": True, "request_key": self.key}
        value.update(changes)
        return value

    def test_prepare_acceptance_is_fast_local_and_exact_replay(self):
        # No draft mutation/preparation is performed by the request itself.
        before_version = self.draft.version
        with patch("services.marketplace_drafts.MarketplaceDraftService.prepare_for_publication",
                   side_effect=AssertionError("HTTP must not prepare"), create=True):
            first = self.client.post("/marketplaces/ozon/uploads/", json=self.source_payload())
            second = self.client.post("/marketplaces/ozon/uploads/", json=self.source_payload())
        self.assertEqual(first.status_code, 202, first.get_json())
        self.assertEqual(second.status_code, 202, second.get_json())
        self.assertFalse(first.json["replayed"])
        self.assertTrue(second.json["replayed"])
        self.assertEqual(first.json["run"]["job_uid"], second.json["run"]["job_uid"])
        self.assertEqual(first.json["run"]["mode"], "source_prepare")
        self.assertIn(first.json["run"]["job_uid"], first.headers["Location"])
        self.assertEqual((BackgroundJob.query.count(), OzonBulkUploadRun.query.count(),
                          OzonBulkUploadItem.query.count(), MarketplaceOperation.query.count()), (1,1,1,0))
        self.assertEqual(self.draft.version, before_version)

    def test_local_prepare_works_without_publication_or_connected_key(self):
        self.app.config["MARKETPLACE_OZON_PUBLICATION_ENABLED"] = False
        self.account.connection_status = "unchecked"
        self.account._credentials_encrypted = None
        db.session.commit()
        response = self.client.post("/marketplaces/ozon/uploads/", json=self.source_payload())
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_old_source_write_contract_requires_new_review(self):
        data = self.source_payload()
        data.pop("confirm_prepare")
        data["confirm_write"] = True
        response = self.client.post("/marketplaces/ozon/uploads/", json=data)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "draft_review_required")
        self.assertEqual(BackgroundJob.query.count(), 0)

    def test_invalid_source_inputs_do_not_create_jobs(self):
        for changes in [
            {"imported_product_ids": []}, {"imported_product_ids": [True]},
            {"imported_product_ids": [self.source.id, self.source.id]},
            {"imported_product_ids": [2**64]}, {"request_key": "short"},
            {"confirm_prepare": "true"}, {"unrecognized": 1},
        ]:
            with self.subTest(changes=changes):
                result = self.client.post("/marketplaces/ozon/uploads/", json=self.source_payload(**changes))
                self.assertEqual(result.status_code, 400, result.get_json())
        self.assertEqual(BackgroundJob.query.count(), 0)

    def test_classic_prepare_303_and_duplicate_singleton_rejected(self):
        data = {"account_id": str(self.account.id), "imported_product_ids": [str(self.source.id)],
                "confirm_prepare": "1", "request_key": self.key}
        response = self.client.post("/marketplaces/ozon/uploads/", data=data)
        self.assertEqual(response.status_code, 303)
        duplicate = MultiDict(data)
        duplicate.add("account_id", str(self.account.id))
        result = self.client.post("/marketplaces/ozon/uploads/", data=duplicate,
                                  headers={"Accept": "application/json"})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(BackgroundJob.query.count(), 1)

    def test_json_duplicate_fields_and_scope_in_query_are_rejected(self):
        raw = json.dumps(self.source_payload())[:-1] + ', "confirm_prepare": true}'
        response = self.client.post("/marketplaces/ozon/uploads/", data=raw,
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/marketplaces/ozon/uploads/?account_id=99",
                                    json=self.source_payload())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(BackgroundJob.query.count(), 0)

    def test_recovery_is_read_only_exact_seller_and_account(self):
        first = self.client.post("/marketplaces/ozon/uploads/", json=self.source_payload())
        self.assertEqual(first.status_code, 202, first.get_json())
        url = f"/marketplaces/ozon/uploads/api/by-request?account_id={self.account.id}"
        headers = {"X-Upload-Request-Key": self.key}
        with patch.object(OzonBulkUploadService, "reconcile_run", side_effect=AssertionError("GET recovery mutation")):
            found = self.client.get(url, headers=headers)
        self.assertEqual(found.status_code, 200, found.get_json())
        self.assertEqual(found.json["run"]["job_uid"], first.json["run"]["job_uid"])
        self.assertIn("no-store", found.headers["Cache-Control"])
        self.user_view.seller.id = self.foreign_seller.id
        foreign = self.client.get(url, headers=headers)
        self.assertEqual(foreign.status_code, 404)
        self.assertTrue(foreign.json["csrf_token"])
        self.assertIn("no-store", foreign.headers["Cache-Control"])
        self.assertEqual((BackgroundJob.query.count(), MarketplaceOperation.query.count()), (1,0))

    def test_from_drafts_never_accepts_missing_or_extra_versions(self):
        for versions in [None, {}, {str(self.draft.id): self.draft.version, "9999": 1}]:
            with self.subTest(versions=versions):
                response = self.client.post("/marketplaces/ozon/uploads/from-drafts", json={
                    "account_id": self.account.id, "draft_ids": [self.draft.id],
                    "confirm_write": True, "request_key": self.key, "expected_versions": versions,
                })
                self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual((BackgroundJob.query.count(), MarketplaceOperation.query.count()), (0,0))

    def test_classic_review_posts_only_checked_versions(self):
        acceptance = SimpleNamespace(job=SimpleNamespace(job_uid="ozon-upload-"+"a"*32), replayed=False)
        with patch.object(OzonBulkUploadService, "accept_reviewed_publish", return_value=acceptance) as accept, \
             patch.object(OzonBulkUploadService, "public_document", return_value={"mode": "reviewed_drafts"}):
            response = self.client.post("/marketplaces/ozon/uploads/from-drafts", data={
                "account_id": str(self.account.id), "draft_ids": [str(self.draft.id)],
                "confirm_write": "1", "request_key": self.key,
                "expected_versions": json.dumps({str(self.draft.id): self.draft.version, "9999": 3}),
            })
        self.assertEqual(response.status_code, 303)
        self.assertEqual(accept.call_args.kwargs["expected_versions"], {str(self.draft.id): self.draft.version})

    def test_review_rebuilds_validation_without_persisting_stored_ready(self):
        self.draft.status = "ready"
        self.draft.validation_status = "valid"
        self.draft.validation_result_json = json.dumps({"publishable": True})
        # A changed source must block even though stored status still says ready.
        self.draft.source_fact_hash = "stale"
        db.session.commit()
        version = self.draft.version
        writes = []
        def capture(_conn, _cursor, statement, _params, _context, _many):
            if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                writes.append(statement)
        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            document = OzonUploadReviewService.document(seller_id=self.seller.id,
                account_id=self.account.id, draft_ids=[self.draft.id])
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)
        row = document["items"][0]
        self.assertFalse(row["selectable"])
        self.assertIn("source_facts_stale", {error["code"] for error in row["errors"]})
        self.assertEqual(self.draft.version, version)
        self.assertEqual(self.draft.validation_status, "valid")
        self.assertEqual(writes, [])

    def test_review_rejects_mixed_foreign_selection_and_repeated_query(self):
        result = self.client.get(f"/marketplaces/ozon/uploads/api/review?account_id={self.account.id}"
                                f"&draft_ids={self.draft.id},9223372036854775807")
        self.assertEqual(result.status_code, 404)
        result = self.client.get(f"/marketplaces/ozon/uploads/api/review?account_id={self.account.id}"
                                f"&draft_ids={self.draft.id}&account_id={self.account.id}")
        self.assertEqual(result.status_code, 400)

    def test_generic_job_gets_do_not_expire_durable_reference_wait(self):
        from flask import jsonify
        first = self.client.post("/marketplaces/ozon/uploads/", json=self.source_payload())
        job = BackgroundJob.query.filter_by(job_uid=first.json["run"]["job_uid"]).one()
        job.updated_at = datetime.utcnow() - timedelta(hours=2)
        job.status = "running"
        db.session.commit()
        # Execute only these handlers, never the application's startup side effects.
        source = Path(__file__).resolve().parents[1] / "seller_platform.py"
        tree = ast.parse(source.read_text())
        functions = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {"api_job_status", "api_jobs_active"}:
                node.decorator_list = []
                functions.append(node)
        self.assertEqual(len(functions), 2)
        namespace = {"current_user": self.user_view, "BackgroundJob": BackgroundJob,
                     "db": db, "jsonify": jsonify}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        for durable_type in ("ozon_bulk_upload", "ozon_draft_completion"):
            with self.subTest(job_type=durable_type):
                job.job_type = durable_type
                db.session.commit()
                with self.app.test_request_context():
                    status = namespace["api_job_status"](job.job_uid).get_json()
                    active = namespace["api_jobs_active"]().get_json()
                self.assertEqual(status["status"], "running")
                self.assertIn(job.job_uid, [row["job_uid"] for row in active["jobs"]])
                db.session.refresh(job)
                self.assertEqual(job.status, "running")
                self.assertIsNone(job.error_message)
