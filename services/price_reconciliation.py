# -*- coding: utf-8 -*-
"""Сверка отправленных цен с фактическими ценами на Wildberries.

Загрузка цен в WB асинхронная: `POST /api/v2/upload/task` лишь ставит задачу в
очередь. Раньше платформа считала это применением, писала новую цену в
`Product.price` и показывала её продавцу как факт — даже если площадка потом
отклоняла позицию. Здесь правда восстанавливается единственным надёжным
способом: читаем фактические цены товаров с WB и сравниваем с тем, что
отправляли.

Сервис только читает WB и обновляет локальные статусы. Никаких повторных
отправок цен: результат неудачной позиции остаётся продавцу для явного
решения (кнопка «Повторить неудачные»).
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from models import (
    PriceChangeBatch,
    PriceChangeItem,
    PriceHistory,
    Product,
    db,
)

logger = logging.getLogger(__name__)

# Статус позиции между отправкой и подтверждением площадки.
STATUS_SUBMITTED = 'submitted'

# Сколько ждём подтверждения, прежде чем признать позицию непринятой.
# WB обычно обрабатывает очередь за минуты; сутки — заведомо достаточный запас,
# чтобы не назвать неудачей то, что просто ещё едет.
CONFIRMATION_DEADLINE = timedelta(hours=24)

# Максимум nmID, которые читаем за один проход сверки одного запуска.
MAX_NM_IDS_PER_RUN = 1000

# Допустимое расхождение с ценой площадки: WB округляет до целых рублей.
PRICE_TOLERANCE = Decimal('1')


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _actual_price_by_nm_id(goods: List[Dict[str, Any]]) -> Dict[int, Decimal]:
    """Фактическая цена товара на площадке по каждому nmID.

    В ответе WB цена лежит в размерах (`sizes[].price`); у товаров без
    размерной сетки размер один. Берём минимальную наблюдённую цену: именно
    её видит покупатель, если размеры стоят по-разному.
    """
    result: Dict[int, Decimal] = {}
    for row in goods or []:
        if not isinstance(row, dict):
            continue
        nm_id = row.get('nmID')
        if not isinstance(nm_id, int):
            continue
        prices = []
        for size in row.get('sizes') or []:
            if not isinstance(size, dict):
                continue
            price = _decimal(size.get('price'))
            if price is not None and price > 0:
                prices.append(price)
        # Некоторые ответы содержат цену на верхнем уровне
        top_level = _decimal(row.get('price'))
        if top_level is not None and top_level > 0:
            prices.append(top_level)
        if prices:
            result[nm_id] = min(prices)
    return result


class PriceReconciliationResult:
    """Что дала одна сверка — для честного показа продавцу."""

    def __init__(self):
        self.confirmed = 0
        self.rejected = 0
        self.still_waiting = 0
        self.unknown = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            'confirmed': self.confirmed,
            'rejected': self.rejected,
            'still_waiting': self.still_waiting,
            'unknown': self.unknown,
        }


class PriceReconciliationService:
    """Сверяет отправленные цены с тем, что реально стоит на площадке."""

    @classmethod
    def pending_batches(cls, *, seller_id: int, limit: int = 20) -> List[PriceChangeBatch]:
        """Запуски, которые ждут подтверждения площадки."""
        return PriceChangeBatch.query.filter(
            PriceChangeBatch.seller_id == seller_id,
            PriceChangeBatch.status.in_(('submitted', 'applying')),
        ).order_by(PriceChangeBatch.applied_at.asc()).limit(limit).all()

    @classmethod
    def reconcile_batch(
        cls,
        *,
        batch: PriceChangeBatch,
        api_client,
        now: Optional[datetime] = None,
    ) -> PriceReconciliationResult:
        """Подтвердить или отклонить позиции запуска по фактическим ценам WB."""
        now = now or datetime.utcnow()
        result = PriceReconciliationResult()

        items = batch.items.filter_by(status=STATUS_SUBMITTED).limit(
            MAX_NM_IDS_PER_RUN
        ).all()
        if not items:
            cls._settle_batch_status(batch)
            return result

        # Читаем фактические цены только по нужным товарам
        actual: Dict[int, Decimal] = {}
        for item in items:
            if not item.nm_id:
                continue
            try:
                response = api_client.get_goods_prices(
                    limit=10,
                    filter_nm_id=item.nm_id,
                    seller_id=batch.seller_id,
                )
            except Exception as exc:  # noqa: BLE001 — сверка не должна падать целиком
                logger.warning(
                    'Не удалось прочитать цену nmID=%s: %s', item.nm_id, exc
                )
                continue
            goods = (response or {}).get('data', {}).get('listGoods', [])
            actual.update(_actual_price_by_nm_id(goods))

        deadline_passed = (
            batch.applied_at is not None
            and now - batch.applied_at > CONFIRMATION_DEADLINE
        )

        for item in items:
            expected = _decimal(item.new_price)
            observed = actual.get(item.nm_id)
            if expected is None:
                result.unknown += 1
                continue
            if observed is None:
                # Площадка не отдала цену: не выдаём это ни за успех, ни за отказ
                if deadline_passed:
                    item.status = 'failed'
                    item.wb_status = 'not_confirmed'
                    item.error_message = (
                        'Wildberries не подтвердил новую цену за сутки'
                    )
                    result.rejected += 1
                else:
                    result.still_waiting += 1
                continue

            if abs(observed - expected) <= PRICE_TOLERANCE:
                item.status = 'applied'
                item.wb_status = 'confirmed'
                item.wb_applied_at = now
                result.confirmed += 1
                # Локальная цена = то, что реально стоит на площадке
                product = Product.query.filter_by(
                    id=item.product_id,
                    seller_id=batch.seller_id,
                ).first()
                if product is not None:
                    product.price = observed
                    db.session.add(PriceHistory(
                        product_id=item.product_id,
                        seller_id=batch.seller_id,
                        old_price=item.old_price,
                        new_price=observed,
                        price_change_percent=item.price_change_percent,
                    ))
            elif deadline_passed:
                item.status = 'failed'
                item.wb_status = 'other_price'
                item.error_message = (
                    f'На Wildberries стоит другая цена: {observed}'
                )
                result.rejected += 1
            else:
                result.still_waiting += 1

        cls._settle_batch_status(batch)
        db.session.commit()
        return result

    @classmethod
    def _settle_batch_status(cls, batch: PriceChangeBatch) -> None:
        """Статус запуска считается по фактическим статусам его позиций."""
        counts = dict(
            db.session.query(
                PriceChangeItem.status,
                db.func.count(PriceChangeItem.id),
            ).filter_by(batch_id=batch.id).group_by(PriceChangeItem.status).all()
        )
        applied = counts.get('applied', 0)
        failed = counts.get('failed', 0)
        waiting = counts.get(STATUS_SUBMITTED, 0)

        batch.applied_count = applied
        batch.failed_count = failed
        if waiting:
            batch.status = 'submitted'
        elif applied and failed:
            batch.status = 'partially_applied'
        elif applied:
            batch.status = 'applied'
        elif failed:
            batch.status = 'failed'
