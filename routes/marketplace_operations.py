"""Seller-facing audit and manual control for durable marketplace operations."""

import secrets

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
from services.marketplace_commercial import (
    MarketplaceCommercialError,
    MarketplaceCommercialService,
)
from services.marketplace_publications import (
    MarketplacePublicationError,
    MarketplacePublicationService,
    MarketplacePublicationValidationError,
)
from services import ozon_write_quarantine as quarantine


marketplace_operations_bp = Blueprint(
    "marketplace_operations",
    __name__,
    url_prefix="/marketplaces/operations",
)


class QuarantineForbidden(quarantine.QuarantineError):
    code = "write_quarantine_forbidden"
    status_code = 403


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


def _boolean(value: Any, field_name: str) -> bool:
    if request.is_json:
        if not isinstance(value, bool):
            raise MarketplacePublicationValidationError(
                f"{field_name} должен быть boolean"
            )
        return value
    if value == "1":
        return True
    if value in (None, "", "0"):
        return False
    raise MarketplacePublicationValidationError(
        f"{field_name} должен быть checkbox boolean"
    )


def _publication_enabled() -> bool:
    return bool(
        current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        and current_app.config.get(
            "MARKETPLACE_OZON_PUBLICATION_ENABLED",
            False,
        )
    )


def _commercial_enabled() -> bool:
    return bool(
        current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        and current_app.config.get(
            "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED",
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
    if isinstance(error, (MarketplacePublicationError, MarketplaceCommercialError, quarantine.QuarantineError)):
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
    if isinstance(error, (MarketplacePublicationError, MarketplaceCommercialError)):
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
        can_submit_queued=(
            _commercial_enabled()
            if operation.operation_kind in MarketplaceCommercialService.COMMERCIAL_OPERATION_KINDS
            else _publication_enabled()
        ),
        rollback_idempotency_key=secrets.token_urlsafe(24),
    )


@marketplace_operations_bp.route("/<int:operation_id>", methods=["GET"])
@login_required
def detail(operation_id: int):
    return _get_operation_response(operation_id)


@marketplace_operations_bp.route("/api/<int:operation_id>", methods=["GET"])
@login_required
def detail_api(operation_id: int):
    return _get_operation_response(operation_id, force_json=True)


def _quarantine_seller():
    seller_id = _seller_id()
    if seller_id is None:
        raise QuarantineForbidden("Нужен аккаунт продавца.")
    return seller_id


def _quarantine_error(exc):
    if isinstance(exc, quarantine.QuarantineError):
        return jsonify({"success": False, "code": exc.code, "error": str(exc)}), exc.status_code
    db.session.rollback()
    current_app.logger.exception("Ozon quarantine request failed")
    return jsonify({"success": False, "code": "write_quarantine_failed",
                    "error": "Не удалось сохранить решение. Проверьте его текущее состояние."}), 500


def _quarantine_document(operation_id, before_id=None):
    return quarantine.preview(seller_id=_quarantine_seller(), origin_id=operation_id,
                              viewer_user_id=current_user.id, before_id=before_id)


def _quarantine_payload(allowed):
    if not request.is_json:
        raise quarantine.QuarantineError("Отправьте JSON с просмотренной версией решения.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != allowed:
        raise quarantine.QuarantineError("Состав запроса не соответствует действию.")
    return data


def _quarantine_version(data, name):
    value = data[name]
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise quarantine.QuarantineError("Версия просмотра должна быть положительным целым числом.")
    return value


@marketplace_operations_bp.route("/<int:operation_id>/review", methods=["GET"])
@login_required
def quarantine_review(operation_id: int):
    try:
        document = _quarantine_document(operation_id)
    except quarantine.QuarantineError as exc:
        return _error_response(exc, exc.status_code)
    return render_template("ozon_operation_review.html", operation_id=operation_id,
                           account_id=document["account"]["id"])


@marketplace_operations_bp.route("/api/<int:operation_id>/review", methods=["GET"])
@login_required
def quarantine_review_api(operation_id: int):
    try:
        values = request.args.getlist("before_id")
        before = values[0] if values else None
        if set(request.args) - {"before_id"} or len(values) > 1 or (before is not None and
            (not 0 < len(before) <= 19 or not before.isascii() or not before.isdigit() or
             not 0 < int(before) <= 2**63 - 1)):
            raise quarantine.QuarantineError("Неверная страница журнала.")
        document = _quarantine_document(operation_id, int(before) if before else None)
    except quarantine.QuarantineError as exc:
        return _quarantine_error(exc)
    return jsonify({"success": True, "review": document})


@marketplace_operations_bp.route("/api/<int:operation_id>/quarantine", methods=["POST"])
@login_required
def quarantine_place(operation_id: int):
    try:
        data = _quarantine_payload({"expected_version", "scope_token", "reason", "confirm_scope"})
        if type(data["confirm_scope"]) is not bool:
            raise quarantine.QuarantineError("Подтвердите область остановки.")
        quarantine.place(seller_id=_quarantine_seller(), origin_id=operation_id,
            expected_version=_quarantine_version(data, "expected_version"),
            scope_token=data["scope_token"], reason=data["reason"],
            confirm_scope=data["confirm_scope"], actor_user_id=current_user.id)
        return jsonify({"success": True, "review": _quarantine_document(operation_id)})
    except Exception as exc:
        return _quarantine_error(exc)


@marketplace_operations_bp.route("/api/<int:operation_id>/quarantine/decision", methods=["POST"])
@login_required
def quarantine_decision(operation_id: int):
    try:
        data = _quarantine_payload({"expected_version", "expected_operation_version",
                                    "action", "reason", "confirm_release"})
        if type(data["confirm_release"]) is not bool:
            raise quarantine.QuarantineError("Подтвердите выбранное действие.")
        quarantine.update_decision(seller_id=_quarantine_seller(), origin_id=operation_id,
            expected_version=_quarantine_version(data, "expected_version"),
            expected_operation_version=_quarantine_version(data, "expected_operation_version"),
            action=data["action"], reason=data["reason"],
            confirm_release=data["confirm_release"], actor_user_id=current_user.id)
        return jsonify({"success": True, "review": _quarantine_document(operation_id)})
    except Exception as exc:
        return _quarantine_error(exc)


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
    "/drafts/<int:draft_id>/update",
    methods=["POST"],
)
@login_required
def update_draft(draft_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _publication_enabled():
        disabled = MarketplacePublicationError(
            "Обновление карточек Ozon отключено отдельным feature flag"
        )
        disabled.status_code = 404
        disabled.code = "ozon_publication_disabled"
        return _error_response(disabled)
    try:
        data = _payload()
        if set(data) - {"expected_version", "idempotency_key", "confirm_write"}:
            raise MarketplacePublicationValidationError(
                "Допустимы только expected_version, idempotency_key и confirm_write"
            )
        if not _boolean(data.get("confirm_write"), "confirm_write"):
            raise MarketplacePublicationValidationError(
                "Full-state update требует явного confirm_write=true"
            )
        operation = MarketplacePublicationService.start_update(
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
        return _write_failure(exc, seller_id=seller_id, action="product_update")
    if _wants_json():
        return jsonify({
            "success": True,
            "operation": _operation_document(operation),
        }), 200 if operation.is_terminal else 202
    flash(
        "Full-state update Ozon зафиксирован; результат и live-state сверяются",
        "success",
    )
    return redirect(url_for(
        "marketplace_operations.detail",
        operation_id=operation.id,
    ))


@marketplace_operations_bp.route(
    "/<int:operation_id>/rollback",
    methods=["POST"],
)
@login_required
def rollback(operation_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _publication_enabled():
        disabled = MarketplacePublicationError(
            "Rollback карточек Ozon отключён отдельным feature flag"
        )
        disabled.status_code = 404
        disabled.code = "ozon_publication_disabled"
        return _error_response(disabled)
    try:
        data = _payload()
        if set(data) - {"expected_version", "idempotency_key", "confirm_write"}:
            raise MarketplacePublicationValidationError(
                "Допустимы только expected_version, idempotency_key и confirm_write"
            )
        if not _boolean(data.get("confirm_write"), "confirm_write"):
            raise MarketplacePublicationValidationError(
                "Rollback требует отдельного явного confirm_write=true"
            )
        parent = MarketplacePublicationService.get_operation(
            seller_id=seller_id,
            operation_id=operation_id,
        )
        common = {
            "seller_id": seller_id,
            "operation_id": operation_id,
            "expected_version": _integer(
                data.get("expected_version"),
                "expected_version",
            ),
            "idempotency_key": data.get("idempotency_key"),
            "created_by_user_id": getattr(current_user, "id", None),
        }
        if parent.operation_kind == "product_import":
            operation = MarketplacePublicationService.start_create_rollback(
                **common
            )
        elif parent.operation_kind == "product_update":
            operation = MarketplacePublicationService.start_update_rollback(
                **common
            )
        else:
            raise MarketplacePublicationValidationError(
                "Этот вид операции не поддерживает product rollback"
            )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="product_rollback")
    if _wants_json():
        return jsonify({
            "success": True,
            "operation": _operation_document(operation),
        }), 200 if operation.is_terminal else 202
    flash("Отдельная rollback-операция Ozon создана", "success")
    return redirect(url_for(
        "marketplace_operations.detail",
        operation_id=operation.id,
    ))


@marketplace_operations_bp.route(
    "/<int:operation_id>/resolve-uncertain",
    methods=["POST"],
)
@login_required
def resolve_uncertain(operation_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        data = _payload()
        if set(data) - {"expected_version", "reason", "confirm_stop"}:
            raise MarketplacePublicationValidationError(
                "Допустимы только expected_version, reason и confirm_stop"
            )
        if not _boolean(data.get("confirm_stop"), "confirm_stop"):
            raise MarketplacePublicationValidationError(
                "Ручная остановка требует confirm_stop=true"
            )
        operation = MarketplacePublicationService.resolve_uncertain(
            seller_id=seller_id,
            operation_id=operation_id,
            expected_version=_integer(
                data.get("expected_version"),
                "expected_version",
            ),
            reason=data.get("reason"),
            resolved_by_user_id=getattr(current_user, "id", None),
        )
    except Exception as exc:
        return _write_failure(
            exc,
            seller_id=seller_id,
            action="resolve_uncertain",
        )
    if _wants_json():
        return jsonify({
            "success": True,
            "operation": _operation_document(operation),
        })
    flash(
        "Автоматическая сверка остановлена, local quota освобождена; "
        "upstream outcome остаётся uncertain",
        "warning",
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
        current = MarketplacePublicationService.get_operation(
            seller_id=seller_id,
            operation_id=operation_id,
        )
        if current.operation_kind in MarketplaceCommercialService.COMMERCIAL_OPERATION_KINDS:
            operation = MarketplaceCommercialService.poll_operation(
                seller_id=seller_id,
                operation_id=operation_id,
                allow_submission=_commercial_enabled(),
            )
        else:
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
