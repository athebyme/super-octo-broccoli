# -*- coding: utf-8 -*-
"""
Мульти-модельный LLM слой.

Поддерживает:
  - Google Gemini (через google-genai SDK)
  - Anthropic Claude (через anthropic SDK)
  - Cloud.ru Foundation Models (OpenAI-compatible API)
  - Любой OpenAI-совместимый API (vLLM, Ollama, LM Studio, etc.)

Унифицированный интерфейс: chat(), chat_with_tools(), structured_output()
"""
import functools
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from .config import AgentConfig

logger = logging.getLogger(__name__)


# ── Retry-декоратор для LLM-вызовов ──────────────────────────────

def llm_retry(max_retries: int = 3, base_delay: float = 2.0):
    """
    Retry с экспоненциальным backoff для LLM-вызовов.

    Перехватывает сетевые ошибки, rate-limit (429), server errors (502/503/529).
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (ConnectionError, TimeoutError, OSError) as e:
                    last_error = e
                except Exception as e:
                    err_str = str(e).lower()
                    status = getattr(getattr(e, 'response', None), 'status_code', 0) or 0
                    is_retryable = (
                        status in (429, 502, 503, 529)
                        or 'rate' in err_str
                        or 'overloaded' in err_str
                        or 'timeout' in err_str
                    )
                    if not is_retryable or attempt >= max_retries:
                        raise
                    last_error = e

                wait = base_delay * (2 ** attempt)
                logger.warning(
                    f"LLM call failed (attempt {attempt+1}/{max_retries+1}), "
                    f"retry in {wait:.0f}s: {last_error}"
                )
                time.sleep(wait)
            raise last_error
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
        }
        """
        ...

    @abstractmethod
    def structured_output(self, system: str, prompt: str,
                          schema: dict) -> dict:
        """Возвращает JSON по заданной схеме."""
        ...


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

        return {
            'text': '\n'.join(text_parts),
            'tool_calls': tool_calls,
            'stop_reason': resp.stop_reason,
        }

    def structured_output(self, system: str, prompt: str,
                          schema: dict) -> dict:
        schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
        full_prompt = (
            f"{prompt}\n\n"
            f"Ответь СТРОГО в JSON формате по схеме:\n```json\n{schema_str}\n```\n"
            f"Без комментариев, только валидный JSON."
        )
        text = self.chat(system, [{'role': 'user', 'content': full_prompt}])

        # Извлекаем JSON из ответа
        text = text.strip()
        if text.startswith('```'):
            lines = text.split('\n')
            text = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])
        return json.loads(text)


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

        stop = resp.candidates[0].finish_reason
        stop_reason = 'tool_use' if tool_calls else 'end_turn'

        return {
            'text': '\n'.join(text_parts),
            'tool_calls': tool_calls,
            'stop_reason': stop_reason,
        }

    def structured_output(self, system: str, prompt: str,
                          schema: dict) -> dict:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.cfg.TEMPERATURE,
            max_output_tokens=self.cfg.MAX_TOKENS,
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

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        logger.info(f"OpenAI-compat LLM initialized: {self.model} @ {self.base_url}")

    def _check_api_error(self, error: Exception):
        """Проверяет, не вернул ли API HTML вместо JSON (типичная ошибка Cloud.ru 404)."""
        err_str = str(error)
        # OpenAI SDK выбрасывает APIConnectionError или APIStatusError с HTML в body
        if any(marker in err_str for marker in ['<!DOCTYPE', '<html', '<!doctype', 'Ошибка 404', 'Page not found']):
            raise LLMProviderError(
                f"LLM API вернул HTML вместо JSON. "
                f"Проверьте CLOUDRU_BASE_URL ({self.base_url}) и CLOUDRU_MODEL ({self.model}). "
                f"Текущий URL может быть некорректным — API возвращает веб-страницу с ошибкой 404."
            ) from error
        if 'Connection error' in err_str or 'connection' in err_str.lower():
            raise LLMProviderError(
                f"Не удалось подключиться к LLM API: {self.base_url}. "
                f"Проверьте CLOUDRU_BASE_URL и доступность сервера."
            ) from error

    @llm_retry()
    def chat(self, system: str, messages: list[dict],
             temperature: float = None, max_tokens: int = None) -> str:
        oai_messages = [{'role': 'system', 'content': system}]
        oai_messages.extend(messages)

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=oai_messages,
                temperature=temperature if temperature is not None else self.cfg.TEMPERATURE,
                max_tokens=max_tokens or self.cfg.MAX_TOKENS,
            )
        except Exception as e:
            self._check_api_error(e)
            raise
        return resp.choices[0].message.content or ''

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
        return {
            'text': text,
            'tool_calls': tool_calls,
            'stop_reason': stop_reason,
        }

    def structured_output(self, system: str, prompt: str,
                          schema: dict) -> dict:
        schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
        full_prompt = (
            f"{prompt}\n\n"
            f"Ответь СТРОГО в JSON формате по схеме:\n```json\n{schema_str}\n```\n"
            f"Без комментариев, только валидный JSON."
        )
        text = self.chat(system, [{'role': 'user', 'content': full_prompt}])
        text = text.strip()
        if text.startswith('```'):
            lines = text.split('\n')
            text = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])
        return json.loads(text)


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


# ── Фабрика ───────────────────────────────────────────────────────

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
    elif provider == 'openai_compat':
        llm = OpenAICompatLLM(config)
        if model_override:
            llm.model = model_override
        return llm
    else:
        raise ValueError(
            f"Unknown LLM provider: {provider}. "
            f"Use 'claude', 'gemini', 'cloudru', or 'openai_compat'."
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
