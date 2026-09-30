"""Synthetic checks for source-scoped, durable startup migration steps."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from scripts import startup_migrations as startup
from scripts.startup_migration_steps import MigrationStep, migration_steps
from migrations.migrate_add_imported_content_overrides import migrate as migrate_common_content


def _write(root: Path, relative: str, body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_common_content_schema_step_is_appended_after_existing_plan():
    steps = migration_steps()
    assert len(steps) == 80
    assert steps[-1].key == "migrate-add-imported-content-overrides"
    assert steps[-1].script == "migrations/migrate_add_imported_content_overrides.py"
    assert len({step.key for step in steps}) == len(steps)


def test_common_content_migration_preserves_populated_legacy_imported_products(tmp_path):
    database = tmp_path / "populated-legacy.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE imported_products ("
            "id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL, title TEXT, "
            "description TEXT, original_data TEXT, import_status TEXT)"
        )
        connection.execute(
            "INSERT INTO imported_products (id,seller_id,title,description,original_data,import_status) "
            "VALUES (41,7,'Existing title','Existing description','{\"title\":\"Existing title\"}','validated')"
        )

    assert migrate_common_content(str(database)) is True
    assert migrate_common_content(str(database)) is True
    with sqlite3.connect(database) as connection:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(imported_products)")}
        row = connection.execute(
            "SELECT id,seller_id,title,description,original_data,import_status,content_overrides_json,content_edit_version "
            "FROM imported_products WHERE id=41"
        ).fetchone()

    assert columns["content_overrides_json"][2] == "TEXT"
    assert columns["content_overrides_json"][3] == 0
    assert columns["content_edit_version"][2] == "INTEGER"
    assert columns["content_edit_version"][3] == 1
    assert str(columns["content_edit_version"][4]).strip("'\"() ") == "1"
    assert row == (41, 7, "Existing title", "Existing description", '{"title":"Existing title"}', "validated", None, 1)


def test_common_content_migration_rolls_back_partial_ddl_on_incompatible_schema(tmp_path):
    database = tmp_path / "incompatible.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE imported_products ("
            "id INTEGER PRIMARY KEY, content_edit_version TEXT NOT NULL DEFAULT '1')"
        )
        connection.execute("INSERT INTO imported_products (id,content_edit_version) VALUES (1,'1')")

    assert migrate_common_content(str(database)) is False
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(imported_products)")}
        values = connection.execute("SELECT content_edit_version FROM imported_products WHERE id=1").fetchone()
    assert "content_overrides_json" not in columns
    assert values == ("1",)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "application"
    root.mkdir()
    _write(root, "docker-entrypoint.sh", startup.EXPECTED_ENTRYPOINT_COMMAND + "\n")
    _write(root, "scripts/startup_migrations.py", "synthetic guard v1\n")
    _write(root, "scripts/startup_migration_steps.py", "synthetic plan v1\n")
    _write(root, "scripts/startup_migration_bootstrap.py", "# synthetic bootstrap v1\n")
    _write(root, "models.py", """
from flask_sqlalchemy import SQLAlchemy
db = SQLAlchemy()
class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    def helper(self):
        return 1
""")
    _write(root, "seller_platform.py", """
import os
from pathlib import Path
from flask import Flask
from models import db
app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = BASE_DIR / 'data'
DEFAULT_DB_PATH = DATA_ROOT / 'seller_platform.db'
database_url_from_env = os.environ.get('DATABASE_URL')
if database_url_from_env:
    database_url = database_url_from_env
else:
    database_url = f"sqlite:///{DEFAULT_DB_PATH.absolute()}"
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)
def ensure_storage_roots():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
def _run_startup_migrations():
    pass
app.register_blueprint(object())
""")
    _write(root, "migrations/helper_a.py", "def apply(): return 1\n")
    _write(root, "services/helper.py", "VALUE = 1\n")
    _write(root, "migrations/a.py", "from services.helper import VALUE\nVALUE_A = VALUE\n")
    _write(root, "migrations/b.py", "VALUE_B = 1\n")
    _write(root, "migrations/independent.py", "VALUE_I = 1\n")
    database = tmp_path / "seller.db"
    steps = (
        MigrationStep("a", "python", "migrations/a.py"),
        MigrationStep("b", "python", "migrations/b.py", depends_on=("a",)),
        MigrationStep("independent", "python", "migrations/independent.py"),
    )
    calls: list[str] = []

    def executor(step, _root, db_path):
        calls.append(step.key)
        table = step.key.replace("-", "_")
        with sqlite3.connect(db_path) as connection:
            connection.execute(f"CREATE TABLE IF NOT EXISTS applied_{table} (id INTEGER PRIMARY KEY)")

    return root, database, steps, calls, executor


def test_repeat_and_presentation_only_changes_skip_all_steps(project):
    root, database, steps, calls, executor = project
    assert startup.run(database, root, executor=executor, steps=steps) is True
    assert startup.run(database, root, executor=executor, steps=steps) is False
    _write(root, "routes/ui.py", "HTML = 'changed route'\n")
    _write(root, "templates/dashboard.html", "changed presentation\n")
    assert startup.run(database, root, executor=executor, steps=steps) is False
    assert calls == ["a", "b", "independent"]
    assert startup.is_current(database, startup.bundle_digest(root, steps), root, steps)


def test_changed_migration_helper_runs_only_step_and_explicit_dependants(project):
    root, database, steps, calls, executor = project
    startup.run(database, root, executor=executor, steps=steps)
    _write(root, "services/helper.py", "VALUE = 2\n")
    assert startup.run(database, root, executor=executor, steps=steps) is True
    assert calls == ["a", "b", "independent", "a", "b"]
    assert startup.is_current(database, startup.bundle_digest(root, steps), root, steps)


def test_app_database_contract_hashes_db_init_and_uri_but_not_ui_registration(project):
    root, *_ = project
    app = root / "seller_platform.py"
    baseline = startup.seller_platform_migration_signature(app)
    _write(root, "seller_platform.py", app.read_text() + "app.register_blueprint(object())\n")
    assert startup.seller_platform_migration_signature(app) == baseline
    body = app.read_text().replace("database_url = database_url_from_env", "database_url = 'sqlite:///different.db'")
    app.write_text(body, encoding="utf-8")
    assert startup.seller_platform_migration_signature(app) != baseline
    body = app.read_text().replace("db.init_app(app)", "db.init_app(other_app)")
    app.write_text(body, encoding="utf-8")
    assert startup.seller_platform_migration_signature(app) != baseline


def test_model_schema_signature_ignores_methods_but_tracks_columns(project):
    root, *_ = project
    models = root / "models.py"
    baseline = startup.models_schema_signature(models)
    models.write_text(models.read_text().replace("return 1", "return 2"), encoding="utf-8")
    assert startup.models_schema_signature(models) == baseline
    models.write_text(models.read_text().replace("db.Integer, primary_key=True", "db.String(40), primary_key=True"), encoding="utf-8")
    assert startup.models_schema_signature(models) != baseline


def test_runtime_import_from_model_method_is_not_a_schema_dependency(project):
    root, *_ = project
    models = root / "models.py"
    step = MigrationStep(
        "bootstrap", "bootstrap", "scripts/startup_migration_bootstrap.py",
        dependencies=("models.py", "seller_platform.py"),
    )
    original_sources = startup.source_files_for_step(step, root)
    original_fp = startup.step_fingerprint(step, root)
    assert root / "services/helper.py" not in original_sources
    models.write_text(
        models.read_text().replace(
            "    def helper(self):\n        return 1",
            "    def helper(self):\n        from services.helper import VALUE\n        return VALUE",
        ),
        encoding="utf-8",
    )
    assert startup.step_fingerprint(step, root) == original_fp
    assert root / "services/helper.py" not in startup.source_files_for_step(step, root)


def test_run_all_base_only_fingerprint_excludes_dynamic_child_chain(tmp_path):
    root = tmp_path / "project"
    script = _write(root, "migrations/run_all_migrations.py", """
import sqlite3

def get_existing_columns(cursor, table):
    return []

def add_column_if_missing(cursor, table, name, kind, existing):
    return False

def migrate(db_path):
    return True

def main():
    success = migrate('db')
    if success and not args.base_only:
        from migrate_child import migrate as child
        child('db')
""")
    step = MigrationStep("base", "python", "migrations/run_all_migrations.py", args=("{database}", "--base-only"))
    baseline = startup.step_fingerprint(step, root)
    script.write_text(script.read_text().replace("child('db')", "child('db', changed=True)"), encoding="utf-8")
    assert startup.step_fingerprint(step, root) == baseline
    with_else = script.read_text().replace(
        "        child('db', changed=True)\n",
        "        child('db', changed=True)\n    else:\n        print('base-only fallback')\n",
    )
    script.write_text(with_else, encoding="utf-8")
    assert startup.step_fingerprint(step, root) != baseline
    changed_condition = script.read_text().replace(
        "if success and not args.base_only:",
        "if success and not args.base_only or force_children:",
    )
    script.write_text(changed_condition, encoding="utf-8")
    assert startup.step_fingerprint(step, root) != baseline
    script.write_text(script.read_text().replace("return True", "return False", 1), encoding="utf-8")
    assert startup.step_fingerprint(step, root) != baseline


def test_new_appended_step_runs_without_reapplying_completed_steps(project):
    root, database, steps, calls, executor = project
    startup.run(database, root, executor=executor, steps=steps)
    _write(root, "migrations/new_step.py", "VALUE = 1\n")
    expanded = (*steps, MigrationStep("new-step", "python", "migrations/new_step.py"))
    assert startup.run(database, root, executor=executor, steps=expanded) is True
    assert calls == ["a", "b", "independent", "new-step"]


def test_schema_drift_after_success_fails_closed(project):
    root, database, steps, calls, executor = project
    startup.run(database, root, executor=executor, steps=steps)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE applied_a ADD COLUMN outside_change TEXT")
    with pytest.raises(startup.StartupMigrationError, match="schema drift"):
        startup.run(database, root, executor=executor, steps=steps)
    assert calls == ["a", "b", "independent"]


def test_incomplete_step_retries_but_completed_prefix_is_reused(project):
    root, database, steps, calls, executor = project
    fail = {"b": True}

    def failing_executor(step, project_root, db_path):
        if step.key == "b" and fail["b"]:
            calls.append(step.key)
            raise subprocess.CalledProcessError(1, [step.key])
        executor(step, project_root, db_path)

    with pytest.raises(subprocess.CalledProcessError):
        startup.run(database, root, executor=failing_executor, steps=steps)
    fail["b"] = False
    assert startup.run(database, root, executor=failing_executor, steps=steps) is True
    assert calls == ["a", "b", "b", "independent"]


def test_interrupted_step_with_schema_effect_requires_manual_recovery(project):
    root, database, steps, calls, executor = project

    def partial(step, _root, db_path):
        calls.append(step.key)
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE partial_effect (id INTEGER)")
        raise subprocess.CalledProcessError(1, [step.key])

    with pytest.raises(subprocess.CalledProcessError):
        startup.run(database, root, executor=partial, steps=steps[:1])
    with pytest.raises(startup.StartupMigrationError, match="interrupted step"):
        startup.run(database, root, executor=executor, steps=steps[:1])
    assert calls == ["a"]


def test_external_schema_change_during_interrupted_run_is_rejected(project):
    root, database, steps, calls, executor = project
    fail = {"b": True}

    def failing(step, project_root, db_path):
        if step.key == "b" and fail["b"]:
            raise subprocess.CalledProcessError(1, [step.key])
        executor(step, project_root, db_path)

    with pytest.raises(subprocess.CalledProcessError):
        startup.run(database, root, executor=failing, steps=steps)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE external_ddl (id INTEGER)")
    fail["b"] = False
    with pytest.raises(startup.StartupMigrationError, match="interrupted step"):
        startup.run(database, root, executor=failing, steps=steps)
    assert calls == ["a"]


def test_source_changed_during_execution_never_records_success(project):
    root, database, steps, calls, _executor = project

    def changing(step, project_root, db_path):
        calls.append(step.key)
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE changed_during_run (id INTEGER)")
        (project_root / step.script).write_text("changed during execution\n", encoding="utf-8")

    with pytest.raises(startup.StartupMigrationError, match="source changed during startup"):
        startup.run(database, root, executor=changing, steps=steps[:1])
    with sqlite3.connect(database) as connection:
        assert startup._read_whole(connection) is None
    assert not startup.is_current(database, "unrecorded", root, steps[:1])


def test_journal_shape_corruption_is_rejected(project):
    root, database, steps, calls, executor = project
    startup.run(database, root, executor=executor, steps=steps)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TRIGGER hostile_journal AFTER INSERT ON seller_hub_startup_migrations BEGIN SELECT 1; END")
    assert not startup.is_current(database, startup.bundle_digest(root, steps), root, steps)
    with pytest.raises(startup.StartupMigrationError, match="unexpected triggers"):
        startup.run(database, root, executor=executor, steps=steps)


@pytest.mark.parametrize("damage", ["fingerprint", "missing"])
def test_completed_step_proof_must_match_previous_whole_receipt(project, damage):
    root, database, steps, calls, executor = project
    startup.run(database, root, executor=executor, steps=steps)
    with sqlite3.connect(database) as connection:
        if damage == "fingerprint":
            connection.execute(
                f"UPDATE {startup.STEP_TABLE} SET fingerprint='forged' WHERE step_key='a'"
            )
        else:
            connection.execute(f"DELETE FROM {startup.STEP_TABLE} WHERE step_key='a'")
    with pytest.raises(startup.StartupMigrationError, match="do not match the previous whole-run"):
        startup.run(database, root, executor=executor, steps=steps)
    assert calls == ["a", "b", "independent"]


def _legacy_fixture(root: Path, database: Path, steps: tuple[MigrationStep, ...], monkeypatch):
    _write(root, "migrations/run_scoped_batch.py", "legacy runner bridge target\n")
    files = {}
    for relative in startup.LEGACY_BRIDGE_FILES:
        files[relative] = hashlib.sha256(("legacy:" + relative).encode()).hexdigest()
    for step in steps:
        for path in startup.source_files_for_step(step, root):
            relative = path.relative_to(root).as_posix()
            if relative not in files:
                files[relative] = startup._sha256(path.read_bytes())
    legacy_digest = "synthetic-legacy-bundle"
    completed_at = "2026-09-30T00:00:00+00:00"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
        connection.execute(
            f"CREATE TABLE {startup.JOURNAL_TABLE} (id INTEGER PRIMARY KEY CHECK (id=1), "
            "bundle_digest TEXT NOT NULL, schema_digest TEXT NOT NULL, completed_at TEXT NOT NULL)"
        )
        legacy_schema = startup._legacy_schema_digest(connection)
        connection.execute(
            f"INSERT INTO {startup.JOURNAL_TABLE} VALUES (1,?,?,?)",
            (legacy_digest, legacy_schema, completed_at),
        )
    receipt = {
        "format_version": 1,
        "legacy_bundle_digest": legacy_digest,
        "legacy_schema_digest": legacy_schema,
        "completed_at": completed_at,
        "source_manifest_aggregate_sha256": "synthetic-manifest",
        "files": files,
    }
    fingerprints = startup.migration_fingerprints(root, steps)
    plan_digest = startup.bundle_digest(root, steps, fingerprints=fingerprints)
    compatibility = startup._legacy_compatibility_payload(root, steps, fingerprints, plan_digest, receipt)
    receipt["compatibility"] = compatibility
    compatibility_sha = startup._compatibility_digest(compatibility)
    receipt["compatibility_sha256"] = compatibility_sha
    cert_path = _write(root, "scripts/certified_legacy.json", json.dumps(receipt, sort_keys=True))
    monkeypatch.setattr(startup, "LEGACY_BUNDLE_DIGEST", legacy_digest)
    monkeypatch.setattr(startup, "LEGACY_SCHEMA_DIGEST", legacy_schema)
    monkeypatch.setattr(startup, "LEGACY_COMPLETED_AT", completed_at)
    monkeypatch.setattr(startup, "LEGACY_SOURCE_MANIFEST_AGGREGATE", "synthetic-manifest")
    monkeypatch.setattr(startup, "LEGACY_RECEIPT", "scripts/certified_legacy.json")
    monkeypatch.setattr(startup, "LEGACY_COMPATIBILITY_SHA256", compatibility_sha)
    return plan_digest, receipt, cert_path


def test_exact_certified_legacy_whole_success_transfers_without_running_steps(project, monkeypatch):
    root, database, steps, calls, executor = project
    plan_digest, _receipt, _path = _legacy_fixture(root, database, steps, monkeypatch)
    assert startup.run(database, root, executor=executor, steps=steps) is False
    assert calls == []
    assert startup.is_current(database, plan_digest, root, steps)
    with sqlite3.connect(database) as connection:
        rows = startup._read_step_rows(connection)
        assert len(rows) == len(steps)
        assert {row[5] for row in rows.values()} == {"certified-legacy:" + startup.LEGACY_BUNDLE_DIGEST}


def test_legacy_certificate_plan_or_source_mismatch_fails_closed(project, monkeypatch):
    root, database, steps, calls, executor = project
    _legacy_fixture(root, database, steps, monkeypatch)
    _write(root, "migrations/new_step.py", "VALUE = 2\n")
    expanded = (*steps, MigrationStep("new-step", "python", "migrations/new_step.py"))
    with pytest.raises(startup.StartupMigrationError, match="Legacy receipt does not certify"):
        startup.run(database, root, executor=executor, steps=expanded)
    assert calls == []


def test_legacy_certificate_never_bootstraps_from_schema_shape_alone(project, monkeypatch):
    root, database, steps, calls, executor = project
    _legacy_fixture(root, database, steps, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE products ADD COLUMN unexpected TEXT")
    with pytest.raises(startup.StartupMigrationError, match="does not match the live SQLite schema"):
        startup.run(database, root, executor=executor, steps=steps)
    assert calls == []


def test_scoped_runner_uses_shared_connection_and_only_selected_children(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from migrations import run_scoped_batch as scoped

    database = tmp_path / "scope.db"
    connection = sqlite3.connect(database)
    seen = []

    def apply(connection_arg, *, verbose=True, **_kwargs):
        seen.append(connection_arg is connection)
        connection_arg.execute("CREATE TABLE ran (id INTEGER)")

    monkeypatch.setattr(scoped, "import_module", lambda _name: SimpleNamespace(apply_migration=apply))
    calls = []
    scripts = [
        "migrations/migrate_add_marketplace_accounts.py",
        "migrations/migrate_add_marketplace_credential_notices.py",
    ]
    scoped.run_batch(
        database, scripts, verbose=False, connection=connection,
        skip_scripts={scripts[1]},
        before_step=lambda conn, name: calls.append(("before", conn is connection, name)),
        after_step=lambda conn, name: calls.append(("after", conn is connection, name)),
    )
    assert seen == [True]
    assert calls == [("before", True, scripts[0]), ("after", True, scripts[0])]
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='ran'").fetchone()
    connection.close()


def test_scoped_callback_row_factory_keeps_schema_and_receipt_checkpoints(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from migrations import run_scoped_batch as scoped

    database = tmp_path / "row-factory.db"
    connection = sqlite3.connect(database)
    step = MigrationStep(
        "marketplace-accounts", "scoped",
        "migrations/migrate_add_marketplace_accounts.py",
        batch_group="synthetic",
    )
    startup._ensure_journal_tables(connection)
    baseline = startup.schema_digest(connection)
    startup._record_running(connection, "synthetic-plan", baseline, [step.key])
    fingerprint = "synthetic-step-fingerprint"
    startup._mark_step_running(connection, step, fingerprint, 0, baseline)

    def apply(connection_arg, *, verbose=True, **_kwargs):
        connection_arg.execute("CREATE TABLE scoped_row_factory_effect (id INTEGER PRIMARY KEY)")
        connection_arg.row_factory = sqlite3.Row

    monkeypatch.setattr(scoped, "import_module", lambda _name: SimpleNamespace(apply_migration=apply))

    def complete_step(connection_arg, _script):
        assert connection_arg.row_factory is sqlite3.Row
        startup._mark_step_completed(connection_arg, step, fingerprint, 0)

    scoped.run_batch(
        database, [step.script], verbose=False, connection=connection,
        after_step=complete_step,
    )

    assert connection.row_factory is sqlite3.Row
    row = startup._read_step_rows(connection)[step.key]
    assert row[2] == "completed"
    assert row[4] == startup.schema_digest(connection)
    bundle = startup._stored_steps_bundle_digest(startup._read_step_rows(connection))
    schema = startup.schema_digest(connection)
    startup._record_success(connection, bundle, schema)
    whole = startup._read_whole(connection)
    assert whole[0] == bundle
    assert whole[1] == schema
    legacy_digest_with_rows = startup._legacy_schema_digest(connection)
    connection.row_factory = None
    assert startup._legacy_schema_digest(connection) == legacy_digest_with_rows
    connection.row_factory = sqlite3.Row
    connection.close()
