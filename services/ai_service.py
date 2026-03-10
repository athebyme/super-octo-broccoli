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
from typing import Optional, Dict, List, Any, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import requests
from html import unescape

logger = logging.getLogger(__name__)


# ============================================================================
# HTML STRIPPING UTILITY
# ============================================================================

def strip_html_tags(text: str) -> str:
    """
    Удаляет HTML теги из текста и очищает от лишних пробелов

    Args:
        text: Текст с возможными HTML тегами

    Returns:
        Очищенный текст без HTML тегов
    """
    if not text:
        return text

    # Удаляем HTML теги
    clean = re.sub(r'<[^>]+>', '', text)
    # Декодируем HTML entities (&nbsp;, &amp;, etc.)
    clean = unescape(clean)
    # Заменяем множественные пробелы и переносы строк
    clean = re.sub(r'\s+', ' ', clean)
    # Убираем пробелы в начале и конце
    clean = clean.strip()

    return clean


def clean_ai_response(response: str, strip_html: bool = True) -> str:
    """
    Очищает ответ AI от нежелательных элементов

    Args:
        response: Ответ AI
        strip_html: Удалять ли HTML теги

    Returns:
        Очищенный ответ
    """
    if not response:
        return response

    text = response.strip()

    # Удаляем markdown code blocks если есть
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\n?', '', text)
        text = re.sub(r'\n?```$', '', text)

    # Удаляем HTML если нужно
    if strip_html:
        text = strip_html_tags(text)

    return text


# ============================================================================
# AI REQUEST LOGGER (Центральное логирование всех AI запросов)
# ============================================================================

# Callback для логирования AI запросов (будет установлен из routes)
_ai_request_logger: Optional[Callable] = None


def set_ai_request_logger(logger_callback: Callable):
    """Устанавливает callback для логирования AI запросов"""
    global _ai_request_logger
    _ai_request_logger = logger_callback


def log_ai_request(
    seller_id: int,
    action_type: str,
    provider: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    response: Optional[str],
    success: bool,
    error_message: Optional[str] = None,
    response_time_ms: int = 0,
    tokens_used: int = 0,
    source_module: str = 'ai_service',
    product_id: Optional[int] = None
):
    """
    Логирует AI запрос в историю

    Args:
        seller_id: ID продавца
        action_type: Тип действия (seo_title, keywords, etc.)
        provider: Провайдер AI
        model: Модель
        system_prompt: Системный промпт
        user_prompt: Пользовательский промпт
        response: Ответ AI
        success: Успешен ли запрос
        error_message: Сообщение об ошибке
        response_time_ms: Время ответа в мс
        tokens_used: Использовано токенов
        source_module: Модуль-источник запроса
        product_id: ID товара (если применимо)
    """
    if _ai_request_logger:
        try:
            _ai_request_logger(
                seller_id=seller_id,
                action_type=action_type,
                provider=provider,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response=response,
                success=success,
                error_message=error_message,
                response_time_ms=response_time_ms,
                tokens_used=tokens_used,
                source_module=source_module,
                product_id=product_id
            )
        except Exception as e:
            logger.error(f"Ошибка логирования AI запроса: {e}")


# ============================================================================
# CLOUD.RU TOKEN MANAGER
# ============================================================================

import base64


class CloudRuApiKeyManager:
    """
    Менеджер API-ключей для Cloud.ru Foundation Models API

    Cloud.ru поддерживает два способа аутентификации:
    1. API-ключ (формат {base64}.{secret}) - используется напрямую:
       Authorization: Api-Key {api_key}

    2. Ключ доступа (отдельные Key ID и Key Secret) - требует обмена на токен:
       Authorization: Bearer {access_token}

    Этот менеджер поддерживает оба формата:
    - Если ключ содержит точку - это API-ключ, используется напрямую с Api-Key
    - Если ключ в формате "keyId:secret" - это ключ доступа, обмениваем на токен
    """

    # URL для получения токена Cloud.ru IAM API (для ключей доступа)
    TOKEN_URL = "https://iam.api.cloud.ru/api/v1/auth/token"
    TOKEN_REFRESH_BUFFER = 300  # 5 минут

    def __init__(self, api_key: str):
        """
        Args:
            api_key: API-ключ или ключ доступа в формате "keyId:secret"
        """
        self.original_key = api_key
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._lock = threading.Lock()

        # Определяем тип ключа
        if ':' in api_key and '.' not in api_key:
            # Формат "keyId:secret" - ключ доступа, требует обмена на токен
            self.auth_type = 'access_key'
            parts = api_key.split(':', 1)
            self.key_id = parts[0]
            self.secret = parts[1] if len(parts) > 1 else ''
            logger.info(f"✅ Cloud.ru ключ доступа: keyId={self.key_id[:8]}...")
        else:
            # API-ключ - используется напрямую
            self.auth_type = 'api_key'
            self.api_key = api_key
            logger.info(f"✅ Cloud.ru API-ключ: {api_key[:12]}...")

    @classmethod
    def from_key_secret(cls, key_secret: str) -> 'CloudRuApiKeyManager':
        """Создает менеджер из ключа"""
        return cls(key_secret)

    def get_access_token(self) -> Optional[str]:
        """
        Получает токен/ключ для Authorization заголовка

        Returns:
            Токен или API-ключ
        """
        if self.auth_type == 'api_key':
            # API-ключ используется напрямую
            return self.api_key

        # Ключ доступа - нужен обмен на токен
        with self._lock:
            current_time = time.time()

            if (self._access_token is None or
                current_time >= self._token_expires_at - self.TOKEN_REFRESH_BUFFER):

                logger.info("🔄 Получаем новый access token от Cloud.ru...")
                success = self._fetch_new_token()
                if not success:
                    return None

            return self._access_token

    def get_auth_header(self) -> Optional[str]:
        """
        Возвращает полный Authorization заголовок

        Returns:
            "Bearer {key}" для API-ключа или "Bearer {token}" для ключа доступа
        """
        token = self.get_access_token()
        if not token:
            return None

        # Cloud.ru Foundation Models всегда использует Bearer
        return f'Bearer {token}'

    def _fetch_new_token(self) -> bool:
        """Запрашивает новый access token у Cloud.ru IAM API"""
        try:
            payload = {
                "keyId": self.key_id,
                "secret": self.secret
            }

            headers = {
                "Content-Type": "application/json"
            }

            logger.info(f"🔑 Запрос токена к {self.TOKEN_URL} с keyId={self.key_id[:8]}...")

            response = requests.post(
                self.TOKEN_URL,
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code != 200:
                logger.error(f"❌ Cloud.ru Token ошибка: {response.status_code}")
                logger.error(f"Response: {response.text[:500]}")
                return False

            data = response.json()
            self._access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            self._token_expires_at = time.time() + expires_in

            logger.info(f"✅ Cloud.ru access token получен (expires_in: {expires_in}s)")
            return True

        except Exception as e:
            logger.error(f"❌ Cloud.ru Token error: {e}")
            return False

    def invalidate_token(self):
        """Инвалидирует текущий токен"""
        with self._lock:
            self._access_token = None
            self._token_expires_at = 0


# Алиас для совместимости
CloudRuTokenManager = CloudRuApiKeyManager


# Глобальный кэш
_token_managers: Dict[str, CloudRuApiKeyManager] = {}
_token_managers_lock = threading.Lock()


def get_cloudru_token_manager(key_secret: str) -> CloudRuApiKeyManager:
    """Получает или создает менеджер для данного ключа"""
    with _token_managers_lock:
        cache_key = key_secret[:20] if len(key_secret) > 20 else key_secret
        if cache_key not in _token_managers:
            _token_managers[cache_key] = CloudRuApiKeyManager.from_key_secret(key_secret)
        return _token_managers[cache_key]


def reset_cloudru_token_managers():
    """Сбрасывает все кэшированные менеджеры"""
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
    "seo_title": {
        "name": "SEO-оптимизация заголовка",
        "description": "Генерация оптимизированного заголовка для поиска на WB",
        "template": """Ты SEO-эксперт для маркетплейса Wildberries.

Твоя задача - создать оптимизированный заголовок товара для лучшего ранжирования в поиске.

КРИТИЧНО - ОГРАНИЧЕНИЕ WB API:
⚠️ МАКСИМУМ 60 СИМВОЛОВ! Это жёсткое ограничение Wildberries API.
Если заголовок длиннее 60 символов - карточка НЕ создастся!

ПРАВИЛА:
1. МАКСИМУМ 60 СИМВОЛОВ (включая пробелы) - это КРИТИЧНО!
2. Начинай с типа товара (самое важное слово)
3. Добавляй 1-2 ключевые характеристики (материал, размер, особенность)
4. Не используй CAPS LOCK и спецсимволы
5. Избегай повторений слов
6. Не используй: "лучший", "топ", "хит", "№1", "качественный"
7. Каждое слово должно нести смысл - нет "воды"
8. ЗАПРЕЩЁННЫЕ СИМВОЛЫ: ™ ® © ℠ ℗ — НЕ ИСПОЛЬЗУЙ их НИКОГДА

ПРИМЕРЫ ХОРОШИХ ЗАГОЛОВКОВ (до 60 символов):
- "Вибратор силиконовый с 10 режимами розовый" (43 символа)
- "Массажер для тела беспроводной водонепроницаемый" (49 символов)
- "Набор эротического белья кружевной черный M/L" (47 символов)

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "title": "<заголовок МАКСИМУМ 60 символов>",
    "length": <количество символов>,
    "keywords_used": ["ключевое1", "ключевое2"],
    "improvements": ["что улучшено"]
}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON. Проверь что title <= 60 символов!"""
    },

    "keywords": {
        "name": "Генерация ключевых слов",
        "description": "Создание ключевых слов для поиска товара",
        "template": """Ты SEO-специалист для маркетплейса Wildberries.

Твоя задача - сгенерировать ключевые слова для товара.

ПРАВИЛА:
1. Генерируй 15-30 релевантных ключевых слов
2. Включай синонимы и вариации написания
3. Добавляй общие и специфичные запросы
4. Учитывай транслитерацию популярных терминов
5. Не дублируй слова из названия напрямую
6. Избегай нерелевантных слов

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "keywords": ["слово1", "слово2", ...],
    "search_queries": ["популярный запрос 1", "популярный запрос 2"]
}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON."""
    },

    "bullet_points": {
        "name": "Преимущества товара",
        "description": "Генерация кратких bullet points с преимуществами",
        "template": """Ты копирайтер для маркетплейса Wildberries.

Твоя задача - создать краткие преимущества товара (bullet points).

ПРАВИЛА:
1. 4-6 кратких преимуществ
2. Каждое преимущество - 1 строка (до 50 символов)
3. Начинай с глагола или существительного
4. Фокус на пользе для покупателя
5. Конкретика вместо абстракций
6. Не повторяй информацию из названия
7. БЕЗ эмодзи и спецсимволов (запрещено на WB!)

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "bullet_points": [
        "Преимущество 1",
        "Преимущество 2"
    ],
    "target_audience": "<целевая аудитория>"
}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON. Без эмодзи!"""
    },

    "description_enhance": {
        "name": "Улучшение описания",
        "description": "SEO-оптимизация и улучшение описания товара",
        "template": """Ты профессиональный копирайтер для маркетплейсов.

Твоя задача - улучшить описание товара для повышения конверсии.

ПРАВИЛА:
1. Сохрани все факты и характеристики из оригинала
2. Добавь структуру (абзацы, списки)
3. Первый абзац - краткое УТП (1-2 предложения)
4. Второй абзац - подробное описание
5. Используй ключевые слова естественно
6. Максимум 2000 символов (ограничение Wildberries API)
7. Без воды и пустых фраз
8. ЗАПРЕЩЁННЫЕ СИМВОЛЫ: ™ ® © ℠ ℗ — НЕ ИСПОЛЬЗУЙ их НИКОГДА

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "description": "<улучшенное описание>",
    "structure": {
        "intro": "<вступление>",
        "features": ["особенность1", "особенность2"],
        "call_to_action": "<призыв>"
    }
}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON."""
    },

    "rich_content": {
        "name": "Rich контент",
        "description": "Генерация продающего rich контента для карточки WB",
        "template": """Ты профессиональный дизайнер инфографики для Wildberries, специализирующийся на создании Rich-контента.

Rich-контент на WB - это визуальные блоки (слайды) с изображениями и текстовыми наложениями.
Твоя задача - создать детальные макеты для 6-10 слайдов инфографики.

ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ WB:
- Размер изображения: 1440x810 px (соотношение 16:9)
- Формат: JPEG или PNG, до 5 МБ
- Максимум 10 блоков

СТРОГИЕ ЗАПРЕТЫ WB:
- Эмодзи и смайлики
- Цены, скидки, акции
- Водяные знаки, логотипы сторонних брендов
- QR-коды, ссылки, контакты
- Надписи: "хит продаж", "лучшая цена", "топ", "бестселлер"
- Призывы: "позвоните", "закажите сейчас"

СТРУКТУРА СЛАЙДОВ:
1. HERO (главный) - крупное фото товара + УТП
2. ПРОБЛЕМА - боль клиента, которую решает товар
3-4. ПРЕИМУЩЕСТВА - по одному ключевому на слайд
5-6. ХАРАКТЕРИСТИКИ - материалы, размеры, технические детали
7-8. ПРИМЕНЕНИЕ - сценарии использования, для кого подходит
9. КОМПЛЕКТАЦИЯ - что входит в набор
10. ДОВЕРИЕ - гарантия, сертификаты, безопасность

ПРАВИЛА ТЕКСТА ДЛЯ СЛАЙДОВ:
- Заголовок: 2-5 слов, КРУПНО, читается за 1 секунду
- Подзаголовок: 1 короткое предложение или 3-5 слов
- Буллеты (если есть): максимум 3-4 пункта по 2-4 слова

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "slides": [
        {
            "number": 1,
            "type": "hero",
            "title": "КОРОТКИЙ ЗАГОЛОВОК",
            "subtitle": "Пояснение в 5-8 слов",
            "bullets": null,
            "image_concept": {
                "main_object": "Что главное на фото (товар, руки с товаром, товар в использовании)",
                "background": "Тип фона (однотонный светлый/темный, градиент, lifestyle фото)",
                "composition": "left/center/right - где товар на изображении",
                "mood": "Настроение (премиум, уютный, энергичный, минималистичный)"
            },
            "text_layout": {
                "title_position": "top-left/top-center/top-right/center/bottom-left/bottom-center/bottom-right",
                "title_color": "Цвет заголовка (белый на темном фоне / темный на светлом)",
                "subtitle_position": "Позиция подзаголовка относительно заголовка",
                "text_background": "none/semi-transparent-dark/semi-transparent-light/solid-box"
            }
        },
        {
            "number": 2,
            "type": "problem",
            "title": "ЗНАКОМАЯ ПРОБЛЕМА?",
            "subtitle": "Описание боли клиента",
            "bullets": ["Проблема 1", "Проблема 2", "Проблема 3"],
            "image_concept": {
                "main_object": "Визуализация проблемы или контраст до/после",
                "background": "Приглушенные тона для контраста с решением",
                "composition": "center",
                "mood": "Сочувствующий, понимающий"
            },
            "text_layout": {
                "title_position": "top-center",
                "title_color": "dark",
                "subtitle_position": "below-title",
                "text_background": "semi-transparent-light"
            }
        }
    ],
    "design_recommendations": {
        "color_palette": ["#основной", "#акцент", "#фон", "#текст"],
        "font_style": "modern/classic/bold/elegant",
        "overall_mood": "Общее настроение карточки",
        "key_visual_elements": ["элемент 1", "элемент 2"]
    },
    "total_slides": 6,
    "main_usp": "Главное УТП товара в 5-7 словах",
    "target_audience": "Для кого этот товар"
}

ВАЖНО:
- Создай 6-10 слайдов с ПОЛНОЙ детализацией для дизайнера
- Текст должен быть ОЧЕНЬ коротким - это для картинок!
- БЕЗ ЭМОДЗИ! Отвечай ТОЛЬКО валидным JSON."""
    },

    "card_analysis": {
        "name": "Анализ карточки",
        "description": "Рекомендации по улучшению карточки товара",
        "template": """Ты эксперт по оптимизации карточек товаров на Wildberries.

Твоя задача - проанализировать карточку и дать рекомендации.

ОЦЕНИ:
1. Заголовок (SEO, читаемость)
2. Описание (полнота, структура)
3. Характеристики (заполненность)
4. Фото (по описанию)
5. Ценообразование (если указано)

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "score": <число 1-100>,
    "issues": [
        {"priority": "high/medium/low", "issue": "проблема", "fix": "решение"}
    ],
    "recommendations": [
        "рекомендация 1",
        "рекомендация 2"
    ],
    "strengths": ["сильная сторона 1"]
}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON."""
    },

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
        "template": """Ты эксперт по парсингу размеров и характеристик товаров для маркетплейса.

Твоя задача - извлечь ВСЕ размеры и характеристики из текста и заполнить ВСЕ подходящие поля.

ДОСТУПНЫЕ ХАРАКТЕРИСТИКИ ДЛЯ ЗАПОЛНЕНИЯ:
{characteristics_list}

ВАЖНЫЕ ПРАВИЛА:
1. Если в тексте есть ОДНО значение длины (например "длина 7,5 см") - заполни им ВСЕ характеристики длины товара (не упаковки)
2. Аналогично для диаметра, ширины и других параметров - одно значение применяй ко всем подходящим характеристикам
3. Характеристики упаковки (с словом "упаковка/упак") заполняй ТОЛЬКО если явно указаны размеры упаковки
4. Преобразуй единицы измерения в стандартные (см, г, мл)
5. Если указан диапазон (например, "15-18 см") - используй среднее или максимальное значение
6. Числа с запятой (7,5) преобразуй в формат с точкой (7.5)

ПРИМЕРЫ:
- "длина вибропули 7,5 см" → заполни: "Длина изделия": "7.5", "Длина": "7.5", "Рабочая длина": "7.5" (если они есть в списке)
- "диаметр 2 см" → заполни: "Диаметр": "2", "Диаметр изделия": "2" (если они есть в списке)
- "размер трусиков 48-50" → заполни: "Размер": "48-50"

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{{
    "characteristics": {{
        "Название характеристики из списка": "значение",
        ...
    }},
    "raw_sizes": ["исходный текст размеров"],
    "has_clothing_sizes": true/false,
    "confidence": <число от 0.0 до 1.0>
}}

ВАЖНО:
- Заполняй МАКСИМУМ характеристик из списка которые подходят
- Отвечай ТОЛЬКО валидным JSON без дополнительного текста"""
    },

    "dimensions_extraction": {
        "name": "Извлечение габаритов",
        "description": "Извлечение физических размеров товара (длина, ширина, высота, вес)",
        "template": """Ты эксперт по извлечению физических размеров товаров для маркетплейса Wildberries.

Твоя задача - найти и извлечь ВСЕ габариты товара из названия, описания и характеристик.

КАКИЕ РАЗМЕРЫ НУЖНО НАЙТИ:
1. Длина (см) - общая длина изделия
2. Ширина (см) - ширина изделия
3. Высота (см) - высота изделия
4. Глубина (см) - если применимо
5. Диаметр (см) - для круглых изделий
6. Вес (г) - вес товара
7. Объем (мл/л) - для жидкостей и емкостей
8. Рабочая длина (см) - для вибраторов и подобных изделий
9. Размеры упаковки (длина x ширина x высота)

ПРАВИЛА:
1. Преобразуй все единицы в стандартные (см, г, мл)
2. Числа с запятой преобразуй в точку (7,5 → 7.5)
3. Если диапазон (15-20 см) - укажи как "15-20" или среднее значение
4. Различай размеры товара и размеры упаковки!
5. Для одежды: обхват груди, талии, бедер в см

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "dimensions": {
        "length_cm": <число или null>,
        "width_cm": <число или null>,
        "height_cm": <число или null>,
        "depth_cm": <число или null>,
        "diameter_cm": <число или null>,
        "weight_g": <число или null>,
        "volume_ml": <число или null>,
        "working_length_cm": <число или null>
    },
    "package_dimensions": {
        "length_cm": <число или null>,
        "width_cm": <число или null>,
        "height_cm": <число или null>,
        "weight_g": <число или null>
    },
    "raw_text": "<исходный текст с размерами>",
    "confidence": <число от 0.0 до 1.0>
}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON."""
    },

    "clothing_sizes": {
        "name": "Парсинг размеров товара",
        "description": "Определение и стандартизация размеров одежды или физических габаритов",
        "template": """Ты эксперт по размерам товаров для маркетплейса Wildberries.

Твоя задача - определить тип размеров и распарсить их:

ТИП 1: РАЗМЕРЫ ОДЕЖДЫ (S/M/L/42/44/46)
- Буквенные: XXS, XS, S, M, L, XL, XXL, XXXL
- Российские: 40, 42, 44, 46, 48, 50, 52, 54, 56
- Бельё: 70A, 75B, 80C
- Обувь: 35-45

ТИП 2: ФИЗИЧЕСКИЕ ГАБАРИТЫ (см, мм, г)
- Длина, ширина, высота, глубина
- Диаметр, толщина
- Вес, объём
- Рабочая длина

ПРАВИЛА:
1. СНАЧАЛА определи - это размеры одежды или физические габариты
2. Если в тексте есть "см", "мм", "длина", "диаметр", "вес" - это ФИЗИЧЕСКИЕ ГАБАРИТЫ
3. Если в тексте S/M/L или 42/44/46 без единиц измерения - это ОДЕЖДА
4. Все числа возвращай на РУССКОМ языке

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "product_type": "clothing/physical/unknown",
    "size_type": "letter/numeric/combined/one_size/dimensions",
    "sizes": [
        {
            "original": "<исходное значение>",
            "standardized": "<стандартизированное значение>",
            "ru_size": "<российский размер если одежда>",
            "international": "<международный если одежда>"
        }
    ],
    "dimensions": {
        "length_cm": "<длина в см>",
        "width_cm": "<ширина в см>",
        "height_cm": "<высота в см>",
        "diameter_cm": "<диаметр в см>",
        "working_length_cm": "<рабочая длина в см>",
        "weight_g": "<вес в граммах>",
        "volume_ml": "<объём в мл>"
    },
    "size_range": {
        "min": "<минимум>",
        "max": "<максимум>"
    },
    "body_measurements": {
        "chest_cm": "<обхват груди>",
        "waist_cm": "<обхват талии>",
        "hips_cm": "<обхват бедер>"
    },
    "fit_type": "tight/regular/loose/oversized",
    "raw_text": "<исходный текст с размерами>",
    "confidence": <число от 0.0 до 1.0>
}

ВАЖНО:
- Если это ФИЗИЧЕСКИЕ ГАБАРИТЫ (product_type="physical"):
  - Заполни поле "dimensions"
  - Массив "sizes" должен быть ПУСТЫМ []
  - size_type должен быть "dimensions"
- Если это ОДЕЖДА (product_type="clothing"):
  - Заполни массив "sizes"
  - Поле "dimensions" может быть пустым {}
- НЕ смешивай физические габариты и размеры одежды!
- Отвечай ТОЛЬКО валидным JSON
- Все текстовые значения на РУССКОМ"""
    },

    "brand_detection": {
        "name": "Определение бренда",
        "description": "Автоматическое определение бренда товара",
        "template": """Ты эксперт по брендам товаров для маркетплейса Wildberries.

Твоя задача - определить бренд товара из его названия, описания и характеристик.

ПРАВИЛА ОПРЕДЕЛЕНИЯ БРЕНДА:
1. Бренд обычно указан в начале названия или в характеристиках
2. Отличай бренд от типа товара (Nike - бренд, Кроссовки - тип)
3. Если бренд неизвестен - попробуй определить по:
   - Упоминанию в описании
   - Характерным особенностям продукции
   - Стилю названия
4. Для китайских товаров без явного бренда - указывай "No brand" или предполагаемый
5. Некоторые бренды имеют несколько написаний - выбирай официальное

ПОПУЛЯРНЫЕ БРЕНДЫ ПО КАТЕГОРИЯМ:
- Одежда: Zara, H&M, Nike, Adidas, Puma, Reebok, Calvin Klein
- Косметика: L'Oreal, Maybelline, MAC, NYX, Garnier
- Электроника: Samsung, Apple, Xiaomi, Huawei, JBL
- Интим-товары: Lola Games, Bior toys, Sexus, Lovetoy, Pipedream

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "brand": "<определенный бренд>",
    "brand_normalized": "<нормализованное название для WB>",
    "brand_type": "known/generic/no_brand/unknown",
    "brand_origin": "название/описание/характеристики/определено_по_контексту",
    "alternative_names": ["альтернативное написание 1", "альтернативное написание 2"],
    "is_official": true/false,
    "confidence": <число от 0.0 до 1.0>,
    "reasoning": "<почему выбран этот бренд>"
}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON."""
    },

    "material_detection": {
        "name": "Определение материалов",
        "description": "Извлечение информации о материалах и составе товара",
        "template": """Ты эксперт по материалам и составу товаров для маркетплейса Wildberries.

Твоя задача - определить материалы и состав товара из описания и характеристик.

ТИПЫ МАТЕРИАЛОВ:

1. ТКАНИ:
   - Натуральные: хлопок, лен, шелк, шерсть, кашемир, вискоза
   - Синтетические: полиэстер, нейлон, акрил, эластан, спандекс
   - Смешанные: хлопок 95% + эластан 5%

2. МАТЕРИАЛЫ ИНТИМ-ТОВАРОВ:
   - Силикон (медицинский, пищевой)
   - TPE/TPR (термопластичный эластомер)
   - ABS-пластик
   - Кибер-кожа (CyberSkin)
   - ПВХ
   - Латекс
   - Металл (нержавеющая сталь)

3. ДРУГИЕ:
   - Кожа (натуральная, искусственная, экокожа)
   - Пластик, металл, стекло, дерево

ПРАВИЛА:
1. Извлеки ВСЕ упомянутые материалы
2. Если указан состав в процентах - сохрани проценты
3. Определи основной материал (доминирующий)
4. Для безопасности укажи гипоаллергенность

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "materials": [
        {
            "name": "<название материала>",
            "name_ru": "<название на русском>",
            "percentage": <процент или null>,
            "is_primary": true/false
        }
    ],
    "composition_text": "<полный состав как строка>",
    "material_type": "natural/synthetic/mixed/unknown",
    "care_instructions": ["рекомендация 1", "рекомендация 2"],
    "properties": {
        "hypoallergenic": true/false/null,
        "waterproof": true/false/null,
        "body_safe": true/false/null,
        "phthalate_free": true/false/null
    },
    "confidence": <число от 0.0 до 1.0>
}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON."""
    },

    "color_detection": {
        "name": "Определение цвета",
        "description": "Извлечение информации о цвете товара",
        "template": """Ты эксперт по цветам товаров для маркетплейса Wildberries.

Твоя задача - определить цвет товара из названия, описания и характеристик.

СТАНДАРТНЫЕ ЦВЕТА WB:
- Базовые: черный, белый, серый, бежевый, коричневый
- Яркие: красный, синий, зеленый, желтый, оранжевый, розовый, фиолетовый
- Оттенки: голубой, бирюзовый, бордовый, мятный, коралловый, персиковый
- Металлик: золотой, серебряный, бронзовый
- Прозрачный, мультиколор

ПРАВИЛА:
1. Определи основной цвет товара
2. Если товар многоцветный - укажи все цвета
3. Нормализуй название цвета для WB (молочный → белый/бежевый)
4. Различай цвет товара и цвет упаковки

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "primary_color": "<основной цвет>",
    "primary_color_wb": "<цвет для WB>",
    "secondary_colors": ["дополнительный цвет 1", "дополнительный цвет 2"],
    "color_description": "<описание цвета из источника>",
    "is_multicolor": true/false,
    "is_transparent": true/false,
    "hex_code": "<#RRGGBB или null>",
    "color_family": "neutral/warm/cool/bright/dark",
    "confidence": <число от 0.0 до 1.0>
}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON."""
    },

    "product_attributes": {
        "name": "Комплексный анализ атрибутов",
        "description": "Полное извлечение всех атрибутов товара за один запрос",
        "template": """Ты эксперт по анализу карточек товаров для маркетплейса Wildberries.

Твоя задача - извлечь ВСЕ возможные атрибуты товара за один проход.

АТРИБУТЫ ДЛЯ ИЗВЛЕЧЕНИЯ:

1. БРЕНД - название производителя/бренда

2. ЦВЕТ - основной и дополнительные цвета

3. МАТЕРИАЛ - состав и материалы изготовления

4. РАЗМЕРЫ:
   - Физические габариты (длина, ширина, высота, вес)
   - Размеры одежды (S, M, L или 42, 44, 46)
   - Специфичные размеры (диаметр, объем)

5. ПОЛ - мужской/женский/унисекс

6. ВОЗРАСТ - взрослый/детский/подростковый + конкретный возраст если указан

7. СЕЗОН - всесезонный/лето/зима/демисезон

8. СТРАНА - страна производства

9. КОМПЛЕКТАЦИЯ - что входит в набор

10. ОСОБЕННОСТИ - уникальные характеристики товара

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{
    "brand": {
        "name": "<бренд>",
        "normalized": "<нормализованное название>",
        "confidence": <0.0-1.0>
    },
    "colors": {
        "primary": "<основной цвет>",
        "secondary": ["доп. цвет"],
        "wb_color": "<цвет для WB>"
    },
    "materials": {
        "primary": "<основной материал>",
        "composition": "<полный состав>",
        "properties": ["свойство1", "свойство2"]
    },
    "dimensions": {
        "length_cm": <число или null>,
        "width_cm": <число или null>,
        "height_cm": <число или null>,
        "diameter_cm": <число или null>,
        "weight_g": <число или null>,
        "volume_ml": <число или null>
    },
    "clothing_size": {
        "type": "letter/numeric/one_size/null",
        "sizes": ["S", "M", "L"],
        "ru_sizes": ["42", "44", "46"]
    },
    "gender": "male/female/unisex/null",
    "age_group": "adult/teen/child/baby/null",
    "age_range": "<возрастной диапазон или null>",
    "season": "all_season/summer/winter/demi/null",
    "country": "<страна производства или null>",
    "package_contents": ["элемент 1", "элемент 2"],
    "special_features": ["особенность 1", "особенность 2"],
    "overall_confidence": <число от 0.0 до 1.0>
}

ВАЖНО: Заполняй ВСЕ поля что можешь определить. Отвечай ТОЛЬКО валидным JSON."""
    },

    "full_product_parsing": {
        "name": "Полный AI парсинг товара",
        "description": "Комплексное извлечение ВСЕХ характеристик товара для маркетплейса",
        "template": """Ты ведущий эксперт по товарным карточкам для маркетплейсов (Wildberries, Ozon, Яндекс.Маркет).
Твоя задача — извлечь АБСОЛЮТНО ВСЕ возможные характеристики и атрибуты товара из предоставленных данных.

══════════════════════════════════════════════════════════════
КАТЕГОРИИ ХАРАКТЕРИСТИК ДЛЯ ИЗВЛЕЧЕНИЯ:
══════════════════════════════════════════════════════════════

1. ИДЕНТИФИКАЦИЯ ТОВАРА:
   - Тип/вид товара (product_type)
   - Подтип/подкатегория (product_subtype)
   - Предполагаемая категория WB (wb_category)
   - Предмет WB (wb_subject)
   - Назначение (purpose)
   - Область применения (application_area)

2. БРЕНД И ПРОИЗВОДИТЕЛЬ:
   - Бренд (brand)
   - Бренд нормализованный для WB (brand_normalized)
   - Производитель (manufacturer)
   - Серия/линейка (product_line)
   - Артикул производителя (manufacturer_sku)

3. ФИЗИЧЕСКИЕ ХАРАКТЕРИСТИКИ:
   - Длина, см (length_cm)
   - Ширина, см (width_cm)
   - Высота, см (height_cm)
   - Глубина, см (depth_cm)
   - Диаметр, см (diameter_cm)
   - Толщина, см (thickness_cm)
   - Вес товара, г (weight_g)
   - Объём, мл (volume_ml)
   - Рабочая длина, см (working_length_cm)
   - Максимальный диаметр, см (max_diameter_cm)
   - Минимальный диаметр, см (min_diameter_cm)
   - Обхват, см (circumference_cm)

4. УПАКОВКА:
   - Длина упаковки, см (package_length_cm)
   - Ширина упаковки, см (package_width_cm)
   - Высота упаковки, см (package_height_cm)
   - Вес с упаковкой, г (package_weight_g)
   - Тип упаковки (package_type)

5. МАТЕРИАЛЫ И СОСТАВ:
   - Основной материал (primary_material)
   - Полный состав с % (composition)
   - Список материалов (materials_list)
   - Свойства материала (material_properties) — гипоаллергенный, водонепроницаемый и т.д.
   - Текстура поверхности (surface_texture)

6. ЦВЕТ:
   - Основной цвет (primary_color)
   - Цвет для WB (wb_color)
   - Дополнительные цвета (secondary_colors)
   - Цветовое семейство (color_family)
   - Многоцветный? (is_multicolor)
   - Прозрачный? (is_transparent)
   - Принт/узор (pattern)

7. РАЗМЕРЫ ОДЕЖДЫ (если применимо):
   - Тип размерной сетки (size_type): letter/numeric/one_size
   - Доступные размеры (available_sizes)
   - Российские размеры (ru_sizes)
   - Международные размеры (international_sizes)
   - Обхват груди, см (chest_cm)
   - Обхват талии, см (waist_cm)
   - Обхват бёдер, см (hips_cm)
   - Тип посадки (fit_type): slim/regular/loose/oversized

8. ЦЕЛЕВАЯ АУДИТОРИЯ:
   - Пол (gender): male/female/unisex
   - Возрастная группа (age_group): adult/teen/child/baby
   - Возрастной диапазон (age_range)
   - Целевая аудитория текстом (target_audience)

9. СЕЗОННОСТЬ И УСЛОВИЯ:
   - Сезон (season): all_season/summer/winter/demi
   - Условия эксплуатации (usage_conditions)
   - Температурный режим (temperature_range)
   - Водонепроницаемость (waterproof): yes/no/splash_proof

10. КОМПЛЕКТАЦИЯ:
    - Список элементов в комплекте (package_contents)
    - Количество предметов (items_count)
    - Включены ли батарейки (batteries_included)
    - Тип батареек/питания (power_type)
    - Зарядное устройство (charger_included)

11. ФУНКЦИОНАЛЬНОСТЬ:
    - Особенности/фичи (special_features)
    - Количество режимов (modes_count)
    - Тип управления (control_type)
    - Бесшумный (is_silent)
    - Совместимость (compatibility)

12. УХОД И ХРАНЕНИЕ:
    - Инструкции по уходу (care_instructions)
    - Условия хранения (storage_conditions)
    - Можно стирать? (washable)

13. ПРОИСХОЖДЕНИЕ:
    - Страна производства (country_of_origin)
    - Импортёр (importer)

14. БЕЗОПАСНОСТЬ И СЕРТИФИКАЦИЯ:
    - Сертификаты (certifications)
    - Предупреждения (warnings)
    - Класс защиты (protection_class)

15. ГАРАНТИЯ:
    - Срок гарантии (warranty_period)
    - Срок годности (shelf_life)

16. ДОПОЛНИТЕЛЬНО:
    - Аромат/запах (fragrance)
    - Вкус (flavor)
    - Форма (shape)
    - Тип поверхности (surface_type)
    - Совместимость со смазками (lubricant_compatible)

══════════════════════════════════════════════════════════════
ПРАВИЛА ПАРСИНГА:
══════════════════════════════════════════════════════════════

1. ЧИСЛОВЫЕ значения — ТОЛЬКО числа без единиц: "length_cm": 15.5 (не "15.5 см")
2. ТЕКСТОВЫЕ значения — полный текст: "primary_material": "медицинский силикон"
3. СПИСКИ — массив строк: "materials_list": ["силикон", "ABS пластик"]
4. БУЛЕВЫ — true/false: "waterproof": true
5. Запятые → точки: 7,5 → 7.5
6. НЕ придумывай данных — только из текста. Если не ясно — не включай
7. Вес ВСЕГДА в граммах, длины в см, объём в мл
8. Для WB цветов используй стандартные: белый, чёрный, красный, синий, зелёный, розовый, фиолетовый, серый, бежевый, коричневый, оранжевый, жёлтый, голубой, бордовый, золотой, серебристый, телесный, прозрачный, мультиколор
9. ЗАПРЕЩЁННЫЕ СИМВОЛЫ: ™ ® © ℠ ℗ — НЕ ИСПОЛЬЗУЙ их НИКОГДА в текстовых полях
10. wb_subject ДОЛЖЕН точно совпадать с одной из ДОСТУПНЫХ КАТЕГОРИЙ МАРКЕТПЛЕЙСА (если список предоставлен ниже)

══════════════════════════════════════════════════════════════
ФОРМАТ ОТВЕТА (СТРОГО JSON):
══════════════════════════════════════════════════════════════
{
    "product_identity": {
        "product_type": "<тип товара>",
        "product_subtype": "<подтип>",
        "wb_category": "<предполагаемая категория WB>",
        "wb_subject": "<предмет WB>",
        "purpose": "<назначение>",
        "application_area": "<область применения>"
    },
    "brand_info": {
        "brand": "<бренд>",
        "brand_normalized": "<для WB>",
        "manufacturer": "<производитель>",
        "product_line": "<серия>",
        "manufacturer_sku": "<артикул>"
    },
    "physical": {
        "length_cm": null,
        "width_cm": null,
        "height_cm": null,
        "depth_cm": null,
        "diameter_cm": null,
        "thickness_cm": null,
        "weight_g": null,
        "volume_ml": null,
        "working_length_cm": null,
        "max_diameter_cm": null,
        "min_diameter_cm": null,
        "circumference_cm": null
    },
    "package": {
        "package_length_cm": null,
        "package_width_cm": null,
        "package_height_cm": null,
        "package_weight_g": null,
        "package_type": null
    },
    "materials": {
        "primary_material": "<основной>",
        "composition": "<полный состав>",
        "materials_list": [],
        "material_properties": [],
        "surface_texture": null
    },
    "color": {
        "primary_color": "<основной>",
        "wb_color": "<для WB>",
        "secondary_colors": [],
        "color_family": null,
        "is_multicolor": false,
        "is_transparent": false,
        "pattern": null
    },
    "sizing": {
        "size_type": null,
        "available_sizes": [],
        "ru_sizes": [],
        "international_sizes": [],
        "chest_cm": null,
        "waist_cm": null,
        "hips_cm": null,
        "fit_type": null
    },
    "audience": {
        "gender": null,
        "age_group": null,
        "age_range": null,
        "target_audience": null
    },
    "seasonality": {
        "season": null,
        "usage_conditions": null,
        "temperature_range": null,
        "waterproof": null
    },
    "contents": {
        "package_contents": [],
        "items_count": null,
        "batteries_included": null,
        "power_type": null,
        "charger_included": null
    },
    "functionality": {
        "special_features": [],
        "modes_count": null,
        "control_type": null,
        "is_silent": null,
        "compatibility": null
    },
    "care": {
        "care_instructions": null,
        "storage_conditions": null,
        "washable": null
    },
    "origin": {
        "country_of_origin": null,
        "importer": null
    },
    "safety": {
        "certifications": [],
        "warnings": [],
        "protection_class": null
    },
    "warranty": {
        "warranty_period": null,
        "shelf_life": null
    },
    "extra": {
        "fragrance": null,
        "flavor": null,
        "shape": null,
        "surface_type": null
    },
    "marketplace_ready": {
        "wb_title_suggestion": "<предложение SEO заголовка до 60 символов>",
        "wb_description_short": "<краткое описание для карточки до 1000 символов>",
        "search_keywords": ["ключевое1", "ключевое2"],
        "bullet_points": ["преимущество1", "преимущество2"]
    },
    "parsing_meta": {
        "total_fields_found": 0,
        "total_fields_possible": 0,
        "fill_percentage": 0.0,
        "confidence": 0.0,
        "data_sources_used": ["title", "description", "original_data"],
        "notes": "<заметки о парсинге>"
    }
}

══════════════════════════════════════════════════════════════
ВАЖНО:
- Заполни МАКСИМУМ полей из всех доступных данных
- Если данных недостаточно — поставь null, не придумывай
- Верни ТОЛЬКО валидный JSON
- marketplace_ready — обязательно заполни, это ключевые данные для продавца
══════════════════════════════════════════════════════════════"""
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
    timeout: int = 120
    # Дополнительные параметры
    top_p: float = 0.95
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    # Кастомные инструкции для каждой AI функции
    custom_category_instruction: str = ""
    custom_size_instruction: str = ""
    custom_seo_title_instruction: str = ""
    custom_keywords_instruction: str = ""
    custom_bullets_instruction: str = ""
    custom_description_instruction: str = ""
    custom_rich_content_instruction: str = ""
    custom_analysis_instruction: str = ""
    # Новые кастомные инструкции
    custom_dimensions_instruction: str = ""
    custom_clothing_sizes_instruction: str = ""
    custom_brand_instruction: str = ""
    custom_material_instruction: str = ""
    custom_color_instruction: str = ""
    custom_attributes_instruction: str = ""
    custom_parsing_instruction: str = ""
    # Для логирования
    seller_id: int = 0

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
            timeout=getattr(settings, 'ai_timeout', 120) or 120,
            top_p=getattr(settings, 'ai_top_p', 0.95) or 0.95,
            presence_penalty=getattr(settings, 'ai_presence_penalty', 0.0) or 0.0,
            frequency_penalty=getattr(settings, 'ai_frequency_penalty', 0.0) or 0.0,
            custom_category_instruction=getattr(settings, 'ai_category_instruction', '') or '',
            custom_size_instruction=getattr(settings, 'ai_size_instruction', '') or '',
            custom_seo_title_instruction=getattr(settings, 'ai_seo_title_instruction', '') or '',
            custom_keywords_instruction=getattr(settings, 'ai_keywords_instruction', '') or '',
            custom_bullets_instruction=getattr(settings, 'ai_bullets_instruction', '') or '',
            custom_description_instruction=getattr(settings, 'ai_description_instruction', '') or '',
            custom_rich_content_instruction=getattr(settings, 'ai_rich_content_instruction', '') or '',
            custom_analysis_instruction=getattr(settings, 'ai_analysis_instruction', '') or '',
            # Новые кастомные инструкции
            custom_dimensions_instruction=getattr(settings, 'ai_dimensions_instruction', '') or '',
            custom_clothing_sizes_instruction=getattr(settings, 'ai_clothing_sizes_instruction', '') or '',
            custom_brand_instruction=getattr(settings, 'ai_brand_instruction', '') or '',
            custom_material_instruction=getattr(settings, 'ai_material_instruction', '') or '',
            custom_color_instruction=getattr(settings, 'ai_color_instruction', '') or '',
            custom_attributes_instruction=getattr(settings, 'ai_attributes_instruction', '') or '',
            custom_parsing_instruction=getattr(settings, 'ai_parsing_instruction', '') or '',
            seller_id=getattr(settings, 'seller_id', 0) or 0
        )


class AIClient:
    """
    Клиент для работы с AI API
    Поддерживает OpenAI-совместимые API (Cloud.ru, OpenAI, Custom)
    """

    def __init__(self, config: AIConfig):
        self.config = config
        self.last_error: Optional[str] = None  # детали последней ошибки
        self._session = requests.Session()
        self._session.headers.update({
            'Content-Type': 'application/json'
        })

        # Для Cloud.ru используем TokenManager (нужен token exchange)
        self._token_manager: Optional[CloudRuTokenManager] = None
        if config.provider == AIProvider.CLOUDRU:
            self._token_manager = get_cloudru_token_manager(config.api_key)
        else:
            # Для OpenAI/Custom используем API key напрямую
            self._session.headers['Authorization'] = f'Bearer {config.api_key}'

    def _get_auth_header(self) -> Optional[str]:
        """Получает актуальный Authorization header"""
        if self._token_manager:
            # Cloud.ru - получаем свежий access token
            token = self._token_manager.get_access_token()
            if token:
                auth_header = f'Bearer {token}'
                logger.info(f"🔐 Auth header: Bearer {token[:20]}... (длина токена: {len(token)})")
                return auth_header
            return None
        else:
            # OpenAI/Custom - API key уже в сессии
            return self._session.headers.get('Authorization')

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

        # Ограничиваем max_tokens разумным потолком (16k достаточно для любого ответа).
        # Слишком большое значение (100k+) вызывает 400 "exceeds model context length"
        # т.к. API проверяет prompt_tokens + max_tokens ≤ context_window.
        effective_max_tokens = min(max_tokens or self.config.max_tokens, 16384)

        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": effective_max_tokens,
            "top_p": self.config.top_p,
            "presence_penalty": self.config.presence_penalty,
            "frequency_penalty": self.config.frequency_penalty
        }

        # response_format не все модели поддерживают, добавляем опционально
        if response_format and self.config.provider != AIProvider.CLOUDRU:
            payload["response_format"] = response_format

        self.last_error = None
        max_retries = 3
        retryable_statuses = {429, 500, 502, 503, 504}

        # Получаем актуальный Authorization header
        auth_header = self._get_auth_header()
        if not auth_header:
            self.last_error = f"Ошибка авторизации: не удалось получить токен для {self.config.provider.value}"
            logger.error(f"❌ {self.last_error}")
            return None

        headers = {'Authorization': auth_header}

        for attempt in range(1, max_retries + 1):
            try:
                if attempt == 1:
                    logger.info(f"🤖 AI запрос к {self.config.provider.value}: модель={self.config.model}")
                else:
                    logger.info(f"🔄 AI retry {attempt}/{max_retries} к {self.config.provider.value}")
                logger.info(f"📍 URL: {url}")
                logger.debug(f"Payload: {json.dumps(payload, ensure_ascii=False)[:500]}")

                # Разделяем таймаут: connect быстро, read — по настройке.
                # Tuple (connect_timeout, read_timeout) предотвращает зависание
                # на медленных моделях (GLM-4.7-Flash и др.)
                connect_timeout = min(self.config.timeout, 30)
                read_timeout = self.config.timeout
                response = self._session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=(connect_timeout, read_timeout)
                )

                # Логируем ответ для отладки
                if response.status_code != 200:
                    logger.error(f"❌ AI HTTP {response.status_code}: {response.text[:500]}")

                # Retryable HTTP errors
                if response.status_code in retryable_statuses:
                    wait = 2 ** attempt  # 2s, 4s, 8s
                    self.last_error = (
                        f"HTTP {response.status_code} от {self.config.provider.value} "
                        f"(попытка {attempt}/{max_retries})"
                    )
                    if response.status_code == 429:
                        # Попробуем получить retry-after из заголовка
                        retry_after = response.headers.get('Retry-After')
                        if retry_after:
                            try:
                                wait = max(wait, int(retry_after))
                            except (ValueError, TypeError):
                                pass
                        self.last_error = (
                            f"Rate limit 429 от {self.config.provider.value} "
                            f"(попытка {attempt}/{max_retries}, жду {wait}с)"
                        )
                    logger.warning(f"⏳ {self.last_error}")
                    if attempt < max_retries:
                        import time
                        time.sleep(wait)
                        continue
                    # Последняя попытка — пусть raise_for_status выбросит ошибку
                    response.raise_for_status()

                response.raise_for_status()

                data = response.json()
                content = data.get('choices', [{}])[0].get('message', {}).get('content')

                if content is None:
                    self.last_error = (
                        f"AI вернул пустой ответ (модель: {self.config.model}, "
                        f"провайдер: {self.config.provider.value})"
                    )
                    logger.error(f"❌ {self.last_error}: {data}")
                    return None

                logger.info(f"✅ AI ответ получен ({len(content)} символов)")
                logger.debug(f"Response: {content[:500]}...")

                return content

            except requests.exceptions.Timeout:
                self.last_error = (
                    f"Таймаут {self.config.timeout}с — AI не ответил вовремя "
                    f"(провайдер: {self.config.provider.value}, модель: {self.config.model})"
                )
                logger.error(f"⏱️ {self.last_error}")
                # Таймауты не ретраим: если модель зависла, повтор тоже зависнет.
                # Лучше быстро вернуть ошибку и перейти к следующему товару.
                return None
            except requests.exceptions.ConnectionError as e:
                self.last_error = (
                    f"Нет соединения с AI API ({self.config.provider.value}): "
                    f"{str(e)[:150]} (попытка {attempt}/{max_retries})"
                )
                logger.error(f"❌ {self.last_error}")
                if attempt < max_retries:
                    import time
                    time.sleep(2 ** attempt)
                    continue
                return None
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code
                body = e.response.text[:300]
                if status in (401, 403):
                    # Для Cloud.ru пробуем сбросить токен и повторить (токен мог протухнуть)
                    if self._token_manager and attempt < max_retries:
                        logger.warning(f"⚠️ HTTP {status} от {self.config.provider.value}, сбрасываем токен и пробуем снова...")
                        self._token_manager._access_token = None
                        self._token_manager._token_expires_at = 0
                        import time
                        time.sleep(2)
                        # Получаем новый токен
                        auth_header = self._get_auth_header()
                        if auth_header:
                            headers = {'Authorization': auth_header}
                            continue
                    self.last_error = (
                        f"Ошибка авторизации AI (HTTP {status}): проверьте API-ключ "
                        f"для {self.config.provider.value}"
                    )
                    # Добавляем тело ответа для отладки
                    if body:
                        self.last_error += f" (ответ: {body[:100]})"
                elif status == 429:
                    self.last_error = (
                        f"Превышен лимит запросов AI (HTTP 429) — "
                        f"провайдер {self.config.provider.value} ограничил частоту "
                        f"(все {max_retries} попыток исчерпаны)"
                    )
                elif status >= 500:
                    self.last_error = (
                        f"Сервер AI недоступен (HTTP {status}): "
                        f"{self.config.provider.value} — {body[:100]}"
                    )
                else:
                    self.last_error = (
                        f"HTTP {status} от AI ({self.config.provider.value}): {body[:150]}"
                    )
                logger.error(f"❌ {self.last_error}")
                return None
            except Exception as e:
                self.last_error = f"Непредвиденная ошибка AI: {type(e).__name__}: {str(e)[:200]}"
                logger.error(f"❌ {self.last_error}")
                import traceback
                logger.error(traceback.format_exc())
                return None

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

            user_prompt = self.build_user_prompt(**kwargs)

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            prompt_len = len(system_prompt) + len(user_prompt)
            task_name = getattr(self, 'task_name', self.__class__.__name__)
            logger.info(
                f"[{task_name}] Промпт: system={len(system_prompt)} + user={len(user_prompt)} "
                f"= {prompt_len} символов (~{prompt_len // 3} токенов)"
            )
            response = self.client.chat_completion(messages)

            if not response:
                detail = self.client.last_error or "пустой ответ"
                task_name = getattr(self, 'task_name', self.__class__.__name__)
                return False, None, f"[{task_name}] {detail} (промпт: {prompt_len} символов)"

            result = self.parse_response(response)
            if result is None:
                task_name = getattr(self, 'task_name', self.__class__.__name__)
                # Показываем начало ответа для диагностики
                snippet = response[:300].replace('\n', ' ')
                return False, None, (
                    f"[{task_name}] AI ответил, но формат некорректный. "
                    f"Ответ ({len(response)} симв.): «{snippet}»"
                )

            return True, result, None

        except Exception as e:
            logger.error(f"❌ Ошибка выполнения AI задачи: {e}")
            import traceback
            logger.error(traceback.format_exc())
            task_name = getattr(self, 'task_name', self.__class__.__name__)
            return False, None, f"[{task_name}] {type(e).__name__}: {str(e)[:200]}"


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
        keyword_hints = kwargs.get('keyword_hints', [])

        prompt = f"""Определи категорию WB для товара:

НАЗВАНИЕ ТОВАРА: {product_title}
КАТЕГОРИЯ ИЗ ИСТОЧНИКА: {source_category}
ВСЕ КАТЕГОРИИ: {' > '.join(all_categories) if all_categories else 'Не указаны'}
БРЕНД: {brand or 'Не указан'}
"""
        if description:
            prompt += f"ОПИСАНИЕ: {description[:500]}\n"

        if keyword_hints:
            hints_text = ', '.join(
                f'"{h["keyword"]}" → {h["suggested_category"]} (ID {h["suggested_id"]})'
                for h in keyword_hints[:5]
            )
            prompt += (
                f"\nПОДСКАЗКИ ПО КЛЮЧЕВЫМ СЛОВАМ (могут быть неточными, "
                f"учитывай контекст): {hints_text}\n"
            )

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


class SEOTitleTask(AITask):
    """Задача генерации SEO-оптимизированного заголовка"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["seo_title"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        category = kwargs.get('category', '')
        brand = kwargs.get('brand', '')
        description = kwargs.get('description', '')

        return f"""Оптимизируй заголовок товара:

ТЕКУЩИЙ ЗАГОЛОВОК: {title}
КАТЕГОРИЯ: {category or 'Не указана'}
БРЕНД: {brand or 'Не указан'}
ОПИСАНИЕ: {description[:300] if description else 'Не указано'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'title': data.get('title', ''),
                'keywords_used': data.get('keywords_used', []),
                'improvements': data.get('improvements', [])
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class KeywordsTask(AITask):
    """Задача генерации ключевых слов"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["keywords"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        category = kwargs.get('category', '')
        description = kwargs.get('description', '')

        return f"""Сгенерируй ключевые слова для товара:

НАЗВАНИЕ: {title}
КАТЕГОРИЯ: {category or 'Не указана'}
ОПИСАНИЕ: {description[:500] if description else 'Не указано'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'keywords': data.get('keywords', []),
                'search_queries': data.get('search_queries', [])
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class BulletPointsTask(AITask):
    """Задача генерации bullet points"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["bullet_points"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        characteristics = kwargs.get('characteristics', {})

        chars_str = ""
        if characteristics:
            chars_str = "\n".join([f"- {k}: {v}" for k, v in characteristics.items()])

        return f"""Создай преимущества товара:

НАЗВАНИЕ: {title}
ОПИСАНИЕ: {description[:500] if description else 'Не указано'}
ХАРАКТЕРИСТИКИ:
{chars_str or 'Не указаны'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'bullet_points': data.get('bullet_points', []),
                'target_audience': data.get('target_audience', '')
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class DescriptionEnhanceTask(AITask):
    """Задача улучшения описания"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["description_enhance"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        category = kwargs.get('category', '')

        return f"""Улучши описание товара:

НАЗВАНИЕ: {title}
КАТЕГОРИЯ: {category or 'Не указана'}
ТЕКУЩЕЕ ОПИСАНИЕ:
{description or 'Описание отсутствует'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'description': data.get('description', ''),
                'structure': data.get('structure', {})
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class RichContentTask(AITask):
    """Задача генерации rich контента для карточки"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["rich_content"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        category = kwargs.get('category', '')
        brand = kwargs.get('brand', '')
        characteristics = kwargs.get('characteristics', {})
        price = kwargs.get('price', 0)

        chars_str = ""
        if characteristics:
            # Фильтруем служебные поля
            chars_str = "\n".join([
                f"- {k}: {v}" for k, v in characteristics.items()
                if not k.startswith('_')
            ])

        return f"""Создай продающий rich контент для товара:

НАЗВАНИЕ: {title}
БРЕНД: {brand or 'Не указан'}
КАТЕГОРИЯ: {category or 'Не указана'}
ЦЕНА: {price} руб.

ОПИСАНИЕ:
{description[:800] if description else 'Описание отсутствует'}

ХАРАКТЕРИСТИКИ:
{chars_str or 'Не указаны'}

Создай мощный продающий контент!"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)

            # Новый формат со слайдами
            result = {
                'slides': data.get('slides', []),
                'design_recommendations': data.get('design_recommendations', {}),
                'total_slides': data.get('total_slides', len(data.get('slides', []))),
                'main_usp': data.get('main_usp', ''),
                'target_audience': data.get('target_audience', ''),
                # Совместимость со старым форматом
                'blocks': data.get('blocks', []),
                'main_message': data.get('main_message', '')
            }

            # Если слайды пустые, но есть блоки - используем блоки
            if not result['slides'] and result['blocks']:
                result['slides'] = result['blocks']
                result['total_slides'] = len(result['blocks'])

            return result
        except Exception as e:
            logger.error(f"Ошибка парсинга Rich content: {e}")
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class CardAnalysisTask(AITask):
    """Задача анализа карточки товара"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["card_analysis"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        category = kwargs.get('category', '')
        characteristics = kwargs.get('characteristics', {})
        photos_count = kwargs.get('photos_count', 0)
        price = kwargs.get('price', 0)

        chars_str = ""
        if characteristics:
            chars_str = "\n".join([f"- {k}: {v}" for k, v in characteristics.items()])

        return f"""Проанализируй карточку товара:

ЗАГОЛОВОК: {title}
КАТЕГОРИЯ: {category or 'Не указана'}
ОПИСАНИЕ: {description[:800] if description else 'Отсутствует'}
ХАРАКТЕРИСТИКИ:
{chars_str or 'Не заполнены'}
ФОТО: {photos_count} шт.
ЦЕНА: {price} руб."""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'score': data.get('score', 0),
                'issues': data.get('issues', []),
                'recommendations': data.get('recommendations', []),
                'strengths': data.get('strengths', [])
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


# ============================================================================
# НОВЫЕ AI ЗАДАЧИ ДЛЯ РАСШИРЕННОГО АНАЛИЗА ТОВАРОВ
# ============================================================================

class DimensionsExtractionTask(AITask):
    """Задача извлечения физических габаритов товара"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["dimensions_extraction"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        characteristics = kwargs.get('characteristics', {})
        sizes_text = kwargs.get('sizes_text', '')

        chars_str = ""
        if characteristics:
            chars_str = "\n".join([f"- {k}: {v}" for k, v in characteristics.items()])

        return f"""Извлеки габариты товара:

НАЗВАНИЕ: {title}
ОПИСАНИЕ: {description[:800] if description else 'Не указано'}
ХАРАКТЕРИСТИКИ:
{chars_str or 'Не указаны'}
СТРОКА РАЗМЕРОВ: {sizes_text or 'Не указана'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'dimensions': data.get('dimensions', {}),
                'package_dimensions': data.get('package_dimensions', {}),
                'raw_text': data.get('raw_text', ''),
                'confidence': max(0.0, min(1.0, float(data.get('confidence', 0.5))))
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class CategoryDimensionsTask(AITask):
    """Задача извлечения характеристик на основе списка характеристик категории WB"""

    def __init__(self, client: 'AIClient', custom_instruction: str = '', category_characteristics: List[str] = None):
        super().__init__(client, custom_instruction)
        self.category_characteristics = category_characteristics or []

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction

        chars_list = "\n".join(self.category_characteristics) if self.category_characteristics else "Характеристики не указаны"

        return f"""Ты эксперт по заполнению характеристик товаров для маркетплейса Wildberries.

Твоя задача - извлечь значения для КОНКРЕТНЫХ характеристик категории из текста товара.

ХАРАКТЕРИСТИКИ КАТЕГОРИИ ДЛЯ ЗАПОЛНЕНИЯ:
{chars_list}

ВАЖНЫЕ ПРАВИЛА:
1. Заполняй ТОЛЬКО характеристики из списка выше
2. Если характеристика помечена [ОБЯЗАТЕЛЬНО] - постарайся найти или вычислить значение
3. Для веса (г, кг) - ВСЕГДА указывай в граммах
4. Для длины/ширины/высоты - указывай в сантиметрах
5. Если значение не найдено - не включай его в ответ
6. Если есть несколько похожих характеристик (Длина, Длина изделия, Максимальная длина) - заполни ВСЕ подходящие одним значением
7. Числа с запятой (7,5) преобразуй в формат с точкой (7.5)
8. Для диапазонов (15-20 см) можно указать либо диапазон, либо максимальное значение

ПРИМЕРЫ:
- "длина 7,5 см, диаметр 2 см, вес 50 г"
  Заполни: Длина = 7.5, Диаметр = 2, Вес = 50

- "размер M (44-46), рост 170-176"
  Заполни: Размер = M, Российский размер = 44-46

ФОРМАТ ОТВЕТА (СТРОГО JSON):
{{
    "extracted_values": {{
        "Название характеристики": "значение",
        ...
    }},
    "missing_required": ["список обязательных характеристик которые не удалось найти"],
    "suggestions": {{
        "Название характеристики": "предположительное значение с объяснением"
    }},
    "raw_found": ["найденные в тексте размеры/веса"],
    "confidence": <число от 0.0 до 1.0>
}}

ВАЖНО: Отвечай ТОЛЬКО валидным JSON."""

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        characteristics = kwargs.get('characteristics', {})
        sizes_text = kwargs.get('sizes_text', '')

        chars_str = ""
        if characteristics:
            chars_str = "\n".join([f"- {k}: {v}" for k, v in characteristics.items()])

        return f"""Извлеки значения характеристик из данных товара:

НАЗВАНИЕ: {title}
СТРОКА РАЗМЕРОВ: {sizes_text or 'Не указана'}
СУЩЕСТВУЮЩИЕ ХАРАКТЕРИСТИКИ:
{chars_str or 'Не указаны'}
ОПИСАНИЕ: {description[:1000] if description else 'Не указано'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'extracted_values': data.get('extracted_values', {}),
                'missing_required': data.get('missing_required', []),
                'suggestions': data.get('suggestions', {}),
                'raw_found': data.get('raw_found', []),
                'confidence': max(0.0, min(1.0, float(data.get('confidence', 0.5))))
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class AllCharacteristicsTask(AITask):
    """Задача извлечения ВСЕХ характеристик категории WB (обязательных и необязательных)"""

    def __init__(self, client: 'AIClient', custom_instruction: str = '', category_characteristics: List[str] = None):
        super().__init__(client, custom_instruction)
        self.category_characteristics = category_characteristics or []

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction

        chars_list = "\n".join(self.category_characteristics) if self.category_characteristics else "Характеристики не указаны"

        return f"""Ты эксперт по заполнению карточек товаров для Wildberries. Твоя задача - МАКСИМАЛЬНО ПОЛНО заполнить карточку товара.

═══════════════════════════════════════════════════════════════
ХАРАКТЕРИСТИКИ ДЛЯ ЗАПОЛНЕНИЯ:
═══════════════════════════════════════════════════════════════
{chars_list}

═══════════════════════════════════════════════════════════════
КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА ПАРСИНГА:
═══════════════════════════════════════════════════════════════

1. КАЖДУЮ ХАРАКТЕРИСТИКУ - В ОТДЕЛЬНОЕ ПОЛЕ!
   ❌ НЕПРАВИЛЬНО: "Размеры": "длина 15 см, диаметр 3 см"
   ✅ ПРАВИЛЬНО:   "Длина": 15, "Диаметр": 3

2. РАЗБИРАЙ СЛОЖНЫЕ СТРОКИ НА СОСТАВЛЯЮЩИЕ:
   Входные данные: "размер трусиков 48-50, длина вибропули 7,5 см, диаметр 2 см"
   ✅ Результат:
   - "Размер": "48-50" (или конкретный размер одежды)
   - "Длина": 7.5 (или "Длина вибропули" если есть такая характеристика)
   - "Диаметр": 2

3. ЧИСЛОВЫЕ ХАРАКТЕРИСТИКИ - ТОЛЬКО ЧИСЛА:
   ❌ "Длина": "15 см"
   ✅ "Длина": 15
   ❌ "Вес": "200 грамм"
   ✅ "Вес": 200

4. ТЕКСТОВЫЕ ХАРАКТЕРИСТИКИ - ПОЛНЫЙ ТЕКСТ:
   ❌ "Материал": 95 (это неправильно!)
   ✅ "Материал": "силикон медицинский"
   ❌ "Цвет": 1
   ✅ "Цвет": "черный"

5. ЕДИНИЦЫ ИЗМЕРЕНИЯ (преобразуй если нужно):
   - Вес: ВСЕГДА в граммах (1 кг = 1000 г)
   - Длина/ширина/высота/диаметр: ВСЕГДА в сантиметрах
   - Объем: ВСЕГДА в миллилитрах (1 л = 1000 мл)

6. ЗАПЯТЫЕ В ЧИСЛАХ → ТОЧКИ:
   "7,5 см" → 7.5
   "1.500 г" → 1500

═══════════════════════════════════════════════════════════════
ПРИМЕРЫ ПРАВИЛЬНОГО ПАРСИНГА:
═══════════════════════════════════════════════════════════════

ПРИМЕР 1 - Сложное описание размеров:
Вход: "Общая длина 19 см, рабочая длина 15 см, диаметр в широкой части 4 см, в узкой 2.5 см"
Результат:
{{{{
  "Общая длина": 19,
  "Рабочая длина": 15,
  "Максимальный диаметр": 4,
  "Минимальный диаметр": 2.5
}}}}

ПРИМЕР 2 - Материал с процентами:
Вход: "Материал: 95% силикон, 5% ABS пластик"
Результат:
{{{{
  "Материал": "силикон, ABS пластик",
  "Состав": "95% силикон, 5% ABS пластик"
}}}}

ПРИМЕР 3 - Комплект с размерами:
Вход: "Трусики размер M/L (44-48), вибропуля длина 6 см диаметр 1.8 см"
Результат:
{{{{
  "Размер": "M/L",
  "Размер (RU)": "44-48",
  "Длина": 6,
  "Диаметр": 1.8
}}}}

ПРИМЕР 4 - Вес и упаковка:
Вход: "Вес изделия 150г, в упаковке 200г, размер коробки 20x10x5 см"
Результат:
{{{{
  "Вес товара": 150,
  "Вес с упаковкой": 200,
  "Длина упаковки": 20,
  "Ширина упаковки": 10,
  "Высота упаковки": 5
}}}}

═══════════════════════════════════════════════════════════════
ТИПЫ ПОЛЕЙ И ФОРМАТ ЗНАЧЕНИЙ:
═══════════════════════════════════════════════════════════════

РАЗМЕРНЫЕ (возвращай ТОЛЬКО число):
- Длина, Ширина, Высота, Глубина → число в см
- Диаметр, Толщина, Обхват → число в см
- Вес → число в граммах
- Объем → число в мл

ТЕКСТОВЫЕ (возвращай строку):
- Материал → "силикон", "пластик ABS", "натуральная кожа"
- Цвет → "черный", "розовый", "телесный"
- Бренд → точное название бренда
- Страна → "Китай", "Россия", "Германия"
- Комплектация → "вибратор, зарядка USB, инструкция"
- Особенности → текстовое описание

ВЫБОР ИЗ СПИСКА:
- Если указано [допустимые: да, нет] → выбери "да" или "нет"
- Если указано [допустимые: S, M, L, XL] → выбери из списка

═══════════════════════════════════════════════════════════════
ФОРМАТ ОТВЕТА (СТРОГО JSON):
═══════════════════════════════════════════════════════════════
{{{{
    "extracted_values": {{{{
        "Точное название характеристики из списка": значение,
        "Длина": 15.5,
        "Диаметр": 3,
        "Вес товара": 120,
        "Материал": "медицинский силикон",
        "Цвет": "черный",
        "Бренд": "Название бренда"
    }}}},
    "missing_required": ["характеристики которые не удалось найти"],
    "fill_summary": {{{{
        "filled_count": 10,
        "total_available": 15,
        "fill_percentage": 66.7
    }}}},
    "confidence": 0.85
}}}}

═══════════════════════════════════════════════════════════════
ФИНАЛЬНЫЕ ТРЕБОВАНИЯ:
═══════════════════════════════════════════════════════════════
✓ Используй ТОЧНЫЕ названия характеристик из списка выше
✓ Каждое значение - в СВОЁ поле
✓ Числа без единиц измерения
✓ Текст - полный, не обрезанный
✓ НЕ придумывай данные - только из текста товара
✓ Отвечай ТОЛЬКО валидным JSON"""

    def build_user_prompt(self, **kwargs) -> str:
        product_info = kwargs.get('product_info', {})
        existing_characteristics = kwargs.get('existing_characteristics', {})
        original_data = kwargs.get('original_data', {})

        parts = []
        parts.append("=" * 60)
        parts.append("ДАННЫЕ ТОВАРА ДЛЯ АНАЛИЗА:")
        parts.append("=" * 60)

        if product_info.get('title'):
            parts.append(f"\n📌 НАЗВАНИЕ ТОВАРА:\n{product_info['title']}")

        if product_info.get('brand'):
            parts.append(f"\n🏷️ БРЕНД: {product_info['brand']}")

        if product_info.get('category'):
            parts.append(f"\n📁 КАТЕГОРИЯ WB: {product_info['category']}")

        if product_info.get('colors'):
            colors = product_info['colors']
            if isinstance(colors, list):
                parts.append(f"\n🎨 ЦВЕТА: {', '.join(str(c) for c in colors)}")
            else:
                parts.append(f"\n🎨 ЦВЕТА: {colors}")

        if product_info.get('materials'):
            materials = product_info['materials']
            if isinstance(materials, list):
                parts.append(f"\n🧱 МАТЕРИАЛЫ: {', '.join(str(m) for m in materials)}")
            else:
                parts.append(f"\n🧱 МАТЕРИАЛЫ: {materials}")

        # Размеры - важная секция, разбираем подробно
        if product_info.get('sizes'):
            sizes = product_info['sizes']
            parts.append("\n📐 РАЗМЕРЫ И ГАБАРИТЫ:")
            if isinstance(sizes, dict):
                for k, v in sizes.items():
                    parts.append(f"   • {k}: {v}")
            elif isinstance(sizes, str):
                parts.append(f"   {sizes}")
            else:
                parts.append(f"   {str(sizes)}")

        # Существующие характеристики
        if existing_characteristics:
            parts.append("\n📋 УЖЕ ИЗВЕСТНЫЕ ХАРАКТЕРИСТИКИ:")
            for k, v in existing_characteristics.items():
                parts.append(f"   • {k}: {v}")

        # Оригинальные данные от поставщика (важный источник!)
        if original_data:
            parts.append("\n📦 ОРИГИНАЛЬНЫЕ ДАННЫЕ ОТ ПОСТАВЩИКА:")
            if isinstance(original_data, dict):
                if original_data.get('characteristics'):
                    orig_chars = original_data['characteristics']
                    if isinstance(orig_chars, dict):
                        for k, v in orig_chars.items():
                            parts.append(f"   • {k}: {v}")
                if original_data.get('description'):
                    parts.append(f"\n   Оригинальное описание:\n   {original_data['description'][:1500]}")
            else:
                parts.append(f"   {str(original_data)[:1500]}")

        # Описание товара
        if product_info.get('description'):
            desc = product_info['description'][:2500]
            parts.append(f"\n📝 ПОЛНОЕ ОПИСАНИЕ ТОВАРА:\n{desc}")

        parts.append("\n" + "=" * 60)
        parts.append("ЗАДАНИЕ:")
        parts.append("=" * 60)
        parts.append("""
1. Внимательно прочитай ВСЕ данные выше
2. Найди ВСЕ размеры, характеристики, материалы
3. РАЗБЕЙ сложные строки на отдельные характеристики
4. Заполни МАКСИМУМ полей из списка характеристик категории
5. Верни результат в формате JSON
""")

        return "\n".join(parts)

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'extracted_values': data.get('extracted_values', {}),
                'missing_required': data.get('missing_required', []),
                'fill_summary': data.get('fill_summary', {}),
                'confidence': max(0.0, min(1.0, float(data.get('confidence', 0.5))))
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class FullProductParsingTask(AITask):
    """Задача полного AI парсинга товара — извлечение ВСЕХ возможных характеристик"""

    def __init__(self, client, custom_instruction: str = "", marketplace_categories_block: str = ""):
        super().__init__(client, custom_instruction)
        self.marketplace_categories_block = marketplace_categories_block

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            base = self.custom_instruction
        else:
            base = DEFAULT_INSTRUCTIONS["full_product_parsing"]["template"]

        if self.marketplace_categories_block:
            base += (
                "\n\n══════════════════════════════════════════════════════════════\n"
                + self.marketplace_categories_block
                + "\n══════════════════════════════════════════════════════════════"
            )

        return base

    def build_user_prompt(self, **kwargs) -> str:
        product_data = kwargs.get('product_data', {})

        parts = []
        parts.append("=" * 60)
        parts.append("ПОЛНЫЕ ДАННЫЕ ТОВАРА ДЛЯ АНАЛИЗА:")
        parts.append("=" * 60)

        if product_data.get('title'):
            parts.append(f"\nНАЗВАНИЕ: {product_data['title']}")

        if product_data.get('description'):
            parts.append(f"\nОПИСАНИЕ:\n{product_data['description'][:3000]}")

        if product_data.get('brand'):
            parts.append(f"\nБРЕНД: {product_data['brand']}")

        if product_data.get('category'):
            parts.append(f"\nКАТЕГОРИЯ ПОСТАВЩИКА: {product_data['category']}")

        if product_data.get('wb_category'):
            parts.append(f"\nКАТЕГОРИЯ WB: {product_data['wb_category']}")

        if product_data.get('vendor_code'):
            parts.append(f"\nАРТИКУЛ: {product_data['vendor_code']}")

        if product_data.get('barcode'):
            parts.append(f"\nШТРИХКОД: {product_data['barcode']}")

        if product_data.get('price'):
            parts.append(f"\nЦЕНА: {product_data['price']} руб.")

        if product_data.get('gender'):
            parts.append(f"\nПОЛ: {product_data['gender']}")

        if product_data.get('country'):
            parts.append(f"\nСТРАНА: {product_data['country']}")

        if product_data.get('season'):
            parts.append(f"\nСЕЗОН: {product_data['season']}")

        # Цвета
        colors = product_data.get('colors', [])
        if colors:
            if isinstance(colors, list):
                parts.append(f"\nЦВЕТА: {', '.join(str(c) for c in colors)}")
            else:
                parts.append(f"\nЦВЕТА: {colors}")

        # Материалы
        materials = product_data.get('materials', [])
        if materials:
            if isinstance(materials, list):
                parts.append(f"\nМАТЕРИАЛЫ: {', '.join(str(m) for m in materials)}")
            else:
                parts.append(f"\nМАТЕРИАЛЫ: {materials}")

        # Размеры
        sizes = product_data.get('sizes', {})
        if sizes:
            parts.append(f"\nРАЗМЕРЫ: {json.dumps(sizes, ensure_ascii=False) if isinstance(sizes, (dict, list)) else sizes}")

        # Габариты
        dimensions = product_data.get('dimensions', {})
        if dimensions:
            parts.append(f"\nГАБАРИТЫ: {json.dumps(dimensions, ensure_ascii=False) if isinstance(dimensions, dict) else dimensions}")

        # Характеристики
        characteristics = product_data.get('characteristics', [])
        if characteristics:
            parts.append("\nХАРАКТЕРИСТИКИ:")
            if isinstance(characteristics, list):
                for ch in characteristics:
                    if isinstance(ch, dict):
                        parts.append(f"  - {ch.get('name', '?')}: {ch.get('value', '?')}")
                    else:
                        parts.append(f"  - {ch}")
            elif isinstance(characteristics, dict):
                for k, v in characteristics.items():
                    parts.append(f"  - {k}: {v}")

        # Оригинальные данные поставщика
        original = product_data.get('original_data', {})
        if original and isinstance(original, dict):
            parts.append("\nОРИГИНАЛЬНЫЕ ДАННЫЕ ПОСТАВЩИКА:")
            for k, v in original.items():
                if v and k not in ('photo_urls', 'photo_codes', 'processed_photos'):
                    val_str = str(v)[:500]
                    parts.append(f"  - {k}: {val_str}")

        # AI данные если уже есть
        if product_data.get('ai_seo_title'):
            parts.append(f"\nAI SEO ЗАГОЛОВОК: {product_data['ai_seo_title']}")
        if product_data.get('ai_description'):
            parts.append(f"\nAI ОПИСАНИЕ:\n{product_data['ai_description'][:1500]}")

        parts.append(f"\nФОТОГРАФИЙ: {product_data.get('photos_count', 0)}")

        parts.append("\n" + "=" * 60)
        parts.append("ЗАДАНИЕ: Извлеки ВСЕ характеристики из данных выше. Заполни максимум полей.")
        parts.append("Верни результат строго в JSON формате.")
        parts.append("=" * 60)

        return "\n".join(parts)

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)

            # Подсчитываем заполненность
            filled = 0
            total = 0

            def count_fields(obj, prefix=''):
                nonlocal filled, total
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in ('parsing_meta', 'marketplace_ready'):
                            continue
                        if isinstance(v, (dict, list)):
                            count_fields(v, f"{prefix}.{k}")
                        else:
                            total += 1
                            if v is not None and v != '' and v != 0 and v is not False:
                                filled += 1
                elif isinstance(obj, list):
                    if len(obj) > 0:
                        filled += 1
                    total += 1

            count_fields(data)

            # Обновляем parsing_meta
            if 'parsing_meta' not in data:
                data['parsing_meta'] = {}
            data['parsing_meta']['total_fields_found'] = filled
            data['parsing_meta']['total_fields_possible'] = total
            data['parsing_meta']['fill_percentage'] = round(filled / total * 100, 1) if total > 0 else 0

            return data
        except Exception:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class ClothingSizesTask(AITask):
    """Задача парсинга и стандартизации размеров одежды"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["clothing_sizes"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        sizes_text = kwargs.get('sizes_text', '')
        category = kwargs.get('category', '')

        return f"""Определи размеры одежды:

НАЗВАНИЕ: {title}
КАТЕГОРИЯ: {category or 'Не указана'}
СТРОКА РАЗМЕРОВ: {sizes_text or 'Не указана'}
ОПИСАНИЕ: {description[:500] if description else 'Не указано'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'product_type': data.get('product_type', 'unknown'),
                'size_type': data.get('size_type', 'unknown'),
                'sizes': data.get('sizes', []),
                'dimensions': data.get('dimensions', {}),
                'size_range': data.get('size_range', {}),
                'body_measurements': data.get('body_measurements', {}),
                'fit_type': data.get('fit_type'),
                'raw_text': data.get('raw_text', ''),
                'confidence': max(0.0, min(1.0, float(data.get('confidence', 0.5))))
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class BrandDetectionTask(AITask):
    """Задача автоматического определения бренда товара"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["brand_detection"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        characteristics = kwargs.get('characteristics', {})
        category = kwargs.get('category', '')

        chars_str = ""
        if characteristics:
            chars_str = "\n".join([f"- {k}: {v}" for k, v in characteristics.items()])

        return f"""Определи бренд товара:

НАЗВАНИЕ: {title}
КАТЕГОРИЯ: {category or 'Не указана'}
ХАРАКТЕРИСТИКИ:
{chars_str or 'Не указаны'}
ОПИСАНИЕ: {description[:500] if description else 'Не указано'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'brand': data.get('brand', ''),
                'brand_normalized': data.get('brand_normalized', ''),
                'brand_type': data.get('brand_type', 'unknown'),
                'brand_origin': data.get('brand_origin', ''),
                'alternative_names': data.get('alternative_names', []),
                'is_official': data.get('is_official', False),
                'confidence': max(0.0, min(1.0, float(data.get('confidence', 0.5)))),
                'reasoning': data.get('reasoning', '')
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class MaterialDetectionTask(AITask):
    """Задача определения материалов и состава товара"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["material_detection"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        characteristics = kwargs.get('characteristics', {})
        category = kwargs.get('category', '')

        chars_str = ""
        if characteristics:
            chars_str = "\n".join([f"- {k}: {v}" for k, v in characteristics.items()])

        return f"""Определи материалы товара:

НАЗВАНИЕ: {title}
КАТЕГОРИЯ: {category or 'Не указана'}
ХАРАКТЕРИСТИКИ:
{chars_str or 'Не указаны'}
ОПИСАНИЕ: {description[:800] if description else 'Не указано'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'materials': data.get('materials', []),
                'composition_text': data.get('composition_text', ''),
                'material_type': data.get('material_type', 'unknown'),
                'care_instructions': data.get('care_instructions', []),
                'properties': data.get('properties', {}),
                'confidence': max(0.0, min(1.0, float(data.get('confidence', 0.5))))
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class ColorDetectionTask(AITask):
    """Задача определения цвета товара"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["color_detection"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        characteristics = kwargs.get('characteristics', {})

        chars_str = ""
        if characteristics:
            chars_str = "\n".join([f"- {k}: {v}" for k, v in characteristics.items()])

        return f"""Определи цвет товара:

НАЗВАНИЕ: {title}
ХАРАКТЕРИСТИКИ:
{chars_str or 'Не указаны'}
ОПИСАНИЕ: {description[:500] if description else 'Не указано'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'primary_color': data.get('primary_color', ''),
                'primary_color_wb': data.get('primary_color_wb', ''),
                'secondary_colors': data.get('secondary_colors', []),
                'color_description': data.get('color_description', ''),
                'is_multicolor': data.get('is_multicolor', False),
                'is_transparent': data.get('is_transparent', False),
                'hex_code': data.get('hex_code'),
                'color_family': data.get('color_family', ''),
                'confidence': max(0.0, min(1.0, float(data.get('confidence', 0.5))))
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


class ProductAttributesTask(AITask):
    """Задача комплексного извлечения всех атрибутов товара за один запрос"""

    def get_system_prompt(self) -> str:
        if self.custom_instruction:
            return self.custom_instruction
        return DEFAULT_INSTRUCTIONS["product_attributes"]["template"]

    def build_user_prompt(self, **kwargs) -> str:
        title = kwargs.get('title', '')
        description = kwargs.get('description', '')
        characteristics = kwargs.get('characteristics', {})
        category = kwargs.get('category', '')
        sizes_text = kwargs.get('sizes_text', '')

        chars_str = ""
        if characteristics:
            chars_str = "\n".join([f"- {k}: {v}" for k, v in characteristics.items()])

        return f"""Извлеки все атрибуты товара:

НАЗВАНИЕ: {title}
КАТЕГОРИЯ: {category or 'Не указана'}
ХАРАКТЕРИСТИКИ:
{chars_str or 'Не указаны'}
РАЗМЕРЫ: {sizes_text or 'Не указаны'}
ОПИСАНИЕ: {description[:1000] if description else 'Не указано'}"""

    def parse_response(self, response: str) -> Optional[Dict]:
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)
            return {
                'brand': data.get('brand', {}),
                'colors': data.get('colors', {}),
                'materials': data.get('materials', {}),
                'dimensions': data.get('dimensions', {}),
                'clothing_size': data.get('clothing_size', {}),
                'gender': data.get('gender'),
                'age_group': data.get('age_group'),
                'age_range': data.get('age_range'),
                'season': data.get('season'),
                'country': data.get('country'),
                'package_contents': data.get('package_contents', []),
                'special_features': data.get('special_features', []),
                'overall_confidence': max(0.0, min(1.0, float(data.get('overall_confidence', 0.5))))
            }
        except:
            return None

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group() if match else text


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
        description: str = '',
        keyword_hints: Optional[list] = None,
    ) -> Tuple[Optional[int], Optional[str], float, str]:
        """
        Определяет категорию товара с помощью AI

        Args:
            keyword_hints: Подсказки из CATEGORY_KEYWORDS_MAPPING
                (список dict с keyword/suggested_category/suggested_id).
                AI использует их как контекст, но принимает решение самостоятельно.

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
            description=description,
            keyword_hints=keyword_hints or [],
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

    def generate_seo_title(
        self,
        title: str,
        category: str = '',
        brand: str = '',
        description: str = ''
    ) -> Tuple[bool, Dict, str]:
        """
        Генерирует SEO-оптимизированный заголовок

        Returns:
            Tuple[success, {title, keywords_used, improvements}, error]
        """
        task = SEOTitleTask(self.client, self.config.custom_seo_title_instruction)
        success, result, error = task.execute(
            title=title,
            category=category,
            brand=brand,
            description=description
        )
        if success and result:
            # Очищаем заголовок от HTML тегов и делаем первую букву большой
            if result.get('title'):
                title_clean = strip_html_tags(result['title']).strip()
                title_clean = re.sub(r'[™®©℠℗⁺]', '', title_clean).strip()
                if title_clean:
                    # КРИТИЧНО: Обрезаем до 60 символов (ограничение WB API)
                    if len(title_clean) > 60:
                        # Пытаемся обрезать по пробелу, чтобы не обрезать слово
                        truncated = title_clean[:60]
                        last_space = truncated.rfind(' ')
                        if last_space > 40:  # Если есть пробел после 40 символов
                            truncated = truncated[:last_space]
                        title_clean = truncated.strip()
                    result['title'] = title_clean[0].upper() + title_clean[1:] if title_clean else ''
                    result['length'] = len(result['title'])
                    result['truncated'] = len(strip_html_tags(result.get('title', ''))) > 60
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def generate_keywords(
        self,
        title: str,
        category: str = '',
        description: str = ''
    ) -> Tuple[bool, Dict, str]:
        """
        Генерирует ключевые слова для товара

        Returns:
            Tuple[success, {keywords, search_queries}, error]
        """
        task = KeywordsTask(self.client, self.config.custom_keywords_instruction)
        success, result, error = task.execute(
            title=title,
            category=category,
            description=description
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def generate_bullet_points(
        self,
        title: str,
        description: str = '',
        characteristics: Optional[Dict] = None
    ) -> Tuple[bool, Dict, str]:
        """
        Генерирует bullet points (преимущества)

        Returns:
            Tuple[success, {bullet_points, target_audience}, error]
        """
        task = BulletPointsTask(self.client, self.config.custom_bullets_instruction)
        success, result, error = task.execute(
            title=title,
            description=description,
            characteristics=characteristics or {}
        )
        if success and result:
            # Очищаем каждый буллет от HTML
            if result.get('bullet_points'):
                result['bullet_points'] = [strip_html_tags(b) for b in result['bullet_points']]
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def enhance_description(
        self,
        title: str,
        description: str,
        category: str = ''
    ) -> Tuple[bool, Dict, str]:
        """
        Улучшает описание товара

        Returns:
            Tuple[success, {description, structure}, error]
        """
        task = DescriptionEnhanceTask(self.client, self.config.custom_description_instruction)
        success, result, error = task.execute(
            title=title,
            description=description,
            category=category
        )
        if success and result:
            # Очищаем описание от HTML тегов и запрещённых символов WB
            if result.get('description'):
                cleaned = strip_html_tags(result['description'])
                cleaned = re.sub(r'[™®©℠℗⁺]', '', cleaned)
                result['description'] = cleaned.strip()
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def analyze_card(
        self,
        title: str,
        description: str = '',
        category: str = '',
        characteristics: Optional[Dict] = None,
        photos_count: int = 0,
        price: float = 0
    ) -> Tuple[bool, Dict, str]:
        """
        Анализирует карточку и дает рекомендации

        Returns:
            Tuple[success, {score, issues, recommendations, strengths}, error]
        """
        task = CardAnalysisTask(self.client, self.config.custom_analysis_instruction)
        success, result, error = task.execute(
            title=title,
            description=description,
            category=category,
            characteristics=characteristics or {},
            photos_count=photos_count,
            price=price
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def generate_rich_content(
        self,
        title: str,
        description: str = '',
        category: str = '',
        brand: str = '',
        characteristics: Optional[Dict] = None,
        price: float = 0
    ) -> Tuple[bool, Dict, str]:
        """
        Генерирует продающий rich контент для карточки

        Returns:
            Tuple[success, {hook, pain_points, benefits, features, social_proof, cta, full_description, infographic_texts}, error]
        """
        task = RichContentTask(self.client, self.config.custom_rich_content_instruction)
        success, result, error = task.execute(
            title=title,
            description=description,
            category=category,
            brand=brand,
            characteristics=characteristics or {},
            price=price
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    # =========================================================================
    # НОВЫЕ AI МЕТОДЫ ДЛЯ РАСШИРЕННОГО АНАЛИЗА
    # =========================================================================

    def extract_dimensions(
        self,
        title: str,
        description: str = '',
        characteristics: Optional[Dict] = None,
        sizes_text: str = ''
    ) -> Tuple[bool, Dict, str]:
        """
        Извлекает физические габариты товара

        Returns:
            Tuple[success, {dimensions, package_dimensions, raw_text, confidence}, error]
        """
        task = DimensionsExtractionTask(self.client, self.config.custom_dimensions_instruction)
        success, result, error = task.execute(
            title=title,
            description=description,
            characteristics=characteristics or {},
            sizes_text=sizes_text
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def extract_category_dimensions(
        self,
        title: str,
        description: str = '',
        characteristics: Optional[Dict] = None,
        sizes_text: str = '',
        category_characteristics: Optional[List[str]] = None
    ) -> Tuple[bool, Dict, str]:
        """
        Извлекает характеристики товара на основе списка характеристик категории WB

        Args:
            title: Название товара
            description: Описание
            characteristics: Существующие характеристики товара
            sizes_text: Строка с размерами
            category_characteristics: Список характеристик из WB API для данной категории

        Returns:
            Tuple[success, {extracted_values, missing_required, confidence}, error]
        """
        task = CategoryDimensionsTask(
            self.client,
            self.config.custom_dimensions_instruction,
            category_characteristics or []
        )
        success, result, error = task.execute(
            title=title,
            description=description,
            characteristics=characteristics or {},
            sizes_text=sizes_text
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def extract_all_characteristics(
        self,
        product_info: Dict,
        existing_characteristics: Optional[Dict] = None,
        category_characteristics: Optional[List[str]] = None,
        original_data: Optional[Dict] = None
    ) -> Tuple[bool, Dict, str]:
        """
        Извлекает ВСЕ характеристики категории WB из данных товара

        Args:
            product_info: Информация о товаре (title, description, brand, colors, materials, sizes)
            existing_characteristics: Уже существующие характеристики товара
            category_characteristics: Список характеристик из WB API для категории
            original_data: Оригинальные данные от поставщика

        Returns:
            Tuple[success, {extracted_values, missing_required, fill_summary, confidence}, error]
        """
        task = AllCharacteristicsTask(
            self.client,
            self.config.custom_attributes_instruction if hasattr(self.config, 'custom_attributes_instruction') else '',
            category_characteristics or []
        )
        success, result, error = task.execute(
            product_info=product_info,
            existing_characteristics=existing_characteristics or {},
            original_data=original_data or {}
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def parse_clothing_sizes(
        self,
        title: str,
        sizes_text: str = '',
        description: str = '',
        category: str = ''
    ) -> Tuple[bool, Dict, str]:
        """
        Парсит и стандартизирует размеры одежды

        Returns:
            Tuple[success, {size_type, sizes, size_range, body_measurements, fit_type, confidence}, error]
        """
        task = ClothingSizesTask(self.client, self.config.custom_clothing_sizes_instruction)
        success, result, error = task.execute(
            title=title,
            sizes_text=sizes_text,
            description=description,
            category=category
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def detect_brand(
        self,
        title: str,
        description: str = '',
        characteristics: Optional[Dict] = None,
        category: str = ''
    ) -> Tuple[bool, Dict, str]:
        """
        Определяет бренд товара

        Returns:
            Tuple[success, {brand, brand_normalized, brand_type, confidence, reasoning}, error]
        """
        task = BrandDetectionTask(self.client, self.config.custom_brand_instruction)
        success, result, error = task.execute(
            title=title,
            description=description,
            characteristics=characteristics or {},
            category=category
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def detect_materials(
        self,
        title: str,
        description: str = '',
        characteristics: Optional[Dict] = None,
        category: str = ''
    ) -> Tuple[bool, Dict, str]:
        """
        Определяет материалы и состав товара

        Returns:
            Tuple[success, {materials, composition_text, material_type, properties, confidence}, error]
        """
        task = MaterialDetectionTask(self.client, self.config.custom_material_instruction)
        success, result, error = task.execute(
            title=title,
            description=description,
            characteristics=characteristics or {},
            category=category
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def detect_color(
        self,
        title: str,
        description: str = '',
        characteristics: Optional[Dict] = None
    ) -> Tuple[bool, Dict, str]:
        """
        Определяет цвет товара

        Returns:
            Tuple[success, {primary_color, primary_color_wb, secondary_colors, confidence}, error]
        """
        task = ColorDetectionTask(self.client, self.config.custom_color_instruction)
        success, result, error = task.execute(
            title=title,
            description=description,
            characteristics=characteristics or {}
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def extract_all_attributes(
        self,
        title: str,
        description: str = '',
        characteristics: Optional[Dict] = None,
        category: str = '',
        sizes_text: str = ''
    ) -> Tuple[bool, Dict, str]:
        """
        Комплексное извлечение всех атрибутов товара за один запрос

        Returns:
            Tuple[success, {brand, colors, materials, dimensions, clothing_size, gender, age_group, season, country, ...}, error]
        """
        task = ProductAttributesTask(self.client, self.config.custom_attributes_instruction)
        success, result, error = task.execute(
            title=title,
            description=description,
            characteristics=characteristics or {},
            category=category,
            sizes_text=sizes_text
        )
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI"

    def full_product_parse(
        self,
        product_data: Dict,
        marketplace_categories_block: str = ""
    ) -> Tuple[bool, Dict, str]:
        """
        Полный AI парсинг товара — извлекает ВСЕ возможные характеристики.

        Args:
            product_data: Словарь со всеми данными товара (из get_all_data_for_parsing())
            marketplace_categories_block: Текстовый блок с включёнными категориями маркетплейса
                                          (из MarketplaceService.get_enabled_categories_for_prompt())

        Returns:
            Tuple[success, {product_identity, brand_info, physical, package, materials,
                           color, sizing, audience, seasonality, contents, functionality,
                           care, origin, safety, warranty, extra, marketplace_ready,
                           parsing_meta}, error]
        """
        task = FullProductParsingTask(
            self.client,
            self.config.custom_parsing_instruction,
            marketplace_categories_block=marketplace_categories_block
        )
        success, result, error = task.execute(product_data=product_data)
        if success and result:
            return True, result, ""
        return False, {}, error or "Ошибка AI парсинга"

    def full_optimize(
        self,
        title: str,
        description: str = '',
        category: str = '',
        brand: str = '',
        characteristics: Optional[Dict] = None,
        photos_count: int = 0,
        price: float = 0
    ) -> Dict:
        """
        Полная оптимизация карточки - все AI функции за один вызов

        Returns:
            Dict с результатами всех оптимизаций
        """
        results = {
            'seo_title': None,
            'keywords': None,
            'bullet_points': None,
            'enhanced_description': None,
            'analysis': None,
            'errors': []
        }

        # SEO заголовок
        success, data, error = self.generate_seo_title(title, category, brand, description)
        if success:
            results['seo_title'] = data
        else:
            results['errors'].append(f"SEO заголовок: {error}")

        # Ключевые слова
        success, data, error = self.generate_keywords(title, category, description)
        if success:
            results['keywords'] = data
        else:
            results['errors'].append(f"Ключевые слова: {error}")

        # Bullet points
        success, data, error = self.generate_bullet_points(title, description, characteristics)
        if success:
            results['bullet_points'] = data
        else:
            results['errors'].append(f"Преимущества: {error}")

        # Улучшенное описание
        if description:
            success, data, error = self.enhance_description(title, description, category)
            if success:
                results['enhanced_description'] = data
            else:
                results['errors'].append(f"Описание: {error}")

        # Анализ карточки
        success, data, error = self.analyze_card(
            title, description, category, characteristics, photos_count, price
        )
        if success:
            results['analysis'] = data
        else:
            results['errors'].append(f"Анализ: {error}")

        return results

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
        # Загружаем категории WB: сначала из БД, потом дополняем хардкодом
        categories = {}
        try:
            from models import MarketplaceCategory, Marketplace
            wb_mp = Marketplace.query.filter_by(code='wb').first()
            if wb_mp:
                db_cats = MarketplaceCategory.query.filter_by(
                    marketplace_id=wb_mp.id,
                    is_enabled=True,
                ).all()
                categories = {c.subject_id: c.subject_name for c in db_cats}
                if categories:
                    logger.info(f"Loaded {len(categories)} categories from DB for AI service")
        except Exception as e:
            logger.debug(f"Could not load DB categories: {e}")

        # Дополняем хардкодом (не перезаписываем то, что есть из БД)
        try:
            from services.wb_categories_mapping import WB_ADULT_CATEGORIES
            for cat_id, cat_name in WB_ADULT_CATEGORIES.items():
                if cat_id not in categories:
                    categories[cat_id] = cat_name
        except ImportError:
            logger.warning("Не удалось загрузить WB_ADULT_CATEGORIES")

        if categories:
            _ai_service_instance.set_categories(categories)
            logger.info(f"AI service initialized with {len(categories)} total categories")
        else:
            logger.warning("No categories available for AI service")

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
