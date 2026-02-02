# -*- coding: utf-8 -*-
"""
AI Service - Универсальный модуль для интеграции с AI провайдерами

Поддерживает:
- Cloud.ru Foundation Models (основной провайдер с OAuth2)
- OpenAI-совместимые API
- Кастомные инструкции для разных задач
- Валидацию ответов AI
- Автоматическую ротацию токенов для Cloud.ru
"""
import json
import re
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import requests

logger = logging.getLogger(__name__)


# ============================================================================
# CLOUD.RU OAUTH2 TOKEN MANAGER
# ============================================================================

class CloudRuTokenManager:
    """
    Менеджер токенов для Cloud.ru Foundation Models API
    Использует OAuth2 Client Credentials flow

    Cloud.ru требует:
    - client_id и client_secret для получения access_token
    - Токены имеют ограниченное время жизни (обычно 1 час)
    - Автоматическая ротация токена при истечении
    """

    # URL для получения токена Cloud.ru
    TOKEN_URL = "https://auth.iam.cloud.ru/realms/platform/protocol/openid-connect/token"

    # Буфер времени до истечения токена (секунды) - обновляем заранее
    TOKEN_REFRESH_BUFFER = 300  # 5 минут

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._lock = threading.Lock()

    def get_access_token(self) -> Optional[str]:
        """
        Получает действующий access token
        Автоматически обновляет если истёк или скоро истечёт

        Returns:
            access_token или None при ошибке
        """
        with self._lock:
            current_time = time.time()

            # Проверяем нужно ли обновить токен
            if (self._access_token is None or
                current_time >= self._token_expires_at - self.TOKEN_REFRESH_BUFFER):

                logger.info("🔄 Получаем новый access token от Cloud.ru...")
                success = self._fetch_new_token()
                if not success:
                    return None

            return self._access_token

    def _fetch_new_token(self) -> bool:
        """
        Запрашивает новый access token у Cloud.ru

        Returns:
            True при успехе, False при ошибке
        """
        try:
            payload = {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret
            }

            headers = {
                "Content-Type": "application/x-www-form-urlencoded"
            }

            response = requests.post(
                self.TOKEN_URL,
                data=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code != 200:
                logger.error(f"❌ Cloud.ru OAuth ошибка: {response.status_code}")
                logger.error(f"Response: {response.text[:500]}")
                return False

            data = response.json()

            self._access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)  # По умолчанию 1 час
            self._token_expires_at = time.time() + expires_in

            logger.info(f"✅ Cloud.ru access token получен (expires_in: {expires_in}s)")
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Cloud.ru OAuth network error: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Cloud.ru OAuth error: {e}")
            return False

    def invalidate_token(self):
        """Принудительно инвалидирует текущий токен"""
        with self._lock:
            self._access_token = None
            self._token_expires_at = 0


# Глобальный кэш token managers (по client_id)
_token_managers: Dict[str, CloudRuTokenManager] = {}
_token_managers_lock = threading.Lock()


def get_cloudru_token_manager(client_id: str, client_secret: str) -> CloudRuTokenManager:
    """
    Получает или создает TokenManager для данных credentials
    Использует кэш для переиспользования
    """
    with _token_managers_lock:
        if client_id not in _token_managers:
            _token_managers[client_id] = CloudRuTokenManager(client_id, client_secret)
        return _token_managers[client_id]


def reset_cloudru_token_managers():
    """Сбрасывает все кэшированные token managers"""
    global _token_managers
    with _token_managers_lock:
        _token_managers = {}


class AIProvider(Enum):
    """Поддерживаемые AI провайдеры"""
    OPENAI = "openai"
    CLOUDRU = "cloudru"  # Cloud.ru Foundation Models
    CUSTOM = "custom"  # Любой OpenAI-совместимый API


# Доступные модели для Cloud.ru Foundation Models
CLOUDRU_MODELS = {
    "openai/gpt-oss-120b": {
        "name": "GPT OSS 120B",
        "description": "Универсальная модель для большинства задач",
        "recommended": True
    },
    "deepseek/DeepSeek-R1-Distill-Llama-70B": {
        "name": "DeepSeek R1 Distill Llama 70B",
        "description": "Высокая точность на уровне state-of-the-art решений",
        "recommended": True
    },
    "deepseek/DeepSeek-V3": {
        "name": "DeepSeek V3",
        "description": "Продвинутая модель DeepSeek",
        "recommended": False
    },
    "qwen/Qwen2.5-72B-Instruct": {
        "name": "Qwen 2.5 72B Instruct",
        "description": "Модель от Alibaba для инструкций",
        "recommended": False
    },
    "meta-llama/Llama-3.3-70B-Instruct": {
        "name": "Llama 3.3 70B Instruct",
        "description": "Модель от Meta",
        "recommended": False
    }
}

# Модели OpenAI
OPENAI_MODELS = {
    "gpt-4o-mini": {
        "name": "GPT-4o Mini",
        "description": "Баланс цены и качества",
        "recommended": True
    },
    "gpt-4o": {
        "name": "GPT-4o",
        "description": "Лучшее качество",
        "recommended": False
    },
    "gpt-4-turbo": {
        "name": "GPT-4 Turbo",
        "description": "Быстрая версия GPT-4",
        "recommended": False
    }
}


# ============================================================================
# СИСТЕМНЫЕ ИНСТРУКЦИИ ПО УМОЛЧАНИЮ
# ============================================================================

DEFAULT_INSTRUCTIONS = {
    "category_detection": {
        "name": "Определение категорий WB",
        "description": "Инструкция для определения категории товара на Wildberries",
        "template": """Ты эксперт по классификации товаров для маркетплейса Wildberries.

Твоя задача - определить наиболее подходящую категорию WB для товара на основе его данных.

ДОСТУПНЫЕ КАТЕГОРИИ WB:
{categories_list}

ПРАВИЛА:
1. Выбирай ТОЛЬКО из предоставленного списка категорий
2. Если товар может относиться к нескольким категориям - выбирай наиболее специфичную
3. Учитывай название товара, категорию из источника и характеристики
4. Для интим-товаров используй специализированные категории (Вибраторы, Фаллоимитаторы и т.д.)
5. Если не уверен - выбирай более общую категорию

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{{
    "category_id": <число - ID категории из списка>,
    "category_name": "<название категории>",
    "confidence": <число от 0.0 до 1.0 - уверенность>,
    "reasoning": "<краткое объяснение выбора>"
}}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON без дополнительного текста."""
    },

    "size_parsing": {
        "name": "Парсинг размеров",
        "description": "Инструкция для извлечения размеров и характеристик товара",
        "template": """Ты эксперт по парсингу размеров и характеристик товаров.

Твоя задача - извлечь структурированные данные о размерах из текстового описания.

ВОЗМОЖНЫЕ ХАРАКТЕРИСТИКИ:
{characteristics_list}

ПРАВИЛА:
1. Извлекай только те характеристики, которые явно указаны в тексте
2. Преобразуй единицы измерения в стандартные (см, г, мл)
3. Если указан диапазон (например, "длина 15-18 см") - используй максимальное значение
4. Для размеров одежды используй стандартные обозначения (S, M, L, XL или числовые)
5. Если характеристика не найдена - не включай её в ответ

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{{
    "characteristics": {{
        "Название характеристики": "значение с единицей измерения",
        ...
    }},
    "raw_sizes": ["исходный текст размеров если есть"],
    "has_clothing_sizes": true/false,
    "confidence": <число от 0.0 до 1.0>
}}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON без дополнительного текста."""
    }
}


@dataclass
class AIConfig:
    """Конфигурация AI провайдера"""
    provider: AIProvider
    api_key: str = ""  # API ключ (Bearer token) для всех провайдеров
    api_base_url: str = "https://foundation-models.api.cloud.ru/v1"
    model: str = "openai/gpt-oss-120b"
    temperature: float = 0.3
    max_tokens: int = 2000
    timeout: int = 60
    # Дополнительные параметры
    top_p: float = 0.95
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    # Кастомные инструкции
    custom_category_instruction: str = ""
    custom_size_instruction: str = ""

    @classmethod
    def from_settings(cls, settings) -> Optional['AIConfig']:
        """Создает конфигурацию из настроек автоимпорта"""
        if not hasattr(settings, 'ai_enabled') or not settings.ai_enabled:
            return None

        provider = AIProvider(settings.ai_provider or 'cloudru')

        # Все провайдеры используют API ключ (Bearer token)
        if not settings.ai_api_key:
            logger.warning("AI включен, но API ключ не указан")
            return None

        # Определяем базовый URL в зависимости от провайдера
        if provider == AIProvider.CLOUDRU:
            api_base = settings.ai_api_base_url or "https://foundation-models.api.cloud.ru/v1"
            default_model = "openai/gpt-oss-120b"
        elif provider == AIProvider.CUSTOM:
            api_base = settings.ai_api_base_url or "https://api.openai.com/v1"
            default_model = "gpt-4o-mini"
        else:  # OpenAI
            api_base = "https://api.openai.com/v1"
            default_model = "gpt-4o-mini"

        return cls(
            provider=provider,
            api_key=settings.ai_api_key,
            api_base_url=api_base,
            model=settings.ai_model or default_model,
            temperature=getattr(settings, 'ai_temperature', 0.3) or 0.3,
            max_tokens=getattr(settings, 'ai_max_tokens', 2000) or 2000,
            timeout=getattr(settings, 'ai_timeout', 60) or 60,
            top_p=getattr(settings, 'ai_top_p', 0.95) or 0.95,
            presence_penalty=getattr(settings, 'ai_presence_penalty', 0.0) or 0.0,
            frequency_penalty=getattr(settings, 'ai_frequency_penalty', 0.0) or 0.0,
            custom_category_instruction=getattr(settings, 'ai_category_instruction', '') or '',
            custom_size_instruction=getattr(settings, 'ai_size_instruction', '') or ''
        )


class AIClient:
    """
    Клиент для работы с AI API
    Поддерживает OpenAI-совместимые API (Cloud.ru, OpenAI, Custom)
    """

    def __init__(self, config: AIConfig):
        self.config = config
        self._session = requests.Session()
        self._session.headers.update({
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {config.api_key}'
        })

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict] = None
    ) -> Optional[str]:
        """
        Отправляет запрос на chat completion

        Args:
            messages: Список сообщений [{role: "system/user/assistant", content: "..."}]
            temperature: Температура (опционально, иначе из конфига)
            max_tokens: Максимум токенов (опционально)
            response_format: Формат ответа ({"type": "json_object"} для JSON)

        Returns:
            Текст ответа или None при ошибке
        """
        url = f"{self.config.api_base_url}/chat/completions"

        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
            "top_p": self.config.top_p,
            "presence_penalty": self.config.presence_penalty,
            "frequency_penalty": self.config.frequency_penalty
        }

        # response_format не все модели поддерживают, добавляем опционально
        if response_format and self.config.provider != AIProvider.CLOUDRU:
            payload["response_format"] = response_format

        try:
            logger.info(f"🤖 AI запрос к {self.config.provider.value}: модель={self.config.model}")
            logger.debug(f"Messages: {messages}")
            logger.debug(f"Payload: {json.dumps(payload, ensure_ascii=False)[:500]}")

            response = self._session.post(
                url,
                json=payload,
                timeout=self.config.timeout
            )

            # Логируем ответ для отладки
            if response.status_code != 200:
                logger.error(f"❌ AI HTTP {response.status_code}: {response.text[:500]}")

            response.raise_for_status()

            data = response.json()
            content = data['choices'][0]['message']['content']

            logger.info(f"✅ AI ответ получен ({len(content)} символов)")
            logger.debug(f"Response: {content[:500]}...")

            return content

        except requests.exceptions.Timeout:
            logger.error(f"⏱️ AI запрос превысил таймаут ({self.config.timeout}с)")
            return None
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ AI HTTP ошибка: {e.response.status_code} - {e.response.text[:500]}")
            return None
        except Exception as e:
            logger.error(f"❌ AI ошибка: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def close(self):
        """Закрывает сессию"""
        self._session.close()


class AITask(ABC):
    """Абстрактный базовый класс для AI задач"""

    def __init__(self, client: AIClient, custom_instruction: str = ""):
        self.client = client
        self.custom_instruction = custom_instruction

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Возвращает системный промпт для задачи"""
        pass

    @abstractmethod
    def build_user_prompt(self, **kwargs) -> str:
        """Строит пользовательский промпт"""
        pass

    @abstractmethod
    def parse_response(self, response: str) -> Any:
        """Парсит и валидирует ответ AI"""
        pass

    def execute(self, **kwargs) -> Tuple[bool, Any, Optional[str]]:
        """
        Выполняет AI задачу

        Returns:
            Tuple[success, result, error_message]
        """
        try:
            system_prompt = self.custom_instruction if self.custom_instruction else self.get_system_prompt()

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": self.build_user_prompt(**kwargs)}
            ]

            response = self.client.chat_completion(messages)

            if not response:
                return False, None, "Не удалось получить ответ от AI"

            result = self.parse_response(response)
            if result is None:
                return False, None, f"Не удалось распарсить ответ AI: {response[:200]}"

            return True, result, None

        except Exception as e:
            logger.error(f"❌ Ошибка выполнения AI задачи: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False, None, str(e)


class CategoryDetectionTask(AITask):
    """
    Задача определения категории товара с помощью AI
    """

    def __init__(self, client: AIClient, categories: Dict[int, str], custom_instruction: str = ""):
        """
        Args:
            client: AI клиент
            categories: Словарь {subject_id: category_name} всех доступных категорий WB
            custom_instruction: Кастомная инструкция (если пусто - используется дефолтная)
        """
        super().__init__(client, custom_instruction)
        self.categories = categories

    def get_system_prompt(self) -> str:
        # Формируем список категорий
        categories_list = "\n".join([
            f"- ID: {cat_id}, Название: {cat_name}"
            for cat_id, cat_name in sorted(self.categories.items(), key=lambda x: x[1])
        ])

        template = DEFAULT_INSTRUCTIONS["category_detection"]["template"]
        return template.format(categories_list=categories_list)

    def build_user_prompt(self, **kwargs) -> str:
        product_title = kwargs.get('product_title', '')
        source_category = kwargs.get('source_category', '')
        all_categories = kwargs.get('all_categories', [])
        brand = kwargs.get('brand', '')
        description = kwargs.get('description', '')

        prompt = f"""Определи категорию WB для товара:

НАЗВАНИЕ ТОВАРА: {product_title}
КАТЕГОРИЯ ИЗ ИСТОЧНИКА: {source_category}
ВСЕ КАТЕГОРИИ: {' > '.join(all_categories) if all_categories else 'Не указаны'}
БРЕНД: {brand or 'Не указан'}
"""
        if description:
            prompt += f"ОПИСАНИЕ: {description[:500]}\n"

        return prompt

    def parse_response(self, response: str) -> Optional[Dict]:
        """
        Парсит ответ AI и валидирует

        Returns:
            {
                'category_id': int,
                'category_name': str,
                'confidence': float,
                'reasoning': str
            }
            или None если ответ невалиден
        """
        try:
            # Пробуем извлечь JSON из ответа
            json_str = response.strip()

            # Убираем markdown code blocks если есть
            if json_str.startswith("```"):
                json_str = re.sub(r'^```(?:json)?\n?', '', json_str)
                json_str = re.sub(r'\n?```$', '', json_str)

            # Ищем JSON объект в тексте
            json_match = re.search(r'\{[^{}]*"category_id"[^{}]*\}', json_str, re.DOTALL)
            if json_match:
                json_str = json_match.group()

            data = json.loads(json_str)

            category_id = data.get('category_id')
            category_name = data.get('category_name')
            confidence = data.get('confidence', 0.5)
            reasoning = data.get('reasoning', '')

            # Валидация
            if not category_id:
                logger.warning(f"AI вернул пустой category_id")
                return None

            # Преобразуем в int если строка
            if isinstance(category_id, str):
                category_id = int(category_id)

            # Проверяем, что категория существует
            if category_id not in self.categories:
                logger.warning(f"AI вернул несуществующую категорию: {category_id}")
                # Пробуем найти по названию
                for cid, cname in self.categories.items():
                    if cname.lower() == str(category_name).lower():
                        category_id = cid
                        break
                else:
                    return None

            # Нормализуем confidence
            confidence = max(0.0, min(1.0, float(confidence)))

            return {
                'category_id': category_id,
                'category_name': self.categories.get(category_id, category_name),
                'confidence': confidence,
                'reasoning': str(reasoning)
            }

        except json.JSONDecodeError as e:
            logger.error(f"AI вернул невалидный JSON: {e}")
            logger.error(f"Response: {response[:500]}")
            return None
        except Exception as e:
            logger.error(f"Ошибка парсинга ответа AI: {e}")
            return None


class SizeParsingTask(AITask):
    """
    Задача парсинга размеров товара с помощью AI
    """

    def __init__(self, client: AIClient, category_characteristics: Optional[List[str]] = None,
                 custom_instruction: str = ""):
        """
        Args:
            client: AI клиент
            category_characteristics: Список возможных характеристик для категории
            custom_instruction: Кастомная инструкция
        """
        super().__init__(client, custom_instruction)
        self.category_characteristics = category_characteristics or []

    def get_system_prompt(self) -> str:
        characteristics = self.category_characteristics or [
            "Длина (см)", "Диаметр (см)", "Ширина (см)", "Глубина (см)",
            "Вес (г)", "Объем (мл)", "Размер (S/M/L/XL)", "Размер (числовой)"
        ]

        chars_list = "\n".join([f"- {c}" for c in characteristics])
        template = DEFAULT_INSTRUCTIONS["size_parsing"]["template"]
        return template.format(characteristics_list=chars_list)

    def build_user_prompt(self, **kwargs) -> str:
        sizes_text = kwargs.get('sizes_text', '')
        product_title = kwargs.get('product_title', '')
        description = kwargs.get('description', '')

        prompt = f"""Извлеки размеры и характеристики из данных товара:

НАЗВАНИЕ: {product_title}
СТРОКА РАЗМЕРОВ: {sizes_text or 'Не указана'}
"""
        if description:
            prompt += f"ОПИСАНИЕ: {description[:300]}\n"

        return prompt

    def parse_response(self, response: str) -> Optional[Dict]:
        """
        Парсит ответ AI

        Returns:
            {
                'characteristics': {'name': 'value', ...},
                'raw_sizes': ['...'],
                'has_clothing_sizes': bool,
                'confidence': float
            }
        """
        try:
            json_str = response.strip()

            # Убираем markdown code blocks
            if json_str.startswith("```"):
                json_str = re.sub(r'^```(?:json)?\n?', '', json_str)
                json_str = re.sub(r'\n?```$', '', json_str)

            # Ищем JSON объект
            json_match = re.search(r'\{[^{}]*"characteristics"[^{}]*\}', json_str, re.DOTALL)
            if json_match:
                json_str = json_match.group()

            data = json.loads(json_str)

            characteristics = data.get('characteristics', {})
            raw_sizes = data.get('raw_sizes', [])
            has_clothing = data.get('has_clothing_sizes', False)
            confidence = max(0.0, min(1.0, float(data.get('confidence', 0.5))))

            return {
                'characteristics': characteristics,
                'raw_sizes': raw_sizes if isinstance(raw_sizes, list) else [raw_sizes],
                'has_clothing_sizes': bool(has_clothing),
                'confidence': confidence
            }

        except json.JSONDecodeError:
            logger.error(f"AI вернул невалидный JSON для размеров: {response[:500]}")
            return None
        except Exception as e:
            logger.error(f"Ошибка парсинга ответа AI (размеры): {e}")
            return None


class AIService:
    """
    Главный сервис для работы с AI
    Объединяет все AI задачи
    """

    def __init__(self, config: AIConfig):
        self.config = config
        self.client = AIClient(config)
        self._categories: Dict[int, str] = {}

    def set_categories(self, categories: Dict[int, str]):
        """Устанавливает список категорий для определения"""
        self._categories = categories

    def detect_category(
        self,
        product_title: str,
        source_category: str,
        all_categories: Optional[List[str]] = None,
        brand: str = '',
        description: str = ''
    ) -> Tuple[Optional[int], Optional[str], float, str]:
        """
        Определяет категорию товара с помощью AI

        Returns:
            Tuple[category_id, category_name, confidence, reasoning]
        """
        if not self._categories:
            logger.warning("Категории не установлены для AI сервиса")
            return None, None, 0.0, "Категории не настроены"

        task = CategoryDetectionTask(
            self.client,
            self._categories,
            custom_instruction=self.config.custom_category_instruction
        )
        success, result, error = task.execute(
            product_title=product_title,
            source_category=source_category,
            all_categories=all_categories or [],
            brand=brand,
            description=description
        )

        if success and result:
            return (
                result['category_id'],
                result['category_name'],
                result['confidence'],
                result['reasoning']
            )

        return None, None, 0.0, error or "Ошибка AI"

    def parse_sizes(
        self,
        sizes_text: str,
        product_title: str = '',
        description: str = '',
        category_characteristics: Optional[List[str]] = None
    ) -> Tuple[bool, Dict, str]:
        """
        Парсит размеры товара с помощью AI

        Returns:
            Tuple[success, parsed_data, error_message]
        """
        task = SizeParsingTask(
            self.client,
            category_characteristics,
            custom_instruction=self.config.custom_size_instruction
        )
        success, result, error = task.execute(
            sizes_text=sizes_text,
            product_title=product_title,
            description=description
        )

        if success and result:
            return True, result, ""

        return False, {}, error or "Ошибка AI"

    def test_connection(self) -> Tuple[bool, str]:
        """
        Тестирует подключение к AI API

        Returns:
            Tuple[success, message]
        """
        try:
            messages = [
                {"role": "user", "content": "Ответь одним словом: работает"}
            ]
            response = self.client.chat_completion(messages, max_tokens=50)
            if response:
                return True, f"Подключение успешно. Модель: {self.config.model}"
            return False, "Пустой ответ от API"
        except Exception as e:
            return False, str(e)

    def close(self):
        """Закрывает клиент"""
        self.client.close()


# Синглтон для глобального доступа
_ai_service_instance: Optional[AIService] = None


def get_ai_service(settings=None) -> Optional[AIService]:
    """
    Получает или создает экземпляр AI сервиса

    Args:
        settings: Настройки автоимпорта (AutoImportSettings)

    Returns:
        AIService или None если AI не настроен
    """
    global _ai_service_instance

    if settings is None:
        return _ai_service_instance

    config = AIConfig.from_settings(settings)
    if config is None:
        _ai_service_instance = None
        return None

    if _ai_service_instance is None:
        _ai_service_instance = AIService(config)
        # Загружаем категории WB
        try:
            from wb_categories_mapping import WB_ADULT_CATEGORIES
            _ai_service_instance.set_categories(WB_ADULT_CATEGORIES)
        except ImportError:
            logger.warning("Не удалось загрузить WB_ADULT_CATEGORIES")

    return _ai_service_instance


def reset_ai_service():
    """Сбрасывает AI сервис (при изменении настроек)"""
    global _ai_service_instance
    if _ai_service_instance:
        _ai_service_instance.close()
    _ai_service_instance = None
    # Также сбрасываем кэшированные token managers
    reset_cloudru_token_managers()


def get_available_models(provider: str) -> Dict[str, Dict]:
    """
    Возвращает доступные модели для провайдера

    Args:
        provider: Провайдер (cloudru, openai, custom)

    Returns:
        Словарь моделей {model_id: {name, description, recommended}}
    """
    if provider == 'cloudru':
        return CLOUDRU_MODELS
    elif provider == 'openai':
        return OPENAI_MODELS
    else:
        # Для custom возвращаем объединенный список
        return {**CLOUDRU_MODELS, **OPENAI_MODELS}


def get_default_instructions() -> Dict[str, Dict]:
    """Возвращает дефолтные инструкции для редактирования"""
    return DEFAULT_INSTRUCTIONS
