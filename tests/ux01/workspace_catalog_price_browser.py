"""Exercise the catalog pricing dialog against isolated real Flask routes."""

from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
import json
import os
import re

from playwright.sync_api import expect, sync_playwright

from tests.ux01 import journey_browser as fixture
from tests.ux01.bulk_workspace_browser import MAIN_TITLE, MANUAL_TITLE, seed_bulk_selection
from tests.ux01.bulk_action_controls import invoke_workspace_action
from models import MarketplaceCommercialProposal, MarketplaceListing, PriceChangeBatch, PricingSettings, Product, Seller, db
from services.marketplace_adapters.ozon import OzonAdapter
from services.marketplace_commercial import MarketplaceCommercialService
from services.pricing_engine import calculate_price


output = Path(os.environ.get("PRICE_QA_ARTIFACTS", "/artifacts/workspace-price"))
output.mkdir(parents=True, exist_ok=True)
def write_report(report):
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


ids = seed_bulk_selection()
listing_id = fixture.FIXTURE["listing_ids"][0]
account_id = fixture.FIXTURE["account_id"]
fixture.app.config["MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED"] = True
with fixture.app.app_context():
    seller = db.session.get(Seller, fixture.FIXTURE["seller_id"])
    seller.wb_seller_id = "562350"
    wb = db.session.get(Product, ids["wb_id"])
    wb.price = wb.wb_price = 4250
    wb.supplier_price = 1760
    wb.wb_price_synced_at = wb.supplier_price_updated_at = datetime.utcnow()
    settings = PricingSettings(is_enabled=True, seller_id=seller.id)
    db.session.add(settings)
    db.session.flush()
    expected_price = round(calculate_price(1760, settings, product_id=wb.id)["final_price"])
    ozon = db.session.get(MarketplaceListing, listing_id)
    ozon.imported_product_id = ids["local_id"]
    ozon.link_status = "linked"
    ozon.link_source = "synthetic_browser_fixture"
    ozon.linked_at = datetime.utcnow()
    db.session.commit()


checks = {"status": "running", "exact_requests": [], "ozon_price_reads": [], "provider_writes": 0, "js_errors": []}


def read_ozon_price(cls, *, listings, adapter, credentials):
    del cls, credentials
    assert adapter.code == "ozon"
    assert [item.id for item in listings] == [listing_id]
    assert listings[0].account_id == account_id
    checks["ozon_price_reads"].append([listing_id])
    return {
        item.id: {
            "kind": "price", "offer_id": item.offer_id, "product_id": item.external_product_id,
            "price": "1000", "old_price": "1500", "min_price": "500", "currency_code": "RUB",
            "auto_action_enabled": False, "auto_add_to_ozon_actions_list_enabled": False,
        } for item in listings
    }


def forbid_price_write(*args, **kwargs):
    checks["provider_writes"] += 1
    raise AssertionError("Provider writes are forbidden in workspace price QA")


MarketplaceCommercialService._read_price_states = classmethod(read_ozon_price)
OzonAdapter.update_prices = forbid_price_write
paths = {
    "wb_preview": "/prices/api/preview",
    "wb_save": "/prices/create-batch",
    "ozon_preview": "/marketplaces/commercial/price-proposals/bulk-preview",
    "ozon_save": "/marketplaces/commercial/price-proposals/bulk-confirm",
}
path_types = {path: key for key, path in paths.items()}
lost_wb_save = False
replayed_wb = False
pending_body_json = None
local_port = urlsplit(fixture.BASE).port


def bridge(route):
    global lost_wb_save, replayed_wb, pending_body_json
    request = route.request
    url = urlsplit(request.url)
    if url.hostname != "127.0.0.1" or url.port != local_port or url.path not in path_types:
        fixture.bridge(route)
        return
    assert request.method == "POST" and request.headers.get("x-csrftoken")
    kind = path_types[url.path]
    body = request.post_data_json
    response = route.fetch(max_redirects=0)
    data = response.json()
    assert response.ok and data["success"] is True, (kind, response.status, data.get("code"), data.get("error"))
    safe = {"kind": kind, "status": response.status}
    if kind.startswith("wb_"):
        assert body["reviewed_write"] and body["exact_selection"] and body["product_ids"] == [ids["wb_id"]]
        assert body["change_type"] == "supplier_pricing"
        if kind == "wb_preview":
            assert [item["product_id"] for item in data["items"]] == body["product_ids"]
            assert data["items"][0]["new_price"] == expected_price
        else:
            assert body["review_token"]
            if not lost_wb_save:
                lost_wb_save = True
                pending_body_json = request.post_data
                assert data["replayed"] is False
                checks["exact_requests"].append({**safe, "response_lost": True})
                route.abort("connectionreset")
                return
            assert request.post_data == pending_body_json and data["replayed"] is True
            replayed_wb = True
    elif kind == "ozon_preview":
        assert body["listing_ids"] == [listing_id] and body["account_id"] == account_id and body["mode"] == "set"
        assert [row["listing_id"] for row in data["items"]] == [listing_id]
        assert data["items"][0]["current_price"] == "1000"
    else:
        assert set(body) == {"preview_token", "confirm_save"} and body["confirm_save"] is True
        assert data["items"][0]["listing_id"] == listing_id
    checks["exact_requests"].append(safe)
    route.fulfill(response=response)


def open_catalog(page):
    response = page.goto(fixture.BASE + "/my-products/beta", wait_until="domcontentloaded")
    assert response and response.status == 200
    row = page.locator(".mp-row").filter(has_text=MAIN_TITLE)
    expect(row).to_have_count(1)
    return row


def geometry(dialog):
    size = dialog.evaluate("""node => ({
      viewport: document.documentElement.clientWidth, root: document.documentElement.scrollWidth,
      rect: node.getBoundingClientRect().toJSON(),
    })""")
    assert size["root"] == size["viewport"]
    assert size["rect"]["left"] >= -1 and size["rect"]["right"] <= size["viewport"] + 1
    for control in dialog.locator("[data-price-local-save], [data-price-preview-action]").all():
        if control.is_visible():
            rect = control.bounding_box()
            assert rect["width"] >= 44 and rect["height"] >= 44


try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=fixture.CHROMIUM, args=["--no-sandbox", "--disable-dev-shm-usage"])
        for width, theme in ((1440, "light"), (320, "dark")):
            context = browser.new_context(viewport={"width": width, "height": 880}, color_scheme=theme,
                                          is_mobile=width == 320, has_touch=width == 320)
            context.add_cookies([fixture.synthetic_login_cookie()])
            context.route("**/*", bridge)
            page = context.new_page()
            page.on("pageerror", lambda error: checks["js_errors"].append(str(error)))
            row = open_catalog(page)
            search_scope = page.url
            selection = row.get_by_role("checkbox")
            selection.check()
            key = "wb" if width == 1440 else "ozon"
            dialog = page.locator("[data-workspace-price-dialog]")
            baseline_count = len(checks["exact_requests"])
            invoke_workspace_action(page, key + "_prices")
            expect(dialog).to_be_visible()
            expect(dialog).to_have_attribute("data-price-stage", "edit")
            assert page.url == search_scope and len(checks["exact_requests"]) == baseline_count
            if key == "ozon":
                expect(dialog.locator("[data-price-mode]")).to_have_value("set")
                expect(dialog.locator("[data-price-amount]")).to_have_value("")
                dialog.locator("[data-price-amount]").fill("1050")
            else:
                expect(dialog.locator("[data-price-mode]")).to_have_value("supplier_pricing")
            geometry(dialog)
            dialog.locator("[data-price-preview-action]").click()
            provider_id = ids["wb_id"] if key == "wb" else listing_id
            expect(dialog.locator(f'[data-price-review-row="{provider_id}"]')).to_be_visible()
            expect(dialog.locator("[data-price-review-row]")).to_have_count(1)
            expect(dialog.locator("[data-price-local-save]")).to_be_enabled()
            assert len(checks["exact_requests"]) == baseline_count + 1 and page.url == search_scope
            geometry(dialog)
            page.screenshot(path=output / f"preview-{key}-{width}-{theme}.png")
            dialog.locator("[data-price-local-save]").click()
            if key == "wb":
                expect(dialog.locator("[data-price-unknown]")).to_be_visible()
                assert len(checks["exact_requests"]) == baseline_count + 2
                row = open_catalog(page)
                selection = row.get_by_role("checkbox")
                selection.check()
                invoke_workspace_action(page, key + "_prices")
                expect(dialog).to_have_attribute("data-price-stage", "unknown")
                assert len(checks["exact_requests"]) == baseline_count + 2
                dialog.locator("[data-price-retry-exact]").click()
            link = dialog.locator("[data-price-review-link]")
            expect(link).to_be_visible()
            href = link.get_attribute("href")
            assert href and href.startswith("/") and not href.startswith("//")
            assert page.url == search_scope
            page.screenshot(path=output / f"receipt-{key}-{width}-{theme}.png")
            page.keyboard.press("Escape")
            expect(dialog).not_to_be_visible()
            expect(selection).to_be_checked()
            unrelated = page.locator(".mp-row").filter(has_text=MANUAL_TITLE).get_by_role("checkbox")
            unrelated.check()
            direct_price = row.locator(f'[data-row-price-action="{key}"]')
            direct_price.click()
            expect(dialog).to_have_attribute("data-price-stage", "edit")
            expect(dialog).to_have_attribute("data-price-marketplace", key)
            assert len(checks["exact_requests"]) == baseline_count + (3 if key == "wb" else 2)
            page.keyboard.press("Escape")
            expect(direct_price).to_be_focused()
            expect(selection).to_be_checked()
            expect(unrelated).to_be_checked()
            context.close()
        browser.close()

    assert replayed_wb and len(checks["ozon_price_reads"]) == 1
    assert not checks["provider_writes"] and not checks["js_errors"]
    with fixture.app.app_context():
        assert PriceChangeBatch.query.count() == 2
        assert db.session.get(Product, ids["wb_id"]).price == 4250
        assert MarketplaceCommercialProposal.query.filter_by(proposal_kind="price", status="pending_review").count() == 1
    assert fixture.REPORT["provider_attempts"] == 0
    assert not fixture.REPORT["unexpected_external_requests"], fixture.REPORT["unexpected_external_requests"]
    assert not fixture.REPORT["unexpected_http"], fixture.REPORT["unexpected_http"]
    checks.update(status="passed", wb_formula_price=expected_price, wb_replayed_without_duplicate=True)
except Exception:
    checks["status"] = "failed"
    raise
finally:
    write_report(checks)
