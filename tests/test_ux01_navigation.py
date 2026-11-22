"""Server-rendered UX-01 seller navigation contracts."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import json
import shutil
import subprocess
import unittest
from types import SimpleNamespace

from flask import Flask, render_template


REPOSITORY = Path(__file__).resolve().parents[1]
BASELINE_FIXTURE = REPOSITORY / "tests/ux01/baseline-actions.json"


class _NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.groups = []
        self.links = []
        self.alternates = []
        self.disclosures = []
        self._current_link = None
        self._current_target = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "button" and "seller-workspace-group-toggle" in classes:
            self.groups.append(attributes)
        if tag == "details" and "data-workspace-alt-nav" in attributes:
            self.disclosures.append(attributes)
        if tag == "a" and (
            "seller-workspace-root-link" in classes
            or "seller-workspace-alt-link" in classes
            or "seller-workspace-primary-link" in classes
            or "seller-workspace-sublink" in classes
        ):
            target = "alternates" if "seller-workspace-alt-link" in classes else "links"
            collection = self.alternates if target == "alternates" else self.links
            collection.append((attributes, ""))
            self._current_link = len(collection) - 1
            self._current_target = target

    def handle_data(self, data):
        if self._current_link is not None:
            collection = self.alternates if self._current_target == "alternates" else self.links
            attributes, text = collection[self._current_link]
            collection[self._current_link] = (attributes, text + data)

    def handle_endtag(self, tag):
        if tag == "a":
            if self._current_link is not None:
                collection = self.alternates if self._current_target == "alternates" else self.links
                attributes, text = collection[self._current_link]
                collection[self._current_link] = (attributes, text.strip())
            self._current_link = None
            self._current_target = None


class SellerWorkspaceNavigationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder=str(REPOSITORY / "templates"))
        cls.app.config.update(TESTING=True, MARKETPLACE_OZON_ENABLED=True)
        cls.app.jinja_env.globals["url_for"] = (
            lambda endpoint, *args, **kwargs: "/synthetic/" + endpoint
        )
        cls.current_nav_account = [41]
        cls.app.jinja_env.globals["mp_nav"] = lambda: SimpleNamespace(
            last_account_id=cls.current_nav_account[0]
        )

        cls.endpoint_cases = {
            "dashboard": ("overview", "Главная"),
            "dashboard_beta": ("overview", "Очередь внимания"),
            "seller_my_products": ("", "Мои товары"),
            "seller_my_products_beta": ("", "Мои товары"),
            "seller_product_wb_preview": ("", "Мои товары"),
            "prices.prices_dashboard": ("prices", "Цены"),
            "bulk_edit_history": ("operations", "Операции"),
            "supplier_catalog_product_detail": ("products", "Каталоги поставщиков"),
            "supplier_catalog_export": ("products", "Каталоги поставщиков"),
            "marketplace_listings.detail": ("products", "Карточки Ozon"),
            "marketplace_drafts.detail": ("products", "Черновики Ozon"),
            "marketplace_operations.detail": ("operations", "Операции"),
            "marketplace_inbox.thread": ("communication", "Общение"),
            "marketplace_analytics_page": ("analytics", "Аналитика"),
            "marketplace_finance.detail": ("analytics", "Финансы Ozon"),
            "marketplace_commercial.detail": ("prices", "Цены"),
        }

        for endpoint in cls.endpoint_cases:
            path = "/case/" + endpoint.replace(".", "-")
            cls.app.add_url_rule(
                path,
                endpoint=endpoint,
                view_func=cls._render_nav,
            )
        cls.app.add_url_rule(
            "/case/settings-health",
            endpoint="ozon_account_health.page",
            view_func=cls._render_settings_nav,
        )

    @staticmethod
    def _render_nav():
        return render_template(
            "partials/seller_workspace_nav.html",
            config=SellerWorkspaceNavigationTest.app.config,
        )

    @staticmethod
    def _render_settings_nav():
        return render_template(
            "partials/seller_workspace_settings_nav.html",
            config=SellerWorkspaceNavigationTest.app.config,
        )

    def _render(self, endpoint):
        response = self.app.test_client().get(
            "/case/" + endpoint.replace(".", "-")
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        parser = _NavigationParser()
        parser.feed(body)
        return body, parser

    def test_deep_routes_keep_their_group_and_parent_link_active(self):
        for endpoint, (expected_group, expected_link) in self.endpoint_cases.items():
            with self.subTest(endpoint=endpoint):
                body, parser = self._render(endpoint)
                self.assertIn(f'data-active-group="{expected_group}"', body)
                all_links = parser.links + parser.alternates
                active_links = [text.strip() for attributes, text in all_links
                                if "active" in (attributes.get("class") or "").split()]
                self.assertTrue(
                    any(expected_link in label for label in active_links),
                    active_links,
                )

    def test_six_high_frequency_roots_are_direct_and_other_routes_share_one_disclosure(self):
        body, parser = self._render("seller_my_products_beta")
        roots = [(attributes, text) for attributes, text in parser.links]
        self.assertEqual(
            [(attributes.get("data-workspace-root"), text) for attributes, text in roots],
            [
                ("home", "Главная"),
                ("products", "Мои товары"),
                ("prices", "Цены"),
                ("operations", "Операции"),
                ("communication", "Общение"),
                ("analytics", "Аналитика"),
            ],
        )
        self.assertIn('data-active-group=""', body)
        primary_links = [
            (attributes, text.strip())
            for attributes, text in parser.links
            if "seller-workspace-primary-link" in attributes.get("class", "").split()
        ]
        self.assertEqual(len(primary_links), 1)
        self.assertEqual(primary_links[0][1], "Мои товары")
        self.assertEqual(primary_links[0][0].get("href"), "/synthetic/seller_my_products_beta")
        self.assertEqual(primary_links[0][0].get("aria-current"), "page")
        self.assertEqual(len(parser.disclosures), 1)
        self.assertEqual(parser.groups, [])
        self.assertNotIn("open", parser.disclosures[0])
        self.assertIn('aria-label="Другие разделы и инструменты"', body)
        self.assertIn("Мониторинг конкурентов", [text for _attrs, text in parser.alternates])
        self.assertIn("Контент-фабрики", [text for _attrs, text in parser.alternates])

        tools_body, tools_nav = self._render("marketplace_drafts.detail")
        self.assertIn('data-active-group="products"', tools_body)
        self.assertNotIn("open", tools_nav.disclosures[0])
        self.assertIn("is-current", tools_body)
        self.assertTrue(any(text == "Черновики Ozon" for _attrs, text in tools_nav.alternates))
        self.assertFalse(any(text == "Автопубликация" for _attrs, text in tools_nav.alternates))

    def test_contextual_sibling_routes_are_not_repeated_in_global_alternate_list(self):
        body, _parser = self._render("prices.prices_dashboard")
        for endpoint in (
            "auto_import_pricing",
            "price_monitor_settings",
            "suspicious_price_changes",
            "prices.prices_history",
        ):
            self.assertNotIn("/synthetic/" + endpoint, body)

        operations_body, _parser = self._render("bulk_edit_history")
        self.assertNotIn("/synthetic/ozon_bulk_uploads.index", operations_body)
        self.assertNotIn("/synthetic/marketplace_operations.index", operations_body)

        settings_body = self.app.test_client().get("/case/settings-health").get_data(as_text=True)
        self.assertNotIn("Центр уведомлений", settings_body)
        self.assertNotIn("/synthetic/notifications_page", settings_body)

    def test_ozon_alternates_stay_behind_the_existing_feature_flag(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        try:
            body, _parser = self._render("seller_my_products_beta")
            self.assertNotIn("Карточки Ozon", body)
            self.assertNotIn("Черновики Ozon", body)
            self.assertNotIn("Заказы и возвраты Ozon", body)
            self.assertNotIn("marketplace_listings.index", body)
            self.assertNotIn("marketplace_drafts.index", body)
        finally:
            self.app.config["MARKETPLACE_OZON_ENABLED"] = True

    def test_primary_products_link_distinguishes_current_page_from_related_location(self):
        for endpoint, expected_current in (
            ("seller_my_products_beta", "page"),
            ("seller_my_products", "location"),
            ("seller_product_wb_preview", "location"),
            ("marketplace_drafts.detail", "location"),
            ("supplier_catalog_product_detail", "location"),
        ):
            with self.subTest(endpoint=endpoint):
                _body, parser = self._render(endpoint)
                primary_links = [
                    attributes for attributes, _text in parser.links
                    if "seller-workspace-primary-link"
                    in attributes.get("class", "").split()
                ]
                self.assertEqual(len(primary_links), 1)
                self.assertEqual(
                    primary_links[0].get("aria-current"), expected_current,
                )
                self.assertIn("active", primary_links[0].get("class", "").split())

    def test_exact_account_health_link_requires_a_selected_account(self):
        path = "/case/settings-health"
        self.current_nav_account[0] = None
        no_selection = self.app.test_client().get(path).get_data(as_text=True)
        self.assertNotIn("Состояние кабинета Ozon", no_selection)
        self.assertNotIn("ozon_account_health.page", no_selection)

        self.current_nav_account[0] = 41
        selected = self.app.test_client().get(path).get_data(as_text=True)
        self.assertIn("href=\"/synthetic/ozon_account_health.page\"", selected)
        self.assertIn("Состояние кабинета Ozon", selected)
        self.assertIn("is-active", selected)

    def test_baseline_base_destinations_remain_in_shell_or_workspace_menu(self):
        baseline = json.loads(BASELINE_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(baseline["baseline"], "ba63371")
        self.assertEqual(baseline["source_template"], "templates/base.html")
        self.assertRegex(baseline["source_sha256"], r"^[a-f0-9]{64}$")
        current = "\n".join(
            (REPOSITORY / path).read_text(encoding="utf-8")
            for path in (
                "templates/base.html",
                "templates/my_products_beta.html",
                "templates/partials/seller_workspace_nav.html",
                "templates/partials/seller_workspace_utility.html",
                "templates/partials/seller_workspace_settings_nav.html",
                "templates/partials/pricing_workspace_nav.html",
                "templates/partials/operations_workspace_history_nav.html",
            )
        )
        endpoint = re.compile(r"url_for\(['\"]([^'\"]+)")
        original_destinations = set(baseline["endpoints"])
        current_destinations = set(endpoint.findall(current))
        self.assertTrue(original_destinations)
        self.assertEqual(
            sorted(original_destinations - current_destinations),
            [],
            "UX-01 must preserve every destination from the old shell",
        )

    def test_command_palette_open_preserves_typed_query_and_guards_delayed_focus(self):
        node = shutil.which("node")
        self.assertTrue(node, "Node is required to exercise the production command palette")
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const root = process.cwd();
const uiSource = fs.readFileSync(root + '/static/sh-ui.js', 'utf8');
const macroSource = fs.readFileSync(root + '/templates/macros/overlays.html', 'utf8');
const paletteSource = uiSource.match(/window\.shCmdPalette = function \(\) \{[\s\S]*?\n    \};/);
const xInitMatch = macroSource.match(/x-init="([^\"]*\$watch\('[^\"]+)"/);
assert.ok(paletteSource, 'actual shCmdPalette production function must be found');
assert.ok(xInitMatch, 'actual command_palette x-init expression must be found');
const xInit = xInitMatch[1].replace(/\{\{ id \}\}/g, 'cmdOpen');
const pendingTimers = new Map();
let nextTimerId = 0;
const context = {
    window: {},
    setTimeout(callback, delay) {
        const id = ++nextTimerId;
        pendingTimers.set(id, {callback, delay});
        return id;
    },
    clearTimeout(id) { pendingTimers.delete(id); },
    fetch() { throw new Error('Command palette search was not expected to execute'); },
};
vm.runInNewContext(paletteSource[0], context, {filename: 'static/sh-ui.js#shCmdPalette'});

function createScenario() {
    let clickedHref = null;
    const classes = new WeakMap();
    const items = [
        {label: 'Главная', hint: 'Обзор', href: '/dashboard'},
        {label: 'Карточки кабинетов', hint: 'Listing · канал и кабинет', href: '/marketplaces/listings/'},
    ].map(row => {
        const itemClasses = new Set(['sh-cmdpal-item']);
        const item = {
            ...row,
            style: {display: ''},
            textContent: row.label + ' ' + row.hint,
            innerText: row.label + '\n' + row.hint,
            classList: {
                add(name) { itemClasses.add(name); },
                remove(name) { itemClasses.delete(name); },
                contains(name) { return itemClasses.has(name); },
            },
            scrollIntoView() {},
            click() { clickedHref = row.href; },
        };
        classes.set(item, itemClasses);
        return item;
    });
    const productList = {innerHTML: ''};
    const productGroup = {style: {display: 'none'}};
    const paletteRoot = {
        querySelectorAll(selector) {
            if (selector === '.sh-cmdpal-item:not([data-cmd-product])' || selector === '.sh-cmdpal-item') return items;
            if (selector === '.sh-cmdpal-group:not(.sh-cmdpal-products-group)') return [];
            if (selector === '.sh-cmdpal-item.active') return items.filter(item => classes.get(item).has('active'));
            return [];
        },
        querySelector(selector) {
            if (selector === '[data-cmd-products]') return productList;
            if (selector === '.sh-cmdpal-products-group') return productGroup;
            return null;
        },
    };
    const panel = {
        visible: true,
        contains(node) { return node === input || items.includes(node); },
        getClientRects() { return this.visible ? [{}] : []; },
    };
    const document = {activeElement: {kind: 'body'}};
    let focusCalls = 0;
    const input = {
        value: '',
        closest(selector) { return selector === '.sh-cmdpal-panel' ? panel : null; },
        focus() { focusCalls += 1; document.activeElement = input; },
    };
    const palette = context.window.shCmdPalette();
    palette.$root = paletteRoot;
    let refreshCalls = 0;
    const actualRefresh = palette.refresh;
    palette.refresh = function () {
        refreshCalls += 1;
        return actualRefresh.call(this);
    };
    const scopeTarget = {
        cmdOpen: false,
        get query() { return palette.query; },
        set query(value) { palette.query = value; },
        refresh: palette.refresh.bind(palette),
        $watch(name, callback) {
            assert.equal(name, 'cmdOpen');
            this.watcher = callback;
        },
        $nextTick(callback) { this.nextTicks.push(callback); },
        $refs: {cmdInput: input},
        watcher: null,
        nextTicks: [],
        document,
    };
    const scope = new Proxy(scopeTarget, {
        has(_target, key) { return key !== Symbol.unscopables; },
        get(target, key, receiver) {
            if (key === Symbol.unscopables) return undefined;
            return Reflect.get(target, key, receiver);
        },
    });
    new Function('scope', `with (scope) { ${xInit}; }`)(scope);
    assert.equal(typeof scopeTarget.watcher, 'function');
    return {
        palette, scopeTarget, items, input, panel, document,
        open() {
            scopeTarget.cmdOpen = true;
            scopeTarget.watcher(true);
        },
        close() { scopeTarget.cmdOpen = false; panel.visible = false; },
        flushNextTick() {
            const callbacks = scopeTarget.nextTicks.splice(0);
            callbacks.forEach(callback => callback());
        },
        get refreshCalls() { return refreshCalls; },
        get focusCalls() { return focusCalls; },
        get clickedHref() { return clickedHref; },
    };
}

const typed = createScenario();
typed.palette.query = 'old query';
typed.palette.filter();
typed.open();
assert.equal(typed.palette.query, '', 'opening refreshes synchronously before input');
assert.equal(typed.refreshCalls, 1, 'open performs one synchronous refresh');
typed.input.value = 'Карточки кабинетов';
typed.palette.query = typed.input.value; // Alpine x-model update from input
typed.palette.filter(); // actual production @input handler
typed.document.activeElement = typed.input; // typing/fill already focused the input
assert.equal(typed.items.find(item => item.classList.contains('active')).href, '/marketplaces/listings/');
typed.flushNextTick();
assert.equal(typed.palette.query, 'Карточки кабинетов', 'nextTick must not erase the typed query');
assert.equal(typed.items.filter(item => item.classList.contains('active')).length, 1);
assert.equal(typed.items.find(item => item.classList.contains('active')).href, '/marketplaces/listings/');
assert.equal(typed.refreshCalls, 1, 'nextTick must not refresh a second time');
assert.equal(typed.focusCalls, 0, 'nextTick must not steal focus already inside the palette');
typed.palette.choose(); // actual @keydown.enter handler
assert.equal(typed.clickedHref, '/marketplaces/listings/');

const defaultFocus = createScenario();
defaultFocus.open();
defaultFocus.flushNextTick();
assert.equal(defaultFocus.focusCalls, 1, 'open focuses the input when focus remains outside');
assert.equal(defaultFocus.document.activeElement, defaultFocus.input);

const resultFocus = createScenario();
resultFocus.open();
resultFocus.document.activeElement = resultFocus.items[0];
resultFocus.flushNextTick();
assert.equal(resultFocus.focusCalls, 0, 'open does not steal focus already inside the panel');

const closedBeforeNextTick = createScenario();
closedBeforeNextTick.open();
closedBeforeNextTick.close();
closedBeforeNextTick.flushNextTick();
assert.equal(closedBeforeNextTick.focusCalls, 0, 'closed palette never receives delayed focus');

const hiddenBeforeNextTick = createScenario();
hiddenBeforeNextTick.open();
hiddenBeforeNextTick.panel.visible = false;
hiddenBeforeNextTick.flushNextTick();
assert.equal(hiddenBeforeNextTick.focusCalls, 0, 'hidden panel never receives delayed focus');
console.log(JSON.stringify({status: 'passed', checks: [
    'actual_palette_and_macro_expression_preserve_typed_query_and_enter_destination',
    'open_focuses_input_only_when_focus_remains_outside_panel',
    'already_in_panel_focus_is_preserved',
    'closed_or_hidden_palette_receives_no_delayed_focus',
]}));
"""
        result = subprocess.run(
            [node, "-e", script],
            cwd=REPOSITORY,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["checks"]), 4)


if __name__ == "__main__":
    unittest.main()
