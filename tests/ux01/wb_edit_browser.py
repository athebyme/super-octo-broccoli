"""Offline Playwright journey for the exact WB product edit flow.

Run with the repository virtualenv:
  python tests/ux01/wb_edit_browser.py

The fixture uses a disposable SQLite database, one synthetic WB account and a
fake provider boundary. Browser traffic is limited to loopback and the pinned
asset manifest. A sandbox that denies loopback listener creation writes a
machine-readable ``status=blocked`` report; it never claims browser success.
"""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import requests
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="ux01-wb-edit-")
TEMP_PATH = Path(TEMP.name)
REPORT_PATH = Path(
    os.environ.get("UX01_WB_EDIT_REPORT")
    or os.environ.get("WB_EDIT_BROWSER_REPORT")
    or "/tmp/wb-edit-browser-report.json"
)
ARTIFACTS_PATH = Path(
    os.environ.get("UX01_WB_EDIT_ARTIFACTS")
    or REPORT_PATH.parent / "wb-edit-browser-artifacts"
)
SOURCE = os.environ.get("UX01_WB_EDIT_SOURCE", "worktree")
ASSET_DIR = ROOT / "tests/ozon_release/assets"
USERNAME = "ux01-wb-edit-seller"
PASSWORD = "synthetic-wb-edit-password"
FIXTURE = {}
SERVER = None
SERVER_THREAD = None
BASE = None

REPORT = {
    "source": SOURCE,
    "status": "running",
    "scope": "synthetic_wb_exact_selection_review_and_edit",
    "database": "disposable_sqlite",
    "network_policy": "loopback_and_hash_pinned_assets_only",
    "layouts": [],
    "overflow_diagnostics": [],
    "checks": [],
    "interactions": [],
    "artifacts": [],
    "artifact_errors": [],
    "failure_context": None,
    "javascript_errors": [],
    "browser_mutations": [],
    "unexpected_external_requests": [],
    "unexpected_http": [],
    "expected_negative_http": [],
    "provider_attempts": 0,
    "fake_wb_write_calls": 0,
    "fake_wb_written_products": [],
    "fake_wb_client_instances": 0,
    "blocked_reason": None,
}
EXPECTED_NEGATIVE_HTTP = {
    ("POST", "/products/selection/resolve"): {
        "status": 403,
        "name": "foreign_product_selection_rejected",
    },
    ("GET", "/api/characteristics/Unmapped%20synthetic%20category"): {
        "status": 409,
        "name": "unmapped_category_rejected",
    },
}
OBSERVED_EXPECTED_NEGATIVE_HTTP = set()
EMPTY_PRODUCT_FORM_FILTERS = frozenset({
    "category", "has_stock", "block_status", "rating_min", "rating_max",
})

os.environ.update({
    "DATABASE_URL": "sqlite:///" + str(TEMP_PATH / "wb-edit.sqlite"),
    "SKIP_SCHEDULER": "1",
    "SECRET_KEY": "synthetic-wb-edit-browser-secret",
    "ENCRYPTION_KEY": base64.urlsafe_b64encode(b"w" * 32).decode(),
    "MARKETPLACE_OZON_ENABLED": "0",
    "MARKETPLACE_OZON_PUBLICATION_ENABLED": "0",
    "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED": "0",
})


def forbidden_python_network(*_args, **_kwargs):
    REPORT["provider_attempts"] += 1
    raise AssertionError("Python provider/network traffic is forbidden in this fixture")


requests.sessions.Session.request = forbidden_python_network
socket.create_connection = forbidden_python_network


def write_report() -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_pinned_assets() -> dict[str, dict[str, object]]:
    manifest = json.loads((ASSET_DIR / "manifest.json").read_text(encoding="utf-8"))
    assets = {}
    for url, row in manifest.items():
        payload = (ASSET_DIR / row["file"]).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != row["sha256"]:
            raise AssertionError(f"Pinned browser asset changed: {url}")
        assets[url] = {"payload": payload, "content_type": row["content_type"]}

    vue_url = "https://cdn.jsdelivr.net/npm/vue@3.4.38/dist/vue.global.prod.js"
    vue_payload = (ROOT / "static/vendor/vue-3.4.38.global.prod.js").read_bytes()
    assets[vue_url] = {
        "payload": vue_payload,
        "content_type": "application/javascript",
    }
    return assets


def seed_synthetic_wb(app) -> dict[str, int]:
    from models import (
        Marketplace,
        MarketplaceCategory,
        MarketplaceCategoryCharacteristic,
        MarketplaceDirectory,
        Product,
        Seller,
        User,
        db,
    )

    with app.app_context():
        # Use one observed UTC instant for every cached reference row. This
        # keeps the fixture fresh at seed time without a hard-coded expiry.
        fixture_now = datetime.utcnow()
        db.create_all()
        owner = User(
            username=USERNAME,
            email="ux01-wb-edit@example.test",
            is_active=True,
        )
        owner.set_password(PASSWORD)
        foreign_user = User(
            username="ux01-wb-edit-foreign",
            email="ux01-wb-edit-foreign@example.test",
            is_active=True,
        )
        foreign_user.set_password(PASSWORD)
        db.session.add_all([owner, foreign_user])
        db.session.flush()

        seller = Seller(
            user_id=owner.id,
            company_name="WB edit synthetic shop",
            wb_seller_id="wb-edit-account-synthetic",
        )
        seller.wb_api_key = "synthetic-provider-key-never-sent"
        foreign_seller = Seller(
            user_id=foreign_user.id,
            company_name="Foreign synthetic shop",
            wb_seller_id="foreign-wb-account-synthetic",
        )
        db.session.add_all([seller, foreign_seller])
        db.session.flush()

        marketplace = Marketplace(
            name="Wildberries",
            code="wb",
            adapter_code="wb",
            is_active=True,
            categories_sync_status="success",
            categories_synced_at=fixture_now,
        )
        db.session.add(marketplace)
        db.session.flush()
        category = MarketplaceCategory(
            marketplace_id=marketplace.id,
            subject_id=5880,
            subject_name="Свечи эротик",
            is_enabled=True,
            is_leaf=True,
            is_available=True,
            characteristics_synced_at=fixture_now,
            characteristics_sync_status="success",
            characteristics_schema_hash="a" * 64,
            characteristics_version=1,
            characteristics_count=32,
        )
        db.session.add(category)
        db.session.flush()

        male_category = MarketplaceCategory(
            marketplace_id=marketplace.id,
            subject_id=5070,
            subject_name="Мастурбаторы мужские",
            is_enabled=True,
            is_leaf=True,
            is_available=True,
            characteristics_synced_at=fixture_now,
            characteristics_sync_status="success",
            characteristics_schema_hash="b" * 64,
            characteristics_version=1,
            characteristics_count=31,
        )
        db.session.add(male_category)
        db.session.flush()

        definitions = [
            (101, "Synthetic free-text field", 1, 1, None, "[]"),
            (202, "Страна производства", 1, 1, None, "[]"),
            (303, "Вес товара", 4, 1, "г", "[]"),
            (404, "Материал", 1, 3, None, '["Силикон", "Пластик", "Металл"]'),
        ]
        definitions.extend(
            (500 + index, f"Synthetic field {index + 1}", 1, 1, None, "[]")
            for index in range(28)
        )
        db.session.add_all([
            MarketplaceCategoryCharacteristic(
                category_id=category.id,
                marketplace_id=marketplace.id,
                charc_id=char_id,
                name=name,
                charc_type=char_type,
                required=False,
                unit_name=unit,
                max_count=max_count,
                dictionary_json=dictionary,
                dictionary_source="none",
                is_enabled=True,
                is_available=True,
            )
            for char_id, name, char_type, max_count, unit, dictionary in definitions
        ])
        db.session.add_all([
            MarketplaceCategoryCharacteristic(
                category_id=male_category.id,
                marketplace_id=marketplace.id,
                charc_id=800 + index,
                name=f"Synthetic male subject field {index + 1}",
                charc_type=1,
                required=False,
                unit_name=None,
                max_count=1,
                dictionary_json="[]",
                dictionary_source="none",
                is_enabled=True,
                is_available=True,
            )
            for index in range(31)
        ])
        db.session.add(MarketplaceDirectory(
            marketplace_id=marketplace.id,
            directory_type="countries",
            data_json='["Россия", "Китай"]',
            synced_at=fixture_now,
            sync_status="success",
            items_count=2,
            version=1,
        ))

        products = []
        for index in range(51):
            local_id = 9741 + index
            products.append(Product(
                id=local_id,
                seller_id=seller.id,
                nm_id=900000 + index,
                vendor_code=f"PD-{index:03d}",
                title=f"Pipedream Synthetic Product {index:03d}",
                brand="Pipedream",
                object_name="Свечи эротик",
                subject_id=5880,
                characteristics_json=(
                    '[{"id":101,"name":"Synthetic free-text field",'
                    '"value":["Existing synthetic value"]}]'
                    if index == 0 else "[]"
                ),
                sizes_json=json.dumps([{
                    "techSize": "ONE SIZE",
                    "skus": [f"SYNTHETIC-WB-SKU-{index:03d}"],
                    "chrtID": 700000 + index,
                }]),
                photos_json="[]",
                is_active=True,
            ))
        foreign = Product(
            id=10001,
            seller_id=foreign_seller.id,
            nm_id=990001,
            vendor_code="FOREIGN-SECRET-CODE",
            title="Foreign private Pipedream title",
            brand="Pipedream",
            object_name="Свечи эротик",
            subject_id=5880,
            characteristics_json="[]",
            sizes_json="[]",
            photos_json="[]",
            is_active=True,
        )
        unmapped = Product(
            id=10002,
            seller_id=seller.id,
            nm_id=990002,
            vendor_code="UNMAPPED-FIXTURE",
            title="Synthetic unmapped category",
            brand="Other fixture brand",
            object_name="Unmapped synthetic category",
            subject_id=None,
            characteristics_json=(
                '[{"id":101,"name":"Stale prior-subject field",'
                '"value":["Preserve read-only historical value"]}]'
            ),
            sizes_json="[]",
            photos_json="[]",
            is_active=True,
        )
        male_subject_product = Product(
            id=10003,
            seller_id=seller.id,
            nm_id=990003,
            vendor_code="MALE-SUBJECT-FIXTURE",
            title="Synthetic male subject card",
            brand="Other fixture brand",
            object_name="Мастурбаторы мужские",
            subject_id=5070,
            characteristics_json="[]",
            sizes_json="[]",
            photos_json="[]",
            is_active=True,
        )
        db.session.add_all([*products, foreign, unmapped, male_subject_product])
        db.session.commit()
        return {
            "seller_id": int(seller.id),
            "foreign_product_id": int(foreign.id),
            "product_id": int(products[0].id),
            "product_count": len(products),
            "male_subject_product_id": int(male_subject_product.id),
            "unmapped_product_id": int(unmapped.id),
        }


class FakeWBClient:
    """Provider boundary fake; records simulated writes without HTTP."""

    def __init__(self, *_args, **_kwargs):
        REPORT["fake_wb_client_instances"] += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def update_cards_merged(self, updates, *, pre_merge_callback=None, **_kwargs):
        REPORT["fake_wb_write_calls"] += 1
        sent = []
        snapshots = {}
        for nm_id, row in updates.items():
            before = {
                "nmID": int(nm_id),
                "subjectID": 5880,
                "brand": "Pipedream",
                "title": f"Pipedream product {nm_id}",
                "vendorCode": f"PD-{int(nm_id) - 900000:03d}",
                "description": "Synthetic description",
                "characteristics": [],
                "sizes": [],
            }
            if pre_merge_callback:
                pre_merge_callback(int(nm_id), before, row)
            after = dict(before)
            after.update(row)
            snapshots[int(nm_id)] = {"before": before, "after": after}
            sent.append(int(nm_id))
        REPORT["fake_wb_written_products"].extend(sent)
        return {"sent": sent, "missing": [], "invalid": {}, "failed": {}, "snapshots": snapshots}


def local_post_allowlist(fixture: dict[str, int]) -> frozenset[str]:
    unmapped_product_id = fixture.get("unmapped_product_id")
    if type(unmapped_product_id) is not int or unmapped_product_id <= 0:
        raise ValueError("browser fixture needs one exact positive unmapped product ID")
    return frozenset({
        "/login",
        "/products/selection/resolve",
        "/products/bulk-edit",
        f"/products/{unmapped_product_id}/edit",
    })


def pinned_asset_for_url(url: str, assets: dict[str, dict[str, object]]):
    asset = assets.get(url)
    if asset is not None:
        return asset
    # Chromium resolves the existing pinned <script src="https://cdn.tailwindcss.com">
    # to the slash-terminated origin URL. Alias only that exact root URL.
    if url == "https://cdn.tailwindcss.com/":
        return assets.get("https://cdn.tailwindcss.com")
    return None


def bridge(
    route,
    *,
    assets: dict[str, dict[str, object]],
    origin: str,
    allowed_local_posts: frozenset[str],
) -> None:
    request = route.request
    parsed = urlsplit(request.url)
    if request.url.startswith(origin + "/"):
        if request.method not in {"GET", "HEAD", "POST"}:
            REPORT["unexpected_external_requests"].append({
                "method": request.method,
                "path": parsed.path,
                "reason": "unsupported_method",
            })
            route.abort()
            return
        if request.method == "POST" and parsed.path not in allowed_local_posts:
            REPORT["unexpected_external_requests"].append({
                "method": request.method,
                "path": parsed.path,
                "reason": "unapproved_local_mutation",
            })
            route.abort()
            return
        route.continue_()
        return
    asset = pinned_asset_for_url(request.url, assets)
    if asset:
        route.fulfill(
            status=200,
            body=asset["payload"],
            content_type=asset["content_type"],
        )
        return
    REPORT["unexpected_external_requests"].append({
        "method": request.method,
        "host": parsed.hostname,
        "path": parsed.path,
        "reason": "not_hash_pinned",
    })
    route.abort()


def record_http_response(response) -> None:
    parsed = urlsplit(response.url)
    if not BASE or f"{parsed.scheme}://{parsed.netloc}" != BASE:
        return
    if response.status < 400:
        return
    request_key = (response.request.method, parsed.path)
    expected = EXPECTED_NEGATIVE_HTTP.get(request_key)
    if expected and response.status == expected["status"]:
        if request_key in OBSERVED_EXPECTED_NEGATIVE_HTTP:
            REPORT["unexpected_http"].append({
                "status": response.status,
                "method": request_key[0],
                "path": request_key[1],
                "reason": "duplicate_expected_negative_probe",
            })
        else:
            OBSERVED_EXPECTED_NEGATIVE_HTTP.add(request_key)
            REPORT["expected_negative_http"].append({
                "name": expected["name"],
                "method": request_key[0],
                "path": request_key[1],
                "status": response.status,
            })
            check(expected["name"], method=request_key[0], status=response.status)
    else:
        REPORT["unexpected_http"].append({
            "status": response.status,
            "method": response.request.method,
            "path": parsed.path,
        })


def check(name: str, **details) -> None:
    REPORT["checks"].append({"name": name, "status": "passed", **details})


def interaction(name: str, **details) -> None:
    REPORT["interactions"].append({"name": name, **details})


def wait_for_layout_settle(page, *, width: int, theme: str) -> dict:
    result = page.evaluate("""async ({expectedWidth, theme, timeoutMs, stableFrames}) => {
        const started = performance.now();
        const boundedTimeout = Math.min(5000, Math.max(1, Number(timeoutMs) || 5000));
        const deadline = started + boundedTimeout;
        const tolerance = 0.75;
        const rounded = value => Math.round(value * 100) / 100;
        const fonts = document.fonts || null;
        let fontsSettled = !fonts;
        let fontsRejected = false;
        if (fonts) {
            Promise.resolve(fonts.ready).then(
                () => { fontsSettled = true; },
                () => { fontsSettled = true; fontsRejected = true; },
            );
        }
        const mainContent = document.querySelector('.main-content');
        const main = document.querySelector('#main-content');
        const sidebar = document.querySelector('.sidebar');
        if (!mainContent || !main || !sidebar) {
            throw new Error('responsive layout anchors are missing');
        }
        const relevantTransitionRunning = (element, properties) => {
            if (typeof element.getAnimations !== 'function') return false;
            return element.getAnimations({subtree: false}).some(animation =>
                animation.playState === 'running'
                && properties.includes(animation.transitionProperty)
            );
        };
        const measure = () => {
            const contentRect = mainContent.getBoundingClientRect();
            const mainRect = main.getBoundingClientRect();
            const sidebarRect = sidebar.getBoundingClientRect();
            return {
                viewportWidth: window.innerWidth,
                contentLeft: rounded(contentRect.left),
                contentWidth: rounded(contentRect.width),
                contentMarginLeft: rounded(parseFloat(getComputedStyle(mainContent).marginLeft) || 0),
                mainLeft: rounded(mainRect.left),
                mainWidth: rounded(mainRect.width),
                activeTheme: document.documentElement.getAttribute('data-theme') || '',
                sidebarLeft: rounded(sidebarRect.left),
                sidebarRight: rounded(sidebarRect.right),
                sidebarWidth: rounded(sidebarRect.width),
                documentWidth: document.documentElement.scrollWidth,
                bodyWidth: document.body.scrollWidth,
                fontStatus: fonts ? fonts.status : 'unsupported',
                fontsSettled,
                fontsRejected,
                mainMarginTransition: relevantTransitionRunning(mainContent, ['margin-left']),
                sidebarTransition: relevantTransitionRunning(sidebar, ['width', 'transform']),
            };
        };
        const stableAcrossFrames = (before, after) => {
            if (!before) return false;
            const numeric = [
                'viewportWidth', 'contentLeft', 'contentWidth', 'contentMarginLeft',
                'mainLeft', 'mainWidth', 'sidebarLeft', 'sidebarRight', 'sidebarWidth',
                'documentWidth', 'bodyWidth',
            ];
            return numeric.every(key => Math.abs(before[key] - after[key]) <= tolerance)
                && before.fontStatus === after.fontStatus
                && before.fontsSettled === after.fontsSettled
                && before.activeTheme === after.activeTheme;
        };
        const targetGeometryMatches = current => {
            if (current.viewportWidth !== expectedWidth) return false;
            if (expectedWidth < 1024) {
                return Math.abs(current.contentLeft) <= tolerance
                    && Math.abs(current.contentWidth - expectedWidth) <= tolerance
                    && Math.abs(current.contentMarginLeft) <= tolerance;
            }
            const availableWidth = expectedWidth - current.sidebarRight;
            return Math.abs(current.sidebarLeft) <= tolerance
                && Math.abs(current.contentLeft - current.sidebarRight) <= tolerance
                && Math.abs(current.contentMarginLeft - current.sidebarRight) <= tolerance
                && Math.abs(current.contentWidth - availableWidth) <= tolerance;
        };
        let previous = null;
        let stableCount = 0;
        let latest = null;
        while (performance.now() < deadline) {
            const remaining = Math.max(1, deadline - performance.now());
            let timer = null;
            const frameArrived = await Promise.race([
                new Promise(resolve => requestAnimationFrame(() => resolve(true))),
                new Promise(resolve => { timer = setTimeout(() => resolve(false), remaining); }),
            ]);
            if (timer !== null) clearTimeout(timer);
            if (!frameArrived) break;
            latest = measure();
            const transitionsRunning = latest.mainMarginTransition || latest.sidebarTransition;
            const fontsReady = latest.fontsSettled && latest.fontStatus === 'loaded';
            const themeReady = latest.activeTheme === theme;
            const geometryMatches = targetGeometryMatches(latest);
            if (
                !transitionsRunning && fontsReady && themeReady && geometryMatches
                && stableAcrossFrames(previous, latest)
            ) {
                stableCount += 1;
                if (stableCount >= stableFrames) {
                    return {
                        theme,
                        elapsed_ms: rounded(performance.now() - started),
                        stable_frames: stableCount,
                        fonts_ready: fontsReady,
                        theme_ready: themeReady,
                        transitions_running: false,
                        viewport_width: latest.viewportWidth,
                        main_content_left: latest.contentLeft,
                        main_content_width: latest.contentWidth,
                        sidebar_right: latest.sidebarRight,
                    };
                }
            } else {
                stableCount = 0;
            }
            previous = latest;
        }
        throw new Error(JSON.stringify({
            reason: 'layout_not_stable_before_deadline',
            expected_width: expectedWidth,
            theme,
            elapsed_ms: rounded(performance.now() - started),
            stable_frames: stableCount,
            latest,
        }));
    }""", arg={
        "expectedWidth": width,
        "theme": theme,
        "timeoutMs": 5000,
        "stableFrames": 3,
    })
    return result


def collect_overflow_diagnostics(
    page, *, label: str, theme: str, width: int, geometry: dict,
) -> dict:
    return page.evaluate("""({label, theme, width, geometry}) => {
        const viewport = window.innerWidth;
        const rounded = value => Math.round(value * 100) / 100;
        const styleText = value => String(value || '').slice(0, 160);
        const describe = element => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return {
                tag: element.tagName.toLowerCase(),
                id: (element.id || '').slice(0, 80),
                classes: typeof element.className === 'string' ? element.className.slice(0, 180) : '',
                rect: {
                    left: rounded(rect.left), right: rounded(rect.right),
                    width: rounded(rect.width),
                },
                clientWidth: element.clientWidth,
                scrollWidth: element.scrollWidth,
                style: {
                    display: styleText(style.display),
                    position: styleText(style.position),
                    boxSizing: styleText(style.boxSizing),
                    width: styleText(style.width),
                    minWidth: styleText(style.minWidth),
                    maxWidth: styleText(style.maxWidth),
                    overflowX: styleText(style.overflowX),
                    whiteSpace: styleText(style.whiteSpace),
                    flex: styleText(style.flex),
                    flexBasis: styleText(style.flexBasis),
                    flexShrink: styleText(style.flexShrink),
                    gridTemplateColumns: styleText(style.gridTemplateColumns),
                    paddingLeft: styleText(style.paddingLeft),
                    paddingRight: styleText(style.paddingRight),
                },
            };
        };
        const all = Array.from(document.body.querySelectorAll('*'));
        const spillsRight = all.map(element => ({element, rect: element.getBoundingClientRect()}))
            .filter(item => item.rect.width > 0 && item.rect.right > viewport + 1)
            .sort((a, b) => b.rect.right - a.rect.right);
        const firstSpillingChildren = spillsRight.filter(({element}) => {
            const parent = element.parentElement;
            return !parent || parent.getBoundingClientRect().right <= viewport + 1;
        }).slice(0, 12).map(({element}) => {
            const ancestors = [];
            for (let parent = element.parentElement; parent && ancestors.length < 6; parent = parent.parentElement) {
                ancestors.push(describe(parent));
            }
            return {element: describe(element), ancestors};
        });
        const scrollContainers = all.filter(element => {
            const style = getComputedStyle(element);
            return ['auto', 'scroll'].includes(style.overflowX)
                && element.scrollWidth > element.clientWidth + 1;
        }).sort((a, b) => {
            return (b.scrollWidth - b.clientWidth) - (a.scrollWidth - a.clientWidth);
        }).slice(0, 8).map(describe);
        const main = document.querySelector('#main-content');
        return {
            page: label,
            theme,
            viewport_width: width,
            geometry,
            viewport_inner_width: viewport,
            document: {
                client_width: document.documentElement.clientWidth,
                scroll_width: document.documentElement.scrollWidth,
            },
            body: {
                client_width: document.body.clientWidth,
                scroll_width: document.body.scrollWidth,
            },
            main: main ? describe(main) : null,
            first_spilling_children: firstSpillingChildren,
            largest_right_edges: spillsRight.slice(0, 12).map(({element}) => describe(element)),
            horizontal_scroll_containers: scrollContainers,
        };
    }""", {
        "label": label,
        "theme": theme,
        "width": width,
        "geometry": geometry,
    })


def _assert_local_products_href(href: str, expected_query: dict[str, list[str]]):
    parsed = urlsplit(href)
    assert (
        not parsed.scheme and not parsed.netloc
        and parsed.path == "/products" and not parsed.fragment
    ), {"href": href[:512], "path": parsed.path}
    query = parse_qs(parsed.query, keep_blank_values=True)
    assert query == expected_query, {"href": href[:512], "query": query}
    return parsed


def _assert_local_products_url(url: str, expected_query: dict[str, list[str]]):
    parsed = urlsplit(url)
    origin = urlsplit(BASE or "")
    assert (
        origin.scheme == "http" and origin.netloc
        and parsed.scheme == origin.scheme and parsed.netloc == origin.netloc
        and parsed.path == "/products" and not parsed.fragment
    ), {"path": parsed.path, "scheme_matches": parsed.scheme == origin.scheme,
        "origin_matches": parsed.netloc == origin.netloc}
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key in EMPTY_PRODUCT_FORM_FILTERS:
        if key not in query:
            continue
        if query[key] != [""]:
            raise AssertionError({"path": parsed.path, "unexpected_filter_value": key})
        del query[key]
    assert query == expected_query, {"path": parsed.path, "query": query}
    return parsed


def _bounded_failure_context(page) -> dict:
    parsed = urlsplit(page.url)
    raw_query = parse_qs(parsed.query, keep_blank_values=True)
    query = {
        str(key)[:80]: [str(value)[:120] for value in values[:4]]
        for key, values in list(raw_query.items())[:16]
    }
    try:
        title = str(page.title())[:160]
    except Exception:
        title = ""
    return {"path": parsed.path[:256], "query": query, "title": title}


def capture_layout(page, label: str) -> None:
    if label not in {
        "products_list", "bulk_editor", "bulk_review",
        "single_product_edit", "unmapped_product_edit",
    }:
        raise AssertionError(f"Unexpected WB browser page label: {label!r}")
    ARTIFACTS_PATH.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        page.evaluate(
            "theme => document.documentElement.setAttribute('data-theme', theme)",
            arg=theme,
        )
        wait_for_layout_settle(page, width=1280, theme=theme)
        for width in (390, 768, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            settled = wait_for_layout_settle(page, width=width, theme=theme)
            geometry = page.evaluate("""() => ({
                viewport_width: window.innerWidth,
                document_width: document.documentElement.scrollWidth,
                body_width: document.body.scrollWidth,
                main_width: document.querySelector('main')?.getBoundingClientRect().width ?? null,
                main_content_left: document.querySelector('.main-content')?.getBoundingClientRect().left ?? null,
                main_content_width: document.querySelector('.main-content')?.getBoundingClientRect().width ?? null,
            })""")
            if (
                geometry["viewport_width"] != width
                or abs(geometry["main_content_left"] - settled["main_content_left"]) > 0.75
                or abs(geometry["main_content_width"] - settled["main_content_width"]) > 0.75
            ):
                raise AssertionError(
                    f"Responsive layout changed after settle on {label} ({theme}, {width}px): "
                    f"{geometry}; settle={settled}"
                )
            if geometry["document_width"] > width or geometry["body_width"] > width:
                try:
                    diagnostics = collect_overflow_diagnostics(
                        page, label=label, theme=theme, width=width, geometry=geometry,
                    )
                except Exception as exc:
                    diagnostics = {
                        "page": label,
                        "theme": theme,
                        "viewport_width": width,
                        "geometry": geometry,
                        "diagnostic_error": type(exc).__name__,
                    }
                REPORT["overflow_diagnostics"].append(diagnostics)
                raise AssertionError(
                    f"Horizontal overflow on {label} ({theme}, {width}px): {geometry}; "
                    f"bounded element diagnostics captured"
                )
            REPORT["layouts"].append({
                "page": label, "theme": theme, **geometry,
                "layout_settle": settled,
            })
            if width in (390, 1280):
                filename = f"wb-edit-{label.replace('_', '-')}-{theme}-{width}.png"
                screenshot = ARTIFACTS_PATH / filename
                page.screenshot(
                    path=str(screenshot), full_page=True,
                    timeout=10000,
                )
                REPORT["artifacts"].append({
                    "kind": "layout_screenshot",
                    "path": filename,
                    "page": label,
                    "theme": theme,
                    "width": width,
                })
        check(
            f"responsive_no_horizontal_overflow_{label}_{theme}",
            widths=[390, 768, 1280],
        )
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
    page.set_viewport_size({"width": 1280, "height": 900})


def assert_keyboard_focus(page, label: str) -> None:
    page.evaluate("document.activeElement?.blur()")
    page.keyboard.press("Tab")
    focus = page.evaluate("""() => {
        const element = document.activeElement;
        const rect = element?.getBoundingClientRect();
        return {
            tag: element?.tagName || null,
            id: element?.id || null,
            role: element?.getAttribute('role') || null,
            text: (element?.innerText || element?.getAttribute('aria-label') || '').trim().slice(0, 80),
            keyboard_visible: !!element && element.matches(':focus-visible'),
            rendered: !!rect && rect.width > 0 && rect.height > 0,
        };
    }""")
    assert focus["tag"] not in (None, "BODY") and focus["keyboard_visible"] and focus["rendered"], focus
    check(f"keyboard_focus_visible_{label}", **focus)


def assert_review_summary(page, *, changed: str, skipped: str = "0") -> None:
    cards = page.locator("section[aria-label='Сводка предпросмотра']").inner_text()
    assert "Выбрано" in cards and "50" in cards, cards
    assert "Изменится" in cards and changed in cards, cards
    assert "Пропущено" in cards and skipped in cards, cards
    assert "Ошибки" in cards and "0" in cards, cards
    assert "Предпросмотр" in page.content() and "не отправляет данные в WB" in page.content()


def click_next_product_page(page, *, sort: str, expected_page: int = 2) -> None:
    """Click the visible next-page anchor and verify its full local query state."""
    links = page.locator('nav[aria-label="Навигация по страницам"] a[aria-label="Следующая"]')
    assert links.count() == 1, f"Expected exactly one next-page link, got {links.count()}"
    href = links.get_attribute("href")
    assert href, "Next-page link has no href"
    expected_query = {
        "search": ["Pipedream"],
        "brand": ["Pipedream"],
        "sort": [sort],
        "order": ["asc"],
        "page": [str(expected_page)],
        "per_page": ["50"],
    }
    _assert_local_products_href(href, expected_query)

    with page.expect_navigation(wait_until="domcontentloaded"):
        links.click()
    _assert_local_products_url(page.url, expected_query)
    check(
        "pagination_anchor_preserves_exact_filter_sort_and_page",
        expected_page=expected_page,
        sort=sort,
    )


def run_browser(app, fixture: dict[str, int]) -> None:
    from seller_platform import app as seller_app
    from models import Product, db

    assets = load_pinned_assets()
    origin = BASE
    with sync_playwright() as playwright:
        executable = (
            os.environ.get("UX01_CHROMIUM")
            or os.environ.get("UX01_BROWSER_CHROMIUM")
            or os.environ.get("OZON_BROWSER_CHROMIUM")
            or os.environ.get("CHROMIUM_BIN")
            or os.environ.get("WB_EDIT_CHROMIUM")
            or shutil.which("chromium")
            or "/opt/google/chrome/chrome"
        )
        browser = playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            service_workers="block",
        )
        allowed_local_posts = local_post_allowlist(fixture)
        context.route(
            "**/*",
            lambda route: bridge(
                route,
                assets=assets,
                origin=origin,
                allowed_local_posts=allowed_local_posts,
            ),
        )
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
        page.on("response", record_http_response)

        try:
            # Avoid the legacy dashboard's automatic WB analytics/finance reads;
            # authenticate directly to the page under test. Those APIs remain
            # blocked by forbidden_python_network everywhere in this fixture.
            page.goto(BASE + "/login?next=%2Fproducts", wait_until="domcontentloaded")
            page.locator('input[name="username"]').fill(USERNAME)
            page.locator('input[name="password"]').fill(PASSWORD)
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.locator('form button[type="submit"]').click()
            _assert_local_products_url(page.url, {})
            assert REPORT["provider_attempts"] == 0
            interaction("authenticated_login_with_csrf_enabled")

            # Exercise a non-default page size before the existing exact-50
            # selection journey. Sorting must retain 100 and all meaningful
            # filters; only the five known blank controls from this form are
            # canonicalized by _assert_local_products_url.
            page.goto(
                BASE + "/products?search=Pipedream&brand=Pipedream"
                "&sort=vendor_code&order=asc&page=1&per_page=100",
                wait_until="domcontentloaded",
            )
            _assert_local_products_url(page.url, {
                "search": ["Pipedream"], "brand": ["Pipedream"],
                "sort": ["vendor_code"], "order": ["asc"],
                "page": ["1"], "per_page": ["100"],
            })
            assert page.locator(".product-checkbox").count() == 51
            page.locator('select[name="sort"]').select_option("title")
            page.locator('select[name="order"]').select_option("asc")
            probe_form = page.locator('form[method="GET"][action="/products"]')
            with page.expect_request(
                lambda request: request.is_navigation_request()
                and urlsplit(request.url).path == "/products"
            ) as sort_request:
                with page.expect_navigation(wait_until="domcontentloaded"):
                    probe_form.locator('button[type="submit"]').click()
            assert sort_request.value.method == "GET"
            _assert_local_products_url(page.url, {
                "search": ["Pipedream"], "brand": ["Pipedream"],
                "sort": ["title"], "order": ["asc"], "per_page": ["100"],
            })
            assert page.locator(".product-checkbox").count() == 51
            assert REPORT["provider_attempts"] == 0
            assert REPORT["fake_wb_client_instances"] == 0
            assert REPORT["fake_wb_write_calls"] == 0
            interaction("sort_form_preserves_nondefault_per_page_100_and_filters")

            list_url = (
                "/products?search=Pipedream&brand=Pipedream&sort=vendor_code"
                "&order=asc&page=1&per_page=50"
            )
            page.goto(BASE + list_url, wait_until="domcontentloaded")
            _assert_local_products_url(page.url, {
                "search": ["Pipedream"], "brand": ["Pipedream"],
                "sort": ["vendor_code"], "order": ["asc"],
                "page": ["1"], "per_page": ["50"],
            })
            assert page.locator(".product-checkbox").count() == 50
            assert_keyboard_focus(page, "products_list")
            page.locator("#selectAll").check()
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            interaction("select_exact_first_page_50")

            foreign_probe = page.evaluate("""async payload => {
                const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
                const response = await fetch('/products/selection/resolve', {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
                    body: JSON.stringify(payload),
                });
                const body = await response.json();
                return {status: response.status, code: body.code};
            }""", {
                "mode": "ids",
                "ids": [fixture["foreign_product_id"]],
                "filters": {"search": "Pipedream", "brand": "Pipedream"},
                "sort": "vendor_code", "order": "asc", "page": 1,
                "per_page": 50,
                "return_to": list_url,
            })
            assert foreign_probe == {"status": 403, "code": "selection_unavailable"}, foreign_probe
            REPORT["checks"].append({
                "name": "foreign_product_id_rejected_before_provider",
                "status": "passed",
                "http_status": foreign_probe["status"],
                "code": foreign_probe["code"],
            })

            click_next_product_page(page, sort="vendor_code")
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            assert page.locator(".product-checkbox").count() == 1
            interaction("selection_survives_cross_page_navigation")

            page.locator('select[name="sort"]').select_option("title")
            page.locator('select[name="order"]').select_option("asc")
            filter_form = page.locator('form[method="GET"][action="/products"]')
            assert filter_form.count() == 1, f"Expected one catalog filter form, got {filter_form.count()}"
            with page.expect_request(
                lambda request: request.is_navigation_request()
                and urlsplit(request.url).path == "/products"
            ) as sort_request:
                with page.expect_navigation(wait_until="domcontentloaded"):
                    filter_form.locator('button[type="submit"]').click()
            assert sort_request.value.method == "GET"
            _assert_local_products_url(page.url, {
                "search": ["Pipedream"], "brand": ["Pipedream"],
                "sort": ["title"], "order": ["asc"], "per_page": ["50"],
            })
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            click_next_product_page(page, sort="title")
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            interaction("selection_survives_sort_and_second_page")
            capture_layout(page, "products_list")

            category_probe = page.evaluate("""async names => {
                const values = {};
                for (const name of names) {
                    const response = await fetch('/api/characteristics/' + encodeURIComponent(name), {credentials: 'same-origin'});
                    const body = await response.json();
                    values[name] = {
                        status: response.status,
                        subject_id: body.subject_id,
                        count: body.count,
                        characteristic_count: body.characteristics?.length ?? 0,
                        schema_source: body.schema_source,
                        provider_io: body.provider_io,
                    };
                }
                return values;
            }""", ["Свечи эротик", "Мастурбаторы мужские", "Unmapped synthetic category"])
            assert category_probe["Свечи эротик"] == {
                "status": 200,
                "subject_id": 5880,
                "count": 32,
                "characteristic_count": 32,
                "schema_source": "local_authoritative_cache",
                "provider_io": False,
            }, category_probe
            assert category_probe["Мастурбаторы мужские"] == {
                "status": 200,
                "subject_id": 5070,
                "count": 31,
                "characteristic_count": 31,
                "schema_source": "local_authoritative_cache",
                "provider_io": False,
            }, category_probe
            assert category_probe["Unmapped synthetic category"]["status"] == 409
            assert category_probe["Unmapped synthetic category"]["provider_io"] is False
            check(
                "exact_subject_cache_and_unmapped_category_fail_closed",
                subject_id=5880,
                working_subject_id=5070,
                unmapped_status=category_probe["Unmapped synthetic category"]["status"],
            )

            page.get_by_role("button", name="Редактировать", exact=True).click()
            page.wait_for_url("**/products/bulk-edit")
            account_summary = page.locator("p.uppercase.tracking-wide")
            assert account_summary.count() == 1, (
                f"Expected one scoped WB account/channel label, got {account_summary.count()}"
            )
            account_summary_text = " ".join(account_summary.inner_text().casefold().split())
            assert account_summary_text.startswith("аккаунт: wb edit synthetic shop"), account_summary_text
            assert "wb account wb-edit-account-synthetic" in account_summary_text, account_summary_text
            assert account_summary_text.endswith("канал: wildberries"), account_summary_text
            assert "50 товаров" in page.locator("body").inner_text()
            assert "Pipedream" in page.locator("body").inner_text()
            assert_keyboard_focus(page, "bulk_editor")
            return_links = page.locator('nav a[href^="/products?"]')
            assert return_links.count() == 1, f"Expected one safe products return link, got {return_links.count()}"
            return_href = return_links.get_attribute("href")
            assert return_href, "Safe products return link has no href"
            expected_return_query = {
                "search": ["Pipedream"], "brand": ["Pipedream"],
                "sort": ["title"], "order": ["asc"],
                "page": ["2"], "per_page": ["50"],
            }
            _assert_local_products_href(return_href, expected_return_query)
            assert REPORT["fake_wb_client_instances"] == 0
            capture_layout(page, "bulk_editor")
            interaction("bulk_editor_names_wb_account_channel_and_safe_return_context")

            with page.expect_navigation(wait_until="domcontentloaded"):
                return_links.click()
            _assert_local_products_url(page.url, expected_return_query)
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            assert page.locator('select[name="sort"]').input_value() == "title"
            interaction("safe_return_link_restores_filter_sort_page_selection")
            page.get_by_role("button", name="Редактировать", exact=True).click()
            page.wait_for_url("**/products/bulk-edit")

            page.locator('input[name="operation"][value="update_brand"]').check()
            page.locator("#value_brand").fill("Synthetic mixed action must stop")
            page.locator('form[action="/products/bulk-edit"]').evaluate("""form => {
                const hidden = document.createElement('input');
                hidden.type = 'hidden'; hidden.name = 'ai_operations'; hidden.value = 'ai_keywords';
                form.appendChild(hidden);
                form.submit();
            }""")
            page.wait_for_load_state("domcontentloaded")
            assert "Ручная и AI-операции не объединяются" in page.locator("body").inner_text()
            assert "Ничего не было применено" in page.locator("body").inner_text()
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            check("mixed_manual_and_ai_operations_blocked_before_write")
            interaction("mixed_manual_ai_post_gets_actionable_no_write_notice")

            page.locator('input[name="operation"][value="update_brand"]').check()
            page.locator("#value_brand").fill("Pipedream")
            page.locator('form[action="/products/bulk-edit"] button[type="submit"]').click()
            page.wait_for_selector("section[aria-label='Сводка предпросмотра']")
            assert_review_summary(page, changed="0")
            assert page.locator('input[name="preview_token"]').input_value() == ""
            assert page.get_by_role("button", name="Подтвердить и применить 0 карточек").is_disabled()
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            check("no_op_bulk_preview_has_no_apply_token_or_provider_client")
            interaction("no_op_preview_has_no_apply_token_or_provider_client")

            page.locator("nav button").filter(has_text="Массовое редактирование").click()
            page.wait_for_url("**/products/bulk-edit")
            page.locator('input[name="operation"][value="update_brand"]').check()
            page.locator("#value_brand").fill("Synthetic reviewed brand")
            page.locator('form[action="/products/bulk-edit"] button[type="submit"]').click()
            page.wait_for_selector("section[aria-label='Сводка предпросмотра']")
            assert_review_summary(page, changed="50")
            assert "Pipedream" in page.content() and "Synthetic reviewed brand" in page.content()
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            assert_keyboard_focus(page, "bulk_review")
            check(
                "manual_preview_reports_exact_selection_diff_before_fake_apply",
                selected=50,
                changed=50,
                skipped=0,
                fake_wb_client_instances=0,
            )
            interaction("manual_bulk_preview_shows_exact_50_row_diff_without_provider_io")
            capture_layout(page, "bulk_review")

            # Simulate a local edit racing the confirmation. The signed review
            # must be rejected before the fake provider is even constructed.
            from models import Product, db
            with seller_app.app_context():
                first_product = db.session.get(Product, fixture["product_id"])
                first_product.brand = "Concurrent local change"
                db.session.commit()
            page.get_by_role("button", name="Подтвердить и применить 50 карточек").click()
            page.wait_for_url("**/products/bulk-edit")
            assert "измен" in page.locator("body").inner_text().casefold()
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
            check(
                "local_review_drift_rejected_before_provider",
                fake_wb_client_instances=REPORT["fake_wb_client_instances"],
            )

            with seller_app.app_context():
                first_product = db.session.get(Product, fixture["product_id"])
                first_product.brand = "Pipedream"
                db.session.commit()
            page.locator('input[name="operation"][value="update_brand"]').check()
            page.locator("#value_brand").fill("Synthetic reviewed brand")
            page.locator('form[action="/products/bulk-edit"] button[type="submit"]').click()
            page.wait_for_selector("section[aria-label='Сводка предпросмотра']")
            assert_review_summary(page, changed="50")
            page.get_by_role("button", name="Подтвердить и применить 50 карточек").click()
            page.wait_for_url("**/bulk-history/*")
            assert REPORT["fake_wb_write_calls"] == 1
            assert len(REPORT["fake_wb_written_products"]) == 50
            assert len(set(REPORT["fake_wb_written_products"])) == 50
            assert REPORT["provider_attempts"] == 0
            check(
                "reviewed_apply_reaches_only_fake_provider_with_exact_50_products",
                fake_wb_write_calls=REPORT["fake_wb_write_calls"],
                exact_product_count=len(REPORT["fake_wb_written_products"]),
            )
            interaction("single_review_confirm_reaches_only_fake_provider_boundary")

            page.goto(BASE + f"/products/{fixture['product_id']}/edit", wait_until="domcontentloaded")
            assert_keyboard_focus(page, "single_product_edit")
            characteristics_panel = page.locator("section[aria-labelledby='wb-characteristics-title']")
            assert "subjectID 5880" in characteristics_panel.inner_text()
            assert "1 заполнено · 32 в схеме" in characteristics_panel.inner_text()
            assert page.locator("#char_202").evaluate("el => el.tagName === 'SELECT'")
            assert ["Россия", "Китай"] == page.locator("#char_202 option").evaluate_all(
                "items => items.map(option => option.value).filter(Boolean)"
            )
            assert page.locator("#char_303").get_attribute("type") == "number"
            assert "(г)" in page.locator('label[for="char_303"]').inner_text()
            assert page.locator("#char_404").get_attribute("multiple") is not None
            assert page.locator("#char_404 option").count() == 3
            assert "SYNTHETIC-WB-SKU-000" in page.locator("body").inner_text()
            assert page.locator('input[name="sku"], input[name="sizes_json"]').count() == 0
            capture_layout(page, "single_product_edit")
            REPORT["checks"].append({
                "name": "single_edit_uses_cached_country_weight_multi_schema_and_read_only_sku",
                "status": "passed",
                "subject_id": 5880,
                "schema_fields": 32,
                "fake_wb_write_calls": REPORT["fake_wb_write_calls"],
            })

            unmapped_url = BASE + f"/products/{fixture['unmapped_product_id']}/edit"
            page.goto(unmapped_url, wait_until="domcontentloaded")
            assert_keyboard_focus(page, "unmapped_product_edit")
            unmapped_panel = page.locator("section[aria-labelledby='wb-characteristics-title']")
            assert "subjectID отсутствует" in unmapped_panel.inner_text()
            assert "Редактирование характеристик недоступно" in unmapped_panel.inner_text()
            assert page.locator('input[name^="char_"], select[name^="char_"], textarea[name^="char_"]').count() == 0
            unmapped_panel.locator("summary").click()
            assert "Stale prior-subject field" in unmapped_panel.inner_text()
            assert "Preserve read-only historical value" in unmapped_panel.inner_text()
            capture_layout(page, "unmapped_product_edit")

            prior_fake_client_count = REPORT["fake_wb_client_instances"]
            stale_post = page.evaluate("""async ({path, value}) => {
                const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
                const body = new URLSearchParams({csrf_token: csrf, char_101: value}).toString();
                const response = await fetch(path, {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrf},
                    body,
                });
                return {status: response.status, html: await response.text()};
            }""", {
                "path": f"/products/{fixture['unmapped_product_id']}/edit",
                "value": "",
            })
            assert stale_post["status"] == 200
            assert "Точная локальная WB-схема устарела или недоступна" in stale_post["html"]
            assert REPORT["fake_wb_client_instances"] == prior_fake_client_count
            check(
                "unmapped_subject_keeps_old_characteristics_read_only_and_rejects_clear_post",
                status=stale_post["status"],
                fake_wb_client_instances=REPORT["fake_wb_client_instances"],
            )

            # Filter change resets the stored exact set rather than expanding
            # it silently. Then all-filtered is a separate explicit action;
            # unchecking one row records an exact exclusion.
            page.goto(BASE + "/products?search=Pipedream&brand=Synthetic%20reviewed%20brand&sort=title&order=asc&page=1&per_page=50")
            assert page.locator("#selectedCount").inner_text().strip() == "0"
            interaction("changing_filter_clears_prior_selection_without_expansion")
            page.get_by_role("button", name="Выбрать все отфильтрованные (50)").click()
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            page.locator(".product-checkbox").first.uncheck()
            assert page.locator("#selectedCount").inner_text().strip() == "49"
            interaction("all_filtered_is_explicit_and_manual_uncheck_is_an_exact_exclusion")
            page.locator("button").filter(has_text="Отменить выбор").click()
            assert page.locator("#selectedCount").inner_text().strip() == "0"

            assert REPORT["provider_attempts"] == 0
            assert REPORT["javascript_errors"] == []
            assert REPORT["unexpected_external_requests"] == []
            assert REPORT["unexpected_http"] == []
            missing_negative = set(EXPECTED_NEGATIVE_HTTP).difference(OBSERVED_EXPECTED_NEGATIVE_HTTP)
            assert not missing_negative, f"Expected negative probes were not observed: {sorted(missing_negative)}"
            expected_layouts = {
                (page_name, theme, width)
                for page_name in (
                    "products_list", "bulk_editor", "bulk_review",
                    "single_product_edit", "unmapped_product_edit",
                )
                for theme in ("light", "dark")
                for width in (390, 768, 1280)
            }
            actual_layouts = {
                (row.get("page"), row.get("theme"), row.get("viewport_width"))
                for row in REPORT["layouts"]
            }
            assert len(REPORT["layouts"]) == 30 and actual_layouts == expected_layouts, (
                f"Expected all 30 page/theme/viewport rows, got {len(REPORT['layouts'])}"
            )
            expected_screenshots = {
                (page_name, theme, width)
                for page_name in (
                    "products_list", "bulk_editor", "bulk_review",
                    "single_product_edit", "unmapped_product_edit",
                )
                for theme in ("light", "dark")
                for width in (390, 1280)
            }
            layout_artifacts = [
                item for item in REPORT["artifacts"]
                if item.get("kind") == "layout_screenshot"
            ]
            actual_screenshots = {
                (item.get("page"), item.get("theme"), item.get("width"))
                for item in layout_artifacts
            }
            if (
                len(layout_artifacts) != 20
                or actual_screenshots != expected_screenshots
                or any(
                    not isinstance(item.get("path"), str)
                    or "/" in item["path"] or "\\" in item["path"]
                    for item in layout_artifacts
                )
            ):
                raise AssertionError("Expected 20 bounded layout screenshots across 5 pages, 2 themes, and 390/1280px")
            assert len(REPORT["checks"]) >= 24, f"Expected at least 24 named browser checks, got {len(REPORT['checks'])}"
            assert REPORT["checks"] and REPORT["interactions"] and REPORT["layouts"]
            REPORT["status"] = "complete"
        except Exception:
            try:
                REPORT["failure_context"] = _bounded_failure_context(page)
            except Exception:
                REPORT["failure_context"] = {"context_error": "unavailable"}
            try:
                ARTIFACTS_PATH.mkdir(parents=True, exist_ok=True)
                screenshot = ARTIFACTS_PATH / "wb-edit-browser-failure.png"
                page.screenshot(path=str(screenshot), full_page=True, timeout=5000)
                REPORT["artifacts"].append({
                    "kind": "failure_screenshot",
                    "path": screenshot.name,
                })
            except Exception as screenshot_error:
                REPORT["artifact_errors"].append({
                    "kind": "failure_screenshot",
                    "error_type": type(screenshot_error).__name__,
                })
            raise
        finally:
            context.close()
            browser.close()


def main() -> int:
    global SERVER, SERVER_THREAD, BASE
    from seller_platform import app
    import seller_platform
    from models import db

    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=True,
        SESSION_COOKIE_SECURE=False,
        MARKETPLACE_OZON_ENABLED=False,
        MARKETPLACE_OZON_PUBLICATION_ENABLED=False,
        MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
    )
    REPORT["source_hashes"] = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in (
            "seller_platform.py",
            "services/product_selection.py",
            "services/wb_edit_review.py",
            "templates/products.html",
            "templates/products_bulk_edit.html",
            "templates/products_bulk_edit_review.html",
            "templates/product_edit.html",
            "tests/ux01/wb_edit_browser.py",
        )
    }
    # Photo URL rendering belongs to WB's CDN. The synthetic browser fixture
    # keeps the page structure while replacing those decorative URLs locally.
    app.jinja_env.globals["wb_photo_url"] = lambda *_args, **_kwargs: "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="
    seller_platform.WildberriesAPIClient = FakeWBClient
    fixture = seed_synthetic_wb(app)

    try:
        SERVER = make_server("127.0.0.1", 0, app, threaded=True)
    except PermissionError as exc:
        REPORT["status"] = "blocked"
        REPORT["blocked_reason"] = f"loopback listener denied by execution policy: {exc}"
        REPORT["checks"].append({
            "name": "loopback_listener_for_flask_playwright_fixture",
            "status": "blocked",
        })
        write_report()
        print(json.dumps({"status": REPORT["status"], "report": str(REPORT_PATH)}))
        return 2
    except OSError as exc:
        REPORT["status"] = "blocked"
        REPORT["blocked_reason"] = f"local browser fixture could not bind its loopback listener: {exc}"
        REPORT["checks"].append({
            "name": "loopback_listener_for_flask_playwright_fixture",
            "status": "blocked",
        })
        write_report()
        print(json.dumps({"status": REPORT["status"], "report": str(REPORT_PATH)}))
        return 2

    BASE = f"http://127.0.0.1:{SERVER.server_port}"
    SERVER_THREAD = threading.Thread(target=SERVER.serve_forever, daemon=True)
    SERVER_THREAD.start()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        run_browser(app, fixture)
    except Exception as exc:
        REPORT["status"] = "failed"
        REPORT["failure"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        SERVER.shutdown()
        SERVER_THREAD.join(timeout=5)
        write_report()
        TEMP.cleanup()
    print(json.dumps({"status": REPORT["status"], "report": str(REPORT_PATH)}))
    return 0 if REPORT["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
