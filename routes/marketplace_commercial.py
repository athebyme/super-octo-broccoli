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
from flask_wtf.csrf import generate_csrf

from models import db
from services.marketplace_accounts import MarketplaceAccountService
from services.marketplace_commercial import (
    MarketplaceCommercialConflict,
    MarketplaceCommercialError,
    MarketplaceCommercialService,
    MarketplaceCommercialValidationError,
)
from services.marketplace_warehouses import (
    MarketplaceWarehouseError,
    MarketplaceWarehouseService,
)
from services import ozon_warehouse_reads as warehouse_reads
from services.marketplace_commercial_display import commercial_context, product_summary
from services import ozon_write_quarantine as write_quarantine


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


def _batch_approval_items() -> list:
    if request.is_json:
        data = _payload()
        if set(data) != {"items", "confirm_write"}:
            raise MarketplaceCommercialValidationError(
                "Batch approve принимает items и confirm_write"
            )
        if not _boolean(data.get("confirm_write"), "confirm_write"):
            raise MarketplaceCommercialValidationError(
                "Batch approve требует явное подтверждение write"
            )
        return data["items"]
    if set(request.form) - {"csrf_token", "proposal", "confirm_write"}:
        raise MarketplaceCommercialValidationError(
            "Batch approve form содержит неподдерживаемые поля"
        )
    if not _boolean(request.form.get("confirm_write"), "confirm_write"):
        raise MarketplaceCommercialValidationError(
            "Batch approve требует явное подтверждение write"
        )
    items = []
    for index, raw in enumerate(request.form.getlist("proposal")):
        if not isinstance(raw, str) or raw.count(":") != 1:
            raise MarketplaceCommercialValidationError(
                f"proposal[{index}] имеет неверный формат"
            )
        proposal_id, expected_version = raw.split(":", 1)
        if not (
            proposal_id.isascii()
            and proposal_id.isdigit()
            and expected_version.isascii()
            and expected_version.isdigit()
        ):
            raise MarketplaceCommercialValidationError(
                f"proposal[{index}] имеет неверный формат"
            )
        items.append({
            "proposal_id": int(proposal_id),
            "expected_version": int(expected_version),
        })
    return items


def _proposal_ids_query() -> Optional[list[int]]:
    values = request.args.getlist("proposal_ids")
    if not values:
        return None
    if len(values) != 1:
        raise MarketplaceCommercialValidationError(
            "proposal_ids должен быть передан один раз"
        )
    raw = values[0]
    if not isinstance(raw, str) or not 1 <= len(raw) <= 1200:
        raise MarketplaceCommercialValidationError(
            "proposal_ids должен содержать точный список заявок"
        )
    parts = raw.split(",")
    if not 1 <= len(parts) <= 100 or any(
        not part.isascii() or not part.isdigit() for part in parts
    ):
        raise MarketplaceCommercialValidationError(
            "proposal_ids должен содержать 1..100 положительных ID"
        )
    parsed = [int(part) for part in parts]
    if any(value <= 0 for value in parsed) or len(set(parsed)) != len(parsed):
        raise MarketplaceCommercialValidationError(
            "proposal_ids содержит неверные или повторяющиеся ID"
        )
    return parsed


def _bulk_listing_ids_query() -> Optional[list[int]]:
    values = request.args.getlist("bulk_listing_ids")
    if not values:
        return None
    if len(values) != 1:
        raise MarketplaceCommercialValidationError(
            "bulk_listing_ids должен быть передан один раз"
        )
    raw = values[0]
    if not isinstance(raw, str) or not 1 <= len(raw) <= 1200:
        raise MarketplaceCommercialValidationError(
            "bulk_listing_ids должен содержать точный список товаров"
        )
    parts = raw.split(",")
    if not 1 <= len(parts) <= 100 or any(
        not part.isascii() or not part.isdigit() for part in parts
    ):
        raise MarketplaceCommercialValidationError(
            "bulk_listing_ids должен содержать 1..100 положительных ID"
        )
    parsed = [int(part) for part in parts]
    if any(value <= 0 for value in parsed) or len(set(parsed)) != len(parsed):
        raise MarketplaceCommercialValidationError(
            "bulk_listing_ids содержит неверные или повторяющиеся ID"
        )
    return parsed


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
    data["write_quarantine"] = write_quarantine.hold_document(
        write_quarantine.proposal_hold(proposal)
    )
    reason = MarketplaceCommercialService.target_unavailable_reason(proposal)
    data["target_available"] = reason is None
    data["target_unavailable_reason"] = reason
    # The list service eagerly loads the listing; no provider or per-row query.
    listing = proposal.listing
    data["product"] = product_summary(listing) if listing and (
        listing.seller_id == proposal.seller_id and listing.account_id == proposal.account_id
        and listing.marketplace_id == proposal.marketplace_id
    ) else None
    operation = proposal.operation
    data["operation"] = operation.to_public_dict(detail=True) if operation else None
    if operation and operation.snapshot:
        data["operation"]["snapshot"] = operation.snapshot.to_public_dict()
    return data


def _error_response(
    error: Exception,
    *,
    force_json: bool = False,
    workspace_return_fallback: bool = False,
):
    if isinstance(error, (MarketplaceCommercialError, MarketplaceWarehouseError, warehouse_reads.WarehouseReadError)):
        status_code = error.status_code
        code = error.code
    else:
        status_code = 400
        code = "invalid_marketplace_commercial_request"
    hold = getattr(error, "write_quarantine", None)
    if force_json or _wants_json():
        payload = {
            "success": False,
            "error": str(error),
            "code": code,
        }
        if code == "ozon_write_quarantined" and isinstance(hold, dict):
            payload["write_quarantine"] = hold
        next_attempt_at = getattr(error, "next_attempt_at", None)
        if next_attempt_at:
            payload["next_attempt_at"] = next_attempt_at
        return jsonify(payload), status_code
    return render_template(
        "marketplace_commercial_error.html",
        error=str(error),
        code=code,
        write_quarantine=hold if code == "ozon_write_quarantined" else None,
        workspace_return_fallback=workspace_return_fallback,
    ), status_code


def _write_failure(error: Exception, *, seller_id: int, action: str):
    if isinstance(error, (MarketplaceCommercialError, MarketplaceWarehouseError, warehouse_reads.WarehouseReadError)):
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
@marketplace_commercial_bp.route("/classic", methods=["GET"], endpoint="classic")
@login_required
def index():
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    workspace_return_fallback = "bulk_listing_ids" in request.args
    try:
        _require_feature()
        account_id = _integer(
            request.args.get("account_id"),
            "account_id",
            required=False,
        )
        proposal_kind = request.args.get("proposal_kind") or None
        status = request.args.get("status") or None
        page = _integer(request.args.get("page"), "page", default=1)
        per_page = _integer(request.args.get("per_page"), "per_page", default=50)
        proposal_ids = _proposal_ids_query()
        bulk_listing_ids = _bulk_listing_ids_query()
        bulk_selection = None
        if bulk_listing_ids is not None:
            if (
                proposal_ids is not None
                or len(request.args.getlist("account_id")) != 1
                or set(request.args.keys()) != {"account_id", "bulk_listing_ids"}
                or request.endpoint.endswith(".classic")
            ):
                raise MarketplaceCommercialValidationError(
                    "Точный набор товаров принимает только account_id и bulk_listing_ids"
                )
            account, listings = MarketplaceCommercialService._bulk_price_scope(
                seller_id=seller_id,
                account_id=account_id,
                listing_ids=bulk_listing_ids,
                require_available=True,
                include_imported=False,
            )
            if not account.is_active:
                raise MarketplaceCommercialConflict(
                    "Для массовой подготовки выберите активный кабинет Ozon"
                )
            bulk_selection = {
                "account_id": account_id,
                "items": [
                    {
                        "id": item.id,
                        "offer_id": item.offer_id,
                        "title": MarketplaceCommercialService._safe_text(
                            item.title, maximum=500
                        ),
                        "account_id": item.account_id,
                        "is_available": bool(item.is_available),
                        "is_archived": bool(item.is_archived),
                    }
                    for item in listings
                ],
            }
            if _wants_json():
                return jsonify(bulk_selection)
        if proposal_ids is not None:
            if (
                account_id is None
                or proposal_kind != "price"
                or status != "pending_review"
                or page != 1
                or per_page not in {50, len(proposal_ids)}
            ):
                raise MarketplaceCommercialValidationError(
                    "Точный набор заявок требует магазин, тип price и статус pending_review"
                )
            per_page = len(proposal_ids)
        pagination = MarketplaceCommercialService.list_proposals(
            seller_id=seller_id,
            account_id=account_id,
            proposal_kind=proposal_kind,
            status=status,
            page=page,
            per_page=per_page,
            proposal_ids=proposal_ids,
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
        warehouse_refresh = (
            warehouse_reads.status_for_scope(
                seller_id=seller_id, kind="warehouses", account_id=account_id,
            ) if account_id is not None else None
        )
    except (MarketplaceCommercialError, MarketplaceWarehouseError) as exc:
        return _error_response(exc, workspace_return_fallback=workspace_return_fallback)
    proposal_docs = {item.id: _document(item) for item in pagination.items}
    if _wants_json():
        return jsonify({
            "success": True,
            "items": list(proposal_docs.values()),
            "accounts": [{"id": a.id, "label": a.label, "is_active": bool(a.is_active)} for a in accounts],
            "write_enabled": _write_enabled(),
            "warehouses": [item.to_public_dict() for item in warehouses],
            "pagination": {
                "page": pagination.page,
                "per_page": pagination.per_page,
                "pages": pagination.pages,
                "total": pagination.total,
            },
            "exact_selection": proposal_ids is not None,
        })
    return render_template(
        "marketplace_commercial_classic.html" if request.endpoint.endswith(".classic") else "marketplace_commercial.html",
        proposals=pagination.items,
        proposal_docs=proposal_docs,
        pagination=pagination,
        accounts=accounts,
        warehouses=warehouses,
        warehouse_refresh=warehouse_refresh,
        filters={
            "account_id": account_id,
            "proposal_kind": proposal_kind,
            "status": status,
            "proposal_ids": proposal_ids,
        },
        bulk_selection=bulk_selection,
        write_enabled=_write_enabled(),
    )


@marketplace_commercial_bp.route("/listings/<int:listing_id>/context", methods=["GET"])
@login_required
def listing_context(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        data = commercial_context(seller_id=seller_id, listing_id=listing_id)
    except MarketplaceCommercialError as exc:
        return _error_response(exc, force_json=True)
    return jsonify({"success": True, **data})


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
        return jsonify({"success": True, "proposal": document, "write_enabled": _write_enabled()})
    return render_template(
        "marketplace_commercial_detail_classic.html" if request.endpoint.endswith(".classic_detail") else "marketplace_commercial_detail.html",
        proposal=proposal,
        proposal_data=document,
        write_enabled=_write_enabled(),
    )


@marketplace_commercial_bp.route("/<int:proposal_id>", methods=["GET"])
@marketplace_commercial_bp.route("/classic/<int:proposal_id>", methods=["GET"], endpoint="classic_detail")
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
        refresh = warehouse_reads.enqueue_refresh(
            seller_id=seller_id, kind="warehouses", account_id=account_id,
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="warehouse_sync")
    if not _wants_json():
        flash("Обновление складов поставлено в очередь; последний полный список сохранён.", "info")
        return redirect(url_for("marketplace_commercial.classic", account_id=account_id), code=303)
    response = jsonify({"success": True, "refresh": refresh})
    response.status_code = 202
    response.headers["Location"] = url_for("marketplace_commercial.refresh_status", job_id=refresh["id"])
    return response


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
        refresh = warehouse_reads.enqueue_refresh(
            seller_id=seller_id, kind="fbs_stock", listing_id=listing_id,
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="stock_refresh")
    if not _wants_json():
        flash("Обновление FBS/rFBS остатков поставлено в очередь; последний полный снимок сохранён.", "info")
        return redirect(url_for("marketplace_listings.detail", listing_id=listing_id), code=303)
    response = jsonify({"success": True, "refresh": refresh})
    response.status_code = 202
    response.headers["Location"] = url_for("marketplace_commercial.refresh_status", job_id=refresh["id"])
    return response


@marketplace_commercial_bp.route("/accounts/<int:account_id>/warehouses/refresh", methods=["GET"])
@login_required
def warehouse_refresh_status(account_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        refresh = warehouse_reads.status_for_scope(
            seller_id=seller_id, kind="warehouses", account_id=account_id,
        )
    except Exception as exc:
        return _error_response(exc, force_json=True)
    return jsonify({"success": True, "refresh": refresh, "csrf_token": generate_csrf()})


@marketplace_commercial_bp.route("/listings/<int:listing_id>/stocks/refresh", methods=["GET"])
@login_required
def stock_refresh_status(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        refresh = warehouse_reads.status_for_scope(
            seller_id=seller_id, kind="fbs_stock", listing_id=listing_id,
        )
    except Exception as exc:
        return _error_response(exc, force_json=True)
    return jsonify({"success": True, "refresh": refresh, "csrf_token": generate_csrf()})


@marketplace_commercial_bp.route("/refreshes/<int:job_id>", methods=["GET"])
@login_required
def refresh_status(job_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        refresh = warehouse_reads.status_for_job(seller_id=seller_id, job_id=job_id)
    except Exception as exc:
        return _error_response(exc, force_json=True)
    return jsonify({"success": True, "refresh": refresh, "csrf_token": generate_csrf()})


@marketplace_commercial_bp.route("/refreshes/<int:job_id>/cancel", methods=["POST"])
@login_required
def cancel_refresh(job_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        if _payload():
            raise MarketplaceCommercialValidationError("Отмена обновления не принимает полей")
        refresh = warehouse_reads.cancel_refresh(seller_id=seller_id, job_id=job_id)
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="refresh_cancel")
    return jsonify({"success": True, "refresh": refresh})


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
    "/price-proposals/bulk-preview",
    methods=["POST"],
)
@login_required
def preview_bulk_price_proposals():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        if not request.is_json:
            raise MarketplaceCommercialValidationError(
                "Массовое сравнение принимает JSON"
            )
        data = _payload()
        allowed = {
            "account_id", "listing_ids", "mode", "value",
            "allow_price_decrease", "allow_large_change", "guardrail_note",
        }
        if set(data) != allowed:
            raise MarketplaceCommercialValidationError(
                "Массовое сравнение содержит неподдерживаемые или отсутствующие поля"
            )
        result = MarketplaceCommercialService.preview_bulk_price_proposals(
            seller_id=seller_id,
            account_id=_integer(data.get("account_id"), "account_id"),
            listing_ids=data.get("listing_ids"),
            mode=data.get("mode"),
            value=data.get("value"),
            created_by_user_id=getattr(current_user, "id", None),
            allow_price_decrease=_boolean(
                data.get("allow_price_decrease"),
                "allow_price_decrease",
            ),
            allow_large_change=_boolean(
                data.get("allow_large_change"),
                "allow_large_change",
            ),
            guardrail_note=data.get("guardrail_note"),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="bulk_price_preview")
    return jsonify(result)


@marketplace_commercial_bp.route(
    "/price-proposals/bulk-confirm",
    methods=["POST"],
)
@login_required
def confirm_bulk_price_proposals():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_feature()
        if not request.is_json:
            raise MarketplaceCommercialValidationError(
                "Сохранение массовых заявок принимает JSON"
            )
        data = _payload()
        if set(data) != {"preview_token", "confirm_save"}:
            raise MarketplaceCommercialValidationError(
                "Подтверждение принимает preview_token и confirm_save"
            )
        if not _boolean(data.get("confirm_save"), "confirm_save"):
            raise MarketplaceCommercialValidationError(
                "Сохранение требует явного подтверждения"
            )
        result = MarketplaceCommercialService.confirm_bulk_price_proposals(
            seller_id=seller_id,
            created_by_user_id=getattr(current_user, "id", None),
            preview_token=data.get("preview_token"),
        )
        proposal_ids = [item["proposal"].id for item in result["items"]]
        pending_review = bool(proposal_ids) and all(
            item["status"] == "pending_review" for item in result["items"]
        )
        review_url = url_for(
            "marketplace_commercial.index",
            account_id=(result["items"][0]["proposal"].account_id if proposal_ids else None),
            proposal_kind="price" if proposal_ids else None,
            status="pending_review" if proposal_ids else None,
            proposal_ids=",".join(str(value) for value in proposal_ids) if proposal_ids else None,
        ) if pending_review else None
        result["review_url"] = review_url
        result["proposal_ids"] = proposal_ids
        result["items"] = [
            {
                "listing_id": item["listing_id"],
                "status": item["status"],
                "review_url": review_url,
                "proposal": _document(item["proposal"]),
            }
            for item in result["items"]
        ]
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="bulk_price_confirm")
    return jsonify({"success": True, **result}), (200 if result["replayed"] else 201)


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
        if set(data) - {"expected_version", "confirm_write"}:
            raise MarketplaceCommercialValidationError(
                "Approve принимает expected_version и confirm_write"
            )
        if not _boolean(data.get("confirm_write"), "confirm_write"):
            raise MarketplaceCommercialValidationError(
                "Approve требует явное подтверждение write"
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


@marketplace_commercial_bp.route("/batch-approve", methods=["POST"])
@login_required
def batch_approve():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _require_write()
        proposals = MarketplaceCommercialService.approve_proposals(
            seller_id=seller_id,
            items=_batch_approval_items(),
            reviewed_by_user_id=getattr(current_user, "id", None),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="batch_approve")
    if _wants_json():
        return jsonify({
            "success": True,
            "items": [_document(proposal) for proposal in proposals],
        })
    flash(
        f"Batch подтверждён: {len(proposals)} proposals, один exact-set Ozon write",
        "success",
    )
    return redirect(url_for(
        "marketplace_commercial.index",
        account_id=proposals[0].account_id,
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
