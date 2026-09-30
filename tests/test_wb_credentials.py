"""Expiry is a negative-only, secret-safe hint, checked before waiting/I/O."""

import base64
import json
from unittest.mock import Mock, patch

import pytest

from services.wb_api_client import WBTokenExpiredException, WildberriesAPIClient
from services.wb_credentials import token_expiry_hint, token_is_expired, token_public_status


def token(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'synthetic.{body}.synthetic'


@pytest.mark.parametrize('value', [None, '', 'opaque', 'a.bad!.c', 'a.e30.c', 'x' * 16385])
def test_unknown_shape_has_no_claim_of_validity_or_expiry(value):
    assert token_expiry_hint(value) is None
    assert not token_is_expired(value, now=100)


@pytest.mark.parametrize('expiry', [None, True, '50', float('nan'), float('inf'), 10**1000])
def test_untyped_expiry_does_not_prove_anything(expiry):
    assert token_expiry_hint(token({'exp': expiry})) is None


def test_expiry_boundary_and_secret_free_public_state():
    key = token({'exp': 100, 'sensitive': 'never-render-this'})
    assert not token_is_expired(key, now=99)
    assert token_is_expired(key, now=100)
    state = token_public_status(key, now=101)
    assert state == {'configured': True, 'expired': True, 'expires_at': '01.01.1970 00:01 UTC'}
    assert 'never-render-this' not in json.dumps(state)
    assert key not in json.dumps(state)


def test_expired_token_never_waits_requests_or_writes_api_log():
    client = WildberriesAPIClient(token({'exp': 100}), db_logger_callback=Mock())
    with patch('services.wb_credentials.time.time', return_value=101), patch.object(
        client.rate_limiter, 'wait_if_needed', side_effect=AssertionError('must not wait'),
    ), patch.object(client.session, 'request') as request:
        with pytest.raises(WBTokenExpiredException, match='истёк') as caught:
            client._make_request('GET', 'content', '/ping', log_to_db=True, seller_id=1)
    assert caught.value.code == 'wb_token_expired'
    request.assert_not_called()
    client.db_logger_callback.assert_not_called()


def test_long_lived_client_rechecks_expiry_without_cached_validity():
    client = WildberriesAPIClient(token({'exp': 100}))
    with patch('services.wb_credentials.time.time', return_value=99), patch.object(
        client.rate_limiter, 'wait_if_needed',
    ), patch.object(client.session, 'request', return_value=Mock(status_code=200)) as request:
        client._make_request('GET', 'statistics', '/ping')
        with patch('services.wb_credentials.time.time', return_value=100):
            with pytest.raises(WBTokenExpiredException):
                client._make_request('GET', 'statistics', '/ping')
    request.assert_called_once()
