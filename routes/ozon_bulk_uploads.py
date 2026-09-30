"""Seller-scoped local preparation and explicitly reviewed Ozon publication."""

import json
import re
import secrets
from io import BytesIO
from typing import Any, Optional

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from flask_wtf.csrf import generate_csrf

from models import db
from services.ozon_bulk_upload import (
    OzonBulkUploadError,
    OzonBulkUploadService,
    OzonBulkUploadValidationError,
)
from services.ozon_bulk_repair import OzonBulkRepairService


ozon_bulk_uploads_bp = Blueprint(
    "ozon_bulk_uploads",
    __name__,
    url_prefix="/marketplaces/ozon/uploads",
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


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise OzonBulkUploadValidationError(
            f"{field_name} должен быть целым числом"
        )
    if isinstance(value, int):
        parsed = value
    elif (
        not request.is_json
        and isinstance(value, str)
        and value.isascii()
        and value.isdigit()
    ):
        parsed = int(value)
    else:
        raise OzonBulkUploadValidationError(
            f"{field_name} должен быть целым числом"
        )
    if not 0 < parsed <= 9223372036854775807:
        raise OzonBulkUploadValidationError(
            f"{field_name} должен быть положительным целым в пределах int64"
        )
    return parsed


def _bounded_integer_list(values: Any, field_name: str) -> list[int]:
    if not isinstance(values, list):
        raise OzonBulkUploadValidationError(
            f"{field_name} должен быть массивом"
        )
    if not values or len(values) > OzonBulkUploadService.MAX_ITEMS:
        raise OzonBulkUploadValidationError(
            "За один запуск можно загрузить не более "
            f"{OzonBulkUploadService.MAX_ITEMS} карточек"
        )
    singular = (
        "draft_id" if field_name == "draft_ids" else "imported_product_id"
    )
    parsed = [_integer(value, singular) for value in values]
    if len(parsed) != len(set(parsed)):
        raise OzonBulkUploadValidationError("Выбранные карточки не должны повторяться")
    return parsed


def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OzonBulkUploadValidationError("Поля JSON не должны повторяться")
        result[key] = value
    return result


def _request_payload(allowed: set[str], *, list_fields=()) -> dict:
    if request.args:
        raise OzonBulkUploadValidationError("Параметры запуска передаются только в body")
    if request.is_json:
        body = request.get_data(cache=True)
        if len(body) > 65536:
            raise OzonBulkUploadValidationError("Запрос загрузки превышает 64 KiB")
        try:
            data = json.loads(body, object_pairs_hook=_unique_json_pairs)
        except (ValueError, UnicodeError):
            raise OzonBulkUploadValidationError("Некорректный JSON body") from None
        if not isinstance(data, dict):
            raise OzonBulkUploadValidationError("JSON body должен быть объектом")
    else:
        data = {
            key: request.form.getlist(key) if key in list_fields else _single_form_value(key)
            for key in request.form if key != "csrf_token"
        }
    if set(data) - allowed:
        raise OzonBulkUploadValidationError("Переданы неизвестные поля запуска")
    return data


def _request_key(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", value):
        raise OzonBulkUploadValidationError("request_key должен содержать 24–128 URL-safe символов")
    return value


def _require_prepare_confirmation(value: Any) -> None:
    if not (value is True if request.is_json else value == "1"):
        raise OzonBulkUploadValidationError("Подтвердите локальную подготовку черновиков")


def _review_required(message: str) -> None:
    error = OzonBulkUploadValidationError(message)
    error.code = "draft_review_required"
    error.status_code = 409
    raise error


def _reviewed_versions(value: Any, draft_ids: list[int]) -> dict:
    if not request.is_json and isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_unique_json_pairs)
        except (ValueError, TypeError):
            value = None
        # A no-JS page posts the rendered page's version map plus checked IDs.
        # Extra unchecked rows are never selected or sent to the worker.
        if isinstance(value, dict) and len(value) <= 20:
            value = {str(pk): value[str(pk)] for pk in draft_ids if str(pk) in value}
    if not isinstance(value, dict) or set(value) != {str(pk) for pk in draft_ids}:
        _review_required("Просмотрите выбранные карточки и подтвердите их актуальные версии")
    if any(type(version) is not int or not 0 < version <= 9223372036854775807
           for version in value.values()):
        raise OzonBulkUploadValidationError("Версии должны быть положительными целыми числами")
    return value


def _require_write_confirmation(value: Any) -> None:
    """Require an explicit acknowledgement before create/update can be queued."""
    confirmed = (
        value is True
        if request.is_json
        else isinstance(value, str) and value == "1"
    )
    if not confirmed:
        raise OzonBulkUploadValidationError(
            "Подтвердите создание новых и полное обновление существующих "
            "карточек Ozon"
        )


def _error_response(error: OzonBulkUploadError):
    if _wants_json():
        return jsonify({
            "success": False,
            "error": str(error),
            "code": error.code,
        }), error.status_code
    flash(str(error), "danger")
    return redirect(url_for("seller_my_products"))


def _created_response(acceptance):
    job = acceptance.job
    document = OzonBulkUploadService.public_document(job, detail=True)
    location = url_for("ozon_bulk_uploads.detail", job_uid=job.job_uid)
    if _wants_json():
        response = jsonify({
            "success": True,
            "run": document,
            "replayed": acceptance.replayed,
        })
        response.status_code = 202
        response.headers["Location"] = location
        response.headers["Cache-Control"] = "private, no-store"
        return response
    flash(
        "Подготовка черновиков начата. После неё проверьте карточки перед отправкой."
        if document.get("mode") == "source_prepare" else
        "Проверенные версии карточек приняты в очередь отправки Ozon.",
        "success",
    )
    return redirect(location, code=303)


def _single_form_value(key: str, *, required: bool = False) -> str:
    values = request.form.getlist(key)
    if len(values) > 1:
        raise OzonBulkUploadValidationError(
            f"Поле {key} не должно повторяться"
        )
    if not values:
        if required:
            raise OzonBulkUploadValidationError(
                f"Поле {key} обязательно"
            )
        return ""
    return values[0]


def _parse_editor_rows(editor: dict) -> list[dict]:
    """Parse the server-rendered exact editor scope from one HTML form."""
    rows_by_id = {
        row["draft_id"]: row
        for group in editor["groups"]
        for row in group["rows"]
    }
    selected_raw = request.form.getlist("selected_draft_id")
    if (
        not selected_raw
        or len(selected_raw) > OzonBulkRepairService.MAX_ROWS
    ):
        raise OzonBulkUploadValidationError(
            "Выберите хотя бы одну карточку для сохранения"
        )
    selected_ids = [
        _integer(value, "draft_id")
        for value in selected_raw
    ]
    if len(selected_ids) != len(set(selected_ids)):
        raise OzonBulkUploadValidationError(
            "Выбранные карточки не должны повторяться"
        )
    if any(draft_id not in rows_by_id for draft_id in selected_ids):
        raise OzonBulkUploadValidationError(
            "Выбрана карточка вне текущего запуска"
        )

    allowed_keys = {
        "csrf_token",
        "selected_draft_id",
    }
    fixed_fields = sorted(
        OzonBulkRepairService.EDITABLE_FIXED - {"action"}
    )
    for draft_id, row in rows_by_id.items():
        prefix = f"row_{draft_id}_"
        allowed_keys.update({
            prefix + "draft_version",
            prefix + "imported_product_id",
            prefix + "action",
            prefix + "product_type_id",
            prefix + "save_mapping",
            *(prefix + field for field in fixed_fields),
        })
        if row["cleanup_candidates"]:
            allowed_keys.add(prefix + "schema_cleanup")
        for attribute in row["attributes"]:
            if not attribute["editable"]:
                continue
            external_id = attribute["external_id"]
            allowed_keys.add(prefix + f"attribute_{external_id}")
            allowed_keys.add(
                prefix + f"attribute_value_id_{external_id}"
            )
    unknown = set(request.form.keys()) - allowed_keys
    if unknown:
        raise OzonBulkUploadValidationError(
            "Форма содержит неизвестные поля"
        )

    result = []
    for draft_id in selected_ids:
        expected = rows_by_id[draft_id]
        prefix = f"row_{draft_id}_"
        expected_version = _integer(
            _single_form_value(
                prefix + "draft_version",
                required=True,
            ),
            "draft_version",
        )
        imported_product_id = _integer(
            _single_form_value(
                prefix + "imported_product_id",
                required=True,
            ),
            "imported_product_id",
        )
        if imported_product_id != expected["imported_product_id"]:
            raise OzonBulkUploadValidationError(
                "Связь строки с товаром изменилась"
            )
        product_type_raw = _single_form_value(
            prefix + "product_type_id"
        )
        product_type_id = (
            _integer(product_type_raw, "product_type_id")
            if product_type_raw else None
        )
        save_mapping_raw = _single_form_value(
            prefix + "save_mapping"
        )
        if save_mapping_raw not in {"", "0", "1"}:
            raise OzonBulkUploadValidationError(
                "save_mapping должен быть 0 или 1"
            )
        schema_cleanup_raw = (
            _single_form_value(prefix + "schema_cleanup")
            if expected["cleanup_candidates"]
            else ""
        )
        if schema_cleanup_raw not in {"", "1"}:
            raise OzonBulkUploadValidationError(
                "schema_cleanup должен быть 1"
            )
        values = {
            field: _single_form_value(prefix + field)
            for field in fixed_fields
        }
        for attribute in expected["attributes"]:
            if not attribute["editable"]:
                continue
            external_id = attribute["external_id"]
            values[f"attribute:{external_id}"] = _single_form_value(
                prefix + f"attribute_{external_id}"
            )
            values[f"attribute_value_id:{external_id}"] = (
                _single_form_value(
                    prefix + f"attribute_value_id_{external_id}"
                )
            )
        result.append({
            "draft_id": draft_id,
            "expected_version": expected_version,
            "imported_product_id": imported_product_id,
            "action": _single_form_value(
                prefix + "action",
                required=True,
            ),
            "product_type_id": product_type_id,
            "save_mapping": save_mapping_raw == "1",
            "schema_cleanup": schema_cleanup_raw == "1",
            "values": values,
        })
    return result


def _search_error_response(error: OzonBulkUploadError):
    return jsonify({
        "success": False,
        "error": str(error),
        "code": error.code,
    }), error.status_code


@ozon_bulk_uploads_bp.route("/", methods=["GET"])
@login_required
def index():
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        if set(request.args) - {'account_id'} or len(request.args.getlist('account_id')) > 1:
            raise OzonBulkUploadValidationError('Допустим только один account_id')
        account_id = _integer(request.args.get('account_id'), 'account_id') if request.args.get('account_id') else None
        runs = OzonBulkUploadService.list_runs(
            seller_id=seller_id,
            limit=50,
            account_id=account_id,
        )
        documents = [
            OzonBulkUploadService.public_document(run)
            for run in runs
        ]
    except OzonBulkUploadError as exc:
        return _error_response(exc)
    if _wants_json():
        return jsonify({"success": True, "items": documents})
    return render_template(
        "ozon_bulk_uploads.html",
        runs=documents,
        selected_account_id=account_id,
        ozon_enabled=bool(
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        ),
        publication_enabled=bool(
            current_app.config.get(
                "MARKETPLACE_OZON_PUBLICATION_ENABLED",
                False,
            )
        ),
    )


@ozon_bulk_uploads_bp.route("/", methods=["POST"])
@login_required
def create():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        if (request.is_json and isinstance(request.get_json(silent=True), dict)
                and "confirm_write" in request.get_json(silent=True)) or (
                not request.is_json and "confirm_write" in request.form):
            _review_required("Сначала подготовьте черновики, затем проверьте их перед отправкой")
        data = _request_payload(
            {"account_id", "imported_product_ids", "confirm_prepare", "request_key"},
            list_fields={"imported_product_ids"},
        )
        _require_prepare_confirmation(data.get("confirm_prepare"))
        acceptance = OzonBulkUploadService.accept_source_prepare(
            seller_id=seller_id,
            account_id=_integer(data.get("account_id"), "account_id"),
            imported_product_ids=_bounded_integer_list(data.get("imported_product_ids"), "imported_product_ids"),
            request_key=_request_key(data.get("request_key")),
            created_by_user_id=getattr(current_user, "id", None),
        )
    except OzonBulkUploadError as exc:
        return _error_response(exc)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Ozon bulk upload start failed seller_id=%s",
            seller_id,
        )
        error = OzonBulkUploadError(
            "Не удалось безопасно запустить загрузку Ozon"
        )
        error.status_code = 500
        error.code = "ozon_bulk_upload_start_failed"
        return _error_response(error)
    return _created_response(acceptance)


@ozon_bulk_uploads_bp.route("/from-drafts", methods=["POST"])
@login_required
def create_from_drafts():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        data = _request_payload(
            {"account_id", "draft_ids", "expected_versions", "confirm_write", "request_key", "parent_prepare_job_uid"},
            list_fields={"draft_ids"},
        )
        _require_write_confirmation(data.get("confirm_write"))
        draft_ids = _bounded_integer_list(data.get("draft_ids"), "draft_ids")
        expected_versions = _reviewed_versions(data.get("expected_versions"), draft_ids)
        parent_uid = data.get("parent_prepare_job_uid") or None
        if parent_uid is not None and (not isinstance(parent_uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parent_uid)):
            raise OzonBulkUploadValidationError("Некорректный parent_prepare_job_uid")
        acceptance = OzonBulkUploadService.accept_reviewed_publish(
            seller_id=seller_id,
            account_id=_integer(data.get("account_id"), "account_id"),
            draft_ids=draft_ids,
            expected_versions=expected_versions,
            request_key=_request_key(data.get("request_key")),
            parent_prepare_job_uid=parent_uid,
            created_by_user_id=getattr(current_user, "id", None),
        )
    except OzonBulkUploadError as exc:
        return _error_response(exc)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Ozon draft upload start failed seller_id=%s",
            seller_id,
        )
        error = OzonBulkUploadError(
            "Не удалось безопасно запустить загрузку Ozon"
        )
        error.status_code = 500
        error.code = "ozon_bulk_upload_start_failed"
        return _error_response(error)
    return _created_response(acceptance)


def _detail_response(job_uid: str, *, force_json: bool = False):
    seller_id = _seller_id()
    if seller_id is None:
        if force_json or _wants_json():
            return jsonify({
                "success": False,
                "error": "Seller account required",
            }), 403
        return "Seller account required", 403
    try:
        job = OzonBulkUploadService.get_run(
            seller_id=seller_id,
            job_uid=job_uid,
            reconcile=True,
        )
        document = OzonBulkUploadService.public_document(job, detail=True)
    except OzonBulkUploadError as exc:
        if force_json:
            return jsonify({
                "success": False,
                "error": str(exc),
                "code": exc.code,
            }), exc.status_code
        return _error_response(exc)
    if force_json or _wants_json():
        return jsonify({"success": True, "run": document})
    return render_template(
        "ozon_bulk_upload_detail.html",
        run=document,
    )


@ozon_bulk_uploads_bp.route("/<job_uid>", methods=["GET"])
@login_required
def detail(job_uid: str):
    return _detail_response(job_uid)


@ozon_bulk_uploads_bp.route("/<job_uid>/repair", methods=["GET"])
@ozon_bulk_uploads_bp.route("/<job_uid>/repair/classic", methods=["GET"], endpoint="repair_editor_classic")
@login_required
def repair_editor(job_uid: str):
    """Render the primary, platform-native mass repair flow."""
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        editor = OzonBulkRepairService.editor_document(
            seller_id=seller_id,
            job_uid=job_uid,
        )
    except OzonBulkUploadError as exc:
        if _wants_json():
            return _error_response(exc)
        flash(str(exc), "danger")
        return redirect(url_for(
            "ozon_bulk_uploads.detail",
            job_uid=job_uid,
        ))
    if _wants_json():
        response = jsonify({"success": True, "editor": editor, "csrf": generate_csrf()})
        response.headers["Cache-Control"] = "private, no-store"
        return response
    return render_template(
        "ozon_bulk_repair_classic.html" if request.endpoint.endswith("_classic") else "ozon_bulk_repair.html",
        editor=editor,
        ozon_enabled=bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False)),
    )


@ozon_bulk_uploads_bp.route("/<job_uid>/repair/apply", methods=["POST"])
@login_required
def apply_repair_editor(job_uid: str):
    """Apply selected platform fields locally; never call Ozon."""
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    back_endpoint = ("ozon_bulk_uploads.repair_editor_classic" if request.args.get("view") == "classic"
                     else "ozon_bulk_uploads.repair_editor")
    try:
        if not current_app.config.get(
            "MARKETPLACE_OZON_ENABLED",
            False,
        ):
            raise OzonBulkUploadValidationError(
                "Изменение черновиков Ozon временно выключено оператором"
            )
        if request.is_json:
            raise OzonBulkUploadValidationError(
                "Используйте форму массового редактора Seller Hub"
            )
        if (
            request.content_length is not None
            and request.content_length
            > OzonBulkRepairService.MAX_EDITOR_FORM_BYTES
        ):
            raise OzonBulkUploadValidationError(
                "Форма массового редактора больше допустимых 2 МБ"
            )
        editor = OzonBulkRepairService.editor_document(
            seller_id=seller_id,
            job_uid=job_uid,
        )
        report = OzonBulkRepairService.apply_editor_rows(
            seller_id=seller_id,
            job_uid=job_uid,
            rows=_parse_editor_rows(editor),
            corrected_by_user_id=getattr(current_user, "id", None),
        )
    except OzonBulkUploadError as exc:
        if _wants_json():
            return _error_response(exc)
        flash(str(exc), "danger")
        return redirect(url_for(
            back_endpoint,
            job_uid=job_uid,
        ))
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Ozon platform repair failed seller_id=%s job_uid=%s",
            seller_id,
            job_uid,
        )
        error = OzonBulkUploadError(
            "Не удалось безопасно сохранить массовые исправления"
        )
        error.status_code = 500
        error.code = "ozon_bulk_repair_failed"
        if _wants_json():
            return _error_response(error)
        flash(str(error), "danger")
        return redirect(url_for(
            back_endpoint,
            job_uid=job_uid,
        ))

    if _wants_json():
        response = jsonify({"success": True, "report": report})
        response.headers["Cache-Control"] = "private, no-store"
        return response
    category = (
        "success"
        if report["ready_to_retry"] and not report["failed"]
        else "warning"
    )
    flash(
        (
            f"Сохранено и проверено: обновлено {report['updated']}, "
            f"готово к синхронизации {report['ready_to_retry']}, "
            f"ещё нужны данные {report['needs_input']}, "
            f"исключено {report['excluded']}, "
            f"ошибок строк {report['failed']}. "
            "В Ozon ничего не отправлялось."
        ),
        category,
    )
    return redirect(url_for(
        back_endpoint,
        job_uid=job_uid,
    ))


@ozon_bulk_uploads_bp.route("/<job_uid>/repair/types", methods=["GET"])
@login_required
def repair_type_search(job_uid: str):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        if set(request.args.keys()) - {"q"} or len(
            request.args.getlist("q")
        ) != 1:
            raise OzonBulkUploadValidationError(
                "Передайте один параметр q"
            )
        items = OzonBulkRepairService.search_product_types(
            seller_id=seller_id,
            job_uid=job_uid,
            query=request.args.get("q"),
        )
    except OzonBulkUploadError as exc:
        return _search_error_response(exc)
    return jsonify({"success": True, "items": items})


@ozon_bulk_uploads_bp.route(
    "/<job_uid>/repair/dictionaries/<int:draft_id>/<external_attribute_id>",
    methods=["GET"],
)
@login_required
def repair_dictionary_search(
    job_uid: str,
    draft_id: int,
    external_attribute_id: str,
):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        if set(request.args.keys()) - {"q"} or len(
            request.args.getlist("q")
        ) > 1:
            raise OzonBulkUploadValidationError(
                "Допустим только один параметр q"
            )
        result = OzonBulkRepairService.search_dictionary_values(
            seller_id=seller_id,
            job_uid=job_uid,
            draft_id=draft_id,
            external_attribute_id=external_attribute_id,
            query=request.args.get("q", ""),
        )
    except OzonBulkUploadError as exc:
        return _search_error_response(exc)
    return jsonify({"success": True, **result})


@ozon_bulk_uploads_bp.route("/<job_uid>/repair.xlsx", methods=["GET"])
@login_required
def export_repair(job_uid: str):
    """Download a bounded local repair workbook; no provider call."""
    seller_id = _seller_id()
    if seller_id is None:
        return "Seller account required", 403
    try:
        filename, payload = OzonBulkRepairService.export_workbook(
            seller_id=seller_id,
            job_uid=job_uid,
        )
    except OzonBulkUploadError as exc:
        if _wants_json():
            return _error_response(exc)
        flash(str(exc), "danger")
        return redirect(url_for(
            "ozon_bulk_uploads.detail",
            job_uid=job_uid,
        ))
    response = send_file(
        BytesIO(payload),
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@ozon_bulk_uploads_bp.route("/<job_uid>/repair", methods=["POST"])
@login_required
def import_repair(job_uid: str):
    """Apply XLSX values to drafts only; publication remains a later action."""
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        if not current_app.config.get(
            "MARKETPLACE_OZON_ENABLED",
            False,
        ):
            raise OzonBulkUploadValidationError(
                "Изменение черновиков Ozon временно выключено оператором"
            )
        if request.is_json:
            raise OzonBulkUploadValidationError(
                "Для массового исправления загрузите файл XLSX"
            )
        uploaded = request.files.get("repair_file")
        if uploaded is None:
            raise OzonBulkUploadValidationError(
                "Выберите файл XLSX с исправлениями"
            )
        payload = uploaded.stream.read(
            OzonBulkRepairService.MAX_FILE_BYTES + 1
        )
        if len(payload) > OzonBulkRepairService.MAX_FILE_BYTES:
            raise OzonBulkUploadValidationError(
                "Файл XLSX больше допустимых 2 МБ"
            )
        report = OzonBulkRepairService.import_workbook(
            seller_id=seller_id,
            job_uid=job_uid,
            payload=payload,
            corrected_by_user_id=getattr(current_user, "id", None),
        )
    except OzonBulkUploadError as exc:
        if _wants_json():
            return _error_response(exc)
        flash(str(exc), "danger")
        return redirect(url_for(
            "ozon_bulk_uploads.detail",
            job_uid=job_uid,
        ))
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Ozon repair workbook failed seller_id=%s job_uid=%s",
            seller_id,
            job_uid,
        )
        error = OzonBulkUploadError(
            "Не удалось безопасно применить таблицу исправлений"
        )
        error.status_code = 500
        error.code = "ozon_bulk_repair_failed"
        if _wants_json():
            return _error_response(error)
        flash(str(error), "danger")
        return redirect(url_for(
            "ozon_bulk_uploads.detail",
            job_uid=job_uid,
        ))

    if _wants_json():
        return jsonify({"success": True, "repair": report})
    category = (
        "success"
        if report["ready_to_retry"] and not report["failed"]
        else "warning"
    )
    flash(
        (
            f"Таблица применена локально: обновлено {report['updated']}, "
            f"готово к повтору {report['ready_to_retry']}, "
            f"ещё нужны данные {report['needs_input']}, "
            f"исключено {report['excluded']}, "
            f"ошибок строк {report['failed']}. "
            "В Ozon ничего не отправлялось."
        ),
        category,
    )
    return redirect(url_for(
        "ozon_bulk_uploads.detail",
        job_uid=job_uid,
    ))


@ozon_bulk_uploads_bp.route("/<job_uid>/retry", methods=["POST"])
@login_required
def retry(job_uid: str):
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        data = _request_payload({"confirm_prepare", "request_key"})
        _require_prepare_confirmation(data.get("confirm_prepare"))
        acceptance = OzonBulkUploadService.retry_run(
            seller_id=seller_id,
            job_uid=job_uid,
            request_key=_request_key(data.get("request_key")),
            created_by_user_id=getattr(current_user, "id", None),
        )
    except OzonBulkUploadError as exc:
        if _wants_json():
            return _error_response(exc)
        flash(str(exc), "danger")
        return redirect(url_for(
            "ozon_bulk_uploads.detail",
            job_uid=job_uid,
        ))
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Ozon bulk upload retry failed seller_id=%s job_uid=%s",
            seller_id,
            job_uid,
        )
        error = OzonBulkUploadError(
            "Не удалось безопасно повторить проблемные карточки"
        )
        error.status_code = 500
        error.code = "ozon_bulk_upload_retry_failed"
        if _wants_json():
            return _error_response(error)
        flash(str(error), "danger")
        return redirect(url_for(
            "ozon_bulk_uploads.detail",
            job_uid=job_uid,
        ))
    return _created_response(acceptance)


@ozon_bulk_uploads_bp.route("/review", methods=["GET"])
@ozon_bulk_uploads_bp.route("/api/review", methods=["GET"])
@login_required
def review():
    from services.ozon_upload_review import OzonUploadReviewService
    seller_id = _seller_id()
    as_json = request.path.endswith("/api/review") or _wants_json()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        allowed = {"account_id", "draft_ids", "parent_prepare_job_uid", "page"}
        if set(request.args) - allowed or any(len(request.args.getlist(key)) != 1 for key in request.args):
            raise OzonBulkUploadValidationError("Неизвестный или повторяющийся параметр просмотра")
        raw_ids = request.args.get("draft_ids", "")
        if len(raw_ids) > 4200:
            raise OzonBulkUploadValidationError("Слишком большой список черновиков")
        draft_ids = _bounded_integer_list(raw_ids.split(","), "draft_ids")
        parent_uid = request.args.get("parent_prepare_job_uid") or None
        if parent_uid is not None and not re.fullmatch(r"ozon-upload-[0-9a-f]{32}", parent_uid):
            raise OzonBulkUploadValidationError("Некорректный запуск подготовки")
        document = OzonUploadReviewService.document(
            seller_id=seller_id,
            account_id=_integer(request.args.get("account_id"), "account_id"),
            draft_ids=draft_ids,
            page=_integer(request.args.get("page", "1"), "page"),
            parent_prepare_job_uid=parent_uid,
        )
        document["csrf_token"] = generate_csrf()
        if as_json:
            response = jsonify({"success": True, "review": document})
        else:
            from flask import make_response
            document["request_key"] = secrets.token_urlsafe(24)
            document["expected_versions"] = {
                str(item["draft_id"]): item["version"] for item in document["items"]
                if item["selectable"]
            }
            response = make_response(render_template("ozon_upload_review.html", review=document))
    except OzonBulkUploadError as exc:
        if not as_json:
            return _error_response(exc)
        response = jsonify({"success": False, "error": str(exc), "code": exc.code})
        response.status_code = exc.status_code
    response.headers["Cache-Control"] = "private, no-store"
    return response


@ozon_bulk_uploads_bp.route("/api/by-request", methods=["GET"])
@login_required
def find_by_request():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        if set(request.args) != {"account_id"} or len(request.args.getlist("account_id")) != 1:
            raise OzonBulkUploadValidationError("Укажите один account_id")
        job = OzonBulkUploadService.find_by_request_key(
            seller_id=seller_id,
            account_id=_integer(request.args.get("account_id"), "account_id"),
            request_key=_request_key(request.headers.get("X-Upload-Request-Key")),
        )
        response = jsonify({
            "success": True,
            "run": OzonBulkUploadService.public_document(job, detail=True),
            "csrf_token": generate_csrf(),
        })
    except OzonBulkUploadError as exc:
        response = jsonify({"success": False, "error": str(exc), "code": exc.code,
                            "csrf_token": generate_csrf()})
        response.status_code = exc.status_code
    response.headers["Cache-Control"] = "private, no-store"
    return response


@ozon_bulk_uploads_bp.route("/api/<job_uid>", methods=["GET"])
@login_required
def detail_api(job_uid: str):
    return _detail_response(job_uid, force_json=True)


def register_ozon_bulk_upload_routes(app) -> None:
    app.register_blueprint(ozon_bulk_uploads_bp)
