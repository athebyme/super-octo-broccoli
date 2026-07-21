# -*- coding: utf-8 -*-
"""Seller-facing image generation lab and experiment journal."""

from __future__ import annotations

import csv
import io
import json
import logging
from functools import wraps

from flask import (
    current_app,
    jsonify,
    render_template,
    Response,
    request,
    send_file,
)
from flask_login import current_user, login_required
import requests
from sqlalchemy.orm import joinedload

from models import ImageGenerationExperiment, ImportedProduct, db
from services import image_lab_service as lab
from services.admin_sales_intelligence import (
    AdminSalesIntelligenceError,
    AdminSalesIntelligenceService,
)
from services.marketplace_listing_media import (
    MarketplaceListingMediaError,
    MarketplaceListingMediaService,
)

logger = logging.getLogger(__name__)


def _image_mimetype(data_or_path) -> str:
    try:
        from PIL import Image

        image = Image.open(data_or_path)
        return Image.MIME.get(image.format, "application/octet-stream")
    except Exception:
        return "application/octet-stream"


def _seller_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({
                "success": False,
                "code": "auth_required",
                "error": "Сессия истекла. Войдите снова",
            }), 401
        if not current_user.seller:
            return jsonify({"success": False, "error": "Нужен профиль продавца"}), 403
        return function(*args, **kwargs)
    return wrapped


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _owned_experiment(experiment_id: int):
    return ImageGenerationExperiment.query.filter_by(
        id=experiment_id,
        seller_id=current_user.seller.id,
    ).first_or_404()


def register_image_lab_routes(app):
    @app.get('/image-lab')
    @login_required
    @_seller_required
    def image_lab_page():
        recommendations = AdminSalesIntelligenceService.seller_recommendations(
            seller_id=current_user.seller.id,
        )
        recommended_ids = []
        for item in recommendations:
            product_id = item["product_id"]
            if product_id not in recommended_ids:
                recommended_ids.append(product_id)
        owned_product_query = ImportedProduct.query.filter(
            ImportedProduct.seller_id == current_user.seller.id,
        ).options(joinedload(ImportedProduct.supplier_product))
        recommended_products = (
            owned_product_query.filter(ImportedProduct.id.in_(recommended_ids)).all()
            if recommended_ids else []
        )
        recommended_by_id = {product.id: product for product in recommended_products}
        recent_products = owned_product_query.filter(
            ImportedProduct.photo_urls.isnot(None),
            ImportedProduct.photo_urls != '',
        ).order_by(
            ImportedProduct.updated_at.desc()
        ).limit(150).all()
        products = []
        for product_id in recommended_ids:
            product = recommended_by_id.get(product_id)
            if product is not None:
                products.append(product)
        for product in recent_products:
            if product.id not in recommended_by_id:
                products.append(product)
            if len(products) >= 150:
                break
        experiments = ImageGenerationExperiment.query.filter_by(
            seller_id=current_user.seller.id,
        ).order_by(ImageGenerationExperiment.created_at.desc()).limit(60).all()
        product_ids = [product.id for product in products]
        marketplace_targets = {}
        if current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
            try:
                marketplace_targets = MarketplaceListingMediaService.targets_for_products(
                    seller_id=current_user.seller.id,
                    imported_product_ids=product_ids,
                )
            except MarketplaceListingMediaError as exc:
                logger.warning("Image Lab marketplace targets unavailable: %s", exc)
        lab_products = []
        for product in products:
            count = lab.effective_photo_count(product)
            if not count:
                continue
            visual_context = lab.build_product_visual_context(product)
            lab_products.append({
                "id": product.id,
                "title": product.title or f"Товар {product.id}",
                "category": product.mapped_wb_category or product.category or "",
                "photo_count": count,
                "photos": [
                    {"index": index, "label": f"Фото {index + 1}"}
                    for index in range(count)
                ],
                "visual_context": visual_context,
                "marketplace_targets": marketplace_targets.get(product.id, []),
                "suggested_overlay": {
                    "title": (product.title or f"Товар {product.id}")[:120],
                    "subtitle": lab.visual_context_summary(visual_context),
                },
            })
        return render_template(
            'image_lab.html',
            lab_products=lab_products,
            lab_capabilities=lab.capabilities(),
            lab_experiments=[lab.experiment_dict(item) for item in experiments],
            lab_analytics=lab.analytics(current_user.seller.id),
            lab_recommendations=recommendations,
            rating_tags=sorted(lab.RATING_TAGS),
        )

    @app.post('/image-lab/api/recommendations/<int:recommendation_id>/status')
    @_seller_required
    @login_required
    def image_lab_review_recommendation(recommendation_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"status"}:
            return jsonify({"success": False, "error": "Ожидается только status"}), 400
        if data.get("status") not in {"dismissed", "completed"}:
            return jsonify({"success": False, "error": "Неизвестный статус рекомендации"}), 400
        try:
            recommendation = AdminSalesIntelligenceService.review_recommendation(
                seller_id=current_user.seller.id,
                recommendation_id=recommendation_id,
                user_id=current_user.id,
                status=data.get("status"),
            )
            return jsonify({
                "success": True,
                "recommendation": {
                    "id": recommendation.id,
                    "status": recommendation.status,
                },
            })
        except AdminSalesIntelligenceError as exc:
            db.session.rollback()
            return jsonify({"success": False, "error": str(exc)}), 404

    @app.post('/image-lab/api/experiments')
    @_seller_required
    @login_required
    def image_lab_create_experiments():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Ожидается JSON object"}), 400
        product_id = _positive_int(data.get("product_id"))
        if product_id is None:
            return jsonify({"success": False, "error": "Некорректный product_id"}), 400
        scene_key = data.get("scene_key", "luxury")
        custom_scene = data.get("custom_scene", "")
        targets = data.get("targets")
        if not isinstance(scene_key, str) or not isinstance(custom_scene, str):
            return jsonify({"success": False, "error": "Некорректный prompt"}), 400
        if not isinstance(targets, list):
            return jsonify({"success": False, "error": "targets должен быть array"}), 400
        if (
            data.get("marketplace_target") is not None
            and not current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        ):
            return jsonify({
                "success": False,
                "error": "Ozon-интеграция выключена",
            }), 404
        try:
            experiments = lab.create_experiments(
                seller_id=current_user.seller.id,
                product_id=product_id,
                scene_key=scene_key,
                custom_scene=custom_scene,
                targets=targets,
                photo_indices=data.get("photo_indices"),
                generation_mode=data.get("generation_mode", "single"),
                generation_strategy=data.get("generation_strategy", "native_scene"),
                primary_photo_index=data.get("primary_photo_index"),
                photo_roles=data.get("photo_roles"),
                include_product_context=data.get("include_product_context", False),
                watermark=data.get("watermark"),
                overlay=data.get("overlay"),
                additional_prompt=data.get("additional_prompt", ""),
                requested_views=data.get("requested_views"),
                marketplace_target=data.get("marketplace_target"),
            )
            ids = [item.id for item in experiments]
            lab.launch_experiments(current_app._get_current_object(), ids)
            return jsonify({
                "success": True,
                "experiments": [lab.experiment_dict(item) for item in experiments],
            }), 202
        except lab.ImageLabError as exc:
            db.session.rollback()
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.get('/image-lab/api/experiments')
    @_seller_required
    @login_required
    def image_lab_list_experiments():
        limit_raw = request.args.get('limit', '60')
        try:
            limit = max(1, min(int(limit_raw), 100))
        except ValueError:
            limit = 60
        query = ImageGenerationExperiment.query.filter_by(
            seller_id=current_user.seller.id)
        product_raw = request.args.get('product_id')
        if product_raw:
            try:
                product_id = int(product_raw)
            except ValueError:
                return jsonify({"success": False, "error": "Некорректный product_id"}), 400
            query = query.filter_by(imported_product_id=product_id)
        listing_raw = request.args.get('listing_id')
        if listing_raw:
            if not listing_raw.isascii() or not listing_raw.isdigit() or int(listing_raw) <= 0:
                return jsonify({"success": False, "error": "Некорректный listing_id"}), 400
            query = query.filter_by(marketplace_listing_id=int(listing_raw))
        items = query.order_by(ImageGenerationExperiment.created_at.desc()).limit(limit).all()
        return jsonify({
            "success": True,
            "experiments": [lab.experiment_dict(item) for item in items],
        })

    @app.get('/image-lab/api/experiments/<int:experiment_id>')
    @_seller_required
    @login_required
    def image_lab_get_experiment(experiment_id):
        experiment = _owned_experiment(experiment_id)
        if experiment.status == 'queued':
            lab.launch_experiments(current_app._get_current_object(), [experiment.id])
        if experiment.status == 'remote_running':
            try:
                lab.refresh_remote_experiment(experiment)
            except requests.RequestException as exc:
                logger.warning("GPU bridge poll failed for %s: %s", experiment.id, exc)
            except Exception as exc:  # noqa: BLE001
                logger.exception("GPU experiment finalize failed: %s", exc)
                db.session.rollback()
        return jsonify({"success": True, "experiment": lab.experiment_dict(experiment)})

    @app.post('/image-lab/api/experiments/<int:experiment_id>/rating')
    @_seller_required
    @login_required
    def image_lab_rate_experiment(experiment_id):
        experiment = _owned_experiment(experiment_id)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Ожидается JSON object"}), 400
        try:
            lab.rate_experiment(
                experiment,
                rating=data.get("rating"),
                tags=data.get("tags") if isinstance(data.get("tags"), list) else [],
                comment=data.get("comment") if isinstance(data.get("comment"), str) else "",
            )
            return jsonify({"success": True, "experiment": lab.experiment_dict(experiment)})
        except lab.ImageLabError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.post('/image-lab/api/experiments/<int:experiment_id>/cancel')
    @_seller_required
    @login_required
    def image_lab_cancel_experiment(experiment_id):
        experiment = _owned_experiment(experiment_id)
        try:
            lab.cancel_experiment(experiment)
            return jsonify({"success": True, "experiment": lab.experiment_dict(experiment)})
        except (lab.ImageLabError, requests.RequestException) as exc:
            db.session.rollback()
            return jsonify({"success": False, "error": str(exc)}), 409

    @app.post('/image-lab/api/experiments/<int:experiment_id>/repeat')
    @_seller_required
    @login_required
    def image_lab_repeat_experiment(experiment_id):
        source = _owned_experiment(experiment_id)
        if source.imported_product_id is None or source.imported_product is None:
            return jsonify({
                "success": False,
                "error": "Исходный товар удалён; повтор недоступен",
            }), 400
        data = request.get_json(silent=True) or {}
        target = data.get("target") if isinstance(data, dict) else None
        backend = target.get("backend") if isinstance(target, dict) else source.backend
        model = target.get("model") if isinstance(target, dict) else source.model
        try:
            strategy = source.generation_strategy or 'background_only'
            cost = lab.validate_target(backend, model, strategy)
            lab.enforce_budget(source.seller_id, cost, 1)
            target_context = None
            if source.marketplace_listing_id is not None:
                stored_target = lab.json_load(source.target_context_json, {})
                target_context = lab.validate_marketplace_target(
                    seller_id=source.seller_id,
                    product_id=source.imported_product_id,
                    value={
                        "entity_kind": "marketplace_listing",
                        "listing_id": source.marketplace_listing_id,
                        "marketplace_code": stored_target.get("marketplace_code"),
                        "account_id": stored_target.get("account_id"),
                    },
                )
            clone = ImageGenerationExperiment(
                seller_id=source.seller_id,
                imported_product_id=source.imported_product_id,
                marketplace_listing_id=(
                    target_context["listing_id"] if target_context else None
                ),
                target_context_json=(
                    json.dumps(
                        target_context,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if target_context else None
                ),
                backend=backend,
                model=model,
                scene_key=source.scene_key,
                generation_strategy=strategy,
                composition_mode=source.composition_mode or 'single',
                source_photo_indices_json=source.source_photo_indices_json or '[0]',
                source_photo_roles_json=source.source_photo_roles_json or '{}',
                primary_photo_index=source.primary_photo_index,
                requested_view=source.requested_view,
                prompt=source.prompt,
                prompt_sha256=source.prompt_sha256,
                status='queued',
                estimated_cost_rub=cost,
                watermark_json=source.watermark_json,
                overlay_json=source.overlay_json,
            )
            db.session.add(clone)
            db.session.commit()
            lab.launch_experiments(current_app._get_current_object(), [clone.id])
            return jsonify({"success": True, "experiment": lab.experiment_dict(clone)}), 202
        except lab.ImageLabError as exc:
            db.session.rollback()
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.get('/image-lab/api/experiments/<int:experiment_id>/image/<kind>')
    @_seller_required
    @login_required
    def image_lab_experiment_image(experiment_id, kind):
        if kind not in ('source', 'reference', 'background', 'final', 'watermark'):
            return jsonify({"success": False, "error": "Неизвестный artifact"}), 404
        experiment = _owned_experiment(experiment_id)
        path = lab.artifact_path(experiment, kind)
        if not path:
            return jsonify({"success": False, "error": "Artifact не найден"}), 404
        response = send_file(path, mimetype=_image_mimetype(path))
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        return response

    @app.post('/image-lab/api/watermarks')
    @_seller_required
    @login_required
    def image_lab_upload_watermark():
        upload = request.files.get('file')
        if upload is None:
            return jsonify({"success": False, "error": "PNG-файл не передан"}), 400
        try:
            result = lab.store_watermark(
                current_user.seller.id,
                upload.stream.read(2 * 1024 * 1024 + 1),
            )
            result["url"] = f"/image-lab/api/watermarks/{result['id']}"
            return jsonify({"success": True, "watermark": result}), 201
        except lab.ImageLabError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    @app.get('/image-lab/api/watermarks/<watermark_id>')
    @_seller_required
    @login_required
    def image_lab_watermark_preview(watermark_id):
        path = lab.watermark_preset_path(current_user.seller.id, watermark_id)
        if path is None:
            return jsonify({"success": False, "error": "Водяной знак не найден"}), 404
        response = send_file(path, mimetype='image/png')
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        return response

    @app.get('/image-lab/api/products/<int:product_id>/original')
    @_seller_required
    @login_required
    def image_lab_product_original(product_id):
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=current_user.seller.id).first_or_404()
        raw_index = request.args.get('photo_index', '0')
        if not raw_index.isdigit():
            return jsonify({"success": False, "error": "Некорректный photo_index"}), 400
        photo_index = int(raw_index)
        prefer_preview = request.args.get('preview') == '1'
        try:
            data = lab.fetch_original_product_bytes(
                product,
                photo_index,
                prefer_preview=prefer_preview,
            )
        except lab.ImageLabError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        response = send_file(io.BytesIO(data), mimetype=_image_mimetype(io.BytesIO(data)))
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        return response

    @app.get('/image-lab/api/analytics')
    @_seller_required
    @login_required
    def image_lab_analytics():
        return jsonify({"success": True, **lab.analytics(current_user.seller.id)})

    @app.get('/image-lab/api/export.csv')
    @_seller_required
    @login_required
    def image_lab_export_csv():
        items = ImageGenerationExperiment.query.filter_by(
            seller_id=current_user.seller.id,
        ).order_by(ImageGenerationExperiment.created_at.asc()).all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'id', 'product_id', 'marketplace_listing_id', 'marketplace_code',
            'marketplace_account_id', 'backend', 'model', 'scene_key',
            'generation_strategy', 'composition_mode', 'primary_photo_index',
            'requested_view',
            'photo_indices', 'photo_roles', 'watermark', 'overlay', 'prompt_sha256',
            'status', 'quality_status', 'publishable', 'latency_s', 'cost_rub',
            'rating', 'rating_tags', 'rating_comment', 'created_at', 'completed_at',
        ])
        for item in items:
            quality = lab.json_load(item.quality_json, {})
            target = lab.json_load(item.target_context_json, {})
            writer.writerow([
                item.id, item.imported_product_id, item.marketplace_listing_id,
                target.get('marketplace_code', ''), target.get('account_id', ''),
                item.backend, item.model,
                item.scene_key or '', item.generation_strategy or 'background_only',
                item.composition_mode or 'single', item.primary_photo_index,
                item.requested_view or '',
                item.source_photo_indices_json or '[0]',
                item.source_photo_roles_json or '{}', item.watermark_json or '',
                item.overlay_json or '', item.prompt_sha256, item.status,
                quality.get('status', ''), quality.get('publishable', False),
                item.latency_s, item.estimated_cost_rub, item.rating,
                '|'.join(lab.json_load(item.rating_tags_json, [])),
                item.rating_comment or '',
                item.created_at.isoformat() if item.created_at else '',
                item.completed_at.isoformat() if item.completed_at else '',
            ])
        return Response(
            '\ufeff' + output.getvalue(),
            mimetype='text/csv; charset=utf-8',
            headers={
                'Content-Disposition': 'attachment; filename=image-lab-experiments.csv',
                'Cache-Control': 'private, no-store',
            },
        )
