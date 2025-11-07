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
                        args=(seller.id, flask_app._get_current_object()),
                        daemon=True,
                        name=f"sync-seller-{seller.id}"
                    )
                    thread.start()
                elif seller.api_sync_status == 'syncing':
                    logger.debug(f"⏳ Seller {seller.id} sync already in progress")

        except Exception as e:
            logger.exception(f"❌ Error in check_and_sync_all_sellers: {str(e)}")


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
