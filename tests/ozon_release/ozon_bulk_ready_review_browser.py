"""Offline Chromium journey for exact mixed Ozon bulk-ready review.

The real Flask routes, SQLite preparation journal, v3 workers and operation
queue run against a new synthetic database. Browser and Python network access
are denied; queued operations are inspected but never dispatched to a provider.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import traceback
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright
from sqlalchemy import event
from werkzeug.serving import make_server
from tests.ux01.bulk_action_controls import invoke_workspace_action


OUT = Path(os.environ.get("OZON_BROWSER_ARTIFACTS", "/artifacts"))
OUT.mkdir(parents=True, exist_ok=True)
ASSETS = Path(__file__).with_name("assets")
ASSET_MANIFEST = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
for _asset in ASSET_MANIFEST.values():
    assert hashlib.sha256((ASSETS / _asset["file"]).read_bytes()).hexdigest() == _asset["sha256"]

report = {
    "status": "running",
    "checks": [],
    "provider_attempts": 0,
    "provider_dispatch_calls": 0,
    "provider_fake_status_reads": 0,
    "unexpected_external": [],
    "unexpected_http": [],
    "js_errors": [],
    "post_counts": {},
    "source_prepare": {},
    "brand_search": {},
    "review": {},
    "reviewed_queue": {},
    "reviewed_result_recovery": {},
    "review_editor_navigation": {},
    "saved_draft_selection": {},
    "mixed_type_selection": {},
    "scope": "synthetic_ozon_bulk_ready_review_browser",
    "multi_page_review": "not_run; 4-row journey exercises the real API; 200-row paging contract stays in its Node test",
}
TEMP = tempfile.TemporaryDirectory(prefix="ozon-bulk-ready-review-")
TEMP_ROOT = Path(TEMP.name)
os.environ.update(
    DATABASE_URL="sqlite:///" + str(TEMP_ROOT / "app.sqlite"),
    SKIP_SCHEDULER="1",
    IMAGE_LAB_INLINE_WORKER="0",
    SECRET_KEY="synthetic-ozon-bulk-ready-review-session",
    ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"),
    PUBLIC_BASE_URL="https://seller.test",
    MARKETPLACE_OZON_ENABLED="1",
    MARKETPLACE_OZON_PUBLICATION_ENABLED="1",
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED="0",
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED="0",
    OZON_RATE_LIMIT_DIR=str(TEMP_ROOT / "limits"),
    MARKETPLACE_IMAGE_ASSET_DIR=str(TEMP_ROOT / "assets"),
)


def forbid_network(*_args, **_kwargs):
    report["provider_attempts"] += 1
    raise AssertionError("External Python network is forbidden in this fixture")


requests.sessions.Session.request = forbid_network
socket.create_connection = forbid_network

from seller_platform import app
from models import (
    BackgroundJob,
    ImportedProduct,
    Marketplace,
    MarketplaceAttributeValue,
    MarketplaceAttributeDefinition,
    MarketplaceCategoryMapping,
    MarketplaceListing,
    MarketplaceOperation,
    MarketplaceProductDraft,
    MarketplaceProductType,
    Product,
    MarketplaceTaxonomyCategory,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerSupplier,
    SellerMarketplaceAccount,
    Supplier,
    SupplierProduct,
    db,
)
from services.marketplace_drafts import MarketplaceDraftService
from services.marketplace_publications import MarketplacePublicationService
from services.marketplace_adapters import MarketplaceCredentials
from services.ozon_api_client import OzonSellerAPIClient
from services.ozon_brand_search_models import OzonBrandSearchEvidence
from services.ozon_bulk_upload import OzonBulkUploadService
from routes.suppliers import _issue_supplier_import_batch_token
from tests.ozon_release.seed import PASSWORD, PHOTO, USERNAME, seed


app.config.update(
    TESTING=True,
    WTF_CSRF_ENABLED=True,
    SESSION_COOKIE_SECURE=False,
    SECRET_KEY=os.environ["SECRET_KEY"],
    MARKETPLACE_OZON_ENABLED=True,
    MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=False,
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
)
fixture = seed(app)
account_id = fixture["account_id"]
marketplace_id = None
source_ids = {}
source_titles = {}
expected_actions = {}
blocked_source_id = None


def _make_source(*, seller_id, external_id, vendor_code, title, description, category,
                 barcode="4600000000001"):
    """Build a source-complete but otherwise ordinary seller-owned product."""
    dimensions = {
        "Ширина упаковки, мм": 200,
        "Высота упаковки, мм": 30,
        "Длина упаковки, мм": 300,
        "Вес упаковки, г": 250,
    }
    raw = {
        "title": title,
        "description": description,
        "category": category,
        "vendor_code": vendor_code,
        "barcodes": [barcode],
        "photo_urls": [PHOTO],
        "dimensions": dimensions,
    }
    return ImportedProduct(
        seller_id=seller_id,
        external_id=external_id,
        external_vendor_code=vendor_code,
        source_type="synthetic",
        title=title,
        description=description,
        category=category,
        # The source display remains photo-cache-free. The observed raw photo
        # is still part of the fact pack used by the real preparation worker.
        photo_urls="[]",
        original_data=json.dumps(raw, ensure_ascii=False),
        calculated_price=1000.0,
        calculated_price_before_discount=1200.0,
        import_status="pending",
    )


with app.app_context():
    account = db.session.get(SellerMarketplaceAccount, account_id)
    marketplace_id = account.marketplace_id
    account.settings_json = json.dumps({"default_vat": "0.22"})
    account.connection_status = "connected"

    # Keep the seed's unrelated existing source out of the photo-cache path.
    seed_source = db.session.get(ImportedProduct, fixture["source_id"])
    seed_source.photo_urls = "[]"

    create_type = MarketplaceProductType.query.filter_by(
        marketplace_id=marketplace_id,
        external_type_id="1000",
    ).one()
    create_title = "BR Review · новая карточка"
    update_title = "BR Review · полное обновление"
    blocked_title = "BR Review · сохранённая готовность с дрейфом"
    excluded_title = "BR Review · готовая карточка без выбора"

    create_source = _make_source(
        seller_id=fixture["seller_id"],
        external_id="br-review-create-source",
        vendor_code="BR-REVIEW-CREATE",
        title=create_title,
        description="Синтетическое наблюдённое описание для новой карточки.",
        category=create_type.category.name,
    )

    update_listing = db.session.get(MarketplaceListing, fixture["listing_ids"][1])
    update_type = db.session.get(MarketplaceProductType, update_listing.product_type_id)
    update_listing.title = update_title
    update_listing.description = "Синтетическое наблюдённое описание для обновления."
    update_listing.external_category_id = update_type.category.external_category_id
    update_listing.external_type_id = update_type.external_type_id
    update_listing.attributes_json = "[]"
    update_listing.complex_attributes_json = "[]"
    update_listing.media_json = json.dumps({"primary_image": PHOTO, "images": []})
    update_listing.dimensions_json = json.dumps({
        "width": 200,
        "height": 30,
        "depth": 300,
        "weight": 250,
        "dimension_unit": "MILLIMETERS",
        "weight_unit": "GRAMS",
    })
    update_listing.barcodes_json = json.dumps(["4600000000001"])
    update_listing.price_summary_json = json.dumps({
        "available": True,
        "currency": "RUB",
        "values": {
            "price": "1000",
            "old_price": "1200",
            "currency_code": "RUB",
            "vat": "0.22",
        },
    })
    update_listing.imported_product_id = None
    update_listing.link_status = "unlinked"
    update_listing.link_source = None
    update_listing.link_evidence_json = "{}"
    update_source = _make_source(
        seller_id=fixture["seller_id"],
        external_id="br-review-update-source",
        vendor_code=update_listing.offer_id,
        title=update_title,
        description=update_listing.description,
        category=update_type.category.name,
    )
    # Give the linked update exact calculated values that the seller will
    # explicitly review before the ordinary preparation run.  The preparation
    # rebase must preserve these signed local decisions.
    update_source.calculated_price = 1300.0
    update_source.calculated_price_before_discount = 1500.0

    blocked_source = _make_source(
        seller_id=fixture["seller_id"],
        external_id="br-review-blocked-source",
        vendor_code="BR-REVIEW-BLOCKED",
        title=blocked_title,
        description="Синтетическое наблюдённое описание для проверки дрейфа.",
        category=create_type.category.name,
    )
    excluded_source = _make_source(
        seller_id=fixture["seller_id"],
        external_id="br-review-excluded-source",
        vendor_code="BR-REVIEW-EXCLUDED",
        title=excluded_title,
        description="Готовая синтетическая карточка, которую пользователь не выберет.",
        category=create_type.category.name,
        barcode="4600000000002",
    )
    db.session.add_all([create_source, update_source, blocked_source, excluded_source])
    db.session.flush()

    # A reviewed exact seller mapping makes the create rows selectable without
    # relying on lexical category inference in this browser fixture.
    identity = MarketplaceDraftService._mapping_identities(create_source)[-1]
    db.session.add(MarketplaceCategoryMapping(
        seller_id=fixture["seller_id"],
        marketplace_id=marketplace_id,
        supplier_id=identity["supplier_id"],
        product_type_id=create_type.id,
        scope_key=identity["scope_key"],
        source_type=identity["source_type"],
        source_category=identity["source_category"],
        source_category_normalized=identity["source_category_normalized"],
        external_category_id=create_type.category.external_category_id,
        external_type_id=create_type.external_type_id,
        mapping_source="manual",
        mapping_status="active",
        confidence=1.0,
        evidence_json='{"fixture":"exact_seller_mapping"}',
        corrected_by_user_id=None,
    ))
    update_listing.imported_product = update_source
    update_listing.link_status = "linked"
    update_listing.link_source = "exact_source_identity"
    update_listing.link_version = 1
    update_listing.linked_at = update_listing.last_seen_at = datetime.utcnow()
    update_listing.link_evidence_json = "{}"

    db.session.commit()

    # Seed the price edits through the real seller-owned service contract so
    # the browser comparison reflects reviewed outgoing values, rather than
    # unreviewed source calculations that the linked-update mapper omits.
    update_draft = MarketplaceDraftService.create_draft(
        seller_id=fixture["seller_id"],
        account_id=account_id,
        imported_product_id=update_source.id,
        corrected_by_user_id=account.seller.user.id,
        source_link_preflight=False,
        observed_mapping_preflight=False,
    )
    MarketplaceDraftService.update_draft(
        seller_id=fixture["seller_id"],
        draft_id=update_draft.id,
        expected_version=update_draft.version,
        patch={
            "commercial": {
                "price": "1300",
                "old_price": "1500",
                "vat": "0.22",
                "currency_code": "RUB",
            },
            "use_calculated_price": True,
            "confirm_old_price_edit": True,
        },
        corrected_by_user_id=account.seller.user.id,
        category_review_required=False,
    )

    source_ids = {
        "create": create_source.id,
        "update": update_source.id,
        "blocked": blocked_source.id,
        "excluded": excluded_source.id,
    }
    source_titles = {
        "create": create_title,
        "update": update_title,
        "blocked": blocked_title,
        "excluded": excluded_title,
    }
    expected_actions = {"create": "create", "update": "update"}
    blocked_source_id = blocked_source.id


logging.getLogger("werkzeug").setLevel(logging.ERROR)
server = make_server("127.0.0.1", 0, app, threaded=True)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
base = f"http://127.0.0.1:{server.server_port}"
photo_svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="100"><rect width="80" height="100" fill="#c7a66a"/></svg>'
state = {
    "source_job_uid": None,
    "source_item_ids": [],
    "source_prepare_expected_ids": [],
    "source_prepare_post": None,
    "source_prepare_repair_post": None,
    "source_worker_stats": None,
    "source_run_document": None,
    "reviewed_post": None,
    "reviewed_worker_stats": None,
    "review_api_pages": [],
    "queue_confirmed": False,
    "recovery_initial_queue_pending": False,
    "recovery_confirmation_pending": False,
    "recovery_review_api_pages": [],
    "recovery_review_queries": [],
    "review_editor_navigation_job_uid": None,
    "review_editor_navigation_draft_ids": [],
    "review_editor_navigation_api_pages": [],
    "review_editor_navigation_draft_id": None,
    "review_editor_navigation_initial_version": None,
    "review_editor_navigation_save_posts": 0,
    "review_editor_navigation_validate_posts": 0,
    "review_editor_navigation_saved_version": None,
    "review_editor_navigation_validated_version": None,
    "package_confirmation_active": False,
    "package_confirmation_draft_ids": [],
    "package_confirmation_post": None,
}
business_write_events = []
BUSINESS_TABLES = {
    "imported_products",
    "marketplace_product_drafts",
    "marketplace_category_mappings",
    "ozon_bulk_upload_runs",
    "ozon_bulk_upload_items",
    "marketplace_operations",
}


def record_business_write(_conn, _cursor, statement, _parameters, _context, _executemany):
    match = re.match(
        r"\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+[\"`]?([A-Za-z0-9_]+)",
        statement,
        re.IGNORECASE,
    )
    if match and match.group(1).lower() in BUSINESS_TABLES:
        business_write_events.append(match.group(1).lower())


with app.app_context():
    event.listen(db.engine, "before_cursor_execute", record_business_write)


def _run_real_source_worker(job_uid):
    with app.app_context():
        stats = OzonBulkUploadService.run_due_preparation()
        job = BackgroundJob.query.filter_by(job_uid=job_uid).one()
        run = OzonBulkUploadRun.query.filter_by(job_id=job.id).one()
        items = OzonBulkUploadItem.query.filter_by(run_id=run.id).order_by(
            OzonBulkUploadItem.ordinal.asc()
        ).all()
        assert run.mode == "source_prepare"
        assert run.account_id == account_id
        assert run.seller_id == fixture["seller_id"]
        assert len(items) == 4
        assert {item.imported_product_id for item in items} == set(source_ids.values())
        assert all(item.phase == "prepared" and item.draft_id for item in items), [
            (item.imported_product_id, item.phase, item.error_code) for item in items
        ]
        drafts = [db.session.get(MarketplaceProductDraft, item.draft_id) for item in items]
        assert all(draft.status == "ready" for draft in drafts)
        assert MarketplaceOperation.query.count() == 0

        blocked_draft = MarketplaceProductDraft.query.filter_by(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            imported_product_id=blocked_source_id,
        ).one()
        assert blocked_draft.status == "ready"
        stored_validation = json.loads(blocked_draft.validation_result_json or "{}")
        assert stored_validation.get("publishable") is True

        # Simulate a source change after preparation while leaving the cached
        # ready status intact. The read-only review must revalidate and block it.
        blocked_product = db.session.get(ImportedProduct, blocked_source_id)
        blocked_product.description = "Изменение источника после локальной подготовки."
        db.session.commit()

        run_document = OzonBulkUploadService.public_document(job, detail=True)
        state["source_worker_stats"] = stats
        state["source_run_document"] = run_document
        state["source_item_ids"] = [item.imported_product_id for item in items]
        state["draft_ids"] = [item.draft_id for item in items]
        state["prepared_versions"] = {
            str(item.draft_id): item.prepared_version for item in items
        }
        state["prepared_phases"] = [item.phase for item in items]
        return stats


def _run_real_review_worker():
    with app.app_context():
        stats = OzonBulkUploadService.run_due_preparation()
        operations = MarketplaceOperation.query.filter_by(
            seller_id=fixture["seller_id"],
            account_id=account_id,
        ).order_by(MarketplaceOperation.id.asc()).all()
        run = OzonBulkUploadRun.query.filter_by(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            mode="reviewed_drafts",
        ).one()
        assert stats["enqueued"] == 2, stats
        assert len(operations) == 2
        assert sorted(operation.operation_kind for operation in operations) == [
            "product_import", "product_update",
        ]
        assert all(operation.status == "queued" for operation in operations)
        assert all(operation.attempt_count == 0 for operation in operations)
        assert all(operation.poll_count == 0 for operation in operations)
        assert all(operation.submitted_at is None for operation in operations)
        assert all(operation.seller_id == fixture["seller_id"] for operation in operations)
        assert all(operation.account_id == account_id for operation in operations)
        state["reviewed_worker_stats"] = stats
        state["queued_operation_kinds"] = sorted(
            operation.operation_kind for operation in operations
        )
        state["queued_operation_statuses"] = [operation.status for operation in operations]
        state["reviewed_run_uid"] = BackgroundJob.query.filter_by(
            id=run.job_id
        ).one().job_uid
        return stats


def _seed_recovery_source_run():
    """Create five real prepared drafts for a settled mixed-result run."""
    recovery_titles = {
        "stale": "BR Recovery · изменённая версия",
        "rejected": "BR Recovery · окончательный отказ Ozon",
        "success": "BR Recovery · подтверждённая карточка",
        "noop": source_titles["update"],
        "uncertain": "BR Recovery · итог требует сверки",
    }
    with app.app_context():
        # The first journey already proved update queue creation. Mark that
        # fixture operation terminal so the same linked source can exercise a
        # second, independently reviewed update without an active-write fence.
        first_update = MarketplaceOperation.query.filter_by(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            draft_id=state["draft_ids"][1],
        ).one()
        assert first_update.operation_kind == "product_update"
        first_update.status = "succeeded"
        first_update.completed_at = datetime.utcnow()
        first_update.next_poll_at = None
        first_update.quota_reserved = 0
        db.session.commit()

        product_type = MarketplaceProductType.query.filter_by(
            marketplace_id=marketplace_id,
            external_type_id="1000",
        ).one()
        recovery_sources = {}
        for ordinal, kind in enumerate(("stale", "rejected", "success", "uncertain"), 1):
            source = _make_source(
                seller_id=fixture["seller_id"],
                external_id=f"br-recovery-{kind}-source",
                vendor_code=f"BR-RECOVERY-{kind.upper()}",
                title=recovery_titles[kind],
                description=f"Синтетические данные для восстановления {kind}.",
                category=product_type.category.name,
                barcode=f"460000000{ordinal:04d}",
            )
            recovery_sources[kind] = source
            db.session.add(source)

        foreign_listing = db.session.get(
            MarketplaceListing, fixture["foreign_listing_id"],
        )
        foreign_source = _make_source(
            seller_id=foreign_listing.seller_id,
            external_id="br-recovery-foreign-source",
            vendor_code="BR-RECOVERY-FOREIGN",
            title="BR Recovery · чужой черновик",
            description="Синтетический чужой черновик для проверки границы владения.",
            category=product_type.category.name,
            barcode="4600000000999",
        )
        db.session.add(foreign_source)
        db.session.flush()
        foreign_draft = MarketplaceDraftService.create_draft(
            seller_id=foreign_listing.seller_id,
            account_id=fixture["foreign_account_id"],
            imported_product_id=foreign_source.id,
            product_type_id=product_type.id,
        )
        db.session.commit()

        recovery_ids = [
            recovery_sources["stale"].id,
            recovery_sources["rejected"].id,
            recovery_sources["success"].id,
            source_ids["update"],
            recovery_sources["uncertain"].id,
        ]
        acceptance = OzonBulkUploadService.accept_source_prepare(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            imported_product_ids=recovery_ids,
            request_key="r42-recovery-source-prepare-key-0000000000000001",
            created_by_user_id=fixture.get("user_id"),
        )
        job = acceptance.job
        stats = OzonBulkUploadService.run_due_preparation()
        run = OzonBulkUploadRun.query.filter_by(job_id=job.id).one()
        items = OzonBulkUploadItem.query.filter_by(run_id=run.id).order_by(
            OzonBulkUploadItem.ordinal.asc(),
        ).all()
        assert len(items) == 5
        assert [item.imported_product_id for item in items] == recovery_ids
        assert all(item.phase == "prepared" and item.draft_id for item in items), [
            (item.imported_product_id, item.phase, item.error_code) for item in items
        ]
        drafts = [db.session.get(MarketplaceProductDraft, item.draft_id) for item in items]
        assert all(draft.status == "ready" for draft in drafts)
        state["recovery_source_job_uid"] = job.job_uid
        state["recovery_source_draft_ids"] = [item.draft_id for item in items]
        state["recovery_draft_by_kind"] = {
            "stale": items[0].draft_id,
            "rejected": items[1].draft_id,
            "success": items[2].draft_id,
            "noop": items[3].draft_id,
            "uncertain": items[4].draft_id,
        }
        state["recovery_titles"] = recovery_titles
        state["foreign_draft_id"] = foreign_draft.id
        state["recovery_source_worker_stats"] = stats
        state["recovery_initial_queue_pending"] = True
        return job.job_uid


def _edit_recovery_draft_after_acceptance():
    """Edit one accepted row before the real reviewed worker checks versions."""
    draft_id = state["recovery_draft_by_kind"]["stale"]
    expected_version = state["recovery_review_versions"][draft_id]
    with app.app_context():
        draft = db.session.get(MarketplaceProductDraft, draft_id)
        assert draft.version == expected_version
        edited_content = json.loads(draft.content_json or "{}")
        edited_content["description"] = (
            "Изменено после подтверждения списка, до запуска worker."
        )
        edited = MarketplaceDraftService.update_draft(
            seller_id=fixture["seller_id"],
            draft_id=draft_id,
            expected_version=expected_version,
            patch={"content": edited_content},
            corrected_by_user_id=fixture.get("user_id"),
        )
        assert edited.version > expected_version
        validated = MarketplaceDraftService.validate_draft(
            seller_id=fixture["seller_id"],
            draft_id=draft_id,
            expected_version=edited.version,
        )
        state["recovery_edited_version"] = validated.version
        assert validated.version > expected_version
        assert validated.status == "ready", (
            validated.status,
            validated.validation_result_json,
        )
        db.session.expire_all()
        latest = db.session.get(MarketplaceProductDraft, draft_id)
        assert latest.version == validated.version
        assert json.loads(latest.content_json)["description"] == (
            "Изменено после подтверждения списка, до запуска worker."
        )


def _run_real_recovery_review_worker():
    """Run the accepted batch, then persist mixed provider outcomes locally."""
    with app.app_context():
        stats = OzonBulkUploadService.run_due_preparation()
        job = BackgroundJob.query.filter_by(
            job_uid=state["recovery_initial_result_uid"],
            seller_id=fixture["seller_id"],
        ).one()
        run = OzonBulkUploadRun.query.filter_by(job_id=job.id).one()
        rows = OzonBulkUploadItem.query.filter_by(run_id=run.id).order_by(
            OzonBulkUploadItem.ordinal.asc(),
        ).all()
        assert run.mode == "reviewed_drafts"
        assert len(rows) == 5
        row_by_draft = {row.draft_id: row for row in rows}
        drafts = state["recovery_draft_by_kind"]
        stale = row_by_draft[drafts["stale"]]
        assert stale.phase == "needs_input", (stale.phase, stale.error_code)
        assert stale.operation_id is None
        assert stale.reviewed_version == state["recovery_review_versions"][drafts["stale"]]
        assert state["recovery_edited_version"] > stale.reviewed_version
        operations = {
            kind: db.session.get(MarketplaceOperation, row_by_draft[draft_id].operation_id)
            for kind, draft_id in drafts.items()
            if kind != "stale"
        }
        assert len(operations) == 4 and all(operations.values())
        assert stats["enqueued"] == 4, stats
        assert operations["noop"].operation_kind == "product_update"

        now = datetime.utcnow()
        success = operations["success"]
        success.status = "succeeded"
        success.error_code = None
        success.error_message = None
        success.quota_reserved = 0
        success.next_poll_at = None
        success.completed_at = now
        success.item_results_json = json.dumps([{
            "offer_id": success.draft.offer_id,
            "product_id": "98765001",
            "status": "imported",
            "errors": [],
        }], ensure_ascii=False)
        db.session.commit()

        uncertain = operations["uncertain"]
        MarketplacePublicationService._mark_uncertain(
            uncertain,
            code="synthetic_task_status_unknown",
            message="Синтетическая неопределённость для проверки исключения из восстановления.",
            now=now,
        )
        db.session.expire_all()
        uncertain = db.session.get(MarketplaceOperation, uncertain.id)
        stopped_uncertain = MarketplacePublicationService.resolve_uncertain(
            seller_id=fixture["seller_id"],
            operation_id=uncertain.id,
            expected_version=uncertain.version,
            reason="Синтетическая приёмка: остановить сверку без повторной записи.",
            resolved_by_user_id=uncertain.created_by_user_id,
            now=now,
        )
        assert stopped_uncertain.status == "uncertain"
        assert stopped_uncertain.next_poll_at is None
        assert "manual_resolution" in json.loads(
            stopped_uncertain.request_summary_json or "{}"
        )

        no_op = operations["noop"]
        submitted = json.loads(no_op.snapshot.submitted_state_json or "{}")
        MarketplacePublicationService._mark_update_already_current(
            no_op,
            live={
                "identity": {
                    "offer_id": no_op.draft.offer_id,
                    "product_id": no_op.listing.external_product_id,
                },
                "payload": submitted,
                "fingerprint": no_op.request_fingerprint,
            },
            now=now,
        )

        rejected = operations["rejected"]
        rejected.status = "submitted"
        rejected.attempt_count = 1
        rejected.external_task_id = "991234567"
        rejected.submitted_at = now
        rejected.next_poll_at = now
        rejected.deadline_at = None
        db.session.commit()
        rejected_offer_id = rejected.draft.offer_id
        recovery_account = db.session.get(SellerMarketplaceAccount, account_id)
        recovery_external_account_id = recovery_account.external_account_id

        class FakeTaskStatusAdapter:
            def require_capability(self, _capability):
                return None

            def get_submission(self, credentials, payload):
                assert credentials.external_account_id == recovery_external_account_id
                assert payload == {"task_id": 991234567}
                report["provider_fake_status_reads"] += 1
                return {"result": {
                    "total": 1,
                    "items": [{
                        "offer_id": rejected_offer_id,
                        "product_id": 0,
                        "status": "failed",
                        "errors": [{
                            "code": "SYNTHETIC_DEFINITIVE_REJECT",
                            "message": "Synthetic item rejection",
                        }],
                    }],
                }}

        credentials = MarketplaceCredentials(
            external_account_id=recovery_external_account_id,
            api_key="synthetic-recovery-status-read",
        )
        # The harness normally replaces poll_operation with a write-denial
        # sentinel. Restore the real method for this single fake status read;
        # allow_submission=False guarantees no provider write method can run.
        MarketplacePublicationService.poll_operation = original_poll_operation
        try:
            final_rejection = original_poll_operation(
                seller_id=fixture["seller_id"],
                operation_id=rejected.id,
                adapter=FakeTaskStatusAdapter(),
                credentials=credentials,
                now=now,
                allow_submission=False,
            )
        finally:
            MarketplacePublicationService.poll_operation = forbid_dispatch
        assert final_rejection.status == "failed"
        assert final_rejection.error_code == "ozon_import_failed"
        assert final_rejection.snapshot is not None
        confirmed_state = json.loads(final_rejection.snapshot.confirmed_state_json or "{}")
        assert confirmed_state.get("source") == "task_status"
        assert confirmed_state.get("result", {}).get("items", [{}])[0].get("status") == "failed"
        assert report["provider_fake_status_reads"] == 1

        OzonBulkUploadService.reconcile_run(
            seller_id=fixture["seller_id"], job_uid=job.job_uid,
        )
        state["recovery_initial_operation_ids"] = {
            kind: operation.id for kind, operation in operations.items()
        }
        state["recovery_initial_operation_statuses"] = {
            kind: operation.status for kind, operation in operations.items()
        }
        state["recovery_initial_result_uid"] = job.job_uid
        state["recovery_initial_worker_stats"] = stats
        state["recovery_expected_ids"] = [drafts["stale"], drafts["rejected"]]
        state["recovery_excluded_ids"] = [
            drafts["success"], drafts["noop"], drafts["uncertain"],
        ]
        state["recovery_confirmation_pending"] = False
        return stats


def _run_real_recovery_followup_worker():
    """Queue only the drafts explicitly accepted from the recovery review."""
    with app.app_context():
        stats = OzonBulkUploadService.run_due_preparation()
        job = BackgroundJob.query.filter_by(
            job_uid=state["recovery_final_result_uid"],
            seller_id=fixture["seller_id"],
        ).one()
        run = OzonBulkUploadRun.query.filter_by(job_id=job.id).one()
        rows = OzonBulkUploadItem.query.filter_by(run_id=run.id).order_by(
            OzonBulkUploadItem.ordinal.asc(),
        ).all()
        assert run.mode == "reviewed_drafts"
        assert len(rows) == 2
        assert {row.draft_id for row in rows} == set(state["recovery_expected_ids"])
        assert all(row.phase == "operation_linked" and row.operation_id for row in rows), [
            (row.draft_id, row.phase, row.error_code) for row in rows
        ]
        assert stats["enqueued"] == 2, stats
        operations = [db.session.get(MarketplaceOperation, row.operation_id) for row in rows]
        assert all(operation.status == "queued" for operation in operations)
        assert all(operation.attempt_count == 0 and operation.poll_count == 0 for operation in operations)
        assert all(operation.draft_id in state["recovery_expected_ids"] for operation in operations)
        state["recovery_final_worker_stats"] = stats
        state["recovery_final_operation_ids"] = [operation.id for operation in operations]
        state["recovery_final_operation_statuses"] = [operation.status for operation in operations]


def bridge(route):
    request = route.request
    url = urlsplit(request.url)

    if url.hostname == "127.0.0.1" and url.port == server.server_port:
        path = url.path
        if request.method not in ("GET", "HEAD"):
            navigation_draft_id = state.get("review_editor_navigation_draft_id")
            navigation_save_path = (
                f"/marketplaces/drafts/{navigation_draft_id}"
                if navigation_draft_id else None
            )
            navigation_validate_path = (
                f"/marketplaces/drafts/{navigation_draft_id}/validate"
                if navigation_draft_id else None
            )
            if path not in {
                "/login",
                "/marketplaces/ozon/uploads/",
                "/marketplaces/ozon/uploads/from-drafts",
            } and not (
                request.method == "POST"
                and path in {navigation_save_path, navigation_validate_path}
            ):
                if not (request.method == "POST" and path.endswith("/repair/apply")):
                    report["unexpected_http"].append({"method": request.method, "path": path})
                    route.abort()
                    return
            if path != "/login":
                report["post_counts"][path] = report["post_counts"].get(path, 0) + 1
                if not request.headers.get("x-csrftoken"):
                    raise AssertionError(f"CSRF header missing for {path}")

        if request.method == "GET" and path == "/marketplaces/ozon/uploads/api/review":
            query = parse_qs(url.query, keep_blank_values=True)
            is_negative_probe = state.get("recovery_negative_probe_active") is True
            if not is_negative_probe:
                assert query.get("account_id") == [str(account_id)], query
            ids = [int(value) for value in query.get("draft_ids", [""])[0].split(",")]
            page = int(query.get("page", ["1"])[0])
            if query.get("result_job_uid"):
                assert query.get("result_job_uid") == [state["recovery_initial_result_uid"]], query
                assert not query.get("parent_prepare_job_uid"), query
                state["recovery_review_api_pages"].append(page)
                state["recovery_review_queries"].append({
                    "account_id": query.get("account_id"),
                    "draft_ids": ids,
                    "result_job_uid": query.get("result_job_uid"),
                    "page": page,
                })
                assert page == 1
                if ids == state["recovery_expected_ids"]:
                    pass
                else:
                    # Negative exact-scope probes deliberately ask for one
                    # altered ID set; the route must reject every one.
                    assert state.get("recovery_negative_probe_active") is True
            elif query.get("parent_prepare_job_uid") == [state.get("recovery_source_job_uid")]:
                assert ids == state["recovery_source_draft_ids"], (ids, state["recovery_source_draft_ids"])
                state.setdefault("recovery_source_review_api_pages", []).append(page)
            elif query.get("parent_prepare_job_uid") == [
                state.get("review_editor_navigation_job_uid")
            ]:
                assert ids == state["review_editor_navigation_draft_ids"], (
                    ids, state["review_editor_navigation_draft_ids"],
                )
                state["review_editor_navigation_api_pages"].append(page)
            elif query.get("parent_prepare_job_uid") == [
                state.get("brand_search_job_uid")
            ]:
                assert ids == state["brand_search_draft_ids"], (
                    ids, state["brand_search_draft_ids"],
                )
                state.setdefault("brand_search_review_api_pages", []).append(page)
            elif state.get("selection_draft_ids") and not query.get("parent_prepare_job_uid"):
                assert set(ids) == set(state["selection_draft_ids"]), query
                assert len(ids) == len(state["selection_draft_ids"]), query
                state["selection_review_read"] = True
            else:
                assert query.get("parent_prepare_job_uid") == [state["source_job_uid"]], query
                assert ids == state["draft_ids"], (ids, state["draft_ids"])
                state["review_api_pages"].append(page)

        navigation_draft_id = state.get("review_editor_navigation_draft_id")
        if request.method == "POST" and navigation_draft_id:
            save_path = f"/marketplaces/drafts/{navigation_draft_id}"
            validate_path = save_path + "/validate"
            if path == save_path:
                body = json.loads(request.post_data or "{}")
                assert set(body) == {"expected_version", "patch"}, set(body)
                assert body["expected_version"] == state[
                    "review_editor_navigation_initial_version"
                ]
                dimensions = body["patch"].get("dimensions")
                assert dimensions and dimensions.get("weight") == "250", dimensions
                assert dimensions.get("weight_unit") == "GRAMS", dimensions
                assert body["patch"].get("confirm_package") is True, body["patch"]
                state["review_editor_navigation_save_posts"] += 1
            elif path == validate_path:
                body = json.loads(request.post_data or "{}")
                assert set(body) == {"expected_version"}, set(body)
                assert body["expected_version"] == state[
                    "review_editor_navigation_saved_version"
                ]
                state["review_editor_navigation_validate_posts"] += 1

        if request.method == "POST" and path == "/marketplaces/ozon/uploads/":
            body = json.loads(request.post_data or "{}")
            assert set(body) == {
                "account_id", "imported_product_ids", "request_key", "confirm_prepare",
            }, set(body)
            assert body["account_id"] == account_id
            assert body["confirm_prepare"] is True
            if state.get("brand_search_case_active"):
                expected_ids = state["brand_search_expected_ids"]
                assert set(body["imported_product_ids"]) == set(expected_ids)
                assert len(body["imported_product_ids"]) == 3
            else:
                expected_ids = state["source_prepare_expected_ids"]
                assert set(body["imported_product_ids"]) == set(expected_ids)
                assert len(body["imported_product_ids"]) == 4
            assert re.fullmatch(r"[A-Za-z0-9_-]{24,128}", body["request_key"])
            receipt_key = (
                "brand_search_prepare_post"
                if state.get("brand_search_case_active")
                else "source_prepare_post"
            )
            state[receipt_key] = {
                "account_id": body["account_id"],
                "source_ids": body["imported_product_ids"],
                "confirm_prepare": body["confirm_prepare"],
                "csrf": True,
            }

        if request.method == "POST" and path.endswith("/repair/apply"):
            form = parse_qs(request.post_data or "", keep_blank_values=True)
            selected_ids = [int(value) for value in form.get("selected_draft_id", [])]
            selection_mode = path == "/marketplaces/ozon/uploads/selection/repair/apply"
            expected = (
                state["package_confirmation_draft_ids"]
                if selection_mode and state.get("package_confirmation_active")
                else state["selection_draft_ids"] if selection_mode
                else state["brand_search_ambiguous_draft_ids"]
                if state.get("brand_search_case_active")
                else state["repair_row_ids"]
            )
            if selection_mode:
                assert sorted(selected_ids) == sorted(expected), selected_ids
                assert form.get("confirm_local") == ["1"]
                query = parse_qs(url.query)
                assert query["account_id"] == [str(account_id)], query
                assert query["draft_ids"] == [",".join(map(str, expected))], query
                if state.get("package_confirmation_active"):
                    target_id, other_id = expected
                    assert form.get(f"row_{target_id}_confirm_package") == ["1"]
                    assert f"row_{other_id}_confirm_package" not in form
                    assert form.get("confirm_local") == ["1"]
                    state["package_confirmation_post"] = {
                        "draft_ids": selected_ids,
                        "confirmed_draft_ids": [target_id],
                        "csrf": bool(request.headers.get("x-csrftoken")),
                    }
            else:
                assert selected_ids == expected, selected_ids
            receipt_key = (
                "selection_repair_post" if selection_mode
                else "brand_search_repair_post"
                if state.get("brand_search_case_active")
                else "source_prepare_repair_post"
            )
            state[receipt_key] = {
                "draft_ids": selected_ids,
                "csrf": bool(request.headers.get("x-csrftoken")),
            }

        if request.method == "POST" and path == "/marketplaces/ozon/uploads/from-drafts":
            body = json.loads(request.post_data or "{}")
            assert body.get("account_id") == account_id
            assert body.get("confirm_write") is True
            assert re.fullmatch(r"[A-Za-z0-9_-]{24,128}", body.get("request_key", ""))
            if state.get("recovery_confirmation_pending"):
                expected_ids = state["recovery_expected_ids"]
                assert body.get("result_job_uid") == state["recovery_initial_result_uid"]
                assert not body.get("parent_prepare_job_uid")
                assert set(body.get("draft_ids", [])) == set(expected_ids)
                assert len(body.get("draft_ids", [])) == len(expected_ids)
                expected_versions = state["recovery_current_review_versions"]
                assert body.get("expected_versions") == {
                    str(draft_id): expected_versions[draft_id]
                    for draft_id in body["draft_ids"]
                }
                state["recovery_final_post"] = {
                    "account_id": body["account_id"],
                    "draft_ids": body["draft_ids"],
                    "expected_versions": body["expected_versions"],
                    "result_job_uid": body["result_job_uid"],
                    "confirm_write": body["confirm_write"],
                    "csrf": True,
                }
            elif state.get("recovery_initial_queue_pending"):
                expected_ids = state["recovery_source_draft_ids"]
                assert body.get("parent_prepare_job_uid") == state["recovery_source_job_uid"]
                assert not body.get("result_job_uid")
                assert set(body.get("draft_ids", [])) == set(expected_ids)
                assert len(body.get("draft_ids", [])) == 5
                assert set(body.get("expected_versions", {})) == {
                    str(draft_id) for draft_id in expected_ids
                }
                assert all(
                    type(version) is int and version == state["recovery_review_versions"][draft_id]
                    for draft_id, version in (
                        (int(key), value) for key, value in body["expected_versions"].items()
                    )
                )
                state["recovery_initial_post"] = {
                    "account_id": body["account_id"],
                    "draft_ids": body["draft_ids"],
                    "expected_versions": body["expected_versions"],
                    "parent_prepare_job_uid": body["parent_prepare_job_uid"],
                    "confirm_write": body["confirm_write"],
                    "csrf": True,
                }
            else:
                assert body.get("parent_prepare_job_uid") == state["source_job_uid"]
                assert not body.get("result_job_uid")
                assert set(body.get("draft_ids", [])) == set(state["ready_draft_ids"])
                assert len(body.get("draft_ids", [])) == 2
                assert set(body.get("expected_versions", {})) == {
                    str(draft_id) for draft_id in state["ready_draft_ids"]
                }
                assert all(
                    type(version) is int and version == state["review_versions"][draft_id]
                    for draft_id, version in (
                        (int(key), value) for key, value in body["expected_versions"].items()
                    )
                )
                state["reviewed_post"] = {
                    "account_id": body["account_id"],
                    "draft_ids": body["draft_ids"],
                    "expected_versions": body["expected_versions"],
                    "parent_prepare_job_uid": body["parent_prepare_job_uid"],
                    "confirm_write": body["confirm_write"],
                    "csrf": True,
                }
                state["queue_confirmed"] = True

        response = route.fetch(max_redirects=0)
        if response.status >= 500:
            report["unexpected_http"].append({"method": request.method, "path": path, "status": response.status})

        if request.method == "POST" and path == "/marketplaces/ozon/uploads/":
            assert response.status == 202, (response.status, response.text()[:300])
            accepted = response.json()
            assert accepted["success"] is True
            assert accepted["run"]["mode"] == "source_prepare"
            assert accepted["run"]["account_id"] == account_id
            if state.get("brand_search_case_active"):
                state["brand_search_job_uid"] = accepted["run"]["job_uid"]
                # Exercise the real preparation worker with only its Ozon read
                # boundary replaced by the synthetic exact response above.
                _run_brand_search_source_worker(state["brand_search_job_uid"])
            else:
                state["source_job_uid"] = accepted["run"]["job_uid"]
                # The real durable v3 worker runs only after the ordinary local
                # prepare POST has committed and before its normal response lands.
                _run_real_source_worker(state["source_job_uid"])

        elif request.method == "POST" and path == (
            f"/marketplaces/drafts/{state.get('review_editor_navigation_draft_id')}"
        ):
            assert response.status == 200, (response.status, response.text()[:300])
            body = response.json()
            assert body["success"] is True
            state["review_editor_navigation_saved_version"] = body["draft"]["version"]

        elif request.method == "POST" and path == (
            f"/marketplaces/drafts/{state.get('review_editor_navigation_draft_id')}/validate"
        ):
            assert response.status == 200, (response.status, response.text()[:300])
            body = response.json()
            assert body["success"] is True
            assert body["draft"]["validation"]["publishable"] is True
            state["review_editor_navigation_validated_version"] = body["draft"]["version"]

        elif request.method == "POST" and path == "/marketplaces/ozon/uploads/from-drafts":
            assert response.status == 202, (response.status, response.text()[:300])
            accepted = response.json()
            assert accepted["success"] is True
            assert accepted["run"]["mode"] == "reviewed_drafts"
            assert accepted["run"]["account_id"] == account_id
            if state.get("recovery_confirmation_pending"):
                state["recovery_final_result_uid"] = accepted["run"]["job_uid"]
                _run_real_recovery_followup_worker()
            elif state.get("recovery_initial_queue_pending"):
                state["recovery_initial_result_uid"] = accepted["run"]["job_uid"]
                _edit_recovery_draft_after_acceptance()
                _run_real_recovery_review_worker()
                state["recovery_initial_queue_pending"] = False
            else:
                # This is the original real queue worker, after the explicit
                # review/confirm POST. It never calls a provider.
                _run_real_review_worker()

        route.fulfill(response=response)
        return

    if request.url == PHOTO:
        route.fulfill(body=photo_svg, content_type="image/svg+xml")
        return

    asset = ASSET_MANIFEST.get(request.url) or ASSET_MANIFEST.get(request.url.rstrip("/"))
    if asset:
        route.fulfill(
            body=(ASSETS / asset["file"]).read_bytes(),
            content_type=asset["content_type"],
        )
        return

    report["unexpected_external"].append({"host": url.hostname, "path": url.path})
    route.abort()


def _business_counts():
    with app.app_context():
        return {
            "drafts": MarketplaceProductDraft.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
            ).count(),
            "runs": OzonBulkUploadRun.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
            ).count(),
            "items": OzonBulkUploadItem.query.join(
                OzonBulkUploadRun, OzonBulkUploadItem.run_id == OzonBulkUploadRun.id,
            ).filter(
                OzonBulkUploadRun.seller_id == fixture["seller_id"],
                OzonBulkUploadRun.account_id == account_id,
            ).count(),
            "operations": MarketplaceOperation.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
            ).count(),
        }


def _seed_legacy_package_confirmation_drafts():
    """Build two old WB-cache drafts using the current provenance helpers."""
    with app.app_context():
        product_type = MarketplaceProductType.query.filter_by(
            marketplace_id=marketplace_id,
            external_type_id="1000",
        ).one()
        draft_ids = []
        for index, suffix in enumerate(("target", "unchecked"), start=1):
            title = f"R103 · legacy package {suffix}"
            wb_product = Product(
                seller_id=fixture["seller_id"],
                nm_id=91991000 + index,
                vendor_code=f"R103-PACKAGE-{suffix.upper()}",
                title=title,
                dimensions_json=json.dumps({
                    "isValid": True,
                    "width": 20,
                    "height": 3,
                    "length": 30,
                    "weightBrutto": 0.25,
                }),
            )
            source = ImportedProduct(
                seller_id=fixture["seller_id"],
                product=wb_product,
                external_id=f"r103-legacy-package-{suffix}",
                external_vendor_code=f"R103-PACKAGE-{suffix.upper()}",
                source_type="synthetic",
                title=title,
                description="Синтетическая старая проекция упаковки WB.",
                category="Посуда",
                photo_urls="[]",
                original_data=json.dumps({
                    "title": title,
                    "description": "Синтетическая старая проекция упаковки WB.",
                }, ensure_ascii=False),
            )
            db.session.add(source)
            db.session.flush()
            draft = MarketplaceDraftService.create_draft(
                seller_id=fixture["seller_id"],
                account_id=account_id,
                imported_product_id=source.id,
                product_type_id=product_type.id,
            )
            facts, provenance, _fact_hash = MarketplaceDraftService._fact_snapshot(
                source
            )
            wb_suggestion = facts["unverified_suggestions"][
                "wb_package_dimensions"
            ]
            dimensions = MarketplaceDraftService._wb_package_dimensions_to_ozon(
                wb_suggestion
            )
            assert dimensions == {
                "width": "200", "height": "30", "depth": "300",
                "weight": "250", "dimension_unit": "MILLIMETERS",
                "weight_unit": "GRAMS",
            }
            facts["facts"].setdefault("physical", {})[
                "wb_package_dimensions"
            ] = wb_suggestion
            provenance["physical.wb_package_dimensions"] = {
                "source": "product.dimensions_json",
                "trust": "marketplace_observed",
            }
            draft.source_facts_json = MarketplaceDraftService._canonical_json(
                facts, dict,
            )
            draft.provenance_json = MarketplaceDraftService._canonical_json(
                provenance, dict,
            )
            draft.dimensions_json = MarketplaceDraftService._canonical_json(
                dimensions, dict,
            )
            db.session.commit()
            resolved = MarketplaceDraftService.package_confirmation(draft)
            assert resolved["required"] is True
            assert resolved["source"] == "legacy_wb_unverified"
            draft_ids.append(draft.id)
        return draft_ids


def _package_draft_snapshots(draft_ids):
    with app.app_context():
        snapshots = {}
        for draft_id in draft_ids:
            draft = db.session.get(MarketplaceProductDraft, draft_id)
            snapshots[draft_id] = {
                "version": draft.version,
                "dimensions": json.loads(draft.dimensions_json),
                "source_facts": json.loads(draft.source_facts_json),
                "provenance": json.loads(draft.provenance_json),
            }
        return snapshots


def run_package_confirmation_bulk_journey(context):
    draft_ids = _seed_legacy_package_confirmation_drafts()
    target_id, other_id = draft_ids
    state["package_confirmation_active"] = True
    state["package_confirmation_draft_ids"] = draft_ids
    state["package_confirmation_post"] = None
    baseline_counts = _business_counts()
    baseline_drafts = _package_draft_snapshots(draft_ids)
    baseline_operations = baseline_counts["operations"]
    baseline_posts = dict(report["post_counts"])
    baseline_writes = len(business_write_events)
    baseline_provider = (
        report["provider_attempts"], report["provider_dispatch_calls"],
    )

    page = context.new_page()
    page.set_default_timeout(20000)
    page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
    page.on("response", lambda response: report["unexpected_http"].append({
        "status": response.status,
        "path": urlsplit(response.url).path,
    }) if response.status >= 500 else None)
    page.goto(
        f"{base}/marketplaces/ozon/uploads/selection/repair?"
        + urlencode({
            "account_id": account_id,
            "draft_ids": ",".join(map(str, draft_ids)),
        })
    )
    editor = page.locator(
        '#ozon-bulk-repair[data-editor-mode="draft_selection"]:not([v-cloak])'
    )
    editor.wait_for()
    bootstrap = page.evaluate(
        "() => JSON.parse(document.querySelector('#orb-bootstrap').textContent)"
    )
    assert bootstrap["editor"]["mode"] == "draft_selection"
    assert bootstrap["editor"]["review_draft_ids"] == draft_ids
    editor_rows = {
        row["draft_id"]: row
        for group in bootstrap["editor"]["groups"]
        for row in group["rows"]
    }
    assert set(editor_rows) == set(draft_ids)
    for draft_id in draft_ids:
        assert editor_rows[draft_id]["package_confirmation"]["required"] is True
        assert editor_rows[draft_id]["package_confirmation"]["source"] == (
            "legacy_wb_unverified"
        )
        assert {
            "package_width_mm": editor_rows[draft_id]["package_width_mm"],
            "package_height_mm": editor_rows[draft_id]["package_height_mm"],
            "package_depth_mm": editor_rows[draft_id]["package_depth_mm"],
            "package_weight_g": editor_rows[draft_id]["package_weight_g"],
        } == {
            "package_width_mm": "200", "package_height_mm": "30",
            "package_depth_mm": "300", "package_weight_g": "250",
        }
        row = editor.locator(f'.orb-row[data-draft="{draft_id}"]')
        assert row.locator('input[type="checkbox"]').first.is_checked()
        row.get_by_role("button", name="Изменить", exact=True).click()
        row.locator("[data-package-confirmation-needed]").wait_for(
            state="visible",
        )
        confirm = page.locator(f'[data-package-confirm="{draft_id}"]')
        assert confirm.is_visible() and confirm.is_enabled()
        assert not confirm.is_checked()
        assert row.locator(f'input[name="width-{draft_id}"]').input_value() == "200"
        assert row.locator(f'input[name="height-{draft_id}"]').input_value() == "30"
        assert row.locator(f'input[name="depth-{draft_id}"]').input_value() == "300"
        assert row.locator(f'input[name="weight-{draft_id}"]').input_value() == "250"

    target_row = editor.locator(f'.orb-row[data-draft="{target_id}"]')
    target_confirm = page.locator(f'[data-package-confirm="{target_id}"]')
    target_width = target_row.locator(f'input[name="width-{target_id}"]')
    target_width.fill("")
    assert target_confirm.is_disabled()
    target_width.fill("200")
    assert target_confirm.is_enabled()
    assert not target_confirm.is_checked()
    other_confirm = page.locator(f'[data-package-confirm="{other_id}"]')
    assert not other_confirm.is_checked()
    assert len(business_write_events) == baseline_writes
    assert _business_counts() == baseline_counts
    assert report["post_counts"] == baseline_posts

    target_confirm.check()
    assert target_confirm.is_checked()
    assert not other_confirm.is_checked()
    assert target_width.input_value() == "200"
    page.locator("footer [data-draft-selection-save]").click()
    dialog = page.get_by_role("dialog")
    dialog.wait_for(state="visible")
    review_items = dialog.locator(".orb-review-list > li")
    target_item = review_items.filter(has_text="legacy package target")
    other_item = review_items.filter(has_text="legacy package unchecked")
    assert target_item.count() == 1
    assert other_item.count() == 1
    target_change = target_item.locator(".orb-review-field").filter(
        has_text="Подтверждение упаковки",
    )
    assert target_change.count() == 1
    assert "Нет" in target_change.inner_text() and "Да" in target_change.inner_text()
    assert other_item.locator(".orb-review-field").count() == 0
    assert "Подтверждение упаковки" not in other_item.inner_text()
    assert len(business_write_events) == baseline_writes
    assert _business_counts() == baseline_counts
    assert report["post_counts"] == baseline_posts

    apply_path = "/marketplaces/ozon/uploads/selection/repair/apply"
    with page.expect_response(lambda response: (
        response.request.method == "POST"
        and urlsplit(response.url).path == apply_path
    )) as apply_response_info:
        dialog.locator("[data-draft-selection-save]").click()
    apply_response = apply_response_info.value
    assert apply_response.status == 200
    apply_document = apply_response.json()
    assert apply_document["success"] is True, apply_document
    assert apply_document["report"]["updated"] == 1, apply_document["report"]
    assert apply_document["report"]["total"] == 2, apply_document["report"]
    assert state["package_confirmation_post"] == {
        "draft_ids": draft_ids,
        "confirmed_draft_ids": [target_id],
        "csrf": True,
    }
    assert report["post_counts"] == {**baseline_posts, apply_path: baseline_posts.get(apply_path, 0) + 1}
    receipt = editor.get_by_role("region", name="Результат сохранения")
    receipt.wait_for()
    page.wait_for_function(
        "document.activeElement?.matches('[data-draft-selection-receipt]')"
    )
    snapshots_after = _package_draft_snapshots(draft_ids)
    assert snapshots_after[target_id]["version"] > baseline_drafts[target_id]["version"]
    assert snapshots_after[target_id]["dimensions"] == baseline_drafts[target_id]["dimensions"]
    assert snapshots_after[other_id]["dimensions"] == baseline_drafts[other_id]["dimensions"]
    assert snapshots_after[other_id]["source_facts"] == baseline_drafts[other_id]["source_facts"]
    assert snapshots_after[other_id]["provenance"] == baseline_drafts[other_id]["provenance"]
    with app.app_context():
        confirmed_status = MarketplaceDraftService.package_confirmation(
            db.session.get(MarketplaceProductDraft, target_id),
        )
        untouched_status = MarketplaceDraftService.package_confirmation(
            db.session.get(MarketplaceProductDraft, other_id),
        )
        assert MarketplaceOperation.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id,
        ).count() == baseline_operations
    assert confirmed_status["source"] == "seller_confirmed"
    assert confirmed_status["required"] is False
    assert untouched_status["source"] == "legacy_wb_unverified"
    assert untouched_status["required"] is True
    assert _business_counts()["operations"] == baseline_operations
    assert (
        report["provider_attempts"], report["provider_dispatch_calls"],
    ) == baseline_provider == (0, 0)
    passed(
        "bulk_package_confirmation_partial_tuple_gated_and_exact_row_saved_locally"
    )
    page.close()
    state["package_confirmation_active"] = False


def _seed_brand_search_sources():
    """Add three exact-linked source rows with one fresh but incomplete dictionary."""
    now = datetime.utcnow()
    category_name = "R101 Brand Search"
    category_external_id = "99101"
    product_type_external_id = "99102"
    attribute_external_id = "31"
    ambiguous_brand = "R101 ambiguous brand"
    unique_brand = "R101 unique brand"
    titles = [
        "R101 brand · первая неоднозначная",
        "R101 brand · вторая неоднозначная",
        "R101 brand · единственный точный вариант",
    ]
    with app.app_context():
        category = MarketplaceTaxonomyCategory(
            marketplace_id=marketplace_id,
            external_category_id=category_external_id,
            name=category_name,
            full_path=category_name,
            is_available=True,
            last_seen_at=now,
        )
        db.session.add(category)
        db.session.flush()
        product_type = MarketplaceProductType(
            marketplace_id=marketplace_id,
            category_id=category.id,
            external_type_id=product_type_external_id,
            name=category_name,
            is_available=True,
            is_enabled=True,
            is_seller_selectable=True,
            attributes_synced_at=now,
            attributes_sync_status="success",
            attributes_schema_hash=hashlib.sha256(
                b"r101-brand-search-schema"
            ).hexdigest(),
            attributes_version=1,
            attributes_count=2,
            required_attributes_count=1,
        )
        db.session.add(product_type)
        db.session.flush()
        brand = MarketplaceAttributeDefinition(
            marketplace_id=marketplace_id,
            product_type_id=product_type.id,
            external_attribute_id=attribute_external_id,
            name="Бренд",
            data_type="String",
            is_required=True,
            dictionary_id="930001",
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=now,
            values_synced_at=now,
            values_sync_status="success",
            values_snapshot_hash=hashlib.sha256(
                b"r101-unrelated-complete-dictionary-sentinel"
            ).hexdigest(),
            values_version=1,
            values_count=1,
        )
        annotation = MarketplaceAttributeDefinition(
            marketplace_id=marketplace_id,
            product_type_id=product_type.id,
            external_attribute_id="4191",
            name="Аннотация",
            data_type="String",
            is_required=False,
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=now,
        )
        db.session.add_all([brand, annotation])
        db.session.flush()
        db.session.add(MarketplaceAttributeValue(
            marketplace_id=marketplace_id,
            product_type_id=product_type.id,
            attribute_id=brand.id,
            external_value_id="51001",
            value="R101 unrelated local dictionary value",
            value_normalized="r101 unrelated local dictionary value",
            is_available=True,
            last_seen_at=now,
        ))

        supplier = Supplier(
            name="R101 synthetic brand supplier",
            code="r101brandsearch",
            description="Synthetic source for the offline brand-search browser gate.",
        )
        db.session.add(supplier)
        db.session.flush()
        db.session.add(SellerSupplier(
            seller_id=fixture["seller_id"],
            supplier_id=supplier.id,
            is_active=True,
        ))

        sources = []
        source_brands = [ambiguous_brand, ambiguous_brand, unique_brand]
        for index, (title, source_brand) in enumerate(
            zip(titles, source_brands), 1,
        ):
            vendor_code = f"R101-BRAND-{index:02d}"
            external_id = f"r101-brand-search-{index:02d}"
            barcode = f"460000011{index:04d}"
            characteristics = [{"name": "Бренд", "value": source_brand}]
            source = _make_source(
                seller_id=fixture["seller_id"], external_id=external_id,
                vendor_code=vendor_code, title=title,
                description=f"Observed source description for {title}.",
                category=category_name, barcode=barcode,
            )
            raw = json.loads(source.original_data)
            raw.update(brand=source_brand, characteristics=characteristics)
            raw_json = json.dumps(raw, ensure_ascii=False)
            supplier_product = SupplierProduct(
                supplier_id=supplier.id,
                external_id=external_id,
                vendor_code=vendor_code,
                barcode=barcode,
                barcodes_json=json.dumps([barcode]),
                title=title,
                description=source.description,
                brand=source_brand,
                category=category_name,
                supplier_price=500.0,
                supplier_quantity=10,
                supplier_status="in_stock",
                characteristics_json=json.dumps(characteristics, ensure_ascii=False),
                dimensions_json=json.dumps(raw["dimensions"], ensure_ascii=False),
                photo_urls_json=json.dumps([PHOTO]),
                original_data_json=raw_json,
                content_revision=1,
            )
            source.source_type = supplier.code
            source.supplier_id = supplier.id
            source.supplier_product = supplier_product
            source.supplier_content_revision = supplier_product.content_revision
            source.brand = source_brand
            source.characteristics = json.dumps(characteristics, ensure_ascii=False)
            source.original_data = raw_json
            db.session.add_all([supplier_product, source])
            sources.append(source)
        db.session.flush()

        identity = MarketplaceDraftService._mapping_identities(sources[0])[-1]
        db.session.add(MarketplaceCategoryMapping(
            seller_id=fixture["seller_id"],
            marketplace_id=marketplace_id,
            supplier_id=supplier.id,
            product_type_id=product_type.id,
            scope_key=identity["scope_key"],
            source_type=identity["source_type"],
            source_category=identity["source_category"],
            source_category_normalized=identity["source_category_normalized"],
            external_category_id=category.external_category_id,
            external_type_id=product_type.external_type_id,
            mapping_source="manual",
            mapping_status="active",
            confidence=1.0,
            evidence_json='{"fixture":"r101_brand_search_exact_mapping"}',
            corrected_by_user_id=fixture.get("user_id"),
        ))
        db.session.commit()
        assert all(
            source.supplier_product_id
            and source.supplier_content_revision == source.supplier_product.content_revision
            and source.supplier_id == supplier.id
            and source.source_type == supplier.code
            and source.external_id == source.supplier_product.external_id
            for source in sources
        )
        assert MarketplaceDraftService._mapping_identities(sources[0])[-1][
            "source_category_normalized"
        ] == MarketplaceDraftService._mapping_identities(sources[2])[-1][
            "source_category_normalized"
        ]
        return {
            "source_ids": [source.id for source in sources],
            "ambiguous_source_ids": [source.id for source in sources[:2]],
            "unique_source_id": sources[2].id,
            "titles": titles,
            "ambiguous_brand": ambiguous_brand,
            "unique_brand": unique_brand,
        }


def _run_brand_search_source_worker(job_uid):
    """Run the production preparation worker against the synthetic search adapter."""
    with app.app_context():
        OzonBulkUploadService.run_due_preparation()
        job = BackgroundJob.query.filter_by(job_uid=job_uid).one()
        run = OzonBulkUploadRun.query.filter_by(job_id=job.id).one()
        items = OzonBulkUploadItem.query.filter_by(run_id=run.id).order_by(
            OzonBulkUploadItem.ordinal.asc()
        ).all()
        expected_ids = state["brand_search_expected_ids"]
        assert run.mode == "source_prepare"
        assert run.account_id == account_id and run.seller_id == fixture["seller_id"]
        assert [item.imported_product_id for item in items] == expected_ids
        expected_phases = [
            (
                source_id,
                "needs_input" if source_id in state["brand_search_ambiguous_source_ids"]
                else "prepared",
                "required_attribute_missing"
                if source_id in state["brand_search_ambiguous_source_ids"] else None,
            )
            for source_id in expected_ids
        ]
        actual_phases = [
            (item.imported_product_id, item.phase, item.error_code)
            for item in items
        ]
        assert actual_phases == expected_phases, (actual_phases, expected_phases)
        assert all(item.draft_id for item in items)
        drafts = [db.session.get(MarketplaceProductDraft, item.draft_id) for item in items]
        assert len(drafts) == 3
        assert MarketplaceOperation.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id,
        ).count() == state["brand_search_operations_before"]
        by_source = {draft.imported_product_id: draft for draft in drafts}
        ambiguous_source_ids = state["brand_search_ambiguous_source_ids"]
        unique_source_id = state["brand_search_unique_source_id"]
        ambiguous_drafts = [by_source[source_id] for source_id in ambiguous_source_ids]
        unique_draft = by_source[unique_source_id]

        def brand_cell(draft):
            attributes = json.loads(draft.attributes_json or "[]")
            return next((item for item in attributes if item.get("attribute_id") == "31"), None)

        assert all(brand_cell(draft) is None for draft in ambiguous_drafts)
        staged_unique = brand_cell(unique_draft)
        assert staged_unique and staged_unique["values"] == [{
            "dictionary_value_id": "11003",
            "value": "R101 unique brand",
        }]
        assert unique_draft.status == "ready"
        current = MarketplaceDraftService._build_validation_result(unique_draft)
        assert current["publishable"] is True, current

        evidence = OzonBrandSearchEvidence.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id,
        ).order_by(OzonBrandSearchEvidence.scope_hash.asc()).all()
        assert len(evidence) == 2
        by_choices = {
            tuple((row["dictionary_value_id"], row["value"]) for row in json.loads(item.choices_json)):
            item
            for item in evidence
        }
        ambiguous_choices = (
            ("11001", "R101 ambiguous brand"),
            ("11002", "R101 Ambiguous Brand"),
        )
        unique_choices = (("11003", "R101 unique brand"),)
        assert set(by_choices) == {ambiguous_choices, unique_choices}, set(by_choices)
        assert all(
            item.status == "completed" and item.complete_result and item.finished_at
            for item in evidence
        )
        assert not MarketplaceAttributeValue.query.filter(
            MarketplaceAttributeValue.product_type_id == unique_draft.product_type_id,
            MarketplaceAttributeValue.external_value_id.in_(
                ["11001", "11002", "11003"]
            ),
        ).count()
        state["brand_search_job_uid"] = job_uid
        state["brand_search_draft_ids"] = [item.draft_id for item in items]
        state["brand_search_ambiguous_draft_ids"] = [draft.id for draft in ambiguous_drafts]
        state["brand_search_unique_draft_id"] = unique_draft.id


def run_brand_search_picker_journey(context):
    """Use only existing beta preparation, native picker, local save, and review UI."""
    fixture_data = _seed_brand_search_sources()
    state["brand_search_expected_ids"] = fixture_data["source_ids"]
    state["brand_search_ambiguous_source_ids"] = fixture_data["ambiguous_source_ids"]
    state["brand_search_unique_source_id"] = fixture_data["unique_source_id"]
    state["brand_search_case_active"] = True
    state["brand_search_review_api_pages"] = []
    state["brand_search_operations_before"] = _business_counts()["operations"]
    dispatch_marker = report["provider_dispatch_calls"]
    publication_post_marker = report["post_counts"].get(
        "/marketplaces/ozon/uploads/from-drafts", 0,
    )
    search_payloads = []
    unique_page = None

    with app.app_context():
        owner_account = db.session.get(SellerMarketplaceAccount, account_id)
        import_batch = _issue_supplier_import_batch_token(
            user_id=owner_account.seller.user.id,
            seller_id=fixture["seller_id"],
            imported_product_ids=fixture_data["source_ids"],
        )
    assert import_batch

    def fake_brand_search(_client, payload):
        assert set(payload) == {
            "attribute_id", "description_category_id", "type_id", "limit", "value",
        }, payload
        assert {key: payload[key] for key in (
            "attribute_id", "description_category_id", "type_id", "limit",
        )} == {
            "attribute_id": 31,
            "description_category_id": 99101,
            "type_id": 99102,
            "limit": 100,
        }, payload
        assert payload["value"] in {
            fixture_data["ambiguous_brand"], fixture_data["unique_brand"],
        }, payload
        search_payloads.append(dict(payload))
        if payload["value"] == fixture_data["ambiguous_brand"]:
            return {"result": [
                {"id": 11001, "value": fixture_data["ambiguous_brand"]},
                {"id": 11002, "value": "R101 Ambiguous Brand"},
            ]}
        if payload["value"] == fixture_data["unique_brand"]:
            return {"result": [{
                "id": 11003,
                "value": fixture_data["unique_brand"],
            }]}
        raise AssertionError(f"Unexpected brand search value: {payload['value']!r}")

    page = context.new_page()
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1440, "height": 980})
    page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
    try:
        page.goto(base + "/my-products/beta?" + urlencode({"import_batch": import_batch}))
        workspace = page.locator("#my-products-app:not([v-cloak])")
        workspace.wait_for()
        assert workspace.locator(".mp-list article.mp-row").count() == 3
        for title in fixture_data["titles"]:
            page.get_by_role("checkbox", name=f"Выбрать {title}", exact=True).check()
        selected = workspace.locator(
            ".mp-list article.mp-row input[type=checkbox]:checked"
        ).evaluate_all("nodes => nodes.map(node => Number(node.value))")
        assert set(selected) == set(fixture_data["source_ids"])
        state["brand_search_expected_ids"] = selected

        invoke_workspace_action(page, "ozon")
        dialog = page.get_by_role("dialog")
        account_select = dialog.locator("#mp-ozon-account")
        account_select.wait_for()
        account_select.select_option(str(account_id))
        prepare_button = dialog.get_by_role(
            "button", name="Подготовить черновики", exact=True,
        )
        assert prepare_button.is_enabled()
        with patch.object(
            OzonSellerAPIClient,
            "search_description_category_attribute_values",
            new=fake_brand_search,
        ):
            with page.expect_response(lambda response: (
                response.request.method == "POST"
                and urlsplit(response.url).path == "/marketplaces/ozon/uploads/"
            )) as prepare_response_info:
                prepare_button.click()
        prepare_response = prepare_response_info.value
        assert prepare_response.status == 202
        page.wait_for_url(f"**/marketplaces/ozon/uploads/{state['brand_search_job_uid']}")
        assert state["brand_search_prepare_post"]["source_ids"] == selected
        assert sorted(payload["value"] for payload in search_payloads) == sorted([
            fixture_data["ambiguous_brand"], fixture_data["unique_brand"],
        ])
        report["brand_search"]["synthetic_provider_read_calls"] = len(search_payloads)

        repair_link = page.locator("a[data-source-prepare-repair]")
        repair_link.wait_for()
        repair_link.click()
        page.wait_for_url(
            f"**/marketplaces/ozon/uploads/{state['brand_search_job_uid']}/repair"
        )
        editor = page.locator(
            '#ozon-bulk-repair[data-editor-mode="source_prepare"]:not([v-cloak])'
        )
        editor.wait_for()
        editor_data = page.evaluate(
            "() => JSON.parse(document.querySelector('#orb-bootstrap').textContent).editor"
        )
        assert editor_data["mode"] == "source_prepare"
        assert editor_data["review_draft_ids"] == state["brand_search_draft_ids"]
        rows = {
            row["imported_product_id"]: row
            for group in editor_data["groups"]
            for row in group["rows"]
        }
        ambiguous_rows = [
            rows[source_id] for source_id in fixture_data["ambiguous_source_ids"]
        ]
        unique_row = rows[fixture_data["unique_source_id"]]
        def field(row, external_id):
            return next(
                item for item in row["attributes"]
                if item["external_id"] == external_id
            )
        assert all(not field(row, "31")["value"] for row in ambiguous_rows)
        assert not any(
            attribute["external_id"] == "31"
            for attribute in unique_row["attributes"]
        )

        unique_page = context.new_page()
        unique_page.set_default_timeout(20000)
        unique_page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
        unique_path = f"/marketplaces/drafts/{state['brand_search_unique_draft_id']}"
        with unique_page.expect_response(lambda response: (
            response.request.method == "GET"
            and urlsplit(response.url).path == unique_path + "/editor"
        )) as unique_editor_response_info:
            unique_page.goto(base + unique_path)
        unique_editor_response = unique_editor_response_info.value
        assert unique_editor_response.status == 200
        unique_editor_data = unique_editor_response.json()
        assert unique_editor_data["draft"]["id"] == state["brand_search_unique_draft_id"]
        unique_brand_document = next(
            row for row in unique_editor_data["documents"]["attributes"]
            if row.get("attribute_id") == "31"
        )
        assert unique_brand_document["values"] == [{
            "dictionary_value_id": "11003",
            "value": fixture_data["unique_brand"],
        }]
        normal_editor = unique_page.locator("#ozon-draft-editor:not([v-cloak])")
        normal_editor.wait_for()
        normal_editor.locator(".ode-tabs").get_by_role(
            "button", name="Характеристики", exact=True,
        ).click()
        unique_brand_field = normal_editor.locator(
            '.ode-attribute[data-attribute="31"]'
        )
        unique_brand_field.wait_for()
        assert unique_brand_field.locator(
            ".ode-value-chip-label"
        ).inner_text() == fixture_data["unique_brand"]
        unique_page.close()
        unique_page = None

        ambiguous_ids = state["brand_search_ambiguous_draft_ids"]
        for draft_id in ambiguous_ids:
            editor.locator(f'.orb-row[data-draft="{draft_id}"] input[type="checkbox"]').check()
        page.get_by_role("button", name="Заполнить поле", exact=True).click()
        bulk_dialog = page.get_by_role("dialog")
        bulk_dialog.wait_for(state="visible")
        bulk_dialog.locator("#orb-bulk-field").select_option("attribute:31")
        brand_field = bulk_dialog.locator('[data-attribute="31"]')
        brand_field.get_by_role(
            "button", name="Выбрать из справочника", exact=True,
        ).click()
        search = bulk_dialog.locator(
            ".orb-inline-picker input[type=search]"
        )
        with page.expect_response(lambda response: (
            response.request.method == "GET"
            and f"/repair/dictionaries/{ambiguous_ids[0]}/31" in urlsplit(response.url).path
        )) as brand_search_response_info:
            search.fill(fixture_data["ambiguous_brand"])
        brand_search_response = brand_search_response_info.value
        assert brand_search_response.status == 200
        brand_items = brand_search_response.json()["items"]
        assert {
            (item["id"], item["value"]) for item in brand_items
        } == {
            ("11001", fixture_data["ambiguous_brand"]),
            ("11002", "R101 Ambiguous Brand"),
        }, brand_items
        assert len(search_payloads) == 2
        picker_buttons = bulk_dialog.locator(".orb-picker-results button")
        assert picker_buttons.count() == 2
        picker_labels = bulk_dialog.locator(
            ".orb-picker-results button strong"
        ).all_text_contents()
        exact_label = bulk_dialog.get_by_text(
            fixture_data["ambiguous_brand"], exact=True,
        )
        exact_label.wait_for()
        assert exact_label.count() == 1, picker_labels
        exact_choice = exact_label.locator("xpath=ancestor::button[1]")
        assert exact_choice.count() == 1, picker_labels
        exact_choice.click()

        bulk_dialog.get_by_role(
            "button", name="Посмотреть изменения", exact=True,
        ).click()
        preview = bulk_dialog.locator("[data-bulk-preview-summary]")
        preview.wait_for()
        assert "Будет заполнено: 2" in preview.inner_text()
        assert bulk_dialog.locator(".orb-review-list li").count() == 2
        assert all(title in bulk_dialog.inner_text() for title in fixture_data["titles"][:2])
        assert fixture_data["ambiguous_brand"] in bulk_dialog.inner_text()
        bulk_dialog.get_by_role("button", name="Заполнить в форме", exact=True).click()

        for draft_id in ambiguous_ids:
            row = editor.locator(f'.orb-row[data-draft="{draft_id}"]')
            if not row.locator('[data-attribute="31"]').count():
                row.get_by_role("button", name="Изменить", exact=True).click()
            assert row.locator(
                '[data-attribute="31"] .ode-value-chip-label'
            ).inner_text() == fixture_data["ambiguous_brand"]

        editor.locator(".orb-toolbar").get_by_role(
            "button", name="Сохранить черновики", exact=True,
        ).click()
        save_dialog = page.get_by_role("dialog")
        save_dialog.wait_for(state="visible")
        assert "Будут сохранены в Seller Hub 2 черновиков" in save_dialog.inner_text()
        assert "Отправка в Ozon потребует отдельной проверки версий" in save_dialog.inner_text()
        assert save_dialog.locator(".orb-review-list li").count() == 2
        with page.expect_response(lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path.endswith("/repair/apply")
        )) as save_response_info:
            save_dialog.get_by_role(
                "button", name="Сохранить черновики", exact=True,
            ).click()
        save_response = save_response_info.value
        assert save_response.status == 200
        assert state["brand_search_repair_post"]["draft_ids"] == ambiguous_ids
        receipt = editor.locator(".orb-receipt[data-source-prepare-receipt]")
        receipt.wait_for()
        assert "Сохранено в Seller Hub. Перед отправкой проверьте актуальные версии." in (
            receipt.inner_text()
        )

        with app.app_context():
            for draft_id in ambiguous_ids:
                draft = db.session.get(MarketplaceProductDraft, draft_id)
                brand_attr = next(
                    item for item in json.loads(draft.attributes_json or "[]")
                    if item.get("attribute_id") == "31"
                )
                assert brand_attr["values"] == [{
                    "dictionary_value_id": "11001",
                    "value": fixture_data["ambiguous_brand"],
                }]
            unique_draft = db.session.get(
                MarketplaceProductDraft, state["brand_search_unique_draft_id"],
            )
            unique_brand_attr = next(
                item for item in json.loads(unique_draft.attributes_json or "[]")
                if item.get("attribute_id") == "31"
            )
            assert unique_brand_attr["values"] == [{
                "dictionary_value_id": "11003",
                "value": fixture_data["unique_brand"],
            }]

        review_link = receipt.locator("a[data-source-prepare-review]")
        review_link.wait_for()
        review_parts = urlsplit(review_link.get_attribute("href"))
        review_query = parse_qs(review_parts.query)
        assert review_parts.path == "/marketplaces/ozon/uploads/review"
        assert review_query.get("account_id") == [str(account_id)]
        assert review_query.get("parent_prepare_job_uid") == [state["brand_search_job_uid"]]
        assert [
            int(value) for value in review_query["draft_ids"][0].split(",")
        ] == state["brand_search_draft_ids"]
        review_link.click()
        page.wait_for_url("**/marketplaces/ozon/uploads/review?*")
        review_page = page.locator("#ozon-upload-review:not([v-cloak])")
        review_page.wait_for()
        with page.expect_response(lambda response: (
            response.request.method == "GET"
            and urlsplit(response.url).path == "/marketplaces/ozon/uploads/api/review"
            and parse_qs(urlsplit(response.url).query).get("parent_prepare_job_uid")
            == [state["brand_search_job_uid"]]
        )) as safe_review_info:
            review_page.get_by_role(
                "button", name="Проверить все готовые", exact=True,
            ).click()
        safe_review = safe_review_info.value
        assert safe_review.status == 200
        reviewed = safe_review.json()["review"]
        assert reviewed["draft_ids"] == state["brand_search_draft_ids"]
        assert all(item["selectable"] for item in reviewed["items"])
        send_dialog = page.get_by_role("dialog")
        send_dialog.wait_for(state="visible")
        assert send_dialog.locator(".our-dialog-list li").count() == 3
        assert not send_dialog.locator(".our-confirm input").is_checked()
        assert send_dialog.get_by_role(
            "button", name="Отправить проверенные карточки", exact=True,
        ).is_disabled()
        send_dialog.get_by_role("button", name="Вернуться", exact=True).click()
        assert state["brand_search_review_api_pages"] == [1]
        assert _business_counts()["operations"] == state["brand_search_operations_before"]
        assert report["provider_dispatch_calls"] == dispatch_marker
        assert report["post_counts"].get(
            "/marketplaces/ozon/uploads/from-drafts", 0,
        ) == publication_post_marker
        report["brand_search"].update({
            "source_ids": fixture_data["source_ids"],
            "draft_ids": state["brand_search_draft_ids"],
            "ambiguous_draft_ids": ambiguous_ids,
            "unique_brand_auto_staged": True,
            "ambiguous_provider_choices": [
                {"id": value_id, "value": value}
                for value_id, value in sorted({
                    (item["id"], item["value"]) for item in brand_items
                })
            ],
            "same_scope_search_calls": 1,
            "unique_scope_search_calls": 1,
            "bulk_preview_applied_rows": 2,
            "locally_saved_rows": state["brand_search_repair_post"]["draft_ids"],
            "review_draft_ids": reviewed["draft_ids"],
            "review_api_pages": state["brand_search_review_api_pages"],
            "publication_confirmation_left_unchecked": True,
            "publication_post_unchanged": True,
            "operation_count_unchanged": True,
            "provider_dispatch_unchanged": True,
        })
        passed("source_prepare_brand_search_bulk_choices_saved_locally_and_reviewed_without_publication")
    except BaseException:
        state["brand_search_case_active"] = False
        for open_page in (unique_page, page):
            if open_page is None:
                continue
            try:
                open_page.close()
            except BaseException:
                pass
        raise
    state["brand_search_case_active"] = False
    if unique_page is not None:
        unique_page.close()
    page.close()


def passed(name):
    assert not report["js_errors"], report["js_errors"]
    assert not report["unexpected_external"], report["unexpected_external"]
    assert report["provider_attempts"] == 0
    report["checks"].append(name)
    print(json.dumps({"browser_check": name}), flush=True)


def run_recovery_browser_journey(page, browser, context):
    """Exercise exact, read-only reviewed-result recovery end to end."""
    source_job_uid = _seed_recovery_source_run()
    page.set_viewport_size({"width": 1440, "height": 980})
    page.goto(f"{base}/marketplaces/ozon/uploads/{source_job_uid}")
    source_review_link = page.locator("a[data-source-prepare-review]")
    source_review_link.wait_for()
    source_review_href = source_review_link.get_attribute("href")
    source_review_query = parse_qs(urlsplit(source_review_href).query)
    assert urlsplit(source_review_href).path == "/marketplaces/ozon/uploads/review"
    assert source_review_query.get("account_id") == [str(account_id)]
    assert source_review_query.get("parent_prepare_job_uid") == [source_job_uid]
    assert [
        int(value) for value in source_review_query["draft_ids"][0].split(",")
    ] == state["recovery_source_draft_ids"]

    page.goto(base + source_review_href)
    page.locator("#ozon-upload-review:not([v-cloak])").wait_for()
    prepared_review = page.evaluate(
        "() => JSON.parse(document.querySelector('#our-bootstrap').textContent).review"
    )
    assert prepared_review["account_id"] == account_id
    assert prepared_review["parent_prepare_job_uid"] == source_job_uid
    assert prepared_review["draft_ids"] == state["recovery_source_draft_ids"]
    assert len(prepared_review["items"]) == 5
    assert all(item["selectable"] for item in prepared_review["items"])
    assert all(item["version"] > 0 for item in prepared_review["items"])
    state["recovery_review_versions"] = {
        item["draft_id"]: item["version"] for item in prepared_review["items"]
    }
    all_ready = page.get_by_role(
        "button", name="Проверить все готовые", exact=True,
    )
    all_ready.click()
    first_confirmation = page.get_by_role("dialog")
    first_confirmation.wait_for(state="visible")
    assert first_confirmation.locator(".our-dialog-list li").count() == 5
    assert "Готовы к отправке: 5 из 5 проверенных." in (
        first_confirmation.locator(".our-dialog-summary").inner_text()
    )
    first_confirm_checkbox = first_confirmation.locator(".our-confirm input")
    first_confirm_checkbox.check()
    with page.expect_response(lambda response: (
        response.request.method == "POST"
        and urlsplit(response.url).path == "/marketplaces/ozon/uploads/from-drafts"
    )) as initial_recovery_queue_response_info:
        first_confirmation.get_by_role(
            "button", name="Отправить проверенные карточки", exact=True,
        ).click()
    initial_recovery_queue_response = initial_recovery_queue_response_info.value
    assert initial_recovery_queue_response.status == 202
    assert state["recovery_initial_post"]["draft_ids"]
    assert state["recovery_initial_worker_stats"]["enqueued"] == 4
    assert state["recovery_initial_operation_statuses"] == {
        "rejected": "failed",
        "success": "succeeded",
        "noop": "succeeded",
        "uncertain": "uncertain",
    }
    assert report["provider_fake_status_reads"] == 1
    assert state["recovery_edited_version"] > state["recovery_review_versions"][
        state["recovery_draft_by_kind"]["stale"]
    ]
    page.wait_for_url(f"**/marketplaces/ozon/uploads/{state['recovery_initial_result_uid']}")

    result_uid = state["recovery_initial_result_uid"]
    expected_ids = state["recovery_expected_ids"]
    excluded_ids = state["recovery_excluded_ids"]
    result_write_marker = len(business_write_events)
    result_counts = _business_counts()
    page.set_viewport_size({"width": 320, "height": 780})
    recovery_cta = page.locator("a[data-reviewed-result-recovery]")
    recovery_cta.wait_for()
    assert recovery_cta.inner_text().strip() == "Проверить оставшиеся карточки"
    assert recovery_cta.get_attribute("aria-label") == (
        "Проверить оставшиеся карточки (2)"
    )
    recovery_href = recovery_cta.get_attribute("href")
    recovery_parts = urlsplit(recovery_href)
    recovery_query = parse_qs(recovery_parts.query)
    assert recovery_parts.path == "/marketplaces/ozon/uploads/review"
    assert recovery_query.get("account_id") == [str(account_id)]
    assert recovery_query.get("result_job_uid") == [result_uid]
    assert [int(value) for value in recovery_query["draft_ids"][0].split(",")] == expected_ids
    assert set(expected_ids).isdisjoint(excluded_ids)
    assert len(expected_ids) == 2 and len(excluded_ids) == 3
    assert "Без изменений" in page.locator(".max-w-7xl").inner_text()
    assert "Требуют внимания" in page.locator(".max-w-7xl").inner_text()
    summary_bounds = page.locator("[data-reviewed-result-summary]").bounding_box()
    guide_bounds = page.locator("[data-reviewed-result-guide]").bounding_box()
    assert summary_bounds and 0 <= summary_bounds["y"] < 780, summary_bounds
    assert guide_bounds and guide_bounds["y"] > summary_bounds["y"] + summary_bounds["height"], guide_bounds
    result_bounds = recovery_cta.bounding_box()
    assert result_bounds and result_bounds["height"] >= 44 and result_bounds["width"] >= 44, result_bounds
    assert result_bounds["x"] >= 0 and result_bounds["x"] + result_bounds["width"] <= 320
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    page.screenshot(path=OUT / "ozon-reviewed-result-recovery-320.png", animations="disabled")
    assert len(business_write_events) == result_write_marker, business_write_events[result_write_marker:]
    assert _business_counts() == result_counts

    # Enter activates the exact CTA on the narrow result page.
    recovery_cta.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    assert recovery_cta.evaluate("""link => document.activeElement === link &&
        getComputedStyle(link).outlineStyle !== 'none' &&
        parseFloat(getComputedStyle(link).outlineWidth) >= 2""")
    page.keyboard.press("Enter")
    page.wait_for_url("**/marketplaces/ozon/uploads/review?*")
    page.locator("#ozon-upload-review:not([v-cloak])").wait_for()
    result_review = page.evaluate(
        "() => JSON.parse(document.querySelector('#our-bootstrap').textContent).review"
    )
    assert result_review["account_id"] == account_id
    assert result_review["result_job_uid"] == result_uid
    assert not result_review.get("parent_prepare_job_uid")
    assert result_review["draft_ids"] == expected_ids
    assert len(result_review["items"]) == 2
    recovery_rows = {item["draft_id"]: item for item in result_review["items"]}
    stale_id = state["recovery_draft_by_kind"]["stale"]
    rejected_id = state["recovery_draft_by_kind"]["rejected"]
    assert set(recovery_rows) == {stale_id, rejected_id}
    assert recovery_rows[stale_id]["version"] == state["recovery_edited_version"]
    assert recovery_rows[rejected_id]["version"] == state["recovery_review_versions"][rejected_id]
    assert all(row["selectable"] for row in recovery_rows.values())
    state["recovery_current_review_versions"] = {
        draft_id: row["version"] for draft_id, row in recovery_rows.items()
    }
    back_link = page.get_by_role("link", name="К результату отправки", exact=True)
    back_link.wait_for()
    assert urlsplit(back_link.get_attribute("href")).path == (
        f"/marketplaces/ozon/uploads/{result_uid}"
    )
    review_context_text = page.locator(".our-context").inner_text()
    assert "Ozon CI 0" in review_context_text
    review_write_marker = len(business_write_events)
    review_counts = _business_counts()

    negative_cases = [
        {
            "name": "wrong_account",
            "account_id": fixture["foreign_account_id"],
            "draft_ids": expected_ids,
        },
        {
            "name": "wrong_owner",
            "account_id": account_id,
            "draft_ids": [stale_id, state["foreign_draft_id"]],
        },
        {
            "name": "extra_success",
            "account_id": account_id,
            "draft_ids": [stale_id, rejected_id, state["recovery_draft_by_kind"]["success"]],
        },
        {
            "name": "missing_current_row",
            "account_id": account_id,
            "draft_ids": [stale_id, 9007199254740991],
        },
        {
            "name": "mixed_unsafe_statuses",
            "account_id": account_id,
            "draft_ids": [*expected_ids, *excluded_ids],
        },
        {
            "name": "no_eligible_rows",
            "account_id": account_id,
            "draft_ids": [state["recovery_draft_by_kind"]["success"]],
        },
    ]
    state["recovery_negative_probe_active"] = True
    negative_responses = page.evaluate(
        """async cases => Promise.all(cases.map(async item => {
            const query = new URLSearchParams({
                account_id: String(item.account_id),
                draft_ids: item.draft_ids.join(','),
                result_job_uid: item.result_job_uid,
            });
            const response = await fetch('/marketplaces/ozon/uploads/api/review?' + query, {
                credentials: 'same-origin',
                headers: {Accept: 'application/json'},
            });
            return {name: item.name, status: response.status,
                    body: await response.json()};
        }))""",
        [dict(case, result_job_uid=result_uid) for case in negative_cases],
    )
    state["recovery_negative_probe_active"] = False
    assert {item["name"] for item in negative_responses} == {
        item["name"] for item in negative_cases
    }
    assert all(item["status"] in {404, 409} for item in negative_responses), negative_responses
    assert all(item["body"].get("success") is False for item in negative_responses)
    assert len(business_write_events) == review_write_marker, business_write_events[review_write_marker:]
    assert _business_counts() == review_counts

    # JavaScript-disabled rendering uses the same current account/result and
    # exact draft versions for the classic form.
    classic_context = browser.new_context(
        java_script_enabled=False,
        viewport={"width": 320, "height": 780},
        service_workers="block",
        timezone_id="Europe/Moscow",
        storage_state=context.storage_state(),
    )
    classic_context.route("**/*", bridge)
    classic_page = classic_context.new_page()
    classic_page.set_default_timeout(20000)
    classic_page.goto(base + recovery_href)
    classic_page.locator(".our-noscript").wait_for()
    classic_bootstrap = json.loads(
        classic_page.locator("#our-bootstrap").inner_text()
    )["review"]
    assert classic_bootstrap["result_job_uid"] == result_uid
    assert classic_bootstrap["account_id"] == account_id
    assert [item["draft_id"] for item in classic_bootstrap["items"]] == expected_ids
    assert {
        int(key): value for key, value in classic_bootstrap["expected_versions"].items()
    } == {
        stale_id: state["recovery_edited_version"],
        rejected_id: state["recovery_review_versions"][rejected_id],
    }
    assert "Ozon CI 0" in classic_page.locator(".our-context").inner_text()
    classic_back = classic_page.get_by_role("link", name="К результату отправки", exact=True)
    assert urlsplit(classic_back.get_attribute("href")).path == (
        f"/marketplaces/ozon/uploads/{result_uid}"
    )
    assert classic_page.locator('input[name="result_job_uid"]').get_attribute("value") == result_uid
    assert "BR Recovery · изменённая версия" in classic_page.locator(".our-noscript").inner_text()
    assert "BR Recovery · окончательный отказ Ozon" in classic_page.locator(".our-noscript").inner_text()
    assert classic_page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    assert len(business_write_events) == review_write_marker, business_write_events[review_write_marker:]
    classic_context.close()

    # The seller checks the exact two currently recoverable rows and confirms
    # once. The worker queues only this subset and no provider write runs.
    batch_review = page.get_by_role(
        "button", name="Проверить все готовые", exact=True,
    )
    batch_review.click()
    final_dialog = page.get_by_role("dialog")
    final_dialog.wait_for(state="visible")
    assert final_dialog.locator(".our-dialog-list li").count() == 2
    final_copy = final_dialog.inner_text()
    assert state["recovery_titles"]["stale"] in final_copy
    assert state["recovery_titles"]["rejected"] in final_copy
    confirm = final_dialog.locator(".our-confirm input")
    confirm.check()
    send = final_dialog.get_by_role(
        "button", name="Отправить проверенные карточки", exact=True,
    )
    assert send.is_enabled()
    state["recovery_confirmation_pending"] = True
    with page.expect_response(lambda response: (
        response.request.method == "POST"
        and urlsplit(response.url).path == "/marketplaces/ozon/uploads/from-drafts"
    )) as final_response_info:
        send.click()
    final_response = final_response_info.value
    assert final_response.status == 202
    assert set(state["recovery_final_post"]["draft_ids"]) == set(expected_ids)
    assert state["recovery_final_post"]["result_job_uid"] == result_uid
    assert state["recovery_final_post"]["expected_versions"] == {
        str(draft_id): version
        for draft_id, version in state["recovery_current_review_versions"].items()
    }
    assert state["recovery_final_worker_stats"]["enqueued"] == 2
    assert len(state["recovery_final_operation_ids"]) == 2
    assert state["recovery_final_operation_statuses"] == ["queued", "queued"]
    assert report["provider_dispatch_calls"] == 0
    assert report["provider_attempts"] == 0
    assert report["provider_fake_status_reads"] == 1
    report["reviewed_result_recovery"].update({
        "source_result_job_uid": result_uid,
        "source_draft_ids": state["recovery_source_draft_ids"],
        "result_recovery_draft_ids": expected_ids,
        "excluded_success_noop_uncertain_ids": excluded_ids,
        "cta_query_exact": True,
        "vue_review_exact": True,
        "classic_review_exact": True,
        "stale_review_version": state["recovery_review_versions"][stale_id],
        "current_version_after_edit": state["recovery_edited_version"],
        "fake_task_status_reads": report["provider_fake_status_reads"],
        "negative_scope_responses": [
            {"name": item["name"], "status": item["status"]}
            for item in negative_responses
        ],
        "final_accepted_draft_ids": state["recovery_final_post"]["draft_ids"],
        "final_enqueued_operation_ids": state["recovery_final_operation_ids"],
        "final_enqueued_operation_statuses": state["recovery_final_operation_statuses"],
        "read_only_result_and_review_gets": True,
        "narrow_keyboard_cta_activation": True,
        "classic_mobile_no_horizontal_overflow": True,
        "result_cta_bounds_320": result_bounds,
        "result_summary_before_workflow_guide": True,
    })
    passed("reviewed_result_recovery_returns_exact_current_rows_read_only_and_queues_only_confirmed_subset")


def _selected_draft_snapshots():
    with app.app_context():
        drafts = {
            draft.id: (draft.version, draft.content_json)
            for draft in MarketplaceProductDraft.query.order_by(MarketplaceProductDraft.id)
        }
        items = {
            item.id: (item.draft_id, item.prepared_version, item.phase, item.error_code)
            for item in OzonBulkUploadItem.query.order_by(OzonBulkUploadItem.id)
        }
        return drafts, items


def run_selected_draft_journey(page):
    """Save one description to a standalone draft and an earlier prepared draft."""
    standalone_title = "BR Selection · отдельный черновик"
    description = "Локальное описание двух выбранных черновиков Ozon."
    with app.app_context():
        product_type = MarketplaceProductType.query.filter_by(
            marketplace_id=marketplace_id, external_type_id="1000",
        ).one()
        source = _make_source(
            seller_id=fixture["seller_id"], external_id="br-standalone-draft-source",
            vendor_code="BR-STANDALONE-DRAFT", title=standalone_title,
            description="Исходное описание отдельной карточки.", category=product_type.category.name,
        )
        db.session.add(source)
        db.session.flush()
        standalone = MarketplaceDraftService.create_draft(
            seller_id=fixture["seller_id"], account_id=account_id,
            imported_product_id=source.id, product_type_id=product_type.id,
        )
        standalone = MarketplaceDraftService.validate_draft(
            seller_id=fixture["seller_id"], draft_id=standalone.id,
            expected_version=standalone.version,
        )
        assert standalone.status == "ready", standalone.validation_result_json
        standalone_id = standalone.id
        prepared = MarketplaceProductDraft.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id,
            imported_product_id=source_ids["excluded"],
        ).one()
        prepared_id = prepared.id
        prepared_title = source_titles["excluded"]
        assert prepared.status == "ready"
        assert OzonBulkUploadItem.query.filter_by(draft_id=prepared_id).count() == 1
        assert OzonBulkUploadItem.query.filter_by(draft_id=standalone_id).count() == 0

    baseline_drafts, baseline_items = _selected_draft_snapshots()
    baseline_counts = _business_counts()
    posts_before = dict(report["post_counts"])
    writes_before = len(business_write_events)
    page_path = "/marketplaces/drafts/"
    page.set_viewport_size({"width": 1440, "height": 980})
    page.goto(f"{base}{page_path}?account_id={account_id}&per_page=1&status=ready")
    list_rows = page.locator("#ozon-drafts-list:not([v-cloak]) .odl-row")
    list_rows.first.wait_for()
    page.locator("[data-draft-selection-mode]").press("Enter")
    picked = []
    expected_titles = {standalone_title: standalone_id, prepared_title: prepared_id}
    pagination = page.get_by_role("navigation", name="Страницы черновиков")
    for _ in range(baseline_counts["drafts"]):
        row = list_rows.first
        title = row.locator("h3").inner_text()
        if title in expected_titles:
            pick = row.locator("[data-draft-selection-check]")
            if not picked:
                page.get_by_role("button", name="Выбрать страницу (1)", exact=True).click()
                assert pick.is_checked()
            else:
                pick.check()
            picked.append(expected_titles[title])
        if len(picked) == 2:
            break
        pagination.get_by_role("link", name="Дальше →", exact=True).click()
        list_rows.first.wait_for()
        assert list_rows.count() == 1
        assert "per_page=1" in page.url

    assert set(picked) == {standalone_id, prepared_id}, picked
    state["selection_draft_ids"] = picked
    origin_query = parse_qs(urlsplit(page.url).query)
    assert int(origin_query["page"][0]) > 1
    start = page.locator("[data-draft-selection-start]")
    assert "(2)" in start.inner_text()
    page.screenshot(path=str(OUT / "ozon-saved-draft-selection-list.png"), animations="disabled")
    page.set_viewport_size({"width": 320, "height": 860})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    start.scroll_into_view_if_needed()
    button_bounds = start.bounding_box()
    assert button_bounds and 0 <= button_bounds["x"] and button_bounds["x"] + button_bounds["width"] <= 321
    assert button_bounds["height"] >= 44 and button_bounds["width"] >= 44
    row_link_text_fits = list_rows.first.locator(".odl-row-action .ode-link").evaluate("""link => {
        const limits = link.closest('.odl-row-action').getBoundingClientRect();
        const text = document.createRange();
        text.selectNodeContents(link);
        const lines = [...text.getClientRects()];
        return lines.length > 0 && lines.every(line =>
            line.left >= limits.left - 1 && line.right <= limits.right + 1);
    }""")
    assert row_link_text_fits
    page.screenshot(path=str(OUT / "ozon-saved-draft-selection-list-mobile.png"), animations="disabled")
    start.press("Enter")
    editor = page.locator("[data-draft-selection-editor]:not([v-cloak])")
    editor.wait_for()
    page.set_viewport_size({"width": 1440, "height": 980})
    editor_path = urlsplit(page.url)
    assert editor_path.path == "/marketplaces/ozon/uploads/selection/repair"
    query = parse_qs(editor_path.query)
    assert query["draft_ids"] == [",".join(map(str, picked))]
    assert query["account_id"] == [str(account_id)]
    assert query["return_page"] == origin_query["page"]
    assert query["return_per_page"] == ["1"]
    assert query["return_status"] == ["ready"]
    assert {int(row.get_attribute("data-draft")) for row in editor.locator(".orb-row").all()} == set(picked)

    for draft_id in picked:
        assert editor.locator(f'.orb-row[data-draft="{draft_id}"] input[type=checkbox]').first.is_checked()
    editor.get_by_role("button", name="Заполнить поле", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Поле", exact=True).select_option("description")
    dialog.get_by_label("Новое значение", exact=True).fill(description)
    dialog.get_by_role("button", name="Посмотреть изменения", exact=True).click()
    fill_form = dialog.get_by_role("button", name="Заполнить в форме", exact=True)
    fill_form.wait_for()
    assert dialog.inner_text().count(description) == 2
    fill_form.click()

    editor.locator("footer [data-draft-selection-save]").click()
    assert dialog.inner_text().count(description) == 2
    dialog.get_by_role("button", name="Вернуться к форме", exact=True).click()
    assert report["post_counts"] == posts_before
    assert len(business_write_events) == writes_before
    assert _selected_draft_snapshots() == (baseline_drafts, baseline_items)
    passed("saved_draft_selection_preserved_across_pages_and_preview_remains_local")

    editor.locator("footer [data-draft-selection-save]").click()
    dialog.locator("[data-draft-selection-save]").click()
    receipt = editor.get_by_role("region", name="Результат сохранения")
    receipt.wait_for()
    page.wait_for_function("document.activeElement?.matches('[data-draft-selection-receipt]')")
    path = "/marketplaces/ozon/uploads/selection/repair/apply"
    assert report["post_counts"] == {**posts_before, path: 1}
    assert set(state["selection_repair_post"]["draft_ids"]) == set(picked)
    assert state["selection_repair_post"]["csrf"] is True
    drafts_after, items_after = _selected_draft_snapshots()
    assert set(drafts_after) == set(baseline_drafts)
    assert _business_counts() == baseline_counts
    assert items_after == baseline_items
    assert {key for key, value in drafts_after.items() if value != baseline_drafts[key]} == set(picked)
    for draft_id in picked:
        version, content = drafts_after[draft_id]
        assert version > baseline_drafts[draft_id][0]
        assert json.loads(content)["description"] == description

    review_link = receipt.get_by_role("link", name="Проверить перед отправкой")
    review_query = parse_qs(urlsplit(review_link.get_attribute("href")).query)
    assert not review_query.get("parent_prepare_job_uid")
    assert set(map(int, review_query["draft_ids"][0].split(","))) == set(picked)
    assert review_query["account_id"] == [str(account_id)]
    list_return = editor.locator("header [data-draft-selection-return]")
    return_query = parse_qs(urlsplit(list_return.get_attribute("href")).query)
    assert return_query == origin_query

    layouts = []
    for theme in ("light", "dark"):
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        for width in (320, 390):
            page.set_viewport_size({"width": width, "height": 860})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            review_link.scroll_into_view_if_needed()
            bounds = review_link.bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
            layouts.append({"width": width, "theme": theme})
        page.screenshot(path=str(OUT / f"ozon-saved-draft-edit-{theme}-mobile.png"), animations="disabled")
    page.set_viewport_size({"width": 1440, "height": 980})
    review_link.click()
    review_page = page.locator("#ozon-upload-review:not([v-cloak])")
    review_page.wait_for()
    with page.expect_response(
        lambda response: urlsplit(response.url).path == "/marketplaces/ozon/uploads/api/review"
    ) as pending_review:
        review_page.get_by_role("button", name="Проверить все готовые", exact=True).click()
    assert pending_review.value.status == 200
    reviewed = pending_review.value.json()["review"]
    assert {row["draft_id"]: row["version"] for row in reviewed["items"]} == {
        draft_id: drafts_after[draft_id][0] for draft_id in picked
    }
    assert state["selection_review_read"] is True
    page.go_back()
    editor.wait_for()
    list_return.click()
    list_rows.first.wait_for()
    assert urlsplit(page.url).path == page_path
    assert parse_qs(urlsplit(page.url).query) == origin_query
    assert "(2)" in page.locator("[data-draft-selection-start]").inner_text()
    assert report["post_counts"] == {**posts_before, path: 1}
    assert _selected_draft_snapshots() == (drafts_after, items_after)
    assert _business_counts() == baseline_counts
    report["saved_draft_selection"] = {
        "pages_kept": True, "selected_count": len(picked), "saved_count": len(picked),
        "origin_run_count": 1, "standalone_draft_count": 1, "confirm_posts": 1,
        "journal_unchanged": True, "exact_fresh_review_read": True, "published": 0,
        "layouts": layouts, "focus_after_save": True, "mobile_list_cta_height": button_bounds["height"],
        "mobile_list_action_text_fits": row_link_text_fits,
    }
    passed("saved_draft_selection_edits_exact_scope_and_reviews_current_versions_without_queueing")


def _seed_mixed_type_selection():
    """Create a small-per-card, wide-union schema for the native editor."""
    type_count = 22
    fields_per_type = 5
    target_attribute_id = "99990001"
    target_attribute_name = "R49 целевое десятичное поле"
    unselected_attribute_id = "99990002"
    unselected_attribute_name = "R49 поле только вне набора"
    with app.app_context():
        now = datetime.utcnow()
        category = MarketplaceTaxonomyCategory.query.filter_by(
            marketplace_id=marketplace_id,
            external_category_id="100",
        ).one()
        draft_ids = []
        titles = {}
        target_ids = set()
        unselected_id = None
        unselected_title = "R49 mixed · неподходящий тип вне выбора"

        def add_type(index, *, include_target=False, unselected=False):
            nonlocal unselected_id
            product_type = MarketplaceProductType(
                marketplace_id=marketplace_id,
                category_id=category.id,
                external_type_id=str(710000 + index),
                name=f"R49 смешанный тип {index:02d}",
                is_available=True,
                is_enabled=True,
                is_seller_selectable=True,
                attributes_synced_at=now,
                attributes_sync_status="success",
                attributes_schema_hash=f"r49-mixed-schema-{index:02d}",
            )
            db.session.add(product_type)
            db.session.flush()
            definitions = [MarketplaceAttributeDefinition(
                marketplace_id=marketplace_id,
                product_type_id=product_type.id,
                external_attribute_id="4191",
                name="Аннотация",
                data_type="String",
                is_required=False,
                max_value_count=1,
                is_available=True,
                is_enabled=True,
                last_seen_at=now,
            )]
            for field_index in range(fields_per_type):
                external_id = str(600000 + index * 10 + field_index)
                definitions.append(MarketplaceAttributeDefinition(
                    marketplace_id=marketplace_id,
                    product_type_id=product_type.id,
                    external_attribute_id=external_id,
                    name=f"R49 поле {index:02d}-{field_index:02d}",
                    data_type="String",
                    is_required=True,
                    max_value_count=1,
                    is_available=True,
                    is_enabled=True,
                    last_seen_at=now,
                ))
            if include_target:
                definitions.append(MarketplaceAttributeDefinition(
                    marketplace_id=marketplace_id,
                    product_type_id=product_type.id,
                    external_attribute_id=target_attribute_id,
                    name=target_attribute_name,
                    data_type="Decimal",
                    is_required=True,
                    max_value_count=1,
                    is_available=True,
                    is_enabled=True,
                    last_seen_at=now,
                ))
            if unselected:
                definitions.append(MarketplaceAttributeDefinition(
                    marketplace_id=marketplace_id,
                    product_type_id=product_type.id,
                    external_attribute_id=unselected_attribute_id,
                    name=unselected_attribute_name,
                    data_type="String",
                    is_required=True,
                    max_value_count=1,
                    is_available=True,
                    is_enabled=True,
                    last_seen_at=now,
                ))
            db.session.add_all(definitions)
            product_type.attributes_count = len(definitions)
            product_type.required_attributes_count = sum(
                1 for definition in definitions if definition.is_required
            )
            source = _make_source(
                seller_id=fixture["seller_id"],
                external_id=f"r49-mixed-source-{index:02d}",
                vendor_code=f"R49-MIXED-{index:02d}",
                title=(
                    unselected_title
                    if unselected
                    else f"R49 mixed · выбранный товар {index:02d}"
                ),
                description=f"Синтетическое описание R49 {index:02d}.",
                category=category.name,
                barcode=f"460000009{index:04d}",
            )
            db.session.add(source)
            db.session.flush()
            draft = MarketplaceDraftService.create_draft(
                seller_id=fixture["seller_id"],
                account_id=account_id,
                imported_product_id=source.id,
                product_type_id=product_type.id,
                source_link_preflight=False,
                observed_mapping_preflight=False,
            )
            if unselected:
                unselected_id = draft.id
            else:
                draft_ids.append(draft.id)
                titles[draft.id] = source.title
                if include_target:
                    target_ids.add(draft.id)

        for index in range(type_count):
            add_type(index, include_target=index in {0, 1})
        add_type(type_count, unselected=True)
        db.session.commit()
        assert len(draft_ids) == type_count
        assert len(target_ids) == 2
        return {
            "draft_ids": draft_ids,
            "titles": titles,
            "target_attribute_id": target_attribute_id,
            "target_attribute_name": target_attribute_name,
            "target_draft_ids": sorted(target_ids),
            "unselected_attribute_id": unselected_attribute_id,
            "unselected_attribute_name": unselected_attribute_name,
            "unselected_draft_id": unselected_id,
            "unselected_title": unselected_title,
            "type_count": type_count,
            "fields_per_type": fields_per_type,
        }


def _mixed_draft_snapshots(draft_ids):
    with app.app_context():
        return {
            draft.id: {
                "version": draft.version,
                "attributes": json.loads(draft.attributes_json or "[]"),
                "content": json.loads(draft.content_json or "{}"),
                "commercial": json.loads(draft.commercial_json or "{}"),
                "dimensions": json.loads(draft.dimensions_json or "{}"),
            }
            for draft in MarketplaceProductDraft.query.filter(
                MarketplaceProductDraft.id.in_(draft_ids),
            ).all()
        }


def run_mixed_type_selection_journey(page):
    """Open and locally save a >100-field union from ordinary Ozon drafts."""
    fixture_rows = _seed_mixed_type_selection()
    draft_ids = fixture_rows["draft_ids"]
    titles = fixture_rows["titles"]
    selected_ids = list(draft_ids)
    target_ids = set(fixture_rows["target_draft_ids"])
    other_selected_ids = set(selected_ids) - target_ids
    outside_id = fixture_rows["unselected_draft_id"]
    target_field = fixture_rows["target_attribute_id"]
    target_name = fixture_rows["target_attribute_name"]
    outside_field_name = fixture_rows["unselected_attribute_name"]
    outside_title = fixture_rows["unselected_title"]
    new_value = "47.25"

    with app.app_context():
        baseline_counts = _business_counts()
        initial = _mixed_draft_snapshots([*selected_ids, outside_id])
        assert set(initial) == set(selected_ids) | {outside_id}
        assert all(
            not any(
                item.get("attribute_id") == target_field
                for item in initial[draft_id]["attributes"]
            )
            for draft_id in selected_ids
        )
        writes_before = len(business_write_events)
    provider_attempts_before = report["provider_attempts"]
    provider_dispatch_calls_before = report["provider_dispatch_calls"]
    state["selection_draft_ids"] = selected_ids
    state["mixed_selection_draft_ids"] = selected_ids

    page.set_viewport_size({"width": 320, "height": 900})
    page.evaluate("document.documentElement.dataset.theme = 'light'")
    page.goto(
        f"{base}/marketplaces/drafts/?account_id={account_id}&per_page=100"
    )
    workspace = page.locator("#ozon-drafts-list:not([v-cloak])")
    workspace.wait_for()
    for draft_id in [*selected_ids, outside_id]:
        page.get_by_role(
            "checkbox",
            name=f"Выбрать для изменения {titles.get(draft_id, outside_title)}",
            exact=True,
        ).wait_for()

    mode = page.locator("[data-draft-selection-mode]")
    mode.focus()
    mode.press("Enter")
    summary = page.locator("[data-draft-selection-summary]")
    summary.wait_for()
    clear = summary.get_by_role("button", name="Снять выбор", exact=True)
    if clear.is_enabled():
        clear.click()
    for draft_id in selected_ids:
        checkbox = page.get_by_role(
            "checkbox",
            name=f"Выбрать для изменения {titles[draft_id]}",
            exact=True,
        )
        checkbox.focus()
        page.keyboard.press("Space")
    assert f"Для изменения выбрано {len(selected_ids)}" in summary.inner_text()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    start = page.locator("[data-draft-selection-start]")
    start.scroll_into_view_if_needed()
    start_bounds = start.bounding_box()
    assert start_bounds and start_bounds["height"] >= 44
    assert 0 <= start_bounds["x"] and start_bounds["x"] + start_bounds["width"] <= 321
    start.focus()
    start.press("Enter")
    page.wait_for_url("**/marketplaces/ozon/uploads/selection/repair?*")
    selection_query = parse_qs(urlsplit(page.url).query)
    assert selection_query["account_id"] == [str(account_id)]
    assert selection_query["draft_ids"] == [",".join(map(str, selected_ids))]
    assert selection_query["return_per_page"] == ["100"]

    editor = page.locator(
        '#ozon-bulk-repair[data-editor-mode="draft_selection"]:not([v-cloak])'
    )
    editor.wait_for()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    editor_model = page.evaluate(
        "() => JSON.parse(document.querySelector('#orb-bootstrap').textContent).editor"
    )
    assert editor_model["mode"] == "draft_selection"
    assert editor_model["review_draft_ids"] == selected_ids
    rows = [row for group in editor_model["groups"] for row in group["rows"]]
    assert {row["draft_id"] for row in rows} == set(selected_ids)
    editable_ids = {
        field["external_id"]
        for row in rows
        for field in row["attributes"]
        if field["editable"]
    }
    assert len(editable_ids) >= 105, len(editable_ids)
    assert target_field in editable_ids
    assert fixture_rows["unselected_attribute_id"] not in editable_ids
    assert max(sum(field["editable"] for field in row["attributes"]) for row in rows) <= 6
    assert all(
        any(
            field["external_id"] == target_field
            and field["data_type"] == "Decimal"
            and field["editable"]
            for field in row["attributes"]
        )
        for row in rows
        if row["draft_id"] in target_ids
    )
    assert not any(row["draft_id"] == outside_id for row in rows)

    editor.locator(".orb-toolbar").get_by_role(
        "button", name="Заполнить поле", exact=True,
    ).click()
    dialog = page.get_by_role("dialog")
    dialog.wait_for(state="visible")
    search = dialog.locator("#orb-bulk-field-query")
    bulk_field = dialog.locator("#orb-bulk-field")
    window = dialog.locator("[data-bulk-field-window]")
    window.wait_for()
    assert int(window.get_attribute("data-total")) >= 105
    assert int(window.get_attribute("data-shown")) == 40
    attribute_options = bulk_field.locator(
        'optgroup[label="Характеристики среди выбранных карточек"] option'
    )
    assert attribute_options.count() == 40
    assert bulk_field.locator(f'option[value="attribute:{target_field}"]').count() == 0
    assert bulk_field.locator(
        'optgroup[label="Основные поля"] option'
    ).count() >= 6
    assert f"Выбрано {len(selected_ids)}" in editor.locator(
        ".orb-toolbar",
    ).inner_text()

    search_bounds = search.bounding_box()
    field_bounds = bulk_field.bounding_box()
    assert search_bounds and field_bounds
    for bounds in (search_bounds, field_bounds):
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 321
    page.screenshot(
        path=str(OUT / "ozon-mixed-type-bulk-fields-mobile.png"),
        animations="disabled",
    )

    search.focus()
    search.fill(outside_field_name)
    assert int(window.get_attribute("data-total")) >= 105
    assert int(window.get_attribute("data-shown")) == 0
    assert bulk_field.locator(
        'optgroup[label="Характеристики среди выбранных карточек"] option'
    ).count() == 0
    assert f"Выбрано {len(selected_ids)}" in editor.locator(
        ".orb-toolbar",
    ).inner_text()

    search.fill(target_name)
    assert int(window.get_attribute("data-shown")) == 1
    target_option = bulk_field.locator(f'option[value="attribute:{target_field}"]')
    assert target_option.count() == 1
    assert target_name in target_option.inner_text()
    bulk_field.focus()
    bulk_field.press("End")
    bulk_field.press("Enter")
    assert bulk_field.input_value() == f"attribute:{target_field}"
    assert f"Выбрано {len(selected_ids)}" in editor.locator(
        ".orb-toolbar",
    ).inner_text()

    typed_value = dialog.get_by_label(target_name, exact=True)
    assert typed_value.get_attribute("inputmode") == "decimal"
    typed_value.fill(new_value)
    dialog.get_by_role("button", name="Посмотреть изменения", exact=True).click()
    assert dialog.locator(".orb-review-list > li").count() == len(target_ids)
    planned_titles = {
        item.locator("strong").first.inner_text()
        for item in dialog.locator(".orb-review-list > li").all()
    }
    assert planned_titles == {titles[draft_id] for draft_id in target_ids}
    preview_summary = dialog.locator("[data-bulk-preview-summary]").inner_text()
    assert "Будет заполнено" in preview_summary
    assert str(len(target_ids)) in preview_summary
    assert "Пропущено" in preview_summary
    assert str(len(other_selected_ids)) in preview_summary
    assert str(len(selected_ids)) in preview_summary
    skipped_details = dialog.locator("details")
    assert skipped_details.count() == 1, "expected one skipped-row disclosure"
    skipped_summary = skipped_details.locator("summary")
    assert skipped_summary.inner_text().strip() == (
        f"Пропущено: {len(other_selected_ids)}"
    )
    skipped_summary.click()
    assert skipped_details.evaluate("element => element.open") is True
    skipped_rows = skipped_details.locator(".orb-skipped li")
    assert skipped_rows.count() == len(other_selected_ids)
    assert all(row.locator("strong").is_visible() for row in skipped_rows.all())
    skipped_titles = {
        row.locator("strong").inner_text().strip()
        for row in skipped_rows.all()
    }
    expected_skipped_titles = {titles[draft_id] for draft_id in other_selected_ids}
    assert skipped_titles == expected_skipped_titles, (
        f"Skipped draft names mismatch: actual={sorted(skipped_titles)!r}; "
        f"expected={sorted(expected_skipped_titles)!r}"
    )
    assert outside_title not in dialog.inner_text()
    assert f"Выбрано {len(selected_ids)}" in editor.locator(
        ".orb-toolbar",
    ).inner_text()

    dialog.get_by_role("button", name="Заполнить в форме", exact=True).click()
    assert f"Выбрано {len(selected_ids)}" in editor.locator(
        ".orb-toolbar",
    ).inner_text()
    editor.locator(".orb-toolbar").get_by_role(
        "button", name="Проверить изменения", exact=True,
    ).click()
    save_dialog = page.get_by_role("dialog")
    save_dialog.wait_for(state="visible")
    assert save_dialog.locator(".orb-review-list > li").count() == len(selected_ids)
    assert save_dialog.locator(".orb-review-field").count() == len(target_ids)
    save_copy = save_dialog.inner_text()
    assert "Будут сохранены в Seller Hub" in save_copy
    assert str(len(selected_ids)) in save_copy

    before_ops = baseline_counts["operations"]
    before_jobs_runs = (baseline_counts["runs"], baseline_counts["items"])
    save_posts_marker = report["post_counts"].get(
        "/marketplaces/ozon/uploads/selection/repair/apply", 0,
    )
    save_dialog.locator("[data-draft-selection-save]").click()
    receipt = editor.locator(".orb-receipt[data-draft-selection-receipt]")
    receipt.wait_for()
    receipt.locator("strong").filter(
        has_text=f"Обработано черновиков: {len(selected_ids)}",
    ).wait_for()
    assert state["selection_repair_post"]["draft_ids"] == selected_ids
    assert state["selection_repair_post"]["csrf"] is True
    assert report["post_counts"][
        "/marketplaces/ozon/uploads/selection/repair/apply"
    ] == save_posts_marker + 1

    after = _mixed_draft_snapshots([*selected_ids, outside_id])
    assert set(after) == set(initial)
    affected = set()
    unchanged = set()
    for draft_id in selected_ids:
        old = initial[draft_id]
        new = after[draft_id]
        old_non_target = [
            item for item in old["attributes"]
            if item.get("attribute_id") != target_field
        ]
        new_non_target = [
            item for item in new["attributes"]
            if item.get("attribute_id") != target_field
        ]
        assert new_non_target == old_non_target
        target_attributes = [
            item for item in new["attributes"]
            if item.get("attribute_id") == target_field
        ]
        assert new["content"] == old["content"]
        assert new["commercial"] == old["commercial"]
        assert new["dimensions"] == old["dimensions"]
        if draft_id in target_ids:
            assert len(target_attributes) == 1
            assert target_attributes[0]["complex_id"] == "0"
            assert target_attributes[0]["values"] == [{"value": new_value}]
            affected.add(draft_id)
        else:
            assert target_attributes == []
            assert new["attributes"] == old["attributes"]
            unchanged.add(draft_id)
    assert affected == target_ids
    assert unchanged == other_selected_ids
    assert after[outside_id] == initial[outside_id]
    counts_after = _business_counts()
    assert counts_after["operations"] == before_ops
    assert (counts_after["runs"], counts_after["items"]) == before_jobs_runs
    assert report["provider_attempts"] == provider_attempts_before
    assert report["provider_dispatch_calls"] == provider_dispatch_calls_before
    assert report["provider_attempts"] == 0
    assert report["provider_dispatch_calls"] == 0
    assert len(business_write_events) > writes_before
    report["mixed_type_selection"] = {
        "draft_rows": len(selected_ids),
        "distinct_editable_attribute_ids": len(editable_ids),
        "max_editable_fields_per_card": max(
            sum(field["editable"] for field in row["attributes"])
            for row in rows
        ),
        "affected_rows": len(affected),
        "unchanged_rows": len(unchanged),
        "planned_draft_ids": sorted(target_ids),
        "skipped_draft_ids": sorted(other_selected_ids),
        "unselected_draft_id": outside_id,
        "unselected_row_unchanged": after[outside_id] == initial[outside_id],
        "field_filter_preserved_exact_membership": True,
        "local_save_posts": 1,
        "explicit_local_save": True,
        "public_operations": counts_after["operations"] - before_ops,
        "no_public_operations": counts_after["operations"] == before_ops,
        "new_runs_or_items": [
            counts_after["runs"] - before_jobs_runs[0],
            counts_after["items"] - before_jobs_runs[1],
        ],
        "provider_attempts": report["provider_attempts"],
        "new_provider_attempts": report["provider_attempts"] - provider_attempts_before,
        "provider_dispatch_calls": report["provider_dispatch_calls"],
        "new_provider_dispatch_calls": (
            report["provider_dispatch_calls"] - provider_dispatch_calls_before
        ),
        "mobile_viewport": {
            "width": 320,
            "height": 900,
            "no_horizontal_overflow": True,
        },
        "keyboard_open_and_field_choice": True,
        "mobile_screenshot": "ozon-mixed-type-bulk-fields-mobile.png",
    }
    passed("mixed_type_bulk_editor_opens_and_saves_exact_applicable_rows")


def _seed_review_editor_navigation_batch():
    """Prepare two unchanged rows and one actionable weight issue for return UX."""
    titles = {
        "unchanged_first": "BR Review Return · неизменённая первая",
        "weight_issue": "BR Review Return · исправление веса",
        "unchanged_last": "BR Review Return · неизменённая последняя",
    }
    with app.app_context():
        product_type = MarketplaceProductType.query.filter_by(
            marketplace_id=marketplace_id,
            external_type_id="1000",
        ).one()
        dimensions_category = product_type.category.name
        first = _make_source(
            seller_id=fixture["seller_id"],
            external_id="br-review-return-unchanged-first",
            vendor_code="BR-REVIEW-RETURN-FIRST",
            title=titles["unchanged_first"],
            description="Синтетическая готовая карточка для точной проверки возврата.",
            category=dimensions_category,
            barcode="4600000000911",
        )
        weight_issue = _make_source(
            seller_id=fixture["seller_id"],
            external_id="br-review-return-weight-issue",
            vendor_code="BR-REVIEW-RETURN-WEIGHT",
            title=titles["weight_issue"],
            description="Синтетическая карточка с одним отсутствующим весом упаковки.",
            category=dimensions_category,
            barcode="4600000000912",
        )
        raw = json.loads(weight_issue.original_data)
        raw["dimensions"].pop("Вес упаковки, г")
        weight_issue.original_data = json.dumps(raw, ensure_ascii=False)
        last = _make_source(
            seller_id=fixture["seller_id"],
            external_id="br-review-return-unchanged-last",
            vendor_code="BR-REVIEW-RETURN-LAST",
            title=titles["unchanged_last"],
            description="Ещё одна синтетическая готовая карточка для точной проверки возврата.",
            category=dimensions_category,
            barcode="4600000000913",
        )
        sources = [first, weight_issue, last]
        db.session.add_all(sources)
        db.session.flush()
        source_ids = [source.id for source in sources]

        source_identity = MarketplaceDraftService._mapping_identities(first)[-1]
        exact_mapping = MarketplaceCategoryMapping.query.filter_by(
            seller_id=fixture["seller_id"],
            marketplace_id=marketplace_id,
            scope_key=source_identity["scope_key"],
            source_type=source_identity["source_type"],
            source_category_normalized=source_identity["source_category_normalized"],
            product_type_id=product_type.id,
            mapping_status="active",
        ).first()
        assert exact_mapping is not None

        acceptance = OzonBulkUploadService.accept_source_prepare(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            imported_product_ids=source_ids,
            request_key="br-review-editor-return-prepare-00000001",
            created_by_user_id=fixture.get("user_id"),
        )
        job_uid = acceptance.job.job_uid
        stats = OzonBulkUploadService.run_due_preparation()
        run = OzonBulkUploadRun.query.filter_by(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            mode="source_prepare",
        ).join(BackgroundJob, BackgroundJob.id == OzonBulkUploadRun.job_id).filter(
            BackgroundJob.job_uid == job_uid,
        ).one()
        items = OzonBulkUploadItem.query.filter_by(
            run_id=run.id,
        ).order_by(OzonBulkUploadItem.ordinal.asc()).all()
        assert [item.imported_product_id for item in items] == source_ids
        assert len(items) == 3 and all(item.draft_id for item in items)
        assert [item.phase for item in items] == ["prepared", "needs_input", "prepared"]
        assert stats["prepared"] == 2 and stats["needs_input"] == 1, stats
        drafts = [db.session.get(MarketplaceProductDraft, item.draft_id) for item in items]
        assert [draft.status for draft in drafts] == ["ready", "blocked", "ready"]
        draft_ids = [item.draft_id for item in items]
        versions = {
            queue_item.draft_id: draft.version
            for queue_item, draft in zip(items, drafts)
        }
        return job_uid, draft_ids, source_ids, titles, versions, stats


def _review_editor_navigation_operation_rows(draft_ids):
    with app.app_context():
        return MarketplaceOperation.query.filter(
            MarketplaceOperation.seller_id == fixture["seller_id"],
            MarketplaceOperation.account_id == account_id,
            MarketplaceOperation.draft_id.in_(draft_ids),
        ).order_by(MarketplaceOperation.id.asc()).with_entities(
            MarketplaceOperation.id,
            MarketplaceOperation.draft_id,
            MarketplaceOperation.operation_kind,
            MarketplaceOperation.status,
        ).all()


def run_review_editor_navigation_journey(page):
    """Follow one exact review issue into a saved fix and back to its batch."""
    job_uid, draft_ids, source_ids, titles, prepared_versions, worker_stats = (
        _seed_review_editor_navigation_batch()
    )
    issue_draft_id = draft_ids[1]
    unchanged_ids = [draft_ids[0], draft_ids[2]]
    state["review_editor_navigation_job_uid"] = job_uid
    state["review_editor_navigation_draft_ids"] = list(draft_ids)
    state["review_editor_navigation_draft_id"] = issue_draft_id
    state["review_editor_navigation_initial_version"] = prepared_versions[issue_draft_id]

    page.set_viewport_size({"width": 320, "height": 780})
    review_html_navigations = []

    def record_review_html_navigation(request):
        if not request.is_navigation_request():
            return
        parts = urlsplit(request.url)
        if parts.path != "/marketplaces/ozon/uploads/review":
            return
        query = parse_qs(parts.query, keep_blank_values=True)
        review_html_navigations.append({
            "account_id": int(query["account_id"][0]),
            "draft_ids": [int(value) for value in query["draft_ids"][0].split(",")],
            "page": int(query.get("page", ["1"])[0]),
            "parent_prepare_job_uid": query.get("parent_prepare_job_uid", [None])[0],
            "result_job_uid": query.get("result_job_uid", [None])[0],
        })

    page.on("request", record_review_html_navigation)
    page.goto(f"{base}/marketplaces/ozon/uploads/{job_uid}")
    source_review_link = page.locator("a[data-source-prepare-review]")
    source_review_link.wait_for()
    source_review_href = source_review_link.get_attribute("href")
    source_review_parts = urlsplit(source_review_href)
    source_review_query = parse_qs(source_review_parts.query)
    assert source_review_parts.path == "/marketplaces/ozon/uploads/review"
    assert source_review_query.get("account_id") == [str(account_id)]
    assert source_review_query.get("parent_prepare_job_uid") == [job_uid]
    assert [
        int(value) for value in source_review_query["draft_ids"][0].split(",")
    ] == draft_ids

    review_counts = _business_counts()
    review_write_marker = len(business_write_events)
    review_post_counts = dict(report["post_counts"])
    provider_marker = (report["provider_attempts"], report["provider_dispatch_calls"])
    assert _review_editor_navigation_operation_rows(draft_ids) == []
    source_review_link.click()
    page.wait_for_url("**/marketplaces/ozon/uploads/review?*")
    review_root = page.locator("#ozon-upload-review:not([v-cloak])")
    review_root.wait_for()
    review_header = page.locator("section.our > header.our-header")
    review_guide = review_header.locator(
        ".preparation-journey--compact[aria-label='Этапы подготовки']"
    )
    guide_disclosure = review_guide.locator("details.preparation-journey-path")
    guide_summary = guide_disclosure.locator("summary")
    initial_review = page.evaluate(
        "() => JSON.parse(document.querySelector('#our-bootstrap').textContent).review"
    )
    assert initial_review["account_id"] == account_id
    assert initial_review["parent_prepare_job_uid"] == job_uid
    assert not initial_review.get("result_job_uid")
    assert initial_review["draft_ids"] == draft_ids
    assert initial_review["pagination"] == {
        "page": 1, "per_page": 20, "pages": 1, "total": 3,
    }
    assert initial_review.get("review_return_token")
    assert [row["draft_id"] for row in initial_review["items"]] == draft_ids

    # The review has one concise SSR header; scoped route guidance remains in
    # one closed disclosure and can be opened and closed before choosing rows.
    assert review_header.locator("#our-title").inner_text() == "Проверка перед отправкой"
    header_text = review_header.inner_text()
    assert initial_review["account_label"] in header_text
    assert f"Карточек в наборе: {len(draft_ids)}" in header_text
    assert "страница 1 из 1" in header_text
    source_checked_at = initial_review["checked_at"]
    parsed_checked_at = datetime.fromisoformat(source_checked_at).replace(
        tzinfo=timezone.utc,
    )
    expected_checked_at_label = parsed_checked_at.strftime("%d.%m.%Y, %H:%M UTC")
    expected_checked_at_datetime = parsed_checked_at.isoformat().replace("+00:00", "Z")
    checked_at_element = review_header.locator("time")
    assert checked_at_element.count() == 1
    assert checked_at_element.inner_text() == expected_checked_at_label
    assert checked_at_element.get_attribute("datetime") == expected_checked_at_datetime
    assert source_checked_at not in header_text
    assert review_header.locator("a.our-back").get_attribute("href") == (
        f"/marketplaces/ozon/uploads/{job_uid}"
    )
    assert guide_summary.inner_text().strip() == "Этапы подготовки"
    assert guide_disclosure.get_attribute("open") is None
    assert review_root.locator(".our-list input[type='checkbox']:checked").count() == 0

    with app.test_request_context("/"):
        from flask import url_for
        expected_guide_links = {
            "Источник поставщика": url_for("supplier_catalog"),
            "Внутренний товар": url_for(
                "seller_my_products", account_id=account_id,
            ),
            "Черновик Ozon": url_for(
                "marketplace_drafts.index", account_id=account_id,
            ),
            "Результат операции": url_for(
                "ozon_bulk_uploads.detail", job_uid=job_uid,
            ),
            "Карточка Ozon": url_for(
                "marketplace_listings.index", marketplace="ozon",
                account_id=account_id,
            ),
        }
    guide_summary.focus()
    page.keyboard.press("Enter")
    assert guide_disclosure.get_attribute("open") is not None
    actual_guide_links = {}
    for step in review_guide.locator("li").all():
        label = step.locator("strong").inner_text().strip()
        link = step.locator("a")
        if label in expected_guide_links:
            assert link.count() == 1, (label, link.count())
            actual_guide_links[label] = link.get_attribute("href")
        elif label == "Свежая проверка":
            assert link.count() == 0 and step.get_attribute("aria-current") == "step"
        elif label == "Отдельная отправка":
            assert link.count() == 0 and step.get_attribute("aria-disabled") == "true"
    assert actual_guide_links == expected_guide_links, (
        actual_guide_links, expected_guide_links,
    )
    first_guide_link = review_guide.locator("ol a").first
    page.keyboard.press("Tab")
    assert first_guide_link.evaluate("el => document.activeElement === el")
    assert first_guide_link.evaluate("el => el.matches(':focus-visible')")
    guide_summary.focus()
    page.keyboard.press("Enter")
    assert guide_disclosure.get_attribute("open") is None
    page.evaluate("() => { document.activeElement?.blur(); window.scrollTo(0, 0); }")

    compact_measurement = page.evaluate("""() => {
        const rect = selector => {
            const element = document.querySelector(selector);
            if (!element) return null;
            const box = element.getBoundingClientRect();
            return {top:box.top, bottom:box.bottom, left:box.left, right:box.right,
                width:box.width, height:box.height};
        };
        return {
            viewport:{width:innerWidth,height:innerHeight},
            document:{width:document.documentElement.clientWidth,
                scrollWidth:document.documentElement.scrollWidth},
            toolbar:rect('#ozon-upload-review .our-toolbar'),
            firstCard:rect('#ozon-upload-review .our-card'),
            firstCardTitle:rect('#ozon-upload-review .our-card h2 a'),
            scrollY:window.scrollY,
            detailsOpen:document.querySelectorAll(
                '.our-header .preparation-journey--compact details[open]').length,
        };
    }""")
    toolbar_action = review_root.get_by_role(
        "button", name="Проверить выбранные", exact=True,
    )
    review_all_action = review_root.get_by_role(
        "button", name="Проверить все готовые", exact=True,
    )
    first_card_title = review_root.locator(".our-card h2 a").first
    toolbar_box = review_root.locator(".our-toolbar").bounding_box()
    action_box = toolbar_action.bounding_box()
    all_ready_box = review_all_action.bounding_box()
    first_card_box = review_root.locator(".our-card").first.bounding_box()
    first_card_title_box = first_card_title.bounding_box()
    assert toolbar_box and 0 <= toolbar_box["y"] < 780
    assert toolbar_box["y"] + toolbar_box["height"] <= 780
    assert action_box and 0 <= action_box["y"] < 780
    assert action_box["y"] + action_box["height"] <= 780
    assert all_ready_box and 0 <= all_ready_box["y"] < 780
    assert all_ready_box["y"] + all_ready_box["height"] <= 780
    assert first_card_box and first_card_box["y"] < 780
    assert first_card_title_box and 0 <= first_card_title_box["y"] < 780
    assert first_card_title_box["y"] + first_card_title_box["height"] <= 780
    assert compact_measurement["document"]["scrollWidth"] <= compact_measurement["document"]["width"]
    assert compact_measurement["detailsOpen"] == 0
    assert compact_measurement["scrollY"] == 0
    assert review_root.locator(".our-card").count() == len(draft_ids)
    generic_links = []
    ordered_titles = [
        titles["unchanged_first"], titles["weight_issue"], titles["unchanged_last"],
    ]
    for draft_id, title, card in zip(
        draft_ids, ordered_titles, review_root.locator(".our-card").all(),
    ):
        title_link = card.locator("h2 > a")
        assert title_link.count() == 1, draft_id
        assert card.locator(".our-card-link").count() == 0, draft_id
        assert title_link.inner_text().strip() == title, draft_id
        assert title_link.get_attribute("aria-label") == "Открыть черновик " + title
        assert title_link.evaluate("""el => {
            const style = getComputedStyle(el), rect = el.getBoundingClientRect();
            return style.textDecorationLine.includes('underline') && rect.height >= 44;
        }"""), draft_id
        title_parts = urlsplit(title_link.get_attribute("href"))
        title_query = parse_qs(title_parts.query)
        assert title_parts.path == f"/marketplaces/drafts/{draft_id}"
        assert title_query.get("review_return") == [initial_review["review_return_token"]]
        assert not title_query.get("focus_code") and not title_query.get("focus_field")
        generic_links.append(title_link.get_attribute("href"))

    compact_screenshot = OUT / "ozon-review-compact-header-320.png"
    page.screenshot(path=str(compact_screenshot), animations="disabled")
    review_rows = {row["draft_id"]: row for row in initial_review["items"]}
    assert all(review_rows[draft_id]["selectable"] for draft_id in unchanged_ids)
    issue_row = review_rows[issue_draft_id]
    assert not issue_row["selectable"]
    weight_issues = [
        issue for issue in issue_row["errors"]
        if issue.get("field") == "dimensions.weight"
    ]
    assert len(weight_issues) == 1, issue_row["errors"]
    weight_issue = weight_issues[0]
    assert weight_issue["code"] == "physical_fact_required"

    # Save a two-row selection in this exact batch. Its identity includes the
    # parent job, account, ordered IDs and page and survives the editor trip.
    for draft_id in unchanged_ids:
        checkbox = page.locator(f"#our-select-{draft_id}")
        assert checkbox.is_enabled()
        checkbox.check()
    initial_selected = page.locator(
        ".our-list input[type='checkbox']:checked"
    ).evaluate_all("nodes => nodes.map(node => Number(node.id.replace('our-select-', '')))")
    assert initial_selected == unchanged_ids, initial_selected

    issue_card = page.locator(".our-card").filter(has_text=titles["weight_issue"])
    issue_links = issue_card.locator("a[data-review-issue-link]")
    issue_link_count = issue_links.count()
    assert issue_link_count >= 1
    issue_link = None
    issue_href = None
    for index in range(issue_links.count()):
        candidate = issue_links.nth(index)
        candidate_href = candidate.get_attribute("href")
        candidate_query = parse_qs(urlsplit(candidate_href).query)
        if candidate_query.get("focus_field") == ["dimensions.weight"]:
            issue_link = candidate
            issue_href = candidate_href
            break
    assert issue_link is not None and issue_href
    issue_parts = urlsplit(issue_href)
    issue_query = parse_qs(issue_parts.query)
    assert issue_parts.path == f"/marketplaces/drafts/{issue_draft_id}"
    assert issue_query.get("review_return") == [initial_review["review_return_token"]]
    assert issue_query.get("focus_code") == [weight_issue["code"]]
    assert issue_query.get("focus_field") == [weight_issue["field"]]

    issue_link.focus()
    assert issue_link.evaluate("link => document.activeElement === link")
    page.keyboard.press("Enter")
    page.wait_for_url(f"**/marketplaces/drafts/{issue_draft_id}?*")
    page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
    editor_boot = page.evaluate(
        "() => JSON.parse(document.querySelector('#ode-bootstrap').textContent)"
    )
    assert editor_boot["draftId"] == issue_draft_id
    assert editor_boot["accountId"] == account_id
    navigation = editor_boot["reviewNavigation"]
    assert navigation["focus"] == {
        "code": weight_issue["code"], "field": weight_issue["field"],
    }
    canonical_return = urlsplit(navigation["url"])
    canonical_query = parse_qs(canonical_return.query)
    assert canonical_return.path == "/marketplaces/ozon/uploads/review"
    assert canonical_query.get("account_id") == [str(account_id)]
    assert [
        int(value) for value in canonical_query["draft_ids"][0].split(",")
    ] == draft_ids
    assert canonical_query.get("page") == ["1"]
    assert canonical_query.get("parent_prepare_job_uid") == [job_uid]
    assert not canonical_query.get("result_job_uid")
    assert parse_qs(urlsplit(page.url).query).get("review_return") == [
        initial_review["review_return_token"]
    ]

    weight_input = page.locator("#ode-weight")
    page.wait_for_function(
        "() => document.activeElement?.id === 'ode-weight'"
    )
    weight_bounds = weight_input.bounding_box()
    assert weight_bounds and 0 <= weight_bounds["y"] < 780
    assert weight_bounds["y"] + weight_bounds["height"] <= 780
    assert weight_input.evaluate("""input => {
        const style = getComputedStyle(input);
        return document.activeElement === input && style.outlineStyle !== 'none' &&
            parseFloat(style.outlineWidth) >= 2;
    }""")
    issue_screenshot = OUT / "ozon-review-edit-issue-320.png"
    page.screenshot(path=str(issue_screenshot), animations="disabled")

    # GET navigation to the editor and its fresh validation payload are read-only.
    assert len(business_write_events) == review_write_marker
    assert _business_counts() == review_counts
    assert report["post_counts"] == review_post_counts
    assert (report["provider_attempts"], report["provider_dispatch_calls"]) == provider_marker
    assert _review_editor_navigation_operation_rows(draft_ids) == []

    weight_input.fill("250")
    page.locator("#ode-weight-unit").select_option("GRAMS")
    page.locator("[data-package-confirmation-needed]").wait_for(state="visible")
    page.locator("#ode-package-confirm").check()
    save_button = page.locator(".ode-savebar").get_by_role(
        "button", name="Сохранить", exact=True,
    )
    with page.expect_response(lambda response: (
        response.request.method == "POST"
        and urlsplit(response.url).path == f"/marketplaces/drafts/{issue_draft_id}"
    )) as save_response_info:
        save_button.click()
    save_response = save_response_info.value
    assert save_response.status == 200
    assert state["review_editor_navigation_save_posts"] == 1
    saved_version = state["review_editor_navigation_saved_version"]
    assert saved_version > prepared_versions[issue_draft_id]

    validate_button = page.locator(".ode-validation").get_by_role(
        "button", name="Проверить карточку", exact=True,
    )
    with page.expect_response(lambda response: (
        response.request.method == "POST"
        and urlsplit(response.url).path == f"/marketplaces/drafts/{issue_draft_id}/validate"
    )) as validate_response_info:
        validate_button.click()
    validate_response = validate_response_info.value
    assert validate_response.status == 200
    validated_version = state["review_editor_navigation_validated_version"]
    assert validated_version > saved_version
    assert state["review_editor_navigation_validate_posts"] == 1
    page.locator(".ode-validation .ode-message--success").wait_for()
    with app.app_context():
        corrected_draft = db.session.get(MarketplaceProductDraft, issue_draft_id)
        assert corrected_draft.status == "ready"
        assert corrected_draft.version == validated_version
        corrected_dimensions = json.loads(corrected_draft.dimensions_json)
        assert corrected_dimensions["weight"] == "250"
        assert corrected_dimensions["weight_unit"] == "GRAMS"
    assert _review_editor_navigation_operation_rows(draft_ids) == []
    assert report["provider_attempts"] == 0 and report["provider_dispatch_calls"] == 0

    return_link = page.locator("a[data-review-return]")
    return_link.wait_for()
    assert return_link.inner_text().strip() == "К проверке партии"
    assert urlsplit(return_link.get_attribute("href")).geturl() == navigation["url"]
    before_return_counts = _business_counts()
    before_return_writes = len(business_write_events)
    before_return_posts = dict(report["post_counts"])
    with app.app_context():
        run_count = OzonBulkUploadRun.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id,
        ).count()
        item_count = OzonBulkUploadItem.query.join(
            OzonBulkUploadRun, OzonBulkUploadItem.run_id == OzonBulkUploadRun.id,
        ).filter(
            OzonBulkUploadRun.seller_id == fixture["seller_id"],
            OzonBulkUploadRun.account_id == account_id,
        ).count()
    return_link.click()
    page.wait_for_url("**/marketplaces/ozon/uploads/review?*")
    review_root.wait_for()
    actual_return_url = urlsplit(page.url)
    assert actual_return_url.path == canonical_return.path
    assert parse_qs(actual_return_url.query) == canonical_query
    returned_review = page.evaluate(
        "() => JSON.parse(document.querySelector('#our-bootstrap').textContent).review"
    )
    assert returned_review["account_id"] == account_id
    assert returned_review["parent_prepare_job_uid"] == job_uid
    assert not returned_review.get("result_job_uid")
    assert returned_review["draft_ids"] == draft_ids
    assert returned_review["pagination"]["page"] == 1
    assert [row["draft_id"] for row in returned_review["items"]] == draft_ids
    returned_rows = {row["draft_id"]: row for row in returned_review["items"]}
    assert returned_rows[issue_draft_id]["selectable"] is True
    assert returned_rows[issue_draft_id]["version"] == validated_version
    assert all(returned_rows[draft_id]["selectable"] for draft_id in unchanged_ids)
    assert all(
        returned_rows[draft_id]["version"] == prepared_versions[draft_id]
        for draft_id in unchanged_ids
    )
    page.wait_for_function(
        "expected => JSON.stringify([...document.querySelectorAll('.our-list input[type=checkbox]:checked')]"
        ".map(node => Number(node.id.replace('our-select-', '')))) === JSON.stringify(expected)",
        arg=unchanged_ids,
    )
    returned_selected = page.locator(
        ".our-list input[type='checkbox']:checked"
    ).evaluate_all("nodes => nodes.map(node => Number(node.id.replace('our-select-', '')))")
    assert returned_selected == unchanged_ids
    changed_checkbox = page.locator(f"#our-select-{issue_draft_id}")
    assert changed_checkbox.is_enabled()
    assert not changed_checkbox.is_checked()
    assert len(business_write_events) == before_return_writes
    assert _business_counts() == before_return_counts
    assert report["post_counts"] == before_return_posts
    assert _review_editor_navigation_operation_rows(draft_ids) == []
    with app.app_context():
        assert OzonBulkUploadRun.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id,
        ).count() == run_count
        assert OzonBulkUploadItem.query.join(
            OzonBulkUploadRun, OzonBulkUploadItem.run_id == OzonBulkUploadRun.id,
        ).filter(
            OzonBulkUploadRun.seller_id == fixture["seller_id"],
            OzonBulkUploadRun.account_id == account_id,
        ).count() == item_count
    assert report["provider_attempts"] == 0 and report["provider_dispatch_calls"] == 0
    expected_review_navigation = {
        "account_id": account_id,
        "draft_ids": draft_ids,
        "page": 1,
        "parent_prepare_job_uid": job_uid,
        "result_job_uid": None,
    }
    assert review_html_navigations == [
        expected_review_navigation, expected_review_navigation,
    ], review_html_navigations
    assert state["review_editor_navigation_api_pages"] == [], (
        state["review_editor_navigation_api_pages"]
    )
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    return_screenshot = OUT / "ozon-review-return-exact-320.png"
    page.screenshot(path=str(return_screenshot), animations="disabled")

    report["review_editor_navigation"] = {
        "account_id": account_id,
        "parent_prepare_job_uid": job_uid,
        "result_job_uid": None,
        "draft_ids": draft_ids,
        "page": 1,
        "initial_selectable_draft_ids": unchanged_ids,
        "issue_draft_id": issue_draft_id,
        "issue_code": weight_issue["code"],
        "issue_field": weight_issue["field"],
        "editor_focused_input": "ode-weight",
        "focus_ring_visible": True,
        "explicit_local_save_posts": state["review_editor_navigation_save_posts"],
        "explicit_local_validation_posts": state["review_editor_navigation_validate_posts"],
        "prepared_version": prepared_versions[issue_draft_id],
        "saved_version": saved_version,
        "validated_version": validated_version,
        "returned_selectable": returned_rows[issue_draft_id]["selectable"],
        "unchanged_selection_preserved": returned_selected == unchanged_ids,
        "changed_entry_not_auto_selected": not changed_checkbox.is_checked(),
        "review_html_navigation_count": len(review_html_navigations),
        "review_html_navigation_scopes": review_html_navigations,
        "review_api_page_numbers": state["review_editor_navigation_api_pages"],
        "review_api_page_request_count": len(state["review_editor_navigation_api_pages"]),
        "navigation_business_writes": 0,
        "navigation_posts": 0,
        "operations_for_batch_drafts": 0,
        "provider_attempts": report["provider_attempts"],
        "provider_dispatch_calls": report["provider_dispatch_calls"],
        "mobile_viewport": {"width": 320, "height": 780},
        "compact_initial": {
            "header_title": "Проверка перед отправкой",
            "account_label": initial_review["account_label"],
            "draft_count": len(draft_ids),
            "page": [initial_review["pagination"]["page"], initial_review["pagination"]["pages"]],
            "checked_at": initial_review["checked_at"],
            "checked_at_label": expected_checked_at_label,
            "checked_at_datetime": expected_checked_at_datetime,
            "guide_label": guide_summary.inner_text().strip(),
            "guide_opened_and_closed_with_keyboard": True,
            "guide_links": actual_guide_links,
            "generic_editor_links": len(generic_links),
            "issue_links": issue_link_count,
            "toolbar_actions_visible": {
                "check_selected": bool(
                    action_box and 0 <= action_box["y"] < 780
                    and action_box["y"] + action_box["height"] <= 780
                ),
                "check_all_ready": bool(
                    all_ready_box and 0 <= all_ready_box["y"] < 780
                    and all_ready_box["y"] + all_ready_box["height"] <= 780
                ),
            },
            "first_card_visible": bool(first_card_box and first_card_box["y"] < 780),
            "first_card_title_visible": bool(
                first_card_title_box and 0 <= first_card_title_box["y"] < 780
            ),
            "no_horizontal_overflow": (
                compact_measurement["document"]["scrollWidth"]
                <= compact_measurement["document"]["width"]
            ),
            "geometry": compact_measurement,
            "screenshot": compact_screenshot.name,
        },
        "screenshots": [issue_screenshot.name, return_screenshot.name, compact_screenshot.name],
    }
    passed("review_issue_editor_returns_to_exact_fresh_batch_without_automatic_publication")


def run_browser_journey():
    with app.app_context():
        additional_account = SellerMarketplaceAccount(
            seller_id=fixture["seller_id"],
            marketplace_id=marketplace_id,
            external_account_id="100003",
            label="Ozon CI additional",
            is_active=True,
            is_default=False,
            connection_status="connected",
        )
        additional_account.set_credentials({
            "client_id": "100003",
            "api_key": "synthetic-key-not-valid-at-provider",
        })
        db.session.add(additional_account)
        db.session.flush()
        additional_account_id = additional_account.id
        db.session.commit()
        account_rows = SellerMarketplaceAccount.query.filter_by(
            seller_id=fixture["seller_id"], marketplace_id=marketplace_id,
            is_active=True,
        ).all()
        assert {row.id for row in account_rows} == {account_id, additional_account_id}
        assert len(account_rows) == 2
        owner_account = db.session.get(SellerMarketplaceAccount, account_id)
        assert owner_account is not None
        origin_product_ids = [*source_ids.values(), fixture["source_id"]]
        origin_batch = _issue_supplier_import_batch_token(
            user_id=owner_account.seller.user.id,
            seller_id=fixture["seller_id"], imported_product_ids=origin_product_ids,
        )
        assert origin_batch
        baseline_counts = _business_counts()
    report["source_prepare"]["source_count"] = 4
    report["source_prepare"]["baseline_counts"] = baseline_counts

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get(
                "OZON_BROWSER_CHROMIUM",
                os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium"),
            ),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 980},
            service_workers="block",
            timezone_id="Europe/Moscow",
        )
        context.route("**/*", bridge)
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
        page.on("response", lambda response: report["unexpected_http"].append({
            "status": response.status,
            "path": urlsplit(response.url).path,
        }) if response.status >= 500 else None)

        page.goto(base + "/login")
        page.locator('input[name="username"]').fill(USERNAME)
        page.locator('input[name="password"]').fill(PASSWORD)
        page.get_by_role("button", name="Войти", exact=True).click()
        page.wait_for_url(lambda value: urlsplit(value).path != "/login")
        page.goto(base + "/my-products/beta?" + urlencode({"import_batch": origin_batch}))
        workspace = page.locator("#my-products-app:not([v-cloak])")
        workspace.wait_for()
        workspace.locator(".mp-list article.mp-row").nth(len(origin_product_ids) - 1).wait_for()
        assert workspace.locator(".mp-list article.mp-row").count() == len(origin_product_ids)

        for key, title in source_titles.items():
            page.get_by_role("checkbox", name=f"Выбрать {title}", exact=True).check()
        selected_ids = page.locator(
            ".mp-list article.mp-row input[type=checkbox]:checked"
        ).evaluate_all("nodes => nodes.map(node => Number(node.value))")
        assert set(selected_ids) == set(source_ids.values())
        assert len(selected_ids) == 4
        assert not business_write_events, business_write_events
        assert _business_counts() == baseline_counts
        report["source_prepare"]["page_selection_ids"] = selected_ids
        report["source_prepare"]["business_writes_before_prepare"] = len(business_write_events)
        state["source_prepare_expected_ids"] = selected_ids
        report["checks"].append("beta_page_exact_selection_is_read_only_before_local_prepare")

        source_action_navigations = []
        page.on("framenavigated", lambda frame: (
            source_action_navigations.append(frame.url)
            if frame == page.main_frame else None
        ))
        source_prepare_path = "/marketplaces/ozon/uploads/"
        source_prepare_posts_before = report["post_counts"].get(source_prepare_path, 0)
        invoke_workspace_action(page, "ozon")
        dialog = page.get_by_role("dialog")
        account_select = dialog.locator("#mp-ozon-account")
        dialog.locator("#mp-ozon-dialog-title").wait_for(state="visible")
        assert dialog.locator("#mp-ozon-dialog-title").inner_text().startswith("Подготовить 4 ")
        assert account_select.input_value() == ""
        assert account_select.evaluate("node => document.activeElement === node")
        prepare_button = dialog.get_by_role("button", name="Подготовить черновики", exact=True)
        assert prepare_button.is_disabled()
        assert report["post_counts"].get(source_prepare_path, 0) == source_prepare_posts_before
        assert state["source_prepare_post"] is None
        assert not source_action_navigations, source_action_navigations

        # Selecting a destination is a local choice. Escape closes without a
        # write; the same selected source set must still be available to reopen.
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        assert report["post_counts"].get(source_prepare_path, 0) == source_prepare_posts_before
        assert state["source_prepare_post"] is None
        assert page.locator(
            ".mp-list article.mp-row input[type=checkbox]:checked"
        ).evaluate_all("nodes => nodes.map(node => Number(node.value))") == selected_ids
        assert not source_action_navigations, source_action_navigations

        invoke_workspace_action(page, "ozon")
        dialog = page.get_by_role("dialog")
        dialog.locator("#mp-ozon-dialog-title").wait_for(state="visible")
        account_select = dialog.locator("#mp-ozon-account")
        assert account_select.input_value() == ""
        assert account_select.evaluate("node => document.activeElement === node")
        prepare_button = dialog.get_by_role("button", name="Подготовить черновики", exact=True)
        assert prepare_button.is_disabled()
        assert report["post_counts"].get(source_prepare_path, 0) == source_prepare_posts_before
        account_select.select_option(str(account_id))
        assert account_select.input_value() == str(account_id)
        assert prepare_button.is_enabled()
        assert report["post_counts"].get(source_prepare_path, 0) == source_prepare_posts_before
        assert state["source_prepare_post"] is None
        assert not source_action_navigations, source_action_navigations

        with page.expect_response(lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path == source_prepare_path
        )) as source_response_info:
            prepare_button.click()
        source_response = source_response_info.value
        assert source_response.status == 202
        assert state["source_prepare_post"] is not None
        assert state["source_prepare_post"]["account_id"] == account_id
        assert state["source_prepare_post"]["source_ids"] == selected_ids
        assert report["post_counts"].get(source_prepare_path, 0) == source_prepare_posts_before + 1
        assert state["source_worker_stats"]["prepared"] == 4
        assert state["source_worker_stats"]["needs_input"] == 0
        assert state["source_worker_stats"]["processed_items"] == 4
        assert state["source_job_uid"]
        report["source_prepare"]["multi_account_confirmation"] = {
            "active_accounts": len(account_rows),
            "select_initially_focused": True,
            "cta_disabled_until_account_chosen": True,
            "no_posts_on_open_escape_or_account_choice": True,
            "chosen_account_id": account_id,
            "selected_source_ids": selected_ids,
            "explicit_cta_posts": 1,
        }
        report["checks"].append("source_prepare_multi_account_choice_posts_exact_scope_once")
        job_path = f"/marketplaces/ozon/uploads/{state['source_job_uid']}"
        page.wait_for_url(f"**{job_path}")
        assert not urlsplit(page.url).fragment
        page.locator("h1").wait_for()

        return_link = page.locator('a[data-source-prepare-products][href*="#workspace-return="]')
        return_link.wait_for()
        original_posts = dict(report["post_counts"])
        workspace_counts = _business_counts()
        page.locator("[data-source-prepare-repair]").click()
        native_editor = page.locator('#ozon-bulk-repair[data-editor-mode="source_prepare"]:not([v-cloak])')
        native_editor.wait_for()
        native_editor.locator(".orb-back").click()
        page.wait_for_url(f"**{job_path}")
        assert not urlsplit(page.url).fragment
        return_link.wait_for()
        parsed_return = urlsplit(return_link.get_attribute("href"))
        assert parsed_return.path == "/my-products/beta" and not parsed_return.scheme and not parsed_return.netloc
        assert parse_qs(parsed_return.query).get("import_batch") == [origin_batch]
        assert re.fullmatch(r"workspace-return=[a-f0-9]{32}", parsed_return.fragment)
        page.set_viewport_size({"width": 320, "height": 850})
        return_link.press("Enter")
        workspace.wait_for()
        marked_rows = workspace.locator(".mp-list article.mp-row input[type=checkbox]:checked")
        marked_rows.nth(3).wait_for()
        assert set(marked_rows.evaluate_all("nodes => nodes.map(node => Number(node.value))")) == set(selected_ids)
        assert marked_rows.count() == 4
        assert workspace.locator(".mp-list article.mp-row").count() == len(origin_product_ids)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert _business_counts() == workspace_counts and report["post_counts"] == original_posts
        page.screenshot(path=OUT / "ozon-source-prepare-exact-import-return-320.png", animations="disabled")
        report["source_prepare"]["origin_import_return"] = {
            "original_cards": len(origin_product_ids), "restored_selected": len(selected_ids),
            "return_after_mass_editor": True, "additional_preparations": 0,
        }
        passed("source_preparation_return_restores_exact_signed_import_subset_after_editor")
        page.set_viewport_size({"width": 1440, "height": 980})
        page.goto(base + job_path)

        source_doc = page.locator("#ozon-result-status").count()
        detail_review_link = page.locator("a[data-source-prepare-review]")
        detail_review_link.wait_for()
        assert detail_review_link.inner_text().strip() == "Проверить перед отправкой"
        review_href = detail_review_link.get_attribute("href")
        review_parts = urlsplit(review_href)
        review_query = parse_qs(review_parts.query)
        assert review_parts.path == "/marketplaces/ozon/uploads/review"
        assert review_query.get("account_id") == [str(account_id)]
        assert review_query.get("parent_prepare_job_uid") == [state["source_job_uid"]]
        assert [int(value) for value in review_query["draft_ids"][0].split(",")] == state["draft_ids"]
        report["source_prepare"].update({
            "job_uid": state["source_job_uid"],
            "worker_stats": state["source_worker_stats"],
            "source_ids": state["source_item_ids"],
            "draft_ids": state["draft_ids"],
            "prepared_versions": state["prepared_versions"],
            "prepared_phases": state["prepared_phases"],
            "detail_review_link_exact": True,
            "detail_has_result_component": bool(source_doc),
        })
        passed("ordinary_beta_prepare_persisted_four_drafts_and_exact_job_review_link")

        repair_link = page.locator("a[data-source-prepare-repair]")
        repair_link.wait_for()
        repair_href = repair_link.get_attribute("href")
        assert urlsplit(repair_href).path == (
            f"/marketplaces/ozon/uploads/{state['source_job_uid']}/repair"
        )
        repair_link.click()
        page.wait_for_url(
            f"**/marketplaces/ozon/uploads/{state['source_job_uid']}/repair"
        )
        page.locator(
            '#ozon-bulk-repair[data-editor-mode="source_prepare"]'
        ).wait_for()
        editor = page.evaluate(
            "() => JSON.parse(document.querySelector('#orb-bootstrap').textContent).editor"
        )
        assert editor["mode"] == "source_prepare"
        assert editor["review_draft_ids"] == state["draft_ids"]
        assert editor["account_id"] == account_id
        page.set_viewport_size({"width": 320, "height": 780})
        draft_by_source = dict(zip(state["source_item_ids"], state["draft_ids"]))
        blocked_draft_id = draft_by_source[source_ids["blocked"]]
        with app.app_context():
            source_run = OzonBulkUploadRun.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
                mode="source_prepare",
            ).one()
            blocked_item = OzonBulkUploadItem.query.filter_by(
                run_id=source_run.id,
                imported_product_id=source_ids["blocked"],
            ).one()
            assert blocked_item.phase == "prepared"
            blocked_source_receipt = (
                blocked_item.phase,
                blocked_item.prepared_version,
                blocked_item.error_code,
                blocked_item.error_message,
            )
        blocked_editor_row_data = [
            row for group in editor["groups"] for row in group["rows"]
            if row["draft_id"] == blocked_draft_id
        ]
        assert len(blocked_editor_row_data) == 1
        assert blocked_editor_row_data[0]["phase"] == "prepared"
        assert blocked_editor_row_data[0]["status"] == "needs_input"
        blocked_editor_row = page.locator(
            f'.orb-row[data-draft="{blocked_draft_id}"]'
        )
        assert blocked_editor_row.locator(".orb-state").inner_text().strip() == "Нужны данные"

        # Use the actual native bulk editor to fill one field on one new and
        # one update draft. This remains a local edit to the prepared run.
        repair_row_ids = [
            draft_by_source[source_ids["create"]],
            draft_by_source[source_ids["update"]],
        ]
        state["repair_row_ids"] = repair_row_ids
        for draft_id in repair_row_ids:
            page.locator(
                f'.orb-row[data-draft="{draft_id}"] input[type="checkbox"]'
            ).check()
        page.get_by_role("button", name="Заполнить поле", exact=True).click()
        bulk_dialog = page.get_by_role("dialog")
        bulk_dialog.wait_for(state="visible")
        bulk_dialog.locator("#orb-bulk-field").select_option("description")
        bulk_dialog.get_by_label("Новое значение").fill(
            "Локальная групповая правка перед повторной проверкой."
        )
        bulk_dialog.get_by_role("button", name="Посмотреть изменения", exact=True).click()
        assert bulk_dialog.locator(".orb-review-list li").count() == 2
        bulk_dialog.get_by_role("button", name="Заполнить в форме", exact=True).click()
        for draft_id in repair_row_ids:
            row = page.locator(f'.orb-row[data-draft="{draft_id}"]')
            if not row.locator("textarea").count():
                row.get_by_role("button", name="Изменить", exact=True).click()
            assert row.locator(
                f'textarea[name="description-{draft_id}"]'
            ).input_value() == "Локальная групповая правка перед повторной проверкой."
        page.locator(".orb-toolbar").get_by_role(
            "button", name="Сохранить черновики", exact=True,
        ).click()
        save_dialog = page.get_by_role("dialog")
        save_dialog.wait_for(state="visible")
        assert save_dialog.locator(".orb-review-list li").count() == 2
        repair_write_marker = len(business_write_events)
        with page.expect_response(lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path.endswith("/repair/apply")
        )) as repair_response_info:
            save_dialog.get_by_role(
                "button", name="Сохранить черновики", exact=True,
            ).click()
        repair_response = repair_response_info.value
        assert repair_response.status == 200
        assert state["source_prepare_repair_post"] == {
            "draft_ids": repair_row_ids,
            "csrf": True,
        }
        page.locator(".orb-receipt[data-source-prepare-receipt] strong").filter(
            has_text="Обработано черновиков: 2"
        ).wait_for()
        receipt = page.locator(".orb-receipt[data-source-prepare-receipt]")
        return_review_link = receipt.locator("a[data-source-prepare-review]")
        return_review_link.wait_for()
        assert return_review_link.count() == 1
        page.wait_for_function(
            "() => document.activeElement === document.querySelector('[data-source-prepare-receipt]')"
        )
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        header_bounds = page.locator(".mobile-header").bounding_box()
        summary_bounds = receipt.locator("strong").bounding_box()
        review_link_bounds = return_review_link.bounding_box()
        assert header_bounds and summary_bounds and review_link_bounds
        header_bottom = header_bounds["y"] + header_bounds["height"]
        for name, bounds in (
            ("summary", summary_bounds),
            ("review link", review_link_bounds),
        ):
            assert bounds["y"] >= header_bottom, (name, bounds, header_bounds)
            assert bounds["y"] + bounds["height"] <= 780, (name, bounds)
            assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 320, (name, bounds)
        assert receipt.evaluate("node => document.activeElement === node")
        repair_mobile_screenshot = OUT / "ozon-bulk-source-prepare-repair-mobile.png"
        page.screenshot(path=str(repair_mobile_screenshot))
        with app.app_context():
            run = OzonBulkUploadRun.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
                mode="source_prepare",
            ).one()
            source_items = OzonBulkUploadItem.query.filter_by(
                run_id=run.id,
            ).order_by(OzonBulkUploadItem.ordinal.asc()).all()
            assert [item.draft_id for item in source_items] == state["draft_ids"]
            assert [item.phase for item in source_items] == ["prepared"] * 4
            blocked_after = next(
                item for item in source_items
                if item.imported_product_id == source_ids["blocked"]
            )
            assert (
                blocked_after.phase,
                blocked_after.prepared_version,
                blocked_after.error_code,
                blocked_after.error_message,
            ) == blocked_source_receipt
            for item in source_items[:2]:
                draft = db.session.get(MarketplaceProductDraft, item.draft_id)
                assert item.prepared_version == draft.version
                assert json.loads(draft.content_json)["description"] == (
                    "Локальная групповая правка перед повторной проверкой."
                )
            assert MarketplaceOperation.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
            ).count() == 0
        assert len(business_write_events) > repair_write_marker
        assert "marketplace_operations" not in business_write_events[repair_write_marker:]
        report["source_prepare"].update({
            "native_repair_mode": editor["mode"],
            "native_repair_ordered_draft_ids": editor["review_draft_ids"],
            "native_repair_saved_draft_ids": repair_row_ids,
            "native_repair_prepared_versions_current": True,
            "native_repair_operations_created": 0,
            "native_repair_mobile_viewport": {
                "width": 320,
                "height": 780,
                "focused_receipt": True,
                "summary_visible_below_fixed_header": True,
                "return_action_visible_below_fixed_header": True,
                "no_horizontal_overflow": True,
                "screenshot": repair_mobile_screenshot.name,
            },
        })
        passed("source_prepare_native_bulk_repair_saved_two_rows_locally_without_operation")

        return_review_href = return_review_link.get_attribute("href")
        return_parts = urlsplit(return_review_href)
        return_query = parse_qs(return_parts.query)
        assert return_parts.path == "/marketplaces/ozon/uploads/review"
        assert return_query.get("account_id") == [str(account_id)]
        assert return_query.get("parent_prepare_job_uid") == [state["source_job_uid"]]
        assert [int(value) for value in return_query["draft_ids"][0].split(",")] == state["draft_ids"]
        review_write_marker = len(business_write_events)
        return_review_link.click()
        page.wait_for_url("**/marketplaces/ozon/uploads/review?*")
        page.locator("#ozon-upload-review:not([v-cloak])").wait_for()
        bootstrap = page.evaluate(
            "() => JSON.parse(document.querySelector('#our-bootstrap').textContent).review"
        )
        assert bootstrap["account_id"] == account_id
        assert bootstrap["parent_prepare_job_uid"] == state["source_job_uid"]
        assert bootstrap["draft_ids"] == state["draft_ids"]
        assert [
            int(value) for value in parse_qs(urlsplit(page.url).query)["draft_ids"][0].split(",")
        ] == state["draft_ids"]
        preparation_back = page.get_by_role(
            "link", name="К результатам подготовки", exact=True,
        )
        preparation_back.wait_for()
        assert urlsplit(preparation_back.get_attribute("href")).path == (
            f"/marketplaces/ozon/uploads/{state['source_job_uid']}"
        )
        assert len(bootstrap["items"]) == 4
        rows_by_source = {
            row["imported_product_id"]: row for row in bootstrap["items"]
        }
        create_row = rows_by_source[source_ids["create"]]
        update_row = rows_by_source[source_ids["update"]]
        blocked_row = rows_by_source[source_ids["blocked"]]
        excluded_row = rows_by_source[source_ids["excluded"]]
        assert create_row["action"] == "create" and create_row["selectable"] is True
        assert update_row["action"] == "update" and update_row["selectable"] is True
        price_context = update_row.get("price_context")
        assert price_context == {
            "current": "1000",
            "calculated": "1300",
            "after_send": "1300",
            "current_old_price": "1200",
            "after_send_old_price": "1500",
            "currency_code": "RUB",
            "will_change": True,
            "old_price_will_change": True,
        }, price_context
        assert blocked_row["selectable"] is False
        assert "source_facts_stale" in {issue["code"] for issue in blocked_row["errors"]}
        page.get_by_text(
            "Исходный товар изменился. Откройте черновик и обновите данные товара.",
            exact=True,
        ).wait_for()
        assert excluded_row["action"] == "create" and excluded_row["selectable"] is True
        blocked_checkbox = page.locator(f"#our-select-{blocked_row['draft_id']}")
        assert blocked_checkbox.is_disabled()

        update_card = page.locator(".our-card").filter(
            has_text=source_titles["update"],
        )
        update_price = update_card.locator(
            '.our-price-fact[data-price-kind="seller"]'
        )
        assert update_price.get_attribute("data-price-mode") == "compare"
        assert update_price.inner_text().replace("\u202f", " ") == (
            "В Ozon: 1 000 ₽\nПосле отправки: 1 300 ₽"
        )
        update_old_price = update_card.locator(
            '.our-price-fact[data-price-kind="old"]'
        )
        assert update_old_price.get_attribute("data-price-mode") == "compare"
        assert update_old_price.inner_text().replace("\u202f", " ") == (
            "Цена до скидки в Ozon: 1 200 ₽\n"
            "Цена до скидки после отправки: 1 500 ₽"
        )
        create_card = page.locator(".our-card").filter(
            has_text=source_titles["create"],
        )
        assert create_card.locator(
            '.our-price-fact[data-price-mode="compare"]'
        ).count() == 0

        no_script_context = browser.new_context(
            java_script_enabled=False,
            storage_state=context.storage_state(),
            viewport={"width": 320, "height": 780},
        )
        no_script_context.route("**/*", bridge)
        no_script_page = no_script_context.new_page()
        no_script_page.goto(base + return_review_href)
        no_script_page.locator(".our-noscript").wait_for(state="visible")
        no_script_price = no_script_page.locator(".our-noscript .our-card").filter(
            has_text=source_titles["update"],
        ).locator(".our-noscript-price-pair")
        assert no_script_price.inner_text().replace("\u202f", " ") == (
            "В Ozon: 1000 ₽\nПосле отправки: 1300 ₽"
        )
        no_script_old_price = no_script_page.locator(
            ".our-noscript .our-card"
        ).filter(has_text=source_titles["update"]).locator(
            ".our-noscript-old-price-pair"
        )
        assert no_script_old_price.inner_text() == (
            "Цена до скидки в Ozon: 1200 ₽\n"
            "Цена до скидки после отправки: 1500 ₽"
        )
        no_script_page.close()
        no_script_context.close()

        page.set_viewport_size({"width": 320, "height": 780})
        batch_button = page.get_by_role("button", name="Проверить все готовые", exact=True)
        assert batch_button.is_visible()
        batch_button.click()
        review_dialog = page.get_by_role("dialog")
        review_dialog.wait_for(state="visible")
        for control in (
            batch_button,
            review_dialog.get_by_role("button", name="Закрыть подтверждение", exact=True),
        ):
            bounds = control.bounding_box()
            assert bounds and bounds["width"] >= 44 and bounds["height"] >= 44
        summary_spans = review_dialog.locator(".our-dialog-summary span").all_text_contents()
        assert "Готовы к отправке: 3 из 4 проверенных." in summary_spans, summary_spans
        assert "Требуют проверки или исправления: 1." in summary_spans, summary_spans
        modal_rows = review_dialog.locator(".our-dialog-list li")
        assert modal_rows.count() == 3
        assert source_titles["blocked"] not in review_dialog.inner_text()
        assert "Создание" in review_dialog.inner_text()
        assert "Обновление" in review_dialog.inner_text()
        update_modal_row = review_dialog.locator(".our-dialog-list li").filter(
            has_text=source_titles["update"],
        )
        update_modal_price = update_modal_row.locator(
            '.our-dialog-price-fact[data-price-kind="seller"]'
        )
        assert update_modal_price.get_attribute("data-price-mode") == "compare"
        actual_price_comparison = update_modal_price.inner_text().replace("\u202f", " ")
        assert actual_price_comparison == (
            "В Ozon: 1 000 ₽\nПосле отправки: 1 300 ₽"
        ), actual_price_comparison
        update_modal_old_price = update_modal_row.locator(
            '.our-dialog-price-fact[data-price-kind="old"]'
        )
        actual_old_price_comparison = update_modal_old_price.inner_text().replace("\u202f", " ")
        assert actual_old_price_comparison == (
            "Цена до скидки в Ozon: 1 200 ₽\n"
            "Цена до скидки после отправки: 1 500 ₽"
        ), actual_old_price_comparison
        assert not review_dialog.locator(".our-confirm input").is_checked()

        price_box = update_price.bounding_box()
        card_box = update_card.bounding_box()
        assert price_box and card_box
        assert price_box["x"] >= card_box["x"]
        assert price_box["x"] + price_box["width"] <= card_box["x"] + card_box["width"]

        ready_draft_ids = [create_row["draft_id"], update_row["draft_id"]]
        excluded_checkbox = review_dialog.get_by_role(
            "checkbox", name=f"Отправить {source_titles['excluded']}", exact=True,
        )
        assert excluded_checkbox.is_checked()
        excluded_checkbox.uncheck()
        assert not excluded_checkbox.is_checked()
        review_versions = {
            create_row["draft_id"]: create_row["version"],
            update_row["draft_id"]: update_row["version"],
        }
        state["ready_draft_ids"] = list(ready_draft_ids)
        state["review_versions"] = dict(review_versions)
        assert len(state["review_api_pages"]) == 1 and state["review_api_pages"] == [1]
        assert _business_counts()["operations"] == 0
        assert len(business_write_events) == review_write_marker, business_write_events[review_write_marker:]
        mobile_screenshot = OUT / "ozon-bulk-ready-review-mobile.png"
        page.screenshot(path=str(mobile_screenshot), full_page=True)
        report["review"].update({
            "account_id": account_id,
            "parent_prepare_job_uid": state["source_job_uid"],
            "all_draft_ids": bootstrap["draft_ids"],
            "row_count": len(bootstrap["items"]),
            "ready_count": 3,
            "blocked_count": 1,
            "ready_actions": [create_row["action"], update_row["action"], excluded_row["action"]],
            "blocked_never_selectable": blocked_checkbox.is_disabled(),
            "blocked_error_codes": sorted(issue["code"] for issue in blocked_row["errors"]),
            "excluded_ready_row_unchecked_before_post": not excluded_checkbox.is_checked(),
            "review_api_pages": state["review_api_pages"],
            "review_versions": {str(key): value for key, value in review_versions.items()},
            "operation_count_before_confirmation": 0,
            "business_write_count_during_review": 0,
            "price_context": {
                "linked_current": price_context["current"],
                "after_send": price_context["after_send"],
                "current_old_price": price_context["current_old_price"],
                "after_send_old_price": price_context["after_send_old_price"],
                "will_change": price_context["will_change"],
                "old_price_will_change": price_context["old_price_will_change"],
                "comparison_visible_in_card_and_confirmation": True,
                "old_price_comparison_visible_in_card_confirmation_and_no_script": True,
                "calculated_intermediate_not_shown": True,
                "mobile_pair_kept_inside_card": True,
                "no_script_pair_matches": True,
                "confirmation_left_unchecked": True,
            },
            "mobile_viewport": {"width": 320, "height": 780, "batch_button_visible": True,
                                "modal_visible": review_dialog.is_visible(),
                                "screenshot": mobile_screenshot.name},
        })
        passed("read_only_exact_mixed_review_includes_create_update_and_excludes_stale_ready_at_mobile_width")

        # Exercise the existing row checkbox before the independent final
        # confirmation checkbox. A change must clear confirmation each time.
        update_checkbox = review_dialog.get_by_role(
            "checkbox", name=f"Отправить {source_titles['update']}", exact=True,
        )
        create_checkbox = review_dialog.get_by_role(
            "checkbox", name=f"Отправить {source_titles['create']}", exact=True,
        )
        assert update_checkbox.is_checked() and create_checkbox.is_checked()
        update_checkbox.uncheck()
        send_button = review_dialog.get_by_role(
            "button", name="Отправить проверенные карточки", exact=True,
        )
        assert send_button.is_disabled()
        update_checkbox.check()
        confirm_checkbox = review_dialog.locator(".our-confirm input")
        assert not confirm_checkbox.is_checked()
        assert send_button.is_disabled()

        with app.app_context():
            assert MarketplaceOperation.query.filter_by(
                seller_id=fixture["seller_id"], account_id=account_id,
            ).count() == 0
        assert len(business_write_events) == review_write_marker, business_write_events[review_write_marker:]

        confirm_checkbox.check()
        assert send_button.is_enabled()
        confirm_checkbox_used = confirm_checkbox.is_checked()
        assert confirm_checkbox_used is True
        with page.expect_response(lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path == "/marketplaces/ozon/uploads/from-drafts"
        )) as reviewed_response_info:
            send_button.click()
        reviewed_response = reviewed_response_info.value
        assert reviewed_response.status == 202
        assert state["queue_confirmed"] is True
        assert state["reviewed_post"]
        assert set(state["reviewed_post"]["draft_ids"]) == set(ready_draft_ids)
        assert blocked_row["draft_id"] not in state["reviewed_post"]["draft_ids"]
        assert state["reviewed_post"]["expected_versions"] == {
            str(key): value for key, value in review_versions.items()
        }
        assert state["reviewed_worker_stats"]["enqueued"] == 2
        assert state["queued_operation_kinds"] == ["product_import", "product_update"]
        page.wait_for_url(f"**/marketplaces/ozon/uploads/{state['reviewed_run_uid']}")

        report["review"].update({
            "final_selected_draft_ids": state["reviewed_post"]["draft_ids"],
            "exact_version_evidence": [
                {
                    "draft_id": key,
                    "review_version": review_versions[key],
                    "posted_version": state["reviewed_post"]["expected_versions"][str(key)],
                    "matches": review_versions[key] == state["reviewed_post"]["expected_versions"][str(key)],
                }
                for key in state["reviewed_post"]["draft_ids"]
            ],
            "confirm_checkbox_used": confirm_checkbox_used,
            "explicit_queue_confirmation_post": True,
        })
        report["reviewed_queue"].update({
            "worker_stats": state["reviewed_worker_stats"],
            "operation_count": 2,
            "operation_kinds": state["queued_operation_kinds"],
            "operation_statuses": state["queued_operation_statuses"],
            "provider_dispatch_calls": report["provider_dispatch_calls"],
            "provider_attempts": report["provider_attempts"],
        })
        assert report["provider_dispatch_calls"] == 0
        assert report["provider_attempts"] == 0
        assert not report["unexpected_external"]
        assert not report["unexpected_http"]
        assert not report["js_errors"]
        passed("explicit_reviewed_queue_creates_exactly_one_create_and_one_update_without_provider_dispatch")
        run_recovery_browser_journey(page, browser, context)
        run_selected_draft_journey(page)
        run_mixed_type_selection_journey(page)
        run_review_editor_navigation_journey(page)
        run_brand_search_picker_journey(context)
        run_package_confirmation_bulk_journey(context)
        browser.close()


try:
    original_poll_operation = MarketplacePublicationService.poll_operation

    def forbid_dispatch(*_args, **_kwargs):
        report["provider_dispatch_calls"] += 1
        raise AssertionError("Provider dispatch is forbidden in this review harness")

    MarketplacePublicationService.poll_operation = forbid_dispatch
    run_browser_journey()
    report["status"] = "passed"
except Exception as error:
    report["status"] = "failed"
    report["error_type"] = type(error).__name__
    report["error"] = str(error)[:1200]
    report["safe_exception_frames"] = [
        {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for frame in traceback.extract_tb(error.__traceback__)[-12:]
    ]
    raise
finally:
    try:
        MarketplacePublicationService.poll_operation = original_poll_operation
    except NameError:
        pass
    try:
        event.remove(db.engine, "before_cursor_execute", record_business_write)
    except Exception:
        pass
    try:
        server.shutdown()
        server_thread.join(timeout=5)
    except Exception:
        pass
    (OUT / "ozon-bulk-ready-review-browser.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "artifact": str(OUT / "ozon-bulk-ready-review-browser.json"),
        "status": report["status"],
        "checks": report["checks"],
        "provider_attempts": report["provider_attempts"],
        "operation_count": report.get("reviewed_queue", {}).get("operation_count", 0),
    }), flush=True)
