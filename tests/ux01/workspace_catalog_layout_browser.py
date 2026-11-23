"""Check the real catalog layout and keyboard behavior in an offline fixture."""

from datetime import datetime
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from tests.ux01 import journey_browser as fixture
from tests.ux01.bulk_workspace_browser import (
    MAIN_TITLE,
    assert_catalog_extra_filter_scope,
    ensure_catalog_extra_filters_open,
    selected_browser_state,
    seed_bulk_selection,
    set_catalog_extra_filters_open_by_keyboard,
)
from models import ImportedProduct, MarketplaceListing, SellerMarketplaceAccount, SupplierProduct, db


target = Path(os.getenv("CATALOG_QA_ARTIFACTS", "/artifacts/catalog-layout"))
target.mkdir(parents=True, exist_ok=True)
AMBIGUOUS_WB_TITLE = "Связь WB только по nmID"
SUPPLIER_REFRESH_PREVIEW = "/api/my-products/supplier-refresh-preview"
SUPPLIER_REFRESH_APPLY = "/my-products/refresh-from-supplier"
scope = seed_bulk_selection()
with fixture.app.app_context():
    main_row = db.session.get(ImportedProduct, scope["local_id"])
    main_row.supplier_price = 1760
    main_row.supplier_quantity = 32
    nm_only_row = ImportedProduct(
        seller_id=main_row.seller_id,
        external_id="SYNTH-NMID-ONLY-LAYOUT",
        source_type="manual",
        title=AMBIGUOUS_WB_TITLE,
        import_status="imported",
        wb_nm_id=990001234,
    )
    db.session.add(nm_only_row)
    first_account = db.session.get(SellerMarketplaceAccount, fixture.FIXTURE["account_id"])
    first_account.label = "Северный склад"
    second_account = SellerMarketplaceAccount(
        seller_id=main_row.seller_id, marketplace_id=first_account.marketplace_id,
        external_account_id="155541", label="Южный склад · товары для дома и дороги",
        is_active=True, connection_status="connected",
    )
    second_account.set_credentials({"client_id": "155541", "api_key": "synthetic-layout-only-api-key"})
    db.session.add(second_account)
    db.session.flush()
    for index, account in enumerate((first_account, second_account)):
        listing = db.session.get(MarketplaceListing, fixture.FIXTURE["listing_ids"][index])
        listing.account_id = account.id
        listing.imported_product_id = main_row.id
        listing.link_status = "linked"
        listing.link_source = "synthetic_browser_fixture"
        listing.linked_at = datetime.utcnow()
    db.session.commit()


def supplier_business_snapshot():
    with fixture.app.app_context():
        target_row = db.session.get(ImportedProduct, scope["local_id"])
        supplier_row = db.session.get(SupplierProduct, scope["refresh_source_id"])
        return {
            "target": (
                target_row.title,
                target_row.supplier_price,
                target_row.supplier_quantity,
                target_row.supplier_content_revision,
            ),
            "supplier": (
                supplier_row.title,
                supplier_row.supplier_price,
                supplier_row.supplier_quantity,
                supplier_row.content_revision,
            ),
        }


supplier_before_preview = supplier_business_snapshot()

report = {"status": "running", "layouts": [], "javascript_errors": []}
supplier_preview_requests = []
supplier_preview_receipts = []
supplier_apply_requests = []


def browser_bridge(route):
    request = route.request
    path = urlsplit(request.url).path
    if path == SUPPLIER_REFRESH_PREVIEW:
        assert request.method == "POST", request.method
        payload = request.post_data_json
        assert payload == {"product_ids": [scope["local_id"]]}, payload
        supplier_preview_requests.append(payload)
        response = route.fetch(max_redirects=0)
        assert response.status == 200, response.status
        data = response.json()
        supplier_preview_receipts.append({
            "success": data.get("success"),
            "requested": data.get("requested"),
            "eligible": data.get("eligible"),
            "changed": data.get("changed"),
            "skipped": data.get("skipped"),
            "product_ids": [item.get("product_id") for item in data.get("items", [])],
        })
        route.fulfill(response=response)
        return
    if path == SUPPLIER_REFRESH_APPLY and request.method == "POST":
        supplier_apply_requests.append(path)
        raise AssertionError("The catalog entry must stop at the read-only supplier preview")
    fixture.bridge(route)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=fixture.CHROMIUM, args=["--no-sandbox", "--disable-dev-shm-usage"])
    for width in (320, 390, 1024, 1440):
        for theme in ("light", "dark"):
            context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme,
                                          is_mobile=width <= 390, has_touch=width <= 390)
            context.add_cookies([fixture.synthetic_login_cookie()])
            context.add_init_script(f"localStorage.setItem('sh-theme', '{theme}');")
            context.route("**/*", browser_bridge)
            page = context.new_page()
            page.on("pageerror", lambda err: report["javascript_errors"].append(str(err)))
            fixture.wait_document(page, "catalog_grid", "/my-products/beta")
            expect(page.locator("[data-workspace-catalog]")).to_be_visible()
            row = page.locator(".mp-row").filter(has_text=MAIN_TITLE)
            expect(row).to_have_count(1)
            expect(row.get_by_role("heading")).to_have_text(MAIN_TITLE)
            expect(row.locator("[data-common-content-row-link]")).to_have_text(MAIN_TITLE)
            expect(row.locator('[data-workspace-column="category"] .mp-cell-label')).to_have_text("Категория каталога")
            expect(row.locator('[data-workspace-column="wb"]')).to_contain_text("На площадке")
            expect(row).to_contain_text("Северный склад")
            expect(row).to_contain_text("Южный склад")
            expect(row).to_contain_text("32")
            cols = row.evaluate("""element => {
              const root = document.documentElement;
              const box = element.getBoundingClientRect().toJSON();
              const cells = [...element.querySelectorAll('.mp-catalog-cell, .mp-chan')].map(node =>
                ({label:node.textContent.trim(), visible:node.getClientRects().length > 0, rect:node.getBoundingClientRect().toJSON()})
              );
              return {device: root.clientWidth, left: box.left, right: box.right, full: root.scrollWidth, cells};
            }""")
            assert cols["device"] == cols["full"], cols
            assert cols["left"] >= -1 and cols["right"] <= cols["device"] + 1
            filter_buttons = page.locator("[data-catalog-marketplace]")
            expect(filter_buttons).to_have_count(3)
            toolbar = page.locator(".mp-catalog-surface .shk-filters")
            expect(toolbar).to_be_visible()
            toolbar_box = toolbar.bounding_box()
            assert toolbar_box and toolbar_box["x"] >= -1
            assert toolbar_box["x"] + toolbar_box["width"] <= width + 1, toolbar_box
            filter_geometry = []
            for control in (
                *filter_buttons.all(),
                page.locator("[data-catalog-marketplace-status]"),
                page.locator("[data-catalog-extra-filters] summary"),
            ):
                expect(control).to_be_visible()
                box = control.bounding_box()
                assert box and box["x"] >= 0 and box["x"] + box["width"] <= width + 1, box
                if width <= 390:
                    assert box["width"] >= 44 and box["height"] >= 44, box
                filter_geometry.append({"width": box["width"], "height": box["height"]})
            expect(filter_buttons.first).to_have_attribute("aria-pressed", "true")
            extra_filters = page.locator("[data-catalog-extra-filters]")
            assert not extra_filters.evaluate("details => details.open")
            assert_catalog_extra_filter_scope(page, opened=False, ozon=False)
            for city in ("Северный склад", "Южный склад"):
                label = row.get_by_text(city, exact=False).last
                expect(label).to_be_visible()
                box = label.bounding_box()
                assert box and box["x"] >= 0 and box["x"] + box["width"] <= width + 1, box
            if width <= 390:
                check_label = row.locator(".mp-check")
                box = check_label.bounding_box()
                assert box and box["width"] >= 44 and box["height"] >= 44
                wb_cell = row.locator('[data-workspace-column="wb"]').bounding_box()
                ozon_cell = row.locator('[data-workspace-column="ozon"]').bounding_box()
                assert wb_cell and ozon_cell and abs(wb_cell["y"] - ozon_cell["y"]) <= 1
                assert wb_cell["x"] + wb_cell["width"] <= ozon_cell["x"] + 1
            if width == 320:
                import_entry = page.locator(".mp-head-import-action")
                updates_entry = page.locator("[data-supplier-updates-entry]")
                expect(import_entry).to_be_visible()
                expect(updates_entry).to_be_visible()
                expect(updates_entry).to_have_attribute("aria-pressed", "false")
                expect(updates_entry.locator(".mp-supplier-updates-count")).to_be_visible()
                expect(filter_buttons.get_by_text("Обновления поставщика", exact=True)).to_have_count(0)
                action_boxes = [control.bounding_box() for control in (import_entry, updates_entry)]
                assert all(box and box["width"] >= 44 and box["height"] >= 44 for box in action_boxes), action_boxes
                assert abs(action_boxes[0]["y"] - action_boxes[1]["y"]) <= 1, action_boxes
                assert action_boxes[0]["x"] + action_boxes[0]["width"] <= action_boxes[1]["x"] + 1, action_boxes
            snapshot = f"catalog-grid-{width}-{theme}.png"
            page.screenshot(path=target / snapshot, animations="disabled")

            selection_before_disclosure = selected_browser_state(page)["selected"]
            assert selection_before_disclosure == []
            set_catalog_extra_filters_open_by_keyboard(page, opened=True)
            assert_catalog_extra_filter_scope(page, opened=True, ozon=False)
            expanded_controls = (
                page.get_by_label("Фото"),
                page.get_by_label("Остаток у поставщика"),
                page.locator("[data-catalog-sort]"),
                page.locator("[data-catalog-wb-status]"),
            )
            for control in expanded_controls:
                expect(control).to_be_visible()
                box = control.bounding_box()
                assert box and box["x"] >= 0
                assert box["x"] + box["width"] <= width + 1, box
                if width <= 390:
                    assert box["width"] >= 44 and box["height"] >= 44, box
            if width <= 390:
                summary_box = extra_filters.locator("summary").bounding_box()
                body_box = extra_filters.locator(".mp-extra-filter-body").bounding_box()
                assert summary_box and body_box
                assert body_box["x"] >= 0 and body_box["x"] + body_box["width"] <= width + 1, body_box
                assert body_box["y"] >= summary_box["y"] + summary_box["height"] - 1, {
                    "summary": summary_box, "body": body_box,
                }
            expanded_dimensions = page.evaluate("""() => ({
                scroll: document.documentElement.scrollWidth,
                viewport: document.documentElement.clientWidth,
            })""")
            assert expanded_dimensions["scroll"] == expanded_dimensions["viewport"], expanded_dimensions
            assert selected_browser_state(page)["selected"] == selection_before_disclosure
            set_catalog_extra_filters_open_by_keyboard(page, opened=False)
            assert_catalog_extra_filter_scope(page, opened=False, ozon=False)
            assert selected_browser_state(page)["selected"] == selection_before_disclosure

            if width == 320 and theme == "light":
                bootstrap = json.loads(page.locator("#mp-bootstrap").text_content())
                feed_path = urlsplit(bootstrap["urls"]["feed"]).path
                feed_requests = []

                def feed_response(response, updates):
                    parsed = urlsplit(response.url)
                    query = parse_qs(parsed.query)
                    return (
                        response.request.method == "GET" and parsed.path == feed_path and
                        (query.get("updates") == ["1"] if updates else "updates" not in query)
                    )

                def record_feed_request(request):
                    parsed = urlsplit(request.url)
                    if request.method == "GET" and parsed.path == feed_path:
                        feed_requests.append(parsed.query)

                page.on("request", record_feed_request)
                page.evaluate("window.__supplierEntryDocumentMarker = 'same-document'")

                before_url = page.url
                ensure_catalog_extra_filters_open(page)
                stock_filter = page.get_by_label("Остаток у поставщика")
                baseline_feed_reads = len(feed_requests)
                with page.expect_response(lambda response: feed_response(response, False)) as stock_response:
                    stock_filter.select_option("in_stock")
                assert stock_response.value.status == 200
                assert len(feed_requests) == baseline_feed_reads + 1, feed_requests
                stock_query = parse_qs(urlsplit(stock_response.value.url).query)
                assert {key: values[-1] for key, values in stock_query.items()
                        if key in {"search", "supplier", "has_photos", "stock", "sort",
                                   "marketplace", "marketplace_state", "status", "updates"}} == {
                    "stock": "in_stock",
                }, stock_query
                assert stock_query.get("page") == ["1"] and stock_query.get("per_page") == ["50"], stock_query

                updates_entry = page.locator("[data-supplier-updates-entry]")
                baseline_feed_reads = len(feed_requests)
                with page.expect_response(lambda response: feed_response(response, True)) as updates_response:
                    updates_entry.click()
                assert updates_response.value.status == 200
                assert len(feed_requests) == baseline_feed_reads + 1, feed_requests
                update_query = parse_qs(urlsplit(updates_response.value.url).query)
                assert update_query.get("stock") == ["in_stock"], update_query
                assert {key: values[-1] for key, values in update_query.items()
                        if key in {"search", "supplier", "has_photos", "stock", "sort",
                                   "marketplace", "marketplace_state", "status", "updates"}} == {
                    "stock": "in_stock", "updates": "1",
                }, update_query
                expect(updates_entry).to_have_attribute("aria-pressed", "true")
                assert page.url == before_url
                assert page.evaluate("window.__supplierEntryDocumentMarker") == "same-document"

                baseline_feed_reads = len(feed_requests)
                with page.expect_response(lambda response: feed_response(response, False)) as all_response:
                    updates_entry.click()
                assert all_response.value.status == 200
                assert len(feed_requests) == baseline_feed_reads + 1, feed_requests
                all_query = parse_qs(urlsplit(all_response.value.url).query)
                assert all_query.get("stock") == ["in_stock"], all_query
                assert {key: values[-1] for key, values in all_query.items()
                        if key in {"search", "supplier", "has_photos", "stock", "sort",
                                   "marketplace", "marketplace_state", "status", "updates"}} == {
                    "stock": "in_stock",
                }, all_query
                expect(updates_entry).to_have_attribute("aria-pressed", "false")
                expect(stock_filter).to_have_value("in_stock")
                assert page.url == before_url
                assert page.evaluate("window.__supplierEntryDocumentMarker") == "same-document"

            checkbox = row.get_by_role("checkbox")
            row_top_before_selection = row.bounding_box()["y"]
            checkbox.check()
            if width == 320 and theme == "light":
                updates_entry = page.locator("[data-supplier-updates-entry]")
                expect(updates_entry).to_be_enabled()
                assert updates_entry.get_attribute("aria-pressed") is None
                assert updates_entry.get_attribute("aria-label").startswith("Обновить выбранные товары, 1 ")
                expect(updates_entry.locator(".mp-supplier-updates-count")).to_have_count(0)
                expect(updates_entry.locator(".mp-head-action-label--compact")).to_have_text("Обновить")
                expect(checkbox).to_be_checked()
                assert page.evaluate("document.documentElement.scrollWidth === document.documentElement.clientWidth")
                assert abs(row.bounding_box()["y"] - row_top_before_selection) <= 1

                preview_count = len(supplier_preview_requests)
                with page.expect_response(
                    lambda response: urlsplit(response.url).path == SUPPLIER_REFRESH_PREVIEW and
                    response.request.method == "POST"
                ) as preview_response:
                    updates_entry.click()
                assert preview_response.value.status == 200
                assert len(supplier_preview_requests) == preview_count + 1
                assert supplier_preview_requests[-1] == {"product_ids": [scope["local_id"]]}
                assert supplier_preview_receipts[-1]["success"] is True
                assert supplier_preview_receipts[-1]["requested"] == 1
                assert supplier_preview_receipts[-1]["product_ids"] == [scope["local_id"]]

                dialog = page.get_by_role("dialog", name="Проверка обновления из поставщика")
                expect(dialog).to_be_visible()
                expect(dialog.locator(".mp-supplier-refresh-counts")).to_be_visible()
                expect(dialog.get_by_role("button", name="Вернуться", exact=True)).to_be_visible()
                expect(dialog.get_by_role("button", name="Обновить", exact=True)).to_be_enabled()
                assert selected_browser_state(page)["selected"] == [scope["local_id"]]
                assert not supplier_apply_requests
                assert supplier_business_snapshot() == supplier_before_preview
                dialog.get_by_role("button", name="Вернуться", exact=True).click()
                expect(dialog).not_to_be_visible()
                assert selected_browser_state(page)["selected"] == [scope["local_id"]]
                assert not supplier_apply_requests
            expect(page.locator(".mp-bulkbar")).to_be_visible()
            for group in ("cards", "prices", "create", "refresh"):
                expect(page.locator(f'[data-bulk-action-group="{group}"]')).to_be_visible()
            checkbox.uncheck()
            if width == 320 and theme == "light":
                with page.expect_response(lambda response: feed_response(response, False)) as reset_stock_response:
                    stock_filter.select_option("")
                assert reset_stock_response.value.status == 200
                reset_query = parse_qs(urlsplit(reset_stock_response.value.url).query)
                assert "stock" not in reset_query, reset_query
                expect(stock_filter).to_have_value("")
                unfiltered_ambiguous_row = page.locator(".mp-row").filter(has_text=AMBIGUOUS_WB_TITLE)
                expect(unfiltered_ambiguous_row).to_have_count(1)
                search = page.get_by_placeholder("Название, бренд или артикул")
                search.fill(AMBIGUOUS_WB_TITLE)
                ambiguous_row = page.locator(".mp-row").filter(has_text=AMBIGUOUS_WB_TITLE)
                expect(ambiguous_row).to_have_count(1)
                wb_cell = ambiguous_row.locator('[data-workspace-column="wb"]')
                expect(wb_cell).to_contain_text("Есть связь")
                expect(wb_cell).not_to_contain_text("На площадке")
                expect(ambiguous_row.locator('[data-row-price-action="wb"]')).to_have_count(0)
            report["layouts"].append({"width": width, "theme": theme, "screenshot": snapshot,
                                      "row_width": cols["right"] - cols["left"],
                                      "filter_targets": filter_geometry})
            context.unroute_all(behavior="wait")
            context.close()
    browser.close()
assert not report["javascript_errors"]
assert not fixture.REPORT["unexpected_http"], fixture.REPORT["unexpected_http"]
assert not fixture.REPORT["unexpected_external_requests"], fixture.REPORT["unexpected_external_requests"]
assert fixture.REPORT["provider_attempts"] == 0 and not fixture.REPORT["writes"]
assert supplier_preview_requests == [{"product_ids": [scope["local_id"]]}]
assert len(supplier_preview_receipts) == 1 and not supplier_apply_requests
assert supplier_business_snapshot() == supplier_before_preview
report["status"] = "passed"
(target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
