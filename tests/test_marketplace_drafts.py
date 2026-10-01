# -*- coding: utf-8 -*-
"""Ozon drafts use observed facts, exact references and optimistic writes."""

from datetime import datetime, timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from flask import Flask

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceCategoryMapping,
    MarketplaceListing,
    MarketplaceProductDraft,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    OzonComplianceDefault,
    OzonMarkingRegistryVersion,
    OzonMarkingRule,
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
from services.common_product_content import CommonProductContentService
from services.ozon_reference_service import OzonReferenceService


class MarketplaceDraftServiceTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-drafts-test-secret",
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

    def _linked_listing(
        self,
        product,
        *,
        product_type=None,
        link_source="exact_source_identity",
        account=None,
    ):
        listing = MarketplaceListing(
            seller_id=product.seller_id,
            marketplace_id=self.marketplace.id,
            account_id=(
                account.id
                if account is not None
                else (
                    self.account1.id
                    if product.seller_id == self.seller1_id
                    else self.account2.id
                )
            ),
            imported_product_id=product.id,
            product_type_id=(
                product_type.id
                if product_type is not None
                else None
            ),
            offer_id=product.external_vendor_code,
            external_product_id=str(900_000 + product.id),
            title=product.title,
            normalized_status="active",
            link_status="linked",
            link_source=link_source,
            is_available=True,
            is_archived=False,
            sync_fingerprint=f"{product.id:064x}"[-64:],
        )
        db.session.add(listing)
        db.session.commit()
        return listing

    def _dictionary_attribute(
        self,
        external_id,
        name,
        values,
        *,
        is_collection=False,
        max_value_count=1,
        is_required=False,
        product_type=None,
    ):
        product_type = product_type or self.product_type
        attribute = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=product_type.id,
            external_attribute_id=external_id,
            name=name,
            data_type="String",
            is_required=is_required,
            dictionary_id=f"dictionary-{external_id}",
            max_value_count=max_value_count,
            is_collection=is_collection,
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
            values_synced_at=self.now,
            values_sync_status="success",
            values_snapshot_hash=f"values-{external_id}",
            values_version=1,
            values_count=len(values),
        )
        db.session.add(attribute)
        db.session.flush()
        for index, value in enumerate(values, start=1):
            db.session.add(MarketplaceAttributeValue(
                marketplace_id=self.marketplace.id,
                product_type_id=product_type.id,
                attribute_id=attribute.id,
                external_value_id=f"{external_id}-{index}",
                value=value,
                value_normalized=OzonReferenceService.normalize_value(value),
                is_available=True,
                last_seen_at=self.now,
            ))
        return attribute

    def _official_type(self, name, path):
        marker = MarketplaceTaxonomyCategory.query.count() + 1
        category = MarketplaceTaxonomyCategory(
            marketplace_id=self.marketplace.id,
            external_category_id=f"official-category-{marker}",
            name=path.rsplit(" / ", 1)[-1],
            full_path=path,
            depth=1,
            is_available=True,
            last_seen_at=self.now,
        )
        db.session.add(category)
        db.session.flush()
        product_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=category.id,
            external_type_id=f"official-type-{name}-{marker}",
            name=name,
            is_available=True,
            is_seller_selectable=True,
            is_enabled=True,
            attributes_synced_at=self.now,
            attributes_sync_status="success",
            attributes_schema_hash=f"official-schema-{name}-{marker}",
            attributes_version=1,
            attributes_count=0,
            required_attributes_count=0,
        )
        db.session.add(product_type)
        db.session.flush()
        return product_type

    def _erotic_clothing_type(self, name="Эротическое белье"):
        return self._official_type(
            name,
            MarketplaceDraftService.EXPLICIT_EROTIC_CLOTHING_PATH,
        )

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

    def test_fact_pack_v3_keeps_distinct_source_description_observed(self):
        product = self._product(external_id="source-description-v3")
        product.description = None
        original = json.loads(product.original_data)
        original["description"] = "Точное описание из фида поставщика"
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data
        db.session.commit()

        pack = MarketplaceFactPackBuilder.build(product)
        content = MarketplaceDraftService._content_from_facts(pack)

        self.assertEqual(pack["version"], 3)
        self.assertNotIn("description", pack["facts"]["identity"])
        self.assertEqual(
            pack["facts"]["identity"]["source_description"],
            "Точное описание из фида поставщика",
        )
        self.assertEqual(
            pack["provenance"]["identity.source_description"]["trust"],
            "observed",
        )
        self.assertEqual(
            content["description"],
            "Точное описание из фида поставщика",
        )

    def test_unedited_legacy_draft_keeps_its_original_fact_hash_and_readiness(self):
        product, draft = self._ready_draft(external_id="legacy-fact-hash")
        draft = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        before_hash = draft.source_fact_hash
        pack = MarketplaceFactPackBuilder.build(product)
        facts_document, _provenance, current_hash = MarketplaceDraftService._fact_snapshot(product)

        self.assertNotIn("seller_common_content", pack)
        self.assertNotIn("seller_common_content", facts_document)
        self.assertEqual(current_hash, before_hash)
        self.assertEqual(pack["fact_hash"], before_hash)
        readiness = MarketplaceDraftService.mapping_readiness(
            seller_id=self.seller1_id,
            draft_id=draft.id,
        )
        self.assertEqual(readiness["overall"], "ready")
        self.assertTrue(readiness["source"]["facts_fresh"])

    def test_common_overrides_are_explicit_draft_projection_not_observed_facts(self):
        product = self._product(external_id="common-override-projection")
        original = json.loads(product.original_data)
        first_photo = "https://img.test/common-override-projection.jpg"
        second_photo = "https://img.test/common-override-second.jpg"
        original["photo_urls"] = [first_photo, second_photo]
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.photo_urls = json.dumps(original["photo_urls"])
        db.session.commit()
        original_snapshot = product.original_data

        preview = CommonProductContentService.preview(
            seller_id=self.seller1_id,
            user_id=1,
            raw_items=[{
                "product_id": product.id,
                "expected_content_edit_version": product.content_edit_version,
                "changes": {
                    "title": {"mode": "override", "value": "Моё название"},
                    "description": {"mode": "override", "value": "Моё описание"},
                    "photos": {"mode": "override", "value": [second_photo, first_photo]},
                },
                "recipients": [],
            }],
        )
        CommonProductContentService.apply(
            seller_id=self.seller1_id,
            user_id=1,
            token=preview["preview_token"],
        )
        db.session.commit()
        db.session.refresh(product)

        pack = MarketplaceFactPackBuilder.build(product)
        self.assertEqual(product.original_data, original_snapshot)
        self.assertEqual(pack["facts"]["identity"]["title"], "Моё название")
        self.assertEqual(
            pack["provenance"]["identity.title"]["trust"],
            "seller_override",
        )
        self.assertEqual(
            pack["facts"]["media"]["images"],
            [first_photo, second_photo],
        )
        self.assertEqual(
            pack["seller_common_content"]["fields"]["photos"],
            {"value": [second_photo, first_photo], "origin": "seller_override"},
        )

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        self.assertEqual(
            json.loads(draft.content_json),
            {"name": "Моё название", "description": "Моё описание"},
        )
        self.assertEqual(
            json.loads(draft.media_json),
            {"images": [second_photo, first_photo]},
        )

    def test_intentionally_empty_common_fields_bootstrap_empty_draft_content(self):
        product = self._product(external_id="common-override-empty")
        preview = CommonProductContentService.preview(
            seller_id=self.seller1_id,
            user_id=1,
            raw_items=[{
                "product_id": product.id,
                "expected_content_edit_version": 1,
                "changes": {
                    "description": {"mode": "override", "value": ""},
                    "photos": {"mode": "override", "value": []},
                },
                "recipients": [],
            }],
        )
        CommonProductContentService.apply(
            seller_id=self.seller1_id,
            user_id=1,
            token=preview["preview_token"],
        )
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        self.assertEqual(json.loads(draft.content_json)["description"], "")
        self.assertEqual(json.loads(draft.media_json), {"images": []})

    def test_common_save_does_not_rewrite_an_existing_draft_snapshot(self):
        product = self._product(external_id="common-override-keeps-draft")
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        before_content = draft.content_json
        before_media = draft.media_json
        before_version = draft.version
        preview = CommonProductContentService.preview(
            seller_id=self.seller1_id,
            user_id=1,
            raw_items=[{
                "product_id": product.id,
                "expected_content_edit_version": 1,
                "changes": {"title": {"mode": "override", "value": "После создания draft"}},
                "recipients": [{"kind": "marketplace_draft", "id": draft.id}],
            }],
        )
        CommonProductContentService.apply(
            seller_id=self.seller1_id,
            user_id=1,
            token=preview["preview_token"],
        )
        db.session.commit()
        db.session.refresh(draft)

        self.assertEqual(draft.content_json, before_content)
        self.assertEqual(draft.media_json, before_media)
        self.assertEqual(draft.version, before_version)

    def test_fact_pack_v3_builds_and_rebases_a_fact_only_description(self):
        product = self._product(
            external_id="fact-description-v3",
            dimensions=False,
            ai_physical=True,
        )
        product.description = None
        original = json.loads(product.original_data)
        original.pop("description", None)
        original.update({
            "colors": ["чёрный"],
            "gender": "для женщин",
            "materials": ["94% нейлон", "6% спандекс"],
            "sizes_raw": "универсальный (42-48)",
        })
        original["characteristics"]["Цена поставщика"] = "999"
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data
        db.session.commit()

        pack = MarketplaceFactPackBuilder.build(product)
        description = MarketplaceDraftService._content_from_facts(pack)[
            "description"
        ]
        for expected in (
            "Футболка.",
            "Бренд: Наблюдаемый бренд.",
            "Категория: Футболки.",
            "Цвет: чёрный.",
            "Материал: 94% нейлон, 6% спандекс.",
            "Размер: универсальный (42-48).",
            "Пол: для женщин.",
            "Страна производства: Россия.",
        ):
            self.assertIn(expected, description)
        self.assertNotIn("999", description)
        self.assertNotIn("Выдуманная", description)

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        self.assertEqual(
            json.loads(draft.content_json)["description"],
            description,
        )

        # A stored v2 draft had no deterministic fallback.  The version bump
        # makes the three-way rebase see a newly available default and fill
        # only the still-empty field.
        legacy_facts = json.loads(draft.source_facts_json)
        legacy_facts["version"] = 2
        draft.source_facts_json = json.dumps(legacy_facts, ensure_ascii=False)
        draft.source_fact_hash = "0" * 64
        draft.content_json = json.dumps({"name": product.title})
        db.session.commit()
        draft = MarketplaceDraftService.get_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
        )
        rebased = MarketplaceDraftService.rebase_source_defaults(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        self.assertEqual(
            json.loads(rebased.content_json)["description"],
            description,
        )
        self.assertEqual(json.loads(rebased.source_facts_json)["version"], 3)

        legacy_facts = json.loads(rebased.source_facts_json)
        legacy_facts["version"] = 2
        rebased.source_facts_json = json.dumps(
            legacy_facts,
            ensure_ascii=False,
        )
        rebased.source_fact_hash = "1" * 64
        rebased.content_json = json.dumps({
            "name": product.title,
            "description": "Ручное описание продавца",
        }, ensure_ascii=False)
        db.session.commit()
        rebased = MarketplaceDraftService.get_draft(
            seller_id=self.seller1_id,
            draft_id=rebased.id,
        )
        preserved = MarketplaceDraftService.rebase_source_defaults(
            seller_id=self.seller1_id,
            draft_id=rebased.id,
            expected_version=rebased.version,
        )
        self.assertEqual(
            json.loads(preserved.content_json)["description"],
            "Ручное описание продавца",
        )

    def test_fact_pack_normalizes_observed_supplier_photo_objects(self):
        product = self._product(external_id="legacy-photo-objects")
        original = json.loads(product.original_data)
        original["photo_urls"] = [
            {
                "sexoptovik": "https://source.test/one.jpg",
                "original": "https://fallback.test/one.jpg",
                "processed": "https://ai.test/forbidden.jpg",
            },
            {"original": "https://source.test/two.jpg"},
            {"original": "https://source.test/two.jpg"},
            {"processed": "https://ai.test/ignored.jpg"},
        ]
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.photo_urls = json.dumps(original["photo_urls"])
        db.session.commit()

        pack = MarketplaceFactPackBuilder.build(product)
        self.assertEqual(pack["facts"]["media"]["images"], [
            "https://source.test/one.jpg",
            "https://source.test/two.jpg",
        ])
        self.assertNotIn(
            "ai.test",
            json.dumps(pack["facts"], ensure_ascii=False),
        )
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        self.assertEqual(
            draft.to_public_dict(detail=True)["media"]["images"],
            [
                "https://source.test/one.jpg",
                "https://source.test/two.jpg",
            ],
        )

    def test_fact_pack_promotes_literal_supplier_sizes_raw_only(self):
        product = self._product(external_id="observed-sizes-raw")
        original = json.loads(product.original_data)
        original.pop("sizes", None)
        original["sizes_raw"] = "универсальный (42-48)"
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data
        product.sizes = json.dumps({
            "raw": "выдуманный AI размер",
            "ai_characteristics": {"Размер": "XXXL"},
        }, ensure_ascii=False)
        db.session.commit()

        pack = MarketplaceFactPackBuilder.build(product)

        self.assertEqual(
            pack["facts"]["attributes"]["sizes"],
            {"raw": "универсальный (42-48)"},
        )
        self.assertEqual(
            pack["provenance"]["attributes.sizes"],
            {
                "source": "imported_product.original_data.sizes_raw",
                "trust": "observed",
            },
        )
        self.assertNotIn(
            "XXXL",
            json.dumps(pack["facts"], ensure_ascii=False),
        )

    def test_account_commercial_defaults_remove_repeated_card_inputs(self):
        self.account1.settings_json = json.dumps({"default_vat": "0.22"})
        db.session.commit()
        product = self._product(external_id="commercial-defaults")

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        commercial = draft.to_public_dict(detail=True)["commercial"]

        self.assertEqual(commercial["currency_code"], "RUB")
        self.assertEqual(commercial["vat"], "0.22")
        self.assertEqual(commercial["price"], "1000")
        self.assertEqual(commercial["old_price"], "1200")

    def test_observed_rrp_and_russian_dimensions_build_complete_defaults(self):
        self.account1.settings_json = json.dumps({"default_vat": "0.22"})
        product = self._product(
            external_id="russian-dimensions",
            dimensions=False,
        )
        original = json.loads(product.original_data)
        original["dimensions"] = {
            "Ширина упаковки, см": "3.5",
            "Высота упаковки, см": "14",
            "Длина упаковки, см": "6",
            "Вес упаковки, кг": "0.123",
        }
        original["recommended_retail_price"] = 1050
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.calculated_price = None
        product.calculated_price_before_discount = None
        product.recommended_retail_price = None
        db.session.commit()

        pack = MarketplaceFactPackBuilder.build(product)
        self.assertEqual(pack["facts"]["commercial"]["price"], 1050.0)
        self.assertEqual(
            pack["provenance"]["commercial.price"],
            {
                "source": (
                    "imported_product.original_data."
                    "recommended_retail_price"
                ),
                "trust": "observed",
            },
        )
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        detail = draft.to_public_dict(detail=True)
        self.assertEqual(detail["dimensions"], {
            "width": "35",
            "height": "140",
            "depth": "60",
            "dimension_unit": "MILLIMETERS",
            "weight": "123",
            "weight_unit": "GRAMS",
        })
        self.assertEqual(detail["commercial"], {
            "currency_code": "RUB",
            "price": "1050",
            "vat": "0.22",
        })

    def test_generic_product_measurements_are_not_package_dimensions(self):
        product = self._product(
            external_id="product-measurements-only",
            dimensions=False,
        )
        original = json.loads(product.original_data)
        original["dimensions"] = {
            "length_cm": 30,
            "width_cm": 5,
            "height_cm": 4,
            "weight_g": 200,
        }
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )

        self.assertEqual(
            draft.to_public_dict(detail=True)["dimensions"],
            {},
        )

    def test_fresh_exact_wb_package_dimensions_fill_missing_source_package(self):
        product = self._product(
            external_id="fresh-wb-package",
            dimensions=False,
        )
        original = json.loads(product.original_data)
        original["dimensions"] = {
            "length_cm": 99,
            "diameter_cm": 7,
            "weight_g": 180,
        }
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data
        wb_product = Product(
            seller_id=self.seller1_id,
            nm_id=700_001,
            vendor_code=product.external_vendor_code,
            title=product.title,
            subject_id=123,
            dimensions_json=json.dumps({
                "length": 31,
                "width": 8,
                "height": 6,
                "weightBrutto": 0.24,
                "isValid": True,
            }),
            last_sync=self.now,
        )
        db.session.add(wb_product)
        db.session.flush()
        product.product_id = wb_product.id
        db.session.commit()

        pack = MarketplaceFactPackBuilder.build(product)
        self.assertEqual(
            pack["facts"]["physical"]["wb_package_dimensions"],
            {
                "length_cm": 31,
                "width_cm": 8,
                "height_cm": 6,
                "weight_kg": 0.24,
            },
        )
        self.assertEqual(
            pack["provenance"]["physical.wb_package_dimensions"],
            {
                "source": "product.dimensions_json",
                "trust": "marketplace_observed",
            },
        )

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        self.assertEqual(
            draft.to_public_dict(detail=True)["dimensions"],
            {
                "depth": "310",
                "dimension_unit": "MILLIMETERS",
                "height": "60",
                "weight": "240",
                "weight_unit": "GRAMS",
                "width": "80",
            },
        )

    def test_stale_or_invalid_wb_package_dimensions_are_not_promoted(self):
        for index, (suffix, last_sync, is_valid) in enumerate((
            ("stale", self.now - timedelta(hours=49), True),
            ("invalid", self.now, False),
        )):
            with self.subTest(suffix=suffix):
                product = self._product(
                    external_id=f"wb-package-{suffix}",
                    dimensions=False,
                )
                wb_product = Product(
                    seller_id=self.seller1_id,
                    nm_id=700_010 + index,
                    vendor_code=product.external_vendor_code,
                    title=product.title,
                    subject_id=123,
                    dimensions_json=json.dumps({
                        "length": 31,
                        "width": 8,
                        "height": 6,
                        "weightBrutto": 0.24,
                        "isValid": is_valid,
                    }),
                    last_sync=last_sync,
                )
                db.session.add(wb_product)
                db.session.flush()
                product.product_id = wb_product.id
                db.session.commit()

                pack = MarketplaceFactPackBuilder.build(product)
                self.assertNotIn(
                    "wb_package_dimensions",
                    pack["facts"].get("physical", {}),
                )

    def test_official_type_name_exactly_fills_ozon_type_attribute(self):
        type_attribute = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id=(
                MarketplaceDraftService.OZON_TYPE_ATTRIBUTE_ID
            ),
            name="Тип",
            data_type="String",
            is_required=True,
            dictionary_id="type-directory",
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
            values_synced_at=self.now,
            values_sync_status="success",
            values_snapshot_hash="type-values",
            values_version=1,
            values_count=2,
        )
        db.session.add(type_attribute)
        db.session.flush()
        db.session.add_all([
            MarketplaceAttributeValue(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                attribute_id=type_attribute.id,
                external_value_id="type-exact",
                value=self.product_type.name,
                value_normalized=OzonReferenceService.normalize_value(
                    self.product_type.name
                ),
                is_available=True,
                last_seen_at=self.now,
            ),
            MarketplaceAttributeValue(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                attribute_id=type_attribute.id,
                external_value_id="type-other",
                value="Другой тип",
                value_normalized=OzonReferenceService.normalize_value(
                    "Другой тип"
                ),
                is_available=True,
                last_seen_at=self.now,
            ),
        ])
        db.session.commit()
        product = self._product(external_id="official-type-default")

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        item = next(
            value
            for value in json.loads(draft.attributes_json)
            if value["attribute_id"]
            == MarketplaceDraftService.OZON_TYPE_ATTRIBUTE_ID
        )
        self.assertEqual(item["values"], [{
            "dictionary_value_id": "type-exact",
            "value": self.product_type.name,
        }])

    def test_observed_adult_product_facts_fill_exact_optional_ozon_fields(self):
        product = self._product(
            external_id="observed-adult-fields",
            category="Насадки и кольца > Удлиняющие и расширяющие насадки",
            dimensions=False,
        )
        original = json.loads(product.original_data)
        original.update({
            "title": (
                "Водонепроницаемая насадка на член с присоской, "
                "10 режимов, двумя моторами и стимулятором клитора"
            ),
            "category": (
                "Насадки и кольца > "
                "Удлиняющие и расширяющие насадки"
            ),
            "all_categories": [
                (
                    "Насадки и кольца > "
                    "Удлиняющие и расширяющие насадки"
                ),
                "Насадки и кольца > С вибрацией, с ротацией",
            ],
            "materials": [
                "Эластичный TPR (термоэластопласт)",
            ],
            "gender": "для пары",
            "colors": ["черный"],
            "dimensions": {
                "length_cm": 19.2,
                "min_diameter_cm": 3.5,
                "max_diameter_cm": 4.2,
            },
        })
        product.title = "Насадка на член с вибрацией"
        product.category = original["category"]
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data

        def dictionary_attribute(
            external_id,
            name,
            values,
            *,
            is_collection=False,
            max_value_count=1,
        ):
            attribute = MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                external_attribute_id=external_id,
                name=name,
                data_type="String",
                is_required=False,
                dictionary_id=f"dictionary-{external_id}",
                max_value_count=max_value_count,
                is_collection=is_collection,
                is_available=True,
                is_enabled=True,
                last_seen_at=self.now,
                values_synced_at=self.now,
                values_sync_status="success",
                values_snapshot_hash=f"values-{external_id}",
                values_version=1,
                values_count=len(values),
            )
            db.session.add(attribute)
            db.session.flush()
            for index, value in enumerate(values, start=1):
                db.session.add(MarketplaceAttributeValue(
                    marketplace_id=self.marketplace.id,
                    product_type_id=self.product_type.id,
                    attribute_id=attribute.id,
                    external_value_id=f"{external_id}-{index}",
                    value=value,
                    value_normalized=OzonReferenceService.normalize_value(
                        value
                    ),
                    is_available=True,
                    last_seen_at=self.now,
                ))

        for external_id, name, values, collection, maximum in (
            (
                "4541",
                "Материал",
                ["Термопластичная резина (TPR)"],
                True,
                0,
            ),
            ("4578", "Вибрация", ["С вибрацией"], False, 1),
            ("22845", "Вид насадки", ["На член"], False, 1),
            (
                "4543",
                "Назначение товара 18+",
                [
                    "Для увеличения члена",
                    "Для клиторальной стимуляции",
                ],
                True,
                4,
            ),
            (
                "4574",
                "Вид стимулятора",
                ["Клиторальный"],
                True,
                0,
            ),
            (
                "4559",
                "Особенности 18+",
                [
                    "Два мотора",
                    "С вращением",
                    "Водонепроницаемость",
                    "На присоске",
                ],
                True,
                0,
            ),
            ("4539", "Пол", ["Унисекс"], False, 1),
            (
                "12817",
                "Размер секс-игрушек",
                ["Medium: 13-20 см"],
                False,
                1,
            ),
        ):
            dictionary_attribute(
                external_id,
                name,
                values,
                is_collection=collection,
                max_value_count=maximum,
            )

        for external_id, name, data_type in (
            ("4180", "Название", "String"),
            ("10097", "Название цвета", "String"),
            ("4566", "Длина, см", "Decimal"),
            ("4568", "Ширина/диаметр, мм", "Decimal"),
            ("4579", "Количество режимов", "Integer"),
            ("20693", "Способ крепления 18+", "String"),
            ("9024", "Код продавца", "String"),
        ):
            db.session.add(MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                external_attribute_id=external_id,
                name=name,
                data_type=data_type,
                is_required=False,
                max_value_count=1,
                is_collection=False,
                is_available=True,
                is_enabled=True,
                last_seen_at=self.now,
            ))
        db.session.commit()

        fact_pack = MarketplaceFactPackBuilder.build(product)
        self.assertEqual(
            fact_pack["facts"]["identity"]["source_title"],
            original["title"],
        )
        self.assertEqual(
            fact_pack["facts"]["identity"]["source_categories"],
            original["all_categories"],
        )
        self.assertEqual(
            fact_pack["provenance"]["identity.source_categories"]["trust"],
            "observed",
        )

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        by_id = {
            item["attribute_id"]: item["values"]
            for item in json.loads(draft.attributes_json)
        }
        self.assertEqual(by_id["4541"], [{
            "dictionary_value_id": "4541-1",
            "value": "Термопластичная резина (TPR)",
        }])
        self.assertEqual(by_id["4578"], [{
            "dictionary_value_id": "4578-1",
            "value": "С вибрацией",
        }])
        self.assertEqual(
            [value["value"] for value in by_id["4543"]],
            [
                "Для увеличения члена",
                "Для клиторальной стимуляции",
            ],
        )
        self.assertEqual(
            [value["value"] for value in by_id["4559"]],
            [
                "Два мотора",
                "Водонепроницаемость",
                "На присоске",
            ],
        )
        self.assertEqual(by_id["4539"][0]["value"], "Унисекс")
        self.assertEqual(by_id["4579"], [{"value": "10"}])
        self.assertEqual(by_id["4566"], [{"value": "19.2"}])
        self.assertEqual(by_id["4568"], [{"value": "42"}])
        self.assertEqual(by_id["20693"], [{"value": "На член"}])
        self.assertEqual(by_id["4180"], [{
            "value": "Насадка на член с вибрацией",
        }])
        self.assertEqual(by_id["10097"], [{"value": "черный"}])
        self.assertEqual(by_id["9024"], [{
            "value": "offer-observed-adult-fields",
        }])
        self.assertEqual(by_id["12817"], [{
            "dictionary_value_id": "12817-1",
            "value": "Medium: 13-20 см",
        }])

    def test_lubricant_defaults_use_exact_facts_without_inventing_taste(self):
        self.product_type.name = "Лубрикант"
        product = self._product(
            external_id="observed-lubricant",
            category="Смазки, косметика > Вагинальные смазки",
            dimensions=False,
        )
        original = json.loads(product.original_data)
        original.update({
            "title": (
                "Силиконовый лубрикант без запаха "
                "TOKO Silicone (165 мл)"
            ),
            "category": "Смазки, косметика > Вагинальные смазки",
            "all_categories": [
                "Смазки, косметика > Вагинальные смазки",
            ],
            "characteristics": {
                "Состав": "Силикон",
            },
            "sizes_raw": "165 мл",
        })
        product.title = original["title"]
        product.category = original["category"]
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data

        for external_id, name, values, collection, maximum in (
            ("4564", "Аромат 18+", ["Без аромата"], True, 3),
            ("4561", "Вкус 18+", ["Без вкуса"], True, 3),
            ("9586", "Основа состава", ["Силиконовая"], False, 1),
            ("8219", "Материал", ["Силикон"], True, 3),
            ("21865", "Область использования", ["Вагинальная"], True, 6),
            (
                "4548",
                "Эффект интимного средства",
                ["Скольжения"],
                True,
                6,
            ),
            ("4552", "Текстура", ["Жидкая"], True, 3),
        ):
            self._dictionary_attribute(
                external_id,
                name,
                values,
                is_collection=collection,
                max_value_count=maximum,
            )
        for external_id, name, data_type in (
            ("9070", "Признак 18+", "Boolean"),
            ("8163", "Объем, мл", "Decimal"),
            ("23171", "#Хештеги", "String"),
        ):
            db.session.add(MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                external_attribute_id=external_id,
                name=name,
                data_type=data_type,
                is_required=False,
                max_value_count=1,
                is_collection=False,
                is_available=True,
                is_enabled=True,
                last_seen_at=self.now,
            ))
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        by_id = {
            item["attribute_id"]: item["values"]
            for item in json.loads(draft.attributes_json)
        }

        self.assertEqual(by_id["9070"], [{"value": "true"}])
        self.assertEqual(by_id["4564"][0]["value"], "Без аромата")
        self.assertEqual(by_id["9586"][0]["value"], "Силиконовая")
        self.assertEqual(by_id["8219"][0]["value"], "Силикон")
        self.assertEqual(by_id["21865"][0]["value"], "Вагинальная")
        self.assertEqual(by_id["4548"][0]["value"], "Скольжения")
        self.assertEqual(by_id["8163"], [{"value": "165"}])
        self.assertEqual(
            by_id["23171"],
            [{"value": "#интимный_уход #лубрикант"}],
        )
        self.assertNotIn("4561", by_id)
        self.assertNotIn("4552", by_id)

    def test_auto_mapping_preserves_values_over_schema_limit_for_preflight(self):
        product = self._product(
            external_id="material-over-limit",
            dimensions=False,
        )
        original = json.loads(product.original_data)
        original["materials"] = [
            "Силикон",
            "Термопластичная резина (TPR)",
        ]
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data
        self._dictionary_attribute(
            "4541",
            "Материал",
            ["Силикон", "Термопластичная резина (TPR)"],
            is_collection=True,
            max_value_count=1,
        )
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        attributes = json.loads(draft.attributes_json)
        material_index = next(
            index for index, item in enumerate(attributes)
            if item["attribute_id"] == "4541"
        )
        self.assertEqual(
            attributes[material_index]["values"],
            [
                {"dictionary_value_id": "4541-1", "value": "Силикон"},
                {
                    "dictionary_value_id": "4541-2",
                    "value": "Термопластичная резина (TPR)",
                },
            ],
        )

        validated = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        errors = validated.to_public_dict(detail=True)["validation"]["errors"]
        limit_error = next(
            item for item in errors
            if item.get("code") == "attribute_max_value_count"
            and item.get("attribute_id") == "4541"
        )
        self.assertEqual(
            limit_error["field"],
            f"attributes[{material_index}].values",
        )
        self.assertEqual(limit_error["attribute_name"], "Материал")
        self.assertEqual(limit_error["actual_count"], 2)
        self.assertEqual(limit_error["max_value_count"], 1)
        self.assertEqual(limit_error["schema_max_value_count"], 1)
        self.assertIn("ID 4541", limit_error["message"])
        self.assertIn("передано значений 2", limit_error["message"])
        self.assertIn("допустимо не более 1", limit_error["message"])

    def test_category_recipes_use_only_literal_taxonomy_and_title_phrases(self):
        def candidates(title, category):
            return MarketplaceDraftService._attribute_candidate_values({
                "facts": {
                    "identity": {
                        "source_title": title,
                        "source_category": category,
                        "source_categories": [category],
                    },
                    "attributes": {
                        "characteristics": [],
                    },
                    "identifiers": {
                        "vendor_code": "source-code",
                    },
                    "physical": {},
                },
            })

        lubricant = candidates(
            "Охлаждающий лубрикант на водной основе 50 мл",
            "Смазки, косметика > Оральные смазки",
        )
        self.assertEqual(lubricant["основа состава"], "Водная")
        self.assertEqual(
            lubricant["область использования"],
            "Пероральная",
        )
        self.assertEqual(
            lubricant["эффект интимного средства"],
            ["Скольжения", "Охлаждающий"],
        )
        self.assertEqual(lubricant["объем, мл"], "50")

        espresso = MarketplaceDraftService._attribute_candidate_values({
            "facts": {
                "identity": {
                    "source_title": (
                        "Вкусовой лубрикант на водной основе "
                        "Hazelnut Espresso 30 мл"
                    ),
                    "source_category": (
                        "Смазки, косметика > Вагинальные смазки"
                    ),
                    "source_categories": [
                        "Смазки, косметика > Вагинальные смазки",
                        "Смазки, косметика > Оральные смазки",
                    ],
                },
                "attributes": {"characteristics": []},
                "physical": {},
            },
        })
        self.assertEqual(
            espresso["вкус презервативов, средств для взрослых"],
            "Кофе",
        )
        self.assertEqual(espresso["текстура"], "На водной основе")
        self.assertEqual(
            espresso["область использования"],
            ["Вагинальная", "Пероральная"],
        )

        rotation = candidates(
            "Вибратор-ротатор",
            "Вибраторы и фаллоимитаторы > С ротацией (вращение)",
        )
        self.assertEqual(rotation["особенности 18+"], ["С вращением"])
        mixed_rotation = candidates(
            "Эрекционное кольцо с вибрацией",
            "Насадки и кольца > С вибрацией, с ротацией",
        )
        self.assertNotIn("особенности 18+", mixed_rotation)

        physical_set = MarketplaceDraftService._attribute_candidate_values({
            "facts": {
                "identity": {
                    "source_title": (
                        "Интимный набор: вакуумный стимулятор и "
                        "виброяйцо с пультом управления"
                    ),
                    "source_category": "Секс-наборы > Секс-наборы",
                    "source_categories": ["Секс-наборы > Секс-наборы"],
                },
                "attributes": {
                    "characteristics": [],
                    "materials": [
                        "Высококачественный силикон "
                        "с бархатистой поверхностью"
                    ],
                },
                "physical": {
                    "dimensions": {"working_length_cm": 12},
                },
            },
        })
        self.assertEqual(physical_set["материал"], ["Силикон"])
        self.assertEqual(physical_set["длина рабочей части, мм"], Decimal("120"))
        self.assertEqual(
            physical_set["состав комплекта"],
            "Вакуумный стимулятор; Виброяйцо; Пульт управления",
        )
        self.assertEqual(
            physical_set["особенности 18+"],
            ["С пультом управления", "Бархатистая поверхность", "Набор"],
        )

        clitoral = candidates(
            "Стимулятор клитора",
            "Женские стимуляторы > Клиторально-вагинальные стимуляторы",
        )
        self.assertEqual(
            clitoral["назначение товара 18+"],
            [
                "Для клиторальной стимуляции",
                "Для вагинального секса",
            ],
        )

        self.assertEqual(
            candidates(
                "Реалистичная вагина",
                "Мастурбаторы и вагины > Вагины без вибрации",
            )["тип мастурбатора"],
            "Вагина",
        )
        self.assertEqual(
            candidates(
                "Страпон-трусики",
                "Страпоны и фаллопротезы > Трусики и насадки",
            )["тип страпона"],
            "Трусики",
        )
        self.assertEqual(
            candidates(
                "Кожаный ошейник",
                "БДСМ товары и фетиш > Ошейники, поводки",
            )["тип аксессуара бдсм"],
            "Ошейник, поводок",
        )
        self.assertEqual(
            candidates(
                "Мягкие наручники",
                "БДСМ товары и фетиш > Наручники, фиксаторы на руки",
            )["тип фиксатора бдсм"],
            "Наручники",
        )
        self.assertEqual(
            candidates(
                "Ультратонкие латексные презервативы",
                "Презервативы > Обычные",
            )["вид презерватива"],
            ["Классические", "Ультратонкие", "Латексные"],
        )
        self.assertEqual(
            candidates(
                "Анальный душ",
                "Анальные стимуляторы и пробки > Анальный душ",
            )["тип стимулятора"],
            "Душ",
        )
        self.assertEqual(
            MarketplaceDraftService._merged_official_hashtags(
                "#для_взрослых",
                product_type_name="Анальная пробка",
            ),
            "#для_взрослых #анальная_пробка",
        )

    def test_clothing_defaults_expand_sizes_and_keep_adult_scope_isolated(self):
        self.assertEqual(
            MarketplaceDraftService._observed_material_composition([
                "100% полиэстер",
            ]),
            "100% полиэстер",
        )
        self.assertEqual(
            MarketplaceDraftService._observed_material_composition([
                "90% полиэстер",
                "10% эластан",
            ]),
            "90% полиэстер, 10% эластан",
        )
        self.assertEqual(
            MarketplaceDraftService._observed_clothing_sizes({
                "raw": "2/3",
            }),
            (["2", "3"], "2/3"),
        )
        self.assertEqual(
            MarketplaceDraftService._observed_clothing_sizes({
                "raw": "165 мл",
            }),
            ([], ""),
        )
        self.assertEqual(
            MarketplaceDraftService._observed_clothing_sizes({
                "raw": "длина лент 38-40 см",
            }),
            ([], ""),
        )
        for raw, expected in (
            ("46-48 (об. бедер 96-100 см)", ["46", "48"]),
            ("46 (об. груди 96-100 см)", ["46"]),
            ("S (42-44)", ["42", "44"]),
            ("2 (S), длина ступни 23-24 см", ["2"]),
            ("3 M, длина ступни 25-26 см", ["3"]),
            ("40-42 размер", ["40", "42"]),
            (
                "об. груди: 91-107 см, об. бедер: 97-112 см (46-48)",
                ["46", "48"],
            ),
            ("4 (длина), длина ступни 27-28 см", ["4"]),
            (
                "4 (длина) на рост 175-182 см, обхват бедер 112-116 см",
                ["4"],
            ),
            ("3 (объем бедер 101-108 см)", ["3"]),
            ("85 C", ["85C"]),
            ("1-2, длина стопы 23-25 см", ["1", "2"]),
            ("3-4 (Mдлина), длина ступни 26-28 см", ["3", "4"]),
            (r"5\\6 размер", ["5", "6"]),
            ("размер 3, 20 диаметр", ["3"]),
            ("52 российский размер", ["52"]),
        ):
            with self.subTest(raw=raw):
                sizes, literal = (
                    MarketplaceDraftService._observed_clothing_sizes({
                        "raw": raw,
                    })
                )
                self.assertEqual(sizes, expected)
                self.assertEqual(literal, raw)
        self.assertEqual(
            MarketplaceDraftService._observed_clothing_sizes({
                "raw": (
                    "об. груди: 81-96 см, об. талии: 61-76 см, "
                    "об. бедер: 86-101 см"
                ),
            }),
            ([], ""),
        )
        for measurement in ("3-4 см", "размер 3 см"):
            with self.subTest(measurement=measurement):
                self.assertEqual(
                    MarketplaceDraftService._observed_clothing_sizes({
                        "raw": measurement,
                    }),
                    ([], ""),
                )
        self.assertEqual(
            MarketplaceDraftService._observed_wearable_size({
                "raw": "универсальный, ширина 6 см",
            }),
            "Универсальный",
        )
        self.assertEqual(
            MarketplaceDraftService._observed_wearable_size({
                "raw": "50 мл",
            }),
            "",
        )
        product = self._product(
            external_id="observed-erotic-clothing",
            category="Эротическое белье для женщин > Комбинезоны",
            dimensions=False,
        )
        original = json.loads(product.original_data)
        original.update({
            "title": "Сетчатый комбинезон",
            "category": "Эротическое белье для женщин > Комбинезоны",
            "all_categories": [
                "Эротическое белье для женщин > Комбинезоны",
            ],
            "gender": "для женщин",
            "colors": ["чёрный"],
            "materials": ["94% нейлон", "6% спандекс"],
            "sizes_raw": "универсальный (42-48)",
        })
        product.title = original["title"]
        product.category = original["category"]
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data
        self.brand_attribute.name = "Бренд в одежде и обуви"

        self._dictionary_attribute(
            "4295",
            "Российский размер",
            ["42", "44", "46", "48"],
            is_collection=True,
            max_value_count=0,
            is_required=True,
        )
        self._dictionary_attribute(
            "10096",
            "Цвет товара",
            ["Черный"],
            is_collection=True,
            max_value_count=0,
            is_required=True,
        )
        self._dictionary_attribute(
            "9163",
            "Пол",
            ["Женский", "Мужской"],
            is_collection=True,
            max_value_count=0,
            is_required=True,
        )
        self._dictionary_attribute(
            "4496",
            "Материал",
            ["нейлон", "спандекс"],
            is_collection=True,
            max_value_count=4,
        )
        for external_id, name, data_type, required in (
            ("8292", "Объединить на одной карточке", "String", True),
            ("9533", "Размер производителя", "String", False),
            ("4604", "Состав материала", "String", False),
            ("9070", "Признак 18+", "Boolean", False),
            ("23171", "#Хештеги", "String", False),
        ):
            db.session.add(MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                external_attribute_id=external_id,
                name=name,
                data_type=data_type,
                is_required=required,
                max_value_count=1,
                is_collection=False,
                is_available=True,
                is_enabled=True,
                last_seen_at=self.now,
            ))
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        by_id = {
            item["attribute_id"]: item["values"]
            for item in json.loads(draft.attributes_json)
        }

        self.assertEqual(
            [value["value"] for value in by_id["4295"]],
            ["42", "44", "46", "48"],
        )
        self.assertEqual(by_id["10096"][0]["value"], "Черный")
        self.assertEqual(by_id["9163"][0]["value"], "Женский")
        self.assertEqual(
            [value["value"] for value in by_id["4496"]],
            ["нейлон", "спандекс"],
        )
        self.assertEqual(by_id["8292"], [{
            "value": product.external_vendor_code,
        }])
        self.assertEqual(by_id["9533"], [{
            "value": "универсальный (42-48)",
        }])
        self.assertEqual(by_id["4604"], [{
            "value": "94% нейлон, 6% спандекс",
        }])
        self.assertEqual(by_id["9070"], [{"value": "true"}])
        self.assertEqual(
            by_id["23171"],
            [{"value": "#эротический_образ #футболка"}],
        )

        ordinary = self._product(
            external_id="ordinary-clothing",
            category="Одежда > Футболки",
            dimensions=False,
        )
        ordinary_original = json.loads(ordinary.original_data)
        ordinary_original.update({
            "category": "Одежда > Футболки",
            "sizes_raw": "44",
            "gender": "для женщин",
        })
        ordinary.original_data = json.dumps(
            ordinary_original,
            ensure_ascii=False,
        )
        ordinary.supplier_product.original_data_json = ordinary.original_data
        db.session.commit()
        ordinary_draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=ordinary.id,
            product_type_id=self.product_type.id,
        )
        ordinary_ids = {
            item["attribute_id"]
            for item in json.loads(ordinary_draft.attributes_json)
        }
        self.assertNotIn("9070", ordinary_ids)
        self.assertIn("23171", ordinary_ids)
        ordinary_attributes = {
            item["attribute_id"]: item["values"]
            for item in json.loads(ordinary_draft.attributes_json)
        }
        self.assertEqual(
            ordinary_attributes["23171"],
            [{"value": "#футболка"}],
        )

    def test_account_defaults_upgrade_old_draft_without_overwriting_card_vat(self):
        self.account1.settings_json = json.dumps({"default_vat": "0.2"})
        product = self._product(external_id="old-commercial-draft")
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        draft.commercial_json = json.dumps({
            "price": "1000",
            "old_price": "1200",
        })
        db.session.commit()

        upgraded = MarketplaceDraftService.apply_account_defaults(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        commercial = upgraded.to_public_dict(detail=True)["commercial"]
        self.assertEqual(commercial["currency_code"], "RUB")
        self.assertEqual(commercial["vat"], "0.2")

        explicit = MarketplaceDraftService.update_draft(
            seller_id=self.seller1_id,
            draft_id=upgraded.id,
            expected_version=upgraded.version,
            patch={"commercial": {
                **commercial,
                "vat": "0.22",
            }},
        )
        self.account1.settings_json = json.dumps({"default_vat": "0.1"})
        db.session.commit()
        preserved = MarketplaceDraftService.apply_account_defaults(
            seller_id=self.seller1_id,
            draft_id=explicit.id,
            expected_version=explicit.version,
        )
        self.assertEqual(
            preserved.to_public_dict(detail=True)["commercial"]["vat"],
            "0.22",
        )

    def test_reference_defaults_fill_new_schema_without_overwriting_seller_value(self):
        self.product_type.attributes_synced_at = None
        self.product_type.attributes_schema_hash = None
        self.product_type.attributes_sync_status = None
        db.session.commit()
        product = self._product(external_id="reference-defaults")
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        self.assertEqual(
            draft.to_public_dict(detail=True)["attributes"],
            [],
        )
        edited = MarketplaceDraftService.update_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
            patch={"attributes": [{
                "attribute_id": "31",
                "complex_id": "0",
                "values": [{"value": "Выбор продавца"}],
            }]},
        )

        self.product_type.attributes_synced_at = self.now
        self.product_type.attributes_schema_hash = "fresh-schema"
        self.product_type.attributes_sync_status = "success"
        db.session.commit()
        upgraded = MarketplaceDraftService.apply_reference_defaults(
            seller_id=self.seller1_id,
            draft_id=edited.id,
            expected_version=edited.version,
        )
        attributes = {
            item["attribute_id"]: item
            for item in upgraded.to_public_dict(detail=True)["attributes"]
        }
        self.assertEqual(
            attributes["31"]["values"],
            [{"value": "Выбор продавца"}],
        )
        self.assertEqual(
            attributes["32"]["values"],
            [{
                "dictionary_value_id": "9001",
                "value": "Россия",
            }],
        )

    def test_reference_defaults_does_not_falsely_attribute_provenance_to_untouched_compliance_value(
        self,
    ):
        """(Ревью Task 8b, Important 2) `_auto_map_attributes` recomputes
        the compliance layer from scratch on every call regardless of what
        the draft already stores, so its own `compliance_report["applied"]`
        always lists a resolvable ID even when that identity is already
        present in the draft and therefore excluded from `additions`.
        `apply_reference_defaults` must only record provenance for IDs that
        genuinely ended up in `additions` in THIS call -- otherwise a later
        admin decision change would make provenance claim a fresh write of
        the NEW value while the value actually stored on disk stayed the
        OLD one, breaking `compliance_value_is_ours` silently.
        """
        product = self._product(external_id="reference-defaults-compliance")
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        # No active compliance decision exists yet at create time (the
        # fixture's product_type has none), so the draft starts without a
        # 22232 record -- exactly the "type bound before the decision
        # existed" scenario `apply_reference_defaults` targets.
        self.assertNotIn(
            "22232",
            {item["attribute_id"] for item in json.loads(draft.attributes_json)},
        )

        old_defaults = {
            "tnved": {
                "code": "1111111111", "value": "1111111111 - A",
                "external_value_id": "111111111", "default_id": 1,
                "dictionary_version": 1,
            },
            "marking": None,
        }
        with patch.object(
            MarketplaceDraftService,
            "_auto_map_attributes",
            return_value=([{
                "attribute_id": "22232",
                "complex_id": "0",
                "values": [{
                    "dictionary_value_id": "111111111",
                    "value": "1111111111 - A",
                }],
            }], {
                "applied": ["22232"], "unresolved": [], "evidence": {},
                "defaults": old_defaults,
            }),
        ):
            first = MarketplaceDraftService.apply_reference_defaults(
                seller_id=self.seller1_id,
                draft_id=draft.id,
                expected_version=draft.version,
            )

        provenance = json.loads(first.provenance_json)
        self.assertEqual(
            provenance["compliance.22232"]["external_value_id"], "111111111",
        )

        # Admin fixes the decision; a NEW attribute also becomes newly
        # addable in this call (so `additions` is non-empty and the
        # function does not short-circuit before reaching the compliance
        # filter). TNVED already exists by identity, so it must stay
        # excluded from `additions` -- and from provenance.
        new_defaults = dict(old_defaults)
        new_defaults["tnved"] = {
            "code": "2222222222", "value": "2222222222 - B",
            "external_value_id": "222222222", "default_id": 2,
            "dictionary_version": 2,
        }
        with patch.object(
            MarketplaceDraftService,
            "_auto_map_attributes",
            return_value=([
                {
                    "attribute_id": "22232",
                    "complex_id": "0",
                    "values": [{
                        "dictionary_value_id": "222222222",
                        "value": "2222222222 - B",
                    }],
                },
                {
                    "attribute_id": "999",
                    "complex_id": "0",
                    "values": [{"value": "Новое значение"}],
                },
            ], {
                "applied": ["22232"], "unresolved": [], "evidence": {},
                "defaults": new_defaults,
            }),
        ):
            second = MarketplaceDraftService.apply_reference_defaults(
                seller_id=self.seller1_id,
                draft_id=first.id,
                expected_version=first.version,
            )

        attrs = {
            item["attribute_id"]: item
            for item in json.loads(second.attributes_json)
        }
        self.assertEqual(
            attrs["22232"]["values"][0]["dictionary_value_id"], "111111111",
            "Уже присутствующее значение не должно перезаписываться этим путём",
        )
        self.assertIn("999", attrs)

        final_provenance = json.loads(second.provenance_json)
        self.assertEqual(
            final_provenance["compliance.22232"]["external_value_id"],
            "111111111",
            "Провенанс не должен заявлять новое значение, которое "
            "фактически не было записано в этом вызове",
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
        completeness = MarketplaceDraftService.completeness_summary(
            validated,
        )
        self.assertEqual(completeness["required_supplied"], 3)
        self.assertEqual(completeness["required_total"], 3)
        self.assertEqual(completeness["supplied_known"], 3)
        self.assertEqual(completeness["schema_total"], 3)
        self.assertEqual(completeness["image_count"], 1)
        self.assertEqual(completeness["barcode_count"], 1)

        self.assertTrue(completeness["content_complete"])
        self.assertTrue(completeness["physical_complete"])
        self.assertTrue(completeness["commercial_complete"])
        self.assertTrue(completeness["publishable"])
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

    def test_mixed_explicit_package_units_convert_exactly_or_block(self):
        facts = {"facts": {"physical": {"dimensions": {
            "Ширина упаковки, мм": 200,
            "Длина упаковки, мм": 300,
            "package_width": 20,
            "package_height_mm": 30,
            "dimension_unit": "CENTIMETERS",
            "package_weight_g": 250,
        }}}}
        dimensions = MarketplaceDraftService._dimensions_from_facts(facts)
        self.assertEqual(dimensions["dimension_unit"], "MILLIMETERS")
        self.assertEqual(dimensions["width"], "200")
        self.assertEqual(dimensions["height"], "30")
        self.assertEqual(dimensions["depth"], "300")

        facts["facts"]["physical"]["dimensions"].pop("dimension_unit")
        self.assertNotIn(
            "dimension_unit",
            MarketplaceDraftService._dimensions_from_facts(facts),
            "An unlabelled alias cannot inherit the old Russian mm unit",
        )

        facts["facts"]["physical"]["wb_package_dimensions"] = {
            "width_cm": 21,
            "height_cm": 4,
            "length_cm": 31,
            "weight_kg": "0.3",
        }
        self.assertEqual(
            MarketplaceDraftService._dimensions_from_facts(facts),
            {
                "width": "210",
                "height": "40",
                "depth": "310",
                "weight": "300",
                "dimension_unit": "MILLIMETERS",
                "weight_unit": "GRAMS",
            },
        )

        inch_facts = {"facts": {"physical": {"dimensions": {
            "package_width": "1",
            "dimension_unit": "INCHES",
            "package_height_mm": 254,
            "package_depth_mm": 508,
            "package_weight_g": 250,
        }}}}
        self.assertEqual(
            MarketplaceDraftService._dimensions_from_facts(inch_facts),
            {
                "width": "1", "height": "10", "depth": "20",
                "dimension_unit": "INCHES", "weight": "250",
                "weight_unit": "GRAMS",
            },
            "Exact inch conversion should work when mm values are fractional",
        )
        inch_facts["facts"]["physical"]["dimensions"]["package_width"] = "0.1"
        incompatible = MarketplaceDraftService._dimensions_from_facts(inch_facts)
        self.assertNotIn("dimension_unit", incompatible)
        self.assertEqual(incompatible["width"], "0.1")

    def test_package_integer_bounds_match_import_builder(self):
        _, draft = self._ready_draft(external_id="dimension-int32-boundary")
        dimensions = draft.to_public_dict(detail=True)["dimensions"]
        dimensions["width"] = "2147483648"
        changed = MarketplaceDraftService.update_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
            patch={"dimensions": dimensions},
        )
        rejected = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=changed.id,
            expected_version=changed.version,
        )
        errors = rejected.to_public_dict(detail=True)["validation"]["errors"]
        self.assertIn(
            ("physical_fact_out_of_range", "dimensions.width"),
            {(item["code"], item["field"]) for item in errors},
        )
        self.assertEqual(rejected.validation_status, "invalid")

        dimensions["width"] = "2147483647"
        corrected = MarketplaceDraftService.update_draft(
            seller_id=self.seller1_id,
            draft_id=rejected.id,
            expected_version=rejected.version,
            patch={"dimensions": dimensions},
        )
        accepted = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=corrected.id,
            expected_version=corrected.version,
        )
        self.assertTrue(accepted.to_public_dict(detail=True)["validation"]["publishable"])

    def test_explicit_erotic_apparel_taxonomy_is_a_literal_allowlist(self):
        cases = {
            "БДСМ товары и фетиш > Одежда и белье для женщин": (
                "БДСМ одежда"
            ),
            "Аксессуары, украшения для тела > Стикини, пестис": (
                "Пэстисы"
            ),
            "Аксессуары, украшения для тела > Портупеи, стрэпы": (
                "Портупея эротическая"
            ),
            "Эротическое белье для женщин > Игровые костюмы": (
                "Костюм для ролевых игр"
            ),
            "Эротическое белье для женщин > Платья, мини-платья": (
                "Платье гоу-гоу"
            ),
            "Эротическое белье для женщин > Комбинезоны": (
                "Эротическое белье"
            ),
            "Эротическое белье для мужчин > Трусы, стринги, шорты": (
                "Эротическое белье"
            ),
        }
        for source_category, expected_type in cases.items():
            with self.subTest(source_category=source_category):
                rule = MarketplaceDraftService._explicit_source_taxonomy_rule(
                    source_category
                )
                self.assertIsNotNone(rule)
                self.assertEqual(rule["product_type_name"], expected_type)
                self.assertEqual(
                    rule["product_type_path"],
                    MarketplaceDraftService.EXPLICIT_EROTIC_CLOTHING_PATH,
                )

        for ambiguous in (
            "Страпоны и фаллопротезы > Трусики и насадки",
            "Вибраторы и фаллоимитаторы > Реалистичные",
            "Аксессуары, украшения для тела > Гартеры",
            "Аксессуары, украшения для тела > Парики",
            "Эротическое белье для женщин > Маскарадные маски",
            "Эротическое белье для женщин > Накладные парики",
            "Эротическое белье для женщин",
            "Эротическое белье для женщин > Новый неизвестный лист",
        ):
            with self.subTest(ambiguous=ambiguous):
                self.assertIsNone(
                    MarketplaceDraftService._explicit_source_taxonomy_rule(
                        ambiguous
                    )
                )

    def test_explicit_adult_taxonomy_is_a_literal_allowlist(self):
        cases = {
            "Гели, смазки и лубриканты": (
                "Лубрикант",
                MarketplaceDraftService.EXPLICIT_PERSONAL_HYGIENE_PATH,
            ),
            "Смазки, косметика > Вагинальные смазки": (
                "Лубрикант",
                MarketplaceDraftService.EXPLICIT_PERSONAL_HYGIENE_PATH,
            ),
            "Анальные стимуляторы и пробки > Анальные пробки, втулки": (
                "Анальная пробка",
                MarketplaceDraftService.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            "Анальные стимуляторы и пробки > Стимуляторы простаты": (
                "Массажер простаты",
                MarketplaceDraftService.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            "Мастурбаторы и вагины > Мастурбаторы Fleshlight, в колбах": (
                "Мастурбатор",
                MarketplaceDraftService.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            "Насадки и кольца > Эрекционные": (
                "Эрекционное кольцо",
                MarketplaceDraftService.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            "Вакуумные помпы > Насадки на помпу": (
                "Аксессуары для помпы",
                MarketplaceDraftService.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            "Презервативы > Ароматизированные": (
                "Презервативы",
                MarketplaceDraftService.EXPLICIT_PERSONAL_HYGIENE_PATH,
            ),
            "БДСМ товары и фетиш > Уретральные стимуляторы": (
                "Расширитель уретральный",
                MarketplaceDraftService.EXPLICIT_ADULT_BDSM_PATH,
            ),
            "Препараты и возбудители > Пролонгаторы для мужчин": (
                "Пролонгатор",
                MarketplaceDraftService.EXPLICIT_ADULT_COSMETICS_PATH,
            ),
            "Аксессуары для игр > Эротические игры": (
                "Игра эротическая",
                MarketplaceDraftService.EXPLICIT_ADULT_SOUVENIRS_PATH,
            ),
            "Сумочки для хранения > Мешочки": (
                "Хранение секс игрушек",
                MarketplaceDraftService.EXPLICIT_ADULT_CARE_PATH,
            ),
        }
        for source_category, expected in cases.items():
            with self.subTest(source_category=source_category):
                rule = MarketplaceDraftService._explicit_source_taxonomy_rule(
                    source_category
                )
                self.assertIsNotNone(rule)
                self.assertEqual(
                    (rule["product_type_name"], rule["product_type_path"]),
                    expected,
                )

        for ambiguous in (
            "Секс-игрушки",
            "Вибраторы и фаллоимитаторы > Реалистичные",
            "Вибраторы и фаллоимитаторы > Нереалистичные",
            "Анальные стимуляторы и пробки > С вибрацией",
            "Анальные стимуляторы и пробки > Без вибрации",
            "Насадки и кольца > Без вибрации",
            "БДСМ товары и фетиш > Плетки, стеки, шлепалки",
            "Смазки, косметика > Массажные масла, свечи, гели",
            "Страпоны и фаллопротезы > Трусики и насадки",
            "Страпоны и фаллопротезы > Без вибрации",
            "Страпоны и фаллопротезы > С вибрацией",
        ):
            with self.subTest(ambiguous=ambiguous):
                self.assertIsNone(
                    MarketplaceDraftService._explicit_source_taxonomy_rule(
                        ambiguous
                    )
                )

    def test_explicit_adult_mapping_uses_exact_official_path_and_survives_v1(self):
        correct = self._official_type(
            "Лубрикант",
            MarketplaceDraftService.EXPLICIT_PERSONAL_HYGIENE_PATH,
        )
        db.session.add(MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=correct.id,
            external_attribute_id=(
                MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID
            ),
            name="Название модели (для объединения в одну карточку)",
            data_type="String",
            is_required=True,
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
        ))
        self._official_type(
            "Лубрикант",
            MarketplaceDraftService.EXPLICIT_ADULT_COSMETICS_PATH,
        )
        source_category = "Смазки, косметика > Вагинальные смазки"
        product = self._product(
            external_id="explicit-vaginal-lubricant",
            category=source_category,
        )

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            observed_mapping_preflight=False,
        )

        self.assertEqual(draft.product_type_id, correct.id)
        mapping = db.session.get(
            MarketplaceCategoryMapping,
            draft.category_mapping_id,
        )
        evidence = json.loads(mapping.evidence_json)
        self.assertEqual(
            evidence["algorithm"],
            MarketplaceDraftService.EXPLICIT_SOURCE_MAPPING_ALGORITHM,
        )
        self.assertEqual(
            evidence["target_type_path"],
            MarketplaceDraftService.EXPLICIT_PERSONAL_HYGIENE_PATH,
        )
        self.assertEqual(evidence["attribute_recipes"], [{
            "attribute_id": (
                MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID
            ),
            "isolation": "per_imported_product",
            "recipe": MarketplaceDraftService.EXPLICIT_SOURCE_MODEL_RECIPE,
        }])
        attributes = {
            item["attribute_id"]: item["values"]
            for item in json.loads(draft.attributes_json)
        }
        self.assertEqual(
            attributes[MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID],
            [{
                "value": (
                    f"{product.external_vendor_code} · "
                    f"SH-{self.supplier.id}-{product.id}"
                ),
            }],
        )

        # Deployed v1 apparel rows remain protected during a rolling upgrade;
        # the next actual remap may upgrade their evidence to v2.
        evidence["algorithm"] = "ozon-explicit-source-taxonomy-v1"
        mapping.evidence_json = json.dumps(evidence)
        db.session.commit()
        MarketplaceDraftService.reconcile_observed_category_mappings(
            seller_id=self.seller1_id,
            marketplace_id=self.marketplace.id,
        )
        db.session.refresh(mapping)
        self.assertEqual(mapping.mapping_status, "active")
        self.assertEqual(
            json.loads(mapping.evidence_json)["algorithm"],
            "ozon-explicit-source-taxonomy-v1",
        )
        legacy_attributes, _ = MarketplaceDraftService._auto_map_attributes(
            product_type=correct,
            facts_document=MarketplaceFactPackBuilder.build(product),
            category_mapping=mapping,
        )
        self.assertNotIn(
            MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID,
            {
                item["attribute_id"]
                for item in legacy_attributes
            },
        )

    def test_explicit_source_model_keys_isolate_duplicate_vendor_codes(self):
        product_type = self._official_type(
            "Анальная пробка",
            MarketplaceDraftService.EXPLICIT_ADULT_SEX_TOYS_PATH,
        )
        db.session.add(MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=product_type.id,
            external_attribute_id=(
                MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID
            ),
            name="Название модели (для объединения в одну карточку)",
            data_type="String",
            is_required=True,
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
        ))
        source_category = (
            "Анальные стимуляторы и пробки > Анальные пробки, втулки"
        )
        products = [
            self._product(
                external_id=f"duplicate-model-{suffix}",
                category=source_category,
            )
            for suffix in ("first", "second")
        ]
        for product in products:
            original = json.loads(product.original_data)
            original["vendor_code"] = "DUPLICATE-SOURCE-CODE"
            product.original_data = json.dumps(original, ensure_ascii=False)
            product.supplier_product.original_data_json = product.original_data
        db.session.commit()

        model_values = []
        for index, product in enumerate(products, start=1):
            draft = MarketplaceDraftService.create_draft(
                seller_id=self.seller1_id,
                account_id=self.account1.id,
                imported_product_id=product.id,
                offer_id=f"unique-offer-{index}",
                observed_mapping_preflight=False,
            )
            self.assertEqual(draft.product_type_id, product_type.id)
            model = next(
                item
                for item in json.loads(draft.attributes_json)
                if item["attribute_id"]
                == MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID
            )
            model_values.append(model["values"][0]["value"])

        self.assertNotEqual(model_values[0], model_values[1])
        self.assertTrue(all(
            value.startswith("DUPLICATE-SOURCE-CODE · SH-")
            for value in model_values
        ))

    def test_dictionary_duplicate_uses_only_one_literal_display_match(self):
        product_type = self._official_type(
            "Лубрикант",
            MarketplaceDraftService.EXPLICIT_PERSONAL_HYGIENE_PATH,
        )
        brand_attribute = self._dictionary_attribute(
            "85",
            "Бренд",
            ["WET", "Wet", "Other"],
            is_required=True,
            product_type=product_type,
        )
        product_type.attributes_count = 1
        product_type.required_attributes_count = 1
        source_category = "Смазки, косметика > Вагинальные смазки"

        exact = self._product(
            external_id="literal-brand-exact",
            category=source_category,
        )
        ambiguous = self._product(
            external_id="literal-brand-ambiguous",
            category=source_category,
        )
        for product, brand in ((exact, "WET"), (ambiguous, "wet")):
            original = json.loads(product.original_data)
            original["brand"] = brand
            original["characteristics"]["Бренд"] = brand
            product.original_data = json.dumps(original, ensure_ascii=False)
            product.supplier_product.original_data_json = product.original_data
        db.session.commit()

        exact_draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=exact.id,
            observed_mapping_preflight=False,
        )
        exact_brand = next(
            item
            for item in json.loads(exact_draft.attributes_json)
            if item["attribute_id"] == "85"
        )
        self.assertEqual(exact_brand["values"], [{
            "dictionary_value_id": "85-1",
            "value": "WET",
        }])

        ambiguous_draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=ambiguous.id,
            observed_mapping_preflight=False,
        )
        self.assertNotIn(
            "85",
            {
                item["attribute_id"]
                for item in json.loads(ambiguous_draft.attributes_json)
            },
        )

        identity_literal = self._product(
            external_id="literal-brand-identity-display",
            category=source_category,
        )
        original = json.loads(identity_literal.original_data)
        original["brand"] = "WET"
        original["characteristics"]["Бренд"] = "wet"
        identity_literal.original_data = json.dumps(
            original, ensure_ascii=False
        )
        identity_literal.supplier_product.original_data_json = (
            identity_literal.original_data
        )
        conflicting = self._product(
            external_id="literal-brand-source-conflict",
            category=source_category,
        )
        original = json.loads(conflicting.original_data)
        original["brand"] = "WET"
        original["characteristics"]["Бренд"] = "Other"
        conflicting.original_data = json.dumps(original, ensure_ascii=False)
        conflicting.supplier_product.original_data_json = conflicting.original_data
        db.session.commit()

        identity_draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=identity_literal.id,
            observed_mapping_preflight=False,
        )
        identity_brand = next(
            item
            for item in json.loads(identity_draft.attributes_json)
            if item["attribute_id"] == "85"
        )
        self.assertEqual(identity_brand["values"][0]["value"], "WET")

        conflicting_draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=conflicting.id,
            observed_mapping_preflight=False,
        )
        self.assertNotIn(
            "85",
            {
                item["attribute_id"]
                for item in json.loads(conflicting_draft.attributes_json)
            },
        )

        brand_attribute.restriction_value_ids_json = json.dumps(["85-2"])
        restricted = self._product(
            external_id="literal-brand-restricted",
            category=source_category,
        )
        original = json.loads(restricted.original_data)
        original["brand"] = "wet"
        original["characteristics"]["Бренд"] = "wet"
        restricted.original_data = json.dumps(original, ensure_ascii=False)
        restricted.supplier_product.original_data_json = restricted.original_data
        db.session.commit()
        restricted_draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=restricted.id,
            observed_mapping_preflight=False,
        )
        restricted_brand = next(
            item
            for item in json.loads(restricted_draft.attributes_json)
            if item["attribute_id"] == "85"
        )
        self.assertEqual(restricted_brand["values"], [{
            "dictionary_value_id": "85-2",
            "value": "Wet",
        }])

    def test_reviewed_brand_aliases_still_require_exact_official_value(self):
        product_type = self._official_type(
            "Лубрикант",
            MarketplaceDraftService.EXPLICIT_PERSONAL_HYGIENE_PATH,
        )
        canonical_values = list(dict.fromkeys(
            MarketplaceDraftService.OBSERVED_BRAND_CANONICAL_ALIASES.values()
        ))
        self._dictionary_attribute(
            "85",
            "Бренд",
            canonical_values,
            is_required=True,
            product_type=product_type,
        )
        product_type.attributes_count = 1
        product_type.required_attributes_count = 1
        source_category = "Смазки, косметика > Вагинальные смазки"

        for index, (source_brand, canonical) in enumerate(
            MarketplaceDraftService.OBSERVED_BRAND_CANONICAL_ALIASES.items(),
            start=1,
        ):
            product = self._product(
                external_id=f"reviewed-brand-{index}",
                category=source_category,
            )
            original = json.loads(product.original_data)
            original["brand"] = source_brand.upper()
            original["characteristics"]["Бренд"] = source_brand.upper()
            product.original_data = json.dumps(original, ensure_ascii=False)
            product.supplier_product.original_data_json = product.original_data
            db.session.commit()

            draft = MarketplaceDraftService.create_draft(
                seller_id=self.seller1_id,
                account_id=self.account1.id,
                imported_product_id=product.id,
                observed_mapping_preflight=False,
            )
            brand = next(
                item
                for item in json.loads(draft.attributes_json)
                if item["attribute_id"] == "85"
            )
            self.assertEqual(brand["values"][0]["value"], canonical)

        near_match = self._product(
            external_id="reviewed-brand-near-match",
            category=source_category,
        )
        original = json.loads(near_match.original_data)
        original["brand"] = "BIORITM LAB"
        original["characteristics"]["Бренд"] = "BIORITM LAB"
        near_match.original_data = json.dumps(original, ensure_ascii=False)
        near_match.supplier_product.original_data_json = near_match.original_data
        db.session.commit()
        near_draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=near_match.id,
            observed_mapping_preflight=False,
        )
        self.assertNotIn(
            "85",
            {
                item["attribute_id"]
                for item in json.loads(near_draft.attributes_json)
            },
        )

    def test_pasties_use_the_official_universal_size_not_source_diameter(self):
        product_type = self._erotic_clothing_type("Пэстисы")
        self._dictionary_attribute(
            "4295",
            "Российский размер",
            ["Универсальный", "42"],
            is_required=True,
            product_type=product_type,
        )
        product_type.attributes_count = 1
        product_type.required_attributes_count = 1
        source_category = (
            "Аксессуары, украшения для тела > Стикини, пестис"
        )
        product = self._product(
            external_id="pasties-universal-size",
            category=source_category,
            dimensions=False,
        )
        original = json.loads(product.original_data)
        original["sizes_raw"] = "диаметр 5,5 см"
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            observed_mapping_preflight=False,
        )
        attributes = {
            item["attribute_id"]: item["values"]
            for item in json.loads(draft.attributes_json)
        }

        self.assertEqual(draft.product_type_id, product_type.id)
        self.assertEqual(attributes["4295"], [{
            "dictionary_value_id": "4295-1",
            "value": "Универсальный",
        }])

    def test_explicit_erotic_lingerie_mapping_fills_every_grounded_field(self):
        product_type = self._erotic_clothing_type()

        def plain(external_id, name, *, data_type="String", required=False):
            row = MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id,
                product_type_id=product_type.id,
                external_attribute_id=external_id,
                name=name,
                data_type=data_type,
                is_required=required,
                max_value_count=1,
                is_collection=False,
                is_available=True,
                is_enabled=True,
                last_seen_at=self.now,
            )
            db.session.add(row)
            return row

        plain("31", "Бренд в одежде и обуви", required=True)
        plain("23536", "Нужен код маркировки", data_type="Boolean", required=True)
        plain("8292", "Объединить на одной карточке", required=True)
        self._dictionary_attribute(
            "22232",
            "ТН ВЭД коды ЕАЭС",
            ["6108210000 - Трусы женские"],
            is_required=True,
            product_type=product_type,
        )
        self._dictionary_attribute(
            "10096",
            "Цвет товара",
            ["Черный"],
            is_collection=True,
            is_required=True,
            product_type=product_type,
        )
        self._dictionary_attribute(
            "4295",
            "Российский размер",
            ["42", "44", "46", "48"],
            is_collection=True,
            max_value_count=0,
            is_required=True,
            product_type=product_type,
        )
        self._dictionary_attribute(
            "8229",
            "Тип",
            ["Эротическое белье"],
            is_required=True,
            product_type=product_type,
        )
        self._dictionary_attribute(
            "9163",
            "Пол",
            ["Женский", "Мужской"],
            is_collection=True,
            product_type=product_type,
        )
        self._dictionary_attribute(
            "4496",
            "Материал",
            ["нейлон", "спандекс"],
            is_collection=True,
            max_value_count=4,
            product_type=product_type,
        )
        self._dictionary_attribute(
            "32",
            "Страна производства",
            ["Россия"],
            product_type=product_type,
        )
        plain("9533", "Размер производителя")
        plain("4604", "Состав материала")
        plain("9070", "Признак 18+", data_type="Boolean")
        plain("23171", "#Хештеги")
        product_type.attributes_count = 14
        product_type.required_attributes_count = 7

        source_category = (
            "Эротическое белье для женщин > Комбинезоны"
        )
        product = self._product(
            external_id="explicit-erotic-lingerie",
            category=source_category,
            dimensions=False,
        )
        original = json.loads(product.original_data)
        original.update({
            "all_categories": [source_category],
            "colors": ["чёрный"],
            "gender": "для женщин",
            "materials": ["94% нейлон", "6% спандекс"],
            "sizes_raw": "универсальный (42-48)",
        })
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.supplier_product.original_data_json = product.original_data

        # A historical exact-listing consensus is not seller review and may
        # be superseded by the narrower literal source-taxonomy recipe.
        wrong_mapping = MarketplaceCategoryMapping(
            seller_id=self.seller1_id,
            marketplace_id=self.marketplace.id,
            supplier_id=self.supplier.id,
            product_type_id=self.product_type.id,
            scope_key=f"supplier:{self.supplier.id}",
            source_type="synthetic",
            source_category=source_category,
            source_category_normalized=(
                MarketplaceDraftService._normalized_text(source_category)
            ),
            external_category_id=self.category.external_category_id,
            external_type_id=self.product_type.external_type_id,
            mapping_source="deterministic",
            mapping_status="active",
            confidence=0.99,
            evidence_json=json.dumps({
                "algorithm": MarketplaceDraftService.OBSERVED_MAPPING_ALGORITHM,
                "state": "active",
            }),
        )
        db.session.add(wrong_mapping)
        db.session.commit()

        # Bulk preparation disables the expensive observed-listing scan; the
        # O(1) explicit recipe must still run on that path.
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            observed_mapping_preflight=False,
        )

        self.assertEqual(draft.product_type_id, product_type.id)
        mapping = db.session.get(
            MarketplaceCategoryMapping,
            draft.category_mapping_id,
        )
        self.assertEqual(mapping.id, wrong_mapping.id)
        self.assertEqual(mapping.mapping_status, "active")
        self.assertEqual(mapping.confidence, 0.995)
        evidence = json.loads(mapping.evidence_json)
        self.assertEqual(
            evidence["algorithm"],
            MarketplaceDraftService.EXPLICIT_SOURCE_MAPPING_ALGORITHM,
        )
        self.assertEqual(
            evidence["rule_id"],
            "explicit_womens_erotic_lingerie_leaf_v1",
        )

        attributes = {
            item["attribute_id"]: item["values"]
            for item in json.loads(draft.attributes_json)
        }
        self.assertEqual(
            {"31", "8292", "10096", "4295", "8229"} - set(attributes),
            set(),
        )
        self.assertNotIn("22232", attributes)
        self.assertNotIn("23536", attributes)
        self.assertEqual(
            {"32", "9163", "4496", "9533", "4604", "9070", "23171"}
            - set(attributes),
            set(),
        )
        self.assertEqual(
            [value["value"] for value in attributes["4295"]],
            ["42", "44", "46", "48"],
        )
        self.assertEqual(attributes["8229"][0]["value"], "Эротическое белье")

        MarketplaceDraftService.reconcile_observed_category_mappings(
            seller_id=self.seller1_id,
            marketplace_id=self.marketplace.id,
        )
        db.session.refresh(mapping)
        self.assertEqual(mapping.mapping_status, "active")
        self.assertEqual(
            json.loads(mapping.evidence_json)["algorithm"],
            MarketplaceDraftService.EXPLICIT_SOURCE_MAPPING_ALGORITHM,
        )

    def test_explicit_source_taxonomy_never_overwrites_seller_mapping(self):
        self._erotic_clothing_type()
        source_category = (
            "Эротическое белье для женщин > Трусики, стринги, шортики"
        )
        first = self._product(
            external_id="manual-erotic-category-first",
            category=source_category,
        )
        manual_draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=first.id,
            product_type_id=self.product_type.id,
            save_mapping=True,
        )
        manual_mapping = db.session.get(
            MarketplaceCategoryMapping,
            manual_draft.category_mapping_id,
        )

        second = self._product(
            external_id="manual-erotic-category-second",
            category=source_category,
        )
        inherited = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=second.id,
        )

        db.session.refresh(manual_mapping)
        self.assertEqual(inherited.product_type_id, self.product_type.id)
        self.assertEqual(inherited.category_mapping_id, manual_mapping.id)
        self.assertEqual(manual_mapping.mapping_source, "manual")
        self.assertEqual(
            json.loads(manual_mapping.evidence_json)["confirmation"],
            "seller",
        )

    def test_explicit_source_taxonomy_honors_a_rejected_mapping(self):
        self._erotic_clothing_type()
        source_category = "Эротическое белье для женщин > Комплекты"
        product = self._product(
            external_id="rejected-erotic-category",
            category=source_category,
        )
        rejected = MarketplaceCategoryMapping(
            seller_id=self.seller1_id,
            marketplace_id=self.marketplace.id,
            supplier_id=self.supplier.id,
            product_type_id=self.product_type.id,
            scope_key=f"supplier:{self.supplier.id}",
            source_type="synthetic",
            source_category=source_category,
            source_category_normalized=(
                MarketplaceDraftService._normalized_text(source_category)
            ),
            external_category_id=self.category.external_category_id,
            external_type_id=self.product_type.external_type_id,
            mapping_source="deterministic",
            mapping_status="rejected",
            confidence=0.99,
            evidence_json="{}",
        )
        db.session.add(rejected)
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
        )

        db.session.refresh(rejected)
        self.assertIsNone(draft.product_type_id)
        self.assertEqual(draft.status, "needs_category")
        self.assertEqual(rejected.mapping_status, "rejected")
        self.assertEqual(rejected.product_type_id, self.product_type.id)

    def test_explicit_source_taxonomy_stales_wrong_auto_type_until_schema_exists(
        self,
    ):
        source_category = (
            "Эротическое белье для женщин > Игровые костюмы"
        )
        product = self._product(
            external_id="missing-roleplay-schema",
            category=source_category,
        )
        automatic = MarketplaceCategoryMapping(
            seller_id=self.seller1_id,
            marketplace_id=self.marketplace.id,
            supplier_id=self.supplier.id,
            product_type_id=self.product_type.id,
            scope_key=f"supplier:{self.supplier.id}",
            source_type="synthetic",
            source_category=source_category,
            source_category_normalized=(
                MarketplaceDraftService._normalized_text(source_category)
            ),
            external_category_id=self.category.external_category_id,
            external_type_id=self.product_type.external_type_id,
            mapping_source="deterministic",
            mapping_status="active",
            confidence=0.99,
            evidence_json=json.dumps({
                "algorithm": MarketplaceDraftService.OBSERVED_MAPPING_ALGORITHM,
                "state": "active",
            }),
        )
        db.session.add(automatic)
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            observed_mapping_preflight=False,
        )

        db.session.refresh(automatic)
        self.assertIsNone(draft.product_type_id)
        self.assertEqual(automatic.mapping_status, "stale")
        evidence = json.loads(automatic.evidence_json)
        self.assertEqual(
            evidence["algorithm"],
            MarketplaceDraftService.EXPLICIT_SOURCE_MAPPING_ALGORITHM,
        )
        self.assertEqual(
            evidence["reason"],
            "official_target_unavailable_or_stale",
        )

    def test_exact_linked_consensus_removes_repeated_category_choice(self):
        first = self._product(
            external_id="observed-consensus-first",
            category="Точная категория поставщика",
        )
        second = self._product(
            external_id="observed-consensus-second",
            category="Точная категория поставщика",
        )
        self._linked_listing(first, product_type=self.product_type)
        self._linked_listing(second, product_type=self.product_type)
        target = self._product(
            external_id="observed-consensus-target",
            category="Точная категория поставщика",
        )

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=target.id,
        )

        self.assertEqual(draft.product_type_id, self.product_type.id)
        mapping = db.session.get(
            MarketplaceCategoryMapping,
            draft.category_mapping_id,
        )
        self.assertEqual(mapping.mapping_source, "deterministic")
        self.assertEqual(mapping.mapping_status, "active")
        self.assertEqual(mapping.confidence, 0.99)
        evidence = json.loads(mapping.evidence_json)
        self.assertEqual(
            evidence["algorithm"],
            MarketplaceDraftService.OBSERVED_MAPPING_ALGORITHM,
        )
        self.assertEqual(evidence["state"], "active")
        self.assertEqual(evidence["observations"], 2)
        self.assertEqual(evidence["exact_link_sources"], [
            "exact_offer_identity",
            "exact_source_identity",
        ])
        readiness = MarketplaceDraftService.mapping_readiness(
            seller_id=self.seller1_id,
            draft_id=draft.id,
        )
        self.assertEqual(
            readiness["category"]["mapping_origin"],
            "deterministic",
        )

    def test_automatic_mapping_guard_rejects_literal_taxonomy_conflicts(self):
        adult_category = MarketplaceTaxonomyCategory(
            marketplace_id=self.marketplace.id,
            external_category_id="adult-category",
            name="Секс игрушки",
            full_path="Товары для взрослых / Секс игрушки",
            depth=1,
            is_available=True,
            last_seen_at=self.now,
        )
        db.session.add(adult_category)
        db.session.flush()

        def product_type(external_id, name):
            row = MarketplaceProductType(
                marketplace_id=self.marketplace.id,
                category_id=adult_category.id,
                external_type_id=external_id,
                name=name,
                is_available=True,
                is_enabled=True,
                attributes_synced_at=self.now,
                attributes_sync_status="success",
                attributes_schema_hash=f"schema-{external_id}",
                attributes_version=1,
                attributes_count=0,
                required_attributes_count=0,
            )
            db.session.add(row)
            db.session.flush()
            return row

        strapon = product_type("adult-strapon", "Страпон")
        erotic_set = product_type("adult-set", "Эротический набор")
        vaginal_balls = product_type(
            "adult-vaginal-balls",
            "Вагинальные шарики",
        )
        anal_plug = product_type("adult-plug", "Анальная пробка")
        extension = product_type(
            "adult-extension",
            "Насадки, удлинители эротические",
        )
        db.session.commit()

        conflict_cases = (
            (
                "Эротическое белье для женщин > Комплекты",
                strapon,
            ),
            ("Тампоны, чаши > Менструальные чаши", erotic_set),
            ("Шарики > Анальные", vaginal_balls),
            ("Анальные крюки", anal_plug),
            ("Вакуумные помпы > Насадки на помпу", extension),
        )
        for source_category, target_type in conflict_cases:
            with self.subTest(
                source_category=source_category,
                target_type=target_type.name,
            ):
                self.assertEqual(
                    MarketplaceDraftService
                    ._automatic_mapping_conflict_reason(
                        identity={"source_category": source_category},
                        product_type=target_type,
                    ),
                    "explicit_source_category_conflict",
                )

        self.assertIsNone(
            MarketplaceDraftService._automatic_mapping_conflict_reason(
                identity={
                    "source_category": (
                        "Эротическое белье для женщин > Комплекты"
                    ),
                },
                product_type=self.product_type,
            )
        )
        self.assertIsNone(
            MarketplaceDraftService._automatic_mapping_conflict_reason(
                identity={
                    "source_category": (
                        "Страпоны и фаллопротезы > Трусики и насадки"
                    ),
                },
                product_type=strapon,
            )
        )

    def test_observed_clothing_consensus_stales_non_clothing_mapping(self):
        adult_category = MarketplaceTaxonomyCategory(
            marketplace_id=self.marketplace.id,
            external_category_id="adult-clothing-conflict",
            name="Секс игрушки",
            full_path="Товары для взрослых / Секс игрушки",
            depth=1,
            is_available=True,
            last_seen_at=self.now,
        )
        db.session.add(adult_category)
        db.session.flush()
        wrong_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=adult_category.id,
            external_type_id="wrong-clothing-strapon",
            name="Страпон",
            is_available=True,
            is_enabled=True,
            attributes_synced_at=self.now,
            attributes_sync_status="success",
            attributes_schema_hash="wrong-clothing-schema",
            attributes_version=1,
            attributes_count=0,
            required_attributes_count=0,
        )
        db.session.add(wrong_type)
        db.session.flush()
        source_category = (
            "БДСМ товары и фетиш > Одежда и белье для женщин"
        )
        mapping = MarketplaceCategoryMapping(
            seller_id=self.seller1_id,
            marketplace_id=self.marketplace.id,
            supplier_id=self.supplier.id,
            product_type_id=wrong_type.id,
            scope_key=f"supplier:{self.supplier.id}",
            source_type="synthetic",
            source_category=source_category,
            source_category_normalized=(
                MarketplaceDraftService._normalized_text(source_category)
            ),
            external_category_id=adult_category.external_category_id,
            external_type_id=wrong_type.external_type_id,
            mapping_source="deterministic",
            mapping_status="active",
            confidence=0.99,
            evidence_json="{}",
        )
        db.session.add(mapping)
        db.session.commit()
        for suffix in ("first", "second"):
            product = self._product(
                external_id=f"clothing-conflict-{suffix}",
                category=source_category,
            )
            self._linked_listing(product, product_type=wrong_type)

        result = (
            MarketplaceDraftService.reconcile_observed_category_mappings(
                seller_id=self.seller1_id,
                marketplace_id=self.marketplace.id,
            )
        )
        db.session.refresh(mapping)

        self.assertTrue(result["success"])
        self.assertEqual(result["safe_groups"], 0)
        self.assertEqual(result["unsafe_groups"], 1)
        self.assertEqual(result["staled"], 1)
        self.assertEqual(mapping.mapping_status, "stale")
        evidence = json.loads(mapping.evidence_json)
        self.assertEqual(
            evidence["algorithm"],
            MarketplaceDraftService.OBSERVED_MAPPING_ALGORITHM,
        )
        self.assertEqual(
            evidence["reason"],
            "explicit_source_category_conflict",
        )

        target = self._product(
            external_id="clothing-conflict-target",
            category=source_category,
        )
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=target.id,
        )
        self.assertIsNone(draft.product_type_id)
        self.assertEqual(draft.status, "needs_category")

    def test_one_product_in_two_accounts_is_not_category_consensus(self):
        second_account = self._account(
            self.seller1_id,
            "client-one-second-account",
        )
        db.session.commit()
        product = self._product(
            external_id="observed-copy-in-two-accounts",
            category="Категория одной canonical карточки",
        )
        self._linked_listing(
            product,
            product_type=self.product_type,
            account=self.account1,
        )
        self._linked_listing(
            product,
            product_type=self.product_type,
            account=second_account,
        )

        result = (
            MarketplaceDraftService.reconcile_observed_category_mappings(
                seller_id=self.seller1_id,
                marketplace_id=self.marketplace.id,
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["safe_groups"], 0)
        self.assertEqual(result["unsafe_groups"], 1)
        self.assertIsNone(
            MarketplaceCategoryMapping.query.filter_by(
                seller_id=self.seller1_id,
                source_category_normalized=(
                    "категория одной canonical карточки"
                ),
            ).first()
        )

    def test_exact_linked_consensus_can_prove_vendor_code_model_recipe(self):
        model_attribute = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id=(
                MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID
            ),
            name="Название модели (для объединения в одну карточку)",
            data_type="String",
            is_required=True,
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
        )
        db.session.add(model_attribute)
        db.session.commit()
        for suffix in ("first", "second"):
            product = self._product(
                external_id=f"observed-model-{suffix}",
                category="Категория с точной моделью",
            )
            listing = self._linked_listing(
                product,
                product_type=self.product_type,
            )
            listing.attributes_synced_at = self.now
            listing.attributes_json = json.dumps([{
                "id": (
                    MarketplaceDraftService
                    .OZON_MODEL_NAME_ATTRIBUTE_ID
                ),
                "complex_id": None,
                "values": [{
                    "dictionary_value_id": None,
                    "value": product.external_vendor_code,
                }],
            }])
            db.session.commit()
        target = self._product(
            external_id="observed-model-target",
            category="Категория с точной моделью",
        )

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=target.id,
        )

        mapping = db.session.get(
            MarketplaceCategoryMapping,
            draft.category_mapping_id,
        )
        evidence = json.loads(mapping.evidence_json)
        self.assertEqual(evidence["attribute_recipes"], [{
            "attribute_id": (
                MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID
            ),
            "recipe": MarketplaceDraftService.OBSERVED_MODEL_RECIPE,
            "observations": 2,
            "distinct_products": 2,
            "evidence_fingerprint": (
                evidence["attribute_recipes"][0][
                    "evidence_fingerprint"
                ]
            ),
        }])
        self.assertRegex(
            evidence["attribute_recipes"][0]["evidence_fingerprint"],
            r"^[0-9a-f]{64}$",
        )
        attributes = json.loads(draft.attributes_json)
        model = next(
            item
            for item in attributes
            if item["attribute_id"]
            == MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID
        )
        self.assertEqual(model["values"], [{
            "value": target.external_vendor_code,
        }])
        self.assertNotIn(
            target.external_vendor_code,
            mapping.evidence_json,
        )

    def test_model_recipe_fails_closed_on_one_mismatching_listing(self):
        model_attribute = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id=(
                MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID
            ),
            name="Название модели (для объединения в одну карточку)",
            data_type="String",
            is_required=True,
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            last_seen_at=self.now,
        )
        db.session.add(model_attribute)
        db.session.commit()
        for index, suffix in enumerate(("first", "second")):
            product = self._product(
                external_id=f"mismatching-model-{suffix}",
                category="Категория с разными моделями",
            )
            listing = self._linked_listing(
                product,
                product_type=self.product_type,
            )
            listing.attributes_synced_at = self.now
            listing.attributes_json = json.dumps([{
                "id": (
                    MarketplaceDraftService
                    .OZON_MODEL_NAME_ATTRIBUTE_ID
                ),
                "complex_id": None,
                "values": [{
                    "value": (
                        product.external_vendor_code
                        if index == 0 else "другая модель"
                    ),
                }],
            }])
            db.session.commit()
        target = self._product(
            external_id="mismatching-model-target",
            category="Категория с разными моделями",
        )

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=target.id,
        )

        self.assertEqual(draft.product_type_id, self.product_type.id)
        mapping = db.session.get(
            MarketplaceCategoryMapping,
            draft.category_mapping_id,
        )
        self.assertNotIn(
            "attribute_recipes",
            json.loads(mapping.evidence_json),
        )
        self.assertNotIn(
            MarketplaceDraftService.OZON_MODEL_NAME_ATTRIBUTE_ID,
            {
                item["attribute_id"]
                for item in json.loads(draft.attributes_json)
            },
        )

    def test_observed_mapping_fails_closed_for_weak_or_conflicting_evidence(self):
        alternative_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.category.id,
            external_type_id="778",
            name="Другой точный тип",
            is_available=True,
            is_enabled=True,
            attributes_synced_at=self.now,
            attributes_sync_status="success",
            attributes_schema_hash="alternative-schema",
            attributes_version=1,
            attributes_count=0,
            required_attributes_count=0,
        )
        db.session.add(alternative_type)
        db.session.commit()

        single = self._product(
            external_id="observed-single",
            category="Один пример",
        )
        self._linked_listing(single, product_type=self.product_type)

        conflict_first = self._product(
            external_id="observed-conflict-first",
            category="Конфликтная категория",
        )
        conflict_second = self._product(
            external_id="observed-conflict-second",
            category="Конфликтная категория",
        )
        self._linked_listing(
            conflict_first,
            product_type=self.product_type,
        )
        self._linked_listing(
            conflict_second,
            product_type=alternative_type,
        )

        unknown_first = self._product(
            external_id="observed-unknown-first",
            category="Неполная категория",
        )
        unknown_second = self._product(
            external_id="observed-unknown-second",
            category="Неполная категория",
        )
        unknown_third = self._product(
            external_id="observed-unknown-third",
            category="Неполная категория",
        )
        self._linked_listing(
            unknown_first,
            product_type=self.product_type,
        )
        self._linked_listing(
            unknown_second,
            product_type=self.product_type,
        )
        self._linked_listing(unknown_third, product_type=None)

        non_exact_first = self._product(
            external_id="observed-nonexact-first",
            category="Неподтверждённая связь",
        )
        non_exact_second = self._product(
            external_id="observed-nonexact-second",
            category="Неподтверждённая связь",
        )
        self._linked_listing(
            non_exact_first,
            product_type=self.product_type,
            link_source="seller_confirmation",
        )
        self._linked_listing(
            non_exact_second,
            product_type=self.product_type,
            link_source="seller_confirmation",
        )

        for index, category in enumerate((
            "Один пример",
            "Конфликтная категория",
            "Неполная категория",
            "Неподтверждённая связь",
        )):
            with self.subTest(category=category):
                target = self._product(
                    external_id=f"observed-blocked-{index}",
                    category=category,
                )
                draft = MarketplaceDraftService.create_draft(
                    seller_id=self.seller1_id,
                    account_id=self.account1.id,
                    imported_product_id=target.id,
                )
                self.assertIsNone(draft.product_type_id)
                self.assertEqual(draft.status, "needs_category")

        self.assertEqual(
            MarketplaceCategoryMapping.query.filter_by(
                seller_id=self.seller1_id,
                mapping_source="deterministic",
                mapping_status="active",
            ).count(),
            0,
        )

    def test_observed_mapping_blocks_structurally_heterogeneous_source_bucket(self):
        first = self._product(
            external_id="heterogeneous-clothing-first",
            category="Широкая одежда и белье",
        )
        second = self._product(
            external_id="heterogeneous-clothing-second",
            category="Широкая одежда и белье",
        )
        # These legacy structured subtype hints are negative-only evidence:
        # neither one is allowed to select the Ozon type, but disagreement
        # proves that one category-wide mapping would be unsafe.
        first.wb_subject_id = 2607
        second.wb_subject_id = 5071
        db.session.commit()
        self._linked_listing(first, product_type=self.product_type)
        self._linked_listing(second, product_type=self.product_type)

        result = (
            MarketplaceDraftService.reconcile_observed_category_mappings(
                seller_id=self.seller1_id,
                marketplace_id=self.marketplace.id,
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["safe_groups"], 0)
        self.assertEqual(result["unsafe_groups"], 1)
        self.assertIsNone(
            MarketplaceCategoryMapping.query.filter_by(
                seller_id=self.seller1_id,
                source_category_normalized="широкая одежда и белье",
            ).first()
        )

    def test_manual_category_mapping_is_never_replaced_by_observed_consensus(self):
        manual_source = self._product(
            external_id="manual-category-source",
            category="Ручная категория",
        )
        manual_draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=manual_source.id,
            product_type_id=self.product_type.id,
            save_mapping=True,
        )
        manual_mapping = db.session.get(
            MarketplaceCategoryMapping,
            manual_draft.category_mapping_id,
        )

        alternative_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.category.id,
            external_type_id="779",
            name="Наблюдаемый другой тип",
            is_available=True,
            is_enabled=True,
            attributes_synced_at=self.now,
            attributes_sync_status="success",
            attributes_schema_hash="observed-other-schema",
            attributes_version=1,
            attributes_count=0,
            required_attributes_count=0,
        )
        db.session.add(alternative_type)
        db.session.commit()
        for suffix in ("first", "second"):
            product = self._product(
                external_id=f"manual-protected-{suffix}",
                category="Ручная категория",
            )
            self._linked_listing(
                product,
                product_type=alternative_type,
            )

        result = (
            MarketplaceDraftService.reconcile_observed_category_mappings(
                seller_id=self.seller1_id,
                marketplace_id=self.marketplace.id,
            )
        )
        db.session.refresh(manual_mapping)

        self.assertTrue(result["success"])
        self.assertEqual(result["protected"], 1)
        self.assertEqual(manual_mapping.mapping_source, "manual")
        self.assertEqual(manual_mapping.mapping_status, "active")
        self.assertEqual(
            manual_mapping.product_type_id,
            self.product_type.id,
        )

    def test_new_exact_conflict_stales_an_automatic_mapping(self):
        for suffix in ("first", "second"):
            product = self._product(
                external_id=f"stale-observed-{suffix}",
                category="Поздний конфликт",
            )
            self._linked_listing(
                product,
                product_type=self.product_type,
            )
        first_result = (
            MarketplaceDraftService.reconcile_observed_category_mappings(
                seller_id=self.seller1_id,
                marketplace_id=self.marketplace.id,
            )
        )
        mapping = MarketplaceCategoryMapping.query.filter_by(
            seller_id=self.seller1_id,
            mapping_source="deterministic",
            source_category_normalized="поздний конфликт",
        ).one()
        self.assertEqual(first_result["created"], 1)
        self.assertEqual(mapping.mapping_status, "active")

        alternative_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.category.id,
            external_type_id="780",
            name="Конфликт после консенсуса",
            is_available=True,
            is_enabled=True,
            attributes_synced_at=self.now,
            attributes_sync_status="success",
            attributes_schema_hash="late-conflict-schema",
            attributes_version=1,
            attributes_count=0,
            required_attributes_count=0,
        )
        db.session.add(alternative_type)
        db.session.commit()
        conflict = self._product(
            external_id="stale-observed-conflict",
            category="Поздний конфликт",
        )
        self._linked_listing(
            conflict,
            product_type=alternative_type,
        )

        second_result = (
            MarketplaceDraftService.reconcile_observed_category_mappings(
                seller_id=self.seller1_id,
                marketplace_id=self.marketplace.id,
            )
        )
        db.session.refresh(mapping)
        self.assertEqual(second_result["staled"], 1)
        self.assertEqual(mapping.mapping_status, "stale")
        self.assertEqual(
            json.loads(mapping.evidence_json)["reason"],
            "conflicting_product_types",
        )

        target = self._product(
            external_id="stale-observed-target",
            category="Поздний конфликт",
        )
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=target.id,
        )
        self.assertIsNone(draft.product_type_id)

    def test_observed_mapping_scan_never_uses_a_truncated_evidence_set(self):
        for suffix in ("first", "second"):
            product = self._product(
                external_id=f"bounded-observed-{suffix}",
                category="Ограниченная категория",
            )
            self._linked_listing(
                product,
                product_type=self.product_type,
            )

        with patch.object(
            MarketplaceDraftService,
            "OBSERVED_MAPPING_MAX_LISTINGS",
            1,
        ):
            result = (
                MarketplaceDraftService
                .reconcile_observed_category_mappings(
                    seller_id=self.seller1_id,
                    marketplace_id=self.marketplace.id,
                )
            )

        self.assertFalse(result["success"])
        self.assertEqual(
            result["code"],
            "observed_category_mapping_scan_truncated",
        )
        self.assertEqual(
            MarketplaceCategoryMapping.query.filter_by(
                seller_id=self.seller1_id,
                mapping_source="deterministic",
            ).count(),
            0,
        )

    def test_completeness_counts_only_nonempty_current_schema_values(self):
        _, draft = self._ready_draft(
            external_id="strict-completeness",
        )
        disabled_required = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="99",
            name="Обязательное поле без разрешения",
            data_type="String",
            is_required=True,
            max_value_count=1,
            is_available=True,
            is_enabled=False,
            last_seen_at=self.now,
        )
        db.session.add(disabled_required)
        attributes = json.loads(draft.attributes_json)
        next(
            item for item in attributes
            if item["attribute_id"] == "31"
        )["values"] = []
        draft.attributes_json = json.dumps(
            attributes,
            ensure_ascii=False,
        )
        draft.media_json = json.dumps({
            "images": [
                "",
                "not-a-public-url",
                "https://img.test/valid.jpg",
                "https://img.test/valid.jpg",
            ],
        })
        draft.barcodes_json = json.dumps([
            "",
            "4600000000001",
            "4600000000001",
        ])
        draft.validation_result_json = json.dumps({
            "publishable": False,
            "errors": [{"code": "synthetic_invalid"}],
            "warnings": [],
        })
        db.session.commit()

        completeness = MarketplaceDraftService.completeness_summary(
            draft,
        )
        self.assertEqual(completeness["required_total"], 4)
        self.assertEqual(completeness["required_supplied"], 2)
        self.assertEqual(completeness["schema_total"], 4)
        self.assertEqual(completeness["supplied_known"], 2)
        self.assertEqual(completeness["image_count"], 1)
        self.assertEqual(completeness["barcode_count"], 1)
        self.assertFalse(completeness["required_complete"])
        self.assertFalse(completeness["publishable"])

    def test_ozon_draft_selects_first_valid_source_barcode(self):
        product = self._product(external_id="multiple-barcodes")
        observed = json.loads(product.original_data)
        observed["barcodes"] = [
            "4600000000011",
            "4600000000028",
        ]
        product.original_data = json.dumps(observed, ensure_ascii=False)
        product.barcodes = json.dumps(observed["barcodes"])
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=self.product_type.id,
        )
        detail = draft.to_public_dict(detail=True)
        source_facts = json.loads(draft.source_facts_json)

        self.assertEqual(detail["barcodes"], ["4600000000011"])
        self.assertEqual(
            source_facts["facts"]["identifiers"]["barcodes"],
            ["4600000000011", "4600000000028"],
        )

    def test_forbidden_ozon_brand_blocks_full_card_validation(self):
        product, draft = self._ready_draft(
            external_id="forbidden-brand",
        )
        product.brand = "  sVaKoM  "
        db.session.commit()

        validated = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        errors = validated.to_public_dict(
            detail=True,
        )["validation"]["errors"]
        blocked = [
            item for item in errors
            if item["code"] == "ozon_brand_forbidden"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["field"], "brand")
        self.assertIn("SVAKOM", blocked[0]["message"])
        completeness = MarketplaceDraftService.completeness_summary(
            validated,
        )
        self.assertFalse(completeness["brand_allowed"])
        self.assertEqual(completeness["forbidden_brand"], "SVAKOM")
        self.assertFalse(completeness["publishable"])

    def test_forbidden_seller_edited_brand_attribute_is_blocked(self):
        _, draft = self._ready_draft(
            external_id="forbidden-brand-attribute",
        )
        attributes = json.loads(draft.attributes_json)
        next(
            item for item in attributes
            if item["attribute_id"] == "31"
        )["values"] = [{"value": "my size"}]
        draft.attributes_json = json.dumps(
            attributes,
            ensure_ascii=False,
        )
        db.session.commit()

        validated = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        self.assertIn(
            "ozon_brand_forbidden",
            {
                item["code"]
                for item in validated.to_public_dict(
                    detail=True,
                )["validation"]["errors"]
            },
        )
        self.assertEqual(
            MarketplaceDraftService.completeness_summary(
                validated,
            )["forbidden_brand"],
            "MY.SIZE",
        )

    def test_retry_applies_exact_mapping_to_existing_untyped_draft(self):
        product = self._product(external_id="existing-before-mapping")
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
        )
        self.assertIsNone(draft.product_type_id)

        detail = draft.to_public_dict(detail=True)
        edited = MarketplaceDraftService.update_draft(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
            patch={
                "content": {
                    **detail["content"],
                    "name": "Название продавца для Ozon",
                },
                "commercial": {
                    **detail["commercial"],
                    "vat": "0.22",
                    "currency_code": "RUB",
                },
            },
        )
        preserved = {
            field: getattr(edited, field)
            for field in (
                "source_fact_hash",
                "source_facts_json",
                "provenance_json",
                "content_json",
                "media_json",
                "dimensions_json",
                "barcodes_json",
                "commercial_json",
            )
        }
        previous_version = edited.version

        # The canonical source drifts after the draft snapshot.  Category
        # reuse must not absorb this change or overwrite seller edits.
        product.title = "Новое название исходного товара"
        db.session.commit()

        representative = self._product(external_id="mapping-representative")
        confirmed = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=representative.id,
            product_type_id=self.product_type.id,
            save_mapping=True,
            corrected_by_user_id=1,
        )

        reused = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
        )
        self.assertEqual(reused.id, draft.id)
        self.assertEqual(reused.version, previous_version + 1)
        self.assertEqual(reused.product_type_id, self.product_type.id)
        self.assertEqual(
            reused.category_mapping_id,
            confirmed.category_mapping_id,
        )
        self.assertEqual(reused.status, "draft")
        self.assertEqual(reused.validation_status, "stale")
        for field, value in preserved.items():
            self.assertEqual(getattr(reused, field), value)
        self.assertEqual(
            {
                item["attribute_id"]
                for item in reused.to_public_dict(detail=True)["attributes"]
            },
            {"31", "32"},
        )
        self.assertEqual(
            reused.to_public_dict(detail=True)["complex_attributes"],
            [],
        )

        validated = MarketplaceDraftService.validate_draft(
            seller_id=self.seller1_id,
            draft_id=reused.id,
            expected_version=reused.version,
        )
        self.assertIn(
            "source_facts_stale",
            {
                item["code"]
                for item in validated.to_public_dict(detail=True)
                ["validation"]["errors"]
            },
        )

    def test_retry_never_replaces_an_existing_explicit_product_type(self):
        alternative_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.category.id,
            external_type_id="778",
            name="Майка",
            is_available=True,
            is_enabled=True,
            attributes_synced_at=self.now,
            attributes_sync_status="success",
            attributes_schema_hash="alternative-schema",
            attributes_version=1,
            attributes_count=0,
            required_attributes_count=0,
        )
        db.session.add(alternative_type)
        db.session.commit()

        product = self._product(external_id="explicit-before-mapping")
        explicit = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            product_type_id=alternative_type.id,
        )
        previous_version = explicit.version
        previous_attributes = explicit.attributes_json

        representative = self._product(external_id="explicit-mapping-source")
        MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=representative.id,
            product_type_id=self.product_type.id,
            save_mapping=True,
            corrected_by_user_id=1,
        )

        unchanged = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
        )
        self.assertEqual(unchanged.product_type_id, alternative_type.id)
        self.assertIsNone(unchanged.category_mapping_id)
        self.assertEqual(unchanged.attributes_json, previous_attributes)
        self.assertEqual(unchanged.version, previous_version)

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
        self.assertFalse(readiness['category']['explicit_source_taxonomy'])
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

    def test_source_suffix_preflight_uses_existing_ozon_offer_for_update(self):
        product = self._product(external_id="7725")
        product.source_type = "sexoptovik"
        product.external_vendor_code = "id-7725-1366"
        listing = MarketplaceListing(
            seller_id=self.seller1_id,
            marketplace_id=self.marketplace.id,
            account_id=self.account1.id,
            product_type_id=self.product_type.id,
            offer_id="id-7725-1364",
            external_product_id="7725001",
            title="Существующая карточка Ozon 7725",
            normalized_status="active",
            link_status="unlinked",
            sync_fingerprint="d" * 64,
        )
        db.session.add(listing)
        db.session.commit()

        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
        )

        self.assertEqual(listing.imported_product_id, product.id)
        self.assertEqual(draft.published_listing_id, listing.id)
        self.assertEqual(draft.offer_id, "id-7725-1364")
        self.assertEqual(draft.product_type_id, self.product_type.id)

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

    def test_source_rebase_updates_defaults_but_preserves_seller_edits(self):
        product, draft = self._ready_draft(external_id="source-rebase")
        content = json.loads(draft.content_json)
        content["description"] = "Описание продавца"
        dimensions = json.loads(draft.dimensions_json)
        dimensions["width"] = "999"
        attributes = json.loads(draft.attributes_json)
        brand = next(
            item for item in attributes
            if item["attribute_id"] == self.brand_attribute.external_attribute_id
        )
        brand["values"] = [{"value": "Бренд продавца"}]
        draft.content_json = json.dumps(content, ensure_ascii=False)
        draft.dimensions_json = json.dumps(dimensions)
        draft.attributes_json = json.dumps(attributes, ensure_ascii=False)
        draft.complex_attributes_json = json.dumps([{
            "attributes": [{
                "attribute_id": "999",
                "complex_id": "500",
                "values": [{"value": "Группа продавца"}],
            }],
        }], ensure_ascii=False)
        db.session.commit()

        original = json.loads(product.original_data)
        original.update({
            "title": "Название источника 2",
            "description": "Описание источника 2",
            "brand": "Наблюдаемый бренд 2",
            "photo_urls": ["https://img.test/source-rebase-2.jpg"],
            "dimensions": {
                "package_width_cm": 25,
                "package_height_cm": 4,
                "package_length_cm": 35,
                "package_weight_g": 300,
            },
        })
        original["characteristics"]["Бренд"] = "Наблюдаемый бренд 2"
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.title = "Название источника 2"
        product.description = "Описание источника 2"
        product.calculated_price = 1500
        db.session.commit()

        rebased = MarketplaceDraftService.rebase_source_defaults(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )
        detail = rebased.to_public_dict(detail=True)
        self.assertEqual(detail["content"]["name"], "Название источника 2")
        self.assertEqual(detail["content"]["description"], "Описание продавца")
        self.assertEqual(
            detail["media"]["images"],
            ["https://img.test/source-rebase-2.jpg"],
        )
        self.assertEqual(detail["dimensions"], {
            "width": "999",
            "height": "40",
            "depth": "350",
            "dimension_unit": "MILLIMETERS",
            "weight": "300",
            "weight_unit": "GRAMS",
        })
        self.assertEqual(detail["commercial"]["price"], "1500")
        rebased_brand = next(
            item for item in detail["attributes"]
            if item["attribute_id"] == self.brand_attribute.external_attribute_id
        )
        self.assertEqual(
            rebased_brand["values"],
            [{"value": "Бренд продавца"}],
        )
        self.assertEqual(
            detail["complex_attributes"],
            [{
                "attributes": [{
                    "attribute_id": "999",
                    "complex_id": "500",
                    "values": [{"value": "Группа продавца"}],
                }],
            }],
        )
        self.assertEqual(rebased.validation_status, "stale")

    def test_source_rebase_never_reclassifies_compliance_attributes(self):
        """22232/23536 не являются source-derived дефолтами.

        `apply_to_attributes` внутри `_auto_map_attributes` не смотрит на
        `facts_document` вовсе -- только на живое админское решение. Поэтому
        previous_auto и current_auto внутри `rebase_source_defaults` всегда
        несут ОДНО И ТО ЖЕ (самое свежее) значение независимо от того, какой
        снимок фактов им передали, и naive three-way merge не должен решать
        за эти два атрибута "правка продавца или нет".
        """
        product, draft = self._ready_draft(external_id="compliance-rebase")

        module = 'services.ozon_compliance_defaults'
        stale_defaults = {
            'tnved': {
                'code': '1111111111',
                'value': '1111111111 - Значение A',
                'external_value_id': 'value-a',
                'default_id': 1,
                'dictionary_version': 1,
            },
            'marking': True,
            'unresolved': [],
            'evidence': {},
        }
        with patch(f'{module}.resolve_type_defaults', return_value=stale_defaults):
            from services.ozon_compliance_defaults import apply_to_attributes
            attributes, _ = apply_to_attributes(
                json.loads(draft.attributes_json), self.product_type.id,
            )
        draft.attributes_json = json.dumps(attributes, ensure_ascii=False)
        db.session.commit()

        stored_tnved = next(
            item for item in json.loads(draft.attributes_json)
            if item['attribute_id'] == '22232'
        )
        self.assertEqual(
            stored_tnved['values'][0]['value'], '1111111111 - Значение A',
        )

        original = json.loads(product.original_data)
        original['description'] = 'Описание источника изменилось для rebase'
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.description = 'Описание источника изменилось для rebase'
        db.session.commit()

        fresh_defaults = dict(stale_defaults)
        fresh_defaults['tnved'] = {
            'code': '2222222222',
            'value': '2222222222 - Значение B',
            'external_value_id': 'value-b',
            'default_id': 2,
            'dictionary_version': 2,
        }
        with patch(f'{module}.resolve_type_defaults', return_value=fresh_defaults):
            rebased = MarketplaceDraftService.rebase_source_defaults(
                seller_id=self.seller1_id,
                draft_id=draft.id,
                expected_version=draft.version,
            )

        rebased_attributes = json.loads(rebased.attributes_json)
        rebased_tnved = next(
            (item for item in rebased_attributes if item['attribute_id'] == '22232'),
            None,
        )
        self.assertIsNotNone(
            rebased_tnved,
            'Сохранённая compliance-запись не должна пропадать при rebase',
        )
        self.assertEqual(
            rebased_tnved['values'][0]['value'],
            '1111111111 - Значение A',
            'Rebase не имеет права переклассифицировать/заменять сохранённое '
            'compliance-значение по сравнению previous/current auto-defaults',
        )

    def test_source_rebase_does_not_invent_missing_compliance_attribute(self):
        """A draft without a stored 22232/23536 record must stay that way
        across `rebase_source_defaults`, even when an active admin decision
        exists and an unrelated source fact (description) changes and
        triggers the rebase. Before the fix this silently created the
        compliance attribute via the current_auto tail-loop despite no
        seller/admin action targeting this specific draft in this call.
        """
        product, draft = self._ready_draft(external_id="compliance-rebase-missing")
        self.assertNotIn(
            '22232',
            {item['attribute_id'] for item in json.loads(draft.attributes_json)},
        )

        original = json.loads(product.original_data)
        original['description'] = 'Описание источника изменилось для rebase 2'
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.description = 'Описание источника изменилось для rebase 2'
        db.session.commit()

        module = 'services.ozon_compliance_defaults'
        fresh_defaults = {
            'tnved': {
                'code': '3333333333',
                'value': '3333333333 - Значение C',
                'external_value_id': 'value-c',
                'default_id': 3,
                'dictionary_version': 1,
            },
            'marking': True,
            'unresolved': [],
            'evidence': {},
        }
        with patch(f'{module}.resolve_type_defaults', return_value=fresh_defaults):
            rebased = MarketplaceDraftService.rebase_source_defaults(
                seller_id=self.seller1_id,
                draft_id=draft.id,
                expected_version=draft.version,
            )
        rebased_ids = {
            item['attribute_id'] for item in json.loads(rebased.attributes_json)
        }
        self.assertNotIn('22232', rebased_ids)
        self.assertNotIn('23536', rebased_ids)

    def test_compliance_attributes_are_never_generic_source_auto_mapped(self):
        """Only the signed default and active registry may populate 22232/23536.

        Exact source labels and a fresh type-scoped TNVED dictionary are not
        authority for either regulatory field. Ordinary content fields still
        map, and the dedicated admin layer remains able to add both values
        with their existing provenance.
        """
        tnved_attribute = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="22232",
            name="ТН ВЭД коды ЕАЭС",
            data_type="String",
            dictionary_id="tnved-current",
            max_value_count=1,
            is_available=True,
            is_enabled=True,
            values_synced_at=self.now,
            values_sync_status="success",
            values_snapshot_hash="fresh-tnved-values",
            values_version=1,
            values_count=1,
        )
        marking_attribute = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="23536",
            name="Нужен код маркировки",
            data_type="Boolean",
            max_value_count=1,
            is_available=True,
            is_enabled=True,
        )
        db.session.add_all([tnved_attribute, marking_attribute])
        db.session.flush()
        db.session.add(MarketplaceAttributeValue(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            attribute_id=tnved_attribute.id,
            external_value_id="tnved-synthetic-3307900008",
            value="3307900008 - Synthetic test entry",
            value_normalized=OzonReferenceService.normalize_value(
                "3307900008 - Synthetic test entry"
            ),
            is_available=True,
            last_seen_at=self.now,
        ))
        db.session.flush()

        facts_document = {
            "facts": {
                "identity": {"brand": "Наблюдаемый бренд"},
                "attributes": {
                    "country": "Россия",
                    "characteristics": [
                        {
                            "name": "ТН ВЭД коды ЕАЭС",
                            "value": "3307900008 - Synthetic test entry",
                        },
                        {
                            "name": "Нужен код маркировки",
                            "value": True,
                        },
                        {
                            "name": "Аннотация",
                            "value": "Наблюдаемое описание",
                        },
                    ],
                },
            },
        }

        automatic, no_authority_report = (
            MarketplaceDraftService._auto_map_attributes(
                product_type=self.product_type,
                facts_document=facts_document,
            )
        )
        automatic_by_id = {
            item["attribute_id"]: item for item in automatic
        }
        self.assertNotIn("22232", automatic_by_id)
        self.assertNotIn("23536", automatic_by_id)
        self.assertEqual(no_authority_report["applied"], [])
        self.assertCountEqual(
            no_authority_report["unresolved"], ["22232", "23536"],
        )
        self.assertTrue({"31", "32", "4191"}.issubset(automatic_by_id))
        self.assertEqual(
            automatic_by_id["32"]["values"],
            [{"dictionary_value_id": "9001", "value": "Россия"}],
        )

        seller = db.session.get(Seller, self.seller1_id)
        default = OzonComplianceDefault(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            tnved_code="3307900008",
            tnved_display="3307900008 - Synthetic test entry",
            status="active",
            decided_by_user_id=seller.user_id,
            rationale="Synthetic fixture decision for regression coverage",
            dictionary_version=1,
            dictionary_hash="fresh-tnved-values",
        )
        registry = OzonMarkingRegistryVersion(
            label="Synthetic complete registry",
            is_complete=True,
            declared_by_user_id=seller.user_id,
            rule_count=1,
            status="active",
        )
        db.session.add_all([default, registry])
        db.session.flush()
        db.session.add(OzonMarkingRule(
            registry_version_id=registry.id,
            code_prefix="3307",
            normative_ref="synthetic-fixture-only",
        ))
        db.session.flush()

        authorized, compliance_report = (
            MarketplaceDraftService._auto_map_attributes(
                product_type=self.product_type,
                facts_document=facts_document,
            )
        )
        authorized_by_id = {
            item["attribute_id"]: item for item in authorized
        }
        self.assertTrue({"31", "32", "4191"}.issubset(authorized_by_id))
        self.assertEqual(
            authorized_by_id["22232"]["values"],
            [{
                "dictionary_value_id": "tnved-synthetic-3307900008",
                "value": "3307900008 - Synthetic test entry",
            }],
        )
        self.assertEqual(
            authorized_by_id["23536"]["values"],
            [{"value": "true"}],
        )
        self.assertEqual(set(compliance_report["applied"]), {"22232", "23536"})
        self.assertEqual(compliance_report["unresolved"], [])

        draft = MarketplaceProductDraft(provenance_json=json.dumps({
            "fact.title": {"source": "supplier_fixture"},
        }))
        MarketplaceDraftService._merge_compliance_provenance(
            draft, compliance_report,
        )
        provenance = json.loads(draft.provenance_json)
        self.assertEqual(
            provenance["compliance.22232"]["source"],
            "admin_compliance_default",
        )
        self.assertEqual(
            provenance["compliance.22232"]["external_value_id"],
            "tnved-synthetic-3307900008",
        )
        self.assertEqual(
            provenance["compliance.23536"]["source"],
            "admin_marking_registry",
        )
        self.assertEqual(
            provenance["compliance.23536"]["registry_version_id"],
            registry.id,
        )
        self.assertEqual(provenance["compliance.23536"]["value"], "true")
        self.assertEqual(
            provenance["fact.title"], {"source": "supplier_fixture"},
        )

    def test_create_draft_records_compliance_provenance(self):
        """Task 8b: type-bind time is when the compliance layer writes a
        value, so it is the only point that can record exactly what it
        wrote -- `apply_to_existing_drafts(refresh=True)` later trusts only
        this record to decide whether a stored value is still "ours".
        """
        module = 'services.ozon_compliance_defaults'
        resolved = {
            'tnved': {
                'code': '4202220000',
                'value': '4202220000 - Тестовое значение',
                'external_value_id': 'tnved-value-1',
                'default_id': 11,
                'dictionary_version': 2,
            },
            'marking': True,
            'unresolved': [],
            'evidence': {'tnved_default_id': 11, 'registry_version_id': 5},
        }
        product = self._product(external_id="compliance-create")
        with patch(f'{module}.resolve_type_defaults', return_value=resolved):
            draft = MarketplaceDraftService.create_draft(
                seller_id=self.seller1_id,
                account_id=self.account1.id,
                imported_product_id=product.id,
                product_type_id=self.product_type.id,
            )

        provenance = json.loads(draft.provenance_json)
        self.assertEqual(
            provenance['compliance.22232']['external_value_id'],
            'tnved-value-1',
        )
        self.assertEqual(
            provenance['compliance.22232']['source'],
            'admin_compliance_default',
        )
        self.assertEqual(provenance['compliance.23536']['value'], 'true')
        # The merge must not clobber the fact-provenance keys `create_draft`
        # already wrote for this same draft.
        fact_keys = [
            key for key in provenance if not key.startswith('compliance.')
        ]
        self.assertTrue(fact_keys)

        stored_attrs = {
            item['attribute_id']: item
            for item in json.loads(draft.attributes_json)
        }
        self.assertEqual(
            stored_attrs['22232']['values'][0]['dictionary_value_id'],
            'tnved-value-1',
        )

    def test_update_draft_type_bind_records_compliance_provenance(self):
        module = 'services.ozon_compliance_defaults'
        resolved = {
            'tnved': {
                'code': '4202220000',
                'value': '4202220000 - Тестовое значение',
                'external_value_id': 'tnved-value-2',
                'default_id': 12,
                'dictionary_version': 2,
            },
            'marking': False,
            'unresolved': [],
            'evidence': {'tnved_default_id': 12},
        }
        product = self._product(external_id="compliance-update")
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
        )
        self.assertIsNone(draft.product_type_id)

        self.app.secret_key = 'synthetic-category-review-test'
        with patch(f'{module}.resolve_type_defaults', return_value=resolved):
            review = MarketplaceDraftService.category_impact(
                seller_id=self.seller1_id,
                draft_id=draft.id,
                expected_version=draft.version,
                target_product_type_id=self.product_type.id,
                save_mapping=False,
                actor_user_id=None,
            )
            updated = MarketplaceDraftService.update_draft(
                seller_id=self.seller1_id,
                draft_id=draft.id,
                expected_version=draft.version,
                patch={'product_type_id': self.product_type.id},
                category_review_token=review['review_token'],
            )

        provenance = json.loads(updated.provenance_json)
        self.assertEqual(
            provenance['compliance.22232']['external_value_id'],
            'tnved-value-2',
        )
        self.assertEqual(provenance['compliance.23536']['value'], 'false')

    def test_source_rebase_preserves_prior_compliance_provenance(self):
        """Task 8b: `rebase_source_defaults` fully replaces `provenance_json`
        with a fresh fact snapshot on every call; it must not discard the
        compliance layer's own provenance markers while doing so (it does
        not recompute them -- see
        `test_source_rebase_never_reclassifies_compliance_attributes`
        above). Otherwise a routine, unrelated facts refresh would make the
        admin refresh path (`apply_to_existing_drafts(refresh=True)`) treat
        the byte-identical stored value as no longer "ours".
        """
        product, draft = self._ready_draft(
            external_id="compliance-provenance-rebase"
        )

        manual_provenance = json.loads(draft.provenance_json)
        self.assertTrue(
            manual_provenance, "sanity: fact provenance already populated"
        )
        manual_provenance['compliance.22232'] = {
            'source': 'admin_compliance_default',
            'default_id': 1,
            'code': '1111111111',
            'external_value_id': 'value-a',
            'dictionary_version': 1,
        }
        draft.provenance_json = json.dumps(
            manual_provenance, ensure_ascii=False,
        )
        db.session.commit()

        original = json.loads(product.original_data)
        original['description'] = 'Описание изменилось для rebase провенанса'
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.description = 'Описание изменилось для rebase провенанса'
        db.session.commit()

        rebased = MarketplaceDraftService.rebase_source_defaults(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )

        rebased_provenance = json.loads(rebased.provenance_json)
        self.assertEqual(
            rebased_provenance['compliance.22232']['external_value_id'],
            'value-a',
            'Compliance-провенанс не должен пропадать при обычном facts '
            'rebase',
        )
        non_compliance_keys = [
            key for key in rebased_provenance
            if not key.startswith('compliance.')
        ]
        self.assertTrue(
            non_compliance_keys,
            'Merge must not have wiped the fact-derived provenance either',
        )

    def test_refresh_facts_preserves_prior_compliance_provenance(self):
        product, draft = self._ready_draft(
            external_id="compliance-provenance-refresh"
        )

        manual_provenance = json.loads(draft.provenance_json)
        manual_provenance['compliance.23536'] = {
            'source': 'admin_marking_registry',
            'registry_version_id': 3,
            'value': 'true',
        }
        draft.provenance_json = json.dumps(
            manual_provenance, ensure_ascii=False,
        )
        db.session.commit()

        original = json.loads(product.original_data)
        original['description'] = 'Описание изменилось для refresh_facts'
        product.original_data = json.dumps(original, ensure_ascii=False)
        product.description = 'Описание изменилось для refresh_facts'
        db.session.commit()

        refreshed = MarketplaceDraftService.refresh_facts(
            seller_id=self.seller1_id,
            draft_id=draft.id,
            expected_version=draft.version,
        )

        refreshed_provenance = json.loads(refreshed.provenance_json)
        self.assertEqual(
            refreshed_provenance['compliance.23536']['value'], 'true',
            'Compliance-провенанс не должен пропадать при refresh_facts',
        )

    def test_seller_can_select_official_type_without_admin_preload(self):
        self.product_type.is_enabled = False
        db.session.commit()

        found = MarketplaceDraftService.search_product_types(
            seller_id=self.seller1_id,
            query="Футболка",
        )
        self.assertEqual([item["id"] for item in found], [self.product_type.id])

        self.product_type.is_seller_selectable = False
        db.session.commit()
        hidden = MarketplaceDraftService.search_product_types(
            seller_id=self.seller1_id,
            query="Футболка",
        )
        self.assertEqual(hidden, [])

    def test_suggest_product_types_lexical_and_only_without_type(self):
        product = self._product(external_id="suggest-1")
        product.mapped_wb_category = "Футболка"
        db.session.commit()
        draft = MarketplaceDraftService.create_draft(
            seller_id=self.seller1_id,
            account_id=self.account1.id,
            imported_product_id=product.id,
            corrected_by_user_id=1,
        )
        suggestions = MarketplaceDraftService.suggest_product_types(
            seller_id=self.seller1_id, draft_id=draft.id,
        )
        self.assertTrue(suggestions)
        self.assertEqual(suggestions[0]["name"], "Футболка")
        self.assertEqual(suggestions[0]["score"], 100)
        self.assertEqual(suggestions[0]["matched_source"], "wb_subject")

        # У черновика с уже выбранным типом предложений нет
        _, typed_draft = self._ready_draft(external_id="suggest-2")
        self.assertEqual(
            MarketplaceDraftService.suggest_product_types(
                seller_id=self.seller1_id, draft_id=typed_draft.id,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
