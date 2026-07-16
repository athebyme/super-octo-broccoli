"""Scoped foreign-key safety checks for additive SQLite migrations.

Production databases can contain old orphan rows in domains unrelated to the
schema being migrated.  A migration must never introduce another violation or
silently accept a violation involving one of the tables it owns, but it also
must not claim that an unchanged legacy orphan was caused by its own DDL.
"""

from __future__ import annotations

import sqlite3
from typing import FrozenSet, Iterable, Tuple, Type


ForeignKeyViolation = Tuple[object, ...]


def foreign_key_snapshot(
    connection: sqlite3.Connection,
) -> FrozenSet[ForeignKeyViolation]:
    """Return the complete, hashable ``PRAGMA foreign_key_check`` result."""

    return frozenset(
        tuple(row)
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    )


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
