"""Seller-scoped P11 readiness dashboard and local projection controls."""

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

from models import MarketplaceProjectionRun, db
from services.marketplace_readiness import MarketplaceReadinessService
from services.marketplace_rollout import (
    MarketplaceRolloutError,
    MarketplaceRolloutNotFound,
    MarketplaceRolloutService,
    MarketplaceRolloutValidationError,
)


marketplace_readiness_bp = Blueprint(
    "marketplace_readiness",
    __name__,
    url_prefix="/marketplaces/readiness",
)


BLOCKER_LABELS = {
    "wb_projection_disabled": "Фоновая WB-проекция выключена",
    "wb_dual_read_disabled": "Shadow parity WB выключен",
    "wb_projection_count_mismatch": "Число WB projection не совпадает с Product",
    "wb_projection_incomplete": "Не все WB-карточки имеют общую проекцию",
    "wb_canonical_link_mismatch": (
        "WB projection связана не с последней canonical-карточкой"
    ),
    "wb_backfill_not_completed": "Backfill WB ещё не завершён",
    "wb_backfill_not_covering_watermark": "Backfill не покрывает текущий Product watermark",
    "wb_parity_not_completed": "Полный parity-sweep WB ещё не завершён",
    "wb_parity_not_covering_watermark": "Parity не покрывает текущий Product watermark",
    "wb_parity_mismatch": "WB legacy и common projection расходятся",
    "wb_parity_precedes_backfill": "Parity выполнен раньше последнего backfill",
    "wb_source_changed_after_sweep": "Product изменился после начала covering sweep",
    "wb_projection_changed_after_parity": "Projection изменилась после начала parity",
    "wb_common_read_requested_before_parity": (
        "Common read запрошен раньше зелёного parity; действует fallback"
    ),
    "ozon_connected_account_missing": "Нет подключённого кабинета Ozon",
    "ozon_credentials_missing": "У подключённого кабинета Ozon нет ключей доступа",
    "ozon_active_account_unhealthy": "Есть активный кабинет Ozon не в connected state",
    "ozon_credentials_expired": "Credential Ozon истёк и требует замены",
    "ozon_reference_not_ready": "Справочники Ozon ещё ни разу не синхронизировались успешно",
    "ozon_uncertain_operations": "Есть Ozon-операции с неизвестным outcome",
}
WARNING_LABELS = {
    "ozon_read_dark": "Ozon read-контур пока выключен feature flag",
    "ozon_terminal_operations_need_review": (
        "Есть partial/failed Ozon-операции для ручного разбора"
    ),
    "ozon_proposals_need_review": (
        "Есть коммерческие proposal, требующие внимания"
    ),
}


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
            raise MarketplaceRolloutValidationError(
                "JSON body должен быть объектом"
            )
        return value
    value = request.form.to_dict(flat=True)
    value.pop("csrf_token", None)
    return value


def _boolean(value: Any, field_name: str, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if request.is_json:
        if not isinstance(value, bool):
            raise MarketplaceRolloutValidationError(
                f"{field_name} должен быть boolean"
            )
        return value
    if value == "1":
        return True
    if value == "0":
        return False
    raise MarketplaceRolloutValidationError(
        f"{field_name} должен быть checkbox boolean"
    )


def _error(error: Exception):
    db.session.rollback()
    status = error.status_code if isinstance(error, MarketplaceRolloutError) else 500
    code = (
        error.code
        if isinstance(error, MarketplaceRolloutError)
        else "marketplace_readiness_failed"
    )
    message = (
        str(error)
        if isinstance(error, MarketplaceRolloutError)
        else "Не удалось получить readiness маркетплейсов"
    )
    if _wants_json():
        return jsonify({
            "success": False,
            "error": message,
            "code": code,
        }), status
    flash(message, "danger")
    return redirect(url_for("marketplace_readiness.index"))


def _document(seller_id: int) -> Dict[str, Any]:
    return MarketplaceReadinessService.build(
        seller_id=seller_id,
        config=current_app.config,
    )


@marketplace_readiness_bp.route("/", methods=["GET"])
@login_required
def index():
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        document = _document(seller_id)
    except Exception as exc:
        current_app.logger.exception(
            "Marketplace readiness failed seller_id=%s",
            seller_id,
        )
        return _error(exc)
    if _wants_json():
        return jsonify({"success": True, "readiness": document})
    return render_template(
        "marketplace_readiness.html",
        readiness=document,
        blocker_labels=BLOCKER_LABELS,
        warning_labels=WARNING_LABELS,
    )


def _projection_enabled() -> None:
    if not current_app.config.get("MARKETPLACE_WB_PROJECTION_ENABLED", True):
        error = MarketplaceRolloutError(
            "WB projection maintenance выключен feature flag"
        )
        error.status_code = 409
        error.code = "wb_projection_disabled"
        raise error


def _dual_read_enabled() -> None:
    if not current_app.config.get("MARKETPLACE_WB_DUAL_READ_ENABLED", True):
        error = MarketplaceRolloutError(
            "WB dual-read parity выключен feature flag"
        )
        error.status_code = 409
        error.code = "wb_dual_read_disabled"
        raise error


@marketplace_readiness_bp.route("/projection/backfill", methods=["POST"])
@login_required
def backfill():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _projection_enabled()
        data = _payload()
        if set(data) - {"force_full"}:
            raise MarketplaceRolloutValidationError(
                "Допустимо только поле force_full"
            )
        run = MarketplaceRolloutService.run_backfill_batch(
            seller_id=seller_id,
            limit=MarketplaceRolloutService.MAX_BATCH,
            force_full=_boolean(data.get("force_full"), "force_full"),
        )
    except Exception as exc:
        return _error(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "run": run.to_public_dict() if run else None,
        })
    flash("Один bounded WB backfill batch обработан", "success")
    return redirect(url_for("marketplace_readiness.index"))


@marketplace_readiness_bp.route("/projection/parity", methods=["POST"])
@login_required
def parity():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        _projection_enabled()
        _dual_read_enabled()
        data = _payload()
        if set(data) - {"force_full"}:
            raise MarketplaceRolloutValidationError(
                "Допустимо только поле force_full"
            )
        run = MarketplaceRolloutService.run_parity_batch(
            seller_id=seller_id,
            limit=MarketplaceRolloutService.MAX_BATCH,
            force_full=_boolean(data.get("force_full"), "force_full"),
        )
    except Exception as exc:
        return _error(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "run": run.to_public_dict() if run else None,
        })
    flash("Один bounded WB parity batch обработан", "success")
    return redirect(url_for("marketplace_readiness.index"))


@marketplace_readiness_bp.route(
    "/projection/runs/<int:run_id>/<string:action>",
    methods=["POST"],
)
@login_required
def control_run(run_id: int, action: str):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        data = _payload()
        if data:
            raise MarketplaceRolloutValidationError(
                "Управление run не принимает полей"
            )
        owned = MarketplaceProjectionRun.query.filter_by(
            id=run_id,
            seller_id=seller_id,
        ).first()
        if owned is None:
            raise MarketplaceRolloutNotFound("Projection run не найден")
        if action == "pause":
            run = MarketplaceRolloutService.pause_run(run_id=owned.id)
        elif action == "resume":
            _projection_enabled()
            if owned.run_kind == "wb_parity":
                _dual_read_enabled()
            run = MarketplaceRolloutService.resume_run(run_id=owned.id)
        else:
            raise MarketplaceRolloutNotFound("Неизвестное действие")
    except Exception as exc:
        return _error(exc)
    if _wants_json():
        return jsonify({"success": True, "run": run.to_public_dict()})
    flash("Состояние projection run обновлено", "success")
    return redirect(url_for("marketplace_readiness.index"))


def register_marketplace_readiness_routes(app) -> None:
    app.register_blueprint(marketplace_readiness_bp)
