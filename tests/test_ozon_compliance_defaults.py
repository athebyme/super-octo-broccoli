# -*- coding: utf-8 -*-
"""Контракт админских compliance-дефолтов Ozon."""
import unittest


class ComplianceModelsTestCase(unittest.TestCase):
    def test_models_expose_admin_owned_decision_fields(self):
        from models import (
            OzonComplianceDefault,
            OzonMarkingRegistryVersion,
            OzonMarkingRule,
        )

        default_columns = set(OzonComplianceDefault.__table__.columns.keys())
        self.assertLessEqual(
            {
                'marketplace_id', 'product_type_id', 'tnved_code',
                'tnved_display', 'status', 'decided_by_user_id', 'decided_at',
                'rationale', 'dictionary_version', 'dictionary_hash', 'version',
            },
            default_columns,
        )

        version_columns = set(
            OzonMarkingRegistryVersion.__table__.columns.keys()
        )
        self.assertLessEqual(
            {
                'label', 'is_complete', 'declared_by_user_id', 'declared_at',
                'rule_count', 'checksum', 'status',
            },
            version_columns,
        )

        rule_columns = set(OzonMarkingRule.__table__.columns.keys())
        self.assertLessEqual(
            {
                'registry_version_id', 'code_prefix', 'normative_ref',
                'valid_from', 'note',
            },
            rule_columns,
        )


class ComplianceMigrationTestCase(unittest.TestCase):
    def test_migration_is_idempotent(self):
        import sqlite3
        import tempfile
        import os
        from migrations.migrate_add_ozon_compliance_defaults import (
            apply_migration,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'test.db')
            con = sqlite3.connect(path)
            con.executescript(
                "CREATE TABLE marketplaces (id INTEGER PRIMARY KEY);"
                "CREATE TABLE users (id INTEGER PRIMARY KEY);"
                "CREATE TABLE marketplace_product_types (id INTEGER PRIMARY KEY);"
            )
            con.commit()
            con.close()

            apply_migration(path)
            apply_migration(path)

            con = sqlite3.connect(path)
            tables = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            con.close()
            self.assertIn('ozon_compliance_defaults', tables)
            self.assertIn('ozon_marking_registry_versions', tables)
            self.assertIn('ozon_marking_rules', tables)


if __name__ == '__main__':
    unittest.main()
