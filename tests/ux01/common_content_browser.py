"""Synthetic browser acceptance for seller-owned common product content.

The editor runs against the real Flask routes, SQLAlchemy models, signed
preview/apply service and CSRF checks. The database and seller are synthetic;
provider networking is blocked. Expected preview/apply POSTs are counted
separately from provider attempts.
"""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import requests
from flask import url_for
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


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
    "browser_api_reads": [],
    "writes": [],
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
    "javascript_errors": [],
    "console_errors": [],
    "provider_attempts": 0,
}
EXPECTED_CONFLICTS = {
    "/api/my-products/common-content/preview": [0],
    "/api/my-products/common-content/apply": [0],
}
SHARED_SHELL_READ_PATH_CATEGORIES = {
    "/api/notifications/unread-count": "notifications_unread_count",
    "/api/tasks/tray": "background_tasks_tray",
}

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
from models import ImportedProduct, MarketplaceProductDraft, Seller, db
from routes.common_product_content import register_common_product_content_routes
from services.common_product_content import CommonProductContentService
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
    second = ImportedProduct(
        seller_id=FIXTURE["seller_id"],
        external_id="COMMON-CONTENT-SECOND-" + ("X" * 180),
        source_type="manual",
        title="Длинное название товара " + ("безразрывного-текста-" * 35),
        description="Второе описание.",
        original_data=json.dumps({
            "title": "Второй синтетический товар",
            "description": "Второе описание.",
        }, ensure_ascii=False),
    )
    db.session.add(second)
    db.session.commit()
    FIXTURE["second_source_id"] = second.id
    FIXTURE["second_title"] = second.title
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
                if any(
                    isinstance(item, dict)
                    and isinstance(item.get("changes"), dict)
                    and item["changes"].get("description") == {"mode": "override", "value": ""}
                    for item in preview_body.get("items", [])
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
            if (
                request.method == "POST"
                and response.status == 409
                and EXPECTED_CONFLICTS.get(parsed.path, [0])[0] > 0
            ):
                EXPECTED_CONFLICTS[parsed.path][0] -= 1
                conflict_key = "expected_apply_conflicts" if parsed.path.endswith("/apply") else "expected_preview_conflicts"
                REPORT["synthetic_actions"][conflict_key] += 1
            else:
                REPORT["unexpected_http_requests"].append({"method": request.method, "path": parsed.path, "status": response.status})
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
        request_key,
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
    if state == "selected" and width in (320, 360, 390):
        REPORT["mobile_touch_target_observations"].append(
            measure_mobile_touch_targets(page, width, theme)
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


def assert_mobile_touch_target_evidence() -> None:
    rows = REPORT["mobile_touch_target_observations"]
    expected = {
        (width, theme)
        for width in (320, 360, 390)
        for theme in ("light", "dark")
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
        page.on("console", lambda message: REPORT["console_errors"].append(message.text[:300])
                if message.type == "error" else None)

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
        page.get_by_role("button", name="Изменить значение").first.click()
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
        page.get_by_text("Открыть карточку отдельно", exact=True).wait_for()
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
        second_button = page.locator("#common-content-product-list button").nth(1)
        second_button.click()
        page.get_by_role("button", name="Изменить значение").first.click()
        second_title = page.get_by_role("textbox", name="Общее название товара")
        second_title.fill("Несохранённое название")
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Отменить правки товара").click()
        page.get_by_text("Правки отменены", exact=False).wait_for()
        REPORT["synthetic_actions"]["cancelled_local_edits"] = True
        page.reload(wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        second_button = page.locator("#common-content-product-list button").nth(1)
        second_button.click()
        second_title = page.get_by_role("textbox", name="Общее название товара")
        assert second_title.is_disabled()
        assert await_text(second_title) == FIXTURE["second_title"]
        REPORT["checks"].append("cancel_reopen_discards_unapplied_edits_and_preserves_other_product")

        page.get_by_role("button", name="Изменить значение").first.click()
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
        page.get_by_role("button", name="Изменить значение").first.click()
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

        # Verify the common editor is not a raw provider payload screen.
        assert page.locator("pre").count() == 0
        assert page.locator("#common-content-fields").get_by_text("общий товар", exact=False).count() > 0
        REPORT["checks"].append("seller_facing_labels_replace_raw_json")

        assert REPORT["synthetic_actions"]["preview_requests"] == 4
        assert REPORT["synthetic_actions"]["apply_requests"] == 2
        assert REPORT["synthetic_actions"]["expected_preview_conflicts"] == 1
        assert REPORT["synthetic_actions"]["expected_apply_conflicts"] == 1
        assert REPORT["synthetic_actions"]["empty_description_override_requests"] == 1
        assert REPORT["provider_attempts"] == 0
        assert REPORT["unexpected_http_requests"] == []
        assert REPORT["unexpected_external_requests"] == []
        assert REPORT["javascript_errors"] == []
        assert REPORT["console_errors"] == []
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
