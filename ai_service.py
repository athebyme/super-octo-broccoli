# -*- coding: utf-8 -*-
"""
AI Service - Универсальный модуль для интеграции с AI провайдерами

Поддерживает:
- OpenAI-совместимые API (GPT, Cloud.ru Foundation Models, etc.)
- Валидацию ответов AI
- Абстракцию для разных задач (категории, размеры, и т.д.)
"""
import json
import re
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass
from enum import Enum
import requests

logger = logging.getLogger(__name__)


class AIProvider(Enum):
    """Поддерживаемые AI провайдеры"""
    OPENAI = "openai"
    CLOUDRU = "cloudru"  # Cloud.ru Foundation Models
    CUSTOM = "custom"  # Любой OpenAI-совместимый API


@dataclass
class AIConfig:
    """Конфигурация AI провайдера"""
    provider: AIProvider
    api_key: str
    api_base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    temperature: float = 0.3
    max_tokens: int = 2000
    timeout: int = 60

    @classmethod
    def from_settings(cls, settings) -> Optional['AIConfig']:
        """Создает конфигурацию из настроек автоимпорта"""
        if not hasattr(settings, 'ai_enabled') or not settings.ai_enabled:
            return None

        if not settings.ai_api_key:
            logger.warning("AI включен, но API ключ не указан")
            return None

        provider = AIProvider(settings.ai_provider or 'openai')

        # Определяем базовый URL в зависимости от провайдера
        if provider == AIProvider.CLOUDRU:
            api_base = settings.ai_api_base_url or "https://api.cloudru.ru/v1"
        elif provider == AIProvider.CUSTOM:
            api_base = settings.ai_api_base_url or "https://api.openai.com/v1"
        else:  # OpenAI
            api_base = "https://api.openai.com/v1"

        return cls(
            provider=provider,
            api_key=settings.ai_api_key,
            api_base_url=api_base,
            model=settings.ai_model or "gpt-4o-mini",
            temperature=settings.ai_temperature or 0.3,
            max_tokens=settings.ai_max_tokens or 2000,
            timeout=settings.ai_timeout or 60
        )


class AIClient:
    """
    Клиент для работы с AI API
    Поддерживает OpenAI-совместимые API
    """

    def __init__(self, config: AIConfig):
        self.config = config
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {config.api_key}',
            'Content-Type': 'application/json'
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
            "max_tokens": max_tokens or self.config.max_tokens
        }

        if response_format:
            payload["response_format"] = response_format

        try:
            logger.info(f"🤖 AI запрос к {self.config.provider.value}: модель={self.config.model}")
            logger.debug(f"Messages: {messages}")

            response = self._session.post(
                url,
                json=payload,
                timeout=self.config.timeout
            )
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
            return None

    def close(self):
        """Закрывает сессию"""
        self._session.close()


class AITask(ABC):
    """Абстрактный базовый класс для AI задач"""

    def __init__(self, client: AIClient):
        self.client = client

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
            messages = [
                {"role": "system", "content": self.get_system_prompt()},
                {"role": "user", "content": self.build_user_prompt(**kwargs)}
            ]

            response = self.client.chat_completion(
                messages,
                response_format={"type": "json_object"}
            )

            if not response:
                return False, None, "Не удалось получить ответ от AI"

            result = self.parse_response(response)
            if result is None:
                return False, None, "Не удалось распарсить ответ AI"

            return True, result, None

        except Exception as e:
            logger.error(f"❌ Ошибка выполнения AI задачи: {e}")
            return False, None, str(e)


class CategoryDetectionTask(AITask):
    """
    Задача определения категории товара с помощью AI
    """

    def __init__(self, client: AIClient, categories: Dict[int, str]):
        """
        Args:
            client: AI клиент
            categories: Словарь {subject_id: category_name} всех доступных категорий WB
        """
        super().__init__(client)
        self.categories = categories

    def get_system_prompt(self) -> str:
        # Формируем список категорий
        categories_list = "\n".join([
            f"- ID: {cat_id}, Название: {cat_name}"
            for cat_id, cat_name in sorted(self.categories.items(), key=lambda x: x[1])
        ])

        return f"""Ты эксперт по классификации товаров для маркетплейса Wildberries.

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
            # Пробуем распарсить JSON
            data = json.loads(response)

            category_id = data.get('category_id')
            category_name = data.get('category_name')
            confidence = data.get('confidence', 0.5)
            reasoning = data.get('reasoning', '')

            # Валидация
            if not category_id or not isinstance(category_id, int):
                logger.warning(f"AI вернул невалидный category_id: {category_id}")
                return None

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
            # Пробуем извлечь JSON из текста
            try:
                json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
                if json_match:
                    return self.parse_response(json_match.group())
            except:
                pass
            return None
        except Exception as e:
            logger.error(f"Ошибка парсинга ответа AI: {e}")
            return None


class SizeParsingTask(AITask):
    """
    Задача парсинга размеров товара с помощью AI
    """

    def __init__(self, client: AIClient, category_characteristics: Optional[List[str]] = None):
        """
        Args:
            client: AI клиент
            category_characteristics: Список возможных характеристик для категории
        """
        super().__init__(client)
        self.category_characteristics = category_characteristics or []

    def get_system_prompt(self) -> str:
        characteristics = self.category_characteristics or [
            "Длина (см)", "Диаметр (см)", "Ширина (см)", "Глубина (см)",
            "Вес (г)", "Объем (мл)", "Размер (S/M/L/XL)", "Размер (числовой)"
        ]

        chars_list = "\n".join([f"- {c}" for c in characteristics])

        return f"""Ты эксперт по парсингу размеров и характеристик товаров.

Твоя задача - извлечь структурированные данные о размерах из текстового описания.

ВОЗМОЖНЫЕ ХАРАКТЕРИСТИКИ:
{chars_list}

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
            data = json.loads(response)

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
            # Пробуем извлечь JSON
            try:
                json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
                if json_match:
                    return self.parse_response(json_match.group())
            except:
                pass
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

        task = CategoryDetectionTask(self.client, self._categories)
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
        task = SizeParsingTask(self.client, category_characteristics)
        success, result, error = task.execute(
            sizes_text=sizes_text,
            product_title=product_title,
            description=description
        )

        if success and result:
            return True, result, ""

        return False, {}, error or "Ошибка AI"

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
