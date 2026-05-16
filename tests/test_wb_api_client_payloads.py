# -*- coding: utf-8 -*-
"""
Regression tests for WB API request body shaping.
"""

import unittest

from services.wb_api_client import WBAPIException, WildberriesAPIClient


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload
        self.content = b"{}"

    def json(self):
        return self._payload


class TestWBAPIClientPayloads(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
