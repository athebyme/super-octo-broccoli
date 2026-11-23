"""Local-only candidate safety and fake-client tests for category preparation."""

from datetime import datetime, timedelta
from dataclasses import replace
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
import pytest

from models import (
    BackgroundJob,
    ImportedProduct,
    Marketplace,
    MarketplaceCategoryMapping,
    MarketplaceListing,
    MarketplaceListingLinkEvent,
    MarketplaceOperation,
    MarketplaceProductDraft,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    Seller,
    SellerMarketplaceAccount,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from services.marketplace_fact_pack import MarketplaceFactPackBuilder
from services.marketplace_drafts import MarketplaceDraftService
from services.ozon_bulk_upload import OzonBulkUploadService
from services.ozon_upload_queue import OzonUploadQueueService
from services.openai_decisions import DecisionAnswer, DecisionOutcome
from services.openai_decisions_cache import OpenAIDecisionsCache
from services.openai_decisions_pipeline import OpenAIDecisionsPipeline
from services.ozon_prepare_category_decisions import (
    build_ozon_category_tree_index,
    build_ozon_prepare_category_candidate,
    same_ozon_prepare_category_scope,
    prepare_ozon_category_window,
    _source_observation,
    selected_product_type_id,
)


NOW = datetime(2026, 11, 23, 12, 0)
SOURCE_TITLE = "Складной текстильный органайзер для дома"
SOURCE_PATH = "Дом и хранение > Органайзеры"
OBSERVED_SOURCE_CATEGORY = "Локальная категория поставщика без пути Ozon"
OBSERVED_MAPPING_ALGORITHM = "ozon-exact-linked-category-consensus-v3"


class FakeDecisionsClient:
    def __init__(self, confidence=0.97):
        self.calls = []
        self.confidence = confidence

    def decide(self, *, input_json, questions):
        questions = tuple(questions)
        self.calls.append((input_json, questions))
        return DecisionOutcome(
            "success",
            answers=tuple(
                DecisionAnswer(
                    question.name,
                    "answered",
                    question.choices[0].value,
                    self.confidence,
                )
                for question in questions
            ),
        )


@pytest.fixture
def case():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="category-helper-local-test",
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    context = app.app_context()
    context.push()
    db.create_all()
    supplier = Supplier(name="Synthetic source", code="synthetic-source")
    user = User(
        username="category-test-seller",
        email="category-test-seller@example.test",
        password_hash="local-only",
    )
    db.session.add_all([supplier, user])
    db.session.flush()
    seller = Seller(user_id=user.id, company_name="Synthetic seller")
    marketplace = Marketplace(
        name="Ozon",
        code="ozon",
        is_active=True,
        categories_synced_at=NOW,
        categories_sync_status="success",
        categories_version=7,
        categories_snapshot_hash="tree-r77-a" * 6 + "1234",
    )
    db.session.add_all([seller, marketplace])
    db.session.flush()
    account = SellerMarketplaceAccount(
        seller_id=seller.id,
        marketplace_id=marketplace.id,
        external_account_id="local-account",
        label="Local Ozon account",
        is_active=True,
    )
    source_raw = {
        "title": SOURCE_TITLE,
        "category": SOURCE_PATH,
        "all_categories": [SOURCE_PATH],
        "vendor_code": "SKU-001",
    }
    raw_json = json.dumps(source_raw, ensure_ascii=False, sort_keys=True)
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="supplier-product-1",
        vendor_code="SKU-001",
        title=SOURCE_TITLE,
        category=SOURCE_PATH,
        content_revision=4,
        original_data_json=raw_json,
    )
    db.session.add_all([account, supplier_product])
    db.session.flush()
    product = ImportedProduct(
        seller_id=seller.id,
        supplier_id=supplier.id,
        supplier_product_id=supplier_product.id,
        supplier_content_revision=4,
        source_type=supplier.code,
        external_id=supplier_product.external_id,
        external_vendor_code=supplier_product.vendor_code,
        title=SOURCE_TITLE,
        category=SOURCE_PATH,
        original_data=raw_json,
        import_status="pending",
    )
    db.session.add(product)
    db.session.flush()
    fact_pack = MarketplaceFactPackBuilder.build(product)
    category_root = MarketplaceTaxonomyCategory(
        marketplace_id=marketplace.id,
        external_category_id="10",
        name="Дом и хранение",
        full_path="Дом и хранение",
        depth=0,
        is_available=True,
        is_disabled_upstream=False,
    )
    category_leaf = MarketplaceTaxonomyCategory(
        marketplace_id=marketplace.id,
        external_category_id="11",
        name="Органайзеры",
        full_path="Дом и хранение / Органайзеры",
        depth=1,
        is_available=True,
        is_disabled_upstream=False,
    )
    db.session.add_all([category_root, category_leaf])
    db.session.flush()
    type_a = MarketplaceProductType(
        marketplace_id=marketplace.id,
        category_id=category_leaf.id,
        external_type_id="7001",
        name="Органайзер для дома",
        is_available=True,
        is_seller_selectable=True,
        is_disabled_upstream=False,
    )
    type_b = MarketplaceProductType(
        marketplace_id=marketplace.id,
        category_id=category_leaf.id,
        external_type_id="7002",
        name="Сумка для хранения",
        is_available=True,
        is_seller_selectable=True,
        is_disabled_upstream=False,
    )
    db.session.add_all([type_a, type_b])
    job = BackgroundJob(
        job_uid="category-prepare-test-job",
        seller_id=seller.id,
        job_type="ozon_bulk_upload",
        status="running",
        total=1,
    )
    db.session.add(job)
    db.session.flush()
    run = OzonBulkUploadRun(
        job_id=job.id,
        seller_id=seller.id,
        account_id=account.id,
        mode="source_prepare",
        request_key_hash="1" * 64,
        request_fingerprint="2" * 64,
        state="active",
        next_due_at=NOW,
        lease_token="3" * 32,
        lease_until=NOW + timedelta(minutes=2),
    )
    db.session.add(run)
    db.session.flush()
    draft = MarketplaceProductDraft(
        seller_id=seller.id,
        marketplace_id=marketplace.id,
        account_id=account.id,
        imported_product_id=product.id,
        supplier_product_id=supplier_product.id,
        offer_id="SKU-001",
        status="needs_category",
        source_fact_hash=fact_pack["fact_hash"],
        source_facts_json="{}",
        provenance_json="{}",
        content_json="{}",
        attributes_json="[]",
        complex_attributes_json="[]",
        attribute_removals_json="[]",
        media_json="{}",
        dimensions_json="{}",
        barcodes_json="[]",
        commercial_json="{}",
        validation_status="never_validated",
        validation_result_json="{}",
        version=1,
    )
    db.session.add(draft)
    db.session.flush()
    item = OzonBulkUploadItem(
        run_id=run.id,
        ordinal=1,
        imported_product_id=product.id,
        draft_id=draft.id,
        prepared_version=draft.version,
        phase="preparing",
        next_due_at=NOW,
        title_snapshot=SOURCE_TITLE,
    )
    db.session.add(item)
    db.session.commit()
    value = SimpleNamespace(
        app=app,
        context=context,
        now=NOW,
        user=user,
        seller=seller,
        supplier=supplier,
        supplier_product=supplier_product,
        marketplace=marketplace,
        account=account,
        product=product,
        draft=draft,
        run=run,
        item=item,
        category=category_leaf,
        type_a=type_a,
        type_b=type_b,
    )
    try:
        yield value
    finally:
        db.session.remove()
        db.drop_all()
        context.pop()


def build_candidate(case, *, prior_draft_id=None, item_draft_id=None,
                    item_prepared_version=None, now=None):
    source_snapshot = json.loads(case.product.original_data)
    tree_index = build_ozon_category_tree_index(
        case.marketplace,
        [source_snapshot.get("category")],
        now=now or case.now,
    )
    if tree_index is None:
        return None
    return build_ozon_prepare_category_candidate(
        run=case.run,
        item=case.item,
        account=case.account,
        product=case.product,
        draft=case.draft,
        tree_index=tree_index,
        prior_draft_id_before_create=prior_draft_id,
        item_draft_id_before_create=item_draft_id,
        item_prepared_version_before_create=item_prepared_version,
        now=now or case.now,
    )


def add_sibling(case, ordinal, *, due_at=NOW, supplier=None, vendor_code=None):
    supplier = supplier or case.supplier
    title = f"Organizer for fabric storage {ordinal}"
    raw = json.dumps({
        "title": title,
        "category": SOURCE_PATH,
        "all_categories": [SOURCE_PATH],
    }, ensure_ascii=False, sort_keys=True)
    source = SupplierProduct(
        supplier_id=supplier.id,
        external_id=f"supplier-product-{ordinal}",
        vendor_code=vendor_code or f"SKU-{ordinal:03d}",
        title=title,
        category=SOURCE_PATH,
        content_revision=4,
        original_data_json=raw,
    )
    db.session.add(source)
    db.session.flush()
    product = ImportedProduct(
        seller_id=case.seller.id,
        supplier_id=supplier.id,
        supplier_product_id=source.id,
        supplier_content_revision=4,
        source_type=supplier.code,
        external_id=source.external_id,
        external_vendor_code=source.vendor_code,
        title=title,
        category=SOURCE_PATH,
        original_data=raw,
        import_status="pending",
    )
    db.session.add(product)
    db.session.flush()
    item = OzonBulkUploadItem(
        run_id=case.run.id,
        ordinal=ordinal,
        imported_product_id=product.id,
        phase="pending",
        next_due_at=due_at,
        title_snapshot=title,
    )
    db.session.add(item)
    db.session.commit()
    return item, product


def set_target_source_category(case, category):
    source_snapshot = json.loads(case.supplier_product.original_data_json)
    source_snapshot["category"] = category
    source_snapshot["all_categories"] = [category]
    raw = json.dumps(source_snapshot, ensure_ascii=False, sort_keys=True)
    case.supplier_product.category = category
    case.supplier_product.original_data_json = raw
    case.product.category = category
    case.product.original_data = raw
    case.draft.source_fact_hash = MarketplaceFactPackBuilder.build(
        case.product,
    )["fact_hash"]
    db.session.commit()
    case.item.prepared_version = case.draft.version
    db.session.commit()


def add_exact_linked_observation(
    case,
    ordinal,
    product_type,
    *,
    source_category=OBSERVED_SOURCE_CATEGORY,
    supplier=None,
    account=None,
    link_source="exact_source_identity",
    link_action="auto_link",
    linked_by_user_id=None,
    listing_changes=None,
    create_event=True,
):
    """Add one fully current local Ozon listing and its exact link lineage."""
    supplier = supplier or case.supplier
    account = account or case.account
    title = f"Observed organizer {ordinal}"
    vendor_code = f"OBS-SKU-{ordinal:03d}"
    external_id = f"observed-supplier-product-{ordinal}"
    source_raw = json.dumps({
        "title": title,
        "category": source_category,
        "all_categories": [source_category],
        "vendor_code": vendor_code,
    }, ensure_ascii=False, sort_keys=True)
    source = SupplierProduct(
        supplier_id=supplier.id,
        external_id=external_id,
        vendor_code=vendor_code,
        title=title,
        category=source_category,
        content_revision=4,
        original_data_json=source_raw,
    )
    db.session.add(source)
    db.session.flush()
    product = ImportedProduct(
        seller_id=case.seller.id,
        supplier_id=supplier.id,
        supplier_product_id=source.id,
        supplier_content_revision=4,
        source_type=supplier.code,
        external_id=external_id,
        external_vendor_code=vendor_code,
        title=title,
        category=source_category,
        original_data=source_raw,
        import_status="pending",
    )
    db.session.add(product)
    db.session.flush()

    product_type.attributes_synced_at = case.now
    product_type.attributes_sync_status = "success"
    product_type.attributes_schema_hash = (
        product_type.attributes_schema_hash or f"schema-{product_type.id}"
    )
    product_type.last_seen_at = case.now
    product_type.is_available = True
    product_type.is_seller_selectable = True
    product_type.is_disabled_upstream = False
    product_type.category.is_available = True
    product_type.category.is_disabled_upstream = False
    listing = MarketplaceListing(
        seller_id=case.seller.id,
        marketplace_id=case.marketplace.id,
        account_id=account.id,
        imported_product_id=product.id,
        product_type_id=product_type.id,
        offer_id=vendor_code,
        external_product_id=f"observed-ozon-product-{ordinal}",
        external_category_id=product_type.category.external_category_id,
        external_type_id=product_type.external_type_id,
        title=title,
        normalized_status="active",
        link_status="linked",
        link_source=link_source,
        link_evidence_json=json.dumps({
            "supplier_product_id": source.id,
            "source_category": source_category,
            "supplier_content_revision": 4,
        }, ensure_ascii=False, sort_keys=True),
        link_version=1,
        linked_at=case.now,
        linked_by_user_id=linked_by_user_id,
        last_seen_at=case.now,
        info_synced_at=case.now,
        attributes_synced_at=case.now,
        is_available=True,
        is_archived=False,
        sync_fingerprint=f"{ordinal:064x}"[-64:],
    )
    for field, value in (listing_changes or {}).items():
        setattr(listing, field, value)
    db.session.add(listing)
    db.session.flush()
    event = None
    if create_event:
        event = MarketplaceListingLinkEvent(
            seller_id=case.seller.id,
            marketplace_id=case.marketplace.id,
            account_id=account.id,
            listing_id=listing.id,
            previous_imported_product_id=None,
            imported_product_id=product.id,
            action="auto_link",
            source=link_source,
            evidence_json=listing.link_evidence_json,
            actor_user_id=linked_by_user_id,
            link_version=listing.link_version,
            created_at=case.now,
        )
        event.action = link_action
        db.session.add(event)
    db.session.flush()
    return SimpleNamespace(
        source=source,
        product=product,
        listing=listing,
        event=event,
    )


def observed_type_pool(case, *, extra_count=0):
    """Return fresh seller-selectable types in the fixture's one official node."""
    product_types = [case.type_a, case.type_b]
    for index in range(extra_count):
        product_type = MarketplaceProductType(
            marketplace_id=case.marketplace.id,
            category_id=case.category.id,
            external_type_id=str(7100 + index),
            name=f"Дополнительный тип {index + 1}",
            is_available=True,
            is_seller_selectable=True,
            is_disabled_upstream=False,
            last_seen_at=case.now,
            attributes_synced_at=case.now,
            attributes_sync_status="success",
            attributes_schema_hash=f"schema-extra-{index}",
        )
        db.session.add(product_type)
        product_types.append(product_type)
    db.session.flush()
    for product_type in product_types:
        product_type.attributes_synced_at = case.now
        product_type.attributes_sync_status = "success"
        product_type.attributes_schema_hash = (
            product_type.attributes_schema_hash or f"schema-{product_type.id}"
        )
        product_type.last_seen_at = case.now
        product_type.is_available = True
        product_type.is_seller_selectable = True
        product_type.is_disabled_upstream = False
        product_type.category.is_available = True
        product_type.category.is_disabled_upstream = False
        product_type.category.last_seen_at = case.now
    return product_types


def add_observed_evidence(case, *, per_type=2, category=OBSERVED_SOURCE_CATEGORY,
                          supplier=None, account=None, extra_count=0,
                          modifications=None):
    """Create exact link evidence, with at least two distinct source rows per type."""
    supplier = supplier or case.supplier
    product_types = observed_type_pool(case, extra_count=extra_count)
    evidence = []
    ordinal = 1
    for product_type in product_types[:2]:
        for _ in range(per_type):
            changes = (modifications or {}).get(ordinal, {})
            evidence.append(add_exact_linked_observation(
                case,
                ordinal,
                product_type,
                source_category=category,
                supplier=changes.get("supplier", supplier),
                account=changes.get("account", account),
                link_source=changes.get("link_source", "exact_source_identity"),
                link_action=changes.get("link_action", "auto_link"),
                linked_by_user_id=changes.get("linked_by_user_id"),
                listing_changes=changes.get("listing", {}),
                create_event=changes.get("create_event", True),
            ))
            ordinal += 1
    db.session.commit()
    return product_types, evidence


def add_stale_supplier_mapping(
    case,
    reason,
    *,
    mapping_source="deterministic",
    mapping_status="stale",
    algorithm=OBSERVED_MAPPING_ALGORITHM,
    corrected_by_user_id=None,
):
    mapping = MarketplaceCategoryMapping(
        seller_id=case.seller.id,
        marketplace_id=case.marketplace.id,
        supplier_id=case.supplier.id,
        product_type_id=case.type_a.id,
        scope_key=f"supplier:{case.supplier.id}",
        source_type=case.supplier.code,
        source_category=OBSERVED_SOURCE_CATEGORY,
        source_category_normalized=MarketplaceDraftService._normalized_text(
            OBSERVED_SOURCE_CATEGORY,
        ),
        external_category_id=case.category.external_category_id,
        external_type_id=case.type_a.external_type_id,
        mapping_source=mapping_source,
        mapping_status=mapping_status,
        confidence=0.99,
        evidence_json=json.dumps({
            "algorithm": algorithm,
            "state": "stale" if mapping_status == "stale" else mapping_status,
            "reason": reason,
        }, ensure_ascii=False, sort_keys=True),
        corrected_by_user_id=corrected_by_user_id,
    )
    db.session.add(mapping)
    db.session.commit()
    return mapping


def test_exact_supplier_path_builds_reusable_scoped_candidate_without_writes(case):
    before_version = case.draft.version
    before_mapping_count = MarketplaceCategoryMapping.query.count()
    tree_index = build_ozon_category_tree_index(
        case.marketplace,
        [SOURCE_PATH],
        now=case.now,
    )

    candidate = build_ozon_prepare_category_candidate(
        run=case.run,
        item=case.item,
        account=case.account,
        product=case.product,
        draft=case.draft,
        tree_index=tree_index,
        prior_draft_id_before_create=None,
        item_draft_id_before_create=None,
        item_prepared_version_before_create=None,
        now=case.now,
    )

    assert candidate is not None
    assert candidate.match_kind == "full_path"
    assert candidate.context.marketplace == "ozon"
    assert candidate.context.product_key == (
        f"seller-{case.seller.id}-account-{case.account.id}-product-{case.product.id}"
    )
    assert candidate.context.source_fields == {
        "name": SOURCE_TITLE,
        "category": SOURCE_PATH,
    }
    assert [choice.type_id for choice in candidate.context.choices] == ["7001", "7002"]
    assert len(candidate.candidate_signature) == 64
    assert len(candidate.scope_signature) == 64
    assert case.draft.version == before_version
    assert MarketplaceCategoryMapping.query.count() == before_mapping_count
    assert not db.session.is_modified(case.draft)


@pytest.mark.parametrize("category_projection", [None, "Different projection label"])
def test_exact_literal_path_survives_missing_or_stale_category_projection(
    case,
    category_projection,
):
    case.product.category = category_projection
    db.session.commit()

    assert _source_observation(case.product) is not None
    candidate = build_candidate(case)

    assert candidate is not None
    assert candidate.match_kind == "full_path"
    assert candidate.context.source_fields["category"] == SOURCE_PATH


def test_unmatched_source_uses_only_observed_types_under_one_fresh_node(
    case,
    tmp_path,
):
    set_target_source_category(case, OBSERVED_SOURCE_CATEGORY)
    types, evidence = add_observed_evidence(
        case,
        extra_count=8,
        modifications={
            1: {"link_source": "exact_offer_identity"},
            4: {
                "link_source": "seller_confirmation",
                "link_action": "manual_link",
                "linked_by_user_id": case.user.id,
            },
        },
    )
    before_mapping_count = MarketplaceCategoryMapping.query.count()
    before_version = case.draft.version
    before_content = case.draft.content_json

    candidate = build_candidate(case)

    assert candidate is not None
    assert len(types) == 10
    assert len(types) > 8
    assert [choice.type_id for choice in candidate.context.choices] == [
        case.type_a.external_type_id,
        case.type_b.external_type_id,
    ]
    assert all(
        choice.category_id == case.category.external_category_id
        for choice in candidate.context.choices
    )
    assert candidate.context.source_fields == {
        "name": SOURCE_TITLE,
        "category": OBSERVED_SOURCE_CATEGORY,
    }

    client = FakeDecisionsClient()
    pipeline = OpenAIDecisionsPipeline(
        OpenAIDecisionsCache(
            str(tmp_path / "private" / "decisions.sqlite3"),
            max_calls_per_day=5,
        ),
        client,
    )
    outcome = pipeline.run_category_phase([candidate.context], now=case.now)
    assert outcome.results[0].status == "selected"
    assert selected_product_type_id(candidate, outcome.results[0]) == case.type_a.id
    assert len(client.calls) == 1
    prompt_payload = json.dumps(
        client.calls[0][0], ensure_ascii=False, sort_keys=True,
    ) + "\n" + "\n".join(
        json.dumps({
            "name": question.name,
            "instructions": question.instructions,
            "choices": [
                {"value": choice.value, "description": choice.description}
                for choice in question.choices
            ],
        }, ensure_ascii=False, sort_keys=True)
        for question in client.calls[0][1]
    )
    assert OBSERVED_SOURCE_CATEGORY in prompt_payload
    assert candidate.context.product_key not in prompt_payload
    for row in evidence:
        assert row.source.external_id not in prompt_payload
        assert row.source.vendor_code not in prompt_payload
        assert row.listing.external_product_id not in prompt_payload
    assert "supplier_product_id" not in prompt_payload
    assert MarketplaceCategoryMapping.query.count() == before_mapping_count
    assert MarketplaceOperation.query.count() == 0
    assert case.draft.version == before_version
    assert case.draft.content_json == before_content
    assert case.draft.product_type_id is None


@pytest.mark.parametrize("fault", [
    "other_account",
    "split_accounts",
    "other_supplier",
    "split_suppliers",
    "stale_listing",
    "stale_type_schema",
    "unknown_type",
    "missing_exact_event",
    "manual_unconfirmed_link",
    "mismatching_official_pair",
    "mixed_nodes",
    "source_revision_drift",
    "truncated_scope",
])
def test_unmatched_source_alias_fails_closed_for_untrusted_evidence(
    case,
    fault,
    monkeypatch,
):
    set_target_source_category(case, OBSERVED_SOURCE_CATEGORY)
    account2 = None
    supplier2 = None
    modifications = {}
    evidence_supplier = case.supplier
    if fault in {"other_account", "split_accounts"}:
        account2 = SellerMarketplaceAccount(
            seller_id=case.seller.id,
            marketplace_id=case.marketplace.id,
            external_account_id=f"alias-evidence-{fault}",
            label="Other synthetic Ozon account",
            is_active=True,
        )
        db.session.add(account2)
        db.session.flush()
        if fault == "other_account":
            evidence_account = account2
        else:
            evidence_account = None
            modifications = {
                2: {"account": account2},
                4: {"account": account2},
            }
    else:
        evidence_account = None
    if fault in {"other_supplier", "split_suppliers"}:
        supplier2 = Supplier(
            name=f"Other alias evidence supplier {fault}",
            code=f"other-alias-{fault}",
        )
        db.session.add(supplier2)
        db.session.flush()
        if fault == "other_supplier":
            evidence_supplier = supplier2
        else:
            modifications.update({
                2: {**modifications.get(2, {}), "supplier": supplier2},
                4: {**modifications.get(4, {}), "supplier": supplier2},
            })
    if fault == "stale_listing":
        modifications[1] = {
            "listing": {"last_seen_at": case.now - timedelta(days=3)},
        }
    elif fault == "missing_exact_event":
        modifications[1] = {"create_event": False}
    elif fault == "manual_unconfirmed_link":
        modifications[1] = {"link_source": "seller_confirmation"}

    _types, evidence = add_observed_evidence(
        case,
        supplier=evidence_supplier,
        account=evidence_account,
        modifications=modifications,
    )
    if fault == "stale_type_schema":
        evidence[0].listing.product_type.attributes_synced_at = (
            case.now - timedelta(days=3)
        )
    elif fault == "unknown_type":
        evidence[0].listing.product_type_id = None
        evidence[0].listing.external_type_id = None
    elif fault == "mismatching_official_pair":
        evidence[0].listing.external_type_id = "999999"
    elif fault == "source_revision_drift":
        evidence[0].product.supplier_content_revision = 3
    elif fault == "truncated_scope":
        monkeypatch.setattr(
            "services.ozon_prepare_category_decisions._MAX_OBSERVED_SCOPE_ROWS",
            3,
        )
    elif fault == "mixed_nodes":
        other_category = MarketplaceTaxonomyCategory(
            marketplace_id=case.marketplace.id,
            external_category_id="12",
            name="Other category",
            full_path="Other category / Other leaf",
            depth=1,
            is_available=True,
            is_disabled_upstream=False,
            last_seen_at=case.now,
        )
        db.session.add(other_category)
        db.session.flush()
        other_type = MarketplaceProductType(
            marketplace_id=case.marketplace.id,
            category_id=other_category.id,
            external_type_id="7999",
            name="Other observed type",
            is_available=True,
            is_seller_selectable=True,
            is_disabled_upstream=False,
            last_seen_at=case.now,
            attributes_synced_at=case.now,
            attributes_sync_status="success",
            attributes_schema_hash="schema-other-node",
        )
        db.session.add(other_type)
        db.session.flush()
        for row in evidence[2:]:
            row.listing.product_type_id = other_type.id
            row.listing.external_category_id = other_category.external_category_id
            row.listing.external_type_id = other_type.external_type_id
            row.event.evidence_json = row.listing.link_evidence_json
    db.session.commit()

    assert build_candidate(case) is None


def test_target_supplier_product_copy_cannot_count_as_an_observed_peer(case):
    set_target_source_category(case, OBSERVED_SOURCE_CATEGORY)
    add_exact_linked_observation(case, 1, case.type_a)
    add_exact_linked_observation(case, 2, case.type_b)
    add_exact_linked_observation(case, 3, case.type_b)

    # This second seller copy points at the target's exact SupplierProduct.
    # It cannot turn one true type-A peer into two independent observations.
    duplicate = ImportedProduct(
        seller_id=case.seller.id,
        supplier_id=case.supplier.id,
        supplier_product_id=case.supplier_product.id,
        supplier_content_revision=case.supplier_product.content_revision,
        source_type=case.supplier.code,
        external_id=case.supplier_product.external_id,
        external_vendor_code=case.supplier_product.vendor_code,
        title=case.product.title,
        category=OBSERVED_SOURCE_CATEGORY,
        original_data=case.product.original_data,
        import_status="pending",
    )
    db.session.add(duplicate)
    db.session.flush()
    duplicate_listing = MarketplaceListing(
        seller_id=case.seller.id,
        marketplace_id=case.marketplace.id,
        account_id=case.account.id,
        imported_product_id=duplicate.id,
        product_type_id=case.type_a.id,
        offer_id="OBS-TARGET-COPY",
        external_product_id="observed-target-copy",
        external_category_id=case.category.external_category_id,
        external_type_id=case.type_a.external_type_id,
        title=case.product.title,
        normalized_status="active",
        link_status="linked",
        link_source="exact_source_identity",
        link_evidence_json=json.dumps({
            "supplier_product_id": case.supplier_product.id,
            "source_category": OBSERVED_SOURCE_CATEGORY,
            "supplier_content_revision": case.supplier_product.content_revision,
        }, ensure_ascii=False, sort_keys=True),
        link_version=1,
        linked_at=case.now,
        last_seen_at=case.now,
        info_synced_at=case.now,
        attributes_synced_at=case.now,
        is_available=True,
        is_archived=False,
        sync_fingerprint="d" * 64,
    )
    db.session.add(duplicate_listing)
    db.session.flush()
    db.session.add(MarketplaceListingLinkEvent(
        seller_id=case.seller.id,
        marketplace_id=case.marketplace.id,
        account_id=case.account.id,
        listing_id=duplicate_listing.id,
        imported_product_id=duplicate.id,
        action="auto_link",
        source="exact_source_identity",
        evidence_json=duplicate_listing.link_evidence_json,
        link_version=duplicate_listing.link_version,
        created_at=case.now,
    ))
    db.session.commit()

    assert build_candidate(case) is None


@pytest.mark.parametrize("reason", [
    "conflicting_product_types",
    "heterogeneous_source_category",
])
def test_alias_fallback_can_replace_only_stale_v3_supplier_conflict(case, reason):
    set_target_source_category(case, OBSERVED_SOURCE_CATEGORY)
    add_observed_evidence(case)
    mapping = add_stale_supplier_mapping(case, reason)
    before = (
        mapping.mapping_source,
        mapping.mapping_status,
        mapping.evidence_json,
        MarketplaceCategoryMapping.query.count(),
    )

    candidate = build_candidate(case)

    assert candidate is not None
    assert [choice.type_id for choice in candidate.context.choices] == [
        case.type_a.external_type_id,
        case.type_b.external_type_id,
    ]
    db.session.refresh(mapping)
    assert (
        mapping.mapping_source,
        mapping.mapping_status,
        mapping.evidence_json,
        MarketplaceCategoryMapping.query.count(),
    ) == before


@pytest.mark.parametrize("mapping_kwargs", [
    {"mapping_source": "manual", "mapping_status": "stale"},
    {"mapping_source": "deterministic", "mapping_status": "active"},
    {"mapping_source": "deterministic", "mapping_status": "rejected"},
    {"mapping_source": "deterministic", "mapping_status": "stale",
     "reason": "not_enough_exact_listings"},
    {"mapping_source": "deterministic", "mapping_status": "stale",
     "algorithm": "ozon-exact-linked-category-consensus-v2"},
    {"mapping_source": "deterministic", "mapping_status": "stale",
     "corrected_by_user_id": "seller"},
])
def test_alias_fallback_does_not_override_other_mapping_states(case, mapping_kwargs):
    set_target_source_category(case, OBSERVED_SOURCE_CATEGORY)
    add_observed_evidence(case)
    options = dict(mapping_kwargs)
    reason = options.pop("reason", "conflicting_product_types")
    corrected_by_user_id = options.pop("corrected_by_user_id", None)
    if corrected_by_user_id == "seller":
        corrected_by_user_id = case.user.id
    add_stale_supplier_mapping(
        case,
        reason,
        corrected_by_user_id=corrected_by_user_id,
        **options,
    )

    assert build_candidate(case) is None


def test_observed_evidence_change_invalidates_category_candidate_scope(
    case,
    tmp_path,
):
    set_target_source_category(case, OBSERVED_SOURCE_CATEGORY)
    _types, evidence = add_observed_evidence(case)
    before = build_candidate(case)
    assert before is not None
    client = FakeDecisionsClient()
    pipeline = OpenAIDecisionsPipeline(
        OpenAIDecisionsCache(
            str(tmp_path / "private" / "decisions.sqlite3"),
            max_calls_per_day=5,
        ),
        client,
    )
    response = pipeline.run_category_phase([before.context], now=case.now)
    assert response.results[0].status == "selected"

    # A new matching current link event still represents exact evidence, but
    # changes the bounded observation set signature after the model response.
    listing = evidence[0].listing
    listing.link_version += 1
    db.session.add(MarketplaceListingLinkEvent(
        seller_id=case.seller.id,
        marketplace_id=case.marketplace.id,
        account_id=case.account.id,
        listing_id=listing.id,
        previous_imported_product_id=None,
        imported_product_id=evidence[0].product.id,
        action="auto_link",
        source="exact_source_identity",
        evidence_json=listing.link_evidence_json,
        actor_user_id=None,
        link_version=listing.link_version,
        created_at=case.now,
    ))
    db.session.commit()

    after = build_candidate(case)
    assert after is not None
    assert before.scope_signature != after.scope_signature
    assert before.candidate_signature != after.candidate_signature
    assert not same_ozon_prepare_category_scope(before, after)


def test_fake_pipeline_selects_exact_local_type_only_above_review_threshold(case, tmp_path):
    candidate = build_candidate(case)
    client = FakeDecisionsClient()
    cache_path = tmp_path / "private" / "decisions.sqlite3"
    pipeline = OpenAIDecisionsPipeline(
        OpenAIDecisionsCache(str(cache_path), max_calls_per_day=5),
        client,
    )

    outcome = pipeline.run_category_phase([candidate.context], now=case.now)
    resolved_id = selected_product_type_id(candidate, outcome.results[0])

    assert len(client.calls) == 1
    assert outcome.results[0].status == "selected"
    assert outcome.results[0].confidence == 0.97
    assert resolved_id == case.type_a.id
    assert len(client.calls[0][1]) == 1
    assert client.calls[0][1][0].name.endswith(".category")


def test_selected_type_mapping_requires_at_least_point_95_confidence(case, tmp_path):
    candidate = build_candidate(case)
    client = FakeDecisionsClient(confidence=0.94)
    pipeline = OpenAIDecisionsPipeline(
        OpenAIDecisionsCache(
            str(tmp_path / "private" / "decisions.sqlite3"),
            max_calls_per_day=5,
        ),
        client,
    )
    outcome = pipeline.run_category_phase([candidate.context], now=case.now)

    assert outcome.results[0].status == "selected"
    assert outcome.results[0].confidence == 0.94
    assert selected_product_type_id(candidate, outcome.results[0]) is None


def test_selected_type_requires_exact_question_fingerprint_and_bounded_confidence(case, tmp_path):
    candidate = build_candidate(case)
    client = FakeDecisionsClient()
    pipeline = OpenAIDecisionsPipeline(
        OpenAIDecisionsCache(
            str(tmp_path / "private" / "decisions.sqlite3"),
            max_calls_per_day=5,
        ),
        client,
    )
    outcome = pipeline.run_category_phase([candidate.context], now=case.now)
    result = outcome.results[0]

    assert selected_product_type_id(
        candidate,
        replace(result, signature="f" * 64),
    ) is None
    assert selected_product_type_id(
        candidate,
        replace(result, confidence=1.01),
    ) is None


def test_category_window_batches_current_plus_clean_due_siblings_once(case, tmp_path):
    sibling_item, sibling_product = add_sibling(case, 2)
    collision_item, collision_product = add_sibling(
        case,
        4,
        vendor_code=case.draft.offer_id,
    )
    other_supplier = Supplier(name="Mapped sibling source", code="mapped-source")
    db.session.add(other_supplier)
    db.session.flush()
    mapped_item, mapped_product = add_sibling(
        case,
        5,
        supplier=other_supplier,
    )
    db.session.add(MarketplaceCategoryMapping(
        seller_id=case.seller.id,
        marketplace_id=case.marketplace.id,
        supplier_id=other_supplier.id,
        product_type_id=case.type_a.id,
        scope_key=f"supplier:{other_supplier.id}",
        source_type=other_supplier.code,
        source_category=SOURCE_PATH,
        source_category_normalized=MarketplaceDraftService._normalized_text(
            SOURCE_PATH,
        ),
        external_category_id="1001",
        external_type_id="7001",
        mapping_source="manual",
        mapping_status="stale",
        confidence=1.0,
    ))
    future_item, future_product = add_sibling(
        case,
        3,
        due_at=case.now + timedelta(minutes=5),
    )
    client = FakeDecisionsClient()
    pipeline = OpenAIDecisionsPipeline(
        OpenAIDecisionsCache(
            str(tmp_path / "private" / "decisions.sqlite3"),
            max_calls_per_day=5,
        ),
        client,
    )
    before_draft_count = MarketplaceProductDraft.query.count()

    result = prepare_ozon_category_window(
        run=case.run,
        account=case.account,
        current_item=case.item,
        current_product=case.product,
        current_draft=case.draft,
        prior_draft_id_before_create=None,
        item_draft_id_before_create=None,
        item_prepared_version_before_create=None,
        sibling_pairs=[
            (sibling_item, sibling_product),
            (collision_item, collision_product),
            (mapped_item, mapped_product),
            (future_item, future_product),
        ],
        pipeline=pipeline,
        now=case.now,
    )

    assert result is not None
    assert len(client.calls) == 1
    assert len(client.calls[0][1]) == 2
    assert len(result.entries) == 2
    request_text = json.dumps(client.calls[0][0], ensure_ascii=False)
    assert f"fabric storage 2" in request_text
    assert "fabric storage 4" not in request_text
    assert "fabric storage 5" not in request_text
    assert "fabric storage 3" not in request_text
    assert result.current_result.status == "selected"
    assert result.current_product_type_id == case.type_a.id
    assert result.requires_current_scope_recheck is True
    assert MarketplaceProductDraft.query.count() == before_draft_count
    assert MarketplaceProductDraft.query.filter_by(
        imported_product_id=sibling_product.id,
    ).first() is None
    assert MarketplaceProductDraft.query.filter_by(
        imported_product_id=future_product.id,
    ).first() is None


def test_category_window_never_exceeds_twelve_product_contexts(case, tmp_path):
    siblings = [add_sibling(case, ordinal) for ordinal in range(2, 14)]
    client = FakeDecisionsClient()
    pipeline = OpenAIDecisionsPipeline(
        OpenAIDecisionsCache(
            str(tmp_path / "private" / "decisions.sqlite3"),
            max_calls_per_day=5,
        ),
        client,
    )

    result = prepare_ozon_category_window(
        run=case.run,
        account=case.account,
        current_item=case.item,
        current_product=case.product,
        current_draft=case.draft,
        prior_draft_id_before_create=None,
        item_draft_id_before_create=None,
        item_prepared_version_before_create=None,
        sibling_pairs=siblings,
        pipeline=pipeline,
        now=case.now,
    )

    assert result is not None
    assert len(client.calls) == 1
    assert len(client.calls[0][1]) == 12
    assert len(result.entries) == 12


def test_same_candidate_scope_rechecks_and_draft_version_drift_fails_closed(case):
    before = build_candidate(case)
    after = build_candidate(case)
    assert same_ozon_prepare_category_scope(before, after)

    case.draft.version += 1
    drifted = build_candidate(case)
    assert drifted is None
    assert not same_ozon_prepare_category_scope(before, drifted)


def test_manual_prior_draft_typed_category_and_target_listing_are_rejected(case):
    assert build_candidate(case, prior_draft_id=case.draft.id) is None

    case.draft.product_type_id = case.type_a.id
    case.draft.status = "draft"
    case.item.prepared_version = case.draft.version
    assert build_candidate(case) is None
    case.draft.product_type_id = None
    case.draft.status = "needs_category"

    listing = MarketplaceListing(
        seller_id=case.seller.id,
        marketplace_id=case.marketplace.id,
        account_id=case.account.id,
        imported_product_id=case.product.id,
        offer_id=case.draft.offer_id,
        external_product_id="50001",
        sync_fingerprint="4" * 64,
    )
    db.session.add(listing)
    db.session.commit()
    assert build_candidate(case) is None


def test_any_supplier_or_wb_mapping_blocks_and_existing_wb_projection_alone_does_not(case):
    case.product.import_status = "imported"
    case.product.wb_nm_id = 4001
    case.product.wb_subject_id = 88
    db.session.commit()
    assert build_candidate(case) is not None

    mapping = MarketplaceCategoryMapping(
        seller_id=case.seller.id,
        marketplace_id=case.marketplace.id,
        supplier_id=None,
        product_type_id=case.type_a.id,
        scope_key="wb_subject",
        source_type="wb",
        source_category="WB subject 88",
        source_category_normalized="wb_subject:88",
        external_category_id="11",
        external_type_id="7001",
        mapping_source="manual",
        mapping_status="stale",
        confidence=1.0,
    )
    db.session.add(mapping)
    db.session.commit()
    assert build_candidate(case) is None


@pytest.mark.parametrize("parity_change", ["revision", "unversioned", "snapshot"])
def test_supplier_revision_or_original_snapshot_mismatch_rejects(parity_change, case):
    if parity_change == "revision":
        case.supplier_product.content_revision += 1
    elif parity_change == "unversioned":
        case.product.supplier_content_revision = 0
        case.supplier_product.content_revision = 0
    else:
        case.product.original_data = json.dumps({
            "title": SOURCE_TITLE,
            "category": "Other > Category",
        }, ensure_ascii=False)
    db.session.commit()
    assert build_candidate(case) is None


def test_missing_copy_vendor_requires_exact_current_source_proof_without_prompt_leak(case, tmp_path):
    exact_copy = build_candidate(case)
    assert exact_copy is not None

    case.product.external_vendor_code = None
    db.session.commit()
    candidate = build_candidate(case)

    assert candidate is not None
    assert candidate.question_fingerprint == exact_copy.question_fingerprint
    assert candidate.context.source_fields == {
        "name": SOURCE_TITLE,
        "category": SOURCE_PATH,
    }
    assert case.draft.offer_id == "SKU-001"

    client = FakeDecisionsClient()
    pipeline = OpenAIDecisionsPipeline(
        OpenAIDecisionsCache(
            str(tmp_path / "private" / "decisions.sqlite3"),
            max_calls_per_day=5,
        ),
        client,
    )
    pipeline.run_category_phase([candidate.context], now=case.now)
    assert len(client.calls) == 1
    request_capture = json.dumps(
        client.calls[0][0], ensure_ascii=False, sort_keys=True,
    ) + "\n" + "\n".join(
        json.dumps({
            "name": question.name,
            "instructions": question.instructions,
            "choices": [
                {"value": choice.value, "description": choice.description}
                for choice in question.choices
            ],
        }, ensure_ascii=False, sort_keys=True)
        for question in client.calls[0][1]
    )
    assert "SKU-001" not in request_capture
    assert "supplier-product-1" not in request_capture


def test_null_copy_and_null_supplier_vendor_keep_the_old_exact_equality_behavior(case):
    # The pre-existing exact-equality path allowed a missing vendor on both
    # rows when the source external ID was present. Preserve that case.
    case.product.external_vendor_code = None
    case.supplier_product.vendor_code = None
    db.session.commit()

    assert case.product.external_id == case.supplier_product.external_id
    assert build_candidate(case) is not None


@pytest.mark.parametrize("invalid_proof", [
    "missing_copy_external_id",
    "missing_supplier_external_id",
    "mismatched_external_id",
    "missing_vendor_snapshot",
    "blank_vendor_snapshot",
    "mismatched_vendor_snapshot",
    "supplier_vendor_changed_without_snapshot",
    "blank_supplier_vendor_code",
])
def test_null_copy_vendor_requires_exact_external_id_and_supplier_snapshot(
    invalid_proof,
    case,
):
    case.product.external_vendor_code = None
    source_snapshot = json.loads(case.supplier_product.original_data_json)
    if invalid_proof == "missing_copy_external_id":
        case.product.external_id = None
    elif invalid_proof == "missing_supplier_external_id":
        case.supplier_product.external_id = None
    elif invalid_proof == "mismatched_external_id":
        case.product.external_id = "other-source-id"
    elif invalid_proof == "missing_vendor_snapshot":
        source_snapshot.pop("vendor_code")
    elif invalid_proof == "blank_vendor_snapshot":
        source_snapshot["vendor_code"] = "   "
    elif invalid_proof == "mismatched_vendor_snapshot":
        source_snapshot["vendor_code"] = "OTHER-SKU"
    elif invalid_proof == "supplier_vendor_changed_without_snapshot":
        case.supplier_product.vendor_code = "OTHER-SKU"
    elif invalid_proof == "blank_supplier_vendor_code":
        case.supplier_product.vendor_code = ""
    case.supplier_product.original_data_json = json.dumps(
        source_snapshot,
        ensure_ascii=False,
        sort_keys=True,
    )
    db.session.commit()

    assert build_candidate(case) is None


@pytest.mark.parametrize("copy_vendor_code", ["DIFFERENT-SKU", ""])
def test_populated_contradictory_copy_vendor_never_uses_missing_code_proof(
    copy_vendor_code,
    case,
):
    case.product.external_vendor_code = copy_vendor_code
    db.session.commit()

    assert build_candidate(case) is None


def test_rebuilt_scope_binds_current_supplier_vendor_snapshot_proof(case):
    case.product.external_vendor_code = None
    db.session.commit()
    before = build_candidate(case)
    assert before is not None

    supplier_snapshot = json.loads(case.supplier_product.original_data_json)
    supplier_snapshot["vendor_code"] = "SKU-UPDATED"
    case.supplier_product.vendor_code = "SKU-UPDATED"
    case.supplier_product.original_data_json = json.dumps(
        supplier_snapshot,
        ensure_ascii=False,
        sort_keys=True,
    )
    db.session.commit()

    after = build_candidate(case)
    assert after is not None
    assert after.question_fingerprint == before.question_fingerprint
    assert after.scope_signature != before.scope_signature
    assert not same_ozon_prepare_category_scope(before, after)


@pytest.mark.parametrize("drift", ["title", "category", "fact"])
def test_rebuilt_scope_rejects_title_category_or_fact_drift_after_request(drift, case):
    case.product.external_vendor_code = None
    db.session.commit()
    before = build_candidate(case)
    assert before is not None
    captured_fact_hash = case.draft.source_fact_hash

    imported_snapshot = json.loads(case.product.original_data)
    supplier_snapshot = json.loads(case.supplier_product.original_data_json)
    if drift == "title":
        imported_snapshot["title"] = "Новый складной органайзер для дома"
        supplier_snapshot["title"] = imported_snapshot["title"]
        case.product.title = imported_snapshot["title"]
        case.supplier_product.title = imported_snapshot["title"]
    elif drift == "category":
        imported_snapshot["category"] = "Органайзеры"
        supplier_snapshot["category"] = imported_snapshot["category"]
        case.product.category = imported_snapshot["category"]
        case.supplier_product.category = imported_snapshot["category"]
    else:
        imported_snapshot["characteristics"] = {"Материал": "Лён"}
        supplier_snapshot["characteristics"] = {"Материал": "Лён"}
    case.product.original_data = json.dumps(
        imported_snapshot,
        ensure_ascii=False,
        sort_keys=True,
    )
    case.supplier_product.original_data_json = json.dumps(
        supplier_snapshot,
        ensure_ascii=False,
        sort_keys=True,
    )
    db.session.commit()

    after = build_candidate(case)
    # The worker's captured draft hash and item version remain unchanged. Source
    # title/category/fact drift makes a fresh candidate ineligible, which is a
    # hard denial before any response can be applied.
    assert MarketplaceFactPackBuilder.build(case.product)["fact_hash"] != captured_fact_hash
    assert after is None


def _queue_source_product(case, *, external_id, vendor_code, copied_vendor_code):
    raw = json.dumps({
        "title": SOURCE_TITLE,
        "category": SOURCE_PATH,
        "all_categories": [SOURCE_PATH],
        "vendor_code": vendor_code,
    }, ensure_ascii=False, sort_keys=True)
    supplier_product = SupplierProduct(
        supplier_id=case.supplier.id,
        external_id=external_id,
        vendor_code=vendor_code,
        title=SOURCE_TITLE,
        category=SOURCE_PATH,
        content_revision=4,
        original_data_json=raw,
    )
    db.session.add(supplier_product)
    db.session.flush()
    product = ImportedProduct(
        seller_id=case.seller.id,
        supplier_id=case.supplier.id,
        supplier_product_id=supplier_product.id,
        supplier_content_revision=4,
        source_type=case.supplier.code,
        external_id=external_id,
        external_vendor_code=copied_vendor_code,
        title=SOURCE_TITLE,
        category=SOURCE_PATH,
        original_data=raw,
        import_status="pending",
    )
    db.session.add(product)
    db.session.flush()
    return product


@pytest.mark.parametrize(
    "proof_drift_during_answer",
    [False, True],
    ids=["stable-proof", "proof-drifts-mid-answer"],
)
def test_ordinary_source_prepare_queue_fences_vendor_proof_and_reuses_sibling_cache(
    case,
    tmp_path,
    proof_drift_during_answer,
):
    # The ordinary service path creates the draft itself. Only the current item
    # has a missing copied code; both source snapshots are exact and current.
    case.run.state = "completed"
    case.run.next_due_at = None
    case.run.lease_token = None
    case.run.lease_until = None
    case.marketplace.categories_synced_at = datetime.utcnow()
    current = _queue_source_product(
        case,
        external_id="queue-source-current",
        vendor_code="QUEUE-SKU-CURRENT",
        copied_vendor_code=None,
    )
    sibling = _queue_source_product(
        case,
        external_id="queue-source-sibling",
        vendor_code="QUEUE-SKU-SIBLING",
        copied_vendor_code="QUEUE-SKU-SIBLING",
    )
    db.session.commit()
    case.app.config["MARKETPLACE_OZON_ENABLED"] = True
    accepted = OzonBulkUploadService.accept_source_prepare(
        seller_id=case.seller.id,
        account_id=case.account.id,
        imported_product_ids=[current.id, sibling.id],
        request_key="local-r80-null-copy-queue-0001",
    )
    run = OzonBulkUploadRun.query.filter_by(job_id=accepted.job.id).one()
    claimed = OzonUploadQueueService.claim_due_run(
        now=datetime.utcnow(), exclude_run_ids=set(),
    )
    assert claimed is not None
    run_id, lease_token = claimed
    assert run_id == run.id
    if proof_drift_during_answer:
        class ProofDriftingDecisionsClient(FakeDecisionsClient):
            def decide(self, *, input_json, questions):
                outcome = super().decide(
                    input_json=input_json,
                    questions=questions,
                )
                source = SupplierProduct.query.filter_by(
                    external_id="queue-source-current",
                ).one()
                snapshot = json.loads(source.original_data_json)
                snapshot["vendor_code"] = "QUEUE-SKU-CURRENT-UPDATED"
                source.vendor_code = "QUEUE-SKU-CURRENT-UPDATED"
                source.original_data_json = json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                # Keep the source revision fixed to exercise the private
                # vendor-proof scope signature after the fake response.
                db.session.commit()
                return outcome

        client = ProofDriftingDecisionsClient(confidence=0.99)
    else:
        client = FakeDecisionsClient(confidence=0.99)
    pipeline = OpenAIDecisionsPipeline(
        OpenAIDecisionsCache(
            str(tmp_path / "queue-cache" / "decisions.sqlite3"),
            max_calls_per_day=5,
        ),
        client,
    )

    def advance(product):
        item = OzonBulkUploadItem.query.filter_by(
            run_id=run.id, imported_product_id=product.id,
        ).one()
        result = OzonUploadQueueService.advance_item(
            run_id=run.id,
            item_id=item.id,
            lease_token=lease_token,
            now=datetime.utcnow(),
            deadline_monotonic=time.monotonic() + 35,
        )
        db.session.refresh(item)
        return item, result

    with patch(
        "services.openai_decisions_runtime.create_openai_decisions_pipeline",
        return_value=pipeline,
    ):
        current_item, _current_result = advance(current)
        current_draft = MarketplaceDraftService.get_draft(
            seller_id=case.seller.id, draft_id=current_item.draft_id,
        )
        if proof_drift_during_answer:
            assert current_draft.product_type_id is None
            current_source = SupplierProduct.query.filter_by(
                external_id="queue-source-current",
            ).one()
            assert current_source.vendor_code == "QUEUE-SKU-CURRENT-UPDATED"
        else:
            assert current_draft.product_type_id == case.type_a.id
        # Draft identity still comes from the exact imported source facts; the
        # queue does not copy or rewrite ImportedProduct.external_vendor_code.
        assert current_draft.offer_id == "QUEUE-SKU-CURRENT"
        assert current.external_vendor_code is None
        assert len(client.calls) == 1
        assert len(client.calls[0][0]["products"]) == 2

        sibling_item, _sibling_result = advance(sibling)
        sibling_draft = MarketplaceDraftService.get_draft(
            seller_id=case.seller.id, draft_id=sibling_item.draft_id,
        )
        assert sibling_draft.product_type_id == case.type_a.id
        assert sibling_draft.offer_id == "QUEUE-SKU-SIBLING"
        assert len(client.calls) == 1

    assert current.external_vendor_code is None
    assert MarketplaceOperation.query.count() == 0


def test_tree_freshness_unique_leaf_ambiguity_and_shortlist_bounds(case):
    case.product.original_data = json.dumps({
        "title": SOURCE_TITLE,
        "category": "Органайзеры",
    }, ensure_ascii=False)
    case.product.category = "Органайзеры"
    case.supplier_product.original_data_json = case.product.original_data
    case.draft.source_fact_hash = MarketplaceFactPackBuilder.build(
        case.product,
    )["fact_hash"]
    db.session.commit()
    case.item.prepared_version = case.draft.version
    db.session.commit()
    assert build_candidate(case).match_kind == "unique_leaf"

    duplicate = MarketplaceTaxonomyCategory(
        marketplace_id=case.marketplace.id,
        external_category_id="12",
        name="Органайзеры",
        full_path="Другая категория / Органайзеры",
        depth=1,
        is_available=True,
        is_disabled_upstream=False,
    )
    db.session.add(duplicate)
    db.session.commit()
    assert build_candidate(case) is None

    case.marketplace.categories_synced_at = case.now - timedelta(days=3)
    db.session.commit()
    assert build_ozon_category_tree_index(
        case.marketplace,
        ["Органайзеры"],
        now=case.now,
    ) is None


def test_same_run_retry_requires_pre_transition_version_match(case):
    candidate = build_candidate(
        case,
        prior_draft_id=case.draft.id,
        item_draft_id=case.draft.id,
        item_prepared_version=case.draft.version,
    )
    assert candidate is not None
    assert build_candidate(
        case,
        prior_draft_id=case.draft.id,
        item_draft_id=case.draft.id,
        item_prepared_version=case.draft.version - 1,
    ) is None
