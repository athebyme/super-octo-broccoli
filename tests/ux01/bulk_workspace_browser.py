"""Exercise the bulk workspace with an isolated database and no provider I/O."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.ux01 import journey_browser as fixture
from models import ImportedProduct, Product, Seller, SupplierProduct, db


OUTPUT = Path(os.environ.get("BULK_WORKSPACE_ARTIFACTS", "/artifacts/bulk-workspace"))
MAIN_TITLE = "Карточка WB с обновлением поставщика"
MANUAL_TITLE = "Внутренний товар без поставщика"
LOCAL_REFRESH = "/my-products/refresh-from-supplier"
LOCAL_OZON_PREPARE = "/marketplaces/ozon/uploads/"
LOCAL_WB_ENRICH_REVIEW = "/products/enrich-bulk"
LOCAL_SUPPLIER_IMPORT = "/supplier-catalog/import"


def seed_bulk_selection():
    with fixture.app.app_context():
        seller_id = fixture.FIXTURE["seller_id"]
        seller = db.session.get(Seller, seller_id)
        seller.wb_api_key = "synthetic-wb-key-for-bulk-workspace-only"
        wb_product = Product(seller_id=seller_id, nm_id=4419901, vendor_code="BULK-WB-001",
                             title=MAIN_TITLE, is_active=True, wb_price=600)
        db.session.add(wb_product)
        db.session.flush()
        supplier_item = db.session.get(SupplierProduct, fixture.FIXTURE["supplier_product_id"])
        supplier_item.content_revision = 2
        original = db.session.get(ImportedProduct, fixture.FIXTURE["source_id"])
        original.title = MAIN_TITLE
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
        db.session.commit()
        return {"wb_id": wb_product.id, "manual_id": manual.id, "import_id": new_source.id,
                "refresh_source_id": supplier_item.id}


def verify_bounds(page):
    dimensions = page.evaluate("""() => ({scroll: document.documentElement.scrollWidth,
                                     viewport: document.documentElement.clientWidth})""")
    assert dimensions["scroll"] <= dimensions["viewport"] + 1, dimensions


def open_products(page):
    fixture.wait_document(page, "bulk_workspace", "/my-products/beta")
    page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").wait_for()


def pick_action(page, name):
    page.get_by_role("button", name="Что сделать").click()
    action = page.get_by_role("menuitem", name=name)
    assert action.is_enabled(), name
    action.click()


def run():
    fixture_ids = seed_bulk_selection()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks = []
    layouts = []
    posts = []
    errors = []

    def bridge(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.netloc == urlsplit(fixture.BASE).netloc and request.method == "POST":
            assert parsed.path in (LOCAL_REFRESH, LOCAL_OZON_PREPARE, LOCAL_WB_ENRICH_REVIEW, LOCAL_SUPPLIER_IMPORT), parsed.path
            if parsed.path in (LOCAL_WB_ENRICH_REVIEW, LOCAL_SUPPLIER_IMPORT):
                values = parse_qs(request.post_data or "")
                assert values.pop("csrf_token", []), parsed.path
            else:
                values = request.post_data_json
            posts.append({"path": parsed.path, "body": values})
            route.fulfill(response=route.fetch(max_redirects=0))
            return
        fixture.bridge(route)

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

        context = browser.new_context(viewport={"width": 1080, "height": 900})
        context.add_cookies([fixture.synthetic_login_cookie()])
        context.route("**/*", bridge)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        open_products(page)
        page.evaluate("localStorage.setItem('price_change_selected', '[999999]')")
        page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").check()
        pick_action(page, "Изменить цены WB")
        page.wait_for_url("**/prices/change?product_ids=*")
        assert parse_qs(urlsplit(page.url).query)["product_ids"] == [str(fixture_ids["wb_id"])]
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
        fixture.wait_document(page, "direct_wb_prices", "/prices/change")
        pricing_form.evaluate("element => Alpine.$data(element).toggleMarketplacePrices()")
        page.get_by_role("button", name="Синхронизировать с WB").wait_for()
        checks.append("account_wide_price_sync_only_on_direct_price_route")

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
        pick_action(page, "Обновить из поставщика")
        page.get_by_role("button", name="Обновить", exact=True).click()
        page.get_by_text("Обновлено локальных карточек: 0.", exact=False).wait_for()
        assert posts == [{"path": LOCAL_REFRESH,
                          "body": {"product_ids": [fixture_ids["manual_id"]]}}], posts
        checks.append("ordinary_local_refresh_reports_zero_and_exact_selection")

        open_products(page)
        page.get_by_role("checkbox", name=f"Выбрать {MANUAL_TITLE}").check()
        pick_action(page, "Подготовить для Ozon")
        dialog = page.locator("dialog.mp-ozon-dialog")
        dialog.wait_for()
        assert dialog.get_by_label("Магазин Ozon").input_value() == str(fixture.FIXTURE["account_id"])
        prepare = dialog.get_by_role("button", name="Подготовить черновики")
        assert prepare.is_disabled()
        dialog.get_by_role("checkbox").check()
        with page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_OZON_PREPARE
                                  and response.request.method == "POST") as completed:
            prepare.click()
        assert completed.value.status == 202
        prepared = posts[1]
        assert prepared["path"] == LOCAL_OZON_PREPARE
        assert prepared["body"]["imported_product_ids"] == [fixture_ids["manual_id"]]
        assert prepared["body"]["account_id"] == fixture.FIXTURE["account_id"]
        assert prepared["body"]["confirm_prepare"] is True and "confirm_write" not in prepared["body"]
        checks.append("ordinary_ozon_bulk_prepare_queues_exact_local_set")

        open_products(page)
        page.get_by_role("checkbox", name=f"Выбрать {MAIN_TITLE}").check()
        page.locator(".mp-more-actions summary").click()
        with page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_WB_ENRICH_REVIEW
                                  and response.request.method == "POST") as review:
            page.get_by_role("button", name="Дополнить карточки на WB").click()
        assert review.value.status == 200
        assert posts[2] == {"path": LOCAL_WB_ENRICH_REVIEW, "body": {"selected_ids": [str(fixture_ids["wb_id"])]}}
        checks.append("ordinary_wb_enrichment_opens_local_review_with_owned_product_ids")

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
        with page.expect_response(lambda response: urlsplit(response.url).path == LOCAL_SUPPLIER_IMPORT
                                  and response.request.method == "POST") as imported:
            page.get_by_role("button", name="Импортировать товары").click()
        assert imported.value.status in (302, 303)
        page.wait_for_url("**/my-products")
        assert posts[3]["body"]["product_ids"] == [str(fixture_ids["import_id"])]
        assert "update_product_ids" not in posts[3]["body"]
        with fixture.app.app_context():
            assert ImportedProduct.query.filter_by(seller_id=fixture.FIXTURE["seller_id"],
                                                   supplier_product_id=fixture_ids["import_id"]).count() == 1
            assert db.session.get(ImportedProduct, fixture.FIXTURE["source_id"]).supplier_content_revision == 1
        checks.append("ordinary_supplier_import_saves_only_its_selected_source")
        assert len(posts) == 4 and not errors, errors
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
