# -*- coding: utf-8 -*-
"""
Image Generation Service - Генерация изображений для инфографики WB

Поддерживаемые провайдеры:
- FluxAPI.ai (простой API, trial credits)
- Tensor.art (дешёвый, $0.003/credit)
- Together AI Flux (быстрый, платный)
- OpenAI DALL-E 3 (лучшее качество, дорогой)
- Replicate Flux/SDXL (требует оплату)

Для работы нужны API ключи:
- FluxAPI: https://fluxapi.ai/ (есть trial)
- Tensor.art: https://tams.tensor.art/
- Together AI: https://api.together.xyz/settings/api-keys
- OpenAI: https://platform.openai.com/api-keys
- Replicate: https://replicate.com/account/api-tokens
"""

import json
import logging
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


class ImageProvider(Enum):
    """Поддерживаемые провайдеры генерации изображений"""
    FLUXAPI = "fluxapi"  # FluxAPI.ai - простой API
    TENSORART = "tensorart"  # Tensor.art - дешёвый
    TOGETHER_FLUX = "together_flux"  # Together AI Flux
    OPENAI_DALLE = "openai_dalle"  # DALL-E 3
    FLUX_PRO = "flux_pro"  # Flux.1 Pro через Replicate
    SDXL = "sdxl"  # Stable Diffusion XL через Replicate


# Конфигурация провайдеров
PROVIDER_CONFIG = {
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
    # Together AI specific
    together_api_key: str = ""
    # FluxAPI specific
    fluxapi_key: str = ""
    # TensorArt specific
    tensorart_app_id: str = ""
    tensorart_api_key: str = ""  # Private key for signing
    # Общие
    default_width: int = 1440
    default_height: int = 810
    timeout: int = 120

    @classmethod
    def from_settings(cls, settings) -> Optional['ImageGenerationConfig']:
        """Создает конфигурацию из настроек"""
        if not hasattr(settings, 'image_gen_enabled') or not settings.image_gen_enabled:
            return None

        provider_str = getattr(settings, 'image_gen_provider', 'fluxapi')  # FluxAPI по умолчанию
        try:
            provider = ImageProvider(provider_str)
        except ValueError:
            provider = ImageProvider.FLUXAPI

        api_key = ""
        replicate_key = ""
        together_key = ""
        fluxapi_key = ""
        tensorart_app_id = ""
        tensorart_api_key = ""

        if provider == ImageProvider.FLUXAPI:
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

        if not api_key and not replicate_key and not together_key and not fluxapi_key and not tensorart_api_key:
            logger.warning("Image generation включен, но API ключ не указан")
            return None

        return cls(
            provider=provider,
            api_key=api_key,
            replicate_api_key=replicate_key,
            together_api_key=together_key,
            fluxapi_key=fluxapi_key,
            tensorart_app_id=tensorart_app_id,
            tensorart_api_key=tensorart_api_key,
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
        if config.provider == ImageProvider.FLUXAPI:
            self.generator = FluxAPIImageGenerator(config)
        elif config.provider == ImageProvider.TENSORART:
            self.generator = TensorArtImageGenerator(config)
        elif config.provider == ImageProvider.TOGETHER_FLUX:
            self.generator = TogetherImageGenerator(config)
        elif config.provider == ImageProvider.OPENAI_DALLE:
            self.generator = OpenAIImageGenerator(config)
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
