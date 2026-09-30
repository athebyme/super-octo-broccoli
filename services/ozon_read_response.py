"""Bound successful read bodies before JSON without hiding HTTP error headers.

Install only on a read-only worker's dedicated requests session. The existing
Seller API transport still reserves attempts and owns status/Retry-After/ledger
handling. Error bodies are unnecessary for these workers' public error codes.
"""
import requests

from services.ozon_api_client import OzonProtocolError


class OzonReadResponseTooLarge(OzonProtocolError):
    def __init__(self):
        super().__init__('Ozon read response exceeds the local byte limit',
                         code='ozon_response_too_large')


def _close(response):
    try:
        response.close()
    except (OSError, requests.RequestException):
        # Cleanup cannot replace an already observed 429/403 with a transport
        # error or erase the provider's required pause.
        pass


def bound_read_responses(session, *, maximum_bytes):
    """Stream bounded 2xx bytes; return closed non-2xx headers to the transport."""
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 64 * 1024 * 1024:
        raise ValueError('Invalid Ozon read response byte limit')
    if getattr(session, '_ozon_read_response_bounded', False):
        raise ValueError('Ozon read session already has a response limit')
    original_request = session.request

    def bounded_request(*args, **kwargs):
        kwargs['stream'] = True
        response = original_request(*args, **kwargs)
        status = int(response.status_code)
        if not 200 <= status < 300:
            # Close before marking the body consumed: an unread pooled socket
            # must be discarded, not returned with an unread response body.
            _close(response)
            response._content = b'{}'
            response._content_consumed = True
            return response
        try:
            length = response.headers.get('Content-Length')
            if length is not None:
                try:
                    length = int(length)
                except (TypeError, ValueError, OverflowError):
                    raise OzonProtocolError('Invalid Ozon read response length',
                                            code='ozon_invalid_response') from None
                if length < 0:
                    raise OzonProtocolError('Invalid Ozon read response length',
                                            code='ozon_invalid_response')
                if length > maximum_bytes:
                    raise OzonReadResponseTooLarge()
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > maximum_bytes:
                    raise OzonReadResponseTooLarge()
                chunks.append(chunk)
            response._content = b''.join(chunks)
            response._content_consumed = True
            return response
        finally:
            _close(response)

    session.request = bounded_request
    session._ozon_read_response_bounded = True
