"""Mocked-session tests for the one-attempt OpenAI Decisions transport."""

from datetime import datetime, timezone
import json

import pytest
import requests

from services.openai_decisions import (
    DECISIONS_MODEL,
    DECISIONS_URL,
    MAX_INPUT_JSON_BYTES,
    MAX_RESPONSE_BYTES,
    DecisionChoice,
    DecisionQuestion,
    OpenAIDecisionsClient,
    parse_retry_after,
)


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, chunks=None, error=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.chunks = chunks
        self.error = error
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size):
        self.iterated = True
        if self.error:
            raise self.error
        if self.chunks is not None:
            yield from self.chunks
        elif self.body:
            yield self.body

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.mounts = {}
        self.trust_env = True
        self.proxies = {}
        self.closed = False

    def mount(self, prefix, adapter):
        self.mounts[prefix] = adapter

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


def question_set():
    return (
        DecisionQuestion(
            name='routing',
            instructions='Select the best route.',
            choices=(
                DecisionChoice('warehouse', 'Warehouse handling'),
                DecisionChoice('seller', 'Seller handling'),
            ),
        ),
        DecisionQuestion(
            name='eligible',
            instructions='Is this eligible?',
            choices=(True, False),
        ),
    )


def response_body(answers=None, usage=None):
    if answers is None:
        answers = [
            {'type': 'choice', 'name': 'routing', 'choice': 'warehouse', 'confidence': 0.91},
            {'type': 'choice', 'name': 'eligible', 'choice': True, 'confidence': 0.0},
        ]
    if usage is None:
        usage = {
            'input_tokens': 176,
            'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
            'output_tokens': 0,
            'output_tokens_details': {'reasoning_tokens': 0},
            'total_tokens': 176,
        }
    return json.dumps({'answers': answers, 'usage': usage}, separators=(',', ':')).encode()


def client(session, **overrides):
    options = dict(
        api_key='test-key-only',
        proxy_url='http://proxy.test:3128',
        session_factory=lambda: session,
    )
    options.update(overrides)
    return OpenAIDecisionsClient(**options)


def test_one_fixed_endpoint_request_uses_explicit_proxy_and_normalizes_typed_answers():
    response = FakeResponse(body=response_body())
    session = FakeSession(response)
    result = client(session).decide(
        input_json={'seller': {'id': 7}, 'facts': ['ready', True]},
        questions=question_set(),
    )

    assert result.kind == 'success'
    assert [answer.name for answer in result.answers] == ['routing', 'eligible']
    assert [answer.choice for answer in result.answers] == ['warehouse', True]
    assert [answer.status for answer in result.answers] == ['answered', 'answered']
    assert result.answers[0].confidence == 0.91
    assert result.answers[1].confidence == 0.0
    assert result.usage.input_tokens == result.usage.total_tokens == 176
    assert result.usage.output_tokens == 0
    assert result.usage.cached_tokens == result.usage.cache_write_tokens == 0
    assert result.usage.reasoning_tokens == 0

    assert len(session.calls) == 1
    url, options = session.calls[0]
    assert url == DECISIONS_URL == 'https://api.openai.com/v1/decisions'
    assert options['headers']['Authorization'] == 'Bearer test-key-only'
    assert options['allow_redirects'] is False
    assert options['stream'] is True
    assert options['timeout'] == (3.0, 15.0)
    assert session.trust_env is False
    assert session.proxies == {
        'http': 'http://proxy.test:3128',
        'https': 'http://proxy.test:3128',
    }
    assert set(session.mounts) == {'http://', 'https://'}
    assert all(adapter.max_retries.total == 0 for adapter in session.mounts.values())
    assert response.closed and session.closed

    payload = json.loads(options['data'])
    assert payload['model'] == DECISIONS_MODEL == 'gpt-6-luna'
    assert payload['input'] == [{
        'role': 'user',
        'content': [{'type': 'input_json', 'data': {'seller': {'id': 7}, 'facts': ['ready', True]}}],
    }]
    assert [question['name'] for question in payload['questions']] == ['routing', 'eligible']
    assert payload['questions'][1]['choices'][0]['value'] is True
    assert payload['questions'][1]['choices'][1]['value'] is False


def test_boolean_and_text_choices_with_same_printed_value_remain_distinct():
    question = DecisionQuestion(
        name='typed', instructions='Choose the boolean.',
        choices=(True, 'True'),
    )
    body = response_body([
        {'type': 'choice', 'name': 'typed', 'choice': True, 'confidence': 0.5},
    ])
    result = client(FakeSession(FakeResponse(body=body))).decide(
        input_json={}, questions=(question,),
    )
    assert result.kind == 'success'
    assert result.answers[0].choice is True


@pytest.mark.parametrize('scheme', ['socks5', 'socks5h'])
def test_explicit_socks_proxy_schemes_are_preserved_without_fallback(scheme):
    proxy = f'{scheme}://proxy.test:1080'
    session = FakeSession(FakeResponse(body=response_body()))
    result = client(session, proxy_url=proxy).decide(
        input_json={}, questions=question_set(),
    )
    assert result.kind == 'success'
    assert len(session.calls) == 1
    assert session.trust_env is False
    assert session.proxies == {'http': proxy, 'https': proxy}


def test_refusal_is_a_typed_abstention_and_partial_answer_is_preserved():
    answers = [
        {'type': 'refusal', 'name': 'routing'},
        {'type': 'choice', 'name': 'eligible', 'choice': False, 'confidence': 0.6},
    ]
    result = client(FakeSession(FakeResponse(body=response_body(answers)))).decide(
        input_json={'case': 1}, questions=question_set(),
    )
    assert result.kind == 'partial_refusal'
    assert result.safe_code == 'provider_refusal'
    assert result.answers[0].status == 'refused'
    assert result.answers[0].choice is result.answers[0].confidence is None
    assert result.answers[1].status == 'answered' and result.answers[1].choice is False


def test_all_questions_refused_is_explicit_abstention():
    body = response_body([
        {'type': 'refusal', 'name': 'routing'},
        {'type': 'refusal', 'name': 'eligible'},
    ])
    result = client(FakeSession(FakeResponse(body=body))).decide(
        input_json={}, questions=question_set(),
    )
    assert result.kind == 'refused'
    assert all(answer.status == 'refused' for answer in result.answers)


def test_http_429_is_advisory_and_never_retries_or_reads_error_body():
    response = FakeResponse(
        status=429, body=b'secret provider error body',
        headers={'Retry-After': '7200'},
    )
    session = FakeSession(response)
    result = client(session).decide(input_json={}, questions=question_set())
    assert result.kind == 'rate_limited'
    assert result.http_status == 429
    assert result.retry_after_seconds == 7200
    assert result.answers == () and result.usage is None
    assert len(session.calls) == 1
    assert response.iterated is False and response.closed and session.closed
    assert 'secret provider error body' not in repr(result)


def test_http_date_retry_after_is_parsed_without_shortening():
    now = datetime(2026, 9, 27, tzinfo=timezone.utc)
    assert parse_retry_after('Sun, 27 Sep 2026 03:00:00 GMT', now=now) == 10800
    assert parse_retry_after('Sun, 27 Sep 2026 00:00:00 GMT', now=now) == 0
    assert parse_retry_after('invalid') is None
    assert parse_retry_after(str(10 * 365 * 24 * 60 * 60 + 1)) is None


@pytest.mark.parametrize('status,kind', [
    (302, 'redirect_blocked'),
    (401, 'http_error'),
    (503, 'http_error'),
])
def test_non_success_http_statuses_are_safe_and_not_retried(status, kind):
    response = FakeResponse(status=status, body=b'raw secret response')
    session = FakeSession(response)
    result = client(session).decide(input_json={}, questions=question_set())
    assert result.kind == kind and result.http_status == status
    assert result.answers == () and result.usage is None
    assert len(session.calls) == 1 and not response.iterated
    assert response.closed and session.closed
    assert 'raw secret response' not in repr(result)


def test_transport_exception_is_unknown_and_does_not_retry_or_fallback():
    session = FakeSession(error=requests.Timeout('test-key-only proxy.test secret'))
    result = client(session).decide(input_json={}, questions=question_set())
    assert result.kind == 'unknown_response'
    assert result.safe_code == 'transport_unknown'
    assert len(session.calls) == 1
    assert session.proxies['https'] == 'http://proxy.test:3128'
    assert session.closed
    assert 'test-key-only' not in repr(result)
    assert 'proxy.test' not in repr(result)


@pytest.mark.parametrize('answers', [
    [{'type': 'choice', 'name': 'eligible', 'choice': True, 'confidence': 0.7},
     {'type': 'choice', 'name': 'routing', 'choice': 'warehouse', 'confidence': 0.8}],
    [{'type': 'choice', 'name': 'routing', 'choice': 'warehouse', 'confidence': 0.8}],
    [{'type': 'choice', 'name': 'routing', 'choice': 'unknown', 'confidence': 0.8},
     {'type': 'choice', 'name': 'eligible', 'choice': True, 'confidence': 0.7}],
    [{'type': 'choice', 'name': 'routing', 'choice': 'warehouse', 'confidence': True},
     {'type': 'choice', 'name': 'eligible', 'choice': True, 'confidence': 0.7}],
    [{'type': 'choice', 'name': 'routing', 'choice': 'warehouse', 'confidence': 1.01},
     {'type': 'choice', 'name': 'eligible', 'choice': True, 'confidence': 0.7}],
    [{'type': 'choice', 'name': 'routing', 'choice': 'warehouse', 'confidence': float('nan')},
     {'type': 'choice', 'name': 'eligible', 'choice': True, 'confidence': 0.7}],
])
def test_answer_order_names_choices_and_confidence_are_validated(answers):
    response = FakeResponse(body=response_body(answers))
    result = client(FakeSession(response)).decide(input_json={}, questions=question_set())
    assert result.kind == 'invalid_response'
    assert result.answers == ()
    assert response.closed


def test_usage_is_bounded_typed_and_zero_values_are_valid():
    usage = {
        'input_tokens': 1 << 60,
        'input_tokens_details': {'cached_tokens': True, 'cache_write_tokens': 0},
        'output_tokens': 0,
        'output_tokens_details': {'reasoning_tokens': -1},
        'total_tokens': 0,
    }
    result = client(FakeSession(FakeResponse(body=response_body(usage=usage)))).decide(
        input_json={}, questions=question_set(),
    )
    assert result.kind == 'success'
    assert result.usage.input_tokens is None
    assert result.usage.cached_tokens is None
    assert result.usage.cache_write_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.reasoning_tokens is None
    assert result.usage.total_tokens == 0


@pytest.mark.parametrize('input_json', [
    ['not an object'],
    [{'not': 'an object'}, 5],
    {'value': float('nan')},
])
def test_invalid_input_json_fails_before_physical_request(input_json):
    session = FakeSession(FakeResponse(body=response_body()))
    result = client(session).decide(input_json=input_json, questions=question_set())
    assert result.kind == 'configuration_error'
    assert session.calls == []


def test_input_json_and_request_budgets_are_enforced_before_http():
    too_large = {'text': 'x' * MAX_INPUT_JSON_BYTES}
    session = FakeSession(FakeResponse(body=response_body()))
    result = client(session).decide(input_json=too_large, questions=question_set())
    assert result.kind == 'configuration_error'
    assert result.safe_code == 'input_json_too_large'
    assert session.calls == []

    result = client(session).decide(
        input_json={},
        questions=(DecisionQuestion('route', 'x' * 9000, ('a', 'b')),
                   DecisionQuestion('valid', 'choose', ('a', 'b'))),
    )
    assert result.kind == 'configuration_error'
    assert session.calls == []


def test_oversized_response_is_rejected_from_header_and_stream():
    header_response = FakeResponse(
        body=b'{}', headers={'Content-Length': str(MAX_RESPONSE_BYTES + 1)},
    )
    header_result = client(FakeSession(header_response)).decide(
        input_json={}, questions=question_set(),
    )
    assert header_result.kind == 'invalid_response'
    assert header_result.safe_code == 'response_too_large'
    assert header_response.iterated is False and header_response.closed

    stream_response = FakeResponse(chunks=[b'x' * (MAX_RESPONSE_BYTES + 1)])
    stream_result = client(FakeSession(stream_response)).decide(
        input_json={}, questions=question_set(),
    )
    assert stream_result.kind == 'invalid_response'
    assert stream_result.safe_code == 'response_too_large'
    assert stream_response.closed


@pytest.mark.parametrize('options', [
    {'api_key': '', 'proxy_url': 'http://proxy.test:3128'},
    {'api_key': 'test-key-only', 'proxy_url': ''},
    {'api_key': 'test-key-only', 'proxy_url': 'https://proxy.test:99999'},
    {'api_key': 'test-key-only', 'proxy_url': 'http://proxy.test:3128\n'},
    {'api_key': 'test key', 'proxy_url': 'http://proxy.test:3128'},
    {'api_key': 'test-key-only', 'proxy_url': 'http://proxy.test:3128', 'read_timeout': 0},
    {'api_key': 'test-key-only', 'proxy_url': 'http://proxy.test:3128', 'connect_timeout': 10},
])
def test_invalid_credentials_proxy_or_timeout_fail_closed(options):
    session = FakeSession(FakeResponse(body=response_body()))
    defaults = {
        'api_key': 'test-key-only',
        'proxy_url': 'http://proxy.test:3128',
        'session_factory': lambda: session,
    }
    defaults.update(options)
    result = OpenAIDecisionsClient(**defaults).decide(
        input_json={}, questions=question_set(),
    )
    assert result.kind == 'configuration_error'
    assert session.calls == []
