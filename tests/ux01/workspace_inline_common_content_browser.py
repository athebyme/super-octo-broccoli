"""Exercise the default in-catalog editor with a disposable seller database."""

import json
import re
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from tests.ux01 import common_content_browser as fixture
from tests.ux01.bulk_action_controls import invoke_workspace_action, workspace_action_launcher


EDITOR = "[data-inline-common-dialog]"
EDITOR_PAGE = "/my-products/common-content"
PREVIEW = "/api/my-products/common-content/preview"
APPLY = "/api/my-products/common-content/apply"
NEW_TITLE = "Название после локальной правки"
REPORT = {"status": "running", "layouts": []}


def catalog_settled(page, search):
    page.wait_for_function("""expected => {
        const vue = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
        return vue && !vue.loading && vue.itemsScope === vue.feedScopeKey() &&
            vue.filters.search === expected;
    }""", arg=search)


def title_edit(dialog, value):
    dialog.locator('[data-field-section="title"]').get_by_role(
        "button", name="Изменить значение", exact=True,
    ).click()
    textbox = dialog.get_by_role("textbox", name="Общее название товара")
    textbox.fill(value)
    return textbox


def editor_read(response):
    return urlsplit(response.url).path == EDITOR_PAGE and response.request.method == "GET"


def assert_editor_read(response, product_ids):
    assert response.status == 200
    assert not response.request.is_navigation_request()
    assert "application/json" in response.request.headers["accept"]
    directives = {value.strip().lower() for value in response.headers["cache-control"].split(",")}
    assert {"private", "no-store"} <= directives, directives
    assert fixture.query_ids(response.url) == product_ids
    payload = response.json()
    assert payload["selectedProductIds"] == product_ids
    assert len(payload["products"]) == len(product_ids)


def domain_snapshot(product_ids):
    with fixture.app.app_context():
        return fixture._bulk_product_snapshot(product_ids), fixture._bulk_audit_snapshot(product_ids)


def run():
    primary = fixture.FIXTURE["source_id"]
    secondary = fixture.FIXTURE["second_source_id"]
    original = fixture.FIXTURE["initial_title"]
    preview_requests = []
    feed_requests = []
    applies = []
    selection_reads = []
    product_ids = [primary, secondary]
    domain_before, audit_before = domain_snapshot(product_ids)
    with fixture.app.app_context():
        listings_before = fixture._model_snapshot(fixture.MarketplaceListing)

    def bridge(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.netloc == urlsplit(fixture.BASE).netloc:
            if request.method == "GET" and parsed.path == "/api/my-products":
                feed_requests.append(parse_qs(parsed.query))
            if request.method == "POST" and parsed.path == PREVIEW:
                ids = [item["product_id"] for item in request.post_data_json["items"]]
                preview_requests.append(ids)
                assert ids == [primary]
            if request.method == "POST" and parsed.path == APPLY:
                applies.append(parsed.path)
                assert len(applies) == 1
            if request.method == "POST" and parsed.path == "/api/my-products/selection":
                body = request.post_data_json
                assert body == {"ids": product_ids}
                provider_attempts_before = fixture.REPORT["provider_attempts"]
                response = route.fetch(max_redirects=0)
                assert response.status == 200
                payload = response.json()
                assert payload.get("success") is True
                returned_ids = [item["id"] for item in payload["items"]]
                assert returned_ids == product_ids
                selection_reads.append({
                    "requested_ids": body["ids"],
                    "returned_ids": returned_ids,
                    "provider_attempt_delta": fixture.REPORT["provider_attempts"] - provider_attempts_before,
                })
                route.fulfill(response=response)
                return
        fixture.bridge(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=fixture.CHROMIUM,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        for width, theme in ((320, "dark"), (1440, "light"), (320, "light")):
            context = browser.new_context(
                viewport={"width": width, "height": 900}, color_scheme=theme, has_touch=width <= 390,
            )
            context.add_cookies([fixture.authenticated_cookie()])
            context.add_init_script(f"localStorage.setItem('sh-theme', '{theme}');")
            context.route("**/*", bridge)
            page = context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: fixture.REPORT["javascript_errors"].append(str(error)))
            page.on("console", fixture.record_console_message)
            start = page.goto(fixture.BASE + "/my-products/beta", wait_until="domcontentloaded")
            assert start and start.status == 200
            catalog_settled(page, "")
            url = page.url
            page.evaluate("window.__workspaceDocument = document")
            row = page.locator(".mp-row").filter(has=page.locator(f'input[data-product-id="{primary}"]'))
            opener = row.locator("[data-common-content-row-link]")
            assert fixture.query_ids(opener.get_attribute("href")) == [primary]
            opener.focus()
            with page.expect_response(editor_read) as opened:
                page.keyboard.press("Enter")
            assert_editor_read(opened.value, [primary])
            dialog = page.locator(EDITOR)
            expect(dialog).to_be_visible()
            expect(dialog.get_by_role("heading", name="Данные товара", exact=True)).to_be_visible()
            expect(dialog.locator("#common-content-fields [data-action=preview]")).to_be_visible()
            expect(dialog.locator(".cpc-selection")).to_be_hidden()
            expect(dialog.get_by_role("note")).to_contain_text("Карточки WB и Ozon обновляются отдельно.")
            title_box = dialog.locator('[data-field-section="title"]').bounding_box()
            assert title_box and 0 <= title_box["y"] < title_box["y"] + title_box["height"] <= 900
            close = dialog.locator("[data-inline-common-close]")
            box = close.bounding_box()
            assert box and box["width"] >= 44 and box["height"] >= 44, box
            geometry = page.evaluate("""selector => {
                const dialog = document.querySelector(selector);
                const root = document.documentElement;
                const bounds = dialog.getBoundingClientRect();
                const editor = dialog.querySelector('[data-inline-common-editor]');
                return {viewport: root.clientWidth, document: root.scrollWidth,
                    dialog: dialog.clientWidth, dialogContent: dialog.scrollWidth,
                    editor: editor.clientWidth, editorContent: editor.scrollWidth,
                    left: bounds.left, right: bounds.right};
            }""", EDITOR)
            assert geometry["viewport"] == geometry["document"] == width
            assert geometry["dialog"] == geometry["dialogContent"]
            assert geometry["editor"] == geometry["editorContent"]
            assert 0 <= geometry["left"] < geometry["right"] <= width
            assert dialog.evaluate("el => el.contains(document.activeElement)")
            page.keyboard.press("Tab")
            assert dialog.evaluate("el => el.contains(document.activeElement)")
            assert page.evaluate("""() => {
                const active = document.activeElement;
                const style = getComputedStyle(active);
                return active.matches(':focus-visible') && style.outlineStyle !== 'none' &&
                    Number.parseFloat(style.outlineWidth) >= 2;
            }""")
            textbox = title_edit(dialog, "Несохранённое название")
            page.keyboard.press("Escape")
            discard = page.locator("[data-inline-common-discard-dialog]")
            expect(discard).to_be_visible()
            discard.locator("[data-inline-common-discard-cancel]").click()
            expect(discard).to_be_hidden()
            expect(textbox).to_have_value("Несохранённое название")
            page.screenshot(path=fixture.OUT / f"inline-editor-{width}-{theme}.png")
            page.keyboard.press("Escape")
            discard.locator("[data-inline-common-discard-confirm]").click()
            expect(dialog).to_be_hidden()
            expect(opener).to_be_focused()
            assert page.url == url and page.evaluate("window.__workspaceDocument === document")
            assert not applies and not preview_requests
            assert domain_snapshot(product_ids) == (domain_before, audit_before)
            REPORT["layouts"].append({"theme": theme, "width": width, "close_hit_area": [box["width"], box["height"]],
                                      "geometry": geometry, "discard_restored_catalog_focus": True})

            if theme == "dark":
                read_key = "GET " + EDITOR_PAGE
                fixture.delay_fetches(page, [read_key])
                opener.press("Enter")
                fixture.wait_for_delayed_fetch(page, read_key)
                expect(dialog).to_have_attribute("aria-busy", "true")
                page.keyboard.press("Escape")
                expect(dialog).to_be_hidden()
                with page.expect_response(editor_read) as fresh_open:
                    opener.press("Enter")
                assert_editor_read(fresh_open.value, [primary])
                fixture.release_delayed_fetch(page, read_key)
                expect(dialog.locator("#common-content-fields [data-action=preview]")).to_be_visible()
                expect(dialog.locator("[data-action=choose-product]")).to_have_count(1)
                expect(dialog.locator("[data-inline-common-retry]")).to_have_count(0)
                dialog.locator("[data-inline-common-close]").click()
                expect(opener).to_be_focused()
                assert page.url == url and page.evaluate("window.__workspaceDocument === document")
                REPORT["cancelled_read_reopens_cleanly"] = True

                def wrong_selection(route):
                    response = route.fetch(max_redirects=0)
                    assert response.status == 200
                    payload = response.json()
                    payload["selectedProductIds"] = [secondary]
                    route.fulfill(response=response, json=payload)

                page.route(re.compile(r"/my-products/common-content\?product_id=\d+$"), wrong_selection, times=1)
                opener.press("Enter")
                expect(dialog.get_by_role("alert")).to_contain_text("Ответ сервера не подтвердил точный состав")
                expect(dialog).to_have_attribute("aria-busy", "false")
                expect(dialog.locator("[data-inline-common-editor]")).to_be_hidden()
                expect(dialog.locator("[data-inline-common-retry]")).to_have_count(0)
                dialog.locator("[data-inline-common-close]").click()
                expect(opener).to_be_focused()
                assert domain_snapshot(product_ids) == (domain_before, audit_before)
                assert not applies and not preview_requests
                REPORT["unexpected_selection_rejected_without_mount"] = True
            if theme == "dark" or width > 320:
                context.close()
                continue

            for product_id in (primary, secondary):
                page.locator(f'.mp-row input[data-product-id="{product_id}"]').check()
            page.get_by_placeholder("Название, бренд или артикул").fill("Синтетический товар")
            catalog_settled(page, "Синтетический товар")
            scope = fixture.beta_catalog_state(page)
            assert scope["selected"] == [primary, secondary]
            assert scope["itemIds"] == [primary]
            page.evaluate("""() => {
                window.__explicitSelectionReviewClicks = 0;
                document.addEventListener('click', event => {
                    if (event.target.closest('[data-selection-review-show]')) {
                        window.__explicitSelectionReviewClicks += 1;
                    }
                }, true);
            }""")
            provider_attempts_before_action = fixture.REPORT["provider_attempts"]
            selection_read_key = "POST /api/my-products/selection"
            fixture.delay_fetches(page, [selection_read_key])

            def release_verified_selection_read(launcher):
                fixture.wait_for_delayed_fetch(page, selection_read_key)
                expect(launcher).to_have_attribute("aria-busy", "true")
                expect(launcher).to_have_attribute("aria-disabled", "true")
                expect(launcher).to_be_focused()
                launcher.press("Enter")
                assert page.locator(".mp-selection-review").count() == 0
                fixture.release_delayed_fetch(page, selection_read_key)

            with page.expect_response(editor_read) as bulk_opened:
                direct_action_steps = invoke_workspace_action(
                    page,
                    "common_content",
                    expected_selection_ids=product_ids,
                    selection_pending=release_verified_selection_read,
                )
            assert_editor_read(bulk_opened.value, [primary, secondary])
            assert direct_action_steps == 1
            assert selection_reads == [{
                "requested_ids": product_ids,
                "returned_ids": product_ids,
                "provider_attempt_delta": 0,
            }]
            assert page.evaluate("window.__explicitSelectionReviewClicks") == 0
            assert page.locator(".mp-selection-review").count() == 0
            assert fixture.REPORT["provider_attempts"] == provider_attempts_before_action
            expect(dialog.locator('[data-action="choose-product"]')).to_have_count(2)
            expect(dialog.locator(".cpc-selection")).to_be_visible()
            expect(dialog.locator('[data-field-section="title"]')).to_contain_text(original)
            title_edit(dialog, NEW_TITLE)
            dialog.get_by_role("button", name="Проверить изменения").click()
            expect(dialog.get_by_role("heading", name="Проверьте изменения товара")).to_be_visible()
            assert preview_requests == [[primary]] and not applies
            assert domain_snapshot(product_ids) == (domain_before, audit_before)
            dialog.locator('[data-action="acknowledge-preview"]').check()
            apply_key = "POST " + APPLY
            fixture.delay_fetches(page, [apply_key])
            dialog.get_by_role("button", name="Сохранить данные товара").click()
            fixture.wait_for_delayed_fetch(page, apply_key)
            page.keyboard.press("Escape")
            expect(dialog).to_be_visible()
            expect(discard).to_be_hidden()
            assert not applies
            fixture.release_delayed_fetch(page, apply_key)
            expect(dialog.get_by_text("Общий товар сохранён.", exact=False)).to_be_visible()
            feeds_before_close = len(feed_requests)
            dialog.locator("[data-inline-common-close]").click()
            expect(dialog).to_be_hidden()
            catalog_settled(page, scope["search"])
            expect(workspace_action_launcher(page, "cards")).to_be_focused()
            expect(row.locator("[data-common-content-row-link]")).to_have_text(NEW_TITLE)
            assert len(feed_requests) == feeds_before_close + 1
            assert feed_requests[-1]["search"] == [scope["search"]]
            after_save = fixture.beta_catalog_state(page)
            assert after_save["selected"] == [primary, secondary]
            assert after_save["itemIds"] == []
            retained_marks = page.evaluate("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy ? {
                    verified: proxy.selectionRowsVerified,
                    selected: proxy.selected.slice(),
                    usableSelectedItems: proxy.selectedItems.map(item => item.id),
                    reviewIds: proxy.selectionReviewIds.slice(),
                    reviewActive: proxy.selectionReviewActive,
                } : null;
            }""")
            assert retained_marks == {
                "verified": True,
                "selected": product_ids,
                "usableSelectedItems": product_ids,
                "reviewIds": product_ids,
                "reviewActive": False,
            }
            assert page.locator("[data-selection-review-show]").is_enabled()
            assert page.url == url and page.evaluate("window.__workspaceDocument === document")
            domain_after, audit_after = domain_snapshot(product_ids)
            assert domain_after[primary]["title"] == NEW_TITLE
            assert domain_after[secondary] == domain_before[secondary]
            assert len(audit_after) == len(audit_before) + 1
            with fixture.app.app_context():
                assert fixture._model_snapshot(fixture.MarketplaceListing) == listings_before
                draft = fixture.db.session.get(fixture.MarketplaceProductDraft, fixture.FIXTURE["draft_id"])
                assert draft.content_json == fixture.FIXTURE["draft_before"]["content_json"]
                assert draft.media_json == fixture.FIXTURE["draft_before"]["media_json"]
                assert draft.version == fixture.FIXTURE["draft_before"]["version"]
            REPORT["bulk"] = {"opened_products": 2, "hidden_marks_before_open": 1, "hidden_marks_retained": 2,
                              "preview_posts": 1,
                              "apply_posts": 1, "new_audit_rows": 1, "feed_refreshes": 1,
                              "marketplace_draft_unchanged": True, "marketplace_listings_unchanged": True,
                              "same_document": True,
                              "busy_blocks_dismiss": True}
            REPORT["selection_on_action"] = {
                "action_steps": direct_action_steps,
                "selection_reads": len(selection_reads),
                "requested_ids": selection_reads[0]["requested_ids"],
                "returned_ids": selection_reads[0]["returned_ids"],
                "explicit_review_clicks_before_open": 0,
                "review_panel_activated": False,
                "selection_read_busy": True,
                "focus_retained_while_pending": True,
                "rapid_reactivation_ignored": True,
                "editor_product_ids": product_ids,
                "provider_attempt_delta": selection_reads[0]["provider_attempt_delta"],
                "filter_empty_after_rename": after_save["itemIds"] == [],
                "retained_selected_ids": retained_marks["selected"],
                "retained_usable_selected_item_ids": retained_marks["usableSelectedItems"],
                "retained_selection_verified": retained_marks["verified"],
            }
            context.close()
        browser.close()

    for name in ("javascript_errors", "console_errors", "unexpected_http_requests",
                 "unexpected_external_requests", "provider_attempts"):
        REPORT[name] = fixture.REPORT[name]
        assert not REPORT[name], name
    assert fixture.REPORT["synthetic_actions"]["preview_requests"] == len(preview_requests) == 1
    assert fixture.REPORT["synthetic_actions"]["apply_requests"] == len(applies) == 1
    REPORT["status"] = "passed"


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        REPORT["status"] = "failed"
        REPORT["error"] = str(error)[:600]
        for name in ("javascript_errors", "console_errors", "unexpected_http_requests",
                     "unexpected_external_requests", "provider_attempts"):
            REPORT[name] = fixture.REPORT[name]
        raise
    finally:
        fixture.REPORT_PATH.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        fixture.server.shutdown()
        fixture.server_thread.join(timeout=5)
        fixture.TEMP.cleanup()
