"""Seller-owned decisions and write fences. No credentials or provider I/O.

Mutation methods own one short transaction under the same account flock as all
Ozon writes. Fence lookups at physical boundaries run under the caller's flock;
they never commit or acquire a second lock. Provider outcome is not editable.
"""
from contextlib import contextmanager
from datetime import datetime
import hmac
import json
import re
import unicodedata

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from models import (db, Marketplace, MarketplaceOperation, MarketplaceMediaOperation, MarketplaceListing,
    MarketplaceListingSnapshot, MarketplaceWriteQuarantine as Hold,
    MarketplaceWriteQuarantineEvent as Event, SellerMarketplaceAccount, Seller, User)
from services.marketplace_operation_locks import try_account_operation_lock
from services.ozon_quarantine_scope import (QuarantineScope, operation_scope, media_scope,
    incoming_scope, offer_id, product_id, _object, _Unknown, PRODUCT_KINDS, COMMERCIAL_KINDS)
from services.ozon_product_state import OzonProductStateContract, OzonProductStateError
from services.ozon_commercial_contracts import OzonPriceContract, OzonStockContract, OzonCommercialContractError


class QuarantineError(RuntimeError):
    code = 'invalid_write_quarantine'
    status_code = 400


class QuarantineNotFound(QuarantineError):
    code = 'write_quarantine_not_found'
    status_code = 404


class QuarantineForbidden(QuarantineError):
    code = 'write_quarantine_forbidden'
    status_code = 403


class QuarantineConflict(QuarantineError):
    code = 'write_quarantine_conflict'
    status_code = 409


class QuarantineBusy(QuarantineConflict):
    code = 'write_quarantine_busy'


def _positive(value):
    if type(value) is not int or not 0 < value <= 2**63-1:
        raise QuarantineError('Не удалось определить операцию или просмотренную версию.')
    return value


def _reason(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 1000 or len(value.strip()) < 10:
        raise QuarantineError('Укажите причину: от 10 до 1000 символов.')
    if any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} and c not in '\n\t' for c in value):
        raise QuarantineError('Причина содержит недопустимые управляющие символы.')
    return value.strip()


def _owned(seller_id, origin_id, origin_type='operation'):
    _positive(seller_id)
    _positive(origin_id)
    if not isinstance(origin_type, str) or origin_type not in {'operation', 'media_operation'}:
        raise QuarantineError('Неизвестный вид операции.')
    model = MarketplaceOperation if origin_type == 'operation' else MarketplaceMediaOperation
    operation = model.query.filter_by(id=origin_id, seller_id=seller_id).first()
    account = None if operation is None else SellerMarketplaceAccount.query.join(
        Marketplace, Marketplace.id == SellerMarketplaceAccount.marketplace_id).filter(
        SellerMarketplaceAccount.id == operation.account_id,
        SellerMarketplaceAccount.seller_id == seller_id, Marketplace.code == 'ozon').first()
    if (account is None or
            (origin_type == 'operation' and operation.marketplace_id != account.marketplace_id) or
            (origin_type == 'media_operation' and operation.marketplace_code != 'ozon')):
        raise QuarantineNotFound('Операция Ozon не найдена.')
    return operation, account


def _snapshot(operation):
    return MarketplaceListingSnapshot.query.filter_by(operation_id=operation.id,
        seller_id=operation.seller_id, marketplace_id=operation.marketplace_id,
        account_id=operation.account_id).first()


def _scope(operation, account, origin_type):
    if origin_type == 'media_operation':
        return media_scope(account_id=account.id, external_item_id=operation.external_item_id,
                           target_json=operation.target_json)
    snapshot = _snapshot(operation)
    return operation_scope(kind=operation.operation_kind, summary_json=operation.request_summary_json,
        submitted_json=snapshot.submitted_state_json if snapshot else None,
        before_json=snapshot.before_state_json if snapshot else None)


def _holds(account):
    return Hold.query.filter_by(seller_id=account.seller_id, marketplace_id=account.marketplace_id,
                                account_id=account.id)


def _origin_hold(operation, account, origin_type):
    return _holds(account).filter_by(**{
        'operation_id' if origin_type == 'operation' else 'media_operation_id': operation.id}).first()


def _token(scope, operation, account, origin_type):
    return scope.review_token(seller_id=operation.seller_id, marketplace_id=account.marketplace_id,
        account_id=account.id, origin_type=origin_type, origin_id=operation.id, version=operation.version)


def _actor(seller_id, actor_id):
    _positive(actor_id)
    actor = User.query.filter_by(id=actor_id, is_active=True, blocked_at=None).first()
    if actor is None or Seller.query.filter_by(id=seller_id, user_id=actor_id).first() is None:
        raise QuarantineForbidden('Решение может принять только действующий владелец этого магазина.')


def _clean_session():
    session = db.session()
    if session.new or session.dirty or session.deleted:
        raise QuarantineConflict('Сначала завершите текущее изменение.')
    if session.in_transaction() and db.engine.dialect.name == 'sqlite':
        if session.connection().connection.driver_connection.in_transaction:
            raise QuarantineConflict('Сначала завершите текущую транзакцию.')


@contextmanager
def _mutation(seller_id, origin_id, origin_type, actor_user_id):
    _clean_session()  # Before ANY query/autoflush; never discard caller-owned writes.
    operation, account = _owned(seller_id, origin_id, origin_type)
    account_id = account.id
    db.session.rollback()  # End only our clean read snapshot before the flock.
    claim = try_account_operation_lock(account_id)
    if claim is None:
        raise QuarantineBusy('Магазин сейчас сверяется с Ozon. Повторите через несколько секунд.')
    try:
        operation, account = _owned(seller_id, origin_id, origin_type)
        _actor(seller_id, actor_user_id)
        yield operation, account
        db.session.commit()
    except (IntegrityError, StaleDataError):
        db.session.rollback()
        raise QuarantineConflict('Решение уже изменилось. Перечитайте текущее состояние.') from None
    except Exception:
        db.session.rollback()
        raise
    finally:
        claim.close()


def _append(hold, operation, actor, action, reason, now):
    db.session.flush()  # Capture optimistic revisions after this local decision.
    db.session.add(Event(quarantine_id=hold.id, seller_id=hold.seller_id,
        marketplace_id=hold.marketplace_id, account_id=hold.account_id,
        actor_user_id=actor, action=action, reason=reason,
        quarantine_version=hold.version, operation_version=operation.version, created_at=now))


def place(*, seller_id, origin_id, expected_version, scope_token, reason, actor_user_id,
          confirm_scope, origin_type='operation', now=None):
    _positive(expected_version)
    reason = _reason(reason)
    if confirm_scope is not True or not isinstance(scope_token, str) or not re.fullmatch('[a-f0-9]{64}', scope_token):
        raise QuarantineError('Подтвердите показанную область остановки новых изменений.')
    with _mutation(seller_id, origin_id, origin_type, actor_user_id) as (operation, account):
        if operation.version != expected_version or _origin_hold(operation, account, origin_type):
            raise QuarantineConflict('Операция или решение уже изменились. Перечитайте текущее состояние.')
        if operation.status != 'uncertain' or operation.attempt_count != 1:
            raise QuarantineConflict('Остановка доступна только для отправленной операции с неизвестным результатом.')
        scope = _scope(operation, account, origin_type)
        if not hmac.compare_digest(scope_token, _token(scope, operation, account, origin_type)):
            raise QuarantineConflict('Область остановки изменилась. Перечитайте её перед подтверждением.')
        current = now or datetime.utcnow()
        hold = Hold(seller_id=seller_id, marketplace_id=account.marketplace_id, account_id=account.id,
            **{'operation_id' if origin_type == 'operation' else 'media_operation_id': operation.id},
            scope_kind=scope.kind, offer_id=scope.offer_id, product_id=scope.product_id,
            scope_reason=scope.reason_code, reviewed_scope_token=scope_token,
            status='active', version=1, created_at=current, updated_at=current)
        db.session.add(hold)
        # Outcome, attempt count, task and immutable snapshots are unchanged.
        # Explicit version advance is required even for an already-stopped row.
        operation.version += 1
        if origin_type == 'operation':
            operation.quota_reserved = 0
            operation.next_poll_at = None
        else:
            operation.next_reconcile_at = None
        _append(hold, operation, actor_user_id, 'placed', reason, current)
        identity = hold.id
    return identity


def _proven_outcome(operation, hold, origin_type):
    """Accept only original-kind evidence produced by existing reconciliation.

    This does not infer success from a newer operation or a current listing.
    There is no proven standalone Ozon media write/reconciliation contract yet.
    """
    if (origin_type != 'operation' or operation.attempt_count != 1 or
            operation.status not in {'succeeded', 'failed'} or not operation.completed_at or
            operation.completed_at < hold.created_at or operation.next_poll_at is not None):
        return False
    snapshot = _snapshot(operation)
    if snapshot is None or not re.fullmatch('[a-f0-9]{64}', snapshot.confirmed_fingerprint or ''):
        return False
    try:
        state = _object(snapshot.confirmed_state_json)
        scope = _scope(operation, operation.account, origin_type)
        if scope.kind != 'product':
            return False
        if hold.scope_kind == 'product' and (scope.offer_id != hold.offer_id or scope.product_id != hold.product_id):
            return False
        if operation.operation_kind in COMMERCIAL_KINDS:
            from services.marketplace_commercial import MarketplaceCommercialService
            kind = 'price' if operation.operation_kind.startswith('price_') else 'stock'
            if state.get('kind') != kind:
                return False
            if kind == 'price':
                if state.get('currency_code') != 'RUB':
                    return False
                # Historical attempted fractional prices remain readable; the
                # current whole-RUB write policy must not rewrite their proof.
                OzonPriceContract.money(state.get('price'), 'price')
                OzonPriceContract.money(state.get('old_price'), 'old_price', allow_zero=True)
                OzonPriceContract.money(state.get('min_price'), 'min_price', allow_zero=True)
            else:
                OzonStockContract.build_item(**{key: state.get(key) for key in ('offer_id', 'product_id', 'warehouse_id', 'stock')})
                product_id(state.get('sku'))
            return (operation.status == 'succeeded' and not operation.error_code and
                offer_id(state.get('offer_id')) == scope.offer_id and product_id(state.get('product_id')) == scope.product_id and
                MarketplaceCommercialService._fingerprint(state) == snapshot.confirmed_fingerprint == snapshot.submitted_fingerprint and
                state == _object(snapshot.submitted_state_json))
        if operation.operation_kind in PRODUCT_KINDS and operation.status == 'failed':
            from services.ozon_product_import import OzonProductImportContract
            result = state.get('result')
            items = result.get('items') if isinstance(result, dict) else None
            return (operation.error_code == 'ozon_import_failed' and bool(operation.external_task_id) and
                state.get('source') == 'task_status' and bool(state.get('confirmed_at')) and
                isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) and
                offer_id(items[0].get('offer_id')) == scope.offer_id and
                result.get('aggregate_status') == 'failed' and items[0].get('status') in {'failed', 'skipped'} and
                OzonProductImportContract.fingerprint(result) == snapshot.confirmed_fingerprint)
        if operation.status != 'succeeded' or operation.error_code:
            return False
        if operation.operation_kind == 'product_import_rollback':
            from services.ozon_product_import import OzonProductImportContract
            return (state.get('archived') is True and bool(state.get('confirmed_at')) and
                offer_id(state.get('offer_id')) == scope.offer_id and product_id(state.get('product_id')) == scope.product_id and
                OzonProductImportContract.fingerprint(state) == snapshot.confirmed_fingerprint)
        if operation.operation_kind in PRODUCT_KINDS:
            return _publication_success(operation, snapshot, scope, state)
    except (ValueError, TypeError, KeyError, AttributeError, OzonProductStateError, OzonCommercialContractError):
        return False
    return False


def _publication_success(operation, snapshot, scope, state):
    """Reconstruct the committed confirmed state, never the current listing."""
    from services.ozon_product_import import OzonProductImportContract
    sources = ({'task_status', 'live_offer_reconciliation', 'task_status_and_live_state',
        'live_full_state_reconciliation'} if operation.operation_kind == 'product_import'
        else {'task_status_and_live_state', 'live_full_state_reconciliation'})
    items = _object('{"items":' + (operation.item_results_json or 'null') + '}').get('items')
    if (state.get('source') not in sources or state.get('status') != 'imported' or
            datetime.fromisoformat(state.get('confirmed_at', '')) != operation.completed_at or
            not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or
            items[0].get('status') != 'imported' or items[0].get('errors') != []):
        return False
    item = items[0]
    if (offer_id(state.get('offer_id')) != scope.offer_id or offer_id(item.get('offer_id')) != scope.offer_id or
            product_id(state.get('external_product_id')) != product_id(item.get('product_id')) or
            (scope.product_id is not None and product_id(item.get('product_id')) != scope.product_id)):
        return False
    reconstructed = OzonProductStateContract.from_listing_projection({
        'product_id': state.get('external_product_id'), 'offer_id': state.get('offer_id'),
        'category_id': state.get('external_category_id'), 'type_id': state.get('external_type_id'),
        'title': state.get('title'), 'attributes': state.get('attributes'),
        'complex_attributes': state.get('complex_attributes'), 'media': state.get('media'),
        'dimensions': state.get('dimensions'), 'barcodes': state.get('barcodes'), 'price': state.get('price_summary')})
    if operation.operation_kind == 'product_import':
        # Create stores the complete listing-state hash; full readback must
        # also reconstruct the exact submitted payload, including media.
        if OzonProductImportContract.fingerprint(state) != snapshot.confirmed_fingerprint:
            return False
        if state['source'] in {'task_status', 'live_offer_reconciliation'}:
            return True  # Historical committed create evidence.
    elif reconstructed['fingerprint'] != snapshot.confirmed_fingerprint:
        return False
    submitted = _object(snapshot.submitted_state_json)
    submitted_fingerprint = OzonProductStateContract.fingerprint(submitted)
    if operation.operation_kind == 'product_import':
        if OzonProductImportContract.fingerprint(submitted) != operation.request_fingerprint:
            return False
    elif submitted_fingerprint != operation.request_fingerprint:
        return False
    if reconstructed['fingerprint'] == submitted_fingerprint:
        return True
    # Finalization advances the mutable draft revision, so re-running its
    # pre-write schema gate would reject already-proven outcomes. Use only the
    # narrow admission recorded by that typed workflow, and independently
    # re-check every byte of the two committed canonical payloads here.
    if not operation.external_task_id or state['source'] != 'task_status_and_live_state':
        return False
    summary = _object(operation.request_summary_json)
    omitted = summary.get('provider_roundtrip_omitted_attribute_ids', [])
    canonicalized = summary.get('provider_roundtrip_offer_id_canonicalized_attribute_ids', [])
    if (not isinstance(omitted, list) or omitted not in ([], ['8229']) or
            not isinstance(canonicalized, list) or canonicalized not in ([], ['9024']) or
            not (omitted or canonicalized)):
        return False
    adjustments = OzonProductStateContract.provider_roundtrip_adjustments(submitted, reconstructed['payload'],
        omittable_simple_attribute_ids=omitted, offer_id_canonicalized_simple_attribute_ids=canonicalized)
    return bool(adjustments and list(adjustments['omitted_attribute_ids']) == omitted and
                list(adjustments['offer_id_canonicalized_attribute_ids']) == canonicalized)


def update_decision(*, seller_id, origin_id, expected_version, expected_operation_version,
                    action, reason, actor_user_id, confirm_release=False, origin_type='operation', now=None):
    _positive(expected_version)
    _positive(expected_operation_version)
    reason = _reason(reason)
    if not isinstance(action, str) or action not in {'note_added', 'released'} or (action == 'released' and confirm_release is not True):
        raise QuarantineError('Подтвердите выбранное действие.')
    with _mutation(seller_id, origin_id, origin_type, actor_user_id) as (operation, account):
        hold = _origin_hold(operation, account, origin_type)
        if (hold is None or hold.version != expected_version or
                operation.version != expected_operation_version):
            raise QuarantineConflict('Операция или решение изменились. Перечитайте текущее состояние.')
        if action == 'released' and (hold.status != 'active' or not _proven_outcome(operation, hold, origin_type)):
            raise QuarantineConflict('Исход исходной операции ещё не подтверждён. Запрет остаётся в силе.')
        current = now or datetime.utcnow()
        hold.version += 1
        hold.updated_at = current
        if action == 'released':
            hold.status = 'released'
            hold.released_at = current
        _append(hold, operation, actor_user_id, action, reason, current)
        identity = hold.id
    return identity


def _iso(value):
    return value.isoformat() + 'Z' if value else None


def preview(*, seller_id, origin_id, viewer_user_id, origin_type='operation', before_id=None):
    operation, account = _owned(seller_id, origin_id, origin_type)
    _actor(seller_id, viewer_user_id)
    if before_id is not None:
        _positive(before_id)
    hold = _origin_hold(operation, account, origin_type)
    scope = _scope(operation, account, origin_type)
    events, next_id = [], None
    if hold:
        scope = QuarantineScope(hold.scope_kind, hold.offer_id, hold.product_id, hold.scope_reason)
        query = Event.query.filter_by(quarantine_id=hold.id, seller_id=seller_id,
            marketplace_id=account.marketplace_id, account_id=account.id)
        if before_id is not None:
            query = query.filter(Event.id < before_id)
        rows = query.order_by(Event.id.desc()).limit(31).all()
        events = [{'id': row.id, 'action': row.action, 'reason': row.reason,
            'actor': 'Вы' if row.actor_user_id == viewer_user_id else 'Инициатор не записан' if row.actor_user_id is None else 'Другой пользователь',
            'created_at': _iso(row.created_at)} for row in rows[:30]]
        next_id = rows[29].id if len(rows) > 30 else None
    return {'origin_type': origin_type, 'operation_id': operation.id,
        'operation_version': operation.version, 'operation_kind': operation.operation_kind,
        'outcome': operation.status, 'attempt_count': operation.attempt_count,
        'created_at': _iso(operation.created_at), 'completed_at': _iso(operation.completed_at),
        'last_checked_at': _iso(getattr(operation, 'last_polled_at', None)),
        'account': {'id': account.id, 'label': account.label},
        'scope': scope.document(), 'scope_token': None if hold else _token(scope, operation, account, origin_type),
        'can_place': hold is None and operation.status == 'uncertain' and operation.attempt_count == 1,
        'hold': None if hold is None else {'id': hold.id, 'status': hold.status, 'version': hold.version,
            'created_at': _iso(hold.created_at), 'released_at': _iso(hold.released_at)},
        'can_release': bool(hold and hold.status == 'active' and _proven_outcome(operation, hold, origin_type)),
        'events': events, 'next_before_id': next_id,
        'blocking_hold': hold_document(operation_hold(operation)) if origin_type == 'operation' else None}


def matching_hold(*, seller_id, marketplace_id, account_id, scope):
    """Caller owns the account lock at a write boundary. No write/commit/I/O.

    Both identities match independently: reusing an offer or renaming a known
    provider product cannot bypass the fence. Unknown incoming identity cannot
    exclude any active fence in this account. An account fence matches all.
    """
    query = Hold.query.filter_by(seller_id=seller_id, marketplace_id=marketplace_id,
                                account_id=account_id, status='active')
    if scope.kind == 'product':
        conditions = [Hold.scope_kind == 'account', Hold.offer_id == scope.offer_id]
        if scope.product_id is not None:
            conditions.append(Hold.product_id == scope.product_id)
        query = query.filter(or_(*conditions))
    return query.order_by(Hold.id).first()


def operation_hold(operation):
    """Fence a never-attempted operation from its own committed snapshot."""
    if operation.attempt_count != 0:
        return None  # Already-attempted reads must remain available.
    snapshot = _snapshot(operation)
    scope = incoming_scope(kind=operation.operation_kind, summary_json=operation.request_summary_json,
        submitted_json=snapshot.submitted_state_json if snapshot else None)
    return matching_hold(seller_id=operation.seller_id, marketplace_id=operation.marketplace_id,
                          account_id=operation.account_id, scope=scope)


def hold_message(hold):
    target = 'магазина' if hold.scope_kind == 'account' else 'товара'
    return f'Новые изменения {target} остановлены решением №{hold.id}. Откройте разбор исходной операции.'


def hold_document(hold):
    if hold is None:
        return None
    return {'id': hold.id, 'scope': hold.scope_kind, 'status': hold.status,
        'operation_id': hold.operation_id, 'media_operation_id': hold.media_operation_id,
        'review_url': f'/marketplaces/operations/{hold.operation_id}/review' if hold.operation_id else None}


def proposal_hold(proposal):
    try:
        before, proposed = map(_object, (proposal.baseline_state_json, proposal.proposed_state_json))
        summary = json.dumps({'offer_id': before.get('offer_id'), 'before': before, 'proposed': proposed})
        scope = incoming_scope(kind=f'{proposal.proposal_kind}_update', summary_json=summary,
                               submitted_json=proposal.proposed_state_json)
    except _Unknown:
        scope = QuarantineScope('account', None, None, 'document_invalid')
    return matching_hold(seller_id=proposal.seller_id, marketplace_id=proposal.marketplace_id,
                        account_id=proposal.account_id, scope=scope)


def draft_hold(draft):
    """Read-only editor hint; the physical gate uses committed write identity."""
    try:
        offer = offer_id(draft.offer_id)
        listing = MarketplaceListing.query.filter_by(id=draft.published_listing_id,
            seller_id=draft.seller_id, marketplace_id=draft.marketplace_id, account_id=draft.account_id).first()
        product = product_id(listing.external_product_id) if listing else None
        scope = QuarantineScope('product', offer, product, 'immutable_target_verified')
    except _Unknown:
        scope = QuarantineScope('account', None, None, 'identity_unknown')
    return matching_hold(seller_id=draft.seller_id, marketplace_id=draft.marketplace_id,
                        account_id=draft.account_id, scope=scope)


def automatic_reconciliation_allowed():
    """SQL predicate applied BEFORE a due queue's bounded selection."""
    active = db.session.query(Hold.id).filter(Hold.seller_id == MarketplaceOperation.seller_id,
        Hold.marketplace_id == MarketplaceOperation.marketplace_id,
        Hold.account_id == MarketplaceOperation.account_id, Hold.operation_id == MarketplaceOperation.id,
        Hold.status == 'active').exists()
    return or_(MarketplaceOperation.attempt_count == 0, ~active)


def has_origin_hold(operation):
    return Hold.query.filter_by(seller_id=operation.seller_id, marketplace_id=operation.marketplace_id,
        account_id=operation.account_id, operation_id=operation.id, status='active').first() is not None


def keep_reconciliation_stopped(operation):
    """A manual read may establish an outcome, but cannot restart polling."""
    if has_origin_hold(operation) and operation.next_poll_at is not None:
        operation.next_poll_at = None
        db.session.commit()
