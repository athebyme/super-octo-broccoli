# -*- coding: utf-8 -*-
"""Админский сервис compliance-дефолтов Ozon."""
import unittest

from flask import Flask
from sqlalchemy import event


class SaveDecisionValidationTestCase(unittest.TestCase):
    def test_empty_rationale_is_rejected(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            validate_decision_input,
        )
        with self.assertRaises(OzonComplianceAdminError):
            validate_decision_input(
                product_type_id=1609, tnved_code='3307900008', rationale='  ',
            )

    def test_non_numeric_code_is_rejected(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            validate_decision_input,
        )
        with self.assertRaises(OzonComplianceAdminError):
            validate_decision_input(
                product_type_id=1609, tnved_code='нет кода', rationale='ок',
            )

    def test_valid_input_returns_normalized_code(self):
        from services.ozon_compliance_admin import validate_decision_input
        cleaned = validate_decision_input(
            product_type_id=1609,
            tnved_code=' 3307 90 0008 ',
            rationale='Лубриканты, косметические средства',
        )
        self.assertEqual(cleaned['tnved_code'], '3307900008')
        self.assertEqual(cleaned['product_type_id'], 1609)


class _AdminServiceDbTestCase(unittest.TestCase):
    """Общая БД-фикстура: Flask app + in-memory SQLite + минимальные строки.

    Паттерн — как в ``tests/test_ozon_reference_service.py``: реальный
    ``db.session`` под приложением, а не мок. Функции этого сервиса делают
    настоящие ``db.session.flush()``/``commit()``/rollback, и именно это не
    было покрыто тестами при ревью Task 6 (Critical-баг в
    ``activate_registry_version`` был обнаружен только вручную).
    """

    def setUp(self):
        from models import db, Marketplace, User

        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        marketplace = Marketplace(
            name="Ozon", code="ozon", adapter_code="ozon", is_active=True,
        )
        db.session.add(marketplace)
        user = User(
            username="admin", email="admin@example.com", password_hash="x",
        )
        db.session.add(user)
        db.session.commit()
        self.marketplace_id = marketplace.id
        self.user_id = user.id
        self._next_imported_product_id = 1

    def tearDown(self):
        from models import db

        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _make_product_type(self, external_type_id, name="Тип"):
        from models import db, MarketplaceProductType

        product_type = MarketplaceProductType(
            marketplace_id=self.marketplace_id,
            category_id=1,
            external_type_id=external_type_id,
            name=name,
        )
        db.session.add(product_type)
        db.session.commit()
        return product_type

    def _make_draft(self, product_type_id, offer_id):
        """Минимальная строка ``marketplace_product_drafts`` — нужна лишь для
        того, чтобы product type попал в ``EXISTS`` из ``TYPE_ROWS_SQL``.
        FK-контроль в тестовой SQLite не включён (нет ``PRAGMA
        foreign_keys``), поэтому ``account_id``/``imported_product_id`` можно
        не подкреплять реальными строками — но ``imported_product_id`` всё
        равно обязан быть уникальным в рамках ``account_id`` (constraint
        модели), поэтому берём монотонно растущий счётчик.
        """
        from models import db, MarketplaceProductDraft

        imported_product_id = self._next_imported_product_id
        self._next_imported_product_id += 1
        draft = MarketplaceProductDraft(
            seller_id=1,
            marketplace_id=self.marketplace_id,
            account_id=1,
            imported_product_id=imported_product_id,
            product_type_id=product_type_id,
            offer_id=offer_id,
            source_fact_hash="fingerprint",
        )
        db.session.add(draft)
        db.session.commit()
        return draft

    def _make_registry_version(
        self, label, *, is_complete, prefixes=(), status="superseded",
    ):
        from models import db, OzonMarkingRegistryVersion, OzonMarkingRule

        version = OzonMarkingRegistryVersion(
            label=label,
            is_complete=is_complete,
            declared_by_user_id=self.user_id,
            status=status,
        )
        db.session.add(version)
        db.session.commit()
        for prefix in prefixes:
            db.session.add(OzonMarkingRule(
                registry_version_id=version.id, code_prefix=prefix,
            ))
        db.session.commit()
        return version


class ActivateRegistryVersionTestCase(_AdminServiceDbTestCase):
    def test_reactivating_an_older_lower_id_version_succeeds(self):
        """Critical (ревью Task 6): активация версии-кандидата с id МЕНЬШЕ,
        чем у текущей активной, раньше гарантированно падала с
        ``IntegrityError`` — unit of work SQLAlchemy сортирует "грязные"
        объекты одной таблицы по возрастанию PK и мог отправить UPDATE
        кандидата (``status='active'``) раньше UPDATE "снять активность" со
        старой строки, из-за чего на мгновение существовали две активные
        строки и partial-unique индекс отклонял транзакцию целиком. Это
        ровно сценарий отката к предыдущей версии перечня после того, как в
        новой нашли ошибку.
        """
        from models import OzonMarkingRegistryVersion
        from services.ozon_compliance_admin import activate_registry_version

        v1 = self._make_registry_version("v1", is_complete=True)
        v2 = self._make_registry_version("v2", is_complete=True)
        self.assertLess(v1.id, v2.id)

        activate_registry_version(version_id=v1.id, user_id=self.user_id)
        activate_registry_version(version_id=v2.id, user_id=self.user_id)
        # Откат к БОЛЕЕ СТАРОЙ версии (меньший id) — это и есть Critical.
        result = activate_registry_version(
            version_id=v1.id, user_id=self.user_id,
        )

        self.assertEqual(result.id, v1.id)
        self.assertEqual(result.status, "active")
        actives = OzonMarkingRegistryVersion.query.filter_by(
            status="active",
        ).all()
        self.assertEqual([item.id for item in actives], [v1.id])

        # Сессия не отравлена предыдущей ошибкой — обычный запрос работает.
        self.assertEqual(OzonMarkingRegistryVersion.query.count(), 2)

    def test_unknown_version_id_is_rejected(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            activate_registry_version,
        )
        with self.assertRaises(OzonComplianceAdminError):
            activate_registry_version(version_id=999999, user_id=self.user_id)


class SaveDecisionPersistenceTestCase(_AdminServiceDbTestCase):
    def test_replacing_active_decision_retires_old_and_activates_new(self):
        from models import OzonComplianceDefault
        from services.ozon_compliance_admin import save_decision

        product_type = self._make_product_type("1609")
        self._make_draft(product_type.id, "offer-1")

        first = save_decision(
            product_type_id=product_type.id,
            tnved_code="3307900008",
            rationale="Начальное решение",
            user_id=self.user_id,
        )
        second = save_decision(
            product_type_id=product_type.id,
            tnved_code="6402990000",
            rationale="Исправленное решение",
            user_id=self.user_id,
        )

        self.assertNotEqual(first.id, second.id)
        refreshed_first = OzonComplianceDefault.query.get(first.id)
        self.assertEqual(refreshed_first.status, "retired")
        self.assertEqual(second.status, "active")

        actives = OzonComplianceDefault.query.filter_by(
            product_type_id=product_type.id, status="active",
        ).all()
        self.assertEqual([item.id for item in actives], [second.id])

    def test_unknown_product_type_is_rejected(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            save_decision,
        )
        with self.assertRaises(OzonComplianceAdminError):
            save_decision(
                product_type_id=999999,
                tnved_code="1234567890",
                rationale="x",
                user_id=self.user_id,
            )


class ListTypeRowsTestCase(_AdminServiceDbTestCase):
    def _seed_types_with_decisions(self, count, *, offset=0):
        from services.ozon_compliance_admin import save_decision

        for index in range(offset, offset + count):
            product_type = self._make_product_type(f"type-{index}")
            self._make_draft(product_type.id, f"offer-{index}")
            save_decision(
                product_type_id=product_type.id,
                tnved_code=f"640299000{index}",
                rationale="test",
                user_id=self.user_id,
            )

    def _call_counting_queries(self):
        from models import db
        from services.ozon_compliance_admin import list_type_rows

        counter = {"n": 0}

        def _before_cursor_execute(*args, **kwargs):
            counter["n"] += 1

        event.listen(db.engine, "before_cursor_execute", _before_cursor_execute)
        try:
            rows = list_type_rows()
        finally:
            event.remove(
                db.engine, "before_cursor_execute", _before_cursor_execute,
            )
        return rows, counter["n"]

    def test_marking_is_resolved_from_active_registry(self):
        self._make_registry_version(
            "actual", is_complete=True, prefixes=["6402"], status="active",
        )
        self._seed_types_with_decisions(2)

        rows, _ = self._call_counting_queries()

        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIs(row["marking"], True)
            self.assertIsNotNone(row["tnved_code"])

    def test_query_count_does_not_grow_with_number_of_types(self):
        """Important (ревью Task 6): измерено 20 строк -> 42 запроса на
        старом коде (`resolve_marking` в цикле per-row). Здесь тот же
        сценарий на реальной БД: число запросов должно быть ОДИНАКОВЫМ и на
        2, и на 7 задействованных типах — иначе это N+1.
        """
        self._make_registry_version(
            "actual", is_complete=True, prefixes=["6402"], status="active",
        )

        self._seed_types_with_decisions(2)
        _, small_count = self._call_counting_queries()

        self._seed_types_with_decisions(5, offset=2)
        _, big_count = self._call_counting_queries()

        self.assertEqual(
            small_count, big_count,
            "list_type_rows должен делать фиксированное число запросов "
            "независимо от числа задействованных типов (N+1 regression)",
        )
        # Достаточно узкая абсолютная граница, чтобы тест не проходил
        # случайно при регрессии на маленьком N (2*7 + запас было бы > 10
        # при возврате к резолву маркировки per-row).
        self.assertLessEqual(big_count, 6)


class CreateRegistryVersionTestCase(_AdminServiceDbTestCase):
    def test_valid_input_creates_superseded_version_with_parsed_rules(self):
        from models import OzonMarkingRule
        from services.ozon_compliance_admin import create_registry_version

        version = create_registry_version(
            label="Перечень 2026-08",
            is_complete=True,
            rules_text=(
                "6402;ПП РФ № 1958 от 05.12.2019;2020-07-01;обувь\n"
                "6403\n"
                "3401\n"
            ),
            user_id=self.user_id,
        )

        self.assertEqual(version.status, "superseded")
        self.assertEqual(version.label, "Перечень 2026-08")
        self.assertTrue(version.is_complete)
        self.assertEqual(version.declared_by_user_id, self.user_id)
        self.assertEqual(version.rule_count, 3)
        self.assertIsNotNone(version.checksum)

        rules = {
            rule.code_prefix: rule
            for rule in OzonMarkingRule.query.filter_by(
                registry_version_id=version.id,
            ).all()
        }
        self.assertEqual(set(rules.keys()), {"6402", "6403", "3401"})
        self.assertEqual(
            rules["6402"].normative_ref, "ПП РФ № 1958 от 05.12.2019",
        )
        self.assertEqual(rules["6402"].note, "обувь")
        self.assertEqual(rules["6402"].valid_from.isoformat(), "2020-07-01")
        self.assertIsNone(rules["6403"].normative_ref)
        self.assertIsNone(rules["6403"].valid_from)

    def test_created_version_is_not_automatically_active(self):
        from models import OzonMarkingRegistryVersion
        from services.ozon_compliance_admin import create_registry_version

        version = create_registry_version(
            label="v1", is_complete=False, rules_text="6402",
            user_id=self.user_id,
        )

        self.assertEqual(version.status, "superseded")
        self.assertIsNone(
            OzonMarkingRegistryVersion.query.filter_by(
                status="active",
            ).first(),
        )

    def test_empty_prefix_is_rejected_with_line_number(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            create_registry_version,
        )
        with self.assertRaises(OzonComplianceAdminError) as ctx:
            create_registry_version(
                label="v1", is_complete=True,
                rules_text="6402\n;пустой код\n6403",
                user_id=self.user_id,
            )
        self.assertIn("Строка 2", str(ctx.exception))

    def test_single_digit_prefix_is_rejected_with_line_number(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            create_registry_version,
        )
        with self.assertRaises(OzonComplianceAdminError) as ctx:
            create_registry_version(
                label="v1", is_complete=True, rules_text="6\n6403",
                user_id=self.user_id,
            )
        self.assertIn("Строка 1", str(ctx.exception))

    def test_duplicate_prefix_is_rejected_with_clear_message(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            create_registry_version,
        )
        with self.assertRaises(OzonComplianceAdminError) as ctx:
            create_registry_version(
                label="v1", is_complete=True,
                rules_text="6402\n6403\n64-02",
                user_id=self.user_id,
            )
        message = str(ctx.exception)
        self.assertIn("Строка 3", message)
        self.assertIn("6402", message)

    def test_comment_and_blank_lines_are_skipped(self):
        from services.ozon_compliance_admin import create_registry_version

        version = create_registry_version(
            label="v1", is_complete=True,
            rules_text="# заголовок\n\n6402\n   \n# ещё комментарий\n6403\n",
            user_id=self.user_id,
        )
        self.assertEqual(version.rule_count, 2)

    def test_exceeding_rule_limit_is_rejected(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            create_registry_version,
        )
        rules_text = "\n".join(f"{1000 + i}" for i in range(5001))
        with self.assertRaises(OzonComplianceAdminError):
            create_registry_version(
                label="v1", is_complete=True, rules_text=rules_text,
                user_id=self.user_id,
            )

    def test_checksum_is_stable_regardless_of_line_order(self):
        from services.ozon_compliance_admin import create_registry_version

        first = create_registry_version(
            label="v1", is_complete=True, rules_text="6402\n6403\n3401",
            user_id=self.user_id,
        )
        second = create_registry_version(
            label="v2", is_complete=True, rules_text="3401\n6403\n6402",
            user_id=self.user_id,
        )
        self.assertEqual(first.checksum, second.checksum)


class PreviewRegistrySwitchTestCase(_AdminServiceDbTestCase):
    def test_preview_counts_changes_without_persisting_anything(self):
        from models import db, OzonMarkingRegistryVersion
        from services.ozon_compliance_admin import (
            preview_registry_switch,
            save_decision,
        )

        active_version = self._make_registry_version(
            "current", is_complete=True, prefixes=["3307"], status="active",
        )
        candidate_version = self._make_registry_version(
            "candidate", is_complete=True, prefixes=["6402"],
        )
        product_type = self._make_product_type("1609")
        self._make_draft(product_type.id, "offer-1")
        save_decision(
            product_type_id=product_type.id,
            tnved_code="6402990000",
            rationale="test",
            user_id=self.user_id,
        )

        counters = preview_registry_switch(candidate_version.id)

        self.assertEqual(counters["to_true"], 1)
        self.assertEqual(sum(counters.values()), 1)

        # Только считает — ничего не добавлено/изменено/удалено в сессии.
        self.assertEqual(list(db.session.new), [])
        self.assertEqual(list(db.session.dirty), [])
        self.assertEqual(list(db.session.deleted), [])

        # И в самой БД ничего не поменялось.
        self.assertEqual(
            OzonMarkingRegistryVersion.query.filter_by(
                status="active",
            ).first().id,
            active_version.id,
        )
        refreshed_candidate = OzonMarkingRegistryVersion.query.get(
            candidate_version.id,
        )
        self.assertEqual(refreshed_candidate.status, "superseded")

    def test_unknown_version_id_is_rejected(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            preview_registry_switch,
        )
        with self.assertRaises(OzonComplianceAdminError):
            preview_registry_switch(999999)


if __name__ == '__main__':
    unittest.main()
