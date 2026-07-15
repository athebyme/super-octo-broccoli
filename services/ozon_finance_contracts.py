"""Strict, ORM-free contracts for the current Ozon finance read APIs.

The normalized ledger is based only on ``accruals[].total_amount`` from
``/v1/finance/accrual/by-day``.  Nested fee rows are explanatory components;
commission snapshots overlap with the top-level amount and are deliberately
not exposed as independently summable facts.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


class OzonFinanceContractError(ValueError):
    """A local request or provider response violates the finance contract."""


MAX_ACCRUALS_PER_PAGE = 5_000
MAX_PRODUCTS_PER_ACCRUAL = 1_000
MAX_COMPONENTS_PER_ACCRUAL = 5_000
MAX_ACCRUAL_TYPES = 5_000
MAX_POSTING_NUMBERS = 100
MAX_POSTING_ACCRUALS = 10_000
ACCRUAL_CATEGORIES = {"UNSPECIFIED", "POSTING", "ITEM", "NON_ITEM"}
COMPONENT_KINDS = {"item_fee", "non_item_fee", "delivery_service"}


def _mapping(value: Any, field_name: str, *, optional: bool = False) -> Mapping[str, Any]:
    if value is None and optional:
        return {}
    if not isinstance(value, Mapping):
        raise OzonFinanceContractError(f"{field_name} must be an object")
    return value


def _rows(value: Any, field_name: str, *, maximum: int) -> Sequence[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise OzonFinanceContractError(
            f"{field_name} must be a list with at most {maximum} rows"
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
        raise OzonFinanceContractError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        if optional:
            return None
        raise OzonFinanceContractError(f"{field_name} must be non-empty")
    if len(normalized) > maximum:
        raise OzonFinanceContractError(
            f"{field_name} exceeds {maximum} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise OzonFinanceContractError(f"{field_name} contains control characters")
    return normalized


def _positive_integer(
    value: Any,
    field_name: str,
    *,
    maximum: int = 9_223_372_036_854_775_807,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise OzonFinanceContractError(
            f"{field_name} must be a positive integer not greater than {maximum}"
        )
    return value


def _identifier(
    value: Any,
    field_name: str,
    *,
    maximum: int = 100,
    optional: bool = False,
) -> Optional[str]:
    if value in (None, "") and optional:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if value <= 0:
            raise OzonFinanceContractError(f"{field_name} must be positive")
        value = str(value)
    return _text(value, field_name, maximum=maximum, optional=optional)


def _cursor(value: Any, field_name: str, *, optional: bool = False) -> Optional[str]:
    normalized = _identifier(
        value,
        field_name,
        maximum=100,
        optional=optional,
    )
    if normalized is not None and len(normalized.encode("utf-8")) > 200:
        raise OzonFinanceContractError(f"{field_name} is too large")
    return normalized


def _iso_date(value: Any, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    raw = _text(value, field_name, maximum=10)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        raise OzonFinanceContractError(f"{field_name} must be YYYY-MM-DD") from None
    if parsed.isoformat() != raw:
        raise OzonFinanceContractError(f"{field_name} must be canonical YYYY-MM-DD")
    return parsed


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise OzonFinanceContractError(
            f"{field_name} must be a decimal string or JSON number"
        )
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OzonFinanceContractError(f"{field_name} is invalid") from None
    if not normalized.is_finite():
        raise OzonFinanceContractError(f"{field_name} must be finite")
    if abs(normalized) > Decimal("9999999999999999.9999"):
        raise OzonFinanceContractError(f"{field_name} exceeds storage bounds")
    return normalized.quantize(Decimal("0.0001"))


def _currency(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name, maximum=3).upper()
    if len(normalized) != 3 or not normalized.isalpha() or not normalized.isascii():
        raise OzonFinanceContractError(
            f"{field_name} must be an ASCII three-letter currency code"
        )
    return normalized


def _money(value: Any, field_name: str) -> Tuple[Decimal, str]:
    money = _mapping(value, field_name)
    unknown = set(money) - {"amount", "currency"}
    if unknown:
        raise OzonFinanceContractError(
            f"{field_name} contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    return (
        _decimal(money.get("amount"), f"{field_name}.amount"),
        _currency(money.get("currency"), f"{field_name}.currency"),
    )


def build_accrual_types_request() -> Dict[str, Any]:
    """Build the parameterless current accrual dictionary request."""
    return {}


def build_accrual_by_day_request(
    *,
    accrual_date: Any,
    last_id: Any = None,
) -> Dict[str, Any]:
    """Build one bounded-day cursor request."""
    payload: Dict[str, Any] = {"date": _iso_date(accrual_date, "date").isoformat()}
    normalized_cursor = _cursor(last_id, "last_id", optional=True)
    if normalized_cursor is not None:
        payload["last_id"] = normalized_cursor
    return payload


def build_accrual_postings_request(*, posting_numbers: Any) -> Dict[str, Any]:
    """Build an exact-set request for selected posting numbers."""
    rows = _rows(
        posting_numbers,
        "posting_numbers",
        maximum=MAX_POSTING_NUMBERS,
    )
    if not rows:
        raise OzonFinanceContractError("posting_numbers must not be empty")
    normalized = [
        _identifier(value, f"posting_numbers[{index}]", maximum=200)
        for index, value in enumerate(rows)
    ]
    if len(set(normalized)) != len(normalized):
        raise OzonFinanceContractError("posting_numbers must be unique")
    return {"posting_numbers": normalized}


def normalize_accrual_types_response(response: Any) -> Dict[str, Any]:
    payload = _mapping(response, "response")
    raw_types = _rows(
        payload.get("accrual_types"),
        "accrual_types",
        maximum=MAX_ACCRUAL_TYPES,
    )
    result = []
    seen = set()
    for index, raw in enumerate(raw_types):
        prefix = f"accrual_types[{index}]"
        item = _mapping(raw, prefix)
        type_id = _positive_integer(item.get("id"), f"{prefix}.id")
        if type_id in seen:
            raise OzonFinanceContractError("accrual_types contains duplicate ids")
        seen.add(type_id)
        result.append({
            "type_id": type_id,
            "name": _text(item.get("name"), f"{prefix}.name", maximum=300),
            "description": _text(
                item.get("description"),
                f"{prefix}.description",
                maximum=2_000,
                optional=True,
            ),
        })
    return {"types": result}


def _fee(raw: Any, field_name: str, *, kind: str, sku: Optional[str]) -> Dict[str, Any]:
    if kind not in COMPONENT_KINDS:
        raise AssertionError("unknown finance component kind")
    fee = _mapping(raw, field_name)
    type_id = _positive_integer(fee.get("type_id"), f"{field_name}.type_id")
    amount, currency = _money(fee.get("accrued"), f"{field_name}.accrued")
    return {
        "component_kind": kind,
        "type_id": type_id,
        "sku": sku,
        "amount": amount,
        "currency": currency,
    }


def _product_skus(posting: Mapping[str, Any], field_name: str) -> Tuple[list, list]:
    products = _rows(
        posting.get("products", []),
        f"{field_name}.products",
        maximum=MAX_PRODUCTS_PER_ACCRUAL,
    )
    skus = []
    components = []
    seen_skus = set()
    for index, raw in enumerate(products):
        prefix = f"{field_name}.products[{index}]"
        product = _mapping(raw, prefix)
        sku = _identifier(product.get("sku"), f"{prefix}.sku", maximum=100)
        if sku in seen_skus:
            raise OzonFinanceContractError(f"{field_name}.products contains duplicate sku")
        seen_skus.add(sku)
        skus.append(sku)
        delivery = _mapping(product.get("delivery"), f"{prefix}.delivery", optional=True)
        services = _rows(
            delivery.get("services", []),
            f"{prefix}.delivery.services",
            maximum=MAX_COMPONENTS_PER_ACCRUAL,
        )
        for service_index, service in enumerate(services):
            components.append(_fee(
                service,
                f"{prefix}.delivery.services[{service_index}]",
                kind="delivery_service",
                sku=sku,
            ))
        # Commission contains overlapping sale/price/commission measures.  It is
        # accepted only as an object and intentionally not normalized as ledger.
        _mapping(product.get("commission"), f"{prefix}.commission", optional=True)
    return skus, components


def _item_fees(raw: Any, field_name: str) -> Tuple[list, list]:
    wrapper = _mapping(raw, field_name, optional=True)
    groups = _rows(
        wrapper.get("fees", []),
        f"{field_name}.fees",
        maximum=MAX_PRODUCTS_PER_ACCRUAL,
    )
    skus = []
    components = []
    seen_skus = set()
    for index, raw_group in enumerate(groups):
        prefix = f"{field_name}.fees[{index}]"
        group = _mapping(raw_group, prefix)
        sku = _identifier(group.get("sku"), f"{prefix}.sku", maximum=100)
        if sku in seen_skus:
            raise OzonFinanceContractError(f"{field_name}.fees contains duplicate sku")
        seen_skus.add(sku)
        skus.append(sku)
        fees = _rows(
            group.get("fees"),
            f"{prefix}.fees",
            maximum=MAX_COMPONENTS_PER_ACCRUAL,
        )
        for fee_index, fee in enumerate(fees):
            components.append(_fee(
                fee,
                f"{prefix}.fees[{fee_index}]",
                kind="item_fee",
                sku=sku,
            ))
    return skus, components


def _accrual(raw: Any, field_name: str, requested_date: date) -> Dict[str, Any]:
    row = _mapping(raw, field_name)
    if row.get("accrual_id") in (None, ""):
        if row.get("type_id") not in (None, ""):
            raise OzonFinanceContractError(
                f"{field_name}.type_id is the retired top-level field; accrual_id is required"
            )
        raise OzonFinanceContractError(f"{field_name}.accrual_id is required")
    accrual_id = _identifier(
        row.get("accrual_id"),
        f"{field_name}.accrual_id",
        maximum=100,
    )
    fact_date = _iso_date(row.get("date"), f"{field_name}.date")
    if fact_date != requested_date:
        raise OzonFinanceContractError(
            f"{field_name}.date does not match the requested day"
        )
    category = _text(
        row.get("accrued_category"),
        f"{field_name}.accrued_category",
        maximum=25,
    ).upper()
    if category not in ACCRUAL_CATEGORIES:
        raise OzonFinanceContractError(
            f"{field_name}.accrued_category is unsupported"
        )
    amount, currency = _money(row.get("total_amount"), f"{field_name}.total_amount")

    raw_container_fees = row.get("container_fees")
    if raw_container_fees not in (None, [], {}):
        raise OzonFinanceContractError(
            f"{field_name}.container_fees is non-empty and has no approved normalization"
        )

    posting = _mapping(row.get("posting"), f"{field_name}.posting", optional=True)
    posting_skus, delivery_components = _product_skus(
        posting,
        f"{field_name}.posting",
    )
    item_skus, fee_components = _item_fees(
        row.get("item_fees"),
        f"{field_name}.item_fees",
    )
    components = delivery_components + fee_components
    if row.get("non_item_fee") not in (None, {}):
        components.append(_fee(
            row.get("non_item_fee"),
            f"{field_name}.non_item_fee",
            kind="non_item_fee",
            sku=None,
        ))
    if len(components) > MAX_COMPONENTS_PER_ACCRUAL:
        raise OzonFinanceContractError(
            f"{field_name} contains too many fee components"
        )
    component_keys = [
        (item["component_kind"], item["sku"], item["type_id"])
        for item in components
    ]
    if len(component_keys) != len(set(component_keys)):
        raise OzonFinanceContractError(
            f"{field_name} contains duplicate fee components"
        )

    skus = []
    seen_skus = set()
    for sku in posting_skus + item_skus:
        if sku not in seen_skus:
            seen_skus.add(sku)
            skus.append(sku)
    return {
        "accrual_id": accrual_id,
        "date": fact_date,
        "unit_number": _identifier(
            row.get("unit_number"),
            f"{field_name}.unit_number",
            maximum=200,
            optional=True,
        ),
        "category": category,
        "amount": amount,
        "currency": currency,
        "skus": skus,
        "components": components,
    }


def normalize_accrual_by_day_response(
    response: Any,
    *,
    requested_date: Any,
    requested_last_id: Any = None,
) -> Dict[str, Any]:
    """Normalize one page and reject cursor or identity ambiguity."""
    payload = _mapping(response, "response")
    day = _iso_date(requested_date, "requested_date")
    current_cursor = _cursor(
        requested_last_id,
        "requested_last_id",
        optional=True,
    )
    raw_rows = _rows(
        payload.get("accruals"),
        "accruals",
        maximum=MAX_ACCRUALS_PER_PAGE,
    )
    rows = [
        _accrual(raw, f"accruals[{index}]", day)
        for index, raw in enumerate(raw_rows)
    ]
    identities = [row["accrual_id"] for row in rows]
    if len(identities) != len(set(identities)):
        raise OzonFinanceContractError("accruals contains duplicate accrual_id")

    next_cursor = _cursor(payload.get("last_id"), "last_id", optional=True)
    if next_cursor is not None and next_cursor == current_cursor:
        raise OzonFinanceContractError("finance cursor did not advance")
    if next_cursor is not None and not rows:
        raise OzonFinanceContractError("empty finance page cannot advance cursor")
    return {
        "rows": rows,
        "has_next": next_cursor is not None,
        "next_last_id": next_cursor,
    }


def normalize_accrual_postings_response(
    response: Any,
    *,
    requested_posting_numbers: Any,
) -> Dict[str, Any]:
    """Normalize an exact-set posting accrual response without persistence."""
    requested = build_accrual_postings_request(
        posting_numbers=requested_posting_numbers,
    )["posting_numbers"]
    requested_set = set(requested)
    payload = _mapping(response, "response")
    groups = _rows(
        payload.get("posting_accruals"),
        "posting_accruals",
        maximum=MAX_POSTING_NUMBERS,
    )
    normalized = []
    seen_postings = set()
    total_rows = 0
    for group_index, raw_group in enumerate(groups):
        prefix = f"posting_accruals[{group_index}]"
        group = _mapping(raw_group, prefix)
        posting_number = _identifier(
            group.get("posting_number"),
            f"{prefix}.posting_number",
            maximum=200,
        )
        if posting_number not in requested_set:
            raise OzonFinanceContractError(
                f"{prefix}.posting_number was not requested"
            )
        if posting_number in seen_postings:
            raise OzonFinanceContractError("posting_accruals contains duplicates")
        seen_postings.add(posting_number)
        raw_accruals = _rows(
            group.get("accruals"),
            f"{prefix}.accruals",
            maximum=MAX_POSTING_ACCRUALS,
        )
        accruals = []
        seen_rows = set()
        for row_index, raw_row in enumerate(raw_accruals):
            row_prefix = f"{prefix}.accruals[{row_index}]"
            row = _mapping(raw_row, row_prefix)
            amount, currency = _money(row.get("accrued"), f"{row_prefix}.accrued")
            seller_price, seller_currency = _money(
                row.get("seller_price"),
                f"{row_prefix}.seller_price",
            )
            sku = _identifier(row.get("sku"), f"{row_prefix}.sku", maximum=100)
            type_id = _positive_integer(row.get("type_id"), f"{row_prefix}.type_id")
            quantity = _positive_integer(
                row.get("quantity"),
                f"{row_prefix}.quantity",
                maximum=1_000_000,
            )
            accrual_date = _iso_date(
                row.get("accrual_date"),
                f"{row_prefix}.accrual_date",
            )
            identity = (accrual_date, sku, type_id)
            if identity in seen_rows:
                raise OzonFinanceContractError(
                    f"{prefix}.accruals contains duplicate rows"
                )
            seen_rows.add(identity)
            accruals.append({
                "date": accrual_date,
                "sku": sku,
                "type_id": type_id,
                "quantity": quantity,
                "amount": amount,
                "currency": currency,
                "seller_price": seller_price,
                "seller_price_currency": seller_currency,
            })
        total_rows += len(accruals)
        if total_rows > MAX_POSTING_ACCRUALS:
            raise OzonFinanceContractError("posting accrual response is too large")
        normalized.append({
            "posting_number": posting_number,
            "accruals": accruals,
        })
    return {"posting_accruals": normalized}
