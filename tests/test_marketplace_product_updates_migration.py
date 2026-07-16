# -*- coding: utf-8 -*-
"""Product-update journal expansion preserves existing P5/P6 audit rows."""

import sqlite3

from migrations.migrate_add_marketplace_commercial import (
    apply_migration as apply_commercial,
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
from migrations.migrate_add_marketplace_product_updates import apply_migration
from migrations.migrate_add_ozon_references import (
    apply_migration as apply_references,
)
from tests.test_marketplace_commercial_migration import (
    _base_schema,
    _seed_p5a_row,
)


def test_product_update_migration_preserves_operations_snapshots_and_children():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _base_schema(connection)
        apply_references(connection, verbose=False)
        apply_listings(connection, verbose=False)
        apply_drafts(connection, verbose=False)
        apply_operations(connection, verbose=False)
        marketplace_id, account_id, listing_id, old_operation_id = _seed_p5a_row(
            connection
        )
        apply_commercial(connection, verbose=False)
        connection.execute('''
            INSERT INTO marketplace_commercial_proposals (
                seller_id, marketplace_id, account_id, listing_id,
                operation_id, proposal_kind, source, status, idempotency_key,
                request_fingerprint, contract_version,
                baseline_fingerprint, proposed_fingerprint,
                baseline_state_json, proposed_state_json
            ) VALUES (
                1, ?, ?, ?, ?, 'price', 'user', 'applied',
                'preserved-proposal-0001', ?, 'price-v1', ?, ?, '{}', '{}'
            )
        ''', (
            marketplace_id,
            account_id,
            listing_id,
            old_operation_id,
            "c" * 64,
            "d" * 64,
            "e" * 64,
        ))
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")

        first = apply_migration(connection, verbose=False)
        second = apply_migration(connection, verbose=False)
        connection.commit()

        preserved = connection.execute('''
            SELECT operation_kind, status FROM marketplace_operations WHERE id=?
        ''', (old_operation_id,)).fetchone()
        proposal_parent = connection.execute('''
            SELECT operation_id FROM marketplace_commercial_proposals
            WHERE idempotency_key='preserved-proposal-0001'
        ''').fetchone()[0]
        proposal_fk_target = {
            row[2]
            for row in connection.execute(
                "PRAGMA foreign_key_list(marketplace_commercial_proposals)"
            ).fetchall()
        }

        connection.execute('''
            INSERT INTO marketplace_operations (
                seller_id, marketplace_id, account_id, listing_id,
                operation_kind, status, idempotency_key,
                request_fingerprint, contract_version
            ) VALUES (
                1, ?, ?, ?, 'product_update', 'queued',
                'product-update-0001', ?, 'product-full-state-v1'
            )
        ''', (marketplace_id, account_id, listing_id, "f" * 64))
        update_id = connection.execute(
            "SELECT id FROM marketplace_operations WHERE operation_kind='product_update'"
        ).fetchone()[0]
        connection.execute('''
            INSERT INTO marketplace_listing_snapshots (
                seller_id, marketplace_id, account_id, operation_id, listing_id,
                snapshot_kind, source_fingerprint, before_fingerprint,
                submitted_fingerprint, before_state_json, submitted_state_json,
                rollback_status
            ) VALUES (
                1, ?, ?, ?, ?, 'product_update', ?, ?, ?, '{}', '{}',
                'not_requested'
            )
        ''', (
            marketplace_id,
            account_id,
            update_id,
            listing_id,
            "1" * 64,
            "2" * 64,
            "3" * 64,
        ))
        invalid_failed = False
        try:
            connection.execute('''
                INSERT INTO marketplace_operations (
                    seller_id, marketplace_id, account_id, listing_id,
                    operation_kind, status, idempotency_key,
                    request_fingerprint, contract_version
                ) VALUES (
                    1, ?, ?, ?, 'product_patch_unsafe', 'queued',
                    'unsafe-patch-0001', ?, 'unsafe-v1'
                )
            ''', (marketplace_id, account_id, listing_id, "4" * 64))
        except sqlite3.IntegrityError:
            invalid_failed = True
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert first > 0
    assert second == 0
    assert tuple(preserved) == ("product_import", "succeeded")
    assert proposal_parent == old_operation_id
    assert "marketplace_operations" in proposal_fk_target
    assert invalid_failed
    assert violations == []
