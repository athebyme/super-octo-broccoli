# -*- coding: utf-8 -*-
"""Обновление карточек от поставщика: выборка дельт по фото и массовая дозагрузка.

Карточка продавца (Product) связана с каталогом поставщика цепочкой
Product ← ImportedProduct → SupplierProduct → Supplier. Актуальный набор фото
берётся напрямую из SupplierProduct.photo_urls_json (staging-копия
ImportedProduct.photo_urls может устареть).

«Дозагрузить фото» = полная пересборка набора карточки из галереи поставщика
(+ стандартные пины продавца). Worker синхронно кэширует файлы и использует
multipart media/file — тот же надёжный transport, что рабочий одиночный поток;
PUBLIC_BASE_URL для массовой операции не требуется. Реальные URL текущих фото
WB в БД недоступны, а после загрузки WB перехостит файлы на CDN, поэтому дедуп
против «уже загруженных» невозможен.
"""
import json
import logging
import time
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import case, func, or_

from models import (
    db, Product, ImportedProduct, Supplier, SupplierProduct, Seller,
    BackgroundJob, BulkEditHistory, Notification,
    get_standard_media, get_min_photos,
)
from services.standard_photos import compose_card_photo_urls, WB_MAX_PHOTOS
from services.wb_api_client import WildberriesAPIClient, normalize_cards_error_list

logger = logging.getLogger(__name__)

JOB_TYPE = 'supplier_photos_update'
VERIFY_JOB_TYPE = 'supplier_updates_verify'

# Пауза между карточками отдаёт Content API другим операциям. Каждый multipart
# запрос дополнительно проходит общий category/endpoint limiter WB-клиента.
JOB_ITEM_PAUSE_SECONDS = 1.0

# Пауза перед авто-проверкой после дозагрузки: WB может обновлять read model
# не мгновенно, поэтому немедленная сверка часто даст «ещё обрабатывается».
VERIFY_DELAY_SECONDS = 20


def _json_len(column):
    """SQL-выражение: длина JSON-массива в колонке (0 для NULL/мусора)."""
    return case(
        (func.json_valid(column), func.json_array_length(column)),
        else_=0,
    )


def _base_query(seller_id: int, supplier_id: Optional[int] = None,
                only_new: bool = True, search: str = ''):
    wb_count = _json_len(Product.photos_json).label('wb_count')
    sp_count = _json_len(SupplierProduct.photo_urls_json).label('sp_count')

    q = (
        db.session.query(Product, ImportedProduct, Supplier, wb_count, sp_count)
        .join(ImportedProduct, ImportedProduct.product_id == Product.id)
        .join(SupplierProduct, SupplierProduct.id == ImportedProduct.supplier_product_id)
        .join(Supplier, Supplier.id == ImportedProduct.supplier_id)
        .filter(
            Product.seller_id == seller_id,
            ImportedProduct.seller_id == seller_id,
            Product.is_active.is_(True),
            Product.nm_id.isnot(None),
        )
        .group_by(Product.id)
    )
    if supplier_id:
        q = q.filter(ImportedProduct.supplier_id == supplier_id)
    if only_new:
        q = q.filter(sp_count > wb_count)
    if search:
        like = f'%{search}%'
        filters = [Product.title.ilike(like), Product.vendor_code.ilike(like)]
        if search.isdigit():
            filters.append(Product.nm_id == int(search))
        q = q.filter(or_(*filters))
    return q, wb_count, sp_count


def query_update_rows(seller_id: int, supplier_id: Optional[int] = None,
                      only_new: bool = True, search: str = '',
                      page: int = 1, per_page: int = 50) -> Tuple[List[dict], int]:
    """Строки хаба: карточки продавца со счётчиками фото WB/поставщик."""
    q, wb_count, sp_count = _base_query(seller_id, supplier_id, only_new, search)
    total = q.count()
    q = q.order_by((sp_count - wb_count).desc(), Product.id.asc())
    items = q.offset((page - 1) * per_page).limit(per_page).all()

    rows = []
    for product, imp, supplier, wb_n, sp_n in items:
        rows.append({
            'product': product,
            'imported_product_id': imp.id,
            'supplier_product_id': imp.supplier_product_id,
            'supplier_id': supplier.id,
            'supplier_name': supplier.name,
            'wb_count': int(wb_n or 0),
            'supplier_count': int(sp_n or 0),
            'delta': int(sp_n or 0) - int(wb_n or 0),
        })
    return rows, total


def expand_filter_to_ids(seller_id: int, supplier_id: Optional[int] = None,
                         only_new: bool = True, search: str = '') -> List[int]:
    """Развернуть фильтр в список product_id (для «выбрать всё по фильтру»)."""
    q, _, sp_count = _base_query(seller_id, supplier_id, only_new, search)
    wb = _json_len(Product.photos_json)
    q = q.order_by((sp_count - wb).desc(), Product.id.asc())
    return [product.id for product, *_ in q.all()]


def get_supplier_chips(seller_id: int) -> List[dict]:
    """Сводка по поставщикам продавца: всего связанных карточек / с новыми фото."""
    wb_count = _json_len(Product.photos_json)
    sp_count = _json_len(SupplierProduct.photo_urls_json)
    rows = (
        db.session.query(
            Supplier.id, Supplier.name, Supplier.code,
            func.count(func.distinct(Product.id)),
            func.sum(case((sp_count > wb_count, 1), else_=0)),
        )
        .select_from(Product)
        .join(ImportedProduct, ImportedProduct.product_id == Product.id)
        .join(SupplierProduct, SupplierProduct.id == ImportedProduct.supplier_product_id)
        .join(Supplier, Supplier.id == ImportedProduct.supplier_id)
        .filter(
            Product.seller_id == seller_id,
            ImportedProduct.seller_id == seller_id,
            Product.is_active.is_(True),
            Product.nm_id.isnot(None),
        )
        .group_by(Supplier.id, Supplier.name, Supplier.code)
        .all()
    )
    return [
        {'supplier_id': sid, 'name': name, 'code': code,
         'total': int(total or 0), 'with_new': int(with_new or 0)}
        for sid, name, code, total, with_new in rows
    ]


def build_target_photo_set(supplier_product: SupplierProduct, product: Product,
                           seller_id: int) -> List[str]:
    """Целевой набор URL для media/save: пины продавца + вся галерея поставщика."""
    photos = supplier_product.get_photos() if supplier_product else []
    if not photos:
        return []

    from routes.photos import generate_public_photo_url
    supplier_urls = [
        generate_public_photo_url(supplier_product.id, idx)
        for idx in range(len(photos))
    ]

    media = get_standard_media(seller_id, getattr(product, 'subject_id', None))
    composed = compose_card_photo_urls(
        supplier_urls, media, seller_id, get_min_photos(seller_id))
    # Композер возвращает [] когда пинов нет — тогда просто галерея поставщика
    return composed if composed else supplier_urls[:WB_MAX_PHOTOS]


def verify_cards_on_wb(seller, product_ids: List[int]) -> dict:
    """Сверка «долетели ли обновления до WB» по списку карточек продавца.

    Два источника истины WB:
      1. /content/v2/cards/error/list — асинхронные ошибки обработки
         (WB отвечает 200/202 на save, а реальные отказы видны только здесь);
      2. фактическая карточка (fetch_cards_by_nm_ids) — сравниваем число фото
         на WB с целевым набором поставщика.

    Статусы per-card: ok | pending (WB ещё обрабатывает) | error (ошибки WB)
    | not_found (карточка не найдена на WB).
    Попутно обновляет Product.photos_json фактическими URL с WB, чтобы
    счётчики дельты хаба отражали реальность.
    """
    products = (
        Product.query
        .filter(
            Product.id.in_(product_ids),
            Product.seller_id == seller.id,
            Product.nm_id.isnot(None),
        )
        .all()
    )
    summary = {'ok': 0, 'pending': 0, 'error': 0, 'not_found': 0}
    if not products:
        return {'items': [], 'summary': summary}

    client = WildberriesAPIClient(seller.wb_api_key)
    cards = client.fetch_cards_by_nm_ids(
        [int(p.nm_id) for p in products], seller_id=seller.id
    )
    wb_errors = client.get_cards_error_list(seller_id=seller.id)
    errors_by_nm, errors_by_vendor = normalize_cards_error_list(wb_errors)

    # Целевое число фото: пины продавца + галерея поставщика
    imps = {
        imp.product_id: imp
        for imp in ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.product_id.in_([p.id for p in products]),
            ImportedProduct.supplier_product_id.isnot(None),
        ).all()
    }
    sp_ids = [imp.supplier_product_id for imp in imps.values()]
    sps_by_id = {
        sp.id: sp
        for sp in SupplierProduct.query.filter(
            SupplierProduct.id.in_(sp_ids)
        ).all()
    } if sp_ids else {}

    items = []
    for p in products:
        nm_id = int(p.nm_id)
        entry = {
            'product_id': p.id,
            'nm_id': nm_id,
            'title': (p.title or '')[:80],
            'vendor_code': p.vendor_code,
            'wb_photos': None,
            'expected_photos': None,
            'errors': [],
        }

        msgs = errors_by_nm.get(nm_id) or errors_by_vendor.get(str(p.vendor_code or ''))
        card = cards.get(nm_id)

        expected = None
        imp = imps.get(p.id)
        if imp:
            sp = sps_by_id.get(imp.supplier_product_id)
            if sp:
                expected = len(build_target_photo_set(sp, p, seller.id))
        entry['expected_photos'] = expected

        if msgs:
            entry['status'] = 'error'
            entry['errors'] = [str(m) for m in msgs[:5]]
            summary['error'] += 1
        elif not card:
            entry['status'] = 'not_found'
            summary['not_found'] += 1
        else:
            photos = card.get('photos') or []
            entry['wb_photos'] = len(photos)
            # Синхронизируем локальный набор фото с фактом WB — дельта хаба
            # пересчитается честно. Пустое состояние тоже факт: карточка без
            # фото обязана вернуться в хаб дозагрузки, а не скрываться за
            # старым локальным списком. Если read model WB запаздывает,
            # следующая сверка поправит счётчик обратно.
            urls = []
            for ph in photos:
                if isinstance(ph, dict):
                    url = ph.get('big') or ph.get('c516x688') or ph.get('square')
                else:
                    url = ph if isinstance(ph, str) else None
                if url:
                    urls.append(url)
            p.photos_json = json.dumps(urls, ensure_ascii=False)
            if expected is not None and len(photos) < expected:
                entry['status'] = 'pending'
                summary['pending'] += 1
            else:
                entry['status'] = 'ok'
                summary['ok'] += 1
        items.append(entry)

    db.session.commit()

    # Дельты изменились — чипы пересчитаются на следующем показе
    try:
        from services.ttl_cache import cache
        cache.invalidate(f'supdates-chips:{seller.id}')
    except Exception:
        pass

    return {'items': items, 'summary': summary}


def run_verify_job(flask_app, job_uid: str, seller_id: int,
                   product_ids: List[int]) -> None:
    """Тело фонового потока: сверка карточек с WB (error list + фото)."""
    with flask_app.app_context():
        job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
        if not job or job.status == 'cancelled':
            return
        job.status = 'running'
        job.total = len(product_ids)
        db.session.commit()

        try:
            seller = db.session.get(Seller, seller_id)
            report = verify_cards_on_wb(seller, product_ids)

            job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
            if job.status != 'cancelled':
                job.status = 'completed'
            job.processed = len(report['items'])
            job.succeeded = report['summary']['ok']
            job.failed_count = report['summary']['error']
            job.set_result(report)
            db.session.commit()
            logger.info(f"[SupplierVerify] job {job_uid} done: {report['summary']}")
        except Exception as e:
            logger.error(f"[SupplierVerify] job {job_uid} failed: {e}", exc_info=True)
            db.session.rollback()
            job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
            if job:
                job.status = 'failed'
                job.error_message = str(e)[:500]
                db.session.commit()


def run_photos_job(flask_app, job_uid: str, seller_id: int,
                   product_ids: List[int]) -> None:
    """Тело фонового потока: последовательная дозагрузка фото по карточкам."""
    with flask_app.app_context():
        job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
        if not job:
            return
        if job.status == 'cancelled':
            return
        job.status = 'running'
        job.total = len(product_ids)
        db.session.commit()

        seller = db.session.get(Seller, seller_id)
        wb_client = WildberriesAPIClient(seller.wb_api_key)

        succeeded = failed = skipped = 0
        item_errors = []
        succeeded_ids = []
        from services.supplier_enrichment import EnrichmentService
        enrichment_service = EnrichmentService()

        for idx, pid in enumerate(product_ids):
            # Проверка отмены между карточками
            db.session.expire(job)
            if job.status == 'cancelled':
                logger.info(f"[SupplierPhotos] job {job_uid} cancelled at {idx}/{len(product_ids)}")
                break

            outcome_error = None
            try:
                product = db.session.get(Product, pid)
                imp = None
                if product and product.seller_id == seller_id:
                    imp = (ImportedProduct.query
                           .filter_by(seller_id=seller_id, product_id=pid)
                           .filter(ImportedProduct.supplier_product_id.isnot(None))
                           .first())
                if not product or product.seller_id != seller_id or not imp:
                    skipped += 1
                    job.processed = idx + 1
                    db.session.commit()
                    continue

                sp = db.session.get(SupplierProduct, imp.supplier_product_id)
                if not sp or not product.nm_id:
                    skipped += 1
                    job.processed = idx + 1
                    db.session.commit()
                    continue

                result = enrichment_service.apply_enrichment(
                    product,
                    imp,
                    ['photos'],
                    'replace',
                    seller,
                    wb_client,
                    is_bulk=True,
                )
                if result.get('success') and result.get('wb_sync'):
                    succeeded += 1
                    succeeded_ids.append(pid)
                elif (result.get('photos') or {}).get('skipped'):
                    skipped += 1
                else:
                    failed += 1
                    outcome_error = result.get('error') or 'не применилось'
            except Exception as e:
                failed += 1
                outcome_error = str(e)[:200]
                logger.error(f"[SupplierPhotos] product {pid}: {e}")
                db.session.rollback()

            if outcome_error and len(item_errors) < 50:
                item_errors.append({'product_id': pid, 'error': outcome_error})

            job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
            job.processed = idx + 1
            job.succeeded = succeeded
            job.failed_count = failed
            job.set_progress({'errors': item_errors, 'skipped': skipped})
            db.session.commit()

            # Пауза, чтобы не выедать общий пул Контент-API (100 req/мин)
            if idx + 1 < len(product_ids):
                time.sleep(JOB_ITEM_PAUSE_SECONDS)

        # Финализация
        job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
        if job.status != 'cancelled':
            job.status = 'completed'
        job.succeeded = succeeded
        job.failed_count = failed
        job.set_result({
            'succeeded': succeeded, 'failed': failed, 'skipped': skipped,
            'total': len(product_ids),
        })

        try:
            history = BulkEditHistory(
                seller_id=seller_id,
                operation_type=JOB_TYPE,
                operation_params={'job_uid': job_uid, 'field': 'photos'},
                description='Дозагрузка фото из каталога поставщика',
                total_products=len(product_ids),
                success_count=succeeded,
                error_count=failed,
                errors_details={'skipped': skipped, 'errors': item_errors[:20]},
            )
            db.session.add(history)
        except Exception as e:
            logger.warning(f"[SupplierPhotos] history write failed: {e}")

        if succeeded:
            try:
                db.session.add(Notification(
                    seller_id=seller_id,
                    category='success',
                    title='Фото от поставщика загружены',
                    message=(f'Обновлено карточек: {succeeded} из {len(product_ids)}'
                             + (f', ошибок: {failed}' if failed else '')),
                    link='/supplier-updates',
                ))
            except Exception as e:
                logger.warning(f"[SupplierPhotos] notification failed: {e}")

        db.session.commit()

        # Чипы поставщиков на странице хаба кешируются — сбрасываем после джобы
        try:
            from services.ttl_cache import cache
            cache.invalidate(f'supdates-chips:{seller_id}')
        except Exception:
            pass

        logger.info(
            f"[SupplierPhotos] job {job_uid} done: ok={succeeded} fail={failed} skip={skipped}"
        )

        # Авто-сверка с WB: provider read model обновляется не мгновенно, поэтому
        # ждём и одним проходом проверяем, что фото реально применились и
        # WB не вернул ошибок обработки. Результат — в result джобы ('verify').
        if succeeded_ids and job.status == 'completed':
            try:
                time.sleep(VERIFY_DELAY_SECONDS)
                report = verify_cards_on_wb(seller, succeeded_ids)
                job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
                result = job.get_result() or {}
                result['verify'] = report
                job.set_result(result)
                db.session.commit()
                logger.info(
                    f"[SupplierPhotos] job {job_uid} verify: {report['summary']}"
                )
            except Exception as e:
                logger.warning(f"[SupplierPhotos] verify после джобы не удался: {e}")
                db.session.rollback()
