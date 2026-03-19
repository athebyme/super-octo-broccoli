# -*- coding: utf-8 -*-
"""
Content Factory Service — генерация и управление контентом для соцсетей

Использует существующий AI Service для генерации текстов
и Image Generation Service для создания визуалов.
"""
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass

import re
import requests as _requests

from models import (
    db, Product, Seller, SupplierProduct, ImportedProduct,
    ProductAnalytics, ContentFactory, ContentItem,
    ContentTemplate, ContentPlan, SocialAccount,
    CONTENT_PLATFORMS, CONTENT_TYPES, CONTENT_STATUSES,
)
from services.ai_service import AIConfig, AIClient, AIProvider

logger = logging.getLogger(__name__)

# Лимиты символов по платформам
PLATFORM_CHAR_LIMITS = {
    'telegram': 4096,
    'vk': 16000,
    'instagram': 2200,
    'tiktok': 300,
    'youtube': 5000,
}


# Маппинг платформ на читаемые названия
PLATFORM_LABELS = {
    'telegram': 'Telegram',
    'vk': 'ВКонтакте',
    'instagram': 'Instagram',
    'tiktok': 'TikTok',
    'youtube': 'YouTube',
}

CONTENT_TYPE_LABELS = {
    'promo_post': 'Промо-пост',
    'review': 'Обзор товара',
    'story_script': 'Сценарий Stories/Reels',
    'carousel': 'Подборка/карусель',
}

# ================================================================
# Встроенные шаблоны (fallback если в БД нет системных шаблонов)
# ================================================================

_BASE_SYSTEM_RULES = """СТРОГИЕ ПРАВИЛА ФОРМАТИРОВАНИЯ:
1. Пиши ТОЛЬКО простым текстом. ЗАПРЕЩЕНО использовать markdown: никаких **, *, ##, ###, ```, -, •
2. Для выделения используй ТОЛЬКО эмодзи и заглавные буквы
3. Для списков используй эмодзи в начале строки (например: ✅, 🔥, 💰, 👉), НЕ тире и НЕ точки
4. Фотографии товара будут прикреплены автоматически — НЕ описывай их в тексте
5. НЕ ВСТАВЛЯЙ ссылки на товар — ссылка будет добавлена автоматически после текста. Не придумывай URL!
6. ЗАПРЕЩЕНО использовать юридические формы: ИП, ООО, ОАО, ЗАО, индивидуальный предприниматель. Упоминай ТОЛЬКО коммерческое название магазина
7. Хештеги пиши отдельным блоком в самом конце через пустую строку, каждый начинается с #
8. ВАЖНО: Текст должен быть КОРОТКИМ и ёмким — максимум 800 символов (без учёта хештегов). К посту прикреплены фото — не нужно описывать внешний вид, фокусируйся на преимуществах и эмоциях."""

_BUILTIN_SYSTEM_PROMPTS = {
    'telegram': {
        'promo_post': 'Ты — опытный SMM-менеджер. Пишешь продающие посты для Telegram-канала магазина на Wildberries.\n\nСтруктура поста:\n1. Эмодзи + цепляющий заголовок (ЗАГЛАВНЫМИ)\n2. 2-3 коротких преимущества с эмодзи\n3. Цена (если есть скидка — покажи старую и новую)\n4. Ссылка на товар на WB\n5. Призыв к действию\n6. Хештеги отдельной строкой\n\n' + _BASE_SYSTEM_RULES,
        'review': 'Ты — блогер-обзорщик товаров с Wildberries для Telegram-канала.\n\nСтруктура обзора:\n1. Заголовок с оценкой (эмодзи звёзды)\n2. Что это за товар (1-2 предложения)\n3. Плюсы (каждый с ✅)\n4. Минусы (каждый с ⚠️) — минимум 1 для достоверности\n5. Для кого подойдёт\n6. Цена + ссылка\n7. Итоговый вердикт\n8. Хештеги\n\n' + _BASE_SYSTEM_RULES,
        'story_script': 'Ты — SMM-менеджер. Создаёшь сценарии для Stories/Reels.\n\nФормат:\nСлайд 1: (интрига)\nСлайд 2: ...\n...\nПоследний слайд: CTA + ссылка\n\nДля каждого слайда укажи текст на экране.\n\n' + _BASE_SYSTEM_RULES,
        'carousel': 'Ты — SMM-менеджер. Составляешь тематические подборки товаров для Telegram-канала.\n\nСтруктура:\n1. Заголовок подборки с эмодзи\n2. Каждый товар: номер, название, краткое описание (1-2 предложения), цена, ссылка\n3. Призыв к действию\n4. Хештеги\n\n' + _BASE_SYSTEM_RULES,
    },
    'vk': {
        'promo_post': 'Ты — SMM-менеджер сообщества ВКонтакте магазина на Wildberries.\n\nСтруктура:\n1. Привлекательный заголовок\n2. Подробное описание товара (3-4 предложения)\n3. Преимущества\n4. Цена\n5. Ссылка\n6. Призыв\n7. Хештеги\n\n' + _BASE_SYSTEM_RULES,
        'review': 'Ты — блогер-обзорщик для сообщества ВКонтакте.\n\n' + _BASE_SYSTEM_RULES,
        'story_script': 'Ты — SMM-менеджер ВКонтакте. Создаёшь сценарии для клипов.\n\n' + _BASE_SYSTEM_RULES,
        'carousel': 'Ты — SMM-менеджер ВКонтакте. Составляешь подборки товаров.\n\n' + _BASE_SYSTEM_RULES,
    },
    'instagram': {
        'promo_post': 'Ты — Instagram-маркетолог магазина на Wildberries.\n\nСтруктура:\n1. Короткий цепляющий заголовок\n2. Описание с эмодзи\n3. Цена\n4. CTA: "ссылка в шапке профиля" или прямая ссылка\n5. Хештеги (до 30 штук)\n\n' + _BASE_SYSTEM_RULES,
        'review': 'Ты — Instagram-блогер. Пишешь обзоры.\n\n' + _BASE_SYSTEM_RULES,
        'story_script': 'Ты — Instagram-маркетолог. Сценарии для Stories.\n\n' + _BASE_SYSTEM_RULES,
        'carousel': 'Ты — Instagram-маркетолог. Подборки для каруселей.\n\n' + _BASE_SYSTEM_RULES,
    },
    'tiktok': {
        'promo_post': 'Ты — TikTok-маркетолог. Описание к видео, максимально коротко.\n\n' + _BASE_SYSTEM_RULES,
        'review': 'Ты — TikTok-блогер. Сценарий обзора с хуком в первые 3 секунды.\n\n' + _BASE_SYSTEM_RULES,
        'story_script': 'Ты — TikTok-маркетолог. Сценарий короткого видео.\n\n' + _BASE_SYSTEM_RULES,
        'carousel': 'Ты — TikTok-маркетолог. Подборка для слайд-шоу.\n\n' + _BASE_SYSTEM_RULES,
    },
    'youtube': {
        'promo_post': 'Ты — YouTube-маркетолог. Описание к видео, SEO-оптимизированное.\n\n' + _BASE_SYSTEM_RULES,
        'review': 'Ты — YouTube-блогер. Сценарий обзора товара.\n\n' + _BASE_SYSTEM_RULES,
        'story_script': 'Ты — YouTube-блогер. Сценарий для Shorts.\n\n' + _BASE_SYSTEM_RULES,
        'carousel': 'Ты — YouTube-блогер. Подборка товаров для видео.\n\n' + _BASE_SYSTEM_RULES,
    },
}

_BUILTIN_USER_PROMPTS = {
    'promo_post': (
        'Напиши продающий пост о товаре:\n\n'
        'Название: {product_name}\n'
        'Цена: {price} руб.{discount_info}\n'
        'Бренд: {brand}\n'
        'Категория: {category}\n'
        'Описание: {description}\n'
        'Рейтинг на WB: {rating}\n'
        'К посту будет прикреплено {photo_count} фото товара.\n\n'
        'Требования:\n'
        '1. Цепляющий заголовок ЗАГЛАВНЫМИ с эмодзи\n'
        '2. 2-3 ключевых преимущества\n'
        '3. Цена (если есть скидка — покажи выгоду)\n'
        '4. Призыв к действию\n'
        '5. 3-5 хештегов в конце отдельным блоком\n'
        '6. НЕ пиши "ИП", "ООО" и другие юридические формы\n'
        '7. НЕ вставляй ссылки — ссылка на товар будет добавлена автоматически'
    ),
    'review': (
        'Напиши обзор товара:\n\n'
        'Название: {product_name}\n'
        'Цена: {price} руб.\n'
        'Бренд: {brand}\n'
        'Категория: {category}\n'
        'Описание: {description}\n'
        'Характеристики: {characteristics}\n'
        'Рейтинг: {rating}\n\n'
        'Требования:\n'
        '1. Заголовок с оценкой (звёзды эмодзи)\n'
        '2. Плюсы и минусы\n'
        '3. Для кого подойдёт\n'
        '4. Хештеги в конце\n'
        '5. НЕ пиши "ИП", "ООО" и другие юридические формы\n'
        '6. НЕ вставляй ссылки — ссылка на товар будет добавлена автоматически'
    ),
    'story_script': (
        'Создай сценарий Stories/Reels для товара:\n\n'
        'Название: {product_name}\nЦена: {price} руб.\nБренд: {brand}\n'
        'Описание: {description}\n\n'
        'Требования:\n5-7 слайдов, для каждого текст на экране.\n'
        'Первый — интрига. Последний — CTA (призыв к действию)\n'
        'НЕ пиши "ИП", "ООО" и другие юридические формы\n'
        'НЕ вставляй ссылки — ссылка на товар будет добавлена автоматически'
    ),
    'carousel': (
        'Составь подборку товаров:\n\n'
        'Тема: {collection_theme}\n\nТовары:\n{products_list}\n\n'
        'Требования:\n1. Заголовок подборки\n'
        '2. Каждый товар: название, описание (1-2 предложения), цена\n'
        '3. Призыв к действию\n4. Хештеги\n'
        '5. НЕ пиши "ИП", "ООО" и другие юридические формы\n'
        '6. НЕ вставляй ссылки — ссылки на товары будут добавлены автоматически'
    ),
}

STATUS_LABELS = {
    'draft': 'Черновик',
    'approved': 'Одобрен',
    'scheduled': 'Запланирован',
    'publishing': 'Публикуется',
    'published': 'Опубликован',
    'failed': 'Ошибка',
    'archived': 'В архиве',
}


@dataclass
class GenerationResult:
    """Результат генерации контента"""
    success: bool
    title: Optional[str] = None
    body_text: Optional[str] = None
    hashtags: Optional[List[str]] = None
    media_urls: Optional[List[str]] = None
    wb_url: Optional[str] = None
    store_name: Optional[str] = None
    product_names: Optional[List[str]] = None
    quality_score: int = 0
    ai_provider: Optional[str] = None
    ai_model: Optional[str] = None
    tokens_used: int = 0
    generation_time_ms: int = 0
    error: Optional[str] = None


class ContentFactoryService:
    """Сервис для работы с контент-фабриками"""

    def __init__(self):
        pass

    # ================================================================
    # AI конфиг
    # ================================================================

    def _get_ai_client(self, factory: ContentFactory) -> Tuple[AIClient, str, str]:
        """Создаёт AI клиент на основе настроек фабрики.

        Использует единый AIConfig.for_seller() для получения конфигурации.

        Returns:
            Tuple[AIClient, provider_name, model_name]
        """
        provider_name = factory.ai_provider or 'cloudru'

        config = AIConfig.for_seller(
            seller_id=factory.seller_id,
            provider_override=provider_name,
            temperature=0.7,  # Более креативно для контента
            max_tokens=3000,
        )

        client = AIClient(config)
        return client, config.provider.value, config.model

    # ================================================================
    # Генерация контента
    # ================================================================

    def generate_content(
        self,
        factory: ContentFactory,
        product_ids: List[int],
        content_type: str,
        template_id: Optional[int] = None,
        custom_prompt: Optional[str] = None,
    ) -> GenerationResult:
        """Генерирует единицу контента через AI."""

        start_time = time.time()

        try:
            # Получаем AI клиент
            client, provider_name, model_name = self._get_ai_client(factory)

            # Получаем шаблон
            template = self._get_template(factory, content_type, template_id)
            if not template:
                return GenerationResult(
                    success=False,
                    error=f"Шаблон для {content_type} на платформе {factory.platform} не найден"
                )

            # Собираем данные о товарах
            products_data = self._collect_products_data(product_ids, factory.seller_id)
            if not products_data:
                return GenerationResult(
                    success=False,
                    error="Товары не найдены"
                )

            # Формируем промпт
            if content_type == 'carousel':
                user_prompt = self._build_carousel_prompt(template, products_data, factory)
            else:
                user_prompt = self._build_product_prompt(template, products_data[0], factory)

            if custom_prompt:
                user_prompt += f"\n\nДополнительные указания: {custom_prompt}"

            # Собираем контекст для system prompt
            store_name = self._get_store_name(factory)
            char_limit = PLATFORM_CHAR_LIMITS.get(factory.platform, 4096)
            first_product = products_data[0] if products_data else {}
            wb_url = first_product.get('wb_url', '')

            # Подставляем контекстные данные в system prompt
            system_prompt = template.system_prompt
            system_prompt = system_prompt.replace('{wb_url}', wb_url)
            system_prompt = system_prompt.replace('{store_name}', store_name)
            system_prompt = system_prompt.replace('{char_limit}', str(char_limit))

            if factory.style_guidelines:
                system_prompt += f"\n\nДополнительные требования к стилю:\n{factory.style_guidelines}"
            if factory.tone:
                tone_map = {
                    'formal': 'Используй формальный, деловой тон.',
                    'casual': 'Используй неформальный, дружелюбный тон.',
                    'creative': 'Используй творческий, яркий тон с метафорами.',
                    'expert': 'Используй экспертный, профессиональный тон.',
                }
                system_prompt += f"\n\n{tone_map.get(factory.tone, '')}"

            # ========== ШАГ 1: Генерация контента ==========
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            response = client.chat_completion(
                messages=messages,
                temperature=0.7,
                max_tokens=3000,
            )

            if not response:
                return GenerationResult(
                    success=False,
                    error=client.last_error or "Пустой ответ от AI",
                    ai_provider=provider_name,
                    ai_model=model_name,
                )

            # ========== ШАГ 2: AI-ревью (самокритика) ==========
            reviewed = self._ai_review_step(client, response, factory.platform, char_limit, wb_url, store_name)
            if reviewed:
                response = reviewed

            # ========== ШАГ 3: Программная очистка ==========
            title, body, hashtags = self._parse_ai_response(response, content_type)

            # ========== ШАГ 4: Гарантированная вставка ссылки ==========
            body = self._ensure_product_url(body, wb_url)

            # Собираем медиа и метаданные
            # Для обзоров берём больше фото
            max_photos_per_product = 10 if content_type in ('product_review', 'review') else 5
            all_photos = []
            product_names = []
            for pd in products_data:
                all_photos.extend(pd.get('photos', [])[:max_photos_per_product])
                if pd.get('name'):
                    product_names.append(pd['name'])
            media_urls = all_photos[:10]

            quality = self._score_content(body, content_type, factory.platform, wb_url, store_name)

            elapsed_ms = int((time.time() - start_time) * 1000)

            return GenerationResult(
                success=True,
                title=title,
                body_text=body,
                hashtags=hashtags,
                media_urls=media_urls,
                wb_url=wb_url,
                store_name=store_name,
                product_names=product_names,
                quality_score=quality,
                ai_provider=provider_name,
                ai_model=model_name,
                tokens_used=0,
                generation_time_ms=elapsed_ms,
            )

        except Exception as e:
            logger.error(f"Content generation error: {e}", exc_info=True)
            elapsed_ms = int((time.time() - start_time) * 1000)
            return GenerationResult(
                success=False,
                error=str(e),
                generation_time_ms=elapsed_ms,
            )

    def generate_and_save(
        self,
        factory: ContentFactory,
        product_ids: List[int],
        content_type: str,
        template_id: Optional[int] = None,
        custom_prompt: Optional[str] = None,
    ) -> Tuple[Optional[ContentItem], Optional[str]]:
        """Генерирует контент и сохраняет в БД.

        Returns:
            Tuple[ContentItem or None, error_message or None]
        """
        result = self.generate_content(
            factory=factory,
            product_ids=product_ids,
            content_type=content_type,
            template_id=template_id,
            custom_prompt=custom_prompt,
        )

        if not result.success:
            return None, result.error

        item = ContentItem(
            factory_id=factory.id,
            seller_id=factory.seller_id,
            template_id=template_id,
            platform=factory.platform,
            content_type=content_type,
            body_text=result.body_text or '',
            title=result.title,
            status='approved' if factory.auto_approve else 'draft',
            ai_provider=result.ai_provider,
            ai_model=result.ai_model,
            tokens_used=result.tokens_used,
            generation_time_ms=result.generation_time_ms,
            social_account_id=factory.default_social_account_id,
        )
        item.set_product_ids(product_ids)
        if result.hashtags:
            item.set_hashtags(result.hashtags)

        # Сохраняем фото товаров
        if result.media_urls:
            item.media_urls_json = json.dumps(result.media_urls)

        # Сохраняем метаданные (ссылка WB, магазин, качество)
        platform_specific = {
            'wb_url': result.wb_url or '',
            'store_name': result.store_name or '',
            'product_names': result.product_names or [],
            'quality_score': result.quality_score,
            'char_count': len(result.body_text or ''),
        }
        item.platform_specific_json = json.dumps(platform_specific, ensure_ascii=False)

        db.session.add(item)
        db.session.commit()

        return item, None

    def generate_bulk(
        self,
        factory: ContentFactory,
        content_type: str,
        count: int = 5,
        template_id: Optional[int] = None,
        custom_prompt: Optional[str] = None,
    ) -> Tuple[List[ContentItem], List[str]]:
        """Массовая генерация контента.

        Автоматически подбирает товары и генерирует указанное количество постов.

        Returns:
            Tuple[list of created items, list of errors]
        """
        items = []
        errors = []

        # Подбираем товары
        products = self.select_products(factory, limit=count)
        if not products:
            return [], ["Не найдены товары для генерации"]

        for product_data in products[:count]:
            product_id = product_data.get('id')
            if not product_id:
                continue

            item, error = self.generate_and_save(
                factory=factory,
                product_ids=[product_id],
                content_type=content_type,
                template_id=template_id,
                custom_prompt=custom_prompt,
            )

            if item:
                items.append(item)
            if error:
                errors.append(f"Товар {product_id}: {error}")

        return items, errors

    # ================================================================
    # Подбор товаров
    # ================================================================

    # Паттерн для извлечения размера из названия товара
    _SIZE_PATTERN = re.compile(
        r'\s+(?:'
        r'(?:р(?:\.|азмер)?\s*)?'  # опциональный префикс "р.", "р ", "размер "
        r'(?:'
        r'(?:\d{1,3}(?:[/-]\d{1,3})?)'  # числовые: 42, 44-46, 48/50
        r'|(?:XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|5XL)'  # буквенные
        r'|(?:one\s*size|единый)'  # one size
        r')'
        r')\s*$',
        re.IGNORECASE,
    )

    def _base_product_name(self, title: str) -> str:
        """Возвращает название товара без суффикса размера для группировки."""
        if not title:
            return ''
        return self._SIZE_PATTERN.sub('', title).strip()

    def _extract_size_label(self, product: Product) -> Optional[str]:
        """Извлекает обозначение размера из названия товара."""
        title = product.title or ''
        m = self._SIZE_PATTERN.search(title)
        if m:
            return m.group(0).strip()
        # Фоллбэк: берём из sizes_json
        if product.sizes_json:
            try:
                sizes = json.loads(product.sizes_json)
                if isinstance(sizes, list) and sizes:
                    for s in sizes:
                        tech = s.get('techSize') or s.get('origName') or ''
                        if tech:
                            return tech
            except Exception:
                pass
        return None

    def _get_available_sizes(self, product: Product) -> List[str]:
        """Возвращает список доступных размеров из sizes_json."""
        if not product.sizes_json:
            return []
        try:
            sizes = json.loads(product.sizes_json)
            if isinstance(sizes, list):
                result = []
                for s in sizes:
                    label = s.get('origName') or s.get('techSize') or ''
                    if label and label not in result:
                        result.append(label)
                return result
        except Exception:
            pass
        return []

    def _deduplicate_products(self, products: List[Dict], limit: int) -> List[Dict]:
        """Дедуплицирует товары по базовому названию (без размера).

        Группирует вариации одного товара (разные размеры) и обогащает
        выбранного представителя информацией о доступных размерах.
        """
        groups = {}  # base_name -> list of product dicts
        for p in products:
            base = self._base_product_name(p.get('name', ''))
            key = (p.get('brand', '').lower(), base.lower())
            if key not in groups:
                groups[key] = []
            groups[key].append(p)

        result = []
        for key, group in groups.items():
            if len(result) >= limit:
                break

            # Берём первый товар как представителя
            representative = group[0]

            if len(group) > 1:
                # Собираем все размеры из группы
                all_sizes = []
                seen_sizes = set()
                for p in group:
                    name = p.get('name', '')
                    m = self._SIZE_PATTERN.search(name)
                    if m:
                        sz = m.group(0).strip()
                        if sz.lower() not in seen_sizes:
                            seen_sizes.add(sz.lower())
                            all_sizes.append(sz)
                    # Также берём размеры из sizes_json через _available_sizes
                    for sz in p.get('available_sizes', []):
                        if sz.lower() not in seen_sizes:
                            seen_sizes.add(sz.lower())
                            all_sizes.append(sz)

                representative['available_sizes'] = all_sizes
                # Базовое название (без размера) для промпта
                representative['name'] = self._base_product_name(representative.get('name', ''))
                # Примечание что есть несколько размеров
                representative['sizes_info'] = f"Доступные размеры: {', '.join(all_sizes)}" if all_sizes else ''

            result.append(representative)

        return result[:limit]

    def select_products(
        self,
        factory: ContentFactory,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Подбирает товары для контента на основе режима фабрики."""

        mode = factory.product_selection_mode or 'manual'

        # Запрашиваем больше товаров чтобы после дедупликации хватило
        fetch_limit = limit * 3

        if mode == 'bestsellers':
            raw = self._select_bestsellers(factory.seller_id, fetch_limit)
        elif mode == 'new_arrivals':
            raw = self._select_new_arrivals(factory.seller_id, fetch_limit)
        elif mode == 'rules':
            raw = self._select_by_rules(factory, fetch_limit)
        else:
            # manual — возвращаем все товары для ручного выбора (без дедупликации)
            return self._select_all_products(factory.seller_id, limit)

        return self._deduplicate_products(raw, limit)

    def _select_bestsellers(self, seller_id: int, limit: int) -> List[Dict]:
        """Выбирает товары с лучшими продажами (по orders_count из аналитики)."""
        # Пробуем подобрать по реальным продажам через ProductAnalytics
        products = (
            Product.query
            .outerjoin(ProductAnalytics, db.and_(
                ProductAnalytics.nm_id == Product.nm_id,
                ProductAnalytics.seller_id == Product.seller_id,
            ))
            .filter(Product.seller_id == seller_id)
            .order_by(db.func.coalesce(ProductAnalytics.orders_count, 0).desc())
            .limit(limit)
            .all()
        )
        if not products:
            # Fallback на все товары
            products = Product.query.filter_by(seller_id=seller_id).limit(limit).all()
        return [self._product_to_dict(p) for p in products]

    def _select_new_arrivals(self, seller_id: int, limit: int) -> List[Dict]:
        """Выбирает недавно добавленные товары."""
        products = Product.query.filter_by(seller_id=seller_id).order_by(
            Product.created_at.desc()
        ).limit(limit).all()
        return [self._product_to_dict(p) for p in products]

    def _select_by_rules(self, factory: ContentFactory, limit: int) -> List[Dict]:
        """Выбирает товары по правилам фабрики."""
        rules = factory.get_selection_rules()
        query = Product.query.filter_by(seller_id=factory.seller_id)

        if rules.get('category'):
            query = query.filter(Product.object_name.ilike(f"%{rules['category']}%"))
        if rules.get('brand'):
            query = query.filter(Product.brand.ilike(f"%{rules['brand']}%"))
        if rules.get('min_price'):
            query = query.filter(Product.price >= rules['min_price'])
        if rules.get('max_price'):
            query = query.filter(Product.price <= rules['max_price'])

        products = query.order_by(Product.updated_at.desc()).limit(limit).all()
        return [self._product_to_dict(p) for p in products]

    def _select_all_products(self, seller_id: int, limit: int) -> List[Dict]:
        """Все товары продавца."""
        products = Product.query.filter_by(seller_id=seller_id).order_by(
            Product.updated_at.desc()
        ).limit(limit).all()
        return [self._product_to_dict(p) for p in products]

    def _collect_products_data(self, product_ids: List[int], seller_id: int) -> List[Dict]:
        """Загружает товары по ID и возвращает данные для промптов."""
        if not product_ids:
            return []

        products = Product.query.filter(
            Product.id.in_(product_ids),
            Product.seller_id == seller_id,
        ).all()

        return [self._product_to_dict(p, validate_photos=True) for p in products]

    def _validate_photo_urls(self, urls: list) -> list:
        """Проверяет доступность фото по URL (HEAD-запрос). Останавливается на первой ошибке."""
        validated = []
        for url in urls:
            try:
                resp = _requests.head(url, timeout=4, allow_redirects=True)
                if resp.status_code == 200:
                    validated.append(url)
                else:
                    break  # WB фото последовательные: если одно не найдено, следующие тоже
            except Exception:
                break
        return validated

    def _product_to_dict(self, product: Product, validate_photos: bool = False) -> Dict:
        """Конвертирует Product в dict для промптов (с фото, ссылкой, рейтингом)."""
        photos = []
        if product.photos_json and product.nm_id:
            try:
                from seller_platform import wb_photo_url
                raw_photos = json.loads(product.photos_json)
                if isinstance(raw_photos, list):
                    for p in raw_photos:
                        if isinstance(p, str) and p.startswith('http'):
                            # Формат wb_product_importer: полные URL
                            photos.append(p)
                        elif isinstance(p, int):
                            # Формат seller_platform Cards sync: индексы фото [1, 2, 3, ...]
                            photos.append(wb_photo_url(product.nm_id, p))
            except Exception:
                pass

        # Если мало фото или нет вообще — дополняем из WB CDN
        if product.nm_id and (not photos or (validate_photos and len(photos) < 5)):
            try:
                from seller_platform import wb_photo_url
                existing_urls = set(photos)
                if validate_photos:
                    candidate_urls = [wb_photo_url(product.nm_id, i) for i in range(1, 11)
                                      if wb_photo_url(product.nm_id, i) not in existing_urls]
                    extra = self._validate_photo_urls(candidate_urls)
                    for url in extra:
                        if url not in existing_urls:
                            photos.append(url)
                            existing_urls.add(url)
                elif not photos:
                    # Без валидации берём только 2 фото (безопасный минимум)
                    photos = [wb_photo_url(product.nm_id, i) for i in range(1, 3)]
            except Exception:
                pass

        characteristics = ''
        if product.characteristics_json:
            try:
                chars = json.loads(product.characteristics_json)
                if isinstance(chars, list):
                    characteristics = '; '.join(
                        f"{c.get('name', '')}: {', '.join(str(v) for v in c.get('value', []))}"
                        for c in chars if c.get('name')
                    )
                elif isinstance(chars, dict):
                    characteristics = '; '.join(f"{k}: {v}" for k, v in chars.items())
            except Exception:
                pass

        nm_id = product.nm_id
        wb_url = f'https://www.wildberries.ru/catalog/{nm_id}/detail.aspx' if nm_id else ''

        price = float(product.price or 0)
        discount_price = float(product.discount_price or 0)
        discount_info = ''
        if discount_price and discount_price < price and price > 0:
            pct = int((1 - discount_price / price) * 100)
            discount_info = f' (скидка {pct}%, было {int(price)} руб.)'

        # Доступные размеры
        available_sizes = self._get_available_sizes(product)
        sizes_info = ''
        if available_sizes:
            sizes_info = f"Доступные размеры: {', '.join(available_sizes)}"

        return {
            'id': product.id,
            'nm_id': nm_id,
            'name': product.title or '',
            'brand': product.brand or '',
            'category': product.object_name or '',
            'price': price,
            'discount_price': discount_price,
            'description': getattr(product, 'description', '') or '',
            'vendor_code': product.vendor_code or '',
            'photos': photos,
            'wb_url': wb_url,
            'rating': product.nm_rating or 0,
            'characteristics': characteristics,
            'discount_info': discount_info,
            'available_sizes': available_sizes,
            'sizes_info': sizes_info,
        }

    # ================================================================
    # Шаблоны
    # ================================================================

    def _get_template(
        self,
        factory: ContentFactory,
        content_type: str,
        template_id: Optional[int] = None,
    ) -> Optional[ContentTemplate]:
        """Находит подходящий шаблон. Если в БД нет — использует встроенный."""
        if template_id:
            return ContentTemplate.query.get(template_id)

        # Сначала ищем шаблон продавца
        template = ContentTemplate.query.filter_by(
            seller_id=factory.seller_id,
            platform=factory.platform,
            content_type=content_type,
            is_active=True,
        ).first()

        if not template:
            # Используем системный из БД
            template = ContentTemplate.query.filter_by(
                is_system=True,
                platform=factory.platform,
                content_type=content_type,
                is_active=True,
            ).first()

        if not template:
            # Fallback: встроенный шаблон (не сохраняется в БД)
            template = self._get_builtin_template(factory.platform, content_type)

        return template

    def _get_builtin_template(self, platform: str, content_type: str) -> Optional[ContentTemplate]:
        """Возвращает встроенный шаблон как объект ContentTemplate (без сохранения в БД)."""
        platform_prompts = _BUILTIN_SYSTEM_PROMPTS.get(platform, {})
        system_prompt = platform_prompts.get(content_type)
        user_prompt = _BUILTIN_USER_PROMPTS.get(content_type)

        if not system_prompt or not user_prompt:
            logger.warning(f"Встроенный шаблон не найден: platform={platform}, type={content_type}")
            return None

        template = ContentTemplate()
        template.id = None
        template.seller_id = None
        template.platform = platform
        template.content_type = content_type
        template.name = f"Встроенный: {CONTENT_TYPE_LABELS.get(content_type, content_type)} ({PLATFORM_LABELS.get(platform, platform)})"
        template.system_prompt = system_prompt
        template.user_prompt_template = user_prompt
        template.is_system = True
        template.is_active = True
        return template

    def get_templates_for_factory(self, factory: ContentFactory) -> List[ContentTemplate]:
        """Все доступные шаблоны для фабрики."""
        content_types = factory.get_content_types()

        query = ContentTemplate.query.filter(
            ContentTemplate.platform == factory.platform,
            ContentTemplate.is_active == True,
            db.or_(
                ContentTemplate.seller_id == factory.seller_id,
                ContentTemplate.is_system == True,
            ),
        )
        if content_types:
            query = query.filter(ContentTemplate.content_type.in_(content_types))

        templates = query.all()

        return templates

    # ================================================================
    # Формирование промптов
    # ================================================================

    def _get_store_name(self, factory: ContentFactory) -> str:
        """Получает коммерческое название магазина (без юр. формы ИП/ООО)."""
        import re
        try:
            seller = Seller.query.get(factory.seller_id)
            name = seller.company_name if seller else 'Наш магазин'
            if not name:
                return 'Наш магазин'
            # Убираем юридическую форму: ИП, ООО, ОАО, ЗАО и т.д.
            name = re.sub(r'^(ИП|ООО|ОАО|ЗАО|ПАО)\s+', '', name, flags=re.IGNORECASE).strip()
            return name or 'Наш магазин'
        except Exception:
            return 'Наш магазин'

    def _build_product_prompt(
        self,
        template: ContentTemplate,
        product_data: Dict,
        factory: ContentFactory,
    ) -> str:
        """Подставляет данные товара в шаблон промпта (с фото, ссылкой, магазином)."""
        prompt = template.user_prompt_template
        store_name = self._get_store_name(factory)

        price = product_data.get('discount_price') or product_data.get('price', 0)
        rating = product_data.get('rating', 0)
        rating_str = f'{rating}/5' if rating else 'нет данных'

        # Информация о размерах
        sizes_info = product_data.get('sizes_info', '')

        replacements = {
            '{product_name}': product_data.get('name', ''),
            '{price}': str(int(float(price))) if price else '0',
            '{brand}': product_data.get('brand', '') or 'Без бренда',
            '{category}': product_data.get('category', ''),
            '{description}': (product_data.get('description', '') or '')[:500],
            '{vendor_code}': product_data.get('vendor_code', ''),
            '{characteristics}': (product_data.get('characteristics', '') or product_data.get('description', ''))[:500],
            '{wb_url}': product_data.get('wb_url', ''),
            '{store_name}': store_name,
            '{rating}': rating_str,
            '{photo_count}': str(len(product_data.get('photos', []))),
            '{discount_info}': product_data.get('discount_info', ''),
            '{sizes_info}': sizes_info,
        }

        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, str(value))

        # Добавляем инфу о размерах если есть (даже если нет плейсхолдера в шаблоне)
        if sizes_info and '{sizes_info}' not in template.user_prompt_template:
            prompt += f"\n\n{sizes_info}"

        if hasattr(template, 'hashtag_strategy') and template.hashtag_strategy:
            prompt += f"\n\nСтратегия хештегов: {template.hashtag_strategy}"

        return prompt

    def _build_carousel_prompt(
        self,
        template: ContentTemplate,
        products_data: List[Dict],
        factory: ContentFactory,
    ) -> str:
        """Формирует промпт для подборки товаров."""
        store_name = self._get_store_name(factory)
        products_list = ""
        for i, p in enumerate(products_data, 1):
            price = p.get('discount_price') or p.get('price', 0)
            wb_url = p.get('wb_url', '')
            products_list += f"{i}. {p.get('name', '')} — {int(float(price))} руб. ({p.get('brand', '')})\n   Ссылка: {wb_url}\n"

        prompt = template.user_prompt_template
        prompt = prompt.replace('{products_list}', products_list)
        prompt = prompt.replace('{collection_theme}', factory.description or 'Подборка товаров')
        prompt = prompt.replace('{store_name}', store_name)

        if hasattr(template, 'hashtag_strategy') and template.hashtag_strategy:
            prompt += f"\n\nСтратегия хештегов: {template.hashtag_strategy}"

        return prompt

    # ================================================================
    # Парсинг ответа AI
    # ================================================================

    def _ai_review_step(
        self,
        client: AIClient,
        draft: str,
        platform: str,
        char_limit: int,
        wb_url: str,
        store_name: str,
    ) -> Optional[str]:
        """Шаг 2: AI проверяет и улучшает черновик. Возвращает улучшенный текст или None."""
        try:
            review_prompt = (
                f'Проверь и улучши этот пост для {PLATFORM_LABELS.get(platform, platform)}.\n\n'
                f'--- НАЧАЛО ЧЕРНОВИКА ---\n{draft}\n--- КОНЕЦ ЧЕРНОВИКА ---\n\n'
                f'ПРОВЕРЬ:\n'
                f'1. Нет ли markdown-разметки (**, *, ##, ```)? Если есть — убери, замени на эмодзи/заглавные\n'
                f'2. УДАЛИ все ссылки на wildberries.ru или любые другие URL — ссылка будет добавлена автоматически\n'
                f'3. Нет ли юридических форм (ИП, ООО, ОАО, индивидуальный предприниматель)? Если есть — УДАЛИ их полностью\n'
                f'4. Есть ли призыв к действию?\n'
                f'5. Есть ли цена?\n'
                f'6. Хештеги отделены пустой строкой в конце? Нет ли в хештегах юр. форм (ИП, ООО)?\n'
                f'7. Текст укладывается в {char_limit} символов?\n\n'
                f'Верни ТОЛЬКО исправленный текст поста. БЕЗ слов "ЧЕРНОВИК:", "ИТОГОВЫЙ ТЕКСТ:", без комментариев и пояснений. Только сам пост.'
            )

            response = client.chat_completion(
                messages=[{"role": "user", "content": review_prompt}],
                temperature=0.3,
                max_tokens=3000,
            )
            if response and len(response.strip()) > 50:
                return response.strip()
            return None
        except Exception as e:
            logger.warning(f"AI review step failed (using original): {e}")
            return None

    def _strip_markdown(self, text: str) -> str:
        """Убирает markdown-артефакты из текста."""
        # Убираем **bold** и *italic*
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        # Убираем ## заголовки
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        # Убираем ```code```
        text = re.sub(r'```[\s\S]*?```', '', text)
        # Убираем `inline code`
        text = re.sub(r'`(.+?)`', r'\1', text)
        # Убираем маркеры списка "- " и "• " в начале строк
        text = re.sub(r'^[\-\•]\s+', '', text, flags=re.MULTILINE)
        # Убираем лишние пустые строки (больше 2 подряд)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _parse_ai_response(
        self,
        response: str,
        content_type: str,
    ) -> Tuple[Optional[str], str, List[str]]:
        """Парсит ответ AI: очищает markdown, извлекает заголовок, текст и хештеги (дедупликация)."""
        # Очищаем markdown
        text = self._strip_markdown(response)

        # Убираем служебные метки AI (ЧЕРНОВИК:, ИТОГОВЫЙ ТЕКСТ: и т.п.)
        text = re.sub(r'^(?:ЧЕРНОВИК|DRAFT|ИТОГОВЫЙ ТЕКСТ|ИСПРАВЛЕННЫЙ ТЕКСТ|ИТОГОВЫЙ ВАРИАНТ)\s*:\s*\n?', '', text, flags=re.IGNORECASE)

        # Извлекаем и дедуплицируем хештеги
        all_hashtags = re.findall(r'#[А-Яа-яA-Za-z0-9_]+', text)
        seen = set()
        hashtags = []
        for tag in all_hashtags:
            tag_lower = tag.lower()
            if tag_lower not in seen:
                seen.add(tag_lower)
                hashtags.append(tag)

        # Удаляем строки с хештегами из тела (они пойдут в отдельное поле)
        body_lines = []
        for line in text.split('\n'):
            stripped = line.strip()
            # Строка целиком из хештегов — убираем
            if stripped and all(w.startswith('#') for w in stripped.split()):
                continue
            body_lines.append(line)
        body_text = '\n'.join(body_lines).strip()
        # Убираем trailing пустые строки
        body_text = re.sub(r'\n{2,}$', '', body_text)

        # Извлекаем заголовок
        title = None
        lines = body_text.split('\n')
        for line in lines:
            clean = line.strip()
            if not clean:
                continue
            for prefix in ('Заголовок:', 'Title:', '# '):
                if clean.lower().startswith(prefix.lower()):
                    title = clean[len(prefix):].strip()
                    break
            if title:
                break
            title = clean
            break

        return title, body_text, hashtags

    def _ensure_product_url(self, body: str, wb_url: str) -> str:
        """Гарантирует наличие правильной ссылки на товар в тексте.

        1. Удаляет все ссылки wildberries.ru которые AI мог нагаллюцинировать
        2. Вставляет правильную ссылку перед призывом к действию или в конец текста
        """
        if not wb_url:
            return body

        # Убираем все WB-ссылки из текста (AI мог вставить неправильные)
        # Паттерн: https://www.wildberries.ru/catalog/ЛЮБЫЕ_ЦИФРЫ/detail.aspx
        body = re.sub(
            r'https?://(?:www\.)?wildberries\.ru/catalog/\d+/detail\.aspx\S*',
            '',
            body,
        )

        # Убираем оставшиеся пустые строки "Ссылка:" / "Ссылка на товар:" без URL
        body = re.sub(
            r'^[🔗📎👉💰🛒]*\s*(?:Ссылка(?:\s+на\s+товар)?|Заказать|Купить)\s*:\s*$',
            '',
            body,
            flags=re.MULTILINE | re.IGNORECASE,
        )

        # Убираем лишние пустые строки
        body = re.sub(r'\n{3,}', '\n\n', body).strip()

        # Вставляем правильную ссылку перед последним абзацем (призыв к действию)
        # или просто в конец
        body = body.rstrip()
        body += f'\n\n👉 {wb_url}'

        return body

    def _score_content(
        self,
        body: str,
        content_type: str,
        platform: str,
        wb_url: str,
        store_name: str,
    ) -> int:
        """Оценка качества контента 0-100."""
        score = 0
        text = body or ''

        # Есть эмодзи? +10
        if re.search(r'[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF]', text):
            score += 10

        # Есть призыв к действию? +15
        cta_words = ['заказ', 'купи', 'переходи', 'жми', 'ссылк', 'скорее', 'успей', 'закажи', 'бери']
        if any(w in text.lower() for w in cta_words):
            score += 15

        # Есть ссылка на WB? +15
        if wb_url and wb_url in text:
            score += 15
        elif 'wildberries.ru' in text.lower():
            score += 10

        # В лимите символов? +15
        char_limit = PLATFORM_CHAR_LIMITS.get(platform, 4096)
        if len(text) <= char_limit:
            score += 15

        # Упомянута цена? +10
        if re.search(r'\d+\s*(руб|₽|р\.)', text):
            score += 10

        # Упомянут магазин? +10
        if store_name and store_name.lower() in text.lower():
            score += 10

        # Нет markdown-артефактов? +15
        if '**' not in text and '##' not in text and '```' not in text:
            score += 15

        # Есть хештеги? +10
        if '#' in text or re.search(r'#\w+', text):
            score += 10

        return min(score, 100)

    # ================================================================
    # Управление контентом
    # ================================================================

    def get_factory_stats(self, factory: ContentFactory) -> Dict:
        """Статистика фабрики."""
        total = ContentItem.query.filter_by(factory_id=factory.id).count()
        by_status = {}
        for status in CONTENT_STATUSES:
            count = ContentItem.query.filter_by(
                factory_id=factory.id, status=status
            ).count()
            if count > 0:
                by_status[status] = count

        return {
            'total': total,
            'by_status': by_status,
            'drafts': by_status.get('draft', 0),
            'published': by_status.get('published', 0),
            'scheduled': by_status.get('scheduled', 0),
        }

    def get_items_for_calendar(
        self,
        factory_id: int,
        date_from: datetime,
        date_to: datetime,
    ) -> List[Dict]:
        """Получает контент для отображения на календаре."""
        items = ContentItem.query.filter(
            ContentItem.factory_id == factory_id,
            ContentItem.status.in_(['scheduled', 'published', 'approved']),
            db.or_(
                db.and_(
                    ContentItem.scheduled_at >= date_from,
                    ContentItem.scheduled_at <= date_to,
                ),
                db.and_(
                    ContentItem.published_at >= date_from,
                    ContentItem.published_at <= date_to,
                ),
            ),
        ).all()

        result = []
        for item in items:
            event_date = item.scheduled_at or item.published_at or item.created_at
            result.append({
                'id': item.id,
                'title': item.title or f"{CONTENT_TYPE_LABELS.get(item.content_type, item.content_type)}",
                'date': event_date.strftime('%Y-%m-%d') if event_date else None,
                'time': event_date.strftime('%H:%M') if event_date else None,
                'status': item.status,
                'content_type': item.content_type,
                'platform': item.platform,
            })

        return result

    def schedule_item(
        self,
        item: ContentItem,
        scheduled_at: datetime,
        social_account_id: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Планирует публикацию контента."""
        if item.status not in ('draft', 'approved'):
            return False, f"Нельзя запланировать контент со статусом '{STATUS_LABELS.get(item.status, item.status)}'"

        if scheduled_at <= datetime.utcnow():
            return False, "Дата публикации должна быть в будущем"

        item.status = 'scheduled'
        item.scheduled_at = scheduled_at
        if social_account_id:
            item.social_account_id = social_account_id

        db.session.commit()
        return True, None

    def approve_item(self, item: ContentItem) -> Tuple[bool, Optional[str]]:
        """Одобряет контент."""
        if item.status != 'draft':
            return False, f"Можно одобрить только черновик"
        item.status = 'approved'
        db.session.commit()
        return True, None

    def archive_item(self, item: ContentItem) -> Tuple[bool, Optional[str]]:
        """Архивирует контент."""
        if item.status == 'publishing':
            return False, "Нельзя архивировать публикуемый контент"
        item.status = 'archived'
        db.session.commit()
        return True, None
