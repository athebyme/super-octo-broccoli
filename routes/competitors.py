# -*- coding: utf-8 -*-
"""
Роуты мониторинга конкурентов v2.

Web-слой никогда не ходит в WB для синка: добавление товаров — только вставка
строк + next_sync_due_at=now (fetch делает scheduler-tick). Интерактивные
поиск/превью каталога строго bounded: одна страница, 429 -> честный 503.
"""
import logging
from datetime import datetime, timedelta

from flask import render_template, request, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user

from models import (
    db, CompetitorMonitorSettings, CompetitorGroup, CompetitorProduct,
    CompetitorPriceSnapshot, CompetitorAlert, CompetitorProxyEncryptionError,
    BackgroundJob, Product,
)
from services.competitor_fetch import CompetitorFetchService, WBRateLimitedError
from services.competitor_matching import (
    CompetitorMatchingError,
    queue_group_matching,
    review_match,
    search_supplier_products,
    serialize_group_matches,
)
from services.competitor_comparison import (
    CompetitorComparisonError,
    build_competitor_comparison,
)
from services.competitor_monitor import normalize_sync_interval_minutes

logger = logging.getLogger('competitor_routes')

MAX_NM_IDS_PER_REQUEST = 300
WB_BUSY_ERROR = 'WB ограничивает запросы, повторите позже'
WB_BUSY_CODE = 'wb_rate_limited'


def _validate_nm_ids(raw):
    """Строгий список уникальных positive int. Ошибка -> (None, 'текст')."""
    if not isinstance(raw, list) or not raw:
        return None, 'nm_ids должен быть непустым списком целых чисел'
    if len(raw) > MAX_NM_IDS_PER_REQUEST:
        return None, f'Не больше {MAX_NM_IDS_PER_REQUEST} товаров за раз'
    seen = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            return None, f'Недопустимый nm_id: {item!r}'
        if item in seen:
            return None, f'Дубликат nm_id: {item}'
        seen.append(item)
    return seen, None


def register_competitor_routes(app):
    """Регистрация роутов мониторинга конкурентов"""

    def _get_seller():
        """Получить текущего селлера или None"""
        if current_user.seller:
            return current_user.seller
        return None

    def _get_or_create_settings(seller_id):
        """Получить или создать настройки мониторинга"""
        settings = CompetitorMonitorSettings.query.filter_by(
            seller_id=seller_id).first()
        if not settings:
            settings = CompetitorMonitorSettings(seller_id=seller_id)
            db.session.add(settings)
            db.session.commit()
        return settings

    def _interactive_fetch_service(seller_id):
        """Fetch для UI с seller proxy, но без записи настроек из GET."""
        settings = CompetitorMonitorSettings.query.filter_by(
            seller_id=seller_id).first()
        return CompetitorFetchService(
            proxy_url=settings.proxy_url if settings else None)

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
        groups = CompetitorGroup.query.filter_by(
            seller_id=seller.id, is_active=True).all()

        total_products = CompetitorProduct.query.filter_by(
            seller_id=seller.id, is_active=True
        ).count()
        unread_alerts = CompetitorAlert.query.filter_by(
            seller_id=seller.id, is_read=False
        ).count()

        recent_alerts = CompetitorAlert.query.filter_by(
            seller_id=seller.id
        ).order_by(CompetitorAlert.created_at.desc()).limit(10).all()

        recent_alerts_data = [a.to_dict() for a in recent_alerts]

        return render_template('competitors_dashboard.html',
                               settings=settings,
                               groups=groups,
                               total_products=total_products,
                               unread_alerts=unread_alerts,
                               recent_alerts=recent_alerts,
                               recent_alerts_data=recent_alerts_data)

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

        groups_data = [g.to_dict() for g in groups]

        return render_template('competitors_groups.html',
                               groups=groups, groups_data=groups_data)

    @app.route('/competitors/comparison')
    @login_required
    def competitors_comparison():
        """Общее сравнение exact-match товаров всех конкурентов с нами."""
        seller = _get_seller()
        if not seller:
            return redirect(url_for('dashboard'))
        return render_template('competitors_comparison.html')

    @app.route('/competitors/groups/<int:group_id>')
    @login_required
    def competitors_group_detail(group_id):
        """Детали группы конкурентов"""
        seller = _get_seller()
        if not seller:
            return redirect(url_for('dashboard'))

        group = CompetitorGroup.query.filter_by(
            id=group_id, seller_id=seller.id).first_or_404()
        products = CompetitorProduct.query.filter_by(
            seller_id=seller.id, group_id=group_id, is_active=True
        ).order_by(CompetitorProduct.current_sale_price.asc().nullslast()).all()

        products_data = [p.to_dict() for p in products]

        return render_template('competitors_group_detail.html',
                               group=group, products=products,
                               products_data=products_data)

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
                               current_type=alert_type,
                               current_severity=severity)

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

        if request.method == 'GET':
            settings = _get_or_create_settings(seller.id)
            return jsonify(settings.to_dict())

        data = request.get_json()
        if not isinstance(data, dict) or not data:
            return jsonify({'error': 'Нет данных'}), 400

        max_products = None
        if 'max_products' in data:
            value = data['max_products']
            if type(value) is not int or not 1 <= value <= 1000:
                return jsonify({
                    'error': 'Лимит обработки за цикл должен быть целым числом от 1 до 1000.'
                }), 400
            max_products = value

        settings = _get_or_create_settings(seller.id)

        if 'is_enabled' in data:
            enabling = bool(data['is_enabled']) and not settings.is_enabled
            settings.is_enabled = bool(data['is_enabled'])
            if enabling:
                settings.next_sync_due_at = datetime.utcnow()
        if 'sync_interval_minutes' in data:
            settings.sync_interval_minutes = normalize_sync_interval_minutes(
                data['sync_interval_minutes'])
        if 'price_change_alert_percent' in data:
            settings.price_change_alert_percent = max(
                0.1, min(90.0, float(data['price_change_alert_percent'])))
        if 'discount_alert_pp' in data:
            settings.discount_alert_pp = max(
                1.0, min(50.0, float(data['discount_alert_pp'])))
        if 'max_products' in data:
            settings.max_products = max_products
        if 'proxy_url' in data:
            try:
                settings.proxy_url = (data['proxy_url'] or '').strip() or None
            except CompetitorProxyEncryptionError as e:
                db.session.rollback()
                return jsonify({'error': str(e)}), 400

        db.session.commit()
        return jsonify(settings.to_dict())

    @app.route('/api/competitors/groups', methods=['GET', 'POST'])
    @login_required
    def api_competitors_groups():
        """GET: список групп; POST: создать группу"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        if request.method == 'GET':
            groups = CompetitorGroup.query.filter_by(
                seller_id=seller.id).order_by(
                CompetitorGroup.created_at.desc()
            ).all()
            return jsonify([g.to_dict() for g in groups])

        data = request.get_json()
        if not data or not data.get('name'):
            return jsonify({'error': 'Укажите название группы'}), 400

        own_product_id = data.get('own_product_id')
        if own_product_id is not None:
            if isinstance(own_product_id, bool) \
                    or not isinstance(own_product_id, int):
                return jsonify({'error': 'Некорректный own_product_id'}), 400
            own = Product.query.filter_by(
                id=own_product_id, seller_id=seller.id).first()
            if not own:
                return jsonify({'error': 'Товар не найден'}), 400

        group = CompetitorGroup(
            seller_id=seller.id,
            name=data['name'],
            description=data.get('description', ''),
            color=data.get('color', '#3B82F6'),
            own_product_id=own_product_id,
        )
        db.session.add(group)
        db.session.commit()

        return jsonify(group.to_dict()), 201

    @app.route('/api/competitors/groups/<int:group_id>',
               methods=['PUT', 'DELETE'])
    @login_required
    def api_competitors_group(group_id):
        """PUT: обновить группу; DELETE: удалить"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        group = CompetitorGroup.query.filter_by(
            id=group_id, seller_id=seller.id).first()
        if not group:
            return jsonify({'error': 'Группа не найдена'}), 404

        if request.method == 'DELETE':
            db.session.delete(group)
            db.session.commit()
            return jsonify({'success': True})

        data = request.get_json() or {}
        if data.get('name'):
            group.name = data['name']
        if 'description' in data:
            group.description = data['description']
        if 'color' in data:
            group.color = data['color']
        if 'own_product_id' in data:
            opid = data['own_product_id']
            if opid is not None:
                if isinstance(opid, bool) or not isinstance(opid, int):
                    return jsonify({'error': 'Некорректный own_product_id'}), 400
                own = Product.query.filter_by(
                    id=opid, seller_id=seller.id).first()
                if not own:
                    return jsonify({'error': 'Товар не найден'}), 400
            group.own_product_id = opid
        if 'is_active' in data:
            group.is_active = bool(data['is_active'])

        db.session.commit()
        return jsonify(group.to_dict())

    @app.route('/api/competitors/groups/<int:group_id>/import',
               methods=['DELETE'])
    @login_required
    def api_competitors_cancel_group_import(group_id):
        """Снять durable-заявку импорта, не удаляя уже добавленные товары."""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        group = CompetitorGroup.query.filter_by(
            id=group_id, seller_id=seller.id).first()
        if not group:
            return jsonify({'error': 'Группа не найдена'}), 404

        group.import_requested = False
        db.session.commit()
        return jsonify({'success': True, 'import_requested': False})

    @app.route('/api/competitors/products', methods=['POST'])
    @login_required
    def api_competitors_add_products():
        """
        Добавить товары конкурентов. Никаких WB-вызовов в запросе.

        Body (один из режимов):
        - group_id + nm_ids: [int] — вставка строк; данные подтянет sync
        - group_id + wb_supplier_id: int — заявка на импорт каталога продавца
        """
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        data = request.get_json() or {}
        group = CompetitorGroup.query.filter_by(
            id=data.get('group_id'), seller_id=seller.id).first()
        if not group:
            return jsonify({'error': 'Группа не найдена'}), 404

        settings = _get_or_create_settings(seller.id)

        # Режим 2: заявка на импорт каталога продавца (fetch делает scheduler)
        if data.get('wb_supplier_id') is not None:
            supplier_id = data['wb_supplier_id']
            if isinstance(supplier_id, bool) \
                    or not isinstance(supplier_id, int) or supplier_id <= 0:
                return jsonify({'error': 'Некорректный wb_supplier_id'}), 400
            group.auto_source = 'seller'
            group.auto_source_value = str(supplier_id)
            group.import_requested = True
            settings.next_sync_due_at = datetime.utcnow()
            db.session.commit()
            return jsonify({'success': True, 'added': 0, 'reactivated': 0,
                            'skipped': 0, 'scheduled': True,
                            'import_requested': True,
                            'wb_supplier_id': supplier_id})

        # Режим 1: точные nm_ids — только вставка, без WB
        nm_ids, err = _validate_nm_ids(data.get('nm_ids'))
        if err:
            return jsonify({'error': err}), 400

        added = reactivated = skipped = 0
        requested_products = []
        for nm_id in nm_ids:
            existing = CompetitorProduct.query.filter_by(
                seller_id=seller.id, nm_id=nm_id, group_id=group.id).first()
            if existing:
                requested_products.append(existing)
                if existing.is_active:
                    skipped += 1
                else:
                    existing.is_active = True
                    existing.fetch_error_count = 0
                    existing.price_miss_count = 0
                    reactivated += 1
                continue
            product = CompetitorProduct(
                seller_id=seller.id, group_id=group.id, nm_id=nm_id)
            db.session.add(product)
            requested_products.append(product)
            added += 1

        settings.next_sync_due_at = datetime.utcnow()
        # Сериализуем bounded-набор до commit, чтобы expire-on-commit не
        # превратил ответ picker-а в сотни повторных SELECT.
        db.session.flush()
        requested_products_data = [p.to_dict() for p in requested_products]
        db.session.commit()
        return jsonify({'success': True, 'added': added,
                        'reactivated': reactivated, 'skipped': skipped,
                        'scheduled': True,
                        # Bounded тем же cap=300. UI может сразу показать
                        # честные pending-строки, не делая WB-вызов в POST.
                        'products': requested_products_data})

    @app.route('/api/competitors/products/<int:product_id>',
               methods=['DELETE'])
    @login_required
    def api_competitors_remove_product(product_id):
        """Удалить товар из мониторинга"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        product = CompetitorProduct.query.filter_by(
            id=product_id, seller_id=seller.id).first()
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

        product = CompetitorProduct.query.filter_by(
            id=product_id, seller_id=seller.id).first()
        if not product:
            return jsonify({'error': 'Товар не найден'}), 404

        period = request.args.get('period', '30d')
        days_map = {'7d': 7, '30d': 30, '90d': 90, '1y': 365}
        days = days_map.get(period, 30)

        cutoff = datetime.utcnow() - timedelta(days=days)
        snapshots = CompetitorPriceSnapshot.query.filter(
            CompetitorPriceSnapshot.product_id == product_id,
            CompetitorPriceSnapshot.created_at >= cutoff
        ).order_by(CompetitorPriceSnapshot.created_at.asc()).all()

        return jsonify({
            'product': product.to_dict(),
            'history': [s.to_dict() for s in snapshots],
        })

    @app.route('/api/competitors/groups/<int:group_id>/matches')
    @login_required
    def api_competitors_group_matches(group_id):
        """Shared AI result plus tenant-scoped review and exact own price."""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403
        try:
            return jsonify(serialize_group_matches(seller.id, group_id))
        except CompetitorMatchingError as error:
            return jsonify({'error': str(error)}), 404

    @app.route('/api/competitors/comparison')
    @login_required
    def api_competitors_comparison():
        """Bounded all-vs-us/one-vs-us read model; no provider calls."""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        def query_int(name, default, *, minimum=1, maximum=None):
            raw = request.args.get(name)
            if raw in (None, ''):
                return default
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise CompetitorComparisonError(
                    f'Некорректный параметр {name}',
                )
            if value < minimum or (maximum is not None and value > maximum):
                raise CompetitorComparisonError(
                    f'Некорректный параметр {name}',
                )
            return value

        try:
            group_id = query_int('group_id', None)
            page = query_int('page', 1)
            per_page = query_int('per_page', 24, maximum=50)
            payload = build_competitor_comparison(
                seller.id,
                group_id=group_id,
                query=request.args.get('q', ''),
                scope=request.args.get('scope', 'all'),
                sort=request.args.get('sort', 'coverage'),
                page=page,
                per_page=per_page,
            )
        except CompetitorComparisonError as error:
            message = str(error)
            status = 404 if message == 'Конкурент не найден' else 400
            return jsonify({'error': message}), status
        return jsonify(payload)

    @app.route('/api/competitors/groups/<int:group_id>/matches/run',
               methods=['POST'])
    @login_required
    def api_competitors_run_group_matching(group_id):
        """Enqueue a bounded job; image/LLM calls never run in HTTP."""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403
        try:
            job = queue_group_matching(seller.id, group_id)
        except CompetitorMatchingError as error:
            return jsonify({'error': str(error)}), 400
        return jsonify({
            'success': True,
            'job': job.to_dict(),
            'shared_cache': True,
            'source_scope': 'supplier_observed_only',
        }), 202

    @app.route('/api/competitors/matches/jobs/<string:job_uid>')
    @login_required
    def api_competitors_matching_job(job_uid):
        """Read only the current seller's matching job."""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403
        job = BackgroundJob.query.filter_by(
            job_uid=job_uid,
            seller_id=seller.id,
            job_type='competitor_matching',
        ).first()
        if not job:
            return jsonify({'error': 'Задача не найдена'}), 404
        return jsonify(job.to_dict())

    @app.route('/api/competitors/matches/<int:match_id>/review',
               methods=['PUT'])
    @login_required
    def api_competitors_review_match(match_id):
        """Write a seller-local decision without changing the shared match."""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403
        data = request.get_json() or {}
        try:
            review = review_match(
                seller.id,
                getattr(current_user, 'id', None),
                match_id,
                action=data.get('action'),
                supplier_product_id=data.get('supplier_product_id'),
                match_type=data.get('match_type'),
            )
        except CompetitorMatchingError as error:
            message = str(error)
            status = 404 if message == 'Сопоставление не найдено' else 400
            return jsonify({'error': message}), status
        return jsonify({
            'success': True,
            'review': ({
                'status': review.status,
                'match_type': review.match_type,
                'supplier_product_id': review.supplier_product_id,
                'reviewed_at': review.reviewed_at.isoformat(),
            } if review else None),
            'shared_suggestion_changed': False,
        })

    @app.route('/api/competitors/supplier-products/search')
    @login_required
    def api_competitors_supplier_product_search():
        """Bounded observed-only search for a seller-local manual override."""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403
        query = request.args.get('q', '').strip()
        if len(query) < 2:
            return jsonify({'error': 'Введите минимум 2 символа'}), 400
        return jsonify({
            'items': search_supplier_products(query, limit=20),
            'source_scope': 'supplier_observed_only',
        })

    @app.route('/api/competitors/search')
    @login_required
    def api_competitors_search():
        """Поиск товаров на WB: одна страница, bounded."""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        query = request.args.get('q', '').strip()
        if not query:
            return jsonify({'error': 'Укажите поисковый запрос'}), 400

        try:
            service = _interactive_fetch_service(seller.id)
            results = service.search_products(query, limit=50)
        except WBRateLimitedError:
            return jsonify({
                'error': WB_BUSY_ERROR,
                'code': WB_BUSY_CODE,
                'retryable': True,
            }), 503

        return jsonify(results)

    @app.route('/api/competitors/seller-catalog')
    @login_required
    def api_competitors_seller_catalog():
        """Превью каталога продавца WB: одна страница, bounded."""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        wb_supplier_id = request.args.get('supplier_id', type=int)
        if not wb_supplier_id:
            return jsonify({'error': 'Укажите supplier_id'}), 400
        page = request.args.get('page', 1, type=int)

        try:
            service = _interactive_fetch_service(seller.id)
            results = service.fetch_seller_catalog_page(
                wb_supplier_id, page=max(1, page))
        except WBRateLimitedError:
            return jsonify({
                'error': WB_BUSY_ERROR,
                'code': WB_BUSY_CODE,
                'retryable': True,
            }), 503

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
        """Данные для дашборда (агрегаты одним SQL, без N+1)"""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        from sqlalchemy import func

        settings = _get_or_create_settings(seller.id)
        groups = CompetitorGroup.query.filter_by(
            seller_id=seller.id, is_active=True).all()

        agg = {}
        rows = db.session.query(
            CompetitorProduct.group_id,
            func.count(CompetitorProduct.id),
            func.min(CompetitorProduct.current_sale_price),
            func.avg(CompetitorProduct.current_sale_price),
            func.max(CompetitorProduct.current_sale_price),
        ).filter(
            CompetitorProduct.seller_id == seller.id,
            CompetitorProduct.is_active.is_(True),
        ).group_by(CompetitorProduct.group_id).all()
        for group_id, cnt, mn, avg, mx in rows:
            agg[group_id] = {'products_count': cnt, 'min_price': mn,
                             'avg_price': round(avg) if avg else None,
                             'max_price': mx}

        groups_data = [{**g.to_dict(), **agg.get(g.id, {
            'products_count': 0, 'min_price': None,
            'avg_price': None, 'max_price': None})} for g in groups]

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

        group = CompetitorGroup.query.filter_by(
            id=group_id, seller_id=seller.id).first()
        if not group:
            return jsonify({'error': 'Группа не найдена'}), 404

        competitors = [p.to_dict() for p in CompetitorProduct.query.filter_by(
            group_id=group_id, is_active=True).all()]

        own_product = None
        if group.own_product_id and group.own_product:
            own = group.own_product
            own_price = float(own.discount_price or own.price or 0) or None
            comp_prices = sorted(
                p['current_sale_price'] for p in competitors
                if p.get('current_sale_price'))
            position = None
            vs_min = None
            median = None
            if own_price and comp_prices:
                position = 1 + sum(1 for c in comp_prices if c < own_price)
                vs_min = round(
                    (own_price - comp_prices[0]) / comp_prices[0] * 100, 1)
                mid = len(comp_prices) // 2
                median = (comp_prices[mid] if len(comp_prices) % 2
                          else round((comp_prices[mid - 1]
                                      + comp_prices[mid]) / 2))
            own_product = {
                'nm_id': own.nm_id,
                'title': own.title,
                'price': float(own.price) if own.price else None,
                'discount_price': (float(own.discount_price)
                                   if own.discount_price else None),
                'position': position,
                'total_with_own': (len(comp_prices) + 1) if comp_prices else None,
                'vs_min_percent': vs_min,
                'median_competitor_price': median,
            }

        return jsonify({
            'group': group.to_dict(),
            'own_product': own_product,
            'competitors': competitors,
        })

    @app.route('/api/competitors/sync', methods=['POST'])
    @login_required
    def api_competitors_force_sync():
        """Запросить синхронизацию: выполнит scheduler в течение минуты."""
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403

        settings = _get_or_create_settings(seller.id)
        # Планировщик пропускает выключенных продавцов, поэтому обещать запуск
        # при выключенном мониторинге — значит врать: он никогда не случится.
        if not settings.is_enabled:
            return jsonify({
                'success': False,
                'scheduled': False,
                'code': 'monitoring_disabled',
                'message': (
                    'Мониторинг конкурентов выключен — включите его в '
                    'настройках, тогда данные начнут обновляться'
                ),
            }), 409
        settings.next_sync_due_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'success': True, 'scheduled': True,
                        'message': 'Синхронизация запустится в течение минуты'})
