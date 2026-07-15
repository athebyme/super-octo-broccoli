"""Publish confirmed agent content proposals to WB in one typed batch."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

from models import (
    db, AgentConversation, AgentTask, CardEditHistory, Product, Seller,
)


logger = logging.getLogger('agent_wb_content')
PUBLISHABLE_CONTENT_FIELDS = frozenset({'title', 'description'})


def _task_conversation_id(task: AgentTask) -> Optional[str]:
    value = task.get_input().get('conversation_id') if task else None
    value = str(value or '').strip()
    return value or None


def _source_task_id(history: CardEditHistory) -> Optional[str]:
    comment = str(history.user_comment or '')
    if not comment.startswith('agent_task:'):
        return None
    task_id = comment[len('agent_task:'):].strip()
    return task_id if 1 <= len(task_id) <= 64 else None


def _history_values(history: CardEditHistory, fields: Iterable[str]) -> Optional[dict]:
    snapshot = history.snapshot_after
    if not isinstance(snapshot, dict):
        return None
    values = {}
    for field in fields:
        value = snapshot.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        values[field] = value.strip()
    return values


def _result_error(product_id: int, code: str, error: str) -> dict:
    return {
        'product_id': product_id,
        'status': 'error',
        'code': code,
        'error': str(error)[:500],
    }


def _validated_wb_batch_result(payload: Any) -> tuple[set[int], set[int], dict, dict]:
    """Normalize the trusted client contract without guessing malformed IDs."""
    if not isinstance(payload, dict):
        raise ValueError('WB batch response must be an object')

    def integer_set(key: str) -> set[int]:
        raw = payload.get(key, [])
        if not isinstance(raw, list):
            raise ValueError(f'WB batch response {key} must be an array')
        values = set()
        for value in raw:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f'WB batch response {key} contains an invalid nmID')
            if value in values:
                raise ValueError(f'WB batch response {key} contains a duplicate nmID')
            values.add(value)
        return values

    def error_map(key: str) -> dict[int, str]:
        raw = payload.get(key, {})
        if not isinstance(raw, dict):
            raise ValueError(f'WB batch response {key} must be an object')
        values = {}
        for raw_id, error in raw.items():
            if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                nm_id = raw_id
            elif isinstance(raw_id, str) and raw_id.isdigit() and not raw_id.startswith('0'):
                nm_id = int(raw_id)
            else:
                raise ValueError(f'WB batch response {key} contains an invalid nmID')
            if nm_id <= 0 or nm_id in values:
                raise ValueError(f'WB batch response {key} contains an invalid nmID')
            values[nm_id] = str(error or 'неизвестная ошибка')[:1000]
        return values

    sent = integer_set('sent')
    missing = integer_set('missing')
    invalid = error_map('invalid')
    failed = error_map('failed')
    accounted = [sent, missing, set(invalid), set(failed)]
    if any(left & right for index, left in enumerate(accounted) for right in accounted[index + 1:]):
        raise ValueError('WB batch response accounts for one nmID more than once')
    return sent, missing, invalid, failed


def publish_confirmed_content_proposals(
    task: AgentTask,
    seller_id: int,
    product_ids: List[int],
    fields: List[str],
    *,
    client_factory: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Publish exact pending proposals created in the same conversation.

    No text is generated here. The proposal snapshot is the source of truth;
    current local values must still match it, otherwise a concurrent/manual
    edit blocks publication. One ``update_cards_merged`` request owns the whole
    selected batch.
    """
    requested_fields = list(fields)
    if (
        not requested_fields
        or len(set(requested_fields)) != len(requested_fields)
        or any(field not in PUBLISHABLE_CONTENT_FIELDS for field in requested_fields)
    ):
        raise ValueError('fields must be a non-empty unique title/description array')
    if (
        not isinstance(product_ids, list) or not 1 <= len(product_ids) <= 100
        or any(
            not isinstance(product_id, int) or isinstance(product_id, bool)
            or product_id <= 0
            for product_id in product_ids
        )
        or len(set(product_ids)) != len(product_ids)
    ):
        raise ValueError(
            'product_ids must contain 1..100 unique positive integers'
        )

    seller = db.session.get(Seller, seller_id)
    if not seller:
        raise LookupError('Продавец не найден')
    if not seller.wb_api_key:
        raise ValueError('API-ключ WB не настроен')

    conversation_id = _task_conversation_id(task)
    conversation = (
        AgentConversation.query.filter_by(
            id=conversation_id, seller_id=seller_id,
        ).first()
        if conversation_id else None
    )
    if not conversation:
        raise ValueError('Не удалось подтвердить текущий seller-scoped диалог')

    products = Product.query.filter(
        Product.seller_id == seller_id,
        Product.id.in_(product_ids),
    ).all()
    products_by_id = {product.id: product for product in products}

    histories = CardEditHistory.query.filter(
        CardEditHistory.seller_id == seller_id,
        CardEditHistory.product_id.in_(product_ids),
        CardEditHistory.reverted.is_(False),
        CardEditHistory.user_comment.like('agent_task:%'),
        CardEditHistory.wb_sync_status.in_((
            'pending', 'failed', 'success', 'uncertain',
        )),
        CardEditHistory.created_at >= conversation.created_at,
    ).order_by(
        CardEditHistory.created_at.desc(), CardEditHistory.id.desc(),
    ).limit(len(product_ids) * 20).all()

    source_ids = {
        source_id for history in histories
        for source_id in [_source_task_id(history)] if source_id
    }
    source_tasks = AgentTask.query.filter(
        AgentTask.id.in_(source_ids or ['']),
        AgentTask.seller_id == seller_id,
    ).all()
    valid_source_ids = {
        source.id for source in source_tasks
        if _task_conversation_id(source) == conversation_id
        and source.status == 'completed'
    }

    histories_by_product: Dict[int, List[CardEditHistory]] = {}
    for history in histories:
        if _source_task_id(history) in valid_source_ids:
            histories_by_product.setdefault(history.product_id, []).append(history)

    expected_fields = set(requested_fields)
    selected: Dict[int, tuple[Product, CardEditHistory, dict]] = {}
    results_by_id: Dict[int, dict] = {}
    for product_id in product_ids:
        product = products_by_id.get(product_id)
        if not product:
            results_by_id[product_id] = _result_error(
                product_id, 'product_not_found', 'Карточка не найдена в области продавца',
            )
            continue
        matching = next((
            history for history in histories_by_product.get(product_id, [])
            if set(history.changed_fields or []) == expected_fields
        ), None)
        if not matching:
            results_by_id[product_id] = _result_error(
                product_id,
                'proposal_not_found',
                'В этом диалоге нет подготовленного предложения с указанными полями',
            )
            continue
        values = _history_values(matching, requested_fields)
        if not values:
            results_by_id[product_id] = _result_error(
                product_id, 'proposal_invalid', 'Снимок предложения неполный',
            )
            continue
        if any(
            str(getattr(product, field, '') or '').strip() != value
            for field, value in values.items()
        ):
            results_by_id[product_id] = _result_error(
                product_id,
                'proposal_conflict',
                'Карточка изменилась после подготовки предложения; публикация заблокирована',
            )
            continue
        if matching.wb_sync_status == 'uncertain':
            results_by_id[product_id] = _result_error(
                product_id,
                'proposal_uncertain',
                'Предыдущий запрос не получил однозначного ответа WB; '
                'автоматический повтор заблокирован до сверки карточки',
            )
            continue
        if matching.wb_synced and matching.wb_sync_status == 'success':
            results_by_id[product_id] = {
                'product_id': product_id,
                'status': 'already_published',
                'fields': requested_fields,
            }
            continue
        if not product.nm_id:
            results_by_id[product_id] = _result_error(
                product_id, 'nm_id_missing', 'У карточки нет nmID WB',
            )
            continue
        selected[product_id] = (product, matching, values)

    if not selected:
        ordered = [results_by_id[product_id] for product_id in product_ids]
        return {
            'published': 0,
            'already_published': sum(
                item['status'] == 'already_published' for item in ordered
            ),
            'failed': sum(item['status'] == 'error' for item in ordered),
            'results': ordered,
        }

    # Atomically claim every still-local proposal before network I/O. A plain
    # ORM assignment is not enough: two workers can both read ``pending`` and
    # then send the same diff. The conditional bulk UPDATE lets only one task
    # own a history row; the task-specific marker is verified after commit.
    claim_marker = f'agent_publish_claim:{task.id}'
    history_ids = [history.id for _, history, _ in selected.values()]
    CardEditHistory.query.filter(
        CardEditHistory.id.in_(history_ids),
        CardEditHistory.seller_id == seller_id,
        CardEditHistory.reverted.is_(False),
        CardEditHistory.wb_sync_status.in_(('pending', 'failed')),
    ).update({
        CardEditHistory.wb_synced: False,
        CardEditHistory.wb_sync_status: 'uncertain',
        CardEditHistory.wb_error_message: claim_marker,
    }, synchronize_session=False)
    db.session.commit()

    # A concurrent publisher or local rollback may have won the conditional
    # update. Reload both rows and verify ownership plus exact content before
    # touching WB.
    db.session.expire_all()
    ready: Dict[int, tuple[Product, CardEditHistory, dict]] = {}
    for product_id, (product, history, values) in selected.items():
        if (
            history.wb_sync_status != 'uncertain'
            or history.wb_error_message != claim_marker
        ):
            if history.wb_synced and history.wb_sync_status == 'success':
                results_by_id[product_id] = {
                    'product_id': product_id,
                    'status': 'already_published',
                    'fields': requested_fields,
                }
            else:
                results_by_id[product_id] = _result_error(
                    product_id,
                    'proposal_uncertain',
                    'Другой запрос уже публикует это предложение или его '
                    'результат требует сверки с WB',
                )
            continue
        if history.reverted or any(
            str(getattr(product, field, '') or '').strip() != value
            for field, value in values.items()
        ):
            if not history.reverted:
                history.wb_sync_status = 'failed'
                history.wb_error_message = (
                    'Карточка изменилась до отправки в WB.'
                )
            results_by_id[product_id] = _result_error(
                product_id,
                'proposal_conflict',
                'Карточка или предложение изменились до отправки; '
                'публикация заблокирована',
            )
            continue
        ready[product_id] = (product, history, values)
    if len(ready) != len(selected):
        db.session.commit()
    selected = ready
    if not selected:
        ordered = [results_by_id[product_id] for product_id in product_ids]
        return {
            'published': 0,
            'already_published': sum(
                item['status'] == 'already_published' for item in ordered
            ),
            'failed': sum(item['status'] == 'error' for item in ordered),
            'results': ordered,
        }

    if client_factory is None:
        from services.wb_api_client import WildberriesAPIClient
        client_factory = WildberriesAPIClient
    try:
        client = client_factory(seller.wb_api_key)
    except Exception as exc:
        for product_id, (_, history, _) in selected.items():
            history.wb_sync_status = 'failed'
            history.wb_error_message = str(exc)[:1000]
            results_by_id[product_id] = _result_error(
                product_id, 'wb_client_unavailable',
                'Не удалось подготовить WB-клиент',
            )
        db.session.commit()
        ordered = [results_by_id[product_id] for product_id in product_ids]
        return {
            'published': 0,
            'already_published': sum(
                item['status'] == 'already_published' for item in ordered
            ),
            'failed': sum(item['status'] == 'error' for item in ordered),
            'results': ordered,
        }
    try:
        nm_updates = {
            int(product.nm_id): values
            for product, _, values in selected.values()
        }
        try:
            wb_result = client.update_cards_merged(
                nm_updates, seller_id=seller_id,
            )
            sent, missing, invalid, failed = _validated_wb_batch_result(wb_result)
            accounted_nm_ids = sent | missing | set(invalid) | set(failed)
            if accounted_nm_ids != set(nm_updates):
                raise ValueError(
                    'WB batch response does not exactly account for requested nmIDs'
                )
        except Exception as exc:
            try:
                from services.wb_api_client import (
                    WBAuthException,
                    WBRateLimitException,
                    WBTransportUncertainException,
                )
                definitely_not_sent = (
                    isinstance(exc, (WBAuthException, WBRateLimitException))
                    or (
                        isinstance(exc, WBTransportUncertainException)
                        and not exc.request_may_have_been_applied
                    )
                )
            except ImportError:
                definitely_not_sent = False
            logger.warning(
                'Agent WB content batch failed for task=%s uncertain=%s: %s',
                task.id, not definitely_not_sent, str(exc)[:300],
            )
            for product_id, (_, history, _) in selected.items():
                history.wb_synced = False
                history.wb_sync_status = (
                    'failed' if definitely_not_sent else 'uncertain'
                )
                history.wb_error_message = str(exc)[:1000]
                results_by_id[product_id] = _result_error(
                    product_id,
                    (
                        'wb_request_not_sent'
                        if definitely_not_sent else 'wb_result_uncertain'
                    ),
                    (
                        'Запрос не был применён в WB; локальное предложение можно '
                        'повторить или откатить'
                        if definitely_not_sent else
                        'WB не подтвердил результат запроса; автоматический '
                        'повтор заблокирован'
                    ),
                )
            db.session.commit()
        else:
            for product_id, (product, history, _) in selected.items():
                nm_id = int(product.nm_id)
                if nm_id in sent:
                    history.wb_synced = True
                    history.wb_sync_status = 'success'
                    history.wb_error_message = None
                    results_by_id[product_id] = {
                        'product_id': product_id,
                        'status': 'published',
                        'nm_id': nm_id,
                        'fields': requested_fields,
                    }
                    continue
                error = (
                    invalid.get(nm_id)
                    or failed.get(nm_id)
                    or ('Карточка не найдена в WB' if nm_id in missing else None)
                    or 'WB не включил карточку в подтверждённый результат batch'
                )
                history.wb_synced = False
                history.wb_sync_status = 'failed'
                history.wb_error_message = str(error)[:1000]
                results_by_id[product_id] = _result_error(
                    product_id, 'wb_publish_failed', str(error),
                )
            db.session.commit()
    finally:
        close = getattr(client, 'close', None)
        if callable(close):
            close()

    ordered = [results_by_id[product_id] for product_id in product_ids]
    return {
        'published': sum(item['status'] == 'published' for item in ordered),
        'already_published': sum(
            item['status'] == 'already_published' for item in ordered
        ),
        'failed': sum(item['status'] == 'error' for item in ordered),
        'results': ordered,
    }
