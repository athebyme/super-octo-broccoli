# -*- coding: utf-8 -*-
"""
Автопостинг контента — фоновые сервисы для автогенерации и публикации постов.

Два независимых цикла, вызываемых из APScheduler:
1. auto_generate_content — генерирует посты для фабрик с auto_generate=True
2. auto_publish_content — публикует одобренные посты для фабрик с auto_publish=True

Полный автопостинг: auto_generate=True + auto_approve=True + auto_publish=True
→ рандомный товар → AI-генерация → авто-одобрение → авто-публикация
"""
import logging
import random as _random
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ================================================================
# Автогенерация контента
# ================================================================

def auto_generate_content(flask_app):
    """
    Фоновая автогенерация контента.
    Для каждой фабрики с auto_generate=True:
    1. Выбирает рандомный товар
    2. Выбирает рандомный тип контента из настроек фабрики
    3. Генерирует пост через AI
    4. Если auto_approve — сразу одобряет (автопубликация подхватит)
    """
    with flask_app.app_context():
        try:
            from models import db, ContentFactory

            factories = ContentFactory.query.filter_by(
                is_active=True,
                auto_generate=True,
            ).all()

            if not factories:
                return

            now = datetime.utcnow()

            for factory in factories:
                try:
                    _auto_generate_for_factory(factory, now, db)
                except Exception as e:
                    logger.error(f"Auto-generate error for factory {factory.id}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Auto-generate global error: {e}", exc_info=True)


def _auto_generate_for_factory(factory, now, db):
    """Генерирует один пост для фабрики, если прошло достаточно времени."""
    from models import ContentItem
    from services.content_factory_service import ContentFactoryService

    interval = factory.generate_interval_minutes or 120

    # Проверяем интервал
    if factory.last_auto_generate_at:
        next_gen = factory.last_auto_generate_at + timedelta(minutes=interval)
        if now < next_gen:
            return  # Ещё рано

    # Проверяем: не слишком ли много неопубликованных постов в очереди (чтобы не копились)
    pending_count = ContentItem.query.filter(
        ContentItem.factory_id == factory.id,
        ContentItem.status.in_(['draft', 'approved']),
    ).count()

    max_pending = 10  # Не генерируем если уже 10+ постов ожидают
    if pending_count >= max_pending:
        logger.info(
            f"Auto-generate: factory {factory.id} has {pending_count} pending items, "
            f"skipping (max {max_pending})"
        )
        return

    service = ContentFactoryService()

    # Подбираем товар
    # Собираем ID товаров, для которых уже есть недавние посты (за последние 7 дней)
    recent_cutoff = now - timedelta(days=7)
    recent_items = ContentItem.query.filter(
        ContentItem.factory_id == factory.id,
        ContentItem.created_at >= recent_cutoff,
    ).all()

    exclude_ids = set()
    for item in recent_items:
        exclude_ids.update(item.get_product_ids())

    products = service.select_products(factory, limit=5, exclude_product_ids=exclude_ids)
    if not products:
        # Если все товары использованы за 7 дней — берём без исключений
        products = service.select_products(factory, limit=5)

    if not products:
        logger.warning(f"Auto-generate: no products for factory {factory.id}")
        return

    # Рандомный товар
    product = _random.choice(products)
    product_id = product.get('id')
    if not product_id:
        logger.warning(f"Auto-generate: product without id for factory {factory.id}")
        return

    # Рандомный тип контента из настроек фабрики
    content_types = factory.get_content_types()
    if not content_types:
        content_types = ['promo_post']
    content_type = _random.choice(content_types)

    logger.info(
        f"Auto-generate: factory {factory.id}, product {product_id} "
        f"({product.get('name', '')[:50]}), type={content_type}"
    )

    # Генерируем и сохраняем
    item, error = service.generate_and_save(
        factory=factory,
        product_ids=[product_id],
        content_type=content_type,
    )

    if error:
        logger.error(f"Auto-generate failed for factory {factory.id}: {error}")
        factory.last_auto_generate_at = now  # Обновляем чтобы не спамить ретраями
        db.session.commit()
        return

    factory.last_auto_generate_at = now
    db.session.commit()

    status_label = 'approved (auto)' if factory.auto_approve else 'draft'
    logger.info(
        f"Auto-generated item {item.id} for factory {factory.id}: "
        f"status={status_label}, product={product_id}, type={content_type}"
    )


# ================================================================
# Автопубликация контента
# ================================================================

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
            item.error_message = result.error  # Может быть warning о фото
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
