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

STALE_PUBLISHING_TIMEOUT = timedelta(minutes=30)
STALE_PUBLISHING_BATCH_SIZE = 100
STALE_PUBLISHING_ERROR = (
    "Результат публикации неизвестен: выполнение не завершилось за 30 минут. "
    "Проверьте соцсеть перед ручным повтором."
)


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
                    try:
                        db.session.rollback()
                    except Exception:
                        db.session.remove()

        except Exception as e:
            logger.error(f"Auto-generate global error: {e}", exc_info=True)
            try:
                from models import db
                db.session.remove()
            except Exception:
                pass


def _auto_generate_for_factory(factory, now, db):
    """Генерирует один пост для фабрики, если прошло достаточно времени."""
    from flask import current_app
    from models import ContentItem
    from services.content_factory_service import ContentFactoryService

    if (
        (factory.catalog_source or 'legacy_wb') == 'marketplace_listing'
        and not current_app.config.get('MARKETPLACE_OZON_ENABLED', False)
    ):
        logger.info(
            "Auto-generate: Ozon source disabled for factory %s",
            factory.id,
        )
        return

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

    # Запрет повтора последнего товара подряд
    last_item = ContentItem.query.filter(
        ContentItem.factory_id == factory.id,
        ContentItem.status.in_(['draft', 'approved', 'published']),
    ).order_by(ContentItem.created_at.desc()).first()
    last_product_ids = (
        set(service.selection_ids_for_item(factory, last_item))
        if last_item else set()
    )

    # select_products теперь сам считает use_count и ротирует — берём топ-10 кандидатов
    products = service.select_products(factory, limit=10, exclude_product_ids=last_product_ids)
    if not products:
        # Если исключение последнего товара дало пустоту — берём без исключений
        products = service.select_products(factory, limit=10)
    if not products:
        logger.warning(f"Auto-generate: no products for factory {factory.id}")
        return

    # Фильтруем: только с фотографиями
    products_with_photos = [p for p in products if p.get('photos')]
    if not products_with_photos:
        logger.warning(
            f"Auto-generate: {len(products)} products found but none have photos "
            f"for factory {factory.id}"
        )
        products_with_photos = products

    # Первый товар уже рандомный (select_products шафлит внутри tier),
    # но берём из топ-3 для доп. вариативности
    product = _random.choice(products_with_photos[:min(3, len(products_with_photos))])
    product_id = product.get('id')
    if not product_id:
        logger.warning(f"Auto-generate: product without id for factory {factory.id}")
        return

    photo_count = len(product.get('photos', []))
    logger.info(f"Auto-generate: selected product {product_id} with {photo_count} photos")

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
        product_ids=[] if product.get('entity_ref') else [product_id],
        content_type=content_type,
        entity_refs=[product['entity_ref']] if product.get('entity_ref') else None,
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

def recover_stale_publishing_items(db, now=None, limit=None):
    """Fail closed for abandoned publish claims without retrying provider writes."""
    from models import ContentItem

    now = now or datetime.utcnow()
    cutoff = now - STALE_PUBLISHING_TIMEOUT
    batch_size = max(
        1,
        min(STALE_PUBLISHING_BATCH_SIZE, int(limit or STALE_PUBLISHING_BATCH_SIZE)),
    )
    candidate_ids = [
        row[0]
        for row in ContentItem.query.with_entities(ContentItem.id).filter(
            ContentItem.status == 'publishing',
            ContentItem.updated_at < cutoff,
        ).order_by(
            ContentItem.updated_at.asc(),
            ContentItem.id.asc(),
        ).limit(batch_size).all()
    ]
    if not candidate_ids:
        return 0

    recovered = ContentItem.query.filter(
        ContentItem.id.in_(candidate_ids),
        ContentItem.status == 'publishing',
        ContentItem.updated_at < cutoff,
    ).update(
        {
            'status': 'failed',
            'error_message': STALE_PUBLISHING_ERROR,
            'updated_at': now,
        },
        synchronize_session=False,
    )
    db.session.commit()
    return int(recovered or 0)


def auto_publish_content(flask_app):
    """
    Основная функция автопубликации.
    Проверяет все фабрики с auto_publish=True и публикует следующий пост.
    """
    with flask_app.app_context():
        try:
            from models import db, ContentFactory

            now = datetime.utcnow()
            recovered = recover_stale_publishing_items(db, now=now)
            if recovered:
                logger.warning(
                    "Auto-publish reconciled %s stale publishing item(s) "
                    "without provider retry",
                    recovered,
                )

            # Находим все активные фабрики с включённой автопубликацией
            factories = ContentFactory.query.filter_by(
                is_active=True,
                auto_publish=True,
            ).all()

            if not factories:
                return

            for factory in factories:
                try:
                    _auto_publish_for_factory(factory, now, db)
                except Exception as e:
                    logger.error(f"Auto-publish error for factory {factory.id}: {e}", exc_info=True)
                    try:
                        db.session.rollback()
                    except Exception:
                        db.session.remove()

        except Exception as e:
            logger.error(f"Auto-publish global error: {e}", exc_info=True)
            try:
                from models import db
                db.session.remove()
            except Exception:
                pass


def _auto_publish_for_factory(factory, now, db):
    """Публикует следующий одобренный пост для фабрики, если пришло время."""
    from models import ContentItem, SocialAccount
    from services.content_publishers import get_publisher
    from services.social_account_publish_health import (
        automatic_publish_is_blocked,
        clear_publish_failure,
        record_publish_failure,
    )

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

    if automatic_publish_is_blocked(account):
        logger.debug(
            "Auto-publish skipped quarantined account=%s code=%s",
            account.id,
            account.last_error_code,
        )
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
        ).update(
            {
                'status': 'publishing',
                'social_account_id': account.id,
            },
            synchronize_session='fetch',
        )
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
            clear_publish_failure(account)
            factory.last_auto_publish_at = now
            logger.info(
                f"Auto-published item {item.id} to {factory.platform} "
                f"(factory={factory.id}, post_url={result.external_post_url})"
            )
        else:
            item.status = 'failed'
            item.error_message = result.error
            record_publish_failure(account, result, now=now)
            logger.warning(
                "Auto-publish failed for item %s: code=%s terminal=%s",
                item.id,
                result.error_code or 'unknown',
                bool(result.terminal),
            )

        db.session.commit()

    except ValueError as e:
        try:
            item.status = 'failed'
            item.error_message = str(e)
            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                db.session.remove()
        logger.error(f"Auto-publish ValueError for item {item.id}: {e}")
    except Exception as e:
        try:
            item.status = 'failed'
            item.error_message = str(e)
            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                db.session.remove()
        logger.error(f"Auto-publish error for item {item.id}: {e}", exc_info=True)
