import sqlite3
from contextlib import closing

import pytest

from migrations.migrate_add_ozon_brand_search_evidence import migrate


def _parents(database):
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript("""
            CREATE TABLE sellers (id INTEGER PRIMARY KEY);
            CREATE TABLE seller_marketplace_accounts (id INTEGER PRIMARY KEY);
            CREATE TABLE marketplace_attribute_values (id INTEGER PRIMARY KEY, value TEXT);
            INSERT INTO sellers VALUES (7);
            INSERT INTO seller_marketplace_accounts VALUES (8);
            INSERT INTO marketplace_attribute_values VALUES (31, 'Existing');
        """)


def test_search_receipt_migration_is_additive_and_keeps_existing_receipts(tmp_path):
    database = tmp_path / "brand-receipts.sqlite"
    _parents(database)
    assert migrate(str(database)) == 3

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO ozon_brand_search_evidence
                (scope_hash, seller_id, account_id, status, reserved_at, expires_at)
               VALUES (?, 7, 8, 'reserved', '2026-11-24 12:00:00', '2026-11-24 12:01:00')""",
            ("a" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE ozon_brand_search_evidence SET complete_result=1")

    assert migrate(str(database)) == 0
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute(
            "SELECT status, choices_json FROM ozon_brand_search_evidence"
        ).fetchall() == [("reserved", "[]")]
        connection.execute("DELETE FROM seller_marketplace_accounts WHERE id=8")
        assert connection.execute("SELECT COUNT(*) FROM ozon_brand_search_evidence").fetchone() == (0,)
        assert connection.execute("SELECT * FROM marketplace_attribute_values").fetchall() == [
            (31, "Existing"),
        ]


def test_search_receipt_migration_rejects_extra_index_without_partial_ddl(tmp_path):
    database = tmp_path / "brand-invalid.sqlite"
    _parents(database)
    migrate(str(database))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP INDEX ix_ozon_brand_search_account")
        connection.execute(
            "CREATE INDEX unexpected_search_index ON ozon_brand_search_evidence(expires_at)"
        )

    with pytest.raises(sqlite3.OperationalError, match="unexpected .* schema objects"):
        migrate(str(database))
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name='ozon_brand_search_evidence' "
            "AND type='index' AND sql IS NOT NULL ORDER BY name"
        ).fetchall() == [("ix_ozon_brand_search_seller",), ("unexpected_search_index",)]
