#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Authenticated HTTP bridge from Seller Hub to the persistent Qwen queue.

Bind to 127.0.0.1 and expose it through an HTTPS reverse proxy or SSH/WireGuard
tunnel.  The bridge accepts background-only prompts; product bytes never reach
the GPU server.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import shutil
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

MAX_BODY = 16 * 1024
JOB_RE = re.compile(r"^[a-f0-9]{32}$")
PROMPT_BANNED_RE = re.compile(
    r"\b(add|include|show|render|depict|write|place|draw|добав\w*|покаж\w*|"
    r"нарис\w*|напиш\w*|размест\w*)\b.{0,40}\b("
    r"product|package|person|people|text|headline|caption|logo|watermark|"
    r"товар\w*|упаков\w*|человек\w*|люд\w*|текст\w*|надпис\w*|логотип\w*)\b",
    re.IGNORECASE,
)


class Bridge:
    def __init__(self, root: Path, queue: Path, token: str, max_queue: int):
        self.root = root.resolve()
        self.queue = queue.resolve()
        self.token = token
        self.max_queue = max_queue
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        if not JOB_RE.fullmatch(job_id):
            raise ValueError("invalid job id")
        return self.root / "jobs" / job_id

    def queue_file(self, job_id: str, suffix: str) -> Path:
        return self.queue / f"{job_id}{suffix}"

    def create(self, payload: dict) -> str:
        pending = sum(1 for suffix in ("*.json", "*.claim") for _ in self.queue.glob(suffix))
        if pending >= self.max_queue:
            raise RuntimeError("queue is full")
        prompt = " ".join(str(payload.get("prompt") or "").split()).strip()
        if not prompt or len(prompt) > 2000:
            raise ValueError("prompt must contain 1..2000 characters")
        if PROMPT_BANNED_RE.search(prompt):
            raise ValueError("only text-free background prompts are allowed")
        steps = payload.get("steps", 4)
        true_cfg = payload.get("true_cfg", 1.0)
        if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 50:
            raise ValueError("steps must be integer 1..50")
        if isinstance(true_cfg, bool) or not isinstance(true_cfg, (int, float)):
            raise ValueError("true_cfg must be numeric")

        job_id = uuid.uuid4().hex
        job_dir = self.job_dir(job_id)
        bundle = job_dir / "bundle"
        output = job_dir / "out"
        bundle.mkdir(parents=True)
        output.mkdir(parents=True)
        manifest = {
            "production_policy": "background_only_pixel_preserved_composite",
            "products": [{"id": 1, "background_prompt": prompt}],
            "presets": {},
            "text_samples": [],
            "short_texts": [],
        }
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        job = {
            "kind": "t2i",
            "bundle": str(bundle),
            "out": str(output),
            "steps": steps,
            "true_cfg": float(true_cfg),
            "width": 896,
            "height": 1200,
            "bridge_job_id": job_id,
            "created_at": int(time.time()),
        }
        temporary = self.queue_file(job_id, ".tmp")
        temporary.write_text(json.dumps(job), encoding="utf-8")
        temporary.replace(self.queue_file(job_id, ".json"))
        return job_id

    def status(self, job_id: str) -> dict:
        job_dir = self.job_dir(job_id)
        if not job_dir.exists():
            raise FileNotFoundError(job_id)
        state = "queued"
        if self.queue_file(job_id, ".claim").exists():
            state = "running"
        elif self.queue_file(job_id, ".done").exists():
            state = "completed"
        elif self.queue_file(job_id, ".failed").exists():
            state = "failed"
        result = self._result(job_id)
        if result and result.get("status") == "error":
            state = "failed"
        return {
            "job_id": job_id,
            "status": state,
            "error": (result or {}).get("error", ""),
            "latency_s": (result or {}).get("latency_s"),
            "cost_rub": (result or {}).get("cost_rub"),
        }

    def _result(self, job_id: str):
        path = self.job_dir(job_id) / "out" / "results.jsonl"
        if not path.is_file():
            return None
        rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return json.loads(rows[-1]) if rows else None

    def image_path(self, job_id: str) -> Path:
        if self.status(job_id)["status"] != "completed":
            raise RuntimeError("job is not completed")
        result = self._result(job_id) or {}
        name = result.get("artifact")
        if not isinstance(name, str) or Path(name).name != name:
            raise RuntimeError("invalid result artifact")
        path = (self.job_dir(job_id) / "out" / name).resolve()
        path.relative_to(self.job_dir(job_id).resolve())
        if not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
            raise RuntimeError("result artifact unavailable")
        return path

    def cancel(self, job_id: str) -> bool:
        queued = self.queue_file(job_id, ".json")
        if not queued.exists():
            return False
        queued.rename(self.queue_file(job_id, ".cancelled"))
        return True


def handler_factory(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SellerHubGPUBridge/1.0"

        def _authorized(self):
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {bridge.token}"
            return hmac.compare_digest(supplied, expected)

        def _json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _auth_or_401(self):
            if self._authorized():
                return True
            self._json(401, {"error": "unauthorized"})
            return False

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/healthz":
                self._json(200, {"ok": True, "queue": len(list(bridge.queue.glob("*.json")))})
                return
            if not self._auth_or_401():
                return
            match = re.fullmatch(r"/v1/jobs/([a-f0-9]{32})(/image)?", path)
            if not match:
                self._json(404, {"error": "not found"})
                return
            try:
                job_id, image_suffix = match.groups()
                if image_suffix:
                    image = bridge.image_path(job_id)
                    data = image.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._json(200, bridge.status(job_id))
            except FileNotFoundError:
                self._json(404, {"error": "job not found"})
            except (RuntimeError, ValueError) as exc:
                self._json(409, {"error": str(exc)})

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/v1/jobs":
                self._json(404, {"error": "not found"})
                return
            if not self._auth_or_401():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise ValueError("invalid body size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("body must be object")
                job_id = bridge.create(payload)
                self._json(202, {"job_id": job_id, "status": "queued"})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self._json(429, {"error": str(exc)})

        def do_DELETE(self):  # noqa: N802
            if not self._auth_or_401():
                return
            match = re.fullmatch(r"/v1/jobs/([a-f0-9]{32})", urlparse(self.path).path)
            if not match:
                self._json(404, {"error": "not found"})
                return
            cancelled = bridge.cancel(match.group(1))
            self._json(200 if cancelled else 409, {"cancelled": cancelled})

        def log_message(self, format_string, *args):
            print("bridge", self.address_string(), format_string % args, flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--root", default="~/image_bridge")
    parser.add_argument("--queue", default="~/jobs")
    parser.add_argument("--max-queue", type=int, default=100)
    args = parser.parse_args()
    token = os.environ.get("GPU_BRIDGE_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("GPU_BRIDGE_TOKEN must contain at least 32 characters")
    bridge = Bridge(
        Path(args.root).expanduser(),
        Path(args.queue).expanduser(),
        token,
        max(1, min(args.max_queue, 1000)),
    )
    server = ThreadingHTTPServer((args.host, args.port), handler_factory(bridge))
    print(f"GPU bridge listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
