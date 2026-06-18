# -*- coding: utf-8 -*-
"""
Тесты B2: унификация центрального LLM-ключа для reviews (AIConfig.for_seller)
и генерации изображений (ImageGenerationConfig.from_settings).

Структура:
  TestForSellerCentral   — reviews проходит через AIConfig.for_seller,
                           который теперь учитывает центральный ключ
  TestImageGenCentral    — ImageGenerationConfig.from_settings использует
                           центральный openrouter-ключ вместо per-supplier
"""

import unittest
from types import SimpleNamespace

from flask import Flask

from models import db, SystemSettings, User, Seller, AutoImportSettings
from services.llm_config import get_central_llm_config
from services.ai_service import AIConfig, AIProvider
from services.image_generation_service import ImageGenerationConfig, ImageProvider


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_app():
    """Создаём Flask-приложение с in-memory SQLite для изолированных тестов."""
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _seed_central(provider: str, api_key: str, model: str = ''):
    """Записываем центральный LLM-конфиг в SystemSettings."""
    db.session.add(SystemSettings(
        key='agent_llm_provider', value=provider, value_type='string',
    ))
    if provider == 'openrouter':
        db.session.add(SystemSettings(
            key='agent_openrouter_api_key', value=api_key, value_type='string',
        ))
        if model:
            db.session.add(SystemSettings(
                key='agent_openrouter_model', value=model, value_type='string',
            ))
    elif provider in ('claude', 'anthropic'):
        db.session.add(SystemSettings(
            key='agent_anthropic_api_key', value=api_key, value_type='string',
        ))
    elif provider == 'cloudru':
        db.session.add(SystemSettings(
            key='agent_llm_api_key', value=api_key, value_type='string',
        ))
    db.session.commit()


_seller_counter = 0


def _seed_seller_with_ai(ai_provider='openrouter',
                          ai_api_key='per-seller-key',
                          ai_model='per-seller-model'):
    """Создаёт User → Seller → AutoImportSettings в тестовой БД."""
    global _seller_counter
    _seller_counter += 1
    suffix = _seller_counter
    user = User(
        username=f'user{suffix}',
        email=f'user{suffix}@test.com',
        password_hash='x',
        is_active=True,
    )
    db.session.add(user)
    db.session.flush()

    seller = Seller(user_id=user.id, company_name=f'SellerCo{suffix}')
    db.session.add(seller)
    db.session.flush()

    ai_settings = AutoImportSettings(
        seller_id=seller.id,
        ai_enabled=True,
        ai_provider=ai_provider,
        ai_api_key=ai_api_key,
        ai_model=ai_model,
    )
    db.session.add(ai_settings)
    db.session.commit()
    return seller


# ---------------------------------------------------------------------------
# Тесты AIConfig.for_seller (путь reviews)
# ---------------------------------------------------------------------------

class TestForSellerCentral(unittest.TestCase):
    """AIConfig.for_seller теперь учитывает центральный LLM-ключ.

    reviews.py вызывает AIConfig.for_seller(seller_id=..., temperature=0.7, max_tokens=500),
    поэтому все тесты идут через этот метод.
    """

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_central_key_overrides_per_seller(self):
        """Если центральный ключ задан → for_seller возвращает его, а не per-seller."""
        # Засеваем центральный ключ
        _seed_central('openrouter', 'central-openrouter-sk')
        # Засеваем per-seller (должен быть проигнорирован)
        seller = _seed_seller_with_ai(ai_provider='openrouter', ai_api_key='per-seller-key')

        config = AIConfig.for_seller(seller_id=seller.id, temperature=0.7, max_tokens=500)

        self.assertIsNotNone(config)
        self.assertEqual(config.api_key, 'central-openrouter-sk')
        self.assertEqual(config.provider, AIProvider.OPENROUTER)
        # temperature и max_tokens переданы явно — должны учитываться
        self.assertAlmostEqual(config.temperature, 0.7)
        self.assertEqual(config.max_tokens, 500)

    def test_fallback_to_per_seller_when_central_absent(self):
        """Нет центрального ключа → for_seller использует per-seller настройки."""
        seller = _seed_seller_with_ai(
            ai_provider='openrouter',
            ai_api_key='per-seller-key',
            ai_model='per-seller-model',
        )

        config = AIConfig.for_seller(seller_id=seller.id)

        self.assertIsNotNone(config)
        self.assertEqual(config.api_key, 'per-seller-key')
        self.assertEqual(config.provider, AIProvider.OPENROUTER)

    def test_fallback_when_central_key_empty(self):
        """Центральный провайдер есть, но api_key пуст → per-seller."""
        # Записываем провайдера без ключа
        db.session.add(SystemSettings(
            key='agent_llm_provider', value='openrouter', value_type='string',
        ))
        db.session.add(SystemSettings(
            key='agent_openrouter_api_key', value='', value_type='string',
        ))
        db.session.commit()
        seller = _seed_seller_with_ai(ai_api_key='per-seller-fallback')

        config = AIConfig.for_seller(seller_id=seller.id)

        self.assertEqual(config.api_key, 'per-seller-fallback')

    def test_central_cloudru_provider_works(self):
        """Центральный провайдер cloudru → config.provider == CLOUDRU."""
        _seed_central('cloudru', 'cloud-token')
        seller = _seed_seller_with_ai(ai_api_key='unused')

        config = AIConfig.for_seller(seller_id=seller.id)

        self.assertEqual(config.api_key, 'cloud-token')
        self.assertEqual(config.provider, AIProvider.CLOUDRU)

    def test_no_ai_settings_and_central_present(self):
        """Нет AutoImportSettings, но центральный ключ есть → всё равно работает."""
        _seed_central('openrouter', 'central-key-only')
        # Создаём пустого продавца без AutoImportSettings
        user = User(
            username='noai', email='noai@test.com',
            password_hash='x', is_active=True,
        )
        db.session.add(user)
        db.session.flush()
        seller = Seller(user_id=user.id, company_name='NoAISeller')
        db.session.add(seller)
        db.session.commit()

        config = AIConfig.for_seller(seller_id=seller.id)

        self.assertEqual(config.api_key, 'central-key-only')
        self.assertEqual(config.provider, AIProvider.OPENROUTER)

    def test_no_ai_settings_and_no_central_raises(self):
        """Нет AutoImportSettings и нет центрального ключа → ValueError."""
        user = User(
            username='noai2', email='noai2@test.com',
            password_hash='x', is_active=True,
        )
        db.session.add(user)
        db.session.flush()
        seller = Seller(user_id=user.id, company_name='NoAISeller2')
        db.session.add(seller)
        db.session.commit()

        with self.assertRaises(ValueError):
            AIConfig.for_seller(seller_id=seller.id)

    def test_explicit_temperature_and_max_tokens_respected(self):
        """Явные temperature/max_tokens перекрывают дефолты даже при центральном ключе."""
        _seed_central('openrouter', 'central-sk')
        seller = _seed_seller_with_ai()

        config = AIConfig.for_seller(seller_id=seller.id, temperature=0.9, max_tokens=300)

        self.assertAlmostEqual(config.temperature, 0.9)
        self.assertEqual(config.max_tokens, 300)


# ---------------------------------------------------------------------------
# Тесты ImageGenerationConfig.from_settings (image-gen путь)
# ---------------------------------------------------------------------------

class TestImageGenCentral(unittest.TestCase):
    """ImageGenerationConfig.from_settings использует центральный openrouter-ключ.

    Тест работает через SimpleNamespace (fake settings), чтобы не требовать
    полной записи в БД — image_generation_service не делает SQL-запросов сам.
    """

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _supplier_settings(self, openrouter_key='supplier-openrouter-key'):
        """Фиктивный объект настроек поставщика с включённой генерацией."""
        return SimpleNamespace(
            image_gen_enabled=True,
            image_gen_provider='openrouter',
            openrouter_api_key=openrouter_key,
            ai_api_key=openrouter_key,
            image_gen_width=900,
            image_gen_height=1200,
            openai_image_quality='standard',
            openai_image_style='vivid',
        )

    def test_central_openrouter_key_overrides_supplier(self):
        """Центральный openrouter-ключ перекрывает per-supplier ключ."""
        _seed_central('openrouter', 'central-openrouter-key')
        settings = self._supplier_settings(openrouter_key='supplier-key-unused')

        config = ImageGenerationConfig.from_settings(settings)

        self.assertIsNotNone(config)
        self.assertEqual(config.openrouter_api_key, 'central-openrouter-key')
        self.assertEqual(config.api_key, 'central-openrouter-key')
        self.assertEqual(config.provider, ImageProvider.OPENROUTER)

    def test_fallback_to_supplier_key_when_central_absent(self):
        """Нет центрального ключа → per-supplier openrouter-ключ."""
        settings = self._supplier_settings(openrouter_key='supplier-own-key')

        config = ImageGenerationConfig.from_settings(settings)

        self.assertIsNotNone(config)
        self.assertEqual(config.openrouter_api_key, 'supplier-own-key')
        self.assertEqual(config.provider, ImageProvider.OPENROUTER)

    def test_central_non_openrouter_does_not_override_image_gen(self):
        """Центральный провайдер cloudru → image-gen НЕ перекрывается (только openrouter совместим)."""
        _seed_central('cloudru', 'cloud-token')
        settings = self._supplier_settings(openrouter_key='supplier-key-used')

        config = ImageGenerationConfig.from_settings(settings)

        self.assertIsNotNone(config)
        # cloudru не подходит для image-gen → берём per-supplier
        self.assertEqual(config.openrouter_api_key, 'supplier-key-used')

    def test_central_empty_api_key_falls_back_to_supplier(self):
        """Центральный провайдер openrouter, но ключ пустой → per-supplier."""
        db.session.add(SystemSettings(
            key='agent_llm_provider', value='openrouter', value_type='string',
        ))
        db.session.add(SystemSettings(
            key='agent_openrouter_api_key', value='', value_type='string',
        ))
        db.session.commit()
        settings = self._supplier_settings(openrouter_key='supplier-fallback-key')

        config = ImageGenerationConfig.from_settings(settings)

        self.assertEqual(config.openrouter_api_key, 'supplier-fallback-key')

    def test_non_openrouter_provider_unaffected(self):
        """Поставщик с fluxapi — центральный ключ openrouter не трогает fluxapi-конфиг."""
        _seed_central('openrouter', 'central-openrouter-key')
        settings = SimpleNamespace(
            image_gen_enabled=True,
            image_gen_provider='fluxapi',
            fluxapi_key='flux-key-123',
            image_gen_width=900,
            image_gen_height=1200,
            openai_image_quality='standard',
            openai_image_style='vivid',
        )

        config = ImageGenerationConfig.from_settings(settings)

        self.assertIsNotNone(config)
        self.assertEqual(config.provider, ImageProvider.FLUXAPI)
        self.assertEqual(config.fluxapi_key, 'flux-key-123')


if __name__ == '__main__':
    unittest.main()
