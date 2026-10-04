"""Synthetic browser acceptance for seller-owned common product content.

The editor runs against the real Flask routes, SQLAlchemy models, signed
preview/apply service and CSRF checks. The database and seller are synthetic;
provider networking is blocked. Expected preview/apply POSTs are counted
separately from provider attempts.
"""
from __future__ import annotations

import base64
from collections import Counter
from datetime import datetime
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import requests
from flask import url_for
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
from werkzeug.serving import make_server
from PIL import Image
from sqlalchemy import event


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SOURCE = os.environ.get("UX01_COMMON_CONTENT_SOURCE", "worktree").strip().lower()
if SOURCE != "worktree":
    raise ValueError("UX01_COMMON_CONTENT_SOURCE must be worktree")
OUT = Path(os.environ.get("UX01_COMMON_CONTENT_ARTIFACTS", "/tmp/ux01-common-content"))
OUT.mkdir(parents=True, exist_ok=True)
REPORT_PATH = Path(os.environ.get("UX01_COMMON_CONTENT_REPORT", str(OUT / "browser-report.json")))
CHROMIUM = os.environ.get("CHROMIUM_BIN", "/usr/bin/google-chrome")
TEMP = tempfile.TemporaryDirectory(prefix="ux01-common-content-")
TEMP_PATH = Path(TEMP.name)
ASSETS = ROOT / "tests/ozon_release/assets"
COMMON_CONTENT_MOBILE_WIDTHS = (320, 360, 390)
COMMON_CONTENT_MOBILE_THEMES = ("light", "dark")
COMMON_CONTENT_NAVIGATOR_CHECK = "common_mobile_product_navigator_bounded_accessible_keyboard"
COMMON_PHOTO_RETRY_CHECK = "common_selected_photo_pending_preview_recovers_after_bounded_retry_without_content_write"
COMMON_PHOTO_RETRY_TOUCH_CHECK = "common_photo_retry_touch_target_focus_hit_320_360_390_light_dark"

REPORT = {
    "source": SOURCE,
    "status": "running",
    "scope": "synthetic_seller_common_content_editor",
    "database": "disposable_sqlite",
    "network_policy": "loopback_and_pinned_assets_only",
    "layouts": [],
    "pages": [],
    "checks": [],
    "interactions": [],
    "screenshots": [],
    "mobile_touch_target_observations": [],
    "mobile_product_navigator_observations": [],
    "common_photo_retry_observations": None,
    "common_photo_retry_touch_observations": [],
    "browser_api_reads": [],
    "writes": [],
    "preview_item_counts": [],
    "preview_selection_observations": [],
    "apply_result_counts": [],
    "apply_result_observations": [],
    "synthetic_actions": {
        "preview_requests": 0,
        "apply_requests": 0,
        "expected_preview_conflicts": 0,
        "expected_apply_conflicts": 0,
        "empty_description_override_requests": 0,
        "empty_route_api_reads": 0,
        "empty_route_shared_shell_reads": 0,
        "empty_route_shared_shell_read_categories": {
            "notifications_unread_count": 0,
            "background_tasks_tray": 0,
        },
        "empty_route_mutating_requests": 0,
        "empty_state_catalog_link_available": False,
        "provider_attempts": 0,
        "selected_photo_order_persisted": False,
        "channel_record_unchanged": False,
        "cancelled_local_edits": False,
    },
    "focus_observations": [],
    "unexpected_http_requests": [],
    "unexpected_external_requests": [],
    "expected_http_rejections": [],
    "bulk_50": {
        "selected_products": 0,
        "selected_product_id_fingerprint": None,
        "page_51_rejected": False,
        "preview_api_51_rejected": False,
        "last_product_keyboard_reachable": False,
        "stale_apply_atomic_rejection": False,
        "stale_denial_unchanged_selected_products": 0,
        "stale_denial_new_audits": None,
        "first_preview_product_ids_match": False,
        "recovery_preview_product_ids_match": False,
        "recovery_apply_product_ids_match": False,
        "recovery_preview_items": 0,
        "recovery_apply_items": 0,
        "persisted_overrides": 0,
        "audit_rows": 0,
        "final_content_edit_version_counts": {},
        "channel_records_unchanged": False,
        "inheritance_and_source_preserved": False,
    },
    "javascript_errors": [],
    "console_errors": [],
    "console_error_locations": [],
    "expected_conflict_console_errors": [],
    "expected_rejection_console_errors": [],
    "provider_attempts": 0,
}
EXPECTED_CONFLICTS = {
    "/api/my-products/common-content/preview": [0],
    "/api/my-products/common-content/apply": [0],
}
EXPECTED_CONFLICT_CONSOLE_COUNTS = {
    "/api/my-products/common-content/preview": 1,
    "/api/my-products/common-content/apply": 2,
}
EXPECTED_CONFLICT_CONSOLE_MESSAGE = (
    "Failed to load resource: the server responded with a status of 409 (Conflict)"
)
PENDING_EXPECTED_CONFLICT_CONSOLES = []
EXPECTED_HTTP_REJECTIONS = {}
PENDING_EXPECTED_REJECTION_CONSOLES = []
EXPECTED_REJECTION_CONSOLE_COUNTS = {
    ("GET", "/my-products/common-content", 413, "too_many_items", True): 1,
    ("POST", "/api/my-products/common-content/preview", 413, "too_many_items", False): 1,
}
EXPECTED_413_RESOURCE_ERROR = re.compile(
    r"Failed to load resource: the server responded with a status of 413 \([A-Za-z0-9 _-]{1,80}\)"
)
SHARED_SHELL_READ_PATH_CATEGORIES = {
    "/api/notifications/unread-count": "notifications_unread_count",
    "/api/tasks/tray": "background_tasks_tray",
}
PHOTO_CASE_CAPTURE = None
PHOTO_CASE_CACHE = None

os.environ.update({
    "DATABASE_URL": "sqlite:///" + str(TEMP_PATH / "seller-hub.sqlite"),
    "SKIP_SCHEDULER": "1",
    "IMAGE_LAB_INLINE_WORKER": "0",
    "SECRET_KEY": "synthetic-common-content-only",
    "ENCRYPTION_KEY": base64.urlsafe_b64encode(b"c" * 32).decode(),
    "MARKETPLACE_OZON_ENABLED": "1",
    "MARKETPLACE_OZON_PUBLICATION_ENABLED": "0",
    "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED": "0",
    "MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED": "0",
    "OZON_RATE_LIMIT_DIR": str(TEMP_PATH / "limits"),
})


def forbid_provider_network(*_args, **_kwargs):
    REPORT["provider_attempts"] += 1
    REPORT["synthetic_actions"]["provider_attempts"] += 1
    raise AssertionError("Provider networking is disabled in common-content browser fixture")


requests.sessions.Session.request = forbid_provider_network
socket.create_connection = forbid_provider_network

from seller_platform import app
from models import (
    AgentChangeSnapshot,
    ImportedProduct,
    MarketplaceListing,
    MarketplaceOperation,
    MarketplaceProductDraft,
    Seller,
    db,
)
from routes.common_product_content import register_common_product_content_routes
from services.common_product_content import CommonProductContentService, MAX_TITLE
from tests.ozon_release.seed import PASSWORD, PHOTO, USERNAME, seed


if "common_product_content" not in app.blueprints:
    register_common_product_content_routes(app)

app.config.update(
    TESTING=True,
    WTF_CSRF_ENABLED=True,
    SESSION_COOKIE_SECURE=False,
)
FIXTURE = seed(app)
SECOND_PHOTO = "https://ozon-fixture.test/common-content-side.svg"
SECOND_USERNAME = "common-content-second"

with app.app_context():
    primary = db.session.get(ImportedProduct, FIXTURE["source_id"])
    primary.title = "Синтетический товар для общего редактора"
    long_source_description = "Описание источника для безопасной проверки. " * 750
    primary.description = long_source_description
    primary.photo_urls = json.dumps([PHOTO, SECOND_PHOTO], ensure_ascii=False)
    primary.characteristics = json.dumps([
        {"name": "Материал", "value": "хлопок"},
        {"name": "Поверхность", "value": "гладкая"},
    ], ensure_ascii=False)
    primary.original_data = json.dumps({
        "title": primary.title,
        "description": long_source_description,
        "photo_urls": [PHOTO, SECOND_PHOTO],
        "characteristics": [
            {"name": "Материал", "value": "хлопок"},
            {"name": "Поверхность", "value": "гладкая"},
        ],
    }, ensure_ascii=False)
    second_external_id = "COMMON-CONTENT-SECOND-" + ("X" * 170)
    external_id_column = ImportedProduct.__table__.columns["external_id"]
    if len(second_external_id) > external_id_column.type.length:
        raise AssertionError("Navigator fixture external ID must fit ImportedProduct.external_id")
    second_title = "Длинное название товара " + ("безразрывного-текста-" * 21)
    title_column = ImportedProduct.__table__.columns["title"]
    second_title_limit = min(title_column.type.length, MAX_TITLE)
    second_title_suffixes = (" — ручная правка", " — проверка конфликта сохранения")
    if (
        len(second_title) < 400
        or len(second_title) > second_title_limit
        or any(len(second_title + suffix) > second_title_limit for suffix in second_title_suffixes)
    ):
        raise AssertionError("Navigator fixture title and edits must fit model and editor limits")
    second = ImportedProduct(
        seller_id=FIXTURE["seller_id"],
        external_id=second_external_id,
        source_type="manual",
        title=second_title,
        description="Второе описание.",
        original_data=json.dumps({
            "title": second_title,
            "description": "Второе описание.",
        }, ensure_ascii=False),
    )
    db.session.add(second)
    db.session.commit()
    FIXTURE["second_source_id"] = second.id
    FIXTURE["second_title"] = second.title
    FIXTURE["second_title_limit"] = second_title_limit
    FIXTURE["second_external_id"] = second.external_id
    FIXTURE["second_external_id_limit"] = external_id_column.type.length
    FIXTURE["user_id"] = db.session.get(Seller, FIXTURE["seller_id"]).user_id

    # Seed a prior seller override with the real service so the UI must review
    # a genuine inherit transition as well as a new override.
    primary = db.session.get(ImportedProduct, FIXTURE["source_id"])
    seed_preview = CommonProductContentService.preview(
        seller_id=FIXTURE["seller_id"],
        user_id=FIXTURE["user_id"],
        raw_items=[{
            "product_id": primary.id,
            "expected_content_edit_version": primary.content_edit_version,
            "changes": {"description": {"mode": "override", "value": "Старое ручное описание."}},
            "recipients": [],
        }],
    )
    CommonProductContentService.apply(
        seller_id=FIXTURE["seller_id"],
        user_id=FIXTURE["user_id"],
        token=seed_preview["preview_token"],
    )
    db.session.commit()
    primary = db.session.get(ImportedProduct, FIXTURE["source_id"])
    recipient = db.session.get(MarketplaceProductDraft, FIXTURE["draft_id"])
    FIXTURE["draft_before"] = {
        "content_json": recipient.content_json,
        "media_json": recipient.media_json,
        "version": recipient.version,
    }
    FIXTURE["initial_title"] = primary.title
    FIXTURE["initial_external_id"] = primary.external_id
    FIXTURE["initial_description"] = long_source_description

logging.getLogger("werkzeug").setLevel(logging.ERROR)
server = make_server("127.0.0.1", 0, app, threaded=True)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
BASE = f"http://127.0.0.1:{server.server_port}"


def load_assets() -> dict[str, dict]:
    rows = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    result = {}
    for url, row in rows.items():
        payload = (ASSETS / row["file"]).read_bytes()
        if hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise AssertionError("Pinned browser asset checksum mismatch")
        result[url] = {**row, "payload": payload}
    return result


ASSETS_BY_URL = load_assets()


def authenticated_cookie() -> dict:
    client = app.test_client()
    login_page = client.get("/login", follow_redirects=False)
    if login_page.status_code != 200:
        raise AssertionError(("synthetic login form failed", login_page.status_code))

    class CsrfInputParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tokens = []

        def handle_starttag(self, tag, attrs):
            if tag.lower() != "input":
                return
            fields = dict(attrs)
            if (
                fields.get("type", "").lower() == "hidden"
                and fields.get("name") == "csrf_token"
            ):
                self.tokens.append(fields.get("value", ""))

    parser = CsrfInputParser()
    parser.feed(login_page.get_data(as_text=True))
    if len(parser.tokens) != 1 or not parser.tokens[0].strip():
        raise AssertionError("Synthetic login form must provide exactly one nonempty CSRF input")

    response = client.post(
        "/login",
        data={
            "username": USERNAME,
            "password": PASSWORD,
            "csrf_token": parser.tokens[0],
        },
        follow_redirects=False,
    )
    if response.status_code not in (302, 303):
        raise AssertionError(("synthetic login failed", response.status_code))
    cookie = client.get_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
    if cookie is None:
        raise AssertionError("Synthetic login did not create a session")
    return {"name": cookie.key, "value": cookie.value, "url": BASE}


def bridge(route):
    request = route.request
    parsed = urlsplit(request.url)
    if parsed.scheme in {"data", "blob", "about"}:
        route.continue_()
        return
    if parsed.hostname == "127.0.0.1" and parsed.port == server.server_port:
        if (
            PHOTO_CASE_CAPTURE is not None
            and request.method == "GET"
            and parsed.path.startswith("/api/photos/imported-product/")
        ):
            try:
                slot = int(parsed.path.rsplit("/", 1)[-1])
                expected = PHOTO_CASE_CAPTURE["route_paths"][slot]
            except (KeyError, TypeError, ValueError):
                expected = None
            query = parse_qs(parsed.query, keep_blank_values=True)
            retry_values = query.get("retry", [])
            initial_query = set(query) == {"deferred"} and not retry_values
            attempt = (
                1 if initial_query else
                int(retry_values[0]) if len(retry_values) == 1 and retry_values[0].isdigit() else -1
            )
            row = {
                "attempt": attempt,
                "trigger": (
                    "initial" if attempt == 1 and PHOTO_CASE_CAPTURE["phase"] != "switch_pending"
                    else "manual_retry" if PHOTO_CASE_CAPTURE["phase"] in {"manual_retry", "switch_pending"}
                    else "automatic_retry"
                ),
                "method": request.method,
                "same_origin": parsed.scheme == "http" and parsed.netloc == urlsplit(BASE).netloc,
                "path_matches_product_and_slot": parsed.path == expected,
                "deferred_query": (
                    query.get("deferred") == ["1"]
                    and (
                        (initial_query and attempt == 1)
                        or (set(query) == {"deferred", "retry"} and retry_values == [str(attempt)])
                    )
                    and attempt > 0
                ),
            }
            if parsed.path != expected or not row["same_origin"] or not row["deferred_query"]:
                REPORT["unexpected_http_requests"].append({
                    "method": request.method,
                    "path_category": "photo_case_unexpected_route",
                })
                route.abort()
                return
            response = route.fetch(max_redirects=0)
            body = response.body()
            headers = response.headers
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            retry_after = headers.get("retry-after")
            try:
                retry_after_seconds = int(retry_after) if retry_after is not None else None
            except (TypeError, ValueError):
                retry_after_seconds = -1
            queue_count = PHOTO_CASE_CACHE.queue_count_for(slot)
            row.update({
                "http_status": response.status,
                "content_type": content_type,
                "response_body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "retry_after_seconds": retry_after_seconds,
                "photo_cache": headers.get("x-photo-cache"),
                "photo_queue": headers.get("x-photo-queue"),
                "queue_count": queue_count,
                "cache_ready": PHOTO_CASE_CACHE.last_lookup_for(slot),
                "_slot": slot,
                "_phase": PHOTO_CASE_CAPTURE["phase"],
            })
            PHOTO_CASE_CAPTURE["requests"].append(row)
            route.fulfill(response=response)
            return
        if request.method in {"GET", "HEAD"}:
            if parsed.path.startswith("/api/"):
                shared_category = SHARED_SHELL_READ_PATH_CATEGORIES.get(parsed.path)
                if shared_category:
                    read_kind = "shared_shell_read"
                    path_category = shared_category
                elif parsed.path.startswith("/api/my-products/"):
                    read_kind = "product_read"
                    path_category = "common_product_content"
                else:
                    # Keep every unallowlisted API request visible to the
                    # empty-editor guard without storing IDs or query values.
                    read_kind = "other_api_read"
                    path_category = "non_allowlisted_api"
                REPORT["browser_api_reads"].append({
                    "method": request.method,
                    "kind": read_kind,
                    "path_category": path_category,
                })
        elif request.method == "POST" and parsed.path == "/api/my-products/common-content/preview":
            REPORT["synthetic_actions"]["preview_requests"] += 1
            REPORT["writes"].append({"method": "POST", "path": parsed.path, "kind": "synthetic_preview"})
            try:
                preview_body = json.loads(request.post_data or "{}")
                preview_items = preview_body.get("items", [])
                REPORT["preview_item_counts"].append(
                    len(preview_items) if isinstance(preview_items, list) else None
                )
                preview_ids = [
                    item.get("product_id") for item in preview_items
                    if isinstance(item, dict)
                ] if isinstance(preview_items, list) else []
                valid_preview_ids = (
                    len(preview_ids) == len(preview_items)
                    and all(isinstance(value, int) and not isinstance(value, bool) for value in preview_ids)
                ) if isinstance(preview_items, list) else False
                REPORT["preview_selection_observations"].append({
                    "item_count": len(preview_items) if isinstance(preview_items, list) else None,
                    "unique_product_count": len(set(preview_ids)) if valid_preview_ids else None,
                    "product_id_fingerprint": _id_set_fingerprint(preview_ids) if valid_preview_ids else None,
                })
                if any(
                    isinstance(item, dict)
                    and isinstance(item.get("changes"), dict)
                    and item["changes"].get("description") == {"mode": "override", "value": ""}
                    for item in preview_items
                ):
                    REPORT["synthetic_actions"]["empty_description_override_requests"] += 1
            except (TypeError, ValueError):
                REPORT["unexpected_http_requests"].append({"method": request.method, "path": parsed.path, "body": "invalid_synthetic_preview_json"})
        elif request.method == "POST" and parsed.path == "/api/my-products/common-content/apply":
            REPORT["synthetic_actions"]["apply_requests"] += 1
            REPORT["writes"].append({"method": "POST", "path": parsed.path, "kind": "synthetic_apply"})
        else:
            REPORT["unexpected_http_requests"].append({"method": request.method, "path": parsed.path})
            route.abort()
            return
        if parsed.path == "/favicon.ico":
            route.fulfill(status=204, body=b"")
            return
        response = route.fetch(max_redirects=0)
        if response.status >= 400:
            rejection_key = (
                request.method, parsed.path, response.status, bool(parsed.query),
            )
            if EXPECTED_HTTP_REJECTIONS.get(rejection_key, 0) > 0:
                try:
                    rejection_body = response.json()
                    rejection_code = rejection_body.get("code") if isinstance(rejection_body, dict) else None
                except Exception:
                    rejection_code = None
                receipt = {
                    "method": request.method,
                    "path": parsed.path,
                    "status": response.status,
                    "code": rejection_code,
                    "has_query": bool(parsed.query),
                    "has_fragment": bool(parsed.fragment),
                }
                expected_receipt_key = (
                    request.method, parsed.path, response.status,
                    rejection_code, bool(parsed.query),
                )
                if (
                    rejection_code == "too_many_items"
                    and not parsed.fragment
                    and EXPECTED_REJECTION_CONSOLE_COUNTS.get(expected_receipt_key, 0) > 0
                ):
                    EXPECTED_HTTP_REJECTIONS[rejection_key] -= 1
                    REPORT["expected_http_rejections"].append(receipt)
                    PENDING_EXPECTED_REJECTION_CONSOLES.append(receipt)
                else:
                    REPORT["unexpected_http_requests"].append({
                        "method": request.method,
                        "path": parsed.path,
                        "status": response.status,
                    })
            elif (
                request.method == "POST"
                and response.status == 409
                and EXPECTED_CONFLICTS.get(parsed.path, [0])[0] > 0
            ):
                EXPECTED_CONFLICTS[parsed.path][0] -= 1
                conflict_key = "expected_apply_conflicts" if parsed.path.endswith("/apply") else "expected_preview_conflicts"
                REPORT["synthetic_actions"][conflict_key] += 1
                PENDING_EXPECTED_CONFLICT_CONSOLES.append({
                    "method": request.method,
                    "path": parsed.path,
                    "status": response.status,
                })
            else:
                REPORT["unexpected_http_requests"].append({"method": request.method, "path": parsed.path, "status": response.status})
        elif request.method == "POST" and parsed.path == "/api/my-products/common-content/apply":
            try:
                apply_body = response.json()
                applied = apply_body.get("applied", []) if isinstance(apply_body, dict) else []
                REPORT["apply_result_counts"].append(
                    len(applied) if isinstance(applied, list) else None
                )
                applied_ids = [
                    item.get("product_id") for item in applied
                    if isinstance(item, dict)
                ] if isinstance(applied, list) else []
                valid_applied_ids = (
                    len(applied_ids) == len(applied)
                    and all(isinstance(value, int) and not isinstance(value, bool) for value in applied_ids)
                ) if isinstance(applied, list) else False
                REPORT["apply_result_observations"].append({
                    "item_count": len(applied) if isinstance(applied, list) else None,
                    "unique_product_count": len(set(applied_ids)) if valid_applied_ids else None,
                    "product_id_fingerprint": _id_set_fingerprint(applied_ids) if valid_applied_ids else None,
                })
            except Exception:
                REPORT["apply_result_counts"].append(None)
        route.fulfill(response=response)
        return

    asset_url = request.url.split("?", 1)[0]
    asset = (
        ASSETS_BY_URL.get(request.url)
        or ASSETS_BY_URL.get(asset_url)
        or ASSETS_BY_URL.get(asset_url.rstrip("/"))
    )
    if asset:
        route.fulfill(
            status=200,
            body=asset["payload"],
            content_type=asset["content_type"],
            headers={"access-control-allow-origin": "*"},
        )
        return
    REPORT["unexpected_external_requests"].append({
        "host": parsed.hostname,
        "path": parsed.path,
        "method": request.method,
    })
    route.abort()


def query_ids(url: str) -> list[int]:
    values = parse_qs(urlsplit(url).query).get("product_id", [])
    return [int(value) for value in values]


def _id_set_fingerprint(values: list[int]) -> str:
    canonical = ",".join(str(value) for value in sorted(values))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _model_snapshot(model) -> tuple:
    """Capture all scalar columns for synthetic no-write assertions."""
    return tuple(
        tuple((column.name, getattr(row, column.name)) for column in model.__table__.columns)
        for row in model.query.order_by(model.id.asc()).all()
    )


def _bulk_product_snapshot(product_ids: list[int]) -> dict[int, dict]:
    products = ImportedProduct.query.filter(
        ImportedProduct.id.in_(product_ids),
    ).order_by(ImportedProduct.id.asc()).all()
    return {
        product.id: {
            column.name: getattr(product, column.name)
            for column in ImportedProduct.__table__.columns
        }
        for product in products
    }


def _bulk_audit_snapshot(product_ids: list[int]) -> tuple:
    rows = AgentChangeSnapshot.query.filter(
        AgentChangeSnapshot.imported_product_id.in_(product_ids),
    ).order_by(AgentChangeSnapshot.id.asc()).all()
    return tuple(
        tuple((column.name, getattr(row, column.name)) for column in AgentChangeSnapshot.__table__.columns)
        for row in rows
    )


def _seed_bulk_fixture() -> list[int]:
    """Add 51 independent source-backed local products after the main fixture."""
    with app.app_context():
        products = []
        for index in range(1, 52):
            title = f"Синтетический товар массового теста {index:02d}"
            description = f"Исходное описание массового товара {index:02d}."
            characteristics = [{"name": "Материал", "value": "хлопок"}]
            source = {
                "title": title,
                "description": description,
                "photo_urls": [],
                "characteristics": characteristics,
            }
            products.append(ImportedProduct(
                seller_id=FIXTURE["seller_id"],
                external_id=f"COMMON-CONTENT-BULK-{index:02d}",
                source_type="synthetic_bulk",
                title=title,
                description=description,
                photo_urls="[]",
                characteristics=json.dumps(characteristics, ensure_ascii=False),
                original_data=json.dumps(source, ensure_ascii=False, sort_keys=True),
            ))
        db.session.add_all(products)
        db.session.commit()
        return [product.id for product in products]


def _edit_bulk_titles(page, product_ids: list[int]) -> None:
    """Make an explicit title override for every selected product via the UI."""
    for index, product_id in enumerate(product_ids, start=1):
        page.locator(
            '#common-content-product-list button[data-product-id="{}"]'.format(product_id)
        ).click()
        title_section = page.locator('section[data-field-section="title"]')
        title_section.get_by_role("button", name="Изменить значение", exact=True).click()
        page.get_by_role("textbox", name="Общее название товара").fill(
            f"Ручное название массового товара {index:02d}"
        )


def _install_synthetic_manual_drift(product_id: int, user_id: int) -> dict:
    """Represent one concurrent seller edit after a reviewed 50-item preview."""
    with app.app_context():
        product = db.session.get(ImportedProduct, product_id)
        before = {
            "title": product.title,
            "description": product.description,
            "photo_urls": product.photo_urls,
            "characteristics": product.characteristics,
            "original_data": product.original_data,
            "content_overrides_json": product.content_overrides_json,
            "content_edit_version": int(product.content_edit_version or 1),
        }
        version = before["content_edit_version"] + 1
        manual_description = "Параллельная ручная правка описания после предпросмотра."
        inherited_description = json.loads(product.original_data)["description"]
        overrides = {
            "schema_version": 1,
            "fields": {
                "description": {
                    "value": manual_description,
                    "inherited_value": inherited_description,
                    "inherited_origin": "source",
                    "edited_by_user_id": user_id,
                    "edited_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "edit_version": version,
                },
            },
        }
        product.description = manual_description
        product.content_overrides_json = json.dumps(
            overrides, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        product.content_edit_version = version
        db.session.commit()
        return {
            "before": before,
            "description": manual_description,
            "version": version,
            "overrides": overrides,
        }


def _console_location_metadata(url) -> dict:
    metadata = {
        "origin": "",
        "path": "",
        "has_query": False,
        "has_fragment": False,
        "url_present": isinstance(url, str) and bool(url),
        "url_too_long": isinstance(url, str) and len(url) > 2048,
    }
    if not isinstance(url, str) or not url or len(url) > 2048:
        return metadata
    try:
        parsed = urlsplit(url)
    except ValueError:
        return metadata
    if parsed.scheme and parsed.netloc:
        metadata["origin"] = (parsed.scheme + "://" + parsed.netloc)[:256]
    metadata["path"] = parsed.path[:256]
    metadata["has_query"] = bool(parsed.query)
    metadata["has_fragment"] = bool(parsed.fragment)
    return metadata


def record_console_message(message) -> None:
    if message.type != "error":
        return
    text = message.text[:300]
    message_location = message.location
    location_url = message_location.get("url") if isinstance(message_location, dict) else None
    location = _console_location_metadata(location_url)
    base = urlsplit(BASE)
    expected_origin = base.scheme + "://" + base.netloc
    exact_local_endpoint = (
        not location["url_too_long"]
        and location["origin"] == expected_origin
        and location["path"] in EXPECTED_CONFLICT_CONSOLE_COUNTS
        and not location["has_query"]
        and not location["has_fragment"]
    )
    pending_index = None
    if exact_local_endpoint:
        pending_index = next((
            index for index, response in enumerate(PENDING_EXPECTED_CONFLICT_CONSOLES)
            if response.get("method") == "POST"
            and response.get("path") == location["path"]
            and response.get("status") == 409
        ), None)
    if (
        text == EXPECTED_CONFLICT_CONSOLE_MESSAGE
        and pending_index is not None
    ):
        exact_response = PENDING_EXPECTED_CONFLICT_CONSOLES.pop(pending_index)
        REPORT["expected_conflict_console_errors"].append({
            **exact_response,
            "message": text,
            "location": location,
        })
        return
    if EXPECTED_413_RESOURCE_ERROR.fullmatch(text):
        rejection_pending_index = None
        if (
            not location["url_too_long"]
            and location["origin"] == expected_origin
            and not location["has_fragment"]
        ):
            rejection_pending_index = next((
                index for index, response in enumerate(PENDING_EXPECTED_REJECTION_CONSOLES)
                if response.get("status") == 413
                and response.get("code") == "too_many_items"
                and response.get("path") == location["path"]
                and response.get("has_query") == location["has_query"]
                and response.get("has_fragment") is False
                and EXPECTED_REJECTION_CONSOLE_COUNTS.get((
                    response.get("method"),
                    response.get("path"),
                    response.get("status"),
                    response.get("code"),
                    response.get("has_query"),
                ), 0) > 0
            ), None)
        if rejection_pending_index is not None:
            receipt = PENDING_EXPECTED_REJECTION_CONSOLES.pop(rejection_pending_index)
            REPORT["expected_rejection_console_errors"].append({
                **receipt,
                "message": text,
                "location": location,
            })
            return
    REPORT["console_errors"].append(text)
    REPORT["console_error_locations"].append({
        "message": text,
        "location": location,
        "pending_conflict_endpoints": [
            response.get("path") for response in PENDING_EXPECTED_CONFLICT_CONSOLES
        ][:4],
        "pending_rejection_receipts": [
            {
                "method": response.get("method"),
                "path": response.get("path"),
                "status": response.get("status"),
                "code": response.get("code"),
                "has_query": response.get("has_query"),
            }
            for response in PENDING_EXPECTED_REJECTION_CONSOLES
        ][:4],
    })


def delay_fetches(page, request_keys: list[str]) -> None:
    page.evaluate("""keys => {
        const nativeFetch = window.__commonContentNativeFetch || window.fetch.bind(window);
        window.__commonContentNativeFetch = nativeFetch;
        const targets = new Set(keys);
        window.__commonContentDelayed = {};
        window.__releaseCommonContentRequest = key => {
            const release = window.__commonContentDelayed[key];
            if (!release) throw new Error('No delayed request is waiting: ' + key);
            delete window.__commonContentDelayed[key];
            return release();
        };
        window.fetch = (input, init) => {
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
            const key = method + ' ' + url.pathname;
            if (!targets.has(key)) return nativeFetch(input, init);
            targets.delete(key);
            return new Promise((resolve, reject) => {
                window.__commonContentDelayed[key] = () => nativeFetch(input, init).then(resolve, reject);
            });
        };
    }""", request_keys)


def wait_for_delayed_fetch(page, request_key: str) -> None:
    page.wait_for_function(
        "key => typeof window.__commonContentDelayed?.[key] === 'function'",
        arg=request_key,
        timeout=5000,
    )


def release_delayed_fetch(page, request_key: str) -> None:
    page.evaluate("key => window.__releaseCommonContentRequest(key)", request_key)


def set_theme(page, theme: str):
    page.evaluate("theme => localStorage.setItem('sh-theme', theme)", theme)
    page.reload(wait_until="domcontentloaded")
    page.locator("#common-content-editor").wait_for()
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    actual = page.evaluate("document.documentElement.dataset.theme")
    assert actual == theme, {"expected": theme, "actual": actual}


def layout_case(page, path: str, width: int, theme: str, state: str, catalog_path: str):
    api_reads_before = len(REPORT["browser_api_reads"])
    writes_before = len(REPORT["writes"])
    page.set_viewport_size({"width": width, "height": 1050})
    response = page.goto(BASE + path, wait_until="domcontentloaded")
    assert response and response.status == 200, {"state": state, "path": path, "response": response.status if response else None}
    page.locator("#common-content-editor").wait_for()
    page.wait_for_load_state("networkidle")
    page.evaluate("theme => localStorage.setItem('sh-theme', theme)", theme)
    page.reload(wait_until="domcontentloaded")
    page.locator("#common-content-editor").wait_for()
    page.wait_for_load_state("networkidle")
    metrics = page.evaluate("""() => ({
        width: innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
        theme: document.documentElement.dataset.theme,
        titleVisible: !!document.querySelector('h1')?.textContent.includes('Общий контент товара'),
        effectNoticeVisible: !!document.querySelector('.cpc-effect-note')
            && getComputedStyle(document.querySelector('.cpc-effect-note')).display !== 'none',
        selectedProductCount: document.querySelectorAll('#common-content-product-list li').length,
        emptyHeadingVisible: !!document.querySelector('#cpc-empty-title')
            && getComputedStyle(document.querySelector('#cpc-empty-title')).display !== 'none',
        longTitleWraps: (() => {
            const el = [...document.querySelectorAll('.cpc-product-name')].find(row => row.textContent.startsWith('Длинное название товара'));
            return !!el && getComputedStyle(el).overflowWrap === 'anywhere' && el.scrollWidth <= el.clientWidth + 1;
        })(),
        longExternalIdWraps: (() => {
            const el = [...document.querySelectorAll('.cpc-product-meta')].find(row => row.textContent.includes('COMMON-CONTENT-SECOND'));
            return !!el && getComputedStyle(el).overflowWrap === 'anywhere' && el.scrollWidth <= el.clientWidth + 1;
        })(),
    })""")
    assert metrics["width"] == width and metrics["theme"] == theme, metrics
    assert metrics["scrollWidth"] <= width + 1, metrics
    assert metrics["titleVisible"] and metrics["effectNoticeVisible"], metrics
    if state == "selected":
        assert metrics["selectedProductCount"] == 2, metrics
        assert metrics["longTitleWraps"] and metrics["longExternalIdWraps"], metrics
    elif state == "empty":
        assert metrics["selectedProductCount"] == 0 and metrics["emptyHeadingVisible"], metrics
        link = page.get_by_role("link", name="Открыть мои товары")
        assert link.is_visible()
        href = link.get_attribute("href") or ""
        parsed_link = urlsplit(href)
        assert not parsed_link.scheme and not parsed_link.netloc and parsed_link.path == catalog_path, href
        return_link = page.get_by_role("link", name="Вернуться к товарам", exact=True)
        return_link.focus()
        page.keyboard.press("Tab")
        link_handle = link.element_handle()
        assert link_handle and page.evaluate("target => document.activeElement === target", link_handle)
        REPORT["synthetic_actions"]["empty_state_catalog_link_available"] = True
        empty_page_reads = REPORT["browser_api_reads"][api_reads_before:]
        shared_shell_reads = [
            read for read in empty_page_reads
            if read.get("kind") == "shared_shell_read"
        ]
        non_shell_reads = [
            read for read in empty_page_reads
            if read.get("kind") != "shared_shell_read"
        ]
        REPORT["synthetic_actions"]["empty_route_shared_shell_reads"] += len(shared_shell_reads)
        REPORT["synthetic_actions"]["empty_route_api_reads"] += len(non_shell_reads)
        shell_category_counts = REPORT["synthetic_actions"]["empty_route_shared_shell_read_categories"]
        for read in shared_shell_reads:
            category = read.get("path_category")
            if category in shell_category_counts:
                shell_category_counts[category] += 1
            else:
                # This should be unreachable because bridge() only emits
                # shared_shell_read for the exact allowlist above.
                REPORT["synthetic_actions"]["empty_route_api_reads"] += 1
                REPORT["synthetic_actions"]["empty_route_shared_shell_reads"] -= 1
        REPORT["synthetic_actions"]["empty_route_mutating_requests"] += (
            len(REPORT["writes"]) - writes_before
        )
    else:
        raise AssertionError("Unknown common editor layout state: " + state)
    REPORT["layouts"].append({
        "kind": "empty_editor" if state == "empty" else "selected_editor",
        "state": state,
        "width": width,
        "theme": theme,
        "page_overflow": False,
    })
    if width in (390, 1440):
        name = f"common-content-{state}-{theme}-{width}.png"
        page.screenshot(path=str(OUT / name), full_page=True)
        REPORT["screenshots"].append(name)
    if state == "selected" and width in COMMON_CONTENT_MOBILE_WIDTHS:
        REPORT["mobile_touch_target_observations"].append(
            measure_mobile_touch_targets(page, width, theme)
        )
        REPORT["mobile_product_navigator_observations"].append(
            measure_mobile_product_navigator(page, width, theme)
        )


def measure_mobile_touch_targets(page, width: int, theme: str) -> dict:
    """Capture actual mobile hit areas without sending editor requests."""
    observation = page.evaluate("""() => {
        const originalModes = {};
        for (const field of ['photos', 'characteristics']) {
            const mode = document.querySelector(
                '[data-field-section="' + field + '"] .cpc-mode-button'
            );
            originalModes[field] = mode?.getAttribute('aria-pressed') === 'true';
            if (mode && mode.getAttribute('aria-pressed') !== 'true') mode.click();
        }
        const pageWidthWhileExpanded = Math.max(
            document.documentElement.scrollWidth,
            document.body?.scrollWidth || 0,
        );
        const overflowWhileExpanded = pageWidthWhileExpanded > innerWidth + 1;

        const groups = [
            {name: 'mode_inherit', selector: '.cpc-mode-button'},
            {name: 'quiet', selector: '.cpc-quiet-button'},
            {name: 'characteristic_remove', selector: '.cpc-char-remove'},
            {name: 'photo_arrows', selector: '.cpc-photo-order-actions button'},
        ];
        const targets = groups.map(group => {
            const candidates = [...document.querySelectorAll(group.selector)];
            const enabled = candidates.filter(el => !el.matches(':disabled')
                && el.getAttribute('aria-disabled') !== 'true');
            const samples = enabled.map(el => {
                el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'auto'});
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const visible = el.getClientRects().length > 0
                    && style.display !== 'none' && style.visibility !== 'hidden'
                    && Number(style.opacity || 1) > 0
                    && rect.width > 0 && rect.height > 0
                    && rect.right > 0 && rect.bottom > 0
                    && rect.left < innerWidth && rect.top < innerHeight;
                const inViewport = rect.left >= -1 && rect.top >= -1
                    && rect.right <= innerWidth + 1 && rect.bottom <= innerHeight + 1;
                return {
                    label: el.getAttribute('aria-label') || el.textContent.trim(),
                    enabled: true,
                    visible,
                    in_viewport: inViewport,
                    rect: {
                        x: rect.x, y: rect.y, width: rect.width, height: rect.height,
                        right: rect.right, bottom: rect.bottom,
                    },
                    meets_minimum: visible && inViewport
                        && rect.width >= 44 && rect.height >= 44,
                };
            });
            return {
                name: group.name,
                selector: group.selector,
                candidate_count: candidates.length,
                enabled_count: enabled.length,
                samples,
                passed: samples.length > 0 && samples.every(sample => sample.meets_minimum),
            };
        });
        for (const field of ['photos', 'characteristics']) {
            const mode = document.querySelector(
                '[data-field-section="' + field + '"] .cpc-mode-button'
            );
            if (mode && (mode.getAttribute('aria-pressed') === 'true') !== originalModes[field]) {
                mode.click();
            }
        }
        const root = document.documentElement;
        const pageWidthAfterRestore = Math.max(root.scrollWidth, document.body?.scrollWidth || 0);
        const overflowAfterRestore = pageWidthAfterRestore > innerWidth + 1;
        const pageWidth = Math.max(pageWidthWhileExpanded, pageWidthAfterRestore);
        const pageOverflow = overflowWhileExpanded || overflowAfterRestore;
        targets.forEach(group => {
            group.samples.forEach(sample => { sample.meets_minimum = sample.meets_minimum && !pageOverflow; });
            group.passed = group.samples.length > 0 && group.samples.every(sample => sample.meets_minimum);
        });
        return {
            viewport_width: innerWidth,
            page_width: pageWidth,
            page_overflow: pageOverflow,
            page_width_while_expanded: pageWidthWhileExpanded,
            page_overflow_while_expanded: overflowWhileExpanded,
            page_width_after_restore: pageWidthAfterRestore,
            page_overflow_after_restore: overflowAfterRestore,
            local_modes_restored: ['photos', 'characteristics'].every(field => {
                const mode = document.querySelector(
                    '[data-field-section="' + field + '"] .cpc-mode-button'
                );
                return !!mode && (mode.getAttribute('aria-pressed') === 'true') === originalModes[field];
            }),
            targets,
        };
    }""")
    row = {
        "state": "selected",
        "width": width,
        "theme": theme,
        "page_overflow": observation["page_overflow"],
        "viewport_width": observation["viewport_width"],
        "page_width": observation["page_width"],
        "page_width_while_expanded": observation["page_width_while_expanded"],
        "page_overflow_while_expanded": observation["page_overflow_while_expanded"],
        "page_width_after_restore": observation["page_width_after_restore"],
        "page_overflow_after_restore": observation["page_overflow_after_restore"],
        "local_modes_restored": observation["local_modes_restored"],
        "targets": observation["targets"],
    }
    row["passed"] = (
        row["viewport_width"] == width
        and row["page_overflow"] is False
        and row["local_modes_restored"] is True
        and len(row["targets"]) == 4
        and all(target["passed"] for target in row["targets"])
    )
    return row


def wait_for_navigator_focus_geometry(page, product_id: str) -> bool:
    try:
        page.wait_for_function("""productId => {
            const list = document.querySelector('#common-content-product-list');
            const active = document.activeElement;
            const box = active?.getBoundingClientRect();
            const listBox = list?.getBoundingClientRect();
            const style = active ? getComputedStyle(active) : null;
            if (!list || !active || active.dataset.productId !== productId
                    || !box || !listBox || !style || !active.matches(':focus-visible')) return false;
            const width = Number.parseFloat(style.outlineWidth || '0') || 0;
            const offset = Math.max(0, Number.parseFloat(style.outlineOffset || '0') || 0);
            const extent = width + offset;
            const left = box.left - extent;
            const right = box.right + extent;
            const top = box.top - extent;
            const bottom = box.bottom + extent;
            const clipLeft = listBox.left + list.clientLeft;
            const clipTop = listBox.top + list.clientTop;
            const clipRight = clipLeft + list.clientWidth;
            const clipBottom = clipTop + list.clientHeight;
            return style.outlineStyle !== 'none' && style.outlineStyle !== 'hidden'
                && width > 0 && style.outlineColor !== 'transparent'
                && left >= clipLeft - 0.5 && right <= clipRight + 0.5
                && top >= clipTop - 0.5 && bottom <= clipBottom + 0.5
                && left >= 0 && top >= 0 && right <= innerWidth && bottom <= innerHeight;
        }""", arg=product_id, timeout=600)
        return True
    except PlaywrightTimeoutError:
        return False


def measure_mobile_product_navigator(page, width: int, theme: str) -> dict:
    """Check the bounded two-card strip and keyboard access without selecting either item."""
    first_id = str(FIXTURE["source_id"])
    second_id = str(FIXTURE["second_source_id"])
    api_reads_before = len(REPORT["browser_api_reads"])
    writes_before = len(REPORT["writes"])
    preview_requests_before = REPORT["synthetic_actions"]["preview_requests"]
    apply_requests_before = REPORT["synthetic_actions"]["apply_requests"]
    page.evaluate("""() => {
        window.scrollTo(0, 0);
        const list = document.querySelector('#common-content-product-list');
        if (list) list.scrollLeft = 0;
    }""")
    before = page.evaluate("""({firstId, secondId}) => {
        const root = document.documentElement;
        const list = document.querySelector('#common-content-product-list');
        const selection = document.querySelector('.cpc-selection');
        const buttons = [...(list?.querySelectorAll('button[data-action="choose-product"]') || [])];
        const first = buttons.find(button => button.dataset.productId === firstId);
        const second = buttons.find(button => button.dataset.productId === secondId);
        const rect = element => {
            if (!element) return null;
            const box = element.getBoundingClientRect();
            return {left: box.left, right: box.right, top: box.top, bottom: box.bottom,
                width: box.width, height: box.height};
        };
        const selectedHeading = document.querySelector('.cpc-product-heading h2');
        const selectedMeta = document.querySelector('.cpc-product-heading p:not(.cpc-source-line)');
        const titleInput = document.querySelector('[data-field-input="title"]');
        const listStyle = list ? getComputedStyle(list) : null;
        return {
            cardCount: buttons.length,
            firstId: first?.dataset.productId || null,
            secondId: second?.dataset.productId || null,
            selectionHeight: rect(selection)?.height || 0,
            cardWidths: buttons.map(button => rect(button)?.width || 0),
            cardHeights: buttons.map(button => rect(button)?.height || 0),
            listClientWidth: list?.clientWidth || 0,
            listScrollWidth: list?.scrollWidth || 0,
            listScrollLeft: list?.scrollLeft || 0,
            listOverflowX: listStyle?.overflowX || '',
            secondNameText: second?.querySelector('.cpc-product-name')?.textContent || null,
            secondSkuText: second?.querySelector('.cpc-product-meta')?.textContent || null,
            secondAccessibleLabel: second?.getAttribute('aria-label') || null,
            secondTitleTooltip: second?.getAttribute('title') || null,
            firstCurrent: first?.getAttribute('aria-current') === 'true',
            secondCurrent: second?.getAttribute('aria-current') === 'true',
            headingTitle: selectedHeading?.textContent || null,
            headingTitleVisible: !!selectedHeading && selectedHeading.getClientRects().length > 0,
            headingMeta: selectedMeta?.textContent || null,
            headingMetaVisible: !!selectedMeta && selectedMeta.getClientRects().length > 0,
            titleInputValue: titleInput?.value || null,
            titleInputVisible: !!titleInput && titleInput.getClientRects().length > 0,
            pageWidth: Math.max(root.scrollWidth, document.body?.scrollWidth || 0),
            pageX: window.scrollX,
            pageY: window.scrollY,
        };
    }""", {"firstId": first_id, "secondId": second_id})

    first = page.locator(
        '#common-content-product-list button[data-product-id="{}"]'.format(first_id)
    )
    second = page.locator(
        '#common-content-product-list button[data-product-id="{}"]'.format(second_id)
    )
    assert first.count() == 1 and second.count() == 1, before
    first.focus()
    page.keyboard.press("Tab")
    second_focus_wait_completed = wait_for_navigator_focus_geometry(page, second_id)
    after_tab = page.evaluate("""() => {
        const list = document.querySelector('#common-content-product-list');
        const active = document.activeElement;
        const box = active?.getBoundingClientRect();
        const listBox = list?.getBoundingClientRect();
        const style = active ? getComputedStyle(active) : null;
        const outlineWidth = Number.parseFloat(style?.outlineWidth || '0') || 0;
        const outlineOffset = Math.max(0, Number.parseFloat(style?.outlineOffset || '0') || 0);
        const expansion = outlineWidth + outlineOffset;
        const focusRing = box ? {
            left: box.left - expansion, top: box.top - expansion,
            right: box.right + expansion, bottom: box.bottom + expansion,
        } : null;
        const scrollport = list && listBox ? {
            left: listBox.left + list.clientLeft,
            top: listBox.top + list.clientTop,
            right: listBox.left + list.clientLeft + list.clientWidth,
            bottom: listBox.top + list.clientTop + list.clientHeight,
        } : null;
        const outlineVisible = !!style && style.outlineStyle !== 'none'
            && style.outlineStyle !== 'hidden' && outlineWidth > 0
            && style.outlineColor !== 'transparent';
        const outlineWithinScrollport = !!focusRing && !!scrollport && outlineVisible
            && focusRing.left >= scrollport.left - 0.5
            && focusRing.right <= scrollport.right + 0.5
            && focusRing.top >= scrollport.top - 0.5
            && focusRing.bottom <= scrollport.bottom + 0.5;
        return {
            activeId: active?.dataset.productId || null,
            focusVisible: !!active && active.matches(':focus-visible'),
            focusVisibleOnScreen: !!focusRing && focusRing.left >= 0 && focusRing.top >= 0
                && focusRing.right <= innerWidth && focusRing.bottom <= innerHeight,
            outlineVisible,
            outlineWithinScrollport,
            focusRing,
            scrollport,
            listScrollLeft: list?.scrollLeft || 0,
            pageX: window.scrollX,
            pageY: window.scrollY,
        };
    }""")
    reached_second = after_tab["activeId"] == second_id
    returned_first = False
    after_return = after_tab
    first_focus_wait_completed = False
    if reached_second:
        page.keyboard.press("Shift+Tab")
        first_focus_wait_completed = wait_for_navigator_focus_geometry(page, first_id)
        after_return = page.evaluate("""() => {
            const list = document.querySelector('#common-content-product-list');
            const active = document.activeElement;
            const box = active?.getBoundingClientRect();
            const listBox = list?.getBoundingClientRect();
            const style = active ? getComputedStyle(active) : null;
            const outlineWidth = Number.parseFloat(style?.outlineWidth || '0') || 0;
            const outlineOffset = Math.max(0, Number.parseFloat(style?.outlineOffset || '0') || 0);
            const expansion = outlineWidth + outlineOffset;
            const focusRing = box ? {
                left: box.left - expansion, top: box.top - expansion,
                right: box.right + expansion, bottom: box.bottom + expansion,
            } : null;
            const scrollport = list && listBox ? {
                left: listBox.left + list.clientLeft,
                top: listBox.top + list.clientTop,
                right: listBox.left + list.clientLeft + list.clientWidth,
                bottom: listBox.top + list.clientTop + list.clientHeight,
            } : null;
            const outlineVisible = !!style && style.outlineStyle !== 'none'
                && style.outlineStyle !== 'hidden' && outlineWidth > 0
                && style.outlineColor !== 'transparent';
            const outlineWithinScrollport = !!focusRing && !!scrollport && outlineVisible
                && focusRing.left >= scrollport.left - 0.5
                && focusRing.right <= scrollport.right + 0.5
                && focusRing.top >= scrollport.top - 0.5
                && focusRing.bottom <= scrollport.bottom + 0.5;
            return {
                activeId: active?.dataset.productId || null,
                focusVisible: !!active && active.matches(':focus-visible'),
                outlineVisible,
                outlineWithinScrollport,
                focusRing,
                scrollport,
                pageX: window.scrollX,
                pageY: window.scrollY,
            };
        }""")
        returned_first = after_return["activeId"] == first_id
    if not returned_first:
        first.focus()

    after = page.evaluate("""({firstId, secondId}) => {
        const root = document.documentElement;
        const list = document.querySelector('#common-content-product-list');
        const buttons = [...(list?.querySelectorAll('button[data-action="choose-product"]') || [])];
        const first = buttons.find(button => button.dataset.productId === firstId);
        const second = buttons.find(button => button.dataset.productId === secondId);
        return {
            pageWidth: Math.max(root.scrollWidth, document.body?.scrollWidth || 0),
            pageX: window.scrollX,
            pageY: window.scrollY,
            firstCurrent: first?.getAttribute('aria-current') === 'true',
            secondCurrent: second?.getAttribute('aria-current') === 'true',
        };
    }""", {"firstId": first_id, "secondId": second_id})
    expected_accessible_label = (
        FIXTURE["second_title"] + " · Артикул " + FIXTURE["second_external_id"]
    )
    accessible_button = page.get_by_role("button", name=expected_accessible_label, exact=True)
    product_reads_during = sum(
        row.get("kind") == "product_read"
        for row in REPORT["browser_api_reads"][api_reads_before:]
    )
    writes_during = len(REPORT["writes"]) - writes_before
    synthetic_writes_during = (
        REPORT["synthetic_actions"]["preview_requests"] - preview_requests_before
        + REPORT["synthetic_actions"]["apply_requests"] - apply_requests_before
    )
    measured = {
        "state": "selected",
        "width": width,
        "theme": theme,
        "card_count": before["cardCount"],
        "selection_height_px": before["selectionHeight"],
        "card_widths_px": before["cardWidths"],
        "card_button_heights_px": before["cardHeights"],
        "local_horizontal_scroll": (
            before["listScrollWidth"] > before["listClientWidth"]
            and before["listOverflowX"] in ("auto", "scroll")
        ),
        "local_scroll_after_tab_px": after_tab["listScrollLeft"],
        "page_overflow": after["pageWidth"] > width + 1,
        "page_scroll_stable": all(
            row["pageX"] == before["pageX"] and row["pageY"] == before["pageY"]
            for row in (after_tab, after_return, after)
        ),
        "keyboard_reached_second_by_tab": reached_second,
        "keyboard_returned_first_by_shift_tab": returned_first,
        "second_focus_visible": after_tab["focusVisible"] and after_tab["outlineVisible"]
            and after_tab["focusVisibleOnScreen"],
        "first_focus_visible": after_return["focusVisible"] and after_return["outlineVisible"]
            and after_return["outlineWithinScrollport"],
        "second_focus_geometry_wait_completed": second_focus_wait_completed,
        "first_focus_geometry_wait_completed": first_focus_wait_completed,
        "second_focus_outline_within_scrollport": after_tab["outlineWithinScrollport"],
        "first_focus_outline_within_scrollport": after_return["outlineWithinScrollport"],
        "second_focus_ring_geometry": {
            "focus_ring": after_tab["focusRing"], "scrollport": after_tab["scrollport"],
        },
        "first_focus_ring_geometry": {
            "focus_ring": after_return["focusRing"], "scrollport": after_return["scrollport"],
        },
        "current_product_preserved": after["firstCurrent"] and not after["secondCurrent"],
        "full_second_title_dom": before["secondNameText"] == FIXTURE["second_title"],
        "full_second_sku_dom": before["secondSkuText"] == "Артикул " + FIXTURE["second_external_id"],
        "full_second_accessible_name": before["secondAccessibleLabel"] == expected_accessible_label
            and accessible_button.count() == 1,
        "full_second_title_tooltip": before["secondTitleTooltip"] == expected_accessible_label,
        "full_second_external_id_within_model_limit": (
            len(FIXTURE["second_external_id"]) <= FIXTURE["second_external_id_limit"]
        ),
        "full_current_heading": before["headingTitle"] == FIXTURE["initial_title"]
            and before["headingTitleVisible"]
            and before["headingMetaVisible"]
            and FIXTURE["initial_external_id"] in (before["headingMeta"] or "")
            and before["titleInputValue"] == FIXTURE["initial_title"]
            and before["titleInputVisible"],
        "product_api_reads_during": product_reads_during,
        "mutating_requests_during": writes_during,
        "preview_apply_requests_during": synthetic_writes_during,
        "no_product_api_reads": product_reads_during == 0,
        "no_mutating_requests": writes_during == 0 and synthetic_writes_during == 0,
    }
    measured["passed"] = (
        measured["state"] == "selected"
        and measured["card_count"] == 2
        and measured["selection_height_px"] <= 220
        and len(measured["card_widths_px"]) == 2
        and abs(measured["card_widths_px"][0] - measured["card_widths_px"][1]) <= 1
        and len(measured["card_button_heights_px"]) == 2
        and all(height >= 44 for height in measured["card_button_heights_px"])
        and measured["local_horizontal_scroll"]
        and measured["local_scroll_after_tab_px"] > before["listScrollLeft"]
        and not measured["page_overflow"]
        and measured["page_scroll_stable"]
        and measured["keyboard_reached_second_by_tab"]
        and measured["keyboard_returned_first_by_shift_tab"]
        and measured["second_focus_visible"]
        and measured["first_focus_visible"]
        and measured["second_focus_geometry_wait_completed"]
        and measured["first_focus_geometry_wait_completed"]
        and measured["second_focus_outline_within_scrollport"]
        and measured["first_focus_outline_within_scrollport"]
        and measured["current_product_preserved"]
        and measured["full_second_title_dom"]
        and measured["full_second_sku_dom"]
        and measured["full_second_accessible_name"]
        and measured["full_second_title_tooltip"]
        and measured["full_second_external_id_within_model_limit"]
        and measured["full_current_heading"]
        and measured["no_product_api_reads"]
        and measured["no_mutating_requests"]
    )
    return measured


def assert_mobile_touch_target_evidence() -> None:
    rows = REPORT["mobile_touch_target_observations"]
    expected = {
        (width, theme)
        for width in COMMON_CONTENT_MOBILE_WIDTHS
        for theme in COMMON_CONTENT_MOBILE_THEMES
    }
    actual = [(row.get("width"), row.get("theme")) for row in rows]
    passed = (
        len(rows) == len(expected)
        and len(set(actual)) == len(expected)
        and set(actual) == expected
        and all(row.get("passed") is True for row in rows)
    )
    check = {
        "name": "common_mobile_touch_targets_44px",
        "status": "passed" if passed else "failed",
        "ok": passed,
        "passed": passed,
        "layouts": len(rows),
        "expected_layouts": len(expected),
        "failed_layouts": [
            {"width": row.get("width"), "theme": row.get("theme"), "targets": [
                target.get("name") for target in row.get("targets", [])
                if target.get("passed") is not True
            ]}
            for row in rows if row.get("passed") is not True
        ],
    }
    REPORT["checks"].append(check)
    if not passed:
        raise AssertionError({"check": check, "observations": rows})


def assert_mobile_product_navigator_evidence() -> None:
    rows = REPORT["mobile_product_navigator_observations"]
    expected = {
        (width, theme)
        for width in COMMON_CONTENT_MOBILE_WIDTHS
        for theme in COMMON_CONTENT_MOBILE_THEMES
    }
    actual = [(row.get("width"), row.get("theme")) for row in rows]
    passed = (
        len(rows) == len(expected)
        and len(set(actual)) == len(expected)
        and set(actual) == expected
        and all(row.get("passed") is True for row in rows)
    )
    check = {
        "name": COMMON_CONTENT_NAVIGATOR_CHECK,
        "status": "passed" if passed else "failed",
        "ok": passed,
        "passed": passed,
        "layouts": len(rows),
        "expected_layouts": len(expected),
        "failed_layouts": [
            {"width": row.get("width"), "theme": row.get("theme")}
            for row in rows if row.get("passed") is not True
        ],
    }
    REPORT["checks"].append(check)
    if not passed:
        raise AssertionError({"check": check, "observations": rows})


def record_keyboard_focus(page, check: str, selector: str, *, photo_url: str | None = None,
                         direction: str | None = None, preview_trigger: bool = False) -> None:
    observation = page.evaluate("""selector => {
        const target = document.querySelector(selector);
        const active = document.activeElement;
        if (!target || !active) return {observed: false};
        const style = getComputedStyle(active);
        const rect = active.getBoundingClientRect();
        const outlineColor = style.outlineColor;
        const outlineAlpha = outlineColor.startsWith('rgba(')
            ? Number(outlineColor.slice(outlineColor.lastIndexOf(',') + 1).replace(')', '').trim())
            : 1;
        return {
            observed: true,
            target_supported: active === target,
            enabled: active instanceof HTMLButtonElement ? !active.disabled : !active.matches(':disabled'),
            visible: active.getClientRects().length > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && Number(style.opacity || 1) > 0,
            focus_visible: active.matches(':focus-visible'),
            outline_visible: style.outlineStyle !== 'none'
                && style.outlineStyle !== 'hidden'
                && Number.parseFloat(style.outlineWidth) > 0
                && outlineColor !== 'transparent' && outlineAlpha > 0,
            outline: {
                style: style.outlineStyle,
                width: Number.parseFloat(style.outlineWidth),
                color: style.outlineColor,
            },
            rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
            viewport: {width: innerWidth, height: innerHeight},
            photo_url: active.dataset.photoUrl || null,
            direction: active.dataset.direction || null,
            action: active.dataset.action || null,
        };
    }""", selector)
    assert observation.get("observed"), {"check": check, "observation": observation}
    row = {
        "check": check,
        "observed": observation["observed"],
        "target_supported": observation["target_supported"],
        "enabled": observation["enabled"],
        "visible": observation["visible"],
        "focus_visible": observation["focus_visible"],
        "outline_visible": observation["outline_visible"],
        "outline": observation["outline"],
        "rect": observation["rect"],
        "viewport": observation["viewport"],
        "focused_photo_url": observation["photo_url"],
        "focused_direction": observation["direction"],
        "focused_action": observation["action"],
    }
    if photo_url is not None:
        row.update({
            "same_photo": observation["photo_url"] == photo_url,
            "target": {"photo_url": photo_url, "direction": direction},
        })
    if preview_trigger:
        row.update({
            "target_action": observation["action"],
            "same_trigger": observation["action"] == "preview",
        })
    assert row["target_supported"] and row["enabled"] and row["visible"], {"check": check, "row": row}
    assert row["focus_visible"] and row["outline_visible"], {"check": check, "row": row}
    rect = row["rect"]
    viewport = row["viewport"]
    assert (
        rect["x"] >= 0 and rect["y"] >= 0 and rect["width"] > 0 and rect["height"] > 0
        and rect["x"] + rect["width"] <= viewport["width"] + 1
        and rect["y"] + rect["height"] <= viewport["height"] + 1
    ), {"check": check, "row": row}
    REPORT["focus_observations"].append(row)


def _run_bulk_50_case(page) -> None:
    """Exercise the real editor's 50-item limit, CAS recovery, and local audit."""
    all_ids = _seed_bulk_fixture()
    product_ids = all_ids[:50]
    overflow_id = all_ids[50]
    query50 = "&".join("product_id=" + str(value) for value in product_ids)
    query51 = "&".join("product_id=" + str(value) for value in all_ids)
    editor_path = "/my-products/common-content?" + query50
    REPORT["bulk_50"]["selected_products"] = len(product_ids)
    REPORT["bulk_50"]["selected_product_id_fingerprint"] = _id_set_fingerprint(product_ids)

    with app.app_context():
        db.session.expire_all()
        initial_products = _bulk_product_snapshot(all_ids)
        initial_audits = _bulk_audit_snapshot(all_ids)
        initial_drafts = _model_snapshot(MarketplaceProductDraft)
        initial_listings = _model_snapshot(MarketplaceListing)
        initial_operations = _model_snapshot(MarketplaceOperation)
    assert set(initial_products) == set(all_ids)
    assert len(product_ids) == 50 and overflow_id not in product_ids

    # The page selection and the preview API each reject 51 before any local or
    # channel write. Fetch from the authenticated browser so both use real
    # routes, cookies, and (for the POST) the real CSRF token.
    page_url_rejection_key = ("GET", "/my-products/common-content", 413, True)
    EXPECTED_HTTP_REJECTIONS[page_url_rejection_key] = (
        EXPECTED_HTTP_REJECTIONS.get(page_url_rejection_key, 0) + 1
    )
    url_rejection = page.evaluate("""async path => {
        const response = await fetch(path, {credentials: 'same-origin'});
        let body = {};
        try { body = await response.json(); } catch (_) {}
        return {status: response.status, code: body.code || null};
    }""", "/my-products/common-content?" + query51)
    assert url_rejection == {"status": 413, "code": "too_many_items"}, url_rejection
    REPORT["bulk_50"]["page_51_rejected"] = True

    opened = page.goto(BASE + editor_path, wait_until="domcontentloaded")
    assert opened and opened.status == 200
    page.wait_for_load_state("networkidle")
    assert query_ids(page.url) == product_ids
    assert page.locator(".cpc-layout").get_attribute("data-product-count") == "50"
    bootstrap = json.loads(page.locator("#common-content-bootstrap").text_content())
    invalid_51_items = [{
        "product_id": product_id,
        "expected_content_edit_version": initial_products[product_id]["content_edit_version"],
        "changes": {"title": {"mode": "override", "value": f"Отклонённое название {index:02d}"}},
        "recipients": [],
    } for index, product_id in enumerate(all_ids, start=1)]
    preview_rejection_key = (
        "POST", "/api/my-products/common-content/preview", 413, False,
    )
    EXPECTED_HTTP_REJECTIONS[preview_rejection_key] = (
        EXPECTED_HTTP_REJECTIONS.get(preview_rejection_key, 0) + 1
    )
    api_rejection = page.evaluate("""async ({items, csrfToken}) => {
        const response = await fetch('/api/my-products/common-content/preview', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
            },
            body: JSON.stringify({items}),
        });
        let body = {};
        try { body = await response.json(); } catch (_) {}
        return {status: response.status, code: body.code || null};
    }""", {"items": invalid_51_items, "csrfToken": bootstrap["csrfToken"]})
    assert api_rejection == {"status": 413, "code": "too_many_items"}, api_rejection
    REPORT["bulk_50"]["preview_api_51_rejected"] = True
    api_preview_observation = REPORT["preview_selection_observations"][-1]
    assert api_preview_observation == {
        "item_count": 51,
        "unique_product_count": 51,
        "product_id_fingerprint": _id_set_fingerprint(all_ids),
    }, api_preview_observation
    with app.app_context():
        db.session.expire_all()
        assert _bulk_product_snapshot(all_ids) == initial_products
        assert _bulk_audit_snapshot(all_ids) == initial_audits
        assert _model_snapshot(MarketplaceProductDraft) == initial_drafts
        assert _model_snapshot(MarketplaceListing) == initial_listings
        assert _model_snapshot(MarketplaceOperation) == initial_operations
    REPORT["checks"].append(
        "common_51_selection_and_csrf_preview_rejected_without_mutation_or_publication"
    )

    # The 50th card must remain reachable by ordinary Tab navigation and Enter.
    choices = page.locator(
        '#common-content-product-list button[data-action="choose-product"]'
    )
    assert choices.count() == 50
    choices.first.focus()
    for _ in range(49):
        page.keyboard.press("Tab")
    last_is_focused = page.evaluate("""expectedId => {
        const active = document.activeElement;
        return active instanceof HTMLButtonElement
            && active.dataset.action === 'choose-product'
            && active.dataset.productId === expectedId;
    }""", str(product_ids[-1]))
    assert last_is_focused
    page.keyboard.press("Enter")
    last_button = choices.nth(49)
    assert last_button.get_attribute("aria-current") == "true"
    REPORT["bulk_50"]["last_product_keyboard_reachable"] = True
    REPORT["checks"].append("common_50_product_navigator_last_card_keyboard_reachable")

    # First review has 50 explicit title changes. Then simulate one valid
    # concurrent manual description edit with its version/audit metadata.
    _edit_bulk_titles(page, product_ids)
    page.get_by_role("button", name="Проверить изменения").click()
    preview_heading = page.get_by_role("heading", name="Проверьте изменения общего товара")
    preview_heading.wait_for(state="attached")
    assert page.locator("#common-content-preview .cpc-diff-product").count() == 50
    assert REPORT["preview_item_counts"][-1] == 50, REPORT["preview_item_counts"][-5:]
    first_preview_observation = REPORT["preview_selection_observations"][-1]
    assert first_preview_observation == {
        "item_count": 50,
        "unique_product_count": 50,
        "product_id_fingerprint": _id_set_fingerprint(product_ids),
    }, first_preview_observation
    REPORT["bulk_50"]["first_preview_product_ids_match"] = True
    REPORT["interactions"].append("review_exact_50_explicit_title_overrides")

    stale_id = product_ids[24]
    manual_drift = _install_synthetic_manual_drift(stale_id, FIXTURE["user_id"])
    with app.app_context():
        db.session.expire_all()
        after_drift_products = _bulk_product_snapshot(all_ids)
        audits_before_stale_apply = _bulk_audit_snapshot(all_ids)
        drafts_before_stale_apply = _model_snapshot(MarketplaceProductDraft)
        listings_before_stale_apply = _model_snapshot(MarketplaceListing)
        operations_before_stale_apply = _model_snapshot(MarketplaceOperation)
    assert after_drift_products[stale_id]["description"] == manual_drift["description"]
    assert after_drift_products[stale_id]["content_edit_version"] == manual_drift["version"]
    assert after_drift_products[overflow_id] == initial_products[overflow_id]

    apply_path = "/api/my-products/common-content/apply"
    EXPECTED_CONFLICTS[apply_path][0] += 1
    page.locator('input[data-action="acknowledge-preview"]').check()
    page.get_by_role("button", name="Сохранить общий товар").click()
    error = page.locator("#common-content-error")
    error.wait_for(state="visible")
    assert "Предыдущий diff отменён" in error.inner_text()
    assert page.locator("#common-content-preview").is_hidden()
    assert page.locator("#common-content-editor").evaluate("el => !el.inert")
    with app.app_context():
        db.session.expire_all()
        rejected_products = _bulk_product_snapshot(all_ids)
        rejected_audits = _bulk_audit_snapshot(all_ids)
        assert rejected_products == after_drift_products
        assert rejected_audits == audits_before_stale_apply
        assert _model_snapshot(MarketplaceProductDraft) == drafts_before_stale_apply
        assert _model_snapshot(MarketplaceListing) == listings_before_stale_apply
        assert _model_snapshot(MarketplaceOperation) == operations_before_stale_apply
    REPORT["bulk_50"]["stale_denial_unchanged_selected_products"] = sum(
        rejected_products[product_id] == after_drift_products[product_id]
        for product_id in product_ids
    )
    REPORT["bulk_50"]["stale_denial_new_audits"] = (
        len(rejected_audits) - len(initial_audits)
    )
    assert REPORT["bulk_50"]["stale_denial_unchanged_selected_products"] == 50
    assert REPORT["bulk_50"]["stale_denial_new_audits"] == 0
    REPORT["bulk_50"]["stale_apply_atomic_rejection"] = True
    REPORT["checks"].append("common_50_stale_manual_member_denied_atomically_without_partial_audit")

    # The stale token is discarded by the real UI. Re-read all 50 records,
    # explicitly restate the edits, review a fresh diff, then acknowledge it.
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Перечитать выбранные").click()
    page.get_by_text("Данные выбранных товаров перечитаны", exact=False).wait_for(timeout=20000)
    _edit_bulk_titles(page, product_ids)
    page.get_by_role("button", name="Проверить изменения").click()
    preview_heading.wait_for(state="attached")
    assert page.locator("#common-content-preview .cpc-diff-product").count() == 50
    assert REPORT["preview_item_counts"][-1] == 50, REPORT["preview_item_counts"][-5:]
    recovery_preview_observation = REPORT["preview_selection_observations"][-1]
    assert recovery_preview_observation == {
        "item_count": 50,
        "unique_product_count": 50,
        "product_id_fingerprint": _id_set_fingerprint(product_ids),
    }, recovery_preview_observation
    REPORT["bulk_50"]["recovery_preview_product_ids_match"] = True
    REPORT["bulk_50"]["recovery_preview_items"] = REPORT["preview_item_counts"][-1]
    save_button = page.get_by_role("button", name="Сохранить общий товар")
    assert not save_button.is_enabled()
    page.locator('input[data-action="acknowledge-preview"]').check()
    assert save_button.is_enabled()
    save_button.click()
    page.get_by_text("Общий товар сохранён", exact=False).wait_for(timeout=30000)
    page.wait_for_function("""() => {
        const root = document.querySelector('#common-content-editor');
        return root && root.getAttribute('aria-busy') === 'false' && !root.inert;
    }""", timeout=30000)
    assert REPORT["apply_result_counts"][-1] == 50, REPORT["apply_result_counts"]
    REPORT["bulk_50"]["recovery_apply_items"] = REPORT["apply_result_counts"][-1]
    recovery_apply_observation = REPORT["apply_result_observations"][-1]
    assert recovery_apply_observation == {
        "item_count": 50,
        "unique_product_count": 50,
        "product_id_fingerprint": _id_set_fingerprint(product_ids),
    }, recovery_apply_observation
    REPORT["bulk_50"]["recovery_apply_product_ids_match"] = True

    with app.app_context():
        db.session.expire_all()
        final_products = _bulk_product_snapshot(all_ids)
        final_audits = _bulk_audit_snapshot(all_ids)
        final_drafts = _model_snapshot(MarketplaceProductDraft)
        final_listings = _model_snapshot(MarketplaceListing)
        final_operations = _model_snapshot(MarketplaceOperation)
    assert set(final_products) == set(all_ids)
    assert final_products[overflow_id] == initial_products[overflow_id]
    assert final_drafts == initial_drafts
    assert final_listings == initial_listings
    assert final_operations == initial_operations

    for index, product_id in enumerate(product_ids, start=1):
        before = initial_products[product_id]
        after = final_products[product_id]
        expected_title = f"Ручное название массового товара {index:02d}"
        expected_version = 3 if product_id == stale_id else 2
        overrides = json.loads(after["content_overrides_json"])
        fields = overrides["fields"]
        assert after["title"] == expected_title
        assert after["content_edit_version"] == expected_version
        assert after["original_data"] == before["original_data"]
        assert after["photo_urls"] == before["photo_urls"]
        assert after["characteristics"] == before["characteristics"]
        mutable_columns = {
            "title", "content_overrides_json", "content_edit_version", "updated_at",
        }
        if product_id == stale_id:
            mutable_columns.add("description")
        assert all(
            after[column] == before[column]
            for column in before if column not in mutable_columns
        )
        title_entry = fields["title"]
        assert set(title_entry) == {
            "value", "inherited_value", "inherited_origin",
            "edited_by_user_id", "edited_at", "edit_version",
        }
        assert title_entry["value"] == expected_title
        assert title_entry["inherited_value"] == before["title"]
        assert title_entry["inherited_origin"] == "source"
        assert title_entry["edited_by_user_id"] == FIXTURE["user_id"]
        assert title_entry["edit_version"] == expected_version
        assert set(fields) == ({"title", "description"} if product_id == stale_id else {"title"})
        if product_id == stale_id:
            assert after["description"] == manual_drift["description"]
            assert fields["description"]["value"] == manual_drift["description"]
            assert fields["description"]["inherited_value"] == json.loads(before["original_data"])["description"]
            assert fields["description"]["inherited_origin"] == "source"
            assert fields["description"]["edit_version"] == manual_drift["version"]
        else:
            assert after["description"] == before["description"]
    REPORT["bulk_50"]["persisted_overrides"] = sum(
        "title" in json.loads(final_products[product_id]["content_overrides_json"])["fields"]
        for product_id in product_ids
    )
    REPORT["bulk_50"]["final_content_edit_version_counts"] = {
        str(version): count
        for version, count in sorted(Counter(
            final_products[product_id]["content_edit_version"]
            for product_id in product_ids
        ).items())
    }
    assert REPORT["bulk_50"]["persisted_overrides"] == 50
    assert REPORT["bulk_50"]["final_content_edit_version_counts"] == {"2": 49, "3": 1}

    old_audit_ids = {dict(row)["id"] for row in initial_audits}
    new_audits = [dict(row) for row in final_audits if dict(row)["id"] not in old_audit_ids]
    assert len(new_audits) == 50
    assert {row["imported_product_id"] for row in new_audits} == set(product_ids)
    for row in new_audits:
        product_id = row["imported_product_id"]
        index = product_ids.index(product_id) + 1
        previous = json.loads(row["previous_values"])
        current = json.loads(row["new_values"])
        metadata = current.pop("__seller_common_content_audit")
        assert row["task_id"] is None
        assert row["agent_id"] == "seller-common-content-v1"
        assert metadata["actor_user_id"] == FIXTURE["user_id"]
        assert metadata["content_edit_version"] == final_products[product_id]["content_edit_version"]
        assert current["title"] == f"Ручное название массового товара {index:02d}"
        expected_prior_description = (
            manual_drift["description"] if product_id == stale_id
            else initial_products[product_id]["description"]
        )
        assert previous["description"] == expected_prior_description
    REPORT["bulk_50"]["audit_rows"] = len(new_audits)
    REPORT["bulk_50"]["channel_records_unchanged"] = (
        final_drafts == initial_drafts
        and final_listings == initial_listings
        and final_operations == initial_operations
    )
    REPORT["bulk_50"]["inheritance_and_source_preserved"] = True
    assert REPORT["bulk_50"]["audit_rows"] == 50
    assert REPORT["bulk_50"]["channel_records_unchanged"]
    assert REPORT["bulk_50"]["inheritance_and_source_preserved"]
    assert REPORT["preview_item_counts"][-3:] == [51, 50, 50]
    assert REPORT["expected_http_rejections"][-2:] == [
        {
            "method": "GET",
            "path": "/my-products/common-content",
            "status": 413,
            "code": "too_many_items",
            "has_query": True,
            "has_fragment": False,
        },
        {
            "method": "POST",
            "path": "/api/my-products/common-content/preview",
            "status": 413,
            "code": "too_many_items",
            "has_query": False,
            "has_fragment": False,
        },
    ]
    assert not any(EXPECTED_HTTP_REJECTIONS.values())
    REPORT["checks"].append("common_50_recovery_apply_persists_50_overrides_and_50_server_audits")
    REPORT["checks"].append("common_50_recovery_preserves_inheritance_source_and_channel_snapshots")


def _photo_fingerprint(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _SyntheticColdPhotoCache:
    """Deterministic local cache for the isolated real-route cold-photo case."""

    def __init__(self, source_urls: list[str], image_bytes: bytes, directory: Path):
        self._lock = threading.Lock()
        self._slot_for_url = {url: slot for slot, url in enumerate(source_urls)}
        self._queue_counts = {slot: 0 for slot in self._slot_for_url.values()}
        self._lookup_history = {slot: [] for slot in self._slot_for_url.values()}
        self._ready_after = {0: 1, 1: 4}
        self._ready = {slot: False for slot in self._slot_for_url.values()}
        self._image_path = directory / "synthetic-cold-photo.jpg"
        self._image_path.write_bytes(image_bytes)

    def _slot(self, url: str) -> int:
        try:
            return self._slot_for_url[url]
        except KeyError as exc:
            raise AssertionError("The isolated photo cache received an unseeded source URL") from exc

    def is_cached(self, _supplier_type, _external_id, url):
        slot = self._slot(url)
        with self._lock:
            ready = bool(self._ready[slot])
            self._lookup_history[slot].append(ready)
            return ready

    def queue_download(self, *, supplier_type, external_id, url, fallback_urls, auth_cookies_provider):
        del supplier_type, external_id, fallback_urls, auth_cookies_provider
        slot = self._slot(url)
        with self._lock:
            self._queue_counts[slot] += 1
            if self._queue_counts[slot] >= self._ready_after[slot]:
                self._ready[slot] = True
        return True

    def get_cache_path(self, _supplier_type, _external_id, url):
        self._slot(url)
        return self._image_path

    def queue_count_for(self, slot: int) -> int:
        with self._lock:
            return self._queue_counts[slot]

    def last_lookup_for(self, slot: int) -> bool:
        with self._lock:
            history = self._lookup_history[slot]
            if not history:
                raise AssertionError("Photo route did not check cache before serving its response")
            return history[-1]

    def lookup_history_for(self, slot: int) -> list[bool]:
        with self._lock:
            return list(self._lookup_history[slot])

    def reset_slot(self, slot: int, *, ready_after: int) -> None:
        with self._lock:
            self._queue_counts[slot] = 0
            self._lookup_history[slot] = []
            self._ready_after[slot] = ready_after
            self._ready[slot] = False


def _synthetic_photo_jpeg() -> bytes:
    image = Image.new("RGB", (120, 80))
    for y in range(80):
        for x in range(120):
            image.putpixel((x, y), ((x * 17 + y * 3) % 256, (y * 29 + x * 5) % 256, (x * 7 + y * 11) % 256))
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=88, optimize=True)
    payload = stream.getvalue()
    if len(payload) < 256:
        raise AssertionError("Synthetic JPEG must be a nontrivial real image body")
    return payload


def _seed_cold_photo_product() -> tuple[int, list[str]]:
    source_urls = [
        "https://photo-cold-fixture.invalid/selected-first.jpg",
        "https://photo-cold-fixture.invalid/selected-second.jpg",
    ]
    with app.app_context():
        product = ImportedProduct(
            seller_id=FIXTURE["seller_id"],
            external_id="COMMON-COLD-PHOTO-FIXTURE",
            source_type="manual",
            title="Синтетический товар с холодным предпросмотром",
            description="Локальный browser fixture; внешняя загрузка запрещена.",
            photo_urls=json.dumps(source_urls, ensure_ascii=False),
            original_data=json.dumps({
                "title": "Синтетический товар с холодным предпросмотром",
                "description": "Локальный browser fixture; внешняя загрузка запрещена.",
                "photo_urls": source_urls,
            }, ensure_ascii=False),
        )
        db.session.add(product)
        db.session.commit()
        return int(product.id), source_urls


def _photo_domain_snapshot(product_id: int) -> dict:
    with app.app_context():
        db.session.expire_all()
        product = db.session.get(ImportedProduct, product_id)
        if product is None:
            raise AssertionError("Synthetic cold-photo source disappeared")
        source_values = {
            column.name: getattr(product, column.name)
            for column in ImportedProduct.__table__.columns
        }
        return {
            "source_fingerprint": _photo_fingerprint(source_values),
            "domain_fingerprint": _photo_fingerprint({
                "source": source_values,
                "drafts": _model_snapshot(MarketplaceProductDraft),
                "listings": _model_snapshot(MarketplaceListing),
                "operations": _model_snapshot(MarketplaceOperation),
                "source_audits": _bulk_audit_snapshot([product_id]),
            }),
        }


def _photo_selection_state(page, product_id: int) -> dict:
    value = page.evaluate("""productId => {
        const current = document.querySelector('#common-content-product-list [aria-current="true"]');
        const cards = [...document.querySelectorAll(
            '.cpc-photo-card[data-photo-preview-product="' + productId + '"]'
        )];
        const selected = cards.flatMap(card => [...card.querySelectorAll(
            '.cpc-photo-option[aria-pressed="true"]'
        )].map(button => button.dataset.photoUrl));
        const ordered = [...document.querySelectorAll(
            '.cpc-photo-order-row .cpc-photo-order-actions button[data-direction="-1"]'
        )].map(button => button.dataset.photoUrl);
        return {
            current_product: current ? current.dataset.productId : null,
            selected_urls: selected,
            ordered_urls: ordered,
        };
    }""", str(product_id))
    selected_urls = value.get("selected_urls") if isinstance(value, dict) else None
    ordered_urls = value.get("ordered_urls") if isinstance(value, dict) else None
    if not isinstance(selected_urls, list) or not isinstance(ordered_urls, list):
        raise AssertionError("Could not read inherited photo selection from the actual editor DOM")
    return {
        "selected_count": len(selected_urls),
        "selected_fingerprint": _photo_fingerprint(selected_urls),
        "order_fingerprint": _photo_fingerprint(ordered_urls),
        "current_product": value.get("current_product"),
    }


def _wait_for_photo_status(page, selector: str, desired: str, *, timeout_ms: int = 25000) -> list[str]:
    deadline = time.monotonic() + timeout_ms / 1000
    sequence: list[str] = []
    started = False
    while time.monotonic() < deadline:
        current = page.locator(selector).get_attribute("data-photo-preview-state")
        if current and (started or current != "loading"):
            started = True
            if not sequence or sequence[-1] != current:
                sequence.append(current)
        if current == desired:
            return sequence
        page.wait_for_timeout(75)
    raise AssertionError({"photo_state_timeout": desired, "observed_states": sequence})


def _install_photo_state_observer(page, product_id: int) -> None:
    script = """(() => {
        const productId = __PRODUCT_ID__;
        const history = {0: [], 1: []};
        const remember = status => {
            if (!status) return;
            const card = status.closest('.cpc-photo-card[data-photo-preview-product][data-photo-preview-slot]');
            if (!card || card.dataset.photoPreviewProduct !== String(productId)) return;
            const slot = Number(card.dataset.photoPreviewSlot);
            const value = status.dataset.photoPreviewState;
            if (!(slot in history) || !value) return;
            const rows = history[slot];
            if (!rows.length || rows[rows.length - 1] !== value) rows.push(value);
        };
        const rememberTree = node => {
            if (!(node instanceof Element)) return;
            if (node.matches('[data-photo-preview-state]')) remember(node);
            node.querySelectorAll('[data-photo-preview-state]').forEach(remember);
        };
        document.querySelectorAll('[data-photo-preview-state]').forEach(remember);
        const observer = new MutationObserver(records => records.forEach(record => {
            if (record.type === 'attributes') remember(record.target);
            else record.addedNodes.forEach(rememberTree);
        }));
        observer.observe(document.documentElement, {
            subtree: true, childList: true, attributes: true,
            attributeFilter: ['data-photo-preview-state'],
        });
        window.__uxCommonPhotoStateHistory = history;
        window.__uxCommonPhotoStateObserver = observer;
    })();""".replace("__PRODUCT_ID__", json.dumps(str(product_id)))
    page.add_init_script(script)


def _photo_state_history(page, slot: int, *, start_at_pending: bool = False) -> list[str]:
    values = page.evaluate("slot => (window.__uxCommonPhotoStateHistory || {})[slot] || []", slot)
    if not isinstance(values, list):
        return []
    result = []
    started = not start_at_pending
    for value in values:
        if not isinstance(value, str):
            continue
        if not started:
            if value != "pending":
                continue
            started = True
        if not result or result[-1] != value:
            result.append(value)
    return result


def _photo_status_selector(product_id: int, slot: int) -> str:
    return (
        '.cpc-photo-card[data-photo-preview-product="' + str(product_id)
        + '"][data-photo-preview-slot="' + str(slot) + '"] [data-photo-preview-state]'
    )


def _photo_card_selector(product_id: int, slot: int) -> str:
    return (
        '.cpc-photo-card[data-photo-preview-product="' + str(product_id)
        + '"][data-photo-preview-slot="' + str(slot) + '"]'
    )


def _photo_requests_for(slot: int) -> list[dict]:
    rows = []
    for row in PHOTO_CASE_CAPTURE["requests"]:
        if row.get("_slot") != slot:
            continue
        rows.append({key: value for key, value in row.items() if not key.startswith("_")})
    return rows


def _photo_side_effects(before: dict, after: dict) -> dict:
    return {
        "preview_post_count": REPORT["synthetic_actions"]["preview_requests"] - before["preview_requests"],
        "apply_post_count": REPORT["synthetic_actions"]["apply_requests"] - before["apply_requests"],
        "provider_attempts": REPORT["synthetic_actions"]["provider_attempts"] - before["provider_attempts"],
        "domain_write_count": PHOTO_CASE_CAPTURE["domain_write_count"],
        "domain_snapshots_unchanged": before["domain_fingerprint"] == after["domain_fingerprint"],
    }


def _photo_selection_receipt(before: dict, after: dict, source_before: dict, source_after: dict) -> dict:
    return {
        "selected_count_before": before["selected_count"],
        "selected_count_after": after["selected_count"],
        "selected_fingerprint_before": before["selected_fingerprint"],
        "selected_fingerprint_after": after["selected_fingerprint"],
        "order_fingerprint_before": before["order_fingerprint"],
        "order_fingerprint_after": after["order_fingerprint"],
        "source_fingerprint_before": source_before["source_fingerprint"],
        "source_fingerprint_after": source_after["source_fingerprint"],
        "current_product_unchanged": (
            before["current_product"] == after["current_product"]
            and before["current_product"] is not None
        ),
    }


def _measure_retry_touch_row(page, product_id: int, slot: int, width: int, theme: str) -> dict:
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("theme => { localStorage.setItem('sh-theme', theme); document.documentElement.dataset.theme = theme; }", theme)
    card_selector = _photo_card_selector(product_id, slot)
    card = page.locator(card_selector)
    card.scroll_into_view_if_needed()
    retry = card.locator('button[data-action="retry-photo-preview"]')
    target = retry.evaluate("el => ({present: !!el, visible: !el.hidden && el.getClientRects().length > 0})")
    if not target["present"] or not target["visible"]:
        raise AssertionError("Manual photo retry was not visible at the failed state")
    mode = page.locator('#common-content-fields section[data-field-section="photos"] button[data-action="toggle-mode"]')
    mode.focus()
    reached = False
    for _ in range(12):
        page.keyboard.press("Tab")
        if retry.evaluate("el => document.activeElement === el"):
            reached = True
            break
    result = retry.evaluate("""(el, requestedTheme) => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        const center = {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
        const top = document.elementFromPoint(center.x, center.y);
        const topRetry = top && top.closest('[data-action="retry-photo-preview"]');
        const viewport = {width: innerWidth, height: innerHeight};
        return {
            width: viewport.width,
            requested_theme: requestedTheme,
            actual_theme: document.documentElement.dataset.theme,
            status: el.closest('[data-photo-preview-state]')?.dataset.photoPreviewState
                || el.closest('.cpc-photo-card')?.querySelector('[data-photo-preview-state]')?.dataset.photoPreviewState,
            visible: !el.hidden && el.getClientRects().length > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && Number(style.opacity || 1) > 0,
            enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
            inherited_mode: document.querySelector(
                '#common-content-fields section[data-field-section="photos"] button[data-action="toggle-mode"]'
            )?.textContent.trim() === 'Изменить значение',
            accessible_name: el.getAttribute('aria-label') || el.innerText.trim(),
            button_rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
            viewport,
            center_hit_target: !!topRetry && topRetry === el,
            nested_in_selection_button: !!el.closest('.cpc-photo-option'),
            focus_visible: document.activeElement === el && el.matches(':focus-visible'),
            focus_outline: {
                style: style.outlineStyle,
                width_px: Number.parseFloat(style.outlineWidth) || 0,
                offset_px: Number.parseFloat(style.outlineOffset) || 0,
            },
            focus_rect_inside_viewport: rect.x >= 0 && rect.y >= 0
                && rect.right <= innerWidth && rect.bottom <= innerHeight,
        };
    }""", theme)
    result["visible"] = bool(result.get("visible"))
    result["enabled"] = bool(result.get("enabled"))
    result["focus_reached_by_tab"] = reached
    if not reached:
        raise AssertionError({"retry_not_tab_reachable": {"width": width, "theme": theme}})
    if result["status"] != "failed" or not result["inherited_mode"]:
        raise AssertionError({"retry_touch_state_invalid": {"width": width, "theme": theme, "state": result}})
    return result


def _run_common_photo_retry_case(page) -> None:
    global PHOTO_CASE_CAPTURE, PHOTO_CASE_CACHE
    source_id, source_urls = _seed_cold_photo_product()
    route_paths = {
        slot: f"/api/photos/imported-product/{source_id}/{slot}"
        for slot in (0, 1)
    }
    image_bytes = _synthetic_photo_jpeg()
    PHOTO_CASE_CACHE = _SyntheticColdPhotoCache(source_urls, image_bytes, TEMP_PATH)
    import services.photo_cache as photo_cache_module
    previous_cache_factory = photo_cache_module.get_photo_cache
    photo_cache_module.get_photo_cache = lambda: PHOTO_CASE_CACHE

    capture = {
        "route_paths": route_paths,
        "phase": "initial",
        "requests": [],
        "domain_write_count": 0,
    }
    PHOTO_CASE_CAPTURE = capture
    with app.app_context():
        engine = db.engine

    def count_domain_write(_conn, _cursor, statement, _parameters, _context, _many):
        if PHOTO_CASE_CAPTURE is None:
            return
        normalized = statement.lstrip().upper()
        if normalized.startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")):
            PHOTO_CASE_CAPTURE["domain_write_count"] += 1

    event.listen(engine, "before_cursor_execute", count_domain_write)
    try:
        with app.app_context():
            before_domain = _photo_domain_snapshot(source_id)
        before_effects = {
            "preview_requests": REPORT["synthetic_actions"]["preview_requests"],
            "apply_requests": REPORT["synthetic_actions"]["apply_requests"],
            "provider_attempts": REPORT["synthetic_actions"]["provider_attempts"],
            "domain_fingerprint": before_domain["domain_fingerprint"],
        }
        query = f"/my-products/common-content?product_id={source_id}&product_id={FIXTURE['second_source_id']}"
        page.set_viewport_size({"width": 1440, "height": 1050})
        _install_photo_state_observer(page, source_id)
        response = page.goto(BASE + query, wait_until="domcontentloaded")
        assert response and response.status == 200
        page.locator("#common-content-fields").wait_for()
        initial_selection = _photo_selection_state(page, source_id)
        assert initial_selection["selected_count"] == 2
        assert initial_selection["current_product"] == str(source_id)
        page.locator(_photo_card_selector(source_id, 0)).scroll_into_view_if_needed()
        page.locator(_photo_card_selector(source_id, 1)).scroll_into_view_if_needed()
        for slot in (0, 1):
            page.locator(_photo_status_selector(source_id, slot)).wait_for()
        cards_visible = page.evaluate("""productId => [0, 1].map(slot => {
            const card = document.querySelector(
                '.cpc-photo-card[data-photo-preview-product="' + productId
                + '"][data-photo-preview-slot="' + slot + '"]'
            );
            if (!card) return false;
            const rect = card.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.left < innerWidth
                && rect.bottom > 0 && rect.top < innerHeight;
        })""", str(source_id))
        assert cards_visible == [True, True], cards_visible
        page.wait_for_function("""productId => {
            const state = slot => document.querySelector(
                '.cpc-photo-card[data-photo-preview-product="' + productId
                + '"][data-photo-preview-slot="' + slot + '"] [data-photo-preview-state]'
            )?.dataset.photoPreviewState;
            return state(0) === 'ready' && state(1) === 'failed';
        }""", arg=str(source_id), timeout=40000)
        slot0_sequence = _photo_state_history(page, 0, start_at_pending=True)
        slot1_sequence = _photo_state_history(page, 1, start_at_pending=True)
        assert PHOTO_CASE_CACHE.lookup_history_for(0)[:2] == [False, True]
        assert PHOTO_CASE_CACHE.lookup_history_for(1)[:4] == [False, False, False, False]
        assert PHOTO_CASE_CACHE.queue_count_for(0) == 1
        assert PHOTO_CASE_CACHE.queue_count_for(1) == 4
        pending_rows = _photo_requests_for(0)
        auto_rows = _photo_requests_for(1)
        assert len(pending_rows) == 2 and len(auto_rows) == 4
        assert all(row["http_status"] == 202 for row in auto_rows)
        assert pending_rows[0]["http_status"] == 202 and pending_rows[1]["http_status"] == 200
        assert pending_rows[0]["trigger"] == "initial" and pending_rows[1]["trigger"] == "automatic_retry"
        assert all(row["trigger"] == ("initial" if index == 0 else "automatic_retry")
                   for index, row in enumerate(auto_rows))

        image0 = page.locator(
            _photo_card_selector(source_id, 0) + ' img[data-photo-preview-image]'
        )
        decoded0 = image0.evaluate("el => ({loaded: el.complete && el.naturalWidth > 0, width: el.naturalWidth, height: el.naturalHeight})")
        assert decoded0 == {"loaded": True, "width": 120, "height": 80}
        auto_selection = _photo_selection_state(page, source_id)
        with app.app_context():
            after_auto = _photo_domain_snapshot(source_id)

        pending_case = {
            "status_sequence": slot0_sequence,
            "photo_requests": pending_rows,
            "automatic_retry_count": 1,
            "queue_count": 1,
            "cache_ready_before_retry": PHOTO_CASE_CACHE.lookup_history_for(0)[1],
            "decoded_image": {
                "loaded": decoded0["loaded"],
                "width": decoded0["width"],
                "height": decoded0["height"],
                "sha256": pending_rows[-1]["body_sha256"],
            },
            "selection": _photo_selection_receipt(initial_selection, auto_selection, before_domain, after_auto),
            "side_effects": _photo_side_effects(before_effects, after_auto),
        }

        touch_rows = []
        for width in (320, 360, 390):
            for theme in ("light", "dark"):
                touch_rows.append(_measure_retry_touch_row(page, source_id, 1, width, theme))
        assert len(touch_rows) == 6

        # The failed photo remains selected/inherited. Manual retry is a
        # separate sibling control reached by the keyboard and performs only
        # the exact same-origin GET route.
        page.set_viewport_size({"width": 1440, "height": 1050})
        page.evaluate("localStorage.setItem('sh-theme', 'light'); document.documentElement.dataset.theme = 'light'")
        retry_slot1 = page.locator(
            _photo_card_selector(source_id, 1) + ' button[data-action="retry-photo-preview"]'
        )
        retry_slot1.scroll_into_view_if_needed()
        mode = page.locator('#common-content-fields section[data-field-section="photos"] button[data-action="toggle-mode"]')
        mode.focus()
        focused_slot1 = False
        for _ in range(12):
            page.keyboard.press("Tab")
            if retry_slot1.evaluate("el => document.activeElement === el"):
                focused_slot1 = True
                break
        assert focused_slot1, "Manual retry control must be reachable by keyboard"
        retry_button_observed = retry_slot1.evaluate("""el => ({
            present: !!el && !el.hidden,
            visible: el.getClientRects().length > 0,
            enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
            nested_in_selection_button: !!el.closest('.cpc-photo-option'),
            accessible_name: el.getAttribute('aria-label') || el.innerText.trim(),
            focus_reached: document.activeElement === el,
            focus_visible: el.matches(':focus-visible'),
            min_width_px: el.getBoundingClientRect().width,
            min_height_px: el.getBoundingClientRect().height,
            selection_button_disabled_in_inherit_mode: !!el.closest('.cpc-photo-card')?.querySelector('.cpc-photo-option')?.disabled,
        })""")
        assert retry_button_observed["present"] and retry_button_observed["visible"]
        assert retry_button_observed["enabled"] and retry_button_observed["focus_visible"]
        assert retry_button_observed["selection_button_disabled_in_inherit_mode"]
        PHOTO_CASE_CAPTURE["phase"] = "manual_retry"
        before_manual_selection = _photo_selection_state(page, source_id)
        with app.app_context():
            before_manual_domain = _photo_domain_snapshot(source_id)
        manual_effects_before = {
            "preview_requests": REPORT["synthetic_actions"]["preview_requests"],
            "apply_requests": REPORT["synthetic_actions"]["apply_requests"],
            "provider_attempts": REPORT["synthetic_actions"]["provider_attempts"],
            "domain_fingerprint": before_manual_domain["domain_fingerprint"],
        }
        pre_manual_count = len(PHOTO_CASE_CAPTURE["requests"])
        page.evaluate("slot => { window.__uxCommonPhotoStateHistory[slot] = []; }", 1)
        page.keyboard.press("Enter")
        manual_image = page.locator(
            _photo_card_selector(source_id, 1) + ' img[data-photo-preview-image][data-photo-preview-attempt="5"]'
        )
        manual_image.wait_for(state="attached", timeout=5000)
        page.wait_for_function("""() => {
            const img = document.querySelector('.cpc-photo-card[data-photo-preview-slot="1"] img[data-photo-preview-image][data-photo-preview-attempt="5"]');
            return !!img && img.complete && img.naturalWidth === 120 && img.naturalHeight === 80;
        }""", timeout=10000)
        manual_states = _photo_state_history(page, 1)
        manual_image_info = manual_image.evaluate("el => ({loaded: el.complete && el.naturalWidth > 0, width: el.naturalWidth, height: el.naturalHeight})")
        assert manual_image_info == {"loaded": True, "width": 120, "height": 80}
        focus_retained_after_ready = retry_slot1.evaluate(
            "el => document.activeElement === el && el.matches(':focus-visible')"
        )
        assert focus_retained_after_ready
        assert len(PHOTO_CASE_CAPTURE["requests"]) == pre_manual_count + 1
        manual_request = _photo_requests_for(1)[-1]
        assert manual_request["attempt"] == 5 and manual_request["trigger"] == "manual_retry"
        assert manual_request["http_status"] == 200
        after_manual_selection = _photo_selection_state(page, source_id)
        with app.app_context():
            after_manual_domain = _photo_domain_snapshot(source_id)
        manual_side_effects = _photo_side_effects(manual_effects_before, after_manual_domain)
        assert manual_side_effects["preview_post_count"] == 0
        assert manual_side_effects["apply_post_count"] == 0
        assert manual_side_effects["provider_attempts"] == 0
        assert manual_side_effects["domain_write_count"] == 0
        assert manual_side_effects["domain_snapshots_unchanged"]

        # Re-arm one real automatic-retry timer, then change current product
        # before its delay. The old generation must stop without another GET
        # or rendering the previous product's image in the decoy editor.
        PHOTO_CASE_CACHE.reset_slot(0, ready_after=100)
        PHOTO_CASE_CAPTURE["phase"] = "switch_pending"
        source_before_switch = _photo_selection_state(page, source_id)
        with app.app_context():
            domain_before_switch = _photo_domain_snapshot(source_id)
        retry_slot0 = page.locator(
            _photo_card_selector(source_id, 0) + ' button[data-action="retry-photo-preview"]'
        )
        retry_slot0.focus()
        page.keyboard.press("Enter")
        switch_state = _wait_for_photo_status(
            page, _photo_status_selector(source_id, 0), "pending", timeout_ms=5000,
        )
        switch_request = _photo_requests_for(0)[-1]
        assert switch_request["http_status"] == 202 and switch_request["trigger"] == "manual_retry"
        switch_request_count = len(PHOTO_CASE_CAPTURE["requests"])
        PHOTO_CASE_CAPTURE["phase"] = "switch_wait"
        page.locator(
            '#common-content-product-list button[data-action="choose-product"][data-product-id="{}"]'.format(FIXTURE["second_source_id"])
        ).click()
        current_other_product = page.locator('#common-content-product-list button[aria-current="true"]').get_attribute("data-product-id") == str(FIXTURE["second_source_id"])
        assert current_other_product
        page.wait_for_timeout(2500)
        requests_after_switch = len(PHOTO_CASE_CAPTURE["requests"]) - switch_request_count
        assert requests_after_switch == 0
        stale_photo_rendered = page.locator('.cpc-photo-card img[data-photo-preview-image]').count() > 0
        assert not stale_photo_rendered
        current_other_product_after_wait = page.locator(
            '#common-content-product-list button[aria-current="true"]'
        ).get_attribute("data-product-id") == str(FIXTURE["second_source_id"])
        assert current_other_product_after_wait
        late_automatic_retries = sum(
            1 for row in PHOTO_CASE_CAPTURE["requests"][switch_request_count:]
            if row.get("trigger") == "automatic_retry"
        )
        page.locator(
            '#common-content-product-list button[data-action="choose-product"][data-product-id="{}"]'.format(source_id)
        ).click()
        assert page.locator('#common-content-product-list button[aria-current="true"]').get_attribute("data-product-id") == str(source_id)
        page.locator(_photo_status_selector(source_id, 0)).wait_for()
        source_after_switch = _photo_selection_state(page, source_id)
        with app.app_context():
            domain_after_switch = _photo_domain_snapshot(source_id)
        switch_receipt = {
            "switch_completed_while_retry_pending": switch_state[-1:] == ["pending"] and current_other_product,
            "cancelled_pending_request": switch_request,
            "requests_after_switch": requests_after_switch,
            "current_other_product_after_cancelled_pending_request": current_other_product_after_wait,
            "stale_photo_rendered_in_current_product": stale_photo_rendered,
            "late_automatic_retries": late_automatic_retries,
            "source_product_restored": source_after_switch["current_product"] == str(source_id),
            "source_selection_fingerprint_before": source_before_switch["selected_fingerprint"],
            "source_selection_fingerprint_after": source_after_switch["selected_fingerprint"],
            "source_order_fingerprint_before": source_before_switch["order_fingerprint"],
            "source_order_fingerprint_after": source_after_switch["order_fingerprint"],
            "source_snapshot_unchanged": domain_before_switch["source_fingerprint"] == domain_after_switch["source_fingerprint"],
        }
        assert switch_receipt["current_other_product_after_cancelled_pending_request"] is True
        assert switch_receipt["stale_photo_rendered_in_current_product"] is False
        assert switch_receipt["late_automatic_retries"] == 0
        assert switch_receipt["source_selection_fingerprint_before"] == switch_receipt["source_selection_fingerprint_after"]
        assert switch_receipt["source_order_fingerprint_before"] == switch_receipt["source_order_fingerprint_after"]
        assert switch_receipt["source_snapshot_unchanged"]

        with app.app_context():
            final_domain = _photo_domain_snapshot(source_id)
        final_effects = _photo_side_effects(before_effects, final_domain)
        assert final_effects["preview_post_count"] == 0
        assert final_effects["apply_post_count"] == 0
        assert final_effects["provider_attempts"] == 0
        assert final_effects["domain_write_count"] == 0
        assert final_effects["domain_snapshots_unchanged"]

        overall_selection = _photo_selection_receipt(initial_selection, after_manual_selection, before_domain, after_manual_domain)
        overall_selection["current_product_unchanged"] = (
            initial_selection["current_product"] == str(source_id)
            and after_manual_selection["current_product"] == str(source_id)
        )
        exhausted_case = {
            "status_sequence": slot1_sequence + manual_states,
            "automatic_requests": auto_rows,
            "manual_request": manual_request,
            "automatic_retry_count": 3,
            "queue_count": 4,
            "selection_button_disabled_in_inherit_mode": retry_button_observed[
                "selection_button_disabled_in_inherit_mode"
            ],
            "retry_button": {
                "present": retry_button_observed["present"],
                "visible": retry_button_observed["visible"],
                "enabled": retry_button_observed["enabled"],
                "nested_in_selection_button": retry_button_observed["nested_in_selection_button"],
                "keyboard_key": "Enter",
                "focus_reached": retry_button_observed["focus_reached"],
                "focus_visible": retry_button_observed["focus_visible"],
                "focus_retained_after_ready": focus_retained_after_ready,
                "min_width_px": retry_button_observed["min_width_px"],
                "min_height_px": retry_button_observed["min_height_px"],
            },
            "selection": overall_selection,
            "side_effects": final_effects,
            "switch_while_photo_retry_pending": switch_receipt,
        }
        REPORT["common_photo_retry_observations"] = {
            "status": "passed",
            "named_check": COMMON_PHOTO_RETRY_CHECK,
            "retry_delays_seconds": [2, 4, 6],
            "max_automatic_retries": 3,
            "pending_recovers": pending_case,
            "exhaustion_manual_retry": exhausted_case,
        }
        REPORT["common_photo_retry_touch_observations"] = touch_rows
        REPORT["checks"].append({
            "name": COMMON_PHOTO_RETRY_CHECK,
            "status": "passed", "ok": True, "passed": True,
            "scenario_count": 2,
        })
        REPORT["checks"].append({
            "name": COMMON_PHOTO_RETRY_TOUCH_CHECK,
            "status": "passed", "ok": True, "passed": True,
            "observed_rows": len(touch_rows), "expected_rows": 6,
            "minimum_target_px": 44,
        })
        assert final_effects["domain_write_count"] == 0
        assert set((row["width"], row["requested_theme"]) for row in touch_rows) == {
            (width, theme) for width in (320, 360, 390) for theme in ("light", "dark")
        }
    finally:
        event.remove(engine, "before_cursor_execute", count_domain_write)
        PHOTO_CASE_CAPTURE = None
        photo_cache_module.get_photo_cache = previous_cache_factory


def run():
    product_ids = [FIXTURE["source_id"], FIXTURE["second_source_id"]]
    query = "&".join("product_id=" + str(value) for value in product_ids)
    editor_path = "/my-products/common-content?" + query
    with app.test_request_context():
        classic_path = url_for("seller_my_products")
        beta_path = url_for("seller_my_products_beta")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=CHROMIUM,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1050})
        context.add_cookies([authenticated_cookie()])
        context.route("**/*", bridge)
        page = context.new_page()
        page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)[:300]))
        page.on("console", record_console_message)

        # Both seller catalog entry points preserve exact IDs and enforce the
        # same 50-item cap before reaching the editor.
        page.set_viewport_size({"width": 1440, "height": 1050})
        classic = page.goto(BASE + classic_path, wait_until="domcontentloaded")
        assert classic and classic.status == 200
        row = page.locator(f'tr[data-product-id="{product_ids[0]}"]')
        single_link = row.get_by_role("link", name="Общий контент")
        single_link.wait_for()
        single_link.click()
        assert query_ids(page.url) == [product_ids[0]], page.url
        REPORT["checks"].append("classic_catalog_single_product_link")
        REPORT["pages"].append({"kind": "classic_catalog", "status": 200})

        beta = page.goto(BASE + beta_path, wait_until="domcontentloaded")
        assert beta and beta.status == 200
        page.locator("#my-products-app").wait_for()
        boxes = page.locator(".mp-check input[type=checkbox]")
        boxes.first.wait_for()
        assert boxes.count() >= 2
        boxes.nth(0).check()
        boxes.nth(1).check()
        page.get_by_role("button", name="Что сделать").click()
        action = page.get_by_role("menuitem", name="Изменить общий контент")
        assert action.is_enabled()
        action.click()
        assert set(query_ids(page.url)) == set(product_ids), page.url
        assert len(query_ids(page.url)) == 2
        REPORT["checks"].append("beta_bulk_selection_preserves_exact_product_ids")
        REPORT["pages"].append({"kind": "beta_catalog", "status": 200})

        # Cover every requested empty/selected route at every width/theme pair.
        empty_path = "/my-products/common-content"
        for state, path in (("empty", empty_path), ("selected", editor_path)):
            for width in (320, 360, 390, 768, 1024, 1280, 1440):
                for theme in ("light", "dark"):
                    layout_case(page, path, width, theme, state, classic_path)
        assert len(REPORT["layouts"]) == 28
        assert_mobile_touch_target_evidence()
        assert_mobile_product_navigator_evidence()
        assert REPORT["synthetic_actions"]["empty_route_api_reads"] == 0
        assert REPORT["synthetic_actions"]["empty_route_mutating_requests"] == 0
        REPORT["checks"].append("common_empty_editor_no_product_api_or_writes")
        REPORT["checks"].append("common_empty_editor_internal_catalog_link")
        REPORT["checks"].append("empty_and_selected_layout_matrix_28_exact_combinations")

        # Return to a comfortable viewport for the actual review interaction.
        page.set_viewport_size({"width": 1440, "height": 1050})
        page.goto(BASE + editor_path, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        page.evaluate("localStorage.setItem('sh-theme', 'light')")
        page.reload(wait_until="domcontentloaded")
        page.locator("#common-content-fields").wait_for()

        page.get_by_text("Сохранится только общий товар в Seller Hub", exact=False).wait_for()
        source_line = page.locator(".cpc-source-line").inner_text()
        assert "локальный источник" in source_line and "synthetic" not in source_line
        assert "needs_category" not in page.locator(".cpc-recipient-section").inner_text()
        REPORT["checks"].append("known_source_and_draft_statuses_are_localized")
        title_section = page.locator('section[data-field-section="title"]')
        title_section.get_by_role("button", name="Изменить значение", exact=True).click()
        title_input = page.get_by_role("textbox", name="Общее название товара")
        title_input.fill("Товар после проверки общего редактора")
        REPORT["interactions"].append("override_title_with_explicit_source_label")

        description_section = page.locator('section[data-field-section="description"]')
        description_section.get_by_role("button", name="Вернуть к источнику").click()
        disclosure = description_section.locator(".cpc-description-disclosure")
        disclosure.locator("summary").click()
        assert disclosure.locator(".cpc-description-full").evaluate("el => el.textContent.length") == len(FIXTURE["initial_description"])
        assert disclosure.locator(".cpc-description-full").evaluate(
            "el => getComputedStyle(el).overflowY === 'auto' && el.clientHeight <= 193"
        )
        disclosure.locator("summary").click()
        REPORT["interactions"].append("restore_description_from_inherited_source")
        REPORT["checks"].append("long_inherited_description_has_collapsed_accessible_readback")

        photos_section = page.locator('section[data-field-section="photos"]')
        photos_section.get_by_role("button", name="Изменить значение").click()
        second_photo_up = photos_section.locator(
            'button[data-action="move-photo"][data-photo-url="' + SECOND_PHOTO + '"][data-direction="-1"]'
        )
        second_photo_up.focus()
        second_photo_up.press("Enter")
        first_boundary_selector = (
            'button[data-action="move-photo"][data-photo-url="' + SECOND_PHOTO + '"][data-direction="1"]'
        )
        page.locator(first_boundary_selector).wait_for()
        record_keyboard_focus(
            page,
            "common_photo_boundary_focus_first",
            first_boundary_selector,
            photo_url=SECOND_PHOTO,
            direction="1",
        )

        second_photo_down = photos_section.locator(first_boundary_selector)
        second_photo_down.focus()
        second_photo_down.press("Enter")
        last_boundary_selector = (
            'button[data-action="move-photo"][data-photo-url="' + SECOND_PHOTO + '"][data-direction="-1"]'
        )
        page.locator(last_boundary_selector).wait_for()
        record_keyboard_focus(
            page,
            "common_photo_boundary_focus_last",
            last_boundary_selector,
            photo_url=SECOND_PHOTO,
            direction="-1",
        )

        # Restore the reviewed order required by the save/readback assertions.
        second_photo_up = photos_section.locator(last_boundary_selector)
        second_photo_up.focus()
        second_photo_up.press("Enter")
        REPORT["interactions"].append("keyboard_reorder_selected_source_photos_at_both_boundaries")
        REPORT["checks"].append("common_photo_boundary_focus_first")
        REPORT["checks"].append("common_photo_boundary_focus_last")
        REPORT["checks"].append("common_focus_visible_geometry")

        characteristics_section = page.locator('section[data-field-section="characteristics"]')
        characteristics_section.get_by_role("button", name="Изменить значение").click()
        characteristic_value = characteristics_section.get_by_role("textbox", name="Значение характеристики 1")
        characteristic_value.fill("лен")
        REPORT["interactions"].append("edit_named_characteristic_without_provider_ids")

        recipient = page.locator('.cpc-recipient input[type="checkbox"]').first
        recipient.check()
        page.get_by_role("button", name="Проверить изменения").click()
        page.get_by_role("heading", name="Проверьте изменения общего товара").wait_for()
        page.get_by_text("Карточки каналов останутся как сейчас", exact=False).wait_for()
        preview_card_link = page.locator("#common-content-preview .cpc-preview").get_by_role(
            "link", name="Открыть карточку отдельно", exact=True,
        )
        assert preview_card_link.count() == 1
        preview_card_link.wait_for()
        assert not page.get_by_role("button", name="Сохранить общий товар").is_enabled()
        REPORT["checks"].append("review_diff_is_explicit_and_apply_requires_acknowledgement")

        cancel_preview = page.get_by_role("button", name="Вернуться к полям")
        cancel_preview.focus()
        cancel_preview.press("Enter")
        assert page.locator("#common-content-preview").is_hidden()
        assert not page.locator("#common-content-fields").is_hidden()
        assert page.get_by_role("button", name="Проверить изменения").is_enabled()
        preview_selector = '#common-content-fields [data-action="preview"]'
        record_keyboard_focus(
            page,
            "common_preview_cancel_focus_return",
            preview_selector,
            preview_trigger=True,
        )
        REPORT["checks"].append("common_preview_cancel_focus_return")
        REPORT["checks"].append("preview_cancel_keeps_local_edits_without_writing")

        preview_key = "POST /api/my-products/common-content/preview"
        delay_fetches(page, [preview_key])
        page.get_by_role("button", name="Проверить изменения").click()
        wait_for_delayed_fetch(page, preview_key)
        busy = page.locator("#common-content-editor").evaluate(
            "el => el.inert && el.getAttribute('aria-busy') === 'true'"
        )
        assert busy
        page.evaluate("""secondId => {
            const title = document.querySelector('[data-field-input="title"]');
            title.value = 'Изменение во время задержанного запроса';
            title.dispatchEvent(new Event('input', {bubbles: true}));
            document.querySelector('[data-action="choose-product"][data-product-id="' + secondId + '"]').click();
        }""", product_ids[1])
        release_delayed_fetch(page, preview_key)
        page.get_by_role("heading", name="Проверьте изменения общего товара").wait_for()
        assert page.locator('#common-content-product-list button[aria-current="true"]').get_attribute("data-product-id") == str(product_ids[0])
        photo_diff = page.locator('.cpc-diff-row').filter(has_text="Фотографии")
        before_order = photo_diff.locator('.cpc-diff-value').nth(0).locator('span').last.inner_text()
        after_order = photo_diff.locator('.cpc-diff-value').nth(1).locator('span').last.inner_text()
        assert before_order == "Фото 1 → Фото 2" and after_order == "Фото 2 → Фото 1", (before_order, after_order)
        REPORT["checks"].append("delayed_preview_blocks_edit_and_product_switch")
        REPORT["checks"].append("photo_diff_preserves_identity_when_order_changes_at_same_count")
        page.locator('input[data-action="acknowledge-preview"]').check()
        apply_key = "POST /api/my-products/common-content/apply"
        read_key = "GET /api/my-products/" + str(product_ids[0]) + "/common-content"
        delay_fetches(page, [apply_key, read_key])
        page.get_by_role("button", name="Сохранить общий товар").click()
        wait_for_delayed_fetch(page, apply_key)
        assert page.locator("#common-content-editor").evaluate("el => el.inert")
        page.evaluate(
            "id => document.querySelector('[data-action=choose-product][data-product-id=\"' + id + '\"]').click()",
            product_ids[1],
        )
        release_delayed_fetch(page, apply_key)
        wait_for_delayed_fetch(page, read_key)
        page.evaluate(
            "id => document.querySelector('[data-action=choose-product][data-product-id=\"' + id + '\"]').click()",
            product_ids[1],
        )
        release_delayed_fetch(page, read_key)
        page.get_by_text("Общий товар сохранён", exact=False).wait_for(timeout=10000)
        assert page.locator('#common-content-product-list button[aria-current="true"]').get_attribute("data-product-id") == str(product_ids[0])
        REPORT["checks"].append("signed_preview_apply_and_common_only_success_notice")
        REPORT["checks"].append("delayed_apply_and_readback_keep_captured_product_context")

        with app.app_context():
            saved = db.session.get(ImportedProduct, product_ids[0])
            draft = db.session.get(MarketplaceProductDraft, FIXTURE["draft_id"])
            saved_photos = json.loads(saved.photo_urls or "[]")
            REPORT["synthetic_actions"]["selected_photo_order_persisted"] = saved_photos == [SECOND_PHOTO, PHOTO]
            REPORT["synthetic_actions"]["channel_record_unchanged"] = (
                draft.content_json == FIXTURE["draft_before"]["content_json"]
                and draft.media_json == FIXTURE["draft_before"]["media_json"]
                and draft.version == FIXTURE["draft_before"]["version"]
            )
            assert saved.title == "Товар после проверки общего редактора"
            assert saved.description == FIXTURE["initial_description"]
            assert json.loads(saved.characteristics)[0]["value"] == "лен"
            assert REPORT["synthetic_actions"]["selected_photo_order_persisted"]
            assert REPORT["synthetic_actions"]["channel_record_unchanged"]
        REPORT["checks"].append("database_readback_confirms_common_save_and_no_channel_write")

        # Keep another product's unsaved text local, then cancel it and reopen
        # the page to prove it was not accidentally applied in the batch.
        second_button = page.locator(
            '#common-content-product-list button[data-product-id="{}"]'.format(product_ids[1])
        )
        second_button.click()
        title_section.get_by_role("button", name="Изменить значение", exact=True).click()
        second_title = page.get_by_role("textbox", name="Общее название товара")
        second_title.fill("Несохранённое название")
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Отменить правки товара").click()
        page.get_by_text("Правки отменены", exact=False).wait_for()
        REPORT["synthetic_actions"]["cancelled_local_edits"] = True
        page.reload(wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        second_button = page.locator(
            '#common-content-product-list button[data-product-id="{}"]'.format(product_ids[1])
        )
        second_button.click()
        second_title = page.get_by_role("textbox", name="Общее название товара")
        assert second_title.is_disabled()
        reopened_title = await_text(second_title)
        assert reopened_title == FIXTURE["second_title"], {
            "expected_length": len(FIXTURE["second_title"]),
            "actual_length": len(reopened_title),
            "expected_prefix": FIXTURE["second_title"][:80],
            "actual_prefix": reopened_title[:80],
            "model_editor_limit": FIXTURE["second_title_limit"],
        }
        REPORT["checks"].append("cancel_reopen_discards_unapplied_edits_and_preserves_other_product")

        title_section.get_by_role("button", name="Изменить значение", exact=True).click()
        second_title = page.get_by_role("textbox", name="Общее название товара")
        second_title.fill(FIXTURE["second_title"] + " — ручная правка")
        second_description = page.locator('section[data-field-section="description"]')
        second_description.get_by_role("button", name="Изменить значение").click()
        page.get_by_role("textbox", name="Общее описание товара").fill("")
        beforeunload_blocked = page.evaluate("""() => {
            const event = new Event('beforeunload', {cancelable: true});
            window.dispatchEvent(event);
            return event.defaultPrevented;
        }""")
        assert beforeunload_blocked
        conflict_path = "/api/my-products/common-content/preview"
        conflict_key = "POST " + conflict_path
        delay_fetches(page, [conflict_key])
        page.get_by_role("button", name="Проверить изменения").click()
        wait_for_delayed_fetch(page, conflict_key)
        with app.app_context():
            changed = db.session.get(ImportedProduct, product_ids[1])
            changed.content_edit_version += 1
            db.session.commit()
        EXPECTED_CONFLICTS[conflict_path][0] += 1
        release_delayed_fetch(page, conflict_key)
        error = page.locator("#common-content-error")
        error.wait_for(state="visible")
        assert "Предыдущая проверка отменена" in error.inner_text()
        assert page.locator("#common-content-preview").is_hidden()
        assert not page.locator("#common-content-editor").evaluate("el => el.inert")
        assert page.get_by_role("button", name="Проверить изменения").is_disabled()
        REPORT["checks"].append("stale_preview_conflict_clears_review_and_keeps_actionable_error")
        REPORT["checks"].append("dirty_navigation_triggers_beforeunload_guard")
        assert REPORT["synthetic_actions"]["empty_description_override_requests"] == 1
        assert all(value[0] == 0 for value in EXPECTED_CONFLICTS.values())
        REPORT["checks"].append("intentional_empty_description_remains_override_in_request")

        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Перечитать выбранные").click()
        page.get_by_text("Данные выбранных товаров перечитаны", exact=False).wait_for()
        title_section.get_by_role("button", name="Изменить значение", exact=True).click()
        second_title = page.get_by_role("textbox", name="Общее название товара")
        second_title.fill(FIXTURE["second_title"] + " — проверка конфликта сохранения")
        page.get_by_role("button", name="Проверить изменения").click()
        page.get_by_role("heading", name="Проверьте изменения общего товара").wait_for()
        with app.app_context():
            changed = db.session.get(ImportedProduct, product_ids[1])
            changed.content_edit_version += 1
            db.session.commit()
        apply_conflict_path = "/api/my-products/common-content/apply"
        EXPECTED_CONFLICTS[apply_conflict_path][0] += 1
        page.locator('input[data-action="acknowledge-preview"]').check()
        page.get_by_role("button", name="Сохранить общий товар").click()
        error.wait_for(state="visible")
        assert "Предыдущий diff отменён" in error.inner_text()
        assert page.locator("#common-content-preview").is_hidden()
        assert page.get_by_role("button", name="Проверить изменения").is_disabled()
        assert page.get_by_role("button", name="Сохранить общий товар").count() == 0
        with app.app_context():
            unchanged = db.session.get(ImportedProduct, product_ids[1])
            assert unchanged.title == FIXTURE["second_title"]
        REPORT["checks"].append("stale_apply_conflict_discards_token_and_requires_readback")
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Перечитать выбранные").click()
        page.get_by_text("Данные выбранных товаров перечитаны", exact=False).wait_for()

        # Check the actual seller-facing labels and common-only scope copy.
        # This selected product has no photos, so the fields need not repeat
        # the page-level common-content wording.
        fields = page.locator("#common-content-fields")
        expected_field_headings = {
            "title": "Название",
            "description": "Описание",
            "photos": "Фотографии",
            "characteristics": "Характеристики",
        }
        actual_field_headings = {}
        for field in expected_field_headings:
            heading = fields.locator(
                'section[data-field-section="' + field + '"] h3'
            )
            actual_field_headings[field] = (
                heading.inner_text().strip() if heading.count() == 1 else None
            )
        title_control = fields.get_by_role(
            "textbox", name="Общее название товара", exact=True,
        )
        description_control = fields.get_by_role(
            "textbox", name="Общее описание товара", exact=True,
        )
        recipient_heading = fields.get_by_role(
            "heading", name="Контексты каналов для сравнения", exact=True,
        )
        scope_note = page.locator(".cpc-effect-note")
        scope_note_text = scope_note.inner_text().strip() if scope_note.count() == 1 else ""
        required_scope_copy = (
            "Сохранится только общий товар в Seller Hub.",
            "Черновики и опубликованные карточки на площадках останутся без изменений.",
            "Чтобы применить новые данные к каналу, отдельно проверьте его карточку и подтвердите действие там.",
        )
        label_evidence = {
            "field_headings": actual_field_headings,
            "expected_field_headings": expected_field_headings,
            "title_control_count": title_control.count(),
            "title_control_visible": title_control.is_visible() if title_control.count() == 1 else False,
            "description_control_count": description_control.count(),
            "description_control_visible": description_control.is_visible() if description_control.count() == 1 else False,
            "recipient_heading_count": recipient_heading.count(),
            "scope_note_count": scope_note.count(),
            "scope_note_visible": scope_note.is_visible() if scope_note.count() == 1 else False,
            "scope_copy_present": {
                phrase: phrase in scope_note_text for phrase in required_scope_copy
            },
            "raw_pre_count": page.locator("pre").count(),
            "scope_note_excerpt": scope_note_text[:400],
        }
        REPORT["seller_facing_label_evidence"] = label_evidence
        labels_are_readable = (
            fields.is_visible()
            and actual_field_headings == expected_field_headings
            and title_control.count() == 1
            and title_control.is_visible()
            and description_control.count() == 1
            and description_control.is_visible()
            and recipient_heading.count() == 1
            and scope_note.count() == 1
            and scope_note.is_visible()
            and all(label_evidence["scope_copy_present"].values())
            and label_evidence["raw_pre_count"] == 0
        )
        assert labels_are_readable, label_evidence
        REPORT["checks"].append("seller_facing_labels_replace_raw_json")

        _run_bulk_50_case(page)
        _run_common_photo_retry_case(page)

        assert REPORT["synthetic_actions"]["preview_requests"] == 7
        assert REPORT["synthetic_actions"]["apply_requests"] == 4
        assert REPORT["synthetic_actions"]["expected_preview_conflicts"] == 1
        assert REPORT["synthetic_actions"]["expected_apply_conflicts"] == 2
        assert REPORT["synthetic_actions"]["empty_description_override_requests"] == 1
        assert REPORT["provider_attempts"] == 0
        assert REPORT["unexpected_http_requests"] == []
        assert REPORT["unexpected_external_requests"] == []
        assert REPORT["javascript_errors"] == []
        expected_console_error_counts = Counter({
            ("POST", path, 409): count
            for path, count in EXPECTED_CONFLICT_CONSOLE_COUNTS.items()
        })
        observed_console_error_counts = Counter(
            (row.get("method"), row.get("path"), row.get("status"))
            for row in REPORT["expected_conflict_console_errors"]
        )
        console_evidence = {
            "expected_by_endpoint": {
                path: count for path, count in EXPECTED_CONFLICT_CONSOLE_COUNTS.items()
            },
            "observed_by_endpoint": {
                path: sum(
                    1 for row in REPORT["expected_conflict_console_errors"]
                    if row.get("path") == path
                )
                for path in EXPECTED_CONFLICT_CONSOLE_COUNTS
            },
            "pending_exact_conflict_responses": PENDING_EXPECTED_CONFLICT_CONSOLES,
            "unexpected_console_errors": REPORT["console_errors"],
            "unexpected_console_locations": REPORT["console_error_locations"],
        }
        assert observed_console_error_counts == expected_console_error_counts, console_evidence
        assert PENDING_EXPECTED_CONFLICT_CONSOLES == [], console_evidence
        expected_rejection_console_counts = Counter(EXPECTED_REJECTION_CONSOLE_COUNTS)
        observed_rejection_console_counts = Counter(
            (
                row.get("method"), row.get("path"), row.get("status"),
                row.get("code"), row.get("has_query"),
            )
            for row in REPORT["expected_rejection_console_errors"]
        )
        rejection_console_evidence = {
            "expected_by_exact_receipt": {
                str(key): count for key, count in EXPECTED_REJECTION_CONSOLE_COUNTS.items()
            },
            "observed_by_exact_receipt": {
                str(key): count for key, count in observed_rejection_console_counts.items()
            },
            "pending_http_rejections": PENDING_EXPECTED_REJECTION_CONSOLES,
            "console_errors": REPORT["console_errors"],
            "console_error_locations": REPORT["console_error_locations"],
        }
        assert observed_rejection_console_counts == expected_rejection_console_counts, rejection_console_evidence
        assert PENDING_EXPECTED_REJECTION_CONSOLES == [], rejection_console_evidence
        assert not any(EXPECTED_HTTP_REJECTIONS.values())
        assert len(REPORT["expected_http_rejections"]) == 2
        assert REPORT["console_errors"] == []
        REPORT["checks"].append("expected_conflict_console_errors_scoped_by_endpoint_and_count")
        REPORT["checks"].append("expected_413_console_rejections_scoped_by_receipt_endpoint_query_code_and_count")
        assert len(REPORT["layouts"]) == 28
        assert len(REPORT["checks"]) >= 8
        REPORT["status"] = "passed"
        browser.close()


def await_text(locator):
    return locator.input_value()


def main():
    try:
        run()
    except Exception as exc:
        REPORT["status"] = "failed"
        REPORT["error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        server.shutdown()
        server_thread.join(timeout=5)
        TEMP.cleanup()
        print(json.dumps({
            "source": SOURCE,
            "status": REPORT["status"],
            "checks": len(REPORT["checks"]),
            "layouts": len(REPORT["layouts"]),
            "writes": len(REPORT["writes"]),
            "provider_attempts": REPORT["provider_attempts"],
            "report": str(REPORT_PATH),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
