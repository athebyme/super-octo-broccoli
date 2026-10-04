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
from io import BytesIO
import json
import logging
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import requests
from jinja2 import ChoiceLoader, DictLoader
from playwright.sync_api import sync_playwright
from sqlalchemy import event
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
    "legacy_action_checks": [],
    "synthetic_wb_photo_asset_reads": [],
    "expected_http_denials": [],
    "legacy_domain_sql_writes": [],
    "legacy_domain_state": {},
    "image_lab_fake_reads": [],
    "image_lab_wb_fallback_reads": [],
    "image_lab_wb_fallback_downloads": 0,
    "image_lab_experiments_before": None,
    "image_lab_experiments_after": None,
    "wb_summary_cache_seeded_observations": {
        "source": "disposable synthetic fixture rows",
        "analytics": [],
        "finance": [],
    },
    "wb_summary_route_reads": [],
    "fixture_asset_reads": [],
    "legacy_post_count": 0,
    "http_errors": [],
    "expected_denial_console_errors": [],
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
    fixture_photo_url = "https://ozon-fixture.test/product.svg"
    assets[fixture_photo_url] = {
        "payload": (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="72"'
            b'><rect width="100%" height="100%" fill="#f3f0ec"/>'
            b'<text x="50%" y="55%" text-anchor="middle" fill="#a34a2f">UX</text></svg>'
        ),
        "content_type": "image/svg+xml",
    }
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
        AnalyticsSnapshot, CardEditHistory, FinanceSnapshot, ImportedProduct,
        ImageGenerationExperiment, Marketplace, MarketplaceListing, Product,
        Seller, SellerMarketplaceAccount, SocialAccount, User, db,
    )
    from services.analytics_service import AnalyticsService
    from services.finance_service import FinanceService
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

        # Dashboard summaries exercise their real service cache paths. Fresh,
        # positive synthetic rows prevent the fixture from attempting provider
        # reads while keeping the provider-attempt guard strict at zero.
        cache_created_at = datetime.utcnow()
        for period_code in ("7d", "30d", "90d", "1y"):
            period_start, period_end = AnalyticsService._calc_period(period_code)
            db.session.add(AnalyticsSnapshot(
                seller_id=seller.id,
                period_start=period_start,
                period_end=period_end,
                revenue=125.0,
                orders_count=1,
                buyouts_count=1,
                open_card_count=1,
                daily_data=[],
                top_products=[],
                created_at=cache_created_at,
            ))
            REPORT["wb_summary_cache_seeded_observations"]["analytics"].append({
                "period": period_code,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "orders_count": 1,
                "open_card_count": 1,
                "created_at": cache_created_at.isoformat(),
            })

            finance_start, finance_end = FinanceService._calc_period(period_code)
            db.session.add(FinanceSnapshot(
                seller_id=seller.id,
                period_start=finance_start,
                period_end=finance_end,
                sales_total=125.0,
                for_pay_total=100.0,
                report_rows_count=1,
                weekly_data=[],
                recent_transactions=[],
                created_at=cache_created_at,
            ))
            REPORT["wb_summary_cache_seeded_observations"]["finance"].append({
                "period": period_code,
                "period_start": finance_start.isoformat(),
                "period_end": finance_end.isoformat(),
                "report_rows_count": 1,
                "created_at": cache_created_at.isoformat(),
            })

        if not Marketplace.query.filter_by(code="wb").first():
            db.session.add(Marketplace(
                name="Wildberries",
                code="wb",
                adapter_code="wb",
                is_active=True,
            ))

        if VARIANT == "worktree":
            # These rows support bounded GET-only navigation evidence. Their
            # media/source bytes are mocked in this fixture; the key below is
            # never valid at WB and is never used for a provider request.
            wb_product = Product(
                seller_id=seller.id,
                nm_id=10_000_001,  # vol=100 resolves via the local static map
                vendor_code="R10-WB-LEGACY-001",
                title="Синтетическая карточка WB для переходов",
                brand="R10 Synthetic",
                object_name="Fixture",
                quantity=3,
                is_active=True,
                photos_json="[1]",
                sizes_json=json.dumps([{"techSize": "M", "skus": ["R10-LEGACY-SKU"]}]),
                characteristics_json=json.dumps([], ensure_ascii=False),
            )
            foreign_seller = db.session.get(
                Seller,
                db.session.get(SellerMarketplaceAccount, fixture["foreign_account_id"]).seller_id,
            )
            foreign_wb_product = Product(
                seller_id=foreign_seller.id,
                nm_id=10_000_002,
                vendor_code="R10-FOREIGN-MUST-STAY-HIDDEN",
                title="Private foreign synthetic product",
                object_name="Fixture",
                quantity=9,
                is_active=True,
                photos_json="[]",
                sizes_json="[]",
                characteristics_json="[]",
            )
            db.session.add_all([wb_product, foreign_wb_product])
            db.session.flush()
            db.session.add(CardEditHistory(
                product_id=wb_product.id,
                seller_id=seller.id,
                action="update",
                changed_fields=["title"],
                snapshot_before={"title": "Исходное синтетическое название"},
                snapshot_after={"title": wb_product.title},
                wb_synced=False,
                wb_sync_status="failed",
                wb_error_message="Synthetic local fixture history only.",
                user_comment="Read-only legacy route fixture.",
            ))
            known_photo = ImportedProduct(
                seller_id=seller.id,
                external_id="r10-known-imported-photo",
                external_vendor_code="R10-PHOTO-SOURCE",
                source_type="synthetic_fixture",
                title="Synthetic imported photo source",
                category="Fixture",
                description="Read-only source context for the Image Lab fixture.",
                photo_urls="[]",
                original_data=json.dumps({
                    "title": "Synthetic imported photo source",
                    "photo_urls": ["fixture-known-imported-photo"],
                }),
            )
            manual_empty_photo = ImportedProduct(
                seller_id=seller.id,
                product_id=wb_product.id,
                wb_nm_id=wb_product.nm_id,
                external_id="r10-manual-empty-photo",
                external_vendor_code="R10-EMPTY-PHOTO",
                source_type="synthetic_fixture",
                title="Synthetic empty manual-photo override",
                category="Fixture",
                photo_urls="[]",
                content_edit_version=2,
                content_overrides_json=json.dumps({
                    "schema_version": 1,
                    "fields": {
                        "photos": {
                            "value": [],
                            "inherited_value": ["fixture-inherited-source-photo"],
                            "inherited_origin": "source",
                            "edited_by_user_id": seller.user_id,
                            "edited_at": "2026-10-04T00:00:00",
                            "edit_version": 2,
                        },
                    },
                }, ensure_ascii=False),
                original_data=json.dumps({
                    "title": "Synthetic empty manual-photo override",
                    "photo_urls": ["fixture-inherited-source-photo"],
                }),
            )
            db.session.add_all([known_photo, manual_empty_photo])
            db.session.flush()
            selected_listing = db.session.get(
                MarketplaceListing, fixture["listing_ids"][0],
            )
            selected_listing.imported_product_id = known_photo.id
            fixture.update({
                "workspace_wb_product_id": wb_product.id,
                "workspace_foreign_wb_product_id": foreign_wb_product.id,
                "workspace_known_photo_product_id": known_photo.id,
                "workspace_manual_empty_photo_product_id": manual_empty_photo.id,
                "workspace_wb_nm_id": wb_product.nm_id,
            })

        admin = User(
            username="ux01-synthetic-admin",
            email="ux01-synthetic-admin@example.test",
            is_active=True,
            is_admin=True,
        )
        admin.set_password("synthetic-admin-password")
        db.session.add(admin)
        db.session.commit()

        if VARIANT == "worktree":
            # Observe server-side SQL writes during the bounded legacy GETs;
            # the fixture seed above is intentionally outside this window.
            legacy_sql = {"active": False, "writes": []}

            def observe_legacy_sql_write(
                _connection, _cursor, statement, _parameters, _context,
                _executemany,
            ):
                if not legacy_sql["active"]:
                    return
                verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
                if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
                    legacy_sql["writes"].append({"verb": verb})

            event.listen(db.engine, "before_cursor_execute", observe_legacy_sql_write)
            fixture["legacy_sql_monitor"] = legacy_sql

    # The server itself binds only to loopback. No live seller credentials are
    # read: all account rows and keys above are synthetic.
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    assets = load_assets()
    fake_photo_png = None
    fake_wb_photo_urls = set()
    fake_wb_photo_hosts = set()
    if VARIANT == "worktree":
        from PIL import Image
        from seller_platform import wb_photo_url
        import services.image_lab_service as image_lab_service

        output = BytesIO()
        Image.new("RGB", (64, 64), color=(72, 122, 156)).save(output, format="PNG")
        fake_photo_png = output.getvalue()
        fake_photo_hash = hashlib.sha256(fake_photo_png).hexdigest()
        # Product detail/enrichment pages show the seller's linked WB gallery.
        # Fulfill only the exact synthetic nmID URLs generated by the real
        # production helper, with disposable fixture pixels (never the network).
        for photo_index in range(1, 11):
            for image_size in ("big", "c246x328", "tm"):
                fake_url = wb_photo_url(10_000_001, photo_index, image_size)
                fake_wb_photo_urls.add(fake_url)
                fake_wb_photo_hosts.add(urlsplit(fake_url).hostname)
                assets[fake_url] = {
                    "payload": fake_photo_png,
                    "content_type": "image/png",
                }

        fallback_product_id = fixture["workspace_manual_empty_photo_product_id"]
        original_linked_wb_photos = image_lab_service.exact_linked_wb_photo_urls

        def observe_linked_wb_photo_fallback(source):
            if getattr(source, "id", None) == fallback_product_id:
                REPORT["image_lab_wb_fallback_reads"].append({
                    "product_id": fallback_product_id,
                })
            return original_linked_wb_photos(source)

        known_photo_url = "https://r10-photo.fixture.invalid/known.png"
        assets[known_photo_url] = {
            "payload": fake_photo_png,
            "content_type": "image/png",
        }

        def synthetic_image_download(url, *, timeout=None):
            if url == known_photo_url:
                REPORT["image_lab_fake_reads"].append({
                    "transport": "synthetic_imported_photo",
                    "timeout": list(timeout) if isinstance(timeout, tuple) else timeout,
                    "fake_photo_sha256": fake_photo_hash,
                })
                return fake_photo_png
            if url in fake_wb_photo_urls:
                REPORT["image_lab_wb_fallback_downloads"] += 1
                return fake_photo_png
            raise AssertionError("Image Lab requested an unapproved fixture image")

        image_lab_service.exact_linked_wb_photo_urls = observe_linked_wb_photo_fallback
        image_lab_service.download_public_image = synthetic_image_download
    else:
        fake_photo_hash = None
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
        if (
            parsed.hostname in fake_wb_photo_hosts
            and request.url not in fake_wb_photo_urls
        ):
            asset = None
        if not asset:
            clean = request.url.split("?", 1)[0]
            asset = assets.get(clean)
            if (
                parsed.hostname in fake_wb_photo_hosts
                and request.url not in fake_wb_photo_urls
            ):
                asset = None
        if not asset and parsed.path in ("", "/"):
            asset = assets.get(f"{parsed.scheme}://{parsed.netloc}")
        if asset:
            if request.url in fake_wb_photo_urls:
                if request.method != "GET":
                    REPORT["unexpected_external_requests"].append({
                        "url": request.url,
                        "method": request.method,
                    })
                    route.abort()
                    return
                REPORT["synthetic_wb_photo_asset_reads"].append({
                    "url": request.url,
                    "method": request.method,
                    "status": 200,
                })
            if request.url == "https://ozon-fixture.test/product.svg":
                if request.method != "GET":
                    REPORT["unexpected_external_requests"].append({
                        "url": request.url,
                        "method": request.method,
                    })
                    route.abort()
                    return
                REPORT["fixture_asset_reads"].append({
                    "url": request.url,
                    "method": request.method,
                    "status": 200,
                })
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
        parsed = urlsplit(response.url)
        if parsed.hostname != "127.0.0.1" or parsed.port != server.server_port:
            return
        if (
            VARIANT == "worktree"
            and response.request.method == "GET"
            and parsed.path in {"/api/analytics/summary", "/api/finances/summary"}
        ):
            REPORT["wb_summary_route_reads"].append({
                "method": response.request.method,
                "path": parsed.path,
                "status": response.status,
            })
        if response.status < 400:
            return
        if (
            VARIANT == "worktree"
            and response.request.method == "GET"
            and response.status == 404
            and parsed.path == f"/products/{fixture['workspace_foreign_wb_product_id']}"
        ):
            REPORT["expected_http_denials"].append({
                "method": "GET",
                "path": parsed.path,
                "status": 404,
            })
            return
        REPORT["http_errors"].append({
            "path": parsed.path,
            "query": parsed.query,
            "status": response.status,
            "method": response.request.method,
            "resource_type": response.request.resource_type,
        })

    def record_console_error(message):
        if message.type != "error":
            return
        location = message.location or {}
        parsed_location = urlsplit(location.get("url", ""))
        denied_path = f"/products/{fixture['workspace_foreign_wb_product_id']}"
        exact_denial_response_seen = any(
            row == {"method": "GET", "path": denied_path, "status": 404}
            for row in REPORT["expected_http_denials"]
        )
        expected_not_found_console = (
            message.text
            == "Failed to load resource: the server responded with a status of 404 (NOT FOUND)"
        )
        if (
            VARIANT == "worktree"
            and parsed_location.scheme == "http"
            and parsed_location.netloc == f"127.0.0.1:{server.server_port}"
            and parsed_location.path == denied_path
            and not parsed_location.query
            and exact_denial_response_seen
            and expected_not_found_console
        ):
            REPORT["expected_denial_console_errors"].append({
                "method": "GET",
                "origin": f"http://127.0.0.1:{server.server_port}",
                "path": denied_path,
                "http_status": 404,
                "text": message.text[:180],
                "location_url": location.get("url", "")[:300],
            })
            return
        REPORT["console_errors"].append({
            "text": message.text[:300],
            "location": {
                "url": location.get("url", "")[:300],
                "lineNumber": location.get("lineNumber"),
                "columnNumber": location.get("columnNumber"),
            },
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

                # The following supplemental receipts exercise the old
                # seller-only destinations independently of the fixed
                # 37-page / 43-layout / 28-interaction matrices above.
                with app.app_context():
                    seller_row = db.session.get(Seller, fixture["seller_id"])
                    seller_row.wb_api_key = (
                        "synthetic-r10-read-only-not-a-provider-credential"
                    )
                    photo_source = db.session.get(
                        ImportedProduct, fixture["workspace_known_photo_product_id"],
                    )
                    photo_source.photo_urls = json.dumps([
                        "https://r10-photo.fixture.invalid/known.png",
                    ])
                    db.session.commit()
                    image_lab_experiments_before = ImageGenerationExperiment.query.filter_by(
                        seller_id=fixture["seller_id"],
                    ).count()
                    REPORT["image_lab_experiments_before"] = image_lab_experiments_before

                    def legacy_domain_counts():
                        return {
                            "products": Product.query.filter_by(
                                seller_id=fixture["seller_id"],
                            ).count(),
                            "card_edit_history": CardEditHistory.query.filter_by(
                                seller_id=fixture["seller_id"],
                            ).count(),
                            "imported_products": ImportedProduct.query.filter_by(
                                seller_id=fixture["seller_id"],
                            ).count(),
                            "marketplace_listings": MarketplaceListing.query.filter_by(
                                seller_id=fixture["seller_id"],
                            ).count(),
                            "image_generation_experiments": ImageGenerationExperiment.query.filter_by(
                                seller_id=fixture["seller_id"],
                            ).count(),
                        }

                    legacy_before = legacy_domain_counts()
                    fixture["legacy_sql_monitor"]["active"] = True

                def goto_extra(target_page, path: str, expected=200):
                    response = target_page.goto(
                        base + path, wait_until="domcontentloaded",
                    )
                    if not response or response.status != expected:
                        raise AssertionError((
                            "supplemental local GET failed", path,
                            response.status if response else None, expected,
                        ))
                    if expected == 200:
                        target_page.locator("#main-content").wait_for()
                    parsed = urlsplit(target_page.url)
                    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                    assert parsed.port == server.server_port
                    return response

                def fixture_origin(target_page):
                    parsed = urlsplit(target_page.url)
                    return f"{parsed.scheme}://{parsed.netloc}"

                extra_page = seller_context.new_page()
                extra_page.set_default_timeout(12000)
                extra_page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
                extra_page.on("console", record_console_error)
                extra_page.on("response", record_http_error)

                # Activate the legacy merge route through real sidebar tab
                # order and Enter, rather than assigning its href directly.
                merge_path = "/products/merge"
                goto_extra(extra_page, dashboard_path)
                toggle = extra_page.locator("#seller-workspace-toggle-products")
                toggle.focus()
                extra_page.keyboard.press("Enter")
                extra_page.wait_for_function(
                    "document.querySelector('#seller-workspace-toggle-products')?.getAttribute('aria-expanded') === 'true'"
                )
                merge_link = extra_page.locator(
                    '.seller-workspace-sublink[href="/products/merge"]',
                )
                assert merge_link.count() == 1
                for _ in range(16):
                    focused_href = extra_page.evaluate(
                        "document.activeElement?.getAttribute('href') || ''",
                    )
                    if focused_href == merge_path:
                        break
                    extra_page.keyboard.press("Tab")
                assert extra_page.evaluate(
                    "document.activeElement?.getAttribute('href')",
                ) == merge_path
                merge_label = merge_link.inner_text().strip()
                with extra_page.expect_navigation(
                    wait_until="domcontentloaded",
                ) as merge_navigation:
                    extra_page.keyboard.press("Enter")
                merge_response = merge_navigation.value
                assert merge_response and merge_response.status == 200
                assert merge_response.request.method == "GET"
                extra_page.wait_for_url(base + merge_path)
                merge_heading = extra_page.locator("#main-content h1").inner_text().strip()
                assert merge_heading == "Объединение карточек WB"
                REPORT["legacy_action_checks"].append({
                    "name": "legacy_sidebar_keyboard_activation_reaches_exact_routes",
                    "status": "passed",
                    "origin": fixture_origin(extra_page),
                    "method": merge_response.request.method,
                    "path": merge_path,
                    "http_status": merge_response.status,
                    "activation": "Tab+Enter",
                    "label": merge_label,
                    "page_heading": merge_heading,
                })

                # The command palette's own filter, active-row state and
                # Enter handler must reach both old destination types.
                palette_routes = []
                for label, expected_path, expected_heading in (
                    ("Документация", "/docs/", "Документация"),
                    ("Социальные подключения", "/content-factory/accounts", "Подключённые аккаунты"),
                ):
                    goto_extra(extra_page, dashboard_path)
                    extra_page.keyboard.press("Control+k")
                    extra_page.locator(".sh-cmdpal").wait_for(state="visible")
                    command_input = extra_page.locator(".sh-cmdpal-input")
                    command_input.fill(label)
                    active_item = extra_page.locator(".sh-cmdpal-item.active")
                    extra_page.wait_for_function(
                        "label => document.querySelector('.sh-cmdpal-item.active')?.innerText.includes(label)",
                        arg=label,
                    )
                    href = active_item.get_attribute("href")
                    assert urlsplit(href).path == expected_path, (label, href)
                    with extra_page.expect_navigation(
                        wait_until="domcontentloaded",
                    ) as palette_navigation:
                        extra_page.keyboard.press("Enter")
                    palette_response = palette_navigation.value
                    assert palette_response and palette_response.status == 200
                    assert palette_response.request.method == "GET"
                    extra_page.wait_for_url(base + expected_path)
                    heading = extra_page.locator("#main-content h1").inner_text().strip()
                    assert heading == expected_heading, (label, heading)
                    palette_routes.append({
                        "label": label,
                        "origin": fixture_origin(extra_page),
                        "method": palette_response.request.method,
                        "path": urlsplit(extra_page.url).path,
                        "http_status": palette_response.status,
                        "activation": "Ctrl+K+Enter",
                        "page_heading": heading,
                    })
                assert [row["path"] for row in palette_routes] == [
                    "/docs/", "/content-factory/accounts",
                ]
                REPORT["legacy_action_checks"].append({
                    "name": "command_palette_enter_reaches_help_and_social",
                    "status": "passed",
                    "routes": palette_routes,
                })

                # First select the exact account in a real marketplace URL;
                # the command palette listing destination must retain that
                # shell context for its next account-scoped Ozon navigation.
                goto_extra(extra_page, accounts_url)
                assert "Ozon CI 0" in extra_page.locator("body").inner_text()
                goto_extra(extra_page, dashboard_path)
                extra_page.keyboard.press("Control+k")
                extra_page.locator(".sh-cmdpal").wait_for(state="visible")
                account_input = extra_page.locator(".sh-cmdpal-input")
                account_input.fill("Карточки кабинетов")
                extra_page.wait_for_function(
                    "document.querySelector('.sh-cmdpal-item.active')?.innerText.includes('Карточки кабинетов')",
                )
                account_link = extra_page.locator(".sh-cmdpal-item.active")
                account_label_node = account_link.locator(".sh-cmdpal-item-label")
                assert account_label_node.count() == 1
                palette_account_label = account_label_node.inner_text().strip()
                palette_account_row_text = account_link.inner_text().strip()
                assert palette_account_label == "Карточки кабинетов", {
                    "expected_label": "Карточки кабинетов",
                    "actual_label": palette_account_label[:120],
                    "active_row_text": palette_account_row_text[:180],
                    "href_path": urlsplit(
                        account_link.get_attribute("href") or "",
                    ).path,
                }
                palette_account_href = account_link.get_attribute("href")
                assert urlsplit(palette_account_href).path == "/marketplaces/listings/"
                with extra_page.expect_navigation(
                    wait_until="domcontentloaded",
                ) as account_navigation:
                    extra_page.keyboard.press("Enter")
                account_response = account_navigation.value
                assert account_response and account_response.status == 200
                assert account_response.request.method == "GET"
                extra_page.wait_for_url(base + "/marketplaces/listings/")
                assert extra_page.locator("#main-content h1").inner_text().strip() == "Каталог маркетплейсов"
                assert "Ozon CI 0" in extra_page.locator("#main-content").inner_text()
                downstream_href = extra_page.locator(
                    '.seller-workspace-sublink[href^="/marketplaces/drafts/"]',
                ).get_attribute("href")
                downstream_query = dict(
                    __import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(
                        urlsplit(downstream_href).query,
                    )
                )
                assert urlsplit(downstream_href).path == "/marketplaces/drafts/"
                assert int(downstream_query.get("account_id", "0")) == int(fixture["account_id"])
                REPORT["legacy_action_checks"].append({
                    "name": "command_palette_account_link_preserves_selected_account",
                    "status": "passed",
                    "palette_label": palette_account_label,
                    "palette_href_path": urlsplit(palette_account_href).path,
                    "origin": fixture_origin(extra_page),
                    "method": account_response.request.method,
                    "path": urlsplit(extra_page.url).path,
                    "http_status": account_response.status,
                    "page_heading": "Каталог маркетплейсов",
                    "selected_account_id": int(fixture["account_id"]),
                    "rendered_account_label": "Ozon CI 0",
                    "downstream_account_href_path": urlsplit(downstream_href).path,
                    "downstream_account_query": {
                        "account_id": int(downstream_query["account_id"]),
                    },
                    "account_context_preserved": True,
                })

                # The WB tool group and account-scoped Ozon listing are
                # separate destinations. Obtain the listing URL from the
                # real seller journey so the exact account query is exercised.
                goto_extra(extra_page, merge_path)
                wb_heading = extra_page.locator(
                    ".seller-workspace-link-group-title",
                ).filter(has_text="Инструменты Wildberries").inner_text().strip()
                wb_merge_href = extra_page.locator(
                    '.seller-workspace-sublink[href="/products/merge"]',
                ).get_attribute("href")
                ozon_listing_href = extra_page.locator(
                    '.seller-workspace-sublink[href="/marketplaces/listings/"]',
                ).get_attribute("href")
                assert wb_heading == "Инструменты Wildberries"
                assert urlsplit(wb_merge_href).path == "/products/merge"
                assert urlsplit(ozon_listing_href).path == "/marketplaces/listings/"
                goto_extra(extra_page, f"/my-products?account_id={fixture['account_id']}")
                extra_page.locator(".preparation-journey-path summary").click()
                scoped_listing_link = extra_page.locator(
                    '.preparation-journey-step a[href^="/marketplaces/listings/"]',
                )
                assert scoped_listing_link.count() == 1
                scoped_listing_href = scoped_listing_link.get_attribute("href")
                scoped_query = parse_qs(urlsplit(scoped_listing_href).query)
                assert urlsplit(scoped_listing_href).path == "/marketplaces/listings/"
                assert scoped_query.get("account_id") == [str(fixture["account_id"])]
                assert scoped_query.get("marketplace") == ["ozon"]
                with extra_page.expect_navigation(wait_until="domcontentloaded") as scoped_navigation:
                    scoped_listing_link.click()
                scoped_listing_response = scoped_navigation.value
                assert scoped_listing_response and scoped_listing_response.status == 200
                extra_page.wait_for_url(base + scoped_listing_href)
                scoped_listing_heading = extra_page.locator("#main-content h1").inner_text().strip()
                assert scoped_listing_heading == "Каталог маркетплейсов"
                assert "Ozon CI 0" in extra_page.locator("#main-content").inner_text()
                scoped_listing_query = {
                    key: values[0] for key, values in parse_qs(
                        urlsplit(extra_page.url).query,
                    ).items() if values
                }
                assert int(scoped_listing_query.get("account_id", "0")) == int(fixture["account_id"])
                image_page_response = goto_extra(
                    extra_page,
                    f"{image_lab_url}?product_id={fixture['workspace_known_photo_product_id']}"
                    f"&listing_id={fixture['listing_ids'][0]}",
                )
                assert image_page_response.status == 200
                image_page_heading = extra_page.locator("#main-content h1").inner_text().strip()
                assert image_page_heading == "Фотостудия"
                REPORT["legacy_action_checks"].append({
                    "name": "wb_only_tool_labels_and_image_lab_source_are_distinct",
                    "status": "passed",
                    "origin": fixture_origin(extra_page),
                    "method": "GET",
                    "path": "/image-lab",
                    "http_status": image_page_response.status,
                    "wb_tool_heading": wb_heading,
                    "wb_merge_href_path": urlsplit(wb_merge_href).path,
                    "ozon_listings_href_path": urlsplit(ozon_listing_href).path,
                    "ozon_listings_query": {
                        "account_id": int(scoped_listing_query["account_id"]),
                    },
                    "ozon_listing_page_heading": scoped_listing_heading,
                    "image_lab_page_heading": image_page_heading,
                    "ozon_account_label": "Ozon CI 0",
                    "groups_distinct": True,
                })

                # Seller-scoped exact legacy routes: every visible action is
                # activated from the real detail page; no form is submitted.
                product_id = int(fixture["workspace_wb_product_id"])
                product_title = "Синтетическая карточка WB для переходов"
                action_specs = (
                    ("История", f"/products/{product_id}/history", "История изменений карточки"),
                    ("Обогатить", f"/products/{product_id}/enrich", "Обогащение от поставщика"),
                    ("Редактировать", f"/products/{product_id}/edit", "Редактирование карточки"),
                )
                product_actions = []
                for label, path, expected_heading in action_specs:
                    action_page = seller_context.new_page()
                    action_page.set_default_timeout(12000)
                    action_page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
                    action_page.on("console", record_console_error)
                    action_page.on("response", record_http_error)
                    goto_extra(action_page, f"/products/{product_id}")
                    link = action_page.locator(f'a[href="{path}"]')
                    assert link.count() == 1 and link.inner_text().strip() == label
                    with action_page.expect_navigation(
                        wait_until="domcontentloaded",
                    ) as action_navigation:
                        link.click()
                    action_response = action_navigation.value
                    assert action_response and action_response.status == 200
                    assert action_response.request.method == "GET"
                    action_page.wait_for_url(base + path)
                    heading = action_page.locator("#main-content h1").inner_text().strip()
                    if path.endswith("/edit"):
                        rendered_product_title = action_page.locator("#title").input_value()
                        title_matches = rendered_product_title == product_title
                        title_evidence = {
                            "kind": "input_value",
                            "value": rendered_product_title[:180],
                        }
                    else:
                        rendered_product_title = action_page.locator("#main-content").inner_text()
                        title_matches = product_title in rendered_product_title
                        title_evidence = {
                            "kind": "main_content_text",
                            "matched": title_matches,
                        }
                    assert heading == expected_heading, (path, heading)
                    assert title_matches, {
                        "path": path,
                        "expected_title": product_title,
                        "title_evidence": title_evidence,
                    }
                    product_actions.append({
                        "label": label,
                        "origin": fixture_origin(action_page),
                        "method": action_response.request.method,
                        "path": urlsplit(action_page.url).path,
                        "http_status": action_response.status,
                        "page_heading": heading,
                        "title_matches": title_matches,
                        "title_evidence": title_evidence,
                    })
                    action_page.close()

                # A foreign seller's exact product route must remain denied.
                foreign_path = f"/products/{fixture['workspace_foreign_wb_product_id']}"
                foreign_response = goto_extra(extra_page, foreign_path, expected=404)
                assert foreign_response.status == 404
                assert REPORT["expected_http_denials"] == [{
                    "method": "GET", "path": foreign_path, "status": 404,
                }]
                REPORT["legacy_action_checks"].append({
                    "name": "legacy_product_actions_open_exact_product_routes",
                    "status": "passed",
                    "origin": fixture_origin(extra_page),
                    "method": "GET",
                    "product_id": product_id,
                    "actions": product_actions,
                    "foreign_product_id": int(fixture["workspace_foreign_wb_product_id"]),
                    "foreign_scope_denial": {
                        "method": "GET",
                        "path": foreign_path,
                        "http_status": foreign_response.status,
                    },
                })
                assert len(REPORT["expected_denial_console_errors"]) == 1, (
                    REPORT["expected_denial_console_errors"],
                )
                assert not REPORT["console_errors"], REPORT["console_errors"]

                # The one valid imported photo is fetched by the real route
                # and returned from fake transport bytes. The explicitly
                # empty manual override remains absent despite a linked WB
                # photo; no create/generate API is invoked.
                known_photo_id = int(fixture["workspace_known_photo_product_id"])
                source_listing_id = int(fixture["listing_ids"][0])
                fallback_product_id = int(fixture["workspace_manual_empty_photo_product_id"])
                REPORT["image_lab_fake_reads"].clear()
                REPORT["image_lab_wb_fallback_reads"].clear()
                REPORT["image_lab_wb_fallback_downloads"] = 0
                image_requests = []
                image_responses = []

                def track_image_lab_source_request(request):
                    parsed = urlsplit(request.url)
                    if (
                        request.method == "GET"
                        and parsed.hostname == "127.0.0.1"
                        and parsed.port == server.server_port
                        and parsed.path == f"/image-lab/api/products/{known_photo_id}/original"
                    ):
                        image_requests.append({
                            "method": request.method,
                            "path": parsed.path,
                        })

                def track_image_lab_source_response(response):
                    parsed = urlsplit(response.url)
                    if parsed.path == f"/image-lab/api/products/{known_photo_id}/original":
                        image_responses.append({
                            "method": response.request.method,
                            "path": parsed.path,
                            "status": response.status,
                            "content_type": response.headers.get("content-type", "").split(";", 1)[0],
                        })

                extra_page.on("request", track_image_lab_source_request)
                extra_page.on("response", track_image_lab_source_response)
                source_path = (
                    f"/image-lab?product_id={known_photo_id}&listing_id={source_listing_id}"
                )
                goto_extra(extra_page, source_path)
                photo_image = extra_page.locator(".lab-preview img")
                photo_image.wait_for(state="visible")
                extra_page.wait_for_function(
                    "img => img.complete && img.naturalWidth === 64 && img.naturalHeight === 64",
                    arg=photo_image.element_handle(),
                )
                extra_page.locator("details summary", has_text="Что попадёт в контекст").click()
                context_payload = extra_page.locator("details pre").inner_text()
                assert '"source": "imported_product"' in context_payload
                assert "Synthetic imported photo source" in context_payload
                lab_state = extra_page.evaluate("""() => {
                    const root = document.querySelector('[x-data="imageLab()"]');
                    const state = Alpine.$data(root);
                    return {
                        productId: Number(state.currentProduct?.id),
                        title: state.currentProduct?.title,
                        source: state.currentProduct?.visual_context?.source,
                        target: state.currentMarketplaceTarget ? {
                            listing_id: Number(state.currentMarketplaceTarget.listing_id),
                            account_id: Number(state.currentMarketplaceTarget.account_id),
                            account_label: state.currentMarketplaceTarget.account_label,
                        } : null,
                    };
                }""")
                assert lab_state == {
                    "productId": known_photo_id,
                    "title": "Synthetic imported photo source",
                    "source": "imported_product",
                    "target": {
                        "listing_id": source_listing_id,
                        "account_id": int(fixture["account_id"]),
                        "account_label": "Ozon CI 0",
                    },
                }, lab_state
                assert len(image_requests) == 1, image_requests
                assert image_responses == [{
                    "method": "GET",
                    "path": f"/image-lab/api/products/{known_photo_id}/original",
                    "status": 200,
                    "content_type": "image/png",
                }], image_responses
                assert len(REPORT["image_lab_fake_reads"]) == 1
                assert not REPORT["image_lab_wb_fallback_reads"]
                assert REPORT["image_lab_wb_fallback_downloads"] == 0
                REPORT["legacy_action_checks"].append({
                    "name": "image_lab_fixture_photo_loads_with_imported_source_context",
                    "status": "passed",
                    "origin": fixture_origin(extra_page),
                    "method": "GET",
                    "path": urlsplit(extra_page.url).path,
                    "http_status": 200,
                    "page_heading": image_page_heading,
                    "source_product_id": known_photo_id,
                    "source_type": lab_state["source"],
                    "source_title": lab_state["title"],
                    "listing_id": source_listing_id,
                    "listing_account_id": lab_state["target"]["account_id"],
                    "fake_transport_read_count": len(REPORT["image_lab_fake_reads"]),
                    "fake_photo_sha256": fake_photo_hash,
                    "image_natural_width": photo_image.evaluate("node => node.naturalWidth"),
                    "image_natural_height": photo_image.evaluate("node => node.naturalHeight"),
                    "original_get": image_responses[0],
                })

                empty_override = extra_page.evaluate("""id => {
                    const product = window.IMAGE_LAB_BOOTSTRAP.products.find(
                        row => Number(row.id) === Number(id));
                    return product ? {id:Number(product.id), title:product.title} : null;
                }""", fallback_product_id)
                assert empty_override is None
                with app.app_context():
                    empty_product = db.session.get(ImportedProduct, fallback_product_id)
                    overrides_json = json.loads(empty_product.content_overrides_json or "{}")
                    photos_override = overrides_json.get("fields", {}).get("photos", {})
                    override_photos = photos_override.get("value")
                    effective_photos = json.loads(empty_product.photo_urls or "[]")
                    inherited_source = json.loads(empty_product.original_data or "{}")
                    inherited_photos = inherited_source.get("photo_urls", [])
                    override_schema_version = overrides_json.get("schema_version")
                    override_photo_count = len(override_photos) if isinstance(override_photos, list) else None
                    effective_photo_count = len(effective_photos) if isinstance(effective_photos, list) else None
                    inherited_source_photo_count = len(inherited_photos) if isinstance(inherited_photos, list) else None
                    content_edit_version = int(empty_product.content_edit_version)
                    assert photos_override.get("value") == []
                    assert override_schema_version == 1
                    assert content_edit_version == 2
                    assert override_photo_count == 0
                    assert effective_photo_count == 0
                    assert inherited_source_photo_count == 1
                    assert effective_photos == []
                    empty_experiments_after = ImageGenerationExperiment.query.filter_by(
                        seller_id=fixture["seller_id"],
                    ).count()
                    REPORT["image_lab_experiments_after"] = empty_experiments_after
                assert empty_experiments_after == image_lab_experiments_before
                assert not REPORT["image_lab_wb_fallback_reads"]
                assert REPORT["image_lab_wb_fallback_downloads"] == 0
                REPORT["legacy_action_checks"].append({
                    "name": "image_lab_empty_manual_override_suppresses_wb_fallback",
                    "status": "passed",
                    "source_product_id": fallback_product_id,
                    "explicit_empty_override": True,
                    "excluded_from_lab": empty_override is None,
                    "override_schema_version": override_schema_version,
                    "content_edit_version": content_edit_version,
                    "override_photo_count": override_photo_count,
                    "effective_photo_count": effective_photo_count,
                    "inherited_source_photo_count": inherited_source_photo_count,
                    "wb_linked_product_id": int(fixture["workspace_wb_product_id"]),
                    "wb_photo_fallback_reads": len(REPORT["image_lab_wb_fallback_reads"]),
                    "wb_photo_fallback_downloads": REPORT["image_lab_wb_fallback_downloads"],
                    "experiments_before": image_lab_experiments_before,
                    "experiments_after": empty_experiments_after,
                })

                with app.app_context():
                    legacy_after = legacy_domain_counts()
                fixture["legacy_sql_monitor"]["active"] = False
                REPORT["legacy_domain_sql_writes"] = fixture["legacy_sql_monitor"]["writes"]
                REPORT["legacy_domain_state"] = {
                    "before": legacy_before,
                    "after": legacy_after,
                    "unchanged": legacy_before == legacy_after,
                }
                REPORT["legacy_post_count"] = len(REPORT["browser_mutations"])
                assert REPORT["legacy_domain_sql_writes"] == []
                assert REPORT["legacy_domain_state"]["unchanged"] is True
                assert REPORT["legacy_post_count"] == 0
                assert REPORT["expected_http_denials"] == [{
                    "method": "GET",
                    "path": f"/products/{fixture['workspace_foreign_wb_product_id']}",
                    "status": 404,
                }]
                expected_legacy_names = {
                    "legacy_sidebar_keyboard_activation_reaches_exact_routes",
                    "command_palette_enter_reaches_help_and_social",
                    "legacy_product_actions_open_exact_product_routes",
                    "command_palette_account_link_preserves_selected_account",
                    "wb_only_tool_labels_and_image_lab_source_are_distinct",
                    "image_lab_fixture_photo_loads_with_imported_source_context",
                    "image_lab_empty_manual_override_suppresses_wb_fallback",
                }
                assert {row["name"] for row in REPORT["legacy_action_checks"]} == expected_legacy_names
                assert all(row.get("status") == "passed" for row in REPORT["legacy_action_checks"])
                extra_page.close()

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
            if VARIANT == "worktree":
                assert REPORT["fixture_asset_reads"], "expected exact synthetic product.svg fixture read"
                assert all(
                    row == {
                        "url": "https://ozon-fixture.test/product.svg",
                        "method": "GET",
                        "status": 200,
                    }
                    for row in REPORT["fixture_asset_reads"]
                ), REPORT["fixture_asset_reads"]
                REPORT["fixture_asset_read_count"] = len(REPORT["fixture_asset_reads"])
                expected_edit_photo_url = wb_photo_url(10_000_001, 1, "tm")
                assert expected_edit_photo_url in fake_wb_photo_urls
                assert REPORT["synthetic_wb_photo_asset_reads"], "expected exact synthetic WB photo read"
                assert all(
                    row["url"] in fake_wb_photo_urls
                    and row["method"] == "GET"
                    and row["status"] == 200
                    for row in REPORT["synthetic_wb_photo_asset_reads"]
                ), REPORT["synthetic_wb_photo_asset_reads"]
                assert expected_edit_photo_url in {
                    row["url"] for row in REPORT["synthetic_wb_photo_asset_reads"]
                }, REPORT["synthetic_wb_photo_asset_reads"]
                REPORT["synthetic_wb_photo_asset_read_count"] = len(
                    REPORT["synthetic_wb_photo_asset_reads"],
                )
                assert len(REPORT["wb_summary_cache_seeded_observations"]["analytics"]) == 4
                assert len(REPORT["wb_summary_cache_seeded_observations"]["finance"]) == 4
                assert REPORT["wb_summary_route_reads"]
                assert all(
                    row["status"] == 200 for row in REPORT["wb_summary_route_reads"]
                ), REPORT["wb_summary_route_reads"]
                assert {
                    row["path"] for row in REPORT["wb_summary_route_reads"]
                } == {"/api/analytics/summary", "/api/finances/summary"}
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
        # The `sync_playwright()` context has already stopped its driver before
        # this outer cleanup runs and owns closing any still-open browser.
        # Calling browser.close() here uses that stopped driver and raises the
        # recurring Playwright `Error` seen in earlier failed reports.
        try:
            server.shutdown()
        except Exception as exc:
            REPORT["status"] = "failed"
            REPORT.setdefault("cleanup_errors", []).append({
                "step": "server.shutdown",
                "type": type(exc).__name__,
                "message": str(exc)[:300],
            })
        try:
            server.server_close()
        except Exception as exc:
            REPORT["status"] = "failed"
            REPORT.setdefault("cleanup_errors", []).append({
                "step": "server.server_close",
                "type": type(exc).__name__,
                "message": str(exc)[:300],
            })
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
