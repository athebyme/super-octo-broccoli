"""Two-step upload HTTP consent, request recovery and read-only fresh review."""
import json
import ast
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from sqlalchemy import event
from werkzeug.datastructures import MultiDict

from models import BackgroundJob, ImportedProduct, MarketplaceOperation, OzonBulkUploadItem, OzonBulkUploadRun, db
from routes.marketplace_drafts import register_marketplace_draft_routes
from routes.ozon_bulk_uploads import register_ozon_bulk_upload_routes
from services.ozon_bulk_repair import OzonBulkRepairService
from services.ozon_bulk_upload import (
    OzonBulkUploadNotFound,
    OzonBulkUploadService,
)
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
        register_marketplace_draft_routes(self.app)
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

    def test_source_prepare_detail_exposes_exact_workspace_binding_and_plain_fallback(self):
        template_root = Path(__file__).resolve().parents[1] / "templates"
        self.app.jinja_env.loader = ChoiceLoader([
            DictLoader({"base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}"}),
            FileSystemLoader(str(template_root)),
        ])
        self.app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"
        self.app.context_processor(lambda: {"current_user": self.user_view})
        if "seller_my_products_beta" not in self.app.view_functions:
            self.app.add_url_rule(
                "/my-products/beta", endpoint="seller_my_products_beta",
                view_func=lambda: "",
            )
        if "marketplace_listings.index" not in self.app.view_functions:
            self.app.add_url_rule(
                "/marketplaces/listings/", endpoint="marketplace_listings.index",
                view_func=lambda: "",
            )
        if "supplier_catalog" not in self.app.view_functions:
            self.app.add_url_rule(
                "/supplier/catalog", endpoint="supplier_catalog",
                view_func=lambda: "",
            )
        second = ImportedProduct(
            seller_id=self.seller.id, external_id="return-source-2",
            source_type="synthetic", title="Второй товар возврата",
        )
        db.session.add(second)
        db.session.flush()
        ordered_product_ids = [second.id, self.source.id]
        accepted = self.client.post(
            "/marketplaces/ozon/uploads/",
            json=self.source_payload(imported_product_ids=ordered_product_ids),
        )
        self.assertEqual(accepted.status_code, 202, accepted.get_json())
        self.assertEqual(accepted.json["run"]["mode"], "source_prepare")
        job_uid = accepted.json["run"]["job_uid"]
        before = (
            BackgroundJob.query.count(), OzonBulkUploadRun.query.count(),
            OzonBulkUploadItem.query.count(), MarketplaceOperation.query.count(),
        )

        response = self.client.get(
            f"/marketplaces/ozon/uploads/{job_uid}",
            headers={"Accept": "text/html"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        html = response.get_data(as_text=True)
        self.assertIn("Подготавливаем черновики", html)
        match = re.search(
            r'<script type="application/json" id="ozon-prepare-workspace-return">(.*?)</script>',
            html, re.DOTALL,
        )
        self.assertIsNotNone(match, "source_prepare must expose its minimal return binding")
        payload = json.loads(match.group(1))
        self.assertEqual(set(payload), {"workspaceUrl", "owner", "accountId", "productIds", "jobUid"})
        self.assertEqual(payload, {
            "workspaceUrl": "/my-products/beta",
            "owner": f"{self.user.id}:{self.seller.id}",
            "accountId": self.account.id,
            "productIds": ordered_product_ids,
            "jobUid": job_uid,
        })
        self.assertRegex(
            html,
            r'<a class="text-xs underline underline-offset-2" data-source-prepare-products\s+'
            r'style="[^"]*"\s+href="/my-products/beta">Мои товары</a>',
        )
        self.assertIn("common-product-content.js?v=20261122-ozon-prepare-return-r48", html)
        self.assertIn("ozon-preparation-workspace-return.js?v=20261122-ozon-prepare-return-r48", html)
        self.assertEqual(before, (
            BackgroundJob.query.count(), OzonBulkUploadRun.query.count(),
            OzonBulkUploadItem.query.count(), MarketplaceOperation.query.count(),
        ), "opening the result must remain a read-only return-link enhancement")

    def test_preparation_workspace_return_script_only_enhances_exact_local_href(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the workspace-return UI contract check")
        script_path = Path(__file__).resolve().parents[1] / "static" / "ozon-preparation-workspace-return.js"
        completed = subprocess.run(
            [node, "-e", r'''
const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const payload={workspaceUrl:'/my-products/beta',owner:'12:34',accountId:56,productIds:[3,2,9],jobUid:'ozon-upload-'+'a'.repeat(32)};
function exercise(result,json=JSON.stringify(payload)){
  const link={href:'/my-products/beta',dataset:{},attributes:{},setAttribute(k,v){this.attributes[k]=v}};
  const node={textContent:json};let calls=0;
  const helpers={ozonPreparationWorkspaceReturnHref(...args){calls++;assert.equal(args.length,1);assert.equal(JSON.stringify(args[0]),JSON.stringify(payload));return result}};
  const context={window:{location:{origin:'https://seller.test'},SellerHubCommonContent:helpers},
    document:{getElementById:id=>id==='ozon-prepare-workspace-return'?node:null,
      querySelector:selector=>selector==='a[data-source-prepare-products]'?link:null},URL,Array,JSON,RegExp};
  context.window.fetch=()=>{throw Error('Return enhancement must not call the network')};
  vm.runInNewContext(source,context,{filename:'ozon-preparation-workspace-return.js'});
  return {link,calls};
}
const nonce='b'.repeat(32);
const valid=exercise('/my-products/beta?import_batch=signed-token#workspace-return='+nonce);
assert.equal(valid.link.href,'/my-products/beta?import_batch=signed-token#workspace-return='+nonce);
assert.equal(valid.link.dataset.workspaceReturn,'verified');
assert.match(valid.link.attributes['aria-label'],/прежним списком/);assert.equal(valid.calls,1);
for(const invalid of ['', '//evil.test/my-products/beta#workspace-return='+nonce,
  'https://evil.test/my-products/beta#workspace-return='+nonce,
  '/my-products/beta?other=1#workspace-return='+nonce,
  '/my-products/beta?import_batch=signed-token#other']){
  const result=exercise(invalid);assert.equal(result.link.href,'/my-products/beta');assert.equal(result.link.dataset.workspaceReturn,undefined);
}
const malformed=exercise('/my-products/beta?import_batch=signed-token#workspace-return='+nonce,'{');
assert.equal(malformed.link.href,'/my-products/beta');assert.equal(malformed.calls,0);
''',
             str(script_path)],
            cwd=script_path.parents[1], text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

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

    def _reviewed_local_result(self, key="o" * 24, phase="needs_input"):
        acceptance = OzonBulkUploadService.accept_reviewed_publish(
            seller_id=self.seller.id, account_id=self.account.id,
            draft_ids=[self.draft.id],
            expected_versions={str(self.draft.id): self.draft.version},
            request_key=key, created_by_user_id=self.user.id,
        )
        run = OzonBulkUploadRun.query.filter_by(job_id=acceptance.job.id).one()
        item = OzonBulkUploadItem.query.filter_by(run_id=run.id).one()
        item.phase = phase
        db.session.commit()
        return acceptance.job, run, item

    def _selection_editor_document(self):
        return {
            "mode": "draft_selection",
            "account_id": self.account.id,
            "account_label": self.account.label,
            "groups": [{
                "id": "group-1",
                "source_category": "Категория",
                "rows": [{
                    "draft_id": self.draft.id,
                    "imported_product_id": self.source.id,
                    "cleanup_candidates": [],
                    "attributes": [],
                }],
            }],
            "bulk_fields": [],
            "total": 1,
            "review_draft_ids": [self.draft.id],
            "blocked_review_items": [],
            "scope_key": "a" * 64,
        }

    def _selection_query(self, *, account_id=None, draft_ids=None, suffix=""):
        account_id = account_id or self.account.id
        draft_ids = draft_ids if draft_ids is not None else [self.draft.id]
        raw_ids = ",".join(str(value) for value in draft_ids)
        return (
            f"account_id={account_id}&draft_ids={raw_ids}"
            + (f"&{suffix}" if suffix else "")
        )

    def _selection_form(self, *, confirmed=True):
        form = MultiDict()
        if confirmed:
            form.add("confirm_local", "1")
        draft_id = self.draft.id
        prefix = f"row_{draft_id}_"
        form.add("selected_draft_id", str(draft_id))
        form.add(prefix + "draft_version", str(self.draft.version))
        form.add(prefix + "imported_product_id", str(self.source.id))
        form.add(prefix + "action", "ИСПРАВИТЬ")
        form.add(prefix + "product_type_id", str(self.product_type.id))
        form.add(prefix + "save_mapping", "0")
        for field in (
            "price_rub", "package_width_mm", "package_height_mm",
            "package_depth_mm", "package_weight_g", "description",
        ):
            form.add(prefix + field, "")
        return form

    def test_selected_repair_html_uses_canonical_scope_and_safe_return_urls(self):
        selection = {
            "account_id": self.account.id,
            "draft_ids": [self.draft.id],
        }
        query = self._selection_query(suffix=(
            f"return_page=2&return_per_page=1&return_status=draft"
            f"&return_account_id={self.account.id}&q=coat&group=group-1"
            "&state=needs_input&page=3"
        ))
        editor = self._selection_editor_document()
        with patch.object(
            OzonBulkRepairService, "editor_document", return_value=editor,
        ) as read, patch(
            "routes.ozon_bulk_uploads.render_template", return_value="selection html",
        ) as render:
            response = self.client.get(
                f"/marketplaces/ozon/uploads/selection/repair?{query}",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        read.assert_called_once_with(seller_id=self.seller.id, selection=selection)
        self.assertEqual(render.call_args.args[0], "ozon_bulk_repair.html")
        context = render.call_args.kwargs
        self.assertTrue(context["selection_mode"])
        self.assertEqual(context["editor_view"], {
            "q": "coat", "group": "group-1", "state": "needs_input", "page": 3,
        })
        urls = context["editor_urls"]
        scope_query = f"?account_id={self.account.id}&draft_ids={self.draft.id}"
        self.assertEqual(
            urls["read"],
            "/marketplaces/ozon/uploads/selection/repair" + scope_query,
        )
        self.assertEqual(
            urls["apply"],
            "/marketplaces/ozon/uploads/selection/repair/apply" + scope_query,
        )
        self.assertEqual(
            urls["types"],
            "/marketplaces/ozon/uploads/selection/repair/types" + scope_query,
        )
        self.assertEqual(
            urls["dictionaries"],
            "/marketplaces/ozon/uploads/selection/repair/dictionaries/",
        )
        self.assertEqual(urls["dictionary_query"], scope_query)
        self.assertEqual(urls["drafts"], "/marketplaces/drafts/")
        self.assertNotIn("catalog_return", urls)
        self.assertEqual(urlsplit(urls["result"]).path, "/marketplaces/drafts/")
        self.assertEqual(parse_qs(urlsplit(urls["result"]).query), {
            "account_id": [str(self.account.id)],
            "status": ["draft"],
            "page": ["2"],
            "per_page": ["1"],
        })
        self.assertEqual(urlsplit(urls["classic"]).path, "/marketplaces/drafts/classic")
        review = urlsplit(urls["review"])
        self.assertEqual(review.path, "/marketplaces/ozon/uploads/review")
        self.assertEqual(parse_qs(review.query), {
            "account_id": [str(self.account.id)],
            "draft_ids": [str(self.draft.id)],
        })

    def test_selected_repair_review_url_is_safe_base_when_all_rows_are_held(self):
        editor = self._selection_editor_document()
        editor["review_draft_ids"] = []
        editor["blocked_review_items"] = [{
            "draft_id": self.draft.id,
            "title": "Безопасный товар",
            "message": "Операция ещё выполняется",
        }]
        with patch.object(
            OzonBulkRepairService, "editor_document", return_value=editor,
        ), patch(
            "routes.ozon_bulk_uploads.render_template", return_value="selection html",
        ) as render:
            response = self.client.get(
                "/marketplaces/ozon/uploads/selection/repair?"
                + self._selection_query(),
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 200)
        review = urlsplit(render.call_args.kwargs["editor_urls"]["review"])
        self.assertEqual(review.path, "/marketplaces/ozon/uploads/review")
        self.assertEqual(parse_qs(review.query), {
            "account_id": [str(self.account.id)],
        })

    def test_selected_repair_preserves_explicit_all_stores_and_direct_account_fallback(self):
        editor = self._selection_editor_document()
        with patch.object(
            OzonBulkRepairService, "editor_document", return_value=editor,
        ), patch(
            "routes.ozon_bulk_uploads.render_template", return_value="selection html",
        ) as render:
            explicit_all_stores = self.client.get(
                "/marketplaces/ozon/uploads/selection/repair?"
                + self._selection_query(suffix=(
                    "return_page=1&return_per_page=20&return_status=&return_account_id="
                )),
                headers={"Accept": "text/html"},
            )
            direct_link = self.client.get(
                "/marketplaces/ozon/uploads/selection/repair?"
                + self._selection_query(),
                headers={"Accept": "text/html"},
            )
        self.assertEqual(explicit_all_stores.status_code, 200)
        self.assertEqual(direct_link.status_code, 200)
        explicit_filters = parse_qs(urlsplit(
            render.call_args_list[0].kwargs["editor_urls"]["result"]
        ).query)
        self.assertNotIn("account_id", explicit_filters)
        self.assertNotIn("status", explicit_filters)
        self.assertEqual(explicit_filters["page"], ["1"])
        self.assertEqual(explicit_filters["per_page"], ["20"])
        direct_filters = parse_qs(urlsplit(
            render.call_args_list[1].kwargs["editor_urls"]["result"]
        ).query)
        self.assertEqual(direct_filters, {"account_id": [str(self.account.id)]})

    def test_selected_repair_json_is_csrf_bearing_and_uncacheable(self):
        editor = self._selection_editor_document()
        query = self._selection_query()
        with patch.object(OzonBulkRepairService, "editor_document", return_value=editor) as read:
            response = self.client.get(
                f"/marketplaces/ozon/uploads/selection/repair?{query}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.json["editor"]["mode"], "draft_selection")
        self.assertTrue(response.json["csrf"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        read.assert_called_once_with(
            seller_id=self.seller.id,
            selection={"account_id": self.account.id, "draft_ids": [self.draft.id]},
        )

    def test_selected_repair_preserves_order_in_service_scope_and_review_url(self):
        draft_ids = [self.draft.id + 1, self.draft.id]
        editor = self._selection_editor_document()
        editor["review_draft_ids"] = draft_ids.copy()
        selection = {"account_id": self.account.id, "draft_ids": draft_ids}
        query = self._selection_query(draft_ids=draft_ids)
        with patch.object(
            OzonBulkRepairService, "editor_document", return_value=editor,
        ) as read, patch(
            "routes.ozon_bulk_uploads.render_template", return_value="selection html",
        ) as render:
            response = self.client.get(
                f"/marketplaces/ozon/uploads/selection/repair?{query}",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 200)
        read.assert_called_once_with(seller_id=self.seller.id, selection=selection)
        review = urlsplit(render.call_args.kwargs["editor_urls"]["review"])
        self.assertEqual(
            parse_qs(review.query)["draft_ids"],
            [f"{draft_ids[0]},{draft_ids[1]}"],
        )

    def test_selected_repair_accepts_only_exact_ozon_catalog_return_context(self):
        editor = self._selection_editor_document()
        catalog_return = (
            "/marketplaces/listings/beta?marketplace=ozon"
            f"&account_id={self.account.id}&search=coat&page=2"
        )
        query = self._selection_query(suffix=urlencode({"return_to": catalog_return}))
        with patch.object(
            OzonBulkRepairService, "editor_document", return_value=editor,
        ) as read, patch(
            "routes.ozon_bulk_uploads.render_template", return_value="selection html",
        ) as render:
            response = self.client.get(
                f"/marketplaces/ozon/uploads/selection/repair?{query}",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            render.call_args.kwargs["editor_urls"]["catalog_return"],
            catalog_return,
        )
        self.assertEqual(
            render.call_args.kwargs["editor_urls"]["catalog_return_label"],
            "К каталогу Ozon",
        )
        read.assert_called_once_with(
            seller_id=self.seller.id,
            selection={"account_id": self.account.id, "draft_ids": [self.draft.id]},
        )

    def test_selected_repair_rejects_foreign_or_arbitrary_catalog_return(self):
        editor = self._selection_editor_document()
        invalid_returns = [
            "https://example.invalid/marketplaces/listings/?marketplace=ozon"
            f"&account_id={self.account.id}",
            "/marketplaces/listings/?marketplace=ozon&account_id=999",
            "/marketplaces/listings/?marketplace=wb"
            f"&account_id={self.account.id}",
            "/marketplaces/listings/?marketplace=ozon"
            f"&account_id={self.account.id}&next=https://example.invalid",
            "/marketplaces/listings/?marketplace=ozon"
            f"&account_id={self.account.id}&account_id={self.account.id}",
        ]
        with patch.object(
            OzonBulkRepairService,
            "editor_document",
            side_effect=AssertionError("unsafe return must stop before service"),
        ) as read:
            for return_to in invalid_returns:
                with self.subTest(return_to=return_to):
                    query = self._selection_query(suffix=urlencode({"return_to": return_to}))
                    response = self.client.get(
                        f"/marketplaces/ozon/uploads/selection/repair?{query}",
                        headers={"Accept": "text/html"},
                    )
                    self.assertEqual(response.status_code, 302)
                    self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        read.assert_not_called()

    def test_selected_repair_json_does_not_accept_catalog_return_query(self):
        with patch.object(
            OzonBulkRepairService,
            "editor_document",
            side_effect=AssertionError("JSON selection has no catalog return context"),
        ) as read:
            response = self.client.get(
                "/marketplaces/ozon/uploads/selection/repair?"
                + self._selection_query(suffix=urlencode({
                    "return_to": "/marketplaces/listings/?marketplace=ozon"
                    f"&account_id={self.account.id}",
                })),
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json["csrf"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        read.assert_not_called()

    def test_selected_repair_rejects_ambiguous_or_untyped_scope_before_service(self):
        base = "/marketplaces/ozon/uploads/selection/repair?"
        valid = self._selection_query()
        invalid_queries = [
            f"account_id={self.account.id}",
            valid + f"&account_id={self.account.id}",
            valid + f"&draft_ids={self.draft.id}",
            f"account_id=0&draft_ids={self.draft.id}",
            f"account_id={self.account.id}&draft_ids=",
            f"account_id={self.account.id}&draft_ids={self.draft.id},0",
            f"account_id={self.account.id}&draft_ids={self.draft.id},{self.draft.id}",
            f"account_id={self.account.id}&draft_ids=0{self.draft.id}",
            f"account_id={'9' * 4_300}&draft_ids={self.draft.id}",
            f"account_id={self.account.id}&draft_ids={'9' * 4_300}",
            valid + "&next=https://example.invalid",
            valid + "&return_page=0",
            valid + "&return_per_page=101",
            valid + "&return_status=unknown",
        ]
        too_many = ",".join(str(value) for value in range(1, 202))
        invalid_queries.append(f"account_id={self.account.id}&draft_ids={too_many}")
        with patch.object(
            OzonBulkRepairService,
            "editor_document",
            side_effect=AssertionError("invalid selection must not reach service"),
        ) as read:
            for query in invalid_queries:
                with self.subTest(query=query[:100]):
                    response = self.client.get(
                        base + query,
                        headers={"Accept": "application/json"},
                    )
                    self.assertEqual(response.status_code, 400, response.get_json())
                    self.assertTrue(response.json["csrf"])
                    self.assertIn("no-store", response.headers["Cache-Control"])
        read.assert_not_called()

    def test_selected_repair_rejects_foreign_scope_through_seller_service(self):
        query = self._selection_query(account_id=self.account.id, draft_ids=[self.draft.id])
        with patch.object(
            OzonBulkRepairService,
            "editor_document",
            side_effect=OzonBulkUploadNotFound("Черновик не найден"),
        ) as read:
            response = self.client.get(
                f"/marketplaces/ozon/uploads/selection/repair?{query}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["code"], "ozon_bulk_upload_not_found")
        self.assertIn("no-store", response.headers["Cache-Control"])
        read.assert_called_once_with(
            seller_id=self.seller.id,
            selection={"account_id": self.account.id, "draft_ids": [self.draft.id]},
        )

    def test_selected_repair_type_and_dictionary_searches_keep_exact_scope(self):
        scope = {"account_id": self.account.id, "draft_ids": [self.draft.id]}
        query = self._selection_query()
        with patch.object(
            OzonBulkRepairService, "search_product_types", return_value=[{"id": 7}],
        ) as type_search, patch.object(
            OzonBulkRepairService,
            "search_dictionary_values",
            return_value={"items": [{"id": "1", "value": "Хлопок"}], "truncated": False},
        ) as dictionary_search:
            type_response = self.client.get(
                f"/marketplaces/ozon/uploads/selection/repair/types?{query}&q=shirt",
                headers={"Accept": "application/json"},
            )
            dictionary_response = self.client.get(
                f"/marketplaces/ozon/uploads/selection/repair/dictionaries/{self.draft.id}/4191?"
                f"{query}&q=cotton",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(type_response.status_code, 200, type_response.get_json())
        self.assertEqual(type_response.json["items"], [{"id": 7}])
        self.assertTrue(type_response.json["csrf"])
        self.assertIn("no-store", type_response.headers["Cache-Control"])
        type_search.assert_called_once_with(
            seller_id=self.seller.id,
            selection=scope,
            query="shirt",
        )
        self.assertEqual(dictionary_response.status_code, 200, dictionary_response.get_json())
        self.assertEqual(dictionary_response.json["items"], [{"id": "1", "value": "Хлопок"}])
        self.assertTrue(dictionary_response.json["csrf"])
        self.assertIn("no-store", dictionary_response.headers["Cache-Control"])
        dictionary_search.assert_called_once_with(
            seller_id=self.seller.id,
            selection=scope,
            draft_id=self.draft.id,
            external_attribute_id="4191",
            query="cotton",
        )

    def test_selected_repair_search_rejects_repeated_and_unknown_query_fields(self):
        scope = self._selection_query()
        with patch.object(
            OzonBulkRepairService,
            "search_product_types",
            side_effect=AssertionError("invalid search must not reach service"),
        ) as search:
            for suffix in ("q=a&q=b", "q=a&job_uid=ozon-upload-abc"):
                with self.subTest(suffix=suffix):
                    response = self.client.get(
                        f"/marketplaces/ozon/uploads/selection/repair/types?{scope}&{suffix}",
                        headers={"Accept": "application/json"},
                    )
                    self.assertEqual(response.status_code, 400, response.get_json())
                    self.assertTrue(response.json["csrf"])
                    self.assertIn("no-store", response.headers["Cache-Control"])
        search.assert_not_called()

    def test_selected_repair_apply_requires_local_confirmation_and_only_calls_local_service(self):
        editor = self._selection_editor_document()
        scope = {"account_id": self.account.id, "draft_ids": [self.draft.id]}
        report = {
            "mode": "draft_selection",
            "scope_key": "a" * 64,
            "total": 1, "updated": 1, "ready_to_retry": 0,
            "needs_input": 1, "excluded": 0, "failed": 0,
        }
        url = "/marketplaces/ozon/uploads/selection/repair/apply?" + self._selection_query()
        with patch.object(
            OzonBulkRepairService, "editor_document", return_value=editor,
        ) as read, patch.object(
            OzonBulkRepairService, "apply_editor_rows", return_value=report,
        ) as apply, patch.object(
            OzonBulkUploadService,
            "accept_reviewed_publish",
            side_effect=AssertionError("local repair must not queue an upload"),
        ) as publish, patch.object(
            OzonBulkUploadService,
            "accept_source_prepare",
            side_effect=AssertionError("local repair must not queue preparation"),
        ) as prepare:
            response = self.client.post(
                url,
                data=self._selection_form(),
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.json["report"], report)
        self.assertEqual(response.json["mode"], "draft_selection")
        self.assertEqual(response.json["scope_key"], "a" * 64)
        self.assertTrue(response.json["csrf"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        read.assert_called_once_with(seller_id=self.seller.id, selection=scope)
        apply_kwargs = apply.call_args.kwargs
        self.assertEqual(apply_kwargs["seller_id"], self.seller.id)
        self.assertEqual(apply_kwargs["selection"], scope)
        self.assertEqual(apply_kwargs["corrected_by_user_id"], self.user.id)
        self.assertEqual(len(apply_kwargs["rows"]), 1)
        self.assertEqual(apply_kwargs["rows"][0]["draft_id"], self.draft.id)
        self.assertEqual(apply_kwargs["rows"][0]["expected_version"], self.draft.version)
        publish.assert_not_called()
        prepare.assert_not_called()

    def test_selected_repair_apply_rejects_json_unknown_repeated_and_unconfirmed_input(self):
        editor = self._selection_editor_document()
        url = "/marketplaces/ozon/uploads/selection/repair/apply?" + self._selection_query()
        with patch.object(
            OzonBulkRepairService, "editor_document", return_value=editor,
        ) as read, patch.object(
            OzonBulkRepairService,
            "apply_editor_rows",
            side_effect=AssertionError("invalid local form must not be applied"),
        ) as apply:
            responses = [
                self.client.post(url, json={"confirm_local": True, "selected_draft_id": [self.draft.id]}),
                self.client.post(url, data=self._selection_form(confirmed=False),
                                 headers={"Accept": "application/json"}),
            ]
            repeated = self._selection_form()
            repeated.add("confirm_local", "1")
            responses.append(self.client.post(url, data=repeated,
                                              headers={"Accept": "application/json"}))
            unknown = self._selection_form()
            unknown.add("extra", "x")
            responses.append(self.client.post(url, data=unknown,
                                              headers={"Accept": "application/json"}))
        for response in responses:
            self.assertEqual(response.status_code, 400, response.get_json())
            self.assertTrue(response.json["csrf"])
            self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(read.call_count, 3)  # JSON is rejected before reading the editor.
        apply.assert_not_called()

    def test_selected_repair_apply_feature_guard_blocks_before_service(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        url = "/marketplaces/ozon/uploads/selection/repair/apply?" + self._selection_query()
        with patch.object(
            OzonBulkRepairService,
            "editor_document",
            side_effect=AssertionError("disabled repair must stop before local service"),
        ) as read, patch.object(
            OzonBulkRepairService,
            "apply_editor_rows",
            side_effect=AssertionError("disabled repair must not apply"),
        ) as apply:
            response = self.client.post(
                url,
                data=self._selection_form(),
                headers={"Accept": "application/json"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "ozon_feature_disabled")
        self.assertIn("no-store", response.headers["Cache-Control"])
        read.assert_not_called()
        apply.assert_not_called()

    def test_reviewed_result_recovery_scope_is_read_only_and_replayable(self):
        job, run, item = self._reviewed_local_result()
        # Queue state can lag the item projection; only observed item/operation
        # state decides whether the whole result is settled.
        self.assertEqual(run.state, "active")
        with patch.object(
            OzonBulkUploadService, "reconcile_run",
            side_effect=AssertionError("V3 result GET must not reconcile"),
        ):
            result = self.client.get(
                f"/marketplaces/ozon/uploads/{job.job_uid}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(result.json["run"]["summary"]["active"], 0)
        self.assertEqual(result.json["run"]["review_recovery_draft_ids"], [self.draft.id])
        self.assertIn("no-store", result.headers["Cache-Control"])
        with patch("routes.ozon_bulk_uploads.render_template", return_value="result html"):
            html_result = self.client.get(
                f"/marketplaces/ozon/uploads/{job.job_uid}",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(html_result.status_code, 200)
        self.assertIn("no-store", html_result.headers["Cache-Control"])
        db.session.refresh(job)
        self.assertEqual(job.status, "running")  # read did not persist the projection

        review_url = (
            f"/marketplaces/ozon/uploads/api/review?account_id={self.account.id}"
            f"&draft_ids={self.draft.id}&result_job_uid={job.job_uid}"
        )
        review = self.client.get(review_url)
        self.assertEqual(review.status_code, 200, review.get_json())
        self.assertEqual(review.json["review"]["result_job_uid"], job.job_uid)
        self.assertEqual(review.json["review"]["draft_ids"], [self.draft.id])

        payload = {
            "account_id": self.account.id,
            "draft_ids": [self.draft.id],
            "expected_versions": {str(self.draft.id): self.draft.version},
            "confirm_write": True,
            "request_key": self.key,
            "result_job_uid": job.job_uid,
        }
        accepted = self.client.post(
            "/marketplaces/ozon/uploads/from-drafts", json=payload,
        )
        self.assertEqual(accepted.status_code, 202, accepted.get_json())
        fingerprint = OzonBulkUploadRun.query.filter_by(
            job_id=BackgroundJob.query.filter_by(
                job_uid=accepted.json["run"]["job_uid"],
            ).one().id,
        ).one().request_fingerprint
        self.assertNotEqual(fingerprint, run.request_fingerprint)

        # A newly accepted active result suppresses a second new request from
        # the stale source, while the original idempotency key still replays.
        replay = self.client.post(
            "/marketplaces/ozon/uploads/from-drafts", json=payload,
        )
        self.assertEqual(replay.status_code, 202, replay.get_json())
        self.assertTrue(replay.json["replayed"])
        payload["request_key"] = "z" * 24
        stale = self.client.post(
            "/marketplaces/ozon/uploads/from-drafts", json=payload,
        )
        self.assertEqual(stale.status_code, 404, stale.get_json())

    def test_reviewed_result_recovery_waits_for_observed_items_not_run_state(self):
        job, run, item = self._reviewed_local_result(phase="reviewed")
        run.state = "completed"
        run.next_due_at = None
        db.session.commit()
        document = OzonBulkUploadService.public_document(job, detail=True)
        self.assertEqual(document["summary"]["active"], 1)
        self.assertEqual(document["review_recovery_draft_ids"], [])
        result = self.client.get(
            f"/marketplaces/ozon/uploads/api/review?account_id={self.account.id}"
            f"&draft_ids={self.draft.id}&result_job_uid={job.job_uid}"
        )
        self.assertEqual(result.status_code, 409, result.get_json())
        self.assertEqual(result.json["code"], "upload_result_not_settled")

    def test_result_job_uid_rejects_empty_and_mutually_exclusive_origins(self):
        job, _run, _item = self._reviewed_local_result()
        base = f"account_id={self.account.id}&draft_ids={self.draft.id}"
        empty_get = self.client.get(
            f"/marketplaces/ozon/uploads/api/review?{base}&result_job_uid="
        )
        self.assertEqual(empty_get.status_code, 400)
        conflict_get = self.client.get(
            f"/marketplaces/ozon/uploads/api/review?{base}"
            f"&result_job_uid={job.job_uid}&parent_prepare_job_uid={job.job_uid}"
        )
        self.assertEqual(conflict_get.status_code, 400)
        post = {
            "account_id": self.account.id, "draft_ids": [self.draft.id],
            "expected_versions": {str(self.draft.id): self.draft.version},
            "confirm_write": True, "request_key": self.key,
            "result_job_uid": "", "parent_prepare_job_uid": job.job_uid,
        }
        empty_post = self.client.post(
            "/marketplaces/ozon/uploads/from-drafts", json=post,
        )
        self.assertEqual(empty_post.status_code, 400)
        post["result_job_uid"] = job.job_uid
        conflict_post = self.client.post(
            "/marketplaces/ozon/uploads/from-drafts", json=post,
        )
        self.assertEqual(conflict_post.status_code, 400)

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
        self.assertNotIn("result_job_uid", document)
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
        extracted_names = {
            "WB_BULK_CREATE_PROGRESS_KEY",
            "_wb_bulk_create_managed_state",
            "_wb_bulk_create_is_stale",
            "_wb_bulk_create_public_job",
            "api_job_status",
            "api_jobs_active",
        }
        extracted = []
        for node in tree.body:
            name = None
            if isinstance(node, ast.FunctionDef):
                name = node.name
            elif isinstance(node, ast.Assign):
                name = next((target.id for target in node.targets
                             if isinstance(target, ast.Name)), None)
            if name in extracted_names:
                if isinstance(node, ast.FunctionDef) and name in {"api_job_status", "api_jobs_active"}:
                    node.decorator_list = []
                extracted.append(node)
        self.assertEqual(
            {node.name if isinstance(node, ast.FunctionDef) else node.targets[0].id
             for node in extracted},
            extracted_names,
        )
        namespace = {"current_user": self.user_view, "BackgroundJob": BackgroundJob,
                     "datetime": datetime, "db": db, "jsonify": jsonify}
        exec(compile(ast.Module(body=extracted, type_ignores=[]), str(source), "exec"), namespace)
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
