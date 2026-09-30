"""Synthetic end-to-end Ozon draft-to-publication browser contract.

The browser uses the real local Flask routes, CSRF, Vue screens, durable
preparation/upload workers, publication service and readback. Only AI transport
and the marketplace adapter are synthetic; the process has no external network.
"""

import base64
from contextlib import ExitStack
from datetime import datetime
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
from playwright.sync_api import sync_playwright
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
    MarketplaceProductDraft,
    MarketplaceProductType,
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
from services.ozon_draft_ai_transport import FlashOutcome, FlashUsage
from services import ozon_draft_ai_worker
from tests.ozon_release.seed import PASSWORD, USERNAME, seed
from tests.test_ozon_complete_journey import (
    ImmediateExecutor,
    OzonCompleteJourneyTest,
    SyntheticOzonProvider,
)
from tests.test_marketplace_publications import SYNTHETIC_CREDENTIALS


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
        db.session.add(new_source)
        db.session.commit()
        return foreign_draft.id, new_source.id, product_type.id


foreign_draft_id, source_id, product_type_id = configure_fixture()


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


def browser_bridge(route):
    request = route.request
    parsed = urlsplit(request.url)
    if parsed.hostname == "127.0.0.1" and parsed.port == server.server_port:
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
            page.on("response", lambda response: report["unexpected_http"].append({
                "status": response.status, "path": urlsplit(response.url).path,
            }) if response.status >= 500 else None)

            page.goto(base + "/login?next=/marketplaces/drafts/?account_id=" + str(account_id))
            page.locator("input[name=username]").fill(USERNAME)
            page.locator("input[name=password]").fill(PASSWORD)
            page.get_by_role("button", name="Войти", exact=True).click()
            page.goto(base + "/marketplaces/drafts/?account_id=" + str(account_id))
            list_config = bootstrap(page, "#odl-bootstrap")
            page.locator("#ozon-drafts-list:not([v-cloak])").wait_for()
            passed("real_login_and_draft_workspace")

            csrf = list_config["csrf"]
            prepared = post_json(page, "/marketplaces/ozon/uploads/", {
                "account_id": account_id,
                "imported_product_ids": [source_id],
                "confirm_prepare": True,
                "request_key": "complete-browser-prepare-000001",
            }, csrf)
            assert prepared["status"] == 202, prepared
            prepare_uid = prepared["document"]["run"]["job_uid"]
            with app.app_context():
                prepare_stats = OzonBulkUploadService.run_due_preparation()
            assert prepare_stats["needs_input"] == 1, prepare_stats
            source_read = api_call_from_page(
                page, "/marketplaces/ozon/uploads/api/" + prepare_uid,
            )
            assert source_read["status"] == 200
            assert source_read["document"]["run"]["items"][0]["status"] == "needs_input"
            with app.app_context():
                created_draft = MarketplaceProductDraft.query.filter_by(
                    seller_id=fixture["seller_id"],
                    account_id=account_id,
                    imported_product_id=source_id,
                ).one()
                draft_id = created_draft.id
                assert created_draft.id != fixture["draft_id"]
            page.reload()
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
            if page.get_by_role("textbox", name="Штрихкод 1").count() == 0:
                page.get_by_role("button", name="+ Добавить штрихкод", exact=True).click()
            page.get_by_role("textbox", name="Штрихкод 1").fill("4600000000001")
            page.get_by_role("button", name="Сохранить", exact=True).click()
            page.get_by_text("Все изменения сохранены", exact=True).wait_for()
            page.get_by_role("button", name="Проверить карточку", exact=True).click()
            page.get_by_text("Карточка прошла локальную проверку. При отправке данные будут проверены повторно.", exact=True).wait_for()
            page.get_by_role("button", name=re.compile("Отправить в Ozon")).wait_for()
            assert page.get_by_role("button", name=re.compile("Отправить в Ozon")).is_enabled()
            page.reload()
            page.locator("#ozon-draft-editor:not([v-cloak])").wait_for()
            layout(page, "reviewed-draft")
            passed("seller_fills_clean_new_category_media_packaging_vat_and_fresh_validation")

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
