"""Strict, ORM-free contracts for read-only Ozon fulfillment feeds.

Only fields needed by Seller Hub are normalized.  Buyer names, phones,
addresses, comments, photos, barcodes and provider ``financial_data`` are never
returned by this module and therefore cannot accidentally enter persistence.
"""

from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


class OzonFulfillmentContractError(ValueError):
    """A local request or provider response violates the bounded contract."""


POSTING_PAGE_LIMIT = 1000
RETURN_PAGE_LIMIT = 500
MAX_PERIOD_DAYS = 31
MAX_ITEMS_PER_POSTING = 1000
FULFILLMENT_KINDS = {"fbo", "fbs"}


def _integer(
    value: Any,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise OzonFulfillmentContractError(
            f"{field_name} must be an integer between {minimum} and {maximum}"
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
        raise OzonFulfillmentContractError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        if optional:
            return None
        raise OzonFulfillmentContractError(f"{field_name} must be non-empty")
    if len(normalized) > maximum:
        raise OzonFulfillmentContractError(
            f"{field_name} exceeds {maximum} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise OzonFulfillmentContractError(
            f"{field_name} contains control characters"
        )
    return normalized


def _identifier(
    value: Any,
    field_name: str,
    *,
    maximum: int = 200,
    optional: bool = False,
) -> Optional[str]:
    if value in (None, "") and optional:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if value <= 0:
            raise OzonFulfillmentContractError(f"{field_name} must be positive")
        value = str(value)
    return _text(value, field_name, maximum=maximum, optional=optional)


def _decimal(
    value: Any,
    field_name: str,
    *,
    optional: bool = False,
) -> Optional[Decimal]:
    if value in (None, "") and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise OzonFulfillmentContractError(
            f"{field_name} must be a decimal string or JSON number"
        )
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OzonFulfillmentContractError(f"{field_name} is invalid") from None
    if not normalized.is_finite() or normalized < 0:
        raise OzonFulfillmentContractError(
            f"{field_name} must be finite and non-negative"
        )
    if normalized > Decimal("9999999999999999.9999"):
        raise OzonFulfillmentContractError(f"{field_name} exceeds storage bounds")
    return normalized.quantize(Decimal("0.0001"))


def _currency(value: Any, field_name: str) -> Optional[str]:
    normalized = _text(value, field_name, maximum=3, optional=True)
    if normalized is None:
        return None
    normalized = normalized.upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise OzonFulfillmentContractError(
            f"{field_name} must be a three-letter currency code"
        )
    return normalized


def _timestamp(
    value: Any,
    field_name: str,
    *,
    optional: bool = True,
) -> Optional[datetime]:
    if value in (None, "", "0001-01-01T00:00:00Z") and optional:
        return None
    raw = _text(value, field_name, maximum=80, optional=optional)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise OzonFulfillmentContractError(
            f"{field_name} must be an RFC3339 timestamp"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OzonFulfillmentContractError(
            f"{field_name} must include a timezone"
        )
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _boundary(value: Any, field_name: str, *, end: bool) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise OzonFulfillmentContractError(
                f"{field_name} datetime must include a timezone"
            )
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        boundary_time = time.max if end else time.min
        return datetime.combine(value, boundary_time, tzinfo=timezone.utc)
    raise OzonFulfillmentContractError(
        f"{field_name} must be a date or timezone-aware datetime"
    )


def _period(period_start: Any, period_end: Any) -> Tuple[datetime, datetime]:
    start = _boundary(period_start, "period_start", end=False)
    end = _boundary(period_end, "period_end", end=True)
    if start > end:
        raise OzonFulfillmentContractError(
            "period_start must not exceed period_end"
        )
    if (end.date() - start.date()).days + 1 > MAX_PERIOD_DAYS:
        raise OzonFulfillmentContractError(
            f"fulfillment period must not exceed {MAX_PERIOD_DAYS} days"
        )
    return start, end


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def build_posting_request(
    *,
    fulfillment_kind: str,
    period_start: Any,
    period_end: Any,
    offset: Any = 0,
    limit: Any = POSTING_PAGE_LIMIT,
) -> Dict[str, Any]:
    """Build the minimal FBO/FBS request without analytics, PII or finance."""
    if fulfillment_kind not in FULFILLMENT_KINDS:
        raise OzonFulfillmentContractError("fulfillment_kind is unsupported")
    start, end = _period(period_start, period_end)
    normalized_offset = _integer(
        offset, "offset", minimum=0, maximum=10_000_000
    )
    normalized_limit = _integer(
        limit, "limit", minimum=1, maximum=POSTING_PAGE_LIMIT
    )
    payload: Dict[str, Any] = {
        "dir": "ASC",
        "filter": {"since": _rfc3339(start), "to": _rfc3339(end)},
        "limit": normalized_limit,
        "offset": normalized_offset,
        "with": {"analytics_data": False, "financial_data": False},
    }
    if fulfillment_kind == "fbs":
        payload["with"]["barcodes"] = False
    else:
        payload["translit"] = False
    return payload


def build_return_request(
    *,
    period_start: Any,
    period_end: Any,
    last_id: Any = 0,
    limit: Any = RETURN_PAGE_LIMIT,
) -> Dict[str, Any]:
    start, end = _period(period_start, period_end)
    return {
        "filter": {
            "visual_status_change_moment": {
                "time_from": _rfc3339(start),
                "time_to": _rfc3339(end),
            }
        },
        "last_id": _integer(
            last_id, "last_id", minimum=0, maximum=9_223_372_036_854_775_807
        ),
        "limit": _integer(limit, "limit", minimum=1, maximum=RETURN_PAGE_LIMIT),
    }


def build_rfbs_return_request(
    *,
    period_start: Any,
    period_end: Any,
    last_id: Any = 0,
    limit: Any = RETURN_PAGE_LIMIT,
) -> Dict[str, Any]:
    start, end = _period(period_start, period_end)
    return {
        "filter": {
            "created_at": {"from": _rfc3339(start), "to": _rfc3339(end)}
        },
        "last_id": _integer(
            last_id, "last_id", minimum=0, maximum=9_223_372_036_854_775_807
        ),
        "limit": _integer(limit, "limit", minimum=1, maximum=RETURN_PAGE_LIMIT),
    }


def build_conditional_cancellation_request(
    *,
    last_id: Any = 0,
    limit: Any = RETURN_PAGE_LIMIT,
) -> Dict[str, Any]:
    return {
        "filters": {"state": "ALL"},
        "last_id": _integer(
            last_id, "last_id", minimum=0, maximum=9_223_372_036_854_775_807
        ),
        "limit": _integer(limit, "limit", minimum=1, maximum=RETURN_PAGE_LIMIT),
        "with": {"counter": False},
    }


def _mapping(value: Any, field_name: str, *, optional: bool = False) -> Mapping[str, Any]:
    if value is None and optional:
        return {}
    if not isinstance(value, Mapping):
        raise OzonFulfillmentContractError(f"{field_name} must be an object")
    return value


def _bounded_rows(value: Any, field_name: str, *, limit: int) -> Sequence[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise OzonFulfillmentContractError(
            f"{field_name} must be a list with at most {limit} rows"
        )
    return value


def _state(raw: Any, field_name: str) -> Tuple[str, Optional[str]]:
    if isinstance(raw, str):
        return _text(raw, field_name, maximum=120), None
    state = _mapping(raw, field_name)
    code = state.get("state")
    if code in (None, ""):
        code = state.get("sys_name")
    if code in (None, ""):
        code = state.get("group_state")
    label = state.get("name")
    if label in (None, ""):
        label = state.get("display_name")
    if label in (None, ""):
        label = state.get("state_name")
    return (
        _text(code, f"{field_name}.code", maximum=120),
        _text(label, f"{field_name}.label", maximum=300, optional=True),
    )


def _product(raw: Any, field_name: str, *, quantity_default: int = 1) -> Dict[str, Any]:
    product = _mapping(raw, field_name)
    sku = _identifier(product.get("sku"), f"{field_name}.sku", maximum=100, optional=True)
    offer_id = _identifier(
        product.get("offer_id"),
        f"{field_name}.offer_id",
        maximum=200,
        optional=True,
    )
    if sku is None and offer_id is None:
        raise OzonFulfillmentContractError(
            f"{field_name} must contain sku or offer_id"
        )
    quantity = product.get("quantity", quantity_default)
    quantity = _integer(
        quantity,
        f"{field_name}.quantity",
        minimum=1,
        maximum=1_000_000,
    )
    raw_price = product.get("price")
    currency_value = product.get("currency_code")
    if isinstance(raw_price, Mapping):
        currency_value = raw_price.get("currency_code", currency_value)
        raw_price = raw_price.get("price")
    return {
        "sku": sku,
        "offer_id": offer_id,
        "name": _text(
            product.get("name"),
            f"{field_name}.name",
            maximum=500,
            optional=True,
        ),
        "quantity": quantity,
        "unit_price": _decimal(
            raw_price,
            f"{field_name}.price",
            optional=True,
        ),
        "currency": _currency(currency_value, f"{field_name}.currency_code"),
    }


def _posting_row(raw: Any, field_name: str, fulfillment_kind: str) -> Dict[str, Any]:
    row = _mapping(raw, field_name)
    raw_products = _bounded_rows(
        row.get("products"),
        f"{field_name}.products",
        limit=MAX_ITEMS_PER_POSTING,
    )
    if not raw_products:
        raise OzonFulfillmentContractError(
            f"{field_name}.products must not be empty"
        )
    products = []
    seen_products = set()
    for index, raw_product in enumerate(raw_products):
        product = _product(raw_product, f"{field_name}.products[{index}]")
        identity = (product["offer_id"], product["sku"])
        if identity in seen_products:
            raise OzonFulfillmentContractError(
                f"{field_name}.products contains duplicate identities"
            )
        seen_products.add(identity)
        products.append(product)

    cancellation = _mapping(
        row.get("cancellation"), f"{field_name}.cancellation", optional=True
    )
    reason_code = cancellation.get("cancel_reason_id")
    if reason_code in (None, 0, ""):
        reason_code = cancellation.get("cancellation_reason_id")
    reason_text = cancellation.get("cancel_reason")
    if reason_text in (None, ""):
        reason_text = cancellation.get("cancellation_reason")
    return {
        "posting_number": _identifier(
            row.get("posting_number"), f"{field_name}.posting_number"
        ),
        "external_order_id": _identifier(
            row.get("order_id"),
            f"{field_name}.order_id",
            maximum=200,
            optional=True,
        ),
        "external_order_number": _identifier(
            row.get("order_number"),
            f"{field_name}.order_number",
            maximum=200,
            optional=True,
        ),
        "fulfillment_kind": fulfillment_kind,
        "status": _text(row.get("status"), f"{field_name}.status", maximum=120),
        "substatus": _text(
            row.get("substatus", row.get("sub_status")),
            f"{field_name}.substatus",
            maximum=120,
            optional=True,
        ),
        "created_at": _timestamp(
            row.get("created_at", row.get("in_process_at")),
            f"{field_name}.created_at",
        ),
        "shipment_at": _timestamp(
            row.get("shipment_date", row.get("delivering_date")),
            f"{field_name}.shipment_at",
        ),
        "delivered_at": _timestamp(
            row.get("delivered_at"), f"{field_name}.delivered_at"
        ),
        "cancelled_at": _timestamp(
            row.get("cancelled_at", cancellation.get("cancelled_at")),
            f"{field_name}.cancelled_at",
        ),
        "cancellation_reason_code": _identifier(
            reason_code,
            f"{field_name}.cancellation_reason_code",
            maximum=100,
            optional=True,
        ),
        "cancellation_reason": _text(
            reason_text,
            f"{field_name}.cancellation_reason",
            maximum=500,
            optional=True,
        ),
        "products": products,
    }


def normalize_posting_response(
    response: Any,
    *,
    fulfillment_kind: str,
    requested_limit: int,
    requested_offset: int,
) -> Dict[str, Any]:
    if fulfillment_kind not in FULFILLMENT_KINDS:
        raise OzonFulfillmentContractError("fulfillment_kind is unsupported")
    limit = _integer(
        requested_limit, "requested_limit", minimum=1, maximum=POSTING_PAGE_LIMIT
    )
    offset = _integer(
        requested_offset, "requested_offset", minimum=0, maximum=10_000_000
    )
    envelope = _mapping(response, "posting response")
    result = envelope.get("result")
    explicit_has_next: Optional[bool] = None
    if isinstance(result, list):
        raw_rows = result
    else:
        result = _mapping(result, "posting response.result")
        raw_rows = result.get("postings")
        if "has_next" in result:
            explicit_has_next = result.get("has_next")
            if not isinstance(explicit_has_next, bool):
                raise OzonFulfillmentContractError(
                    "posting response.result.has_next must be boolean"
                )
    rows = _bounded_rows(raw_rows, "posting response rows", limit=limit)
    normalized = []
    seen = set()
    for index, raw_row in enumerate(rows):
        item = _posting_row(
            raw_row,
            f"posting response rows[{index}]",
            fulfillment_kind,
        )
        if item["posting_number"] in seen:
            raise OzonFulfillmentContractError(
                "posting response contains duplicate posting numbers"
            )
        seen.add(item["posting_number"])
        normalized.append(item)
    has_next = explicit_has_next if explicit_has_next is not None else len(rows) == limit
    if has_next and not rows:
        raise OzonFulfillmentContractError(
            "posting response cannot continue after an empty page"
        )
    return {
        "rows": normalized,
        "has_next": has_next,
        "next_offset": offset + len(rows),
    }


def _v1_return_row(raw: Any, field_name: str) -> Dict[str, Any]:
    row = _mapping(raw, field_name)
    visual = _mapping(row.get("visual"), f"{field_name}.visual")
    status, status_label = _state(
        visual.get("status"), f"{field_name}.visual.status"
    )
    logistic = _mapping(
        row.get("logistic"), f"{field_name}.logistic", optional=True
    )
    return {
        "source_kind": "fbo_fbs",
        "external_return_id": _identifier(
            row.get("id"), f"{field_name}.id", maximum=100
        ),
        "posting_number": _identifier(
            row.get("posting_number"),
            f"{field_name}.posting_number",
            optional=True,
        ),
        "external_order_id": _identifier(
            row.get("order_id"),
            f"{field_name}.order_id",
            maximum=200,
            optional=True,
        ),
        "fulfillment_kind": _text(
            row.get("schema"),
            f"{field_name}.schema",
            maximum=80,
            optional=True,
        ),
        "status": status,
        "status_label": status_label,
        "reason": _text(
            row.get("return_reason_name"),
            f"{field_name}.return_reason_name",
            maximum=500,
            optional=True,
        ),
        "created_at": _timestamp(
            logistic.get("return_date"), f"{field_name}.logistic.return_date"
        ),
        "status_changed_at": _timestamp(
            visual.get("change_moment"), f"{field_name}.visual.change_moment"
        ),
        "completed_at": _timestamp(
            logistic.get("final_moment"), f"{field_name}.logistic.final_moment"
        ),
        "product": _product(row.get("product"), f"{field_name}.product"),
    }


def _rfbs_return_row(raw: Any, field_name: str) -> Dict[str, Any]:
    row = _mapping(raw, field_name)
    status, status_label = _state(row.get("state"), f"{field_name}.state")
    return {
        "source_kind": "rfbs",
        "external_return_id": _identifier(
            row.get("return_id"), f"{field_name}.return_id", maximum=100
        ),
        "posting_number": _identifier(
            row.get("posting_number"), f"{field_name}.posting_number"
        ),
        "external_order_id": _identifier(
            row.get("order_number"),
            f"{field_name}.order_number",
            maximum=200,
            optional=True,
        ),
        "fulfillment_kind": "rfbs",
        "status": status,
        "status_label": status_label,
        "reason": None,
        "created_at": _timestamp(
            row.get("created_at"), f"{field_name}.created_at", optional=False
        ),
        "status_changed_at": None,
        "completed_at": None,
        "product": _product(
            row.get("product"), f"{field_name}.product", quantity_default=1
        ),
    }


def _cursor_page(
    *,
    rows: Sequence[Any],
    requested_last_id: int,
    explicit_last_id: Any,
    has_next: bool,
    identity_key: str,
) -> int:
    cursor = requested_last_id
    if explicit_last_id not in (None, ""):
        cursor = _integer(
            explicit_last_id,
            "response.last_id",
            minimum=0,
            maximum=9_223_372_036_854_775_807,
        )
    elif rows:
        cursor = max(int(item[identity_key]) for item in rows)
    if has_next and cursor <= requested_last_id:
        raise OzonFulfillmentContractError(
            "response cursor did not advance while has_next is true"
        )
    return cursor


def normalize_return_response(
    response: Any,
    *,
    requested_limit: int,
    requested_last_id: int,
) -> Dict[str, Any]:
    limit = _integer(
        requested_limit, "requested_limit", minimum=1, maximum=RETURN_PAGE_LIMIT
    )
    last_id = _integer(
        requested_last_id,
        "requested_last_id",
        minimum=0,
        maximum=9_223_372_036_854_775_807,
    )
    envelope = _mapping(response, "returns response")
    raw_rows = _bounded_rows(envelope.get("returns"), "returns", limit=limit)
    has_next = envelope.get("has_next")
    if not isinstance(has_next, bool):
        raise OzonFulfillmentContractError("returns.has_next must be boolean")
    rows = []
    seen = set()
    for index, raw_row in enumerate(raw_rows):
        item = _v1_return_row(raw_row, f"returns[{index}]")
        if item["external_return_id"] in seen:
            raise OzonFulfillmentContractError("returns contains duplicate ids")
        seen.add(item["external_return_id"])
        rows.append(item)
    next_last_id = _cursor_page(
        rows=rows,
        requested_last_id=last_id,
        explicit_last_id=envelope.get("last_id"),
        has_next=has_next,
        identity_key="external_return_id",
    )
    return {"rows": rows, "has_next": has_next, "next_last_id": next_last_id}


def normalize_rfbs_return_response(
    response: Any,
    *,
    requested_limit: int,
    requested_last_id: int,
) -> Dict[str, Any]:
    limit = _integer(
        requested_limit, "requested_limit", minimum=1, maximum=RETURN_PAGE_LIMIT
    )
    last_id = _integer(
        requested_last_id,
        "requested_last_id",
        minimum=0,
        maximum=9_223_372_036_854_775_807,
    )
    envelope = _mapping(response, "rFBS returns response")
    raw_rows = _bounded_rows(
        envelope.get("returns"), "rFBS returns", limit=limit
    )
    rows = []
    seen = set()
    for index, raw_row in enumerate(raw_rows):
        item = _rfbs_return_row(raw_row, f"rFBS returns[{index}]")
        if item["external_return_id"] in seen:
            raise OzonFulfillmentContractError(
                "rFBS returns contains duplicate ids"
            )
        seen.add(item["external_return_id"])
        rows.append(item)
    explicit_last_id = envelope.get("last_id")
    if explicit_last_id is None:
        raise OzonFulfillmentContractError("rFBS returns.last_id is required")
    next_last_id = _integer(
        explicit_last_id,
        "rFBS returns.last_id",
        minimum=0,
        maximum=9_223_372_036_854_775_807,
    )
    has_next = bool(rows) and len(rows) == limit
    if has_next and next_last_id <= last_id:
        raise OzonFulfillmentContractError(
            "rFBS returns cursor did not advance on a full page"
        )
    return {"rows": rows, "has_next": has_next, "next_last_id": next_last_id}


def normalize_conditional_cancellation_response(
    response: Any,
    *,
    requested_limit: int,
    requested_last_id: int,
) -> Dict[str, Any]:
    limit = _integer(
        requested_limit, "requested_limit", minimum=1, maximum=RETURN_PAGE_LIMIT
    )
    last_id = _integer(
        requested_last_id,
        "requested_last_id",
        minimum=0,
        maximum=9_223_372_036_854_775_807,
    )
    envelope = _mapping(response, "conditional cancellation response")
    raw_rows = _bounded_rows(
        envelope.get("result"), "conditional cancellation result", limit=limit
    )
    rows = []
    seen = set()
    for index, raw_row in enumerate(raw_rows):
        field_name = f"conditional cancellation result[{index}]"
        row = _mapping(raw_row, field_name)
        external_id = _identifier(
            row.get("cancellation_id"),
            f"{field_name}.cancellation_id",
            maximum=100,
        )
        if external_id in seen:
            raise OzonFulfillmentContractError(
                "conditional cancellations contain duplicate ids"
            )
        seen.add(external_id)
        state, state_label = _state(row.get("state"), f"{field_name}.state")
        reason = _mapping(
            row.get("cancellation_reason"),
            f"{field_name}.cancellation_reason",
            optional=True,
        )
        rows.append({
            "source_kind": "rfbs_conditional",
            "external_cancellation_id": external_id,
            "posting_number": _identifier(
                row.get("posting_number"), f"{field_name}.posting_number"
            ),
            "status": state,
            "status_label": state_label,
            "initiator": _text(
                row.get("cancellation_initiator"),
                f"{field_name}.cancellation_initiator",
                maximum=80,
                optional=True,
            ),
            "reason_code": _identifier(
                reason.get("id"),
                f"{field_name}.cancellation_reason.id",
                maximum=100,
                optional=True,
            ),
            "reason": _text(
                reason.get("name"),
                f"{field_name}.cancellation_reason.name",
                maximum=500,
                optional=True,
            ),
            "requested_at": _timestamp(
                row.get("cancelled_at"), f"{field_name}.cancelled_at"
            ),
            "resolved_at": _timestamp(
                row.get("approve_date"), f"{field_name}.approve_date"
            ),
        })
    if "last_id" not in envelope:
        raise OzonFulfillmentContractError(
            "conditional cancellation last_id is required"
        )
    next_last_id = _integer(
        envelope.get("last_id"),
        "conditional cancellation last_id",
        minimum=0,
        maximum=9_223_372_036_854_775_807,
    )
    has_next = bool(rows) and len(rows) == limit
    if has_next and next_last_id <= last_id:
        raise OzonFulfillmentContractError(
            "conditional cancellation cursor did not advance on a full page"
        )
    return {"rows": rows, "has_next": has_next, "next_last_id": next_last_id}
