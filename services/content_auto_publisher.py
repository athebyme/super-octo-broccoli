# -*- coding: utf-8 -*-
"""
Автопубликация контента — фоновый сервис для публикации одобренных постов по расписанию.

Вызывается из планировщика (APScheduler) каждые 2 минуты.
Для каждой фабрики с auto_publish=True находит следующий одобренный пост
и публикует его, если прошло достаточно времени с последней публикации.
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def auto_publish_content(flask_app):
    """
    Основная функция автопубликации.
    Проверяет все фабрики с auto_publish=True и публикует следующий пост.
    """
    with flask_app.app_context():
        try:
            from models import db, ContentFactory, ContentItem, SocialAccount
            from services.content_publishers import get_publisher

            # Находим все активные фабрики с включённой автопубликацией
            factories = ContentFactory.query.filter_by(
                is_active=True,
                auto_publish=True,
            ).all()

            if not factories:
                return

            now = datetime.utcnow()

            for factory in factories:
                try:
                    _auto_publish_for_factory(factory, now, db)
                except Exception as e:
                    logger.error(f"Auto-publish error for factory {factory.id}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Auto-publish global error: {e}", exc_info=True)


def _auto_publish_for_factory(factory, now, db):
    """Публикует следующий одобренный пост для фабрики, если пришло время."""
    from models import ContentItem, SocialAccount
    from services.content_publishers import get_publisher

    interval = factory.publish_interval_minutes or 60

    # Проверяем, прошло ли достаточно времени с последней публикации
    if factory.last_auto_publish_at:
        next_publish = factory.last_auto_publish_at + timedelta(minutes=interval)
        if now < next_publish:
            return  # Ещё рано

    # Находим аккаунт для публикации
    account = None
    if factory.default_social_account_id:
        account = SocialAccount.query.filter_by(
            id=factory.default_social_account_id,
            is_active=True,
        ).first()

    # Фоллбэк: любой активный аккаунт для платформы
    if not account:
        account = SocialAccount.query.filter_by(
            seller_id=factory.seller_id,
            platform=factory.platform,
            is_active=True,
        ).first()

    if not account:
        logger.warning(f"Auto-publish: no account for factory {factory.id} ({factory.platform})")
        return

    # Находим следующий одобренный пост (FIFO)
    item = ContentItem.query.filter_by(
        factory_id=factory.id,
        status='approved',
    ).order_by(ContentItem.created_at.asc()).first()

    if not item:
        return  # Нет постов для публикации

    # Публикуем — атомарно ставим статус чтобы избежать дублей
    try:
        updated = ContentItem.query.filter(
            ContentItem.id == item.id,
            ContentItem.status == 'approved',
        ).update({'status': 'publishing'}, synchronize_session='fetch')
        db.session.commit()
        if not updated:
            logger.info(f"Auto-publish: item {item.id} already picked up by another process")
            return
        db.session.refresh(item)

        publisher = get_publisher(factory.platform)
        result = publisher.publish(item, account)

        if result.success:
            item.status = 'published'
            item.published_at = now
            item.external_post_id = result.external_post_id
            item.external_post_url = result.external_post_url
            item.error_message = None
            account.last_used_at = now
            account.last_error = None
            factory.last_auto_publish_at = now
            logger.info(
                f"Auto-published item {item.id} to {factory.platform} "
                f"(factory={factory.id}, post_url={result.external_post_url})"
            )
        else:
            item.status = 'failed'
            item.error_message = result.error
            account.last_error = result.error
            logger.warning(f"Auto-publish failed for item {item.id}: {result.error}")

        db.session.commit()

    except ValueError as e:
        item.status = 'failed'
        item.error_message = str(e)
        db.session.commit()
        logger.error(f"Auto-publish ValueError for item {item.id}: {e}")
    except Exception as e:
        item.status = 'failed'
        item.error_message = str(e)
        db.session.commit()
        logger.error(f"Auto-publish error for item {item.id}: {e}", exc_info=True)
