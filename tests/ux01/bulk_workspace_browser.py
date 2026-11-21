"""Exercise the bulk workspace with an isolated database and no provider I/O."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.ux01 import journey_browser as fixture
from models import BackgroundJob, EnrichmentJob, ImportedProduct, Product, Seller, SupplierProduct, db


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
LOCAL_SUPPLIER_IMPORT = "/supplier-catalog/import"


def seed_bulk_selection():
    with fixture.app.app_context():
        seller_id = fixture.FIXTURE["seller_id"]
        seller = db.session.get(Seller, seller_id)
        seller.wb_api_key = "synthetic-wb-key-for-bulk-workspace-only"
        wb_product = Product(id=4419902, seller_id=seller_id, nm_id=4419901, vendor_code="BULK-WB-001",
                             title=MAIN_TITLE, is_active=True, wb_price=600)
        db.session.add(wb_product)
        db.session.flush()
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
                "bulk_ids": [row.id for row in bulk_rows]}


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


def pick_action(page, name):
    page.get_by_role("button", name="Что сделать").click()
    action = page.get_by_role("menuitem", name=name)
    assert action.is_enabled(), name
    action.click()


def feed_matches(response, expected):
    parsed = urlsplit(response.url)
    if parsed.path != "/api/my-products" or response.request.method != "GET":
        return False
    query = parse_qs(parsed.query)
    return all(query.get(key) == [value] for key, value in expected.items())


def is_supplier_refresh_result(response):
    parsed = urlsplit(response.url)
    return parsed.path == LOCAL_REFRESH_RESULT and response.request.method == "GET"


def is_supplier_refresh_preview(response):
    parsed = urlsplit(response.url)
    return parsed.path == LOCAL_REFRESH_PREVIEW and response.request.method == "POST"


def open_supplier_refresh_preview(page, expected_ids, preview_posts):
    """Open the real no-provider review and keep its signed token only in RAM."""
    page.get_by_role("button", name="Что сделать").click()
    action = page.get_by_role("menuitem", name="Обновить из поставщика")
    assert action.is_enabled()
    with page.expect_response(is_supplier_refresh_preview) as response_info:
        action.click()
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
    errors = []
    feed_gets = []
    feed_responses = []
    bulk_photo_ids = set(fixture_ids["bulk_ids"])
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

    def bridge(route):
        nonlocal refresh_response_override, refresh_response_status
        nonlocal delay_next_full_select, server_delay_next_full_select
        nonlocal override_over_limit_initial, over_limit_initial_total, mock_over_limit_full
        request = route.request
        parsed = urlsplit(request.url)
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
            assert urlsplit(page.get_by_role("link", name="Импортировать товары").get_attribute("href")).path == "/supplier-catalog"
            ozon_prices = page.get_by_role("link", name="Ценовые заявки Ozon").get_attribute("href")
            assert parse_qs(urlsplit(ozon_prices).query) == {
                "proposal_kind": ["price"], "status": ["pending_review"],
                "account_id": [str(fixture.FIXTURE["account_id"])],
            }
            page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").check()
            primary = page.get_by_role("button", name="Что сделать")
            primary.focus()
            primary.press("ArrowDown")
            menu = page.get_by_role("menu")
            menu.locator("button:focus").wait_for()
            assert page.get_by_role("menuitem", name="Изменить общий контент").is_enabled()
            assert page.get_by_role("menuitem", name="Изменить цены WB").is_enabled()
            verify_bounds(page)
            screenshot = OUTPUT / f"selected-{theme}-{width}.png"
            page.screenshot(path=screenshot, animations="disabled")
            menu.locator("button:focus").press("Escape")
            assert primary.evaluate("element => element === document.activeElement")
            more = page.locator(".mp-more-actions summary")
            more.press("Enter")
            secondary = page.locator(".mp-more-actions-menu")
            secondary.wait_for()
            bounds = secondary.bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width, bounds
            if width <= 390:
                assert primary.bounding_box()["height"] >= 44 and more.bounding_box()["height"] >= 44
            primary.press("ArrowDown")
            menu.locator("button:focus").wait_for()
            assert not more.evaluate("element => element.parentElement.open")
            layouts.append({"width": width, "theme": theme, "screenshot": screenshot.name})
            context.close()
        checks.append("action_menu_keyboard_and_secondary_overlay_fit_all_layouts")

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
        pick_action(page, "Изменить цены WB")
        page.wait_for_url(lambda value: (
            urlsplit(str(value)).path == "/prices/change"
            and re.fullmatch(r"workspace-return=[a-f0-9]{32}", urlsplit(str(value)).fragment)
        ))
        price_url = urlsplit(page.url)
        nonce_hash = price_url.fragment
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
        assert (unlinked_preview["eligible"], unlinked_preview["changed"],
                unlinked_preview["skipped"]) == (0, 0, 1)
        assert unlinked_preview["items"][0]["state"] == "unlinked"
        assert dialog.get_by_text(MANUAL_TITLE, exact=False).is_visible()
        assert dialog.get_by_text("Нет связи с каталогом поставщика", exact=True).is_visible()
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
            "heading", name="Обновление из поставщика завершено", exact=True,
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
            "heading", name="Сохранённый итог обновления из поставщика", exact=True,
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
        my_products_link = page.locator(".sh-page-header .sh-page-actions").get_by_role(
            "link", name="Мои товары", exact=True,
        )
        ozon_return_url = "/my-products/beta"
        assert my_products_link.get_attribute("href") == ozon_return_url
        refresh_posts_before_ozon_return = sum(post["path"] == LOCAL_REFRESH for post in posts)
        with page.expect_response(
            lambda response: is_supplier_refresh_result(response)
            and response.request.headers.get("x-supplier-refresh-receipt") == resume_receipt,
        ) as ozon_resumed:
            my_products_link.click()
        assert_resumed_refresh(ozon_resumed.value)
        page.wait_for_url(fixture.BASE + ozon_return_url, wait_until="domcontentloaded")
        restored_result = page.locator(".mp-refresh-result")
        restored_result.get_by_role(
            "heading", name="Сохранённый итог обновления из поставщика", exact=True,
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

        result_gets_before_hide = len(resume_result_requests)
        refresh_posts_before_hide = sum(post["path"] == LOCAL_REFRESH for post in posts)
        page.get_by_role("button", name="Скрыть итог обновления", exact=True).click()
        assert page.locator(".mp-refresh-result").count() == 0
        assert page.evaluate("key => sessionStorage.getItem(key) === null", resume_key)
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
        open_products(preview_page)
        preview_page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").check()
        preview_page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        preview_posts_before = len(refresh_preview_posts)
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
        assert title_change.locator("dd").inner_text().count("→") == 1
        assert MAIN_TITLE in title_change.locator("dd").inner_text()
        assert REFRESHED_TITLE in title_change.locator("dd").inner_text()
        quantity_text = quantity_change.locator("dd").inner_text()
        assert "3" in quantity_text and "0" in quantity_text and "→" in quantity_text
        assert dialog.get_by_text("Выбрано", exact=True).is_visible()
        assert dialog.get_by_text("Можно обновить", exact=True).is_visible()
        assert dialog.get_by_text("Изменятся", exact=True).is_visible()
        assert dialog.get_by_text("Без связи", exact=True).is_visible()
        assert dialog.get_by_text(MAIN_TITLE, exact=True).is_visible()
        assert dialog.get_by_text(REFRESHED_TITLE, exact=True).is_visible()
        assert dialog.get_by_text("Название", exact=True).is_visible()
        assert dialog.get_by_text("Остаток у поставщика", exact=True).is_visible()
        assert dialog.get_by_text("0", exact=True).is_visible()
        assert dialog.get_by_text(MANUAL_TITLE, exact=True).is_visible()
        assert dialog.get_by_text("Нет связи с каталогом поставщика", exact=True).is_visible()
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
        assert len(posts) == 3, posts
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
        mobile_context.close()

        assert not fixture.REPORT["unexpected_http"] and not fixture.REPORT["unexpected_external_requests"]
        assert fixture.REPORT["provider_attempts"] == 0
        browser.close()
    result = {"status": "passed", "layouts": layouts, "checks": checks, "provider_attempts": 0}
    (OUTPUT / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        run()
    finally:
        fixture.server.shutdown()
        fixture.TEMP.cleanup()
