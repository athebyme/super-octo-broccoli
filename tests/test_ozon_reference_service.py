# -*- coding: utf-8 -*-
"""Ozon reference truth is complete, pair-scoped and fail closed."""

from datetime import datetime, timedelta
from unittest.mock import patch
import unittest

from flask import Flask

from models import (
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    db,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.ozon_reference_service import (
    OzonReferenceService,
    OzonReferenceValidationError,
)


SYNTHETIC_CREDENTIALS = MarketplaceCredentials(
    external_account_id="synthetic-client",
    api_key="synthetic-key",
)


def _category(category_id, name, children, *, disabled=False):
    return {
        "description_category_id": category_id,
        "category_name": name,
        "disabled": disabled,
        "children": children,
    }


def _product_type(type_id, name, *, disabled=False):
    return {
        "type_id": type_id,
        "type_name": name,
        "disabled": disabled,
        "children": [],
    }


def _attribute(
    attribute_id,
    name,
    *,
    required=False,
    dictionary_id=0,
    max_value_count=0,
):
    return {
        "id": attribute_id,
        "attribute_complex_id": 0,
        "name": name,
        "description": f"Описание: {name}",
        "type": "String",
        "is_collection": False,
        "is_required": required,
        "max_value_count": max_value_count,
        "group_name": "Общие",
        "group_id": 1,
        "dictionary_id": dictionary_id,
        "category_dependent": bool(dictionary_id),
    }


class SyntheticOzonAdapter:
    def __init__(self, *, tree=None, attributes=None, value_pages=None):
        self.tree = tree
        self.attributes = attributes
        self.value_pages = list(value_pages or [])
        self.tree_payloads = []
        self.attribute_payloads = []
        self.value_payloads = []

    def fetch_category_tree(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.tree_payloads.append(payload)
        return self.tree

    def fetch_attribute_schema(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.attribute_payloads.append(payload)
        return self.attributes

    def fetch_attribute_values(self, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.value_payloads.append(payload)
        if not self.value_pages:
            raise AssertionError("Unexpected values page request")
        return self.value_pages.pop(0)


class OzonReferenceServiceTest(unittest.TestCase):
    def setUp(self):
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
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add(marketplace)
        db.session.commit()
        self.marketplace_id = marketplace.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    @staticmethod
    def _tree_response(*, first_name="Категория A", include_second=True):
        roots = [
            _category(
                10,
                first_name,
                [
                    _product_type(777, "Тип A"),
                    _product_type(778, "Недоступный тип", disabled=True),
                ],
            )
        ]
        if include_second:
            # The same type_id is valid under a different category. Ozon's
            # category/type pair, not type_id by itself, is the identity.
            roots.append(_category(11, "Категория B", [_product_type(777, "Тип B")]))
        return {"result": roots}

    def _sync_tree(self, response, *, now=None):
        adapter = SyntheticOzonAdapter(tree=response)
        result = OzonReferenceService.sync_tree(
            self.marketplace_id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=now,
        )
        return adapter, result

    def _create_type_with_schema(self):
        _, result = self._sync_tree(self._tree_response())
        self.assertTrue(result["success"])
        category = MarketplaceTaxonomyCategory.query.filter_by(
            marketplace_id=self.marketplace_id,
            external_category_id="10",
        ).one()
        return MarketplaceProductType.query.filter_by(
            category_id=category.id,
            external_type_id="777",
        ).one()

    def test_tree_normalizer_is_strict_and_hash_is_order_independent(self):
        response = self._tree_response()
        normalized = OzonReferenceService.normalize_tree(response)
        reversed_normalized = OzonReferenceService.normalize_tree({
            "result": list(reversed(response["result"])),
        })
        self.assertEqual(len(normalized["categories"]), 2)
        self.assertEqual(len(normalized["product_types"]), 3)
        self.assertEqual(
            normalized["snapshot_hash"],
            reversed_normalized["snapshot_hash"],
        )

        malformed = self._tree_response()
        malformed["result"][0]["disabled"] = 0
        with self.assertRaises(OzonReferenceValidationError):
            OzonReferenceService.normalize_tree(malformed)

        ambiguous = self._tree_response()
        ambiguous["result"][0]["type_id"] = 1
        with self.assertRaises(OzonReferenceValidationError):
            OzonReferenceService.normalize_tree(ambiguous)

        disabled_ancestor = OzonReferenceService.normalize_tree({
            "result": [
                _category(
                    100,
                    "Закрытая ветка",
                    [_category(101, "Дочерняя", [_product_type(999, "Тип")])],
                    disabled=True,
                )
            ]
        })
        self.assertFalse(disabled_ancestor["categories"][1]["available"])
        self.assertFalse(disabled_ancestor["product_types"][0]["available"])

    def test_complete_tree_sync_is_pair_scoped_idempotent_and_marks_missing(self):
        first_at = datetime(2026, 7, 15, 10, 0, 0)
        adapter, first = self._sync_tree(self._tree_response(), now=first_at)
        self.assertTrue(first["success"])
        self.assertEqual(adapter.tree_payloads, [{"language": "DEFAULT"}])
        self.assertEqual(MarketplaceTaxonomyCategory.query.count(), 2)
        self.assertEqual(MarketplaceProductType.query.count(), 3)
        self.assertEqual(
            MarketplaceProductType.query.filter_by(external_type_id="777").count(),
            2,
        )
        marketplace = db.session.get(Marketplace, self.marketplace_id)
        self.assertEqual(marketplace.total_categories, 2)
        self.assertEqual(marketplace.total_product_types, 2)
        self.assertEqual(marketplace.categories_version, 1)

        _, identical = self._sync_tree(
            self._tree_response(),
            now=first_at + timedelta(hours=1),
        )
        self.assertTrue(identical["success"])
        self.assertEqual(identical["categories_added"], 0)
        self.assertEqual(identical["types_added"], 0)
        self.assertEqual(identical["version"], 1)

        _, changed = self._sync_tree(
            self._tree_response(first_name="Категория A новая", include_second=False),
            now=first_at + timedelta(hours=2),
        )
        self.assertTrue(changed["success"])
        missing_category = MarketplaceTaxonomyCategory.query.filter_by(
            marketplace_id=self.marketplace_id,
            external_category_id="11",
        ).one()
        missing_type = MarketplaceProductType.query.filter_by(
            category_id=missing_category.id,
            external_type_id="777",
        ).one()
        self.assertFalse(missing_category.is_available)
        self.assertFalse(missing_type.is_available)
        self.assertEqual(changed["version"], 2)

    def test_malformed_tree_preserves_last_good_cache(self):
        _, first = self._sync_tree(self._tree_response())
        self.assertTrue(first["success"])
        before = [
            (item.external_category_id, item.name, item.is_available)
            for item in MarketplaceTaxonomyCategory.query.order_by(
                MarketplaceTaxonomyCategory.external_category_id
            )
        ]

        _, failed = self._sync_tree({"result": []})
        self.assertFalse(failed["success"])
        after = [
            (item.external_category_id, item.name, item.is_available)
            for item in MarketplaceTaxonomyCategory.query.order_by(
                MarketplaceTaxonomyCategory.external_category_id
            )
        ]
        marketplace = db.session.get(Marketplace, self.marketplace_id)
        self.assertEqual(after, before)
        self.assertEqual(marketplace.categories_sync_status, "failed")
        self.assertIn("empty", marketplace.categories_sync_error.lower())

    def test_large_snapshot_shrink_is_rejected(self):
        with self.assertRaises(OzonReferenceValidationError):
            OzonReferenceService._guard_shrink(
                "category",
                previous_count=100,
                new_count=20,
                minimum=50,
                ratio=0.60,
            )

    def test_attribute_sync_preserves_custom_instruction_and_required_enablement(self):
        product_type = self._create_type_with_schema()
        adapter = SyntheticOzonAdapter(attributes={
            "result": [
                _attribute(31, "Бренд", required=True, dictionary_id=900),
                _attribute(32, "Материал", max_value_count=3),
            ]
        })
        first = OzonReferenceService.sync_attributes(
            product_type.id,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertTrue(first["success"])
        self.assertEqual(adapter.attribute_payloads, [{
            "description_category_id": 10,
            "type_id": 777,
            "language": "DEFAULT",
        }])
        brand = MarketplaceAttributeDefinition.query.filter_by(
            product_type_id=product_type.id,
            external_attribute_id="31",
        ).one()
        brand.ai_instruction = "Проверенная ручная инструкция"
        brand.ai_instruction_source = "custom"
        brand.is_enabled = False
        db.session.commit()

        second_adapter = SyntheticOzonAdapter(attributes={
            "result": [
                _attribute(31, "Бренд новый", required=True, dictionary_id=900),
                _attribute(32, "Материал", max_value_count=3),
            ]
        })
        second = OzonReferenceService.sync_attributes(
            product_type.id,
            adapter=second_adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertTrue(second["success"])
        db.session.refresh(brand)
        self.assertEqual(brand.ai_instruction, "Проверенная ручная инструкция")
        self.assertEqual(brand.ai_instruction_source, "custom")
        self.assertTrue(brand.is_enabled)
        self.assertEqual(brand.name, "Бренд новый")
        self.assertEqual(second["version"], 2)

    def test_malformed_attribute_response_preserves_last_good_schema(self):
        product_type = self._create_type_with_schema()
        good = SyntheticOzonAdapter(attributes={
            "result": [_attribute(31, "Бренд", required=True)]
        })
        self.assertTrue(OzonReferenceService.sync_attributes(
            product_type.id,
            adapter=good,
            credentials=SYNTHETIC_CREDENTIALS,
        )["success"])
        before = MarketplaceAttributeDefinition.query.filter_by(
            product_type_id=product_type.id,
        ).one()
        before_state = (before.name, before.is_available, before.ai_instruction)

        malformed = SyntheticOzonAdapter(attributes={
            "result": [{**_attribute(31, "Испорчено"), "is_required": "yes"}]
        })
        result = OzonReferenceService.sync_attributes(
            product_type.id,
            adapter=malformed,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertFalse(result["success"])
        db.session.refresh(before)
        db.session.refresh(product_type)
        self.assertEqual(
            (before.name, before.is_available, before.ai_instruction),
            before_state,
        )
        self.assertEqual(product_type.attributes_sync_status, "failed")

    def test_dictionary_sync_is_complete_before_mutating_cache(self):
        product_type = self._create_type_with_schema()
        schema_adapter = SyntheticOzonAdapter(attributes={
            "result": [_attribute(31, "Бренд", required=True, dictionary_id=900)]
        })
        self.assertTrue(OzonReferenceService.sync_attributes(
            product_type.id,
            adapter=schema_adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )["success"])
        attribute = MarketplaceAttributeDefinition.query.filter_by(
            product_type_id=product_type.id,
            external_attribute_id="31",
        ).one()
        values_adapter = SyntheticOzonAdapter(value_pages=[
            {
                "result": [
                    {"id": 1, "value": "  Бренд А  ", "info": "A"},
                    {"id": 2, "value": "БРЕНД Б", "picture": "https://example.test/b.png"},
                ],
                "has_next": True,
            },
            {
                "result": [{"id": 3, "value": "Бренд В"}],
                "has_next": False,
            },
        ])
        with patch.object(OzonReferenceService, "VALUES_PAGE_SIZE", 2):
            result = OzonReferenceService.sync_attribute_values(
                attribute.id,
                adapter=values_adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        self.assertTrue(result["success"])
        self.assertEqual(
            [payload["last_value_id"] for payload in values_adapter.value_payloads],
            [0, 2],
        )
        self.assertTrue(all(
            payload["description_category_id"] == 10
            and payload["type_id"] == 777
            and payload["attribute_id"] == 31
            and payload["limit"] == 2
            for payload in values_adapter.value_payloads
        ))
        values = MarketplaceAttributeValue.query.filter_by(
            attribute_id=attribute.id,
        ).order_by(MarketplaceAttributeValue.external_value_id).all()
        self.assertEqual([value.external_value_id for value in values], ["1", "2", "3"])
        self.assertEqual(values[0].value, "Бренд А")
        self.assertEqual(values[1].value_normalized, "бренд б")

        # A duplicated value across pages means the snapshot is incomplete or
        # corrupt. Existing availability and contents must remain untouched.
        duplicate_adapter = SyntheticOzonAdapter(value_pages=[
            {
                "result": [
                    {"id": 1, "value": "НЕ СОХРАНЯТЬ"},
                    {"id": 2, "value": "БРЕНД Б"},
                ],
                "has_next": True,
            },
            {
                "result": [{"id": 2, "value": "Дубль"}],
                "has_next": False,
            },
        ])
        with patch.object(OzonReferenceService, "VALUES_PAGE_SIZE", 2):
            failed = OzonReferenceService.sync_attribute_values(
                attribute.id,
                adapter=duplicate_adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        self.assertFalse(failed["success"])
        db.session.refresh(values[0])
        db.session.refresh(attribute)
        self.assertEqual(values[0].value, "Бренд А")
        self.assertTrue(values[0].is_available)
        self.assertEqual(attribute.values_sync_status, "failed")
        self.assertIsNotNone(attribute.values_sync_checkpoint)

        partial_adapter = SyntheticOzonAdapter(value_pages=[
            {
                "result": [
                    {"id": 1, "value": "НЕ СОХРАНЯТЬ"},
                    {"id": 2, "value": "БРЕНД Б"},
                ],
                "has_next": True,
            },
            {"result": [], "has_next": True},
        ])
        with patch.object(OzonReferenceService, "VALUES_PAGE_SIZE", 2):
            partial = OzonReferenceService.sync_attribute_values(
                attribute.id,
                adapter=partial_adapter,
                credentials=SYNTHETIC_CREDENTIALS,
            )
        self.assertFalse(partial["success"])
        db.session.refresh(values[0])
        db.session.refresh(attribute)
        self.assertEqual(values[0].value, "Бренд А")
        self.assertIsNotNone(attribute.values_sync_checkpoint)

    def test_attribute_configuration_is_strict_and_restriction_is_official_subset(self):
        product_type = self._create_type_with_schema()
        schema_adapter = SyntheticOzonAdapter(attributes={
            "result": [_attribute(31, "Бренд", required=True, dictionary_id=900)]
        })
        self.assertTrue(OzonReferenceService.sync_attributes(
            product_type.id,
            adapter=schema_adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )["success"])
        attribute = MarketplaceAttributeDefinition.query.filter_by(
            product_type_id=product_type.id,
            external_attribute_id="31",
        ).one()
        values_adapter = SyntheticOzonAdapter(value_pages=[{
            "result": [
                {"id": 1, "value": "Бренд А"},
                {"id": 2, "value": "Бренд Б"},
                {"id": 3, "value": "Бренд В"},
            ],
            "has_next": False,
        }])
        self.assertTrue(OzonReferenceService.sync_attribute_values(
            attribute.id,
            adapter=values_adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )["success"])

        restricted = OzonReferenceService.update_attribute_configuration(
            attribute.id,
            restriction_value_ids=["3", "1"],
        )
        self.assertEqual(restricted["restriction_value_ids"], ["1", "3"])
        with self.assertRaises(OzonReferenceValidationError):
            OzonReferenceService.update_attribute_configuration(
                attribute.id,
                restriction_value_ids=["999"],
            )
        db.session.refresh(attribute)
        self.assertEqual(attribute.restriction_value_ids, ["1", "3"])

        with self.assertRaises(OzonReferenceValidationError):
            OzonReferenceService.update_attribute_configuration(
                attribute.id,
                is_enabled=False,
            )
        custom = OzonReferenceService.update_attribute_configuration(
            attribute.id,
            ai_instruction="  Только подтверждённый бренд  ",
        )
        self.assertEqual(custom["ai_instruction"], "Только подтверждённый бренд")
        reset = OzonReferenceService.update_attribute_configuration(
            attribute.id,
            ai_instruction="",
        )
        self.assertEqual(reset["ai_instruction_source"], "generated")

    def test_enabling_type_requires_fresh_schema(self):
        product_type = self._create_type_with_schema()
        adapter = SyntheticOzonAdapter(attributes={
            "result": [_attribute(31, "Бренд", required=True)]
        })
        enabled = OzonReferenceService.set_product_type_enabled(
            product_type.id,
            True,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertTrue(enabled["success"])
        self.assertTrue(enabled["synced"])
        self.assertTrue(product_type.is_enabled)
        self.assertEqual(product_type.attributes_count, 1)

        product_type.attributes_synced_at = datetime.utcnow() - timedelta(hours=49)
        db.session.commit()
        failed_adapter = SyntheticOzonAdapter(attributes={"result": []})
        failed = OzonReferenceService.set_product_type_enabled(
            product_type.id,
            True,
            adapter=failed_adapter,
            credentials=SYNTHETIC_CREDENTIALS,
        )
        self.assertFalse(failed["success"])

    def test_refresh_ahead_selects_only_enabled_stale_types_and_required_dictionary(self):
        product_type = self._create_type_with_schema()
        product_type.is_enabled = True
        db.session.commit()
        adapter = SyntheticOzonAdapter(
            attributes={
                "result": [
                    _attribute(31, "Бренд", required=True, dictionary_id=900),
                    _attribute(32, "Описание"),
                ]
            },
            value_pages=[{
                "result": [{"id": 1, "value": "Бренд А"}],
                "has_next": False,
            }],
        )
        with patch.object(
            OzonReferenceService,
            "_adapter_credentials",
            return_value=(adapter, SYNTHETIC_CREDENTIALS),
        ):
            result = OzonReferenceService.sync_stale_enabled_types(
                self.marketplace_id,
                limit=10,
                dictionary_limit=5,
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["dictionaries_synced"], 1)
        self.assertEqual(len(adapter.attribute_payloads), 1)
        self.assertEqual(len(adapter.value_payloads), 1)

    def test_reference_freshness_has_hard_48_hour_ttl(self):
        product_type = self._create_type_with_schema()
        now = datetime(2026, 7, 15, 12, 0, 0)
        product_type.marketplace.categories_synced_at = now
        product_type.marketplace.categories_snapshot_hash = "b" * 64
        product_type.attributes_sync_status = "success"
        product_type.attributes_schema_hash = "a" * 64
        product_type.attributes_synced_at = now - timedelta(hours=47)
        self.assertTrue(OzonReferenceService.reference_is_fresh(
            product_type,
            now=now,
        ))
        product_type.attributes_synced_at = now - timedelta(hours=49)
        self.assertFalse(OzonReferenceService.reference_is_fresh(
            product_type,
            now=now,
        ))

        # A failed refresh attempt does not erase a still-fresh last-good
        # snapshot; hashes and successful timestamps define usability.
        product_type.attributes_synced_at = now - timedelta(hours=1)
        product_type.attributes_sync_status = "failed"
        product_type.marketplace.categories_sync_status = "failed"
        self.assertTrue(OzonReferenceService.reference_is_fresh(
            product_type,
            now=now,
        ))


if __name__ == "__main__":
    unittest.main()
