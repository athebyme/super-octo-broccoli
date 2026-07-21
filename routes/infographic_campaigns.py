# -*- coding: utf-8 -*-
"""Seller-facing bulk infographic campaign UI and bounded JSON actions."""

from __future__ import annotations

from functools import wraps

from flask import jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from models import InfographicCampaign, InfographicCampaignSlide
from services import infographic_campaigns as campaigns


def _seller_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'success': False, 'error': 'Сессия истекла'}), 401
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Нужен профиль продавца'}), 403
        return function(*args, **kwargs)
    return wrapped


def _owned_campaign(campaign_id: int) -> InfographicCampaign:
    return InfographicCampaign.query.filter_by(
        id=campaign_id, seller_id=current_user.seller.id,
    ).first_or_404()


def _page_number() -> int:
    raw = str(request.args.get('page') or '1')
    return min(max(int(raw), 1), 10000) if raw.isascii() and raw.isdigit() else 1


def _form_slide_limit(value) -> int:
    raw = str(value or '6').strip()
    if not raw.isascii() or not raw.isdigit():
        raise campaigns.InfographicCampaignError('Некорректное число слайдов')
    return int(raw)


def _json_error(error: campaigns.InfographicCampaignError, status: int = 400):
    return jsonify({
        'success': False,
        'code': error.code,
        'error': str(error),
    }), status


def register_infographic_campaign_routes(app):
    @app.get('/image-lab/campaigns')
    @login_required
    @_seller_required
    def infographic_campaign_list():
        page = _page_number()
        query = InfographicCampaign.query.filter_by(
            seller_id=current_user.seller.id,
        ).order_by(InfographicCampaign.created_at.desc())
        total = query.count()
        per_page = 50
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
        return render_template(
            'infographic_campaigns.html',
            campaigns=[campaigns.campaign_summary(row) for row in rows],
            page=page,
            pages=max(1, (total + per_page - 1) // per_page),
            total=total,
        )

    @app.route('/image-lab/campaigns/new', methods=['GET', 'POST'])
    @login_required
    @_seller_required
    def infographic_campaign_new():
        try:
            if request.method == 'POST':
                product_ids = campaigns.parse_form_product_ids(
                    request.form.getlist('product_ids'),
                )
            else:
                product_ids = campaigns.parse_form_product_ids(
                    request.args.getlist('product_ids'),
                )
            previews = campaigns.preview_products(
                current_user.seller.id, product_ids,
            )
            return render_template(
                'infographic_campaign_new.html',
                product_ids=product_ids,
                products=previews,
                ready_count=sum(1 for product in previews if product['ready']),
                blocked_count=sum(1 for product in previews if not product['ready']),
                templates=campaigns.template_options(),
                error='',
            )
        except campaigns.InfographicCampaignError as exc:
            empty_get = (
                request.method == 'GET'
                and not request.args.getlist('product_ids')
            )
            return render_template(
                'infographic_campaign_new.html',
                product_ids=[], products=[], ready_count=0, blocked_count=0,
                templates=campaigns.template_options(),
                error='' if empty_get else str(exc),
            ), 200 if empty_get else 400

    @app.post('/image-lab/campaigns')
    @login_required
    @_seller_required
    def infographic_campaign_create():
        is_json = request.is_json
        try:
            if is_json:
                data = request.get_json(silent=True)
                if not isinstance(data, dict):
                    raise campaigns.InfographicCampaignError('Ожидается JSON object')
                unknown = set(data) - {
                    'product_ids', 'template_key', 'slide_limit', 'name',
                }
                if unknown:
                    raise campaigns.InfographicCampaignError(
                        'Неизвестные поля: ' + ', '.join(sorted(unknown)),
                    )
                for field in ('template_key', 'name'):
                    if field in data and not isinstance(data[field], str):
                        raise campaigns.InfographicCampaignError(
                            f'{field} должен быть string',
                        )
                product_ids = data.get('product_ids')
                slide_limit = data.get('slide_limit', 6)
                template_key = str(data.get('template_key') or 'botanical')
                name = str(data.get('name') or '')
            else:
                product_ids = campaigns.parse_form_product_ids(
                    request.form.getlist('product_ids'),
                )
                slide_limit = _form_slide_limit(request.form.get('slide_limit'))
                template_key = str(request.form.get('template_key') or 'botanical')
                name = str(request.form.get('name') or '')
            campaign = campaigns.create_campaign(
                seller_id=current_user.seller.id,
                user_id=current_user.id,
                product_ids=product_ids,
                template_key=template_key,
                slide_limit=slide_limit,
                name=name,
            )
            campaigns.launch_campaign(app, campaign.id)
            if is_json:
                return jsonify({
                    'success': True,
                    'campaign': campaigns.campaign_summary(campaign),
                }), 201
            return redirect(url_for(
                'infographic_campaign_detail', campaign_id=campaign.id,
            ))
        except campaigns.InfographicCampaignError as exc:
            if is_json:
                return _json_error(exc)
            try:
                preview_ids = campaigns.parse_form_product_ids(
                    request.form.getlist('product_ids'),
                )
                previews = campaigns.preview_products(
                    current_user.seller.id, preview_ids,
                )
            except campaigns.InfographicCampaignError:
                preview_ids, previews = [], []
            return render_template(
                'infographic_campaign_new.html',
                product_ids=preview_ids,
                products=previews,
                ready_count=sum(1 for product in previews if product['ready']),
                blocked_count=sum(1 for product in previews if not product['ready']),
                templates=campaigns.template_options(), error=str(exc),
            ), 400

    @app.get('/image-lab/campaigns/<int:campaign_id>')
    @login_required
    @_seller_required
    def infographic_campaign_detail(campaign_id):
        campaign = _owned_campaign(campaign_id)
        campaigns.launch_campaign(app, campaign.id)
        page_data = campaigns.campaign_item_page(
            campaign, page=_page_number(), per_page=40,
        )
        return render_template(
            'infographic_campaign_detail.html',
            campaign=campaigns.campaign_summary(campaign),
            **page_data,
        )

    @app.get('/image-lab/api/campaigns/<int:campaign_id>')
    @login_required
    @_seller_required
    def infographic_campaign_status(campaign_id):
        campaign = _owned_campaign(campaign_id)
        campaigns.launch_campaign(app, campaign.id)
        page_data = campaigns.campaign_item_page(
            campaign, page=_page_number(), per_page=40,
        )
        return jsonify({
            'success': True,
            'campaign': campaigns.campaign_summary(campaign),
            **page_data,
        })

    @app.post('/image-lab/api/campaigns/<int:campaign_id>/review')
    @login_required
    @_seller_required
    def infographic_campaign_review(campaign_id):
        campaign = _owned_campaign(campaign_id)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'success': False, 'error': 'Ожидается JSON object'}), 400
        unknown = set(data) - {'action', 'slide_ids', 'item_ids'}
        if unknown:
            return jsonify({
                'success': False,
                'error': 'Неизвестные поля: ' + ', '.join(sorted(unknown)),
            }), 400
        try:
            changed = campaigns.review_slides(
                campaign,
                seller_id=current_user.seller.id,
                user_id=current_user.id,
                action=str(data.get('action') or ''),
                slide_ids=data.get('slide_ids'),
                item_ids=data.get('item_ids'),
            )
            return jsonify({
                'success': True,
                'reviewed': changed,
                'campaign': campaigns.campaign_summary(campaign),
            })
        except campaigns.InfographicCampaignError as exc:
            return _json_error(exc, 409 if exc.code == 'quality_rejected' else 400)

    @app.post('/image-lab/api/campaigns/<int:campaign_id>/cancel')
    @login_required
    @_seller_required
    def infographic_campaign_cancel(campaign_id):
        campaign = _owned_campaign(campaign_id)
        try:
            cancelled = campaigns.cancel_campaign(
                campaign, seller_id=current_user.seller.id,
            )
            return jsonify({
                'success': True,
                'cancelled_items': cancelled,
                'campaign': campaigns.campaign_summary(campaign),
            })
        except campaigns.InfographicCampaignError as exc:
            return _json_error(exc, 409)

    @app.post('/image-lab/api/campaigns/<int:campaign_id>/retry')
    @login_required
    @_seller_required
    def infographic_campaign_retry(campaign_id):
        campaign = _owned_campaign(campaign_id)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'item_ids'}:
            return jsonify({
                'success': False,
                'error': 'Ожидается только item_ids',
            }), 400
        try:
            reset = campaigns.retry_items(
                campaign,
                seller_id=current_user.seller.id,
                item_ids=data.get('item_ids'),
            )
            campaigns.launch_campaign(app, campaign.id)
            return jsonify({
                'success': True,
                'queued_items': reset,
                'campaign': campaigns.campaign_summary(campaign),
            })
        except campaigns.InfographicCampaignError as exc:
            return _json_error(exc)

    @app.get(
        '/image-lab/campaigns/<int:campaign_id>/slides/<int:slide_id>/image',
    )
    @login_required
    @_seller_required
    def infographic_campaign_slide_image(campaign_id, slide_id):
        campaign = _owned_campaign(campaign_id)
        slide = InfographicCampaignSlide.query.filter_by(
            id=slide_id,
            campaign_id=campaign.id,
            seller_id=current_user.seller.id,
        ).first_or_404()
        path = campaigns.slide_artifact_path(slide)
        if path is None:
            return jsonify({'success': False, 'error': 'Слайд не найден'}), 404
        response = send_file(path, mimetype='image/png')
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        return response
