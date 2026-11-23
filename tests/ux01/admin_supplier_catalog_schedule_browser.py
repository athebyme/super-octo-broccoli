"""Synthetic browser acceptance for the admin supplier catalog schedule.

The real Flask admin page and CSRF-protected save route use a disposable
SQLite database. One explicit call to the normal catalog scheduler consumes
fixed in-memory CSV bytes; every other provider/network path is denied.

Run with the repository venv and Chromium installed. The report is written to
``/artifacts/supplier-catalog-schedule/report.json`` by default.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.ux01 import journey_browser as fixture
from models import (
    AdminAuditLog,
    ImportedProduct,
    MarketplaceListing,
    Product,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from services.supplier_service import SupplierCSVParser, SupplierService


ARTIFACTS = Path(os.environ.get(
    "SUPPLIER_CATALOG_SCHEDULE_ARTIFACTS",
    "/artifacts/supplier-catalog-schedule",
))
ARTIFACTS.mkdir(parents=True, exist_ok=True)
REPORT_PATH = Path(os.environ.get(
    "SUPPLIER_CATALOG_SCHEDULE_REPORT",
    str(ARTIFACTS / "report.json"),
))
CSV_BYTES = (
    b"id,sku,title,description,price,quantity\n"
    b"R95-SCHEDULE-001,R95-SCHEDULE-001,Updated catalog title,"
    b"Synthetic scheduled catalog description,37,6\n"
)
ADMIN_USERNAME = "r95-catalog-schedule-admin"
ADMIN_PASSWORD = "synthetic-r95-admin-password"

REPORT = {
    "status": "running",
    "scope": "synthetic_authenticated_admin_supplier_catalog_schedule",
    "network_policy": "network_none_except_local_flask_and_allowlisted_static_assets",
    "route_counts": {},
    "admission": {},
    "execution": {},
    "visible_receipt": {},
    "negative_cases": {},
    "geometry": [],
    "keyboard_focus": [],
    "screenshots": [],
    "provider_write_counters": {},
    "unexpected_external_requests": [],
    "unexpected_http": [],
    "javascript_errors": [],
}


def _ensure_storage():
    """Install the additive queue tables in the journey's disposable DB."""
    from migrations.migrate_add_supplier_catalog_refresh_runs import migrate as migrate_refresh
    from migrations.migrate_add_supplier_catalog_schedule import migrate as migrate_schedule

    with fixture.app.app_context():
        database = db.engine.url.database
        migrate_refresh(database)
        migrate_schedule(database)


def _seed_supplier_and_admin():
    with fixture.app.app_context():
        user = User(
            username=ADMIN_USERNAME,
            email=f"{ADMIN_USERNAME}@example.test",
            is_admin=True,
            is_active=True,
        )
        user.set_password(ADMIN_PASSWORD)
        supplier = Supplier(
            name="R95 schedule browser fixture",
            code="r95-schedule-browser-fixture",
            csv_source_url="https://r95-schedule-fixture.invalid/catalog.csv",
            csv_delimiter=",",
            csv_encoding="utf-8",
            csv_has_header=True,
            csv_column_mapping={
                "external_id": {"type": "string", "column": "id"},
                "vendor_code": {"type": "string", "column": "sku"},
                "title": {"type": "string", "column": "title"},
                "description": {"type": "string", "column": "description"},
                "supplier_price": {"type": "number", "column": "price"},
                "supplier_quantity": {"type": "number", "column": "quantity"},
            },
            price_file_url="https://r95-schedule-fixture.invalid/prices.csv",
            price_file_encoding="utf-8",
            price_file_delimiter=",",
            auto_sync_prices=True,
        )
        seller_id = fixture.FIXTURE["seller_id"]
        db.session.add_all([user, supplier])
        db.session.flush()
        supplier.last_price_sync_status = "success"
        supplier.last_price_sync_at = datetime(2026, 11, 20, 10, 0)
        supplier.last_price_sync_attempt_at = datetime(2026, 11, 20, 10, 0)
        supplier.last_price_file_hash = "c" * 64

        source = SupplierProduct(
            supplier_id=supplier.id,
            external_id="R95-SCHEDULE-001",
            vendor_code="R95-SCHEDULE-001",
            title="Previous catalog title",
            description="Previous source description",
            supplier_price=19.0,
            supplier_quantity=2,
            original_data_json=json.dumps({"title": "Previous catalog title"}),
            content_revision=1,
        )
        market_product = Product(
            seller_id=seller_id,
            nm_id=987654321001,
            vendor_code="R95-LOCAL-MARKET",
            title="Existing marketplace product remains untouched",
            price=150.0,
            discount_price=120.0,
            quantity=11,
            supplier_price=55.0,
            is_active=True,
        )
        db.session.add_all([source, market_product])
        db.session.flush()
        seller_copy = ImportedProduct(
            seller_id=seller_id,
            supplier_id=supplier.id,
            supplier_product_id=source.id,
            product_id=market_product.id,
            external_id="R95-SELLER-COPY-001",
            external_vendor_code="R95-SCHEDULE-001",
            title="Seller-owned copy stays unchanged",
            description="Seller copy description remains local.",
            supplier_price=23.0,
            supplier_quantity=8,
            calculated_price=123.0,
            calculated_discount_price=111.0,
            calculated_price_before_discount=145.0,
            content_overrides_json='{"schema_version":1,"fields":{"title":"Seller-owned copy stays unchanged"}}',
            supplier_content_revision=1,
            import_status="imported",
        )
        db.session.add_all([
            seller_copy,
            SellerSupplier(
                seller_id=seller_id,
                supplier_id=supplier.id,
                is_active=True,
            ),
        ])
        db.session.commit()

        user_id = user.id
        supplier_id = supplier.id
        source_id = source.id
        imported_id = seller_copy.id
        product_id = market_product.id
        price_state = {
            "price_file_url": supplier.price_file_url,
            "last_price_sync_status": supplier.last_price_sync_status,
            "last_price_sync_at": supplier.last_price_sync_at.isoformat(),
            "last_price_sync_attempt_at": supplier.last_price_sync_attempt_at.isoformat(),
            "last_price_file_hash": supplier.last_price_file_hash,
        }
        db.session.remove()

    return {
        "admin_id": user_id,
        "supplier_id": supplier_id,
        "source_id": source_id,
        "imported_id": imported_id,
        "product_id": product_id,
        "seller_id": seller_id,
        "price_state": price_state,
        "listing_id": fixture.FIXTURE["listing_ids"][0],
    }


def _admin_cookie():
    """Create an actual authenticated admin session using the CSRF login form."""
    client = fixture.app.test_client()
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(rb'name="csrf_token" value="([^"]+)"', login_page.data)
    assert match, "Login page did not render a CSRF token"
    login = client.post(
        "/login",
        data={
            "csrf_token": match.group(1).decode("ascii"),
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}, login.status_code
    cookie = client.get_cookie(fixture.app.config.get("SESSION_COOKIE_NAME", "session"))
    assert cookie is not None
    return {"name": cookie.key, "value": cookie.value, "url": fixture.BASE}


def _state(fixture_ids):
    """Read exact existing seller and local-market snapshots without writes."""
    with fixture.app.app_context():
        db.session.expire_all()
        source = db.session.get(SupplierProduct, fixture_ids["source_id"])
        supplier = db.session.get(Supplier, fixture_ids["supplier_id"])
        imported = db.session.get(ImportedProduct, fixture_ids["imported_id"])
        product = db.session.get(Product, fixture_ids["product_id"])
        listing = db.session.get(MarketplaceListing, fixture_ids["listing_id"])
        assert all((source, supplier, imported, product, listing))
        return {
            "source": {
                "title": source.title,
                "description": source.description,
                "supplier_price": source.supplier_price,
                "supplier_quantity": source.supplier_quantity,
                "original_data_json": source.original_data_json,
                "content_revision": source.content_revision,
            },
            "price_state": {
                "price_file_url": supplier.price_file_url,
                "last_price_sync_status": supplier.last_price_sync_status,
                "last_price_sync_at": supplier.last_price_sync_at.isoformat(),
                "last_price_sync_attempt_at": supplier.last_price_sync_attempt_at.isoformat(),
                "last_price_file_hash": supplier.last_price_file_hash,
            },
            "imported": {
                "title": imported.title,
                "description": imported.description,
                "supplier_price": imported.supplier_price,
                "supplier_quantity": imported.supplier_quantity,
                "calculated_price": imported.calculated_price,
                "calculated_discount_price": imported.calculated_discount_price,
                "calculated_price_before_discount": imported.calculated_price_before_discount,
                "product_id": imported.product_id,
                "supplier_content_revision": imported.supplier_content_revision,
                "content_overrides_json": imported.content_overrides_json,
                "import_status": imported.import_status,
            },
            "market_product": {
                "title": product.title,
                "price": str(product.price),
                "discount_price": str(product.discount_price),
                "quantity": product.quantity,
                "supplier_price": product.supplier_price,
                "is_active": product.is_active,
            },
            "marketplace_listing": {
                "title": listing.title,
                "price_summary_json": listing.price_summary_json,
                "stock_summary_json": listing.stock_summary_json,
                "sync_fingerprint": listing.sync_fingerprint,
                "updated_at": listing.updated_at.isoformat() if listing.updated_at else None,
            },
        }


def _browser_bridge(route, *, page_path, schedule_post_path, counts, posts):
    request = route.request
    parsed = urlsplit(request.url)
    local_origin = urlsplit(fixture.BASE).netloc
    if parsed.netloc == local_origin:
        route_key = f"{request.method} {parsed.path}"
        local_routes = counts.setdefault("local_routes", {})
        local_routes[route_key] = local_routes.get(route_key, 0) + 1
        if parsed.path == page_path and request.method == "GET":
            counts["admin_products_get"] += 1
        if parsed.path == schedule_post_path and request.method == "POST":
            counts["schedule_post"] += 1
            fields = parse_qs(request.post_data or "", keep_blank_values=True)
            assert set(fields) == {
                "csrf_token",
                "catalog_schedule_enabled",
                "catalog_schedule_interval_hours",
                "catalog_schedule_revision",
            }
            assert len(fields["csrf_token"]) == 1 and fields["csrf_token"][0]
            assert len(fields["catalog_schedule_enabled"]) == 1
            assert fields["catalog_schedule_interval_hours"] == ["24"]
            post = {
                "enabled": fields["catalog_schedule_enabled"][0],
                "interval_hours": fields["catalog_schedule_interval_hours"][0],
                "expected_revision": fields["catalog_schedule_revision"][0],
                "csrf_present": True,
            }
            posts.append(post)
            if len(posts) == 1:
                assert post == {
                    "enabled": "1",
                    "interval_hours": "24",
                    "expected_revision": "",
                    "csrf_present": True,
                }
            elif len(posts) == 2:
                assert post == {
                    "enabled": "0",
                    "interval_hours": "24",
                    "expected_revision": "1",
                    "csrf_present": True,
                }
            else:
                raise AssertionError("Unexpected extra schedule write")
            response = route.fetch(max_redirects=0)
            assert response.status == 303, response.status
            posts[-1]["http_status"] = response.status
            route.fulfill(response=response)
            return
        if request.method not in {"GET", "HEAD"}:
            counts["unexpected_local_writes"] += 1
            raise AssertionError(("unexpected local browser write", request.method, parsed.path))
    fixture.bridge(route)


def _assert_geometry(page, width, label):
    page.set_viewport_size({"width": width, "height": 950})
    page.wait_for_timeout(100)
    geometry = page.evaluate("""() => {
        const panel = document.querySelector('[data-testid="catalog-schedule"]');
        const rect = panel.getBoundingClientRect();
        return {
            viewport_width: innerWidth,
            document_scroll_width: document.documentElement.scrollWidth,
            body_scroll_width: document.body.scrollWidth,
            panel_left: Math.round(rect.left),
            panel_right: Math.round(rect.right),
            panel_client_width: panel.clientWidth,
            panel_scroll_width: panel.scrollWidth,
        };
    }""")
    assert geometry["viewport_width"] == width
    assert geometry["document_scroll_width"] <= width + 1, (label, geometry)
    assert geometry["body_scroll_width"] <= width + 1, (label, geometry)
    assert geometry["panel_left"] >= -1 and geometry["panel_right"] <= width + 1, (label, geometry)
    assert geometry["panel_scroll_width"] <= geometry["panel_client_width"] + 1, (label, geometry)
    geometry["label"] = label
    REPORT["geometry"].append(geometry)


def _assert_keyboard_path(page, label):
    summary = page.locator("#catalog-schedule > summary")
    summary.focus()
    page.keyboard.press("Tab")
    enabled_radio = page.locator('input[name="catalog_schedule_enabled"]:checked')
    expect(enabled_radio).to_be_focused()
    page.keyboard.press("Tab")
    interval = page.locator('input[name="catalog_schedule_interval_hours"]')
    expect(interval).to_be_focused()
    focus_style = page.evaluate("""() => {
        const el = document.activeElement;
        const style = getComputedStyle(el);
        return {name: el.name, outline_style: style.outlineStyle, outline_width: style.outlineWidth};
    }""")
    assert focus_style["name"] == "catalog_schedule_interval_hours"
    assert focus_style["outline_style"] != "none" and focus_style["outline_width"] != "0px", focus_style
    page.keyboard.press("Tab")
    expect(page.get_by_test_id("catalog-schedule-submit")).to_be_focused()
    REPORT["keyboard_focus"].append({
        "layout": label,
        "radio_reached_by_tab": True,
        "interval_reached_by_tab": True,
        "interval_visible_outline": focus_style["outline_width"],
        "submit_reached_by_tab": True,
    })


def run():
    from services.supplier_catalog_refresh import (
        get_catalog_refresh,
        run_catalog_refresh_tick,
    )
    from services.supplier_catalog_refresh_models import SupplierCatalogRefreshRun
    from services.supplier_catalog_schedule_models import (
        SupplierCatalogSchedule,
        SupplierCatalogScheduledRun,
    )

    _ensure_storage()
    fixture_ids = _seed_supplier_and_admin()
    page_path = f"/admin/suppliers/{fixture_ids['supplier_id']}/products"
    schedule_post_path = f"/admin/suppliers/{fixture_ids['supplier_id']}/catalog-schedule"
    post_log = []
    counts = {
        "admin_products_get": 0,
        "schedule_post": 0,
        "unexpected_local_writes": 0,
        "local_routes": {},
    }
    before = _state(fixture_ids)
    assert before["price_state"] == fixture_ids["price_state"]
    report_result = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=fixture.CHROMIUM,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                viewport={"width": 320, "height": 950},
                timezone_id="UTC",
                reduced_motion="reduce",
                service_workers="block",
            )
            context.add_cookies([_admin_cookie()])
            context.route(
                "**/*",
                lambda route: _browser_bridge(
                    route,
                    page_path=page_path,
                    schedule_post_path=schedule_post_path,
                    counts=counts,
                    posts=post_log,
                ),
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
            response = page.goto(fixture.BASE + page_path, wait_until="domcontentloaded")
            assert response and response.status == 200
            expect(page.get_by_test_id("catalog-schedule")).to_be_visible()
            expect(page.get_by_test_id("catalog-schedule-status")).to_contain_text("Выключено")
            expect(page.locator('[data-testid="catalog-refresh-status"]')).to_have_attribute(
                "data-refresh-state", "idle",
            )
            details = page.get_by_test_id("catalog-schedule")
            details.locator("summary").click()
            expect(page.get_by_test_id("catalog-schedule-form")).to_be_visible()
            _assert_geometry(page, 320, "initial_mobile")
            _assert_keyboard_path(page, "mobile_enable_form")
            page.screenshot(path=ARTIFACTS / "schedule-disabled-mobile-320.png", full_page=True)
            REPORT["screenshots"].append("schedule-disabled-mobile-320.png")

            # GET/rendering is read-only: an opt-in receipt appears only after
            # the explicit browser POST, and even then the worker has not run.
            with fixture.app.app_context():
                assert SupplierCatalogSchedule.query.filter_by(
                    supplier_id=fixture_ids["supplier_id"],
                ).count() == 0
                assert SupplierCatalogRefreshRun.query.filter_by(
                    supplier_id=fixture_ids["supplier_id"],
                ).count() == 0

            page.locator('input[name="catalog_schedule_interval_hours"]').fill("24")
            page.locator('input[name="catalog_schedule_enabled"][value="1"]').check()
            with page.expect_response(
                lambda item: urlsplit(item.url).path == schedule_post_path and item.request.method == "POST",
            ) as posted:
                page.get_by_test_id("catalog-schedule-submit").click()
            assert posted.value.status == 303
            expect(page.get_by_test_id("catalog-schedule-status")).to_contain_text("Включено")
            expect(page.get_by_test_id("catalog-schedule-next-due")).to_contain_text("Следующая попытка:")
            expect(page.locator('[data-testid="catalog-refresh-status"]')).to_have_attribute(
                "data-refresh-state", "idle",
            )
            with fixture.app.app_context():
                schedule = db.session.get(
                    SupplierCatalogSchedule, fixture_ids["supplier_id"],
                )
                assert schedule is not None
                assert schedule.enabled is True
                assert schedule.interval_hours == 24
                assert schedule.authorized_by_user_id == fixture_ids["admin_id"]
                assert schedule.revision == 1
                exact_initial_due = schedule.next_due_at.strftime(
                    "%d.%m.%Y %H:%M UTC",
                )
                assert SupplierCatalogRefreshRun.query.filter_by(
                    supplier_id=fixture_ids["supplier_id"],
                ).count() == 0
            REPORT["admission"] = {
                "browser_post_status": posted.value.status,
                "enabled": True,
                "interval_hours": 24,
                "authorized_by_admin_id": fixture_ids["admin_id"],
                "revision": 1,
                "initial_next_due_at": schedule.next_due_at.isoformat(),
                "visible_next_due_label": exact_initial_due,
                "queue_rows_after_post_before_worker": 0,
                "get_and_render_are_read_only": True,
                "explicit_worker_call_before_post": False,
            }

            # Reuse this authenticated page at desktop width for keyboard and
            # geometry evidence before the one explicitly requested worker run.
            _assert_geometry(page, 1440, "enabled_desktop")
            details.locator("summary").click()
            _assert_keyboard_path(page, "desktop_enabled_form")
            page.screenshot(path=ARTIFACTS / "schedule-enabled-desktop-1440.png", full_page=True)
            REPORT["screenshots"].append("schedule-enabled-desktop-1440.png")

            # This is the only source read: transport is fixed bytes, while the
            # parser, atomic raw apply, local postprocessor, and worker receipt
            # lifecycle remain real.
            fetched = []

            def fake_catalog_transport(_parser):
                fetched.append(len(CSV_BYTES))
                return CSV_BYTES

            price_calls = []

            def forbidden_price_followup(*_args, **_kwargs):
                price_calls.append("called")
                raise AssertionError("scheduled catalog read must not enter price follow-up")

            with patch.object(
                SupplierCSVParser,
                "fetch_csv_raw",
                autospec=True,
                side_effect=fake_catalog_transport,
            ) as fetch_raw, patch.object(
                SupplierService,
                "sync_prices_and_stock",
                side_effect=forbidden_price_followup,
            ) as price_followup:
                worker_summary = run_catalog_refresh_tick(fixture.app)

            assert worker_summary["processed"] == 1, worker_summary
            assert worker_summary["scheduled"] == 1, worker_summary
            fetch_raw.assert_called_once()
            price_followup.assert_not_called()
            assert fetched == [len(CSV_BYTES)]
            after_run = _state(fixture_ids)
            assert after_run["source"]["title"] == "Updated catalog title"
            assert after_run["source"]["supplier_price"] == 37.0
            assert after_run["source"]["supplier_quantity"] == 6
            raw_snapshot = json.loads(after_run["source"]["original_data_json"])
            assert raw_snapshot["external_id"] == "R95-SCHEDULE-001"
            assert raw_snapshot["title"] == "Updated catalog title"
            assert raw_snapshot["supplier_price"] == 37.0
            assert after_run["source"]["content_revision"] > before["source"]["content_revision"]
            assert after_run["price_state"] == before["price_state"]
            assert after_run["imported"] == before["imported"]
            assert after_run["market_product"] == before["market_product"]
            assert after_run["marketplace_listing"] == before["marketplace_listing"]

            with fixture.app.app_context():
                run = SupplierCatalogRefreshRun.query.filter_by(
                    supplier_id=fixture_ids["supplier_id"],
                ).one()
                receipt = get_catalog_refresh(fixture_ids["supplier_id"], run.id)
                scheduled_extension = db.session.get(SupplierCatalogScheduledRun, run.id)
                assert receipt["status"] == "completed"
                assert receipt["trigger_kind"] == "scheduled"
                assert receipt["raw_committed_at"]
                assert receipt["due_at"] == scheduled_extension.due_at.isoformat()
                assert receipt["price_status"] == "skipped"
                assert (receipt["total"], receipt["updated"], receipt["added"]) == (1, 1, 0)
                run_id = run.id
                receipt_before_disable = receipt.copy()
                schedule_revision_after_worker = db.session.get(
                    SupplierCatalogSchedule, fixture_ids["supplier_id"],
                ).revision

            # Observe the durable worker result through the existing admin page.
            page.reload(wait_until="domcontentloaded")
            refresh_status = page.get_by_test_id("catalog-refresh-status")
            expect(refresh_status).to_have_attribute("data-refresh-state", "completed")
            origin = page.locator('[data-refresh-trigger-kind="scheduled"]')
            expect(origin).to_be_visible()
            expect(origin).to_have_text("По расписанию")
            expect(page.locator("[data-refresh-due-at]")).to_contain_text("плановая дата")
            expect(page.get_by_test_id("catalog-refresh-price")).to_contain_text(
                "Проверка цен и остатков пропущена.",
            )
            REPORT["visible_receipt"] = {
                "run_id": run_id,
                "status": refresh_status.get_attribute("data-refresh-state"),
                "origin_attribute": origin.get_attribute("data-refresh-trigger-kind"),
                "origin_label": origin.inner_text().strip(),
                "planned_date_visible": page.locator("[data-refresh-due-at]").is_visible(),
                "raw_committed": refresh_status.get_attribute("data-refresh-raw-committed"),
                "price_status_visible": page.get_by_test_id("catalog-refresh-price").inner_text().strip(),
            }
            page.screenshot(path=ARTIFACTS / "schedule-completed-desktop-1440.png", full_page=True)
            REPORT["screenshots"].append("schedule-completed-desktop-1440.png")

            # Disable through the same CSRF-protected form. The completed
            # receipt and committed raw snapshot must remain readable.
            page.get_by_test_id("catalog-schedule").locator("summary").click()
            page.locator('input[name="catalog_schedule_enabled"][value="0"]').check()
            with page.expect_response(
                lambda item: urlsplit(item.url).path == schedule_post_path and item.request.method == "POST",
            ) as disabled_post:
                page.get_by_test_id("catalog-schedule-submit").click()
            assert disabled_post.value.status == 303
            expect(page.get_by_test_id("catalog-schedule-status")).to_contain_text("Выключено")
            expect(page.get_by_test_id("catalog-refresh-status")).to_have_attribute(
                "data-refresh-state", "completed",
            )
            expect(page.locator('[data-refresh-trigger-kind="scheduled"]')).to_have_text("По расписанию")
            _assert_geometry(page, 320, "disabled_mobile")
            page.screenshot(path=ARTIFACTS / "schedule-disabled-preserved-mobile-320.png", full_page=True)
            REPORT["screenshots"].append("schedule-disabled-preserved-mobile-320.png")

            # An extra explicit scheduler tick after disable is a negative
            # check: no second byte fetch, queue receipt, or state mutation.
            with fixture.app.app_context():
                disabled_schedule = db.session.get(
                    SupplierCatalogSchedule, fixture_ids["supplier_id"],
                )
                disabled_schedule.next_due_at = datetime.utcnow() - timedelta(minutes=1)
                db.session.commit()
            disabled_summary = run_catalog_refresh_tick(fixture.app)
            assert disabled_summary["processed"] == 0, disabled_summary
            assert disabled_summary["scheduled"] == 0, disabled_summary
            fetch_raw.assert_called_once()
            price_followup.assert_not_called()
            final_state = _state(fixture_ids)
            assert final_state == after_run
            with fixture.app.app_context():
                final_schedule = db.session.get(
                    SupplierCatalogSchedule, fixture_ids["supplier_id"],
                )
                final_receipt = get_catalog_refresh(fixture_ids["supplier_id"], run_id)
                assert final_schedule.enabled is False
                assert final_schedule.revision == schedule_revision_after_worker + 1
                assert final_receipt == receipt_before_disable
                assert SupplierCatalogRefreshRun.query.filter_by(
                    supplier_id=fixture_ids["supplier_id"],
                ).count() == 1
                assert SupplierCatalogScheduledRun.query.count() == 1
                schedule_audit_count = AdminAuditLog.query.filter_by(
                    action="set_supplier_catalog_schedule",
                    target_id=fixture_ids["supplier_id"],
                ).count()
                manual_queue_audit_count = AdminAuditLog.query.filter_by(
                    action="queue_supplier_catalog_refresh",
                    target_id=fixture_ids["supplier_id"],
                ).count()
                assert schedule_audit_count == 2
                assert manual_queue_audit_count == 0

            REPORT["execution"] = {
                "worker_summary": worker_summary,
                "disabled_tick_summary": disabled_summary,
                "fake_csv_transport_calls": len(fetched),
                "fake_csv_bytes": len(CSV_BYTES),
                "raw_receipt_counts": {
                    "total": receipt["total"],
                    "added": receipt["added"],
                    "updated": receipt["updated"],
                },
                "raw_snapshot_title": raw_snapshot["title"],
                "receipt_preserved_after_disable": final_receipt == receipt_before_disable,
                "schedule_revision_after_disable": final_schedule.revision,
            }
            context.close()
            browser.close()

        assert not REPORT["javascript_errors"], REPORT["javascript_errors"]
        assert not fixture.REPORT["unexpected_http"], fixture.REPORT["unexpected_http"]
        assert not fixture.REPORT["unexpected_external_requests"], fixture.REPORT["unexpected_external_requests"]
        assert fixture.REPORT["provider_attempts"] == 0, fixture.REPORT["provider_attempts"]
        assert counts["admin_products_get"] == 4, counts
        assert counts["schedule_post"] == 2, counts
        assert counts["unexpected_local_writes"] == 0, counts
        assert counts["local_routes"].get(f"GET {page_path}") == 4, counts
        assert counts["local_routes"].get(f"POST {schedule_post_path}") == 2, counts
        assert len(post_log) == 2
        assert not price_calls

        REPORT["route_counts"] = {
            **counts,
            "schedule_post_fields": post_log,
            "browser_admin_writes": len(post_log),
            "csrf_verified_posts": sum(bool(post["csrf_present"]) for post in post_log),
        }
        REPORT["negative_cases"] = {
            "GET_did_not_create_schedule_or_run": True,
            "enable_POST_did_not_start_worker_or_create_queue_receipt": True,
            "only_explicit_scheduler_call_read_fake_source": True,
            "price_feed_entry_point_not_called": True,
            "disabled_schedule_worker_tick_created_no_new_run": True,
            "seller_imported_product_unchanged": True,
            "local_market_product_unchanged": True,
            "marketplace_listing_unchanged": True,
        }
        REPORT["provider_write_counters"] = {
            "python_external_network_attempts": fixture.REPORT["provider_attempts"],
            "browser_external_request_attempts": len(fixture.REPORT["unexpected_external_requests"]),
            "fake_catalog_transport_reads": len(fetched),
            "price_feed_calls": len(price_calls),
            "admin_schedule_posts": len(post_log),
            "schedule_audit_rows": schedule_audit_count,
            "manual_queue_audit_rows": manual_queue_audit_count,
            "scheduled_refresh_receipts": 1,
            "scheduled_run_extensions": 1,
            "supplier_product_rows_updated": 1,
            "seller_imported_product_changes": 0,
            "local_market_product_changes": 0,
            "marketplace_listing_changes": 0,
        }
        REPORT["status"] = "passed"
        report_result = REPORT
        REPORT_PATH.write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(REPORT, ensure_ascii=False), flush=True)
    except Exception as exc:
        REPORT["status"] = "failed"
        REPORT["failure"] = {"type": type(exc).__name__, "message": str(exc)[:800]}
        REPORT["unexpected_external_requests"] = fixture.REPORT["unexpected_external_requests"]
        REPORT["unexpected_http"] = fixture.REPORT["unexpected_http"]
        REPORT["provider_write_counters"] = {
            "python_external_network_attempts": fixture.REPORT["provider_attempts"],
            "browser_external_request_attempts": len(fixture.REPORT["unexpected_external_requests"]),
            "admin_schedule_posts": counts["schedule_post"],
        }
        REPORT_PATH.write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        raise
    finally:
        fixture.server.shutdown()
        fixture.TEMP.cleanup()

    return report_result


if __name__ == "__main__":
    run()
