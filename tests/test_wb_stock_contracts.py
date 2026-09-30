from unittest.mock import Mock, patch

import pytest
import requests

from services.wb_api_client import WBRateLimitException, WBTransportUncertainException, WildberriesAPIClient
from services.wb_stock_contracts import ENDPOINT, WBStockContractError, stock_request


@pytest.mark.parametrize('ids', [[], [True], ['1'], [0], [1, 1], list(range(1, 102))])
def test_stock_request_requires_bounded_exact_ids(ids):
    with pytest.raises(WBStockContractError):
        stock_request(ids)


def test_stock_transport_uses_current_read_post_with_no_date_or_fallback():
    client = WildberriesAPIClient('synthetic-stock-transport')
    with patch.object(client, '_make_request', return_value=Mock(json=lambda: {'data': {'items': []}})) as call:
        assert client.get_stocks_page([101], offset=5000) == []
    call.assert_called_once_with('POST', 'analytics', ENDPOINT, json={
        'nmIds': [101], 'chrtIds': [], 'offset': 5000, 'limit': 5000,
    }, allow_redirects=False)


def test_stock_read_timeout_is_not_an_uncertain_write_and_never_retried():
    client = WildberriesAPIClient('synthetic-stock-timeout', max_retries=0)
    with patch.object(client.session, 'request', side_effect=requests.exceptions.Timeout) as request:
        with pytest.raises(WBTransportUncertainException) as caught:
            client.get_stocks_page([101])
    assert caught.value.request_may_have_been_applied is False
    request.assert_called_once()
    assert request.call_args.kwargs['allow_redirects'] is False


def test_stock_endpoint_limiter_is_shared_and_never_sleeps():
    one = WildberriesAPIClient('synthetic-shared-stock-budget')
    two = WildberriesAPIClient('synthetic-shared-stock-budget')
    response = Mock(status_code=200, json=lambda: {'data': {'items': []}})
    with patch.object(one.session, 'request', return_value=response), patch.object(
        two.session, 'request',
    ) as second, patch('services.wb_api_client.time.sleep', side_effect=AssertionError('no sleep')):
        one.get_stocks_page([101])
        with pytest.raises(WBRateLimitException):
            two.get_stocks_page([101])
    second.assert_not_called()
