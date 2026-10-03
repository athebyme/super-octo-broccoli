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

from scripts.check_ux01 import (
    BROWSER_INTERACTION_FIELDS,
    BROWSER_MINIMUMS,
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
    WB_EDIT_LAYOUT_THEMES,
    WB_EDIT_LAYOUT_WIDTHS,
    WB_EDIT_PAGES,
    WB_EDIT_REQUIRED_CHECKS,
    REQUIRED_TESTS,
    _canonical_json,
    _clean_environment,
    _safe_repo_file,
    build_stages,
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
        else:
            required_checks.append({"name": name, "status": "passed"})
    generic_checks = [
        {"name": f"fixture_interaction_{index}", "status": "passed"}
        for index in range(24 - len(required_checks))
    ]
    boundary_names = (
        "wrong_weight_unit", "non_numeric_weight_type", "unlisted_dictionary_value",
    )
    write_request = {
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
    return {
        "status": "complete",
        "source": "worktree",
        "provider_attempts": 0,
        "unexpected_external_requests": [],
        "unexpected_http": [],
        "javascript_errors": [],
        "browser_mutations": [],
        "layouts": layouts,
        "checks": required_checks + generic_checks,
        "fake_wb_single_write_calls": 1,
        "fake_wb_single_write_requests": [write_request],
        "fake_wb_write_calls": 2,
        "fake_wb_written_products": [*range(900000, 900050), 910000, 910001],
        "fake_wb_client_instances": 3,
        "single_edit_observations": {
            "form_post": {
                "http_status": 302,
                "path": "/products/9876/edit",
                "normal_html_form": True,
                "csrf_field_present": True,
                "fake_write_count": 1,
                "readback_and_history": {
                    "characteristic_ids": [101, 202, 303, 404],
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
                "sku_read_only": True,
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
                "characteristics"][1]["value"] = "125"
            invalid_reports.append(("provider request is not exact", wrong_characteristic_payload,
                                    "wb_single_edit_fake_request_unexpected"))

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
