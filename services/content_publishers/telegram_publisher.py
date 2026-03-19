# -*- coding: utf-8 -*-
"""
TelegramPublisher — публикация контента в Telegram через Bot API

Использует библиотеку requests для отправки сообщений.
Для работы нужен bot_token и chat_id в credentials аккаунта.
"""
import logging
from typing import Optional

import requests

from models import ContentItem, SocialAccount
from services.content_publishers.base_publisher import BasePublisher, PublishResult

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = 'https://api.telegram.org/bot{token}'


class TelegramPublisher(BasePublisher):
    """Публишер для Telegram каналов и групп."""

    platform = 'telegram'

    def publish(self, item: ContentItem, account: SocialAccount) -> PublishResult:
        """Отправляет пост в Telegram канал/группу."""
        creds = account.get_credentials_dict()
        bot_token = creds.get('bot_token', '')
        chat_id = creds.get('chat_id', '') or account.account_id

        if not bot_token or not chat_id:
            return PublishResult(
                success=False,
                error="Не указан bot_token или chat_id"
            )

        text = self.format_text(item)
        media_urls = item.get_media_urls()

        try:
            if media_urls:
                # Отправка с фото
                result = self._send_photo_message(bot_token, chat_id, text, media_urls[0])
            else:
                # Отправка текста
                result = self._send_text_message(bot_token, chat_id, text)

            return result

        except requests.exceptions.Timeout:
            return PublishResult(success=False, error="Таймаут при отправке в Telegram")
        except requests.exceptions.ConnectionError:
            return PublishResult(success=False, error="Ошибка подключения к Telegram API")
        except Exception as e:
            logger.error(f"Telegram publish error: {e}", exc_info=True)
            return PublishResult(success=False, error=str(e))

    def validate_account(self, account: SocialAccount) -> tuple[bool, Optional[str]]:
        """Проверяет валидность Telegram бота."""
        creds = account.get_credentials_dict()
        bot_token = creds.get('bot_token', '')

        if not bot_token:
            return False, "Не указан токен бота (bot_token)"

        chat_id = creds.get('chat_id', '') or account.account_id
        if not chat_id:
            return False, "Не указан ID чата/канала (chat_id)"

        # Проверяем бота через getMe
        try:
            url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/getMe"
            resp = requests.get(url, timeout=10)
            data = resp.json()

            if not data.get('ok'):
                return False, f"Невалидный токен бота: {data.get('description', 'unknown error')}"

            return True, None

        except requests.exceptions.RequestException as e:
            return False, f"Ошибка подключения к Telegram API: {e}"

    def _send_text_message(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
    ) -> PublishResult:
        """Отправляет текстовое сообщение."""
        url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/sendMessage"

        # Telegram лимит 4096 символов
        if len(text) > 4096:
            text = text[:4090] + '...'

        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': False,
        }

        resp = requests.post(url, json=payload, timeout=30)
        data = resp.json()

        if data.get('ok'):
            message_id = data.get('result', {}).get('message_id')
            # Формируем URL поста (для каналов)
            post_url = None
            if str(chat_id).startswith('@') or str(chat_id).startswith('-100'):
                channel = str(chat_id).lstrip('@')
                if channel.startswith('-100'):
                    channel = channel[4:]  # убираем -100
                post_url = f"https://t.me/c/{channel}/{message_id}"

            return PublishResult(
                success=True,
                external_post_id=str(message_id),
                external_post_url=post_url,
            )
        else:
            return PublishResult(
                success=False,
                error=data.get('description', 'Ошибка отправки в Telegram'),
            )

    def _send_photo_message(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        photo_url: str,
    ) -> PublishResult:
        """Отправляет сообщение с фото."""
        url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/sendPhoto"

        # Telegram caption лимит 1024 символа
        caption = text if len(text) <= 1024 else text[:1020] + '...'

        payload = {
            'chat_id': chat_id,
            'photo': photo_url,
            'caption': caption,
            'parse_mode': 'HTML',
        }

        resp = requests.post(url, json=payload, timeout=30)
        data = resp.json()

        if data.get('ok'):
            message_id = data.get('result', {}).get('message_id')
            return PublishResult(
                success=True,
                external_post_id=str(message_id),
            )
        else:
            # Если фото не удалось — шлём как текст
            logger.warning(f"Photo send failed, falling back to text: {data.get('description')}")
            return self._send_text_message(bot_token, chat_id, text)
