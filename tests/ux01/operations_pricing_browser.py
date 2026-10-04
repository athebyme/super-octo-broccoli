"""Offline visual and interaction matrix for UX-01 operations and pricing.

The fixture serves real Flask/Jinja pages from the selected source with a new
temporary SQLite database, synthetic seller rows, and an HTTP bridge that
allows only local reads and hash-pinned static assets. It never connects to a
marketplace or executes an application write from the browser.

Run with UX01_OPERATIONS_PRICING_SOURCE=worktree or ba63371 and provide
UX01_OPERATIONS_PRICING_ARTIFACTS / UX01_OPERATIONS_PRICING_REPORT.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import unquote, urlsplit

import requests
from jinja2 import ChoiceLoader, DictLoader
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ASSETS = ROOT / "tests/ozon_release/assets"
SOURCE = os.environ.get("UX01_OPERATIONS_PRICING_SOURCE", "worktree").strip()
if SOURCE not in {"worktree", "ba63371"}:
    raise ValueError("UX01_OPERATIONS_PRICING_SOURCE must be worktree or ba63371")
_DEFAULT_WIDTHS = (320, 390, 768, 1024, 1280, 1440)
_WIDTHS_INPUT = os.environ.get("UX01_OPERATIONS_PRICING_WIDTHS", "").strip()
WIDTHS = tuple(int(value.strip()) for value in _WIDTHS_INPUT.split(",") if value.strip()) \
    if _WIDTHS_INPUT else _DEFAULT_WIDTHS
if not WIDTHS or any(width < 1 for width in WIDTHS):
    raise ValueError("UX01_OPERATIONS_PRICING_WIDTHS must contain positive pixel widths")
OWNED_TEMPLATES = (
    "templates/base.html",
    "templates/bulk_edit_history.html",
    "templates/bulk_edit_history_detail.html",
    "templates/marketplace_operations.html",
    "templates/marketplace_operation_detail.html",
    "templates/prices_dashboard.html",
    "templates/prices_change.html",
    "templates/prices_settings.html",
    "templates/prices_history.html",
    "templates/prices_batch_detail.html",
    "templates/pricing_settings.html",
    "templates/price_monitor_settings.html",
    "templates/suspicious_price_changes.html",
    "templates/marketplace_commercial.html",
    "templates/marketplace_commercial_detail.html",
    "templates/marketplace_commercial_classic.html",
    "templates/marketplace_commercial_detail_classic.html",
    "templates/partials/operations_workspace_history_nav.html",
    "templates/partials/operations_workspace_status_note.html",
    "templates/partials/operations_workspace_wb_result.html",
    "templates/partials/pricing_workspace_nav.html",
    "templates/partials/pricing_workspace_ozon_facts.html",
    "templates/partials/ozon_commercial_dialogs.html",
)
OWNED_STATIC = (
    "static/operations-workspace.css",
    "static/pricing-workspace.css",
    "static/ozon-commercial.js",
)

TEMP = tempfile.TemporaryDirectory(prefix="ux01-operations-pricing-")
TEMP_PATH = Path(TEMP.name)
ARTIFACTS = Path(os.environ.get(
    "UX01_OPERATIONS_PRICING_ARTIFACTS",
    str(TEMP_PATH / "artifacts"),
))
ARTIFACTS.mkdir(parents=True, exist_ok=True)
REPORT_PATH = Path(os.environ.get(
    "UX01_OPERATIONS_PRICING_REPORT",
    str(ARTIFACTS / f"{SOURCE}-report.json"),
))
REPORT = {
    "status": "running",
    "source": SOURCE,
    "scope": "synthetic_full_flask_jinja_operations_pricing",
    "database": "disposable_sqlite",
    "network_policy": "loopback_reads_and_hash_pinned_assets_only",
    "widths": list(WIDTHS),
    "themes": ["light", "dark"],
    "layouts": [],
    "pages": [],
    "checks": [],
    "interactions": [],
    "api_reads": [],
    "price_initialization": [],
    "history_scenario_checks": [],
    "history_domain_sql_writes": [],
    "history_domain_state": {},
    "request_failures": [],
    "writes": [],
    "browser_mutations": [],
    "blocked_writes": [],
    "browser_writes": [],
    "actual_theme_assertions": [],
    "javascript_errors": [],
    "console_errors": [],
    "unexpected_http": [],
    "unexpected_external_requests": [],
    "provider_attempts": 0,
    "source_hashes": {},
    "pinned_asset_hashes": {},
    "after_failures": [],
}

os.environ.update({
    "DATABASE_URL": "sqlite:///" + str(TEMP_PATH / "seller-hub.sqlite"),
    "SKIP_SCHEDULER": "1",
    "SECRET_KEY": "ux01-synthetic-operations-pricing-only",
    "ENCRYPTION_KEY": base64.urlsafe_b64encode(b"p" * 32).decode(),
    "MARKETPLACE_OZON_ENABLED": "1",
    "MARKETPLACE_OZON_PUBLICATION_ENABLED": "0",
    "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED": "0",
    "MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED": "0",
    "OZON_RATE_LIMIT_DIR": str(TEMP_PATH / "limits"),
})


def no_provider_network(*_args, **_kwargs):
    REPORT["provider_attempts"] += 1
    raise AssertionError("Provider network is forbidden in this fixture")


requests.sessions.Session.request = no_provider_network
socket.create_connection = no_provider_network


def source_bytes(path: str) -> bytes:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe source path: {path}")
    if SOURCE == "worktree":
        return (ROOT / str(relative)).read_bytes()
    return subprocess.check_output(
        ["git", "show", f"{SOURCE}:{relative.as_posix()}"],
        cwd=ROOT,
        stderr=subprocess.DEVNULL,
    )


def add_source_overrides(app) -> None:
    templates = {}
    for path in OWNED_TEMPLATES:
        try:
            payload = source_bytes(path)
        except (FileNotFoundError, subprocess.CalledProcessError):
            REPORT["source_hashes"][path] = None
            continue
        REPORT["source_hashes"][path] = hashlib.sha256(payload).hexdigest()
        templates[path.removeprefix("templates/")] = payload.decode("utf-8")
    for path in OWNED_STATIC:
        try:
            payload = source_bytes(path)
        except (FileNotFoundError, subprocess.CalledProcessError):
            REPORT["source_hashes"][path] = None
            continue
        REPORT["source_hashes"][path] = hashlib.sha256(payload).hexdigest()
    app.jinja_env.loader = ChoiceLoader([
        DictLoader(templates),
        app.jinja_env.loader,
    ])
    app.jinja_env.cache.clear()


def seed_synthetic_rows(app) -> dict:
    from models import (
        BulkEditHistory,
        CardEditHistory,
        Marketplace,
        MarketplaceCommercialProposal,
        MarketplaceOperation,
        PriceChangeBatch,
        PriceChangeItem,
        PriceHistory,
        PriceMonitorSettings,
        Product,
        Seller,
        SellerMarketplaceAccount,
        SuspiciousPriceChange,
        db,
    )
    from services.wb_enrichment_reconciliation import schedule_history_reconciliation
    from tests.ozon_release.seed import seed

    fixture = seed(app)
    with app.app_context():
        now = datetime.utcnow()
        seller_id = fixture["seller_id"]
        ozon = Marketplace.query.filter_by(code="ozon").one()
        wb = Marketplace.query.filter_by(code="wb").first()
        if wb is None:
            wb = Marketplace(
                code="wb", name="Wildberries", adapter_code="wb", is_active=True,
            )
            db.session.add(wb)
            db.session.flush()

        product = Product(
            seller_id=seller_id,
            nm_id=990000101,
            vendor_code="UX01-PRICE-101",
            title="Синтетический товар для проверки расчёта цен Wildberries",
            brand="Synthetic",
            object_name="Тестовый товар",
            price=1500,
            discount_price=1200,
            supplier_price=1000,
            wb_price=1500,
            wb_discount=20,
            wb_discounted_price=1200,
            quantity=12,
            is_active=True,
        )
        db.session.add(product)
        db.session.flush()

        nullable_price_product = Product(
            seller_id=seller_id,
            nm_id=990000102,
            vendor_code="UX01-PRICE-102",
            title="Синтетический товар без закупочной цены",
            brand="Synthetic",
            object_name="Тестовый товар",
            price=0,
            discount_price=0,
            supplier_price=None,
            wb_price=0,
            wb_discount=0,
            wb_discounted_price=0,
            quantity=0,
            is_active=True,
        )
        db.session.add(nullable_price_product)
        db.session.flush()

        bulk = BulkEditHistory(
            seller_id=seller_id,
            operation_type="update_characteristic",
            operation_params={"field": "brand", "value": "Synthetic"},
            description="UX-01 synthetic batch with row errors",
            total_products=2,
            success_count=0,
            error_count=2,
            errors_details=[
                {"product_id": 990000101, "error": "Synthetic row error one"},
                {"product_id": 990000102, "reason": "supplier_photo_source_drift",
                 "error": "Synthetic row error two"},
            ],
            status="completed",
            wb_synced=False,
            created_at=now,
            completed_at=now,
        )
        db.session.add(bulk)
        db.session.flush()

        batch = PriceChangeBatch(
            seller_id=seller_id,
            name="UX-01 synthetic price review",
            description="Synthetic baseline and proposed seller prices",
            change_type="fixed",
            change_value=25,
            status="pending_review",
            has_safe_changes=True,
            total_items=1,
            safe_count=1,
            applied_count=0,
            failed_count=0,
            created_at=now,
        )
        db.session.add(batch)
        db.session.flush()
        db.session.add(PriceChangeItem(
            batch_id=batch.id,
            product_id=product.id,
            nm_id=product.nm_id,
            vendor_code=product.vendor_code,
            product_title=product.title,
            old_price=1500,
            new_price=1525,
            price_change_amount=25,
            price_change_percent=1.67,
            safety_level="safe",
            status="pending",
        ))

        history = PriceHistory(
            product_id=product.id,
            seller_id=seller_id,
            old_price=1500,
            new_price=1200,
            price_change_percent=-20,
            created_at=now,
        )
        db.session.add(history)
        db.session.flush()
        db.session.add(SuspiciousPriceChange(
            price_history_id=history.id,
            product_id=product.id,
            seller_id=seller_id,
            change_type="price",
            old_value=1500,
            new_value=1200,
            change_percent=-20,
            threshold_percent=10,
            is_reviewed=False,
            created_at=now,
        ))
        db.session.add(PriceMonitorSettings(
            seller_id=seller_id,
            is_enabled=True,
            monitor_prices=True,
            monitor_stocks=True,
        ))

        listing = db.session.get(
            __import__("models").MarketplaceListing,
            fixture["listing_ids"][0],
        )
        listing.title = "Синтетическая карточка Ozon для проверки длинных значений UX-01"
        listing.price_summary_json = json.dumps({
            "available": True,
            "currency": "RUB",
            "values": {
                "price": "1250",
                "old_price": "1500",
                "marketing_seller_price": "1200",
            },
        })

        operation = MarketplaceOperation(
            seller_id=seller_id,
            marketplace_id=ozon.id,
            account_id=fixture["account_id"],
            listing_id=listing.id,
            operation_kind="price_update",
            status="uncertain",
            idempotency_key="ux01-synthetic-price-operation-0001",
            request_fingerprint="c" * 64,
            contract_version="ux01-synthetic-contract-v1",
            request_summary_json=json.dumps({
                "offer_id": listing.offer_id,
                "before": {"price": "1250"},
                "proposed": {"price": "1150"},
                "warehouse_id": None,
            }),
            quota_snapshot_json="{}",
            item_results_json="[]",
            error_code="synthetic_uncertain_result",
            error_message="Синтетический результат требует сверки; записи провайдеру не было.",
            attempt_count=1,
            poll_count=2,
            reconcile_count=1,
            external_task_id="ux01-synthetic-task",
            submitted_at=now,
            next_poll_at=now,
            created_at=now,
        )
        db.session.add(operation)
        proposal = MarketplaceCommercialProposal(
            seller_id=seller_id,
            marketplace_id=ozon.id,
            account_id=fixture["account_id"],
            listing_id=listing.id,
            proposal_kind="price",
            source="user",
            status="pending_review",
            idempotency_key="ux01-synthetic-proposal-0001",
            request_fingerprint="d" * 64,
            contract_version="ux01-synthetic-contract-v1",
            baseline_fingerprint="e" * 64,
            proposed_fingerprint="f" * 64,
            baseline_state_json=json.dumps({"price": "1250", "old_price": "1500"}),
            proposed_state_json=json.dumps({"price": "1150"}),
            guardrails_json=json.dumps({"direction": "decrease", "change_pct": 8}),
            created_by_user_id=db.session.get(
                __import__("models").User,
                db.session.get(__import__("models").Seller, seller_id).user_id,
            ).id,
            created_at=now,
        )
        db.session.add(proposal)

        history_batch = None
        history_row_ids = []
        history_product_ids = []
        history_owned_product_ids = []
        foreign_history_product_id = None
        rollback_history_batch = None
        rollback_history_product = None
        rollback_history_row = None
        if SOURCE == "worktree":
            foreign_account = db.session.get(
                SellerMarketplaceAccount, fixture["foreign_account_id"],
            )
            foreign_seller = db.session.get(Seller, foreign_account.seller_id)
            history_batch = BulkEditHistory(
                seller_id=seller_id,
                operation_type="update_quantity",
                operation_params={"field": "quantity"},
                description="R10 synthetic batch31 mixed WB row outcomes",
                total_products=31,
                success_count=29,
                error_count=1,
                errors_details=None,
                status="in_progress",
                wb_synced=False,
                created_at=now + timedelta(seconds=1),
                completed_at=None,
                duration_seconds=None,
            )
            db.session.add(history_batch)
            db.session.flush()
            for index in range(31):
                # The current unprocessed pending marker is last in the
                # durable row order; the foreign conflict is a deliberately
                # mismatched legacy-history boundary immediately before it.
                is_foreign = index == 29
                owner_id = foreign_seller.id if is_foreign else seller_id
                history_product = Product(
                    seller_id=owner_id,
                    nm_id=990001000 + index,
                    vendor_code=(
                        "R10-FOREIGN-HISTORY-SECRET"
                        if is_foreign else f"R10-HISTORY-{index:02d}"
                    ),
                    title=(
                        "Private foreign history product title"
                        if is_foreign else f"Синтетический товар истории {index + 1:02d}"
                    ),
                    brand="R10 Synthetic",
                    object_name="Read-only fixture",
                    quantity=201 + index,
                    is_active=False,
                    photos_json="[]",
                    sizes_json="[]",
                    characteristics_json="[]",
                )
                db.session.add(history_product)
                db.session.flush()
                if is_foreign:
                    foreign_history_product_id = history_product.id
                else:
                    history_owned_product_ids.append(history_product.id)
                history_product_ids.append(history_product.id)

                status = (
                    "success" if index < 25 else
                    "failed" if index == 25 else
                    "submitted" if index == 26 else
                    "uncertain" if index == 27 else
                    "partial" if index == 28 else
                    "conflict" if index == 29 else
                    "pending"
                )
                history_row = CardEditHistory(
                    product_id=history_product.id,
                    # Legacy history can outlive or contradict its linked
                    # product tenant. The owning batch's seller scope is the
                    # tested boundary; the current route hides foreign details.
                    seller_id=seller_id,
                    bulk_edit_id=history_batch.id,
                    action="update",
                    changed_fields=["quantity"],
                    snapshot_before={"quantity": 200 + index},
                    snapshot_after={"quantity": 201 + index},
                    wb_synced=status == "success",
                    wb_sync_status=status,
                    created_at=now + timedelta(seconds=3, microseconds=index),
                    user_comment=(
                        "Private conflict details must remain hidden"
                        if is_foreign else "Synthetic local history fixture; no WB request."
                    ),
                    wb_error_message=(
                        "Private foreign conflict detail"
                        if is_foreign else
                        "Synthetic failure; no request was sent."
                        if status == "failed" else None
                    ),
                )
                if status in {"pending", "submitted", "uncertain", "partial"}:
                    schedule_history_reconciliation(
                        history_row,
                        status=status,
                        now=now + timedelta(seconds=3, microseconds=index),
                    )
                elif status == "conflict":
                    history_row.wb_synced = False
                    history_row.wb_reconcile_code = "synthetic_history_conflict"
                    history_row.wb_reconciled_at = now + timedelta(seconds=3)
                db.session.add(history_row)
                db.session.flush()
                history_row_ids.append(history_row.id)

            history_batch.errors_details = [{
                "product_id": int(history_product_ids[25]),
                "error": "Synthetic failed row; no WB request was made.",
            }]

            rollback_history_product = Product(
                seller_id=seller_id,
                nm_id=990002001,
                vendor_code="R10-QUANTITY-ROLLBACK",
                title="Синтетический quantity-only rollback",
                brand="R10 Synthetic",
                object_name="Read-only fixture",
                quantity=8,
                is_active=False,
                photos_json="[]",
                sizes_json="[]",
                characteristics_json="[]",
            )
            db.session.add(rollback_history_product)
            db.session.flush()
            rollback_history_batch = BulkEditHistory(
                seller_id=seller_id,
                operation_type="update_quantity",
                operation_params={"field": "quantity"},
                description="R10 completed quantity-only rollback fixture",
                total_products=1,
                success_count=1,
                error_count=0,
                errors_details=None,
                status="completed",
                wb_synced=True,
                created_at=now + timedelta(seconds=4),
                completed_at=now + timedelta(seconds=5),
                duration_seconds=1.0,
            )
            db.session.add(rollback_history_batch)
            db.session.flush()
            rollback_history_row = CardEditHistory(
                product_id=rollback_history_product.id,
                seller_id=seller_id,
                bulk_edit_id=rollback_history_batch.id,
                action="update",
                changed_fields=["quantity"],
                snapshot_before={"quantity": 7},
                snapshot_after={"quantity": 8},
                wb_synced=True,
                wb_sync_status="success",
                created_at=now + timedelta(seconds=6),
                user_comment="Synthetic quantity-only snapshot; no WB request.",
            )
            db.session.add(rollback_history_row)
            db.session.flush()

        db.session.commit()
        return {
            **fixture,
            "bulk_id": bulk.id,
            "batch_id": batch.id,
            "operation_id": operation.id,
            "proposal_id": proposal.id,
            "product_id": product.id,
            "listing_id": listing.id,
            **({
                "history_batch31_id": history_batch.id,
                "history_batch31_row_ids": history_row_ids,
                "history_batch31_product_ids": history_product_ids,
                "history_batch31_owned_product_ids": history_owned_product_ids,
                "history_batch31_foreign_product_id": foreign_history_product_id,
                "quantity_rollback_batch_id": rollback_history_batch.id,
                "quantity_rollback_product_id": rollback_history_product.id,
                "quantity_rollback_history_id": rollback_history_row.id,
            } if history_batch is not None else {}),
        }


def load_pinned_assets() -> dict[str, dict]:
    manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    result = {}
    for url, item in manifest.items():
        payload = (ASSETS / item["file"]).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != item["sha256"]:
            raise AssertionError(f"Pinned browser asset changed: {url}")
        REPORT["pinned_asset_hashes"][url] = digest
        result[url] = {**item, "payload": payload}
    vue_path = ROOT / "static/vendor/vue-3.4.38.global.prod.js"
    if vue_path.exists():
        vue_url = "https://cdn.jsdelivr.net/npm/vue@3.4.38/dist/vue.global.prod.js"
        payload = vue_path.read_bytes()
        REPORT["pinned_asset_hashes"][vue_url] = hashlib.sha256(payload).hexdigest()
        result[vue_url] = {
            "payload": payload,
            "content_type": "application/javascript",
        }
    result["https://ozon-fixture.test/product.svg"] = {
        "payload": (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="72"'
            b'><rect width="100%" height="100%" fill="#f3f0ec"/>'
            b'<text x="50%" y="55%" text-anchor="middle" fill="#a34a2f">UX</text></svg>'
        ),
        "content_type": "image/svg+xml",
    }
    return result


def login_cookie(app, base_url: str) -> dict:
    from tests.ozon_release.seed import PASSWORD, USERNAME

    client = app.test_client()
    response = client.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD},
        follow_redirects=False,
    )
    if response.status_code not in (302, 303):
        raise AssertionError(("synthetic login failed", response.status_code))
    cookie = client.get_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
    if cookie is None:
        raise AssertionError("synthetic login did not create a session cookie")
    return {"name": cookie.key, "value": cookie.value, "url": base_url}


def _measure(page, *, path: str, width: int, theme: str, scale: int) -> dict:
    return page.evaluate(
        """({path, requestedTheme, width, scale}) => {
            const box = node => {
                if (!node) return null;
                const r = node.getBoundingClientRect();
                return {left:Math.round(r.left), right:Math.round(r.right),
                    width:Math.round(r.width), height:Math.round(r.height)};
            };
            const main = document.querySelector('.main-content');
            const content = document.querySelector('#main-content');
            const workspaces = Array.from(document.querySelectorAll(
                '.operations-workspace, .pricing-workspace'));
            const tables = Array.from(document.querySelectorAll(
                '.pricing-table-scroll, .operations-table-scroll, .overflow-x-auto'))
                .filter(node => node.querySelector('table'))
                .map(node => ({role:node.getAttribute('role'), label:node.getAttribute('aria-label'),
                    tabIndex:node.getAttribute('tabindex'), clientWidth:node.clientWidth,
                    scrollWidth:node.scrollWidth, overflowX:getComputedStyle(node).overflowX,
                    box:box(node)}));
            const root = document.documentElement;
            const offenders = Array.from(document.querySelectorAll('body *'))
                .map(node => ({node, r:node.getBoundingClientRect()}))
                .filter(({node, r}) => r.width > 0 && r.right > innerWidth + 1
                    && !node.closest('.sidebar'))
                .slice(0, 40)
                .map(({node, r}) => ({tag:node.tagName,
                    className:typeof node.className === 'string' ? node.className.slice(0, 90) : '',
                    right:Math.round(r.right), width:Math.round(r.width),
                    text:(node.innerText || '').slice(0, 72)}));
            const overflowSources = Array.from(document.querySelectorAll('body *'))
                .filter(node => node.scrollWidth > node.clientWidth + 2)
                .slice(0, 40)
                .map(node => {
                    const r = node.getBoundingClientRect(), style = getComputedStyle(node);
                    return {tag:node.tagName,
                        className:typeof node.className === 'string' ? node.className.slice(0, 100) : '',
                        left:Math.round(r.left), right:Math.round(r.right),
                        clientWidth:node.clientWidth, scrollWidth:node.scrollWidth,
                        overflowX:style.overflowX, position:style.position,
                        text:(node.innerText || '').slice(0, 80)};
                });
            const formulaForm = document.querySelector('.pricing-settings-form');
            const formulaFormGeometry = formulaForm ? {
                box:box(formulaForm), clientWidth:formulaForm.clientWidth,
                scrollWidth:formulaForm.scrollWidth,
                children:Array.from(formulaForm.children).map(node => {
                    const r=node.getBoundingClientRect(), style=getComputedStyle(node);
                    return {tag:node.tagName,
                        className:typeof node.className === 'string' ? node.className.slice(0, 100) : '',
                        left:Math.round(r.left), right:Math.round(r.right),
                        width:Math.round(r.width), clientWidth:node.clientWidth,
                        scrollWidth:node.scrollWidth, overflowX:style.overflowX,
                        text:(node.innerText || '').slice(0, 60)};
                }),
                outsideLocalScroll:Array.from(formulaForm.querySelectorAll('*'))
                    .filter(node => !node.closest('.pricing-table-scroll'))
                    .map(node => ({node, r:node.getBoundingClientRect()}))
                    .filter(({r}) => r.width > 0 && r.right > innerWidth + 1)
                    .slice(0, 20)
                    .map(({node,r}) => ({tag:node.tagName,
                        className:typeof node.className === 'string' ? node.className.slice(0, 100) : '',
                        left:Math.round(r.left),right:Math.round(r.right),width:Math.round(r.width),
                        text:(node.innerText || '').slice(0,60)}))
            } : null;
            return {path, requestedTheme, actualTheme:root.dataset.theme, width, innerWidth,
                textScale:scale, rootClientWidth:root.clientWidth, rootScrollWidth:root.scrollWidth,
                rootOverflow:root.scrollWidth > root.clientWidth + 1,
                main:box(main), content:box(content),
                pageOverflowX:getComputedStyle(root).overflowX,
                bodyOverflowX:getComputedStyle(document.body).overflowX,
                workspaces:workspaces.map(box), tables, offenders, overflowSources, formulaFormGeometry,
                themeClass:root.className};
        }""",
        {"path": path, "requestedTheme": theme, "width": width, "scale": scale},
    )


def _after_failures(row: dict) -> list[str]:
    failures = []
    if row["rootOverflow"]:
        failures.append("root_horizontal_overflow")
    if row["pageOverflowX"] == "hidden" or row["bodyOverflowX"] == "hidden":
        failures.append("page_wide_overflow_hidden")
    if row["requestedTheme"] != row["actualTheme"]:
        failures.append("requested_theme_not_applied")
    if row["width"] <= 768:
        for table in row["tables"]:
            if table["scrollWidth"] > table["clientWidth"] + 1:
                if table["role"] != "region" or not table["label"] or table["tabIndex"] != "0":
                    failures.append("unlabelled_or_unfocusable_local_table_scroll")
                    break
    return failures


def _settle_main_geometry(page) -> None:
    page.evaluate("""() => {
        const node = document.querySelector('.main-content');
        if (node) {
            delete node.dataset.ux01StableLeft;
            delete node.dataset.ux01StableCount;
        }
    }""")
    page.wait_for_function("""() => {
        const node = document.querySelector('.main-content');
        if (!node) return false;
        const running = node.getAnimations().some(a => a.playState === 'running');
        const left = Math.round(node.getBoundingClientRect().left);
        const prior = Number(node.dataset.ux01StableLeft);
        const count = Number(node.dataset.ux01StableCount || 0);
        node.dataset.ux01StableLeft = String(left);
        node.dataset.ux01StableCount = String(prior === left ? count + 1 : 1);
        return !running && Number(node.dataset.ux01StableCount) >= 3;
    }""")


def _set_text_scale(page, scale: int) -> None:
    page.evaluate("""scale => {
        window.__ux01FontRestores = [];
        if (scale !== 200) return;
        for (const node of document.querySelectorAll('body *')) {
            const computed = parseFloat(getComputedStyle(node).fontSize);
            if (!Number.isFinite(computed) || computed <= 0) continue;
            window.__ux01FontRestores.push([
                node,
                node.style.getPropertyValue('font-size'),
                node.style.getPropertyPriority('font-size')
            ]);
            node.style.setProperty('font-size', `${computed * 2}px`, 'important');
        }
    }""", scale)


def _restore_text_scale(page) -> None:
    page.evaluate("""() => {
        for (const [node, value, priority] of window.__ux01FontRestores || []) {
            if (!node.isConnected) continue;
            if (value) node.style.setProperty('font-size', value, priority);
            else node.style.removeProperty('font-size');
        }
        delete window.__ux01FontRestores;
    }""")


def _keyboard_nav_check(page, selector: str) -> dict:
    target = page.locator(selector).first
    if target.count() == 0:
        return {"passed": False, "reason": "shared_navigation_missing"}
    target.evaluate("node => { window.__ux01FocusTarget = node; node.focus(); }")
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    result = page.evaluate("""() => {
        const target = window.__ux01FocusTarget;
        if (!target) return {passed:false, reason:'focus_target_missing'};
        const nav = target.closest('nav');
        const style = getComputedStyle(target);
        const parse = value => {
            const raw = String(value);
            const match = raw.slice(raw.indexOf('(') + 1, raw.lastIndexOf(')'));
            if (!raw.includes('(') || !raw.includes(')')) return null;
            const channels = match.split(',').map(part => parseFloat(part.trim()));
            return {r:channels[0], g:channels[1], b:channels[2], a:channels.length > 3 ? channels[3] : 1};
        };
        let background = {r:255,g:255,b:255,a:0};
        for (let node = target; node && background.a < 0.999; node = node.parentElement) {
            const layer = parse(getComputedStyle(node).backgroundColor);
            if (!layer || layer.a <= 0) continue;
            background = {
                r:layer.r*layer.a + background.r*(1-layer.a),
                g:layer.g*layer.a + background.g*(1-layer.a),
                b:layer.b*layer.a + background.b*(1-layer.a),
                a:layer.a + background.a*(1-layer.a)
            };
        }
        const outline = parse(style.outlineColor);
        const blend = color => color && ({
            r:color.r*color.a + background.r*(1-color.a),
            g:color.g*color.a + background.g*(1-color.a),
            b:color.b*color.a + background.b*(1-color.a)
        });
        const luminance = color => {
            const channel = value => {
                const linear = value / 255;
                return linear <= 0.04045 ? linear / 12.92 : ((linear + 0.055) / 1.055) ** 2.4;
            };
            return 0.2126*channel(color.r) + 0.7152*channel(color.g) + 0.0722*channel(color.b);
        };
        const foreground = blend(outline);
        const first = foreground ? luminance(foreground) : 0;
        const second = luminance(background);
        const contrast = (Math.max(first,second)+0.05)/(Math.min(first,second)+0.05);
        const rect = target.getBoundingClientRect();
        const navRect = nav?.getBoundingClientRect();
        const navUnclipped = !!nav && nav.scrollWidth <= nav.clientWidth + 1
            && navRect.left >= -1 && navRect.right <= innerWidth + 1;
        const targetUnclipped = rect.left >= -1 && rect.right <= innerWidth + 1;
        const keyboardFocused = document.activeElement === target;
        const outlineWidth = parseFloat(style.outlineWidth) || 0;
        const passed = keyboardFocused && style.outlineStyle === 'solid'
            && outlineWidth >= 2 && contrast >= 3 && rect.height >= 44
            && navUnclipped && targetUnclipped;
        return {passed, keyboardFocused, outlineStyle:style.outlineStyle,
            outlineWidth, outlineColor:style.outlineColor, contrast:Math.round(contrast*100)/100,
            targetHeight:Math.round(rect.height), navUnclipped, targetUnclipped,
            label:(target.innerText || '').trim()};
    }""")
    return result


def main() -> None:
    from seller_platform import app
    from sqlalchemy import event
    from models import BulkEditHistory, CardEditHistory, Product, Seller, db
    from tests.ozon_release.seed import USERNAME

    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SESSION_COOKIE_SECURE=False,
        MARKETPLACE_OZON_ENABLED=True,
        MARKETPLACE_OZON_PUBLICATION_ENABLED=False,
        MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
        MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=False,
    )
    add_source_overrides(app)
    fixture = seed_synthetic_rows(app)
    history_sql = {"active": False, "writes": []}
    if SOURCE == "worktree":
        def observe_history_sql_write(
            _connection, _cursor, statement, _parameters, _context,
            _executemany,
        ):
            if not history_sql["active"]:
                return
            verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
                history_sql["writes"].append({"verb": verb})

        with app.app_context():
            event.listen(db.engine, "before_cursor_execute", observe_history_sql_write)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    base_origin = urlsplit(base)
    assets = load_pinned_assets()

    def capture_request_failure(request, *, page_label: str, page_theme: str) -> None:
        parsed = urlsplit(request.url)
        is_local = parsed.hostname == base_origin.hostname and parsed.port == base_origin.port
        failure_text = str(request.failure or "unknown")
        failure_text = re.sub(r"https?://[^\s\"'<>]+", "<url>", failure_text)
        failure_text = re.sub(r"[?&][^\s\"'<>]*", "?<redacted>", failure_text)
        REPORT["request_failures"].append({
            "page": page_label,
            "theme": page_theme,
            "method": request.method,
            "path": parsed.path if is_local else "<external>",
            "resource_type": request.resource_type,
            "failure_text": failure_text[:240],
        })

    def bridge(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.hostname == "127.0.0.1" and parsed.port == server.server_port:
            if request.method not in {"GET", "HEAD"}:
                row = {"method": request.method, "path": parsed.path}
                REPORT["browser_mutations"].append(row)
                REPORT["blocked_writes"].append(row)
                REPORT["browser_writes"].append(row)
                REPORT["unexpected_http"].append(row)
                route.abort()
                return
            if parsed.path.startswith("/api/") or parsed.path == "/prices/api/products":
                REPORT["api_reads"].append({"method": request.method, "path": parsed.path})
            if parsed.path.startswith("/static/"):
                try:
                    payload = source_bytes(unquote(parsed.path.removeprefix("/")))
                except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
                    route.fulfill(status=404, body=b"missing synthetic asset")
                    return
                REPORT["source_hashes"][parsed.path.removeprefix("/")] = hashlib.sha256(payload).hexdigest()
                content_type = "text/css" if parsed.path.endswith(".css") else "application/javascript"
                if parsed.path.endswith((".svg", ".png", ".webp")):
                    content_type = "image/svg+xml" if parsed.path.endswith(".svg") else "image/png"
                route.fulfill(status=200, body=payload, content_type=content_type)
                return
            route.continue_()
            return
        if request.url.startswith(("data:", "blob:", "about:")):
            route.continue_()
            return
        asset_url = request.url.split("?", 1)[0]
        asset = (
            assets.get(request.url)
            or assets.get(asset_url)
            or assets.get(asset_url.rstrip("/"))
        )
        if asset:
            route.fulfill(
                status=200,
                body=asset["payload"],
                content_type=asset["content_type"],
                headers={"access-control-allow-origin": "*"},
            )
            return
        REPORT["unexpected_external_requests"].append({
            "url": request.url,
            "method": request.method,
        })
        route.abort()

    def observe_response(response):
        parsed = urlsplit(response.url)
        if (parsed.hostname == "127.0.0.1" and parsed.port == server.server_port
                and response.status >= 400 and parsed.path != "/favicon.ico"):
            REPORT["unexpected_http"].append({
                "status": response.status,
                "method": response.request.method,
                "path": parsed.path,
            })

    pages = [
        ("wb_bulk_history", f"/bulk-history"),
        ("wb_bulk_detail", f"/bulk-history/{fixture['bulk_id']}"),
        ("ozon_operations", f"/marketplaces/operations/?account_id={fixture['account_id']}"),
        ("ozon_operation_detail", f"/marketplaces/operations/{fixture['operation_id']}"),
        ("wb_prices_dashboard", "/prices/?account_id=" + str(fixture["account_id"])),
        ("wb_prices_change", "/prices/change"),
        ("wb_prices_settings", "/prices/settings"),
        ("wb_prices_history", "/prices/history"),
        ("wb_prices_batch", f"/prices/batch/{fixture['batch_id']}"),
        ("supplier_formula", "/pricing"),
        ("wb_price_monitor", "/price-monitor/settings"),
        ("wb_price_alerts", "/price-monitor/suspicious"),
        ("ozon_commercial_vue", f"/marketplaces/commercial/?account_id={fixture['account_id']}"),
        ("ozon_commercial_classic", f"/marketplaces/commercial/classic?account_id={fixture['account_id']}"),
        ("ozon_proposal_vue", f"/marketplaces/commercial/{fixture['proposal_id']}"),
        ("ozon_proposal_classic", f"/marketplaces/commercial/classic/{fixture['proposal_id']}"),
    ]
    page_filter = os.environ.get("UX01_OPERATIONS_PRICING_PAGES", "").strip()
    if page_filter:
        requested_pages = {label.strip() for label in page_filter.split(",") if label.strip()}
        available_pages = {label for label, _path in pages}
        unknown_pages = requested_pages - available_pages
        if unknown_pages:
            raise ValueError(f"Unknown UX-01 operations/pricing page labels: {sorted(unknown_pages)}")
        pages = [(label, path) for label, path in pages if label in requested_pages]
    report_error = None
    try:
        with sync_playwright() as playwright:
            launch = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            executable = os.environ.get("CHROMIUM_BIN")
            if executable:
                launch["executable_path"] = executable
            browser = playwright.chromium.launch(**launch)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                service_workers="block",
                reduced_motion="reduce",
            )
            context.route("**/*", bridge)
            cookie = login_cookie(app, base)
            context.add_cookies([cookie])

            for label, path in pages:
                for theme in ("light", "dark"):
                    page = context.new_page()
                    page.set_default_timeout(12000)
                    page.add_init_script(
                        "if (location.origin === " + json.dumps(base) + ") {"
                        + "localStorage.setItem('sh-theme', " + json.dumps(theme) + ");"
                        + "localStorage.setItem('sh-sidebar', 'closed');"
                        + ("localStorage.removeItem('price_change_selected');" if label == "wb_prices_change" else "")
                        + "}"
                    )
                    page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
                    page.on("console", lambda message: REPORT["console_errors"].append(message.text)
                            if message.type == "error" else None)
                    page.on("response", observe_response)
                    page.on("requestfailed", lambda request, page_label=label, page_theme=theme:
                            capture_request_failure(request, page_label=page_label, page_theme=page_theme))
                    price_product_requests = []
                    if label == "wb_prices_change":
                        def track_price_request(request):
                            parsed = urlsplit(request.url)
                            if (request.method == "GET" and parsed.hostname == base_origin.hostname
                                    and parsed.port == base_origin.port
                                    and parsed.path == "/prices/api/products"):
                                price_product_requests.append(request)

                        page.on("request", track_price_request)
                        with page.expect_response(
                            lambda candidate: candidate.request.method == "GET"
                            and urlsplit(candidate.url).hostname == base_origin.hostname
                            and urlsplit(candidate.url).port == base_origin.port
                            and urlsplit(candidate.url).path == "/prices/api/products",
                            timeout=12000,
                        ) as price_response_info:
                            response = page.goto(base + path, wait_until="domcontentloaded")
                        initial_price_response = price_response_info.value
                    else:
                        response = page.goto(base + path, wait_until="domcontentloaded")
                        initial_price_response = None
                    if not response or response.status != 200:
                        raise AssertionError((label, path, response.status if response else None))
                    page.wait_for_function("() => !!document.querySelector('.main-content') && !!document.querySelector('#main-content')")
                    _settle_main_geometry(page)
                    page.evaluate("document.fonts.ready")
                    actual_theme = page.evaluate("document.documentElement.dataset.theme")
                    REPORT["actual_theme_assertions"].append({
                        "page": label,
                        "requested": theme,
                        "actual": actual_theme,
                        "passed": actual_theme == theme,
                    })
                    if actual_theme != theme:
                        raise AssertionError({"page": label, "requested_theme": theme, "actual_theme": actual_theme})
                    REPORT["pages"].append({"label": label, "path": urlsplit(path).path,
                                            "status": response.status, "theme": actual_theme})

                    if label == "wb_prices_change":
                        page.wait_for_function("""() => {
                            const root = document.querySelector('.pricing-workspace');
                            const state = root && window.Alpine && Alpine.$data(root);
                            return !!state && state.loading === false;
                        }""", timeout=12000)
                        try:
                            api_payload = initial_price_response.json()
                        except Exception:
                            api_payload = None
                        api_products = api_payload.get("products") if isinstance(api_payload, dict) else None
                        api_products = api_products if isinstance(api_products, list) else []
                        state = page.evaluate("""() => {
                            const root = document.querySelector('.pricing-workspace');
                            const data = root && window.Alpine && Alpine.$data(root);
                            return data ? {
                                loading: data.loading,
                                productCount: Array.isArray(data.products) ? data.products.length : null,
                                selectedCount: Array.isArray(data.selectedIds) ? data.selectedIds.length : null,
                            } : {loading: null, productCount: null, selectedCount: null};
                        }""")
                        rendered_product_count = page.locator(".pricing-workspace tbody tr").count()
                        synthetic_products_exact = (
                            {product.get("vendor_code") for product in api_products if isinstance(product, dict)}
                            == {"UX01-PRICE-101", "UX01-PRICE-102"}
                            and len(api_products) == 2
                        )
                        price_row = {
                            "theme": theme,
                            "actual_theme": actual_theme,
                            "products_get_count": len(price_product_requests),
                            "http_status": initial_price_response.status,
                            "success": isinstance(api_payload, dict) and api_payload.get("success") is True,
                            "rendered_product_count": rendered_product_count,
                            "expected_product_count": 2,
                            "loading": state.get("loading"),
                            "selected_count": state.get("selectedCount"),
                            "synthetic_products_exact": synthetic_products_exact,
                        }
                        price_check_passed = (
                            actual_theme == theme
                            and price_row["products_get_count"] == 1
                            and price_row["http_status"] == 200
                            and price_row["success"] is True
                            and len(api_products) == price_row["expected_product_count"]
                            and rendered_product_count == price_row["expected_product_count"]
                            and state.get("loading") is False
                            and state.get("selectedCount") == 0
                            and synthetic_products_exact
                        )
                        REPORT["price_initialization"].append(price_row)
                        REPORT["checks"].append({
                            "name": "wb_price_change_initializes_once_and_renders_products",
                            "status": "passed" if price_check_passed else "failed",
                            "theme": theme,
                        })

                    if label == "wb_bulk_detail":
                        body = page.locator("body").inner_text()
                        REPORT["checks"].append({"page": label, "check": "two_row_errors_are_not_success",
                                                 "passed": "Завершено с ошибками" in body and "0 из 2" in body,
                                                 "scope": "after_acceptance" if SOURCE == "worktree" else "baseline_observation"})
                        row_error_check = page.evaluate("""() => {
                            const summary = document.querySelector('[data-operations-error-summary]');
                            const raw = document.querySelector('[data-operations-raw-errors]');
                            const row = summary?.querySelector('[data-operations-error-id="990000101"]');
                            const text = summary?.innerText || '';
                            const rawText = raw?.querySelector('pre')?.textContent || '';
                            return {
                                passed: !!summary && !!row && !summary.closest('details')
                                    && text.includes('Строка товара #990000101')
                                    && text.includes('Synthetic row error one')
                                    && text.includes('Строка товара #990000102')
                                    && text.includes('Synthetic row error two')
                                    && text.includes('Галерея поставщика изменилась после выбора; выбранные фотографии не отправлялись.')
                                    && !text.includes('supplier_photo_source_drift')
                                    && text.includes('Перед новым изменением проверьте актуальное состояние карточки в WB.')
                                    && !!raw && !raw.open && rawText.includes('Synthetic row error one')
                                    && rawText.includes('supplier_photo_source_drift'),
                                summaryText: text,
                                rawDetailsCollapsed: raw ? !raw.open : null,
                                rawPayloadContainsReason: rawText.includes('Synthetic row error one'),
                            };
                        }""")
                        REPORT["checks"].append({
                            "page": label,
                            "check": "error_reason_and_row_identity_visible_outside_collapsed_raw_details",
                            **row_error_check,
                            "scope": "after_acceptance" if SOURCE == "worktree" else "baseline_observation",
                        })
                        contrast = page.evaluate("""() => {
                            const text = document.querySelector('.operations-wb-result--dark-hero p');
                            if (!text) return {passed:false, reason:'hero_outcome_text_missing'};
                            const channels = value => {
                                const raw = String(value);
                                const values = raw.slice(raw.indexOf('(')+1, raw.lastIndexOf(')'))
                                    .split(',').map(part => parseFloat(part.trim()));
                                return raw.includes('(') && values.length >= 3
                                    ? {r:values[0],g:values[1],b:values[2],a:values.length > 3 ? values[3] : 1} : null;
                            };
                            const luminance = color => {
                                const channel = value => {
                                    const linear = value / 255;
                                    return linear <= 0.04045 ? linear / 12.92 : ((linear + 0.055) / 1.055) ** 2.4;
                                };
                                return 0.2126*channel(color.r) + 0.7152*channel(color.g) + 0.0722*channel(color.b);
                            };
                            let node = text;
                            let background = null;
                            while (node && !background) {
                                const candidate = channels(getComputedStyle(node).backgroundColor);
                                if (candidate && candidate.a >= 0.999) background = candidate;
                                node = node.parentElement;
                            }
                            const foreground = channels(getComputedStyle(text).color);
                            if (!foreground || !background) return {passed:false, reason:'hero_colors_unavailable'};
                            const a = luminance(foreground), b = luminance(background);
                            const ratio = (Math.max(a,b)+0.05)/(Math.min(a,b)+0.05);
                            return {passed:ratio >= 4.5, ratio:Math.round(ratio*100)/100,
                                foreground:getComputedStyle(text).color,
                                background:getComputedStyle(text.parentElement.parentElement).backgroundColor};
                        }""")
                        REPORT["checks"].append({
                            "page": label,
                            "check": "dark_hero_outcome_text_contrast",
                            **contrast,
                            "scope": "after_acceptance" if SOURCE == "worktree" else "baseline_observation",
                        })
                    if label == "ozon_proposal_classic":
                        body = page.locator("body").inner_text()
                        captions = [value.strip().casefold() for value in page.locator(".pricing-value-caption").all_inner_texts()]
                        REPORT["checks"].append({"page": label, "check": "unknown_currency_is_explicit",
                                                 "passed": "валюта неизвестна" in body and "снимок до предложения" in captions,
                                                 "scope": "after_acceptance" if SOURCE == "worktree" else "baseline_observation"})

                    if label == "wb_prices_change":
                        page.wait_for_function(
                            "() => document.body.innerText.includes('UX01-PRICE-102')",
                            timeout=12000,
                        )
                        page.locator('input[type="radio"][value="supplier_pricing"]').check()
                        page.wait_for_function("""() => Array.from(document.querySelectorAll('tbody tr')).some(row =>
                            row.innerText.includes('UX01-PRICE-102') && row.innerText.includes('нет данных'))""",
                            timeout=12000,
                        )
                        null_price_rows = page.locator("tbody tr").filter(has_text="UX01-PRICE-102")
                        positive_price_rows = page.locator("tbody tr").filter(has_text="UX01-PRICE-101")
                        REPORT["checks"].append({
                            "page": label,
                            "check": "nullable_supplier_price_and_confirmed_zero_are_visible",
                            "passed": null_price_rows.count() == 1
                                and "нет данных" in null_price_rows.inner_text()
                                and positive_price_rows.count() == 1
                                and "1\u00a0000 ₽" in positive_price_rows.inner_text(),
                            "scope": "after_acceptance" if SOURCE == "worktree" else "baseline_observation",
                        })

                    for width in WIDTHS:
                        page.set_viewport_size({"width": width, "height": 1000})
                        _settle_main_geometry(page)
                        page.evaluate("""() => new Promise(resolve => requestAnimationFrame(
                            () => requestAnimationFrame(resolve)))""")
                        for scale in ((100, 200) if width in (320, 390) else (100,)):
                            _set_text_scale(page, scale)
                            page.evaluate("""() => new Promise(resolve => requestAnimationFrame(
                                () => requestAnimationFrame(resolve)))""")
                            row = _measure(page, path=urlsplit(path).path, width=width, theme=theme, scale=scale)
                            _restore_text_scale(page)
                            row["page"] = label
                            REPORT["layouts"].append(row)
                            if SOURCE == "worktree":
                                failures = _after_failures(row)
                            if failures:
                                REPORT["after_failures"].append({
                                    "page": label, "width": width, "theme": theme,
                                    "text_scale": scale, "failures": failures,
                                })
                                if "root_horizontal_overflow" in failures and width in (320, 390) and scale == 200:
                                    page.screenshot(
                                        path=str(ARTIFACTS / f"{label}-{theme}-{width}-200-root-overflow.png"),
                                        full_page=True,
                                        animations="disabled",
                                    )
                        if width in (390, 1440) and label in {
                            "wb_bulk_detail", "ozon_operation_detail", "wb_prices_change",
                            "wb_prices_batch", "ozon_commercial_classic", "ozon_proposal_classic",
                        }:
                            page.screenshot(
                                path=str(ARTIFACTS / f"{label}-{theme}-{width}.png"),
                                full_page=True,
                                animations="disabled",
                            )

                    # Keyboard pass checks real Tab navigation, indicator contrast,
                    # touch target size, and whether the local nav clips its links.
                    nav_selector = ".operations-history-link, .pricing-workspace-link"
                    page.set_viewport_size({"width": 320, "height": 1000})
                    _settle_main_geometry(page)
                    focus = _keyboard_nav_check(page, nav_selector)
                    REPORT["interactions"].append({
                        "page": label,
                        "check": "keyboard_route_focus",
                        "passed": focus.get("passed") is True,
                        "scope": "after_acceptance" if SOURCE == "worktree" else "baseline_observation",
                        "result": focus,
                    })
                    page.close()
            if SOURCE == "worktree":
                from collections import Counter

                bulk_id = int(fixture["history_batch31_id"])
                expected_product_ids = [
                    int(value) for value in fixture["history_batch31_product_ids"]
                ]
                owned_product_ids = [
                    int(value) for value in fixture["history_batch31_owned_product_ids"]
                ]
                foreign_product_id = int(fixture["history_batch31_foreign_product_id"])
                rollback_batch_id = int(fixture["quantity_rollback_batch_id"])
                rollback_product_id = int(fixture["quantity_rollback_product_id"])
                rollback_history_id = int(fixture["quantity_rollback_history_id"])
                history_url = f"/bulk-history/{bulk_id}"
                rollback_history_url = f"/bulk-history/{rollback_batch_id}"

                def history_domain_counts():
                    return {
                        "bulk_edit_history": BulkEditHistory.query.filter(
                            BulkEditHistory.id.in_((bulk_id, rollback_batch_id)),
                            BulkEditHistory.seller_id == fixture["seller_id"],
                        ).count(),
                        "card_edit_history": CardEditHistory.query.filter(
                            CardEditHistory.bulk_edit_id.in_((bulk_id, rollback_batch_id)),
                            CardEditHistory.seller_id == fixture["seller_id"],
                        ).count(),
                        "products": Product.query.filter(
                            Product.id.in_(expected_product_ids + [rollback_product_id]),
                        ).count(),
                    }

                with app.app_context():
                    stored_history_rows = CardEditHistory.query.filter_by(
                        bulk_edit_id=bulk_id, seller_id=fixture["seller_id"],
                    ).order_by(CardEditHistory.created_at.asc(), CardEditHistory.id.asc()).all()
                    assert len(stored_history_rows) == 31
                    product_statuses = [
                        {
                            "product_id": int(row.product_id),
                            "wb_sync_status": row.wb_sync_status or "unknown",
                        }
                        for row in stored_history_rows
                    ]
                    status_counts = dict(sorted(Counter(
                        row["wb_sync_status"] for row in product_statuses
                    ).items()))
                    before_values = {
                        int(row.product_id): {
                            "before": int(row.snapshot_before["quantity"]),
                            "after": int(row.snapshot_after["quantity"]),
                            "status": row.wb_sync_status or "unknown",
                        }
                        for row in stored_history_rows
                    }
                    history_before = history_domain_counts()
                    batch = db.session.get(BulkEditHistory, bulk_id)
                    assert batch is not None
                    assert batch.total_products == 31
                    assert batch.status == "in_progress"
                    assert batch.success_count == 29
                    assert batch.error_count == 1
                    assert batch.completed_at is None
                    assert batch.can_revert() is False
                    operation_status = batch.status
                    operation_total_products = int(batch.total_products)
                    operation_success_count = int(batch.success_count)
                    operation_error_count = int(batch.error_count)
                    operation_completed_at = (
                        batch.completed_at.isoformat() if batch.completed_at else None
                    )
                    operation_duration_seconds = batch.duration_seconds
                    assert operation_success_count + operation_error_count == 30
                    assert operation_completed_at is None
                    assert operation_duration_seconds is None
                    pending_history_rows = [
                        row for row in stored_history_rows
                        if row.wb_sync_status == "pending"
                    ]
                    assert len(pending_history_rows) == 1
                    pending_unprocessed_product_id = int(
                        pending_history_rows[0].product_id,
                    )
                    assert pending_unprocessed_product_id == expected_product_ids[-1]
                    assert stored_history_rows[-1].id == pending_history_rows[0].id
                    quantity_rollback_contract_supported = any(
                        row.supports_safe_revert()
                        for row in stored_history_rows
                    )
                    assert quantity_rollback_contract_supported is False
                    assert before_values[expected_product_ids[-1]]["status"] == "pending"

                history_sql["active"] = True
                history_page = context.new_page()
                history_page.set_default_timeout(12000)
                history_page.on("pageerror", lambda error: REPORT["javascript_errors"].append(str(error)))
                history_page.on("console", lambda message: REPORT["console_errors"].append(message.text)
                                if message.type == "error" else None)
                history_page.on("response", observe_response)
                history_page.on("requestfailed", lambda request, page_label="r10_history", page_theme="light":
                                capture_request_failure(request, page_label=page_label, page_theme=page_theme))
                history_response = history_page.goto(
                    base + history_url, wait_until="domcontentloaded",
                )
                assert history_response and history_response.status == 200
                assert history_response.request.method == "GET"
                history_page.wait_for_function(
                    "() => !!document.querySelector('#main-content h1')",
                )
                history_heading = history_page.locator("#main-content h1").inner_text().strip()
                assert history_heading == "R10 synthetic batch31 mixed WB row outcomes"
                aggregate_cards = history_page.locator(
                    ".operations-stats-grid > div",
                ).evaluate_all("""cards => cards.map(card => {
                    const paragraphs = Array.from(card.querySelectorAll('p'));
                    return {
                        label: paragraphs[0]?.innerText.trim() || '',
                        value: paragraphs[1]?.innerText.trim() || '',
                    };
                })""")
                aggregate_by_label = {
                    row["label"]: row["value"] for row in aggregate_cards
                }
                assert aggregate_by_label == {
                    "Всего товаров": "31",
                    "Обработано без ошибки": "29",
                    "Ошибок": "1",
                    "Без общей ошибки": "94%",
                }, aggregate_cards
                aggregate_map = {
                    "total_products": int(aggregate_by_label["Всего товаров"]),
                    "success_count": int(aggregate_by_label["Обработано без ошибки"]),
                    "error_count": int(aggregate_by_label["Ошибок"]),
                }

                rendered_rows = history_page.evaluate("""() => {
                    const textAfter = (diff, label) => {
                        const line = Array.from(diff?.children || []).find(node =>
                            node.querySelector('span')?.textContent.trim() === label);
                        if (!line) return null;
                        return line.textContent.trim().slice(label.length).trim();
                    };
                    return Array.from(document.querySelectorAll('[data-operations-product-id]')).map(row => {
                        const productId = Number(row.getAttribute('data-operations-product-id'));
                        const result = row.querySelector('.operations-row-result');
                        const diff = row.querySelector('[data-operations-changed-field="quantity"]');
                        const fix = row.querySelector('[data-operations-change-fix-link]');
                        const history = Array.from(row.querySelectorAll('a')).find(a =>
                            (a.textContent || '').includes('История'));
                        return {
                            product_id: productId,
                            title: row.querySelector('h4')?.textContent.trim() || '',
                            wb_sync_status: result?.getAttribute('data-wb-sync-status') || null,
                            readable_outcome: result?.innerText.trim() || null,
                            quantity: diff ? {
                                before: textAfter(diff, 'До:'),
                                after: textAfter(diff, 'После:'),
                            } : null,
                            fix_href: fix?.getAttribute('href') || null,
                            history_href: history?.getAttribute('href') || null,
                            private_row_text: row.textContent,
                            retry_affordance_count: Array.from(row.querySelectorAll('a,button,input[type="submit"]'))
                                .filter(control => /повторить|повтор|retry/i.test(
                                    `${control.textContent || ''} ${control.getAttribute('aria-label') || ''} ${control.getAttribute('href') || ''}`
                                )).length,
                        };
                    });
                }""")
                rendered_product_ids = [int(row["product_id"]) for row in rendered_rows]
                owned_set = set(owned_product_ids)
                foreign_rows = [row for row in rendered_rows if row["product_id"] == foreign_product_id]
                assert len(rendered_rows) == 31
                assert rendered_product_ids == expected_product_ids
                assert len(owned_set) == 30 and foreign_product_id not in owned_set
                assert len(foreign_rows) == 1
                visible_rows = [row for row in rendered_rows if row["product_id"] in owned_set]
                visible_statuses = [
                    {
                        "product_id": row["product_id"],
                        "wb_sync_status": row["wb_sync_status"],
                        "readable_outcome": row["readable_outcome"],
                    }
                    for row in visible_rows
                ]
                assert all(
                    isinstance(row["readable_outcome"], str)
                    and row["readable_outcome"]
                    for row in visible_statuses
                )
                rendered_status_counts = dict(sorted(Counter(
                    row["wb_sync_status"] for row in visible_statuses
                ).items()))
                owned_quantity_values = []
                for row in visible_rows:
                    expected = before_values[row["product_id"]]
                    actual = row["quantity"]
                    assert actual is not None
                    owned_quantity_values.append({
                        "product_id": row["product_id"],
                        "before": expected["before"],
                        "after": expected["after"],
                        "rendered_before": actual["before"],
                        "rendered_after": actual["after"],
                    })
                values_exact = len(owned_quantity_values) == 30 and all(
                    row["rendered_before"] == str(row["before"])
                    and row["rendered_after"] == str(row["after"])
                    for row in owned_quantity_values
                )
                exact_owned_product_ids = (
                    len(rendered_product_ids) == len(set(rendered_product_ids))
                    and set(rendered_product_ids) == set(expected_product_ids)
                    and len([row for row in rendered_rows if row["product_id"] in owned_set]) == 30
                    and len([row for row in rendered_rows if row["product_id"] == foreign_product_id]) == 1
                )
                foreign_row = foreign_rows[0]
                foreign_text = foreign_row["private_row_text"]
                private_text_absent = all(value not in foreign_text for value in (
                    "Private foreign history product title",
                    "R10-FOREIGN-HISTORY-SECRET",
                    "Private foreign conflict detail",
                    "9999",
                    "10000",
                ))
                assert private_text_absent
                assert foreign_row["fix_href"] is None and foreign_row["history_href"] is None
                assert foreign_row["wb_sync_status"] is None
                assert status_counts == {
                    "conflict": 1,
                    "failed": 1,
                    "partial": 1,
                    "pending": 1,
                    "submitted": 1,
                    "success": 25,
                    "uncertain": 1,
                }
                assert rendered_status_counts == {
                    "failed": 1,
                    "partial": 1,
                    "pending": 1,
                    "submitted": 1,
                    "success": 25,
                    "uncertain": 1,
                }
                assert values_exact and exact_owned_product_ids
                REPORT["history_scenario_checks"].append({
                    "name": "wb_history_batch31_exact_rows_values_and_outcomes",
                    "status": "passed",
                    "origin": base,
                    "method": history_response.request.method,
                    "path": history_url,
                    "http_status": history_response.status,
                    "page_heading": history_heading,
                    "bulk_id": bulk_id,
                    "total_products": operation_total_products,
                    "operation_status": operation_status,
                    "operation_success_count": operation_success_count,
                    "operation_error_count": operation_error_count,
                    "operation_completed_at": operation_completed_at,
                    "operation_duration_seconds": operation_duration_seconds,
                    "pending_unprocessed_product_id": pending_unprocessed_product_id,
                    "rendered_rows": len(rendered_rows),
                    "owned_visible_rows": len(visible_rows),
                    "foreign_hidden_rows": len(foreign_rows),
                    "product_ids": rendered_product_ids,
                    "owned_product_ids": owned_product_ids,
                    "foreign_product_id": foreign_product_id,
                    "status_counts": status_counts,
                    "product_statuses": product_statuses,
                    "rendered_product_statuses": visible_statuses,
                    "rendered_status_counts": rendered_status_counts,
                    "owned_quantity_values": owned_quantity_values,
                    "values_exact": values_exact,
                    "exact_owned_product_ids": exact_owned_product_ids,
                    "aggregates": {
                        **aggregate_map,
                    },
                    "aggregate_cards": aggregate_cards,
                })

                # The row-level repair link must navigate to the actual owned
                # card route. No status-changing or rollback form is submitted.
                owned_row = history_page.locator(
                    f'[data-operations-product-id="{owned_product_ids[0]}"]',
                )
                owned_fix_link = owned_row.locator("[data-operations-change-fix-link]")
                assert owned_fix_link.count() == 1
                clicked_label = owned_fix_link.inner_text().strip()
                assert clicked_label == "Карточка WB", clicked_label
                expected_product_path = f"/products/{owned_product_ids[0]}"
                with history_page.expect_navigation(wait_until="domcontentloaded") as navigation:
                    owned_fix_link.click()
                product_response = navigation.value
                assert product_response and product_response.status == 200
                assert product_response.request.method == "GET"
                history_page.wait_for_url(base + expected_product_path)
                product_heading = history_page.locator("#main-content h1").inner_text().strip()
                product_text = history_page.locator("#main-content").inner_text()
                with app.app_context():
                    exact_product = db.session.get(Product, owned_product_ids[0])
                    exact_product_data = {
                        "title": exact_product.title,
                        "vendor_code": exact_product.vendor_code,
                        "nm_id": int(exact_product.nm_id),
                    }
                title_matches = exact_product_data["title"] in product_text
                vendor_code_matches = exact_product_data["vendor_code"] in product_text
                nm_id_matches = str(exact_product_data["nm_id"]) in product_text
                assert title_matches and vendor_code_matches and nm_id_matches
                REPORT["history_scenario_checks"].append({
                    "name": "wb_history_owned_fix_link_opens_exact_product",
                    "status": "passed",
                    "origin": base,
                    "method": product_response.request.method,
                    "path": expected_product_path,
                    "http_status": product_response.status,
                    "product_id": owned_product_ids[0],
                    "clicked_label": clicked_label,
                    "page_heading": product_heading,
                    "title_matches": title_matches,
                    "vendor_code_matches": vendor_code_matches,
                    "nm_id_matches": nm_id_matches,
                })

                unresolved_statuses = {"pending", "submitted", "uncertain", "partial"}
                unresolved_rows = [
                    row for row in visible_rows
                    if row["wb_sync_status"] in unresolved_statuses
                ]
                retry_free = len(unresolved_rows) == 4 and all(
                    row["retry_affordance_count"] == 0 for row in unresolved_rows
                )
                main_history_refresh = history_page.goto(
                    base + history_url, wait_until="domcontentloaded",
                )
                assert main_history_refresh and main_history_refresh.status == 200
                revert_form_count = history_page.locator(
                    f'form[action="/bulk-history/{bulk_id}/revert"]',
                ).count()
                unsupported_note_rendered = history_page.locator(
                    ".operations-revert-note",
                ).count() > 0
                assert retry_free
                assert revert_form_count == 0
                assert not unsupported_note_rendered
                assert not REPORT["browser_mutations"]
                REPORT["history_scenario_checks"].append({
                    "name": "wb_history_foreign_fix_link_absent",
                    "status": "passed",
                    "foreign_product_id": foreign_product_id,
                    "fix_link_count": 0,
                    "history_link_count": 0,
                    "private_text_absent": private_text_absent,
                })
                REPORT["history_scenario_checks"].append({
                    "name": "wb_history_unresolved_rows_no_retry_or_revert",
                    "status": "passed",
                    "unresolved_product_statuses": [
                        {
                            "product_id": row["product_id"],
                            "wb_sync_status": row["wb_sync_status"],
                        }
                        for row in unresolved_rows
                    ],
                    "retry_affordances_absent": retry_free,
                    "revert_form_count": revert_form_count,
                    "mutation_count": len(REPORT["browser_mutations"]),
                    "post_count": len([
                        row for row in REPORT["browser_mutations"]
                        if row.get("method") == "POST"
                    ]),
                    "quantity_rollback_contract_supported": quantity_rollback_contract_supported,
                    "unsupported_rollback_note_rendered": unsupported_note_rendered,
                })

                # Separately visit a completed one-row quantity-only fixture
                # to verify the existing explanatory copy without submitting
                # any revert or provider write request.
                rollback_response = history_page.goto(
                    base + rollback_history_url, wait_until="domcontentloaded",
                )
                assert rollback_response and rollback_response.status == 200
                assert rollback_response.request.method == "GET"
                rollback_heading = history_page.locator(
                    "#main-content h1",
                ).inner_text().strip()
                assert rollback_heading == "R10 completed quantity-only rollback fixture"
                rollback_row_locator = history_page.locator(
                    f'[data-operations-product-id="{rollback_product_id}"]',
                )
                assert rollback_row_locator.count() == 1
                rollback_rendered = history_page.evaluate("""productId => {
                    const row = document.querySelector(
                        `[data-operations-product-id="${productId}"]`);
                    const diff = row?.querySelector('[data-operations-changed-field="quantity"]');
                    const textAfter = (node, label) => {
                        const line = Array.from(node?.children || []).find(child =>
                            child.querySelector('span')?.textContent.trim() === label);
                        return line ? line.textContent.trim().slice(label.length).trim() : null;
                    };
                    const identityText = Array.from(row?.querySelectorAll('p') || [])
                        .map(node => node.innerText.trim())
                        .find(text => text.includes('Артикул:') && text.includes('NM ID:')) || '';
                    const identityMatch = identityText.match(/Артикул:\s*(.*?)\s*\|\s*NM ID:\s*(\d+)/);
                    return {
                        product_id: Number(row?.getAttribute('data-operations-product-id')),
                        title: row?.querySelector('h4')?.innerText.trim() || '',
                        vendor_code: identityMatch?.[1] || null,
                        nm_id: identityMatch ? Number(identityMatch[2]) : null,
                        quantity: diff ? {
                            before: textAfter(diff, 'До:'),
                            after: textAfter(diff, 'После:'),
                        } : null,
                    };
                }""", rollback_product_id)
                rollback_note = history_page.locator(".operations-revert-note")
                assert rollback_note.count() == 1
                rollback_note_text = rollback_note.inner_text().strip()
                assert rollback_note_text == (
                    "Безопасный откат для этой операции недоступен. Проверьте результаты строк выше."
                )
                rollback_form_count = history_page.locator(
                    f'form[action="{rollback_history_url}/revert"]',
                ).count()
                with app.app_context():
                    rollback_batch = db.session.get(BulkEditHistory, rollback_batch_id)
                    rollback_history = db.session.get(CardEditHistory, rollback_history_id)
                    assert rollback_batch is not None and rollback_history is not None
                    rollback_operation_status = rollback_batch.status
                    rollback_total_products = int(rollback_batch.total_products)
                    rollback_success_count = int(rollback_batch.success_count)
                    rollback_error_count = int(rollback_batch.error_count)
                    operation_seller_id = int(rollback_batch.seller_id)
                    rollback_changed_fields = list(rollback_history.changed_fields or [])
                    rollback_before = (rollback_history.snapshot_before or {}).get("quantity")
                    rollback_after = (rollback_history.snapshot_after or {}).get("quantity")
                    product_seller_id = int(rollback_history.product.seller_id)
                    rollback_product_title = rollback_history.product.title
                    rollback_product_vendor_code = rollback_history.product.vendor_code
                    rollback_product_nm_id = int(rollback_history.product.nm_id)
                    rollback_card_history_count = CardEditHistory.query.filter_by(
                        bulk_edit_id=rollback_batch_id,
                        seller_id=fixture["seller_id"],
                    ).count()
                    rollback_safe_revert = rollback_history.supports_safe_revert()
                    assert rollback_batch.can_revert() is False
                assert rollback_operation_status == "completed"
                assert rollback_total_products == 1
                assert rollback_success_count == 1 and rollback_error_count == 0
                assert operation_seller_id == int(fixture["seller_id"])
                assert product_seller_id == operation_seller_id
                assert rollback_card_history_count == 1
                assert rollback_changed_fields == ["quantity"]
                assert rollback_safe_revert is False
                assert rollback_rendered == {
                    "product_id": rollback_product_id,
                    "title": rollback_product_title,
                    "vendor_code": rollback_product_vendor_code,
                    "nm_id": rollback_product_nm_id,
                    "quantity": {
                        "before": str(rollback_before),
                        "after": str(rollback_after),
                    },
                }
                assert rollback_form_count == 0
                assert not REPORT["browser_mutations"]
                completed_rollback_view = {
                    "operation_id": rollback_batch_id,
                    "origin": base,
                    "method": rollback_response.request.method,
                    "path": rollback_history_url,
                    "http_status": rollback_response.status,
                    "page_heading": rollback_heading,
                    "operation_status": rollback_operation_status,
                    "operation_seller_id": operation_seller_id,
                    "product_seller_id": product_seller_id,
                    "owned_identity_matches": product_seller_id == operation_seller_id,
                    "total_products": rollback_total_products,
                    "success_count": rollback_success_count,
                    "error_count": rollback_error_count,
                    "product_id": rollback_product_id,
                    "product_title": rollback_product_title,
                    "vendor_code": rollback_product_vendor_code,
                    "nm_id": rollback_product_nm_id,
                    "title_matches": rollback_rendered["title"] == rollback_product_title,
                    "vendor_code_matches": rollback_rendered["vendor_code"] == rollback_product_vendor_code,
                    "nm_id_matches": rollback_rendered["nm_id"] == rollback_product_nm_id,
                    "card_edit_history_count": rollback_card_history_count,
                    "changed_fields": rollback_changed_fields,
                    "snapshot_before": {"quantity": rollback_before},
                    "snapshot_after": {"quantity": rollback_after},
                    "rendered_before": rollback_rendered["quantity"]["before"],
                    "rendered_after": rollback_rendered["quantity"]["after"],
                    "safe_revert_supported": rollback_safe_revert,
                    "revert_form_count": rollback_form_count,
                    "unsupported_note_visible": True,
                    "unsupported_note_text": rollback_note_text,
                    "post_count": len([
                        row for row in REPORT["browser_mutations"]
                        if row.get("method") == "POST"
                    ]),
                }
                REPORT["history_scenario_checks"][-1][
                    "completed_quantity_rollback_view"
                ] = completed_rollback_view

                history_page.close()
                history_sql["active"] = False
                REPORT["history_domain_sql_writes"] = history_sql["writes"]
                with app.app_context():
                    history_after = history_domain_counts()
                REPORT["history_domain_state"] = {
                    "before": history_before,
                    "after": history_after,
                    "unchanged": history_before == history_after,
                }
                assert REPORT["history_domain_sql_writes"] == []
                assert REPORT["history_domain_state"]["unchanged"] is True
                assert not REPORT["browser_mutations"]
                expected_history_names = {
                    "wb_history_batch31_exact_rows_values_and_outcomes",
                    "wb_history_owned_fix_link_opens_exact_product",
                    "wb_history_foreign_fix_link_absent",
                    "wb_history_unresolved_rows_no_retry_or_revert",
                }
                assert {row["name"] for row in REPORT["history_scenario_checks"]} == expected_history_names
                assert all(row.get("status") == "passed" for row in REPORT["history_scenario_checks"])

            context.close()
            browser.close()
    except Exception as exc:
        report_error = {"type": type(exc).__name__, "message": str(exc)}
        REPORT["harness_error"] = report_error
    finally:
        server.shutdown()
        thread.join(timeout=3)

    failed_checks = [row for row in REPORT["checks"]
                     if row.get("scope") == "after_acceptance" and row.get("passed") is not True]
    failed_interactions = [row for row in REPORT["interactions"]
                           if row.get("scope") == "after_acceptance" and row.get("passed") is not True]
    failed_theme_assertions = [row for row in REPORT["actual_theme_assertions"]
                               if row.get("passed") is not True]
    layouts_per_theme = sum(2 if width in (320, 390) else 1 for width in WIDTHS)
    expected_layouts = len(pages) * 2 * layouts_per_theme
    safe_capture = (
        not report_error
        and not REPORT["javascript_errors"]
        and not REPORT["console_errors"]
        and not REPORT["unexpected_http"]
        and not REPORT["unexpected_external_requests"]
        and not REPORT["request_failures"]
        and not REPORT["browser_mutations"]
        and not REPORT["blocked_writes"]
        and not REPORT["writes"]
        and REPORT["provider_attempts"] == 0
        and not failed_theme_assertions
        and len(REPORT["pages"]) == len(pages) * 2
        and len(REPORT["layouts"]) == expected_layouts
        and len(REPORT["actual_theme_assertions"]) == len(pages) * 2
        and len(REPORT["interactions"]) == len(pages) * 2
        and (
            "wb_prices_change" not in {label for label, _path in pages}
            or (
                len(REPORT["price_initialization"]) == 2
                and {row.get("theme") for row in REPORT["price_initialization"]} == {"light", "dark"}
                and all(row.get("actual_theme") == row.get("theme") for row in REPORT["price_initialization"])
            )
        )
    )
    passed = safe_capture and (
        SOURCE == "ba63371"
        or (
            not REPORT["after_failures"]
            and not failed_checks
            and not failed_interactions
            and all(row.get("status") == "passed" for row in REPORT["checks"]
                    if row.get("name") == "wb_price_change_initializes_once_and_renders_products")
        )
    )
    REPORT["status"] = (
        "baseline_recorded" if passed and SOURCE == "ba63371"
        else "passed" if passed
        else "failed"
    )
    REPORT["failed_checks"] = failed_checks
    REPORT["failed_interactions"] = failed_interactions
    REPORT["failed_theme_assertions"] = failed_theme_assertions
    REPORT["fixture_limits"] = [
        "Synthetic seller, listing, proposal, operation, and WB history rows only.",
        "The browser bridge blocks non-GET/HEAD local requests and all unlisted external hosts.",
        "Tailwind/chart/fonts/Vue assets are served only from checked-in pinned files; no provider/API credentials are used.",
        "At each viewport width, text enlargement doubles freshly computed font sizes and restores them after measurement.",
        "Baseline mode records unsupported new checks as observations; worktree mode enforces newly introduced primitives.",
    ]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": REPORT["status"],
        "source": SOURCE,
        "pages": len(REPORT["pages"]),
        "layouts": len(REPORT["layouts"]),
        "checks": REPORT["checks"],
        "after_failures": len(REPORT["after_failures"]),
        "javascript_errors": len(REPORT["javascript_errors"]),
        "unexpected_http": len(REPORT["unexpected_http"]),
        "unexpected_external_requests": len(REPORT["unexpected_external_requests"]),
        "provider_attempts": REPORT["provider_attempts"],
        "failed_interactions": len(failed_interactions),
        "failed_theme_assertions": len(failed_theme_assertions),
        "report": str(REPORT_PATH),
    }, ensure_ascii=False))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
