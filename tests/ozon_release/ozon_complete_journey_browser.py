"""Synthetic end-to-end Ozon draft-to-publication browser contract.

The browser uses the real local Flask routes, CSRF, Vue screens, durable
preparation/upload workers, publication service and readback. Only AI transport
and the marketplace adapter are synthetic; the process has no external network.
"""

import base64
from contextlib import ExitStack
from datetime import datetime
from decimal import Decimal
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
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from werkzeug.serving import make_server

OUT = Path(os.environ.get("OZON_BROWSER_ARTIFACTS", "/artifacts"))
OUT.mkdir(parents=True, exist_ok=True)
ASSETS = Path(__file__).with_name("assets")
ASSET_MANIFEST = json.loads((ASSETS / "manifest.json").read_text())
for _asset in ASSET_MANIFEST.values():
    assert hashlib.sha256((ASSETS / _asset["file"]).read_bytes()).hexdigest() == _asset["sha256"]
report = {
    "status": "running",
    "checks": [],
    "layouts": [],
    "js_errors": [],
    "unexpected_http": [],
    "external": [],
    "provider_attempts": 0,
    "safe_exception_frames": [],
    "ai_create_response": {},
    "synthetic": {},
    "action_error_focus": {},
    "action_failure_injection": {
        "requests": 0,
        "expected_responses": 0,
        "blocked_repeats": 0,
    },
    "mobile_ozon_photo_touch_observations": [],
    "scope": "synthetic_full_ozon_draft_to_publication",
}
temporary = tempfile.TemporaryDirectory(prefix="ozon-complete-browser-")
temporary_root = Path(temporary.name)
os.environ.update(
    DATABASE_URL="sqlite:///" + str(temporary_root / "app.sqlite"),
    SKIP_SCHEDULER="1",
    IMAGE_LAB_INLINE_WORKER="0",
    SECRET_KEY="synthetic-ozon-complete-browser-session",
    ENCRYPTION_KEY=base64.urlsafe_b64encode(b"0" * 32).decode("ascii"),
    MARKETPLACE_OZON_ENABLED="1",
    MARKETPLACE_OZON_PUBLICATION_ENABLED="1",
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED="0",
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED="0",
    OZON_RATE_LIMIT_DIR=str(temporary_root / "limits"),
    MARKETPLACE_IMAGE_ASSET_DIR=str(temporary_root / "assets"),
    PUBLIC_BASE_URL="https://seller.test",
)


def no_network(*_args, **_kwargs):
    report["provider_attempts"] += 1
    raise AssertionError("External Python network is forbidden in this fixture")


requests.sessions.Session.request = no_network
socket.create_connection = no_network

from seller_platform import app
from models import (
    ImportedProduct,
    MarketplaceAttributeDefinition,
    MarketplaceOperation,
    MarketplaceProductDraft,
    MarketplaceProductType,
    Product,
    Seller,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_drafts import MarketplaceDraftService
from services.marketplace_image_assets import (
    _store_jpeg,
    public_asset_url,
    resolve_public_asset,
)
from services.marketplace_publications import MarketplacePublicationService
from services.ozon_bulk_upload import OzonBulkUploadService
from services.ozon_product_import import OzonProductImportContract
from services.ozon_draft_ai_transport import FlashOutcome, FlashUsage
from services import ozon_draft_ai_worker
from tests.ozon_release.seed import PASSWORD, USERNAME, seed
from tests.test_ozon_complete_journey import (
    ImmediateExecutor,
    OzonCompleteJourneyTest,
    SyntheticOzonProvider,
)
from tests.test_marketplace_publications import OzonPublicationFixture, SYNTHETIC_CREDENTIALS


app.config.update(
    TESTING=True,
    WTF_CSRF_ENABLED=True,
    SESSION_COOKIE_SECURE=False,
    SECRET_KEY=os.environ["SECRET_KEY"],
    PUBLIC_BASE_URL="https://seller.test",
    MARKETPLACE_IMAGE_ASSET_DIR=os.environ["MARKETPLACE_IMAGE_ASSET_DIR"],
    MARKETPLACE_OZON_ENABLED=True,
    MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=False,
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=False,
    OZON_RATE_LIMIT_DIR=os.environ["OZON_RATE_LIMIT_DIR"],
)
fixture = seed(app)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

def install_local_asset():
    from io import BytesIO
    from PIL import Image

    image = BytesIO()
    Image.new("RGB", (640, 640), (177, 128, 45)).save(image, format="JPEG", quality=90)
    with app.app_context():
        digest = _store_jpeg(image.getvalue(), config=app.config)
        return public_asset_url(
            config=app.config,
            secret_key=app.config["SECRET_KEY"],
            digest=digest,
        )


photo_url = install_local_asset()
draft_id = fixture["draft_id"]
account_id = fixture["account_id"]
source_id = fixture["source_id"]
product_type_id = None
legacy_package_confirmation_draft_id = None


def configure_fixture():
    with app.app_context():
        account = db.session.get(SellerMarketplaceAccount, account_id)
        account.external_account_id = SYNTHETIC_CREDENTIALS.external_account_id
        account.set_credentials({
            "client_id": SYNTHETIC_CREDENTIALS.external_account_id,
            "api_key": SYNTHETIC_CREDENTIALS.api_key,
        })
        source = db.session.get(ImportedProduct, source_id)
        source.title = "Безопасный товар янтарного цвета"
        source.description = "Цвет: янтарный. Поверхность: гладкая."
        source.original_data = json.dumps({
            "title": source.title,
            "description": source.description,
        }, ensure_ascii=False)
        source.photo_urls = json.dumps([photo_url])
        product_type = db.session.get(
            MarketplaceProductType,
            db.session.get(MarketplaceProductDraft, draft_id).product_type_id,
        )
        for external_id, name in (("909001", "Цвет"), ("909002", "Фактура")):
            db.session.add(MarketplaceAttributeDefinition(
                marketplace_id=product_type.marketplace_id,
                product_type_id=product_type.id,
                external_attribute_id=external_id,
                name=name,
                data_type="String",
                is_required=False,
                is_collection=False,
                max_value_count=1,
                is_available=True,
                is_enabled=True,
                last_seen_at=datetime.utcnow(),
            ))
        product_type.attributes_count += 2
        foreign_account = db.session.get(
            SellerMarketplaceAccount, fixture["foreign_account_id"],
        )
        foreign_seller = db.session.get(Seller, foreign_account.seller_id)
        foreign_source = ImportedProduct(
            seller_id=foreign_seller.id,
            external_id="foreign-source",
            external_vendor_code="private-offer",
            source_type="synthetic",
            title="Foreign private title",
            description="Private synthetic source",
            category="Посуда",
            photo_urls="[]",
        )
        db.session.add(foreign_source)
        db.session.flush()
        foreign_draft = MarketplaceDraftService.create_draft(
            seller_id=foreign_seller.id,
            account_id=foreign_account.id,
            imported_product_id=foreign_source.id,
            product_type_id=product_type.id,
        )
        new_source = ImportedProduct(
            seller_id=fixture["seller_id"],
            external_id="journey-clean-source",
            external_vendor_code="journey-clean-offer",
            source_type="synthetic",
            title="Новая карточка янтарного цвета",
            description="Цвет: янтарный. Поверхность: гладкая.",
            category="Посуда",
            original_data=json.dumps({
                "title": "Новая карточка янтарного цвета",
                "description": "Цвет: янтарный. Поверхность: гладкая.",
            }, ensure_ascii=False),
            photo_urls=json.dumps([photo_url]),
        )
        stale_source = ImportedProduct(
            seller_id=fixture["seller_id"],
            external_id="journey-stale-ready-source",
            external_vendor_code="journey-stale-ready-offer",
            source_type="synthetic",
            title="Карточка с устаревшей проверкой",
            description="Синтетическая карточка для проверки текущей готовности.",
            category="Посуда",
            original_data=json.dumps({
                "title": "Карточка с устаревшей проверкой",
                "description": "Синтетическая карточка для проверки текущей готовности.",
            }, ensure_ascii=False),
            photo_urls=json.dumps([photo_url]),
        )
        retry_prepared_source = ImportedProduct(
            seller_id=fixture["seller_id"],
            external_id="journey-retry-prepared-source",
            external_vendor_code="journey-retry-prepared-offer",
            source_type="synthetic",
            title="Полностью заполненная тестовая карточка",
            description="Сохранённые синтетические сведения для проверки повтора.",
            category="Посуда",
            original_data=json.dumps({
                "title": "Полностью заполненная тестовая карточка",
                "description": "Сохранённые синтетические сведения для проверки повтора.",
                "dimensions": {
                    "Ширина упаковки, мм": 200,
                    "Высота упаковки, мм": 30,
                    "Длина упаковки, мм": 300,
                    "Вес упаковки, г": 250,
                },
            }, ensure_ascii=False),
            photo_urls=json.dumps([photo_url]),
        )
        supplier_rrp_source = ImportedProduct(
            seller_id=fixture["seller_id"],
            external_id="journey-supplier-rrp-only-source",
            external_vendor_code="journey-supplier-rrp-only-offer",
            source_type="synthetic",
            title="Товар с рекомендованной ценой поставщика",
            description="Синтетическая проверка отдельной цены продавца.",
            category="Посуда",
            original_data=json.dumps({
                "title": "Товар с рекомендованной ценой поставщика",
                "description": "Синтетическая проверка отдельной цены продавца.",
                "dimensions": {
                    "Ширина упаковки, мм": 200,
                    "Высота упаковки, мм": 30,
                    "Длина упаковки, мм": 300,
                    "Вес упаковки, г": 250,
                },
                "recommended_retail_price": 1050,
            }, ensure_ascii=False),
            photo_urls=json.dumps([photo_url]),
            recommended_retail_price=None,
            calculated_price=None,
            calculated_price_before_discount=None,
        )
        legacy_wb_product = Product(
            seller_id=fixture["seller_id"],
            nm_id=90990001,
            vendor_code="journey-legacy-package-offer",
            title="Синтетический WB источник упаковки",
            dimensions_json=json.dumps({
                "isValid": True,
                "width": 20,
                "height": 3,
                "length": 30,
                "weightBrutto": 0.25,
            }),
        )
        legacy_package_source = ImportedProduct(
            seller_id=fixture["seller_id"],
            product=legacy_wb_product,
            external_id="journey-legacy-package-source",
            external_vendor_code="journey-legacy-package-offer",
            source_type="synthetic",
            title="Синтетический товар со старым WB набором упаковки",
            description="Сохранённый черновик для явного подтверждения упаковки.",
            category="Посуда",
            original_data=json.dumps({
                "title": "Синтетический товар со старым WB набором упаковки",
                "description": "Сохранённый черновик для явного подтверждения упаковки.",
            }, ensure_ascii=False),
            photo_urls="[]",
        )
        db.session.add_all([
            new_source, stale_source, retry_prepared_source,
            supplier_rrp_source, legacy_package_source,
        ])
        db.session.flush()
        supplier_rrp_draft = MarketplaceDraftService.create_draft(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            imported_product_id=supplier_rrp_source.id,
            product_type_id=product_type.id,
        )
        supplier_rrp_commercial = json.loads(
            supplier_rrp_draft.commercial_json or "{}"
        )
        assert supplier_rrp_commercial.get("price") in (None, ""), {
            "created_seller_price": supplier_rrp_commercial.get("price"),
            "supplier_rrp": json.loads(supplier_rrp_source.original_data).get(
                "recommended_retail_price"
            ),
        }
        supplier_rrp_draft.content_json = json.dumps({
            "name": supplier_rrp_source.title,
            "description": supplier_rrp_source.description,
        }, ensure_ascii=False)
        supplier_rrp_draft.media_json = json.dumps({
            "primary_image": photo_url,
            "images": [],
        })
        supplier_rrp_draft.dimensions_json = json.dumps({
            "width": "200", "height": "30", "depth": "300",
            "dimension_unit": "MILLIMETERS", "weight": "250",
            "weight_unit": "GRAMS",
        })
        supplier_rrp_draft.barcodes_json = json.dumps(["4600000000004"])
        supplier_rrp_draft.commercial_json = json.dumps({
            "vat": "0.22", "currency_code": "RUB",
        })
        legacy_package_draft = MarketplaceDraftService.create_draft(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            imported_product_id=legacy_package_source.id,
            product_type_id=product_type.id,
        )
        legacy_facts, legacy_provenance, _legacy_fact_hash = (
            MarketplaceDraftService._fact_snapshot(legacy_package_source)
        )
        wb_suggestion = legacy_facts["unverified_suggestions"][
            "wb_package_dimensions"
        ]
        legacy_dimensions = MarketplaceDraftService._wb_package_dimensions_to_ozon(
            wb_suggestion
        )
        assert legacy_dimensions == {
            "width": "200", "height": "30", "depth": "300",
            "weight": "250", "dimension_unit": "MILLIMETERS",
            "weight_unit": "GRAMS",
        }
        # Recreate the saved fact/provenance shape from before WB cache values
        # were demoted to suggestions. The production resolver must identify
        # this exact tuple as legacy and still require seller confirmation.
        legacy_facts["facts"].setdefault("physical", {})[
            "wb_package_dimensions"
        ] = wb_suggestion
        legacy_provenance["physical.wb_package_dimensions"] = {
            "source": "product.dimensions_json",
            "trust": "marketplace_observed",
        }
        legacy_package_draft.source_facts_json = (
            MarketplaceDraftService._canonical_json(legacy_facts, dict)
        )
        legacy_package_draft.provenance_json = (
            MarketplaceDraftService._canonical_json(legacy_provenance, dict)
        )
        legacy_package_draft.dimensions_json = (
            MarketplaceDraftService._canonical_json(legacy_dimensions, dict)
        )
        assert MarketplaceDraftService.package_confirmation(
            legacy_package_draft,
        )["source"] == "legacy_wb_unverified"
        assert MarketplaceDraftService.package_confirmation(
            legacy_package_draft,
        )["required"] is True
        retry_prepared_draft = MarketplaceDraftService.create_draft(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            imported_product_id=retry_prepared_source.id,
            product_type_id=product_type.id,
        )
        retry_prepared_draft.content_json = json.dumps({
            "name": retry_prepared_source.title,
            "description": retry_prepared_source.description,
        }, ensure_ascii=False)
        retry_prepared_draft.media_json = json.dumps({
            "primary_image": photo_url,
            "images": [],
        })
        retry_prepared_draft.dimensions_json = json.dumps({
            "width": "200", "height": "30", "depth": "300",
            "dimension_unit": "MILLIMETERS", "weight": "250",
            "weight_unit": "GRAMS",
        })
        retry_prepared_draft.barcodes_json = json.dumps(["4600000000002"])
        retry_prepared_draft.commercial_json = json.dumps({
            "price": "1000", "old_price": "1200", "vat": "0.22",
            "currency_code": "RUB",
        })
        db.session.commit()
        retry_prepared_draft = MarketplaceDraftService.validate_draft(
            seller_id=fixture["seller_id"],
            draft_id=retry_prepared_draft.id,
            expected_version=retry_prepared_draft.version,
        )
        assert retry_prepared_draft.status == "ready"
        assert MarketplaceDraftService._build_validation_result(
            retry_prepared_draft,
        )["publishable"] is True
        stale_draft = MarketplaceDraftService.create_draft(
            seller_id=fixture["seller_id"],
            account_id=account_id,
            imported_product_id=stale_source.id,
            product_type_id=product_type.id,
        )
        stale_draft.content_json = json.dumps({
            "name": stale_source.title,
            "description": stale_source.description,
        }, ensure_ascii=False)
        stale_draft.media_json = json.dumps({
            "primary_image": photo_url,
            "images": [],
        }, ensure_ascii=False)
        stale_draft.dimensions_json = "{}"
        stale_draft.commercial_json = json.dumps({
            "price": "1000",
            "currency_code": "RUB",
        })
        stale_draft.barcodes_json = "[]"
        stale_draft.status = "ready"
        stale_draft.validation_status = "valid"
        stale_draft.validation_result_json = json.dumps({
            "version": 1,
            "marketplace": "ozon",
            "publishable": True,
            "errors": [],
            "warnings": [],
            "validated_at": "2026-07-25T12:00:00",
        })
        stale_draft.validated_at = datetime(2026, 7, 25, 12, 0, 0)
        db.session.commit()
        current = MarketplaceDraftService._build_validation_result(stale_draft)
        current_fields = {item["field"] for item in current["errors"]}
        current_codes = {item["code"] for item in current["errors"]}
        assert current["publishable"] is False
        assert current_fields >= {
            "dimensions.width", "dimensions.height", "dimensions.depth",
            "dimensions.weight", "dimensions.dimension_unit", "dimensions.weight_unit",
            "commercial.vat",
        }, current
        assert current_codes >= {"physical_fact_required", "vat_required"}, current
        return (
            foreign_draft.id, new_source.id, product_type.id, stale_draft.id,
            retry_prepared_source.id, legacy_package_draft.id,
            supplier_rrp_source.id, supplier_rrp_draft.id,
        )


(
    foreign_draft_id, source_id, product_type_id, stale_ready_draft_id,
    retry_prepared_source_id, legacy_package_confirmation_draft_id,
    supplier_rrp_source_id, supplier_rrp_draft_id,
) = configure_fixture()


class SyntheticRegistry:
    def __init__(self, provider):
        self.provider = provider

    def get(self, code):
        assert code == "ozon"
        return self.provider


def flash_profile(*_args, **_kwargs):
    return SimpleNamespace(
        provider="deepseek",
        model="deepseek-flash",
        api_key="synthetic-ai-key",
        api_base_url="https://api.deepseek.com/v1",
        task_profile="seller_draft_completion_flash",
        max_retries=1,
        proxy_enabled=False,
    )


def flash_outcome(*_args, **_kwargs):
    return FlashOutcome(
        "success",
        content={"items": [{
            "draft_id": draft_id,
            "suggestions": [
                {
                    "attribute_id": "909001",
                    "complex_id": "0",
                    "group_ordinal": 0,
                    "values": [{"value": "янтарный"}],
                    "evidence": [{"path": "/description", "quote": "янтарный"}],
                    "provenance_code": "literal_source",
                },
                {
                    "attribute_id": "909002",
                    "complex_id": "0",
                    "group_ordinal": 0,
                    "values": [{"value": "гладкая"}],
                    "evidence": [{"path": "/description", "quote": "гладкая"}],
                    "provenance_code": "literal_source",
                },
            ],
        }]},
        http_status=200,
        usage=FlashUsage(prompt_tokens=100, completion_tokens=20),
    )


server = make_server("127.0.0.1", 0, app, threaded=True)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
base = "http://127.0.0.1:" + str(server.server_port)
provider = SyntheticOzonProvider()
registry = SyntheticRegistry(provider)
action_failure = {
    "armed": False,
    "path": None,
    "status": 503,
    "attempts": 0,
    "blocked_repeats": 0,
}


def browser_bridge(route):
    request = route.request
    parsed = urlsplit(request.url)
    if parsed.hostname == "127.0.0.1" and parsed.port == server.server_port:
        if (action_failure["armed"] and request.method == "POST"
                and parsed.path == action_failure["path"]):
            action_failure["attempts"] += 1
            report["action_failure_injection"]["requests"] = action_failure["attempts"]
            if action_failure["attempts"] == 1:
                route.fulfill(
                    status=action_failure["status"],
                    content_type="application/json",
                    body=json.dumps({
                        "success": False,
                        "error": "Синтетический сбой: результат действия не подтверждён.",
                    }, ensure_ascii=False),
                )
            else:
                # Keep any retry away from the local API/provider, but make the
                # duplicate visible to the assertion below.
                action_failure["blocked_repeats"] += 1
                report["action_failure_injection"]["blocked_repeats"] = action_failure["blocked_repeats"]
                route.abort(error_code="blockedbyclient")
            return
        response = route.fetch(max_redirects=0)
        if parsed.path == "/marketplaces/api/drafts/ai-completions" and request.method == "POST":
            try:
                document = response.json()
                report["ai_create_response"] = {
                    "status": response.status,
                    "code": document.get("code"),
                }
            except Exception:
                report["ai_create_response"] = {"status": response.status, "code": "non_json"}
        if response.status >= 500:
            report["unexpected_http"].append({"status": response.status, "path": parsed.path})
        route.fulfill(response=response)
        return
    if parsed.hostname == "seller.test" and parsed.path.startswith("/marketplace-assets/images/"):
        digest = Path(parsed.path).name.removesuffix(".jpg")
        signature = parse_qs(parsed.query).get("sig", [""])[0]
        try:
            asset = resolve_public_asset(
                digest=digest,
                signature=signature,
                config=app.config,
                secret_key=app.config["SECRET_KEY"],
            )
            route.fulfill(path=str(asset), content_type="image/jpeg")
        except Exception:
            report["unexpected_http"].append({"status": 404, "path": parsed.path})
            route.fulfill(status=404, body="")
        return
    manifest_asset = ASSET_MANIFEST.get(request.url) or ASSET_MANIFEST.get(request.url.rstrip("/"))
    if manifest_asset:
        route.fulfill(
            body=(ASSETS / manifest_asset["file"]).read_bytes(),
            content_type=manifest_asset["content_type"],
        )
        return
    if parsed.hostname == "ozon-fixture.test" and parsed.path == "/product.svg":
        route.fulfill(
            body=(b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320">'
                  b'<rect width="320" height="320" fill="#b18032"/></svg>'),
            content_type="image/svg+xml",
        )
        return
    report["external"].append({"host": parsed.hostname, "path": parsed.path})
    route.abort()


def passed(name):
    assert not report["js_errors"], report["js_errors"]
    assert not report["unexpected_http"], report["unexpected_http"]
    assert not report["external"], report["external"]
    report["checks"].append(name)
    print(json.dumps({"journey_check": name}), flush=True)


def action_error_focus_snapshot(page):
    """Capture safe keyboard/focus geometry for the expected error region."""
    return page.evaluate("""() => {
        const root = document.querySelector('#ozon-draft-editor');
        const region = root?.querySelector('.ode-action-error') || null;
        const fallback = root?.querySelector(':scope > .ode-message--error') || null;
        const visibleRegion = region || fallback;
        const recovery = visibleRegion?.querySelector('button') || null;
        const active = document.activeElement;
        const rect = element => {
            if (!element) return null;
            const value = element.getBoundingClientRect();
            return Object.fromEntries(['left','top','right','bottom','width','height'].map(key =>
                [key, Math.round(value[key] * 10) / 10]));
        };
        const hitTest = (owner, x, y) => {
            const hit = document.elementFromPoint(x, y);
            return {
                x:Math.round(x * 10) / 10,
                y:Math.round(y * 10) / 10,
                covered:!!owner && !!hit && (hit === owner || owner.contains(hit)),
                hit_tag:hit?.tagName || null,
                hit_id:hit?.id || null,
                hit_class:typeof hit?.className === 'string' ? hit.className.slice(0,120) : null,
            };
        };
        const visible = element => {
            if (!element) return false;
            const style = getComputedStyle(element);
            const value = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
                value.width > 0 && value.height > 0;
        };
        const inViewport = element => {
            const value = rect(element);
            return !!value && value.right > 0 && value.left < innerWidth &&
                value.bottom > 0 && value.top < innerHeight;
        };
        const focusStyle = active ? getComputedStyle(active) : null;
        const className = active && typeof active.className === 'string' ? active.className : '';
        const regionRect = visibleRegion?.getBoundingClientRect() || null;
        const firstEdgeY = regionRect ? regionRect.top + Math.min(12, regionRect.height / 2) : null;
        const firstEdgeHitTests = regionRect ? {
            left:hitTest(visibleRegion, regionRect.left + 8, firstEdgeY),
            center_near_top:hitTest(visibleRegion, (regionRect.left + regionRect.right) / 2, firstEdgeY),
            right:hitTest(visibleRegion, regionRect.right - 8, firstEdgeY),
        } : null;
        const recoveryRect = recovery?.getBoundingClientRect() || null;
        const recoveryButtonHitTest = recoveryRect ? hitTest(
            recovery, (recoveryRect.left + recoveryRect.right) / 2,
            (recoveryRect.top + recoveryRect.bottom) / 2,
        ) : null;
        return {
            expected_region_count:root?.querySelectorAll('.ode-action-error').length || 0,
            fallback_region_count:fallback ? 1 : 0,
            region_visible:visible(visibleRegion),
            region_in_viewport:inViewport(visibleRegion),
            region_rect:rect(visibleRegion),
            region_first_edge_hit_tests:firstEdgeHitTests,
            active_tag:active?.tagName || null,
            active_id:active?.id || null,
            active_class:className.slice(0,120),
            focus_matches_region:!!region && active === region,
            focus_visible:!!active?.matches?.(':focus-visible'),
            focus_outline_width:focusStyle ? parseFloat(focusStyle.outlineWidth) || 0 : 0,
            focus_outline_style:focusStyle?.outlineStyle || null,
            recovery_count:visibleRegion?.querySelectorAll('button').length || 0,
            recovery_visible:visible(recovery),
            recovery_in_viewport:inViewport(recovery),
            recovery_disabled:recovery?.disabled ?? null,
            recovery_focused:!!recovery && active === recovery,
            recovery_rect:rect(recovery),
            recovery_button_hit_test:recoveryButtonHitTest,
            viewport:{width:innerWidth,height:innerHeight},
            page_width:document.documentElement.scrollWidth,
        };
    }""")


def focus_error_view(page, theme):
    page.set_viewport_size({"width": 390, "height": 800})
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    page.wait_for_function("document.fonts.status === 'loaded'")
    page.wait_for_function(
        "!document.querySelector('.main-content')?.getAnimations().some(a => a.playState === 'running')",
        timeout=3000,
    )


def track_browser_response(response):
    if response.status < 500:
        return
    path = urlsplit(response.url).path
    expected = (
        action_failure["armed"]
        and path == action_failure["path"]
        and response.status == action_failure["status"]
        and action_failure["attempts"] == 1
    )
    if expected:
        report["action_failure_injection"]["expected_responses"] += 1
        return
    report["unexpected_http"].append({"status": response.status, "path": path})


def local_draft_commercial_state(target_draft_id):
    with app.app_context():
        db.session.expire_all()
        draft = db.session.get(MarketplaceProductDraft, target_draft_id)
        assert draft is not None
        return {
            "version": draft.version,
            "status": draft.status,
            "validation_status": draft.validation_status,
            "commercial": json.loads(draft.commercial_json or "{}"),
        }


def action_error_focus_check(context, page):
    """Exercise both failed editor actions through real browser controls."""
    config = bootstrap(page, "#ode-bootstrap")
    save_path = urlsplit(config["urls"]["save"]).path
    editor_url = base + f"/marketplaces/drafts/{draft_id}"
    focus_results = {}

    # Keep a dirty price in this tab while a second normal editor tab saves a
    # different value at the same draft version. The first tab then gets the
    # real optimistic-concurrency 409 through the production Flask route.
    focus_error_view(page, "light")
    page.get_by_role("button", name="Цена и упаковка", exact=True).click()
    page.locator("#ode-old-price").wait_for(state="visible")
    page.locator("#ode-old-price").fill("1400")
    concurrent = context.new_page()
    concurrent.set_default_timeout(20000)
    concurrent.on("pageerror", lambda error: report["js_errors"].append(str(error)))
    concurrent.goto(editor_url)
    concurrent.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
    concurrent.get_by_role("button", name="Цена и упаковка", exact=True).click()
    concurrent.locator("#ode-old-price").wait_for(state="visible")
    concurrent.locator("#ode-old-price").fill("1300")
    with concurrent.expect_response(lambda response:
            response.request.method == "POST" and
            urlsplit(response.url).path == save_path) as concurrent_save_info:
        concurrent.get_by_role("button", name="Сохранить", exact=True).click()
    concurrent_save = concurrent_save_info.value
    assert concurrent_save.status == 200, {
        "concurrent_save_status": concurrent_save.status,
        "path": urlsplit(concurrent_save.url).path,
    }
    concurrent.get_by_text(
        "Изменения сохранены. Теперь проверьте карточку перед отправкой.", exact=True,
    ).wait_for()
    saved_state = local_draft_commercial_state(draft_id)
    assert Decimal(str(saved_state["commercial"].get("old_price"))) == Decimal("1300"), {
        "saved_version": saved_state["version"],
        "saved_old_price_matches": False,
    }

    page.bring_to_front()
    save_posts = []
    page.on("request", lambda request: save_posts.append(request.url)
            if request.method == "POST" and urlsplit(request.url).path == save_path else None)
    save_button = page.locator(".ode-savebar button").filter(has_text="Сохранить")
    save_button.focus()
    save_focused_before = save_button.evaluate("node => node === document.activeElement")
    with page.expect_response(lambda response:
            response.request.method == "POST" and
            urlsplit(response.url).path == save_path) as conflict_info:
        page.keyboard.press("Enter")
    conflict_response = conflict_info.value
    page.locator("#ozon-draft-editor .ode-message--error").wait_for()
    page.wait_for_function("""() => {
        const banner = document.querySelector('#ozon-draft-editor .ode-message--error');
        const footer = document.querySelector('.ode-savebar strong');
        return !!banner && !!footer && !footer.textContent.includes('Выполняем действие');
    }""")
    dirty_value_after_conflict = page.locator("#ode-old-price").input_value()
    save_focus_result = action_error_focus_snapshot(page)
    page.keyboard.press("Tab")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    save_tab_result = action_error_focus_snapshot(page)
    save_recovery = page.get_by_role("button", name="Загрузить сохранённое", exact=True)
    save_recovery_visible = save_recovery.is_visible()
    save_recovery_enabled = save_recovery.is_enabled()
    recovery_confirm_message = "Загрузить сохранённую версию? Несохранённые правки в этой вкладке будут потеряны."
    recovery_dialogs = []
    recovery_editor_responses = []
    recovery_editor_gets = []
    save_focus_record = {
        "theme": "light",
        "width": 390,
        "height": 800,
        "http_status": conflict_response.status,
        "keyboard_enter_activated_save": save_focused_before,
        "dirty_old_price_preserved": dirty_value_after_conflict == "1400",
        "concurrent_saved_old_price_preserved": Decimal(str(
            saved_state["commercial"].get("old_price"),
        )) == Decimal("1300"),
        "posts_before_recovery": len(save_posts),
        "recovery_visible": save_recovery_visible,
        "recovery_enabled": save_recovery_enabled,
        "after_action": save_focus_result,
        "after_tab": save_tab_result,
        "recovery_dialogs": recovery_dialogs,
        "recovery_editor_gets": recovery_editor_gets,
    }
    focus_results["save_conflict"] = save_focus_record
    report["action_error_focus"] = focus_results

    def handle_recovery_dialog(dialog):
        observed = {"type": dialog.type, "message": dialog.message}
        recovery_dialogs.append(observed)
        if dialog.type == "confirm" and dialog.message == recovery_confirm_message:
            dialog.accept()
        else:
            dialog.dismiss()

    expected_editor_get_url = base + config["urls"]["editor"]

    def capture_recovery_editor_get(response):
        if response.request.method == "GET" and response.url == expected_editor_get_url:
            recovery_editor_responses.append(response)
            recovery_editor_gets.append({
                "method": response.request.method,
                "url": response.url,
                "status": response.status,
            })

    page.once("dialog", handle_recovery_dialog)
    page.on("response", capture_recovery_editor_get)
    if save_tab_result["recovery_focused"] and save_recovery_enabled:
        page.keyboard.press("Enter")
    else:
        # Let the baseline continue to the publication case even when it has
        # no action-error focus handoff; this is a GET readback, not a retry.
        save_recovery.click()
    assert recovery_dialogs == [{"type": "confirm", "message": recovery_confirm_message}], {
        "recovery_dialogs": recovery_dialogs,
    }
    page.wait_for_function("""() =>
        !document.querySelector('#ozon-draft-editor .ode-message--error') &&
        document.querySelector('#ode-old-price')?.value === '1300'
    """)
    page.remove_listener("response", capture_recovery_editor_get)
    assert len(recovery_editor_responses) == 1, recovery_editor_gets
    recovery_editor_response = recovery_editor_responses[0]
    assert recovery_editor_response.status == 200 and recovery_editor_response.url == expected_editor_get_url, {
        "status": recovery_editor_response.status,
        "url": recovery_editor_response.url,
        "expected_url": expected_editor_get_url,
    }
    recovery_editor_document = recovery_editor_response.json()
    assert recovery_editor_document["draft"]["id"] == draft_id
    recovered_editor_old_price = recovery_editor_document["documents"]["commercial"].get("old_price")
    assert Decimal(str(recovered_editor_old_price)) == Decimal("1300"), {
        "recovery_editor_old_price": recovered_editor_old_price,
    }
    save_focus_record["recovery_editor_old_price"] = str(recovered_editor_old_price)
    save_focus_record["recovery_ui_old_price"] = page.locator("#ode-old-price").input_value()
    save_posts_after_readback = len(save_posts)
    recovered_state = local_draft_commercial_state(draft_id)
    assert Decimal(str(recovered_state["commercial"].get("old_price"))) == Decimal("1300")
    concurrent.close()

    # Revalidate the locally recovered, unchanged valid fields before the
    # publication attempt. This is a local Seller Hub check only.
    validate = page.get_by_role("button", name="Проверить карточку", exact=True)
    validate.focus()
    page.keyboard.press("Enter")
    page.get_by_text(
        "Текущие поля проходят локальную проверку. При отправке данные будут проверены повторно.",
        exact=True,
    ).wait_for()
    publish_config = bootstrap(page, "#ode-bootstrap")
    publish_path = urlsplit(publish_config["urls"]["publish"]).path

    # Model one exact HTTP 503 at the browser boundary: it never reaches the
    # local route or Ozon adapter. Any repeat is also blocked and counted.
    focus_error_view(page, "dark")
    provider_before = {"writes": provider.physical_writes, "readbacks": len(provider.full_read_calls)}
    draft_before_failure = local_draft_commercial_state(draft_id)
    action_failure.update({"armed": True, "path": publish_path, "status": 503, "attempts": 0, "blocked_repeats": 0})
    publish_posts = []
    page.on("request", lambda request: publish_posts.append(request.url)
            if request.method == "POST" and urlsplit(request.url).path == publish_path else None)

    publish_button = page.locator(".ode-savebar button.sh-btn--primary")
    publish_button.focus()
    page.keyboard.press("Enter")
    dialog = page.locator('dialog[aria-labelledby="ode-publish-title"]')
    dialog.wait_for(state="visible")
    confirm_box = dialog.get_by_role("checkbox")
    confirm_box.focus()
    page.keyboard.press("Space")
    assert confirm_box.is_checked()
    confirm_button = dialog.get_by_role("button", name="Подтвердить отправку", exact=True)
    confirm_button.focus()
    with page.expect_response(lambda response:
            response.request.method == "POST" and
            urlsplit(response.url).path == publish_path) as publish_failure_info:
        page.keyboard.press("Enter")
    publish_failure = publish_failure_info.value
    page.locator("#ozon-draft-editor .ode-message--error").wait_for()
    page.wait_for_function("""() => {
        const dialog = document.querySelector('dialog[aria-labelledby="ode-publish-title"]');
        const banner = document.querySelector('#ozon-draft-editor .ode-message--error');
        const footer = document.querySelector('.ode-savebar strong');
        return !!dialog && !dialog.open && !!banner && !!footer &&
            !footer.textContent.includes('Выполняем действие');
    }""")
    publish_focus_result = action_error_focus_snapshot(page)
    page.keyboard.press("Tab")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    publish_tab_result = action_error_focus_snapshot(page)
    publish_recovery = page.get_by_role("button", name="Загрузить сохранённое", exact=True)
    publish_recovery_visible = publish_recovery.is_visible()
    publish_recovery_enabled = publish_recovery.is_enabled()
    provider_after = {"writes": provider.physical_writes, "readbacks": len(provider.full_read_calls)}
    draft_after_failure = local_draft_commercial_state(draft_id)

    save_focus_record["posts_after_recovery"] = save_posts_after_readback
    save_focus_record["concurrent_saved_old_price_preserved"] = Decimal(str(
        recovered_state["commercial"].get("old_price"),
    )) == Decimal("1300")
    focus_results["publication_unknown"] = {
        "theme": "dark",
        "width": 390,
        "height": 800,
        "http_status": publish_failure.status,
        "publish_post_count": len(publish_posts),
        "injected_request_count": action_failure["attempts"],
        "blocked_repeat_count": action_failure["blocked_repeats"],
        "dialog_closed": not dialog.evaluate("node => node.open"),
        "publish_remains_locked": publish_button.is_disabled(),
        "recovery_visible": publish_recovery_visible,
        "recovery_enabled": publish_recovery_enabled,
        "provider_before": provider_before,
        "provider_after": provider_after,
        "draft_unchanged": {
            "version": draft_after_failure["version"] == draft_before_failure["version"],
            "status": draft_after_failure["status"] == draft_before_failure["status"],
            "validation_status": draft_after_failure["validation_status"] == draft_before_failure["validation_status"],
            "old_price": Decimal(str(draft_after_failure["commercial"].get("old_price"))) ==
                Decimal(str(draft_before_failure["commercial"].get("old_price"))),
        },
        "after_action": publish_focus_result,
        "after_tab": publish_tab_result,
    }
    report["action_error_focus"] = focus_results

    # Evaluate both outcomes only after both failure cases have been captured,
    # so a baseline without the new selector still yields both diagnostics.
    for name in ("save_conflict", "publication_unknown"):
        result = focus_results[name]
        after_action = result["after_action"]
        after_tab = result["after_tab"]
        assert after_action["expected_region_count"] == 1, (name, result)
        assert after_action["focus_matches_region"], (name, result)
        assert after_action["region_visible"] and after_action["region_in_viewport"], (name, result)
        first_edge_hits = after_action["region_first_edge_hit_tests"]
        assert first_edge_hits and all(point["covered"] for point in first_edge_hits.values()), (name, result)
        assert after_action["focus_visible"] and after_action["focus_outline_width"] >= 2 and after_action["focus_outline_style"] != "none", (name, result)
        assert after_action["recovery_count"] == 1 and after_action["recovery_visible"] and not after_action["recovery_disabled"], (name, result)
        assert after_tab["recovery_focused"] and after_tab["recovery_in_viewport"], (name, result)
        assert after_tab["recovery_button_hit_test"] and after_tab["recovery_button_hit_test"]["covered"], (name, result)
        assert after_tab["focus_visible"] and after_tab["focus_outline_width"] >= 2 and after_tab["focus_outline_style"] != "none", (name, result)
    save_result = focus_results["save_conflict"]
    assert save_result["http_status"] == 409, save_result
    assert save_result["keyboard_enter_activated_save"] and save_result["dirty_old_price_preserved"], save_result
    assert save_result["concurrent_saved_old_price_preserved"] and save_result["posts_before_recovery"] == 1 and save_result["posts_after_recovery"] == 1, save_result
    publish_result = focus_results["publication_unknown"]
    assert publish_result["http_status"] == 503 and publish_result["publish_post_count"] == 1, publish_result
    assert publish_result["injected_request_count"] == 1 and publish_result["blocked_repeat_count"] == 0, publish_result
    assert publish_result["dialog_closed"] and publish_result["publish_remains_locked"], publish_result
    assert publish_result["provider_before"] == publish_result["provider_after"], publish_result
    assert all(publish_result["draft_unchanged"].values()), publish_result
    assert report["action_failure_injection"]["expected_responses"] == 1, report["action_failure_injection"]
    passed("failed_save_and_publication_focus_visible_recovery_without_repeat")


def bootstrap(page, selector):
    page.locator(selector).wait_for(state="attached")
    return page.locator(selector).evaluate("node => JSON.parse(node.textContent)")


def post_json(page, path, body, csrf):
    return page.evaluate(
        """async ({path,body,csrf}) => {
            const response = await fetch(path, {method:'POST', credentials:'same-origin',
              headers:{'Content-Type':'application/json','Accept':'application/json','X-CSRFToken':csrf},
              body:JSON.stringify(body), redirect:'manual'});
            let document = null;
            try { document = await response.json(); } catch (_) {}
            return {status:response.status, document};
        }""",
        {"path": path, "body": body, "csrf": csrf},
    )


def layout(page, name):
    for theme in ("light", "dark"):
        for width in (390, 1440):
            page.set_viewport_size({"width": width, "height": 980})
            page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
            page.wait_for_function("document.fonts.status === 'loaded'")
            page.wait_for_function(
                "!document.querySelector('.main-content')?.getAnimations().some(a => a.playState === 'running')",
                timeout=3000,
            )
            if not page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"):
                page.screenshot(
                    path=str(OUT / f"journey-overflow-{name}-{theme}-{width}.png"),
                    animations="disabled",
                )
                metrics = page.evaluate("""() => ({
                    viewport:innerWidth, page:document.documentElement.scrollWidth,
                    nodes:Array.from(document.querySelectorAll('*'))
                      .filter(node => node.getBoundingClientRect().right > innerWidth + 1)
                      .slice(0,10).map(node => ({tag:node.tagName,
                        className:String(node.className).slice(0,90),
                        right:Math.round(node.getBoundingClientRect().right),
                        width:Math.round(node.getBoundingClientRect().width),
                        scroll:node.scrollWidth,client:node.clientWidth}))
                })""")
                raise AssertionError((name, theme, width, "horizontal overflow", metrics))
            for dialog in page.locator("dialog[open]").all():
                assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1"), (
                    name, theme, width, "dialog overflow",
                )
            page.screenshot(
                path=str(OUT / f"journey-{name}-{theme}-{width}.png"),
                animations="disabled",
            )
            report["layouts"].append({"name": name, "theme": theme, "width": width})
    page.set_viewport_size({"width": 1440, "height": 980})
    page.evaluate("document.documentElement.dataset.theme = 'light'")


def set_theme_through_visible_control(page, theme):
    page.set_viewport_size({"width": 1440, "height": 900})
    current = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    if current != theme:
        toggle = page.locator('button[aria-label="Сменить тему"]:visible')
        assert toggle.count() == 1 and toggle.is_enabled(), "theme_control_unavailable"
        toggle.click()
        page.wait_for_function(
            "expected => document.documentElement.getAttribute('data-theme') === expected",
            arg=theme,
        )
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == theme, (
        "theme_control_did_not_apply",
    )


def measure_mobile_ozon_photo_touch_targets(page):
    """Measure actual editor controls without activating any media/save action."""
    observations = report["mobile_ozon_photo_touch_observations"]
    for theme in ("light", "dark"):
        set_theme_through_visible_control(page, theme)
        for width in (320, 360, 390):
            row = {
                "width_px": width,
                "theme": theme,
                "actual_theme": None,
                "document_overflow_px": None,
                "main_overflow_px": None,
                "visible_enabled_tabs": 0,
                "enabled_tab_targets": [],
                "enabled_tabs_min_width_px": None,
                "enabled_tabs_min_height_px": None,
                "photo_tab": None,
                "photo_tab_keyboard_reached": False,
                "photo_tab_focus_visible": False,
                "photo_tab_outline_px": 0.0,
                "photo_tab_outline_style_visible": False,
                "photo_tab_outline_inside_viewport": False,
                "visible_enabled_media_actions": 0,
                "media_actions_min_width_px": None,
                "media_actions_min_height_px": None,
                "media_action_keyboard_reached": False,
                "media_action_enabled": False,
                "media_action_focus_visible": False,
                "media_action_outline_px": 0.0,
                "media_action_outline_style_visible": False,
                "media_action_outline_inside_viewport": False,
                "status": "failed",
                "failure_code": None,
            }
            try:
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_function("document.fonts.status === 'loaded'")
                row["actual_theme"] = page.evaluate(
                    "() => document.documentElement.getAttribute('data-theme')"
                )

                tabs = page.locator("#ozon-draft-editor .ode-tabs button").filter(
                    has_text=re.compile(r"^Фотографии")
                )
                assert tabs.count() == 1 and tabs.is_visible(), "photo_tab_unavailable"
                tab = tabs.first
                enabled_tabs = page.locator(
                    "#ozon-draft-editor .ode-tabs button:visible:not([disabled])"
                )
                tab_targets = [
                    target.evaluate("""(el, index) => {
                        const r = el.getBoundingClientRect();
                        return {index, width_px:r.width, height_px:r.height,
                            enabled:!el.disabled && !el.hasAttribute('disabled')};
                    }""", index)
                    for index, target in enumerate(enabled_tabs.all())
                ]
                row["visible_enabled_tabs"] = len(tab_targets)
                row["enabled_tab_targets"] = tab_targets
                if tab_targets:
                    row["enabled_tabs_min_width_px"] = min(
                        item["width_px"] for item in tab_targets
                    )
                    row["enabled_tabs_min_height_px"] = min(
                        item["height_px"] for item in tab_targets
                    )
                row["photo_tab"] = tab.evaluate("""el => {
                    const r = el.getBoundingClientRect();
                    return {width_px:r.width, height_px:r.height,
                        enabled:!el.disabled && !el.hasAttribute('disabled')};
                }""")

                page.evaluate("document.activeElement instanceof HTMLElement && document.activeElement.blur()")
                found_tab = False
                for _ in range(240):
                    page.keyboard.press("Tab")
                    if tab.evaluate("el => document.activeElement === el"):
                        found_tab = True
                        break
                row["photo_tab_keyboard_reached"] = found_tab
                if found_tab:
                    tab_focus = tab.evaluate("""el => {
                        const r=el.getBoundingClientRect(),s=getComputedStyle(el);
                        const w=parseFloat(s.outlineWidth)||0,o=parseFloat(s.outlineOffset)||0;
                        return {visible:el.matches(':focus-visible'),outline_px:w,
                            outline_style_visible:s.outlineStyle!=='none' && s.outlineStyle!=='',
                            inside:r.left-w-o>=0 && r.top-w-o>=0 &&
                                r.right+w+o<=innerWidth && r.bottom+w+o<=innerHeight};
                    }""")
                    row["photo_tab_focus_visible"] = tab_focus["visible"]
                    row["photo_tab_outline_px"] = tab_focus["outline_px"]
                    row["photo_tab_outline_style_visible"] = tab_focus["outline_style_visible"]
                    row["photo_tab_outline_inside_viewport"] = tab_focus["inside"]

                actions = page.locator(
                    "#ode-section .ode-media-grid .ode-media-item button:visible:not([disabled])"
                )
                row["visible_enabled_media_actions"] = actions.count()
                sizes = [
                    action.evaluate("""el => {
                        const r=el.getBoundingClientRect();
                        return {width_px:r.width,height_px:r.height};
                    }""")
                    for action in actions.all()
                ]
                if sizes:
                    row["media_actions_min_width_px"] = min(x["width_px"] for x in sizes)
                    row["media_actions_min_height_px"] = min(x["height_px"] for x in sizes)
                    action = actions.first
                    page.evaluate("document.activeElement instanceof HTMLElement && document.activeElement.blur()")
                    found_action = False
                    for _ in range(240):
                        page.keyboard.press("Tab")
                        if action.evaluate("el => document.activeElement === el"):
                            found_action = True
                            break
                    row["media_action_keyboard_reached"] = found_action
                    if found_action:
                        action_focus = action.evaluate("""el => {
                            const r=el.getBoundingClientRect(),s=getComputedStyle(el);
                            const w=parseFloat(s.outlineWidth)||0,o=parseFloat(s.outlineOffset)||0;
                            return {enabled:!el.disabled,focus_visible:el.matches(':focus-visible'),
                                outline_px:w,outline_style_visible:s.outlineStyle!=='none' && s.outlineStyle!=='',
                                inside:r.left-w-o>=0 && r.top-w-o>=0 &&
                                    r.right+w+o<=innerWidth && r.bottom+w+o<=innerHeight};
                        }""")
                        row["media_action_focus_visible"] = action_focus["focus_visible"]
                        row["media_action_enabled"] = action_focus["enabled"]
                        row["media_action_outline_px"] = action_focus["outline_px"]
                        row["media_action_outline_style_visible"] = action_focus["outline_style_visible"]
                        row["media_action_outline_inside_viewport"] = action_focus["inside"]
                dimensions = page.evaluate("""() => {
                    const doc=document.documentElement,body=document.body;
                    const main=document.querySelector('#main-content') || body;
                    return {document:Math.max(0,doc.scrollWidth-innerWidth,body.scrollWidth-innerWidth),
                        main:Math.max(0,main.scrollWidth-main.clientWidth)};
                }""")
                row["document_overflow_px"] = dimensions["document"]
                row["main_overflow_px"] = dimensions["main"]
                row["status"] = "measured"

                assert row["actual_theme"] == theme, "theme_mismatch"
                assert row["visible_enabled_tabs"] > 0, "enabled_tabs_unavailable"
                assert all(
                    item["enabled"] and item["width_px"] >= 44 and item["height_px"] >= 44
                    for item in row["enabled_tab_targets"]
                ), "enabled_tab_below_44px"
                assert row["photo_tab"]["enabled"], "photo_tab_disabled"
                assert row["photo_tab"]["width_px"] >= 44 and row["photo_tab"]["height_px"] >= 44, "photo_tab_below_44px"
                assert row["photo_tab_keyboard_reached"] and row["photo_tab_focus_visible"], "photo_tab_keyboard_focus_missing"
                assert row["photo_tab_outline_px"] >= 2 and row["photo_tab_outline_style_visible"] and row["photo_tab_outline_inside_viewport"], "photo_tab_focus_ring_invalid"
                assert row["visible_enabled_media_actions"] > 0, "media_actions_unavailable"
                assert row["media_actions_min_width_px"] >= 44 and row["media_actions_min_height_px"] >= 44, "media_action_below_44px"
                assert row["media_action_keyboard_reached"] and row["media_action_enabled"] and row["media_action_focus_visible"], "media_action_keyboard_focus_missing"
                assert row["media_action_outline_px"] >= 2 and row["media_action_outline_style_visible"] and row["media_action_outline_inside_viewport"], "media_action_focus_ring_invalid"
                assert row["document_overflow_px"] <= 1 and row["main_overflow_px"] <= 1, "mobile_editor_root_overflow"
                row["status"] = "passed"
            except Exception as error:
                message = str(error)
                safe_codes = {
                    "photo_tab_unavailable", "theme_mismatch", "enabled_tabs_unavailable",
                    "enabled_tab_below_44px", "photo_tab_disabled", "photo_tab_below_44px",
                    "photo_tab_keyboard_focus_missing", "photo_tab_focus_ring_invalid", "media_actions_unavailable",
                    "media_action_below_44px", "media_action_keyboard_focus_missing",
                    "media_action_focus_ring_invalid", "mobile_editor_root_overflow",
                }
                row["status"] = "failed"
                row["failure_code"] = message if message in safe_codes else "measurement_failed"
                observations.append(row)
                raise AssertionError(("mobile_ozon_photo_touch_target", row["failure_code"], row)) from None
            observations.append(row)


def stale_ready_layout(page):
    """Reuse the journey layout check and add 320px/200% text coverage."""
    layout(page, "stale-ready-editor")  # 390 and desktop at both themes
    send_button = page.locator(".ode-savebar button.sh-btn--primary")
    for theme in ("light", "dark"):
        page.set_viewport_size({"width": 320, "height": 980})
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        page.wait_for_function("document.fonts.status === 'loaded'")
        page.wait_for_function(
            "!document.querySelector('.main-content')?.getAnimations().some(a => a.playState === 'running')",
            timeout=3000,
        )
        metrics = page.evaluate("""() => ({
            viewport:innerWidth, page:document.documentElement.scrollWidth,
            theme:document.documentElement.dataset.theme,
            status:document.querySelector('.ode-status')?.innerText || '',
            issueRows:document.querySelectorAll('.ode-issues--actionable li').length,
        })""")
        assert metrics["theme"] == theme and metrics["page"] <= 321, ("stale_ready_320_geometry", metrics)
        assert metrics["issueRows"] >= 5 and "ндс" in metrics["status"].lower(), metrics
        assert send_button.is_disabled(), ("stale_ready_320_send_enabled", theme)
        report["layouts"].append({
            "name": "stale-ready-editor", "width": 320, "theme": theme, "text_scale": 100,
        })
        page.screenshot(path=str(OUT / f"stale-ready-{theme}-320.png"), animations="disabled")

    for width in (320, 390, 1440):
        for theme in ("light", "dark"):
            page.set_viewport_size({"width": width, "height": 980})
            page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
            scale_state = page.locator("#ozon-draft-editor").evaluate("""root => {
                const nodes = Array.from(root.querySelectorAll('*')).filter(element =>
                    Array.from(element.childNodes).some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim())
                );
                const saved = nodes.map(element => ({element, value:element.style.getPropertyValue('font-size'),
                    priority:element.style.getPropertyPriority('font-size'), size:parseFloat(getComputedStyle(element).fontSize)}));
                const status = root.querySelector('.ode-status'), before = parseFloat(getComputedStyle(status).fontSize);
                window.__staleReadyTextScale = saved;
                saved.forEach(({element, size}) => element.style.setProperty('font-size', `${size * 2}px`, 'important'));
                return {count:nodes.length, before};
            }""")
            scaled = dict(scale_state)
            try:
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                scaled.update(page.evaluate("""() => {
                    const nodes = Array.from(document.querySelectorAll('#ozon-draft-editor *')).map(element => {
                        const rect = element.getBoundingClientRect();
                        const style = getComputedStyle(element);
                        const parent = element.parentElement?.getBoundingClientRect();
                        return {
                            tag:element.tagName, id:element.id || null,
                            className:typeof element.className === 'string' ? element.className.slice(0, 100) : null,
                            left:Math.round(rect.left), right:Math.round(rect.right), width:Math.round(rect.width),
                            scrollWidth:element.scrollWidth, clientWidth:element.clientWidth,
                            fontSize:style.fontSize, display:style.display, whiteSpace:style.whiteSpace,
                            overflowWrap:style.overflowWrap, overflowX:style.overflowX,
                            minWidth:style.minWidth, maxWidth:style.maxWidth,
                            parentLeft:parent ? Math.round(parent.left) : null,
                            parentRight:parent ? Math.round(parent.right) : null,
                            text:(element.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 100),
                        };
                    });
                    return {
                        after:parseFloat(getComputedStyle(document.querySelector('.ode-status')).fontSize),
                        page:document.documentElement.scrollWidth,
                        overflowing:nodes.filter(row => row.left < -1 || row.right > innerWidth + 1)
                            .sort((a, b) => b.right - a.right).slice(0, 24),
                        innerOverflowing:nodes.filter(row => row.scrollWidth > row.clientWidth + 1 &&
                            !['auto','scroll','hidden','clip'].includes(row.overflowX))
                            .sort((a, b) => b.scrollWidth - a.scrollWidth).slice(0, 24),
                    };
                }"""))
                if scaled["page"] > width + 1:
                    screenshot_name = f"stale-ready-text-200-overflow-{theme}-{width}.png"
                    page.screenshot(path=str(OUT / screenshot_name), animations="disabled")
                    scaled["overflow_screenshot"] = screenshot_name
                else:
                    screenshot_name = f"stale-ready-text-200-ok-{theme}-{width}.png"
                    page.screenshot(path=str(OUT / screenshot_name), animations="disabled")
                    scaled["layout_screenshot"] = screenshot_name
            finally:
                scaled["restored"] = page.evaluate("""() => {
                    const saved = window.__staleReadyTextScale || [];
                    saved.forEach(({element, value, priority}) => value
                        ? element.style.setProperty('font-size', value, priority)
                        : element.style.removeProperty('font-size'));
                    delete window.__staleReadyTextScale;
                    return saved.every(({element, value, priority}) =>
                        element.style.getPropertyValue('font-size') === value &&
                        element.style.getPropertyPriority('font-size') === priority);
                }""")
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            ratio = scaled["after"] / scaled["before"] if scaled["before"] else 0
            assert scaled["count"] and 1.99 <= ratio <= 2.01 and scaled["restored"], (width, theme, scaled)
            assert scaled["page"] <= width + 1, ("stale_ready_text_200_overflow", width, theme, scaled)
            assert send_button.is_disabled(), ("stale_ready_text_200_send_enabled", width, theme)
            layout_result = {
                "name": "stale-ready-editor-text-200", "width": width, "theme": theme, "text_scale": 200,
            }
            if scaled.get("layout_screenshot"):
                layout_result["screenshot"] = scaled["layout_screenshot"]
            report["layouts"].append(layout_result)
    page.set_viewport_size({"width": 1440, "height": 980})
    page.evaluate("document.documentElement.dataset.theme = 'light'")


def api_call_from_page(page, path, method="GET", body=None, csrf=None):
    return page.evaluate(
        """async ({path,method,body,csrf}) => {
            const headers = {'Accept':'application/json'};
            if (body !== null) headers['Content-Type'] = 'application/json';
            if (csrf) headers['X-CSRFToken'] = csrf;
            const response = await fetch(path, {method, credentials:'same-origin', headers,
              ...(body === null ? {} : {body:JSON.stringify(body)}), redirect:'manual'});
            let document = null;
            try { document = await response.json(); } catch (_) {}
            return {status:response.status, document};
        }""",
        {"path": path, "method": method, "body": body, "csrf": csrf},
    )


def legacy_package_confirmation_editor_check(context):
    page = context.new_page()
    page.set_default_timeout(20000)
    page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
    page.on("response", track_browser_response)
    page.goto(base + f"/marketplaces/drafts/{legacy_package_confirmation_draft_id}")
    page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
    config = bootstrap(page, "#ode-bootstrap")
    response = api_call_from_page(page, config["urls"]["editor"])
    assert response["status"] == 200, response
    document = response["document"]
    assert document["package_confirmation"]["required"] is True
    assert document["package_confirmation"]["source"] == "legacy_wb_unverified"
    expected_dimensions = {
        "width": "200", "height": "30", "depth": "300",
        "weight": "250", "dimension_unit": "MILLIMETERS",
        "weight_unit": "GRAMS",
    }
    assert document["documents"]["dimensions"] == expected_dimensions
    page.get_by_role("button", name="Цена и упаковка", exact=True).click()
    page.locator("[data-package-confirmation-needed]").wait_for(state="visible")
    confirmation = page.locator("#ode-package-confirm")
    assert confirmation.is_visible() and confirmation.is_enabled()
    assert not confirmation.is_checked()
    assert page.locator("#ode-width").input_value() == "200"
    assert page.locator("#ode-height").input_value() == "30"
    assert page.locator("#ode-depth").input_value() == "300"
    assert page.locator("#ode-weight").input_value() == "250"

    with app.app_context():
        draft = db.session.get(
            MarketplaceProductDraft, legacy_package_confirmation_draft_id,
        )
        version_before = draft.version
        dimensions_before = json.loads(draft.dimensions_json)
        operations_before = MarketplaceOperation.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id,
            draft_id=legacy_package_confirmation_draft_id,
        ).count()
    provider_writes_before = provider.physical_writes
    confirmation.click()
    assert confirmation.is_checked()
    page.get_by_text("Изменения не сохранены", exact=True).wait_for()
    save_button = page.get_by_role("button", name="Сохранить", exact=True)
    assert save_button.is_enabled()
    with page.expect_response(lambda item: (
        item.request.method == "POST"
        and urlsplit(item.url).path == config["urls"]["save"]
    )) as saved_response_info:
        save_button.click()
    saved_response = saved_response_info.value
    assert saved_response.status == 200
    save_payload = saved_response.request.post_data_json
    assert save_payload["expected_version"] == version_before
    assert save_payload["patch"]["confirm_package"] is True
    assert save_payload["patch"]["dimensions"] == expected_dimensions
    page.get_by_text("Все изменения сохранены", exact=True).wait_for()

    with app.app_context():
        saved_draft = db.session.get(
            MarketplaceProductDraft, legacy_package_confirmation_draft_id,
        )
        assert saved_draft.version > version_before
        assert json.loads(saved_draft.dimensions_json) == dimensions_before
        assert MarketplaceOperation.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id,
            draft_id=legacy_package_confirmation_draft_id,
        ).count() == operations_before

    page.reload()
    page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
    reopened_config = bootstrap(page, "#ode-bootstrap")
    reopened = api_call_from_page(page, reopened_config["urls"]["editor"])
    assert reopened["status"] == 200, reopened
    reopened_document = reopened["document"]
    assert reopened_document["package_confirmation"]["required"] is False
    assert reopened_document["package_confirmation"]["source"] == "seller_confirmed"
    assert reopened_document["documents"]["dimensions"] == expected_dimensions
    with app.app_context():
        assert MarketplaceOperation.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id,
            draft_id=legacy_package_confirmation_draft_id,
        ).count() == operations_before
    assert provider.physical_writes == provider_writes_before == 0
    assert not provider.full_read_calls
    passed(
        "legacy_package_confirmation_saves_exact_tuple_and_reopens_seller_confirmed_without_operations"
    )
    page.close()


def seller_price_source_separation_check(context):
    page = context.new_page()
    page.set_viewport_size({"width": 390, "height": 844})
    page.set_default_timeout(20000)
    page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
    page.on("response", track_browser_response)
    with app.app_context():
        source = db.session.get(ImportedProduct, supplier_rrp_source_id)
        raw_source = json.loads(source.original_data)
        assert raw_source.get("recommended_retail_price") == 1050
        assert source.calculated_price is None
        assert source.recommended_retail_price is None
        draft = db.session.get(MarketplaceProductDraft, supplier_rrp_draft_id)
        assert json.loads(draft.commercial_json).get("price") in (None, "")
        assert draft.status != "ready"
    writes_before = provider.physical_writes
    reads_before = len(provider.full_read_calls)

    page.goto(base + f"/marketplaces/drafts/{supplier_rrp_draft_id}")
    page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
    config = bootstrap(page, "#ode-bootstrap")
    response = api_call_from_page(page, config["urls"]["editor"])
    assert response["status"] == 200, response
    document = response["document"]
    assert document["draft"]["id"] == supplier_rrp_draft_id
    assert document["documents"]["commercial"].get("price") in (None, "")
    current = document["current_validation"]
    assert current["publishable"] is False, current
    price_errors = [
        item for item in current["errors"]
        if item.get("field") == "commercial.price"
    ]
    assert len(price_errors) == 1 and price_errors[0].get("code") == "price_required", current
    assert [item.get("code") for item in current["errors"]] == ["price_required"], current

    page.get_by_role("button", name="Цена и упаковка", exact=True).click()
    price_input = page.locator("#ode-price")
    assert price_input.input_value() == ""
    send_button = page.locator(".ode-savebar button.sh-btn--primary")
    assert send_button.count() == 1 and send_button.is_disabled()
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 844})
        currency = page.locator("#ode-currency").evaluate("""select => {
            const option = select.selectedOptions[0];
            const style = getComputedStyle(select);
            const canvas = document.createElement('canvas').getContext('2d');
            canvas.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
            const label = option.textContent.trim();
            const nativeArrowSpace = 20;
            const available = select.clientWidth - parseFloat(style.paddingLeft)
                - parseFloat(style.paddingRight) - nativeArrowSpace;
            return {value: option.value, label, fits: canvas.measureText(label).width <= available};
        }""")
        assert currency == {"value": "RUB", "label": "Рубль (₽)", "fits": True}, (width, currency)
    passed("seller_draft_currency_label_fits_at_320_and_390")
    assert page.evaluate("document.documentElement.scrollWidth === window.innerWidth")
    price_input.scroll_into_view_if_needed()
    page.screenshot(path=str(OUT / "ozon-seller-price-required-390.png"))

    price_input.fill("1050")
    save_button = page.get_by_role("button", name="Сохранить", exact=True)
    assert save_button.is_enabled()
    with page.expect_response(lambda item: (
        item.request.method in {"POST", "PATCH"}
        and urlsplit(item.url).path == config["urls"]["save"]
    )) as save_info:
        save_button.click()
    saved_response = save_info.value
    assert saved_response.status == 200, saved_response.status
    save_payload = saved_response.request.post_data_json
    assert save_payload["patch"]["commercial"]["price"] == "1050", save_payload
    page.get_by_text("Все изменения сохранены", exact=True).wait_for()

    saved = api_call_from_page(page, config["urls"]["editor"])
    assert saved["status"] == 200, saved
    assert saved["document"]["documents"]["commercial"]["price"] == "1050", saved
    with app.app_context():
        saved_draft = db.session.get(MarketplaceProductDraft, supplier_rrp_draft_id)
        assert json.loads(saved_draft.commercial_json)["price"] == "1050"
        saved_source = db.session.get(ImportedProduct, supplier_rrp_source_id)
        assert json.loads(saved_source.original_data)["recommended_retail_price"] == 1050
        assert saved_source.calculated_price is None

    page.reload()
    page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
    reopened_config = bootstrap(page, "#ode-bootstrap")
    assert page.locator("#ode-price").input_value() == "1050"
    reopened = api_call_from_page(page, reopened_config["urls"]["editor"])
    assert reopened["status"] == 200, reopened
    reopened_document = reopened["document"]
    assert reopened_document["documents"]["commercial"]["price"] == "1050"
    reopened_validation = reopened_document["current_validation"]
    assert not [
        item for item in reopened_validation["errors"]
        if item.get("field") == "commercial.price"
    ], reopened_validation
    page.get_by_role("button", name="Проверить карточку", exact=True).click()
    page.get_by_text(
        "Текущие поля проходят локальную проверку. При отправке данные будут проверены повторно.",
        exact=True,
    ).wait_for()
    validated = api_call_from_page(page, reopened_config["urls"]["editor"])
    assert validated["status"] == 200, validated
    assert validated["document"]["current_validation"]["publishable"] is True
    assert validated["document"]["current_validation"]["errors"] == []
    assert page.locator(".ode-savebar button.sh-btn--primary").is_enabled()
    page.get_by_role("button", name="Цена и упаковка", exact=True).click()
    assert page.evaluate("document.documentElement.scrollWidth === window.innerWidth")
    page.locator("#ode-price").scroll_into_view_if_needed()
    page.screenshot(path=str(OUT / "ozon-seller-price-saved-390.png"))
    with app.app_context():
        local_operations = MarketplaceOperation.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id,
            draft_id=supplier_rrp_draft_id,
        ).count()
    assert local_operations == 0, local_operations
    assert provider.physical_writes == writes_before
    assert len(provider.full_read_calls) == reads_before
    report["seller_price_source_separation"] = {
        "supplier_rrp": 1050,
        "calculated_price": None,
        "initial_editor_price": "",
        "initial_only_blocker": "price_required",
        "publish_enabled_before_seller_price": False,
        "seller_price_after_save_and_reopen": "1050",
        "local_publication_operations": local_operations,
        "provider_writes": 0,
        "provider_readbacks": 0,
    }
    passed("supplier_rrp_does_not_prefill_seller_price_and_manual_price_persists")
    page.close()


def linked_editor_price_comparison_check(context):
    with app.app_context():
        seller = db.session.get(Seller, fixture["seller_id"])
        account = db.session.get(SellerMarketplaceAccount, account_id)
        product_type = db.session.get(MarketplaceProductType, product_type_id)
        source = ImportedProduct(
            seller_id=seller.id, external_id="linked-price-source",
            external_vendor_code="linked-price-offer", source_type="synthetic",
            title="Карточка для проверки цены", description="Проверка цены связанной карточки.",
            category="Посуда", calculated_price=1250, calculated_price_before_discount=1500,
            photo_urls=json.dumps([photo_url]),
        )
        db.session.add(source)
        db.session.commit()
        draft = MarketplaceDraftService.create_draft(
            seller_id=seller.id, account_id=account.id, imported_product_id=source.id,
            product_type_id=product_type.id,
        )
        draft.dimensions_json = json.dumps({
            "width": "200", "height": "30", "depth": "300", "dimension_unit": "MILLIMETERS",
            "weight": "250", "weight_unit": "GRAMS",
        })
        draft.barcodes_json = json.dumps(["4600000000008"])
        draft.commercial_json = json.dumps({
            "price": "1250", "old_price": "1500", "currency_code": "RUB", "vat": "0.22",
        })
        db.session.commit()
        baseline = OzonProductImportContract.build_payload(draft)
        baseline["items"][0].update(price="1000", old_price="1300")
        listing_fixture = SimpleNamespace(
            seller=seller, account=account, marketplace=account.marketplace,
            product_type=product_type, source=source, draft=draft,
        )
        listing = OzonPublicationFixture.attach_listing(listing_fixture, baseline)
        target_id = draft.id
        listing_prices = listing.price_summary_json
    provider_before = (provider.physical_writes, len(provider.full_read_calls))
    page = context.new_page()
    page.set_viewport_size({"width": 390, "height": 844})
    page.set_default_timeout(20000)
    page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
    page.on("response", track_browser_response)
    page.goto(base + f"/marketplaces/drafts/{target_id}")
    page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
    config = bootstrap(page, "#ode-bootstrap")

    def read_editor():
        result = api_call_from_page(page, config["urls"]["editor"])
        assert result["status"] == 200
        return result["document"]

    def save_editor():
        with page.expect_response(lambda response: (
            response.request.method == "POST" and urlsplit(response.url).path == config["urls"]["save"]
        )) as saved:
            page.get_by_role("button", name="Сохранить", exact=True).click()
        assert saved.value.status == 200, saved.value.status
        page.get_by_text("Все изменения сохранены", exact=True).wait_for()
        return saved.value.request.post_data_json["patch"]

    initial = read_editor()
    assert initial["price_context"] == {
        "current": "1000", "calculated": "1250", "after_send": "1000",
        "currency_code": "RUB", "will_change": False,
        "current_old_price": "1300", "after_send_old_price": "1300", "old_price_will_change": False,
    }
    page.locator("#ode-name").fill("Обновлённое название связанной карточки")
    unrelated = save_editor()
    assert "use_calculated_price" not in unrelated and "commercial" not in unrelated
    assert read_editor()["price_context"] == initial["price_context"]

    page.get_by_role("button", name="Цена и упаковка", exact=True).click()
    comparison = page.locator("[data-linked-price-context]")
    assert comparison.is_visible()
    assert comparison.locator("[data-ozon-current-price]").inner_text() == "1\u00a0000\u00a0₽"
    assert "1\u00a0250\u00a0₽" in comparison.inner_text()
    assert page.locator("#ode-price").input_value() == "1000"
    assert page.locator("#ode-old-price").input_value() == "1300"
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth === window.innerWidth")
    comparison.screenshot(path=str(OUT / "ozon-linked-price-choice-390.png"))
    choose_price = page.get_by_role("button", name="Подставить расчётную цену", exact=True)
    choose_price.focus()
    choose_price.press("Enter")
    page.wait_for_function("document.activeElement?.id === 'ode-price'")
    assert page.locator("#ode-price").input_value() == "1250"
    assert page.locator("#ode-old-price").input_value() == "1300"
    selected = save_editor()
    assert selected["use_calculated_price"] is True
    assert selected["commercial"]["price"] == "1250"
    assert selected["commercial"]["old_price"] == "1300"
    page.reload()
    page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
    result = read_editor()
    assert result["price_context"] == {**initial["price_context"], "after_send": "1250", "will_change": True}
    assert result["documents"]["commercial"]["old_price"] == "1300"
    page.get_by_role("button", name="Цена и упаковка", exact=True).click()
    page.locator("#ode-old-price").fill("1500")
    old_price_edit = save_editor()
    assert old_price_edit["confirm_old_price_edit"] is True
    assert "use_calculated_price" not in old_price_edit
    assert old_price_edit["commercial"]["old_price"] == "1500"
    page.reload()
    page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
    result = read_editor()
    assert result["price_context"]["current_old_price"] == "1300"
    assert result["price_context"]["after_send_old_price"] == "1500"
    assert result["price_context"]["old_price_will_change"] is True
    page.get_by_role("button", name="Цена и упаковка", exact=True).click()
    page.locator("#ode-price").scroll_into_view_if_needed()
    page.screenshot(path=str(OUT / "ozon-linked-price-saved-390.png"))
    with app.app_context():
        saved = db.session.get(MarketplaceProductDraft, target_id)
        assert saved.published_listing.price_summary_json == listing_prices
        assert MarketplaceOperation.query.filter_by(
            seller_id=fixture["seller_id"], account_id=account_id, draft_id=target_id,
        ).count() == 0
    assert provider_before == (provider.physical_writes, len(provider.full_read_calls))
    report["linked_price_editor"] = {"title_only_preserves_price": True,
                                     "explicit_price_choice_persists": True,
                                     "old_price_preserved_until_explicit_edit": True, "local_operations": 0}
    passed("linked_editor_compares_exact_prices_and_saves_only_explicit_choice")
    page.close()


def stale_ready_editor_check(page):
    assert provider.physical_writes == 0 and not provider.full_read_calls
    console_messages = []

    def capture_editor_console(message):
        if message.type in {"error", "warning"} and len(console_messages) < 12:
            safe_text = re.sub(r"\?[^\s]*", "?[redacted]", message.text)
            console_messages.append({"type": message.type, "text": safe_text[:400]})

    page.on("console", capture_editor_console)
    page.goto(base + f"/marketplaces/drafts/{stale_ready_draft_id}")
    page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
    config = bootstrap(page, "#ode-bootstrap")
    editor_response = api_call_from_page(page, config["urls"]["editor"])
    if editor_response["status"] != 200:
        body = editor_response.get("document")
        raise AssertionError(("stale_ready_editor_api", {
            "status": editor_response["status"],
            "code": body.get("code") if isinstance(body, dict) else None,
            "error": str(body.get("error", ""))[:300] if isinstance(body, dict) else None,
        }))
    document = editor_response["document"]
    assert document["draft"]["status"] == "ready", document["draft"]
    assert document["draft"]["validation_status"] == "valid", document["draft"]
    assert document["draft"]["validation"]["publishable"] is True, document["draft"]
    assert document["draft"]["validated_at"].startswith("2026-07-25"), document["draft"]
    assert document["readiness"]["overall"] == "ready", document["readiness"]
    assert not document["baseline_error"], document["baseline_error"]
    current = document["current_validation"]
    current_codes = {item["code"] for item in current["errors"]}
    current_fields = {item["field"] for item in current["errors"]}
    assert current["publishable"] is False, current
    assert "vat_required" in current_codes, current
    assert {
        "dimensions.width", "dimensions.height", "dimensions.depth",
        "dimensions.weight", "dimensions.dimension_unit", "dimensions.weight_unit",
    } <= current_fields, current

    render_failure = None
    try:
        page.wait_for_function(
            "() => !!document.querySelector('.ode-status') || "
            "!!document.querySelector('#ozon-draft-editor .ode-message--error')"
        )
    except PlaywrightTimeoutError as error:
        render_failure = f"{type(error).__name__}: {error}"[:400]
    if render_failure or not page.locator(".ode-status").is_visible():
        diagnostic = page.evaluate("""() => {
            const root = document.querySelector('#ozon-draft-editor');
            const error = root?.querySelector('.ode-message--error');
            return {
                editor_present:!!root,
                editor_visible:root ? getComputedStyle(root).display !== 'none' : false,
                editor_cloaked:root?.hasAttribute('v-cloak') ?? null,
                workspace_present:!!root?.querySelector('.ode-workspace'),
                loading_present:!!root?.querySelector('.ode-loading'),
                status_present:!!root?.querySelector('.ode-status'),
                error_present:!!error,
                error_text:error?.innerText?.slice(0, 300) || null,
                root_text_length:root?.innerText?.length || 0,
                visible_text:root?.innerText?.slice(0, 800) || null,
                child_count:root?.children?.length || 0,
                top_children:Array.from(root?.children || []).slice(0, 12).map(node => ({
                    tag:node.tagName, id:node.id || null,
                    className:typeof node.className === 'string' ? node.className.slice(0, 100) : null,
                    display:getComputedStyle(node).display,
                })),
            };
        }""")
        screenshot = OUT / "stale-ready-editor-render-diagnostic.png"
        page.screenshot(path=str(screenshot), animations="disabled")
        report["stale_ready_editor_render_diagnostic"] = {
            "editor_api_status": editor_response["status"],
            "draft_id_matches": document["draft"].get("id") == stale_ready_draft_id,
            "bootstrap_draft_id_matches": config.get("draftId") == stale_ready_draft_id,
            "bootstrap_account_id_matches": config.get("accountId") == document["draft"].get("account_id"),
            "bootstrap_editor_url_path_matches": urlsplit(config["urls"]["editor"]).path
                == f"/marketplaces/drafts/{stale_ready_draft_id}/editor",
            "bootstrap_editor_url_has_query": bool(urlsplit(config["urls"]["editor"]).query),
            "wait_error": render_failure,
            "console_messages": console_messages,
            "dom": diagnostic,
            "screenshot": screenshot.name,
        }
        page.remove_listener("console", capture_editor_console)
        raise AssertionError(("stale_ready_editor_render", report["stale_ready_editor_render_diagnostic"]))
    page.remove_listener("console", capture_editor_console)

    send_button = page.locator(".ode-savebar button.sh-btn--primary")
    assert send_button.count() == 1 and send_button.is_disabled()
    status = page.locator(".ode-status").inner_text().lower()
    assert "упаковку" in status and "ндс" in status, status
    issue_rows = page.locator(".ode-issues--actionable li")
    assert issue_rows.count() >= 7
    assert "НДС" in issue_rows.all_inner_texts()[-1] or any(
        "НДС" in text for text in issue_rows.all_inner_texts()
    ), issue_rows.all_inner_texts()
    history_note = page.locator(".ode-readiness > p.ode-caption")
    assert history_note.count() == 1, history_note.count()
    history_text = history_note.inner_text()
    assert "Последняя сохранённая проверка: 25.07.2026" in history_text, history_text
    assert "Результат выше рассчитан по текущим полям." in history_text, history_text
    current_warning = page.locator("#ode-validation > p.ode-message--warning")
    assert current_warning.count() == 1, current_warning.count()
    assert "Текущая проверка нашла" in current_warning.inner_text(), current_warning.inner_text()
    assert page.locator("#ode-validation p.ode-message--success").count() == 0
    stale_ready_layout(page)

    focus_cases = (
        ("Ширина упаковки", "ode-width"),
        ("Выберите ставку НДС", "ode-vat"),
    )
    for issue_text, target_id in focus_cases:
        row = page.locator(".ode-issues--actionable li").filter(has_text=issue_text)
        assert row.count() == 1, (issue_text, row.count())
        row.get_by_role(
            "button",
            name=re.compile(r"Перейти к полю: " + re.escape(issue_text)),
        ).click()
        page.wait_for_function(
            "target => document.activeElement?.id === target",
            arg=target_id,
        )
        assert page.evaluate("() => document.activeElement?.id") == target_id
    assert page.locator(".ode-tabs button[aria-current='page']").inner_text() == "Цена и упаковка"

    page.locator("#ode-width").fill("100")
    assert page.locator(".ode-savebar strong").inner_text() == "Изменения не сохранены"
    assert send_button.is_disabled(), "a dirty stale-ready form must keep send disabled"
    save_button = page.locator(".ode-savebar button").filter(has_text="Сохранить")
    assert save_button.count() == 1 and save_button.is_enabled()
    save_button.click()
    page.get_by_text(
        "Изменения сохранены. Теперь проверьте карточку перед отправкой.",
        exact=True,
    ).wait_for()
    assert page.locator("#ode-width").input_value() == "100"
    assert send_button.is_disabled(), "local save must not turn a stale draft into a sendable draft"
    assert page.locator(".ode-issues--actionable li").count() >= 6
    saved_response = api_call_from_page(page, config["urls"]["editor"])
    assert saved_response["status"] == 200, saved_response
    saved_document = saved_response["document"]
    assert saved_document["documents"]["dimensions"]["width"] == "100"
    assert saved_document["current_validation"]["publishable"] is False
    assert saved_document["draft"]["status"] != "ready"
    assert send_button.is_disabled()
    assert provider.physical_writes == 0 and not provider.full_read_calls
    report["stale_ready_editor"] = {
        "stored_status_before_repair": "ready",
        "stored_validation_publishable_before_repair": True,
        "current_validation_publishable_before_repair": False,
        "current_error_codes_before_repair": sorted(current_codes),
        "historical_validation_date_visible": "25.07.2026",
        "send_enabled_before_repair": False,
        "send_enabled_after_local_save": False,
        "local_save_preserved_value": "100",
        "provider_writes": provider.physical_writes,
        "provider_readbacks": len(provider.full_read_calls),
    }
    passed("legacy_ready_snapshot_is_historical_and_current_errors_gate_send")
    passed("current_error_links_focus_fields_and_dirty_local_save_stays_blocked")
    page.goto(base + "/marketplaces/drafts/?account_id=" + str(account_id))
    page.locator("#ozon-drafts-list:not([v-cloak])").wait_for()


def seed_branch_counts():
    OzonCompleteJourneyTest.branch_counts = {
        "success": {"ai_calls": 0, "provider_writes": 0, "readbacks": 0},
        "invalid": {"ai_calls": 0, "provider_writes": 0},
        "repair": {"ai_calls": 0, "human_repairs": 0, "repreparations": 0},
        "clean_new_source": {
            "new_sources": 0,
            "prepared_drafts": 0,
            "category_selections": 0,
            "human_packaging_vat": 0,
            "fresh_validations": 0,
            "provider_writes": 0,
            "full_readbacks": 0,
        },
        "unknown": {
            "provider_writes": 0,
            "automatic_retries": 0,
            "quarantines": 0,
            "safe_reconciliations": 0,
        },
    }


def patcher_contexts():
    import services.marketplace_publications as publication_service
    import services.ozon_draft_ai_completion as ai_completion

    contexts = ExitStack()
    contexts.enter_context(patch.object(
        ai_completion, "resolve_config", side_effect=flash_profile,
    ))
    contexts.enter_context(patch.object(
        ozon_draft_ai_worker, "resolve_config", side_effect=flash_profile,
    ))
    contexts.enter_context(patch.object(
        ozon_draft_ai_worker, "flash_completion", side_effect=flash_outcome,
    ))
    original_accept = ai_completion.OzonDraftAICompletionService.accept

    def inspect_ai_accept(cls, **kwargs):
        try:
            return original_accept(**kwargs)
        except Exception as error:
            report["safe_exception_frames"] = [
                {"file": Path(frame.filename).name, "line": frame.lineno,
                 "function": frame.name}
                for frame in traceback.extract_tb(error.__traceback__)
            ]
            report["safe_exception_type"] = type(error).__name__
            raise

    contexts.enter_context(patch.object(
        ai_completion.OzonDraftAICompletionService,
        "accept", classmethod(inspect_ai_accept),
    ))
    contexts.enter_context(patch.object(
        publication_service, "get_marketplace_registry", return_value=registry,
    ))
    return contexts


try:
    with patcher_contexts():
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("OZON_BROWSER_CHROMIUM", "/usr/bin/chromium"),
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 980},
                service_workers="block",
                timezone_id="Europe/Moscow",
            )
            context.route("**/*", browser_bridge)
            page = context.new_page()
            page.set_default_timeout(20000)
            page.on("pageerror", lambda error: report["js_errors"].append(str(error)))
            page.on("response", track_browser_response)

            page.goto(base + "/login?next=/marketplaces/drafts/?account_id=" + str(account_id))
            page.locator("input[name=username]").fill(USERNAME)
            page.locator("input[name=password]").fill(PASSWORD)
            page.get_by_role("button", name="Войти", exact=True).click()
            page.goto(base + "/marketplaces/drafts/?account_id=" + str(account_id))
            list_config = bootstrap(page, "#odl-bootstrap")
            page.locator("#ozon-drafts-list:not([v-cloak])").wait_for()
            passed("real_login_and_draft_workspace")
            stale_ready_editor_check(page)
            seller_price_source_separation_check(context)
            linked_editor_price_comparison_check(context)
            legacy_package_confirmation_editor_check(context)

            csrf = list_config["csrf"]
            prepared = post_json(page, "/marketplaces/ozon/uploads/", {
                "account_id": account_id,
                "imported_product_ids": [source_id, retry_prepared_source_id],
                "confirm_prepare": True,
                "request_key": "complete-browser-prepare-000001",
            }, csrf)
            assert prepared["status"] == 202, prepared
            prepare_uid = prepared["document"]["run"]["job_uid"]
            parent_result_path = f"/marketplaces/ozon/uploads/{prepare_uid}"
            with app.app_context():
                owner_seller = db.session.get(Seller, fixture["seller_id"])
                workspace_owner = f"{owner_seller.user_id}:{owner_seller.id}"
            # This journey accepts preparation directly from the drafts page,
            # so seed the exact browser return capsule that the My Products
            # flow normally stores and binds before opening the result.
            page.add_script_tag(url=base + "/static/common-product-content.js")
            workspace_product_ids = [source_id, retry_prepared_source_id]
            workspace_return_seed = page.evaluate("""({owner,accountId,productIds,jobUid}) => {
                const helper = window.SellerHubCommonContent;
                if (!helper || typeof helper.storeWorkspaceReturn !== 'function' ||
                    typeof helper.bindOzonPreparationWorkspaceResult !== 'function' ||
                    typeof helper.ozonPreparationWorkspaceReturnHref !== 'function') {
                    return null;
                }
                const payload = {
                    owner,
                    prepareTarget:{accountId,productIds},
                    selectionIds:[],
                    scope:{tab:'all',search:'крем',sort:'title',page:4},
                    batch:null,
                };
                const nonce = helper.storeWorkspaceReturn(payload);
                if (typeof nonce !== 'string' || !/^[a-f0-9]{32}$/.test(nonce)) return null;
                const hash = '#workspace-return=' + nonce;
                const bound = helper.bindOzonPreparationWorkspaceResult(hash, {
                    owner,
                    prepareTarget:{accountId,productIds},
                    jobUid,
                });
                const href = helper.ozonPreparationWorkspaceReturnHref({
                    workspaceUrl:'/my-products/beta',owner,accountId,productIds,jobUid,
                });
                return {nonce,bound,href};
            }""", {
                "owner": workspace_owner,
                "accountId": account_id,
                "productIds": workspace_product_ids,
                "jobUid": prepare_uid,
            })
            assert isinstance(workspace_return_seed, dict), workspace_return_seed
            workspace_return_nonce = workspace_return_seed["nonce"]
            assert re.fullmatch(r"[a-f0-9]{32}", workspace_return_nonce), workspace_return_seed
            assert workspace_return_seed == {
                "nonce": workspace_return_nonce,
                "bound": "#workspace-return=" + workspace_return_nonce,
                "href": "/my-products/beta#workspace-return=" + workspace_return_nonce,
            }, workspace_return_seed
            page.goto(base + parent_result_path)
            assert page.locator("[data-source-prepare-retry]").count() == 0
            initial_regions = page.locator("[data-source-prepare-region]")
            assert initial_regions.count() == 2
            region_content = page.evaluate("""() => Array.from(document.querySelectorAll(
                '[data-source-prepare-region]'
            )).map(region => ({
                name:region.getAttribute('data-source-prepare-region'),
                forbidden:Array.from(region.querySelectorAll(
                    'script,link,iframe,object,embed'
                )).map(node => node.tagName.toLowerCase()),
            }))""")
            assert len(region_content) == 2, region_content
            assert all(not row["forbidden"] for row in region_content), region_content
            for region_name in ("header", "body"):
                region = page.locator(
                    f'[data-source-prepare-region="{region_name}"]',
                )
                assert region.count() == 1
                assert region.get_attribute("data-source-prepare-job") == prepare_uid
                assert region.get_attribute("data-source-prepare-account") == str(account_id)
                assert region.get_attribute("data-source-prepare-complete") == "0"
            return_link = page.locator("a[data-source-prepare-products]")
            return_link.wait_for()
            page.wait_for_function(
                "document.querySelector('a[data-source-prepare-products]')"
                "?.dataset.workspaceReturn === 'verified'",
            )
            return_identity = page.evaluate("""() => {
                const link = document.querySelector('a[data-source-prepare-products]');
                window.__r90PrepareDocument = {};
                window.__r90WorkspaceReturnNode = link;
                window.__r90WorkspaceReturnHref = link?.getAttribute('href') || '';
                window.__r90TerminalHtmlFetches = [];
                const originalFetch = window.fetch.bind(window);
                window.fetch = async function(input, init) {
                    const inputUrl = typeof input === 'string' ? input : input?.url;
                    const inputMethod = init?.method ||
                        (input instanceof Request ? input.method : 'GET');
                    const parsed = new URL(inputUrl, window.location.href);
                    const response = await originalFetch(input, init);
                    if (parsed.pathname === window.location.pathname &&
                        String(inputMethod).toUpperCase() === 'GET') {
                        window.__r90TerminalHtmlFetches.push({
                            status:response.status,
                            content_type:response.headers.get('content-type') || '',
                        });
                    }
                    return response;
                };
                return {
                    href:link?.getAttribute('href') || '',
                    label:link?.getAttribute('aria-label') || '',
                    title:link?.getAttribute('title') || '',
                    workspace_return:link?.dataset.workspaceReturn || '',
                };
            }""")
            assert return_identity["href"].startswith("/my-products/beta")
            assert return_identity["workspace_return"] == "verified"
            return_link.focus()
            assert page.evaluate(
                "document.activeElement === window.__r90WorkspaceReturnNode",
            )
            source_prepare_browser_requests = []
            source_prepare_navigations = []

            def capture_source_prepare_request(request):
                parsed = urlsplit(request.url)
                source_prepare_browser_requests.append({
                    "method": request.method,
                    "path": parsed.path,
                    "resource_type": request.resource_type,
                })

            def capture_source_prepare_navigation(frame):
                if frame == page.main_frame:
                    source_prepare_navigations.append(frame.url)

            page.on("request", capture_source_prepare_request)
            page.on("framenavigated", capture_source_prepare_navigation)
            with app.app_context():
                prepare_stats = OzonBulkUploadService.run_due_preparation()
            assert prepare_stats["needs_input"] == 1, prepare_stats
            assert prepare_stats["prepared"] == 1, prepare_stats
            source_read = api_call_from_page(
                page, "/marketplaces/ozon/uploads/api/" + prepare_uid,
            )
            assert source_read["status"] == 200
            source_run = source_read["document"]["run"]
            assert source_run["source_prepare_complete"] is True, source_run
            original_items = source_run["items"]
            assert [item["status"] for item in original_items] == [
                "needs_input", "prepared",
            ], original_items
            assert source_run["summary"]["prepared"] == 1, source_run["summary"]
            assert source_run["summary"]["needs_input"] == 1, source_run["summary"]
            assert source_run["summary"]["total"] == 2, source_run["summary"]
            assert source_run["summary"]["active"] == 0, source_run["summary"]
            review_draft_ids = [
                str(item["draft_id"])
                for item in original_items
                if item.get("draft_id") is not None
            ]
            assert len(review_draft_ids) == 2, original_items
            page.wait_for_function(
                "window.__r90TerminalHtmlFetches?.length === 1",
                timeout=30000,
            )
            deferred_state = page.evaluate("""() => ({
                fetches:window.__r90TerminalHtmlFetches,
                focused_old_return:document.activeElement === window.__r90WorkspaceReturnNode,
                same_old_return:document.querySelector('a[data-source-prepare-products]') ===
                    window.__r90WorkspaceReturnNode,
                header_complete:document.querySelector(
                    '[data-source-prepare-region="header"]'
                )?.getAttribute('data-source-prepare-complete') || null,
                body_complete:document.querySelector(
                    '[data-source-prepare-region="body"]'
                )?.getAttribute('data-source-prepare-complete') || null,
                retry_count:document.querySelectorAll('[data-source-prepare-retry]').length,
            })""")
            assert deferred_state["fetches"] == [{
                "status": 200,
                "content_type": "text/html; charset=utf-8",
            }], deferred_state
            assert deferred_state["focused_old_return"], deferred_state
            assert deferred_state["same_old_return"], deferred_state
            assert deferred_state["header_complete"] == "0", deferred_state
            assert deferred_state["body_complete"] == "0", deferred_state
            assert deferred_state["retry_count"] == 0, deferred_state
            assert page.locator(
                "#ozon-result-status a.sh-btn--primary",
            ).count() == 1
            fallback_link = page.locator(
                "#ozon-result-status a[data-source-prepare-refresh-fallback]",
            )
            assert fallback_link.count() == 1
            fallback_link.focus()
            page.wait_for_timeout(50)
            fallback_focus_state = page.evaluate("""() => ({
                focused_fallback:document.activeElement === document.querySelector(
                    '[data-source-prepare-refresh-fallback]'
                ),
                header_complete:document.querySelector(
                    '[data-source-prepare-region="header"]'
                )?.getAttribute('data-source-prepare-complete') || null,
                body_complete:document.querySelector(
                    '[data-source-prepare-region="body"]'
                )?.getAttribute('data-source-prepare-complete') || null,
                retry_count:document.querySelectorAll('[data-source-prepare-retry]').length,
            })""")
            assert fallback_focus_state["focused_fallback"], fallback_focus_state
            assert fallback_focus_state["header_complete"] == "0", fallback_focus_state
            assert fallback_focus_state["body_complete"] == "0", fallback_focus_state
            assert fallback_focus_state["retry_count"] == 0, fallback_focus_state
            search_button = page.get_by_role(
                "button", name="Поиск по разделам", exact=True,
            )
            assert search_button.count() == 1
            search_button.focus()
            assert page.evaluate("""() => {
                window.__r90GlobalSearchButton = document.activeElement;
                return window.__r90GlobalSearchButton === document.querySelector(
                    'button[aria-label="Поиск по разделам"]'
                );
            }""")
            page.wait_for_function("""() => {
                const regions = Array.from(document.querySelectorAll(
                    '[data-source-prepare-region]'
                ));
                return regions.length === 2 && regions.every(region =>
                    region.getAttribute('data-source-prepare-complete') === '1'
                );
            }""", timeout=10000)
            terminal_state = page.evaluate("""() => {
                const regions = Array.from(document.querySelectorAll(
                    '[data-source-prepare-region]'
                ));
                const link = document.querySelector('a[data-source-prepare-products]');
                const slot = document.querySelector('[data-source-prepare-products-slot]');
                const body = document.querySelector(
                    '[data-source-prepare-region="body"]'
                );
                const counts = {};
                for (const node of body.querySelectorAll('div')) {
                    const label = node.querySelector(':scope > span');
                    const value = node.querySelector(':scope > strong');
                    if (label && value) counts[label.innerText.trim()] = value.innerText.trim();
                }
                return {
                    fetches:window.__r90TerminalHtmlFetches,
                    regions:regions.map(region => ({
                        name:region.getAttribute('data-source-prepare-region'),
                        job:region.getAttribute('data-source-prepare-job'),
                        account:region.getAttribute('data-source-prepare-account'),
                        complete:region.getAttribute('data-source-prepare-complete'),
                    })),
                    counts,
                    row_text:Array.from(body.querySelectorAll('article')).map(row => row.innerText),
                    retry_count:document.querySelectorAll('[data-source-prepare-retry]').length,
                    retry_total:document.querySelector(
                        '[data-source-prepare-retry-count]'
                    )?.innerText.trim() || null,
                    review_count:document.querySelectorAll(
                        '[data-source-prepare-review]'
                    ).length,
                    review_href:document.querySelector(
                        '[data-source-prepare-review]'
                    )?.getAttribute('href') || '',
                    repair_count:document.querySelectorAll(
                        '[data-source-prepare-repair]'
                    ).length,
                    repair_href:document.querySelector(
                        '[data-source-prepare-repair]'
                    )?.getAttribute('href') || '',
                    same_return_node:link === window.__r90WorkspaceReturnNode,
                    return_href:link?.getAttribute('href') || '',
                    return_label:link?.getAttribute('aria-label') || '',
                    return_title:link?.getAttribute('title') || '',
                    return_verified:link?.dataset.workspaceReturn || '',
                    return_in_slot:!!slot && slot.contains(link),
                    same_document:window.__r90PrepareDocument !== undefined,
                    same_global_search_focus:document.activeElement ===
                        window.__r90GlobalSearchButton &&
                        window.__r90GlobalSearchButton.isConnected,
                    focus_outside_regions:!regions.some(region =>
                        region.contains(document.activeElement)
                    ),
                    pathname:window.location.pathname,
                };
            }""")
            assert terminal_state["fetches"] == deferred_state["fetches"], terminal_state
            assert terminal_state["regions"] == [
                {"name": "header", "job": prepare_uid,
                 "account": str(account_id), "complete": "1"},
                {"name": "body", "job": prepare_uid,
                 "account": str(account_id), "complete": "1"},
            ], terminal_state
            assert terminal_state["counts"].get("Всего") == "2", terminal_state
            assert terminal_state["counts"].get("Подготовлено локально") == "1", terminal_state
            assert terminal_state["counts"].get("Нужно заполнить") == "1", terminal_state
            assert len(terminal_state["row_text"]) == 2, terminal_state
            assert any("Подготовлена" in row for row in terminal_state["row_text"]), terminal_state
            assert any("Нужно заполнить" in row for row in terminal_state["row_text"]), terminal_state
            assert terminal_state["retry_count"] == 1, terminal_state
            assert terminal_state["retry_total"] == "1", terminal_state
            assert terminal_state["review_count"] == 1, terminal_state
            assert terminal_state["repair_count"] == 1, terminal_state
            review_parts = urlsplit(terminal_state["review_href"])
            review_query = parse_qs(review_parts.query)
            assert review_parts.path == "/marketplaces/ozon/uploads/review", terminal_state
            assert review_query.get("account_id") == [str(account_id)], terminal_state
            assert review_query.get("draft_ids") == [",".join(review_draft_ids)], terminal_state
            assert review_query.get("parent_prepare_job_uid") == [prepare_uid], terminal_state
            assert urlsplit(terminal_state["repair_href"]).path == (
                parent_result_path + "/repair"
            ), terminal_state
            assert terminal_state["same_return_node"], terminal_state
            assert terminal_state["return_href"] == return_identity["href"], terminal_state
            assert terminal_state["return_label"] == return_identity["label"], terminal_state
            assert terminal_state["return_title"] == return_identity["title"], terminal_state
            assert terminal_state["return_verified"] == "verified", terminal_state
            assert terminal_state["return_in_slot"], terminal_state
            assert terminal_state["same_document"], terminal_state
            assert terminal_state["same_global_search_focus"], terminal_state
            assert terminal_state["focus_outside_regions"], terminal_state
            assert terminal_state["pathname"] == parent_result_path, terminal_state
            assert source_prepare_navigations == [], source_prepare_navigations
            assert [request for request in source_prepare_browser_requests
                    if request["method"] == "GET"
                    and request["path"] == parent_result_path
                    and request["resource_type"] == "fetch"] == [{
                        "method": "GET",
                        "path": parent_result_path,
                        "resource_type": "fetch",
                    }], source_prepare_browser_requests
            assert not [request for request in source_prepare_browser_requests
                        if request["method"] == "POST"], source_prepare_browser_requests
            assert report["provider_attempts"] == 0
            assert provider.physical_writes == 0 and not provider.full_read_calls
            report["source_prepare_inline"] = {
                "terminal_html_gets": len(deferred_state["fetches"]),
                "workspace_capsule_seeded_and_bound": True,
                "focused_region_deferred_swap": True,
                "focused_fallback_deferred_swap": True,
                "focus_preserved_on_global_search": True,
                "same_document_and_return_node": True,
                "mixed_prepared_needs_input_counts": {
                    "prepared": terminal_state["counts"]["Подготовлено локально"],
                    "needs_input": terminal_state["counts"]["Нужно заполнить"],
                },
                "automatic_transition_posts": 0,
                "provider_attempts": report["provider_attempts"],
            }
            passed("inline_source_prepare_terminal_result_focus_deferred_workspace_return_retry")
            retry_form = page.locator("[data-source-prepare-retry]")
            assert retry_form.count() == 1
            assert page.locator("[data-source-prepare-parent]").count() == 0
            assert retry_form.locator(
                "[data-source-prepare-retry-count]",
            ).inner_text().strip() == "1"
            retry_help = retry_form.inner_text()
            assert "Ozon CI 0" in retry_help
            assert "Подготовка перечитает сохранённые данные." in retry_help
            retry_action = urlsplit(retry_form.get_attribute("action")).path
            assert retry_action == parent_result_path + "/retry", retry_action
            retry_csrf = retry_form.locator(
                'input[name="csrf_token"]',
            ).input_value()
            assert retry_csrf
            assert retry_form.locator(
                'input[name="confirm_prepare"]',
            ).input_value() == "1"
            retry_request_key = retry_form.locator(
                'input[name="request_key"]',
            ).input_value()
            assert re.fullmatch(
                r"prepare-retry-v1-[0-9a-f]{64}", retry_request_key,
            ), retry_request_key
            assert retry_form.get_by_role(
                "button", name="Подготовить повторно", exact=True,
            ).count() == 1
            retry_button = retry_form.get_by_role(
                "button", name="Подготовить повторно", exact=True,
            )
            retry_target = retry_button.bounding_box()
            assert retry_target is not None
            assert retry_target["width"] >= 44 and retry_target["height"] >= 44, retry_target
            page.screenshot(
                path=str(OUT / "ozon-source-prepare-retry-result.png"),
                full_page=True,
            )
            with app.app_context():
                created_draft = MarketplaceProductDraft.query.filter_by(
                    seller_id=fixture["seller_id"],
                    account_id=account_id,
                    imported_product_id=source_id,
                ).one()
                draft_id = created_draft.id
                assert created_draft.id != fixture["draft_id"]
                from models import MarketplaceOperation
                operation_count_before_retry = MarketplaceOperation.query.count()
                from models import AIParsingAttempt
                ai_attempt_count_before_retry = AIParsingAttempt.query.count()
            page.goto(base + "/marketplaces/drafts/?account_id=" + str(account_id))
            page.locator("#ozon-drafts-list:not([v-cloak])").wait_for()
            passed("csrf_new_source_to_incomplete_draft_durable_prepare_and_readback")

            page.goto(base + f"/marketplaces/drafts/{draft_id}")
            page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
            page.locator("#ode-type-search").fill("Посуда")
            page.locator("label.ode-type-option input[name=ozon-product-type]").first.wait_for()
            page.locator("label.ode-type-option input[name=ozon-product-type]").first.check()
            page.get_by_role("button", name="Проверить смену категории", exact=True).click()
            category_dialog = page.get_by_role("dialog")
            category_dialog.get_by_role("checkbox").check()
            category_dialog.get_by_role("button", name="Сохранить новую категорию", exact=True).click()
            page.get_by_text("Категория выбрана. Проверьте сохранённые характеристики перед отправкой.", exact=True).wait_for()
            layout(page, "category-impact")
            passed("fresh_category_impact_review_and_explicit_type_selection")

            with app.app_context():
                editable_draft = MarketplaceDraftService.get_draft(
                    seller_id=fixture["seller_id"], draft_id=draft_id,
                )
                expected_version = editable_draft.version
            saved_local_fields = api_call_from_page(
                page,
                f"/marketplaces/drafts/{draft_id}",
                method="PATCH",
                body={
                    "expected_version": expected_version,
                    "patch": {
                        "dimensions": {
                            "width": "231", "height": "30", "depth": "300",
                            "dimension_unit": "MILLIMETERS", "weight": "250",
                            "weight_unit": "GRAMS",
                        },
                        "confirm_package": True,
                        "barcodes": ["4600000000003"],
                        "commercial": {
                            "price": "1000", "old_price": "1200",
                            "vat": "0.22", "currency_code": "RUB",
                        },
                    },
                },
                csrf=csrf,
            )
            assert saved_local_fields["status"] == 200, saved_local_fields
            expected_version = saved_local_fields["document"]["draft"]["version"]
            fresh_validation = api_call_from_page(
                page,
                f"/marketplaces/drafts/{draft_id}/validate",
                method="POST",
                body={"expected_version": expected_version},
                csrf=csrf,
            )
            assert fresh_validation["status"] == 200, fresh_validation
            assert fresh_validation["document"]["draft"]["validation"][
                "publishable"
            ] is True, fresh_validation["document"]["draft"]["validation"]

            refreshed_description = (
                "Обновлённое локальное описание источника. "
                "Цвет: янтарный. Поверхность: гладкая."
            )
            with app.app_context():
                changed_source = db.session.get(ImportedProduct, source_id)
                changed_source.description = refreshed_description
                changed_source.original_data = json.dumps({
                    "title": changed_source.title,
                    "description": refreshed_description,
                }, ensure_ascii=False)
                db.session.commit()

            page.goto(base + parent_result_path)
            retry_form = page.locator("[data-source-prepare-retry]")
            assert retry_form.count() == 1
            assert retry_form.locator(
                'input[name="request_key"]',
            ).input_value() == retry_request_key
            with app.app_context():
                from models import (
                    BackgroundJob, MarketplaceOperation, OzonBulkUploadItem,
                    OzonBulkUploadRun,
                )
                parent_job = BackgroundJob.query.filter_by(
                    seller_id=fixture["seller_id"], job_uid=prepare_uid,
                ).one()
                parent_run = OzonBulkUploadRun.query.filter_by(
                    seller_id=fixture["seller_id"], account_id=account_id,
                    job_id=parent_job.id,
                ).one()
                parent_run_id = parent_run.id
                parent_rows_before_retry = [
                    (row.imported_product_id, row.phase, row.draft_id,
                     row.prepared_version)
                    for row in OzonBulkUploadItem.query.filter_by(
                        run_id=parent_run.id,
                    ).order_by(OzonBulkUploadItem.ordinal).all()
                ]
                source_prepare_runs_before_retry = OzonBulkUploadRun.query.filter_by(
                    seller_id=fixture["seller_id"], account_id=account_id,
                    mode="source_prepare",
                ).count()
                operations_before_retry = MarketplaceOperation.query.count()
                assert operations_before_retry == operation_count_before_retry

            dialog_events = []

            def dismiss_unexpected_retry_dialog(dialog):
                dialog_events.append(dialog.type)
                dialog.dismiss()

            page.on("dialog", dismiss_unexpected_retry_dialog)
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and urlsplit(response.url).path == retry_action,
            ) as retry_response_info:
                retry_form.get_by_role(
                    "button", name="Подготовить повторно", exact=True,
                ).click()
            first_retry_response = retry_response_info.value
            assert first_retry_response.status == 303, first_retry_response.status
            assert dialog_events == [], dialog_events
            child_result_path = urlsplit(
                first_retry_response.headers["location"],
            ).path
            assert child_result_path != parent_result_path
            page.wait_for_url(base + child_result_path)
            assert urlsplit(page.url).path == child_result_path
            child_uid = child_result_path.rsplit("/", 1)[-1]
            child_before_run = api_call_from_page(
                page, "/marketplaces/ozon/uploads/api/" + child_uid,
            )
            assert child_before_run["status"] == 200, child_before_run
            child_initial_run = child_before_run["document"]["run"]
            assert child_initial_run["account_id"] == account_id
            assert [item["imported_product_id"] for item in
                    child_initial_run["items"]] == [source_id]
            assert child_initial_run["items"][0]["status"] == "pending"

            parent_link = page.locator("[data-source-prepare-parent]")
            assert parent_link.count() == 1
            assert urlsplit(parent_link.get_attribute("href")).path == parent_result_path
            with app.app_context():
                child_job = BackgroundJob.query.filter_by(
                    seller_id=fixture["seller_id"], job_uid=child_uid,
                ).one()
                child_run = OzonBulkUploadRun.query.filter_by(
                    seller_id=fixture["seller_id"], account_id=account_id,
                    job_id=child_job.id,
                ).one()
                assert child_run.mode == "source_prepare"
                assert child_run.parent_prepare_run_id == parent_run_id
                assert child_job.seller_id == fixture["seller_id"]
                assert child_run.seller_id == fixture["seller_id"]
                assert OzonBulkUploadItem.query.filter_by(
                    run_id=child_run.id,
                ).count() == 1

                retry_stats = OzonBulkUploadService.run_due_preparation()
                assert retry_stats["prepared"] == 1, retry_stats
                child_item = OzonBulkUploadItem.query.filter_by(
                    run_id=child_run.id,
                ).one()
                assert child_item.imported_product_id == source_id
                assert child_item.phase == "prepared"
                assert child_item.draft_id == draft_id
                rebased_draft = MarketplaceDraftService.get_draft(
                    seller_id=fixture["seller_id"], draft_id=draft_id,
                )
                rebased_content = json.loads(rebased_draft.content_json or "{}")
                rebased_dimensions = json.loads(
                    rebased_draft.dimensions_json or "{}",
                )
                assert rebased_content.get("description") == refreshed_description
                assert rebased_dimensions.get("width") == "231"
                assert MarketplaceDraftService._build_validation_result(
                    rebased_draft,
                )["publishable"] is True
                parent_rows_after_retry = [
                    (row.imported_product_id, row.phase, row.draft_id,
                     row.prepared_version)
                    for row in OzonBulkUploadItem.query.filter_by(
                        run_id=parent_run_id,
                    ).order_by(OzonBulkUploadItem.ordinal).all()
                ]
                assert parent_rows_after_retry == parent_rows_before_retry
                assert OzonBulkUploadRun.query.filter_by(
                    seller_id=fixture["seller_id"], account_id=account_id,
                    mode="source_prepare",
                ).count() == source_prepare_runs_before_retry + 1
                assert MarketplaceOperation.query.count() == operations_before_retry
                assert AIParsingAttempt.query.count() == ai_attempt_count_before_retry
                operations_after_retry = MarketplaceOperation.query.count()

            page.reload()
            child_result = api_call_from_page(
                page, "/marketplaces/ozon/uploads/api/" + child_uid,
            )
            assert child_result["document"]["run"]["account_id"] == account_id
            child_items = child_result["document"]["run"]["items"]
            assert len(child_items) == 1
            assert child_items[0]["imported_product_id"] == source_id
            assert child_items[0]["status"] == "prepared"
            assert page.locator("[data-source-prepare-retry]").count() == 0
            parent_link = page.locator("[data-source-prepare-parent]")
            assert parent_link.count() == 1
            assert urlsplit(parent_link.get_attribute("href")).path == parent_result_path
            parent_link.click()
            page.wait_for_url("**" + parent_result_path)
            parent_retry_form = page.locator("[data-source-prepare-retry]")
            assert parent_retry_form.count() == 1
            assert parent_retry_form.locator(
                'input[name="request_key"]',
            ).input_value() == retry_request_key
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and urlsplit(response.url).path == retry_action,
            ) as replay_response_info:
                parent_retry_form.get_by_role(
                    "button", name="Подготовить повторно", exact=True,
                ).click()
            replay_response = replay_response_info.value
            assert replay_response.status == 303, replay_response.status
            assert dialog_events == [], dialog_events
            assert urlsplit(replay_response.headers["location"]).path == child_result_path
            page.wait_for_url(base + child_result_path)
            assert urlsplit(page.url).path == child_result_path
            page.remove_listener("dialog", dismiss_unexpected_retry_dialog)
            with app.app_context():
                assert OzonBulkUploadRun.query.filter_by(
                    seller_id=fixture["seller_id"], account_id=account_id,
                    mode="source_prepare",
                ).count() == source_prepare_runs_before_retry + 1
                assert OzonBulkUploadItem.query.filter_by(
                    run_id=child_run.id,
                ).count() == 1
                assert MarketplaceOperation.query.count() == operations_after_retry
                assert AIParsingAttempt.query.count() == ai_attempt_count_before_retry
            assert report["provider_attempts"] == 0
            assert provider.physical_writes == 0 and not provider.full_read_calls
            passed("completed_local_retry_exact_scope_csrf_replay_parent_return_and_source_rebase")

            page.goto(base + "/marketplaces/drafts/?account_id=" + str(account_id))
            page.locator("#ozon-drafts-list:not([v-cloak])").wait_for()

            foreign = api_call_from_page(
                page,
                f"/marketplaces/api/drafts/{foreign_draft_id}/ai-suggestions",
            )
            assert foreign["status"] == 404, foreign
            assert "Foreign private title" not in json.dumps(foreign, ensure_ascii=False)
            passed("foreign_account_draft_denied_without_disclosure")

            page.get_by_role("button", name="Для AI-дополнения", exact=True).click()
            ai_checkbox = page.get_by_role(
                "checkbox", name=re.compile("Выбрать для AI Новая карточка янтарного цвета"),
            )
            ai_checkbox.check()
            assert page.get_by_text("Для AI выбрано 1", exact=False).is_visible()
            page.get_by_role("button", name="Предложить характеристики", exact=False).click()
            page.get_by_role("dialog").get_by_role("checkbox").check()
            with page.expect_response(lambda response:
                    response.request.method == "POST" and
                    urlsplit(response.url).path == "/marketplaces/api/drafts/ai-completions") as ai_response_info:
                page.get_by_role("dialog").get_by_role(
                    "button", name="Запустить поиск предложений", exact=True,
                ).click()
            ai_response = ai_response_info.value
            if ai_response.status != 202:
                raise AssertionError(("ai_create_status", ai_response.status,
                                      report["ai_create_response"].get("code")))
            page.wait_for_url(re.compile(r"/marketplaces/drafts/ai-completions/[^/]+$"))
            ai_run_path = urlsplit(page.url).path
            with app.app_context():
                first_ai_tick = ozon_draft_ai_worker.tick(executor=ImmediateExecutor())
                second_ai_tick = ozon_draft_ai_worker.tick(executor=ImmediateExecutor())
            assert first_ai_tick["started_calls"] == 1, first_ai_tick
            assert second_ai_tick["completed_calls"] == 1, second_ai_tick
            page.reload()
            page.locator("#ozon-ai-run:not([v-cloak])").wait_for()
            passed("real_ai_reservation_worker_completion_and_run_readback")

            page.get_by_role("link", name="Просмотреть предложения", exact=True).click()
            page.wait_for_url(f"**/marketplaces/drafts/{draft_id}?ai_item_id=*")
            page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
            page.locator(".ode-ai-item").first.wait_for()
            assert page.locator(".ode-ai-item").count() == 2
            evidence = page.locator(".ode-ai-item").first.locator("details summary")
            evidence.click()
            assert page.locator(".ode-ai-item q").first.inner_text() == "янтарный"
            layout(page, "ai-evidence")
            page.locator(".ode-ai-item").first.get_by_role("checkbox").check()
            page.get_by_role("button", name="Принять выбранные", exact=True).click()
            page.locator(".ode-ai-dialog[open]").get_by_role("checkbox").check()
            page.locator(".ode-ai-dialog[open]").get_by_role(
                "button", name="Подтвердить решение", exact=True,
            ).click()
            page.locator(".ode-ai-item.is-accepted").wait_for()
            assert page.locator(".ode-ai-item.is-accepted").count() == 1
            assert page.locator(".ode-ai-item.is-proposed").count() == 1
            passed("selective_ai_apply_reopen_preserves_unselected_proposal")

            page.get_by_role("button", name="Основное", exact=True).click()
            page.locator("#ode-name").fill("Новая карточка янтарного цвета")
            page.locator("#ode-description").fill("Цвет: янтарный. Поверхность: гладкая.")
            page.locator(".ode-tabs").get_by_role(
                "button", name=re.compile("^Фотографии"),
            ).click()
            if page.locator(".ode-media-item").count() == 0:
                page.locator("#ode-photo-url").fill(photo_url)
                page.get_by_role("button", name="Добавить", exact=True).click()
            assert page.locator(".ode-media-item").count() >= 1
            measure_mobile_ozon_photo_touch_targets(page)
            assert [
                (row["width_px"], row["theme"])
                for row in report["mobile_ozon_photo_touch_observations"]
            ] == [
                (width, theme)
                for theme in ("light", "dark")
                for width in (320, 360, 390)
            ]
            passed("mobile_ozon_editor_tabs_and_photo_actions_touch_focus_320_360_390_light_dark")
            # Return to the pre-check desktop/light UI state through the actual theme control.
            set_theme_through_visible_control(page, "light")
            page.get_by_role("button", name="Цена и упаковка", exact=True).click()
            for selector, value in (
                ("#ode-price", "1000"),
                ("#ode-old-price", "1200"),
                ("#ode-width", "200"),
                ("#ode-height", "30"),
                ("#ode-depth", "300"),
                ("#ode-weight", "250"),
            ):
                page.locator(selector).fill(value)
            page.locator("#ode-vat").select_option("0.22")
            page.locator("#ode-currency").select_option("RUB")
            page.locator("#ode-dimension-unit").select_option("MILLIMETERS")
            page.locator("#ode-weight-unit").select_option("GRAMS")
            page.locator("[data-package-confirmation-needed]").wait_for(state="visible")
            page.locator("#ode-package-confirm").check()
            if page.get_by_role("textbox", name="Штрихкод 1").count() == 0:
                page.get_by_role("button", name="+ Добавить штрихкод", exact=True).click()
            page.get_by_role("textbox", name="Штрихкод 1").fill("4600000000001")
            page.get_by_role("button", name="Сохранить", exact=True).click()
            page.get_by_text("Все изменения сохранены", exact=True).wait_for()
            page.get_by_role("button", name="Проверить карточку", exact=True).click()
            page.get_by_text(
                "Текущие поля проходят локальную проверку. При отправке данные будут проверены повторно.",
                exact=True,
            ).wait_for()
            positive_config = bootstrap(page, "#ode-bootstrap")
            assert positive_config.get("draftId") == draft_id, {
                "bootstrap_draft_id_matches": positive_config.get("draftId") == draft_id,
            }
            fresh_editor_response = api_call_from_page(page, positive_config["urls"]["editor"])
            assert fresh_editor_response["status"] == 200, {
                "status": fresh_editor_response["status"],
                "code": (fresh_editor_response.get("document") or {}).get("code"),
            }
            fresh_editor = fresh_editor_response["document"]
            assert fresh_editor.get("package_confirmation", {}).get("source") == "seller_confirmed"
            fresh_validation = fresh_editor.get("current_validation") or {}
            fresh_draft = fresh_editor.get("draft") or {}
            fresh_errors = fresh_validation.get("errors")
            assert fresh_draft.get("id") == draft_id, {
                "draft_id_matches": fresh_draft.get("id") == draft_id,
            }
            assert fresh_validation.get("publishable") is True, {
                "current_publishable": fresh_validation.get("publishable"),
                "error_codes": [
                    item.get("code") for item in fresh_errors[:30] if isinstance(item, dict)
                ] if isinstance(fresh_errors, list) else None,
            }
            assert fresh_errors == [], {"current_errors": fresh_errors}
            assert fresh_draft.get("status") == "ready" and fresh_draft.get("validation_status") == "valid", {
                "stored_status": fresh_draft.get("status"),
                "stored_validation_status": fresh_draft.get("validation_status"),
            }
            page.get_by_role("button", name=re.compile("Отправить в Ozon")).wait_for()
            assert page.get_by_role("button", name=re.compile("Отправить в Ozon")).is_enabled()
            page.reload()
            page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
            layout(page, "reviewed-draft")
            passed("seller_fills_clean_new_category_media_packaging_vat_and_fresh_validation")
            action_error_focus_check(context, page)

            page.goto(base + "/marketplaces/drafts/?account_id=" + str(account_id))
            page.locator("#ozon-drafts-list:not([v-cloak])").wait_for()
            with app.app_context():
                account_default_vat_configured = (
                    db.session.get(SellerMarketplaceAccount, account_id)
                    .public_settings.get("default_vat") is not None
                )
            list_snapshot = page.locator("#odl-bootstrap").evaluate(
                "element => JSON.parse(element.textContent)"
            )
            report["synthetic"]["clean_draft_list_state"] = {
                "account_default_vat_configured": account_default_vat_configured,
                "rows": [{
                    "title": row.get("title"),
                    "status": row.get("status"),
                    "validation_status": row.get("validation_status"),
                    "publishable": (row.get("validation_summary") or {}).get("publishable"),
                } for row in list_snapshot.get("rows", [])],
                "accounts": [{
                    "can_publish": account.get("can_publish"),
                    "credential_expired": bool(account.get("credential_expires_at") and
                        account.get("credential_expired")),
                } for account in list_snapshot.get("accounts", [])],
            }
            assert account_default_vat_configured is False
            clean_row = next(
                row for row in list_snapshot["rows"]
                if row.get("title") == "Новая карточка янтарного цвета"
            )
            clean_account = next(
                row for row in list_snapshot["accounts"] if row.get("id") == account_id
            )
            assert clean_row["status"] == "ready"
            assert clean_row["validation_status"] == "valid"
            assert clean_row["validation_summary"]["publishable"] is True
            assert clean_account["can_publish"] is True
            page.get_by_role("button", name="Для отправки", exact=True).click()
            send_checkbox = page.get_by_role(
                "checkbox", name=re.compile("Выбрать для отправки Новая карточка янтарного цвета"),
            )
            assert send_checkbox.count() == 1
            send_checkbox.check()
            passed("ready_explicit_vat_draft_is_sendable_without_account_default_vat")
            page.get_by_role("button", name="Просмотреть карточки →", exact=True).click()
            page.locator("#ozon-upload-review:not([v-cloak])").wait_for()
            assert page.locator(".our-card--blocked").count() == 0
            layout(page, "fresh-review")
            page.get_by_role("checkbox", name=re.compile("Выбрать карточку")).first.check()
            page.get_by_role("button", name="Проверить выбранные", exact=True).click()
            page.get_by_role("dialog").get_by_role("checkbox").check()
            page.get_by_role("dialog").get_by_role(
                "button", name="Отправить проверенные карточки", exact=True,
            ).click()
            page.wait_for_url(re.compile(
                r"/marketplaces/ozon/uploads/(?!review(?:/|$))[^/?#]+$",
            ))
            upload_uid = urlsplit(page.url).path.rsplit("/", 1)[-1]
            with app.app_context():
                from models import OzonBulkUploadRun, OzonBulkUploadItem, MarketplaceOperation
                job = OzonBulkUploadService.get_run(
                    seller_id=fixture["seller_id"], job_uid=upload_uid,
                    reconcile=False,
                )
                run = OzonBulkUploadRun.query.filter_by(
                    job_id=job.id, seller_id=fixture["seller_id"],
                ).one()
                assert run.account_id == account_id
                queued_stats = OzonBulkUploadService.run_due_preparation()
                db.session.expire_all()
                item = OzonBulkUploadItem.query.filter_by(run_id=run.id).one()
                operation_id = item.operation_id
                report["synthetic"]["reviewed_queue_outcome"] = {
                    "phase": item.phase,
                    "error_code": item.error_code,
                    "operation_linked": operation_id is not None,
                    "enqueued_count": queued_stats["enqueued"],
                }
                assert operation_id is not None, report["synthetic"]["reviewed_queue_outcome"]
                operation = db.session.get(MarketplaceOperation, operation_id)
                assert operation.account_id == account_id and operation.draft_id == draft_id
            assert queued_stats["enqueued"] == 1, queued_stats
            page.goto(
                base + f"/marketplaces/operations/{operation_id}",
                wait_until="domcontentloaded",
            )
            csrf_token = page.locator('input[name="csrf_token"]').first.input_value()
            submitted = post_json(page, f"/marketplaces/operations/{operation_id}/poll", {}, csrf_token)
            assert submitted["status"] == 200, submitted
            assert submitted["document"]["operation"]["status"] == "submitted"
            completed = post_json(page, f"/marketplaces/operations/{operation_id}/poll", {}, csrf_token)
            assert completed["status"] == 200, completed
            assert completed["document"]["operation"]["status"] == "succeeded"
            assert provider.physical_writes == 1
            assert len(provider.full_read_calls) == 4
            submitted_media = provider.submitted_payloads[0]["items"][0]
            assert photo_url in [
                submitted_media.get("primary_image"),
                *(submitted_media.get("images") or []),
            ]
            page.goto(base + f"/marketplaces/operations/{operation_id}")
            assert "успеш" in page.locator("main").inner_text().lower()
            layout(page, "operation-readback")
            passed("fresh_review_one_provider_write_task_and_exact_full_readback")

            upload_result = api_call_from_page(
                page, f"/marketplaces/ozon/uploads/api/{upload_uid}",
            )
            assert upload_result["status"] == 200
            assert upload_result["document"]["run"]["items"][0]["status"] == "succeeded"
            passed("upload_run_result_readback_matches_completed_operation")
            browser.close()

    server.shutdown()
    server.server_close()

    seed_branch_counts()
    branch_result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(OzonCompleteJourneyTest).run(branch_result)
    assert branch_result.wasSuccessful(), branch_result.errors + branch_result.failures
    branch_counts = OzonCompleteJourneyTest.branch_counts
    assert branch_counts["success"] == {"ai_calls": 1, "provider_writes": 1, "readbacks": 4}
    assert branch_counts["invalid"] == {"ai_calls": 1, "provider_writes": 0}
    assert branch_counts["repair"] == {
        "ai_calls": 1, "human_repairs": 1, "repreparations": 1,
    }
    assert branch_counts["clean_new_source"] == {
        "new_sources": 1, "prepared_drafts": 1, "category_selections": 1,
        "human_packaging_vat": 1, "fresh_validations": 1,
        "provider_writes": 1, "full_readbacks": 4,
    }
    assert branch_counts["unknown"] == {
        "provider_writes": 1, "automatic_retries": 0, "quarantines": 1,
        "safe_reconciliations": 1,
    }
    report["synthetic"] = {
        "browser_happy_path": {
            "ai_calls": 1,
            "provider_writes": provider.physical_writes,
            "task_polls": 1,
            "full_readbacks": len(provider.full_read_calls),
            "photo_present_in_submit": True,
        },
        "clean_draft_list_state": report["synthetic"]["clean_draft_list_state"],
        "reviewed_queue_outcome": report["synthetic"]["reviewed_queue_outcome"],
        "invalid_ai": branch_counts["invalid"],
        "human_repair": branch_counts["repair"],
        "clean_new_source": branch_counts["clean_new_source"],
        "unknown_write_quarantine_recovery": branch_counts["unknown"],
    }
    report["checks"].append("unknown_write_no_retry_quarantine_and_safe_reconciliation_contract")
    report["checks"].append("invalid_ai_evidence_is_rejected_without_draft_mutation")
    assert len(report["checks"]) >= 8, report["checks"]
    assert len(report["layouts"]) >= 4, report["layouts"]
    assert report["provider_attempts"] == 0
    assert not report["js_errors"] and not report["unexpected_http"] and not report["external"]
    report["status"] = "passed"
except Exception as error:
    report["status"] = "failed"
    report["error"] = f"{type(error).__name__}: {error}"
    print(report["error"], flush=True)
    raise
finally:
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass
    temporary.cleanup()
    filename = OUT / "ozon-complete-journey-browser.json"
    filename.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "status": report["status"],
        "checks": len(report["checks"]),
        "layouts": len(report["layouts"]),
        "provider_attempts": report["provider_attempts"],
    }), flush=True)
