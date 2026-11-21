"""Offline Chromium journey for exact mixed Ozon bulk-ready review.

The real Flask routes, SQLite preparation journal, v3 workers and operation
queue run against a new synthetic database. Browser and Python network access
are denied; queued operations are inspected but never dispatched to a provider.
"""

from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import traceback
from urllib.parse import parse_qs, urlsplit

import requests
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright
from sqlalchemy import event
from werkzeug.serving import make_server


OUT = Path(os.environ.get("OZON_BROWSER_ARTIFACTS", "/artifacts"))
OUT.mkdir(parents=True, exist_ok=True)
ASSETS = Path(__file__).with_name("assets")
ASSET_MANIFEST = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
for _asset in ASSET_MANIFEST.values():
    assert hashlib.sha256((ASSETS / _asset["file"]).read_bytes()).hexdigest() == _asset["sha256"]

report = {
    "status": "running",
    "checks": [],
    "provider_attempts": 0,
    "provider_dispatch_calls": 0,
    "unexpected_external": [],
    "unexpected_http": [],
    "js_errors": [],
    "post_counts": {},
    "source_prepare": {},
    "review": {},
    "reviewed_queue": {},
    "scope": "synthetic_ozon_bulk_ready_review_browser",
    "multi_page_review": "not_run; 4-row journey exercises the real API; 200-row paging contract stays in its Node test",
}
TEMP = tempfile.TemporaryDirectory(prefix="ozon-bulk-ready-review-")
TEMP_ROOT = Path(TEMP.name)
os.environ.update(
    DATABASE_URL="sqlite:///" + str(TEMP_ROOT / "app.sqlite"),
    SKIP_SCHEDULER="1",
    IMAGE_LAB_INLINE_WORKER="0",
    SECRET_KEY="synthetic-ozon-bulk-ready-review-session",
    ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"),
    PUBLIC_BASE_URL="https://seller.test",
    MARKETPLACE_OZON_ENABLED="1",
    MARKETPLACE_OZON_PUBLICATION_ENABLED="1",
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED="0",
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED="0",
    OZON_RATE_LIMIT_DIR=str(TEMP_ROOT / "limits"),
    MARKETPLACE_IMAGE_ASSET_DIR=str(TEMP_ROOT / "assets"),
)


def forbid_network(*_args, **_kwargs):
    report["provider_attempts"] += 1
    raise AssertionError("External Python network is forbidden in this fixture")


requests.sessions.Session.request = forbid_network
socket.create_connection = forbid_network

from seller_platform import app
from models import (
    BackgroundJob,
    ImportedProduct,
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceCategoryMapping,
    MarketplaceListing,
    MarketplaceOperation,
    MarketplaceProductDraft,
    MarketplaceProductType,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_drafts import MarketplaceDraftService
from services.marketplace_publications import MarketplacePublicationService
from services.ozon_bulk_upload import OzonBulkUploadService
from tests.ozon_release.seed import PASSWORD, PHOTO, USERNAME, seed


app.config.update(
    TESTING=True,
    WTF_CSRF_ENABLED=True,
    SESSION_COOKIE_SECURE=False,
    SECRET_KEY=os.environ["SECRET_KEY"],
    MARKETPLACE_OZON_ENABLED=True,
    MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=False,
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
)
fixture = seed(app)
account_id = fixture["account_id"]
marketplace_id = None
source_ids = {}
source_titles = {}
expected_actions = {}
blocked_source_id = None


def _make_source(*, seller_id, external_id, vendor_code, title, description, category,
                 barcode="4600000000001"):
    """Build a source-complete but otherwise ordinary seller-owned product."""
    dimensions = {
        "Ширина упаковки, мм": 200,
        "Высота упаковки, мм": 30,
        "Длина упаковки, мм": 300,
        "Вес упаковки, г": 250,
    }
    raw = {
        "title": title,
        "description": description,
        "category": category,
        "vendor_code": vendor_code,
        "barcodes": [barcode],
        "photo_urls": [PHOTO],
        "dimensions": dimensions,
    }
    return ImportedProduct(
        seller_id=seller_id,
        external_id=external_id,
        external_vendor_code=vendor_code,
        source_type="synthetic",
        title=title,
        description=description,
        category=category,
        # The source display remains photo-cache-free. The observed raw photo
        # is still part of the fact pack used by the real preparation worker.
        photo_urls="[]",
        original_data=json.dumps(raw, ensure_ascii=False),
        calculated_price=1000.0,
        calculated_price_before_discount=1200.0,
        import_status="pending",
    )


with app.app_context():
    account = db.session.get(SellerMarketplaceAccount, account_id)
    marketplace_id = account.marketplace_id
    account.settings_json = json.dumps({"default_vat": "0.22"})
    account.connection_status = "connected"

    # Keep the seed's unrelated existing source out of the photo-cache path.
    seed_source = db.session.get(ImportedProduct, fixture["source_id"])
    seed_source.photo_urls = "[]"

    create_type = MarketplaceProductType.query.filter_by(
        marketplace_id=marketplace_id,
        external_type_id="1000",
    ).one()
    create_title = "BR Review · новая карточка"
    update_title = "BR Review · полное обновление"
    blocked_title = "BR Review · сохранённая готовность с дрейфом"
    excluded_title = "BR Review · готовая карточка без выбора"

    create_source = _make_source(
        seller_id=fixture["seller_id"],
        external_id="br-review-create-source",
        vendor_code="BR-REVIEW-CREATE",
        title=create_title,
        description="Синтетическое наблюдённое описание для новой карточки.",
        category=create_type.category.name,
    )

    update_listing = db.session.get(MarketplaceListing, fixture["listing_ids"][1])
    update_type = db.session.get(MarketplaceProductType, update_listing.product_type_id)
    update_listing.title = update_title
    update_listing.description = "Синтетическое наблюдённое описание для обновления."
    update_listing.external_category_id = update_type.category.external_category_id
    update_listing.external_type_id = update_type.external_type_id
    update_listing.attributes_json = "[]"
    update_listing.complex_attributes_json = "[]"
    update_listing.media_json = json.dumps({"primary_image": PHOTO, "images": []})
    update_listing.dimensions_json = json.dumps({
        "width": 200,
        "height": 30,
        "depth": 300,
        "weight": 250,
        "dimension_unit": "MILLIMETERS",
        "weight_unit": "GRAMS",
    })
    update_listing.barcodes_json = json.dumps(["4600000000001"])
    update_listing.price_summary_json = json.dumps({
        "available": True,
        "currency": "RUB",
        "values": {
            "price": "1000",
            "old_price": "1200",
            "currency_code": "RUB",
            "vat": "0.22",
        },
    })
    update_listing.imported_product_id = None
    update_listing.link_status = "unlinked"
    update_listing.link_source = None
    update_listing.link_evidence_json = "{}"
    update_source = _make_source(
        seller_id=fixture["seller_id"],
        external_id="br-review-update-source",
        vendor_code=update_listing.offer_id,
        title=update_title,
        description=update_listing.description,
        category=update_type.category.name,
    )

    blocked_source = _make_source(
        seller_id=fixture["seller_id"],
        external_id="br-review-blocked-source",
        vendor_code="BR-REVIEW-BLOCKED",
        title=blocked_title,
        description="Синтетическое наблюдённое описание для проверки дрейфа.",
        category=create_type.category.name,
    )
    excluded_source = _make_source(
        seller_id=fixture["seller_id"],
        external_id="br-review-excluded-source",
        vendor_code="BR-REVIEW-EXCLUDED",
        title=excluded_title,
        description="Готовая синтетическая карточка, которую пользователь не выберет.",
        category=create_type.category.name,
        barcode="4600000000002",
    )
    db.session.add_all([create_source, update_source, blocked_source, excluded_source])
    db.session.flush()

    # A reviewed exact seller mapping makes the create rows selectable without
    # relying on lexical category inference in this browser fixture.
    identity = MarketplaceDraftService._mapping_identities(create_source)[-1]
    db.session.add(MarketplaceCategoryMapping(
        seller_id=fixture["seller_id"],
        marketplace_id=marketplace_id,
        supplier_id=identity["supplier_id"],
        product_type_id=create_type.id,
        scope_key=identity["scope_key"],
        source_type=identity["source_type"],
        source_category=identity["source_category"],
        source_category_normalized=identity["source_category_normalized"],
        external_category_id=create_type.category.external_category_id,
        external_type_id=create_type.external_type_id,
        mapping_source="manual",
        mapping_status="active",
        confidence=1.0,
        evidence_json='{"fixture":"exact_seller_mapping"}',
        corrected_by_user_id=None,
    ))
    update_listing.imported_product = update_source
    update_listing.link_status = "linked"
    update_listing.link_source = "exact_source_identity"
    update_listing.link_version = 1
    update_listing.linked_at = update_listing.last_seen_at = datetime.utcnow()
    update_listing.link_evidence_json = "{}"

    db.session.commit()
    source_ids = {
        "create": create_source.id,
        "update": update_source.id,
        "blocked": blocked_source.id,
        "excluded": excluded_source.id,
    }
    source_titles = {
        "create": create_title,
        "update": update_title,
        "blocked": blocked_title,
        "excluded": excluded_title,
    }
    expected_actions = {"create": "create", "update": "update"}
    blocked_source_id = blocked_source.id


logging.getLogger("werkzeug").setLevel(logging.ERROR)
server = make_server("127.0.0.1", 0, app, threaded=True)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
base = f"http://127.0.0.1:{server.server_port}"
photo_svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="100"><rect width="80" height="100" fill="#c7a66a"/></svg>'
state = {
    "source_job_uid": None,
    "source_item_ids": [],
    "source_prepare_post": None,
    "source_prepare_repair_post": None,
    "source_worker_stats": None,
    "source_run_document": None,
    "reviewed_post": None,
    "reviewed_worker_stats": None,
    "review_api_pages": [],
    "queue_confirmed": False,
}
business_write_events = []
BUSINESS_TABLES = {
    "imported_products",
    "marketplace_product_drafts",
    "marketplace_category_mappings",
    "ozon_bulk_upload_runs",
    "ozon_bulk_upload_items",
    "marketplace_operations",
}


def record_business_write(_conn, _cursor, statement, _parameters, _context, _executemany):
    match = re.match(
        r"\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+[\"`]?([A-Za-z0-9_]+)",
        statement,
        re.IGNORECASE,
    )
    if match and match.group(1).lower() in BUSINESS_TABLES:
        business_write_events.append(match.group(1).lower())


with app.app_context():
    event.listen(db.engine, "before_cursor_execute", record_business_write)


def _run_real_source_worker(job_uid):
    with app.app_context():
        stats = OzonBulkUploadService.run_due_preparation()
        job = BackgroundJob.query.filter_by(job_uid=job_uid).one()
        run = OzonBulkUploadRun.query.filter_by(job_id=job.id).one()
        items = OzonBulkUploadItem.query.filter_by(run_id=run.id).order_by(
            OzonBulkUploadItem.ordinal.asc()
        ).all()
        assert run.mode == "source_prepare"
        assert run.account_id == account_id
        assert run.seller_id == fixture["seller_id"]
        assert len(items) == 4
        assert {item.imported_product_id for item in items} == set(source_ids.values())
        assert all(item.phase == "prepared" and item.draft_id for item in items), [
            (item.imported_product_id, item.phase, item.error_code) for item in items
        ]
        drafts = [db.session.get(MarketplaceProductDraft, item.draft_id) for item in items]
        assert all(draft.status == "ready" for draft in drafts)
        assert MarketplaceOperation.query.count() == 0

        blocked_draft = MarketplaceProductDraft.query.filter_by(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            imported_product_id=blocked_source_id,
        ).one()
        assert blocked_draft.status == "ready"
        stored_validation = json.loads(blocked_draft.validation_result_json or "{}")
        assert stored_validation.get("publishable") is True

        # Simulate a source change after preparation while leaving the cached
        # ready status intact. The read-only review must revalidate and block it.
        blocked_product = db.session.get(ImportedProduct, blocked_source_id)
        blocked_product.description = "Изменение источника после локальной подготовки."
        db.session.commit()

        run_document = OzonBulkUploadService.public_document(job, detail=True)
        state["source_worker_stats"] = stats
        state["source_run_document"] = run_document
        state["source_item_ids"] = [item.imported_product_id for item in items]
        state["draft_ids"] = [item.draft_id for item in items]
        state["prepared_versions"] = {
            str(item.draft_id): item.prepared_version for item in items
        }
        state["prepared_phases"] = [item.phase for item in items]
        return stats


def _run_real_review_worker():
    with app.app_context():
        stats = OzonBulkUploadService.run_due_preparation()
        operations = MarketplaceOperation.query.filter_by(
            seller_id=fixture["seller_id"],
            account_id=account_id,
        ).order_by(MarketplaceOperation.id.asc()).all()
        run = OzonBulkUploadRun.query.filter_by(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            mode="reviewed_drafts",
        ).one()
        assert stats["enqueued"] == 2, stats
        assert len(operations) == 2
        assert sorted(operation.operation_kind for operation in operations) == [
            "product_import", "product_update",
        ]
        assert all(operation.status == "queued" for operation in operations)
        assert all(operation.attempt_count == 0 for operation in operations)
        assert all(operation.poll_count == 0 for operation in operations)
        assert all(operation.submitted_at is None for operation in operations)
        assert all(operation.seller_id == fixture["seller_id"] for operation in operations)
        assert all(operation.account_id == account_id for operation in operations)
        state["reviewed_worker_stats"] = stats
        state["queued_operation_kinds"] = sorted(
            operation.operation_kind for operation in operations
        )
        state["queued_operation_statuses"] = [operation.status for operation in operations]
        state["reviewed_run_uid"] = BackgroundJob.query.filter_by(
            id=run.job_id
        ).one().job_uid
        return stats


def bridge(route):
    request = route.request
    url = urlsplit(request.url)

    if url.hostname == "127.0.0.1" and url.port == server.server_port:
        path = url.path
        if request.method not in ("GET", "HEAD"):
            if path not in {
                "/login",
                "/marketplaces/ozon/uploads/",
                "/marketplaces/ozon/uploads/from-drafts",
            }:
                if not (request.method == "POST" and path.endswith("/repair/apply")):
                    report["unexpected_http"].append({"method": request.method, "path": path})
                    route.abort()
                    return
            if path != "/login":
                report["post_counts"][path] = report["post_counts"].get(path, 0) + 1
                if not request.headers.get("x-csrftoken"):
                    raise AssertionError(f"CSRF header missing for {path}")

        if request.method == "GET" and path == "/marketplaces/ozon/uploads/api/review":
            query = parse_qs(url.query, keep_blank_values=True)
            assert query.get("account_id") == [str(account_id)], query
            assert query.get("parent_prepare_job_uid") == [state["source_job_uid"]], query
            ids = [int(value) for value in query.get("draft_ids", [""])[0].split(",")]
            assert ids == state["draft_ids"], (ids, state["draft_ids"])
            page = int(query.get("page", ["1"])[0])
            state["review_api_pages"].append(page)

        if request.method == "POST" and path == "/marketplaces/ozon/uploads/":
            body = json.loads(request.post_data or "{}")
            assert set(body) == {
                "account_id", "imported_product_ids", "request_key", "confirm_prepare",
            }, set(body)
            assert body["account_id"] == account_id
            assert body["confirm_prepare"] is True
            assert set(body["imported_product_ids"]) == set(source_ids.values())
            assert len(body["imported_product_ids"]) == 4
            assert re.fullmatch(r"[A-Za-z0-9_-]{24,128}", body["request_key"])
            state["source_prepare_post"] = {
                "account_id": body["account_id"],
                "source_ids": body["imported_product_ids"],
                "confirm_prepare": body["confirm_prepare"],
                "csrf": True,
            }

        if request.method == "POST" and path.endswith("/repair/apply"):
            form = parse_qs(request.post_data or "", keep_blank_values=True)
            selected_ids = [int(value) for value in form.get("selected_draft_id", [])]
            assert selected_ids == state["repair_row_ids"], selected_ids
            state["source_prepare_repair_post"] = {
                "draft_ids": selected_ids,
                "csrf": bool(request.headers.get("x-csrftoken")),
            }

        if request.method == "POST" and path == "/marketplaces/ozon/uploads/from-drafts":
            body = json.loads(request.post_data or "{}")
            assert body.get("account_id") == account_id
            assert body.get("confirm_write") is True
            assert body.get("parent_prepare_job_uid") == state["source_job_uid"]
            assert set(body.get("draft_ids", [])) == set(state["ready_draft_ids"])
            assert len(body.get("draft_ids", [])) == 2
            assert set(body.get("expected_versions", {})) == {
                str(draft_id) for draft_id in state["ready_draft_ids"]
            }
            assert all(
                type(version) is int and version == state["review_versions"][draft_id]
                for draft_id, version in (
                    (int(key), value) for key, value in body["expected_versions"].items()
                )
            )
            assert re.fullmatch(r"[A-Za-z0-9_-]{24,128}", body.get("request_key", ""))
            state["reviewed_post"] = {
                "account_id": body["account_id"],
                "draft_ids": body["draft_ids"],
                "expected_versions": body["expected_versions"],
                "parent_prepare_job_uid": body["parent_prepare_job_uid"],
                "confirm_write": body["confirm_write"],
                "csrf": True,
            }
            state["queue_confirmed"] = True

        response = route.fetch(max_redirects=0)
        if response.status >= 500:
            report["unexpected_http"].append({"method": request.method, "path": path, "status": response.status})

        if request.method == "POST" and path == "/marketplaces/ozon/uploads/":
            assert response.status == 202, (response.status, response.text()[:300])
            accepted = response.json()
            assert accepted["success"] is True
            assert accepted["run"]["mode"] == "source_prepare"
            assert accepted["run"]["account_id"] == account_id
            state["source_job_uid"] = accepted["run"]["job_uid"]
            # The real durable v3 worker runs only after the ordinary local
            # prepare POST has committed and before its normal response lands.
            _run_real_source_worker(state["source_job_uid"])

        elif request.method == "POST" and path == "/marketplaces/ozon/uploads/from-drafts":
            assert response.status == 202, (response.status, response.text()[:300])
            accepted = response.json()
            assert accepted["success"] is True
            assert accepted["run"]["mode"] == "reviewed_drafts"
            assert accepted["run"]["account_id"] == account_id
            # This is the real queue worker, after the explicit review/confirm
            # POST. It creates durable operation rows but never calls a provider.
            _run_real_review_worker()

        route.fulfill(response=response)
        return

    if request.url == PHOTO:
        route.fulfill(body=photo_svg, content_type="image/svg+xml")
        return

    asset = ASSET_MANIFEST.get(request.url) or ASSET_MANIFEST.get(request.url.rstrip("/"))
    if asset:
        route.fulfill(
            body=(ASSETS / asset["file"]).read_bytes(),
            content_type=asset["content_type"],
        )
        return

    report["unexpected_external"].append({"host": url.hostname, "path": url.path})
    route.abort()


def _business_counts():
    with app.app_context():
        return {
            "drafts": MarketplaceProductDraft.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
            ).count(),
            "runs": OzonBulkUploadRun.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
            ).count(),
            "items": OzonBulkUploadItem.query.join(
                OzonBulkUploadRun, OzonBulkUploadItem.run_id == OzonBulkUploadRun.id,
            ).filter(
                OzonBulkUploadRun.seller_id == fixture["seller_id"],
                OzonBulkUploadRun.account_id == account_id,
            ).count(),
            "operations": MarketplaceOperation.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
            ).count(),
        }


def passed(name):
    assert not report["js_errors"], report["js_errors"]
    assert not report["unexpected_external"], report["unexpected_external"]
    assert report["provider_attempts"] == 0
    report["checks"].append(name)
    print(json.dumps({"browser_check": name}), flush=True)


def run_browser_journey():
    with app.app_context():
        account_rows = SellerMarketplaceAccount.query.filter_by(
            seller_id=fixture["seller_id"], marketplace_id=marketplace_id,
            is_active=True,
        ).all()
        assert [row.id for row in account_rows] == [account_id]
        baseline_counts = _business_counts()
    report["source_prepare"]["source_count"] = 4
    report["source_prepare"]["baseline_counts"] = baseline_counts

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get(
                "OZON_BROWSER_CHROMIUM",
                os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium"),
            ),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 980},
            service_workers="block",
            timezone_id="Europe/Moscow",
        )
        context.route("**/*", bridge)
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
        page.on("response", lambda response: report["unexpected_http"].append({
            "status": response.status,
            "path": urlsplit(response.url).path,
        }) if response.status >= 500 else None)

        page.goto(base + "/login")
        page.locator('input[name="username"]').fill(USERNAME)
        page.locator('input[name="password"]').fill(PASSWORD)
        page.get_by_role("button", name="Войти", exact=True).click()
        page.wait_for_url(lambda value: urlsplit(value).path != "/login")
        page.goto(base + "/my-products/beta")
        page.locator("#my-products-app:not([v-cloak])").wait_for()
        page.locator(".mp-list article.mp-row").first.wait_for()

        for key, title in source_titles.items():
            page.get_by_role("checkbox", name=f"Выбрать {title}", exact=True).check()
        selected_ids = page.locator(
            ".mp-list article.mp-row input[type=checkbox]:checked"
        ).evaluate_all("nodes => nodes.map(node => Number(node.value))")
        assert set(selected_ids) == set(source_ids.values())
        assert len(selected_ids) == 4
        assert not business_write_events, business_write_events
        assert _business_counts() == baseline_counts
        report["source_prepare"]["page_selection_ids"] = selected_ids
        report["source_prepare"]["business_writes_before_prepare"] = len(business_write_events)
        report["checks"].append("beta_page_exact_selection_is_read_only_before_local_prepare")

        page.get_by_role("button", name="Что сделать", exact=False).click()
        page.get_by_role("menuitem", name="Подготовить для Ozon", exact=False).click()
        dialog = page.get_by_role("dialog")
        account_select = dialog.locator("#mp-ozon-account")
        account_select.select_option(str(account_id))
        assert account_select.input_value() == str(account_id)
        dialog.locator(".mp-ozon-confirm input").check()

        with page.expect_response(lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path == "/marketplaces/ozon/uploads/"
        )) as source_response_info:
            dialog.get_by_role("button", name="Подготовить черновики", exact=False).click()
        source_response = source_response_info.value
        assert source_response.status == 202
        assert state["source_prepare_post"] is not None
        assert state["source_worker_stats"]["prepared"] == 4
        assert state["source_worker_stats"]["needs_input"] == 0
        assert state["source_worker_stats"]["processed_items"] == 4
        assert state["source_job_uid"]
        page.wait_for_url(f"**/marketplaces/ozon/uploads/{state['source_job_uid']}")
        page.locator("h1").wait_for()

        source_doc = page.locator("#ozon-result-status").count()
        detail_review_link = page.locator("a[data-source-prepare-review]")
        detail_review_link.wait_for()
        assert detail_review_link.inner_text().strip() == "Проверить перед отправкой"
        review_href = detail_review_link.get_attribute("href")
        review_parts = urlsplit(review_href)
        review_query = parse_qs(review_parts.query)
        assert review_parts.path == "/marketplaces/ozon/uploads/review"
        assert review_query.get("account_id") == [str(account_id)]
        assert review_query.get("parent_prepare_job_uid") == [state["source_job_uid"]]
        assert [int(value) for value in review_query["draft_ids"][0].split(",")] == state["draft_ids"]
        report["source_prepare"].update({
            "job_uid": state["source_job_uid"],
            "worker_stats": state["source_worker_stats"],
            "source_ids": state["source_item_ids"],
            "draft_ids": state["draft_ids"],
            "prepared_versions": state["prepared_versions"],
            "prepared_phases": state["prepared_phases"],
            "detail_review_link_exact": True,
            "detail_has_result_component": bool(source_doc),
        })
        passed("ordinary_beta_prepare_persisted_four_drafts_and_exact_job_review_link")

        repair_link = page.locator("a[data-source-prepare-repair]")
        repair_link.wait_for()
        repair_href = repair_link.get_attribute("href")
        assert urlsplit(repair_href).path == (
            f"/marketplaces/ozon/uploads/{state['source_job_uid']}/repair"
        )
        repair_link.click()
        page.wait_for_url(
            f"**/marketplaces/ozon/uploads/{state['source_job_uid']}/repair"
        )
        page.locator(
            '#ozon-bulk-repair[data-editor-mode="source_prepare"]'
        ).wait_for()
        editor = page.evaluate(
            "() => JSON.parse(document.querySelector('#orb-bootstrap').textContent).editor"
        )
        assert editor["mode"] == "source_prepare"
        assert editor["review_draft_ids"] == state["draft_ids"]
        assert editor["account_id"] == account_id
        page.set_viewport_size({"width": 320, "height": 780})
        draft_by_source = dict(zip(state["source_item_ids"], state["draft_ids"]))
        blocked_draft_id = draft_by_source[source_ids["blocked"]]
        with app.app_context():
            source_run = OzonBulkUploadRun.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
                mode="source_prepare",
            ).one()
            blocked_item = OzonBulkUploadItem.query.filter_by(
                run_id=source_run.id,
                imported_product_id=source_ids["blocked"],
            ).one()
            assert blocked_item.phase == "prepared"
            blocked_source_receipt = (
                blocked_item.phase,
                blocked_item.prepared_version,
                blocked_item.error_code,
                blocked_item.error_message,
            )
        blocked_editor_row_data = [
            row for group in editor["groups"] for row in group["rows"]
            if row["draft_id"] == blocked_draft_id
        ]
        assert len(blocked_editor_row_data) == 1
        assert blocked_editor_row_data[0]["phase"] == "prepared"
        assert blocked_editor_row_data[0]["status"] == "needs_input"
        blocked_editor_row = page.locator(
            f'.orb-row[data-draft="{blocked_draft_id}"]'
        )
        assert blocked_editor_row.locator(".orb-state").inner_text().strip() == "Нужны данные"

        # Use the actual native bulk editor to fill one field on one new and
        # one update draft. This remains a local edit to the prepared run.
        repair_row_ids = [
            draft_by_source[source_ids["create"]],
            draft_by_source[source_ids["update"]],
        ]
        state["repair_row_ids"] = repair_row_ids
        for draft_id in repair_row_ids:
            page.locator(
                f'.orb-row[data-draft="{draft_id}"] input[type="checkbox"]'
            ).check()
        page.get_by_role("button", name="Заполнить поле", exact=True).click()
        bulk_dialog = page.get_by_role("dialog")
        bulk_dialog.wait_for(state="visible")
        bulk_dialog.locator("#orb-bulk-field").select_option("description")
        bulk_dialog.get_by_label("Новое значение").fill(
            "Локальная групповая правка перед повторной проверкой."
        )
        bulk_dialog.get_by_role("button", name="Посмотреть изменения", exact=True).click()
        assert bulk_dialog.locator(".orb-review-list li").count() == 2
        bulk_dialog.get_by_role("button", name="Заполнить в форме", exact=True).click()
        for draft_id in repair_row_ids:
            row = page.locator(f'.orb-row[data-draft="{draft_id}"]')
            if not row.locator("textarea").count():
                row.get_by_role("button", name="Изменить", exact=True).click()
            assert row.locator(
                f'textarea[name="description-{draft_id}"]'
            ).input_value() == "Локальная групповая правка перед повторной проверкой."
        page.locator(".orb-toolbar").get_by_role(
            "button", name="Сохранить черновики", exact=True,
        ).click()
        save_dialog = page.get_by_role("dialog")
        save_dialog.wait_for(state="visible")
        assert save_dialog.locator(".orb-review-list li").count() == 2
        repair_write_marker = len(business_write_events)
        with page.expect_response(lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path.endswith("/repair/apply")
        )) as repair_response_info:
            save_dialog.get_by_role(
                "button", name="Сохранить черновики", exact=True,
            ).click()
        repair_response = repair_response_info.value
        assert repair_response.status == 200
        assert state["source_prepare_repair_post"] == {
            "draft_ids": repair_row_ids,
            "csrf": True,
        }
        page.locator(".orb-receipt[data-source-prepare-receipt] strong").filter(
            has_text="Обработано черновиков: 2"
        ).wait_for()
        receipt = page.locator(".orb-receipt[data-source-prepare-receipt]")
        return_review_link = receipt.locator("a[data-source-prepare-review]")
        assert return_review_link.count() == 1
        return_review_link.wait_for()
        page.wait_for_function(
            "() => document.activeElement === document.querySelector('[data-source-prepare-receipt]')"
        )
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        header_bounds = page.locator(".mobile-header").bounding_box()
        summary_bounds = receipt.locator("strong").bounding_box()
        review_link_bounds = return_review_link.bounding_box()
        assert header_bounds and summary_bounds and review_link_bounds
        header_bottom = header_bounds["y"] + header_bounds["height"]
        for name, bounds in (
            ("summary", summary_bounds),
            ("review link", review_link_bounds),
        ):
            assert bounds["y"] >= header_bottom, (name, bounds, header_bounds)
            assert bounds["y"] + bounds["height"] <= 780, (name, bounds)
            assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 320, (name, bounds)
        assert receipt.evaluate("node => document.activeElement === node")
        repair_mobile_screenshot = OUT / "ozon-bulk-source-prepare-repair-mobile.png"
        page.screenshot(path=str(repair_mobile_screenshot))
        with app.app_context():
            run = OzonBulkUploadRun.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
                mode="source_prepare",
            ).one()
            source_items = OzonBulkUploadItem.query.filter_by(
                run_id=run.id,
            ).order_by(OzonBulkUploadItem.ordinal.asc()).all()
            assert [item.draft_id for item in source_items] == state["draft_ids"]
            assert [item.phase for item in source_items] == ["prepared"] * 4
            blocked_after = next(
                item for item in source_items
                if item.imported_product_id == source_ids["blocked"]
            )
            assert (
                blocked_after.phase,
                blocked_after.prepared_version,
                blocked_after.error_code,
                blocked_after.error_message,
            ) == blocked_source_receipt
            for item in source_items[:2]:
                draft = db.session.get(MarketplaceProductDraft, item.draft_id)
                assert item.prepared_version == draft.version
                assert json.loads(draft.content_json)["description"] == (
                    "Локальная групповая правка перед повторной проверкой."
                )
            assert MarketplaceOperation.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
            ).count() == 0
        assert len(business_write_events) > repair_write_marker
        assert "marketplace_operations" not in business_write_events[repair_write_marker:]
        report["source_prepare"].update({
            "native_repair_mode": editor["mode"],
            "native_repair_ordered_draft_ids": editor["review_draft_ids"],
            "native_repair_saved_draft_ids": repair_row_ids,
            "native_repair_prepared_versions_current": True,
            "native_repair_operations_created": 0,
            "native_repair_mobile_viewport": {
                "width": 320,
                "height": 780,
                "focused_receipt": True,
                "summary_visible_below_fixed_header": True,
                "return_action_visible_below_fixed_header": True,
                "no_horizontal_overflow": True,
                "screenshot": repair_mobile_screenshot.name,
            },
        })
        passed("source_prepare_native_bulk_repair_saved_two_rows_locally_without_operation")

        return_review_href = return_review_link.get_attribute("href")
        return_parts = urlsplit(return_review_href)
        return_query = parse_qs(return_parts.query)
        assert return_parts.path == "/marketplaces/ozon/uploads/review"
        assert return_query.get("account_id") == [str(account_id)]
        assert return_query.get("parent_prepare_job_uid") == [state["source_job_uid"]]
        assert [int(value) for value in return_query["draft_ids"][0].split(",")] == state["draft_ids"]
        review_write_marker = len(business_write_events)
        return_review_link.click()
        page.wait_for_url("**/marketplaces/ozon/uploads/review?*")
        page.locator("#ozon-upload-review:not([v-cloak])").wait_for()
        bootstrap = page.evaluate(
            "() => JSON.parse(document.querySelector('#our-bootstrap').textContent).review"
        )
        assert bootstrap["account_id"] == account_id
        assert bootstrap["parent_prepare_job_uid"] == state["source_job_uid"]
        assert bootstrap["draft_ids"] == state["draft_ids"]
        assert [
            int(value) for value in parse_qs(urlsplit(page.url).query)["draft_ids"][0].split(",")
        ] == state["draft_ids"]
        preparation_back = page.get_by_role(
            "link", name="К результатам подготовки", exact=True,
        )
        preparation_back.wait_for()
        assert urlsplit(preparation_back.get_attribute("href")).path == (
            f"/marketplaces/ozon/uploads/{state['source_job_uid']}"
        )
        assert len(bootstrap["items"]) == 4
        rows_by_source = {
            row["imported_product_id"]: row for row in bootstrap["items"]
        }
        create_row = rows_by_source[source_ids["create"]]
        update_row = rows_by_source[source_ids["update"]]
        blocked_row = rows_by_source[source_ids["blocked"]]
        excluded_row = rows_by_source[source_ids["excluded"]]
        assert create_row["action"] == "create" and create_row["selectable"] is True
        assert update_row["action"] == "update" and update_row["selectable"] is True
        assert blocked_row["selectable"] is False
        assert "source_facts_stale" in {issue["code"] for issue in blocked_row["errors"]}
        page.get_by_text(
            "Исходный товар изменился. Откройте черновик и обновите данные товара.",
            exact=True,
        ).wait_for()
        assert excluded_row["action"] == "create" and excluded_row["selectable"] is True
        blocked_checkbox = page.locator(f"#our-select-{blocked_row['draft_id']}")
        assert blocked_checkbox.is_disabled()

        page.set_viewport_size({"width": 320, "height": 780})
        batch_button = page.get_by_role("button", name="Проверить все готовые", exact=True)
        assert batch_button.is_visible()
        batch_button.click()
        review_dialog = page.get_by_role("dialog")
        review_dialog.wait_for(state="visible")
        for control in (
            batch_button,
            review_dialog.get_by_role("button", name="Закрыть подтверждение", exact=True),
        ):
            bounds = control.bounding_box()
            assert bounds and bounds["width"] >= 44 and bounds["height"] >= 44
        summary_spans = review_dialog.locator(".our-dialog-summary span").all_text_contents()
        assert "Готовы к отправке: 3 из 4 проверенных." in summary_spans, summary_spans
        assert "Требуют проверки или исправления: 1." in summary_spans, summary_spans
        modal_rows = review_dialog.locator(".our-dialog-list li")
        assert modal_rows.count() == 3
        assert source_titles["blocked"] not in review_dialog.inner_text()
        assert "Создание" in review_dialog.inner_text()
        assert "Обновление" in review_dialog.inner_text()

        ready_draft_ids = [create_row["draft_id"], update_row["draft_id"]]
        excluded_checkbox = review_dialog.get_by_role(
            "checkbox", name=f"Отправить {source_titles['excluded']}", exact=True,
        )
        assert excluded_checkbox.is_checked()
        excluded_checkbox.uncheck()
        assert not excluded_checkbox.is_checked()
        review_versions = {
            create_row["draft_id"]: create_row["version"],
            update_row["draft_id"]: update_row["version"],
        }
        state["ready_draft_ids"] = list(ready_draft_ids)
        state["review_versions"] = dict(review_versions)
        assert len(state["review_api_pages"]) == 1 and state["review_api_pages"] == [1]
        assert _business_counts()["operations"] == 0
        assert len(business_write_events) == review_write_marker, business_write_events[review_write_marker:]
        mobile_screenshot = OUT / "ozon-bulk-ready-review-mobile.png"
        page.screenshot(path=str(mobile_screenshot), full_page=True)
        report["review"].update({
            "account_id": account_id,
            "parent_prepare_job_uid": state["source_job_uid"],
            "all_draft_ids": bootstrap["draft_ids"],
            "row_count": len(bootstrap["items"]),
            "ready_count": 3,
            "blocked_count": 1,
            "ready_actions": [create_row["action"], update_row["action"], excluded_row["action"]],
            "blocked_never_selectable": blocked_checkbox.is_disabled(),
            "blocked_error_codes": sorted(issue["code"] for issue in blocked_row["errors"]),
            "excluded_ready_row_unchecked_before_post": not excluded_checkbox.is_checked(),
            "review_api_pages": state["review_api_pages"],
            "review_versions": {str(key): value for key, value in review_versions.items()},
            "operation_count_before_confirmation": 0,
            "business_write_count_during_review": 0,
            "mobile_viewport": {"width": 320, "height": 780, "batch_button_visible": True,
                                "modal_visible": review_dialog.is_visible(),
                                "screenshot": mobile_screenshot.name},
        })
        passed("read_only_exact_mixed_review_includes_create_update_and_excludes_stale_ready_at_mobile_width")

        # Exercise the existing row checkbox before the independent final
        # confirmation checkbox. A change must clear confirmation each time.
        update_checkbox = review_dialog.get_by_role(
            "checkbox", name=f"Отправить {source_titles['update']}", exact=True,
        )
        create_checkbox = review_dialog.get_by_role(
            "checkbox", name=f"Отправить {source_titles['create']}", exact=True,
        )
        assert update_checkbox.is_checked() and create_checkbox.is_checked()
        update_checkbox.uncheck()
        send_button = review_dialog.get_by_role(
            "button", name="Отправить проверенные карточки", exact=True,
        )
        assert send_button.is_disabled()
        update_checkbox.check()
        confirm_checkbox = review_dialog.locator(".our-confirm input")
        assert not confirm_checkbox.is_checked()
        assert send_button.is_disabled()

        with app.app_context():
            assert MarketplaceOperation.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
            ).count() == 0
        assert len(business_write_events) == review_write_marker, business_write_events[review_write_marker:]

        confirm_checkbox.check()
        assert send_button.is_enabled()
        confirm_checkbox_used = confirm_checkbox.is_checked()
        assert confirm_checkbox_used is True
        with page.expect_response(lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path == "/marketplaces/ozon/uploads/from-drafts"
        )) as reviewed_response_info:
            send_button.click()
        reviewed_response = reviewed_response_info.value
        assert reviewed_response.status == 202
        assert state["queue_confirmed"] is True
        assert state["reviewed_post"]
        assert set(state["reviewed_post"]["draft_ids"]) == set(ready_draft_ids)
        assert blocked_row["draft_id"] not in state["reviewed_post"]["draft_ids"]
        assert state["reviewed_post"]["expected_versions"] == {
            str(key): value for key, value in review_versions.items()
        }
        assert state["reviewed_worker_stats"]["enqueued"] == 2
        assert state["queued_operation_kinds"] == ["product_import", "product_update"]
        page.wait_for_url(f"**/marketplaces/ozon/uploads/{state['reviewed_run_uid']}")

        report["review"].update({
            "final_selected_draft_ids": state["reviewed_post"]["draft_ids"],
            "exact_version_evidence": [
                {
                    "draft_id": key,
                    "review_version": review_versions[key],
                    "posted_version": state["reviewed_post"]["expected_versions"][str(key)],
                    "matches": review_versions[key] == state["reviewed_post"]["expected_versions"][str(key)],
                }
                for key in state["reviewed_post"]["draft_ids"]
            ],
            "confirm_checkbox_used": confirm_checkbox_used,
            "explicit_queue_confirmation_post": True,
        })
        report["reviewed_queue"].update({
            "worker_stats": state["reviewed_worker_stats"],
            "operation_count": 2,
            "operation_kinds": state["queued_operation_kinds"],
            "operation_statuses": state["queued_operation_statuses"],
            "provider_dispatch_calls": report["provider_dispatch_calls"],
            "provider_attempts": report["provider_attempts"],
        })
        assert report["provider_dispatch_calls"] == 0
        assert report["provider_attempts"] == 0
        assert not report["unexpected_external"]
        assert not report["unexpected_http"]
        assert not report["js_errors"]
        passed("explicit_reviewed_queue_creates_exactly_one_create_and_one_update_without_provider_dispatch")
        browser.close()


try:
    original_poll_operation = MarketplacePublicationService.poll_operation

    def forbid_dispatch(*_args, **_kwargs):
        report["provider_dispatch_calls"] += 1
        raise AssertionError("Provider dispatch is forbidden in this review harness")

    MarketplacePublicationService.poll_operation = forbid_dispatch
    run_browser_journey()
    report["status"] = "passed"
except Exception as error:
    report["status"] = "failed"
    report["error_type"] = type(error).__name__
    report["error"] = str(error)[:1200]
    report["safe_exception_frames"] = [
        {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for frame in traceback.extract_tb(error.__traceback__)[-12:]
    ]
    raise
finally:
    try:
        MarketplacePublicationService.poll_operation = original_poll_operation
    except NameError:
        pass
    try:
        event.remove(db.engine, "before_cursor_execute", record_business_write)
    except Exception:
        pass
    try:
        server.shutdown()
        server_thread.join(timeout=5)
    except Exception:
        pass
    (OUT / "ozon-bulk-ready-review-browser.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "artifact": str(OUT / "ozon-bulk-ready-review-browser.json"),
        "status": report["status"],
        "checks": report["checks"],
        "provider_attempts": report["provider_attempts"],
        "operation_count": report.get("reviewed_queue", {}).get("operation_count", 0),
    }), flush=True)
