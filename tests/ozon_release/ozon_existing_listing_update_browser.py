"""Prepare exact Ozon update drafts through the ordinary overview and catalog."""

from datetime import datetime
import base64
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import socket
import tempfile
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

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
    "bulk_update": {}, "workspace_update": {},
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
    BackgroundJob, ImportedProduct, MarketplaceListing, MarketplaceListingLinkEvent,
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
bulk_ready_id = fixture["listing_ids"][2]
bulk_unlinked_id = fixture["listing_ids"][3]
bulk_listing_ids = {listing_ids[0], bulk_ready_id, bulk_unlinked_id}
workspace_listing_ids = fixture["listing_ids"][4:6]
bulk_catalog = "/marketplaces/listings/?" + urlencode({
    "marketplace": "ozon", "account_id": fixture["account_id"],
    "status": "active", "per_page": 2, "search": "R47 набор",
})
source_ids = {}
with app.app_context():
    for listing_id in bulk_listing_ids:
        listing = db.session.get(MarketplaceListing, listing_id)
        listing.title = f"R47 набор · {listing.offer_id}"
    for index, listing_id in enumerate(workspace_listing_ids):
        listing = db.session.get(MarketplaceListing, listing_id)
        listing.title = f"R56 workspace item {index}"
    for listing_id in [*listing_ids, bulk_ready_id, *workspace_listing_ids]:
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
    workspace_unselected_source = ImportedProduct(
        seller_id=fixture["seller_id"], source_type="synthetic",
        external_id="r56-workspace-unrelated-source",
        external_vendor_code="r56-workspace-unrelated-source",
        title="R56 workspace item 2", photo_urls=json.dumps([PHOTO]),
    )
    db.session.add(workspace_unselected_source)
    db.session.flush()
    workspace_unselected_source_id = workspace_unselected_source.id
    db.session.commit()
    initial_draft_count = MarketplaceProductDraft.query.count()
    original_links = {
        listing.id: (listing.imported_product_id, listing.link_version)
        for listing in MarketplaceListing.query.filter(
            MarketplaceListing.id.in_([*listing_ids, bulk_ready_id, bulk_unlinked_id, *workspace_listing_ids]),
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
bulk_state = {"selected": [], "preview": 0, "apply": 0, "token": None, "report": None}
workspace_source_ids = [source_ids[listing_id] for listing_id in workspace_listing_ids]
workspace_state = {
    "active": False, "handoff": 0, "preview": 0, "apply": 0,
    "token": None, "confirm_token": None, "return_to": None,
    "listing_ids": workspace_listing_ids[:], "source_ids": workspace_source_ids[:],
    "unselected_source_id": workspace_unselected_source_id,
    "report": None, "catalog_reads": [],
}
bulk_preview_path = "/marketplaces/listings/update-drafts/preview"
bulk_apply_path = "/marketplaces/listings/update-drafts/apply"
workspace_handoff_path = "/marketplaces/listings/update-drafts/workspace-handoff"


def listing_url(listing_id):
    return f"/marketplaces/listings/view/{listing_id}"


def _decode_unpadded_base64url(value):
    assert isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value)
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def decode_workspace_selection(value):
    raw = _decode_unpadded_base64url(value)
    assert raw and len(raw) % 8 == 0
    return [int.from_bytes(raw[index:index + 8], "big") for index in range(0, len(raw), 8)]


def decode_workspace_return(fragment):
    match = re.fullmatch(r"workspace-return=([A-Za-z0-9_-]+)", fragment)
    assert match
    return _decode_unpadded_base64url(match.group(1)).decode("utf-8")


def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname == "127.0.0.1" and url.port == server.server_port:
        method = request.method
        path = url.path
        if method not in ("GET", "HEAD") and (method, path) not in {
            ("POST", "/login"), ("POST", "/marketplaces/drafts/"),
            ("POST", bulk_preview_path), ("POST", bulk_apply_path),
            ("POST", workspace_handoff_path),
        }:
            REPORT["unexpected"].append((method, path))
            route.abort()
            return
        if method == "GET" and path == "/marketplaces/listings/":
            query = parse_qs(url.query)
            tokens = query.get("workspace_update", [])
            if tokens:
                assert tokens == ["1"]
                workspace_state["active"] = True
        if method == "GET" and workspace_state["active"] and path in {
            "/marketplaces/listings/api", "/marketplaces/listings/groups",
            "/marketplaces/listings/facets",
        }:
            workspace_state["catalog_reads"].append(path)
        response = route.fetch(max_redirects=0)
        if method == "POST" and path == workspace_handoff_path:
            body = json.loads(request.post_data or "{}")
            assert request.headers.get("x-csrftoken")
            assert set(body) == {"account_id", "source_ids", "listing_ids", "return_to"}
            assert body["account_id"] == fixture["account_id"]
            assert body["source_ids"] == workspace_state["source_ids"]
            assert body["listing_ids"] == workspace_state["listing_ids"]
            return_url = urlsplit(body["return_to"])
            assert return_url.path == "/my-products/beta" and not return_url.query
            assert return_url.fragment.startswith("workspace-return=")
            assert len(return_url.fragment.split("=", 1)[1]) == 32
            assert response.status == 200 and "no-store" in response.headers.get("cache-control", "")
            outcome = response.json()
            assert outcome["success"] is True
            handoff = urlsplit(outcome["url"])
            assert handoff.path == "/marketplaces/listings/"
            query = parse_qs(handoff.query)
            assert query["marketplace"] == ["ozon"]
            assert query["account_id"] == [str(fixture["account_id"])]
            assert query["workspace_update"] == ["1"]
            assert len(handoff.path + "?" + handoff.query) < 256
            token_match = re.fullmatch(r"workspace-update=([A-Za-z0-9_.-]{1,32768})", handoff.fragment)
            assert token_match
            workspace_state["token"] = token_match.group(1)
            workspace_state["return_to"] = body["return_to"]
            workspace_state["handoff_url"] = outcome["url"]
            workspace_state["handoff"] += 1
        elif method == "POST" and path == bulk_preview_path and workspace_state["active"]:
            body = json.loads(request.post_data or "{}")
            assert request.headers.get("x-csrftoken")
            assert set(body) == {"workspace_update"}
            assert body["workspace_update"] == workspace_state["token"]
            assert response.status == 200 and "no-store" in response.headers.get("cache-control", "")
            result = response.json()
            assert result["success"] is True
            metadata = result["workspace_update"]
            assert set(metadata) == {"account_id", "account_label", "listing_ids", "return_to"}
            assert metadata["account_id"] == fixture["account_id"]
            assert metadata["account_label"] == "Ozon CI 0"
            assert metadata["listing_ids"] == workspace_state["listing_ids"]
            assert metadata["return_to"] == workspace_state["return_to"]
            preview = result["preview"]
            assert preview["listing_ids"] == workspace_state["listing_ids"]
            assert preview["account_id"] == fixture["account_id"]
            assert preview["account_label"] == "Ozon CI 0"
            assert {row["listing_id"]: row["status"] for row in preview["rows"]} == {
                listing_id: "ready" for listing_id in workspace_state["listing_ids"]
            }
            workspace_state["preview"] += 1
            workspace_state["confirm_token"] = preview["token"]
        elif method == "POST" and path == bulk_preview_path:
            body = json.loads(request.post_data or "{}")
            assert request.headers.get("x-csrftoken")
            assert set(body) == {"account_id", "listing_ids", "return_to"}
            assert body["account_id"] == fixture["account_id"]
            assert body["listing_ids"] == bulk_state["selected"]
            assert urlsplit(body["return_to"]).path == urlsplit(bulk_catalog).path
            expected_query = parse_qs(urlsplit(bulk_catalog).query)
            observed_query = parse_qs(urlsplit(body["return_to"]).query)
            assert {name: observed_query.get(name) for name in expected_query} == expected_query
            assert response.status == 200 and "no-store" in response.headers.get("cache-control", "")
            result = response.json()
            assert result["success"] is True
            preview = result["preview"]
            assert preview["listing_ids"] == bulk_state["selected"]
            assert preview["account_id"] == fixture["account_id"]
            assert {row["listing_id"]: row["status"] for row in preview["rows"]} == {
                listing_ids[0]: "existing", bulk_ready_id: "ready", bulk_unlinked_id: "blocked",
            }
            bulk_state["preview"] += 1
            bulk_state["token"] = preview["token"]
        elif method == "POST" and path == bulk_apply_path and workspace_state["active"]:
            body = json.loads(request.post_data or "{}")
            assert request.headers.get("x-csrftoken")
            assert set(body) == {"token", "confirm_local"}
            assert body["token"] == workspace_state["confirm_token"]
            assert body["confirm_local"] is True
            assert response.status in (200, 201) and "no-store" in response.headers.get("cache-control", "")
            result = response.json()
            assert result["success"] is True
            report = result["report"]
            assert report["listing_ids"] == workspace_state["listing_ids"]
            assert report["account_id"] == fixture["account_id"]
            assert [row["status"] for row in report["rows"]] == ["created"] * len(workspace_state["listing_ids"])
            editor_url = urlsplit(report["editor_url"])
            assert editor_url.path == "/marketplaces/ozon/uploads/selection/repair"
            editor_query = parse_qs(editor_url.query)
            assert editor_query["account_id"] == [str(fixture["account_id"])]
            assert editor_query["workspace_return"] == ["1"]
            draft_ids = [row["draft_id"] for row in report["rows"]]
            assert decode_workspace_selection(editor_query["workspace_selection"][0]) == draft_ids
            assert decode_workspace_return(editor_url.fragment) == workspace_state["return_to"]
            workspace_state["apply"] += 1
            workspace_state["report"] = report
        elif method == "POST" and path == bulk_apply_path:
            body = json.loads(request.post_data or "{}")
            assert request.headers.get("x-csrftoken")
            assert set(body) == {"token", "confirm_local"}
            assert body["token"] == bulk_state["token"] and body["confirm_local"] is True
            assert response.status in (200, 201) and "no-store" in response.headers.get("cache-control", "")
            result = response.json()
            assert result["success"] is True
            report = result["report"]
            assert report["listing_ids"] == bulk_state["selected"]
            assert report["account_id"] == fixture["account_id"]
            assert {row["listing_id"]: row["status"] for row in report["rows"]} == {
                listing_ids[0]: "existing", bulk_ready_id: "created", bulk_unlinked_id: "blocked",
            }
            bulk_state["apply"] += 1
            bulk_state["report"] = report
        elif method == "GET" and path.endswith("/update-draft-context"):
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


def batch_snapshots():
    with app.app_context():
        return {
            "drafts": {
                row.id: (row.version, row.imported_product_id, row.published_listing_id)
                for row in MarketplaceProductDraft.query.order_by(MarketplaceProductDraft.id)
            },
            "links": {
                row.id: (row.imported_product_id, row.link_version)
                for row in MarketplaceListing.query.order_by(MarketplaceListing.id)
            },
            "link_events": MarketplaceListingLinkEvent.query.count(),
            "operations": MarketplaceOperation.query.count(),
            "jobs": BackgroundJob.query.count(),
        }


def exercise_catalog_batch(page):
    before = batch_snapshots()
    page.set_viewport_size({"width": 320, "height": 850})
    page.goto(base + bulk_catalog)
    catalog = page.locator("#marketplace-catalog-app:not([v-cloak])")
    catalog.wait_for()
    choose_updates = catalog.locator("[data-listing-update-switch-to-listings]")
    choose_updates.wait_for()
    choice_bounds = choose_updates.bounding_box()
    assert choice_bounds and choice_bounds["height"] >= 44 and choice_bounds["width"] >= 44
    choose_updates.press("Enter")
    toggle = catalog.locator("[data-listing-update-toggle]")
    checks = catalog.locator("[data-listing-update-select]")
    checks.first.wait_for()
    assert checks.count() == 2
    toggle.press("Space")
    for candidate in checks.all():
        listing_id = int(candidate.get_attribute("data-listing-update-select"))
        assert candidate.is_checked()
        bulk_state["selected"].append(listing_id)

    catalog.get_by_role("button", name="Показать ещё", exact=True).click()
    checks.nth(2).wait_for()
    assert checks.count() == 3
    for candidate in checks.all():
        listing_id = int(candidate.get_attribute("data-listing-update-select"))
        if listing_id in bulk_state["selected"]:
            assert candidate.is_checked()
        else:
            candidate.check()
            bulk_state["selected"].append(listing_id)
    assert set(bulk_state["selected"]) == bulk_listing_ids
    # Supported exclusions stay exact even after fetching another page.
    last = checks.nth(2)
    last.uncheck()
    assert not last.is_checked()
    last.check()

    preview_action = catalog.locator("[data-listing-update-preview]")
    preview_action.scroll_into_view_if_needed()
    size = preview_action.bounding_box()
    assert size and size["height"] >= 44 and size["width"] >= 44
    preview_action.press("Enter")
    dialog = page.locator("dialog[open]")
    confirm = dialog.locator("[data-listing-update-confirm]")
    confirm.wait_for()
    assert confirm.is_disabled()
    preview_text = dialog.inner_text()
    assert "Ozon CI 0" in preview_text
    assert all(f"CI-{number:03}" in preview_text for number in (0, 2, 3))
    assert bulk_state["preview"] == 1 and bulk_state["apply"] == 0
    assert batch_snapshots() == before
    page.keyboard.press("Escape")
    dialog.wait_for(state="hidden")
    assert preview_action.evaluate("element => element === document.activeElement")
    assert batch_snapshots() == before

    preview_action.click()
    confirm.wait_for()
    dialog.get_by_role("checkbox").check()
    assert confirm.is_enabled()
    sizes = []
    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        confirm_size = confirm.bounding_box()
        assert confirm_size and confirm_size["height"] >= 44 and confirm_size["width"] >= 44
        assert confirm_size["x"] >= 0 and confirm_size["x"] + confirm_size["width"] <= 321
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert dialog.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        sizes.append({"width": 320, "theme": theme, "confirm_height": confirm_size["height"]})
        page.screenshot(path=OUT / f"bulk-existing-update-review-320-{theme}.png", animations="disabled")
    confirm.click()
    editor_link = page.locator("[data-listing-update-editor]")
    editor_link.wait_for()
    page.wait_for_function("document.activeElement?.matches('.mcat-update-report h3')")
    assert dialog.locator(".mcat-update-review").count() == 0
    editor_link_size = editor_link.bounding_box()
    assert editor_link_size and editor_link_size["height"] >= 44
    assert editor_link_size["y"] >= 0 and editor_link_size["y"] + editor_link_size["height"] <= 850
    assert bulk_state["preview"] == 2 and bulk_state["apply"] == 1
    report = bulk_state["report"]
    existing = int(saved_urls[listing_ids[0]].split("/")[-1])
    created = next(row["draft_id"] for row in report["rows"] if row["status"] == "created")
    after = batch_snapshots()
    assert after["drafts"].keys() - before["drafts"].keys() == {created}
    assert {key: after["drafts"][key] for key in before["drafts"]} == before["drafts"]
    assert after["drafts"][created][1:] == (source_ids[bulk_ready_id], bulk_ready_id)
    assert {key: value for key, value in after.items() if key != "drafts"} == {
        key: value for key, value in before.items() if key != "drafts"
    }
    href = editor_link.get_attribute("href")
    assert href == report["editor_url"]
    editor_target = urlsplit(href)
    assert editor_target.path == "/marketplaces/ozon/uploads/selection/repair"
    editor_query = parse_qs(editor_target.query)
    assert editor_query["account_id"] == [str(fixture["account_id"])]
    assert set(map(int, editor_query["draft_ids"][0].split(","))) == {existing, created}
    page.screenshot(path=OUT / "bulk-existing-update-receipt-320.png", animations="disabled")
    editor_link.click()
    editor = page.locator("[data-draft-selection-editor]:not([v-cloak])")
    editor.wait_for()
    assert {int(row.get_attribute("data-draft")) for row in editor.locator(".orb-row").all()} == {existing, created}
    return_to_catalog = editor.get_by_role("link", name="К каталогу Ozon", exact=True)
    return_to_catalog.click()
    catalog.wait_for()
    return_query = parse_qs(urlsplit(page.url).query)
    assert urlsplit(page.url).path == urlsplit(bulk_catalog).path
    expected_query = parse_qs(urlsplit(bulk_catalog).query)
    assert {name: return_query.get(name) for name in expected_query} == expected_query
    checks.first.wait_for()
    assert all(selected.is_checked() for selected in checks.all())
    assert batch_snapshots() == after
    REPORT["bulk_update"] = {
        "selected": len(bulk_state["selected"]), "created": 1, "existing": 1, "blocked": 1,
        "preview_requests": bulk_state["preview"], "confirm_requests": bulk_state["apply"],
        "layouts": sizes, "catalog_scope_preserved": True, "operation_delta": 0,
        "focused_result": True, "editor_visible_without_scroll": True, "duplicate_preview_hidden": True,
    }
    REPORT["checks"].append("catalog_selected_update_reuses_guarded_drafts_and_preserves_filtered_return")


def exercise_workspace_update(page):
    before = batch_snapshots()
    page.set_viewport_size({"width": 320, "height": 850})
    page.goto(base + "/my-products/beta")
    workspace = page.locator("#my-products-app:not([v-cloak])")
    workspace.wait_for()
    search = page.get_by_role("searchbox", name="Название, бренд или артикул")
    workspace_titles = [
        "R56 workspace item 0", "R56 workspace item 1", "R56 workspace item 2",
    ]
    with page.expect_response(lambda response: (
        response.request.method == "GET"
        and urlsplit(response.url).path == "/api/my-products"
        and parse_qs(urlsplit(response.url).query).get("search") == ["R56 workspace item"]
        and parse_qs(urlsplit(response.url).query).get("sort") == ["title"]
    )) as filtered_feed_info:
        search.fill("R56 workspace item")
        page.get_by_label("Сортировка").select_option("title")
    filtered_feed = filtered_feed_info.value
    assert filtered_feed.status == 200
    filtered_data = filtered_feed.json()
    assert filtered_data["success"] is True
    assert filtered_data["pagination"]["total"] == 3
    assert [item["title"] for item in filtered_data["items"]] == workspace_titles
    rows = workspace.locator("article.mp-row")
    page.wait_for_function("""expected => {
        const rows = Array.from(document.querySelectorAll('#my-products-app article.mp-row'));
        return rows.length === expected.length && rows.every((row, index) =>
            row.querySelector('.mp-title')?.textContent.trim() === expected[index]);
    }""", arg=workspace_titles)
    assert rows.count() == 3
    expected_titles = ["R56 workspace item 0", "R56 workspace item 1"]
    for title in expected_titles:
        workspace.get_by_role("checkbox", name="Выбрать " + title, exact=True).check()
    chosen = workspace.get_by_role("button", name="Что сделать", exact=True)
    chosen.click()
    workspace_update_action = workspace.get_by_role(
        "menuitem", name=re.compile(r"^Изменить карточки Ozon(?:$|\s)"),
    )
    assert workspace_update_action.count() == 1
    assert workspace_update_action.is_enabled()
    workspace_update_action.click()

    page.wait_for_url(lambda value: urlsplit(value).path == "/marketplaces/listings/")
    handoff_url = urlsplit(page.url)
    query = parse_qs(handoff_url.query)
    assert set(query) == {"account_id", "marketplace", "workspace_update"}
    assert query["account_id"] == [str(fixture["account_id"])]
    assert query["marketplace"] == ["ozon"] and query["workspace_update"] == ["1"]
    assert handoff_url.fragment.startswith("workspace-update=")
    assert len(page.url.encode("utf-8")) < 4094

    catalog = page.locator("#marketplace-catalog-app:not([v-cloak])")
    catalog.wait_for()
    assert catalog.locator(".mcat-head").count() == 0
    shell = catalog.locator("[data-workspace-update]")
    shell.wait_for()
    dialog = page.locator("dialog.mcat-update-dialog[open]")
    confirm = dialog.locator("[data-listing-update-confirm]")
    confirm_checkbox = dialog.get_by_role("checkbox")
    confirm.wait_for()
    assert confirm_checkbox.evaluate("element => element === document.activeElement")
    preview_text = dialog.inner_text()
    assert "Ozon CI 0" in preview_text
    assert all(title in preview_text for title in expected_titles)
    assert workspace_state["handoff"] == 1 and workspace_state["preview"] == 1
    assert workspace_state["apply"] == 0 and not workspace_state["catalog_reads"]
    assert batch_snapshots() == before

    reopen = shell.locator("[data-workspace-update-reopen]")
    return_link = shell.locator("[data-workspace-return]")
    assert return_link.is_visible()
    assert return_link.get_attribute("href") == workspace_state["return_to"]
    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        reopen_size = reopen.bounding_box()
        confirm_size = confirm.bounding_box()
        assert reopen_size and reopen_size["height"] >= 44 and reopen_size["width"] >= 44
        assert confirm_size and confirm_size["height"] >= 44 and confirm_size["width"] >= 44
        assert confirm_size["x"] >= 0 and confirm_size["x"] + confirm_size["width"] <= 321
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert dialog.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        REPORT["layouts"].append({"width": 320, "theme": theme, "workspace_confirm_height": confirm_size["height"]})
        page.screenshot(path=OUT / f"workspace-existing-update-320-{theme}.png", animations="disabled")

    page.keyboard.press("Escape")
    dialog.wait_for(state="hidden")
    assert reopen.evaluate("element => element === document.activeElement")
    assert shell.locator("[data-workspace-return]").get_attribute("href") == workspace_state["return_to"]
    reopen.click()
    confirm.wait_for()
    assert confirm_checkbox.evaluate("element => element === document.activeElement")
    assert workspace_state["preview"] == 2 and workspace_state["apply"] == 0
    assert batch_snapshots() == before

    dialog.get_by_role("checkbox").check()
    assert confirm.is_enabled()
    confirm.click()
    editor_link = dialog.locator("[data-listing-update-editor]")
    editor_link.wait_for()
    editor_url = urlsplit(editor_link.get_attribute("href"))
    editor_query = parse_qs(editor_url.query)
    assert editor_url.path == "/marketplaces/ozon/uploads/selection/repair"
    assert editor_query["account_id"] == [str(fixture["account_id"])]
    assert editor_query["workspace_return"] == ["1"]
    assert decode_workspace_return(editor_url.fragment) == workspace_state["return_to"]
    assert workspace_state["apply"] == 1
    assert [row["status"] for row in workspace_state["report"]["rows"]] == ["created", "created"]
    created_ids = [row["draft_id"] for row in workspace_state["report"]["rows"]]
    after = batch_snapshots()
    assert after["drafts"].keys() - before["drafts"].keys() == set(created_ids)
    assert {key: after["drafts"][key] for key in before["drafts"]} == before["drafts"]
    assert {key: value for key, value in after.items() if key != "drafts"} == {
        key: value for key, value in before.items() if key != "drafts"
    }

    editor_link.click()
    editor = page.locator("[data-draft-selection-editor]:not([v-cloak])")
    editor.wait_for()
    assert {int(row.get_attribute("data-draft")) for row in editor.locator(".orb-row").all()} == set(created_ids)
    return_link = editor.locator("[data-existing-update-catalog-return]")
    return_link.wait_for()
    assert return_link.inner_text().strip().endswith("К моим товарам")
    assert return_link.get_attribute("href") == workspace_state["return_to"]
    with page.expect_response(lambda response: (
        response.request.method == "GET"
        and urlsplit(response.url).path == "/api/my-products"
        and parse_qs(urlsplit(response.url).query).get("search") == ["R56 workspace item"]
        and parse_qs(urlsplit(response.url).query).get("sort") == ["title"]
    )) as restored_feed_info:
        return_link.click()
    restored_feed = restored_feed_info.value
    assert restored_feed.status == 200
    restored_data = restored_feed.json()
    assert restored_data["success"] is True
    assert restored_data["pagination"]["total"] == 3
    assert [item["title"] for item in restored_data["items"]] == workspace_titles
    workspace.wait_for()
    page.wait_for_function("""expected => {
        const rows = Array.from(document.querySelectorAll('#my-products-app article.mp-row'));
        if (rows.length !== expected.length) return false;
        const titles = rows.map(row => row.querySelector('.mp-title')?.textContent.trim());
        const selected = rows.map(row => !!row.querySelector('.mp-check input')?.checked);
        return titles.every((title, index) => title === expected[index]) &&
            selected.join(',') === 'true,true,false';
    }""", arg=workspace_titles)
    assert search.input_value() == "R56 workspace item"
    assert page.get_by_label("Сортировка").input_value() == "title"
    for title in expected_titles:
        assert workspace.get_by_role("checkbox", name="Выбрать " + title, exact=True).is_checked()
    assert not workspace.get_by_role("checkbox", name="Выбрать R56 workspace item 2", exact=True).is_checked()
    assert batch_snapshots() == after
    assert not workspace_state["catalog_reads"]

    invalid_shell = "/marketplaces/listings/?" + urlencode({
        "marketplace": "ozon", "account_id": fixture["account_id"], "workspace_update": "1",
    })
    preview_count = workspace_state["preview"]
    page.goto(base + invalid_shell)
    catalog.wait_for()
    assert catalog.locator("[data-workspace-update]").is_visible()
    assert catalog.locator(".mcat-head").count() == 0
    assert page.locator("dialog.mcat-update-dialog[open]").count() == 0
    assert catalog.locator("[data-workspace-return]").get_attribute("href") == "/my-products/beta"
    assert workspace_state["preview"] == preview_count
    assert not workspace_state["catalog_reads"]

    REPORT["workspace_update"] = {
        "handoff_requests": workspace_state["handoff"],
        "preview_requests": workspace_state["preview"],
        "apply_requests": workspace_state["apply"],
        "source_ids_count": len(workspace_state["source_ids"]),
        "listing_ids_count": len(workspace_state["listing_ids"]),
        "visible_rows": 3,
        "catalog_reads": len(workspace_state["catalog_reads"]),
        "return_scope_kind": "ordinary_filtered",
        "filters_preserved": True,
        "selection_preserved": True,
        "unselected_stayed_unselected": True,
        "workspace_return_preserved": True,
        "focused_dialog": True,
        "local_only": True,
        "invalid_fragment_fail_closed": True,
        "signed_batch_return": "not_tested",
    }
    REPORT["checks"].append("my_products_exact_ozon_set_scoped_update_and_filtered_selection_return")


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
        exercise_catalog_batch(page)
        exercise_workspace_update(page)
        browser.close()


try:
    test_browser()
    with app.app_context():
        assert MarketplaceProductDraft.query.count() == initial_draft_count + 5
        for listing_id, draft_id in REPORT["local_saves"]:
            draft = db.session.get(MarketplaceProductDraft, draft_id)
            assert draft.seller_id == fixture["seller_id"]
            assert draft.account_id == fixture["account_id"]
            assert draft.published_listing_id == listing_id
            assert draft.imported_product_id == source_ids[listing_id]
            listing = db.session.get(MarketplaceListing, listing_id)
            assert (listing.imported_product_id, listing.link_version) == original_links[listing_id]
        for listing_id, original_link in original_links.items():
            listing = db.session.get(MarketplaceListing, listing_id)
            assert (listing.imported_product_id, listing.link_version) == original_link
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
