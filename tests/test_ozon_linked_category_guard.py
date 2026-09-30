"""Linked Ozon cards keep their observed category; legacy drift is repairable."""
from datetime import timedelta

import pytest

from models import db
from services.marketplace_draft_editor import MarketplaceDraftEditor
from services.marketplace_drafts import MarketplaceDraftConflict, MarketplaceDraftService
from tests import test_marketplace_drafts as fixtures
from tests.test_marketplace_draft_editor import run_node


@pytest.fixture
def linked():
    fx = fixtures.MarketplaceDraftServiceTest()
    fx.setUp()
    fx.app.secret_key = "synthetic-linked-category-review-secret"
    try:
        product, draft = fx._ready_draft(external_id="linked-category-guard")
        listing = fx._linked_listing(product, product_type=fx.product_type)
        listing.external_category_id = fx.category.external_category_id
        listing.external_type_id = fx.product_type.external_type_id
        listing.last_seen_at = fx.now
        listing.info_synced_at = fx.now
        draft.published_listing_id = listing.id
        db.session.commit()
        yield fx, draft, listing
    finally:
        fx.tearDown()


def impact(fx, draft, target_id):
    return MarketplaceDraftService.category_impact(
        seller_id=fx.seller1_id, draft_id=draft.id,
        expected_version=draft.version, target_product_type_id=target_id,
        save_mapping=False, actor_user_id=None,
    )


def test_linked_type_change_rejected_before_preview_or_patch_mutation(linked):
    fx, draft, _ = linked
    other = fx._official_type("Другой тип", "Новая категория / Другой тип")
    state = MarketplaceDraftEditor.document(seller_id=fx.seller1_id, draft_id=draft.id)["linked_category"]
    assert state == {"mode": "fixed"}
    with pytest.raises(MarketplaceDraftConflict, match="Категория существующей карточки"):
        impact(fx, draft, other.id)
    original_name = draft.content_json
    with pytest.raises(MarketplaceDraftConflict, match="Категория существующей карточки"):
        MarketplaceDraftService._update_draft_core(
            seller_id=fx.seller1_id, draft_id=draft.id,
            expected_version=draft.version, commit=False,
            category_review_required=False,
            patch={"product_type_id": other.id, "content": {"name": "Не сохранять"}},
        )
    assert draft.product_type_id == fx.product_type.id
    assert draft.content_json == original_name


def test_new_draft_for_exact_existing_listing_cannot_choose_other_type():
    fx = fixtures.MarketplaceDraftServiceTest()
    fx.setUp()
    try:
        product = fx._product(external_id="linked-before-create")
        listing = fx._linked_listing(product, product_type=fx.product_type)
        listing.external_category_id = fx.category.external_category_id
        listing.external_type_id = fx.product_type.external_type_id
        db.session.commit()
        other = fx._official_type("Другой тип", "Новая категория / Другой тип")
        with pytest.raises(MarketplaceDraftConflict, match="существующей карточки"):
            MarketplaceDraftService.create_draft(
                seller_id=fx.seller1_id, account_id=fx.account1.id,
                imported_product_id=product.id, product_type_id=other.id,
                source_link_preflight=False,
            )
        created = MarketplaceDraftService.create_draft(
            seller_id=fx.seller1_id, account_id=fx.account1.id,
            imported_product_id=product.id, product_type_id=fx.product_type.id,
            source_link_preflight=False,
        )
        assert created.product_type_id == fx.product_type.id
    finally:
        fx.tearDown()


def test_aligned_linked_type_resubmitted_with_title_edit_is_noop(linked):
    fx, draft, _ = linked
    preview = impact(fx, draft, fx.product_type.id)
    updated = MarketplaceDraftService.update_draft(
        seller_id=fx.seller1_id, draft_id=draft.id,
        expected_version=draft.version,
        patch={"product_type_id": fx.product_type.id,
               "content": {"name": "Обновлённое название"}},
        category_review_token=preview["review_token"],
    )
    assert updated.product_type_id == fx.product_type.id
    assert updated.to_public_dict(detail=True)["content"]["name"] == "Обновлённое название"


def test_legacy_mismatch_can_only_restore_fresh_observed_type(linked):
    fx, draft, _ = linked
    other = fx._official_type("Другой тип", "Новая категория / Другой тип")
    MarketplaceDraftService._bind_type(draft, other)
    db.session.commit()
    state = MarketplaceDraftEditor.document(seller_id=fx.seller1_id, draft_id=draft.id)["linked_category"]
    assert state["mode"] == "repair"
    assert state["observed_type"]["id"] == fx.product_type.id
    with pytest.raises(MarketplaceDraftConflict):
        impact(fx, draft, other.id)
    preview = impact(fx, draft, fx.product_type.id)
    updated = MarketplaceDraftService.update_draft(
        seller_id=fx.seller1_id, draft_id=draft.id,
        expected_version=draft.version,
        patch={"product_type_id": fx.product_type.id, "save_mapping": False},
        category_review_token=preview["review_token"],
    )
    assert updated.product_type_id == fx.product_type.id
    assert updated.external_category_id == fx.category.external_category_id
    assert updated.external_type_id == fx.product_type.external_type_id
    assert MarketplaceDraftService.linked_category_state(updated) == {"mode": "fixed"}


def test_legacy_external_identity_drift_repairs_even_when_local_type_id_matches(linked):
    fx, draft, _ = linked
    draft.external_category_id = "legacy-mismatch"
    db.session.commit()
    assert MarketplaceDraftService.linked_category_state(draft)["mode"] == "repair"
    preview = impact(fx, draft, fx.product_type.id)
    updated = MarketplaceDraftService.update_draft(
        seller_id=fx.seller1_id, draft_id=draft.id,
        expected_version=draft.version,
        patch={"product_type_id": fx.product_type.id},
        category_review_token=preview["review_token"],
    )
    assert updated.external_category_id == fx.category.external_category_id
    assert MarketplaceDraftService.linked_category_state(updated) == {"mode": "fixed"}


@pytest.mark.parametrize("break_identity", [
    lambda fx, listing: setattr(listing, "last_seen_at", fx.now - timedelta(days=3)),
    lambda fx, listing: setattr(listing, "external_type_id", "wrong-type"),
    lambda fx, listing: setattr(listing, "account_id", fx.account2.id),
])
def test_untrusted_observed_type_fails_closed(linked, break_identity):
    fx, draft, listing = linked
    other = fx._official_type("Другой тип", "Новая категория / Другой тип")
    MarketplaceDraftService._bind_type(draft, other)
    break_identity(fx, listing)
    db.session.commit()
    assert MarketplaceDraftService.linked_category_state(draft) == {"mode": "unavailable"}
    with pytest.raises(MarketplaceDraftConflict):
        impact(fx, draft, fx.product_type.id)


def test_editor_hides_linked_search_and_opens_only_observed_repair():
    run_node(r'''
(async()=>{
page.data.linked_category={mode:'fixed'};page.draft.published_listing_id=42;
assert.equal(page.linkedCategory.mode,'fixed');
page.selectedType={id:9,name:'Other'};page.applyType();
assert.match(page.typeError,/существующей карточки/);
page.data.linked_category={mode:'repair',observed_type:{id:7,name:'Наблюдённый тип',category_path:'Одежда'}};
let reviewed=0;page.loadCategoryImpact=async()=>{reviewed++};
page.$refs.categoryDialog={showModal(){},close(){}};
page.prepareLinkedCategoryRepair();await new Promise(setImmediate);
assert.equal(page.selectedType.id,7);assert.equal(page.saveMapping,false);
assert.equal(page.categoryReview.target.id,7);assert.equal(reviewed,1);
})().catch(e=>{console.error(e);process.exitCode=1});
''')
