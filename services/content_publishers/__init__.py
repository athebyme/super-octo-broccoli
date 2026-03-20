# -*- coding: utf-8 -*-
"""
Content Publishers — модули публикации контента в социальные сети

Каждый publisher реализует BasePublisher и умеет отправлять
контент на соответствующую платформу.
"""
from services.content_publishers.base_publisher import BasePublisher, PublishResult
from services.content_publishers.telegram_publisher import TelegramPublisher
from services.content_publishers.vk_publisher import VKPublisher

# Реестр доступных публишеров
PUBLISHERS = {
    'telegram': TelegramPublisher,
    'vk': VKPublisher,
}


def get_publisher(platform: str) -> BasePublisher:
    """Возвращает экземпляр публишера для платформы."""
    publisher_class = PUBLISHERS.get(platform)
    if not publisher_class:
        raise ValueError(f"Публишер для платформы '{platform}' не реализован. Доступны: {', '.join(PUBLISHERS.keys())}")
    return publisher_class()
