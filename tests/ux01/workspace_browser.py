"""Synthetic offline browser fixture for the UX-01 Seller Hub shell.

Run sequentially for a baseline and the working tree:
  UX01_WORKSPACE_SOURCE=ba63371 python tests/ux01/workspace_browser.py
  UX01_WORKSPACE_SOURCE=worktree python tests/ux01/workspace_browser.py

The app binds only to loopback, uses a disposable SQLite database and seeded
synthetic seller/account rows. Provider networking is forbidden in Python and
the browser bridge serves only pinned assets plus this local app. This fixture
does not publish, synchronize or contact a marketplace.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

import requests
from jinja2 import ChoiceLoader, DictLoader
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ASSETS = ROOT / "tests/ozon_release/assets"
VARIANT = os.environ.get("UX01_WORKSPACE_SOURCE", "worktree").strip()
if VARIANT not in {"ba63371", "worktree"}:
    raise ValueError("UX01_WORKSPACE_SOURCE must be ba63371 or worktree")
WIDTHS = (320, 390, 768, 1024)
OWNED_TEMPLATES = (
    "templates/base.html",
    "templates/api_settings.html",
    "templates/marketplace_accounts.html",
    "templates/marketplace_readiness.html",
    "templates/card_quality.html",
    "templates/card_quality_beta.html",
    "templates/image_lab.html",
    "templates/product_defaults.html",
    "templates/auto_publish.html",
    "templates/api_logs.html",
)

TEMP = tempfile.TemporaryDirectory(prefix="ux01-workspace-browser-")
TEMP_PATH = Path(TEMP.name)
ARTIFACTS = Path(os.environ.get(
    "UX01_WORKSPACE_ARTIFACTS", "/artifacts/ux01-workspace"
))
ARTIFACTS.mkdir(parents=True, exist_ok=True)
REPORT_PATH = Path(os.environ.get(
    "UX01_WORKSPACE_REPORT", str(ARTIFACTS / f"{VARIANT}-report.json")
))
REPORT = {
    "status": "running",
    "source": VARIANT,
    "scope": "synthetic_full_flask_jinja_alpine_vue_workspace",
    "database": "disposable_sqlite",
    "network_policy": "loopback_only_pinned_browser_assets_no_provider_calls",
    "widths": list(WIDTHS),
    "themes": ["light", "dark"],
    "layouts": [],
    "pages": [],
    "interactions": [],
    "browser_api_reads": [],
    "browser_mutations": [],
    "http_errors": [],
    "unexpected_external_requests": [],
    "provider_attempts": 0,
    "javascript_errors": [],
    "console_errors": [],
    "source_hashes": {},
    "pinned_asset_hashes": {},
}

os.environ.update({
    "DATABASE_URL": "sqlite:///" + str(TEMP_PATH / "seller-hub.sqlite"),
    "SKIP_SCHEDULER": "1",
    "IMAGE_LAB_INLINE_WORKER": "0",
    "SECRET_KEY": "synthetic-ux01-workspace-only",
    "ENCRYPTION_KEY": base64.urlsafe_b64encode(b"u" * 32).decode(),
    "MARKETPLACE_OZON_ENABLED": "1",
    "MARKETPLACE_OZON_PUBLICATION_ENABLED": "0",
    "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED": "0",
    "MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED": "0",
    "OZON_RATE_LIMIT_DIR": str(TEMP_PATH / "limits"),
})


def no_provider_network(*_args, **_kwargs):
    REPORT["provider_attempts"] += 1
    raise AssertionError("Provider network is forbidden in this fixture")


requests.sessions.Session.request = no_provider_network
socket.create_connection = no_provider_network


def source_bytes(path: str) -> bytes:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe source path: {path}")
    if VARIANT == "worktree":
        return (ROOT / str(relative)).read_bytes()
    return subprocess.check_output(
        ["git", "show", f"{VARIANT}:{relative.as_posix()}"], cwd=ROOT,
        stderr=subprocess.DEVNULL,
    )


def load_assets() -> dict[str, dict]:
    manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    assets = {}
    for url, row in manifest.items():
        payload = (ASSETS / row["file"]).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != row["sha256"]:
            raise AssertionError(f"Pinned browser asset changed: {url}")
        assets[url] = {**row, "payload": payload}
        REPORT["pinned_asset_hashes"][url] = digest
    # card_quality_beta.html still references the existing checked-in Vue
    # global by its exact CDN URL. Fulfill that URL from the vendored 3.4.38
    # file so the real Vue page runs without an external request.
    vue_url = "https://cdn.jsdelivr.net/npm/vue@3.4.38/dist/vue.global.prod.js"
    vue_payload = (ROOT / "static/vendor/vue-3.4.38.global.prod.js").read_bytes()
    vue_digest = hashlib.sha256(vue_payload).hexdigest()
    assets[vue_url] = {
        "payload": vue_payload,
        "content_type": "application/javascript",
    }
    REPORT["pinned_asset_hashes"][vue_url] = vue_digest
    return assets


def source_template_overrides() -> dict[str, str]:
    result = {}
    for path in OWNED_TEMPLATES:
        payload = source_bytes(path)
        relative = path.removeprefix("templates/")
        result[relative] = payload.decode("utf-8")
        REPORT["source_hashes"][path] = hashlib.sha256(payload).hexdigest()
    css_path = "static/seller-workspace.css"
    try:
        REPORT["source_hashes"][css_path] = hashlib.sha256(
            source_bytes(css_path)
        ).hexdigest()
    except (FileNotFoundError, subprocess.CalledProcessError):
        REPORT["source_hashes"][css_path] = None
    return result


def login_cookie(app, username: str, password: str, base_url: str) -> dict:
    client = app.test_client()
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    if response.status_code not in (302, 303):
        raise AssertionError(("synthetic login failed", username, response.status_code))
    cookie = client.get_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
    if cookie is None:
        raise AssertionError("synthetic login did not create an authenticated session")
    return {"name": cookie.key, "value": cookie.value, "url": base_url}


def theme_path(path: str, theme: str) -> str:
    """Set the requested theme before document scripts run on navigation."""
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}__ux_theme={theme}"


def set_theme_and_wait(page, theme: str) -> dict:
    """Persist and assert the rendered theme after two layout frames."""
    page.evaluate("""theme => {
        localStorage.setItem('sh-theme', theme);
        document.documentElement.dataset.theme = theme;
    }""", theme)
    page.evaluate("""() => new Promise(resolve => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
    })""")
    result = page.evaluate("""() => ({
        actual_theme: document.documentElement.dataset.theme,
        stored_theme: localStorage.getItem('sh-theme'),
    })""")
    assert result == {"actual_theme": theme, "stored_theme": theme}, (theme, result)
    return result


def main() -> None:
    from seller_platform import app
    from models import (
        ImportedProduct, Marketplace, Seller, SellerMarketplaceAccount, User, db,
    )
    from tests.ozon_release.seed import PASSWORD, USERNAME, seed

    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SESSION_COOKIE_SECURE=False,
        MARKETPLACE_OZON_ENABLED=True,
        MARKETPLACE_OZON_PUBLICATION_ENABLED=False,
        MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
        MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=False,
    )

    overrides = source_template_overrides()
    app.jinja_env.loader = ChoiceLoader([
        DictLoader(overrides),
        app.jinja_env.loader,
    ])
    app.jinja_env.cache.clear()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    fixture = seed(app)

    with app.app_context():
        seller = db.session.get(Seller, fixture["seller_id"])
        # The release seed's illustrative photo uses an intentionally
        # unresolvable `.test` hostname. Image Lab eagerly previews imported
        # photos, so leave this UI-only product photo-free instead of asking
        # the app to resolve a fake external URL and producing a synthetic 400.
        source_product = db.session.get(ImportedProduct, fixture["source_id"])
        source_product.photo_urls = "[]"
        # A deliberately unverified, expired JWT exercises the public status
        # hint. It is synthetic and must never appear in rendered HTML.
        jwt_payload = base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip("=")
        synthetic_jwt = "synthetic-header." + jwt_payload + ".synthetic-signature"
        seller.wb_api_key = synthetic_jwt
        account = db.session.get(
            SellerMarketplaceAccount, fixture["account_id"]
        )
        account.connection_status = "error"
        account.connection_checked_at = datetime.utcnow()
        account.credential_expires_at = datetime.utcnow() - timedelta(days=1)
        account.last_error_code = "synthetic_connection_check_failed"
        account.last_error_message = "Синтетическая проверка кабинета не подтвердила доступ."

        if not Marketplace.query.filter_by(code="wb").first():
            db.session.add(Marketplace(
                name="Wildberries",
                code="wb",
                adapter_code="wb",
                is_active=True,
            ))

        admin = User(
            username="ux01-synthetic-admin",
            email="ux01-synthetic-admin@example.test",
            is_active=True,
            is_admin=True,
        )
        admin.set_password("synthetic-admin-password")
        db.session.add(admin)
        db.session.commit()

    # The server itself binds only to loopback. No live seller credentials are
    # read: all account rows and keys above are synthetic.
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    assets = load_assets()
    browser = None
    page = None

    def bridge(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.hostname == "127.0.0.1" and parsed.port == server.server_port:
            if request.method not in {"GET", "HEAD"}:
                REPORT["browser_mutations"].append({
                    "method": request.method,
                    "path": parsed.path,
                })
                route.abort()
                return
            if parsed.path.startswith("/api/"):
                REPORT["browser_api_reads"].append({
                    "method": request.method,
                    "path": parsed.path,
                })
            route.continue_()
            return

        if request.url.startswith(("data:", "blob:", "about:")):
            route.continue_()
            return
        asset = assets.get(request.url)
        if not asset:
            clean = request.url.split("?", 1)[0]
            asset = assets.get(clean)
        if not asset and parsed.path in ("", "/"):
            asset = assets.get(f"{parsed.scheme}://{parsed.netloc}")
        if asset:
            route.fulfill(
                status=200,
                body=asset["payload"],
                content_type=asset["content_type"],
                headers={"access-control-allow-origin": "*"},
            )
            return
        REPORT["unexpected_external_requests"].append({
            "url": request.url,
            "method": request.method,
        })
        route.abort()

    def goto(page, path: str, expected=200):
        response = page.goto(base + path, wait_until="domcontentloaded")
        if not response or response.status != expected:
            raise AssertionError((path, response.status if response else None))
        page.locator("#main-content").wait_for()
        page.evaluate("document.fonts.ready")
        REPORT["pages"].append({
            "path": urlsplit(path).path,
            "status": response.status,
            "source": VARIANT,
        })
        return response

    def record_http_error(response):
        """Keep bounded, synthetic local status telemetry for failed resources.

        The URL is restricted to this fixture's loopback origin and records
        only path/query (never request bodies, cookies, or response bodies).
        """
        if response.status < 400:
            return
        parsed = urlsplit(response.url)
        if parsed.hostname != "127.0.0.1" or parsed.port != server.server_port:
            return
        REPORT["http_errors"].append({
            "path": parsed.path,
            "query": parsed.query,
            "status": response.status,
            "method": response.request.method,
            "resource_type": response.request.resource_type,
        })

    def record_console_error(message):
        if message.type == "error":
            REPORT["console_errors"].append({
                "text": message.text,
                "location": message.location,
            })

    def add_cookie(context, username: str, password: str):
        context.add_cookies([login_cookie(app, username, password, base)])

    def settle_workspace_layout(target_width: int) -> dict:
        page.wait_for_function("""() => {
            const stylesheet = document.querySelector('link[href*="seller-workspace.css"]');
            const sellerShell = !!document.querySelector('.seller-workspace-groups');
            return !!window.Alpine && (!sellerShell || !!stylesheet?.sheet);
        }""")
        page.evaluate("""() => new Promise(resolve => {
            requestAnimationFrame(() => requestAnimationFrame(resolve));
        })""")
        metrics = page.evaluate("""() => {
            const shell = document.querySelector('.main-content');
            const sidebar = document.querySelector('.sidebar');
            const open = Alpine.$data(document.body).sidebarOpen;
            const root = getComputedStyle(document.documentElement);
            const expected = innerWidth <= 1023 ? 0 : parseFloat(
                root.getPropertyValue(open ? '--sidebar-w' : '--sidebar-collapsed'));
            return {width:innerWidth, left:shell.getBoundingClientRect().left,
                marginLeft:getComputedStyle(shell).marginLeft, expected,
                sidebarWidth:sidebar.getBoundingClientRect().width};
        }""")
        assert metrics["width"] == target_width, (target_width, metrics)
        assert abs(metrics["left"] - metrics["expected"]) <= 1, (target_width, metrics)
        return metrics

    try:
        with sync_playwright() as playwright:
            launch_options = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            executable = os.environ.get("CHROMIUM_BIN")
            if executable:
                launch_options["executable_path"] = executable
            browser = playwright.chromium.launch(**launch_options)

            seller_context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                service_workers="block",
                reduced_motion="reduce",
            )
            seller_context.route("**/*", bridge)
            seller_context.add_init_script("""
                (() => {
                    const q = new URL(location.href).searchParams;
                    const theme = q.get('__ux_theme');
                    const sidebar = q.get('__ux_sidebar');
                    if (theme === 'light' || theme === 'dark') localStorage.setItem('sh-theme', theme);
                    if (sidebar === 'open' || sidebar === 'collapsed') {
                        localStorage.setItem('sh-sidebar', sidebar === 'open' ? 'open' : 'closed');
                    }
                })();
            """)
            add_cookie(seller_context, USERNAME, PASSWORD)
            page = seller_context.new_page()
            page.set_default_timeout(12000)
            page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
            page.on("console", record_console_error)
            page.on("response", record_http_error)

            with app.test_request_context():
                from flask import url_for
                dashboard_url = url_for("dashboard")
                quality_url = url_for("card_quality_page")
                beta_url = url_for("card_quality_beta_page")
                api_settings_url = url_for("api_settings")
                accounts_url = url_for(
                    "marketplace_accounts.index",
                    account_id=fixture["account_id"],
                )
                readiness_url = url_for("marketplace_readiness.index")
                auto_publish_url = url_for(
                    "auto_publish_settings",
                    marketplace="ozon",
                    account_id=fixture["account_id"],
                )
                product_defaults_url = url_for("product_defaults_page")
                api_logs_url = url_for("api_logs")
                image_lab_url = url_for("image_lab_page")
                account_health_url = url_for(
                    "ozon_account_health.page",
                    account_id=fixture["account_id"],
                )
                admin_url = url_for("admin_panel")

            dashboard_path = urlsplit(dashboard_url).path
            page.set_viewport_size({"width": 1280, "height": 1000})
            goto(page, dashboard_url)
            page.add_style_tag(content=".main-content { transition: none !important; }")
            for width in WIDTHS:
                page.set_viewport_size({"width": width, "height": 1000})
                settled = settle_workspace_layout(width)
                for theme in ("light", "dark"):
                    set_theme_and_wait(page, theme)
                    page.wait_for_function(
                        "!document.querySelector('.main-content')?.getAnimations().some(a => a.playState === 'running')",
                        timeout=3000,
                    )
                    metrics = page.evaluate("""() => {
                        const root = document.documentElement;
                        const main = document.querySelector('.main-content');
                        const sidebar = document.querySelector('.sidebar');
                        const box = node => {
                            if (!node) return null;
                            const r = node.getBoundingClientRect();
                            return {left: Math.round(r.left), right: Math.round(r.right),
                                width: Math.round(r.width), height: Math.round(r.height)};
                        };
                        return {width: innerWidth, clientWidth: root.clientWidth,
                            scrollWidth: root.scrollWidth,
                            overflows: root.scrollWidth > root.clientWidth + 1,
                            theme: root.dataset.theme,
                            main: box(main), sidebar: box(sidebar),
                            activeGroup: document.querySelector('.seller-workspace-groups')?.dataset.activeGroup || null,
                            focusColor: getComputedStyle(document.querySelector('.seller-workspace-group-toggle') || document.body).outlineColor};
                    }""")
                    metrics["layout_settle"] = settled
                    REPORT["layouts"].append(metrics)
                    if width in (390, 1024):
                        page.screenshot(
                            path=str(ARTIFACTS / f"{VARIANT}-{theme}-{width}.png"),
                            animations="disabled",
                        )

            # Server-rendered active routes and settings/account context.
            smoke_pages = (
                ("dashboard", dashboard_url),
                ("quality_classic", quality_url),
                ("quality_beta", beta_url),
                ("marketplace_accounts", accounts_url),
                ("api_settings", api_settings_url),
                ("marketplace_readiness", readiness_url),
                ("auto_publish_ozon", auto_publish_url),
                ("product_defaults", product_defaults_url),
                ("api_logs", api_logs_url),
                ("image_lab", image_lab_url),
                ("account_health", account_health_url),
            )
            page.set_viewport_size({"width": 1280, "height": 1000})
            for label, path in smoke_pages:
                response = goto(page, path)
                body = page.locator("body").inner_text()
                if label == "quality_classic":
                    assert page.locator("#cq-search").count() == 1
                    if VARIANT == "worktree":
                        assert "Качество и медиа" in body
                elif label == "quality_beta":
                    assert page.locator("#card-quality-app").count() == 1
                    if VARIANT == "worktree":
                        assert "Качество WB · очередь" in body
                elif label == "marketplace_accounts":
                    assert fixture["account_id"]
                    assert "Ozon CI 0" in body
                elif label == "api_settings":
                    assert "wb-api-settings-title" in response.text()
                    if VARIANT == "worktree":
                        assert "seller-workspace-settings-nav" in response.text()
                elif label == "marketplace_readiness":
                    assert "Готовность" in body
                elif label == "auto_publish_ozon":
                    assert "Synthetic" in body or "Ozon CI 0" in body
                    assert "pendingCount: " in response.text()
                    if VARIANT == "worktree":
                        assert "Срок ключа истёк" in body
                        assert "не подтверждает" in body
                elif label == "account_health":
                    assert "Synthetic" not in body or "Кабинет" in body
                REPORT["interactions"].append({"check": f"render_{label}", "status": "passed"})

            # Measure the new shared primitives on every page that owns one.
            # The checks stay bounded to the primitive and its content column;
            # legacy page content may have its own responsive behavior.
            if VARIANT == "worktree":
                primitive_pages = (
                    ("quality_classic", quality_url, ".seller-workspace-context"),
                    ("quality_beta", beta_url, ".seller-workspace-context"),
                    ("image_lab", image_lab_url, ".seller-workspace-context"),
                    ("api_settings", api_settings_url, ".seller-workspace-settings-nav"),
                    ("marketplace_accounts", accounts_url, ".seller-workspace-settings-nav"),
                    ("marketplace_readiness", readiness_url, ".seller-workspace-settings-nav"),
                    ("auto_publish_ozon", auto_publish_url, ".seller-workspace-settings-nav"),
                )
                for label, path, selector in primitive_pages:
                    goto(page, path)
                    page.add_style_tag(content=".main-content { transition: none !important; }")
                    for width in (390, 1440):
                        page.set_viewport_size({"width": width, "height": 1000})
                        settled = settle_workspace_layout(width)
                        for theme in ("light", "dark"):
                            set_theme_and_wait(page, theme)
                            metrics = page.evaluate("""selector => {
                                const primitive = document.querySelector(selector);
                                const main = document.querySelector('#main-content');
                                const shell = main?.closest('.main-content');
                                const rect = el => {
                                    const r = el.getBoundingClientRect();
                                    return {left:r.left, right:r.right, top:r.top, bottom:r.bottom,
                                        width:r.width, height:r.height};
                                };
                                if (!primitive || !main) return {missing:true, selector};
                                const mainRect = rect(main);
                                const primitiveRect = rect(primitive);
                                const links = Array.from(primitive.querySelectorAll('a,button,[role="button"]'))
                                    .filter(el => {
                                        const s = getComputedStyle(el), r = el.getBoundingClientRect();
                                        return s.display !== 'none' && s.visibility !== 'hidden'
                                            && r.width > 0 && r.height > 0;
                                    }).map(el => {
                                        const r = rect(el);
                                        return {label:(el.innerText || el.getAttribute('aria-label') || '').trim(),
                                            className:String(el.className || ''),
                                            left:r.left, right:r.right, width:r.width, height:r.height,
                                            minHeight:getComputedStyle(el).minHeight,
                                            scrollWidth:el.scrollWidth, clientWidth:el.clientWidth};
                                    });
                                const root = document.documentElement, body = document.body;
                                const overflow = node => getComputedStyle(node).overflowX;
                                const shellStyle = shell ? getComputedStyle(shell) : null;
                                return {missing:false, viewport:innerWidth,
                                    mobileMedia:matchMedia('(max-width: 639px)').matches,
                                    theme:root.dataset.theme,
                                    main:mainRect, primitive:primitiveRect,
                                    shell: shell ? {left:rect(shell).left,right:rect(shell).right,
                                        marginLeft:shellStyle.marginLeft,width:shellStyle.width,
                                        paddingTop:shellStyle.paddingTop} : null,
                                    primitiveScrollWidth:primitive.scrollWidth,
                                    primitiveClientWidth:primitive.clientWidth,
                                    rootOverflowX:overflow(root), bodyOverflowX:overflow(body),
                                    links};
                            }""", selector)
                            assert not metrics.get("missing"), (label, selector, metrics)
                            assert metrics["viewport"] == width, (label, width, metrics)
                            main_box = metrics["main"]
                            primitive_box = metrics["primitive"]
                            assert primitive_box["left"] >= max(0, main_box["left"]) - 1, (label, width, theme, metrics)
                            assert primitive_box["right"] <= min(width, main_box["right"]) + 1, (label, width, theme, metrics)
                            assert metrics["primitiveScrollWidth"] <= metrics["primitiveClientWidth"] + 2, (label, width, theme, metrics)
                            assert metrics["rootOverflowX"] not in {"hidden", "clip"}, (label, width, theme, metrics)
                            assert metrics["bodyOverflowX"] not in {"hidden", "clip"}, (label, width, theme, metrics)
                            for link in metrics["links"]:
                                assert link["left"] >= max(0, main_box["left"]) - 1, (label, width, theme, link, metrics)
                                assert link["right"] <= min(width, main_box["right"]) + 1, (label, width, theme, link, metrics)
                                assert link["scrollWidth"] <= link["clientWidth"] + 2, (label, width, theme, link)
                                if width == 390:
                                    assert link["height"] >= 44, (label, width, theme, link, metrics)
                            REPORT["layouts"].append({
                                "page": label, "primitive": selector,
                                "width": width, "theme": theme,
                                "link_count": len(metrics["links"]),
                                "min_link_height": min((item["height"] for item in metrics["links"]), default=None),
                                "primitive_scroll_width": metrics["primitiveScrollWidth"],
                                "primitive_client_width": metrics["primitiveClientWidth"],
                                "root_overflow_x": metrics["rootOverflowX"],
                                "body_overflow_x": metrics["bodyOverflowX"],
                                "layout_settle": settled,
                            })
                            if label in {"quality_classic", "quality_beta", "api_settings"}:
                                page.screenshot(
                                    path=str(ARTIFACTS / f"{VARIANT}-{label}-{theme}-{width}.png"),
                                    animations="disabled",
                                )

                # Simulate a 200% text-only enlargement inside each new shared
                # primitive at the narrow breakpoint, then verify its links
                # still fit the content column and retain touch-sized targets.
                for label, path, selector in primitive_pages:
                    goto(page, path)
                    page.add_style_tag(content=".main-content { transition: none !important; }")
                    page.set_viewport_size({"width": 390, "height": 1000})
                    settle_workspace_layout(390)
                    set_theme_and_wait(page, "light")
                    page.evaluate("""selector => {
                        const root = document.querySelector(selector);
                        if (!root) throw new Error(`Missing text zoom target ${selector}`);
                        const nodes = [root, ...root.querySelectorAll('*')]
                            .filter(el => !['SCRIPT','STYLE'].includes(el.tagName));
                        window.__ux01FontRestore = nodes.map(el => ({
                            el, value:el.style.getPropertyValue('font-size'),
                            priority:el.style.getPropertyPriority('font-size'),
                            computed:getComputedStyle(el).fontSize
                        }));
                        for (const item of window.__ux01FontRestore) {
                            const px = parseFloat(item.computed);
                            if (Number.isFinite(px) && px > 0) {
                                item.el.style.setProperty('font-size', `${px * 2}px`, 'important');
                            }
                        }
                    }""", selector)
                    zoom_metrics = page.evaluate("""selector => {
                        const primitive = document.querySelector(selector);
                        const main = document.querySelector('#main-content');
                        const p = primitive.getBoundingClientRect(), m = main.getBoundingClientRect();
                        const links = Array.from(primitive.querySelectorAll('a,button,[role="button"]'))
                            .filter(el => {
                                const s=getComputedStyle(el),r=el.getBoundingClientRect();
                                return s.display !== 'none' && s.visibility !== 'hidden' && r.width>0 && r.height>0;
                            });
                        const textOverflow = [];
                        const walker = document.createTreeWalker(primitive, NodeFilter.SHOW_TEXT);
                        let node;
                        while ((node = walker.nextNode())) {
                            if (!node.textContent.trim()) continue;
                            const range = document.createRange(); range.selectNodeContents(node);
                            const host = node.parentElement.closest('a,button,[role="button"]') || node.parentElement;
                            const h = host.getBoundingClientRect();
                            for (const r of range.getClientRects()) {
                                if (r.left < h.left-1 || r.right > h.right+1
                                    || r.left < Math.max(0,m.left)-1 || r.right > Math.min(innerWidth,m.right)+1) {
                                    textOverflow.push({text:node.textContent.trim(),left:r.left,right:r.right,
                                        hostLeft:h.left,hostRight:h.right});
                                }
                            }
                        }
                        return {left:p.left,right:p.right,mainLeft:m.left,mainRight:m.right,
                            scrollWidth:primitive.scrollWidth,clientWidth:primitive.clientWidth,
                            minLinkHeight:Math.min(...links.map(el=>el.getBoundingClientRect().height)),
                            linkCount:links.length,textOverflow};
                    }""", selector)
                    assert zoom_metrics["left"] >= max(0, zoom_metrics["mainLeft"]) - 1, (label, zoom_metrics)
                    assert zoom_metrics["right"] <= min(390, zoom_metrics["mainRight"]) + 1, (label, zoom_metrics)
                    assert zoom_metrics["scrollWidth"] <= zoom_metrics["clientWidth"] + 2, (label, zoom_metrics)
                    assert zoom_metrics["linkCount"] > 0, (label, zoom_metrics)
                    assert zoom_metrics["minLinkHeight"] >= 44, (label, zoom_metrics)
                    assert not zoom_metrics["textOverflow"], (label, zoom_metrics["textOverflow"])
                    page.evaluate("""() => {
                        for (const item of window.__ux01FontRestore || []) {
                            if (item.value) item.el.style.setProperty('font-size', item.value, item.priority);
                            else item.el.style.removeProperty('font-size');
                        }
                        delete window.__ux01FontRestore;
                    }""")
                    REPORT["layouts"].append({
                        "page": label, "primitive": selector, "width": 390,
                        "theme": "light", "text_zoom": "200%",
                        "link_count": zoom_metrics["linkCount"],
                        "min_link_height": zoom_metrics["minLinkHeight"],
                        "text_overflow_count": len(zoom_metrics["textOverflow"]),
                    })
                REPORT["interactions"].append({"check": "workspace_primitives_responsive_and_200pct_text", "status": "passed"})

            # The shell's seller menu must keep a single server-selected group;
            # changing groups is mutually exclusive and keyboard accessible.
            if VARIANT == "worktree":
                page.set_viewport_size({"width": 1280, "height": 1000})
                goto(page, dashboard_url)
                page.wait_for_function("!!window.Alpine")

                def focus_metrics(surface_selector: str) -> dict:
                    return page.evaluate("""surfaceSelector => {
                        const el = document.activeElement;
                        const parse = color => {
                            const values = color.match(/[\\d.]+/g)?.map(Number) || [];
                            if (values.length < 3) return null;
                            return values.slice(0,3).map(v => v / 255);
                        };
                        const linear = value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
                        const luminance = color => {
                            const rgb = parse(color);
                            return rgb ? .2126*linear(rgb[0]) + .7152*linear(rgb[1]) + .0722*linear(rgb[2]) : null;
                        };
                        const color = getComputedStyle(el).outlineColor;
                        const surface = document.querySelector(surfaceSelector);
                        let node = surface, background = null;
                        while (node && !background) {
                            const value = getComputedStyle(node).backgroundColor;
                            const channels = value.match(/[\\d.]+/g)?.map(Number) || [];
                            if (channels.length < 4 || channels[3] >= .99) background = value;
                            node = node.parentElement;
                        }
                        const a=luminance(color), b=luminance(background);
                        return {tag:el.tagName, id:el.id, className:String(el.className || ''),
                            focusVisible:el.matches(':focus-visible'), outlineColor:color,
                            outlineWidth:parseFloat(getComputedStyle(el).outlineWidth),
                            outlineStyle:getComputedStyle(el).outlineStyle,
                            actualTheme:document.documentElement.dataset.theme,
                            storedTheme:localStorage.getItem('sh-theme'),
                            background, contrast:a === null || b === null ? null : (Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
                    }""", surface_selector)

                def assert_focus_ring(result: dict, context: str, theme: str) -> None:
                    assert result["focusVisible"], (context, result)
                    assert result["outlineWidth"] >= 2 and result["outlineStyle"] == "solid", (context, result)
                    assert result["contrast"] is not None and result["contrast"] >= 3, (context, result)
                    assert result["actualTheme"] == theme, (context, theme, result)
                    assert result["storedTheme"] == theme, (context, theme, result)
                    REPORT["interactions"].append({
                        "check": context, "status": "passed",
                        "outline_width": result["outlineWidth"],
                        "outline_color": result["outlineColor"],
                        "background": result["background"],
                        "contrast": round(result["contrast"], 2),
                        "actual_theme": result["actualTheme"],
                    })

                def sidebar_normal_text_metrics() -> dict:
                    return page.evaluate("""() => {
                        const visible = el => {
                            const s=getComputedStyle(el),r=el.getBoundingClientRect();
                            return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0;
                        };
                        const link=Array.from(document.querySelectorAll('.seller-workspace-sublink'))
                            .find(el => !el.classList.contains('active') && visible(el));
                        if (!link) return {missing:true};
                        const parse = color => (color.match(/[\\d.]+/g)||[]).map(Number).slice(0,3).map(v=>v/255);
                        const linear = value => value<=.04045 ? value/12.92 : ((value+.055)/1.055)**2.4;
                        const luminance = color => { const rgb=parse(color); return rgb.length===3 ? .2126*linear(rgb[0])+.7152*linear(rgb[1])+.0722*linear(rgb[2]) : null; };
                        const color=getComputedStyle(link).color;
                        const background=getComputedStyle(document.querySelector('.sidebar')).backgroundColor;
                        const a=luminance(color),b=luminance(background);
                        return {missing:false,label:link.textContent.trim(),color,background,
                            fontSize:parseFloat(getComputedStyle(link).fontSize),
                            actualTheme:document.documentElement.dataset.theme,
                            storedTheme:localStorage.getItem('sh-theme'),
                            contrast:a===null||b===null?null:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
                    }""")

                for theme in ("light", "dark"):
                    # The query seeds localStorage before the application's
                    # theme loader runs; this post-navigation assertion waits
                    # for the resulting theme to settle before focus checks.
                    page.evaluate(
                        "theme => localStorage.setItem('sh-theme', theme)", theme
                    )
                    goto(page, theme_path(dashboard_url, theme))
                    set_theme_and_wait(page, theme)
                    page.evaluate("""() => {
                        Alpine.$data(document.body).sidebarOpen = true;
                        Alpine.$data(document.querySelector('.seller-workspace-groups')).activeGroup = 'overview';
                    }""")
                    page.wait_for_function("""() => Array.from(document.querySelectorAll('.seller-workspace-sublink'))
                        .some(el => !el.classList.contains('active') && el.getBoundingClientRect().height > 0)""")
                    normal_text = sidebar_normal_text_metrics()
                    assert not normal_text.get("missing"), (theme, normal_text)
                    assert normal_text["actualTheme"] == theme and normal_text["storedTheme"] == theme, (theme, normal_text)
                    assert normal_text["fontSize"] >= 12 and normal_text["contrast"] >= 4.5, (theme, normal_text)
                    REPORT["interactions"].append({
                        "check": f"sidebar_normal_link_text_contrast_{theme}",
                        "status": "passed",
                        "font_size": normal_text["fontSize"],
                        "color": normal_text["color"],
                        "background": normal_text["background"],
                        "contrast": round(normal_text["contrast"], 2),
                        "actual_theme": normal_text["actualTheme"],
                    })
                    page.locator("#seller-workspace-toggle-products").focus()
                    page.keyboard.press("Enter")
                    page.wait_for_function("document.querySelector('#seller-workspace-toggle-products')?.getAttribute('aria-expanded') === 'true'")
                    assert_focus_ring(focus_metrics(".sidebar"), f"sidebar_focus_after_enter_{theme}", theme)
                    page.keyboard.press("Tab")
                    page.wait_for_function("document.activeElement?.matches('.seller-workspace-sublink:focus-visible')")
                    assert_focus_ring(focus_metrics(".sidebar"), f"sidebar_focus_after_tab_{theme}", theme)

                    page.evaluate(
                        "theme => localStorage.setItem('sh-theme', theme)", theme
                    )
                    goto(page, theme_path(quality_url, theme))
                    set_theme_and_wait(page, theme)
                    page.locator(".seller-workspace-context-link").first.focus()
                    page.keyboard.press("Tab")
                    page.wait_for_function("document.activeElement?.matches('.seller-workspace-context-link:focus-visible')")
                    assert_focus_ring(focus_metrics(".seller-workspace-context"), f"quality_context_focus_after_tab_{theme}", theme)

                    page.evaluate(
                        "theme => localStorage.setItem('sh-theme', theme)", theme
                    )
                    goto(page, theme_path(api_settings_url, theme))
                    set_theme_and_wait(page, theme)
                    page.locator(".seller-workspace-settings-link").first.focus()
                    page.keyboard.press("Tab")
                    page.wait_for_function("document.activeElement?.matches('.seller-workspace-settings-link:focus-visible')")
                    assert_focus_ring(focus_metrics(".seller-workspace-settings-nav"), f"settings_focus_after_tab_{theme}", theme)

                goto(page, dashboard_url)
                page.wait_for_function("!!window.Alpine")
                group_buttons = page.locator(".seller-workspace-group-toggle")
                assert group_buttons.count() == 8
                initial_open = page.locator(
                    ".seller-workspace-group-toggle[aria-expanded='true']"
                ).count()
                assert initial_open == 1
                page.locator("#seller-workspace-toggle-products").focus()
                page.keyboard.press("Enter")
                page.wait_for_function("""() => {
                    const products = document.querySelector('#seller-workspace-toggle-products');
                    const overview = document.querySelector('#seller-workspace-toggle-overview');
                    return products?.getAttribute('aria-expanded') === 'true'
                        && overview?.getAttribute('aria-expanded') === 'false';
                }""")
                REPORT["interactions"].append({"check": "keyboard_group_toggle_single_open", "status": "passed"})

                page.locator(".sidebar-toggle").click()
                page.wait_for_function("Alpine.$data(document.body).sidebarOpen === false")
                assert page.locator(
                    "#seller-workspace-toggle-products"
                ).get_attribute("aria-expanded") == "false"
                page.locator("#seller-workspace-toggle-competitors").click()
                page.wait_for_function("""() => Alpine.$data(document.body).sidebarOpen === true
                    && Alpine.$data(document.querySelector('.seller-workspace-groups')).activeGroup === 'competitors'
                    && document.querySelector('#seller-workspace-toggle-competitors')?.getAttribute('aria-expanded') === 'true'""")
                REPORT["interactions"].append({"check": "collapsed_sidebar_group_click_opens_sidebar_and_panel", "status": "passed"})

                page.keyboard.press("Control+k")
                palette = page.locator(".sh-cmdpal")
                palette.wait_for(state="visible")
                command_input = page.locator(".sh-cmdpal-input")
                command_input.fill("Качество WB")
                page.wait_for_function("""() => Array.from(document.querySelectorAll('.sh-cmdpal-item'))
                    .some(item => item.style.display !== 'none' && item.textContent.includes('Качество WB'))""")
                page.keyboard.press("ArrowDown")
                active_palette_item = page.locator(".sh-cmdpal-item.active").inner_text()
                assert "Качество WB" in active_palette_item
                page.keyboard.press("Escape")
                palette.wait_for(state="hidden")
                REPORT["interactions"].append({"check": "command_palette_search_arrow_escape", "status": "passed"})

                page.keyboard.press("Control+k")
                palette.wait_for(state="visible")
                command_input = page.locator(".sh-cmdpal-input")
                command_input.fill("Внутренние товары")
                page.wait_for_function("""() => Array.from(document.querySelectorAll('.sh-cmdpal-item'))
                    .some(item => item.style.display !== 'none' && item.textContent.includes('Внутренние товары'))""")
                # Every useful old base URL is still present in the command
                # destination layer/menu; labels now distinguish the objects.
                visible_palette = page.locator(".sh-cmdpal-item").all_inner_texts()
                assert any("Внутренние товары" in text for text in visible_palette)
                assert any("Карточки WB" in text for text in visible_palette)
                assert any("Карточки кабинетов" in text for text in visible_palette)
                assert any("Черновики Ozon" in text for text in visible_palette)
                REPORT["interactions"].append({"check": "command_palette_object_labels", "status": "passed"})

                # Active group is evaluated from the real Flask request endpoint.
                goto(page, quality_url)
                assert page.locator('.seller-workspace-groups').get_attribute("data-active-group") == "products"
                goto(page, api_settings_url)
                assert page.locator('.seller-workspace-utility-link.active').inner_text().strip() == "Настройки"
                assert page.locator('.seller-workspace-settings-nav').count() == 1
                REPORT["interactions"].append({"check": "server_active_group_and_settings", "status": "passed"})

            # Administrator account has its own branch and no seller-only menu.
            admin_context = browser.new_context(
                viewport={"width": 1280, "height": 1000},
                service_workers="block",
                reduced_motion="reduce",
            )
            admin_context.route("**/*", bridge)
            admin_context.add_init_script(
                "localStorage.setItem('sh-theme','light'); localStorage.setItem('sh-sidebar','open');"
            )
            add_cookie(admin_context, "ux01-synthetic-admin", "synthetic-admin-password")
            admin_page = admin_context.new_page()
            admin_page.set_default_timeout(12000)
            admin_page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
            admin_page.on("console", record_console_error)
            admin_page.on("response", record_http_error)
            goto(admin_page, admin_url)
            if VARIANT == "worktree":
                assert admin_page.locator(".seller-workspace-groups").count() == 0
                assert admin_page.get_by_text("Админ", exact=True).count() > 0
                REPORT["interactions"].append({"check": "admin_branch_not_replaced_by_seller_menu", "status": "passed"})
            admin_page.close()
            admin_context.close()
            browser.close()
            browser = None

            assert not REPORT["browser_mutations"], REPORT["browser_mutations"]
            assert not REPORT["unexpected_external_requests"], REPORT["unexpected_external_requests"]
            assert not REPORT["http_errors"], REPORT["http_errors"]
            assert not REPORT["console_errors"], REPORT["console_errors"]
            assert REPORT["provider_attempts"] == 0, REPORT["provider_attempts"]
            assert not REPORT["javascript_errors"], REPORT["javascript_errors"]
            REPORT["status"] = "completed"
            REPORT["authenticated_synthetic_users"] = [USERNAME, "ux01-synthetic-admin"]
            REPORT["account_id"] = fixture["account_id"]
    except Exception as exc:
        REPORT["status"] = "failed"
        REPORT["harness_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        raise
    finally:
        if browser:
            try:
                browser.close()
            except Exception as exc:
                REPORT["status"] = "failed"
                REPORT.setdefault("cleanup_errors", []).append(type(exc).__name__)
        try:
            server.shutdown()
        except Exception as exc:
            REPORT["status"] = "failed"
            REPORT.setdefault("cleanup_errors", []).append(type(exc).__name__)
        try:
            server.server_close()
        except Exception as exc:
            REPORT["status"] = "failed"
            REPORT.setdefault("cleanup_errors", []).append(type(exc).__name__)
        if REPORT["status"] == "running":
            REPORT["status"] = "failed"
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({
            "report": str(REPORT_PATH),
            "status": REPORT["status"],
            "layouts": len(REPORT["layouts"]),
            "pages": len(REPORT["pages"]),
            "interactions": len(REPORT["interactions"]),
            "provider_attempts": REPORT["provider_attempts"],
            "unexpected_external_requests": len(REPORT["unexpected_external_requests"]),
            "javascript_errors": len(REPORT["javascript_errors"]),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
