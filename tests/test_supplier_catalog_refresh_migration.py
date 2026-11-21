import sqlite3

import pytest

from migrations.migrate_add_supplier_catalog_refresh_runs import migrate


def test_catalog_refresh_upgrade_is_additive_and_idempotent(tmp_path):
    path = tmp_path / "app.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO users VALUES (1);
            INSERT INTO suppliers VALUES (1, 'catalog fixture');
        """)
    assert migrate(str(path)) == 4
    assert migrate(str(path)) == 0

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM suppliers").fetchall() == [(1, "catalog fixture")]
        assert conn.execute("SELECT * FROM users").fetchall() == [(1,)]
        insert = (
            "INSERT INTO supplier_catalog_refresh_runs "
            "(id, supplier_id, admin_user_id, status, config_digest, created_at) "
            "VALUES (?, 1, 1, ?, 'digest', '2026-01-01T00:00:00')"
        )
        conn.execute(insert, ("old", "completed"))
        conn.execute(insert, ("queued", "pending"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("duplicate", "running"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("invalid", "succeeded"))
        assert conn.execute(
            "SELECT total, added, updated, raw_committed_at FROM supplier_catalog_refresh_runs "
            "WHERE id = 'queued'"
        ).fetchone() == (None, None, None, None)


def test_catalog_refresh_upgrade_refuses_absent_or_wrong_schema(tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        migrate(str(missing))
    assert not missing.exists()

    path = tmp_path / "incompatible.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE suppliers (id INTEGER PRIMARY KEY);
            CREATE TABLE supplier_catalog_refresh_runs (wrong_column TEXT);
        """)
    with pytest.raises(sqlite3.OperationalError, match="incompatible"):
        migrate(str(path))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM supplier_catalog_refresh_runs").description[0][0] == "wrong_column"
        assert not conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
