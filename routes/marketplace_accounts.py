"""Seller-facing marketplace account settings."""

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
from services.marketplace_accounts import (
    MarketplaceAccountError,
    MarketplaceAccountService,
)


marketplace_accounts_bp = Blueprint(
    "marketplace_accounts",
    __name__,
    url_prefix="/marketplaces/accounts",
)


def _seller_id() -> Optional[int]:
    seller = getattr(current_user, "seller", None)
    return getattr(seller, "id", None) if seller is not None else None


def _ozon_enabled() -> bool:
    return bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False))


def _payload() -> Dict[str, Any]:
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    return request.form.to_dict(flat=True)


def _is_default(data: Dict[str, Any]) -> Any:
    value = data.get("is_default", False)
    if request.is_json:
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _wants_json() -> bool:
    return request.is_json or (
        request.accept_mimetypes.best == "application/json"
        and request.accept_mimetypes["application/json"]
        >= request.accept_mimetypes["text/html"]
    )


def _html_redirect_endpoint() -> str:
    """Return only a known local endpoint; never accept an arbitrary URL."""
    if request.form.get("return_to") == "api_settings":
        return "api_settings"
    return "marketplace_accounts.index"


def _success_response(
    account,
    *,
    message: str,
    status_code: int = 200,
    extra: Optional[dict] = None,
):
    data = {
        "success": True,
        "message": message,
        "account": account.to_public_dict(),
    }
    if extra:
        data.update(extra)
    if _wants_json():
        return jsonify(data), status_code
    flash(message, "success")
    return redirect(url_for(_html_redirect_endpoint()))


def _error_response(error: MarketplaceAccountError):
    if _wants_json():
        return jsonify({
            "success": False,
            "error": str(error),
            "code": error.code,
        }), error.status_code
    flash(str(error), "danger")
    return redirect(url_for(_html_redirect_endpoint()))


def _feature_disabled_response():
    if _wants_json():
        return jsonify({
            "success": False,
            "error": "Подключение Ozon пока отключено feature flag",
            "code": "ozon_feature_disabled",
        }), 404
    flash("Подключение Ozon пока отключено", "warning")
    return redirect(url_for(_html_redirect_endpoint()))


@marketplace_accounts_bp.route("/")
@login_required
def index():
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    accounts = MarketplaceAccountService.list_accounts(seller_id=seller_id)
    return render_template(
        "marketplace_accounts.html",
        accounts=accounts,
        ozon_enabled=_ozon_enabled(),
    )


@marketplace_accounts_bp.route("/api")
@login_required
def list_api():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    accounts = MarketplaceAccountService.list_accounts(seller_id=seller_id)
    return jsonify({
        "success": True,
        "accounts": [account.to_public_dict() for account in accounts],
        "ozon_enabled": _ozon_enabled(),
    })


def _save_ozon(account_id: Optional[int] = None):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _ozon_enabled():
        return _feature_disabled_response()
    data = _payload()
    try:
        account = MarketplaceAccountService.save_ozon_account(
            seller_id=seller_id,
            account_id=account_id,
            external_account_id=data.get("client_id"),
            label=data.get("label"),
            api_key=data.get("api_key"),
            is_default=_is_default(data),
        )
        return _success_response(
            account,
            message="Подключение Ozon сохранено. Выполните проверку доступа.",
            status_code=201 if account_id is None else 200,
        )
    except MarketplaceAccountError as exc:
        return _error_response(exc)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Marketplace account save failed seller_id=%s account_id=%s",
            seller_id,
            account_id,
        )
        generic = MarketplaceAccountError("Не удалось сохранить подключение")
        generic.status_code = 500
        generic.code = "marketplace_account_save_failed"
        return _error_response(generic)


@marketplace_accounts_bp.route("/ozon", methods=["POST"])
@login_required
def create_ozon():
    return _save_ozon()


@marketplace_accounts_bp.route("/<int:account_id>", methods=["POST"])
@login_required
def update(account_id: int):
    return _save_ozon(account_id)


@marketplace_accounts_bp.route("/<int:account_id>/check", methods=["POST"])
@login_required
def check(account_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _ozon_enabled():
        return _feature_disabled_response()
    try:
        account, result = MarketplaceAccountService.check_connection(
            seller_id=seller_id,
            account_id=account_id,
        )
        message = (
            "Подключение Ozon подтверждено"
            if result.ok
            else result.error_message or "Проверка подключения не пройдена"
        )
        return _success_response(
            account,
            message=message,
            extra={"connection_check": result.to_public_dict()},
        )
    except MarketplaceAccountError as exc:
        return _error_response(exc)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Marketplace connection check failed seller_id=%s account_id=%s",
            seller_id,
            account_id,
        )
        generic = MarketplaceAccountError("Не удалось проверить подключение")
        generic.status_code = 500
        generic.code = "marketplace_connection_check_failed"
        return _error_response(generic)


@marketplace_accounts_bp.route("/<int:account_id>/default", methods=["POST"])
@login_required
def make_default(account_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _ozon_enabled():
        return _feature_disabled_response()
    try:
        account = MarketplaceAccountService.set_default(
            seller_id=seller_id,
            account_id=account_id,
        )
        return _success_response(account, message="Основной кабинет изменён")
    except MarketplaceAccountError as exc:
        return _error_response(exc)


@marketplace_accounts_bp.route("/<int:account_id>/disconnect", methods=["POST"])
@login_required
def disconnect(account_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        account = MarketplaceAccountService.disconnect(
            seller_id=seller_id,
            account_id=account_id,
        )
        return _success_response(
            account,
            message="Кабинет отключён, сохранённый API key удалён",
        )
    except MarketplaceAccountError as exc:
        return _error_response(exc)


def register_marketplace_account_routes(app) -> None:
    app.register_blueprint(marketplace_accounts_bp)
