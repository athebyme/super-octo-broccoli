"""Seller-facing Ozon warehouse reads and reviewed price/stock proposals."""

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
    MarketplaceCommercialValidationError,
)
from services.marketplace_warehouses import (
    MarketplaceWarehouseError,
    MarketplaceWarehouseService,
)


marketplace_commercial_bp = Blueprint(
    "marketplace_commercial",
    __name__,
    url_prefix="/marketplaces/commercial",
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
            raise MarketplaceCommercialValidationError(
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
        raise MarketplaceCommercialValidationError(f"{field_name} обязателен")
    if isinstance(value, bool):
        raise MarketplaceCommercialValidationError(
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
        raise MarketplaceCommercialValidationError(
            f"{field_name} должен быть целым числом"
        )
    if parsed <= 0:
        raise MarketplaceCommercialValidationError(
            f"{field_name} должен быть положительным"
        )
    return parsed


def _stock(value: Any) -> int:
    if isinstance(value, bool):
        raise MarketplaceCommercialValidationError("stock должен быть целым числом")
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
        raise MarketplaceCommercialValidationError("stock должен быть целым числом")
    if parsed < 0:
        raise MarketplaceCommercialValidationError("stock не может быть отрицательным")
    return parsed


def _price(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, float):
        raise MarketplaceCommercialValidationError(
            "price передавайте decimal-строкой или целым числом"
        )
    if not isinstance(value, (str, int)):
        raise MarketplaceCommercialValidationError("price обязателен")
    return value


def _boolean(value: Any, field_name: str, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if not request.is_json and isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise MarketplaceCommercialValidationError(f"{field_name} должен быть boolean")


def _feature_enabled() -> bool:
    return bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False))


def _write_enabled() -> bool:
    return bool(
        _feature_enabled()
        and current_app.config.get(
            "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED",
            False,
        )
    )


def _require_feature() -> None:
    if not _feature_enabled():
        error = MarketplaceCommercialError("Ozon commercial feature отключена")
        error.status_code = 404
        error.code = "ozon_commercial_disabled"
        raise error


def _require_write() -> None:
    if not _write_enabled():
        error = MarketplaceCommercialError(
            "Новые Ozon price/stock writes отключены отдельным feature flag"
        )
        error.status_code = 404
        error.code = "ozon_commercial_writes_disabled"
        raise error


def _document(proposal) -> dict:
    data = proposal.to_public_dict(detail=True)
    operation = proposal.operation
    data["operation"] = operation.to_public_dict(detail=True) if operation else None
    if operation and operation.snapshot:
        data["operation"]["snapshot"] = operation.snapshot.to_public_dict()
    return data


def _error_response(error: Exception, *, force_json: bool = False):
    if isinstance(error, (MarketplaceCommercialError, MarketplaceWarehouseError)):
        status_code = error.status_code
        code = error.code
    else:
        status_code = 400
        code = "invalid_marketplace_commercial_request"
    if force_json or _wants_json():
        return jsonify({
            "success": False,
            "error": str(error),
            "code": code,
        }), status_code
    return render_template(
        "marketplace_commercial_error.html",
        error=str(error),
        code=code,
    ), status_code


def _write_failure(error: Exception, *, seller_id: int, action: str):
    if isinstance(error, (MarketplaceCommercialError, MarketplaceWarehouseError)):
        return _error_response(error)
    db.session.rollback()
    current_app.logger.exception(
        "Marketplace commercial %s failed seller_id=%s",
        action,
        seller_id,
    )
    generic = MarketplaceCommercialError(
        "Не удалось безопасно выполнить commercial workflow Ozon"
    )
    generic.status_code = 500
    generic.code = "marketplace_commercial_failed"
    return _error_response(generic)


@marketplace_commercial_bp.route("/", methods=["GET"])
@login_required
def index():
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        _require_feature()
        account_id = _integer(
            request.args.get("account_id"),
            "account_id",
            required=False,
        )
        status = request.args.get("status") or None
        page = _integer(request.args.get("page"), "page", default=1)
        per_page = _integer(request.args.get("per_page"), "per_page", default=50)
        pagination = MarketplaceCommercialService.list_proposals(
            seller_id=seller_id,
            account_id=account_id,
            status=status,
            page=page,
            per_page=per_page,
        )
        accounts = MarketplaceAccountService.list_accounts(
            seller_id=seller_id,
            marketplace_code="ozon",
        )
        warehouses = (
            MarketplaceWarehouseService.list_warehouses(
                seller_id=seller_id,
                account_id=account_id,
                include_unavailable=True,
            )
            if account_id is not None else []
        )
    except (MarketplaceCommercialError, MarketplaceWarehouseError) as exc:
        return _error_response(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "items": [_document(item) for item in pagination.items],
            "warehouses": [item.to_public_dict() for item in warehouses],
            "pagination": {
                "page": pagination.page,
                "per_page": pagination.per_page,
                "pages": pagination.pages,
                "total": pagination.total,
            },
        })
    return render_template(
        "marketplace_commercial.html",
        proposals=pagination.items,
        pagination=pagination,
        accounts=accounts,
        warehouses=warehouses,
        filters={"account_id": account_id, "status": status},
        write_enabled=_write_enabled(),
    )


def _detail_response(proposal_id: int, *, force_json: bool = False):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        proposal = MarketplaceCommercialService.get_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
        )
    except MarketplaceCommercialError as exc:
        return _error_response(exc, force_json=force_json)
    document = _document(proposal)
    if force_json or _wants_json():
        return jsonify({"success": True, "proposal": document})
    return render_template(
        "marketplace_commercial_detail.html",
        proposal=proposal,
        proposal_data=document,
        write_enabled=_write_enabled(),
    )


@marketplace_commercial_bp.route("/<int:proposal_id>", methods=["GET"])
@login_required
def detail(proposal_id: int):
    return _detail_response(proposal_id)


@marketplace_commercial_bp.route("/api/<int:proposal_id>", methods=["GET"])
@login_required
def detail_api(proposal_id: int):
    return _detail_response(proposal_id, force_json=True)


@marketplace_commercial_bp.route(
    "/accounts/<int:account_id>/warehouses/sync",
    methods=["POST"],
)
@login_required
def sync_warehouses(account_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        data = _payload()
        if data:
            raise MarketplaceCommercialValidationError(
                "Warehouse sync не принимает полей"
            )
        run = MarketplaceWarehouseService.sync_warehouses(
            seller_id=seller_id,
            account_id=account_id,
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="warehouse_sync")
    if _wants_json():
        return jsonify({"success": True, "sync": run.to_public_dict()})
    flash("Склады Ozon синхронизированы полным snapshot", "success")
    return redirect(url_for(
        "marketplace_commercial.index",
        account_id=account_id,
    ))


@marketplace_commercial_bp.route(
    "/listings/<int:listing_id>/stocks/refresh",
    methods=["POST"],
)
@login_required
def refresh_stocks(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        data = _payload()
        if data:
            raise MarketplaceCommercialValidationError(
                "Stock refresh не принимает полей"
            )
        rows = MarketplaceWarehouseService.refresh_listing_stocks(
            seller_id=seller_id,
            listing_id=listing_id,
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="stock_refresh")
    if _wants_json():
        return jsonify({
            "success": True,
            "items": [row.to_public_dict() for row in rows],
        })
    flash("Точные FBS/rFBS остатки Ozon обновлены", "success")
    return redirect(url_for("marketplace_listings.detail", listing_id=listing_id))


@marketplace_commercial_bp.route(
    "/listings/<int:listing_id>/price-proposals",
    methods=["POST"],
)
@login_required
def create_price_proposal(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        data = _payload()
        allowed = {
            "price",
            "idempotency_key",
            "allow_price_decrease",
            "allow_large_change",
            "guardrail_note",
        }
        if set(data) - allowed:
            raise MarketplaceCommercialValidationError(
                "Price proposal содержит неподдерживаемые поля"
            )
        proposal = MarketplaceCommercialService.create_price_proposal(
            seller_id=seller_id,
            listing_id=listing_id,
            price=_price(data.get("price")),
            source="user",
            idempotency_key=data.get("idempotency_key") or None,
            created_by_user_id=getattr(current_user, "id", None),
            allow_price_decrease=_boolean(
                data.get("allow_price_decrease"),
                "allow_price_decrease",
            ),
            allow_large_change=_boolean(
                data.get("allow_large_change"),
                "allow_large_change",
            ),
            guardrail_note=data.get("guardrail_note") or None,
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="price_proposal")
    if _wants_json():
        return jsonify({"success": True, "proposal": _document(proposal)}), 201
    flash("Price proposal сохранён и ждёт отдельного подтверждения", "success")
    return redirect(url_for(
        "marketplace_commercial.detail",
        proposal_id=proposal.id,
    ))


@marketplace_commercial_bp.route(
    "/listings/<int:listing_id>/stock-proposals",
    methods=["POST"],
)
@login_required
def create_stock_proposal(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        data = _payload()
        if set(data) - {"warehouse_id", "stock", "idempotency_key"}:
            raise MarketplaceCommercialValidationError(
                "Stock proposal содержит неподдерживаемые поля"
            )
        proposal = MarketplaceCommercialService.create_stock_proposal(
            seller_id=seller_id,
            listing_id=listing_id,
            warehouse_id=_integer(data.get("warehouse_id"), "warehouse_id"),
            stock=_stock(data.get("stock")),
            source="user",
            idempotency_key=data.get("idempotency_key") or None,
            created_by_user_id=getattr(current_user, "id", None),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="stock_proposal")
    if _wants_json():
        return jsonify({"success": True, "proposal": _document(proposal)}), 201
    flash("Stock proposal сохранён и ждёт отдельного подтверждения", "success")
    return redirect(url_for(
        "marketplace_commercial.detail",
        proposal_id=proposal.id,
    ))


@marketplace_commercial_bp.route(
    "/<int:proposal_id>/approve",
    methods=["POST"],
)
@login_required
def approve(proposal_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_write()
        data = _payload()
        if set(data) - {"expected_version"}:
            raise MarketplaceCommercialValidationError(
                "Approve принимает только expected_version"
            )
        proposal = MarketplaceCommercialService.approve_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
            expected_version=_integer(
                data.get("expected_version"),
                "expected_version",
            ),
            reviewed_by_user_id=getattr(current_user, "id", None),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="approve")
    if _wants_json():
        return jsonify({"success": True, "proposal": _document(proposal)})
    flash("Proposal проверен; write выполнен или поставлен на live-сверку", "success")
    return redirect(url_for(
        "marketplace_commercial.detail",
        proposal_id=proposal.id,
    ))


@marketplace_commercial_bp.route(
    "/<int:proposal_id>/reject",
    methods=["POST"],
)
@login_required
def reject(proposal_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        data = _payload()
        if set(data) - {"expected_version", "note"}:
            raise MarketplaceCommercialValidationError(
                "Reject принимает expected_version и note"
            )
        proposal = MarketplaceCommercialService.reject_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
            expected_version=_integer(
                data.get("expected_version"),
                "expected_version",
            ),
            reviewed_by_user_id=getattr(current_user, "id", None),
            note=data.get("note") or None,
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="reject")
    if _wants_json():
        return jsonify({"success": True, "proposal": _document(proposal)})
    flash("Proposal отклонён без Ozon write", "success")
    return redirect(url_for(
        "marketplace_commercial.detail",
        proposal_id=proposal.id,
    ))


@marketplace_commercial_bp.route(
    "/operations/<int:operation_id>/rollback-proposals",
    methods=["POST"],
)
@login_required
def create_rollback(operation_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        data = _payload()
        if set(data) - {"idempotency_key"}:
            raise MarketplaceCommercialValidationError(
                "Rollback proposal принимает только idempotency_key"
            )
        proposal = MarketplaceCommercialService.create_rollback_proposal(
            seller_id=seller_id,
            operation_id=operation_id,
            idempotency_key=data.get("idempotency_key") or None,
            created_by_user_id=getattr(current_user, "id", None),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="rollback_proposal")
    if _wants_json():
        return jsonify({"success": True, "proposal": _document(proposal)}), 201
    flash("Rollback proposal создан; восстановление требует второго подтверждения", "success")
    return redirect(url_for(
        "marketplace_commercial.detail",
        proposal_id=proposal.id,
    ))


def register_marketplace_commercial_routes(app) -> None:
    app.register_blueprint(marketplace_commercial_bp)
