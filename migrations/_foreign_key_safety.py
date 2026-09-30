"""Scoped foreign-key safety checks for additive SQLite migrations.

Production databases can contain old orphan rows in domains unrelated to the
schema being migrated.  A migration must never introduce another violation or
silently accept a violation involving one of the tables it owns, but it also
must not claim that an unchanged legacy orphan was caused by its own DDL.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from typing import FrozenSet, Iterable, Tuple, Type


ForeignKeyViolation = Tuple[object, ...]
_scan_scope = ContextVar("migration_foreign_key_scan_scope", default=None)


@contextmanager
def reuse_foreign_key_scans(connection: sqlite3.Connection):
    """Reuse a verified scan only inside one explicit connection lifetime.

    Ordinary standalone migrations retain their independent full checks. A
    batch may reuse evidence from an earlier no-op step, but each step still
    rejects managed violations and any changed revision requires another scan.
    """
    state = {"connection": connection, "snapshot": None}
    token = _scan_scope.set(state)
    try:
        yield
    finally:
        state.clear()
        _scan_scope.reset(token)


def _database_revision(connection: sqlite3.Connection) -> tuple:
    """Include local DML, DDL, transaction boundaries and other writers."""
    return (
        connection.total_changes,
        connection.in_transaction,
        connection.execute("PRAGMA main.schema_version").fetchone()[0],
        connection.execute("PRAGMA main.data_version").fetchone()[0],
    )


class _ForeignKeySnapshot(frozenset):
    """A normal violation set with evidence that the same DB is unchanged."""

    def __new__(cls, violations, *, connection, revision):
        result = super().__new__(cls, violations)
        result.connection = connection
        result.revision = revision
        return result


def foreign_key_snapshot(
    connection: sqlite3.Connection,
) -> FrozenSet[ForeignKeyViolation]:
    """Return the complete, hashable ``PRAGMA foreign_key_check`` result."""

    before = _database_revision(connection)
    scope = _scan_scope.get()
    if scope and scope["connection"] is connection:
        previous = scope["snapshot"]
        if previous is not None and previous.revision == before:
            return previous
    violations = (
        tuple(row)
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    )
    after = _database_revision(connection)
    snapshot = _ForeignKeySnapshot(
        violations,
        connection=connection,
        revision=after if before == after else None,
    )
    if scope and scope["connection"] is connection:
        scope["snapshot"] = snapshot if snapshot.revision is not None else None
    return snapshot


def assert_foreign_key_safety(
    connection: sqlite3.Connection,
    *,
    baseline: FrozenSet[ForeignKeyViolation],
    managed_tables: Iterable[str],
    label: str,
    error_type: Type[Exception] = sqlite3.IntegrityError,
) -> None:
    """Reject new violations and every violation in the migrated domain.

    Unchanged violations outside ``managed_tables`` remain visible to
    operations/repair tooling but do not block an unrelated schema migration.
    The parent table is checked as well as the child table so rebuilding a
    referenced table cannot hide a regression in another domain.
    """

    # CREATE TABLE/INDEX IF NOT EXISTS and schema inspection often make no
    # change on a deployed database. Repeating a full FK scan of its large
    # product/history tables then adds minutes to startup without new evidence.
    # Only reuse the actual scan when every SQLite revision remains identical;
    # local writes, DDL, other connections and transaction changes force a scan.
    if (
        isinstance(baseline, _ForeignKeySnapshot)
        and baseline.connection is connection
        and baseline.revision == _database_revision(connection)
    ):
        current = baseline
    else:
        current = foreign_key_snapshot(connection)
    managed = frozenset(str(table) for table in managed_tables)
    scoped = {
        row
        for row in current
        if str(row[0]) in managed or str(row[2]) in managed
    }
    introduced = current - baseline
    if scoped or introduced:
        raise error_type(
            f"{label} foreign-key safety check failed: "
            f"managed={len(scoped)}, introduced={len(introduced)}"
        )
