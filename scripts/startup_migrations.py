#!/usr/bin/env python3
"""Apply only pending, source-verified startup migration steps."""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from scripts.startup_migration_steps import MigrationStep, migration_steps

JOURNAL_TABLE = "seller_hub_startup_migrations"
STEP_TABLE = "seller_hub_startup_migration_steps"
RUN_TABLE = "seller_hub_startup_migration_runs"
JOURNAL_TABLES = (JOURNAL_TABLE, STEP_TABLE, RUN_TABLE)
FORMAT_VERSION = 2
LEGACY_BUNDLE_DIGEST = "8bb293a89bff925150aa6e883cfb10181594f23f911e0b5bc5ad4f359cea6bbb"
LEGACY_SCHEMA_DIGEST = "25c975bf6724f99e3046763c4b9b6faa235cecf63345ca5891141363f24b6ae1"
LEGACY_COMPLETED_AT = "2026-09-30T18:06:49.653129+00:00"
LEGACY_SOURCE_MANIFEST_AGGREGATE = "52c23165f72335628b20be3db3287d5a5be0a7f6a2b62a37c129b55c8f4c5a94"
LEGACY_RECEIPT = "scripts/startup_migration_legacy_receipt.json"
# Root pins this digest to the reviewed compatibility object in the receipt.
# There is deliberately no environment override.
LEGACY_COMPATIBILITY_SHA256 = "ed5eb1349fa6c94b5e9ccc0047b89ee0b2d02e269f72bfb5d7ff49394379746e"
LEGACY_BRIDGE_FILES = ("docker-entrypoint.sh", "migrations/run_scoped_batch.py")
NEW_GUARD_FILES = (
    "scripts/startup_migration_steps.py",
    "scripts/startup_migration_bootstrap.py",
)
LEGACY_COMPATIBILITY_FORMAT = 1
EXPECTED_ENTRYPOINT_COMMAND = (
    "python scripts/startup_migrations.py --database-path /app/data/seller_platform.db"
)


class StartupMigrationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_digest(value) -> str:
    return _sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                              separators=(",", ":")).encode("ascii"))


def seller_platform_migration_signature(path: Path) -> str:
    """Hash only the application/bootstrap contract migrations actually use.

    The module registers routes and UI handlers at import time. Those runtime
    registrations must not invalidate migration receipts, so this signature
    includes the database URL/config, storage setup, and migration helper only.
    """
    return _seller_platform_signature(str(path), path.read_text(encoding="utf-8"))


@lru_cache(maxsize=64)
def _seller_platform_signature(path_name: str, source: str) -> str:
    path = Path(path_name)
    tree = ast.parse(source, filename=str(path))
    selected = []
    database_names = {
        "BASE_DIR", "DATA_ROOT", "DEFAULT_DB_PATH", "database_url_from_env",
        "database_url",
    }

    def assigns_database_name(node: ast.Assign | ast.AnnAssign) -> bool:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return any(
            isinstance(target, ast.Name) and target.id in database_names
            for target in targets
        )

    def is_db_init_app(node: ast.AST) -> bool:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            return False
        call = node.value
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "init_app"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "db"
            and any(isinstance(arg, ast.Name) and arg.id == "app" for arg in call.args)
        )

    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "_run_startup_migrations", "ensure_storage_roots",
        }:
            selected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.ImportFrom) and node.module == "models":
            if any(alias.name == "db" for alias in node.names):
                selected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.Import):
            if any(alias.name == "os" for alias in node.names):
                selected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.ImportFrom):
            if node.module == "pathlib" and any(alias.name == "Path" for alias in node.names):
                selected.append(ast.dump(node, include_attributes=False))
            elif node.module == "flask" and any(alias.name == "Flask" for alias in node.names):
                selected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)):
            dump = ast.dump(node, include_attributes=False)
            if "SQLALCHEMY_DATABASE_URI" in dump or "SQLALCHEMY_TRACK_MODIFICATIONS" in dump:
                selected.append(dump)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and assigns_database_name(node):
                selected.append(dump)
            elif is_db_init_app(node):
                selected.append(dump)
            elif isinstance(node, ast.Assign):
                targets = node.targets
                if any(isinstance(target, ast.Name) and target.id == "app" for target in targets):
                    value = node.value
                    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "Flask":
                        selected.append(dump)
        elif isinstance(node, ast.If):
            if any(
                isinstance(child, (ast.Assign, ast.AnnAssign))
                and assigns_database_name(child)
                for child in ast.walk(node)
            ):
                selected.append(ast.dump(node, include_attributes=False))
    if not selected:
        raise StartupMigrationError("seller_platform migration contract could not be identified")
    return _json_digest(selected)


def models_schema_signature(path: Path) -> str:
    """Fingerprint SQLAlchemy table declarations, not unrelated model methods."""
    return _models_schema_signature(str(path), path.read_text(encoding="utf-8"))


@lru_cache(maxsize=64)
def _models_schema_signature(path_name: str, source: str) -> str:
    path = Path(path_name)
    tree = ast.parse(source, filename=str(path))
    selected = []

    def is_schema_call(node: ast.AST) -> bool:
        text = ast.dump(node, include_attributes=False)
        return "Column" in text or "__table_args__" in text

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "flask_sqlalchemy":
            if any(alias.name == "SQLAlchemy" for alias in node.names):
                selected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.Assign):
            target_names = {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
            value_dump = ast.dump(node.value, include_attributes=False)
            if "db" in target_names and "SQLAlchemy" in value_dump:
                selected.append(ast.dump(node, include_attributes=False))
            elif "Table" in value_dump and "db" in value_dump:
                selected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.ClassDef):
            if not any(
                isinstance(base, ast.Attribute)
                and isinstance(base.value, ast.Name)
                and base.value.id == "db" and base.attr == "Model"
                for base in node.bases
            ):
                continue
            selected.append([ast.dump(base, include_attributes=False) for base in node.bases])
            for member in node.body:
                if not isinstance(member, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = member.targets if isinstance(member, ast.Assign) else [member.target]
                names = {
                    target.id for target in targets if isinstance(target, ast.Name)
                }
                value = member.value
                if names & {"__tablename__", "__table_args__", "__bind_key__", "__table__"} or is_schema_call(value):
                    selected.append(ast.dump(member, include_attributes=False))
    if not selected:
        raise StartupMigrationError("SQLAlchemy model schema declarations could not be identified")
    return _json_digest(selected)


def _module_file(project_root: Path, module: str) -> Path | None:
    top = module.split(".", 1)[0]
    if top == "models":
        candidates = [project_root / "models.py"]
    elif top == "seller_platform":
        candidates = [project_root / "seller_platform.py"]
    elif top in {"migrations", "services"}:
        relative = Path(*module.split("."))
        candidates = [project_root / relative.with_suffix(".py"), project_root / relative / "__init__.py"]
    else:
        return None
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _imports_for(path: Path, project_root: Path) -> set[Path]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, SyntaxError) as exc:
        raise StartupMigrationError(f"Cannot inspect migration dependency {path.name}") from exc
    return {
        Path(item) for item in _cached_imports(str(path), str(project_root), source)
    }


@lru_cache(maxsize=512)
def _cached_imports(path_name: str, project_root_name: str, source: str) -> tuple[str, ...]:
    path = Path(path_name)
    project_root = Path(project_root_name)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise StartupMigrationError(f"Cannot inspect migration dependency {path.name}") from exc
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return set()
    parts = relative.with_suffix("").parts
    package = parts[:-1]
    found: set[Path] = set()

    def add_module(module: str) -> None:
        resolved = _module_file(project_root, module)
        if resolved is not None:
            found.add(resolved)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package[:max(0, len(package) - node.level + 1)]
                if node.module:
                    base_parts += tuple(node.module.split("."))
                base_module = ".".join(base_parts)
            else:
                base_module = node.module or ""
            if base_module:
                add_module(base_module)
            for alias in node.names:
                candidate = f"{base_module}.{alias.name}" if base_module else alias.name
                add_module(candidate)
    return tuple(sorted(str(item) for item in found))


def source_files_for_step(step: MigrationStep, project_root: Path) -> tuple[Path, ...]:
    project_root = project_root.resolve()
    starts = [step.script] if step.script else []
    starts.extend(step.dependencies)
    if step.kind == "scoped":
        starts.append("migrations/run_scoped_batch.py")
    paths: set[Path] = set()
    queue: list[Path] = []
    for relative in starts:
        path = (project_root / relative).resolve()
        if project_root not in path.parents and path != project_root:
            raise StartupMigrationError("Migration dependency escapes project root")
        if not path.is_file():
            raise StartupMigrationError(f"Missing migration dependency: {relative}")
        if path not in paths:
            paths.add(path)
            queue.append(path)
    visited: set[Path] = set()
    while queue:
        path = queue.pop()
        if path in visited:
            continue
        visited.add(path)
        # App registration and methods on ORM models pull in runtime-only
        # routes/services. Their migration signatures are the narrow AST slices
        # above; neither module's implementation imports are schema dependencies.
        if path in {project_root / "seller_platform.py", project_root / "models.py"}:
            continue
        for dependency in _imports_for(path, project_root):
            if dependency not in paths:
                paths.add(dependency)
                # Application initialization is fingerprinted narrowly; do not
                # recursively hash its route/service registry.
                if dependency.parent.name in {"migrations", "services"}:
                    queue.append(dependency)
    return tuple(sorted(paths, key=lambda item: item.relative_to(project_root).as_posix()))


def _source_hash(path: Path, project_root: Path) -> str:
    if path == project_root / "seller_platform.py":
        return seller_platform_migration_signature(path)
    if path == project_root / "models.py":
        return models_schema_signature(path)
    if path == project_root / "migrations/run_all_migrations.py":
        return run_all_base_only_signature(path)
    return _sha256(path.read_bytes())


def run_all_base_only_signature(path: Path) -> str:
    """Fingerprint only the command path used with ``--base-only``.

    ``run_all_migrations.py`` also contains a legacy dynamic child-migration
    chain. The entrypoint always passes ``--base-only`` and runs those child
    migrations as separately journaled steps, so edits to that unreachable
    branch must not rerun its broad base DML migration.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "migrate", "get_existing_columns", "add_column_if_missing",
        }:
            selected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            selected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.FunctionDef) and node.name == "main":
            class OmitNonBaseOnlyChildren(ast.NodeTransformer):
                @staticmethod
                def is_exact_non_base_only_guard(test: ast.AST) -> bool:
                    if not isinstance(test, ast.BoolOp) or not isinstance(test.op, ast.And) or len(test.values) != 2:
                        return False
                    success, base_only = test.values
                    return (
                        isinstance(success, ast.Name) and success.id == "success"
                        and isinstance(base_only, ast.UnaryOp) and isinstance(base_only.op, ast.Not)
                        and isinstance(base_only.operand, ast.Attribute)
                        and base_only.operand.attr == "base_only"
                        and isinstance(base_only.operand.value, ast.Name)
                        and base_only.operand.value.id == "args"
                    )

                def visit_If(self, branch):
                    if self.is_exact_non_base_only_guard(branch.test):
                        # With --base-only, the guard is false. Preserve and
                        # fingerprint an else/elif branch if one is added.
                        return [self.visit(statement) for statement in branch.orelse]
                    return self.generic_visit(branch)

            selected.append(ast.dump(OmitNonBaseOnlyChildren().visit(node), include_attributes=False))
    if not selected:
        raise StartupMigrationError("run_all base-only migration path could not be identified")
    return _json_digest(selected)


def step_fingerprint(step: MigrationStep, project_root: Path, fingerprints=None) -> str:
    dependencies = {
        path.relative_to(project_root.resolve()).as_posix(): _source_hash(path, project_root.resolve())
        for path in source_files_for_step(step, project_root)
    }
    descriptor = {
        "format": FORMAT_VERSION,
        "key": step.key,
        "kind": step.kind,
        "script": step.script,
        "args": list(step.args),
        "dependencies": list(step.dependencies),
        "depends_on": list(step.depends_on),
        "batch_group": step.batch_group,
        "sources": dependencies,
        "upstream": {key: fingerprints[key] for key in step.depends_on} if fingerprints else {},
    }
    return _json_digest(descriptor)


def migration_fingerprints(project_root: Path, steps: tuple[MigrationStep, ...] | None = None) -> dict[str, str]:
    steps = steps or migration_steps()
    result: dict[str, str] = {}
    for step in steps:
        result[step.key] = step_fingerprint(step, project_root, result)
    return result


def bundle_digest(project_root: Path, steps: tuple[MigrationStep, ...] | None = None,
                  *, fingerprints: dict[str, str] | None = None) -> str:
    steps = steps or migration_steps()
    fingerprints = fingerprints or migration_fingerprints(project_root, steps)
    ordered = [{"key": step.key, "fingerprint": fingerprints[step.key]} for step in steps]
    return _json_digest({"format": FORMAT_VERSION, "steps": ordered})


def schema_digest(connection: sqlite3.Connection) -> str:
    placeholders = ",".join("?" for _ in JOURNAL_TABLES)
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        f"WHERE name NOT IN ({placeholders}) AND tbl_name NOT IN ({placeholders}) "
        "ORDER BY type, name",
        (*JOURNAL_TABLES, *JOURNAL_TABLES),
    ).fetchall()
    # Migration callbacks may deliberately set sqlite3.Row for their queries.
    # Normalize only the digest input; do not mutate the shared connection's
    # row_factory, which belongs to the migration and its following steps.
    return _json_digest([tuple(row) for row in rows])


def _legacy_schema_digest(connection: sqlite3.Connection) -> str:
    """Schema algorithm used by the certified whole-bundle guard."""
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE tbl_name != ? ORDER BY type, name",
        (JOURNAL_TABLE,),
    ).fetchall()
    return _json_digest([tuple(row) for row in rows])


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


def _connect_rw(database_path: Path) -> sqlite3.Connection:
    if not database_path.exists():
        database_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(database_path).close()
    return sqlite3.connect(database_path.as_uri() + "?mode=rw", uri=True, timeout=30)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone())


def _ensure_journal_tables(connection: sqlite3.Connection) -> None:
    present = {name for name in JOURNAL_TABLES if _table_exists(connection, name)}
    if present and present not in ({JOURNAL_TABLE}, set(JOURNAL_TABLES)):
        raise StartupMigrationError("Startup migration journal is incomplete; manual review required")
    connection.executescript(f"""
        CREATE TABLE IF NOT EXISTS {JOURNAL_TABLE} (
            id INTEGER PRIMARY KEY CHECK (id=1),
            bundle_digest TEXT NOT NULL,
            schema_digest TEXT NOT NULL,
            completed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {STEP_TABLE} (
            step_key TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running','completed')),
            schema_before TEXT NOT NULL,
            schema_after TEXT,
            origin TEXT NOT NULL DEFAULT 'executed',
            started_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS {RUN_TABLE} (
            id INTEGER PRIMARY KEY CHECK (id=1),
            plan_digest TEXT NOT NULL,
            baseline_schema TEXT NOT NULL,
            required_steps TEXT NOT NULL,
            current_step TEXT,
            started_at TEXT NOT NULL
        );
    """)
    connection.commit()
    _validate_journal_tables(connection)


def _normalized_sql(sql: str | None) -> str:
    if not sql:
        return ""
    return "".join(sql.lower().replace('"', "").replace("`", "").split())


def _validate_journal_tables(connection: sqlite3.Connection) -> None:
    expected = {
        JOURNAL_TABLE: (
            "id INTEGER PRIMARY KEY CHECK (id=1),"
            "bundle_digest TEXT NOT NULL, schema_digest TEXT NOT NULL, completed_at TEXT NOT NULL"
        ),
        STEP_TABLE: (
            "step_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, ordinal INTEGER NOT NULL,"
            "status TEXT NOT NULL CHECK (status IN ('running','completed')),"
            "schema_before TEXT NOT NULL, schema_after TEXT, origin TEXT NOT NULL DEFAULT 'executed',"
            "started_at TEXT NOT NULL, completed_at TEXT"
        ),
        RUN_TABLE: (
            "id INTEGER PRIMARY KEY CHECK (id=1), plan_digest TEXT NOT NULL,"
            "baseline_schema TEXT NOT NULL, required_steps TEXT NOT NULL, current_step TEXT, started_at TEXT NOT NULL"
        ),
    }
    for name, body in expected.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,),
        ).fetchone()
        expected_sql = _normalized_sql(f"CREATE TABLE {name} ({body})")
        if not row or _normalized_sql(row[0]) != expected_sql:
            raise StartupMigrationError("Startup migration journal schema is invalid; manual review required")
        if connection.execute(f"PRAGMA foreign_key_list({name})").fetchall():
            raise StartupMigrationError("Startup migration journal must not have foreign keys")
        extra = connection.execute(
            "SELECT type FROM sqlite_master WHERE tbl_name=? AND type IN ('trigger','index') "
            "AND name NOT LIKE 'sqlite_autoindex_%'", (name,),
        ).fetchall()
        if extra:
            raise StartupMigrationError("Startup migration journal has unexpected triggers or indexes")


def _read_whole(connection: sqlite3.Connection):
    if not _table_exists(connection, JOURNAL_TABLE):
        return None
    return connection.execute(
        f"SELECT bundle_digest, schema_digest, completed_at FROM {JOURNAL_TABLE} WHERE id=1"
    ).fetchone()


def _read_step_rows(connection: sqlite3.Connection) -> dict[str, tuple]:
    if not _table_exists(connection, STEP_TABLE):
        return {}
    return {
        row[0]: row[1:]
        for row in connection.execute(
            f"SELECT step_key, fingerprint, ordinal, status, schema_before, schema_after, origin, started_at, completed_at FROM {STEP_TABLE}"
        )
    }


def _read_run(connection: sqlite3.Connection):
    if not _table_exists(connection, RUN_TABLE):
        return None
    return connection.execute(
        f"SELECT plan_digest, baseline_schema, required_steps, current_step, started_at FROM {RUN_TABLE} WHERE id=1"
    ).fetchone()


def _receipt(project_root: Path) -> dict:
    path = project_root / LEGACY_RECEIPT
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StartupMigrationError("Certified legacy startup receipt is missing or invalid") from exc
    expected = {
        "legacy_bundle_digest": LEGACY_BUNDLE_DIGEST,
        "legacy_schema_digest": LEGACY_SCHEMA_DIGEST,
        "completed_at": LEGACY_COMPLETED_AT,
        "source_manifest_aggregate_sha256": LEGACY_SOURCE_MANIFEST_AGGREGATE,
    }
    if data.get("format_version") != 1 or any(data.get(k) != v for k, v in expected.items()):
        raise StartupMigrationError("Certified legacy startup receipt metadata mismatch")
    if not LEGACY_COMPATIBILITY_SHA256 or data.get("compatibility_sha256") != LEGACY_COMPATIBILITY_SHA256:
        raise StartupMigrationError("Certified legacy startup compatibility pin mismatch")
    return data


def _entrypoint_migration_signature(project_root: Path) -> str:
    path = project_root / "docker-entrypoint.sh"
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except OSError as exc:
        raise StartupMigrationError("Docker entrypoint migration command cannot be verified") from exc
    commands = [line for line in lines if "scripts/startup_migrations.py" in line]
    if commands != [EXPECTED_ENTRYPOINT_COMMAND]:
        raise StartupMigrationError("Docker entrypoint migration command changed; review required")
    return _json_digest(commands)


def _legacy_compatibility_payload(project_root: Path, steps: tuple[MigrationStep, ...],
                                 fingerprints: dict[str, str], plan_digest: str,
                                 receipt: dict) -> dict:
    old_files = receipt.get("files")
    if not isinstance(old_files, dict):
        raise StartupMigrationError("Certified legacy source manifest is missing")
    root = project_root.resolve()
    relevant = {"docker-entrypoint.sh"}
    for step in steps:
        relevant.update(
            path.relative_to(root).as_posix()
            for path in source_files_for_step(step, root)
        )
    bridge = set(LEGACY_BRIDGE_FILES)
    old_source_hashes = {}
    for relative in sorted(relevant):
        if relative not in old_files:
            if relative not in NEW_GUARD_FILES:
                raise StartupMigrationError(f"Legacy receipt does not certify source file {relative}")
            continue
        current = _sha256((root / relative).read_bytes())
        if relative not in bridge and current != old_files[relative]:
            raise StartupMigrationError(f"Legacy migration source changed: {relative}")
        old_source_hashes[relative] = old_files[relative]
    current_bridges = {
        relative: _sha256((root / relative).read_bytes())
        for relative in sorted(bridge)
    }
    new_guard_hashes = {}
    for relative in NEW_GUARD_FILES:
        path = root / relative
        if not path.is_file():
            raise StartupMigrationError(f"Missing startup migration guard file: {relative}")
        new_guard_hashes[relative] = _sha256(path.read_bytes())
    legacy_entrypoint_hash = old_files.get("docker-entrypoint.sh")
    if not legacy_entrypoint_hash:
        raise StartupMigrationError("Certified legacy entrypoint source is missing")
    return {
        "compatibility_format": LEGACY_COMPATIBILITY_FORMAT,
        "plan_digest": plan_digest,
        "step_fingerprints": fingerprints,
        "plan_source_sha256": _sha256((root / "scripts/startup_migration_steps.py").read_bytes()),
        "entrypoint_migration_signature": _entrypoint_migration_signature(root),
        "legacy_entrypoint": {
            "source": "docker-entrypoint.sh",
            "sha256": legacy_entrypoint_hash,
            "transfer_basis": "successful whole-bundle run including inline bootstrap",
        },
        "legacy_source_hashes": old_source_hashes,
        "current_bridge_hashes": current_bridges,
        "new_guard_hashes": new_guard_hashes,
    }


def _compatibility_digest(payload: dict) -> str:
    return _json_digest(payload)


def _certified_legacy_bootstrap(connection: sqlite3.Connection, project_root: Path,
                                steps: tuple[MigrationStep, ...], fingerprints: dict[str, str],
                                plan_digest: str) -> bool:
    if _table_exists(connection, STEP_TABLE):
        return False
    whole = _read_whole(connection)
    if whole is None:
        return False
    legacy_bundle, stored_schema, completed_at = whole
    if legacy_bundle != LEGACY_BUNDLE_DIGEST:
        raise StartupMigrationError("Legacy success is not from the certified migration source")
    if (stored_schema != LEGACY_SCHEMA_DIGEST or completed_at != LEGACY_COMPLETED_AT
            or _legacy_schema_digest(connection) != stored_schema):
        raise StartupMigrationError("Certified legacy success does not match the live SQLite schema")
    receipt = _receipt(project_root)
    compatible = receipt.get("compatibility", {})
    current_compatibility = _legacy_compatibility_payload(
        project_root, steps, fingerprints, plan_digest, receipt,
    )
    compatibility_digest = _compatibility_digest(current_compatibility)
    if (compatible != current_compatibility
            or compatibility_digest != LEGACY_COMPATIBILITY_SHA256
            or receipt.get("compatibility_sha256") != compatibility_digest):
        raise StartupMigrationError("Certified legacy migration steps do not match this release")
    _ensure_journal_tables(connection)
    current_schema = schema_digest(connection)
    now = completed_at
    for ordinal, step in enumerate(steps):
        connection.execute(
            f"INSERT INTO {STEP_TABLE} "
            "(step_key,fingerprint,ordinal,status,schema_before,schema_after,origin,started_at,completed_at) "
            "VALUES (?,?,?,'completed',?,?,?, ?,?)",
            (step.key, fingerprints[step.key], ordinal, current_schema, current_schema,
             "certified-legacy:" + LEGACY_BUNDLE_DIGEST, now, now),
        )
    connection.commit()
    # This is a transfer of the exact prior successful whole-bundle receipt,
    # certified by source and schema. No migration is claimed beyond that receipt.
    _record_success(connection, plan_digest, current_schema)
    return True


def _record_success(connection: sqlite3.Connection, plan_digest: str, digest: str) -> None:
    connection.execute(
        f"INSERT INTO {JOURNAL_TABLE} (id,bundle_digest,schema_digest,completed_at) VALUES (1,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET bundle_digest=excluded.bundle_digest, "
        "schema_digest=excluded.schema_digest, completed_at=excluded.completed_at",
        (plan_digest, digest, _utc_now()),
    )
    connection.execute(f"DELETE FROM {RUN_TABLE} WHERE id=1")
    connection.commit()


def _record_running(connection: sqlite3.Connection, plan_digest: str, baseline: str,
                    required: list[str], current_step: str | None = None) -> None:
    connection.execute(
        f"INSERT INTO {RUN_TABLE} (id,plan_digest,baseline_schema,required_steps,current_step,started_at) "
        "VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET current_step=excluded.current_step",
        (plan_digest, baseline, json.dumps(required, separators=(",", ":")), current_step, _utc_now()),
    )
    connection.commit()


def _mark_step_running(connection: sqlite3.Connection, step: MigrationStep, fingerprint: str,
                       ordinal: int, current_schema: str) -> None:
    now = _utc_now()
    connection.execute(
        f"INSERT INTO {STEP_TABLE} "
        "(step_key,fingerprint,ordinal,status,schema_before,schema_after,origin,started_at,completed_at) "
        "VALUES (?,?,?,'running',?,NULL,'executed',?,NULL) "
        "ON CONFLICT(step_key) DO UPDATE SET fingerprint=excluded.fingerprint, ordinal=excluded.ordinal, "
        "status='running', schema_before=excluded.schema_before, schema_after=NULL, "
        "origin='executed', started_at=excluded.started_at, completed_at=NULL",
        (step.key, fingerprint, ordinal, current_schema, now),
    )
    _record_running(connection, _active_plan(connection), _active_baseline(connection),
                    _active_required(connection), step.key)


def _mark_step_completed(connection: sqlite3.Connection, step: MigrationStep, fingerprint: str,
                         ordinal: int) -> None:
    current_schema = schema_digest(connection)
    connection.execute(
        f"UPDATE {STEP_TABLE} SET status='completed',schema_after=?,completed_at=? WHERE step_key=? AND fingerprint=?",
        (current_schema, _utc_now(), step.key, fingerprint),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise StartupMigrationError(f"Migration step {step.key} lost its running receipt")
    connection.execute(f"UPDATE {RUN_TABLE} SET current_step=NULL WHERE id=1")
    connection.commit()


def _active_plan(connection: sqlite3.Connection) -> str:
    row = _read_run(connection)
    if row is None:
        raise StartupMigrationError("Migration run receipt is missing")
    return row[0]


def _active_baseline(connection: sqlite3.Connection) -> str:
    row = _read_run(connection)
    if row is None:
        raise StartupMigrationError("Migration run receipt is missing")
    return row[1]


def _active_required(connection: sqlite3.Connection) -> list[str]:
    row = _read_run(connection)
    if row is None:
        raise StartupMigrationError("Migration run receipt is missing")
    return json.loads(row[2])


def _execute_step(step: MigrationStep, project_root: Path, database_path: Path) -> None:
    if step.kind == "bootstrap":
        command = [sys.executable, str(project_root / step.script), str(database_path)]
    elif step.kind == "scoped":
        command = [sys.executable, "-m", "migrations.run_scoped_batch", str(database_path), step.script]
    else:
        args = [str(database_path) if arg == "{database}" else arg for arg in step.args]
        command = [sys.executable, str(project_root / step.script), *args]
    environment = os.environ.copy()
    environment["SKIP_SCHEDULER"] = "1"
    environment["PYTHONPATH"] = str(project_root) + os.pathsep + environment.get("PYTHONPATH", "")
    environment["DATABASE_URL"] = f"sqlite:///{database_path}"
    subprocess.run(command, cwd=project_root, env=environment, check=True)


def _validate_order(steps: tuple[MigrationStep, ...], step_rows: dict[str, tuple]) -> None:
    ordinals = {step.key: index for index, step in enumerate(steps)}
    unknown = set(step_rows) - set(ordinals)
    if unknown:
        raise StartupMigrationError("A previously completed migration step was removed; manual review required")
    for key, row in step_rows.items():
        if row[1] != ordinals[key]:
            raise StartupMigrationError("Migration step order changed; manual review required")


def _stored_steps_bundle_digest(step_rows: dict[str, tuple]) -> str:
    ordered = sorted(step_rows.items(), key=lambda pair: pair[1][1])
    if any(row[2] != "completed" or row[4] is None or row[7] is None for _, row in ordered):
        raise StartupMigrationError("Completed startup receipt contains an incomplete step")
    return _json_digest({
        "format": FORMAT_VERSION,
        "steps": [
            {"key": key, "fingerprint": row[0]}
            for key, row in ordered
        ],
    })


def _needs(steps: tuple[MigrationStep, ...], step_rows: dict[str, tuple], fingerprints: dict[str, str]) -> list[str]:
    required = {
        step.key for step in steps
        if (step.key not in step_rows or step_rows[step.key][0] != fingerprints[step.key]
            or step_rows[step.key][2] != "completed")
    }
    changed = True
    while changed:
        changed = False
        for step in steps:
            if step.key not in required and any(dep in required for dep in step.depends_on):
                required.add(step.key)
                changed = True
    return [step.key for step in steps if step.key in required]


def _execute_scoped_group(connection: sqlite3.Connection, project_root: Path, database_path: Path,
                          group_steps: list[MigrationStep], required: set[str], fingerprints: dict[str, str],
                          step_rows: dict[str, tuple], ordinals: dict[str, int]) -> None:
    from migrations.run_scoped_batch import run_batch

    pending = []
    for step in group_steps:
        row = step_rows.get(step.key)
        if step.key in required and not (row and row[0] == fingerprints[step.key] and row[2] == "completed"):
            pending.append(step)
    if not pending:
        return
    pending_keys = {step.script: step for step in pending}
    all_scripts = [step.script for step in group_steps]
    selected = {step.script for step in pending}

    def before(_migration_connection, script):
        step = pending_keys[script]
        if connection is not _migration_connection:
            raise StartupMigrationError("Migration journal and scoped runner must share one connection")
        _assert_fingerprints(project_root, group_steps, fingerprints, keys={step.key})
        _mark_step_running(connection, step, fingerprints[step.key], ordinals[step.key], schema_digest(connection))

    def after(_migration_connection, script):
        step = pending_keys[script]
        if connection is not _migration_connection:
            raise StartupMigrationError("Migration journal and scoped runner must share one connection")
        _assert_fingerprints(project_root, group_steps, fingerprints, keys={step.key})
        _mark_step_completed(connection, step, fingerprints[step.key], ordinals[step.key])

    run_batch(
        database_path, all_scripts, verbose=True, skip_scripts=set(all_scripts) - selected,
        before_step=before, after_step=after, connection=connection,
    )


def _assert_fingerprints(project_root: Path, steps: tuple[MigrationStep, ...] | list[MigrationStep],
                         expected: dict[str, str], *, keys: set[str] | None = None) -> None:
    for step in steps:
        if keys is not None and step.key not in keys:
            continue
        try:
            actual = step_fingerprint(step, project_root, expected)
        except StartupMigrationError as exc:
            raise StartupMigrationError(f"Migration source changed during startup: {step.key}") from exc
        if actual != expected[step.key]:
            raise StartupMigrationError(f"Migration source changed during startup: {step.key}")


def _validate_interrupted_checkpoint(connection: sqlite3.Connection, active: tuple,
                                     step_rows: dict[str, tuple], current_schema: str,
                                     fingerprints: dict[str, str], ordinals: dict[str, int]) -> None:
    """Reuse completed steps only when SQLite still matches their last checkpoint."""
    current_step = active[3]
    running = [(key, row) for key, row in step_rows.items() if row[2] == "running"]
    if current_step:
        if len(running) != 1 or running[0][0] != current_step:
            raise StartupMigrationError("Interrupted migration journal has inconsistent running steps")
        row = step_rows[current_step]
        if row[0] != fingerprints.get(current_step) or row[1] != ordinals.get(current_step):
            raise StartupMigrationError("Interrupted migration step changed; manual recovery required")
        # A failed step may be retried only when it left no unjournaled schema
        # change. Otherwise the database state cannot be attributed safely.
        if current_schema != row[3]:
            raise StartupMigrationError("SQLite schema changed during an interrupted step; manual recovery required")
        return
    if running:
        raise StartupMigrationError("Interrupted migration step is missing from the run journal")
    completed = [row for row in step_rows.values() if row[2] == "completed" and row[4]]
    expected_schema = max(completed, key=lambda row: row[7])[4] if completed else active[1]
    if current_schema != expected_schema:
        raise StartupMigrationError("SQLite schema drift during an interrupted run; manual recovery required")


def run(database_path: Path, project_root: Path, *, executor=None,
        steps: tuple[MigrationStep, ...] | None = None,
        legacy_receipt_path: Path | None = None) -> bool:
    database_path = Path(database_path).resolve()
    project_root = Path(project_root).resolve()
    steps = steps or migration_steps()
    if legacy_receipt_path is not None:
        global LEGACY_RECEIPT
        LEGACY_RECEIPT = str(Path(legacy_receipt_path).resolve().relative_to(project_root))

    keys = [step.key for step in steps]
    if not steps or len(keys) != len(set(keys)):
        raise StartupMigrationError("Migration plan must contain unique ordered steps")
    _entrypoint_migration_signature(project_root)
    fingerprints = migration_fingerprints(project_root, steps)
    plan_digest = bundle_digest(project_root, steps, fingerprints=fingerprints)

    with startup_lock(database_path):
        locked_fingerprints = migration_fingerprints(project_root, steps)
        if locked_fingerprints != fingerprints:
            raise StartupMigrationError("Migration sources changed before startup acquired the lock")
        if bundle_digest(project_root, steps, fingerprints=locked_fingerprints) != plan_digest:
            raise StartupMigrationError("Migration plan changed before startup acquired the lock")
        # Check the old receipt before adding new journal tables: the certified
        # old schema hash intentionally excluded only the old whole-run table.
        connection = _connect_rw(database_path)
        try:
            had_step_table = _table_exists(connection, STEP_TABLE)
            if not had_step_table and _read_whole(connection) is not None:
                if _certified_legacy_bootstrap(connection, project_root, steps, fingerprints, plan_digest):
                    print("Database startup: certified legacy migration receipt transferred to per-step journal", flush=True)
                    return False
            _ensure_journal_tables(connection)

            whole = _read_whole(connection)
            step_rows = _read_step_rows(connection)
            active = _read_run(connection)
            current_schema = schema_digest(connection)
            _validate_order(steps, step_rows)

            if active:
                if active[0] != plan_digest:
                    raise StartupMigrationError("Migration plan changed during an interrupted run; manual recovery required")
                _validate_interrupted_checkpoint(
                    connection, active, step_rows, current_schema, fingerprints,
                    {step.key: index for index, step in enumerate(steps)},
                )
                required = json.loads(active[2])
                if any(key not in set(keys) for key in required):
                    raise StartupMigrationError("Interrupted run references an unknown migration step")
            else:
                if step_rows and whole is None:
                    raise StartupMigrationError("Per-step receipts have no whole-run success record; manual review required")
                if whole and step_rows and _stored_steps_bundle_digest(step_rows) != whole[0]:
                    raise StartupMigrationError("Per-step receipts do not match the previous whole-run success")
                if whole and whole[0] != plan_digest and not step_rows and whole[0] == LEGACY_BUNDLE_DIGEST:
                    raise StartupMigrationError("Certified legacy migration receipt did not match this step plan")
                if whole and current_schema != whole[1]:
                    raise StartupMigrationError("SQLite schema drift without an interrupted migration; startup is fail-closed")
                if whole and not step_rows:
                    raise StartupMigrationError("Whole-run receipt has no per-step proof; startup is fail-closed")
                required = _needs(steps, step_rows, fingerprints)
                if not required:
                    if whole and whole[0] == plan_digest and whole[1] == current_schema:
                        if migration_fingerprints(project_root, steps) != fingerprints:
                            raise StartupMigrationError("Migration sources changed during startup")
                        print("Database startup: verified per-step plan and schema are current", flush=True)
                        return False
                    if migration_fingerprints(project_root, steps) != fingerprints:
                        raise StartupMigrationError("Migration sources changed before startup success could be recorded")
                    _record_success(connection, plan_digest, current_schema)
                    return False
                _record_running(connection, plan_digest, current_schema, required)

            required_set = set(required)
            ordinals = {step.key: index for index, step in enumerate(steps)}
            step_rows = _read_step_rows(connection)
            index = 0
            while index < len(steps):
                step = steps[index]
                if step.kind == "scoped":
                    group = step.batch_group
                    group_steps = []
                    while index < len(steps) and steps[index].kind == "scoped" and steps[index].batch_group == group:
                        group_steps.append(steps[index])
                        index += 1
                    _execute_scoped_group(connection, project_root, database_path, group_steps,
                                          required_set, fingerprints, step_rows, ordinals)
                    step_rows = _read_step_rows(connection)
                    continue
                index += 1
                row = step_rows.get(step.key)
                if step.key not in required_set or (row and row[0] == fingerprints[step.key] and row[2] == "completed"):
                    continue
                _assert_fingerprints(project_root, [step], fingerprints, keys={step.key})
                _mark_step_running(connection, step, fingerprints[step.key], ordinals[step.key], schema_digest(connection))
                if executor is None:
                    _execute_step(step, project_root, database_path)
                else:
                    executor(step, project_root, database_path)
                _assert_fingerprints(project_root, [step], fingerprints, keys={step.key})
                current_schema = schema_digest(connection)
                _mark_step_completed(connection, step, fingerprints[step.key], ordinals[step.key])
                step_rows = _read_step_rows(connection)

            # No success is recorded until every requested and dependent step
            # has a completed receipt under the current source fingerprint.
            final_rows = _read_step_rows(connection)
            for step in steps:
                row = final_rows.get(step.key)
                if not row or row[0] != fingerprints[step.key] or row[2] != "completed":
                    raise StartupMigrationError(f"Migration step {step.key} is not complete")
            if migration_fingerprints(project_root, steps) != fingerprints:
                raise StartupMigrationError("Migration sources changed before startup success could be recorded")
            final_schema = schema_digest(connection)
            _record_success(connection, plan_digest, final_schema)
            print(f"Database startup: migration plan complete ({len(required)} changed step(s))", flush=True)
            return True
        finally:
            connection.close()


def is_current(database_path: Path, expected_bundle: str,
               project_root: Path | None = None,
               steps: tuple[MigrationStep, ...] | None = None) -> bool:
    if not database_path.exists():
        return False
    connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    try:
        whole = _read_whole(connection)
        if not whole or whole[0] != expected_bundle or whole[1] != schema_digest(connection):
            return False
        if _read_run(connection) is not None:
            return False
        try:
            _validate_journal_tables(connection)
        except StartupMigrationError:
            return False
        project_root = Path(project_root or Path(__file__).resolve().parent.parent).resolve()
        steps = steps or migration_steps()
        fingerprints = migration_fingerprints(project_root, steps)
        if expected_bundle != bundle_digest(project_root, steps, fingerprints=fingerprints):
            return False
        rows = _read_step_rows(connection)
        if len(rows) != len(steps):
            return False
        for ordinal, step in enumerate(steps):
            row = rows.get(step.key)
            if not row or row[0] != fingerprints[step.key] or row[1] != ordinal or row[2] != "completed":
                return False
        return True
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-path", type=Path, required=True)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parent.parent
    try:
        run(args.database_path, project_root)
    except subprocess.CalledProcessError as exc:
        print("Database startup: migration step failed; success was not recorded", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (StartupMigrationError, OSError, sqlite3.Error) as exc:
        print(f"Database startup failed ({type(exc).__name__}); success was not recorded", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
