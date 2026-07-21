# -*- coding: utf-8 -*-
"""WB-ревизия: реальная сверка состояния карточек с Wildberries.

Локальные `Product`-поля — это последняя известная проекция WB, но записи
могут «успешно» уходить и не применяться, а фоновая синхронизация может
запаздывать. Сервис читает точные live-карточки (`fetch_cards_by_nm_ids`) и
async error list (`/content/v2/cards/error/list`), затем фиксирует по каждой
карточке наблюдённый факт: существует ли, сколько фото, какие характеристики
реально лежат на WB и чем они отличаются от локальных.

Контракт:
- контент-поля карточки НЕ перезаписываются реальностью WB — расхождения
  только фиксируются в `Product.wb_audit_json` (человек решает, что править);
- исключение — `Product.photos_json`: это и есть проекция галереи WB, поэтому
  она зеркалит live-факт, включая честное пустое состояние (карточка без фото
  обязана вернуться в хаб дозагрузки);
- никаких provider write; только read-методы с существующим rate limiter.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from models import db, Product
from services.wb_api_client import (
    WildberriesAPIClient,
    normalize_cards_error_list,
)

logger = logging.getLogger(__name__)

AUDIT_VERSION = 1
MAX_AUDIT_BATCH = 200
# В отчёте храним только bounded списки имён, не полные значения.
MAX_SAMPLE_NAMES = 20
MAX_ERROR_TEXTS = 5

AUDIT_JOB_TYPE = 'wb_card_audit'


def _photo_urls(card: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for ph in card.get('photos') or []:
        if isinstance(ph, dict):
            url = ph.get('big') or ph.get('c516x688') or ph.get('square')
        else:
            url = ph if isinstance(ph, str) else None
        if url:
            urls.append(url)
    return urls


def _char_list(raw: Any) -> List[Any]:
    """Однократный parse WB-массива характеристик из строки/списка."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return raw if isinstance(raw, list) else []


def _char_map(raw: Any) -> Dict[int, Any]:
    """{charc_id: value} из WB-формата [{id, name, value}] без coercion."""
    result: Dict[int, Any] = {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return result
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict):
            continue
        charc_id = item.get('id')
        if isinstance(charc_id, bool) or not isinstance(charc_id, int):
            continue
        result[charc_id] = item.get('value')
    return result


def _char_names(raw: Any, ids: List[int]) -> List[str]:
    """Имена характеристик по id из того же WB-массива (bounded)."""
    names: List[str] = []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return names
    if not isinstance(raw, list):
        return names
    wanted = set(ids)
    for item in raw:
        if isinstance(item, dict) and item.get('id') in wanted:
            name = item.get('name')
            names.append(str(name) if name else f'id {item.get("id")}')
        if len(names) >= MAX_SAMPLE_NAMES:
            break
    return names


def _normalized_value(value: Any) -> Any:
    """Каноническая форма значения для сравнения local vs WB."""
    if isinstance(value, list):
        return [str(v).strip() for v in value]
    if value is None:
        return None
    return [str(value).strip()]


def audit_cards(seller, product_ids: List[int],
                persist: bool = True) -> Dict[str, Any]:
    """Сверить до MAX_AUDIT_BATCH карточек продавца с live-состоянием WB."""
    ids = []
    seen = set()
    for pid in product_ids or []:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            continue
        if pid not in seen:
            seen.add(pid)
            ids.append(pid)
    ids = ids[:MAX_AUDIT_BATCH]

    products = (
        Product.query
        .filter(Product.id.in_(ids), Product.seller_id == seller.id)
        .filter(Product.nm_id.isnot(None))
        .all()
    ) if ids else []

    summary = {'ok': 0, 'diverged': 0, 'not_found': 0, 'error': 0}
    items: List[Dict[str, Any]] = []
    if not products:
        return {'items': items, 'summary': summary}

    client = WildberriesAPIClient(seller.wb_api_key)
    nm_ids = [p.nm_id for p in products]
    cards = client.fetch_cards_by_nm_ids(nm_ids, seller_id=seller.id)

    try:
        raw_errors = client.get_cards_error_list(seller_id=seller.id)
        errors_by_nm, errors_by_vendor = normalize_cards_error_list(raw_errors)
    except Exception as exc:  # error list опционален для сверки
        logger.warning('WB audit: error list unavailable: %s', exc)
        errors_by_nm, errors_by_vendor = {}, {}

    audited_at = datetime.utcnow()
    for product in products:
        card = cards.get(product.nm_id)
        msgs = list(errors_by_nm.get(product.nm_id) or [])
        if not msgs and product.vendor_code:
            msgs = list(errors_by_vendor.get(product.vendor_code) or [])

        entry: Dict[str, Any] = {
            'product_id': product.id,
            'nm_id': product.nm_id,
            'vendor_code': product.vendor_code,
            'exists': card is not None,
            'wb_errors': [str(m)[:300] for m in msgs[:MAX_ERROR_TEXTS]],
        }

        if card is None:
            entry['status'] = 'not_found'
            summary['not_found'] += 1
        else:
            urls = _photo_urls(card)
            entry['photos_count'] = len(urls)
            entry['has_title'] = bool((card.get('title') or '').strip())
            entry['has_description'] = bool(
                (card.get('description') or '').strip())
            entry['has_dimensions'] = bool(card.get('dimensions'))

            local_raw = _char_list(product.characteristics_json)
            wb_raw = _char_list(card.get('characteristics'))
            local_chars = _char_map(local_raw)
            wb_chars = _char_map(wb_raw)
            missing_ids = [i for i in local_chars if i not in wb_chars]
            extra_ids = [i for i in wb_chars if i not in local_chars]
            diverged_ids = [
                i for i in local_chars
                if i in wb_chars
                and _normalized_value(local_chars[i])
                != _normalized_value(wb_chars[i])
            ]
            entry['characteristics'] = {
                'wb_count': len(wb_chars),
                'local_count': len(local_chars),
                'missing_on_wb': len(missing_ids),
                'missing_on_wb_names': _char_names(local_raw, missing_ids),
                'extra_on_wb': len(extra_ids),
                'extra_on_wb_names': _char_names(wb_raw, extra_ids),
                'diverged': len(diverged_ids),
                'diverged_names': _char_names(wb_raw, diverged_ids),
            }

            diverged = bool(
                msgs or missing_ids or diverged_ids or not urls
            )
            entry['status'] = 'diverged' if diverged else 'ok'
            summary['diverged' if diverged else 'ok'] += 1
            if msgs:
                summary['error'] += 1

            if persist:
                # Галерея — проекция WB: зеркалим live-факт, включая пустой.
                product.photos_json = json.dumps(urls, ensure_ascii=False)

        if persist:
            product.wb_audit_json = json.dumps({
                'version': AUDIT_VERSION,
                'audited_at': audited_at.isoformat(),
                **{k: v for k, v in entry.items() if k != 'product_id'},
            }, ensure_ascii=False)
            product.wb_audited_at = audited_at

        items.append(entry)

    if persist:
        db.session.commit()

    return {'items': items, 'summary': summary}


def audit_single_card_after_write(seller, product) -> Optional[Dict[str, Any]]:
    """Bounded happy-path read-after-write: одна карточка, ошибки не фатальны."""
    try:
        report = audit_cards(seller, [product.id], persist=True)
        return report['items'][0] if report['items'] else None
    except Exception as exc:
        logger.warning(
            'WB audit after write failed for product %s: %s', product.id, exc)
        db.session.rollback()
        return None


def run_audit_job(flask_app, job_uid: str, seller_id: int,
                  product_ids: List[int]) -> None:
    """Тело фонового потока bulk-ревизии из «Моих товаров»."""
    from models import BackgroundJob, Seller

    with flask_app.app_context():
        job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
        if not job or job.status == 'cancelled':
            return
        job.status = 'running'
        job.total = len(product_ids)
        db.session.commit()

        try:
            seller = db.session.get(Seller, seller_id)
            report = audit_cards(seller, product_ids)

            job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
            if job.status != 'cancelled':
                job.status = 'completed'
            # audit_cards сам дедуплицирует и обрезает вход — выравниваем
            # total по фактически обработанному набору.
            job.total = len(report['items'])
            job.processed = len(report['items'])
            job.succeeded = report['summary']['ok']
            job.failed_count = (
                report['summary']['diverged'] + report['summary']['not_found']
            )
            job.set_result(report)
            db.session.commit()
            logger.info('[WBAudit] job %s done: %s', job_uid, report['summary'])
        except Exception as exc:
            logger.error('[WBAudit] job %s failed: %s', job_uid, exc,
                         exc_info=True)
            db.session.rollback()
            job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
            if job:
                job.status = 'failed'
                job.error_message = str(exc)[:500]
                db.session.commit()
