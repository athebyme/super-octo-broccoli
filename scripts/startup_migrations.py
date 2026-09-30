#!/usr/bin/env python3
"""Run the verified Docker migration bundle once per code and schema revision.

The journal records a successful *whole* migration run, never an attempted one.
Normal application DML does not invalidate it; changed migration code, changed
schema, missing state or an interrupted run requires the full migration path.
Direct migration commands retain their independent validation and repair role.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


JOURNAL_TABLE = "seller_hub_startup_migrations"
FORMAT_VERSION = 1


class StartupMigrationError(RuntimeError):
    pass


def bundle_digest(project_root: Path) -> str:
    files = {
        project_root / "docker-entrypoint.sh",
        project_root / "seller_platform.py",
        project_root / "models.py",
        project_root / "requirements.txt",
        project_root / "scripts" / "startup_migrations.py",
    }
    # Include runtime helpers imported by migrations as well as migration
    # modules themselves. A changed dependency must not leave a stale success.
    for directory in ("migrations", "services", "routes", "scripts", "agents"):
        files.update(project_root.joinpath(directory).rglob("*.py"))
    digest = hashlib.sha256(str(FORMAT_VERSION).encode("ascii"))
    for path in sorted(files):
        digest.update(str(path.relative_to(project_root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def schema_digest(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE tbl_name != ? ORDER BY type, name",
        (JOURNAL_TABLE,),
    ).fetchall()
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def is_current(database_path: Path, expected_bundle: str) -> bool:
    if not database_path.exists():
        return False
    connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    try:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (JOURNAL_TABLE,),
        ).fetchone():
            return False
        row = connection.execute(
            f"SELECT bundle_digest, schema_digest FROM {JOURNAL_TABLE} "
            "WHERE id=1"
        ).fetchone()
        return bool(
            row and row[0] == expected_bundle
            and row[1] == schema_digest(connection)
        )
    finally:
        connection.close()


def record_success(database_path: Path, expected_bundle: str) -> None:
    # The child must have created/migrated this exact database. mode=rw prevents
    # an erroneous path from silently becoming a second, empty database.
    connection = sqlite3.connect(
        database_path.as_uri() + "?mode=rw", uri=True, timeout=30,
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS {JOURNAL_TABLE} (
                id INTEGER PRIMARY KEY CHECK (id=1),
                bundle_digest TEXT NOT NULL,
                schema_digest TEXT NOT NULL,
                completed_at TEXT NOT NULL
            )
        """)
        connection.execute(
            f"INSERT INTO {JOURNAL_TABLE} "
            "(id, bundle_digest, schema_digest, completed_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET bundle_digest=excluded.bundle_digest, "
            "schema_digest=excluded.schema_digest, completed_at=excluded.completed_at",
            (
                expected_bundle, schema_digest(connection),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def startup_lock(database_path: Path):
    lock_path = database_path.with_name(database_path.name + ".startup.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StartupMigrationError("Another database startup is in progress") from exc
        yield
    finally:
        os.close(descriptor)


def run(database_path: Path, project_root: Path, command: list[str]) -> bool:
    database_path = database_path.resolve()
    project_root = project_root.resolve()
    with startup_lock(database_path):
        expected_bundle = bundle_digest(project_root)
        if is_current(database_path, expected_bundle):
            print("Database startup: verified migration bundle and schema are current", flush=True)
            return False
        print("Database startup: running migration bundle", flush=True)
        subprocess.run(command, cwd=project_root, check=True)
        if bundle_digest(project_root) != expected_bundle:
            raise StartupMigrationError("Migration code changed during startup; retry required")
        record_success(database_path, expected_bundle)
        print("Database startup: successful migration bundle recorded", flush=True)
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-path", type=Path, required=True)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parent.parent
    try:
        run(
            args.database_path, project_root,
            [str(project_root / "docker-entrypoint.sh"), "--run-database-migrations"],
        )
    except subprocess.CalledProcessError as exc:
        print("Database startup: migration failed; success was not recorded", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (StartupMigrationError, OSError, sqlite3.Error) as exc:
        print(f"Database startup failed ({type(exc).__name__}); success was not recorded", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
