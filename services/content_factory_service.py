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

from models import (
    db, Product, SupplierProduct, ImportedProduct,
    ProductAnalytics, ContentFactory, ContentItem,
    ContentTemplate, ContentPlan, SocialAccount,
    CONTENT_PLATFORMS, CONTENT_TYPES, CONTENT_STATUSES,
)
from services.ai_service import AIConfig, AIClient, AIProvider

logger = logging.getLogger(__name__)


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

        Returns:
            Tuple[AIClient, provider_name, model_name]
        """
        provider_name = factory.ai_provider or 'openai'

        # Настройки AI берём из AutoImportSettings продавца (там хранятся ключи)
        from models import AutoImportSettings
        ai_settings = AutoImportSettings.query.filter_by(
            seller_id=factory.seller_id
        ).first()

        if not ai_settings:
            raise ValueError("AI не настроен. Настройте AI в разделе Авто-импорт.")

        # Определяем провайдер и параметры
        if provider_name in ('cloudru', 'gigachat'):
            provider = AIProvider.CLOUDRU
            api_key = ai_settings.ai_client_secret or ''
            api_base_url = 'https://foundation-models.api.cloud.ru/v1'
            model = ai_settings.ai_model if hasattr(ai_settings, 'ai_model') else 'openai/gpt-oss-120b'
        elif provider_name == 'openai':
            provider = AIProvider.OPENAI
            api_key = ai_settings.openai_api_key or ''
            api_base_url = 'https://api.openai.com/v1'
            model = 'gpt-4o'
        else:
            provider = AIProvider.CUSTOM
            api_key = ai_settings.openai_api_key or ai_settings.ai_client_secret or ''
            api_base_url = getattr(ai_settings, 'ai_api_base_url', '') or 'https://api.openai.com/v1'
            model = getattr(ai_settings, 'ai_model', '') or 'gpt-4o'

        config = AIConfig(
            provider=provider,
            api_key=api_key,
            api_base_url=api_base_url,
            model=model,
            temperature=0.7,  # Более креативно для контента
            max_tokens=3000,
        )

        client = AIClient(config)
        return client, provider_name, model

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

            # Добавляем стиль-гайдлайны фабрики
            system_prompt = template.system_prompt
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

            # Запрос к AI
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

            # Парсим ответ
            title, body, hashtags = self._parse_ai_response(response, content_type)

            elapsed_ms = int((time.time() - start_time) * 1000)

            return GenerationResult(
                success=True,
                title=title,
                body_text=body,
                hashtags=hashtags,
                ai_provider=provider_name,
                ai_model=model_name,
                tokens_used=0,  # TODO: extract from response metadata
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

        db.session.add(item)
        db.session.commit()

        return item, None

    def generate_bulk(
        self,
        factory: ContentFactory,
        content_type: str,
        count: int = 5,
        template_id: Optional[int] = None,
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
            )

            if item:
                items.append(item)
            if error:
                errors.append(f"Товар {product_id}: {error}")

        return items, errors

    # ================================================================
    # Подбор товаров
    # ================================================================

    def select_products(
        self,
        factory: ContentFactory,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Подбирает товары для контента на основе режима фабрики."""

        mode = factory.product_selection_mode or 'manual'

        if mode == 'bestsellers':
            return self._select_bestsellers(factory.seller_id, limit)
        elif mode == 'new_arrivals':
            return self._select_new_arrivals(factory.seller_id, limit)
        elif mode == 'rules':
            return self._select_by_rules(factory, limit)
        else:
            # manual — возвращаем все товары для ручного выбора
            return self._select_all_products(factory.seller_id, limit)

    def _select_bestsellers(self, seller_id: int, limit: int) -> List[Dict]:
        """Выбирает товары с лучшими продажами."""
        products = Product.query.filter_by(seller_id=seller_id).order_by(
            Product.quantity.desc()
        ).limit(limit).all()
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

    def _product_to_dict(self, product: Product) -> Dict:
        """Конвертирует Product в dict для промптов."""
        photos = []
        if product.photos_json:
            try:
                photos = json.loads(product.photos_json)
            except Exception:
                pass

        return {
            'id': product.id,
            'nm_id': product.nm_id,
            'name': product.title or '',
            'brand': product.brand or '',
            'category': product.object_name or '',
            'price': product.price or 0,
            'discount_price': product.discount_price or 0,
            'description': getattr(product, 'description', '') or '',
            'vendor_code': product.vendor_code or '',
            'photos': photos,
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
        """Находит подходящий шаблон."""
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
            # Используем системный
            template = ContentTemplate.query.filter_by(
                is_system=True,
                platform=factory.platform,
                content_type=content_type,
                is_active=True,
            ).first()

        return template

    def get_templates_for_factory(self, factory: ContentFactory) -> List[ContentTemplate]:
        """Все доступные шаблоны для фабрики."""
        content_types = factory.get_content_types()

        templates = ContentTemplate.query.filter(
            ContentTemplate.platform == factory.platform,
            ContentTemplate.content_type.in_(content_types) if content_types else True,
            ContentTemplate.is_active == True,
            db.or_(
                ContentTemplate.seller_id == factory.seller_id,
                ContentTemplate.is_system == True,
            ),
        ).all()

        return templates

    # ================================================================
    # Формирование промптов
    # ================================================================

    def _build_product_prompt(
        self,
        template: ContentTemplate,
        product_data: Dict,
        factory: ContentFactory,
    ) -> str:
        """Подставляет данные товара в шаблон промпта."""
        prompt = template.user_prompt_template

        replacements = {
            '{product_name}': product_data.get('name', ''),
            '{price}': str(product_data.get('discount_price') or product_data.get('price', '')),
            '{brand}': product_data.get('brand', ''),
            '{category}': product_data.get('category', ''),
            '{description}': product_data.get('description', ''),
            '{vendor_code}': product_data.get('vendor_code', ''),
            '{characteristics}': product_data.get('description', ''),
        }

        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, str(value))

        if template.hashtag_strategy:
            prompt += f"\n\nСтратегия хештегов: {template.hashtag_strategy}"

        return prompt

    def _build_carousel_prompt(
        self,
        template: ContentTemplate,
        products_data: List[Dict],
        factory: ContentFactory,
    ) -> str:
        """Формирует промпт для подборки товаров."""
        products_list = ""
        for i, p in enumerate(products_data, 1):
            price = p.get('discount_price') or p.get('price', 0)
            products_list += f"{i}. {p.get('name', '')} — {price} руб. ({p.get('brand', '')})\n"

        prompt = template.user_prompt_template
        prompt = prompt.replace('{products_list}', products_list)
        prompt = prompt.replace('{collection_theme}', factory.description or 'Подборка товаров')

        if template.hashtag_strategy:
            prompt += f"\n\nСтратегия хештегов: {template.hashtag_strategy}"

        return prompt

    # ================================================================
    # Парсинг ответа AI
    # ================================================================

    def _parse_ai_response(
        self,
        response: str,
        content_type: str,
    ) -> Tuple[Optional[str], str, List[str]]:
        """Парсит ответ AI, извлекает заголовок, текст и хештеги.

        Returns:
            Tuple[title, body_text, hashtags]
        """
        import re

        # Извлекаем хештеги
        hashtag_pattern = r'#\w+'
        hashtags = re.findall(hashtag_pattern, response)

        # Извлекаем заголовок (первая строка, или строка после "Заголовок:")
        lines = response.strip().split('\n')
        title = None

        for line in lines:
            clean = line.strip()
            if not clean:
                continue
            # Ищем явный заголовок
            for prefix in ('Заголовок:', 'Title:', '**Заголовок', '# '):
                if clean.lower().startswith(prefix.lower()):
                    title = clean[len(prefix):].strip().strip('*').strip()
                    break
            if title:
                break
            # Берём первую непустую строку как заголовок
            title = clean.strip('*').strip('#').strip()
            break

        # Основной текст — весь ответ
        body_text = response.strip()

        return title, body_text, hashtags

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
