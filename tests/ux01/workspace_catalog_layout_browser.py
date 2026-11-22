"""Check the real catalog layout and keyboard behavior in an offline fixture."""

from datetime import datetime
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.ux01 import journey_browser as fixture
from tests.ux01.bulk_workspace_browser import MAIN_TITLE, seed_bulk_selection
from models import ImportedProduct, MarketplaceListing, SellerMarketplaceAccount, db


target = Path(os.getenv("CATALOG_QA_ARTIFACTS", "/artifacts/catalog-layout"))
target.mkdir(parents=True, exist_ok=True)
AMBIGUOUS_WB_TITLE = "Связь WB только по nmID"
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

report = {"status": "running", "layouts": [], "javascript_errors": []}
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=fixture.CHROMIUM, args=["--no-sandbox", "--disable-dev-shm-usage"])
    for width in (320, 390, 1024, 1440):
        for theme in ("light", "dark"):
            context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme,
                                          is_mobile=width <= 390, has_touch=width <= 390)
            context.add_cookies([fixture.synthetic_login_cookie()])
            context.add_init_script(f"localStorage.setItem('sh-theme', '{theme}');")
            context.route("**/*", fixture.bridge)
            page = context.new_page()
            page.on("pageerror", lambda err: report["javascript_errors"].append(str(err)))
            fixture.wait_document(page, "catalog_grid", "/my-products/beta")
            expect(page.locator("[data-workspace-catalog]")).to_be_visible()
            row = page.locator(".mp-row").filter(has_text=MAIN_TITLE)
            expect(row).to_have_count(1)
            expect(row.get_by_role("heading")).to_have_text(MAIN_TITLE)
            expect(row.locator("[data-common-content-row-link]")).to_have_text(MAIN_TITLE)
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
            snapshot = f"catalog-grid-{width}-{theme}.png"
            page.screenshot(path=target / snapshot, animations="disabled")
            checkbox = row.get_by_role("checkbox")
            checkbox.check()
            expect(page.locator(".mp-bulkbar")).to_be_visible()
            for group in ("cards", "prices", "create", "refresh"):
                expect(page.locator(f'[data-bulk-action-group="{group}"]')).to_be_visible()
            checkbox.uncheck()
            if width == 320 and theme == "light":
                search = page.get_by_placeholder("Название, бренд или артикул")
                search.fill(AMBIGUOUS_WB_TITLE)
                ambiguous_row = page.locator(".mp-row").filter(has_text=AMBIGUOUS_WB_TITLE)
                expect(ambiguous_row).to_have_count(1)
                wb_cell = ambiguous_row.locator('[data-workspace-column="wb"]')
                expect(wb_cell).to_contain_text("Есть связь")
                expect(wb_cell).not_to_contain_text("На площадке")
                expect(ambiguous_row.locator('[data-row-price-action="wb"]')).to_have_count(0)
            report["layouts"].append({"width": width, "theme": theme, "screenshot": snapshot, "row_width": cols["right"] - cols["left"]})
            context.unroute_all(behavior="wait")
            context.close()
    browser.close()
assert not report["javascript_errors"]
assert not fixture.REPORT["unexpected_http"], fixture.REPORT["unexpected_http"]
assert not fixture.REPORT["unexpected_external_requests"], fixture.REPORT["unexpected_external_requests"]
assert fixture.REPORT["provider_attempts"] == 0 and not fixture.REPORT["writes"]
report["status"] = "passed"
(target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
