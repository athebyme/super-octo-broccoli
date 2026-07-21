# -*- coding: utf-8 -*-
"""Admin bestseller dashboard and local Image Lab recommendation handoff."""

from functools import wraps

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from models import db
from services.admin_sales_intelligence import (
    AdminSalesIntelligenceError,
    AdminSalesIntelligenceService,
    SalesDashboardFilters,
)


admin_sales_bp = Blueprint(
    "admin_sales",
    __name__,
    url_prefix="/admin/sales",
)


def _admin_required(function):
    @wraps(function)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("У вас нет прав для доступа к этой странице", "danger")
            return redirect(url_for("dashboard"))
        return function(*args, **kwargs)
    return decorated


def _ozon_enabled() -> bool:
    return bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False))


@admin_sales_bp.get("/")
@login_required
@_admin_required
def index():
    try:
        filters = SalesDashboardFilters.from_mapping(request.args)
        result = AdminSalesIntelligenceService.dashboard(
            filters,
            include_ozon=_ozon_enabled(),
        )
    except AdminSalesIntelligenceError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("admin_sales.index"))

    pagination = result["pagination"]
    prev_url = (
        url_for("admin_sales.index", **filters.query_params(page=pagination["page"] - 1))
        if pagination["has_prev"] else None
    )
    next_url = (
        url_for("admin_sales.index", **filters.query_params(page=pagination["page"] + 1))
        if pagination["has_next"] else None
    )
    return render_template(
        "admin_sales_intelligence.html",
        filters=filters,
        result=result,
        prev_url=prev_url,
        next_url=next_url,
    )


@admin_sales_bp.post("/recommendations")
@login_required
@_admin_required
def create_recommendations():
    try:
        filters = SalesDashboardFilters.from_mapping(request.form)
        outcome = AdminSalesIntelligenceService.recommend(
            filters=filters,
            row_keys=request.form.getlist("row_keys"),
            admin_user_id=current_user.id,
            include_ozon=_ozon_enabled(),
            remote_addr=request.remote_addr,
        )
        message = (
            f"Передано продавцам: {outcome['total']}. "
            f"Новых: {outcome['created']}, обновлено: {outcome['updated']}."
        )
        if outcome["skipped"]:
            message += f" Пропущено после повторной проверки: {outcome['skipped']}."
        flash(message, "success")
    except AdminSalesIntelligenceError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to create bestseller image recommendations")
        flash("Не удалось сохранить рекомендации. Изменения отменены.", "danger")
    try:
        params = SalesDashboardFilters.from_mapping(request.form).query_params()
    except AdminSalesIntelligenceError:
        params = {}
    return redirect(url_for("admin_sales.index", **params))


def register_admin_sales_intelligence_routes(app) -> None:
    app.register_blueprint(admin_sales_bp)
