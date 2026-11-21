"""Synthetic browser proof for supplier import selection and WB create result.

Uses the isolated app, SQLite fixture, network-denying browser bridge, and
synthetic seller session from ``journey_browser``. Browser writes are limited
to the normal local supplier import form and one exact WB bulk-create POST. A
scoped fake importer supplies deterministic WB outcomes after the real route
and BackgroundJob worker perform their normal seller scope/readiness checks.

Run with the repository venv and Chromium installed. The summary is written to
``/artifacts/supplier-filtered-import.json`` by default.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import sync_playwright


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.ux01 import journey_browser as fixture
from models import (
    ImportedProduct,
    PricingSettings,
    Seller,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    db,
)


REPORT_PATH = Path(os.environ.get(
    "SUPPLIER_FILTERED_IMPORT_REPORT",
    "/artifacts/supplier-filtered-import.json",
))
IMPORT_PATH = "/supplier-catalog/import"
WB_CREATE_PATH = "/my-products/push-to-wb-bulk"
JOB_STATUS_PREFIX = "/api/jobs/"
FILTERS = {
    "search": "R28 Mass batch",
    "stock_status": "in_stock",
    "ai_filter": "ai_parsed",
    "wb_filter": "not_on_wb",
    "show_imported": "1",
}


def seed_supplier_products():
    """Create 53 eligible current-supplier matches plus exact exclusions.

    The eligible source rows also carry enough observed facts to pass the real
    WB upload-readiness validator after the normal supplier import. This keeps
    the later mixed result focused on the importer outcome, not missing seed
    data.
    """
    with fixture.app.app_context():
        seller_id = fixture.FIXTURE["seller_id"]
        supplier_id = fixture.FIXTURE["supplier_id"]
        seller = db.session.get(Seller, seller_id)
        assert seller is not None
        seller.wb_api_key = "synthetic-wb-key-never-sent"
        pricing_settings = PricingSettings.query.filter_by(seller_id=seller_id).first()
        if pricing_settings is None:
            db.session.add(PricingSettings(
                seller_id=seller_id,
                is_enabled=True,
            ))
        else:
            pricing_settings.is_enabled = True
        seller_supplier = SellerSupplier.query.filter_by(
            seller_id=seller_id,
            supplier_id=supplier_id,
            is_active=True,
        ).one()
        assert seller_supplier

        eligible = []
        for index in range(53):
            product = SupplierProduct(
                supplier_id=supplier_id,
                external_id=f"R28-MASS-{index:03d}",
                vendor_code=f"R28-MASS-{index:03d}",
                title=f"R28 Mass batch item {index:03d}",
                description=(
                    f"Synthetic supplier description for WB readiness item {index:03d}. "
                    "This fixture text is local and is not sent to a provider."
                ),
                brand="R28 Synthetic Brand",
                category="Synthetic supplier category",
                wb_category_name="Synthetic WB category",
                wb_subject_id=7000,
                wb_subject_name="Synthetic WB subject",
                supplier_price=100 + index,
                currency="RUB",
                supplier_quantity=4,
                supplier_status="in_stock",
                status="ready",
                ai_parsed_at=datetime(2026, 11, 21),
                photo_urls_json=json.dumps([fixture.PHOTO]),
                colors_json=json.dumps(["Blue"]),
                materials_json=json.dumps(["Cotton"]),
                gender="unisex",
                country="Russia",
                characteristics_json=json.dumps({
                    "Synthetic material": "Cotton",
                    "Synthetic color": "Blue",
                    "Synthetic use": "Daily",
                }),
            )
            db.session.add(product)
            eligible.append(product)

        # These source rows match the search but must not widen the exact
        # active-supplier/filter result.
        out_of_stock = SupplierProduct(
            supplier_id=supplier_id,
            external_id="R28-MASS-OUT",
            title="R28 Mass batch out of stock",
            supplier_quantity=0,
            supplier_status="out_of_stock",
            status="ready",
            ai_parsed_at=datetime(2026, 11, 21),
        )
        not_parsed = SupplierProduct(
            supplier_id=supplier_id,
            external_id="R28-MASS-NO-AI",
            title="R28 Mass batch not parsed",
            supplier_quantity=3,
            supplier_status="in_stock",
            status="ready",
        )
        other_supplier = Supplier(
            name="R28 Mass other supplier",
            code="r28-mass-other-supplier",
            is_active=True,
        )
        db.session.add_all([out_of_stock, not_parsed, other_supplier])
        db.session.flush()
        db.session.add(SellerSupplier(
            seller_id=seller_id,
            supplier_id=other_supplier.id,
            is_active=True,
        ))
        foreign_source = SupplierProduct(
            supplier_id=other_supplier.id,
            external_id="R28-MASS-OTHER",
            title="R28 Mass batch from other supplier",
            supplier_quantity=5,
            supplier_status="in_stock",
            status="ready",
            ai_parsed_at=datetime(2026, 11, 21),
        )
        db.session.add(foreign_source)

        # More than 200 rows use a separate search so the same route's
        # explicit fallback can be checked without changing the 53-row proof.
        oversized = [
            SupplierProduct(
                supplier_id=supplier_id,
                external_id=f"R28-OVERSIZE-{index:03d}",
                title=f"R28 Oversize batch item {index:03d}",
                supplier_quantity=2,
                supplier_status="in_stock",
                status="ready",
            )
            for index in range(201)
        ]
        db.session.add_all(oversized)
        db.session.commit()
        imported_before = ImportedProduct.query.count()
        return {
            "supplier_id": supplier_id,
            "eligible_ids": [str(product.id) for product in eligible],
            "excluded_ids": [
                str(out_of_stock.id), str(not_parsed.id), str(foreign_source.id),
            ],
            "oversized_count": len(oversized),
            "imported_before": imported_before,
        }


def run():
    fixture_ids = seed_supplier_products()
    checks = []
    posts = []
    wb_create_posts = []
    product_feed_reads = []
    facet_reads = []
    job_status_reads = []
    page_errors = []
    event_number = [0]
    wb_create_report = None

    def next_event():
        event_number[0] += 1
        return event_number[0]

    def bridge(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.netloc == urlsplit(fixture.BASE).netloc and request.method == "GET":
            if parsed.path == "/api/my-products":
                query = parse_qs(parsed.query, keep_blank_values=True)
                try:
                    response = route.fetch(max_redirects=0)
                except Exception:
                    raise AssertionError("local product feed request failed") from None
                payload = None
                if "import_batch" in query:
                    try:
                        payload = response.json()
                    except Exception:
                        payload = None
                product_feed_reads.append({
                    "sequence": next_event(),
                    "query_keys": tuple(sorted(query)),
                    "batch_tokens": tuple(query.get("import_batch", [])),
                    "status": response.status,
                    "payload": payload,
                })
                if response.status >= 400:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
                route.fulfill(response=response)
                return
            if (
                parsed.path.startswith(JOB_STATUS_PREFIX)
                and parsed.path.removeprefix(JOB_STATUS_PREFIX)
                not in {"active", "recent"}
            ):
                try:
                    response = route.fetch(max_redirects=0)
                except Exception:
                    raise AssertionError("local WB job status request failed") from None
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                job_status_reads.append({
                    "sequence": next_event(),
                    "method": request.method,
                    "path": parsed.path,
                    "status": response.status,
                    "payload": payload,
                })
                if response.status >= 400:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
                route.fulfill(response=response)
                return
            if parsed.path == "/api/my-products/facets":
                facet_reads.append(tuple(sorted(parse_qs(parsed.query, keep_blank_values=True))))
                try:
                    response = route.fetch(max_redirects=0)
                except Exception:
                    raise AssertionError("local product facets request failed") from None
                if response.status >= 400:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
                route.fulfill(response=response)
                return
        if parsed.netloc == urlsplit(fixture.BASE).netloc and request.method == "POST":
            assert parsed.path in {IMPORT_PATH, WB_CREATE_PATH}, (
                request.method, parsed.path,
            )
            if parsed.path == IMPORT_PATH:
                body = parse_qs(request.post_data or "", keep_blank_values=True)
                assert body.get("csrf_token"), "normal supplier import form must carry CSRF"
                posts.append({"path": parsed.path, "body": body})
            else:
                assert "application/json" in (request.headers.get("content-type") or "")
                assert request.headers.get("x-csrftoken"), "WB create POST must carry CSRF"
                try:
                    body = request.post_data_json
                except Exception:
                    raise AssertionError("WB create POST must carry JSON") from None
                wb_create_posts.append({
                    "sequence": next_event(),
                    "path": parsed.path,
                    "body": body,
                })
            fixture.REPORT["writes"].append({"method": "POST", "path": parsed.path})
            response = route.fetch(max_redirects=0)
            route.fulfill(response=response)
            return
        fixture.bridge(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=fixture.CHROMIUM,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_init_script("localStorage.setItem('sh-theme', 'dark');")
        context.add_cookies([fixture.synthetic_login_cookie()])
        context.route("**/*", bridge)
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        normal_path = "/supplier-catalog/{}/products?{}".format(
            fixture_ids["supplier_id"], urlencode(FILTERS),
        )
        response = page.goto(
            fixture.BASE + normal_path,
            wait_until="domcontentloaded",
        )
        assert response and response.status == 200
        page.wait_for_load_state("networkidle")
        cards = page.locator(".grid > div.p-4")
        assert cards.count() == 50, cards.count()
        with fixture.app.app_context():
            assert ImportedProduct.query.count() == fixture_ids["imported_before"]
        standard_checks = page.locator(
            'input[form="supplier-import-form"][name="product_ids"]:checked',
        )
        assert standard_checks.count() == 0, standard_checks.count()
        all_filter_link = page.get_by_role(
            "link", name="Проверить все товары текущего фильтра (53)",
        )
        href = all_filter_link.get_attribute("href")
        assert href
        parsed_href = urlsplit(href)
        assert parsed_href.path == (
            f"/supplier-catalog/{fixture_ids['supplier_id']}/products"
        ), href
        assert parse_qs(parsed_href.query) == {
            **{key: [value] for key, value in FILTERS.items()},
            "page": ["1"],
            "select_all": ["1"],
        }, href
        checks.append("ordinary_page_keeps_50_rows_and_preserves_exact_filters_for_selection")

        all_filter_link.click()
        page.wait_for_load_state("networkidle")
        with fixture.app.app_context():
            assert ImportedProduct.query.count() == fixture_ids["imported_before"]
        all_checkboxes = page.locator(
            'input[form="supplier-import-form"][name="product_ids"]',
        )
        assert all_checkboxes.count() == 53, all_checkboxes.count()
        rendered_ids = all_checkboxes.evaluate_all(
            "items => items.map(item => item.value)",
        )
        assert set(rendered_ids) == set(fixture_ids["eligible_ids"])
        assert len(set(rendered_ids)) == 53
        assert all_checkboxes.evaluate_all(
            "items => items.every(item => item.checked)",
        )
        review_text = page.locator(
            '[data-supplier-products] [aria-live="polite"] strong',
        ).inner_text()
        assert review_text == "Выбрано 53 из 53 по текущему фильтру.", review_text
        for excluded_id in fixture_ids["excluded_ids"]:
            assert excluded_id not in rendered_ids, excluded_id
        assert page.get_by_text("R28 Mass batch out of stock", exact=True).count() == 0
        assert page.get_by_text("R28 Mass batch not parsed", exact=True).count() == 0
        assert page.get_by_text("R28 Mass batch from other supplier", exact=True).count() == 0
        assert page.get_by_role("link", name="Отменить массовый выбор").count() == 1
        checks.append("all_filtered_result_checks_exact_53_current_supplier_rows_and_excludes_other_matches")

        excluded_ids = set(rendered_ids[:3])
        for product_id in excluded_ids:
            page.locator(
                f'input[form="supplier-import-form"][name="product_ids"][value="{product_id}"]',
            ).uncheck()
        review_text = page.locator(
            '[data-supplier-products] [aria-live="polite"] strong',
        ).inner_text()
        assert review_text == "Выбрано 50 из 53 по текущему фильтру.", review_text

        selected_ids = set(rendered_ids) - excluded_ids
        submitted = page.evaluate("""() => ({
            supplierIds: new FormData(document.querySelector('#supplier-import-form')).getAll('supplier_id'),
            productIds: new FormData(document.querySelector('#supplier-import-form')).getAll('product_ids'),
        })""")
        assert submitted["supplierIds"] == [str(fixture_ids["supplier_id"])]
        assert set(submitted["productIds"]) == selected_ids
        assert len(submitted["productIds"]) == 50
        checks.append("three_explicit_exclusions_update_count_and_normal_form_contains_exact_50_ids")

        with page.expect_response(
            lambda result: urlsplit(result.url).path == IMPORT_PATH
            and result.request.method == "POST",
        ) as imported:
            page.get_by_role("button", name="Импортировать товары").click()
        assert imported.value.status == 303, "supplier import must redirect to its exact committed set"
        redirect = urlsplit(imported.value.headers.get("location", ""))
        redirect_query = parse_qs(redirect.query, keep_blank_values=True)
        assert redirect.path == "/my-products/beta", "supplier import must open the modern workspace"
        assert set(redirect_query) == {"import_batch"}, "redirect must carry only the signed import batch"
        batch_tokens = redirect_query.get("import_batch", [])
        assert len(batch_tokens) == 1 and 0 < len(batch_tokens[0]) <= 8192, (
            "redirect must carry one bounded signed import batch",
        )
        batch_token = batch_tokens[0]
        safe_page_errors = lambda: [
            error.replace(batch_token, "[redacted]") for error in page_errors
        ]

        def assert_batch_page(expected_product_ids):
            page.get_by_role("heading", name="Товары из импорта", exact=True).wait_for()
            page.wait_for_load_state("networkidle")
            location = page.evaluate("() => ({path: location.pathname, search: location.search})")
            location_query = parse_qs(location["search"].removeprefix("?"), keep_blank_values=True)
            assert location["path"] == "/my-products/beta", "batch page path changed"
            assert set(location_query) == {"import_batch"}, "batch page lost its exact scope"
            assert location_query.get("import_batch") == [batch_token], (
                "batch page scope differs from the redirect",
            )
            assert page.locator(".mp-list > .mp-row").count() == 50, (
                "batch page must render exactly 50 imported rows",
            )
            state = page.evaluate("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy ? {
                    importBatchMode: proxy.importBatchMode,
                    items: proxy.items.map(item => item.id),
                    selected: proxy.selected.slice(),
                    loading: proxy.loading,
                } : null;
            }""")
            assert state is not None and state["importBatchMode"] and not state["loading"], (
                "batch page did not finish loading its fixed scope",
            )
            assert len(state["items"]) == 50 and set(state["items"]) == expected_product_ids, (
                "batch UI differs from the persisted imported product set",
            )
            return state

        assert len(posts) == 1, posts
        post_body = posts[0]["body"]
        assert post_body.get("supplier_id") == [str(fixture_ids["supplier_id"])]
        assert set(post_body.get("product_ids", [])) == selected_ids
        assert len(post_body.get("product_ids", [])) == 50
        with fixture.app.app_context():
            persisted = ImportedProduct.query.filter(
                ImportedProduct.seller_id == fixture.FIXTURE["seller_id"],
                ImportedProduct.supplier_id == fixture_ids["supplier_id"],
                ImportedProduct.supplier_product_id.in_(fixture_ids["eligible_ids"]),
            ).all()
            persisted_source_ids = {
                str(row.supplier_product_id) for row in persisted
            }
            assert persisted_source_ids == selected_ids, "only the exact selected source rows may persist"
            persisted_product_ids = {row.id for row in persisted}
            assert len(persisted_product_ids) == 50
        imported_count = len(persisted_source_ids)
        with fixture.app.app_context():
            assert ImportedProduct.query.count() == fixture_ids["imported_before"] + imported_count
        try:
            first_state = assert_batch_page(persisted_product_ids)
            assert first_state["selected"] == [], "import batch must not preselect rows"
            assert len(product_feed_reads) == 1, "batch view must make one product feed read"
            assert product_feed_reads[0]["query_keys"] == ("import_batch",)
            assert product_feed_reads[0]["batch_tokens"] == (batch_token,)
            assert product_feed_reads[0]["status"] == 200
            first_payload = product_feed_reads[0]["payload"]
            pagination = first_payload.get("pagination") if isinstance(first_payload, dict) else None
            first_items = first_payload.get("items") if isinstance(first_payload, dict) else None
            assert isinstance(first_payload, dict) and first_payload.get("success") is True
            assert first_payload.get("import_batch_count") == 50
            assert isinstance(first_items, list) and len(first_items) == 50
            assert {item.get("id") for item in first_items if isinstance(item, dict)} == persisted_product_ids
            assert isinstance(pagination, dict) and pagination == {
                "page": 1,
                "per_page": 200,
                "pages": 1,
                "total": 50,
                "has_next": False,
            }
            assert facet_reads == [], "batch view must not request global facets"
            checks.append("modern_redirect_loads_exactly_50_persisted_seller_owned_rows_without_facets")

            page.get_by_role("checkbox", name="Выбрать товары из импорта").check()
            selected_state = page.evaluate("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy ? proxy.selected.slice() : null;
            }""")
            assert selected_state is not None and set(selected_state) == persisted_product_ids
            page.locator(".mp-list > .mp-row").first.locator(".mp-check input").uncheck()
            selected_after_one = page.evaluate("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy ? proxy.selected.slice() : null;
            }""")
            assert selected_after_one is not None and len(selected_after_one) == 49
            assert set(selected_after_one) < persisted_product_ids
            assert len(product_feed_reads) == 1 and facet_reads == []
            page.get_by_role("button", name="Что сделать").click()
            page.get_by_role("menuitem", name="Подготовить для Ozon", exact=False).click()
            ozon_dialog = page.locator('dialog[aria-labelledby="mp-ozon-dialog-title"]')
            ozon_dialog.wait_for(state="visible")
            assert page.locator("#mp-ozon-dialog-title").inner_text().startswith("Подготовить 49 ")
            assert page.locator("#mp-ozon-account").input_value()
            assert not page.locator(".mp-ozon-confirm input").is_checked()
            assert page.get_by_role("button", name="Подготовить черновики").is_disabled()
            assert len(posts) == 1 and fixture.REPORT["provider_attempts"] == 0
            assert len(product_feed_reads) == 1 and facet_reads == []
            page.get_by_role("button", name="Вернуться", exact=True).click()
            ozon_dialog.wait_for(state="hidden")
            checks.append("explicit_import_selection_can_review_49_for_ozon_without_confirming_a_write")

            page.reload(wait_until="domcontentloaded")
            reloaded_state = assert_batch_page(persisted_product_ids)
            assert reloaded_state["selected"] == []
            assert len(product_feed_reads) == 2, "reload must re-read the exact batch once"
            assert all(read["query_keys"] == ("import_batch",) for read in product_feed_reads)
            assert all(read["batch_tokens"] == (batch_token,) for read in product_feed_reads)
            assert all(read["status"] == 200 for read in product_feed_reads)
            reloaded_payload = product_feed_reads[1]["payload"]
            reloaded_items = reloaded_payload.get("items") if isinstance(reloaded_payload, dict) else None
            assert isinstance(reloaded_payload, dict) and reloaded_payload.get("success") is True
            assert reloaded_payload.get("import_batch_count") == 50
            assert isinstance(reloaded_items, list) and len(reloaded_items) == 50
            assert {item.get("id") for item in reloaded_items if isinstance(item, dict)} == persisted_product_ids
            assert reloaded_payload.get("pagination") == pagination
            assert facet_reads == []
            page.set_viewport_size({"width": 320, "height": 900})
            dimensions = page.evaluate("""() => ({
                scroll: document.documentElement.scrollWidth,
                viewport: document.documentElement.clientWidth,
            })""")
            assert dimensions["viewport"] == 320 and dimensions["scroll"] <= 321, (
                "import batch must fit a 320px viewport",
            )
            screenshot_name = "supplier-filtered-import-batch-dark-320.png"
            page.screenshot(path=REPORT_PATH.parent / screenshot_name, animations="disabled")
            page.get_by_role("link", name="Все мои товары", exact=True).click()
            page.get_by_role("heading", name="Мои товары", exact=True).wait_for()
            page.wait_for_load_state("networkidle")
            normal_location = page.evaluate("() => ({path: location.pathname, search: location.search})")
            assert normal_location["path"] == "/my-products/beta"
            assert "import_batch" not in parse_qs(
                normal_location["search"].removeprefix("?"), keep_blank_values=True,
            )
            assert len([read for read in product_feed_reads if "import_batch" not in read["query_keys"]]) >= 1
            assert len(facet_reads) >= 1
            assert len(posts) == 1 and fixture.REPORT["provider_attempts"] == 0
            checks.append("batch_reload_keeps_exact_50_then_all_my_products_exits_to_normal_workspace_at_320px")
        except Exception as error:
            message = str(error).replace(batch_token, "[redacted]")
            raise AssertionError(f"import batch browser assertion failed: {message}") from None
        checks.append("normal_post_persists_only_the_exact_selected_owned_supplier_rows")
        checks.append("import_batch_mobile_screenshot_saved")

        # Reopen the signed receipt after the R31 53 → 50 → 49 Ozon review.
        # This terminal WB create proof selects only two retained rows, so one
        # actual partial result does not depend on the whole import batch.
        batch_query = urlencode({"import_batch": batch_token})
        batch_response = page.goto(
            fixture.BASE + "/my-products/beta?" + batch_query,
            wait_until="domcontentloaded",
        )
        assert batch_response and batch_response.status == 200
        batch_state = assert_batch_page(persisted_product_ids)
        assert batch_state["selected"] == []
        assert product_feed_reads[-1]["query_keys"] == ("import_batch",)
        assert product_feed_reads[-1]["batch_tokens"] == (batch_token,)
        assert product_feed_reads[-1]["status"] == 200
        assert product_feed_reads[-1]["payload"].get("import_batch_count") == 50
        page.set_viewport_size({"width": 320, "height": 900})
        batch_item_ids = page.evaluate("""() => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy ? proxy.items.map(item => item.id) : null;
        }""")
        assert isinstance(batch_item_ids, list) and len(batch_item_ids) == 50
        wb_selected_ids = batch_item_ids[-2:]
        assert len(wb_selected_ids) == 2 and len(set(wb_selected_ids)) == 2
        assert set(wb_selected_ids) <= persisted_product_ids
        for product_id in wb_selected_ids:
            page.locator(
                f'.mp-row .mp-check input[type="checkbox"][value="{product_id}"]',
            ).check()
        before_confirmation_position = page.evaluate("""() => {
            const selectedRows = Array.from(document.querySelectorAll('.mp-row.is-selected'));
            const rects = selectedRows.map(row => row.getBoundingClientRect());
            return {
                scrollY: window.scrollY,
                viewportHeight: window.innerHeight,
                pageHeight: document.documentElement.scrollHeight,
                selectedCount: selectedRows.length,
                selectedRowsVisible: rects.length === 2 && rects.every(rect =>
                    rect.top >= 0 && rect.bottom <= window.innerHeight
                ),
            };
        }""")
        assert before_confirmation_position["scrollY"] > 0, (
            "selecting the last two signed-batch rows must scroll the page down",
            before_confirmation_position,
        )
        assert before_confirmation_position["pageHeight"] > before_confirmation_position["viewportHeight"]
        assert before_confirmation_position["selectedCount"] == 2
        assert before_confirmation_position["selectedRowsVisible"], before_confirmation_position
        checks.append("last_two_signed_batch_rows_are_selected_from_a_measured_scrolled_position")
        selected_before_create = page.evaluate("""() => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy ? proxy.selected.slice() : null;
        }""")
        assert selected_before_create == wb_selected_ids
        page.get_by_role("button", name="Что сделать").click()
        wb_create_action = page.get_by_role(
            "menuitem", name="Создать карточки WB",
        )
        wb_create_action.wait_for(state="visible")
        assert wb_create_action.is_enabled(), "WB create must be enabled for the two retained rows"
        assert wb_create_action.locator(".shk-actions-item-label").inner_text() == "Создать карточки WB"
        assert wb_create_action.locator(".shk-actions-item-hint").inner_text() == (
            "для 2 товаров, ещё не связанных с WB"
        ), "action hint must name the exact two-row WB create scope"
        wb_create_action.click()
        confirmation = page.get_by_role("alertdialog")
        confirmation.wait_for(state="visible")
        confirmation_scroll_y = page.evaluate("() => window.scrollY")
        assert confirmation_scroll_y > 0, (
            "confirmation should preserve the scrolled batch context",
            confirmation_scroll_y,
        )

        from services.upload_readiness_validator import validate_product_upload_readiness
        from services.wb_product_importer import WBProductImporter

        fake_wb_calls = []
        original_batch_import = WBProductImporter.batch_import_products_to_wb

        def deterministic_wb_batch_import(self, imported_products, progress_callback=None):
            product_ids = [product.id for product in imported_products]
            assert product_ids == wb_selected_ids, (
                "real BackgroundJob worker widened or reordered the selected product set",
            )
            checks_by_product = []
            for product in imported_products:
                readiness = validate_product_upload_readiness(
                    product,
                    seller=db.session.get(Seller, fixture.FIXTURE["seller_id"]),
                )
                checks_by_product.append({
                    "id": product.id,
                    "status": product.import_status,
                    "is_ready": readiness["is_ready"],
                    "can_push": readiness["can_push"],
                })
            assert all(
                item["status"] == "validated" and item["is_ready"] and item["can_push"]
                for item in checks_by_product
            ), checks_by_product

            outcomes = []
            for index, product in enumerate(imported_products):
                success = index == 0
                outcomes.append({
                    "product_id": product.id,
                    "title": (product.title or "")[:50],
                    "status": "success" if success else "failed",
                    "error": None if success else "Synthetic WB rejected this fixture card",
                    "nm_id": 700000 + index if success else None,
                    "warnings": [],
                })
            fake_wb_calls.append({
                "product_ids": product_ids,
                "readiness": checks_by_product,
                "outcomes": outcomes,
            })
            if progress_callback:
                progress_callback("done", {"items": outcomes})
            return outcomes

        WBProductImporter.batch_import_products_to_wb = deterministic_wb_batch_import
        try:
            create_button = confirmation.get_by_role(
                "button", name="Создать карточки", exact=True,
            )
            assert create_button.is_enabled()
            status_reads_before_create = len(job_status_reads)
            feed_reads_before_create = len(product_feed_reads)
            facet_reads_before_create = len(facet_reads)
            with page.expect_response(
                lambda response: urlsplit(response.url).path == WB_CREATE_PATH
                and response.request.method == "POST",
            ) as created:
                create_button.click()
            create_response = created.value
            assert create_response.status == 200, (
                "normal WB create action must queue a BackgroundJob",
                create_response.status,
            )
            confirmation.wait_for(state="hidden")
            create_payload = create_response.json()
            assert isinstance(create_payload, dict) and create_payload.get("success") is True
            job_uid = create_payload.get("job_uid")
            assert isinstance(job_uid, str) and re.fullmatch(
                r"bulk_wb_[0-9a-f]{12}", job_uid,
            ), "WB create response must identify the actual durable job"
            assert page.get_by_role("alertdialog").count() == 0
            assert len(wb_create_posts) == 1, wb_create_posts
            create_post = wb_create_posts[0]
            assert create_post["path"] == WB_CREATE_PATH
            assert set(create_post["body"]) == {"product_ids"}
            assert create_post["body"]["product_ids"] == wb_selected_ids
            assert set(create_post["body"]["product_ids"]) <= persisted_product_ids
            checks.append("signed_import_batch_posts_one_exact_two_product_wb_create_request")

            job_panel = page.locator(".shk-job")
            job_panel.wait_for(state="visible")
            page.evaluate("""() => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
            job_position_before_screenshot = page.evaluate("""() => {
                const panel = document.querySelector('.shk-job');
                const mobileHeader = document.querySelector('.mobile-header');
                if (!panel || !mobileHeader) return null;
                const panelRect = panel.getBoundingClientRect();
                const headerRect = mobileHeader.getBoundingClientRect();
                const panelStyle = getComputedStyle(panel);
                return {
                    focused: document.activeElement === panel,
                    outlineStyle: panelStyle.outlineStyle,
                    outlineWidth: parseFloat(panelStyle.outlineWidth) || 0,
                    scrollY: window.scrollY,
                    panelTop: panelRect.top,
                    panelBottom: panelRect.bottom,
                    panelLeft: panelRect.left,
                    panelRight: panelRect.right,
                    headerBottom: headerRect.bottom,
                    headerPosition: getComputedStyle(mobileHeader).position,
                    viewportWidth: document.documentElement.clientWidth,
                    viewportHeight: window.innerHeight,
                };
            }""")

            expected_status_path = JOB_STATUS_PREFIX + job_uid
            terminal_status_values = {"completed", "completed_with_errors", "failed", "cancelled"}
            deadline = time.monotonic() + 15
            terminal_status_read = None
            while time.monotonic() < deadline:
                matching_reads = [
                    read for read in job_status_reads
                    if read["path"] == expected_status_path and read["status"] == 200
                    and isinstance(read["payload"], dict)
                    and read["payload"].get("status") in terminal_status_values
                ]
                if matching_reads:
                    terminal_status_read = matching_reads[-1]
                    break
                page.wait_for_timeout(50)
            assert terminal_status_read is not None, (
                "browser did not observe a terminal raw BackgroundJob status GET",
                job_status_reads[status_reads_before_create:],
            )
            assert terminal_status_read["method"] == "GET"
            assert terminal_status_read["sequence"] > create_post["sequence"]
            assert terminal_status_read["payload"]["status"] == "completed"
            page.evaluate("""() => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
            terminal_screenshot = "supplier-filtered-import-wb-create-terminal-dark-320.png"
            page.locator(".shk-job").screenshot(
                path=REPORT_PATH.parent / terminal_screenshot,
                animations="disabled",
            )

            terminal_payload = terminal_status_read["payload"]
            terminal_progress = terminal_payload.get("progress")
            terminal_result = terminal_payload.get("result")
            assert terminal_payload.get("job_type") == "bulk_wb_import"
            assert terminal_payload.get("total") == 2
            assert terminal_payload.get("processed") == 2
            assert terminal_payload.get("succeeded") == 1
            assert terminal_payload.get("failed_count") == 1
            assert isinstance(terminal_progress, dict)
            assert terminal_progress.get("product_ids") == wb_selected_ids
            progress_items = terminal_progress.get("items")
            assert isinstance(progress_items, list) and len(progress_items) == 2
            assert [item.get("product_id") for item in progress_items] == wb_selected_ids
            assert [item.get("status") for item in progress_items] == ["success", "failed"]
            assert progress_items[1].get("error") == "Synthetic WB rejected this fixture card"
            assert isinstance(terminal_result, dict)
            assert terminal_result.get("total") == 2
            assert terminal_result.get("imported") == 1
            assert terminal_result.get("failed") == 1
            assert terminal_result.get("errors") == [
                "{}: Synthetic WB rejected this fixture card".format(
                    progress_items[1].get("title", ""),
                ),
            ]
            assert len(fake_wb_calls) == 1
            assert fake_wb_calls[0]["product_ids"] == wb_selected_ids
            assert all(item["is_ready"] and item["can_push"] for item in fake_wb_calls[0]["readiness"])
            with fixture.app.app_context():
                from models import BackgroundJob

                job = BackgroundJob.query.filter_by(
                    job_uid=job_uid,
                    seller_id=fixture.FIXTURE["seller_id"],
                ).one()
                assert job.status == "completed"
                assert job.total == 2 and job.processed == 2
                assert job.succeeded == 1 and job.failed_count == 1
                assert job.get_progress()["product_ids"] == wb_selected_ids
                assert [row["product_id"] for row in job.get_progress()["items"]] == wb_selected_ids
                assert job.get_result()["imported"] == 1 and job.get_result()["failed"] == 1

            terminal_title = page.locator(".shk-job-title")
            terminal_title.wait_for(state="visible")
            assert terminal_title.inner_text() == "Завершено с ошибками"
            assert job_position_before_screenshot is not None
            assert job_position_before_screenshot["focused"], job_position_before_screenshot
            assert job_position_before_screenshot["outlineStyle"] != "none"
            assert job_position_before_screenshot["outlineWidth"] >= 2, job_position_before_screenshot
            assert job_position_before_screenshot["headerPosition"] == "fixed"
            assert job_position_before_screenshot["panelTop"] >= job_position_before_screenshot["headerBottom"]
            assert job_position_before_screenshot["panelBottom"] <= job_position_before_screenshot["viewportHeight"]
            assert job_position_before_screenshot["panelLeft"] >= 0
            assert job_position_before_screenshot["panelRight"] <= job_position_before_screenshot["viewportWidth"]
            assert job_position_before_screenshot["scrollY"] < confirmation_scroll_y, (
                "accepted create should bring the result panel into view from the deep batch position",
                job_position_before_screenshot,
            )
            checks.append("accepted_create_focuses_and_reveals_progress_below_fixed_mobile_header")
            assert job_panel.get_by_text("2 из 2", exact=True).count() == 1
            assert job_panel.locator(".shk-job-stat.is-ok").inner_text() == "Успешно: 1"
            assert job_panel.locator(".shk-job-stat.is-danger").inner_text() == "С ошибкой: 1"
            close_button = page.get_by_role("button", name="Скрыть итог задачи")
            assert close_button.count() == 1 and close_button.is_visible()
            close_geometry = close_button.evaluate("""element => {
                const rect = element.getBoundingClientRect();
                return {width: rect.width, height: rect.height};
            }""")
            assert close_geometry["width"] >= 44 and close_geometry["height"] >= 44, close_geometry
            viewport_geometry = page.evaluate("""() => ({
                width: document.documentElement.clientWidth,
                scrollWidth: document.documentElement.scrollWidth,
            })""")
            assert viewport_geometry["width"] == 320
            assert viewport_geometry["scrollWidth"] <= viewport_geometry["width"] + 1
            page.keyboard.press("Tab")
            close_button.focus()
            close_focus = close_button.evaluate("""element => {
                const style = getComputedStyle(element);
                return {
                    focused: document.activeElement === element,
                    focusVisible: element.matches(':focus-visible'),
                    outlineStyle: style.outlineStyle,
                    outlineWidth: parseFloat(style.outlineWidth) || 0,
                };
            }""")
            assert close_focus["focused"] and close_focus["focusVisible"], close_focus
            assert close_focus["outlineStyle"] != "none" and close_focus["outlineWidth"] >= 2, close_focus
            checks.append("raw_terminal_job_read_renders_one_success_and_one_distinct_failure")
            checks.append("320px_terminal_close_target_is_44px_keyboard_visible_and_overflow_free")

            refresh_deadline = time.monotonic() + 5
            while time.monotonic() < refresh_deadline and not any(
                read["sequence"] > terminal_status_read["sequence"]
                and read["batch_tokens"] == (batch_token,)
                for read in product_feed_reads
            ):
                page.wait_for_timeout(50)
            refreshed_reads = [
                read for read in product_feed_reads
                if read["sequence"] > terminal_status_read["sequence"]
                and read["batch_tokens"] == (batch_token,)
            ]
            assert refreshed_reads, "terminal result must refresh the signed import-batch product list"
            assert refreshed_reads[-1]["status"] == 200
            assert refreshed_reads[-1]["payload"].get("import_batch_count") == 50
            assert {
                item.get("id") for item in refreshed_reads[-1]["payload"].get("items", [])
            } == persisted_product_ids
            assert len(product_feed_reads) >= feed_reads_before_create + 1
            assert len(facet_reads) == facet_reads_before_create, (
                "terminal import-batch refresh must stay exact and avoid global facets",
            )
            assert page.locator(".mp-list > .mp-row").count() == 50
            checks.append("terminal_job_refreshes_the_same_exact_signed_batch_product_list")

            observed_status_get_count = sum(
                1 for read in job_status_reads if read["path"] == expected_status_path
            )
            page.wait_for_timeout(2700)
            assert sum(
                1 for read in job_status_reads if read["path"] == expected_status_path
            ) == observed_status_get_count, "terminal polling must stop after the raw completed status"
            assert len(wb_create_posts) == 1, "terminal UI must not resubmit WB creation"
            close_button.click()
            job_panel.wait_for(state="hidden")
            assert page.locator(".shk-job").count() == 0
            assert len(wb_create_posts) == 1
            checks.append("terminal_job_stops_polling_after_2_5s_and_closes_without_a_retry_or_resubmit")

            wb_create_report = {
                "selected_count": len(wb_selected_ids),
                "terminal_raw_status": terminal_payload["status"],
                "succeeded_count": terminal_payload["succeeded"],
                "failed_count": terminal_payload["failed_count"],
                "screenshot": terminal_screenshot,
                "physical_post_count": len(wb_create_posts),
                "status_get_count": observed_status_get_count,
            }
        finally:
            WBProductImporter.batch_import_products_to_wb = original_batch_import

        oversized_path = "/supplier-catalog/{}/products?{}".format(
            fixture_ids["supplier_id"],
            urlencode({
                "search": "R28 Oversize batch",
                "stock_status": "all",
                "select_all": "1",
            }),
        )
        oversized_response = page.goto(
            fixture.BASE + oversized_path,
            wait_until="domcontentloaded",
        )
        assert oversized_response and oversized_response.status == 200
        page.wait_for_load_state("networkidle")
        assert page.locator(".grid > div.p-4").count() == 50
        oversized_checks = page.locator(
            'input[form="supplier-import-form"][name="product_ids"]',
        )
        assert oversized_checks.count() == 50
        assert oversized_checks.evaluate_all(
            "items => items.every(item => !item.checked)",
        )
        assert page.get_by_text(
            "В текущем фильтре 201 товаров.", exact=False,
        ).count() == 1
        assert page.get_by_text("Ни один товар не был отмечен", exact=False).count() == 1
        with fixture.app.app_context():
            assert ImportedProduct.query.count() == fixture_ids["imported_before"] + imported_count
        checks.append("over_200_filter_falls_back_to_50_unselected_rows_without_partial_mass_selection")

        assert not safe_page_errors(), "batch browser reported a JavaScript error"
        assert fixture.REPORT["provider_attempts"] == 0
        assert fixture.REPORT["unexpected_external_requests"] == []
        assert fixture.REPORT["unexpected_http"] == []
        assert fixture.REPORT["writes"] == [
            {"method": "POST", "path": IMPORT_PATH},
            {"method": "POST", "path": WB_CREATE_PATH},
        ], fixture.REPORT["writes"]
        assert wb_create_report is not None
        checks.append("synthetic_import_and_wb_create_have_zero_provider_attempts_external_requests_and_http_errors")
        context.close()
        browser.close()

    return {
        "status": "passed",
        "scope": "synthetic_supplier_filtered_import_and_terminal_wb_bulk_create",
        "checks": checks,
        "provider_attempts": fixture.REPORT["provider_attempts"],
        "selected_count": len(selected_ids),
        "imported_count": imported_count,
        "wb_create": wb_create_report,
        "screenshots": [
            "supplier-filtered-import-batch-dark-320.png",
            wb_create_report["screenshot"] if wb_create_report else None,
        ],
        "writes": fixture.REPORT["writes"],
        "external_requests": fixture.REPORT["unexpected_external_requests"],
        "unexpected_http": fixture.REPORT["unexpected_http"],
    }


def main():
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = {"status": "failed", "checks": [], "provider_attempts": 0}
    try:
        result = run()
    finally:
        if result.get("status") != "passed":
            result["provider_attempts"] = fixture.REPORT.get("provider_attempts", 0)
        REPORT_PATH.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        fixture.server.shutdown()
        fixture.TEMP.cleanup()


if __name__ == "__main__":
    main()
