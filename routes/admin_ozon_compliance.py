# -*- coding: utf-8 -*-
"""Админский экран compliance-дефолтов Ozon.

Экран не вызывает Ozon API и не создаёт ``MarketplaceOperation`` — это чистая
надстройка над ``services/ozon_compliance_admin.py``: подписанные админом
решения по ТН ВЭД на Ozon product type и версии нормативного перечня
маркировки. Обе таблицы читаются в ``index``; write-действия (сохранить
решение, создать/активировать версию перечня) — отдельные POST-роуты с общей
конвертацией ``OzonComplianceAdminError`` в понятный ``flash``.
"""
from functools import wraps

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from services.ozon_compliance_admin import (
    OzonComplianceAdminError,
    activate_registry_version,
    create_registry_version,
    list_type_rows,
    preview_registry_switch,
    save_decision,
    type_tnved_dictionary,
)

admin_ozon_compliance_bp = Blueprint(
    "admin_ozon_compliance",
    __name__,
    url_prefix="/admin/ozon/compliance",
)


def _admin_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return function(*args, **kwargs)
    return wrapper


@admin_ozon_compliance_bp.get("/")
@login_required
@_admin_required
def index():
    from models import OzonMarkingRegistryVersion

    return render_template(
        "admin_ozon_compliance.html",
        type_rows=list_type_rows(),
        registry_versions=OzonMarkingRegistryVersion.query.order_by(
            OzonMarkingRegistryVersion.id.desc()
        ).limit(20).all(),
    )


@admin_ozon_compliance_bp.post("/decision")
@login_required
@_admin_required
def save():
    try:
        save_decision(
            product_type_id=request.form.get("product_type_id"),
            tnved_code=request.form.get("tnved_code"),
            rationale=request.form.get("rationale"),
            user_id=current_user.id,
        )
        flash("Решение по ТН ВЭД сохранено", "success")
    except OzonComplianceAdminError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin_ozon_compliance.index"))


@admin_ozon_compliance_bp.post("/registry")
@login_required
@_admin_required
def create_registry():
    try:
        create_registry_version(
            label=request.form.get("label"),
            is_complete=bool(request.form.get("is_complete")),
            rules_text=request.form.get("rules_text"),
            user_id=current_user.id,
        )
        flash(
            "Версия перечня создана. Она не активна — проверьте превью "
            "последствий и активируйте её отдельно.",
            "success",
        )
    except OzonComplianceAdminError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin_ozon_compliance.index", tab="registry"))


@admin_ozon_compliance_bp.post("/registry/activate")
@login_required
@_admin_required
def activate_registry():
    try:
        activate_registry_version(
            version_id=request.form.get("version_id"),
            user_id=current_user.id,
        )
        flash("Версия перечня активирована", "success")
    except OzonComplianceAdminError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin_ozon_compliance.index", tab="registry"))


@admin_ozon_compliance_bp.get("/registry/<int:version_id>/preview")
@login_required
@_admin_required
def registry_preview(version_id):
    try:
        return preview_registry_switch(version_id)
    except OzonComplianceAdminError as exc:
        return {"error": str(exc)}, 400


@admin_ozon_compliance_bp.get("/<int:product_type_id>/tnved-dictionary")
@login_required
@_admin_required
def tnved_dictionary(product_type_id):
    """Официальный словарь ТН ВЭД одного типа — для per-row datalist.

    Читается по требованию (JS-fetch), когда админ раскрывает форму решения
    конкретного типа — не встроен в ``index()`` целиком: страница со всеми
    задействованными типами не должна тянуть в разметку словарь КАЖДОГО из
    них (для крупных категорий это тысячи `<option>` на страницу, из которых
    реально нужен один).
    """
    return type_tnved_dictionary(product_type_id)
