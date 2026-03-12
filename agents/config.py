# -*- coding: utf-8 -*-
"""
Конфигурация агентного рантайма.

Переменные окружения:
  PLATFORM_URL        — URL платформы (default: http://localhost:5000)
  AGENT_ID            — ID агента в БД платформы
  AGENT_API_KEY       — API-ключ агента

  LLM_PROVIDER        — "gemini" | "claude" (default: claude)
  GEMINI_API_KEY       — ключ Google AI
  GEMINI_MODEL         — модель Gemini (default: gemini-2.0-flash)
  ANTHROPIC_API_KEY    — ключ Anthropic
  CLAUDE_MODEL         — модель Claude (default: claude-sonnet-4-20250514)

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

        if errors:
            raise ValueError(f"Agent config errors: {'; '.join(errors)}")
