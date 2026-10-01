"""Synthetic Flask/Jinja/Vue browser checks for UX-01 listing navigation.

Run once per visual variant:
  UX01_LISTING_VARIANT=baseline python tests/ux01/listing_browser.py
  UX01_LISTING_VARIANT=after python tests/ux01/listing_browser.py

The baseline loader reads the frozen ba63371 templates and Vue scripts from
Git without changing the worktree. Both runs use a fresh temporary SQLite DB,
synthetic seller rows, and an HTTP bridge that denies unlisted external hosts.
"""

import base64
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, quote, urlsplit

import requests
from jinja2 import ChoiceLoader, DictLoader
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ASSETS = ROOT / "tests/ozon_release/assets"
VARIANT = os.environ.get("UX01_LISTING_VARIANT", "after").strip().lower()
if VARIANT not in {"baseline", "after"}:
    raise ValueError("UX01_LISTING_VARIANT must be baseline or after")
OUT = Path(os.environ.get("UX01_LISTING_ARTIFACTS", "/artifacts/ux01-listing"))
OUT.mkdir(parents=True, exist_ok=True)
TEMP = tempfile.TemporaryDirectory(prefix="ux01-listing-browser-")
TEMP_PATH = Path(TEMP.name)
REPORT = {
    "status": "running",
    "variant": VARIANT,
    "scope": "synthetic_flask_jinja_vue_listing_workspace",
    "checks": [],
    "screenshots": [],
    "js_errors": [],
    "unexpected_http": [],
    "external": [],
    "provider_attempts": 0,
    "writes": [],
    "local_api": [],
    "geometry": [],
    "keyboard_focus": [],
    "text_scale": [],
}
ACTIVE_PAGE = None

os.environ.update({
    "DATABASE_URL": "sqlite:///" + str(TEMP_PATH / "app.sqlite"),
    "SKIP_SCHEDULER": "1",
    "IMAGE_LAB_INLINE_WORKER": "0",
    "SECRET_KEY": "synthetic-ux01-listing-browser",
    "ENCRYPTION_KEY": base64.urlsafe_b64encode(b"u" * 32).decode(),
    "MARKETPLACE_OZON_ENABLED": "1",
    "MARKETPLACE_OZON_PUBLICATION_ENABLED": "0",
    "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED": "0",
    "MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED": "0",
    "OZON_RATE_LIMIT_DIR": str(TEMP_PATH / "limits"),
})


def no_network(*_args, **_kwargs):
    REPORT["provider_attempts"] += 1
    raise AssertionError("External Python network is forbidden in this fixture")


requests.sessions.Session.request = no_network
socket.create_connection = no_network

from seller_platform import app
from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    SellerMarketplaceAccount,
    db,
)
from tests.ozon_release.seed import PASSWORD, PHOTO, USERNAME, seed

PHOTO2 = "https://ozon-fixture.test/product-2.svg"


app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False)
FIXTURE = seed(app)


def baseline_file(path):
    completed = subprocess.run(
        ["git", "show", "ba63371:" + path],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


BASELINE_TEMPLATES = (
    "templates/base.html",
    "templates/marketplace_listings_beta.html",
    "templates/marketplace_listings.html",
    "templates/marketplace_listing_beta_detail.html",
    "templates/marketplace_listing_detail.html",
    "templates/partials/marketplace_catalog_products.html",
)
BASELINE_SCRIPTS = (
    "static/marketplace-catalog-beta.js",
    "static/marketplace-detail-beta.js",
)
TEMPLATE_OVERRIDE = {}
SCRIPT_OVERRIDE = {}
if VARIANT == "baseline":
    TEMPLATE_OVERRIDE = {
        path.removeprefix("templates/"): baseline_file(path).decode("utf-8")
        for path in BASELINE_TEMPLATES
    }
    SCRIPT_OVERRIDE = {
        "/static/" + path.removeprefix("static/"): baseline_file(path)
        for path in BASELINE_SCRIPTS
    }
    current_loader = app.jinja_env.loader
    app.jinja_env.loader = ChoiceLoader([
        DictLoader(TEMPLATE_OVERRIDE),
        current_loader,
    ])
    app.jinja_env.cache.clear()


with app.app_context():
    seller_account = db.session.get(
        SellerMarketplaceAccount,
        FIXTURE["account_id"],
    )
    seller_id = FIXTURE["seller_id"]
    ozon = Marketplace.query.filter_by(code="ozon").one()
    wb = Marketplace.query.filter_by(code="wb").first()
    if wb is None:
        wb = Marketplace(
            code="wb",
            name="Wildberries",
            adapter_code="wb",
            is_active=True,
        )
        db.session.add(wb)
        db.session.flush()

    source_ids = [FIXTURE["source_id"]]
    for index in (1, 2):
        source = ImportedProduct(
            seller_id=seller_id,
            external_id=f"ux01-return-source-{index}",
            external_vendor_code=f"UX01-{index}",
            source_type="synthetic",
            title=f"UX return source {index}",
            category="Synthetic",
            photo_urls=json.dumps([PHOTO]),
        )
        db.session.add(source)
        db.session.flush()
        source_ids.append(source.id)

    ozon_ids = FIXTURE["listing_ids"][:3]
    now = datetime.utcnow()
    for index, listing_id in enumerate(ozon_ids):
        listing = db.session.get(MarketplaceListing, listing_id)
        listing.title = (
            "UX Listing Target" if index == 1 else f"UX Listing Result {index + 1}"
        )
        listing.offer_id = f"UX-RETURN-{index + 1}"
        listing.external_product_id = f"ux-return-{index + 1}"
        listing.normalized_status = "error"
        listing.provider_status = "CONTENT_ERROR"
        listing.visibility = "VISIBLE"
        listing.imported_product_id = source_ids[index]
        listing.link_status = "linked"
        listing.link_source = "manual"
        listing.linked_at = now
        listing.link_version = max(1, listing.link_version)
        listing.is_available = True
        listing.list_synced_at = now
        listing.last_seen_at = now
        listing.info_synced_at = now
        listing.attributes_synced_at = now
        listing.prices_synced_at = now
        listing.stocks_synced_at = now

    target = db.session.get(MarketplaceListing, ozon_ids[1])
    inner_moderation_error = json.dumps({
        "code": "DESCRIPTION_DECLINE",
        "message": "Request failed",
        "error": json.dumps({
            "description": "Описание содержит слишком длинные слова. Разделите склеенные слова."
        }, ensure_ascii=True),
    }, ensure_ascii=True)
    target.moderation_errors_json = json.dumps([
        inner_moderation_error,
        json.dumps({
            "code": "FUTURE_REASON",
            "description": "<img src=x onerror=alert(1)>",
        }, ensure_ascii=False),
    ], ensure_ascii=False)
    target.media_json = json.dumps({
        "primary_image": PHOTO,
        "images": [PHOTO, PHOTO2],
    })
    target.provider_status = "declined"
    target.visibility = "inactive"
    wb_member = MarketplaceListing(
        seller_id=seller_id,
        marketplace_id=wb.id,
        account_id=None,
        imported_product_id=target.imported_product_id,
        offer_id="UX-WB-MEMBER",
        external_product_id="91000001",
        primary_sku="81000001",
        title="UX WB Member",
        description="Synthetic member used to exercise browser history.",
        normalized_status="active",
        provider_status="published",
        visibility="VISIBLE",
        is_available=True,
        is_archived=False,
        link_status="linked",
        link_source="manual",
        link_version=1,
        linked_at=now,
        sync_fingerprint=hashlib.sha256(b"ux01-wb-member").hexdigest(),
        media_json=json.dumps({"primary_image": PHOTO, "images": [PHOTO]}),
        price_summary_json=json.dumps({"currency": "RUB", "available": False}),
        stock_summary_json=json.dumps({"present": None}),
        list_synced_at=now,
        info_synced_at=now,
        attributes_synced_at=now,
        prices_synced_at=now,
        stocks_synced_at=now,
        last_seen_at=now,
    )
    db.session.add(wb_member)
    db.session.commit()
    FIXTURE["target_listing_id"] = target.id
    FIXTURE["wb_member_id"] = wb_member.id
    FIXTURE["account_id"] = seller_account.id


logging.getLogger("werkzeug").setLevel(logging.ERROR)
server = make_server("127.0.0.1", 0, app, threaded=True)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
BASE = f"http://127.0.0.1:{server.server_port}"

manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
for asset in manifest.values():
    content = (ASSETS / asset["file"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == asset["sha256"]

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="300"><rect width="240" height="300" fill="#e8e5df"/><rect x="60" y="50" width="120" height="200" rx="10" fill="#566b79"/><circle cx="120" cy="135" r="32" fill="#f6f4ef"/></svg>'
CATALOG_CONTEXT = (
    "/marketplaces/listings/beta?marketplace=ozon"
    f"&account_id={FIXTURE['account_id']}"
    "&status=error&link_status=linked&include_unavailable=1"
    "&search=UX+Listing&page=2&per_page=1"
)
CATALOG_PAGE_ONE = CATALOG_CONTEXT.replace("page=2", "page=1")


def bridge(route):
    request = route.request
    parts = urlsplit(request.url)
    if parts.hostname == "127.0.0.1" and parts.port == server.server_port:
        if request.method not in ("GET", "HEAD"):
            assert parts.path == "/login", (request.method, parts.path)
            REPORT["writes"].append({"method": request.method, "path": parts.path})
        if VARIANT == "baseline" and parts.path in SCRIPT_OVERRIDE:
            route.fulfill(
                body=SCRIPT_OVERRIDE[parts.path],
                content_type="application/javascript; charset=utf-8",
            )
            return
        result = route.fetch(max_redirects=0)
        if result.status >= 500:
            REPORT["unexpected_http"].append({"path": parts.path, "status": result.status})
        route.fulfill(response=result)
        return
    if request.url in {PHOTO, PHOTO2}:
        route.fulfill(body=SVG, content_type="image/svg+xml")
        return
    asset = manifest.get(request.url) or manifest.get(request.url.rstrip("/"))
    if asset:
        route.fulfill(
            body=(ASSETS / asset["file"]).read_bytes(),
            content_type=asset["content_type"],
        )
        return
    REPORT["external"].append({"host": parts.hostname, "path": parts.path})
    route.abort()


def record_local_api_response(response):
    parts = urlsplit(response.url)
    if parts.hostname != "127.0.0.1" or parts.port != server.server_port:
        return
    if parts.path not in {
        "/marketplaces/listings/api/groups",
        "/marketplaces/listings/api",
        "/marketplaces/listings/api/facets",
    }:
        return
    entry = {"path": parts.path, "status": response.status}
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        entry["success"] = payload.get("success")
        items = payload.get("items")
        if isinstance(items, list):
            entry["items_count"] = len(items)
        pagination = payload.get("pagination")
        if isinstance(pagination, dict):
            entry["pagination"] = {
                key: pagination.get(key)
                for key in ("page", "per_page", "pages", "total", "has_next")
                if key in pagination
            }
        for key in ("code", "error"):
            value = payload.get(key)
            if isinstance(value, str):
                entry[key] = value[:180]
    REPORT["local_api"].append(entry)


def passed(name):
    assert not REPORT["js_errors"], REPORT["js_errors"]
    assert not REPORT["unexpected_http"], REPORT["unexpected_http"]
    REPORT["checks"].append(name)
    print(json.dumps({"ux01_listing_check": VARIANT + ":" + name}), flush=True)


def wait_catalog(page):
    page.locator("article.mcat-card:not(.mcat-skel)").first.wait_for()
    page.wait_for_function(
        "Array.from(document.querySelectorAll('[v-cloak]')).every(node => !node.offsetParent)"
    )
    page.wait_for_load_state("networkidle")


def failure_page_state(page):
    try:
        return page.evaluate("""() => {
            const root = document.documentElement;
            const fallback = document.getElementById('mcat-bootstrap-fallback');
            const bootstrapNode = document.getElementById('mcat-bootstrap');
            let bootstrap = null;
            try { bootstrap = bootstrapNode ? JSON.parse(bootstrapNode.textContent) : null; }
            catch (_) { bootstrap = 'invalid-json'; }
            const overflow = Array.from(document.querySelectorAll('body *')).map(el => {
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0 || rect.right <= innerWidth + 1) return null;
                const style = getComputedStyle(el);
                return {
                    tag: el.tagName,
                    className: typeof el.className === 'string' ? el.className.slice(0, 120) : '',
                    right: Math.round(rect.right),
                    width: Math.round(rect.width),
                    position: style.position,
                    overflowX: style.overflowX
                };
            }).filter(Boolean).slice(0, 12);
            return {
                url: location.pathname + location.search,
                title: document.title,
                viewport: {width: innerWidth, height: innerHeight},
                root: {clientWidth: root.clientWidth, scrollWidth: root.scrollWidth},
                bootstrap,
                fallback: fallback ? {
                    display: getComputedStyle(fallback).display,
                    text: fallback.innerText.slice(0, 240)
                } : null,
                app: {
                    exists: !!document.getElementById('marketplace-catalog-app'),
                    visible: !!document.querySelector('#marketplace-catalog-app:not([v-cloak])'),
                    text: (document.getElementById('marketplace-catalog-app') || {}).innerText?.slice(0, 500) || ''
                },
                cards: document.querySelectorAll('article.mcat-card:not(.mcat-skel)').length,
                skeletons: document.querySelectorAll('article.mcat-skel').length,
                apiError: document.querySelector('.mcat-error')?.innerText?.slice(0, 240) || null,
                overflow
            };
        }""")
    except Exception as exc:
        return {"diagnostic_error": f"{type(exc).__name__}: {exc}"}


def capture_failure(page, exc):
    REPORT["status"] = "failed"
    REPORT["failure"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
    if page is not None:
        REPORT["failure_page"] = failure_page_state(page)
        try:
            path = OUT / f"listing-{VARIANT}-failure.png"
            page.screenshot(path=str(path), full_page=True, animations="disabled")
            REPORT["failure_screenshot"] = str(path)
        except Exception as screenshot_error:
            REPORT["failure_screenshot_error"] = (
                f"{type(screenshot_error).__name__}: {screenshot_error}"
            )
    print(json.dumps({
        "ux01_listing_failure": REPORT.get("failure"),
        "page": REPORT.get("failure_page"),
        "local_api": REPORT["local_api"][-10:],
    }, ensure_ascii=False), flush=True)


def go(page, path, selector):
    response = page.goto(BASE + path)
    assert response and response.status == 200, (
        path,
        response.status if response else None,
    )
    page.locator(selector).wait_for()
    page.wait_for_function(
        "Array.from(document.querySelectorAll('[v-cloak]')).every(node => !node.offsetParent)"
    )
    page.wait_for_load_state("networkidle")


def screenshot(page, name):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.evaluate('document.documentElement.dataset.theme="light"')
    page.screenshot(
        path=str(OUT / f"listing-{VARIANT}-{name}.png"),
        full_page=True,
        animations="disabled",
    )
    REPORT["screenshots"].append(name)


def url_return_value(href):
    return parse_qs(urlsplit(href).query).get("return_to", [None])[0]


def assert_page_width(page, label, width, theme):
    measured = page.evaluate("""() => {
        const root = document.documentElement;
        const body = document.body;
        const header = document.querySelector('.listing-workspace-head');
        const headerRect = header ? header.getBoundingClientRect() : null;
        const overflow = Array.from(document.querySelectorAll('body *')).map(el => {
            const rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0 || rect.right <= innerWidth + 1) return null;
            const style = getComputedStyle(el);
            return {
                tag: el.tagName,
                className: typeof el.className === 'string' ? el.className.slice(0, 120) : '',
                right: Math.round(rect.right),
                width: Math.round(rect.width),
                position: style.position,
                overflowX: style.overflowX
            };
        }).filter(Boolean).slice(0, 12);
        return {
            viewport: innerWidth,
            rootClientWidth: root.clientWidth,
            rootScrollWidth: root.scrollWidth,
            bodyClientWidth: body.clientWidth,
            bodyScrollWidth: body.scrollWidth,
            header: headerRect ? {
                left: Math.round(headerRect.left),
                right: Math.round(headerRect.right),
                width: Math.round(headerRect.width)
            } : null,
            overflow
        };
    }""")
    measured.update({"route": label, "requested_width": width, "theme": theme})
    actual_theme = page.evaluate("document.documentElement.dataset.theme")
    measured["actual_theme"] = actual_theme
    REPORT["geometry"].append(measured)
    assert actual_theme == theme, measured
    assert measured["viewport"] == width, measured
    assert measured["rootScrollWidth"] <= measured["rootClientWidth"] + 1, measured
    assert measured["bodyScrollWidth"] <= measured["bodyClientWidth"] + 1, measured
    if measured["header"]:
        assert measured["header"]["left"] >= -1, measured
        assert measured["header"]["right"] <= measured["rootClientWidth"] + 1, measured
    return measured


def tab_to_and_check_focus(page, target, label):
    assert target.count() == 1, (label, target.count())
    prepared = target.evaluate("""target => {
        const selector = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
        const tabbable = Array.from(document.querySelectorAll(selector)).filter(el => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
                style.visibility !== 'hidden' && !el.closest('[inert]') && el.tabIndex >= 0;
        });
        const index = tabbable.indexOf(target);
        if (index < 0) return {index, prepared: false};
        if (index === 0) {
            if (document.activeElement && document.activeElement !== document.body) {
                document.activeElement.blur();
            }
        } else {
            tabbable[index - 1].focus();
        }
        return {index, prepared: true};
    }""")
    assert prepared["prepared"], (label, prepared)
    page.keyboard.press("Tab")
    state = target.evaluate("""target => {
        const style = getComputedStyle(target);
        const parseColor = value => {
            const match = value.match(/^rgba?\(([^)]+)\)$/);
            if (!match) return null;
            const parts = match[1].split(/[,\s/]+/).filter(Boolean).map(Number);
            if (parts.length < 3 || parts.slice(0, 3).some(part => !Number.isFinite(part))) return null;
            return {rgb: parts.slice(0, 3), alpha: Number.isFinite(parts[3]) ? parts[3] : 1};
        };
        const composite = (foreground, background) => foreground.rgb.map((channel, index) =>
            channel * foreground.alpha + background[index] * (1 - foreground.alpha)
        );
        let focusBackground = [255, 255, 255];
        const ancestors = [];
        for (let node = target.parentElement; node; node = node.parentElement) ancestors.unshift(node);
        ancestors.forEach(node => {
            const background = parseColor(getComputedStyle(node).backgroundColor);
            if (background && background.alpha > 0) focusBackground = composite(background, focusBackground);
        });
        const outline = parseColor(style.outlineColor);
        const visibleOutline = outline ? composite(outline, focusBackground) : null;
        const luminance = rgb => {
            const linear = rgb.map(value => {
                const channel = value / 255;
                return channel <= 0.04045 ? channel / 12.92 : Math.pow((channel + 0.055) / 1.055, 2.4);
            });
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
        };
        const contrast = visibleOutline ? (() => {
            const a = luminance(visibleOutline);
            const b = luminance(focusBackground);
            return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
        })() : null;
        return {
            active: document.activeElement === target,
            focusVisible: target.matches(':focus-visible'),
            outlineStyle: style.outlineStyle,
            outlineWidth: style.outlineWidth,
            outlineColor: style.outlineColor,
            focusBackground: focusBackground.map(Math.round),
            focusContrast: contrast === null ? null : Number(contrast.toFixed(2))
        };
    }""")
    state.update({"control": label})
    REPORT["keyboard_focus"].append(state)
    assert state["active"], (label, prepared, state)
    assert state["focusVisible"], (label, state)
    assert state["outlineStyle"] not in ("none", "hidden"), (label, state)
    assert float(state["outlineWidth"].removesuffix("px")) >= 1, (label, state)
    if "/member/" in label:
        assert state["focusContrast"] is not None and state["focusContrast"] >= 3, (label, state)


def apply_text_scale_200(page, label, width, theme):
    result = page.evaluate("""async () => {
        const title = document.querySelector('.listing-workspace-title');
        const before = title ? parseFloat(getComputedStyle(title).fontSize) : null;
        let scaled = 0;
        Array.from(document.body.querySelectorAll('*')).forEach(el => {
            const directText = Array.from(el.childNodes).some(node =>
                node.nodeType === Node.TEXT_NODE && node.textContent.trim()
            );
            if (!directText) return;
            const rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return;
            const size = parseFloat(getComputedStyle(el).fontSize);
            if (!Number.isFinite(size) || size <= 0) return;
            el.style.setProperty('font-size', (size * 2) + 'px', 'important');
            scaled += 1;
        });
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const after = title ? parseFloat(getComputedStyle(title).fontSize) : null;
        return {elements_scaled: scaled, title_before_px: before, title_after_px: after};
    }""")
    result.update({"route": label, "width": width, "theme": theme, "method": "text-node computed font sizes doubled; viewport unchanged"})
    REPORT["text_scale"].append(result)
    assert result["elements_scaled"] > 0, result
    assert result["title_before_px"] and result["title_after_px"] >= result["title_before_px"] * 1.9, result
    assert_page_width(page, label + "_text_200", width, theme)


def matrix_screenshot(page, name):
    page.screenshot(
        path=str(OUT / f"listing-{VARIANT}-{name}.png"),
        full_page=True,
        animations="disabled",
    )
    REPORT["screenshots"].append(name)


def run_layout_accessibility_matrix(page):
    widths = (320, 390, 768, 1024, 1440)
    themes = ("light", "dark")
    overview_path = (
        f"/marketplaces/listings/beta/{FIXTURE['target_listing_id']}?return_to="
        + quote(CATALOG_CONTEXT, safe="")
    )
    classic_query = CATALOG_CONTEXT.replace("/beta?", "/classic?")
    management_path = (
        f"/marketplaces/listings/{FIXTURE['target_listing_id']}?return_to="
        + quote(classic_query, safe="")
    )
    for width in widths:
        for theme in themes:
            page.set_viewport_size({"width": width, "height": 900})
            page.evaluate("theme => { localStorage.setItem('sh-theme', theme); document.documentElement.dataset.theme = theme; }", theme)
            go(page, overview_path, ".listing-workspace-head")
            header = page.locator(".listing-workspace-head")
            header.get_by_role("heading", name="UX Listing Target", exact=True).wait_for()
            assert header.locator(".listing-workspace-back").get_attribute("href") == CATALOG_CONTEXT
            assert header.get_by_role("link", name="Обзор", exact=True).count() == 1
            assert url_return_value(header.get_by_role("link", name="Управление", exact=True).get_attribute("href")) == CATALOG_CONTEXT
            assert header.locator(".listing-workspace-members button").count() >= 2
            assert_page_width(page, "overview", width, theme)
            tab_to_and_check_focus(page, header.locator(".listing-workspace-back"), f"overview/back/{width}/{theme}")
            tab_to_and_check_focus(page, header.get_by_role("link", name="Обзор", exact=True), f"overview/mode-overview/{width}/{theme}")
            tab_to_and_check_focus(page, header.get_by_role("link", name="Управление", exact=True), f"overview/mode-management/{width}/{theme}")
            tab_to_and_check_focus(page, header.locator(".listing-workspace-members button").first, f"overview/member/{width}/{theme}")
            if width == 390 and theme == "light":
                matrix_screenshot(page, "matrix-390-light-overview")
            if width == 1440 and theme == "dark":
                matrix_screenshot(page, "matrix-1440-dark-overview")
            if width in (390, 1024):
                apply_text_scale_200(page, "overview", width, theme)

            page.set_viewport_size({"width": width, "height": 900})
            page.evaluate("theme => { localStorage.setItem('sh-theme', theme); document.documentElement.dataset.theme = theme; }", theme)
            go(page, management_path, ".listing-workspace-head")
            header = page.locator(".listing-workspace-head")
            header.get_by_role("heading", name="UX Listing Target", exact=True).wait_for()
            assert header.locator(".listing-workspace-back").get_attribute("href") == classic_query
            assert url_return_value(header.get_by_role("link", name="Обзор", exact=True).get_attribute("href")) == classic_query
            assert url_return_value(header.get_by_role("link", name="Управление", exact=True).get_attribute("href")) == classic_query
            assert_page_width(page, "management", width, theme)
            tab_to_and_check_focus(page, header.locator(".listing-workspace-back"), f"management/back/{width}/{theme}")
            tab_to_and_check_focus(page, header.get_by_role("link", name="Обзор", exact=True), f"management/mode-overview/{width}/{theme}")
            tab_to_and_check_focus(page, header.get_by_role("link", name="Управление", exact=True), f"management/mode-management/{width}/{theme}")
            if width == 390 and theme == "light":
                matrix_screenshot(page, "matrix-390-light-management")
            if width == 1024 and theme == "dark":
                matrix_screenshot(page, "matrix-1024-dark-management")
            if width in (390, 1024):
                apply_text_scale_200(page, "management", width, theme)
    REPORT["checks"].append("detail_layout_theme_keyboard_and_text_200_matrix")
    print(json.dumps({
        "ux01_listing_check": VARIANT + ":detail_layout_theme_keyboard_and_text_200_matrix",
        "geometry_cases": len(REPORT["geometry"]),
        "keyboard_focus_cases": len(REPORT["keyboard_focus"]),
        "text_scale_cases": len(REPORT["text_scale"]),
    }), flush=True)


def run_browser_scenario(page, context, browser):
    first = BASE + "/login?next=" + quote(CATALOG_CONTEXT, safe="")
    response = page.goto(first)
    assert response and response.status == 200
    page.locator("input[name=username]").wait_for()
    page.locator("input[name=username]").fill(USERNAME)
    page.locator("input[name=password]").fill(PASSWORD)
    page.get_by_role("button", name="Войти", exact=True).click()
    wait_catalog(page)
    assert urlsplit(page.url).path == "/marketplaces/listings/beta"
    if VARIANT == "baseline":
        screenshot(page, "catalog-before")
        go(
            page,
            f"/marketplaces/listings/view/{FIXTURE['target_listing_id']}",
            "#marketplace-detail-app",
        )
        screenshot(page, "overview-before")
        classic_query = CATALOG_CONTEXT.replace("/beta?", "/classic?")
        go(page, classic_query, ".catalog-products .catalog-product")
        go(
            page,
            f"/marketplaces/listings/{FIXTURE['target_listing_id']}",
            "main",
        )
        screenshot(page, "management-before")
        assert not REPORT["external"], REPORT["external"]
        assert REPORT["provider_attempts"] == 0, REPORT
        REPORT["checks"].extend([
            "baseline_catalog_rendered",
            "baseline_overview_rendered",
            "baseline_management_rendered",
        ])
        REPORT["status"] = "passed"
        context.close()
        browser.close()
        return
    assert page.locator("article.mcat-card:not(.mcat-skel)").count() == 1
    assert "UX Listing Target" in page.locator("article.mcat-card").first.inner_text()
    detail_link = page.locator("article.mcat-card a.mcat-open").first
    assert url_return_value(detail_link.get_attribute("href")) == CATALOG_CONTEXT
    screenshot(page, "catalog-initial-page-two")
    passed("initial_page_two_keeps_all_catalog_filters_and_explicit_return")

    canonical_context = CATALOG_CONTEXT.replace("/beta?", "/?")
    page.goto(BASE + canonical_context)
    wait_catalog(page)
    canonical_link = page.locator("article.mcat-card a.mcat-open").first
    assert url_return_value(canonical_link.get_attribute("href")) == canonical_context
    passed("canonical_catalog_route_is_preserved_as_exact_return_source")

    target_detail_path = f"/marketplaces/listings/beta/{FIXTURE['target_listing_id']}"
    go(page, target_detail_path + "?return_to=" + quote(CATALOG_CONTEXT, safe=""), ".listing-workspace-head")
    page.locator(".listing-workspace-title").filter(has_text="UX Listing Target").wait_for()
    hero_image = page.locator(".mdet-hero img")
    assert hero_image.get_attribute("alt") == "Фото товара: UX Listing Target"
    assert hero_image.get_attribute("width") == "320"
    assert hero_image.get_attribute("height") == "427"
    gallery_buttons = page.locator(".mcat-gallery .mcat-gal-thumb")
    assert gallery_buttons.count() == 2
    assert [gallery_buttons.nth(i).get_attribute("aria-pressed") for i in range(2)] == ["true", "false"]
    moderation_section = page.locator(".mdet-sect--danger")
    moderation_text = moderation_section.inner_text()
    assert "Слишком длинные слова в описании." in moderation_text
    assert "Разделите склеенные слова" in moderation_text
    assert "Откройте черновики Ozon" in moderation_text
    assert moderation_section.locator("details").count() == 2
    assert all(not moderation_section.locator("details").nth(i).evaluate("el => el.open") for i in range(2))
    assert page.locator('.mdet-sect--danger img[src="x"]').count() == 0
    draft_link = moderation_section.get_by_role("link", name="Открыть черновики Ozon")
    draft_parts = urlsplit(draft_link.get_attribute("href"))
    assert draft_parts.path == "/marketplaces/drafts/"
    assert parse_qs(draft_parts.query)["account_id"] == [str(FIXTURE["account_id"])]
    warning_text = page.locator(".listing-workspace-warning").inner_text()
    assert "Ошибка" in warning_text and "Отклонён площадкой" in warning_text
    assert "declined" not in warning_text
    passed("moderation_reason_next_step_and_gallery_controls_have_accessible_semantics")
    link_label = page.locator("#marketplace-detail-app .mcat-linkline > .mcat-link")
    assert link_label.count() == 1, "template DOM must match the scoped link-label selector"
    link_label_style = link_label.evaluate("""el => {
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return {
            selectorMatches: el.matches('#marketplace-detail-app .mcat-linkline > .mcat-link'),
            whiteSpace: style.whiteSpace,
            overflowWrap: style.overflowWrap,
            left: Math.round(rect.left),
            right: Math.round(rect.right),
            width: Math.round(rect.width),
        };
    }""")
    assert link_label_style["selectorMatches"], link_label_style
    assert link_label_style["whiteSpace"] == "normal", link_label_style
    assert link_label_style["overflowWrap"] == "anywhere", link_label_style
    REPORT["checks"].append("listing_link_label_scoped_wrap_matches_rendered_dom")
    REPORT["link_label_wrap"] = link_label_style
    header = page.locator(".listing-workspace-head")
    assert header.get_by_role("link", name="Вернуться к найденным карточкам").get_attribute("href") == CATALOG_CONTEXT
    assert header.locator(".listing-workspace-channel").inner_text().strip() == "Ozon"
    assert header.locator(".listing-workspace-account").count() == 1
    assert header.locator(".listing-workspace-account").inner_text().strip() == "Ozon CI 0"
    for label, expected_path in (
        ("Обзор", f"/marketplaces/listings/view/{FIXTURE['target_listing_id']}"),
        ("Управление", f"/marketplaces/listings/{FIXTURE['target_listing_id']}"),
    ):
        mode_link = header.get_by_role("link", name=label, exact=True)
        parts = urlsplit(mode_link.get_attribute("href"))
        assert parts.path == expected_path, (label, parts.path)
        assert url_return_value(mode_link.get_attribute("href")) == CATALOG_CONTEXT
    screenshot(page, "overview-member-ozon")
    passed("overview_header_modes_and_catalog_return_preserved")

    wb_tab = header.get_by_role("button", name="Wildberries", exact=False)
    assert wb_tab.count() == 1
    wb_tab.click()
    header.get_by_role("heading", name="UX WB Member", exact=True).wait_for()
    assert header.locator(".listing-workspace-channel").inner_text().strip() == "Wildberries"
    assert header.locator(".listing-workspace-account").count() == 0
    assert urlsplit(page.url).path == f"/marketplaces/listings/view/{FIXTURE['wb_member_id']}"
    assert url_return_value(page.url) == CATALOG_CONTEXT
    screenshot(page, "overview-member-wb")
    page.go_back(wait_until="domcontentloaded")
    header.get_by_role("heading", name="UX Listing Target", exact=True).wait_for()
    assert header.locator(".listing-workspace-channel").inner_text().strip() == "Ozon"
    assert header.locator(".listing-workspace-account").count() == 1
    assert header.locator(".listing-workspace-account").inner_text().strip() == "Ozon CI 0"
    assert urlsplit(page.url).path == target_detail_path
    assert url_return_value(page.url) == CATALOG_CONTEXT
    page.go_forward(wait_until="domcontentloaded")
    header.get_by_role("heading", name="UX WB Member", exact=True).wait_for()
    assert header.locator(".listing-workspace-channel").inner_text().strip() == "Wildberries"
    assert header.locator(".listing-workspace-account").count() == 0
    assert urlsplit(page.url).path == f"/marketplaces/listings/view/{FIXTURE['wb_member_id']}"
    assert header.locator(".listing-workspace-channel").inner_text().strip() == "Wildberries"
    assert header.locator(".listing-workspace-account").count() == 0
    assert url_return_value(page.url) == CATALOG_CONTEXT
    passed("member_switch_back_forward_updates_shared_header_and_keeps_context")

    page.goto(BASE + CATALOG_PAGE_ONE)
    wait_catalog(page)
    assert page.locator("article.mcat-card:not(.mcat-skel)").count() == 1
    page.get_by_role("button", name="Показать ещё", exact=True).click()
    page.wait_for_function("document.querySelectorAll('article.mcat-card:not(.mcat-skel)').length === 2")
    target_card = page.locator("article.mcat-card").filter(has_text="UX Listing Target")
    target_link = target_card.locator("a.mcat-open")
    assert url_return_value(target_link.get_attribute("href")) == CATALOG_CONTEXT
    passed("load_more_group_index_maps_to_its_actual_return_page")

    classic_query = CATALOG_CONTEXT.replace("/beta?", "/classic?")
    page.goto(BASE + classic_query)
    page.locator(".catalog-products .catalog-product").first.wait_for()
    assert page.locator(".catalog-products .catalog-product").count() == 1
    classic_row = page.locator(".catalog-products .catalog-product").first
    # The beta page number refers to grouped ImportedProduct rows, while the
    # classic fallback paginates individual listings by updated_at/id. The
    # target therefore is beta group page 2 but flat page 1; its later seeded
    # update makes Result 3 the second classic row. Keep the same page and all
    # filters and follow the row actually returned by that flat page.
    assert "UX Listing Result 3" in classic_row.inner_text()
    classic_detail = classic_row.get_attribute("href")
    assert url_return_value(classic_detail) == classic_query
    assert urlsplit(classic_detail).path == (
        f"/marketplaces/listings/{FIXTURE['listing_ids'][2]}"
    )
    go(page, classic_detail, ".listing-workspace-head")
    classic_header = page.locator(".listing-workspace-head")
    assert classic_header.get_by_role("heading", name="UX Listing Result 3", exact=True).count() == 1
    assert classic_header.locator(".listing-workspace-channel").inner_text().strip() == "Ozon"
    assert classic_header.locator(".listing-workspace-account").count() == 1
    assert classic_header.locator(".listing-workspace-account").inner_text().strip() == "Ozon CI 0"
    assert classic_header.get_by_role("link", name="Вернуться к найденным карточкам").get_attribute("href") == classic_query
    assert url_return_value(classic_header.get_by_role("link", name="Обзор").get_attribute("href")) == classic_query
    assert url_return_value(classic_header.get_by_role("link", name="Управление").get_attribute("href")) == classic_query
    screenshot(page, "classic-management")
    passed("classic_row_explicit_return_and_management_workspace_modes")

    invalid = (
        f"/marketplaces/listings/view/{FIXTURE['target_listing_id']}"
        "?return_to=https%3A%2F%2Fevil.test%2Fprivate"
    )
    go(page, invalid, ".listing-workspace-head")
    fallback = f"/marketplaces/listings/?marketplace=ozon&account_id={FIXTURE['account_id']}"
    back_href = page.locator(".listing-workspace-back").get_attribute("href")
    assert back_href == fallback, back_href
    assert "evil.test" not in page.content()
    passed("direct_detail_uses_safe_marketplace_account_fallback")

    run_layout_accessibility_matrix(page)

    assert not REPORT["external"], REPORT["external"]
    assert REPORT["provider_attempts"] == 0, REPORT
    REPORT["status"] = "passed"
    context.close()
    browser.close()


def run_browser():
    global ACTIVE_PAGE
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("UX01_CHROMIUM", "/usr/bin/chromium"),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            service_workers="block",
        )
        context.route("**/*", bridge)
        page = context.new_page()
        ACTIVE_PAGE = page
        page.set_default_timeout(20000)
        page.on("pageerror", lambda error: REPORT["js_errors"].append(str(error)))
        page.on("response", record_local_api_response)

        try:
            run_browser_scenario(page, context, browser)
        except Exception as exc:
            capture_failure(page, exc)
            raise


try:
    run_browser()
except Exception as exc:
    if REPORT.get("status") != "failed":
        capture_failure(ACTIVE_PAGE, exc)
    raise
finally:
    server.shutdown()
    server_thread.join(timeout=5)
    TEMP.cleanup()
    (OUT / f"listing-browser-{VARIANT}.json").write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
