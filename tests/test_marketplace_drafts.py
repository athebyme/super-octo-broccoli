# -*- coding: utf-8 -*-
"""Ozon drafts use observed facts, exact references and optimistic writes."""

from datetime import datetime, timedelta
import json
import unittest

from flask import Flask

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceCategoryMapping,
    MarketplaceListing,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    Product,
    Seller,
    SellerMarketplaceAccount,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from services.marketplace_drafts import (
    MarketplaceDraftConflict,
    MarketplaceDraftNotFound,
    MarketplaceDraftService,
)
from services.marketplace_fact_pack import MarketplaceFactPackBuilder
from services.ozon_reference_service import OzonReferenceService


class MarketplaceDraftServiceTest(unittest.TestCase):
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
        self.now = datetime.utcnow()
        self.seller1_id = self._seller("draft-one", "draft1@test.local")
        self.seller2_id = self._seller("draft-two", "draft2@test.local")
        self.supplier = Supplier(name="Synthetic", code="synthetic")
        self.marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
            categories_synced_at=self.now,
            categories_snapshot_hash="tree-hash",
        )
        db.session.add_all([self.supplier, self.marketplace])
        db.session.flush()
        self.category = MarketplaceTaxonomyCategory(
            marketplace_id=self.marketplace.id,
            external_category_id="10",
            name="Одежда",
            full_path="Одежда",
            depth=0,
            is_available=True,
            last_seen_at=self.now,
        )
        db.session.add(self.category)
        db.session.flush()
        self.product_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.category.id,
            external_type_id="777",
            name="Футболка",
            is_available=True,
            is_enabled=True,
            attributes_synced_at=self.now,
            attributes_sync_status="success",
            attributes_schema_hash="schema-hash",
            attributes_version=3,
            attributes_count=3,
            required_attributes_count=3,
        )
        db.session.add(self.product_type)
        db.session.flush()
        self.brand_attribute = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="31",
            name="Бренд",
            data_type="String",
            is_required=True,
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
        )
        self.country_attribute = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="32",
            name="Страна производства",
            data_type="String",
            is_required=True,
            dictionary_id="700",
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
            values_synced_at=self.now,
            values_sync_status="success",
            values_snapshot_hash="country-hash",
            values_version=1,
            values_count=1,
        )
        self.description_attribute = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="4191",
            name="Аннотация",
            data_type="String",
            is_required=True,
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
        )
        db.session.add_all([
            self.brand_attribute,
            self.country_attribute,
            self.description_attribute,
        ])
        db.session.flush()
        self.russia = MarketplaceAttributeValue(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            attribute_id=self.country_attribute.id,
            external_value_id="9001",
            value="Россия",
            value_normalized=OzonReferenceService.normalize_value("Россия"),
            is_available=True,
            last_seen_at=self.now,
        )
        db.session.add(self.russia)
        db.session.flush()
        self.account1 = self._account(self.seller1_id, "client-one")
        self.account2 = self._account(self.seller2_id, "client-two")
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    @staticmethod
    def _seller(username, email):
        user = User(username=username, email=email, is_active=True)
        user.set_password("synthetic-password")
        seller = Seller(user=user, company_name=username)
        db.session.add(seller)
        db.session.commit()
        return seller.id

    def _account(self, seller_id, external_id):
        account = SellerMarketplaceAccount(
            seller_id=seller_id,
            marketplace_id=self.marketplace.id,
            external_account_id=external_id,
            label=external_id,
            is_active=True,
            connection_status="connected",
            _credentials_encrypted="synthetic-encrypted-credential",
        )
        db.session.add(account)
        db.session.flush()
        return account

    def _product(
        self,
        *,
        seller_id=None,
        external_id="source-1",
        category="Футболки",
        dimensions=True,
        ai_physical=False,
    ):
        original = {
            "external_id": external_id,
            "vendor_code": f"offer-{external_id}",
            "title": "Футболка",
            "description": "Подробное описание товара",
            "brand": "Наблюдаемый бренд",
            "category": category,
            "country": "Россия",
            "characteristics": {
                "Бренд": "Наблюдаемый бренд",
                "Страна производства": "Россия",
            },
            "barcodes": [f"4600000{len(external_id):06d}"],
            "photo_urls": [f"https://img.test/{external_id}.jpg"],
        }
        if dimensions:
            original["dimensions"] = {
                "package_width_cm": 20,
                "package_height_cm": 3,
                "package_length_cm": 30,
                "package_weight_g": 250,
            }
        supplier_product = SupplierProduct(
            supplier_id=self.supplier.id,
            external_id=external_id,
            vendor_code=f"offer-{external_id}",
            title="Футболка",
            description="Подробное описание товара",
            brand="Наблюдаемый бренд",
            category=category,
            original_data_json=json.dumps(original, ensure_ascii=False),
            ai_parsed_data_json=(
                json.dumps({
                    "physical": {
                        "weight_g": 150,
                        "length_cm": 99,
                        "width_cm": 88,
                        "height_cm": 77,
                    },
                    "origin": {"country_of_origin": "Выдуманная страна"},
                }, ensure_ascii=False)
                if ai_physical else None
            ),
        )
        db.session.add(supplier_product)
        db.session.flush()
        product = ImportedProduct(
            seller_id=seller_id or self.seller1_id,
            supplier_id=self.supplier.id,
            supplier_product_id=supplier_product.id,
            source_type="synthetic",
            external_id=external_id,
            external_vendor_code=f"offer-{external_id}",
            title="Футболка",
            description="Подробное описание товара",
            brand="Наблюдаемый бренд",
            category=category,
            original_data=json.dumps(original, ensure_ascii=False),
            photo_urls=json.dumps(original["photo_urls"]),
            barcodes=json.dumps(original["barcodes"]),
            calculated_price=1000,
            calculated_price_before_discount=1200,
        )
        db.session.add(product)
        db.session.commit()
        return product

    def _ready_draft(self, *, external_id="source-1"):
        product = self._product(external_id=external_id)
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
            save_mapping=True,
            corrected_by_user_id=1,
        )
        draft = MarketplaceDraftService.update_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
            patch={"commercial": {
                "price": "1000",
                "old_price": "1200",
                "vat": "0.22",
                "currency_code": "RUB",
            }},
        )
        return product, draft

    def test_fact_pack_never_promotes_legacy_ai_physical_values(self):
        product = self._product(dimensions=False, ai_physical=True)
        original = json.loads(product.original_data)
        original.pop("brand", None)
        original["characteristics"].pop("Бренд", None)
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data
        db.session.commit()
        pack = MarketplaceFactPackBuilder.build(product)
        self.assertNotIn("physical", pack["facts"])
        self.assertNotIn("brand", pack["facts"].get("identity", {}))
        self.assertEqual(pack["unverified_suggestions"]["brand"], product.brand)
        self.assertEqual(
            pack["unverified_suggestions"]["legacy_ai"]["physical"]["weight_g"],
            150,
        )

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        self.assertEqual(draft.to_public_dict(detail=True)["dimensions"], {})
        validated = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        codes = {
            item["code"]
            for item in validated.to_public_dict(detail=True)["validation"]["errors"]
        }
        self.assertIn("physical_fact_required", codes)
        self.assertIn(
            "unverified_ai_suggestions_ignored",
            {
                item["code"]
                for item in validated.to_public_dict(detail=True)["validation"]["warnings"]
            },
        )

    def test_exact_mapping_auto_attributes_and_full_validation(self):
        product, draft = self._ready_draft()
        detail = draft.to_public_dict(detail=True)
        self.assertEqual(detail["dimensions"], {
            "depth": "300",
            "dimension_unit": "MILLIMETERS",
            "height": "30",
            "weight": "250",
            "weight_unit": "GRAMS",
            "width": "200",
        })
        self.assertEqual(
            {item["attribute_id"] for item in detail["attributes"]},
            {"31", "32"},
        )
        country = next(
            item for item in detail["attributes"]
            if item["attribute_id"] == "32"
        )
        self.assertEqual(country["values"], [{
            "dictionary_value_id": "9001",
            "value": "Россия",
        }])

        validated = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        result = validated.to_public_dict(detail=True)["validation"]
        self.assertTrue(result["publishable"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(validated.status, "ready")
        self.assertEqual(validated.validation_status, "valid")
        self.assertEqual(validated.schema_hash, "schema-hash")
        self.assertEqual(validated.schema_version, 3)
        self.assertEqual(
            MarketplaceCategoryMapping.query.filter_by(
                seller_id=self.seller1_id,
                source_category_normalized="футболки",
            ).count(),
            1,
        )

        second = self._product(external_id="source-2")
        mapped = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=second.id,
        )
        self.assertEqual(mapped.product_type_id, self.product_type.id)
        self.assertIsNotNone(mapped.category_mapping_id)

    def test_mapping_readiness_explains_exact_refs_and_stale_dictionary(self):
        _, draft = self._ready_draft(external_id="readiness-source")
        validated = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        readiness = MarketplaceDraftService.mapping_readiness(
            seller_id=self.seller1_id,
            draft_id=validated.id,
        )

        self.assertEqual(readiness["overall"], "ready")
        self.assertEqual(readiness["category"]["status"], "exact_mapping")
        self.assertTrue(readiness["schema"]["fresh"])
        self.assertEqual(readiness["attributes"]["required_total"], 3)
        self.assertEqual(readiness["attributes"]["required_supplied"], 3)
        self.assertEqual(readiness["attributes"]["missing_required"], [])
        self.assertEqual(readiness["dictionaries"]["total"], 1)
        self.assertEqual(readiness["dictionaries"]["fresh"], 1)
        self.assertTrue(readiness["source"]["facts_fresh"])
        self.assertFalse(
            readiness["reverse_mapping"]["automatic_round_trip"]
        )

        self.country_attribute.values_synced_at = self.now - timedelta(hours=49)
        db.session.commit()
        stale = MarketplaceDraftService.mapping_readiness(
            seller_id=self.seller1_id,
            draft_id=validated.id,
        )
        self.assertEqual(stale["overall"], "references_stale")
        self.assertEqual(stale["dictionaries"]["fresh"], 0)
        self.assertEqual(
            stale["dictionaries"]["stale"][0]["attribute_id"],
            "32",
        )

    def test_confirmed_wb_subject_mapping_precedes_supplier_category(self):
        first = self._product(
            external_id="wb-subject-first",
            category="Supplier category A",
        )
        first.wb_subject_id = 123456
        wb_projection = Product(
            seller_id=self.seller1_id,
            nm_id=700001,
            subject_id=123456,
            title="Футболка — версия WB",
            description=first.description,
            brand=first.brand,
        )
        db.session.add(wb_projection)
        db.session.flush()
        first.product_id = wb_projection.id
        first.wb_subject_id = 999999
        first.mapped_wb_category = "Футболки WB"
        db.session.commit()
        confirmed = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=first.id,
            product_type_id=self.product_type.id,
            save_mapping=True,
        )
        mapping = db.session.get(
            MarketplaceCategoryMapping,
            confirmed.category_mapping_id,
        )
        self.assertEqual(mapping.scope_key, "wb_subject")
        self.assertEqual(mapping.source_type, "wb")
        self.assertEqual(
            mapping.source_category_normalized,
            "wb_subject:123456",
        )
        self.assertIsNone(mapping.supplier_id)
        self.assertEqual(
            json.loads(mapping.evidence_json)["wb_subject_id"],
            123456,
        )
        self.assertEqual(
            json.loads(mapping.evidence_json)["wb_subject_source"],
            "product_projection",
        )
        projection_drift = MarketplaceFactPackBuilder.wb_projection_drift(
            first
        )
        self.assertEqual(projection_drift["differing_fields"], ["title"])
        checked = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=confirmed.id,
            expected_version=confirmed.version,
        )
        self.assertIn(
            "wb_projection_differs_from_canonical",
            {
                item["code"]
                for item in checked.to_public_dict(detail=True)
                ["validation"]["warnings"]
            },
        )
        readiness = MarketplaceDraftService.mapping_readiness(
            seller_id=self.seller1_id,
            draft_id=checked.id,
        )
        self.assertEqual(
            readiness["source"]["wb_projection"]["differing_fields"],
            ["title"],
        )

        second = self._product(
            external_id="wb-subject-second",
            category="Completely different supplier category",
        )
        second.wb_subject_id = 123456
        second.wb_nm_id = 700002
        second.import_status = "imported"
        second.mapped_wb_category = "Футболки WB"
        db.session.commit()
        reused = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=second.id,
        )
        self.assertEqual(reused.product_type_id, self.product_type.id)
        self.assertEqual(reused.category_mapping_id, mapping.id)

        unconfirmed = self._product(
            external_id="wb-subject-unconfirmed",
            category="Unmapped supplier category",
        )
        unconfirmed.wb_subject_id = 123456
        db.session.commit()
        not_confirmed = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=unconfirmed.id,
        )
        self.assertIsNone(not_confirmed.product_type_id)

        foreign = self._product(
            seller_id=self.seller2_id,
            external_id="wb-subject-foreign",
            category="Supplier category A",
        )
        foreign.wb_subject_id = 123456
        foreign.wb_nm_id = 700003
        foreign.import_status = "imported"
        db.session.commit()
        not_reused = MarketplaceDraftService.create_draft(
            seller_id=self.seller2_id,
            account_id=self.account2.id,
            imported_product_id=foreign.id,
        )
        self.assertIsNone(not_reused.product_type_id)

    def test_linked_existing_ozon_listing_reuses_fact_pack_as_update_draft(self):
        product = self._product(
            external_id="already-on-ozon",
            ai_physical=True,
        )
        listing = MarketplaceListing(
            seller_id=self.seller1_id,
            marketplace_id=self.marketplace.id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
            offer_id=product.external_vendor_code,
            external_product_id="987654321",
            title="Существующая карточка Ozon",
            normalized_status="active",
            link_status="linked",
            link_source="seller_confirmation",
            sync_fingerprint="c" * 64,
        )
        db.session.add(listing)
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
        )
        detail = draft.to_public_dict(detail=True)
        self.assertEqual(draft.published_listing_id, listing.id)
        self.assertEqual(draft.product_type_id, self.product_type.id)
        self.assertEqual(draft.offer_id, listing.offer_id)
        self.assertEqual(
            json.loads(draft.source_facts_json)["unverified_suggestions"]
            ["legacy_ai"]["physical"]["weight_g"],
            150,
        )
        self.assertEqual(detail["content"]["name"], product.title)

    def test_description_4191_is_built_from_content_and_conflicts_fail_validation(self):
        _, draft = self._ready_draft(external_id="description-source")
        content = draft.to_public_dict(detail=True)["content"]
        content["description"] = "Новое подтверждённое описание"
        changed = MarketplaceDraftService.update_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
            patch={"content": content},
        )
        valid = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=changed.id,
            expected_version=changed.version,
        )
        self.assertTrue(valid.to_public_dict(detail=True)["validation"]["publishable"])

        attributes = valid.to_public_dict(detail=True)["attributes"]
        attributes.append({
            "attribute_id": "4191",
            "complex_id": "0",
            "values": [{"value": "Старое описание"}],
        })
        conflicting = MarketplaceDraftService.update_draft(
            seller_id=self.seller1_id,
            draft_id=valid.id,
            expected_version=valid.version,
            patch={"attributes": attributes},
        )
        invalid = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=conflicting.id,
            expected_version=conflicting.version,
        )
        codes = {
            item["code"]
            for item in invalid.to_public_dict(detail=True)["validation"]["errors"]
        }
        self.assertIn("description_attribute_conflict", codes)

    def test_mapping_is_seller_scoped_and_foreign_draft_is_hidden(self):
        self._ready_draft()
        foreign_product = self._product(
            seller_id=self.seller2_id,
            external_id="foreign-source",
        )
        foreign = MarketplaceDraftService.create_draft(
            seller_id=self.seller2_id,
            account_id=self.account2.id,
            imported_product_id=foreign_product.id,
        )
        self.assertIsNone(foreign.product_type_id)
        self.assertEqual(foreign.status, "needs_category")
        with self.assertRaises(MarketplaceDraftNotFound):
            MarketplaceDraftService.get_draft(
                seller_id=self.seller1_id,
                draft_id=foreign.id,
            )

    def test_stale_schema_and_foreign_dictionary_value_fail_closed(self):
        _, draft = self._ready_draft(external_id="stale-source")
        self.product_type.attributes_synced_at = self.now - timedelta(hours=49)
        db.session.commit()
        stale = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        stale_codes = {
            item["code"]
            for item in stale.to_public_dict(detail=True)["validation"]["errors"]
        }
        self.assertIn("schema_stale", stale_codes)

        self.product_type.attributes_synced_at = self.now
        db.session.commit()
        attributes = stale.to_public_dict(detail=True)["attributes"]
        country = next(item for item in attributes if item["attribute_id"] == "32")
        country["values"] = [{
            "dictionary_value_id": "9999",
            "value": "Россия",
        }]
        changed = MarketplaceDraftService.update_draft(
            seller_id=self.seller1_id,
            draft_id=stale.id,
            expected_version=stale.version,
            patch={"attributes": attributes},
        )
        invalid = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=changed.id,
            expected_version=changed.version,
        )
        codes = {
            item["code"]
            for item in invalid.to_public_dict(detail=True)["validation"]["errors"]
        }
        self.assertIn("dictionary_value_out_of_scope", codes)

    def test_required_complex_attribute_and_optimistic_version(self):
        complex_attribute = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="40",
            name="Состав комплекта",
            data_type="String",
            is_required=True,
            max_value_count=1,
            attribute_complex_id="500",
            complex_is_collection=True,
            is_collection=False,
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
        )
        db.session.add(complex_attribute)
        self.product_type.attributes_count = 4
        self.product_type.required_attributes_count = 4
        self.product_type.attributes_schema_hash = "schema-complex"
        self.product_type.attributes_version = 4
        db.session.commit()

        _, draft = self._ready_draft(external_id="complex-source")
        invalid = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        self.assertIn(
            "required_attribute_missing",
            {
                item["code"]
                for item in invalid.to_public_dict(detail=True)["validation"]["errors"]
            },
        )
        with self.assertRaises(MarketplaceDraftConflict):
            MarketplaceDraftService.update_draft(
                seller_id=self.seller1_id,
                draft_id=invalid.id,
                expected_version=invalid.version - 1,
                patch={"offer_id": "stale-write"},
            )

        changed = MarketplaceDraftService.update_draft(
            seller_id=self.seller1_id,
            draft_id=invalid.id,
            expected_version=invalid.version,
            patch={"complex_attributes": [{
                "attributes": [{
                    "attribute_id": "40",
                    "complex_id": "500",
                    "values": [{"value": "Футболка"}],
                }],
            }]},
        )
        valid = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=changed.id,
            expected_version=changed.version,
        )
        self.assertTrue(valid.to_public_dict(detail=True)["validation"]["publishable"])

    def test_source_drift_requires_explicit_fact_refresh_without_overwrite(self):
        product, draft = self._ready_draft(external_id="drift-source")
        before_content = draft.content_json
        before_hash = draft.source_fact_hash
        product.title = "Новое название источника"
        db.session.commit()
        invalid = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        self.assertIn(
            "source_facts_stale",
            {
                item["code"]
                for item in invalid.to_public_dict(detail=True)["validation"]["errors"]
            },
        )
        refreshed = MarketplaceDraftService.refresh_facts(
            seller_id=self.seller1_id,
            draft_id=invalid.id,
            expected_version=invalid.version,
        )
        self.assertNotEqual(refreshed.source_fact_hash, before_hash)
        self.assertEqual(refreshed.content_json, before_content)


if __name__ == "__main__":
    unittest.main()
