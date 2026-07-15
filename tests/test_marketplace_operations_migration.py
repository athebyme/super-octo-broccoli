# -*- coding: utf-8 -*-
"""Marketplace operation migration is additive, repeatable and constrained."""

import sqlite3

from migrations.migrate_add_marketplace_drafts import (
    apply_migration as apply_drafts,
)
from migrations.migrate_add_marketplace_listings import (
    apply_migration as apply_listings,
)
from migrations.migrate_add_marketplace_operations import apply_migration
from migrations.migrate_add_ozon_references import (
    apply_migration as apply_references,
)


def _base_schema(connection):
    connection.executescript('''
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE suppliers (id INTEGER PRIMARY KEY);
        CREATE TABLE supplier_products (
            id INTEGER PRIMARY KEY,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id)
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            nm_id INTEGER NOT NULL
        );
        CREATE TABLE imported_products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            supplier_product_id INTEGER REFERENCES supplier_products(id),
            supplier_id INTEGER REFERENCES suppliers(id),
            product_id INTEGER REFERENCES products(id)
        );
    ''')


def test_marketplace_operation_migration_is_idempotent_and_enforces_journal_guards():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        _base_schema(connection)
        apply_references(connection, verbose=False)
        apply_listings(connection, verbose=False)
        apply_drafts(connection, verbose=False)
        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)

        connection.executescript('''
            INSERT INTO users(id) VALUES (1);
            INSERT INTO sellers(id) VALUES (1);
            INSERT INTO suppliers(id) VALUES (1);
            INSERT INTO supplier_products(id, supplier_id) VALUES (1, 1);
            INSERT INTO imported_products(
                id, seller_id, supplier_product_id, supplier_id
            ) VALUES (1, 1, 1, 1);
        ''')
        marketplace_id = connection.execute(
            "SELECT id FROM marketplaces WHERE code='ozon'"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO seller_marketplace_accounts (
                seller_id, marketplace_id, external_account_id, label
            ) VALUES (1, ?, 'client', 'Client')
        ''', (marketplace_id,))
        account_id = connection.execute(
            "SELECT id FROM seller_marketplace_accounts"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO marketplace_product_drafts (
                seller_id, marketplace_id, account_id, imported_product_id,
                offer_id, source_fact_hash
            ) VALUES (1, ?, ?, 1, 'offer-1', ?)
        ''', (marketplace_id, account_id, "a" * 64))
        draft_id = connection.execute(
            "SELECT id FROM marketplace_product_drafts"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO marketplace_operations (
                seller_id, marketplace_id, account_id, draft_id,
                created_by_user_id, operation_kind, status, idempotency_key,
                request_fingerprint, contract_version, draft_version
            ) VALUES (
                1, ?, ?, ?, 1, 'product_import', 'queued', 'key-one-00000001',
                ?, 'contract-v1', 1
            )
        ''', (marketplace_id, account_id, draft_id, "b" * 64))
        operation_id = connection.execute(
            "SELECT id FROM marketplace_operations"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO marketplace_listing_snapshots (
                seller_id, marketplace_id, account_id, operation_id, draft_id,
                snapshot_kind, source_fingerprint, submitted_fingerprint,
                submitted_state_json, rollback_status
            ) VALUES (
                1, ?, ?, ?, ?, 'product_import', ?, ?, '{}', 'unavailable'
            )
        ''', (
            marketplace_id,
            account_id,
            operation_id,
            draft_id,
            "a" * 64,
            "b" * 64,
        ))

        active_duplicate_failed = False
        try:
            connection.execute('''
                INSERT INTO marketplace_operations (
                    seller_id, marketplace_id, account_id, draft_id,
                    operation_kind, status, idempotency_key,
                    request_fingerprint, contract_version
                ) VALUES (
                    1, ?, ?, ?, 'product_import', 'polling',
                    'key-two-00000002', ?, 'contract-v1'
                )
            ''', (marketplace_id, account_id, draft_id, "c" * 64))
        except sqlite3.IntegrityError:
            active_duplicate_failed = True

        invalid_status_failed = False
        try:
            connection.execute(
                "UPDATE marketplace_operations SET status='maybe' WHERE id=?",
                (operation_id,),
            )
        except sqlite3.IntegrityError:
            invalid_status_failed = True

        duplicate_snapshot_failed = False
        try:
            connection.execute('''
                INSERT INTO marketplace_listing_snapshots (
                    seller_id, marketplace_id, account_id, operation_id,
                    snapshot_kind, source_fingerprint, submitted_fingerprint,
                    submitted_state_json
                ) VALUES (1, ?, ?, ?, 'product_import', ?, ?, '{}')
            ''', (
                marketplace_id,
                account_id,
                operation_id,
                "a" * 64,
                "b" * 64,
            ))
        except sqlite3.IntegrityError:
            duplicate_snapshot_failed = True

        connection.execute(
            "DELETE FROM marketplace_product_drafts WHERE id=?",
            (draft_id,),
        )
        operation_draft_id = connection.execute(
            "SELECT draft_id FROM marketplace_operations WHERE id=?",
            (operation_id,),
        ).fetchone()[0]
        snapshot_draft_id = connection.execute(
            "SELECT draft_id FROM marketplace_listing_snapshots WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(marketplace_operations)"
            ).fetchall()
        }
    finally:
        connection.close()

    assert first > 0
    assert second == 0
    assert active_duplicate_failed
    assert invalid_status_failed
    assert duplicate_snapshot_failed
    assert operation_draft_id is None
    assert snapshot_draft_id is None
    assert "uq_marketplace_operation_active_draft" in indexes
