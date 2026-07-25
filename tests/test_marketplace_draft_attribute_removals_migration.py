import sqlite3

from migrations.migrate_add_marketplace_draft_attribute_removals import (
    migrate,
)


def test_attribute_removals_migration_is_additive_and_idempotent(tmp_path):
    database = tmp_path / "draft-removals.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE marketplace_product_drafts ("
        "id INTEGER PRIMARY KEY"
        ")"
    )
    connection.commit()
    connection.close()

    migrate(str(database))
    migrate(str(database))

    connection = sqlite3.connect(database)
    try:
        columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(marketplace_product_drafts)"
            )
        }
        assert "attribute_removals_json" in columns
        connection.execute(
            "INSERT INTO marketplace_product_drafts (id) VALUES (1)"
        )
        value = connection.execute(
            "SELECT attribute_removals_json "
            "FROM marketplace_product_drafts WHERE id=1"
        ).fetchone()[0]
        assert value == "[]"
    finally:
        connection.close()
