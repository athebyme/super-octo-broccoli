# -*- coding: utf-8 -*-
"""
BasePublisher — абстрактный базовый класс для публишеров контента
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any

from models import ContentItem, SocialAccount

logger = logging.getLogger(__name__)


@dataclass
class PublishResult:
    """Результат публикации"""
    success: bool
    external_post_id: Optional[str] = None
    external_post_url: Optional[str] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class BasePublisher(ABC):
    """Базовый класс для всех публишеров контента."""

    platform: str = ''

    @abstractmethod
    def publish(self, item: ContentItem, account: SocialAccount) -> PublishResult:
        """
        Публикует контент в социальную сеть.

        Args:
            item: ContentItem для публикации
            account: SocialAccount с учётными данными

        Returns:
            PublishResult с результатом операции
        """
        pass

    @abstractmethod
    def validate_account(self, account: SocialAccount) -> tuple[bool, Optional[str]]:
        """
        Проверяет, что аккаунт правильно настроен.

        Returns:
            Tuple[is_valid, error_message]
        """
        pass

    def format_text(self, item: ContentItem) -> str:
        """Форматирует текст контента для платформы."""
        text = item.body_text or ''
        hashtags = item.get_hashtags()
        if hashtags:
            text += '\n\n' + ' '.join(hashtags)
        return text
