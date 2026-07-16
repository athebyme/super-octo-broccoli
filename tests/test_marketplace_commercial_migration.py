# -*- coding: utf-8 -*-
"""Commercial marketplace migration preserves P5a and adds strict scopes."""

import sqlite3

from migrations.migrate_add_marketplace_commercial import (
    _create_commercial_tables,
    apply_migration,
)
from migrations.migrate_add_marketplace_drafts import (
    apply_migration as apply_drafts,
)
from migrations.migrate_add_marketplace_listings import (
    apply_migration as apply_listings,
)
from migrations.migrate_add_marketplace_operations import (
    apply_migration as apply_operations,
)
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
        CREATE TABLE agent_tasks (id INTEGER PRIMARY KEY);
    ''')


def _seed_p5a_row(connection):
    connection.executescript('''
        INSERT INTO users(id) VALUES (1);
        INSERT INTO sellers(id) VALUES (1);
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
        INSERT INTO marketplace_listings (
            seller_id, marketplace_id, account_id, offer_id,
            external_product_id, sync_fingerprint
        ) VALUES (1, ?, ?, 'offer-1', '101', ?)
    ''', (marketplace_id, account_id, "a" * 64))
    listing_id = connection.execute(
        "SELECT id FROM marketplace_listings"
    ).fetchone()[0]
    connection.execute('''
        INSERT INTO marketplace_operations (
            seller_id, marketplace_id, account_id, listing_id,
            operation_kind, status, idempotency_key,
            request_fingerprint, contract_version
        ) VALUES (
            1, ?, ?, ?, 'product_import', 'succeeded',
            'old-operation-0001', ?, 'product-import-v1'
        )
    ''', (marketplace_id, account_id, listing_id, "b" * 64))
    operation_id = connection.execute(
        "SELECT id FROM marketplace_operations"
    ).fetchone()[0]
    connection.execute('''
        INSERT INTO marketplace_listing_snapshots (
            seller_id, marketplace_id, account_id, operation_id, listing_id,
            snapshot_kind, source_fingerprint, submitted_fingerprint,
            submitted_state_json, rollback_status
        ) VALUES (
            1, ?, ?, ?, ?, 'product_import', ?, ?, '{}', 'unavailable'
        )
    ''', (
        marketplace_id,
        account_id,
        operation_id,
        listing_id,
        "a" * 64,
        "b" * 64,
    ))
    return marketplace_id, account_id, listing_id, operation_id


def test_commercial_migration_rebuilds_checks_without_losing_p5a_rows():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        _base_schema(connection)
        apply_references(connection, verbose=False)
        apply_listings(connection, verbose=False)
        apply_drafts(connection, verbose=False)
        apply_operations(connection, verbose=False)
        marketplace_id, account_id, listing_id, old_operation_id = _seed_p5a_row(
            connection
        )

        # Docker runs db.create_all() before scripts. Reproduce the important
        # case: an empty new child table already references the P5a parent.
        _create_commercial_tables(connection)
        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)

        preserved = connection.execute('''
            SELECT operation_kind, status
            FROM marketplace_operations WHERE id=?
        ''', (old_operation_id,)).fetchone()
        preserved_snapshot = connection.execute('''
            SELECT snapshot_kind
            FROM marketplace_listing_snapshots WHERE operation_id=?
        ''', (old_operation_id,)).fetchone()

        connection.execute('''
            INSERT INTO marketplace_operations (
                seller_id, marketplace_id, account_id, listing_id,
                operation_kind, status, idempotency_key,
                request_fingerprint, contract_version
            ) VALUES (
                1, ?, ?, ?, 'price_update', 'queued',
                'price-operation-0001', ?, 'price-v1'
            )
        ''', (marketplace_id, account_id, listing_id, "c" * 64))
        price_operation_id = connection.execute(
            "SELECT id FROM marketplace_operations WHERE operation_kind='price_update'"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO marketplace_listing_snapshots (
                seller_id, marketplace_id, account_id, operation_id, listing_id,
                snapshot_kind, source_fingerprint, before_fingerprint,
                submitted_fingerprint, before_state_json, submitted_state_json,
                rollback_status
            ) VALUES (
                1, ?, ?, ?, ?, 'price', ?, ?, ?, '{}', '{}', 'available'
            )
        ''', (
            marketplace_id,
            account_id,
            price_operation_id,
            listing_id,
            "d" * 64,
            "e" * 64,
            "f" * 64,
        ))

        invalid_kind_failed = False
        try:
            connection.execute('''
                INSERT INTO marketplace_operations (
                    seller_id, marketplace_id, account_id, listing_id,
                    operation_kind, status, idempotency_key,
                    request_fingerprint, contract_version
                ) VALUES (
                    1, ?, ?, ?, 'unsafe_write', 'queued',
                    'unsafe-operation-0001', ?, 'unsafe-v1'
                )
            ''', (marketplace_id, account_id, listing_id, "9" * 64))
        except sqlite3.IntegrityError:
            invalid_kind_failed = True

        connection.execute('''
            INSERT INTO marketplace_warehouses (
                seller_id, marketplace_id, account_id, external_warehouse_id,
                name, sync_fingerprint, last_seen_at, last_synced_at
            ) VALUES (1, ?, ?, '7001', 'Main FBS', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ''', (marketplace_id, account_id, "1" * 64))
        warehouse_id = connection.execute(
            "SELECT id FROM marketplace_warehouses"
        ).fetchone()[0]

        invalid_scope_failed = False
        try:
            connection.execute('''
                INSERT INTO marketplace_commercial_proposals (
                    seller_id, marketplace_id, account_id, listing_id,
                    proposal_kind, source, status, idempotency_key,
                    request_fingerprint, contract_version,
                    baseline_fingerprint, proposed_fingerprint,
                    baseline_state_json, proposed_state_json
                ) VALUES (
                    1, ?, ?, ?, 'stock', 'user', 'pending_review',
                    'invalid-stock-0001', ?, 'stock-v1', ?, ?, '{}', '{}'
                )
            ''', (
                marketplace_id,
                account_id,
                listing_id,
                "2" * 64,
                "3" * 64,
                "4" * 64,
            ))
        except sqlite3.IntegrityError:
            invalid_scope_failed = True

        connection.execute('''
            INSERT INTO marketplace_commercial_proposals (
                seller_id, marketplace_id, account_id, listing_id,
                warehouse_id, proposal_kind, source, status, idempotency_key,
                request_fingerprint, contract_version,
                baseline_fingerprint, proposed_fingerprint,
                baseline_state_json, proposed_state_json
            ) VALUES (
                1, ?, ?, ?, ?, 'stock', 'agent', 'pending_review',
                'valid-stock-0001', ?, 'stock-v1', ?, ?, '{}', '{}'
            )
        ''', (
            marketplace_id,
            account_id,
            listing_id,
            warehouse_id,
            "5" * 64,
            "6" * 64,
            "7" * 64,
        ))
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert first > 0
    assert second == 0
    assert tuple(preserved) == ("product_import", "succeeded")
    assert tuple(preserved_snapshot) == ("product_import",)
    assert invalid_kind_failed
    assert invalid_scope_failed
    assert violations == []
