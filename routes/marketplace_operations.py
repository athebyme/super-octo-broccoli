"""Seller-facing audit and manual control for durable marketplace operations."""

from typing import Any, Dict, Optional

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from models import db
from services.marketplace_accounts import MarketplaceAccountService
from services.marketplace_publications import (
    MarketplacePublicationError,
    MarketplacePublicationService,
    MarketplacePublicationValidationError,
)


marketplace_operations_bp = Blueprint(
    "marketplace_operations",
    __name__,
    url_prefix="/marketplaces/operations",
)


def _seller_id() -> Optional[int]:
    seller = getattr(current_user, "seller", None)
    return getattr(seller, "id", None) if seller is not None else None


def _wants_json() -> bool:
    return request.is_json or (
        request.accept_mimetypes.best == "application/json"
        and request.accept_mimetypes["application/json"]
        >= request.accept_mimetypes["text/html"]
    )


def _payload() -> Dict[str, Any]:
    if request.is_json:
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise MarketplacePublicationValidationError(
                "JSON body должен быть объектом"
            )
        return value
    value = request.form.to_dict(flat=True)
    value.pop("csrf_token", None)
    return value


def _integer(
    value: Any,
    field_name: str,
    *,
    required: bool = True,
    default: Optional[int] = None,
) -> Optional[int]:
    if value in (None, ""):
        if default is not None:
            return default
        if not required:
            return None
        raise MarketplacePublicationValidationError(f"{field_name} обязателен")
    if isinstance(value, bool):
        raise MarketplacePublicationValidationError(
            f"{field_name} должен быть целым числом"
        )
    if isinstance(value, int):
        parsed = value
    elif (
        not request.is_json
        and isinstance(value, str)
        and value.isascii()
        and value.isdigit()
    ):
        parsed = int(value)
    else:
        raise MarketplacePublicationValidationError(
            f"{field_name} должен быть целым числом"
        )
    if parsed <= 0:
        raise MarketplacePublicationValidationError(
            f"{field_name} должен быть положительным"
        )
    return parsed


def _publication_enabled() -> bool:
    return bool(
        current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        and current_app.config.get(
            "MARKETPLACE_OZON_PUBLICATION_ENABLED",
            False,
        )
    )


def _operation_document(operation) -> dict:
    result = operation.to_public_dict(detail=True)
    result["snapshot"] = (
        operation.snapshot.to_public_dict() if operation.snapshot else None
    )
    return result


def _error_response(
    error: Exception,
    status_code: Optional[int] = None,
    *,
    force_json: bool = False,
):
    if isinstance(error, MarketplacePublicationError):
        status_code = status_code or error.status_code
        code = error.code
        validation = getattr(error, "validation", None)
    else:
        status_code = status_code or 400
        code = "invalid_marketplace_operation_request"
        validation = None
    if force_json or _wants_json():
        body = {
            "success": False,
            "error": str(error),
            "code": code,
        }
        if validation:
            body["validation"] = validation
        return jsonify(body), status_code
    return render_template(
        "marketplace_operation_error.html",
        error=str(error),
        code=code,
    ), status_code


def _write_failure(error: Exception, *, seller_id: int, action: str):
    if isinstance(error, MarketplacePublicationError):
        return _error_response(error)
    db.session.rollback()
    current_app.logger.exception(
        "Marketplace operation %s failed seller_id=%s",
        action,
        seller_id,
    )
    generic = MarketplacePublicationError(
        "Не удалось безопасно выполнить операцию Ozon"
    )
    generic.status_code = 500
    generic.code = "marketplace_operation_failed"
    return _error_response(generic)


@marketplace_operations_bp.route("/", methods=["GET"])
@login_required
def index():
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        filters = {
            "account_id": _integer(
                request.args.get("account_id"),
                "account_id",
                required=False,
            ),
            "status": request.args.get("status") or None,
            "page": _integer(request.args.get("page"), "page", default=1),
            "per_page": _integer(
                request.args.get("per_page"),
                "per_page",
                default=50,
            ),
        }
        pagination = MarketplacePublicationService.list_operations(
            seller_id=seller_id,
            **filters,
        )
        accounts = MarketplaceAccountService.list_accounts(
            seller_id=seller_id,
            marketplace_code="ozon",
        )
    except MarketplacePublicationError as exc:
        return _error_response(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "items": [
                _operation_document(operation)
                for operation in pagination.items
            ],
            "pagination": {
                "page": pagination.page,
                "per_page": pagination.per_page,
                "pages": pagination.pages,
                "total": pagination.total,
                "has_next": pagination.has_next,
                "has_prev": pagination.has_prev,
            },
        })
    return render_template(
        "marketplace_operations.html",
        pagination=pagination,
        operations=pagination.items,
        accounts=accounts,
        filters=filters,
    )


def _get_operation_response(operation_id: int, *, force_json: bool = False):
    seller_id = _seller_id()
    if seller_id is None:
        if force_json or _wants_json():
            return jsonify({
                "success": False,
                "error": "Seller account required",
            }), 403
        return "Seller account required", 403
    try:
        operation = MarketplacePublicationService.get_operation(
            seller_id=seller_id,
            operation_id=operation_id,
        )
    except MarketplacePublicationError as exc:
        return _error_response(exc, force_json=force_json)
    document = _operation_document(operation)
    if force_json or _wants_json():
        return jsonify({"success": True, "operation": document})
    return render_template(
        "marketplace_operation_detail.html",
        operation=operation,
        operation_data=document,
        can_submit_queued=_publication_enabled(),
    )


@marketplace_operations_bp.route("/<int:operation_id>", methods=["GET"])
@login_required
def detail(operation_id: int):
    return _get_operation_response(operation_id)


@marketplace_operations_bp.route("/api/<int:operation_id>", methods=["GET"])
@login_required
def detail_api(operation_id: int):
    return _get_operation_response(operation_id, force_json=True)


@marketplace_operations_bp.route(
    "/drafts/<int:draft_id>/publish",
    methods=["POST"],
)
@login_required
def publish_draft(draft_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _publication_enabled():
        disabled = MarketplacePublicationError(
            "Ручная публикация Ozon отключена отдельным feature flag"
        )
        disabled.status_code = 404
        disabled.code = "ozon_publication_disabled"
        return _error_response(disabled)
    try:
        data = _payload()
        if set(data) - {"expected_version", "idempotency_key"}:
            raise MarketplacePublicationValidationError(
                "Допустимы только expected_version и idempotency_key"
            )
        operation = MarketplacePublicationService.start_publication(
            seller_id=seller_id,
            draft_id=draft_id,
            expected_version=_integer(
                data.get("expected_version"),
                "expected_version",
            ),
            idempotency_key=data.get("idempotency_key"),
            created_by_user_id=getattr(current_user, "id", None),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="publish")
    if _wants_json():
        status_code = 200 if operation.is_terminal else 202
        return jsonify({
            "success": True,
            "operation": _operation_document(operation),
        }), status_code
    flash(
        "Операция Ozon сохранена; результат отслеживается асинхронно",
        "success",
    )
    return redirect(url_for(
        "marketplace_operations.detail",
        operation_id=operation.id,
    ))


@marketplace_operations_bp.route(
    "/<int:operation_id>/poll",
    methods=["POST"],
)
@login_required
def poll(operation_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        data = _payload()
        if data:
            raise MarketplacePublicationValidationError(
                "Poll не принимает полей"
            )
        operation = MarketplacePublicationService.poll_operation(
            seller_id=seller_id,
            operation_id=operation_id,
            allow_submission=_publication_enabled(),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="poll")
    if _wants_json():
        return jsonify({
            "success": True,
            "operation": _operation_document(operation),
        })
    flash("Статус операции Ozon обновлён", "success")
    return redirect(url_for(
        "marketplace_operations.detail",
        operation_id=operation.id,
    ))


def register_marketplace_operation_routes(app) -> None:
    app.register_blueprint(marketplace_operations_bp)
