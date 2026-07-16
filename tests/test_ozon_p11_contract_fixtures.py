# -*- coding: utf-8 -*-
"""Stable P11 provider failure fixtures exercise the production contracts."""

from pathlib import Path
import json

import pytest

from services.marketplace_adapters import MarketplaceCredentials
from services.ozon_api_client import (
    OzonAuthError,
    OzonRateLimitError,
    OzonSellerAPIClient,
)
from services.ozon_commercial_contracts import OzonPriceContract
from services.ozon_product_import import OzonProductImportContract


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "ozon" / "p11_provider_failures.json")
    .read_text(encoding="utf-8")
)


class _Response:
    def __init__(self, document):
        self.status_code = document["status_code"]
        self.headers = document.get("headers", {})
        self._body = document["body"]

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _client(documents, *, retries=0):
    session = _Session([_Response(document) for document in documents])
    sleeps = []
    client = OzonSellerAPIClient(
        MarketplaceCredentials(
            external_account_id="synthetic-client",
            api_key="synthetic-api-key",
        ),
        session=session,
        read_retries=retries,
        sleep_fn=sleeps.append,
    )
    return client, session, sleeps


def test_rate_limit_fixture_honors_retry_after_but_stays_bounded():
    success = {
        "status_code": 200,
        "headers": {},
        "body": {"operation_limits": []},
    }
    client, session, sleeps = _client(
        [FIXTURE["rate_limit"], success],
        retries=1,
    )
    assert client.get_product_operation_limits() == {"operation_limits": []}
    assert len(session.calls) == 2
    assert sleeps == [12.0]

    client, session, _ = _client([FIXTURE["rate_limit"]], retries=0)
    with pytest.raises(OzonRateLimitError) as caught:
        client.get_product_operation_limits()
    assert caught.value.retry_after == 12.0
    assert caught.value.request_id == "fixture-rate-1"
    assert len(session.calls) == 1

    write_client, write_session, write_sleeps = _client(
        [FIXTURE["rate_limit"]],
        retries=5,
    )
    with pytest.raises(OzonRateLimitError):
        write_client.submit_products({"items": []})
    assert len(write_session.calls) == 1
    assert write_sleeps == []


def test_auth_expiry_fixture_never_retries_or_discloses_credentials():
    client, session, sleeps = _client([FIXTURE["auth_expiry"]], retries=5)
    with pytest.raises(OzonAuthError) as caught:
        client.get_product_operation_limits()
    assert len(session.calls) == 1
    assert sleeps == []
    assert "synthetic-api-key" not in str(caught.value)
    assert "synthetic-client" not in str(caught.value)
    assert caught.value.request_id == "fixture-auth-1"


def test_quota_and_partial_async_fixtures_are_not_promoted_to_success():
    quota = OzonProductImportContract.normalize_quota(
        FIXTURE["quota_exhausted"],
    )
    partial_fixture = FIXTURE["partial_async_result"]
    partial = OzonProductImportContract.normalize_status(
        partial_fixture["response"],
        expected_offer_ids=partial_fixture["expected_offer_ids"],
    )
    assert quota["remaining"] == 0
    assert partial["aggregate_status"] == "partial"
    assert [item["status"] for item in partial["items"]] == [
        "imported",
        "failed",
    ]


def test_provider_drift_fixture_is_a_third_normalized_state():
    fixture = FIXTURE["provider_drift"]
    current = OzonPriceContract.normalize_read_page(
        fixture["read_response"],
    )["items"][0]
    comparable = {
        key: current.get(key)
        for key in ("offer_id", "product_id", "price", "currency_code", "old_price")
    }
    assert comparable != fixture["baseline"]
    assert comparable != fixture["proposed"]
    assert comparable["price"] == "1050"
