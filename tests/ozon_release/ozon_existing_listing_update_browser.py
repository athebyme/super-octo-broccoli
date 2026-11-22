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
import traceback
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
    "bulk_update": {}, "workspace_update": {}, "signed_import_review_return": {},
    "publication_attempts_blocked": 0, "local_editor_save_posts": 0,
    "failure": None, "stage": "startup",
}
ACTIVE_PAGE = None
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
    MarketplaceOperation, MarketplaceProductDraft, MarketplaceProductType,
    Seller, SellerMarketplaceAccount, db,
)
from routes.suppliers import _issue_supplier_import_batch_token
from seller_platform import app
from services.ozon_bulk_repair import decode_compact_draft_ids, encode_compact_draft_ids
from services.ozon_review_navigation import OzonReviewNavigation
from services.marketplace_drafts import MarketplaceDraftService
from tests.ozon_release.seed import PHOTO, PASSWORD, USERNAME, seed


app.config.update(
    TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False,
    MARKETPLACE_OZON_ENABLED=True, MARKETPLACE_OZON_PUBLICATION_ENABLED=False,
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=False,
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
)
fixture = seed(app)
listing_ids = fixture["listing_ids"][:2]
bulk_ready_id = fixture["listing_ids"][2]
bulk_unlinked_id = fixture["listing_ids"][3]
bulk_listing_ids = {listing_ids[0], bulk_ready_id, bulk_unlinked_id}
workspace_listing_ids = fixture["listing_ids"][4:6]
signed_workspace_listing_ids = fixture["listing_ids"][6:28]
signed_workspace_selected_count = 21
bulk_catalog = "/marketplaces/listings/?" + urlencode({
    "marketplace": "ozon", "account_id": fixture["account_id"],
    "status": "active", "per_page": 2, "search": "R47 набор",
})
source_ids = {}
with app.app_context():
    signed_workspace_user_id = db.session.get(Seller, fixture["seller_id"]).user_id
    ozon_account = db.session.get(SellerMarketplaceAccount, fixture["account_id"])
    ozon_account.settings_json = json.dumps({"default_vat": "0.22"})
    for listing_id in bulk_listing_ids:
        listing = db.session.get(MarketplaceListing, listing_id)
        listing.title = f"R47 набор · {listing.offer_id}"
    for index, listing_id in enumerate(workspace_listing_ids):
        listing = db.session.get(MarketplaceListing, listing_id)
        listing.title = f"R56 workspace item {index}"
    for index, listing_id in enumerate(signed_workspace_listing_ids):
        listing = db.session.get(MarketplaceListing, listing_id)
        listing.title = f"R57 signed import item {index:02d}"
    for listing_id in [
        *listing_ids, bulk_ready_id, *workspace_listing_ids,
        *signed_workspace_listing_ids,
    ]:
        listing = db.session.get(MarketplaceListing, listing_id)
        is_signed_workspace_source = listing_id in signed_workspace_listing_ids
        original_data = None
        source_description = None
        source_category = None
        if is_signed_workspace_source:
            product_type = db.session.get(
                MarketplaceProductType, listing.product_type_id,
            )
            source_description = (
                f"Синтетическое описание {listing.offer_id} для проверки возврата."
            )
            source_category = product_type.category.name
            package_dimensions = {
                "Ширина упаковки, мм": 200,
                "Высота упаковки, мм": 30,
                "Длина упаковки, мм": 300,
                "Вес упаковки, г": 250,
            }
            # Make the final selected source the one actionable issue on page 2.
            if listing_id == signed_workspace_listing_ids[signed_workspace_selected_count - 1]:
                package_dimensions.pop("Вес упаковки, г")
            original_data = json.dumps({
                "title": listing.title,
                "description": source_description,
                "category": source_category,
                "vendor_code": listing.offer_id,
                "barcodes": ["4600000000001"],
                "photo_urls": [PHOTO],
                "dimensions": package_dimensions,
            }, ensure_ascii=False)
        source = ImportedProduct(
            seller_id=fixture["seller_id"], source_type="synthetic",
            external_id=f"source-for-{listing_id}",
            external_vendor_code=listing.offer_id, title=listing.title,
            description=source_description,
            category=source_category,
            photo_urls="[]" if is_signed_workspace_source else json.dumps([PHOTO]),
            original_data=original_data,
            calculated_price=1000.0 if is_signed_workspace_source else None,
            calculated_price_before_discount=1200.0 if is_signed_workspace_source else None,
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
        listing_dimensions = {
            "width": 20, "height": 3, "depth": 30, "weight": 250,
            "dimension_unit": "CENTIMETERS", "weight_unit": "GRAMS",
        }
        listing.dimensions_json = json.dumps(listing_dimensions)
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
    signed_workspace_source_ids = [
        source_ids[listing_id] for listing_id in signed_workspace_listing_ids
    ]
    signed_import_batch = _issue_supplier_import_batch_token(
        user_id=signed_workspace_user_id,
        seller_id=fixture["seller_id"],
        imported_product_ids=signed_workspace_source_ids,
    )
    assert signed_import_batch
    signed_workspace_existing_listing_id = (
        signed_workspace_listing_ids[signed_workspace_selected_count - 1]
    )
    # Mint the exact linked draft while its safe live snapshot is complete,
    # then give the saved draft a synthetic zero weight to expose a real
    # physical-fact issue without weakening listing readiness.
    existing_context = MarketplaceDraftService.existing_listing_update_draft_context(
        seller_id=fixture["seller_id"],
        listing_id=signed_workspace_existing_listing_id,
    )
    signed_workspace_existing_draft = MarketplaceDraftService.create_draft(
        seller_id=fixture["seller_id"],
        account_id=fixture["account_id"],
        imported_product_id=source_ids[signed_workspace_existing_listing_id],
        expected_existing_listing=existing_context["expected_existing_listing"],
    )
    existing_dimensions = json.loads(signed_workspace_existing_draft.dimensions_json)
    existing_dimensions["weight"] = "0"
    signed_workspace_existing_draft.dimensions_json = json.dumps(existing_dimensions)
    db.session.commit()
    signed_workspace_existing_draft_id = signed_workspace_existing_draft.id
    initial_draft_count = MarketplaceProductDraft.query.count()
    original_links = {
        listing.id: (listing.imported_product_id, listing.link_version)
        for listing in MarketplaceListing.query.filter(
            MarketplaceListing.id.in_([
                *listing_ids, bulk_ready_id, bulk_unlinked_id,
                *workspace_listing_ids, *signed_workspace_listing_ids,
            ]),
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
signed_workspace_state = {
    "active": False, "handoff": 0, "preview": 0, "apply": 0,
    "token": None, "confirm_token": None, "return_to": None,
    "local_save_posts": 0, "local_save_post": None,
    "local_save_target_id": None, "local_save_description": None,
    "compact_selection": None, "scope_key": None,
    "listing_ids": signed_workspace_listing_ids[:signed_workspace_selected_count],
    "source_ids": signed_workspace_source_ids[:signed_workspace_selected_count],
    "existing_draft_id": signed_workspace_existing_draft_id,
    "import_batch": signed_import_batch,
    "feed_reads": [], "review_api_reads": [], "report": None,
    "catalog_reads": [],
}
active_workspace_state = workspace_state
bulk_preview_path = "/marketplaces/listings/update-drafts/preview"
bulk_apply_path = "/marketplaces/listings/update-drafts/apply"
workspace_handoff_path = "/marketplaces/listings/update-drafts/workspace-handoff"
selection_apply_path = "/marketplaces/ozon/uploads/selection/repair/apply"


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


def encode_workspace_return(value):
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(b"=").decode("ascii")
    return "workspace-return=" + encoded


def review_bootstrap(page):
    return page.evaluate(
        "() => JSON.parse(document.querySelector('#our-bootstrap').textContent).review"
    )


def read_review_api(page, *, page_number, compact_selection):
    query = urlencode({
        "account_id": fixture["account_id"],
        "workspace_selection": compact_selection,
        "page": page_number,
    })
    return page.evaluate("""async url => {
        const response = await fetch(url, {
            credentials: 'same-origin', cache: 'no-store',
            headers: {Accept: 'application/json'}
        });
        return {
            status: response.status,
            cacheControl: response.headers.get('cache-control') || '',
            body: await response.json(),
        };
    }""", "/marketplaces/ozon/uploads/api/review?" + query)


def safe_location(page):
    if page is None:
        return None
    try:
        parsed = urlsplit(page.url)
        return {
            "path": parsed.path,
            "query_keys": sorted(parse_qs(parsed.query, keep_blank_values=True)),
            "fragment_present": bool(parsed.fragment),
        }
    except Exception:
        return {"path": "unavailable", "query_keys": [], "fragment_present": False}


def safe_failure_message(exc):
    message = str(exc)
    secrets_to_remove = [
        signed_import_batch,
        workspace_state.get("token"),
        workspace_state.get("confirm_token"),
        workspace_state.get("return_to"),
        signed_workspace_state.get("token"),
        signed_workspace_state.get("confirm_token"),
        signed_workspace_state.get("return_to"),
    ]
    for secret in secrets_to_remove:
        if isinstance(secret, str) and secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(
        r"(?i)(import_batch=)[^&#\s]+", r"\1[redacted]", message,
    )
    message = re.sub(
        r"(?i)(workspace-return=)[A-Za-z0-9_-]+", r"\1[redacted]", message,
    )
    message = re.sub(
        r"(?i)(workspace-update=)[A-Za-z0-9_.-]+", r"\1[redacted]", message,
    )
    message = re.sub(
        r"(?i)(review_return=)[A-Za-z0-9_.-]+", r"\1[redacted]", message,
    )
    return message[:500]


def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname == "127.0.0.1" and url.port == server.server_port:
        method = request.method
        path = url.path
        state = active_workspace_state
        if method == "POST" and path in {
            "/marketplaces/ozon/uploads/",
            "/marketplaces/ozon/uploads/review/publish",
            "/marketplaces/ozon/uploads/publish",
        }:
            REPORT["publication_attempts_blocked"] += 1
            REPORT["unexpected"].append((method, path, "publication is forbidden"))
            route.abort()
            return
        if method not in ("GET", "HEAD") and (method, path) not in {
            ("POST", "/login"), ("POST", "/marketplaces/drafts/"),
            ("POST", bulk_preview_path), ("POST", bulk_apply_path),
            ("POST", workspace_handoff_path), ("POST", selection_apply_path),
        }:
            REPORT["unexpected"].append((method, path))
            route.abort()
            return
        if method == "GET" and path == "/marketplaces/listings/":
            query = parse_qs(url.query)
            tokens = query.get("workspace_update", [])
            if tokens:
                assert tokens == ["1"]
                state["active"] = True
        if method == "GET" and state["active"] and path in {
            "/marketplaces/listings/api", "/marketplaces/listings/groups",
            "/marketplaces/listings/facets",
        }:
            state["catalog_reads"].append(path)
        response = route.fetch(max_redirects=0)
        if method == "GET" and path == "/api/my-products":
            query = parse_qs(url.query)
            if state.get("import_batch") and query.get("import_batch") == [state["import_batch"]]:
                state["feed_reads"].append({
                    "status": response.status,
                    "no_store": "no-store" in response.headers.get("cache-control", ""),
                    "query_keys": sorted(query),
                })
        if method == "GET" and path in {
            "/marketplaces/ozon/uploads/review",
            "/marketplaces/ozon/uploads/api/review",
        }:
            query = parse_qs(url.query)
            state.setdefault("review_api_reads", []).append({
                "path": path,
                "status": response.status,
                "page": query.get("page", ["1"])[0],
                "compact_selection": bool(query.get("workspace_selection")),
                "legacy_draft_ids": "draft_ids" in query,
            })
        if method == "POST" and path == workspace_handoff_path:
            body = json.loads(request.post_data or "{}")
            assert request.headers.get("x-csrftoken")
            assert set(body) == {"account_id", "source_ids", "listing_ids", "return_to"}
            assert body["account_id"] == fixture["account_id"]
            assert body["source_ids"] == state["source_ids"]
            assert body["listing_ids"] == state["listing_ids"]
            return_url = urlsplit(body["return_to"])
            assert return_url.path == "/my-products/beta"
            return_query = parse_qs(return_url.query, keep_blank_values=True)
            if state.get("import_batch"):
                assert return_query == {"import_batch": [state["import_batch"]]}
            else:
                assert not return_query
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
            state["token"] = token_match.group(1)
            state["return_to"] = body["return_to"]
            state["handoff_url"] = outcome["url"]
            state["handoff"] += 1
        elif method == "POST" and path == bulk_preview_path and state["active"]:
            body = json.loads(request.post_data or "{}")
            assert request.headers.get("x-csrftoken")
            assert set(body) == {"workspace_update"}
            assert body["workspace_update"] == state["token"]
            assert response.status == 200 and "no-store" in response.headers.get("cache-control", "")
            result = response.json()
            assert result["success"] is True
            metadata = result["workspace_update"]
            assert set(metadata) == {"account_id", "account_label", "listing_ids", "return_to"}
            assert metadata["account_id"] == fixture["account_id"]
            assert metadata["account_label"] == "Ozon CI 0"
            assert metadata["listing_ids"] == state["listing_ids"]
            assert metadata["return_to"] == state["return_to"]
            preview = result["preview"]
            assert preview["listing_ids"] == state["listing_ids"]
            assert preview["account_id"] == fixture["account_id"]
            assert preview["account_label"] == "Ozon CI 0"
            expected_preview_statuses = (
                ["ready"] * (len(state["listing_ids"]) - 1) + ["existing"]
                if state.get("existing_draft_id") else
                ["ready"] * len(state["listing_ids"])
            )
            observed_preview = [
                {
                    "listing_id": row.get("listing_id"),
                    "status": row.get("status"),
                    "message": row.get("message"),
                }
                for row in preview["rows"]
            ]
            assert [row["status"] for row in preview["rows"]] == expected_preview_statuses, observed_preview
            if state.get("existing_draft_id"):
                assert preview["rows"][-1]["listing_id"] == state["listing_ids"][-1]
                assert preview["rows"][-1]["draft_id"] == state["existing_draft_id"]
            state["preview"] += 1
            state["confirm_token"] = preview["token"]
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
        elif method == "POST" and path == bulk_apply_path and state["active"]:
            body = json.loads(request.post_data or "{}")
            assert request.headers.get("x-csrftoken")
            assert set(body) == {"token", "confirm_local"}
            assert body["token"] == state["confirm_token"]
            assert body["confirm_local"] is True
            assert response.status in (200, 201) and "no-store" in response.headers.get("cache-control", "")
            result = response.json()
            assert result["success"] is True
            report = result["report"]
            assert report["listing_ids"] == state["listing_ids"]
            assert report["account_id"] == fixture["account_id"]
            expected_apply_statuses = (
                ["created"] * (len(state["listing_ids"]) - 1) + ["existing"]
                if state.get("existing_draft_id") else
                ["created"] * len(state["listing_ids"])
            )
            observed_apply = [
                {
                    "listing_id": row.get("listing_id"),
                    "status": row.get("status"),
                    "message": row.get("message"),
                }
                for row in report["rows"]
            ]
            assert [row["status"] for row in report["rows"]] == expected_apply_statuses, observed_apply
            if state.get("existing_draft_id"):
                assert report["rows"][-1]["draft_id"] == state["existing_draft_id"]
            editor_url = urlsplit(report["editor_url"])
            assert editor_url.path == "/marketplaces/ozon/uploads/selection/repair"
            editor_query = parse_qs(editor_url.query)
            assert editor_query["account_id"] == [str(fixture["account_id"])]
            assert editor_query["workspace_return"] == ["1"]
            draft_ids = [row["draft_id"] for row in report["rows"]]
            assert decode_workspace_selection(editor_query["workspace_selection"][0]) == draft_ids
            assert decode_workspace_return(editor_url.fragment) == state["return_to"]
            state["apply"] += 1
            state["report"] = report
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
        elif method == "POST" and path == selection_apply_path:
            assert state is signed_workspace_state
            expected = state.get("local_save_expected")
            assert isinstance(expected, dict)
            query = parse_qs(url.query, keep_blank_values=True)
            assert query == {
                "account_id": [str(fixture["account_id"])],
                "workspace_selection": [expected["compact_selection"]],
            }
            form = parse_qs(request.post_data or "", keep_blank_values=True)
            assert request.headers.get("x-csrftoken")
            assert form.get("csrf_token") and form.get("confirm_local") == ["1"]
            assert form.get("selected_draft_id") == [str(expected["draft_id"])]
            prefix = f"row_{expected['draft_id']}_"
            assert form.get(prefix + "draft_version") == [str(expected["version"])]
            assert form.get(prefix + "imported_product_id") == [str(expected["source_id"])]
            assert form.get(prefix + "description") == [expected["description"]]
            assert all(
                key in {"csrf_token", "confirm_local", "selected_draft_id"}
                or key.startswith(prefix)
                for key in form
            )
            assert state["local_save_posts"] == 0
            assert response.status == 200
            assert "no-store" in response.headers.get("cache-control", "")
            result = response.json()
            assert result["success"] is True
            assert result["mode"] == "draft_selection"
            assert result["scope_key"] == expected["scope_key"]
            report = result["report"]
            assert report["mode"] == "draft_selection"
            assert report["scope_key"] == expected["scope_key"]
            assert report["total"] == report["updated"] == 1
            assert len(report["rows"]) == 1
            assert report["rows"][0]["draft_id"] == expected["draft_id"]
            assert report["rows"][0]["version"] == expected["version"] + 1
            assert report["rows"][0]["status"] in {"ready_to_retry", "needs_input"}
            state["local_save_posts"] += 1
            state["local_save_post"] = {
                "draft_id": expected["draft_id"],
                "expected_version": expected["version"],
                "saved_version": report["rows"][0]["version"],
                "csrf": True,
                "compact_scope_exact": True,
                "status": report["rows"][0]["status"],
            }
            REPORT["local_editor_save_posts"] += 1
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


def draft_business_snapshots():
    with app.app_context():
        return {
            row.id: {
                "version": row.version,
                "imported_product_id": row.imported_product_id,
                "published_listing_id": row.published_listing_id,
                "status": row.status,
                "validation_status": row.validation_status,
                "content_json": row.content_json,
                "media_json": row.media_json,
                "dimensions_json": row.dimensions_json,
                "attributes_json": row.attributes_json,
                "complex_attributes_json": row.complex_attributes_json,
                "commercial_json": row.commercial_json,
                "barcodes_json": row.barcodes_json,
            }
            for row in MarketplaceProductDraft.query.order_by(MarketplaceProductDraft.id)
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


def exercise_signed_import_review_return(page):
    """Preserve a signed My Products origin through paged review and issue edit."""
    global active_workspace_state
    active_workspace_state = signed_workspace_state
    state = signed_workspace_state
    state.update({
        "active": False, "handoff": 0, "preview": 0, "apply": 0,
        "token": None, "confirm_token": None, "return_to": None,
        "local_save_posts": 0, "local_save_post": None,
        "local_save_target_id": None, "local_save_description": None,
        "compact_selection": None, "scope_key": None,
        "local_save_expected": None,
        "report": None, "catalog_reads": [], "feed_reads": [],
        "review_api_reads": [],
    })
    REPORT["stage"] = "signed_import_batch_open"
    before = batch_snapshots()
    unexpected_before = len(REPORT["unexpected"])
    provider_before = REPORT["provider_attempts"]
    publication_before = REPORT["publication_attempts_blocked"]
    source_href = "/my-products/beta?" + urlencode({"import_batch": signed_import_batch})
    source_titles = [
        f"R57 signed import item {index:02d}"
        for index in range(len(signed_workspace_listing_ids))
    ]

    page.set_viewport_size({"width": 1280, "height": 900})
    with page.expect_response(lambda response: (
        response.request.method == "GET"
        and urlsplit(response.url).path == "/api/my-products"
        and parse_qs(urlsplit(response.url).query).get("import_batch") == [signed_import_batch]
    )) as initial_feed_info:
        page.goto(base + source_href)
    initial_feed = initial_feed_info.value
    assert initial_feed.status == 200
    assert "no-store" in initial_feed.headers.get("cache-control", "")
    initial_feed_doc = initial_feed.json()
    assert initial_feed_doc["success"] is True
    assert initial_feed_doc["import_batch_count"] == 22
    assert [item["id"] for item in initial_feed_doc["items"]] == signed_workspace_source_ids
    assert [item["title"] for item in initial_feed_doc["items"]] == source_titles
    workspace = page.locator("#my-products-app:not([v-cloak])")
    workspace.wait_for()
    rows = workspace.locator("article.mp-row")
    assert rows.count() == 22
    for title in source_titles[:signed_workspace_selected_count]:
        workspace.get_by_role("checkbox", name="Выбрать " + title, exact=True).check()
    unselected_title = source_titles[signed_workspace_selected_count]
    unselected = workspace.get_by_role(
        "checkbox", name="Выбрать " + unselected_title, exact=True,
    )
    assert not unselected.is_checked()
    selected_ids = workspace.locator(
        "article.mp-row input[type=checkbox]:checked"
    ).evaluate_all("nodes => nodes.map(node => Number(node.value))")
    assert selected_ids == signed_workspace_source_ids[:signed_workspace_selected_count]

    REPORT["stage"] = "signed_import_batch_existing_update_handoff"
    workspace.get_by_role("button", name="Что сделать", exact=True).click()
    action = workspace.get_by_role(
        "menuitem", name=re.compile(r"^Изменить карточки Ozon(?:$|\s)"),
    )
    assert action.count() == 1 and action.is_enabled()
    action.click()
    page.wait_for_url(lambda value: urlsplit(value).path == "/marketplaces/listings/")
    shell = page.locator("#marketplace-catalog-app:not([v-cloak])")
    shell.wait_for()
    preview_dialog = page.locator("dialog.mcat-update-dialog[open]")
    preview_dialog.wait_for()
    preview_confirm = preview_dialog.locator("[data-listing-update-confirm]")
    preview_confirm.wait_for()
    assert state["handoff"] == 1 and state["preview"] == 1
    assert state["return_to"].startswith(source_href + "#workspace-return=")
    assert len(state["listing_ids"]) == 21 and len(state["source_ids"]) == 21
    assert preview_confirm.is_disabled()
    preview_dialog.get_by_role("checkbox").check()
    confirm = preview_dialog.locator("[data-listing-update-confirm]")
    assert confirm.is_enabled()
    confirm.click()
    editor_link = preview_dialog.locator("[data-listing-update-editor]")
    editor_link.wait_for()
    assert state["apply"] == 1
    assert [row["status"] for row in state["report"]["rows"]] == [
        "created"
    ] * 20 + ["existing"]
    assert state["report"]["rows"][-1]["draft_id"] == signed_workspace_existing_draft_id
    draft_ids = [row["draft_id"] for row in state["report"]["rows"]]
    assert len(draft_ids) == 21
    assert draft_ids[-1] == signed_workspace_existing_draft_id
    after_prepare = batch_snapshots()
    assert after_prepare["drafts"].keys() - before["drafts"].keys() == set(draft_ids[:20])
    assert after_prepare["drafts"][signed_workspace_existing_draft_id] == before["drafts"][signed_workspace_existing_draft_id]
    assert {key: after_prepare[key] for key in before if key != "drafts"} == {
        key: before[key] for key in before if key != "drafts"
    }
    editor_url = urlsplit(editor_link.get_attribute("href"))
    editor_query = parse_qs(editor_url.query)
    compact_selection = encode_compact_draft_ids(draft_ids)
    assert editor_url.path == "/marketplaces/ozon/uploads/selection/repair"
    assert editor_query.get("account_id") == [str(fixture["account_id"])]
    assert editor_query.get("workspace_selection") == [compact_selection]
    assert not editor_query.get("draft_ids")
    assert editor_query.get("workspace_return") == ["1"]
    assert decode_workspace_return(editor_url.fragment) == state["return_to"]
    editor_link.click()
    repair_editor = page.locator(
        "#ozon-bulk-repair[data-editor-mode=\"draft_selection\"]:not([v-cloak])"
    )
    repair_editor.wait_for()
    editor_boot = page.evaluate(
        "() => JSON.parse(document.querySelector('#orb-bootstrap').textContent)"
    )
    selection_editor = editor_boot["editor"]
    assert selection_editor["mode"] == "draft_selection"
    assert selection_editor["review_draft_ids"] == draft_ids
    scope_key = selection_editor["scope_key"]
    assert re.fullmatch(r"[0-9a-f]{64}", scope_key)
    target_draft_id = draft_ids[0]
    target_source_id = state["source_ids"][0]
    target_description = (
        "R57 synthetic local edit for signed import review return."
    )
    state["compact_selection"] = compact_selection
    state["scope_key"] = scope_key
    state["local_save_target_id"] = target_draft_id
    state["local_save_description"] = target_description
    before_local_save = batch_snapshots()
    draft_details_before_save = draft_business_snapshots()
    expected_target = draft_details_before_save[target_draft_id]
    state["local_save_expected"] = {
        "draft_id": target_draft_id,
        "source_id": target_source_id,
        "version": expected_target["version"],
        "description": target_description,
        "compact_selection": compact_selection,
        "scope_key": scope_key,
    }

    selected_on_pages = []
    for page_number in (1, 2):
        for card in repair_editor.locator(".orb-row").all():
            row_id = int(card.get_attribute("data-draft"))
            checkbox = card.locator(".orb-select input[type=checkbox]")
            if row_id == target_draft_id:
                if not checkbox.is_checked():
                    checkbox.check()
            elif checkbox.is_checked():
                checkbox.uncheck()
        selected_on_pages.append(repair_editor.locator(
            ".orb-row input[type=checkbox]:checked"
        ).evaluate_all("nodes => nodes.map(node => Number(node.closest('.orb-row').dataset.draft))"))
        if page_number == 1:
            repair_editor.locator(".orb-pagination").get_by_role(
                "button", name="Далее", exact=True,
            ).click()
            page.wait_for_function(
                "document.querySelector('.orb-pagination span')?.textContent.trim() === '2 из 2'"
            )
    assert selected_on_pages == [[target_draft_id], []]
    repair_editor.locator(".orb-pagination").get_by_role(
        "button", name="Назад", exact=True,
    ).click()
    page.wait_for_function(
        "document.querySelector('.orb-pagination span')?.textContent.trim() === '1 из 2'"
    )
    target_row = repair_editor.locator(f'.orb-row[data-draft="{target_draft_id}"]')
    target_row.wait_for()
    assert target_row.locator(".orb-select input[type=checkbox]").is_checked()
    target_row.get_by_role("button", name="Изменить", exact=True).click()
    target_row.get_by_label("Описание", exact=True).fill(target_description)
    assert batch_snapshots() == before_local_save
    assert draft_business_snapshots() == draft_details_before_save

    REPORT["stage"] = "signed_import_local_editor_save"
    repair_editor.locator("footer [data-draft-selection-save]").click()
    save_dialog = page.get_by_role("dialog")
    save_dialog.wait_for()
    save_button = save_dialog.get_by_role(
        "button", name="Сохранить локально", exact=True,
    )
    with page.expect_response(lambda response: (
        response.request.method == "POST"
        and urlsplit(response.url).path == selection_apply_path
    )) as local_save_info:
        save_button.click()
    local_save_response = local_save_info.value
    assert local_save_response.status == 200
    assert "no-store" in local_save_response.headers.get("cache-control", "")
    local_save_result = local_save_response.json()
    assert local_save_result["success"] is True
    assert local_save_result["mode"] == "draft_selection"
    assert local_save_result["scope_key"] == scope_key
    local_save_report = local_save_result["report"]
    assert local_save_report["total"] == local_save_report["updated"] == 1
    assert len(local_save_report["rows"]) == 1
    assert local_save_report["rows"][0]["draft_id"] == target_draft_id
    assert local_save_report["rows"][0]["version"] == expected_target["version"] + 1
    assert local_save_report["rows"][0]["status"] in {"ready_to_retry", "needs_input"}
    receipt = repair_editor.get_by_role("region", name="Результат сохранения")
    receipt.wait_for()
    review_link = receipt.locator("a[data-draft-selection-review]")
    review_link.wait_for()
    assert state["local_save_posts"] == 1
    assert state["local_save_post"]["draft_id"] == target_draft_id
    assert state["local_save_post"]["csrf"] is True
    assert state["local_save_post"]["compact_scope_exact"] is True
    assert REPORT["local_editor_save_posts"] == 1

    after_local_save = batch_snapshots()
    draft_details_after_save = draft_business_snapshots()
    assert set(draft_details_after_save) == set(draft_details_before_save)
    assert all(
        draft_details_after_save[draft_id] == draft_details_before_save[draft_id]
        for draft_id in draft_details_before_save if draft_id != target_draft_id
    )
    saved_target = draft_details_after_save[target_draft_id]
    assert saved_target["version"] == expected_target["version"] + 1
    assert saved_target["imported_product_id"] == expected_target["imported_product_id"]
    assert saved_target["published_listing_id"] == expected_target["published_listing_id"]
    assert {
        key: value for key, value in saved_target.items()
        if key not in {"version", "content_json"}
    } == {
        key: value for key, value in expected_target.items()
        if key not in {"version", "content_json"}
    }
    before_content = json.loads(expected_target["content_json"])
    after_content = json.loads(saved_target["content_json"])
    assert after_content["description"] == target_description
    assert {key: value for key, value in after_content.items() if key != "description"} == {
        key: value for key, value in before_content.items() if key != "description"
    }
    assert after_local_save["links"] == before_local_save["links"]
    assert after_local_save["link_events"] == before_local_save["link_events"]
    assert after_local_save["operations"] == before_local_save["operations"]
    assert after_local_save["jobs"] == before_local_save["jobs"]
    assert set(after_local_save["drafts"]) == set(before_local_save["drafts"])
    for draft_id, previous in before_local_save["drafts"].items():
        current = after_local_save["drafts"][draft_id]
        if draft_id == target_draft_id:
            assert current[0] == previous[0] + 1
            assert current[1:] == previous[1:]
        else:
            assert current == previous
    assert REPORT["provider_attempts"] == provider_before
    assert REPORT["publication_attempts_blocked"] == publication_before

    repair_review_href = urlsplit(review_link.get_attribute("href"))
    repair_review_query = parse_qs(repair_review_href.query)
    assert repair_review_href.path == "/marketplaces/ozon/uploads/review"
    assert repair_review_query.get("account_id") == [str(fixture["account_id"])]
    assert repair_review_query.get("workspace_selection") == [compact_selection]
    assert not repair_review_query.get("draft_ids")
    assert decode_workspace_return(repair_review_href.fragment) == state["return_to"]

    REPORT["stage"] = "signed_import_review_page_one"
    with page.expect_response(lambda response: (
        response.request.method == "GET"
        and response.request.resource_type == "document"
        and urlsplit(response.url).path == "/marketplaces/ozon/uploads/review"
    )) as review_page_one_info:
        review_link.click()
    review_page_one_response = review_page_one_info.value
    assert review_page_one_response.status == 200
    review_root = page.locator("#ozon-upload-review:not([v-cloak])")
    review_root.wait_for()
    review_one = review_bootstrap(page)
    assert review_one["account_id"] == fixture["account_id"]
    assert review_one["draft_ids"] == draft_ids
    assert review_one["workspace_selection"] == compact_selection
    assert decode_compact_draft_ids(review_one["workspace_selection"]) == draft_ids
    assert review_one["workspace_owner"] == f"{signed_workspace_user_id}:{fixture['seller_id']}"
    assert review_one["pagination"] == {
        "page": 1, "per_page": 20, "pages": 2, "total": 21,
    }
    assert [row["draft_id"] for row in review_one["items"]] == draft_ids[:20]
    assert all(any(
        issue.get("code") == "publication_disabled" for issue in row["errors"]
    ) for row in review_one["items"])
    assert all(not any(
        issue.get("code") == "physical_fact_required"
        and issue.get("field") == "dimensions.weight"
        for issue in row["errors"]
    ) for row in review_one["items"])
    assert review_one["publication_enabled"] is False
    review_return_link = page.locator("a[data-review-workspace-return]")
    assert review_return_link.count() == 1
    page.wait_for_function(
        "expected => document.querySelector('a[data-review-workspace-return]')?.getAttribute('href') === expected",
        arg=state["return_to"],
    )
    return_label = review_return_link.locator("[data-review-workspace-return-label]")
    assert return_label.inner_text().strip() == "К выбранным товарам"

    review_api_one = read_review_api(
        page, page_number=1, compact_selection=compact_selection,
    )
    assert review_api_one["status"] == 200
    assert "no-store" in review_api_one["cacheControl"]
    api_one = review_api_one["body"]["review"]
    assert api_one["draft_ids"] == draft_ids
    assert api_one["workspace_selection"] == compact_selection
    assert api_one["pagination"] == review_one["pagination"]
    assert [row["draft_id"] for row in api_one["items"]] == draft_ids[:20]
    assert "workspace_owner" not in api_one

    page_one_parts = urlsplit(page.url)
    assert decode_workspace_return(page_one_parts.fragment) == state["return_to"]
    next_page = review_root.locator(".our-pagination a").filter(
        has_text="Следующие 20",
    )
    next_href = urlsplit(next_page.get_attribute("href"))
    next_query = parse_qs(next_href.query)
    assert next_href.path == "/marketplaces/ozon/uploads/review"
    assert next_query == {
        "account_id": [str(fixture["account_id"])],
        "workspace_selection": [compact_selection],
        "page": ["2"],
    }
    assert decode_workspace_return(next_href.fragment) == state["return_to"]
    page.set_viewport_size({"width": 390, "height": 850})
    page.screenshot(path=OUT / "workspace-review-page-1-390.png", animations="disabled")

    REPORT["stage"] = "signed_import_review_page_two"
    with page.expect_response(lambda response: (
        response.request.method == "GET"
        and response.request.resource_type == "document"
        and urlsplit(response.url).path == "/marketplaces/ozon/uploads/review"
        and parse_qs(urlsplit(response.url).query).get("page") == ["2"]
    )) as review_page_two_info:
        next_page.click()
    review_page_two_response = review_page_two_info.value
    assert review_page_two_response.status == 200
    review_two = review_bootstrap(page)
    assert review_two["account_id"] == fixture["account_id"]
    assert review_two["draft_ids"] == draft_ids
    assert review_two["workspace_selection"] == compact_selection
    assert review_two["workspace_owner"] == f"{signed_workspace_user_id}:{fixture['seller_id']}"
    assert review_two["pagination"] == {
        "page": 2, "per_page": 20, "pages": 2, "total": 21,
    }
    assert [row["draft_id"] for row in review_two["items"]] == draft_ids[20:]
    assert decode_workspace_return(urlsplit(page.url).fragment) == state["return_to"]
    review_api_two = read_review_api(
        page, page_number=2, compact_selection=compact_selection,
    )
    assert review_api_two["status"] == 200
    api_two = review_api_two["body"]["review"]
    assert api_two["draft_ids"] == draft_ids
    assert api_two["workspace_selection"] == compact_selection
    assert api_two["pagination"] == review_two["pagination"]
    assert [row["draft_id"] for row in api_two["items"]] == draft_ids[20:]
    assert "workspace_owner" not in api_two

    issue_row = review_two["items"][0]
    assert issue_row["draft_id"] == draft_ids[20]
    assert issue_row["selectable"] is False
    weight_issues = [
        issue for issue in issue_row["errors"]
        if issue.get("code") == "physical_fact_required"
        and issue.get("field") == "dimensions.weight"
    ]
    assert len(weight_issues) == 1
    issue_link = None
    issue_href = None
    for candidate in review_root.locator("a[data-review-issue-link]").all():
        candidate_href = candidate.get_attribute("href")
        candidate_query = parse_qs(urlsplit(candidate_href).query)
        if candidate_query.get("focus_field") == ["dimensions.weight"]:
            issue_link, issue_href = candidate, candidate_href
            break
    assert issue_link is not None and issue_href
    issue_parts = urlsplit(issue_href)
    issue_query = parse_qs(issue_parts.query)
    assert issue_parts.path == f"/marketplaces/drafts/{issue_row['draft_id']}"
    assert issue_query.get("focus_code") == ["physical_fact_required"]
    assert issue_query.get("focus_field") == ["dimensions.weight"]
    review_return_token = issue_query.get("review_return", [None])[0]
    assert review_return_token and len(review_return_token) <= 8192
    with app.app_context():
        loaded_return = OzonReviewNavigation._load_payload(
            review_return_token,
            user_id=signed_workspace_user_id,
            seller_id=fixture["seller_id"],
        )
    assert loaded_return is not None
    return_payload, return_ids = loaded_return
    assert return_payload["v"] == 2
    assert return_payload["account_id"] == fixture["account_id"]
    assert return_payload["page"] == 2
    assert return_payload["workspace_selection"] == compact_selection
    assert decode_compact_draft_ids(return_payload["workspace_selection"]) == draft_ids
    assert return_ids == draft_ids

    REPORT["stage"] = "signed_import_issue_detail_read_return"
    before_issue_navigation = batch_snapshots()
    with page.expect_response(lambda response: (
        response.request.method == "GET"
        and response.request.resource_type == "document"
        and urlsplit(response.url).path == f"/marketplaces/drafts/{issue_row['draft_id']}"
    )) as issue_detail_info:
        issue_link.click()
    assert issue_detail_info.value.status == 200
    editor = page.locator("#ozon-draft-editor:not([v-cloak])")
    editor.wait_for()
    editor_boot = page.evaluate(
        "() => JSON.parse(document.querySelector('#ode-bootstrap').textContent)"
    )
    assert editor_boot["draftId"] == issue_row["draft_id"]
    assert editor_boot["reviewNavigation"]["focus"] == {
        "code": "physical_fact_required", "field": "dimensions.weight",
    }
    detail_return_href = editor_boot["reviewNavigation"]["url"]
    detail_return = urlsplit(detail_return_href)
    detail_return_query = parse_qs(detail_return.query)
    assert detail_return.path == "/marketplaces/ozon/uploads/review"
    assert detail_return_query == {
        "account_id": [str(fixture["account_id"])],
        "workspace_selection": [compact_selection],
        "page": ["2"],
    }
    detail_return_link = page.locator("a[data-review-return]")
    detail_return_link.wait_for()
    assert detail_return_link.get_attribute("href") == detail_return_href
    page.screenshot(path=OUT / "workspace-review-issue-detail-390.png", animations="disabled")
    assert batch_snapshots() == before_issue_navigation

    with page.expect_response(lambda response: (
        response.request.method == "GET"
        and response.request.resource_type == "document"
        and urlsplit(response.url).path == "/marketplaces/ozon/uploads/review"
        and parse_qs(urlsplit(response.url).query).get("page") == ["2"]
    )) as returned_review_info:
        detail_return_link.click()
    assert returned_review_info.value.status == 200
    returned_review = review_bootstrap(page)
    assert returned_review["account_id"] == fixture["account_id"]
    assert returned_review["draft_ids"] == draft_ids
    assert returned_review["workspace_selection"] == compact_selection
    assert returned_review["workspace_owner"] == f"{signed_workspace_user_id}:{fixture['seller_id']}"
    assert returned_review["pagination"]["page"] == 2
    assert [row["draft_id"] for row in returned_review["items"]] == draft_ids[20:]
    page.wait_for_function(
        "expected => document.querySelector('a[data-review-workspace-return]')?.getAttribute('href') === expected",
        arg=state["return_to"],
    )
    review_return_link = page.locator("a[data-review-workspace-return]")
    return_label = review_return_link.locator("[data-review-workspace-return-label]")
    assert return_label.inner_text().strip() == "К выбранным товарам"
    previous_page = page.locator(".our-pagination a").filter(has_text="Предыдущие 20")
    previous_href = urlsplit(previous_page.get_attribute("href"))
    assert parse_qs(previous_href.query) == {
        "account_id": [str(fixture["account_id"])],
        "workspace_selection": [compact_selection],
        "page": ["1"],
    }
    assert decode_workspace_return(previous_href.fragment) == state["return_to"]
    assert batch_snapshots() == after_local_save
    assert draft_business_snapshots() == draft_details_after_save
    page.screenshot(path=OUT / "workspace-review-return-page-2-390.png", animations="disabled")

    REPORT["stage"] = "signed_import_batch_selection_restore"
    page.set_viewport_size({"width": 320, "height": 850})
    with page.expect_response(lambda response: (
        response.request.method == "GET"
        and response.request.resource_type == "document"
        and urlsplit(response.url).path == "/my-products/beta"
        and parse_qs(urlsplit(response.url).query).get("import_batch") == [signed_import_batch]
    )) as workspace_return_info:
        with page.expect_response(lambda response: (
            response.request.method == "GET"
            and urlsplit(response.url).path == "/api/my-products"
            and parse_qs(urlsplit(response.url).query).get("import_batch") == [signed_import_batch]
        )) as restored_feed_info:
            review_return_link.click()
    assert workspace_return_info.value.status == 200
    restored_feed = restored_feed_info.value
    assert restored_feed.status == 200
    assert "no-store" in restored_feed.headers.get("cache-control", "")
    restored_feed_doc = restored_feed.json()
    assert restored_feed_doc["success"] is True
    assert restored_feed_doc["import_batch_count"] == 22
    assert [item["id"] for item in restored_feed_doc["items"]] == signed_workspace_source_ids
    rows = workspace.locator("article.mp-row")
    page.wait_for_function("""expected => {
        const rows = Array.from(document.querySelectorAll('#my-products-app article.mp-row'));
        if (rows.length !== expected.count) return false;
        const ids = rows.map(row => Number(row.querySelector('.mp-check input')?.value));
        const selected = rows.map(row => !!row.querySelector('.mp-check input')?.checked);
        return ids.every((id, index) => id === expected.ids[index]) &&
            selected.every((value, index) => value === (index < expected.selected));
    }""", arg={
        "ids": signed_workspace_source_ids,
        "count": 22,
        "selected": signed_workspace_selected_count,
    })
    assert rows.count() == 22
    assert [
        int(node.get_attribute("value"))
        for node in rows.locator("input[type=checkbox]:checked").all()
    ] == signed_workspace_source_ids[:signed_workspace_selected_count]
    assert not workspace.get_by_role(
        "checkbox", name="Выбрать " + unselected_title, exact=True,
    ).is_checked()
    restored_url = urlsplit(page.url)
    assert restored_url.path == "/my-products/beta"
    assert parse_qs(restored_url.query) == {"import_batch": [signed_import_batch]}
    assert re.fullmatch(r"workspace-return=[a-f0-9]{32}", restored_url.fragment)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert batch_snapshots() == after_local_save
    assert draft_business_snapshots() == draft_details_after_save
    assert state["handoff"] == 1 and state["preview"] == 1 and state["apply"] == 1
    assert state["catalog_reads"] == []
    assert state["feed_reads"] and all(
        row["status"] == 200 and row["no_store"]
        and row["query_keys"] == ["import_batch"]
        for row in state["feed_reads"]
    )
    assert REPORT["provider_attempts"] == provider_before
    assert REPORT["publication_attempts_blocked"] == publication_before
    assert len(REPORT["unexpected"]) == unexpected_before
    page.screenshot(path=OUT / "workspace-signed-import-restored-320.png", animations="disabled")

    REPORT["signed_import_review_return"] = {
        "scope": "synthetic_signed_supplier_import_batch_existing_update",
        "origin_batch_count": 22,
        "origin_selected_count": signed_workspace_selected_count,
        "origin_unselected_count": 1,
        "origin_token_bytes": len(signed_import_batch.encode("utf-8")),
        "selected_source_count": len(state["source_ids"]),
        "selected_listing_count": len(state["listing_ids"]),
        "draft_count": len(draft_ids),
        "drafts_created": 20,
        "preexisting_draft_id": signed_workspace_existing_draft_id,
        "preview_statuses": ["ready"] * 20 + ["existing"],
        "apply_statuses": ["created"] * 20 + ["existing"],
        "draft_ids_in_compact_selection": decode_compact_draft_ids(compact_selection),
        "review": {
            "account_id": fixture["account_id"],
            "workspace_owner_html": f"{signed_workspace_user_id}:{fixture['seller_id']}",
            "workspace_owner_api_absent": True,
            "page_count": 2,
            "page_one_rows": len(review_one["items"]),
            "page_one_no_weight_issue": True,
            "page_one_selection_blocked_by_publication_switch": True,
            "page_two_rows": len(review_two["items"]),
            "compact_scope_canonical_in_html_and_api": True,
            "page_two_issue_draft_id": issue_row["draft_id"],
            "page_two_issue": {
                "code": "physical_fact_required",
                "field": "dimensions.weight",
            },
            "signed_issue_return_version": return_payload["v"],
            "signed_issue_return_page": return_payload["page"],
            "returned_review_page": returned_review["pagination"]["page"],
            "safe_fragment_survived_pagination_and_issue_return": True,
            "primary_return_label": return_label.inner_text().strip(),
        },
        "workspace_return": {
            "same_tab": True,
            "signed_batch_query_preserved": True,
            "feed_rows_preserved_in_order": True,
            "selected_source_ids_restored": signed_workspace_source_ids[:signed_workspace_selected_count],
            "unselected_source_id_stayed_unselected": signed_workspace_source_ids[-1],
            "local_editor_save_posts": state["local_save_posts"],
            "local_editor_save": {
                "draft_id": target_draft_id,
                "version_delta": 1,
                "description_only_changed": True,
                "compact_scope_exact": True,
            },
            "business_state_unchanged_after_local_save": True,
            "provider_attempts": REPORT["provider_attempts"],
            "publication_attempts_blocked": REPORT["publication_attempts_blocked"],
        },
        "feed_reads": state["feed_reads"],
        "review_requests": state["review_api_reads"],
        "screenshots": [
            "workspace-review-page-1-390.png",
            "workspace-review-issue-detail-390.png",
            "workspace-review-return-page-2-390.png",
            "workspace-signed-import-restored-320.png",
        ],
    }
    REPORT["checks"].append(
        "signed_import_batch_existing_update_compact_review_page2_issue_return_and_selection_restore"
    )


def test_browser():
    global ACTIVE_PAGE
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium"),
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900}, service_workers="block")
        ACTIVE_PAGE = page
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
        exercise_signed_import_review_return(page)
        browser.close()


try:
    test_browser()
    with app.app_context():
        assert MarketplaceProductDraft.query.count() == initial_draft_count + 5 + 20
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
except Exception as exc:
    REPORT["status"] = "failed"
    frames = traceback.extract_tb(exc.__traceback__)
    failure_frame = frames[-1] if frames else None
    REPORT["failure"] = {
        "exception_type": type(exc).__name__,
        "message": safe_failure_message(exc),
        "stage": REPORT.get("stage"),
        "location": safe_location(ACTIVE_PAGE),
        "frame": (
            {
                "file": Path(failure_frame.filename).name,
                "line": failure_frame.lineno,
                "function": failure_frame.name,
            }
            if failure_frame else None
        ),
    }
    if ACTIVE_PAGE is not None:
        try:
            failure_screenshot = OUT / "failure-browser.png"
            ACTIVE_PAGE.screenshot(path=failure_screenshot, animations="disabled")
            REPORT["failure"]["screenshot"] = failure_screenshot.name
        except Exception:
            pass
    raise
finally:
    server.shutdown()
    thread.join(timeout=5)
    TEMP.cleanup()
    (OUT / "ozon-existing-update-browser.json").write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8",
    )
