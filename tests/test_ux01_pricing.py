import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from flask import Flask, render_template, render_template_string
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = str(ROOT / "templates")


def _app(*, ozon_enabled=True):
    app = Flask(__name__, template_folder=TEMPLATES)
    app.config.update(
        TESTING=True,
        SECRET_KEY="ux01-pricing-template-test",
        MARKETPLACE_OZON_ENABLED=ozon_enabled,
    )
    app.jinja_loader = ChoiceLoader([
        DictLoader({
            "base.html": (
                "{% block title %}{% endblock %}"
                "{% block extra_head %}{% endblock %}"
                "<main>{% block content %}{% endblock %}</main>"
            ),
        }),
        FileSystemLoader(TEMPLATES),
    ])
    endpoints = {
        "prices.prices_dashboard": "/prices/",
        "prices.prices_change": "/prices/change",
        "prices.prices_history": "/prices/history",
        "prices.prices_settings": "/prices/settings",
        "auto_import_pricing": "/pricing",
        "price_monitor_settings": "/price-monitor/settings",
        "suspicious_price_changes": "/price-monitor/suspicious",
        "marketplace_commercial.index": "/marketplaces/commercial/",
        "api_settings": "/settings/api",
        "ozon_bulk_uploads.index": "/marketplaces/ozon/uploads/",
        "bulk_edit_history": "/bulk-history",
        "products_merge_history_list": "/products/merge/history",
        "marketplace_operations.index": "/marketplaces/operations/",
    }
    for endpoint, path in endpoints.items():
        app.add_url_rule(path, endpoint=endpoint, view_func=lambda: "")
    app.jinja_env.globals["mp_nav"] = lambda: SimpleNamespace(
        last_account_id=92,
        ozon_enabled=True,
        accounts=[
            SimpleNamespace(id=41, label="Synthetic one"),
            SimpleNamespace(id=42, label="Synthetic two"),
        ],
    )
    return app


def test_pricing_navigation_preserves_account_and_respects_ozon_gate():
    app = _app()
    with app.test_request_context("/prices/?account_id=41"):
        html = render_template(
            "partials/pricing_workspace_nav.html",
            pricing_workspace_current="wb-current",
        )
    assert 'href="/marketplaces/commercial/?account_id=41"' in html
    assert 'aria-current="page"' in html
    assert '<span class="pricing-workspace-link is-current" aria-current="page" data-current-href="/prices/">Текущие цены WB</span>' in html
    assert '<a href="/prices/"' not in html

    disabled = _app(ozon_enabled=False)
    with disabled.test_request_context("/prices/"):
        html = render_template(
            "partials/pricing_workspace_nav.html",
            pricing_workspace_current="wb-current",
        )
    assert "Предложения Ozon" not in html
    assert "marketplaces/commercial" not in html


def test_prices_dashboard_has_one_current_wb_label_and_explicit_ozon_shop_destinations():
    app = _app()
    with app.test_request_context("/prices/"):
        nav = render_template(
            "partials/pricing_workspace_nav.html",
            pricing_workspace_current="wb-current",
            pricing_workspace_hide_ozon_context=True,
        )
        channels = render_template_string(
            '{% from "partials/pricing_workspace_nav.html" import render_pricing_channel_nav %}'
            "{{ render_pricing_channel_nav() }}"
        )

    assert "Предложения Ozon" not in nav
    assert '<a href="/prices/"' not in nav
    assert '<span class="sh-channel-tab sh-channel-tab--active" aria-current="page">' in channels
    assert "Wildberries" in channels
    assert 'href="/marketplaces/commercial/?account_id=41"' in channels
    assert 'href="/marketplaces/commercial/?account_id=42"' in channels
    assert channels.count("class=\"sh-channel-tab\"") == 2


def test_operations_history_context_preserves_all_accounts_and_marks_current_without_link():
    app = _app()
    with app.test_request_context("/marketplaces/operations/?account_id="):
        all_accounts = render_template(
            "partials/operations_workspace_history_nav.html",
            operations_history_current="ozon-operations",
            filters=SimpleNamespace(account_id=""),
        )
    assert 'href="/marketplaces/ozon/uploads/?account_id="' in all_accounts
    assert 'href="/marketplaces/ozon/uploads/?account_id=92"' not in all_accounts
    assert '<span class="operations-history-link is-current" aria-current="page" data-current-href="/marketplaces/operations/?account_id=">Операции Ozon</span>' in all_accounts

    with app.test_request_context("/marketplaces/operations/?account_id=41"):
        one_account = render_template(
            "partials/operations_workspace_history_nav.html",
            operations_history_current="ozon-operations",
            filters=SimpleNamespace(account_id="41"),
        )
    assert 'href="/marketplaces/ozon/uploads/?account_id=41"' in one_account
    assert '<a href="/marketplaces/operations/?account_id=41"' not in one_account


def test_ozon_facts_call_buyer_price_and_marketplace_discount_unknown():
    app = _app()
    with app.app_context():
        html = render_template("partials/pricing_workspace_ozon_facts.html")
    assert "Цена в заявке — цена продавца" in html
    visible_price_fact = "Цена покупателя и скидка площадки здесь неизвестны"
    assert visible_price_fact in html and html.index(visible_price_fact) < html.index("<details>")
    assert "валюта неизвестна" in html
    assert "точному выбранному складу FBS/rFBS" in html
    assert "Зачёркнутая цена сама по себе не подтверждает цену покупателя" in html


def test_classic_proposal_labels_snapshot_and_unknown_currency_without_rub_fallback():
    app = _app()
    operation_routes = {
        "marketplace_commercial.index": "/marketplaces/commercial/",
        "marketplace_operations.detail": "/marketplaces/operations/3",
    }
    for endpoint, path in operation_routes.items():
        if endpoint not in app.view_functions:
            app.add_url_rule(path, endpoint=endpoint, view_func=lambda: "")

    proposal = SimpleNamespace(
        id=3,
        status="applied",
        proposal_kind="price",
        account_id=41,
        account=SimpleNamespace(label="Synthetic account"),
        source="user",
        listing=SimpleNamespace(offer_id="synthetic-offer"),
        operation=None,
        version=2,
        rollback_of_operation_id=None,
        baseline_fingerprint="a" * 64,
        proposed_fingerprint="b" * 64,
        error_code=None,
        error_message=None,
    )
    proposal_data = {
        "baseline_state": {"price": "1500"},
        "proposed_state": {"price": "1200"},
        "guardrails": {"direction": "decrease", "change_pct": 20},
        "write_quarantine": None,
        "target_available": True,
        "target_unavailable_reason": None,
    }
    with app.test_request_context("/marketplaces/commercial/classic/3"):
        html = render_template(
            "marketplace_commercial_detail_classic.html",
            proposal=proposal,
            proposal_data=proposal_data,
            write_enabled=False,
        )

    assert "Снимок до предложения" in html
    assert "Снимок состояния Ozon при создании заявки" in html
    assert html.count("валюта неизвестна") >= 2
    assert "Сейчас в Ozon" not in html
    assert "currency_code&#39;,&#39;RUB&#39;" not in html


def test_money_formatter_never_leaves_amount_bare_without_currency():
    node = shutil.which("node")
    if not node:
        raise AssertionError("Node.js is required for the Ozon value presentation test")
    script = r"""
const fs = require('fs');
let options;
global.location = { search: '' };
global.document = {
  getElementById(id) {
    if (id === 'ozon-commercial-app') return {};
    if (id === 'oc-bootstrap') return { textContent: JSON.stringify({ mode: 'list', filters: {}, base: '/', catalog: '/', writeEnabled: false }) };
    return null;
  },
  querySelector() { return null; },
  querySelectorAll() { return []; }
};
global.window = {
  Vue: { createApp(value) { options = value; return { directive() { return this; }, mount() {} }; } },
  mcatShared: { ozonPrices: {}, imageDeadline: {} }
};
global.Vue = global.window.Vue;
eval(fs.readFileSync(process.argv[1], 'utf8'));
const money = options.methods.money;
process.stdout.write(JSON.stringify([
  money('1500.5', 'RUB'),
  money('20', 'USD'),
  money('20', null),
  money('20', 'rub')
]));
"""
    result = subprocess.run(
        [node, "-e", script, str(ROOT / "static/ozon-commercial.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    rub, usd, missing, invalid = json.loads(result.stdout)
    assert rub.endswith("₽")
    assert usd.endswith("USD")
    assert "валюта неизвестна" in missing
    assert "валюта неизвестна" in invalid
    assert missing.startswith("20")
    assert invalid.startswith("20")


def test_price_change_template_displays_nullable_supplier_price_and_preserves_zero_values():
    node = shutil.which("node")
    if not node:
        raise AssertionError("Node.js is required for the price value presentation test")
    script = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[1], 'utf8');
const supplier = html.match(/x-text="(product\.supplier_price == null \?[^\"]+)"/);
const wb = html.match(/x-text="(product\.wb_price\.toLocaleString[^\"]+)"/);
const discounted = html.match(/x-text="(product\.wb_discounted_price != null \?[^\"]+)"/);
const current = html.match(/x-text="(product\.price != null \?[^\"]+)"/);
if (!supplier || !wb || !discounted || !current) throw new Error('expected live display expressions are missing');
const evaluate = (expression, product) => new Function('product', `return ${expression}`)(product);
const results = {
  supplierNull: evaluate(supplier[1], {supplier_price:null}),
  supplierUndefined: evaluate(supplier[1], {}),
  supplierZero: evaluate(supplier[1], {supplier_price:0}),
  wbZero: evaluate(wb[1], {wb_price:0}),
  discountedZero: evaluate(discounted[1], {wb_discounted_price:0}),
  currentZero: evaluate(current[1], {price:0})
};
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(
        [node, "-e", script, str(ROOT / "templates/prices_change.html")],
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(result.stdout)
    assert values["supplierNull"] == "нет данных"
    assert values["supplierUndefined"] == "нет данных"
    assert values["supplierZero"] == "0 ₽"
    assert values["wbZero"] == "0 ₽"
    assert values["discountedZero"] == "0 ₽"
    assert values["currentZero"] == "0 ₽"


def test_price_tables_have_keyboard_accessible_local_scroll_regions():
    for filename, label in (
        ("prices_dashboard.html", "Недавние пакетные изменения цен Wildberries"),
        ("prices_change.html", "Предпросмотр цен товаров Wildberries"),
        ("prices_history.html", "История изменений цен Wildberries"),
        ("prices_batch_detail.html", "Цены и статусы товаров в пакете Wildberries"),
        ("pricing_settings.html", "Диапазоны формулы цены поставщика"),
        ("suspicious_price_changes.html", "Подозрительные изменения цен Wildberries"),
    ):
        text = (ROOT / "templates" / filename).read_text()
        assert "class=\"pricing-table-scroll\"" in text
        assert "role=\"region\"" in text
        assert "tabindex=\"0\"" in text
        assert label in text
