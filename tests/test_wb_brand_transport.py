from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock, patch

import pytest

from services.wb_api_client import WBRateLimitException, WildberriesAPIClient


def test_brand_get_has_single_attempt_adapter_even_with_general_read_retries():
    with WildberriesAPIClient('synthetic-brand-adapter', max_retries=3) as client:
        brands=client.session.get_adapter('https://content-api.wildberries.ru/api/content/v1/brands?subjectId=1')
        content=client.session.get_adapter('https://content-api.wildberries.ru/content/v2/object/all')
        assert brands.max_retries.total == 0
        assert content.max_retries.total == 3
        assert content.max_retries.is_retry('GET',429,has_retry_after=True) is False
        assert content.max_retries.is_retry('GET',503,has_retry_after=True) is True


@pytest.mark.parametrize('max_retries', [0, 3])
@pytest.mark.parametrize('retry_after', [None, '120'])
def test_reference_429_is_one_physical_attempt_without_transport_sleep(max_retries, retry_after):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(429)
            if retry_after is not None:
                self.send_header('Retry-After', retry_after)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        token = f'synthetic-reference-429-{max_retries}-{retry_after}'
        with WildberriesAPIClient(token, max_retries=max_retries, timeout=2) as client:
            client.session.trust_env = False
            with patch.object(client, '_get_base_url', return_value=f'http://127.0.0.1:{server.server_port}'), patch(
                'services.wb_api_client.time.sleep', side_effect=AssertionError('unexpected sleep')
            ), pytest.raises(WBRateLimitException) as caught:
                client.get_directory_kinds()
            assert caught.value.retry_after == (120 if retry_after else None)
        assert calls == ['/content/v2/directory/kinds?locale=ru']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('sandbox', [False, True])
def test_brand_429_stops_sweep_with_one_attempt_and_no_sleep(sandbox):
    with WildberriesAPIClient(f'synthetic-brand-429-{sandbox}',sandbox=sandbox) as client:
        response=Mock(status_code=429, headers={'Retry-After':'90'},text='rate limited')
        with patch.object(client.session,'request',return_value=response) as call, patch(
            'services.wb_api_client.time.sleep',side_effect=AssertionError('unexpected sleep')
        ):
            result=client.fetch_all_brands([1,2])
        call.assert_called_once()
        assert call.call_args.kwargs['allow_redirects'] is False
        assert result['completed_subject_ids'] == []
        assert result['errors']
        assert client.session.get_adapter(client._get_base_url('content')+'/api/content/v1/brands').max_retries.total == 0
