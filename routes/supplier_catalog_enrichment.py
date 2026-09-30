# -*- coding: utf-8 -*-
"""Admin UI and HTTP boundary for shared supplier catalog enrichment."""

import json
import math
from functools import wraps

from flask import (
    abort, current_app, flash, jsonify, redirect, render_template, request,
    url_for,
)
from flask_login import current_user, login_required

from models import (
    Supplier,
    SupplierCatalogEnrichmentItem,
    SupplierCatalogEnrichmentRun,
    SupplierProduct,
    db,
    log_admin_action,
)
from services.supplier_catalog_enrichment import (
    MAX_CHARACTERISTIC_SELECTION,
    MAX_SELECTION,
    SupplierCatalogEnrichmentError,
    SupplierCatalogEnrichmentService,
)


def _admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _supplier_or_404(supplier_id):
    supplier = db.session.get(Supplier, supplier_id)
    if not supplier:
        abort(404)
    return supplier


def _filters(source):
    return {
        'search': (source.get('search') or '').strip()[:200],
        'wb_category': (source.get('wb_category') or '').strip()[:300],
        'source_category': (source.get('source_category') or '').strip()[:300],
        'stock_status': (source.get('stock_status') or '').strip(),
        'mapping_state': (source.get('mapping_state') or '').strip(),
    }


def _positive_form_ids(values):
    prepared = []
    for value in values:
        if (not isinstance(value, str) or len(value) > 20 or
                not value.isdigit() or int(value) <= 0):
            raise SupplierCatalogEnrichmentError(
                'invalid_product_ids', 'Некорректный ID товара.',
            )
        prepared.append(int(value))
    return prepared


_LEGACY_FILTER_KEYS = (
    'search', 'stock_status', 'parse_status', 'fill_max', 'category',
    'brand', 'ai_model', 'has_description', 'price_min', 'price_max',
    'limit',
)


def _legacy_filters(source):
    """Preserve the old parser's selection predicates, not its completion claim."""
    filters = {}
    for key in _LEGACY_FILTER_KEYS:
        value = (source.get(f'legacy_{key}') or '').strip()
        if len(value) > 300:
            raise SupplierCatalogEnrichmentError(
                'filter_too_long',
                'Фильтр слишком длинный; сократите значение до 300 символов.',
            )
        filters[key] = value
    return filters


def _legacy_filter_query(supplier_id, filters):
    query = SupplierProduct.query.filter_by(supplier_id=supplier_id)
    search = filters['search']
    if search:
        term = f'%{search}%'
        query = query.filter(db.or_(
            SupplierProduct.title.ilike(term),
            SupplierProduct.external_id.ilike(term),
            SupplierProduct.brand.ilike(term),
        ))
    if filters['stock_status'] == 'in_stock':
        query = query.filter(SupplierProduct.supplier_status == 'in_stock')
    elif filters['stock_status'] == 'out_of_stock':
        query = query.filter(SupplierProduct.supplier_status == 'out_of_stock')
    if filters['parse_status'] == 'not_parsed':
        query = query.filter(SupplierProduct.ai_parsed_data_json.is_(None))
    elif filters['parse_status'] == 'parsed':
        query = query.filter(SupplierProduct.ai_parsed_data_json.isnot(None))
    elif filters['parse_status'] == 'fill_below' and filters['fill_max']:
        try:
            threshold = float(filters['fill_max'])
        except ValueError:
            threshold = None
        if threshold is not None and math.isfinite(threshold):
            query = query.filter(db.or_(
                SupplierProduct.ai_fill_pct.is_(None),
                SupplierProduct.ai_fill_pct < threshold,
            ))
    if filters['category']:
        term = f"%{filters['category']}%"
        query = query.filter(db.or_(
            SupplierProduct.wb_category_name.ilike(term),
            SupplierProduct.category.ilike(term),
        ))
    if filters['brand']:
        query = query.filter(SupplierProduct.brand.ilike(f"%{filters['brand']}%"))
    if filters['ai_model']:
        query = query.filter(SupplierProduct.ai_model_used.ilike(
            f"%{filters['ai_model']}%"
        ))
    if filters['has_description'] == 'yes':
        query = query.filter(
            SupplierProduct.description.isnot(None),
            SupplierProduct.description != '',
        )
    elif filters['has_description'] == 'no':
        query = query.filter(db.or_(
            SupplierProduct.description.is_(None),
            SupplierProduct.description == '',
        ))
    for key, operator in (
        ('price_min', 'min'), ('price_max', 'max'),
    ):
        if not filters[key]:
            continue
        try:
            value = float(filters[key])
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        query = query.filter(
            SupplierProduct.supplier_price >= value if operator == 'min'
            else SupplierProduct.supplier_price <= value
        )
    return query


def _legacy_selection(supplier_id, kind, source):
    """Recompute an exact, bounded handoff at preview and launch time."""
    if kind == 'ids':
        raw = (source.get('product_ids') or '').split(',')
        if len(raw) > MAX_CHARACTERISTIC_SELECTION:
            raise SupplierCatalogEnrichmentError(
                'selection_too_large',
                'Выбрано больше 5000 товаров. Разделите выбор.',
            )
        ids = _positive_form_ids(raw)
        if len(set(ids)) != len(ids):
            raise SupplierCatalogEnrichmentError(
                'duplicate_product_ids', 'В выборе есть повторяющиеся товары.',
            )
        owned_count = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.id.in_(ids),
        ).count()
        if owned_count != len(ids):
            raise SupplierCatalogEnrichmentError(
                'product_scope_mismatch',
                'Часть товаров больше не принадлежит выбранному поставщику.',
            )
        return ids, {}, len(ids)
    if kind != 'filter':
        raise SupplierCatalogEnrichmentError(
            'invalid_selection_scope', 'Неизвестный способ выбора.',
        )
    filters = _legacy_filters(source)
    raw_limit = filters['limit']
    if raw_limit and (not raw_limit.isdigit() or int(raw_limit) < 0):
        raise SupplierCatalogEnrichmentError(
            'invalid_limit', 'Лимит должен быть неотрицательным числом.',
        )
    limit = int(raw_limit or 0)
    query = _legacy_filter_query(supplier_id, filters)
    count = query.count()
    count = min(count, limit) if limit else count
    if count > MAX_CHARACTERISTIC_SELECTION:
        return [], filters, count
    rows = query.with_entities(SupplierProduct.id).order_by(
        SupplierProduct.title.asc(), SupplierProduct.id.asc(),
    )
    if limit:
        rows = rows.limit(limit)
    return [row[0] for row in rows.all()], filters, count


def register_supplier_catalog_enrichment_routes(app):
    @app.route('/admin/suppliers/<int:supplier_id>/catalog-enrichment')
    @login_required
    @_admin_required
    def admin_supplier_catalog_enrichment(supplier_id):
        supplier = _supplier_or_404(supplier_id)
        filters = _filters(request.args)
        page = max(1, request.args.get('page', 1, type=int))
        handoff_page_args = {
            key: value for key, value in request.args.items() if key != 'page'
        }
        per_page = min(max(request.args.get('per_page', 50, type=int), 20), 100)
        handoff_kind = (request.args.get('handoff') or '').strip()
        handoff = None
        if handoff_kind:
            try:
                ids, legacy_filters, count = _legacy_selection(
                    supplier_id, handoff_kind, request.args,
                )
                handoff = {
                    'kind': handoff_kind,
                    'ids': ids,
                    'filters': legacy_filters,
                    'count': count,
                    'error': (
                        'По выбору найдено больше 5000 товаров. Сузьте '
                        'фильтр или разделите запуск.'
                        if count > MAX_CHARACTERISTIC_SELECTION else None
                    ),
                }
            except SupplierCatalogEnrichmentError as exc:
                handoff = {
                    'kind': handoff_kind, 'ids': [], 'filters': {},
                    'count': 0, 'error': str(exc),
                }
            if (handoff['kind'] == 'filter' and handoff['error']
                    and handoff['filters'] and not handoff['ids']):
                preview_ids = [row[0] for row in _legacy_filter_query(
                    supplier_id, handoff['filters'],
                ).with_entities(SupplierProduct.id).order_by(
                    SupplierProduct.title.asc(), SupplierProduct.id.asc(),
                ).limit(100).all()]
                query = SupplierProduct.query.filter(
                    SupplierProduct.supplier_id == supplier_id,
                    SupplierProduct.id.in_(preview_ids),
                )
            else:
                query = SupplierProduct.query.filter(
                    SupplierProduct.supplier_id == supplier_id,
                    SupplierProduct.id.in_(handoff['ids']),
                )
        else:
            query = SupplierCatalogEnrichmentService.selection_query(
                supplier_id, filters,
            )
        pagination = query.order_by(
            SupplierProduct.id.asc(),
        ).paginate(page=page, per_page=per_page, error_out=False)

        category_rows = db.session.query(
            SupplierProduct.wb_category_name,
            db.func.count(SupplierProduct.id),
        ).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.wb_category_name.isnot(None),
            SupplierProduct.wb_category_name != '',
        ).group_by(
            SupplierProduct.wb_category_name,
        ).order_by(
            db.func.count(SupplierProduct.id).desc(),
            SupplierProduct.wb_category_name.asc(),
        ).limit(250).all()
        source_category_rows = db.session.query(
            SupplierProduct.category,
            db.func.count(SupplierProduct.id),
        ).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.category.isnot(None),
            SupplierProduct.category != '',
        ).group_by(SupplierProduct.category).order_by(
            db.func.count(SupplierProduct.id).desc(),
            SupplierProduct.category.asc(),
        ).limit(250).all()

        recent_runs = SupplierCatalogEnrichmentRun.query.filter_by(
            supplier_id=supplier_id,
        ).order_by(
            SupplierCatalogEnrichmentRun.created_at.desc(),
        ).limit(8).all()
        active_run = next((
            run for run in recent_runs
            if run.status in ('pending', 'running', 'cancelling')
        ), None)
        invalid_count = SupplierCatalogEnrichmentService.selection_query(
            supplier_id, {'mapping_state': 'invalid'},
        ).count()
        adult_parent_count = SupplierCatalogEnrichmentService.selection_query(
            supplier_id, {'wb_category': 'Товары для взрослых'},
        ).count()
        return render_template(
            'admin_supplier_catalog_enrichment.html',
            supplier=supplier,
            pagination=pagination,
            filters=filters,
            category_rows=category_rows,
            source_category_rows=source_category_rows,
            recent_runs=recent_runs,
            active_run=active_run,
            reference_status=SupplierCatalogEnrichmentService.reference_status(),
            invalid_count=invalid_count,
            adult_parent_count=adult_parent_count,
            max_selection=MAX_SELECTION,
            max_characteristic_selection=MAX_CHARACTERISTIC_SELECTION,
            handoff=handoff,
            handoff_page_args=handoff_page_args,
        )

    @app.route(
        '/admin/suppliers/<int:supplier_id>/catalog-enrichment/runs',
        methods=['POST'],
    )
    @login_required
    @_admin_required
    def admin_supplier_catalog_enrichment_create(supplier_id):
        supplier = _supplier_or_404(supplier_id)
        mode = (request.form.get('mode') or 'category_only').strip()
        scope = (request.form.get('selection_scope') or 'selected').strip()
        filters = _filters(request.form)
        try:
            if scope == 'handoff':
                kind = (request.form.get('handoff') or '').strip()
                product_ids, legacy_filters, count = _legacy_selection(
                    supplier_id, kind, request.form,
                )
                if count > MAX_CHARACTERISTIC_SELECTION:
                    raise SupplierCatalogEnrichmentError(
                        'selection_too_large',
                        'По выбору найдено больше 5000 товаров. Сузьте фильтр.',
                    )
                if mode not in (
                    'category_and_characteristics', 'characteristics_inference',
                ):
                    raise SupplierCatalogEnrichmentError(
                        'invalid_mode', 'Выберите режим обогащения характеристик.',
                    )
            elif scope == 'filtered':
                limit = (
                    MAX_CHARACTERISTIC_SELECTION
                    if mode in (
                        'category_and_characteristics', 'characteristics_inference',
                    )
                    else MAX_SELECTION
                )
                rows = SupplierCatalogEnrichmentService.selection_query(
                    supplier_id, filters,
                ).with_entities(SupplierProduct.id).order_by(
                    SupplierProduct.id.asc(),
                ).limit(limit + 1).all()
                if len(rows) > limit:
                    raise SupplierCatalogEnrichmentError(
                        'selection_too_large',
                        f'По фильтру найдено больше {limit} товаров. '
                        'Сузьте фильтр и повторите.',
                    )
                product_ids = [row[0] for row in rows]
            elif scope == 'selected':
                product_ids = _positive_form_ids(
                    request.form.getlist('product_ids')
                )
            else:
                raise SupplierCatalogEnrichmentError(
                    'invalid_selection_scope', 'Неизвестный способ выбора.',
                )
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=supplier_id,
                admin_user_id=current_user.id,
                product_ids=product_ids,
                mode=mode,
                selection={
                    'scope': scope,
                    'filters': legacy_filters if scope == 'handoff' else filters,
                },
            )
        except SupplierCatalogEnrichmentError as exc:
            flash(str(exc), 'danger')
            if scope == 'handoff':
                return redirect(url_for(
                    'admin_supplier_catalog_enrichment',
                    supplier_id=supplier_id,
                    **{key: value for key, value in request.form.items()
                       if key == 'handoff' or key == 'product_ids'
                       or key.startswith('legacy_')},
                ))
            return redirect(url_for(
                'admin_supplier_catalog_enrichment',
                supplier_id=supplier_id,
                **{key: value for key, value in filters.items() if value},
            ))

        log_admin_action(
            admin_user_id=current_user.id,
            action='create_supplier_catalog_enrichment_run',
            target_type='supplier',
            target_id=supplier_id,
            details={
                'run_id': run.id,
                'mode': run.mode,
                'selection_scope': scope,
                'product_count': run.total,
                'llm_call_limit': run.llm_call_limit,
            },
            request=request,
        )
        SupplierCatalogEnrichmentService.kick(
            current_app._get_current_object(), run.id,
        )
        flash(
            f'Массовое обогащение запущено: {run.total} товаров.', 'success',
        )
        return redirect(url_for(
            'admin_supplier_catalog_enrichment_run',
            supplier_id=supplier_id,
            run_id=run.id,
        ))

    @app.route(
        '/admin/suppliers/<int:supplier_id>/catalog-enrichment/runs/<run_id>'
    )
    @login_required
    @_admin_required
    def admin_supplier_catalog_enrichment_run(supplier_id, run_id):
        supplier = _supplier_or_404(supplier_id)
        run = SupplierCatalogEnrichmentRun.query.filter_by(
            id=run_id, supplier_id=supplier_id,
        ).first_or_404()
        status = (request.args.get('status') or '').strip()
        page = max(1, request.args.get('page', 1, type=int))
        query = SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id)
        if status:
            query = query.filter_by(status=status)
        pagination = query.order_by(
            SupplierCatalogEnrichmentItem.ordinal.asc(),
        ).paginate(page=page, per_page=50, error_out=False)
        inference_by_item = {}
        for row in pagination.items:
            if not row.inference_json:
                continue
            try:
                parsed = json.loads(row.inference_json)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, list):
                inference_by_item[row.id] = [
                    s for s in parsed
                    if isinstance(s, dict) and s.get('name')
                ]
        return render_template(
            'admin_supplier_catalog_enrichment_run.html',
            supplier=supplier,
            run=run,
            run_data=SupplierCatalogEnrichmentService.serialize_run(run),
            pagination=pagination,
            current_status=status,
            inference_by_item=inference_by_item,
        )

    @app.route(
        '/admin/suppliers/<int:supplier_id>/catalog-enrichment/runs/'
        '<run_id>/status'
    )
    @login_required
    @_admin_required
    def admin_supplier_catalog_enrichment_status(supplier_id, run_id):
        run = SupplierCatalogEnrichmentRun.query.filter_by(
            id=run_id, supplier_id=supplier_id,
        ).first_or_404()
        return jsonify(SupplierCatalogEnrichmentService.serialize_run(run))

    @app.route(
        '/admin/suppliers/<int:supplier_id>/catalog-enrichment/runs/'
        '<run_id>/cancel', methods=['POST'],
    )
    @login_required
    @_admin_required
    def admin_supplier_catalog_enrichment_cancel(supplier_id, run_id):
        _supplier_or_404(supplier_id)
        if SupplierCatalogEnrichmentService.request_cancel(run_id, supplier_id):
            log_admin_action(
                admin_user_id=current_user.id,
                action='cancel_supplier_catalog_enrichment_run',
                target_type='supplier',
                target_id=supplier_id,
                details={'run_id': run_id},
                request=request,
            )
            flash('Остановка запрошена. Уже применённые строки сохранены.', 'success')
        else:
            flash('Запуск уже завершён.', 'warning')
        return redirect(url_for(
            'admin_supplier_catalog_enrichment_run',
            supplier_id=supplier_id,
            run_id=run_id,
        ))

    @app.route(
        '/admin/suppliers/<int:supplier_id>/catalog-enrichment/categories/search'
    )
    @login_required
    @_admin_required
    def admin_supplier_catalog_enrichment_category_search(supplier_id):
        _supplier_or_404(supplier_id)
        query = (request.args.get('q') or '').strip()
        if len(query) < 2 or len(query) > 120:
            return jsonify({'error': 'Введите от 2 до 120 символов.'}), 400
        try:
            categories = (
                SupplierCatalogEnrichmentService.search_reference_categories(
                    query, limit=20,
                )
            )
        except SupplierCatalogEnrichmentError as exc:
            return jsonify({'error': str(exc), 'code': exc.code}), 409
        return jsonify({'categories': categories})

    @app.route(
        '/admin/suppliers/<int:supplier_id>/catalog-enrichment/items/'
        '<int:item_id>/category', methods=['POST'],
    )
    @login_required
    @_admin_required
    def admin_supplier_catalog_enrichment_apply_category(
        supplier_id, item_id,
    ):
        _supplier_or_404(supplier_id)
        subject_id = request.form.get('subject_id', type=int)
        run_id = (request.form.get('run_id') or '').strip()
        try:
            item = SupplierCatalogEnrichmentService.apply_review_category(
                item_id=item_id,
                supplier_id=supplier_id,
                subject_id=subject_id,
            )
        except SupplierCatalogEnrichmentError as exc:
            flash(str(exc), 'danger')
        else:
            run_id = item.run_id
            log_admin_action(
                admin_user_id=current_user.id,
                action='review_supplier_catalog_category',
                target_type='supplier_product',
                target_id=item.supplier_product_id,
                details={
                    'run_id': item.run_id,
                    'subject_id': subject_id,
                },
                request=request,
            )
            if item.status == 'pending':
                SupplierCatalogEnrichmentService.kick(
                    current_app._get_current_object(), item.run_id,
                )
            flash('Конечная категория подтверждена.', 'success')
        if not run_id:
            return redirect(url_for(
                'admin_supplier_catalog_enrichment', supplier_id=supplier_id,
            ))
        return redirect(url_for(
            'admin_supplier_catalog_enrichment_run',
            supplier_id=supplier_id,
            run_id=run_id,
            status='needs_review',
        ))

    @app.route(
        '/admin/suppliers/<int:supplier_id>/catalog-enrichment/runs/'
        '<run_id>/items/<int:item_id>/apply-inference', methods=['POST'],
    )
    @login_required
    @_admin_required
    def admin_supplier_catalog_enrichment_apply_inference(
        supplier_id, run_id, item_id,
    ):
        _supplier_or_404(supplier_id)
        field_names = [
            name.strip() for name in request.form.getlist('field_names')
            if name and name.strip()
        ]
        try:
            result = SupplierCatalogEnrichmentService.apply_inference_selection(
                run_id=run_id,
                item_id=item_id,
                supplier_id=supplier_id,
                admin_user_id=current_user.id,
                field_names=field_names,
            )
        except SupplierCatalogEnrichmentError as exc:
            flash(str(exc), 'danger')
        else:
            log_admin_action(
                admin_user_id=current_user.id,
                action='apply_supplier_catalog_inference',
                target_type='supplier_catalog_enrichment_item',
                target_id=item_id,
                details={
                    'run_id': run_id,
                    'applied': result['applied'],
                    'skipped': result['skipped'],
                },
                request=request,
            )
            flash(
                f"Применено предположений: {len(result['applied'])}.",
                'success',
            )
        return redirect(url_for(
            'admin_supplier_catalog_enrichment_run',
            supplier_id=supplier_id,
            run_id=run_id,
            status='needs_review',
        ))

    @app.route(
        '/admin/suppliers/<int:supplier_id>/catalog-enrichment/items/'
        '<int:item_id>/rollback', methods=['POST'],
    )
    @login_required
    @_admin_required
    def admin_supplier_catalog_enrichment_rollback(supplier_id, item_id):
        _supplier_or_404(supplier_id)
        run_id = (request.form.get('run_id') or '').strip()
        try:
            item = SupplierCatalogEnrichmentService.rollback_item(
                item_id=item_id, supplier_id=supplier_id,
            )
        except SupplierCatalogEnrichmentError as exc:
            flash(str(exc), 'danger')
        else:
            run_id = item.run_id
            log_admin_action(
                admin_user_id=current_user.id,
                action='rollback_supplier_catalog_enrichment_item',
                target_type='supplier_product',
                target_id=item.supplier_product_id,
                details={'run_id': item.run_id, 'item_id': item.id},
                request=request,
            )
            flash('Изменение центральной карточки отменено.', 'success')
        return redirect(url_for(
            'admin_supplier_catalog_enrichment_run',
            supplier_id=supplier_id,
            run_id=run_id,
        ))
