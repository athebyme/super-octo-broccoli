from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import base64
import os

from scripts.check_ux01 import (
    BROWSER_INTERACTION_FIELDS,
    _canonical_json,
    _clean_environment,
    _safe_repo_file,
    build_stages,
    load_manifest,
    parse_junit,
    summarize_browser_report,
    verify_snapshot,
)


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

            path.write_text(json.dumps({"status": "passed", "source": "worktree"}), encoding="utf-8")
            missing_safety = summarize_browser_report(path, "worktree")
            self.assertFalse(missing_safety["valid"])
            self.assertEqual(missing_safety["reason"], "browser_report_safety_telemetry_missing")

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
            writes = [
                {"method": "POST", "path": "/api/my-products/common-content/preview", "kind": "synthetic_preview"},
                {"method": "POST", "path": "/api/my-products/common-content/apply", "kind": "synthetic_apply"},
            ]
            report = {
                "status": "passed", "source": "worktree", "provider_attempts": 0,
                "unexpected_external_requests": [], "unexpected_http_requests": [],
                "javascript_errors": [], "console_errors": [],
                "layouts": [{"width": index} for index in range(4)],
                "checks": ["check-" + str(index) for index in range(8)],
                "writes": writes,
                "synthetic_actions": {"preview_requests": 1, "apply_requests": 1, "provider_attempts": 0},
            }
            path.write_text(json.dumps(report), encoding="utf-8")
            accepted = summarize_browser_report(
                path, "worktree", allow_synthetic_common_content=True,
                minimum_layout_count=4, minimum_interaction_count=8,
                required_interaction_fields=("checks",),
            )
            self.assertTrue(accepted["valid"])
            self.assertEqual(accepted["synthetic_common_content_writes"], 2)

            report["writes"].append({"method": "POST", "path": "/api/unexpected", "kind": "synthetic_apply"})
            path.write_text(json.dumps(report), encoding="utf-8")
            rejected_write = summarize_browser_report(
                path, "worktree", allow_synthetic_common_content=True,
                minimum_layout_count=4, minimum_interaction_count=8,
                required_interaction_fields=("checks",),
            )
            self.assertFalse(rejected_write["valid"])

            report["writes"] = writes
            report["checks"] = ["too-few"]
            path.write_text(json.dumps(report), encoding="utf-8")
            rejected_depth = summarize_browser_report(
                path, "worktree", allow_synthetic_common_content=True,
                minimum_layout_count=4, minimum_interaction_count=8,
                required_interaction_fields=("checks",),
            )
            self.assertFalse(rejected_depth["valid"])
            self.assertIn("interaction_rows_below_8", rejected_depth["missing_evidence"])

            report["checks"] = ["check-" + str(index) for index in range(8)]
            report["writes"] = []
            path.write_text(json.dumps(report), encoding="utf-8")
            rejected_missing_write = summarize_browser_report(
                path, "worktree", allow_synthetic_common_content=True,
                minimum_layout_count=4, minimum_interaction_count=8,
                required_interaction_fields=("checks",),
            )
            self.assertFalse(rejected_missing_write["valid"])

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
                *REQUIRED_TESTS_FOR_TEST,
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
            for path in REQUIRED_TESTS_FOR_TEST:
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


REQUIRED_TESTS_FOR_TEST = (
    "tests/test_competitor_routes.py",
    "tests/test_marketplace_readiness.py",
    "tests/test_marketplace_listing_routes.py",
    "tests/test_product_selection.py",
    "tests/test_wb_edit_review_replay.py",
    "tests/test_wb_bulk_review_key_migration.py",
    "tests/test_common_product_content_service.py",
    "tests/test_common_product_content_routes.py",
    "tests/test_common_product_content_ui.py",
)
REQUIRED_BROWSER_FILES_FOR_TEST = (
    "tests/ux01/analytics_browser.py",
    "tests/ux01/listing_browser.py",
    "tests/ux01/workspace_browser.py",
    "tests/ux01/journey_browser.py",
    "tests/ux01/operations_pricing_browser.py",
    "tests/ux01/wb_edit_browser.py",
    "tests/ux01/common_content_browser.py",
)


if __name__ == "__main__":
    unittest.main()
