# -*- coding: utf-8 -*-
"""Schema invariants for shared competitor-to-supplier matching."""
import sqlite3
import unittest

from migrations.migrate_add_competitor_matching import apply_migration


def _prerequisites(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE supplier_products (id INTEGER PRIMARY KEY);
        CREATE TABLE competitor_products (
            id INTEGER PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            nm_id BIGINT NOT NULL
        );
        CREATE TABLE background_jobs (
            id INTEGER PRIMARY KEY,
            job_uid VARCHAR(64) NOT NULL,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            job_type VARCHAR(50) NOT NULL,
            status VARCHAR(20) NOT NULL,
            progress_data TEXT
        );
    """)


class CompetitorMatchingMigrationTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.execute('PRAGMA foreign_keys=ON')
        _prerequisites(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_idempotent_constrained_and_foreign_key_safe(self):
        first = apply_migration(self.connection, verbose=False)
        second = apply_migration(self.connection, verbose=False)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0)

        product_columns = {
            row[1] for row in self.connection.execute(
                'PRAGMA table_info(competitor_products)')
        }
        self.assertTrue({
            'subject_id', 'subject_name', 'photo_count',
            'characteristics_json',
        }.issubset(product_columns))

        self.connection.executescript("""
            INSERT INTO users(id) VALUES (1);
            INSERT INTO sellers(id) VALUES (10), (20);
            INSERT INTO supplier_products(id) VALUES (100), (200);
            INSERT INTO competitor_product_matches (
                id, nm_id, suggested_supplier_product_id,
                processing_status, algorithm_version, llm_status
            ) VALUES (
                1000, 777001, 100, 'completed',
                'supplier-observed-v1', 'completed'
            );
            INSERT INTO seller_competitor_match_reviews (
                seller_id, match_id, supplier_product_id,
                status, match_type, actor_user_id
            ) VALUES (10, 1000, 100, 'confirmed', 'same', 1);
            INSERT INTO competitor_match_events (
                seller_id, match_id, supplier_product_id,
                action, match_type, actor_user_id
            ) VALUES (10, 1000, 100, 'confirm', 'same', 1);
        """)

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("""
                INSERT INTO competitor_product_matches (
                    nm_id, processing_status, algorithm_version, llm_status
                ) VALUES (
                    777001, 'queued', 'supplier-observed-v1', 'pending'
                )
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("""
                INSERT INTO competitor_product_matches (
                    nm_id, processing_status, algorithm_version,
                    llm_status, final_score
                ) VALUES (
                    777002, 'queued', 'supplier-observed-v1', 'pending', 101
                )
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("""
                INSERT INTO seller_competitor_match_reviews (
                    seller_id, match_id, status
                ) VALUES (20, 1000, 'maybe')
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("""
                INSERT INTO seller_competitor_match_reviews (
                    seller_id, match_id, status
                ) VALUES (10, 1000, 'rejected')
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("""
                INSERT INTO competitor_product_matches (
                    nm_id, suggested_supplier_product_id,
                    processing_status, algorithm_version, llm_status
                ) VALUES (
                    777003, 999, 'queued',
                    'supplier-observed-v1', 'pending'
                )
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("""
                INSERT INTO competitor_match_events (
                    seller_id, match_id, action
                ) VALUES (10, 1000, 'overwrite_shared')
            """)

        self.assertEqual(
            self.connection.execute('PRAGMA foreign_key_check').fetchall(),
            [],
        )

    def test_missing_prerequisite_fails_closed(self):
        connection = sqlite3.connect(':memory:')
        try:
            with self.assertRaisesRegex(
                sqlite3.OperationalError,
                'prerequisite missing',
            ):
                apply_migration(connection, verbose=False)
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
