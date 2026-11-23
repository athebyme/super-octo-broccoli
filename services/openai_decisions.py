"""Bounded, single-attempt transport for OpenAI's typed Decisions API.

Credentials and the server proxy are explicit inputs. This module has no
environment, database, logging, or retry policy; callers own durable budgets.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
from typing import Any, Callable, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter


DECISIONS_URL = 'https://api.openai.com/v1/decisions'
DECISIONS_MODEL = 'gpt-6-luna'

MAX_INPUT_JSON_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 384 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_INPUT_JSON_DEPTH = 32
MAX_INPUT_JSON_VALUES = 10_000
MAX_QUESTIONS = 32
MAX_CHOICES_PER_QUESTION = 64
MAX_NAME_BYTES = 64
MAX_INSTRUCTIONS_BYTES = 8192
MAX_CHOICE_VALUE_BYTES = 512
MAX_CHOICE_DESCRIPTION_BYTES = 2048
MAX_API_KEY_BYTES = 4096
MAX_PROXY_URL_BYTES = 4096
MAX_CONNECT_TIMEOUT_SECONDS = 5.0
MAX_READ_TIMEOUT_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 10 * 365 * 24 * 60 * 60
MAX_USAGE_INTEGER = 2_147_483_647
RESPONSE_CHUNK_BYTES = 16 * 1024


ChoiceValue = Union[str, bool]


@dataclass(frozen=True)
class DecisionChoice:
    value: ChoiceValue
    description: Optional[str] = None


@dataclass(frozen=True)
class DecisionQuestion:
    name: str
    instructions: str
    choices: Sequence[Union[DecisionChoice, ChoiceValue]]


@dataclass(frozen=True)
class DecisionAnswer:
    name: str
    status: str  # answered | refused
    choice: Optional[ChoiceValue] = None
    confidence: Optional[float] = None


@dataclass(frozen=True)
class DecisionUsage:
    input_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass(frozen=True)
class DecisionOutcome:
    kind: str
    answers: Tuple[DecisionAnswer, ...] = ()
    usage: Optional[DecisionUsage] = None
    http_status: Optional[int] = None
    retry_after_seconds: Optional[int] = None
    safe_code: Optional[str] = None


class _InvalidRequest(ValueError):
    pass


class _InvalidResponse(ValueError):
    pass


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode('utf-8'))
    except UnicodeError:
        raise _InvalidRequest('invalid_unicode') from None


def _bounded_text(value: Any, maximum: int, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value.strip()):
        raise _InvalidRequest('invalid_text')
    if _utf8_size(value) > maximum:
        raise _InvalidRequest('text_too_large')
    return value


def _validate_input_json(data: Any) -> None:
    if type(data) is dict:
        pass
    elif type(data) is list and all(type(item) is dict for item in data):
        pass
    else:
        raise _InvalidRequest('invalid_input_json_shape')

    try:
        encoded = json.dumps(
            data, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise _InvalidRequest('invalid_input_json') from None
    if len(encoded) > MAX_INPUT_JSON_BYTES:
        raise _InvalidRequest('input_json_too_large')

    stack = [(data, 1)]
    value_count = 0
    while stack:
        value, depth = stack.pop()
        value_count += 1
        if value_count > MAX_INPUT_JSON_VALUES:
            raise _InvalidRequest('input_json_too_many_values')
        if depth > MAX_INPUT_JSON_DEPTH:
            raise _InvalidRequest('input_json_too_deep')
        if type(value) is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise _InvalidRequest('input_json_key_not_string')
                stack.append((child, depth + 1))
        elif type(value) is list:
            stack.extend((child, depth + 1) for child in value)
        elif value is None or type(value) in (str, bool, int):
            continue
        elif type(value) is float and math.isfinite(value):
            continue
        else:
            raise _InvalidRequest('invalid_input_json_value')


def _typed_key(value: ChoiceValue) -> Tuple[type, ChoiceValue]:
    return type(value), value


def _normalize_questions(questions: Sequence[DecisionQuestion]) -> Tuple[list, list]:
    if type(questions) not in (list, tuple) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise _InvalidRequest('invalid_question_count')

    normalized = []
    expected = []
    names = set()
    for question in questions:
        if type(question) is not DecisionQuestion:
            raise _InvalidRequest('invalid_question')
        name = _bounded_text(question.name, MAX_NAME_BYTES)
        if name in names:
            raise _InvalidRequest('duplicate_question_name')
        names.add(name)
        instructions = _bounded_text(question.instructions, MAX_INSTRUCTIONS_BYTES)
        if type(question.choices) not in (list, tuple):
            raise _InvalidRequest('invalid_choice_list')
        if not 2 <= len(question.choices) <= MAX_CHOICES_PER_QUESTION:
            raise _InvalidRequest('invalid_choice_count')

        choices = []
        values = []
        seen = set()
        for option in question.choices:
            if type(option) is DecisionChoice:
                value = option.value
                description = option.description
            else:
                value = option
                description = None
            if type(value) not in (str, bool):
                raise _InvalidRequest('invalid_choice_value')
            if type(value) is str:
                _bounded_text(value, MAX_CHOICE_VALUE_BYTES, allow_empty=True)
            key = _typed_key(value)
            if key in seen:
                raise _InvalidRequest('duplicate_choice_value')
            seen.add(key)
            item = {'value': value}
            if description is not None:
                item['description'] = _bounded_text(
                    description, MAX_CHOICE_DESCRIPTION_BYTES, allow_empty=True,
                )
            choices.append(item)
            values.append(value)
        normalized.append({
            'type': 'choice',
            'name': name,
            'instructions': instructions,
            'choices': choices,
        })
        expected.append((name, tuple(values)))
    return normalized, expected


def _request_bytes(input_json: Any, questions: Sequence[DecisionQuestion]) -> Tuple[bytes, list]:
    _validate_input_json(input_json)
    wire_questions, expected = _normalize_questions(questions)
    payload = {
        'model': DECISIONS_MODEL,
        'input': [{
            'role': 'user',
            'content': [{'type': 'input_json', 'data': input_json}],
        }],
        'questions': wire_questions,
    }
    try:
        body = json.dumps(
            payload, ensure_ascii=False, allow_nan=False,
            separators=(',', ':'),
        ).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise _InvalidRequest('request_encoding_failed') from None
    if len(body) > MAX_REQUEST_BYTES:
        raise _InvalidRequest('request_too_large')
    return body, expected


def _strict_json(body: bytes) -> Any:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result

    def finite_float(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError('non_finite_number')
        return value

    def reject_constant(_value):
        raise ValueError('invalid_number')

    try:
        text = body.decode('utf-8')
        return json.loads(
            text, object_pairs_hook=unique_object,
            parse_float=finite_float, parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise _InvalidResponse('invalid_json') from None


def _confidence(value: Any) -> float:
    if type(value) not in (int, float):
        raise _InvalidResponse('invalid_confidence')
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise _InvalidResponse('invalid_confidence') from None
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise _InvalidResponse('invalid_confidence')
    return number


def _usage_counter(usage: dict, name: str) -> Optional[int]:
    value = usage.get(name)
    if type(value) is int and 0 <= value <= MAX_USAGE_INTEGER:
        return value
    return None


def _parse_usage(value: Any) -> Optional[DecisionUsage]:
    if value is None:
        return None
    if type(value) is not dict:
        raise _InvalidResponse('invalid_usage')
    input_details = value.get('input_tokens_details')
    output_details = value.get('output_tokens_details')
    if input_details is not None and type(input_details) is not dict:
        raise _InvalidResponse('invalid_usage_details')
    if output_details is not None and type(output_details) is not dict:
        raise _InvalidResponse('invalid_usage_details')
    input_details = input_details or {}
    output_details = output_details or {}
    return DecisionUsage(
        input_tokens=_usage_counter(value, 'input_tokens'),
        cached_tokens=_usage_counter(input_details, 'cached_tokens'),
        cache_write_tokens=_usage_counter(input_details, 'cache_write_tokens'),
        output_tokens=_usage_counter(value, 'output_tokens'),
        reasoning_tokens=_usage_counter(output_details, 'reasoning_tokens'),
        total_tokens=_usage_counter(value, 'total_tokens'),
    )


def _parse_answers(envelope: Any, expected: list) -> Tuple[Tuple[DecisionAnswer, ...], Optional[DecisionUsage]]:
    if type(envelope) is not dict:
        raise _InvalidResponse('invalid_envelope')
    answers = envelope.get('answers')
    if type(answers) is not list or len(answers) != len(expected):
        raise _InvalidResponse('answer_count_mismatch')

    normalized = []
    for answer, (expected_name, expected_choices) in zip(answers, expected):
        if type(answer) is not dict or answer.get('name') != expected_name:
            raise _InvalidResponse('answer_name_or_order_mismatch')
        if answer.get('type') == 'refusal':
            normalized.append(DecisionAnswer(name=expected_name, status='refused'))
            continue
        if answer.get('type') != 'choice':
            raise _InvalidResponse('answer_type_mismatch')
        choice = answer.get('choice')
        if type(choice) not in (str, bool) or not any(
            _typed_key(choice) == _typed_key(known) for known in expected_choices
        ):
            raise _InvalidResponse('unknown_choice')
        normalized.append(DecisionAnswer(
            name=expected_name,
            status='answered',
            choice=choice,
            confidence=_confidence(answer.get('confidence')),
        ))
    usage = _parse_usage(envelope.get('usage'))
    return tuple(normalized), usage


def _valid_api_key(value: Any) -> bool:
    if type(value) is not str or not value or len(value) > MAX_API_KEY_BYTES:
        return False
    try:
        value.encode('ascii')
    except UnicodeError:
        return False
    return not any(char.isspace() or ord(char) == 0x7f for char in value)


def _valid_proxy_url(value: Any) -> bool:
    if type(value) is not str or not value or len(value) > MAX_PROXY_URL_BYTES:
        return False
    if value != value.strip() or any(char.isspace() or ord(char) == 0x7f for char in value):
        return False
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https', 'socks5', 'socks5h')
                or not parsed.hostname or parsed.fragment):
            return False
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return port is None or 1 <= port <= 65535


def _close_safely(value: Any) -> None:
    try:
        if value is not None:
            value.close()
    except Exception:
        pass


def parse_retry_after(value: Any, *, now: Optional[datetime] = None) -> Optional[int]:
    """Parse bounded Retry-After advice without shortening a valid delay."""
    if type(value) is not str or not value.strip():
        return None
    header = value.strip()
    if header.isascii() and header.isdecimal():
        if len(header) > len(str(MAX_RETRY_AFTER_SECONDS)):
            return None
        seconds = int(header)
        return seconds if seconds <= MAX_RETRY_AFTER_SECONDS else None
    try:
        target = parsedate_to_datetime(header)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        seconds = max(0, math.ceil((target - current).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if seconds <= MAX_RETRY_AFTER_SECONDS else None


class OpenAIDecisionsClient:
    """One bounded POST through the explicitly supplied server proxy."""

    def __init__(
        self,
        api_key: str,
        proxy_url: str,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        connect_timeout: Union[int, float] = 3,
        read_timeout: Union[int, float] = 15,
    ):
        self._api_key = api_key
        self._proxy_url = proxy_url
        self._session_factory = session_factory or requests.Session
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

    def _configuration_is_valid(self) -> bool:
        return (
            _valid_api_key(self._api_key)
            and _valid_proxy_url(self._proxy_url)
            and callable(self._session_factory)
            and type(self._connect_timeout) in (int, float)
            and 0 < self._connect_timeout <= MAX_CONNECT_TIMEOUT_SECONDS
            and type(self._read_timeout) in (int, float)
            and 0 < self._read_timeout <= MAX_READ_TIMEOUT_SECONDS
        )

    def decide(
        self,
        *,
        input_json: Any,
        questions: Sequence[DecisionQuestion],
    ) -> DecisionOutcome:
        if not self._configuration_is_valid():
            return DecisionOutcome('configuration_error', safe_code='invalid_configuration')
        try:
            body, expected = _request_bytes(input_json, questions)
        except _InvalidRequest as exc:
            return DecisionOutcome('configuration_error', safe_code=str(exc))

        session = None
        response = None
        request_started = False
        try:
            session = self._session_factory()
            session.trust_env = False
            session.proxies = {'http': self._proxy_url, 'https': self._proxy_url}
            mount = getattr(session, 'mount', None)
            if callable(mount):
                session.mount('http://', HTTPAdapter(max_retries=0))
                session.mount('https://', HTTPAdapter(max_retries=0))

            request_started = True
            response = session.post(
                DECISIONS_URL,
                data=body,
                headers={
                    'Authorization': 'Bearer ' + self._api_key,
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                timeout=(float(self._connect_timeout), float(self._read_timeout)),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException:
            _close_safely(session)
            return DecisionOutcome(
                'unknown_response' if request_started else 'configuration_error',
                safe_code='transport_unknown' if request_started else 'session_unavailable',
            )
        except Exception:
            # Never include exception text: Requests can include proxy URL or
            # authorization context in diagnostic strings.
            _close_safely(session)
            return DecisionOutcome(
                'unknown_response' if request_started else 'configuration_error',
                safe_code='transport_unknown' if request_started else 'session_unavailable',
            )

        try:
            status = response.status_code
            if type(status) is not int or not 100 <= status <= 599:
                return DecisionOutcome('unknown_response', safe_code='invalid_http_status')
            if 300 <= status < 400:
                return DecisionOutcome(
                    'redirect_blocked', http_status=status,
                    safe_code='redirect_not_followed',
                )
            if status == 429:
                headers = getattr(response, 'headers', {}) or {}
                retry_after = parse_retry_after(headers.get('Retry-After'))
                return DecisionOutcome(
                    'rate_limited', http_status=429,
                    retry_after_seconds=retry_after,
                    safe_code='provider_rate_limited',
                )
            if status != 200:
                return DecisionOutcome(
                    'http_error', http_status=status,
                    safe_code='provider_http_error',
                )

            headers = getattr(response, 'headers', {}) or {}
            content_length = headers.get('Content-Length')
            if isinstance(content_length, str) and content_length.isascii() and content_length.isdecimal():
                if (len(content_length) > len(str(MAX_RESPONSE_BYTES))
                        or int(content_length) > MAX_RESPONSE_BYTES):
                    return DecisionOutcome(
                        'invalid_response', http_status=200,
                        safe_code='response_too_large',
                    )

            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=RESPONSE_CHUNK_BYTES):
                if not chunk:
                    continue
                if type(chunk) is not bytes:
                    return DecisionOutcome(
                        'invalid_response', http_status=200,
                        safe_code='invalid_response_chunk',
                    )
                if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                    return DecisionOutcome(
                        'invalid_response', http_status=200,
                        safe_code='response_too_large',
                    )
                chunks.extend(chunk)
            envelope = _strict_json(bytes(chunks))
            answers, usage = _parse_answers(envelope, expected)
            answered = sum(answer.status == 'answered' for answer in answers)
            kind = 'success' if answered == len(answers) else (
                'refused' if answered == 0 else 'partial_refusal'
            )
            return DecisionOutcome(
                kind, answers=answers, usage=usage,
                http_status=200,
                safe_code=None if kind == 'success' else 'provider_refusal',
            )
        except _InvalidResponse as exc:
            return DecisionOutcome(
                'invalid_response', http_status=200, safe_code=str(exc),
            )
        except Exception:
            # Includes incomplete streamed bodies and decoder failures after a
            # provider request. The caller must not infer or replay an answer.
            return DecisionOutcome(
                'unknown_response', http_status=200,
                safe_code='response_body_unknown',
            )
        finally:
            _close_safely(response)
            _close_safely(session)
