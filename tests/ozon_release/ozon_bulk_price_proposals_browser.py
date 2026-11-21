"""Focused offline Chromium acceptance for multi-item Ozon price proposals."""

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.fernet import Fernet
import requests
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

OUT = Path(os.environ.get("OZON_BROWSER_ARTIFACTS", "/artifacts"))
OUT.mkdir(parents=True, exist_ok=True)
CHROMIUM_BIN = os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium")
ASSETS = Path(__file__).with_name("assets")
report = {
    "status": "running",
    "checks": [],
    "postcounts": {},
    "provider_attempts": 0,
    "provider_write_callbacks": 0,
    "external": [],
    "unexpected_http": [],
    "js_errors": [],
    "preview_payloads": [],
    "confirm_payloads": [],
    "preview_results": [],
    "synthetic_price_reads": [],
    "proposal_receipt": None,
    "exact_review_get": None,
    "replay_receipt": None,
    "unknown_confirm_response_losses": 0,
    "confirm_replay": None,
    "scope": "synthetic_ozon_bulk_price_proposals_browser",
}
temporary = tempfile.TemporaryDirectory(prefix="ozon-bulk-price-browser-")
os.environ.update(
    DATABASE_URL="sqlite:///" + str(Path(temporary.name) / "app.sqlite"),
    SKIP_SCHEDULER="1",
    IMAGE_LAB_INLINE_WORKER="0",
    ENCRYPTION_KEY=Fernet.generate_key().decode(),
    SECRET_KEY="synthetic-ozon-bulk-price-browser-session-key",
)


def forbid_provider(*_args, **_kwargs):
    report["provider_attempts"] += 1
    raise AssertionError("Provider I/O is forbidden in this browser fixture")


requests.sessions.Session.request = forbid_provider
socket.create_connection = forbid_provider

from seller_platform import app
from tests.ozon_release.seed import PHOTO, PASSWORD, USERNAME, seed

app.config.update(
    TESTING=True,
    WTF_CSRF_ENABLED=True,
    SESSION_COOKIE_SECURE=False,
    MARKETPLACE_OZON_ENABLED=True,
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=True,
)
fixture = seed(app)
account_id = fixture["account_id"]
listing_ids = fixture["listing_ids"]

from models import ImportedProduct, MarketplaceCommercialProposal, MarketplaceListing, MarketplaceOperation, db
from services.marketplace_adapters.ozon import OzonAdapter
from services.marketplace_commercial import MarketplaceCommercialService


synthetic_prices = {
    listing_ids[0]: ("1000", "1500", "500"),
    listing_ids[1]: ("1200", "1500", "500"),
    listing_ids[2]: ("1400", "1800", "1000"),
    listing_ids[4]: ("1100", "1500", "500"),
}
with app.app_context():
    for listing_id, (price, old_price, min_price) in synthetic_prices.items():
        listing = db.session.get(MarketplaceListing, listing_id)
        values = json.loads(listing.price_summary_json or "{}")
        values.update({
            "available": True,
            "currency": "RUB",
            "values": {
                "price": price,
                "old_price": old_price,
                "min_price": min_price,
            },
        })
        listing.price_summary_json = json.dumps(values, ensure_ascii=False)
    linked_listing = db.session.get(MarketplaceListing, listing_ids[0])
    linked_listing.imported_product_id = fixture["source_id"]
    linked_listing.link_status = "linked"
    linked_listing.link_source = "synthetic_browser_fixture"
    linked_listing.linked_at = datetime.utcnow()
    db.session.commit()


def synthetic_price_states(cls, *, listings, adapter, credentials):
    del cls, credentials
    ids = [listing.id for listing in listings]
    if adapter.code != "ozon" or not ids or len(ids) > 100:
        raise AssertionError("Bulk price route lost its exact Ozon read scope")
    report["synthetic_price_reads"].append(ids)
    result = {}
    for listing in listings:
        raw = json.loads(listing.price_summary_json or "{}")
        values = raw.get("values") if isinstance(raw, dict) else None
        if not isinstance(values, dict):
            raise AssertionError("Synthetic baseline is unavailable")
        result[listing.id] = {
            "kind": "price",
            "offer_id": listing.offer_id,
            "product_id": listing.external_product_id,
            "price": str(values["price"]),
            "old_price": str(values["old_price"]),
            "min_price": str(values.get("min_price", "0")),
            "currency_code": "RUB",
            "auto_action_enabled": False,
            "auto_add_to_ozon_actions_list_enabled": False,
        }
    return result


MarketplaceCommercialService._read_price_states = classmethod(synthetic_price_states)


def forbid_price_write(_self, *_args, **_kwargs):
    report["provider_write_callbacks"] += 1
    raise AssertionError("Ozon price write callback is forbidden in this fixture")


OzonAdapter.update_prices = forbid_price_write

busy_delay = {"saved_return_confirm": False}


def delay_saved_return_confirmation():
    from flask import request

    if (busy_delay["saved_return_confirm"]
            and request.method == "POST"
            and request.path == "/marketplaces/commercial/price-proposals/bulk-confirm"):
        busy_delay["saved_return_confirm"] = False
        time.sleep(0.35)


app.before_request_funcs.setdefault(None, []).append(delay_saved_return_confirmation)

logging.getLogger("werkzeug").setLevel(logging.ERROR)
server = make_server("127.0.0.1", 0, app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = "http://127.0.0.1:" + str(server.server_port)
svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="120"><rect width="100" height="120" fill="#ded8cd"/></svg>'
manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
for asset in manifest.values():
    assert hashlib.sha256((ASSETS / asset["file"]).read_bytes()).hexdigest() == asset["sha256"]
mock_state = {"preview_number": 0, "confirm_attempts": 0}
expected_http = set()


def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname == "127.0.0.1" and url.port == server.server_port:
        path = url.path
        confirm_attempt = None
        if request.method == "POST":
            if path == "/login":
                pass
            elif path == "/marketplaces/commercial/price-proposals/bulk-preview":
                report["postcounts"][path] = report["postcounts"].get(path, 0) + 1
                if not request.headers.get("x-csrftoken"):
                    raise AssertionError("Bulk preview omitted the CSRF header")
                body = json.loads(request.post_data or "{}")
                if set(body) != {
                    "account_id", "listing_ids", "mode", "value",
                    "allow_price_decrease", "allow_large_change", "guardrail_note",
                }:
                    raise AssertionError("Bulk preview sent unexpected fields")
                if body["account_id"] != account_id or not isinstance(body["listing_ids"], list):
                    raise AssertionError("Bulk preview account or exact listing set changed")
                report["preview_payloads"].append(body)
                mock_state["preview_number"] += 1
                if mock_state["preview_number"] == 1 and body["listing_ids"] != listing_ids[:3]:
                    raise AssertionError("Primary preview changed the exact three-listing selection")
                if mock_state["preview_number"] == 2 and body["listing_ids"] != [listing_ids[3]]:
                    raise AssertionError("Percent preview changed its exact one-listing selection")
            elif path == "/marketplaces/commercial/price-proposals/bulk-confirm":
                report["postcounts"][path] = report["postcounts"].get(path, 0) + 1
                mock_state["confirm_attempts"] += 1
                confirm_attempt = mock_state["confirm_attempts"]
                if not request.headers.get("x-csrftoken"):
                    raise AssertionError("Bulk confirmation omitted the CSRF header")
                body = json.loads(request.post_data or "{}")
                if set(body) != {"preview_token", "confirm_save"} or body["confirm_save"] is not True:
                    raise AssertionError("Bulk confirmation did not freeze one preview token")
                tokens = mock_state.get("preview_tokens", [])
                expected_token = tokens[0] if confirm_attempt == 1 else (
                    tokens[2] if confirm_attempt in {2, 3} and len(tokens) >= 3 else
                    tokens[3] if confirm_attempt == 4 and len(tokens) >= 4 else
                    tokens[4] if confirm_attempt == 5 and len(tokens) >= 5 else None
                )
                if not expected_token or body["preview_token"] != expected_token:
                    raise AssertionError("Bulk confirmation did not use the exact reviewed preview")
                report["confirm_payloads"].append({
                    "attempt": confirm_attempt,
                    "confirm_save": body["confirm_save"],
                    "token_matches_reviewed_preview": True,
                })
                if confirm_attempt > 5:
                    raise AssertionError("Unexpected extra bulk-confirm request")
            elif path == "/marketplaces/commercial/batch-approve" or path.endswith("/approve"):
                report["postcounts"][path] = report["postcounts"].get(path, 0) + 1
                raise AssertionError("Provider approval is outside this local-save journey")
            else:
                raise AssertionError(("Unexpected browser mutation", path))
        response = route.fetch(max_redirects=0)
        if request.method == "POST" and path == "/marketplaces/commercial/price-proposals/bulk-preview":
            if response.status != 200:
                raise AssertionError(("Actual Flask preview failed", response.status))
            result = response.json()
            mock_state.setdefault("preview_tokens", []).append(result["preview_token"])
            summary = {
                "selected_count": result["selected_count"],
                "eligible_count": result["eligible_count"],
                "noop_count": result["noop_count"],
                "blocked_count": result["blocked_count"],
                "statuses": [item["status"] for item in result["items"]],
                "reason_codes": [item["reason_code"] for item in result["items"]],
            }
            report["preview_results"].append(summary)
            if mock_state["preview_number"] == 1:
                assert summary == {
                    "selected_count": 3,
                    "eligible_count": 1,
                    "noop_count": 1,
                    "blocked_count": 1,
                    "statuses": ["eligible", "noop", "blocked"],
                    "reason_codes": [None, "price_unchanged", "price_decrease_requires_review"],
                }, summary
            elif mock_state["preview_number"] == 2:
                assert summary["selected_count"] == 1
                assert summary["eligible_count"] == 1
                assert summary["statuses"] == ["eligible"]
            elif mock_state["preview_number"] == 3:
                assert summary["selected_count"] == 1
                assert summary["eligible_count"] == 1
                assert summary["statuses"] == ["eligible"]
                assert report["preview_payloads"][-1]["listing_ids"] == [listing_ids[4]]
            elif mock_state["preview_number"] == 4:
                assert summary["selected_count"] == 1
                assert summary["eligible_count"] == 1
                assert summary["statuses"] == ["eligible"]
                assert report["preview_payloads"][-1]["listing_ids"] == [listing_ids[5]]
            elif mock_state["preview_number"] == 5:
                assert summary["selected_count"] == 3
                assert summary["eligible_count"] == 2
                assert summary["noop_count"] == 1
                assert summary["blocked_count"] == 0
                assert summary["statuses"] == ["eligible", "noop", "eligible"]
                assert report["preview_payloads"][-1]["listing_ids"] == listing_ids[6:9]
        elif request.method == "POST" and path == "/marketplaces/commercial/price-proposals/bulk-confirm":
            result = response.json()
            proposal_ids = [item["proposal"]["id"] for item in result["items"]]
            listing_result_ids = [item["listing_id"] for item in result["items"]]
            if confirm_attempt == 1:
                if response.status != 201 or (
                    result["selected_count"] != 3
                    or result["created_count"] != 1
                    or result["saved_count"] != 1
                    or result["noop_count"] != 1
                    or result["blocked_count"] != 1
                    or result["replayed"]
                    or len(proposal_ids) != 1
                    or listing_result_ids != [listing_ids[0]]
                ):
                    raise AssertionError("Actual Flask confirmation returned a different exact-set result")
                review_url = result["review_url"]
                parsed = urlsplit(review_url or "")
                query = parse_qs(parsed.query, strict_parsing=True)
                if parsed.path != "/marketplaces/commercial/" or query != {
                    "account_id": [str(account_id)],
                    "proposal_kind": ["price"],
                    "status": ["pending_review"],
                    "proposal_ids": [str(proposal_ids[0])],
                }:
                    raise AssertionError("Actual Flask review URL is not the exact pending proposal")
                proposal_id = proposal_ids[0]
                with app.app_context():
                    proposal = db.session.get(MarketplaceCommercialProposal, proposal_id)
                    if (
                        proposal is None
                        or proposal.seller_id != fixture["seller_id"]
                        or proposal.account_id != account_id
                        or proposal.listing_id != listing_ids[0]
                        or proposal.proposal_kind != "price"
                        or proposal.status != "pending_review"
                        or proposal.operation_id is not None
                    ):
                        raise AssertionError("Persisted proposal is not an immutable pending local receipt")
                    baseline = json.loads(proposal.baseline_state_json)
                    proposed = json.loads(proposal.proposed_state_json)
                    if baseline["price"] != "1000" or proposed["price"] != "1200":
                        raise AssertionError("Persisted proposal does not match reviewed baseline and seller price")
                    if proposal.baseline_fingerprint != MarketplaceCommercialService._fingerprint(baseline):
                        raise AssertionError("Persisted baseline fingerprint is not stable")
                    if proposal.proposed_fingerprint != MarketplaceCommercialService._fingerprint(proposed):
                        raise AssertionError("Persisted proposal fingerprint is not stable")
                    if MarketplaceOperation.query.count() != 0:
                        raise AssertionError("Local proposal unexpectedly created a provider operation")
                    report["proposal_receipt"] = {
                        "proposal_id": proposal.id,
                        "listing_id": proposal.listing_id,
                        "status": proposal.status,
                        "version": proposal.version,
                        "baseline_fingerprint": proposal.baseline_fingerprint,
                        "proposed_fingerprint": proposal.proposed_fingerprint,
                        "baseline_fingerprint_valid": True,
                        "proposed_fingerprint_valid": True,
                        "provider_operation_created": False,
                    }
            elif confirm_attempt == 2:
                if response.status != 201 or (
                    result["selected_count"] != 1
                    or result["created_count"] != 1
                    or result["saved_count"] != 1
                    or result["replayed"]
                    or len(proposal_ids) != 1
                    or listing_result_ids != [listing_ids[4]]
                ):
                    raise AssertionError("First recovery save did not create its exact local proposal")
                proposal_id = proposal_ids[0]
                with app.app_context():
                    proposal = db.session.get(MarketplaceCommercialProposal, proposal_id)
                    if (
                        proposal is None
                        or proposal.seller_id != fixture["seller_id"]
                        or proposal.account_id != account_id
                        or proposal.listing_id != listing_ids[4]
                        or proposal.status != "pending_review"
                        or proposal.version != 1
                        or proposal.operation_id is not None
                    ):
                        raise AssertionError("Lost browser response did not leave one pending local receipt")
                    baseline = json.loads(proposal.baseline_state_json)
                    proposed = json.loads(proposal.proposed_state_json)
                    if baseline["price"] != "1100" or proposed["price"] != "1200":
                        raise AssertionError("Recovery proposal does not match its reviewed baseline")
                    if MarketplaceOperation.query.count() != 0:
                        raise AssertionError("Recovery proposal unexpectedly created a provider operation")
                    if MarketplaceCommercialProposal.query.filter_by(
                        seller_id=fixture["seller_id"],
                        account_id=account_id,
                        proposal_kind="price",
                    ).count() != 2:
                        raise AssertionError("Recovery save created an unexpected proposal count")
                    report["replay_receipt"] = {
                        "proposal_id": proposal.id,
                        "listing_id": proposal.listing_id,
                        "status": proposal.status,
                        "version": proposal.version,
                        "baseline_fingerprint": proposal.baseline_fingerprint,
                        "proposed_fingerprint": proposal.proposed_fingerprint,
                        "provider_operation_created": False,
                    }
                report["unknown_confirm_response_losses"] += 1
                route.fulfill(
                    status=502,
                    content_type="application/json",
                    body=json.dumps({"success": False, "error": "Temporary response unavailable"}),
                )
                return
            elif confirm_attempt == 3:
                replay_id = report["replay_receipt"]["proposal_id"]
                if response.status != 200 or (
                    result["selected_count"] != 1
                    or result["created_count"] != 0
                    or result["saved_count"] != 1
                    or result["replayed_count"] != 1
                    or not result["replayed"]
                    or proposal_ids != [replay_id]
                    or listing_result_ids != [listing_ids[4]]
                ):
                    raise AssertionError("Same-token retry did not replay the original local proposal")
                with app.app_context():
                    proposal = db.session.get(MarketplaceCommercialProposal, replay_id)
                    if (
                        proposal is None
                        or proposal.status != "pending_review"
                        or proposal.version != report["replay_receipt"]["version"]
                        or proposal.baseline_fingerprint != report["replay_receipt"]["baseline_fingerprint"]
                        or proposal.proposed_fingerprint != report["replay_receipt"]["proposed_fingerprint"]
                        or MarketplaceCommercialProposal.query.filter_by(
                            seller_id=fixture["seller_id"],
                            account_id=account_id,
                            proposal_kind="price",
                        ).count() != 2
                        or MarketplaceOperation.query.count() != 0
                    ):
                        raise AssertionError("Replay changed or duplicated the original local proposal")
                report["confirm_replay"] = {
                    "same_preview_token_reused": True,
                    "same_proposal_id": replay_id,
                    "replayed": True,
                    "saved_proposal_count": 1,
                    "provider_operation_created": False,
                }
            elif confirm_attempt == 4:
                if response.status != 201 or (
                    result["selected_count"] != 1
                    or result["created_count"] != 1
                    or result["saved_count"] != 1
                    or result["replayed"]
                    or len(proposal_ids) != 1
                    or listing_result_ids != [listing_ids[5]]
                ):
                    raise AssertionError("Saved return fixture did not create its exact local proposal")
                with app.app_context():
                    proposals = [db.session.get(MarketplaceCommercialProposal, proposal_id)
                                 for proposal_id in proposal_ids]
                    if (
                        any(proposal is None for proposal in proposals)
                        or [proposal.listing_id for proposal in proposals] != [listing_ids[5]]
                        or any(
                            proposal.seller_id != fixture["seller_id"]
                            or proposal.account_id != account_id
                            or proposal.status != "pending_review"
                            or proposal.operation_id is not None
                            for proposal in proposals
                        )
                        or MarketplaceCommercialProposal.query.filter_by(
                            seller_id=fixture["seller_id"],
                            account_id=account_id,
                            proposal_kind="price",
                        ).count() != 3
                        or MarketplaceOperation.query.count() != 0
                    ):
                        raise AssertionError("Saved return proposals were not the exact local pending subset")
                    proposal = proposals[0]
                    baseline = json.loads(proposal.baseline_state_json)
                    proposed = json.loads(proposal.proposed_state_json)
                    if baseline["price"] != "1000" or proposed["price"] != "1200":
                        raise AssertionError("Saved return proposal changed reviewed seller-price values")
                report["saved_return_proposal_created"] = True
                report["saved_return_proposal_ids"] = proposal_ids
            elif confirm_attempt == 5:
                if response.status != 201 or (
                    result["selected_count"] != 3
                    or result["created_count"] != 2
                    or result["saved_count"] != 2
                    or result["noop_count"] != 1
                    or result["blocked_count"] != 0
                    or result["replayed"]
                    or len(proposal_ids) != 2
                    or listing_result_ids != [listing_ids[6], listing_ids[8]]
                ):
                    raise AssertionError("R41 saved return did not persist exactly the eligible subset")
                with app.app_context():
                    proposals = [db.session.get(MarketplaceCommercialProposal, proposal_id)
                                 for proposal_id in proposal_ids]
                    if (
                        any(proposal is None for proposal in proposals)
                        or [proposal.listing_id for proposal in proposals] != [listing_ids[6], listing_ids[8]]
                        or any(
                            proposal.seller_id != fixture["seller_id"]
                            or proposal.account_id != account_id
                            or proposal.status != "pending_review"
                            or proposal.operation_id is not None
                            for proposal in proposals
                        )
                        or MarketplaceCommercialProposal.query.filter_by(
                            seller_id=fixture["seller_id"],
                            account_id=account_id,
                            proposal_kind="price",
                        ).count() != 5
                        or MarketplaceOperation.query.count() != 0
                    ):
                        raise AssertionError("R41 saved proposal subset lost its exact local account/listing binding")
                report["saved_return_proposal_subset_created"] = True
                report["saved_return_subset_proposal_ids"] = proposal_ids
                report["saved_return_subset_listing_ids"] = listing_result_ids
                report["saved_return_subset_noop_listing_id"] = listing_ids[7]
            else:
                raise AssertionError("Unexpected bulk-confirm response path")
        elif request.method == "GET" and path == "/marketplaces/commercial/" and \
                parse_qs(url.query).get("proposal_ids") and \
                "application/json" in request.headers.get("accept", ""):
            if response.status != 200 and (path, response.status) in expected_http:
                route.fulfill(response=response)
                return
            if response.status != 200:
                raise AssertionError(("Actual exact proposal GET failed", response.status))
            result = response.json()
            expected_ids = [int(value) for value in parse_qs(url.query)["proposal_ids"][0].split(",")]
            actual_ids = [item["id"] for item in result["items"]]
            if sorted(actual_ids) != sorted(expected_ids) or not result.get("exact_selection"):
                raise AssertionError("Exact proposal GET returned another set")
            report["exact_review_get"] = {
                "proposal_ids": actual_ids,
                "statuses": [item["status"] for item in result["items"]],
                "exact_selection": result["exact_selection"],
            }
        if response.status >= 400 and (path, response.status) not in expected_http:
            report["unexpected_http"].append({"path": path, "status": response.status})
        route.fulfill(response=response)
        return
    if request.url == PHOTO:
        route.fulfill(body=svg, content_type="image/svg+xml")
        return
    asset = manifest.get(request.url) or manifest.get(request.url.rstrip("/"))
    if asset:
        route.fulfill(body=(ASSETS / asset["file"]).read_bytes(),
                      content_type=asset["content_type"])
        return
    report["external"].append({"host": url.hostname, "path": url.path})
    route.abort()


def passed(name):
    report["checks"].append(name)
    print(json.dumps({"ozon_bulk_price_check": name}), flush=True)


def beta_feed_matches(response, expected):
    parsed = urlsplit(response.url)
    if parsed.path != "/api/my-products" or response.request.method != "GET":
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    return all(query.get(key) == [value] for key, value in expected.items())


def begin_domain_write_watch():
    """Track app database writes only while a return navigation is in flight."""
    from sqlalchemy import event

    with app.app_context():
        engine = db.engine
    writes = []

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        command = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if command in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(command)

    event.listen(engine, "before_cursor_execute", observe)
    return engine, observe, writes


def end_domain_write_watch(watch):
    from sqlalchemy import event

    engine, observe, writes = watch
    event.remove(engine, "before_cursor_execute", observe)
    return writes


try:
    with sync_playwright() as playwright:
        assert Path(CHROMIUM_BIN).is_file(), "Chromium executable is missing"
        browser = playwright.chromium.launch(executable_path=CHROMIUM_BIN, headless=True)
        context = browser.new_context(viewport={"width": 320, "height": 900})
        context.route("**/*", bridge)
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
        page.goto(base + "/login?next=/marketplaces/commercial/")
        page.locator("[name=username]").fill(USERNAME)
        page.locator("[name=password]").fill(PASSWORD)
        page.get_by_role("button", name="Войти", exact=True).click()
        page.goto(base + "/marketplaces/commercial/")
        page.locator("#ozon-commercial-app").wait_for(state="visible")
        page.get_by_role("button", name="Подготовить цены для нескольких товаров").wait_for(state="visible")

        page.get_by_role("button", name="Подготовить цены для нескольких товаров").click()
        dialog = page.get_by_role("dialog", name="Цены для нескольких товаров")
        dialog.wait_for(state="visible")
        assert dialog.evaluate("node => node.contains(document.activeElement)"), "Dialog did not receive keyboard focus"
        assert dialog.get_by_text("Покупательская цена и скидка Ozon остаются неизвестными.", exact=False).is_visible()
        search = dialog.get_by_placeholder("Найдите товары магазина…")
        for offer in ("CI-000", "CI-001", "CI-002"):
            search.fill(offer)
            dialog.get_by_role("button", name="Найти", exact=True).click()
            row = dialog.locator(".oc-bulk-product").filter(has_text=offer)
            row.wait_for()
            row.locator("button").click()
        assert dialog.locator(".oc-bulk-selected li").count() == 3, "Selection was lost across search pages"
        assert dialog.get_by_text("Выбрано товаров: 3 из 100", exact=True).is_visible()
        dialog.locator(".oc-bulk-rule input[type=text]").fill("1200")
        dialog.get_by_role("button", name="Сравнить выбранные: 3", exact=True).click()
        dialog.get_by_role("heading", name="Проверка 3 товара").wait_for()
        assert dialog.get_by_text("Можно сохранить: 1", exact=True).is_visible()
        assert dialog.get_by_text("Цена не изменится: 1", exact=True).is_visible()
        assert dialog.get_by_text("Нельзя сохранить: 1", exact=True).is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile horizontal overflow appeared"
        passed("exact_three_item_preview_and_mobile_reflow")

        dialog.get_by_role("button", name="Сохранить 1 заявку", exact=True).click()
        dialog.get_by_role("heading", name="Заявки сохранены").wait_for()
        saved_status = dialog.get_by_role("status").filter(has_text="Сохранено заявок: 1")
        assert saved_status.count() == 1 and saved_status.is_visible()
        link = dialog.get_by_role("link", name="Проверить точный набор заявок", exact=True)
        href = link.get_attribute("href")
        assert href and href.startswith("/marketplaces/commercial/?")
        query = parse_qs(urlsplit(href).query, strict_parsing=True)
        proposal_id = report["proposal_receipt"]["proposal_id"]
        assert query == {
            "account_id": [str(account_id)],
            "proposal_kind": ["price"],
            "status": ["pending_review"],
            "proposal_ids": [str(proposal_id)],
        }, href
        passed("local_save_exact_pending_review_handoff")

        with page.expect_navigation(wait_until="domcontentloaded"):
            link.click()
        page.wait_for_url(lambda value: urlsplit(value).path == "/marketplaces/commercial/" and
                          parse_qs(urlsplit(value).query).get("proposal_ids") == [str(proposal_id)])
        page.locator(".oc-list .oc-row").first.wait_for(state="visible")
        exact_rows = page.locator(".oc-list .oc-row")
        assert exact_rows.count() == 1, "Exact pending-review GET did not return only the saved proposal"
        assert exact_rows.first.locator(".oc-row-status").get_attribute("href") == \
            "/marketplaces/commercial/" + str(proposal_id)
        assert report.get("exact_review_get") == {
            "proposal_ids": [proposal_id],
            "statuses": ["pending_review"],
            "exact_selection": True,
        }, report.get("exact_review_get")
        with app.app_context():
            persisted = db.session.get(MarketplaceCommercialProposal, proposal_id)
            assert persisted is not None
            assert persisted.status == "pending_review" and persisted.version == 1
            assert persisted.baseline_fingerprint == report["proposal_receipt"]["baseline_fingerprint"]
            assert persisted.proposed_fingerprint == report["proposal_receipt"]["proposed_fingerprint"]
        passed("authenticated_exact_review_get_keeps_local_receipt_immutable")

        page.get_by_role("button", name="Подготовить цены для нескольких товаров").click()
        dialog = page.get_by_role("dialog", name="Цены для нескольких товаров")
        search = dialog.get_by_placeholder("Найдите товары магазина…")
        search.fill("CI-003")
        dialog.get_by_role("button", name="Найти", exact=True).click()
        dialog.locator(".oc-bulk-product").filter(has_text="CI-003").wait_for()
        row = dialog.locator(".oc-bulk-product").filter(has_text="CI-003")
        row.locator("button").click()
        dialog.get_by_label("Изменить на процент").check()
        value = dialog.locator(".oc-bulk-rule input[type=text]")
        value.fill("-5")
        assert dialog.get_by_text("Для снижения цены явно разрешите снижение и укажите основание.", exact=True).is_visible()
        assert dialog.get_by_role("button", name="Сравнить выбранные: 1", exact=True).is_disabled()
        dialog.get_by_label("Разрешаю снижение цены у товаров, где правило уменьшит сумму").check()
        dialog.locator(".oc-bulk-rule textarea").fill("Синтетическое основание для теста")
        assert dialog.get_by_role("button", name="Сравнить выбранные: 1", exact=True).is_enabled()
        dialog.get_by_role("button", name="Сравнить выбранные: 1", exact=True).click()
        dialog.get_by_role("heading", name="Проверка 1 товар").wait_for()
        assert report["preview_payloads"][1]["mode"] == "percent"
        assert report["preview_payloads"][1]["value"] == "-5"
        assert report["preview_payloads"][1]["allow_price_decrease"] is True
        assert report["preview_payloads"][1]["guardrail_note"] == "Синтетическое основание для теста"
        passed("signed_percent_requires_explicit_decrease_reason")
        page.once("dialog", lambda confirm: confirm.accept())
        dialog.get_by_role("button", name="Закрыть массовую подготовку цен").click()
        assert report["postcounts"] == {
            "/marketplaces/commercial/price-proposals/bulk-preview": 2,
            "/marketplaces/commercial/price-proposals/bulk-confirm": 1,
        }, report["postcounts"]
        assert len(report["confirm_payloads"]) == 1
        assert page.evaluate("document.activeElement === document.querySelector('.oc-header-actions button:last-child')")
        assert report["provider_attempts"] == 0
        assert report["provider_write_callbacks"] == 0
        assert not report["external"], report["external"]
        assert not report["unexpected_http"], report["unexpected_http"]
        assert not report["js_errors"], report["js_errors"]
        passed("explicit_close_does_not_save_or_send_to_ozon")

        assert report["synthetic_price_reads"] == [listing_ids[:3], [listing_ids[3]]], \
            "The actual preview route read beyond the explicit selected sets"
        preview_reads_before_handoff = len(report["synthetic_price_reads"])
        handoff_posts_before = dict(report["postcounts"])
        page.goto(base + "/my-products/beta")
        page.locator("#my-products-app").wait_for(state="visible")
        source_owner = page.evaluate("JSON.parse(document.querySelector('#mp-bootstrap').textContent).workspaceOwner")
        source_title = "Тестовый товар для подготовки"
        with page.expect_response(lambda response: beta_feed_matches(response, {"search": source_title})) as source_search:
            page.get_by_placeholder("Название, бренд или артикул").fill(source_title)
        assert source_search.value.status == 200
        with page.expect_response(lambda response: beta_feed_matches(response, {
                "search": source_title, "sort": "title",
        })) as source_sort:
            page.get_by_label("Сортировка").select_option("title")
        assert source_sort.value.status == 200
        source_row = page.locator(".mp-row").filter(has_text="Тестовый товар для подготовки")
        source_row.wait_for(state="visible")
        source_row.get_by_role("checkbox", name="Выбрать Тестовый товар для подготовки").check()
        page.get_by_role("button", name="Что сделать", exact=True).click()
        authorized_navigations = []

        def capture_authorized_navigation(frame):
            parsed = urlsplit(frame.url)
            if (frame == page.main_frame and parsed.path == "/marketplaces/commercial/" and
                    parse_qs(parsed.query) == {
                        "account_id": [str(account_id)], "bulk_listing_ids": [str(listing_ids[0])],
                    } and re.fullmatch(r"workspace-return=[a-f0-9]{32}", parsed.fragment)):
                authorized_navigations.append(frame.url)

        page.on("framenavigated", capture_authorized_navigation)
        page.get_by_role("menuitem", name="Изменить цены Ozon", exact=False).click()
        page.wait_for_function("""() => {
            const dialog = document.querySelector('.oc-bulk-dialog');
            return dialog && dialog.open;
        }""")
        assert authorized_navigations, "The handoff did not navigate the same tab to the authorized page"
        authorized_url = urlsplit(authorized_navigations[-1])
        assert authorized_url.path == "/marketplaces/commercial/"
        assert parse_qs(authorized_url.query) == {
            "account_id": [str(account_id)], "bulk_listing_ids": [str(listing_ids[0])],
        }
        assert re.fullmatch(r"workspace-return=[a-f0-9]{32}", authorized_url.fragment)
        price_hash = authorized_url.fragment
        handoff = page.get_by_role("dialog", name="Цены для нескольких товаров")
        handoff.wait_for(state="visible")
        assert handoff.evaluate("node => node.contains(document.activeElement)")
        assert handoff.get_by_text("Выбранный набор передан из «Моих товаров»", exact=False).is_visible()
        assert handoff.get_by_text("Выбрано товаров: 1 из 100", exact=True).is_visible()
        assert handoff.locator(".oc-bulk-selected li").filter(has_text="CI-000").count() == 1
        header_return = page.locator("a[data-price-workspace-return]")
        header_href = urlsplit(header_return.get_attribute("href") or "")
        assert header_href.path == "/my-products/beta" and not header_href.query
        assert header_href.fragment == price_hash
        assert handoff.locator("button[data-price-workspace-return-button]").get_attribute("type") == "button"
        handoff_return = handoff.locator("button[data-price-workspace-return-button]")
        handoff_return.wait_for(state="visible")
        assert handoff_return.inner_text().strip() == "Вернуться к товарам"
        assert page.evaluate("""([expectedOwner, expectedAccount, expectedListing, expectedSource, nonce]) => {
            const boot = JSON.parse(document.querySelector('#oc-bootstrap').textContent);
            const capsule = window.SellerHubCommonContent.workspaceReturnFromHash(
                '#'+nonce, {
                    owner: boot.workspaceOwner,
                    priceTarget: {channel: 'ozon', accountId: expectedAccount, ids: [expectedListing]},
                }
            );
            return !!capsule && capsule.v === 2 && capsule.owner === expectedOwner &&
                boot.workspaceOwner === expectedOwner && capsule.selectionIds.length === 1 &&
                capsule.selectionIds[0] === expectedSource && capsule.priceTarget.channel === 'ozon' &&
                capsule.priceTarget.accountId === expectedAccount &&
                capsule.priceTarget.ids.length === 1 && capsule.priceTarget.ids[0] === expectedListing &&
                capsule.scope.search === 'Тестовый товар для подготовки' && capsule.scope.sort === 'title';
        }""", [source_owner, account_id, listing_ids[0], fixture["source_id"], price_hash])
        page.screenshot(path=OUT / "ozon-workspace-return-authorized-320.png", animations="disabled")
        assert report["postcounts"] == {
            "/marketplaces/commercial/price-proposals/bulk-preview": 2,
            "/marketplaces/commercial/price-proposals/bulk-confirm": 1,
        }, report["postcounts"]
        assert len(report["synthetic_price_reads"]) == preview_reads_before_handoff, \
            "My Products handoff unexpectedly read prices before Preview was clicked"
        assert report["provider_write_callbacks"] == 0
        passed("validated_my_products_handoff_prefills_without_preview")

        handoff_close_confirmations = []
        page.once("dialog", lambda prompt: (handoff_close_confirmations.append(prompt.message), prompt.accept()))
        handoff.get_by_role("button", name="Закрыть массовую подготовку цен").click()
        assert handoff_close_confirmations and "Выбранный набор" in handoff_close_confirmations[0]
        handoff.wait_for(state="hidden")
        header_return = page.get_by_role("link", name="Вернуться к товарам", exact=True)
        header_return.wait_for(state="visible")
        with page.expect_response(lambda response: beta_feed_matches(response, {
                "search": source_title, "sort": "title", "per_page": "50",
        })) as returned_beta_feed:
            with page.expect_navigation(wait_until="domcontentloaded"):
                header_return.click()
        assert returned_beta_feed.value.status == 200
        page.wait_for_url(lambda value: (
            urlsplit(str(value)).path == "/my-products/beta" and
            urlsplit(str(value)).fragment == price_hash
        ))
        page.wait_for_function("""([id, search]) => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy && !proxy.loading && proxy.filters.search === search &&
                proxy.filters.sort === 'title' && proxy.selected.join(',') === String(id);
        }""", arg=[fixture["source_id"], source_title])
        assert page.get_by_placeholder("Название, бренд или артикул").input_value() == source_title
        assert page.locator(".mp-list article.mp-row input[type=checkbox]:checked").evaluate_all(
            "boxes => boxes.map(box => Number(box.value))"
        ) == [fixture["source_id"]]
        assert report["postcounts"] == handoff_posts_before, report["postcounts"]
        assert len(report["synthetic_price_reads"]) == preview_reads_before_handoff
        assert report["provider_attempts"] == 0 and report["provider_write_callbacks"] == 0
        passed("beta_ozon_price_handoff_returns_exact_filtered_marks_without_preview")

        page.goto(base + "/marketplaces/commercial/")
        page.locator("#ozon-commercial-app").wait_for(state="visible")
        page.get_by_role("button", name="Подготовить цены для нескольких товаров").click()
        dialog = page.get_by_role("dialog", name="Цены для нескольких товаров")
        search = dialog.get_by_placeholder("Найдите товары магазина…")
        search.fill("CI-004")
        dialog.get_by_role("button", name="Найти", exact=True).click()
        row = dialog.locator(".oc-bulk-product").filter(has_text="CI-004")
        row.wait_for()
        row.locator("button").click()
        dialog.locator(".oc-bulk-rule input[type=text]").fill("1200")
        dialog.get_by_role("button", name="Сравнить выбранные: 1", exact=True).click()
        dialog.get_by_role("heading", name="Проверка 1 товар").wait_for()
        assert dialog.get_by_text("Можно сохранить: 1", exact=True).is_visible()
        assert report["preview_payloads"][2]["listing_ids"] == [listing_ids[4]]
        assert report["preview_results"][2]["statuses"] == ["eligible"]
        dialog.get_by_role("button", name="Сохранить 1 заявку", exact=True).click()

        unknown_status = dialog.get_by_text(
            "Ответ на сохранение неизвестен: заявки могли появиться.", exact=False
        )
        unknown_status.wait_for(state="visible")
        assert page.locator("a[data-price-workspace-return]").count() == 0
        assert page.locator("button[data-price-workspace-return-button]").count() == 0
        retry = dialog.get_by_role(
            "button", name="Проверить сохранение тем же подтверждением", exact=True
        )
        retry.wait_for(state="visible")
        close_unknown = dialog.get_by_role(
            "button", name="Закрыть массовую подготовку цен", exact=True
        )
        assert close_unknown.is_enabled(), "Unknown result must offer a deliberate close path"
        assert retry.is_enabled(), "Unknown result must offer the protected same-token retry"
        assert report["unknown_confirm_response_losses"] == 1
        assert report["replay_receipt"] is not None
        assert report["postcounts"] == {
            "/marketplaces/commercial/price-proposals/bulk-preview": 3,
            "/marketplaces/commercial/price-proposals/bulk-confirm": 2,
        }
        assert report["synthetic_price_reads"] == [
            listing_ids[:3], [listing_ids[3]], [listing_ids[4]],
        ]
        passed("actual_save_with_lost_browser_response_is_unknown")

        unknown_close_confirmations = []
        page.once("dialog", lambda prompt: (unknown_close_confirmations.append(prompt.message), prompt.accept()))
        close_unknown.click()
        assert unknown_close_confirmations and \
            unknown_close_confirmations[0].startswith("Ответ на сохранение неизвестен:")
        dialog.wait_for(state="hidden")

        page.get_by_role("button", name="Подготовить цены для нескольких товаров").click()
        dialog.wait_for(state="visible")
        retry = dialog.get_by_role(
            "button", name="Проверить сохранение тем же подтверждением", exact=True
        )
        retry.wait_for(state="visible")
        assert retry.evaluate("node => node === document.activeElement"), \
            "Reopening unknown confirmation must focus the protected retry"
        assert dialog.get_by_text(
            "Ответ на сохранение неизвестен: заявки могли появиться.", exact=False
        ).is_visible()
        assert report["postcounts"] == {
            "/marketplaces/commercial/price-proposals/bulk-preview": 3,
            "/marketplaces/commercial/price-proposals/bulk-confirm": 2,
        }, "Reopening unknown confirmation must not start a new preview or save"
        assert len(report["synthetic_price_reads"]) == 3
        passed("close_reopen_keeps_unknown_review_and_same_token")

        retry.click()
        dialog.get_by_role("heading", name="Ранее сохранённые заявки найдены").wait_for()
        replay_status = dialog.get_by_role("status").filter(has_text="Сохранено заявок: 1")
        assert replay_status.count() == 1 and replay_status.is_visible()
        assert report["confirm_replay"] == {
            "same_preview_token_reused": True,
            "same_proposal_id": report["replay_receipt"]["proposal_id"],
            "replayed": True,
            "saved_proposal_count": 1,
            "provider_operation_created": False,
        }
        assert [item["attempt"] for item in report["confirm_payloads"]] == [1, 2, 3]
        assert all(item["token_matches_reviewed_preview"] for item in report["confirm_payloads"])
        assert report["postcounts"] == {
            "/marketplaces/commercial/price-proposals/bulk-preview": 3,
            "/marketplaces/commercial/price-proposals/bulk-confirm": 3,
        }
        assert report["unknown_confirm_response_losses"] == 1
        assert report["provider_attempts"] == 0
        assert report["provider_write_callbacks"] == 0
        assert not report["external"], report["external"]
        assert not report["unexpected_http"], report["unexpected_http"]
        assert not report["js_errors"], report["js_errors"]
        with app.app_context():
            assert MarketplaceCommercialProposal.query.filter_by(
                seller_id=fixture["seller_id"],
                account_id=account_id,
                proposal_kind="price",
            ).count() == 2
            assert MarketplaceOperation.query.count() == 0
        passed("same_token_retry_replays_one_existing_pending_proposal")

        # A second real Beta handoff uses a fresh unproposed listing, then
        # returns from the local saved state without another read or write.
        saved_return_listing_id = listing_ids[5]
        with app.app_context():
            source = db.session.get(ImportedProduct, fixture["source_id"])
            previous_link = db.session.get(MarketplaceListing, listing_ids[0])
            saved_return_listing = db.session.get(MarketplaceListing, saved_return_listing_id)
            if source is None or previous_link is None or saved_return_listing is None:
                raise AssertionError("Saved return fixture is missing its exact local source or listing")
            previous_link.imported_product_id = None
            previous_link.link_status = "unlinked"
            previous_link.link_source = None
            previous_link.linked_at = None
            previous_link.linked_by_user_id = None
            saved_return_listing.imported_product_id = source.id
            saved_return_listing.link_status = "linked"
            saved_return_listing.link_source = "synthetic_browser_fixture"
            saved_return_listing.linked_at = datetime.utcnow()
            price_summary = json.loads(saved_return_listing.price_summary_json or "{}")
            price_summary.update({
                "available": True,
                "currency": "RUB",
                "values": {"price": "1000", "old_price": "1500", "min_price": "500"},
            })
            saved_return_listing.price_summary_json = json.dumps(price_summary, ensure_ascii=False)
            db.session.commit()
        synthetic_prices[saved_return_listing_id] = ("1000", "1500", "500")

        page.goto(base + "/my-products/beta")
        page.locator("#my-products-app").wait_for(state="visible")
        with page.expect_response(lambda response: beta_feed_matches(response, {"search": source_title})) as saved_source_search:
            page.get_by_placeholder("Название, бренд или артикул").fill(source_title)
        assert saved_source_search.value.status == 200
        with page.expect_response(lambda response: beta_feed_matches(response, {
                "search": source_title, "sort": "title",
        })) as saved_source_sort:
            page.get_by_label("Сортировка").select_option("title")
        assert saved_source_sort.value.status == 200
        saved_source_row = page.locator(".mp-row").filter(has_text=source_title)
        saved_source_row.wait_for(state="visible")
        saved_source_row.get_by_role("checkbox", name="Выбрать Тестовый товар для подготовки").check()
        page.get_by_role("button", name="Что сделать", exact=True).click()
        saved_authorized_navigations = []

        def capture_saved_authorized_navigation(frame):
            parsed = urlsplit(frame.url)
            if (frame == page.main_frame and parsed.path == "/marketplaces/commercial/" and
                    parse_qs(parsed.query) == {
                        "account_id": [str(account_id)],
                        "bulk_listing_ids": [str(saved_return_listing_id)],
                    } and re.fullmatch(r"workspace-return=[a-f0-9]{32}", parsed.fragment)):
                saved_authorized_navigations.append(frame.url)

        page.on("framenavigated", capture_saved_authorized_navigation)
        page.get_by_role("menuitem", name="Изменить цены Ozon", exact=False).click()
        page.wait_for_function("""() => {
            const dialog = document.querySelector('.oc-bulk-dialog');
            return dialog && dialog.open;
        }""")
        assert saved_authorized_navigations
        saved_authorized_url = urlsplit(saved_authorized_navigations[-1])
        assert saved_authorized_url.path == "/marketplaces/commercial/"
        assert parse_qs(saved_authorized_url.query) == {
            "account_id": [str(account_id)],
            "bulk_listing_ids": [str(saved_return_listing_id)],
        }
        assert re.fullmatch(r"workspace-return=[a-f0-9]{32}", saved_authorized_url.fragment)
        saved_price_hash = saved_authorized_url.fragment
        dialog = page.get_by_role("dialog", name="Цены для нескольких товаров")
        dialog.get_by_text("Выбранный набор передан из «Моих товаров»", exact=False).wait_for()
        assert page.evaluate("""([expectedOwner, expectedAccount, expectedListing, expectedSource, nonce]) => {
            const boot = JSON.parse(document.querySelector('#oc-bootstrap').textContent);
            const capsule = window.SellerHubCommonContent.workspaceReturnFromHash(
                '#'+nonce, {
                    owner: boot.workspaceOwner,
                    priceTarget: {channel: 'ozon', accountId: expectedAccount, ids: [expectedListing]},
                }
            );
            return !!capsule && capsule.v === 2 && boot.workspaceOwner === expectedOwner &&
                capsule.owner === expectedOwner && capsule.selectionIds.length === 1 &&
                capsule.selectionIds[0] === expectedSource && capsule.priceTarget.accountId === expectedAccount &&
                capsule.priceTarget.ids.length === 1 && capsule.priceTarget.ids[0] === expectedListing &&
                capsule.scope.search === 'Тестовый товар для подготовки' && capsule.scope.sort === 'title';
        }""", [source_owner, account_id, saved_return_listing_id, fixture["source_id"], saved_price_hash])
        dialog.locator(".oc-bulk-rule input[type=text]").fill("1200")
        dialog.get_by_role("button", name="Сравнить выбранные: 1", exact=True).click()
        dialog.get_by_role("heading", name="Проверка 1 товар").wait_for()
        assert report["preview_payloads"][3]["listing_ids"] == [saved_return_listing_id]
        assert report["preview_results"][3]["statuses"] == ["eligible"]

        busy_delay["saved_return_confirm"] = True
        dialog.get_by_role("button", name="Сохранить 1 заявку", exact=True).click()
        dialog.get_by_role("button", name="Сохраняем заявки…", exact=True).wait_for(state="visible")
        assert dialog.locator("button[data-price-workspace-return-button]").count() == 0
        assert page.evaluate("""() => {
            const dialog = document.querySelector('.oc-bulk-dialog');
            const headerReturn = document.querySelector('a[data-price-workspace-return]');
            return !!dialog && dialog.matches(':modal') && !!headerReturn &&
                !dialog.contains(headerReturn);
        }""")
        dialog.get_by_role("heading", name="Заявки сохранены").wait_for()
        saved_return_button = dialog.locator("button[data-price-workspace-return-button]")
        saved_return_button.wait_for(state="visible")
        assert saved_return_button.inner_text().strip() == "Вернуться к товарам"
        header_href = urlsplit(page.locator("a[data-price-workspace-return]").get_attribute("href") or "")
        assert header_href.path == "/my-products/beta" and not header_href.query
        assert header_href.fragment == saved_price_hash
        saved_counts_before_return = dict(report["postcounts"])
        saved_reads_before_return = list(report["synthetic_price_reads"])
        saved_return_prompts = []
        page.on("dialog", lambda prompt: (saved_return_prompts.append(prompt.type), prompt.accept()))
        with page.expect_response(lambda response: beta_feed_matches(response, {
                "search": source_title, "sort": "title", "per_page": "50",
        })) as saved_return_feed:
            with page.expect_navigation(wait_until="domcontentloaded"):
                saved_return_button.click()
        assert not saved_return_prompts, "A completed local save should return without another confirmation"
        assert saved_return_feed.value.status == 200
        page.wait_for_url(lambda value: (
            urlsplit(str(value)).path == "/my-products/beta" and
            urlsplit(str(value)).fragment == saved_price_hash
        ))
        page.wait_for_function("""([id, search]) => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy && !proxy.loading && proxy.filters.search === search &&
                proxy.filters.sort === 'title' && proxy.selected.join(',') === String(id);
        }""", arg=[fixture["source_id"], source_title])
        assert page.locator(".mp-list article.mp-row input[type=checkbox]:checked").evaluate_all(
            "boxes => boxes.map(box => Number(box.value))"
        ) == [fixture["source_id"]]
        assert report["postcounts"] == saved_counts_before_return
        assert report["synthetic_price_reads"] == saved_reads_before_return
        assert report["provider_attempts"] == 0 and report["provider_write_callbacks"] == 0
        passed("ozon_saved_dialog_return_restores_exact_beta_marks_without_provider_replay")

        # Exercise a saved exact Ozon result whose saved proposal subset is
        # smaller than the original three-item My Products selection because
        # one item is a no-op. The result link is available only on this exact
        # pending-review set and returns all three original source marks.
        multi_source_title = "R41 Ozon price return source"
        multi_listing_ids = listing_ids[6:9]
        with app.app_context():
            multi_sources = [ImportedProduct(
                seller_id=fixture["seller_id"],
                external_id=f"r41-price-return-{index}",
                external_vendor_code=f"R41-PRICE-RETURN-{index}",
                source_type="synthetic",
                title=f"{multi_source_title} {letter}",
                import_status="validated",
                photo_urls="[]",
            ) for index, letter in enumerate(("A", "B", "C"), start=1)]
            db.session.add_all(multi_sources)
            db.session.flush()
            for offset, (source, listing_id) in enumerate(zip(multi_sources, multi_listing_ids)):
                listing = db.session.get(MarketplaceListing, listing_id)
                if listing is None:
                    raise AssertionError("R41 exact return fixture listing is missing")
                listing.imported_product_id = source.id
                listing.link_status = "linked"
                listing.link_source = "synthetic_browser_fixture"
                listing.linked_at = datetime.utcnow()
                price = "1200" if offset == 1 else "1000"
                summary = json.loads(listing.price_summary_json or "{}")
                summary.update({
                    "available": True,
                    "currency": "RUB",
                    "values": {"price": price, "old_price": "1500", "min_price": "500"},
                })
                listing.price_summary_json = json.dumps(summary, ensure_ascii=False)
            multi_source_ids = [source.id for source in multi_sources]
            db.session.commit()
        synthetic_prices.update({
            multi_listing_ids[0]: ("1000", "1500", "500"),
            multi_listing_ids[1]: ("1200", "1500", "500"),
            multi_listing_ids[2]: ("1000", "1500", "500"),
        })

        page.goto(base + "/my-products/beta")
        page.locator("#my-products-app").wait_for(state="visible")
        with page.expect_response(lambda response: beta_feed_matches(response, {
                "search": multi_source_title,
        })) as multi_source_search:
            page.get_by_placeholder("Название, бренд или артикул").fill(multi_source_title)
        assert multi_source_search.value.status == 200
        with page.expect_response(lambda response: beta_feed_matches(response, {
                "search": multi_source_title, "sort": "title",
        })) as multi_source_sort:
            page.get_by_label("Сортировка").select_option("title")
        assert multi_source_sort.value.status == 200
        multi_rows = page.locator(".mp-row")
        assert multi_rows.count() == 3
        for letter in ("A", "B", "C"):
            page.get_by_role(
                "checkbox", name=f"Выбрать {multi_source_title} {letter}", exact=True,
            ).check()
        selected_multi_ids = page.locator(
            ".mp-list article.mp-row input[type=checkbox]:checked"
        ).evaluate_all("boxes => boxes.map(box => Number(box.value))")
        assert selected_multi_ids == multi_source_ids
        page.get_by_role("button", name="Что сделать", exact=True).click()
        multi_authorized_navigations = []

        def capture_multi_authorized_navigation(frame):
            parsed = urlsplit(frame.url)
            if (frame == page.main_frame and parsed.path == "/marketplaces/commercial/" and
                    parse_qs(parsed.query) == {
                        "account_id": [str(account_id)],
                        "bulk_listing_ids": [",".join(str(value) for value in multi_listing_ids)],
                    } and re.fullmatch(r"workspace-return=[a-f0-9]{32}", parsed.fragment)):
                multi_authorized_navigations.append(frame.url)

        page.on("framenavigated", capture_multi_authorized_navigation)
        page.get_by_role("menuitem", name="Изменить цены Ozon", exact=False).click()
        page.wait_for_function("""() => {
            const dialog = document.querySelector('.oc-bulk-dialog');
            return dialog && dialog.open;
        }""")
        assert multi_authorized_navigations
        multi_hash = urlsplit(multi_authorized_navigations[-1]).fragment
        dialog = page.get_by_role("dialog", name="Цены для нескольких товаров")
        assert dialog.get_by_text("Выбрано товаров: 3 из 100", exact=True).is_visible()
        assert dialog.locator(".oc-bulk-selected li").count() == 3
        multi_capsule = page.evaluate("""([owner, accountId, listingIds, sourceIds, search, nonce]) => {
            const capsule = window.SellerHubCommonContent.workspaceReturnFromHash(
                '#'+nonce, {
                    owner,
                    priceTarget: {channel: 'ozon', accountId, ids: listingIds},
                }
            );
            return !!capsule && capsule.v === 2 && capsule.owner === owner &&
                capsule.priceTarget.accountId === accountId &&
                JSON.stringify(capsule.priceTarget.ids) === JSON.stringify(listingIds) &&
                JSON.stringify(capsule.selectionIds) === JSON.stringify(sourceIds) &&
                capsule.scope.search === search && capsule.scope.sort === 'title';
        }""", [source_owner, account_id, multi_listing_ids, multi_source_ids,
              multi_source_title, multi_hash])
        assert multi_capsule
        dialog.locator(".oc-bulk-rule input[type=text]").fill("1200")
        dialog.get_by_role("button", name="Сравнить выбранные: 3", exact=True).click()
        dialog.get_by_role("heading", name="Проверка 3 товара").wait_for()
        assert dialog.get_by_text("Можно сохранить: 2", exact=True).is_visible()
        assert dialog.get_by_text("Цена не изменится: 1", exact=True).is_visible()
        assert dialog.get_by_text("Нельзя сохранить: 0", exact=True).is_visible()
        assert report["preview_payloads"][4]["listing_ids"] == multi_listing_ids
        assert report["preview_results"][4] == {
            "selected_count": 3,
            "eligible_count": 2,
            "noop_count": 1,
            "blocked_count": 0,
            "statuses": ["eligible", "noop", "eligible"],
            "reason_codes": [None, "price_unchanged", None],
        }
        dialog.get_by_role("button", name="Сохранить 2 заявки", exact=True).click()
        dialog.get_by_role("heading", name="Заявки сохранены").wait_for()
        exact_review_link = dialog.get_by_role("link", name="Проверить точный набор заявок", exact=True)
        exact_review_href = exact_review_link.get_attribute("href") or ""
        exact_parts = urlsplit(exact_review_href)
        exact_query = parse_qs(exact_parts.query, strict_parsing=True)
        assert exact_parts.path == "/marketplaces/commercial/"
        assert exact_query.get("account_id") == [str(account_id)]
        assert exact_query.get("proposal_kind") == ["price"]
        assert exact_query.get("status") == ["pending_review"]
        exact_proposal_ids = [int(value) for value in exact_query["proposal_ids"][0].split(",")]
        assert len(exact_proposal_ids) == 2
        assert report["saved_return_subset_proposal_ids"] == exact_proposal_ids
        assert not set(exact_proposal_ids) & set(report.get("noop_proposal_ids", []))
        assert exact_parts.fragment == multi_hash

        with app.app_context():
            exact_receipt_before_open = {
                "proposal_ids": [proposal.id for proposal in MarketplaceCommercialProposal.query.filter(
                    MarketplaceCommercialProposal.id.in_(exact_proposal_ids)
                ).order_by(MarketplaceCommercialProposal.id).all()],
                "operations": MarketplaceOperation.query.count(),
            }
        assert sorted(exact_receipt_before_open["proposal_ids"]) == sorted(exact_proposal_ids)
        saved_capsule_key = "seller-hub-common-content-workspace-return-v1"
        saved_capsule_json = page.evaluate("key => sessionStorage.getItem(key)", saved_capsule_key)
        assert saved_capsule_json

        # Follow the exact proposal-list link in this same tab. The browser
        # preserves the session capsule and its returned selection despite
        # the saved set containing only two of the original three listings.
        page.set_viewport_size({"width": 320, "height": 640})
        exact_get_watch = begin_domain_write_watch()
        try:
            with page.expect_navigation(wait_until="domcontentloaded") as exact_navigation:
                exact_review_link.click()
            assert exact_navigation.value is not None and exact_navigation.value.status == 200
            page.locator("#ozon-commercial-app").wait_for(state="visible")
            page.locator(".oc-list .oc-row").first.wait_for(state="visible")
        finally:
            exact_get_writes = end_domain_write_watch(exact_get_watch)
        assert exact_get_writes == [], exact_get_writes
        assert urlsplit(page.url).fragment == multi_hash
        assert parse_qs(urlsplit(page.url).query).get("proposal_ids") == [
            ",".join(str(value) for value in exact_proposal_ids),
        ]
        exact_rows = page.locator(".oc-list .oc-row")
        assert exact_rows.count() == 2
        exact_row_ids = exact_rows.evaluate_all(
            "rows => rows.map(row => Number(row.querySelector('.oc-row-status')?.getAttribute('href')?.split('/').pop()))"
        )
        assert sorted(exact_row_ids) == sorted(exact_proposal_ids)
        # The bridge reads the real Flask JSON body from route.fetch before
        # fulfilling it; do not consume the browser Response after navigation.
        assert report["exact_review_get"]["exact_selection"] is True
        assert sorted(report["exact_review_get"]["proposal_ids"]) == sorted(exact_proposal_ids)
        assert report["exact_review_get"]["statuses"] == ["pending_review", "pending_review"]
        exact_return = page.locator("a[data-price-workspace-return]")
        exact_return.wait_for(state="visible")
        exact_return_href = urlsplit(exact_return.get_attribute("href") or "")
        assert exact_return_href.path == "/my-products/beta" and not exact_return_href.query
        assert exact_return_href.fragment == multi_hash
        assert page.evaluate("""([owner, accountId, listingIds, proposalIds]) => {
            const capsule = window.SellerHubCommonContent.workspaceReturnFromHash(
                window.location.hash, {owner}
            );
            return !!capsule && capsule.v === 3 && capsule.owner === owner &&
                capsule.priceResult.channel === 'ozon' &&
                capsule.priceResult.accountId === accountId &&
                JSON.stringify(capsule.priceResult.proposalIds) ===
                    JSON.stringify(proposalIds.slice().sort((a,b)=>a-b)) &&
                capsule.priceTarget.ids.length === listingIds.length &&
                JSON.stringify(capsule.priceTarget.ids) === JSON.stringify(listingIds) &&
                capsule.selectionIds.length === 3;
        }""", [source_owner, account_id, multi_listing_ids, exact_proposal_ids])

        mobile_nav = page.locator("details.pricing-workspace-mobile-nav")
        assert mobile_nav.count() == 1
        mobile_summary = mobile_nav.locator("summary")
        mobile_links = mobile_nav.locator(".pricing-workspace-mobile-links a")
        assert not mobile_nav.evaluate("element => element.open")
        assert mobile_summary.is_visible()
        initial_mobile_geometry = page.evaluate("""() => {
            const rect = element => {
                const box = element.getBoundingClientRect();
                return {left:box.left, right:box.right, top:box.top, bottom:box.bottom,
                        width:box.width, height:box.height};
            };
            const summary = document.querySelector('details.pricing-workspace-mobile-nav > summary');
            const returnLink = document.querySelector('a[data-price-workspace-return]');
            return {
                viewportWidth:document.documentElement.clientWidth,
                viewportHeight:window.innerHeight,
                scrollWidth:document.documentElement.scrollWidth,
                scrollX:window.scrollX,
                scrollY:window.scrollY,
                summary:summary ? rect(summary) : null,
                returnLink:returnLink ? rect(returnLink) : null,
            };
        }""")
        assert initial_mobile_geometry["viewportWidth"] == 320, initial_mobile_geometry
        assert initial_mobile_geometry["viewportHeight"] == 640, initial_mobile_geometry
        assert initial_mobile_geometry["scrollWidth"] <= initial_mobile_geometry["viewportWidth"] + 1, initial_mobile_geometry
        assert initial_mobile_geometry["scrollX"] == initial_mobile_geometry["scrollY"] == 0, initial_mobile_geometry
        for element_name in ("summary", "returnLink"):
            bounds = initial_mobile_geometry[element_name]
            assert bounds and bounds["width"] > 0 and bounds["height"] > 0, initial_mobile_geometry
            assert bounds["left"] >= -1 and bounds["right"] <= 321, initial_mobile_geometry
            assert bounds["top"] >= 0 and bounds["bottom"] <= 640, initial_mobile_geometry
        assert initial_mobile_geometry["summary"]["height"] >= 44, initial_mobile_geometry
        page.screenshot(path=OUT / "ozon-price-result-return-exact-320.png", animations="disabled")

        nav_posts_before = dict(report["postcounts"])
        nav_price_reads_before = list(report["synthetic_price_reads"])
        nav_provider_attempts_before = report["provider_attempts"]
        nav_provider_writes_before = report["provider_write_callbacks"]
        nav_external_before = list(report["external"])
        nav_watch = begin_domain_write_watch()
        try:
            mobile_summary.focus()
            mobile_summary.press("Enter")
            assert mobile_nav.evaluate("element => element.open")
            mobile_links.wait_for(state="visible")
            link_accessibility = mobile_links.evaluate_all("""links => links.map(link => ({
                label:link.innerText.trim(), href:link.getAttribute('href'),
                current:link.getAttribute('aria-current'), visible:link.getClientRects().length > 0,
            }))""")
            assert link_accessibility and all(
                item["label"] and item["href"] and item["visible"] for item in link_accessibility
            ), link_accessibility
            assert any(item["label"] == "Предложения Ozon" and item["current"] == "page"
                       for item in link_accessibility), link_accessibility
            mobile_summary.press("Enter")
            assert not mobile_nav.evaluate("element => element.open")
            assert mobile_nav.locator(".pricing-workspace-mobile-links a:visible").count() == 0
        finally:
            nav_writes = end_domain_write_watch(nav_watch)
        assert nav_writes == [], nav_writes
        assert report["postcounts"] == nav_posts_before
        assert report["synthetic_price_reads"] == nav_price_reads_before
        assert report["provider_attempts"] == nav_provider_attempts_before == 0
        assert report["provider_write_callbacks"] == nav_provider_writes_before == 0
        assert report["external"] == nav_external_before

        def open_result_probe(target_url, mutation=None, expected_status=200):
            probe = context.new_page()
            probe.goto(base + "/my-products/beta")
            probe.locator("#my-products-app").wait_for(state="visible")
            probe.evaluate("([key, value]) => sessionStorage.setItem(key, value)",
                           [saved_capsule_key, saved_capsule_json])
            if mutation == "expired":
                probe.evaluate("""key => {
                    const capsule = JSON.parse(sessionStorage.getItem(key));
                    capsule.expiresAt = Date.now() - 1;
                    sessionStorage.setItem(key, JSON.stringify(capsule));
                }""", saved_capsule_key)
            elif mutation == "legacy":
                probe.evaluate("""key => {
                    const capsule = JSON.parse(sessionStorage.getItem(key));
                    capsule.v = 2;
                    delete capsule.priceResult;
                    sessionStorage.setItem(key, JSON.stringify(capsule));
                }""", saved_capsule_key)
            watch = begin_domain_write_watch()
            try:
                response = probe.goto(base + target_url)
                assert response is not None and response.status == expected_status, (
                    target_url, response.status if response else None,
                )
                if expected_status == 200:
                    probe.locator(".oc-list[aria-busy='false']").wait_for(state="visible")
            finally:
                writes = end_domain_write_watch(watch)
            assert writes == [], writes
            assert probe.locator("a[data-price-workspace-return]:visible").count() == 0
            probe.close()

        # A different saved proposal set is same-owner and pending, but is
        # not the exact result created from this tab's target selection.
        different_set_query = [
            ("account_id", str(account_id)),
            ("proposal_kind", "price"),
            ("status", "pending_review"),
            ("proposal_ids", str(report["proposal_receipt"]["proposal_id"])),
        ]
        different_set_url = "/marketplaces/commercial/?" + urlencode(different_set_query) + "#" + multi_hash
        open_result_probe(different_set_url)

        # The same saved result under a different account is rejected by the
        # authenticated exact-scope route and must never expose a return link.
        expected_http.add(("/marketplaces/commercial/", 404))
        foreign_account_url = "/marketplaces/commercial/?" + urlencode([
            ("account_id", str(fixture["foreign_account_id"])),
            ("proposal_kind", "price"),
            ("status", "pending_review"),
            ("proposal_ids", ",".join(str(value) for value in exact_proposal_ids)),
        ]) + "#" + multi_hash
        open_result_probe(foreign_account_url, expected_status=404)
        open_result_probe(exact_review_href, mutation="expired")
        open_result_probe(exact_review_href, mutation="legacy")

        # Exact review navigation and return are reads only: no new preview,
        # confirmation, provider price read, or marketplace operation occurs.
        return_posts_before = dict(report["postcounts"])
        return_reads_before = list(report["synthetic_price_reads"])
        exact_review_return_watch = begin_domain_write_watch()
        try:
            with page.expect_response(lambda response: beta_feed_matches(response, {
                    "search": multi_source_title, "sort": "title", "per_page": "50",
            })) as exact_return_feed:
                with page.expect_navigation(wait_until="domcontentloaded"):
                    exact_return.click()
            assert exact_return_feed.value.status == 200
        finally:
            exact_review_return_writes = end_domain_write_watch(exact_review_return_watch)
        page.wait_for_function("""([ids, search]) => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy && !proxy.loading && proxy.filters.search === search &&
                proxy.filters.sort === 'title' &&
                JSON.stringify(proxy.selected) === JSON.stringify(ids);
        }""", arg=[multi_source_ids, multi_source_title])
        assert page.locator(".mp-list article.mp-row input[type=checkbox]:checked").evaluate_all(
            "boxes => boxes.map(box => Number(box.value))"
        ) == multi_source_ids
        assert exact_get_writes == [] and exact_review_return_writes == []
        assert report["postcounts"] == return_posts_before
        assert report["synthetic_price_reads"] == return_reads_before
        assert report["provider_attempts"] == 0 and report["provider_write_callbacks"] == 0
        with app.app_context():
            assert MarketplaceOperation.query.count() == exact_receipt_before_open["operations"] == 0
            current_proposals = MarketplaceCommercialProposal.query.filter(
                MarketplaceCommercialProposal.id.in_(exact_proposal_ids)
            ).order_by(MarketplaceCommercialProposal.id).all()
            assert [proposal.id for proposal in current_proposals] == sorted(exact_proposal_ids)
            assert all(proposal.status == "pending_review" and proposal.operation_id is None
                       for proposal in current_proposals)
        passed("ozon_exact_saved_subset_review_return_restores_all_source_marks_without_io_or_writes")
        passed("ozon_result_binding_rejects_different_set_account_expired_and_legacy_capsules")

        assert report["postcounts"] == {
            "/marketplaces/commercial/price-proposals/bulk-preview": 5,
            "/marketplaces/commercial/price-proposals/bulk-confirm": 5,
        }, report["postcounts"]
        assert report["synthetic_price_reads"] == [
            listing_ids[:3], [listing_ids[3]], [listing_ids[4]], [saved_return_listing_id], multi_listing_ids,
        ]
        assert report["saved_return_proposal_created"] is True
        assert len(report["saved_return_proposal_ids"]) == 1
        assert report["saved_return_proposal_subset_created"] is True
        assert len(report["saved_return_subset_proposal_ids"]) == 2
        with app.app_context():
            assert MarketplaceCommercialProposal.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id, proposal_kind="price",
            ).count() == 5
            assert MarketplaceOperation.query.count() == 0
        browser.close()
    report["status"] = "passed"
except Exception as error:
    report["status"] = "failed"
    report["error"] = str(error)[:3000]
    raise
finally:
    try:
        server.shutdown()
        thread.join(timeout=3)
    finally:
        temporary.cleanup()
        (OUT / "ozon-bulk-price-proposals-browser-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
