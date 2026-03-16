# -*- coding: utf-8 -*-
"""
Конфигурация агентного рантайма.

Все значения читаются из os.environ ЛЕНИВО — при первом обращении,
а не при импорте модуля. Это позволяет вызвать load_dotenv() до
обращения к полям конфига.

Переменные окружения:
  PLATFORM_URL        — URL платформы (default: http://localhost:5000)
  AGENT_ID            — ID агента в БД платформы
  AGENT_API_KEY       — API-ключ агента

  LLM_PROVIDER        — "cloudru" | "claude" | "gemini" | "openai_compat" (default: cloudru)
  CLOUDRU_API_KEY      — ключ Cloud.ru Foundation Models
  CLOUDRU_MODEL        — модель Cloud.ru (default: openai/gpt-oss-120b)
  CLOUDRU_BASE_URL     — base URL Cloud.ru API
  ANTHROPIC_API_KEY    — ключ Anthropic
  CLAUDE_MODEL         — модель Claude (default: claude-sonnet-4-20250514)
  GEMINI_API_KEY       — ключ Google AI
  GEMINI_MODEL         — модель Gemini (default: gemini-2.0-flash)
  OPENAI_COMPAT_API_KEY — ключ для OpenAI-совместимого API
  OPENAI_COMPAT_BASE_URL — base URL (для любого OAI-compatible провайдера)
  OPENAI_COMPAT_MODEL  — модель

  FALLBACK_LLM_PROVIDER — провайдер для сложных задач (default: "" — отключён)
  FALLBACK_LLM_MODEL    — модель fallback-провайдера (default: claude-haiku-4-5-20251001)

  AGENT_POLL_INTERVAL  — интервал опроса задач, сек (default: 5)
  AGENT_HEARTBEAT_INTERVAL — интервал heartbeat, сек (default: 30)
  LOG_LEVEL            — уровень логирования (default: INFO)
"""
import os


# ── Описание полей: attr_name → (env_var, default, type) ──────────
_FIELD_DEFS = {
    # Платформа
    'PLATFORM_URL':       ('PLATFORM_URL', 'http://localhost:5000', str),
    'AGENT_ID':           ('AGENT_ID', '', str),
    'AGENT_API_KEY':      ('AGENT_API_KEY', '', str),

    # LLM провайдер (основной)
    'LLM_PROVIDER':       ('LLM_PROVIDER', 'cloudru', str),

    # Cloud.ru Foundation Models (OpenAI-compatible) — основной провайдер
    'CLOUDRU_API_KEY':    ('CLOUDRU_API_KEY', '', str),
    'CLOUDRU_BASE_URL':   ('CLOUDRU_BASE_URL', 'https://foundation-models.api.cloud.ru/v1/', str),
    'CLOUDRU_MODEL':      ('CLOUDRU_MODEL', 'openai/gpt-oss-120b', str),

    # Anthropic Claude — для сложных задач (fallback)
    'ANTHROPIC_API_KEY':  ('ANTHROPIC_API_KEY', '', str),
    'CLAUDE_MODEL':       ('CLAUDE_MODEL', 'claude-sonnet-4-20250514', str),

    # Google Gemini
    'GEMINI_API_KEY':     ('GEMINI_API_KEY', '', str),
    'GEMINI_MODEL':       ('GEMINI_MODEL', 'gemini-2.0-flash', str),

    # Универсальный OpenAI-совместимый провайдер (vLLM, Ollama, LM Studio, etc.)
    'OPENAI_COMPAT_API_KEY':  ('OPENAI_COMPAT_API_KEY', '', str),
    'OPENAI_COMPAT_BASE_URL': ('OPENAI_COMPAT_BASE_URL', 'http://localhost:8000/v1', str),
    'OPENAI_COMPAT_MODEL':    ('OPENAI_COMPAT_MODEL', '', str),

    # Fallback LLM (для сложных агентов)
    'FALLBACK_LLM_PROVIDER': ('FALLBACK_LLM_PROVIDER', '', str),
    'FALLBACK_LLM_MODEL':    ('FALLBACK_LLM_MODEL', 'claude-haiku-4-5-20251001', str),

    # Рантайм
    'POLL_INTERVAL':      ('AGENT_POLL_INTERVAL', '5', int),
    'HEARTBEAT_INTERVAL': ('AGENT_HEARTBEAT_INTERVAL', '30', int),
    'LOG_LEVEL':          ('LOG_LEVEL', 'INFO', str),

    # LLM параметры
    'MAX_TOKENS':         ('LLM_MAX_TOKENS', '4096', int),
    'TEMPERATURE':        ('LLM_TEMPERATURE', '0.3', float),

    # Безопасность
    'PLATFORM_SKIP_TLS_VERIFY': ('PLATFORM_SKIP_TLS_VERIFY', '1', int),  # 1=skip (Docker default), 0=verify
}


def _resolve(name: str):
    """Читает значение поля из os.environ на момент вызова (ленивое чтение)."""
    field = _FIELD_DEFS.get(name)
    if field is None:
        raise AttributeError(f"AgentConfig has no field '{name}'")
    env_var, default, typ = field
    return typ(os.getenv(env_var, default))


class _AgentConfigMeta(type):
    """Метакласс: перехватывает AgentConfig.FIELD без создания экземпляра."""

    def __getattr__(cls, name: str):
        return _resolve(name)


class AgentConfig(metaclass=_AgentConfigMeta):
    """
    Настройки агента. Все поля читаются из os.environ лениво.

    Можно использовать как класс (AgentConfig.FIELD) или как экземпляр.
    """

    def __getattr__(self, name: str):
        return _resolve(name)

    @classmethod
    def validate(cls):
        """Проверяет обязательные настройки."""
        errors = []
        if not _resolve('AGENT_ID'):
            errors.append('AGENT_ID not set')
        if not _resolve('AGENT_API_KEY'):
            errors.append('AGENT_API_KEY not set')

        provider = _resolve('LLM_PROVIDER')
        if provider == 'gemini' and not _resolve('GEMINI_API_KEY'):
            errors.append('GEMINI_API_KEY not set (provider=gemini)')
        if provider == 'claude' and not _resolve('ANTHROPIC_API_KEY'):
            errors.append('ANTHROPIC_API_KEY not set (provider=claude)')
        if provider == 'cloudru' and not _resolve('CLOUDRU_API_KEY'):
            errors.append('CLOUDRU_API_KEY not set (provider=cloudru)')
        if provider == 'openai_compat' and not _resolve('OPENAI_COMPAT_BASE_URL'):
            errors.append('OPENAI_COMPAT_BASE_URL not set (provider=openai_compat)')

        if errors:
            raise ValueError(f"Agent config errors: {'; '.join(errors)}")
