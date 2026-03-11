# -*- coding: utf-8 -*-
"""
Сервис инкрементальной синхронизации данных WB в локальную БД.

Загружает сырые данные из WB Statistics API и Feedbacks API,
сохраняет в таблицы wb_sales, wb_orders, wb_feedbacks, wb_realization_rows.

Дедупликация:
- Sales: по srid (уникальный ID операции)
- Orders: по srid
- Feedbacks: по wb_id (id отзыва)
- Realization: по rrd_id (уникальный ID строки отчёта)

Инкрементальность:
- Sales/Orders: dateFrom = max(last_change_date) из нашей БД
- Feedbacks: загружаем все, upsert по wb_id
- Realization: dateFrom = max(rr_dt) - 7 дней (перекрытие для обновлений)
"""
import logging
import time
import requests
from datetime import datetime, timedelta, date
from typing import Optional

from models import db, Seller, WBSale, WBOrder, WBFeedback, WBRealizationRow

logger = logging.getLogger('wb_data_sync')

STATISTICS_API_URL = "https://statistics-api.wildberries.ru"
FEEDBACKS_API_URL = "https://feedbacks-api.wildberries.ru"


def _make_session(api_key: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        'Authorization': api_key,
        'Content-Type': 'application/json'
    })
    return session


# =============================================================================
# Sales sync (/api/v1/supplier/sales)
# =============================================================================

def sync_sales(seller: Seller) -> int:
    """Инкрементальная загрузка продаж/возвратов. Returns count of new/updated rows."""
    # Определяем dateFrom: max(last_change_date) или 90 дней назад для первой загрузки
    last_date = db.session.query(db.func.max(WBSale.last_change_date)).filter(
        WBSale.seller_id == seller.id
    ).scalar()

    if last_date:
        date_from = last_date.strftime('%Y-%m-%dT%H:%M:%S')
    else:
        date_from = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')

    logger.info(f"Sync sales for seller={seller.id}, dateFrom={date_from}")

    session = _make_session(seller.wb_api_key)
    resp = session.get(
        f"{STATISTICS_API_URL}/api/v1/supplier/sales",
        params={'dateFrom': date_from},
        timeout=60
    )
    resp.raise_for_status()
    rows = resp.json()

    if not isinstance(rows, list):
        logger.warning(f"Sales API returned non-list: {type(rows)}")
        return 0

    count = 0
    for r in rows:
        srid = r.get('srid')
        if not srid:
            continue

        existing = WBSale.query.filter_by(seller_id=seller.id, srid=srid).first()
        sale_id = str(r.get('saleID', ''))
        is_return = sale_id.startswith('R') or float(r.get('finishedPrice', 0) or 0) < 0

        if existing:
            # Обновляем, если last_change_date новее
            new_lcd = r.get('lastChangeDate', '')
            if new_lcd and existing.last_change_date:
                try:
                    new_dt = datetime.fromisoformat(new_lcd.replace('Z', '+00:00').replace('+00:00', ''))
                    if new_dt > existing.last_change_date:
                        existing.last_change_date = new_dt
                        existing.finished_price = float(r.get('finishedPrice', 0) or 0)
                        existing.for_pay = float(r.get('forPay', 0) or 0)
                        existing.is_return = is_return
                        count += 1
                except (ValueError, TypeError):
                    pass
        else:
            sale = WBSale(
                seller_id=seller.id,
                srid=srid,
                sale_id=sale_id,
                nm_id=r.get('nmId'),
                date=_parse_dt(r.get('date')),
                last_change_date=_parse_dt(r.get('lastChangeDate')),
                supplier_article=r.get('supplierArticle', ''),
                subject=r.get('subject', ''),
                brand=r.get('brand', ''),
                warehouse_name=r.get('warehouseName', ''),
                region_name=r.get('regionName', ''),
                country_name=r.get('countryName', ''),
                finished_price=float(r.get('finishedPrice', 0) or 0),
                price_with_disc=float(r.get('priceWithDisc', 0) or 0),
                for_pay=float(r.get('forPay', 0) or 0),
                is_return=is_return,
            )
            db.session.add(sale)
            count += 1

    db.session.commit()
    logger.info(f"Sales sync done for seller={seller.id}: {count} new/updated from {len(rows)} API rows")
    return count


# =============================================================================
# Orders sync (/api/v1/supplier/orders)
# =============================================================================

def sync_orders(seller: Seller) -> int:
    """Инкрементальная загрузка заказов."""
    last_date = db.session.query(db.func.max(WBOrder.last_change_date)).filter(
        WBOrder.seller_id == seller.id
    ).scalar()

    if last_date:
        date_from = last_date.strftime('%Y-%m-%dT%H:%M:%S')
    else:
        date_from = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')

    logger.info(f"Sync orders for seller={seller.id}, dateFrom={date_from}")

    session = _make_session(seller.wb_api_key)
    resp = session.get(
        f"{STATISTICS_API_URL}/api/v1/supplier/orders",
        params={'dateFrom': date_from},
        timeout=60
    )
    resp.raise_for_status()
    rows = resp.json()

    if not isinstance(rows, list):
        logger.warning(f"Orders API returned non-list: {type(rows)}")
        return 0

    count = 0
    for r in rows:
        srid = r.get('srid')
        if not srid:
            continue

        existing = WBOrder.query.filter_by(seller_id=seller.id, srid=srid).first()

        if existing:
            # Обновляем статус отмены
            new_lcd = r.get('lastChangeDate', '')
            is_cancel = bool(r.get('isCancel', False))
            if is_cancel and not existing.is_cancel:
                existing.is_cancel = True
                existing.cancel_dt = _parse_dt(r.get('cancelDate') or r.get('cancel_dt'))
                existing.last_change_date = _parse_dt(new_lcd) or existing.last_change_date
                count += 1
            elif new_lcd:
                try:
                    new_dt = datetime.fromisoformat(new_lcd.replace('Z', '+00:00').replace('+00:00', ''))
                    if existing.last_change_date and new_dt > existing.last_change_date:
                        existing.last_change_date = new_dt
                        existing.finished_price = float(r.get('finishedPrice', 0) or 0)
                except (ValueError, TypeError):
                    pass
        else:
            order = WBOrder(
                seller_id=seller.id,
                srid=srid,
                nm_id=r.get('nmId'),
                date=_parse_dt(r.get('date')),
                last_change_date=_parse_dt(r.get('lastChangeDate')),
                supplier_article=r.get('supplierArticle', ''),
                subject=r.get('subject', ''),
                brand=r.get('brand', ''),
                warehouse_name=r.get('warehouseName', ''),
                region_name=r.get('regionName', ''),
                oblast_okrug_name=r.get('oblastOkrugName', ''),
                country_name=r.get('countryName', ''),
                total_price=float(r.get('totalPrice', 0) or 0),
                finished_price=float(r.get('finishedPrice', 0) or 0),
                is_cancel=bool(r.get('isCancel', False)),
                cancel_dt=_parse_dt(r.get('cancelDate') or r.get('cancel_dt')),
                order_type=r.get('orderType', ''),
                sticker=r.get('sticker', ''),
            )
            db.session.add(order)
            count += 1

    db.session.commit()
    logger.info(f"Orders sync done for seller={seller.id}: {count} new/updated from {len(rows)} API rows")
    return count


# =============================================================================
# Feedbacks sync (/api/v1/feedbacks)
# =============================================================================

def sync_feedbacks(seller: Seller) -> int:
    """Загрузка отзывов с upsert по wb_id."""
    logger.info(f"Sync feedbacks for seller={seller.id}")

    session = _make_session(seller.wb_api_key)
    all_feedbacks = []

    # Загружаем оба типа: отвеченные и неотвеченные
    for is_answered in [True, False]:
        skip = 0
        while True:
            resp = session.get(
                f"{FEEDBACKS_API_URL}/api/v1/feedbacks",
                params={
                    'isAnswered': str(is_answered).lower(),
                    'take': 5000,
                    'skip': skip,
                },
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            feedbacks = data.get('data', {}).get('feedbacks', [])
            if not feedbacks:
                break
            all_feedbacks.extend(feedbacks)
            if len(feedbacks) < 5000:
                break
            skip += 5000

    count = 0
    for f in all_feedbacks:
        wb_id = str(f.get('id', ''))
        if not wb_id:
            continue

        existing = WBFeedback.query.filter_by(seller_id=seller.id, wb_id=wb_id).first()

        if existing:
            # Обновляем статус ответа
            new_answered = bool(f.get('answer'))
            if new_answered and not existing.is_answered:
                existing.is_answered = True
                existing.updated_date = _parse_dt(f.get('updatedDate'))
                count += 1
        else:
            fb = WBFeedback(
                seller_id=seller.id,
                wb_id=wb_id,
                nm_id=f.get('nmId') or f.get('productDetails', {}).get('nmId'),
                created_date=_parse_dt(f.get('createdDate')),
                updated_date=_parse_dt(f.get('updatedDate')),
                valuation=f.get('productValuation', 0),
                text=f.get('text', ''),
                user_name=f.get('userName', ''),
                product_name=f.get('productDetails', {}).get('productName', ''),
                subject_name=f.get('productDetails', {}).get('subjectName', ''),
                brand_name=f.get('productDetails', {}).get('brandName', ''),
                is_answered=bool(f.get('answer')),
            )
            db.session.add(fb)
            count += 1

    db.session.commit()
    logger.info(f"Feedbacks sync done for seller={seller.id}: {count} new/updated from {len(all_feedbacks)} API rows")
    return count


# =============================================================================
# Realization sync (/api/v5/supplier/reportDetailByPeriod)
# =============================================================================

def sync_realization(seller: Seller) -> int:
    """Инкрементальная загрузка строк реализации. Перезагружаем последние 7 дней
    от max(rr_dt) для подхватывания обновлений в незакрытых отчётах."""
    last_rr_dt = db.session.query(db.func.max(WBRealizationRow.rr_dt)).filter(
        WBRealizationRow.seller_id == seller.id
    ).scalar()

    if last_rr_dt:
        # Перекрытие 7 дней для обновлений
        date_from = (last_rr_dt - timedelta(days=7)).strftime('%Y-%m-%d')
    else:
        date_from = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')

    date_to = datetime.now().strftime('%Y-%m-%d')

    logger.info(f"Sync realization for seller={seller.id}, dateFrom={date_from}")

    session = _make_session(seller.wb_api_key)
    all_rows = []
    rrdid = 0
    limit = 100000

    while True:
        params = {
            'dateFrom': date_from,
            'dateTo': date_to,
            'limit': limit,
            'rrdid': rrdid,
        }
        resp = session.get(
            f"{STATISTICS_API_URL}/api/v5/supplier/reportDetailByPeriod",
            params=params,
            timeout=120
        )
        if resp.status_code == 204:
            break

        resp.raise_for_status()
        page = resp.json()

        if not isinstance(page, list) or not page:
            break

        all_rows.extend(page)

        if len(page) < limit:
            break

        last_rrd_id = page[-1].get('rrd_id', 0)
        if last_rrd_id and last_rrd_id != rrdid:
            rrdid = last_rrd_id
            logger.info(f"Realization pagination: {len(all_rows)} rows, next rrdid={rrdid}, sleeping 61s...")
            time.sleep(61)
        else:
            break

    # Upsert по rrd_id
    count = 0
    existing_rrd_ids = set()
    if all_rows:
        rrd_ids = [r.get('rrd_id') for r in all_rows if r.get('rrd_id')]
        if rrd_ids:
            existing = db.session.query(WBRealizationRow.rrd_id).filter(
                WBRealizationRow.seller_id == seller.id,
                WBRealizationRow.rrd_id.in_(rrd_ids)
            ).all()
            existing_rrd_ids = {row[0] for row in existing}

    for r in all_rows:
        rrd_id_val = r.get('rrd_id')
        if not rrd_id_val:
            continue

        if rrd_id_val in existing_rrd_ids:
            # Обновляем финансовые поля (отчёт мог быть скорректирован)
            db.session.query(WBRealizationRow).filter(
                WBRealizationRow.seller_id == seller.id,
                WBRealizationRow.rrd_id == rrd_id_val
            ).update({
                'ppvz_for_pay': float(r.get('ppvz_for_pay', 0) or 0),
                'ppvz_sales_commission': float(r.get('ppvz_sales_commission', 0) or 0),
                'delivery_rub': float(r.get('delivery_rub', 0) or 0),
                'storage_fee': float(r.get('storage_fee', 0) or 0),
                'penalty': float(r.get('penalty', 0) or 0),
                'deduction': float(r.get('deduction', 0) or 0),
            })
            count += 1
        else:
            row = WBRealizationRow(
                seller_id=seller.id,
                rrd_id=rrd_id_val,
                realizationreport_id=r.get('realizationreport_id'),
                rr_dt=_parse_dt(r.get('rr_dt')),
                date_from=_parse_date(r.get('date_from')),
                date_to=_parse_date(r.get('date_to')),
                nm_id=r.get('nm_id'),
                sa_name=r.get('sa_name', ''),
                subject_name=r.get('subject_name', ''),
                brand_name=r.get('brand_name', ''),
                supplier_oper_name=r.get('supplier_oper_name', ''),
                doc_type_name=r.get('doc_type_name', ''),
                retail_price_withdisc_rub=float(r.get('retail_price_withdisc_rub', 0) or 0),
                retail_amount=float(r.get('retail_amount', 0) or 0),
                ppvz_for_pay=float(r.get('ppvz_for_pay', 0) or 0),
                ppvz_sales_commission=float(r.get('ppvz_sales_commission', 0) or 0),
                commission_percent=float(r.get('commission_percent', 0) or 0),
                delivery_rub=float(r.get('delivery_rub', 0) or 0),
                rebill_logistic_cost=float(r.get('rebill_logistic_cost', 0) or 0),
                storage_fee=float(r.get('storage_fee', 0) or 0),
                penalty=float(r.get('penalty', 0) or 0),
                deduction=float(r.get('deduction', 0) or 0),
                acceptance=float(r.get('acceptance', 0) or 0),
                additional_payment=float(r.get('additional_payment', 0) or 0),
                return_amount=int(r.get('return_amount', 0) or 0),
                delivery_amount=int(r.get('delivery_amount', 0) or 0),
            )
            db.session.add(row)
            count += 1

    db.session.commit()
    logger.info(f"Realization sync done for seller={seller.id}: {count} rows processed from {len(all_rows)} API rows")
    return count


# =============================================================================
# Full sync (all data types)
# =============================================================================

def sync_all(seller: Seller) -> dict:
    """Запустить полную инкрементальную синхронизацию всех данных."""
    results = {}
    errors = {}

    for name, fn in [
        ('sales', sync_sales),
        ('orders', sync_orders),
        ('feedbacks', sync_feedbacks),
        ('realization', sync_realization),
    ]:
        try:
            results[name] = fn(seller)
        except Exception as e:
            logger.error(f"Sync {name} failed for seller={seller.id}: {e}")
            errors[name] = str(e)
            db.session.rollback()

    return {'results': results, 'errors': errors}


def sync_all_sellers():
    """Синхронизировать данные для всех активных продавцов.
    Вызывается из scheduler."""
    from flask import current_app
    try:
        sellers = Seller.query.all()
        for seller in sellers:
            if not seller.wb_api_key:
                continue
            try:
                result = sync_all(seller)
                logger.info(f"Full sync for seller={seller.id}: {result}")
            except Exception as e:
                logger.error(f"Full sync failed for seller={seller.id}: {e}")
                db.session.rollback()
    except Exception as e:
        logger.error(f"sync_all_sellers error: {e}")


# =============================================================================
# Helpers
# =============================================================================

def _parse_dt(val) -> Optional[datetime]:
    """Парсинг даты из WB API (ISO формат с возможным Z)."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        s = str(val).replace('Z', '').replace('+00:00', '')
        # Handle 'T' separator
        if 'T' in s:
            return datetime.fromisoformat(s)
        # Date only
        return datetime.strptime(s[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return None


def _parse_date(val) -> Optional[date]:
    """Парсинг даты (без времени)."""
    if not val:
        return None
    try:
        s = str(val)[:10]
        return datetime.strptime(s, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None
