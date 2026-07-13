# -*- coding: utf-8 -*-
"""
Мульти-модельный LLM слой.

Поддерживает:
  - Google Gemini (через google-genai SDK)
  - Anthropic Claude (через anthropic SDK)
  - Cloud.ru Foundation Models (OpenAI-compatible API)
  - Любой OpenAI-совместимый API (vLLM, Ollama, LM Studio, etc.)

Унифицированный интерфейс: chat(), chat_with_tools(), structured_output()
Все методы chat_with_tools() возвращают usage (input_tokens, output_tokens).
"""
import functools
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import AgentConfig

logger = logging.getLogger(__name__)


# Official DeepSeek API prices, USD per 1M tokens, checked 2026-07-13.
# Keep estimates explicitly separate from provider-reported cost_usd.
# https://api-docs.deepseek.com/quick_start/pricing/
DEEPSEEK_PRICING_USD_PER_MILLION = {
    'deepseek-v4-flash': {
        'cache_hit_input': 0.0028,
        'cache_miss_input': 0.14,
        'output': 0.28,
    },
    'deepseek-v4-pro': {
        'cache_hit_input': 0.003625,
        'cache_miss_input': 0.435,
        'output': 0.87,
    },
}


def _field(value: Any, name: str, default=None):
    """Reads SDK response fields from either objects or dictionaries."""
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _extract_openai_usage(usage: Any, model: str = '') -> dict:
    """Normalizes OpenAI-compatible usage, including provider cache metrics."""
    if not usage:
        return {}

    input_tokens = int(_field(usage, 'prompt_tokens', 0) or 0)
    output_tokens = int(_field(usage, 'completion_tokens', 0) or 0)
    normalized = {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'api_requests': 1,
    }
    if str(model or '').strip():
        normalized['model'] = str(model).strip()

    # DeepSeek exposes top-level hit/miss fields. OpenAI-compatible proxies may
    # expose only prompt_tokens_details.cached_tokens, so support both shapes.
    cache_hit_tokens = _field(usage, 'prompt_cache_hit_tokens')
    cache_miss_tokens = _field(usage, 'prompt_cache_miss_tokens')
    prompt_details = _field(usage, 'prompt_tokens_details')
    if cache_hit_tokens is None and prompt_details is not None:
        cache_hit_tokens = _field(prompt_details, 'cached_tokens')
    if cache_hit_tokens is not None:
        cache_hit_tokens = int(cache_hit_tokens or 0)
        normalized['cache_hit_tokens'] = cache_hit_tokens
        if cache_miss_tokens is None:
            cache_miss_tokens = max(input_tokens - cache_hit_tokens, 0)
    if cache_miss_tokens is not None:
        normalized['cache_miss_tokens'] = int(cache_miss_tokens or 0)

    completion_details = _field(usage, 'completion_tokens_details')
    reasoning_tokens = _field(completion_details, 'reasoning_tokens')
    if reasoning_tokens is not None:
        normalized['reasoning_tokens'] = int(reasoning_tokens or 0)

    provider_cost = _field(usage, 'cost')
    if provider_cost is None:
        provider_cost = _field(usage, 'total_cost')
    if provider_cost is not None:
        normalized['cost_usd'] = float(provider_cost or 0)

    prices = DEEPSEEK_PRICING_USD_PER_MILLION.get(str(model).lower())
    if prices:
        hit_tokens = int(normalized.get('cache_hit_tokens') or 0)
        miss_tokens = normalized.get('cache_miss_tokens')
        if miss_tokens is None:
            miss_tokens = max(input_tokens - hit_tokens, 0)
        estimated_cost = (
            hit_tokens * prices['cache_hit_input']
            + int(miss_tokens) * prices['cache_miss_input']
            + output_tokens * prices['output']
        ) / 1_000_000
        normalized['estimated_cost_usd'] = round(estimated_cost, 12)

    if 'cache_hit_tokens' in normalized:
        normalized['cache_hit'] = normalized['cache_hit_tokens'] > 0
        normalized['cache_hit_rate'] = (
            round(normalized['cache_hit_tokens'] / input_tokens, 6)
            if input_tokens else 0.0
        )
    return normalized


def _safe_base_url_for_log(value: str) -> str:
    """Удаляет userinfo/query/fragment перед записью URL в лог или ошибку."""
    try:
        parsed = urlsplit(value or '')
        hostname = parsed.hostname or ''
        if parsed.port:
            hostname = f'{hostname}:{parsed.port}'
        return urlunsplit((parsed.scheme, hostname, parsed.path, '', ''))
    except Exception:
        return '[configured URL]'


# ── Retry-декоратор для LLM-вызовов ──────────────────────────────

_llm_attempt_count: ContextVar[int | None] = ContextVar(
    'llm_attempt_count', default=None,
)
_llm_attempts_remaining: ContextVar[int | None] = ContextVar(
    'llm_attempts_remaining', default=None,
)


@contextmanager
def llm_retry_attempt_limit(max_attempts: int):
    """Caps physical retry attempts within the current request context."""
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts <= 0
    ):
        raise ValueError('max_attempts must be a positive integer')

    parent_remaining = _llm_attempts_remaining.get()
    effective_limit = (
        max_attempts
        if parent_remaining is None
        else min(parent_remaining, max_attempts)
    )
    token = _llm_attempts_remaining.set(effective_limit)
    try:
        yield
    finally:
        remaining = _llm_attempts_remaining.get()
        consumed = max(effective_limit - int(remaining or 0), 0)
        _llm_attempts_remaining.reset(token)
        if parent_remaining is not None:
            _llm_attempts_remaining.set(max(parent_remaining - consumed, 0))


def _start_attempt_capture():
    """Starts or joins a request-local physical-attempt capture."""
    current = _llm_attempt_count.get()
    if current is None:
        return _llm_attempt_count.set(0), 0
    return None, current


def _captured_attempts_since(start: int) -> int:
    current = _llm_attempt_count.get()
    if current is None:
        return 0
    return max(int(current) - int(start), 0)


def _finish_attempt_capture(token) -> None:
    if token is not None:
        _llm_attempt_count.reset(token)


def _usage_with_api_requests(usage: Any, api_requests: int) -> dict:
    normalized = dict(usage) if isinstance(usage, dict) else {}
    normalized['api_requests'] = max(int(api_requests), 0)
    return normalized


def _attach_api_request_usage(error: Exception, api_requests: int) -> None:
    existing = getattr(error, 'llm_usage', None)
    existing_count = 0
    if isinstance(existing, dict):
        try:
            existing_count = int(existing.get('api_requests') or 0)
        except (TypeError, ValueError):
            existing_count = 0
    error.llm_usage = _usage_with_api_requests(
        existing, max(int(api_requests), existing_count, 0),
    )


def _result_with_api_request_usage(result: Any, api_requests: int) -> Any:
    """Adds attempt count only to result shapes that already expose usage."""
    if not isinstance(result, dict) or not isinstance(result.get('usage'), dict):
        return result
    normalized = dict(result)
    normalized['usage'] = _usage_with_api_requests(
        result['usage'], api_requests,
    )
    return normalized


def llm_retry(max_retries: int = 3, base_delay: float = 2.0):
    """
    Retry с экспоненциальным backoff для LLM-вызовов.

    Перехватывает сетевые ошибки, rate-limit (429), server errors (502/503/529).
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            capture_token, capture_start = _start_attempt_capture()
            try:
                for attempt in range(max_retries + 1):
                    attempts_remaining = _llm_attempts_remaining.get()
                    if attempts_remaining is not None:
                        # A positive limit is guaranteed by the public context
                        # manager. Reaching zero is handled after the preceding
                        # failed attempt, before another provider call or sleep.
                        if attempts_remaining <= 0:
                            error = RuntimeError(
                                'LLM retry attempt limit exhausted',
                            )
                            _attach_api_request_usage(
                                error,
                                _captured_attempts_since(capture_start),
                            )
                            raise error
                        _llm_attempts_remaining.set(attempts_remaining - 1)
                    _llm_attempt_count.set(
                        int(_llm_attempt_count.get() or 0) + 1,
                    )
                    try:
                        result = func(*args, **kwargs)
                    except (ConnectionError, TimeoutError, OSError) as caught:
                        last_error = caught
                        is_retryable = True
                    except Exception as caught:
                        last_error = caught
                        err_str = str(caught).lower()
                        status = (
                            getattr(
                                getattr(caught, 'response', None),
                                'status_code', 0,
                            ) or 0
                        )
                        is_retryable = (
                            status in (429, 502, 503, 529)
                            or 'rate' in err_str
                            or 'overloaded' in err_str
                            or 'timeout' in err_str
                        )
                    else:
                        return _result_with_api_request_usage(
                            result,
                            _captured_attempts_since(capture_start),
                        )

                    attempts = _captured_attempts_since(capture_start)
                    attempt_limit_reached = (
                        _llm_attempts_remaining.get() == 0
                    )
                    if (
                        not is_retryable
                        or attempt >= max_retries
                        or attempt_limit_reached
                    ):
                        _attach_api_request_usage(last_error, attempts)
                        raise last_error

                    wait = base_delay * (2 ** attempt)
                    logger.warning(
                        "LLM call failed (attempt %s/%s), retry in %.0fs: %s",
                        attempt + 1, max_retries + 1, wait, last_error,
                    )
                    time.sleep(wait)
            finally:
                _finish_attempt_capture(capture_token)
        return wrapper
    return decorator


# ── Базовый интерфейс ─────────────────────────────────────────────

class BaseLLM(ABC):
    """Абстрактный LLM-провайдер."""

    @abstractmethod
    def chat(self, system: str, messages: list[dict],
             temperature: float = None, max_tokens: int = None) -> str:
        """Простой чат. Возвращает текстовый ответ."""
        ...

    @abstractmethod
    def chat_with_tools(self, system: str, messages: list[dict],
                        tools: list[dict],
                        temperature: float = None,
                        max_tokens: int = None) -> dict:
        """
        Чат с поддержкой tool_use / function_calling.
        Возвращает: {
            'text': str,           # текстовая часть ответа
            'tool_calls': [        # вызовы инструментов
                {'name': str, 'arguments': dict, 'id': str}
            ],
            'stop_reason': str,    # 'end_turn' | 'tool_use' | 'stop'
            'usage': {             # трекинг токенов
                'input_tokens': int,
                'output_tokens': int,
            },
        }
        """
        ...

    @abstractmethod
    def structured_output(self, system: str, prompt: str,
                          schema: dict, max_tokens: int = None) -> dict:
        """Возвращает JSON по заданной схеме."""
        ...

    def structured_output_with_usage(self, system: str, prompt: str,
                                     schema: dict,
                                     max_tokens: int = None) -> dict:
        """Compatibility wrapper for providers without structured usage data."""
        capture_token, capture_start = _start_attempt_capture()
        try:
            data = self.structured_output(
                system, prompt, schema, max_tokens=max_tokens,
            )
            attempts = _captured_attempts_since(capture_start)
            return {
                'data': data,
                'usage': _usage_with_api_requests({}, attempts or 1),
            }
        except Exception as error:
            attempts = _captured_attempts_since(capture_start)
            _attach_api_request_usage(error, attempts or 1)
            raise
        finally:
            _finish_attempt_capture(capture_token)

    def chat_with_usage(self, system: str, messages: list[dict],
                        temperature: float = None,
                        max_tokens: int = None) -> dict:
        """Compatibility wrapper for providers whose chat API returns text only."""
        capture_token, capture_start = _start_attempt_capture()
        try:
            text = self.chat(system, messages, temperature, max_tokens)
            attempts = _captured_attempts_since(capture_start)
            return {
                'text': text,
                'usage': _usage_with_api_requests({}, attempts or 1),
            }
        except Exception as error:
            attempts = _captured_attempts_since(capture_start)
            _attach_api_request_usage(error, attempts or 1)
            raise
        finally:
            _finish_attempt_capture(capture_token)


def _extract_json_from_text(text: str) -> dict:
    """Надёжное извлечение JSON из текстового ответа LLM."""
    text = text.strip()

    # 1. Весь текст как JSON
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Из code block
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. Первый { ... } блок
    brace_depth = 0
    start_idx = None
    for i, ch in enumerate(text):
        if ch == '{':
            if brace_depth == 0:
                start_idx = i
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0 and start_idx is not None:
                try:
                    return json.loads(text[start_idx:i + 1])
                except (json.JSONDecodeError, ValueError):
                    start_idx = None

    raise ValueError(f"Cannot extract JSON from LLM response: {text[:200]}")


# ── Claude ─────────────────────────────────────────────────────────

class ClaudeLLM(BaseLLM):
    """Anthropic Claude через anthropic SDK."""

    def __init__(self, config: AgentConfig = None):
        self.cfg = config or AgentConfig
        import anthropic
        self.client = anthropic.Anthropic(api_key=self.cfg.ANTHROPIC_API_KEY)
        self.model = self.cfg.CLAUDE_MODEL
        logger.info(f"Claude LLM initialized: {self.model}")

    @llm_retry()
    def chat(self, system: str, messages: list[dict],
             temperature: float = None, max_tokens: int = None) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens or self.cfg.MAX_TOKENS,
            temperature=temperature if temperature is not None else self.cfg.TEMPERATURE,
            system=system,
            messages=messages,
        )
        return resp.content[0].text

    @llm_retry()
    def chat_with_tools(self, system: str, messages: list[dict],
                        tools: list[dict],
                        temperature: float = None,
                        max_tokens: int = None) -> dict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens or self.cfg.MAX_TOKENS,
            temperature=temperature if temperature is not None else self.cfg.TEMPERATURE,
            system=system,
            messages=messages,
            tools=tools,
        )

        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == 'text':
                text_parts.append(block.text)
            elif block.type == 'tool_use':
                tool_calls.append({
                    'name': block.name,
                    'arguments': block.input,
                    'id': block.id,
                })

        # Извлекаем usage из ответа Claude
        usage = {'api_requests': 1}
        if hasattr(resp, 'usage') and resp.usage:
            usage = {
                'input_tokens': getattr(resp.usage, 'input_tokens', 0),
                'output_tokens': getattr(resp.usage, 'output_tokens', 0),
                'api_requests': 1,
            }

        return {
            'text': '\n'.join(text_parts),
            'tool_calls': tool_calls,
            'stop_reason': resp.stop_reason,
            'usage': usage,
        }

    def structured_output(self, system: str, prompt: str,
                          schema: dict, max_tokens: int = None) -> dict:
        schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
        full_prompt = (
            f"{prompt}\n\n"
            f"Ответь СТРОГО в JSON формате по схеме:\n```json\n{schema_str}\n```\n"
            f"Без комментариев, только валидный JSON."
        )
        text = self.chat(
            system, [{'role': 'user', 'content': full_prompt}],
            max_tokens=max_tokens,
        )
        return _extract_json_from_text(text)


# ── Gemini ─────────────────────────────────────────────────────────

class GeminiLLM(BaseLLM):
    """Google Gemini через google-genai SDK."""

    def __init__(self, config: AgentConfig = None):
        self.cfg = config or AgentConfig
        from google import genai
        self.client = genai.Client(api_key=self.cfg.GEMINI_API_KEY)
        self.model = self.cfg.GEMINI_MODEL
        logger.info(f"Gemini LLM initialized: {self.model}")

    @llm_retry()
    def chat(self, system: str, messages: list[dict],
             temperature: float = None, max_tokens: int = None) -> str:
        from google.genai import types

        contents = []
        for msg in messages:
            role = 'user' if msg['role'] == 'user' else 'model'
            contents.append(types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg['content'])]
            ))

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature if temperature is not None else self.cfg.TEMPERATURE,
            max_output_tokens=max_tokens or self.cfg.MAX_TOKENS,
        )

        resp = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )
        return resp.text

    @llm_retry()
    def chat_with_tools(self, system: str, messages: list[dict],
                        tools: list[dict],
                        temperature: float = None,
                        max_tokens: int = None) -> dict:
        from google.genai import types

        # Конвертируем tools из формата Claude/OpenAI в формат Gemini
        gemini_tools = []
        for tool in tools:
            func_decl = types.FunctionDeclaration(
                name=tool['name'],
                description=tool.get('description', ''),
                parameters=tool.get('input_schema', tool.get('parameters', {})),
            )
            gemini_tools.append(types.Tool(function_declarations=[func_decl]))

        contents = []
        for msg in messages:
            role = 'user' if msg['role'] == 'user' else 'model'
            contents.append(types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg['content'])]
            ))

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature if temperature is not None else self.cfg.TEMPERATURE,
            max_output_tokens=max_tokens or self.cfg.MAX_TOKENS,
            tools=gemini_tools,
        )

        resp = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )

        text_parts = []
        tool_calls = []
        for part in resp.candidates[0].content.parts:
            if part.text:
                text_parts.append(part.text)
            elif part.function_call:
                tool_calls.append({
                    'name': part.function_call.name,
                    'arguments': dict(part.function_call.args) if part.function_call.args else {},
                    'id': f"call_{part.function_call.name}_{int(time.time())}",
                })

        stop_reason = 'tool_use' if tool_calls else 'end_turn'

        # Извлекаем usage из ответа Gemini
        usage = {'api_requests': 1}
        if hasattr(resp, 'usage_metadata') and resp.usage_metadata:
            um = resp.usage_metadata
            usage = {
                'input_tokens': getattr(um, 'prompt_token_count', 0) or 0,
                'output_tokens': getattr(um, 'candidates_token_count', 0) or 0,
                'api_requests': 1,
            }

        return {
            'text': '\n'.join(text_parts),
            'tool_calls': tool_calls,
            'stop_reason': stop_reason,
            'usage': usage,
        }

    def structured_output(self, system: str, prompt: str,
                          schema: dict, max_tokens: int = None) -> dict:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.cfg.TEMPERATURE,
            max_output_tokens=max_tokens or self.cfg.MAX_TOKENS,
            response_mime_type='application/json',
            response_schema=schema,
        )

        resp = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )
        return json.loads(resp.text)


# ── OpenAI-совместимый API (Cloud.ru, vLLM, Ollama, etc.) ─────────

class LLMProviderError(Exception):
    """Ошибка провайдера LLM (неверный URL, HTML вместо JSON и т.д.)."""
    pass


class OpenAICompatLLM(BaseLLM):
    """
    Универсальный провайдер через OpenAI-совместимый API.

    Работает с:
      - Cloud.ru Foundation Models (DeepSeek, Qwen, Llama)
      - vLLM / TGI (self-hosted)
      - Ollama
      - LM Studio
      - Together AI, Fireworks, Groq и др.
    """

    def __init__(self, config: AgentConfig = None,
                 api_key: str = None, base_url: str = None, model: str = None):
        self.cfg = config or AgentConfig
        from openai import OpenAI

        self.api_key = api_key or self.cfg.OPENAI_COMPAT_API_KEY or 'not-needed'
        self.base_url = base_url or self.cfg.OPENAI_COMPAT_BASE_URL
        self.model = model or self.cfg.OPENAI_COMPAT_MODEL
        self.thinking = None

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        logger.info(
            "OpenAI-compat LLM initialized: %s @ %s",
            self.model, _safe_base_url_for_log(self.base_url),
        )

    def _thinking_request_kwargs(self) -> dict:
        if not getattr(self, 'supports_thinking_toggle', False):
            return {}
        thinking = getattr(self, 'thinking', None)
        if thinking is None:
            return {}
        return {
            'extra_body': {
                'thinking': {'type': 'enabled' if thinking else 'disabled'},
            },
        }

    def _check_api_error(self, error: Exception):
        """Проверяет, не вернул ли API HTML вместо JSON (типичная ошибка Cloud.ru 404)."""
        err_str = str(error)
        # OpenAI SDK выбрасывает APIConnectionError или APIStatusError с HTML в body
        if any(marker in err_str for marker in ['<!DOCTYPE', '<html', '<!doctype', 'Ошибка 404', 'Page not found']):
            raise LLMProviderError(
                f"LLM API вернул HTML вместо JSON. "
                f"Проверьте CLOUDRU_BASE_URL ({_safe_base_url_for_log(self.base_url)}) "
                f"и CLOUDRU_MODEL ({self.model}). "
                f"Текущий URL может быть некорректным — API возвращает веб-страницу с ошибкой 404."
            ) from error
        if 'Connection error' in err_str or 'connection' in err_str.lower():
            raise LLMProviderError(
                f"Не удалось подключиться к LLM API: {_safe_base_url_for_log(self.base_url)}. "
                f"Проверьте CLOUDRU_BASE_URL и доступность сервера."
            ) from error

    def chat(self, system: str, messages: list[dict],
             temperature: float = None, max_tokens: int = None) -> str:
        return self.chat_with_usage(
            system, messages, temperature=temperature, max_tokens=max_tokens,
        )['text']

    @llm_retry()
    def chat_with_usage(self, system: str, messages: list[dict],
                        temperature: float = None,
                        max_tokens: int = None) -> dict:
        oai_messages = [{'role': 'system', 'content': system}]
        oai_messages.extend(messages)

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=oai_messages,
                temperature=temperature if temperature is not None else self.cfg.TEMPERATURE,
                max_tokens=max_tokens or self.cfg.MAX_TOKENS,
                **self._thinking_request_kwargs(),
            )
        except Exception as e:
            self._check_api_error(e)
            raise
        return {
            'text': resp.choices[0].message.content or '',
            'usage': _extract_openai_usage(getattr(resp, 'usage', None), self.model),
        }

    @llm_retry()
    def chat_with_tools(self, system: str, messages: list[dict],
                        tools: list[dict],
                        temperature: float = None,
                        max_tokens: int = None) -> dict:
        oai_messages = [{'role': 'system', 'content': system}]
        oai_messages.extend(messages)

        # Конвертируем tools в формат OpenAI
        oai_tools = []
        for tool in tools:
            oai_tools.append({
                'type': 'function',
                'function': {
                    'name': tool['name'],
                    'description': tool.get('description', ''),
                    'parameters': tool.get('input_schema', tool.get('parameters', {})),
                },
            })

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=oai_messages,
                tools=oai_tools if oai_tools else None,
                temperature=temperature if temperature is not None else self.cfg.TEMPERATURE,
                max_tokens=max_tokens or self.cfg.MAX_TOKENS,
                **self._thinking_request_kwargs(),
            )
        except Exception as e:
            self._check_api_error(e)
            raise

        msg = resp.choices[0].message
        text = msg.content or ''
        tool_calls = []

        if msg.tool_calls:
            for tc in msg.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                tool_calls.append({
                    'name': tc.function.name,
                    'arguments': args,
                    'id': tc.id or f"call_{tc.function.name}_{int(time.time())}",
                })

        stop_reason = 'tool_use' if tool_calls else 'end_turn'

        usage = _extract_openai_usage(getattr(resp, 'usage', None), self.model)

        return {
            'text': text,
            'tool_calls': tool_calls,
            'stop_reason': stop_reason,
            'usage': usage,
        }

    def structured_output(self, system: str, prompt: str,
                          schema: dict, max_tokens: int = None) -> dict:
        return self.structured_output_with_usage(
            system, prompt, schema, max_tokens=max_tokens,
        )['data']

    @llm_retry()
    def structured_output_with_usage(self, system: str, prompt: str,
                                     schema: dict,
                                     max_tokens: int = None) -> dict:
        """Structured call with a stable cacheable prefix and raw usage metrics."""
        schema_str = json.dumps(
            schema, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        )
        structured_system = (
            f"{system}\n\n"
            "СТАБИЛЬНЫЙ КОНТРАКТ JSON-ОТВЕТА:\n"
            f"{schema_str}\n"
            "Ответь строго валидным JSON по этой схеме, без markdown и комментариев."
        )
        messages = [
            {'role': 'system', 'content': structured_system},
            {'role': 'user', 'content': prompt},
        ]
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.cfg.TEMPERATURE,
                max_tokens=max_tokens or self.cfg.MAX_TOKENS,
                **self._thinking_request_kwargs(),
            )
        except Exception as e:
            self._check_api_error(e)
            raise

        text = resp.choices[0].message.content or ''
        usage = _extract_openai_usage(getattr(resp, 'usage', None), self.model)
        try:
            data = _extract_json_from_text(text)
        except Exception as exc:
            # Callers with a deterministic fallback can still account for the
            # completed provider request when the model returned invalid JSON.
            exc.llm_usage = usage
            raise
        return {
            'data': data,
            'usage': usage,
        }


class CloudRuLLM(OpenAICompatLLM):
    """Cloud.ru Foundation Models — основной провайдер (GPT-OSS-120B и др.)."""

    def __init__(self, config: AgentConfig = None):
        cfg = config or AgentConfig
        super().__init__(
            config=cfg,
            api_key=cfg.CLOUDRU_API_KEY,
            base_url=cfg.CLOUDRU_BASE_URL,
            model=cfg.CLOUDRU_MODEL,
        )
        logger.info(f"Cloud.ru LLM initialized: {self.model}")


class OpenRouterLLM(OpenAICompatLLM):
    """
    OpenRouter — единый API для 300+ моделей (DeepSeek, Claude, Gemini, GPT-4o, Llama и др.)

    Поддерживает OpenAI-совместимый формат с доп. заголовками.
    https://openrouter.ai/docs
    """

    def __init__(self, config: AgentConfig = None):
        cfg = config or AgentConfig
        from openai import OpenAI

        self.cfg = cfg
        self.api_key = cfg.OPENROUTER_API_KEY
        self.base_url = 'https://openrouter.ai/api/v1'
        self.model = cfg.OPENROUTER_MODEL

        if not self.api_key:
            raise LLMProviderError("OPENROUTER_API_KEY не задан")
        if not self.model:
            raise LLMProviderError("OPENROUTER_MODEL не задан")

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers={
                'HTTP-Referer': 'https://seller-platform.tech',
                'X-Title': 'Seller Hub Agents',
            },
        )
        logger.info(f"OpenRouter LLM initialized: {self.model}")


class DeepSeekLLM(OpenAICompatLLM):
    """
    DeepSeek Platform — нативный API (deepseek-v4-pro / deepseek-v4-flash).

    OpenAI-совместимый формат. https://api-docs.deepseek.com
    """

    supports_thinking_toggle = True

    def __init__(self, config: AgentConfig = None):
        cfg = config or AgentConfig
        if not cfg.DEEPSEEK_API_KEY:
            raise LLMProviderError("DEEPSEEK_API_KEY не задан")
        super().__init__(
            config=cfg,
            api_key=cfg.DEEPSEEK_API_KEY,
            base_url=cfg.DEEPSEEK_BASE_URL,
            model=cfg.DEEPSEEK_MODEL,
        )
        self.thinking = getattr(cfg, 'DEEPSEEK_THINKING', None)
        logger.info(f"DeepSeek LLM initialized: {self.model}")


# ── Фабрика ───────────────────────────────────────────────────────

class _ConfigOverride:
    """Read-only config proxy для task-scoped provider credentials."""

    def __init__(self, base, **overrides):
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


def create_llm_from_profile(profile: dict, config: AgentConfig = None) -> BaseLLM:
    """Создаёт LLM из task profile, не записывая credentials в логи."""
    base = config or AgentConfig
    profile = profile or {}
    provider = str(profile.get('provider') or 'deepseek').strip().lower()
    model = str(profile.get('model') or '').strip()
    api_key = profile.get('key') or ''
    base_url = str(profile.get('base_url') or '').strip()

    if provider == 'deepseek':
        cfg = _ConfigOverride(
            base,
            DEEPSEEK_API_KEY=api_key or base.DEEPSEEK_API_KEY,
            DEEPSEEK_BASE_URL=base_url or base.DEEPSEEK_BASE_URL,
            DEEPSEEK_MODEL=model or 'deepseek-v4-pro',
            DEEPSEEK_THINKING=profile.get('thinking') if 'thinking' in profile else None,
        )
        return DeepSeekLLM(cfg)
    if provider == 'cloudru':
        cfg = _ConfigOverride(
            base,
            CLOUDRU_API_KEY=api_key or base.CLOUDRU_API_KEY,
            CLOUDRU_BASE_URL=base_url or base.CLOUDRU_BASE_URL,
            CLOUDRU_MODEL=model or base.CLOUDRU_MODEL,
        )
        return CloudRuLLM(cfg)
    if provider == 'openrouter':
        cfg = _ConfigOverride(
            base,
            OPENROUTER_API_KEY=api_key or base.OPENROUTER_API_KEY,
            OPENROUTER_MODEL=model or base.OPENROUTER_MODEL,
        )
        return OpenRouterLLM(cfg)
    if provider == 'claude':
        cfg = _ConfigOverride(
            base,
            ANTHROPIC_API_KEY=api_key or base.ANTHROPIC_API_KEY,
            CLAUDE_MODEL=model or base.CLAUDE_MODEL,
        )
        return ClaudeLLM(cfg)
    if provider == 'gemini':
        cfg = _ConfigOverride(
            base,
            GEMINI_API_KEY=api_key or base.GEMINI_API_KEY,
            GEMINI_MODEL=model or base.GEMINI_MODEL,
        )
        return GeminiLLM(cfg)
    if provider in ('openai', 'custom', 'openai_compat'):
        default_url = (
            'https://api.openai.com/v1'
            if provider == 'openai' else base.OPENAI_COMPAT_BASE_URL
        )
        return OpenAICompatLLM(
            config=base,
            api_key=api_key or base.OPENAI_COMPAT_API_KEY,
            base_url=base_url or default_url,
            model=model or base.OPENAI_COMPAT_MODEL,
        )

    raise ValueError(f"Unknown task LLM provider: {provider}")

def _create_by_provider(provider: str, config: AgentConfig,
                        model_override: str = None) -> BaseLLM:
    """Создаёт LLM по имени провайдера."""
    provider = provider.lower()

    if provider == 'claude':
        llm = ClaudeLLM(config)
        if model_override:
            llm.model = model_override
        return llm
    elif provider == 'gemini':
        llm = GeminiLLM(config)
        if model_override:
            llm.model = model_override
        return llm
    elif provider == 'cloudru':
        llm = CloudRuLLM(config)
        if model_override:
            llm.model = model_override
        return llm
    elif provider == 'openrouter':
        llm = OpenRouterLLM(config)
        if model_override:
            llm.model = model_override
        return llm
    elif provider == 'deepseek':
        llm = DeepSeekLLM(config)
        if model_override:
            llm.model = model_override
        return llm
    elif provider == 'openai_compat':
        llm = OpenAICompatLLM(config)
        if model_override:
            llm.model = model_override
        return llm
    else:
        raise ValueError(
            f"Unknown LLM provider: {provider}. "
            f"Use 'claude', 'gemini', 'cloudru', 'openrouter', 'deepseek', or 'openai_compat'."
        )


def create_llm(config: AgentConfig = None) -> BaseLLM:
    """Создаёт основной LLM по конфигурации."""
    cfg = config or AgentConfig
    return _create_by_provider(cfg.LLM_PROVIDER, cfg)


def create_fallback_llm(config: AgentConfig = None) -> BaseLLM | None:
    """
    Создаёт fallback LLM для сложных агентов.

    Возвращает None если FALLBACK_LLM_PROVIDER не задан.
    Используется агентами с use_fallback_llm=True (auto-importer, card-doctor и др.)
    """
    cfg = config or AgentConfig
    fallback_provider = cfg.FALLBACK_LLM_PROVIDER

    if not fallback_provider:
        return None

    logger.info(f"Creating fallback LLM: {fallback_provider} / {cfg.FALLBACK_LLM_MODEL}")
    return _create_by_provider(fallback_provider, cfg, cfg.FALLBACK_LLM_MODEL or None)


def create_step_namer_llm(config: AgentConfig = None) -> BaseLLM | None:
    """
    Создаёт быструю модель для генерации креативных названий шагов.

    Возвращает None если STEP_NAMER_PROVIDER не задан.
    Использует самую дешёвую/быструю доступную модель.
    """
    cfg = config or AgentConfig
    provider = cfg.STEP_NAMER_PROVIDER

    if not provider:
        return None

    model = cfg.STEP_NAMER_MODEL
    logger.info(f"Creating step namer LLM: {provider} / {model}")
    return _create_by_provider(provider, cfg, model or None)
