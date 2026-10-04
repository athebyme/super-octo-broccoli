#!/usr/bin/env python3
"""Bounded, synthetic Caddy↔Gunicorn upstream transport acceptance.

Requires an explicit --run. Uses only the pinned Caddy image and the accepted
runtime image, an internal Docker network, a temporary self-signed backend
certificate, and an isolated in-memory synthetic WSGI app. It does not start
seller_platform, mount application data, publish host ports, or use credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
CADDYFILE = ROOT / "Caddyfile"
CADDY_IMAGE = "sha256:818bec5261db8072f732f941ed335d7f027b1657230c0f62479a4398683e911f"
CADDY_VERSION = "v2.11.1"
RUNTIME_IMAGE = (
    "sha256:6aabfed527b6c98bda502e223b571d4eae0b903d72332887091edc99732e39b8"
)
RUNTIME_IMAGE_TAG = "seller-hub-completion-r9-runtime:20261003-g2"
GUNICORN_VERSION = "21.2.0"
GUNICORN_KEEPALIVE_SECONDS = 2
CADDY_IDLE_TIMEOUT_SECONDS = 1
MAX_CAPTURE = 256 * 1024
REPORT: dict[str, Any] = {
    "status": "not_started",
    "scope": "synthetic_caddy_gunicorn_upstream_transport",
    "synthetic_only": True,
    "provider_calls": 0,
    "database_mounts": 0,
    "published_host_ports": 0,
    "production_reload": False,
    "checks": [],
    "caddy_adapter": {},
    "runtime": {},
    "synthetic_probe": {
        "backend_worker_class": "gthread",
        "client_http_requests": 0,
        "client_exit_code": None,
        "client_status": None,
        "listeners_ready": None,
        "observed_response_sequence": [],
        "backend_event_sequence": [],
        "backend_event_history_exact": False,
        "observations": [],
        "backend_physical_request_count": 0,
        "post_request_count": 0,
        "post_after_idle_exact": False,
        "post_responses_exactly_once": False,
        "incomplete_post_not_replayed": False,
    },
    "cleanup": {
        "containers_removed": 0,
        "network_removed": False,
        "synthetic_material_removed": None,
    },
    "failure_code": None,
}


class CheckFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fail(code: str) -> None:
    raise CheckFailure(code)


def add_check(name: str, passed: bool) -> None:
    REPORT["checks"].append({"name": name, "status": "passed" if passed else "failed"})


def walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def duration_is(value: Any, seconds: int) -> bool:
    if isinstance(value, str):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)", value.strip())
        if not match:
            return False
        scale = {
            "ns": 1e-9, "us": 1e-6, "µs": 1e-6,
            "ms": 1e-3, "s": 1, "m": 60, "h": 3600,
        }[match.group(2)]
        return float(match.group(1)) * scale == seconds
    if type(value) is int:
        # Caddy's JSON duration representation is a nanosecond count.
        return value == seconds * 1_000_000_000
    return False


def duration_is_zero(value: Any) -> bool:
    return (
        value is None
        or (type(value) is int and value == 0)
        or (isinstance(value, str) and value == "0s")
    )


def inspect_adapted_config(adapted: Any, caddyfile_text: str) -> dict[str, Any]:
    proxies = [node for node in walk_dicts(adapted) if node.get("handler") == "reverse_proxy"]
    single_proxy = len(proxies) == 1
    proxy = proxies[0] if single_proxy else {}
    transport = proxy.get("transport") if isinstance(proxy, dict) else None
    transport = transport if isinstance(transport, dict) else {}
    tls = transport.get("tls")
    tls = tls if isinstance(tls, dict) else {}
    keepalive = transport.get("keep_alive")
    keepalive = keepalive if isinstance(keepalive, dict) else {}
    upstreams = proxy.get("upstreams") if isinstance(proxy, dict) else None
    upstream_exact = (
        isinstance(upstreams, list) and len(upstreams) == 1
        and isinstance(upstreams[0], dict)
        and upstreams[0].get("dial") == "seller-platform:5001"
    )
    retry = proxy.get("load_balancing") if isinstance(proxy, dict) else None
    retry = retry if isinstance(retry, dict) else {}
    retries = retry.get("retries")
    retries_zero = retries is None or (type(retries) is int and retries == 0)
    duration_zero = duration_is_zero(retry.get("try_duration"))
    no_retry_directive = not re.search(
        r"(?m)^\s*lb_(?:retries|try_duration|try_interval|retry_match)\b",
        caddyfile_text,
    )
    result = {
        "one_reverse_proxy_handler": single_proxy,
        "upstream_exact_seller_platform_5001": bool(upstream_exact),
        "http_transport": transport.get("protocol") == "http",
        "upstream_tls_option_preserved": tls.get("insecure_skip_verify") is True,
        "caddy_idle_timeout_1s": duration_is(keepalive.get("idle_timeout"), CADDY_IDLE_TIMEOUT_SECONDS),
        "explicit_caddy_retry_directive_absent": bool(no_retry_directive),
        "adapted_load_balancer_retries_zero": bool(retries_zero and duration_zero),
    }
    result["valid"] = all(result.values())
    return result


def run_command(
    argv: list[str], *, timeout: float, failure_code: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            check=False, env=env,
        )
    except FileNotFoundError:
        fail("docker_cli_unavailable")
    except subprocess.TimeoutExpired:
        fail(failure_code + "_timeout")
    if completed.returncode != 0:
        fail(failure_code)
    if len(completed.stdout) > MAX_CAPTURE or len(completed.stderr) > MAX_CAPTURE:
        fail(failure_code + "_output_over_limit")
    return completed


def docker_output(args: list[str], *, timeout: float, failure_code: str) -> str:
    return run_command(
        ["docker", *args], timeout=timeout, failure_code=failure_code,
    ).stdout.strip()


def inspect_image_reference(reference: str, failure_code: str) -> str:
    return docker_output(
        ["image", "inspect", "--format", "{{.Id}}", reference],
        timeout=10, failure_code=failure_code,
    )


def verify_local_images() -> None:
    caddy_id = inspect_image_reference(CADDY_IMAGE, "pinned_caddy_image_unavailable")
    if caddy_id != CADDY_IMAGE:
        fail("pinned_caddy_image_identity_mismatch")
    runtime_id = inspect_image_reference(RUNTIME_IMAGE, "accepted_runtime_image_unavailable")
    if runtime_id != RUNTIME_IMAGE:
        fail("accepted_runtime_image_identity_mismatch")
    REPORT["runtime"]["accepted_image_exact"] = True
    REPORT["runtime"]["accepted_image_tag"] = RUNTIME_IMAGE_TAG
    REPORT["caddy_adapter"]["pinned_caddy_image_exact"] = True
    add_check("pinned_caddy_and_accepted_runtime_images_available_exactly", True)


def check_caddy_version() -> None:
    completed = run_command(
        caddy_run_prefix() + ["version"], timeout=10,
        failure_code="pinned_caddy_version_check_failed",
    )
    parts = completed.stdout.strip().split(maxsplit=1)
    version_exact = bool(parts and parts[0] == CADDY_VERSION)
    REPORT["caddy_adapter"]["caddy_version_exact"] = bool(version_exact)
    add_check("pinned_caddy_image_is_version_2_11_1", version_exact)
    if not version_exact:
        fail("pinned_caddy_version_mismatch")


def caddy_run_prefix(*, network: str = "none") -> list[str]:
    return [
        "docker", "run", "--rm", "--pull=never", "--network", network,
        "--memory=192m", "--cpus=0.5", "--pids-limit=32", "--read-only",
        "--cap-drop=ALL", "--cap-add=NET_BIND_SERVICE",
        "--security-opt=no-new-privileges",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--mount", f"type=bind,source={CADDYFILE.resolve()},target=/etc/caddy/Caddyfile,readonly",
        "--env", "DOMAIN=:8080", "--env", "SELLER_PORT=5001",
        "--entrypoint", "caddy",
        CADDY_IMAGE,
    ]


def adapt_caddyfile() -> None:
    try:
        text = CADDYFILE.read_text(encoding="utf-8")
    except Exception:
        fail("caddyfile_unreadable")
    command = caddy_run_prefix() + [
        "adapt", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile",
        "--pretty", "--validate",
    ]
    completed = run_command(
        command, timeout=20, failure_code="caddy_adapt_failed",
    )
    try:
        adapted = json.loads(completed.stdout)
    except Exception:
        fail("caddy_adapt_output_invalid")
    inspection = inspect_adapted_config(adapted, text)
    REPORT["caddy_adapter"].update(inspection)
    add_check("actual_pinned_caddy_adapter_preserves_route_tls_and_no_retry_policy", inspection["valid"])
    if not inspection["valid"]:
        fail("caddy_adapted_transport_contract_mismatch")


def check_runtime_gunicorn() -> None:
    snippet = (
        "import json, gunicorn; from gunicorn.config import Config; "
        "c=Config(); print(json.dumps({'version':gunicorn.__version__,'keepalive':c.keepalive}))"
    )
    command = [
        "docker", "run", "--rm", "--pull=never", "--network", "none",
        "--memory=192m", "--cpus=0.5", "--pids-limit=32", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--entrypoint", "/usr/bin/env", RUNTIME_IMAGE,
        "-i", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONDONTWRITEBYTECODE=1", "python", "-c", snippet,
    ]
    completed = run_command(command, timeout=20, failure_code="runtime_gunicorn_check_failed")
    try:
        values = json.loads(completed.stdout.strip())
    except Exception:
        fail("runtime_gunicorn_output_invalid")
    version_exact = isinstance(values, dict) and values.get("version") == GUNICORN_VERSION
    keepalive_exact = isinstance(values, dict) and type(values.get("keepalive")) is int and values.get("keepalive") == GUNICORN_KEEPALIVE_SECONDS
    REPORT["runtime"].update({
        "gunicorn_version_exact": bool(version_exact),
        "gunicorn_default_keepalive_seconds": values.get("keepalive") if isinstance(values, dict) else None,
        "gunicorn_default_keepalive_exact_2s": bool(keepalive_exact),
    })
    add_check("accepted_runtime_has_gunicorn_21_2_default_keepalive_2s", version_exact and keepalive_exact)
    if not (version_exact and keepalive_exact):
        fail("runtime_gunicorn_contract_mismatch")


BACKEND_SOURCE = r'''from __future__ import annotations
import hashlib
import json
import threading
from urllib.parse import parse_qs

_lock = threading.Lock()
_count = 0
_events = []


def app(environ, start_response):
    global _count
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = -1
    if length < 0 or length > 8192:
        start_response("413 Payload Too Large", [("Content-Length", "0")])
        return [b""]
    body = environ["wsgi.input"].read(length) if length else b""
    query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    with _lock:
        _count += 1
        sequence = _count
        _events.append({
            "sequence": sequence,
            "method": environ.get("REQUEST_METHOD", ""),
            "path": environ.get("PATH_INFO", ""),
            "query": environ.get("QUERY_STRING", ""),
            "probe_header": environ.get("HTTP_X_TRANSPORT_PROBE", ""),
            "content_type": environ.get("CONTENT_TYPE", ""),
            "content_length": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        })
        event_history = list(_events)
    if query.get("case") == ["incomplete"]:
        start_response("200 OK", [
            ("Content-Type", "application/octet-stream"),
            ("Content-Length", "64"),
            ("Cache-Control", "no-store"),
        ])
        def truncated_response():
            yield b"synthetic-partial-response"
            raise RuntimeError("synthetic_incomplete_upstream_response")
        return truncated_response()
    raw_status = query.get("status", ["200"])[0]
    status = int(raw_status) if raw_status in {"200", "503"} else 500
    reason = {200: "OK", 503: "Service Unavailable", 500: "Internal Server Error"}[status]
    value = {
        "sequence": sequence,
        "method": environ.get("REQUEST_METHOD", ""),
        "path": environ.get("PATH_INFO", ""),
        "query": environ.get("QUERY_STRING", ""),
        "probe_header": environ.get("HTTP_X_TRANSPORT_PROBE", ""),
        "content_type": environ.get("CONTENT_TYPE", ""),
        "content_length": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "event_history": event_history,
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(payload))),
        ("X-Synthetic-Backend-Sequence", str(sequence)),
        ("Cache-Control", "no-store"),
    ]
    start_response(f"{status} {reason}", headers)
    return [payload]
'''

CLIENT_SOURCE = r'''import hashlib
import http.client
import json
import socket
import ssl
import sys
import time

HOST = "transport-caddy"
PORT = 8080
CLIENT_SOCKET_TIMEOUT_SECONDS = 5
BODY_OK = b"synthetic-body-success-v1"
BODY_503 = b"synthetic-body-known-503-response-v1"
BODY_INCOMPLETE = b"synthetic-body-incomplete-response-v1"


def expected_event(sequence, method, query, probe, body=None):
    payload = body or b""
    return {
        "sequence": sequence,
        "method": method,
        "path": "/__transport__/probe",
        "query": query,
        "probe_header": probe,
        "content_type": "application/octet-stream" if body is not None else "",
        "content_length": len(payload),
        "body_sha256": hashlib.sha256(payload).hexdigest(),
    }


EXPECTED_EVENTS = [
    expected_event(1, "GET", "case=initial", "synthetic-get"),
    expected_event(2, "POST", "case=post-after-idle", "synthetic-success", BODY_OK),
    expected_event(3, "POST", "case=known-503&status=503", "synthetic-known-503", BODY_503),
    expected_event(4, "POST", "case=incomplete", "synthetic-incomplete", BODY_INCOMPLETE),
    expected_event(5, "GET", "case=final", "synthetic-final"),
]


def wait_for_listener(host, port, tls=False):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=0.5)
            if tls:
                context = ssl._create_unverified_context()
                secure = context.wrap_socket(sock, server_hostname='synthetic-backend.invalid')
                secure.close()
            else:
                sock.close()
            return True
        except Exception:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            time.sleep(0.1)
    return False


def request(case, method, target, probe, body=None, expected_status=200, expected_sequence=1):
    connection = http.client.HTTPConnection(HOST, PORT, timeout=CLIENT_SOCKET_TIMEOUT_SECONDS)
    headers = {"X-Transport-Probe": probe}
    if body is not None:
        headers["Content-Type"] = "application/octet-stream"
    try:
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        raw = response.read(16384)
    except Exception as exc:
        connection.close()
        return {
            "case": case,
            "http_status": None,
            "backend_sequence": None,
            "response_contract_exact": False,
            "failure_class": type(exc).__name__,
        }
    finally:
        try:
            connection.close()
        except Exception:
            pass
    try:
        value = json.loads(raw.decode("ascii"))
    except Exception:
        value = None
    if body is None:
        expected_length = 0
        expected_hash = hashlib.sha256(b"").hexdigest()
    else:
        expected_length = len(body)
        expected_hash = hashlib.sha256(body).hexdigest()
    exact = (
        isinstance(value, dict)
        and status == expected_status
        and value.get("sequence") == expected_sequence
        and value.get("method") == method
        and value.get("path") == "/__transport__/probe"
        and value.get("query") == target.partition("?")[2]
        and value.get("probe_header") == probe
        and value.get("content_type") == ("application/octet-stream" if body is not None else "")
        and value.get("content_length") == expected_length
        and value.get("body_sha256") == expected_hash
    )
    event_history = value.get("event_history") if isinstance(value, dict) else None
    history_exact = event_history == EXPECTED_EVENTS if case == "final_get_sequence" else None
    event_sequence = (
        [event.get("sequence") for event in event_history if isinstance(event, dict)]
        if case == "final_get_sequence" and isinstance(event_history, list) else None
    )
    return {
        "case": case,
        "http_status": status,
        "backend_sequence": value.get("sequence") if isinstance(value, dict) else None,
        "response_contract_exact": bool(exact),
        "backend_event_history_exact": history_exact,
        "backend_event_sequence": event_sequence,
        "failure_class": None if exact else ("synthetic_response_contract_mismatch" if isinstance(value, dict) else "non_json_proxy_response"),
    }


def request_incomplete_post():
    connection = http.client.HTTPConnection(HOST, PORT, timeout=CLIENT_SOCKET_TIMEOUT_SECONDS)
    status = None
    partial_read = False
    transport_failure = None
    content_length_bounded = False
    response_body_bytes_read = 0
    try:
        connection.request(
            "POST", "/__transport__/probe?case=incomplete", body=BODY_INCOMPLETE,
            headers={"X-Transport-Probe": "synthetic-incomplete",
                     "Content-Type": "application/octet-stream"},
        )
        response = connection.getresponse()
        status = response.status
        try:
            declared = int(response.getheader("Content-Length", ""))
        except (TypeError, ValueError):
            declared = -1
        content_length_bounded = 0 <= declared <= 16384
        if content_length_bounded:
            try:
                body = response.read()
                response_body_bytes_read = len(body)
                partial_read = len(body) < declared
            except http.client.IncompleteRead as exc:
                partial_read = True
                response_body_bytes_read = len(exc.partial)
    except Exception as exc:
        transport_failure = type(exc).__name__
    finally:
        try:
            connection.close()
        except Exception:
            pass
    if status == 502 and content_length_bounded:
        response_state = "gateway_error"
    elif status == 200 and content_length_bounded and partial_read:
        response_state = "partial_body"
    elif status is None and transport_failure == "RemoteDisconnected" and response_body_bytes_read == 0:
        response_state = "disconnected_before_headers"
    else:
        response_state = "unexpected_response_state"
    unknown_response_observed = response_state in {
        "gateway_error", "partial_body", "disconnected_before_headers"
    }
    return {
        "case": "post_incomplete_response_not_replayed",
        "backend_sequence": None,
        "http_status": status,
        "response_state": response_state,
        "response_headers_received": status is not None,
        "unknown_response_observed": bool(unknown_response_observed),
        "partial_response_observed": bool(partial_read),
        "content_length_bounded": bool(content_length_bounded),
        "response_body_bytes_read": response_body_bytes_read,
        "socket_timeout_seconds": CLIENT_SOCKET_TIMEOUT_SECONDS,
        "transport_failure_class": transport_failure,
    }


def assess_receipt(observations, listeners_ready):
    observed_response_sequence = [
        row.get("backend_sequence") if isinstance(row, dict) else None
        for row in observations
    ]
    final = observations[-1] if observations and isinstance(observations[-1], dict) else {}
    backend_event_sequence = final.get("backend_event_sequence")
    backend_history_exact = final.get("backend_event_history_exact") is True
    response_sequence_exact = observed_response_sequence == [1, 2, 3, None, 5]
    backend_events_exact = (
        backend_event_sequence == [1, 2, 3, 4, 5]
        and backend_history_exact
        and final.get("backend_sequence") == 5
    )
    normal_responses_exact = len(observations) == 5 and all(
        isinstance(observations[index], dict)
        and observations[index].get("response_contract_exact") is True
        for index in (0, 1, 2, 4)
    )
    incomplete = observations[3] if len(observations) == 5 and isinstance(observations[3], dict) else {}
    incomplete_state = incomplete.get("response_state")
    disconnected_before_headers = (
        incomplete_state == "disconnected_before_headers"
        and incomplete.get("http_status") is None
        and incomplete.get("response_headers_received") is False
        and incomplete.get("transport_failure_class") == "RemoteDisconnected"
        and incomplete.get("response_body_bytes_read") == 0
        and incomplete.get("content_length_bounded") is False
        and incomplete.get("socket_timeout_seconds") == CLIENT_SOCKET_TIMEOUT_SECONDS
    )
    partial_body = (
        incomplete_state == "partial_body"
        and incomplete.get("http_status") == 200
        and incomplete.get("response_headers_received") is True
        and incomplete.get("content_length_bounded") is True
        and incomplete.get("partial_response_observed") is True
    )
    gateway_error = (
        incomplete_state == "gateway_error"
        and incomplete.get("http_status") == 502
        and incomplete.get("response_headers_received") is True
        and incomplete.get("content_length_bounded") is True
    )
    incomplete_observed = (
        len(observations) == 5
        and incomplete.get("backend_sequence") is None
        and incomplete.get("unknown_response_observed") is True
        and (disconnected_before_headers or partial_body or gateway_error)
    )
    post_after_idle_exact = (
        len(observations) == 5
        and isinstance(observations[1], dict)
        and observations[1].get("case") == "post_after_idle_gt_upstream_timeout"
        and observations[1].get("backend_sequence") == 2
        and observations[1].get("response_contract_exact") is True
    )
    exact_once = (
        listeners_ready
        and response_sequence_exact
        and backend_events_exact
        and normal_responses_exact
        and incomplete_observed
        and post_after_idle_exact
    )
    return {
        "status": "client_complete" if exact_once else "client_failed",
        "listeners_ready": bool(listeners_ready),
        "request_count": 5 if listeners_ready and len(observations) == 5 else 0,
        "observed_response_sequence": observed_response_sequence,
        "response_sequence_exact": response_sequence_exact,
        "backend_physical_request_count": len(backend_event_sequence)
            if isinstance(backend_event_sequence, list) else None,
        "backend_event_sequence": backend_event_sequence,
        "backend_history_exact": backend_events_exact,
        "normal_responses_exact": normal_responses_exact,
        "incomplete_response_observed": incomplete_observed,
        "post_after_idle_exact": post_after_idle_exact,
    }


observations = []
listeners_ready = (
    wait_for_listener("seller-platform", 5001, tls=True)
    and wait_for_listener(HOST, PORT)
)
if listeners_ready:
    observations.append(request(
        "initial_get", "GET", "/__transport__/probe?case=initial", "synthetic-get",
        expected_status=200, expected_sequence=1,
    ))
    time.sleep(2.25)
    observations.append(request(
        "post_after_idle_gt_upstream_timeout", "POST", "/__transport__/probe?case=post-after-idle",
        "synthetic-success", BODY_OK, expected_status=200, expected_sequence=2,
    ))
    observations.append(request(
        "post_known_503_response_once", "POST", "/__transport__/probe?case=known-503&status=503",
        "synthetic-known-503", BODY_503, expected_status=503, expected_sequence=3,
    ))
    observations.append(request_incomplete_post())
    observations.append(request(
        "final_get_sequence", "GET", "/__transport__/probe?case=final", "synthetic-final",
        expected_status=200, expected_sequence=5,
    ))
receipt = assess_receipt(observations, listeners_ready)
print(json.dumps({
    **receipt,
    "observations": observations,
}, separators=(",", ":")))
if receipt["status"] != "client_complete":
    sys.exit(2)
'''


class DockerResources:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.network_id: str | None = None
        self.container_ids: list[str] = []
        self.network_name = f"transport-check-{run_id}"

    def create_network(self) -> None:
        network_id = docker_output([
            "network", "create", "--internal", "--driver", "bridge",
            "--label", f"seller-hub.transport-check={self.run_id}",
            self.network_name,
        ], timeout=15, failure_code="synthetic_network_create_failed")
        if not re.fullmatch(r"[0-9a-f]{12,64}", network_id):
            fail("synthetic_network_identity_invalid")
        self.network_id = network_id

    def start_container(self, args: list[str], failure_code: str) -> str:
        result = run_command(["docker", *args], timeout=25, failure_code=failure_code)
        container_id = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            fail("synthetic_container_identity_invalid")
        self.container_ids.append(container_id)
        return container_id

    def cleanup(self) -> None:
        for container_id in reversed(self.container_ids):
            try:
                completed = subprocess.run(
                    ["docker", "rm", "-f", container_id],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                if completed.returncode == 0:
                    REPORT["cleanup"]["containers_removed"] += 1
            except Exception:
                pass
        if self.network_id:
            try:
                completed = subprocess.run(
                    ["docker", "network", "rm", self.network_id],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                REPORT["cleanup"]["network_removed"] = completed.returncode == 0
            except Exception:
                REPORT["cleanup"]["network_removed"] = False


def write_synthetic_backend(temp_dir: Path) -> tuple[Path, Path, Path]:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except Exception:
        fail("synthetic_tls_crypto_unavailable")
    backend = temp_dir / "synthetic_proxy_backend.py"
    cert_file = temp_dir / "synthetic-backend.pem"
    key_file = temp_dir / "synthetic-backend-key.pem"
    backend.write_text(BACKEND_SOURCE, encoding="utf-8")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-backend.invalid")])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    for path in (backend, cert_file, key_file):
        path.chmod(0o644)
    return backend, cert_file, key_file


def run_synthetic_probe(resources: DockerResources, temp_dir: Path) -> None:
    backend, cert_file, key_file = write_synthetic_backend(temp_dir)
    net = resources.network_name
    runtime_base = [
        "--pull=never", "--network", net,
        "--memory=256m", "--cpus=0.5", "--pids-limit=48",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--shm-size=16m", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
    ]
    backend_name = f"transport-backend-{resources.run_id}"
    resources.start_container([
        "run", "--detach", "--name", backend_name,
        "--label", f"seller-hub.transport-check={resources.run_id}",
        *runtime_base,
        "--network-alias", "seller-platform",
        "--mount", f"type=bind,source={backend.resolve()},target=/tmp/synthetic_proxy_backend.py,readonly",
        "--mount", f"type=bind,source={cert_file.resolve()},target=/tmp/synthetic-backend.pem,readonly",
        "--mount", f"type=bind,source={key_file.resolve()},target=/tmp/synthetic-backend-key.pem,readonly",
        "--entrypoint", "/usr/bin/env", RUNTIME_IMAGE,
        "-i", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH=/tmp", "PYTHONDONTWRITEBYTECODE=1",
        "python", "-m", "gunicorn",
        "--bind", "0.0.0.0:5001", "--workers", "1", "--threads", "4",
        "--worker-class", "gthread", "--timeout", "600", "--worker-tmp-dir", "/tmp",
        "--certfile", "/tmp/synthetic-backend.pem", "--keyfile", "/tmp/synthetic-backend-key.pem",
        "--access-logfile", "-", "--error-logfile", "-",
        "synthetic_proxy_backend:app",
    ], "synthetic_gunicorn_start_failed")

    caddy_name = f"transport-caddy-{resources.run_id}"
    resources.start_container([
        "run", "--detach", "--name", caddy_name,
        "--label", f"seller-hub.transport-check={resources.run_id}",
        "--pull=never", "--network", net,
        "--memory=256m", "--cpus=0.5", "--pids-limit=64",
        "--read-only", "--cap-drop=ALL", "--cap-add=NET_BIND_SERVICE",
        "--security-opt=no-new-privileges",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--tmpfs", "/data:rw,nosuid,nodev,size=32m",
        "--tmpfs", "/config:rw,nosuid,nodev,size=16m",
        "--mount", f"type=bind,source={CADDYFILE.resolve()},target=/etc/caddy/Caddyfile,readonly",
        "--network-alias", "transport-caddy",
        "--env", "DOMAIN=:8080", "--env", "SELLER_PORT=5001",
        "--env", "XDG_DATA_HOME=/data", "--env", "XDG_CONFIG_HOME=/config",
        "--entrypoint", "caddy", CADDY_IMAGE,
        "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile",
    ], "synthetic_caddy_start_failed")

    client_code = CLIENT_SOURCE
    client_name = f"transport-client-{resources.run_id}"
    client_id = resources.start_container([
        "run", "--detach", "--pull=never", "--network", net,
        "--memory=128m", "--cpus=0.25", "--pids-limit=32",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=8m",
        "--name", client_name,
        "--label", f"seller-hub.transport-check={resources.run_id}",
        "--entrypoint", "/usr/bin/env", RUNTIME_IMAGE,
        "-i", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONDONTWRITEBYTECODE=1", "python", "-c", client_code,
    ], "synthetic_client_start_failed")
    waited = run_command(
        ["docker", "wait", client_id], timeout=40,
        failure_code="synthetic_client_wait_failed",
    )
    try:
        client_exit_code = int(waited.stdout.strip())
    except ValueError:
        fail("synthetic_client_exit_code_invalid")
    completed = run_command(
        ["docker", "logs", client_id], timeout=10,
        failure_code="synthetic_client_logs_failed",
    )
    try:
        result = json.loads(completed.stdout.strip())
    except Exception:
        fail("synthetic_client_output_invalid")
    observations = result.get("observations") if isinstance(result, dict) else None
    if not isinstance(observations, list):
        fail("synthetic_client_observation_count_invalid")
    REPORT["synthetic_probe"].update({
        "client_exit_code": client_exit_code,
        "client_status": result.get("status"),
        "listeners_ready": result.get("listeners_ready"),
        "client_http_requests": result.get("request_count"),
        "post_after_idle_exact": result.get("post_after_idle_exact") is True,
        "observed_response_sequence": result.get("observed_response_sequence"),
        "backend_event_sequence": result.get("backend_event_sequence"),
        "backend_event_history_exact": result.get("backend_history_exact") is True,
        "observations": observations,
        "backend_physical_request_count": result.get("backend_physical_request_count"),
    })
    if len(observations) != 5:
        add_check("synthetic_client_emits_five_http_observations", False)
        fail("synthetic_client_observation_count_invalid")
    REPORT["synthetic_probe"].update({
        "observed_response_sequence": [
            row.get("backend_sequence") for row in observations if isinstance(row, dict)
        ],
        "post_request_count": sum(
            1 for row in observations
            if isinstance(row, dict) and row.get("case", "").startswith("post_")
        ),
        "post_after_idle_exact": result.get("post_after_idle_exact") is True,
        "post_responses_exactly_once": (
            observations[1].get("backend_sequence") == 2
            and observations[2].get("backend_sequence") == 3
            and observations[1].get("response_contract_exact") is True
            and observations[2].get("response_contract_exact") is True
        ),
        "incomplete_post_not_replayed": (
            result.get("incomplete_response_observed") is True
            and observations[3].get("unknown_response_observed") is True
            and observations[3].get("backend_sequence") is None
            and observations[4].get("backend_sequence") == 5
            and result.get("backend_history_exact") is True
            and result.get("backend_event_sequence") == [1, 2, 3, 4, 5]
            and result.get("backend_physical_request_count") == 5
        ),
    })
    expected_response_sequence = [1, 2, 3, None, 5]
    sequence_exact = REPORT["synthetic_probe"]["observed_response_sequence"] == expected_response_sequence
    request_headers_body_exact = (
        all(
            isinstance(observations[index], dict)
            and observations[index].get("response_contract_exact") is True
            for index in (0, 1, 2, 4)
        )
        and result.get("backend_history_exact") is True
    )
    client_count_exact = result.get("request_count") == 5
    backend_count_exact = (
        result.get("backend_physical_request_count") == 5
        and result.get("backend_event_sequence") == [1, 2, 3, 4, 5]
        and result.get("backend_history_exact") is True
    )
    success = (
        client_exit_code == 0 and result.get("status") == "client_complete" and sequence_exact
        and request_headers_body_exact and client_count_exact and backend_count_exact
        and REPORT["synthetic_probe"]["post_after_idle_exact"]
        and REPORT["synthetic_probe"]["post_responses_exactly_once"]
        and REPORT["synthetic_probe"]["incomplete_post_not_replayed"]
    )
    add_check("synthetic_post_after_idle_over_2s_routes_through_keepalive_1s", REPORT["synthetic_probe"]["post_after_idle_exact"])
    add_check("synthetic_request_headers_and_body_reach_tls_backend_exactly", request_headers_body_exact)
    add_check("synthetic_success_and_known_503_posts_each_reach_backend_once", REPORT["synthetic_probe"]["post_responses_exactly_once"])
    add_check("synthetic_incomplete_post_is_observed_and_not_replayed", REPORT["synthetic_probe"]["incomplete_post_not_replayed"])
    add_check("synthetic_transport_scenario_all_contracts", success)
    if not success:
        fail("synthetic_transport_behavior_mismatch")


def self_test() -> None:
    synthetic = {
        "apps": {"http": {"servers": {"srv0": {"routes": [{
            "handle": [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": "seller-platform:5001"}],
                "transport": {
                    "protocol": "http",
                    "tls": {"insecure_skip_verify": True},
                    "keep_alive": {"enabled": True, "idle_timeout": "1s"},
                },
            }],
        }]}}}},
    }
    valid = inspect_adapted_config(synthetic, "reverse_proxy seller-platform:5001 { transport http { keepalive 1s } }")
    wrong_timeout = json.loads(json.dumps(synthetic))
    wrong_timeout["apps"]["http"]["servers"]["srv0"]["routes"][0]["handle"][0]["transport"]["keep_alive"]["idle_timeout"] = "2m"
    invalid = inspect_adapted_config(wrong_timeout, "reverse_proxy seller-platform:5001 { transport http { keepalive 2m } }")
    retry_config = json.loads(json.dumps(synthetic))
    retry_config["apps"]["http"]["servers"]["srv0"]["routes"][0]["handle"][0]["load_balancing"] = {"retries": 1}
    retry_invalid = inspect_adapted_config(
        retry_config,
        "reverse_proxy seller-platform:5001 { lb_retries 1 transport http { keepalive 1s } }",
    )
    assert valid["valid"] is True
    assert invalid["valid"] is False and invalid["caddy_idle_timeout_1s"] is False
    assert retry_invalid["valid"] is False and retry_invalid["adapted_load_balancer_retries_zero"] is False
    assert duration_is_zero(False) is False
    add_check("self_test_accepts_exact_transport_and_rejects_default_timeout_or_retry", True)

    client_prefix = CLIENT_SOURCE.split("\nobservations = []", 1)[0]
    client_namespace: dict[str, Any] = {}
    exec(compile(client_prefix, "<synthetic-client-receipt-self-test>", "exec"), client_namespace)
    normal = [
        {"case": "initial_get", "backend_sequence": 1, "response_contract_exact": True},
        {"case": "post_after_idle_gt_upstream_timeout", "backend_sequence": 2, "response_contract_exact": True},
        {"case": "post_known_503_response_once", "backend_sequence": 3, "response_contract_exact": True},
    ]
    incomplete = {
        "case": "post_incomplete_response_not_replayed",
        "backend_sequence": None,
        "http_status": None,
        "response_state": "disconnected_before_headers",
        "response_headers_received": False,
        "unknown_response_observed": True,
        "partial_response_observed": False,
        "content_length_bounded": False,
        "response_body_bytes_read": 0,
        "socket_timeout_seconds": 5,
        "transport_failure_class": "RemoteDisconnected",
    }
    final = {
        "case": "final_get_sequence",
        "backend_sequence": 5,
        "response_contract_exact": True,
        "backend_event_sequence": [1, 2, 3, 4, 5],
        "backend_event_history_exact": True,
    }
    receipt_observations = [*normal, incomplete, final]
    good_receipt = client_namespace["assess_receipt"](receipt_observations, True)
    assert good_receipt["status"] == "client_complete"
    assert good_receipt["observed_response_sequence"] == [1, 2, 3, None, 5]
    assert good_receipt["backend_physical_request_count"] == 5
    assert good_receipt["backend_event_sequence"] == [1, 2, 3, 4, 5]

    partial_observations = json.loads(json.dumps(receipt_observations))
    partial_observations[3].update({
        "http_status": 200,
        "response_state": "partial_body",
        "response_headers_received": True,
        "unknown_response_observed": True,
        "partial_response_observed": True,
        "content_length_bounded": True,
        "response_body_bytes_read": 27,
        "transport_failure_class": None,
    })
    partial_receipt = client_namespace["assess_receipt"](partial_observations, True)
    assert partial_receipt["status"] == "client_complete"

    arbitrary_failure = json.loads(json.dumps(receipt_observations))
    arbitrary_failure[3]["transport_failure_class"] = "TimeoutError"
    arbitrary_failure[3]["unknown_response_observed"] = True
    arbitrary_receipt = client_namespace["assess_receipt"](arbitrary_failure, True)
    assert arbitrary_receipt["status"] == "client_failed"
    add_check("self_test_accepts_bounded_disconnect_or_partial_and_rejects_other_exception", True)

    replayed_observations = json.loads(json.dumps(receipt_observations))
    replayed_observations[-1]["backend_sequence"] = 6
    replayed_observations[-1]["backend_event_sequence"] = [1, 2, 3, 4, 5, 6]
    replayed_observations[-1]["backend_event_history_exact"] = False
    replay_receipt = client_namespace["assess_receipt"](replayed_observations, True)
    assert replay_receipt["status"] == "client_failed"
    assert replay_receipt["observed_response_sequence"] == [1, 2, 3, None, 6]
    assert replay_receipt["backend_physical_request_count"] == 6
    add_check("self_test_preserves_unknown_response_sequence_and_rejects_replay_counter", True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="run the isolated synthetic Docker acceptance")
    parser.add_argument("--self-test", action="store_true", help="exercise local acceptance predicates without Docker")
    args = parser.parse_args(argv)
    if args.self_test and not args.run:
        try:
            self_test()
            REPORT["status"] = "self_test_passed"
            REPORT["synthetic_only"] = True
        except Exception:
            REPORT["status"] = "self_test_failed"
            REPORT["failure_code"] = "self_test_assertion_failed"
        print(json.dumps(REPORT, separators=(",", ":"), sort_keys=True))
        return 0 if REPORT["status"] == "self_test_passed" else 2
    if not args.run or args.self_test:
        REPORT["status"] = "not_started"
        REPORT["failure_code"] = "explicit_run_flag_required"
        print(json.dumps(REPORT, separators=(",", ":"), sort_keys=True))
        return 2

    resources: DockerResources | None = None
    temp_dir_path: Path | None = None
    try:
        if not CADDYFILE.is_file():
            fail("caddyfile_missing")
        if not shutil.which("docker"):
            fail("docker_cli_unavailable")
        verify_local_images()
        check_caddy_version()
        adapt_caddyfile()
        check_runtime_gunicorn()
        add_check("actual_caddyfile_adapts_to_keepalive_1s", True)

        temp_dir_path = Path(tempfile.mkdtemp(prefix="reverse-proxy-transport-"))
        temp_dir_path.chmod(0o700)
        run_id = os.urandom(8).hex()
        resources = DockerResources(run_id)
        resources.create_network()
        run_synthetic_probe(resources, temp_dir_path)
        REPORT["status"] = "passed_synthetic"
    except CheckFailure as error:
        REPORT["status"] = "failed"
        REPORT["failure_code"] = error.code
    except Exception:
        REPORT["status"] = "failed"
        REPORT["failure_code"] = "operator_internal_error"
    finally:
        if resources is not None:
            resources.cleanup()
        if temp_dir_path is not None:
            try:
                shutil.rmtree(temp_dir_path)
                REPORT["cleanup"]["synthetic_material_removed"] = not temp_dir_path.exists()
            except Exception:
                REPORT["cleanup"]["synthetic_material_removed"] = False
        if REPORT["status"] == "passed_synthetic":
            add_check(
                "only_run_scoped_containers_and_network_removed",
                REPORT["cleanup"]["network_removed"] is True
                and REPORT["cleanup"]["containers_removed"] == 3
                and REPORT["cleanup"]["synthetic_material_removed"] is True,
            )
            if REPORT["checks"] and any(row["status"] != "passed" for row in REPORT["checks"]):
                REPORT["status"] = "failed"
                REPORT["failure_code"] = "cleanup_check_failed"
        print(json.dumps(REPORT, separators=(",", ":"), sort_keys=True))
    return 0 if REPORT["status"] in {"passed_synthetic", "self_test_passed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
