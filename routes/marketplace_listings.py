"""Seller-facing unified marketplace listing read model."""

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

from models import MarketplaceCommercialProposal, db
from services.marketplace_accounts import MarketplaceAccountService
from services.marketplace_listings import (
    MarketplaceListingError,
    MarketplaceListingService,
)
from services.marketplace_warehouses import MarketplaceWarehouseService


marketplace_listings_bp = Blueprint(
    "marketplace_listings",
    __name__,
    url_prefix="/marketplaces/listings",
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
        return value if isinstance(value, dict) else {}
    return request.form.to_dict(flat=True)


def _integer(value: Any, field_name: str, default: Optional[int] = None) -> int:
    if value in (None, "") and default is not None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} должен быть целым числом")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise ValueError(f"{field_name} должен быть целым числом")
    if parsed <= 0:
        raise ValueError(f"{field_name} должен быть положительным")
    return parsed


def _boolean(value: Any, field_name: str, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "on", "yes"}:
            return True
        if normalized in {"0", "false", "off", "no"}:
            return False
    raise ValueError(f"{field_name} должен быть boolean")


def _payload_integer(data: Dict[str, Any], field_name: str, default: int) -> int:
    value = data.get(field_name)
    if request.is_json and value is not None and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise ValueError(f"{field_name} должен быть целым числом")
    return _integer(value, field_name, default)


def _payload_boolean(data: Dict[str, Any], field_name: str, default: bool) -> bool:
    value = data.get(field_name)
    if request.is_json and value is not None and not isinstance(value, bool):
        raise ValueError(f"{field_name} должен быть boolean")
    return _boolean(value, field_name, default)


def _error_response(error: Exception, status_code: int = 400):
    if isinstance(error, MarketplaceListingError):
        status_code = error.status_code
        code = error.code
    else:
        code = "invalid_marketplace_listing_request"
    if _wants_json():
        return jsonify({
            "success": False,
            "error": str(error),
            "code": code,
        }), status_code
    return render_template(
        "marketplace_listing_error.html",
        error=str(error),
    ), status_code


def _listing_filters() -> Dict[str, Any]:
    return {
        "marketplace_code": request.args.get("marketplace") or None,
        "account_id": (
            _integer(request.args.get("account_id"), "account_id")
            if request.args.get("account_id") else None
        ),
        "normalized_status": request.args.get("status") or None,
        "include_unavailable": _boolean(
            request.args.get("include_unavailable"),
            "include_unavailable",
            False,
        ),
        "search": request.args.get("search") or None,
        "page": _integer(request.args.get("page"), "page", 1),
        "per_page": _integer(request.args.get("per_page"), "per_page", 50),
    }


@marketplace_listings_bp.route("/")
@login_required
def index():
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        filters = _listing_filters()
        pagination = MarketplaceListingService.list_listings(
            seller_id=seller_id,
            **filters,
        )
        accounts = MarketplaceAccountService.list_accounts(
            seller_id=seller_id,
        )
        latest_syncs = MarketplaceListingService.latest_syncs(
            seller_id=seller_id,
        )
    except (MarketplaceListingError, ValueError) as exc:
        return _error_response(exc)
    return render_template(
        "marketplace_listings.html",
        pagination=pagination,
        listings=pagination.items,
        accounts=accounts,
        latest_syncs=latest_syncs,
        filters=filters,
        ozon_enabled=bool(
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        ),
    )


@marketplace_listings_bp.route("/api")
@login_required
def list_api():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        pagination = MarketplaceListingService.list_listings(
            seller_id=seller_id,
            **_listing_filters(),
        )
    except (MarketplaceListingError, ValueError) as exc:
        return _error_response(exc)
    return jsonify({
        "success": True,
        "items": [item.to_public_dict() for item in pagination.items],
        "pagination": {
            "page": pagination.page,
            "per_page": pagination.per_page,
            "pages": pagination.pages,
            "total": pagination.total,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        },
    })


@marketplace_listings_bp.route("/<int:listing_id>")
@login_required
def detail(listing_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        listing = MarketplaceListingService.get_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
    except MarketplaceListingError as exc:
        return _error_response(exc)
    if _wants_json():
        warehouse_stocks = (
            MarketplaceWarehouseService.list_listing_stocks(
                seller_id=seller_id,
                listing_id=listing.id,
            )
            if listing.marketplace and listing.marketplace.code == "ozon"
            else []
        )
        return jsonify({
            "success": True,
            "listing": listing.to_public_dict(detail=True),
            "warehouse_stocks": [row.to_public_dict() for row in warehouse_stocks],
        })
    warehouse_stocks = []
    proposals = []
    if listing.marketplace and listing.marketplace.code == "ozon":
        warehouse_stocks = MarketplaceWarehouseService.list_listing_stocks(
            seller_id=seller_id,
            listing_id=listing.id,
            include_unavailable=True,
        )
        proposals = MarketplaceCommercialProposal.query.filter_by(
            seller_id=seller_id,
            account_id=listing.account_id,
            listing_id=listing.id,
        ).order_by(
            MarketplaceCommercialProposal.created_at.desc(),
            MarketplaceCommercialProposal.id.desc(),
        ).limit(20).all()
    return render_template(
        "marketplace_listing_detail.html",
        listing=listing,
        listing_data=listing.to_public_dict(detail=True),
        warehouse_stocks=warehouse_stocks,
        commercial_proposals=proposals,
        commercial_feature_enabled=bool(
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        ),
        commercial_write_enabled=bool(
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
            and current_app.config.get(
                "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED",
                False,
            )
        ),
    )


@marketplace_listings_bp.route("/accounts/<int:account_id>/sync", methods=["POST"])
@login_required
def sync_account(account_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
        if _wants_json():
            return jsonify({
                "success": False,
                "error": "Синхронизация Ozon отключена feature flag",
                "code": "ozon_feature_disabled",
            }), 404
        return render_template(
            "marketplace_listing_error.html",
            error="Синхронизация Ozon отключена feature flag",
        ), 404
    data = _payload()
    try:
        run = MarketplaceListingService.sync_ozon_account(
            seller_id=seller_id,
            account_id=account_id,
            max_pages=_payload_integer(data, "max_pages", 5),
            force_restart=_payload_boolean(data, "force_restart", False),
        )
    except (MarketplaceListingError, ValueError) as exc:
        return _error_response(exc)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Ozon catalog sync failed seller_id=%s account_id=%s",
            seller_id,
            account_id,
        )
        return _error_response(
            MarketplaceListingError("Не удалось синхронизировать каталог Ozon"),
            500,
        )
    message = (
        "Каталог Ozon полностью синхронизирован"
        if run.status == "completed"
        else "Синхронизация сохранена; продолжите следующий пакет"
    )
    if _wants_json():
        return jsonify({
            "success": True,
            "message": message,
            "sync": run.to_public_dict(),
        })
    flash(message, "success")
    return redirect(url_for("marketplace_listings.index", account_id=account_id))


def register_marketplace_listing_routes(app) -> None:
    app.register_blueprint(marketplace_listings_bp)
