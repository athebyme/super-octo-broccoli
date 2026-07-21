# -*- coding: utf-8 -*-
"""Seller-reviewed bulk media publication UI and signed provider assets."""

from __future__ import annotations

from functools import wraps

from flask import abort, jsonify, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from models import InfographicCampaign, MarketplaceMediaPublication
from services import marketplace_media_publications as media_publications


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
        id=campaign_id,
        seller_id=current_user.seller.id,
    ).first_or_404()


def _owned_publication(publication_id: int) -> MarketplaceMediaPublication:
    return MarketplaceMediaPublication.query.filter_by(
        id=publication_id,
        seller_id=current_user.seller.id,
    ).first_or_404()


def _json_error(error, status: int = 400):
    return jsonify({
        'success': False,
        'code': getattr(error, 'code', 'media_publication_error'),
        'error': str(error),
    }), status


def register_marketplace_media_publication_routes(app):
    @app.get('/image-lab/campaigns/<int:campaign_id>/publication/new')
    @login_required
    @_seller_required
    def marketplace_media_publication_new(campaign_id):
        campaign = _owned_campaign(campaign_id)
        try:
            readiness = media_publications.channel_readiness(
                campaign,
                seller_id=current_user.seller.id,
                config=app.config,
            )
        except media_publications.MarketplaceMediaPublicationError as exc:
            return render_template(
                'marketplace_media_publication_new.html',
                campaign=campaign,
                readiness=None,
                recent=[],
                error=str(exc),
            ), 400
        return render_template(
            'marketplace_media_publication_new.html',
            campaign=campaign,
            readiness=readiness,
            recent=media_publications.list_publications(
                seller_id=current_user.seller.id,
                campaign_id=campaign.id,
                limit=10,
            ),
            error='',
        )

    @app.post('/image-lab/api/campaigns/<int:campaign_id>/publications')
    @login_required
    @_seller_required
    def marketplace_media_publication_create(campaign_id):
        campaign = _owned_campaign(campaign_id)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {
            'marketplace_code', 'account_id', 'item_ids',
        }:
            return jsonify({
                'success': False,
                'error': 'Ожидаются marketplace_code, account_id и item_ids',
            }), 400
        try:
            publication = media_publications.prepare_publication(
                campaign,
                seller_id=current_user.seller.id,
                user_id=current_user.id,
                marketplace_code=data.get('marketplace_code'),
                account_id=data.get('account_id'),
                item_ids=data.get('item_ids'),
            )
            return jsonify({
                'success': True,
                'publication': media_publications.publication_summary(
                    publication, detail=True,
                ),
                'redirect_url': url_for(
                    'marketplace_media_publication_detail',
                    publication_id=publication.id,
                ),
            }), 201
        except media_publications.MarketplaceMediaPublicationError as exc:
            return _json_error(exc, 409 if exc.code.endswith('drift') else 400)

    @app.get('/image-lab/publications/<int:publication_id>')
    @login_required
    @_seller_required
    def marketplace_media_publication_detail(publication_id):
        publication = _owned_publication(publication_id)
        return render_template(
            'marketplace_media_publication_detail.html',
            publication=media_publications.publication_summary(
                publication, detail=True,
            ),
            public_host=media_publications.public_base_status(app.config),
        )

    @app.get('/image-lab/api/publications/<int:publication_id>')
    @login_required
    @_seller_required
    def marketplace_media_publication_status(publication_id):
        publication = _owned_publication(publication_id)
        return jsonify({
            'success': True,
            'publication': media_publications.publication_summary(
                publication, detail=True,
            ),
            'public_host': media_publications.public_base_status(app.config),
        })

    @app.post('/image-lab/api/publications/<int:publication_id>/confirm')
    @login_required
    @_seller_required
    def marketplace_media_publication_confirm(publication_id):
        publication = _owned_publication(publication_id)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {
            'expected_version', 'confirm_exact_order',
        }:
            return jsonify({
                'success': False,
                'error': 'Ожидаются expected_version и confirm_exact_order',
            }), 400
        try:
            queued = media_publications.confirm_publication(
                publication,
                seller_id=current_user.seller.id,
                user_id=current_user.id,
                expected_version=data.get('expected_version'),
                confirm_exact_order=data.get('confirm_exact_order') is True,
                config=app.config,
            )
            media_publications.launch_publication(app, publication.id)
            return jsonify({
                'success': True,
                'queued': queued,
                'publication': media_publications.publication_summary(
                    publication, detail=True,
                ),
            })
        except media_publications.MarketplaceMediaPublicationError as exc:
            return _json_error(exc, 409 if exc.code in {
                'version_conflict', 'target_operation_active',
            } else 400)

    @app.post('/image-lab/api/publications/<int:publication_id>/cancel')
    @login_required
    @_seller_required
    def marketplace_media_publication_cancel(publication_id):
        publication = _owned_publication(publication_id)
        cancelled = media_publications.cancel_publication(
            publication, seller_id=current_user.seller.id,
        )
        return jsonify({
            'success': True,
            'cancelled': cancelled,
            'publication': media_publications.publication_summary(
                publication, detail=True,
            ),
        })

    @app.post('/image-lab/api/publications/<int:publication_id>/rollbacks')
    @login_required
    @_seller_required
    def marketplace_media_publication_rollback(publication_id):
        publication = _owned_publication(publication_id)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {
            'operation_ids', 'confirm_exact_state',
        }:
            return jsonify({
                'success': False,
                'error': 'Ожидаются operation_ids и confirm_exact_state',
            }), 400
        try:
            rollbacks = media_publications.create_rollbacks(
                publication,
                seller_id=current_user.seller.id,
                user_id=current_user.id,
                operation_ids=data.get('operation_ids'),
                confirm_exact_state=data.get('confirm_exact_state') is True,
            )
            media_publications.launch_publication(app, publication.id)
            return jsonify({
                'success': True,
                'queued': len(rollbacks),
                'publication': media_publications.publication_summary(
                    publication, detail=True,
                ),
            }), 201
        except media_publications.MarketplaceMediaPublicationError as exc:
            return _json_error(exc, 409 if exc.code in {
                'rollback_live_drift', 'rollback_already_exists',
                'target_operation_active',
            } else 400)

    @app.get(
        '/media-publications/assets/<int:operation_id>/<int:position>/'
        '<int:expires>/<signature>.img',
    )
    def marketplace_media_public_asset(
        operation_id, position, expires, signature,
    ):
        try:
            path, mime_type = media_publications.resolve_public_asset(
                operation_id=operation_id,
                position=position,
                expires=expires,
                signature=signature,
                secret_key=app.config['SECRET_KEY'],
            )
        except media_publications.MarketplaceMediaPublicationError as exc:
            if exc.code == 'asset_url_expired':
                abort(410)
            if exc.code == 'asset_signature_invalid':
                abort(403)
            abort(404)
        response = send_file(path, mimetype=mime_type, conditional=True)
        response.headers['Cache-Control'] = 'public, max-age=300, immutable'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
