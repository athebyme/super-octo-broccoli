"""
Планировщик автоматической синхронизации товаров
Использует APScheduler для запуска синхронизации по расписанию
"""
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# Глобальный планировщик
scheduler = None


def init_scheduler(flask_app):
    """
    Инициализировать планировщик автоматической синхронизации

    Args:
        flask_app: Экземпляр Flask приложения
    """
    global scheduler

    if scheduler is not None:
        logger.warning("Scheduler already initialized")
        return scheduler

    logger.info("🕐 Initializing product sync scheduler...")

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

    # Задача регулярной фоновой синхронизации общих справочников маркетплейсов (каждые 24 часа)
    scheduler.add_job(
        func=lambda: sync_marketplaces(flask_app),
        trigger=IntervalTrigger(hours=24),
        id='sync_marketplaces_data',
        name='Sync marketplace directories and categories globally',
        replace_existing=True
    )

    # Фоновая синхронизация брендов с WB (каждые 6 часов)
    scheduler.add_job(
        func=lambda: sync_brands_background(flask_app),
        trigger=IntervalTrigger(hours=6),
        id='brand_wb_sync',
        name='Sync brands from WB API',
        replace_existing=True
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

    # Запускаем планировщик
    scheduler.start()

    logger.info("✅ Product sync scheduler started")

    return scheduler


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
    from models import Marketplace, db
    from services.marketplace_service import MarketplaceService
    import logging

    logger = logging.getLogger(__name__)
    with flask_app.app_context():
        try:
            logger.info("🌍 Starting global marketplace sync...")
            marketplaces = Marketplace.query.filter_by(is_active=True).all()
            for mp in marketplaces:
                logger.info(f"Syncing directories for {mp.name} ({mp.code})")
                res = MarketplaceService.sync_directories(mp.id)
                if not res.get('success'):
                    logger.error(f"Failed to sync directories for {mp.code}: {res.get('error')}")

                logger.info(f"Syncing categories for {mp.name} ({mp.code})")
                res2 = MarketplaceService.sync_categories(mp.id)
                if not res2.get('success'):
                    logger.error(f"Failed to sync categories for {mp.code}: {res2.get('error')}")

            logger.info("✅ Global marketplace sync finished.")
        except Exception as e:
            logger.exception(f"❌ Error in sync_marketplaces: {e}")



def shutdown_scheduler():
    """Остановить планировщик"""
    global scheduler

    if scheduler is not None:
        logger.info("🛑 Shutting down product sync scheduler...")
        scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("✅ Product sync scheduler stopped")


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
            from models import Seller, Marketplace
            from services.brand_engine import get_brand_engine

            # Находим WB маркетплейс
            wb = Marketplace.query.filter_by(code='wb').first()
            if not wb:
                logger.info("Brand sync skipped: WB marketplace not found")
                return

            # Находим продавца с WB API ключом
            seller = Seller.query.filter(Seller._wb_api_key_encrypted.isnot(None)).first()
            if not seller or not seller.wb_api_key:
                logger.info("Brand sync skipped: no WB API key available")
                return

            from services.wb_api_client import WildberriesAPIClient
            with WildberriesAPIClient(seller.wb_api_key) as wb_client:
                engine = get_brand_engine(flask_app)
                stats = engine.sync_marketplace_brands(wb.id, wb_client)
                logger.info(f"Brand sync completed: {stats}")

        except Exception as e:
            logger.error(f"Brand sync background task failed: {e}")


def auto_resolve_pending_brands(flask_app):
    """Фоновый авто-резолв pending брендов."""
    with flask_app.app_context():
        try:
            from models import Seller, Marketplace
            from services.brand_engine import get_brand_engine

            # Находим WB маркетплейс
            wb = Marketplace.query.filter_by(code='wb').first()
            if not wb:
                logger.info("Brand auto-resolve skipped: WB marketplace not found")
                return

            seller = Seller.query.filter(Seller._wb_api_key_encrypted.isnot(None)).first()
            if not seller or not seller.wb_api_key:
                logger.info("Brand auto-resolve skipped: no WB API key available")
                return

            from services.wb_api_client import WildberriesAPIClient
            with WildberriesAPIClient(seller.wb_api_key) as wb_client:
                engine = get_brand_engine(flask_app)
                stats = engine.auto_resolve_pending(wb_client, marketplace_id=wb.id)
                logger.info(f"Brand auto-resolve completed: {stats}")

        except Exception as e:
            logger.error(f"Brand auto-resolve background task failed: {e}")
