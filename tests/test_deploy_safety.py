from __future__ import annotations

from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import threading
import io

import pytest

from scripts import deploy_safety


def _ledger(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("CREATE TABLE ai_parsing_attempts (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
    connection.execute("CREATE TABLE ozon_draft_completion_items (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
    connection.execute("CREATE TABLE supplier_catalog_enrichment_runs (id TEXT PRIMARY KEY, model_used TEXT)")
    connection.execute("CREATE TABLE supplier_catalog_enrichment_items (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL)")
    return connection


def test_gate_refuses_reserved_calls_and_missing_database_schema(tmp_path):
    path = tmp_path / "ledger.sqlite"
    connection = _ledger(path)
    connection.execute("INSERT INTO ai_parsing_attempts(status) VALUES ('reserved')")
    connection.close()

    with pytest.raises(deploy_safety.ActiveAttempts) as error:
        deploy_safety.acquire_sqlite_gate(str(path))
    assert error.value.count == 1

    with pytest.raises(sqlite3.OperationalError):
        deploy_safety.acquire_sqlite_gate(str(tmp_path / "missing-table.sqlite"))
    assert not (tmp_path / "missing-table.sqlite").exists()


@pytest.mark.parametrize(
    "setup",
    [
        "INSERT INTO ozon_draft_completion_items(status) VALUES ('reserved')",
        "INSERT INTO supplier_catalog_enrichment_runs(id, model_used) VALUES ('run', 'deepseek-flash'); "
        "INSERT INTO supplier_catalog_enrichment_items(run_id, status) VALUES ('run', 'running')",
    ],
)
def test_gate_refuses_pre_ledger_worker_claims(tmp_path, setup):
    path = tmp_path / "ledger.sqlite"
    connection = _ledger(path)
    connection.executescript(setup)
    connection.close()

    with pytest.raises(deploy_safety.ActiveAttempts) as error:
        deploy_safety.acquire_sqlite_gate(str(path))
    assert error.value.count == 1


def test_non_flash_admin_item_does_not_hold_native_flash_drain(tmp_path):
    path = tmp_path / "ledger.sqlite"
    connection = _ledger(path)
    connection.execute(
        "INSERT INTO supplier_catalog_enrichment_runs(id, model_used) VALUES ('run', 'deepseek-v4-pro')"
    )
    connection.execute(
        "INSERT INTO supplier_catalog_enrichment_items(run_id, status) VALUES ('run', 'running')"
    )
    connection.close()

    gate = deploy_safety.acquire_sqlite_gate(str(path))
    gate.rollback()
    gate.close()


def test_database_gate_serializes_reservation_before_physical_call(tmp_path):
    path = tmp_path / "ledger.sqlite"
    connection = _ledger(path)
    connection.close()

    gate = deploy_safety.acquire_sqlite_gate(str(path))
    outcome = {}
    physical_calls = []

    def reserve_then_call():
        writer = sqlite3.connect(path, timeout=0, isolation_level=None)
        writer.execute("PRAGMA busy_timeout=30")
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO ai_parsing_attempts(status) VALUES ('reserved')")
            writer.commit()
            # The real caller creates its HTTP future only after reservation commit.
            physical_calls.append("started_after_commit")
            outcome["reserved"] = True
        except sqlite3.OperationalError as error:
            writer.rollback()
            outcome["reserved"] = False
            outcome["error"] = str(error).lower()
        finally:
            writer.close()

    thread = threading.Thread(target=reserve_then_call)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert outcome["reserved"] is False
    assert "locked" in outcome["error"] or "busy" in outcome["error"]
    assert physical_calls == []
    assert gate.execute("SELECT COUNT(*) FROM ai_parsing_attempts").fetchone()[0] == 0

    gate.rollback()
    gate.close()
    reserve_then_call()
    assert outcome["reserved"] is True
    assert physical_calls == ["started_after_commit"]


def test_lock_holder_cli_holds_gate_until_explicit_release(tmp_path, capsys, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    connection = _ledger(path)
    connection.close()

    gate = deploy_safety.acquire_sqlite_gate(str(path))
    assert deploy_safety._lock_holder(str(path)) == 4
    gate.rollback()
    gate.close()

    # A real host deployment uses a separate container process and stdin as its
    # release handshake. Exercise the same holder through a thread-local stdin.
    import io

    monkeypatch.setattr(deploy_safety.sys, "stdin", io.StringIO("release\n"))
    monkeypatch.setattr(
        deploy_safety.select, "select",
        lambda readers, _writers, _errors, _timeout: (readers, [], []),
    )
    assert deploy_safety._lock_holder(str(path)) == 0
    output = capsys.readouterr().out
    assert "LOCKED" in output
    assert "RELEASED" in output


def test_database_gate_process_release_handshake(tmp_path):
    path = tmp_path / "ledger.sqlite"
    connection = _ledger(path)
    connection.close()
    process = subprocess.Popen(
        [sys.executable, str(Path(deploy_safety.__file__).resolve()),
         "--lock-holder", "--db-path", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "LOCKED"

    deploy_safety.DatabaseGate(process, "unused-test-container").release()
    assert process.returncode == 0


def test_restart_keeps_gate_until_container_is_stopped_then_releases_before_up(tmp_path):
    events = []
    state = {"running": True}

    class FakeGate:
        held = True

        def release(self):
            self.held = False
            events.append("release")

    gate = FakeGate()

    def service_state(_project_dir):
        events.append(("state", state["running"]))
        return state["running"], "container-id"

    def acquire_gate(_project_dir, _container_id):
        events.append("acquire")
        return gate

    def run_compose(_project_dir, args, **_kwargs):
        command = tuple(args)
        if args[0] == "stop":
            assert gate.held
            events.append("stop_under_gate")
            state["running"] = False
        elif args[0] == "up":
            assert not gate.held
            events.append("up_after_release")
        else:
            events.append(command)
        return None

    deploy_safety._run_compose_restart(
        tmp_path, up_all=False, compose_down=False,
        run_compose=run_compose, service_state=service_state,
        acquire_gate=acquire_gate,
    )

    assert events.index("acquire") < events.index("stop_under_gate")
    assert events.index("stop_under_gate") < events.index("release")
    assert events.index("release") < events.index("up_after_release")


def test_deploy_builds_before_guarded_restart_without_docker(tmp_path, monkeypatch):
    events = []
    lock_fd = os.open(os.devnull, os.O_RDONLY)

    def fake_run_compose(_project_dir, args, **_kwargs):
        events.append(("compose", tuple(args)))
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_restart(project_dir, **kwargs):
        events.append(("restart", kwargs))

    monkeypatch.setattr(deploy_safety, "_acquire_host_deploy_lock", lambda _path: lock_fd)
    monkeypatch.setattr(deploy_safety, "_run_compose", fake_run_compose)
    monkeypatch.setattr(deploy_safety, "_run_compose_restart", fake_restart)

    deploy_safety.deploy(
        tmp_path, no_cache=True, pull=True, build_all=False,
        up_all=True, compose_down=False,
    )

    assert events[0] == ("compose", ("build", "--no-cache", "--pull", "seller-platform"))
    assert events[1][0] == "restart"
    assert events[1][1] == {"up_all": True, "compose_down": False}


def test_database_gate_sidecar_is_networkless_nonroot_and_volume_scoped(tmp_path, monkeypatch):
    events = []

    class FakeProcess:
        stdin = io.StringIO()
        stdout = io.StringIO("LOCKED\n")

        def poll(self):
            return None

    monkeypatch.setattr(
        deploy_safety, "_run_docker",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0,
            stdout=("sha256:app-image" if args[0] == "inspect" and args[1] == "--format"
                    and args[2] == "{{.Image}}" else json.dumps([
                        {"Destination": "/app/data", "Type": "volume", "Name": "project_data"},
                    ])),
            stderr="",
        ),
    )
    monkeypatch.setattr(deploy_safety.subprocess, "Popen", lambda args, **_kwargs: (events.append(args) or FakeProcess()))
    monkeypatch.setattr(deploy_safety.select, "select", lambda readers, _w, _e, _t: (readers, [], []))

    gate = deploy_safety._start_database_gate(tmp_path, "container-id")
    assert isinstance(gate, deploy_safety.DatabaseGate)
    args = events[0]
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--user") + 1] == "1000:1000"
    assert args[args.index("--mount") + 1] == "type=volume,source=project_data,target=/app/data"
    assert "--volumes-from" not in args


@pytest.mark.parametrize(
    "mounts",
    [
        [{"Destination": "/app/data", "Type": "bind", "Source": "/srv/private-data"}],
        [],
    ],
)
def test_database_mount_must_be_an_inspectable_named_volume(monkeypatch, mounts):
    monkeypatch.setattr(
        deploy_safety, "_run_docker",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, json.dumps(mounts), "",
        ),
    )
    with pytest.raises(deploy_safety.DeploySafetyError, match="named volume"):
        deploy_safety._data_volume_name("container-id")


def test_failed_stop_releases_gate_and_does_not_start_container(tmp_path):
    events = []
    gate_released = []

    class FakeGate:
        def release(self):
            gate_released.append(True)

    def service_state(_project_dir):
        return True, "container-id"

    def run_compose(_project_dir, args, **_kwargs):
        events.append(tuple(args))
        return None

    with pytest.raises(deploy_safety.DeploySafetyError, match="did not stop"):
        deploy_safety._run_compose_restart(
            tmp_path, up_all=False, compose_down=False,
            run_compose=run_compose, service_state=service_state,
            acquire_gate=lambda *_args: FakeGate(),
        )
    assert len(gate_released) == 1
    assert not any(command[0] == "up" for command in events)


def test_stopped_app_is_fail_closed_without_starting_or_downloading(tmp_path):
    events = []

    with pytest.raises(deploy_safety.DeploySafetyError, match="not running"):
        deploy_safety._run_compose_restart(
            tmp_path, up_all=False, compose_down=False,
            run_compose=lambda _path, args, **_kwargs: events.append(tuple(args)),
            service_state=lambda _path: (False, "container-id"),
            acquire_gate=lambda *_args: pytest.fail("gate must not run for stopped app"),
        )
    assert events == []


def test_absent_app_container_is_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        deploy_safety, "_run_compose",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    with pytest.raises(deploy_safety.DeploySafetyError, match="absent"):
        deploy_safety._service_container_state(tmp_path)


def test_cli_does_not_echo_subprocess_error_details(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        deploy_safety, "deploy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["docker", "--private-path", "/hidden/token"])
        ),
    )
    assert deploy_safety.main(["--project-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().err
    assert "Docker Compose command failed" in output
    assert "/hidden/token" not in output
