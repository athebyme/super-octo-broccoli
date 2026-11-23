"""Seller-facing presentation for legacy WB bulk history records."""

from collections.abc import Mapping


_REVIEW_COUNT_FIELDS = ("selected", "eligible", "changed", "skipped")


def _nonnegative_int(value):
    return type(value) is int and value >= 0


def validated_wb_review_summary(operation):
    """Return the established reviewed-batch counters when their shape is valid.

    Legacy operations and malformed historical JSON do not get inferred review
    counts. The stored summary is only used as a denominator after validating
    its typed counters and checking it against the operation's selected count.
    """
    operation_params = getattr(operation, "operation_params", None)
    if not isinstance(operation_params, Mapping):
        return None
    raw_summary = operation_params.get("review_summary")
    if not isinstance(raw_summary, Mapping):
        return None

    summary = {}
    for field in _REVIEW_COUNT_FIELDS:
        value = raw_summary.get(field)
        if not _nonnegative_int(value):
            return None
        summary[field] = value

    total_products = getattr(operation, "total_products", None)
    if (
        not _nonnegative_int(total_products)
        or summary["selected"] != total_products
        or summary["eligible"] > summary["selected"]
        or summary["changed"] > summary["eligible"]
        or summary["skipped"] > summary["selected"]
        or summary["changed"] + summary["skipped"] > summary["selected"]
    ):
        return None

    raw_errors = raw_summary.get("errors")
    if _nonnegative_int(raw_errors):
        if raw_errors > 0:
            return None
        summary["errors"] = raw_errors
    elif raw_errors is not None:
        return None
    return summary


def _legacy_completed_presentation(operation):
    success_count = getattr(operation, "success_count", None)
    error_count = getattr(operation, "error_count", None)
    total_products = getattr(operation, "total_products", None)

    if not all(
        _nonnegative_int(value)
        for value in (success_count, error_count, total_products)
    ):
        return {
            "tone": "unknown",
            "label": "Завершено, счётчики результата не подтверждены",
            "message": (
                "В записи истории отсутствуют корректные числовые счётчики. "
                "Проверьте детали и статусы строк в WB. Не повторяйте изменение, "
                "пока исход не установлен."
            ),
        }

    if success_count + error_count > total_products:
        return {
            "tone": "unknown",
            "label": "Завершено, счётчики результата не согласуются с числом товаров",
            "message": (
                f"Без ошибки обработано {success_count} из {total_products} строк. "
                f"Ошибок: {error_count}. Счётчики превышают общее число товаров. "
                "Проверьте детали и статусы строк в WB. Не повторяйте изменение, "
                "пока исход не установлен."
            ),
        }

    if error_count > 0 and success_count > 0:
        return {
            "tone": "partial",
            "label": "Завершено частично",
            "message": (
                f"Без ошибки обработано {success_count} из {total_products} строк. "
                f"Ошибок: {error_count}. Результат отправки в WB смотрите "
                "отдельно по каждой строке."
            ),
        }
    if error_count > 0:
        return {
            "tone": "error",
            "label": "Завершено с ошибками",
            "message": (
                f"Без ошибки обработано {success_count} из {total_products} строк. "
                f"Ошибок: {error_count}. Откройте детали, чтобы проверить "
                "причины и доступные действия."
            ),
        }
    if total_products > 0 and success_count >= total_products:
        return {
            "tone": "complete",
            "label": "Завершено",
            "message": (
                f"Без общей ошибки обработано {success_count} строк. "
                "Состояние синхронизации с WB показывается отдельно."
            ),
        }
    return {
        "tone": "unknown",
        "label": "Завершено, успешный результат не подтверждён счётчиками",
        "message": (
            "Проверьте детали и статусы строк в WB. Не повторяйте изменение, "
            "пока исход не установлен."
        ),
    }


def _reviewed_completed_presentation(operation, summary):
    success_count = getattr(operation, "success_count", None)
    error_count = getattr(operation, "error_count", None)
    changed_count = summary["changed"]
    selected_count = summary["selected"]
    skipped_count = summary["skipped"]

    if (
        not _nonnegative_int(success_count)
        or not _nonnegative_int(error_count)
        or success_count + error_count != changed_count
    ):
        observed_counts = ""
        if _nonnegative_int(success_count) and _nonnegative_int(error_count):
            observed_counts = (
                f" Без ошибки обработано {success_count} из {changed_count}. "
                f"Ошибок операции: {error_count}."
            )
        return {
            "tone": "unknown",
            "label": "Завершено, успешный результат не подтверждён счётчиками",
            "message": (
                f"Выбрано {selected_count}. Подготовлено к записи {changed_count}. "
                f"Пропущено {skipped_count}.{observed_counts} "
                "Счётчики выполнения не подтверждают полный результат. "
                "Проверьте детали и статусы строк в WB. "
                "Не повторяйте изменение, пока исход не установлен."
            ),
        }

    if changed_count > 0:
        execution_counts = (
            f"Выбрано {selected_count}. Подготовлено к записи {changed_count}. "
            f"Без ошибки обработано {success_count} из {changed_count}. "
            f"Ошибок операции: {error_count}. Пропущено {skipped_count}."
        )
        if error_count > 0 and success_count > 0:
            return {
                "tone": "partial",
                "label": "Завершено частично",
                "message": (
                    f"{execution_counts} Результат отправки в WB смотрите отдельно "
                    "по каждой строке."
                ),
            }
        if error_count > 0:
            return {
                "tone": "error",
                "label": "Завершено с ошибками",
                "message": (
                    f"{execution_counts} Откройте детали, чтобы проверить причины "
                    "и доступные действия."
                ),
            }
        if success_count == changed_count:
            return {
                "tone": "complete",
                "label": "Завершено",
                "message": (
                    f"{execution_counts} Состояние синхронизации с WB показывается "
                    "отдельно по каждой строке."
                ),
            }

    return {
        "tone": "unknown",
        "label": "Завершено, изменений к записи не подготовлено",
        "message": (
            f"Выбрано {selected_count}. Подготовлено к записи 0. "
            f"Пропущено {skipped_count}. Откройте детали операции и проверьте "
            "её сохранённые строки."
        ),
    }


def present_wb_bulk_operation(operation):
    """Build a trusted, count-correct view for the shared history result UI."""
    summary = validated_wb_review_summary(operation)
    status = getattr(operation, "status", None)

    if status == "completed":
        result = (
            _reviewed_completed_presentation(operation, summary)
            if summary is not None
            else _legacy_completed_presentation(operation)
        )
    elif status == "in_progress":
        result = {
            "tone": "running",
            "label": "Выполняется",
            "message": "Дождитесь окончания обработки, затем откройте результат по строкам.",
        }
    elif status == "failed":
        result = {
            "tone": "error",
            "label": "Ошибка выполнения",
            "message": (
                "Откройте причины и статусы строк. Новый запуск требует проверки "
                "фактического состояния WB и повторного просмотра значений."
            ),
        }
    elif status == "pending":
        result = {
            "tone": "running",
            "label": "Ожидает выполнения",
            "message": "Операция ещё не завершена. Сверьте строки после обработки.",
        }
    else:
        result = {
            "tone": "unknown",
            "label": "Исход не распознан",
            "message": "Проверьте детали истории. Повтор не предлагается, пока исход не установлен.",
        }

    result["status"] = status
    result["review_summary"] = summary
    total_products = getattr(operation, "total_products", None)
    success_count = getattr(operation, "success_count", None)
    error_count = getattr(operation, "error_count", None)
    outcome_counts_available = (
        summary is None or status not in {"pending", "in_progress"}
    )
    result["counts"] = {
        "total_products": (
            total_products
            if _nonnegative_int(total_products)
            else None
        ),
        "success_count": (
            success_count
            if outcome_counts_available
            and _nonnegative_int(success_count)
            else None
        ),
        "error_count": (
            error_count
            if outcome_counts_available
            and _nonnegative_int(error_count)
            else None
        ),
    }
    result["reviewed_counts_reconciled"] = bool(
        status == "completed"
        and summary is not None
        and _nonnegative_int(success_count)
        and _nonnegative_int(error_count)
        and success_count + error_count == summary["changed"]
    )
    visible_counts = result["counts"]
    visible_total = visible_counts["total_products"]
    visible_success = visible_counts["success_count"]
    visible_errors = visible_counts["error_count"]
    result["legacy_success_ratio_available"] = bool(
        summary is None
        and _nonnegative_int(visible_total)
        and visible_total > 0
        and _nonnegative_int(visible_success)
        and _nonnegative_int(visible_errors)
        and visible_success + visible_errors <= visible_total
    )
    result["reverted"] = bool(getattr(operation, "reverted", False))
    result["is_pending"] = status in {"pending", "in_progress"}
    return result
