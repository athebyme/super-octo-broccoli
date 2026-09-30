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
    "http_errors",
)
REQUIRED_TESTS = (
    "tests/test_competitor_routes.py",
    "tests/test_marketplace_readiness.py",
    "tests/test_marketplace_listing_routes.py",
)
REQUIRED_BROWSERS = (
    "tests/ux01/analytics_browser.py",
    "tests/ux01/listing_browser.py",
    "tests/ux01/workspace_browser.py",
    "tests/ux01/journey_browser.py",
    "tests/ux01/operations_pricing_browser.py",
)
BROWSER_INTERACTION_FIELDS = {
    # Analytics has no separate click log; the real page's bounded API reads
    # are the fixture's evidence that its shell behavior ran.
    "analytics_browser": ("api_calls",),
    "listing_browser": ("checks",),
    "workspace_browser": ("interactions",),
    "journey_browser": ("interactions", "checks"),
    "operations_pricing_browser": ("interactions", "checks"),
}


@dataclass(frozen=True)
class Stage:
    name: str
    kind: str
    command: tuple[str, ...]
    timeout_seconds: int
    report_path: Path | None = None
    expected_source: str | None = None
    environment: tuple[tuple[str, str], ...] = ()


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


def summarize_browser_report(path: Path, expected_source: str,
                             allow_synthetic_login: bool = False,
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
    if not isinstance(writes, list):
        error_count += 1
    elif writes:
        allowed = (
            allow_synthetic_login and len(writes) <= 2
            and all(isinstance(row, dict)
                    and row.get("method") == "POST"
                    and row.get("path") == "/login" for row in writes)
        )
        if allowed:
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
        "provider_attempts": provider_attempts,
        "page_count": len(data.get("pages", [])) if isinstance(data.get("pages"), list) else 0,
        "layout_count": layout_count,
        "interaction_count": interaction_count,
        "missing_evidence": missing_evidence,
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
    )
    for name, browser_key, script, expected_source, output_key in browser_specs:
        path = root / script
        stage_dir = output / name
        stage_dir.mkdir(parents=True, exist_ok=True)
        report_path = stage_dir / ("browser-report.json" if browser_key in {"workspace", "journey", "operations_pricing"}
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
