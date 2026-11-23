import sqlite3

import pytest

from migrations.migrate_add_supplier_catalog_refresh_runs import (
    migrate as migrate_refresh_runs,
)
from migrations.migrate_add_supplier_catalog_schedule import migrate


def _database_with_legacy_refresh_receipt(path):
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, is_admin BOOLEAN NOT NULL DEFAULT 0);
            CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            INSERT INTO users (id, is_admin) VALUES (7, 1);
            INSERT INTO suppliers (id, name) VALUES (11, 'existing supplier');
        """)
    migrate_refresh_runs(str(path))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO supplier_catalog_refresh_runs "
            "(id, supplier_id, admin_user_id, status, config_digest, created_at, "
            "total, added, updated) VALUES "
            "('legacy-run', 11, 7, 'completed', 'old-digest', "
            "'2026-10-01T12:00:00', 20, 3, 4)"
        )


def test_supplier_catalog_schedule_is_additive_idempotent_and_has_expected_fk_behavior(tmp_path):
    path = tmp_path / "app.sqlite"
    _database_with_legacy_refresh_receipt(path)
    with sqlite3.connect(path) as connection:
        old_schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='supplier_catalog_refresh_runs'"
        ).fetchone()[0]
        old_receipt = connection.execute(
            "SELECT id,supplier_id,admin_user_id,status,config_digest,created_at,"
            "total,added,updated FROM supplier_catalog_refresh_runs WHERE id='legacy-run'"
        ).fetchone()

    assert migrate(str(path)) == 4
    assert migrate(str(path)) == 0

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='supplier_catalog_refresh_runs'"
        ).fetchone()[0] == old_schema
        assert connection.execute(
            "SELECT id,supplier_id,admin_user_id,status,config_digest,created_at,"
            "total,added,updated FROM supplier_catalog_refresh_runs WHERE id='legacy-run'"
        ).fetchone() == old_receipt
        # Scheduling is opt-in: migration did not manufacture a schedule row
        # or an administrator identity.
        assert connection.execute(
            "SELECT COUNT(*) FROM supplier_catalog_schedules"
        ).fetchone() == (0,)
        assert connection.execute("SELECT id FROM users").fetchall() == [(7,)]

        connection.execute(
            "INSERT INTO supplier_catalog_schedules "
            "(supplier_id, authorized_by_user_id) VALUES (11, 7)"
        )
        enabled, interval_hours, next_due_at, revision, updated_at = connection.execute(
            "SELECT enabled,interval_hours,next_due_at,revision,updated_at "
            "FROM supplier_catalog_schedules WHERE supplier_id=11"
        ).fetchone()
        assert (enabled, interval_hours, next_due_at, revision) == (0, 24, None, 1)
        assert updated_at

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO supplier_catalog_schedules "
                "(supplier_id, authorized_by_user_id, interval_hours) "
                "VALUES (11, 7, 5)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE supplier_catalog_schedules SET revision=1 WHERE supplier_id=11"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE supplier_catalog_schedules SET revision=0 WHERE supplier_id=11"
            )
        connection.execute(
            "UPDATE supplier_catalog_schedules SET revision=2 WHERE supplier_id=11"
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO supplier_catalog_scheduled_runs (run_id,schedule_revision,due_at) "
                "VALUES ('missing-run',1,'2026-10-02T12:00:00')"
            )
        connection.execute(
            "INSERT INTO supplier_catalog_scheduled_runs (run_id,schedule_revision,due_at) "
            "VALUES ('legacy-run',2,'2026-10-02T12:00:00')"
        )
        connection.execute(
            "DELETE FROM supplier_catalog_refresh_runs WHERE id='legacy-run'"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM supplier_catalog_scheduled_runs"
        ).fetchone() == (0,)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id=7")
        connection.execute("DELETE FROM suppliers WHERE id=11")
        assert connection.execute(
            "SELECT COUNT(*) FROM supplier_catalog_schedules"
        ).fetchone() == (0,)


def test_supplier_catalog_schedule_migration_fails_closed_and_rolls_back(tmp_path):
    missing_parent = tmp_path / "missing-parent.sqlite"
    with sqlite3.connect(missing_parent) as connection:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE suppliers (id INTEGER PRIMARY KEY)")
    with pytest.raises(sqlite3.OperationalError, match="parent table"):
        migrate(str(missing_parent))
    with sqlite3.connect(missing_parent) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'supplier_catalog_schedule%'"
        ).fetchall() == []

    incompatible = tmp_path / "incompatible.sqlite"
    with sqlite3.connect(incompatible) as connection:
        connection.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE suppliers (id INTEGER PRIMARY KEY);
            CREATE TABLE supplier_catalog_refresh_runs (id TEXT PRIMARY KEY);
            CREATE TABLE supplier_catalog_schedules (wrong_column TEXT);
        """)
    with pytest.raises(sqlite3.OperationalError, match="incompatible"):
        migrate(str(incompatible))
    with sqlite3.connect(incompatible) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='supplier_catalog_scheduled_runs'"
        ).fetchall() == []
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='ix_supplier_catalog_schedules_enabled_due'"
        ).fetchall() == []


def test_supplier_catalog_schedule_migration_requires_existing_database(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        migrate(str(tmp_path / "absent.sqlite"))
