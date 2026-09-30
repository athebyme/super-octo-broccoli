"""Seller-facing marketplace account settings."""

from datetime import datetime
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
    MarketplaceAccountVersionConflict,
)
from services.marketplace_listings import MarketplaceListingService
from services.ozon_account_sync import enqueue_account_sync, get_account_job, latest_account_jobs
from services.marketplace_account_history import history_page


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
    category: str = "success",
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
    flash(message, category)
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
    accounts = [account for account in accounts if account.marketplace.code == 'ozon']
    now = datetime.utcnow()
    ozon_ids = [account.id for account in accounts if account.marketplace.code == 'ozon']
    upload_ready_account_ids = {
        account.id
        for account in accounts
        if (
            account.marketplace
            and account.marketplace.code == "ozon"
            and account.marketplace.is_active
            and account.is_active
            and account.connection_status == "connected"
            and account.has_credentials
            and account.public_settings.get("default_vat") is not None
            and (
                account.credential_expires_at is None
                or account.credential_expires_at > now
            )
        )
    }
    jobs = latest_account_jobs(seller_id=seller_id, account_ids=ozon_ids)
    syncs = MarketplaceListingService.latest_syncs(seller_id=seller_id)
    return render_template(
        "marketplace_accounts.html",
        accounts=accounts,
        now=now,
        upload_ready_account_ids=upload_ready_account_ids,
        onboarding_jobs=jobs,
        latest_syncs=syncs,
        setup_config={
            'accounts': [account.to_public_dict() for account in accounts if account.marketplace.code == 'ozon'],
            'onboarding_jobs': jobs,
            'catalog_syncs': {account_id: sync.to_public_dict() for account_id, sync in syncs.items()},
            'status_url': url_for('marketplace_accounts.list_api'),
            'accounts_url': url_for('marketplace_accounts.index'),
            'ozon_enabled': _ozon_enabled(),
        },
        ozon_enabled=_ozon_enabled(),
        publication_enabled=bool(
            _ozon_enabled()
            and current_app.config.get(
                "MARKETPLACE_OZON_PUBLICATION_ENABLED",
                False,
            )
        ),
    )


@marketplace_accounts_bp.route("/api")
@login_required
def list_api():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    accounts = MarketplaceAccountService.list_accounts(seller_id=seller_id)
    ozon_ids = [account.id for account in accounts if account.marketplace.code == 'ozon']
    syncs = MarketplaceListingService.latest_syncs(seller_id=seller_id)
    return jsonify({
        "success": True,
        "accounts": [account.to_public_dict() for account in accounts],
        "ozon_enabled": _ozon_enabled(),
        "onboarding_jobs": latest_account_jobs(seller_id=seller_id, account_ids=ozon_ids),
        "catalog_syncs": {account_id: sync.to_public_dict() for account_id, sync in syncs.items()},
    })


def _save_ozon(account_id: Optional[int] = None, *, connect_after_save=False):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _ozon_enabled():
        return _feature_disabled_response()
    data = _payload()
    account = None
    try:
        if connect_after_save:
            unknown = set(data) - {'client_id', 'api_key', 'label', 'is_default', 'default_vat', 'csrf_token', 'expected_version'}
            if unknown or (account_id is None and 'expected_version' in data):
                raise MarketplaceAccountError('Форма подключения содержит неизвестные поля')
        if account_id is not None and connect_after_save:
            # Recovery changes only the key. Settings have their own form and
            # remain blocked while a reviewed operation is outstanding.
            current = MarketplaceAccountService.get_owned_account(
                seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
            expected_version = data.get('expected_version')
            if isinstance(expected_version, str) and expected_version.isascii() and expected_version.isdecimal() and len(expected_version) <= 10:
                expected_version = int(expected_version)
            # A stale hidden label belongs to the same viewed version. Give
            # the UI its review path before checking current settings; the
            # service repeats the authoritative version check under the lock.
            if type(expected_version) is int and expected_version > 0 and current.version != expected_version:
                raise MarketplaceAccountVersionConflict('Настройки магазина изменились. Новый ключ не сохранён. Перечитайте настройки и подтвердите замену снова.')
            if (data.get('label', current.label) != current.label
                    or data.get('default_vat') not in (None, '')
                    or _is_default(data)):
                raise MarketplaceAccountError('Замените ключ отдельно от настроек магазина.')
            account = MarketplaceAccountService.rotate_ozon_key(
                seller_id=seller_id, account_id=account_id,
                external_account_id=data.get('client_id'), api_key=data.get('api_key'),
                expected_version=expected_version, actor_user_id=current_user.id)
        else:
            if account_id is not None and data.get('api_key') not in (None, ''):
                MarketplaceAccountService.get_owned_account(seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
                raise MarketplaceAccountError('Для замены ключа используйте отдельную форму «Заменить ключ и проверить» с актуальной версией магазина. Обычная форма меняет только настройки.')
            account = MarketplaceAccountService.save_ozon_account(
                seller_id=seller_id,
                account_id=account_id,
                external_account_id=data.get("client_id"),
                label=(data.get("label") or "Мой кабинет Ozon") if connect_after_save else data.get("label"),
                api_key=data.get("api_key"),
                is_default=_is_default(data),
                default_vat=data.get("default_vat"),
                actor_user_id=current_user.id,
            )
        if connect_after_save:
            job = enqueue_account_sync(
                seller_id=seller_id, account_id=account.id,
                check_connection=True, force_restart=account_id is not None,
            )
            return _success_response(
                account, message=job['message'], status_code=202, extra={'job': job},
            )
        return _success_response(
            account,
            message="Подключение Ozon сохранено. Выполните проверку доступа.",
            status_code=201 if account_id is None else 200,
        )
    except MarketplaceAccountError as exc:
        if account is not None and connect_after_save:
            return _success_response(
                account, message='Кабинет сохранён. Не удалось запустить проверку; нажмите «Проверить и загрузить каталог».',
                status_code=202, extra={'job': None, 'setup_error': str(exc)}, category='warning',
            )
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


@marketplace_accounts_bp.route("/ozon/connect", methods=["POST"])
@login_required
def connect_ozon():
    """Primary UX: encrypted save plus a local, durable read-only request."""
    return _save_ozon(connect_after_save=True)


@marketplace_accounts_bp.route("/<int:account_id>/connect", methods=["POST"])
@login_required
def connect_existing(account_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({'success': False, 'error': 'Seller account required'}), 403
    if not _ozon_enabled():
        return _feature_disabled_response()
    try:
        if set(_payload()) - {'csrf_token'}:
            raise MarketplaceAccountError('Запрос подключения содержит неизвестные поля')
        job = enqueue_account_sync(seller_id=seller_id, account_id=account_id, check_connection=True, force_restart=True)
        account = MarketplaceAccountService.get_owned_account(seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
        return _success_response(account, message=job['message'], status_code=202, extra={'job': job})
    except MarketplaceAccountError as exc:
        return _error_response(exc)


@marketplace_accounts_bp.route("/<int:account_id>/setup")
@login_required
def setup_status(account_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({'success': False, 'error': 'Seller account required'}), 403
    try:
        if request.args:
            raise MarketplaceAccountError('Запрос статуса не принимает дополнительные параметры')
        account = MarketplaceAccountService.get_owned_account(seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
        sync = MarketplaceListingService.latest_syncs(seller_id=seller_id).get(account_id)
        return jsonify({
            'success': True, 'account': account.to_public_dict(),
            'job': get_account_job(seller_id=seller_id, account_id=account_id),
            'sync': sync.to_public_dict() if sync else None,
            'ozon_enabled': _ozon_enabled(),
        })
    except MarketplaceAccountError as exc:
        return _error_response(exc)


@marketplace_accounts_bp.route("/<int:account_id>", methods=["POST"])
@login_required
def update(account_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({'success': False, 'error': 'Seller account required'}), 403
    if not _ozon_enabled():
        return _feature_disabled_response()
    try:
        data = _payload()
        MarketplaceAccountService.get_owned_account(seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
        if set(data) - {'label', 'client_id', 'default_vat', 'expected_version', 'csrf_token'}:
            raise MarketplaceAccountError('Форма меняет только название магазина и НДС новых карточек. Для ключа используйте отдельную форму.')
        account = MarketplaceAccountService.save_settings(seller_id=seller_id, account_id=account_id,
            external_account_id=data.get('client_id'), label=data.get('label'),
            default_vat=data.get('default_vat'), expected_version=_viewed_integer(data.get('expected_version')),
            actor_user_id=current_user.id)
        return _success_response(account, message='Настройки сохранены. Существующие карточки не изменены.')
    except MarketplaceAccountError as exc:
        return _error_response(exc)
    except Exception:
        db.session.rollback()
        current_app.logger.error('Marketplace settings save failed safely account_id=%s', account_id)
        error = MarketplaceAccountError('Не удалось сохранить настройки. Перечитайте состояние перед повтором.')
        error.status_code = 500
        return _error_response(error)


def _viewed_integer(value, *, optional=False):
    if optional and value in (None, ''):
        return None
    if (request.method == 'GET' or not request.is_json) and isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value <= 2**63-1:
        raise MarketplaceAccountError('Перечитайте настройки: просмотренная версия не подтверждена.')
    return value


@marketplace_accounts_bp.get('/<int:account_id>/history')
@login_required
def account_history(account_id):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({'success': False, 'error': 'Seller account required'}), 403
    try:
        if set(request.args) - {'before_id'} or any(len(values) != 1 for _, values in request.args.lists()):
            raise MarketplaceAccountError('Некорректные параметры истории.')
        before_id = _viewed_integer(request.args.get('before_id'), optional=True)
        data = history_page(seller_id=seller_id, account_id=account_id, viewer_user_id=current_user.id, before_id=before_id)
        response = jsonify({'success': True, **data})
        response.headers['Cache-Control'] = 'no-store'
        return response
    except MarketplaceAccountError as exc:
        return _error_response(exc)


@marketplace_accounts_bp.route("/<int:account_id>/reconnect", methods=["POST"])
@login_required
def reconnect(account_id: int):
    return _save_ozon(account_id, connect_after_save=True)


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
            category='success' if result.ok else 'danger',
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
        MarketplaceAccountService.get_owned_account(seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
        data = _payload()
        if set(data) - {'csrf_token', 'expected_version', 'expected_default_id', 'expected_default_version'}:
            raise MarketplaceAccountError('Форма выбора основного магазина содержит неизвестные поля.')
        account = MarketplaceAccountService.set_default(
            seller_id=seller_id,
            account_id=account_id,
            expected_version=_viewed_integer(data.get('expected_version')),
            expected_default={'id': _viewed_integer(data.get('expected_default_id'), optional=True),
                              'version': _viewed_integer(data.get('expected_default_version'), optional=True)},
            actor_user_id=current_user.id,
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
        MarketplaceAccountService.get_owned_account(seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
        data = _payload()
        if set(data) - {'csrf_token', 'expected_version'}:
            raise MarketplaceAccountError('Форма отключения содержит неизвестные поля.')
        account = MarketplaceAccountService.disconnect(
            seller_id=seller_id,
            account_id=account_id,
            expected_version=_viewed_integer(data.get('expected_version')),
            actor_user_id=current_user.id,
        )
        return _success_response(
            account,
            message="Кабинет отключён, сохранённый API key удалён",
        )
    except MarketplaceAccountError as exc:
        return _error_response(exc)


def register_marketplace_account_routes(app) -> None:
    app.register_blueprint(marketplace_accounts_bp)
