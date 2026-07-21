# -*- coding: utf-8 -*-
"""
Image Generation Service - Генерация изображений для инфографики WB.

Новые seller-facing запуски Фотостудии используют dedicated image API
OpenRouter. Остальные реализации сохранены для совместимости с историческими
экспериментами и отдельными legacy call sites; доступность новых целей задаёт
``services.image_lab_service``.
"""

import json
import logging
import os
import requests
import time
import base64
import io
import hashlib
from abc import ABC, abstractmethod
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass
from enum import Enum
from PIL import Image

logger = logging.getLogger(__name__)


def _get_proxy_config() -> Optional[Dict[str, str]]:
    """Получает настройки прокси из переменных окружения.

    Поддерживаемые переменные:
        IMAGE_GEN_PROXY  — прокси только для image gen (приоритет)
        HTTPS_PROXY      — общий прокси

    Примеры значений:
        socks5://127.0.0.1:1080
        http://127.0.0.1:8080
        socks5h://127.0.0.1:1080  (DNS через прокси)
    """
    proxy_url = os.environ.get('IMAGE_GEN_PROXY') or os.environ.get('AI_PROXY') or os.environ.get('HTTPS_PROXY')
    if proxy_url:
        return {'http': proxy_url, 'https': proxy_url}
    return None


class ImageProvider(Enum):
    """Поддерживаемые провайдеры генерации изображений"""
    OPENROUTER = "openrouter"  # OpenRouter — доступ к DALL-E, Imagen и др.
    FLUXAPI = "fluxapi"  # FluxAPI.ai - простой API
    TENSORART = "tensorart"  # Tensor.art - дешёвый
    TOGETHER_FLUX = "together_flux"  # Together AI Flux
    OPENAI_DALLE = "openai_dalle"  # DALL-E 3
    FLUX_PRO = "flux_pro"  # Flux.1 Pro через Replicate
    SDXL = "sdxl"  # Stable Diffusion XL через Replicate
    GEN_API = "gen_api"  # Gen-API.ru — российский агрегатор, оплата в рублях
    AITUNNEL = "aitunnel"  # AITunnel.ru — OpenAI-совместимый агрегатор, рубли


# Конфигурация провайдеров
PROVIDER_CONFIG = {
    ImageProvider.OPENROUTER: {
        "name": "OpenRouter",
        "description": "Dedicated image API: Gemini image, GPT Image и Grok Imagine",
        "api_url": "https://openrouter.ai/api/v1/images",
        "price_per_image": "$0.034-0.21",
        "max_size": "2K",
        "supports_reference": True,
        "recommended": True
    },
    ImageProvider.FLUXAPI: {
        "name": "FluxAPI.ai",
        "description": "Простой API, есть trial credits",
        "api_url": "https://api.fluxapi.ai/api/v1/flux/kontext/generate",
        "price_per_image": "~$0.025",
        "max_size": "1440x810",
        "supports_reference": True,
        "recommended": True
    },
    ImageProvider.TENSORART: {
        "name": "Tensor.art",
        "description": "Дешёвый ($0.003/credit), много моделей",
        "api_url": "https://api.tensor.art",
        "price_per_image": "~$0.01",
        "max_size": "1440x810",
        "supports_reference": True,
        "recommended": True
    },
    ImageProvider.TOGETHER_FLUX: {
        "name": "Together AI Flux",
        "description": "Быстрый, высокий лимит запросов",
        "api_url": "https://api.together.xyz/v1/images/generations",
        "model": "black-forest-labs/FLUX.1-schnell-Free",
        "price_per_image": "~$0.02",
        "max_size": "1440x810",
        "supports_reference": False,
        "recommended": False
    },
    ImageProvider.OPENAI_DALLE: {
        "name": "OpenAI DALL-E 3",
        "description": "Лучшее качество, понимает русский язык, дорогой",
        "api_url": "https://api.openai.com/v1/images/generations",
        "price_per_image": "$0.04-0.12",
        "max_size": "1792x1024",
        "supports_reference": False,
        "recommended": False
    },
    ImageProvider.FLUX_PRO: {
        "name": "Flux.1 Pro",
        "description": "Высокое качество, быстрый, средняя цена",
        "api_url": "https://api.replicate.com/v1/predictions",
        "model": "black-forest-labs/flux-1.1-pro",
        "price_per_image": "~$0.05",
        "max_size": "1440x810",
        "supports_reference": True,
        "recommended": True
    },
    ImageProvider.SDXL: {
        "name": "Stable Diffusion XL",
        "description": "Хорошее качество, бюджетный вариант",
        "api_url": "https://api.replicate.com/v1/predictions",
        "model": "stability-ai/sdxl:39ed52f2a78e934b3ba6e2a89f5b1c712de7dfea535525255b1aa35c5565e08b",
        "price_per_image": "~$0.01",
        "max_size": "1024x1024",
        "supports_reference": True,
        "recommended": False
    },
    ImageProvider.GEN_API: {
        "name": "Gen-API",
        "description": "Российский агрегатор (FLUX.2, Kontext Pro, Seedream, Nano Banana), рубли",
        "api_url": "https://api.gen-api.ru/api/v1",
        "price_per_image": "3.3-10 ₽",
        "max_size": "2048x2048",
        "supports_reference": True,
        "recommended": True
    },
    ImageProvider.AITUNNEL: {
        "name": "AITunnel",
        "description": "OpenAI-совместимый российский агрегатор (Seedream, GPT Image), рубли",
        "api_url": "https://api.aitunnel.ru/v1",
        "price_per_image": "1.5-7 ₽",
        "max_size": "2048x2048",
        "supports_reference": True,
        "recommended": True
    }
}

# Маркеры цензурного отказа провайдера (а не технического сбоя).
# Используются пилотом «Фотостудии» и fallback-цепочкой для честной метрики отказов.
NSFW_ERROR_MARKERS = (
    "nsfw",
    "safety",
    "content policy",
    "content_policy",
    "moderation",
    "flagged",
    "censor",
    "policy violation",
    "prohibited",
    "blocked",
    "sensitive",
    "недопустимый контент",
)


def is_censorship_refusal(error_message):
    """True, если ошибка похожа на отказ цензуры/модерации провайдера."""
    msg = (error_message or "").lower()
    return any(marker in msg for marker in NSFW_ERROR_MARKERS)


def fit_image_to_size(image_bytes, width, height):
    """Приводит картинку к точному размеру: scale-to-cover + центральный кроп.

    Провайдеры отдают размеры по своим сеткам (кратно 16, фикс-пресеты и т.п.);
    белый padding на тёмных сценах даёт видимую полосу, поэтому cover+crop.
    При любой ошибке постобработки возвращает оригинал — генерацию не роняем.
    """
    try:
        from PIL import ImageOps

        img = Image.open(io.BytesIO(image_bytes))
        if img.size == (width, height):
            return image_bytes
        img = ImageOps.fit(img.convert("RGB"), (width, height), method=Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return image_bytes


@dataclass
class ImageGenerationConfig:
    """Конфигурация генерации изображений"""
    provider: ImageProvider
    api_key: str
    # OpenRouter specific
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-3.1-flash-lite-image"
    openrouter_resolution: str = "1K"
    openrouter_aspect_ratio: str = "3:4"
    openrouter_quality: Optional[str] = None
    openrouter_background: Optional[str] = None
    # OpenAI specific
    openai_model: str = "dall-e-3"
    openai_quality: str = "standard"  # standard или hd
    openai_style: str = "vivid"  # vivid или natural
    # Replicate specific
    replicate_api_key: str = ""
    # Together AI specific
    together_api_key: str = ""
    # FluxAPI specific
    fluxapi_key: str = ""
    # TensorArt specific
    tensorart_app_id: str = ""
    tensorart_api_key: str = ""  # Private key for signing
    # Gen-API specific (https://gen-api.ru — slugs сетей сверяются с доками)
    gen_api_key: str = ""
    gen_api_model: str = "flux-2"  # t2i-сеть (режим B)
    gen_api_edit_model: str = "nano-banana"  # i2i-сеть (режим A)
    # AITunnel specific (https://aitunnel.ru — OpenAI images API)
    aitunnel_api_key: str = ""
    aitunnel_model: str = "gpt-image-2"
    aitunnel_edit_model: str = "seedream-4.5"
    # Общие
    default_width: int = 900
    default_height: int = 1200
    timeout: int = 120
    proxy_enabled: bool = False

    @classmethod
    def from_settings(cls, settings) -> Optional['ImageGenerationConfig']:
        """Создает конфигурацию из настроек.

        Поддерживает два варианта:
        1. Объект с image_gen_enabled/image_gen_provider (AutoImportSettings)
        2. Объект Supplier с ai_provider/ai_api_key — для OpenRouter/OpenAI
           fallback на AI ключ поставщика
        """
        has_image_gen = hasattr(settings, 'image_gen_enabled') and settings.image_gen_enabled

        if not has_image_gen:
            # Fallback: если у Supplier включен AI с OpenRouter/OpenAI — используем его для image gen
            ai_provider = getattr(settings, 'ai_provider', None)
            ai_key = None
            if hasattr(settings, 'ai_api_key'):
                ai_key = settings.ai_api_key
            if not ai_key:
                ai_key = getattr(settings, '_ai_api_key_encrypted', None)

            proxy_flag = getattr(settings, 'ai_proxy_enabled', False) or False

            if ai_provider == 'openrouter' and ai_key:
                return cls(
                    provider=ImageProvider.OPENROUTER,
                    api_key=ai_key,
                    openrouter_api_key=ai_key,
                    proxy_enabled=proxy_flag,
                )
            elif ai_provider == 'openai' and ai_key:
                return cls(
                    provider=ImageProvider.OPENAI_DALLE,
                    api_key=ai_key,
                    proxy_enabled=proxy_flag,
                )
            return None

        provider_str = getattr(settings, 'image_gen_provider', 'openrouter')
        try:
            provider = ImageProvider(provider_str)
        except ValueError:
            provider = ImageProvider.OPENROUTER

        api_key = ""
        openrouter_key = ""
        replicate_key = ""
        together_key = ""
        fluxapi_key = ""
        tensorart_app_id = ""
        tensorart_api_key = ""

        if provider == ImageProvider.OPENROUTER:
            # Сначала пробуем центральный ключ (только для openrouter-совместимых провайдеров)
            _central_key = ''
            try:
                from services.llm_config import get_central_llm_config
                _central = get_central_llm_config()
                # Центральный ключ применяем только если провайдер — openrouter
                # (image-gen не знает, как работать с cloudru/mimo/openai через центральный конфиг)
                if _central and _central.get('api_key') and _central.get('provider') == 'openrouter':
                    _central_key = _central['api_key']
            except Exception:
                _central_key = ''

            if _central_key:
                # Центральный openrouter-ключ перекрывает per-supplier ключ
                openrouter_key = _central_key
            else:
                # Берём ключ из AI настроек поставщика (прежнее поведение)
                openrouter_key = getattr(settings, 'openrouter_api_key', '') or ''
                if not openrouter_key:
                    # Fallback на ai_api_key
                    if hasattr(settings, 'ai_api_key'):
                        openrouter_key = settings.ai_api_key or ''
            api_key = openrouter_key
        elif provider == ImageProvider.FLUXAPI:
            fluxapi_key = getattr(settings, 'fluxapi_key', '') or ''
        elif provider == ImageProvider.TENSORART:
            tensorart_app_id = getattr(settings, 'tensorart_app_id', '') or ''
            tensorart_api_key = getattr(settings, 'tensorart_api_key', '') or ''
        elif provider == ImageProvider.OPENAI_DALLE:
            api_key = getattr(settings, 'openai_api_key', '') or ''
        elif provider == ImageProvider.TOGETHER_FLUX:
            together_key = getattr(settings, 'together_api_key', '') or ''
        else:
            replicate_key = getattr(settings, 'replicate_api_key', '') or ''

        if not api_key and not replicate_key and not together_key and not fluxapi_key and not tensorart_api_key and not openrouter_key:
            logger.warning("Image generation включен, но API ключ не указан")
            return None

        return cls(
            provider=provider,
            api_key=api_key,
            openrouter_api_key=openrouter_key,
            replicate_api_key=replicate_key,
            together_api_key=together_key,
            fluxapi_key=fluxapi_key,
            tensorart_app_id=tensorart_app_id,
            tensorart_api_key=tensorart_api_key,
            openai_quality=getattr(settings, 'openai_image_quality', 'standard') or 'standard',
            openai_style=getattr(settings, 'openai_image_style', 'vivid') or 'vivid',
            default_width=getattr(settings, 'image_gen_width', 900) or 900,
            default_height=getattr(settings, 'image_gen_height', 1200) or 1200
        )

    @classmethod
    def from_env(cls, provider):
        """Конфиг из переменных окружения — для one-off скриптов (пилот).

        OPENROUTER_API_KEY / GEN_API_KEY / AITUNNEL_API_KEY. Возвращает None,
        если ключа нет. Российские backend'ы сохранены только для уже созданных
        экспериментов; новые запуски выбирает Image Lab policy.
        """
        if provider == ImageProvider.OPENROUTER:
            key = os.environ.get('OPENROUTER_API_KEY', '')
            if not key:
                return None
            model = (
                os.environ.get('OPENROUTER_IMAGE_MODEL')
                or 'google/gemini-3.1-flash-lite-image'
            )
            is_gpt_image_2 = model == 'openai/gpt-image-2'
            return cls(
                provider=provider,
                api_key=key,
                openrouter_api_key=key,
                openrouter_model=model,
                openrouter_resolution=(
                    '' if is_gpt_image_2
                    else (os.environ.get('OPENROUTER_IMAGE_RESOLUTION') or '1K')
                ),
                openrouter_aspect_ratio='' if is_gpt_image_2 else '3:4',
                openrouter_quality='medium' if is_gpt_image_2 else None,
                openrouter_background='opaque' if is_gpt_image_2 else None,
                proxy_enabled=bool(_get_proxy_config()),
            )
        if provider == ImageProvider.GEN_API:
            key = os.environ.get('GEN_API_KEY', '')
            if not key:
                return None
            return cls(provider=provider, api_key=key, gen_api_key=key)
        if provider == ImageProvider.AITUNNEL:
            key = os.environ.get('AITUNNEL_API_KEY', '')
            if not key:
                return None
            return cls(provider=provider, api_key=key, aitunnel_api_key=key)
        return None


class ImageGenerator(ABC):
    """Абстрактный базовый класс для генераторов изображений"""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        width: int = 1440,
        height: int = 810,
        reference_image_url: Optional[str] = None
    ) -> Tuple[bool, Optional[bytes], str]:
        """
        Генерирует изображение

        Args:
            prompt: Текстовый промпт
            width: Ширина
            height: Высота
            reference_image_url: URL референсного изображения (опционально)

        Returns:
            Tuple[success, image_bytes, error_message]
        """
        pass

    def edit(
        self,
        prompt: str,
        source_image_url: Optional[str] = None,
        source_image_bytes: Optional[bytes] = None,
        additional_source_images: Optional[List[bytes]] = None,
        mask_bytes: Optional[bytes] = None,
        input_fidelity: Optional[str] = None,
        quality: Optional[str] = None,
        width: int = 900,
        height: int = 1200,
    ) -> Tuple[bool, Optional[bytes], str]:
        """Image-to-image edit. Провайдеры без i2i возвращают ошибку."""
        return False, None, (
            f"Провайдер {self.__class__.__name__} не поддерживает image-to-image"
        )


class OpenAIImageGenerator(ImageGenerator):
    """Генератор изображений через OpenAI DALL-E 3"""

    def __init__(self, config: ImageGenerationConfig):
        self.config = config
        self.api_url = "https://api.openai.com/v1/images/generations"
        self.proxies = _get_proxy_config() if config.proxy_enabled else None

    def generate(
        self,
        prompt: str,
        width: int = 1440,
        height: int = 810,
        reference_image_url: Optional[str] = None
    ) -> Tuple[bool, Optional[bytes], str]:
        """Генерирует изображение через DALL-E 3"""

        # DALL-E 3 поддерживает ограниченные размеры
        # Выбираем ближайший поддерживаемый размер
        if width > height:
            size = "1792x1024"  # Landscape
        elif height > width:
            size = "1024x1792"  # Portrait
        else:
            size = "1024x1024"  # Square

        # Улучшаем промпт для DALL-E
        enhanced_prompt = self._enhance_prompt_for_dalle(prompt)

        payload = {
            "model": self.config.openai_model,
            "prompt": enhanced_prompt,
            "n": 1,
            "size": size,
            "quality": self.config.openai_quality,
            "style": self.config.openai_style,
            "response_format": "b64_json"
        }

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json"
        }

        try:
            logger.info(f"🎨 DALL-E 3 генерация: {prompt[:100]}...")
            logger.debug(f"Payload: {json.dumps(payload, ensure_ascii=False)[:500]}")

            response = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                timeout=self.config.timeout,
                proxies=self.proxies
            )

            if response.status_code != 200:
                error_data = response.json() if response.text else {}
                error_msg = error_data.get('error', {}).get('message', response.text[:200])
                logger.error(f"❌ DALL-E ошибка: {response.status_code} - {error_msg}")
                return False, None, f"DALL-E ошибка: {error_msg}"

            data = response.json()
            b64_image = data['data'][0]['b64_json']
            image_bytes = base64.b64decode(b64_image)

            # Resize если нужно
            if size != f"{width}x{height}":
                image_bytes = self._resize_image(image_bytes, width, height)

            logger.info(f"✅ DALL-E изображение создано ({len(image_bytes)} байт)")
            return True, image_bytes, ""

        except requests.exceptions.Timeout:
            return False, None, f"Таймаут запроса ({self.config.timeout}с)"
        except Exception as e:
            logger.error(f"❌ DALL-E ошибка: {e}")
            return False, None, str(e)

    def _enhance_prompt_for_dalle(self, prompt: str) -> str:
        """Улучшает промпт для лучших результатов DALL-E"""
        # Добавляем стилистические указания
        style_hints = """
Professional product infographic slide for e-commerce marketplace.
Clean modern design, high quality, commercial photography style.
No text overlays, no watermarks, no logos.
"""
        return f"{prompt}\n\n{style_hints}"

    def _resize_image(self, image_bytes: bytes, width: int, height: int) -> bytes:
        """Изменяет размер изображения"""
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img = img.resize((width, height), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            img.save(output, format='PNG', quality=95)
            return output.getvalue()
        except Exception as e:
            logger.warning(f"Ошибка resize: {e}, возвращаем оригинал")
            return image_bytes


class OpenRouterImageGenerator(ImageGenerator):
    """OpenRouter dedicated image API with native image references."""

    MODEL_MAX_REFERENCES = {
        "google/gemini-3.1-flash-lite-image": 14,
        "google/gemini-3.1-flash-image": 14,
        "x-ai/grok-imagine-image-quality": 3,
        "openai/gpt-image-2": 16,
    }

    def __init__(self, config: ImageGenerationConfig):
        self.config = config
        self.api_url = "https://openrouter.ai/api/v1/images"
        self.proxies = _get_proxy_config() if config.proxy_enabled else None

    @staticmethod
    def _data_url(image_bytes: bytes) -> str:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif image_bytes.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            mime = "image/png"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _error_message(response) -> str:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:500]
            if isinstance(error, str) and error:
                return error[:500]
            if payload.get("message"):
                return str(payload["message"])[:500]
        return f"HTTP {response.status_code}"

    def _headers(self) -> Dict[str, str]:
        api_key = self.config.openrouter_api_key or self.config.api_key
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://seller-platform.tech",
            "X-Title": "Seller Platform",
        }

    def _request(
        self,
        *,
        prompt: str,
        reference_urls: Optional[List[str]] = None,
        width: int,
        height: int,
    ) -> Tuple[bool, Optional[bytes], str]:
        model = self.config.openrouter_model
        references = list(reference_urls or [])
        max_references = self.MODEL_MAX_REFERENCES.get(model, 1)
        if len(references) > max_references:
            return False, None, (
                f"OpenRouter: {model} принимает не более {max_references} "
                "фото-референсов"
            )
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
        }
        if self.config.openrouter_resolution:
            payload["resolution"] = self.config.openrouter_resolution
        if self.config.openrouter_aspect_ratio:
            payload["aspect_ratio"] = self.config.openrouter_aspect_ratio
        if self.config.openrouter_quality:
            payload["quality"] = self.config.openrouter_quality
        if self.config.openrouter_background:
            payload["background"] = self.config.openrouter_background
        if references:
            payload["input_references"] = [
                {"type": "image_url", "image_url": {"url": url}}
                for url in references
            ]
        try:
            logger.info(
                "OpenRouter image request model=%s resolution=%s quality=%s refs=%s proxy=%s",
                model,
                self.config.openrouter_resolution or "provider-default",
                self.config.openrouter_quality or "provider-default",
                len(references),
                bool(self.proxies),
            )
            response = requests.post(
                self.api_url,
                json=payload,
                headers=self._headers(),
                timeout=self.config.timeout,
                proxies=self.proxies,
            )
            if not 200 <= response.status_code < 300:
                error = self._error_message(response)
                logger.error("OpenRouter image HTTP %s: %s", response.status_code, error)
                prefix = "NSFW: " if is_censorship_refusal(error) else ""
                return False, None, f"{prefix}OpenRouter: {error}"
            try:
                data = response.json()
            except (TypeError, ValueError):
                return False, None, "OpenRouter: некорректный JSON в ответе"
            rows = data.get("data") if isinstance(data, dict) else None
            image_data = rows[0] if isinstance(rows, list) and rows else None
            if not isinstance(image_data, dict):
                return False, None, "OpenRouter: пустой ответ"
            encoded = image_data.get("b64_json")
            if isinstance(encoded, str) and encoded:
                try:
                    image_bytes = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    return False, None, "OpenRouter: битый base64 в ответе"
            elif isinstance(image_data.get("url"), str):
                downloaded = requests.get(
                    image_data["url"], timeout=60, proxies=self.proxies,
                )
                if downloaded.status_code != 200:
                    return False, None, "OpenRouter: не удалось скачать результат"
                image_bytes = downloaded.content
            else:
                return False, None, "OpenRouter: в ответе нет изображения"
            return True, fit_image_to_size(image_bytes, width, height), ""
        except requests.exceptions.Timeout:
            return False, None, f"OpenRouter: таймаут ({self.config.timeout}с)"
        except requests.RequestException as exc:
            logger.error("OpenRouter image transport error: %s", exc.__class__.__name__)
            return False, None, "OpenRouter: ошибка соединения"

    def generate(
        self,
        prompt: str,
        width: int = 900,
        height: int = 1200,
        reference_image_url: Optional[str] = None
    ) -> Tuple[bool, Optional[bytes], str]:
        references = [reference_image_url] if reference_image_url else []
        return self._request(
            prompt=prompt,
            reference_urls=references,
            width=width,
            height=height,
        )

    def edit(
        self,
        prompt: str,
        source_image_url: Optional[str] = None,
        source_image_bytes: Optional[bytes] = None,
        additional_source_images: Optional[List[bytes]] = None,
        mask_bytes: Optional[bytes] = None,
        input_fidelity: Optional[str] = None,
        quality: Optional[str] = None,
        width: int = 900,
        height: int = 1200,
    ) -> Tuple[bool, Optional[bytes], str]:
        del input_fidelity, quality
        if mask_bytes:
            return False, None, "OpenRouter: protection mask не поддерживается"
        if not source_image_bytes and not source_image_url:
            return False, None, "OpenRouter: не передано исходное изображение"
        references: List[str] = []
        if source_image_bytes:
            references.append(self._data_url(source_image_bytes))
        elif source_image_url:
            references.append(source_image_url)
        for item in additional_source_images or []:
            if not isinstance(item, bytes) or not item:
                return False, None, "OpenRouter: некорректный дополнительный референс"
            references.append(self._data_url(item))
        return self._request(
            prompt=prompt,
            reference_urls=references,
            width=width,
            height=height,
        )


class FluxAPIImageGenerator(ImageGenerator):
    """
    Генератор изображений через FluxAPI.ai

    Преимущества:
    - Простой REST API
    - Trial credits при регистрации
    - Поддержка Flux Kontext моделей
    """

    def __init__(self, config: ImageGenerationConfig):
        self.config = config
        self.api_url = "https://api.fluxapi.ai/api/v1/flux/kontext/generate"
        self.result_url = "https://api.fluxapi.ai/api/v1/flux/kontext/result"

    def generate(
        self,
        prompt: str,
        width: int = 1440,
        height: int = 810,
        reference_image_url: Optional[str] = None
    ) -> Tuple[bool, Optional[bytes], str]:
        """Генерирует изображение через FluxAPI.ai"""

        api_key = self.config.fluxapi_key

        # Определяем aspect ratio
        if width > height:
            aspect_ratio = "16:9"
        elif height > width:
            aspect_ratio = "9:16"
        else:
            aspect_ratio = "1:1"

        enhanced_prompt = self._enhance_prompt(prompt)

        payload = {
            "prompt": enhanced_prompt,
            "aspectRatio": aspect_ratio,
            "outputFormat": "png",
            "model": "flux-kontext-pro",
            "enableTranslation": True,
            "safetyTolerance": 2
        }

        if reference_image_url:
            payload["inputImage"] = reference_image_url

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        try:
            logger.info(f"🎨 FluxAPI генерация: {prompt[:100]}...")

            # Создаём задачу
            response = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code != 200:
                error_data = response.json() if response.text else {}
                error_msg = error_data.get('msg', response.text[:300])
                logger.error(f"❌ FluxAPI ошибка: {response.status_code} - {error_msg}")
                return False, None, f"FluxAPI ошибка: {error_msg}"

            data = response.json()
            if data.get('code') != 200:
                return False, None, f"FluxAPI ошибка: {data.get('msg', 'Unknown error')}"

            task_id = data.get('data', {}).get('taskId')
            if not task_id:
                return False, None, "FluxAPI: не получен taskId"

            # Ждём результат
            max_wait = self.config.timeout
            waited = 0
            poll_interval = 3

            while waited < max_wait:
                time.sleep(poll_interval)
                waited += poll_interval

                result_response = requests.get(
                    f"{self.result_url}/{task_id}",
                    headers=headers,
                    timeout=30
                )

                if result_response.status_code != 200:
                    continue

                result_data = result_response.json()
                status = result_data.get('data', {}).get('status')

                if status == 'completed':
                    image_url = result_data.get('data', {}).get('imageUrl')
                    if image_url:
                        img_response = requests.get(image_url, timeout=60)
                        if img_response.status_code == 200:
                            logger.info(f"✅ FluxAPI изображение создано")
                            return True, img_response.content, ""
                    return False, None, "FluxAPI: пустой результат"

                elif status == 'failed':
                    error = result_data.get('data', {}).get('error', 'Unknown error')
                    return False, None, f"FluxAPI ошибка: {error}"

                logger.debug(f"⏳ FluxAPI статус: {status}, ждем...")

            return False, None, f"FluxAPI таймаут ({max_wait}с)"

        except requests.exceptions.Timeout:
            return False, None, f"Таймаут запроса ({self.config.timeout}с)"
        except Exception as e:
            logger.error(f"❌ FluxAPI ошибка: {e}")
            return False, None, str(e)

    def _enhance_prompt(self, prompt: str) -> str:
        return f"""Professional e-commerce product infographic slide.
{prompt}
Style: clean, modern, minimalist, commercial photography.
No text, no watermarks, no logos. High quality, sharp details."""


class TensorArtImageGenerator(ImageGenerator):
    """
    Генератор изображений через Tensor.art TAMS API

    Преимущества:
    - Дешёвый ($0.003/credit)
    - Много моделей
    - Хорошее качество
    """

    def __init__(self, config: ImageGenerationConfig):
        self.config = config
        self.api_url = "https://ap-east-1.tensorart.cloud/v1/jobs"

    def generate(
        self,
        prompt: str,
        width: int = 1440,
        height: int = 810,
        reference_image_url: Optional[str] = None
    ) -> Tuple[bool, Optional[bytes], str]:
        """Генерирует изображение через Tensor.art"""

        app_id = self.config.tensorart_app_id
        api_key = self.config.tensorart_api_key

        if not app_id or not api_key:
            return False, None, "TensorArt: не указаны app_id или api_key"

        # Ограничения размера
        width = min(max(width, 512), 1536)
        height = min(max(height, 512), 1536)

        enhanced_prompt = self._enhance_prompt(prompt)

        # Генерируем request_id
        request_id = hashlib.md5(str(time.time()).encode()).hexdigest()

        payload = {
            "request_id": request_id,
            "stages": [
                {
                    "type": "INPUT_INITIALIZE",
                    "inputInitialize": {
                        "seed": -1,
                        "count": 1
                    }
                },
                {
                    "type": "DIFFUSION",
                    "diffusion": {
                        "width": width,
                        "height": height,
                        "prompts": [{"text": enhanced_prompt}],
                        "negativePrompts": [{"text": "text, watermark, logo, blurry, low quality"}],
                        "sampler": "DPM++ 2M Karras",
                        "sdVae": "Automatic",
                        "steps": 25,
                        "cfgScale": 7
                    }
                }
            ]
        }

        # Создаём подпись (упрощённая версия)
        timestamp = str(int(time.time() * 1000))

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-App-Id": app_id,
            "X-Timestamp": timestamp
        }

        try:
            logger.info(f"🎨 TensorArt генерация: {prompt[:100]}...")

            response = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code != 200:
                error_msg = response.text[:300]
                logger.error(f"❌ TensorArt ошибка: {response.status_code} - {error_msg}")
                return False, None, f"TensorArt ошибка: {error_msg}"

            data = response.json()
            job_id = data.get('job', {}).get('id')

            if not job_id:
                return False, None, "TensorArt: не получен job_id"

            # Ждём результат
            max_wait = self.config.timeout
            waited = 0
            poll_interval = 3

            while waited < max_wait:
                time.sleep(poll_interval)
                waited += poll_interval

                status_response = requests.get(
                    f"{self.api_url}/{job_id}",
                    headers=headers,
                    timeout=30
                )

                if status_response.status_code != 200:
                    continue

                status_data = status_response.json()
                job_status = status_data.get('job', {}).get('status')

                if job_status == 'SUCCESS':
                    images = status_data.get('job', {}).get('successInfo', {}).get('images', [])
                    if images:
                        image_url = images[0].get('url')
                        if image_url:
                            img_response = requests.get(image_url, timeout=60)
                            if img_response.status_code == 200:
                                logger.info(f"✅ TensorArt изображение создано")
                                return True, img_response.content, ""
                    return False, None, "TensorArt: пустой результат"

                elif job_status == 'FAILED':
                    error = status_data.get('job', {}).get('failedInfo', {}).get('reason', 'Unknown')
                    return False, None, f"TensorArt ошибка: {error}"

                logger.debug(f"⏳ TensorArt статус: {job_status}, ждем...")

            return False, None, f"TensorArt таймаут ({max_wait}с)"

        except requests.exceptions.Timeout:
            return False, None, f"Таймаут запроса ({self.config.timeout}с)"
        except Exception as e:
            logger.error(f"❌ TensorArt ошибка: {e}")
            return False, None, str(e)

    def _enhance_prompt(self, prompt: str) -> str:
        return f"""Professional e-commerce product infographic slide, {prompt},
clean modern design, commercial photography, studio lighting, high quality, sharp details"""


class TogetherImageGenerator(ImageGenerator):
    """
    Генератор изображений через Together AI API

    Преимущества:
    - Высокий лимит запросов
    - OpenAI-совместимый API
    - Поддержка Flux моделей
    """

    def __init__(self, config: ImageGenerationConfig):
        self.config = config
        self.api_url = "https://api.together.xyz/v1/images/generations"
        self.models = {
            "schnell": "black-forest-labs/FLUX.1-schnell",
            "dev": "black-forest-labs/FLUX.1-dev",
        }

    def generate(
        self,
        prompt: str,
        width: int = 1440,
        height: int = 810,
        reference_image_url: Optional[str] = None
    ) -> Tuple[bool, Optional[bytes], str]:
        """Генерирует изображение через Together AI"""

        api_key = self.config.together_api_key

        # Together поддерживает размеры кратные 32
        width = (width // 32) * 32
        height = (height // 32) * 32
        width = min(max(width, 256), 1440)
        height = min(max(height, 256), 1440)

        enhanced_prompt = self._enhance_prompt(prompt)

        payload = {
            "model": self.models["schnell"],
            "prompt": enhanced_prompt,
            "width": width,
            "height": height,
            "n": 1,
            "steps": 4,  # Schnell оптимизирован для 4 шагов
            "response_format": "b64_json"
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        try:
            logger.info(f"🎨 Together AI Flux генерация: {prompt[:100]}...")

            response = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                timeout=self.config.timeout
            )

            if response.status_code == 429:
                # Rate limit - ждём и повторяем
                retry_after = int(response.headers.get('Retry-After', 10))
                logger.warning(f"⏳ Together AI rate limit, ждём {retry_after}с...")
                time.sleep(retry_after)
                response = requests.post(
                    self.api_url, json=payload, headers=headers, timeout=self.config.timeout
                )

            if response.status_code != 200:
                error_data = response.json() if response.text else {}
                error_msg = error_data.get('error', {}).get('message', response.text[:300])
                logger.error(f"❌ Together AI ошибка: {response.status_code} - {error_msg}")
                return False, None, f"Together AI ошибка: {error_msg}"

            data = response.json()

            # Together возвращает данные в формате OpenAI
            if 'data' in data and len(data['data']) > 0:
                image_data = data['data'][0]

                if 'b64_json' in image_data:
                    image_bytes = base64.b64decode(image_data['b64_json'])
                elif 'url' in image_data:
                    # Скачиваем по URL
                    img_response = requests.get(image_data['url'], timeout=60)
                    if img_response.status_code == 200:
                        image_bytes = img_response.content
                    else:
                        return False, None, "Не удалось скачать изображение"
                else:
                    return False, None, "Неожиданный формат ответа от Together AI"

                logger.info(f"✅ Together AI изображение создано ({len(image_bytes)} байт)")
                return True, image_bytes, ""

            return False, None, "Пустой ответ от Together AI"

        except requests.exceptions.Timeout:
            return False, None, f"Таймаут запроса ({self.config.timeout}с)"
        except Exception as e:
            logger.error(f"❌ Together AI ошибка: {e}")
            return False, None, str(e)

    def _enhance_prompt(self, prompt: str) -> str:
        """Улучшает промпт для Flux"""
        return f"""Professional e-commerce product infographic slide.
{prompt}
Style: clean, modern, minimalist, commercial photography.
No text, no watermarks, no logos. High quality, sharp details."""


class ReplicateImageGenerator(ImageGenerator):
    """Генератор изображений через Replicate API (Flux, SDXL)"""

    # Настройки retry для rate limiting
    MAX_RETRIES = 3
    INITIAL_BACKOFF = 10  # секунд

    def __init__(self, config: ImageGenerationConfig):
        self.config = config
        self.api_url = "https://api.replicate.com/v1/predictions"
        # Новый API endpoint для моделей (без указания версии)
        self.models_api_url = "https://api.replicate.com/v1/models"

    def generate(
        self,
        prompt: str,
        width: int = 1440,
        height: int = 810,
        reference_image_url: Optional[str] = None
    ) -> Tuple[bool, Optional[bytes], str]:
        """Генерирует изображение через Replicate"""

        api_key = self.config.replicate_api_key or self.config.api_key

        if self.config.provider == ImageProvider.FLUX_PRO:
            return self._generate_flux(prompt, width, height, reference_image_url, api_key)
        else:
            return self._generate_sdxl(prompt, width, height, reference_image_url, api_key)

    def _generate_flux(
        self,
        prompt: str,
        width: int,
        height: int,
        reference_url: Optional[str],
        api_key: str
    ) -> Tuple[bool, Optional[bytes], str]:
        """Генерация через Flux.1 Pro (новый API без версии)"""

        # Используем новый API endpoint без указания версии
        # https://api.replicate.com/v1/models/{owner}/{model}/predictions
        model_endpoint = f"{self.models_api_url}/black-forest-labs/flux-1.1-pro/predictions"

        # Flux поддерживает произвольные размеры
        payload = {
            "input": {
                "prompt": self._enhance_prompt_for_flux(prompt),
                "width": width,
                "height": height,
                "output_format": "png",
                "aspect_ratio": "custom"
            }
        }

        # Если есть референс - добавляем (если модель поддерживает)
        if reference_url:
            payload["input"]["image_prompt"] = reference_url
            payload["input"]["prompt_strength"] = 0.8

        return self._run_replicate_prediction(payload, api_key, "Flux.1 Pro", model_endpoint)

    def _generate_sdxl(
        self,
        prompt: str,
        width: int,
        height: int,
        reference_url: Optional[str],
        api_key: str
    ) -> Tuple[bool, Optional[bytes], str]:
        """Генерация через SDXL (новый API без версии)"""

        # Используем новый API endpoint без указания версии
        model_endpoint = f"{self.models_api_url}/stability-ai/sdxl/predictions"

        # SDXL лучше работает с размерами кратными 8
        width = (width // 8) * 8
        height = (height // 8) * 8

        payload = {
            "input": {
                "prompt": self._enhance_prompt_for_sdxl(prompt),
                "width": min(width, 1024),
                "height": min(height, 1024),
                "num_outputs": 1,
                "scheduler": "K_EULER",
                "num_inference_steps": 30,
                "guidance_scale": 7.5,
                "negative_prompt": "text, watermark, logo, blurry, low quality, distorted"
            }
        }

        if reference_url:
            payload["input"]["image"] = reference_url
            payload["input"]["prompt_strength"] = 0.75

        return self._run_replicate_prediction(payload, api_key, "SDXL", model_endpoint)

    def _run_replicate_prediction(
        self,
        payload: Dict,
        api_key: str,
        model_name: str,
        endpoint_url: Optional[str] = None
    ) -> Tuple[bool, Optional[bytes], str]:
        """
        Запускает prediction на Replicate и ждет результат.
        Поддерживает retry с exponential backoff при rate limiting.
        """

        headers = {
            "Authorization": f"Bearer {api_key}",  # Новый формат авторизации
            "Content-Type": "application/json",
            "Prefer": "wait"  # Синхронный режим если возможно
        }

        # Используем переданный endpoint или дефолтный
        api_endpoint = endpoint_url or self.api_url

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                logger.info(f"🎨 {model_name} генерация запущена... (попытка {attempt + 1})")

                # Создаем prediction
                response = requests.post(
                    api_endpoint,
                    json=payload,
                    headers=headers,
                    timeout=30
                )

                # Обработка rate limiting (429)
                if response.status_code == 429:
                    if attempt < self.MAX_RETRIES:
                        # Пытаемся получить время ожидания из ответа
                        retry_after = self._parse_retry_after(response)
                        wait_time = retry_after or (self.INITIAL_BACKOFF * (2 ** attempt))
                        logger.warning(f"⏳ Rate limit! Ждём {wait_time}с перед повтором...")
                        time.sleep(wait_time)
                        continue
                    else:
                        error_msg = response.json().get('detail', 'Rate limit exceeded')
                        return False, None, f"Replicate rate limit: {error_msg}. Попробуйте позже."

                if response.status_code not in [200, 201]:
                    error_msg = response.json().get('detail', response.text[:200])
                    logger.error(f"❌ Replicate ошибка: {response.status_code} - {error_msg}")
                    return False, None, f"Replicate ошибка: {error_msg}"

                prediction = response.json()

                # Проверяем, завершён ли запрос синхронно (Prefer: wait)
                if prediction.get('status') == 'succeeded':
                    output = prediction.get('output')
                    if output:
                        image_url = output[0] if isinstance(output, list) else output
                        img_response = requests.get(image_url, timeout=60)
                        if img_response.status_code == 200:
                            logger.info(f"✅ {model_name} изображение создано (синхронно)")
                            return True, img_response.content, ""

                prediction_id = prediction['id']
                get_url = prediction.get('urls', {}).get('get', f"{self.api_url}/{prediction_id}")

                # Ждем завершения (polling)
                max_wait = self.config.timeout
                waited = 0
                poll_interval = 2

                while waited < max_wait:
                    time.sleep(poll_interval)
                    waited += poll_interval

                    status_response = requests.get(get_url, headers=headers, timeout=30)
                    if status_response.status_code != 200:
                        continue

                    status_data = status_response.json()
                    status = status_data.get('status')

                    if status == 'succeeded':
                        output = status_data.get('output')
                        if output:
                            image_url = output[0] if isinstance(output, list) else output
                            # Скачиваем изображение
                            img_response = requests.get(image_url, timeout=60)
                            if img_response.status_code == 200:
                                logger.info(f"✅ {model_name} изображение создано")
                                return True, img_response.content, ""
                        return False, None, "Пустой output от Replicate"

                    elif status == 'failed':
                        error = status_data.get('error', 'Unknown error')
                        logger.error(f"❌ {model_name} failed: {error}")
                        return False, None, f"{model_name} ошибка: {error}"

                    elif status == 'canceled':
                        return False, None, "Генерация отменена"

                    logger.debug(f"⏳ {model_name} статус: {status}, ждем...")

                return False, None, f"Таймаут генерации ({max_wait}с)"

            except Exception as e:
                logger.error(f"❌ Replicate ошибка: {e}")
                if attempt < self.MAX_RETRIES:
                    wait_time = self.INITIAL_BACKOFF * (2 ** attempt)
                    logger.warning(f"⏳ Повтор через {wait_time}с...")
                    time.sleep(wait_time)
                    continue
                return False, None, str(e)

        return False, None, "Превышено количество попыток"

    def _parse_retry_after(self, response: requests.Response) -> Optional[int]:
        """Извлекает время ожидания из ответа rate limit"""
        try:
            # Из заголовка Retry-After
            retry_header = response.headers.get('Retry-After')
            if retry_header:
                return int(retry_header)

            # Из тела ответа (Replicate часто указывает "resets in ~Xs")
            import re
            text = response.text
            match = re.search(r'resets in ~?(\d+)s', text)
            if match:
                return int(match.group(1)) + 1  # +1 для надёжности
        except:
            pass
        return None

    def _enhance_prompt_for_flux(self, prompt: str) -> str:
        """Улучшает промпт для Flux"""
        return f"""Professional e-commerce product infographic slide.
{prompt}
Style: clean, modern, minimalist, commercial photography.
No text, no watermarks, no logos. High quality, sharp details."""

    def _enhance_prompt_for_sdxl(self, prompt: str) -> str:
        """Улучшает промпт для SDXL"""
        return f"""Professional product photography for e-commerce, {prompt},
clean background, studio lighting, high quality, sharp focus,
commercial style, minimalist design"""


class GenApiImageGenerator(ImageGenerator):
    """Gen-API.ru — российский агрегатор генеративных сетей (рубли).

    Контракт (сверен с https://gen-api.ru/docs, июль 2026):
    - POST {BASE_URL}/networks/<slug>  -> {"request_id": int}
    - GET  {BASE_URL}/request/get/<request_id> ->
      {"status": "processing|success|error", "result": [...], "cost": ...}
    Slugs: t2i — config.gen_api_model, i2i — config.gen_api_edit_model.
    Исходное изображение для i2i — поле image_urls (per-network параметр).
    """

    BASE_URL = "https://api.gen-api.ru/api/v1"

    def __init__(self, config: ImageGenerationConfig):
        self.config = config

    def _headers(self, *, multipart: bool = False):
        headers = {
            "Authorization": f"Bearer {self.config.gen_api_key}",
            "Accept": "application/json",
        }
        # requests сам добавляет boundary для multipart. Жёсткий application/json
        # превращает files_array в обычные строки и Flux 2 отвечает HTTP 422.
        if not multipart:
            headers["Content-Type"] = "application/json"
        return headers

    def _submit_and_poll(
        self,
        network: str,
        payload: dict,
        files: Optional[List[Tuple[str, Tuple[str, bytes, str]]]] = None,
    ) -> Tuple[bool, Optional[bytes], str]:
        try:
            request_kwargs: Dict[str, Any] = {
                "headers": self._headers(multipart=bool(files)),
                "timeout": 30,
            }
            if files:
                request_kwargs["data"] = payload
                request_kwargs["files"] = files
            else:
                request_kwargs["json"] = payload
            response = requests.post(
                f"{self.BASE_URL}/networks/{network}",
                **request_kwargs,
            )
            if response.status_code != 200:
                return False, None, f"Gen-API {network}: HTTP {response.status_code} {response.text[:600]}"
            request_id = (response.json() or {}).get("request_id")
            if not request_id:
                return False, None, f"Gen-API {network}: не получен request_id"

            waited, poll_interval = 0, 3
            while waited < self.config.timeout:
                time.sleep(poll_interval)
                waited += poll_interval
                poll = requests.get(
                    f"{self.BASE_URL}/request/get/{request_id}",
                    headers=self._headers(),
                    timeout=30,
                )
                if poll.status_code != 200:
                    continue
                data = poll.json() or {}
                status = data.get("status")
                if status == "success":
                    urls = data.get("result") or data.get("output") or []
                    if isinstance(urls, str):
                        urls = [urls]
                    if urls:
                        img = requests.get(urls[0], timeout=60)
                        if img.status_code == 200:
                            return True, img.content, ""
                    return False, None, f"Gen-API {network}: пустой результат"
                if status in ("failed", "error"):
                    err = data.get("error") or data.get("message")
                    if not err:
                        # Причина отказа часто лежит в full_response (объекты провайдера)
                        full = data.get("full_response") or data.get("result")
                        if full:
                            err = json.dumps(full, ensure_ascii=False)
                    err = str(err or "unknown")
                    if is_censorship_refusal(err):
                        return False, None, f"NSFW: {err[:200]}"
                    return False, None, f"Gen-API {network}: {err[:300]}"
            return False, None, f"Gen-API {network}: таймаут ({self.config.timeout}с)"
        except requests.exceptions.Timeout:
            return False, None, f"Gen-API {network}: таймаут запроса"
        except Exception as e:
            logger.error(f"Gen-API {network} ошибка: {e}")
            return False, None, f"Gen-API {network}: {e}"

    @staticmethod
    def _snap16_up(value: int) -> int:
        """Gen-API требует размеры, кратные 16 (HTTP 422 иначе).

        Округляем ВВЕРХ: холст чуть больше, затем fit_image_to_size кропает
        до точного запрошенного размера без полос и искажений.
        """
        v = max(256, int(value))
        return ((v + 15) // 16) * 16

    def generate(
        self,
        prompt: str,
        width: int = 900,
        height: int = 1200,
        reference_image_url: Optional[str] = None,
    ) -> Tuple[bool, Optional[bytes], str]:
        payload = {
            "prompt": prompt,
            "width": self._snap16_up(width),
            "height": self._snap16_up(height),
        }
        files = None
        if reference_image_url:
            try:
                source = requests.get(reference_image_url, timeout=60)
                if source.status_code != 200:
                    return False, None, f"Gen-API: исходное фото HTTP {source.status_code}"
                files = [self._multipart_image(source.content, 0)]
            except Exception as exc:
                return False, None, f"Gen-API: не скачалось исходное фото: {exc}"
        ok, data, err = self._submit_and_poll(
            self.config.gen_api_model,
            payload,
            files=files,
        )
        if ok and data:
            data = fit_image_to_size(data, width, height)
        return ok, data, err

    def edit(
        self,
        prompt: str,
        source_image_url: Optional[str] = None,
        source_image_bytes: Optional[bytes] = None,
        additional_source_images: Optional[List[bytes]] = None,
        mask_bytes: Optional[bytes] = None,
        input_fidelity: Optional[str] = None,
        quality: Optional[str] = None,
        width: int = 900,
        height: int = 1200,
    ) -> Tuple[bool, Optional[bytes], str]:
        if not source_image_url and not source_image_bytes:
            return False, None, "Gen-API edit: не передано исходное изображение"
        if source_image_bytes is None:
            try:
                source = requests.get(source_image_url, timeout=60)
                if source.status_code != 200:
                    return False, None, f"Gen-API edit: исходное фото HTTP {source.status_code}"
                source_image_bytes = source.content
            except Exception as exc:
                return False, None, f"Gen-API edit: не скачалось исходное фото: {exc}"
        payload = {"prompt": prompt}
        images = [bytes(source_image_bytes)]
        for image in additional_source_images or []:
            if not isinstance(image, (bytes, bytearray)) or not image:
                return False, None, "Gen-API edit: некорректный дополнительный референс"
            images.append(bytes(image))
        if len(images) > 10:
            return False, None, "Gen-API edit: разрешено не более 10 референсов"
        try:
            files = [self._multipart_image(image, index) for index, image in enumerate(images)]
        except ValueError as exc:
            return False, None, f"Gen-API edit: {exc}"
        if self.config.gen_api_edit_model == "flux-2":
            payload["width"] = self._snap16_up(width)
            payload["height"] = self._snap16_up(height)
        return self._submit_and_poll(
            self.config.gen_api_edit_model,
            payload,
            files=files,
        )

    @staticmethod
    def _multipart_image(
        image_bytes: bytes,
        index: int,
    ) -> Tuple[str, Tuple[str, bytes, str]]:
        """Build the exact Gen-API files_array part: repeated image_urls[]."""
        if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
            raise ValueError("пустой файл изображения")
        data = bytes(image_bytes)
        if len(data) > 25 * 1024 * 1024:
            raise ValueError("референс больше 25 МБ")
        extension, mime = "png", "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            extension, mime = "jpg", "image/jpeg"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            extension, mime = "webp", "image/webp"
        return "image_urls[]", (f"reference-{index}.{extension}", data, mime)


class AITunnelImageGenerator(ImageGenerator):
    """AITunnel — OpenAI-совместимый российский агрегатор (рубли).

    Контракт (сверен с https://docs.aitunnel.ru, июль 2026):
    - POST {BASE_URL}/images/generations — JSON (model, prompt, n, size, response_format)
    - POST {BASE_URL}/images/edits — multipart (image, model, prompt, ...)
    Ключ формата sk-aitunnel-..., Bearer.
    """

    BASE_URL = "https://api.aitunnel.ru/v1"

    def __init__(self, config: ImageGenerationConfig):
        self.config = config

    def _auth(self):
        return {"Authorization": f"Bearer {self.config.aitunnel_api_key}"}

    # Seedream отклоняет запросы меньше ~3,69 Мп (1920x1920)
    _SEEDREAM_MIN_PIXELS = 3686400

    def _effective_size(self, width, height, model):
        """Подгоняет размер под требования модели AITunnel.

        Все image-модели требуют кратность 16; Seedream дополнительно —
        минимум _SEEDREAM_MIN_PIXELS. Пропорции сохраняются, результат
        не уменьшается обратно: провайдер отдаёт столько, сколько попросили.
        """
        import math

        def snap16(v):
            return ((max(256, int(v)) + 15) // 16) * 16

        model_name = (model or "").lower()
        if model_name.startswith("gpt-image"):
            ratio = max(1, int(width)) / max(1, int(height))
            if ratio < 0.9:
                return 1024, 1536
            if ratio > 1.1:
                return 1536, 1024
            return 1024, 1024
        w, h = snap16(width), snap16(height)
        if "seedream" in (model or "").lower() and w * h < self._SEEDREAM_MIN_PIXELS:
            k = math.sqrt(self._SEEDREAM_MIN_PIXELS / (w * h))
            w, h = snap16(math.ceil(w * k)), snap16(math.ceil(h * k))
        return w, h

    def _fail(self, response) -> Tuple[bool, Optional[bytes], str]:
        err = response.text[:300]
        if is_censorship_refusal(err):
            return False, None, f"NSFW: {err[:200]}"
        return False, None, f"AITunnel: HTTP {response.status_code} {err}"

    def _extract_image(self, data: dict) -> Tuple[bool, Optional[bytes], str]:
        items = data.get("data") or []
        if not items:
            return False, None, "AITunnel: пустой ответ"
        first = items[0] or {}
        if first.get("b64_json"):
            try:
                return True, base64.b64decode(first["b64_json"]), ""
            except Exception:
                return False, None, "AITunnel: битый base64 в ответе"
        if first.get("url"):
            img = requests.get(first["url"], timeout=60)
            if img.status_code == 200:
                return True, img.content, ""
        return False, None, "AITunnel: нет изображения в ответе"

    @staticmethod
    def _multipart_image(image_bytes: bytes, name: str) -> Tuple[str, bytes, str]:
        """Label local references by their actual byte signature."""
        if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
            raise ValueError("пустой файл изображения")
        data = bytes(image_bytes)
        extension, mime = "png", "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            extension, mime = "jpg", "image/jpeg"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            extension, mime = "webp", "image/webp"
        return f"{name}.{extension}", data, mime

    def generate(
        self,
        prompt: str,
        width: int = 900,
        height: int = 1200,
        reference_image_url: Optional[str] = None,
    ) -> Tuple[bool, Optional[bytes], str]:
        w, h = self._effective_size(width, height, self.config.aitunnel_model)
        payload = {
            "model": self.config.aitunnel_model,
            "prompt": prompt,
            "n": 1,
            "size": f"{w}x{h}",
            "response_format": "b64_json",
        }
        try:
            response = requests.post(
                f"{self.BASE_URL}/images/generations",
                json=payload,
                headers={**self._auth(), "Content-Type": "application/json"},
                timeout=self.config.timeout,
            )
            if response.status_code != 200:
                return self._fail(response)
            return self._extract_image(response.json() or {})
        except requests.exceptions.Timeout:
            return False, None, f"AITunnel: таймаут ({self.config.timeout}с)"
        except Exception as e:
            logger.error(f"AITunnel ошибка: {e}")
            return False, None, f"AITunnel: {e}"

    def edit(
        self,
        prompt: str,
        source_image_url: Optional[str] = None,
        source_image_bytes: Optional[bytes] = None,
        additional_source_images: Optional[List[bytes]] = None,
        mask_bytes: Optional[bytes] = None,
        input_fidelity: Optional[str] = None,
        quality: Optional[str] = None,
        width: int = 900,
        height: int = 1200,
    ) -> Tuple[bool, Optional[bytes], str]:
        if source_image_bytes is None:
            if not source_image_url:
                return False, None, "AITunnel edit: не передано исходное изображение"
            try:
                src = requests.get(source_image_url, timeout=60)
                if src.status_code != 200:
                    return False, None, f"AITunnel edit: исходное фото HTTP {src.status_code}"
                source_image_bytes = src.content
            except Exception as e:
                return False, None, f"AITunnel edit: не скачалось исходное фото: {e}"
        try:
            additional = list(additional_source_images or [])
            for image in additional:
                if not isinstance(image, (bytes, bytearray)) or not image:
                    return False, None, "AITunnel edit: некорректный дополнительный референс"
            if additional:
                files = [("image[]", self._multipart_image(source_image_bytes, "source"))]
                files.extend(
                    (
                        "image[]",
                        self._multipart_image(bytes(image), f"reference-{index}"),
                    )
                    for index, image in enumerate(additional, start=1)
                )
                if mask_bytes:
                    files.append(("mask", ("mask.png", mask_bytes, "image/png")))
            else:
                files = {"image": self._multipart_image(source_image_bytes, "source")}
                if mask_bytes:
                    files["mask"] = ("mask.png", mask_bytes, "image/png")
            request_data = {
                "model": self.config.aitunnel_edit_model,
                "prompt": prompt,
                "n": "1",
                "size": "{}x{}".format(
                    *self._effective_size(width, height, self.config.aitunnel_edit_model)
                ),
                "response_format": "b64_json",
            }
            if input_fidelity in ("high", "low") and self.config.aitunnel_edit_model.startswith("gpt-image"):
                request_data["input_fidelity"] = input_fidelity
            if (
                quality in ("low", "medium", "high", "auto")
                and self.config.aitunnel_edit_model.startswith("gpt-image")
            ):
                request_data["quality"] = quality
            response = requests.post(
                f"{self.BASE_URL}/images/edits",
                files=files,
                data=request_data,
                headers=self._auth(),
                timeout=self.config.timeout,
            )
            if response.status_code != 200:
                return self._fail(response)
            return self._extract_image(response.json() or {})
        except requests.exceptions.Timeout:
            return False, None, f"AITunnel: таймаут ({self.config.timeout}с)"
        except Exception as e:
            logger.error(f"AITunnel edit ошибка: {e}")
            return False, None, f"AITunnel: {e}"


class ImageGenerationService:
    """
    Главный сервис генерации изображений

    Пример использования:
        config = ImageGenerationConfig(
            provider=ImageProvider.OPENAI_DALLE,
            api_key="sk-..."
        )
        service = ImageGenerationService(config)
        success, image_bytes, error = service.generate_slide_image(
            slide_data={"title": "...", "image_concept": {...}},
            product_photos=["https://..."]
        )
    """

    def __init__(self, config: ImageGenerationConfig):
        self.config = config

        # Создаем генератор под провайдера
        if config.provider == ImageProvider.OPENROUTER:
            self.generator = OpenRouterImageGenerator(config)
        elif config.provider == ImageProvider.FLUXAPI:
            self.generator = FluxAPIImageGenerator(config)
        elif config.provider == ImageProvider.TENSORART:
            self.generator = TensorArtImageGenerator(config)
        elif config.provider == ImageProvider.TOGETHER_FLUX:
            self.generator = TogetherImageGenerator(config)
        elif config.provider == ImageProvider.OPENAI_DALLE:
            self.generator = OpenAIImageGenerator(config)
        elif config.provider == ImageProvider.GEN_API:
            self.generator = GenApiImageGenerator(config)
        elif config.provider == ImageProvider.AITUNNEL:
            self.generator = AITunnelImageGenerator(config)
        else:
            # Replicate (FLUX_PRO, SDXL)
            self.generator = ReplicateImageGenerator(config)

    def generate_from_prompt(
        self,
        prompt: str,
        width: Optional[int] = None,
        height: Optional[int] = None,
        reference_image_url: Optional[str] = None
    ) -> Tuple[bool, Optional[bytes], str]:
        """
        Генерирует изображение по промпту

        Args:
            prompt: Текстовый промпт
            width: Ширина (по умолчанию 1440)
            height: Высота (по умолчанию 810)
            reference_image_url: URL референсного изображения

        Returns:
            Tuple[success, image_bytes, error]
        """
        w = width or self.config.default_width
        h = height or self.config.default_height

        return self.generator.generate(prompt, w, h, reference_image_url)

    def edit_image(
        self,
        prompt: str,
        source_image_url: Optional[str] = None,
        source_image_bytes: Optional[bytes] = None,
        additional_source_images: Optional[List[bytes]] = None,
        mask_bytes: Optional[bytes] = None,
        input_fidelity: Optional[str] = None,
        quality: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Tuple[bool, Optional[bytes], str]:
        """Image-to-image: сцена вокруг товара по исходному фото (режим A)."""
        return self.generator.edit(
            prompt=prompt,
            source_image_url=source_image_url,
            source_image_bytes=source_image_bytes,
            additional_source_images=additional_source_images,
            mask_bytes=mask_bytes,
            input_fidelity=input_fidelity,
            quality=quality,
            width=width or self.config.default_width,
            height=height or self.config.default_height,
        )

    def generate_slide_image(
        self,
        slide_data: Dict,
        product_photos: Optional[List[str]] = None,
        product_title: str = ""
    ) -> Tuple[bool, Optional[bytes], str]:
        """Generate only a text-free background for a fact-safe slide.

        Product photos, product title and visible copy intentionally never enter
        the image model.  The foreground and typography are added downstream.
        """
        image_concept = slide_data.get("image_concept") or {}
        scene_key = image_concept.get("scene_key", "luxury")
        return self.generate_background(scene_key)

    def generate_background(
        self,
        scene_key: str = "luxury",
        *,
        width: int = 900,
        height: int = 1200,
    ) -> Tuple[bool, Optional[bytes], str]:
        """Production model boundary: approved scene key in, empty canvas out."""
        from services.infographic_prompts import (
            ATMOSPHERE_PRESETS,
            build_background_prompt,
        )
        from services.infographic_quality import ImageQualityError, canonicalize_image

        if scene_key not in ATMOSPHERE_PRESETS:
            return False, None, f"Неизвестная сцена: {scene_key}"
        success, image_bytes, error = self.generate_from_prompt(
            prompt=build_background_prompt(scene_key),
            width=width,
            height=height,
            reference_image_url=None,
        )
        if not success or not image_bytes:
            return False, None, error or "Провайдер не вернул фон"
        try:
            return True, canonicalize_image(image_bytes, (width, height)), ""
        except ImageQualityError as exc:
            return False, None, str(exc)

    def generate_all_slides(
        self,
        slides: List[Dict],
        product_photos: Optional[List[str]] = None,
        product_title: str = ""
    ) -> List[Dict]:
        """
        Генерирует изображения для всех слайдов

        Args:
            slides: Список слайдов из Rich content
            product_photos: Фотографии товара
            product_title: Название товара

        Returns:
            Список результатов [{slide_number, success, image_bytes, error}]
        """
        results = []

        # Определяем паузу между запросами в зависимости от провайдера
        # FluxAPI, TensorArt, Together AI, OpenAI - высокий лимит
        # Replicate free tier: 6 запросов/минуту
        high_limit_providers = (
            ImageProvider.FLUXAPI,
            ImageProvider.TENSORART,
            ImageProvider.TOGETHER_FLUX,
            ImageProvider.OPENAI_DALLE
        )
        if self.config.provider in high_limit_providers:
            pause_between_requests = 2  # Высокий лимит
        else:
            pause_between_requests = 12  # Replicate: 6/мин = 10с + запас

        for i, slide in enumerate(slides):
            slide_num = slide.get('number', i + 1)
            logger.info(f"📸 Генерация слайда {slide_num}/{len(slides)}...")

            success, image_bytes, error = self.generate_slide_image(
                slide_data=slide,
                product_photos=product_photos,
                product_title=product_title
            )

            results.append({
                'slide_number': slide_num,
                'slide_type': slide.get('type', 'unknown'),
                'success': success,
                'image_bytes': image_bytes,
                'error': error
            })

            # Пауза между запросами для соблюдения rate limit
            if i < len(slides) - 1:
                logger.info(f"⏳ Пауза {pause_between_requests}с перед следующим слайдом...")
                time.sleep(pause_between_requests)

        return results

    def _build_slide_prompt(self, slide_data: Dict, product_title: str = "") -> str:
        """Compatibility helper returning the same background-only prompt."""
        from services.infographic_prompts import (
            ATMOSPHERE_PRESETS,
            build_background_prompt,
        )

        image_concept = slide_data.get("image_concept") or {}
        scene_key = image_concept.get("scene_key", "luxury")
        if scene_key not in ATMOSPHERE_PRESETS:
            scene_key = "luxury"
        return build_background_prompt(scene_key)

    def test_connection(self) -> Tuple[bool, str]:
        """Тестирует подключение к API"""
        try:
            success, _, error = self.generate_from_prompt(
                prompt="Simple test image: white background with a small blue dot in center",
                width=256,
                height=256
            )
            if success:
                return True, f"Подключение к {self.config.provider.value} успешно"
            return False, error
        except Exception as e:
            return False, str(e)


# ============================================================================
# УТИЛИТЫ
# ============================================================================

def get_available_providers() -> Dict[str, Dict]:
    """Возвращает доступные провайдеры с описанием"""
    return {
        provider.value: {
            "name": info["name"],
            "description": info["description"],
            "price": info["price_per_image"],
            "max_size": info["max_size"],
            "supports_reference": info["supports_reference"],
            "recommended": info["recommended"]
        }
        for provider, info in PROVIDER_CONFIG.items()
    }


def create_image_service(settings) -> Optional[ImageGenerationService]:
    """Создает сервис генерации изображений из настроек"""
    config = ImageGenerationConfig.from_settings(settings)
    if config:
        return ImageGenerationService(config)
    return None


# Глобальный instance
_image_service: Optional[ImageGenerationService] = None


def get_image_service(settings=None) -> Optional[ImageGenerationService]:
    """Получает или создает глобальный сервис"""
    global _image_service

    if settings is None:
        return _image_service

    _image_service = create_image_service(settings)
    return _image_service


def reset_image_service():
    """Сбрасывает глобальный сервис"""
    global _image_service
    _image_service = None
