"""ORM-free contracts for Ozon Seller analytics reads.

The endpoint is read-only but still uses POST.  This module owns the exact
request shape and rejects partial, duplicate or numerically unsafe responses
before the service can persist metric facts.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Dict, List, Mapping, Optional, Tuple
import json


class OzonAnalyticsContractError(ValueError):
    """The local request or provider response violates the analytics contract."""


@dataclass(frozen=True)
class AnalyticsMetricDefinition:
    metric_code: str
    provider_metric: str
    unit: str
    definition_code: str
    cross_marketplace_comparable: bool = False

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "metric_code": self.metric_code,
            "provider_metric": self.provider_metric,
            "unit": self.unit,
            "definition_code": self.definition_code,
            "cross_marketplace_comparable": self.cross_marketplace_comparable,
        }


METRIC_DEFINITIONS: Tuple[AnalyticsMetricDefinition, ...] = (
    AnalyticsMetricDefinition(
        "ordered_revenue_rub",
        "revenue",
        "rub",
        "ozon.analytics.v1/revenue",
    ),
    AnalyticsMetricDefinition(
        "ordered_units",
        "ordered_units",
        "count",
        "ozon.analytics.v1/ordered_units",
    ),
    AnalyticsMetricDefinition(
        "views",
        "hits_view",
        "count",
        "ozon.analytics.v1/hits_view",
    ),
    AnalyticsMetricDefinition(
        "cart_additions",
        "hits_tocart",
        "count",
        "ozon.analytics.v1/hits_tocart",
    ),
    AnalyticsMetricDefinition(
        "cart_conversion_percent",
        "conv_tocart_percent",
        "percent",
        "ozon.analytics.v1/conv_tocart_percent",
    ),
    AnalyticsMetricDefinition(
        "delivered_units",
        "delivered_units",
        "count",
        "ozon.analytics.v1/delivered_units",
    ),
    AnalyticsMetricDefinition(
        "cancelled_units",
        "cancellations",
        "count",
        "ozon.analytics.v1/cancellations",
    ),
    AnalyticsMetricDefinition(
        "returned_units",
        "returns",
        "count",
        "ozon.analytics.v1/returns",
    ),
)

METRIC_BY_CODE = {item.metric_code: item for item in METRIC_DEFINITIONS}
METRIC_BY_PROVIDER = {
    item.provider_metric: item for item in METRIC_DEFINITIONS
}
PROVIDER_METRICS = tuple(item.provider_metric for item in METRIC_DEFINITIONS)
DIMENSIONS = {"product": "sku", "day": "day"}
PAGE_LIMIT = 1000
MAX_OFFSET = 1_000_000
MAX_PERIOD_DAYS = 31


def _strict_integer(
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
        raise OzonAnalyticsContractError(
            f"{field_name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _text(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise OzonAnalyticsContractError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise OzonAnalyticsContractError(f"{field_name} must be non-empty")
    if len(normalized) > maximum:
        raise OzonAnalyticsContractError(
            f"{field_name} exceeds {maximum} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise OzonAnalyticsContractError(
            f"{field_name} contains control characters"
        )
    return normalized


def _date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        raise OzonAnalyticsContractError(f"{field_name} must be a date")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise OzonAnalyticsContractError(
            f"{field_name} must be an ISO date string"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise OzonAnalyticsContractError(
            f"{field_name} must be an ISO date string"
        ) from None
    if parsed.isoformat() != value:
        raise OzonAnalyticsContractError(
            f"{field_name} must be a canonical ISO date string"
        )
    return parsed


def build_analytics_request(
    *,
    period_start: Any,
    period_end: Any,
    dimension_kind: str,
    offset: Any = 0,
    limit: Any = PAGE_LIMIT,
) -> Dict[str, Any]:
    """Build the only allowed `/v1/analytics/data` request shape."""
    start = _date(period_start, "period_start")
    end = _date(period_end, "period_end")
    if start > end:
        raise OzonAnalyticsContractError("period_start must not exceed period_end")
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise OzonAnalyticsContractError(
            f"analytics period must not exceed {MAX_PERIOD_DAYS} days"
        )
    if dimension_kind not in DIMENSIONS:
        raise OzonAnalyticsContractError("dimension_kind is unsupported")
    normalized_offset = _strict_integer(
        offset,
        "offset",
        minimum=0,
        maximum=MAX_OFFSET,
    )
    normalized_limit = _strict_integer(
        limit,
        "limit",
        minimum=1,
        maximum=PAGE_LIMIT,
    )
    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "metrics": list(PROVIDER_METRICS),
        "dimension": [DIMENSIONS[dimension_kind]],
        "filters": [],
        "sort": [],
        "limit": normalized_limit,
        "offset": normalized_offset,
    }


def request_fingerprint(
    *,
    period_start: Any,
    period_end: Any,
) -> str:
    canonical = {
        "contract": "ozon-analytics-v1",
        "period_start": _date(period_start, "period_start").isoformat(),
        "period_end": _date(period_end, "period_end").isoformat(),
        "metrics": list(PROVIDER_METRICS),
        "phases": [DIMENSIONS["product"], DIMENSIONS["day"]],
    }
    payload = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _decimal(value: Any, field_name: str, definition: AnalyticsMetricDefinition) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OzonAnalyticsContractError(f"{field_name} must be a JSON number")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OzonAnalyticsContractError(f"{field_name} is invalid") from None
    if not normalized.is_finite() or normalized < 0:
        raise OzonAnalyticsContractError(
            f"{field_name} must be a finite non-negative number"
        )
    if definition.unit == "percent" and normalized > Decimal("100"):
        raise OzonAnalyticsContractError(
            f"{field_name} percentage exceeds 100"
        )
    if normalized > Decimal("9999999999999999.9999"):
        raise OzonAnalyticsContractError(f"{field_name} exceeds storage bounds")
    return normalized.quantize(Decimal("0.0001"))


def _dimension_id(value: Any, dimension_kind: str, field_name: str) -> str:
    if dimension_kind == "day":
        parsed = _date(value, field_name)
        return parsed.isoformat()
    if isinstance(value, int) and not isinstance(value, bool):
        if value <= 0:
            raise OzonAnalyticsContractError(f"{field_name} must be positive")
        return str(value)
    normalized = _text(value, field_name, maximum=100)
    if not normalized.isdigit() or normalized.startswith("0"):
        raise OzonAnalyticsContractError(
            f"{field_name} must be a canonical positive SKU"
        )
    return normalized


def normalize_analytics_response(
    response: Any,
    *,
    dimension_kind: str,
    requested_limit: int = PAGE_LIMIT,
) -> Dict[str, Any]:
    """Validate one exact analytics page and return bounded typed rows."""
    if dimension_kind not in DIMENSIONS:
        raise OzonAnalyticsContractError("dimension_kind is unsupported")
    limit = _strict_integer(
        requested_limit,
        "requested_limit",
        minimum=1,
        maximum=PAGE_LIMIT,
    )
    if not isinstance(response, Mapping):
        raise OzonAnalyticsContractError("analytics response must be an object")
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise OzonAnalyticsContractError("analytics response has no result object")
    raw_rows = result.get("data")
    if not isinstance(raw_rows, list) or len(raw_rows) > limit:
        raise OzonAnalyticsContractError(
            "analytics result.data must be a bounded list"
        )
    raw_totals = result.get("totals")
    if not isinstance(raw_totals, list) or len(raw_totals) != len(METRIC_DEFINITIONS):
        raise OzonAnalyticsContractError(
            "analytics totals must match requested metrics exactly"
        )
    totals = {
        definition.metric_code: _decimal(
            raw_totals[index],
            f"result.totals[{index}]",
            definition,
        )
        for index, definition in enumerate(METRIC_DEFINITIONS)
    }

    rows = []
    seen = set()
    for row_index, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, Mapping):
            raise OzonAnalyticsContractError(
                f"result.data[{row_index}] must be an object"
            )
        raw_dimensions = raw_row.get("dimensions")
        if not isinstance(raw_dimensions, list) or len(raw_dimensions) != 1:
            raise OzonAnalyticsContractError(
                f"result.data[{row_index}].dimensions must contain exactly one item"
            )
        raw_dimension = raw_dimensions[0]
        if not isinstance(raw_dimension, Mapping):
            raise OzonAnalyticsContractError(
                f"result.data[{row_index}].dimensions[0] must be an object"
            )
        dimension_id = _dimension_id(
            raw_dimension.get("id"),
            dimension_kind,
            f"result.data[{row_index}].dimensions[0].id",
        )
        if dimension_id in seen:
            raise OzonAnalyticsContractError(
                "analytics page contains duplicate dimension identity"
            )
        seen.add(dimension_id)
        raw_name = raw_dimension.get("name")
        dimension_name: Optional[str]
        if raw_name in (None, ""):
            dimension_name = None
        else:
            dimension_name = _text(
                raw_name,
                f"result.data[{row_index}].dimensions[0].name",
                maximum=500,
            )
        raw_metrics = raw_row.get("metrics")
        if not isinstance(raw_metrics, list) or len(raw_metrics) != len(METRIC_DEFINITIONS):
            raise OzonAnalyticsContractError(
                f"result.data[{row_index}].metrics must match requested metrics exactly"
            )
        metrics = {
            definition.metric_code: _decimal(
                raw_metrics[index],
                f"result.data[{row_index}].metrics[{index}]",
                definition,
            )
            for index, definition in enumerate(METRIC_DEFINITIONS)
        }
        rows.append({
            "dimension_id": dimension_id,
            "dimension_name": dimension_name,
            "fact_date": (
                date.fromisoformat(dimension_id)
                if dimension_kind == "day"
                else None
            ),
            "metrics": metrics,
        })

    raw_timestamp = response.get("timestamp")
    timestamp = None
    if raw_timestamp not in (None, ""):
        timestamp = _text(raw_timestamp, "timestamp", maximum=80)
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise OzonAnalyticsContractError(
                "timestamp must be ISO-8601"
            ) from None

    return {
        "rows": rows,
        "totals": totals,
        "timestamp": timestamp,
        "has_more": len(rows) == limit,
    }


def metric_definitions_public() -> List[Dict[str, Any]]:
    return [item.to_public_dict() for item in METRIC_DEFINITIONS]
