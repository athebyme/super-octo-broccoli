"""Seller-scoped APIs for editing common imported-product content."""

import json

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import current_user, login_required
from flask_wtf.csrf import generate_csrf
from werkzeug.exceptions import RequestEntityTooLarge

from models import db
from services.common_product_content import (
    CommonProductContentError,
    CommonProductContentService,
    MAX_DB_ID,
    MAX_BODY_BYTES,
)


common_product_content_bp = Blueprint(
    "common_product_content",
    __name__,
    url_prefix="/api/my-products",
)


def _identity():
    seller = getattr(current_user, "seller", None)
    seller_id = getattr(seller, "id", None) if seller is not None else None
    user_id = getattr(current_user, "id", None)
    if (
        isinstance(seller_id, bool) or not isinstance(seller_id, int) or seller_id <= 0
        or isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
    ):
        return None
    return seller_id, user_id


def _error(error: CommonProductContentError):
    db.session.rollback()
    return jsonify({
        "success": False,
        "error": str(error),
        "code": error.code,
    }), error.status_code


def _read_json_body(*, expected_keys: set[str]):
    if not request.is_json:
        raise CommonProductContentError("Нужен JSON-запрос с типом application/json")
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise CommonProductContentError(
            "Запрос превышает 256 КиБ; разделите изменения на несколько сохранений",
            status_code=413,
            code="body_too_large",
        )
    try:
        raw = request.stream.read(MAX_BODY_BYTES + 1)
    except RequestEntityTooLarge:
        raise CommonProductContentError(
            "Запрос превышает 256 КиБ; разделите изменения на несколько сохранений",
            status_code=413,
            code="body_too_large",
        ) from None
    if len(raw) > MAX_BODY_BYTES:
        raise CommonProductContentError(
            "Запрос превышает 256 КиБ; разделите изменения на несколько сохранений",
            status_code=413,
            code="body_too_large",
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CommonProductContentError("Тело запроса должно содержать корректный UTF-8 JSON") from None
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise CommonProductContentError("Тело запроса содержит неизвестные или отсутствующие поля")
    return value


@common_product_content_bp.get("/<int:product_id>/common-content")
@login_required
def get_common_product_content(product_id: int):
    identity = _identity()
    if identity is None:
        return jsonify(success=False, error="Для этой операции нужен кабинет продавца", code="seller_required"), 404
    seller_id, _ = identity
    try:
        product = CommonProductContentService.read_many(
            seller_id=seller_id,
            product_ids=[product_id],
        )[0]
        return jsonify({
            "success": True,
            "product": product,
            "save_effect": "common_only",
            "notice": "Сохранение меняет только общий товар. Черновики и опубликованные карточки останутся без изменений.",
        })
    except CommonProductContentError as error:
        return _error(error)


@common_product_content_bp.post("/common-content/preview")
@login_required
def preview_common_product_content():
    identity = _identity()
    if identity is None:
        return jsonify(success=False, error="Для этой операции нужен кабинет продавца", code="seller_required"), 404
    seller_id, user_id = identity
    try:
        payload = _read_json_body(expected_keys={"items"})
        preview = CommonProductContentService.preview(
            seller_id=seller_id,
            user_id=user_id,
            raw_items=payload["items"],
        )
        return jsonify({
            "success": True,
            "preview": preview,
            "save_effect": "common_only",
            "notice": "Это сохранит общий товар. Черновики и карточки на площадках не обновятся.",
        })
    except CommonProductContentError as error:
        return _error(error)


@common_product_content_bp.post("/common-content/apply")
@login_required
def apply_common_product_content():
    identity = _identity()
    if identity is None:
        return jsonify(success=False, error="Для этой операции нужен кабинет продавца", code="seller_required"), 404
    seller_id, user_id = identity
    try:
        payload = _read_json_body(expected_keys={"preview_token"})
        applied = CommonProductContentService.apply(
            seller_id=seller_id,
            user_id=user_id,
            token=payload["preview_token"],
        )
        db.session.commit()
        return jsonify({
            "success": True,
            "applied": applied,
            "save_effect": "common_only",
            "notice": "Общий товар сохранён. Черновики и опубликованные карточки не изменены.",
        })
    except CommonProductContentError as error:
        return _error(error)
    except Exception:
        current_app.logger.exception("Common product content save failed")
        db.session.rollback()
        return jsonify({
            "success": False,
            "error": "Не удалось сохранить общий товар. Обновите просмотр и повторите проверку.",
            "code": "common_content_save_failed",
        }), 500


def register_common_product_content_routes(app) -> None:
    app.register_blueprint(common_product_content_bp)

    @app.get("/my-products/common-content")
    @login_required
    def common_product_content_editor_page():
        identity = _identity()
        if identity is None:
            return jsonify(success=False, error="Для этой операции нужен кабинет продавца", code="seller_required"), 404
        seller_id, _ = identity
        if set(request.args.keys()) - {"product_id"}:
            return jsonify(success=False, error="Используйте только параметр product_id", code="invalid_selection"), 400
        raw_ids = request.args.getlist("product_id")
        if len(raw_ids) > 50:
            return jsonify(success=False, error="Можно открыть не больше 50 товаров", code="too_many_items"), 413
        product_ids = []
        for raw_id in raw_ids:
            if (
                not raw_id.isascii() or not raw_id.isdigit()
                or len(raw_id) > 19
            ):
                return jsonify(success=False, error="Выбор содержит неверный ID товара", code="invalid_selection"), 400
            product_id = int(raw_id)
            if product_id <= 0 or product_id > MAX_DB_ID:
                return jsonify(success=False, error="Выбор содержит неверный ID товара", code="invalid_selection"), 400
            product_ids.append(product_id)
        if len(product_ids) != len(set(product_ids)):
            return jsonify(success=False, error="Выбор содержит повтор товара", code="duplicate_selection"), 400
        try:
            products = (
                CommonProductContentService.read_many(seller_id=seller_id, product_ids=product_ids)
                if product_ids else []
            )
        except CommonProductContentError as error:
            return _error(error)
        return render_template(
            "common_product_content.html",
            products=products,
            selected_product_ids=product_ids,
            csrf_token=generate_csrf(),
        )
