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
REPORT_PATH = Path(os.environ.get(
    "WB_EDIT_BROWSER_REPORT", "/tmp/wb-edit-browser-report.json",
))
ASSET_DIR = ROOT / "tests/ozon_release/assets"
USERNAME = "ux01-wb-edit-seller"
PASSWORD = "synthetic-wb-edit-password"
FIXTURE = {}
SERVER = None
SERVER_THREAD = None
BASE = None

REPORT = {
    "source": "worktree",
    "status": "running",
    "scope": "synthetic_wb_exact_selection_review_and_edit",
    "database": "disposable_sqlite",
    "network_policy": "loopback_and_hash_pinned_assets_only",
    "layouts": [],
    "checks": [],
    "interactions": [],
    "javascript_errors": [],
    "unexpected_external_requests": [],
    "unexpected_http": [],
    "provider_attempts": 0,
    "fake_wb_write_calls": 0,
    "fake_wb_written_products": [],
    "fake_wb_client_instances": 0,
    "blocked_reason": None,
}

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
            categories_synced_at=datetime(2026, 9, 30, 18, 0),
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
            characteristics_synced_at=datetime(2026, 9, 30, 18, 0),
            characteristics_sync_status="success",
            characteristics_schema_hash="a" * 64,
            characteristics_version=1,
            characteristics_count=32,
        )
        db.session.add(category)
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
        db.session.add(MarketplaceDirectory(
            marketplace_id=marketplace.id,
            directory_type="countries",
            data_json='["Россия", "Китай"]',
            synced_at=datetime(2026, 9, 30, 18, 0),
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
            characteristics_json="[]",
            sizes_json="[]",
            photos_json="[]",
            is_active=True,
        )
        db.session.add_all([*products, foreign, unmapped])
        db.session.commit()
        return {
            "seller_id": int(seller.id),
            "foreign_product_id": int(foreign.id),
            "product_id": int(products[0].id),
            "product_count": len(products),
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


def bridge(route, *, assets: dict[str, dict[str, object]], origin: str) -> None:
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
        if request.method == "POST" and parsed.path not in {
            "/login",
            "/products/selection/resolve",
            "/products/bulk-edit",
        }:
            REPORT["unexpected_external_requests"].append({
                "method": request.method,
                "path": parsed.path,
                "reason": "unapproved_local_mutation",
            })
            route.abort()
            return
        route.continue_()
        return
    asset = assets.get(request.url)
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
    expected_security = (
        parsed.path == "/products/selection/resolve"
        and response.status in {400, 403, 409}
    )
    expected_unmapped = (
        parsed.path.endswith("/Unmapped%20synthetic%20category")
        or parsed.path.endswith("/Unmapped synthetic category")
    ) and response.status == 409
    if not expected_security and not expected_unmapped:
        REPORT["unexpected_http"].append({
            "status": response.status,
            "method": response.request.method,
            "path": parsed.path,
        })


def check(name: str, **details) -> None:
    REPORT["checks"].append({"name": name, "status": "passed", **details})


def interaction(name: str, **details) -> None:
    REPORT["interactions"].append({"name": name, **details})


def capture_layout(page, label: str) -> None:
    for width in (390, 768, 1280):
        page.set_viewport_size({"width": width, "height": 900})
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        geometry = page.evaluate("""() => ({
            viewport_width: window.innerWidth,
            document_width: document.documentElement.scrollWidth,
            body_width: document.body.scrollWidth,
            main_width: document.querySelector('main')?.getBoundingClientRect().width ?? null,
        })""")
        REPORT["layouts"].append({"page": label, **geometry})
    page.set_viewport_size({"width": 1280, "height": 900})


def assert_review_summary(page, *, changed: str, skipped: str = "0") -> None:
    cards = page.locator("section[aria-label='Сводка предпросмотра']").inner_text()
    assert "Выбрано" in cards and "50" in cards, cards
    assert "Изменится" in cards and changed in cards, cards
    assert "Пропущено" in cards and skipped in cards, cards
    assert "Ошибки" in cards and "0" in cards, cards
    assert "Предпросмотр" in page.content() and "не отправляет данные в WB" in page.content()


def run_browser(app, fixture: dict[str, int]) -> None:
    from seller_platform import app as seller_app
    from models import Product, db

    assets = load_pinned_assets()
    origin = BASE
    with sync_playwright() as playwright:
        executable = os.environ.get("WB_EDIT_CHROMIUM") or shutil.which("chromium") or "/opt/google/chrome/chrome"
        browser = playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            service_workers="block",
        )
        context.route("**/*", lambda route: bridge(route, assets=assets, origin=origin))
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
        page.on("response", record_http_response)

        try:
            page.goto(BASE + "/login", wait_until="domcontentloaded")
            page.locator('input[name="username"]').fill(USERNAME)
            page.locator('input[name="password"]').fill(PASSWORD)
            page.locator('form button[type="submit"]').click()
            page.wait_for_url("**/dashboard")
            interaction("authenticated_login_with_csrf_enabled")

            list_url = (
                "/products?search=Pipedream&brand=Pipedream&sort=vendor_code"
                "&order=asc&page=1&per_page=50"
            )
            page.goto(BASE + list_url, wait_until="domcontentloaded")
            assert page.locator(".product-checkbox").count() == 50
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

            page.locator('a[aria-label="Следующая"]').click()
            page.wait_for_url("**page=2**")
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            assert page.locator(".product-checkbox").count() == 1
            interaction("selection_survives_cross_page_navigation")

            page.locator('select[name="sort"]').select_option("title")
            page.locator('select[name="order"]').select_option("asc")
            page.locator('form[method="GET"] button[type="submit"]').first.click()
            page.wait_for_url("**sort=title**")
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            page.locator('a[aria-label="Следующая"]').click()
            page.wait_for_url("**page=2**")
            assert page.locator("#selectedCount").inner_text().strip() == "50"
            interaction("selection_survives_sort_and_second_page")
            capture_layout(page, "products_list")

            category_probe = page.evaluate("""async names => {
                const values = {};
                for (const name of names) {
                    const response = await fetch('/api/characteristics/' + encodeURIComponent(name), {credentials: 'same-origin'});
                    const body = await response.json();
                    values[name] = {status: response.status, subject_id: body.subject_id, provider_io: body.provider_io};
                }
                return values;
            }""", ["Свечи эротик", "Unmapped synthetic category"])
            assert category_probe["Свечи эротик"] == {
                "status": 200, "subject_id": 5880, "provider_io": False,
            }, category_probe
            assert category_probe["Unmapped synthetic category"]["status"] == 409
            assert category_probe["Unmapped synthetic category"]["provider_io"] is False
            REPORT["checks"].append({
                "name": "exact_subject_cache_and_unmapped_category_fail_closed",
                "status": "passed",
                "subject_id": 5880,
                "unmapped_status": category_probe["Unmapped synthetic category"]["status"],
            })

            page.get_by_role("button", name="Редактировать", exact=True).click()
            page.wait_for_url("**/products/bulk-edit")
            assert "WB edit synthetic shop" in page.locator("body").inner_text()
            assert "wb-edit-account-synthetic" in page.locator("body").inner_text()
            assert "50 товаров" in page.locator("body").inner_text()
            assert "Pipedream" in page.locator("body").inner_text()
            editor_return = page.locator('nav a[href^="/products?"]').first.get_attribute("href")
            assert editor_return
            returned = urlsplit(editor_return)
            params = parse_qs(returned.query)
            assert returned.path == "/products"
            assert params.get("search") == ["Pipedream"]
            assert params.get("brand") == ["Pipedream"]
            assert params.get("sort") == ["title"]
            assert params.get("order") == ["asc"]
            assert params.get("page") == ["2"]
            assert params.get("per_page") == ["50"]
            assert REPORT["fake_wb_client_instances"] == 0
            capture_layout(page, "bulk_editor")
            interaction("bulk_editor_names_wb_account_channel_and_safe_return_context")

            page.locator('a[href^="/products?"]').first.click()
            page.wait_for_url("**page=2**")
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
            interaction("mixed_manual_ai_post_gets_actionable_no_write_notice")

            page.locator('input[name="operation"][value="update_brand"]').check()
            page.locator("#value_brand").fill("Pipedream")
            page.locator('form[action="/products/bulk-edit"] button[type="submit"]').click()
            page.wait_for_selector("section[aria-label='Сводка предпросмотра']")
            assert_review_summary(page, changed="0")
            assert page.locator('input[name="preview_token"]').input_value() == ""
            assert page.get_by_role("button", name="Подтвердить и применить 0 карточек").is_disabled()
            assert REPORT["fake_wb_client_instances"] == REPORT["fake_wb_write_calls"] == 0
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
            REPORT["checks"].append({
                "name": "local_review_drift_rejected_before_provider",
                "status": "passed",
                "fake_wb_client_instances": REPORT["fake_wb_client_instances"],
            })

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
            interaction("single_review_confirm_reaches_only_fake_provider_boundary")

            page.goto(BASE + f"/products/{fixture['product_id']}/edit", wait_until="domcontentloaded")
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
            assert REPORT["checks"] and REPORT["interactions"] and REPORT["layouts"]
            REPORT["status"] = "complete"
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
