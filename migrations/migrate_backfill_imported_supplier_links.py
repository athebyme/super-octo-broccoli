#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill exact legacy ImportedProduct -> SupplierProduct provenance links.

Legacy seller imports often predate ``supplier_product_id`` even though they
retain both ``supplier_id`` and the source feed's ``external_id``.  The central
catalog has a unique ``(supplier_id, external_id)`` constraint, so that pair is
an exact provenance key—not a title, AI or fuzzy-content match.

Only NULL foreign keys are filled.  Work is committed in small chunks to avoid
a long SQLite write lock.  The migration is idempotent and performs no provider
or LLM calls.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


BATCH_SIZE = 500
REQUIRED_COLUMNS = {
    'imported_products': {
        'id', 'supplier_id', 'external_id', 'supplier_product_id',
    },
    'supplier_products': {'id', 'supplier_id', 'external_id'},
}


def _columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall()
    }


def _require_schema(connection: sqlite3.Connection) -> None:
    for table_name, expected in REQUIRED_COLUMNS.items():
        actual = _columns(connection, table_name)
        if not actual:
            raise sqlite3.OperationalError(
                f'imported supplier link prerequisite missing: {table_name}'
            )
        missing = expected - actual
        if missing:
            raise sqlite3.OperationalError(
                f'{table_name} is missing columns: '
                + ', '.join(sorted(missing))
            )


def _next_batch(
    connection: sqlite3.Connection,
    batch_size: int,
) -> list[tuple[int, int]]:
    return [
        (int(row[0]), int(row[1]))
        for row in connection.execute(
            '''
            SELECT imported.id, source.id
            FROM imported_products AS imported
            JOIN supplier_products AS source
              ON source.supplier_id = imported.supplier_id
             AND source.external_id = imported.external_id
            WHERE imported.supplier_product_id IS NULL
              AND imported.supplier_id IS NOT NULL
              AND imported.external_id IS NOT NULL
              AND imported.external_id != ''
            ORDER BY imported.id ASC
            LIMIT ?
            ''',
            (batch_size,),
        ).fetchall()
    ]


def _validate_batch(
    connection: sqlite3.Connection,
    imported_ids: list[int],
) -> None:
    if not imported_ids:
        return
    placeholders = ','.join('?' for _ in imported_ids)
    invalid = connection.execute(
        f'''
        SELECT COUNT(*)
        FROM imported_products AS imported
        LEFT JOIN supplier_products AS source
          ON source.id = imported.supplier_product_id
        WHERE imported.id IN ({placeholders})
          AND (
               source.id IS NULL
            OR source.supplier_id != imported.supplier_id
            OR source.external_id != imported.external_id
          )
        ''',
        imported_ids,
    ).fetchone()[0]
    if invalid:
        raise sqlite3.IntegrityError(
            f'exact imported supplier link validation failed for {invalid} rows'
        )


def migrate(
    db_path: str | Path,
    *,
    batch_size: int = BATCH_SIZE,
) -> dict[str, int]:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) \
            or batch_size < 1 or batch_size > 5000:
        raise ValueError('batch_size must be between 1 and 5000')
    connection = sqlite3.connect(str(db_path), timeout=30)
    try:
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA busy_timeout = 30000')
        _require_schema(connection)
        linked = 0
        batches = 0
        while True:
            rows = _next_batch(connection, batch_size)
            if not rows:
                break
            cursor = connection.executemany(
                '''
                UPDATE imported_products
                   SET supplier_product_id = ?
                 WHERE id = ?
                   AND supplier_product_id IS NULL
                ''',
                [(supplier_product_id, imported_id)
                 for imported_id, supplier_product_id in rows],
            )
            _validate_batch(connection, [row[0] for row in rows])
            connection.commit()
            linked += max(0, int(cursor.rowcount or 0))
            batches += 1
        remaining = connection.execute(
            '''
            SELECT COUNT(*)
            FROM imported_products AS imported
            JOIN supplier_products AS source
              ON source.supplier_id = imported.supplier_id
             AND source.external_id = imported.external_id
            WHERE imported.supplier_product_id IS NULL
              AND imported.supplier_id IS NOT NULL
              AND imported.external_id IS NOT NULL
              AND imported.external_id != ''
            ''',
        ).fetchone()[0]
        if remaining:
            raise sqlite3.IntegrityError(
                f'{remaining} exact imported supplier links remain unfilled'
            )
        return {'linked': linked, 'batches': batches, 'remaining': 0}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    if len(sys.argv) != 2:
        print('Usage: migrate_backfill_imported_supplier_links.py /path/to/db')
        return 2
    db_path = Path(sys.argv[1])
    if not db_path.exists():
        print(f'Database not found: {db_path}')
        return 1
    result = migrate(db_path)
    print(
        'imported supplier links: '
        f"linked={result['linked']} batches={result['batches']} "
        f"remaining={result['remaining']}"
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
