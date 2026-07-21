# -*- coding: utf-8 -*-
"""
Мониторинг конкурентов v2: bounded-синхронизация через singleton scheduler.

Архитектура:
- никакого собственного threading: run_competitor_monitor_tick() вызывается
  scheduler-джобом раз в минуту и обрабатывает до 2 due-продавцов;
- фазирование: сначала ВСЯ сеть (fetch), затем один write-проход
  (savepoint на товар, общий commit) — SQLite-инвариант;
- честные наблюдения: fetch-miss не затирает current_* и не создаёт снимок;
- снимок только при успешном наблюдении с фактическим изменением;
- деактивация только по доказанному basket 404 (товар удалён с WB).

HTTP-слой — services/competitor_fetch.py (глобальный rate limiter,
circuit breaker, кросс-селлер кэш).
"""
import logging
import time
from datetime import datetime, timedelta

from services.competitor_fetch import (
    CompetitorFetchService, WBRateLimitedError,
    get_cached_observation, put_cached_observation,
)

logger = logging.getLogger(__name__)

MIN_SYNC_INTERVAL_MINUTES = 30
MAX_SYNC_INTERVAL_MINUTES = 1440
DEFAULT_SYNC_INTERVAL_MINUTES = 60

MAX_PRODUCTS_HARD_CAP = 1000
METADATA_REFRESH_DAYS = 7
METADATA_BATCH_PER_SYNC = 50
PRICE_MISS_RECHECK_THRESHOLD = 3
DEACTIVATE_AFTER_GONE = 20
SYNC_WALL_CLOCK_BUDGET_SECONDS = 90
SUPPLIER_PAGES_PER_SYNC = 5
BRAND_PAGES_PER_SYNC = 2
IMPORT_PAGES_PER_TICK = 3
IMPORT_MAX_PRODUCTS = 300

COMPETITOR_NOTIFICATION_TITLE = 'Конкуренты: изменения'
NOTIFICATION_DEDUP_HOURS = 4


def normalize_sync_interval_minutes(value):
    """Интервал между синками продавца: 30..1440 минут, default 60."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = DEFAULT_SYNC_INTERVAL_MINUTES
    return max(MIN_SYNC_INTERVAL_MINUTES,
               min(MAX_SYNC_INTERVAL_MINUTES, minutes))


def _generate_alerts(product, obs, settings):
    """Алерты по успешному наблюдению против current_* товара."""
    from models import CompetitorAlert

    alerts = []
    threshold = settings.price_change_alert_percent or 5.0
    discount_pp = settings.discount_alert_pp or 5.0

    old_sale = product.current_sale_price
    new_sale = obs.get('sale_price')
    if old_sale and new_sale and old_sale > 0:
        change_pct = round((new_sale - old_sale) / old_sale * 100, 2)
        if abs(change_pct) >= threshold:
            alert_type = 'price_drop' if change_pct < 0 else 'price_increase'
            severity = 'critical' if abs(change_pct) >= threshold * 2 else 'warning'
            alerts.append(CompetitorAlert(
                seller_id=product.seller_id, product_id=product.id,
                group_id=product.group_id, alert_type=alert_type,
                severity=severity, old_value=old_sale, new_value=new_sale,
                change_percent=change_pct,
                message=(f'{product.title or product.nm_id}: цена '
                         f'{"снизилась" if change_pct < 0 else "выросла"} '
                         f'на {abs(change_pct):.1f}% ({old_sale} → {new_sale} ₽)')))

    old_discount = None
    new_discount = None
    if product.current_price and old_sale and product.current_price > 0:
        old_discount = round((1 - old_sale / product.current_price) * 100, 1)
    if obs.get('price') and new_sale and obs['price'] > 0:
        new_discount = round((1 - new_sale / obs['price']) * 100, 1)
    if old_discount is not None and new_discount is not None:
        delta = round(new_discount - old_discount, 1)
        if abs(delta) >= discount_pp:
            alerts.append(CompetitorAlert(
                seller_id=product.seller_id, product_id=product.id,
                group_id=product.group_id,
                alert_type='discount_increase' if delta > 0 else 'discount_decrease',
                severity='warning' if abs(delta) >= discount_pp * 2 else 'info',
                old_value=old_discount, new_value=new_discount,
                change_percent=delta,
                message=(f'{product.title or product.nm_id}: скидка '
                         f'{"выросла" if delta > 0 else "уменьшилась"} '
                         f'на {abs(delta):.0f} п.п. '
                         f'({old_discount:.0f}% → {new_discount:.0f}%)')))

    old_stock = product.current_total_stock
    new_stock = obs.get('total_stock')
    if old_stock and old_stock > 0 and new_stock == 0:
        alerts.append(CompetitorAlert(
            seller_id=product.seller_id, product_id=product.id,
            group_id=product.group_id, alert_type='out_of_stock',
            severity='info', old_value=old_stock, new_value=0,
            message=(f'{product.title or product.nm_id}: товар закончился '
                     f'(было {old_stock} шт.)')))
    if old_stock is not None and old_stock == 0 and (new_stock or 0) > 0:
        alerts.append(CompetitorAlert(
            seller_id=product.seller_id, product_id=product.id,
            group_id=product.group_id, alert_type='back_in_stock',
            severity='info', old_value=0, new_value=new_stock,
            message=(f'{product.title or product.nm_id}: снова в наличии '
                     f'({new_stock} шт.)')))
    return alerts


def _observation_changed(product, obs):
    return (
        product.current_price != obs.get('price')
        or product.current_sale_price != obs.get('sale_price')
        or product.current_total_stock != obs.get('total_stock')
        or product.current_rating != obs.get('rating')
    )


def sync_seller_competitors(seller_id, flask_app, fetch_service=None, now=None):
    """Один bounded sync продавца. Возвращает summary-dict."""
    from models import (
        db, CompetitorMonitorSettings, CompetitorPriceSnapshot,
        CompetitorProduct,
    )

    started = time.time()
    now = now or datetime.utcnow()
    result = {'status': 'ok', 'observed': 0, 'misses': 0,
              'snapshots': 0, 'alerts': 0, 'deactivated': 0}

    with flask_app.app_context():
        settings = CompetitorMonitorSettings.query.filter_by(
            seller_id=seller_id).first()
        if not settings or not settings.is_enabled:
            result['status'] = 'disabled'
            return result

        settings.is_running = True
        settings.last_sync_status = 'running'
        db.session.commit()

        try:
            service = fetch_service or CompetitorFetchService(
                proxy_url=settings.proxy_url)

            limit = min(settings.max_products or MAX_PRODUCTS_HARD_CAP,
                        MAX_PRODUCTS_HARD_CAP)
            products = CompetitorProduct.query.filter_by(
                seller_id=seller_id, is_active=True,
            ).order_by(
                CompetitorProduct.priority.asc(),
                CompetitorProduct.last_fetched_at.asc().nullsfirst(),
            ).limit(limit).all()

            # ---------- Фаза A: сеть (write-транзакция не открыта) ----------
            def out_of_budget():
                return (time.time() - started) > SYNC_WALL_CLOCK_BUDGET_SECONDS

            # A0: заявки на импорт каталога продавца (только сеть; ORM не трогаем:
            # изменение флагов здесь открыло бы через autoflush write-транзакцию
            # SQLite посреди сетевых вызовов)
            from models import CompetitorGroup
            import_rows = []      # [(group_id, product_dict)]
            import_done = set()   # group_id, у которых заявку снимаем в B0
            import_groups = CompetitorGroup.query.filter_by(
                seller_id=seller_id, import_requested=True).all()
            import_existing = {
                g.id: CompetitorProduct.query.filter_by(group_id=g.id).count()
                for g in import_groups}
            for group in import_groups:
                try:
                    import_supplier_id = int(group.auto_source_value or 0)
                except (TypeError, ValueError):
                    import_supplier_id = 0
                if not import_supplier_id:
                    import_done.add(group.id)
                    continue
                fetched = 0
                exhausted = False
                for page in range(1, IMPORT_PAGES_PER_TICK + 1):
                    if out_of_budget() or (
                            import_existing[group.id] + fetched
                            >= IMPORT_MAX_PRODUCTS):
                        break
                    try:
                        items = service.fetch_seller_catalog_page(
                            import_supplier_id, page=page)
                    except WBRateLimitedError:
                        break
                    for item in items:
                        import_rows.append((group.id, item))
                    fetched += len(items)
                    if len(items) < 100:
                        exhausted = True
                        break
                if exhausted or (import_existing[group.id] + fetched
                                 >= IMPORT_MAX_PRODUCTS):
                    import_done.add(group.id)

            if not products and not import_rows:
                settings.last_sync_at = now
                settings.last_sync_status = 'idle'
                settings.total_products_monitored = 0
                settings.next_sync_due_at = now + timedelta(
                    minutes=normalize_sync_interval_minutes(
                        settings.sync_interval_minutes))
                settings.is_running = False
                for done_group_id in import_done:
                    grp = db.session.get(CompetitorGroup, done_group_id)
                    if grp:
                        grp.import_requested = False
                db.session.commit()
                result['status'] = 'no_products'
                return result

            # A1: метаданные — отсутствующие/протухшие/подозрительные на gone
            metadata_cutoff = now - timedelta(days=METADATA_REFRESH_DAYS)
            meta_targets = [
                p for p in products
                if p.metadata_synced_at is None
                or p.metadata_synced_at < metadata_cutoff
                or (p.price_miss_count or 0) >= PRICE_MISS_RECHECK_THRESHOLD
            ][:METADATA_BATCH_PER_SYNC]
            meta_results = {}
            for p in meta_targets:
                if out_of_budget():
                    result['status'] = 'partial'
                    break
                meta_results[p.nm_id] = service.fetch_basket_metadata(p.nm_id)

            # A2: цены — кэш, затем каталог продавца, затем search по бренду
            observations = {}
            uncached = []
            for p in products:
                cached = get_cached_observation(p.nm_id)
                if cached is not None:
                    observations[p.nm_id] = cached
                else:
                    uncached.append(p)

            def supplier_of(p):
                meta = meta_results.get(p.nm_id)
                if isinstance(meta, dict) and meta.get('wb_supplier_id'):
                    return meta['wb_supplier_id']
                return p.wb_supplier_id

            suppliers = {}
            for p in uncached:
                sid = supplier_of(p)
                if sid:
                    suppliers.setdefault(sid, set()).add(p.nm_id)
            for sid, nm_ids in suppliers.items():
                if out_of_budget():
                    result['status'] = 'partial'
                    break
                found = service.fetch_supplier_prices(
                    sid, nm_ids, max_pages=SUPPLIER_PAGES_PER_SYNC)
                observations.update(found)

            remaining = [p for p in uncached if p.nm_id not in observations]
            brands = {}
            for p in remaining:
                meta = meta_results.get(p.nm_id)
                brand = ((meta.get('brand') if isinstance(meta, dict) else None)
                         or p.brand)
                if brand:
                    brands.setdefault(brand, set()).add(p.nm_id)
            for brand, nm_ids in brands.items():
                if out_of_budget():
                    result['status'] = 'partial'
                    break
                found = service.fetch_brand_prices(
                    brand, nm_ids, max_pages=BRAND_PAGES_PER_SYNC)
                observations.update(found)

            for nm_id, obs in observations.items():
                put_cached_observation(nm_id, obs)

            # ---------- Фаза B: запись ----------
            # B0: создать товары из импорта каталога (идемпотентно по nm_id)
            for group_id, item in import_rows:
                nm_id = item.get('nm_id')
                if not nm_id:
                    continue
                try:
                    with db.session.begin_nested():
                        existing = CompetitorProduct.query.filter_by(
                            seller_id=seller_id, nm_id=nm_id,
                            group_id=group_id).first()
                        if existing:
                            continue
                        if CompetitorProduct.query.filter_by(
                                group_id=group_id,
                        ).count() >= IMPORT_MAX_PRODUCTS:
                            break
                        db.session.add(CompetitorProduct(
                            seller_id=seller_id, group_id=group_id, nm_id=nm_id,
                            title=item.get('title'), brand=item.get('brand'),
                            supplier_name=item.get('supplier_name'),
                            wb_supplier_id=item.get('wb_supplier_id'),
                            image_url=item.get('image_url'),
                            current_price=item.get('price'),
                            current_sale_price=item.get('sale_price'),
                            current_rating=item.get('rating'),
                            current_feedbacks_count=item.get('feedbacks_count'),
                            current_total_stock=item.get('total_stock'),
                            metadata_synced_at=now,
                            last_price_at=(now if item.get('sale_price') is not None
                                           else None),
                            last_fetched_at=now))
                except Exception:
                    logger.exception('Импорт товара %s в группу %s не удался',
                                     nm_id, group_id)

            for done_group_id in import_done:
                grp = db.session.get(CompetitorGroup, done_group_id)
                if grp:
                    grp.import_requested = False

            new_alerts = []
            for product in products:
                try:
                    with db.session.begin_nested():
                        meta = meta_results.get(product.nm_id)
                        if meta == 'gone':
                            product.fetch_error_count = (
                                product.fetch_error_count or 0) + 1
                            if product.fetch_error_count >= DEACTIVATE_AFTER_GONE:
                                product.is_active = False
                                result['deactivated'] += 1
                        elif isinstance(meta, dict):
                            product.fetch_error_count = 0
                            product.title = meta.get('title') or product.title
                            product.brand = meta.get('brand') or product.brand
                            product.supplier_name = (
                                meta.get('supplier_name') or product.supplier_name)
                            product.wb_supplier_id = (
                                meta.get('wb_supplier_id') or product.wb_supplier_id)
                            product.image_url = (
                                meta.get('image_url') or product.image_url)
                            if meta.get('is_adult') is not None:
                                product.is_adult = meta['is_adult']
                            product.metadata_synced_at = now

                        obs = observations.get(product.nm_id)
                        price_observed = obs is not None and (
                            obs.get('sale_price') is not None
                            or obs.get('price') is not None)

                        if price_observed:
                            first = (product.current_price is None
                                     and product.current_sale_price is None)
                            changed = _observation_changed(product, obs)
                            if first or changed:
                                change_pct = None
                                if (product.current_sale_price
                                        and obs.get('sale_price')
                                        and product.current_sale_price > 0):
                                    change_pct = round(
                                        (obs['sale_price']
                                         - product.current_sale_price)
                                        / product.current_sale_price * 100, 2)
                                db.session.add(CompetitorPriceSnapshot(
                                    product_id=product.id, seller_id=seller_id,
                                    price=obs.get('price'),
                                    sale_price=obs.get('sale_price'),
                                    rating=obs.get('rating'),
                                    feedbacks_count=obs.get('feedbacks_count'),
                                    total_stock=obs.get('total_stock'),
                                    price_change_percent=change_pct,
                                    created_at=now))
                                result['snapshots'] += 1
                                if not first:
                                    alerts = _generate_alerts(product, obs, settings)
                                    for a in alerts:
                                        db.session.add(a)
                                    new_alerts.extend(alerts)
                                    result['alerts'] += len(alerts)
                            product.current_price = obs.get('price')
                            product.current_sale_price = obs.get('sale_price')
                            product.current_rating = obs.get('rating')
                            product.current_feedbacks_count = obs.get(
                                'feedbacks_count')
                            product.current_total_stock = obs.get('total_stock')
                            product.last_price_at = now
                            product.price_miss_count = 0
                            result['observed'] += 1
                        elif meta != 'gone':
                            product.price_miss_count = (
                                product.price_miss_count or 0) + 1
                            result['misses'] += 1

                        product.last_fetched_at = now
                except Exception:
                    logger.exception('Ошибка записи товара %s (seller=%s)',
                                     product.nm_id, seller_id)

            settings.last_sync_at = now
            settings.last_sync_status = (
                'success' if result['status'] == 'ok' else 'partial')
            settings.last_sync_error = None
            settings.last_full_cycle_duration = round(time.time() - started, 2)
            settings.total_products_monitored = result['observed']
            settings.total_cycles_completed = (
                settings.total_cycles_completed or 0) + 1
            settings.next_sync_due_at = now + timedelta(
                minutes=normalize_sync_interval_minutes(
                    settings.sync_interval_minutes))
            settings.is_running = False
            db.session.commit()

            _notify_new_alerts(seller_id, new_alerts)
            return result

        except Exception as e:
            db.session.rollback()
            logger.exception('[Seller %s] Sync упал: %s', seller_id, e)
            settings = CompetitorMonitorSettings.query.filter_by(
                seller_id=seller_id).first()
            if settings:
                settings.is_running = False
                settings.last_sync_status = 'failed'
                settings.last_sync_error = str(e)[:500]
                settings.next_sync_due_at = now + timedelta(
                    minutes=normalize_sync_interval_minutes(
                        settings.sync_interval_minutes))
                db.session.commit()
            result['status'] = 'failed'
            return result


def _create_notification_compat(**kwargs):
    """Тонкая обёртка для тестируемости (patched в unit-тестах)."""
    from seller_platform import create_notification
    return create_notification(**kwargs)


def _notify_new_alerts(seller_id, new_alerts):
    """Одно агрегированное уведомление в общий центр, дедуп 4 часа."""
    if not new_alerts:
        return
    from models import Notification

    cutoff = datetime.utcnow() - timedelta(hours=NOTIFICATION_DEDUP_HOURS)
    recent = Notification.query.filter(
        Notification.seller_id == seller_id,
        Notification.title == COMPETITOR_NOTIFICATION_TITLE,
        Notification.created_at >= cutoff,
    ).first()
    if recent:
        return

    severities = {a.severity for a in new_alerts}
    category = ('error' if 'critical' in severities
                else 'warning' if 'warning' in severities else 'info')
    price_changes = sum(1 for a in new_alerts
                        if a.alert_type in ('price_drop', 'price_increase'))
    discount_changes = sum(1 for a in new_alerts
                           if a.alert_type.startswith('discount'))
    stock_events = sum(1 for a in new_alerts
                       if a.alert_type in ('out_of_stock', 'back_in_stock'))
    parts = []
    if price_changes:
        parts.append(f'изменений цены: {price_changes}')
    if discount_changes:
        parts.append(f'изменений скидки: {discount_changes}')
    if stock_events:
        parts.append(f'событий наличия: {stock_events}')
    try:
        _create_notification_compat(
            seller_id=seller_id, category=category,
            title=COMPETITOR_NOTIFICATION_TITLE,
            message='У конкурентов ' + ', '.join(parts) + '.',
            link='/competitors/alerts')
    except Exception:
        logger.exception('Не удалось создать уведомление о конкурентах '
                         '(seller=%s)', seller_id)


def compact_competitor_snapshots(flask_app, max_seconds=55, chunk_size=5000):
    """
    Чанковая компакция без длинного write-lock:
    1) all-NULL снимки (исторический мусор fetch-miss'ов v1);
    2) подряд идущие дубликаты per product (NULL-safe сравнение);
    3) прочитанные алерты старше 90 дней.
    Каждый чанк — отдельная короткая транзакция.
    """
    from models import db

    started = time.time()
    out = {'deleted_null': 0, 'deleted_dup': 0, 'deleted_alerts': 0,
           'complete': True}

    def out_of_time():
        return (time.time() - started) > max_seconds

    with flask_app.app_context():
        # 1) all-NULL
        while True:
            if out_of_time():
                out['complete'] = False
                return out
            res = db.session.execute(db.text("""
                DELETE FROM competitor_price_snapshots WHERE id IN (
                    SELECT id FROM competitor_price_snapshots
                    WHERE price IS NULL AND sale_price IS NULL
                      AND total_stock IS NULL AND rating IS NULL
                    LIMIT :chunk)
            """), {'chunk': chunk_size})
            db.session.commit()
            out['deleted_null'] += res.rowcount
            if res.rowcount < chunk_size:
                break

        # 2) подряд-дубликаты (LAG, NULL-safe IS)
        while True:
            if out_of_time():
                out['complete'] = False
                return out
            res = db.session.execute(db.text("""
                DELETE FROM competitor_price_snapshots WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               price IS LAG(price) OVER w
                               AND sale_price IS LAG(sale_price) OVER w
                               AND total_stock IS LAG(total_stock) OVER w
                               AND rating IS LAG(rating) OVER w AS is_dup
                        FROM competitor_price_snapshots
                        WINDOW w AS (PARTITION BY product_id
                                     ORDER BY created_at, id)
                    ) WHERE is_dup LIMIT :chunk)
            """), {'chunk': chunk_size})
            db.session.commit()
            out['deleted_dup'] += res.rowcount
            if res.rowcount < chunk_size:
                break

        # 3) ретеншн прочитанных алертов
        cutoff = datetime.utcnow() - timedelta(days=90)
        while True:
            if out_of_time():
                out['complete'] = False
                return out
            res = db.session.execute(db.text("""
                DELETE FROM competitor_alerts WHERE id IN (
                    SELECT id FROM competitor_alerts
                    WHERE is_read = 1 AND created_at < :cutoff
                    LIMIT :chunk)
            """), {'cutoff': cutoff, 'chunk': chunk_size})
            db.session.commit()
            out['deleted_alerts'] += res.rowcount
            if res.rowcount < chunk_size:
                break

    return out


def run_competitor_monitor_tick(flask_app, seller_limit=2):
    """Scheduler-джоб: выбрать до seller_limit due-продавцов и синхронизировать."""
    from models import CompetitorMonitorSettings, db

    now = datetime.utcnow()
    with flask_app.app_context():
        due = CompetitorMonitorSettings.query.filter(
            CompetitorMonitorSettings.is_enabled.is_(True),
            db.or_(
                CompetitorMonitorSettings.next_sync_due_at.is_(None),
                CompetitorMonitorSettings.next_sync_due_at <= now,
            ),
        ).order_by(
            CompetitorMonitorSettings.next_sync_due_at.asc().nullsfirst(),
        ).limit(seller_limit).all()
        seller_ids = [s.seller_id for s in due]

    synced = []
    for seller_id in seller_ids:
        try:
            sync_seller_competitors(seller_id, flask_app)
            synced.append(seller_id)
        except Exception:
            logger.exception('Tick: sync продавца %s упал', seller_id)
    return {'synced': synced}
