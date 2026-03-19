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
            if len(media_urls) > 1:
                # Отправка нескольких фото через media group
                result = self._send_media_group(bot_token, chat_id, text, media_urls[:10])
            elif media_urls:
                # Отправка с одним фото
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
        """Проверяет валидность Telegram бота и что chat_id — это канал/группа."""
        creds = account.get_credentials_dict()
        bot_token = creds.get('bot_token', '')

        if not bot_token:
            return False, "Не указан токен бота (bot_token)"

        chat_id = creds.get('chat_id', '') or account.account_id
        if not chat_id:
            return False, "Не указан ID канала (chat_id). Укажите @username канала или его числовой ID (начинается с -100)"

        try:
            # 1. Проверяем бота через getMe
            url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/getMe"
            resp = requests.get(url, timeout=10)
            data = resp.json()

            if not data.get('ok'):
                return False, f"Невалидный токен бота: {data.get('description', 'unknown error')}"

            bot_name = data.get('result', {}).get('username', 'бот')

            # 2. Проверяем что chat_id — это канал/группа, а не личный чат
            url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/getChat"
            resp = requests.post(url, json={'chat_id': chat_id}, timeout=10)
            data = resp.json()

            if not data.get('ok'):
                desc = data.get('description', '')
                if 'chat not found' in desc.lower():
                    return False, (
                        f"Канал не найден. Убедитесь что:\n"
                        f"1) Указан @username канала или ID (начинается с -100)\n"
                        f"2) Бот @{bot_name} добавлен в канал как администратор"
                    )
                return False, f"Ошибка проверки канала: {desc}"

            chat_info = data.get('result', {})
            chat_type = chat_info.get('type', '')

            # Личный чат — нельзя, нужен канал или группа
            if chat_type == 'private':
                return False, (
                    f"ID {chat_id} — это личный чат, а не канал. "
                    f"Укажите @username вашего Telegram-канала (например @mychannel) "
                    f"или числовой ID канала (начинается с -100)"
                )

            if chat_type not in ('channel', 'supergroup', 'group'):
                return False, f"Неподдерживаемый тип чата: {chat_type}. Нужен канал или группа."

            # Обновляем account_name из данных Telegram
            chat_title = chat_info.get('title', '')
            if chat_title and not account.account_name:
                account.account_name = chat_title

            # 3. Проверяем что бот — администратор канала
            if chat_type == 'channel':
                url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/getChatMember"
                bot_id = data.get('result', {}).get('id') if 'result' not in data else None
                # Получаем ID бота
                me_url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/getMe"
                me_resp = requests.get(me_url, timeout=10)
                me_data = me_resp.json()
                if me_data.get('ok'):
                    bot_user_id = me_data['result']['id']
                    member_url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/getChatMember"
                    member_resp = requests.post(member_url, json={
                        'chat_id': chat_id,
                        'user_id': bot_user_id,
                    }, timeout=10)
                    member_data = member_resp.json()
                    if member_data.get('ok'):
                        status = member_data.get('result', {}).get('status', '')
                        if status not in ('administrator', 'creator'):
                            return False, (
                                f"Бот @{bot_name} не является администратором канала \"{chat_title}\". "
                                f"Добавьте бота в канал как администратора с правом отправки сообщений."
                            )

            logger.info(f"Telegram account validated: {chat_type} \"{chat_title}\" (chat_id={chat_id})")
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

    def _send_media_group(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        photo_urls: list,
    ) -> PublishResult:
        """Отправляет несколько фото с текстом через sendMediaGroup."""
        import json as _json
        url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/sendMediaGroup"

        caption = text if len(text) <= 1024 else text[:1020] + '...'

        media = []
        for i, photo_url in enumerate(photo_urls[:10]):
            item = {'type': 'photo', 'media': photo_url}
            if i == 0:
                item['caption'] = caption
                item['parse_mode'] = 'HTML'
            media.append(item)

        payload = {
            'chat_id': chat_id,
            'media': _json.dumps(media),
        }

        resp = requests.post(url, json=payload, timeout=60)
        data = resp.json()

        if data.get('ok'):
            results = data.get('result', [])
            message_id = results[0].get('message_id') if results else None
            return PublishResult(
                success=True,
                external_post_id=str(message_id) if message_id else None,
            )
        else:
            # Фоллбэк на одно фото
            logger.warning(f"Media group failed, falling back to single photo: {data.get('description')}")
            return self._send_photo_message(bot_token, chat_id, text, photo_urls[0])

    def format_text(self, item: ContentItem) -> str:
        """Форматирует текст для Telegram (HTML)."""
        text = item.body_text or ''

        # Добавляем хештеги если они есть и не в тексте
        hashtags = item.get_hashtags()
        if hashtags:
            existing_tags = set(tag.lower() for tag in text.split() if tag.startswith('#'))
            new_tags = [t for t in hashtags if t.lower() not in existing_tags]
            if new_tags:
                text = text.rstrip() + '\n\n' + ' '.join(new_tags)

        return text
