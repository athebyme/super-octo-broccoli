# -*- coding: utf-8 -*-
"""
Image Generation Service - Генерация изображений для инфографики WB

Поддерживаемые провайдеры:
- OpenAI DALL-E 3 (лучшее качество, понимает русский)
- Flux.1 Pro через Replicate (быстрый, высокое качество)
- Stable Diffusion XL через Replicate (бюджетный)

Для работы нужны API ключи:
- OpenAI: https://platform.openai.com/api-keys
- Replicate: https://replicate.com/account/api-tokens
"""

import json
import logging
import requests
import time
import base64
import io
from abc import ABC, abstractmethod
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass
from enum import Enum
from PIL import Image

logger = logging.getLogger(__name__)


class ImageProvider(Enum):
    """Поддерживаемые провайдеры генерации изображений"""
    OPENAI_DALLE = "openai_dalle"  # DALL-E 3
    FLUX_PRO = "flux_pro"  # Flux.1 Pro через Replicate
    SDXL = "sdxl"  # Stable Diffusion XL через Replicate


# Конфигурация провайдеров
PROVIDER_CONFIG = {
    ImageProvider.OPENAI_DALLE: {
        "name": "OpenAI DALL-E 3",
        "description": "Лучшее качество, понимает русский язык, дорогой",
        "api_url": "https://api.openai.com/v1/images/generations",
        "price_per_image": "$0.04-0.12",
        "max_size": "1792x1024",
        "supports_reference": False,  # Не поддерживает img2img напрямую
        "recommended": True
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
    }
}


@dataclass
class ImageGenerationConfig:
    """Конфигурация генерации изображений"""
    provider: ImageProvider
    api_key: str
    # OpenAI specific
    openai_model: str = "dall-e-3"
    openai_quality: str = "standard"  # standard или hd
    openai_style: str = "vivid"  # vivid или natural
    # Replicate specific
    replicate_api_key: str = ""
    # Общие
    default_width: int = 1440
    default_height: int = 810
    timeout: int = 120

    @classmethod
    def from_settings(cls, settings) -> Optional['ImageGenerationConfig']:
        """Создает конфигурацию из настроек"""
        if not hasattr(settings, 'image_gen_enabled') or not settings.image_gen_enabled:
            return None

        provider_str = getattr(settings, 'image_gen_provider', 'openai_dalle')
        try:
            provider = ImageProvider(provider_str)
        except ValueError:
            provider = ImageProvider.OPENAI_DALLE

        api_key = ""
        replicate_key = ""

        if provider == ImageProvider.OPENAI_DALLE:
            api_key = getattr(settings, 'openai_api_key', '') or ''
        else:
            replicate_key = getattr(settings, 'replicate_api_key', '') or ''

        if not api_key and not replicate_key:
            logger.warning("Image generation включен, но API ключ не указан")
            return None

        return cls(
            provider=provider,
            api_key=api_key,
            replicate_api_key=replicate_key,
            openai_quality=getattr(settings, 'openai_image_quality', 'standard') or 'standard',
            openai_style=getattr(settings, 'openai_image_style', 'vivid') or 'vivid',
            default_width=getattr(settings, 'image_gen_width', 1440) or 1440,
            default_height=getattr(settings, 'image_gen_height', 810) or 810
        )


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


class OpenAIImageGenerator(ImageGenerator):
    """Генератор изображений через OpenAI DALL-E 3"""

    def __init__(self, config: ImageGenerationConfig):
        self.config = config
        self.api_url = "https://api.openai.com/v1/images/generations"

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
                timeout=self.config.timeout
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


class ReplicateImageGenerator(ImageGenerator):
    """Генератор изображений через Replicate API (Flux, SDXL)"""

    def __init__(self, config: ImageGenerationConfig):
        self.config = config
        self.api_url = "https://api.replicate.com/v1/predictions"

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
        """Генерация через Flux.1 Pro"""

        # Flux поддерживает произвольные размеры
        payload = {
            "version": "2c09d6e1903deb32a364c1a29f88dd3a639665769a97e9a5f90e0b5bc666fa33",
            "input": {
                "prompt": self._enhance_prompt_for_flux(prompt),
                "width": width,
                "height": height,
                "num_outputs": 1,
                "output_format": "png",
                "guidance": 3.5,
                "num_inference_steps": 28
            }
        }

        # Если есть референс - добавляем
        if reference_url:
            payload["input"]["image"] = reference_url
            payload["input"]["prompt_strength"] = 0.8

        return self._run_replicate_prediction(payload, api_key, "Flux.1 Pro")

    def _generate_sdxl(
        self,
        prompt: str,
        width: int,
        height: int,
        reference_url: Optional[str],
        api_key: str
    ) -> Tuple[bool, Optional[bytes], str]:
        """Генерация через SDXL"""

        # SDXL лучше работает с размерами кратными 8
        width = (width // 8) * 8
        height = (height // 8) * 8

        payload = {
            "version": "39ed52f2a78e934b3ba6e2a89f5b1c712de7dfea535525255b1aa35c5565e08b",
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

        return self._run_replicate_prediction(payload, api_key, "SDXL")

    def _run_replicate_prediction(
        self,
        payload: Dict,
        api_key: str,
        model_name: str
    ) -> Tuple[bool, Optional[bytes], str]:
        """Запускает prediction на Replicate и ждет результат"""

        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json"
        }

        try:
            logger.info(f"🎨 {model_name} генерация запущена...")

            # Создаем prediction
            response = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code not in [200, 201]:
                error_msg = response.json().get('detail', response.text[:200])
                logger.error(f"❌ Replicate ошибка: {response.status_code} - {error_msg}")
                return False, None, f"Replicate ошибка: {error_msg}"

            prediction = response.json()
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
            return False, None, str(e)

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
        if config.provider == ImageProvider.OPENAI_DALLE:
            self.generator = OpenAIImageGenerator(config)
        else:
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

    def generate_slide_image(
        self,
        slide_data: Dict,
        product_photos: Optional[List[str]] = None,
        product_title: str = ""
    ) -> Tuple[bool, Optional[bytes], str]:
        """
        Генерирует изображение для слайда Rich-контента

        Args:
            slide_data: Данные слайда из AI (title, subtitle, image_concept, etc.)
            product_photos: Список URL фотографий товара
            product_title: Название товара

        Returns:
            Tuple[success, image_bytes, error]
        """
        # Строим промпт из данных слайда
        prompt = self._build_slide_prompt(slide_data, product_title)

        # Выбираем референс если есть
        reference_url = None
        if product_photos and self.config.provider != ImageProvider.OPENAI_DALLE:
            # Для Flux/SDXL можем использовать референс
            reference_url = product_photos[0]

        return self.generate_from_prompt(
            prompt=prompt,
            width=1440,
            height=810,
            reference_image_url=reference_url
        )

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

            # Небольшая пауза между запросами
            if i < len(slides) - 1:
                time.sleep(1)

        return results

    def _build_slide_prompt(self, slide_data: Dict, product_title: str = "") -> str:
        """Строит промпт для слайда из его данных"""

        slide_type = slide_data.get('type', 'feature')
        title = slide_data.get('title', '')
        subtitle = slide_data.get('subtitle', '')
        image_concept = slide_data.get('image_concept', {})

        # Базовая информация
        prompt_parts = [
            f"Product infographic slide for: {product_title}" if product_title else "Product infographic slide",
            f"Slide type: {slide_type}",
        ]

        # Добавляем концепцию изображения
        if image_concept:
            main_obj = image_concept.get('main_object', '')
            background = image_concept.get('background', '')
            composition = image_concept.get('composition', 'center')
            mood = image_concept.get('mood', '')

            if main_obj:
                prompt_parts.append(f"Main visual: {main_obj}")
            if background:
                prompt_parts.append(f"Background: {background}")
            if mood:
                prompt_parts.append(f"Mood/style: {mood}")
            if composition:
                prompt_parts.append(f"Composition: product positioned {composition}")

        # Контекст из заголовков
        if title:
            prompt_parts.append(f"Theme: {title}")
        if subtitle:
            prompt_parts.append(f"Context: {subtitle}")

        return "\n".join(prompt_parts)

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
