from datetime import date
import math
import unittest

from services.ozon_analytics_contracts import (
    METRIC_DEFINITIONS,
    OzonAnalyticsContractError,
    build_analytics_request,
    normalize_analytics_response,
    request_fingerprint,
)


def _response(dimension_id="101", *, values=None, totals=None):
    values = values or [1000, 4, 120, 10, 8.3, 3, 1, 0]
    totals = totals or list(values)
    return {
        "result": {
            "data": [{
                "dimensions": [{"id": dimension_id, "name": "Товар"}],
                "metrics": values,
            }],
            "totals": totals,
        },
        "timestamp": "2026-07-15T10:00:00Z",
    }


class OzonAnalyticsContractsTest(unittest.TestCase):
    def test_request_is_exact_bounded_and_definition_ordered(self):
        payload = build_analytics_request(
            period_start=date(2026, 7, 9),
            period_end=date(2026, 7, 15),
            dimension_kind="product",
            offset=0,
            limit=1000,
        )
        self.assertEqual(
            set(payload),
            {
                "date_from", "date_to", "metrics", "dimension", "filters",
                "sort", "limit", "offset",
            },
        )
        self.assertEqual(payload["dimension"], ["sku"])
        self.assertEqual(
            payload["metrics"],
            [item.provider_metric for item in METRIC_DEFINITIONS],
        )
        self.assertEqual(
            request_fingerprint(
                period_start="2026-07-09",
                period_end="2026-07-15",
            ),
            request_fingerprint(
                period_start=date(2026, 7, 9),
                period_end=date(2026, 7, 15),
            ),
        )

    def test_request_rejects_coercion_and_unbounded_period(self):
        bad_cases = (
            {"period_start": "2026-01-01", "period_end": "2026-07-15", "dimension_kind": "product"},
            {"period_start": "2026-07-09", "period_end": "2026-07-15", "dimension_kind": "sku"},
            {"period_start": "2026-07-09", "period_end": "2026-07-15", "dimension_kind": "product", "offset": True},
            {"period_start": "2026-07-09", "period_end": "2026-07-15", "dimension_kind": "product", "limit": "1000"},
        )
        for kwargs in bad_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(OzonAnalyticsContractError):
                    build_analytics_request(**kwargs)

    def test_response_normalizes_exact_product_and_day_dimensions(self):
        product = normalize_analytics_response(
            _response(),
            dimension_kind="product",
        )
        self.assertEqual(product["rows"][0]["dimension_id"], "101")
        self.assertIsNone(product["rows"][0]["fact_date"])
        self.assertEqual(
            str(product["rows"][0]["metrics"]["ordered_revenue_rub"]),
            "1000.0000",
        )
        day = normalize_analytics_response(
            _response("2026-07-15"),
            dimension_kind="day",
        )
        self.assertEqual(day["rows"][0]["fact_date"], date(2026, 7, 15))

    def test_response_rejects_partial_duplicate_foreign_shape_and_bad_numbers(self):
        malformed = []
        response = _response()
        response["result"]["data"][0]["metrics"] = [1]
        malformed.append(response)
        response = _response()
        response["result"]["totals"] = [1]
        malformed.append(response)
        response = _response()
        response["result"]["data"].append(response["result"]["data"][0].copy())
        malformed.append(response)
        response = _response(values=[1000, 4, math.nan, 10, 8.3, 3, 1, 0])
        malformed.append(response)
        response = _response(values=[1000, 4, 120, 10, 101, 3, 1, 0])
        malformed.append(response)
        response = _response(dimension_id="00101")
        malformed.append(response)
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(OzonAnalyticsContractError):
                    normalize_analytics_response(
                        payload,
                        dimension_kind="product",
                    )

    def test_definitions_explicitly_forbid_cross_marketplace_comparison(self):
        self.assertTrue(METRIC_DEFINITIONS)
        self.assertTrue(all(
            item.definition_code.startswith("ozon.analytics.v1/")
            and item.cross_marketplace_comparable is False
            for item in METRIC_DEFINITIONS
        ))


if __name__ == "__main__":
    unittest.main()
