"""Seller-scoped Ozon reviews/questions UI and local draft API."""

import logging

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from services.marketplace_accounts import MarketplaceAccountError, MarketplaceAccountService
from services.marketplace_inbox import (
    MarketplaceInboxError,
    MarketplaceInboxService,
    MarketplaceInboxValidationError,
)


logger = logging.getLogger("marketplace_inbox_routes")
marketplace_inbox_bp = Blueprint("marketplace_inbox", __name__)


def _feature_enabled() -> bool:
    return bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False))


def _seller_id() -> int:
    seller = current_user.seller
    if seller is None:
        raise MarketplaceInboxValidationError("Нет привязки к продавцу")
    return seller.id


def _positive_query(name: str, default=None, *, maximum=None) -> int:
    if len(request.args.getlist(name)) > 1:
        raise MarketplaceInboxValidationError(
            f"{name} должен быть указан ровно один раз"
        )
    raw = request.args.get(name)
    if raw is None and default is not None:
        return default
    if (
        not isinstance(raw, str)
        or not raw.isascii()
        or not raw.isdecimal()
        or raw.startswith("0")
    ):
        raise MarketplaceInboxValidationError(
            f"{name} должен быть положительным целым числом"
        )
    value = int(raw)
    if maximum is not None and value > maximum:
        raise MarketplaceInboxValidationError(f"{name} превышает лимит {maximum}")
    return value


def _validate_query(allowed: set) -> None:
    unknown = sorted(set(request.args) - allowed)
    if unknown:
        raise MarketplaceInboxValidationError(
            "Неизвестные query-параметры: " + ", ".join(unknown)
        )
    duplicates = sorted(
        name for name in request.args if len(request.args.getlist(name)) > 1
    )
    if duplicates:
        raise MarketplaceInboxValidationError(
            "Query-параметры нельзя повторять: " + ", ".join(duplicates)
        )


def _optional_positive_query(name: str):
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    return _positive_query(name)


def _body(allowed: set) -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise MarketplaceInboxValidationError("JSON body должен быть объектом")
    if "account_id" in payload or "marketplace" in payload:
        raise MarketplaceInboxValidationError(
            "Marketplace scope задаётся только query-параметрами"
        )
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise MarketplaceInboxValidationError(
            "Неизвестные поля: " + ", ".join(unknown)
        )
    return payload


def _strict_bool(value, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise MarketplaceInboxValidationError(f"{field_name} должен быть boolean")
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
    if request.args.get("account_id") is None:
        return accounts[0] if accounts else None
    return MarketplaceAccountService.get_owned_account(
        seller_id=seller_id,
        account_id=_positive_query("account_id"),
        marketplace_code="ozon",
    )


def _known_error(exc):
    return jsonify({
        "error": str(exc),
        "code": getattr(exc, "code", "marketplace_inbox_error"),
    }), getattr(exc, "status_code", 400)


@marketplace_inbox_bp.get("/marketplaces/reviews")
@login_required
def page():
    if not _feature_enabled():
        return redirect(url_for("reviews_page"))
    try:
        _validate_query({"account_id", "source_kind"})
        if request.args.get("source_kind") not in (None, "review", "question"):
            raise MarketplaceInboxValidationError(
                "source_kind должен быть review или question"
            )
        seller_id = _seller_id()
        accounts = _accounts(seller_id)
        if not accounts:
            return redirect(url_for("marketplace_accounts.index"))
        selected = _selected_account(seller_id, accounts)
        return render_template(
            "marketplace_inbox.html",
            ozon_accounts=[account.to_public_dict() for account in accounts],
            selected_account=selected.to_public_dict(),
        )
    except (MarketplaceInboxError, MarketplaceAccountError):
        return redirect(url_for("marketplace_accounts.index"))


@marketplace_inbox_bp.get("/marketplaces/api/reviews")
@login_required
def list_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({
            "account_id", "source_kind", "page", "per_page", "status",
            "listing_id", "search",
        })
        data = MarketplaceInboxService.list_items(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            source_kind=request.args.get("source_kind", "review"),
            page=_positive_query("page", 1, maximum=100_000),
            per_page=_positive_query("per_page", 30, maximum=100),
            provider_status=request.args.get("status") or None,
            listing_id=_optional_positive_query("listing_id"),
            search=request.args.get("search", ""),
        )
        return jsonify({"success": True, "data": data})
    except (MarketplaceInboxError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon inbox list failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось загрузить отзывы Ozon"}), 500


@marketplace_inbox_bp.post("/marketplaces/api/reviews/sync")
@login_required
def sync_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({"account_id"})
        payload = _body({"source_kind", "force", "max_pages"})
        force = _strict_bool(payload.get("force", False), "force")
        max_pages = payload.get("max_pages", 5)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool):
            raise MarketplaceInboxValidationError(
                "max_pages должен быть целым числом"
            )
        run = MarketplaceInboxService.sync_kind(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            source_kind=payload.get("source_kind", "review"),
            force=force,
            max_pages=max_pages,
        )
        return jsonify({"success": True, "data": run.to_public_dict()})
    except (MarketplaceInboxError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon inbox sync failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось синхронизировать отзывы Ozon"}), 500


@marketplace_inbox_bp.post("/marketplaces/api/reviews/<int:item_id>/draft")
@login_required
def draft_api(item_id):
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({"account_id"})
        payload = _body({"generation_mode"})
        draft = MarketplaceInboxService.create_reply_draft(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            item_id=item_id,
            generation_mode=payload.get("generation_mode", "ai"),
            created_by_user_id=current_user.id,
        )
        return jsonify({"success": True, "data": draft.to_public_dict()})
    except (MarketplaceInboxError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon reply draft failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось подготовить черновик"}), 500


def register_marketplace_inbox_routes(app):
    app.register_blueprint(marketplace_inbox_bp)
