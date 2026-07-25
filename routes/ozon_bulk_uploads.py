"""Seller-facing one-click bulk upload runs for Ozon."""

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
    if parsed <= 0:
        raise OzonBulkUploadValidationError(
            f"{field_name} должен быть положительным"
        )
    return parsed


def _bounded_integer_list(values: Any, field_name: str) -> list[int]:
    if not isinstance(values, list):
        raise OzonBulkUploadValidationError(
            f"{field_name} должен быть массивом"
        )
    if len(values) > OzonBulkUploadService.MAX_ITEMS:
        raise OzonBulkUploadValidationError(
            "За один запуск можно загрузить не более "
            f"{OzonBulkUploadService.MAX_ITEMS} карточек"
        )
    singular = (
        "draft_id" if field_name == "draft_ids" else "imported_product_id"
    )
    return [_integer(value, singular) for value in values]


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


def _created_response(job):
    document = OzonBulkUploadService.public_document(job, detail=True)
    if _wants_json():
        return jsonify({
            "success": True,
            "run": document,
        }), 202 if job.status == "running" else 200
    summary = document["summary"]
    flash(
        (
            f"Синхронизация Ozon запущена: "
            f"{summary.get('active', 0)} в работе, "
            f"{summary.get('needs_input', 0)} требуют данных. "
            "Итог и причины сохранены в одном запуске."
        ),
        "success" if not summary.get("needs_input") else "warning",
    )
    return redirect(url_for(
        "ozon_bulk_uploads.detail",
        job_uid=job.job_uid,
    ))


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
        runs = OzonBulkUploadService.list_runs(
            seller_id=seller_id,
            limit=50,
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
        if request.is_json:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise OzonBulkUploadValidationError(
                    "JSON body должен быть объектом"
                )
            if set(data) - {
                "account_id",
                "imported_product_ids",
                "confirm_write",
            }:
                raise OzonBulkUploadValidationError(
                    "Допустимы только account_id, imported_product_ids "
                    "и confirm_write"
                )
            _require_write_confirmation(data.get("confirm_write"))
            raw_ids = data.get("imported_product_ids")
            account_id = _integer(data.get("account_id"), "account_id")
            product_ids = _bounded_integer_list(
                raw_ids,
                "imported_product_ids",
            )
        else:
            _require_write_confirmation(
                request.form.get("confirm_write"),
            )
            account_id = _integer(
                request.form.get("account_id"),
                "account_id",
            )
            product_ids = _bounded_integer_list(
                request.form.getlist("imported_product_ids"),
                "imported_product_ids",
            )
        job = OzonBulkUploadService.create_run(
            seller_id=seller_id,
            account_id=account_id,
            imported_product_ids=product_ids,
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
    return _created_response(job)


@ozon_bulk_uploads_bp.route("/from-drafts", methods=["POST"])
@login_required
def create_from_drafts():
    seller_id = _seller_id()
    if seller_id is None:
        return jsonify({"success": False, "error": "Seller account required"}), 403
    try:
        if request.is_json:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise OzonBulkUploadValidationError(
                    "JSON body должен быть объектом"
                )
            if set(data) - {"account_id", "draft_ids", "confirm_write"}:
                raise OzonBulkUploadValidationError(
                    "Допустимы только account_id, draft_ids и confirm_write"
                )
            _require_write_confirmation(data.get("confirm_write"))
            raw_ids = data.get("draft_ids")
            account_id = _integer(data.get("account_id"), "account_id")
            draft_ids = _bounded_integer_list(raw_ids, "draft_ids")
        else:
            _require_write_confirmation(
                request.form.get("confirm_write"),
            )
            account_id = _integer(
                request.form.get("account_id"),
                "account_id",
            )
            draft_ids = _bounded_integer_list(
                request.form.getlist("draft_ids"),
                "draft_ids",
            )
        job = OzonBulkUploadService.create_run_from_drafts(
            seller_id=seller_id,
            account_id=account_id,
            draft_ids=draft_ids,
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
    return _created_response(job)


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
        return jsonify({"success": True, "editor": editor})
    return render_template(
        "ozon_bulk_repair.html",
        editor=editor,
    )


@ozon_bulk_uploads_bp.route("/<job_uid>/repair/apply", methods=["POST"])
@login_required
def apply_repair_editor(job_uid: str):
    """Apply selected platform fields locally; never call Ozon."""
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
            "ozon_bulk_uploads.repair_editor",
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
            "ozon_bulk_uploads.repair_editor",
            job_uid=job_uid,
        ))

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
        "ozon_bulk_uploads.repair_editor",
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
        if request.is_json:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise OzonBulkUploadValidationError(
                    "JSON body должен быть объектом"
                )
            if set(data) - {"confirm_write"}:
                raise OzonBulkUploadValidationError(
                    "Допустим только confirm_write"
                )
            _require_write_confirmation(data.get("confirm_write"))
        else:
            _require_write_confirmation(
                request.form.get("confirm_write"),
            )
        job = OzonBulkUploadService.retry_run(
            seller_id=seller_id,
            job_uid=job_uid,
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
    return _created_response(job)


@ozon_bulk_uploads_bp.route("/api/<job_uid>", methods=["GET"])
@login_required
def detail_api(job_uid: str):
    return _detail_response(job_uid, force_json=True)


def register_ozon_bulk_upload_routes(app) -> None:
    app.register_blueprint(ozon_bulk_uploads_bp)
