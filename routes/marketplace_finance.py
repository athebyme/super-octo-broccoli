"""Seller-scoped read-only Ozon finance snapshot UI and API."""

import logging

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from services.marketplace_accounts import MarketplaceAccountError, MarketplaceAccountService
from services.marketplace_finance import (
    MarketplaceFinanceError,
    MarketplaceFinanceService,
    MarketplaceFinanceValidationError,
)


logger = logging.getLogger("marketplace_finance_routes")
marketplace_finance_bp = Blueprint("marketplace_finance", __name__)


def _feature_enabled() -> bool:
    return bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False))


def _seller_id() -> int:
    seller = current_user.seller
    if seller is None:
        raise MarketplaceFinanceValidationError("Нет привязки к продавцу")
    return seller.id


def _positive_query(name: str, default=None, *, maximum=None) -> int:
    raw = request.args.get(name)
    if raw is None and default is not None:
        return default
    if not isinstance(raw, str) or not raw.isdigit() or raw.startswith("0"):
        raise MarketplaceFinanceValidationError(
            f"{name} должен быть положительным целым числом"
        )
    value = int(raw)
    if maximum is not None and value > maximum:
        raise MarketplaceFinanceValidationError(
            f"{name} превышает лимит {maximum}"
        )
    return value


def _optional_positive_query(name: str):
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    return _positive_query(name)


def _body(allowed: set) -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise MarketplaceFinanceValidationError("JSON body должен быть объектом")
    if "account_id" in payload or "marketplace" in payload:
        raise MarketplaceFinanceValidationError(
            "Marketplace scope задаётся только query-параметрами"
        )
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise MarketplaceFinanceValidationError(
            "Неизвестные поля: " + ", ".join(unknown)
        )
    return payload


def _strict_bool(value, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise MarketplaceFinanceValidationError(
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
    return MarketplaceAccountService.get_owned_account(
        seller_id=seller_id,
        account_id=_positive_query("account_id"),
        marketplace_code="ozon",
    )


def _known_error(exc):
    return jsonify({
        "error": str(exc),
        "code": getattr(exc, "code", "marketplace_finance_error"),
    }), getattr(exc, "status_code", 400)


@marketplace_finance_bp.get("/marketplaces/finance")
@login_required
def page():
    if not _feature_enabled():
        return redirect(url_for("finances_page"))
    try:
        seller_id = _seller_id()
        accounts = _accounts(seller_id)
        if not accounts:
            return redirect(url_for("marketplace_accounts.index"))
        selected = _selected_account(seller_id, accounts)
        return render_template(
            "marketplace_finance.html",
            ozon_accounts=[account.to_public_dict() for account in accounts],
            selected_account=selected.to_public_dict(),
        )
    except (MarketplaceFinanceError, MarketplaceAccountError):
        return redirect(url_for("marketplace_accounts.index"))


@marketplace_finance_bp.get("/marketplaces/api/finance")
@login_required
def list_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        data = MarketplaceFinanceService.list_facts(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            page=_positive_query("page", 1, maximum=100_000),
            per_page=_positive_query("per_page", 50, maximum=100),
            period_code=request.args.get("period", "30d"),
            category=request.args.get("category") or None,
            amount_sign=request.args.get("sign") or None,
            type_id=_optional_positive_query("type_id"),
            search=request.args.get("search", ""),
        )
        return jsonify({"success": True, "data": data})
    except (MarketplaceFinanceError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon finance list failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось загрузить финансы Ozon"}), 500


@marketplace_finance_bp.get("/marketplaces/api/finance/<int:fact_id>")
@login_required
def detail_api(fact_id):
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        fact = MarketplaceFinanceService.get_fact(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            fact_id=fact_id,
        )
        return jsonify({"success": True, "data": fact.to_public_dict(detail=True)})
    except (MarketplaceFinanceError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon finance detail failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось загрузить начисление Ozon"}), 500


@marketplace_finance_bp.post("/marketplaces/api/finance/sync")
@login_required
def sync_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        payload = _body({"period", "force", "max_pages"})
        force = _strict_bool(payload.get("force", False), "force")
        max_pages = payload.get("max_pages", 5)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool):
            raise MarketplaceFinanceValidationError(
                "max_pages должен быть целым числом"
            )
        run = MarketplaceFinanceService.sync_account(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            period_code=payload.get("period", "30d"),
            force=force,
            max_pages=max_pages,
        )
        return jsonify({"success": True, "data": run.to_public_dict()})
    except (MarketplaceFinanceError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon finance sync failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось синхронизировать финансы Ozon"}), 500


def register_marketplace_finance_routes(app):
    app.register_blueprint(marketplace_finance_bp)
