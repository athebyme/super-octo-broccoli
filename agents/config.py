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

  LLM_PROVIDER        — "cloudru" | "claude" | "gemini" | "openrouter" | "openai_compat" (default: cloudru)
  OPENROUTER_API_KEY   — ключ OpenRouter (для DeepSeek, Claude, Gemini и др.)
  OPENROUTER_MODEL     — модель OpenRouter (default: deepseek/deepseek-v3.2)
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
  AGENT_RUN_TOKEN_BUDGET — лимит input+output токенов на ReAct run (default: 30000)
  AGENT_RUN_API_BUDGET — максимум LLM API-вызовов одного запуска (default: 24)
  AGENT_OBSERVATION_MAX_CHARS — максимум символов одного tool result в контексте (default: 1200)
  AGENT_STEP_NAMER_ENABLED — разрешить отдельные LLM-вызовы для названий шагов (default: 0)
  LOG_LEVEL            — уровень логирования (default: INFO)
"""
import os
import logging

_logger = logging.getLogger(__name__)

# Remote config cache: загружается один раз при первом обращении
_remote_config: dict | None = None


def _load_remote_config() -> dict:
    """Загружает LLM-конфигурацию из платформы (один раз)."""
    global _remote_config
    if _remote_config is not None:
        return _remote_config

    _remote_config = {}

    # Нужны PLATFORM_URL, AGENT_ID, AGENT_API_KEY из env для подключения
    platform_url = os.getenv('PLATFORM_URL', 'http://localhost:5000')
    agent_id = os.getenv('AGENT_ID', '')
    agent_key = os.getenv('AGENT_API_KEY', '')

    if not agent_id or not agent_key:
        return _remote_config

    try:
        import requests
        # TLS verification: по умолчанию включена. Отключить можно только явно
        # через AGENT_TLS_INSECURE=1 для отладки в локальной среде с self-signed
        # сертификатами. В проде переменная не должна быть выставлена.
        tls_insecure = os.getenv('AGENT_TLS_INSECURE', '').lower() in ('1', 'true', 'yes')
        if tls_insecure:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            _logger.warning("AGENT_TLS_INSECURE=1 — TLS verification disabled (debug only!)")
        resp = requests.get(
            f'{platform_url.rstrip("/")}/internal/v1/config/llm',
            headers={
                'X-Agent-Id': agent_id,
                'X-Agent-Key': agent_key,
            },
            timeout=10,
            verify=not tls_insecure,
        )
        if resp.status_code == 200:
            _remote_config = resp.json().get('config', {})
            if _remote_config:
                _logger.info(
                    "Remote non-secret LLM config loaded: %s",
                    ', '.join(sorted(_remote_config)),
                )
    except Exception as e:
        _logger.debug(f"Remote config unavailable: {e}")

    return _remote_config


# ── Описание полей: attr_name → (env_var, default, type) ──────────
_FIELD_DEFS = {
    # Платформа
    'PLATFORM_URL':       ('PLATFORM_URL', 'http://localhost:5000', str),
    'AGENT_ID':           ('AGENT_ID', '', str),
    'AGENT_API_KEY':      ('AGENT_API_KEY', '', str),

    # LLM провайдер (основной)
    'LLM_PROVIDER':       ('LLM_PROVIDER', 'deepseek', str),

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

    # OpenRouter — единый API для 300+ моделей
    'OPENROUTER_API_KEY':  ('OPENROUTER_API_KEY', '', str),
    'OPENROUTER_MODEL':    ('OPENROUTER_MODEL', 'deepseek/deepseek-v3.2', str),

    # DeepSeek Platform — нативный API (api.deepseek.com)
    'DEEPSEEK_API_KEY':  ('DEEPSEEK_API_KEY', '', str),
    'DEEPSEEK_BASE_URL': ('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1', str),
    'DEEPSEEK_MODEL':    ('DEEPSEEK_MODEL', 'deepseek-v4-pro', str),

    # Универсальный OpenAI-совместимый провайдер (vLLM, Ollama, LM Studio, etc.)
    'OPENAI_COMPAT_API_KEY':  ('OPENAI_COMPAT_API_KEY', '', str),
    'OPENAI_COMPAT_BASE_URL': ('OPENAI_COMPAT_BASE_URL', 'http://localhost:8000/v1', str),
    'OPENAI_COMPAT_MODEL':    ('OPENAI_COMPAT_MODEL', '', str),

    # Fallback LLM (для сложных агентов)
    'FALLBACK_LLM_PROVIDER': ('FALLBACK_LLM_PROVIDER', '', str),
    'FALLBACK_LLM_MODEL':    ('FALLBACK_LLM_MODEL', 'claude-haiku-4-5-20251001', str),

    # Step Namer LLM (быстрая модель для генерации названий шагов в UI)
    'STEP_NAMER_PROVIDER': ('STEP_NAMER_PROVIDER', '', str),
    'STEP_NAMER_MODEL':    ('STEP_NAMER_MODEL', 'gemini-2.0-flash', str),

    # Рантайм
    'POLL_INTERVAL':      ('AGENT_POLL_INTERVAL', '5', int),
    'HEARTBEAT_INTERVAL': ('AGENT_HEARTBEAT_INTERVAL', '30', int),
    'RUN_TOKEN_BUDGET':   ('AGENT_RUN_TOKEN_BUDGET', '30000', int),
    'RUN_API_BUDGET':     ('AGENT_RUN_API_BUDGET', '24', int),
    'OBSERVATION_MAX_CHARS': ('AGENT_OBSERVATION_MAX_CHARS', '1200', int),
    'MAX_PRODUCTS_PER_RUN': ('AGENT_MAX_PRODUCTS_PER_RUN', '200', int),
    'STEP_NAMER_ENABLED': ('AGENT_STEP_NAMER_ENABLED', '0', int),
    'LOG_LEVEL':          ('LOG_LEVEL', 'INFO', str),

    # LLM параметры
    'MAX_TOKENS':         ('LLM_MAX_TOKENS', '4096', int),
    'TEMPERATURE':        ('LLM_TEMPERATURE', '0.3', float),

    # Batch обработка
    'BATCH_STRUCTURED_CHUNK_SIZE': ('BATCH_STRUCTURED_CHUNK_SIZE', '25', int),
    'BATCH_TOOL_CHUNK_SIZE':       ('BATCH_TOOL_CHUNK_SIZE', '15', int),
    'BATCH_MAX_WORKERS':           ('BATCH_MAX_WORKERS', '3', int),

    # Безопасность
    'PLATFORM_SKIP_TLS_VERIFY': ('PLATFORM_SKIP_TLS_VERIFY', '1', int),  # 1=skip (Docker default), 0=verify
}


def _resolve(name: str):
    """Читает значение поля: remote config → os.environ → default."""
    field = _FIELD_DEFS.get(name)
    if field is None:
        raise AttributeError(f"AgentConfig has no field '{name}'")
    env_var, default, typ = field

    # 1. Remote config из платформы (приоритет)
    remote = _load_remote_config()
    if env_var in remote and remote[env_var]:
        return typ(remote[env_var])

    # 2. Переменная окружения
    # 3. Default
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
    def reload_remote_config(cls):
        """Сбрасывает кэш remote config — при следующем обращении будет перезагружен."""
        global _remote_config
        _remote_config = None

    @classmethod
    def validate(cls):
        """Проверяет обязательные настройки."""
        errors = []
        if not _resolve('AGENT_ID'):
            errors.append('AGENT_ID not set')
        if not _resolve('AGENT_API_KEY'):
            errors.append('AGENT_API_KEY not set')

        if errors:
            raise ValueError(f"Agent config errors: {'; '.join(errors)}")
