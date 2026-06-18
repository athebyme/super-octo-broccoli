# -*- coding: utf-8 -*-
"""
Центральный LLM-конфиг: читает ключи из SystemSettings (таблица system_settings).
Функция get_central_llm_config() возвращает единый конфиг для всей платформы
или None, если центральный ключ не настроен.
"""


def get_central_llm_config():
    """Возвращает центральный LLM-конфиг из SystemSettings или None.

    Приоритет: центральный ключ (agent_*) > per-seller ключ.
    Если провайдер не задан или API-ключ отсутствует/пуст — возвращает None,
    и вызывающий код должен использовать настройки продавца как запасной вариант.

    Returns:
        dict с полями {'provider', 'api_key', 'model', 'base_url'} или None.
    """
    try:
        # Отложенный импорт, чтобы не создавать циклических зависимостей
        # и не падать при запуске тестов без полного Flask-контекста
        from models import SystemSettings

        # Читаем провайдер из центральных настроек
        setting = SystemSettings.query.filter_by(key='agent_llm_provider').first()
        provider = setting.get_value() if setting else None

        # Без провайдера центральный конфиг не сформирован
        if not provider:
            return None

        # Определяем ключ БД для API-ключа по провайдеру
        if provider == 'openrouter':
            api_key_db_key = 'agent_openrouter_api_key'
        elif provider in ('claude', 'anthropic'):
            api_key_db_key = 'agent_anthropic_api_key'
        elif provider == 'gemini':
            api_key_db_key = 'agent_gemini_api_key'
        elif provider == 'cloudru':
            api_key_db_key = 'agent_llm_api_key'
        else:
            api_key_db_key = 'agent_llm_api_key'

        # Читаем API-ключ
        api_key_setting = SystemSettings.query.filter_by(key=api_key_db_key).first()
        api_key = api_key_setting.get_value() if api_key_setting else None

        # Пустой или отсутствующий ключ — центральный конфиг недоступен
        if not api_key:
            return None

        # Определяем ключ БД для модели по провайдеру
        if provider == 'openrouter':
            model_db_key = 'agent_openrouter_model'
        elif provider in ('claude', 'anthropic'):
            model_db_key = 'agent_claude_model'
        elif provider == 'gemini':
            model_db_key = 'agent_gemini_model'
        elif provider == 'cloudru':
            model_db_key = 'agent_llm_model'
        else:
            model_db_key = 'agent_llm_model'

        # Читаем модель (необязательное поле — может быть пустым)
        model_setting = SystemSettings.query.filter_by(key=model_db_key).first()
        model = model_setting.get_value() if model_setting else ''

        return {
            'provider': provider,    # строка, например 'openrouter'
            'api_key': api_key,      # непустая строка
            'model': model or '',    # может быть пустой строкой
            'base_url': '',          # потребитель использует дефолт провайдера
        }

    except Exception:
        # БД недоступна (тесты без контекста, миграции и т.д.) — игнорируем
        return None
