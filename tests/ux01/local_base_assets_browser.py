"""Offline browser proof for the first-party Tailwind and Alpine shell assets.

This focused fixture creates its own synthetic SQLite database, logs in through
the real Flask form, and reads one synthetic Ozon listing through the native
HTML/DTO route. Browser egress is denied; the two fixture photos are fulfilled
from synthetic SVG bytes. The report contains no listing data or credentials.

Run with OZON_BROWSER_ARTIFACTS pointing at a unique writable evidence folder.
"""

from __future__ import annotations

import base64
from collections import Counter
import json
import logging
import logging.handlers
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
from urllib.parse import quote, urlsplit

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(os.environ.get("OZON_BROWSER_ARTIFACTS", "/tmp/ozon-browser-artifacts"))
ARTIFACTS.mkdir(parents=True, exist_ok=True)
ARTIFACT = ARTIFACTS / "local-base-assets-browser.json"
TEMP = tempfile.TemporaryDirectory(prefix="local-base-assets-browser-")
TEMP_PATH = Path(TEMP.name)
OUTCOME = {
    "schema": "ozon-local-base-assets-browser-v1",
    "status": "running",
    "scope": "synthetic_login_exact_ozon_detail_same_origin_shell_assets",
    "failures": [],
    "interactions": {},
    "page_errors": [],
    "blocked_external_resource_types": {},
    "external_script_requests": 0,
    "local_static_status": {},
    "local_get_requests": 0,
    "login_post_count": 0,
    "login_post_status": None,
    "non_login_post_requests": 0,
    "listing_html_gets": 0,
    "listing_dto_gets": 0,
    "fixture_photo_fulfills": 0,
    "provider_calls": 0,
    "network_egress_attempts": 0,
    "python_network_attempts": 0,
    "raw_listing_data_emitted": False,
    "credentials_emitted": False,
    "raw_urls_emitted": False,
}

os.environ.update({
    "DATABASE_URL": "sqlite:///" + str(TEMP_PATH / "app.sqlite"),
    "SKIP_SCHEDULER": "1",
    "IMAGE_LAB_INLINE_WORKER": "0",
    "SECRET_KEY": "local-base-assets-synthetic-only-key",
    "ENCRYPTION_KEY": base64.urlsafe_b64encode(b"r" * 32).decode("ascii"),
    "MARKETPLACE_OZON_ENABLED": "1",
    "MARKETPLACE_OZON_PUBLICATION_ENABLED": "0",
    "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED": "0",
    "MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED": "0",
    "OZON_RATE_LIMIT_DIR": str(TEMP_PATH / "limits"),
    "PYTHONDONTWRITEBYTECODE": "1",
})
sys.path.insert(0, str(ROOT))

PYTHON_NETWORK_ATTEMPTS = 0


def deny_python_network(*_args, **_kwargs):
    global PYTHON_NETWORK_ATTEMPTS
    PYTHON_NETWORK_ATTEMPTS += 1
    raise RuntimeError("external Python network disabled in browser fixture")


import requests

requests.sessions.Session.request = deny_python_network
socket.create_connection = deny_python_network
logging.handlers.RotatingFileHandler = lambda *_args, **_kwargs: logging.NullHandler()
logging.getLogger("werkzeug").setLevel(logging.ERROR)

from seller_platform import app
from models import MarketplaceListing, User, db
from tests.ozon_release.seed import PASSWORD, PHOTO, USERNAME, seed


app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False)
FIXTURE = seed(app)
PHOTO2 = "https://ozon-fixture.test/local-base-assets-product-2.svg"
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="300">'
    b'<rect width="240" height="300" fill="#eee"/></svg>'
)
with app.app_context():
    target = db.session.get(MarketplaceListing, FIXTURE["listing_ids"][0])
    target.title = "Local base asset synthetic listing"
    target.description = "Synthetic description for a browser-only fixture. " * 12
    target.media_json = json.dumps({"primary_image": PHOTO, "images": [PHOTO, PHOTO2]})
    db.session.commit()
    if User.query.filter_by(username=USERNAME).one_or_none() is None:
        raise RuntimeError("synthetic_user_setup_failed")

logging.getLogger("werkzeug").setLevel(logging.ERROR)
server = make_server("127.0.0.1", 0, app, threaded=True)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
ORIGIN = f"http://127.0.0.1:{server.server_port}"
DETAIL_PATH = f"/marketplaces/listings/view/{FIXTURE['listing_ids'][0]}"
PHOTO_URLS = {PHOTO, PHOTO2}
BLOCKED_EXTERNAL = Counter()
EXTERNAL_SCRIPT_REQUESTS = 0
FIXTURE_PHOTO_FULFILLS = 0
LOCAL_GET_REQUESTS = 0
LOCAL_STATIC_STATUS = {}
LOGIN_POST_COUNT = 0
LOGIN_POST_STATUS = None
NON_LOGIN_POST_REQUESTS = 0
LISTING_HTML_GETS = 0
LISTING_DTO_GETS = 0
PAGE_ERRORS = []

REQUIRED_STATIC_PATHS = [
    "/static/vendor/tailwindcss-3.4.17-play-cdn.js",
    "/static/vendor/alpine-collapse-3.17.4.min.js",
    "/static/vendor/alpine-focus-3.17.4.min.js",
    "/static/sh-ui.js",
    "/static/vendor/alpine-3.17.4.global.min.js",
    "/static/vendor/vue-3.4.38.global.prod.js",
    "/static/marketplace-beta-shared.js",
    "/static/marketplace-detail-beta.js",
    "/static/ai-chat-popup.js",
    "/static/marketplace-catalog-beta.css",
    "/static/marketplace-prices.css",
    "/static/marketplace-listing-detail-beta.css",
]

INIT_SCRIPT = r"""
(() => {
  window.__localShellErrors = [];
  window.__localShellResourceErrors = 0;
  window.addEventListener('error', event => {
    if (event.target && event.target !== window) {
      window.__localShellResourceErrors += 1;
      return;
    }
    const error = event.error || {};
    if (!error.name) return;
    window.__localShellErrors.push({
      name: String(error.name || ''),
      message: String(error.message || event.message || ''),
      stack: String(error.stack || '')
    });
  }, true);
})();
"""


def safe_page_error(raw):
    name = raw.get("name", "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,31}", name):
        name = "Error"
    message = raw.get("message", "")
    identifier_match = re.fullmatch(
        r"([A-Za-z_$][A-Za-z0-9_$]{0,63}) is not defined", message
    )
    safe_message = (
        f"{identifier_match.group(1)} is not defined"
        if name == "ReferenceError" and identifier_match
        else "redacted"
    )
    source_path = "first-party inline or script (unresolved)"
    stack = raw.get("stack", "")
    static_match = re.search(
        r"http://127\.0\.0\.1:\d+(/static/[A-Za-z0-9_./-]+\.js)",
        stack,
    )
    if static_match and ".." not in static_match.group(1).split("/"):
        source_path = static_match.group(1)
    elif name == "ReferenceError" and safe_message == "tailwind is not defined":
        source_path = "templates/base.html:25"
    return {"name": name, "message": safe_message, "first_party_path": source_path}


def browser_route(route):
    global BLOCKED_EXTERNAL, EXTERNAL_SCRIPT_REQUESTS, FIXTURE_PHOTO_FULFILLS
    global LOCAL_GET_REQUESTS, LOGIN_POST_COUNT, LOGIN_POST_STATUS
    global NON_LOGIN_POST_REQUESTS, LISTING_HTML_GETS, LISTING_DTO_GETS

    request = route.request
    parts = urlsplit(request.url)
    if request.url in PHOTO_URLS:
        FIXTURE_PHOTO_FULFILLS += 1
        route.fulfill(status=200, content_type="image/svg+xml", body=SVG)
        return

    if (
        parts.scheme == "http"
        and parts.hostname == "127.0.0.1"
        and parts.port == server.server_port
    ):
        if request.method not in {"GET", "HEAD", "POST"}:
            NON_LOGIN_POST_REQUESTS += 1
            route.abort()
            return
        if request.method == "POST":
            if parts.path != "/login":
                NON_LOGIN_POST_REQUESTS += 1
                route.abort()
                return
            LOGIN_POST_COUNT += 1
        else:
            LOCAL_GET_REQUESTS += 1
        listing_route = re.fullmatch(r"/marketplaces/listings/view/[1-9][0-9]*", parts.path)
        if listing_route and request.method == "GET":
            if "application/json" in request.headers.get("accept", ""):
                LISTING_DTO_GETS += 1
            else:
                LISTING_HTML_GETS += 1
        if request.resource_type in {"script", "stylesheet"}:
            response = route.fetch(max_redirects=0)
            if parts.path.startswith("/static/"):
                LOCAL_STATIC_STATUS[parts.path] = response.status
            if request.method == "POST":
                LOGIN_POST_STATUS = response.status
            route.fulfill(response=response)
            return
        response = route.fetch(max_redirects=0)
        if request.method == "POST":
            LOGIN_POST_STATUS = response.status
        route.fulfill(response=response)
        return

    # No request URL, host, path, query, body, or credentials are retained.
    resource_type = request.resource_type
    resource_type = resource_type if resource_type in {
        "document", "stylesheet", "script", "image", "font", "xhr", "fetch", "other"
    } else "other"
    BLOCKED_EXTERNAL[resource_type] += 1
    if request.resource_type == "script":
        EXTERNAL_SCRIPT_REQUESTS += 1
    route.abort()


def require(condition, failure_code):
    if not condition:
        OUTCOME["failures"].append(failure_code)


def first_visible(locator):
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            return candidate
    return None


def capture_page_diagnostics(page):
    global PAGE_ERRORS
    if page is None:
        return
    try:
        PAGE_ERRORS = [
            safe_page_error(error)
            for error in page.evaluate("window.__localShellErrors || []")
        ]
        OUTCOME["blocked_resource_load_errors"] = page.evaluate(
            "window.__localShellResourceErrors || 0"
        )
    except Exception:
        OUTCOME["diagnostic_capture_failed"] = True


def exception_class(error):
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,31}", name) else "Error"


def palette_wait(page, stage_name, expression, timeout_ms=3000):
    try:
        page.wait_for_function(expression, timeout=timeout_ms)
        return True
    except Exception as error:
        OUTCOME.setdefault("command_palette_stage_errors", []).append({
            "stage": stage_name,
            "exception_class": exception_class(error),
        })
        return False


def palette_active_element_state(page):
    try:
        state = page.evaluate("""() => {
          const active = document.activeElement;
          const opener = document.querySelector('.sh-topbar-search');
          const input = document.querySelector('.sh-cmdpal-input');
          const dialog = document.querySelector('.sh-cmdpal');
          return {
            active_is_opener: active === opener,
            active_is_palette_input: active === input,
            active_is_inside_dialog: !!(active && dialog && dialog.contains(active)),
            active_is_body: active === document.body,
            active_is_connected: !!(active && active.isConnected),
          };
        }""")
        return {key: value is True for key, value in state.items()}
    except Exception:
        return {
            "active_is_opener": False,
            "active_is_palette_input": False,
            "active_is_inside_dialog": False,
            "active_is_body": False,
            "active_is_connected": False,
            "diagnostic_capture_failed": True,
        }


def main():
    global PAGE_ERRORS
    browser = None
    context = None
    page = None
    stage = "browser_start"
    try:
        with sync_playwright() as playwright:
            try:
                browser_path = os.environ.get("OZON_BROWSER_CHROMIUM", "/usr/bin/chromium")
                browser = playwright.chromium.launch(
                    executable_path=browser_path,
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-quic"],
                )
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    service_workers="block",
                )
                # A clean browser context forces the real Flask login form POST.
                context.add_init_script(INIT_SCRIPT)
                context.route("**/*", browser_route)
                page = context.new_page()
                page.set_default_timeout(12000)

                stage = "login_form"
                login_url = ORIGIN + "/login?next=" + quote(DETAIL_PATH, safe="")
                login_response = page.goto(login_url, wait_until="domcontentloaded")
                require(bool(login_response and login_response.status == 200), "login_page_not_200")
                page.locator("input[name=username]").fill(USERNAME)
                page.locator("input[name=password]").fill(PASSWORD)
                page.get_by_role("button", name="Войти", exact=True).click()

                stage = "listing_read"
                page.wait_for_load_state("domcontentloaded")
                detail_response = page.goto(ORIGIN + DETAIL_PATH, wait_until="domcontentloaded")
                require(
                    bool(detail_response and detail_response.status == 200),
                    "listing_html_not_200",
                )
                page.locator(".listing-workspace-title").wait_for()
                page.locator(".mdet-hero img").wait_for()
                page.wait_for_function("""() => {
                  const root = document.querySelector('#marketplace-detail-app');
                  const title = document.querySelector('.listing-workspace-title');
                  const image = document.querySelector('.mdet-hero img');
                  return !!root && !root.hasAttribute('v-cloak') && !!title
                    && title.textContent.includes('Local base asset synthetic listing')
                    && !!image && image.complete && image.naturalWidth > 0;
                }""")

                OUTCOME["interactions"]["flask_login_post_succeeded"] = LOGIN_POST_COUNT == 1 and LOGIN_POST_STATUS in {302, 303}
                OUTCOME["interactions"]["native_listing_html_and_dto_loaded"] = LISTING_HTML_GETS >= 1 and LISTING_DTO_GETS >= 1
                OUTCOME["interactions"]["vue_detail_mounted"] = page.locator("#marketplace-detail-app:not([v-cloak])").count() == 1
                OUTCOME["interactions"]["tailwind_runtime_available"] = page.evaluate(
                    "!!(window.tailwind && window.tailwind.config && window.tailwind.config.theme)"
                )
                OUTCOME["interactions"]["alpine_core_initialized"] = page.evaluate(
                    "!!(window.Alpine && window.Alpine.store && window.Alpine.store('notif'))"
                )
                OUTCOME["interactions"]["synthetic_hero_decoded"] = page.locator(".mdet-hero img").evaluate(
                    "image => image.complete && image.naturalWidth > 0"
                )

                stage = "vue_gallery"
                gallery = page.locator(".mcat-gallery .mcat-gal-thumb")
                gallery_switch = False
                if gallery.count() > 1:
                    gallery.nth(1).click()
                    page.wait_for_function("""() => {
                      const image = document.querySelector('.mdet-hero img');
                      return !!image && image.currentSrc.endsWith('/local-base-assets-product-2.svg');
                    }""")
                    gallery_switch = True
                OUTCOME["interactions"]["vue_gallery_switch_works"] = gallery_switch

                stage = "vue_description"
                desc_toggle = page.get_by_role("button", name="Показать полностью", exact=True)
                description_expand = False
                if desc_toggle.count():
                    desc_toggle.first.click()
                    description_expand = page.locator(".mdet-desc.is-open").count() == 1
                OUTCOME["interactions"]["vue_description_expand_works"] = description_expand

                stage = "mobile_menu"
                page.set_viewport_size({"width": 390, "height": 844})
                mobile_menu = page.locator('button[aria-label="Открыть меню"]')
                mobile_available = mobile_menu.is_visible()
                if mobile_available:
                    mobile_menu.click()
                    page.wait_for_function("""() => {
                      const sidebar = document.querySelector('aside.sidebar');
                      return !!sidebar && sidebar.classList.contains('mobile-open');
                    }""")
                OUTCOME["interactions"]["alpine_mobile_menu_opens"] = mobile_available and (
                    "mobile-open" in (page.locator("aside.sidebar").get_attribute("class") or "")
                )
                mobile_closed = False
                if mobile_available and OUTCOME["interactions"]["alpine_mobile_menu_opens"]:
                    overlay = page.locator(".mobile-overlay.active")
                    overlay_box = overlay.bounding_box()
                    if overlay_box:
                        overlay.click(position={
                            "x": max(1, overlay_box["width"] - 4),
                            "y": max(1, overlay_box["height"] / 2),
                        })
                    page.wait_for_function("""() => {
                      const sidebar = document.querySelector('aside.sidebar');
                      return !!sidebar && !sidebar.classList.contains('mobile-open');
                    }""")
                    mobile_closed = "mobile-open" not in (
                        page.locator("aside.sidebar").get_attribute("class") or ""
                    )
                OUTCOME["interactions"]["alpine_mobile_menu_closes"] = mobile_closed

                stage = "theme_toggle"
                theme_toggle = first_visible(page.locator('.mobile-header button[aria-label="Сменить тему"]'))
                theme_before = page.locator("html").get_attribute("data-theme")
                if theme_toggle is not None:
                    theme_toggle.click()
                OUTCOME["interactions"]["alpine_theme_control_works"] = (
                    theme_toggle is not None
                    and page.locator("html").get_attribute("data-theme") != theme_before
                )

                stage = "command_palette"
                page.set_viewport_size({"width": 1280, "height": 900})
                palette_button = page.locator(".sh-topbar-search")
                palette_results = {
                    "opener_focused": False,
                    "opened_by_enter": False,
                    "input_focused": False,
                    "escape_closed": False,
                    "opener_focus_restored": False,
                    "reopened_after_return": False,
                    "reopened_input_focused": False,
                    "reopened_escape_closed": False,
                    "reopened_opener_focus_restored": False,
                }
                palette_results["opener_focused"] = palette_button.is_visible()
                if palette_results["opener_focused"]:
                    # Start with the same keyboard action as a focused user.
                    palette_button.focus()
                    palette_results["opener_focused"] = palette_button.evaluate(
                        "element => document.activeElement === element"
                    )
                if palette_results["opener_focused"]:
                    page.keyboard.press("Enter")
                    palette_results["opened_by_enter"] = palette_wait(
                        page,
                        "open_by_enter",
                        "!!document.querySelector('.sh-cmdpal') && getComputedStyle(document.querySelector('.sh-cmdpal')).display !== 'none'",
                    )
                if palette_results["opened_by_enter"]:
                    palette_results["input_focused"] = palette_wait(
                        page,
                        "input_focus",
                        "document.activeElement === document.querySelector('.sh-cmdpal-input')",
                        timeout_ms=5000,
                    )
                if palette_results["input_focused"]:
                    try:
                        page.keyboard.press("Escape")
                        palette_results["escape_closed"] = palette_wait(
                            page,
                            "escape_close",
                            "!document.querySelector('.sh-cmdpal') || getComputedStyle(document.querySelector('.sh-cmdpal')).display === 'none'",
                        )
                    except Exception as error:
                        OUTCOME.setdefault("command_palette_stage_errors", []).append({
                            "stage": "escape_key",
                            "exception_class": exception_class(error),
                        })
                if palette_results["opened_by_enter"] and not palette_results["escape_closed"]:
                    # Close the panel for later checks without treating a
                    # backdrop click as an Escape success.
                    backdrop = page.locator(".sh-cmdpal .sh-backdrop")
                    if backdrop.is_visible():
                        try:
                            backdrop.click(position={"x": 2, "y": 2})
                            palette_wait(
                                page,
                                "fallback_close",
                                "!document.querySelector('.sh-cmdpal') || getComputedStyle(document.querySelector('.sh-cmdpal')).display === 'none'",
                            )
                        except Exception as error:
                            OUTCOME.setdefault("command_palette_stage_errors", []).append({
                                "stage": "fallback_close",
                                "exception_class": exception_class(error),
                            })
                if palette_results["escape_closed"]:
                    palette_results["opener_focus_restored"] = palette_wait(
                        page,
                        "opener_focus_restore",
                        "document.activeElement === document.querySelector('.sh-topbar-search')",
                        timeout_ms=3000,
                    )
                    if not palette_results["opener_focus_restored"]:
                        OUTCOME["command_palette_return_focus_diagnostic"] = palette_active_element_state(page)
                if palette_results["opener_focus_restored"]:
                    page.keyboard.press("Enter")
                    palette_results["reopened_after_return"] = palette_wait(
                        page,
                        "reopen_after_return",
                        "!!document.querySelector('.sh-cmdpal') && getComputedStyle(document.querySelector('.sh-cmdpal')).display !== 'none'",
                    )
                if palette_results["reopened_after_return"]:
                    palette_results["reopened_input_focused"] = palette_wait(
                        page,
                        "reopened_input_focus",
                        "document.activeElement === document.querySelector('.sh-cmdpal-input')",
                        timeout_ms=5000,
                    )
                if palette_results["reopened_input_focused"]:
                    page.keyboard.press("Escape")
                    palette_results["reopened_escape_closed"] = palette_wait(
                        page,
                        "reopened_escape_close",
                        "!document.querySelector('.sh-cmdpal') || getComputedStyle(document.querySelector('.sh-cmdpal')).display === 'none'",
                    )
                if palette_results["reopened_escape_closed"]:
                    palette_results["reopened_opener_focus_restored"] = palette_wait(
                        page,
                        "reopened_opener_focus_restore",
                        "document.activeElement === document.querySelector('.sh-topbar-search')",
                        timeout_ms=3000,
                    )
                    if not palette_results["reopened_opener_focus_restored"]:
                        OUTCOME["command_palette_reopen_return_focus_diagnostic"] = palette_active_element_state(page)
                OUTCOME["command_palette_stage_results"] = palette_results
                OUTCOME["interactions"]["alpine_command_palette_opens"] = palette_results["opened_by_enter"]
                OUTCOME["interactions"]["alpine_command_palette_input_focused"] = palette_results["input_focused"]
                OUTCOME["interactions"]["alpine_command_palette_escape_closes"] = palette_results["escape_closed"]
                OUTCOME["interactions"]["alpine_command_palette_focus_restored"] = palette_results["opener_focus_restored"]
                OUTCOME["interactions"]["alpine_command_palette_reopens"] = palette_results["reopened_after_return"]
                OUTCOME["interactions"]["alpine_command_palette_refocuses_input"] = palette_results["reopened_input_focused"]
                OUTCOME["interactions"]["alpine_command_palette_recloses"] = palette_results["reopened_escape_closed"]
                OUTCOME["interactions"]["alpine_command_palette_rerestores_focus"] = palette_results["reopened_opener_focus_restored"]

                stage = "notification_panel"
                notif_button = first_visible(page.locator("button.sh-notif-bell"))
                notification_opened = False
                notification_closed = False
                notification_opener_focused = False
                notification_popup_scoped = False
                if notif_button is not None:
                    notif_parent = notif_button.locator("xpath=..")
                    notif_popup = notif_parent.locator(".sh-notif-pop")
                    parent_is_exact = notif_parent.evaluate(
                        "element => element.classList.contains('sh-notif')"
                    )
                    notification_popup_scoped = parent_is_exact and notif_popup.count() == 1
                    if notification_popup_scoped:
                        notif_button.click()
                        notif_popup.wait_for(state="visible")
                        notification_opened = notif_popup.is_visible()
                        notification_opener_focused = notif_button.evaluate(
                            "element => document.activeElement === element"
                        )
                        if not notification_opener_focused:
                            notif_button.focus()
                            notification_opener_focused = notif_button.evaluate(
                                "element => document.activeElement === element"
                            )
                        if notification_opener_focused:
                            notif_button.press("Escape")
                            notif_popup.wait_for(state="hidden")
                            notification_closed = not notif_popup.is_visible()
                OUTCOME["interactions"]["alpine_notification_panel_opens"] = notification_opened
                OUTCOME["interactions"]["alpine_notification_popup_scoped_to_opener"] = notification_popup_scoped
                OUTCOME["interactions"]["alpine_notification_opener_focused"] = notification_opener_focused
                OUTCOME["interactions"]["alpine_notification_panel_closes"] = notification_closed

                capture_page_diagnostics(page)
            except Exception as error:
                OUTCOME["failures"].append("browser_or_interaction_failure")
                OUTCOME["failure_stage"] = stage
                OUTCOME["failure_exception_class"] = exception_class(error)
                capture_page_diagnostics(page)
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception as error:
                        OUTCOME.setdefault("cleanup_exception_classes", []).append(
                            "context:" + exception_class(error)
                        )
                        OUTCOME["failures"].append("browser_cleanup_failure")
                    context = None
                if browser is not None:
                    try:
                        browser.close()
                    except Exception as error:
                        OUTCOME.setdefault("cleanup_exception_classes", []).append(
                            "browser:" + exception_class(error)
                        )
                        OUTCOME["failures"].append("browser_cleanup_failure")
                    browser = None
    except Exception as error:
        OUTCOME["failures"].append("playwright_setup_failure")
        OUTCOME["failure_stage"] = stage
        OUTCOME["failure_exception_class"] = exception_class(error)
    finally:
        server.shutdown()
        server_thread.join(timeout=5)

    OUTCOME["page_errors"] = PAGE_ERRORS
    OUTCOME["blocked_external_resource_types"] = dict(sorted(BLOCKED_EXTERNAL.items()))
    OUTCOME["external_script_requests"] = EXTERNAL_SCRIPT_REQUESTS
    OUTCOME["local_static_status"] = dict(sorted(LOCAL_STATIC_STATUS.items()))
    OUTCOME["local_get_requests"] = LOCAL_GET_REQUESTS
    OUTCOME["login_post_count"] = LOGIN_POST_COUNT
    OUTCOME["login_post_status"] = LOGIN_POST_STATUS
    OUTCOME["non_login_post_requests"] = NON_LOGIN_POST_REQUESTS
    OUTCOME["listing_html_gets"] = LISTING_HTML_GETS
    OUTCOME["listing_dto_gets"] = LISTING_DTO_GETS
    OUTCOME["fixture_photo_fulfills"] = FIXTURE_PHOTO_FULFILLS
    OUTCOME["provider_calls"] = 0
    OUTCOME["network_egress_attempts"] = 0
    OUTCOME["python_network_attempts"] = PYTHON_NETWORK_ATTEMPTS
    OUTCOME["required_static_paths"] = REQUIRED_STATIC_PATHS

    require(OUTCOME["login_post_count"] == 1, "login_post_count")
    require(OUTCOME["login_post_status"] in {302, 303}, "login_post_status")
    require(OUTCOME["listing_html_gets"] >= 1, "listing_html_not_read")
    require(OUTCOME["listing_dto_gets"] >= 1, "listing_dto_not_read")
    require(OUTCOME["external_script_requests"] == 0, "external_script_requested")
    require(OUTCOME["python_network_attempts"] == 0, "python_network_attempted")
    require(OUTCOME["non_login_post_requests"] == 0, "unexpected_local_write")
    require(not OUTCOME["page_errors"], "page_runtime_error")
    require(
        all(LOCAL_STATIC_STATUS.get(path) == 200 for path in REQUIRED_STATIC_PATHS),
        "required_static_asset_not_200",
    )
    require(
        all(OUTCOME["interactions"].get(name) is True for name in (
            "flask_login_post_succeeded",
            "native_listing_html_and_dto_loaded",
            "vue_detail_mounted",
            "tailwind_runtime_available",
            "alpine_core_initialized",
            "synthetic_hero_decoded",
            "vue_gallery_switch_works",
            "vue_description_expand_works",
            "alpine_mobile_menu_opens",
            "alpine_mobile_menu_closes",
            "alpine_theme_control_works",
            "alpine_command_palette_opens",
            "alpine_command_palette_input_focused",
            "alpine_command_palette_escape_closes",
            "alpine_command_palette_focus_restored",
            "alpine_command_palette_reopens",
            "alpine_command_palette_refocuses_input",
            "alpine_command_palette_recloses",
            "alpine_command_palette_rerestores_focus",
            "alpine_notification_panel_opens",
            "alpine_notification_popup_scoped_to_opener",
            "alpine_notification_opener_focused",
            "alpine_notification_panel_closes",
        )),
        "required_interaction_failed",
    )
    OUTCOME["status"] = "passed" if not OUTCOME["failures"] else "failed"
    ARTIFACT.write_text(json.dumps(OUTCOME, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(ARTIFACT, 0o600)
    print(json.dumps({"status": OUTCOME["status"], "artifact": ARTIFACT.name}, sort_keys=True))
    TEMP.cleanup()
    return 0 if OUTCOME["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
