"""Seller-scoped read-only Ozon orders, returns and cancellations UI/API."""

import logging

from services.ozon_read_requests import enqueue_read, read_status

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from services.marketplace_accounts import MarketplaceAccountError, MarketplaceAccountService
from services.marketplace_fulfillment import (
    MarketplaceFulfillmentError,
    MarketplaceFulfillmentService,
    MarketplaceFulfillmentValidationError,
)


logger = logging.getLogger("marketplace_fulfillment_routes")
marketplace_fulfillment_bp = Blueprint(
    "marketplace_fulfillment",
    __name__,
)


def _feature_enabled() -> bool:
    return bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False))


def _seller_id() -> int:
    seller = current_user.seller
    if seller is None:
        raise MarketplaceFulfillmentValidationError("Нет привязки к продавцу")
    return seller.id


def _positive_query(name: str, default=None, *, maximum=None) -> int:
    raw = request.args.get(name)
    if raw is None and default is not None:
        return default
    if (len(request.args.getlist(name)) > 1 or not isinstance(raw, str)
            or not raw.isascii() or not raw.isdecimal() or raw.startswith("0") or len(raw) > 18):
        raise MarketplaceFulfillmentValidationError(
            f"{name} должен быть положительным целым числом"
        )
    value = int(raw)
    if maximum is not None and value > maximum:
        raise MarketplaceFulfillmentValidationError(
            f"{name} превышает лимит {maximum}"
        )
    return value


def _validate_query(allowed):
    if set(request.args) - allowed or any(len(request.args.getlist(k)) != 1 for k in request.args):
        raise MarketplaceFulfillmentValidationError('Неизвестные или повторяющиеся параметры запроса')


def _compact_view():
    value = request.args.get('view')
    if value not in (None, 'compact'):
        raise MarketplaceFulfillmentValidationError('Неизвестное представление списка')
    return value == 'compact'


def _body(allowed: set) -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise MarketplaceFulfillmentValidationError("JSON body должен быть объектом")
    if "account_id" in payload or "marketplace" in payload:
        raise MarketplaceFulfillmentValidationError(
            "Marketplace scope задаётся только query-параметрами"
        )
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise MarketplaceFulfillmentValidationError(
            "Неизвестные поля: " + ", ".join(unknown)
        )
    return payload


def _strict_bool(value, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise MarketplaceFulfillmentValidationError(
            f"{field_name} должен быть boolean"
        )
    return value


def _accounts(seller_id: int) -> list:
    return [
        account
        for account in MarketplaceAccountService.list_accounts(
            seller_id=seller_id,
            marketplace_code="ozon",
        )
        if account.is_active
    ]


def _selected_account(seller_id: int, accounts: list):
    raw = request.args.get("account_id")
    if raw is None:
        return accounts[0] if accounts else None
    account_id = _positive_query("account_id")
    return MarketplaceAccountService.get_owned_account(
        seller_id=seller_id,
        account_id=account_id,
        marketplace_code="ozon",
    )


def _known_error(exc):
    return jsonify({
        "error": str(exc),
        "code": getattr(exc, "code", "marketplace_fulfillment_error"),
    }), getattr(exc, "status_code", 400)


def _page(page_kind: str):
    if not _feature_enabled():
        return redirect(url_for("analytics_page"))
    try:
        seller_id = _seller_id()
        accounts = _accounts(seller_id)
        if not accounts:
            return redirect(url_for("marketplace_accounts.index"))
        selected = _selected_account(seller_id, accounts)
        return render_template(
            "marketplace_fulfillment.html",
            page_kind=page_kind,
            ozon_accounts=[account.to_public_dict() for account in accounts],
            selected_account=selected.to_public_dict(),
        )
    except (MarketplaceFulfillmentError, MarketplaceAccountError):
        return redirect(url_for("marketplace_accounts.index"))


@marketplace_fulfillment_bp.get("/marketplaces/orders")
@login_required
def orders_page():
    return _page("orders")


@marketplace_fulfillment_bp.get("/marketplaces/returns")
@login_required
def returns_page():
    return _page("returns")


@marketplace_fulfillment_bp.get("/marketplaces/cancellations")
@login_required
def cancellations_page():
    return _page("cancellations")


@marketplace_fulfillment_bp.get("/marketplaces/api/orders")
@login_required
def orders_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id', 'page', 'per_page', 'period', 'fulfillment', 'status', 'search', 'view'})
        data = MarketplaceFulfillmentService.list_postings(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            page=_positive_query("page", 1, maximum=100_000),
            per_page=_positive_query("per_page", 50, maximum=100),
            period_code=request.args.get("period", "30d"),
            fulfillment_kind=request.args.get("fulfillment") or None,
            status=request.args.get("status") or None,
            search=request.args.get("search", ""),
            compact=_compact_view(),
        )
        return jsonify({"success": True, "data": data})
    except (MarketplaceFulfillmentError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon orders list failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось загрузить заказы Ozon"}), 500


@marketplace_fulfillment_bp.get("/marketplaces/api/orders/<int:posting_id>")
@login_required
def order_detail_api(posting_id):
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id', 'view', 'item_page', 'item_per_page'})
        posting = MarketplaceFulfillmentService.get_posting(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            posting_id=posting_id,
        )
        if _compact_view():
            from services.marketplace_fulfillment_display import posting_detail
            document = posting_detail(
                posting, account=posting.account,
                page=_positive_query('item_page', 1, maximum=100_000),
                per_page=_positive_query('item_per_page', 50, maximum=100),
            )
        else:
            document = posting.to_public_dict(detail=True)
        return jsonify({"success": True, "data": document})
    except (MarketplaceFulfillmentError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon order detail failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось загрузить отправление Ozon"}), 500


@marketplace_fulfillment_bp.get("/marketplaces/api/returns")
@login_required
def returns_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id', 'page', 'per_page', 'period', 'source', 'status', 'search', 'view'})
        data = MarketplaceFulfillmentService.list_returns(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            page=_positive_query("page", 1, maximum=100_000),
            per_page=_positive_query("per_page", 50, maximum=100),
            period_code=request.args.get("period", "30d"),
            source_kind=request.args.get("source") or None,
            status=request.args.get("status") or None,
            search=request.args.get("search", ""),
            compact=_compact_view(),
        )
        return jsonify({"success": True, "data": data})
    except (MarketplaceFulfillmentError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon returns list failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось загрузить возвраты Ozon"}), 500


@marketplace_fulfillment_bp.get("/marketplaces/api/cancellations")
@login_required
def cancellations_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id', 'page', 'per_page', 'period', 'source', 'status', 'search', 'view'})
        data = MarketplaceFulfillmentService.list_cancellations(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            page=_positive_query("page", 1, maximum=100_000),
            per_page=_positive_query("per_page", 50, maximum=100),
            period_code=request.args.get("period", "30d"),
            source_kind=request.args.get("source") or None,
            status=request.args.get("status") or None,
            search=request.args.get("search", ""),
            compact=_compact_view(),
        )
        return jsonify({"success": True, "data": data})
    except (MarketplaceFulfillmentError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon cancellations list failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось загрузить отмены Ozon"}), 500


@marketplace_fulfillment_bp.get("/marketplaces/api/fulfillment/sync")
@login_required
def sync_status_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id', 'period'})
        result = read_status(seller_id=_seller_id(), account_id=_positive_query('account_id'),
                             domain='fulfillment', period_code=request.args.get('period', '30d'))
        return jsonify({"success": True, "data": result})
    except (MarketplaceFulfillmentError, MarketplaceAccountError) as exc:
        return _known_error(exc)


@marketplace_fulfillment_bp.post("/marketplaces/api/fulfillment/sync")
@login_required
def sync_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id'})
        payload = _body({"period", "force", "max_pages"})
        force = _strict_bool(payload.get("force", False), "force")
        max_pages = payload.get("max_pages", 5)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool):
            raise MarketplaceFulfillmentValidationError(
                "max_pages должен быть целым числом"
            )
        MarketplaceFulfillmentService._positive_integer(max_pages, "max_pages", maximum=MarketplaceFulfillmentService.MAX_PAGES_PER_CALL)
        result = enqueue_read(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            period_code=payload.get("period", "30d"),
            force=force,
            domain='fulfillment',
        )
        return jsonify({"success": True, "data": result}), (202 if result["active"] else 200)
    except (MarketplaceFulfillmentError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon fulfillment sync failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось синхронизировать Ozon"}), 500


def register_marketplace_fulfillment_routes(app):
    app.register_blueprint(marketplace_fulfillment_bp)
