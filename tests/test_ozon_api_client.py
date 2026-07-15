# -*- coding: utf-8 -*-
"""Ozon transport and adapter contract tests (no real network calls)."""

import json
import unittest

import requests

from services.marketplace_adapters.ozon import OzonAdapter
from services.marketplace_adapters.registry import MarketplaceRegistry
from services.marketplace_adapters.types import MarketplaceCredentials
from services.ozon_api_client import (
    OZON_ENDPOINTS,
    OzonAmbiguousWriteError,
    OzonAPIError,
    OzonAuthError,
    OzonProtocolError,
    OzonSellerAPIClient,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, invalid_json=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.invalid_json = invalid_json

    def json(self):
        if self.invalid_json:
            raise ValueError("invalid json")
        return self._payload


class FakeSession:
    def __init__(self, actions):
        self.actions = list(actions)
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class OzonSellerAPIClientTest(unittest.TestCase):
    def setUp(self):
        self.api_key = "synthetic-secret-key"
        self.credentials = MarketplaceCredentials(
            external_account_id="123456",
            api_key=self.api_key,
        )

    def _client(self, actions, **kwargs):
        session = FakeSession(actions)
        sleeps = []
        client = OzonSellerAPIClient(
            self.credentials,
            session=session,
            sleep_fn=sleeps.append,
            **kwargs,
        )
        return client, session, sleeps

    def test_credentials_repr_and_public_errors_do_not_expose_api_key(self):
        self.assertNotIn(self.api_key, repr(self.credentials))
        response = FakeResponse(
            401,
            {
                "code": "UNAUTHENTICATED",
                "message": f"bad {self.api_key} for 123456",
            },
        )
        client, _, _ = self._client([response], read_retries=0)
        with self.assertRaises(OzonAuthError) as caught:
            client.get_product_operation_limits()
        self.assertNotIn(self.api_key, str(caught.exception))
        self.assertNotIn("123456", str(caught.exception))

    def test_headers_are_exact_and_credentials_never_enter_payload(self):
        client, session, _ = self._client([FakeResponse(200, {"total": 1})])
        result = client.get_product_operation_limits()
        self.assertEqual(result, {"total": 1})
        self.assertEqual(session.headers["Client-Id"], "123456")
        self.assertEqual(session.headers["Api-Key"], self.api_key)
        self.assertEqual(session.calls[0][2]["json"], {})
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        with self.assertRaises(ValueError):
            client.request("product_operation_limits", {"api_key": "leak"})

    def test_read_post_retries_transport_and_uses_same_typed_endpoint(self):
        client, session, sleeps = self._client([
            requests.Timeout("secret must not escape"),
            FakeResponse(200, {"operation_limits": []}),
        ])
        result = client.get_product_operation_limits()
        self.assertEqual(result["operation_limits"], [])
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sleeps, [1])
        self.assertTrue(session.calls[0][1].endswith("/v4/product/info/limit"))

    def test_analytics_is_allowlisted_read_only_and_retries_without_mutation(self):
        payload = {
            "date_from": "2026-07-09",
            "date_to": "2026-07-15",
            "dimension": ["sku"],
            "metrics": ["ordered_units"],
            "filters": [],
            "limit": 1000,
            "offset": 0,
        }
        client, session, sleeps = self._client([
            requests.Timeout("synthetic timeout"),
            FakeResponse(200, {"result": {"data": [], "totals": [0]}}),
        ])

        result = client.get_analytics_data(payload)

        self.assertEqual(result["result"]["data"], [])
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sleeps, [1])
        self.assertTrue(session.calls[0][1].endswith("/v1/analytics/data"))
        self.assertEqual(session.calls[0][2]["json"], payload)
        self.assertEqual(OZON_ENDPOINTS["analytics_data"].retry_class, "read")

    def test_read_post_honors_bounded_retry_after(self):
        client, session, sleeps = self._client([
            FakeResponse(429, {}, {"Retry-After": "99"}),
            FakeResponse(200, {"ok": True}),
        ])
        self.assertTrue(client.get_product_operation_limits()["ok"])
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sleeps, [30])

    def test_write_transport_failure_is_ambiguous_and_never_retried(self):
        client, session, sleeps = self._client([
            requests.ConnectionError(f"failed {self.api_key}"),
            FakeResponse(200, {"task_id": 1}),
        ])
        with self.assertRaises(OzonAmbiguousWriteError) as caught:
            client.submit_products({"items": []})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertNotIn(self.api_key, str(caught.exception))

    def test_write_5xx_and_malformed_success_are_ambiguous(self):
        for response in (
            FakeResponse(503, {"message": "temporary"}),
            FakeResponse(200, ["not", "an", "object"]),
            FakeResponse(200, invalid_json=True),
        ):
            with self.subTest(status=response.status_code):
                client, session, _ = self._client([response])
                with self.assertRaises(OzonAmbiguousWriteError):
                    client.submit_products({"items": []})
                self.assertEqual(len(session.calls), 1)

    def test_read_malformed_success_is_protocol_error(self):
        client, _, _ = self._client([
            FakeResponse(200, ["wrong"]),
        ])
        with self.assertRaises(OzonProtocolError):
            client.get_product_operation_limits()

    def test_redirect_is_not_followed_with_credential_headers(self):
        client, session, _ = self._client([
            FakeResponse(302, {}, {"Location": "https://attacker.invalid/collect"}),
        ])
        with self.assertRaises(OzonAPIError):
            client.get_product_operation_limits()
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            session.calls[0][1],
            "https://api-seller.ozon.ru/v4/product/info/limit",
        )

    def test_manifest_has_current_families_and_no_deprecated_fallbacks(self):
        paths = json.dumps(
            {name: spec.path for name, spec in OZON_ENDPOINTS.items()},
            sort_keys=True,
        )
        for required in (
            "/v1/roles",
            "/v1/seller/info",
            "/v1/description-category/tree",
            "/v3/product/list",
            "/v3/product/info/list",
            "/v4/product/info/attributes",
            "/v2/product/pictures/info",
            "/v1/product/pictures/import",
            "/v3/product/import",
            "/v1/product/archive",
            "/v1/product/unarchive",
            "/v5/product/info/prices",
            "/v4/product/info/stocks",
            "/v2/product/info/stocks-by-warehouse/fbs",
            "/v1/product/info/stocks-by-warehouse/fbo",
            "/v2/warehouse/list",
            "/v1/analytics/data",
            "/v1/finance/accrual/by-day",
        ):
            self.assertIn(required, paths)
        for deprecated in (
            "/v2/category/tree",
            "/v3/category/attribute",
            "/v1/warehouse/list",
            "/v1/product/info/stocks-by-warehouse/fbs",
            "/v3/finance/transaction/list",
            "/v3/finance/transaction/totals",
        ):
            self.assertNotIn(deprecated, paths)


class OzonAdapterTest(unittest.TestCase):
    def test_connection_check_is_read_only_and_sanitized(self):
        class Client:
            def __init__(self, credentials):
                self.credentials = credentials

            def get_roles(self):
                return {
                    "roles": [{"name": "Product"}, {"name": "Finance"}],
                    "expires_at": "2027-01-02T03:04:05Z",
                }

        adapter = OzonAdapter(client_factory=Client)
        result = adapter.check_connection(MarketplaceCredentials(
            external_account_id="42",
            api_key="test-key",
        ))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "connected")
        self.assertIn("connection_check", result.capabilities)
        self.assertNotIn("catalog_write", result.capabilities)
        self.assertEqual(result.roles, ("Finance", "Product"))
        self.assertEqual(result.expires_at.isoformat(), "2027-01-02T03:04:05")

    def test_registry_is_explicit_and_rejects_duplicates(self):
        adapter = OzonAdapter(client_factory=lambda credentials: None)
        registry = MarketplaceRegistry([adapter])
        self.assertIs(registry.get("OZON"), adapter)
        with self.assertRaises(Exception):
            registry.register(adapter)

    def test_analytics_capability_delegates_to_typed_read_method(self):
        calls = []

        class Client:
            def __init__(self, credentials):
                self.credentials = credentials

            def get_analytics_data(self, payload):
                calls.append(payload)
                return {"result": {"data": [], "totals": [0]}}

        adapter = OzonAdapter(client_factory=Client)
        credentials = MarketplaceCredentials(
            external_account_id="42",
            api_key="synthetic-key",
        )
        payload = {"dimension": ["day"]}

        response = adapter.read_analytics(credentials, payload)

        self.assertIn("analytics_read", adapter.capabilities)
        self.assertEqual(calls, [payload])
        self.assertEqual(response["result"]["data"], [])


if __name__ == "__main__":
    unittest.main()
