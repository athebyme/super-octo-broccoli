"""Synthetic contract tests for read-only Ozon fulfillment feeds."""

from datetime import date
from decimal import Decimal
import unittest

from services.ozon_fulfillment_contracts import (
    FBO_POSTING_PAGE_LIMIT,
    FBS_POSTING_PAGE_LIMIT,
    OzonFulfillmentContractError,
    build_conditional_cancellation_request,
    build_posting_request,
    build_return_request,
    build_rfbs_return_request,
    normalize_conditional_cancellation_response,
    normalize_posting_response,
    normalize_return_response,
    normalize_rfbs_return_response,
)


class OzonFulfillmentRequestContractTest(unittest.TestCase):
    def test_posting_request_is_bounded_and_explicitly_excludes_enrichment(self):
        payload = build_posting_request(
            fulfillment_kind="fbs",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 15),
            cursor="opaque-page-2",
            limit=FBS_POSTING_PAGE_LIMIT,
        )
        self.assertEqual(payload["dir"], "ASC")
        self.assertEqual(payload["cursor"], "opaque-page-2")
        self.assertNotIn("offset", payload)
        self.assertEqual(payload["limit"], FBS_POSTING_PAGE_LIMIT)
        self.assertEqual(
            payload["with"],
            {
                "analytics_data": False,
                "financial_data": False,
                "barcodes": False,
            },
        )
        self.assertTrue(payload["filter"]["since"].endswith("Z"))
        self.assertTrue(payload["filter"]["to"].endswith("Z"))

    def test_posting_page_limits_are_endpoint_specific(self):
        common = {
            "period_start": date(2026, 7, 1),
            "period_end": date(2026, 7, 15),
        }
        fbs = build_posting_request(fulfillment_kind="fbs", **common)
        fbo = build_posting_request(fulfillment_kind="fbo", **common)
        self.assertEqual(fbs["limit"], FBS_POSTING_PAGE_LIMIT)
        self.assertEqual(fbo["limit"], FBO_POSTING_PAGE_LIMIT)

        with self.assertRaises(OzonFulfillmentContractError):
            build_posting_request(
                fulfillment_kind="fbs",
                limit=FBS_POSTING_PAGE_LIMIT + 1,
                **common,
            )

    def test_return_requests_use_current_cursor_contracts(self):
        common = {
            "period_start": date(2026, 7, 1),
            "period_end": date(2026, 7, 15),
            "last_id": 12,
            "limit": 200,
        }
        fbo_fbs = build_return_request(**common)
        rfbs = build_rfbs_return_request(**common)
        cancellation = build_conditional_cancellation_request(
            last_id=12,
            limit=200,
        )
        self.assertIn("visual_status_change_moment", fbo_fbs["filter"])
        self.assertIn("created_at", rfbs["filter"])
        self.assertEqual(cancellation["filters"], {"state": "ALL"})
        self.assertEqual(cancellation["with"], {"counter": False})

    def test_request_contract_rejects_oversized_period_and_loose_numbers(self):
        with self.assertRaises(OzonFulfillmentContractError):
            build_posting_request(
                fulfillment_kind="fbs",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 2, 1),
            )
        with self.assertRaises(OzonFulfillmentContractError):
            build_return_request(
                period_start=date(2026, 7, 1),
                period_end=date(2026, 7, 2),
                last_id=True,
            )


class OzonPostingResponseContractTest(unittest.TestCase):
    @staticmethod
    def _posting(number="100-1-1"):
        return {
            "posting_number": number,
            "order_id": 77,
            "order_number": "100-1",
            "status": "delivered",
            "substatus": "posting_received",
            "in_process_at": "2026-07-10T10:00:00Z",
            "shipment_date": "2026-07-11T10:00:00+00:00",
            "products": [
                {
                    "sku": 123456,
                    "offer_id": "offer-1",
                    "name": "Safe synthetic product",
                    "quantity": 2,
                    "price": "199.90",
                    "currency_code": "RUB",
                }
            ],
            # These fields deliberately must not survive normalization.
            "customer": {"name": "Synthetic Buyer", "phone": "+70000000000"},
            "analytics_data": {"region": "private"},
            "financial_data": {"commission": 99},
        }

    def test_fbs_cursor_envelope_normalizes_only_safe_fields(self):
        page = normalize_posting_response(
            {
                "postings": [self._posting()],
                "has_next": False,
                "cursor": "",
            },
            fulfillment_kind="fbs",
            requested_limit=FBS_POSTING_PAGE_LIMIT,
            requested_cursor="",
        )
        self.assertFalse(page["has_next"])
        self.assertIsNone(page["next_cursor"])
        row = page["rows"][0]
        self.assertEqual(row["posting_number"], "100-1-1")
        self.assertEqual(row["fulfillment_kind"], "fbs")
        self.assertEqual(row["products"][0]["unit_price"], Decimal("199.9000"))
        self.assertNotIn("customer", row)
        self.assertNotIn("financial_data", row)
        self.assertNotIn("analytics_data", row)

    def test_fbo_cursor_envelope_requires_provider_has_next(self):
        page = normalize_posting_response(
            {
                "postings": [self._posting()],
                "has_next": True,
                "cursor": "opaque-page-2",
            },
            fulfillment_kind="fbo",
            requested_limit=1,
            requested_cursor="opaque-page-1",
        )
        self.assertTrue(page["has_next"])
        self.assertEqual(page["next_cursor"], "opaque-page-2")

    def test_duplicate_postings_and_products_fail_whole_page(self):
        duplicate_product = self._posting()
        duplicate_product["products"].append(dict(duplicate_product["products"][0]))
        with self.assertRaises(OzonFulfillmentContractError):
            normalize_posting_response(
                {
                    "postings": [duplicate_product],
                    "has_next": False,
                    "cursor": "",
                },
                fulfillment_kind="fbs",
                requested_limit=10,
                requested_cursor="",
            )
        with self.assertRaises(OzonFulfillmentContractError):
            normalize_posting_response(
                {
                    "postings": [self._posting(), self._posting()],
                    "has_next": False,
                    "cursor": "",
                },
                fulfillment_kind="fbs",
                requested_limit=10,
                requested_cursor="",
            )

    def test_non_advancing_empty_page_is_rejected(self):
        with self.assertRaises(OzonFulfillmentContractError):
            normalize_posting_response(
                {"postings": [], "has_next": True, "cursor": "next"},
                fulfillment_kind="fbs",
                requested_limit=10,
                requested_cursor="",
            )
        with self.assertRaises(OzonFulfillmentContractError):
            normalize_posting_response(
                {
                    "postings": [self._posting()],
                    "has_next": True,
                    "cursor": "same-cursor",
                },
                fulfillment_kind="fbs",
                requested_limit=10,
                requested_cursor="same-cursor",
            )


class OzonReturnResponseContractTest(unittest.TestCase):
    def test_fbo_fbs_return_normalization_uses_visual_status(self):
        response = {
            "returns": [
                {
                    "id": 101,
                    "order_id": 777,
                    "posting_number": "100-1-1",
                    "schema": "FBO",
                    "return_reason_name": "Не подошёл товар",
                    "visual": {
                        "status": {
                            "sys_name": "MovingToSeller",
                            "display_name": "Едет продавцу",
                        },
                        "change_moment": "2026-07-12T09:00:00Z",
                    },
                    "logistic": {
                        "return_date": "2026-07-11T09:00:00Z",
                        "final_moment": "0001-01-01T00:00:00Z",
                        "barcode": "must-not-be-persisted",
                    },
                    "place": {"address": "must-not-be-persisted"},
                    "product": {
                        "sku": 123456,
                        "offer_id": "offer-1",
                        "name": "Synthetic product",
                        "quantity": 1,
                        "price": {"price": 300.5, "currency_code": "RUB"},
                    },
                }
            ],
            "has_next": False,
        }
        page = normalize_return_response(
            response,
            requested_limit=500,
            requested_last_id=0,
        )
        row = page["rows"][0]
        self.assertEqual(row["status"], "MovingToSeller")
        self.assertEqual(row["fulfillment_kind"], "FBO")
        self.assertEqual(row["product"]["unit_price"], Decimal("300.5000"))
        self.assertEqual(page["next_last_id"], 101)
        self.assertNotIn("place", row)
        self.assertNotIn("barcode", row)

    def test_rfbs_return_omits_buyer_and_freeform_comment(self):
        page = normalize_rfbs_return_response(
            {
                "returns": [
                    {
                        "return_id": 51,
                        "return_number": "R-51",
                        "posting_number": "200-2-1",
                        "order_number": "200-2",
                        "created_at": "2026-07-12T10:00:00Z",
                        "client_name": "Synthetic Buyer",
                        "comment": "private freeform text",
                        "state": {
                            "group_state": "IN_PROGRESS",
                            "state_name": "На проверке",
                        },
                        "product": {
                            "sku": 654321,
                            "offer_id": "offer-2",
                            "name": "Synthetic product",
                            "price": "500",
                            "currency_code": "RUB",
                        },
                    }
                ],
                "last_id": 51,
            },
            requested_limit=500,
            requested_last_id=0,
        )
        row = page["rows"][0]
        self.assertEqual(row["fulfillment_kind"], "rfbs")
        self.assertNotIn("client_name", row)
        self.assertNotIn("comment", row)
        self.assertFalse(page["has_next"])

    def test_conditional_cancellation_keeps_enum_reason_not_freeform_message(self):
        page = normalize_conditional_cancellation_response(
            {
                "result": [
                    {
                        "cancellation_id": 9,
                        "posting_number": "300-3-1",
                        "cancellation_initiator": "CLIENT",
                        "cancellation_reason": {"id": 12, "name": "Передумал"},
                        "cancellation_reason_message": "private freeform text",
                        "cancelled_at": "2026-07-13T10:00:00Z",
                        "state": {
                            "state": "ON_APPROVAL",
                            "name": "На согласовании",
                        },
                    }
                ],
                "last_id": 9,
                "counter": 1,
            },
            requested_limit=500,
            requested_last_id=0,
        )
        row = page["rows"][0]
        self.assertEqual(row["reason_code"], "12")
        self.assertEqual(row["reason"], "Передумал")
        self.assertNotIn("cancellation_reason_message", row)

    def test_empty_rfbs_page_without_cursor_is_terminal(self):
        page = normalize_rfbs_return_response(
            {"returns": []},
            requested_limit=500,
            requested_last_id=12,
        )
        self.assertEqual(
            page,
            {"rows": [], "has_next": False, "next_last_id": 12},
        )

    def test_cursor_pages_reject_missing_or_non_advancing_cursors(self):
        with self.assertRaises(OzonFulfillmentContractError):
            normalize_rfbs_return_response(
                {
                    "returns": [
                        {
                            "return_id": 51,
                            "posting_number": "200-2-1",
                            "order_number": "200-2",
                            "created_at": "2026-07-12T10:00:00Z",
                            "state": {"group_state": "IN_PROGRESS"},
                            "product": {
                                "sku": 654321,
                                "offer_id": "offer-2",
                                "name": "Synthetic product",
                                "price": "500",
                                "currency_code": "RUB",
                            },
                        }
                    ]
                },
                requested_limit=500,
                requested_last_id=0,
            )
        with self.assertRaises(OzonFulfillmentContractError):
            normalize_conditional_cancellation_response(
                {
                    "result": [
                        {
                            "cancellation_id": 1,
                            "posting_number": "1-1-1",
                            "state": {"state": "ON_APPROVAL"},
                        }
                    ],
                    "last_id": 0,
                },
                requested_limit=1,
                requested_last_id=0,
            )


if __name__ == "__main__":
    unittest.main()
