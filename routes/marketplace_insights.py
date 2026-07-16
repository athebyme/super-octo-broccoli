"""Seller-scoped Ozon quality and analytics UI/API routes."""

import logging

from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from models import MarketplaceQualityAssessment
from services.marketplace_accounts import MarketplaceAccountError, MarketplaceAccountService
from services.marketplace_analytics import (
    MarketplaceAnalyticsError,
    MarketplaceAnalyticsService,
)
from services.marketplace_quality import (
    MarketplaceQualityError,
    MarketplaceQualityService,
)


logger = logging.getLogger("marketplace_insights_routes")


def _feature_enabled() -> bool:
    return bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False))


def _seller_id() -> int:
    seller = current_user.seller
    if seller is None:
        raise MarketplaceQualityError("Нет привязки к продавцу")
    return seller.id


def _query_account_id() -> int:
    raw = request.args.get("account_id", "")
    if not isinstance(raw, str) or not raw.isdigit() or raw.startswith("0"):
        raise MarketplaceQualityError("account_id обязателен в query")
    return int(raw)


def _strict_query_integer(name: str, default: int, *, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    if not raw.isdigit() or raw.startswith("0"):
        raise MarketplaceQualityError(f"{name} должен быть положительным целым числом")
    value = int(raw)
    if value > maximum:
        raise MarketplaceQualityError(f"{name} превышает лимит {maximum}")
    return value


def _strict_bool(value, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise MarketplaceAnalyticsError(f"{field_name} должен быть boolean")
    return value


def _body(allowed: set) -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise MarketplaceQualityError("JSON body должен быть объектом")
    if "account_id" in payload or "marketplace" in payload:
        raise MarketplaceQualityError(
            "Marketplace scope задаётся только query-параметрами"
        )
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise MarketplaceQualityError(
            "Неизвестные поля: " + ", ".join(unknown)
        )
    return payload


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
    if raw is None and accounts:
        return accounts[0]
    account_id = _query_account_id()
    return MarketplaceAccountService.get_owned_account(
        seller_id=seller_id,
        account_id=account_id,
        marketplace_code="ozon",
    )


def _known_error(exc):
    return jsonify({"error": str(exc), "code": getattr(exc, "code", "error")}), getattr(exc, "status_code", 400)


def register_marketplace_insight_routes(app):
    @app.get("/marketplaces/quality")
    @login_required
    def marketplace_quality_page():
        if not _feature_enabled():
            flash("Поддержка Ozon пока выключена", "warning")
            return redirect(url_for("card_quality_page"))
        try:
            seller_id = _seller_id()
            accounts = _accounts(seller_id)
            if not accounts:
                flash("Сначала подключите кабинет Ozon", "warning")
                return redirect(url_for("marketplace_accounts_page"))
            account = _selected_account(seller_id, accounts)
            return render_template(
                "marketplace_quality.html",
                ozon_accounts=[item.to_public_dict() for item in accounts],
                selected_account=account.to_public_dict(),
            )
        except (MarketplaceQualityError, MarketplaceAccountError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("marketplace_accounts_page"))

    @app.get("/marketplaces/analytics")
    @login_required
    def marketplace_analytics_page():
        if not _feature_enabled():
            flash("Поддержка Ozon пока выключена", "warning")
            return redirect(url_for("analytics_page"))
        try:
            seller_id = _seller_id()
            accounts = _accounts(seller_id)
            if not accounts:
                flash("Сначала подключите кабинет Ozon", "warning")
                return redirect(url_for("marketplace_accounts_page"))
            account = _selected_account(seller_id, accounts)
            return render_template(
                "marketplace_analytics.html",
                ozon_accounts=[item.to_public_dict() for item in accounts],
                selected_account=account.to_public_dict(),
            )
        except (MarketplaceQualityError, MarketplaceAccountError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("marketplace_accounts_page"))

    @app.get("/marketplaces/api/quality")
    @login_required
    def marketplace_quality_list_api():
        if not _feature_enabled():
            return jsonify({"error": "Поддержка Ozon выключена"}), 404
        try:
            seller_id = _seller_id()
            account_id = _query_account_id()
            existing = MarketplaceQualityAssessment.query.filter(
                MarketplaceQualityAssessment.seller_id == seller_id,
                MarketplaceQualityAssessment.account_id == account_id,
            ).count()
            if existing == 0:
                MarketplaceQualityService.recompute_account(
                    seller_id=seller_id,
                    account_id=account_id,
                    limit=200,
                )
            result = MarketplaceQualityService.list_assessments(
                seller_id=seller_id,
                account_id=account_id,
                page=_strict_query_integer("page", 1, maximum=100000),
                per_page=_strict_query_integer("per_page", 50, maximum=100),
                severity=request.args.get("severity") or None,
                reason=request.args.get("reason") or None,
                sort_by=request.args.get("sort_by", "impact"),
                sort_dir=request.args.get("sort_dir", "desc"),
                search=request.args.get("search", ""),
            )
            return jsonify({"success": True, **result})
        except (MarketplaceQualityError, MarketplaceAccountError) as exc:
            return _known_error(exc)
        except Exception as exc:
            logger.exception(
                "Marketplace quality list failed: %s",
                type(exc).__name__,
            )
            return jsonify({"error": "Не удалось загрузить качество Ozon"}), 500

    @app.post("/marketplaces/api/quality/recompute")
    @login_required
    def marketplace_quality_recompute_api():
        if not _feature_enabled():
            return jsonify({"error": "Поддержка Ozon выключена"}), 404
        try:
            payload = _body({"listing_ids", "limit", "offset"})
            limit = payload.get("limit", 200)
            offset = payload.get("offset", 0)
            result = MarketplaceQualityService.recompute_account(
                seller_id=_seller_id(),
                account_id=_query_account_id(),
                listing_ids=payload.get("listing_ids"),
                limit=limit,
                offset=offset,
            )
            return jsonify({"success": True, "data": result})
        except (MarketplaceQualityError, MarketplaceAccountError) as exc:
            return _known_error(exc)
        except Exception as exc:
            logger.exception(
                "Marketplace quality recompute failed: %s",
                type(exc).__name__,
            )
            return jsonify({"error": "Не удалось пересчитать качество Ozon"}), 500

    @app.get("/marketplaces/api/quality/<int:listing_id>")
    @login_required
    def marketplace_quality_detail_api(listing_id):
        if not _feature_enabled():
            return jsonify({"error": "Поддержка Ozon выключена"}), 404
        try:
            assessment = MarketplaceQualityService.get_assessment(
                seller_id=_seller_id(),
                account_id=_query_account_id(),
                listing_id=listing_id,
            )
            return jsonify({"success": True, "data": assessment.to_public_dict()})
        except (MarketplaceQualityError, MarketplaceAccountError) as exc:
            return _known_error(exc)
        except Exception as exc:
            logger.exception(
                "Marketplace quality detail failed: %s",
                type(exc).__name__,
            )
            return jsonify({"error": "Не удалось загрузить оценку Ozon"}), 500

    @app.get("/marketplaces/api/analytics/summary")
    @login_required
    def marketplace_analytics_summary_api():
        if not _feature_enabled():
            return jsonify({"error": "Поддержка Ozon выключена"}), 404
        try:
            result = MarketplaceAnalyticsService.get_summary(
                seller_id=_seller_id(),
                account_id=_query_account_id(),
                period_code=request.args.get("period", "30d"),
            )
            return jsonify({"success": True, "data": result})
        except (
            MarketplaceAnalyticsError,
            MarketplaceQualityError,
            MarketplaceAccountError,
        ) as exc:
            return _known_error(exc)
        except Exception as exc:
            logger.exception(
                "Marketplace analytics summary failed: %s",
                type(exc).__name__,
            )
            return jsonify({"error": "Не удалось загрузить аналитику Ozon"}), 500

    @app.get("/marketplaces/api/analytics/products")
    @login_required
    def marketplace_analytics_products_api():
        if not _feature_enabled():
            return jsonify({"error": "Поддержка Ozon выключена"}), 404
        try:
            result = MarketplaceAnalyticsService.get_products(
                seller_id=_seller_id(),
                account_id=_query_account_id(),
                period_code=request.args.get("period", "30d"),
                sort_by=request.args.get("sort_by", "ordered_revenue_rub"),
                sort_dir=request.args.get("sort_dir", "desc"),
                search=request.args.get("search", ""),
                page=_strict_query_integer("page", 1, maximum=100000),
                per_page=_strict_query_integer("per_page", 20, maximum=100),
            )
            return jsonify({"success": True, "data": result})
        except (
            MarketplaceAnalyticsError,
            MarketplaceQualityError,
            MarketplaceAccountError,
        ) as exc:
            return _known_error(exc)
        except Exception as exc:
            logger.exception(
                "Marketplace analytics products failed: %s",
                type(exc).__name__,
            )
            return jsonify({"error": "Не удалось загрузить товары Ozon"}), 500

    @app.post("/marketplaces/api/analytics/sync")
    @login_required
    def marketplace_analytics_sync_api():
        if not _feature_enabled():
            return jsonify({"error": "Поддержка Ozon выключена"}), 404
        try:
            payload = _body({"period", "force"})
            force = _strict_bool(payload.get("force", False), "force")
            run = MarketplaceAnalyticsService.sync_account(
                seller_id=_seller_id(),
                account_id=_query_account_id(),
                period_code=payload.get("period", "30d"),
                force=force,
                max_pages=2,
            )
            return jsonify({"success": True, "data": run.to_public_dict()})
        except (
            MarketplaceAnalyticsError,
            MarketplaceQualityError,
            MarketplaceAccountError,
        ) as exc:
            return _known_error(exc)
        except Exception as exc:
            logger.exception(
                "Marketplace analytics sync failed: %s",
                type(exc).__name__,
            )
            return jsonify({"error": "Не удалось синхронизировать аналитику Ozon"}), 500
