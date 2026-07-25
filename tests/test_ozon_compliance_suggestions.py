# -*- coding: utf-8 -*-
"""TNVED hints are explainable official candidates, never automatic facts."""

from datetime import datetime
import json
import unittest

from flask import Flask

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceListing,
    MarketplaceOperation,
    MarketplaceProductDraft,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.ozon_compliance_suggestions import (
    OzonComplianceSuggestionService,
)
from services.ozon_reference_service import OzonReferenceService


class OzonComplianceSuggestionServiceTest(unittest.TestCase):
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
        now = datetime.utcnow()

        user = User(
            username="compliance-seller",
            email="compliance@test.local",
            is_active=True,
        )
        user.set_password("synthetic-password")
        self.seller = Seller(user=user, company_name="Compliance")
        self.marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
            categories_synced_at=now,
            categories_snapshot_hash="tree-hash",
        )
        db.session.add_all([self.seller, self.marketplace])
        db.session.flush()
        self.account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="synthetic-account",
            label="Synthetic",
            is_active=True,
            connection_status="connected",
        )
        self.category = MarketplaceTaxonomyCategory(
            marketplace_id=self.marketplace.id,
            external_category_id="17028959",
            name="Товары для взрослых",
            full_path="Товары для взрослых",
            depth=0,
            is_available=True,
            last_seen_at=now,
        )
        db.session.add_all([self.account, self.category])
        db.session.flush()
        self.product_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id,
            category_id=self.category.id,
            external_type_id="971749404",
            name="Насадки, удлинители эротические",
            is_available=True,
            is_enabled=True,
            attributes_synced_at=now,
            attributes_sync_status="success",
            attributes_schema_hash="schema-hash",
            attributes_version=1,
            attributes_count=1,
            required_attributes_count=1,
        )
        db.session.add(self.product_type)
        db.session.flush()
        self.definition = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id,
            product_type_id=self.product_type.id,
            external_attribute_id="22232",
            name="ТН ВЭД коды ЕАЭС",
            data_type="String",
            is_required=True,
            dictionary_id="tnved",
            max_value_count=1,
            is_collection=False,
            is_available=True,
            is_enabled=True,
            last_seen_at=now,
            values_synced_at=now,
            values_sync_status="success",
            values_snapshot_hash="tnved-values",
            values_version=1,
            values_count=4,
        )
        db.session.add(self.definition)
        db.session.flush()
        for external_id, value in (
            (
                "antibiotic",
                "3003200000 - Лекарственные средства, "
                "содержащие антибиотики",
            ),
            (
                "plastic",
                "3926909709 - Изделия прочие из пластмасс",
            ),
            (
                "rubber",
                "4016999708 - Изделия из вулканизованной "
                "резины, кроме твердой резины, прочие",
            ),
            (
                "vibration",
                "9019101000 - Аппараты электрические вибромассажные",
            ),
        ):
            db.session.add(MarketplaceAttributeValue(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                attribute_id=self.definition.id,
                external_value_id=external_id,
                value=value,
                value_normalized=OzonReferenceService.normalize_value(value),
                is_available=True,
                last_seen_at=now,
            ))
        self.product = ImportedProduct(
            seller_id=self.seller.id,
            source_type="synthetic",
            external_id="source-1",
            external_vendor_code="source-1",
            title="Насадка на член",
            category=(
                "Насадки и кольца > "
                "Удлиняющие и расширяющие насадки"
            ),
        )
        db.session.add(self.product)
        db.session.flush()
        self.draft = MarketplaceProductDraft(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            imported_product_id=self.product.id,
            product_type_id=self.product_type.id,
            offer_id="source-1",
            status="blocked",
            source_fact_hash="a" * 64,
            source_facts_json="{}",
        )
        db.session.add(self.draft)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _facts(self, *, category, title, materials):
        self.draft.source_facts_json = json.dumps({
            "version": 2,
            "facts": {
                "identity": {
                    "source_title": title,
                    "source_category": (
                        "Насадки и кольца > "
                        "Удлиняющие и расширяющие насадки"
                    ),
                    "source_categories": [category],
                },
                "attributes": {"materials": materials},
            },
        }, ensure_ascii=False)
        db.session.commit()

    def test_vibrating_product_ranks_function_code_and_ignores_bad_history(self):
        self._facts(
            category="Насадки и кольца > С вибрацией, с ротацией",
            title="Насадка на член с двумя моторами",
            materials=["Эластичный TPR"],
        )
        # A historically linked card can contain a semantically impossible
        # code. Suggestions intentionally never learn from listing consensus.
        listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            imported_product_id=self.product.id,
            product_type_id=self.product_type.id,
            offer_id="old-source-1",
            external_product_id="100",
            title=self.product.title,
            normalized_status="active",
            is_available=True,
            is_archived=False,
            link_status="linked",
            link_source="exact_source_identity",
            attributes_json=json.dumps([{
                "attribute_id": "22232",
                "complex_id": "0",
                "values": [{
                    "dictionary_value_id": "antibiotic",
                    "value": "3003200000 - Лекарственные средства",
                }],
            }], ensure_ascii=False),
            sync_fingerprint="b" * 64,
        )
        db.session.add(listing)
        db.session.commit()
        before = self.draft.attributes_json

        suggestions = (
            OzonComplianceSuggestionService.tnved_suggestions(
                draft=self.draft,
                definition=self.definition,
            )
        )

        self.assertEqual(suggestions[0]["code"], "9019101000")
        self.assertEqual(suggestions[0]["dictionary_value_id"], "vibration")
        self.assertEqual(suggestions[0]["confidence"], "medium")
        self.assertNotIn(
            "3003200000",
            {item["code"] for item in suggestions},
        )
        self.assertEqual(self.draft.attributes_json, before)
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_non_vibrating_tpr_shows_material_alternatives_not_vibration(self):
        self._facts(
            category="Насадки и кольца > Без вибрации",
            title="Насадка на член",
            materials=["TPR (Термопластичная резина)"],
        )

        suggestions = (
            OzonComplianceSuggestionService.tnved_suggestions(
                draft=self.draft,
                definition=self.definition,
            )
        )

        codes = [item["code"] for item in suggestions]
        self.assertEqual(codes[:2], ["4016999708", "3926909709"])
        self.assertNotIn("9019101000", codes)
        self.assertTrue(all(
            item["confidence"] == "low"
            for item in suggestions
        ))


class MaterialSignalTestCase(unittest.TestCase):
    def _signals(self, material):
        from services.ozon_compliance_suggestions import (
            OzonComplianceSuggestionService as S,
        )
        return S._material_flags(material)

    def test_cyrillic_tpe_abbreviation_is_rubber_like(self):
        flags = self._signals('ТПЭ')
        self.assertTrue(flags['rubber_like'])

    def test_cyrillic_tpr_abbreviation_is_rubber_like(self):
        flags = self._signals('ТПР')
        self.assertTrue(flags['rubber_like'])

    def test_latin_tpe_still_recognized(self):
        self.assertTrue(self._signals('TPE')['rubber_like'])

    def test_unrelated_material_is_not_rubber_like(self):
        self.assertFalse(self._signals('Стекло')['rubber_like'])


if __name__ == "__main__":
    unittest.main()
