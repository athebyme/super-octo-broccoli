"""Synthetic exact Ozon source comparisons and save receipts."""

from datetime import datetime, timedelta
from hashlib import sha256
import json

import pytest
from sqlalchemy import update

from tests import test_marketplace_publications as fixtures
from models import (
    ImportedProduct,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    Supplier,
    SupplierProduct,
    db,
)
from services.marketplace_drafts import MarketplaceDraftConflict
from services.ozon_bulk_repair import OzonBulkRepairService as Repair
from services.ozon_reference_service import OzonReferenceService
from services.supplier_color_observation_store import SupplierColorObservationStore
from services.supplier_service import (
    hydrate_missing_imported_observed_snapshot,
    supplier_catalog_config_digest,
)


@pytest.fixture
def source_case():
    case = fixtures.OzonBulkRepairServiceTest(methodName="runTest")
    case.setUp()
    case.app.secret_key = "synthetic-ozon-source-receipt-tests"
    try:
        now = datetime.utcnow()
        marker = {
            "observed_supplier_fields_version": 1,
            "observed_supplier_fields": {
                "colors": ["pink", "white"],
                "country": "Russia",
            },
            "colors": ["pink", "white"],
            "country": "Russia",
        }
        supplier = Supplier(name="Synthetic supplier", code="synthetic")
        db.session.add(supplier)
        db.session.flush()
        supplier_product = SupplierProduct(
            supplier=supplier,
            external_id="supplier-sku-1",
            content_revision=7,
            original_data_json=json.dumps(marker, ensure_ascii=False),
        )
        case.source.supplier_product = supplier_product
        db.session.add(supplier_product)
        db.session.flush()
        case.source.supplier_product_id = supplier_product.id
        case.source.supplier_id = supplier.id
        case.source.source_type = supplier.code
        case.source.external_id = "supplier-sku-1"
        case.source.supplier_content_revision = 7
        case.source.original_data = json.dumps(marker, ensure_ascii=False)
        db.session.add(case.source)
        definitions = {}
        dictionary_values = {
            "10096": [
                ("pink-id", "pink"),
                ("white-id", "white"),
                ("blue-id", "blue"),
                ("green-id", "green"),
                ("black-id", "black"),
            ],
            "4389": [("ru-id", "Russia"), ("jp-id", "Japan")],
        }
        for external_id, label, dictionary_id in (
            ("10096", "Цвет товара", "1494"),
            ("4389", "Страна-изготовитель", "1935"),
        ):
            definition = MarketplaceAttributeDefinition(
                marketplace_id=case.marketplace.id,
                product_type_id=case.product_type.id,
                external_attribute_id=external_id,
                name=label,
                data_type="String",
                is_required=False,
                dictionary_id=dictionary_id,
                max_value_count=2,
                is_collection=True,
                is_available=True,
                is_enabled=True,
                last_seen_at=now,
                values_synced_at=now,
                values_sync_status="success",
                values_snapshot_hash=f"source-dict-{external_id}",
                values_version=1,
                values_count=len(dictionary_values[external_id]),
            )
            db.session.add(definition)
            db.session.flush()
            definitions[external_id] = definition
            for external_value_id, value in dictionary_values[external_id]:
                db.session.add(MarketplaceAttributeValue(
                    marketplace_id=case.marketplace.id,
                    product_type_id=case.product_type.id,
                    attribute_id=definition.id,
                    external_value_id=external_value_id,
                    value=value,
                    value_normalized=OzonReferenceService.normalize_value(
                        value
                    ),
                    is_available=True,
                    last_seen_at=now,
                ))
        case.draft.attributes_json = json.dumps([
            {
                "attribute_id": "10096",
                "complex_id": "0",
                "values": [
                    {"dictionary_value_id": "blue-id", "value": "blue"},
                    {"dictionary_value_id": "green-id", "value": "green"},
                ],
            },
            {
                "attribute_id": "4389",
                "complex_id": "0",
                "values": [{"dictionary_value_id": "ru-id", "value": "Russia"}],
            },
        ], ensure_ascii=False)
        case.product_type.attributes_count += 2
        db.session.commit()
        yield case, definitions, supplier_product
    finally:
        case.tearDown()


def test_exact_supplier_fields_resolve_to_type_scoped_collection_ids(source_case):
    case, _definitions, _supplier_product = source_case

    comparisons, proofs, editable_ids = Repair._source_comparisons_for_draft(
        case.draft,
    )

    colors = comparisons["10096"]
    country = comparisons["4389"]
    assert colors["status"] == country["status"] == "confirmed"
    assert colors["literal_labels"] == ["pink", "white"]
    assert [cell["dictionary_value_id"] for cell in colors["resolved_values"]] == [
        "pink-id", "white-id",
    ]
    assert country["resolved_values"] == [{
        "value": "Russia",
        "dictionary_value_id": "ru-id",
    }]
    assert editable_ids == {"10096", "4389"}
    assert set(proofs) == {"10096", "4389"}
    assert colors["evidence_receipt"]
    assert len(colors["evidence_receipt"]) < 4096


def test_native_editor_exposes_filled_optional_fields_per_exact_row(source_case):
    case, _definitions, _supplier_product = source_case
    editor = Repair.editor_document(
        seller_id=case.seller.id,
        selection={
            "account_id": case.account.id,
            "draft_ids": [case.draft.id],
        },
    )
    row = editor["groups"][0]["rows"][0]
    color = next(field for field in row["attributes"] if field["external_id"] == "10096")
    country = next(field for field in row["attributes"] if field["external_id"] == "4389")

    assert row["source_comparisons"]["10096"]["status"] == "confirmed"
    assert row["source_comparisons"]["4389"]["status"] == "confirmed"
    assert color["editable"] and color["source_managed"]
    assert country["editable"] and country["source_managed"]
    assert color["required"] is False and country["required"] is False
    assert color["value"] == "blue\ngreen"
    assert color["dictionary_value_ids"] == ["blue-id", "green-id"]
    assert country["value"] == "Russia"
    assert country["dictionary_value_ids"] == ["ru-id"]
    assert "attribute:10096" not in {
        field["key"] for field in editor["bulk_fields"]
    }


def test_live_top_level_source_must_match_the_v1_marker(source_case):
    case, _definitions, supplier_product = source_case
    current = json.loads(supplier_product.original_data_json)
    current["country"] = "Japan"
    supplier_product.original_data_json = json.dumps(current, ensure_ascii=False)
    db.session.commit()

    evidence = Repair._source_supplier_evidence(case.draft)

    assert evidence["status"] == "unknown"
    assert evidence["reason"] == "source_snapshot_values_disagree"


def test_legacy_copied_snapshot_requires_exact_live_literal_parity(source_case):
    case, _definitions, supplier_product = source_case
    copied = json.loads(case.source.original_data)
    copied.pop("observed_supplier_fields_version")
    copied.pop("observed_supplier_fields")
    case.source.original_data = json.dumps(copied, ensure_ascii=False)
    db.session.commit()

    assert Repair._source_supplier_evidence(case.draft)["status"] == "confirmed"

    copied["colors"] = ["blue", "green"]
    case.source.original_data = json.dumps(copied, ensure_ascii=False)
    db.session.commit()
    evidence = Repair._source_supplier_evidence(case.draft)
    assert evidence["status"] == "unknown"
    assert evidence["reason"] == "source_snapshot_values_disagree"


def test_catalog_observation_supplies_missing_color_and_withdraws_changed_feed(
    source_case, tmp_path, monkeypatch,
):
    case, _definitions, supplier_product = source_case
    raw = json.dumps({"colors": ["pink", "white"], "country": "Russia"})
    case.source.original_data = raw
    supplier_product.original_data_json = raw
    db.session.commit()
    store = SupplierColorObservationStore(tmp_path / "color-proof.sqlite3")
    monkeypatch.setattr(
        "services.supplier_color_observation_store.configured_supplier_color_observation_store",
        lambda: store,
    )
    scope = supplier_catalog_config_digest(supplier_product.supplier)
    source_key = sha256(supplier_product.external_id.encode()).hexdigest()
    now = datetime.utcnow()
    observed_at = now - timedelta(hours=25)
    supplier_id = supplier_product.supplier_id
    claim = store.reserve(supplier_id, scope, now=observed_at)
    assert claim
    assert store.finish(supplier_id, claim, scope, {
        supplier_product.id: (supplier_product.content_revision, source_key, ("pink", "white")),
    }, observed_at=observed_at)

    evidence = Repair._source_supplier_evidence(case.draft)
    assert evidence["status"] == "confirmed"
    assert evidence["fields"] == {"colors": ["pink", "white"]}
    assert case.source.original_data == supplier_product.original_data_json == raw

    next_claim = store.reserve(supplier_id, scope, now=now)
    assert next_claim
    # The new CSV has no matching literal for this item. It replaces that
    # supplier's proof without writing either application snapshot.
    assert store.finish(supplier_id, next_claim, scope, {}, observed_at=now)
    missing = Repair._source_supplier_evidence(case.draft)
    assert missing["reason"] == "source_marker_unavailable"
    assert case.source.original_data == supplier_product.original_data_json == raw


@pytest.mark.parametrize("mismatch", ["literal", "configuration", "revision", "invalid_marker"])
def test_catalog_observation_cannot_cross_source_scope(source_case, tmp_path, monkeypatch, mismatch):
    case, _definitions, supplier_product = source_case
    raw = json.dumps({"colors": ["pink", "white"]})
    case.source.original_data = raw
    supplier_product.original_data_json = raw
    db.session.commit()
    store = SupplierColorObservationStore(tmp_path / "catalog-color.sqlite3")
    monkeypatch.setattr(
        "services.supplier_color_observation_store.configured_supplier_color_observation_store",
        lambda: store,
    )
    supplier = supplier_product.supplier
    scope = supplier_catalog_config_digest(supplier)
    now = datetime.utcnow()
    claim = store.reserve(supplier.id, scope, now=now)
    assert claim
    assert store.finish(supplier.id, claim, scope, {
        supplier_product.id: (
            supplier_product.content_revision,
            sha256(supplier_product.external_id.encode()).hexdigest(),
            ("pink", "white"),
        ),
    }, observed_at=now)
    if mismatch == "literal":
        case.source.original_data = json.dumps({"colors": ["black"]})
    elif mismatch == "configuration":
        supplier.csv_source_url = "https://different-source.invalid/catalog.csv"
    elif mismatch == "revision":
        supplier_product.content_revision += 1
        case.source.supplier_content_revision += 1
    else:
        supplier_product.original_data_json = json.dumps({
            "colors": ["pink", "white"],
            "observed_supplier_fields_version": 2,
        })
    db.session.commit()

    evidence = Repair._source_supplier_evidence(case.draft)
    assert evidence["status"] == "unknown"
    assert evidence["fields"] == {}


def test_unknown_source_does_not_block_ordinary_dictionary_edit(source_case):
    case, _definitions, supplier_product = source_case
    supplier_product.content_revision = 8
    db.session.commit()
    comparisons, proofs, _editable_ids = Repair._source_comparisons_for_draft(
        case.draft,
    )
    assert comparisons["10096"]["status"] == "unknown"
    assert comparisons["10096"]["resolved_values"] == []

    Repair._validate_source_proposals(
        draft=case.draft,
        values={"attribute:10096": "black\nwhite"},
        source_review_json="",
        comparisons=comparisons,
        proof_payloads=proofs,
    )


def test_hydration_keeps_newer_snapshot_after_compare_and_set_miss(source_case):
    case, _definitions, _supplier_product = source_case
    source = case.source
    source.original_data = '{}'
    db.session.commit()
    assert source.original_data == '{}' and source.supplier_content_revision == 7
    assert source.supplier_product.supplier is not None
    source.title = 'Unrelated pending seller edit'
    newer = json.dumps({'colors': ['black'], 'description': 'Newer confirmed snapshot'})

    # Change the database row while retaining the copy already loaded by
    # preparation. The snapshot comparison must use the stored row.
    with db.session.no_autoflush:
        db.session.execute(
            update(ImportedProduct)
            .filter_by(id=source.id)
            .values(original_data=newer, supplier_content_revision=8)
            .execution_options(synchronize_session=False)
        )
    assert source.original_data == '{}'
    assert hydrate_missing_imported_observed_snapshot(source) is False
    assert source.original_data == newer
    assert source.supplier_content_revision == 8
    assert source.title == 'Unrelated pending seller edit'


def test_successful_hydration_participates_in_the_existing_transaction(source_case):
    case, _definitions, _supplier_product = source_case
    source = case.source
    source.original_data = '{}'
    db.session.commit()

    assert hydrate_missing_imported_observed_snapshot(source) is True
    assert json.loads(source.original_data)['colors'] == ['pink', 'white']
    assert not db.session.is_modified(source)
    db.session.rollback()
    assert source.original_data == '{}'
    assert source.supplier_content_revision == 7


def test_exact_mapping_rejects_nearby_attribute_or_dictionary_ids(source_case):
    case, definitions, _supplier_product = source_case
    color = definitions["10096"]
    color.dictionary_id = "1935"
    db.session.commit()

    definition, reason = Repair._source_attribute_definition(
        draft=case.draft,
        external_id="10096",
    )
    assert definition is None
    assert reason == "field_contract_mismatch"
    assert Repair._source_attribute_definition(
        draft=case.draft,
        external_id="10097",
    ) == (None, "field_contract_mismatch")


def test_dictionary_candidate_overflow_is_not_treated_as_unique(source_case):
    case, definitions, _supplier_product = source_case
    definition = definitions["10096"]
    for index in range(50):
        db.session.add(MarketplaceAttributeValue(
            marketplace_id=case.marketplace.id,
            product_type_id=case.product_type.id,
            attribute_id=definition.id,
            external_value_id=f"pink-duplicate-{index}",
            value="pink",
            value_normalized=OzonReferenceService.normalize_value("pink"),
            is_available=True,
            last_seen_at=datetime.utcnow(),
        ))
    db.session.commit()

    comparisons, _proofs, _editable_ids = Repair._source_comparisons_for_draft(
        case.draft,
    )

    assert comparisons["10096"]["status"] == "confirmed"
    assert comparisons["10096"]["literal_labels"] == ["pink", "white"]
    assert comparisons["10096"]["resolved_values"] == []
    assert comparisons["10096"]["reason"] == "dictionary_value_ambiguous"


def test_matching_collection_set_uses_ack_false_and_mismatch_requires_ack(source_case):
    case, _definitions, _supplier_product = source_case
    comparisons, proofs, _editable_ids = Repair._source_comparisons_for_draft(
        case.draft,
    )
    proposal = "white\npink"
    proposal_id = ""
    proposal_sha = Repair._source_proposal_sha256(
        raw_value=proposal,
        dictionary_value_id=proposal_id,
    )
    values = {"attribute:10096": proposal}
    receipt = comparisons["10096"]["evidence_receipt"]

    # Existing current/source drift is informational until a field is edited.
    Repair._validate_source_proposals(
        draft=case.draft,
        values={},
        source_review_json="",
        comparisons=comparisons,
        proof_payloads=proofs,
    )

    Repair._validate_source_proposals(
        draft=case.draft,
        values=values,
        source_review_json=json.dumps({
            "10096": {
                "receipt": receipt,
                "ack": False,
                "proposal_sha256": proposal_sha,
            },
        }),
        comparisons=comparisons,
        proof_payloads=proofs,
    )

    mismatch = "black\nwhite"
    mismatch_sha = Repair._source_proposal_sha256(
        raw_value=mismatch,
        dictionary_value_id="",
    )
    mismatch_values = {"attribute:10096": mismatch}
    with pytest.raises(MarketplaceDraftConflict):
        Repair._validate_source_proposals(
            draft=case.draft,
            values=mismatch_values,
            source_review_json="",
            comparisons=comparisons,
            proof_payloads=proofs,
        )
    Repair._validate_source_proposals(
        draft=case.draft,
        values=mismatch_values,
        source_review_json=json.dumps({
            "10096": {
                "receipt": receipt,
                "ack": True,
                "proposal_sha256": mismatch_sha,
            },
        }),
        comparisons=comparisons,
        proof_payloads=proofs,
    )


def test_proposal_receipt_is_bound_to_live_source_and_current_draft(source_case):
    case, _definitions, supplier_product = source_case
    comparisons, proofs, _editable_ids = Repair._source_comparisons_for_draft(
        case.draft,
    )
    decision = json.dumps({
        "10096": {
            "receipt": comparisons["10096"]["evidence_receipt"],
            "ack": True,
            "proposal_sha256": Repair._source_proposal_sha256(
                raw_value="black\nwhite",
                dictionary_value_id="",
            ),
        },
    })
    values = {"attribute:10096": "black\nwhite"}

    supplier_product.content_revision = 8
    db.session.commit()
    with pytest.raises(MarketplaceDraftConflict):
        Repair._validate_source_proposals(
            draft=case.draft,
            values=values,
            source_review_json=decision,
            comparisons=Repair._source_comparisons_for_draft(case.draft)[0],
            proof_payloads=Repair._source_comparisons_for_draft(case.draft)[1],
        )
