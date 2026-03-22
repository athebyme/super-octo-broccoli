# -*- coding: utf-8 -*-
"""
Роуты мониторинга конкурентов.
Управление группами, товарами, алертами и настройками мониторинга.
"""
import logging
from flask import render_template, request, jsonify, redirect, url_for, flash, current_app
from flask_login import login_required, current_user

from models import (
    db, CompetitorMonitorSettings, CompetitorGroup, CompetitorProduct,
    CompetitorPriceSnapshot, CompetitorAlert
)

logger = logging.getLogger('competitor_routes')


def register_competitor_routes(app):
    """Регистрация роутов мониторинга конкурентов"""

    def _get_seller():
        """Получить текущего селлера или None"""
        if current_user.seller:
            return current_user.seller
        return None

    def _get_or_create_settings(seller_id):
        """Получить или создать настройки мониторинга"""
        settings = CompetitorMonitorSettings.query.filter_by(seller_id=seller_id).first()
        if not settings:
            settings = CompetitorMonitorSettings(seller_id=seller_id)
            db.session.add(settings)
            db.session.commit()
        return settings

    # ========================= СТРАНИЦЫ =========================

    @app.route('/competitors')
    @login_required
    def competitors_dashboard():
        """Главная страница мониторинга конкурентов"""
        seller = _get_seller()
        if not seller:
            flash('Необходимо настроить магазин', 'warning')
            return redirect(url_for('dashboard'))

        settings = _get_or_create_settings(seller.id)
        groups = CompetitorGroup.query.filter_by(seller_id=seller.id, is_active=True).all()

        # Статистика
        total_products = CompetitorProduct.query.filter_by(
            seller_id=seller.id, is_active=True
        ).count()
        unread_alerts = CompetitorAlert.query.filter_by(
            seller_id=seller.id, is_read=False
        ).count()

        # Последние алерты
        recent_alerts = CompetitorAlert.query.filter_by(
            seller_id=seller.id
        ).order_by(CompetitorAlert.created_at.desc()).limit(10).all()

        return render_template('competitors_dashboard.html',
                               settings=settings,
                               groups=groups,
                               total_products=total_products,
                               unread_alerts=unread_alerts,
                               recent_alerts=recent_alerts)

    @app.route('/competitors/groups')
    @login_required
    def competitors_groups():
        """Страница управления группами конкурентов"""
        seller = _get_seller()
        if not seller:
            return redirect(url_for('dashboard'))

        groups = CompetitorGroup.query.filter_by(seller_id=seller.id).order_by(
            CompetitorGroup.created_at.desc()
        ).all()

        return render_template('competitors_groups.html', groups=groups)

    @app.route('/competitors/groups/<int:group_id>')
    @login_required
    def competitors_group_detail(group_id):
        """Детали группы конкурентов"""
        seller = _get_seller()
        if not seller:
            return redirect(url_for('dashboard'))

        group = CompetitorGroup.query.filter_by(id=group_id, seller_id=seller.id).first_or_404()
        products = CompetitorProduct.query.filter_by(
            group_id=group_id, is_active=True
        ).order_by(CompetitorProduct.current_sale_price.asc().nullslast()).all()

        products_data = [p.to_dict() for p in products]

        return render_template('competitors_group_detail.html',
                               group=group, products=products, products_data=products_data)

    @app.route('/competitors/alerts')
    @login_required
    def competitors_alerts():
        """Страница алертов"""
        seller = _get_seller()
        if not seller:
            return redirect(url_for('dashboard'))

        page = request.args.get('page', 1, type=int)
        alert_type = request.args.get('type', '')
        severity = request.args.get('severity', '')

        query = CompetitorAlert.query.filter_by(seller_id=seller.id)
        if alert_type:
            query = query.filter_by(alert_type=alert_type)
        if severity:
            query = query.filter_by(severity=severity)

        alerts = query.order_by(CompetitorAlert.created_at.desc()).paginate(
            page=page, per_page=50, error_out=False
        )

        return render_template('competitors_alerts.html', alerts=alerts,
                               current_type=alert_type, current_severity=severity)

    @app.route('/competitors/settings')
    @login_required
    def competitors_settings():
        """Страница настроек мониторинга"""
        seller = _get_seller()
        if not seller:
            return redirect(url_for('dashboard'))

        settings = _get_or_create_settings(seller.id)
        return render_template('competitors_settings.html', settings=settings)

    # ========================= API =========================

    @app.route('/api/competitors/settings', methods=['GET', 'PUT'])
    @login_required
    def api_competitors_settings():
        """GET/PUT настройки мониторинга"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        settings = _get_or_create_settings(seller.id)

        if request.method == 'GET':
            return jsonify(settings.to_dict())

        data = request.get_json()
        if not data:
            return jsonify({'error': 'Нет данных'}), 400

        was_enabled = settings.is_enabled

        if 'is_enabled' in data:
            settings.is_enabled = bool(data['is_enabled'])
        if 'price_change_alert_percent' in data:
            settings.price_change_alert_percent = max(0.1, float(data['price_change_alert_percent']))
        if 'requests_per_minute' in data:
            settings.requests_per_minute = max(1, min(120, int(data['requests_per_minute'])))
        if 'max_products' in data:
            settings.max_products = max(1, int(data['max_products']))
        if 'pause_between_cycles_seconds' in data:
            settings.pause_between_cycles_seconds = max(0, int(data['pause_between_cycles_seconds']))

        db.session.commit()

        # Запуск/остановка мониторинга
        if settings.is_enabled and not was_enabled:
            from services.competitor_monitor import start_competitor_monitor_loop
            start_competitor_monitor_loop(seller.id, current_app._get_current_object())
        elif not settings.is_enabled and was_enabled:
            from services.competitor_monitor import stop_competitor_monitor_loop
            stop_competitor_monitor_loop(seller.id)

        return jsonify(settings.to_dict())

    @app.route('/api/competitors/groups', methods=['GET', 'POST'])
    @login_required
    def api_competitors_groups():
        """GET: список групп; POST: создать группу"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        if request.method == 'GET':
            groups = CompetitorGroup.query.filter_by(seller_id=seller.id).order_by(
                CompetitorGroup.created_at.desc()
            ).all()
            return jsonify([g.to_dict() for g in groups])

        data = request.get_json()
        if not data or not data.get('name'):
            return jsonify({'error': 'Укажите название группы'}), 400

        group = CompetitorGroup(
            seller_id=seller.id,
            name=data['name'],
            description=data.get('description', ''),
            color=data.get('color', '#3B82F6'),
            own_product_id=data.get('own_product_id'),
            auto_source=data.get('auto_source', 'manual'),
            auto_source_value=data.get('auto_source_value'),
        )
        db.session.add(group)
        db.session.commit()

        return jsonify(group.to_dict()), 201

    @app.route('/api/competitors/groups/<int:group_id>', methods=['PUT', 'DELETE'])
    @login_required
    def api_competitors_group(group_id):
        """PUT: обновить группу; DELETE: удалить"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        group = CompetitorGroup.query.filter_by(id=group_id, seller_id=seller.id).first()
        if not group:
            return jsonify({'error': 'Группа не найдена'}), 404

        if request.method == 'DELETE':
            db.session.delete(group)
            db.session.commit()
            return jsonify({'success': True})

        data = request.get_json()
        if data.get('name'):
            group.name = data['name']
        if 'description' in data:
            group.description = data['description']
        if 'color' in data:
            group.color = data['color']
        if 'own_product_id' in data:
            group.own_product_id = data['own_product_id']
        if 'is_active' in data:
            group.is_active = bool(data['is_active'])

        db.session.commit()
        return jsonify(group.to_dict())

    @app.route('/api/competitors/products', methods=['POST'])
    @login_required
    def api_competitors_add_products():
        """
        Добавить товары конкурентов.

        Body:
        - group_id: ID группы
        - nm_ids: [list] — добавить по nm_id
        - search_query: str — найти и добавить по запросу
        - wb_supplier_id: int — добавить все товары продавца
        """
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        data = request.get_json()
        if not data:
            return jsonify({'error': 'Нет данных'}), 400

        group_id = data.get('group_id')
        if not group_id:
            return jsonify({'error': 'Укажите group_id'}), 400

        group = CompetitorGroup.query.filter_by(id=group_id, seller_id=seller.id).first()
        if not group:
            return jsonify({'error': 'Группа не найдена'}), 404

        from services.competitor_monitor import CompetitorMonitorService
        service = CompetitorMonitorService()

        products_data = []
        source = 'manual'

        # Вариант 1: по nm_ids
        if data.get('nm_ids'):
            nm_ids = data['nm_ids']
            if isinstance(nm_ids, str):
                nm_ids = [int(x.strip()) for x in nm_ids.split(',') if x.strip().isdigit()]
            products_data_dict = service.fetch_products_batch(nm_ids[:100])
            products_data = list(products_data_dict.values())
            # Если не все найдены через API, добавляем как заглушки
            found_ids = {p['nm_id'] for p in products_data}
            for nm_id in nm_ids:
                if nm_id not in found_ids:
                    products_data.append({'nm_id': nm_id})

        # Вариант 2: по поисковому запросу
        elif data.get('search_query'):
            products_data = service.search_products(data['search_query'], limit=data.get('limit', 50))
            source = 'category'

        # Вариант 3: по ID продавца на WB
        elif data.get('wb_supplier_id'):
            products_data = service.fetch_seller_catalog(
                int(data['wb_supplier_id']),
                limit=data.get('limit', 500)
            )
            source = 'seller'
            group.auto_source = 'seller'
            group.auto_source_value = str(data['wb_supplier_id'])

        if not products_data:
            return jsonify({'error': 'Товары не найдены'}), 404

        added = 0
        skipped = 0
        for pd in products_data:
            nm_id = pd.get('nm_id')
            if not nm_id:
                continue

            # Проверяем дубликат
            existing = CompetitorProduct.query.filter_by(
                seller_id=seller.id, nm_id=nm_id, group_id=group_id
            ).first()
            if existing:
                skipped += 1
                continue

            product = CompetitorProduct(
                seller_id=seller.id,
                group_id=group_id,
                nm_id=nm_id,
                title=pd.get('title'),
                brand=pd.get('brand'),
                supplier_name=pd.get('supplier_name'),
                wb_supplier_id=pd.get('wb_supplier_id'),
                image_url=pd.get('image_url'),
                current_price=pd.get('price'),
                current_sale_price=pd.get('sale_price'),
                current_rating=pd.get('rating'),
                current_feedbacks_count=pd.get('feedbacks_count'),
                current_total_stock=pd.get('total_stock'),
                last_fetched_at=None if not pd.get('price') else db.func.now(),
            )
            db.session.add(product)
            added += 1

            # Создаём начальный снимок если есть данные
            if pd.get('price') or pd.get('sale_price'):
                snapshot = CompetitorPriceSnapshot(
                    product_id=None,  # будет заполнено после flush
                    seller_id=seller.id,
                    price=pd.get('price'),
                    sale_price=pd.get('sale_price'),
                    rating=pd.get('rating'),
                    feedbacks_count=pd.get('feedbacks_count'),
                    total_stock=pd.get('total_stock'),
                )
                db.session.flush()  # получаем product.id
                snapshot.product_id = product.id
                db.session.add(snapshot)

        db.session.commit()

        return jsonify({
            'success': True,
            'added': added,
            'skipped': skipped,
            'total_in_group': group.products.filter_by(is_active=True).count(),
        })

    @app.route('/api/competitors/products/<int:product_id>', methods=['DELETE'])
    @login_required
    def api_competitors_remove_product(product_id):
        """Удалить товар из мониторинга"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        product = CompetitorProduct.query.filter_by(id=product_id, seller_id=seller.id).first()
        if not product:
            return jsonify({'error': 'Товар не найден'}), 404

        db.session.delete(product)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/api/competitors/products/<int:product_id>/history')
    @login_required
    def api_competitors_product_history(product_id):
        """История цен товара для графика"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        product = CompetitorProduct.query.filter_by(id=product_id, seller_id=seller.id).first()
        if not product:
            return jsonify({'error': 'Товар не найден'}), 404

        period = request.args.get('period', '30d')
        days_map = {'7d': 7, '30d': 30, '90d': 90, '1y': 365}
        days = days_map.get(period, 30)

        from services.competitor_monitor import CompetitorMonitorService
        history = CompetitorMonitorService.get_price_history(product_id, period_days=days)

        return jsonify({
            'product': product.to_dict(),
            'history': history,
        })

    @app.route('/api/competitors/search')
    @login_required
    def api_competitors_search():
        """Поиск товаров на WB"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        query = request.args.get('q', '').strip()
        if not query:
            return jsonify({'error': 'Укажите поисковый запрос'}), 400

        from services.competitor_monitor import CompetitorMonitorService
        service = CompetitorMonitorService()
        results = service.search_products(query, limit=50)

        return jsonify(results)

    @app.route('/api/competitors/seller-catalog')
    @login_required
    def api_competitors_seller_catalog():
        """Получить каталог продавца на WB"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        wb_supplier_id = request.args.get('supplier_id', type=int)
        if not wb_supplier_id:
            return jsonify({'error': 'Укажите supplier_id'}), 400

        from services.competitor_monitor import CompetitorMonitorService
        service = CompetitorMonitorService()
        results = service.fetch_seller_catalog(wb_supplier_id, limit=200)

        return jsonify(results)

    @app.route('/api/competitors/alerts', methods=['GET'])
    @login_required
    def api_competitors_alerts_list():
        """Список алертов (пагинация)"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)

        query = CompetitorAlert.query.filter_by(seller_id=seller.id)

        alert_type = request.args.get('type')
        if alert_type:
            query = query.filter_by(alert_type=alert_type)

        unread_only = request.args.get('unread', '0') == '1'
        if unread_only:
            query = query.filter_by(is_read=False)

        alerts = query.order_by(CompetitorAlert.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        return jsonify({
            'alerts': [a.to_dict() for a in alerts.items],
            'total': alerts.total,
            'page': alerts.page,
            'pages': alerts.pages,
        })

    @app.route('/api/competitors/alerts/mark-read', methods=['POST'])
    @login_required
    def api_competitors_alerts_mark_read():
        """Пометить алерты как прочитанные"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        data = request.get_json()
        if not data:
            return jsonify({'error': 'Нет данных'}), 400

        alert_ids = data.get('alert_ids', [])
        mark_all = data.get('mark_all', False)

        if mark_all:
            CompetitorAlert.query.filter_by(
                seller_id=seller.id, is_read=False
            ).update({'is_read': True})
        elif alert_ids:
            CompetitorAlert.query.filter(
                CompetitorAlert.id.in_(alert_ids),
                CompetitorAlert.seller_id == seller.id
            ).update({'is_read': True}, synchronize_session=False)

        db.session.commit()
        return jsonify({'success': True})

    @app.route('/api/competitors/dashboard-data')
    @login_required
    def api_competitors_dashboard_data():
        """Данные для дашборда"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        settings = _get_or_create_settings(seller.id)
        groups = CompetitorGroup.query.filter_by(seller_id=seller.id, is_active=True).all()

        groups_data = []
        for g in groups:
            products = CompetitorProduct.query.filter_by(
                group_id=g.id, is_active=True
            ).all()

            prices = [p.current_sale_price for p in products if p.current_sale_price]
            groups_data.append({
                **g.to_dict(),
                'products_count': len(products),
                'avg_price': round(sum(prices) / len(prices)) if prices else None,
                'min_price': min(prices) if prices else None,
                'max_price': max(prices) if prices else None,
            })

        total_products = CompetitorProduct.query.filter_by(
            seller_id=seller.id, is_active=True
        ).count()
        unread_alerts = CompetitorAlert.query.filter_by(
            seller_id=seller.id, is_read=False
        ).count()

        return jsonify({
            'settings': settings.to_dict(),
            'groups': groups_data,
            'total_products': total_products,
            'unread_alerts': unread_alerts,
        })

    @app.route('/api/competitors/compare/<int:group_id>')
    @login_required
    def api_competitors_compare(group_id):
        """Сравнение товаров в группе с собственным товаром"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        group = CompetitorGroup.query.filter_by(id=group_id, seller_id=seller.id).first()
        if not group:
            return jsonify({'error': 'Группа не найдена'}), 404

        from services.competitor_monitor import CompetitorMonitorService
        competitors = CompetitorMonitorService.get_group_comparison(group_id)

        own_product = None
        if group.own_product_id and group.own_product:
            own_product = {
                'nm_id': group.own_product.nm_id,
                'title': group.own_product.title,
                'price': float(group.own_product.price) if group.own_product.price else None,
                'discount_price': float(group.own_product.discount_price) if group.own_product.discount_price else None,
            }

        return jsonify({
            'group': group.to_dict(),
            'own_product': own_product,
            'competitors': competitors,
        })

    @app.route('/api/competitors/sync', methods=['POST'])
    @login_required
    def api_competitors_force_sync():
        """Принудительный запуск синхронизации"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        import threading
        flask_app = current_app._get_current_object()

        def _sync():
            CompetitorMonitorService.sync_seller_competitors(seller.id, flask_app)

        from services.competitor_monitor import CompetitorMonitorService
        thread = threading.Thread(target=_sync, daemon=True)
        thread.start()

        return jsonify({'success': True, 'message': 'Синхронизация запущена'})
