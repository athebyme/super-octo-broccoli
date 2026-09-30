"""Synthetic before/after browser evidence for the Ozon preparation journey.

The runner starts the real Flask/Jinja/Vue app on loopback with a disposable
SQLite database. Python and browser network calls outside the allowlisted local
server/static manifest are denied. It performs login only; no browser form is
submitted and no provider, upload, review-write, or publication API is called.

Environment:
  UX01_JOURNEY_SOURCE=ba63371|worktree
  UX01_JOURNEY_ARTIFACTS=/tmp/ux01-journey
  UX01_JOURNEY_REPORT=/tmp/ux01-journey/report.json
  CHROMIUM_BIN=/usr/bin/chromium
"""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
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
from urllib.parse import parse_qs, urlsplit

import requests
from jinja2 import ChoiceLoader, DictLoader
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ASSETS = ROOT / "tests/ozon_release/assets"
SOURCE = os.environ.get("UX01_JOURNEY_SOURCE", "worktree").strip().lower()
if SOURCE not in {"ba63371", "worktree"}:
    raise ValueError("UX01_JOURNEY_SOURCE must be ba63371 or worktree")
OUT = Path(os.environ.get("UX01_JOURNEY_ARTIFACTS", "/tmp/ux01-journey"))
OUT.mkdir(parents=True, exist_ok=True)
REPORT_PATH = Path(os.environ.get("UX01_JOURNEY_REPORT", str(OUT / "report.json")))
CHROMIUM = os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium")
TEMP = tempfile.TemporaryDirectory(prefix="ux01-journey-browser-")
TEMP_PATH = Path(TEMP.name)
REPORT = {
    "source": SOURCE,
    "status": "running",
    "scope": "synthetic_flask_jinja_vue_preparation_journey",
    "pages": [],
    "layouts": [],
    "geometry": [],
    "checks": [],
    "interactions": [],
    "screenshots": [],
    "javascript_errors": [],
    "unexpected_http": [],
    "unexpected_external_requests": [],
    "provider_attempts": 0,
    "writes": [],
}
ACTIVE_PAGE = None

os.environ.update({
    "DATABASE_URL": "sqlite:///" + str(TEMP_PATH / "app.sqlite"),
    "SKIP_SCHEDULER": "1",
    "IMAGE_LAB_INLINE_WORKER": "0",
    "SECRET_KEY": "synthetic-ux01-journey",
    "ENCRYPTION_KEY": base64.urlsafe_b64encode(b"j" * 32).decode(),
    "MARKETPLACE_OZON_ENABLED": "1",
    "MARKETPLACE_OZON_PUBLICATION_ENABLED": "0",
    "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED": "0",
    "MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED": "0",
    "OZON_RATE_LIMIT_DIR": str(TEMP_PATH / "limits"),
})


def forbid_network(*_args, **_kwargs):
    REPORT["provider_attempts"] += 1
    raise AssertionError("External Python network is forbidden in this fixture")


requests.sessions.Session.request = forbid_network
socket.create_connection = forbid_network

from seller_platform import app
from models import SellerSupplier, Supplier, SupplierProduct, db
from services.ozon_bulk_upload import OzonBulkUploadService
from tests.ozon_release.seed import PASSWORD, PHOTO, USERNAME, seed

app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False)
FIXTURE = seed(app)
with app.app_context():
    supplier = Supplier(
        name="UX Synthetic Supplier",
        code="ux01-synthetic-supplier",
        description="Synthetic supplier fixture; no network source configured.",
    )
    db.session.add(supplier)
    db.session.flush()
    db.session.add(SellerSupplier(
        seller_id=FIXTURE["seller_id"], supplier_id=supplier.id, is_active=True,
    ))
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="UX01-SOURCE-44",
        vendor_code="UX01-44",
        title="Synthetic source product",
        description="Synthetic source facts for browser rendering.",
        brand="Fixture",
        category="Synthetic category",
        supplier_price=100,
        supplier_quantity=4,
        currency="RUB",
        supplier_status="in_stock",
        photo_urls_json="[]",
        characteristics_json="[]",
        original_data_json=json.dumps({"title": "Synthetic source product"}),
        status="ready",
    )
    db.session.add(supplier_product)
    db.session.commit()
    FIXTURE["supplier_id"] = supplier.id
    FIXTURE["supplier_product_id"] = supplier_product.id

    # This is a local-only synthetic row used to exercise the existing result
    # page. Scheduler is disabled; no Ozon/provider operation is created.
    acceptance = OzonBulkUploadService.accept_source_prepare(
        seller_id=FIXTURE["seller_id"],
        account_id=FIXTURE["account_id"],
        imported_product_ids=[FIXTURE["source_id"]],
        request_key="UX01-JOURNEY-SOURCE-PREPARE-0001",
    )
    FIXTURE["run_uid"] = acceptance.job.job_uid

logging.getLogger("werkzeug").setLevel(logging.ERROR)
server = make_server("127.0.0.1", 0, app, threaded=True)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
BASE = f"http://127.0.0.1:{server.server_port}"

MANIFEST = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
for asset in MANIFEST.values():
    content = (ASSETS / asset["file"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == asset["sha256"]

TEMPLATE_PATHS = (
    "templates/base.html",
    "templates/supplier_catalog.html",
    "templates/supplier_catalog_products.html",
    "templates/supplier_catalog_product_detail.html",
    "templates/seller_my_products.html",
    "templates/my_products_beta.html",
    "templates/marketplace_drafts.html",
    "templates/marketplace_drafts_classic.html",
    "templates/marketplace_draft_detail.html",
    "templates/marketplace_draft_detail_classic.html",
    "templates/ozon_upload_review.html",
    "templates/ozon_bulk_uploads.html",
    "templates/ozon_bulk_upload_detail.html",
    "templates/marketplace_listing_detail.html",
    "templates/marketplace_listing_beta_detail.html",
)
if SOURCE == "ba63371":
    overrides = {}
    for path in TEMPLATE_PATHS:
        raw = subprocess.run(
            ["git", "show", "ba63371:" + path], cwd=ROOT, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout
        overrides[path.removeprefix("templates/")] = raw.decode("utf-8")
    app.jinja_env.loader = ChoiceLoader([DictLoader(overrides), app.jinja_env.loader])
    app.jinja_env.cache.clear()

with app.test_request_context("/"):
    from flask import url_for
    PAGE_PATHS = {
        "supplier_catalog": url_for("supplier_catalog"),
        "supplier_products": url_for("supplier_catalog_products", supplier_id=FIXTURE["supplier_id"], stock_status="all"),
        "supplier_source_detail": url_for(
            "supplier_catalog_product_detail",
            supplier_id=FIXTURE["supplier_id"],
            product_id=FIXTURE["supplier_product_id"],
        ),
        "internal_ozon": url_for("seller_my_products", account_id=FIXTURE["account_id"]),
        "internal_beta": url_for("seller_my_products_beta"),
        "drafts_vue": url_for("marketplace_drafts.index", account_id=FIXTURE["account_id"]),
        "drafts_classic": url_for("marketplace_drafts.classic_index", account_id=FIXTURE["account_id"]),
        "draft_detail_vue": url_for("marketplace_drafts.detail", draft_id=FIXTURE["draft_id"]),
        "draft_detail_classic": url_for("marketplace_drafts.classic_detail", draft_id=FIXTURE["draft_id"]),
        # Flask registers the JSON alias last for the shared view endpoint;
        # browser coverage must exercise the HTML review page explicitly.
        "review": url_for(
            "ozon_bulk_uploads.review",
            account_id=FIXTURE["account_id"],
            draft_ids=FIXTURE["draft_id"],
        ).replace("/api/review", "/review"),
        "upload_history": url_for("ozon_bulk_uploads.index", account_id=FIXTURE["account_id"]),
        "upload_result": url_for("ozon_bulk_uploads.detail", job_uid=FIXTURE["run_uid"]),
        "listing_vue": url_for("marketplace_listings.view", listing_id=FIXTURE["listing_ids"][0]),
        "listing_classic": url_for("marketplace_listings.detail", listing_id=FIXTURE["listing_ids"][0]),
    }


def baseline_bytes(path: str) -> bytes:
    return subprocess.run(
        ["git", "show", "ba63371:" + path], cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def bridge(route):
    request = route.request
    parts = urlsplit(request.url)
    if parts.hostname == "127.0.0.1" and parts.port == server.server_port:
        if request.method not in ("GET", "HEAD"):
            assert parts.path == "/login", (request.method, parts.path)
            REPORT["writes"].append({"method": request.method, "path": parts.path})
        if parts.path == "/favicon.ico":
            route.fulfill(status=204, body=b"")
            return
        if SOURCE == "ba63371" and parts.path.startswith("/static/"):
            relative = parts.path.removeprefix("/static/")
            candidate = "static/" + relative
            if candidate in {
                "static/marketplace-catalog-beta.js",
                "static/marketplace-detail-beta.js",
            }:
                route.fulfill(
                    body=baseline_bytes(candidate),
                    content_type="application/javascript; charset=utf-8",
                )
                return
        response = route.fetch(max_redirects=0)
        if response.status >= 400:
            REPORT["unexpected_http"].append({"path": parts.path, "status": response.status})
        route.fulfill(response=response)
        return
    if request.url == PHOTO:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="300"><rect width="240" height="300" fill="#e8e5df"/><rect x="60" y="50" width="120" height="200" rx="10" fill="#566b79"/></svg>'
        route.fulfill(body=svg, content_type="image/svg+xml")
        return
    asset = MANIFEST.get(request.url) or MANIFEST.get(request.url.rstrip("/"))
    if asset:
        route.fulfill(
            body=(ASSETS / asset["file"]).read_bytes(),
            content_type=asset["content_type"],
        )
        return
    REPORT["unexpected_external_requests"].append({
        "host": parts.hostname, "path": parts.path, "method": request.method,
    })
    route.abort()


def wait_document(page, name: str, path: str):
    response = page.goto(BASE + path, wait_until="domcontentloaded")
    assert response and response.status == 200, (
        name, path, response.status if response else None,
    )
    page.wait_for_load_state("networkidle")
    page.locator("body").wait_for()
    REPORT["pages"].append({"name": name, "path": urlsplit(path).path, "status": response.status})


def synthetic_login_cookie() -> dict:
    """Authenticate the browser through Flask's test client, outside HTTP telemetry."""
    client = app.test_client()
    login_page = client.get("/login")
    assert login_page.status_code == 200, login_page.status_code
    match = re.search(
        rb'name="csrf_token" value="([^"]+)"', login_page.data,
    )
    assert match, "Synthetic login form did not provide a CSRF token"
    response = client.post(
        "/login",
        data={
            "csrf_token": match.group(1).decode("ascii"),
            "username": USERNAME,
            "password": PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), (
        "Synthetic login failed", response.status_code,
    )
    cookie = client.get_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
    assert cookie is not None, "Synthetic login did not create an authenticated cookie"
    return {"name": cookie.key, "value": cookie.value, "url": BASE}


def record_check(name: str):
    assert not REPORT["javascript_errors"], REPORT["javascript_errors"]
    assert not REPORT["unexpected_http"], REPORT["unexpected_http"]
    REPORT["checks"].append(name)
    REPORT["interactions"].append(name)


def set_theme(page, theme: str):
    page.evaluate("theme => { localStorage.setItem('sh-theme', theme); document.documentElement.dataset.theme = theme; }", theme)
    actual = page.evaluate("document.documentElement.dataset.theme")
    assert actual == theme, {"expected_theme": theme, "actual_theme": actual}
    return actual


def journey_assertions(page, name: str, current: str, account_id: int | None = None):
    section = page.locator("section.preparation-journey")
    section.wait_for()
    text = section.inner_text()
    assert "Канал:" in text and "Ozon" in text, (name, text)
    assert "Дальше:" in text, (name, text)
    assert "Объект:" in text, (name, text)
    assert section.locator("h2").inner_text().startswith("Сейчас:"), (name, text)
    assert section.locator("details").count() == 1
    assert section.locator("details[open]").count() == 0
    assert section.locator("li[aria-current=step]").count() == 1
    # The seven-step path lives inside a closed disclosure, so use its DOM text
    # while separately verifying the current/next summary is visible above it.
    current_label = section.locator("li.is-current strong").text_content().strip()
    assert current_label == current, {
        "page": name,
        "expected_current": current,
        "actual_current": current_label,
        "journey_text": text,
    }
    assert section.locator("li.is-complete").count() == 0
    for href in section.locator("a").evaluate_all("items => items.map(item => item.getAttribute('href'))"):
        assert href and href.startswith("/") and not href.startswith("//"), (name, href)
    if account_id is not None and current != "Внутренний товар":
        internal_href = section.locator("li").filter(has_text="Внутренний товар").locator("a").get_attribute("href")
        assert internal_href, (name, "internal step must remain a link")
        assert parse_qs(urlsplit(internal_href).query).get("account_id") == [str(account_id)], (name, internal_href)
    section.locator("summary").focus()
    page.keyboard.press("Enter")
    assert section.locator("details[open]").count() == 1, name
    assert section.locator("li").count() == 7, name
    first_link = section.locator("ol a").first
    assert first_link.count() == 1, name
    page.keyboard.press("Tab")
    focus = first_link.evaluate("""el => {
        const parse = value => {
            const match = value.match(/^rgba?\\(([^)]+)\\)$/);
            if (!match) return null;
            const parts = match[1].split(/[,\\s/]+/).filter(Boolean).map(Number);
            return parts.length < 3 ? null : {rgb:parts.slice(0,3), alpha:Number.isFinite(parts[3]) ? parts[3] : 1};
        };
        const blend = (front, back) => front.rgb.map((value,index) => value*front.alpha+back.rgb[index]*(1-front.alpha));
        const bodyBg = parse(getComputedStyle(document.body).backgroundColor) || {rgb:[255,255,255],alpha:1};
        let bg = bodyBg.rgb;
        const parents=[]; for(let node=el.parentElement;node;node=node.parentElement) parents.unshift(node);
        parents.forEach(node => { const color=parse(getComputedStyle(node).backgroundColor); if(color && color.alpha>0) bg=blend(color,{rgb:bg,alpha:1}); });
        const outline=parse(getComputedStyle(el).outlineColor);
        const visible=outline ? blend(outline,{rgb:bg,alpha:1}) : null;
        const lum=rgb=>{const c=rgb.map(v=>{v/=255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4)});return .2126*c[0]+.7152*c[1]+.0722*c[2]};
        const contrast=visible ? (Math.max(lum(visible),lum(bg))+.05)/(Math.min(lum(visible),lum(bg))+.05) : null;
        return {active:document.activeElement===el,visible:el.matches(':focus-visible'),width:getComputedStyle(el).outlineWidth,color:getComputedStyle(el).outlineColor,contrast,background:bg};
    }""")
    assert focus["active"] and focus["visible"], (name, focus)
    width = float(focus["width"].removesuffix("px"))
    assert width >= 2, (name, focus)
    assert focus["contrast"] is not None and focus["contrast"] >= 3, (name, focus)
    targets = section.evaluate("el => ({summary:el.querySelector('summary').getBoundingClientRect().height,links:Array.from(el.querySelectorAll('ol a')).map(a=>a.getBoundingClientRect().height)})")
    assert targets["summary"] >= 44 and all(height >= 44 for height in targets["links"]), (name, targets)
    REPORT["interactions"].append({"name": name + "_disclosure_and_keyboard_focus", "focus": focus})
    REPORT["checks"].append(name + "_seven_steps_and_current_next")
    return section


def geometry(page, name: str, width: int, theme: str):
    page.set_viewport_size({"width": width, "height": 1000})
    page.wait_for_function("!document.querySelector('.main-content')?.getAnimations().some(a => a.playState === 'running')", timeout=2500)
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    actual = set_theme(page, theme)
    result = page.evaluate("""() => {
        const roots = Array.from(document.querySelectorAll('.preparation-journey, .preparation-listing-end-state'));
        return roots.map(el => {
            const rect = el.getBoundingClientRect();
            const disclosure = el.querySelector('.preparation-journey-path ol');
            const nav = el.querySelector('nav');
            return {
                kind: el.classList.contains('preparation-journey') ? 'journey' : 'listing_end_state',
                left: Math.round(rect.left), right: Math.round(rect.right), width: Math.round(rect.width),
                clientWidth: el.clientWidth, scrollWidth: el.scrollWidth,
                pathClientWidth: disclosure?.clientWidth ?? null,
                pathScrollWidth: disclosure?.scrollWidth ?? null,
                navClientWidth: nav?.clientWidth ?? null, navScrollWidth: nav?.scrollWidth ?? null,
                touchTargets: Array.from(el.querySelectorAll('a')).map(a => Math.round(a.getBoundingClientRect().height)),
                summaryHeight: el.querySelector('summary') ? Math.round(el.querySelector('summary').getBoundingClientRect().height) : null,
            };
        });
    }""")
    assert result, (name, width, theme, "no journey/end state")
    for item in result:
        assert item["left"] >= -1 and item["right"] <= width + 1, (name, width, theme, item)
        assert item["scrollWidth"] <= item["clientWidth"] + 1, (name, width, theme, item)
        if item["pathClientWidth"] is not None:
            assert item["pathScrollWidth"] <= item["pathClientWidth"] + 1, (name, width, theme, item)
        if item["navClientWidth"] is not None:
            assert item["navScrollWidth"] <= item["navClientWidth"] + 1, (name, width, theme, item)
        if width <= 620:
            assert item["summaryHeight"] is None or item["summaryHeight"] >= 44, (name, width, theme, item)
            assert all(height >= 44 for height in item["touchTargets"]), (name, width, theme, item)
    row = {"page": name, "width": width, "theme": theme, "actual_theme": actual, "components": result}
    REPORT["geometry"].append(row)
    REPORT["layouts"].append(row)
    return row


def screenshot(page, name: str, width: int, theme: str):
    # Interaction assertions open the disclosure and move focus into its first
    # link. Snapshots should show the page's default compact composition.
    expanded_paths = page.locator(".preparation-journey-path[open]")
    for index in range(expanded_paths.count()):
        expanded_paths.nth(index).evaluate("element => { element.open = false; }")
    page.evaluate("() => { if (document.activeElement?.closest('.preparation-journey-path')) document.activeElement.blur(); }")
    page.set_viewport_size({"width": width, "height": 960})
    page.wait_for_function("!document.querySelector('.main-content')?.getAnimations().some(a => a.playState === 'running')", timeout=2500)
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    set_theme(page, theme)
    path = OUT / f"journey-{SOURCE}-{name}-{theme}-{width}.png"
    page.screenshot(path=str(path), full_page=True, animations="disabled")
    REPORT["screenshots"].append({"page": name, "theme": theme, "width": width, "path": str(path)})


def text_200(page, name: str, width: int, theme: str):
    page.set_viewport_size({"width": width, "height": 1000})
    set_theme(page, theme)
    component = page.locator(".preparation-journey")
    assert component.count() == 1, name
    before = component.evaluate("el => ({width:el.clientWidth, links:el.querySelectorAll('a').length, summaries:el.querySelectorAll('summary').length})")
    page_controls = page.evaluate("() => ({forms:document.querySelectorAll('form').length, buttons:document.querySelectorAll('button').length})")
    result = component.evaluate("""async root => {
        const elements = Array.from(root.querySelectorAll('*')).filter(el => Array.from(el.childNodes).some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim()));
        const originals = elements.map(el => ({
            el,
            value: el.style.getPropertyValue('font-size'),
            priority: el.style.getPropertyPriority('font-size'),
        }));
        const sizes = elements.map(el => [el, parseFloat(getComputedStyle(el).fontSize)]);
        const summary = root.querySelector('summary');
        const summaryIncluded = sizes.some(([el]) => el === summary);
        const summaryChildNodes = Array.from(summary.childNodes).map(node => ({
            type: node.nodeType,
            name: node.nodeName,
            text: node.textContent.trim(),
        }));
        const summaryFontBefore = parseFloat(getComputedStyle(summary).fontSize);
        const summaryInlineBefore = summary.style.getPropertyValue('font-size');
        const summaryPriorityBefore = summary.style.getPropertyPriority('font-size');
        const waitForFontSizeTransitions = async () => {
            // Reduced-motion CSS still uses a 10µs transition. Chromium reports
            // the old computed value until that transition reaches its target.
            root.getBoundingClientRect();
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            root.getBoundingClientRect();
        };
        let scaledMeasurement;
        try {
            sizes.forEach(([el, size]) => el.style.setProperty('font-size', `${size * 2}px`, 'important'));
            const summaryInlineAfterWrite = summary.style.getPropertyValue('font-size');
            const summaryPriorityAfterWrite = summary.style.getPropertyPriority('font-size');
            await waitForFontSizeTransitions();
            const clientWidth = root.clientWidth;
            const scrollWidth = root.scrollWidth;
            const right = root.getBoundingClientRect().right;
            const summaryComputedAfterWrite = parseFloat(getComputedStyle(summary).fontSize);
            scaledMeasurement = {
                clientWidth,
                scrollWidth,
                right,
                links: root.querySelectorAll('a').length,
                summaries: root.querySelectorAll('summary').length,
                summaryFontAfter: summaryComputedAfterWrite,
                summaryInlineAfterWrite,
                summaryPriorityAfterWrite,
            };
        } finally {
            originals.forEach(({el, value, priority}) => {
                if (value) el.style.setProperty('font-size', value, priority);
                else el.style.removeProperty('font-size');
            });
            await waitForFontSizeTransitions();
        }
        const inlineStylesRestored = originals.every(({el, value, priority}) =>
            el.style.getPropertyValue('font-size') === value &&
            el.style.getPropertyPriority('font-size') === priority
        );
        return {
            scaled: sizes.length,
            maxFontBefore: Math.max(...sizes.map(row => row[1])),
            summaryIncluded,
            summaryChildNodes,
            summaryFontBefore,
            summaryInlineBefore,
            summaryPriorityBefore,
            summaryFontRestored: parseFloat(getComputedStyle(summary).fontSize),
            inlineStylesRestored,
            measured: scaledMeasurement,
        };
    }""")
    measured = result["measured"]
    assert result["scaled"] > 0, (name, result)
    assert result["summaryIncluded"], (name, width, theme, "summary must be included in text scaling", result)
    summary_ratio = measured["summaryFontAfter"] / result["summaryFontBefore"]
    restored_ratio = result["summaryFontRestored"] / result["summaryFontBefore"]
    assert 1.99 <= summary_ratio <= 2.01, (name, width, theme, "summary should scale exactly 200%", result)
    assert 0.99 <= restored_ratio <= 1.01, (name, width, theme, "summary font must be restored", result)
    assert result["inlineStylesRestored"], (name, width, theme, "inline font styles must be restored", result)
    assert measured["scrollWidth"] <= measured["clientWidth"] + 1, (name, width, theme, measured)
    assert measured["right"] <= width + 1, (name, width, theme, measured)
    assert measured["links"] == before["links"] and measured["summaries"] == before["summaries"], (name, before, measured)
    assert page.evaluate("() => ({forms:document.querySelectorAll('form').length, buttons:document.querySelectorAll('button').length})") == page_controls
    REPORT.setdefault("text_200", []).append({
        "page": name,
        "width": width,
        "theme": theme,
        "scaled_text_nodes": result["scaled"],
        "summary_included_in_scale": result["summaryIncluded"],
        "summary_child_nodes": result["summaryChildNodes"],
        "summary_font_before": result["summaryFontBefore"],
        "summary_inline_before": result["summaryInlineBefore"],
        "summary_priority_before": result["summaryPriorityBefore"],
        "summary_font_after": measured["summaryFontAfter"],
        "summary_inline_after_write": measured["summaryInlineAfterWrite"],
        "summary_priority_after_write": measured["summaryPriorityAfterWrite"],
        "summary_scale_ratio": summary_ratio,
        "summary_font_restored": result["summaryFontRestored"],
        "inline_styles_restored": result["inlineStylesRestored"],
        **measured,
    })


def capture_failure(page, exc):
    REPORT["status"] = "failed"
    REPORT["failure"] = {"type": type(exc).__name__, "message": str(exc)[:600]}
    if page is not None and not page.is_closed():
        try:
            page.screenshot(path=str(OUT / f"journey-{SOURCE}-failure.png"), full_page=True, animations="disabled")
            REPORT["failure_page"] = {
                "url": page.url,
                "title": page.title(),
                "visible_text": page.locator("body").inner_text()[:5000],
            }
        except Exception as screenshot_error:
            REPORT["failure_screenshot_error"] = f"{type(screenshot_error).__name__}: {screenshot_error}"


def run():
    global ACTIVE_PAGE
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=CHROMIUM,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="SellerHubUX01Journey/1.0",
            viewport={"width": 1440, "height": 960},
            timezone_id="Europe/Moscow",
            reduced_motion="reduce",
            service_workers="block",
        )
        context.add_cookies([synthetic_login_cookie()])
        context.route("**/*", bridge)
        page = context.new_page()
        ACTIVE_PAGE = page
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))

        REPORT["checks"].append("synthetic_test_client_login_with_csrf")

        paths = [
            ("supplier_catalog", "supplier_catalog", "Источник поставщика"),
            ("supplier_products", "supplier_products", "Источник поставщика"),
            ("supplier_source_detail", "supplier_source_detail", "Источник поставщика"),
            ("internal_ozon", "internal_ozon", "Внутренний товар"),
            ("drafts_vue", "drafts_vue", "Черновик Ozon"),
            ("drafts_classic", "drafts_classic", "Черновик Ozon"),
            ("draft_detail_vue", "draft_detail_vue", "Черновик Ozon"),
            ("draft_detail_classic", "draft_detail_classic", "Черновик Ozon"),
            ("review", "review", "Свежая проверка"),
            ("upload_history", "upload_history", "Результат операции"),
            ("upload_result", "upload_result", "Результат операции"),
        ]
        macro_pages = []
        for name, key, current in paths:
            wait_document(page, name, PAGE_PATHS[key])
            if SOURCE == "worktree":
                scoped_account = None if name in {"supplier_catalog", "supplier_products", "supplier_source_detail"} else FIXTURE["account_id"]
                section = journey_assertions(page, name, current, scoped_account)
                macro_pages.append((name, PAGE_PATHS[key], current))
                if name == "draft_detail_vue":
                    page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
                    assert page.locator("#ode-name").count() == 1
                    assert page.get_by_role("button", name="Сохранить", exact=True).count() == 1
                    assert page.get_by_role("button", name="Отправить в Ozon", exact=False).count() == 1
                    REPORT["checks"].append("vue_draft_form_save_and_separate_publish_controls_preserved")
                if name == "draft_detail_classic":
                    assert page.locator('form[action*="validate"]').count() == 1
                    assert page.locator('form[action*="refresh-facts"]').count() == 1
                    REPORT["checks"].append("classic_draft_validation_and_refresh_actions_preserved")
                if name == "review":
                    page.locator("#ozon-upload-review:not([v-cloak])").wait_for()
                    assert page.locator("input[type=checkbox]").count() >= 1
                    assert page.locator("button").filter(has_text="Проверить выбранные").count() >= 1
                    assert page.locator("button").filter(has_text="Отправить проверенные карточки").count() >= 1
                    assert "Сохраните" not in section.inner_text()
                    REPORT["checks"].append("review_exact_set_selection_and_explicit_write_controls_preserved")
                if name == "internal_ozon":
                    assert page.locator(".sh-channel-bar a").filter(has_text="Wildberries").count() >= 1
                    assert page.locator(".sh-channel-bar a").filter(has_text="Ozon").count() >= 1
                    REPORT["checks"].append("wb_channel_and_ozon_contextual_actions_coexist")
            else:
                # Before snapshot uses the same real routes and fixture, but the
                # baseline intentionally has no new journey partial.
                assert page.locator("body").inner_text().strip()

            if name in {"supplier_source_detail", "internal_ozon", "draft_detail_vue", "review", "upload_result"}:
                for theme in ("light", "dark"):
                    for width in (1440, 390):
                        screenshot(page, name, width, theme)

        # The beta seller list already has Ozon-specific action cards and must
        # keep them alongside the existing WB row actions; no static account
        # identity is inserted into its reactive multi-channel table.
        wait_document(page, "internal_beta", PAGE_PATHS["internal_beta"])
        page.locator("#my-products-app").wait_for()
        if SOURCE == "worktree":
            page.locator("#my-products-app:not([v-cloak])").wait_for()
            page.locator(".mp-row").first.wait_for()
            assert page.locator("#mp-ozon-account").count() == 1
            assert page.locator(".mp-ozon-confirm").count() == 1
            assert page.locator(".mp-open[href*='wb-preview']").count() >= 1
            REPORT["checks"].append("beta_internal_page_keeps_existing_channel_actions")

        # Listing screens are the terminal Ozon state; listing status/visibility
        # remain in the existing identifying workspace header and journey links
        # retain the exact account scope as the member changes.
        for name in ("listing_vue", "listing_classic"):
            wait_document(page, name, PAGE_PATHS[name])
            if SOURCE == "worktree":
                if name == "listing_vue":
                    page.locator("#marketplace-detail-app:not([v-cloak])").wait_for()
                    page.locator(".listing-workspace-head").wait_for()
                    end_state = page.locator(".preparation-listing-end-state")
                    end_state.wait_for()
                    hrefs = end_state.locator("a").evaluate_all("items => items.map(item => item.href)")
                    assert all(f"account_id={FIXTURE['account_id']}" in value for value in hrefs), hrefs
                    assert "сама по себе не подтверждает предыдущие этапы" in end_state.inner_text()
                else:
                    end_state = page.locator(".preparation-listing-end-state")
                    end_state.wait_for()
                    hrefs = end_state.locator("a").evaluate_all("items => items.map(item => item.getAttribute('href'))")
                    assert all(f"account_id={FIXTURE['account_id']}" in value for value in hrefs), hrefs
                REPORT["checks"].append(name + "_ozon_listing_terminal_context")
                for theme in ("light", "dark"):
                    for width in (1440, 390):
                        geometry(page, name, width, theme)
                        screenshot(page, name, width, theme)

        if SOURCE == "worktree":
            # Responsive sweep for every Jinja journey state; old page geometry
            # is recorded but assertions are scoped to the new components.
            for name, path, current in macro_pages:
                wait_document(page, name, path)
                scoped_account = None if name in {"supplier_catalog", "supplier_products", "supplier_source_detail"} else FIXTURE["account_id"]
                journey_assertions(page, name, current, scoped_account)
                for theme in ("light", "dark"):
                    for width in (320, 390, 768, 1024, 1440):
                        geometry(page, name, width, theme)
                if name in {"supplier_source_detail", "draft_detail_vue", "review"}:
                    for theme in ("light", "dark"):
                        for width in (390, 1024):
                            text_200(page, name, width, theme)
            assert all(row["actual_theme"] == row["theme"] for row in REPORT["geometry"]), REPORT["geometry"]

        assert REPORT["provider_attempts"] == 0, REPORT
        assert REPORT["unexpected_external_requests"] == [], REPORT["unexpected_external_requests"]
        assert REPORT["unexpected_http"] == [], REPORT["unexpected_http"]
        assert REPORT["writes"] == [], REPORT["writes"]
        REPORT["checks"].append("provider_external_and_unapproved_writes_zero")
        REPORT["status"] = "passed"
        browser.close()


def main():
    try:
        run()
    except Exception as exc:
        capture_failure(ACTIVE_PAGE, exc)
        raise
    finally:
        server.shutdown()
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "source": REPORT.get("source"),
            "status": REPORT.get("status"),
            "report": str(REPORT_PATH),
            "pages": len(REPORT["pages"]),
            "layouts": len(REPORT["layouts"]),
            "checks": len(REPORT["checks"]),
            "screenshots": len(REPORT["screenshots"]),
            "provider_attempts": REPORT["provider_attempts"],
            "writes": REPORT["writes"],
        }, ensure_ascii=False), flush=True)
        TEMP.cleanup()


if __name__ == "__main__":
    main()
