"""Repeated boot skips only a successfully verified, unchanged migration bundle."""

import sqlite3
import subprocess
from unittest.mock import Mock

import pytest

from scripts import startup_migrations as startup


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    root = tmp_path / "application"
    root.mkdir()
    for name in (
        "docker-entrypoint.sh", "seller_platform.py", "models.py", "requirements.txt",
        "scripts/startup_migrations.py", "migrations/schema.py", "migrations/_helper.py",
        "services/helper.py",
    ):
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("synthetic version 1", encoding="utf-8")
    database = tmp_path / "seller.db"

    def migrate(*args, **kwargs):
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY)")

    command = Mock(side_effect=migrate)
    monkeypatch.setattr(startup.subprocess, "run", command)
    return database, root, command


def test_normal_restart_and_product_changes_do_not_repeat_migrations(deployment):
    database, root, command = deployment
    assert startup.run(database, root, ["migrate"]) is True
    assert startup.run(database, root, ["migrate"]) is False
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO products VALUES (1)")
    assert startup.run(database, root, ["migrate"]) is False
    assert command.call_count == 1


@pytest.mark.parametrize("filename", [
    "models.py", "migrations/schema.py", "migrations/_helper.py", "services/helper.py",
])
def test_changed_migration_or_dependency_requires_full_run(deployment, filename):
    database, root, command = deployment
    startup.run(database, root, ["migrate"])
    (root / filename).write_text("synthetic version 2", encoding="utf-8")
    assert startup.run(database, root, ["migrate"]) is True
    assert command.call_count == 2


def test_schema_drift_and_missing_journal_require_full_run(deployment):
    database, root, command = deployment
    startup.run(database, root, ["migrate"])
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE products ADD COLUMN title TEXT")
    assert startup.run(database, root, ["migrate"]) is True
    with sqlite3.connect(database) as connection:
        connection.execute(f"DROP TABLE {startup.JOURNAL_TABLE}")
    assert startup.run(database, root, ["migrate"]) is True
    assert command.call_count == 3


def test_failed_partial_run_is_not_recorded_and_can_resume(deployment):
    database, root, command = deployment
    migrate = command.side_effect

    def partial_failure(*args, **kwargs):
        migrate()
        raise subprocess.CalledProcessError(1, ["migrate"])

    command.side_effect = partial_failure
    with pytest.raises(subprocess.CalledProcessError):
        startup.run(database, root, ["migrate"])
    assert not startup.is_current(database, startup.bundle_digest(root))
    command.side_effect = migrate
    assert startup.run(database, root, ["migrate"]) is True


def test_failed_upgrade_does_not_erase_last_success_or_mark_new_bundle(deployment):
    database, root, command = deployment
    startup.run(database, root, ["migrate"])
    before = startup.bundle_digest(root)
    (root / "models.py").write_text("new schema", encoding="utf-8")
    command.side_effect = subprocess.CalledProcessError(1, ["migrate"])
    with pytest.raises(subprocess.CalledProcessError):
        startup.run(database, root, ["migrate"])
    assert startup.is_current(database, before)
    assert not startup.is_current(database, startup.bundle_digest(root))


def test_concurrent_startup_does_not_run_second_writer(deployment):
    database, root, command = deployment
    with startup.startup_lock(database):
        with pytest.raises(startup.StartupMigrationError, match="in progress"):
            startup.run(database, root, ["migrate"])
    command.assert_not_called()


def test_code_changed_while_migrating_is_not_recorded(deployment):
    database, root, command = deployment
    migrate = command.side_effect

    def changing_code(*args, **kwargs):
        migrate()
        (root / "models.py").write_text("changed while running", encoding="utf-8")

    command.side_effect = changing_code
    with pytest.raises(startup.StartupMigrationError, match="changed during startup"):
        startup.run(database, root, ["migrate"])
    assert not startup.is_current(database, startup.bundle_digest(root))


def test_success_cannot_create_an_empty_database_on_wrong_path(deployment):
    database, root, command = deployment
    command.side_effect = None
    with pytest.raises(sqlite3.OperationalError):
        startup.run(database, root, ["migrate"])
    assert not database.exists()
