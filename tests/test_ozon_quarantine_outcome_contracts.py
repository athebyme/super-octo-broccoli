"""Release evidence from the actual Ozon publication and commercial workflows.

These tests use the existing synthetic provider fixtures, never a real account.
They intentionally place a decision after attempt 1 and then run the workflow's
ordinary read reconciliation against that same original operation.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json

import pytest

from models import db, MarketplaceOperation, SellerMarketplaceAccount
from services import ozon_write_quarantine as quarantine
from services.ozon_quarantine_scope import QuarantineScope
from services.marketplace_publications import MarketplacePublicationService
from services.marketplace_commercial import MarketplaceCommercialService
from services.ozon_api_client import OzonAmbiguousWriteError
from services.ozon_product_import import OzonProductImportContract
from services.ozon_product_state import OzonProductStateContract
from tests import test_marketplace_publications as publication_fixtures
from tests import test_marketplace_commercial as commercial_fixtures


@contextmanager
def _case(case_type):
    case = case_type(next(name for name in dir(case_type) if name.startswith('test_')))
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _place(case, operation):
    viewed = quarantine.preview(seller_id=case.seller.id, origin_id=operation.id,
                                 viewer_user_id=case.user.id)
    assert viewed['can_place']
    hold_id = quarantine.place(seller_id=case.seller.id, origin_id=operation.id,
        expected_version=viewed['operation_version'], scope_token=viewed['scope_token'],
        reason='Awaiting exact original provider outcome', actor_user_id=case.user.id,
        confirm_scope=True)
    return hold_id


def _can_release(case, operation):
    return quarantine.preview(seller_id=case.seller.id, origin_id=operation.id,
        viewer_user_id=case.user.id)['can_release']


def _projection(state):
    return {'product_id': state['external_product_id'], 'offer_id': state['offer_id'],
        'category_id': state['external_category_id'], 'type_id': state['external_type_id'],
        'title': state['title'], 'attributes': state['attributes'],
        'complex_attributes': state['complex_attributes'], 'media': state['media'],
        'dimensions': state['dimensions'], 'barcodes': state['barcodes'],
        'price': state['price_summary']}


def test_real_create_live_reconciliation_proves_original_then_rejects_corruption():
    with _case(publication_fixtures.MarketplacePublicationServiceTest) as case:
        adapter = publication_fixtures.SyntheticPublicationAdapter(ambiguous=True)
        operation = case.start(adapter, key='quarantine-create-001')
        assert operation.status == 'uncertain' and operation.attempt_count == 1
        _place(case, operation)
        completed = MarketplacePublicationService.poll_operation(
            seller_id=case.seller.id, operation_id=operation.id,
            adapter=adapter, credentials=publication_fixtures.SYNTHETIC_CREDENTIALS,
            now=datetime.utcnow() + timedelta(seconds=1))
        assert completed.status == 'succeeded'
        assert completed.snapshot.confirmed_fingerprint
        assert _can_release(case, completed)
        assert len(adapter.submitted_payloads) == 1  # readback did not send a second write

        saved_state = completed.snapshot.confirmed_state_json
        original = completed.snapshot.confirmed_fingerprint
        changed = json.loads(saved_state)
        changed['media']['images'] = ['https://img.test/other.jpg']
        completed.snapshot.confirmed_state_json = json.dumps(changed)
        completed.snapshot.confirmed_fingerprint = OzonProductImportContract.fingerprint(changed)
        db.session.commit()
        assert not _can_release(case, completed)
        completed.snapshot.confirmed_state_json = saved_state
        completed.snapshot.confirmed_fingerprint = original
        db.session.commit()
        assert _can_release(case, completed)

        completed.snapshot.confirmed_fingerprint = '0' * 64
        db.session.commit()
        assert not _can_release(case, completed)
        completed.snapshot.confirmed_fingerprint = original
        db.session.commit()
        assert _can_release(case, completed)

        original_results = completed.item_results_json
        results = json.loads(original_results)
        results[0]['product_id'] = '999999'
        completed.item_results_json = json.dumps(results)
        db.session.commit()
        assert not _can_release(case, completed)
        completed.item_results_json = original_results
        db.session.commit()

        # The current draft is not origin identity; its edits cannot change
        # the reviewed hold or become proof of another product.
        case.draft.offer_id = 'other-draft-offer'
        db.session.commit()
        assert _can_release(case, completed)
        with pytest.raises(quarantine.QuarantineNotFound):
            quarantine.preview(seller_id=case.foreign_seller.id,
                origin_id=completed.id, viewer_user_id=case.foreign_user.id)
        other_account = SellerMarketplaceAccount(seller_id=case.seller.id,
            marketplace_id=case.marketplace.id, external_account_id='different-client',
            label='Independent account')
        db.session.add(other_account)
        db.session.commit()
        scope = QuarantineScope('product', 'safe-offer', '987654', 'immutable_target_verified')
        assert quarantine.matching_hold(seller_id=case.seller.id,
            marketplace_id=case.marketplace.id, account_id=case.account.id,
            scope=scope) is not None
        assert quarantine.matching_hold(seller_id=case.seller.id,
            marketplace_id=case.marketplace.id, account_id=other_account.id,
            scope=scope) is None


def test_real_task_create_full_readback_proves_original_media_only():
    with _case(publication_fixtures.MarketplacePublicationServiceTest) as case:
        adapter = publication_fixtures.SyntheticPublicationAdapter()
        operation = case.start(adapter, key='quarantine-task-create-001')
        assert operation.status == 'submitted' and operation.attempt_count == 1
        operation.status = 'uncertain'
        operation.next_poll_at = None
        db.session.commit()
        _place(case, operation)
        completed = MarketplacePublicationService.poll_operation(
            seller_id=case.seller.id, operation_id=operation.id,
            adapter=adapter, credentials=publication_fixtures.SYNTHETIC_CREDENTIALS,
            now=datetime.utcnow() + timedelta(seconds=1))
        assert completed.status == 'succeeded'
        saved_state = completed.snapshot.confirmed_state_json
        saved_fingerprint = completed.snapshot.confirmed_fingerprint
        assert json.loads(saved_state)['source'] == 'task_status_and_live_state'
        assert _can_release(case, completed)
        assert len(adapter.submitted_payloads) == 1

        changed = json.loads(saved_state)
        changed['media']['images'] = ['https://img.test/other.jpg']
        completed.snapshot.confirmed_state_json = json.dumps(changed)
        completed.snapshot.confirmed_fingerprint = OzonProductImportContract.fingerprint(changed)
        db.session.commit()
        assert not _can_release(case, completed)
        completed.snapshot.confirmed_state_json = saved_state
        completed.snapshot.confirmed_fingerprint = saved_fingerprint
        db.session.commit()
        assert _can_release(case, completed)


def test_real_full_state_update_requires_exact_reconstructed_payload():
    with _case(publication_fixtures.MarketplacePublicationServiceTest) as case:
        prior = case.prior_payload()
        case.attach_listing(prior)
        adapter = publication_fixtures.SyntheticFullStateAdapter(prior)
        operation = case.start_update(adapter, key='quarantine-update-001')
        assert operation.status == 'submitted' and operation.attempt_count == 1
        operation.status = 'uncertain'
        operation.next_poll_at = None
        db.session.commit()
        _place(case, operation)
        completed = MarketplacePublicationService.poll_operation(
            seller_id=case.seller.id, operation_id=operation.id,
            adapter=adapter, credentials=publication_fixtures.SYNTHETIC_CREDENTIALS,
            now=datetime.utcnow() + timedelta(seconds=1))
        assert completed.status == 'succeeded'
        assert _can_release(case, completed)
        assert len(adapter.submitted_payloads) == 1

        saved = completed.snapshot.confirmed_state_json
        state = json.loads(saved)
        state['media']['images'] = ['https://img.test/other.jpg']
        completed.snapshot.confirmed_state_json = json.dumps(state)
        db.session.commit()
        assert not _can_release(case, completed)
        completed.snapshot.confirmed_state_json = saved
        db.session.commit()
        assert _can_release(case, completed)

        # A conflicting committed before identity changes the server-owned
        # scope to account-wide; the original product release must fail.
        before = json.loads(completed.snapshot.before_state_json)
        before['identity']['product_id'] = '987655'
        completed.snapshot.before_state_json = json.dumps(before)
        db.session.commit()
        assert not _can_release(case, completed)


def test_real_task_confirmed_provider_attribute_omission_still_proves_update():
    with _case(publication_fixtures.MarketplacePublicationServiceTest) as case:
        case.enable_import_only_type_attribute()
        prior = case.prior_payload()
        prior['items'][0]['attributes'] = [
            item for item in prior['items'][0]['attributes'] if item['id'] != 8229]
        case.attach_listing(prior)
        adapter = publication_fixtures.SyntheticTypeOmittingAdapter(prior)
        operation = case.start_update(adapter, key='quarantine-roundtrip-001')
        assert operation.status == 'submitted' and operation.attempt_count == 1
        operation.status = 'uncertain'
        operation.next_poll_at = None
        db.session.commit()
        _place(case, operation)
        completed = MarketplacePublicationService.poll_operation(
            seller_id=case.seller.id, operation_id=operation.id,
            adapter=adapter, credentials=publication_fixtures.SYNTHETIC_CREDENTIALS,
            now=datetime.utcnow() + timedelta(seconds=1))
        assert completed.status == 'succeeded'
        assert completed.snapshot.confirmed_fingerprint != completed.request_fingerprint
        assert _can_release(case, completed)
        assert len(adapter.submitted_payloads) == 1

        original_summary = completed.request_summary_json
        summary = json.loads(original_summary)
        for claimed_ids in ([], ['99999'], ['8229', '99999']):
            summary['provider_roundtrip_omitted_attribute_ids'] = claimed_ids
            completed.request_summary_json = json.dumps(summary)
            db.session.commit()
            assert not _can_release(case, completed)
        completed.request_summary_json = original_summary
        db.session.commit()
        assert _can_release(case, completed)

        # Even a matching new fingerprint cannot excuse an unrelated changed
        # field under the recorded import-only attribute exception.
        original_state = completed.snapshot.confirmed_state_json
        original_fingerprint = completed.snapshot.confirmed_fingerprint
        state = json.loads(original_state)
        state['title'] = 'Unrelated provider title drift'
        completed.snapshot.confirmed_state_json = json.dumps(state)
        completed.snapshot.confirmed_fingerprint = (
            OzonProductStateContract.from_listing_projection(_projection(state))['fingerprint'])
        db.session.commit()
        assert not _can_release(case, completed)
        completed.snapshot.confirmed_state_json = original_state
        completed.snapshot.confirmed_fingerprint = original_fingerprint
        db.session.commit()
        assert _can_release(case, completed)


def test_real_failed_task_is_releaseable_only_for_original_failed_item():
    with _case(publication_fixtures.MarketplacePublicationServiceTest) as case:
        adapter = publication_fixtures.SyntheticPublicationAdapter(status='failed')
        operation = case.start(adapter, key='quarantine-failed-001')
        assert operation.status == 'submitted' and operation.attempt_count == 1
        operation.status = 'uncertain'
        operation.next_poll_at = None
        db.session.commit()
        _place(case, operation)
        completed = MarketplacePublicationService.poll_operation(
            seller_id=case.seller.id, operation_id=operation.id,
            adapter=adapter, credentials=publication_fixtures.SYNTHETIC_CREDENTIALS,
            now=datetime.utcnow() + timedelta(seconds=1))
        assert completed.status == 'failed' and completed.error_code == 'ozon_import_failed'
        assert _can_release(case, completed)
        assert len(adapter.submitted_payloads) == 1

        saved = completed.snapshot.confirmed_state_json
        state = json.loads(saved)
        state['result']['items'][0]['offer_id'] = 'another-offer'
        completed.snapshot.confirmed_state_json = json.dumps(state)
        db.session.commit()
        assert not _can_release(case, completed)


def test_real_ambiguous_price_reconciliation_stays_bound_to_original_operation():
    with _case(commercial_fixtures.MarketplaceCommercialServiceTest) as case:
        case.adapter.ambiguous_mode = 'no_apply'
        proposal = case.price_proposal()
        approved = case.approve(proposal)
        operation = db.session.get(MarketplaceOperation, approved.operation_id)
        assert operation.status == 'uncertain' and operation.attempt_count == 1
        _place(case, operation)
        case.adapter.price = json.loads(proposal.proposed_state_json)['price']
        completed = MarketplaceCommercialService.poll_operation(
            seller_id=case.seller.id, operation_id=operation.id,
            adapter=case.adapter, credentials=commercial_fixtures.SYNTHETIC_CREDENTIALS,
            now=datetime.utcnow() + timedelta(seconds=1), allow_submission=False)
        assert completed.status == 'succeeded'
        assert _can_release(case, completed)
        assert len(case.adapter.price_writes) == 1

        saved = completed.snapshot.confirmed_state_json
        state = json.loads(saved)
        state['price'] = '9999'
        completed.snapshot.confirmed_state_json = json.dumps(state)
        db.session.commit()
        assert not _can_release(case, completed)
        completed.snapshot.confirmed_state_json = saved
        db.session.commit()
        assert _can_release(case, completed)


def test_real_archive_reconciliation_proves_original_archive_only():
    with _case(publication_fixtures.MarketplacePublicationServiceTest) as case:
        adapter = publication_fixtures.SyntheticFullStateAdapter(create_mode=True)
        created = case.start(adapter, key='quarantine-archive-parent-001')
        parent = MarketplacePublicationService.poll_operation(
            seller_id=case.seller.id, operation_id=created.id,
            adapter=adapter, credentials=publication_fixtures.SYNTHETIC_CREDENTIALS)
        assert parent.status == 'succeeded'

        def ambiguous_archive(credentials, payload):
            adapter.archived = True
            adapter.archive_calls.append(payload)
            raise OzonAmbiguousWriteError('synthetic lost archive response', code='synthetic_ambiguous')

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(adapter, 'archive_products', ambiguous_archive)
            original = MarketplacePublicationService.start_create_rollback(
                seller_id=case.seller.id, operation_id=parent.id,
                expected_version=parent.version,
                idempotency_key='quarantine-archive-child-001',
                created_by_user_id=case.user.id, adapter=adapter,
                credentials=publication_fixtures.SYNTHETIC_CREDENTIALS)
        assert original.status == 'uncertain' and original.attempt_count == 1
        _place(case, original)
        assert not _can_release(case, original)
        completed = MarketplacePublicationService.poll_operation(
            seller_id=case.seller.id, operation_id=original.id,
            adapter=adapter, credentials=publication_fixtures.SYNTHETIC_CREDENTIALS,
            now=datetime.utcnow() + timedelta(seconds=1))
        assert completed.status == 'succeeded'
        assert _can_release(case, completed)
        assert len(adapter.archive_calls) == 1

        saved = completed.snapshot.confirmed_fingerprint
        completed.snapshot.confirmed_fingerprint = '0' * 64
        db.session.commit()
        assert not _can_release(case, completed)
        completed.snapshot.confirmed_fingerprint = saved
        db.session.commit()
        assert _can_release(case, completed)


def test_real_stock_readback_proves_original_warehouse_and_offer():
    with _case(commercial_fixtures.MarketplaceCommercialServiceTest) as case:
        proposal = MarketplaceCommercialService.create_stock_proposal(
            seller_id=case.seller.id, listing_id=case.listing.id,
            warehouse_id=case.warehouse.id, stock=0,
            created_by_user_id=case.user.id, adapter=case.adapter,
            credentials=commercial_fixtures.SYNTHETIC_CREDENTIALS)
        def ambiguous_stock(credentials, payload):
            case.adapter.stock_writes.append(payload)
            raise OzonAmbiguousWriteError('synthetic lost stock response', code='synthetic_ambiguous')

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(case.adapter, 'update_stocks', ambiguous_stock)
            approved = case.approve(proposal)
        operation = db.session.get(MarketplaceOperation, approved.operation_id)
        assert operation.status == 'uncertain' and operation.attempt_count == 1
        _place(case, operation)
        case.adapter.stock = 0  # Provider state becomes visible only after review.
        completed = MarketplaceCommercialService.poll_operation(
            seller_id=case.seller.id, operation_id=operation.id,
            adapter=case.adapter, credentials=commercial_fixtures.SYNTHETIC_CREDENTIALS,
            now=datetime.utcnow() + timedelta(seconds=1), allow_submission=False)
        assert completed.status == 'succeeded'
        assert _can_release(case, completed)
        assert len(case.adapter.stock_writes) == 1

        saved = completed.snapshot.confirmed_state_json
        state = json.loads(saved)
        state['warehouse_id'] = '7002'
        completed.snapshot.confirmed_state_json = json.dumps(state)
        db.session.commit()
        assert not _can_release(case, completed)
