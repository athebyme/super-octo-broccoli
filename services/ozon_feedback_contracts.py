"""Strict, ORM-free contracts for the current Ozon review/question inbox.

Reviews use the 2026 ``/v2/review/list`` status model. Questions remain on
``/v1/question/list``. Customer names, links and arbitrary provider payloads
are deliberately excluded from normalized rows.
"""

from datetime import date, datetime, time, timezone
from typing import Any, Dict, Mapping, Optional, Sequence
import re


class OzonFeedbackContractError(ValueError):
    """A local request or provider response violates the inbox contract."""


INBOX_STATUSES = ("NEW", "VIEWED", "PROCESSED")
MAX_PAGE_SIZE = 100
MAX_ROWS_PER_PAGE = 100
MAX_TEXT_LENGTH = 10_000
RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OzonFeedbackContractError(f"{field_name} must be an object")
    return value


def _rows(value: Any, field_name: str) -> Sequence[Any]:
    if not isinstance(value, list) or len(value) > MAX_ROWS_PER_PAGE:
        raise OzonFeedbackContractError(
            f"{field_name} must be a list with at most {MAX_ROWS_PER_PAGE} rows"
        )
    return value


def _text(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    optional: bool = False,
) -> Optional[str]:
    if value in (None, "") and optional:
        return None
    if not isinstance(value, str):
        raise OzonFeedbackContractError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        if optional:
            return None
        raise OzonFeedbackContractError(f"{field_name} must be non-empty")
    if len(normalized) > maximum:
        raise OzonFeedbackContractError(
            f"{field_name} exceeds {maximum} characters"
        )
    if any(ord(character) < 32 and character not in "\n\r\t" for character in normalized):
        raise OzonFeedbackContractError(f"{field_name} contains control characters")
    if any(ord(character) == 127 for character in normalized):
        raise OzonFeedbackContractError(f"{field_name} contains control characters")
    return normalized


def _identifier(value: Any, field_name: str, *, maximum: int = 200) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        if value <= 0:
            raise OzonFeedbackContractError(f"{field_name} must be positive")
        value = str(value)
    return _text(value, field_name, maximum=maximum)


def _positive_integer(value: Any, field_name: str, *, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise OzonFeedbackContractError(
            f"{field_name} must be a positive integer not greater than {maximum}"
        )
    return value


def _nonnegative_integer(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise OzonFeedbackContractError(
            f"{field_name} must be a non-negative integer"
        )
    return value


def _optional_nonnegative_integer(value: Any, field_name: str) -> int:
    if value is None:
        return 0
    return _nonnegative_integer(value, field_name)


def _required_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise OzonFeedbackContractError(f"{field_name} must be boolean")
    return value


def _optional_bool(value: Any, field_name: str) -> Optional[bool]:
    if value is None:
        return None
    return _required_bool(value, field_name)


def _status(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name, maximum=20)
    if normalized not in INBOX_STATUSES:
        raise OzonFeedbackContractError(f"{field_name} is unsupported")
    return normalized


def _cursor(value: Any, field_name: str, *, optional: bool = False) -> Optional[str]:
    if value in (None, "") and optional:
        return None
    return _identifier(value, field_name, maximum=200)


def _day(value: Any, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    raw = _text(value, field_name, maximum=10)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        raise OzonFeedbackContractError(f"{field_name} must be YYYY-MM-DD") from None
    if parsed.isoformat() != raw:
        raise OzonFeedbackContractError(f"{field_name} must be canonical YYYY-MM-DD")
    return parsed


def _timestamp(value: Any, field_name: str) -> datetime:
    raw = _text(value, field_name, maximum=50)
    if RFC3339_TIMESTAMP.fullmatch(raw) is None:
        raise OzonFeedbackContractError(f"{field_name} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise OzonFeedbackContractError(f"{field_name} must be RFC3339") from None
    if parsed.tzinfo is None:
        raise OzonFeedbackContractError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _window(date_from: Any, date_to: Any) -> tuple:
    start = _day(date_from, "date_from")
    end = _day(date_to, "date_to")
    if start > end or (end - start).days > 92:
        raise OzonFeedbackContractError(
            "feedback date window must be ordered and at most 93 days"
        )
    start_at = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end, time(23, 59, 59), tzinfo=timezone.utc)
    return (
        start_at.isoformat().replace("+00:00", "Z"),
        end_at.isoformat().replace("+00:00", "Z"),
    )


def build_review_list_request(
    *,
    status: Any,
    date_from: Any,
    date_to: Any,
    limit: Any = MAX_PAGE_SIZE,
    last_id: Any = None,
) -> Dict[str, Any]:
    """Build one bounded page for ``/v2/review/list``."""
    start_at, end_at = _window(date_from, date_to)
    payload: Dict[str, Any] = {
        "limit": _positive_integer(limit, "limit", maximum=MAX_PAGE_SIZE),
        "sort_dir": "DESC",
        "filters": {
            "status": _status(status, "status"),
            "published_from": start_at,
            "published_to": end_at,
        },
    }
    cursor = _cursor(last_id, "last_id", optional=True)
    if cursor is not None:
        payload["last_id"] = cursor
    return payload


def build_question_list_request(
    *,
    status: Any,
    date_from: Any,
    date_to: Any,
    limit: Any = MAX_PAGE_SIZE,
    last_id: Any = None,
) -> Dict[str, Any]:
    """Build one bounded page for ``/v1/question/list``."""
    start_at, end_at = _window(date_from, date_to)
    payload: Dict[str, Any] = {
        "limit": _positive_integer(limit, "limit", maximum=MAX_PAGE_SIZE),
        "sort_dir": "DESC",
        "filter": {
            "status": _status(status, "status"),
            "date_from": start_at,
            "date_to": end_at,
        },
    }
    cursor = _cursor(last_id, "last_id", optional=True)
    if cursor is not None:
        payload["last_id"] = cursor
    return payload


def _review(raw: Any, field_name: str, requested_status: str) -> Dict[str, Any]:
    row = _mapping(raw, field_name)
    status = _status(row.get("status"), f"{field_name}.status")
    if status != requested_status:
        raise OzonFeedbackContractError(
            f"{field_name}.status escaped the requested status"
        )
    rating = _positive_integer(
        row.get("rating"),
        f"{field_name}.rating",
        maximum=5,
    )
    text = _text(
        row.get("text"),
        f"{field_name}.text",
        maximum=MAX_TEXT_LENGTH,
        optional=True,
    )
    photos = _optional_nonnegative_integer(
        row.get("photos_amount"),
        f"{field_name}.photos_amount",
    )
    videos = _optional_nonnegative_integer(
        row.get("videos_amount"),
        f"{field_name}.videos_amount",
    )
    return {
        "kind": "review",
        "external_id": _identifier(row.get("id"), f"{field_name}.id"),
        "sku": _identifier(row.get("sku"), f"{field_name}.sku", maximum=100),
        "text": text,
        "rating": rating,
        "status": status,
        "order_status": _text(
            row.get("order_status"),
            f"{field_name}.order_status",
            maximum=100,
            optional=True,
        ),
        "published_at": _timestamp(
            row.get("published_at"),
            f"{field_name}.published_at",
        ),
        "is_rating_participant": _optional_bool(
            row.get("is_rating_participant"),
            f"{field_name}.is_rating_participant",
        ),
        "comments_count": _optional_nonnegative_integer(
            row.get("comments_amount"),
            f"{field_name}.comments_amount",
        ),
        "photos_count": photos,
        "videos_count": videos,
        "answers_count": 0,
        "reply_eligible": bool(text or photos or videos),
    }


def _question(raw: Any, field_name: str, requested_status: str) -> Dict[str, Any]:
    row = _mapping(raw, field_name)
    status = _status(row.get("status"), f"{field_name}.status")
    if status != requested_status:
        raise OzonFeedbackContractError(
            f"{field_name}.status escaped the requested status"
        )
    text = _text(
        row.get("text"),
        f"{field_name}.text",
        maximum=MAX_TEXT_LENGTH,
    )
    return {
        "kind": "question",
        "external_id": _identifier(row.get("id"), f"{field_name}.id"),
        "sku": _identifier(row.get("sku"), f"{field_name}.sku", maximum=100),
        "text": text,
        "rating": None,
        "status": status,
        "order_status": None,
        "published_at": _timestamp(
            row.get("published_at"),
            f"{field_name}.published_at",
        ),
        "is_rating_participant": None,
        "comments_count": 0,
        "photos_count": 0,
        "videos_count": 0,
        "answers_count": _optional_nonnegative_integer(
            row.get("answers_count"),
            f"{field_name}.answers_count",
        ),
        "reply_eligible": True,
    }


def _normalize_page(
    response: Any,
    *,
    rows_field: str,
    requested_status: Any,
    requested_last_id: Any,
    normalizer,
) -> Dict[str, Any]:
    payload = _mapping(response, "response")
    status = _status(requested_status, "requested_status")
    current_cursor = _cursor(
        requested_last_id,
        "requested_last_id",
        optional=True,
    )
    raw_rows = _rows(payload.get(rows_field), rows_field)
    rows = [
        normalizer(raw, f"{rows_field}[{index}]", status)
        for index, raw in enumerate(raw_rows)
    ]
    identities = [row["external_id"] for row in rows]
    if len(identities) != len(set(identities)):
        raise OzonFeedbackContractError(f"{rows_field} contains duplicate ids")

    has_next = _required_bool(payload.get("has_next"), "has_next")
    next_cursor = _cursor(payload.get("last_id"), "last_id", optional=True)
    if has_next:
        if not rows or next_cursor is None:
            raise OzonFeedbackContractError(
                "non-terminal inbox page requires rows and last_id"
            )
        if next_cursor == current_cursor:
            raise OzonFeedbackContractError("inbox cursor did not advance")
    return {
        "rows": rows,
        "has_next": has_next,
        "next_last_id": next_cursor if has_next else None,
    }


def normalize_review_list_response(
    response: Any,
    *,
    requested_status: Any,
    requested_last_id: Any = None,
) -> Dict[str, Any]:
    return _normalize_page(
        response,
        rows_field="reviews",
        requested_status=requested_status,
        requested_last_id=requested_last_id,
        normalizer=_review,
    )


def normalize_question_list_response(
    response: Any,
    *,
    requested_status: Any,
    requested_last_id: Any = None,
) -> Dict[str, Any]:
    return _normalize_page(
        response,
        rows_field="questions",
        requested_status=requested_status,
        requested_last_id=requested_last_id,
        normalizer=_question,
    )
