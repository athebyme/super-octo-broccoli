"""Exercise real requests.Response streaming and the existing Ozon transport."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from services.marketplace_adapters import MarketplaceCredentials
from services.ozon_api_client import OzonAuthError, OzonProtocolError, OzonRateLimitError, OzonSellerAPIClient
from services.ozon_read_response import OzonReadResponseTooLarge, bound_read_responses


class RawBody:
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False
        self.released = False

    def stream(self, amount, decode_content=True):
        for chunk in self.chunks:
            self.reads += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


def response(status, chunks, **headers):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers)
    result.raw = RawBody(chunks)
    return result


def client_for(result, *, cap=100):
    budget = SimpleNamespace(reserve=Mock(return_value=0), defer=Mock())
    client = OzonSellerAPIClient(MarketplaceCredentials(
        external_account_id='synthetic', api_key='synthetic'), rate_budget=budget,
        timeout=(3.0, 6.0), read_retries=0)
    physical = Mock(return_value=result)
    client.session.request = physical
    bound_read_responses(client.session, maximum_bytes=cap)
    return client, budget, physical


def test_success_is_bounded_before_transport_json_and_keeps_single_attempt():
    result = response(200, [b'{"warehouses":', b'[],"has_next":false}'])
    client, budget, physical = client_for(result)
    assert client.request('warehouses', {}) == {'warehouses': [], 'has_next': False}
    assert result.raw.reads == 2 and result.raw.released
    assert physical.call_count == budget.reserve.call_count == 1
    assert physical.call_args.kwargs['stream'] is True
    assert physical.call_args.kwargs['allow_redirects'] is False
    assert physical.call_args.kwargs['timeout'] == (3.0, 6.0)


@pytest.mark.parametrize('header', ['999999999', 'not-a-length', '-1'])
def test_429_headers_survive_any_body_or_content_length(header):
    result = response(429, [AssertionError('Error body must not be consumed')],
                      **{'Content-Length': header, 'Retry-After': '259200'})
    client, budget, physical = client_for(result)
    with pytest.raises(OzonRateLimitError) as failure:
        client.request('warehouses', {})
    assert failure.value.retry_after == 259200
    budget.defer.assert_called_once_with(259200)
    assert result.raw.reads == 0 and result.raw.closed and result.raw.released
    assert physical.call_count == 1


def test_403_keeps_access_error_without_consuming_provider_body():
    result = response(403, [AssertionError('No error-body read')], **{'Content-Length': '999999999'})
    client, _, _ = client_for(result)
    with pytest.raises(OzonAuthError) as failure:
        client.request('warehouses', {})
    assert failure.value.status_code == 403
    assert result.raw.reads == 0 and result.raw.closed


@pytest.mark.parametrize('header,chunks,reads', [
    ('101', [], 0),
    (None, [b'a' * 60, b'b' * 41, AssertionError('Stop before next chunk')], 2),
    ('1', [b'a' * 101, AssertionError('Header is not a body budget')], 1),
])
def test_oversized_success_stops_before_json_and_never_retries(header, chunks, reads):
    result = response(200, chunks, **({'Content-Length': header} if header is not None else {}))
    result.json = Mock(side_effect=AssertionError('Oversized body must not reach JSON'))
    client, budget, physical = client_for(result)
    with pytest.raises(OzonReadResponseTooLarge):
        client.request('warehouses', {})
    assert result.raw.reads == reads and result.raw.closed
    assert physical.call_count == budget.reserve.call_count == 1
    result.json.assert_not_called()


@pytest.mark.parametrize('header', ['invalid', '-1'])
def test_invalid_success_length_fails_locally_and_closes_socket(header):
    result = response(200, [AssertionError('No body read')], **{'Content-Length': header})
    client, _, _ = client_for(result)
    with pytest.raises(OzonProtocolError):
        client.request('warehouses', {})
    assert result.raw.closed and result.raw.reads == 0
