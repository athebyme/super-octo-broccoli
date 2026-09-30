#!/usr/bin/env python3
"""Build and restart the app only after native Flash reservations have drained.

The gate uses SQLite's writer lock shared with ``reserve_attempt``. Every
physical native Flash request must commit its ``reserved`` ledger row before
HTTP, so holding this lock after observing zero reserved rows fences new calls
until the old app container has stopped.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote
from typing import Callable, Optional, Sequence


SERVICE = "seller-platform"
DATABASE_PATH = "/app/data/seller_platform.db"
LOCK_HOLDER_PATH = "/app/deploy_safety.py"
LOCK_BUSY_TIMEOUT_MS = 200
LOCK_HOLDER_START_SECONDS = 15
GATE_WAIT_SECONDS = 300
GATE_POLL_SECONDS = 1
CONTAINER_STOP_SECONDS = 60


class DeploySafetyError(RuntimeError):
    pass


class ActiveAttempts(DeploySafetyError):
    def __init__(self, count: int):
        self.count = count
        super().__init__(f"{count} native Flash call(s) are still reserved")


@dataclass
class DatabaseGate:
    process: subprocess.Popen[str]
    container_name: str

    def release(self) -> None:
        if self.process.poll() is not None:
            raise DeploySafetyError("database gate exited before release")
        if self.process.stdin is None:
            raise DeploySafetyError("database gate stdin is unavailable")
        self.process.stdin.write("release\n")
        self.process.stdin.flush()
        try:
            output, _ = self.process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _run_docker(["rm", "-f", self.container_name], check=False)
            self.process.wait(timeout=10)
            raise DeploySafetyError("database gate did not release cleanly") from None
        if self.process.returncode != 0 or "RELEASED" not in output:
            raise DeploySafetyError("database gate release was not confirmed")


def acquire_sqlite_gate(database_path: str) -> sqlite3.Connection:
    """Acquire the shared writer lock; reject ledger or worker pre-claims."""
    uri = "file:" + quote(str(Path(database_path).resolve()), safe="/:") + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
    try:
        connection.execute(f"PRAGMA busy_timeout={LOCK_BUSY_TIMEOUT_MS}")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM ai_parsing_attempts WHERE status = 'reserved')
              + (SELECT COUNT(*) FROM ozon_draft_completion_items WHERE status = 'reserved')
              + (SELECT COUNT(*)
                   FROM supplier_catalog_enrichment_items AS item
                   JOIN supplier_catalog_enrichment_runs AS run ON run.id = item.run_id
                  WHERE run.model_used = 'deepseek-flash' AND item.status = 'running')
            """
        ).fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise DeploySafetyError("native Flash reservation count is invalid")
        if row[0]:
            connection.rollback()
            raise ActiveAttempts(row[0])
        return connection
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        raise


def _lock_holder(database_path: str) -> int:
    try:
        connection = acquire_sqlite_gate(database_path)
    except ActiveAttempts as error:
        print(f"ACTIVE:{error.count}", flush=True)
        return 3
    except sqlite3.OperationalError as error:
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            print("BUSY", flush=True)
            return 4
        print("ERROR:database_unavailable", flush=True)
        return 5
    except (sqlite3.Error, DeploySafetyError):
        print("ERROR:database_unavailable", flush=True)
        return 5

    print("LOCKED", flush=True)
    try:
        # A finite holder lifetime keeps an interrupted host wrapper from
        # leaving every SQLite writer blocked indefinitely.
        ready, _, _ = select.select([sys.stdin], [], [], GATE_WAIT_SECONDS)
        if not ready:
            print("ERROR:gate_timeout", flush=True)
            return 6
        sys.stdin.readline()
        connection.rollback()
        print("RELEASED", flush=True)
        return 0
    finally:
        connection.close()


def _run_docker(args: Sequence[str], *, check: bool = True,
                capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], check=check,
        text=True, capture_output=capture_output,
    )


def _run_compose(project_dir: Path, args: Sequence[str], *, check: bool = True,
                 capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args], cwd=project_dir, check=check,
        text=True, capture_output=capture_output,
    )


def _acquire_host_deploy_lock(project_dir: Path) -> int:
    resolved = str(project_dir.resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:20]
    lock_path = Path("/tmp") / f"seller-deploy-{digest}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise DeploySafetyError("deployment lock file is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise DeploySafetyError("another guarded deployment is already running") from None
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _service_container_state(project_dir: Path) -> tuple[bool, str]:
    listing = _run_compose(
        project_dir, ["ps", "-a", "-q", SERVICE],
        capture_output=True,
    )
    container_ids = listing.stdout.strip().splitlines() if listing.stdout.strip() else []
    if not container_ids:
        raise DeploySafetyError("seller-platform container is absent; deployment is fail-closed")
    if len(container_ids) != 1:
        raise DeploySafetyError("seller-platform container count is ambiguous; deployment is fail-closed")
    container_id = container_ids[0]
    inspected = _run_docker(
        ["inspect", "--format", "{{.State.Running}}", container_id],
        capture_output=True,
    )
    state = inspected.stdout.strip().lower()
    if state not in ("true", "false"):
        raise DeploySafetyError("could not verify seller-platform container state")
    return state == "true", container_id


def _data_volume_name(container_id: str) -> str:
    inspected = _run_docker(
        ["inspect", "--format", "{{json .Mounts}}", container_id], capture_output=True,
    ).stdout.strip()
    try:
        mounts = json.loads(inspected)
    except (TypeError, json.JSONDecodeError):
        raise DeploySafetyError("could not inspect app database volume") from None
    for mount in mounts if isinstance(mounts, list) else []:
        if (isinstance(mount, dict) and mount.get("Destination") == "/app/data"
                and mount.get("Type") == "volume"
                and isinstance(mount.get("Name"), str)
                and mount["Name"] and all(c.isalnum() or c in "_.-" for c in mount["Name"])):
            return mount["Name"]
    raise DeploySafetyError("app database is not on an inspectable named volume")


def _discard_gate_process(container_name: str, process: subprocess.Popen[str]) -> None:
    _run_docker(["rm", "-f", container_name], check=False)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _start_database_gate(project_dir: Path, container_id: str) -> DatabaseGate:
    image = _run_docker(
        ["inspect", "--format", "{{.Image}}", container_id], capture_output=True,
    ).stdout.strip()
    if not image:
        raise DeploySafetyError("could not identify the running app image")
    container_name = f"seller-deploy-gate-{os.getpid()}"
    script_path = Path(__file__).resolve()
    command = [
        "docker", "run", "--rm", "-i", "--name", container_name,
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "1000:1000",
        "--mount", f"type=volume,source={_data_volume_name(container_id)},target=/app/data",
        "-v", f"{script_path}:/app/deploy_safety.py:ro",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python", image, LOCK_HOLDER_PATH,
        "--lock-holder", "--db-path", DATABASE_PATH,
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    if process.stdout is None:
        raise DeploySafetyError("database gate output is unavailable")
    deadline = time.monotonic() + LOCK_HOLDER_START_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read().strip()
            if output.startswith("ACTIVE:"):
                try:
                    raise ActiveAttempts(int(output.split(":", 1)[1]))
                except ValueError:
                    raise DeploySafetyError("database gate returned an invalid count") from None
            if output == "BUSY":
                raise DeploySafetyError("database writer is busy; retrying safely")
            if output.startswith("ERROR:"):
                raise DeploySafetyError("native Flash database gate failed closed")
            raise DeploySafetyError("database gate exited before acquiring lock")
        ready, _, _ = select.select([process.stdout], [], [], 0.2)
        if not ready:
            continue
        line = process.stdout.readline().strip()
        if line == "LOCKED":
            return DatabaseGate(process, container_name)
        if line.startswith("ACTIVE:"):
            try:
                count = int(line.split(":", 1)[1])
            except ValueError:
                _discard_gate_process(container_name, process)
                raise DeploySafetyError("database gate returned an invalid count") from None
            process.wait(timeout=10)
            raise ActiveAttempts(count)
        if line == "BUSY":
            process.wait(timeout=10)
            raise DeploySafetyError("database writer is busy; retrying safely")
        _discard_gate_process(container_name, process)
        raise DeploySafetyError("database gate returned an unexpected state")
    _discard_gate_process(container_name, process)
    raise DeploySafetyError("database gate did not become ready")


def _acquire_database_gate(project_dir: Path, container_id: str,
                           *, timeout: int = GATE_WAIT_SECONDS,
                           sleep: Callable[[float], None] = time.sleep) -> DatabaseGate:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _start_database_gate(project_dir, container_id)
        except ActiveAttempts as error:
            print(f"Waiting for {error.count} reserved native Flash call(s) to finish", flush=True)
        except DeploySafetyError as error:
            if str(error) != "database writer is busy; retrying safely":
                raise
            print("Waiting for SQLite writer transaction to finish", flush=True)
        if time.monotonic() >= deadline:
            raise DeploySafetyError("native Flash calls did not drain before the deployment deadline")
        sleep(GATE_POLL_SECONDS)


def _run_compose_restart(project_dir: Path, *, up_all: bool,
                         compose_down: bool,
                         run_compose: Callable[..., subprocess.CompletedProcess[str]] = _run_compose,
                         service_state: Callable[[Path], tuple[bool, str]] = _service_container_state,
                         acquire_gate: Callable[..., DatabaseGate] = _acquire_database_gate) -> None:
    running, container_id = service_state(project_dir)
    if not running:
        raise DeploySafetyError("seller-platform is not running; deployment is fail-closed")
    gate: Optional[DatabaseGate] = None
    try:
        if running:
            gate = acquire_gate(project_dir, container_id)
            # The database write lock stays held through the complete stop.
            run_compose(project_dir, ["stop", "--timeout", str(CONTAINER_STOP_SECONDS), SERVICE])
            still_running, _ = service_state(project_dir)
            if still_running:
                raise DeploySafetyError("seller-platform did not stop; deployment aborted")
            if compose_down:
                # Do not use -v: the persistent database volume must survive.
                run_compose(project_dir, ["down", "--remove-orphans"])
            gate.release()
            gate = None
        elif compose_down:
            run_compose(project_dir, ["down", "--remove-orphans"])

        if up_all:
            run_compose(project_dir, ["up", "-d", "--no-build"])
        else:
            run_compose(project_dir, [
                "up", "-d", "--no-deps", "--no-build", "--force-recreate", SERVICE,
            ])
    finally:
        if gate is not None:
            # On a failed stop, release the gate so the still-running app can
            # continue accepting bounded writes. No stop is retried implicitly.
            try:
                gate.release()
            except Exception:
                pass


def deploy(project_dir: Path, *, no_cache: bool, pull: bool,
           build_all: bool, up_all: bool, compose_down: bool) -> None:
    lock_fd = _acquire_host_deploy_lock(project_dir)
    try:
        build_args = ["build"]
        if no_cache:
            build_args.append("--no-cache")
        if pull:
            build_args.append("--pull")
        if not build_all:
            build_args.append(SERVICE)
        print("Building deployment image(s)", flush=True)
        _run_compose(project_dir, build_args)
        print("Waiting for native Flash calls and restarting app", flush=True)
        _run_compose_restart(project_dir, up_all=up_all, compose_down=compose_down)
        print("Guarded deployment command completed", flush=True)
    finally:
        os.close(lock_fd)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--no-cache", action="store_true", help="build without Docker cache")
    parser.add_argument("--pull", action="store_true", help="pull base images during build")
    parser.add_argument("--build-all", action="store_true", help="build all default Compose services")
    parser.add_argument("--up-all", action="store_true", help="start all default Compose services")
    parser.add_argument("--compose-down", action="store_true",
                        help="remove default Compose containers after the guarded stop; keep volumes")
    parser.add_argument("--lock-holder", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--db-path", default=DATABASE_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.lock_holder:
        return _lock_holder(args.db_path)
    try:
        deploy(
            args.project_dir.resolve(), no_cache=args.no_cache, pull=args.pull,
            build_all=args.build_all, up_all=args.up_all,
            compose_down=args.compose_down,
        )
    except DeploySafetyError as error:
        print(f"ERROR: guarded deployment aborted: {error}", file=sys.stderr, flush=True)
        return 1
    except subprocess.CalledProcessError:
        print("ERROR: Docker Compose command failed; restart aborted", file=sys.stderr, flush=True)
        return 1
    except OSError:
        print("ERROR: deployment tool unavailable; restart aborted", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
