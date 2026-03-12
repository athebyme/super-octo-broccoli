# -*- coding: utf-8 -*-
"""
Мульти-модельный LLM слой.

Поддерживает:
  - Google Gemini (через google-genai SDK)
  - Anthropic Claude (через anthropic SDK)

Унифицированный интерфейс: chat(), chat_with_tools(), structured_output()
"""
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from .config import AgentConfig

logger = logging.getLogger(__name__)


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


# ── Фабрика ───────────────────────────────────────────────────────

def create_llm(config: AgentConfig = None) -> BaseLLM:
    """Создаёт LLM по конфигурации."""
    cfg = config or AgentConfig
    provider = cfg.LLM_PROVIDER.lower()

    if provider == 'claude':
        return ClaudeLLM(cfg)
    elif provider == 'gemini':
        return GeminiLLM(cfg)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}. Use 'claude' or 'gemini'.")
