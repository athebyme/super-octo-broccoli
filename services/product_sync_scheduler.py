"""
Планировщик автоматической синхронизации товаров
Использует APScheduler для запуска синхронизации по расписанию
"""
import logging
import os
import threading
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

BRAND_SYNC_RESUME_MINUTES = 10

# Глобальный планировщик
scheduler = None
_scheduler_lock_handle = None
_scheduler_lock_retry_thread = None
_scheduler_lock_retry_stop = threading.Event()


def _acquire_scheduler_process_lock() -> bool:
    """Elect one scheduler process inside a multi-worker web container."""
    global _scheduler_lock_handle

    if _scheduler_lock_handle is not None:
        return True

    try:
        import fcntl
    except ImportError:
        logger.warning("fcntl is unavailable; scheduler process lock is disabled")
        return True

    lock_path = os.environ.get(
        'SCHEDULER_LOCK_FILE', '/tmp/seller-platform-scheduler.lock',
    )
    try:
        handle = open(lock_path, 'a+', encoding='utf-8')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        if 'handle' in locals():
            handle.close()
        return False

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _scheduler_lock_handle = handle
    return True


def _release_scheduler_process_lock() -> None:
    global _scheduler_lock_handle

    handle = _scheduler_lock_handle
    _scheduler_lock_handle = None
    if handle is None:
        return
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass
    handle.close()


def _scheduler_lock_retry_loop(flask_app, wait_fn=None) -> None:
    """Take over scheduling after the previous Gunicorn worker exits."""
    wait_fn = wait_fn or _scheduler_lock_retry_stop.wait
    try:
        retry_seconds = max(
            1.0,
            float(os.environ.get('SCHEDULER_LOCK_RETRY_SECONDS', '15')),
        )
    except (TypeError, ValueError):
        retry_seconds = 15.0

    while not wait_fn(retry_seconds):
        if not _acquire_scheduler_process_lock():
            continue
        logger.info("Scheduler ownership transferred to this web worker")
        init_scheduler(flask_app, retry_if_locked=False)
        return


def _start_scheduler_lock_retry(flask_app) -> None:
    global _scheduler_lock_retry_thread

    if (
        _scheduler_lock_retry_thread is not None
        and _scheduler_lock_retry_thread.is_alive()
    ):
        return
    _scheduler_lock_retry_stop.clear()
    _scheduler_lock_retry_thread = threading.Thread(
        target=_scheduler_lock_retry_loop,
        args=(flask_app,),
        name='scheduler-lock-contender',
        daemon=True,
    )
    _scheduler_lock_retry_thread.start()


def parse_sales_funnel_metrics(api_response: dict) -> dict:
    """Извлечь рейтинги и метрики воронки {nm_id: {...}} из ответа sales-funnel v3.

    Все поля опциональны (None = нет данных). Фактическая схема WB v3
    (проверена живым вызовом): item.statistic.selected{openCount, orderCount,
    conversions{addToCartPercent, cartToOrderPercent, buyoutPercent}}.
    Допускается и старая форма statistics.selectedPeriod{openCardCount, ...}.
    """
    out = {}
    data = (api_response or {}).get('data') or {}
    for item in data.get('products', []) or []:
        if not isinstance(item, dict):
            continue
        prod = item.get('product')
        if not isinstance(prod, dict):
            continue
        nm = prod.get('nmId', prod.get('nmID'))
        if nm is None:
            continue
        stat = item.get('statistic') if isinstance(item.get('statistic'), dict) else {}
        sel = stat.get('selected') if isinstance(stat.get('selected'), dict) else {}
        if not sel:
            stats = item.get('statistics') if isinstance(item.get('statistics'), dict) else {}
            sel = stats.get('selectedPeriod') if isinstance(stats.get('selectedPeriod'), dict) else {}
        conv = sel.get('conversions') if isinstance(sel.get('conversions'), dict) else {}
        out[int(nm)] = {
            'product_rating': prod.get('productRating'),
            'feedback_rating': prod.get('feedbackRating'),
            'views': sel.get('openCount', sel.get('openCardCount')),
            'orders': sel.get('orderCount', sel.get('ordersCount')),
            'cart_conv': conv.get('addToCartPercent'),
            'order_conv': conv.get('cartToOrderPercent'),
            'buyout_rate': conv.get('buyoutPercent', conv.get('buyoutsPercent')),
        }
    return out


def init_scheduler(flask_app, *, retry_if_locked=True):
    """
    Инициализировать планировщик автоматической синхронизации

    Args:
        flask_app: Экземпляр Flask приложения
    """
    global scheduler

    if scheduler is not None:
        logger.warning("Scheduler already initialized")
        return scheduler

    if not _acquire_scheduler_process_lock():
        logger.info("Scheduler is already owned by another web worker")
        if retry_if_locked:
            _start_scheduler_lock_retry(flask_app)
        return None

    logger.info("🕐 Initializing product sync scheduler...")

    # Сброс зависших статусов синхронизации после перезапуска
    with flask_app.app_context():
        try:
            from models import Seller, db as _db
            stuck = Seller.query.filter(Seller.api_sync_status == 'syncing').all()
            if stuck:
                for s in stuck:
                    logger.warning(f"Resetting stuck sync status for seller {s.id} (was 'syncing' at startup)")
                    s.api_sync_status = 'error'
                    if s.product_sync_settings:
                        s.product_sync_settings.last_sync_status = 'error'
                        s.product_sync_settings.last_sync_error = 'Sync interrupted by server restart'
                _db.session.commit()
                logger.info(f"✅ Reset {len(stuck)} stuck sync statuses")
        except Exception as e:
            logger.error(f"Failed to reset stuck sync statuses: {e}")

        # Сброс зависших авто-публикаций после перезапуска
        try:
            from services.auto_publish_service import reset_stuck_auto_publish
            reset_stuck_auto_publish(flask_app)
        except Exception as e:
            logger.error(f"Failed to reset stuck auto-publish runs: {e}")

    # Создаем фоновый планировщик
    scheduler = BackgroundScheduler(
        daemon=True,
        timezone='UTC'
    )

    # Добавляем задачу проверки настроек синхронизации (каждые 5 минут)
    scheduler.add_job(
        func=lambda: check_and_sync_all_sellers(flask_app),
        trigger=IntervalTrigger(minutes=5),
        id='check_sync_settings',
        name='Check sync settings for all sellers',
        replace_existing=True
    )

    # Сверка отправленных цен с фактическими на WB (каждые 5 минут).
    # Без неё позиции навсегда зависали бы в статусе «отправлено».
    scheduler.add_job(
        func=lambda: reconcile_submitted_prices(flask_app),
        trigger=IntervalTrigger(minutes=5),
        id='reconcile_submitted_prices',
        name='Reconcile submitted WB prices with actual marketplace prices',
        replace_existing=True
    )

    # Добавляем задачу проверки настроек мониторинга цен (каждые 5 минут)
    scheduler.add_job(
        func=lambda: check_and_monitor_prices_all_sellers(flask_app),
        trigger=IntervalTrigger(minutes=5),
        id='check_price_monitoring',
        name='Check price monitoring settings for all sellers',
        replace_existing=True
    )

    # Задача синхронизации заблокированных карточек (каждые 10 минут)
    scheduler.add_job(
        func=lambda: sync_blocked_cards_all_sellers(flask_app),
        trigger=IntervalTrigger(minutes=10),
        id='sync_blocked_cards',
        name='Sync blocked/shadowed cards for all sellers',
        replace_existing=True
    )

    # Синхронизация рейтингов карточек WB и пересчёт Quality Score (каждые 6 часов)
    scheduler.add_job(
        func=lambda: sync_card_ratings_all_sellers(flask_app),
        trigger=IntervalTrigger(hours=6),
        id='sync_card_ratings',
        name='Sync WB card ratings and recompute quality scores',
        replace_existing=True
    )

    # Задача регулярной фоновой синхронизации общих справочников маркетплейсов (каждые 24 часа)
    scheduler.add_job(
        func=lambda: sync_marketplaces(flask_app),
        trigger=IntervalTrigger(hours=24),
        id='sync_marketplaces_data',
        name='Sync marketplace directories and categories globally',
        replace_existing=True
    )

    # Характеристики меняются независимо от дерева категорий. Обновляем только
    # bounded batch самых старых включённых схем, чтобы не создавать API burst.
    scheduler.add_job(
        func=lambda: sync_marketplace_characteristics(flask_app),
        trigger=IntervalTrigger(hours=6),
        id='sync_marketplace_characteristics',
        name='Refresh stale marketplace characteristic schemas (bounded)',
        replace_existing=True,
    )

    # Seller-visible Ozon types do not require a global 8k-schema preload.
    # Once a draft/mapping/listing references an exact type, this small,
    # frequent read-only worker loads its schema and required dictionaries.
    scheduler.add_job(
        func=lambda: sync_ozon_demanded_references(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='sync_ozon_demanded_references',
        name='Refresh seller-demanded Ozon references (bounded)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # One explicit connect/sync request survives page closure and restarts.
    # No new thread pool: one account / one fully validated read page per tick.
    from services.ozon_account_sync import run_account_sync_tick
    scheduler.add_job(
        func=lambda: run_account_sync_tick(flask_app),
        trigger=IntervalTrigger(seconds=10),
        id='ozon_account_sync',
        name='Check Ozon connection and load requested catalog in background',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    from services.ozon_quality_queue import run_quality_tick
    scheduler.add_job(
        func=lambda: run_quality_tick(flask_app),
        trigger=IntervalTrigger(seconds=10),
        id='ozon_quality_recompute',
        name='Recompute requested Ozon quality from local facts',
        replace_existing=True, max_instances=1, coalesce=True,
    )

    from services.ozon_credential_notices import run_credential_notice_tick
    scheduler.add_job(
        func=lambda: run_credential_notice_tick(flask_app),
        trigger=IntervalTrigger(minutes=15),
        id='ozon_credential_notices',
        next_run_time=datetime.utcnow() + timedelta(seconds=60),
        name='Warn sellers before observed Ozon key expiry (local only)',
        replace_existing=True, max_instances=1, coalesce=True,
    )

    from services.ozon_catalog_scheduler import run_catalog_discovery_tick
    scheduler.add_job(
        func=lambda: run_catalog_discovery_tick(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='ozon_catalog_discovery',
        name='Refresh stale Ozon catalogs through the durable read queue',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Первый reference refresh не должен ждать сутки после нового deploy.
    scheduler.add_job(
        func=lambda: sync_marketplaces(flask_app),
        trigger='date',
        run_date=datetime.utcnow() + timedelta(seconds=90),
        id='sync_marketplaces_initial',
        name='Initial marketplace reference refresh',
        replace_existing=True,
    )

    scheduler.add_job(
        func=lambda: sync_marketplace_characteristics(flask_app, limit=200),
        trigger='date',
        run_date=datetime.utcnow() + timedelta(seconds=180),
        id='sync_marketplace_characteristics_initial',
        name='Initial marketplace characteristic schema refresh',
        replace_existing=True,
    )

    # Admin supplier-card enrichment is durable: the HTTP request only
    # creates exact run/item rows, while this bounded worker resumes them
    # after a process restart. A per-supplier file claim also coordinates the
    # immediate kick from the admin UI in the current singleton deployment.
    scheduler.add_job(
        func=lambda: process_supplier_catalog_enrichment_runs(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='supplier_catalog_enrichment',
        name='Process durable shared supplier catalog enrichment runs',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Seller WB enrichment has two durable phases: a leased dispatch cursor
    # and read-only confirmation against the asynchronous WB read model. The
    # same singleton tick resumes both after a web-worker/container restart.
    scheduler.add_job(
        func=lambda: process_wb_supplier_enrichment(flask_app),
        trigger=IntervalTrigger(seconds=20),
        id='wb_supplier_enrichment',
        name='Dispatch and reconcile durable WB supplier enrichment',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Уведомление продавцам о свежих обновлениях общих карточек поставщика:
    # локальный SQL-подсчёт без provider/LLM, не чаще одного раза в сутки
    # на продавца (дедуп по заголовку за 24 часа).
    scheduler.add_job(
        func=lambda: notify_supplier_updates(flask_app),
        trigger=IntervalTrigger(hours=6),
        id='notify_supplier_updates',
        name='Notify sellers about pending supplier card updates',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Reviewed media writes and their read-after-write reconciliation are
    # durable even when the optional Image Lab worker profile is disabled.
    # Per-target file claims make overlap with the immediate UI kick harmless.
    scheduler.add_job(
        func=lambda: process_marketplace_media_publications(flask_app),
        trigger=IntervalTrigger(seconds=15),
        id='marketplace_media_publications',
        name='Process and reconcile reviewed marketplace media operations',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # P11 strangler maintenance never calls WB/Ozon.  Each seller advances by
    # at most 200 Product rows and a short DB lease prevents duplicate batches
    # if another worker/CLI overlaps the scheduler.
    scheduler.add_job(
        func=lambda: maintain_marketplace_projection(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='maintain_marketplace_projection',
        name='Backfill WB listing projection and collect dual-read parity',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        func=lambda: maintain_marketplace_projection(flask_app),
        trigger='date',
        run_date=datetime.utcnow() + timedelta(seconds=15),
        id='maintain_marketplace_projection_initial',
        name='Initial bounded WB listing projection maintenance',
        replace_existing=True,
        max_instances=1,
    )

    # Existing Ozon offers and WB vendor codes may use different historical
    # seller suffixes around the same supplier ID.  Reconcile that local
    # backlog by a durable keyset (no provider/LLM), at most three account
    # scopes and 200 listings per minute.
    scheduler.add_job(
        func=lambda: maintain_marketplace_source_links(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='maintain_marketplace_source_links',
        name='Reconcile exact supplier identities across Ozon and WB',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        func=lambda: maintain_marketplace_source_links(flask_app),
        trigger='date',
        run_date=datetime.utcnow() + timedelta(seconds=30),
        id='maintain_marketplace_source_links_initial',
        name='Initial exact Ozon/WB source identity reconciliation',
        replace_existing=True,
        max_instances=1,
    )

    # Durable Ozon operations must keep reconciling after a rollout flag is
    # disabled. Only definitely-not-submitted queued rows require the separate
    # publication flag; submitting/uncertain rows are never abandoned.
    scheduler.add_job(
        func=lambda: poll_ozon_marketplace_operations(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='poll_ozon_marketplace_operations',
        name='Poll and reconcile durable Ozon product operations',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Local-only preparation has a separate short cadence: it neither occupies
    # the provider poll's minute slot nor starts its own thread or executor.
    scheduler.add_job(
        func=lambda: prepare_ozon_uploads(flask_app),
        trigger=IntervalTrigger(seconds=10),
        id='prepare_ozon_uploads',
        name='Prepare reviewed Ozon card batches locally',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        func=lambda: process_ozon_draft_ai_completions(flask_app),
        trigger=IntervalTrigger(seconds=10),
        id='process_ozon_draft_ai_completions',
        name='Process optional local Ozon AI suggestions',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        func=lambda: reconcile_ozon_auto_publish_runs(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='reconcile_ozon_auto_publish_runs',
        name='Reflect durable Ozon operations in account-scoped auto-publish runs',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        func=lambda: poll_ozon_commercial_operations(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='poll_ozon_commercial_operations',
        name='Reconcile reviewed Ozon price and stock operations',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Фоновая синхронизация брендов с WB (каждые 6 часов)
    scheduler.add_job(
        func=lambda: sync_brands_background(flask_app),
        trigger=IntervalTrigger(hours=6),
        id='brand_wb_sync',
        name='Sync brands from WB API',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Categories are refreshed at +90s and stale schemas at +180s. Start the
    # category-scoped brand cycle afterwards so a deploy does not leave agent
    # validation blocked until the first six-hour interval tick.
    scheduler.add_job(
        func=lambda: sync_brands_background(flask_app),
        trigger='date',
        run_date=datetime.utcnow() + timedelta(seconds=360),
        id='brand_wb_sync_initial',
        name='Initial WB brand reference refresh',
        replace_existing=True,
        max_instances=1,
    )

    # Continue a bounded multi-run sweep promptly, but only while a durable
    # checkpoint exists. The brand engine advisory lock handles overlap with
    # manual/regular runs.
    scheduler.add_job(
        func=lambda: resume_brand_sync_if_needed(flask_app),
        trigger=IntervalTrigger(minutes=BRAND_SYNC_RESUME_MINUTES),
        id='brand_wb_sync_resume',
        name='Resume partial WB brand reference sweep',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Авто-резолв pending брендов (каждый час)
    scheduler.add_job(
        func=lambda: auto_resolve_pending_brands(flask_app),
        trigger=IntervalTrigger(hours=1),
        id='brand_auto_resolve',
        name='Auto-resolve pending brands',
        replace_existing=True
    )

    # Синхронизация аналитических данных WB (каждые 3 часа)
    scheduler.add_job(
        func=lambda: sync_wb_analytics_all_sellers(flask_app),
        trigger=IntervalTrigger(hours=3),
        id='wb_analytics_sync',
        name='Sync WB analytics data (sales, orders, feedbacks, realization)',
        replace_existing=True
    )

    # Explicit seller refreshes share the durable schedule/cooldown with cron.
    scheduler.add_job(
        func=lambda: sync_ozon_requested_reads(flask_app),
        trigger=IntervalTrigger(seconds=10),
        id='ozon_requested_reads',
        name='Process requested Ozon refreshes (read-only)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Ozon analytics is a separate account-scoped read model. A ten-minute
    # bounded runner both resumes large pages and starts snapshots whose
    # four-hour cache expired; it never writes to Ozon.
    scheduler.add_job(
        func=lambda: sync_ozon_analytics_accounts(flask_app),
        trigger=IntervalTrigger(minutes=10),
        id='ozon_analytics_sync',
        name='Sync account-scoped Ozon analytics facts (read-only)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Orders, returns and cancellations use a separate read-only projection.
    # The durable phase/cursor lets this bounded job resume large accounts
    # without ever invoking shipment, refund or cancellation writes.
    scheduler.add_job(
        func=lambda: sync_ozon_fulfillment_accounts(flask_app),
        trigger=IntervalTrigger(minutes=10),
        id='ozon_fulfillment_sync',
        name='Sync account-scoped Ozon orders and returns (read-only)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Ozon finance is an immutable snapshot projection. The runner only reads
    # current accrual endpoints and keeps partial snapshots invisible until all
    # requested days have passed strict normalization.
    scheduler.add_job(
        func=lambda: sync_ozon_finance_accounts(flask_app),
        trigger=IntervalTrigger(minutes=10),
        id='ozon_finance_sync',
        name='Sync account-scoped Ozon accrual facts (read-only)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Reviews and questions are capability-gated Premium APIs. This runner
    # only selects account/kind pairs whose exact read method was confirmed by
    # /v1/roles; reply drafts remain local and no provider write is registered.
    scheduler.add_job(
        func=lambda: sync_ozon_inbox_accounts(flask_app),
        trigger=IntervalTrigger(minutes=15),
        id='ozon_inbox_sync',
        name='Sync account-scoped Ozon reviews and questions (read-only)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Первоначальная загрузка аналитики через 30 сек после старта (если данных нет)
    scheduler.add_job(
        func=lambda: initial_analytics_sync_if_empty(flask_app),
        trigger='date',
        run_date=datetime.utcnow() + timedelta(seconds=30),
        id='wb_analytics_initial_sync',
        name='Initial WB analytics sync (if tables empty)',
        replace_existing=True
    )

    # Автогенерация контента (каждые 3 минуты проверяет фабрики с auto_generate)
    from services.content_auto_publisher import auto_generate_content, auto_publish_content
    scheduler.add_job(
        func=lambda: auto_generate_content(flask_app),
        trigger=IntervalTrigger(minutes=3),
        id='content_auto_generate',
        name='Auto-generate content for factories',
        replace_existing=True
    )

    # Автопубликация контента (каждую минуту проверяет очередь)
    scheduler.add_job(
        func=lambda: auto_publish_content(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='content_auto_publish',
        name='Auto-publish approved content items',
        replace_existing=True
    )

    # Мониторинг конкурентов v2: bounded tick (до 2 due-продавцов за минуту)
    scheduler.add_job(
        func=lambda: _run_competitor_monitor_tick(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='competitor_monitor_tick',
        name='Sync due competitor monitor sellers (bounded)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Shared competitor identity matching: one bounded seller job at a time.
    # Supplier/photo/LLM evidence is cached globally by nmID fingerprint;
    # seller decisions and price comparisons remain tenant-scoped.
    scheduler.add_job(
        func=lambda: _run_competitor_matching_tick(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='competitor_matching_tick',
        name='Match competitor nmIDs to shared supplier catalog (bounded)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Компакция старых снимков конкурентов (раз в сутки)
    scheduler.add_job(
        func=lambda: _compact_competitor_snapshots(flask_app),
        trigger=IntervalTrigger(hours=24),
        id='competitor_snapshot_compaction',
        name='Compact old competitor price snapshots',
        replace_existing=True
    )

    # Account-scoped WB/Ozon auto-publish (каждые 5 минут).
    from services.auto_publish_service import check_and_auto_publish_all_sellers
    scheduler.add_job(
        func=lambda: check_and_auto_publish_all_sellers(flask_app),
        trigger=IntervalTrigger(minutes=5),
        id='auto_publish_products',
        name='Auto-publish marketplace product drafts',
        replace_existing=True
    )

    from services.wb_stock_sync import run_stock_sync_tick
    scheduler.add_job(
        func=lambda: run_stock_sync_tick(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='wb_warehouse_stocks',
        name='Read bounded WB warehouse stock page',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    from services.scheduler_heartbeat import INTERVAL_SECONDS
    scheduler.add_job(
        func=publish_scheduler_heartbeat,
        trigger=IntervalTrigger(seconds=INTERVAL_SECONDS),
        id='scheduler_heartbeat', name='Observe scheduler executor progress',
        replace_existing=True, max_instances=1, coalesce=True,
    )

    # Запускаем планировщик
    scheduler.start()
    publish_scheduler_heartbeat()

    logger.info("✅ Product sync scheduler started")

    return scheduler


def publish_scheduler_heartbeat():
    if scheduler is None or not scheduler.running or _scheduler_lock_handle is None:
        return
    try:
        from services.scheduler_heartbeat import publish
        publish(_scheduler_lock_handle)
    except Exception as exc:
        logger.warning('Scheduler heartbeat unavailable: %s', type(exc).__name__)


def process_marketplace_media_publications(flask_app):
    """Advance bounded reviewed media writes and safety reconciliation."""
    from services.marketplace_media_publications import (
        process_pending_once,
        recover_stale_operations,
    )
    try:
        recover_stale_operations(flask_app, limit=20)
        process_pending_once(flask_app, limit=4)
    except Exception:
        logger.exception('Marketplace media publication scheduler tick failed')


def process_supplier_catalog_enrichment_runs(flask_app):
    """Advance a bounded number of durable admin enrichment runs."""
    with flask_app.app_context():
        from models import db
        from services.supplier_catalog_enrichment import (
            SupplierCatalogEnrichmentService,
        )
        try:
            SupplierCatalogEnrichmentService.process_due_runs(limit=2)
        except Exception:
            logger.exception('Supplier catalog enrichment scheduler tick failed')
        finally:
            db.session.remove()


def process_wb_supplier_enrichment(flask_app):
    """Advance durable seller enrichment and confirm accepted WB writes."""
    try:
        from services.supplier_enrichment import process_due_enrichment_jobs
        process_due_enrichment_jobs(flask_app, max_jobs=1)
    except Exception:
        logger.exception('WB supplier enrichment dispatch tick failed')

    try:
        from services.supplier_update_hub import process_due_photo_jobs
        process_due_photo_jobs(flask_app, max_jobs=1)
    except Exception:
        logger.exception('WB supplier photo-hub dispatch tick failed')

    try:
        from services.supplier_update_hub import process_due_verify_jobs
        process_due_verify_jobs(flask_app, max_jobs=1)
    except Exception:
        logger.exception('WB supplier verify dispatch tick failed')

    with flask_app.app_context():
        from models import db
        from services.wb_enrichment_reconciliation import (
            process_due_reconciliations,
        )
        try:
            process_due_reconciliations()
        except Exception:
            db.session.rollback()
            logger.exception('WB supplier enrichment reconciliation tick failed')
        finally:
            db.session.remove()


SUPPLIER_UPDATES_NOTIFICATION_TITLE = 'Обновления карточек от поставщика'


def notify_supplier_updates(flask_app):
    """Разово в сутки сообщить продавцу о накопившихся обновлениях.

    Только локальный SQL: сравнение content_revision общей карточки с
    последней скопированной версией продавца. Ничего не изменяет."""
    with flask_app.app_context():
        from datetime import datetime, timedelta
        from models import (
            db, ImportedProduct, Notification, SupplierProduct,
        )
        try:
            rows = db.session.query(
                ImportedProduct.seller_id,
                db.func.count(ImportedProduct.id),
            ).join(
                SupplierProduct,
                SupplierProduct.id == ImportedProduct.supplier_product_id,
            ).filter(
                SupplierProduct.content_revision
                > ImportedProduct.supplier_content_revision,
            ).group_by(ImportedProduct.seller_id).all()
            cutoff = datetime.utcnow() - timedelta(hours=24)
            for seller_id, count in rows:
                if not count:
                    continue
                recent = Notification.query.filter(
                    Notification.seller_id == seller_id,
                    Notification.title == SUPPLIER_UPDATES_NOTIFICATION_TITLE,
                    Notification.created_at >= cutoff,
                ).first()
                if recent:
                    continue
                from seller_platform import create_notification
                create_notification(
                    seller_id=seller_id,
                    category='info',
                    title=SUPPLIER_UPDATES_NOTIFICATION_TITLE,
                    message=(
                        f'У {count} ваших карточек обновились общие данные '
                        'поставщика (категории, характеристики, цены или фото). '
                        'Откройте «Мои товары» → «Обновления поставщика», '
                        'выберите карточки и нажмите «Обновить из каталога».'
                    ),
                    link='/my-products?updates=1',
                )
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception('Supplier updates notification tick failed')
        finally:
            db.session.remove()


def check_and_sync_all_sellers(flask_app):
    """
    Проверить настройки синхронизации для всех продавцов и запустить синхронизацию если нужно

    Args:
        flask_app: Экземпляр Flask приложения
    """
    from models import Seller, ProductSyncSettings
    from seller_platform import _perform_product_sync_task
    import threading

    with flask_app.app_context():
        try:
            # Получаем всех продавцов с включенной автосинхронизацией
            sellers = Seller.query.join(ProductSyncSettings).filter(
                ProductSyncSettings.is_enabled == True
            ).all()

            logger.info(f"📋 Checking sync settings for {len(sellers)} sellers with auto-sync enabled")

            for seller in sellers:
                settings = seller.product_sync_settings

                if not settings:
                    continue

                # Проверяем нужно ли синхронизировать
                should_sync = False

                if settings.next_sync_at is None:
                    # Первая синхронизация - запускаем сразу
                    should_sync = True
                    logger.info(f"🆕 First sync for seller {seller.id}")
                elif datetime.utcnow() >= settings.next_sync_at:
                    # Пришло время следующей синхронизации
                    should_sync = True
                    logger.info(f"⏰ Time for scheduled sync for seller {seller.id}")

                if should_sync and seller.api_sync_status != 'syncing':
                    # Запускаем синхронизацию в фоновом потоке
                    logger.info(f"🚀 Starting background sync for seller {seller.id} ({seller.company_name})")

                    # Обновляем next_sync_at
                    settings.next_sync_at = datetime.utcnow() + timedelta(minutes=settings.sync_interval_minutes)
                    from models import db
                    db.session.commit()

                    # Запускаем синхронизацию
                    thread = threading.Thread(
                        target=_perform_product_sync_task,
                        args=(seller.id, flask_app),
                        daemon=True,
                        name=f"sync-seller-{seller.id}"
                    )
                    thread.start()
                elif seller.api_sync_status == 'syncing':
                    logger.debug(f"⏳ Seller {seller.id} sync already in progress")

        except Exception as e:
            logger.exception(f"❌ Error in check_and_sync_all_sellers: {str(e)}")


def check_and_monitor_prices_all_sellers(flask_app):
    """
    Проверить настройки мониторинга цен для всех продавцов и запустить мониторинг если нужно

    Args:
        flask_app: Экземпляр Flask приложения
    """
    from models import Seller, PriceMonitorSettings
    from seller_platform import perform_price_monitoring_sync
    import threading

    with flask_app.app_context():
        try:
            # Получаем всех продавцов с включенным мониторингом цен
            sellers = Seller.query.join(PriceMonitorSettings).filter(
                PriceMonitorSettings.is_enabled == True
            ).all()

            logger.info(f"📋 Checking price monitoring settings for {len(sellers)} sellers with monitoring enabled")

            for seller in sellers:
                settings = seller.price_monitor_settings

                if not settings:
                    continue

                # Проверяем нужно ли запускать мониторинг
                should_monitor = False

                if settings.last_sync_at is None:
                    # Первый запуск мониторинга
                    should_monitor = True
                    logger.info(f"🆕 First price monitoring for seller {seller.id}")
                else:
                    # Проверяем прошло ли достаточно времени с последней синхронизации
                    time_since_last_sync = datetime.utcnow() - settings.last_sync_at
                    interval_minutes = settings.sync_interval_minutes

                    if time_since_last_sync >= timedelta(minutes=interval_minutes):
                        should_monitor = True
                        logger.info(f"⏰ Time for scheduled price monitoring for seller {seller.id}")

                # Проверяем что мониторинг не запущен в данный момент
                if should_monitor and settings.last_sync_status != 'running':
                    # Запускаем мониторинг в фоновом потоке
                    logger.info(f"🚀 Starting price monitoring for seller {seller.id} ({seller.company_name})")

                    # Запускаем мониторинг
                    thread = threading.Thread(
                        target=_perform_price_monitoring_task,
                        args=(seller.id, flask_app),
                        daemon=True,
                        name=f"price-monitor-seller-{seller.id}"
                    )
                    thread.start()
                elif settings.last_sync_status == 'running':
                    logger.debug(f"⏳ Price monitoring for seller {seller.id} already in progress")

        except Exception as e:
            logger.exception(f"❌ Error in check_and_monitor_prices_all_sellers: {str(e)}")


def _perform_price_monitoring_task(seller_id, flask_app):
    """
    Выполнить мониторинг цен в фоновом потоке

    Args:
        seller_id: ID продавца
        flask_app: Экземпляр Flask приложения
    """
    from models import Seller, PriceMonitorSettings, db
    from seller_platform import perform_price_monitoring_sync

    with flask_app.app_context():
        try:
            logger.info(f"🔍 Price monitoring task started for seller_id={seller_id}")

            # Получаем продавца
            seller = Seller.query.get(seller_id)
            if not seller:
                logger.error(f"Seller {seller_id} not found for price monitoring")
                return

            # Получаем настройки
            settings = seller.price_monitor_settings
            if not settings:
                logger.error(f"Price monitor settings not found for seller {seller_id}")
                return

            # Выполняем мониторинг
            result = perform_price_monitoring_sync(seller, settings)

            logger.info(f"✅ Price monitoring completed for seller {seller_id}: {result}")

        except Exception as e:
            logger.exception(f"❌ Price monitoring failed for seller {seller_id}: {str(e)}")


def sync_card_ratings_for_seller(flask_app, seller_id):
    """Тянет WB productRating/feedbackRating для одного продавца и
    пересчитывает Quality Score.
    Лимит sales-funnel: 3 req/min → пауза 20с между батчами по 1000 nmId."""
    import time
    from datetime import timedelta
    from models import Seller, Product, APILog, db
    from services.wb_api_client import WildberriesAPIClient
    from services.card_quality_scorer import (
        build_seller_scoring_context, recompute_and_persist)
    from services.subject_charcs_cache import refresh_subject_charcs

    with flask_app.app_context():
        try:
            seller = Seller.query.get(seller_id)
            if not seller or not seller.has_valid_api_key():
                logger.info(f"sync_card_ratings_for_seller: seller {seller_id} not found or no valid API key")
                return

            products = Product.query.filter_by(seller_id=seller.id, is_active=True)\
                .filter(Product.nm_id.isnot(None)).all()
            if not products:
                logger.info(f"sync_card_ratings_for_seller: no active products with nm_id for seller {seller_id}")
                return

            by_nm = {p.nm_id: p for p in products}
            nm_ids = list(by_nm.keys())

            period_end = datetime.utcnow().date()
            period_start = period_end - timedelta(days=30)
            ps, pe = period_start.isoformat(), period_end.isoformat()

            client = WildberriesAPIClient(
                api_key=seller.wb_api_key,
                db_logger_callback=lambda **kwargs: APILog.log_request(**kwargs)
            )
            now = datetime.utcnow()
            try:
                for i in range(0, len(nm_ids), 1000):
                    batch = nm_ids[i:i + 1000]
                    resp = client.get_sales_funnel_products(
                        period_start=ps, period_end=pe,
                        nm_ids=batch, limit=1000,
                        log_to_db=True, seller_id=seller.id
                    )
                    metrics = parse_sales_funnel_metrics(resp)
                    for nm_id, m in metrics.items():
                        p = by_nm.get(nm_id)
                        if not p:
                            continue
                        if m['product_rating'] is not None:
                            p.nm_rating = m['product_rating']
                        if m['feedback_rating'] is not None:
                            p.wb_feedback_rating = m['feedback_rating']
                        p.nm_rating_checked_at = now
                        if m['views'] is not None:
                            p.wb_views_30d = int(m['views'])
                        if m['orders'] is not None:
                            p.wb_orders_30d = int(m['orders'])
                        if m['cart_conv'] is not None:
                            p.wb_cart_conv = float(m['cart_conv'])
                        if m['order_conv'] is not None:
                            p.wb_order_conv = float(m['order_conv'])
                        if m['buyout_rate'] is not None:
                            p.wb_buyout_rate = float(m['buyout_rate'])
                        p.funnel_checked_at = now
                    if i + 1000 < len(nm_ids):
                        time.sleep(20)  # лимит 3 req/min

                # Ленивое обновление кэша конфигов категорий (TTL 7 дней)
                try:
                    refresh_subject_charcs(
                        client, {p.subject_id for p in products if p.subject_id})
                except Exception as e:
                    logger.warning(f"charcs cache refresh failed: {e}")

                # Пересчёт Quality Score v2 + причины + impact (дёшево, один контекст)
                context = build_seller_scoring_context(seller.id)
                for p in products:
                    recompute_and_persist(p, capture_history=True, context=context)
                db.session.commit()
                logger.info(f"✅ Card ratings synced for seller {seller.id}: {len(products)} products")
            except Exception as e:
                db.session.rollback()
                logger.error(f"❌ Card rating sync failed for seller {seller.id}: {e}")
        except Exception as e:
            logger.exception(f"❌ Error in sync_card_ratings_for_seller for seller {seller_id}: {e}")


def sync_card_ratings_all_sellers(flask_app):
    """Тянет WB productRating/feedbackRating для активного каталога всех продавцов и
    пересчитывает Quality Score. Запускается планировщиком (раз в несколько часов)."""
    from models import Seller, db

    with flask_app.app_context():
        try:
            sellers = Seller.query.filter(
                Seller._wb_api_key_encrypted.isnot(None),
                Seller._wb_api_key_encrypted != ''
            ).all()
            seller_ids = [s.id for s in sellers]
        except Exception as e:
            logger.exception(f"❌ Error in sync_card_ratings_all_sellers: {e}")
            return

    for seller_id in seller_ids:
        sync_card_ratings_for_seller(flask_app, seller_id)


def sync_blocked_cards_all_sellers(flask_app):
    """
    Синхронизировать заблокированные и скрытые карточки для всех продавцов с валидным API ключом.
    Запускается каждые 10 минут планировщиком.
    """
    from models import Seller, BlockedCard, ShadowedCard, BlockedCardsSyncSettings, APILog, db
    from services.wb_api_client import WildberriesAPIClient

    with flask_app.app_context():
        try:
            sellers = Seller.query.filter(
                Seller._wb_api_key_encrypted.isnot(None),
                Seller._wb_api_key_encrypted != ''
            ).all()

            logger.info(f"📋 Syncing blocked cards for {len(sellers)} sellers")

            for seller in sellers:
                if not seller.has_valid_api_key():
                    continue

                # Получаем или создаём настройки синка
                sync_settings = BlockedCardsSyncSettings.query.filter_by(
                    seller_id=seller.id
                ).first()
                if not sync_settings:
                    sync_settings = BlockedCardsSyncSettings(seller_id=seller.id)
                    db.session.add(sync_settings)
                    db.session.flush()

                if sync_settings.last_sync_status == 'running':
                    logger.debug(f"⏳ Blocked cards sync already running for seller {seller.id}")
                    continue

                sync_settings.last_sync_status = 'running'
                db.session.commit()

                try:
                    client = WildberriesAPIClient(
                        api_key=seller.wb_api_key,
                        db_logger_callback=lambda **kwargs: APILog.log_request(**kwargs)
                    )

                    # --- Заблокированные ---
                    blocked_api = client.get_blocked_cards(
                        sort='nmId', order='asc',
                        log_to_db=True, seller_id=seller.id
                    )
                    _upsert_blocked_cards(seller.id, blocked_api, db)

                    # --- Скрытые ---
                    shadowed_api = client.get_shadowed_cards(
                        sort='nmId', order='asc',
                        log_to_db=True, seller_id=seller.id
                    )
                    _upsert_shadowed_cards(seller.id, shadowed_api, db)

                    sync_settings.last_sync_at = datetime.utcnow()
                    sync_settings.last_sync_status = 'success'
                    sync_settings.last_sync_error = None
                    sync_settings.blocked_count = len(blocked_api)
                    sync_settings.shadowed_count = len(shadowed_api)
                    db.session.commit()

                    logger.info(
                        f"✅ Blocked cards synced for seller {seller.id}: "
                        f"{len(blocked_api)} blocked, {len(shadowed_api)} shadowed"
                    )

                except Exception as e:
                    logger.error(f"❌ Blocked cards sync failed for seller {seller.id}: {e}")
                    sync_settings.last_sync_status = 'error'
                    sync_settings.last_sync_error = str(e)[:500]
                    db.session.commit()

        except Exception as e:
            logger.exception(f"❌ Error in sync_blocked_cards_all_sellers: {e}")


def _upsert_blocked_cards(seller_id, api_data, db):
    """Обновить таблицу blocked_cards по данным из API"""
    from models import BlockedCard

    now = datetime.utcnow()
    api_nm_ids = set()

    for item in api_data:
        nm_id = item.get('nmId')
        if not nm_id:
            continue
        api_nm_ids.add(nm_id)

        existing = BlockedCard.query.filter_by(
            seller_id=seller_id, nm_id=nm_id
        ).first()

        if existing:
            existing.vendor_code = item.get('vendorCode', existing.vendor_code)
            existing.title = item.get('title', existing.title)
            existing.brand = item.get('brand', existing.brand)
            existing.reason = item.get('reason', existing.reason)
            existing.last_seen_at = now
            existing.is_active = True
        else:
            card = BlockedCard(
                seller_id=seller_id,
                nm_id=nm_id,
                vendor_code=item.get('vendorCode'),
                title=item.get('title'),
                brand=item.get('brand'),
                reason=item.get('reason'),
                first_seen_at=now,
                last_seen_at=now,
                is_active=True,
            )
            db.session.add(card)

    # Помечаем карточки, которых нет в API, как неактивные (разблокированы)
    BlockedCard.query.filter(
        BlockedCard.seller_id == seller_id,
        BlockedCard.is_active == True,
        ~BlockedCard.nm_id.in_(api_nm_ids) if api_nm_ids else True
    ).update({'is_active': False, 'last_seen_at': now}, synchronize_session='fetch')

    db.session.commit()


def _upsert_shadowed_cards(seller_id, api_data, db):
    """Обновить таблицу shadowed_cards по данным из API"""
    from models import ShadowedCard, Product

    now = datetime.utcnow()
    api_nm_ids = set()

    for item in api_data:
        nm_id = item.get('nmId')
        if not nm_id:
            continue
        api_nm_ids.add(nm_id)

        existing = ShadowedCard.query.filter_by(
            seller_id=seller_id, nm_id=nm_id
        ).first()

        if existing:
            existing.vendor_code = item.get('vendorCode', existing.vendor_code)
            existing.title = item.get('title', existing.title)
            existing.brand = item.get('brand', existing.brand)
            existing.nm_rating = item.get('nmRating', existing.nm_rating)
            existing.last_seen_at = now
            existing.is_active = True
        else:
            card = ShadowedCard(
                seller_id=seller_id,
                nm_id=nm_id,
                vendor_code=item.get('vendorCode'),
                title=item.get('title'),
                brand=item.get('brand'),
                nm_rating=item.get('nmRating'),
                first_seen_at=now,
                last_seen_at=now,
                is_active=True,
            )
            db.session.add(card)

    # Помечаем карточки, которых нет в API, как неактивные (разблокированы)
    ShadowedCard.query.filter(
        ShadowedCard.seller_id == seller_id,
        ShadowedCard.is_active == True,
        ~ShadowedCard.nm_id.in_(api_nm_ids) if api_nm_ids else True
    ).update({'is_active': False, 'last_seen_at': now}, synchronize_session='fetch')

    # Обновляем nm_rating в таблице products (cross-update)
    for item in api_data:
        nm_id = item.get('nmId')
        nm_rating = item.get('nmRating')
        if nm_id and nm_rating is not None:
            Product.query.filter_by(
                seller_id=seller_id, nm_id=nm_id
            ).update({'nm_rating': nm_rating}, synchronize_session=False)

    db.session.commit()


def initial_analytics_sync_if_empty(flask_app):
    """Запускает первоначальную синхронизацию аналитики, если таблицы пустые."""
    with flask_app.app_context():
        try:
            from models import Seller, WBSale
            # Проверяем есть ли вообще данные
            has_data = WBSale.query.first() is not None
            if has_data:
                logger.info("📊 Analytics tables already have data, skipping initial sync")
                return

            sellers = Seller.query.all()
            has_api_key = any(s.wb_api_key for s in sellers)
            if not has_api_key:
                logger.info("📊 No sellers with API keys, skipping initial analytics sync")
                return

            logger.info("📊 Analytics tables are empty — running initial sync...")
            from services.wb_data_sync import sync_all_sellers
            sync_all_sellers()
            logger.info("✅ Initial analytics sync completed")
        except Exception as e:
            logger.exception(f"❌ Error in initial_analytics_sync_if_empty: {e}")


def sync_wb_analytics_all_sellers(flask_app):
    """Синхронизация аналитических данных WB (sales, orders, feedbacks, realization)
    для всех продавцов с валидным API ключом."""
    with flask_app.app_context():
        try:
            from services.wb_data_sync import sync_all_sellers
            logger.info("📊 Starting WB analytics sync for all sellers...")
            sync_all_sellers()
            logger.info("✅ WB analytics sync finished.")
        except Exception as e:
            logger.exception(f"❌ Error in sync_wb_analytics_all_sellers: {e}")


def sync_marketplaces(flask_app):
    """
    Периодическая синхронизация справочников и категорий всех маркетплейсов.
    """
    from models import Marketplace, MarketplaceReferenceAccount
    from services.marketplace_service import MarketplaceService
    from services.ozon_reference_service import OzonReferenceService
    with flask_app.app_context():
        try:
            logger.info("🌍 Starting global marketplace sync...")
            marketplaces = Marketplace.query.filter_by(
                is_active=True, code='wb',
            ).all()
            for mp in marketplaces:
                client = MarketplaceService.get_wb_client(mp.id)
                if not client:
                    logger.info(f"Reference sync skipped for {mp.code}: no API key")
                    continue
                try:
                    logger.info(f"Syncing categories for {mp.name} ({mp.code})")
                    categories_result = MarketplaceService.sync_categories(
                        mp.id, client=client,
                    )
                    if not categories_result.get('success'):
                        logger.error(
                            f"Failed to sync categories for {mp.code}: "
                            f"{categories_result.get('error')}"
                        )

                    logger.info(f"Syncing directories for {mp.name} ({mp.code})")
                    directories_result = MarketplaceService.sync_directories(
                        mp.id, client=client,
                    )
                    if not directories_result.get('success'):
                        logger.error(
                            f"Failed to sync directories for {mp.code}: "
                            f"{directories_result.get('error')}"
                        )

                except Exception:
                    logger.exception(
                        "Marketplace reference sync failed for %s", mp.code,
                    )
                finally:
                    close = getattr(client, 'close', None)
                    if callable(close):
                        close()

            if flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
                ozon_marketplaces = Marketplace.query.filter_by(
                    is_active=True,
                    code='ozon',
                ).all()
                for marketplace in ozon_marketplaces:
                    reference = MarketplaceReferenceAccount.query.filter_by(
                        marketplace_id=marketplace.id,
                        connection_status='connected',
                    ).first()
                    if reference is None or not reference.has_credentials:
                        logger.info(
                            'Ozon taxonomy refresh skipped: reference account unavailable'
                        )
                        continue
                    try:
                        result = OzonReferenceService.sync_tree(marketplace.id)
                        if not result.get('success') and not result.get('skipped'):
                            logger.warning(
                                'Ozon taxonomy refresh failed for marketplace_id=%s',
                                marketplace.id,
                            )
                    except Exception:
                        logger.exception(
                            'Ozon taxonomy refresh crashed for marketplace_id=%s',
                            marketplace.id,
                        )

            logger.info("✅ Global marketplace sync finished.")
        except Exception as e:
            logger.exception(f"❌ Error in sync_marketplaces: {e}")


def maintain_marketplace_projection(flask_app, *, seller_limit=3, batch_size=200):
    """Advance bounded local WB backfill/parity runs without provider calls."""
    with flask_app.app_context():
        if not flask_app.config.get('MARKETPLACE_WB_PROJECTION_ENABLED', True):
            return {
                'selected_sellers': 0,
                'backfill_batches': 0,
                'parity_batches': 0,
                'busy': 0,
                'failed': 0,
            }
        try:
            from services.marketplace_rollout import MarketplaceRolloutService
            result = MarketplaceRolloutService.maintenance_tick(
                seller_limit=seller_limit,
                batch_size=batch_size,
                dual_read_enabled=bool(flask_app.config.get(
                    'MARKETPLACE_WB_DUAL_READ_ENABLED',
                    True,
                )),
            )
        except Exception:
            logger.exception('Marketplace projection maintenance failed')
            return {
                'selected_sellers': 0,
                'backfill_batches': 0,
                'parity_batches': 0,
                'busy': 0,
                'failed': 1,
            }
        if result['backfill_batches'] or result['parity_batches'] or result['failed']:
            logger.info(
                'Marketplace projection maintenance: sellers=%s backfill=%s parity=%s busy=%s failed=%s',
                result['selected_sellers'],
                result['backfill_batches'],
                result['parity_batches'],
                result['busy'],
                result['failed'],
            )
        return result


def maintain_marketplace_source_links(
    flask_app,
    *,
    account_limit=3,
    batch_size=200,
):
    """Advance durable local Ozon/WB source-link reconciliation."""
    with flask_app.app_context():
        if not flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            return {
                'created': 0,
                'processed_account_batches': 0,
                'linked': 0,
                'materialized': 0,
                'ambiguous': 0,
                'busy': 0,
                'failed': 0,
            }
        try:
            from services.marketplace_source_link_reconciliation import (
                MarketplaceSourceLinkReconciliation,
            )
            result = MarketplaceSourceLinkReconciliation.maintenance_tick(
                account_limit=account_limit,
                batch_size=batch_size,
            )
        except Exception:
            logger.exception(
                'Marketplace source identity reconciliation failed'
            )
            return {
                'created': 0,
                'processed_account_batches': 0,
                'linked': 0,
                'materialized': 0,
                'ambiguous': 0,
                'busy': 0,
                'failed': 1,
            }
        if (
            result['created']
            or result['linked']
            or result['materialized']
            or result['ambiguous']
            or result['failed']
        ):
            logger.info(
                'Marketplace source links: created=%s batches=%s linked=%s '
                'materialized=%s ambiguous=%s busy=%s failed=%s',
                result['created'],
                result['processed_account_batches'],
                result['linked'],
                result['materialized'],
                result['ambiguous'],
                result['busy'],
                result['failed'],
            )
        return result


def sync_marketplace_characteristics(flask_app, limit: int = 50):
    """Refresh a bounded stale-schema batch for every active marketplace."""
    from models import Marketplace
    from services.marketplace_service import MarketplaceService
    from services.ozon_reference_service import OzonReferenceService

    with flask_app.app_context():
        for marketplace in Marketplace.query.filter_by(
            is_active=True, code='wb',
        ).all():
            client = MarketplaceService.get_wb_client(marketplace.id)
            if not client:
                continue
            try:
                result = MarketplaceService.sync_stale_characteristics(
                    marketplace.id,
                    limit=limit,
                    client=client,
                )
                if result.get('failed'):
                    logger.warning(
                        "Stale schema refresh for %s had %s failures",
                        marketplace.code,
                        result['failed'],
                    )
            except Exception:
                logger.exception(
                    "Stale characteristic refresh failed for %s", marketplace.code,
                )
            finally:
                close = getattr(client, 'close', None)
                if callable(close):
                    close()

        if flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            for marketplace in Marketplace.query.filter_by(
                is_active=True,
                code='ozon',
            ).all():
                try:
                    result = OzonReferenceService.sync_stale_enabled_types(
                        marketplace.id,
                        limit=limit,
                    )
                    if result.get('failed') or result.get('dictionaries_failed'):
                        logger.warning(
                            'Stale Ozon schema refresh had failures for marketplace_id=%s',
                            marketplace.id,
                        )
                except Exception:
                    logger.exception(
                        'Stale Ozon schema refresh crashed for marketplace_id=%s',
                        marketplace.id,
                    )


def sync_ozon_demanded_references(
    flask_app,
    *,
    limit: int = 3,
    dictionary_limit: int = 6,
):
    """Refresh the exact Ozon types currently demanded by seller state."""
    from models import Marketplace
    from services.ozon_reference_service import OzonReferenceService

    result = {
        'marketplaces': 0,
        'selected': 0,
        'synced': 0,
        'failed': 0,
        'dictionaries_synced': 0,
        'dictionaries_failed': 0,
    }
    with flask_app.app_context():
        if not flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            return result
        marketplaces = Marketplace.query.filter_by(
            is_active=True,
            code='ozon',
        ).all()
        result['marketplaces'] = len(marketplaces)
        for marketplace in marketplaces:
            try:
                refreshed = OzonReferenceService.sync_demanded_types(
                    marketplace.id,
                    limit=limit,
                    dictionary_limit=dictionary_limit,
                )
            except Exception:
                logger.exception(
                    'Demanded Ozon reference refresh crashed marketplace_id=%s',
                    marketplace.id,
                )
                result['failed'] += 1
                continue
            for key in (
                'selected',
                'synced',
                'failed',
                'dictionaries_synced',
                'dictionaries_failed',
            ):
                result[key] += int(refreshed.get(key, 0) or 0)
            if (
                refreshed.get('failed')
                or refreshed.get('dictionaries_failed')
                or refreshed.get('tree_waiting')
            ):
                logger.warning(
                    'Demanded Ozon references waiting marketplace_id=%s '
                    'selected=%s failed=%s dictionaries_failed=%s tree_waiting=%s',
                    marketplace.id,
                    refreshed.get('selected', 0),
                    refreshed.get('failed', 0),
                    refreshed.get('dictionaries_failed', 0),
                    bool(refreshed.get('tree_waiting')),
                )
        return result


def poll_ozon_marketplace_operations(flask_app, limit: int = 20):
    """Advance a bounded due batch without ever logging payloads or secrets."""
    from services.marketplace_publications import MarketplacePublicationService
    from services.ozon_bulk_upload import OzonBulkUploadService

    with flask_app.app_context():
        allow_submission = bool(
            flask_app.config.get('MARKETPLACE_OZON_ENABLED', False)
            and flask_app.config.get(
                'MARKETPLACE_OZON_PUBLICATION_ENABLED',
                False,
            )
        )
        try:
            result = MarketplacePublicationService.poll_due_operations(
                limit=limit,
                allow_submission=allow_submission,
            )
        except Exception:
            logger.exception('Durable Ozon operation poll failed')
            result = {
                'selected': 0,
                'processed': 0,
                'busy': 0,
                'failed': 1,
            }
        if result['selected'] or result['failed']:
            logger.info(
                'Durable Ozon operation poll: selected=%s processed=%s busy=%s failed=%s queued_submission=%s',
                result['selected'],
                result['processed'],
                result['busy'],
                result['failed'],
                allow_submission,
            )
        # Seller-facing bulk runs only reflect already durable operation rows;
        # this hook performs no provider call and remains safe when writes are
        # disabled after a rollout.
        try:
            upload_runs = OzonBulkUploadService.reconcile_active_runs(limit=20)
        except Exception:
            logger.exception('Ozon bulk upload run reconciliation failed')
            upload_runs = {'selected': 0, 'reconciled': 0, 'failed': 1}
        result['upload_runs'] = upload_runs
        return result


def prepare_ozon_uploads(flask_app):
    """Short local-only work in the existing singleton scheduler pool."""
    from models import db
    from services.ozon_bulk_upload import OzonBulkUploadService

    with flask_app.app_context():
        try:
            return OzonBulkUploadService.run_due_preparation(
                run_limit=20, item_limit=40, seconds_budget=8,
            )
        except Exception:
            db.session.rollback()
            logger.exception('Ozon local upload preparation failed')
            return {'selected': 0, 'processed_items': 0, 'failed': 1}
        finally:
            db.session.remove()


def reconcile_ozon_auto_publish_runs(flask_app, limit: int = 50):
    """Update async auto-publish items from durable Ozon operation state."""
    from services.marketplace_auto_publish import OzonAutoPublishService

    with flask_app.app_context():
        try:
            result = OzonAutoPublishService.reconcile_waiting_runs(limit=limit)
        except Exception as exc:
            logger.error(
                'Ozon auto-publish run reconciliation failed (%s)',
                type(exc).__name__,
            )
            return {
                'selected': 0,
                'processed': 0,
                'busy': 0,
                'failed': 1,
            }
        if result['selected'] or result['failed']:
            logger.info(
                'Ozon auto-publish reconcile: selected=%s processed=%s busy=%s failed=%s',
                result['selected'],
                result['processed'],
                result['busy'],
                result['failed'],
            )
        return result


def poll_ozon_commercial_operations(flask_app, limit: int = 20):
    """Reconcile commercial writes; only never-attempted rows honor write flag."""
    from services.marketplace_commercial import MarketplaceCommercialService

    with flask_app.app_context():
        allow_submission = bool(
            flask_app.config.get('MARKETPLACE_OZON_ENABLED', False)
            and flask_app.config.get(
                'MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED',
                False,
            )
        )
        try:
            result = MarketplaceCommercialService.poll_due_operations(
                limit=limit,
                allow_submission=allow_submission,
            )
        except Exception:
            logger.exception('Durable Ozon commercial operation poll failed')
            return {
                'selected': 0,
                'processed': 0,
                'busy': 0,
                'failed': 1,
            }
        if result['selected'] or result['failed']:
            logger.info(
                'Durable Ozon commercial poll: selected=%s processed=%s busy=%s failed=%s queued_submission=%s',
                result['selected'],
                result['processed'],
                result['busy'],
                result['failed'],
                allow_submission,
            )
        return result

def sync_ozon_requested_reads(flask_app, limit=1):
    """Advance one due period/warehouse/FBS read in the existing shared slot."""
    with flask_app.app_context():
        if not flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            return {'selected': 0, 'completed': 0, 'running': 0, 'failed': 0, 'unavailable': 0}
        from services.ozon_read_scheduler import run_requested_reads
        return run_requested_reads(limit=limit)


def sync_ozon_analytics_accounts(flask_app, limit=3):
    """Resume analytics and rotate bounded local quality assessments."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        return {
            'selected': 0,
            'completed': 0,
            'running': 0,
            'failed': 1,
            'quality_selected': 0,
            'quality_processed': 0,
            'quality_failed': 0,
        }
    with flask_app.app_context():
        if not flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            return {
                'selected': 0,
                'completed': 0,
                'running': 0,
                'failed': 0,
                'quality_selected': 0,
                'quality_processed': 0,
                'quality_failed': 0,
            }
        from sqlalchemy import case, func
        from models import (
            Marketplace,
            MarketplaceListing,
            MarketplaceQualityAssessment,
            SellerMarketplaceAccount,
            db,
        )
        from services.ozon_read_scheduler import run_due_reads
        from services.marketplace_quality import MarketplaceQualityService

        current_time = datetime.utcnow()
        result = run_due_reads(domain='analytics', limit=limit, now=current_time)
        result.update(quality_selected=0, quality_processed=0, quality_failed=0)

        last_quality_at = db.session.query(
            func.min(MarketplaceQualityAssessment.evaluated_at)
        ).join(
            MarketplaceListing,
            MarketplaceListing.id == MarketplaceQualityAssessment.listing_id,
        ).filter(
            MarketplaceQualityAssessment.seller_id
            == SellerMarketplaceAccount.seller_id,
            MarketplaceQualityAssessment.marketplace_id
            == SellerMarketplaceAccount.marketplace_id,
            MarketplaceQualityAssessment.account_id
            == SellerMarketplaceAccount.id,
            MarketplaceListing.seller_id
            == SellerMarketplaceAccount.seller_id,
            MarketplaceListing.marketplace_id
            == SellerMarketplaceAccount.marketplace_id,
            MarketplaceListing.account_id == SellerMarketplaceAccount.id,
            MarketplaceListing.is_available.is_(True),
            MarketplaceListing.is_archived.is_(False),
        ).correlate(SellerMarketplaceAccount).scalar_subquery()
        active_listing_exists = db.session.query(
            MarketplaceListing.id
        ).filter(
            MarketplaceListing.seller_id
            == SellerMarketplaceAccount.seller_id,
            MarketplaceListing.marketplace_id
            == SellerMarketplaceAccount.marketplace_id,
            MarketplaceListing.account_id == SellerMarketplaceAccount.id,
            MarketplaceListing.is_available.is_(True),
            MarketplaceListing.is_archived.is_(False),
        ).exists()
        quality_accounts = SellerMarketplaceAccount.query.join(
            Marketplace
        ).filter(
            Marketplace.code == 'ozon',
            Marketplace.is_active.is_(True),
            SellerMarketplaceAccount.is_active.is_(True),
            SellerMarketplaceAccount.connection_status == 'connected',
            active_listing_exists,
        ).order_by(
            case((last_quality_at.is_(None), 0), else_=1).asc(),
            last_quality_at.asc(),
            SellerMarketplaceAccount.is_default.desc(),
            SellerMarketplaceAccount.id.asc(),
        ).limit(limit).all()
        result['quality_selected'] = len(quality_accounts)
        for account in quality_accounts:
            try:
                quality_result = (
                    MarketplaceQualityService.recompute_next_account_batch(
                        seller_id=account.seller_id,
                        account_id=account.id,
                        limit=MarketplaceQualityService.MAX_BATCH,
                        now=current_time,
                    )
                )
                result['quality_processed'] += int(
                    quality_result.get('processed') or 0
                )
            except Exception as exc:
                db.session.rollback()
                result['quality_failed'] += 1
                logger.error(
                    'Ozon quality scheduler failed for account=%s: %s',
                    account.id,
                    type(exc).__name__,
                )
        if (
            result['selected']
            or result['failed']
            or result['quality_selected']
            or result['quality_failed']
        ):
            logger.info(
                'Ozon analytics scheduler: selected=%s completed=%s running=%s '
                'failed=%s quality_selected=%s quality_processed=%s '
                'quality_failed=%s',
                result['selected'],
                result['completed'],
                result['running'],
                result['failed'],
                result['quality_selected'],
                result['quality_processed'],
                result['quality_failed'],
            )
        return result


def sync_ozon_fulfillment_accounts(flask_app, limit=2):
    """Resume/start a bounded set of read-only Ozon fulfillment snapshots."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        return {'selected': 0, 'completed': 0, 'running': 0, 'failed': 1}
    with flask_app.app_context():
        if not flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            return {'selected': 0, 'completed': 0, 'running': 0, 'failed': 0}
        from services.ozon_read_scheduler import run_due_reads

        result = run_due_reads(domain='fulfillment', limit=limit)
        if result['selected'] or result['failed']:
            logger.info(
                'Ozon fulfillment scheduler: selected=%s completed=%s running=%s failed=%s',
                result['selected'],
                result['completed'],
                result['running'],
                result['failed'],
            )
        return result


def sync_ozon_finance_accounts(flask_app, limit=2):
    """Resume/start a bounded set of read-only Ozon finance snapshots."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        return {'selected': 0, 'completed': 0, 'running': 0, 'failed': 1}
    with flask_app.app_context():
        if not flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            return {'selected': 0, 'completed': 0, 'running': 0, 'failed': 0}
        from services.ozon_read_scheduler import run_due_reads

        result = run_due_reads(domain='finance', limit=limit)
        if result['selected'] or result['failed']:
            logger.info(
                'Ozon finance scheduler: selected=%s completed=%s running=%s failed=%s',
                result['selected'],
                result['completed'],
                result['running'],
                result['failed'],
            )
        return result


def sync_ozon_inbox_accounts(flask_app, limit=2):
    """Resume/start bounded capability-proven Ozon inbox read sweeps."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        return {'selected': 0, 'completed': 0, 'running': 0, 'unavailable': 0, 'failed': 1}
    with flask_app.app_context():
        from models import db
        from services.marketplace_inbox import MarketplaceInboxService

        current_time = datetime.utcnow()
        current_date = current_time.date()
        try:
            pruned = MarketplaceInboxService.prune_expired_items(
                today=current_date,
                limit=500,
            )
            if pruned:
                logger.info('Ozon inbox retention removed %s expired rows', pruned)
        except Exception as exc:
            db.session.rollback()
            logger.error(
                'Ozon inbox retention cleanup failed: %s',
                type(exc).__name__,
            )
        if not flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            return {'selected': 0, 'completed': 0, 'running': 0, 'unavailable': 0, 'failed': 0}
        from services.ozon_read_scheduler import run_due_inbox_reads
        result = run_due_inbox_reads(limit=limit, now=current_time)
        if result['selected']:
            logger.info('Ozon inbox durable reads: %s', result)
        return result


def shutdown_scheduler():
    """Остановить планировщик"""
    global scheduler, _scheduler_lock_retry_thread

    _scheduler_lock_retry_stop.set()

    if scheduler is not None:
        logger.info("🛑 Shutting down product sync scheduler...")
        scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("✅ Product sync scheduler stopped")
    _release_scheduler_process_lock()
    _scheduler_lock_retry_thread = None


def get_scheduler_status():
    """
    Получить статус планировщика

    Returns:
        dict: Информация о планировщике и запланированных задачах
    """
    global scheduler

    if scheduler is None:
        return {
            'running': False,
            'jobs': []
        }

    jobs_info = []
    for job in scheduler.get_jobs():
        jobs_info.append({
            'id': job.id,
            'name': job.name,
            'next_run': job.next_run_time.isoformat() if job.next_run_time else None
        })

    return {
        'running': scheduler.running,
        'jobs': jobs_info
    }


def sync_brands_background(flask_app):
    """Фоновая синхронизация брендов через API маркетплейсов."""
    with flask_app.app_context():
        try:
            from models import Marketplace
            from services.brand_engine import get_brand_engine
            from services.marketplace_service import MarketplaceService

            # Находим WB маркетплейс
            wb = Marketplace.query.filter_by(code='wb', is_active=True).first()
            if not wb:
                logger.info("Brand sync skipped: WB marketplace not found")
                return

            wb_client = MarketplaceService.get_wb_client(wb.id)
            if not wb_client:
                logger.info("Brand sync skipped: marketplace API key is not configured")
                return

            with wb_client:
                engine = get_brand_engine(flask_app)
                stats = engine.sync_marketplace_brands(wb.id, wb_client)
                logger.info(f"Brand sync completed: {stats}")

        except Exception as e:
            logger.error(f"Brand sync background task failed: {e}")


def _brand_sync_needs_resume(marketplace) -> bool:
    """Return true only for a durable, incomplete category sweep."""
    return bool(
        marketplace
        and marketplace.is_active
        and marketplace.brands_sync_status == 'partial'
        and marketplace.brands_sync_checkpoint
    )


def resume_brand_sync_if_needed(flask_app):
    """Resume a partial brand sweep without polling WB when none is pending."""
    with flask_app.app_context():
        from models import Marketplace
        wb = Marketplace.query.filter_by(code='wb', is_active=True).first()
        should_resume = _brand_sync_needs_resume(wb)
    if should_resume:
        sync_brands_background(flask_app)


def auto_resolve_pending_brands(flask_app):
    """Фоновый авто-резолв pending брендов."""
    with flask_app.app_context():
        try:
            from models import Marketplace
            from services.brand_engine import get_brand_engine
            from services.marketplace_service import MarketplaceService

            # Находим WB маркетплейс
            wb = Marketplace.query.filter_by(code='wb', is_active=True).first()
            if not wb:
                logger.info("Brand auto-resolve skipped: WB marketplace not found")
                return

            wb_client = MarketplaceService.get_wb_client(wb.id)
            if not wb_client:
                logger.info("Brand auto-resolve skipped: marketplace API key is not configured")
                return

            with wb_client:
                engine = get_brand_engine(flask_app)
                stats = engine.auto_resolve_pending(wb_client, marketplace_id=wb.id)
                logger.info(f"Brand auto-resolve completed: {stats}")

        except Exception as e:
            logger.error(f"Brand auto-resolve background task failed: {e}")


def _run_competitor_monitor_tick(flask_app):
    """Bounded tick мониторинга конкурентов: до 2 due-продавцов за минуту."""
    try:
        from services.competitor_monitor import run_competitor_monitor_tick
        run_competitor_monitor_tick(flask_app)
    except Exception as e:
        logger.error(f"Ошибка competitor monitor tick: {e}")


def _run_competitor_matching_tick(flask_app):
    """Изолированный wrapper общей очереди сопоставлений конкурентов."""
    try:
        from services.competitor_matching import run_competitor_matching_tick
        run_competitor_matching_tick(flask_app)
    except Exception as e:
        logger.error(f"Ошибка competitor matching tick: {e}")


def _compact_competitor_snapshots(flask_app):
    """Чанковая компакция снимков конкурентов (без длинного write-lock)."""
    try:
        from services.competitor_monitor import compact_competitor_snapshots
        compact_competitor_snapshots(flask_app)
    except Exception as e:
        logger.error(f"Ошибка компакции снимков конкурентов: {e}")


def reconcile_submitted_prices(flask_app):
    """Подтвердить или отклонить отправленные цены по факту с площадки.

    Bounded: до трёх продавцов и до трёх запусков на продавца за тик, только
    чтение WB. Повторных отправок цен здесь нет — неудачные позиции остаются
    продавцу для явного решения.
    """
    with flask_app.app_context():
        from models import PriceChangeBatch, Seller, db
        from services.price_reconciliation import PriceReconciliationService
        from services.wb_api_client import WildberriesAPIClient

        seller_ids = [
            row[0] for row in db.session.query(
                PriceChangeBatch.seller_id
            ).filter(
                PriceChangeBatch.status.in_(('submitted', 'applying'))
            ).distinct().limit(3).all()
        ]
        for seller_id in seller_ids:
            seller = Seller.query.get(seller_id)
            if not seller or not seller.has_valid_api_key():
                continue
            batches = PriceReconciliationService.pending_batches(
                seller_id=seller_id, limit=3,
            )
            if not batches:
                continue
            api_client = WildberriesAPIClient(seller.wb_api_key)
            try:
                for batch in batches:
                    try:
                        outcome = PriceReconciliationService.reconcile_batch(
                            batch=batch, api_client=api_client,
                        )
                        logger.info(
                            'Price reconcile batch=%s confirmed=%s rejected=%s waiting=%s',
                            batch.id, outcome.confirmed, outcome.rejected,
                            outcome.still_waiting,
                        )
                    except Exception as exc:  # noqa: BLE001
                        db.session.rollback()
                        logger.error(
                            'Price reconcile failed for batch %s: %s', batch.id, exc
                        )
            finally:
                try:
                    api_client.close()
                except Exception:  # noqa: BLE001
                    pass


def process_ozon_draft_ai_completions(flask_app):
    """A short coordinator tick; model HTTP runs in three bounded futures."""
    with flask_app.app_context():
        from models import db
        try:
            from services.ozon_draft_ai_worker import tick
            return tick(seconds_budget=8,
                allow_new=bool(flask_app.config.get('MARKETPLACE_OZON_ENABLED', False)))
        except Exception as exc:
            db.session.rollback()
            flask_app.logger.error('Ozon AI coordinator deferred type=%s', type(exc).__name__)
            return {'error': 'ai_coordinator_deferred'}
        finally:
            db.session.remove()
