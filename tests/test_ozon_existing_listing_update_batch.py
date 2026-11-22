"""Exact local batch preparation for linked Ozon listing update drafts."""

from unittest.mock import Mock

import pytest

from models import (
    MarketplaceListingLinkEvent,
    MarketplaceOperation,
    MarketplaceProductDraft,
    db,
)
from services.marketplace_drafts import (
    MarketplaceDraftConflict,
    MarketplaceDraftNotFound,
    MarketplaceDraftService,
    MarketplaceDraftValidationError,
)
from services.ozon_api_client import OzonSellerAPIClient
from services.ozon_existing_update_batch import OzonExistingUpdateBatch as Batch
import tests.test_marketplace_drafts as draft_test_module


@pytest.fixture
def case():
    value = draft_test_module.MarketplaceDraftServiceTest(methodName="runTest")
    value.setUp()
    try:
        yield value
    finally:
        value.tearDown()


def listing(case, external_id):
    return case._existing_listing_precondition(external_id=external_id)[:2]


def context(case, target):
    return MarketplaceDraftService.existing_listing_update_draft_context(
        seller_id=case.seller1_id,
        listing_id=target.id,
    )


def test_preview_is_ordered_exact_local_and_read_only(case, monkeypatch):
    first_product, first = listing(case, "batch-preview-first")
    second_product, second = listing(case, "batch-preview-second")
    request = Mock(side_effect=AssertionError("No Ozon provider calls"))
    monkeypatch.setattr(OzonSellerAPIClient, "request", request)
    before = [
        (row.id, row.imported_product_id, row.link_version, row.link_evidence_json)
        for row in (first, second)
    ]

    result = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[second.id, first.id],
    )
    replay = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[second.id, first.id],
    )

    assert result["account_id"] == case.account1.id
    assert result["account_label"] == case.account1.label
    assert result["listing_ids"] == [second.id, first.id]
    assert [row["listing_id"] for row in result["rows"]] == [second.id, first.id]
    assert [row["status"] for row in result["rows"]] == ["ready", "ready"]
    assert result["digest"] == replay["digest"]
    assert len(result["digest"]) == 64
    assert all("expected_existing_listing" not in row for row in result["rows"])
    assert MarketplaceProductDraft.query.count() == 0
    assert MarketplaceListingLinkEvent.query.count() == 0
    assert MarketplaceOperation.query.count() == 0
    assert [
        (row.id, row.imported_product_id, row.link_version, row.link_evidence_json)
        for row in (first, second)
    ] == before
    assert first_product.id == first.imported_product_id
    assert second_product.id == second.imported_product_id
    request.assert_not_called()


def test_apply_creates_in_order_and_replay_requires_new_preview(case, monkeypatch):
    _first_product, first = listing(case, "batch-apply-first")
    _second_product, second = listing(case, "batch-apply-second")
    request = Mock(side_effect=AssertionError("No Ozon provider calls"))
    monkeypatch.setattr(OzonSellerAPIClient, "request", request)
    links_before = {
        target.id: (
            target.imported_product_id,
            target.link_status,
            target.link_source,
            target.link_version,
            target.linked_at,
            target.link_evidence_json,
        )
        for target in (first, second)
    }
    preview = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[second.id, first.id],
    )

    result = Batch.apply(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[second.id, first.id],
        expected_digest=preview["digest"],
    )

    assert result["listing_ids"] == [second.id, first.id]
    assert [row["listing_id"] for row in result["rows"]] == [second.id, first.id]
    assert [row["status"] for row in result["rows"]] == ["created", "created"]
    created = MarketplaceProductDraft.query.order_by(
        MarketplaceProductDraft.id.asc()
    ).all()
    assert [row.id for row in created] == [
        item["draft_id"] for item in result["rows"]
    ]
    assert [row.published_listing_id for row in created] == [second.id, first.id]
    assert MarketplaceListingLinkEvent.query.count() == 0
    assert MarketplaceOperation.query.count() == 0
    assert {
        target.id: (
            target.imported_product_id,
            target.link_status,
            target.link_source,
            target.link_version,
            target.linked_at,
            target.link_evidence_json,
        )
        for target in (first, second)
    } == links_before
    request.assert_not_called()

    with pytest.raises(MarketplaceDraftConflict):
        Batch.apply(
            seller_id=case.seller1_id,
            account_id=case.account1.id,
            listing_ids=[second.id, first.id],
            expected_digest=preview["digest"],
        )
    assert MarketplaceProductDraft.query.count() == 2


def test_whole_scope_rejects_foreign_or_other_account_before_preparing(case):
    _product, own = listing(case, "batch-whole-scope-own")
    foreign_product = case._product(
        seller_id=case.seller2_id,
        external_id="batch-whole-scope-foreign",
    )
    foreign = case._linked_listing(
        foreign_product,
        product_type=case.product_type,
    )
    other_account = case._account(case.seller1_id, "batch-second-account")
    other_product, other_listing = listing(case, "batch-other-account")
    other_listing.account_id = other_account.id
    db.session.commit()

    for ids in ([own.id, foreign.id], [own.id, other_listing.id]):
        with pytest.raises(MarketplaceDraftNotFound):
            Batch.preview(
                seller_id=case.seller1_id,
                account_id=case.account1.id,
                listing_ids=list(ids),
            )
    assert MarketplaceProductDraft.query.count() == 0
    assert MarketplaceListingLinkEvent.query.count() == 0
    assert foreign_product.seller_id == case.seller2_id
    assert other_product.seller_id == case.seller1_id


@pytest.mark.parametrize(
    "listing_ids",
    [
        [],
        [1, 1],
        [True],
        [0],
        [-1],
        (1,),
        list(range(1, Batch.MAX_ITEMS + 2)),
    ],
)
def test_listing_scope_is_bounded_ordered_unique_native_ids(case, listing_ids):
    with pytest.raises(MarketplaceDraftValidationError):
        Batch.preview(
            seller_id=case.seller1_id,
            account_id=case.account1.id,
            listing_ids=listing_ids,
        )


def test_preview_accepts_exact_200_item_scope_and_keeps_order(case):
    listing_ids = []
    for index in range(Batch.MAX_ITEMS):
        _product, target = listing(case, f"batch-max-scope-{index:03d}")
        listing_ids.append(target.id)
    ordered = list(reversed(listing_ids))

    result = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=ordered,
    )

    assert result["listing_ids"] == ordered
    assert [row["listing_id"] for row in result["rows"]] == ordered
    assert all(row["status"] == "ready" for row in result["rows"])


def test_apply_rejects_listing_drift_before_any_write(case):
    _product, target = listing(case, "batch-stale-listing")
    preview = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[target.id],
    )
    target.link_version += 1
    target.title = "Changed after review"
    db.session.commit()

    with pytest.raises(MarketplaceDraftConflict):
        Batch.apply(
            seller_id=case.seller1_id,
            account_id=case.account1.id,
            listing_ids=[target.id],
            expected_digest=preview["digest"],
        )
    assert MarketplaceProductDraft.query.count() == 0
    assert MarketplaceListingLinkEvent.query.count() == 0


def test_active_listing_operation_is_reported_as_blocked(case):
    _product, target = listing(case, "batch-active-operation")
    db.session.add(MarketplaceOperation(
        seller_id=case.seller1_id,
        marketplace_id=case.marketplace.id,
        account_id=case.account1.id,
        listing_id=target.id,
        operation_kind="product_update",
        status="queued",
        idempotency_key="batch-active-listing-operation",
        request_fingerprint="a" * 64,
        contract_version="batch-active-listing-v1",
        request_summary_json="{}",
    ))
    db.session.commit()

    result = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[target.id],
    )

    assert result["rows"] == [{
        "listing_id": target.id,
        "title": target.title,
        "offer_id": target.offer_id,
        "status": "blocked",
        "message": (
            "По этой карточке Ozon уже выполняется операция; "
            "откройте её статус и повторите позже"
        ),
    }]
    assert MarketplaceProductDraft.query.count() == 0
    assert MarketplaceOperation.query.count() == 1


def test_active_catalog_operation_on_existing_draft_is_blocked(case):
    _product, target = listing(case, "batch-existing-active-operation")
    existing_context = context(case, target)
    draft = MarketplaceDraftService.create_draft(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        imported_product_id=existing_context["imported_product_id"],
        expected_existing_listing=existing_context["expected_existing_listing"],
    )
    db.session.add(MarketplaceOperation(
        seller_id=case.seller1_id,
        marketplace_id=case.marketplace.id,
        account_id=case.account1.id,
        listing_id=target.id,
        operation_kind="product_update",
        status="queued",
        idempotency_key="batch-existing-active-operation",
        request_fingerprint="c" * 64,
        contract_version="batch-existing-active-v1",
        request_summary_json="{}",
    ))
    db.session.commit()

    preview = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[target.id],
    )
    result = Batch.apply(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[target.id],
        expected_digest=preview["digest"],
    )

    assert preview["rows"][0]["status"] == "blocked"
    assert result["rows"][0]["status"] == "blocked"
    assert result["rows"][0]["message"] == (
        "Черновик нельзя менять, пока публикация не завершена"
    )
    assert "draft_id" not in result["rows"][0]
    assert MarketplaceProductDraft.query.count() == 1
    assert draft.status != "archived"


def test_existing_non_archived_reused_and_archived_blocked(case):
    _existing_product, existing_listing = listing(case, "batch-existing-draft")
    existing_context = context(case, existing_listing)
    existing_draft = MarketplaceDraftService.create_draft(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        imported_product_id=existing_context["imported_product_id"],
        expected_existing_listing=existing_context["expected_existing_listing"],
    )
    _archived_product, archived_listing = listing(case, "batch-archived-draft")
    archived_context = context(case, archived_listing)
    archived_draft = MarketplaceDraftService.create_draft(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        imported_product_id=archived_context["imported_product_id"],
        expected_existing_listing=archived_context["expected_existing_listing"],
    )
    archived_draft.status = "archived"
    db.session.commit()
    existing_version = existing_draft.version

    preview = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[archived_listing.id, existing_listing.id],
    )
    assert [row["status"] for row in preview["rows"]] == ["blocked", "existing"]
    result = Batch.apply(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[archived_listing.id, existing_listing.id],
        expected_digest=preview["digest"],
    )

    assert [row["status"] for row in result["rows"]] == ["blocked", "existing"]
    assert result["rows"][0]["message"] == "Архивный черновик нельзя включить в обновление"
    assert result["rows"][1]["draft_id"] == existing_draft.id
    db.session.refresh(existing_draft)
    db.session.refresh(archived_draft)
    assert existing_draft.version == existing_version
    assert archived_draft.status == "archived"
    assert MarketplaceProductDraft.query.count() == 2


def test_existing_draft_version_drift_rejects_before_write(case):
    _existing_product, existing_listing = listing(case, "batch-existing-version")
    existing_context = context(case, existing_listing)
    existing_draft = MarketplaceDraftService.create_draft(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        imported_product_id=existing_context["imported_product_id"],
        expected_existing_listing=existing_context["expected_existing_listing"],
    )
    _ready_product, ready_listing = listing(case, "batch-ready-version-drift")
    preview = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[ready_listing.id, existing_listing.id],
    )
    existing_draft.version += 1
    db.session.commit()

    with pytest.raises(MarketplaceDraftConflict):
        Batch.apply(
            seller_id=case.seller1_id,
            account_id=case.account1.id,
            listing_ids=[ready_listing.id, existing_listing.id],
            expected_digest=preview["digest"],
        )
    assert MarketplaceProductDraft.query.count() == 1
    assert MarketplaceDraftService.existing_listing_update_draft_context(
        seller_id=case.seller1_id,
        listing_id=ready_listing.id,
    )["status"] == "ready"


def test_partial_concurrent_row_failure_preserves_exact_successes(case, monkeypatch):
    _first_product, first = listing(case, "batch-partial-first")
    _second_product, second = listing(case, "batch-partial-second")
    preview = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[first.id, second.id],
    )
    original = MarketplaceDraftService.create_draft
    calls = []

    def create_draft(**kwargs):
        calls.append(kwargs["imported_product_id"])
        if len(calls) == 2:
            raise MarketplaceDraftConflict("Карточка занята параллельной операцией")
        return original(**kwargs)

    monkeypatch.setattr(MarketplaceDraftService, "create_draft", create_draft)
    result = Batch.apply(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[first.id, second.id],
        expected_digest=preview["digest"],
    )

    assert [row["listing_id"] for row in result["rows"]] == [first.id, second.id]
    assert [row["status"] for row in result["rows"]] == ["created", "blocked"]
    assert result["rows"][1]["message"] == "Карточка занята параллельной операцией"
    assert MarketplaceProductDraft.query.count() == 1
    assert MarketplaceProductDraft.query.one().published_listing_id == first.id
    assert MarketplaceListingLinkEvent.query.count() == 0


def test_post_apply_operation_fence_drops_newly_held_draft_id(case, monkeypatch):
    _product, target = listing(case, "batch-post-apply-operation")
    preview = Batch.preview(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[target.id],
    )
    original_assert = MarketplaceDraftService._assert_no_active_publication
    inserted = False

    def insert_operation_then_assert(draft):
        nonlocal inserted
        if not inserted:
            inserted = True
            db.session.add(MarketplaceOperation(
                seller_id=case.seller1_id,
                marketplace_id=case.marketplace.id,
                account_id=case.account1.id,
                listing_id=target.id,
                operation_kind="product_update",
                status="queued",
                idempotency_key="batch-post-apply-operation",
                request_fingerprint="d" * 64,
                contract_version="batch-post-apply-v1",
                request_summary_json="{}",
            ))
            db.session.commit()
        return original_assert(draft)

    monkeypatch.setattr(
        MarketplaceDraftService,
        "_assert_no_active_publication",
        insert_operation_then_assert,
    )
    result = Batch.apply(
        seller_id=case.seller1_id,
        account_id=case.account1.id,
        listing_ids=[target.id],
        expected_digest=preview["digest"],
    )

    assert result["rows"][0]["status"] == "blocked"
    assert result["rows"][0]["message"] == (
        "Черновик нельзя менять, пока публикация не завершена"
    )
    assert "draft_id" not in result["rows"][0]
    assert MarketplaceProductDraft.query.count() == 1
    assert MarketplaceOperation.query.count() == 1
