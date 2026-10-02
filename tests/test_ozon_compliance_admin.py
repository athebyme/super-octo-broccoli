# -*- coding: utf-8 -*-
"""Админский сервис compliance-дефолтов Ozon."""
from datetime import date, datetime
import unittest
from unittest.mock import MagicMock

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

        # ``categories_synced_at``/``categories_snapshot_hash`` — часть
        # ``OzonReferenceService.reference_is_fresh``, которую
        # ``dictionary_is_fresh`` проверяет через ``product_type.marketplace``.
        # Задано один раз здесь для всех типов этого маркетплейса, а не в
        # каждом тесте по отдельности.
        marketplace = Marketplace(
            name="Ozon", code="ozon", adapter_code="ozon", is_active=True,
            categories_synced_at=datetime.utcnow(),
            categories_snapshot_hash="fresh-categories-hash",
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
        """Тип с уже свежим ``reference_is_fresh``: реальная категория и
        собственные ``attributes_synced_at``/``attributes_schema_hash``.

        Это НЕ делает свежим словарь конкретного атрибута ТН ВЭД — для этого
        нужен отдельный ``_make_tnved_dictionary``, который создаёт
        ``MarketplaceAttributeDefinition`` со своим ``values_synced_at``.
        Без свежей категории/типа ``dictionary_is_fresh`` был бы false
        независимо от словаря, и все тесты `save_decision` ловили бы новый
        fail-closed отказ «словарь недоступен» вместо своего сценария.
        """
        from models import db, MarketplaceProductType, MarketplaceTaxonomyCategory

        category = MarketplaceTaxonomyCategory(
            marketplace_id=self.marketplace_id,
            external_category_id=f"cat-{external_type_id}",
            name="Категория",
            full_path="Категория",
            is_available=True,
        )
        db.session.add(category)
        db.session.flush()

        product_type = MarketplaceProductType(
            marketplace_id=self.marketplace_id,
            category_id=category.id,
            external_type_id=external_type_id,
            name=name,
            is_available=True,
            attributes_synced_at=datetime.utcnow(),
            attributes_schema_hash=f"fresh-schema-{external_type_id}",
        )
        db.session.add(product_type)
        db.session.commit()
        return product_type

    def _make_tnved_dictionary(self, product_type, codes, *, restriction=None):
        """Создать fresh официальный словарь ТН ВЭД типа с данными кодами.

        ``codes`` — iterable нормализованных кодов (строки цифр); каждому
        присваивается уникальный ``external_value_id`` и значение вида
        ``"<код> - Описание"``, как в наблюдённой форме официального
        словаря. ``restriction`` — опциональный iterable ``external_value_id``
        для проверки admin-restriction сценария.
        """
        from models import db, MarketplaceAttributeDefinition, MarketplaceAttributeValue
        from services.ozon_compliance_defaults import TNVED_ATTRIBUTE_ID
        from services.ozon_reference_service import OzonReferenceService

        definition = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace_id,
            product_type_id=product_type.id,
            external_attribute_id=TNVED_ATTRIBUTE_ID,
            name="ТН ВЭД коды ЕАЭС",
            data_type="String",
            dictionary_id="22232",
            is_available=True,
            values_synced_at=datetime.utcnow(),
            values_snapshot_hash=f"fresh-values-{product_type.id}",
            values_version=1,
        )
        if restriction is not None:
            import json
            definition.restriction_value_ids_json = json.dumps(list(restriction))
        db.session.add(definition)
        db.session.flush()

        for index, code in enumerate(codes, start=1):
            value = f"{code} - Описание {code}"
            db.session.add(MarketplaceAttributeValue(
                marketplace_id=self.marketplace_id,
                product_type_id=product_type.id,
                attribute_id=definition.id,
                external_value_id=str(1000 + index),
                value=value,
                value_normalized=OzonReferenceService.normalize_value(value),
                is_available=True,
            ))
        db.session.commit()
        return definition

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

    def _make_operation(self, draft_id, status, *, operation_kind="product_update"):
        """Минимальная строка ``marketplace_operations``, привязанная к черновику.

        Используется только для проверки, что ``apply_to_existing_drafts``
        пропускает черновик, пока по нему есть НЕтерминальная операция.
        ``idempotency_key`` обязан быть уникальным в рамках
        ``(account_id, operation_kind)`` — берём id черновика и статус, этого
        достаточно для теста.
        """
        from models import db, MarketplaceOperation

        operation = MarketplaceOperation(
            seller_id=1,
            marketplace_id=self.marketplace_id,
            account_id=1,
            draft_id=draft_id,
            operation_kind=operation_kind,
            status=status,
            idempotency_key=f"test-{draft_id}-{status}",
            request_fingerprint="fingerprint",
            contract_version="v1",
        )
        db.session.add(operation)
        db.session.commit()
        return operation


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

    def test_non_numeric_version_id_is_rejected_cleanly(self):
        """Important (ревью Task 7): ``version_id`` приходит из тела формы
        как произвольная строка, не через URL ``<int:...>`` converter.
        ``int("abc")`` до правки ронял запрос необработанным ``ValueError``
        в голый Flask 500 вместо ``OzonComplianceAdminError`` -> flash.
        """
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            activate_registry_version,
        )
        with self.assertRaises(OzonComplianceAdminError):
            activate_registry_version(version_id="abc", user_id=self.user_id)

    def test_missing_version_id_is_rejected_cleanly(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            activate_registry_version,
        )
        with self.assertRaises(OzonComplianceAdminError):
            activate_registry_version(version_id=None, user_id=self.user_id)

    def test_non_positive_version_id_is_rejected_cleanly(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            activate_registry_version,
        )
        with self.assertRaises(OzonComplianceAdminError):
            activate_registry_version(version_id=0, user_id=self.user_id)
        with self.assertRaises(OzonComplianceAdminError):
            activate_registry_version(version_id=-5, user_id=self.user_id)


class SaveDecisionPersistenceTestCase(_AdminServiceDbTestCase):
    @staticmethod
    def _decision_snapshot(decision):
        return (
            decision.id,
            decision.status,
            decision.tnved_code,
            decision.tnved_display,
            decision.decided_by_user_id,
            decision.decided_at,
            decision.rationale,
            decision.dictionary_version,
            decision.dictionary_hash,
            decision.version,
        )

    def _assert_existing_active_unchanged(self, product_type, before):
        from models import OzonComplianceDefault

        active = OzonComplianceDefault.query.filter_by(
            product_type_id=product_type.id, status="active",
        ).all()
        self.assertEqual(len(active), 1)
        self.assertEqual(self._decision_snapshot(active[0]), before)
        self.assertEqual(
            OzonComplianceDefault.query.filter_by(
                product_type_id=product_type.id,
            ).count(),
            1,
        )

    def test_replacing_active_decision_retires_old_and_activates_new(self):
        from models import OzonComplianceDefault
        from services.ozon_compliance_admin import save_decision

        product_type = self._make_product_type("1609")
        self._make_draft(product_type.id, "offer-1")
        self._make_tnved_dictionary(
            product_type, ["3307900008", "6402990000"],
        )

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

    def test_code_absent_from_fresh_dictionary_is_rejected_and_named(self):
        """Critical (ревью Task 7): опечатка в коде раньше молча сохранялась
        как «успех» — ``resolve_tnved`` потом навсегда возвращал ``None`` для
        несуществующего кода, и карточки типа оставались заблокированы без
        видимой причины. Теперь код обязан пройти по свежему словарю ДО
        записи, а ошибка называет введённый код.
        """
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            save_decision,
        )

        product_type = self._make_product_type("1609")
        self._make_draft(product_type.id, "offer-1")
        self._make_tnved_dictionary(product_type, ["3307900008"])

        with self.assertRaises(OzonComplianceAdminError) as ctx:
            save_decision(
                product_type_id=product_type.id,
                tnved_code="9999999999",
                rationale="опечатка",
                user_id=self.user_id,
            )
        self.assertIn("9999999999", str(ctx.exception))

        from models import OzonComplianceDefault
        self.assertEqual(
            OzonComplianceDefault.query.filter_by(
                product_type_id=product_type.id,
            ).count(),
            0,
        )

    def test_missing_code_rejection_preserves_existing_active_decision(self):
        from models import OzonComplianceDefault
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            save_decision,
        )

        product_type = self._make_product_type("1612")
        self._make_draft(product_type.id, "offer-4")
        self._make_tnved_dictionary(
            product_type, ["6402990000", "3307900008"],
        )
        old = save_decision(
            product_type_id=product_type.id,
            tnved_code="6402990000",
            rationale="Существующее решение",
            user_id=self.user_id,
        )
        before = self._decision_snapshot(old)

        with self.assertRaises(OzonComplianceAdminError) as ctx:
            save_decision(
                product_type_id=product_type.id,
                tnved_code="9999999999",
                rationale="Код отсутствует",
                user_id=self.user_id,
            )

        self.assertIn("не найден", str(ctx.exception))
        self._assert_existing_active_unchanged(product_type, before)

    def test_stale_dictionary_rejection_preserves_existing_active_decision(self):
        from models import db
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            save_decision,
        )

        product_type = self._make_product_type("1613")
        self._make_draft(product_type.id, "offer-5")
        definition = self._make_tnved_dictionary(
            product_type, ["6402990000", "3307900008"],
        )
        old = save_decision(
            product_type_id=product_type.id,
            tnved_code="6402990000",
            rationale="Существующее решение",
            user_id=self.user_id,
        )
        before = self._decision_snapshot(old)
        definition.values_synced_at = datetime(2000, 1, 1)
        db.session.commit()

        with self.assertRaises(OzonComplianceAdminError) as ctx:
            save_decision(
                product_type_id=product_type.id,
                tnved_code="3307900008",
                rationale="Словарь устарел",
                user_id=self.user_id,
            )

        self.assertIn("синхронизации", str(ctx.exception))
        self._assert_existing_active_unchanged(product_type, before)

    def test_stale_or_missing_dictionary_blocks_save_with_sync_message(self):
        """Critical (ревью Task 7): без свежего словаря (или вовсе без
        определения атрибута ТН ВЭД у типа) решение нельзя ни подтвердить,
        ни отклонить по содержимому — сохранение обязано отказать явным
        сообщением про синхронизацию справочника, а не тихо пройти.
        """
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            save_decision,
        )

        product_type = self._make_product_type("1610")
        self._make_draft(product_type.id, "offer-2")
        # Ни одного _make_tnved_dictionary(...) для этого типа — определения
        # атрибута ТН ВЭД нет вовсе, ``is_type_tnved_code_available`` обязан
        # вернуть ``None``, а не ``False``.

        with self.assertRaises(OzonComplianceAdminError) as ctx:
            save_decision(
                product_type_id=product_type.id,
                tnved_code="3307900008",
                rationale="без словаря",
                user_id=self.user_id,
            )
        message = str(ctx.exception)
        self.assertIn("синхрон", message.lower())

    def test_valid_code_in_fresh_dictionary_is_accepted(self):
        from services.ozon_compliance_defaults import resolve_tnved
        from services.ozon_compliance_admin import save_decision

        product_type = self._make_product_type("1611")
        self._make_draft(product_type.id, "offer-3")
        self._make_tnved_dictionary(
            product_type, ["3307900008", "6402990000"],
        )

        decision = save_decision(
            product_type_id=product_type.id,
            tnved_code="3307900008",
            rationale="код есть в словаре",
            user_id=self.user_id,
        )
        self.assertEqual(decision.tnved_code, "3307900008")
        self.assertEqual(decision.status, "active")
        resolved = resolve_tnved(product_type.id)
        self.assertEqual(resolved["code"], "3307900008")
        self.assertEqual(resolved["external_value_id"], "1001")

    def test_ambiguous_code_is_rejected_before_replacing_active_decision(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            save_decision,
        )

        product_type = self._make_product_type("1614")
        self._make_draft(product_type.id, "offer-6")
        self._make_tnved_dictionary(
            product_type,
            ["6402990000", "3307900008", "3307900008"],
        )
        old = save_decision(
            product_type_id=product_type.id,
            tnved_code="6402990000",
            rationale="Существующее решение",
            user_id=self.user_id,
        )
        before = self._decision_snapshot(old)

        with self.assertRaises(OzonComplianceAdminError) as ctx:
            save_decision(
                product_type_id=product_type.id,
                tnved_code="3307900008",
                rationale="Код встречается несколько раз",
                user_id=self.user_id,
            )

        message = str(ctx.exception).lower()
        self.assertIn("неоднозначен", message)
        self.assertIn("несколько", message)
        self.assertIn("карточки", message)
        self._assert_existing_active_unchanged(product_type, before)

    def test_type_restriction_narrowing_duplicate_code_resolves_exact_value(self):
        from services.ozon_compliance_admin import save_decision
        from services.ozon_compliance_defaults import resolve_tnved

        product_type = self._make_product_type("1615")
        self._make_draft(product_type.id, "offer-7")
        self._make_tnved_dictionary(
            product_type,
            ["3307900008", "3307900008"],
            restriction=["1002"],
        )

        decision = save_decision(
            product_type_id=product_type.id,
            tnved_code="3307900008",
            rationale="Точное значение разрешено ограничением типа",
            user_id=self.user_id,
        )
        resolved = resolve_tnved(product_type.id)

        self.assertEqual(decision.tnved_display, "3307900008 - Описание 3307900008")
        self.assertEqual(resolved["external_value_id"], "1002")

    def test_type_restriction_excluding_duplicate_codes_preserves_active_decision(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            save_decision,
        )

        product_type = self._make_product_type("1616")
        self._make_draft(product_type.id, "offer-8")
        self._make_tnved_dictionary(
            product_type,
            ["6402990000", "3307900008", "3307900008"],
            restriction=["1001"],
        )
        old = save_decision(
            product_type_id=product_type.id,
            tnved_code="6402990000",
            rationale="Существующее разрешённое решение",
            user_id=self.user_id,
        )
        before = self._decision_snapshot(old)

        with self.assertRaises(OzonComplianceAdminError) as ctx:
            save_decision(
                product_type_id=product_type.id,
                tnved_code="3307900008",
                rationale="Код исключён restriction",
                user_id=self.user_id,
            )

        self.assertIn("не найден", str(ctx.exception))
        self._assert_existing_active_unchanged(product_type, before)


class ListTypeRowsTestCase(_AdminServiceDbTestCase):
    def _seed_types_with_decisions(self, count, *, offset=0):
        from services.ozon_compliance_admin import save_decision

        for index in range(offset, offset + count):
            product_type = self._make_product_type(f"type-{index}")
            self._make_draft(product_type.id, f"offer-{index}")
            self._make_tnved_dictionary(product_type, [f"640299000{index}"])
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

    def test_dictionary_readiness_and_count_reflect_fresh_type(self):
        """Critical (ревью Task 7): без этих полей на свежем стенде даталист
        типа пуст, а после первых решений показывал бы коды ЧУЖИХ типов —
        экран обязан знать per-row готовность и размер словаря заранее.
        """
        product_type = self._make_product_type("1609")
        self._make_draft(product_type.id, "offer-1")
        self._make_tnved_dictionary(
            product_type, ["3307900008", "6402990000", "3401199000"],
        )

        from services.ozon_compliance_admin import list_type_rows
        rows = list_type_rows()
        row = next(r for r in rows if r["product_type_id"] == product_type.id)
        self.assertTrue(row["tnved_dictionary_ready"])
        self.assertEqual(row["tnved_dictionary_count"], 3)

    def test_dictionary_not_ready_for_type_without_definition(self):
        product_type = self._make_product_type("1609")
        self._make_draft(product_type.id, "offer-1")
        # Ни одного _make_tnved_dictionary(...) — определения нет вовсе.

        from services.ozon_compliance_admin import list_type_rows
        rows = list_type_rows()
        row = next(r for r in rows if r["product_type_id"] == product_type.id)
        self.assertFalse(row["tnved_dictionary_ready"])
        self.assertEqual(row["tnved_dictionary_count"], 0)


class TypeTnvedDictionarySummaryBatchTestCase(_AdminServiceDbTestCase):
    """``type_tnved_dictionary_summary_batch`` — прямые тесты batched-функции,
    отдельно от того, как её использует ``list_type_rows``.
    """

    def test_ready_type_reports_exact_count(self):
        from services.ozon_compliance_defaults import (
            type_tnved_dictionary_summary_batch,
        )

        product_type = self._make_product_type("1609")
        self._make_tnved_dictionary(
            product_type, ["3307900008", "6402990000", "3401199000"],
        )

        summary = type_tnved_dictionary_summary_batch([product_type.id])
        self.assertEqual(
            summary[product_type.id], {"ready": True, "count": 3},
        )

    def test_type_without_definition_reports_not_ready(self):
        from services.ozon_compliance_defaults import (
            type_tnved_dictionary_summary_batch,
        )

        product_type = self._make_product_type("1609")

        summary = type_tnved_dictionary_summary_batch([product_type.id])
        self.assertEqual(
            summary[product_type.id], {"ready": False, "count": 0},
        )

    def test_restriction_narrows_count_to_allowed_subset(self):
        from services.ozon_compliance_defaults import (
            type_tnved_dictionary_summary_batch,
        )

        product_type = self._make_product_type("1609")
        definition = self._make_tnved_dictionary(
            product_type,
            ["3307900008", "6402990000", "3401199000"],
            restriction=["1001", "1002"],
        )
        self.assertIsNotNone(definition)

        summary = type_tnved_dictionary_summary_batch([product_type.id])
        self.assertEqual(
            summary[product_type.id], {"ready": True, "count": 2},
        )

    def test_multiple_types_are_reported_independently_in_one_call(self):
        from services.ozon_compliance_defaults import (
            type_tnved_dictionary_summary_batch,
        )

        ready_type = self._make_product_type("1609")
        self._make_tnved_dictionary(ready_type, ["3307900008", "6402990000"])
        not_ready_type = self._make_product_type("1610")

        summary = type_tnved_dictionary_summary_batch(
            [ready_type.id, not_ready_type.id],
        )
        self.assertEqual(
            summary[ready_type.id], {"ready": True, "count": 2},
        )
        self.assertEqual(
            summary[not_ready_type.id], {"ready": False, "count": 0},
        )

    def test_empty_ids_returns_empty_dict(self):
        from services.ozon_compliance_defaults import (
            type_tnved_dictionary_summary_batch,
        )
        self.assertEqual(type_tnved_dictionary_summary_batch([]), {})


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

    def test_eleven_digit_prefix_is_rejected_with_line_number(self):
        """Important (ревью Task 7): граница длины префикса не была
        покрыта тестом на верхнем конце (только 1-цифровой снизу).
        """
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            create_registry_version,
        )
        with self.assertRaises(OzonComplianceAdminError) as ctx:
            create_registry_version(
                label="v1", is_complete=True,
                rules_text="6402\n12345678901",
                user_id=self.user_id,
            )
        self.assertIn("Строка 2", str(ctx.exception))

    def test_ten_digit_prefix_is_accepted_at_the_boundary(self):
        from services.ozon_compliance_admin import create_registry_version

        version = create_registry_version(
            label="v1", is_complete=True, rules_text="1234567890",
            user_id=self.user_id,
        )
        self.assertEqual(version.rule_count, 1)

    def test_invalid_valid_from_format_is_rejected_with_line_number(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            create_registry_version,
        )
        with self.assertRaises(OzonComplianceAdminError) as ctx:
            create_registry_version(
                label="v1", is_complete=True,
                rules_text="6402;акт;01.07.2020;прим",
                user_id=self.user_id,
            )
        self.assertIn("Строка 1", str(ctx.exception))

    def test_normative_ref_length_is_enforced(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            create_registry_version,
        )
        too_long_ref = "x" * 301
        with self.assertRaises(OzonComplianceAdminError):
            create_registry_version(
                label="v1", is_complete=True,
                rules_text=f"6402;{too_long_ref}",
                user_id=self.user_id,
            )

    def test_note_length_is_enforced(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            create_registry_version,
        )
        too_long_note = "y" * 2001
        with self.assertRaises(OzonComplianceAdminError):
            create_registry_version(
                label="v1", is_complete=True,
                rules_text=f"6402;;;{too_long_note}",
                user_id=self.user_id,
            )

    def test_label_length_is_enforced(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            create_registry_version,
        )
        with self.assertRaises(OzonComplianceAdminError):
            create_registry_version(
                label="z" * 201, is_complete=True, rules_text="6402",
                user_id=self.user_id,
            )

    def test_exactly_5000_rules_succeeds(self):
        """Important (ревью Task 7): было покрыто только «5001 отклоняется»,
        не «5000 успешно проходит» — граница проверялась только с одной
        стороны.
        """
        from services.ozon_compliance_admin import create_registry_version

        rules_text = "\n".join(f"{100000 + i}" for i in range(5000))
        version = create_registry_version(
            label="max", is_complete=True, rules_text=rules_text,
            user_id=self.user_id,
        )
        self.assertEqual(version.rule_count, 5000)


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
        self._make_tnved_dictionary(product_type, ["6402990000"])
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

    def test_non_numeric_version_id_is_rejected_cleanly(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            preview_registry_switch,
        )
        with self.assertRaises(OzonComplianceAdminError):
            preview_registry_switch("abc")


class ApplyToExistingDraftsTestCase(unittest.TestCase):
    """Чистый предикат ``_draft_is_eligible`` — без БД и app context."""

    def test_draft_with_active_operation_is_skipped(self):
        from services.ozon_compliance_admin import _draft_is_eligible

        draft = MagicMock()
        draft.status = 'blocked'
        self.assertFalse(_draft_is_eligible(draft, has_active_operation=True))

    def test_archived_draft_is_skipped(self):
        from services.ozon_compliance_admin import _draft_is_eligible

        draft = MagicMock()
        draft.status = 'archived'
        self.assertFalse(_draft_is_eligible(draft, has_active_operation=False))

    def test_blocked_draft_without_operation_is_eligible(self):
        from services.ozon_compliance_admin import _draft_is_eligible

        draft = MagicMock()
        draft.status = 'blocked'
        self.assertTrue(_draft_is_eligible(draft, has_active_operation=False))


class BoundedLimitTestCase(unittest.TestCase):
    """``_bounded_limit`` — чистая функция, без БД и app context.

    (ревью Task 8, Minor): дешёвый тест верхней границы ``limit`` без
    необходимости создавать 200+ строк в тестовой БД.
    """

    def test_limit_above_200_is_clamped_to_200(self):
        from services.ozon_compliance_admin import _bounded_limit
        self.assertEqual(_bounded_limit(500), 200)

    def test_limit_below_1_is_clamped_to_1(self):
        from services.ozon_compliance_admin import _bounded_limit
        self.assertEqual(_bounded_limit(0), 1)
        self.assertEqual(_bounded_limit(-5), 1)

    def test_limit_within_range_is_unchanged(self):
        from services.ozon_compliance_admin import _bounded_limit
        self.assertEqual(_bounded_limit(50), 50)


class RefreshExistingDraftsTestCase(unittest.TestCase):
    """``compliance_value_is_ours`` — чистая функция, без БД и app context.

    (Task 8b) Единственное допустимое доказательство «это наше значение» —
    побайтовое совпадение с тем, что зафиксировано в провенансе в момент
    записи. Ничего, кроме этого совпадения (включая уверенность/heuristics),
    не должно давать ``True``.
    """

    def test_only_value_we_wrote_is_refreshed(self):
        from services.ozon_compliance_admin import compliance_value_is_ours

        provenance = {'compliance.22232': {'external_value_id': '971397758'}}
        stored = {'attribute_id': '22232',
                  'values': [{'dictionary_value_id': '971397758',
                              'value': '3307900008 - X'}]}
        self.assertTrue(compliance_value_is_ours('22232', stored, provenance))

    def test_seller_edited_value_is_not_ours(self):
        from services.ozon_compliance_admin import compliance_value_is_ours

        provenance = {'compliance.22232': {'external_value_id': '971397758'}}
        stored = {'attribute_id': '22232',
                  'values': [{'dictionary_value_id': '999999999',
                              'value': 'Другой код'}]}
        self.assertFalse(compliance_value_is_ours('22232', stored, provenance))

    def test_value_without_provenance_is_not_ours(self):
        from services.ozon_compliance_admin import compliance_value_is_ours

        stored = {'attribute_id': '22232',
                  'values': [{'dictionary_value_id': '971397758'}]}
        self.assertFalse(compliance_value_is_ours('22232', stored, {}))

    def test_marking_compares_the_literal_value_field(self):
        from services.ozon_compliance_admin import compliance_value_is_ours

        provenance = {'compliance.23536': {'value': 'false'}}
        stored = {'attribute_id': '23536', 'values': [{'value': 'false'}]}
        self.assertTrue(compliance_value_is_ours('23536', stored, provenance))

        stored_edited = {'attribute_id': '23536', 'values': [{'value': 'true'}]}
        self.assertFalse(
            compliance_value_is_ours('23536', stored_edited, provenance)
        )

    def test_multi_value_or_malformed_entry_is_never_ours(self):
        """Значение из этого слоя всегда ровно одна запись в ``values``; всё
        остальное (несколько значений, отсутствующий/не-list/не-dict values)
        не может быть побайтово сравнено и fail-closed не считается нашим.
        """
        from services.ozon_compliance_admin import compliance_value_is_ours

        provenance = {'compliance.22232': {'external_value_id': '971397758'}}
        multi = {'attribute_id': '22232', 'values': [
            {'dictionary_value_id': '971397758', 'value': 'X'},
            {'dictionary_value_id': '971397758', 'value': 'X'},
        ]}
        self.assertFalse(compliance_value_is_ours('22232', multi, provenance))

        empty = {'attribute_id': '22232', 'values': []}
        self.assertFalse(compliance_value_is_ours('22232', empty, provenance))

        missing = {'attribute_id': '22232'}
        self.assertFalse(compliance_value_is_ours('22232', missing, provenance))


class ApplyToExistingDraftsIntegrationTestCase(_AdminServiceDbTestCase):
    """``apply_to_existing_drafts`` на реальной БД — полный прогон."""

    def _setup_resolved_type(self, external_type_id="1609", *, marking=True):
        """Тип с активным решением по ТН ВЭД и, опционально, активной версией
        перечня маркировки (``marking=True``) либо вовсе без неё
        (``marking=False`` -> версия не создаётся, признак маркировки
        остаётся неразрешённым для проверки частичного резолва).
        """
        from services.ozon_compliance_admin import save_decision

        product_type = self._make_product_type(external_type_id)
        self._make_tnved_dictionary(product_type, ["3307900008"])
        if marking:
            self._make_registry_version(
                "actual", is_complete=True, prefixes=["3307"], status="active",
            )
        save_decision(
            product_type_id=product_type.id,
            tnved_code="3307900008",
            rationale="test",
            user_id=self.user_id,
        )
        return product_type

    def test_updates_empty_attribute_and_marks_validation_stale(self):
        import json as json_module

        from models import MarketplaceProductDraft
        from services.ozon_compliance_admin import apply_to_existing_drafts
        from services.ozon_compliance_defaults import (
            MARKING_ATTRIBUTE_ID, TNVED_ATTRIBUTE_ID,
        )

        product_type = self._setup_resolved_type()
        draft = self._make_draft(product_type.id, "offer-1")
        original_status = draft.status

        counters = apply_to_existing_drafts(product_type_id=product_type.id)

        self.assertEqual(counters["updated"], 1)
        self.assertEqual(counters["failed"], 0)
        self.assertEqual(counters["unresolved"], [])

        refreshed = MarketplaceProductDraft.query.get(draft.id)
        attributes = json_module.loads(refreshed.attributes_json)
        by_id = {item["attribute_id"]: item for item in attributes}
        self.assertIn(TNVED_ATTRIBUTE_ID, by_id)
        self.assertIn(MARKING_ATTRIBUTE_ID, by_id)
        self.assertEqual(refreshed.validation_status, "stale")
        # `status` — не трогаем: CHECK-констрейнт не допускает
        # `ready_to_retry`, а этот прогон вообще не write-ит status.
        self.assertEqual(refreshed.status, original_status)

    def test_active_operation_blocks_update(self):
        from models import MarketplaceProductDraft
        from services.ozon_compliance_admin import apply_to_existing_drafts

        product_type = self._setup_resolved_type()
        draft = self._make_draft(product_type.id, "offer-1")
        self._make_operation(draft.id, "polling")

        counters = apply_to_existing_drafts(product_type_id=product_type.id)

        self.assertEqual(counters["updated"], 0)
        self.assertEqual(counters["skipped_active_operation"], 1)
        refreshed = MarketplaceProductDraft.query.get(draft.id)
        self.assertEqual(refreshed.attributes_json, "[]")
        self.assertEqual(refreshed.validation_status, "never_validated")

    def test_terminal_operation_does_not_block_update(self):
        """Терминальные статусы (``succeeded``/``failed``/``partial``/
        ``cancelled``) не входят в ``ACTIVE_OPERATION_STATUSES`` — черновик с
        только терминальной операцией остаётся приемлемым.
        """
        from services.ozon_compliance_admin import apply_to_existing_drafts

        product_type = self._setup_resolved_type()
        draft = self._make_draft(product_type.id, "offer-1")
        self._make_operation(draft.id, "succeeded")

        counters = apply_to_existing_drafts(product_type_id=product_type.id)

        self.assertEqual(counters["updated"], 1)
        self.assertEqual(counters["skipped_active_operation"], 0)

    def test_archived_draft_is_skipped_in_full_run(self):
        from models import db, MarketplaceProductDraft
        from services.ozon_compliance_admin import apply_to_existing_drafts

        product_type = self._setup_resolved_type()
        draft = self._make_draft(product_type.id, "offer-1")
        draft.status = "archived"
        db.session.commit()

        counters = apply_to_existing_drafts(product_type_id=product_type.id)

        self.assertEqual(counters["updated"], 0)
        self.assertEqual(counters["skipped_archived"], 1)
        refreshed = MarketplaceProductDraft.query.get(draft.id)
        self.assertEqual(refreshed.attributes_json, "[]")

    def test_already_filled_attribute_is_not_touched(self):
        import json as json_module

        from models import db, MarketplaceProductDraft
        from services.ozon_compliance_admin import apply_to_existing_drafts
        from services.ozon_compliance_defaults import (
            MARKING_ATTRIBUTE_ID, TNVED_ATTRIBUTE_ID,
        )

        product_type = self._setup_resolved_type()
        draft = self._make_draft(product_type.id, "offer-1")
        seller_value = [
            {
                "attribute_id": TNVED_ATTRIBUTE_ID,
                "complex_id": "0",
                "values": [{"dictionary_value_id": "999", "value": "seller value"}],
            },
            {
                "attribute_id": MARKING_ATTRIBUTE_ID,
                "complex_id": "0",
                "values": [{"value": "true"}],
            },
        ]
        draft.attributes_json = json_module.dumps(seller_value)
        db.session.commit()

        counters = apply_to_existing_drafts(product_type_id=product_type.id)

        self.assertEqual(counters["updated"], 0)
        self.assertEqual(counters["skipped_already_filled"], 1)
        refreshed = MarketplaceProductDraft.query.get(draft.id)
        self.assertEqual(
            json_module.loads(refreshed.attributes_json), seller_value,
        )
        self.assertEqual(refreshed.validation_status, "never_validated")

    def test_unresolved_type_short_circuits_without_touching_any_draft(self):
        from models import MarketplaceProductDraft
        from services.ozon_compliance_admin import apply_to_existing_drafts
        from services.ozon_compliance_defaults import (
            MARKING_ATTRIBUTE_ID, TNVED_ATTRIBUTE_ID,
        )

        # Тип задействован (нужен драфт), но ни одного save_decision для
        # него нет — резолв ТН ВЭД возвращает None, а значит и маркировка.
        product_type = self._make_product_type("1610")
        self._make_tnved_dictionary(product_type, ["3307900008"])
        draft = self._make_draft(product_type.id, "offer-1")

        counters = apply_to_existing_drafts(product_type_id=product_type.id)

        self.assertEqual(counters["updated"], 0)
        self.assertEqual(counters["skipped_active_operation"], 0)
        self.assertEqual(counters["skipped_archived"], 0)
        self.assertEqual(counters["skipped_already_filled"], 0)
        self.assertEqual(counters["failed"], 0)
        self.assertEqual(
            set(counters["unresolved"]),
            {TNVED_ATTRIBUTE_ID, MARKING_ATTRIBUTE_ID},
        )
        refreshed = MarketplaceProductDraft.query.get(draft.id)
        self.assertEqual(refreshed.attributes_json, "[]")
        self.assertEqual(refreshed.validation_status, "never_validated")

    def test_partial_resolution_still_updates_resolved_attribute(self):
        """ТН ВЭД резолвится, маркировка — нет (нет активной версии перечня):
        черновик всё равно получает ТН ВЭД, а маркировка остаётся честно
        ``unresolved`` вместо того, чтобы блокировать всё дозаполнение целиком.
        """
        import json as json_module

        from models import MarketplaceProductDraft
        from services.ozon_compliance_admin import apply_to_existing_drafts
        from services.ozon_compliance_defaults import (
            MARKING_ATTRIBUTE_ID, TNVED_ATTRIBUTE_ID,
        )

        product_type = self._setup_resolved_type(marking=False)
        draft = self._make_draft(product_type.id, "offer-1")

        counters = apply_to_existing_drafts(product_type_id=product_type.id)

        self.assertEqual(counters["updated"], 1)
        self.assertEqual(counters["unresolved"], [MARKING_ATTRIBUTE_ID])
        refreshed = MarketplaceProductDraft.query.get(draft.id)
        attributes = json_module.loads(refreshed.attributes_json)
        by_id = {item["attribute_id"]: item for item in attributes}
        self.assertIn(TNVED_ATTRIBUTE_ID, by_id)
        self.assertNotIn(MARKING_ATTRIBUTE_ID, by_id)

    def test_error_in_one_draft_does_not_prevent_other_updates_from_persisting(self):
        """Повреждённый ``attributes_json`` одного черновика считается
        ``failed`` и не мешает уже применённому изменению более раннего (по
        id) черновика того же вызова быть закоммиченным — здесь и проверяется
        отсутствие полного ``db.session.rollback()`` в per-row except.
        """
        import json as json_module

        from models import db, MarketplaceProductDraft
        from services.ozon_compliance_admin import apply_to_existing_drafts
        from services.ozon_compliance_defaults import TNVED_ATTRIBUTE_ID

        product_type = self._setup_resolved_type(marking=False)
        draft_ok = self._make_draft(product_type.id, "offer-ok")
        draft_broken = self._make_draft(product_type.id, "offer-broken")
        self.assertLess(draft_ok.id, draft_broken.id)

        draft_broken.attributes_json = "not-json"
        db.session.commit()

        counters = apply_to_existing_drafts(product_type_id=product_type.id)

        self.assertEqual(counters["updated"], 1)
        self.assertEqual(counters["failed"], 1)

        refreshed_ok = MarketplaceProductDraft.query.get(draft_ok.id)
        attributes = json_module.loads(refreshed_ok.attributes_json)
        by_id = {item["attribute_id"]: item for item in attributes}
        self.assertIn(TNVED_ATTRIBUTE_ID, by_id)
        self.assertEqual(refreshed_ok.validation_status, "stale")

        refreshed_broken = MarketplaceProductDraft.query.get(draft_broken.id)
        self.assertEqual(refreshed_broken.attributes_json, "not-json")

    def test_limit_bounds_number_of_processed_drafts(self):
        from models import MarketplaceProductDraft
        from services.ozon_compliance_admin import apply_to_existing_drafts

        product_type = self._setup_resolved_type()
        draft_first = self._make_draft(product_type.id, "offer-first")
        draft_second = self._make_draft(product_type.id, "offer-second")
        self.assertLess(draft_first.id, draft_second.id)

        counters = apply_to_existing_drafts(
            product_type_id=product_type.id, limit=1,
        )

        self.assertEqual(counters["updated"], 1)
        refreshed_first = MarketplaceProductDraft.query.get(draft_first.id)
        refreshed_second = MarketplaceProductDraft.query.get(draft_second.id)
        self.assertEqual(refreshed_first.validation_status, "stale")
        self.assertEqual(refreshed_second.validation_status, "never_validated")

    def test_unknown_product_type_id_is_rejected_cleanly(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            apply_to_existing_drafts,
        )
        with self.assertRaises(OzonComplianceAdminError):
            apply_to_existing_drafts(product_type_id="abc")

    def test_active_operation_check_does_not_grow_per_draft(self):
        """Important (ревью Task 8): раньше принадлежность черновика активной
        операции проверялась отдельным SELECT на КАЖДЫЙ черновик — до 200
        лишних round-trip'ов вместо одного batched запроса на весь набор.
        Маргинальная стоимость ОДНОГО дополнительного черновика теперь —
        только SAVEPOINT+UPDATE+RELEASE самой записи (3 запроса); если
        N+1 вернётся, здесь появится ещё один SELECT сверху (4).
        """
        from models import db
        from services.ozon_compliance_admin import apply_to_existing_drafts

        def _count_queries(fn):
            counter = {"n": 0}

            def _before_cursor_execute(*args, **kwargs):
                counter["n"] += 1

            event.listen(db.engine, "before_cursor_execute", _before_cursor_execute)
            try:
                fn()
            finally:
                event.remove(
                    db.engine, "before_cursor_execute", _before_cursor_execute,
                )
            return counter["n"]

        small_type = self._setup_resolved_type("1609", marking=False)
        for i in range(3):
            self._make_draft(small_type.id, f"small-{i}")
        small_count = _count_queries(
            lambda: apply_to_existing_drafts(product_type_id=small_type.id)
        )

        big_type = self._setup_resolved_type("1611", marking=False)
        for i in range(15):
            self._make_draft(big_type.id, f"big-{i}")
        big_count = _count_queries(
            lambda: apply_to_existing_drafts(product_type_id=big_type.id)
        )

        marginal_per_draft = (big_count - small_count) / (15 - 3)
        self.assertLessEqual(marginal_per_draft, 3)


class RefreshExistingDraftsIntegrationTestCase(_AdminServiceDbTestCase):
    """``apply_to_existing_drafts(refresh=True)`` на реальной БД (Task 8b).

    Marking намеренно оставлен нерезолвленным (без активной версии перечня)
    во всех сценариях этого класса: иначе он резолвился бы в одно и то же
    ``True`` в обоих прогонах и добавлял бы собственный
    ``already_current``/``refreshed`` инкремент поверх сценария теста,
    смешивая два независимых атрибута в одном счётчике.
    """

    def _resolved_type_with_decision(self, external_type_id, tnved_codes, active_code):
        from services.ozon_compliance_admin import save_decision

        product_type = self._make_product_type(external_type_id)
        self._make_tnved_dictionary(product_type, tnved_codes)
        save_decision(
            product_type_id=product_type.id,
            tnved_code=active_code,
            rationale="test",
            user_id=self.user_id,
        )
        return product_type

    def test_refresh_replaces_stale_value_we_wrote_and_updates_provenance(self):
        import json as json_module

        from models import MarketplaceProductDraft
        from services.ozon_compliance_admin import (
            apply_to_existing_drafts, save_decision,
        )
        from services.ozon_compliance_defaults import TNVED_ATTRIBUTE_ID

        product_type = self._resolved_type_with_decision(
            "1609", ["3307900008", "3307900009"], "3307900008",
        )
        draft = self._make_draft(product_type.id, "offer-1")

        first = apply_to_existing_drafts(product_type_id=product_type.id)
        self.assertEqual(first["updated"], 1)

        save_decision(
            product_type_id=product_type.id,
            tnved_code="3307900009",
            rationale="исправление",
            user_id=self.user_id,
        )

        second = apply_to_existing_drafts(
            product_type_id=product_type.id, refresh=True,
        )
        self.assertEqual(second["refreshed"], 1)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["skipped_seller_owned"], 0)

        refreshed = MarketplaceProductDraft.query.get(draft.id)
        refreshed_attrs = json_module.loads(refreshed.attributes_json)
        refreshed_tnved = next(
            item for item in refreshed_attrs
            if item["attribute_id"] == TNVED_ATTRIBUTE_ID
        )
        self.assertEqual(
            refreshed_tnved["values"][0]["value"].split(" ")[0],
            "3307900009",
        )
        provenance = json_module.loads(refreshed.provenance_json)
        self.assertEqual(
            provenance[f"compliance.{TNVED_ATTRIBUTE_ID}"]["code"],
            "3307900009",
        )

    def test_refresh_leaves_seller_edited_value_untouched(self):
        import json as json_module

        from models import db, MarketplaceProductDraft
        from services.ozon_compliance_admin import (
            apply_to_existing_drafts, save_decision,
        )
        from services.ozon_compliance_defaults import TNVED_ATTRIBUTE_ID

        product_type = self._resolved_type_with_decision(
            "1610", ["3307900008", "3307900009"], "3307900008",
        )
        draft = self._make_draft(product_type.id, "offer-2")
        apply_to_existing_drafts(product_type_id=product_type.id)

        # Продавец (или экран массовой починки) вручную переписал значение,
        # не трогая провенанс — ровно сценарий, который обязан заблокировать
        # автоматическое обновление.
        stored = MarketplaceProductDraft.query.get(draft.id)
        attrs = json_module.loads(stored.attributes_json)
        for item in attrs:
            if item["attribute_id"] == TNVED_ATTRIBUTE_ID:
                item["values"] = [{
                    "dictionary_value_id": "manual-value-id",
                    "value": "Ручной код продавца",
                }]
        stored.attributes_json = json_module.dumps(attrs, ensure_ascii=False)
        db.session.commit()

        save_decision(
            product_type_id=product_type.id,
            tnved_code="3307900009",
            rationale="исправление",
            user_id=self.user_id,
        )
        counters = apply_to_existing_drafts(
            product_type_id=product_type.id, refresh=True,
        )
        self.assertEqual(counters["refreshed"], 0)
        self.assertEqual(counters["skipped_seller_owned"], 1)
        self.assertEqual(counters["skipped_already_filled"], 1)

        final = MarketplaceProductDraft.query.get(draft.id)
        final_attrs = json_module.loads(final.attributes_json)
        final_tnved = next(
            item for item in final_attrs
            if item["attribute_id"] == TNVED_ATTRIBUTE_ID
        )
        self.assertEqual(
            final_tnved["values"][0]["dictionary_value_id"],
            "manual-value-id",
        )

    def test_refresh_is_a_noop_when_value_already_matches_current_default(self):
        from services.ozon_compliance_admin import apply_to_existing_drafts

        product_type = self._resolved_type_with_decision(
            "1611", ["3307900008"], "3307900008",
        )
        self._make_draft(product_type.id, "offer-3")
        apply_to_existing_drafts(product_type_id=product_type.id)

        counters = apply_to_existing_drafts(
            product_type_id=product_type.id, refresh=True,
        )
        self.assertEqual(counters["refreshed"], 0)
        self.assertEqual(counters["already_current"], 1)
        self.assertEqual(counters["skipped_seller_owned"], 0)
        self.assertEqual(counters["skipped_already_filled"], 1)

    def test_without_refresh_flag_stale_value_is_left_alone(self):
        """Regression guard: дефолтный ``refresh=False`` обязан сохранять
        старое поведение бит-в-бит — уже заполненный атрибут не трогается,
        даже если админское решение с тех пор изменилось.
        """
        from services.ozon_compliance_admin import (
            apply_to_existing_drafts, save_decision,
        )

        product_type = self._resolved_type_with_decision(
            "1612", ["3307900008", "3307900009"], "3307900008",
        )
        self._make_draft(product_type.id, "offer-4")
        apply_to_existing_drafts(product_type_id=product_type.id)

        save_decision(
            product_type_id=product_type.id,
            tnved_code="3307900009",
            rationale="исправление",
            user_id=self.user_id,
        )
        counters = apply_to_existing_drafts(product_type_id=product_type.id)
        self.assertEqual(counters["updated"], 0)
        self.assertEqual(counters["skipped_already_filled"], 1)

    def test_refresh_mode_does_not_add_queries_per_draft(self):
        """(Ревью Task 8b, Minor) Симметрично уже существующему
        ``test_active_operation_check_does_not_grow_per_draft``, но для
        ``refresh=True``: сравнение хранимого значения с провенансом — это
        чистая работа с уже загруженным JSON, без дополнительных SELECT.
        Drafts прогреваются обычным (``refresh=False``) вызовом заранее,
        чтобы измеряемый вызов реально прогонял полную ветку сравнения
        (``compliance_value_is_ours`` + ``already_current``) на каждой
        строке, а не короткое замыкание "атрибут ещё пуст".
        """
        from models import db
        from services.ozon_compliance_admin import apply_to_existing_drafts

        def _count_queries(fn):
            counter = {"n": 0}

            def _before_cursor_execute(*args, **kwargs):
                counter["n"] += 1

            event.listen(db.engine, "before_cursor_execute", _before_cursor_execute)
            try:
                fn()
            finally:
                event.remove(
                    db.engine, "before_cursor_execute", _before_cursor_execute,
                )
            return counter["n"]

        small_type = self._resolved_type_with_decision(
            "1613", ["3307900008"], "3307900008",
        )
        for i in range(3):
            self._make_draft(small_type.id, f"refresh-small-{i}")
        apply_to_existing_drafts(product_type_id=small_type.id)
        small_count = _count_queries(
            lambda: apply_to_existing_drafts(
                product_type_id=small_type.id, refresh=True,
            )
        )

        big_type = self._resolved_type_with_decision(
            "1614", ["3307900008"], "3307900008",
        )
        for i in range(15):
            self._make_draft(big_type.id, f"refresh-big-{i}")
        apply_to_existing_drafts(product_type_id=big_type.id)
        big_count = _count_queries(
            lambda: apply_to_existing_drafts(
                product_type_id=big_type.id, refresh=True,
            )
        )

        marginal_per_draft = (big_count - small_count) / (15 - 3)
        self.assertLessEqual(marginal_per_draft, 3)


if __name__ == '__main__':
    unittest.main()
