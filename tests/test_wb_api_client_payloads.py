# -*- coding: utf-8 -*-
"""
Regression tests for WB API request body shaping.
"""

import unittest

from services.wb_api_client import (
    WBAPIException,
    WBContentProtocolException,
    WBVendorCardLookupUncertain,
    WBVendorCardNotFound,
    WildberriesAPIClient,
)


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload
        self.content = b"{}"

    def json(self):
        return self._payload


class _InvalidJSONResponse:
    def json(self):
        raise ValueError("invalid JSON")


class TestWBAPIClientPayloads(unittest.TestCase):
    @staticmethod
    def _lookup_client(payload):
        client = WildberriesAPIClient("token")
        calls = []

        def fake_make_request(method, api_type, endpoint, **kwargs):
            calls.append((method, api_type, endpoint, kwargs))
            if isinstance(payload, Exception):
                raise payload
            if isinstance(payload, _InvalidJSONResponse):
                return payload
            return _DummyResponse(payload)

        client._make_request = fake_make_request
        return client, calls

    def test_cards_error_list_uses_post_body_from_current_api(self):
        client = WildberriesAPIClient("token")
        calls = []

        def fake_make_request(method, api_type, endpoint, **kwargs):
            calls.append((method, api_type, endpoint, kwargs))
            return _DummyResponse({
                "data": {"items": [], "cursor": {"next": False}},
                "error": False,
                "errorText": "",
            })

        client._make_request = fake_make_request

        result = client.get_cards_errors_list(log_to_db=False)

        self.assertFalse(result["error"])
        method, api_type, endpoint, kwargs = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(api_type, "content")
        self.assertEqual(endpoint, "/content/v2/cards/error/list")
        self.assertEqual(kwargs["json"]["cursor"]["limit"], 100)
        self.assertIn("order", kwargs["json"])

    def test_upload_prices_v2_sends_only_official_fields(self):
        client = WildberriesAPIClient("token")
        calls = []

        def fake_make_request(method, api_type, endpoint, **kwargs):
            calls.append((method, api_type, endpoint, kwargs))
            return _DummyResponse({"data": {"id": 1}, "error": False, "errorText": ""})

        client._make_request = fake_make_request

        client.upload_prices_v2([
            {"nmID": 123, "price": 999.0, "discount": 15.0, "_final": 849},
        ])

        body = calls[0][3]["json"]
        self.assertEqual(body, {
            "data": [{"nmID": 123, "price": 999, "discount": 15}]
        })

    def test_create_product_card_rejects_too_many_variants(self):
        client = WildberriesAPIClient("token")

        with self.assertRaises(WBAPIException):
            client.create_product_card(
                subject_id=1,
                variants=[{"vendorCode": f"SKU-{idx}"} for idx in range(31)],
            )

    def test_vendor_code_lookup_returns_only_exact_row_from_complete_page(self):
        client, calls = self._lookup_client({
            "cards": [
                {"nmID": 101, "vendorCode": "SKU-ABC-NEAR"},
                {"nmID": 102, "vendorCode": "sku-abc"},
                {"nmID": 202, "vendorCode": "SKU-ABC"},
            ],
            "cursor": {"total": 3},
        })

        result = client.get_card_by_vendor_code("SKU-ABC")

        self.assertEqual(result, {"nmID": 202, "vendorCode": "SKU-ABC"})
        self.assertEqual(len(calls), 1)
        method, api_type, endpoint, kwargs = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(api_type, "content")
        self.assertEqual(endpoint, "/content/v2/get/cards/list")
        self.assertEqual(kwargs["json"], {
            "settings": {
                "cursor": {"limit": 20},
                "filter": {"textSearch": "SKU-ABC", "withPhoto": -1},
            },
        })

    def test_vendor_code_lookup_reports_not_found_for_complete_near_or_empty_page(self):
        payloads = (
            {
                "cards": [{"nmID": 101, "vendorCode": "SKU-ABC-NEAR"}],
                "cursor": {"total": 1},
            },
            {"cards": [], "cursor": {"total": 0}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                client, calls = self._lookup_client(payload)
                with self.assertRaises(WBVendorCardNotFound):
                    client.get_card_by_vendor_code("SKU-ABC")
                self.assertEqual(len(calls), 1)

    def test_vendor_code_lookup_fails_closed_for_multiple_exact_rows(self):
        client, calls = self._lookup_client({
            "cards": [
                {"nmID": 101, "vendorCode": "SKU-ABC"},
                {"nmID": 202, "vendorCode": "SKU-ABC"},
            ],
            "cursor": {"total": 2},
        })

        with self.assertRaises(WBVendorCardLookupUncertain):
            client.get_card_by_vendor_code("SKU-ABC")
        self.assertEqual(len(calls), 1)

    def test_vendor_code_lookup_fails_closed_for_invalid_target_or_nm_id(self):
        for vendor_code in ("", "   ", None, True, "X" * 201):
            with self.subTest(vendor_code=vendor_code):
                client, calls = self._lookup_client({})
                with self.assertRaises(WBVendorCardLookupUncertain):
                    client.get_card_by_vendor_code(vendor_code)
                self.assertEqual(calls, [])

        for nm_id in (0, -1, True, "123", None):
            with self.subTest(nm_id=nm_id):
                client, calls = self._lookup_client({
                    "cards": [{"nmID": nm_id, "vendorCode": "SKU-ABC"}],
                    "cursor": {"total": 1},
                })
                with self.assertRaises(WBVendorCardLookupUncertain):
                    client.get_card_by_vendor_code("SKU-ABC")
                self.assertEqual(len(calls), 1)

    def test_vendor_code_lookup_rejects_malformed_mismatched_and_saturated_pages(self):
        malformed = (
            None,
            {"cursor": {"total": 0}},
            {"cards": "not-a-list", "cursor": {"total": 0}},
            {"cards": ["not-a-card"], "cursor": {"total": 1}},
            {"cards": [{"nmID": 1}], "cursor": {"total": 1}},
            {"cards": [{"nmID": 1, "vendorCode": "  "}], "cursor": {"total": 1}},
            {"cards": [], "cursor": []},
            {"cards": [], "cursor": {"total": True}},
            {"cards": [], "cursor": {"total": -1}},
            {"cards": [{"nmID": 1, "vendorCode": "near"}], "cursor": {"total": 0}},
            {
                "cards": [],
                "cursor": {"total": 0},
                "error": True,
                "errorText": "provider error",
            },
            {
                "cards": [
                    {"nmID": idx + 1, "vendorCode": f"SKU-NEAR-{idx}"}
                    for idx in range(20)
                ],
                "cursor": {"total": 20},
            },
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                client, calls = self._lookup_client(payload)
                with self.assertRaises(WBVendorCardLookupUncertain):
                    client.get_card_by_vendor_code("SKU-ABC")
                self.assertEqual(len(calls), 1)

        client, calls = self._lookup_client(_InvalidJSONResponse())
        with self.assertRaises(WBContentProtocolException):
            client.get_card_by_vendor_code("SKU-ABC")
        self.assertEqual(len(calls), 1)

    def test_vendor_code_lookup_propagates_api_failure(self):
        failure = WBAPIException("provider failure")
        client, calls = self._lookup_client(failure)

        with self.assertRaises(WBAPIException) as raised:
            client.get_card_by_vendor_code("SKU-ABC")

        self.assertIs(raised.exception, failure)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
