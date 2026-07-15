# -*- coding: utf-8 -*-
"""Canonical content proposal migration is additive and fail-closed."""

import sqlite3
import unittest

from migrations.migrate_add_marketplace_canonical_content import apply_migration


class MarketplaceCanonicalContentMigrationTest(unittest.TestCase):
    @staticmethod
    def _base_schema(connection):
        connection.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE sellers (id INTEGER PRIMARY KEY);
            CREATE TABLE marketplaces (
                id INTEGER PRIMARY KEY,
                code TEXT NOT NULL
            );
            CREATE TABLE seller_marketplace_accounts (
                id INTEGER PRIMARY KEY,
                seller_id INTEGER NOT NULL REFERENCES sellers(id),
                marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id)
            );
            CREATE TABLE imported_products (
                id INTEGER PRIMARY KEY,
                seller_id INTEGER NOT NULL REFERENCES sellers(id)
            );
            CREATE TABLE marketplace_listings (
                id INTEGER PRIMARY KEY,
                seller_id INTEGER NOT NULL REFERENCES sellers(id),
                marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
                account_id INTEGER REFERENCES seller_marketplace_accounts(id),
                imported_product_id INTEGER REFERENCES imported_products(id)
            );
            CREATE TABLE agent_change_snapshots (
                id INTEGER PRIMARY KEY,
                imported_product_id INTEGER NOT NULL REFERENCES imported_products(id)
            );
        ''')

    @staticmethod
    def _seed(connection):
        connection.executescript('''
            INSERT INTO users(id) VALUES (1);
            INSERT INTO sellers(id) VALUES (1);
            INSERT INTO marketplaces(id, code) VALUES (2, 'ozon');
            INSERT INTO seller_marketplace_accounts(
                id, seller_id, marketplace_id
            ) VALUES (3, 1, 2);
            INSERT INTO imported_products(id, seller_id) VALUES (4, 1);
            INSERT INTO marketplace_listings(
                id, seller_id, marketplace_id, account_id, imported_product_id
            ) VALUES (5, 1, 2, 3, 4);
        ''')

    @staticmethod
    def _insert_proposal(connection, *, status="pending_review"):
        connection.execute('''
            INSERT INTO marketplace_canonical_content_proposals (
                seller_id, marketplace_id, account_id, listing_id,
                imported_product_id, created_by_user_id, status, fields_json,
                baseline_state_json, proposed_state_json,
                baseline_fingerprint, source_fingerprint, contract_version,
                source_observed_at
            ) VALUES (
                1, 2, 3, 5, 4, 1, ?, '["title"]',
                '{"title":"before"}', '{"title":"after"}', ?, ?,
                'ozon-canonical-common-content-v1', CURRENT_TIMESTAMP
            )
        ''', (status, "a" * 64, "b" * 64))

    def test_schema_is_idempotent_and_allows_only_one_pending_per_listing(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            self._base_schema(connection)
            self._seed(connection)
            first = apply_migration(connection, verbose=False)
            second = apply_migration(connection, verbose=False)
            self._insert_proposal(connection)

            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_proposal(connection)
            connection.rollback()

            # The rollback removed the first uncommitted row; exercise the
            # lifecycle again with explicit commits.
            self._insert_proposal(connection)
            connection.commit()
            connection.execute('''
                UPDATE marketplace_canonical_content_proposals
                SET status='applied' WHERE listing_id=5
            ''')
            self._insert_proposal(connection)
            connection.commit()

            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(marketplace_canonical_content_proposals)"
                ).fetchall()
            }
            indexes = {
                row[1]
                for row in connection.execute(
                    "PRAGMA index_list(marketplace_canonical_content_proposals)"
                ).fetchall()
            }
            statuses = [
                row[0]
                for row in connection.execute('''
                    SELECT status
                    FROM marketplace_canonical_content_proposals
                    ORDER BY id
                ''').fetchall()
            ]
            violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        finally:
            connection.close()

        self.assertGreater(first, 0)
        self.assertEqual(second, 0)
        self.assertIn("source_observed_at", columns)
        self.assertIn("snapshot_id", columns)
        self.assertIn("uq_marketplace_canonical_content_pending", indexes)
        self.assertEqual(statuses, ["applied", "pending_review"])
        self.assertEqual(violations, [])

    def test_status_version_and_foreign_keys_are_enforced(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            self._base_schema(connection)
            self._seed(connection)
            apply_migration(connection, verbose=False)
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_proposal(connection, status="auto_applied")
            connection.rollback()

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute('''
                    INSERT INTO marketplace_canonical_content_proposals (
                        seller_id, marketplace_id, account_id, listing_id,
                        imported_product_id, status, fields_json,
                        baseline_state_json, proposed_state_json,
                        baseline_fingerprint, source_fingerprint,
                        source_observed_at, version
                    ) VALUES (
                        1, 2, 3, 999, 4, 'pending_review', '["title"]',
                        '{}', '{}', ?, ?, CURRENT_TIMESTAMP, 0
                    )
                ''', ("c" * 64, "d" * 64))
        finally:
            connection.close()

    def test_missing_prerequisite_fails_before_creating_table(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE sellers (id INTEGER PRIMARY KEY)")
            with self.assertRaises(sqlite3.OperationalError):
                apply_migration(connection, verbose=False)
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                "name='marketplace_canonical_content_proposals'"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(exists)


if __name__ == "__main__":
    unittest.main()
