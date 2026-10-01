"""Bounded seller-common content updates stay separate from source evidence."""

from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask
from sqlalchemy import update

from models import (
    AgentChangeSnapshot,
    ImportedProduct,
    Marketplace,
    MarketplaceProductDraft,
    Seller,
    SellerMarketplaceAccount,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from services.common_product_content import (
    CommonProductContentConflict,
    CommonProductContentError,
    CommonProductContentService,
    content_writer_guard_snapshot,
    guard_content_writer,
    _characteristic_rows,
    normalize_value,
    refresh_from_source,
)
from services.ozon_draft_ai_validation import _source_facts


@pytest.fixture
def content_app():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="common-content-test-key",
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _seller():
    number = User.query.count() + 1
    user = User(username=f"common-seller-{number}", email=f"common-seller-{number}@test.local")
    user.set_password("synthetic")
    seller = Seller(user=user, company_name=f"Test seller {number}")
    db.session.add(seller)
    db.session.flush()
    return seller


def _supplier_product(seller_supplier, **updates):
    values = {
        "supplier_id": seller_supplier.id,
        "external_id": "supplier-1",
        "title": "Source title",
        "description": "Source description",
        "photo_urls_json": json.dumps(["https://images.example/source.jpg"]),
        "characteristics_json": json.dumps([{"name": "Материал", "value": "Хлопок"}]),
        "content_revision": 1,
    }
    values.update(updates)
    product = SupplierProduct(**values)
    db.session.add(product)
    db.session.flush()
    return product


def _imported(seller_id, **updates):
    values = {
        "seller_id": seller_id,
        "external_id": "import-1",
        "source_type": "supplier_catalog",
        "title": "Source title",
        "description": "Source description",
        "photo_urls": json.dumps(["https://images.example/source.jpg"]),
        "characteristics": json.dumps([{"name": "Материал", "value": "Хлопок"}]),
        "original_data": json.dumps({
            "title": "Source title",
            "description": "Source description",
            "photo_urls": ["https://images.example/source.jpg"],
            "characteristics": [{"name": "Материал", "value": "Хлопок"}],
        }),
    }
    values.update(updates)
    product = ImportedProduct(**values)
    db.session.add(product)
    db.session.flush()
    return product


def _override(product, user_id, **changes):
    preview = CommonProductContentService.preview(
        seller_id=product.seller_id,
        user_id=user_id,
        raw_items=[{
            "product_id": product.id,
            "expected_content_edit_version": product.content_edit_version or 1,
            "changes": changes,
            "recipients": [],
        }],
    )
    result = CommonProductContentService.apply(
        seller_id=product.seller_id,
        user_id=user_id,
        token=preview["preview_token"],
    )
    db.session.commit()
    return result


def test_supplier_json_photos_and_characteristics_are_tolerant_and_keep_provenance(content_app):
    with content_app.app_context():
        seller = _seller()
        supplier = Supplier(name="Feed", code="feed")
        db.session.add(supplier)
        db.session.flush()
        shared = _supplier_product(
            supplier,
            title="Supplier title",
            description=None,
            ai_description="AI suggestion only",
            photo_urls_json=json.dumps([{"original": "https://images.example/source.jpg"}]),
            characteristics_json=json.dumps([{"charcName": "Материал", "value": "Хлопок", "charcID": 17}]),
        )
        imported = _imported(
            seller.id,
            title=shared.title,
            description=shared.ai_description,
            supplier_id=supplier.id,
            supplier_product_id=shared.id,
            supplier_product=shared,
            photo_urls=shared.photo_urls_json,
            characteristics=shared.characteristics_json,
            original_data=shared.original_data_json,
        )
        db.session.commit()

        with patch("services.source_photo_display.imported_photo_previews", return_value={}):
            state = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[imported.id])[0]

        assert state["fields"]["photos"]["inherited"] == ["https://images.example/source.jpg"]
        assert state["fields"]["characteristics"]["inherited"] == [
            {"name": "Материал", "value": "Хлопок"},
        ]
        assert state["fields"]["description"]["origin"] == "ai_suggestion"
        assert state["fields"]["characteristics"]["origin"] == "source"
        assert state["source"]["observed_at"] == shared.updated_at.isoformat()


def test_blank_unknown_source_can_be_overridden_and_reopened_without_fake_freshness(content_app):
    with content_app.app_context():
        seller = _seller()
        product = _imported(seller.id, title="", description="", original_data=None)
        db.session.commit()
        initial = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[product.id])[0]
        assert initial["fields"]["title"]["inherited"] == ""
        assert initial["fields"]["title"]["origin"] == "unknown"
        assert initial["source"]["observed_at"] is None

        _override(product, 73, title={"mode": "override", "value": "Новое название"})
        product = db.session.get(ImportedProduct, product.id)
        product.updated_at = datetime.utcnow() + timedelta(days=1)
        db.session.commit()
        reopened = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[product.id])[0]

        assert reopened["fields"]["title"]["effective"] == "Новое название"
        assert reopened["fields"]["title"]["inherited"] == ""
        assert reopened["fields"]["title"]["origin"] == "seller_override"
        assert reopened["fields"]["title"]["inherited_origin"] == "unknown"
        assert reopened["source"]["observed_at"] is None
        override_data = json.loads(product.content_overrides_json)
        assert override_data["fields"]["title"]["inherited_value"] == ""


def test_source_refresh_updates_inherited_snapshot_but_preserves_manual_effective_value(content_app):
    with content_app.app_context():
        seller = _seller()
        product = _imported(seller.id)
        db.session.commit()
        _override(product, 74, title={"mode": "override", "value": "Ручное название"})
        product = db.session.get(ImportedProduct, product.id)

        refresh_from_source(
            product,
            {"title": "Обновлённый источник"},
            provenance={"title": "source"},
        )
        db.session.commit()
        state = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[product.id])[0]
        override_data = json.loads(product.content_overrides_json)

        assert product.title == "Ручное название"
        assert state["fields"]["title"]["effective"] == "Ручное название"
        assert state["fields"]["title"]["inherited"] == "Обновлённый источник"
        assert state["fields"]["title"]["inherited_origin"] == "source"
        assert json.loads(product.original_data)["title"] == "Обновлённый источник"
        assert override_data["fields"]["title"]["inherited_value"] == "Обновлённый источник"


def test_refresh_accepts_supplier_json_strings_for_photos_and_characteristics(content_app):
    with content_app.app_context():
        seller = _seller()
        product = _imported(seller.id, photo_urls="[]", characteristics="[]")
        db.session.commit()
        raw_photos = json.dumps([{"original": "https://images.example/new.jpg"}], ensure_ascii=False)
        raw_characteristics = json.dumps([{"charcName": "Размер", "value": "Большой", "charcID": 11}], ensure_ascii=False)

        refresh_from_source(
            product,
            {"photos": raw_photos, "characteristics": raw_characteristics},
            provenance={"photos": "source", "characteristics": "source"},
        )
        db.session.commit()
        state = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[product.id])[0]

        assert state["fields"]["photos"]["inherited"] == ["https://images.example/new.jpg"]
        assert state["fields"]["characteristics"]["inherited"] == [
            {"name": "Размер", "value": "Большой"},
        ]
        assert product.photo_urls == raw_photos
        assert product.characteristics == raw_characteristics


def test_ai_description_is_not_labeled_as_observed_source(content_app):
    with content_app.app_context():
        seller = _seller()
        supplier = Supplier(name="Feed", code="feed-ai")
        db.session.add(supplier)
        db.session.flush()
        shared = _supplier_product(
            supplier,
            description=None,
            ai_description="Generated suggestion",
            description_source="ai",
        )
        imported = _imported(
            seller.id,
            supplier_id=supplier.id,
            supplier_product_id=shared.id,
            supplier_product=shared,
            description=shared.ai_description,
        )
        db.session.commit()

        state = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[imported.id])[0]

        assert state["fields"]["description"]["inherited"] == "Generated suggestion"
        assert state["fields"]["description"]["origin"] == "ai_suggestion"


def test_supplier_refresh_preserves_all_manual_fields_and_keeps_ai_out_of_source(content_app):
    with content_app.app_context():
        seller = _seller()
        supplier = Supplier(name="Feed", code="feed-manual-refresh")
        db.session.add(supplier)
        db.session.flush()
        shared = _supplier_product(
            supplier,
            title="Old source title",
            description=None,
            ai_description="Old suggestion",
            description_source="ai",
            original_data_json=json.dumps({"description": "Raw source description"}),
        )
        product = _imported(
            seller.id,
            supplier_id=supplier.id,
            supplier_product_id=shared.id,
            supplier_product=shared,
            title=shared.title,
            description=shared.ai_description,
            photo_urls=shared.photo_urls_json,
            characteristics=shared.characteristics_json,
            original_data=shared.original_data_json,
        )
        db.session.commit()
        _override(
            product,
            785,
            title={"mode": "override", "value": "Моё название"},
            description={"mode": "override", "value": "Моё описание"},
            photos={"mode": "override", "value": ["https://images.example/source.jpg"]},
            characteristics={"mode": "override", "value": [{"name": "Ручное свойство", "value": "Да"}]},
        )
        product = db.session.get(ImportedProduct, product.id)
        shared = db.session.get(SupplierProduct, shared.id)
        shared.title = "Fresh source title"
        shared.description = None
        shared.ai_description = "Fresh AI suggestion"
        shared.description_source = "ai"
        shared.photo_urls_json = json.dumps(["https://images.example/fresh.jpg"])
        shared.characteristics_json = json.dumps([{"name": "Материал", "value": "Лён"}], ensure_ascii=False)
        shared.ai_marketplace_json = json.dumps({
            "Материал": "Хлопок",
            "_meta": {"source": "supplier_catalog_enrichment"},
        }, ensure_ascii=False)
        shared.original_data_json = json.dumps({"description": "Fresh raw source description"})
        shared.content_revision += 1
        db.session.commit()

        from services.supplier_service import _update_imported_from_supplier
        _update_imported_from_supplier(product, shared)
        db.session.commit()
        db.session.refresh(product)
        state = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[product.id])[0]
        source = json.loads(product.original_data)

        assert product.title == "Моё название"
        assert product.description == "Моё описание"
        assert json.loads(product.photo_urls) == ["https://images.example/source.jpg"]
        assert _characteristic_rows(product.characteristics) == [
            {"name": "Ручное свойство", "value": "Да"},
        ]
        assert state["fields"]["title"]["inherited"] == "Fresh source title"
        assert state["fields"]["description"]["inherited"] == "Fresh AI suggestion"
        assert state["fields"]["description"]["inherited_origin"] == "ai_suggestion"
        assert state["fields"]["characteristics"]["inherited"] == [
            {"name": "Материал", "value": "Хлопок"},
        ]
        assert state["fields"]["characteristics"]["inherited_origin"] == "supplier_enrichment"
        assert source["description"] == "Fresh raw source description"
        assert source["characteristics"] == [{"name": "Материал", "value": "Лён"}]
        serialized_source = json.dumps(source, ensure_ascii=False)
        assert "Моё название" not in serialized_source
        assert "Моё описание" not in serialized_source
        assert "Ручное свойство" not in serialized_source


def test_supplier_sync_clears_removed_shared_characteristics(content_app):
    with content_app.app_context():
        seller = _seller()
        supplier = Supplier(name="Feed", code="feed-removed-chars")
        db.session.add(supplier)
        db.session.flush()
        shared = _supplier_product(
            supplier,
            ai_marketplace_json=json.dumps({
                "Материал изделия": "Силикон",
                "_meta": {"source": "supplier_catalog_enrichment"},
            }, ensure_ascii=False),
        )
        product = _imported(
            seller.id,
            wb_subject_id=17,
            supplier_id=supplier.id,
            supplier_product_id=shared.id,
            supplier_product=shared,
            characteristics=json.dumps([{"name": "Материал изделия", "value": "Силикон"}], ensure_ascii=False),
        )
        db.session.commit()
        shared.wb_subject_id = None
        shared.characteristics_json = None
        shared.ai_marketplace_json = None
        shared.content_revision += 1
        db.session.commit()

        from services.supplier_service import _update_imported_from_supplier
        _update_imported_from_supplier(product, shared)
        db.session.commit()
        db.session.refresh(product)

        assert product.characteristics is None
        assert json.loads(product.original_data)["characteristics"] is None


def test_supplier_missing_fields_reopen_from_saved_inherited_values(content_app):
    with content_app.app_context():
        seller = _seller()
        supplier = Supplier(name="Feed", code="feed-missing-refresh")
        db.session.add(supplier)
        db.session.flush()
        shared = _supplier_product(
            supplier,
            title=None,
            description=None,
            ai_description=None,
            photo_urls_json=None,
            characteristics_json=None,
        )
        product = _imported(
            seller.id,
            supplier_id=supplier.id,
            supplier_product_id=shared.id,
            supplier_product=shared,
            title="Original inherited title",
            description="Original inherited description",
            photo_urls=json.dumps(["https://images.example/original.jpg"]),
            characteristics=json.dumps([{"name": "Исходное свойство", "value": "Да"}], ensure_ascii=False),
            original_data=None,
        )
        db.session.commit()
        _override(
            product,
            786,
            title={"mode": "override", "value": "Новое ручное название"},
            description={"mode": "override", "value": "Новое ручное описание"},
            photos={"mode": "override", "value": ["https://images.example/original.jpg"]},
            characteristics={"mode": "override", "value": [{"name": "Ручное свойство", "value": "Да"}]},
        )
        product = db.session.get(ImportedProduct, product.id)
        shared = db.session.get(SupplierProduct, shared.id)
        shared.content_revision += 1
        db.session.commit()

        from services.supplier_service import _update_imported_from_supplier
        _update_imported_from_supplier(product, shared)
        db.session.commit()
        db.session.refresh(product)
        reopened = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[product.id])[0]

        assert reopened["fields"]["title"]["effective"] == "Новое ручное название"
        assert reopened["fields"]["title"]["inherited"] == "Original inherited title"
        assert reopened["fields"]["title"]["inherited_origin"] == "unknown"
        assert reopened["fields"]["description"]["inherited"] == "Original inherited description"
        assert reopened["fields"]["photos"]["inherited"] == ["https://images.example/original.jpg"]
        assert reopened["fields"]["characteristics"]["effective"] == [
            {"name": "Ручное свойство", "value": "Да"},
        ]
        assert reopened["fields"]["characteristics"]["inherited"] == []
        assert "Новое ручное" not in (product.original_data or "")
        assert "Ручное свойство" not in (product.original_data or "")

        _override(
            product,
            786,
            title={"mode": "inherit"},
            description={"mode": "inherit"},
            photos={"mode": "inherit"},
            characteristics={"mode": "inherit"},
        )
        db.session.refresh(product)
        assert product.title == "Original inherited title"
        assert product.description == "Original inherited description"
        assert json.loads(product.photo_urls) == ["https://images.example/original.jpg"]
        assert json.loads(product.characteristics) == []


def test_agent_common_field_update_is_blocked_by_manual_override(content_app):
    with content_app.app_context():
        seller = _seller()
        product = _imported(seller.id)
        db.session.commit()
        _override(product, 787, title={"mode": "override", "value": "Manual title"})
        db.session.refresh(product)

        from routes.internal_api import _validate_and_apply_imported_product_update
        ok, error = _validate_and_apply_imported_product_update(
            product,
            {"title": "AI replacement"},
            task_id="test-task",
            agent_id="test-agent",
        )

        assert ok is False
        assert "снимите переопределение" in error
        assert product.title == "Manual title"


def test_agent_writer_conflict_after_same_value_override_race_is_retryable(content_app):
    with content_app.app_context():
        seller = _seller()
        product = _imported(seller.id, title="Current title")
        db.session.commit()
        from routes.internal_api import (
            _is_common_content_override_error,
            _validate_and_apply_imported_product_update,
        )

        def race_after_initial_check(_product):
            db.session.execute(
                update(ImportedProduct)
                .where(ImportedProduct.id == product.id)
                .values(
                    title="Current title",
                    content_edit_version=2,
                    content_overrides_json=json.dumps({
                        "schema_version": 1,
                        "fields": {
                            "title": {
                                "value": "Current title",
                                "inherited_value": "Current title",
                                "inherited_origin": "source",
                                "edited_by_user_id": 790,
                                "edited_at": "2026-10-01T00:00:00",
                                "edit_version": 2,
                            },
                        },
                    }),
                )
            )
            db.session.commit()
            return set()

        with patch("services.common_product_content.active_override_fields", side_effect=race_after_initial_check):
            ok, error = _validate_and_apply_imported_product_update(
                product,
                {"title": "Stale AI title"},
                task_id="test-task-race",
                agent_id="test-agent",
            )

        assert ok is False
        assert _is_common_content_override_error(error) is True
        db.session.expire_all()
        current = db.session.get(ImportedProduct, product.id)
        assert current.title == "Current title"
        assert current.content_edit_version == 2


def test_csv_refresh_keeps_manual_common_values_and_raw_source_snapshot(content_app):
    with content_app.app_context():
        seller = _seller()
        old_photo = "https://images.example/csv-old.jpg"
        new_photo = "https://images.example/csv-new.jpg"
        product = _imported(
            seller.id,
            external_id="csv-refresh",
            source_type="fixture-csv",
            import_status="imported",
            title="Old CSV title",
            description="Old CSV description",
            photo_urls=json.dumps([old_photo]),
            original_data=json.dumps({
                "title": "Old CSV title",
                "description": "Old raw description",
                "photo_urls": [old_photo],
                "characteristics": {},
            }, ensure_ascii=False),
        )
        db.session.commit()
        _override(
            product,
            788,
            title={"mode": "override", "value": "Моё CSV название"},
            description={"mode": "override", "value": "Моё CSV описание"},
            photos={"mode": "override", "value": [old_photo]},
        )
        product = db.session.get(ImportedProduct, product.id)

        from services.auto_import_manager import AutoImportManager
        manager = AutoImportManager.__new__(AutoImportManager)
        manager.seller = seller
        manager.settings = SimpleNamespace(
            csv_source_type="fixture-csv",
            vendor_code_pattern="{external_id}",
            supplier_code="fixture",
            ai_use_for_sizes=False,
            ai_use_for_categories=False,
        )
        manager.ai_service = None
        manager.category_mapper = SimpleNamespace(
            map_category=lambda *args, **kwargs: (17, "Тестовая категория", 0.95),
        )
        manager.validator = SimpleNamespace(
            validate_product=lambda data: (True, []),
        )
        manager._generate_description = lambda data: "AI suggestion from CSV"
        manager._find_duplicate_by_barcode = lambda product, barcodes: 0
        product_data = {
            "external_id": "csv-refresh",
            "external_vendor_code": "CSV-1",
            "title": "Fresh CSV title",
            "description": "Fresh raw CSV description",
            "category": "Категория",
            "general_category": "",
            "all_categories": [],
            "brand": "Brand",
            "country": "",
            "gender": "",
            "colors": [],
            "sizes": {},
            "materials": [],
            "photo_urls": [new_photo],
            "characteristics": {"Материал": "Хлопок"},
            "barcodes": [],
            "supplier_price": None,
            "supplier_quantity": None,
        }

        result = manager._process_product(product_data)
        db.session.expire_all()
        refreshed = db.session.get(ImportedProduct, product.id)
        state = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[product.id])[0]
        source = json.loads(refreshed.original_data)

        assert result == "skipped"
        assert refreshed.title == "Моё CSV название"
        assert refreshed.description == "Моё CSV описание"
        assert json.loads(refreshed.photo_urls) == [old_photo]
        assert state["fields"]["title"]["inherited"] == "Fresh CSV title"
        assert state["fields"]["description"]["inherited"] == "Fresh raw CSV description"
        assert state["fields"]["photos"]["inherited"] == [new_photo]
        assert source["title"] == "Fresh CSV title"
        assert source["description"] == "Fresh raw CSV description"
        assert source["photo_urls"] == [new_photo]
        assert "Моё CSV" not in json.dumps(source, ensure_ascii=False)


def test_csv_same_value_override_race_rolls_back_row_and_releases_transaction(content_app):
    with content_app.app_context():
        from services.auto_import_manager import AutoImportManager
        from services.common_product_content import guard_content_writer as real_guard

        seller = _seller()
        product = _imported(
            seller.id,
            external_id="csv-race",
            source_type="fixture-csv-race",
            import_status="imported",
            title="Old CSV title",
            description="Old CSV description",
            original_data=json.dumps({"title": "Old CSV title", "description": "Old raw description"}),
        )
        db.session.commit()
        _override(product, 789, title={"mode": "override", "value": "Ручное название"})
        product = db.session.get(ImportedProduct, product.id)
        product_id = product.id
        observed_version = product.content_edit_version

        manager = AutoImportManager.__new__(AutoImportManager)
        manager.seller = seller
        manager.settings = SimpleNamespace(
            csv_source_type="fixture-csv-race",
            vendor_code_pattern="{external_id}",
            supplier_code="fixture",
            ai_use_for_sizes=False,
            ai_use_for_categories=False,
        )
        manager.ai_service = None
        manager.category_mapper = SimpleNamespace(
            map_category=lambda *args, **kwargs: (17, "Тестовая категория", 0.95),
        )
        manager.validator = SimpleNamespace(validate_product=lambda data: (True, []))
        manager._generate_description = lambda data: "AI suggestion from CSV"
        manager._find_duplicate_by_barcode = lambda imported, barcodes: 0
        product_data = {
            "external_id": "csv-race",
            "external_vendor_code": "CSV-race",
            "title": "Concurrent CSV title",
            "description": "Concurrent source description",
            "category": "Категория",
            "general_category": "",
            "all_categories": [],
            "brand": "Brand",
            "country": "",
            "gender": "",
            "colors": [],
            "sizes": {},
            "materials": [],
            "photo_urls": [],
            "characteristics": {},
            "barcodes": [],
            "supplier_price": None,
            "supplier_quantity": None,
        }

        def bump_same_effective_value(product, expected):
            # A concurrent common save can advance only its metadata while
            # leaving the effective value unchanged. The CSV writer must still
            # fail closed against its stale pre-read seal.
            db.session.execute(
                update(ImportedProduct)
                .where(ImportedProduct.id == product_id)
                .values(content_edit_version=observed_version + 1)
                .execution_options(synchronize_session=False)
            )
            db.session.commit()
            real_guard(product, expected=expected)

        with patch("services.common_product_content.guard_content_writer", side_effect=bump_same_effective_value):
            outcome = manager._process_product(product_data)

        assert outcome == "failed"
        assert not db.session().in_transaction()
        db.session.expire_all()
        current = db.session.get(ImportedProduct, product_id)
        assert current.content_edit_version == observed_version + 1
        assert current.title == "Ручное название"
        assert current.import_status == "imported"
        assert json.loads(current.original_data)["title"] == "Old CSV title"


def test_selected_photo_survives_source_gallery_refresh_and_missing_preview(content_app):
    with content_app.app_context():
        seller = _seller()
        supplier = Supplier(name="Feed", code="feed-photo")
        db.session.add(supplier)
        db.session.flush()
        old_url = "https://images.example/old.jpg"
        new_url = "https://images.example/new.jpg"
        shared = _supplier_product(supplier, photo_urls_json=json.dumps([old_url]))
        imported = _imported(
            seller.id,
            supplier_id=supplier.id,
            supplier_product_id=shared.id,
            supplier_product=shared,
            photo_urls=json.dumps([old_url]),
        )
        db.session.commit()
        _override(imported, 75, photos={"mode": "override", "value": [old_url]})
        imported = db.session.get(ImportedProduct, imported.id)
        shared = db.session.get(SupplierProduct, shared.id)
        shared.photo_urls_json = json.dumps([new_url])
        shared.content_revision += 1
        refresh_from_source(
            imported,
            {"photos": shared.photo_urls_json},
            provenance={"photos": "source"},
        )
        db.session.commit()

        with patch("services.source_photo_display.imported_photo_previews", return_value={}):
            state = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[imported.id])[0]
        options = {option["url"]: option for option in state["photo_options"]}

        assert state["fields"]["photos"]["effective"] == [old_url]
        assert state["fields"]["photos"]["inherited"] == [new_url]
        assert options[old_url]["available_for_selection"] is True
        assert options[new_url]["available_for_selection"] is False
        preview = CommonProductContentService.preview(
            seller_id=seller.id,
            user_id=75,
            raw_items=[{
                "product_id": imported.id,
                "expected_content_edit_version": imported.content_edit_version,
                "changes": {"description": {"mode": "override", "value": "Описание"}},
                "recipients": [],
            }],
        )
        assert preview["preview_token"]


def test_supplier_refresh_batch_savepoint_discards_failed_row_after_guard(content_app):
    with content_app.app_context():
        from services.supplier_service import SupplierService
        import services.supplier_service as supplier_service

        seller = _seller()
        supplier = Supplier(name="Feed", code="feed-savepoint")
        db.session.add(supplier)
        db.session.flush()
        first_source = _supplier_product(supplier, external_id="first", title="Fresh first")
        second_source = _supplier_product(supplier, external_id="second", title="Fresh second")
        first = _imported(
            seller.id,
            external_id="first",
            supplier_id=supplier.id,
            supplier_product_id=first_source.id,
            supplier_product=first_source,
        )
        second = _imported(
            seller.id,
            external_id="second",
            supplier_id=supplier.id,
            supplier_product_id=second_source.id,
            supplier_product=second_source,
        )
        db.session.commit()
        first_id, second_id = first.id, second.id
        update_row = supplier_service._update_imported_from_supplier

        def update_then_fail_second(product, source):
            update_row(product, source)
            if product.id == second_id:
                raise RuntimeError("synthetic failure after guarded update")

        with patch.object(
            supplier_service,
            "_update_imported_from_supplier",
            side_effect=update_then_fail_second,
        ):
            result = SupplierService.update_seller_products(seller.id)

        assert result.imported == 1
        assert result.errors == 1
        assert not db.session().in_transaction()
        db.session.expire_all()
        assert db.session.get(ImportedProduct, first_id).title == "Fresh first"
        assert db.session.get(ImportedProduct, second_id).title == "Source title"


@pytest.mark.parametrize("bad", [
    "{\"Размер\": \"M\"}",
    [{"name": "Цвет", "value": "Синий", "charcID": 7}],
    [None],
    [{"name": "Артикул", "value": "123"}],
    {f"Свойство {index}": str(index) for index in range(101)},
    [{"name": f"Свойство {index}", "value": str(index)} for index in range(101)],
])
def test_manual_characteristics_reject_raw_json_provider_ids_and_oversized_sets(bad):
    with pytest.raises(CommonProductContentError):
        normalize_value("characteristics", bad)


def test_manual_characteristics_accept_named_physical_properties_and_bounded_values():
    assert normalize_value("characteristics", [
        {"name": "Длина", "value": 42},
        {"name": "Материал", "value": ["Хлопок", "Лён"]},
    ]) == [
        {"name": "Длина", "value": 42},
        {"name": "Материал", "value": ["Хлопок", "Лён"]},
    ]


def test_legacy_nested_characteristics_remain_visible_without_silent_truncation():
    rows = _characteristic_rows(json.dumps([
        {"name": "Размер", "value": {"label": "XL", "source_id": 12}},
        {"name": "Ткань", "value": ["Хлопок", "Лён"]},
    ]))
    assert rows == [
        {"name": "Размер", "value": {"label": "XL", "source_id": 12}},
        {"name": "Ткань", "value": ["Хлопок", "Лён"]},
    ]


def test_signed_preview_detects_current_and_source_drift_and_cannot_cross_seller(content_app):
    with content_app.app_context():
        seller = _seller()
        other = _seller()
        product = _imported(seller.id)
        foreign = _imported(other.id, external_id="foreign")
        db.session.commit()
        changes = {"title": {"mode": "override", "value": "Reviewed title"}}
        preview = CommonProductContentService.preview(
            seller_id=seller.id,
            user_id=76,
            raw_items=[{
                "product_id": product.id,
                "expected_content_edit_version": product.content_edit_version,
                "changes": changes,
                "recipients": [],
            }],
        )
        with pytest.raises(Exception):
            CommonProductContentService.read_many(seller_id=seller.id, product_ids=[foreign.id])

        product.description = "Changed current value"
        db.session.commit()
        with pytest.raises(CommonProductContentConflict):
            CommonProductContentService.apply(seller_id=seller.id, user_id=76, token=preview["preview_token"])


def test_supplier_source_drift_after_preview_is_rejected(content_app):
    with content_app.app_context():
        seller = _seller()
        supplier = Supplier(name="Feed", code="feed-drift")
        db.session.add(supplier)
        db.session.flush()
        shared = _supplier_product(supplier)
        product = _imported(
            seller.id,
            supplier_id=supplier.id,
            supplier_product_id=shared.id,
            supplier_product=shared,
        )
        db.session.commit()
        preview = CommonProductContentService.preview(
            seller_id=seller.id,
            user_id=77,
            raw_items=[{
                "product_id": product.id,
                "expected_content_edit_version": product.content_edit_version,
                "changes": {"title": {"mode": "override", "value": "Review"}},
                "recipients": [],
            }],
        )
        shared.title = "Source changed after preview"
        shared.content_revision += 1
        db.session.commit()

        with pytest.raises(CommonProductContentConflict):
            CommonProductContentService.apply(seller_id=seller.id, user_id=77, token=preview["preview_token"])

        db.session.refresh(product)
        assert product.title == "Source title"
        assert product.content_edit_version == 1


@pytest.mark.parametrize("field,value", [
    ("description_source", "manual"),
    ("ai_marketplace_json", '{"_meta":{"source":"supplier_catalog_enrichment"},"Материал":"Лён"}'),
])
def test_supplier_metadata_only_drift_invalidates_review(content_app, field, value):
    with content_app.app_context():
        seller = _seller()
        supplier = Supplier(name="Feed", code=f"feed-metadata-{field}")
        db.session.add(supplier)
        db.session.flush()
        shared = _supplier_product(supplier, description=None, ai_description="Generated")
        product = _imported(
            seller.id,
            supplier_id=supplier.id,
            supplier_product_id=shared.id,
            supplier_product=shared,
        )
        db.session.commit()
        preview = CommonProductContentService.preview(
            seller_id=seller.id,
            user_id=781,
            raw_items=[{
                "product_id": product.id,
                "expected_content_edit_version": 1,
                "changes": {"title": {"mode": "override", "value": "Reviewed"}},
                "recipients": [],
            }],
        )
        # Keep the timestamp and revision fixed to exercise the exact metadata seal.
        db.session.execute(
            update(SupplierProduct)
            .where(SupplierProduct.id == shared.id)
            .values(**{field: value}, updated_at=shared.updated_at)
        )
        db.session.expire_all()

        with pytest.raises(CommonProductContentConflict):
            CommonProductContentService.apply(
                seller_id=seller.id,
                user_id=781,
                token=preview["preview_token"],
            )

        db.session.expire_all()
        assert db.session.get(ImportedProduct, product.id).title == "Source title"
        assert db.session.get(ImportedProduct, product.id).content_edit_version == 1


def test_raw_legacy_current_snapshot_drift_invalidates_review(content_app):
    with content_app.app_context():
        seller = _seller()
        product = _imported(seller.id)
        db.session.commit()
        preview = CommonProductContentService.preview(
            seller_id=seller.id,
            user_id=782,
            raw_items=[{
                "product_id": product.id,
                "expected_content_edit_version": 1,
                "changes": {"title": {"mode": "override", "value": "Reviewed"}},
                "recipients": [],
            }],
        )
        before = product.characteristics
        product.characteristics = json.dumps(json.loads(before), ensure_ascii=False, indent=2)
        db.session.commit()

        with pytest.raises(CommonProductContentConflict):
            CommonProductContentService.apply(
                seller_id=seller.id,
                user_id=782,
                token=preview["preview_token"],
            )


def test_two_item_apply_rolls_back_first_update_when_later_cas_fails(content_app):
    with content_app.app_context():
        seller = _seller()
        first = _imported(seller.id, external_id="atomic-first")
        second = _imported(seller.id, external_id="atomic-second")
        db.session.commit()
        preview = CommonProductContentService.preview(
            seller_id=seller.id,
            user_id=78,
            raw_items=[
                {
                    "product_id": first.id,
                    "expected_content_edit_version": 1,
                    "changes": {"title": {"mode": "override", "value": "First reviewed"}},
                    "recipients": [],
                },
                {
                    "product_id": second.id,
                    "expected_content_edit_version": 1,
                    "changes": {"title": {"mode": "override", "value": "Second reviewed"}},
                    "recipients": [],
                },
            ],
        )
        execute = db.session.execute
        calls = 0

        def fail_second_update(statement, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                return SimpleNamespace(rowcount=0)
            return execute(statement, *args, **kwargs)

        with patch.object(db.session, "execute", side_effect=fail_second_update):
            with pytest.raises(CommonProductContentConflict):
                CommonProductContentService.apply(seller_id=seller.id, user_id=78, token=preview["preview_token"])

        db.session.expire_all()
        assert db.session.get(ImportedProduct, first.id).title == "Source title"
        assert db.session.get(ImportedProduct, second.id).title == "Source title"
        assert AgentChangeSnapshot.query.count() == 0


def test_postwrite_supplier_source_drift_rolls_back_entire_apply(content_app):
    with content_app.app_context():
        seller = _seller()
        supplier = Supplier(name="Feed", code="feed-postwrite-drift")
        db.session.add(supplier)
        db.session.flush()
        shared = _supplier_product(supplier)
        product = _imported(
            seller.id,
            supplier_id=supplier.id,
            supplier_product_id=shared.id,
            supplier_product=shared,
        )
        db.session.commit()
        preview = CommonProductContentService.preview(
            seller_id=seller.id,
            user_id=79,
            raw_items=[{
                "product_id": product.id,
                "expected_content_edit_version": 1,
                "changes": {"title": {"mode": "override", "value": "Reviewed"}},
                "recipients": [],
            }],
        )
        import services.common_product_content as content_service
        source_state = content_service._source_state
        source_checks = 0

        def change_source_during_apply(current, overrides):
            nonlocal source_checks
            source_checks += 1
            if source_checks == 2:
                supplier_id = current.supplier_product_id
                db.session.execute(
                    update(SupplierProduct)
                    .where(SupplierProduct.id == supplier_id)
                    .values(title="Raced source", content_revision=2)
                )
                db.session.expire_all()
            return source_state(current, overrides)

        with patch("services.common_product_content._source_state", side_effect=change_source_during_apply):
            with pytest.raises(CommonProductContentConflict):
                CommonProductContentService.apply(seller_id=seller.id, user_id=79, token=preview["preview_token"])

        db.session.expire_all()
        assert db.session.get(ImportedProduct, product.id).title == "Source title"
        assert db.session.get(SupplierProduct, shared.id).title == "Source title"
        assert AgentChangeSnapshot.query.count() == 0


def test_recipient_context_drift_after_write_rolls_back_common_save(content_app):
    with content_app.app_context():
        seller = _seller()
        product = _imported(seller.id)
        marketplace = Marketplace(name="Ozon", code="ozon", adapter_code="ozon")
        db.session.add(marketplace)
        db.session.flush()
        account = SellerMarketplaceAccount(
            seller_id=seller.id,
            marketplace_id=marketplace.id,
            external_account_id="account-1",
            label="Основной кабинет",
            is_active=True,
        )
        db.session.add(account)
        db.session.flush()
        draft = MarketplaceProductDraft(
            seller_id=seller.id,
            marketplace_id=marketplace.id,
            account_id=account.id,
            imported_product_id=product.id,
            offer_id="offer-1",
            status="needs_category",
            source_fact_hash="a" * 64,
            content_json='{"name":"Draft title"}',
        )
        db.session.add(draft)
        db.session.commit()
        preview = CommonProductContentService.preview(
            seller_id=seller.id,
            user_id=80,
            raw_items=[{
                "product_id": product.id,
                "expected_content_edit_version": 1,
                "changes": {"title": {"mode": "override", "value": "Reviewed"}},
                "recipients": [{"kind": "marketplace_draft", "id": draft.id}],
            }],
        )
        assert preview["items"][0]["recipients"][0]["diff"][0]["effect"] == "local_only"
        records = __import__("services.common_product_content", fromlist=["_recipient_records"])._recipient_records
        recipient_checks = 0

        def change_recipient_during_apply(seller_id, current):
            nonlocal recipient_checks
            recipient_checks += 1
            if recipient_checks == 2:
                db.session.execute(
                    update(MarketplaceProductDraft)
                    .where(MarketplaceProductDraft.id == draft.id)
                    .values(version=2)
                )
                db.session.expire_all()
            return records(seller_id, current)

        with patch("services.common_product_content._recipient_records", side_effect=change_recipient_during_apply):
            with pytest.raises(CommonProductContentConflict):
                CommonProductContentService.apply(seller_id=seller.id, user_id=80, token=preview["preview_token"])

        db.session.expire_all()
        assert db.session.get(ImportedProduct, product.id).title == "Source title"
        assert db.session.get(MarketplaceProductDraft, draft.id).version == 1
        assert db.session.get(MarketplaceProductDraft, draft.id).content_json == '{"name":"Draft title"}'
        assert AgentChangeSnapshot.query.count() == 0


def test_malformed_recipient_and_raw_only_context_drift_are_safe(content_app):
    with content_app.app_context():
        seller = _seller()
        product = _imported(seller.id)
        marketplace = Marketplace(name="Ozon", code="ozon", adapter_code="ozon")
        db.session.add(marketplace)
        db.session.flush()
        account = SellerMarketplaceAccount(
            seller_id=seller.id,
            marketplace_id=marketplace.id,
            external_account_id="account-legacy",
            label="Legacy cabinet",
            is_active=True,
        )
        db.session.add(account)
        db.session.flush()
        draft = MarketplaceProductDraft(
            seller_id=seller.id,
            marketplace_id=marketplace.id,
            account_id=account.id,
            imported_product_id=product.id,
            offer_id="offer-legacy",
            status="needs_category",
            source_fact_hash="b" * 64,
            content_json="[]",
            media_json='"legacy scalar"',
        )
        db.session.add(draft)
        db.session.commit()
        preview = CommonProductContentService.preview(
            seller_id=seller.id,
            user_id=783,
            raw_items=[{
                "product_id": product.id,
                "expected_content_edit_version": 1,
                "changes": {"title": {"mode": "override", "value": "Reviewed"}},
                "recipients": [{"kind": "marketplace_draft", "id": draft.id}],
            }],
        )
        diff = preview["items"][0]["recipients"][0]["diff"][0]
        assert diff["current"] is None
        assert diff["matches"] is False

        # Keep recipient version/time unchanged; the exact raw context still seals the review.
        db.session.execute(
            update(MarketplaceProductDraft)
            .where(MarketplaceProductDraft.id == draft.id)
            .values(content_json="[ ]", media_json="null", updated_at=draft.updated_at)
        )
        db.session.expire_all()
        with pytest.raises(CommonProductContentConflict):
            CommonProductContentService.apply(
                seller_id=seller.id,
                user_id=783,
                token=preview["preview_token"],
            )


def test_nonfinite_and_oversized_characteristic_numbers_are_safe(content_app):
    rows = _characteristic_rows([{"name": "Размер", "value": float("nan")}, {
        "name": "Длина", "value": [float("inf")],
    }])
    assert "некорректное число источника" in rows[0]["value"]
    assert "некорректное число источника" in rows[1]["value"][0]
    with pytest.raises(CommonProductContentError, match="Число характеристики"):
        normalize_value("characteristics", [{"name": "Размер", "value": 10**500}])
    with pytest.raises(CommonProductContentError, match="Число характеристики"):
        normalize_value("characteristics", [{"name": "Размер", "value": [float("inf")]}])

    with content_app.app_context():
        seller = _seller()
        product = _imported(
            seller.id,
            characteristics='{"Длина":NaN}',
            original_data='{"characteristics":{"Длина":NaN}}',
        )
        db.session.commit()
        state = CommonProductContentService.read_many(seller_id=seller.id, product_ids=[product.id])[0]
        assert "некорректное число источника" in state["fields"]["characteristics"]["effective"][0]["value"]
        assert state["source"]["fingerprint"]


def test_manual_common_content_never_rewrites_native_flash_source_snapshot(content_app):
    with content_app.app_context():
        seller = _seller()
        product = _imported(seller.id)
        original_snapshot = product.original_data
        db.session.commit()

        _override(
            product,
            784,
            title={"mode": "override", "value": "Seller-authored title"},
            description={"mode": "override", "value": "Seller-authored description"},
            photos={"mode": "override", "value": ["https://images.example/source.jpg"]},
        )
        db.session.refresh(product)

        assert product.title == "Seller-authored title"
        assert product.description == "Seller-authored description"
        assert product.original_data == original_snapshot
        source_facts = _source_facts(json.loads(product.original_data))
        assert source_facts["title"] == "Source title"
        assert source_facts["description"] == "Source description"
        assert "Seller-authored title" not in json.dumps(source_facts, ensure_ascii=False)
        assert "Seller-authored description" not in json.dumps(source_facts, ensure_ascii=False)


def test_tolerant_characteristics_keep_unrecognized_nested_rows_for_reading():
    rows = _characteristic_rows(json.dumps([{"provider_only": "kept as source diagnostics"}]))
    assert rows[0]["name"] == "Источник · 1"
    assert rows[0]["value"] == {"provider_only": "kept as source diagnostics"}


@pytest.mark.parametrize("item", [
    {
        "product_id": (1 << 63),
        "expected_content_edit_version": 1,
        "changes": {"title": {"mode": "override", "value": "X"}},
        "recipients": [],
    },
    {
        "product_id": 1,
        "expected_content_edit_version": 1,
        "changes": {"title": {"mode": "override", "value": "X"}},
        "recipients": [{"kind": "marketplace_draft", "id": (1 << 63)}],
    },
])
def test_preview_rejects_ids_outside_signed_sqlite_range(item):
    with pytest.raises(CommonProductContentError, match="ID|Контекст"):
        CommonProductContentService.preview(seller_id=1, user_id=1, raw_items=[item])


def test_read_many_rejects_signed_sqlite_overflow_id():
    with pytest.raises(CommonProductContentError):
        CommonProductContentService.read_many(seller_id=1, product_ids=[1 << 63])


def test_non_editor_writer_guard_detects_same_value_override_race(content_app):
    with content_app.app_context():
        seller = _seller()
        product = _imported(seller.id, title="Same effective value")
        db.session.commit()
        observed = content_writer_guard_snapshot(product)
        active = json.dumps({
            "schema_version": 1,
            "fields": {
                "title": {
                    "value": "Same effective value",
                    "inherited_value": "Same effective value",
                    "inherited_origin": "source",
                    "edited_by_user_id": 999,
                    "edited_at": "2026-10-01T00:00:00",
                    "edit_version": 2,
                },
            },
        }, ensure_ascii=False, separators=(",", ":"))
        db.session.execute(
            update(ImportedProduct)
            .where(ImportedProduct.id == product.id)
            .values(content_overrides_json=active, content_edit_version=2)
        )
        db.session.commit()

        with pytest.raises(CommonProductContentConflict):
            guard_content_writer(product, expected=observed)

        db.session.expire_all()
        current = db.session.get(ImportedProduct, product.id)
        assert current.title == "Same effective value"
        assert current.content_edit_version == 2
        assert json.loads(current.content_overrides_json)["fields"]["title"]["value"] == "Same effective value"
