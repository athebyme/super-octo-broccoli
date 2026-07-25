# -*- coding: utf-8 -*-
"""Контракт админских compliance-дефолтов Ozon."""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch


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


class ComplianceORMConstraintTestCase(unittest.TestCase):
    """`db.create_all()` запускается раньше миграций (docker-entrypoint.sh),
    поэтому реальную схему в проде создаёт ORM. `CREATE TABLE IF NOT EXISTS`
    в миграции после этого — no-op, а CHECK нельзя добавить `ALTER TABLE`.
    Значит CHECK/partial-unique обязаны быть объявлены в самих моделях
    (`__table_args__`), а не только в тексте миграции — эти тесты создают
    таблицы буквально через `db.metadata.create_all()` (не через миграцию) и
    доказывают, что constraints реально работают на такой схеме.
    """

    def _create_via_orm_metadata(self, *tables):
        import sqlalchemy as sa
        from models import db

        engine = sa.create_engine('sqlite:///:memory:')
        db.metadata.create_all(bind=engine, tables=list(tables))
        return engine

    def _default_row(self, **overrides):
        row = {
            'marketplace_id': 1,
            'product_type_id': 1,
            'tnved_code': '1234567890',
            'decided_by_user_id': 1,
            'decided_at': datetime.utcnow(),
            'rationale': 'test rationale',
            'version': 1,
        }
        row.update(overrides)
        return row

    def _registry_row(self, **overrides):
        row = {
            'label': 'v1',
            'declared_by_user_id': 1,
            'declared_at': datetime.utcnow(),
            'rule_count': 0,
        }
        row.update(overrides)
        return row

    def _row_count(self, engine, table):
        import sqlalchemy as sa

        with engine.connect() as conn:
            return conn.execute(
                sa.select(sa.func.count()).select_from(table)
            ).scalar()

    def test_orm_created_table_rejects_invalid_compliance_default_status(self):
        import sqlalchemy as sa
        from models import OzonComplianceDefault

        engine = self._create_via_orm_metadata(OzonComplianceDefault.__table__)
        with engine.connect() as conn:
            with self.assertRaises(sa.exc.IntegrityError):
                conn.execute(
                    OzonComplianceDefault.__table__.insert(),
                    self._default_row(status='inactive'),
                )
                conn.commit()

    def test_orm_created_table_rejects_invalid_registry_version_status(self):
        import sqlalchemy as sa
        from models import OzonMarkingRegistryVersion

        engine = self._create_via_orm_metadata(
            OzonMarkingRegistryVersion.__table__
        )
        with engine.connect() as conn:
            with self.assertRaises(sa.exc.IntegrityError):
                conn.execute(
                    OzonMarkingRegistryVersion.__table__.insert(),
                    self._registry_row(status='draft'),
                )
                conn.commit()

    def test_orm_created_table_enforces_one_active_default_per_scope(self):
        import sqlalchemy as sa
        from models import OzonComplianceDefault

        engine = self._create_via_orm_metadata(OzonComplianceDefault.__table__)
        with engine.connect() as conn:
            conn.execute(
                OzonComplianceDefault.__table__.insert(),
                self._default_row(status='active'),
            )
            conn.commit()
            with self.assertRaises(sa.exc.IntegrityError):
                conn.execute(
                    OzonComplianceDefault.__table__.insert(),
                    self._default_row(status='active', tnved_code='9999999999'),
                )
                conn.commit()

    def test_orm_created_table_allows_active_and_retired_same_scope(self):
        from models import OzonComplianceDefault

        engine = self._create_via_orm_metadata(OzonComplianceDefault.__table__)
        with engine.connect() as conn:
            conn.execute(
                OzonComplianceDefault.__table__.insert(),
                self._default_row(status='active'),
            )
            conn.execute(
                OzonComplianceDefault.__table__.insert(),
                self._default_row(status='retired', tnved_code='9999999999'),
            )
            conn.commit()

        self.assertEqual(
            self._row_count(engine, OzonComplianceDefault.__table__), 2,
        )

    def test_orm_created_table_enforces_single_active_registry_version(self):
        import sqlalchemy as sa
        from models import OzonMarkingRegistryVersion

        engine = self._create_via_orm_metadata(
            OzonMarkingRegistryVersion.__table__
        )
        with engine.connect() as conn:
            conn.execute(
                OzonMarkingRegistryVersion.__table__.insert(),
                self._registry_row(status='active', label='v1'),
            )
            conn.commit()
            with self.assertRaises(sa.exc.IntegrityError):
                conn.execute(
                    OzonMarkingRegistryVersion.__table__.insert(),
                    self._registry_row(status='active', label='v2'),
                )
                conn.commit()

    def test_orm_created_table_allows_active_and_superseded_registry_versions(
        self,
    ):
        from models import OzonMarkingRegistryVersion

        engine = self._create_via_orm_metadata(
            OzonMarkingRegistryVersion.__table__
        )
        with engine.connect() as conn:
            conn.execute(
                OzonMarkingRegistryVersion.__table__.insert(),
                self._registry_row(status='active', label='v1'),
            )
            conn.execute(
                OzonMarkingRegistryVersion.__table__.insert(),
                self._registry_row(status='superseded', label='v0'),
            )
            conn.commit()

        self.assertEqual(
            self._row_count(engine, OzonMarkingRegistryVersion.__table__), 2,
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


class ComplianceMigrationMainTestCase(unittest.TestCase):
    def test_main_reports_missing_db_without_creating_it(self):
        import os
        import sys
        import tempfile
        from unittest.mock import patch
        from migrations import migrate_add_ozon_compliance_defaults as mod

        with tempfile.TemporaryDirectory() as tmp:
            missing_path = os.path.join(tmp, 'does-not-exist.db')
            with patch.object(
                sys, 'argv',
                ['migrate_add_ozon_compliance_defaults.py', missing_path],
            ):
                exit_code = mod.main()
            self.assertEqual(exit_code, 1)
            self.assertFalse(
                os.path.exists(missing_path),
                'sqlite3.connect() must not silently create the DB file',
            )


class TnvedCodeParsingTestCase(unittest.TestCase):
    def test_normalize_keeps_digits_only(self):
        from services.ozon_compliance_defaults import normalize_code
        self.assertEqual(normalize_code(' 3307 90 000 8 '), '3307900008')
        self.assertEqual(normalize_code('6402-99'), '640299')
        self.assertEqual(normalize_code(''), '')
        self.assertEqual(normalize_code(None), '')

    def test_dictionary_code_takes_leading_digits(self):
        from services.ozon_compliance_defaults import dictionary_code
        self.assertEqual(
            dictionary_code('3307900008 - Косметические средства'),
            '3307900008',
        )
        self.assertEqual(dictionary_code('6402990000'), '6402990000')
        self.assertEqual(dictionary_code('Без кода'), '')
        self.assertEqual(dictionary_code(None), '')


def _definition(*, fresh=True, available=True, restriction=None):
    defn = MagicMock()
    defn.id = 678
    defn.external_attribute_id = '22232'
    defn.is_available = available
    defn.dictionary_id = '124412395'
    defn.values_version = 3
    defn.restriction_value_ids = (
        list(restriction) if restriction is not None else []
    )
    return defn


def _value_row(external_value_id, value):
    row = MagicMock()
    row.external_value_id = external_value_id
    row.value = value
    return row


class ResolveTnvedTestCase(unittest.TestCase):
    def _run(self, *, default, definition, rows, fresh=True):
        module = 'services.ozon_compliance_defaults'
        with patch(f'{module}._active_default', return_value=default), \
             patch(f'{module}._tnved_definition', return_value=definition), \
             patch(f'{module}._dictionary_rows', return_value=rows), \
             patch(f'{module}._dictionary_is_fresh', return_value=fresh):
            from services.ozon_compliance_defaults import resolve_tnved
            return resolve_tnved(1609)

    def _default(self, code='3307900008'):
        obj = MagicMock()
        obj.id = 7
        obj.tnved_code = code
        obj.dictionary_version = 3
        return obj

    def test_exact_single_match_resolves_value_id(self):
        result = self._run(
            default=self._default(),
            definition=_definition(),
            rows=[
                _value_row('971397774', '3403990000 - Прочие смазочные'),
                _value_row('971397758', '3307900008 - Косметические средства'),
            ],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['code'], '3307900008')
        self.assertEqual(result['external_value_id'], '971397758')
        self.assertEqual(result['value'], '3307900008 - Косметические средства')

    def test_missing_decision_returns_none(self):
        self.assertIsNone(
            self._run(default=None, definition=_definition(), rows=[])
        )

    def test_stale_dictionary_returns_none(self):
        self.assertIsNone(self._run(
            default=self._default(),
            definition=_definition(),
            rows=[_value_row('1', '3307900008 - X')],
            fresh=False,
        ))

    def test_code_absent_from_dictionary_returns_none(self):
        self.assertIsNone(self._run(
            default=self._default(code='9999999999'),
            definition=_definition(),
            rows=[_value_row('1', '3307900008 - X')],
        ))

    def test_duplicate_code_in_dictionary_returns_none(self):
        self.assertIsNone(self._run(
            default=self._default(),
            definition=_definition(),
            rows=[
                _value_row('1', '3307900008 - X'),
                _value_row('2', '3307900008 - Y'),
            ],
        ))

    def test_empty_allowlist_does_not_restrict(self):
        result = self._run(
            default=self._default(),
            definition=_definition(restriction=[]),
            rows=[
                _value_row('971397774', '3403990000 - Прочие смазочные'),
                _value_row('971397758', '3307900008 - Косметические средства'),
            ],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['external_value_id'], '971397758')

    def test_allowlist_permits_matching_value_id(self):
        result = self._run(
            default=self._default(),
            definition=_definition(restriction=['971397758']),
            rows=[_value_row('971397758', '3307900008 - Косметические средства')],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['external_value_id'], '971397758')

    def test_allowlist_blocks_non_allowed_value_id(self):
        result = self._run(
            default=self._default(),
            definition=_definition(restriction=['999999999']),
            rows=[_value_row('971397758', '3307900008 - Косметические средства')],
        )
        self.assertIsNone(result)

    def test_allowlist_narrows_duplicate_matches_to_single_allowed(self):
        result = self._run(
            default=self._default(),
            definition=_definition(restriction=['971397758']),
            rows=[
                _value_row('971397758', '3307900008 - Косметические A'),
                _value_row('971397759', '3307900008 - Косметические B'),
            ],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['external_value_id'], '971397758')


if __name__ == '__main__':
    unittest.main()
