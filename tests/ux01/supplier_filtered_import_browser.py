"""Synthetic browser proof for bounded all-filtered supplier import selection.

Uses the isolated app, SQLite fixture, network-denying browser bridge, and
synthetic seller session from ``journey_browser``. The only browser POST is the
normal local supplier import form; no supplier/provider endpoint is called.

Run with the repository venv and Chromium installed. The summary is written to
``/artifacts/supplier-filtered-import.json`` by default.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import sync_playwright


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.ux01 import journey_browser as fixture
from models import ImportedProduct, SellerSupplier, Supplier, SupplierProduct, db


REPORT_PATH = Path(os.environ.get(
    "SUPPLIER_FILTERED_IMPORT_REPORT",
    "/artifacts/supplier-filtered-import.json",
))
IMPORT_PATH = "/supplier-catalog/import"
FILTERS = {
    "search": "R28 Mass batch",
    "stock_status": "in_stock",
    "ai_filter": "ai_parsed",
    "wb_filter": "not_on_wb",
    "show_imported": "1",
}


def seed_supplier_products():
    """Create 53 current-supplier matches plus filter/supplier exclusions."""
    with fixture.app.app_context():
        seller_id = fixture.FIXTURE["seller_id"]
        supplier_id = fixture.FIXTURE["supplier_id"]
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
                supplier_price=100 + index,
                currency="RUB",
                supplier_quantity=4,
                supplier_status="in_stock",
                status="ready",
                ai_parsed_at=datetime(2026, 11, 21),
                photo_urls_json="[]",
                characteristics_json="[]",
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
    product_feed_reads = []
    facet_reads = []
    page_errors = []

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
            assert parsed.path == IMPORT_PATH, (request.method, parsed.path)
            body = parse_qs(request.post_data or "", keep_blank_values=True)
            assert body.get("csrf_token"), "normal supplier import form must carry CSRF"
            posts.append({"path": parsed.path, "body": body})
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
        ], fixture.REPORT["writes"]
        checks.append("synthetic_import_has_zero_provider_attempts_external_requests_and_http_errors")
        context.close()
        browser.close()

    return {
        "status": "passed",
        "scope": "synthetic_normal_supplier_filtered_bulk_import",
        "checks": checks,
        "provider_attempts": fixture.REPORT["provider_attempts"],
        "selected_count": len(selected_ids),
        "imported_count": imported_count,
        "screenshots": ["supplier-filtered-import-batch-dark-320.png"],
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
