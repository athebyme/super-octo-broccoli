# -*- coding: utf-8 -*-
"""
Content Factory Routes — роуты для контент-фабрики

Паттерн: register_content_factory_routes(app)
"""
import json
import logging
from datetime import datetime, timedelta

from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user

from models import (
    db, ContentFactory, ContentItem, ContentTemplate,
    ContentPlan, SocialAccount, Product,
    CONTENT_PLATFORMS, CONTENT_TYPES, CONTENT_TONES,
    CONTENT_STATUSES, PRODUCT_SELECTION_MODES,
)
from services.content_factory_service import (
    ContentFactoryService,
    PLATFORM_LABELS, CONTENT_TYPE_LABELS, STATUS_LABELS,
)

logger = logging.getLogger(__name__)


def register_content_factory_routes(app):
    """Регистрирует роуты контент-фабрики."""

    service = ContentFactoryService()

    # ================================================================
    # Страницы
    # ================================================================

    @app.route('/content-factory')
    @login_required
    def content_factory_dashboard():
        """Дашборд контент-фабрик."""
        if not current_user.seller:
            return redirect(url_for('dashboard'))

        factories = ContentFactory.query.filter_by(
            seller_id=current_user.seller.id
        ).order_by(ContentFactory.updated_at.desc()).all()

        # Статистика для каждой фабрики
        factories_data = []
        for f in factories:
            stats = service.get_factory_stats(f)
            factories_data.append({
                'factory': f,
                'stats': stats,
            })

        # Общая статистика
        total_items = ContentItem.query.filter_by(
            seller_id=current_user.seller.id
        ).count()
        total_published = ContentItem.query.filter_by(
            seller_id=current_user.seller.id, status='published'
        ).count()
        total_scheduled = ContentItem.query.filter_by(
            seller_id=current_user.seller.id, status='scheduled'
        ).count()

        # Подключённые аккаунты
        accounts = SocialAccount.query.filter_by(
            seller_id=current_user.seller.id, is_active=True
        ).all()

        return render_template(
            'content_factory_dashboard.html',
            factories=factories_data,
            total_items=total_items,
            total_published=total_published,
            total_scheduled=total_scheduled,
            accounts=accounts,
            platform_labels=PLATFORM_LABELS,
            content_type_labels=CONTENT_TYPE_LABELS,
            status_labels=STATUS_LABELS,
        )

    @app.route('/content-factory/create', methods=['GET', 'POST'])
    @login_required
    def content_factory_create():
        """Создание контент-фабрики."""
        if not current_user.seller:
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            if not name:
                flash('Укажите название фабрики', 'error')
                return redirect(url_for('content_factory_create'))

            platform = request.form.get('platform', 'telegram')
            content_types = request.form.getlist('content_types')
            if not content_types:
                content_types = ['promo_post']

            factory = ContentFactory(
                seller_id=current_user.seller.id,
                name=name,
                description=request.form.get('description', '').strip(),
                platform=platform,
                tone=request.form.get('tone', 'casual'),
                style_guidelines=request.form.get('style_guidelines', '').strip(),
                ai_provider=request.form.get('ai_provider', 'openai'),
                product_selection_mode=request.form.get('product_selection_mode', 'manual'),
                auto_approve=bool(request.form.get('auto_approve')),
            )
            factory.set_content_types(content_types)

            # Правила подбора товаров
            rules = {}
            if request.form.get('rule_category'):
                rules['category'] = request.form['rule_category']
            if request.form.get('rule_brand'):
                rules['brand'] = request.form['rule_brand']
            if request.form.get('rule_min_price'):
                try:
                    rules['min_price'] = float(request.form['rule_min_price'])
                except ValueError:
                    pass
            if request.form.get('rule_max_price'):
                try:
                    rules['max_price'] = float(request.form['rule_max_price'])
                except ValueError:
                    pass
            factory.set_selection_rules(rules)

            # Соцаккаунт по умолчанию
            default_account_id = request.form.get('default_social_account_id')
            if default_account_id:
                factory.default_social_account_id = int(default_account_id)

            db.session.add(factory)
            db.session.commit()

            flash(f'Фабрика "{name}" создана', 'success')
            return redirect(url_for('content_factory_items', factory_id=factory.id))

        accounts = SocialAccount.query.filter_by(
            seller_id=current_user.seller.id, is_active=True
        ).all()

        return render_template(
            'content_factory_form.html',
            factory=None,
            accounts=accounts,
            platforms=CONTENT_PLATFORMS,
            content_types=CONTENT_TYPES,
            tones=CONTENT_TONES,
            selection_modes=PRODUCT_SELECTION_MODES,
            platform_labels=PLATFORM_LABELS,
            content_type_labels=CONTENT_TYPE_LABELS,
        )

    @app.route('/content-factory/<int:factory_id>/edit', methods=['GET', 'POST'])
    @login_required
    def content_factory_edit(factory_id):
        """Редактирование фабрики."""
        if not current_user.seller:
            return redirect(url_for('dashboard'))

        factory = ContentFactory.query.filter_by(
            id=factory_id, seller_id=current_user.seller.id
        ).first_or_404()

        if request.method == 'POST':
            factory.name = request.form.get('name', factory.name).strip()
            factory.description = request.form.get('description', '').strip()
            factory.platform = request.form.get('platform', factory.platform)
            factory.tone = request.form.get('tone', factory.tone)
            factory.style_guidelines = request.form.get('style_guidelines', '').strip()
            factory.ai_provider = request.form.get('ai_provider', factory.ai_provider)
            factory.product_selection_mode = request.form.get('product_selection_mode', factory.product_selection_mode)
            factory.auto_approve = bool(request.form.get('auto_approve'))
            factory.is_active = bool(request.form.get('is_active'))

            content_types = request.form.getlist('content_types')
            if content_types:
                factory.set_content_types(content_types)

            rules = {}
            if request.form.get('rule_category'):
                rules['category'] = request.form['rule_category']
            if request.form.get('rule_brand'):
                rules['brand'] = request.form['rule_brand']
            if request.form.get('rule_min_price'):
                try:
                    rules['min_price'] = float(request.form['rule_min_price'])
                except ValueError:
                    pass
            if request.form.get('rule_max_price'):
                try:
                    rules['max_price'] = float(request.form['rule_max_price'])
                except ValueError:
                    pass
            factory.set_selection_rules(rules)

            default_account_id = request.form.get('default_social_account_id')
            if default_account_id:
                factory.default_social_account_id = int(default_account_id)
            else:
                factory.default_social_account_id = None

            db.session.commit()
            flash('Настройки сохранены', 'success')
            return redirect(url_for('content_factory_items', factory_id=factory.id))

        accounts = SocialAccount.query.filter_by(
            seller_id=current_user.seller.id, is_active=True
        ).all()

        return render_template(
            'content_factory_form.html',
            factory=factory,
            accounts=accounts,
            platforms=CONTENT_PLATFORMS,
            content_types=CONTENT_TYPES,
            tones=CONTENT_TONES,
            selection_modes=PRODUCT_SELECTION_MODES,
            platform_labels=PLATFORM_LABELS,
            content_type_labels=CONTENT_TYPE_LABELS,
        )

    @app.route('/content-factory/<int:factory_id>/items')
    @login_required
    def content_factory_items(factory_id):
        """Список контента фабрики."""
        if not current_user.seller:
            return redirect(url_for('dashboard'))

        factory = ContentFactory.query.filter_by(
            id=factory_id, seller_id=current_user.seller.id
        ).first_or_404()

        status_filter = request.args.get('status', '')
        type_filter = request.args.get('content_type', '')
        page = request.args.get('page', 1, type=int)

        query = ContentItem.query.filter_by(factory_id=factory.id)
        if status_filter:
            query = query.filter_by(status=status_filter)
        if type_filter:
            query = query.filter_by(content_type=type_filter)

        items = query.order_by(ContentItem.created_at.desc()).paginate(
            page=page, per_page=20, error_out=False
        )

        stats = service.get_factory_stats(factory)
        templates = service.get_templates_for_factory(factory)

        # Товары для генерации
        products = service.select_products(factory, limit=50)

        return render_template(
            'content_factory_items.html',
            factory=factory,
            items=items,
            stats=stats,
            templates=templates,
            products=products,
            status_filter=status_filter,
            type_filter=type_filter,
            platform_labels=PLATFORM_LABELS,
            content_type_labels=CONTENT_TYPE_LABELS,
            status_labels=STATUS_LABELS,
            content_types=factory.get_content_types() or CONTENT_TYPES,
        )

    @app.route('/content-factory/<int:factory_id>/calendar')
    @login_required
    def content_factory_calendar(factory_id):
        """Контент-календарь."""
        if not current_user.seller:
            return redirect(url_for('dashboard'))

        factory = ContentFactory.query.filter_by(
            id=factory_id, seller_id=current_user.seller.id
        ).first_or_404()

        return render_template(
            'content_factory_calendar.html',
            factory=factory,
            platform_labels=PLATFORM_LABELS,
            content_type_labels=CONTENT_TYPE_LABELS,
            status_labels=STATUS_LABELS,
        )

    @app.route('/content-factory/accounts')
    @login_required
    def content_factory_accounts():
        """Управление подключёнными аккаунтами."""
        if not current_user.seller:
            return redirect(url_for('dashboard'))

        accounts = SocialAccount.query.filter_by(
            seller_id=current_user.seller.id
        ).order_by(SocialAccount.connected_at.desc()).all()

        return render_template(
            'content_factory_accounts.html',
            accounts=accounts,
            platforms=CONTENT_PLATFORMS,
            platform_labels=PLATFORM_LABELS,
        )

    # ================================================================
    # API endpoints
    # ================================================================

    @app.route('/api/content-factory/<int:factory_id>/generate', methods=['POST'])
    @login_required
    def api_content_factory_generate(factory_id):
        """Генерация контента."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        factory = ContentFactory.query.filter_by(
            id=factory_id, seller_id=current_user.seller.id
        ).first()
        if not factory:
            return jsonify({'error': 'Фабрика не найдена'}), 404

        data = request.get_json() or {}
        product_ids = data.get('product_ids', [])
        content_type = data.get('content_type', 'promo_post')
        template_id = data.get('template_id')
        custom_prompt = data.get('custom_prompt')

        if not product_ids:
            return jsonify({'error': 'Выберите товары для генерации'}), 400

        item, error = service.generate_and_save(
            factory=factory,
            product_ids=product_ids,
            content_type=content_type,
            template_id=template_id,
            custom_prompt=custom_prompt,
        )

        if error:
            return jsonify({'error': error}), 500

        return jsonify({
            'success': True,
            'item': item.to_dict(),
        })

    @app.route('/api/content-factory/<int:factory_id>/generate-bulk', methods=['POST'])
    @login_required
    def api_content_factory_generate_bulk(factory_id):
        """Массовая генерация контента."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        factory = ContentFactory.query.filter_by(
            id=factory_id, seller_id=current_user.seller.id
        ).first()
        if not factory:
            return jsonify({'error': 'Фабрика не найдена'}), 404

        data = request.get_json() or {}
        content_type = data.get('content_type', 'promo_post')
        count = min(data.get('count', 5), 20)
        template_id = data.get('template_id')
        custom_prompt = data.get('custom_prompt')

        items, errors = service.generate_bulk(
            factory=factory,
            content_type=content_type,
            count=count,
            template_id=template_id,
            custom_prompt=custom_prompt,
        )

        if not items and errors:
            return jsonify({
                'error': errors[0] if len(errors) == 1 else f"Ошибки генерации: {'; '.join(errors[:3])}",
                'errors': errors,
            }), 500

        return jsonify({
            'success': True,
            'generated': len(items),
            'errors': errors,
            'items': [i.to_dict() for i in items],
        })

    @app.route('/api/content-factory/items/<int:item_id>', methods=['GET'])
    @login_required
    def api_content_item_get(item_id):
        """Получение контента."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        item = ContentItem.query.filter_by(
            id=item_id, seller_id=current_user.seller.id
        ).first()
        if not item:
            return jsonify({'error': 'Контент не найден'}), 404

        return jsonify(item.to_dict())

    @app.route('/api/content-factory/items/<int:item_id>/approve', methods=['POST'])
    @login_required
    def api_content_item_approve(item_id):
        """Одобрение контента."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        item = ContentItem.query.filter_by(
            id=item_id, seller_id=current_user.seller.id
        ).first()
        if not item:
            return jsonify({'error': 'Контент не найден'}), 404

        success, error = service.approve_item(item)
        if not success:
            return jsonify({'error': error}), 400

        return jsonify({'success': True, 'status': item.status})

    @app.route('/api/content-factory/items/<int:item_id>/schedule', methods=['POST'])
    @login_required
    def api_content_item_schedule(item_id):
        """Планирование публикации."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        item = ContentItem.query.filter_by(
            id=item_id, seller_id=current_user.seller.id
        ).first()
        if not item:
            return jsonify({'error': 'Контент не найден'}), 404

        data = request.get_json() or {}
        scheduled_at_str = data.get('scheduled_at')
        if not scheduled_at_str:
            return jsonify({'error': 'Укажите дату публикации'}), 400

        try:
            scheduled_at = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            return jsonify({'error': 'Неверный формат даты'}), 400

        social_account_id = data.get('social_account_id')

        success, error = service.schedule_item(item, scheduled_at, social_account_id)
        if not success:
            return jsonify({'error': error}), 400

        return jsonify({'success': True, 'status': item.status})

    @app.route('/api/content-factory/items/<int:item_id>/publish', methods=['POST'])
    @login_required
    def api_content_item_publish(item_id):
        """Немедленная публикация контента."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        item = ContentItem.query.filter_by(
            id=item_id, seller_id=current_user.seller.id
        ).first()
        if not item:
            return jsonify({'error': 'Контент не найден'}), 404

        if item.status not in ('draft', 'approved', 'scheduled'):
            return jsonify({'error': f'Нельзя опубликовать контент со статусом {item.status}'}), 400

        # Определяем аккаунт для публикации
        account = None
        data = request.get_json(silent=True) or {}
        social_account_id = data.get('social_account_id') or item.social_account_id
        # Фоллбэк на дефолтный аккаунт фабрики
        if not social_account_id:
            factory = ContentFactory.query.get(item.factory_id)
            if factory:
                social_account_id = factory.default_social_account_id
        if social_account_id:
            account = SocialAccount.query.filter_by(
                id=social_account_id, seller_id=current_user.seller.id
            ).first()

        # Последний фоллбэк: любой активный аккаунт для этой платформы
        if not account:
            account = SocialAccount.query.filter_by(
                seller_id=current_user.seller.id,
                platform=item.platform,
                is_active=True,
            ).first()

        if not account:
            return jsonify({'error': 'Не указан аккаунт для публикации. Подключите аккаунт в настройках.'}), 400

        # Публикуем
        try:
            from services.content_publishers import get_publisher
            publisher = get_publisher(item.platform)

            item.status = 'publishing'
            db.session.commit()

            result = publisher.publish(item, account)

            if result.success:
                item.status = 'published'
                item.published_at = datetime.utcnow()
                item.external_post_id = result.external_post_id
                item.external_post_url = result.external_post_url
                item.error_message = None
                account.last_used_at = datetime.utcnow()
                account.last_error = None
            else:
                item.status = 'failed'
                item.error_message = result.error
                account.last_error = result.error

            db.session.commit()

            if result.success:
                return jsonify({
                    'success': True,
                    'external_post_id': result.external_post_id,
                    'external_post_url': result.external_post_url,
                })
            else:
                return jsonify({'error': result.error}), 500

        except ValueError as e:
            item.status = 'failed'
            item.error_message = str(e)
            db.session.commit()
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            logger.error(f"Publish error: {e}", exc_info=True)
            item.status = 'failed'
            item.error_message = str(e)
            db.session.commit()
            return jsonify({'error': f'Ошибка публикации: {e}'}), 500

    @app.route('/api/content-factory/items/<int:item_id>/edit', methods=['POST'])
    @login_required
    def api_content_item_edit(item_id):
        """Редактирование контента."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        item = ContentItem.query.filter_by(
            id=item_id, seller_id=current_user.seller.id
        ).first()
        if not item:
            return jsonify({'error': 'Контент не найден'}), 404

        if item.status in ('publishing', 'published'):
            return jsonify({'error': 'Нельзя редактировать опубликованный контент'}), 400

        data = request.get_json() or {}
        if 'title' in data:
            item.title = data['title']
        if 'body_text' in data:
            item.body_text = data['body_text']
        if 'hashtags' in data:
            item.set_hashtags(data['hashtags'])

        db.session.commit()
        return jsonify({'success': True, 'item': item.to_dict()})

    @app.route('/api/content-factory/items/<int:item_id>/regenerate', methods=['POST'])
    @login_required
    def api_content_item_regenerate(item_id):
        """Перегенерация контента."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        item = ContentItem.query.filter_by(
            id=item_id, seller_id=current_user.seller.id
        ).first()
        if not item:
            return jsonify({'error': 'Контент не найден'}), 404

        factory = ContentFactory.query.get(item.factory_id)
        if not factory:
            return jsonify({'error': 'Фабрика не найдена'}), 404

        data = request.get_json() or {}
        custom_prompt = data.get('custom_prompt')

        result = service.generate_content(
            factory=factory,
            product_ids=item.get_product_ids(),
            content_type=item.content_type,
            template_id=item.template_id,
            custom_prompt=custom_prompt,
        )

        if not result.success:
            return jsonify({'error': result.error}), 500

        item.body_text = result.body_text or ''
        item.title = result.title
        item.ai_provider = result.ai_provider
        item.ai_model = result.ai_model
        item.tokens_used = result.tokens_used
        item.generation_time_ms = result.generation_time_ms
        item.status = 'draft'
        if result.hashtags:
            item.set_hashtags(result.hashtags)
        if result.media_urls:
            item.media_urls_json = json.dumps(result.media_urls)

        # Обновляем метаданные
        platform_specific = item.get_platform_specific()
        if result.wb_url:
            platform_specific['wb_url'] = result.wb_url
        if result.store_name:
            platform_specific['store_name'] = result.store_name
        if result.product_names:
            platform_specific['product_names'] = result.product_names
        if result.quality_score:
            platform_specific['quality_score'] = result.quality_score
        platform_specific['char_count'] = len(result.body_text or '')
        item.platform_specific_json = json.dumps(platform_specific, ensure_ascii=False)

        db.session.commit()
        return jsonify({'success': True, 'item': item.to_dict()})

    @app.route('/api/content-factory/items/<int:item_id>', methods=['DELETE'])
    @login_required
    def api_content_item_delete(item_id):
        """Удаление контента."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        item = ContentItem.query.filter_by(
            id=item_id, seller_id=current_user.seller.id
        ).first()
        if not item:
            return jsonify({'error': 'Контент не найден'}), 404

        if item.status == 'publishing':
            return jsonify({'error': 'Нельзя удалить публикуемый контент'}), 400

        db.session.delete(item)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/api/content-factory/items/<int:item_id>/archive', methods=['POST'])
    @login_required
    def api_content_item_archive(item_id):
        """Архивирование контента."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        item = ContentItem.query.filter_by(
            id=item_id, seller_id=current_user.seller.id
        ).first()
        if not item:
            return jsonify({'error': 'Контент не найден'}), 404

        success, error = service.archive_item(item)
        if not success:
            return jsonify({'error': error}), 400

        return jsonify({'success': True})

    # ================================================================
    # API: Управление аккаунтами
    # ================================================================

    @app.route('/api/content-factory/accounts', methods=['GET'])
    @login_required
    def api_social_accounts_list():
        """Список аккаунтов."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        accounts = SocialAccount.query.filter_by(
            seller_id=current_user.seller.id
        ).all()

        return jsonify([a.to_dict() for a in accounts])

    @app.route('/api/content-factory/accounts', methods=['POST'])
    @login_required
    def api_social_account_create():
        """Подключение нового аккаунта."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        data = request.get_json() or {}
        platform = data.get('platform', '').strip()
        if platform not in CONTENT_PLATFORMS:
            return jsonify({'error': f'Неподдерживаемая платформа: {platform}'}), 400

        account = SocialAccount(
            seller_id=current_user.seller.id,
            platform=platform,
            account_name=data.get('account_name', '').strip(),
            account_id=data.get('account_id', '').strip(),
        )

        # Сохраняем credentials
        creds = data.get('credentials', {})
        if creds:
            account.set_credentials_dict(creds)

        db.session.add(account)
        db.session.commit()

        # Валидируем аккаунт
        try:
            from services.content_publishers import get_publisher
            publisher = get_publisher(platform)
            is_valid, error = publisher.validate_account(account)
            if not is_valid:
                return jsonify({
                    'success': True,
                    'account': account.to_dict(),
                    'warning': f'Аккаунт сохранён, но проверка не пройдена: {error}',
                })
        except ValueError:
            pass  # Публишер не реализован — пропускаем валидацию

        return jsonify({
            'success': True,
            'account': account.to_dict(),
        })

    @app.route('/api/content-factory/accounts/<int:account_id>', methods=['DELETE'])
    @login_required
    def api_social_account_delete(account_id):
        """Удаление аккаунта."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        account = SocialAccount.query.filter_by(
            id=account_id, seller_id=current_user.seller.id
        ).first()
        if not account:
            return jsonify({'error': 'Аккаунт не найден'}), 404

        db.session.delete(account)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/api/content-factory/<int:factory_id>', methods=['DELETE'])
    @login_required
    def api_content_factory_delete(factory_id):
        """Удаление фабрики и всего её контента."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        factory = ContentFactory.query.filter_by(
            id=factory_id, seller_id=current_user.seller.id
        ).first()
        if not factory:
            return jsonify({'error': 'Фабрика не найдена'}), 404

        try:
            # Явно удаляем связанные записи для надёжности
            ContentItem.query.filter_by(factory_id=factory.id).delete()
            ContentPlan.query.filter_by(factory_id=factory.id).delete()
            db.session.delete(factory)
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            logger.error(f"Factory delete error: {e}", exc_info=True)
            return jsonify({'error': f'Ошибка удаления: {e}'}), 500

    # ================================================================
    # API: Контент-календарь
    # ================================================================

    @app.route('/api/content-factory/<int:factory_id>/calendar', methods=['GET'])
    @login_required
    def api_content_factory_calendar(factory_id):
        """Данные для контент-календаря."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        factory = ContentFactory.query.filter_by(
            id=factory_id, seller_id=current_user.seller.id
        ).first()
        if not factory:
            return jsonify({'error': 'Фабрика не найдена'}), 404

        # По умолчанию текущий месяц
        date_from_str = request.args.get('date_from')
        date_to_str = request.args.get('date_to')

        if date_from_str:
            date_from = datetime.fromisoformat(date_from_str)
        else:
            now = datetime.utcnow()
            date_from = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        if date_to_str:
            date_to = datetime.fromisoformat(date_to_str)
        else:
            # Конец месяца
            next_month = date_from.replace(day=28) + timedelta(days=4)
            date_to = next_month.replace(day=1) - timedelta(seconds=1)

        events = service.get_items_for_calendar(factory.id, date_from, date_to)

        return jsonify({
            'events': events,
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
        })
