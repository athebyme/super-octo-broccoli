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
