"""Tenant-safe display and optimistic local edits; synthetic provider only."""
from datetime import date, datetime
import json
from unittest.mock import patch

import pytest

from tests import test_marketplace_inbox as fixtures
from models import db, MarketplaceInboxItem, MarketplaceListing, MarketplaceReplyDraft
from services.marketplace_inbox import (
    MarketplaceInboxService as Service, MarketplaceInboxConflict,
    MarketplaceInboxNotFound, MarketplaceReplyGenerationError,
)


@pytest.fixture
def seed():
    fixture = fixtures.MarketplaceInboxServiceTest()
    fixture.setUp()
    fixture._sync()
    fixture.item = MarketplaceInboxItem.query.one()
    fixture.scope = dict(seller_id=fixture.seller.id, account_id=fixture.account.id, item_id=fixture.item.id)
    try:
        yield fixture
    finally:
        fixture.tearDown()
        fixture.doCleanups()


def draft(seed):
    return Service.create_reply_draft(**seed.scope, generation_mode='template', created_by_user_id=seed.user.id)


def save(seed, previous, text='Спасибо за отзыв. Учтём ваше замечание.'):
    return Service.save_reply_draft(**seed.scope, draft_id=previous.id,
        expected_content_hash=previous.content_hash, text=text, created_by_user_id=seed.user.id)


def test_exact_photo_url_and_listing_reference_only(seed):
    seed.listing.media_json = json.dumps({'images':['https://example.test/own.jpg']})
    db.session.commit()
    public = Service.get_item(**seed.scope)
    assert public['listing']['id'] == seed.listing.id
    assert public['listing']['image'] == 'https://example.test/own.jpg'
    assert public['listing']['url'].endswith('?account_id=' + str(seed.account.id))
    assert 'price' not in public['listing']


def test_foreign_listing_is_hidden_from_display_search_and_ai(seed):
    seed.listing.seller_id = seed.other_seller.id
    seed.listing.account_id = seed.other_account.id
    seed.listing.title = 'Secret FOREIGN product'
    seed.listing.description = 'Private facts'
    db.session.commit()
    public = Service.get_item(**seed.scope)
    assert public['listing'] is None and public['listing_id'] is None and public['match_status'] == 'unavailable'
    result = Service.list_items(seller_id=seed.seller.id, account_id=seed.account.id,
        source_kind='review', search='FOREIGN', today=date(2026, 7, 15))
    assert not result['items']
    prompts = []
    created = Service.create_reply_draft(**seed.scope, generation_mode='ai',
        generator=lambda _system, message: prompts.append(message) or 'Спасибо за отзыв.')
    assert json.loads(prompts[0])['FACTS'] == {'listing_matched':False}
    assert created.listing_id is None
    assert 'Secret' not in prompts[0]


def test_literal_search_does_not_turn_percent_or_underscore_into_wildcards(seed):
    for search in ['%', '_', '\\']:
        result = Service.list_items(seller_id=seed.seller.id, account_id=seed.account.id,
            source_kind='review', search=search, today=date(2026,7,15))
        assert result['pagination']['total'] == 0
    seed.item.text = 'Материал 100%'; db.session.commit()
    result = Service.list_items(seller_id=seed.seller.id, account_id=seed.account.id,
        source_kind='review', search='100%', today=date(2026,7,15))
    assert result['pagination']['total'] == 1


def test_local_save_appends_version_without_provider_or_ai(seed):
    previous = draft(seed)
    with patch('services.marketplace_inbox.get_marketplace_registry') as provider, patch.object(Service,'_ai_draft') as ai:
        current = save(seed, previous)
    provider.assert_not_called(); ai.assert_not_called()
    assert current.id != previous.id and current.status == 'draft'
    assert db.session.get(MarketplaceReplyDraft, previous.id).status == 'superseded'
    assert current.source_fingerprint == seed.item.source_fingerprint
    assert current.content_hash != previous.content_hash
    assert Service.get_item(**seed.scope)['draft']['id'] == current.id
    with pytest.raises(MarketplaceInboxConflict):
        save(seed, previous)
    assert MarketplaceReplyDraft.query.count() == 2
    assert MarketplaceReplyDraft.query.filter_by(status='draft').one().id == current.id


@pytest.mark.parametrize('changed', ['source', 'facts', 'foreign_listing', 'expired', 'ineligible'])
def test_drift_rolls_back_claim_and_keeps_prior_text(seed, changed):
    previous = draft(seed); old_text = previous.text
    if changed == 'source': seed.item.source_fingerprint = 'b'*64
    if changed == 'facts': seed.listing.description = 'Новые факты'
    if changed == 'foreign_listing': seed.listing.account_id = seed.other_account.id
    if changed == 'expired': seed.item.published_at = datetime(2025,1,1)
    if changed == 'ineligible': seed.item.reply_eligible = False
    db.session.commit()
    with pytest.raises(MarketplaceInboxConflict): save(seed, previous)
    db.session.expire_all()
    assert db.session.get(MarketplaceReplyDraft,previous.id).status == 'draft'
    assert db.session.get(MarketplaceReplyDraft,previous.id).text == old_text
    assert MarketplaceReplyDraft.query.count() == 1


def test_stale_create_version_rejected_before_paid_generation(seed):
    current = draft(seed)
    with patch.object(Service,'_ai_draft') as ai, pytest.raises(MarketplaceInboxConflict):
        Service.create_reply_draft(**seed.scope, generation_mode='ai', expected_draft_id=None)
    ai.assert_not_called()
    assert MarketplaceReplyDraft.query.filter_by(status='draft').one().id == current.id


def test_manual_save_wins_over_generation_already_in_flight(seed):
    previous = draft(seed); saved = []
    def generator(_system, _message):
        saved.append(save(seed, previous).id)
        return 'Другой подготовленный ответ.'
    with pytest.raises(MarketplaceInboxConflict):
        Service.create_reply_draft(**seed.scope, generation_mode='ai', generator=generator, expected_draft_id=previous.id)
    assert MarketplaceReplyDraft.query.filter_by(status='draft').one().id == saved[0]
    assert MarketplaceReplyDraft.query.count() == 2


def test_foreign_scope_author_and_unsafe_text_do_not_change_draft(seed):
    previous = draft(seed)
    with pytest.raises(MarketplaceInboxNotFound):
        Service.save_reply_draft(seller_id=seed.other_seller.id, account_id=seed.other_account.id,
            item_id=seed.item.id, draft_id=previous.id, expected_content_hash=previous.content_hash,
            text='Ответ', created_by_user_id=seed.other_user.id)
    with pytest.raises(MarketplaceInboxNotFound):
        Service.save_reply_draft(**seed.scope, draft_id=previous.id, expected_content_hash=previous.content_hash,
            text='Ответ', created_by_user_id=seed.other_user.id)
    with pytest.raises(MarketplaceReplyGenerationError): save(seed, previous, '<script>bad</script>')
    assert MarketplaceReplyDraft.query.filter_by(status='draft').one().id == previous.id


def test_foreign_draft_relation_not_exposed(seed):
    previous = draft(seed)
    previous.seller_id=seed.other_seller.id; previous.account_id=seed.other_account.id
    db.session.commit()
    assert Service.get_item(**seed.scope)['draft'] is None
    result=Service.list_items(seller_id=seed.seller.id,account_id=seed.account.id,source_kind='review',today=date(2026,7,15))
    assert result['items'][0]['draft'] is None


def test_expired_detail_is_unavailable(seed):
    seed.item.published_at=datetime(2025,1,1);db.session.commit()
    with pytest.raises(MarketplaceInboxNotFound):Service.get_item(**seed.scope)


@pytest.mark.parametrize('field,value,search', [
    ('text','Прекрасный МАТЕРИАЛ','материал'),
    ('text','Прекрасный материал','МАТЕРИАЛ'),
    ('title','Синий Набор','СИНИЙ'),
    ('offer_id','АРТ_100%','арт_100%'),
    ('text','Straße №１','STRASSE No1'),
])
def test_unicode_casefold_search_preserves_display_query_and_exact_scope(seed, field, value, search):
    setattr(seed.item if field == 'text' else seed.listing, field, value)
    db.session.commit()
    result = Service.list_items(seller_id=seed.seller.id, account_id=seed.account.id,
        source_kind='review', search=search, today=date(2026,7,15))
    assert result['pagination']['total'] == 1
    assert result['filters']['search'] == search
