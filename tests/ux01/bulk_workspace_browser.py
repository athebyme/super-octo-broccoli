"""Exercise the bulk workspace with an isolated database and no provider I/O."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.ux01 import journey_browser as fixture
from tests.ux01.bulk_action_controls import invoke_workspace_action, workspace_action_launcher
from models import (
    BackgroundJob, EnrichmentJob, ImportedProduct, PriceChangeBatch,
    PriceChangeItem, Product, Seller, SupplierProduct, db,
)


OUTPUT = Path(os.environ.get("BULK_WORKSPACE_ARTIFACTS", "/artifacts/bulk-workspace"))
MAIN_TITLE = "Карточка WB с обновлением поставщика"
REFRESHED_TITLE = "Обновлённое название из каталога поставщика"
MANUAL_TITLE = "Внутренний товар без поставщика"
LOCAL_REFRESH = "/my-products/refresh-from-supplier"
LOCAL_REFRESH_PREVIEW = "/api/my-products/supplier-refresh-preview"
LOCAL_REFRESH_RESULT = "/api/my-products/supplier-refresh-result"
LOCAL_OZON_PREPARE = "/marketplaces/ozon/uploads/"
LOCAL_WB_ENRICH_REVIEW = "/products/enrich-bulk"
LOCAL_WB_ENRICH_START = "/api/products/enrich-bulk/start"
LOCAL_WB_SELECTION_RESOLVE = "/products/selection/resolve"
LOCAL_WB_BULK_EDIT = "/products/bulk-edit"
LOCAL_SUPPLIER_IMPORT = "/supplier-catalog/import"
LOCAL_PRICE_CREATE = "/prices/create-batch"
LOCAL_PRICE_PREVIEW = "/prices/api/preview"
WB_THUMBNAIL_ORIGIN = "https://basket-01.wbbasket.ru"
WB_THUMBNAIL_GIF = base64.b64decode("R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=")


def seed_bulk_selection():
    with fixture.app.app_context():
        seller_id = fixture.FIXTURE["seller_id"]
        seller = db.session.get(Seller, seller_id)
        seller.wb_api_key = "synthetic-wb-key-for-bulk-workspace-only"
        wb_product = Product(id=4419902, seller_id=seller_id, nm_id=4419901, vendor_code="BULK-WB-001",
                             title=MAIN_TITLE, is_active=True, price=1000, wb_price=600)
        preview_specs = [
            (4420001, 4420101, "Сравнение цен WB · база", "PREVIEW-BASE", 900, 600),
            (4420002, 4420102, "Сравнение цен WB · ноль", "PREVIEW-ZERO", 900, 0),
            (4420003, 4420103, "Сравнение цен WB · fallback", "PREVIEW-FALLBACK", 1200, None),
        ]
        preview_products = [Product(
            id=product_id,
            seller_id=seller_id,
            nm_id=nm_id,
            vendor_code=vendor_code,
            title=title,
            is_active=True,
            price=local_price,
            wb_price=wb_price,
            wb_price_synced_at=(datetime.utcnow() if wb_price is not None else None),
        ) for product_id, nm_id, title, vendor_code, local_price, wb_price in preview_specs]
        preview_sources = [ImportedProduct(
            seller_id=seller_id,
            external_id=f"WB-PRICE-PREVIEW-{index}",
            external_vendor_code=vendor_code,
            source_type="manual",
            title=title,
            photo_urls="[]",
            product_id=product_id,
            wb_nm_id=nm_id,
            import_status="imported",
            supplier_quantity=3,
            created_at=datetime(2020, 1, 1) + timedelta(days=3 - index),
        ) for index, (product_id, nm_id, title, vendor_code, _local, _wb) in enumerate(
            preview_specs, 1,
        )]
        db.session.add(wb_product)
        db.session.add_all(preview_products)
        db.session.flush()
        db.session.add_all(preview_sources)
        unrelated_price_batch = PriceChangeBatch(
            seller_id=seller_id,
            name="Unrelated price batch for return binding",
            change_type="set",
            change_value=900,
            status="confirmed",
            total_items=1,
            safe_count=1,
            has_safe_changes=True,
        )
        db.session.add(unrelated_price_batch)
        db.session.flush()
        db.session.add(PriceChangeItem(
            batch_id=unrelated_price_batch.id,
            product_id=wb_product.id,
            nm_id=wb_product.nm_id,
            vendor_code=wb_product.vendor_code,
            product_title=wb_product.title,
            old_price=1000,
            new_price=900,
            price_change_amount=-100,
            price_change_percent=-10,
            safety_level="safe",
        ))
        supplier_item = db.session.get(SupplierProduct, fixture.FIXTURE["supplier_product_id"])
        supplier_item.content_revision = 2
        supplier_item.title = REFRESHED_TITLE
        supplier_item.supplier_quantity = 0
        original = db.session.get(ImportedProduct, fixture.FIXTURE["source_id"])
        original.title = MAIN_TITLE
        original.supplier_quantity = 3
        original.product_id = wb_product.id
        original.wb_nm_id = wb_product.nm_id
        original.import_status = "imported"
        original.supplier_id = supplier_item.supplier_id
        original.supplier_product_id = supplier_item.id
        original.supplier_content_revision = 1
        manual = ImportedProduct(seller_id=seller_id, external_id="BULK-LOCAL-002", source_type="manual",
                                 title=MANUAL_TITLE, import_status="validated")
        new_source = SupplierProduct(supplier_id=supplier_item.supplier_id, external_id="BULK-IMPORT-003",
                                     title="Позиция для импорта", vendor_code="BULK-IMPORT-003",
                                     supplier_price=100, currency="RUB", supplier_quantity=4,
                                     supplier_status="in_stock", status="ready")
        db.session.add_all([manual, new_source])
        bulk_rows = [ImportedProduct(
            seller_id=seller_id,
            external_id=f"UX-BULK-OWNED-{index:03}",
            external_vendor_code=f"UX-BULK-OWNED-{index:03}",
            source_type="manual",
            title=f"UX-BULK-OWNED-{index:03}",
            photo_urls=json.dumps([f"https://fixture.invalid/{index}.svg"]),
            supplier_quantity=3,
            import_status="validated",
            created_at=datetime(2020, 1, 1) + timedelta(days=index),
        ) for index in range(1, 54)]
        other_filter = ImportedProduct(
            seller_id=seller_id,
            external_id="UX-BULK-OWNED-000-NO-PHOTO",
            external_vendor_code="UX-BULK-OWNED-000-NO-PHOTO",
            source_type="manual",
            title="UX-BULK-OWNED-000-NO-PHOTO",
            photo_urls="[]",
            supplier_quantity=3,
            import_status="validated",
            created_at=datetime(2019, 1, 1),
        )
        foreign_seller = Seller.query.filter(Seller.id != seller_id).first()
        assert foreign_seller is not None
        foreign = ImportedProduct(
            seller_id=foreign_seller.id,
            external_id="UX-BULK-OWNED-000-FOREIGN",
            external_vendor_code="UX-BULK-OWNED-000-FOREIGN",
            source_type="manual",
            title="UX-BULK-OWNED-000-FOREIGN",
            photo_urls=json.dumps(["https://fixture.invalid/foreign.svg"]),
            supplier_quantity=3,
            import_status="validated",
            created_at=datetime(2021, 1, 1),
        )
        db.session.add_all([*bulk_rows, other_filter, foreign])
        db.session.commit()
        assert wb_product.id != original.id
        return {"wb_id": wb_product.id, "local_id": original.id,
                "manual_id": manual.id, "import_id": new_source.id,
                "refresh_source_id": supplier_item.id,
                "unrelated_price_batch_id": unrelated_price_batch.id,
                "bulk_ids": [row.id for row in bulk_rows],
                "preview_wb_ids": [item[0] for item in preview_specs],
                "preview_nm_ids": [item[1] for item in preview_specs],
                "preview_local_ids": [source.id for source in preview_sources],
                "preview_titles": [item[2] for item in preview_specs]}


def verify_bounds(page):
    dimensions = page.evaluate("""() => ({scroll: document.documentElement.scrollWidth,
                                     viewport: document.documentElement.clientWidth})""")
    assert dimensions["scroll"] <= dimensions["viewport"] + 1, dimensions


def verify_mobile_supplier_refresh_focus(page):
    page.wait_for_function("""() => {
        const result = document.querySelector('#supplier-refresh-result');
        const header = document.querySelector('.mobile-header');
        const title = result && result.querySelector('h2');
        if (!result || !header || !title || document.activeElement !== result) return false;
        const headerRect = header.getBoundingClientRect();
        const resultRect = result.getBoundingClientRect();
        const titleRect = title.getBoundingClientRect();
        return getComputedStyle(header).position === 'fixed' &&
            getComputedStyle(header).display !== 'none' &&
            headerRect.top >= 0 && headerRect.bottom <= window.innerHeight &&
            titleRect.top >= headerRect.bottom && titleRect.bottom <= window.innerHeight &&
            resultRect.left >= 0 && resultRect.right <= document.documentElement.clientWidth;
    }""")
    geometry = page.evaluate("""() => {
        const result = document.querySelector('#supplier-refresh-result');
        const header = document.querySelector('.mobile-header');
        const title = result.querySelector('h2');
        return {
            focused: document.activeElement === result,
            headerBottom: header.getBoundingClientRect().bottom,
            titleTop: title.getBoundingClientRect().top,
            titleBottom: title.getBoundingClientRect().bottom,
            viewportHeight: window.innerHeight,
            resultLeft: result.getBoundingClientRect().left,
            resultRight: result.getBoundingClientRect().right,
            viewportWidth: document.documentElement.clientWidth,
        };
    }""")
    assert geometry["focused"], geometry
    assert geometry["titleTop"] >= geometry["headerBottom"], geometry
    assert geometry["titleBottom"] <= geometry["viewportHeight"], geometry
    assert geometry["resultLeft"] >= 0 and geometry["resultRight"] <= geometry["viewportWidth"], geometry
    verify_bounds(page)


def verify_enrichment_review_bounds(page):
    verify_bounds(page)
    geometry = page.evaluate("""() => {
        const rect = element => {
            const box = element.getBoundingClientRect();
            return {left: box.left, right: box.right, width: box.width, height: box.height};
        };
        const visibleRects = selector => Array.from(document.querySelectorAll(selector))
            .filter(element => element.getClientRects().length > 0)
            .map(rect);
        return {
            viewport: document.documentElement.clientWidth,
            cards: visibleRects('.be-grid > .sh-card'),
            labels: visibleRects('.be-grid label'),
            controls: visibleRects('.be-grid input'),
            confirm: (() => {
                const button = document.querySelector('.be-grid .be-actions > button');
                return button ? rect(button) : null;
            })(),
        };
    }""")
    assert geometry["viewport"] == 320, geometry
    assert len(geometry["cards"]) == 2, geometry
    assert geometry["labels"] and geometry["controls"] and geometry["confirm"], geometry
    for group in (geometry["cards"], geometry["labels"], geometry["controls"],
                  [geometry["confirm"]]):
        for bounds in group:
            assert bounds["width"] > 0 and bounds["height"] > 0, bounds
            assert bounds["left"] >= -1 and bounds["right"] <= geometry["viewport"] + 1, bounds


def open_products(page, title=MAIN_TITLE):
    fixture.wait_document(page, "bulk_workspace", "/my-products/beta")
    page.get_by_role("checkbox", name=f"Выбрать {title}").wait_for()


def selected_browser_state(page):
    return page.evaluate("""() => {
        const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
        return proxy ? {
            selected: proxy.selected.slice(),
            ozonIds: proxy.ozonIds.slice(),
        } : null;
    }""")


def refresh_ozon_button(page, count=1):
    return page.get_by_role("button", name=f"Подготовить для Ozon ({count})")


def refresh_wb_button(page, count=1):
    return page.get_by_role("button", name=f"Проверить характеристики WB ({count})")


BULK_ACTION_KEYS = {
    "Создать карточки WB": "push_wb",
    "Подготовить для Ozon": "ozon",
    "Обновить из поставщика": "refresh_supplier",
    "Изменить общий контент": "common_content",
    "Изменить карточки WB": "wb_edit",
    "Изменить цены WB": "wb_prices",
    "Изменить цены Ozon": "ozon_prices",
    "Изменить карточки Ozon": "ozon_workspace_update",
}


def pick_action(page, name):
    key = BULK_ACTION_KEYS.get(name)
    assert key, f"No intent route mapped for {name!r}"
    return invoke_workspace_action(page, key)


def feed_matches(response, expected):
    parsed = urlsplit(response.url)
    if parsed.path != "/api/my-products" or response.request.method != "GET":
        return False
    query = parse_qs(parsed.query)
    return all(query.get(key) == [value] for key, value in expected.items())


def begin_domain_write_watch():
    """Track app database writes only for a browser navigation window."""
    from sqlalchemy import event

    with fixture.app.app_context():
        engine = db.engine
    writes = []

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        command = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if command in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(command)

    event.listen(engine, "before_cursor_execute", observe)
    return engine, observe, writes


def end_domain_write_watch(watch):
    from sqlalchemy import event

    engine, observe, writes = watch
    event.remove(engine, "before_cursor_execute", observe)
    return writes


def is_supplier_refresh_result(response):
    parsed = urlsplit(response.url)
    return parsed.path == LOCAL_REFRESH_RESULT and response.request.method == "GET"


def is_supplier_refresh_preview(response):
    parsed = urlsplit(response.url)
    return parsed.path == LOCAL_REFRESH_PREVIEW and response.request.method == "POST"


def open_supplier_refresh_preview(page, expected_ids, preview_posts):
    """Open the real no-provider review and keep its signed token only in RAM."""
    with page.expect_response(is_supplier_refresh_preview) as response_info:
        action_steps = invoke_workspace_action(page, "refresh_supplier")
    assert action_steps == 1, "supplier refresh must be a one-click preview action"
    response = response_info.value
    assert response.status == 200
    preview = response.json()
    assert preview.get("success") is True
    assert preview.get("requested") == len(expected_ids)
    assert preview_posts[-1] == {"product_ids": expected_ids}
    assert [item.get("product_id") for item in preview.get("items", [])] == expected_ids
    review_token = preview.get("review_token")
    assert isinstance(review_token, str) and review_token
    dialog = page.get_by_role("dialog", name="Проверка обновления из поставщика")
    dialog.wait_for(state="visible")
    dialog.locator(".mp-supplier-refresh-item").first.wait_for(state="visible")
    return dialog, preview


def assert_supplier_refresh_token_applied(preview, apply_tokens):
    """Compare capabilities in memory without adding them to reports or posts."""
    assert apply_tokens
    assert apply_tokens[-1] == preview["review_token"]


def filter_bulk_results(page):
    with page.expect_response(lambda response: feed_matches(response, {"has_photos": "yes"})) as photos:
        page.get_by_label("Фото").select_option("yes")
    assert photos.value.status == 200
    with page.expect_response(lambda response: feed_matches(response, {
            "has_photos": "yes", "sort": "title",
    })) as sorting:
        page.get_by_label("Сортировка").select_option("title")
    assert sorting.value.status == 200
    with page.expect_response(lambda response: feed_matches(response, {
            "search": "UX-BULK-OWNED", "has_photos": "yes", "sort": "title",
    })) as searching:
        page.get_by_placeholder("Название, бренд или артикул").fill("UX-BULK-OWNED")
    assert searching.value.status == 200


def run():
    fixture_ids = seed_bulk_selection()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks = []
    layouts = []
    posts = []
    refresh_preview_posts = []
    refresh_apply_tokens = []
    wb_enrich_start_posts = []
    wb_bulk_edit_resolver_posts = []
    wb_bulk_edit_resolver_results = []
    wb_bulk_edit_editor_posts = []
    wb_bulk_edit_photo_requests = []
    wb_bulk_edit_expected = {"source_ids": None, "wb_ids": None, "photo_urls": []}
    wb_bulk_edit_handoff = None
    price_create_posts = []
    price_create_receipts = []
    price_create_attempts = []
    price_create_server_results = []
    lost_price_create_response = {
        "armed": False, "body": None, "body_text": None,
        "receipt": None, "retry_seen": False,
    }
    price_preview_posts = []
    price_preview_responses = []
    price_decisions = []
    errors = []
    feed_gets = []
    feed_responses = []
    bulk_photo_ids = set(fixture_ids["bulk_ids"])
    price_review_scope = {"product_ids": [fixture_ids["wb_id"]]}
    latest_price_preview = {"body": None, "token": None, "response": None}
    delay_next_full_select = False
    server_delay_next_full_select = False
    override_over_limit_initial = False
    over_limit_initial_total = 201
    mock_over_limit_full = False

    def remember_feed_response(query, status, body):
        pagination = body.get("pagination") if isinstance(body, dict) else None
        items = body.get("items") if isinstance(body, dict) else None
        feed_responses.append({
            "query": query,
            "status": status,
            "page": pagination.get("page") if isinstance(pagination, dict) else None,
            "per_page": pagination.get("per_page") if isinstance(pagination, dict) else None,
            "total": pagination.get("total") if isinstance(pagination, dict) else None,
            "has_next": pagination.get("has_next") if isinstance(pagination, dict) else None,
            "items": len(items) if isinstance(items, list) else None,
        })
    refresh_response_override = None
    refresh_response_status = 200
    refresh_failure_ids = set()
    refresh_failure_injections = []

    def bridge(route):
        nonlocal refresh_response_override, refresh_response_status
        nonlocal delay_next_full_select, server_delay_next_full_select
        nonlocal override_over_limit_initial, over_limit_initial_total, mock_over_limit_full
        request = route.request
        parsed = urlsplit(request.url)
        if (request.method == "GET" and request.resource_type == "image"
                and request.url in wb_bulk_edit_expected["photo_urls"]):
            wb_bulk_edit_photo_requests.append(request.url)
            route.fulfill(status=200, body=WB_THUMBNAIL_GIF, content_type="image/gif")
            return
        if parsed.netloc == urlsplit(fixture.BASE).netloc and request.method == "GET":
            if parsed.path == "/api/my-products":
                query = parse_qs(parsed.query, keep_blank_values=True)
                # The signed import-batch token is a short-lived capability.
                # Keep it out of diagnostics and persisted runner evidence.
                safe_query = {
                    key: value for key, value in query.items()
                    if key != "import_batch"
                }
                feed_gets.append(safe_query)
                if delay_next_full_select and query.get("per_page") == ["200"]:
                    delay_next_full_select = False
                    server_delay_next_full_select = True
                    route.continue_()
                    return
                if (override_over_limit_initial and query.get("search") == ["UX-BULK-OWNED"]
                        and query.get("per_page") == ["50"]):
                    override_over_limit_initial = False
                    response = route.fetch(max_redirects=0)
                    body = response.json()
                    body["pagination"]["total"] = over_limit_initial_total
                    body["pagination"]["has_next"] = True
                    remember_feed_response(safe_query, response.status, body)
                    route.fulfill(status=response.status, content_type="application/json",
                                  body=json.dumps(body))
                    return
                if (mock_over_limit_full and query.get("search") == ["UX-BULK-OWNED"]
                        and query.get("per_page") == ["200"]):
                    mock_over_limit_full = False
                    response = route.fetch(max_redirects=0)
                    body = response.json()
                    source_items = body["items"]
                    for index in range(len(source_items), 200):
                        extra = dict(source_items[index % len(source_items)])
                        extra["id"] = 900000000 + index
                        extra["title"] = f"Synthetic overflow item {index}"
                        extra["vendor_code"] = f"Synthetic overflow {index}"
                        body["items"].append(extra)
                    body["pagination"].update({
                        "total": 201, "pages": 2, "per_page": 200,
                        "has_next": True,
                    })
                    remember_feed_response(safe_query, response.status, body)
                    route.fulfill(status=response.status, content_type="application/json",
                                  body=json.dumps(body))
                    return
                if parsed.path == "/api/my-products":
                    response = route.fetch(max_redirects=0)
                    try:
                        body = response.json()
                    except Exception:
                        body = None
                    remember_feed_response(safe_query, response.status, body)
                    if response.status >= 400:
                        fixture.REPORT["unexpected_http"].append({
                            "path": parsed.path, "status": response.status,
                        })
                    route.fulfill(response=response)
                    return
            if parsed.path.startswith("/api/photos/imported-product/"):
                path_parts = parsed.path.rstrip("/").split("/")
                try:
                    photo_product_id = int(path_parts[-2])
                except (IndexError, ValueError):
                    photo_product_id = None
                if photo_product_id in bulk_photo_ids:
                    route.fulfill(status=204, body=b"")
                    return
        if parsed.netloc == urlsplit(fixture.BASE).netloc and request.method == "POST":
            if parsed.path == LOCAL_WB_SELECTION_RESOLVE:
                body = request.post_data_json
                if not isinstance(body, dict) or body.get("mode") != "workspace":
                    route.fulfill(response=route.fetch(max_redirects=0))
                    return
                expected_source_ids = wb_bulk_edit_expected["source_ids"]
                expected_wb_ids = wb_bulk_edit_expected["wb_ids"]
                assert isinstance(body, dict) and set(body) == {
                    "mode", "ids", "wb_ids", "return_to",
                }, body
                assert body["mode"] == "workspace"
                assert body["ids"] == expected_source_ids, body
                assert body["wb_ids"] == expected_wb_ids, body
                return_parts = urlsplit(body["return_to"])
                assert return_parts.path == "/my-products/beta"
                assert return_parts.query == ""
                assert re.fullmatch(r"workspace-return=[a-f0-9]{32}", return_parts.fragment)
                wb_bulk_edit_resolver_posts.append({
                    "ids": body["ids"],
                    "wb_ids": body["wb_ids"],
                    "return_to": body["return_to"],
                })
                response = route.fetch(max_redirects=0)
                assert response.status == 200, (response.status, response.text()[:300])
                resolved = response.json()
                assert resolved.get("marketplace") == "wb"
                assert resolved.get("count") == len(expected_wb_ids)
                assert resolved.get("ids") == expected_wb_ids, resolved
                selection_token = resolved.get("selection_token")
                assert isinstance(selection_token, str) and 0 < len(selection_token) <= 24 * 1024
                wb_bulk_edit_resolver_results.append({
                    "status": response.status,
                    "count": resolved["count"],
                    "ids": resolved["ids"],
                    "selection_token_length": len(selection_token),
                })
                route.fulfill(response=response)
                return
            if parsed.path == LOCAL_WB_BULK_EDIT:
                values = parse_qs(request.post_data or "", keep_blank_values=True)
                assert set(values) == {"csrf_token", "selection_token"}, values.keys()
                assert len(values["csrf_token"]) == 1 and values["csrf_token"][0]
                assert len(values["selection_token"]) == 1
                token = values["selection_token"][0]
                assert token and len(token) <= 24 * 1024
                response = route.fetch(max_redirects=0)
                assert response.status == 200, (response.status, response.text()[:300])
                assert "Массовое редактирование" in response.text()
                wb_bulk_edit_editor_posts.append({
                    "status": response.status,
                    "field_names": sorted(values),
                    "selection_token_present": True,
                    "selection_token_length": len(token),
                })
                route.fulfill(response=response)
                return
            if parsed.path == LOCAL_PRICE_PREVIEW:
                body = request.post_data_json
                if not isinstance(body, dict) or set(body) != {
                    "reviewed_write", "product_ids", "change_type", "change_value",
                    "formula", "formula_variables", "price_source", "exact_selection",
                }:
                    raise AssertionError(("WB preview request shape changed", body))
                if body["reviewed_write"] is not True or body["product_ids"] != price_review_scope["product_ids"]:
                    raise AssertionError(("WB preview did not use the exact ordered selected IDs", body))
                if not isinstance(body["formula_variables"], dict):
                    raise AssertionError("WB review omitted formula variable object")
                price_preview_posts.append({
                    "reviewed_write": body["reviewed_write"],
                    "product_ids": body["product_ids"],
                    "change_type": body["change_type"],
                    "change_value": body["change_value"],
                    "price_source": body["price_source"],
                    "exact_selection": body["exact_selection"],
                })
                response = route.fetch(max_redirects=0)
                if response.status != 200:
                    raise AssertionError(("Authoritative WB preview failed", response.status,
                                          response.text()[:300]))
                preview = response.json()
                token = preview.get("review_token")
                if (preview.get("success") is not True or preview.get("reviewed") is not True
                        or not isinstance(token, str) or len(token) < 40 or "." not in token):
                    raise AssertionError("WB preview returned no signed review receipt")
                if [row.get("product_id") for row in preview.get("items", [])] != body["product_ids"]:
                    raise AssertionError("WB preview row order differs from the reviewed selection")
                if preview.get("stats", {}).get("selected_count") != len(body["product_ids"]):
                    raise AssertionError("WB preview selected count differs from exact selection")
                latest_price_preview.update({
                    "body": body,
                    "token": token,
                    "response": preview,
                })
                price_preview_responses.append({
                    "status": response.status,
                    "selected_count": preview["stats"]["selected_count"],
                    "changed_count": preview["stats"]["changed_count"],
                    "no_op_count": preview["stats"]["no_op_count"],
                    "review_token_length": len(token),
                })
                route.fulfill(response=response)
                return
            if parsed.path == LOCAL_PRICE_CREATE:
                body = request.post_data_json
                if not isinstance(body, dict) or body.get("reviewed_write") is not True:
                    raise AssertionError("WB price batch omitted reviewed-write consent")
                if set(body) != {
                    "reviewed_write", "review_token", "product_ids", "change_type",
                    "change_value", "formula", "formula_variables", "name",
                    "description", "price_source", "exact_selection",
                }:
                    raise AssertionError("WB price batch sent unexpected fields")
                reviewed_input = latest_price_preview.get("body")
                if (not reviewed_input or body.get("review_token") != latest_price_preview.get("token")
                        or body.get("product_ids") != price_review_scope["product_ids"]
                        or body.get("product_ids") != reviewed_input.get("product_ids")):
                    raise AssertionError("WB save did not use the exact current signed preview")
                for field in (
                    "change_type", "change_value", "formula", "formula_variables",
                    "price_source", "exact_selection",
                ):
                    if body.get(field) != reviewed_input.get(field):
                        raise AssertionError(("WB save inputs differ from signed preview", field))
                response = route.fetch(max_redirects=0)
                attempt = {
                    "product_ids": body["product_ids"],
                    "reviewed_write": body["reviewed_write"],
                    "token_matches_last_preview": body["review_token"] == latest_price_preview["token"],
                    "status": response.status,
                }
                if response.status == 409:
                    try:
                        error = response.json()
                    except Exception:
                        error = {}
                    attempt["code"] = error.get("code")
                    price_create_attempts.append(attempt)
                    route.fulfill(response=response)
                    return
                if response.status != 200:
                    raise AssertionError(("Actual WB price batch create failed", response.status,
                                          response.text()[:300]))
                receipt = response.json()
                if receipt.get("success") is not True or not isinstance(receipt.get("batch_id"), int):
                    raise AssertionError("WB price create returned no exact batch receipt")
                attempt["batch_id"] = receipt["batch_id"]
                attempt["replayed"] = receipt.get("replayed") is True
                attempt["status"] = response.status
                price_create_attempts.append(attempt)
                price_create_server_results.append({
                    "status": response.status,
                    "batch_id": receipt["batch_id"],
                    "selected_count": receipt.get("selected_count"),
                    "changed_count": receipt.get("changed_count"),
                    "batch_status": receipt.get("status"),
                    "redirect": receipt.get("redirect"),
                    "stats": receipt.get("stats"),
                    "replayed": receipt.get("replayed") is True,
                })
                if lost_price_create_response["armed"]:
                    if lost_price_create_response["body"] is not None:
                        raise AssertionError("Only one simulated lost price response is supported")
                    lost_price_create_response.update({
                        "armed": False,
                        "body": json.loads(json.dumps(body)),
                        "body_text": request.post_data,
                        "receipt": receipt,
                    })
                    route.abort("failed")
                    return
                if (lost_price_create_response["body"] is not None
                        and not lost_price_create_response["retry_seen"]):
                    if body != lost_price_create_response["body"]:
                        raise AssertionError("WB retry changed the immutable reviewed request body")
                    if request.post_data != lost_price_create_response["body_text"]:
                        raise AssertionError("WB retry did not replay the exact original JSON body")
                    lost_price_create_response["retry_seen"] = True
                price_create_posts.append({
                    "product_ids": body["product_ids"],
                    "reviewed_write": body["reviewed_write"],
                    "exact_selection": body["exact_selection"],
                    "change_type": body["change_type"],
                    "change_value": body["change_value"],
                })
                price_create_receipts.append(receipt)
                route.fulfill(response=response)
                return
            if parsed.path.startswith("/prices/batch/") and parsed.path.endswith("/confirm"):
                match = re.fullmatch(r"/prices/batch/(\d+)/confirm", parsed.path)
                values = parse_qs(request.post_data or "", keep_blank_values=True)
                action = values.get("action", [""])[0]
                nonce_values = values.get("workspace_return", [])
                if not match or action not in {"confirm", "reject"} or len(nonce_values) != 1:
                    raise AssertionError("WB dangerous-price decision omitted its result-bound return marker")
                price_decisions.append({
                    "batch_id": int(match.group(1)),
                    "action": action,
                    "workspace_return": nonce_values[0],
                })
                response = route.fetch(max_redirects=0)
                if response.status not in {302, 303}:
                    raise AssertionError(("Actual WB decision did not redirect", response.status))
                route.fulfill(response=response)
                return
            if parsed.path == LOCAL_WB_ENRICH_START:
                wb_enrich_start_posts.append(request.post_data_json)
                route.fulfill(status=409, content_type="application/json",
                              body=json.dumps({"error": "synthetic browser guard"}))
                return
            assert parsed.path in (LOCAL_REFRESH, LOCAL_REFRESH_PREVIEW, LOCAL_OZON_PREPARE,
                                   LOCAL_WB_ENRICH_REVIEW, LOCAL_SUPPLIER_IMPORT), parsed.path
            if parsed.path in (LOCAL_WB_ENRICH_REVIEW, LOCAL_SUPPLIER_IMPORT):
                values = parse_qs(request.post_data or "")
                assert values.pop("csrf_token", []), parsed.path
            else:
                values = request.post_data_json
            if parsed.path == LOCAL_REFRESH_PREVIEW:
                assert set(values) == {"product_ids"}
                refresh_preview_posts.append({"product_ids": values["product_ids"]})
                route.fulfill(response=route.fetch(max_redirects=0))
                return
            if parsed.path == LOCAL_REFRESH:
                assert set(values) == {"product_ids", "review_token"}
                refresh_apply_tokens.append(values["review_token"])
                # Review capabilities stay in memory and are excluded from
                # mutation diagnostics, which are also used as test evidence.
                safe_values = {"product_ids": values["product_ids"]}
                posts.append({"path": parsed.path, "body": safe_values})
            else:
                posts.append({"path": parsed.path, "body": values})
            if parsed.path == LOCAL_REFRESH and refresh_response_override is not None:
                response_body = refresh_response_override
                response_status = refresh_response_status
                refresh_response_override = None
                refresh_response_status = 200
                route.fulfill(status=response_status, content_type="application/json",
                              body=json.dumps(response_body))
            elif parsed.path == LOCAL_REFRESH and refresh_failure_ids:
                # Fail one exact local service row inside its savepoint. Keep
                # the real review/apply/result routes so the response carries
                # the server-signed receipt; no provider client is involved.
                from services import supplier_service

                original_update = supplier_service._update_imported_from_supplier

                def fail_selected_local_row(imported_product, supplier_product):
                    if imported_product.id in refresh_failure_ids:
                        refresh_failure_injections.append(imported_product.id)
                        raise RuntimeError("synthetic isolated browser row failure")
                    return original_update(imported_product, supplier_product)

                with patch(
                    "services.supplier_service._update_imported_from_supplier",
                    side_effect=fail_selected_local_row,
                ):
                    route.fulfill(response=route.fetch(max_redirects=0))
            else:
                route.fulfill(response=route.fetch(max_redirects=0))
            return
        fixture.bridge(route)

    def delay_full_select_response():
        nonlocal server_delay_next_full_select
        from flask import request
        if (server_delay_next_full_select and request.path == "/api/my-products"
                and request.args.get("per_page") == "200"):
            server_delay_next_full_select = False
            time.sleep(0.5)

    fixture.app.before_request_funcs.setdefault(None, []).append(delay_full_select_response)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=fixture.CHROMIUM, headless=True,
                                              args=["--no-sandbox", "--disable-dev-shm-usage"])
        for width, theme in ((320, "light"), (390, "dark"), (1440, "light"), (1440, "dark")):
            context = browser.new_context(viewport={"width": width, "height": 950})
            context.add_init_script(f"localStorage.setItem('sh-theme', '{theme}');")
            context.add_cookies([fixture.synthetic_login_cookie()])
            context.route("**/*", bridge)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            open_products(page)
            import_link = page.get_by_role("link", name="Импортировать товары")
            import_target = urlsplit(import_link.get_attribute("href") or "")
            assert import_target.path == "/supplier-catalog"
            assert parse_qs(import_target.query) == {"action": ["import"]}
            checks.append("main_import_entry_uses_import_intent")
            if width == 320 and theme == "light":
                with page.expect_navigation(wait_until="domcontentloaded") as import_navigation:
                    import_link.click()
                import_response = import_navigation.value
                assert import_response and import_response.status == 200
                assert urlsplit(page.url).path == (
                    f"/supplier-catalog/{fixture.FIXTURE['supplier_id']}/products"
                ), page.url
                checks.append("main_import_entry_skips_single_connected_supplier_list")
                page.goto(fixture.BASE + "/my-products/beta", wait_until="domcontentloaded")
                page.locator("#my-products-app").wait_for(state="visible")
                page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").wait_for()
            other_sections = page.locator(".mp-head-more summary")
            other_sections.click()
            ozon_prices = page.get_by_role("link", name="Ценовые заявки Ozon").get_attribute("href")
            assert parse_qs(urlsplit(ozon_prices).query) == {
                "proposal_kind": ["price"], "status": ["pending_review"],
                "account_id": [str(fixture.FIXTURE["account_id"])],
            }
            other_sections.click()
            page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").check()
            cards = workspace_action_launcher(page, "cards")
            assert cards.get_attribute("data-bulk-direct-action") is None
            cards.focus()
            cards.press("Enter")
            card_choices = page.locator("#mp-bulk-panel-cards")
            card_choices.wait_for()
            common_option = card_choices.locator('[data-bulk-action-key="common_content"]')
            wb_option = card_choices.locator('[data-bulk-action-key="wb_edit"]')
            assert common_option.is_enabled()
            assert wb_option.is_enabled()
            common_bounds = common_option.bounding_box()
            assert common_bounds and common_bounds["height"] >= 44, common_bounds
            choice_keys = card_choices.locator("[data-bulk-action-key]").evaluate_all(
                "items => items.map(item => item.dataset.bulkActionKey)"
            )
            assert len(choice_keys) >= 2 and set(choice_keys) <= {
                "common_content", "wb_edit", "ozon_workspace_update",
            }, choice_keys
            cards.press("Tab")
            assert common_option.evaluate("element => element === document.activeElement")
            verify_bounds(page)
            choices_bounds = card_choices.bounding_box()
            assert choices_bounds and choices_bounds["x"] >= 0 \
                and choices_bounds["x"] + choices_bounds["width"] <= width, choices_bounds
            screenshot = OUTPUT / f"selected-{theme}-{width}.png"
            page.screenshot(path=screenshot, animations="disabled")
            common_option.press("Escape")
            assert cards.evaluate("element => element === document.activeElement")
            more = page.locator(".mp-more-actions summary")
            more.press("Enter")
            secondary = page.locator(".mp-more-actions-menu")
            secondary.wait_for()
            bounds = secondary.bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width, bounds
            if width <= 390:
                launchers = page.locator("[data-bulk-action-group]").evaluate_all(
                    "items => items.map(item => { const r = item.getBoundingClientRect(); return {w:r.width,h:r.height,left:r.left,right:r.right}; })"
                )
                assert len(launchers) == 4 and all(
                    bounds["w"] >= 44 and bounds["h"] >= 44 and
                    bounds["left"] >= 0 and bounds["right"] <= width
                    for bounds in launchers
                ), launchers
                assert more.bounding_box()["height"] >= 44
            cards.press("Enter")
            card_choices.wait_for()
            assert not more.evaluate("element => element.parentElement.open")
            cards.press("Escape")
            assert cards.evaluate("element => element === document.activeElement")
            layouts.append({"width": width, "theme": theme, "screenshot": screenshot.name})
            context.close()
        checks.append("intent_choice_keyboard_escape_focus_and_secondary_overlay_fit_all_layouts")

        # Hand the exact ordered workspace selection to the native WB editor,
        # then return through its signed same-tab URL and restore those marks.
        wb_context = browser.new_context(viewport={"width": 320, "height": 900})
        wb_context.add_cookies([fixture.synthetic_login_cookie()])
        wb_context.route("**/*", bridge)
        wb_page = wb_context.new_page()
        wb_page.on("pageerror", lambda error: errors.append(str(error)))
        open_products(wb_page)
        source_owner = wb_page.evaluate(
            "JSON.parse(document.querySelector('#mp-bootstrap').textContent).workspaceOwner",
        )
        search_input = wb_page.get_by_placeholder("Название, бренд или артикул")
        with wb_page.expect_response(lambda response: feed_matches(response, {
                "search": "Сравнение цен WB",
        })) as wb_handoff_filtered:
            search_input.fill("Сравнение цен WB")
        assert wb_handoff_filtered.value.status == 200
        with wb_page.expect_response(lambda response: feed_matches(response, {
                "search": "Сравнение цен WB", "sort": "title",
        })) as wb_handoff_sorted:
            wb_page.get_by_label("Сортировка").select_option("title")
        assert wb_handoff_sorted.value.status == 200
        expected_workspace_ids = [
            fixture_ids["preview_local_ids"][2],
            fixture_ids["preview_local_ids"][0],
        ]
        expected_editor_ids = [
            fixture_ids["preview_wb_ids"][2],
            fixture_ids["preview_wb_ids"][0],
        ]
        expected_photo_urls = [
            f"{WB_THUMBNAIL_ORIGIN}/vol{fixture_ids['preview_nm_ids'][index] // 100000}"
            f"/part{fixture_ids['preview_nm_ids'][index] // 1000}/"
            f"{fixture_ids['preview_nm_ids'][index]}/images/tm/1.webp"
            for index in (2, 0)
        ]
        wb_bulk_edit_expected.update({
            "source_ids": expected_workspace_ids,
            "wb_ids": expected_editor_ids,
            "photo_urls": expected_photo_urls,
        })
        for index in (2, 0):
            wb_page.get_by_role(
                "checkbox", name=f"Выбрать {fixture_ids['preview_titles'][index]}", exact=True,
            ).check()
        assert selected_browser_state(wb_page)["selected"] == expected_workspace_ids
        provider_attempts_before_handoff = fixture.REPORT["provider_attempts"]
        unexpected_external_before_handoff = len(
            fixture.REPORT["unexpected_external_requests"]
        )
        posts_before_handoff = len(posts)
        handoff_watch = begin_domain_write_watch()
        try:
            with wb_page.expect_response(
                lambda response: urlsplit(response.url).path == LOCAL_WB_SELECTION_RESOLVE
                and response.request.method == "POST",
                timeout=10000,
            ) as wb_resolved:
                pick_action(wb_page, "Изменить карточки WB")
            assert wb_resolved.value.status == 200
            wb_page.wait_for_url(
                lambda value: urlsplit(str(value)).path == LOCAL_WB_BULK_EDIT,
                wait_until="domcontentloaded",
            )
            wb_page.get_by_role("heading", name="Массовое редактирование", exact=True).wait_for()
            assert "2 товаров" in wb_page.locator("body").inner_text()
            resolver = wb_bulk_edit_resolver_posts[-1]
            resolver_result = wb_bulk_edit_resolver_results[-1]
            return_parts = urlsplit(resolver["return_to"])
            return_hash = "#" + return_parts.fragment
            capsule_key = "seller-hub-common-content-workspace-return-v1"
            capsule = wb_page.evaluate(
                "key => JSON.parse(sessionStorage.getItem(key))", capsule_key,
            )
            assert capsule["v"] == 5
            assert capsule["owner"] == source_owner
            assert capsule["batch"] is None
            assert capsule["selectionIds"] == expected_workspace_ids
            assert capsule["wbEditTarget"] == {
                "accountId": None,
                "productIds": expected_editor_ids,
            }
            assert capsule["nonce"] == return_parts.fragment.split("=", 1)[1]
            assert wb_bulk_edit_editor_posts[-1]["field_names"] == [
                "csrf_token", "selection_token",
            ]
            assert wb_bulk_edit_editor_posts[-1]["selection_token_present"] is True
            editor_return = wb_page.locator(f'nav a[href="{resolver["return_to"]}"]')
            assert editor_return.count() == 1
            verify_bounds(wb_page)
            editor_screenshot = OUTPUT / "wb-bulk-from-workspace-editor-320.png"
            wb_page.screenshot(path=editor_screenshot, animations="disabled")
        finally:
            handoff_writes = end_domain_write_watch(handoff_watch)
        assert handoff_writes == [], handoff_writes
        assert len(posts) == posts_before_handoff
        assert fixture.REPORT["provider_attempts"] == provider_attempts_before_handoff == 0
        assert sorted(wb_bulk_edit_photo_requests) == sorted(expected_photo_urls), {
            "synthetic_image_gets": wb_bulk_edit_photo_requests,
            "expected_image_gets": expected_photo_urls,
        }
        return_watch = begin_domain_write_watch()
        try:
            with wb_page.expect_response(lambda response: feed_matches(response, {
                    "search": "Сравнение цен WB", "sort": "title", "per_page": "50",
            })) as wb_handoff_returned:
                with wb_page.expect_navigation(wait_until="domcontentloaded"):
                    editor_return.click()
            assert wb_handoff_returned.value.status == 200
        finally:
            return_writes = end_domain_write_watch(return_watch)
        wb_page.wait_for_url(lambda value: urlsplit(str(value)).path == "/my-products/beta")
        wb_page.wait_for_function("""({ids, search}) => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy && !proxy.loading && proxy.filters.search === search &&
                proxy.filters.sort === 'title' && proxy.selected.join(',') === ids.join(',');
        }""", arg={"ids": expected_workspace_ids, "search": "Сравнение цен WB"})
        assert urlsplit(wb_page.url).fragment == return_parts.fragment
        assert selected_browser_state(wb_page)["selected"] == expected_workspace_ids
        restored_scope = wb_page.evaluate("""() => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy ? {
                search: proxy.filters.search,
                sort: proxy.filters.sort,
                selected: proxy.selected.slice(),
            } : null;
        }""")
        assert restored_scope == {
            "search": "Сравнение цен WB",
            "sort": "title",
            "selected": expected_workspace_ids,
        }
        restored_rows = wb_page.locator(
            ".mp-list article.mp-row input[type=checkbox]:checked",
        ).evaluate_all("boxes => boxes.map(box => Number(box.value))")
        assert set(restored_rows) == set(expected_workspace_ids)
        assert len(restored_rows) == len(expected_workspace_ids)
        assert wb_page.evaluate("key => sessionStorage.getItem(key)", capsule_key) is None
        assert return_writes == [], return_writes
        assert fixture.REPORT["provider_attempts"] == provider_attempts_before_handoff == 0
        unexpected_external_handoff = fixture.REPORT["unexpected_external_requests"][
            unexpected_external_before_handoff:
        ]
        assert unexpected_external_handoff == [], {
            "unexpected_external_requests": unexpected_external_handoff,
            "synthetic_image_gets": wb_bulk_edit_photo_requests,
            "expected_image_gets": expected_photo_urls,
        }
        wb_bulk_edit_expected["photo_urls"] = []
        verify_bounds(wb_page)
        returned_screenshot = OUTPUT / "wb-bulk-from-workspace-returned-320.png"
        wb_page.screenshot(path=returned_screenshot, animations="disabled")
        wb_bulk_edit_handoff = {
            "selected_workspace_rows": len(expected_workspace_ids),
            "exact_mapped_editor_rows": resolver_result["count"],
            "resolver_calls": len(wb_bulk_edit_resolver_posts),
            "editor_open_posts": len(wb_bulk_edit_editor_posts),
            "restored_selected_rows": len(restored_rows),
            "ordered_mapping_matches": resolver_result["ids"] == expected_editor_ids,
            "return_scope_restored": restored_scope == {
                "search": "Сравнение цен WB",
                "sort": "title",
                "selected": expected_workspace_ids,
            } and urlsplit(wb_page.url).fragment == return_parts.fragment,
            "editor_form_fields": wb_bulk_edit_editor_posts[-1]["field_names"],
            "synthetic_image_gets": len(wb_bulk_edit_photo_requests),
            "local_db_write_statements": len(handoff_writes) + len(return_writes),
            "apply_posts": 0,
            "provider_attempts": fixture.REPORT["provider_attempts"] - provider_attempts_before_handoff,
            "screenshots": [editor_screenshot.name, returned_screenshot.name],
        }
        assert wb_bulk_edit_handoff["resolver_calls"] == 1
        assert wb_bulk_edit_handoff["editor_open_posts"] == 1
        assert wb_bulk_edit_handoff["ordered_mapping_matches"] is True
        assert wb_bulk_edit_handoff["restored_selected_rows"] == len(expected_workspace_ids)
        assert wb_bulk_edit_handoff["local_db_write_statements"] == 0
        assert wb_bulk_edit_handoff["apply_posts"] == 0
        assert wb_bulk_edit_handoff["provider_attempts"] == 0
        checks.append("wb_workspace_exact_order_handoff_opens_review_and_restores_exact_selection")
        wb_context.close()

        context = browser.new_context(viewport={"width": 320, "height": 900})
        context.add_cookies([fixture.synthetic_login_cookie()])
        context.route("**/*", bridge)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        open_products(page)
        filter_bulk_results(page)
        select_all = page.get_by_role("button", name=f"Выбрать все {len(fixture_ids['bulk_ids'])}")
        try:
            select_all.wait_for(timeout=5000)
        except Exception as error:
            diagnostics = page.evaluate("""() => ({
                search: document.querySelector('input[placeholder="Название, бренд или артикул"]')?.value,
                photos: document.querySelector('select[aria-label="Фото"]')?.value,
                sort: document.querySelector('select[aria-label="Сортировка"]')?.value,
                listhead: document.querySelector('.mp-listhead')?.innerText || '',
                fullSelectButtons: Array.from(document.querySelectorAll('.mp-select-filtered')).map(button => ({
                    text: button.innerText, disabled: button.disabled,
                })),
                visibleRows: document.querySelectorAll('.mp-list article.mp-row').length,
                footer: document.querySelector('.mp-foot-info')?.innerText || '',
                appState: (() => {
                    const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                    return proxy ? {
                        searchInput: proxy.searchInput,
                        filters: {...proxy.filters},
                        activeTab: proxy.activeTab,
                        pagination: {...proxy.pagination},
                        itemsScope: proxy.itemsScope,
                        currentScope: proxy.feedScopeKey(),
                        loading: proxy.loading,
                    } : null;
                })(),
            })""")
            raise AssertionError(json.dumps({
                "reason": "filtered select control did not appear",
                "actual": diagnostics,
                "feed_gets": feed_gets[-6:],
                "feed_responses": feed_responses[-6:],
                "pageerrors": errors[-6:],
            }, ensure_ascii=False)) from error
        verify_bounds(page)
        control_bounds = select_all.bounding_box()
        assert control_bounds and control_bounds["height"] >= 44 and control_bounds["width"] >= 44
        page.screenshot(path=OUTPUT / "filtered-select-light-320.png", animations="disabled")
        initial_ids = page.locator(".mp-list article.mp-row input[type=checkbox]").evaluate_all(
            "boxes => boxes.map(box => Number(box.value))"
        )
        expected_ids = fixture_ids["bulk_ids"]
        assert initial_ids == expected_ids[:50], (initial_ids[:3], expected_ids[:3], len(initial_ids))
        before_select_posts = len(posts)
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/my-products"
                                  and response.request.method == "GET"
                                  and parse_qs(urlsplit(response.url).query).get("per_page") == ["200"]
                                  and parse_qs(urlsplit(response.url).query).get("page") == ["1"]) as full_feed:
            select_all.click()
        assert full_feed.value.status == 200
        full_query = parse_qs(urlsplit(full_feed.value.url).query, keep_blank_values=True)
        assert full_query == {
            "search": ["UX-BULK-OWNED"], "has_photos": ["yes"],
            "sort": ["title"], "page": ["1"], "per_page": ["200"],
        }, full_query
        page.get_by_text(f"Показано {len(expected_ids)} из {len(expected_ids)}", exact=True).wait_for()
        selected_ids = page.locator(".mp-list article.mp-row input[type=checkbox]").evaluate_all(
            "boxes => boxes.filter(box => box.checked).map(box => Number(box.value))"
        )
        assert selected_ids == expected_ids, (selected_ids[:3], expected_ids[:3], len(selected_ids))
        assert page.locator(".mp-list article.mp-row").count() == len(expected_ids)
        row_boxes = page.locator(".mp-list article.mp-row input[type=checkbox]")
        for index in range(50, len(expected_ids)):
            row_boxes.nth(index).uncheck()
        selected_ids = page.locator(".mp-list article.mp-row input[type=checkbox]").evaluate_all(
            "boxes => boxes.filter(box => box.checked).map(box => Number(box.value))"
        )
        assert selected_ids == expected_ids[:50], (selected_ids[:3], expected_ids[:3], len(selected_ids))
        with page.expect_response(lambda response: urlsplit(response.url).path == "/my-products/common-content"
                                  and response.request.method == "GET") as content_review:
            pick_action(page, "Изменить общий контент")
        assert content_review.value.status == 200
        assert parse_qs(urlsplit(page.url).query)["product_id"] == [str(value) for value in expected_ids[:50]]
        assert len(posts) == before_select_posts, posts
        assert full_query in feed_gets
        assert len([query for query in feed_gets if query.get("per_page") == ["200"]]) == 1
        checks.append("filtered_full_select_uses_exact_owned_ordered_set_and_existing_action_data_without_writes")

        fixture.wait_document(page, "bulk_workspace_over_limit", "/my-products/beta")
        override_over_limit_initial = True
        over_limit_initial_total = 201
        filter_bulk_results(page)
        page.get_by_text("Показано 50 из 201", exact=True).wait_for()
        page.locator(".mp-select-limit").get_by_text("не больше 200 товаров", exact=False).wait_for()
        assert page.locator(".mp-select-filtered").count() == 0
        over_limit_requests_before = len([query for query in feed_gets
                                          if query.get("search") == ["UX-BULK-OWNED"]
                                          and query.get("per_page") == ["200"]])
        assert len(posts) == before_select_posts, posts
        checks.append("over_200_filtered_result_keeps_manual_pages_and_shows_selection_limit_without_full_read")

        override_over_limit_initial = True
        over_limit_initial_total = 200
        page.reload(wait_until="domcontentloaded")
        filter_bulk_results(page)
        overridden_select = page.get_by_role("button", name="Выбрать все 200")
        overridden_select.wait_for()
        mock_over_limit_full = True
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/my-products"
                                  and response.request.method == "GET"
                                  and parse_qs(urlsplit(response.url).query).get("per_page") == ["200"]) as over_limit_full:
            overridden_select.click()
        assert over_limit_full.value.status == 200
        over_limit_body = over_limit_full.value.json()
        assert over_limit_body["pagination"]["total"] == 201
        assert over_limit_body["pagination"]["has_next"] is True
        assert len(over_limit_body["items"]) == 200
        page.get_by_text("Список изменился или не загрузился целиком", exact=False).wait_for()
        assert page.locator(".mp-list article.mp-row").count() == 50
        assert page.locator(".mp-list article.mp-row input[type=checkbox]:checked").count() == 0
        assert len(posts) == before_select_posts, posts
        assert len([query for query in feed_gets
                    if query.get("search") == ["UX-BULK-OWNED"]
                    and query.get("per_page") == ["200"]]) == over_limit_requests_before + 1
        checks.append("full_response_over_200_is_rejected_without_selection_or_write")

        race_context = browser.new_context(viewport={"width": 1080, "height": 900})
        race_context.add_cookies([fixture.synthetic_login_cookie()])
        race_context.route("**/*", bridge)
        race_page = race_context.new_page()
        race_page.on("pageerror", lambda error: errors.append(str(error)))
        open_products(race_page)
        filter_bulk_results(race_page)
        race_page.get_by_role("button", name=f"Выбрать все {len(expected_ids)}").wait_for()
        delay_next_full_select = True
        race_page.evaluate("""() => {
            const button = document.querySelector('.mp-select-filtered');
            const search = document.querySelector('input[placeholder="Название, бренд или артикул"]');
            setTimeout(() => {
                search.value = 'UX-BULK-NO-MATCH';
                search.dispatchEvent(new Event('input', {bubbles: true}));
            }, 0);
            button.click();
        }""")
        race_page.wait_for_function("""() => {
            const search = document.querySelector('input[placeholder="Название, бренд или артикул"]');
            return search && search.value === 'UX-BULK-NO-MATCH';
        }""")
        race_page.get_by_text("По этим условиям товаров нет", exact=True).wait_for()
        race_page.wait_for_timeout(600)
        assert race_page.locator(".mp-list article.mp-row").count() == 0
        assert race_page.locator(".mp-list article.mp-row input[type=checkbox]:checked").count() == 0
        assert any(query.get("search") == ["UX-BULK-NO-MATCH"] for query in feed_gets)
        assert len(posts) == before_select_posts, posts
        checks.append("delayed_full_select_is_discarded_when_search_changes")
        race_context.close()
        context.close()

        context = browser.new_context(viewport={"width": 1080, "height": 900})
        context.add_cookies([fixture.synthetic_login_cookie()])
        context.route("**/*", bridge)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        open_products(page)
        source_owner = page.evaluate("JSON.parse(document.querySelector('#mp-bootstrap').textContent).workspaceOwner")
        with page.expect_response(lambda response: feed_matches(response, {"search": MAIN_TITLE})) as filtered_feed:
            page.get_by_placeholder("Название, бренд или артикул").fill(MAIN_TITLE)
        assert filtered_feed.value.status == 200
        with page.expect_response(lambda response: feed_matches(response, {
                "search": MAIN_TITLE, "sort": "title",
        })) as sorted_feed:
            page.get_by_label("Сортировка").select_option("title")
        assert sorted_feed.value.status == 200
        page.evaluate("localStorage.setItem('price_change_selected', '[999999]')")
        page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").check()
        assert selected_browser_state(page) == {
            "selected": [fixture_ids["local_id"]], "ozonIds": [],
        }
        price_intent_steps = pick_action(page, "Изменить цены WB")
        assert price_intent_steps == 1, \
            "a WB-only linked selection must open prices directly without a destination menu"
        page.wait_for_url(lambda value: (
            urlsplit(str(value)).path == "/prices/change"
            and re.fullmatch(r"workspace-return=[a-f0-9]{32}", urlsplit(str(value)).fragment)
        ))
        price_url = urlsplit(page.url)
        nonce_hash = price_url.fragment
        checks.append("sole_wb_price_intent_dispatches_without_choice_menu")
        assert parse_qs(price_url.query)["product_ids"] == [str(fixture_ids["wb_id"])]
        assert page.evaluate("""([owner, importedId, productId, search]) => {
            const capsule = window.SellerHubCommonContent.workspaceReturnFromHash(
                window.location.hash, {owner}
            );
            return !!capsule && capsule.v === 2 && capsule.owner === owner &&
                capsule.selectionIds.length === 1 && capsule.selectionIds[0] === importedId &&
                capsule.priceTarget.channel === 'wb' && capsule.priceTarget.accountId === null &&
                capsule.priceTarget.ids.length === 1 && capsule.priceTarget.ids[0] === productId &&
                capsule.scope.search === search;
        }""", [source_owner, fixture_ids["local_id"], fixture_ids["wb_id"], MAIN_TITLE])
        page.wait_for_function("""(id) => {
            const element = document.querySelector('[x-data^="priceChangeApp("]');
            return element && window.Alpine && Alpine.$data(element).selectedIds?.join(',') === String(id);
        }""", arg=fixture_ids["wb_id"])
        assert page.evaluate("localStorage.getItem('price_change_selected')") == '[999999]'
        checks.append("selected_wb_price_transfer_does_not_merge_or_overwrite_old_selection")
        pricing_form = page.locator('[x-data^="priceChangeApp("]')
        pricing_form.evaluate("""async element => {
            Alpine.$data(element).toggleMarketplacePrices();
            await Alpine.nextTick();
        }""")
        assert pricing_form.evaluate("element => Alpine.$data(element).useMarketplacePrices")
        assert page.get_by_role("button", name="Синхронизировать с WB").count() == 0
        checks.append("account_wide_price_sync_only_on_direct_price_route")

        return_link = page.get_by_role("link", name="Вернуться к товарам", exact=True)
        return_link.wait_for(state="visible")
        return_href = urlsplit(return_link.get_attribute("href") or "")
        assert return_href.path == "/my-products/beta" and not return_href.query
        assert return_href.fragment == nonce_hash
        assert return_link.inner_text().strip() == "Вернуться к товарам"
        with page.expect_response(lambda response: feed_matches(response, {
                "search": MAIN_TITLE, "sort": "title", "per_page": "50",
        })) as restored_feed:
            with page.expect_navigation(wait_until="domcontentloaded"):
                return_link.click()
        page.wait_for_url(lambda value: urlsplit(str(value)).path == "/my-products/beta")
        assert restored_feed.value.status == 200
        page.locator("#my-products-app").wait_for(state="visible")
        page.wait_for_function("""([id, search]) => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy && !proxy.loading && proxy.filters.search === search &&
                proxy.filters.sort === 'title' && proxy.selected.join(',') === String(id);
        }""", arg=[fixture_ids["local_id"], MAIN_TITLE])
        assert page.get_by_placeholder("Название, бренд или артикул").input_value() == MAIN_TITLE
        assert page.locator(".mp-list article.mp-row input[type=checkbox]:checked").evaluate_all(
            "boxes => boxes.map(box => Number(box.value))"
        ) == [fixture_ids["local_id"]]
        assert page.evaluate("localStorage.getItem('price_change_selected')") == '[999999]'
        checks.append("filtered_wb_price_return_restores_exact_owned_marks_and_scope")

        # Re-run the filtered action with an authorized WB product, then make
        # its ImportedProduct stop matching the captured search before return.
        # The fresh selector read must reject the stale mark as a whole.
        pick_action(page, "Изменить цены WB")
        page.wait_for_url(lambda value: (
            urlsplit(str(value)).path == "/prices/change"
            and re.fullmatch(r"workspace-return=[a-f0-9]{32}", urlsplit(str(value)).fragment)
        ))
        stale_return = page.get_by_role("link", name="Вернуться к товарам", exact=True)
        stale_return.wait_for(state="visible")
        stale_hash = urlsplit(page.url).fragment
        stale_href = urlsplit(stale_return.get_attribute("href") or "")
        assert stale_href.path == "/my-products/beta" and not stale_href.query
        assert stale_href.fragment == stale_hash
        with fixture.app.app_context():
            changed_source = db.session.get(ImportedProduct, fixture_ids["local_id"])
            changed_source.title = "Изменённый товар вне исходного поиска"
            db.session.commit()
        with page.expect_response(lambda response: feed_matches(response, {
                "search": MAIN_TITLE, "sort": "title", "per_page": "50",
        })) as stale_feed:
            with page.expect_navigation(wait_until="domcontentloaded"):
                stale_return.click()
        page.wait_for_url(lambda value: urlsplit(str(value)).path == "/my-products/beta")
        assert stale_feed.value.status == 200
        page.get_by_text("Список изменился. Проверьте его и отметьте товары заново.", exact=True).wait_for()
        page.wait_for_function("""search => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy && !proxy.loading && proxy.selected.length === 0 &&
                proxy.filters.search === search &&
                proxy.filters.sort === 'title';
        }""", arg=MAIN_TITLE)
        assert page.locator(".mp-list article.mp-row input[type=checkbox]:checked").count() == 0
        with fixture.app.app_context():
            changed_source = db.session.get(ImportedProduct, fixture_ids["local_id"])
            changed_source.title = MAIN_TITLE
            db.session.commit()
        checks.append("stale_wb_price_return_drops_all_marks_after_feed_change")
        fixture.wait_document(page, "direct_wb_prices", "/prices/change")
        pricing_form = page.locator('[x-data^="priceChangeApp("]')
        pricing_form.evaluate("element => Alpine.$data(element).toggleMarketplacePrices()")
        page.get_by_role("button", name="Синхронизировать с WB").wait_for()

        # A successful exact batch binds the existing tab capsule to one
        # seller-owned result. Its return is navigation-only and restores the
        # original ImportedProduct set after a fresh feed read.
        page.goto(fixture.BASE + "/my-products/beta")
        page.locator("#my-products-app").wait_for(state="visible")
        with page.expect_response(lambda response: feed_matches(response, {"search": MAIN_TITLE})) as price_search:
            page.get_by_placeholder("Название, бренд или артикул").fill(MAIN_TITLE)
        assert price_search.value.status == 200
        with page.expect_response(lambda response: feed_matches(response, {
                "search": MAIN_TITLE, "sort": "title",
        })) as price_sort:
            page.get_by_label("Сортировка").select_option("title")
        assert price_sort.value.status == 200
        page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").check()

        def open_exact_wb_price_form():
            page.wait_for_function("""(id) => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy && !proxy.loading && proxy.selected.join(',') === String(id);
            }""", arg=fixture_ids["local_id"])
            assert pick_action(page, "Изменить цены WB") == 1, \
                "WB-only price selections should dispatch in one click"
            page.wait_for_url(lambda value: (
                urlsplit(str(value)).path == "/prices/change"
                and re.fullmatch(r"workspace-return=[a-f0-9]{32}", urlsplit(str(value)).fragment)
            ))
            assert parse_qs(urlsplit(page.url).query).get("product_ids") == [str(fixture_ids["wb_id"])]
            return urlsplit(page.url).fragment

        def calculate_price_preview(change_type, change_value):
            page.locator(f'input[type="radio"][value="{change_type}"]').check()
            page.locator("#change_value").fill(str(change_value))
            preview_posts_before = len(price_preview_posts)
            successful_creates_before = len(price_create_posts)
            create_attempts_before = len(price_create_attempts)
            decisions_before = len(price_decisions)
            provider_attempts_before = fixture.REPORT["provider_attempts"]
            watch = begin_domain_write_watch()
            domain_writes = None
            try:
                with page.expect_response(lambda response: (
                    urlsplit(response.url).path == LOCAL_PRICE_PREVIEW
                    and response.request.method == "POST"
                )) as preview_response_info:
                    page.locator("[data-price-review-run]").click()
                domain_writes = end_domain_write_watch(watch)
            finally:
                if domain_writes is None:
                    end_domain_write_watch(watch)
            response = preview_response_info.value
            assert response.status == 200
            preview = response.json()
            assert preview.get("success") is True and preview.get("reviewed") is True
            assert isinstance(preview.get("review_token"), str) and len(preview["review_token"]) >= 40
            assert price_preview_posts[-1]["product_ids"] == price_review_scope["product_ids"]
            assert price_preview_posts[-1]["reviewed_write"] is True
            assert len(price_preview_posts) == preview_posts_before + 1
            assert len(price_create_posts) == successful_creates_before
            assert len(price_create_attempts) == create_attempts_before
            assert len(price_decisions) == decisions_before
            assert domain_writes == [], domain_writes
            assert fixture.REPORT["provider_attempts"] == provider_attempts_before == 0
            assert page.locator("[data-price-review-table]").is_visible()
            assert page.locator("[data-price-review-row]").count() == len(price_review_scope["product_ids"])
            save_button = page.locator("[data-price-reviewed-save]")
            assert save_button.is_enabled() is bool(preview.get("can_create"))
            return preview, response

        def create_exact_wb_batch(change_type, change_value):
            nonce_hash = open_exact_wb_price_form()
            preview, _preview_response = calculate_price_preview(change_type, change_value)
            assert len(preview["items"]) == 1
            reviewed_row = preview["items"][0]
            assert reviewed_row["product_id"] == fixture_ids["wb_id"]
            assert reviewed_row["status"] == "changed"
            assert reviewed_row["safety_level"] == {
                5: "safe", 15: "warning", 25: "dangerous",
            }[change_value]
            assert page.locator("[data-price-review-row]").get_attribute("data-product-id") == str(
                fixture_ids["wb_id"]
            )
            assert page.locator("[data-price-review-status]").inner_text() == {
                "safe": "Изменится", "warning": "Проверить", "dangerous": "Опасно",
            }[reviewed_row["safety_level"]]
            posts_before = len(price_create_posts)
            receipts_before = len(price_create_receipts)
            with page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_PRICE_CREATE
                                      and response.request.method == "POST") as create_response:
                page.locator("[data-price-reviewed-save]").click()
            response = create_response.value
            assert response.status == 200
            assert response.request.method == "POST"
            assert urlsplit(response.url).path == LOCAL_PRICE_CREATE
            assert len(price_create_posts) == posts_before + 1
            assert len(price_create_receipts) == receipts_before + 1
            assert len(price_create_posts) == len(price_create_receipts)
            assert price_create_posts[-1] == {
                "product_ids": [fixture_ids["wb_id"]],
                "reviewed_write": True,
                "exact_selection": True,
                "change_type": change_type,
                "change_value": change_value,
            }, price_create_posts[-1]
            # route.fetch already read and validated the real Flask JSON body;
            # the browser response can be retired when the redirect navigates.
            receipt = price_create_receipts[-1]
            assert receipt.get("success") is True
            batch_id = receipt.get("batch_id")
            assert isinstance(batch_id, int) and batch_id > 0
            page.wait_for_url(lambda value: (
                urlsplit(str(value)).path in {
                    f"/prices/batch/{batch_id}",
                    f"/prices/batch/{batch_id}/confirm",
                }
                and urlsplit(str(value)).fragment == nonce_hash
            ))
            return batch_id, nonce_hash, receipt

        def assert_exact_result_capsule(batch_id, nonce_hash):
            assert page.evaluate("""([owner, expectedBatch, sourceId]) => {
                const capsule = window.SellerHubCommonContent.workspaceReturnFromHash(
                    window.location.hash, {owner}
                );
                return !!capsule && capsule.v === 3 && capsule.owner === owner &&
                    capsule.priceResult.channel === 'wb' &&
                    capsule.priceResult.batchId === expectedBatch &&
                    capsule.selectionIds.length === 1 &&
                    capsule.selectionIds[0] === sourceId;
            }""", [source_owner, batch_id, fixture_ids["local_id"]])
            assert urlsplit(page.url).fragment == nonce_hash
            return_link = page.locator("a[data-price-workspace-return]")
            return_link.wait_for(state="visible")
            href = urlsplit(return_link.get_attribute("href") or "")
            assert href.path == "/my-products/beta" and href.fragment == nonce_hash
            assert return_link.inner_text().strip() == "Вернуться к товарам"
            return return_link

        def return_from_exact_wb_result(return_link, nonce_hash):
            posts_before = (len(price_create_posts), len(price_decisions), len(posts))
            provider_attempts_before = fixture.REPORT["provider_attempts"]
            watch = begin_domain_write_watch()
            try:
                with page.expect_response(lambda response: feed_matches(response, {
                        "search": MAIN_TITLE, "sort": "title", "per_page": "50",
                })) as returned:
                    with page.expect_navigation(wait_until="domcontentloaded"):
                        return_link.click()
                assert returned.value.status == 200
            finally:
                domain_writes = end_domain_write_watch(watch)
            page.wait_for_url(lambda value: urlsplit(str(value)).path == "/my-products/beta")
            page.wait_for_function("""([id, search]) => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy && !proxy.loading && proxy.filters.search === search &&
                    proxy.filters.sort === 'title' && proxy.selected.join(',') === String(id);
            }""", arg=[fixture_ids["local_id"], MAIN_TITLE])
            assert page.locator(".mp-list article.mp-row input[type=checkbox]:checked").evaluate_all(
                "boxes => boxes.map(box => Number(box.value))"
            ) == [fixture_ids["local_id"]]
            assert page.evaluate("localStorage.getItem('price_change_selected')") == '[999999]'
            assert domain_writes == [], domain_writes
            assert (len(price_create_posts), len(price_decisions), len(posts)) == posts_before
            assert fixture.REPORT["provider_attempts"] == provider_attempts_before == 0
            assert urlsplit(page.url).fragment == nonce_hash
            assert page.evaluate("key => sessionStorage.getItem(key)", capsule_key) is None
            assert page.evaluate("""([owner, nonce]) =>
                window.SellerHubCommonContent.workspaceReturnFromHash('#' + nonce, {owner}) === null
            """, [source_owner, nonce_hash.removeprefix("workspace-return=")])

        safe_batch_id, safe_hash, safe_receipt = create_exact_wb_batch("percent", 5)
        assert safe_receipt["status"] == "confirmed", safe_receipt
        assert page.get_by_text("Готово к применению", exact=True).is_visible()
        safe_row = page.locator("tbody tr").filter(has_text=MAIN_TITLE)
        assert safe_row.count() == 1
        assert safe_row.get_by_text("Безопасно", exact=True).is_visible()
        safe_return = assert_exact_result_capsule(safe_batch_id, safe_hash)
        assert len(price_create_posts) == len(price_create_receipts) == 1
        assert price_create_receipts[-1]["batch_id"] == safe_batch_id

        # A copied nonce on another owned batch is not enough to expose the
        # result return link. Probe it in a separate tab so the original
        # one-time tab capsule remains available for the real return.
        capsule_key = "seller-hub-common-content-workspace-return-v1"
        encoded_capsule = page.evaluate("key => sessionStorage.getItem(key)", capsule_key)
        assert encoded_capsule
        probe_page = context.new_page()
        probe_page.goto(fixture.BASE + "/my-products/beta")
        probe_page.locator("#my-products-app").wait_for(state="visible")
        probe_page.evaluate("([key, value]) => sessionStorage.setItem(key, value)",
                            [capsule_key, encoded_capsule])
        unrelated_watch = begin_domain_write_watch()
        try:
            probe_page.goto(
                fixture.BASE + f"/prices/batch/{fixture_ids['unrelated_price_batch_id']}#{safe_hash}"
            )
            probe_page.locator("h1").first.wait_for(state="visible")
        finally:
            unrelated_writes = end_domain_write_watch(unrelated_watch)
        assert probe_page.locator("a[data-price-workspace-return]:visible").count() == 0
        assert unrelated_writes == [], unrelated_writes
        probe_page.close()
        assert_exact_result_capsule(safe_batch_id, safe_hash)
        return_from_exact_wb_result(safe_return, safe_hash)
        checks.append("wb_exact_safe_batch_return_restores_source_rows_without_io_or_writes")
        checks.append("wb_same_nonce_on_unrelated_owned_batch_does_not_show_return")

        warning_batch_id, warning_hash, warning_receipt = create_exact_wb_batch("percent", 15)
        assert warning_receipt["status"] == "confirmed", warning_receipt
        assert warning_receipt["stats"]["warning"] == 1
        assert page.get_by_text("Готово к применению", exact=True).is_visible()
        warning_row = page.locator("tbody tr").filter(has_text=MAIN_TITLE)
        assert warning_row.get_by_text("Внимание", exact=True).is_visible()
        warning_return = assert_exact_result_capsule(warning_batch_id, warning_hash)
        return_from_exact_wb_result(warning_return, warning_hash)
        checks.append("wb_warning_price_batch_is_visible_and_returns_exact_selection")

        rejected_batch_id, rejected_hash, rejected_receipt = create_exact_wb_batch("percent", 25)
        assert rejected_receipt["status"] == "pending_review", rejected_receipt
        assert urlsplit(page.url).path == f"/prices/batch/{rejected_batch_id}/confirm"
        assert page.get_by_text("Требуется подтверждение опасных изменений цен", exact=True).is_visible()
        confirm_return = assert_exact_result_capsule(rejected_batch_id, rejected_hash)
        assert page.locator('input[name="workspace_return"]').input_value() == rejected_hash.removeprefix("workspace-return=")
        page.get_by_role("button", name="Отклонить изменения").click()
        page.wait_for_url(lambda value: (
            urlsplit(str(value)).path == f"/prices/batch/{rejected_batch_id}"
            and urlsplit(str(value)).fragment == rejected_hash
        ))
        assert page.get_by_text("Отменено", exact=True).is_visible()
        confirm_return = assert_exact_result_capsule(rejected_batch_id, rejected_hash)
        return_from_exact_wb_result(confirm_return, rejected_hash)
        assert price_decisions[-1]["action"] == "reject"
        checks.append("wb_dangerous_price_rejection_keeps_exact_return_context")

        accepted_batch_id, accepted_hash, accepted_receipt = create_exact_wb_batch("percent", 25)
        assert accepted_receipt["status"] == "pending_review", accepted_receipt
        assert urlsplit(page.url).path == f"/prices/batch/{accepted_batch_id}/confirm"
        page.locator("#comment").fill("Синтетическое основание подтверждения")
        page.locator('input[type="checkbox"][required]').check()
        page.get_by_role("button", name="Подтвердить изменения").click()
        page.wait_for_url(lambda value: (
            urlsplit(str(value)).path == f"/prices/batch/{accepted_batch_id}"
            and urlsplit(str(value)).fragment == accepted_hash
        ))
        assert page.get_by_text("Готово к применению", exact=True).is_visible()
        accepted_return = assert_exact_result_capsule(accepted_batch_id, accepted_hash)
        return_from_exact_wb_result(accepted_return, accepted_hash)
        assert [item["action"] for item in price_decisions] == ["reject", "confirm"]
        assert all(item["workspace_return"] for item in price_decisions)
        checks.append("wb_dangerous_price_confirmation_keeps_exact_return_context")

        # Compare three authoritative source cases in one exact selection:
        # a no-op WB price, a real numeric zero, and documented local fallback.
        open_products(page)
        with page.expect_response(lambda response: feed_matches(response, {
                "search": "Сравнение цен WB",
        })) as comparison_feed:
            page.get_by_placeholder("Название, бренд или артикул").fill("Сравнение цен WB")
        assert comparison_feed.value.status == 200
        assert page.locator(".mp-list article.mp-row").count() == 3
        for title in fixture_ids["preview_titles"]:
            page.get_by_role("checkbox", name=f"Выбрать {title}", exact=True).check()
        checked_local_ids = page.locator(".mp-list article.mp-row input[type=checkbox]:checked").evaluate_all(
            "boxes => boxes.map(box => Number(box.value))"
        )
        assert set(checked_local_ids) == set(fixture_ids["preview_local_ids"])
        assert selected_browser_state(page)["selected"] == fixture_ids["preview_local_ids"]
        pick_action(page, "Изменить цены WB")
        page.wait_for_url(lambda value: (
            urlsplit(str(value)).path == "/prices/change"
            and re.fullmatch(r"workspace-return=[a-f0-9]{32}", urlsplit(str(value)).fragment)
        ))
        comparison_hash = urlsplit(page.url).fragment
        comparison_product_ids = fixture_ids["preview_wb_ids"]
        assert parse_qs(urlsplit(page.url).query).get("product_ids") == [
            ",".join(str(value) for value in comparison_product_ids)
        ]
        price_review_scope["product_ids"] = comparison_product_ids
        page.wait_for_function("""ids => {
            const element = document.querySelector('[x-data^="priceChangeApp("]');
            return element && window.Alpine &&
                Alpine.$data(element).selectedIds.join(',') === ids.join(',');
        }""", arg=comparison_product_ids)
        comparison_form = page.locator('[x-data^="priceChangeApp("]')
        comparison_form.locator("label").filter(
            has_text="Цены с маркетплейса",
        ).locator("div.relative").click()
        assert comparison_form.evaluate("element => Alpine.$data(element).useMarketplacePrices")
        comparison_preview, _comparison_response = calculate_price_preview("set", 600)
        comparison_rows = comparison_preview["items"]
        assert [row["product_id"] for row in comparison_rows] == comparison_product_ids
        comparison_by_id = {row["product_id"]: row for row in comparison_rows}
        base_price_id, zero_price_id, fallback_price_id = comparison_product_ids
        assert comparison_preview["stats"]["selected_count"] == 3
        assert comparison_preview["stats"]["changed_count"] == 2
        assert comparison_preview["stats"]["no_op_count"] == 1
        assert comparison_preview["stats"]["skipped_count"] == 0
        assert comparison_preview["stats"]["invalid_count"] == 0
        assert comparison_preview["can_create"] is True
        assert (comparison_by_id[base_price_id]["source_kind"],
                comparison_by_id[base_price_id]["old_price"],
                comparison_by_id[base_price_id]["new_price"],
                comparison_by_id[base_price_id]["status"]) == (
                    "wb_seller_price", 600.0, 600, "no_op",
                )
        assert (comparison_by_id[zero_price_id]["source_kind"],
                comparison_by_id[zero_price_id]["old_price"],
                comparison_by_id[zero_price_id]["new_price"],
                comparison_by_id[zero_price_id]["status"]) == (
                    "wb_seller_price", 0.0, 600, "changed",
                )
        assert (comparison_by_id[fallback_price_id]["source_kind"],
                comparison_by_id[fallback_price_id]["old_price"],
                comparison_by_id[fallback_price_id]["new_price"],
                comparison_by_id[fallback_price_id]["status"]) == (
                    "local_fallback", 1200.0, 600, "changed",
                )
        comparison_table_rows = {
            int(row.get_attribute("data-product-id")): row
            for row in page.locator("[data-price-review-row]").all()
        }
        assert set(comparison_table_rows) == set(comparison_product_ids)
        base_cells = comparison_table_rows[base_price_id].locator("td").all_text_contents()
        zero_cells = comparison_table_rows[zero_price_id].locator("td").all_text_contents()
        fallback_cells = comparison_table_rows[fallback_price_id].locator("td").all_text_contents()
        assert "Цена продавца из WB" in base_cells[1] and "WB:" in base_cells[1]
        assert "Без изменения" in base_cells[4]
        assert "0 ₽" in zero_cells[2] and "Цена продавца из WB" in zero_cells[1]
        assert "Локальная цена каталога" in fallback_cells[1]
        assert "Дата источника не записана" in fallback_cells[1]
        assert "Без изменения" in comparison_table_rows[base_price_id].locator(
            "[data-price-review-status]"
        ).inner_text()
        assert len(price_create_posts) == len(price_create_receipts) == 4
        checks.append("wb_multirow_preview_compares_noop_zero_and_explicit_local_fallback")

        # At narrow width the exact result remains reachable by keyboard and
        # the explicit return stays readable inside the viewport.
        page.set_viewport_size({"width": 320, "height": 900})
        verify_bounds(page)
        review_return = page.locator("a[data-price-workspace-return]:visible").first
        assert review_return.inner_text().strip() == "Вернуться к товарам"
        return_bounds = review_return.bounding_box()
        assert return_bounds and return_bounds["height"] >= 44
        assert return_bounds["x"] >= 0 and return_bounds["x"] + return_bounds["width"] <= 320
        review_region = page.locator(
            '[role="region"][aria-label="Проверенные цены всех выбранных товаров"]'
        )
        run_button = page.locator("[data-price-review-run]")
        run_button.focus()
        page.keyboard.press("Tab")
        assert review_region.evaluate("element => element === document.activeElement")
        page.set_viewport_size({"width": 1080, "height": 900})
        checks.append("wb_review_and_return_remain_keyboard_accessible_at_320px")

        # Any edit to the reviewed rule invalidates the signed preview in the
        # browser before a save request can be sent.
        attempts_before_parameter_edit = len(price_create_attempts)
        page.locator("#change_value").fill("601")
        assert page.locator("[data-price-reviewed-save]").is_disabled()
        page.locator("[data-price-review-table]").wait_for(state="detached")
        assert len(price_create_attempts) == attempts_before_parameter_edit
        checks.append("wb_price_rule_edit_invalidates_preview_and_disables_save")

        # A signed preview also becomes stale when its authoritative WB price
        # changes in storage. The real Flask save route rejects it with 409 and
        # creates no batch; preview requests themselves remain read-only.
        refreshed_preview, _refreshed_response = calculate_price_preview("set", 600)
        assert refreshed_preview["review_token"]
        with fixture.app.app_context():
            stale_source = db.session.get(Product, base_price_id)
            original_wb_price = stale_source.wb_price
            stale_source.wb_price = 599
            db.session.commit()
            batches_before_stale_save = PriceChangeBatch.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
            ).count()
        stale_attempts_before = len(price_create_attempts)
        stale_create_posts_before = len(price_create_posts)
        stale_receipts_before = len(price_create_receipts)
        stale_provider_attempts_before = fixture.REPORT["provider_attempts"]
        stale_write_watch = begin_domain_write_watch()
        try:
            with page.expect_response(lambda response: (
                urlsplit(response.url).path == LOCAL_PRICE_CREATE
                and response.request.method == "POST"
            )) as stale_save_response_info:
                page.locator("[data-price-reviewed-save]").click()
            stale_save_writes = end_domain_write_watch(stale_write_watch)
        finally:
            if "stale_save_writes" not in locals():
                end_domain_write_watch(stale_write_watch)
        stale_save_response = stale_save_response_info.value
        assert stale_save_response.status == 409
        assert stale_save_response.json().get("code") == "review_stale"
        assert stale_save_writes == [], stale_save_writes
        assert len(price_create_attempts) == stale_attempts_before + 1
        assert price_create_attempts[-1]["code"] == "review_stale"
        assert price_create_attempts[-1]["token_matches_last_preview"] is True
        assert len(price_create_posts) == stale_create_posts_before
        assert len(price_create_receipts) == stale_receipts_before
        assert fixture.REPORT["provider_attempts"] == stale_provider_attempts_before == 0
        stale_error = page.locator("[data-price-review-error]")
        stale_error.wait_for(state="visible")
        assert "Цена, источник или настройки изменились" in stale_error.inner_text()
        page.wait_for_function("""() => {
            const save = document.querySelector('[data-price-reviewed-save]');
            return save && save.disabled && !document.querySelector('[data-price-review-table]');
        }""")
        with fixture.app.app_context():
            stale_source = db.session.get(Product, base_price_id)
            stale_source.wb_price = original_wb_price
            db.session.commit()
            assert PriceChangeBatch.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
            ).count() == batches_before_stale_save
        checks.append("wb_server_rejects_stale_review_token_without_batch_or_provider_write")

        # A committed request whose response is lost remains explicitly
        # retryable after reload. The retry must replay the same signed body
        # and return the original batch without a second insert.
        replay_preview, _replay_preview_response = calculate_price_preview("fixed", 5)
        assert replay_preview["stats"]["selected_count"] == 3
        assert replay_preview["stats"]["changed_count"] == 3
        assert replay_preview["stats"]["dangerous"] == 0
        assert replay_preview["can_create"] is True
        with fixture.app.app_context():
            replay_batches_before = PriceChangeBatch.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
            ).count()
        replay_attempts_before = len(price_create_attempts)
        replay_server_results_before = len(price_create_server_results)
        replay_ui_receipts_before = len(price_create_receipts)
        replay_ui_posts_before = len(price_create_posts)
        replay_provider_attempts_before = fixture.REPORT["provider_attempts"]
        lost_price_create_response["armed"] = True
        with page.expect_event("requestfailed", predicate=lambda request: (
            urlsplit(request.url).path == LOCAL_PRICE_CREATE
            and request.method == "POST"
        )):
            page.locator("[data-price-reviewed-save]").click()
        page.locator("[data-price-review-error]").wait_for(state="visible")
        page.locator("[data-price-reviewed-retry]").wait_for(state="visible")
        assert "Повторите тот же запрос" in page.locator(
            "[data-price-review-error]"
        ).inner_text()
        assert lost_price_create_response["body"] is not None
        assert len(price_create_attempts) == replay_attempts_before + 1
        assert len(price_create_server_results) == replay_server_results_before + 1
        first_commit = price_create_server_results[-1]
        lost_receipt = lost_price_create_response["receipt"]
        assert first_commit["status"] == 200
        assert first_commit["replayed"] is False
        assert first_commit["batch_id"] == lost_receipt["batch_id"]
        assert len(price_create_posts) == replay_ui_posts_before
        assert len(price_create_receipts) == replay_ui_receipts_before
        assert urlsplit(page.url).path == "/prices/change"
        with fixture.app.app_context():
            assert PriceChangeBatch.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
            ).count() == replay_batches_before + 1
            replay_batch = PriceChangeBatch.query.filter_by(
                id=lost_receipt["batch_id"],
                seller_id=fixture.FIXTURE["seller_id"],
            ).one()
            assert PriceChangeItem.query.filter_by(batch_id=replay_batch.id).count() == 3

        attempts_before_reload = len(price_create_attempts)
        server_results_before_reload = len(price_create_server_results)
        page.reload(wait_until="domcontentloaded")
        page.locator('[x-data^="priceChangeApp("]').wait_for(state="visible")
        page.locator("[data-price-reviewed-retry]").wait_for(state="visible")
        page.wait_for_function("""ids => {
            const element = document.querySelector('[x-data^="priceChangeApp("]');
            return element && window.Alpine &&
                Alpine.$data(element).selectedIds.join(',') === ids.join(',');
        }""", arg=comparison_product_ids)
        assert len(price_create_attempts) == attempts_before_reload
        assert len(price_create_server_results) == server_results_before_reload
        assert fixture.REPORT["provider_attempts"] == replay_provider_attempts_before == 0

        with page.expect_response(lambda response: (
            urlsplit(response.url).path == LOCAL_PRICE_CREATE
            and response.request.method == "POST"
        )) as recovered_save_response_info:
            page.locator("[data-price-reviewed-retry]").click()
        recovered_save_response = recovered_save_response_info.value
        assert recovered_save_response.status == 200
        # The browser navigates as soon as it consumes this response. Read the
        # already-captured Flask route receipt instead of asking Playwright for
        # response protocol data after that navigation.
        assert len(price_create_receipts) == replay_ui_receipts_before + 1
        replayed_receipt = price_create_receipts[-1]
        expected_batch_path = f"/prices/batch/{first_commit['batch_id']}"
        page.wait_for_url(lambda value: urlsplit(str(value)).path == expected_batch_path)
        page.get_by_text("Готово к применению", exact=True).wait_for(state="visible")
        assert replayed_receipt.get("replayed") is True
        assert lost_price_create_response["retry_seen"] is True
        assert len(price_create_attempts) == replay_attempts_before + 2
        assert price_create_attempts[-1]["status"] == 200
        assert price_create_attempts[-1]["replayed"] is True
        assert price_create_server_results[-1]["replayed"] is True
        assert price_create_server_results[-1]["batch_id"] == first_commit["batch_id"]
        for receipt_field in (
            "batch_id", "status", "redirect", "selected_count", "changed_count", "stats",
        ):
            assert replayed_receipt[receipt_field] == lost_receipt[receipt_field], receipt_field
        assert replayed_receipt["selected_count"] == 3
        assert replayed_receipt["changed_count"] == 3
        assert urlsplit(page.url).path == expected_batch_path
        assert page.locator("tbody tr").count() == 3
        replay_return = page.locator("a[data-price-workspace-return]:visible").first
        assert replay_return.inner_text().strip() == "Вернуться к товарам"
        assert urlsplit(replay_return.get_attribute("href") or "").fragment == comparison_hash
        assert len(price_create_posts) == replay_ui_posts_before + 1
        assert len(price_create_receipts) == replay_ui_receipts_before + 1
        assert price_create_receipts[-1]["batch_id"] == first_commit["batch_id"]
        assert fixture.REPORT["provider_attempts"] == replay_provider_attempts_before == 0
        with fixture.app.app_context():
            assert PriceChangeBatch.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
            ).count() == replay_batches_before + 1
            assert PriceChangeItem.query.filter_by(
                batch_id=first_commit["batch_id"],
            ).count() == 3
        checks.append("wb_lost_save_response_retries_same_body_after_reload_without_duplicate_batch")

        # Return to the catalogue through the exact selection receipt restored
        # by the replayed successful batch.
        replay_return = page.locator("a[data-price-workspace-return]:visible").first
        assert urlsplit(replay_return.get_attribute("href") or "").fragment == comparison_hash
        page.goto(fixture.BASE + "/my-products/beta")
        page.locator("#my-products-app").wait_for(state="visible")
        price_review_scope["product_ids"] = [fixture_ids["wb_id"]]

        open_products(page)
        page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        pick_action(page, "Создать карточки WB")
        confirmation = page.get_by_role("alertdialog")
        confirmation.get_by_text("рассчитанную цену", exact=False).wait_for()
        page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").evaluate("""box => {
            box.checked = true;
            box.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        confirmation.get_by_role("button", name="Создать карточки").click()
        page.get_by_text("Выбор изменился", exact=False).wait_for()
        assert not posts
        checks.append("selection_drift_during_wb_create_review_stops_before_post")

        open_products(page)
        page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        refresh_posts_before_unlinked = sum(post["path"] == LOCAL_REFRESH for post in posts)
        dialog, unlinked_preview = open_supplier_refresh_preview(
            page, [fixture_ids["manual_id"]], refresh_preview_posts,
        )
        checks.append("supplier_refresh_intent_dispatches_without_choice_menu")
        assert (unlinked_preview["eligible"], unlinked_preview["changed"],
                unlinked_preview["skipped"]) == (0, 0, 1)
        assert unlinked_preview["items"][0]["state"] == "unlinked"
        unlinked_row = dialog.locator(".mp-supplier-refresh-item.is-unlinked")
        assert unlinked_row.count() == 1
        assert unlinked_row.get_by_text(MANUAL_TITLE, exact=False).count() >= 1
        assert unlinked_row.get_by_text("Нет связи с каталогом поставщика", exact=True).count() == 1
        assert dialog.get_by_role("button", name="Обновить", exact=True).count() == 0
        dialog.get_by_role("button", name="Вернуться", exact=True).click()
        assert not dialog.is_visible()
        assert sum(post["path"] == LOCAL_REFRESH for post in posts) == refresh_posts_before_unlinked
        checks.append("ordinary_local_refresh_reports_zero_and_exact_selection")
        assert page.locator(".shk-job").count() == 0
        assert page.get_by_role("button", name=re.compile(r"^Подготовить для Ozon")).count() == 0
        checks.append("supplier_refresh_skip_has_exact_named_reason_without_generic_job")

        open_products(page)
        page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        pick_action(page, "Подготовить для Ozon")
        dialog = page.locator('dialog[aria-labelledby="mp-ozon-dialog-title"]')
        dialog.wait_for()
        assert dialog.get_by_label("Магазин Ozon").input_value() == str(fixture.FIXTURE["account_id"])
        prepare = dialog.get_by_role("button", name="Подготовить черновики")
        assert prepare.is_disabled()
        dialog.get_by_role("checkbox").check()
        with page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_OZON_PREPARE
                                  and response.request.method == "POST") as completed:
            prepare.click()
        assert completed.value.status == 202
        prepared = posts[0]
        assert prepared["path"] == LOCAL_OZON_PREPARE
        assert prepared["body"]["imported_product_ids"] == [fixture_ids["manual_id"]]
        assert prepared["body"]["account_id"] == fixture.FIXTURE["account_id"]
        assert prepared["body"]["confirm_prepare"] is True and "confirm_write" not in prepared["body"]
        job_path = completed.value.headers["location"]
        assert re.fullmatch(r"/marketplaces/ozon/uploads/ozon-upload-[0-9a-f]{32}", job_path)
        page.wait_for_url(fixture.BASE + job_path, wait_until="domcontentloaded")
        with fixture.app.app_context():
            queued_job = BackgroundJob.query.filter_by(
                job_uid=job_path.rsplit("/", 1)[-1], seller_id=fixture.FIXTURE["seller_id"],
            ).one()
            assert queued_job.total == 1
        checks.append("ordinary_ozon_bulk_prepare_queues_exact_local_set")

        open_products(page)
        page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").check()
        page.locator(".mp-more-actions summary").click()
        with fixture.app.app_context():
            default_enrichment_jobs = EnrichmentJob.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
            ).count()
        with page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_WB_ENRICH_REVIEW
                                  and response.request.method == "POST") as review:
            page.get_by_role("button", name="Дополнить карточки на WB").click()
        assert review.value.status == 200
        assert posts[1] == {"path": LOCAL_WB_ENRICH_REVIEW, "body": {"selected_ids": [str(fixture_ids["wb_id"])]}}
        page.wait_for_url(fixture.BASE + LOCAL_WB_ENRICH_REVIEW, wait_until="domcontentloaded")
        page.get_by_role("heading", name="Массовое обновление фото").wait_for()
        assert page.get_by_role(
            "link", name="К карточкам", exact=True,
        ).get_attribute("href") == "/products"
        assert page.get_by_role(
            "link", name="Отмена", exact=True,
        ).get_attribute("href") == "/products"
        default_review_fields = page.locator(".be-shell").evaluate("""element =>
            Object.fromEntries(Array.from(element.querySelectorAll('input[x-model^="fields."]'))
                .map(input => [input.getAttribute('x-model').slice('fields.'.length), input.checked]))
        """)
        assert default_review_fields == {
            "photos": True, "title": False, "description": False,
            "characteristics": False, "dimensions": False, "brand": False,
        }, default_review_fields
        page.get_by_role("button", name="Подтвердить обновление фото").wait_for()
        default_review_viewport = page.evaluate("""() => ({
            width: window.innerWidth, height: window.innerHeight,
        })""")
        page.set_viewport_size({"width": 320, "height": 900})
        verify_enrichment_review_bounds(page)
        page.set_viewport_size(default_review_viewport)
        checks.append("ordinary_wb_photo_review_fits_320")
        with fixture.app.app_context():
            assert EnrichmentJob.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
            ).count() == default_enrichment_jobs
        assert not wb_enrich_start_posts
        assert fixture.REPORT["provider_attempts"] == 0
        checks.append("ordinary_wb_enrichment_opens_owned_review_and_keeps_photo_only_default")

        supplier_path = fixture.PAGE_PATHS["supplier_products"] + "&show_imported=1"
        fixture.wait_document(page, "supplier_bulk", supplier_path)
        page.locator("#select-all-import").check()
        page.locator("#select-all-updates").check()
        submitted = page.evaluate("""() => ({
            importIds: new FormData(document.querySelector('#supplier-import-form')).getAll('product_ids'),
            importUpdates: new FormData(document.querySelector('#supplier-import-form')).getAll('update_product_ids'),
            updateIds: new FormData(document.querySelector('#supplier-update-form')).getAll('update_product_ids'),
            updateImports: new FormData(document.querySelector('#supplier-update-form')).getAll('product_ids'),
        })""")
        assert submitted == {"importIds": [str(fixture_ids["import_id"])], "importUpdates": [],
                             "updateIds": [str(fixture_ids["refresh_source_id"])], "updateImports": []}, submitted
        checks.append("supplier_import_and_refresh_forms_carry_independent_exact_scope")

        def is_supplier_import_batch_feed(response):
            parsed = urlsplit(response.url)
            query = parse_qs(parsed.query, keep_blank_values=True)
            return (parsed.path == "/api/my-products"
                    and response.request.method == "GET"
                    and len(query.get("import_batch", [])) == 1)

        with page.expect_response(is_supplier_import_batch_feed) as batch_feed, \
                page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_SUPPLIER_IMPORT
                                     and response.request.method == "POST") as imported:
            page.get_by_role("button", name="Импортировать товары").click()
        assert imported.value.status == 303
        page.wait_for_function("""() => {
            const tokens = new URLSearchParams(location.search).getAll('import_batch');
            return location.pathname === '/my-products/beta' && tokens.length === 1 &&
                tokens[0].length > 0 && new TextEncoder().encode(tokens[0]).length <= 8192;
        }""")
        assert batch_feed.value.status == 200
        batch_data = batch_feed.value.json()
        assert posts[2]["body"]["product_ids"] == [str(fixture_ids["import_id"])]
        assert "update_product_ids" not in posts[2]["body"]
        with fixture.app.app_context():
            imported_local = ImportedProduct.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
                supplier_product_id=fixture_ids["import_id"],
            ).one()
            imported_local_id = imported_local.id
            imported_local_title = imported_local.title
            assert imported_local_title == "Позиция для импорта"
            assert db.session.get(ImportedProduct, fixture.FIXTURE["source_id"]).supplier_content_revision == 1
        checks.append("ordinary_supplier_import_saves_only_its_selected_source")
        assert batch_data["import_batch_count"] == 1
        assert batch_data["pagination"] == {
            "page": 1, "per_page": 200, "pages": 1,
            "total": 1, "has_next": False,
        }
        assert [item["id"] for item in batch_data["items"]] == [imported_local_id]
        assert [item["title"] for item in batch_data["items"]] == [imported_local_title]
        batch_rows = page.locator(".mp-list article.mp-row")
        page.get_by_role("checkbox", name="Выбрать Позиция для импорта").wait_for()
        assert batch_rows.count() == 1
        assert batch_rows.locator(".mp-title").inner_text() == imported_local_title
        assert batch_rows.locator('input[type="checkbox"]').evaluate_all(
            "boxes => boxes.map(box => Number(box.value))"
        ) == [imported_local_id]
        checks.append("ordinary_supplier_import_batch_displays_exact_single_new_product")

        # Keep the opaque signed token local to this assertion flow. It is
        # deliberately omitted from feed diagnostics, runner output and
        # screenshots; the normal row action must carry the exact same token
        # back through the existing server-validated import_batch path.
        batch_tokens = parse_qs(urlsplit(page.url).query, keep_blank_values=True).get("import_batch", [])
        assert len(batch_tokens) == 1 and batch_tokens[0]
        batch_token = batch_tokens[0]
        imported_row = page.locator(".mp-list article.mp-row")
        common_content_link = imported_row.locator("[data-common-content-row-link]")
        assert common_content_link.count() == 1
        plain_href = common_content_link.get_attribute("href") or ""
        plain_parts = urlsplit(plain_href)
        assert plain_parts.path == "/my-products/common-content"
        assert parse_qs(plain_parts.query) == {"product_id": [str(imported_local_id)]}
        assert not plain_parts.fragment

        # Ctrl-click stays a direct editor URL and does not mint a return
        # capsule. This preserves the link's ordinary browser behavior.
        with context.expect_page() as direct_editor_page:
            common_content_link.click(modifiers=["Control"])
        direct_editor = direct_editor_page.value
        direct_editor.wait_for_url(lambda value: (
            urlsplit(str(value)).path == "/my-products/common-content"
            and parse_qs(urlsplit(str(value)).query) == {"product_id": [str(imported_local_id)]}
            and not urlsplit(str(value)).fragment
        ))
        direct_editor.wait_for_load_state("domcontentloaded")
        direct_editor.locator("#common-content-editor").wait_for(state="visible")
        direct_url = urlsplit(direct_editor.url)
        assert direct_url.path == "/my-products/common-content", {
            "path": direct_url.path,
            "query_keys": sorted(parse_qs(direct_url.query)),
            "has_fragment": bool(direct_url.fragment),
        }
        assert parse_qs(direct_url.query) == {"product_id": [str(imported_local_id)]}
        assert not direct_url.fragment
        direct_return = direct_editor.get_by_role("link", name="Вернуться к товарам", exact=True)
        direct_return.wait_for(state="visible")
        direct_return_parts = urlsplit(direct_return.get_attribute("href") or "")
        assert direct_return_parts.path == "/my-products" and not direct_return_parts.query
        assert not direct_return_parts.fragment
        direct_editor.close()
        checks.append("signed_import_batch_modifier_click_keeps_classic_direct_editor_href")

        page.get_by_role("checkbox", name="Выбрать Позиция для импорта").check()
        assert selected_browser_state(page) == {"selected": [imported_local_id], "ozonIds": []}
        with page.expect_response(lambda response: urlsplit(response.url).path == "/my-products/common-content"
                                  and response.request.method == "GET") as batch_editor:
            common_content_link.click()
        assert batch_editor.value.status == 200
        page.wait_for_url(lambda value: (
            urlsplit(str(value)).path == "/my-products/common-content"
            and parse_qs(urlsplit(str(value)).query) == {"product_id": [str(imported_local_id)]}
            and re.fullmatch(r"workspace-return=[a-f0-9]{32}", urlsplit(str(value)).fragment)
        ))
        page.locator("#common-content-editor").wait_for(state="visible")
        page.locator("[data-workspace-return-link]").first.wait_for(state="visible")
        assert parse_qs(urlsplit(page.url).query).get("product_id", []) == [str(imported_local_id)]
        batch_editor_hash = urlsplit(page.url).fragment
        assert re.fullmatch(r"workspace-return=[a-f0-9]{32}", batch_editor_hash)
        batch_return_links = page.locator("[data-workspace-return-link]").evaluate_all(
            "links => links.map(link => link.getAttribute('href'))"
        )
        assert len(batch_return_links) == 2
        for href in batch_return_links:
            parts = urlsplit(href or "")
            returned_tokens = parse_qs(parts.query, keep_blank_values=True).get("import_batch", [])
            assert parts.path == "/my-products/beta" and parts.fragment == batch_editor_hash
            assert set(parse_qs(parts.query, keep_blank_values=True)) == {"import_batch"}
            token_matches = returned_tokens == [batch_token]
            assert token_matches, {"returned_token_count": len(returned_tokens)}

        def is_same_import_batch_feed(response):
            parsed = urlsplit(response.url)
            query = parse_qs(parsed.query, keep_blank_values=True)
            return (parsed.path == "/api/my-products"
                    and response.request.method == "GET"
                    and query.get("import_batch", []) == [batch_token])

        with page.expect_response(is_same_import_batch_feed) as returned_batch_feed:
            page.get_by_role("link", name="Вернуться к товарам", exact=True).evaluate(
                "link => link.click()"
            )
        assert returned_batch_feed.value.status == 200
        page.wait_for_url(lambda value: (
            urlsplit(str(value)).path == "/my-products/beta"
            and parse_qs(urlsplit(str(value)).query, keep_blank_values=True).get("import_batch", [])
                == [batch_token]
            and urlsplit(str(value)).fragment == batch_editor_hash
        ))
        page.locator("#my-products-app").wait_for(state="visible")
        page.locator(".mp-list article.mp-row input[type=checkbox]").wait_for(state="attached")
        returned_batch_data = returned_batch_feed.value.json()
        assert returned_batch_data.get("import_batch_count") == 1
        assert [item.get("id") for item in returned_batch_data.get("items", [])] == [imported_local_id]
        page.wait_for_function("""id => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy && !proxy.loading && proxy.importBatchMode &&
                proxy.items.map(item => item.id).join(',') === String(id) &&
                proxy.selected.join(',') === String(id);
        }""", arg=imported_local_id)
        assert page.locator(".mp-list article.mp-row input[type=checkbox]:checked").evaluate_all(
            "boxes => boxes.map(box => Number(box.value))"
        ) == [imported_local_id]
        assert len(posts) == 3, posts
        checks.append("signed_import_batch_common_content_return_reuses_exact_server_token_and_selection")
        assert len(posts) == 3 and not errors, errors

        # The original early-photo check still covers the classic page after
        # leaving the receipt-scoped workspace explicitly.
        posts_before_classic = len(posts)
        fixture.wait_document(page, "classic_after_supplier_import", "/my-products")
        assert len(posts) == posts_before_classic == 3
        assert not errors, errors
        checks.append("classic_products_after_supplier_import_remains_error_free")

        # One real local refresh supplies both downstream channel handoffs.
        page.evaluate("localStorage.setItem('sh-theme', 'dark')")
        page.reload(wait_until="domcontentloaded")
        open_products(page)
        resume_result_requests = []

        def record_resume_result_request(browser_request):
            parsed = urlsplit(browser_request.url)
            if browser_request.method == "GET" and parsed.path == LOCAL_REFRESH_RESULT:
                resume_result_requests.append(True)

        page.on("request", record_resume_result_request)
        resume_route = page.locator("#mp-bootstrap").evaluate(
            "element => JSON.parse(element.textContent).urls.supplierRefreshResult"
        )
        assert resume_route == LOCAL_REFRESH_RESULT
        page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").check()
        page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        refresh_posts_before_success = sum(post["path"] == LOCAL_REFRESH for post in posts)
        posts_before_mixed_refresh = len(posts)
        dialog, mixed_preview = open_supplier_refresh_preview(
            page, [fixture_ids["local_id"], fixture_ids["manual_id"]],
            refresh_preview_posts,
        )
        assert (mixed_preview["eligible"], mixed_preview["changed"],
                mixed_preview["skipped"]) == (1, 1, 1)
        assert mixed_preview["items"][0]["state"] == "changed"
        assert mixed_preview["items"][1]["state"] == "unlinked"
        with page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_REFRESH
                                  and response.request.method == "POST") as mixed_refresh:
            dialog.get_by_role("button", name="Обновить", exact=True).click()
        assert mixed_refresh.value.status == 200
        assert_supplier_refresh_token_applied(mixed_preview, refresh_apply_tokens)
        mixed_data = mixed_refresh.value.json()
        assert (mixed_data["requested"], mixed_data["updated"], mixed_data["skipped"],
                mixed_data["errors"]) == (2, 1, 1, 0), mixed_data
        assert [entry["status"] for entry in mixed_data["items"]].count("updated") == 1
        assert [entry["status"] for entry in mixed_data["items"]].count("skipped") == 1
        assert mixed_data["wb_ready"] == 1
        assert mixed_data["wb_targets"] == [{
            "imported_product_id": fixture_ids["local_id"],
            "product_id": fixture_ids["wb_id"],
        }], mixed_data["wb_targets"]
        assert mixed_data.get("resume_receipt")
        assert posts[posts_before_mixed_refresh:] == [{
            "path": LOCAL_REFRESH,
            "body": {"product_ids": [fixture_ids["local_id"], fixture_ids["manual_id"]]},
        }]
        resume_key = "supplier-refresh-resume-v1"
        resume_receipt = page.evaluate(
            "key => sessionStorage.getItem(key)", resume_key,
        )
        assert isinstance(resume_receipt, str) and 0 < len(resume_receipt) <= 7500, len(resume_receipt or "")
        assert resume_receipt == mixed_data["resume_receipt"]
        refresh_posts_after_success = sum(post["path"] == LOCAL_REFRESH for post in posts)
        assert refresh_posts_after_success == refresh_posts_before_success + 1
        mixed_result = page.locator(".mp-refresh-result")
        mixed_result.get_by_text("В запросе товаров: 2.", exact=False).wait_for()
        mixed_result.get_by_role(
            "heading", name="Обновление от поставщика завершено", exact=True,
        ).wait_for()
        assert page.evaluate(
            "document.activeElement === document.querySelector('.mp-refresh-result')"
        )
        mixed_result.get_by_text("Карточки и итог (2)").click()
        updated_title = mixed_result.locator(".mp-refresh-result-item.is-ok strong")
        updated_title.wait_for()
        assert MAIN_TITLE in updated_title.inner_text()
        skipped_title = mixed_result.locator(".mp-refresh-result-item.is-muted strong")
        skipped_title.wait_for()
        assert MANUAL_TITLE in skipped_title.inner_text()
        mixed_result.get_by_text("Данные поставщика сохранены в Seller Hub.").wait_for()
        mixed_result.get_by_text("Нет связи с каталогом поставщика. Данные не менялись.").wait_for()
        with fixture.app.app_context():
            persisted = db.session.get(ImportedProduct, fixture_ids["local_id"])
            assert persisted.title == REFRESHED_TITLE and persisted.supplier_content_revision == 2
            assert db.session.get(Product, fixture_ids["wb_id"]).title == MAIN_TITLE
        checks.append("supplier_refresh_mixed_local_update_and_skip_are_named")

        wb_result = mixed_result
        wb_cta = refresh_wb_button(page)
        assert wb_cta.count() == 1
        assert refresh_ozon_button(page).count() == 1
        verify_bounds(page)
        page.set_viewport_size({"width": 320, "height": 900})
        verify_bounds(page)
        assert page.locator(".main-content").evaluate(
            "(element) => getComputedStyle(element).marginLeft"
        ) == "0px"
        assert wb_result.is_visible()
        checks.append("seller_workspace_resize_releases_desktop_sidebar_space_immediately")
        wb_cta_bounds = wb_cta.bounding_box()
        assert wb_cta_bounds and wb_cta_bounds["height"] >= 44, wb_cta_bounds
        page.screenshot(
            path=OUTPUT / "supplier-refresh-channel-actions-dark-320.png",
            animations="disabled",
        )

        # The result CTA submits only the verified WB Product IDs to the old
        # review route. That POST renders the review; it must not start a job.
        page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        page.evaluate("window.scrollTo(0, 0)")
        wb_cta.focus()
        assert wb_cta.evaluate("element => element === document.activeElement")
        with fixture.app.app_context():
            jobs_before_wb_review = EnrichmentJob.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
            ).count()
        posts_before_wb_review = len(posts)
        starts_before_wb_review = len(wb_enrich_start_posts)
        provider_attempts_before_wb_review = fixture.REPORT["provider_attempts"]
        with page.expect_response(
            lambda response: urlsplit(response.url).path == LOCAL_WB_ENRICH_REVIEW
            and response.request.method == "POST",
        ) as wb_review:
            wb_cta.press("Enter")
        assert wb_review.value.status == 200
        assert posts[posts_before_wb_review:] == [{
            "path": LOCAL_WB_ENRICH_REVIEW,
            "body": {
                "selected_ids": [str(fixture_ids["wb_id"])],
                "preset": ["characteristics"],
                "from_supplier_refresh": ["1"],
            },
        }], posts[posts_before_wb_review:]
        page.wait_for_url(
            fixture.BASE + LOCAL_WB_ENRICH_REVIEW,
            wait_until="domcontentloaded",
        )
        page.get_by_role(
            "heading", name="Отправка характеристик на WB", exact=True,
        ).wait_for()
        page.get_by_text("Выбрано: 1", exact=False).wait_for()
        page.get_by_role(
            "button", name="Подтвердить отправку характеристик", exact=True,
        ).wait_for()
        reviewed_fields = page.locator(".be-shell").evaluate("""element =>
            Object.fromEntries(Array.from(element.querySelectorAll('input[x-model^="fields."]'))
                .map(input => [input.getAttribute('x-model').slice('fields.'.length), input.checked]))
        """)
        assert reviewed_fields == {
            "photos": False, "title": False, "description": False,
            "characteristics": True, "dimensions": True, "brand": False,
        }, reviewed_fields
        verify_enrichment_review_bounds(page)
        page.locator(".be-shell").screenshot(
            path=OUTPUT / "supplier-refresh-wb-review-dark-320.png",
            animations="disabled",
        )
        assert len(posts) == posts_before_wb_review + 1
        assert len(wb_enrich_start_posts) == starts_before_wb_review
        assert fixture.REPORT["provider_attempts"] == provider_attempts_before_wb_review == 0
        with fixture.app.app_context():
            assert EnrichmentJob.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
            ).count() == jobs_before_wb_review
        checks.append("supplier_refresh_wb_handoff_posts_exact_owned_ids_to_characteristics_review_only")
        checks.append("supplier_refresh_wb_characteristics_review_fits_320")
        return_url = "/my-products/beta#supplier-refresh-result"
        assert page.get_by_role(
            "link", name="К итогу обновления поставщика", exact=True,
        ).get_attribute("href") == return_url
        assert page.get_by_role(
            "link", name="К итогу обновления", exact=True,
        ).get_attribute("href") == return_url

        def assert_resumed_refresh(response):
            assert response.status == 200
            assert response.request.headers.get("x-supplier-refresh-receipt") == resume_receipt
            data = response.json()
            assert data["success"] is True
            assert (data["requested"], data["updated"], data["skipped"], data["errors"]) == (2, 1, 1, 0)
            assert data["items"] == [
                {"product_id": fixture_ids["local_id"], "status": "updated", "reason": None},
                {"product_id": fixture_ids["manual_id"], "status": "skipped", "reason": "no_supplier_link"},
            ]
            assert data["wb_targets"] == [{
                "imported_product_id": fixture_ids["local_id"],
                "product_id": fixture_ids["wb_id"],
            }]
            assert data["wb_unavailable"] == 0
            assert [item["productId"] for item in data["labels"]] == [
                fixture_ids["local_id"], fixture_ids["manual_id"],
            ]
            assert REFRESHED_TITLE in data["labels"][0]["label"]
            assert MANUAL_TITLE in data["labels"][1]["label"]
            return data

        wb_return = page.get_by_role(
            "link", name="К итогу обновления поставщика", exact=True,
        )
        with page.expect_response(
            lambda response: is_supplier_refresh_result(response)
            and response.request.headers.get("x-supplier-refresh-receipt") == resume_receipt,
        ) as wb_resumed:
            wb_return.click()
        assert_resumed_refresh(wb_resumed.value)
        page.wait_for_url(fixture.BASE + return_url, wait_until="domcontentloaded")
        restored_result = page.locator(".mp-refresh-result")
        restored_result.get_by_role(
            "heading", name="Сохранённый итог обновления от поставщика", exact=True,
        ).wait_for()
        restored_result.get_by_text("В запросе товаров: 2.", exact=False).wait_for()
        verify_mobile_supplier_refresh_focus(page)
        page.screenshot(
            path=OUTPUT / "supplier-refresh-restored-after-wb-dark-320.png",
            animations="disabled",
        )
        checks.append("supplier_refresh_wb_return_focus_clears_mobile_header_and_stays_in_bounds")
        restored_result.get_by_text("Карточки и итог (2)", exact=True).click()
        restored_result.get_by_text(REFRESHED_TITLE, exact=False).wait_for()
        restored_result.get_by_text(MANUAL_TITLE, exact=False).wait_for()
        assert restored_result.locator(".mp-refresh-result-counts dd").all_text_contents() == ["1", "1", "0"]
        assert refresh_wb_button(page).count() == 1
        assert refresh_ozon_button(page).count() == 1
        assert page.get_by_role("alertdialog").count() == 0
        assert sum(post["path"] == LOCAL_REFRESH for post in posts) == refresh_posts_after_success
        checks.append("supplier_refresh_wb_return_restores_signed_receipt_without_refresh_post")

        # Hand off the verified update from the real local mixed result. The
        # skipped row is selected separately to prove the CTA uses its own
        # exact updated set and leaves the catalog selection alone.
        page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        assert selected_browser_state(page) == {
            "selected": [fixture_ids["manual_id"]], "ozonIds": [],
        }
        refresh_cta = refresh_ozon_button(page)
        assert refresh_cta.count() == 1
        posts_before_handoff = len(posts)
        refresh_cta.click()
        refresh_dialog = page.locator('dialog[aria-labelledby="mp-ozon-dialog-title"]')
        refresh_dialog.wait_for()
        assert refresh_dialog.get_by_label("Магазин Ozon").input_value() == str(fixture.FIXTURE["account_id"])
        assert selected_browser_state(page) == {
            "selected": [fixture_ids["manual_id"]], "ozonIds": [fixture_ids["local_id"]],
        }
        assert len(posts) == posts_before_handoff, posts
        prepare = refresh_dialog.get_by_role("button", name="Подготовить черновики")
        assert prepare.is_disabled()
        refresh_dialog.get_by_role("button", name="Вернуться").click()
        assert not refresh_dialog.is_visible()
        assert len(posts) == posts_before_handoff, posts
        assert selected_browser_state(page) == {
            "selected": [fixture_ids["manual_id"]], "ozonIds": [fixture_ids["local_id"]],
        }
        assert refresh_cta.evaluate("element => element === document.activeElement")

        refresh_cta.click()
        refresh_dialog.wait_for()
        prepare = refresh_dialog.get_by_role("button", name="Подготовить черновики")
        assert prepare.is_disabled()
        refresh_dialog.get_by_role("checkbox").check()
        assert prepare.is_enabled()
        with page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_OZON_PREPARE
                                  and response.request.method == "POST") as refreshed_prepare:
            prepare.click()
        assert refreshed_prepare.value.status == 202
        prepared_from_refresh = posts[-1]
        assert prepared_from_refresh["path"] == LOCAL_OZON_PREPARE
        assert set(prepared_from_refresh["body"]) == {
            "imported_product_ids", "account_id", "request_key", "confirm_prepare",
        }
        assert prepared_from_refresh["body"]["imported_product_ids"] == [fixture_ids["local_id"]]
        assert prepared_from_refresh["body"]["account_id"] == fixture.FIXTURE["account_id"]
        assert re.fullmatch(r"[a-f0-9]{32}", prepared_from_refresh["body"]["request_key"])
        assert prepared_from_refresh["body"]["request_key"] != posts[0]["body"]["request_key"]
        assert prepared_from_refresh["body"]["confirm_prepare"] is True
        assert "confirm_write" not in prepared_from_refresh["body"]
        handoff_job_path = refreshed_prepare.value.headers["location"]
        assert re.fullmatch(r"/marketplaces/ozon/uploads/ozon-upload-[0-9a-f]{32}", handoff_job_path)
        page.wait_for_url(fixture.BASE + handoff_job_path, wait_until="domcontentloaded")
        with fixture.app.app_context():
            handoff_job = BackgroundJob.query.filter_by(
                job_uid=handoff_job_path.rsplit("/", 1)[-1], seller_id=fixture.FIXTURE["seller_id"],
            ).one()
            assert handoff_job.total == 1
        checks.append("supplier_refresh_verified_update_handoff_posts_exact_local_ozon_prepare_key")

        # Ozon's local preparation result returns to the same signed supplier
        # receipt and preserves the WB handoff derived from that receipt.
        my_products_link = page.locator('a[data-source-prepare-products][href*="#workspace-return="]')
        my_products_link.wait_for()
        ozon_return_url = "/my-products/beta"
        workspace_href = urlsplit(my_products_link.get_attribute("href"))
        assert workspace_href.path == ozon_return_url and not workspace_href.query
        assert re.fullmatch(r"workspace-return=[a-f0-9]{32}", workspace_href.fragment)
        refresh_posts_before_ozon_return = sum(post["path"] == LOCAL_REFRESH for post in posts)
        with page.expect_response(
            lambda response: is_supplier_refresh_result(response)
            and response.request.headers.get("x-supplier-refresh-receipt") == resume_receipt,
        ) as ozon_resumed:
            my_products_link.click()
        assert_resumed_refresh(ozon_resumed.value)
        page.wait_for_url(lambda value: urlsplit(value).path == ozon_return_url, wait_until="domcontentloaded")
        restored_manual = page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}")
        restored_manual.wait_for()
        assert restored_manual.is_checked()
        restored_result = page.locator(".mp-refresh-result")
        restored_result.get_by_role(
            "heading", name="Сохранённый итог обновления от поставщика", exact=True,
        ).wait_for()
        restored_result.get_by_text("Карточки и итог (2)", exact=True).click()
        restored_result.get_by_text(REFRESHED_TITLE, exact=False).wait_for()
        restored_result.get_by_text(MANUAL_TITLE, exact=False).wait_for()
        assert restored_result.locator(".mp-refresh-result-counts dd").all_text_contents() == ["1", "1", "0"]
        assert refresh_wb_button(page).count() == 1
        assert refresh_ozon_button(page).count() == 1
        assert page.get_by_role("alertdialog").count() == 0
        assert sum(post["path"] == LOCAL_REFRESH for post in posts) == refresh_posts_before_ozon_return
        checks.append("supplier_refresh_ozon_result_returns_to_same_receipt_and_wb_scope")

        posts_before_updated_selection = len(posts)

        def is_updated_batch_feed(response):
            path = urlsplit(response.url)
            return (
                path.path == "/api/my-products" and response.request.method == "GET"
                and "import_batch" in parse_qs(path.query)
            )

        select_updated = restored_result.get_by_role(
            "button", name=re.compile(r"^Выбрать обновлённые товары"),
        )
        assert select_updated.is_enabled()
        assert select_updated.bounding_box()["height"] >= 44
        with page.expect_response(is_updated_batch_feed) as updated_feed:
            select_updated.press("Enter")
        assert updated_feed.value.status == 200
        scoped = updated_feed.value.json()
        assert scoped["batch_kind"] == "supplier_refresh_result"
        assert [item["id"] for item in scoped["items"]] == [fixture_ids["local_id"]]
        page.get_by_role("checkbox", name=f"Выбрать {REFRESHED_TITLE}").wait_for()
        assert page.get_by_role("checkbox", name=f"Выбрать {REFRESHED_TITLE}").is_checked()
        assert page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").count() == 0
        assert selected_browser_state(page)["selected"] == [fixture_ids["local_id"]]
        scoped_result = page.locator(".mp-refresh-result")
        scoped_result.get_by_role(
            "heading", name="Сохранённый итог обновления от поставщика", exact=True,
        ).wait_for()
        assert scoped_result.locator(".mp-refresh-result-counts dd").all_text_contents() == ["1", "1", "0"]
        verify_bounds(page)
        page.screenshot(path=OUTPUT / "supplier-refresh-selected-price-scope-320.png", animations="disabled")
        assert len(posts) == posts_before_updated_selection

        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/prices/change"
            and response.request.method == "GET",
        ) as price_entry:
            pick_action(page, "Изменить цены WB")
        assert price_entry.value.status == 200
        price_query = parse_qs(urlsplit(price_entry.value.url).query)
        assert price_query["product_ids"] == [str(fixture_ids["wb_id"])], price_query.get("product_ids")
        with page.expect_response(is_updated_batch_feed) as returned_updated_feed:
            page.get_by_role("link", name="Вернуться к товарам", exact=True).first.click()
        assert returned_updated_feed.value.status == 200
        assert returned_updated_feed.value.json()["batch_kind"] == "supplier_refresh_result"
        page.get_by_role("checkbox", name=f"Выбрать {REFRESHED_TITLE}").wait_for()
        assert page.get_by_role("checkbox", name=f"Выбрать {REFRESHED_TITLE}").is_checked()
        page.locator(".mp-refresh-result").get_by_role(
            "heading", name="Сохранённый итог обновления от поставщика", exact=True,
        ).wait_for()
        assert len(posts) == posts_before_updated_selection
        assert fixture.REPORT["provider_attempts"] == 0
        checks.append("supplier_refresh_updated_selection_keeps_exact_price_scope_and_signed_result")

        with page.expect_response(is_supplier_refresh_result) as ordinary_result:
            page.goto(fixture.BASE + return_url, wait_until="domcontentloaded")
        assert_resumed_refresh(ordinary_result.value)
        page.locator(".mp-refresh-result").get_by_role(
            "heading", name="Сохранённый итог обновления от поставщика", exact=True,
        ).wait_for()

        result_gets_before_hide = len(resume_result_requests)
        refresh_posts_before_hide = sum(post["path"] == LOCAL_REFRESH for post in posts)
        page.get_by_role("button", name="Скрыть итог обновления", exact=True).click()
        assert page.locator(".mp-refresh-result").count() == 0
        assert page.evaluate("key => sessionStorage.getItem(key) === null", resume_key)
        assert not urlsplit(page.url).fragment
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/api/my-products"
            and response.request.method == "GET",
        ) as feed_after_hide:
            page.reload(wait_until="domcontentloaded")
        assert feed_after_hide.value.status == 200
        page.get_by_role("checkbox", name=f"Выбрать {REFRESHED_TITLE}").wait_for()
        assert len(resume_result_requests) == result_gets_before_hide
        assert sum(post["path"] == LOCAL_REFRESH for post in posts) == refresh_posts_before_hide
        assert page.locator(".mp-refresh-result").count() == 0
        checks.append("supplier_refresh_hide_clears_receipt_before_reload_without_restore_or_refresh")

        # The actual review is a local, read-only preview. Exercise the mixed
        # linked/unlinked selection at 320px before any apply changes its title.
        preview_context = browser.new_context(viewport={"width": 320, "height": 900})
        preview_context.add_init_script("localStorage.setItem('sh-theme', 'dark');")
        preview_context.add_cookies([fixture.synthetic_login_cookie()])
        preview_context.route("**/*", bridge)
        preview_page = preview_context.new_page()
        preview_page.on("pageerror", lambda error: errors.append(str(error)))
        # The earlier real mixed apply has already advanced this fixture row.
        # Temporarily restore its old baseline so this independent context can
        # review the same title/quantity delta; restore the saved state below.
        with fixture.app.app_context():
            preview_target = db.session.get(ImportedProduct, fixture_ids["local_id"])
            target_state_after_good_apply = (
                preview_target.title,
                preview_target.supplier_content_revision,
                preview_target.supplier_quantity,
                preview_target.supplier_price,
            )
            preview_target.title = MAIN_TITLE
            preview_target.supplier_content_revision = 1
            preview_target.supplier_quantity = 3
            db.session.commit()
        open_products(preview_page)
        preview_page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").check()
        preview_page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        preview_posts_before = len(refresh_preview_posts)
        posts_before_cancel = len(posts)
        refresh_posts_before_cancel = sum(post["path"] == LOCAL_REFRESH for post in posts)
        dialog, mobile_preview = open_supplier_refresh_preview(
            preview_page, [fixture_ids["local_id"], fixture_ids["manual_id"]],
            refresh_preview_posts,
        )
        assert len(refresh_preview_posts) == preview_posts_before + 1
        assert (mobile_preview["eligible"], mobile_preview["changed"],
                mobile_preview["skipped"]) == (1, 1, 1)
        changed_item, unlinked_item = mobile_preview["items"]
        assert changed_item["state"] == "changed"
        assert any(change.get("field") == "title"
                   and change.get("before") == MAIN_TITLE
                   and change.get("after") == REFRESHED_TITLE
                   for change in changed_item["changes"])
        assert any(change.get("field") == "supplier_quantity"
                   and change.get("after") == "0"
                   for change in changed_item["changes"])
        assert unlinked_item["state"] == "unlinked"
        changed_row = dialog.locator(".mp-supplier-refresh-item.is-changed").filter(
            has_text=MAIN_TITLE,
        )
        title_change = changed_row.locator(".mp-supplier-refresh-changes > div").filter(
            has_text="Название",
        )
        quantity_change = changed_row.locator(".mp-supplier-refresh-changes > div").filter(
            has_text="Остаток у поставщика",
        )
        unlinked_row = dialog.locator(".mp-supplier-refresh-item.is-unlinked").filter(
            has_text=MANUAL_TITLE,
        )
        assert title_change.locator("dd").inner_text().count("→") == 1
        assert MAIN_TITLE in title_change.locator("dd").inner_text()
        assert REFRESHED_TITLE in title_change.locator("dd").inner_text()
        quantity_text = quantity_change.locator("dd").inner_text()
        assert "3" in quantity_text and "0" in quantity_text and "→" in quantity_text
        assert changed_row.count() == 1 and title_change.count() == 1 and quantity_change.count() == 1
        assert unlinked_row.count() == 1
        assert dialog.get_by_text("Выбрано", exact=True).is_visible()
        assert dialog.get_by_text("Можно обновить", exact=True).is_visible()
        assert dialog.get_by_text("Изменятся", exact=True).is_visible()
        assert dialog.get_by_text("Без связи", exact=True).is_visible()
        assert dialog.get_by_text("Название", exact=True).is_visible()
        assert dialog.get_by_text("Остаток у поставщика", exact=True).is_visible()
        assert unlinked_row.get_by_text(MANUAL_TITLE, exact=False).count() >= 1
        assert unlinked_row.get_by_text("Нет связи с каталогом поставщика", exact=True).count() == 1
        assert mobile_preview["review_token"] not in dialog.inner_text()
        verify_bounds(preview_page)
        preview_geometry = preview_page.evaluate("""() => {
            const dialog = document.querySelector('dialog.mp-supplier-refresh-dialog');
            const rect = dialog.getBoundingClientRect();
            return {width: document.documentElement.clientWidth, height: window.innerHeight,
                    left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom};
        }""")
        assert preview_geometry["width"] == 320, preview_geometry
        assert preview_geometry["left"] >= -1 and preview_geometry["right"] <= 321, preview_geometry
        assert preview_geometry["top"] >= 0 and preview_geometry["bottom"] <= preview_geometry["height"] + 1, preview_geometry
        preview_page.screenshot(
            path=OUTPUT / "supplier-refresh-preview-mixed-dark-320.png",
            animations="disabled",
        )
        dialog.get_by_role("button", name="Вернуться", exact=True).click()
        assert not dialog.is_visible()
        assert sum(post["path"] == LOCAL_REFRESH for post in posts) == refresh_posts_before_cancel
        assert len(posts) == posts_before_cancel, posts
        assert selected_browser_state(preview_page)["selected"] == [
            fixture_ids["local_id"], fixture_ids["manual_id"],
        ]
        checks.append("supplier_refresh_preview_cancel_has_zero_apply")
        checks.append("supplier_refresh_preview_exact_mixed_unlinked_zero_quantity_fits_320")

        # A real source change after review invalidates its capability. The
        # existing local item must remain untouched when the server returns 409.
        preview_page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").uncheck()
        with fixture.app.app_context():
            source_before_stale = db.session.get(SupplierProduct, fixture_ids["refresh_source_id"])
            old_source_title = source_before_stale.title
            old_source_revision = source_before_stale.content_revision
            target_before_stale = db.session.get(ImportedProduct, fixture_ids["local_id"])
            target_state_before_stale = (
                target_before_stale.title,
                target_before_stale.supplier_content_revision,
                target_before_stale.supplier_quantity,
                target_before_stale.supplier_price,
            )
        stale_dialog, stale_preview = open_supplier_refresh_preview(
            preview_page, [fixture_ids["local_id"]], refresh_preview_posts,
        )
        with fixture.app.app_context():
            stale_source = db.session.get(SupplierProduct, fixture_ids["refresh_source_id"])
            stale_source.title = "Изменено после предпросмотра"
            stale_source.content_revision = old_source_revision + 1
            db.session.commit()
        with preview_page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_REFRESH
                                         and response.request.method == "POST") as stale_apply:
            stale_dialog.get_by_role("button", name="Обновить", exact=True).click()
        assert stale_apply.value.status == 409
        assert_supplier_refresh_token_applied(stale_preview, refresh_apply_tokens)
        assert posts[-1] == {"path": LOCAL_REFRESH,
                             "body": {"product_ids": [fixture_ids["local_id"]]}}
        stale_dialog.get_by_role("alert").get_by_text(
            "Данные товаров изменились. Проверьте обновление снова.", exact=True,
        ).wait_for()
        assert stale_dialog.get_by_role("button", name="Проверить снова", exact=True).is_visible()
        assert stale_dialog.get_by_role("button", name="Обновить", exact=True).count() == 0
        with fixture.app.app_context():
            unchanged_target = db.session.get(ImportedProduct, fixture_ids["local_id"])
            assert (
                unchanged_target.title,
                unchanged_target.supplier_content_revision,
                unchanged_target.supplier_quantity,
                unchanged_target.supplier_price,
            ) == target_state_before_stale
            stale_source = db.session.get(SupplierProduct, fixture_ids["refresh_source_id"])
            stale_source.title = old_source_title
            stale_source.content_revision = old_source_revision
            restored_target = db.session.get(ImportedProduct, fixture_ids["local_id"])
            (
                restored_target.title,
                restored_target.supplier_content_revision,
                restored_target.supplier_quantity,
                restored_target.supplier_price,
            ) = target_state_after_good_apply
            db.session.commit()
        stale_dialog.get_by_role("button", name="Вернуться", exact=True).click()
        assert not stale_dialog.is_visible()
        assert sum(post["path"] == LOCAL_REFRESH for post in posts) == refresh_posts_before_cancel + 1
        assert fixture.REPORT["provider_attempts"] == 0
        checks.append("supplier_refresh_stale_source_review_returns_409_without_atomic_apply")
        preview_context.close()

        # Start a separate mobile browser context at 320px. Do not resize a
        # desktop page across the shared sidebar breakpoint for this check.
        mobile_context = browser.new_context(viewport={"width": 320, "height": 900})
        mobile_context.add_init_script("localStorage.setItem('sh-theme', 'dark');")
        mobile_context.add_cookies([fixture.synthetic_login_cookie()])
        mobile_context.route("**/*", bridge)
        mobile_page = mobile_context.new_page()
        mobile_page.on("pageerror", lambda error: errors.append(str(error)))
        open_products(mobile_page, REFRESHED_TITLE)
        verify_bounds(mobile_page)

        # Exercise a row-level local error using a fixed terminal API response;
        # this tests presentation only and does not call a provider.
        mobile_page.get_by_role("checkbox", name=f"Выбрать {REFRESHED_TITLE}").check()
        mobile_page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        mobile_page.get_by_role("checkbox", name=f"Выбрать {imported_local_title}").check()
        refresh_response_override = {
            "success": True,
            "status": "completed_with_errors",
            "requested": 3,
            "updated": 1,
            "skipped": 1,
            "errors": 1,
            "wb_ready": 1,
            "items": [
                {"product_id": fixture_ids["local_id"], "status": "updated", "reason": None},
                {"product_id": fixture_ids["manual_id"], "status": "skipped", "reason": "no_supplier_link"},
                {"product_id": imported_local_id, "status": "error", "reason": "update_failed"},
            ],
            "wb_targets": [{
                "imported_product_id": fixture_ids["local_id"],
                "product_id": fixture_ids["wb_id"],
            }],
        }
        partial_dialog, partial_preview = open_supplier_refresh_preview(
            mobile_page,
            [fixture_ids["local_id"], fixture_ids["manual_id"], imported_local_id],
            refresh_preview_posts,
        )
        assert partial_preview["requested"] == 3
        assert partial_preview["skipped"] == 1
        with mobile_page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_REFRESH
                                         and response.request.method == "POST") as partial_refresh:
            partial_dialog.get_by_role("button", name="Обновить", exact=True).click()
        assert partial_refresh.value.status == 200
        assert_supplier_refresh_token_applied(partial_preview, refresh_apply_tokens)
        assert posts[-1] == {"path": LOCAL_REFRESH,
                             "body": {"product_ids": [fixture_ids["local_id"], fixture_ids["manual_id"],
                                                        imported_local_id]}}
        partial_result = mobile_page.locator(".mp-refresh-result")
        partial_result.get_by_role("heading", name="Обновление завершено с ошибками").wait_for()
        partial_result.get_by_text("В запросе товаров: 3.", exact=False).wait_for()
        assert [value.strip() for value in partial_result.locator(
            ".mp-refresh-result-counts dd"
        ).all_text_contents()] == ["1", "1", "1"]
        partial_result.get_by_text("Карточки и итог (3)").click()
        error_row = partial_result.locator(".mp-refresh-result-item.is-danger")
        error_row.get_by_text(imported_local_title, exact=False).wait_for()
        error_row.get_by_text("Не удалось сохранить данные. Откройте карточку и проверьте её.").wait_for()
        assert error_row.get_by_role("link", name="Открыть карточку").count() == 1
        updated_row = partial_result.locator(".mp-refresh-result-item.is-ok")
        updated_row.get_by_text(REFRESHED_TITLE, exact=False).wait_for()
        partial_result.locator(".mp-refresh-result-item.is-muted").get_by_text(MANUAL_TITLE, exact=False).wait_for()
        assert mobile_page.locator(".shk-job").count() == 0
        assert mobile_page.get_by_role("button", name="Повторить неудачные").count() == 0
        verify_bounds(mobile_page)
        summary = partial_result.locator(".mp-refresh-result-details summary")
        summary_bounds = summary.bounding_box()
        close_bounds = partial_result.get_by_role("button", name="Скрыть итог обновления").bounding_box()
        assert summary_bounds and summary_bounds["height"] >= 44, summary_bounds
        assert close_bounds and close_bounds["height"] >= 44, close_bounds
        summary.focus()
        summary.press("Enter")
        assert not partial_result.locator("details").evaluate("element => element.open")
        summary.press("Enter")
        assert partial_result.locator("details").evaluate("element => element.open")
        assert refresh_ozon_button(mobile_page).count() == 1
        assert refresh_wb_button(mobile_page).count() == 1
        checks.append("supplier_refresh_partial_error_has_owner_link_and_no_retry")
        checks.append("supplier_refresh_result_fits_320_and_details_are_keyboard_operable")

        # Even a partial result with updated, skipped, and failed rows may hand
        # off only the verified update; opening and dismissing is local UI only.
        mobile_page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        assert selected_browser_state(mobile_page) == {
            "selected": [fixture_ids["manual_id"]], "ozonIds": [],
        }
        partial_cta = refresh_ozon_button(mobile_page)
        partial_post_count = len(posts)
        partial_cta.click()
        partial_dialog = mobile_page.locator('dialog[aria-labelledby="mp-ozon-dialog-title"]')
        partial_dialog.wait_for()
        assert partial_dialog.get_by_label("Магазин Ozon").input_value() == str(fixture.FIXTURE["account_id"])
        assert selected_browser_state(mobile_page) == {
            "selected": [fixture_ids["manual_id"]], "ozonIds": [fixture_ids["local_id"]],
        }
        assert len(posts) == partial_post_count, posts
        partial_dialog.get_by_role("button", name="Вернуться").click()
        assert not partial_dialog.is_visible()
        assert selected_browser_state(mobile_page) == {
            "selected": [fixture_ids["manual_id"]], "ozonIds": [fixture_ids["local_id"]],
        }
        assert partial_cta.evaluate("element => element === document.activeElement")
        assert len(posts) == partial_post_count, posts
        checks.append("supplier_refresh_partial_updated_skipped_error_handoff_is_exact_and_dismissible")

        # Unknown and rejected requests show the submitted count and refresh
        # action once, without duplicating a generic row for every selection.
        mobile_page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").uncheck()
        mobile_page.get_by_role("checkbox", name=f"Выбрать {REFRESHED_TITLE}").check()
        refresh_response_override = {
            "success": True, "status": "completed", "requested": 1,
            "updated": 1, "skipped": 0, "errors": 0, "items": [],
        }
        unknown_dialog, unknown_preview = open_supplier_refresh_preview(
            mobile_page, [fixture_ids["local_id"]], refresh_preview_posts,
        )
        assert unknown_preview["eligible"] == 1 and unknown_preview["skipped"] == 0
        with mobile_page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_REFRESH
                                         and response.request.method == "POST"):
            unknown_dialog.get_by_role("button", name="Обновить", exact=True).click()
        assert_supplier_refresh_token_applied(unknown_preview, refresh_apply_tokens)
        assert posts[-1] == {"path": LOCAL_REFRESH,
                             "body": {"product_ids": [fixture_ids["local_id"]]}}
        unknown_result = mobile_page.locator(".mp-refresh-result")
        unknown_result.get_by_role("heading", name="Итог обновления не подтверждён").wait_for()
        unknown_result.get_by_text("В запросе товаров: 1.", exact=False).wait_for()
        assert unknown_result.locator(".mp-refresh-result-details").count() == 0
        assert unknown_result.get_by_role("button", name="Обновить список").count() == 1
        assert mobile_page.get_by_role("button", name=re.compile(r"^Подготовить для Ozon")).count() == 0
        assert mobile_page.get_by_role("button", name=re.compile(r"^Проверить характеристики WB")).count() == 0
        assert unknown_result.get_by_text(REFRESHED_TITLE, exact=False).count() == 0
        checks.append("supplier_refresh_unknown_result_has_count_and_one_refresh_action")

        mobile_page.get_by_role("checkbox", name=f"Выбрать {REFRESHED_TITLE}").check()
        refresh_response_override = {"success": False, "error": "synthetic rejection"}
        refresh_response_status = 422
        rejected_dialog, rejected_preview = open_supplier_refresh_preview(
            mobile_page, [fixture_ids["local_id"]], refresh_preview_posts,
        )
        assert rejected_preview["eligible"] == 1 and rejected_preview["skipped"] == 0
        with mobile_page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_REFRESH
                                         and response.request.method == "POST") as rejected_refresh:
            rejected_dialog.get_by_role("button", name="Обновить", exact=True).click()
        assert rejected_refresh.value.status == 422
        assert_supplier_refresh_token_applied(rejected_preview, refresh_apply_tokens)
        assert posts[-1] == {"path": LOCAL_REFRESH,
                             "body": {"product_ids": [fixture_ids["local_id"]]}}
        rejected_result = mobile_page.locator(".mp-refresh-result")
        rejected_result.get_by_role("heading", name="Выбранные товары не обновлялись").wait_for()
        rejected_result.get_by_text("В запросе товаров: 1.", exact=False).wait_for()
        assert rejected_result.locator(".mp-refresh-result-details").count() == 0
        assert rejected_result.get_by_role("button", name="Обновить список").count() == 1
        assert mobile_page.get_by_role("button", name=re.compile(r"^Подготовить для Ozon")).count() == 0
        assert mobile_page.get_by_role("button", name=re.compile(r"^Проверить характеристики WB")).count() == 0
        assert rejected_result.get_by_text(REFRESHED_TITLE, exact=False).count() == 0
        verify_bounds(mobile_page)
        checks.append("supplier_refresh_rejected_result_has_count_and_one_refresh_action")

        # Exercise a real terminal zero-update result. The local service gets
        # one synthetic row-write failure inside its normal savepoint, then
        # issues the usual signed receipt. No provider or channel API is used.
        failure_target_id = fixture_ids["local_id"]
        with fixture.app.app_context():
            failure_source = db.session.get(SupplierProduct, fixture_ids["refresh_source_id"])
            source_before_failure = (failure_source.title, failure_source.content_revision)
            failure_source.title = "Источник для синтетической ошибки R51"
            failure_source.content_revision += 1
            db.session.commit()

        ozon_ids_before_failure_selection = selected_browser_state(mobile_page)["ozonIds"]
        mobile_page.get_by_role("checkbox", name=f"Выбрать {REFRESHED_TITLE}").check()
        failure_selection_state = selected_browser_state(mobile_page)
        assert failure_selection_state and failure_selection_state["selected"] == [failure_target_id], {
            "expected_selected_ids": [failure_target_id],
            "actual_browser_state": failure_selection_state,
        }
        assert failure_selection_state["ozonIds"] == ozon_ids_before_failure_selection, {
            "ozon_ids_before_supplier_selection": ozon_ids_before_failure_selection,
            "actual_browser_state": failure_selection_state,
        }
        failure_dialog, failure_preview = open_supplier_refresh_preview(
            mobile_page, [failure_target_id], refresh_preview_posts,
        )
        assert (failure_preview["requested"], failure_preview["eligible"],
                failure_preview["changed"], failure_preview["skipped"]) == (1, 1, 1, 0)

        mobile_result_requests = []

        def record_mobile_result_request(browser_request):
            parsed = urlsplit(browser_request.url)
            if browser_request.method == "GET" and parsed.path == LOCAL_REFRESH_RESULT:
                mobile_result_requests.append(browser_request.headers.get(
                    "x-supplier-refresh-receipt",
                ))

        mobile_page.on("request", record_mobile_result_request)
        refresh_posts_before_zero_update = sum(post["path"] == LOCAL_REFRESH for post in posts)
        posts_before_zero_update = len(posts)
        wb_review_posts_before_zero_update = sum(
            post["path"] == LOCAL_WB_ENRICH_REVIEW for post in posts
        )
        ozon_prepare_posts_before_zero_update = sum(
            post["path"] == LOCAL_OZON_PREPARE for post in posts
        )
        wb_start_posts_before_zero_update = len(wb_enrich_start_posts)
        provider_attempts_before_zero_update = fixture.REPORT["provider_attempts"]
        unexpected_external_before_zero_update = len(fixture.REPORT["unexpected_external_requests"])
        refresh_failure_ids.add(failure_target_id)
        with mobile_page.expect_response(
            lambda response: urlsplit(response.url).path == LOCAL_REFRESH
            and response.request.method == "POST",
        ) as zero_update_response:
            failure_dialog.get_by_role("button", name="Обновить", exact=True).click()
        refresh_failure_ids.clear()
        assert zero_update_response.value.status == 200
        zero_update_data = zero_update_response.value.json()
        assert zero_update_data["success"] is True
        assert (zero_update_data["requested"], zero_update_data["updated"],
                zero_update_data["skipped"], zero_update_data["errors"]) == (1, 0, 0, 1)
        assert zero_update_data["items"] == [{
            "product_id": failure_target_id,
            "status": "error",
            "reason": "update_failed",
        }]
        assert zero_update_data["updated_batch"] is None
        assert zero_update_data["wb_ready"] == 0 and zero_update_data["wb_targets"] == []
        zero_update_receipt = zero_update_data.get("resume_receipt")
        assert isinstance(zero_update_receipt, str) and 0 < len(zero_update_receipt) <= 7500
        assert refresh_failure_injections == [failure_target_id]

        zero_update_result = mobile_page.locator(".mp-refresh-result")
        zero_update_result.get_by_role(
            "heading", name="Обновление завершено с ошибками", exact=True,
        ).wait_for()
        zero_update_result.get_by_text("В запросе товаров: 1.", exact=False).wait_for()
        assert [value.strip() for value in zero_update_result.locator(
            ".mp-refresh-result-counts dd",
        ).all_text_contents()] == ["0", "0", "1"]
        zero_update_result.get_by_text("Карточки и итог (1)", exact=True).click()
        zero_update_row = zero_update_result.locator(".mp-refresh-result-item.is-danger")
        zero_update_row.get_by_text(REFRESHED_TITLE, exact=False).wait_for()
        zero_update_row.get_by_text(
            "Не удалось сохранить данные. Откройте карточку и проверьте её.", exact=True,
        ).wait_for()
        zero_update_open_card = zero_update_row.get_by_role("link", name="Открыть карточку")
        assert zero_update_open_card.count() == 1
        zero_update_open_card_href = zero_update_open_card.get_attribute("href") or ""
        zero_update_href = urlsplit(zero_update_open_card_href)
        assert zero_update_href.path == "/my-products/common-content"
        assert parse_qs(zero_update_href.query) == {"product_id": [str(failure_target_id)]}
        assert zero_update_result.get_by_role("button", name=re.compile(r"^Подготовить для Ozon")).count() == 0
        assert zero_update_result.get_by_role("button", name=re.compile(r"^Проверить характеристики WB")).count() == 0
        assert zero_update_result.get_by_role("button", name=re.compile(r"^Выбрать обновлённые товары")).count() == 0
        assert mobile_page.evaluate(
            "key => sessionStorage.getItem(key)", resume_key,
        ) == zero_update_receipt
        assert sum(post["path"] == LOCAL_REFRESH for post in posts) == refresh_posts_before_zero_update + 1
        assert posts[posts_before_zero_update:] == [{
            "path": LOCAL_REFRESH,
            "body": {"product_ids": [failure_target_id]},
        }]
        with fixture.app.app_context():
            failure_source = db.session.get(SupplierProduct, fixture_ids["refresh_source_id"])
            failure_source.title, failure_source.content_revision = source_before_failure
            db.session.commit()

        zero_update_page_url = mobile_page.url
        zero_update_route = urlsplit(zero_update_page_url)
        assert zero_update_route.path == "/my-products/beta"
        with mobile_page.expect_response(
            lambda response: is_supplier_refresh_result(response)
            and response.request.headers.get("x-supplier-refresh-receipt") == zero_update_receipt,
        ) as zero_update_resumed:
            mobile_page.reload(wait_until="domcontentloaded")
        assert zero_update_resumed.value.status == 200
        zero_update_resumed_data = zero_update_resumed.value.json()
        assert (zero_update_resumed_data["requested"], zero_update_resumed_data["updated"],
                zero_update_resumed_data["skipped"], zero_update_resumed_data["errors"]) == (1, 0, 0, 1)
        assert zero_update_resumed_data["items"] == zero_update_data["items"]
        assert [item["productId"] for item in zero_update_resumed_data["labels"]] == [failure_target_id]
        assert mobile_page.url == zero_update_page_url
        restored_zero_update = mobile_page.locator(".mp-refresh-result")
        restored_zero_update.get_by_role(
            "heading", name="Сохранённый итог обновления от поставщика", exact=True,
        ).wait_for()
        restored_zero_update.get_by_text("В запросе товаров: 1.", exact=False).wait_for()
        restored_zero_update.get_by_text("Карточки и итог (1)", exact=True).click()
        restored_error_row = restored_zero_update.locator(".mp-refresh-result-item.is-danger")
        restored_error_row.get_by_text(REFRESHED_TITLE, exact=False).wait_for()
        restored_error_row.get_by_text(
            "Не удалось сохранить данные. Откройте карточку и проверьте её.", exact=True,
        ).wait_for()
        restored_open_card = restored_error_row.get_by_role("link", name="Открыть карточку")
        assert restored_open_card.get_attribute("href") == zero_update_open_card_href
        assert restored_zero_update.get_by_role("button", name=re.compile(r"^Подготовить для Ozon")).count() == 0
        assert restored_zero_update.get_by_role("button", name=re.compile(r"^Проверить характеристики WB")).count() == 0
        assert restored_zero_update.get_by_role("button", name=re.compile(r"^Выбрать обновлённые товары")).count() == 0
        assert mobile_page.evaluate(
            "key => sessionStorage.getItem(key)", resume_key,
        ) == zero_update_receipt
        assert mobile_result_requests == [zero_update_receipt]
        assert sum(post["path"] == LOCAL_REFRESH for post in posts) == refresh_posts_before_zero_update + 1
        assert len(posts) == posts_before_zero_update + 1
        assert sum(post["path"] == LOCAL_WB_ENRICH_REVIEW for post in posts) == wb_review_posts_before_zero_update
        assert sum(post["path"] == LOCAL_OZON_PREPARE for post in posts) == ozon_prepare_posts_before_zero_update
        assert len(wb_enrich_start_posts) == wb_start_posts_before_zero_update
        assert fixture.REPORT["provider_attempts"] == provider_attempts_before_zero_update == 0
        assert len(fixture.REPORT["unexpected_external_requests"]) == unexpected_external_before_zero_update == 0
        verify_bounds(mobile_page)
        zero_update_screenshot = OUTPUT / "supplier-refresh-zero-updates-error-dark-320.png"
        mobile_page.screenshot(path=zero_update_screenshot, animations="disabled")
        supplier_refresh_zero_update_report = {
            "product_ids": [failure_target_id],
            "requested": zero_update_data["requested"],
            "updated": zero_update_data["updated"],
            "skipped": zero_update_data["skipped"],
            "errors": zero_update_data["errors"],
            "refresh_apply_posts": sum(post["path"] == LOCAL_REFRESH for post in posts)
                - refresh_posts_before_zero_update,
            "restore_result_gets": len(mobile_result_requests),
            "wb_handoff_posts": (
                sum(post["path"] == LOCAL_WB_ENRICH_REVIEW for post in posts)
                - wb_review_posts_before_zero_update
                + len(wb_enrich_start_posts) - wb_start_posts_before_zero_update
            ),
            "ozon_handoff_posts": (
                sum(post["path"] == LOCAL_OZON_PREPARE for post in posts)
                - ozon_prepare_posts_before_zero_update
            ),
            "provider_attempts": (
                fixture.REPORT["provider_attempts"] - provider_attempts_before_zero_update
            ),
            "screenshot": zero_update_screenshot.name,
        }
        assert supplier_refresh_zero_update_report["refresh_apply_posts"] == 1
        assert supplier_refresh_zero_update_report["restore_result_gets"] == 1
        assert supplier_refresh_zero_update_report["wb_handoff_posts"] == 0
        assert supplier_refresh_zero_update_report["ozon_handoff_posts"] == 0
        assert supplier_refresh_zero_update_report["provider_attempts"] == 0
        checks.append("supplier_refresh_zero_update_error_keeps_signed_result_on_reload_without_handoff")
        mobile_context.close()

        assert not fixture.REPORT["unexpected_http"] and not fixture.REPORT["unexpected_external_requests"]
        assert fixture.REPORT["provider_attempts"] == 0
        browser.close()
    result = {
        "status": "passed",
        "layouts": layouts,
        "checks": checks,
        "provider_attempts": 0,
        "wb_bulk_edit_handoff": wb_bulk_edit_handoff,
        "supplier_refresh_zero_update": supplier_refresh_zero_update_report,
    }
    (OUTPUT / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        run()
    finally:
        fixture.server.shutdown()
        fixture.TEMP.cleanup()
