"""The local editor explains held writes without locking local preparation."""
from unittest.mock import patch

from models import db, MarketplaceListing, MarketplaceOperation, MarketplaceWriteQuarantine
from services.marketplace_draft_editor import MarketplaceDraftEditor
from services.ozon_write_quarantine import draft_hold
from tests.test_marketplace_draft_editor import draft_fixture


def place_fixture(draft, *, scope='product', offer=None, product=None):
    operation = MarketplaceOperation(seller_id=draft.seller_id, marketplace_id=draft.marketplace_id,
        account_id=draft.account_id, operation_kind='product_import', status='uncertain', attempt_count=1,
        request_fingerprint='a'*64, idempotency_key='synthetic-held-original', contract_version='synthetic')
    db.session.add(operation)
    db.session.flush()
    hold = MarketplaceWriteQuarantine(seller_id=draft.seller_id, marketplace_id=draft.marketplace_id,
        account_id=draft.account_id, operation_id=operation.id, scope_kind=scope,
        offer_id=offer or draft.offer_id if scope == 'product' else None, product_id=product,
        scope_reason='immutable_target_verified' if scope == 'product' else 'identity_unknown',
        reviewed_scope_token='a'*64)
    db.session.add(hold)
    db.session.commit()
    return hold


def test_editor_exact_hold_links_original_without_mutating_local_draft(draft_fixture):
    f = draft_fixture
    hold = place_fixture(f.draft)
    before = f.draft.to_public_dict(detail=True)
    with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
        doc = MarketplaceDraftEditor.document(seller_id=f.seller1_id, draft_id=f.draft.id)
    assert doc['write_quarantine']['review_url'] == f'/marketplaces/operations/{hold.operation_id}/review'
    assert doc['write_quarantine']['scope'] == 'product'
    assert f.draft.to_public_dict(detail=True) == before
    assert doc['active_operation_id'] is None  # The origin need not be this draft.


def test_editor_account_hold_and_other_product_are_distinct(draft_fixture):
    f = draft_fixture
    hold = place_fixture(f.draft, offer='unrelated')
    assert draft_hold(f.draft) is None
    hold.scope_kind, hold.offer_id, hold.scope_reason = 'account', None, 'identity_unknown'
    db.session.commit()  # Synthetic fixture only: service never mutates scope.
    assert draft_hold(f.draft).id == hold.id
    hold.account_id = f.account2.id
    db.session.commit()
    assert draft_hold(f.draft) is None


def test_editor_known_product_id_survives_offer_rename(draft_fixture):
    f = draft_fixture
    hold = place_fixture(f.draft, offer='old-offer', product='777')
    listing = MarketplaceListing(seller_id=f.draft.seller_id, marketplace_id=f.draft.marketplace_id,
        account_id=f.draft.account_id, offer_id=f.draft.offer_id, external_product_id='777', sync_fingerprint='a'*64)
    db.session.add(listing)
    db.session.flush()
    f.draft.published_listing_id = listing.id
    db.session.commit()
    assert draft_hold(f.draft).id == hold.id
    listing.seller_id = f.seller2_id
    db.session.commit()
    assert draft_hold(f.draft) is None
