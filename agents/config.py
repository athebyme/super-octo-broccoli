# -*- coding: utf-8 -*-
"""
Конфигурация агентного рантайма.

Переменные окружения:
  PLATFORM_URL        — URL платформы (default: http://localhost:5000)
  AGENT_ID            — ID агента в БД платформы
  AGENT_API_KEY       — API-ключ агента

  LLM_PROVIDER        — "gemini" | "claude" | "cloudru" | "openai_compat" (default: claude)
  GEMINI_API_KEY       — ключ Google AI
  GEMINI_MODEL         — модель Gemini (default: gemini-2.0-flash)
  ANTHROPIC_API_KEY    — ключ Anthropic
  CLAUDE_MODEL         — модель Claude (default: claude-sonnet-4-20250514)
  CLOUDRU_API_KEY      — ключ Cloud.ru Foundation Models
  CLOUDRU_MODEL        — модель Cloud.ru (default: deepseek-ai/DeepSeek-V3)
  CLOUDRU_BASE_URL     — base URL Cloud.ru API
  OPENAI_COMPAT_API_KEY — ключ для OpenAI-совместимого API
  OPENAI_COMPAT_BASE_URL — base URL (для любого OAI-compatible провайдера)
  OPENAI_COMPAT_MODEL  — модель

  AGENT_POLL_INTERVAL  — интервал опроса задач, сек (default: 5)
  AGENT_HEARTBEAT_INTERVAL — интервал heartbeat, сек (default: 30)
  LOG_LEVEL            — уровень логирования (default: INFO)
"""
import os


class AgentConfig:
    """Настройки одного экземпляра агента."""

    # ── Платформа ──────────────────────────────────────────────────
    PLATFORM_URL: str = os.getenv('PLATFORM_URL', 'http://localhost:5000')
    AGENT_ID: str = os.getenv('AGENT_ID', '')
    AGENT_API_KEY: str = os.getenv('AGENT_API_KEY', '')

    # ── LLM провайдер ─────────────────────────────────────────────
    LLM_PROVIDER: str = os.getenv('LLM_PROVIDER', 'claude')

    # Google Gemini
    GEMINI_API_KEY: str = os.getenv('GEMINI_API_KEY', '')
    GEMINI_MODEL: str = os.getenv('GEMINI_MODEL', 'gemini-2.0-flash')

    # Anthropic Claude
    ANTHROPIC_API_KEY: str = os.getenv('ANTHROPIC_API_KEY', '')
    CLAUDE_MODEL: str = os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-20250514')

    # Cloud.ru Foundation Models (OpenAI-compatible)
    CLOUDRU_API_KEY: str = os.getenv('CLOUDRU_API_KEY', '')
    CLOUDRU_BASE_URL: str = os.getenv('CLOUDRU_BASE_URL', 'https://api.foundation-models.ru/v1')
    CLOUDRU_MODEL: str = os.getenv('CLOUDRU_MODEL', 'deepseek-ai/DeepSeek-V3')

    # Универсальный OpenAI-совместимый провайдер (vLLM, Ollama, LM Studio, etc.)
    OPENAI_COMPAT_API_KEY: str = os.getenv('OPENAI_COMPAT_API_KEY', '')
    OPENAI_COMPAT_BASE_URL: str = os.getenv('OPENAI_COMPAT_BASE_URL', 'http://localhost:8000/v1')
    OPENAI_COMPAT_MODEL: str = os.getenv('OPENAI_COMPAT_MODEL', '')

    # ── Рантайм ───────────────────────────────────────────────────
    POLL_INTERVAL: int = int(os.getenv('AGENT_POLL_INTERVAL', '5'))
    HEARTBEAT_INTERVAL: int = int(os.getenv('AGENT_HEARTBEAT_INTERVAL', '30'))
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')

    # ── LLM параметры ─────────────────────────────────────────────
    MAX_TOKENS: int = int(os.getenv('LLM_MAX_TOKENS', '4096'))
    TEMPERATURE: float = float(os.getenv('LLM_TEMPERATURE', '0.3'))

    @classmethod
    def validate(cls):
        """Проверяет обязательные настройки."""
        errors = []
        if not cls.AGENT_ID:
            errors.append('AGENT_ID not set')
        if not cls.AGENT_API_KEY:
            errors.append('AGENT_API_KEY not set')

        if cls.LLM_PROVIDER == 'gemini' and not cls.GEMINI_API_KEY:
            errors.append('GEMINI_API_KEY not set (provider=gemini)')
        if cls.LLM_PROVIDER == 'claude' and not cls.ANTHROPIC_API_KEY:
            errors.append('ANTHROPIC_API_KEY not set (provider=claude)')
        if cls.LLM_PROVIDER == 'cloudru' and not cls.CLOUDRU_API_KEY:
            errors.append('CLOUDRU_API_KEY not set (provider=cloudru)')
        if cls.LLM_PROVIDER == 'openai_compat' and not cls.OPENAI_COMPAT_BASE_URL:
            errors.append('OPENAI_COMPAT_BASE_URL not set (provider=openai_compat)')

        if errors:
            raise ValueError(f"Agent config errors: {'; '.join(errors)}")
