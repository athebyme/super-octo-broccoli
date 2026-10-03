#!/usr/bin/env python3
"""Run the frozen UX-01 offline acceptance stages in sequence.

The source manifest is produced by the host-side UX-01 acceptance workflow and
is deliberately kept outside the manifest's own input list. This script runs
inside the network-disabled acceptance container; it does not start the app,
touch a persistent database, or inherit seller/provider credentials.
"""

from __future__ import annotations

import argparse
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
    "mixed_fixture_preview_selected50_eligible2_changed2_skipped48",
    "mixed_fixture_confirm_writes_exact_two_provider_products_with_history_readback",
})
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
        "characteristic_ids": [202, 303, 404],
        "characteristics": [
            {"id": 202, "value": ["Россия"]},
            {"id": 303, "value": 125},
            {"id": 404, "value": ["Пластик", "Металл"]},
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
        and readback.get("characteristic_ids") == [101, 202, 303, 404]
        and type(readback.get("size_count")) is int and readback["size_count"] == 1
        and readback.get("sku") == "SYNTHETIC-WB-SKU-000"
        and type(readback.get("direct_history_count")) is int
        and readback["direct_history_count"] == 1
        and readback.get("history_changed_fields") == ["characteristics"]
    ):
        issues.append("wb_single_edit_form_and_history_evidence_incomplete")

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


def _operations_pricing_protocol_issues(data: dict) -> list[str]:
    """Require exact receipts for the WB prices page's read-only first load."""
    issues: list[str] = []

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
                             )) -> dict:
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
