from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path
import tempfile
import unittest
import base64
import os
import subprocess
import sys
from unittest.mock import patch

from scripts.check_ux01 import (
    BROWSER_INTERACTION_FIELDS,
    BROWSER_MINIMUMS,
    CLASSIC_DRAFT_FACTS_CHECK,
    COMMON_CONTENT_LAYOUT_STATES,
    COMMON_CONTENT_LAYOUT_THEMES,
    COMMON_CONTENT_LAYOUT_WIDTHS,
    COMMON_CONTENT_MOBILE_THEMES,
    COMMON_CONTENT_MOBILE_WIDTHS,
    COMMON_CONTENT_NAVIGATOR_CHECK,
    COMMON_CONTENT_REQUIRED_CHECKS,
    COMMON_CONTENT_REQUIRED_FOCUS,
    OPERATIONS_PRICING_PAGE_LABELS,
    OPERATIONS_PRICING_LAYOUT_VARIANTS,
    OPERATIONS_PRICE_INIT_CHECK,
    WB_HISTORY_AGGREGATE_CARDS,
    WB_HISTORY_DOMAIN_COUNTS,
    WB_HISTORY_PARENT_AGGREGATES,
    WB_QUANTITY_ROLLBACK_NOTE,
    WB_EDIT_LAYOUT_THEMES,
    WB_EDIT_LAYOUT_WIDTHS,
    WB_EDIT_PAGES,
    WB_EDIT_REQUIRED_CHECKS,
    WB_EDIT_CONTRAST_CHECK,
    WB_EDIT_CONTRAST_THEMES,
    WB_EDIT_CONTRAST_CONTROL_NAMES,
    REQUIRED_TESTS,
    Stage,
    _canonical_json,
    _clean_environment,
    _safe_repo_file,
    build_stages,
    execute_stage,
    load_manifest,
    parse_junit,
    summarize_browser_report,
    verify_snapshot,
)
from scripts.check_ozon_release import EXTRA_TESTS as OZON_EXTRA_TESTS


def _operations_pricing_browser_report() -> dict:
    pages = [
        {"label": label, "theme": theme, "status": 200}
        for label in sorted(OPERATIONS_PRICING_PAGE_LABELS)
        for theme in ("light", "dark")
    ]
    checks = [
        {"name": OPERATIONS_PRICE_INIT_CHECK, "status": "passed", "theme": theme}
        for theme in ("light", "dark")
    ]
    price_initialization = [
        {
            "theme": theme,
            "actual_theme": theme,
            "products_get_count": 1,
            "http_status": 200,
            "success": True,
            "rendered_product_count": 2,
            "expected_product_count": 2,
            "loading": False,
            "selected_count": 0,
            "synthetic_products_exact": True,
        }
        for theme in ("light", "dark")
    ]
    return {
        "status": "completed",
        "source": "worktree",
        "provider_attempts": 0,
        "unexpected_external_requests": [],
        "unexpected_http": [],
        "request_failures": [],
        "javascript_errors": [],
        "console_errors": [],
        "blocked_writes": [],
        "browser_mutations": [],
        "writes": [],
        "pages": pages,
        "layouts": [
            {
                "page": label,
                "requestedTheme": theme,
                "actualTheme": theme,
                "width": width,
                "textScale": text_scale,
            }
            for label in sorted(OPERATIONS_PRICING_PAGE_LABELS)
            for theme in ("light", "dark")
            for width, text_scale in OPERATIONS_PRICING_LAYOUT_VARIANTS
        ],
        "interactions": [
            {"check": "keyboard_route_focus", "passed": True, "row": index}
            for index in range(32)
        ],
        "checks": checks,
        "price_initialization": price_initialization,
        "history_scenario_checks": _operations_history_scenario_checks(),
        "history_domain_sql_writes": [],
        "history_domain_state": {
            "before": copy.deepcopy(WB_HISTORY_DOMAIN_COUNTS),
            "after": copy.deepcopy(WB_HISTORY_DOMAIN_COUNTS),
            "unchanged": True,
        },
    }


def _operations_history_scenario_checks() -> list[dict]:
    origin = "http://127.0.0.1:41111"
    product_ids = list(range(20001, 20032))
    foreign_id = product_ids[-2]
    owned_ids = [product_id for product_id in product_ids if product_id != foreign_id]
    owned_statuses = ["success"] * 25 + [
        "failed", "submitted", "uncertain", "partial", "pending",
    ]
    status_by_id = dict(zip(owned_ids, owned_statuses))
    status_by_id[foreign_id] = "conflict"
    product_statuses = [
        {"product_id": product_id, "wb_sync_status": status_by_id[product_id]}
        for product_id in product_ids
    ]
    status_counts = {
        "success": 25, "failed": 1, "pending": 1, "submitted": 1,
        "uncertain": 1, "partial": 1, "conflict": 1,
    }
    rendered_status_counts = {
        "success": 25, "failed": 1, "pending": 1, "submitted": 1,
        "uncertain": 1, "partial": 1,
    }
    outcome_text = {
        "success": "WB сообщил об успехе.",
        "failed": "WB вернул ошибку; проверьте фактическое состояние перед новым действием.",
        "pending": "Ожидается отправка или подтверждение.",
        "submitted": "Изменение отправлено; итог ещё требует проверки.",
        "uncertain": "Точный исход неизвестен. Не повторяйте изменение до сверки с WB.",
        "partial": "WB подтвердил только часть изменения; проверьте сохранённые значения.",
    }
    rendered_product_statuses = [
        {
            "product_id": product_id,
            "wb_sync_status": status,
            "readable_outcome": f"Результат WB: {outcome_text[status]}",
        }
        for product_id, status in zip(owned_ids, owned_statuses)
    ]
    quantities = [
        {
            "product_id": product_id,
            "before": index + 20,
            "after": index + 21,
            "rendered_before": str(index + 20),
            "rendered_after": str(index + 21),
        }
        for index, product_id in enumerate(owned_ids)
    ]
    return [
        {
            "name": "wb_history_batch31_exact_rows_values_and_outcomes",
            "status": "passed",
            "bulk_id": 905,
            "origin": origin,
            "method": "GET",
            "path": "/bulk-history/905",
            "http_status": 200,
            "page_heading": "R10 synthetic batch31 mixed WB row outcomes",
            "total_products": 31,
            "operation_status": "in_progress",
            "operation_success_count": 29,
            "operation_error_count": 1,
            "operation_completed_at": None,
            "operation_duration_seconds": None,
            "pending_unprocessed_product_id": owned_ids[-1],
            "rendered_rows": 31,
            "owned_visible_rows": 30,
            "foreign_hidden_rows": 1,
            "product_ids": product_ids,
            "owned_product_ids": owned_ids,
            "foreign_product_id": foreign_id,
            "status_counts": status_counts,
            "rendered_status_counts": rendered_status_counts,
            "product_statuses": product_statuses,
            "rendered_product_statuses": rendered_product_statuses,
            "owned_quantity_values": quantities,
            "values_exact": True,
            "exact_owned_product_ids": True,
            "aggregates": copy.deepcopy(WB_HISTORY_PARENT_AGGREGATES),
            "aggregate_cards": copy.deepcopy(WB_HISTORY_AGGREGATE_CARDS),
        },
        {
            "name": "wb_history_owned_fix_link_opens_exact_product",
            "status": "passed",
            "origin": origin,
            "method": "GET",
            "path": f"/products/{owned_ids[0]}",
            "http_status": 200,
            "product_id": owned_ids[0],
            "clicked_label": "Карточка WB",
            "title_matches": True,
            "vendor_code_matches": True,
            "nm_id_matches": True,
        },
        {
            "name": "wb_history_foreign_fix_link_absent",
            "status": "passed",
            "foreign_product_id": foreign_id,
            "fix_link_count": 0,
            "history_link_count": 0,
            "private_text_absent": True,
        },
        {
            "name": "wb_history_unresolved_rows_no_retry_or_revert",
            "status": "passed",
            "unresolved_product_statuses": [
                {"product_id": product_id, "wb_sync_status": status}
                for product_id, status in zip(owned_ids, owned_statuses)
                if status in {"pending", "submitted", "uncertain", "partial"}
            ],
            "retry_affordances_absent": True,
            "revert_form_count": 0,
            "mutation_count": 0,
            "post_count": 0,
            "quantity_rollback_contract_supported": False,
            "unsupported_rollback_note_rendered": False,
            "completed_quantity_rollback_view": {
                "operation_id": 906,
                "origin": origin,
                "method": "GET",
                "path": "/bulk-history/906",
                "http_status": 200,
                "page_heading": "R10 completed quantity-only rollback fixture",
                "operation_status": "completed",
                "total_products": 1,
                "success_count": 1,
                "error_count": 0,
                "operation_seller_id": 77,
                "product_seller_id": 77,
                "owned_identity_matches": True,
                "card_edit_history_count": 1,
                "product_id": 20032,
                "product_title": "Synthetic quantity history product",
                "vendor_code": "R10-QTY-ROLLBACK",
                "nm_id": 70000032,
                "title_matches": True,
                "vendor_code_matches": True,
                "nm_id_matches": True,
                "changed_fields": ["quantity"],
                "snapshot_before": {"quantity": 17},
                "snapshot_after": {"quantity": 18},
                "rendered_before": "17",
                "rendered_after": "18",
                "safe_revert_supported": False,
                "revert_form_count": 0,
                "unsupported_note_visible": True,
                "unsupported_note_text": WB_QUANTITY_ROLLBACK_NOTE,
                "post_count": 0,
            },
        },
    ]


def _workspace_browser_report() -> dict:
    origin = "http://127.0.0.1:41112"
    account_id = 77
    product_id = 30101
    photo_product_id = 30102
    return {
        "status": "completed",
        "source": "worktree",
        "provider_attempts": 0,
        "unexpected_external_requests": [],
        "unexpected_http_requests": [],
        "javascript_errors": [],
        "console_errors": [],
        "browser_mutations": [],
        "pages": [{} for _ in range(37)],
        "layouts": [{} for _ in range(43)],
        "interactions": [{} for _ in range(28)],
        "legacy_action_checks": [
            {
                "name": "legacy_sidebar_keyboard_activation_reaches_exact_routes",
                "status": "passed", "origin": origin, "method": "GET",
                "path": "/products/merge", "http_status": 200,
                "activation": "Tab+Enter", "label": "Объединить карточки WB",
                "page_heading": "Объединение карточек WB",
            },
            {
                "name": "command_palette_enter_reaches_help_and_social",
                "status": "passed",
                "routes": [
                    {"label": "Документация", "origin": origin, "method": "GET",
                     "path": "/docs/", "http_status": 200,
                     "activation": "Ctrl+K+Enter", "page_heading": "Документация"},
                    {"label": "Социальные подключения", "origin": origin, "method": "GET",
                     "path": "/content-factory/accounts", "http_status": 200,
                     "activation": "Ctrl+K+Enter", "page_heading": "Подключённые аккаунты"},
                ],
            },
            {
                "name": "legacy_product_actions_open_exact_product_routes",
                "status": "passed", "origin": origin, "method": "GET", "product_id": product_id,
                "foreign_product_id": 30104,
                "foreign_scope_denial": {
                    "method": "GET", "path": "/products/30104", "http_status": 404,
                },
                "actions": [
                    {"label": "История", "origin": origin, "method": "GET",
                     "path": f"/products/{product_id}/history", "http_status": 200,
                     "page_heading": "История изменений карточки", "title_matches": True},
                    {"label": "Обогатить", "origin": origin, "method": "GET",
                     "path": f"/products/{product_id}/enrich", "http_status": 200,
                     "page_heading": "Обогащение от поставщика", "title_matches": True},
                    {"label": "Редактировать", "origin": origin, "method": "GET",
                     "path": f"/products/{product_id}/edit", "http_status": 200,
                     "page_heading": "Редактирование карточки", "title_matches": True},
                ],
            },
            {
                "name": "command_palette_account_link_preserves_selected_account",
                "status": "passed", "palette_label": "Карточки кабинетов",
                "palette_href_path": "/marketplaces/listings/", "origin": origin,
                "method": "GET", "path": "/marketplaces/listings/", "http_status": 200,
                "page_heading": "Каталог маркетплейсов",
                "selected_account_id": account_id, "rendered_account_label": "Ozon CI 0",
                "downstream_account_href_path": "/marketplaces/drafts/",
                "downstream_account_query": {"account_id": account_id},
                "account_context_preserved": True,
            },
            {
                "name": "wb_only_tool_labels_and_image_lab_source_are_distinct",
                "status": "passed", "origin": origin, "method": "GET",
                "path": "/image-lab", "http_status": 200, "page_heading": "Фотостудия",
                "wb_tool_heading": "Инструменты Wildberries",
                "wb_merge_href_path": "/products/merge",
                "ozon_listings_href_path": "/marketplaces/listings/",
                "ozon_listings_query": {"account_id": account_id},
                "ozon_listing_page_heading": "Каталог маркетплейсов",
                "image_lab_page_heading": "Фотостудия",
                "ozon_account_label": "Ozon CI 0", "groups_distinct": True,
            },
            {
                "name": "image_lab_fixture_photo_loads_with_imported_source_context",
                "status": "passed", "origin": origin, "method": "GET",
                "path": "/image-lab",
                "http_status": 200, "page_heading": "Фотостудия",
                "source_product_id": photo_product_id,
                "source_type": "imported_product", "source_title": "Synthetic imported photo source",
                "listing_id": 605, "listing_account_id": account_id,
                "fake_transport_read_count": 1, "fake_photo_sha256": "a" * 64,
                "original_get": {
                    "method": "GET",
                    "path": f"/image-lab/api/products/{photo_product_id}/original",
                    "status": 200,
                    "content_type": "image/png",
                },
                "image_natural_width": 64, "image_natural_height": 64,
            },
            {
                "name": "image_lab_empty_manual_override_suppresses_wb_fallback",
                "status": "passed", "source_product_id": 30103,
                "explicit_empty_override": True, "excluded_from_lab": True,
                "override_schema_version": 1, "content_edit_version": 2,
                "override_photo_count": 0, "effective_photo_count": 0,
                "inherited_source_photo_count": 1,
                "wb_linked_product_id": product_id,
                "wb_photo_fallback_reads": 0, "wb_photo_fallback_downloads": 0,
                "experiments_before": 0, "experiments_after": 0,
            },
        ],
        "legacy_domain_sql_writes": [],
        "legacy_domain_state": {
            "before": {
                "products": 1, "card_edit_history": 1, "imported_products": 2,
                "marketplace_listings": 1, "image_generation_experiments": 0,
            },
            "after": {
                "products": 1, "card_edit_history": 1, "imported_products": 2,
                "marketplace_listings": 1, "image_generation_experiments": 0,
            },
            "unchanged": True,
        },
        "legacy_post_count": 0,
        "expected_http_denials": [{
            "method": "GET", "path": "/products/30104", "status": 404,
        }],
        "expected_denial_console_errors": [{
            "method": "GET", "origin": origin, "path": "/products/30104",
            "http_status": 404,
            "text": "Failed to load resource: the server responded with a status of 404 (NOT FOUND)",
            "location_url": f"{origin}/products/30104",
        }],
        "image_lab_fake_reads": [{
            "transport": "synthetic_imported_photo", "fake_photo_sha256": "a" * 64,
        }],
        "image_lab_wb_fallback_reads": [],
        "image_lab_wb_fallback_downloads": 0,
    }


def _common_content_browser_report() -> dict:
    layouts = [
        {
            "kind": kind,
            "state": state,
            "width": width,
            "theme": theme,
            "page_overflow": False,
        }
        for state, kind in COMMON_CONTENT_LAYOUT_STATES.items()
        for width in COMMON_CONTENT_LAYOUT_WIDTHS
        for theme in COMMON_CONTENT_LAYOUT_THEMES
    ]
    focus_observations = []
    for name in COMMON_CONTENT_REQUIRED_FOCUS:
        row = {
            "check": name,
            "observed": True,
            "target_supported": True,
            "enabled": True,
            "visible": True,
            "focus_visible": True,
            "outline_visible": True,
            "outline": {"style": "solid", "width": 2, "color": "rgb(20, 80, 180)"},
            "rect": {"x": 20, "y": 30, "width": 80, "height": 28},
            "viewport": {"width": 1440, "height": 1050},
        }
        if name in {
            "common_photo_boundary_focus_first",
            "common_photo_boundary_focus_last",
        }:
            row.update({
                "same_photo": True,
                "focused_photo_url": "https://fixture.test/photo.svg",
                "focused_direction": "1" if name == "common_photo_boundary_focus_first" else "-1",
                "target": {
                    "photo_url": "https://fixture.test/photo.svg",
                    "direction": "1" if name == "common_photo_boundary_focus_first" else "-1",
                },
            })
        else:
            row.update({
                "target_action": "preview",
                "focused_action": "preview",
                "same_trigger": True,
            })
        focus_observations.append(row)
    writes = [
        *([{"method": "POST", "path": "/api/my-products/common-content/preview", "kind": "synthetic_preview"}] * 7),
        *([{"method": "POST", "path": "/api/my-products/common-content/apply", "kind": "synthetic_apply"}] * 4),
    ]
    mobile_touch_check = {
        "name": "common_mobile_touch_targets_44px",
        "status": "passed",
        "ok": True,
        "passed": True,
    }
    mobile_navigator_check = {
        "name": COMMON_CONTENT_NAVIGATOR_CHECK,
        "status": "passed",
        "ok": True,
        "passed": True,
        "layouts": 6,
        "expected_layouts": 6,
        "failed_layouts": [],
    }
    mobile_navigator_observations = [
        {
            "state": "selected",
            "width": width,
            "theme": theme,
            "card_count": 2,
            "selection_height_px": 190,
            "card_widths_px": [230, 230],
            "card_button_heights_px": [108, 108],
            "local_horizontal_scroll": True,
            "local_scroll_after_tab_px": 230,
            "page_overflow": False,
            "page_scroll_stable": True,
            "keyboard_reached_second_by_tab": True,
            "keyboard_returned_first_by_shift_tab": True,
            "second_focus_visible": True,
            "first_focus_visible": True,
            "second_focus_geometry_wait_completed": True,
            "first_focus_geometry_wait_completed": True,
            "second_focus_outline_within_scrollport": True,
            "first_focus_outline_within_scrollport": True,
            "second_focus_ring_geometry": {
                "focus_ring": {"left": 6, "top": 6, "right": 26, "bottom": 26},
                "scrollport": {"left": 5, "top": 5, "right": 27, "bottom": 27},
            },
            "first_focus_ring_geometry": {
                "focus_ring": {"left": 6, "top": 6, "right": 26, "bottom": 26},
                "scrollport": {"left": 5, "top": 5, "right": 27, "bottom": 27},
            },
            "current_product_preserved": True,
            "full_second_title_dom": True,
            "full_second_sku_dom": True,
            "full_second_accessible_name": True,
            "full_second_title_tooltip": True,
            "full_second_external_id_within_model_limit": True,
            "full_current_heading": True,
            "product_api_reads_during": 0,
            "mutating_requests_during": 0,
            "preview_apply_requests_during": 0,
            "no_product_api_reads": True,
            "no_mutating_requests": True,
            "passed": True,
        }
        for width in COMMON_CONTENT_MOBILE_WIDTHS
        for theme in COMMON_CONTENT_MOBILE_THEMES
    ]
    selected_ids = list(range(10001, 10051))
    selected_fingerprint = hashlib.sha256(
        ",".join(str(value) for value in selected_ids).encode("ascii")
    ).hexdigest()
    overflow_fingerprint = hashlib.sha256(
        ",".join(str(value) for value in [*selected_ids, 10051]).encode("ascii")
    ).hexdigest()
    expected_413 = [
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
    expected_413_console = [
        {
            **receipt,
            "message": "Failed to load resource: the server responded with a status of 413 (Payload Too Large)",
            "location": {
                "origin": "http://127.0.0.1:40001",
                "path": receipt["path"],
                "has_query": receipt["has_query"],
                "has_fragment": False,
                "url_too_long": False,
            },
        }
        for receipt in expected_413
    ]
    check_names = sorted(COMMON_CONTENT_REQUIRED_CHECKS - {
        mobile_touch_check["name"], COMMON_CONTENT_NAVIGATOR_CHECK,
    }) + [
        "existing-review-safety", "existing-cancel-reopen", mobile_touch_check,
        mobile_navigator_check,
    ]
    return {
        "status": "passed",
        "source": "worktree",
        "provider_attempts": 0,
        "unexpected_external_requests": [],
        "unexpected_http_requests": [],
        "javascript_errors": [],
        "console_errors": [],
        "layouts": layouts,
        "checks": check_names,
        "focus_observations": focus_observations,
        "mobile_product_navigator_observations": mobile_navigator_observations,
        "writes": writes,
        "preview_item_counts": [1, 1, 1, 1, 51, 50, 50],
        "preview_selection_observations": [
            {"item_count": 1, "unique_product_count": 1,
             "product_id_fingerprint": "6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b"},
            {"item_count": 1, "unique_product_count": 1,
             "product_id_fingerprint": "d4735e3a265e16eee03f59718b9b5d03019c07d8b6c51f90da3a666eec13ab35"},
            {"item_count": 1, "unique_product_count": 1,
             "product_id_fingerprint": "6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b"},
            {"item_count": 1, "unique_product_count": 1,
             "product_id_fingerprint": "d4735e3a265e16eee03f59718b9b5d03019c07d8b6c51f90da3a666eec13ab35"},
            {"item_count": 51, "unique_product_count": 51,
             "product_id_fingerprint": overflow_fingerprint},
            {"item_count": 50, "unique_product_count": 50,
             "product_id_fingerprint": selected_fingerprint},
            {"item_count": 50, "unique_product_count": 50,
             "product_id_fingerprint": selected_fingerprint},
        ],
        "apply_result_counts": [1, 50],
        "apply_result_observations": [
            {"item_count": 1, "unique_product_count": 1,
             "product_id_fingerprint": "6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b"},
            {"item_count": 50, "unique_product_count": 50,
             "product_id_fingerprint": selected_fingerprint},
        ],
        "expected_http_rejections": expected_413,
        "expected_rejection_console_errors": expected_413_console,
        "bulk_50": {
            "selected_products": 50,
            "selected_product_id_fingerprint": selected_fingerprint,
            "page_51_rejected": True,
            "preview_api_51_rejected": True,
            "last_product_keyboard_reachable": True,
            "stale_apply_atomic_rejection": True,
            "stale_denial_unchanged_selected_products": 50,
            "stale_denial_new_audits": 0,
            "first_preview_product_ids_match": True,
            "recovery_preview_product_ids_match": True,
            "recovery_apply_product_ids_match": True,
            "recovery_preview_items": 50,
            "recovery_apply_items": 50,
            "persisted_overrides": 50,
            "audit_rows": 50,
            "final_content_edit_version_counts": {"2": 49, "3": 1},
            "channel_records_unchanged": True,
            "inheritance_and_source_preserved": True,
        },
        "synthetic_actions": {
            "preview_requests": 7,
            "apply_requests": 4,
            "expected_preview_conflicts": 1,
            "expected_apply_conflicts": 2,
            "empty_description_override_requests": 1,
            "provider_attempts": 0,
            "empty_route_api_reads": 0,
            "empty_route_mutating_requests": 0,
            "empty_state_catalog_link_available": True,
            "selected_photo_order_persisted": True,
            "channel_record_unchanged": True,
        },
    }


def _wb_contrast_control(name: str) -> dict:
    selectors = {
        "cancel": '.sticky.bottom-0 a[href^="/products/"]',
        "optional_picker_label": 'label[for="wb-optional-characteristic-picker"]',
        "optional_picker": "#wb-optional-characteristic-picker",
        "optional_add": "#wb-add-optional-characteristic",
        "save": 'form.space-y-6 button[type="submit"]',
    }
    return {
        "name": name,
        "selector": selectors[name],
        "text": name,
        "visible": True,
        "in_viewport": True,
        "enabled": True,
        "disabled": False,
        "computed_color": "rgb(0, 0, 0)",
        "computed_background_color": "rgb(255, 255, 255)",
        "computed_opacity": "1",
        "opacity_product": 1.0,
        "opacity_chain": [{"tag": "button", "id": name, "opacity": 1.0}],
        "effective_background_rgb": [255.0, 255.0, 255.0],
        "effective_foreground_rgb": [0.0, 0.0, 0.0],
        "contrast_ratio_estimate": 21.0,
        "normal_text_wcag_aa": True,
        "background_layers": [{
            "tag": "button", "id": name,
            "background_color": "rgb(255, 255, 255)",
            "background_rgb_after_compositing": [255.0, 255.0, 255.0],
            "background_image": "none", "opacity": 1.0, "filter": "none",
            "backdrop_filter": "none", "mix_blend_mode": "normal",
            "has_background_image": False, "background_changed": True,
        }],
        "ancestor_effects": {
            "has_background_image": False,
            "has_filter": False,
            "has_backdrop_filter": False,
            "has_non_normal_blend": False,
        },
    }


def _wb_contrast_diagnostic() -> list[dict]:
    selectors = [
        '.sticky.bottom-0 a[href^="/products/"]',
        'label[for="wb-optional-characteristic-picker"]',
        "#wb-optional-characteristic-picker",
        "#wb-add-optional-characteristic",
        'form.space-y-6 button[type="submit"]',
    ]
    return [
        {
            "requested_theme": theme,
            "actual_theme": theme,
            "viewport": {"width": 390, "height": 900},
            "measurement_valid": True,
            "appearance_stability": {
                "settled": True,
                "samples": 4,
                "stable_frames": 3,
                "elapsed_ms": 100.0,
                "active_relevant_transitions": [],
                "final_computed_styles": [
                    {
                        "selector": selector,
                        "color": "rgb(0, 0, 0)",
                        "background_color": "rgb(255, 255, 255)",
                        "border_color": "rgb(0, 0, 0)",
                        "opacity": "1",
                        "box_shadow": "none",
                        "outline_color": "rgb(0, 0, 0)",
                    }
                    for selector in selectors
                ],
            },
            "controls": [_wb_contrast_control(name) for name in WB_EDIT_CONTRAST_CONTROL_NAMES],
        }
        for theme in WB_EDIT_CONTRAST_THEMES
    ]


def _wb_edit_browser_report() -> dict:
    layouts = []
    for page in WB_EDIT_PAGES:
        for theme in WB_EDIT_LAYOUT_THEMES:
            for width in WB_EDIT_LAYOUT_WIDTHS:
                left = 0 if width < 1024 else 260
                content_width = width - left
                layouts.append({
                    "page": page,
                    "theme": theme,
                    "viewport_width": width,
                    "document_width": width,
                    "body_width": width,
                    "main_width": width,
                    "main_content_left": left,
                    "main_content_width": content_width,
                    "layout_settle": {
                        "theme": theme,
                        "stable_frames": 3,
                        "fonts_ready": True,
                        "theme_ready": True,
                        "transitions_running": False,
                        "viewport_width": width,
                        "main_content_left": left,
                        "main_content_width": content_width,
                    },
                })
    required_checks = []
    for name in sorted(WB_EDIT_REQUIRED_CHECKS):
        if name == "single_edit_owner_session_cannot_post_foreign_product":
            required_checks.append({
                "name": name, "status": "passed", "http_status": 404,
                "provider_writes": 1,
            })
        elif name == "single_edit_foreign_owner_session_cannot_post_seller_product":
            required_checks.append({
                "name": name, "status": "passed", "http_status": 404,
                "provider_writes": 1, "separate_browser_session": True,
            })
        elif name == WB_EDIT_CONTRAST_CHECK:
            required_checks.append({
                "name": name, "status": "passed",
                "themes": list(WB_EDIT_CONTRAST_THEMES),
                "control_names": list(WB_EDIT_CONTRAST_CONTROL_NAMES),
                "control_count": 10, "minimum_contrast_ratio": 4.5,
                "all_enabled_visible": True, "all_settled": True,
                "all_contrast_aa": True, "failures": [],
            })
        else:
            required_checks.append({"name": name, "status": "passed"})
    generic_checks = [
        {"name": f"fixture_interaction_{index}", "status": "passed"}
        for index in range(24 - len(required_checks))
    ]
    checks = required_checks + generic_checks
    for check in checks:
        if check["name"] == "single_edit_real_form_submit_reaches_fake_wb_and_persists_exact_history":
            check.update({
                "changed_characteristics": [202],
                "submit_button_label": "Сохранить в WB",
                "target_channel": "Wildberries",
                "required_missing_value": "",
                "required_missing_omitted_from_patch": True,
                "direct_history_count": 1,
                "sizes_and_sku_preserved": True,
            })
        elif check["name"] == "single_edit_uses_cached_country_weight_multi_schema_and_read_only_sku":
            check.update({
                "subject_id": 5880,
                "schema_fields": 31,
                "initial_visible_field_ids": [101, 303, 404, 500, 502, 506],
                "required_missing_id": 500,
                "stale_read_only_id": 501,
                "stale_read_only_disclosure_keyboard": True,
                "stale_read_only_displayed": True,
                "stale_read_only_control_count": 0,
                "optional_country_picker_label": "Страна производства",
                "picker_keyboard_selection": 202,
                "present_empty_id": 506,
                "present_empty_visible": True,
                "present_empty_excluded_from_picker": True,
                "empty_add_remove_dirty": False,
                "empty_add_no_post_or_provider": True,
                "fake_wb_write_calls": 0,
            })
    boundary_names = (
        "wrong_weight_unit", "non_numeric_weight_type", "unlisted_dictionary_value",
    )
    write_request = {
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
    return {
        "status": "complete",
        "source": "worktree",
        "provider_attempts": 0,
        "unexpected_external_requests": [],
        "unexpected_http": [],
        "javascript_errors": [],
        "browser_mutations": [],
        "layouts": layouts,
        "checks": checks,
        "single_edit_contrast_diagnostic": _wb_contrast_diagnostic(),
        "fake_wb_single_write_calls": 1,
        "fake_wb_single_write_requests": [write_request],
        "fake_wb_write_calls": 2,
        "fake_wb_written_products": [*range(900000, 900050), 910000, 910001],
        "fake_wb_client_instances": 3,
        "single_edit_observations": {
            "form_post": {
                "http_status": 302,
                "path": "/products/9876/edit",
                "submit_button_label": "Сохранить в WB",
                "target_channel": "Wildberries",
                "required_missing_id": 500,
                "required_missing_value": "",
                "required_missing_omitted_from_patch": True,
                "normal_html_form": True,
                "csrf_field_present": True,
                "fake_write_count": 1,
                "readback_and_history": {
                    "characteristic_ids": [101, 202, 303, 404, 501, 502, 506],
                    "size_count": 1,
                    "sku": "SYNTHETIC-WB-SKU-000",
                    "direct_history_count": 1,
                    "history_changed_fields": ["characteristics"],
                },
            },
            "core_form_alignment": {
                "persisted_core_values": {
                    "vendor_code": "SYNTHETIC-VENDOR-000",
                    "title": "Pipedream Synthetic Product 000",
                    "description": "Synthetic description",
                    "brand": "Synthetic reviewed brand",
                },
                "initial_form_values": {
                    "vendor_code": "SYNTHETIC-VENDOR-000",
                    "title": "Pipedream Synthetic Product 000",
                    "description": "Synthetic description",
                    "brand": "Synthetic reviewed brand",
                },
                "initial_mismatch_fields": [],
                "aligned_form_values": {
                    "vendor_code": "SYNTHETIC-VENDOR-000",
                    "title": "Pipedream Synthetic Product 000",
                    "description": "Synthetic description",
                    "brand": "Synthetic reviewed brand",
                },
                "exact_before_characteristic_submit": True,
            },
            "reopen": {
                "country": "Россия",
                "weight_grams": 125,
                "materials": ["Пластик", "Металл"],
                "present_empty_field_preserved": True,
                "sku_read_only": True,
            },
            "progressive_ui": {
                "initial_view": {
                    "viewport_width": 390,
                    "document_width": 390,
                    "schema_field_count": 31,
                    "visible_field_ids": [101, 303, 404, 500, 502, 506],
                    "saved_text_input": True,
                    "numeric_grams_input": True,
                    "dictionary_multiple_select": True,
                    "bounded_textarea": True,
                    "present_empty_visible": True,
                    "present_empty_excluded_from_picker": True,
                    "filled_summary": "4 заполнено · 31 в схеме",
                    "country_hidden_until_chosen": True,
                    "picker_country_label": "Страна производства",
                    "optional_choice_count": 25,
                    "required_missing_visible": True,
                    "required_missing_input_visible": True,
                    "has_changes": False,
                    "save_disabled": True,
                    "picker_box": {"width": 300, "height": 44},
                    "add_button_box": {"width": 200, "height": 44},
                },
                "stale_legacy_field_read_only": {
                    "field_id": 501,
                    "disclosure_opened_by_keyboard": True,
                    "field_text_present": True,
                    "saved_value_present": True,
                    "form_control_count": 0,
                },
                "empty_optional_add_keyboard": {
                    "picker_focus": {
                        "id": "wb-optional-characteristic-picker",
                        "focus_visible": True,
                        "selected_value": "202",
                    },
                    "add_button_focus": {
                        "text": "Добавить поле", "focus_visible": True, "box_height": 44,
                    },
                    "added_control_focus": {
                        "active_id": "char_202", "field_tag": "SELECT", "focus_visible": True,
                    },
                },
                "empty_add_enter_attempt": {
                    "still_on_edit_route": True,
                    "has_changes": False,
                    "save_disabled": True,
                    "post_count": 0,
                    "fake_client_instances": 0,
                    "fake_single_write_calls": 0,
                    "provider_attempts": 0,
                    "post_count_delta": 0,
                    "fake_client_delta": 0,
                    "fake_write_delta": 0,
                },
                "empty_add_request_submit_attempt": {
                    "submit_event": {"seen": True, "default_prevented": True},
                    "still_on_edit_route": True,
                    "post_count": 0,
                    "fake_client_instances": 0,
                    "fake_single_write_calls": 0,
                    "provider_attempts": 0,
                    "post_count_delta": 0,
                    "fake_client_delta": 0,
                    "fake_write_delta": 0,
                },
                "empty_optional_remove_keyboard": {
                    "has_changes": False,
                    "picker_focused": True,
                    "focus": {
                        "label": "Убрать пустое поле «Страна производства»",
                        "focus_visible": True,
                        "box_height": 44,
                    },
                    "post_count_delta": 0,
                    "fake_client_delta": 0,
                    "fake_write_delta": 0,
                },
                "country_picker_add_keyboard": {
                    "picker_focus": {
                        "id": "wb-optional-characteristic-picker",
                        "focus_visible": True,
                        "selected_value": "202",
                    },
                    "add_button_focus": {
                        "text": "Добавить поле", "focus_visible": True, "box_height": 44,
                    },
                    "added_control_focus": {
                        "active_id": "char_202", "field_tag": "SELECT", "focus_visible": True,
                    },
                },
            },
            "rejections": [
                {
                    "name": name,
                    "http_status": 200,
                    "provider_writes": 1,
                    "local_product_preserved": True,
                    "history_count": 1,
                }
                for name in boundary_names
            ],
            "seller_scope_denials": [
                {
                    "session": "owner", "target": "foreign_product",
                    "http_status": 404, "fake_writes_unchanged": True,
                },
                {
                    "session": "foreign_owner", "target": "seller_product",
                    "http_status": 404, "separate_browser_session": True,
                    "fake_writes_unchanged": True,
                },
            ],
            "no_profile_denial": {
                "final_path": "/dashboard",
                "redirected": True,
                "separate_browser_session": True,
                "fake_writes_unchanged": True,
            },
        },
        "single_edit_boundary_attempts": [
            {
                "name": name,
                "status": 200,
                "provider_writes": 1,
                "fake_client_instances": 1,
                "history_count": 1,
                "local_product_preserved": True,
            }
            for name in boundary_names
        ],
        "mixed_fixture_observations": {
            "selection": 50,
            "eligible": 2,
            "changed": 2,
            "skipped": 48,
            "errors": 0,
            "fake_provider_call_delta": 1,
            "fake_provider_product_ids": [910000, 910001],
            "history_id": 123,
            "history_product_ids": [20000, 20001],
            "history_success_count": 2,
        },
    }


def _classic_draft_facts_browser_report() -> dict:
    page_names = [
        "supplier_catalog", "supplier_products", "supplier_source_detail",
        "internal_ozon", "drafts_vue", "drafts_classic", "draft_detail_vue",
        "draft_detail_classic", "review", "upload_history", "upload_result",
        "internal_beta", "listing_vue", "listing_classic",
    ]
    page_visits = page_names[:11] + page_names[11:] + page_names[:11]
    macro_pages = page_names[:11]
    layouts = [
        {
            "page": page, "width": width, "theme": theme,
            "actual_theme": theme, "components": [],
        }
        for page in macro_pages
        for width in (320, 390, 768, 1024, 1440)
        for theme in ("light", "dark")
    ]
    layouts.extend(
        {
            "page": page, "width": width, "theme": theme,
            "actual_theme": theme, "components": [],
        }
        for page in ("listing_vue", "listing_classic")
        for width in (1440, 390)
        for theme in ("light", "dark")
    )
    rows = []
    for width in (320, 360):
        for theme in ("light", "dark"):
            client_width = width - 24
            region_rows = []
            for index in range(3):
                visible = index < 2
                region_rows.append({
                    "visible": visible,
                    "left_px": 8,
                    "right_px": width - 8,
                    "client_width_px": client_width,
                    "scroll_width_px": client_width + (120 if visible else 0),
                    "scrolls_horizontally": visible,
                    "overflow_x_auto": visible,
                    "role_region": True,
                    "has_accessible_name": True,
                    "tabindex": 0,
                    "min_height_px": 44,
                    "table_width_px": 352,
                    "table_min_width_px": 352,
                    "table_within_bounded_width": True,
                    "row_count": 2,
                    "value_wraps": True,
                })
            rows.append({
                "page": "draft_detail_classic",
                "width": width,
                "theme": theme,
                "actual_theme": theme,
                "navigation_receipt": "classic_content_navigation",
                "document_overflow_px": 0,
                "body_overflow_px": 0,
                "main_overflow_px": 0,
                "content_overflow_px": 0,
                "form_overflow_px": 0,
                "main_left_px": 0,
                "main_right_px": width,
                "summary_count": 1,
                "summaries_fit_viewport": True,
                "details_summary_bounds_px": [{
                    "visible": True, "details_open": True, "left": 8,
                    "right": width - 8, "inside_viewport": True,
                }],
                "fact_region_count": 3,
                "visible_fact_region_count": 2,
                "local_scroll_region_count": 2,
                "all_regions_accessible": True,
                "all_regions_fit_viewport": True,
                "all_visible_regions_have_touch_height": True,
                "all_visible_tables_within_bounded_width": True,
                "all_visible_values_wrap": True,
                "synthetic_fact_marker_visible": True,
                "full_snapshot_retains_synthetic_fact": True,
                "keyboard_focus_reached": True,
                "keyboard_focus_visible": True,
                "focus_outline_px": 2,
                "focus_outline_offset_px": 2,
                "focus_outline_visible": True,
                "focus_outline_inside_viewport": True,
                "keyboard_scroll_delta_px": 36,
                "classic_update_form_preserved": True,
                "classic_validate_form_preserved": True,
                "classic_refresh_form_preserved": True,
                "region_rows": region_rows,
            })
    return {
        "status": "completed",
        "source": "worktree",
        "provider_attempts": 0,
        "unexpected_external_requests": [],
        "unexpected_http": [],
        "javascript_errors": [],
        "browser_mutations": [],
        "writes": [],
        "pages": [
            {"name": name, "path": f"/fixture/{name}?visit={index}", "status": 200}
            for index, name in enumerate(page_visits)
        ],
        "layouts": layouts,
        "geometry": copy.deepcopy(layouts),
        "interactions": [{"name": f"existing_journey_{index}"} for index in range(22)],
        "checks": [CLASSIC_DRAFT_FACTS_CHECK],
        "classic_content_navigation": {
            "method": "GET",
            "status": 200,
            "same_loopback_origin": True,
            "exact_fixture_classic_path_match": True,
            "route_kind": "classic_draft_detail",
        },
        "classic_draft_content_layouts": rows,
    }


class Ux01RunnerContractTest(unittest.TestCase):
    def _manifest(self, root: Path, manifest_path: Path, entries: list[dict]) -> dict:
        payload = {
            "schema_version": 1,
            "baseline": "ba63371",
            "generated_at": "2026-09-30T12:00:00Z",
            "files": entries,
            "aggregate_sha256": hashlib.sha256(_canonical_json(entries)).hexdigest(),
        }
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_manifest_hashes_and_aggregate_are_verified(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            root = temp / "repo"
            root.mkdir()
            source = root / "templates" / "base.html"
            source.parent.mkdir()
            source.write_text("synthetic shell", encoding="utf-8")
            entry = {"path": "templates/base.html", "sha256": hashlib.sha256(
                source.read_bytes()).hexdigest()}
            manifest_path = temp / "manifest.json"
            self._manifest(root, manifest_path, [entry])

            snapshot = load_manifest(manifest_path, root)
            self.assertEqual(snapshot["aggregate_sha256"], hashlib.sha256(
                _canonical_json([entry])).hexdigest())
            self.assertTrue(verify_snapshot(snapshot, root)["verified"])

            source.write_text("changed shell", encoding="utf-8")
            changed = verify_snapshot(snapshot, root)
            self.assertFalse(changed["verified"])
            self.assertEqual(changed["issues"], [
                {"path": "templates/base.html", "reason": "sha256_mismatch"}
            ])

    def test_manifest_rejects_bad_aggregate_and_unsupported_timestamp(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            root = temp / "repo"
            root.mkdir()
            source = root / "source.py"
            source.write_text("pass\n", encoding="utf-8")
            entry = {"path": "source.py", "sha256": hashlib.sha256(
                source.read_bytes()).hexdigest()}
            manifest_path = temp / "manifest.json"
            payload = self._manifest(root, manifest_path, [entry])
            payload["aggregate_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "aggregate_sha256"):
                load_manifest(manifest_path, root)

            payload["aggregate_sha256"] = hashlib.sha256(_canonical_json([entry])).hexdigest()
            payload["generated_at"] = "2026-09-30T12:00:00+03:00"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "UTC"):
                load_manifest(manifest_path, root)

    def test_manifest_paths_reject_traversal_absolute_and_symlink_paths(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            root = temp / "repo"
            root.mkdir()
            (root / "safe.txt").write_text("safe", encoding="utf-8")
            for bad in ("../outside.txt", "/etc/passwd", "a/../safe.txt", "a\\b"):
                with self.subTest(path=bad), self.assertRaises(ValueError):
                    _safe_repo_file(root, bad)
            external = temp / "outside.txt"
            external.write_text("outside", encoding="utf-8")
            try:
                (root / "linked.txt").symlink_to(external)
            except OSError:
                self.skipTest("symlinks are unavailable on this filesystem")
            with self.assertRaisesRegex(ValueError, "symlink"):
                _safe_repo_file(root, "linked.txt")

    def test_manifest_cannot_hash_itself_and_paths_must_be_regular_files(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            root = temp / "repo"
            root.mkdir()
            source = root / "source.py"
            source.write_text("pass\n", encoding="utf-8")
            manifest_path = temp / "manifest.json"
            entry = {"path": "../manifest.json", "sha256": "0" * 64}
            self._manifest(root, manifest_path, [entry])
            with self.assertRaises(ValueError):
                load_manifest(manifest_path, root)

            directory_entry = {"path": "folder", "sha256": "0" * 64}
            (root / "folder").mkdir()
            self._manifest(root, manifest_path, [directory_entry])
            # The path check rejects directories before a digest can be accepted.
            with self.assertRaisesRegex(ValueError, "regular file"):
                load_manifest(manifest_path, root)

    def test_junit_empty_skipped_and_failed_cases_never_pass(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "results.xml"
            path.write_text('<testsuite tests="0"></testsuite>', encoding="utf-8")
            empty = parse_junit(path)
            self.assertFalse(empty["valid"])
            self.assertEqual(empty["reason"], "junit_xml_empty")

            path.write_text(
                '<testsuite><testcase name="ok"/><testcase name="skip"><skipped/></testcase></testsuite>',
                encoding="utf-8",
            )
            skipped = parse_junit(path)
            self.assertTrue(skipped["valid"])
            self.assertEqual(skipped["skipped"], 1)
            self.assertEqual(skipped["passed"], 1)

            path.write_text(
                '<testsuite><testcase name="bad"><failure/></testcase><testcase name="error"><error/></testcase></testsuite>',
                encoding="utf-8",
            )
            failed = parse_junit(path)
            self.assertEqual((failed["failures"], failed["errors"], failed["passed"]), (1, 1, 0))

    def test_browser_report_requires_expected_source_clean_status_and_no_errors(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "browser.json"
            path.write_text(json.dumps({
                "status": "completed", "source": "worktree", "pages": [{}],
                "layouts": [{}, {}], "interactions": [{}], "provider_attempts": 0,
                "javascript_errors": [], "unexpected_external_requests": [],
                "browser_mutations": [],
            }), encoding="utf-8")
            passed = summarize_browser_report(path, "worktree")
            self.assertTrue(passed["valid"])
            self.assertEqual(passed["page_count"], 1)
            self.assertEqual(passed["layout_count"], 2)

            failed_source = summarize_browser_report(path, "after")
            self.assertFalse(failed_source["valid"])
            report = json.loads(path.read_text(encoding="utf-8"))
            report["javascript_errors"] = ["synthetic"]
            path.write_text(json.dumps(report), encoding="utf-8")
            failed_js = summarize_browser_report(path, "worktree")
            self.assertFalse(failed_js["valid"])
            self.assertEqual(failed_js["error_count"], 1)

            report["javascript_errors"] = []
            report["http_errors"] = [{"path": "/synthetic", "status": 400}]
            path.write_text(json.dumps(report), encoding="utf-8")
            failed_http = summarize_browser_report(path, "worktree")
            self.assertFalse(failed_http["valid"])
            self.assertEqual(failed_http["error_count"], 1)

            report["http_errors"] = []
            report["request_failures"] = [{"method": "GET", "path": "/synthetic"}]
            path.write_text(json.dumps(report), encoding="utf-8")
            failed_request = summarize_browser_report(path, "worktree")
            self.assertFalse(failed_request["valid"])
            self.assertEqual(failed_request["error_count"], 1)

            path.write_text(json.dumps({"status": "passed", "source": "worktree"}), encoding="utf-8")
            missing_safety = summarize_browser_report(path, "worktree")
            self.assertFalse(missing_safety["valid"])
            self.assertEqual(missing_safety["reason"], "browser_report_safety_telemetry_missing")

    def test_workspace_legacy_action_receipts_are_separate_and_strict(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "workspace.json"

            def summarize(report):
                path.write_text(json.dumps(report), encoding="utf-8")
                return summarize_browser_report(
                    path, "worktree", require_workspace_browser=True,
                    minimum_layout_count=43, minimum_interaction_count=28,
                    required_interaction_fields=("interactions",),
                )

            report = _workspace_browser_report()
            accepted = summarize(report)
            self.assertTrue(accepted["valid"], accepted)
            self.assertEqual(accepted["page_count"], 37)
            self.assertEqual(accepted["layout_count"], 43)
            self.assertEqual(accepted["interaction_count"], 28)
            self.assertEqual(accepted["workspace_protocol_issues"], [])

            invalid_reports = []
            missing_receipt = copy.deepcopy(report)
            missing_receipt["legacy_action_checks"].pop()
            invalid_reports.append(("missing legacy receipt", missing_receipt,
                                    "workspace_legacy_action_receipts_missing_duplicate_or_failed"))

            duplicate_receipt = copy.deepcopy(report)
            duplicate_receipt["legacy_action_checks"][-1] = copy.deepcopy(
                duplicate_receipt["legacy_action_checks"][0]
            )
            invalid_reports.append(("duplicate legacy receipt", duplicate_receipt,
                                    "workspace_legacy_action_receipts_missing_duplicate_or_failed"))

            failed_receipt = copy.deepcopy(report)
            failed_receipt["legacy_action_checks"][0]["status"] = "complete"
            invalid_reports.append(("non-passed status", failed_receipt,
                                    "workspace_legacy_action_receipts_missing_duplicate_or_failed"))

            wrong_route = copy.deepcopy(report)
            wrong_route["legacy_action_checks"][0]["path"] = "/products/merge/confirm"
            invalid_reports.append(("wrong exact route", wrong_route,
                                    "workspace_legacy_sidebar_route_receipt_invalid"))

            wrong_origin = copy.deepcopy(report)
            wrong_origin["legacy_action_checks"][0]["origin"] = "https://fixture.test"
            invalid_reports.append(("non-loopback origin", wrong_origin,
                                    "workspace_legacy_sidebar_route_receipt_invalid"))

            wrong_product_scope = copy.deepcopy(report)
            wrong_product_scope["legacy_action_checks"][2]["actions"][1]["path"] = (
                "/products/999999/enrich"
            )
            invalid_reports.append(("product action crossed fixture ID", wrong_product_scope,
                                    "workspace_product_action_routes_incomplete_or_wrong"))

            signed64_overflow_id = copy.deepcopy(report)
            signed64_overflow_id["legacy_action_checks"][2]["product_id"] = 2**63
            invalid_reports.append(("legacy fixture ID exceeds signed 64-bit range", signed64_overflow_id,
                                    "workspace_product_action_routes_incomplete_or_wrong"))

            wrong_foreign_scope = copy.deepcopy(report)
            wrong_foreign_scope["legacy_action_checks"][2]["foreign_scope_denial"]["path"] = (
                "/products/30105"
            )
            invalid_reports.append(("foreign denial points to another ID", wrong_foreign_scope,
                                    "workspace_product_action_routes_incomplete_or_wrong"))

            for label, field, value in (
                ("wrong origin", "origin", "http://127.0.0.1:49999"),
                ("wrong path", "path", "/products/30105"),
                ("wrong method", "method", "POST"),
                ("wrong status", "http_status", 200),
                ("wrong text", "text", "Failed to load resource: 404"),
            ):
                bad_console_receipt = copy.deepcopy(report)
                bad_console_receipt["expected_denial_console_errors"][0][field] = value
                invalid_reports.append((
                    f"foreign denial console {label}", bad_console_receipt,
                    "workspace_foreign_denial_console_receipt_invalid",
                ))

            extra_console_receipt = copy.deepcopy(report)
            extra_console_receipt["expected_denial_console_errors"].append(
                copy.deepcopy(extra_console_receipt["expected_denial_console_errors"][0])
            )
            invalid_reports.append((
                "extra foreign denial console receipt", extra_console_receipt,
                "workspace_foreign_denial_console_receipt_invalid",
            ))

            wrong_account = copy.deepcopy(report)
            wrong_account["legacy_action_checks"][3]["downstream_account_query"]["account_id"] = 78
            invalid_reports.append(("account context changed", wrong_account,
                                    "workspace_account_context_route_receipt_invalid"))

            missing_known_photo_binding = copy.deepcopy(report)
            missing_known_photo_binding["image_lab_fake_reads"][0]["fake_photo_sha256"] = "b" * 64
            invalid_reports.append(("known photo bytes differ", missing_known_photo_binding,
                                    "workspace_image_lab_transport_reads_unexpected"))

            photo_page_is_not_image_lab = copy.deepcopy(report)
            photo_page_is_not_image_lab["legacy_action_checks"][5]["path"] = (
                "/image-lab/api/products/30102/original"
            )
            invalid_reports.append((
                "photo page route is not Image Lab", photo_page_is_not_image_lab,
                "workspace_image_lab_known_photo_receipt_invalid",
            ))

            photo_original_get_crossed_id = copy.deepcopy(report)
            photo_original_get_crossed_id["legacy_action_checks"][5]["original_get"]["path"] = (
                "/image-lab/api/products/99999/original"
            )
            invalid_reports.append((
                "photo original GET crossed product ID", photo_original_get_crossed_id,
                "workspace_image_lab_known_photo_receipt_invalid",
            ))

            fallback_used = copy.deepcopy(report)
            fallback_used["image_lab_wb_fallback_reads"] = [{"product_id": 30103}]
            invalid_reports.append(("WB fallback was read", fallback_used,
                                    "workspace_image_lab_transport_reads_unexpected"))

            missing_schema_proof = copy.deepcopy(report)
            missing_schema_proof["legacy_action_checks"][6]["override_schema_version"] = 2
            invalid_reports.append(("empty override schema differs", missing_schema_proof,
                                    "workspace_empty_manual_photo_fallback_receipt_invalid"))

            experiment_changed = copy.deepcopy(report)
            experiment_changed["legacy_domain_state"]["after"]["image_generation_experiments"] = 1
            invalid_reports.append(("experiment count changed", experiment_changed,
                                    "workspace_legacy_domain_state_changed_or_write_attempted"))

            post_attempted = copy.deepcopy(report)
            post_attempted["legacy_post_count"] = 1
            invalid_reports.append(("POST occurred", post_attempted,
                                    "workspace_legacy_domain_state_changed_or_write_attempted"))

            matrix_changed = copy.deepcopy(report)
            matrix_changed["interactions"].append({})
            invalid_reports.append(("legacy matrix grew", matrix_changed,
                                    "workspace_original_37_43_28_matrices_changed"))

            for label, invalid, expected_issue in invalid_reports:
                with self.subTest(case=label):
                    rejected = summarize(invalid)
                    self.assertFalse(rejected["valid"], (label, rejected))
                    self.assertIn(expected_issue, rejected["workspace_protocol_issues"])

    def test_operations_pricing_requires_single_load_receipts_and_original_matrix(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "operations-pricing.json"
            minimums = BROWSER_MINIMUMS["operations_pricing_browser"]
            self.assertEqual(minimums, {"layouts": 256, "interactions": 32})

            def summarize(report):
                path.write_text(json.dumps(report), encoding="utf-8")
                return summarize_browser_report(
                    path,
                    "worktree",
                    require_operations_pricing=True,
                    minimum_layout_count=minimums["layouts"],
                    minimum_interaction_count=minimums["interactions"],
                    required_interaction_fields=("interactions", "checks"),
                )

            report = _operations_pricing_browser_report()
            accepted = summarize(report)
            self.assertTrue(accepted["valid"], accepted)
            self.assertEqual(accepted["page_count"], 32)
            self.assertEqual(accepted["layout_count"], 256)
            self.assertEqual(accepted["interaction_count"], 32)

            invalid_reports = []

            missing_check = copy.deepcopy(report)
            missing_check["checks"].pop()
            invalid_reports.append(("one theme named check missing", missing_check,
                                    "ops_price_initialization_named_check_incomplete_or_duplicate"))

            duplicate_check = copy.deepcopy(report)
            duplicate_check["checks"].append(copy.deepcopy(duplicate_check["checks"][0]))
            invalid_reports.append(("named check duplicated", duplicate_check,
                                    "ops_price_initialization_named_check_incomplete_or_duplicate"))

            failed_check = copy.deepcopy(report)
            failed_check["checks"][0]["status"] = "failed"
            invalid_reports.append(("named check failed", failed_check,
                                    "ops_price_initialization_named_check_failed"))

            mismatched_check_theme = copy.deepcopy(report)
            mismatched_check_theme["checks"][1]["theme"] = "light"
            invalid_reports.append(("named check theme duplicated", mismatched_check_theme,
                                    "ops_price_initialization_named_check_incomplete_or_duplicate"))

            missing_telemetry = copy.deepcopy(report)
            missing_telemetry.pop("price_initialization")
            invalid_reports.append(("price telemetry missing", missing_telemetry,
                                    "ops_price_initialization_telemetry_incomplete_or_duplicate"))

            duplicate_telemetry = copy.deepcopy(report)
            duplicate_telemetry["price_initialization"].append(
                copy.deepcopy(duplicate_telemetry["price_initialization"][0])
            )
            invalid_reports.append(("price telemetry duplicated", duplicate_telemetry,
                                    "ops_price_initialization_telemetry_incomplete_or_duplicate"))

            wrong_telemetry_theme = copy.deepcopy(report)
            wrong_telemetry_theme["price_initialization"][1]["theme"] = "light"
            wrong_telemetry_theme["price_initialization"][1]["actual_theme"] = "light"
            invalid_reports.append(("price telemetry misses dark theme", wrong_telemetry_theme,
                                    "ops_price_initialization_telemetry_theme_mismatch"))

            actual_theme_mismatch = copy.deepcopy(report)
            actual_theme_mismatch["price_initialization"][0]["actual_theme"] = "dark"
            invalid_reports.append(("actual browser theme differs", actual_theme_mismatch,
                                    "ops_price_initialization_telemetry_invalid"))

            repeated_products_get = copy.deepcopy(report)
            repeated_products_get["price_initialization"][0]["products_get_count"] = 2
            invalid_reports.append(("products endpoint fetched twice", repeated_products_get,
                                    "ops_price_initialization_telemetry_invalid"))

            failed_get = copy.deepcopy(report)
            failed_get["price_initialization"][0]["http_status"] = 503
            invalid_reports.append(("products response failed", failed_get,
                                    "ops_price_initialization_telemetry_invalid"))

            false_success = copy.deepcopy(report)
            false_success["price_initialization"][0]["success"] = False
            invalid_reports.append(("products response unsuccessful", false_success,
                                    "ops_price_initialization_telemetry_invalid"))

            not_rendered = copy.deepcopy(report)
            not_rendered["price_initialization"][0]["rendered_product_count"] = 1
            invalid_reports.append(("products not rendered", not_rendered,
                                    "ops_price_initialization_telemetry_invalid"))

            busy = copy.deepcopy(report)
            busy["price_initialization"][0]["loading"] = True
            invalid_reports.append(("page still loading", busy,
                                    "ops_price_initialization_telemetry_invalid"))

            wrong_count_type = copy.deepcopy(report)
            wrong_count_type["price_initialization"][0]["products_get_count"] = "1"
            invalid_reports.append(("count telemetry is not an integer", wrong_count_type,
                                    "ops_price_initialization_telemetry_invalid"))

            nonempty_selection = copy.deepcopy(report)
            nonempty_selection["price_initialization"][0]["selected_count"] = 1
            invalid_reports.append(("fixture selection is not empty", nonempty_selection,
                                    "ops_price_initialization_telemetry_invalid"))

            wrong_selection_type = copy.deepcopy(report)
            wrong_selection_type["price_initialization"][0]["selected_count"] = False
            invalid_reports.append(("selection count is not an integer", wrong_selection_type,
                                    "ops_price_initialization_telemetry_invalid"))

            wrong_fixture_products = copy.deepcopy(report)
            wrong_fixture_products["price_initialization"][0]["synthetic_products_exact"] = False
            invalid_reports.append(("rendered products differ from exact fixture", wrong_fixture_products,
                                    "ops_price_initialization_telemetry_invalid"))

            missing_fixture_proof = copy.deepcopy(report)
            missing_fixture_proof["price_initialization"][0].pop("synthetic_products_exact")
            invalid_reports.append(("exact fixture proof missing", missing_fixture_proof,
                                    "ops_price_initialization_telemetry_invalid"))

            missing_history_receipt = copy.deepcopy(report)
            missing_history_receipt["history_scenario_checks"].pop()
            invalid_reports.append(("history named receipt missing", missing_history_receipt,
                                    "ops_history_scenario_receipts_missing_duplicate_or_failed"))

            duplicate_history_receipt = copy.deepcopy(report)
            duplicate_history_receipt["history_scenario_checks"][-1] = copy.deepcopy(
                duplicate_history_receipt["history_scenario_checks"][0]
            )
            invalid_reports.append(("history named receipt duplicated", duplicate_history_receipt,
                                    "ops_history_scenario_receipts_missing_duplicate_or_failed"))

            failed_history_receipt = copy.deepcopy(report)
            failed_history_receipt["history_scenario_checks"][0]["status"] = "complete"
            invalid_reports.append(("history receipt not exact passed", failed_history_receipt,
                                    "ops_history_scenario_receipts_missing_duplicate_or_failed"))

            wrong_history_route = copy.deepcopy(report)
            wrong_history_route["history_scenario_checks"][0]["path"] = "/bulk-history/906"
            invalid_reports.append(("history route points at another operation", wrong_history_route,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            duplicated_history_id = copy.deepcopy(report)
            duplicated_history_id["history_scenario_checks"][0]["product_ids"][-1] = (
                duplicated_history_id["history_scenario_checks"][0]["product_ids"][0]
            )
            invalid_reports.append(("history product ID duplicated", duplicated_history_id,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            foreign_wrong_status = copy.deepcopy(report)
            foreign_wrong_status["history_scenario_checks"][0]["product_statuses"][-1][
                "wb_sync_status"
            ] = "failed"
            invalid_reports.append(("foreign control status is not conflict", foreign_wrong_status,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            quantity_not_rendered = copy.deepcopy(report)
            quantity_not_rendered["history_scenario_checks"][0]["owned_quantity_values"][0][
                "rendered_before"
            ] = "999"
            invalid_reports.append(("before value differs from DOM", quantity_not_rendered,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            exposed_foreign_link = copy.deepcopy(report)
            exposed_foreign_link["history_scenario_checks"][2]["fix_link_count"] = 1
            invalid_reports.append(("foreign fix link exposed", exposed_foreign_link,
                                    "ops_history_foreign_fix_link_or_data_exposed"))

            retry_available = copy.deepcopy(report)
            retry_available["history_scenario_checks"][3]["retry_affordances_absent"] = False
            invalid_reports.append(("unresolved rows offer retry", retry_available,
                                    "ops_history_unresolved_retry_or_revert_affordance_present"))

            mismatched_rendered_status = copy.deepcopy(report)
            mismatched_rendered_status["history_scenario_checks"][0][
                "rendered_product_statuses"
            ][0]["wb_sync_status"] = "failed"
            invalid_reports.append(("DOM status differs from stored row", mismatched_rendered_status,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            unreadable_rendered_status = copy.deepcopy(report)
            unreadable_rendered_status["history_scenario_checks"][0][
                "rendered_product_statuses"
            ][0]["readable_outcome"] = "The update is complete."
            invalid_reports.append(("status semantics not rendered", unreadable_rendered_status,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            child_counter_inferred_for_parent = copy.deepcopy(report)
            child_counter_inferred_for_parent["history_scenario_checks"][0]["aggregates"] = {
                "total_products": 31, "success_count": 25, "error_count": 6,
            }
            invalid_reports.append(("parent counters replaced by child statuses", child_counter_inferred_for_parent,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            completed_parent = copy.deepcopy(report)
            completed_parent["history_scenario_checks"][0]["operation_status"] = "completed"
            invalid_reports.append(("parent falsely marked completed", completed_parent,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            parent_timestamped = copy.deepcopy(report)
            parent_timestamped["history_scenario_checks"][0]["operation_completed_at"] = "2026-10-04T00:00:00"
            invalid_reports.append(("in-progress parent has completion time", parent_timestamped,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            pending_not_last = copy.deepcopy(report)
            pending_not_last["history_scenario_checks"][0]["pending_unprocessed_product_id"] = (
                pending_not_last["history_scenario_checks"][0]["owned_product_ids"][0]
            )
            invalid_reports.append(("unprocessed pending row is not last", pending_not_last,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            aggregate_card_drift = copy.deepcopy(report)
            aggregate_card_drift["history_scenario_checks"][0]["aggregate_cards"][2]["value"] = "6"
            invalid_reports.append(("aggregate card differs from observed parent card", aggregate_card_drift,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            signed64_overflow_id = copy.deepcopy(report)
            signed64_overflow_id["history_scenario_checks"][0]["product_ids"][0] = 2**63
            invalid_reports.append(("history fixture ID exceeds signed 64-bit range", signed64_overflow_id,
                                    "ops_history_batch31_scope_values_or_outcomes_invalid"))

            nested_view_changes = [
                ("nested operation aliases in-progress batch", "operation_id", 905,
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
                ("nested completed route is wrong", "path", "/bulk-history/999",
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
                ("nested page is not completed", "operation_status", "in_progress",
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
                ("nested sample is foreign", "product_seller_id", 78,
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
                ("nested sample owner IDs disagree", "operation_seller_id", 78,
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
                ("nested history identity row count differs", "card_edit_history_count", 2,
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
                ("nested rendered identity mismatches", "nm_id_matches", False,
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
                ("nested sample has other changed fields", "changed_fields", ["quantity", "price"],
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
                ("quantity rollback is incorrectly available", "safe_revert_supported", True,
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
                ("unsupported rollback note hidden", "unsupported_note_visible", False,
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
                ("unsupported rollback text not observed", "unsupported_note_text", "hidden",
                 "ops_history_unresolved_retry_or_revert_affordance_present"),
            ]
            for label, key, value, issue in nested_view_changes:
                invalid = copy.deepcopy(report)
                invalid["history_scenario_checks"][3]["completed_quantity_rollback_view"][key] = value
                invalid_reports.append((label, invalid, issue))

            nested_post = copy.deepcopy(report)
            nested_post["history_scenario_checks"][3]["completed_quantity_rollback_view"]["post_count"] = 1
            invalid_reports.append(("nested history receipt includes a POST", nested_post,
                                    "ops_history_unresolved_retry_or_revert_affordance_present"))

            changed_history_state = copy.deepcopy(report)
            changed_history_state["history_domain_state"]["after"]["products"] = 1
            invalid_reports.append(("history domain count changed", changed_history_state,
                                    "ops_history_domain_state_changed_or_write_attempted"))

            extra_history_write = copy.deepcopy(report)
            extra_history_write["writes"].append({
                "method": "POST", "path": "/bulk-history/905/revert",
            })
            invalid_reports.append(("extra history POST", extra_history_write,
                                    "ops_history_domain_state_changed_or_write_attempted"))

            missing_request_failures = copy.deepcopy(report)
            missing_request_failures.pop("request_failures")
            invalid_reports.append(("fatal request-failure field missing", missing_request_failures,
                                    "ops_request_failure_telemetry_missing"))

            failed_request = copy.deepcopy(report)
            failed_request["request_failures"] = [{
                "page": "wb_prices_change", "theme": "light", "method": "GET",
                "path": "/prices/api/products", "resource_type": "fetch",
                "failure_text": "synthetic network failure",
            }]
            invalid_reports.append(("request failure recorded", failed_request,
                                    "ops_request_failures_present"))

            incomplete_pages = copy.deepcopy(report)
            incomplete_pages["pages"].pop()
            invalid_reports.append(("page/theme row missing", incomplete_pages,
                                    "ops_page_theme_matrix_incomplete_or_duplicate"))

            duplicate_page_theme = copy.deepcopy(report)
            duplicate_page_theme["pages"][-1]["theme"] = "light"
            invalid_reports.append(("page/theme row duplicated", duplicate_page_theme,
                                    "ops_page_theme_matrix_incomplete_or_duplicate"))

            missing_layout = copy.deepcopy(report)
            missing_layout["layouts"].pop()
            invalid_reports.append(("layout matrix shortened", missing_layout,
                                    "ops_layout_matrix_incomplete_or_duplicate_or_invalid"))

            duplicate_layout = copy.deepcopy(report)
            duplicate_layout["layouts"][-1] = copy.deepcopy(duplicate_layout["layouts"][0])
            invalid_reports.append(("layout tuple duplicated and omitted", duplicate_layout,
                                    "ops_layout_matrix_incomplete_or_duplicate_or_invalid"))

            missing_layout_variant = copy.deepcopy(report)
            missing_layout_variant["layouts"] = [
                row for row in missing_layout_variant["layouts"]
                if not (row["page"] == "wb_prices_change"
                        and row["requestedTheme"] == "light"
                        and row["width"] == 320
                        and row["textScale"] == 200)
            ]
            invalid_reports.append(("required width/text-scale tuple missing", missing_layout_variant,
                                    "ops_layout_matrix_incomplete_or_duplicate_or_invalid"))

            wrong_layout_scale = copy.deepcopy(report)
            wrong_layout_scale["layouts"][0]["textScale"] = 150
            invalid_reports.append(("unexpected text scale", wrong_layout_scale,
                                    "ops_layout_matrix_incomplete_or_duplicate_or_invalid"))

            wrong_layout_theme = copy.deepcopy(report)
            wrong_layout_theme["layouts"][0]["actualTheme"] = (
                "dark" if wrong_layout_theme["layouts"][0]["requestedTheme"] == "light" else "light"
            )
            invalid_reports.append(("measured layout theme differs", wrong_layout_theme,
                                    "ops_layout_matrix_incomplete_or_duplicate_or_invalid"))

            missing_interaction = copy.deepcopy(report)
            missing_interaction["interactions"].pop()
            invalid_reports.append(("interaction matrix shortened", missing_interaction,
                                    "ops_interaction_matrix_incomplete"))

            for label, invalid, expected_issue in invalid_reports:
                with self.subTest(case=label):
                    rejected = summarize(invalid)
                    self.assertFalse(rejected["valid"], (label, rejected))
                    self.assertIn(expected_issue, rejected["operations_pricing_protocol_issues"])

    def test_browser_report_rejects_noop_and_accepts_analytics_api_evidence(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "browser.json"
            report = {
                "status": "completed", "source": "worktree", "provider_attempts": 0,
                "unexpected_external_requests": [], "javascript_errors": [],
                "browser_mutations": [], "layouts": [], "interactions": [],
            }
            path.write_text(json.dumps(report), encoding="utf-8")
            no_op = summarize_browser_report(path, "worktree")
            self.assertFalse(no_op["valid"])
            self.assertEqual(no_op["reason"], "browser_required_evidence_missing")
            self.assertEqual(no_op["missing_evidence"], ["layout_rows", "interaction_rows"])

            report["layouts"] = [{"width": 390}]
            report.pop("interactions")
            report["api_calls"] = [{"method": "GET", "path": "/api/analytics/summary"}]
            path.write_text(json.dumps(report), encoding="utf-8")
            analytics = summarize_browser_report(
                path,
                "worktree",
                required_interaction_fields=BROWSER_INTERACTION_FIELDS["analytics_browser"],
            )
            self.assertTrue(analytics["valid"])
            self.assertEqual(analytics["interaction_count"], 1)

    def test_browser_report_counts_primary_fields_when_reports_duplicate_aliases(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "journey.json"
            layouts = [{"row": index} for index in range(118)]
            report = {
                "status": "completed", "source": "worktree", "provider_attempts": 0,
                "unexpected_external_requests": [], "javascript_errors": [],
                "browser_mutations": [],
                "layouts": layouts,
                "geometry": layouts,
                "interactions": [{"row": index} for index in range(22)],
                "checks": [{"row": index} for index in range(31)],
            }
            path.write_text(json.dumps(report), encoding="utf-8")

            summary = summarize_browser_report(
                path,
                "worktree",
                required_interaction_fields=("interactions", "checks"),
            )

            self.assertTrue(summary["valid"])
            self.assertEqual(summary["layout_count"], 118)
            self.assertEqual(summary["interaction_count"], 22)

    def test_journey_stage_requires_separate_classic_fact_navigation_and_dom_receipts(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "journey-classic.json"

            def summarize(report: dict) -> dict:
                path.write_text(json.dumps(report), encoding="utf-8")
                return summarize_browser_report(
                    path, "worktree", require_classic_draft_facts=True,
                )

            report = _classic_draft_facts_browser_report()
            accepted = summarize(report)
            self.assertTrue(accepted["valid"], accepted)
            self.assertEqual(accepted["page_count"], 25)
            self.assertEqual(accepted["layout_count"], 118)
            self.assertEqual(accepted["classic_draft_protocol_issues"], [])

            invalid_reports = []

            def changed(label, mutation, issue):
                invalid = copy.deepcopy(report)
                mutation(invalid)
                invalid_reports.append((label, invalid, issue))

            changed("missing named check", lambda value: value["checks"].clear(),
                    "journey_classic_facts_named_check_missing_duplicate_or_wrong_shape")
            changed("duplicate named check", lambda value: value["checks"].append(CLASSIC_DRAFT_FACTS_CHECK),
                    "journey_classic_facts_named_check_missing_duplicate_or_wrong_shape")
            changed("fabricated object check", lambda value: value["checks"].__setitem__(0, {
                "name": CLASSIC_DRAFT_FACTS_CHECK, "status": "passed",
            }), "journey_classic_facts_named_check_missing_duplicate_or_wrong_shape")

            changed("missing navigation receipt", lambda value: value.pop("classic_content_navigation"),
                    "journey_classic_facts_navigation_receipt_invalid")
            changed("navigation is not GET", lambda value: value["classic_content_navigation"].update(method="POST"),
                    "journey_classic_facts_navigation_receipt_invalid")
            changed("navigation status is not 200", lambda value: value["classic_content_navigation"].update(status=404),
                    "journey_classic_facts_navigation_receipt_invalid")
            changed("navigation claims foreign origin", lambda value: value["classic_content_navigation"].update(
                same_loopback_origin=False), "journey_classic_facts_navigation_receipt_invalid")
            changed("navigation does not match fixture route", lambda value: value["classic_content_navigation"].update(
                exact_fixture_classic_path_match=False), "journey_classic_facts_navigation_receipt_invalid")
            changed("navigation has wrong route kind", lambda value: value["classic_content_navigation"].update(
                route_kind="draft_detail_vue"), "journey_classic_facts_navigation_receipt_invalid")
            changed("navigation carries unreviewed fields", lambda value: value["classic_content_navigation"].update(
                response_body="raw"), "journey_classic_facts_navigation_receipt_invalid")

            changed("missing DOM case", lambda value: value["classic_draft_content_layouts"].pop(),
                    "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("duplicate DOM case", lambda value: value["classic_draft_content_layouts"].__setitem__(
                3, copy.deepcopy(value["classic_draft_content_layouts"][0])),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("DOM row bound to another navigation", lambda value: value[
                "classic_draft_content_layouts"][0].update(navigation_receipt="other"),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("wrong measured theme", lambda value: value["classic_draft_content_layouts"][0].update(
                actual_theme="dark"), "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("root viewport overflow", lambda value: value["classic_draft_content_layouts"][0].update(
                document_overflow_px=1), "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("form overflow is missing", lambda value: value["classic_draft_content_layouts"][0].pop(
                "form_overflow_px"), "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("summary outside viewport", lambda value: value["classic_draft_content_layouts"][0][
                "details_summary_bounds_px"][0].update(inside_viewport=False),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("facts collapsed from three to two", lambda value: value["classic_draft_content_layouts"][0].update(
                fact_region_count=2), "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("local scroll region is not focusable", lambda value: value[
                "classic_draft_content_layouts"][0]["region_rows"][0].update(tabindex=-1),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("visible region loses accessible name", lambda value: value[
                "classic_draft_content_layouts"][0]["region_rows"][0].update(has_accessible_name=False),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("touch target too small", lambda value: value[
                "classic_draft_content_layouts"][0]["region_rows"][0].update(min_height_px=43),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("table exceeds bounded width", lambda value: value[
                "classic_draft_content_layouts"][0]["region_rows"][0].update(table_width_px=353),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("values do not wrap", lambda value: value[
                "classic_draft_content_layouts"][0]["region_rows"][0].update(value_wraps=False),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("full facts snapshot lost", lambda value: value["classic_draft_content_layouts"][0].update(
                full_snapshot_retains_synthetic_fact=False),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("keyboard focus was not reached", lambda value: value["classic_draft_content_layouts"][0].update(
                keyboard_focus_reached=False), "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("focus outline is too thin", lambda value: value["classic_draft_content_layouts"][0].update(
                focus_outline_px=1), "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("focus did not scroll local region", lambda value: value[
                "classic_draft_content_layouts"][0].update(keyboard_scroll_delta_px=0),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("classic update form disappeared", lambda value: value[
                "classic_draft_content_layouts"][0].update(classic_update_form_preserved=False),
                "journey_classic_facts_layout_receipts_missing_duplicate_or_invalid")
            changed("extra direct GET inflated page matrix", lambda value: value["pages"].append({
                "name": "classic_content_navigation", "path": "/fixture/classic", "status": 200,
            }), "journey_original_page_matrix_changed")
            changed("original repeat visit is missing", lambda value: value["pages"].pop(),
                    "journey_original_page_matrix_changed")
            changed("original visit order changed", lambda value: value["pages"].__setitem__(
                slice(13, 15), [value["pages"][14], value["pages"][13]]),
                "journey_original_page_matrix_changed")
            changed("repeat visit duplicated over another route", lambda value: value["pages"].__setitem__(
                14, copy.deepcopy(value["pages"][15])),
                "journey_original_page_matrix_changed")
            changed("repeat visit status not successful", lambda value: value["pages"][14].update(
                status=500), "journey_original_page_matrix_changed")
            changed("new cases appended to old layout matrix", lambda value: (
                value["layouts"].extend(copy.deepcopy(value["classic_draft_content_layouts"])),
                value["geometry"].extend(copy.deepcopy(value["classic_draft_content_layouts"])),
            ), "journey_original_layout_geometry_matrix_changed")
            changed("layout and geometry diverged", lambda value: value["geometry"].pop(),
                    "journey_original_layout_geometry_matrix_changed")

            for label, invalid, issue in invalid_reports:
                with self.subTest(case=label):
                    rejected = summarize(invalid)
                    self.assertFalse(rejected["valid"], (label, rejected))
                    self.assertIn(issue, rejected["classic_draft_protocol_issues"])

    def test_journey_stage_execution_enables_classic_receipt_gate(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output = root / "out"
            report_path = output / "journey_browser" / "browser-report.json"
            stage = Stage(
                name="journey_browser", kind="browser",
                command=(sys.executable, "-c", "pass"), timeout_seconds=5,
                report_path=report_path, expected_source="worktree",
            )
            with patch(
                "scripts.check_ux01.summarize_browser_report",
                return_value={"valid": True, "source": "worktree", "reason": None},
            ) as summarize:
                result = execute_stage(stage, root, output, "/usr/bin/chromium")
            self.assertEqual(result["status"], "passed")
            self.assertTrue(summarize.call_args.kwargs["require_classic_draft_facts"])

    def test_common_content_stage_requires_review_depth_and_exact_synthetic_write_counts(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "common-content.json"
            report = _common_content_browser_report()
            path.write_text(json.dumps(report), encoding="utf-8")
            accepted = summarize_browser_report(
                path, "worktree", allow_synthetic_common_content=True,
                minimum_layout_count=28, minimum_interaction_count=8,
                required_interaction_fields=("checks",),
            )
            self.assertTrue(accepted["valid"])
            self.assertEqual(accepted["layout_count"], 28)
            self.assertEqual(accepted["synthetic_common_content_writes"], 11)

            unexpected_write = copy.deepcopy(report)
            unexpected_write["writes"].append({
                "method": "POST", "path": "/api/unexpected", "kind": "synthetic_apply",
            })
            path.write_text(json.dumps(unexpected_write), encoding="utf-8")
            rejected_write = summarize_browser_report(
                path, "worktree", allow_synthetic_common_content=True,
                minimum_layout_count=28, minimum_interaction_count=8,
                required_interaction_fields=("checks",),
            )
            self.assertFalse(rejected_write["valid"])
            self.assertGreater(rejected_write["error_count"], 0)

            invalid_reports = []

            missing_touch_check = copy.deepcopy(report)
            missing_touch_check["checks"] = [
                check for check in missing_touch_check["checks"]
                if not (isinstance(check, dict)
                        and check.get("name") == "common_mobile_touch_targets_44px")
            ]
            invalid_reports.append((
                "missing mobile target check", missing_touch_check,
                "common_named_checks_missing_or_duplicate",
            ))

            failed_touch_check = copy.deepcopy(report)
            # The prior photo-arrow rule was 2rem square (32px at the
            # browser's 16px root size), so the same 44x44 threshold used by
            # the browser fixture must reject this measured geometry.
            legacy_arrow_rect = {"width": 32, "height": 32}
            legacy_arrow_meets_minimum = (
                legacy_arrow_rect["width"] >= 44 and legacy_arrow_rect["height"] >= 44
            )
            self.assertFalse(legacy_arrow_meets_minimum)
            failed_touch_check["checks"] = [
                check for check in failed_touch_check["checks"]
                if not (isinstance(check, dict)
                        and check.get("name") == "common_mobile_touch_targets_44px")
            ]
            failed_touch_check["checks"].append({
                "name": "common_mobile_touch_targets_44px",
                "status": "passed" if legacy_arrow_meets_minimum else "failed",
                "ok": legacy_arrow_meets_minimum,
                "passed": legacy_arrow_meets_minimum,
                "legacy_geometry": legacy_arrow_rect,
            })
            invalid_reports.append((
                "undersized mobile target check fails", failed_touch_check,
                "common_named_checks_missing_or_duplicate",
            ))

            missing_navigator_check = copy.deepcopy(report)
            missing_navigator_check["checks"] = [
                check for check in missing_navigator_check["checks"]
                if not (isinstance(check, dict)
                        and check.get("name") == COMMON_CONTENT_NAVIGATOR_CHECK)
            ]
            invalid_reports.append((
                "missing bounded navigator check", missing_navigator_check,
                "common_named_checks_missing_or_duplicate",
            ))

            failed_navigator_geometry = copy.deepcopy(report)
            failed_navigator_geometry["mobile_product_navigator_observations"][0]["selection_height_px"] = 221
            invalid_reports.append((
                "overheight navigator evidence fails", failed_navigator_geometry,
                "common_mobile_product_navigator_incomplete_or_unsafe",
            ))

            zeroheight_navigator = copy.deepcopy(report)
            zeroheight_navigator["mobile_product_navigator_observations"][0]["selection_height_px"] = 0
            invalid_reports.append((
                "zero-height navigator geometry fails", zeroheight_navigator,
                "common_mobile_product_navigator_incomplete_or_unsafe",
            ))

            clipped_navigator_focus_ring = copy.deepcopy(report)
            clipped_navigator_focus_ring["mobile_product_navigator_observations"][0][
                "second_focus_ring_geometry"]["focus_ring"]["right"] = 28
            invalid_reports.append((
                "focus ring outside local scrollport fails", clipped_navigator_focus_ring,
                "common_mobile_product_navigator_incomplete_or_unsafe",
            ))

            missing_navigator_layout = copy.deepcopy(report)
            missing_navigator_layout["mobile_product_navigator_observations"].pop()
            invalid_reports.append((
                "missing navigator viewport-theme row", missing_navigator_layout,
                "common_mobile_product_navigator_incomplete_or_unsafe",
            ))

            missing_check = copy.deepcopy(report)
            missing_check["checks"].remove("common_photo_boundary_focus_first")
            invalid_reports.append(("missing named check", missing_check, "common_named_checks_missing_or_duplicate"))

            missing_combo = copy.deepcopy(report)
            missing_combo["layouts"].pop()
            invalid_reports.append(("missing layout combination", missing_combo, "common_layout_matrix_incomplete_or_duplicate"))

            duplicate_combo = copy.deepcopy(report)
            duplicate_combo["layouts"][-1] = copy.deepcopy(duplicate_combo["layouts"][0])
            invalid_reports.append(("duplicate layout combination", duplicate_combo, "common_layout_matrix_incomplete_or_duplicate"))

            unmeasured_layout = copy.deepcopy(report)
            unmeasured_layout["layouts"][0].pop("page_overflow")
            invalid_reports.append(("layout without overflow measurement", unmeasured_layout, "common_layout_matrix_incomplete_or_duplicate"))

            failed_named_check = copy.deepcopy(report)
            failed_named_check["checks"].remove("common_photo_boundary_focus_first")
            failed_named_check["checks"].append({"name": "common_photo_boundary_focus_first", "status": "failed"})
            invalid_reports.append(("failed named check object", failed_named_check, "common_named_checks_missing_or_duplicate"))

            contradictory_check = copy.deepcopy(report)
            contradictory_check["checks"].remove("common_photo_boundary_focus_first")
            contradictory_check["checks"].append({
                "name": "common_photo_boundary_focus_first", "status": "failed", "ok": True,
            })
            invalid_reports.append(("contradictory failed check object", contradictory_check, "common_named_checks_missing_or_duplicate"))

            duplicate_check = copy.deepcopy(report)
            duplicate_check["checks"].append("common_51_selection_and_csrf_preview_rejected_without_mutation_or_publication")
            invalid_reports.append((
                "duplicate named bulk check", duplicate_check,
                "common_named_checks_missing_or_duplicate",
            ))

            untested_check = copy.deepcopy(report)
            untested_check["checks"].remove("common_photo_boundary_focus_first")
            untested_check["checks"].append({
                "name": "common_photo_boundary_focus_first", "status": "not-tested", "ok": True,
            })
            invalid_reports.append(("untested check cannot be promoted by ok", untested_check, "common_named_checks_missing_or_duplicate"))

            no_focus = copy.deepcopy(report)
            no_focus.pop("focus_observations")
            invalid_reports.append(("missing focus telemetry", no_focus, "common_focus_observation_missing_or_duplicate"))

            unsupported_focus = copy.deepcopy(report)
            unsupported_focus["focus_observations"][0]["target_supported"] = False
            invalid_reports.append(("unsupported focus target", unsupported_focus, "common_focus_observation_unconfirmed"))

            wrong_boundary_direction = copy.deepcopy(report)
            first_focus = next(row for row in wrong_boundary_direction["focus_observations"]
                              if row["check"] == "common_photo_boundary_focus_first")
            first_focus["target"]["direction"] = "-1"
            invalid_reports.append(("wrong focus direction for first boundary", wrong_boundary_direction, "common_focus_observation_unconfirmed"))

            invisible_focus = copy.deepcopy(report)
            invisible_focus["focus_observations"][0]["focus_visible"] = False
            invalid_reports.append(("focus without visible ring", invisible_focus, "common_focus_observation_unconfirmed"))

            outside_focus = copy.deepcopy(report)
            outside_focus["focus_observations"][0]["rect"]["x"] = 2000
            invalid_reports.append(("focus outside viewport", outside_focus, "common_focus_observation_unconfirmed"))

            missing_bulk_apply_fingerprint = copy.deepcopy(report)
            missing_bulk_apply_fingerprint["apply_result_observations"][-1][
                "product_id_fingerprint"
            ] = "0" * 64
            invalid_reports.append((
                "apply IDs differ from selected 50", missing_bulk_apply_fingerprint,
                "common_bulk_50_apply_fingerprint_incomplete",
            ))

            missing_stale_receipt = copy.deepcopy(report)
            missing_stale_receipt["bulk_50"]["stale_denial_new_audits"] = 1
            invalid_reports.append((
                "stale denial produced an audit", missing_stale_receipt,
                "common_bulk_50_persisted_counts_unexpected",
            ))

            false_version_counts = copy.deepcopy(report)
            false_version_counts["bulk_50"]["final_content_edit_version_counts"] = {"2": 50}
            invalid_reports.append((
                "wrong final version counts", false_version_counts,
                "common_bulk_50_version_counts_unexpected",
            ))

            missing_413_receipt = copy.deepcopy(report)
            missing_413_receipt["expected_http_rejections"].pop()
            invalid_reports.append((
                "missing exact 413 route receipt", missing_413_receipt,
                "common_bulk_50_http_rejections_missing_or_unscoped",
            ))

            wrong_413_console_endpoint = copy.deepcopy(report)
            wrong_413_console_endpoint["expected_rejection_console_errors"][0][
                "location"]["path"
            ] = "/other/local/path"
            invalid_reports.append((
                "console 413 is from a different local endpoint", wrong_413_console_endpoint,
                "common_bulk_50_413_console_rejections_missing_or_unscoped",
            ))

            for label, invalid, expected_issue in invalid_reports:
                with self.subTest(case=label):
                    path.write_text(json.dumps(invalid), encoding="utf-8")
                    rejected = summarize_browser_report(
                        path, "worktree", allow_synthetic_common_content=True,
                        minimum_layout_count=28, minimum_interaction_count=8,
                        required_interaction_fields=("checks",),
                    )
                    self.assertFalse(rejected["valid"])
                    self.assertTrue(any(
                        issue == expected_issue or issue.startswith(expected_issue + ":")
                        for issue in rejected["common_content_protocol_issues"]
                    ), rejected["common_content_protocol_issues"])

            shallow = copy.deepcopy(report)
            shallow["checks"] = ["too-few"]
            path.write_text(json.dumps(shallow), encoding="utf-8")
            rejected_depth = summarize_browser_report(
                path, "worktree", allow_synthetic_common_content=True,
                minimum_layout_count=28, minimum_interaction_count=8,
                required_interaction_fields=("checks",),
            )
            self.assertFalse(rejected_depth["valid"])
            self.assertIn("interaction_rows_below_8", rejected_depth["missing_evidence"])

            missing_write = copy.deepcopy(report)
            missing_write["writes"] = []
            path.write_text(json.dumps(missing_write), encoding="utf-8")
            rejected_missing_write = summarize_browser_report(
                path, "worktree", allow_synthetic_common_content=True,
                minimum_layout_count=28, minimum_interaction_count=8,
                required_interaction_fields=("checks",),
            )
            self.assertFalse(rejected_missing_write["valid"])

            wrong_counters = copy.deepcopy(report)
            wrong_counters["synthetic_actions"]["empty_route_api_reads"] = 1
            path.write_text(json.dumps(wrong_counters), encoding="utf-8")
            rejected_empty_api = summarize_browser_report(
                path, "worktree", allow_synthetic_common_content=True,
                minimum_layout_count=28, minimum_interaction_count=8,
                required_interaction_fields=("checks",),
            )
            self.assertFalse(rejected_empty_api["valid"])
            self.assertIn("common_synthetic_action_counts_unexpected", rejected_empty_api["common_content_protocol_issues"])

    def test_wb_edit_stage_requires_all_theme_viewports_and_named_checks(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "wb-edit.json"
            minimums = BROWSER_MINIMUMS["wb_edit_browser"]
            self.assertEqual(minimums, {"layouts": 30, "interactions": 24})
            report = _wb_edit_browser_report()
            self.assertEqual(len(report["layouts"]), 30)
            self.assertEqual(len(report["checks"]), 24)
            path.write_text(json.dumps(report), encoding="utf-8")
            accepted = summarize_browser_report(
                path,
                "worktree",
                require_synthetic_wb_edit=True,
                minimum_layout_count=minimums["layouts"],
                minimum_interaction_count=minimums["interactions"],
                required_interaction_fields=("checks",),
            )
            self.assertTrue(accepted["valid"])
            self.assertEqual(accepted["layout_count"], 30)
            self.assertEqual(accepted["interaction_count"], 24)

            invalid_reports = []
            missing_save = copy.deepcopy(report)
            missing_save["checks"] = [
                row for row in missing_save["checks"]
                if row.get("name") != "single_edit_real_form_submit_reaches_fake_wb_and_persists_exact_history"
            ]
            invalid_reports.append(("missing saved single-edit check", missing_save,
                                    "wb_edit_named_check_missing_or_failed:"))

            failed_permission_check = copy.deepcopy(report)
            next(row for row in failed_permission_check["checks"]
                 if row["name"] == "single_edit_foreign_owner_session_cannot_post_seller_product")[
                     "status"
                 ] = "failed"
            invalid_reports.append(("failed seller-boundary check", failed_permission_check,
                                    "wb_edit_named_check_missing_or_failed:"))

            wrong_characteristic_payload = copy.deepcopy(report)
            wrong_characteristic_payload["fake_wb_single_write_requests"][0][
                "characteristics"][0]["value"] = "Китай"
            invalid_reports.append(("provider request is not exact", wrong_characteristic_payload,
                                    "wb_single_edit_fake_request_unexpected"))

            extra_characteristic_write = copy.deepcopy(report)
            extra_characteristic_write["fake_wb_single_write_requests"][0][
                "characteristic_ids"].append(303)
            extra_characteristic_write["fake_wb_single_write_requests"][0][
                "characteristics"].append({"id": 303, "value": 125})
            invalid_reports.append(("save silently includes unchanged weight", extra_characteristic_write,
                                    "wb_single_edit_fake_request_unexpected"))

            wrong_saved_channel = copy.deepcopy(report)
            wrong_saved_channel["single_edit_observations"]["form_post"][
                "target_channel"] = "Ozon"
            invalid_reports.append(("saved form reports wrong provider channel", wrong_saved_channel,
                                    "wb_single_edit_form_and_history_evidence_incomplete"))

            wrong_save_check_payload = copy.deepcopy(report)
            next(row for row in wrong_save_check_payload["checks"]
                 if row["name"] == "single_edit_real_form_submit_reaches_fake_wb_and_persists_exact_history")[
                     "changed_characteristics"
                 ] = [202, 303]
            invalid_reports.append(("save receipt claims extra changed field", wrong_save_check_payload,
                                    "wb_single_edit_save_receipt_details_incomplete"))

            request_submit_not_canceled = copy.deepcopy(report)
            request_submit_not_canceled["single_edit_observations"]["progressive_ui"][
                "empty_add_request_submit_attempt"]["submit_event"]["default_prevented"] = False
            invalid_reports.append(("empty requestSubmit is not canceled", request_submit_not_canceled,
                                    "wb_single_edit_progressive_empty_submit_guard_incomplete"))

            request_submit_causes_provider = copy.deepcopy(report)
            request_submit_causes_provider["single_edit_observations"]["progressive_ui"][
                "empty_add_request_submit_attempt"]["provider_attempts"] = 1
            invalid_reports.append(("empty requestSubmit reaches provider", request_submit_causes_provider,
                                    "wb_single_edit_progressive_empty_submit_guard_incomplete"))

            empty_field_not_preserved = copy.deepcopy(report)
            empty_field_not_preserved["single_edit_observations"]["reopen"][
                "present_empty_field_preserved"] = False
            invalid_reports.append(("present empty characteristic lost on readback", empty_field_not_preserved,
                                    "wb_single_edit_reopen_evidence_incomplete"))

            stale_history_field_lost = copy.deepcopy(report)
            stale_history_field_lost["single_edit_observations"]["form_post"][
                "readback_and_history"]["characteristic_ids"].remove(501)
            invalid_reports.append(("historical characteristic lost from full-card readback", stale_history_field_lost,
                                    "wb_single_edit_form_and_history_evidence_incomplete"))

            stale_field_claimed_as_current_schema = copy.deepcopy(report)
            stale_field_claimed_as_current_schema["single_edit_observations"]["progressive_ui"][
                "initial_view"]["visible_field_ids"].insert(4, 501)
            invalid_reports.append(("stale historical field fabricated as current schema", stale_field_claimed_as_current_schema,
                                    "wb_single_edit_progressive_empty_submit_guard_incomplete"))

            stale_field_not_disclosed_by_keyboard = copy.deepcopy(report)
            stale_field_not_disclosed_by_keyboard["single_edit_observations"]["progressive_ui"][
                "stale_legacy_field_read_only"]["disclosure_opened_by_keyboard"] = False
            invalid_reports.append(("stale value disclosure was not keyboard opened", stale_field_not_disclosed_by_keyboard,
                                    "wb_single_edit_progressive_empty_submit_guard_incomplete"))

            stale_field_has_edit_control = copy.deepcopy(report)
            stale_field_has_edit_control["single_edit_observations"]["progressive_ui"][
                "stale_legacy_field_read_only"]["form_control_count"] = 1
            invalid_reports.append(("stale value is editable", stale_field_has_edit_control,
                                    "wb_single_edit_progressive_empty_submit_guard_incomplete"))

            bad_weight_preservation = copy.deepcopy(report)
            bad_weight_preservation["single_edit_observations"]["rejections"][0][
                "local_product_preserved"
            ] = False
            invalid_reports.append(("invalid weight rejection loses local value", bad_weight_preservation,
                                    "wb_single_edit_validation_boundaries_incomplete"))

            missing_single_history = copy.deepcopy(report)
            missing_single_history["single_edit_observations"]["form_post"][
                "readback_and_history"]["direct_history_count"
            ] = 0
            invalid_reports.append(("missing direct history", missing_single_history,
                                    "wb_single_edit_form_and_history_evidence_incomplete"))

            incorrect_mixed_counts = copy.deepcopy(report)
            incorrect_mixed_counts["mixed_fixture_observations"]["skipped"] = 47
            invalid_reports.append(("wrong mixed skipped count", incorrect_mixed_counts,
                                    "wb_mixed_selection_and_history_counts_unexpected"))

            wrong_mixed_provider_ids = copy.deepcopy(report)
            wrong_mixed_provider_ids["mixed_fixture_observations"][
                "fake_provider_product_ids"
            ] = [910000, 910002]
            invalid_reports.append(("mixed apply writes another product", wrong_mixed_provider_ids,
                                    "wb_mixed_provider_or_history_identity_unexpected"))

            extra_fake_write = copy.deepcopy(report)
            extra_fake_write["fake_wb_write_calls"] += 1
            invalid_reports.append(("unexpected extra fake write", extra_fake_write,
                                    "wb_fake_provider_bulk_write_totals_unexpected"))

            incomplete_layout = copy.deepcopy(report)
            incomplete_layout["layouts"].pop()
            invalid_reports.append(("missing theme viewport layout", incomplete_layout,
                                    "wb_edit_layout_matrix_incomplete_or_unmeasured"))

            unmeasured_layout = copy.deepcopy(report)
            unmeasured_layout["layouts"][0]["layout_settle"]["fonts_ready"] = False
            invalid_reports.append(("unstable measured layout", unmeasured_layout,
                                    "wb_edit_layout_matrix_incomplete_or_unmeasured"))

            missing_contrast_theme = copy.deepcopy(report)
            missing_contrast_theme["single_edit_contrast_diagnostic"].pop()
            invalid_reports.append(("missing dark contrast observation", missing_contrast_theme,
                                    "wb_edit_enabled_control_contrast_aa_incomplete_or_invalid"))

            dark_control_below_aa = copy.deepcopy(report)
            dark_control_below_aa["single_edit_contrast_diagnostic"][1]["controls"][0][
                "contrast_ratio_estimate"
            ] = 4.49
            invalid_reports.append(("dark control below AA threshold", dark_control_below_aa,
                                    "wb_edit_enabled_control_contrast_aa_incomplete_or_invalid"))

            unsettled_contrast_theme = copy.deepcopy(report)
            unsettled_contrast_theme["single_edit_contrast_diagnostic"][1][
                "appearance_stability"]["settled"] = False
            invalid_reports.append(("dark contrast sampled before settling", unsettled_contrast_theme,
                                    "wb_edit_enabled_control_contrast_aa_incomplete_or_invalid"))

            duplicate_contrast_control = copy.deepcopy(report)
            duplicate_contrast_control["single_edit_contrast_diagnostic"][0]["controls"][4][
                "name"
            ] = "optional_add"
            invalid_reports.append(("duplicate control replaces save", duplicate_contrast_control,
                                    "wb_edit_enabled_control_contrast_aa_incomplete_or_invalid"))

            contrast_ratio_not_backed_by_rgb = copy.deepcopy(report)
            contrast_ratio_not_backed_by_rgb["single_edit_contrast_diagnostic"][0]["controls"][0][
                "effective_foreground_rgb"
            ] = [254.9, 254.9, 254.9]
            invalid_reports.append(("reported contrast differs from measured RGB", contrast_ratio_not_backed_by_rgb,
                                    "wb_edit_enabled_control_contrast_aa_incomplete_or_invalid"))

            missing_final_style = copy.deepcopy(report)
            missing_final_style["single_edit_contrast_diagnostic"][1]["appearance_stability"][
                "final_computed_styles"
            ].pop()
            invalid_reports.append(("missing settled computed style", missing_final_style,
                                    "wb_edit_enabled_control_contrast_aa_incomplete_or_invalid"))

            duplicate_final_style = copy.deepcopy(report)
            duplicate_styles = duplicate_final_style["single_edit_contrast_diagnostic"][0][
                "appearance_stability"]["final_computed_styles"]
            duplicate_styles[-1] = copy.deepcopy(duplicate_styles[0])
            invalid_reports.append(("duplicate settled computed style selector", duplicate_final_style,
                                    "wb_edit_enabled_control_contrast_aa_incomplete_or_invalid"))

            for label, invalid, expected_issue in invalid_reports:
                with self.subTest(case=label):
                    path.write_text(json.dumps(invalid), encoding="utf-8")
                    rejected = summarize_browser_report(
                        path,
                        "worktree",
                        require_synthetic_wb_edit=True,
                        minimum_layout_count=minimums["layouts"],
                        minimum_interaction_count=minimums["interactions"],
                        required_interaction_fields=("checks",),
                    )
                    self.assertFalse(rejected["valid"])
                    self.assertTrue(any(
                        issue == expected_issue or issue.startswith(expected_issue)
                        for issue in rejected["wb_edit_protocol_issues"]
                    ), rejected["wb_edit_protocol_issues"])

    def test_wb_browser_bridge_allows_only_the_seeded_unmapped_edit_post(self):
        root = Path(__file__).resolve().parents[1]
        code = r'''
import importlib.util
from pathlib import Path
from types import SimpleNamespace

source = Path("tests/ux01/wb_edit_browser.py").resolve()
spec = importlib.util.spec_from_file_location("wb_edit_browser_fixture", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
origin = "http://sellerhub.synthetic"
allowed_paths = module.local_post_allowlist({
    "product_id": 10001,
    "unmapped_product_id": 10002,
    "foreign_product_id": 10003,
})

class FakeRoute:
    def __init__(self, path):
        self.request = SimpleNamespace(url=origin + path, method="POST")
        self.continued = False
        self.aborted = False
    def continue_(self): self.continued = True
    def abort(self): self.aborted = True
    def fulfill(self, **_kwargs): raise AssertionError("unexpected asset fulfillment")

allowed = FakeRoute("/products/10002/edit")
module.bridge(allowed, assets={}, origin=origin, allowed_local_posts=allowed_paths)
assert allowed.continued and not allowed.aborted
assert not module.REPORT["unexpected_external_requests"]

denied = FakeRoute("/products/9741/edit")
module.bridge(denied, assets={}, origin=origin, allowed_local_posts=allowed_paths)
assert denied.aborted and not denied.continued
assert module.REPORT["unexpected_external_requests"] == [{
    "method": "POST", "path": "/products/9741/edit",
    "reason": "unapproved_local_mutation",
}]
print("bridge exact fixture POST allow/deny passed")
'''
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bridge exact fixture POST allow/deny passed", result.stdout)

    def test_listing_report_allows_only_bounded_synthetic_login_posts(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "browser.json"
            base = {
                "status": "passed", "variant": "after", "provider_attempts": 0,
                "external": [], "js_errors": [],
                "geometry": [{}], "checks": ["synthetic-layout-action"],
            }
            path.write_text(json.dumps({
                **base,
                "writes": [{"method": "POST", "path": "/login"}],
            }), encoding="utf-8")
            allowed = summarize_browser_report(path, "after", allow_synthetic_login=True)
            self.assertTrue(allowed["valid"])
            self.assertEqual(allowed["synthetic_auth_writes"], 1)

            for writes in (
                [{"method": "POST", "path": "/api/write"}],
                [{"method": "GET", "path": "/login"}],
                [{"method": "POST", "path": "/login"}] * 3,
            ):
                with self.subTest(writes=writes):
                    path.write_text(json.dumps({**base, "writes": writes}), encoding="utf-8")
                    rejected = summarize_browser_report(path, "after", allow_synthetic_login=True)
                    self.assertFalse(rejected["valid"])
                    self.assertGreater(rejected["error_count"], 0)

    def test_child_environment_uses_valid_synthetic_key_and_isolates_temp_state(self):
        with tempfile.TemporaryDirectory() as temp_name:
            environment = _clean_environment(
                Path(__file__).resolve().parents[1],
                Path(temp_name),
                "/usr/bin/chromium",
                (),
            )
            key = base64.urlsafe_b64decode(environment["ENCRYPTION_KEY"])
            self.assertEqual(len(key), 32)
            self.assertEqual(environment["HOME"], os.environ.get("HOME", "/tmp"))
            self.assertTrue(environment["TMPDIR"].startswith(temp_name))
            self.assertTrue(environment["XDG_CONFIG_HOME"].startswith(temp_name))
            self.assertNotIn("AITUNNEL_API_KEY", environment)
            self.assertEqual(environment["SKIP_SCHEDULER"], "1")

    def test_stage_plan_is_sequential_and_includes_all_required_browser_sources(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "tests").mkdir()
            for relative in (
                "tests/test_ux01_runner.py", "tests/test_ux01_navigation.py",
                *REQUIRED_TESTS,
                *REQUIRED_BROWSER_FILES_FOR_TEST,
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# synthetic", encoding="utf-8")
            stages = build_stages(root, root.parent / "out", "/usr/bin/chromium")
            self.assertEqual([stage.name for stage in stages], [
                "ux01_pytest", "analytics_browser", "listing_browser",
                "workspace_browser", "journey_browser", "operations_pricing_browser",
                "wb_edit_browser", "common_content_browser",
            ])
            self.assertEqual([stage.timeout_seconds for stage in stages], [600] * 8)
            pytest_command = stages[0].command
            self.assertIn("tests/test_ux01_runner.py", pytest_command)
            for path in REQUIRED_TESTS:
                self.assertIn(path, pytest_command)
            self.assertEqual(stages[1].expected_source, "worktree")
            self.assertEqual(stages[2].expected_source, "after")
            self.assertEqual(stages[3].expected_source, "worktree")
            listing = stages[2]
            expected_listing_report = (
                root.parent / "out" / "listing_browser" / "artifacts"
                / "listing-browser-after.json"
            )
            self.assertEqual(listing.report_path, expected_listing_report)
            self.assertEqual(
                dict(listing.environment)["UX01_LISTING_ARTIFACTS"],
                str(expected_listing_report.parent),
            )
            for index, name, prefix in (
                (6, "wb_edit_browser", "UX01_WB_EDIT"),
                (7, "common_content_browser", "UX01_COMMON_CONTENT"),
            ):
                stage = stages[index]
                stage_dir = root.parent / "out" / name
                self.assertEqual(stage.expected_source, "worktree")
                self.assertEqual(stage.report_path, stage_dir / "browser-report.json")
                self.assertEqual(dict(stage.environment)[prefix + "_SOURCE"], "worktree")
                self.assertEqual(dict(stage.environment)[prefix + "_ARTIFACTS"],
                                 str(stage_dir / "artifacts"))
                self.assertEqual(dict(stage.environment)[prefix + "_REPORT"],
                                 str(stage.report_path))


REQUIRED_BROWSER_FILES_FOR_TEST = (
    "tests/ux01/analytics_browser.py",
    "tests/ux01/listing_browser.py",
    "tests/ux01/workspace_browser.py",
    "tests/ux01/journey_browser.py",
    "tests/ux01/operations_pricing_browser.py",
    "tests/ux01/wb_edit_browser.py",
    "tests/ux01/common_content_browser.py",
)


def test_product_selection_dom_is_required_by_both_release_gates():
    assert "tests/test_product_selection_dom.py" in REQUIRED_TESTS
    assert "test_product_selection_dom.py" in OZON_EXTRA_TESTS


if __name__ == "__main__":
    unittest.main()
