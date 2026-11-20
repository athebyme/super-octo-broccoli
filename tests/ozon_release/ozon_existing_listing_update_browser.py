"""Open and save one exact Ozon update draft through the ordinary overview."""

from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright
import requests
from werkzeug.serving import make_server


OUT = Path(os.environ.get("OZON_BROWSER_ARTIFACTS", "/artifacts"))
OUT.mkdir(parents=True, exist_ok=True)
ASSETS = Path(__file__).with_name("assets")
REPORT = {
    "status": "running", "checks": [], "contexts": [], "local_saves": [],
    "provider_attempts": 0, "unexpected": [], "js_errors": [], "layouts": [],
}
TEMP = tempfile.TemporaryDirectory(prefix="ozon-existing-update-browser-")
os.environ.update(
    DATABASE_URL="sqlite:///" + str(Path(TEMP.name) / "app.sqlite"),
    SKIP_SCHEDULER="1",
    IMAGE_LAB_INLINE_WORKER="0",
    ENCRYPTION_KEY=Fernet.generate_key().decode(),
    SECRET_KEY="synthetic-ozon-exact-update-browser",
    OZON_RATE_LIMIT_DIR=str(Path(TEMP.name) / "limits"),
)


def forbid_network(*_args, **_kwargs):
    REPORT["provider_attempts"] += 1
    raise AssertionError("Provider I/O is forbidden in this fixture")


requests.sessions.Session.request = forbid_network
socket.create_connection = forbid_network

from models import (
    ImportedProduct, MarketplaceListing, MarketplaceListingLinkEvent,
    MarketplaceOperation, MarketplaceProductDraft, db,
)
from seller_platform import app
from tests.ozon_release.seed import PHOTO, PASSWORD, USERNAME, seed


app.config.update(
    TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False,
    MARKETPLACE_OZON_ENABLED=True, MARKETPLACE_OZON_PUBLICATION_ENABLED=False,
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=False,
)
fixture = seed(app)
listing_ids = fixture["listing_ids"][:2]
source_ids = {}
with app.app_context():
    for listing_id in listing_ids:
        listing = db.session.get(MarketplaceListing, listing_id)
        source = ImportedProduct(
            seller_id=fixture["seller_id"], source_type="synthetic",
            external_id=f"source-for-{listing_id}",
            external_vendor_code=listing.offer_id, title=listing.title,
            photo_urls=json.dumps([PHOTO]),
        )
        db.session.add(source)
        db.session.flush()
        source_ids[listing_id] = source.id
        listing.imported_product_id = source.id
        listing.link_status = "linked"
        listing.link_source = "exact_source_identity"
        listing.link_version = 1
        listing.linked_at = listing.last_seen_at = datetime.utcnow()
        listing.link_evidence_json = "{}"
        listing.attributes_json = listing.complex_attributes_json = "[]"
        listing.dimensions_json = json.dumps({
            "width": 20, "height": 3, "depth": 30, "weight": 250,
            "dimension_unit": "CENTIMETERS", "weight_unit": "GRAMS",
        })
        listing.barcodes_json = json.dumps(["4600000000001"])
        listing.price_summary_json = json.dumps({
            "currency": "RUB", "available": True,
            "values": {
                "price": "1000", "old_price": "1200",
                "currency_code": "RUB", "vat": "0.22",
            },
        })
    db.session.commit()
    initial_draft_count = MarketplaceProductDraft.query.count()
    original_links = {
        listing.id: (listing.imported_product_id, listing.link_version)
        for listing in MarketplaceListing.query.filter(
            MarketplaceListing.id.in_(listing_ids),
        )
    }

logging.getLogger("werkzeug").setLevel(logging.ERROR)
server = make_server("127.0.0.1", 0, app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{server.server_port}"
assets = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
for asset in assets.values():
    assert hashlib.sha256((ASSETS / asset["file"]).read_bytes()).hexdigest() == asset["sha256"]

contexts = {}
saved_urls = {}
lost_response_id = listing_ids[1]


def listing_url(listing_id):
    return f"/marketplaces/listings/view/{listing_id}"


def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname == "127.0.0.1" and url.port == server.server_port:
        method = request.method
        path = url.path
        if method not in ("GET", "HEAD") and (method, path) not in {
            ("POST", "/login"), ("POST", "/marketplaces/drafts/"),
        }:
            REPORT["unexpected"].append((method, path))
            route.abort()
            return
        response = route.fetch(max_redirects=0)
        if method == "GET" and path.endswith("/update-draft-context"):
            listing_id = int(path.split("/")[-2])
            result = response.json()
            assert response.status == 200 and result["success"] is True, result
            assert result["imported_product_id"] == source_ids[listing_id]
            assert result["account_id"] == fixture["account_id"]
            REPORT["contexts"].append([listing_id, result["status"]])
            if result["status"] == "ready":
                contexts[listing_id] = result["expected_existing_listing"]
            else:
                assert result["status"] == "existing"
                assert result["draft_url"] == saved_urls[listing_id]
        elif method == "POST" and path == "/marketplaces/drafts/":
            body = json.loads(request.post_data or "{}")
            proof = body.pop("expected_existing_listing")
            listing_id = proof["listing_id"]
            assert proof == contexts[listing_id] and listing_id not in saved_urls
            assert body == {
                "account_id": fixture["account_id"],
                "imported_product_id": source_ids[listing_id],
            }
            assert request.headers.get("x-csrftoken")
            outcome = response.json()
            assert response.status == 201 and outcome["success"] is True, outcome
            draft_id = outcome["draft"]["id"]
            saved_urls[listing_id] = f"/marketplaces/drafts/{draft_id}"
            REPORT["local_saves"].append([listing_id, draft_id])
            if listing_id == lost_response_id:
                route.abort("failed")
                return
        if response.status >= 500:
            REPORT["unexpected"].append((method, path, response.status))
        route.fulfill(response=response)
        return

    asset = assets.get(request.url) or assets.get(request.url.rstrip("/"))
    if asset:
        route.fulfill(body=(ASSETS / asset["file"]).read_bytes(), content_type=asset["content_type"])
    elif request.url == PHOTO:
        route.fulfill(
            body='<svg xmlns="http://www.w3.org/2000/svg" width="80" height="100"/>',
            content_type="image/svg+xml",
        )
    else:
        REPORT["unexpected"].append((request.method, url.hostname, url.path))
        route.abort()


def open_overview(page, listing_id):
    page.goto(base + listing_url(listing_id))
    page.locator("#marketplace-detail-app:not([v-cloak])").wait_for()
    action = page.get_by_role("button", name="Подготовить обновление", exact=True)
    action.wait_for()
    return action


def test_browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium"),
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900}, service_workers="block")
        page.context.route("**/*", bridge)
        page.on("pageerror", lambda exc: REPORT["js_errors"].append(str(exc)))
        page.goto(base + "/login")
        page.locator('input[name="username"]').fill(USERNAME)
        page.locator('input[name="password"]').fill(PASSWORD)
        page.locator('button[type="submit"]').click()
        page.wait_for_url(lambda value: urlsplit(value).path != "/login")

        target_id = listing_ids[0]
        action = open_overview(page, target_id)
        assert not REPORT["contexts"]
        action.focus()
        action.press("Enter")
        dialog = page.get_by_role("dialog")
        save = dialog.get_by_role("button", name="Сохранить черновик", exact=True)
        save.wait_for()
        assert "Ozon CI 0" in dialog.inner_text()
        assert REPORT["contexts"] == [[target_id, "ready"]]
        assert not REPORT["local_saves"]
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        assert action.evaluate("el => document.activeElement === el")
        REPORT["checks"].append("preview_cancel_keyboard_restores_focus_without_creating")

        page.set_viewport_size({"width": 320, "height": 850})
        action.click()
        save.wait_for()
        close = dialog.get_by_role("button", name="Закрыть", exact=True)
        for theme in ("light", "dark"):
            page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
            action_size = action.bounding_box()
            save_size = save.bounding_box()
            close_size = close.bounding_box()
            assert all(size["height"] >= 44 and size["width"] >= 44 for size in (action_size, save_size, close_size))
            assert close.evaluate("""el => {
                const text = document.createRange();
                text.selectNodeContents(el);
                return text.getClientRects().length === 1;
            }""")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            REPORT["layouts"].append({"width": 320, "theme": theme, "action_height": action_size["height"], "save_height": save_size["height"]})
            page.screenshot(path=OUT / f"exact-update-320-{theme}.png")
        save.click()
        page.wait_for_url(lambda value: urlsplit(value).path == saved_urls.get(target_id))
        REPORT["checks"].append("exact_draft_saved_and_opened_after_explicit_confirmation")

        action = open_overview(page, lost_response_id)
        action.click()
        save.wait_for()
        save.click()
        recover = dialog.get_by_role("button", name="Проверить результат", exact=True)
        recover.wait_for()
        assert REPORT["local_saves"] == [
            [target_id, int(saved_urls[target_id].split("/")[-1])],
            [lost_response_id, int(saved_urls[lost_response_id].split("/")[-1])],
        ]
        recover.click()
        dialog.get_by_role("link", name="Открыть существующий черновик", exact=True).click()
        page.wait_for_url(lambda value: urlsplit(value).path == saved_urls.get(lost_response_id))
        assert [value for value in REPORT["contexts"] if value[0] == lost_response_id] == [
            [lost_response_id, "ready"], [lost_response_id, "existing"],
        ]
        REPORT["checks"].append("lost_response_read_recovery_opens_same_draft_without_resending")
        browser.close()


try:
    test_browser()
    with app.app_context():
        assert MarketplaceProductDraft.query.count() == initial_draft_count + 2
        for listing_id, draft_id in REPORT["local_saves"]:
            draft = db.session.get(MarketplaceProductDraft, draft_id)
            assert draft.seller_id == fixture["seller_id"]
            assert draft.account_id == fixture["account_id"]
            assert draft.published_listing_id == listing_id
            assert draft.imported_product_id == source_ids[listing_id]
            listing = db.session.get(MarketplaceListing, listing_id)
            assert (listing.imported_product_id, listing.link_version) == original_links[listing_id]
        assert MarketplaceOperation.query.count() == 0
        assert MarketplaceListingLinkEvent.query.count() == 0
    assert not any(REPORT[key] for key in ("provider_attempts", "unexpected", "js_errors")), REPORT
    REPORT["checks"].append("exact_seller_account_and_links_preserved_without_publication")
    REPORT["status"] = "passed"
except Exception:
    REPORT["status"] = "failed"
    raise
finally:
    server.shutdown()
    thread.join(timeout=5)
    TEMP.cleanup()
    (OUT / "ozon-existing-update-browser.json").write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8",
    )
