# -*- coding: utf-8 -*-
"""Focused contracts for the chat -> Gemini -> Image Lab workflow."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents.llm import OpenAICompatLLM, OpenRouterLLM
from agents.platform_client import PlatformAPIError
from agents.unified import ImageGeneratorSkill, UnifiedSellerAgent
from services.agent_harness import (
    _image_source_safety_rank,
    _message_numeric_references,
    build_plan,
)


STYLE_URL = (
    "https://mow-basket-cdn-11.geobasket.ru/vol3209/part320944/"
    "320944209/images/big/1.webp"
)


class _ImagePromptLLM:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def structured_output_multimodal_with_usage(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {
            "data": {
                "scene_title": "Cyan water studio",
                "scene_prompt": (
                    "Saturated cyan gradient studio, a vertical water splash on "
                    "the left, glossy white pedestal on the right, crisp rim light"
                ),
                "composition": "product_right",
            },
            "usage": {
                "input_tokens": 120,
                "output_tokens": 48,
                "api_requests": 1,
            },
        }


class _ImagePlatform:
    def __init__(self):
        self.create_calls = []

    def get_image_generation_brief(self, seller_id, entity_kind, product_id):
        assert (seller_id, entity_kind, product_id) == (7, "product", 29589)
        return {
            "photo_count": 4,
            "visual_context": {
                "title": "Verified catalog title",
                "colors": ["beige"],
            },
            "generation": {
                "backend": "openrouter",
                "model": "google/gemini-3.1-flash-lite-image",
                "strategy": "native_scene",
                "estimated_cost_rub": 3.30,
                "review_required": True,
                "publishable": False,
            },
        }

    def create_image_generation_experiment(
        self, seller_id, entity_kind, product_id, **kwargs,
    ):
        self.create_calls.append((seller_id, entity_kind, product_id, kwargs))
        return {
            "experiment": {
                "id": 44,
                "product_title": "Verified catalog title",
                "status": "completed",
                "has_final": True,
                "image_url": "/image-lab/api/experiments/44/image/final",
                "source_url": "/image-lab/api/experiments/44/image/source",
                "lab_url": "/image-lab?product_id=18#experiment-44",
                "model": "google/gemini-3.1-flash-lite-image",
                "backend": "openrouter",
                "generation_strategy": "native_scene",
                "estimated_cost_rub": 3.30,
                "quality_status": "review_required",
            },
        }

    def get_task_status(self, task_id):
        return {"task": {"status": "running"}}


class _ImagePlannerLLM:
    def structured_output_with_usage(self, **kwargs):
        return {
            "data": {
                "title": "Anything from model",
                "summary": "Anything from model",
                "risk": "write",
                "confidence": 0.93,
                "scope_label": "One card",
                "scope_mode": "active",
                "steps": [{
                    "skill": "image-generator",
                    "label": "Create a scene",
                    "params": {"photo_index": 0},
                }],
            },
            "usage": {"input_tokens": 20, "output_tokens": 10, "api_requests": 1},
        }


def _skill(prompt_llm, platform):
    skill = object.__new__(ImageGeneratorSkill)
    skill.config = SimpleNamespace(
        IMAGE_PROMPT_MAX_TOKENS=420,
        IMAGE_WAIT_SECONDS=30,
    )
    skill.platform = platform
    skill._prompt_writer = lambda: (prompt_llm, "google/gemini-2.5-flash")
    return skill


def _task(style_url=STYLE_URL):
    return {
        "id": "task-image-1",
        "seller_id": 7,
        "input_data": {
            "text": "Сгенерируй фото в стиле референса",
            "product_ids": [29589],
            "entity_scope": {"kind": "product", "ids": [29589]},
            "params": {
                "entity_kind": "product",
                "photo_index": 0,
                "scene_hint": "Сделай яркую водную композицию как в референсе",
                "style_reference_url": style_url,
            },
        },
    }


def _check_deterministic_plan_prices_one_review_only_generation():
    text = f"Сгенерируй новое фото 1 товара в стиле {STYLE_URL}"
    plan = build_plan(text, product_ids=[29589], entity_kind="product")

    assert plan is not None
    assert plan.risk == "write"
    assert plan.requires_approval is True
    assert plan.steps[0]["agent"] == "image-generator"
    assert plan.steps[0]["params"]["style_reference_url"] == STYLE_URL
    assert plan.steps[0]["params"]["photo_index"] == 0
    assert "≈3,30 ₽" in plan.summary
    assert "Автопубликации не будет" in plan.summary

    any_plan = build_plan(
        "собери сцену для карточки любой",
        product_ids=[29589],
        entity_kind="product",
    )
    assert any_plan is not None
    assert any_plan.steps[0]["agent"] == "image-generator"


def _check_reference_url_numbers_are_not_grounded_as_product_articles():
    references, explicit = _message_numeric_references(
        f"Сделай для артикула 907560659 фото как здесь {STYLE_URL}",
    )

    assert references == ["907560659"]
    assert explicit == {"907560659"}
    assert "320944209" not in references


def _check_semantic_image_plan_keeps_typed_scope_and_fixed_price():
    agent = object.__new__(UnifiedSellerAgent)
    agent.llm = _ImagePlannerLLM()
    agent.system_prompt = UnifiedSellerAgent.system_prompt

    result = agent._plan_request(
        {"id": "plan-image"},
        {
            "text": f"Собери красивую сцену по примеру {STYLE_URL}",
            "product_ids": [29589],
            "entity_scope": {"kind": "product", "ids": [29589]},
            "scope_origin": "request",
        },
    )

    assert result["status"] == "completed"
    assert result["risk"] == "write"
    assert result["product_ids"] == [29589]
    assert result["steps"][0]["agent"] == "image-generator"
    assert result["steps"][0]["params"]["style_reference_url"] == STYLE_URL
    assert result["title"] == "Сгенерировать фото товара"
    assert "≈3,30 ₽" in result["summary"]


def _check_image_skill_sends_foreign_reference_only_to_gemini():
    prompt_llm = _ImagePromptLLM()
    platform = _ImagePlatform()

    result = _skill(prompt_llm, platform).execute_task(_task())

    assert result["status"] == "completed"
    assert result["needs_review"] == 1
    assert result["estimated_cost_rub"] == 3.30
    assert len(prompt_llm.calls) == 1
    assert prompt_llm.calls[0]["image_urls"] == [STYLE_URL]
    assert "игнорируя товар" in prompt_llm.calls[0]["prompt"]
    assert len(platform.create_calls) == 1
    seller_id, entity_kind, product_id, create = platform.create_calls[0]
    assert (seller_id, entity_kind, product_id) == (7, "product", 29589)
    assert create["prompt_model"] == "google/gemini-2.5-flash"
    assert STYLE_URL not in create["scene_prompt"]
    assert "clean negative space on the left" in create["scene_prompt"]
    artifact = result["artifacts"][0]
    assert artifact["type"] == "image_generation"
    assert artifact["has_final"] is True
    assert artifact["publishable"] is False
    assert artifact["review_required"] is True


def _check_gemini_failure_never_starts_paid_image_generation():
    platform = _ImagePlatform()
    result = _skill(
        _ImagePromptLLM(error=RuntimeError("prompt provider unavailable")),
        platform,
    ).execute_task(_task())

    assert result["status"] == "failed"
    assert "платная генерация не запускалась" in result["message"]
    assert platform.create_calls == []


def _check_missing_image_lab_source_is_friendly_and_never_paid():
    platform = _ImagePlatform()

    def unavailable(*_args, **_kwargs):
        raise PlatformAPIError(
            409,
            'У карточки нет доступного исходного фото',
        )

    platform.get_image_generation_brief = unavailable
    result = _skill(_ImagePromptLLM(), platform).execute_task(_task())

    assert result['status'] == 'needs_clarification'
    assert 'У карточки нет доступного исходного фото' in result['message']
    assert 'Платная генерация не запускалась' in result['message']
    assert platform.create_calls == []


def _check_openrouter_multimodal_payload_contains_one_style_reference():
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content='{"scene":"cyan studio"}'),
                )],
                usage=SimpleNamespace(prompt_tokens=80, completion_tokens=12),
            )

    provider = object.__new__(OpenAICompatLLM)
    provider.model = "google/gemini-2.5-flash"
    provider.base_url = "https://openrouter.ai/api/v1"
    provider.cfg = SimpleNamespace(TEMPERATURE=0.1, MAX_TOKENS=256)
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions()),
    )
    result = provider.structured_output_multimodal_with_usage(
        "stable system",
        "inspect style only",
        {
            "type": "object",
            "properties": {"scene": {"type": "string"}},
            "required": ["scene"],
        },
        image_urls=[STYLE_URL],
        max_tokens=64,
    )

    assert result["data"] == {"scene": "cyan studio"}
    assert STYLE_URL not in calls[0]["messages"][0]["content"]
    assert calls[0]["messages"][1]["content"][1] == {
        "type": "image_url", "image_url": {"url": STYLE_URL},
    }


def _check_openrouter_uses_explicit_agent_proxy():
    captured = {}
    marker = object()

    def fake_http_client(**kwargs):
        captured["httpx"] = kwargs
        return marker

    def fake_openai(**kwargs):
        captured["openai"] = kwargs
        return SimpleNamespace()

    config = SimpleNamespace(
        OPENROUTER_API_KEY="test-secret",
        OPENROUTER_MODEL="google/gemini-2.5-flash",
        AI_PROXY="socks5://proxy.local:1080",
    )
    with patch("httpx.Client", side_effect=fake_http_client), patch(
        "openai.OpenAI", side_effect=fake_openai,
    ):
        OpenRouterLLM(config)

    assert captured["httpx"]["proxy"] == "socks5://proxy.local:1080"
    assert captured["openai"]["http_client"] is marker


def _check_prompt_writer_uses_openrouter_gemini_flash_only():
    skill = object.__new__(ImageGeneratorSkill)
    skill.config = SimpleNamespace(
        IMAGE_PROMPT_MODEL="google/gemini-2.5-flash",
        OPENROUTER_API_KEY="sk-or-test",
        OPENROUTER_MODEL="unused/model",
        AI_PROXY="",
        OPENAI_COMPAT_BASE_URL="http://unused.invalid/v1",
    )

    provider, model = skill._prompt_writer()

    assert isinstance(provider, OpenRouterLLM)
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert model == "google/gemini-2.5-flash"
    assert provider.model == model


def _check_any_card_selection_rejects_explicit_sku_and_prefers_neutral_oil():
    assert _image_source_safety_rank(SimpleNamespace(
        title="ALIVE / Мастурбатор / Lola",
        category="Мастурбаторы и вагины",
        mapped_wb_category="",
    )) is None
    assert _image_source_safety_rank(SimpleNamespace(
        title="Массажное масло Friday Bae TOUCH 50 мл",
        category="Смазки, косметика > Массажные масла",
        mapped_wb_category="",
    )) == 0


class ImageGenerationWorkflowTestCase(unittest.TestCase):
    """Expose the focused pytest-style checks to the runtime unittest suite."""

    test_deterministic_plan_prices_one_review_only_generation = staticmethod(
        _check_deterministic_plan_prices_one_review_only_generation
    )
    test_reference_url_numbers_are_not_grounded_as_product_articles = staticmethod(
        _check_reference_url_numbers_are_not_grounded_as_product_articles
    )
    test_semantic_image_plan_keeps_typed_scope_and_fixed_price = staticmethod(
        _check_semantic_image_plan_keeps_typed_scope_and_fixed_price
    )
    test_image_skill_sends_foreign_reference_only_to_gemini = staticmethod(
        _check_image_skill_sends_foreign_reference_only_to_gemini
    )
    test_gemini_failure_never_starts_paid_image_generation = staticmethod(
        _check_gemini_failure_never_starts_paid_image_generation
    )
    test_missing_image_lab_source_is_friendly_and_never_paid = staticmethod(
        _check_missing_image_lab_source_is_friendly_and_never_paid
    )
    test_openrouter_multimodal_payload_contains_one_style_reference = staticmethod(
        _check_openrouter_multimodal_payload_contains_one_style_reference
    )
    test_openrouter_uses_explicit_agent_proxy = staticmethod(
        _check_openrouter_uses_explicit_agent_proxy
    )
    test_prompt_writer_uses_openrouter_gemini_flash_only = staticmethod(
        _check_prompt_writer_uses_openrouter_gemini_flash_only
    )
    test_any_card_selection_rejects_explicit_sku_and_prefers_neutral_oil = staticmethod(
        _check_any_card_selection_rejects_explicit_sku_and_prefers_neutral_oil
    )
