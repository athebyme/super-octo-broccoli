"""Migration FK checks distinguish legacy debt from new regressions."""

import sqlite3

import pytest

from migrations._foreign_key_safety import (
    assert_foreign_key_safety,
    foreign_key_snapshot,
)


def _connection_with_orphan(*, child_table="legacy_child"):
    connection = sqlite3.connect(":memory:")
    connection.executescript(f"""
        CREATE TABLE legacy_parent (id INTEGER PRIMARY KEY);
        CREATE TABLE {child_table} (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES legacy_parent(id)
        );
        INSERT INTO {child_table}(id, parent_id) VALUES (1, 404);
    """)
    return connection


def test_unchanged_unrelated_orphan_does_not_block_migration():
    connection = _connection_with_orphan()
    try:
        baseline = foreign_key_snapshot(connection)
        assert_foreign_key_safety(
            connection,
            baseline=baseline,
            managed_tables={"marketplace_operations"},
            label="test migration",
        )
    finally:
        connection.close()


def test_new_or_managed_violation_fails_closed():
    connection = _connection_with_orphan()
    try:
        baseline = foreign_key_snapshot(connection)
        connection.execute(
            "INSERT INTO legacy_child(id, parent_id) VALUES (2, 405)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="introduced=1"):
            assert_foreign_key_safety(
                connection,
                baseline=baseline,
                managed_tables={"marketplace_operations"},
                label="test migration",
            )

        with pytest.raises(sqlite3.IntegrityError, match="managed=2"):
            assert_foreign_key_safety(
                connection,
                baseline=foreign_key_snapshot(connection),
                managed_tables={"legacy_parent"},
                label="test migration",
            )
    finally:
        connection.close()


def test_unchanged_schema_reuses_scan_but_still_rejects_managed_orphan():
    connection = _connection_with_orphan()
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        baseline = foreign_key_snapshot(connection)
        connection.execute("CREATE TABLE IF NOT EXISTS legacy_parent (id INTEGER PRIMARY KEY)")
        assert_foreign_key_safety(
            connection, baseline=baseline, managed_tables={"unrelated"}, label="test",
        )
        with pytest.raises(sqlite3.IntegrityError, match="managed=1"):
            assert_foreign_key_safety(
                connection, baseline=baseline, managed_tables={"legacy_parent"}, label="test",
            )
        assert statements.count("PRAGMA foreign_key_check") == 1
    finally:
        connection.close()


def test_changed_schema_forces_fresh_scan():
    connection = _connection_with_orphan()
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        baseline = foreign_key_snapshot(connection)
        connection.execute("CREATE TABLE new_table (id INTEGER PRIMARY KEY)")
        assert_foreign_key_safety(
            connection, baseline=baseline, managed_tables={"new_table"}, label="test",
        )
        assert statements.count("PRAGMA foreign_key_check") == 2
    finally:
        connection.close()


def test_other_connection_write_forces_fresh_scan(tmp_path):
    database_path = tmp_path / "migration.db"
    connection = sqlite3.connect(database_path)
    other = sqlite3.connect(database_path)
    try:
        connection.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id REFERENCES parent(id));
        """)
        baseline = foreign_key_snapshot(connection)
        other.execute("INSERT INTO child VALUES (1, 404)")
        other.commit()
        with pytest.raises(sqlite3.IntegrityError, match="introduced=1"):
            assert_foreign_key_safety(
                connection, baseline=baseline, managed_tables={"unrelated"}, label="test",
            )
    finally:
        connection.close()
        other.close()


def test_snapshot_from_different_database_is_never_reused():
    empty = sqlite3.connect(":memory:")
    orphaned = _connection_with_orphan()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="introduced=1"):
            assert_foreign_key_safety(
                orphaned, baseline=foreign_key_snapshot(empty),
                managed_tables={"unrelated"}, label="test",
            )
    finally:
        empty.close()
        orphaned.close()
