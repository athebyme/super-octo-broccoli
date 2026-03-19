# -*- coding: utf-8 -*-
"""
VKPublisher — публикация контента ВКонтакте через VK API

Credentials формат:
{
    "access_token": "vk1.a.xxx...",
    "group_id": "123456789",    # ID сообщества (без минуса)
    "api_version": "5.199"      # (опционально)
}
"""
import logging
from typing import Optional

import requests

from models import ContentItem, SocialAccount
from services.content_publishers.base_publisher import BasePublisher, PublishResult

logger = logging.getLogger(__name__)

VK_API_BASE = 'https://api.vk.com/method'
VK_API_VERSION = '5.199'


class VKPublisher(BasePublisher):
    """Публишер для сообществ ВКонтакте."""

    platform = 'vk'

    def publish(self, item: ContentItem, account: SocialAccount) -> PublishResult:
        """Публикует пост на стене сообщества ВКонтакте."""
        creds = account.get_credentials_dict()
        access_token = creds.get('access_token', '')
        group_id = creds.get('group_id', '') or account.account_id
        api_version = creds.get('api_version', VK_API_VERSION)

        if not access_token or not group_id:
            return PublishResult(
                success=False,
                error="Не указан access_token или group_id"
            )

        text = self.format_text(item)
        media_urls = item.get_media_urls()

        try:
            params = {
                'access_token': access_token,
                'v': api_version,
                'owner_id': f'-{group_id}',  # Минус = от имени сообщества
                'from_group': 1,
                'message': text,
            }

            # Если есть фото, загружаем через VK API
            attachments = []
            if media_urls:
                for url in media_urls[:10]:  # VK максимум 10 вложений
                    attachment = self._upload_photo_by_url(access_token, group_id, url, api_version)
                    if attachment:
                        attachments.append(attachment)

            if attachments:
                params['attachments'] = ','.join(attachments)

            resp = requests.post(
                f'{VK_API_BASE}/wall.post',
                data=params,
                timeout=30,
            )
            data = resp.json()

            if 'error' in data:
                error_msg = data['error'].get('error_msg', 'Неизвестная ошибка VK API')
                error_code = data['error'].get('error_code', 0)
                return PublishResult(
                    success=False,
                    error=f"VK API ошибка {error_code}: {error_msg}"
                )

            post_id = data.get('response', {}).get('post_id')
            post_url = f"https://vk.com/wall-{group_id}_{post_id}" if post_id else None

            return PublishResult(
                success=True,
                external_post_id=str(post_id) if post_id else None,
                external_post_url=post_url,
            )

        except requests.exceptions.Timeout:
            return PublishResult(success=False, error="Таймаут при отправке в VK")
        except requests.exceptions.ConnectionError:
            return PublishResult(success=False, error="Ошибка подключения к VK API")
        except Exception as e:
            logger.error(f"VK publish error: {e}", exc_info=True)
            return PublishResult(success=False, error=str(e))

    def validate_account(self, account: SocialAccount) -> tuple[bool, Optional[str]]:
        """Проверяет валидность VK токена."""
        creds = account.get_credentials_dict()
        access_token = creds.get('access_token', '')

        if not access_token:
            return False, "Не указан токен доступа (access_token)"

        group_id = creds.get('group_id', '') or account.account_id
        if not group_id:
            return False, "Не указан ID сообщества (group_id)"

        try:
            resp = requests.get(
                f'{VK_API_BASE}/groups.getById',
                params={
                    'access_token': access_token,
                    'group_id': group_id,
                    'v': VK_API_VERSION,
                },
                timeout=10,
            )
            data = resp.json()

            if 'error' in data:
                error_msg = data['error'].get('error_msg', 'unknown')
                return False, f"VK API ошибка: {error_msg}"

            return True, None

        except requests.exceptions.RequestException as e:
            return False, f"Ошибка подключения к VK API: {e}"

    def _upload_photo_by_url(
        self,
        access_token: str,
        group_id: str,
        photo_url: str,
        api_version: str,
    ) -> Optional[str]:
        """Загружает фото по URL для использования в посте.

        Returns:
            VK attachment string (photo{owner_id}_{photo_id}) или None
        """
        try:
            # 1. Получаем URL для загрузки
            resp = requests.get(
                f'{VK_API_BASE}/photos.getWallUploadServer',
                params={
                    'access_token': access_token,
                    'group_id': group_id,
                    'v': api_version,
                },
                timeout=10,
            )
            upload_url = resp.json().get('response', {}).get('upload_url')
            if not upload_url:
                return None

            # 2. Скачиваем фото
            photo_resp = requests.get(photo_url, timeout=15)
            if photo_resp.status_code != 200:
                return None

            # 3. Загружаем на VK
            files = {'photo': ('photo.jpg', photo_resp.content, 'image/jpeg')}
            upload_resp = requests.post(upload_url, files=files, timeout=30)
            upload_data = upload_resp.json()

            # 4. Сохраняем фото
            save_resp = requests.post(
                f'{VK_API_BASE}/photos.saveWallPhoto',
                data={
                    'access_token': access_token,
                    'group_id': group_id,
                    'photo': upload_data.get('photo', ''),
                    'server': upload_data.get('server', ''),
                    'hash': upload_data.get('hash', ''),
                    'v': api_version,
                },
                timeout=10,
            )
            save_data = save_resp.json()

            photos = save_data.get('response', [])
            if photos:
                photo = photos[0]
                return f"photo{photo['owner_id']}_{photo['id']}"

        except Exception as e:
            logger.warning(f"VK photo upload failed for {photo_url}: {e}")

        return None
