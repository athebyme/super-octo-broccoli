# -*- coding: utf-8 -*-
"""Тесты нативного DeepSeek-провайдера (api.deepseek.com, OpenAI-совместимый).

Центральный конфиг, базовые URL/модели, приоритет per-seller модели
при совпадении провайдера с центральным, каталог моделей для UI.
"""

import unittest

from flask import Flask

from models import db, SystemSettings, AutoImportSettings
from services.llm_config import get_central_llm_config
from services.ai_service import AIConfig, AIProvider, get_available_models

DEEPSEEK_BASE = 'https://api.deepseek.com/v1'


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _add_central_deepseek(model=''):
    db.session.add(SystemSettings(key='agent_llm_provider', value='deepseek', value_type='string'))
    db.session.add(SystemSettings(key='agent_deepseek_api_key', value='sk-central', value_type='string'))
    if model:
        db.session.add(SystemSettings(key='agent_deepseek_model', value=model, value_type='string'))
    db.session.commit()


class DeepSeekDBTestCase(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()


class TestCentralConfigDeepSeek(DeepSeekDBTestCase):
    def test_reads_deepseek_key_and_model(self):
        _add_central_deepseek(model='deepseek-v4-flash')
        result = get_central_llm_config()
        self.assertIsNotNone(result)
        self.assertEqual(result['provider'], 'deepseek')
        self.assertEqual(result['api_key'], 'sk-central')
        self.assertEqual(result['model'], 'deepseek-v4-flash')

    def test_missing_key_returns_none(self):
        db.session.add(SystemSettings(key='agent_llm_provider', value='deepseek', value_type='string'))
        db.session.commit()
        self.assertIsNone(get_central_llm_config())


class TestCentralProviderBaseModel(unittest.TestCase):
    def test_deepseek_mapping(self):
        central = {'provider': 'deepseek', 'api_key': 'sk-x', 'model': '', 'base_url': ''}
        provider, base_url, default_model = AIConfig._central_provider_base_model(central)
        self.assertEqual(provider, AIProvider.DEEPSEEK)
        self.assertEqual(base_url, DEEPSEEK_BASE)
        self.assertEqual(default_model, 'deepseek-v4-pro')


class FakeSettings:
    ai_enabled = True
    ai_provider = 'deepseek'
    ai_api_key = 'per-seller-key'
    ai_api_base_url = ''
    ai_model = 'deepseek-v4-flash'
    ai_temperature = 0.3
    ai_max_tokens = 2000
    ai_timeout = 120
    ai_top_p = 0.95
    ai_presence_penalty = 0.0
    ai_frequency_penalty = 0.0


class TestFromSettingsDeepSeek(DeepSeekDBTestCase):
    def test_per_seller_deepseek_without_central(self):
        config = AIConfig.from_settings(FakeSettings())
        self.assertIsNotNone(config)
        self.assertEqual(config.provider, AIProvider.DEEPSEEK)
        self.assertEqual(config.api_base_url, DEEPSEEK_BASE)
        self.assertEqual(config.model, 'deepseek-v4-flash')

    def test_per_seller_deepseek_default_model(self):
        class NoModel(FakeSettings):
            ai_model = ''
        config = AIConfig.from_settings(NoModel())
        self.assertEqual(config.model, 'deepseek-v4-pro')

    def test_central_active_seller_same_provider_keeps_seller_model(self):
        """Центральный deepseek + продавец выбрал flash того же провайдера → flash."""
        _add_central_deepseek()
        config = AIConfig.from_settings(FakeSettings())
        self.assertEqual(config.api_key, 'sk-central')
        self.assertEqual(config.model, 'deepseek-v4-flash')

    def test_central_active_seller_other_provider_uses_central_model(self):
        """Модель продавца от ДРУГОГО провайдера не протекает в central-конфиг."""
        _add_central_deepseek()

        class CloudruSettings(FakeSettings):
            ai_provider = 'cloudru'
            ai_model = 'openai/gpt-oss-120b'

        config = AIConfig.from_settings(CloudruSettings())
        self.assertEqual(config.provider, AIProvider.DEEPSEEK)
        self.assertEqual(config.model, 'deepseek-v4-pro')


class TestForSellerDeepSeek(DeepSeekDBTestCase):
    def _seller_settings(self, provider='deepseek', model='deepseek-v4-flash'):
        s = AutoImportSettings(seller_id=1, ai_provider=provider,
                               ai_api_key='per-seller-key', ai_model=model)
        db.session.add(s)
        db.session.commit()

    def test_central_active_respects_matching_seller_model(self):
        _add_central_deepseek()
        self._seller_settings()
        config = AIConfig.for_seller(1)
        self.assertEqual(config.api_key, 'sk-central')
        self.assertEqual(config.model, 'deepseek-v4-flash')
        self.assertEqual(config.api_base_url, DEEPSEEK_BASE)

    def test_central_active_ignores_foreign_seller_model(self):
        _add_central_deepseek()
        self._seller_settings(provider='cloudru', model='openai/gpt-oss-120b')
        config = AIConfig.for_seller(1)
        self.assertEqual(config.model, 'deepseek-v4-pro')

    def test_model_override_beats_seller_model(self):
        _add_central_deepseek()
        self._seller_settings()
        config = AIConfig.for_seller(1, model_override='deepseek-v4-pro')
        self.assertEqual(config.model, 'deepseek-v4-pro')

    def test_per_seller_deepseek_without_central(self):
        self._seller_settings()
        config = AIConfig.for_seller(1)
        self.assertEqual(config.provider, AIProvider.DEEPSEEK)
        self.assertEqual(config.api_key, 'per-seller-key')
        self.assertEqual(config.api_base_url, DEEPSEEK_BASE)


class TestModelCatalog(unittest.TestCase):
    def test_get_available_models_deepseek(self):
        models = get_available_models('deepseek')
        self.assertIn('deepseek-v4-pro', models)
        self.assertIn('deepseek-v4-flash', models)

    def test_internal_runtime_config_exposes_model_but_not_credentials(self):
        from routes.internal_api import _LLM_CONFIG_KEYS
        self.assertNotIn('deepseek_api_key', _LLM_CONFIG_KEYS)
        self.assertEqual(_LLM_CONFIG_KEYS.get('deepseek_model'), 'DEEPSEEK_MODEL')


if __name__ == '__main__':
    unittest.main()
