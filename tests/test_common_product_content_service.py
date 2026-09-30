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
    _characteristic_rows,
    normalize_value,
    refresh_from_source,
)


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


def test_tolerant_characteristics_keep_unrecognized_nested_rows_for_reading():
    rows = _characteristic_rows(json.dumps([{"provider_only": "kept as source diagnostics"}]))
    assert rows[0]["name"] == "Источник · 1"
    assert rows[0]["value"] == {"provider_only": "kept as source diagnostics"}
