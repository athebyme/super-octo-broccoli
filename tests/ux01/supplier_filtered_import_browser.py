"""Synthetic browser proof for supplier selection, review, and WB create.

Uses the isolated app, SQLite fixture, network-denying browser bridge, and
synthetic seller session from ``journey_browser``. It covers exact local
supplier catalog and classic-to-beta refresh review/confirm alongside the
normal import form and one exact WB bulk-create POST. A scoped fake importer
supplies deterministic WB outcomes after the real route and BackgroundJob
worker perform their normal seller scope/readiness checks.

Run with the repository venv and Chromium installed. The summary is written to
``/artifacts/supplier-filtered-import.json`` by default.
"""

from __future__ import annotations

import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import sync_playwright


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.ux01 import journey_browser as fixture
from tests.ux01.bulk_action_controls import invoke_workspace_action, workspace_action_launcher
from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceCategory,
    PricingSettings,
    Seller,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    User,
    db,
)


REPORT_PATH = Path(os.environ.get(
    "SUPPLIER_FILTERED_IMPORT_REPORT",
    "/artifacts/supplier-filtered-import.json",
))
IMPORT_PATH = "/supplier-catalog/import"
SUPPLIER_REVIEW_PATH = "/supplier-catalog/update/review"
SUPPLIER_CONFIRM_PATH = "/supplier-catalog/update/confirm"
SUPPLIER_REFRESH_HANDOFF_PATH = "/my-products/refresh-from-supplier/review"
SUPPLIER_REFRESH_PREVIEW_PATH = "/api/my-products/supplier-refresh-preview"
SUPPLIER_REFRESH_APPLY_PATH = "/my-products/refresh-from-supplier"
WB_CREATE_PATH = "/my-products/push-to-wb-bulk"
WB_CREATE_RESULT_PATH = "/my-products/push-to-wb-bulk/result"
JOB_STATUS_PREFIX = "/api/jobs/"
FILTERS = {
    "search": "R28 Mass batch",
    "stock_status": "in_stock",
    "ai_filter": "ai_parsed",
    "wb_filter": "not_on_wb",
    "show_imported": "1",
}


def seed_supplier_products():
    """Create 53 eligible current-supplier matches plus exact exclusions.

    The eligible source rows also carry enough observed facts to pass the real
    WB upload-readiness validator after the normal supplier import. This keeps
    the later mixed result focused on the importer outcome, not missing seed
    data.
    """
    with fixture.app.app_context():
        seller_id = fixture.FIXTURE["seller_id"]
        supplier_id = fixture.FIXTURE["supplier_id"]
        seller = db.session.get(Seller, seller_id)
        assert seller is not None
        seller.wb_api_key = "synthetic-wb-key-never-sent"
        pricing_settings = PricingSettings.query.filter_by(seller_id=seller_id).first()
        if pricing_settings is None:
            db.session.add(PricingSettings(
                seller_id=seller_id,
                is_enabled=True,
            ))
        else:
            pricing_settings.is_enabled = True
        seller_supplier = SellerSupplier.query.filter_by(
            seller_id=seller_id,
            supplier_id=supplier_id,
            is_active=True,
        ).one()
        assert seller_supplier
        supplier = db.session.get(Supplier, supplier_id)
        assert supplier is not None
        supplier.last_sync_at = datetime(2026, 9, 21, 10, 15)
        supplier.last_sync_status = "success"
        supplier.price_file_url = "https://fixture.invalid/synthetic-price-feed.csv"
        supplier.auto_sync_prices = True
        supplier.last_price_sync_at = datetime(2026, 9, 22, 11, 45)
        supplier.last_price_sync_status = "success"

        wb_marketplace = Marketplace.query.filter_by(code="wb").first()
        if wb_marketplace is None:
            wb_marketplace = Marketplace(
                name="Wildberries",
                code="wb",
                adapter_code="wb",
                is_active=True,
            )
            db.session.add(wb_marketplace)
            db.session.flush()
        db.session.add_all([
            MarketplaceCategory(
                marketplace_id=wb_marketplace.id,
                subject_id=7901 + index,
                subject_name=name,
                parent_name="Women's Clothing",
                is_enabled=True,
                is_leaf=True,
                is_available=True,
            )
            for index, name in enumerate((
                "Women's Jackets",
                "Women's Shoes",
                "Women's Bags",
                "Women's Tops",
                "Women's Dresses",
            ))
        ])

        eligible = []
        for index in range(53):
            product = SupplierProduct(
                supplier_id=supplier_id,
                external_id=f"R28-MASS-{index:03d}",
                vendor_code=f"R28-MASS-{index:03d}",
                title=f"R28 Mass batch item {index:03d}",
                description=(
                    f"Synthetic supplier description for WB readiness item {index:03d}. "
                    "This fixture text is local and is not sent to a provider."
                ),
                brand="R28 Synthetic Brand",
                category="Synthetic supplier category",
                wb_category_name=(
                    "Women's Legacy Category" if index == 0
                    else "Synthetic WB category"
                ),
                wb_subject_id=7000,
                wb_subject_name="Synthetic WB subject",
                supplier_price=100 + index,
                currency="RUB",
                supplier_quantity=4,
                supplier_status="in_stock",
                status="ready",
                ai_parsed_at=datetime(2026, 11, 21),
                photo_urls_json=json.dumps([fixture.PHOTO]),
                colors_json=json.dumps(["Blue"]),
                materials_json=json.dumps(["Cotton"]),
                gender="unisex",
                country="Russia",
                characteristics_json=json.dumps({
                    "Synthetic material": "Cotton",
                    "Synthetic color": "Blue",
                    "Synthetic use": "Daily",
                }),
            )
            db.session.add(product)
            eligible.append(product)

        # These source rows match the search but must not widen the exact
        # active-supplier/filter result.
        out_of_stock = SupplierProduct(
            supplier_id=supplier_id,
            external_id="R28-MASS-OUT",
            title="R28 Mass batch out of stock",
            supplier_quantity=0,
            supplier_status="out_of_stock",
            status="ready",
            ai_parsed_at=datetime(2026, 11, 21),
        )
        not_parsed = SupplierProduct(
            supplier_id=supplier_id,
            external_id="R28-MASS-NO-AI",
            title="R28 Mass batch not parsed",
            supplier_quantity=3,
            supplier_status="in_stock",
            status="ready",
        )
        other_supplier = Supplier(
            name="R28 Mass other supplier",
            code="r28-mass-other-supplier",
            is_active=True,
        )
        db.session.add_all([out_of_stock, not_parsed, other_supplier])
        db.session.flush()
        db.session.add(ImportedProduct(
            seller_id=seller_id,
            supplier_id=supplier_id,
            supplier_product_id=out_of_stock.id,
            supplier_content_revision=1,
            title="R28 existing local supplier copy",
            import_status="validated",
        ))
        db.session.add(SellerSupplier(
            seller_id=seller_id,
            supplier_id=other_supplier.id,
            is_active=True,
        ))
        foreign_source = SupplierProduct(
            supplier_id=other_supplier.id,
            external_id="R28-MASS-OTHER",
            title="R28 Mass batch from other supplier",
            supplier_quantity=5,
            supplier_status="in_stock",
            status="ready",
            ai_parsed_at=datetime(2026, 11, 21),
        )
        db.session.add(foreign_source)

        # More than 200 rows use a separate search so the same route's
        # explicit fallback can be checked without changing the 53-row proof.
        oversized = [
            SupplierProduct(
                supplier_id=supplier_id,
                external_id=f"R28-OVERSIZE-{index:03d}",
                title=f"R28 Oversize batch item {index:03d}",
                supplier_quantity=2,
                supplier_status="in_stock",
                status="ready",
            )
            for index in range(201)
        ]
        db.session.add_all(oversized)
        db.session.commit()
        imported_before = ImportedProduct.query.count()
        return {
            "supplier_id": supplier_id,
            "category_source_id": str(eligible[0].id),
            "eligible_ids": [str(product.id) for product in eligible],
            "excluded_ids": [
                str(out_of_stock.id), str(not_parsed.id), str(foreign_source.id),
            ],
            "oversized_count": len(oversized),
            "imported_before": imported_before,
        }


def seed_supplier_update_review_rows(fixture_ids, imported_source_ids):
    """Seed exact selected/no-op rows and 201 tail rows for all-scope review."""
    with fixture.app.app_context():
        seller_id = fixture.FIXTURE["seller_id"]
        supplier_id = fixture_ids["supplier_id"]
        already_imported = {int(source_id) for source_id in imported_source_ids}
        selected_source_id, current_source_id = [
            int(source_id)
            for source_id in fixture_ids["eligible_ids"]
            if source_id not in already_imported
        ][:2]
        assert selected_source_id and current_source_id

        oversized_sources = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.external_id.like("R28-OVERSIZE-%"),
        ).order_by(SupplierProduct.id).all()
        assert len(oversized_sources) == fixture_ids["oversized_count"] == 201
        copies = [
            ImportedProduct(
                seller_id=seller_id, supplier_id=supplier_id,
                supplier_product_id=selected_source_id, supplier_content_revision=0,
                title="R42 stale selected local title", import_status="validated",
            ),
            ImportedProduct(
                seller_id=seller_id, supplier_id=supplier_id,
                supplier_product_id=current_source_id, supplier_content_revision=0,
                title="R42 current local seed title", import_status="validated",
            ),
            ImportedProduct(
                seller_id=seller_id, supplier_id=supplier_id,
                supplier_product_id=None, external_id="R42-UNLINKED-LOCAL",
                title="R42 unlinked local card", import_status="validated",
            ),
            *[
                ImportedProduct(
                    seller_id=seller_id,
                    supplier_id=supplier_id,
                    supplier_product_id=source.id,
                    supplier_content_revision=0,
                    title=f"R42 stale {source.title}",
                    import_status="validated",
                )
                for source in oversized_sources
            ],
        ]
        db.session.add_all(copies)
        db.session.flush()

        # The service takes a fresh SQLite writer transaction and rolls back
        # pending session state before BEGIN IMMEDIATE. Persist this browser
        # fixture first so the legacy refresh sees the intended local copies.
        db.session.commit()
        persisted_current = ImportedProduct.query.filter_by(
            seller_id=seller_id,
            supplier_id=supplier_id,
            supplier_product_id=current_source_id,
        ).one()
        assert persisted_current.id == copies[1].id

        from services.supplier_service import SupplierService

        seeded_current = SupplierService.update_supplier_products(
            seller_id,
            supplier_id,
            [current_source_id],
        )
        assert (seeded_current.total_requested, seeded_current.imported) == (1, 1)
        current_copy = db.session.get(ImportedProduct, copies[1].id)
        current_source = db.session.get(SupplierProduct, current_source_id)
        assert current_copy.seller_id == seller_id
        assert current_copy.supplier_id == supplier_id
        assert current_copy.supplier_content_revision == current_source.content_revision
        assert current_copy.title == current_source.title
        seeded_review = SupplierService.preview_supplier_products(
            seller_id, supplier_id, [current_source_id], details_limit=1,
        )
        assert (
            seeded_review["requested"], seeded_review["eligible"],
            seeded_review["changed"], seeded_review["already_current"],
            seeded_review["unlinked"],
        ) == (1, 1, 0, 1, 0)

        detail_source_id = int(fixture_ids["excluded_ids"][0])
        detail_copy = ImportedProduct.query.filter_by(
            seller_id=seller_id,
            supplier_product_id=detail_source_id,
        ).one()
        return {
            "detail_source_id": detail_source_id,
            "detail_copy_id": detail_copy.id,
            "selected_source_id": selected_source_id,
            "selected_source_external_id": SupplierProduct.query.filter_by(
                id=selected_source_id,
            ).one().external_id,
            "selected_copy_id": copies[0].id,
            "current_source_id": current_source_id,
            "current_copy_id": copies[1].id,
            "unlinked_copy_id": copies[2].id,
            "oversized_tail_source_id": oversized_sources[-1].id,
            "oversized_tail_copy_id": copies[-1].id,
        }


def seed_filtered_supplier_update_rows(supplier_id):
    """Seed multi-page stale sources with exact seller-owned local copies.

    One source has both a stale and already-current local copy, built from the
    same transient supplier projection as preview, and one matching source
    has no local copy for this seller.
    """
    with fixture.app.app_context():
        from services.supplier_service import (
            SUPPLIER_REFRESH_REVIEW_PRODUCT_FIELDS,
            SupplierService,
            _supplier_refresh_clone,
            _update_imported_from_supplier,
        )

        seller_id = fixture.FIXTURE["seller_id"]
        sources = []
        stale_copies = []
        for index in range(55):
            source = SupplierProduct(
                supplier_id=supplier_id,
                external_id=f"R52-FILTERED-{index:03d}",
                vendor_code=f"R52-FILTERED-{index:03d}",
                title=f"R52 Filtered update {index:03d}",
                description=f"Synthetic filtered-update source {index:03d}.",
                brand="R52 Filtered update fixture",
                category="Synthetic supplier category",
                wb_category_name="Synthetic WB category",
                wb_subject_id=7000,
                wb_subject_name="Synthetic WB subject",
                supplier_price=200 + index,
                currency="RUB",
                supplier_quantity=5,
                supplier_status="in_stock",
                status="ready",
                ai_parsed_at=datetime(2026, 11, 21),
                content_revision=2,
            )
            db.session.add(source)
            sources.append(source)

        unimported_source = SupplierProduct(
            supplier_id=supplier_id,
            external_id="R52-FILTERED-UNIMPORTED",
            vendor_code="R52-FILTERED-UNIMPORTED",
            title="R52 Filtered update unimported",
            brand="R52 Filtered update fixture",
            category="Synthetic supplier category",
            wb_category_name="Synthetic WB category",
            wb_subject_id=7000,
            wb_subject_name="Synthetic WB subject",
            supplier_quantity=5,
            supplier_status="in_stock",
            status="ready",
            ai_parsed_at=datetime(2026, 11, 21),
            content_revision=2,
        )
        db.session.add(unimported_source)
        db.session.flush()

        for index, source in enumerate(sources):
            stale_copy = ImportedProduct(
                seller_id=seller_id,
                supplier_id=supplier_id,
                supplier_product_id=source.id,
                supplier_content_revision=1,
                title=f"R52 stale local copy {index:03d}",
                import_status="validated",
            )
            db.session.add(stale_copy)
            stale_copies.append(stale_copy)
            if index == 0:
                current_duplicate = ImportedProduct(
                    seller_id=seller_id,
                    supplier_id=supplier_id,
                    supplier_product_id=source.id,
                    import_status="validated",
                )
                # Build every reviewed field through the same nonpersisting
                # projection updater used by supplier refresh preview. A
                # matching title/revision alone is not a current row because
                # supplier metadata and source snapshots also participate.
                projected_current = _supplier_refresh_clone(current_duplicate)
                _update_imported_from_supplier(projected_current, source)
                for field in SUPPLIER_REFRESH_REVIEW_PRODUCT_FIELDS:
                    setattr(
                        current_duplicate,
                        field,
                        copy.deepcopy(getattr(projected_current, field)),
                    )
                db.session.add(current_duplicate)
        db.session.flush()
        current_duplicate = ImportedProduct.query.filter_by(
            seller_id=seller_id,
            supplier_id=supplier_id,
            supplier_product_id=sources[0].id,
            supplier_content_revision=sources[0].content_revision,
        ).one()
        db.session.commit()
        seeded_review = SupplierService.preview_supplier_products(
            seller_id,
            supplier_id,
            [sources[0].id],
            details_limit=2,
        )
        seeded_counts = {
            key: seeded_review[key]
            for key in ("requested", "eligible", "changed", "already_current", "unlinked")
        }
        assert seeded_counts == {
            "requested": 2,
            "eligible": 2,
            "changed": 1,
            "already_current": 1,
            "unlinked": 0,
        }, f"transient projection did not seed one current duplicate: {seeded_counts!r}"
        return {
            "source_ids": [source.id for source in sources],
            "stale_copy_ids": [copy.id for copy in stale_copies],
            "current_duplicate_copy_id": current_duplicate.id,
            "unimported_source_id": unimported_source.id,
        }


def assert_supplier_warning_contrast(page, selector):
    initial_theme = page.evaluate("document.documentElement.dataset.theme")
    for theme in ("light", "dark"):
        page.evaluate("(theme) => document.documentElement.dataset.theme = theme", theme)
        ratio = page.locator(selector).evaluate("""(element) => {
            const style = getComputedStyle(element);
            const luminance = color => {
                const rgb = color.match(/[0-9]+(?:[.][0-9]+)?/g).slice(0, 3).map(
                    value => {
                        const component = Number(value) / 255;
                        return component <= 0.04045
                            ? component / 12.92 : ((component + 0.055) / 1.055) ** 2.4;
                    }
                );
                return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
            };
            const foreground = luminance(style.color), background = luminance(style.backgroundColor);
            return (Math.max(foreground, background) + 0.05) /
                (Math.min(foreground, background) + 0.05);
        }""")
        assert ratio >= 4.5, (selector, theme, ratio)
    page.evaluate("(theme) => document.documentElement.dataset.theme = theme", initial_theme)


def run():
    fixture_ids = seed_supplier_products()
    category_update_path = (
        f"/supplier-catalog/{fixture_ids['supplier_id']}/products/"
        f"{fixture_ids['category_source_id']}/update-category"
    )
    checks = []
    posts = []
    supplier_handoff_posts = []
    supplier_handoff_responses = []
    supplier_review_posts = []
    supplier_confirm_posts = []
    supplier_refresh_preview_posts = []
    supplier_refresh_preview_responses = []
    supplier_refresh_apply_posts = []
    supplier_refresh_apply_responses = []
    wb_create_posts = []
    wb_create_result_reads = []
    product_feed_reads = []
    facet_reads = []
    category_reads = []
    category_search_reads = []
    category_update_posts = []
    document_requests = []
    job_status_reads = []
    page_errors = []
    classic_fallback_documents = []
    event_number = [0]
    wb_create_report = None

    def next_event():
        event_number[0] += 1
        return event_number[0]

    def bridge(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.netloc == urlsplit(fixture.BASE).netloc and request.method == "GET":
            if parsed.path == "/static/supplier-catalog-update-review.js":
                # Exercise the server forms if just the inline enhancer is unavailable.
                classic_fallback_documents.append(parsed.path)
                route.fulfill(status=200, content_type="application/javascript", body="")
                return
            if parsed.path == "/api/wb-subjects/search":
                query = parse_qs(parsed.query, keep_blank_values=True)
                assert query == {"q": ["Women's"]}
                try:
                    response = route.fetch(max_redirects=0)
                    payload = response.json()
                except Exception:
                    raise AssertionError("local WB category search failed") from None
                category_search_reads.append({
                    "query": query,
                    "status": response.status,
                    "payload": payload,
                })
                assert response.status == 200 and payload.get("success") is True
                assert len(payload.get("subjects", [])) == 5
                route.fulfill(response=response)
                return
            if parsed.path == "/api/my-products":
                query = parse_qs(parsed.query, keep_blank_values=True)
                try:
                    response = route.fetch(max_redirects=0)
                except Exception:
                    raise AssertionError("local product feed request failed") from None
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                product_feed_reads.append({
                    "sequence": next_event(),
                    "query_keys": tuple(sorted(query)),
                    "batch_tokens": tuple(query.get("import_batch", [])),
                    "supplier_refresh_tokens": tuple(query.get("supplier_refresh_batch", [])),
                    "status": response.status,
                    "payload": payload,
                })
                if response.status >= 400:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
                route.fulfill(response=response)
                return
            if (
                parsed.path.startswith(JOB_STATUS_PREFIX)
                and parsed.path.removeprefix(JOB_STATUS_PREFIX)
                not in {"active", "recent"}
            ):
                try:
                    response = route.fetch(max_redirects=0)
                except Exception:
                    raise AssertionError("local WB job status request failed") from None
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                job_status_reads.append({
                    "sequence": next_event(),
                    "method": request.method,
                    "path": parsed.path,
                    "status": response.status,
                    "payload": payload,
                })
                if response.status >= 400:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
                route.fulfill(response=response)
                return
            if parsed.path == "/api/my-products/facets":
                facet_reads.append(tuple(sorted(parse_qs(parsed.query, keep_blank_values=True))))
                try:
                    response = route.fetch(max_redirects=0)
                except Exception:
                    raise AssertionError("local product facets request failed") from None
                if response.status >= 400:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
                route.fulfill(response=response)
                return
            if parsed.path == "/api/my-products/categories":
                query = parse_qs(parsed.query, keep_blank_values=True)
                try:
                    response = route.fetch(max_redirects=0)
                except Exception:
                    raise AssertionError("local product category options request failed") from None
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                category_reads.append({
                    "sequence": next_event(),
                    "query_keys": tuple(sorted(query)),
                    "pages": tuple(query.get("page", [])),
                    "status": response.status,
                    "payload": payload,
                })
                if response.status >= 400:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
                route.fulfill(response=response)
                return
            if parsed.path == WB_CREATE_RESULT_PATH:
                assert not parsed.query, "WB receipt key must not appear in the query string"
                request_key = request.headers.get("x-wb-create-key")
                assert request_key and re.fullmatch(r"[a-f0-9]{32}", request_key)
                try:
                    response = route.fetch(max_redirects=0)
                except Exception:
                    raise AssertionError("local WB create receipt read failed") from None
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                wb_create_result_reads.append({
                    "sequence": next_event(),
                    "request_key": request_key,
                    "query_keys": tuple(sorted(parse_qs(parsed.query, keep_blank_values=True))),
                    "status": response.status,
                    "payload": payload,
                })
                if response.status >= 400:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
                route.fulfill(response=response)
                return
        if parsed.netloc == urlsplit(fixture.BASE).netloc and request.method == "POST":
            if parsed.path == category_update_path:
                assert "application/json" in (request.headers.get("content-type") or "")
                assert request.headers.get("x-csrftoken")
                try:
                    body = request.post_data_json
                except Exception:
                    raise AssertionError("category assignment must carry JSON") from None
                assert body == {
                    "subject_id": 7901,
                    "subject_name": "Women's Jackets",
                    "display": "Women's Clothing > Women's Jackets",
                }
                category_update_posts.append({"path": parsed.path, "body": body})
                response = route.fetch(max_redirects=0)
                payload = response.json()
                category_update_posts[-1].update({
                    "status": response.status,
                    "payload": payload,
                })
                if response.status != 200 or payload.get("success") is not True:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
                fixture.REPORT["writes"].append({
                    "method": "POST", "path": parsed.path,
                })
                route.fulfill(response=response)
                return
            assert parsed.path in {
                IMPORT_PATH,
                SUPPLIER_REVIEW_PATH,
                SUPPLIER_CONFIRM_PATH,
                SUPPLIER_REFRESH_HANDOFF_PATH,
                SUPPLIER_REFRESH_PREVIEW_PATH,
                SUPPLIER_REFRESH_APPLY_PATH,
                WB_CREATE_PATH,
            }, (
                request.method, parsed.path,
            )
            if parsed.path in {
                IMPORT_PATH,
                SUPPLIER_REVIEW_PATH,
                SUPPLIER_CONFIRM_PATH,
                SUPPLIER_REFRESH_HANDOFF_PATH,
            }:
                body = parse_qs(request.post_data or "", keep_blank_values=True)
                assert body.get("csrf_token"), "supplier local form must carry CSRF"
                if parsed.path == IMPORT_PATH:
                    posts.append({"path": parsed.path, "body": body})
                elif parsed.path == SUPPLIER_REVIEW_PATH:
                    supplier_review_posts.append({"path": parsed.path, "body": body})
                elif parsed.path == SUPPLIER_REFRESH_HANDOFF_PATH:
                    supplier_handoff_posts.append({
                        "path": parsed.path,
                        "query": parse_qs(parsed.query, keep_blank_values=True),
                        "body": body,
                    })
                else:
                    supplier_confirm_posts.append({"path": parsed.path, "body": body})
            elif parsed.path in {
                SUPPLIER_REFRESH_PREVIEW_PATH,
                SUPPLIER_REFRESH_APPLY_PATH,
            }:
                assert "application/json" in (request.headers.get("content-type") or "")
                assert request.headers.get("x-csrftoken"), "supplier refresh API POST must carry CSRF"
                try:
                    body = request.post_data_json
                except Exception:
                    raise AssertionError("supplier refresh API POST must carry JSON") from None
                if parsed.path == SUPPLIER_REFRESH_PREVIEW_PATH:
                    supplier_refresh_preview_posts.append({"path": parsed.path, "body": body})
                else:
                    supplier_refresh_apply_posts.append({"path": parsed.path, "body": body})
            else:
                assert not parsed.query, "WB create request must not carry a scope in the URL"
                assert request.headers.get("accept") == "application/json"
                assert "application/json" in (request.headers.get("content-type") or "")
                assert request.headers.get("x-csrftoken"), "WB create POST must carry CSRF"
                try:
                    body = request.post_data_json
                except Exception:
                    raise AssertionError("WB create POST must carry JSON") from None
                wb_create_posts.append({
                    "sequence": next_event(),
                    "path": parsed.path,
                    "body": body,
                })
            response = route.fetch(max_redirects=0)
            if parsed.path == WB_CREATE_PATH:
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                wb_create_posts[-1].update({
                    "status": response.status,
                    "payload": payload,
                })
                if response.status >= 400:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
            if parsed.path == SUPPLIER_REFRESH_HANDOFF_PATH:
                supplier_handoff_responses.append({
                    "status": response.status,
                    "location": response.headers.get("location", ""),
                })
                if response.status != 303:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
            elif parsed.path == SUPPLIER_REFRESH_PREVIEW_PATH:
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                supplier_refresh_preview_responses.append({
                    "status": response.status, "payload": payload,
                })
                if response.status != 200:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
            elif parsed.path == SUPPLIER_REFRESH_APPLY_PATH:
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                supplier_refresh_apply_responses.append({
                    "status": response.status, "payload": payload,
                })
                if response.status != 200:
                    fixture.REPORT["unexpected_http"].append({
                        "path": parsed.path, "status": response.status,
                    })
            if parsed.path not in {
                SUPPLIER_REVIEW_PATH,
                SUPPLIER_REFRESH_HANDOFF_PATH,
                SUPPLIER_REFRESH_PREVIEW_PATH,
            }:
                fixture.REPORT["writes"].append({"method": "POST", "path": parsed.path})
            if parsed.path == SUPPLIER_REVIEW_PATH and response.status != 200:
                fixture.REPORT["unexpected_http"].append({
                    "path": parsed.path, "status": response.status,
                })
            if parsed.path == SUPPLIER_CONFIRM_PATH and response.status not in (302, 409):
                fixture.REPORT["unexpected_http"].append({
                    "path": parsed.path, "status": response.status,
                })
            route.fulfill(response=response)
            return
        fixture.bridge(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=fixture.CHROMIUM,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_init_script("localStorage.setItem('sh-theme', 'dark');")
        context.add_cookies([fixture.synthetic_login_cookie()])
        context.route("**/*", bridge)
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "request",
            lambda request: document_requests.append(request.url)
            if request.resource_type == "document" else None,
        )

        normal_path = "/supplier-catalog/{}/products?{}".format(
            fixture_ids["supplier_id"], urlencode(FILTERS),
        )
        page.set_viewport_size({"width": 320, "height": 900})
        catalog_response = page.goto(
            fixture.BASE + "/supplier-catalog",
            wait_until="domcontentloaded",
        )
        assert catalog_response and catalog_response.status == 200
        page.wait_for_load_state("networkidle")
        supplier_card = page.locator(".sc-card").filter(
            has=page.locator(
                f'a[href="/supplier-catalog/{fixture_ids["supplier_id"]}/products"]',
            ),
        )
        card_freshness = supplier_card.locator(".sc-feed-freshness")
        assert card_freshness.is_visible()
        assert card_freshness.locator("time").count() == 2
        card_times = card_freshness.locator("time")
        assert card_times.nth(0).get_attribute("datetime") == "2026-09-21T10:15:00Z"
        assert card_times.nth(0).inner_text() == "21.09.2026 10:15 UTC"
        assert card_times.nth(1).get_attribute("datetime") == "2026-09-22T11:45:00Z"
        assert card_times.nth(1).inner_text() == "22.09.2026 11:45 UTC"
        assert "Автообновление включено" in card_freshness.inner_text()
        apply_button = supplier_card.locator(".sc-card-secondary")
        assert apply_button.is_visible()
        assert apply_button.inner_text() == "Проверить локальные изменения"
        assert "Сначала сравнить сохранённые данные с локальными карточками." in (
            apply_button.get_attribute("title")
        )
        all_review_form = apply_button.locator("xpath=ancestor::form")
        assert all_review_form.get_attribute("action") == SUPPLIER_REVIEW_PATH
        initial_all_scope = all_review_form.evaluate("""form => {
            const data = new FormData(form);
            return {
                supplierIds: data.getAll('supplier_id'),
                all: data.getAll('update_all_for_supplier'),
                sourceIds: data.getAll('update_product_ids'),
                csrf: data.getAll('csrf_token'),
            };
        }""")
        assert initial_all_scope["supplierIds"] == [str(fixture_ids["supplier_id"])]
        assert initial_all_scope["all"] == ["1"]
        assert initial_all_scope["sourceIds"] == []
        assert initial_all_scope["csrf"] and initial_all_scope["csrf"][0]
        assert posts == [] and fixture.REPORT["writes"] == []

        response = page.goto(
            fixture.BASE + normal_path,
            wait_until="domcontentloaded",
        )
        assert response and response.status == 200
        page.wait_for_load_state("networkidle")
        feed_disclosure = page.locator(".sc-feed-disclosure")
        feed_summary = feed_disclosure.locator(":scope > summary")
        assert not feed_disclosure.evaluate("element => element.open")
        feed_summary.focus()
        page.keyboard.press("Enter")
        assert feed_disclosure.evaluate("element => element.open"), (
            "keyboard activation should expand supplier freshness details",
        )
        page_freshness = page.locator(".sc-feed-freshness--page")
        assert page_freshness.is_visible()
        assert page_freshness.locator("time").count() == 2
        page_times = page_freshness.locator("time")
        assert page_times.nth(0).get_attribute("datetime") == "2026-09-21T10:15:00Z"
        assert page_times.nth(0).inner_text() == "21.09.2026 10:15 UTC"
        assert page_times.nth(1).get_attribute("datetime") == "2026-09-22T11:45:00Z"
        assert page_times.nth(1).inner_text() == "22.09.2026 11:45 UTC"
        assert "Цены и остатки · отдельный файл" in page_freshness.inner_text()
        assert "Автообновление включено" in page_freshness.inner_text()
        freshness_geometry = page_freshness.evaluate("""element => {
            const rect = element.getBoundingClientRect();
            return {
                left: rect.left,
                right: rect.right,
                top: rect.top,
                bottom: rect.bottom,
                clientWidth: element.clientWidth,
                scrollWidth: element.scrollWidth,
                viewportWidth: document.documentElement.clientWidth,
                documentScrollWidth: document.documentElement.scrollWidth,
                viewportHeight: window.innerHeight,
            };
        }""")
        assert freshness_geometry["viewportWidth"] == 320
        assert freshness_geometry["left"] >= 0
        assert freshness_geometry["right"] <= freshness_geometry["viewportWidth"]
        assert freshness_geometry["scrollWidth"] <= freshness_geometry["clientWidth"] + 1
        assert freshness_geometry["documentScrollWidth"] <= freshness_geometry["viewportWidth"] + 1
        assert freshness_geometry["top"] < freshness_geometry["viewportHeight"]
        assert freshness_geometry["bottom"] > 0
        assert posts == [] and fixture.REPORT["writes"] == []

        freshness_screenshot = "supplier-feed-freshness-dark-320.png"
        page_freshness.screenshot(
            path=REPORT_PATH.parent / freshness_screenshot,
            animations="disabled",
        )
        page.keyboard.press("Enter")
        assert not feed_disclosure.evaluate("element => element.open")
        assert not page_freshness.is_visible()
        checks.append(
            "supplier_feed_freshness_details_open_and_close_by_keyboard_without_writes",
        )

        category_source_id = fixture_ids["category_source_id"]
        category_label = page.locator(f"#cat-label-{category_source_id}")
        assert category_label.inner_text() == "Women's Legacy Category"
        category_label.click()
        category_modal = page.locator("#category-modal")
        category_modal.wait_for(state="visible")
        assert page.locator("#cat-current").inner_text() == (
            "Текущая: Women's Legacy Category (ID: 7000)"
        )
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/api/wb-subjects/search"
            and parse_qs(urlsplit(response.url).query, keep_blank_values=True)
            == {"q": ["Women's"]},
        ) as category_search_response:
            page.locator("#cat-search-input").fill("Women's")
        assert category_search_response.value.status == 200
        assert len(category_search_reads) == 1
        category_choice = category_modal.locator("#cat-search-results button").filter(
            has_text="Women's Clothing > Women's Jackets",
        )
        category_choice.wait_for(state="visible")
        assert category_choice.count() == 1
        page.evaluate("""() => {
            const nativeFetch = window.fetch.bind(window);
            window.__categorySaveGate = {pending: false, release: null};
            window.fetch = function(url, options) {
                const response = nativeFetch(url, options);
                if (!String(url).endsWith('/update-category')) return response;
                return response.then(value => new Promise(resolve => {
                    window.__categorySaveGate.pending = true;
                    window.__categorySaveGate.release = () => resolve(value);
                }));
            };
        }""")
        with page.expect_response(
            lambda response: urlsplit(response.url).path == category_update_path
            and response.request.method == "POST",
        ) as category_update_response:
            category_choice.click()
        assert category_update_response.value.status == 200
        assert len(category_update_posts) == 1
        assert category_update_posts[0]["payload"] == {
            "success": True,
            "wb_subject_id": 7901,
            "wb_category_name": "Women's Clothing > Women's Jackets",
        }
        page.wait_for_function("window.__categorySaveGate.pending === true")
        other_category_label = page.locator(
            f"#cat-label-{fixture_ids['eligible_ids'][1]}",
        )
        page.keyboard.press("Escape")
        assert category_modal.is_hidden()
        other_category_label.click()
        assert category_modal.is_visible()
        assert page.locator("#cat-current").inner_text() == (
            "Текущая: Synthetic WB category (ID: 7000)"
        )
        page.evaluate("window.__categorySaveGate.release()")
        page.wait_for_function(
            """id => document.getElementById('cat-label-' + id).textContent
                === "Women's Clothing > Women's Jackets".substring(0, 30)""",
            category_source_id,
        )
        assert category_modal.is_visible(), "late save for the previous product closed the active modal"
        assert page.locator("#cat-current").inner_text() == (
            "Текущая: Synthetic WB category (ID: 7000)"
        )
        assert other_category_label.inner_text() == "Synthetic WB category"
        page.keyboard.press("Escape")
        assert category_modal.is_hidden()
        with fixture.app.app_context():
            category_source = db.session.get(
                SupplierProduct, int(category_source_id),
            )
            assert category_source.wb_subject_id == 7901
            assert category_source.wb_category_name == (
                "Women's Clothing > Women's Jackets"
            )
            category_source.wb_subject_id = 7000
            category_source.wb_subject_name = "Synthetic WB subject"
            category_source.wb_category_name = "Women's Legacy Category"
            db.session.commit()
        category_label.click()
        category_modal.wait_for(state="visible")
        assert category_label.get_attribute("data-current-name") == (
            "Women's Clothing > Women's Jackets"
        )
        assert category_label.get_attribute("data-current-subject-id") == "7901"
        assert page.locator("#cat-current").inner_text() == (
            "Текущая: Women's Clothing > Women's Jackets (ID: 7901)"
        )
        page.keyboard.press("Escape")
        assert category_modal.is_hidden()
        checks.append(
            "supplier_category_editor_handles_apostrophes_and_reopens_exact_saved_label",
        )
        cards = page.locator(".sc-product-grid > .sc-product-tile")
        assert cards.count() == 50, cards.count()
        with fixture.app.app_context():
            assert ImportedProduct.query.count() == fixture_ids["imported_before"]
        standard_checks = page.locator(
            'input[form="supplier-import-form"][name="product_ids"]:checked',
        )
        assert standard_checks.count() == 0, standard_checks.count()
        all_filter_link = page.get_by_role(
            "link", name="Выбрать все 53 по фильтру", exact=True,
        )
        href = all_filter_link.get_attribute("href")
        assert href
        parsed_href = urlsplit(href)
        assert parsed_href.path == (
            f"/supplier-catalog/{fixture_ids['supplier_id']}/products"
        ), href
        assert parse_qs(parsed_href.query) == {
            **{key: [value] for key, value in FILTERS.items()},
            "page": ["1"],
            "select_all": ["1"],
        }, href
        checks.append("ordinary_page_keeps_50_rows_and_preserves_exact_filters_for_selection")

        all_filter_link.click()
        page.wait_for_load_state("networkidle")
        with fixture.app.app_context():
            assert ImportedProduct.query.count() == fixture_ids["imported_before"]
        all_checkboxes = page.locator(
            'input[form="supplier-import-form"][name="product_ids"]',
        )
        assert all_checkboxes.count() == 53, all_checkboxes.count()
        rendered_ids = all_checkboxes.evaluate_all(
            "items => items.map(item => item.value)",
        )
        assert set(rendered_ids) == set(fixture_ids["eligible_ids"])
        assert len(set(rendered_ids)) == 53
        assert all_checkboxes.evaluate_all(
            "items => items.every(item => item.checked)",
        )
        review_text = page.locator(
            '[data-supplier-products] [aria-live="polite"] strong',
        ).inner_text()
        assert review_text == "Выбрано 53 из 53", review_text
        assert page.locator("#supplier-selection-status").get_attribute("aria-live") == "polite"
        assert page.locator("#supplier-selection-count").count() == 1
        assert page.locator("#import-selection-count").count() == 0
        import_submit = page.locator('button[form="supplier-import-form"].sc-action-submit')
        assert import_submit.get_attribute("aria-describedby") == "supplier-selection-count"
        assert " ".join(import_submit.inner_text().split()) == "Импортировать товары (53)"
        for excluded_id in fixture_ids["excluded_ids"]:
            assert excluded_id not in rendered_ids, excluded_id
        assert page.get_by_text("R28 Mass batch out of stock", exact=True).count() == 0
        assert page.get_by_text("R28 Mass batch not parsed", exact=True).count() == 0
        assert page.get_by_text("R28 Mass batch from other supplier", exact=True).count() == 0
        assert page.get_by_role("link", name="Снять выбор", exact=True).count() == 1
        checks.append("all_filtered_result_checks_exact_53_current_supplier_rows_and_excludes_other_matches")

        excluded_ids = set(rendered_ids[:3])
        for product_id in excluded_ids:
            page.locator(
                f'input[form="supplier-import-form"][name="product_ids"][value="{product_id}"]',
            ).uncheck()
        review_text = page.locator(
            '[data-supplier-products] [aria-live="polite"] strong',
        ).inner_text()
        assert review_text == "Выбрано 50 из 53", review_text
        assert " ".join(import_submit.inner_text().split()) == "Импортировать товары (50)"

        selected_ids = set(rendered_ids) - excluded_ids
        submitted = page.evaluate("""() => ({
            supplierIds: new FormData(document.querySelector('#supplier-import-form')).getAll('supplier_id'),
            productIds: new FormData(document.querySelector('#supplier-import-form')).getAll('product_ids'),
        })""")
        assert submitted["supplierIds"] == [str(fixture_ids["supplier_id"])]
        assert set(submitted["productIds"]) == selected_ids
        assert len(submitted["productIds"]) == 50
        checks.append("three_explicit_exclusions_update_count_and_normal_form_contains_exact_50_ids")

        with page.expect_response(
            lambda result: urlsplit(result.url).path == IMPORT_PATH
            and result.request.method == "POST",
        ) as imported:
            page.get_by_role("button", name="Импортировать товары").click()
        assert imported.value.status == 303, "supplier import must redirect to its exact committed set"
        redirect = urlsplit(imported.value.headers.get("location", ""))
        redirect_query = parse_qs(redirect.query, keep_blank_values=True)
        assert redirect.path == "/my-products/beta", "supplier import must open the modern workspace"
        assert set(redirect_query) == {"import_batch"}, "redirect must carry only the signed import batch"
        batch_tokens = redirect_query.get("import_batch", [])
        assert len(batch_tokens) == 1 and 0 < len(batch_tokens[0]) <= 8192, (
            "redirect must carry one bounded signed import batch",
        )
        batch_token = batch_tokens[0]
        safe_page_tokens = [batch_token]

        def safe_page_errors():
            messages = []
            for error in page_errors:
                for token in safe_page_tokens:
                    error = error.replace(token, "[redacted]")
                messages.append(error)
            return messages

        def assert_batch_page(expected_product_ids):
            page.get_by_role("heading", name="Товары из импорта", exact=True).wait_for()
            page.wait_for_load_state("networkidle")
            location = page.evaluate("() => ({path: location.pathname, search: location.search})")
            location_query = parse_qs(location["search"].removeprefix("?"), keep_blank_values=True)
            assert location["path"] == "/my-products/beta", "batch page path changed"
            assert set(location_query) == {"import_batch"}, "batch page lost its exact scope"
            assert location_query.get("import_batch") == [batch_token], (
                "batch page scope differs from the redirect",
            )
            assert page.locator(".mp-list > .mp-row").count() == 50, (
                "batch page must render exactly 50 imported rows",
            )
            state = page.evaluate("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy ? {
                    importBatchMode: proxy.importBatchMode,
                    items: proxy.items.map(item => item.id),
                    selected: proxy.selected.slice(),
                    loading: proxy.loading,
                } : null;
            }""")
            assert state is not None and state["importBatchMode"] and not state["loading"], (
                "batch page did not finish loading its fixed scope",
            )
            assert len(state["items"]) == 50 and set(state["items"]) == expected_product_ids, (
                "batch UI differs from the persisted imported product set",
            )
            return state

        assert len(posts) == 1, posts
        post_body = posts[0]["body"]
        assert post_body.get("supplier_id") == [str(fixture_ids["supplier_id"])]
        assert set(post_body.get("product_ids", [])) == selected_ids
        assert len(post_body.get("product_ids", [])) == 50
        with fixture.app.app_context():
            persisted = ImportedProduct.query.filter(
                ImportedProduct.seller_id == fixture.FIXTURE["seller_id"],
                ImportedProduct.supplier_id == fixture_ids["supplier_id"],
                ImportedProduct.supplier_product_id.in_(fixture_ids["eligible_ids"]),
            ).all()
            persisted_source_ids = {
                str(row.supplier_product_id) for row in persisted
            }
            assert persisted_source_ids == selected_ids, "only the exact selected source rows may persist"
            persisted_product_ids = {row.id for row in persisted}
            assert len(persisted_product_ids) == 50
            all_workspace_product_ids = {
                row.id for row in ImportedProduct.query.filter_by(
                    seller_id=fixture.FIXTURE["seller_id"],
                ).all()
            }
            assert persisted_product_ids <= all_workspace_product_ids
        imported_count = len(persisted_source_ids)
        with fixture.app.app_context():
            assert ImportedProduct.query.count() == fixture_ids["imported_before"] + imported_count
        try:
            first_state = assert_batch_page(persisted_product_ids)
            assert first_state["selected"] == [], "import batch must not preselect rows"
            assert len(product_feed_reads) == 1, "batch view must make one product feed read"
            assert product_feed_reads[0]["query_keys"] == ("import_batch",)
            assert product_feed_reads[0]["batch_tokens"] == (batch_token,)
            assert product_feed_reads[0]["status"] == 200
            first_payload = product_feed_reads[0]["payload"]
            pagination = first_payload.get("pagination") if isinstance(first_payload, dict) else None
            first_items = first_payload.get("items") if isinstance(first_payload, dict) else None
            assert isinstance(first_payload, dict) and first_payload.get("success") is True
            assert first_payload.get("import_batch_count") == 50
            assert isinstance(first_items, list) and len(first_items) == 50
            assert {item.get("id") for item in first_items if isinstance(item, dict)} == persisted_product_ids
            assert isinstance(pagination, dict) and pagination == {
                "page": 1,
                "per_page": 200,
                "pages": 1,
                "total": 50,
                "has_next": False,
            }
            assert facet_reads == [], "batch view must not request global facets"
            assert category_reads == [], "exact batch view must not request category options"
            checks.append("modern_redirect_loads_exactly_50_persisted_seller_owned_rows_without_facets")

            page.get_by_role("checkbox", name="Выбрать товары из импорта").check()
            selected_state = page.evaluate("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy ? proxy.selected.slice() : null;
            }""")
            assert selected_state is not None and set(selected_state) == persisted_product_ids
            page.locator(".mp-list > .mp-row").first.locator(".mp-check input").uncheck()
            selected_after_one = page.evaluate("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy ? proxy.selected.slice() : null;
            }""")
            assert selected_after_one is not None and len(selected_after_one) == 49
            assert set(selected_after_one) < persisted_product_ids
            assert len(product_feed_reads) == 1 and facet_reads == []
            create_launcher = workspace_action_launcher(page, "create")
            assert invoke_workspace_action(page, "ozon") == 2
            ozon_dialog = page.locator('dialog[aria-labelledby="mp-ozon-dialog-title"]')
            ozon_dialog.wait_for(state="visible")
            assert page.locator("#mp-ozon-dialog-title").inner_text().startswith("Подготовить 49 ")
            preparation_account = ozon_dialog.locator("[data-ozon-preparation-account]")
            assert preparation_account.count() == 1 and preparation_account.is_visible()
            assert preparation_account.inner_text().strip() == fixture.OZON_ACCOUNT_LABEL
            assert ozon_dialog.locator("#mp-ozon-account").count() == 0
            assert ozon_dialog.locator(".mp-ozon-confirm").count() == 0
            assert ozon_dialog.locator('input[type="checkbox"], select').count() == 0
            page.wait_for_function("document.activeElement?.id === 'mp-ozon-dialog-title'")
            assert ozon_dialog.locator("#mp-ozon-dialog-title").evaluate(
                "element => element === document.activeElement",
            )
            assert page.get_by_role("button", name="Подготовить черновики").is_enabled()
            assert len(posts) == 1 and fixture.REPORT["provider_attempts"] == 0
            assert len(product_feed_reads) == 1 and facet_reads == []

            page.keyboard.press("Escape")
            ozon_dialog.wait_for(state="hidden")
            assert create_launcher.evaluate("element => element === document.activeElement")
            assert page.evaluate("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy ? proxy.selected.slice() : null;
            }""") == selected_after_one
            assert len(posts) == 1 and fixture.REPORT["provider_attempts"] == 0

            assert invoke_workspace_action(page, "ozon") == 2
            ozon_dialog.wait_for(state="visible")
            preparation_account = ozon_dialog.locator("[data-ozon-preparation-account]")
            assert preparation_account.inner_text().strip() == fixture.OZON_ACCOUNT_LABEL
            assert ozon_dialog.locator("#mp-ozon-account").count() == 0
            assert ozon_dialog.locator(".mp-ozon-confirm").count() == 0
            page.wait_for_function("document.activeElement?.id === 'mp-ozon-dialog-title'")
            assert page.get_by_role("button", name="Подготовить черновики").is_enabled()
            page.get_by_role("button", name="Вернуться", exact=True).click()
            ozon_dialog.wait_for(state="hidden")
            assert create_launcher.evaluate("element => element === document.activeElement")
            assert page.evaluate("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy ? proxy.selected.slice() : null;
            }""") == selected_after_one
            assert len(posts) == 1 and fixture.REPORT["provider_attempts"] == 0
            checks.append("explicit_import_selection_can_review_49_for_ozon_without_confirming_a_write")

            page.reload(wait_until="domcontentloaded")
            reloaded_state = assert_batch_page(persisted_product_ids)
            assert reloaded_state["selected"] == []
            assert len(product_feed_reads) == 2, "reload must re-read the exact batch once"
            assert all(read["query_keys"] == ("import_batch",) for read in product_feed_reads)
            assert all(read["batch_tokens"] == (batch_token,) for read in product_feed_reads)
            assert all(read["status"] == 200 for read in product_feed_reads)
            reloaded_payload = product_feed_reads[1]["payload"]
            reloaded_items = reloaded_payload.get("items") if isinstance(reloaded_payload, dict) else None
            assert isinstance(reloaded_payload, dict) and reloaded_payload.get("success") is True
            assert reloaded_payload.get("import_batch_count") == 50
            assert isinstance(reloaded_items, list) and len(reloaded_items) == 50
            assert {item.get("id") for item in reloaded_items if isinstance(item, dict)} == persisted_product_ids
            assert reloaded_payload.get("pagination") == pagination
            assert facet_reads == []
            assert category_reads == [], "reloaded exact batch must not request category options"
            page.set_viewport_size({"width": 320, "height": 900})
            dimensions = page.evaluate("""() => ({
                scroll: document.documentElement.scrollWidth,
                viewport: document.documentElement.clientWidth,
            })""")
            assert dimensions["viewport"] == 320 and dimensions["scroll"] <= 321, (
                "import batch must fit a 320px viewport",
            )
            screenshot_name = "supplier-filtered-import-batch-dark-320.png"
            page.screenshot(path=REPORT_PATH.parent / screenshot_name, animations="disabled")
            feeds_before_workspace_switch = len(product_feed_reads)
            facets_before_workspace_switch = len(facet_reads)
            categories_before_workspace_switch = len(category_reads)
            documents_before_workspace_switch = len(document_requests)
            assert categories_before_workspace_switch == 0
            with page.expect_response(
                lambda response: urlsplit(response.url).path == "/api/my-products"
                and response.request.method == "GET"
                and "import_batch" not in parse_qs(
                    urlsplit(response.url).query, keep_blank_values=True,
                )
                and "supplier_refresh_batch" not in parse_qs(
                    urlsplit(response.url).query, keep_blank_values=True,
                ),
            ) as full_workspace_response, page.expect_response(
                lambda response: urlsplit(response.url).path == "/api/my-products/categories"
                and response.request.method == "GET"
                and parse_qs(
                    urlsplit(response.url).query, keep_blank_values=True,
                ) == {"page": ["1"]},
            ) as category_options_response:
                page.get_by_role("button", name="Все мои товары", exact=True).click()
            assert full_workspace_response.value.status == 200
            assert category_options_response.value.status == 200
            page.wait_for_function("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy && !proxy.loading && !proxy.importBatchMode &&
                    !proxy.supplierRefreshBatchMode && Array.isArray(proxy.items) &&
                    proxy.items.length > 0 && proxy.pagination.total > 0;
            }""")
            page.wait_for_function("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy && !proxy.catalogCategoriesLoading &&
                    proxy.catalogCategoriesPage === 1;
            }""")
            normal_location = page.evaluate("() => ({path: location.pathname, search: location.search})")
            assert normal_location["path"] == "/my-products/beta"
            assert "import_batch" not in parse_qs(
                normal_location["search"].removeprefix("?"), keep_blank_values=True,
            )
            assert len(document_requests) == documents_before_workspace_switch, (
                "switching from the signed import batch must not request another document",
            )
            assert len(product_feed_reads) == feeds_before_workspace_switch + 1, (
                "workspace switch must perform one ordinary product feed read",
            )
            assert len(category_reads) == categories_before_workspace_switch + 1, (
                "workspace switch must request the first category options page once",
            )
            first_category_read = category_reads[-1]
            assert first_category_read["query_keys"] == ("page",)
            assert first_category_read["pages"] == ("1",)
            assert first_category_read["status"] == 200
            first_category_payload = first_category_read["payload"]
            assert isinstance(first_category_payload, dict)
            assert first_category_payload.get("success") is True
            assert first_category_payload.get("page") == 1
            assert isinstance(first_category_payload.get("options"), list)
            expected_category = "Synthetic WB category"
            batch_category_labels = {
                item.get("category") for item in reloaded_items
                if isinstance(item, dict) and isinstance(item.get("category"), str)
            }
            assert expected_category in batch_category_labels, (
                "the exact imported batch should retain its fixture category",
                batch_category_labels,
            )
            assert any(
                option.get("key") == "label:" + expected_category and
                option.get("label") == expected_category
                for option in first_category_payload["options"]
                if isinstance(option, dict)
            )
            rendered_category_options = page.locator(
                "[data-catalog-category] option",
            ).evaluate_all("""options => options.map(option => ({
                value: option.value,
                label: option.textContent.trim(),
            }))""")
            assert any(
                option["value"] == "label:" + expected_category and
                option["label"] == expected_category
                for option in rendered_category_options
            ), "the imported category must be present in the visible category selector"
            full_workspace_read = product_feed_reads[-1]
            assert full_workspace_read["query_keys"] == ("page", "per_page"), (
                "the default workspace omits empty filters from its feed URL",
                full_workspace_read["query_keys"],
            )
            assert full_workspace_read["status"] == 200
            full_workspace_payload = full_workspace_read["payload"]
            assert isinstance(full_workspace_payload, dict)
            assert full_workspace_payload.get("success") is True
            full_workspace_items = full_workspace_payload.get("items")
            full_workspace_pagination = full_workspace_payload.get("pagination")
            assert isinstance(full_workspace_items, list) and isinstance(
                full_workspace_pagination, dict,
            )
            assert full_workspace_pagination["total"] == len(all_workspace_product_ids)
            assert full_workspace_pagination["page"] == 1
            assert full_workspace_pagination["per_page"] == 50
            assert full_workspace_pagination["pages"] == (
                (len(all_workspace_product_ids) + 49) // 50
            )
            assert full_workspace_pagination["has_next"] == (
                len(all_workspace_product_ids) > 50
            )
            full_workspace_item_ids = {
                item.get("id") for item in full_workspace_items if isinstance(item, dict)
            }
            assert len(full_workspace_items) == min(len(all_workspace_product_ids), 50)
            assert len(full_workspace_item_ids) == len(full_workspace_items)
            assert full_workspace_item_ids <= all_workspace_product_ids
            full_workspace_state = page.evaluate("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy ? {
                    importBatchMode: proxy.importBatchMode,
                    supplierRefreshBatchMode: proxy.supplierRefreshBatchMode,
                    items: proxy.items.map(item => item.id),
                    selected: proxy.selected.slice(),
                    pagination: proxy.pagination,
                    loading: proxy.loading,
                } : null;
            }""")
            assert full_workspace_state is not None
            assert not full_workspace_state["importBatchMode"]
            assert not full_workspace_state["supplierRefreshBatchMode"]
            assert not full_workspace_state["loading"]
            assert full_workspace_state["selected"] == []
            assert set(full_workspace_state["items"]) == full_workspace_item_ids
            assert full_workspace_state["pagination"]["total"] == len(all_workspace_product_ids)
            assert len(facet_reads) == facets_before_workspace_switch + 1
            assert len(posts) == 1 and fixture.REPORT["provider_attempts"] == 0
            checks.append(
                "batch_reload_skips_category_options_then_same_document_exit_loads_page_one"
            )
            checks.append(
                "batch_reload_keeps_exact_50_then_same_document_exit_loads_full_workspace_at_320px"
            )
        except Exception as error:
            message = str(error).replace(batch_token, "[redacted]")
            if not message:
                frames = traceback.extract_tb(error.__traceback__)
                location = frames[-1] if frames else None
                message = (
                    f"{type(error).__name__} at "
                    f"{Path(location.filename).name}:{location.lineno}"
                    if location else type(error).__name__
                )
            raise AssertionError(f"import batch browser assertion failed: {message}") from None
        checks.append("normal_post_persists_only_the_exact_selected_owned_supplier_rows")
        checks.append("import_batch_mobile_screenshot_saved")

        # Reopen the signed receipt after the R31 53 → 50 → 49 Ozon review.
        # This terminal WB create proof selects only two retained rows, so one
        # actual partial result does not depend on the whole import batch.
        batch_query = urlencode({"import_batch": batch_token})
        batch_response = page.goto(
            fixture.BASE + "/my-products/beta?" + batch_query,
            wait_until="domcontentloaded",
        )
        assert batch_response and batch_response.status == 200
        batch_state = assert_batch_page(persisted_product_ids)
        assert batch_state["selected"] == []
        assert product_feed_reads[-1]["query_keys"] == ("import_batch",)
        assert product_feed_reads[-1]["batch_tokens"] == (batch_token,)
        assert product_feed_reads[-1]["status"] == 200
        assert product_feed_reads[-1]["payload"].get("import_batch_count") == 50
        page.set_viewport_size({"width": 320, "height": 900})
        batch_item_ids = page.evaluate("""() => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy ? proxy.items.map(item => item.id) : null;
        }""")
        assert isinstance(batch_item_ids, list) and len(batch_item_ids) == 50
        wb_selected_ids = batch_item_ids[-2:]
        assert len(wb_selected_ids) == 2 and len(set(wb_selected_ids)) == 2
        assert set(wb_selected_ids) <= persisted_product_ids
        for product_id in wb_selected_ids:
            page.locator(
                f'.mp-row .mp-check input[type="checkbox"][value="{product_id}"]',
            ).check()
        page.mouse.move(100, 450)
        page.mouse.wheel(0, 600)
        page.wait_for_function("""() => {
            const rail = document.querySelector('.mp-bulkbar');
            const selected = Array.from(document.querySelectorAll('.mp-row.is-selected'));
            if (!rail || selected.length !== 2) return false;
            const railTop = rail.getBoundingClientRect().top;
            const rowRects = selected.map(row => row.getBoundingClientRect());
            return window.scrollY + window.innerHeight >=
                    document.documentElement.scrollHeight - 1 &&
                rowRects.every(rect => rect.top >= 0 && rect.bottom <= railTop);
        }""")
        before_confirmation_position = page.evaluate("""() => {
            const selectedRows = Array.from(document.querySelectorAll('.mp-row.is-selected'));
            const rects = selectedRows.map(row => row.getBoundingClientRect());
            const rail = document.querySelector('.mp-bulkbar');
            const railRect = rail && rail.getBoundingClientRect();
            return {
                scrollY: window.scrollY,
                viewportHeight: window.innerHeight,
                pageHeight: document.documentElement.scrollHeight,
                selectedCount: selectedRows.length,
                selectedIds: selectedRows.map(row => row.querySelector('.mp-check input')?.value),
                railTop: railRect && railRect.top,
                railBottom: railRect && railRect.bottom,
                railVisible: !!railRect && railRect.top < window.innerHeight && railRect.bottom > 0,
                selectedRowsVisible: rects.length === 2 && rects.every(rect =>
                    rect.top >= 0 && rect.bottom <= window.innerHeight &&
                    !!railRect && rect.bottom <= railRect.top
                ),
            };
        }""")
        assert before_confirmation_position["scrollY"] + before_confirmation_position[
            "viewportHeight"
        ] >= before_confirmation_position["pageHeight"] - 1, (
            "user scroll should reach the bottom of the signed batch",
            before_confirmation_position,
        )
        assert before_confirmation_position["pageHeight"] > before_confirmation_position["viewportHeight"]
        assert before_confirmation_position["selectedCount"] == 2
        assert before_confirmation_position["selectedIds"] == [str(value) for value in wb_selected_ids]
        assert before_confirmation_position["railVisible"]
        assert before_confirmation_position["selectedRowsVisible"], before_confirmation_position
        checks.append("last_two_signed_batch_rows_are_selected_from_a_measured_scrolled_position")
        selected_before_create = page.evaluate("""() => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy ? proxy.selected.slice() : null;
        }""")
        assert selected_before_create == wb_selected_ids
        create_launcher = page.locator('button[data-bulk-action-group="create"]')
        assert create_launcher.count() == 1 and create_launcher.is_enabled()
        direct_create_action = create_launcher.get_attribute("data-bulk-direct-action")
        if direct_create_action:
            assert direct_create_action == "push_wb"
        else:
            create_launcher.click()
            wb_create_choice = page.locator('[data-bulk-action-key="push_wb"]')
            wb_create_choice.wait_for(state="visible")
            assert wb_create_choice.locator(
                ".mp-bulk-intent-option-label",
            ).inner_text() == "Создать карточки WB"
            assert wb_create_choice.locator(
                ".mp-bulk-intent-option-hint",
            ).inner_text() == "для 2 товаров, ещё не связанных с WB"
            create_launcher.click()
            assert not wb_create_choice.is_visible()
        invoke_workspace_action(page, "push_wb")
        confirmation = page.get_by_role("alertdialog")
        confirmation.wait_for(state="visible")
        confirmation_scroll_y = page.evaluate("() => window.scrollY")
        assert confirmation_scroll_y > 0, (
            "confirmation should preserve the scrolled batch context",
            confirmation_scroll_y,
        )

        from services.upload_readiness_validator import validate_product_upload_readiness
        from services.wb_product_importer import WBProductImporter

        fake_wb_calls = []
        wb_worker_started = threading.Event()
        wb_worker_release = threading.Event()
        wb_worker_returned = threading.Event()
        original_batch_import = WBProductImporter.batch_import_products_to_wb

        def deterministic_wb_batch_import(self, imported_products, progress_callback=None):
            product_ids = [product.id for product in imported_products]
            assert product_ids == wb_selected_ids, (
                "real BackgroundJob worker widened or reordered the selected product set",
            )
            checks_by_product = []
            for product in imported_products:
                readiness = validate_product_upload_readiness(
                    product,
                    seller=db.session.get(Seller, fixture.FIXTURE["seller_id"]),
                )
                checks_by_product.append({
                    "id": product.id,
                    "status": product.import_status,
                    "is_ready": readiness["is_ready"],
                    "can_push": readiness["can_push"],
                })
            assert all(
                item["status"] == "validated" and item["is_ready"] and item["can_push"]
                for item in checks_by_product
            ), checks_by_product

            outcomes = []
            for index, product in enumerate(imported_products):
                success = index == 0
                outcomes.append({
                    "product_id": product.id,
                    "title": (product.title or "")[:50],
                    "status": "success" if success else "failed",
                    "error": None if success else "Synthetic WB rejected this fixture card",
                    "nm_id": 700000 + index if success else None,
                    "warnings": [],
                })
            fake_wb_calls.append({
                "product_ids": product_ids,
                "readiness": checks_by_product,
                "outcomes": outcomes,
            })
            wb_worker_started.set()
            try:
                if not wb_worker_release.wait(20):
                    raise RuntimeError("synthetic WB worker release timed out")
                if progress_callback:
                    progress_callback("done", {"items": outcomes})
                return outcomes
            finally:
                wb_worker_returned.set()

        WBProductImporter.batch_import_products_to_wb = deterministic_wb_batch_import
        try:
            create_button = confirmation.get_by_role(
                "button", name="Создать карточки", exact=True,
            )
            assert create_button.is_enabled()
            feed_reads_before_create = len(product_feed_reads)
            facet_reads_before_create = len(facet_reads)
            with page.expect_response(
                lambda response: urlsplit(response.url).path == WB_CREATE_PATH
                and response.request.method == "POST",
            ) as created:
                create_button.click()
            create_response = created.value
            assert create_response.status == 202, (
                "new WB create action must return one accepted keyed receipt",
                create_response.status,
            )
            confirmation.wait_for(state="hidden")
            create_payload = create_response.json()
            assert isinstance(create_payload, dict) and set(create_payload) == {
                "success", "job_uid", "total", "product_ids", "status", "replayed",
                "worker_exited", "cancel_requested", "result_counts",
            }
            assert create_payload.get("success") is True
            job_uid = create_payload.get("job_uid")
            assert isinstance(job_uid, str) and re.fullmatch(
                r"bulk_wb_[0-9a-f]{56}", job_uid,
            ), "WB create response must identify the actual durable job"
            assert create_payload["total"] == len(wb_selected_ids) == 2
            assert create_payload["product_ids"] == wb_selected_ids
            assert create_payload["status"] in {"pending", "running"}
            assert create_payload["replayed"] is False
            assert create_payload["worker_exited"] is False
            assert create_payload["cancel_requested"] is False
            assert create_payload["result_counts"] is None
            assert page.get_by_role("alertdialog").count() == 0
            assert len(wb_create_posts) == 1, {
                "count": len(wb_create_posts),
                "paths": [entry.get("path") for entry in wb_create_posts],
            }
            create_post = wb_create_posts[0]
            assert create_post["path"] == WB_CREATE_PATH
            assert create_post["status"] == 202
            assert create_post["payload"] == create_payload
            assert set(create_post["body"]) == {"product_ids", "request_key"}
            assert create_post["body"]["product_ids"] == wb_selected_ids
            assert set(create_post["body"]["product_ids"]) <= persisted_product_ids
            request_key = create_post["body"]["request_key"]
            assert isinstance(request_key, str) and re.fullmatch(r"[a-f0-9]{32}", request_key)
            checks.append("signed_import_batch_posts_one_exact_two_product_wb_create_request")

            receipt = page.locator("[data-wb-create-receipt]")
            receipt.wait_for(state="visible")
            page.wait_for_function("""() => {
                const receipt = document.querySelector('[data-wb-create-receipt]');
                return receipt && receipt.dataset.wbCreateState === 'accepted';
            }""")
            worker_deadline = time.monotonic() + 10
            while not wb_worker_started.is_set() and time.monotonic() < worker_deadline:
                page.wait_for_timeout(25)
            assert wb_worker_started.is_set(), "real WB worker did not enter the synthetic importer"
            with fixture.app.app_context():
                from models import BackgroundJob

                accepted_job = BackgroundJob.query.filter_by(
                    job_uid=job_uid,
                    seller_id=fixture.FIXTURE["seller_id"],
                ).one()
                assert accepted_job.status == "running"
                assert accepted_job.total == 2 and accepted_job.processed == 0
                assert accepted_job.get_progress()["product_ids"] == wb_selected_ids

            with page.expect_response(
                lambda response: urlsplit(response.url).path == WB_CREATE_RESULT_PATH
                and response.request.method == "GET",
            ) as active_result_read:
                page.locator("[data-wb-create-reconcile]").click()
            active_response = active_result_read.value
            assert active_response.status == 200
            active_payload = active_response.json()
            assert set(active_payload) == {
                "success", "job_uid", "total", "product_ids", "status", "replayed",
                "worker_exited", "cancel_requested", "result_counts",
            }
            assert active_payload["success"] is True
            assert active_payload["job_uid"] == job_uid
            assert active_payload["total"] == 2 and active_payload["product_ids"] == wb_selected_ids
            assert active_payload["status"] == "running" and active_payload["replayed"] is True
            assert active_payload["worker_exited"] is False
            assert active_payload["cancel_requested"] is False
            assert active_payload["result_counts"] is None
            active_read = next(
                read for read in reversed(wb_create_result_reads)
                if read["request_key"] == request_key and read["payload"] == active_payload
            )
            assert active_read["query_keys"] == () and active_read["status"] == 200
            assert len(wb_create_posts) == 1 and len(fake_wb_calls) == 1
            assert page.get_by_role("alertdialog").count() == 0
            checks.append("accepted_wb_create_is_read_only_by_exact_typed_key_receipt")

            wb_worker_release.set()
            worker_return_deadline = time.monotonic() + 10
            while not wb_worker_returned.is_set() and time.monotonic() < worker_return_deadline:
                page.wait_for_timeout(25)
            assert wb_worker_returned.is_set(), "synthetic WB worker did not return"

            page.wait_for_function("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                const receipt = document.querySelector('[data-wb-create-receipt]');
                return proxy && !proxy.loading && !proxy.wbCreatePending &&
                    proxy.wbCreateTerminalResult?.status === 'completed' &&
                    receipt && receipt.dataset.wbCreateState === 'terminal_result';
            }""", timeout=20000)
            terminal_reads = [
                read for read in wb_create_result_reads
                if read["request_key"] == request_key and read["status"] == 200
                and isinstance(read["payload"], dict)
                and read["payload"].get("worker_exited") is True
                and read["payload"].get("status") == "completed"
            ]
            assert terminal_reads, (
                "browser did not observe the terminal typed WB receipt",
                [read["status"] for read in wb_create_result_reads],
            )
            terminal_receipt_read = terminal_reads[-1]
            terminal_payload = terminal_receipt_read["payload"]
            assert set(terminal_payload) == {
                "success", "job_uid", "total", "product_ids", "status", "replayed",
                "worker_exited", "cancel_requested", "result_counts",
            }
            assert terminal_payload["job_uid"] == job_uid
            assert terminal_payload["total"] == 2 and terminal_payload["product_ids"] == wb_selected_ids
            assert terminal_payload["status"] == "completed"
            assert terminal_payload["replayed"] is True and terminal_payload["worker_exited"] is True
            assert terminal_payload["cancel_requested"] is False
            assert terminal_payload["result_counts"] == {
                "processed": 2, "succeeded": 1, "failed": 1,
            }
            assert terminal_receipt_read["query_keys"] == ()
            assert terminal_receipt_read["sequence"] > active_read["sequence"]

            assert len(fake_wb_calls) == 1
            assert fake_wb_calls[0]["product_ids"] == wb_selected_ids
            assert all(item["is_ready"] and item["can_push"] for item in fake_wb_calls[0]["readiness"])
            with fixture.app.app_context():
                job = BackgroundJob.query.filter_by(
                    job_uid=job_uid,
                    seller_id=fixture.FIXTURE["seller_id"],
                ).one()
                assert job.status == "completed"
                assert job.total == 2 and job.processed == 2
                assert job.succeeded == 1 and job.failed_count == 1
                assert job.get_progress()["product_ids"] == wb_selected_ids
                assert [row["product_id"] for row in job.get_progress()["items"]] == wb_selected_ids
                assert job.get_result()["imported"] == 1 and job.get_result()["failed"] == 1

            page.evaluate("""() => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
            terminal_screenshot = "supplier-filtered-import-wb-create-terminal-dark-320.png"
            receipt = page.locator("[data-wb-create-receipt]")
            receipt.screenshot(
                path=REPORT_PATH.parent / terminal_screenshot,
                animations="disabled",
            )
            assert receipt.get_attribute("role") == "status"
            assert receipt.get_attribute("aria-live") == "polite"
            receipt_text = receipt.inner_text()
            assert "Локальная задача создания карточек завершилась" in receipt_text
            assert "В Seller Hub обработано: 2 из 2" in receipt_text
            assert "успешно: 1" in receipt_text
            assert "не созданы: 1" in receipt_text
            receipt_position = receipt.evaluate("""element => {
                const rect = element.getBoundingClientRect();
                const header = document.querySelector('.mobile-header');
                const headerRect = header && header.getBoundingClientRect();
                const active = document.activeElement;
                const activeRect = active && active.getBoundingClientRect();
                return {
                    scrollY: window.scrollY,
                    top: rect.top,
                    bottom: rect.bottom,
                    left: rect.left,
                    right: rect.right,
                    headerBottom: headerRect ? headerRect.bottom : 0,
                    headerPosition: header ? getComputedStyle(header).position : '',
                    viewportWidth: document.documentElement.clientWidth,
                    viewportHeight: window.innerHeight,
                    activeConnected: !!active && active.isConnected,
                    activeVisible: !!activeRect && activeRect.bottom > 0 &&
                        activeRect.top < window.innerHeight,
                    activeInDialog: !!active && !!active.closest('[role="alertdialog"]'),
                };
            }""")
            assert receipt_position["headerPosition"] == "fixed", receipt_position
            assert receipt_position["top"] >= receipt_position["headerBottom"], receipt_position
            assert receipt_position["bottom"] <= receipt_position["viewportHeight"], receipt_position
            assert receipt_position["left"] >= 0
            assert receipt_position["right"] <= receipt_position["viewportWidth"]
            assert receipt_position["activeConnected"] and receipt_position["activeVisible"]
            assert not receipt_position["activeInDialog"]
            assert receipt_position["scrollY"] < confirmation_scroll_y, (
                "accepted create should reveal its live receipt from the deep batch position",
                receipt_position,
            )
            checks.append("accepted_create_reveals_live_receipt_below_fixed_mobile_header")
            checks.append("typed_terminal_receipt_renders_one_success_and_one_distinct_failure")

            viewport_geometry = page.evaluate("""() => ({
                width: document.documentElement.clientWidth,
                scrollWidth: document.documentElement.scrollWidth,
            })""")
            assert viewport_geometry["width"] == 320
            assert viewport_geometry["scrollWidth"] <= viewport_geometry["width"] + 1
            dismiss_button = receipt.locator("[data-wb-create-dismiss]")
            assert dismiss_button.count() == 1 and dismiss_button.is_visible()
            dismiss_geometry = dismiss_button.evaluate("""element => {
                const rect = element.getBoundingClientRect();
                return {width: rect.width, height: rect.height};
            }""")
            assert dismiss_geometry["width"] >= 44 and dismiss_geometry["height"] >= 44, dismiss_geometry
            dismiss_button.focus()
            page.keyboard.press("Shift+Tab")
            page.keyboard.press("Tab")
            dismiss_focus = dismiss_button.evaluate("""element => {
                const style = getComputedStyle(element);
                return {
                    focused: document.activeElement === element,
                    focusVisible: element.matches(':focus-visible'),
                    outlineStyle: style.outlineStyle,
                    outlineWidth: parseFloat(style.outlineWidth) || 0,
                };
            }""")
            assert dismiss_focus["focused"] and dismiss_focus["focusVisible"], dismiss_focus
            assert dismiss_focus["outlineStyle"] != "none" and dismiss_focus["outlineWidth"] >= 2, dismiss_focus
            checks.append("320px_terminal_receipt_dismiss_is_44px_keyboard_visible_and_overflow_free")

            refresh_deadline = time.monotonic() + 5
            while time.monotonic() < refresh_deadline and not any(
                read["sequence"] > terminal_receipt_read["sequence"]
                and read["batch_tokens"] == (batch_token,)
                for read in product_feed_reads
            ):
                page.wait_for_timeout(50)
            refreshed_reads = [
                read for read in product_feed_reads
                if read["sequence"] > terminal_receipt_read["sequence"]
                and read["batch_tokens"] == (batch_token,)
            ]
            assert refreshed_reads, "terminal result must refresh the signed import-batch product list"
            assert refreshed_reads[-1]["status"] == 200
            assert refreshed_reads[-1]["payload"].get("import_batch_count") == 50
            assert {
                item.get("id") for item in refreshed_reads[-1]["payload"].get("items", [])
            } == persisted_product_ids
            assert len(product_feed_reads) >= feed_reads_before_create + 1
            assert len(facet_reads) == facet_reads_before_create, (
                "terminal import-batch refresh must stay exact and avoid global facets",
            )
            assert page.locator(".mp-list > .mp-row").count() == 50
            checks.append("terminal_receipt_refreshes_the_same_exact_signed_batch_product_list")

            assert not any(
                read["path"] == JOB_STATUS_PREFIX + job_uid for read in job_status_reads
            ), "keyed create finality must come from the typed key receipt, not generic job polling"
            observed_receipt_get_count = len(wb_create_result_reads)
            page.wait_for_timeout(2700)
            assert len(wb_create_result_reads) == observed_receipt_get_count, (
                "terminal typed receipt must stop polling after the exact completed status"
            )
            assert len(wb_create_posts) == 1, "terminal UI must not resubmit WB creation"
            dismiss_button.click()
            receipt.wait_for(state="hidden")
            assert page.locator("[data-wb-create-receipt]").count() == 0
            assert len(wb_create_posts) == 1
            checks.append("terminal_receipt_stops_polling_and_hides_without_retry_or_resubmit")

            wb_create_report = {
                "selected_count": len(wb_selected_ids),
                "terminal_status": terminal_payload["status"],
                "succeeded_count": terminal_payload["result_counts"]["succeeded"],
                "failed_count": terminal_payload["result_counts"]["failed"],
                "screenshot": terminal_screenshot,
                "physical_post_count": len(wb_create_posts),
                "typed_receipt_get_count": observed_receipt_get_count,
                "worker_invocation_count": len(fake_wb_calls),
            }
        finally:
            wb_worker_release.set()
            WBProductImporter.batch_import_products_to_wb = original_batch_import

        oversized_path = "/supplier-catalog/{}/products?{}".format(
            fixture_ids["supplier_id"],
            urlencode({
                "search": "R28 Oversize batch",
                "stock_status": "all",
                "select_all": "1",
            }),
        )
        oversized_response = page.goto(
            fixture.BASE + oversized_path,
            wait_until="domcontentloaded",
        )
        assert oversized_response and oversized_response.status == 200
        page.wait_for_load_state("networkidle")
        assert page.locator(".sc-product-grid > .sc-product-tile").count() == 50
        oversized_checks = page.locator(
            'input[form="supplier-import-form"][name="product_ids"]',
        )
        assert oversized_checks.count() == 50
        assert oversized_checks.evaluate_all(
            "items => items.every(item => !item.checked)",
        )
        assert page.get_by_text(
            "В текущем фильтре 201 товаров.", exact=False,
        ).count() == 1
        assert page.get_by_text("Ни один товар не был отмечен", exact=False).count() == 1
        with fixture.app.app_context():
            assert ImportedProduct.query.count() == fixture_ids["imported_before"] + imported_count
        checks.append("over_200_filter_falls_back_to_50_unselected_rows_without_partial_mass_selection")

        review_fixture = seed_supplier_update_review_rows(fixture_ids, selected_ids)

        # Exact detail review stays read-only, and source drift rejects confirm.
        detail_url = fixture.BASE + "/supplier-catalog/{}/products/{}".format(
            fixture_ids["supplier_id"], review_fixture["detail_source_id"],
        )
        response = page.goto(detail_url, wait_until="domcontentloaded")
        assert response and response.status == 200
        detail_action = page.get_by_role("button", name="Проверить обновление", exact=True)
        with fixture.app.app_context():
            detail_copy = db.session.get(ImportedProduct, review_fixture["detail_copy_id"])
            before_detail = (detail_copy.title, detail_copy.updated_at)

        with page.expect_response(
            lambda r: urlsplit(r.url).path == SUPPLIER_REVIEW_PATH and r.request.method == "POST",
        ) as detail_review:
            detail_action.click()
        assert detail_review.value.status == 200
        page.locator("[data-supplier-review-summary]").wait_for(state="visible")
        assert page.locator("[data-review-requested]").inner_text() == "1"
        assert page.locator("[data-review-changed]").inner_text() == "1"
        detail_row = page.locator("[data-supplier-review-row]")
        assert detail_row.count() == 1
        assert detail_row.get_attribute("data-local-product-id") == str(review_fixture["detail_copy_id"])
        assert detail_row.get_attribute("data-row-state") == "changed"
        assert_supplier_warning_contrast(
            page, '[data-row-state="changed"] [data-supplier-review-state]',
        )
        assert supplier_review_posts[-1]["body"].get("product_ids") == [
            str(review_fixture["detail_source_id"]),
        ]
        assert len(supplier_confirm_posts) == 0
        with fixture.app.app_context():
            detail_copy = db.session.get(ImportedProduct, review_fixture["detail_copy_id"])
            assert (detail_copy.title, detail_copy.updated_at) == before_detail

        # The review controls remain keyboard reachable at the existing 320px viewport.
        page.locator("[data-supplier-review-return]").focus()
        page.keyboard.press("Shift+Tab")
        keyboard_state = page.evaluate("""() => {
            const button = document.querySelector('[data-supplier-review-confirm]');
            const rect = button.getBoundingClientRect();
            return [document.activeElement === button, button.matches(':focus-visible'),
                rect.height, rect.left, rect.right, document.documentElement.clientWidth,
                document.documentElement.scrollWidth];
        }""")
        assert keyboard_state[0] and keyboard_state[1]
        assert keyboard_state[2] >= 44 and keyboard_state[3] >= 0
        assert keyboard_state[4] <= keyboard_state[5] and keyboard_state[6] <= keyboard_state[5] + 1
        supplier_review_screenshot = "supplier-detail-refresh-review-320.png"
        page.screenshot(path=REPORT_PATH.parent / supplier_review_screenshot, animations="disabled")
        checks.append("supplier_review_confirm_is_keyboard_reachable_at_320px")

        with fixture.app.app_context():
            detail_source = db.session.get(SupplierProduct, review_fixture["detail_source_id"])
            detail_source.title = "R42 detail source changed after review"
            detail_source.content_revision += 1
            db.session.commit()
        with page.expect_response(
            lambda r: urlsplit(r.url).path == SUPPLIER_CONFIRM_PATH and r.request.method == "POST",
        ) as stale_confirm:
            page.locator("[data-supplier-review-confirm]").click()
        assert stale_confirm.value.status == 409
        page.locator("[data-supplier-review-recheck]").wait_for(state="visible")
        assert page.locator("[data-supplier-review-confirm]").count() == 0
        assert_supplier_warning_contrast(page, '[data-supplier-update-review] [role="alert"]')
        with fixture.app.app_context():
            detail_copy = db.session.get(ImportedProduct, review_fixture["detail_copy_id"])
            detail_source = db.session.get(SupplierProduct, review_fixture["detail_source_id"])
            assert (detail_copy.title, detail_copy.updated_at) == before_detail
            assert detail_source.title == "R42 detail source changed after review"
        checks.append("supplier_source_drift_returns_409_without_local_write")
        checks.append("supplier_review_warning_and_stale_notice_meet_aa_in_both_themes")

        # One checked source ID remains exact through review and explicit confirm.
        selected_filters = dict(FILTERS)
        selected_filters["search"] = review_fixture["selected_source_external_id"]
        selected_filters["intent"] = "updates"
        selected_filters["updates_only"] = "1"
        selected_source_path = "/supplier-catalog/{}/products?{}".format(
            fixture_ids["supplier_id"], urlencode(selected_filters),
        )
        response = page.goto(fixture.BASE + selected_source_path, wait_until="domcontentloaded")
        assert response and response.status == 200
        page.wait_for_load_state("networkidle")
        selected_id = str(review_fixture["selected_source_id"])
        visible_update_ids = page.locator(
            'input[form="supplier-update-form"][name="update_product_ids"]',
        ).evaluate_all("items => items.map(item => item.value)")
        assert visible_update_ids == [selected_id]
        page.locator(
            f'input[form="supplier-update-form"][name="update_product_ids"][value="{selected_id}"]',
        ).check()
        selection = page.evaluate("""() => {
            const data = new FormData(document.querySelector('#supplier-update-form'));
            return [data.getAll('update_product_ids'), data.getAll('product_ids'),
                data.getAll('update_all_for_supplier')];
        }""")
        assert selection == [[selected_id], [], []]
        with fixture.app.app_context():
            selected_copy = db.session.get(ImportedProduct, review_fixture["selected_copy_id"])
            selected_before = (selected_copy.title, selected_copy.updated_at)

        with page.expect_response(
            lambda r: urlsplit(r.url).path == SUPPLIER_REVIEW_PATH and r.request.method == "POST",
        ) as selected_review:
            page.get_by_role("button", name=re.compile("Проверить выбранное")).click()
        assert selected_review.value.status == 200
        page.locator("[data-supplier-review-summary]").wait_for(state="visible")
        assert page.locator("[data-review-requested]").inner_text() == "1"
        assert page.locator("[data-review-changed]").inner_text() == "1"
        selected_row = page.locator("[data-supplier-review-row]")
        assert selected_row.count() == 1
        assert selected_row.get_attribute("data-local-product-id") == str(review_fixture["selected_copy_id"])
        assert supplier_review_posts[-1]["body"].get("update_product_ids") == [selected_id]
        assert not supplier_review_posts[-1]["body"].get("product_ids")
        assert not supplier_review_posts[-1]["body"].get("update_all_for_supplier")
        assert len(supplier_confirm_posts) == 1  # the stale detail attempt only
        with fixture.app.app_context():
            selected_copy = db.session.get(ImportedProduct, review_fixture["selected_copy_id"])
            assert (selected_copy.title, selected_copy.updated_at) == selected_before

        with page.expect_response(
            lambda r: urlsplit(r.url).path == SUPPLIER_CONFIRM_PATH and r.request.method == "POST",
        ) as selected_confirm:
            page.locator("[data-supplier-review-confirm]").click()
        assert selected_confirm.value.status == 302
        selected_return = urlsplit(page.url)
        assert selected_return.path == selected_source_path.split("?", 1)[0]
        assert parse_qs(selected_return.query) == {
            key: [value] for key, value in selected_filters.items()
        }
        assert supplier_confirm_posts[-1]["body"].get("update_product_ids") == [selected_id]
        assert not supplier_confirm_posts[-1]["body"].get("product_ids")
        assert not supplier_confirm_posts[-1]["body"].get("update_all_for_supplier")
        with fixture.app.app_context():
            selected_copy = db.session.get(ImportedProduct, review_fixture["selected_copy_id"])
            selected_source = db.session.get(SupplierProduct, review_fixture["selected_source_id"])
            detail_copy = db.session.get(ImportedProduct, review_fixture["detail_copy_id"])
            assert selected_copy.title == selected_source.title
            assert detail_copy.title == before_detail[0]
        checks.append("supplier_checked_source_is_reviewed_before_exact_local_confirm")

        # Explicit supplier-wide review keeps the full 201+ scope and bounds only details.
        response = page.goto(fixture.BASE + "/supplier-catalog", wait_until="domcontentloaded")
        assert response and response.status == 200
        page.wait_for_load_state("networkidle")
        card = page.locator(".sc-card").filter(has=page.locator(
            f'a[href="/supplier-catalog/{fixture_ids["supplier_id"]}/products"]',
        ))
        all_button = card.locator(".sc-card-secondary")
        assert all_button.inner_text() == "Проверить локальные изменения"
        with fixture.app.app_context():
            current_copy = db.session.get(ImportedProduct, review_fixture["current_copy_id"])
            unlinked_copy = db.session.get(ImportedProduct, review_fixture["unlinked_copy_id"])
            current_before = current_copy.updated_at
            unlinked_before = (unlinked_copy.title, unlinked_copy.updated_at)
            preview_snapshot = [
                (row.id, row.title, row.supplier_content_revision, row.updated_at)
                for row in ImportedProduct.query.filter_by(
                    seller_id=fixture.FIXTURE["seller_id"],
                    supplier_id=fixture_ids["supplier_id"],
                ).order_by(ImportedProduct.id).all()
            ]
        with page.expect_response(
            lambda r: urlsplit(r.url).path == SUPPLIER_REVIEW_PATH and r.request.method == "POST",
        ) as all_review:
            all_button.click()
        assert all_review.value.status == 200
        page.locator("[data-supplier-review-summary]").wait_for(state="visible")
        all_body = supplier_review_posts[-1]["body"]
        assert all_body.get("update_all_for_supplier") == ["1"]
        assert not all_body.get("update_product_ids") and not all_body.get("product_ids")
        requested = int(page.locator("[data-review-requested]").inner_text())
        eligible = int(page.locator("[data-review-eligible]").inner_text())
        changed = int(page.locator("[data-review-changed]").inner_text())
        unchanged = int(page.locator("[data-review-already-current]").inner_text())
        unlinked = int(page.locator("[data-review-unlinked]").inner_text())
        skipped = int(page.locator("[data-review-skipped]").inner_text())
        assert requested > 200 and changed > 0 and unchanged > 0 and unlinked > 0
        assert eligible + unlinked == requested and changed + unchanged == eligible
        assert skipped == unchanged + unlinked
        details = page.locator("[data-supplier-review-row]")
        rendered_details = details.count()
        assert 0 < rendered_details <= 50
        assert page.get_by_text(
            re.compile(rf"Показаны первые 50 из {requested} карточек"),
        ).count() == 1
        rendered_ids = details.evaluate_all(
            "rows => rows.map(row => Number(row.dataset.localProductId))",
        )
        assert all_body.get("supplier_id") == [str(fixture_ids["supplier_id"])]
        assert len(supplier_confirm_posts) == 2  # review itself has no write
        with fixture.app.app_context():
            after_review = [
                (row.id, row.title, row.supplier_content_revision, row.updated_at)
                for row in ImportedProduct.query.filter_by(
                    seller_id=fixture.FIXTURE["seller_id"],
                    supplier_id=fixture_ids["supplier_id"],
                ).order_by(ImportedProduct.id).all()
            ]
        assert after_review == preview_snapshot

        with page.expect_response(
            lambda r: urlsplit(r.url).path == SUPPLIER_CONFIRM_PATH and r.request.method == "POST",
        ) as all_confirm:
            page.locator("[data-supplier-review-confirm]").click()
        assert all_confirm.value.status == 302
        page.wait_for_url(fixture.BASE + "/supplier-catalog")
        assert urlsplit(page.url).path == "/supplier-catalog"
        assert supplier_confirm_posts[-1]["body"].get("supplier_id") == [
            str(fixture_ids["supplier_id"]),
        ]
        assert supplier_confirm_posts[-1]["body"].get("update_all_for_supplier") == ["1"]
        assert not supplier_confirm_posts[-1]["body"].get("update_product_ids")
        page.get_by_text(
            f"Локально обновлено: {changed}, пропущено: {skipped}, ошибок: 0.",
            exact=False,
        ).wait_for(state="visible")
        with fixture.app.app_context():
            current_copy = db.session.get(ImportedProduct, review_fixture["current_copy_id"])
            unlinked_copy = db.session.get(ImportedProduct, review_fixture["unlinked_copy_id"])
            tail_copy = db.session.get(ImportedProduct, review_fixture["oversized_tail_copy_id"])
            tail_source = db.session.get(SupplierProduct, review_fixture["oversized_tail_source_id"])
            assert tail_copy.id not in rendered_ids
            assert current_copy.updated_at == current_before
            assert (unlinked_copy.title, unlinked_copy.updated_at) == unlinked_before
            assert tail_copy.title == tail_source.title
            assert tail_copy.supplier_content_revision == tail_source.content_revision
        checks.append("supplier_all_review_counts_full_scope_and_applies_rows_beyond_bounded_sample")
        supplier_review_report = {
            "all_requested": requested,
            "all_changed": changed,
            "all_already_current": unchanged,
            "all_unlinked": unlinked,
            "rendered_detail_rows": rendered_details,
            "review_post_count": len(supplier_review_posts),
            "confirm_post_count": len(supplier_confirm_posts),
        }

        # Selecting stale sources from a filtered catalog spans the initial
        # 50-row page without turning the update into a supplier-wide action.
        filtered_update_fixture = seed_filtered_supplier_update_rows(
            fixture_ids["supplier_id"],
        )
        filtered_update_filters = {
            "page": "1",
            "search": "R52 Filtered update",
            "show_imported": "1",
            "stock_status": "in_stock",
            "wb_filter": "not_on_wb",
            "ai_filter": "ai_parsed",
        }
        filtered_import_path = "/supplier-catalog/{}/products?{}".format(
            fixture_ids["supplier_id"], urlencode(filtered_update_filters),
        )
        filtered_catalog_path = (
            f"/supplier-catalog/{fixture_ids['supplier_id']}/products"
        )
        page.set_viewport_size({"width": 320, "height": 900})
        filtered_page_response = page.goto(
            fixture.BASE + filtered_import_path,
            wait_until="domcontentloaded",
        )
        assert filtered_page_response and filtered_page_response.status == 200
        page.wait_for_load_state("networkidle")
        filtered_source_ids = filtered_update_fixture["source_ids"]
        unimported_source_id = str(filtered_update_fixture["unimported_source_id"])
        assert page.locator(
            'input[form="supplier-update-form"][name="update_product_ids"]',
        ).count() == 0, "import intent must not expose update selections"
        updates_tab = page.get_by_role("link", name="Обновления", exact=True)
        assert updates_tab.is_visible()
        updates_href = urlsplit(updates_tab.get_attribute("href"))
        assert updates_href.path == (
            f"/supplier-catalog/{fixture_ids['supplier_id']}/products"
        )
        switched_filters = {
            **filtered_update_filters,
            "intent": "updates",
            "updates_only": "1",
        }
        switched_query = parse_qs(updates_href.query, keep_blank_values=True)
        assert switched_query == {
            key: [value] for key, value in switched_filters.items()
        }
        with page.expect_response(
            lambda response: urlsplit(response.url).path == updates_href.path
            and response.request.method == "GET"
            and parse_qs(urlsplit(response.url).query, keep_blank_values=True)
            == switched_query,
        ) as updates_mode_response:
            updates_tab.click()
        assert updates_mode_response.value.status == 200
        page.wait_for_load_state("networkidle")
        assert parse_qs(
            urlsplit(page.url).query, keep_blank_values=True,
        ) == switched_query
        filtered_update_filters = switched_filters
        ordinary_update_inputs = page.locator(
            'input[form="supplier-update-form"][name="update_product_ids"]',
        )
        ordinary_visible_ids = ordinary_update_inputs.evaluate_all(
            "items => items.map(item => item.value)",
        )
        assert len(ordinary_visible_ids) == 50, len(ordinary_visible_ids)
        assert ordinary_visible_ids == [str(value) for value in filtered_source_ids[:50]]
        assert str(filtered_source_ids[54]) not in ordinary_visible_ids
        assert unimported_source_id not in ordinary_visible_ids
        assert ordinary_update_inputs.evaluate_all(
            "items => items.filter(item => item.checked).map(item => item.value)",
        ) == []
        ordinary_count = page.locator("#update-selection-count")
        assert ordinary_count.locator("span").all_inner_texts() == [
            "0 выбрано", f"из {len(ordinary_visible_ids)} с обновлениями",
        ]
        ordinary_count_geometry = ordinary_count.evaluate("""element => {
            const parts = Array.from(element.children).map(child => child.getBoundingClientRect());
            const style = getComputedStyle(element);
            const bounds = element.getBoundingClientRect();
            return {
                partCount: parts.length,
                separated: parts.length === 2 && (
                    parts[0].bottom <= parts[1].top || parts[1].bottom <= parts[0].top ||
                    parts[0].right + parseFloat(style.columnGap || style.gap || '0') <= parts[1].left + 1
                ),
                left: bounds.left,
                right: bounds.right,
                viewport: document.documentElement.clientWidth,
                scroll: document.documentElement.scrollWidth,
            };
        }""")
        assert ordinary_count_geometry["partCount"] == 2
        assert ordinary_count_geometry["separated"]
        assert ordinary_count_geometry["left"] >= 0
        assert ordinary_count_geometry["right"] <= ordinary_count_geometry["viewport"]
        assert ordinary_count_geometry["scroll"] <= ordinary_count_geometry["viewport"] + 1

        filtered_update_link = page.locator("a[data-supplier-filter-update-select]")
        assert filtered_update_link.count() == 1
        assert filtered_update_link.is_visible()
        assert filtered_update_link.inner_text().strip() == (
            "Выбрать 55 обновлений по фильтру"
        )
        filtered_link_geometry = filtered_update_link.evaluate("""element => {
            const rect = element.getBoundingClientRect();
            return {
                height: rect.height,
                left: rect.left,
                right: rect.right,
                viewport: document.documentElement.clientWidth,
                scroll: document.documentElement.scrollWidth,
            };
        }""")
        assert filtered_link_geometry["height"] >= 44
        assert filtered_link_geometry["left"] >= 0
        assert filtered_link_geometry["right"] <= 320
        assert filtered_link_geometry["scroll"] <= 321
        filtered_select_href = urlsplit(filtered_update_link.get_attribute("href"))
        assert filtered_select_href.path == (
            f"/supplier-catalog/{fixture_ids['supplier_id']}/products"
        )
        filtered_select_query = parse_qs(filtered_select_href.query, keep_blank_values=True)
        select_filter_scope = {
            key: value for key, value in filtered_update_filters.items()
            if key != "intent"
        }
        assert filtered_select_query == {
            **{key: [value] for key, value in select_filter_scope.items()},
            "select_all": ["updates"],
        }

        update_selection_screenshot = "supplier-filtered-update-selection-dark-320.png"
        with page.expect_response(
            lambda response: urlsplit(response.url).path == filtered_select_href.path
            and response.request.method == "GET"
            and parse_qs(urlsplit(response.url).query, keep_blank_values=True)
            == filtered_select_query,
        ) as filtered_selection_response:
            filtered_update_link.click()
        assert filtered_selection_response.value.status == 200
        page.wait_for_load_state("networkidle")
        assert urlsplit(page.url).path == filtered_select_href.path
        assert parse_qs(page.url.split("?", 1)[1], keep_blank_values=True) == filtered_select_query

        all_update_inputs = page.locator(
            'input[form="supplier-update-form"][name="update_product_ids"]',
        )
        assert all_update_inputs.count() == 55
        selected_stale_source_ids = all_update_inputs.evaluate_all(
            "items => items.filter(item => item.checked).map(item => item.value)",
        )
        expected_stale_source_ids = [str(value) for value in filtered_source_ids]
        assert selected_stale_source_ids == expected_stale_source_ids
        assert selected_stale_source_ids[-1] == str(filtered_source_ids[54])
        assert unimported_source_id not in selected_stale_source_ids
        assert page.locator(
            'input[form="supplier-import-form"][name="product_ids"]:checked',
        ).count() == 0
        selection_screenshot_geometry = page.evaluate("""() => ({
            viewport: document.documentElement.clientWidth,
            scroll: document.documentElement.scrollWidth,
            checkedUpdateCount: document.querySelectorAll(
                'input[form="supplier-update-form"][name="update_product_ids"]:checked'
            ).length,
        })""")
        assert selection_screenshot_geometry["viewport"] == 320
        assert selection_screenshot_geometry["scroll"] <= 321
        assert selection_screenshot_geometry["checkedUpdateCount"] == 55
        selection_status_container = page.locator("#supplier-selection-status")
        assert selection_status_container.get_attribute("role") == "status"
        assert selection_status_container.get_attribute("aria-live") == "polite"
        assert page.locator("#supplier-selection-count").count() == 1
        assert page.locator("#update-selection-count").count() == 0
        update_submit = page.locator('button[form="supplier-update-form"].sc-action-submit')
        assert update_submit.get_attribute("aria-describedby") == "supplier-selection-count"
        assert " ".join(update_submit.inner_text().split()) == "Проверить выбранное (55)"
        selection_status = page.locator('[role="status"] strong').filter(
            has_text="Выбрано 55 из 55",
        )
        selection_status.evaluate("""element => {
            const status = element.closest('[role="status"]');
            if (!status) throw new Error('filtered update selection status is missing');
            // The mobile header is fixed at 56px; leave a small gap below it.
            status.style.scrollMarginTop = '72px';
            status.scrollIntoView({block: 'start', inline: 'nearest'});
        }""")
        page.screenshot(
            path=REPORT_PATH.parent / update_selection_screenshot,
            animations="disabled",
        )
        selected_count_geometry = page.evaluate("""() => {
            const status = document.querySelector('#supplier-selection-status');
            const count = document.querySelector('#supplier-selection-count');
            const button = document.querySelector('button[form="supplier-update-form"].sc-action-submit');
            const statusRect = status.getBoundingClientRect();
            const buttonRect = button.getBoundingClientRect();
            return {
                statusText: count.innerText.trim(),
                statusRole: status.getAttribute('role'),
                live: status.getAttribute('aria-live'),
                duplicate: !!document.querySelector('#update-selection-count'),
                describedBy: button.getAttribute('aria-describedby'),
                buttonText: button.innerText.replace(/\\s+/g, ' ').trim(),
                statusBounds: [statusRect.left, statusRect.top, statusRect.right, statusRect.bottom],
                buttonBounds: [buttonRect.left, buttonRect.top, buttonRect.right, buttonRect.bottom],
                viewport: [document.documentElement.clientWidth, window.innerHeight],
                scroll: document.documentElement.scrollWidth,
                overlap: !(statusRect.bottom <= buttonRect.top || buttonRect.bottom <= statusRect.top ||
                    statusRect.right <= buttonRect.left || buttonRect.right <= statusRect.left),
            };
        }""")
        assert selected_count_geometry["statusText"] == "Выбрано 55 из 55"
        assert selected_count_geometry["statusRole"] == "status"
        assert selected_count_geometry["live"] == "polite"
        assert not selected_count_geometry["duplicate"]
        assert selected_count_geometry["describedBy"] == "supplier-selection-count"
        assert selected_count_geometry["buttonText"] == "Проверить выбранное (55)"
        assert selected_count_geometry["statusBounds"][0] >= 0
        assert selected_count_geometry["statusBounds"][2] <= selected_count_geometry["viewport"][0]
        assert selected_count_geometry["buttonBounds"][0] >= 0
        assert selected_count_geometry["buttonBounds"][2] <= selected_count_geometry["viewport"][0]
        assert not selected_count_geometry["overlap"]
        assert selected_count_geometry["scroll"] <= selected_count_geometry["viewport"][0] + 1

        first_price = page.locator(".sc-product-price").first
        assert " ".join(first_price.inner_text().split()) == "200 ₽"
        first_price_geometry = first_price.evaluate("""price => {
            const range = document.createRange();
            range.selectNodeContents(price);
            const textRects = Array.from(range.getClientRects());
            const row = price.closest('.sc-product-price-row');
            const status = row && row.querySelector('.sc-product-status');
            const tile = price.closest('.sc-product-tile');
            const priceRect = price.getBoundingClientRect();
            const statusRect = status && status.getBoundingClientRect();
            const tileRect = tile.getBoundingClientRect();
            return {
                whiteSpace: getComputedStyle(price).whiteSpace,
                lineCount: textRects.length,
                priceBounds: [priceRect.left, priceRect.top, priceRect.right, priceRect.bottom],
                statusBounds: statusRect && [statusRect.left, statusRect.top, statusRect.right, statusRect.bottom],
                tileBounds: [tileRect.left, tileRect.top, tileRect.right, tileRect.bottom],
                overlaps: !!statusRect && priceRect.left < statusRect.right &&
                    priceRect.right > statusRect.left && priceRect.top < statusRect.bottom &&
                    priceRect.bottom > statusRect.top,
            };
        }""")
        assert first_price_geometry["whiteSpace"] == "nowrap"
        assert first_price_geometry["lineCount"] == 1
        assert first_price_geometry["priceBounds"][0] >= first_price_geometry["tileBounds"][0]
        assert first_price_geometry["priceBounds"][2] <= first_price_geometry["tileBounds"][2]
        assert first_price_geometry["statusBounds"] is not None
        assert not first_price_geometry["overlaps"]

        # The duplicate current copy is included in preview scope, while the
        # explicitly unchecked source remains outside preview and apply.
        excluded_source_id = str(filtered_source_ids[1])
        current_duplicate_copy_id = filtered_update_fixture["current_duplicate_copy_id"]
        page.locator(
            f'input[form="supplier-update-form"][name="update_product_ids"]'
            f'[value="{excluded_source_id}"]',
        ).uncheck()
        selected_after_exclusion = [
            value for value in expected_stale_source_ids if value != excluded_source_id
        ]
        actual_selection_form_data = page.evaluate("""() => {
            const form = document.querySelector('#supplier-update-form');
            const data = new FormData(form);
            return {
                supplierIds: data.getAll('supplier_id'),
                sourceIds: data.getAll('update_product_ids'),
                fallbackIds: data.getAll('product_ids'),
                allSupplier: data.getAll('update_all_for_supplier'),
                returnTo: data.getAll('return_to'),
                csrf: data.getAll('csrf_token'),
            };
        }""")
        assert actual_selection_form_data["supplierIds"] == [
            str(fixture_ids["supplier_id"]),
        ]
        assert actual_selection_form_data["sourceIds"] == selected_after_exclusion
        assert actual_selection_form_data["fallbackIds"] == []
        assert actual_selection_form_data["allSupplier"] == []
        assert len(actual_selection_form_data["returnTo"]) == 1
        assert urlsplit(actual_selection_form_data["returnTo"][0]).path == filtered_catalog_path
        return_to_query = parse_qs(
            urlsplit(actual_selection_form_data["returnTo"][0]).query,
            keep_blank_values=True,
        )
        assert return_to_query == {
            key: [value] for key, value in filtered_update_filters.items()
        }
        assert "select_all" not in return_to_query
        assert actual_selection_form_data["csrf"] and actual_selection_form_data["csrf"][0]

        local_copy_ids = filtered_update_fixture["stale_copy_ids"] + [
            current_duplicate_copy_id,
        ]
        with fixture.app.app_context():
            filtered_before_preview = {
                row.id: (row.title, row.supplier_content_revision, row.updated_at)
                for row in ImportedProduct.query.filter(
                    ImportedProduct.seller_id == fixture.FIXTURE["seller_id"],
                    ImportedProduct.supplier_id == fixture_ids["supplier_id"],
                    ImportedProduct.id.in_(local_copy_ids),
                ).all()
            }

        with page.expect_response(
            lambda response: urlsplit(response.url).path == SUPPLIER_REVIEW_PATH
            and response.request.method == "POST",
        ) as filtered_review_response:
            page.get_by_role("button", name=re.compile("Проверить выбранное")).click()
        assert filtered_review_response.value.status == 200
        page.locator("[data-supplier-review-summary]").wait_for(state="visible")
        filtered_review_body = supplier_review_posts[-1]["body"]
        assert filtered_review_body.get("supplier_id") == [
            str(fixture_ids["supplier_id"]),
        ]
        assert filtered_review_body.get("update_product_ids") == selected_after_exclusion
        assert not filtered_review_body.get("product_ids")
        assert not filtered_review_body.get("update_all_for_supplier")
        assert urlsplit(filtered_review_body["return_to"][0]).path == filtered_catalog_path
        assert parse_qs(
            urlsplit(filtered_review_body["return_to"][0]).query,
            keep_blank_values=True,
        ) == {
            key: [value] for key, value in filtered_update_filters.items()
        }
        filtered_review_counts = {
            "requested": int(page.locator("[data-review-requested]").inner_text()),
            "eligible": int(page.locator("[data-review-eligible]").inner_text()),
            "changed": int(page.locator("[data-review-changed]").inner_text()),
            "already_current": int(
                page.locator("[data-review-already-current]").inner_text()
            ),
            "unlinked": int(page.locator("[data-review-unlinked]").inner_text()),
            "skipped": int(page.locator("[data-review-skipped]").inner_text()),
        }
        expected_filtered_review_counts = {
            "requested": 55,
            "eligible": 55,
            "changed": 54,
            "already_current": 1,
            "unlinked": 0,
            "skipped": 1,
        }
        assert filtered_review_counts == expected_filtered_review_counts, (
            "selected-only supplier preview counts differ: "
            f"expected={expected_filtered_review_counts!r}, "
            f"actual={filtered_review_counts!r}"
        )
        filtered_detail_rows = page.locator("[data-supplier-review-row]")
        filtered_detail_ids = filtered_detail_rows.evaluate_all(
            "items => items.map(item => item.dataset.localProductId)",
        )
        selected_local_copy_ids = [
            copy_id
            for source_id, copy_id in zip(
                filtered_source_ids,
                filtered_update_fixture["stale_copy_ids"],
            )
            if str(source_id) != excluded_source_id
        ] + [current_duplicate_copy_id]
        expected_filtered_detail_ids = [
            str(copy_id) for copy_id in sorted(selected_local_copy_ids)[:50]
        ]
        assert len(selected_local_copy_ids) == 55, (
            "selected supplier sources should scope 55 owned local copies: "
            f"source_count={len(selected_after_exclusion)}, "
            f"local_copy_ids={len(selected_local_copy_ids)}"
        )
        assert filtered_detail_rows.count() == 50, (
            "supplier preview should render its first 50 local-copy details: "
            f"rendered={filtered_detail_rows.count()}, "
            f"requested={filtered_review_counts['requested']}"
        )
        assert filtered_detail_ids == expected_filtered_detail_ids, (
            "supplier preview details must be the first 50 local rows in ID order: "
            f"expected={expected_filtered_detail_ids!r}, actual={filtered_detail_ids!r}"
        )
        assert str(current_duplicate_copy_id) in filtered_detail_ids
        assert not set(
            map(str, filtered_update_fixture["stale_copy_ids"][50:]),
        ).intersection(filtered_detail_ids), (
            "tail local-copy details must be omitted after the first 50: "
            f"tail={filtered_update_fixture['stale_copy_ids'][50:]!r}, "
            f"rendered={filtered_detail_ids[-1]!r}"
        )
        assert page.get_by_text(
            "Показаны первые 50 из 55 карточек",
            exact=False,
        ).count() == 1
        assert page.locator(
            f'[data-supplier-review-row][data-local-product-id="{current_duplicate_copy_id}"]'
            '[data-row-state="unchanged"]',
        ).count() == 1
        assert page.locator(
            f'[data-supplier-review-row][data-local-product-id="'
            f'{filtered_update_fixture["stale_copy_ids"][0]}"]'
            '[data-row-state="changed"]',
        ).count() == 1
        assert len(supplier_confirm_posts) == 3
        with fixture.app.app_context():
            reviewed_copy_state = {
                row.id: (row.title, row.supplier_content_revision, row.updated_at)
                for row in ImportedProduct.query.filter(
                    ImportedProduct.seller_id == fixture.FIXTURE["seller_id"],
                    ImportedProduct.supplier_id == fixture_ids["supplier_id"],
                    ImportedProduct.id.in_(local_copy_ids),
                ).all()
            }
        assert reviewed_copy_state == filtered_before_preview

        filtered_confirm_form_data = page.evaluate("""() => {
            const button = document.querySelector('[data-supplier-review-confirm]');
            if (!button || !button.form) return null;
            const data = new FormData(button.form);
            return {
                supplierIds: data.getAll('supplier_id'),
                sourceIds: data.getAll('update_product_ids'),
                fallbackIds: data.getAll('product_ids'),
                allSupplier: data.getAll('update_all_for_supplier'),
                returnTo: data.getAll('return_to'),
                reviewTokens: data.getAll('review_token'),
                csrf: data.getAll('csrf_token'),
            };
        }""")
        assert filtered_confirm_form_data is not None
        assert filtered_confirm_form_data["supplierIds"] == [
            str(fixture_ids["supplier_id"]),
        ]
        assert filtered_confirm_form_data["sourceIds"] == selected_after_exclusion
        assert filtered_confirm_form_data["fallbackIds"] == []
        assert filtered_confirm_form_data["allSupplier"] == []
        assert urlsplit(filtered_confirm_form_data["returnTo"][0]).path == filtered_catalog_path
        assert parse_qs(
            urlsplit(filtered_confirm_form_data["returnTo"][0]).query,
            keep_blank_values=True,
        ) == {
            key: [value] for key, value in filtered_update_filters.items()
        }
        assert len(filtered_confirm_form_data["reviewTokens"]) == 1
        assert filtered_confirm_form_data["csrf"] and filtered_confirm_form_data["csrf"][0]

        with page.expect_response(
            lambda response: urlsplit(response.url).path == SUPPLIER_CONFIRM_PATH
            and response.request.method == "POST",
        ) as filtered_confirm_response:
            page.locator("[data-supplier-review-confirm]").click()
        assert filtered_confirm_response.value.status == 302
        filtered_confirm_body = supplier_confirm_posts[-1]["body"]
        assert filtered_confirm_body.get("supplier_id") == [
            str(fixture_ids["supplier_id"]),
        ]
        assert filtered_confirm_body.get("update_product_ids") == selected_after_exclusion
        assert not filtered_confirm_body.get("product_ids")
        assert not filtered_confirm_body.get("update_all_for_supplier")
        page.wait_for_url(lambda url: urlsplit(url).path == filtered_catalog_path)
        page.get_by_text(
            "Локально обновлено: 54, пропущено: 1, ошибок: 0.",
            exact=False,
        ).wait_for(state="visible")
        filtered_return_location = urlsplit(page.url)
        filtered_return_query = parse_qs(
            filtered_return_location.query,
            keep_blank_values=True,
        )
        assert filtered_return_query == {
            key: [value] for key, value in filtered_update_filters.items()
        }
        assert "select_all" not in filtered_return_query
        returned_update_checks = page.locator(
            'input[form="supplier-update-form"][name="update_product_ids"]',
        )
        assert returned_update_checks.count() == 1
        assert returned_update_checks.first.get_attribute("value") == excluded_source_id
        assert returned_update_checks.evaluate_all(
            "items => items.filter(item => item.checked).map(item => item.value)",
        ) == [], "returning from confirm must clear the automatic selection state"
        with fixture.app.app_context():
            db.session.expire_all()
            source_by_id = {
                row.id: row for row in SupplierProduct.query.filter(
                    SupplierProduct.id.in_(filtered_source_ids),
                ).all()
            }
            updated_copies = {
                row.id: row for row in ImportedProduct.query.filter(
                    ImportedProduct.seller_id == fixture.FIXTURE["seller_id"],
                    ImportedProduct.supplier_id == fixture_ids["supplier_id"],
                    ImportedProduct.id.in_(local_copy_ids),
                ).all()
            }
            assert len(updated_copies) == 56
            assert updated_copies[current_duplicate_copy_id].title == source_by_id[
                filtered_source_ids[0]
            ].title
            assert updated_copies[current_duplicate_copy_id].supplier_content_revision == 2
            assert updated_copies[current_duplicate_copy_id].updated_at == (
                filtered_before_preview[current_duplicate_copy_id][2]
            )
            for index, (source_id, copy_id) in enumerate(zip(
                filtered_source_ids,
                filtered_update_fixture["stale_copy_ids"],
            )):
                copy = updated_copies[copy_id]
                source = source_by_id[source_id]
                if index == 1:
                    assert copy.title == f"R52 stale local copy {index:03d}"
                    assert copy.supplier_content_revision == 1
                    assert copy.updated_at == filtered_before_preview[copy_id][2]
                else:
                    assert copy.title == source.title
                    assert copy.supplier_content_revision == source.content_revision
            actual_changed_copy_ids = {
                copy_id
                for copy_id in filtered_update_fixture["stale_copy_ids"]
                if (
                    updated_copies[copy_id].title,
                    updated_copies[copy_id].supplier_content_revision,
                    updated_copies[copy_id].updated_at,
                ) != filtered_before_preview[copy_id]
            }
            expected_changed_copy_ids = {
                copy_id
                for source_id, copy_id in zip(
                    filtered_source_ids,
                    filtered_update_fixture["stale_copy_ids"],
                )
                if str(source_id) != excluded_source_id
            }
            assert actual_changed_copy_ids == expected_changed_copy_ids, (
                "apply should change exactly the selected stale copies: "
                f"expected_count={len(expected_changed_copy_ids)}, "
                f"actual_count={len(actual_changed_copy_ids)}, "
                f"missing={sorted(expected_changed_copy_ids - actual_changed_copy_ids)!r}, "
                f"unexpected={sorted(actual_changed_copy_ids - expected_changed_copy_ids)!r}"
            )
            assert ImportedProduct.query.filter_by(
                seller_id=fixture.FIXTURE["seller_id"],
                supplier_id=fixture_ids["supplier_id"],
                supplier_product_id=filtered_update_fixture["unimported_source_id"],
            ).count() == 0
        filtered_update_report = {
            "filtered_source_rows": len(filtered_source_ids),
            "initial_page_rows": len(ordinary_visible_ids),
            "initially_selected_sources": len(selected_stale_source_ids),
            "selected_sources_after_exclusion": len(selected_after_exclusion),
            "selected_from_beyond_initial_page": sum(
                source_id in selected_after_exclusion
                for source_id in expected_stale_source_ids[50:]
            ),
            "reviewed_local_copies": filtered_review_counts["requested"],
            "changed_local_copies": filtered_review_counts["changed"],
            "already_current_local_copies": filtered_review_counts["already_current"],
            "visible_detail_rows": len(filtered_detail_ids),
            "first_50_detail_ids": filtered_detail_ids,
            "unimported_source_rows": 1,
            "returned_filters": filtered_return_query,
            "screenshot": update_selection_screenshot,
        }
        checks.append("filtered_update_selection_checks_exact_stale_sources_across_pages_at_320px")
        checks.append("filtered_update_review_apply_preserve_owner_scope_and_excluded_current_unimported_rows")

        # Classic updates hand off the exact checked IDs to beta. The handoff
        # and exact feed read are read-only; the existing action opens preview,
        # then its separate confirmation applies only Seller Hub data.
        handoff_ids = [
            review_fixture["selected_copy_id"],
            review_fixture["current_copy_id"],
        ]
        with fixture.app.app_context():
            for source_id in (
                review_fixture["selected_source_id"],
                review_fixture["current_source_id"],
            ):
                source = db.session.get(SupplierProduct, source_id)
                source.content_revision = int(source.content_revision or 1) + 1
            db.session.commit()

            def handoff_snapshot():
                return {
                    row.id: (row.title, row.supplier_content_revision, row.updated_at)
                    for row in ImportedProduct.query.filter(
                        ImportedProduct.seller_id == fixture.FIXTURE["seller_id"],
                        ImportedProduct.id.in_(handoff_ids),
                    ).order_by(ImportedProduct.id).all()
                }

            handoff_before = handoff_snapshot()

        handoff_filters = {
            "updates": "1",
            "search": "R28 Mass batch",
            "supplier": str(fixture_ids["supplier_id"]),
            "sort": "title",
        }
        page.set_viewport_size({"width": 1280, "height": 900})
        response = page.goto(
            fixture.BASE + "/my-products?" + urlencode(handoff_filters),
            wait_until="domcontentloaded",
        )
        assert response and response.status == 200
        page.wait_for_load_state("networkidle")
        classic_rows = page.locator("tr[data-product-id]")
        classic_ids = [
            int(value) for value in classic_rows.evaluate_all(
                "rows => rows.map(row => row.dataset.productId)",
            )
        ]
        assert len(classic_ids) == 2 and set(classic_ids) == set(handoff_ids)
        for product_id in classic_ids:
            page.locator(
                f'tr[data-product-id="{product_id}"] input[type="checkbox"]',
            ).check()
        classic_form = page.locator('[data-supplier-refresh-review-handoff="bulk"]')
        assert classic_form.is_visible()
        classic_selection = classic_form.evaluate("""form => {
            const data = new FormData(form);
            return {ids: data.getAll('selected_ids'), csrf: data.getAll('csrf_token'), action: form.action};
        }""")
        assert classic_selection["ids"] == [str(product_id) for product_id in classic_ids]
        assert classic_selection["csrf"] and classic_selection["csrf"][0]
        assert parse_qs(urlsplit(classic_selection["action"]).query) == {
            "updates": ["1"],
            "search": [handoff_filters["search"]],
            "supplier": [str(fixture_ids["supplier_id"])],
            "sort": ["title"],
        }

        writes_before_handoff = len(fixture.REPORT["writes"])
        feeds_before_handoff = len(product_feed_reads)
        facets_before_handoff = len(facet_reads)
        with page.expect_response(
            lambda r: urlsplit(r.url).path == SUPPLIER_REFRESH_HANDOFF_PATH
            and r.request.method == "POST",
        ) as handoff_response:
            classic_form.get_by_role(
                "button", name=re.compile("Проверить применение в мои карточки"),
            ).click()
        assert handoff_response.value.status == 303
        assert len(supplier_handoff_posts) == len(supplier_handoff_responses) == 1
        handoff_post = supplier_handoff_posts[0]
        assert set(handoff_post["body"]) == {"csrf_token", "selected_ids"}
        assert handoff_post["body"].get("selected_ids") == [
            str(product_id) for product_id in classic_ids
        ]
        assert handoff_post["query"] == {
            key: [value] for key, value in handoff_filters.items()
        }
        location = urlsplit(supplier_handoff_responses[0]["location"])
        token_values = parse_qs(location.query, keep_blank_values=True)
        assert supplier_handoff_responses[0]["status"] == 303
        assert location.path == "/my-products/beta"
        assert set(token_values) == {"supplier_refresh_batch"}
        assert len(token_values["supplier_refresh_batch"]) == 1
        supplier_handoff_token = token_values["supplier_refresh_batch"][0]
        assert 0 < len(supplier_handoff_token) <= 8192
        safe_page_tokens.append(supplier_handoff_token)

        from routes.suppliers import _load_supplier_refresh_handoff_token
        with fixture.app.app_context():
            owner = User.query.filter_by(username=fixture.USERNAME).one()
            signed_scope = _load_supplier_refresh_handoff_token(
                supplier_handoff_token,
                user_id=owner.id,
                seller_id=fixture.FIXTURE["seller_id"],
            )
            assert signed_scope["ids"] == classic_ids
            assert signed_scope["return_args"] == {
                "updates": 1,
                "search": handoff_filters["search"],
                "supplier": fixture_ids["supplier_id"],
                "sort": "title",
            }

        page.locator('#my-products-app[data-supplier-refresh-handoff="active"]').wait_for()
        page.wait_for_load_state("networkidle")
        beta_location = urlsplit(page.url)
        assert beta_location.path == "/my-products/beta"
        beta_token_values = parse_qs(beta_location.query, keep_blank_values=True)
        assert set(beta_token_values) == {"supplier_refresh_batch"}
        assert beta_token_values["supplier_refresh_batch"] == [supplier_handoff_token], (
            "beta URL must retain the exact signed selection",
        )
        assert page.get_by_role("heading", name="Обновление из каталога").count() == 1
        return_link = page.locator("[data-supplier-refresh-return]")
        return_url = urlsplit(return_link.get_attribute("href"))
        assert return_url.path == "/my-products"
        assert parse_qs(return_url.query) == {
            "updates": ["1"],
            "search": [handoff_filters["search"]],
            "supplier": [str(fixture_ids["supplier_id"])],
            "sort": ["title"],
        }
        supplier_reads = [
            read for read in product_feed_reads[feeds_before_handoff:]
            if supplier_handoff_token in read["supplier_refresh_tokens"]
        ]
        assert len(supplier_reads) == 1
        assert len(product_feed_reads[feeds_before_handoff:]) == 1
        assert len(facet_reads) == facets_before_handoff
        exact_feed = supplier_reads[0]
        assert exact_feed["query_keys"] == ("supplier_refresh_batch",)
        assert exact_feed["status"] == 200
        assert exact_feed["payload"]["success"] is True
        assert exact_feed["payload"]["supplier_refresh_batch_count"] == 2
        assert [item["id"] for item in exact_feed["payload"]["items"]] == classic_ids
        beta_state = page.evaluate("""() => {
            const app = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return app ? {loading: app.loading, mode: app.supplierRefreshBatchMode,
                ids: app.items.map(item => item.id), selected: app.selected.slice()} : null;
        }""")
        assert beta_state == {
            "loading": False, "mode": True, "ids": classic_ids, "selected": classic_ids,
        }
        assert supplier_refresh_preview_posts == []
        assert supplier_refresh_apply_posts == []
        assert len(fixture.REPORT["writes"]) == writes_before_handoff
        with fixture.app.app_context():
            assert handoff_snapshot() == handoff_before

        with page.expect_response(
            lambda r: urlsplit(r.url).path == SUPPLIER_REFRESH_PREVIEW_PATH
            and r.request.method == "POST",
        ) as preview_response:
            invoke_workspace_action(page, "refresh_supplier")
        assert preview_response.value.status == 200
        review_dialog = page.locator(
            'dialog[aria-labelledby="mp-supplier-refresh-dialog-title"]',
        )
        review_dialog.wait_for(state="visible")
        assert "Изменения останутся в Seller Hub" in review_dialog.inner_text()
        assert len(supplier_refresh_preview_posts) == 1
        assert supplier_refresh_preview_posts[0]["body"] == {"product_ids": classic_ids}
        assert supplier_refresh_preview_responses[0]["status"] == 200
        preview_payload = supplier_refresh_preview_responses[0]["payload"]
        assert preview_payload["requested"] == preview_payload["eligible"] == 2
        assert preview_payload["changed"] == 2
        assert [item["product_id"] for item in preview_payload["items"]] == classic_ids
        assert supplier_refresh_apply_posts == []
        assert len(fixture.REPORT["writes"]) == writes_before_handoff
        with fixture.app.app_context():
            assert handoff_snapshot() == handoff_before

        with page.expect_response(
            lambda r: urlsplit(r.url).path == SUPPLIER_REFRESH_APPLY_PATH
            and r.request.method == "POST",
        ) as apply_response:
            review_dialog.get_by_role("button", name="Обновить", exact=True).click()
        assert apply_response.value.status == 200
        assert len(supplier_refresh_apply_posts) == len(supplier_refresh_apply_responses) == 1
        apply_body = supplier_refresh_apply_posts[0]["body"]
        assert set(apply_body) == {"product_ids", "review_token"}
        assert apply_body["product_ids"] == classic_ids
        assert 0 < len(apply_body["review_token"]) <= 8192
        apply_payload = supplier_refresh_apply_responses[0]["payload"]
        assert apply_payload["success"] is True
        assert (
            apply_payload["requested"], apply_payload["updated"],
            apply_payload["skipped"], apply_payload["errors"],
        ) == (2, 2, 0, 0)
        assert [item["product_id"] for item in apply_payload["items"]] == classic_ids
        page.locator("#supplier-refresh-result").wait_for()
        with fixture.app.app_context():
            for product_id in classic_ids:
                imported = db.session.get(ImportedProduct, product_id)
                source = db.session.get(SupplierProduct, imported.supplier_product_id)
                assert imported.supplier_content_revision == source.content_revision

        with page.expect_navigation(wait_until="domcontentloaded"):
            return_link.click()
        returned_location = urlsplit(page.url)
        assert returned_location.path == "/my-products"
        assert parse_qs(returned_location.query) == {
            "updates": ["1"],
            "search": [handoff_filters["search"]],
            "supplier": [str(fixture_ids["supplier_id"])],
            "sort": ["title"],
        }
        assert "supplier_refresh_batch" not in parse_qs(returned_location.query)
        assert len(supplier_refresh_preview_posts) == len(supplier_refresh_apply_posts) == 1
        checks.append("classic_refresh_handoff_keeps_exact_order_until_explicit_beta_review_and_filtered_return")
        supplier_refresh_handoff_report = {
            "selected_count": len(classic_ids),
            "reviewed_count": preview_payload["requested"],
            "updated_count": apply_payload["updated"],
            "returned_filters": parse_qs(returned_location.query),
        }

        assert not safe_page_errors(), "batch browser reported a JavaScript error"
        assert fixture.REPORT["provider_attempts"] == 0
        assert fixture.REPORT["unexpected_external_requests"] == []
        assert fixture.REPORT["unexpected_http"] == []
        assert classic_fallback_documents, "Classic form fallback needs a missing inline enhancer"
        assert fixture.REPORT["writes"] == [
            {"method": "POST", "path": category_update_path},
            {"method": "POST", "path": IMPORT_PATH},
            {"method": "POST", "path": WB_CREATE_PATH},
            {"method": "POST", "path": SUPPLIER_CONFIRM_PATH},
            {"method": "POST", "path": SUPPLIER_CONFIRM_PATH},
            {"method": "POST", "path": SUPPLIER_CONFIRM_PATH},
            {"method": "POST", "path": SUPPLIER_CONFIRM_PATH},
            {"method": "POST", "path": SUPPLIER_REFRESH_APPLY_PATH},
        ], fixture.REPORT["writes"]
        assert wb_create_report is not None
        checks.append("synthetic_supplier_refresh_review_and_wb_create_have_zero_provider_attempts_external_requests_and_http_errors")
        page.wait_for_load_state("networkidle")
        context.unroute_all(behavior="wait")
        context.close()
        browser.close()

    return {
        "status": "passed",
        "scope": "synthetic_filtered_supplier_update_selection_review_and_classic_beta_handoff",
        "checks": checks,
        "category_editor": {
            "search_reads": len(category_search_reads),
            "local_updates": len(category_update_posts),
            "saved_label": category_update_posts[0]["payload"]["wb_category_name"],
        },
        "provider_attempts": fixture.REPORT["provider_attempts"],
        "selected_count": len(selected_ids),
        "imported_count": imported_count,
        "classic_update_form_fallback": True,
        "supplier_catalog_review": supplier_review_report,
        "filtered_update_selection": filtered_update_report,
        "supplier_refresh_handoff": supplier_refresh_handoff_report,
        "wb_create": wb_create_report,
        "screenshots": [
            freshness_screenshot,
            "supplier-filtered-import-batch-dark-320.png",
            supplier_review_screenshot,
            update_selection_screenshot,
            wb_create_report["screenshot"] if wb_create_report else None,
        ],
        "writes": fixture.REPORT["writes"],
        "external_requests": fixture.REPORT["unexpected_external_requests"],
        "unexpected_http": fixture.REPORT["unexpected_http"],
    }


def main():
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = {"status": "failed", "checks": [], "provider_attempts": 0}
    try:
        result = run()
    finally:
        if result.get("status") != "passed":
            result["provider_attempts"] = fixture.REPORT.get("provider_attempts", 0)
        REPORT_PATH.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        fixture.server.shutdown()
        fixture.TEMP.cleanup()


if __name__ == "__main__":
    main()
