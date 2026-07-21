"""Seller-facing marketplace product drafts and deterministic validation."""

import json
import secrets
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
from services.marketplace_accounts import MarketplaceAccountService
from services.marketplace_drafts import (
    MarketplaceDraftError,
    MarketplaceDraftService,
    MarketplaceDraftValidationError,
)
from services.marketplace_publications import MarketplacePublicationService


marketplace_drafts_bp = Blueprint(
    "marketplace_drafts",
    __name__,
    url_prefix="/marketplaces/drafts",
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
        if not isinstance(value, dict):
            raise MarketplaceDraftValidationError(
                "JSON body должен быть объектом"
            )
        return value
    data = request.form.to_dict(flat=True)
    data.pop("csrf_token", None)
    return data


def _integer(
    value: Any,
    field_name: str,
    *,
    required: bool = True,
    default: Optional[int] = None,
) -> Optional[int]:
    if value in (None, ""):
        if default is not None:
            return default
        if not required:
            return None
        raise MarketplaceDraftValidationError(f"{field_name} обязателен")
    if isinstance(value, bool):
        raise MarketplaceDraftValidationError(
            f"{field_name} должен быть целым числом"
        )
    if isinstance(value, int):
        parsed = value
    elif not request.is_json and isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise MarketplaceDraftValidationError(
            f"{field_name} должен быть целым числом"
        )
    if parsed <= 0:
        raise MarketplaceDraftValidationError(
            f"{field_name} должен быть положительным"
        )
    return parsed


def _boolean(value: Any, field_name: str, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if not request.is_json and isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise MarketplaceDraftValidationError(f"{field_name} должен быть boolean")


def _feature_enabled() -> bool:
    return bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False))


def _publication_enabled() -> bool:
    return bool(
        _feature_enabled()
        and current_app.config.get(
            "MARKETPLACE_OZON_PUBLICATION_ENABLED",
            False,
        )
    )


def _feature_disabled_response():
    error = MarketplaceDraftError("Черновики Ozon отключены feature flag")
    error.status_code = 404
    error.code = "ozon_feature_disabled"
    return _error_response(error)


def _error_response(error: Exception, status_code: Optional[int] = None):
    if isinstance(error, MarketplaceDraftError):
        status_code = status_code or error.status_code
        code = error.code
    else:
        status_code = status_code or 400
        code = "invalid_marketplace_draft_request"
    if _wants_json():
        return jsonify({
            "success": False,
            "error": str(error),
            "code": code,
        }), status_code
    return render_template(
        "marketplace_draft_error.html",
        error=str(error),
    ), status_code


def _write_failure(error: Exception, *, seller_id: int, action: str):
    if isinstance(error, MarketplaceDraftError):
        return _error_response(error)
    db.session.rollback()
    current_app.logger.exception(
        "Marketplace draft %s failed seller_id=%s",
        action,
        seller_id,
    )
    generic = MarketplaceDraftError("Не удалось сохранить черновик")
    generic.status_code = 500
    generic.code = "marketplace_draft_write_failed"
    return _error_response(generic)


def _list_filters() -> dict:
    return {
        "account_id": _integer(
            request.args.get("account_id"),
            "account_id",
            required=False,
        ),
        "status": request.args.get("status") or None,
        "page": _integer(request.args.get("page"), "page", default=1),
        "per_page": _integer(
            request.args.get("per_page"),
            "per_page",
            default=50,
        ),
    }


@marketplace_drafts_bp.route("/", methods=["GET"])
@login_required
def index():
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        filters = _list_filters()
        pagination = MarketplaceDraftService.list_drafts(
            seller_id=seller_id,
            **filters,
        )
        accounts = MarketplaceAccountService.list_accounts(
            seller_id=seller_id,
            marketplace_code="ozon",
        )
        sources = MarketplaceDraftService.recent_sources(seller_id=seller_id)
    except MarketplaceDraftError as exc:
        return _error_response(exc)
    return render_template(
        "marketplace_drafts.html",
        pagination=pagination,
        drafts=pagination.items,
        accounts=accounts,
        sources=sources,
        filters=filters,
        ozon_enabled=_feature_enabled(),
        publication_enabled=_publication_enabled(),
    )


@marketplace_drafts_bp.route("/api", methods=["GET"])
@login_required
def list_api():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        pagination = MarketplaceDraftService.list_drafts(
            seller_id=seller_id,
            **_list_filters(),
        )
    except MarketplaceDraftError as exc:
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


@marketplace_drafts_bp.route("/types", methods=["GET"])
@login_required
def product_types():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        items = MarketplaceDraftService.search_product_types(
            seller_id=seller_id,
            query=request.args.get("query", ""),
            limit=_integer(request.args.get("limit"), "limit", default=50),
        )
    except MarketplaceDraftError as exc:
        return _error_response(exc)
    return jsonify({"success": True, "items": items})


@marketplace_drafts_bp.route("/", methods=["POST"])
@login_required
def create():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _feature_enabled():
        return _feature_disabled_response()
    try:
        data = _payload()
        allowed = {
            "account_id", "imported_product_id", "product_type_id",
            "offer_id", "save_mapping", "validate",
        }
        unknown = set(data) - allowed
        if unknown:
            raise MarketplaceDraftValidationError(
                "Неизвестные поля: " + ", ".join(sorted(unknown))
            )
        account_id = _integer(data.get("account_id"), "account_id")
        imported_product_id = _integer(
            data.get("imported_product_id"),
            "imported_product_id",
        )
        product_type_id = _integer(
            data.get("product_type_id"),
            "product_type_id",
            required=False,
        )
        save_mapping = _boolean(
            data.get("save_mapping"),
            "save_mapping",
        )
        validate_immediately = _boolean(
            data.get("validate"),
            "validate",
        )
        draft = MarketplaceDraftService.create_draft(
            seller_id=seller_id,
            account_id=account_id,
            imported_product_id=imported_product_id,
            product_type_id=product_type_id,
            offer_id=data.get("offer_id") or None,
            save_mapping=save_mapping,
            corrected_by_user_id=getattr(current_user, "id", None),
        )
        if validate_immediately:
            draft = MarketplaceDraftService.validate_draft(
                seller_id=seller_id,
                draft_id=draft.id,
                expected_version=draft.version,
            )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="create")
    if _wants_json():
        return jsonify({
            "success": True,
            "draft": draft.to_public_dict(detail=True),
        }), 201
    result = draft.to_public_dict(detail=True)
    if validate_immediately and result["validation"].get("publishable"):
        flash(
            "Карточка подготовлена: Ozon-черновик полностью готов к публикации",
            "success",
        )
    elif validate_immediately:
        flash(
            "Ozon-черновик подготовлен; ниже показано, что нужно уточнить",
            "warning",
        )
    else:
        flash("Черновик Ozon создан", "success")
    return redirect(url_for("marketplace_drafts.detail", draft_id=draft.id))


@marketplace_drafts_bp.route("/bulk-prepare", methods=["POST"])
@login_required
def bulk_prepare():
    """Готовит Ozon-черновики для выбранных ImportedProduct одного кабинета.

    Тонкий route: parse входа + вызов MarketplaceDraftService.bulk_prepare
    (детерминированный локальный путь без LLM/provider calls; ошибка одного
    товара не блокирует остальные) + ответ.
    """
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _feature_enabled():
        return _feature_disabled_response()
    try:
        if request.is_json:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise MarketplaceDraftValidationError(
                    "JSON body должен быть объектом"
                )
            raw_ids = data.get("imported_product_ids")
            account_raw = data.get("account_id")
            validate_raw = data.get("validate")
        else:
            raw_ids = request.form.getlist("imported_product_ids")
            account_raw = request.form.get("account_id")
            validate_raw = request.form.get("validate")
        account_id = _integer(account_raw, "account_id")
        validate_immediately = _boolean(validate_raw, "validate")
        if not isinstance(raw_ids, list):
            raise MarketplaceDraftValidationError(
                "imported_product_ids обязателен и должен быть непустым списком"
            )
        # Form-строки приводим к int здесь (сервис принимает только строгие int)
        product_ids = [
            _integer(raw, "imported_product_id") for raw in raw_ids
        ]
        summary = MarketplaceDraftService.bulk_prepare(
            seller_id=seller_id,
            account_id=account_id,
            imported_product_ids=product_ids,
            validate=validate_immediately,
            corrected_by_user_id=getattr(current_user, "id", None),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="bulk_prepare")
    if _wants_json():
        return jsonify({"success": True, **summary}), 200
    parts = []
    if summary["created"]:
        parts.append(f"создано {summary['created']}")
    if summary["existing"]:
        parts.append(f"уже были {summary['existing']}")
    if summary["failed"]:
        parts.append(f"с ошибкой {summary['failed']}")
    message = "Ozon-черновики: " + (", ".join(parts) if parts else "нет изменений")
    if summary["failed"] and not summary["created"]:
        category = "error"
    elif summary["failed"]:
        # Частичный результат не маскируем под полный успех
        category = "warning"
    else:
        category = "success"
    flash(message, category)
    return redirect(url_for("marketplace_drafts.index", account_id=account_id))


@marketplace_drafts_bp.route("/bulk-publish", methods=["POST"])
@login_required
def bulk_publish():
    """Ставит выбранные готовые черновики в durable-очередь публикации.

    Тонкий route: provider не вызывается — операции создаются queued, их
    отправляет минутный scheduler под account claim с квотой и preflight.
    """
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _feature_enabled():
        return _feature_disabled_response()
    if not _publication_enabled():
        message = (
            "Публикация Ozon выключена флагом MARKETPLACE_OZON_PUBLICATION_ENABLED."
        )
        if _wants_json():
            return jsonify({"success": False, "error": message}), 403
        flash(message, "warning")
        return redirect(url_for("marketplace_drafts.index"))
    from services.marketplace_publications import (
        MarketplacePublicationError,
        MarketplacePublicationService,
    )
    try:
        if request.is_json:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise MarketplaceDraftValidationError(
                    "JSON body должен быть объектом"
                )
            raw_ids = data.get("draft_ids")
            account_raw = data.get("account_id")
        else:
            raw_ids = request.form.getlist("draft_ids")
            account_raw = request.form.get("account_id")
        account_id = _integer(account_raw, "account_id")
        if not isinstance(raw_ids, list):
            raise MarketplaceDraftValidationError(
                "draft_ids обязателен и должен быть непустым списком"
            )
        draft_ids = [_integer(raw, "draft_id") for raw in raw_ids]
        result = MarketplacePublicationService.enqueue_bulk_publications(
            seller_id=seller_id,
            account_id=account_id,
            draft_ids=draft_ids,
            created_by_user_id=getattr(current_user, "id", None),
        )
    except (MarketplaceDraftError, MarketplacePublicationError) as exc:
        if _wants_json():
            return jsonify({"success": False, "error": str(exc)}), 400
        flash(str(exc), "error")
        return redirect(url_for("marketplace_drafts.index"))
    if _wants_json():
        return jsonify({"success": True, **result}), 200
    queued_count = len(result["queued"])
    skipped = result["skipped"]
    message = f"Поставлено в очередь публикации: {queued_count}."
    if skipped:
        reasons = "; ".join(
            f"#{row['draft_id']}: {row['reason']}" for row in skipped[:3]
        )
        message += f" Пропущено: {len(skipped)} ({reasons}"
        if len(skipped) > 3:
            message += " …"
        message += ")."
    message += " Прогресс — в разделе «Операции Ozon»."
    flash(
        message,
        "success" if queued_count and not skipped
        else ("warning" if queued_count else "error"),
    )
    return redirect(url_for("marketplace_drafts.index", account_id=account_id))


@marketplace_drafts_bp.route("/<int:draft_id>", methods=["GET"])
@login_required
def detail(draft_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        draft = MarketplaceDraftService.get_draft(
            seller_id=seller_id,
            draft_id=draft_id,
        )
        type_query = request.args.get("type_query", "")
        type_options = (
            MarketplaceDraftService.search_product_types(
                seller_id=seller_id,
                query=type_query,
                limit=50,
            )
            if type_query else []
        )
        operations = MarketplacePublicationService.list_for_draft(
            seller_id=seller_id,
            draft_id=draft.id,
            limit=20,
        )
        mapping_readiness = MarketplaceDraftService.mapping_readiness(
            seller_id=seller_id,
            draft_id=draft.id,
        )
    except MarketplaceDraftError as exc:
        return _error_response(exc)
    if _wants_json():
        return jsonify({
            "success": True,
            "draft": draft.to_public_dict(detail=True),
            "operations": [
                operation.to_public_dict(detail=False)
                for operation in operations
            ],
            "mapping_readiness": mapping_readiness,
        })
    active_operation = next(
        (
            operation for operation in operations
            if operation.status in MarketplacePublicationService.ACTIVE_STATUSES
        ),
        None,
    )
    # Рекомендуемые типы Ozon для черновика без типа (только предложение,
    # привязка остаётся явным действием продавца)
    type_suggestions = []
    if not draft.product_type_id:
        try:
            type_suggestions = MarketplaceDraftService.suggest_product_types(
                seller_id=seller_id, draft_id=draft.id,
            )
        except MarketplaceDraftError:
            type_suggestions = []

    # Имена атрибутов типа для человекочитаемого отображения (read-only)
    attribute_names = {}
    if draft.product_type_id:
        from models import MarketplaceAttributeDefinition
        attribute_names = {
            row[0]: row[1]
            for row in db.session.query(
                MarketplaceAttributeDefinition.external_attribute_id,
                MarketplaceAttributeDefinition.name,
            ).filter(
                MarketplaceAttributeDefinition.product_type_id
                == draft.product_type_id,
            ).all()
        }
    return render_template(
        "marketplace_draft_detail.html",
        draft=draft,
        draft_data=draft.to_public_dict(detail=True),
        attribute_names=attribute_names,
        type_suggestions=type_suggestions,
        type_query=type_query,
        type_options=type_options,
        ozon_enabled=_feature_enabled(),
        publication_enabled=_publication_enabled(),
        publication_idempotency_key=secrets.token_urlsafe(24),
        operations=operations,
        active_operation=active_operation,
        mapping_readiness=mapping_readiness,
    )


def _form_patch(data: Dict[str, Any]) -> dict:
    patch = {}
    if data.get("offer_id") not in (None, ""):
        patch["offer_id"] = data["offer_id"]
    if "product_type_id" in data and data.get("product_type_id") not in (None, ""):
        patch["product_type_id"] = _integer(
            data.get("product_type_id"),
            "product_type_id",
        )
    if "save_mapping" in data:
        patch["save_mapping"] = _boolean(
            data.get("save_mapping"),
            "save_mapping",
        )
    json_fields = {
        "content_json": "content",
        "attributes_json": "attributes",
        "complex_attributes_json": "complex_attributes",
        "media_json": "media",
        "dimensions_json": "dimensions",
        "barcodes_json": "barcodes",
        "commercial_json": "commercial",
    }
    for form_name, patch_name in json_fields.items():
        if form_name not in data:
            continue
        try:
            patch[patch_name] = json.loads(data[form_name])
        except (TypeError, ValueError):
            raise MarketplaceDraftValidationError(
                f"{form_name} содержит невалидный JSON"
            ) from None
    return patch


@marketplace_drafts_bp.route("/<int:draft_id>", methods=["POST", "PATCH"])
@login_required
def update(draft_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _feature_enabled():
        return _feature_disabled_response()
    try:
        data = _payload()
        if request.is_json:
            if set(data) - {"expected_version", "patch"}:
                raise MarketplaceDraftValidationError(
                    "JSON update принимает только expected_version и patch"
                )
            patch = data.get("patch")
        else:
            patch = _form_patch(data)
        draft = MarketplaceDraftService.update_draft(
            seller_id=seller_id,
            draft_id=draft_id,
            expected_version=_integer(
                data.get("expected_version"),
                "expected_version",
            ),
            patch=patch,
            corrected_by_user_id=getattr(current_user, "id", None),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="update")
    if _wants_json():
        return jsonify({
            "success": True,
            "draft": draft.to_public_dict(detail=True),
        })
    flash("Черновик сохранён; выполните повторную валидацию", "success")
    return redirect(url_for("marketplace_drafts.detail", draft_id=draft.id))


def _expected_version_payload() -> int:
    data = _payload()
    if set(data) - {"expected_version"}:
        raise MarketplaceDraftValidationError(
            "Допустимо только поле expected_version"
        )
    return _integer(data.get("expected_version"), "expected_version")


@marketplace_drafts_bp.route("/<int:draft_id>/validate", methods=["POST"])
@login_required
def validate(draft_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _feature_enabled():
        return _feature_disabled_response()
    try:
        draft = MarketplaceDraftService.validate_draft(
            seller_id=seller_id,
            draft_id=draft_id,
            expected_version=_expected_version_payload(),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="validate")
    data = draft.to_public_dict(detail=True)
    if _wants_json():
        return jsonify({"success": True, "draft": data})
    if data["validation"]["publishable"]:
        flash("Черновик полностью прошёл deterministic validation", "success")
    else:
        flash("Черновик заблокирован: исправьте структурированные ошибки", "warning")
    return redirect(url_for("marketplace_drafts.detail", draft_id=draft.id))


@marketplace_drafts_bp.route("/<int:draft_id>/refresh-facts", methods=["POST"])
@login_required
def refresh_facts(draft_id: int):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    if not _feature_enabled():
        return _feature_disabled_response()
    try:
        draft = MarketplaceDraftService.refresh_facts(
            seller_id=seller_id,
            draft_id=draft_id,
            expected_version=_expected_version_payload(),
        )
    except Exception as exc:
        return _write_failure(exc, seller_id=seller_id, action="refresh_facts")
    if _wants_json():
        return jsonify({
            "success": True,
            "draft": draft.to_public_dict(detail=True),
        })
    flash(
        "Fact snapshot обновлён; пользовательские поля не перезаписаны",
        "success",
    )
    return redirect(url_for("marketplace_drafts.detail", draft_id=draft.id))


def register_marketplace_draft_routes(app) -> None:
    app.register_blueprint(marketplace_drafts_bp)
