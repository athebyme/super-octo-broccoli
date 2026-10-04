#!/usr/bin/env python3
"""Run the frozen UX-01 offline acceptance stages in sequence.

The source manifest is produced by the host-side UX-01 acceptance workflow and
is deliberately kept outside the manifest's own input list. This script runs
inside the network-disabled acceptance container; it does not start the app,
touch a persistent database, or inherit seller/provider credentials.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BASELINE = "ba63371"
REPORT_NAME = "ux01-report.json"
PASS_REPORT_STATUSES = {"passed", "complete", "completed"}
ERROR_LIST_FIELDS = (
    "javascript_errors", "js_errors", "console_errors",
    "unexpected_external_requests", "unexpected_http_requests",
    "unexpected_http", "unexpected_api_calls", "blocked_writes",
    "browser_mutations", "external_provider_requests", "external",
    "http_errors", "request_failures",
)
REQUIRED_TESTS = (
    "tests/test_competitor_routes.py",
    "tests/test_marketplace_readiness.py",
    "tests/test_marketplace_listing_routes.py",
    "tests/test_product_selection.py",
    "tests/test_product_selection_dom.py",
    "tests/test_wb_edit_review_replay.py",
    "tests/test_wb_sync_read_only.py",
    "tests/test_wb_bulk_review_key_migration.py",
    "tests/test_common_product_content_service.py",
    "tests/test_common_product_content_routes.py",
    "tests/test_common_product_content_ui.py",
)
REQUIRED_BROWSERS = (
    "tests/ux01/analytics_browser.py",
    "tests/ux01/listing_browser.py",
    "tests/ux01/workspace_browser.py",
    "tests/ux01/journey_browser.py",
    "tests/ux01/operations_pricing_browser.py",
    "tests/ux01/wb_edit_browser.py",
    "tests/ux01/common_content_browser.py",
)
BROWSER_INTERACTION_FIELDS = {
    # Analytics has no separate click log; the real page's bounded API reads
    # are the fixture's evidence that its shell behavior ran.
    "analytics_browser": ("api_calls",),
    "listing_browser": ("checks",),
    "workspace_browser": ("interactions",),
    "journey_browser": ("interactions", "checks"),
    "operations_pricing_browser": ("interactions", "checks"),
    "wb_edit_browser": ("checks",),
    "common_content_browser": ("checks",),
}
BROWSER_MINIMUMS = {
    # The operations/pricing sweep covers 16 routes in both themes and keeps
    # its original 32 page/256 layout measurements alongside price-load proof.
    "operations_pricing_browser": {"layouts": 256, "interactions": 32},
    # Five WB editor pages, each measured at 3 widths in 2 themes; checks
    # include overflow and keyboard-focus evidence plus named safety probes.
    "wb_edit_browser": {"layouts": 30, "interactions": 24},
    "common_content_browser": {"layouts": 28, "interactions": 8},
}
OPERATIONS_PRICING_PAGE_LABELS = frozenset({
    "wb_bulk_history",
    "wb_bulk_detail",
    "ozon_operations",
    "ozon_operation_detail",
    "wb_prices_dashboard",
    "wb_prices_change",
    "wb_prices_settings",
    "wb_prices_history",
    "wb_prices_batch",
    "supplier_formula",
    "wb_price_monitor",
    "wb_price_alerts",
    "ozon_commercial_vue",
    "ozon_commercial_classic",
    "ozon_proposal_vue",
    "ozon_proposal_classic",
})
OPERATIONS_PRICE_INIT_CHECK = "wb_price_change_initializes_once_and_renders_products"
WORKSPACE_LEGACY_ACTION_CHECKS = frozenset({
    "legacy_sidebar_keyboard_activation_reaches_exact_routes",
    "command_palette_enter_reaches_help_and_social",
    "legacy_product_actions_open_exact_product_routes",
    "command_palette_account_link_preserves_selected_account",
    "wb_only_tool_labels_and_image_lab_source_are_distinct",
    "image_lab_fixture_photo_loads_with_imported_source_context",
    "image_lab_empty_manual_override_suppresses_wb_fallback",
})
OPERATIONS_HISTORY_SCENARIO_CHECKS = frozenset({
    "wb_history_batch31_exact_rows_values_and_outcomes",
    "wb_history_owned_fix_link_opens_exact_product",
    "wb_history_foreign_fix_link_absent",
    "wb_history_unresolved_rows_no_retry_or_revert",
})
CLASSIC_DRAFT_FACTS_CHECK = "classic_draft_facts_scroll_regions_fit_320_360_light_dark"
CLASSIC_DRAFT_FACTS_PAGE = "draft_detail_classic"
CLASSIC_DRAFT_FACTS_WIDTHS = (320, 360)
CLASSIC_DRAFT_FACTS_THEMES = ("light", "dark")
CLASSIC_JOURNEY_PAGES = frozenset({
    "supplier_catalog", "supplier_products", "supplier_source_detail",
    "internal_ozon", "drafts_vue", "drafts_classic", "draft_detail_vue",
    "draft_detail_classic", "review", "upload_history", "upload_result",
    "internal_beta", "listing_vue", "listing_classic",
})
CLASSIC_JOURNEY_PAGE_VISITS = (
    "supplier_catalog", "supplier_products", "supplier_source_detail",
    "internal_ozon", "drafts_vue", "drafts_classic", "draft_detail_vue",
    "draft_detail_classic", "review", "upload_history", "upload_result",
    "internal_beta", "listing_vue", "listing_classic",
    "supplier_catalog", "supplier_products", "supplier_source_detail",
    "internal_ozon", "drafts_vue", "drafts_classic", "draft_detail_vue",
    "draft_detail_classic", "review", "upload_history", "upload_result",
)
CLASSIC_JOURNEY_MACRO_LAYOUT_PAGES = frozenset({
    "supplier_catalog", "supplier_products", "supplier_source_detail",
    "internal_ozon", "drafts_vue", "drafts_classic", "draft_detail_vue",
    "draft_detail_classic", "review", "upload_history", "upload_result",
})
CLASSIC_DRAFT_FACTS_REGION_KEYS = frozenset({
    "visible", "left_px", "right_px", "client_width_px", "scroll_width_px",
    "scrolls_horizontally", "overflow_x_auto", "role_region",
    "has_accessible_name", "tabindex", "min_height_px", "table_width_px",
    "table_min_width_px", "table_within_bounded_width", "row_count",
    "value_wraps",
})
CLASSIC_DRAFT_FACTS_LAYOUT_KEYS = frozenset({
    "page", "width", "theme", "actual_theme", "navigation_receipt",
    "document_overflow_px", "body_overflow_px", "main_overflow_px",
    "content_overflow_px", "form_overflow_px", "main_left_px",
    "main_right_px", "summary_count", "summaries_fit_viewport",
    "details_summary_bounds_px", "fact_region_count",
    "visible_fact_region_count", "local_scroll_region_count",
    "all_regions_accessible", "all_regions_fit_viewport",
    "all_visible_regions_have_touch_height",
    "all_visible_tables_within_bounded_width", "all_visible_values_wrap",
    "synthetic_fact_marker_visible", "full_snapshot_retains_synthetic_fact",
    "keyboard_focus_reached", "keyboard_focus_visible", "focus_outline_px",
    "focus_outline_offset_px", "focus_outline_visible",
    "focus_outline_inside_viewport", "keyboard_scroll_delta_px",
    "classic_update_form_preserved", "classic_validate_form_preserved",
    "classic_refresh_form_preserved", "region_rows",
})
WB_HISTORY_STORED_STATUS_COUNTS = {
    "success": 25,
    "failed": 1,
    "pending": 1,
    "submitted": 1,
    "uncertain": 1,
    "partial": 1,
    "conflict": 1,
}
WB_HISTORY_RENDERED_STATUS_COUNTS = {
    key: value for key, value in WB_HISTORY_STORED_STATUS_COUNTS.items()
    if key != "conflict"
}
WB_HISTORY_PARENT_AGGREGATES = {
    "total_products": 31,
    "success_count": 29,
    "error_count": 1,
}
WB_HISTORY_AGGREGATE_CARDS = [
    {"label": "Всего товаров", "value": "31"},
    {"label": "Обработано без ошибки", "value": "29"},
    {"label": "Ошибок", "value": "1"},
    {"label": "Без общей ошибки", "value": "94%"},
]
WB_QUANTITY_ROLLBACK_NOTE = (
    "Безопасный откат для этой операции недоступен. Проверьте результаты строк выше."
)
WB_HISTORY_DOMAIN_COUNTS = {
    "bulk_edit_history": 2,
    "card_edit_history": 32,
    "products": 32,
}
WB_HISTORY_READABLE_STATUS_TEXT = {
    "success": "Результат WB: WB сообщил об успехе.",
    "failed": "Результат WB: WB вернул ошибку; проверьте фактическое состояние перед новым действием.",
    "pending": "Результат WB: Ожидается отправка или подтверждение.",
    "submitted": "Результат WB: Изменение отправлено; итог ещё требует проверки.",
    "uncertain": "Результат WB: Точный исход неизвестен. Не повторяйте изменение до сверки с WB.",
    "partial": "Результат WB: WB подтвердил только часть изменения; проверьте сохранённые значения.",
}
OPERATIONS_PRICING_LAYOUT_VARIANTS = (
    (320, 100), (390, 100), (768, 100), (1024, 100),
    (1280, 100), (1440, 100), (320, 200), (390, 200),
)
COMMON_CONTENT_LAYOUT_WIDTHS = (320, 360, 390, 768, 1024, 1280, 1440)
COMMON_CONTENT_LAYOUT_THEMES = ("light", "dark")
COMMON_CONTENT_MOBILE_WIDTHS = (320, 360, 390)
COMMON_CONTENT_MOBILE_THEMES = ("light", "dark")
COMMON_CONTENT_NAVIGATOR_CHECK = "common_mobile_product_navigator_bounded_accessible_keyboard"
COMMON_CONTENT_LAYOUT_STATES = {
    "empty": "empty_editor",
    "selected": "selected_editor",
}
COMMON_CONTENT_REQUIRED_CHECKS = frozenset({
    "common_empty_editor_no_product_api_or_writes",
    "common_empty_editor_internal_catalog_link",
    "common_photo_boundary_focus_first",
    "common_photo_boundary_focus_last",
    "common_preview_cancel_focus_return",
    "common_focus_visible_geometry",
    "common_mobile_touch_targets_44px",
    COMMON_CONTENT_NAVIGATOR_CHECK,
    "common_51_selection_and_csrf_preview_rejected_without_mutation_or_publication",
    "common_50_product_navigator_last_card_keyboard_reachable",
    "common_50_stale_manual_member_denied_atomically_without_partial_audit",
    "common_50_recovery_apply_persists_50_overrides_and_50_server_audits",
    "common_50_recovery_preserves_inheritance_source_and_channel_snapshots",
    "expected_413_console_rejections_scoped_by_receipt_endpoint_query_code_and_count",
})
COMMON_CONTENT_REQUIRED_FOCUS = frozenset({
    "common_photo_boundary_focus_first",
    "common_photo_boundary_focus_last",
    "common_preview_cancel_focus_return",
})
WB_EDIT_REQUIRED_CHECKS = frozenset({
    "single_edit_form_core_fields_match_persisted_values_before_targeted_characteristic_save",
    "single_edit_fake_provider_full_read_merge_readback_preserves_core_fields_sizes_and_sku",
    "single_edit_real_form_submit_reaches_fake_wb_and_persists_exact_history",
    "single_edit_reopens_exact_saved_values_with_sizes_and_sku_read_only",
    "single_edit_rejects_wrong_weight_unit_without_local_loss",
    "single_edit_rejects_non_numeric_weight_type_without_local_loss",
    "single_edit_rejects_unlisted_dictionary_value_without_local_loss",
    "single_edit_owner_session_cannot_post_foreign_product",
    "single_edit_foreign_owner_session_cannot_post_seller_product",
    "single_edit_no_profile_post_redirects_without_provider_write",
    "single_edit_uses_cached_country_weight_multi_schema_and_read_only_sku",
    "single_edit_enabled_controls_contrast_aa_light_dark",
    "single_edit_footer_assistant_clearance_320_360_390_light_dark",
    "mixed_fixture_preview_selected50_eligible2_changed2_skipped48",
    "mixed_fixture_confirm_writes_exact_two_provider_products_with_history_readback",
})
WB_EDIT_CONTRAST_CHECK = "single_edit_enabled_controls_contrast_aa_light_dark"
WB_EDIT_CONTRAST_THEMES = ("light", "dark")
WB_EDIT_CONTRAST_CONTROL_NAMES = (
    "cancel", "optional_picker_label", "optional_picker", "optional_add", "save",
)
WB_EDIT_CONTRAST_SELECTORS = (
    '.sticky.bottom-0 a[href^="/products/"]',
    'label[for="wb-optional-characteristic-picker"]',
    "#wb-optional-characteristic-picker",
    "#wb-add-optional-characteristic",
    "form.space-y-6 button[type=\"submit\"]",
)
WB_EDIT_FOOTER_CLEARANCE_CHECK = "single_edit_footer_assistant_clearance_320_360_390_light_dark"
WB_EDIT_FOOTER_CLEARANCE_WIDTHS = (320, 360, 390)
WB_EDIT_FOOTER_CLEARANCE_THEMES = ("light", "dark")
WB_EDIT_FOOTER_CLEARANCE_CONTROLS = ("save", "cancel")
WB_EDIT_FOOTER_CLEARANCE_LABELS = {
    "save": "Сохранить в WB",
    "cancel": "Отмена",
}
WB_EDIT_PAGES = frozenset({
    "products_list", "bulk_editor", "bulk_review",
    "single_product_edit", "unmapped_product_edit",
})
WB_EDIT_LAYOUT_WIDTHS = (390, 768, 1280)
WB_EDIT_LAYOUT_THEMES = ("light", "dark")


@dataclass(frozen=True)
class Stage:
    name: str
    kind: str
    command: tuple[str, ...]
    timeout_seconds: int
    report_path: Path | None = None
    expected_source: str | None = None
    environment: tuple[tuple[str, str], ...] = ()


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _common_bulk_50_protocol_issues(data: dict) -> list[str]:
    """Bind the common editor's reported 50-item work to exact IDs and receipts."""
    issues: list[str] = []
    bulk = data.get("bulk_50")
    if not isinstance(bulk, dict):
        return ["common_bulk_50_telemetry_incomplete"]

    fingerprint = bulk.get("selected_product_id_fingerprint")
    if (
        type(bulk.get("selected_products")) is not int
        or bulk.get("selected_products") != 50
        or not _valid_sha256(fingerprint)
    ):
        issues.append("common_bulk_50_selection_identity_invalid")
    for name in (
        "page_51_rejected", "preview_api_51_rejected",
        "last_product_keyboard_reachable", "stale_apply_atomic_rejection",
        "first_preview_product_ids_match", "recovery_preview_product_ids_match",
        "recovery_apply_product_ids_match", "channel_records_unchanged",
        "inheritance_and_source_preserved",
    ):
        if bulk.get(name) is not True:
            issues.append("common_bulk_50_telemetry_incomplete:" + name)
    exact_counts = {
        "stale_denial_unchanged_selected_products": 50,
        "stale_denial_new_audits": 0,
        "recovery_preview_items": 50,
        "recovery_apply_items": 50,
        "persisted_overrides": 50,
        "audit_rows": 50,
    }
    if any(type(bulk.get(name)) is not int or bulk.get(name) != value
           for name, value in exact_counts.items()):
        issues.append("common_bulk_50_persisted_counts_unexpected")
    if bulk.get("final_content_edit_version_counts") != {"2": 49, "3": 1}:
        issues.append("common_bulk_50_version_counts_unexpected")

    preview_rows = data.get("preview_selection_observations")
    expected_50_rows = 0
    observed_51_rows = 0
    preview_rows_valid = isinstance(preview_rows, list) and bool(preview_rows)
    if preview_rows_valid:
        for row in preview_rows:
            if not isinstance(row, dict):
                preview_rows_valid = False
                break
            count = row.get("item_count")
            unique_count = row.get("unique_product_count")
            row_fingerprint = row.get("product_id_fingerprint")
            if (type(count) is not int or count <= 0
                    or type(unique_count) is not int or unique_count != count
                    or not _valid_sha256(row_fingerprint)):
                preview_rows_valid = False
                break
            if count == 50:
                if row_fingerprint == fingerprint:
                    expected_50_rows += 1
                else:
                    preview_rows_valid = False
                    break
            elif count == 51:
                observed_51_rows += 1
                if row_fingerprint == fingerprint:
                    preview_rows_valid = False
                    break
    if not preview_rows_valid or expected_50_rows < 2 or observed_51_rows != 1:
        issues.append("common_bulk_50_preview_fingerprints_incomplete")
    preview_counts = data.get("preview_item_counts")
    if (not isinstance(preview_counts, list) or len(preview_counts) < 3
            or preview_counts[-3:] != [51, 50, 50]):
        issues.append("common_bulk_50_preview_counts_unexpected")

    apply_rows = data.get("apply_result_observations")
    matching_apply_rows = 0
    apply_rows_valid = isinstance(apply_rows, list) and bool(apply_rows)
    if apply_rows_valid:
        for row in apply_rows:
            if not isinstance(row, dict):
                apply_rows_valid = False
                break
            count = row.get("item_count")
            unique_count = row.get("unique_product_count")
            row_fingerprint = row.get("product_id_fingerprint")
            if (type(count) is not int or count <= 0
                    or type(unique_count) is not int or unique_count != count
                    or not _valid_sha256(row_fingerprint)):
                apply_rows_valid = False
                break
            if count == 50:
                if row_fingerprint == fingerprint:
                    matching_apply_rows += 1
                else:
                    apply_rows_valid = False
                    break
    if not apply_rows_valid or matching_apply_rows < 1:
        issues.append("common_bulk_50_apply_fingerprint_incomplete")

    expected_rejections = [
        {
            "method": "GET", "path": "/my-products/common-content",
            "status": 413, "code": "too_many_items",
            "has_query": True, "has_fragment": False,
        },
        {
            "method": "POST", "path": "/api/my-products/common-content/preview",
            "status": 413, "code": "too_many_items",
            "has_query": False, "has_fragment": False,
        },
    ]
    receipts = data.get("expected_http_rejections")
    if not isinstance(receipts, list) or len(receipts) != 2 or set(
        json.dumps(row, sort_keys=True) for row in receipts if isinstance(row, dict)
    ) != set(json.dumps(row, sort_keys=True) for row in expected_rejections):
        issues.append("common_bulk_50_http_rejections_missing_or_unscoped")

    console_receipts = data.get("expected_rejection_console_errors")
    expected_receipt_keys = {
        ("GET", "/my-products/common-content", 413, "too_many_items", True),
        ("POST", "/api/my-products/common-content/preview", 413, "too_many_items", False),
    }
    console_valid = isinstance(console_receipts, list) and len(console_receipts) == 2
    observed_keys = set()
    if console_valid:
        for row in console_receipts:
            if not isinstance(row, dict):
                console_valid = False
                break
            key = (
                row.get("method"), row.get("path"), row.get("status"),
                row.get("code"), row.get("has_query"),
            )
            if not (
                isinstance(key[0], str) and isinstance(key[1], str)
                and type(key[2]) is int and isinstance(key[3], str)
                and type(key[4]) is bool
            ):
                console_valid = False
                break
            observed_keys.add(key)
            message = row.get("message")
            location = row.get("location")
            message_prefix = (
                "Failed to load resource: the server responded with a status of 413 ("
            )
            message_suffix = message[len(message_prefix):-1] if (
                isinstance(message, str) and message.startswith(message_prefix)
                and message.endswith(")")
            ) else ""
            expected_message = (
                isinstance(message, str)
                and 1 <= len(message_suffix) <= 80
                and all(character.isascii() and (
                    character.isalnum() or character in " _-"
                ) for character in message_suffix)
                and len(message) <= 300
            )
            origin = location.get("origin") if isinstance(location, dict) else None
            port = (
                origin[len("http://127.0.0.1:"):] if isinstance(origin, str)
                and origin.startswith("http://127.0.0.1:") else ""
            )
            valid_local_origin = (
                len(port) <= 5 and port.isascii() and port.isdigit()
                and 1 <= int(port) <= 65535
            ) if port else False
            local_endpoint = (
                isinstance(location, dict)
                and valid_local_origin
                and location.get("path") == row.get("path")
                and location.get("has_query") == row.get("has_query")
                and location.get("has_fragment") is False
                and location.get("url_too_long") is False
            )
            if not expected_message or not local_endpoint:
                console_valid = False
                break
    if not console_valid or observed_keys != expected_receipt_keys:
        issues.append("common_bulk_50_413_console_rejections_missing_or_unscoped")
    return issues


def _wb_keyboard_add_receipt_valid(receipt: object, *, field_id: int) -> bool:
    if not isinstance(receipt, dict):
        return False
    picker = receipt.get("picker_focus")
    add_button = receipt.get("add_button_focus")
    control = receipt.get("added_control_focus")
    return (
        isinstance(picker, dict)
        and picker.get("id") == "wb-optional-characteristic-picker"
        and picker.get("selected_value") == str(field_id)
        and isinstance(add_button, dict)
        and add_button.get("text") == "Добавить поле"
        and add_button.get("focus_visible") is True
        and type(add_button.get("box_height")) is int
        and add_button["box_height"] >= 44
        and isinstance(control, dict)
        and control.get("active_id") == f"char_{field_id}"
        and control.get("field_tag") == "SELECT"
        and control.get("focus_visible") is True
    )


def _finite_json_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _valid_rgb_triplet(value: object) -> bool:
    return (
        isinstance(value, list) and len(value) == 3
        and all(_finite_json_number(channel) and 0 <= channel <= 255 for channel in value)
    )


def _wcag_contrast_ratio(foreground: list, background: list) -> float:
    def luminance(rgb: list) -> float:
        linear = []
        for channel in rgb:
            normalized = channel / 255
            linear.append(
                normalized / 12.92
                if normalized <= 0.04045
                else ((normalized + 0.055) / 1.055) ** 2.4
            )
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    first, second = sorted((luminance(foreground), luminance(background)))
    return (second + 0.05) / (first + 0.05)


def _wb_edit_contrast_protocol_issues(data: dict, checks: object) -> list[str]:
    issue = "wb_edit_enabled_control_contrast_aa_incomplete_or_invalid"
    issues: list[str] = []
    named = [
        row for row in checks
        if isinstance(row, dict) and row.get("name") == WB_EDIT_CONTRAST_CHECK
    ] if isinstance(checks, list) else []
    expected_summary = (
        len(named) == 1
        and named[0].get("status") == "passed"
        and named[0].get("themes") == list(WB_EDIT_CONTRAST_THEMES)
        and named[0].get("control_names") == list(WB_EDIT_CONTRAST_CONTROL_NAMES)
        and type(named[0].get("control_count")) is int
        and named[0]["control_count"] == 10
        and _finite_json_number(named[0].get("minimum_contrast_ratio"))
        and named[0]["minimum_contrast_ratio"] == 4.5
        and named[0].get("all_enabled_visible") is True
        and named[0].get("all_settled") is True
        and named[0].get("all_contrast_aa") is True
        and named[0].get("failures") == []
    )
    if not expected_summary:
        issues.append(issue)

    rows = data.get("single_edit_contrast_diagnostic")
    if not isinstance(rows, list) or len(rows) != 2:
        issues.append(issue)
        return issues
    observed_themes = [row.get("requested_theme") if isinstance(row, dict) else None for row in rows]
    if observed_themes != list(WB_EDIT_CONTRAST_THEMES):
        issues.append(issue)
        return issues

    expected_ancestor_effects = {
        "has_background_image", "has_filter", "has_backdrop_filter", "has_non_normal_blend",
    }
    expected_final_style_fields = {
        "selector", "color", "background_color", "border_color", "opacity",
        "box_shadow", "outline_color",
    }
    valid = True
    for expected_theme, row in zip(WB_EDIT_CONTRAST_THEMES, rows):
        viewport = row.get("viewport") if isinstance(row, dict) else None
        stability = row.get("appearance_stability") if isinstance(row, dict) else None
        final_styles = stability.get("final_computed_styles") if isinstance(stability, dict) else None
        final_styles_valid = (
            isinstance(final_styles, list)
            and len(final_styles) == len(WB_EDIT_CONTRAST_SELECTORS)
            and all(
                isinstance(style, dict)
                and set(style) == expected_final_style_fields
                and style.get("selector") == selector
                and all(
                    isinstance(style.get(field), str)
                    and 0 < len(style[field]) <= 160
                    and style[field].strip()
                    for field in expected_final_style_fields - {"selector"}
                )
                for style, selector in zip(
                    final_styles if isinstance(final_styles, list) else (),
                    WB_EDIT_CONTRAST_SELECTORS,
                )
            )
            and len({style.get("selector") for style in final_styles if isinstance(style, dict)})
                == len(WB_EDIT_CONTRAST_SELECTORS)
        )
        if not (
            isinstance(row, dict)
            and row.get("requested_theme") == expected_theme
            and row.get("actual_theme") == expected_theme
            and viewport == {"width": 390, "height": 900}
            and row.get("measurement_valid") is True
            and isinstance(stability, dict)
            and stability.get("settled") is True
            and type(stability.get("samples")) is int
            and stability["samples"] >= 3
            and type(stability.get("stable_frames")) is int
            and stability["stable_frames"] >= 3
            and _finite_json_number(stability.get("elapsed_ms"))
            and stability["elapsed_ms"] >= 0
            and stability.get("active_relevant_transitions") == []
            and final_styles_valid
        ):
            valid = False
            continue
        controls = row.get("controls")
        if not isinstance(controls, list) or len(controls) != len(WB_EDIT_CONTRAST_CONTROL_NAMES):
            valid = False
            continue
        names = [control.get("name") if isinstance(control, dict) else None for control in controls]
        if names != list(WB_EDIT_CONTRAST_CONTROL_NAMES):
            valid = False
            continue
        selectors = [control.get("selector") if isinstance(control, dict) else None for control in controls]
        if selectors != list(WB_EDIT_CONTRAST_SELECTORS):
            valid = False
            continue
        for control in controls:
            if not isinstance(control, dict):
                valid = False
                continue
            foreground = control.get("effective_foreground_rgb")
            background = control.get("effective_background_rgb")
            ratio = control.get("contrast_ratio_estimate")
            opacity_product = control.get("opacity_product")
            opacity_chain = control.get("opacity_chain")
            ancestor_effects = control.get("ancestor_effects")
            background_layers = control.get("background_layers")
            if not (
                control.get("visible") is True
                and control.get("in_viewport") is True
                and control.get("enabled") is True
                and control.get("disabled") is False
                and isinstance(control.get("text"), str)
                and bool(control["text"].strip())
                and isinstance(control.get("computed_color"), str)
                and bool(control["computed_color"].strip())
                and isinstance(control.get("computed_background_color"), str)
                and bool(control["computed_background_color"].strip())
                and _valid_rgb_triplet(foreground)
                and _valid_rgb_triplet(background)
                and _finite_json_number(ratio)
                and ratio >= 4.5
                and abs(ratio - _wcag_contrast_ratio(foreground, background)) <= 0.02
                and control.get("normal_text_wcag_aa") is True
                and _finite_json_number(opacity_product)
                and 0 < opacity_product <= 1
                and isinstance(opacity_chain, list) and bool(opacity_chain)
                and isinstance(ancestor_effects, dict)
                and set(ancestor_effects) == expected_ancestor_effects
                and all(type(value) is bool for value in ancestor_effects.values())
                and isinstance(background_layers, list) and bool(background_layers)
            ):
                valid = False
                continue
            for opacity in opacity_chain:
                if not (
                    isinstance(opacity, dict)
                    and isinstance(opacity.get("tag"), str)
                    and isinstance(opacity.get("id"), str)
                    and _finite_json_number(opacity.get("opacity"))
                    and 0 <= opacity["opacity"] <= 1
                ):
                    valid = False
            for layer in background_layers:
                if not (
                    isinstance(layer, dict)
                    and all(isinstance(layer.get(field), str) for field in (
                        "tag", "id", "background_color", "background_image",
                        "filter", "backdrop_filter", "mix_blend_mode",
                    ))
                    and _valid_rgb_triplet(layer.get("background_rgb_after_compositing"))
                    and _finite_json_number(layer.get("opacity"))
                    and 0 <= layer["opacity"] <= 1
                    and type(layer.get("has_background_image")) is bool
                    and type(layer.get("background_changed")) is bool
                ):
                    valid = False
    if not valid:
        issues.append(issue)
    return issues


def _wb_edit_footer_clearance_protocol_issues(data: dict, checks: object) -> list[str]:
    """Require measured mobile clearance, pointer delivery and keyboard focus."""
    issue = "wb_edit_footer_assistant_clearance_incomplete_or_invalid"
    issues: list[str] = []
    named = [
        row for row in checks
        if isinstance(row, dict) and row.get("name") == WB_EDIT_FOOTER_CLEARANCE_CHECK
    ] if isinstance(checks, list) else []
    summary_ok = (
        len(named) == 1
        and named[0].get("status") == "passed"
        and named[0].get("viewports") == list(WB_EDIT_FOOTER_CLEARANCE_WIDTHS)
        and named[0].get("themes") == list(WB_EDIT_FOOTER_CLEARANCE_THEMES)
        and type(named[0].get("row_count")) is int
        and named[0]["row_count"] == 6
        and named[0].get("all_controls_clear") is True
        and named[0].get("all_keyboard_focus_visible") is True
        and named[0].get("no_side_effects") is True
        and named[0].get("failures") == []
    )
    if not summary_ok:
        issues.append(issue)

    rows = data.get("single_edit_footer_clearance")
    expected = [
        (width, theme)
        for width in WB_EDIT_FOOTER_CLEARANCE_WIDTHS
        for theme in WB_EDIT_FOOTER_CLEARANCE_THEMES
    ]
    if not isinstance(rows, list) or len(rows) != len(expected):
        issues.append(issue)
        return issues

    def rect(value: object, viewport: dict) -> dict | None:
        if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
            return None
        if any(not _finite_json_number(value.get(key)) for key in ("x", "y", "width", "height")):
            return None
        if value["x"] < 0 or value["y"] < 0 or value["width"] <= 0 or value["height"] <= 0:
            return None
        if value["x"] + value["width"] > viewport["width"] + 0.75:
            return None
        if value["y"] + value["height"] > viewport["height"] + 0.75:
            return None
        return value

    def inside(inner: dict, outer: dict) -> bool:
        return (
            inner["x"] >= outer["x"] - 0.75
            and inner["y"] >= outer["y"] - 0.75
            and inner["x"] + inner["width"] <= outer["x"] + outer["width"] + 0.75
            and inner["y"] + inner["height"] <= outer["y"] + outer["height"] + 0.75
        )

    def intersection_area(first: dict, second: dict) -> float:
        width = max(0.0, min(first["x"] + first["width"], second["x"] + second["width"])
                    - max(first["x"], second["x"]))
        height = max(0.0, min(first["y"] + first["height"], second["y"] + second["height"])
                     - max(first["y"], second["y"]))
        return width * height

    def near_point(point: dict, target: dict) -> bool:
        return (
            abs(point["x"] - (target["x"] + target["width"] / 2)) <= 1.0
            and abs(point["y"] - (target["y"] + target["height"] / 2)) <= 1.0
        )

    observed: list[tuple[int, str]] = []
    heights: set[int] = set()
    valid = True
    for row in rows:
        if not isinstance(row, dict):
            valid = False
            continue
        requested_theme = row.get("requested_theme")
        actual_theme = row.get("actual_theme")
        viewport = row.get("viewport")
        width = viewport.get("width") if isinstance(viewport, dict) else None
        height = viewport.get("height") if isinstance(viewport, dict) else None
        if (
            type(width) is not int or width not in WB_EDIT_FOOTER_CLEARANCE_WIDTHS
            or requested_theme not in WB_EDIT_FOOTER_CLEARANCE_THEMES
            or actual_theme != requested_theme
            or type(height) is not int or height < 600 or height > 1600
            or set(viewport) != {"width", "height"}
        ):
            valid = False
            continue
        observed.append((width, requested_theme))
        heights.add(height)
        if row.get("actual_theme") != requested_theme:
            valid = False

        for selector in ("root", "body"):
            widths = row.get(selector)
            if not (
                isinstance(widths, dict)
                and type(widths.get("client_width")) is int
                and widths["client_width"] == width
                and type(widths.get("scroll_width")) is int
                and widths["scroll_width"] <= width
                and type(widths.get("overflow_px")) is int
                and widths["overflow_px"] == 0
                and max(0, widths["scroll_width"] - widths["client_width"]) == 0
            ):
                valid = False

        heading = row.get("heading")
        heading_box = rect(heading.get("rect"), viewport) if isinstance(heading, dict) else None
        heading_container = rect(heading.get("container_rect"), viewport) if isinstance(heading, dict) else None
        line_rects_raw = heading.get("line_rects") if isinstance(heading, dict) else None
        if (
            heading_box is None or heading_container is None
            or not isinstance(line_rects_raw, list) or not line_rects_raw
            or len(line_rects_raw) > 4
            or not inside(heading_box, heading_container)
            or type(heading.get("window_scroll_y")) is not int
            or heading["window_scroll_y"] != 0
            or heading.get("fully_inside_container") is not True
        ):
            valid = False
            heading_lines = []
        else:
            heading_lines = [rect(value, viewport) for value in line_rects_raw]
            if any(value is None or not inside(value, heading_container) or not inside(value, heading_box)
                   for value in heading_lines):
                valid = False

        assistant = row.get("assistant")
        open_trigger = rect(assistant.get("open_rect"), viewport) if isinstance(assistant, dict) else None
        closed_trigger = rect(assistant.get("closed_trigger_rect"), viewport) if isinstance(assistant, dict) else None
        open_panel = rect(assistant.get("open_panel_rect"), viewport) if isinstance(assistant, dict) else None
        message_region = assistant.get("message_scroll_region") if isinstance(assistant, dict) else None
        message_region_rect = (
            rect(message_region.get("rect"), viewport)
            if isinstance(message_region, dict) else None
        )
        composer_rect = rect(assistant.get("composer_rect"), viewport) if isinstance(assistant, dict) else None
        close_control_rect = rect(assistant.get("close_control_rect"), viewport) if isinstance(assistant, dict) else None
        if not (
            isinstance(assistant, dict)
            and assistant.get("opened_via_trigger") is True
            and assistant.get("closed_via_trigger") is True
            and assistant.get("closed_panel_hidden") is True
            and assistant.get("open_panel_inside_viewport") is True
            and open_trigger is not None and closed_trigger is not None and open_panel is not None
            and message_region_rect is not None
            and isinstance(message_region, dict)
            and message_region.get("inside_panel") is True
            and inside(message_region_rect, open_panel)
            and message_region.get("overflow_y") in {"auto", "scroll"}
            and type(message_region.get("client_height")) is int
            and message_region["client_height"] > 0
            and type(message_region.get("scroll_height")) is int
            and message_region["scroll_height"] >= message_region["client_height"]
            and composer_rect is not None
            and assistant.get("composer_inside_panel") is True
            and inside(composer_rect, open_panel)
            and close_control_rect is not None
            and assistant.get("close_control_inside_panel") is True
            and inside(close_control_rect, open_panel)
        ):
            valid = False
            continue

        controls = row.get("controls")
        if not isinstance(controls, list) or len(controls) != len(WB_EDIT_FOOTER_CLEARANCE_CONTROLS):
            valid = False
            controls = []
        names = [control.get("name") if isinstance(control, dict) else None for control in controls]
        if names != list(WB_EDIT_FOOTER_CLEARANCE_CONTROLS):
            valid = False

        overlap_open = assistant.get("open_intersection_area_px") if isinstance(assistant, dict) else None
        overlap_closed = assistant.get("closed_intersection_area_px") if isinstance(assistant, dict) else None
        overlap_panel = assistant.get("open_panel_intersection_area_px") if isinstance(assistant, dict) else None
        if not (
            isinstance(overlap_open, dict) and set(overlap_open) == set(WB_EDIT_FOOTER_CLEARANCE_CONTROLS)
            and isinstance(overlap_closed, dict) and set(overlap_closed) == set(WB_EDIT_FOOTER_CLEARANCE_CONTROLS)
            and isinstance(overlap_panel, dict) and set(overlap_panel) == set(WB_EDIT_FOOTER_CLEARANCE_CONTROLS)
        ):
            valid = False

        button_rectangles = []
        for control in controls:
            if not isinstance(control, dict):
                valid = False
                continue
            name = control.get("name")
            button = rect(control.get("button_rect"), viewport)
            label = rect(control.get("label_rect"), viewport)
            label_parts_raw = control.get("label_rects")
            label_parts = (
                [rect(value, viewport) for value in label_parts_raw]
                if isinstance(label_parts_raw, list) else []
            )
            label_text = control.get("label_text")
            if not (
                name in WB_EDIT_FOOTER_CLEARANCE_CONTROLS
                and control.get("visible") is True
                and control.get("enabled") is True
                and button is not None and button["width"] >= 44 and button["height"] >= 44
                and label is not None and inside(label, button)
                and bool(label_parts) and len(label_parts) <= 4
                and all(part is not None and inside(part, button) for part in label_parts)
                and isinstance(label_text, str)
                and label_text.strip() == WB_EDIT_FOOTER_CLEARANCE_LABELS.get(name)
                and control.get("label_fully_inside_button") is True
            ):
                valid = False
                continue
            button_rectangles.append(button)

            # Compare telemetry with the actual measured geometry; a zero boolean alone is not proof.
            open_area = intersection_area(button, open_trigger) if open_trigger is not None else None
            closed_area = intersection_area(button, closed_trigger) if closed_trigger is not None else None
            panel_area = intersection_area(button, open_panel) if open_panel is not None else None
            if not (
                _finite_json_number(control.get("assistant_open_intersection_area_px"))
                and abs(control["assistant_open_intersection_area_px"] - open_area) <= 0.1
                and control["assistant_open_intersection_area_px"] <= 0.1
                and _finite_json_number(control.get("assistant_closed_intersection_area_px"))
                and abs(control["assistant_closed_intersection_area_px"] - closed_area) <= 0.1
                and control["assistant_closed_intersection_area_px"] <= 0.1
                and panel_area is not None and panel_area <= 0.1
                and _finite_json_number(control.get("open_panel_intersection_area_px"))
                and abs(control["open_panel_intersection_area_px"] - panel_area) <= 0.1
                and control["open_panel_intersection_area_px"] <= 0.1
                and isinstance(overlap_open, dict)
                and _finite_json_number(overlap_open.get(name))
                and abs(overlap_open[name] - open_area) <= 0.1
                and overlap_open[name] <= 0.1
                and isinstance(overlap_closed, dict)
                and _finite_json_number(overlap_closed.get(name))
                and abs(overlap_closed[name] - closed_area) <= 0.1
                and overlap_closed[name] <= 0.1
                and isinstance(overlap_panel, dict)
                and _finite_json_number(overlap_panel.get(name))
                and abs(overlap_panel[name] - panel_area) <= 0.1
                and overlap_panel[name] <= 0.1
            ):
                valid = False

            hits = control.get("hit_tests")
            expected_hits_per_state = 1 + len(label_parts)
            if not isinstance(hits, list) or len(hits) != 2 * expected_hits_per_state:
                valid = False
                hits = []
            for state in ("closed", "open"):
                state_hits = [hit for hit in hits if isinstance(hit, dict) and hit.get("state") == state]
                if len(state_hits) != expected_hits_per_state:
                    valid = False
                    continue
                points = [hit.get("point") for hit in state_hits]
                if any(
                    not isinstance(hit.get("point"), dict)
                    or set(hit["point"]) != {"x", "y"}
                    or any(not _finite_json_number(hit["point"].get(axis)) for axis in ("x", "y"))
                    or hit.get("topmost_is_control") is not True
                    or hit.get("topmost_is_assistant") is not False
                    for hit in state_hits
                ):
                    valid = False
                good_points = [
                    point for point in points
                    if isinstance(point, dict)
                    and set(point) == {"x", "y"}
                    and _finite_json_number(point.get("x"))
                    and _finite_json_number(point.get("y"))
                ]
                if len({(p.get("x"), p.get("y")) for p in good_points}) != len(good_points):
                    valid = False
                if button is not None and any(
                    point["x"] < button["x"] - 0.75
                    or point["y"] < button["y"] - 0.75
                    or point["x"] > button["x"] + button["width"] + 0.75
                    or point["y"] > button["y"] + button["height"] + 0.75
                    for point in good_points
                ):
                    valid = False
                if button is not None and not any(near_point(p, button) for p in good_points):
                    valid = False
                for label_part in label_parts:
                    if label_part is not None and not any(near_point(p, label_part) for p in good_points):
                        valid = False

            focus = control.get("keyboard_focus")
            if not (
                isinstance(focus, dict)
                and focus.get("reached") is True
                and focus.get("visible") is True
                and focus.get("inside_viewport") is True
                and _finite_json_number(focus.get("outline_width_px"))
                and focus["outline_width_px"] >= 2
                and _finite_json_number(focus.get("outline_offset_px"))
                and focus["outline_offset_px"] >= 2
                and focus.get("outline_style") == "solid"
            ):
                valid = False

        if (
            len(button_rectangles) == 2
            and intersection_area(button_rectangles[0], button_rectangles[1]) > 0.1
        ):
            valid = False

        effects = row.get("side_effects")
        if not (
            isinstance(effects, dict)
            and all(type(effects.get(field)) is int and effects[field] == 0 for field in (
                "generation_requests", "form_post_count", "form_submit_events",
                "provider_attempts", "browser_mutations",
            ))
            and effects.get("form_unchanged") is True
        ):
            valid = False

    if observed != expected or len(set(observed)) != len(expected) or len(heights) != 1:
        valid = False
    if not valid:
        issues.append(issue)
    return issues


def _wb_edit_protocol_issues(data: dict) -> list[str]:
    """Require real single-edit and mixed-selection fixture evidence."""
    issues: list[str] = []
    checks = data.get("checks")
    for name in WB_EDIT_REQUIRED_CHECKS:
        rows = [
            row for row in checks
            if isinstance(row, dict) and row.get("name") == name
        ] if isinstance(checks, list) else []
        if len(rows) != 1:
            issues.append("wb_edit_named_check_missing_or_failed:" + name)
            continue
        row = rows[0]
        expected_details = (
            row.get("status") == "passed"
            and row.get("ok") is not False
            and row.get("passed") is not False
        )
        if name in {
            "single_edit_owner_session_cannot_post_foreign_product",
            "single_edit_foreign_owner_session_cannot_post_seller_product",
        }:
            expected_details = expected_details and (
                type(row.get("http_status")) is int and row["http_status"] == 404
                and type(row.get("provider_writes")) is int
                and row["provider_writes"] == 1
                and (name != "single_edit_foreign_owner_session_cannot_post_seller_product"
                     or row.get("separate_browser_session") is True)
            )
        if not expected_details:
            issues.append("wb_edit_named_check_missing_or_failed:" + name)
    issues.extend(_wb_edit_contrast_protocol_issues(data, checks))
    issues.extend(_wb_edit_footer_clearance_protocol_issues(data, checks))

    expected_layouts = {
        (page, theme, width)
        for page in WB_EDIT_PAGES
        for theme in WB_EDIT_LAYOUT_THEMES
        for width in WB_EDIT_LAYOUT_WIDTHS
    }
    layouts = data.get("layouts")
    observed_layouts = []
    layout_valid = isinstance(layouts, list) and len(layouts) == len(expected_layouts)
    if layout_valid:
        for row in layouts:
            if not isinstance(row, dict):
                layout_valid = False
                break
            page, theme, width = (
                row.get("page"), row.get("theme"), row.get("viewport_width"),
            )
            numeric_fields = (
                "document_width", "body_width", "main_width",
                "main_content_left", "main_content_width",
            )
            if (
                not isinstance(page, str) or page not in WB_EDIT_PAGES
                or not isinstance(theme, str) or theme not in WB_EDIT_LAYOUT_THEMES
                or type(width) is not int or width not in WB_EDIT_LAYOUT_WIDTHS
                or any(type(row.get(field)) not in (int, float)
                       or not math.isfinite(row.get(field))
                       for field in numeric_fields)
                or row.get("document_width") > width
                or row.get("body_width") > width
                or row.get("main_width") <= 0
                or row.get("main_content_left") < 0
                or row.get("main_content_width") <= 0
            ):
                layout_valid = False
                break
            observed_layouts.append((page, theme, width))
            settled = row.get("layout_settle")
            if not (
                isinstance(settled, dict)
                and settled.get("theme") == theme
                and type(settled.get("stable_frames")) is int
                and settled["stable_frames"] >= 3
                and settled.get("fonts_ready") is True
                and settled.get("theme_ready") is True
                and settled.get("transitions_running") is False
                and settled.get("viewport_width") == width
                and type(settled.get("main_content_left")) in (int, float)
                and type(settled.get("main_content_width")) in (int, float)
                and math.isfinite(settled.get("main_content_left"))
                and math.isfinite(settled.get("main_content_width"))
                and abs(settled["main_content_left"] - row["main_content_left"]) <= 0.75
                and abs(settled["main_content_width"] - row["main_content_width"]) <= 0.75
            ):
                layout_valid = False
                break
    if (not layout_valid or len(observed_layouts) != len(expected_layouts)
            or len(set(observed_layouts)) != len(expected_layouts)
            or set(observed_layouts) != expected_layouts):
        issues.append("wb_edit_layout_matrix_incomplete_or_unmeasured")

    if type(data.get("fake_wb_single_write_calls")) is not int or data.get("fake_wb_single_write_calls") != 1:
        issues.append("wb_single_edit_write_count_unexpected")
    expected_request = {
        "nm_id": 900000,
        "requested_fields": ["characteristics"],
        "core_fields_requested": [],
        "core_fields_changed": [],
        "characteristic_ids": [202],
        "characteristics": [
            {"id": 202, "value": ["Россия"]},
        ],
        "full_card_read_before": True,
        "full_card_patch_merged": True,
        "full_card_readback": True,
        "sizes_preserved_in_readback": True,
        "sku_preserved_in_readback": True,
    }
    if data.get("fake_wb_single_write_requests") != [expected_request]:
        issues.append("wb_single_edit_fake_request_unexpected")
    expected_bulk_written_products = [*range(900000, 900050), 910000, 910001]
    if (
        type(data.get("fake_wb_write_calls")) is not int
        or data.get("fake_wb_write_calls") != 2
        or data.get("fake_wb_written_products") != expected_bulk_written_products
        or type(data.get("fake_wb_client_instances")) is not int
        or data.get("fake_wb_client_instances") != 3
    ):
        issues.append("wb_fake_provider_bulk_write_totals_unexpected")

    observations = data.get("single_edit_observations")
    form_post = observations.get("form_post") if isinstance(observations, dict) else None
    readback = form_post.get("readback_and_history") if isinstance(form_post, dict) else None
    path = form_post.get("path") if isinstance(form_post, dict) else None
    product_path_id = (
        path[len("/products/"):-len("/edit")]
        if isinstance(path, str) and path.startswith("/products/") and path.endswith("/edit")
        else ""
    )
    valid_product_path_id = (
        product_path_id.isascii() and product_path_id.isdigit()
        and len(product_path_id) <= 19 and int(product_path_id) > 0
    ) if product_path_id else False
    if not (
        isinstance(form_post, dict)
        and type(form_post.get("http_status")) is int and form_post["http_status"] == 302
        and valid_product_path_id
        and form_post.get("normal_html_form") is True
        and form_post.get("csrf_field_present") is True
        and type(form_post.get("fake_write_count")) is int and form_post["fake_write_count"] == 1
        and isinstance(readback, dict)
        and readback.get("characteristic_ids") == [101, 202, 303, 404, 501, 502, 506]
        and type(readback.get("size_count")) is int and readback["size_count"] == 1
        and readback.get("sku") == "SYNTHETIC-WB-SKU-000"
        and type(readback.get("direct_history_count")) is int
        and readback["direct_history_count"] == 1
        and readback.get("history_changed_fields") == ["characteristics"]
        and form_post.get("submit_button_label") == "Сохранить в WB"
        and form_post.get("target_channel") == "Wildberries"
        and form_post.get("required_missing_id") == 500
        and form_post.get("required_missing_value") == ""
        and form_post.get("required_missing_omitted_from_patch") is True
    ):
        issues.append("wb_single_edit_form_and_history_evidence_incomplete")

    save_checks = [
        row for row in checks
        if isinstance(row, dict)
        and row.get("name") == "single_edit_real_form_submit_reaches_fake_wb_and_persists_exact_history"
    ] if isinstance(checks, list) else []
    if not (
        len(save_checks) == 1
        and save_checks[0].get("changed_characteristics") == [202]
        and save_checks[0].get("submit_button_label") == "Сохранить в WB"
        and save_checks[0].get("target_channel") == "Wildberries"
        and save_checks[0].get("required_missing_value") == ""
        and save_checks[0].get("required_missing_omitted_from_patch") is True
        and type(save_checks[0].get("direct_history_count")) is int
        and save_checks[0]["direct_history_count"] == 1
        and save_checks[0].get("sizes_and_sku_preserved") is True
    ):
        issues.append("wb_single_edit_save_receipt_details_incomplete")

    progressive_checks = [
        row for row in checks
        if isinstance(row, dict)
        and row.get("name") == "single_edit_uses_cached_country_weight_multi_schema_and_read_only_sku"
    ] if isinstance(checks, list) else []
    progressive = observations.get("progressive_ui") if isinstance(observations, dict) else None
    initial_view = progressive.get("initial_view") if isinstance(progressive, dict) else None
    request_submit = (
        progressive.get("empty_add_request_submit_attempt")
        if isinstance(progressive, dict) else None
    )
    enter_submit = (
        progressive.get("empty_add_enter_attempt")
        if isinstance(progressive, dict) else None
    )
    if not (
        len(progressive_checks) == 1
        and progressive_checks[0].get("schema_fields") == 31
        and progressive_checks[0].get("initial_visible_field_ids") == [101, 303, 404, 500, 502, 506]
        and progressive_checks[0].get("required_missing_id") == 500
        and progressive_checks[0].get("stale_read_only_id") == 501
        and progressive_checks[0].get("stale_read_only_disclosure_keyboard") is True
        and progressive_checks[0].get("stale_read_only_displayed") is True
        and progressive_checks[0].get("stale_read_only_control_count") == 0
        and progressive_checks[0].get("optional_country_picker_label") == "Страна производства"
        and progressive_checks[0].get("picker_keyboard_selection") == 202
        and progressive_checks[0].get("present_empty_id") == 506
        and progressive_checks[0].get("present_empty_visible") is True
        and progressive_checks[0].get("present_empty_excluded_from_picker") is True
        and progressive_checks[0].get("empty_add_remove_dirty") is False
        and progressive_checks[0].get("empty_add_no_post_or_provider") is True
        and isinstance(initial_view, dict)
        and initial_view.get("viewport_width") == 390
        and type(initial_view.get("document_width")) is int
        and initial_view.get("document_width") <= 390
        and initial_view.get("schema_field_count") == 31
        and initial_view.get("visible_field_ids") == [101, 303, 404, 500, 502, 506]
        and initial_view.get("required_missing_visible") is True
        and initial_view.get("required_missing_input_visible") is True
        and initial_view.get("country_hidden_until_chosen") is True
        and initial_view.get("picker_country_label") == "Страна производства"
        and initial_view.get("optional_choice_count") == 25
        and initial_view.get("present_empty_visible") is True
        and initial_view.get("present_empty_excluded_from_picker") is True
        and initial_view.get("filled_summary") == "4 заполнено · 31 в схеме"
        and initial_view.get("has_changes") is False
        and initial_view.get("save_disabled") is True
        and isinstance(initial_view.get("picker_box"), dict)
        and type(initial_view["picker_box"].get("width")) is int
        and initial_view["picker_box"]["width"] >= 44
        and type(initial_view["picker_box"].get("height")) is int
        and initial_view["picker_box"]["height"] >= 44
        and isinstance(initial_view.get("add_button_box"), dict)
        and type(initial_view["add_button_box"].get("width")) is int
        and initial_view["add_button_box"]["width"] >= 44
        and type(initial_view["add_button_box"].get("height")) is int
        and initial_view["add_button_box"]["height"] >= 44
        and initial_view.get("saved_text_input") is True
        and initial_view.get("numeric_grams_input") is True
        and initial_view.get("dictionary_multiple_select") is True
        and initial_view.get("bounded_textarea") is True
        and isinstance(progressive.get("stale_legacy_field_read_only"), dict)
        and progressive["stale_legacy_field_read_only"].get("field_id") == 501
        and progressive["stale_legacy_field_read_only"].get("disclosure_opened_by_keyboard") is True
        and progressive["stale_legacy_field_read_only"].get("field_text_present") is True
        and progressive["stale_legacy_field_read_only"].get("saved_value_present") is True
        and progressive["stale_legacy_field_read_only"].get("form_control_count") == 0
        and isinstance(request_submit, dict)
        and request_submit.get("submit_event") == {"seen": True, "default_prevented": True}
        and request_submit.get("still_on_edit_route") is True
        and all(type(request_submit.get(field)) is int for field in (
            "post_count", "fake_client_instances", "fake_single_write_calls",
            "provider_attempts", "post_count_delta", "fake_client_delta", "fake_write_delta",
        ))
        and request_submit.get("provider_attempts") == 0
        and request_submit.get("post_count_delta") == 0
        and request_submit.get("fake_client_delta") == 0
        and request_submit.get("fake_write_delta") == 0
        and _wb_keyboard_add_receipt_valid(
            progressive.get("empty_optional_add_keyboard") if isinstance(progressive, dict) else None,
            field_id=202,
        )
        and _wb_keyboard_add_receipt_valid(
            progressive.get("country_picker_add_keyboard") if isinstance(progressive, dict) else None,
            field_id=202,
        )
        and isinstance(progressive.get("empty_optional_remove_keyboard"), dict)
        and progressive["empty_optional_remove_keyboard"].get("picker_focused") is True
        and progressive["empty_optional_remove_keyboard"].get("has_changes") is False
        and isinstance(progressive["empty_optional_remove_keyboard"].get("focus"), dict)
        and progressive["empty_optional_remove_keyboard"]["focus"].get("label")
        == "Убрать пустое поле «Страна производства»"
        and progressive["empty_optional_remove_keyboard"]["focus"].get("focus_visible") is True
        and type(progressive["empty_optional_remove_keyboard"]["focus"].get("box_height")) is int
        and progressive["empty_optional_remove_keyboard"]["focus"]["box_height"] >= 44
        and progressive["empty_optional_remove_keyboard"].get("post_count_delta") == 0
        and progressive["empty_optional_remove_keyboard"].get("fake_client_delta") == 0
        and progressive["empty_optional_remove_keyboard"].get("fake_write_delta") == 0
        and isinstance(enter_submit, dict)
        and enter_submit.get("still_on_edit_route") is True
        and enter_submit.get("has_changes") is False
        and enter_submit.get("save_disabled") is True
        and all(type(enter_submit.get(field)) is int for field in (
            "post_count", "fake_client_instances", "fake_single_write_calls",
            "provider_attempts", "post_count_delta", "fake_client_delta", "fake_write_delta",
        ))
        and enter_submit.get("provider_attempts") == 0
        and enter_submit.get("post_count_delta") == 0
        and enter_submit.get("fake_client_delta") == 0
        and enter_submit.get("fake_write_delta") == 0
    ):
        issues.append("wb_single_edit_progressive_empty_submit_guard_incomplete")

    alignment = observations.get("core_form_alignment") if isinstance(observations, dict) else None
    core_fields = {"vendor_code", "title", "description", "brand"}
    if not (
        isinstance(alignment, dict)
        and isinstance(alignment.get("persisted_core_values"), dict)
        and set(alignment["persisted_core_values"]) == core_fields
        and isinstance(alignment.get("initial_form_values"), dict)
        and set(alignment["initial_form_values"]) == core_fields
        and isinstance(alignment.get("aligned_form_values"), dict)
        and set(alignment["aligned_form_values"]) == core_fields
        and alignment["aligned_form_values"] == alignment["persisted_core_values"]
        and alignment.get("initial_mismatch_fields") == sorted(
            field for field in core_fields
            if alignment["initial_form_values"].get(field)
            != alignment["persisted_core_values"].get(field)
        )
        and alignment.get("exact_before_characteristic_submit") is True
    ):
        issues.append("wb_single_edit_core_form_alignment_incomplete")

    reopen = observations.get("reopen") if isinstance(observations, dict) else None
    if not (
        isinstance(reopen, dict)
        and reopen.get("country") == "Россия"
        and type(reopen.get("weight_grams")) is int and reopen["weight_grams"] == 125
        and reopen.get("materials") == ["Пластик", "Металл"]
        and reopen.get("present_empty_field_preserved") is True
        and reopen.get("sku_read_only") is True
    ):
        issues.append("wb_single_edit_reopen_evidence_incomplete")

    expected_boundaries = {
        "wrong_weight_unit", "non_numeric_weight_type", "unlisted_dictionary_value",
    }
    rejected = observations.get("rejections") if isinstance(observations, dict) else None
    boundary_attempts = data.get("single_edit_boundary_attempts")
    if not isinstance(rejected, list) or not isinstance(boundary_attempts, list):
        issues.append("wb_single_edit_validation_boundaries_incomplete")
    else:
        def valid_boundary(row: object, *, attempt: bool) -> bool:
            if not isinstance(row, dict):
                return False
            name = row.get("name")
            status = row.get("status") if attempt else row.get("http_status")
            return (
                isinstance(name, str) and name in expected_boundaries
                and type(status) is int and status == 200
                and type(row.get("provider_writes")) is int and row["provider_writes"] == 1
                and row.get("local_product_preserved") is True
                and type(row.get("history_count")) is int and row["history_count"] == 1
                and (not attempt or (
                    type(row.get("fake_client_instances")) is int
                    and row["fake_client_instances"] >= 0
                ))
            )
        if (
            len(rejected) != 3 or not all(valid_boundary(row, attempt=False) for row in rejected)
            or {row.get("name") for row in rejected if isinstance(row, dict)} != expected_boundaries
            or len(boundary_attempts) != 3
            or not all(valid_boundary(row, attempt=True) for row in boundary_attempts)
            or {row.get("name") for row in boundary_attempts if isinstance(row, dict)} != expected_boundaries
            or len({row["fake_client_instances"] for row in boundary_attempts if isinstance(row, dict)}) != 1
        ):
            issues.append("wb_single_edit_validation_boundaries_incomplete")

    denials = observations.get("seller_scope_denials") if isinstance(observations, dict) else None
    expected_denials = {("owner", "foreign_product"), ("foreign_owner", "seller_product")}
    if not isinstance(denials, list) or len(denials) != 2:
        issues.append("wb_single_edit_seller_scope_evidence_incomplete")
    else:
        denial_keys = set()
        denials_valid = True
        for row in denials:
            if not isinstance(row, dict):
                denials_valid = False
                continue
            key = (row.get("session"), row.get("target"))
            if not all(isinstance(value, str) for value in key):
                denials_valid = False
                continue
            denial_keys.add(key)
            if not (
                type(row.get("http_status")) is int and row["http_status"] == 404
                and row.get("fake_writes_unchanged") is True
                and (key != ("foreign_owner", "seller_product")
                     or row.get("separate_browser_session") is True)
            ):
                denials_valid = False
        if not denials_valid or denial_keys != expected_denials:
            issues.append("wb_single_edit_seller_scope_evidence_incomplete")

    no_profile = observations.get("no_profile_denial") if isinstance(observations, dict) else None
    if not (
        isinstance(no_profile, dict)
        and no_profile.get("final_path") == "/dashboard"
        and no_profile.get("redirected") is True
        and no_profile.get("separate_browser_session") is True
        and no_profile.get("fake_writes_unchanged") is True
    ):
        issues.append("wb_single_edit_no_profile_evidence_incomplete")

    mixed = data.get("mixed_fixture_observations")
    expected_mixed_counts = {
        "selection": 50, "eligible": 2, "changed": 2, "skipped": 48,
        "errors": 0, "fake_provider_call_delta": 1, "history_success_count": 2,
    }
    if not isinstance(mixed, dict) or any(
        type(mixed.get(key)) is not int or mixed.get(key) != value
        for key, value in expected_mixed_counts.items()
    ):
        issues.append("wb_mixed_selection_and_history_counts_unexpected")
    elif (
        mixed.get("fake_provider_product_ids") != [910000, 910001]
        or type(mixed.get("history_id")) is not int or mixed["history_id"] <= 0
        or mixed.get("history_product_ids") != [20000, 20001]
    ):
        issues.append("wb_mixed_provider_or_history_identity_unexpected")
    return issues


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_repo_file(root: Path, raw_path: object) -> tuple[str, Path]:
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("manifest file path must be a non-empty string")
    if "\\" in raw_path or ":" in raw_path.split("/", 1)[0]:
        raise ValueError(f"manifest path is not a portable relative path: {raw_path!r}")
    relative = PurePosixPath(raw_path)
    if (relative.is_absolute() or relative.as_posix() != raw_path
            or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts)):
        raise ValueError(f"manifest path is not a normalized relative path: {raw_path!r}")

    resolved_root = root.resolve(strict=True)
    candidate = resolved_root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"manifest path contains a symlink: {raw_path}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"manifest path escapes the repository or is missing: {raw_path}") from exc
    if not resolved.is_file():
        raise ValueError(f"manifest path is not a regular file: {raw_path}")
    return raw_path, resolved


def load_manifest(manifest_path: Path, root: Path = ROOT) -> dict:
    if manifest_path.is_symlink():
        raise ValueError("manifest itself must not be a symlink")
    manifest = manifest_path.resolve(strict=True)
    if not manifest.is_file():
        raise ValueError("manifest must be a regular JSON file")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("manifest is unreadable or invalid JSON") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("unsupported UX-01 manifest schema")
    if data.get("baseline") != EXPECTED_BASELINE:
        raise ValueError(f"manifest baseline must be {EXPECTED_BASELINE}")
    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("manifest generated_at must be an ISO UTC timestamp")
    try:
        parsed_time = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("manifest generated_at must be an ISO UTC timestamp") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() != timezone.utc.utcoffset(parsed_time):
        raise ValueError("manifest generated_at must use UTC")

    rows = data.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("manifest files must be a non-empty list")
    entries: list[dict[str, str]] = []
    resolved_paths: dict[str, Path] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ValueError("each manifest file must contain only path and sha256")
        relative, path = _safe_repo_file(root, row["path"])
        digest = row["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError(f"invalid sha256 for manifest path: {relative}")
        if relative in resolved_paths:
            raise ValueError(f"duplicate manifest path: {relative}")
        if path == manifest:
            raise ValueError("manifest must remain outside its own hash list")
        resolved_paths[relative] = path
        entries.append({"path": relative, "sha256": digest})

    aggregate = data.get("aggregate_sha256")
    if not isinstance(aggregate, str) or len(aggregate) != 64:
        raise ValueError("manifest aggregate_sha256 is missing or invalid")
    expected_aggregate = hashlib.sha256(_canonical_json(entries)).hexdigest()
    if aggregate != expected_aggregate:
        raise ValueError("manifest aggregate_sha256 does not match its canonical files list")
    return {
        "path": manifest,
        "baseline": EXPECTED_BASELINE,
        "generated_at": generated_at,
        "files": entries,
        "resolved_paths": resolved_paths,
        "aggregate_sha256": aggregate,
    }


def verify_snapshot(snapshot: dict, root: Path = ROOT) -> dict:
    problems = []
    for entry in snapshot["files"]:
        relative = entry["path"]
        try:
            _normalized, path = _safe_repo_file(root, relative)
        except (OSError, ValueError) as exc:
            problems.append({"path": relative, "reason": "missing_or_unsafe"})
            continue
        try:
            actual = _sha256(path)
        except OSError:
            problems.append({"path": relative, "reason": "unreadable"})
            continue
        if actual != entry["sha256"]:
            problems.append({"path": relative, "reason": "sha256_mismatch"})
    return {"verified": not problems, "issues": problems}


def parse_junit(path: Path) -> dict:
    if not path.is_file():
        return {"valid": False, "reason": "junit_xml_missing", "tests": 0,
                "passed": 0, "skipped": 0, "failures": 0, "errors": 0}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return {"valid": False, "reason": "junit_xml_invalid", "tests": 0,
                "passed": 0, "skipped": 0, "failures": 0, "errors": 0}
    cases = list(root.iter("testcase"))
    skipped = sum(case.find("skipped") is not None for case in cases)
    failures = sum(case.find("failure") is not None for case in cases)
    errors = sum(case.find("error") is not None for case in cases)
    return {
        "valid": bool(cases),
        "reason": None if cases else "junit_xml_empty",
        "tests": len(cases),
        "passed": len(cases) - skipped - failures - errors,
        "skipped": skipped,
        "failures": failures,
        "errors": errors,
    }


def _common_content_protocol_issues(data: dict) -> list[str]:
    """Require the common editor's exact responsive/focus evidence contract."""
    issues: list[str] = []

    layouts = data.get("layouts")
    expected_layouts = {
        (state, width, theme)
        for state in COMMON_CONTENT_LAYOUT_STATES
        for width in COMMON_CONTENT_LAYOUT_WIDTHS
        for theme in COMMON_CONTENT_LAYOUT_THEMES
    }
    observed_layouts = []
    if isinstance(layouts, list):
        for row in layouts:
            if not isinstance(row, dict):
                continue
            state, width, theme = row.get("state"), row.get("width"), row.get("theme")
            if (
                not isinstance(state, str)
                or state not in COMMON_CONTENT_LAYOUT_STATES
                or row.get("kind") != COMMON_CONTENT_LAYOUT_STATES.get(state)
                or type(width) is not int
                or theme not in COMMON_CONTENT_LAYOUT_THEMES
                or row.get("page_overflow") is not False
            ):
                continue
            observed_layouts.append((state, width, theme))
    if (
        not isinstance(layouts, list)
        or len(layouts) != len(expected_layouts)
        or len(observed_layouts) != len(expected_layouts)
        or len(set(observed_layouts)) != len(expected_layouts)
        or set(observed_layouts) != expected_layouts
    ):
        issues.append("common_layout_matrix_incomplete_or_duplicate")

    checks = data.get("checks")
    named_rows: dict[str, list[object]] = {}
    if isinstance(checks, list):
        for value in checks:
            if isinstance(value, str):
                named_rows.setdefault(value, []).append(value)
            elif isinstance(value, dict) and isinstance(value.get("name"), str):
                named_rows.setdefault(value["name"], []).append(value)
    bad_named_check = False
    for name in COMMON_CONTENT_REQUIRED_CHECKS:
        rows = named_rows.get(name, [])
        if len(rows) != 1:
            bad_named_check = True
            continue
        row = rows[0]
        if isinstance(row, dict):
            status_value = row.get("status")
            status_is_valid = (
                isinstance(status_value, str) and status_value in PASS_REPORT_STATUSES
            )
            explicitly_failed = (
                row.get("ok") is False or row.get("passed") is False
                or ("status" in row and not status_is_valid)
            )
            passed = not explicitly_failed and (
                row.get("ok") is True or row.get("passed") is True or status_is_valid
            )
            if not passed:
                bad_named_check = True
    if bad_named_check:
        issues.append("common_named_checks_missing_or_duplicate")

    focus_rows = data.get("focus_observations")
    if not isinstance(focus_rows, list):
        focus_rows = []

    def finite_number(value) -> bool:
        if type(value) not in (int, float):
            return False
        try:
            return math.isfinite(value)
        except (OverflowError, TypeError):
            return False

    navigator_check_rows = [
        value for value in checks
        if isinstance(value, dict)
        and value.get("name") == COMMON_CONTENT_NAVIGATOR_CHECK
    ] if isinstance(checks, list) else []
    if not (
        len(navigator_check_rows) == 1
        and navigator_check_rows[0].get("status") == "passed"
        and navigator_check_rows[0].get("ok") is True
        and navigator_check_rows[0].get("passed") is True
        and navigator_check_rows[0].get("layouts") == 6
        and navigator_check_rows[0].get("expected_layouts") == 6
    ):
        issues.append("common_mobile_product_navigator_check_missing_or_failed")

    navigator_rows = data.get("mobile_product_navigator_observations")
    expected_navigator_rows = {
        (width, theme)
        for width in COMMON_CONTENT_MOBILE_WIDTHS
        for theme in COMMON_CONTENT_MOBILE_THEMES
    }

    def focus_ring_inside_scrollport(value) -> bool:
        if not isinstance(value, dict):
            return False
        ring, scrollport = value.get("focus_ring"), value.get("scrollport")
        if not isinstance(ring, dict) or not isinstance(scrollport, dict):
            return False
        keys = ("left", "top", "right", "bottom")
        if any(
            not finite_number(box.get(key))
            for box in (ring, scrollport)
            for key in keys
        ):
            return False
        return (
            ring["right"] > ring["left"]
            and ring["bottom"] > ring["top"]
            and scrollport["right"] > scrollport["left"]
            and scrollport["bottom"] > scrollport["top"]
            and ring["left"] >= scrollport["left"] - 0.5
            and ring["right"] <= scrollport["right"] + 0.5
            and ring["top"] >= scrollport["top"] - 0.5
            and ring["bottom"] <= scrollport["bottom"] + 0.5
        )

    observed_navigator_rows = []
    if isinstance(navigator_rows, list):
        for row in navigator_rows:
            if not isinstance(row, dict):
                continue
            width, theme = row.get("width"), row.get("theme")
            if type(width) is not int or theme not in COMMON_CONTENT_MOBILE_THEMES:
                continue
            observed_navigator_rows.append((width, theme))
            widths = row.get("card_widths_px")
            heights = row.get("card_button_heights_px")
            local_scroll = row.get("local_scroll_after_tab_px")
            if (
                row.get("state") != "selected"
                or row.get("passed") is not True
                or type(row.get("card_count")) is not int
                or row.get("card_count") != 2
                or not finite_number(row.get("selection_height_px"))
                or row.get("selection_height_px") <= 0
                or row.get("selection_height_px") > 220
                or not isinstance(widths, list)
                or len(widths) != 2
                or any(not finite_number(value) or value <= 0 for value in widths)
                or abs(widths[0] - widths[1]) > 1
                or not isinstance(heights, list)
                or len(heights) != 2
                or any(not finite_number(value) or value < 44 for value in heights)
                or row.get("local_horizontal_scroll") is not True
                or not finite_number(local_scroll)
                or local_scroll <= 0
                or row.get("page_overflow") is not False
                or any(row.get(key) is not True for key in (
                    "page_scroll_stable",
                    "keyboard_reached_second_by_tab",
                    "keyboard_returned_first_by_shift_tab",
                    "second_focus_visible",
                    "first_focus_visible",
                    "second_focus_geometry_wait_completed",
                    "first_focus_geometry_wait_completed",
                    "second_focus_outline_within_scrollport",
                    "first_focus_outline_within_scrollport",
                    "current_product_preserved",
                    "full_second_title_dom",
                    "full_second_sku_dom",
                    "full_second_accessible_name",
                    "full_second_title_tooltip",
                    "full_second_external_id_within_model_limit",
                    "full_current_heading",
                    "no_product_api_reads",
                    "no_mutating_requests",
                ))
                or not focus_ring_inside_scrollport(row.get("second_focus_ring_geometry"))
                or not focus_ring_inside_scrollport(row.get("first_focus_ring_geometry"))
                or row.get("product_api_reads_during") != 0
                or row.get("mutating_requests_during") != 0
                or row.get("preview_apply_requests_during") != 0
            ):
                issues.append("common_mobile_product_navigator_incomplete_or_unsafe")
    if (
        not isinstance(navigator_rows, list)
        or len(navigator_rows) != len(expected_navigator_rows)
        or len(observed_navigator_rows) != len(expected_navigator_rows)
        or len(set(observed_navigator_rows)) != len(expected_navigator_rows)
        or set(observed_navigator_rows) != expected_navigator_rows
    ):
        issues.append("common_mobile_product_navigator_incomplete_or_unsafe")

    def valid_focus_geometry(row: dict) -> bool:
        rect = row.get("rect")
        viewport = row.get("viewport")
        if not isinstance(rect, dict) or not isinstance(viewport, dict):
            return False
        keys = ("x", "y", "width", "height")
        if any(not finite_number(rect.get(key)) for key in keys):
            return False
        if any(not finite_number(viewport.get(key)) for key in ("width", "height")):
            return False
        x, y = rect["x"], rect["y"]
        width, height = rect["width"], rect["height"]
        viewport_width, viewport_height = viewport["width"], viewport["height"]
        return (
            x >= 0 and y >= 0 and width > 0 and height > 0
            and viewport_width > 0 and viewport_height > 0
            and x + width <= viewport_width + 1
            and y + height <= viewport_height + 1
        )

    def valid_computed_outline(row: dict) -> bool:
        outline = row.get("outline")
        if not isinstance(outline, dict):
            return False
        style = outline.get("style")
        color = outline.get("color")
        width = outline.get("width")
        if not isinstance(style, str) or style.lower() in {"none", "hidden"}:
            return False
        if not finite_number(width) or width <= 0:
            return False
        if not isinstance(color, str) or color.lower().strip() in {"", "transparent"}:
            return False
        normalized = color.lower().replace(" ", "")
        if normalized.startswith("rgba("):
            try:
                alpha = float(normalized.rsplit(",", 1)[1].removesuffix(")"))
            except (IndexError, ValueError):
                return False
            if alpha <= 0:
                return False
        return True

    by_check: dict[str, list[dict]] = {}
    for row in focus_rows:
        if isinstance(row, dict) and isinstance(row.get("check"), str):
            by_check.setdefault(row["check"], []).append(row)

    for name in COMMON_CONTENT_REQUIRED_FOCUS:
        rows = by_check.get(name, [])
        if len(rows) != 1:
            issues.append("common_focus_observation_missing_or_duplicate:" + name)
            continue
        row = rows[0]
        common_evidence = all(row.get(field) is True for field in (
            "observed", "target_supported", "enabled", "visible",
            "focus_visible", "outline_visible",
        ))
        if not common_evidence or not valid_focus_geometry(row) or not valid_computed_outline(row):
            issues.append("common_focus_observation_unconfirmed:" + name)
            continue
        if name in {
            "common_photo_boundary_focus_first",
            "common_photo_boundary_focus_last",
        }:
            expected_direction = "1" if name == "common_photo_boundary_focus_first" else "-1"
            target = row.get("target")
            if (
                row.get("same_photo") is not True
                or not isinstance(target, dict)
                or not isinstance(target.get("photo_url"), str)
                or not target.get("photo_url")
                or not isinstance(target.get("direction"), str)
                or target.get("direction") != expected_direction
                or row.get("focused_photo_url") != target.get("photo_url")
                or row.get("focused_direction") != expected_direction
            ):
                issues.append("common_focus_observation_unconfirmed:" + name)
        elif (
            row.get("target_action") != "preview"
            or row.get("focused_action") != "preview"
            or row.get("same_trigger") is not True
        ):
            issues.append("common_focus_observation_unconfirmed:" + name)

    actions = data.get("synthetic_actions")
    exact_counters = {
        "preview_requests": 7,
        "apply_requests": 4,
        "expected_preview_conflicts": 1,
        "expected_apply_conflicts": 2,
        "empty_description_override_requests": 1,
        "provider_attempts": 0,
        "empty_route_api_reads": 0,
        "empty_route_mutating_requests": 0,
    }
    if not isinstance(actions, dict) or any(
        type(actions.get(key)) is not int or actions.get(key) != value
        for key, value in exact_counters.items()
    ):
        issues.append("common_synthetic_action_counts_unexpected")
    if not isinstance(actions, dict) or any(actions.get(key) is not True for key in (
        "empty_state_catalog_link_available",
        "selected_photo_order_persisted",
        "channel_record_unchanged",
    )):
        issues.append("common_state_assertion_missing")
    issues.extend(_common_bulk_50_protocol_issues(data))
    return issues


def _is_positive_id(value: object) -> bool:
    return type(value) is int and 0 < value <= (2**63 - 1)


def _is_loopback_origin(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None and 1 <= port <= 65535
        and parsed.username is None and parsed.password is None
        and parsed.path == "" and parsed.query == "" and parsed.fragment == ""
    )


def _route_receipt_valid(row: object, path: str, heading: str | None = None) -> bool:
    if not isinstance(row, dict):
        return False
    valid = (
        _is_loopback_origin(row.get("origin"))
        and row.get("method") == "GET"
        and row.get("path") == path
        and type(row.get("http_status")) is int
        and row.get("http_status") == 200
    )
    if heading is not None:
        valid = valid and row.get("page_heading") == heading
    return valid


def _named_receipt_rows(data: dict, field: str,
                        required_names: frozenset[str]) -> tuple[dict[str, dict], bool]:
    rows = data.get(field)
    if not isinstance(rows, list) or len(rows) != len(required_names):
        return {}, False
    by_name: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            return {}, False
        name = row["name"]
        if name in by_name:
            return {}, False
        by_name[name] = row
    if set(by_name) != required_names:
        return {}, False
    return by_name, all(row.get("status") == "passed" for row in by_name.values())


def _count_snapshot_unchanged(data: dict, field: str,
                              expected_keys: set[str]) -> bool:
    state = data.get(field)
    if not isinstance(state, dict) or state.get("unchanged") is not True:
        return False
    before, after = state.get("before"), state.get("after")
    return (
        isinstance(before, dict) and isinstance(after, dict)
        and set(before) == expected_keys and set(after) == expected_keys
        and all(type(value) is int and value >= 0 for value in before.values())
        and all(type(value) is int and value >= 0 for value in after.values())
        and before == after
    )


def _exact_integer_count_map(actual: object, expected: dict[str, int]) -> bool:
    return (
        isinstance(actual, dict) and set(actual) == set(expected)
        and all(type(value) is int for value in actual.values())
        and actual == expected
    )


def _workspace_legacy_action_protocol_issues(data: dict) -> list[str]:
    """Require separate, typed receipts for the new legacy-navigation checks."""
    issues: list[str] = []
    rows, passed = _named_receipt_rows(
        data, "legacy_action_checks", WORKSPACE_LEGACY_ACTION_CHECKS,
    )
    if not passed:
        issues.append("workspace_legacy_action_receipts_missing_duplicate_or_failed")
        return issues
    if (not isinstance(data.get("pages"), list) or len(data["pages"]) != 37
            or not isinstance(data.get("layouts"), list) or len(data["layouts"]) != 43
            or not isinstance(data.get("interactions"), list) or len(data["interactions"]) != 28):
        issues.append("workspace_original_37_43_28_matrices_changed")

    sidebar = rows["legacy_sidebar_keyboard_activation_reaches_exact_routes"]
    if not (
        _route_receipt_valid(sidebar, "/products/merge", "Объединение карточек WB")
        and sidebar.get("activation") == "Tab+Enter"
        and sidebar.get("label") == "Объединить карточки WB"
    ):
        issues.append("workspace_legacy_sidebar_route_receipt_invalid")

    palette = rows["command_palette_enter_reaches_help_and_social"]
    expected_palette_routes = {
        ("Документация", "/docs/", "Документация"),
        ("Социальные подключения", "/content-factory/accounts", "Подключённые аккаунты"),
    }
    palette_routes = palette.get("routes")
    seen_palette_routes = set()
    if isinstance(palette_routes, list):
        for route in palette_routes:
            if not isinstance(route, dict):
                continue
            route_identity = (route.get("label"), route.get("path"), route.get("page_heading"))
            if all(isinstance(value, str) for value in route_identity):
                seen_palette_routes.add(route_identity)
            else:
                issues.append("workspace_command_palette_route_receipt_invalid")
                continue
            if (route.get("activation") != "Ctrl+K+Enter"
                    or not _route_receipt_valid(
                        route, route.get("path"), route.get("page_heading")
                        if isinstance(route.get("page_heading"), str) else None,
                    )):
                issues.append("workspace_command_palette_route_receipt_invalid")
                break
    if (not isinstance(palette_routes, list) or len(palette_routes) != 2
            or seen_palette_routes != expected_palette_routes):
        issues.append("workspace_command_palette_routes_incomplete_or_wrong")

    product_actions = rows["legacy_product_actions_open_exact_product_routes"]
    product_id = product_actions.get("product_id")
    expected_product_actions = {
        ("История", f"/products/{product_id}/history", "История изменений карточки"),
        ("Обогатить", f"/products/{product_id}/enrich", "Обогащение от поставщика"),
        ("Редактировать", f"/products/{product_id}/edit", "Редактирование карточки"),
    } if _is_positive_id(product_id) else set()
    action_rows = product_actions.get("actions")
    seen_actions = set()
    if isinstance(action_rows, list):
        for action in action_rows:
            if not isinstance(action, dict):
                continue
            label, path, heading = (
                action.get("label"), action.get("path"), action.get("page_heading"),
            )
            if all(isinstance(value, str) for value in (label, path, heading)):
                seen_actions.add((label, path, heading))
            else:
                issues.append("workspace_product_action_route_receipt_invalid")
                continue
            if (action.get("method") != "GET"
                    or not _is_loopback_origin(action.get("origin"))
                    or type(action.get("http_status")) is not int
                    or action.get("http_status") != 200
                    or action.get("title_matches") is not True):
                issues.append("workspace_product_action_route_receipt_invalid")
                break
    if (not _is_positive_id(product_id)
            or not _is_loopback_origin(product_actions.get("origin"))
            or product_actions.get("method") != "GET"
            or not _is_positive_id(product_actions.get("foreign_product_id"))
            or product_actions.get("foreign_product_id") == product_id
            or product_actions.get("foreign_scope_denial") != {
                "method": "GET",
                "path": f"/products/{product_actions.get('foreign_product_id')}",
                "http_status": 404,
            }
            or not isinstance(action_rows, list) or len(action_rows) != 3
            or seen_actions != expected_product_actions):
        issues.append("workspace_product_action_routes_incomplete_or_wrong")
    if not isinstance(data.get("expected_http_denials"), list) or data.get("expected_http_denials") != [{
        "method": "GET",
        "path": f"/products/{product_actions.get('foreign_product_id')}",
        "status": 404,
    }]:
        issues.append("workspace_foreign_product_scope_denial_receipt_invalid")
    denial_origin = product_actions.get("origin")
    denial_path = f"/products/{product_actions.get('foreign_product_id')}"
    expected_denial_console_errors = [{
        "method": "GET",
        "origin": denial_origin,
        "path": denial_path,
        "http_status": 404,
        "text": "Failed to load resource: the server responded with a status of 404 (NOT FOUND)",
        "location_url": f"{denial_origin}{denial_path}",
    }]
    denial_console_errors = data.get("expected_denial_console_errors")
    if (not _is_loopback_origin(denial_origin)
            or not isinstance(denial_console_errors, list)
            or len(denial_console_errors) != 1
            or denial_console_errors != expected_denial_console_errors):
        issues.append("workspace_foreign_denial_console_receipt_invalid")

    account = rows["command_palette_account_link_preserves_selected_account"]
    account_id = account.get("selected_account_id")
    downstream_query = account.get("downstream_account_query")
    if not (
        _route_receipt_valid(account, "/marketplaces/listings/", "Каталог маркетплейсов")
        and account.get("palette_label") == "Карточки кабинетов"
        and account.get("palette_href_path") == "/marketplaces/listings/"
        and _is_positive_id(account_id)
        and account.get("page_heading") == "Каталог маркетплейсов"
        and account.get("rendered_account_label") == "Ozon CI 0"
        and account.get("downstream_account_href_path") == "/marketplaces/drafts/"
        and isinstance(downstream_query, dict)
        and set(downstream_query) == {"account_id"}
        and type(downstream_query.get("account_id")) is int
        and downstream_query.get("account_id") == account_id
        and account.get("account_context_preserved") is True
    ):
        issues.append("workspace_account_context_route_receipt_invalid")

    tools = rows["wb_only_tool_labels_and_image_lab_source_are_distinct"]
    if not (
        _route_receipt_valid(tools, "/image-lab")
        and tools.get("image_lab_page_heading") == "Фотостудия"
        and tools.get("ozon_listing_page_heading") == "Каталог маркетплейсов"
        and tools.get("wb_tool_heading") == "Инструменты Wildberries"
        and tools.get("wb_merge_href_path") == "/products/merge"
        and tools.get("ozon_listings_href_path") == "/marketplaces/listings/"
        and isinstance(tools.get("ozon_listings_query"), dict)
        and set(tools["ozon_listings_query"]) == {"account_id"}
        and type(tools["ozon_listings_query"].get("account_id")) is int
        and tools["ozon_listings_query"].get("account_id") == account_id
        and tools.get("ozon_account_label") == "Ozon CI 0"
        and tools.get("groups_distinct") is True
    ):
        issues.append("workspace_wb_ozon_tool_scope_receipt_invalid")

    photo = rows["image_lab_fixture_photo_loads_with_imported_source_context"]
    photo_product_id = photo.get("source_product_id")
    photo_path = f"/image-lab/api/products/{photo_product_id}/original"
    digest = photo.get("fake_photo_sha256")
    original_get = photo.get("original_get")
    if not (
        _is_positive_id(photo_product_id)
        and _route_receipt_valid(photo, "/image-lab", "Фотостудия")
        and photo.get("page_heading") == "Фотостудия"
        and isinstance(original_get, dict)
        and original_get == {
            "method": "GET", "path": photo_path, "status": 200,
            "content_type": "image/png",
        }
        and photo.get("source_type") == "imported_product"
        and photo.get("source_title") == "Synthetic imported photo source"
        and _is_positive_id(photo.get("listing_id"))
        and _is_positive_id(photo.get("listing_account_id"))
        and photo.get("listing_account_id") == account_id
        and type(photo.get("fake_transport_read_count")) is int
        and photo.get("fake_transport_read_count") == 1
        and isinstance(digest, str) and _valid_sha256(digest)
        and type(photo.get("image_natural_width")) is int
        and photo.get("image_natural_width") == 64
        and type(photo.get("image_natural_height")) is int
        and photo.get("image_natural_height") == 64
    ):
        issues.append("workspace_image_lab_known_photo_receipt_invalid")

    empty_photo = rows["image_lab_empty_manual_override_suppresses_wb_fallback"]
    experiments_before, experiments_after = (
        empty_photo.get("experiments_before"), empty_photo.get("experiments_after"),
    )
    if not (
        _is_positive_id(empty_photo.get("source_product_id"))
        and empty_photo.get("explicit_empty_override") is True
        and empty_photo.get("excluded_from_lab") is True
        and type(empty_photo.get("override_schema_version")) is int
        and empty_photo.get("override_schema_version") == 1
        and type(empty_photo.get("content_edit_version")) is int
        and empty_photo.get("content_edit_version") == 2
        and type(empty_photo.get("override_photo_count")) is int
        and empty_photo.get("override_photo_count") == 0
        and type(empty_photo.get("effective_photo_count")) is int
        and empty_photo.get("effective_photo_count") == 0
        and type(empty_photo.get("inherited_source_photo_count")) is int
        and empty_photo.get("inherited_source_photo_count") == 1
        and _is_positive_id(empty_photo.get("wb_linked_product_id"))
        and empty_photo.get("wb_linked_product_id") == product_id
        and type(empty_photo.get("wb_photo_fallback_reads")) is int
        and empty_photo.get("wb_photo_fallback_reads") == 0
        and type(empty_photo.get("wb_photo_fallback_downloads")) is int
        and empty_photo.get("wb_photo_fallback_downloads") == 0
        and type(experiments_before) is int and type(experiments_after) is int
        and experiments_before == experiments_after
    ):
        issues.append("workspace_empty_manual_photo_fallback_receipt_invalid")

    if (data.get("legacy_domain_sql_writes") != []
            or not _count_snapshot_unchanged(data, "legacy_domain_state", {
                "products", "card_edit_history", "imported_products",
                "marketplace_listings", "image_generation_experiments",
            })
            or type(data.get("legacy_post_count")) is not int
            or data.get("legacy_post_count") != 0
            or data.get("browser_mutations") != []):
        issues.append("workspace_legacy_domain_state_changed_or_write_attempted")
    elif type(experiments_before) is int:
        state = data.get("legacy_domain_state", {})
        if (experiments_before != state["before"]["image_generation_experiments"]
                or experiments_after != state["after"]["image_generation_experiments"]):
            issues.append("workspace_image_lab_experiment_count_mismatch")

    fake_reads = data.get("image_lab_fake_reads")
    if (not isinstance(fake_reads, list) or len(fake_reads) != 1
            or not isinstance(fake_reads[0], dict)
            or fake_reads[0].get("transport") != "synthetic_imported_photo"
            or fake_reads[0].get("fake_photo_sha256") != photo.get("fake_photo_sha256")
            or not isinstance(data.get("image_lab_wb_fallback_reads"), list)
            or data.get("image_lab_wb_fallback_reads") != []
            or type(data.get("image_lab_wb_fallback_downloads")) is not int
            or data.get("image_lab_wb_fallback_downloads") != 0):
        issues.append("workspace_image_lab_transport_reads_unexpected")

    origins = [sidebar.get("origin"), account.get("origin"), tools.get("origin"), photo.get("origin")]
    origins.extend(
        route.get("origin") for route in palette_routes if isinstance(route, dict)
    ) if isinstance(palette_routes, list) else None
    origins.extend(
        action.get("origin") for action in action_rows if isinstance(action, dict)
    ) if isinstance(action_rows, list) else None
    if (not all(_is_loopback_origin(origin) for origin in origins)
            or len(set(origins)) != 1):
        issues.append("workspace_legacy_routes_not_bound_to_one_loopback_origin")
    return issues


def _history_scenario_protocol_issues(data: dict) -> list[str]:
    """Require exact, read-only receipts for the isolated 31-row WB history fixture."""
    issues: list[str] = []
    rows, passed = _named_receipt_rows(
        data, "history_scenario_checks", OPERATIONS_HISTORY_SCENARIO_CHECKS,
    )
    if not passed:
        issues.append("ops_history_scenario_receipts_missing_duplicate_or_failed")
        return issues

    batch = rows["wb_history_batch31_exact_rows_values_and_outcomes"]
    bulk_id = batch.get("bulk_id")
    product_ids = batch.get("product_ids")
    owned_ids = batch.get("owned_product_ids")
    foreign_id = batch.get("foreign_product_id")
    id_lists_valid = (
        isinstance(product_ids, list) and len(product_ids) == 31
        and all(_is_positive_id(value) for value in product_ids)
        and len(set(product_ids)) == 31
        and isinstance(owned_ids, list) and len(owned_ids) == 30
        and all(_is_positive_id(value) for value in owned_ids)
        and len(set(owned_ids)) == 30
        and _is_positive_id(foreign_id)
        and foreign_id in product_ids and foreign_id not in owned_ids
        and set(owned_ids).issubset(set(product_ids))
        and set(product_ids) == set(owned_ids) | {foreign_id}
        and product_ids[-2] == foreign_id
        and product_ids[-1] in owned_ids
    )
    stored_status_counts = batch.get("status_counts")
    rendered_status_counts = batch.get("rendered_status_counts")
    expected_status_rows = batch.get("product_statuses")
    status_scope_valid = False
    statuses_by_id: dict[int, str] = {}
    if id_lists_valid and isinstance(expected_status_rows, list) and len(expected_status_rows) == 31:
        for status_row in expected_status_rows:
            if (not isinstance(status_row, dict)
                    or not _is_positive_id(status_row.get("product_id"))
                    or not isinstance(status_row.get("wb_sync_status"), str)
                    or status_row["product_id"] in statuses_by_id):
                statuses_by_id = {}
                break
            statuses_by_id[status_row["product_id"]] = status_row["wb_sync_status"]
        status_scope_valid = (
            set(statuses_by_id) == set(product_ids)
            and statuses_by_id.get(foreign_id) == "conflict"
            and Counter(
                statuses_by_id[value] for value in owned_ids
            ) == Counter({
                "success": 25, "failed": 1, "pending": 1,
                "submitted": 1, "uncertain": 1, "partial": 1,
            })
        )
    rendered_status_rows = batch.get("rendered_product_statuses")
    rendered_status_scope_valid = False
    if id_lists_valid and isinstance(rendered_status_rows, list) and len(rendered_status_rows) == 30:
        rendered_by_id: dict[int, dict] = {}
        for status_row in rendered_status_rows:
            if (not isinstance(status_row, dict)
                    or not _is_positive_id(status_row.get("product_id"))
                    or status_row["product_id"] in rendered_by_id
                    or not isinstance(status_row.get("wb_sync_status"), str)
                    or not isinstance(status_row.get("readable_outcome"), str)):
                rendered_by_id = {}
                break
            rendered_by_id[status_row["product_id"]] = status_row
        rendered_status_scope_valid = (
            set(rendered_by_id) == set(owned_ids)
            and all(
                rendered_by_id[product_id].get("wb_sync_status") == statuses_by_id.get(product_id)
                and " ".join(rendered_by_id[product_id]["readable_outcome"].split())
                == WB_HISTORY_READABLE_STATUS_TEXT.get(statuses_by_id.get(product_id))
                for product_id in owned_ids
            )
        )
    quantity_values = batch.get("owned_quantity_values")
    quantity_scope_valid = False
    if id_lists_valid and isinstance(quantity_values, list) and len(quantity_values) == 30:
        quantities_by_id: dict[int, dict] = {}
        for value in quantity_values:
            if (not isinstance(value, dict)
                    or not _is_positive_id(value.get("product_id"))
                    or value["product_id"] in quantities_by_id
                    or any(type(value.get(field)) is not int for field in ("before", "after"))
                    or type(value.get("rendered_before")) is not str
                    or type(value.get("rendered_after")) is not str
                    or value.get("rendered_before") != str(value.get("before"))
                    or value.get("rendered_after") != str(value.get("after"))):
                quantities_by_id = {}
                break
            quantities_by_id[value["product_id"]] = value
        quantity_scope_valid = set(quantities_by_id) == set(owned_ids)

    pending_id = batch.get("pending_unprocessed_product_id")
    pending_scope_valid = (
        id_lists_valid
        and _is_positive_id(pending_id)
        and pending_id == product_ids[-1]
        and pending_id in owned_ids
        and statuses_by_id.get(pending_id) == "pending"
    )
    aggregate_cards = batch.get("aggregate_cards")
    if not (
        _is_positive_id(bulk_id)
        and _route_receipt_valid(
            batch, f"/bulk-history/{bulk_id}",
            "R10 synthetic batch31 mixed WB row outcomes",
        )
        and batch.get("operation_status") == "in_progress"
        and type(batch.get("operation_success_count")) is int
        and batch.get("operation_success_count") == 29
        and type(batch.get("operation_error_count")) is int
        and batch.get("operation_error_count") == 1
        and batch.get("operation_completed_at") is None
        and batch.get("operation_duration_seconds") is None
        and pending_scope_valid
        and type(batch.get("total_products")) is int and batch.get("total_products") == 31
        and type(batch.get("rendered_rows")) is int and batch.get("rendered_rows") == 31
        and type(batch.get("owned_visible_rows")) is int and batch.get("owned_visible_rows") == 30
        and type(batch.get("foreign_hidden_rows")) is int and batch.get("foreign_hidden_rows") == 1
        and id_lists_valid
        and _exact_integer_count_map(stored_status_counts, WB_HISTORY_STORED_STATUS_COUNTS)
        and _exact_integer_count_map(rendered_status_counts, WB_HISTORY_RENDERED_STATUS_COUNTS)
        and status_scope_valid
        and rendered_status_scope_valid
        and quantity_scope_valid
        and batch.get("values_exact") is True
        and batch.get("exact_owned_product_ids") is True
        and _exact_integer_count_map(batch.get("aggregates"), WB_HISTORY_PARENT_AGGREGATES)
        and aggregate_cards == WB_HISTORY_AGGREGATE_CARDS
    ):
        issues.append("ops_history_batch31_scope_values_or_outcomes_invalid")

    owned_link = rows["wb_history_owned_fix_link_opens_exact_product"]
    linked_id = owned_link.get("product_id")
    if not (
        id_lists_valid and linked_id in owned_ids
        and _route_receipt_valid(owned_link, f"/products/{linked_id}")
        and owned_link.get("clicked_label") == "Карточка WB"
        and owned_link.get("title_matches") is True
        and owned_link.get("vendor_code_matches") is True
        and owned_link.get("nm_id_matches") is True
    ):
        issues.append("ops_history_owned_fix_link_scope_or_route_invalid")

    foreign = rows["wb_history_foreign_fix_link_absent"]
    if not (
        id_lists_valid
        and foreign.get("foreign_product_id") == foreign_id
        and type(foreign.get("fix_link_count")) is int
        and foreign.get("fix_link_count") == 0
        and type(foreign.get("history_link_count")) is int
        and foreign.get("history_link_count") == 0
        and foreign.get("private_text_absent") is True
    ):
        issues.append("ops_history_foreign_fix_link_or_data_exposed")

    unresolved = rows["wb_history_unresolved_rows_no_retry_or_revert"]
    unresolved_rows = unresolved.get("unresolved_product_statuses")
    expected_unresolved = {
        product_id: statuses_by_id.get(product_id)
        for product_id in owned_ids
        if statuses_by_id.get(product_id) in {"pending", "submitted", "uncertain", "partial"}
    } if id_lists_valid else {}
    observed_unresolved = {}
    if isinstance(unresolved_rows, list):
        for row in unresolved_rows:
            if (not isinstance(row, dict)
                    or not _is_positive_id(row.get("product_id"))
                    or not isinstance(row.get("wb_sync_status"), str)
                    or row["product_id"] in observed_unresolved):
                observed_unresolved = {}
                break
            observed_unresolved[row["product_id"]] = row["wb_sync_status"]
    completed_view = unresolved.get("completed_quantity_rollback_view")
    completed_product_id = (
        completed_view.get("product_id") if isinstance(completed_view, dict) else None
    )
    before_snapshot = (
        completed_view.get("snapshot_before") if isinstance(completed_view, dict) else None
    )
    after_snapshot = (
        completed_view.get("snapshot_after") if isinstance(completed_view, dict) else None
    )
    rollback_note_text = (
        completed_view.get("unsupported_note_text")
        if isinstance(completed_view, dict) else None
    )
    completed_quantity_view_valid = (
        isinstance(completed_view, dict)
        and _is_positive_id(completed_view.get("operation_id"))
        and completed_view.get("operation_id") != bulk_id
        and completed_view.get("origin") == batch.get("origin")
        and _route_receipt_valid(
            completed_view,
            f"/bulk-history/{completed_view.get('operation_id')}",
            "R10 completed quantity-only rollback fixture",
        )
        and completed_view.get("operation_status") == "completed"
        and type(completed_view.get("total_products")) is int
        and completed_view.get("total_products") == 1
        and type(completed_view.get("success_count")) is int
        and completed_view.get("success_count") == 1
        and type(completed_view.get("error_count")) is int
        and completed_view.get("error_count") == 0
        and _is_positive_id(completed_product_id)
        and completed_product_id not in product_ids
        and _is_positive_id(completed_view.get("operation_seller_id"))
        and _is_positive_id(completed_view.get("product_seller_id"))
        and completed_view.get("operation_seller_id") == completed_view.get("product_seller_id")
        and completed_view.get("owned_identity_matches") is True
        and type(completed_view.get("card_edit_history_count")) is int
        and completed_view.get("card_edit_history_count") == 1
        and isinstance(completed_view.get("product_title"), str)
        and bool(completed_view.get("product_title").strip())
        and isinstance(completed_view.get("vendor_code"), str)
        and bool(completed_view.get("vendor_code").strip())
        and _is_positive_id(completed_view.get("nm_id"))
        and completed_view.get("title_matches") is True
        and completed_view.get("vendor_code_matches") is True
        and completed_view.get("nm_id_matches") is True
        and completed_view.get("changed_fields") == ["quantity"]
        and isinstance(before_snapshot, dict)
        and set(before_snapshot) == {"quantity"}
        and type(before_snapshot.get("quantity")) is int
        and isinstance(after_snapshot, dict)
        and set(after_snapshot) == {"quantity"}
        and type(after_snapshot.get("quantity")) is int
        and before_snapshot["quantity"] != after_snapshot["quantity"]
        and completed_view.get("rendered_before") == str(before_snapshot["quantity"])
        and completed_view.get("rendered_after") == str(after_snapshot["quantity"])
        and completed_view.get("safe_revert_supported") is False
        and type(completed_view.get("revert_form_count")) is int
        and completed_view.get("revert_form_count") == 0
        and completed_view.get("unsupported_note_visible") is True
        and isinstance(rollback_note_text, str)
        and " ".join(rollback_note_text.split())
        == " ".join(WB_QUANTITY_ROLLBACK_NOTE.split())
        and type(completed_view.get("post_count")) is int
        and completed_view.get("post_count") == 0
    )
    if not (
        id_lists_valid
        and len(expected_unresolved) == 4
        and observed_unresolved == expected_unresolved
        and unresolved.get("retry_affordances_absent") is True
        and type(unresolved.get("revert_form_count")) is int
        and unresolved.get("revert_form_count") == 0
        and type(unresolved.get("mutation_count")) is int
        and unresolved.get("mutation_count") == 0
        and type(unresolved.get("post_count")) is int
        and unresolved.get("post_count") == 0
        and unresolved.get("quantity_rollback_contract_supported") is False
        and unresolved.get("unsupported_rollback_note_rendered") is False
        and completed_quantity_view_valid
    ):
        issues.append("ops_history_unresolved_retry_or_revert_affordance_present")

    if (data.get("history_domain_sql_writes") != []
            or not _count_snapshot_unchanged(data, "history_domain_state", {
                "bulk_edit_history", "card_edit_history", "products",
            })
            or (isinstance(data.get("history_domain_state"), dict)
                and data["history_domain_state"].get("before") != WB_HISTORY_DOMAIN_COUNTS)
            or data.get("writes") != []
            or data.get("browser_mutations") != []):
        issues.append("ops_history_domain_state_changed_or_write_attempted")
    if (not _is_loopback_origin(batch.get("origin"))
            or not _is_loopback_origin(owned_link.get("origin"))
            or batch.get("origin") != owned_link.get("origin")):
        issues.append("ops_history_routes_not_bound_to_one_loopback_origin")
    return issues


def _classic_draft_facts_protocol_issues(data: dict) -> list[str]:
    """Require four separately measured, keyboard-accessible classic fact views."""
    issues: list[str] = []

    checks = data.get("checks")
    named = []
    if isinstance(checks, list):
        named = [
            row for row in checks
            if row == CLASSIC_DRAFT_FACTS_CHECK
            or (isinstance(row, dict) and row.get("name") == CLASSIC_DRAFT_FACTS_CHECK)
        ]
    if named != [CLASSIC_DRAFT_FACTS_CHECK]:
        issues.append("journey_classic_facts_named_check_missing_duplicate_or_wrong_shape")

    # Keep this new direct GET and its four measurements out of the existing
    # page/layout/geometry matrix. The original runner records 25 visits in a
    # fixed order (11 initial pages, beta and two listings, then the same 11
    # pages during the macro sweep), covering 14 distinct routes and 118 rows.
    pages = data.get("pages")
    page_names = []
    page_rows_valid = (
        isinstance(pages, list)
        and len(pages) == len(CLASSIC_JOURNEY_PAGE_VISITS)
    )
    if page_rows_valid:
        for row in pages:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or row.get("status") != 200
                or not isinstance(row.get("path"), str)
                or not row["path"].startswith("/")
                or urlsplit(row["path"]).scheme
                or urlsplit(row["path"]).netloc
            ):
                page_rows_valid = False
                break
            page_names.append(row["name"])
    if (not page_rows_valid
            or tuple(page_names) != CLASSIC_JOURNEY_PAGE_VISITS
            or set(page_names) != CLASSIC_JOURNEY_PAGES):
        issues.append("journey_original_page_matrix_changed")

    expected_layouts = {
        (page, width, theme)
        for page in CLASSIC_JOURNEY_MACRO_LAYOUT_PAGES
        for width in (320, 390, 768, 1024, 1440)
        for theme in ("light", "dark")
    }
    expected_layouts.update({
        (page, width, theme)
        for page in ("listing_vue", "listing_classic")
        for width in (1440, 390)
        for theme in ("light", "dark")
    })
    layouts = data.get("layouts")
    geometry = data.get("geometry")
    observed_layouts = []
    matrix_valid = (
        isinstance(layouts, list) and len(layouts) == len(expected_layouts)
        and isinstance(geometry, list) and len(geometry) == len(expected_layouts)
        and layouts == geometry
    )
    if matrix_valid:
        for row in layouts:
            if not isinstance(row, dict):
                matrix_valid = False
                break
            page, width, theme = row.get("page"), row.get("width"), row.get("theme")
            if (
                not isinstance(page, str)
                or type(width) is not int
                or not isinstance(theme, str)
                or row.get("actual_theme") != theme
            ):
                matrix_valid = False
                break
            observed_layouts.append((page, width, theme))
    if (not matrix_valid or len(observed_layouts) != len(expected_layouts)
            or len(set(observed_layouts)) != len(expected_layouts)
            or set(observed_layouts) != expected_layouts):
        issues.append("journey_original_layout_geometry_matrix_changed")

    navigation = data.get("classic_content_navigation")
    if not (
        isinstance(navigation, dict)
        and set(navigation) == {
            "method", "status", "same_loopback_origin",
            "exact_fixture_classic_path_match", "route_kind",
        }
        and navigation.get("method") == "GET"
        and type(navigation.get("status")) is int
        and navigation.get("status") == 200
        and navigation.get("same_loopback_origin") is True
        and navigation.get("exact_fixture_classic_path_match") is True
        and navigation.get("route_kind") == "classic_draft_detail"
    ):
        issues.append("journey_classic_facts_navigation_receipt_invalid")

    rows = data.get("classic_draft_content_layouts")
    expected_cases = {
        (CLASSIC_DRAFT_FACTS_PAGE, width, theme)
        for width in CLASSIC_DRAFT_FACTS_WIDTHS
        for theme in CLASSIC_DRAFT_FACTS_THEMES
    }
    observed_cases = []
    if isinstance(rows, list) and len(rows) == len(expected_cases):
        for row in rows:
            if not isinstance(row, dict) or set(row) != CLASSIC_DRAFT_FACTS_LAYOUT_KEYS:
                continue
            case = (row.get("page"), row.get("width"), row.get("theme"))
            if (
                row.get("page") != CLASSIC_DRAFT_FACTS_PAGE
                or type(row.get("width")) is not int
                or row.get("width") not in CLASSIC_DRAFT_FACTS_WIDTHS
                or row.get("theme") not in CLASSIC_DRAFT_FACTS_THEMES
                or row.get("actual_theme") != row.get("theme")
                or row.get("navigation_receipt") != "classic_content_navigation"
            ):
                continue
            width = row["width"]
            viewport_overflows = (
                "document_overflow_px", "body_overflow_px", "main_overflow_px",
                "content_overflow_px", "form_overflow_px",
            )
            if any(type(row.get(field)) is not int or row[field] != 0
                   for field in viewport_overflows):
                continue

            numeric = (
                "main_left_px", "main_right_px", "focus_outline_px",
                "focus_outline_offset_px", "keyboard_scroll_delta_px",
            )
            if any(type(row.get(field)) not in (int, float)
                   or not math.isfinite(row.get(field)) for field in numeric):
                continue
            if not (
                row["main_left_px"] >= -1
                and row["main_right_px"] <= width + 1
                and row["main_right_px"] > row["main_left_px"]
                and row["focus_outline_px"] >= 2
                and row["keyboard_scroll_delta_px"] > 0
            ):
                continue

            summary_rows = row.get("details_summary_bounds_px")
            summary_count = row.get("summary_count")
            if (
                type(summary_count) is not int or summary_count < 1
                or not isinstance(summary_rows, list)
                or len(summary_rows) != summary_count
                or row.get("summaries_fit_viewport") is not True
            ):
                continue
            summary_valid = True
            for summary in summary_rows:
                if (
                    not isinstance(summary, dict)
                    or set(summary) != {"visible", "details_open", "left", "right", "inside_viewport"}
                    or summary.get("visible") is not True
                    or type(summary.get("details_open")) is not bool
                    or summary.get("inside_viewport") is not True
                    or type(summary.get("left")) not in (int, float)
                    or type(summary.get("right")) not in (int, float)
                    or not math.isfinite(summary.get("left"))
                    or not math.isfinite(summary.get("right"))
                    or summary["left"] < -1
                    or summary["right"] > width + 1
                    or summary["right"] <= summary["left"]
                ):
                    summary_valid = False
                    break
            if not summary_valid:
                continue

            region_rows = row.get("region_rows")
            if not isinstance(region_rows, list) or len(region_rows) != 3:
                continue
            region_valid = True
            visible_count = 0
            for region in region_rows:
                if not isinstance(region, dict) or set(region) != CLASSIC_DRAFT_FACTS_REGION_KEYS:
                    region_valid = False
                    break
                if type(region.get("visible")) is not bool:
                    region_valid = False
                    break
                visible = region["visible"]
                visible_count += int(visible)
                numeric_region = (
                    "left_px", "right_px", "client_width_px", "scroll_width_px",
                    "min_height_px", "table_width_px", "table_min_width_px",
                )
                if any(type(region.get(field)) not in (int, float)
                       or not math.isfinite(region.get(field))
                       for field in numeric_region):
                    region_valid = False
                    break
                bool_region = (
                    "scrolls_horizontally", "overflow_x_auto", "role_region",
                    "has_accessible_name", "table_within_bounded_width", "value_wraps",
                )
                if any(type(region.get(field)) is not bool for field in bool_region):
                    region_valid = False
                    break
                if (
                    type(region.get("tabindex")) is not int
                    or region["tabindex"] != 0
                    or type(region.get("row_count")) is not int
                    or region["row_count"] <= 0
                    or region["scroll_width_px"] < region["client_width_px"]
                    or not region["role_region"]
                    or not region["has_accessible_name"]
                ):
                    region_valid = False
                    break
                if visible:
                    width_limit = max(region["client_width_px"], 352)
                    if (
                        region["client_width_px"] <= 0
                        or region["right_px"] <= region["left_px"]
                        or region["left_px"] < -1
                        or region["right_px"] > width + 1
                        or region["min_height_px"] < 44
                        or not region["scrolls_horizontally"]
                        or not region["overflow_x_auto"]
                        or not region["table_within_bounded_width"]
                        or not region["value_wraps"]
                        or region["table_width_px"] > width_limit
                        or region["table_min_width_px"] > width_limit
                    ):
                        region_valid = False
                        break
            if (
                not region_valid
                or visible_count != 2
                or type(row.get("fact_region_count")) is not int
                or row.get("fact_region_count") != 3
                or type(row.get("visible_fact_region_count")) is not int
                or row.get("visible_fact_region_count") != visible_count
                or type(row.get("local_scroll_region_count")) is not int
                or row.get("local_scroll_region_count") != 2
                or row.get("all_regions_accessible") is not True
                or row.get("all_regions_fit_viewport") is not True
                or row.get("all_visible_regions_have_touch_height") is not True
                or row.get("all_visible_tables_within_bounded_width") is not True
                or row.get("all_visible_values_wrap") is not True
            ):
                continue

            if not all(row.get(field) is True for field in (
                "synthetic_fact_marker_visible", "full_snapshot_retains_synthetic_fact",
                "keyboard_focus_reached", "keyboard_focus_visible",
                "focus_outline_visible", "focus_outline_inside_viewport",
                "classic_update_form_preserved", "classic_validate_form_preserved",
                "classic_refresh_form_preserved",
            )):
                continue
            observed_cases.append(case)

    if (not isinstance(rows, list) or len(rows) != len(expected_cases)
            or len(observed_cases) != len(expected_cases)
            or len(set(observed_cases)) != len(expected_cases)
            or set(observed_cases) != expected_cases):
        issues.append("journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
    return issues


def _operations_pricing_protocol_issues(data: dict) -> list[str]:
    """Require exact receipts for the WB prices page's read-only first load."""
    issues: list[str] = _history_scenario_protocol_issues(data)

    checks = data.get("checks")
    named_checks = [
        row for row in checks
        if isinstance(row, dict) and row.get("name") == OPERATIONS_PRICE_INIT_CHECK
    ] if isinstance(checks, list) else []
    named_themes = [row.get("theme") for row in named_checks]
    if (len(named_checks) != 2
            or any(type(theme) is not str for theme in named_themes)
            or set(named_themes) != {"light", "dark"}):
        issues.append("ops_price_initialization_named_check_incomplete_or_duplicate")
    elif any(row.get("status") != "passed" for row in named_checks):
        issues.append("ops_price_initialization_named_check_failed")

    observations = data.get("price_initialization")
    expected_themes = {"light", "dark"}
    if not isinstance(observations, list) or len(observations) != 2:
        issues.append("ops_price_initialization_telemetry_incomplete_or_duplicate")
    else:
        themes = [
            row.get("theme") if isinstance(row, dict) else None
            for row in observations
        ]
        if (any(type(theme) is not str for theme in themes)
                or set(themes) != expected_themes):
            issues.append("ops_price_initialization_telemetry_theme_mismatch")
        required_ints = {
            "products_get_count": 1,
            "http_status": 200,
            "rendered_product_count": 2,
            "expected_product_count": 2,
        }
        invalid_observation = False
        for row in observations:
            if not isinstance(row, dict):
                invalid_observation = True
                continue
            if row.get("actual_theme") != row.get("theme"):
                invalid_observation = True
            if any(type(row.get(field)) is not int or row[field] != expected
                   for field, expected in required_ints.items()):
                invalid_observation = True
            if type(row.get("selected_count")) is not int or row["selected_count"] != 0:
                invalid_observation = True
            if row.get("synthetic_products_exact") is not True:
                invalid_observation = True
            if row.get("success") is not True or row.get("loading") is not False:
                invalid_observation = True
        if invalid_observation:
            issues.append("ops_price_initialization_telemetry_invalid")

    request_failures = data.get("request_failures")
    if not isinstance(request_failures, list):
        issues.append("ops_request_failure_telemetry_missing")
    elif request_failures:
        issues.append("ops_request_failures_present")

    pages = data.get("pages")
    expected_page_pairs = {
        (label, theme)
        for label in OPERATIONS_PRICING_PAGE_LABELS
        for theme in ("light", "dark")
    }
    observed_page_pairs = []
    page_rows_valid = isinstance(pages, list) and len(pages) == 32
    if isinstance(pages, list):
        for row in pages:
            if not isinstance(row, dict):
                page_rows_valid = False
                continue
            label, theme = row.get("label"), row.get("theme")
            if (type(label) is not str or type(theme) is not str
                    or row.get("status") != 200):
                page_rows_valid = False
                continue
            observed_page_pairs.append((label, theme))
    if (not page_rows_valid or len(observed_page_pairs) != 32
            or len(set(observed_page_pairs)) != 32
            or set(observed_page_pairs) != expected_page_pairs):
        issues.append("ops_page_theme_matrix_incomplete_or_duplicate")

    layouts = data.get("layouts")
    expected_layout_rows = {
        (label, theme, width, text_scale)
        for label in OPERATIONS_PRICING_PAGE_LABELS
        for theme in ("light", "dark")
        for width, text_scale in OPERATIONS_PRICING_LAYOUT_VARIANTS
    }
    observed_layout_rows = []
    layout_rows_valid = isinstance(layouts, list) and len(layouts) == 256
    if isinstance(layouts, list):
        for row in layouts:
            if not isinstance(row, dict):
                layout_rows_valid = False
                continue
            label = row.get("page")
            requested_theme = row.get("requestedTheme")
            actual_theme = row.get("actualTheme")
            width = row.get("width")
            text_scale = row.get("textScale")
            if (type(label) is not str
                    or type(requested_theme) is not str
                    or actual_theme != requested_theme
                    or type(width) is not int
                    or type(text_scale) is not int):
                layout_rows_valid = False
                continue
            observed_layout_rows.append((label, requested_theme, width, text_scale))
    if (not layout_rows_valid or len(observed_layout_rows) != 256
            or len(set(observed_layout_rows)) != 256
            or set(observed_layout_rows) != expected_layout_rows):
        issues.append("ops_layout_matrix_incomplete_or_duplicate_or_invalid")
    interactions = data.get("interactions")
    if not isinstance(interactions, list) or len(interactions) != 32:
        issues.append("ops_interaction_matrix_incomplete")
    return issues


def summarize_browser_report(path: Path, expected_source: str,
                             allow_synthetic_login: bool = False,
                             allow_synthetic_common_content: bool = False,
                             require_synthetic_wb_edit: bool = False,
                             require_operations_pricing: bool = False,
                             minimum_layout_count: int = 0,
                             minimum_interaction_count: int = 0,
                             required_interaction_fields: tuple[str, ...] = (
                                 "interactions", "checks",
                             ),
                             require_workspace_browser: bool = False,
                             require_classic_draft_facts: bool = False) -> dict:
    if not path.is_file():
        return {"valid": False, "reason": "browser_report_missing", "source": None,
                "report_status": None, "error_count": 0, "blocked_count": 0,
                "provider_attempts": 0, "page_count": 0, "layout_count": 0,
                "interaction_count": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"valid": False, "reason": "browser_report_invalid", "source": None,
                "report_status": None, "error_count": 0, "blocked_count": 0,
                "provider_attempts": 0, "page_count": 0, "layout_count": 0,
                "interaction_count": 0}
    if not isinstance(data, dict):
        return {"valid": False, "reason": "browser_report_not_object", "source": None,
                "report_status": None, "error_count": 0, "blocked_count": 0,
                "provider_attempts": 0, "page_count": 0, "layout_count": 0,
                "interaction_count": 0}
    source = data.get("source", data.get("variant"))
    status = data.get("status")
    missing_safety: list[str] = []
    if "provider_attempts" not in data:
        missing_safety.append("provider_attempts")
    if not any(isinstance(data.get(field), list)
               for field in ("unexpected_external_requests", "external")):
        missing_safety.append("external_request_telemetry")
    if not any(isinstance(data.get(field), list)
               for field in ("javascript_errors", "js_errors", "console_errors")):
        missing_safety.append("javascript_error_telemetry")
    if not any(isinstance(data.get(field), list)
               for field in ("blocked_writes", "browser_mutations", "writes")):
        missing_safety.append("write_telemetry")
    error_count = sum(
        len(data[field]) for field in ERROR_LIST_FIELDS
        if isinstance(data.get(field), list)
    )
    error_count += len(missing_safety)
    blocked_count = sum(
        len(data[field]) for field in ("blocked_writes", "browser_mutations")
        if isinstance(data.get(field), list)
    )
    writes = data.get("writes", [])
    synthetic_auth_writes = 0
    synthetic_common_content_writes = 0
    if not isinstance(writes, list):
        error_count += 1
    elif allow_synthetic_common_content:
        synthetic_actions = data.get("synthetic_actions")
        counters_valid = (
            isinstance(synthetic_actions, dict)
            and type(synthetic_actions.get("preview_requests")) is int
            and synthetic_actions["preview_requests"] >= 0
            and type(synthetic_actions.get("apply_requests")) is int
            and synthetic_actions["apply_requests"] >= 0
            and synthetic_actions.get("provider_attempts") == 0
        )
        rows_valid = all(
            isinstance(row, dict) and row.get("method") == "POST"
            and row.get("path") in {
                "/api/my-products/common-content/preview",
                "/api/my-products/common-content/apply",
            }
            and row.get("kind") in {"synthetic_preview", "synthetic_apply"}
            and ((row.get("kind") == "synthetic_preview"
                  and row.get("path") == "/api/my-products/common-content/preview")
                 or (row.get("kind") == "synthetic_apply"
                     and row.get("path") == "/api/my-products/common-content/apply"))
            for row in writes
        )
        observed = {
            "preview_requests": sum(row.get("kind") == "synthetic_preview" for row in writes if isinstance(row, dict)),
            "apply_requests": sum(row.get("kind") == "synthetic_apply" for row in writes if isinstance(row, dict)),
        }
        expected = {
            "preview_requests": synthetic_actions["preview_requests"] if counters_valid else -1,
            "apply_requests": synthetic_actions["apply_requests"] if counters_valid else -1,
        }
        if counters_valid and rows_valid and observed == expected and len(writes) == sum(expected.values()):
            synthetic_common_content_writes = len(writes)
        else:
            error_count += max(1, len(writes))
    elif writes:
        allowed_auth = (
            allow_synthetic_login and len(writes) <= 2
            and all(isinstance(row, dict)
                    and row.get("method") == "POST"
                    and row.get("path") == "/login" for row in writes)
        )
        if allowed_auth:
            synthetic_auth_writes = len(writes)
        else:
            error_count += len(writes)
    provider_attempts = data.get("provider_attempts", 0)
    if not isinstance(provider_attempts, int) or provider_attempts < 0:
        provider_attempts = 1
    error_count += int(bool(data.get("harness_error")))
    error_count += int(bool(data.get("error")))
    layout_count = next((
        len(data[field]) for field in ("layouts", "geometry")
        if isinstance(data.get(field), list) and data[field]
    ), 0)
    interaction_count = next((
        len(data[field]) for field in required_interaction_fields
        if isinstance(data.get(field), list) and data[field]
    ), 0)
    missing_evidence = []
    if layout_count == 0:
        missing_evidence.append("layout_rows")
    if interaction_count == 0:
        missing_evidence.append("interaction_rows")
    if layout_count < minimum_layout_count:
        missing_evidence.append(f"layout_rows_below_{minimum_layout_count}")
    if interaction_count < minimum_interaction_count:
        missing_evidence.append(f"interaction_rows_below_{minimum_interaction_count}")
    common_protocol_issues = (
        _common_content_protocol_issues(data)
        if allow_synthetic_common_content else []
    )
    missing_evidence.extend(common_protocol_issues)
    wb_edit_protocol_issues = (
        _wb_edit_protocol_issues(data) if require_synthetic_wb_edit else []
    )
    missing_evidence.extend(wb_edit_protocol_issues)
    workspace_protocol_issues = (
        _workspace_legacy_action_protocol_issues(data)
        if require_workspace_browser else []
    )
    missing_evidence.extend(workspace_protocol_issues)
    classic_draft_protocol_issues = (
        _classic_draft_facts_protocol_issues(data)
        if require_classic_draft_facts else []
    )
    missing_evidence.extend(classic_draft_protocol_issues)
    operations_pricing_protocol_issues = (
        _operations_pricing_protocol_issues(data) if require_operations_pricing else []
    )
    missing_evidence.extend(operations_pricing_protocol_issues)
    error_count += len(missing_evidence)
    valid = (status in PASS_REPORT_STATUSES and source == expected_source
             and error_count == 0 and provider_attempts == 0)
    return {
        "valid": valid,
        "reason": None if valid else (
            "browser_report_safety_telemetry_missing" if missing_safety
            else "browser_required_evidence_missing" if missing_evidence
            else "browser_report_status_source_or_error_check_failed"
        ),
        "missing_safety_telemetry": missing_safety,
        "source": source,
        "report_status": status,
        "error_count": error_count,
        "blocked_count": blocked_count,
        "synthetic_auth_writes": synthetic_auth_writes,
        "synthetic_common_content_writes": synthetic_common_content_writes,
        "provider_attempts": provider_attempts,
        "page_count": len(data.get("pages", [])) if isinstance(data.get("pages"), list) else 0,
        "layout_count": layout_count,
        "interaction_count": interaction_count,
        "missing_evidence": missing_evidence,
        "common_content_protocol_issues": common_protocol_issues,
        "wb_edit_protocol_issues": wb_edit_protocol_issues,
        "workspace_protocol_issues": workspace_protocol_issues,
        "classic_draft_protocol_issues": classic_draft_protocol_issues,
        "operations_pricing_protocol_issues": operations_pricing_protocol_issues,
    }


def _browser_env(name: str, stage_dir: Path, chromium: str) -> tuple[tuple[str, str], ...]:
    artifacts = stage_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    report = stage_dir / "browser-report.json"
    shared = {
        "CHROMIUM_BIN": chromium,
        "UX01_CHROMIUM": chromium,
        "UX01_BROWSER_CHROMIUM": chromium,
        "OZON_BROWSER_CHROMIUM": chromium,
    }
    specific = {
        "analytics": {},
        "listing": {
            "UX01_LISTING_VARIANT": "after",
            "UX01_LISTING_ARTIFACTS": str(artifacts),
        },
        "workspace": {
            "UX01_WORKSPACE_SOURCE": "worktree",
            "UX01_WORKSPACE_ARTIFACTS": str(artifacts),
            "UX01_WORKSPACE_REPORT": str(report),
        },
        "journey": {
            "UX01_JOURNEY_SOURCE": "worktree",
            "UX01_JOURNEY_ARTIFACTS": str(artifacts),
            "UX01_JOURNEY_REPORT": str(report),
        },
        "operations_pricing": {
            "UX01_OPERATIONS_PRICING_SOURCE": "worktree",
            "UX01_OPERATIONS_PRICING_ARTIFACTS": str(artifacts),
            "UX01_OPERATIONS_PRICING_REPORT": str(report),
        },
        "wb_edit": {
            "UX01_WB_EDIT_SOURCE": "worktree",
            "UX01_WB_EDIT_ARTIFACTS": str(artifacts),
            "UX01_WB_EDIT_REPORT": str(report),
        },
        "common_content": {
            "UX01_COMMON_CONTENT_SOURCE": "worktree",
            "UX01_COMMON_CONTENT_ARTIFACTS": str(artifacts),
            "UX01_COMMON_CONTENT_REPORT": str(report),
        },
    }[name]
    return tuple(sorted({**shared, **specific}.items()))


def build_stages(root: Path, output: Path, chromium: str) -> list[Stage]:
    pytest_paths = sorted((root / "tests").glob("test_ux01_*.py"))
    required = [root / item for item in REQUIRED_TESTS]
    missing = [path.relative_to(root).as_posix() for path in required if not path.is_file()]
    if missing:
        raise ValueError("required UX-01 regression tests are missing: " + ", ".join(missing))
    test_paths = sorted({*pytest_paths, *required}, key=lambda path: path.relative_to(root).as_posix())
    if not test_paths:
        raise ValueError("no tests/test_ux01_*.py files were found")

    pytest_dir = output / "pytest"
    pytest_dir.mkdir(parents=True, exist_ok=True)
    junit_path = pytest_dir / "results.xml"
    pytest_command = (
        sys.executable, "-m", "pytest", "-q", f"--junitxml={junit_path}",
        *(path.relative_to(root).as_posix() for path in test_paths),
    )
    stages = [Stage(
        name="ux01_pytest", kind="pytest", command=pytest_command,
        timeout_seconds=600, report_path=junit_path,
    )]

    browser_specs = (
        ("analytics_browser", "analytics", "tests/ux01/analytics_browser.py", "worktree", "analytics"),
        ("listing_browser", "listing", "tests/ux01/listing_browser.py", "after", "listing"),
        ("workspace_browser", "workspace", "tests/ux01/workspace_browser.py", "worktree", "workspace"),
        ("journey_browser", "journey", "tests/ux01/journey_browser.py", "worktree", "journey"),
        ("operations_pricing_browser", "operations_pricing", "tests/ux01/operations_pricing_browser.py", "worktree", "operations_pricing"),
        ("wb_edit_browser", "wb_edit", "tests/ux01/wb_edit_browser.py", "worktree", "wb_edit"),
        ("common_content_browser", "common_content", "tests/ux01/common_content_browser.py", "worktree", "common_content"),
    )
    for name, browser_key, script, expected_source, output_key in browser_specs:
        path = root / script
        stage_dir = output / name
        stage_dir.mkdir(parents=True, exist_ok=True)
        report_path = stage_dir / ("browser-report.json" if browser_key in {
            "workspace", "journey", "operations_pricing", "wb_edit", "common_content",
        }
                                   else f"{output_key}-report.json")
        if browser_key == "analytics":
            artifacts = stage_dir / "artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            command = (
                sys.executable, script, "--source", "worktree",
                "--report", str(report_path), "--artifacts", str(artifacts),
                "--chromium", chromium,
            )
            env = _browser_env(browser_key, stage_dir, chromium)
        elif browser_key == "listing":
            command = (sys.executable, script)
            env = _browser_env(browser_key, stage_dir, chromium)
            report_path = stage_dir / "artifacts" / "listing-browser-after.json"
        else:
            command = (sys.executable, script)
            env = _browser_env(browser_key, stage_dir, chromium)
        if not path.is_file():
            # Keep the missing path explicit in a command; stage execution will
            # record a bounded actionable failure instead of silently skipping.
            command = (sys.executable, script)
        stages.append(Stage(
            name=name, kind="browser", command=command, timeout_seconds=600,
            report_path=report_path, expected_source=expected_source,
            environment=env,
        ))
    return stages


def _clean_environment(root: Path, temp_dir: Path, chromium: str,
                       stage_environment: tuple[tuple[str, str], ...]) -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("PATH", "LD_LIBRARY_PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR")
        if key in os.environ
    }
    home = temp_dir / "home"
    tmp = temp_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    xdg = temp_dir / "xdg"
    for child in ("config", "cache", "data", "state"):
        (xdg / child).mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": os.environ.get("HOME", "/tmp"),
        "TMPDIR": str(tmp),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_STATE_HOME": str(xdg / "state"),
        "PYTHONPATH": str(root),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": env.get("LANG", "C.UTF-8"),
        "TZ": "UTC",
        "DATABASE_URL": "sqlite:///" + str(tmp / "ux01-stage.sqlite"),
        "SKIP_SCHEDULER": "1",
        "IMAGE_LAB_INLINE_WORKER": "0",
        "SECRET_KEY": "ux01-acceptance-synthetic-session-only",
        "ENCRYPTION_KEY": "dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXU=",
        "CHROMIUM_BIN": chromium,
        "UX01_CHROMIUM": chromium,
        "UX01_BROWSER_CHROMIUM": chromium,
        "OZON_BROWSER_CHROMIUM": chromium,
    })
    env.update(dict(stage_environment))
    return env


def _stop_process(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def execute_stage(stage: Stage, root: Path, output: Path, chromium: str) -> dict:
    stage_dir = output / stage.name
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "stage.log"
    started = time.monotonic()
    timed_out = False
    launch_error = None
    return_code = None
    with tempfile.TemporaryDirectory(prefix=f"ux01-{stage.name}-") as temp_name:
        env = _clean_environment(root, Path(temp_name), chromium, stage.environment)
        try:
            with log_path.open("wb") as log:
                process = subprocess.Popen(
                    stage.command,
                    cwd=root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name == "posix"),
                )
                try:
                    return_code = process.wait(timeout=stage.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _stop_process(process)
                    return_code = process.returncode
        except OSError as exc:
            launch_error = type(exc).__name__
    elapsed = round(time.monotonic() - started, 3)
    result = {
        "name": stage.name,
        "kind": stage.kind,
        "status": "failed",
        "exit_code": return_code,
        "timed_out": timed_out,
        "timeout_seconds": stage.timeout_seconds,
        "duration_seconds": elapsed,
        "log": log_path.relative_to(output).as_posix(),
        "error_type": launch_error,
    }
    if launch_error or timed_out or return_code != 0:
        result["reason"] = "launch_error" if launch_error else "timeout" if timed_out else "nonzero_exit"
        return result

    if stage.kind == "pytest":
        counts = parse_junit(stage.report_path or Path())
        result["counts"] = counts
        if counts["valid"] and not counts["skipped"] and not counts["failures"] and not counts["errors"]:
            result["status"] = "passed"
        else:
            result["reason"] = counts["reason"] or "pytest_counts_not_clean"
        result["xml"] = (stage.report_path.relative_to(output).as_posix()
                         if stage.report_path else None)
        return result

    summary = summarize_browser_report(
        stage.report_path or Path(),
        stage.expected_source or "",
        allow_synthetic_login=(stage.name == "listing_browser"),
        allow_synthetic_common_content=(stage.name == "common_content_browser"),
        require_synthetic_wb_edit=(stage.name == "wb_edit_browser"),
        require_workspace_browser=(stage.name == "workspace_browser"),
        require_classic_draft_facts=(stage.name == "journey_browser"),
        require_operations_pricing=(stage.name == "operations_pricing_browser"),
        minimum_layout_count=BROWSER_MINIMUMS.get(stage.name, {}).get("layouts", 0),
        minimum_interaction_count=BROWSER_MINIMUMS.get(stage.name, {}).get("interactions", 0),
        required_interaction_fields=BROWSER_INTERACTION_FIELDS.get(
            stage.name, ("interactions", "checks"),
        ),
    )
    result["counts"] = summary
    result["source"] = summary.get("source")
    result["source_verified"] = summary.get("source") == stage.expected_source
    if summary["valid"] and result["source_verified"]:
        result["status"] = "passed"
    else:
        result["reason"] = summary["reason"]
    result["report"] = (stage.report_path.relative_to(output).as_posix()
                        if stage.report_path else None)
    return result


def _validate_required_inputs(snapshot: dict, root: Path) -> list[str]:
    dynamic_tests = sorted((root / "tests").glob("test_ux01_*.py"))
    required = {
        "scripts/check_ux01.py",
        "tests/test_ux01_runner.py",
        *REQUIRED_TESTS,
        *(path.relative_to(root).as_posix() for path in dynamic_tests),
        *REQUIRED_BROWSERS,
    }
    included = {entry["path"] for entry in snapshot["files"]}
    return sorted(required - included)


def run_pipeline(manifest_path: Path, output: Path, chromium: str,
                 root: Path = ROOT) -> dict:
    root = root.resolve(strict=True)
    output = output.resolve()
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("--output must be outside the repository worktree")
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / REPORT_NAME
    report = {
        "schema_version": 1,
        "status": "running",
        "source_verified": False,
        "manifest": {"baseline": EXPECTED_BASELINE, "file_count": 0,
                     "aggregate_sha256": None},
        "environment_policy": "clean subprocess environment; temporary SQLite; scheduler disabled; no credentials",
        "stages": [],
    }
    try:
        if not chromium or not Path(chromium).is_absolute() or not os.access(chromium, os.X_OK):
            raise ValueError("--chromium must be an executable absolute path")
        snapshot = load_manifest(manifest_path, root)
        if snapshot["path"] == report_path:
            raise ValueError("manifest and runner report must be separate files")
        missing_inputs = _validate_required_inputs(snapshot, root)
        if missing_inputs:
            raise ValueError("manifest does not freeze all runner inputs: " + ", ".join(missing_inputs))
        report["manifest"].update({
            "generated_at": snapshot["generated_at"],
            "file_count": len(snapshot["files"]),
            "aggregate_sha256": snapshot["aggregate_sha256"],
        })
        before = verify_snapshot(snapshot, root)
        report["source_verified_before"] = before["verified"]
        if not before["verified"]:
            report["source_issues"] = before["issues"]
            raise ValueError("source does not match the UX-01 manifest before testing")

        stages = build_stages(root, output, chromium)
        for stage in stages:
            pre = verify_snapshot(snapshot, root)
            if not pre["verified"]:
                report["source_issues"] = pre["issues"]
                break
            result = execute_stage(stage, root, output, chromium)
            result["source_verified_before"] = pre["verified"]
            post = verify_snapshot(snapshot, root)
            result["source_verified_after"] = post["verified"]
            report["stages"].append(result)
            if not post["verified"]:
                report["source_issues"] = post["issues"]
                break

        final = verify_snapshot(snapshot, root)
        report["source_verified_after"] = final["verified"]
        report["source_verified"] = bool(report.get("source_verified_before") and final["verified"])
        if not final["verified"]:
            report["source_issues"] = final["issues"]
        all_stages_passed = len(report["stages"]) == len(stages) and all(
            stage["status"] == "passed" for stage in report["stages"]
        )
        report["status"] = "passed" if report["source_verified"] and all_stages_passed else "failed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if "snapshot" in locals():
            final = verify_snapshot(snapshot, root)
            report["source_verified_after"] = final["verified"]
            report["source_verified"] = bool(report.get("source_verified_before") and final["verified"])
            if not final["verified"]:
                report["source_issues"] = final["issues"]
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--chromium", required=True)
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(args.manifest, args.output, args.chromium)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({
        "status": result["status"],
        "source_verified": result.get("source_verified", False),
        "stages": len(result["stages"]),
        "passed_stages": sum(stage.get("status") == "passed" for stage in result["stages"]),
        "report": str(args.output.resolve() / REPORT_NAME),
    }, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
