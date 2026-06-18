# -*- coding: utf-8 -*-
"""Тесты для центрального LLM-ключа: get_central_llm_config + AIConfig.from_settings."""

import unittest

from flask import Flask

from models import db, SystemSettings
from services.llm_config import get_central_llm_config
from services.ai_service import AIConfig, AIProvider


def _make_app():
    """Создаём Flask-приложение с in-memory SQLite для тестов."""
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


class FakeSettings:
    """Фиктивные настройки продавца (без записи в БД)."""
    ai_enabled = True
    ai_provider = 'cloudru'
    ai_api_key = 'per-seller-key'
    ai_api_base_url = ''
    ai_model = 'per-seller-model'
    # Необязательные атрибуты, которые AIConfig.from_settings читает через getattr
    ai_temperature = 0.3
    ai_max_tokens = 2000
    ai_timeout = 120
    ai_top_p = 0.95
    ai_presence_penalty = 0.0
    ai_frequency_penalty = 0.0


class TestGetCentralLlmConfig(unittest.TestCase):
    """Тесты функции get_central_llm_config."""

    def setUp(self):
        """Инициализируем приложение и создаём таблицы."""
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        """Очищаем сессию и удаляем таблицы."""
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_returns_none_when_no_settings(self):
        """Без строк в SystemSettings → None."""
        result = get_central_llm_config()
        self.assertIsNone(result)

    def test_returns_none_when_provider_missing_key(self):
        """Есть provider='openrouter', но api_key отсутствует → None."""
        db.session.add(SystemSettings(key='agent_llm_provider', value='openrouter', value_type='string'))
        db.session.commit()
        result = get_central_llm_config()
        self.assertIsNone(result)

    def test_returns_config_for_openrouter(self):
        """provider=openrouter + api_key sk-x → dict с правильными полями."""
        db.session.add(SystemSettings(key='agent_llm_provider', value='openrouter', value_type='string'))
        db.session.add(SystemSettings(key='agent_openrouter_api_key', value='sk-x', value_type='string'))
        db.session.commit()
        result = get_central_llm_config()
        self.assertIsNotNone(result)
        self.assertEqual(result['provider'], 'openrouter')
        self.assertEqual(result['api_key'], 'sk-x')
        self.assertIn('model', result)
        self.assertIn('base_url', result)

    def test_returns_config_with_model_when_set(self):
        """Если задана модель — она попадает в результат."""
        db.session.add(SystemSettings(key='agent_llm_provider', value='openrouter', value_type='string'))
        db.session.add(SystemSettings(key='agent_openrouter_api_key', value='sk-y', value_type='string'))
        db.session.add(SystemSettings(key='agent_openrouter_model', value='anthropic/claude-3.5-sonnet', value_type='string'))
        db.session.commit()
        result = get_central_llm_config()
        self.assertEqual(result['model'], 'anthropic/claude-3.5-sonnet')

    def test_cloudru_uses_llm_api_key(self):
        """Для cloudru ключ берётся из agent_llm_api_key."""
        db.session.add(SystemSettings(key='agent_llm_provider', value='cloudru', value_type='string'))
        db.session.add(SystemSettings(key='agent_llm_api_key', value='cloud-token', value_type='string'))
        db.session.commit()
        result = get_central_llm_config()
        self.assertIsNotNone(result)
        self.assertEqual(result['api_key'], 'cloud-token')

    def test_empty_api_key_returns_none(self):
        """Пустой api_key → None (как будто ключа нет)."""
        db.session.add(SystemSettings(key='agent_llm_provider', value='openrouter', value_type='string'))
        db.session.add(SystemSettings(key='agent_openrouter_api_key', value='', value_type='string'))
        db.session.commit()
        result = get_central_llm_config()
        self.assertIsNone(result)


class TestAIConfigFromSettingsCentralPriority(unittest.TestCase):
    """Тесты приоритета центрального LLM-ключа в AIConfig.from_settings."""

    def setUp(self):
        """Инициализируем приложение и создаём таблицы."""
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        """Очищаем сессию и удаляем таблицы."""
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_central_key_overrides_per_seller(self):
        """Центральный ключ есть → config.api_key == центральный, provider == OPENROUTER."""
        db.session.add(SystemSettings(key='agent_llm_provider', value='openrouter', value_type='string'))
        db.session.add(SystemSettings(key='agent_openrouter_api_key', value='central-sk', value_type='string'))
        db.session.commit()

        config = AIConfig.from_settings(FakeSettings())
        self.assertIsNotNone(config)
        self.assertEqual(config.api_key, 'central-sk')
        self.assertEqual(config.provider, AIProvider.OPENROUTER)

    def test_fallback_to_per_seller_when_central_absent(self):
        """Нет центральных настроек → используется per-seller ключ."""
        config = AIConfig.from_settings(FakeSettings())
        self.assertIsNotNone(config)
        self.assertEqual(config.api_key, 'per-seller-key')

    def test_fallback_to_per_seller_when_central_key_empty(self):
        """Центральный провайдер есть, но api_key пуст → per-seller."""
        db.session.add(SystemSettings(key='agent_llm_provider', value='openrouter', value_type='string'))
        db.session.add(SystemSettings(key='agent_openrouter_api_key', value='', value_type='string'))
        db.session.commit()

        config = AIConfig.from_settings(FakeSettings())
        self.assertIsNotNone(config)
        self.assertEqual(config.api_key, 'per-seller-key')

    def test_from_settings_returns_none_when_ai_disabled(self):
        """ai_enabled=False → None (существующее поведение не изменено)."""
        class DisabledSettings(FakeSettings):
            ai_enabled = False

        config = AIConfig.from_settings(DisabledSettings())
        self.assertIsNone(config)


if __name__ == '__main__':
    unittest.main()
