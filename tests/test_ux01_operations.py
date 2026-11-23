from pathlib import Path
from datetime import datetime
from types import SimpleNamespace

from flask import Flask, render_template
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = str(ROOT / "templates")


def _app(*, ozon_enabled=True):
    app = Flask(__name__, template_folder=TEMPLATES)
    app.config.update(
        TESTING=True,
        SECRET_KEY="ux01-operations-template-test",
        MARKETPLACE_OZON_ENABLED=ozon_enabled,
    )
    app.jinja_loader = ChoiceLoader([
        DictLoader({
            "base.html": (
                "{% block title %}{% endblock %}"
                "{% block extra_head %}{% endblock %}"
                "<main>{% block content %}{% endblock %}</main>"
            ),
            "partials/ozon_result_status.html": "",
        }),
        FileSystemLoader(TEMPLATES),
    ])
    endpoints = {
        "bulk_edit_history": "/bulk-history",
        "bulk_edit_history_detail": "/bulk-history/<int:bulk_id>",
        "bulk_edit_export": "/bulk-history/<int:bulk_id>/export",
        "products_bulk_edit": "/products/bulk-edit",
        "prices.prices_dashboard": "/prices/",
        "prices.prices_change": "/prices/change",
        "prices.prices_history": "/prices/history",
        "prices.prices_settings": "/prices/settings",
        "products_merge_history_list": "/products/merge/history",
        "auto_import_pricing": "/pricing",
        "price_monitor_settings": "/price-monitor/settings",
        "suspicious_price_changes": "/price-monitor/suspicious",
        "ozon_bulk_uploads.index": "/marketplaces/ozon/uploads/",
        "marketplace_operations.index": "/marketplaces/operations/",
        "marketplace_operations.detail": "/marketplaces/operations/9",
        "marketplace_commercial.index": "/marketplaces/commercial/",
        "product_detail": "/products/<int:product_id>",
        "product_edit_history": "/products/<int:product_id>/history",
    }
    for endpoint, path in endpoints.items():
        app.add_url_rule(path, endpoint=endpoint, view_func=lambda: "")
    app.jinja_env.globals["mp_nav"] = lambda: SimpleNamespace(last_account_id=92)
    app.jinja_env.globals["csrf_token"] = lambda: "synthetic-csrf"
    return app


def test_bulk_completed_with_two_errors_is_not_shown_as_success():
    from services.wb_history_presentation import present_wb_bulk_operation

    app = _app()
    operation = SimpleNamespace(
        id=3,
        status="completed",
        total_products=2,
        success_count=0,
        error_count=2,
        reverted=False,
        operation_params=None,
    )
    with app.test_request_context("/bulk-history"):
        html = render_template(
            "partials/operations_workspace_wb_result.html",
            operation=operation,
            wb_history_presentation=present_wb_bulk_operation(operation),
        )

    assert "Завершено с ошибками" in html
    assert "0 из 2" in html
    assert "ошибок: 2" in html
    assert "Завершено</span>" not in html
    assert "operations-wb-result--dark-hero" not in html


def test_wb_detail_result_uses_hero_text_token():
    from services.wb_history_presentation import present_wb_bulk_operation

    app = _app()
    operation = SimpleNamespace(
        status="completed",
        total_products=2,
        success_count=0,
        error_count=2,
        reverted=True,
    )
    with app.test_request_context("/bulk-history/3"):
        html = render_template(
            "partials/operations_workspace_wb_result.html",
            operation=operation,
            wb_history_presentation=present_wb_bulk_operation(operation),
            operations_wb_result_context="dark-hero",
        )
    css = (ROOT / "static/operations-workspace.css").read_text()
    assert "operations-wb-result--dark-hero" in html
    assert ".operations-wb-result--dark-hero" in css
    assert "color: var(--text-sidebar)" in css
    assert "operations-wb-reverted" in html


def test_unknown_ozon_status_has_no_write_or_retry_action():
    app = _app()
    operation = SimpleNamespace(
        id=9,
        status="provider_future_state",
        operation_kind="product_update",
        account=None,
        account_id=17,
        attempt_count=1,
        poll_count=0,
        reconcile_count=0,
        is_terminal=False,
        contract_version="contract-v1",
        draft_id=None,
        draft_version=None,
        listing_id=48,
        next_poll_at=None,
        error_code="safe_error_code",
        error_message="Synthetic technical message",
        external_task_id="synthetic-task",
        request_fingerprint="f" * 64,
        created_at=None,
        submitted_at=None,
        completed_at=None,
        version=2,
    )
    operation_data = {
        "request_summary": {"offer_id": "synthetic-offer"},
        "quota_snapshot": {},
        "item_results": [],
        "snapshot": None,
    }
    app.jinja_env.globals["mp_nav"] = lambda: SimpleNamespace(last_account_id=17)
    with app.test_request_context("/marketplaces/operations/9"):
        html = render_template(
            "marketplace_operation_detail.html",
            operation=operation,
            operation_data=operation_data,
            can_submit_queued=False,
            rollback_idempotency_key="synthetic-key",
        )

    assert "Неизвестный статус" in html
    assert "Состояние операции не распознано. Не повторяйте запись" in html
    assert 'action="/marketplace_operations.poll' not in html
    assert "Проверить статус сейчас" not in html
    assert "Synthetic technical message" in html
    details_start = html.index("<details class=\"operations-technical-details sh-card p-4 mt-4\">")
    assert details_start < html.index("Synthetic technical message")
    assert "<form" not in html


def test_succeeded_operation_does_not_claim_card_is_visible_for_sale():
    app = _app()
    with app.test_request_context("/marketplaces/operations/9"):
        html = render_template(
            "partials/operations_workspace_status_note.html",
            operation=SimpleNamespace(status="succeeded"),
        )

    assert "Результат записи подтверждён" in html
    assert "не подтверждение отображения карточки в продаже" in html


def test_history_navigation_uses_real_routes_and_keeps_selected_account():
    app = _app()
    with app.test_request_context("/marketplaces/operations/?account_id=17"):
        html = render_template(
            "partials/operations_workspace_history_nav.html",
            operations_history_current="ozon-operations",
            filters=SimpleNamespace(account_id=17),
        )

    assert 'href="/bulk-history"' in html
    assert 'href="/prices/history"' in html
    assert 'href="/products/merge/history"' in html
    assert 'href="/marketplaces/ozon/uploads/?account_id=17"' in html
    assert 'href="/marketplaces/operations/?account_id=17"' in html
    assert 'aria-current="page"' in html


def test_history_navigation_hides_ozon_when_feature_is_disabled():
    app = _app(ozon_enabled=False)
    with app.test_request_context("/bulk-history"):
        html = render_template(
            "partials/operations_workspace_history_nav.html",
            operations_history_current="wb-bulk",
        )

    assert "Изменения WB" in html
    assert "Ozon" not in html
    assert "marketplaces/operations" not in html


def _render_bulk_history_detail(
    errors_details,
    *,
    error_count=2,
    operation_id=44,
    product_changes=None,
    owned_product_ids=None,
    operation_params=None,
    total_products=2,
    success_count=0,
    status="completed",
    legacy_wb_characteristic_presentations=None,
):
    from services.wb_history_presentation import present_wb_bulk_operation

    app = _app()
    operation = SimpleNamespace(
        id=operation_id,
        seller_id=92,
        description="Synthetic bulk operation",
        status=status,
        total_products=total_products,
        success_count=success_count,
        error_count=error_count,
        reverted=False,
        reverted_at=None,
        created_at=datetime(2026, 9, 30, 12, 0, 0),
        completed_at=datetime(2026, 9, 30, 12, 0, 1),
        duration_seconds=1.0,
        operation_params=operation_params,
        errors_details=errors_details,
        can_revert=lambda: False,
    )
    with app.test_request_context(f"/bulk-history/{operation_id}"):
        return render_template(
            "bulk_edit_history_detail.html",
            bulk_operation=operation,
            product_changes=product_changes or [],
            owned_product_ids=owned_product_ids or [],
            wb_history_presentation=present_wb_bulk_operation(operation),
            legacy_wb_characteristic_presentations=(
                legacy_wb_characteristic_presentations or {}
            ),
        )


def _render_bulk_history_list(operation):
    from services.wb_history_presentation import present_wb_bulk_operation

    app = _app()
    pagination = SimpleNamespace(
        items=[operation], pages=1, page=1, has_prev=False, has_next=False,
    )
    with app.test_request_context("/bulk-history"):
        return render_template(
            "bulk_edit_history.html",
            history_records=[operation],
            pagination=pagination,
            wb_history_presentations={
                operation.id: present_wb_bulk_operation(operation),
            },
        )


def test_reviewed_batch31_summary_counts_skips_and_uses_changed_denominator():
    import re
    html = _render_bulk_history_detail(
        [],
        error_count=0,
        total_products=50,
        success_count=2,
        operation_params={
            "review_summary": {
                "selected": 50,
                "eligible": 50,
                "changed": 2,
                "skipped": 48,
                "errors": 0,
                "changed_product_ids": [814, 815],
                "mode": "update_characteristic",
                "subject_id": 123,
            },
        },
    )
    summary_start = html.index('<section class="mt-6 rounded-lg border border-blue-200')
    stats_end = html.index("<!-- Параметры операции -->", summary_start)
    summary = html[summary_start:stats_end]
    plain_summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", summary))

    assert "Выбрано" in summary and "50" in summary
    assert "Допущено к изменению" in summary
    assert "Подготовлено к записи" in summary and "2" in summary
    assert "Пропущено" in summary and "48" in summary
    assert "2 из 2 подготовленных карточек" in plain_summary
    assert "100% подготовленных карточек" in plain_summary
    assert "Ошибок операции: <strong>0</strong>" in summary
    assert 'data-operations-review-summary' in summary
    assert "subject_id" not in summary
    assert "Без ошибки из подготовленных" in html
    assert "4%" not in html
    assert "100%" in html
    assert "Завершено</span>" in html
    assert "без ошибки обработано 2 из 2" in html
    assert "ошибок операции: 0" in html
    assert "успешный результат не подтверждён счётчиками" not in html
    technical_start = html.index("Технические параметры операции")
    details_start = html.rfind("<details", 0, technical_start)
    assert "\"subject_id\": 123" in html[technical_start:]
    assert html.startswith('<details class="operations-technical-details mt-4">', details_start)

    list_operation = SimpleNamespace(
        id=31,
        description="Synthetic reviewed batch 31",
        status="completed",
        total_products=50,
        success_count=2,
        error_count=0,
        reverted=False,
        created_at=datetime(2026, 9, 30, 12, 0, 0),
        completed_at=datetime(2026, 9, 30, 12, 0, 1),
        duration_seconds=1.0,
        operation_params={
            "review_summary": {
                "selected": 50,
                "eligible": 50,
                "changed": 2,
                "skipped": 48,
                "errors": 0,
            },
        },
        can_revert=lambda: False,
    )
    list_html = _render_bulk_history_list(list_operation)
    assert "Завершено</span>" in list_html
    assert "без ошибки обработано 2 из 2" in list_html
    assert "2 из 50" not in list_html


def test_reviewed_partial_history_uses_changed_denominator_in_list_and_detail():
    from services.wb_history_presentation import present_wb_bulk_operation

    operation = SimpleNamespace(
        id=31,
        seller_id=92,
        description="Synthetic reviewed batch 31",
        status="completed",
        total_products=50,
        success_count=1,
        error_count=1,
        reverted=False,
        reverted_at=None,
        created_at=datetime(2026, 9, 30, 12, 0, 0),
        completed_at=datetime(2026, 9, 30, 12, 0, 1),
        duration_seconds=1.0,
        operation_params={
            "review_summary": {
                "selected": 50,
                "eligible": 50,
                "changed": 2,
                "skipped": 48,
                "errors": 0,
            },
        },
        errors_details=None,
        can_revert=lambda: False,
    )
    list_html = _render_bulk_history_list(operation)
    detail_html = _render_bulk_history_detail(
        [],
        operation_id=31,
        error_count=1,
        total_products=50,
        success_count=1,
        operation_params=operation.operation_params,
    )

    for html in (list_html, detail_html):
        assert "Завершено частично" in html
        assert "без ошибки обработано 1 из 2" in html
        assert "ошибок операции: 1" in html
        assert "пропущено 48" in html
        assert "1 из 50" not in html


def test_reviewed_result_requires_complete_counts_and_keeps_pending_state():
    from services.wb_history_presentation import present_wb_bulk_operation

    base = {
        "selected": 50,
        "eligible": 50,
        "changed": 2,
        "skipped": 48,
        "errors": 0,
    }
    incomplete = SimpleNamespace(
        status="completed", total_products=50, success_count=1, error_count=0,
        operation_params={"review_summary": base}, reverted=False,
    )
    pending = SimpleNamespace(
        status="pending", total_products=50, success_count=2, error_count=0,
        operation_params={"review_summary": base}, reverted=False,
    )
    over_counted = SimpleNamespace(
        status="completed", total_products=50, success_count=2, error_count=1,
        operation_params={"review_summary": base}, reverted=False,
    )

    incomplete_result = present_wb_bulk_operation(incomplete)
    pending_result = present_wb_bulk_operation(pending)
    over_counted_result = present_wb_bulk_operation(over_counted)
    assert incomplete_result["label"] == "Завершено, успешный результат не подтверждён счётчиками"
    assert "1 из 2" in incomplete_result["message"]
    assert not incomplete_result["reviewed_counts_reconciled"]
    assert pending_result["label"] == "Ожидает выполнения"
    assert "ещё не завершена" in pending_result["message"]
    assert not pending_result["reviewed_counts_reconciled"]
    pending_detail = _render_bulk_history_detail(
        [],
        operation_id=31,
        total_products=50,
        success_count=2,
        error_count=0,
        status="pending",
        operation_params={"review_summary": base},
    )
    assert "Операция ещё не завершена" in pending_detail
    assert "Итоговые счётчики появятся после завершения" in pending_detail
    assert "Ошибок операции: <strong>—</strong>" in pending_detail
    assert "Доля успеха пока неизвестна" in pending_detail
    assert over_counted_result["label"] == incomplete_result["label"]
    assert "Счётчики выполнения не подтверждают полный результат" in over_counted_result["message"]
    assert "Без ошибки обработано 2 из 2" in over_counted_result["message"]
    assert "Ошибок операции: 1" in over_counted_result["message"]
    assert not over_counted_result["reviewed_counts_reconciled"]


def test_legacy_completed_null_counters_remain_explicitly_unknown():
    from services.wb_history_presentation import present_wb_bulk_operation

    operation = SimpleNamespace(
        id=44,
        seller_id=92,
        description="Synthetic legacy history with missing counts",
        status="completed",
        total_products=None,
        success_count=None,
        error_count=None,
        reverted=False,
        reverted_at=None,
        created_at=datetime(2026, 9, 30, 12, 0, 0),
        completed_at=datetime(2026, 9, 30, 12, 0, 1),
        duration_seconds=1.0,
        operation_params=None,
        errors_details=None,
        can_revert=lambda: False,
    )
    result = present_wb_bulk_operation(operation)
    list_html = _render_bulk_history_list(operation)
    detail_html = _render_bulk_history_detail(
        [], error_count=None, total_products=None, success_count=None,
    )

    assert result["tone"] == "unknown"
    assert result["label"] == "Завершено, счётчики результата не подтверждены"
    assert "отсутствуют корректные числовые счётчики" in result["message"]
    assert "Завершено, счётчики результата не подтверждены" in list_html
    assert "Завершено, счётчики результата не подтверждены" in detail_html
    stats = detail_html[detail_html.index('class="operations-stats-grid'):detail_html.index("<!-- Параметры операции -->")]
    assert "—" in stats and "None" not in stats and "0%" not in stats
    missing = present_wb_bulk_operation(SimpleNamespace(
        status="completed", operation_params=None, reverted=False,
    ))
    assert missing["tone"] == "unknown"
    assert missing["counts"] == {
        "total_products": None,
        "success_count": None,
        "error_count": None,
    }


def test_legacy_completed_counters_above_total_remain_unknown():
    from services.wb_history_presentation import present_wb_bulk_operation

    operation = SimpleNamespace(
        status="completed",
        total_products=1,
        success_count=2,
        error_count=0,
        operation_params=None,
        reverted=False,
    )
    result = present_wb_bulk_operation(operation)

    assert result["tone"] == "unknown"
    assert result["label"] == "Завершено, счётчики результата не согласуются с числом товаров"
    assert "2 из 1" in result["message"]
    assert "Счётчики превышают общее число товаров" in result["message"]


def test_legacy_characteristic_labels_use_only_agreeing_current_wb_cache():
    from unittest.mock import patch

    from services.wb_history_characteristics import present_legacy_wb_characteristics

    class QueryStub:
        def __init__(self, first=None, rows=None):
            self.first_row = first
            self.rows = rows or []

        def filter_by(self, **_kwargs):
            return self

        def filter(self, *_args):
            return self

        def first(self):
            return self.first_row

        def all(self):
            return self.rows

    class ColumnStub:
        def __eq__(self, _value):
            return self

        def is_(self, _value):
            return self

        def in_(self, _values):
            return self

    marketplace = SimpleNamespace(id=7, code="wb", is_active=True)
    category = SimpleNamespace(
        id=5,
        marketplace_id=7,
        subject_id=123,
        subject_name="Товары для дома",
        is_available=True,
    )
    schema = {
        "usable": True,
        "subject_id": 123,
        "subject_name": "Товары для дома",
        "characteristics": [
            {"id": 101, "name": "Материал"},
            {"id": 202, "name": "Цвет"},
            {"id": 303, "name": "Комплектация"},
        ],
    }
    before_items = [
        {"id": 101, "value": ["до 1"]},
        {"id": 202, "value": ["до 2"]},
        {"id": 303, "value": ["до 3"]},
        {"id": 404, "value": ["неизвестное поле"]},
        {"id": 505, "name": "Сохранённое название", "value": ["сохранённое"]},
    ]
    after_items = [
        {"id": 101, "value": ["после 1"]},
        {"id": 202, "value": ["после 2"]},
        {"id": 303, "value": ["после 3"]},
        {"id": 404, "value": ["неизвестное поле после"]},
        {"id": 505, "name": "Сохранённое название", "value": ["сохранённое после"]},
    ]
    product = SimpleNamespace(
        id=814, seller_id=92, subject_id=123, object_name="Товары для дома",
        title="Синтетическая карточка", vendor_code="SKU-814", nm_id=814,
    )
    change = SimpleNamespace(
        id=3101,
        product_id=814,
        seller_id=92,
        product=product,
        changed_fields=["characteristics"],
        snapshot_before={
            "object_name": "Товары для дома",
            "characteristics": before_items,
        },
        snapshot_after={
            "object_name": "Товары для дома",
            "characteristics": after_items,
        },
        reverted=False,
        wb_synced=True,
        wb_sync_status="success",
        wb_error_message=None,
    )
    fake_marketplace_model = SimpleNamespace(
        query=QueryStub(first=marketplace),
    )
    fake_category_model = SimpleNamespace(
        query=QueryStub(rows=[category]),
        marketplace_id=ColumnStub(),
        is_available=ColumnStub(),
        subject_name=ColumnStub(),
    )
    cached_characteristics = [
        SimpleNamespace(
            category_id=5, marketplace_id=7, is_available=True,
            charc_id=101, name="Материал",
        ),
        SimpleNamespace(
            category_id=5, marketplace_id=7, is_available=True,
            charc_id=202, name="Цвет",
        ),
        SimpleNamespace(
            category_id=5, marketplace_id=7, is_available=True,
            charc_id=303, name="Комплектация",
        ),
    ]
    fake_characteristic_model = SimpleNamespace(
        query=QueryStub(rows=cached_characteristics),
        marketplace_id=ColumnStub(),
        category_id=ColumnStub(),
        is_available=ColumnStub(),
    )
    foreign_change = SimpleNamespace(
        **{
            **change.__dict__,
            "id": 3102,
            "product": SimpleNamespace(
                **{
                    **product.__dict__,
                    "seller_id": 93,
                }
            ),
        }
    )

    with patch(
        "services.wb_history_characteristics.Marketplace",
        fake_marketplace_model,
    ), patch(
        "services.wb_history_characteristics.MarketplaceCategory",
        fake_category_model,
    ), patch(
        "services.wb_history_characteristics.MarketplaceCategoryCharacteristic",
        fake_characteristic_model,
    ), patch(
        "services.wb_history_characteristics.schema_for_subject",
        return_value=schema,
    ) as schema_lookup:
        presentations = present_legacy_wb_characteristics(
            [change, foreign_change],
            92,
        )
        schema_lookup.assert_called_once()

    assert set(presentations) == {3101}
    legacy = presentations[3101]
    assert legacy["source_label"] == "По сохранённому справочнику WB"
    assert [item.get("name") for item in legacy["before"][:3]] == [
        "Материал", "Цвет", "Комплектация",
    ]
    assert legacy["before"][3] == before_items[3]
    assert legacy["before"][4]["name"] == "Сохранённое название"
    assert legacy["before"][0]["value"] == ["до 1"]
    assert "name" not in before_items[0]

    html = _render_bulk_history_detail(
        None,
        error_count=0,
        success_count=1,
        total_products=1,
        product_changes=[change],
        owned_product_ids={814},
        legacy_wb_characteristic_presentations=presentations,
    )
    assert "По сохранённому справочнику WB" in html
    assert "Значения взяты из снимка истории" in html
    assert "Материал" in html and "Цвет" in html and "Комплектация" in html
    assert "неизвестное поле" in html
    assert "Сохранённое название" in html
    assert "до 1" in html and "после 1" in html

    duplicate_category_model = SimpleNamespace(
        query=QueryStub(rows=[category, category]),
        marketplace_id=ColumnStub(),
        is_available=ColumnStub(),
        subject_name=ColumnStub(),
    )
    with patch(
        "services.wb_history_characteristics.Marketplace",
        fake_marketplace_model,
    ), patch(
        "services.wb_history_characteristics.MarketplaceCategory",
        duplicate_category_model,
    ), patch(
        "services.wb_history_characteristics.schema_for_subject",
        return_value=schema,
    ) as duplicate_schema_lookup:
        duplicate_presentations = present_legacy_wb_characteristics([change], 92)
        duplicate_schema_lookup.assert_not_called()
    assert duplicate_presentations == {}

    ambiguous_characteristic_model = SimpleNamespace(
        query=QueryStub(rows=[
            *cached_characteristics,
            SimpleNamespace(
                category_id=5, marketplace_id=7, is_available=True,
                charc_id=101, name="Неоднозначная подпись",
            ),
        ]),
        marketplace_id=ColumnStub(),
        category_id=ColumnStub(),
        is_available=ColumnStub(),
    )
    with patch(
        "services.wb_history_characteristics.Marketplace",
        fake_marketplace_model,
    ), patch(
        "services.wb_history_characteristics.MarketplaceCategory",
        fake_category_model,
    ), patch(
        "services.wb_history_characteristics.MarketplaceCategoryCharacteristic",
        ambiguous_characteristic_model,
    ), patch(
        "services.wb_history_characteristics.schema_for_subject",
        return_value=schema,
    ):
        ambiguous_presentations = present_legacy_wb_characteristics([change], 92)
    assert 101 not in {
        item.get("id") for item in ambiguous_presentations[3101]["before"]
        if item.get("name")
    }
    assert ambiguous_presentations[3101]["before"][0] == before_items[0]
    assert ambiguous_presentations[3101]["before"][1]["name"] == "Цвет"

    stale_schema = {**schema, "usable": False}
    with patch(
        "services.wb_history_characteristics.Marketplace",
        fake_marketplace_model,
    ), patch(
        "services.wb_history_characteristics.MarketplaceCategory",
        fake_category_model,
    ), patch(
        "services.wb_history_characteristics.schema_for_subject",
        return_value=stale_schema,
    ):
        stale_presentations = present_legacy_wb_characteristics([change], 92)
    assert stale_presentations == {}

    changed_category = SimpleNamespace(
        **{
            **change.__dict__,
            "snapshot_before": {
                "object_name": "Другая категория",
                "characteristics": before_items,
            },
        }
    )
    assert present_legacy_wb_characteristics([changed_category], 92) == {}

    string_subject_product = SimpleNamespace(
        **{**product.__dict__, "subject_id": "123"}
    )
    string_subject_change = SimpleNamespace(
        **{**change.__dict__, "product": string_subject_product}
    )
    assert present_legacy_wb_characteristics([string_subject_change], 92) == {}


def test_legacy_history_without_review_summary_keeps_legacy_summary_only():
    html = _render_bulk_history_detail([], error_count=1)
    assert 'data-operations-review-summary' not in html
    assert "Всего товаров" in html
    assert "Без общей ошибки" in html
    assert "Завершено с ошибками" in html


def test_review_history_zero_changed_uses_dash_and_bad_summary_falls_back_safely():
    no_changes = _render_bulk_history_detail(
        [],
        total_products=20,
        success_count=0,
        operation_params={
            "review_summary": {
                "selected": 20,
                "eligible": 0,
                "changed": 0,
                "skipped": 20,
            },
        },
    )
    stats = no_changes[
        no_changes.index('class="operations-stats-grid'):no_changes.index("data-operations-review-summary")
    ]
    assert "Без ошибки из подготовленных" in stats
    assert "—" in stats
    assert "0%" not in stats

    malformed = _render_bulk_history_detail(
        [],
        total_products=50,
        success_count=2,
        operation_params={
            "review_summary": {
                "selected": True,
                "eligible": 50,
                "changed": 2,
                "skipped": 48,
            },
        },
    )
    stats = malformed[
        malformed.index('class="operations-stats-grid'):malformed.index("<!-- Параметры операции -->")
    ]
    assert 'data-operations-review-summary' not in malformed
    assert "Без общей ошибки" in stats
    assert "Без ошибки из подготовленных" not in stats
    assert "4%" in stats
    assert "успешный результат не подтверждён счётчиками" in malformed


def test_bulk_detail_shows_row_identity_and_typed_error_outside_collapsed_raw_data():
    html = _render_bulk_history_detail([{
        "product_id": 990000101,
        "nm_id": 777000111,
        "vendor_code": "SYNTH-ROW-101",
        "status": "failed",
        "reason": "supplier_photo_source_drift",
        "error": "Synthetic row failure",
    }])

    summary_start = html.index('<section class="operations-error-summary')
    details_start = html.index(
        '<details class="operations-technical-details mt-4" data-operations-raw-errors>'
    )
    visible_summary = html[summary_start:details_start]
    raw_details = html[details_start:html.index("</details>", details_start)]

    assert "SYNTH-ROW-101" in visible_summary
    assert "Артикул WB: 777000111" in visible_summary
    assert "ID строки: #990000101" in visible_summary
    assert "Не выполнено" in visible_summary
    assert "Галерея поставщика изменилась после выбора; выбранные фотографии не отправлялись." in visible_summary
    assert "Synthetic row failure" in visible_summary
    assert "supplier_photo_source_drift" not in visible_summary
    assert "Сверьте текущую галерею поставщика и карточку WB до нового выбора." in visible_summary
    assert "Перед новым изменением проверьте актуальное состояние карточки в WB." in visible_summary
    assert 'data-operations-error-fix-link' not in visible_summary
    assert 'href="/products/990000101"' not in visible_summary
    assert 'data-operations-error-id="990000101"' in visible_summary
    assert '"product_id": 990000101' in raw_details
    assert '"reason": "supplier_photo_source_drift"' in raw_details
    assert "Synthetic row failure" in raw_details
    assert " open" not in raw_details


def test_bulk_detail_maps_only_confirmed_enrichment_reason_codes_and_keeps_raw_data():
    reason_cases = {
        "content_update_failed": (
            "Обновление полей товара не подтвердило успех; фото в этом проходе были пропущены.",
            "Сверьте фактические значения полей в WB перед новым изменением.",
        ),
        "awaiting_content_reconciliation": (
            "Результат предыдущей записи полей в WB ещё сверяется; отправка фото отложена.",
            "Дождитесь завершения сверки, затем проверьте карточку WB.",
        ),
        "previous_photo_write_pending": (
            "Предыдущая отправка фото в WB ещё ожидает сверки; новая отправка отложена.",
            "Дождитесь результата сверки предыдущей отправки и проверьте актуальную галерею WB.",
        ),
        "media_operation_busy": (
            "Другая операция записи фото в WB ещё выполнялась; этот шаг был отложен.",
            "Дождитесь завершения текущей операции и проверьте актуальную галерею WB.",
        ),
        "supplier_photo_source_drift": (
            "Галерея поставщика изменилась после выбора; выбранные фотографии не отправлялись.",
            "Сверьте текущую галерею поставщика и карточку WB до нового выбора.",
        ),
        "empty_photo_list": (
            "В источнике для этой карточки нет фотографий; загрузка не начиналась.",
            "Проверьте, какие фотографии доступны в карточке поставщика.",
        ),
        "photos_not_cached_after_timeout": (
            "Копии фото не появились в кэше до окончания ожидания; отправка фото в WB не начиналась.",
            "Проверьте доступность фотоисточника и актуальную галерею WB перед новым изменением.",
        ),
        "live_photo_drift": (
            "Повторная проверка галереи или фото перед дозагрузкой не прошла; операция остановлена до отправки.",
            "Сравните текущую галерею WB с выбранными фото перед новым изменением.",
        ),
        "already_has_photos": (
            "При стратегии «Только если нет фото» загрузка пропущена: галерея WB уже содержит фотографии.",
            "Проверьте текущую галерею WB; этот шаг не добавлял новые фотографии.",
        ),
        "selective_mode": (
            "Режим «Выборочно» не обрабатывает фото в общем проходе обогащения; фото были пропущены.",
            "Проверьте выбранный режим и фактическую галерею WB перед новым изменением.",
        ),
    }
    html = _render_bulk_history_detail([
        {
            "product_id": index,
            "reason": code,
            "error": f"Helpful human explanation {index}",
        }
        for index, code in enumerate(reason_cases, start=1)
    ], error_count=len(reason_cases))

    details_start = html.index(
        '<details class="operations-technical-details mt-4" data-operations-raw-errors>'
    )
    visible_summary = html[html.index('<section class="operations-error-summary'):details_start]
    raw_details = html[details_start:html.index("</details>", details_start)]
    for index, (code, (summary, next_step)) in enumerate(reason_cases.items(), start=1):
        assert summary in visible_summary
        assert next_step in visible_summary
        assert f"Helpful human explanation {index}" in visible_summary
        assert code not in visible_summary
        assert f'"reason": "{code}"' in raw_details
        assert f"Helpful human explanation {index}" in raw_details


def test_bulk_detail_handles_legacy_string_unknown_shape_and_escapes_text():
    html = _render_bulk_history_detail([
        "Legacy row: field value rejected",
        {"product_id": "line-17", "status": "skipped", "reason": "no_supplier_data"},
        {"product_id": 18, "error": {"nested": "value"}},
        {"unexpected": ["shape"]},
        19,
        "<script>alert(1)</script>",
        {"product_id": 20, "reason": "future_reason_v2", "error": "Human explanation for future code"},
    ], error_count=7)

    details_start = html.index(
        '<details class="operations-technical-details mt-4" data-operations-raw-errors>'
    )
    visible_summary = html[html.index('<section class="operations-error-summary'):details_start]
    raw_details = html[details_start:html.index("</details>", details_start)]

    assert "Legacy row: field value rejected" in visible_summary
    assert "ID строки: #line-17" in visible_summary
    assert "Пропущено" in visible_summary
    assert "no_supplier_data" in visible_summary
    assert "Описание причины не сохранено в поддерживаемом текстовом поле." in visible_summary
    assert "Запись об ошибке #4" in visible_summary
    assert "Запись об ошибке #5" in visible_summary
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in visible_summary
    assert "<script>alert(1)</script>" not in visible_summary
    assert "Нераспознанный код причины:" in visible_summary
    assert "future_reason_v2" in visible_summary
    assert "Human explanation for future code" in visible_summary
    assert '"reason": "future_reason_v2"' in raw_details
    assert "unexpected" in raw_details
    assert "nested" in raw_details


def test_batch31_detail_shows_readable_field_values_and_seller_scoped_fix_links():
    from types import SimpleNamespace

    product = SimpleNamespace(
        id=814,
        seller_id=92,
        title="Synthetic updated shirt",
        vendor_code="SYNTH-WB-814",
        nm_id=7000814,
    )
    change = SimpleNamespace(
        product_id=814,
        product=product,
        reverted=False,
        wb_synced=False,
        wb_sync_status="failed",
        wb_error_message="Synthetic provider detail",
        changed_fields=["title", "description", "characteristics", "is_active", "extra_metadata"],
        snapshot_before={
            "title": "Старое название",
            "description": "Старое описание " + ("а" * 400),
            "characteristics": [
                {"id": 11, "name": "Материал", "value": "Хлопок"},
                {"id": 12, "name": "Состав", "value": ["Хлопок", "Эластан"]},
                {"id": 13, "name": "Размер", "value": {"value": 42, "unit": "RU"}},
            ],
            "is_active": True,
            "extra_metadata": {"legacy": {"provider_value": [1, 2, 3]}},
        },
        snapshot_after={
            "title": "Новое название",
            "description": "Новое описание",
            "characteristics": [
                {"id": 11, "name": "Материал", "value": "Лён"},
                {"id": 12, "name": "Состав", "value": ["Лён", "Вискоза"]},
            ],
            "is_active": False,
            "extra_metadata": {"legacy": {"provider_value": [4, 5]}},
        },
    )
    foreign_product = SimpleNamespace(
        id=815,
        seller_id=999,
        title="Foreign seller private title",
        vendor_code="FOREIGN-815",
        nm_id=7000815,
    )
    foreign_change = SimpleNamespace(
        product_id=815,
        product=foreign_product,
        reverted=False,
        wb_synced=False,
        wb_sync_status="failed",
        wb_error_message="Foreign details must stay hidden",
        changed_fields=["title"],
        snapshot_before={"title": "foreign before"},
        snapshot_after={"title": "foreign after"},
    )
    html = _render_bulk_history_detail([{
        "product_id": 814,
        "vendor_code": "SYNTH-WB-814",
        "status": "failed",
        "reason": "future_failure_code",
        "error": "Synthetic item failure",
    }, {
        "product_id": 815,
        "status": "failed",
        "reason": "foreign_id_payload",
        "error": "Foreign row must not receive a link",
    }], error_count=1, operation_id=31, product_changes=[change, foreign_change], owned_product_ids=[814])

    assert "Завершено с ошибками" in html
    assert "0 из 2" in html
    assert 'data-operations-error-fix-link' in html
    assert 'href="/products/814"' in html
    assert 'data-operations-change-fix-link' in html
    assert 'href="/products/814"' in html
    assert 'href="/products/814/history"' in html
    assert 'data-operations-changed-field="title"' in html
    assert 'data-operations-changed-field="description"' in html
    assert 'data-operations-changed-field="characteristics"' in html
    assert 'data-operations-changed-field="is_active"' in html
    assert 'data-operations-changed-field="extra_metadata"' in html
    assert "Название" in html
    assert "Описание" in html
    assert "Характеристики" in html
    assert "Старое название" in html and "Новое название" in html
    assert "Старое описание" in html and "Новое описание" in html
    assert "а" * 400 in html
    assert "Материал" in html and "Хлопок" in html and "Лён" in html
    assert "Да" in html and "Нет" in html
    assert "Полное структурированное значение" in html
    assert "Foreign seller private title" not in html
    assert "Foreign details must stay hidden" not in html
    assert "foreign before" not in html and "foreign after" not in html
    assert "Сведения об этой карточке скрыты" in html
    assert 'href="/products/815"' not in html
    assert "WB вернул ошибку" in html
