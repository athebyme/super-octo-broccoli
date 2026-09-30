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
        "bulk_edit_export": "/bulk-history/<int:bulk_id>/export",
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
    app = _app()
    operation = SimpleNamespace(
        status="completed",
        total_products=2,
        success_count=0,
        error_count=2,
        reverted=False,
    )
    with app.test_request_context("/bulk-history"):
        html = render_template(
            "partials/operations_workspace_wb_result.html",
            operation=operation,
        )

    assert "Завершено с ошибками" in html
    assert "0 из 2" in html
    assert "ошибок: 2" in html
    assert "Завершено</span>" not in html
    assert "operations-wb-result--dark-hero" not in html


def test_wb_detail_result_uses_hero_text_token():
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
):
    app = _app()
    operation = SimpleNamespace(
        id=operation_id,
        seller_id=92,
        description="Synthetic bulk operation",
        status="completed",
        total_products=2,
        success_count=0,
        error_count=error_count,
        reverted=False,
        reverted_at=None,
        created_at=datetime(2026, 9, 30, 12, 0, 0),
        completed_at=datetime(2026, 9, 30, 12, 0, 1),
        duration_seconds=1.0,
        operation_params=None,
        errors_details=errors_details,
        can_revert=lambda: False,
    )
    with app.test_request_context(f"/bulk-history/{operation_id}"):
        return render_template(
            "bulk_edit_history_detail.html",
            bulk_operation=operation,
            product_changes=product_changes or [],
            owned_product_ids=owned_product_ids or [],
        )


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
