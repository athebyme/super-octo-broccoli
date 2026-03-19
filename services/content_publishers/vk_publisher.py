# -*- coding: utf-8 -*-
"""
VKPublisher — публикация контента ВКонтакте через VK API

Процедура загрузки фото (по документации VK API):
1. photos.getWallUploadServer(group_id) → upload_url
2. POST файл на upload_url в поле "photo" (multipart/form-data) → server, photo, hash
3. photos.saveWallPhoto(group_id, server, photo, hash) → photo object с owner_id и id
4. wall.post(attachments="photo{owner_id}_{id}")

Credentials формат:
{
    "access_token": "vk1.a.xxx...",
    "group_id": "123456789",    # ID сообщества (положительное число, без минуса)
    "api_version": "5.199"      # (опционально)
}
"""
import io
import logging
from typing import Optional

import requests
from PIL import Image

from models import ContentItem, SocialAccount
from services.content_publishers.base_publisher import BasePublisher, PublishResult

logger = logging.getLogger(__name__)

VK_API_BASE = 'https://api.vk.com/method'
VK_API_VERSION = '5.199'


def _download_and_convert_to_jpeg(photo_url: str) -> Optional[tuple[bytes, str]]:
    """Скачивает фото по URL и конвертирует в JPEG.

    Returns:
        (jpeg_bytes, filename) или None при ошибке
    """
    # Шаг 1: Скачиваем
    try:
        resp = requests.get(
            photo_url,
            timeout=15,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; ContentBot/1.0)'},
        )
        if resp.status_code != 200:
            logger.warning(f"Photo download HTTP {resp.status_code}: {photo_url}")
            return None

        raw = resp.content
        if len(raw) < 512:
            logger.warning(f"Photo too small ({len(raw)}B): {photo_url}")
            return None

        content_type = resp.headers.get('Content-Type', '')
        logger.info(f"Photo downloaded: {len(raw)}B, content-type={content_type} from {photo_url[:80]}")
    except Exception as e:
        logger.error(f"Photo download failed: {e} URL: {photo_url[:100]}")
        return None

    # Шаг 2: Если уже JPEG — используем напрямую (серверные фото /photos/public/ уже JPEG)
    if content_type.startswith('image/jpeg') or raw[:3] == b'\xff\xd8\xff':
        logger.info(f"Photo already JPEG ({len(raw)}B), skipping conversion")
        return raw, 'photo.jpg'

    # Шаг 3: Конвертируем через Pillow (для webp, png и т.д.)
    try:
        img = Image.open(io.BytesIO(raw))
        if img.mode in ('RGBA', 'LA', 'P', 'PA'):
            img = img.convert('RGB')
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=93)
        jpeg_bytes = buf.getvalue()

        logger.info(f"Photo converted to JPEG: {len(raw)}B → {len(jpeg_bytes)}B from {photo_url[:80]}")
        return jpeg_bytes, 'photo.jpg'
    except Exception as e:
        logger.error(f"Pillow conversion failed: {e} URL: {photo_url[:100]}")

    # Последний fallback — отправляем raw напрямую (VK может принять jpeg/png)
    logger.warning(f"Conversion failed, sending raw to VK for {photo_url[:80]}")
    return raw, 'photo.jpg'


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

        # group_id ВСЕГДА положительное число (по доке VK API)
        group_id = str(group_id).lstrip('-').strip()

        text = self.format_text(item)
        media_urls = item.get_media_urls()

        logger.info(f"VK publish item={item.id}: group_id={group_id}, media_urls={len(media_urls)}")
        for i, url in enumerate(media_urls[:5]):
            logger.info(f"  photo[{i}]: {url[:120]}")

        try:
            # Загружаем фото через VK Upload API
            attachments = []
            photo_errors = []

            for i, url in enumerate(media_urls[:10]):
                result = self._upload_photo(access_token, group_id, url, api_version)
                if result:
                    attachments.append(result)
                    logger.info(f"  photo[{i}] OK: {result}")
                else:
                    photo_errors.append(url[:80])
                    logger.warning(f"  photo[{i}] FAILED: {url[:80]}")

            if media_urls and not attachments:
                logger.error(f"VK publish: ALL {len(media_urls)} photos failed to upload")

            # wall.post: owner_id отрицательный для сообщества
            params = {
                'access_token': access_token,
                'v': api_version,
                'owner_id': f'-{group_id}',
                'from_group': 1,
                'message': text,
            }

            if attachments:
                params['attachments'] = ','.join(attachments)

            resp = requests.post(
                f'{VK_API_BASE}/wall.post',
                data=params,
                timeout=30,
            )
            data = resp.json()
            logger.info(f"VK wall.post response: {data}")

            if 'error' in data:
                error_msg = data['error'].get('error_msg', 'Неизвестная ошибка VK API')
                error_code = data['error'].get('error_code', 0)
                return PublishResult(
                    success=False,
                    error=f"VK API ошибка {error_code}: {error_msg}"
                )

            post_id = data.get('response', {}).get('post_id')
            post_url = f"https://vk.com/wall-{group_id}_{post_id}" if post_id else None

            # Сообщаем об ошибках фото даже при успешной публикации
            error_detail = None
            if photo_errors:
                error_detail = f"Фото не загружены ({len(photo_errors)} из {len(media_urls)}): {'; '.join(photo_errors[:3])}"
                logger.warning(f"VK post published but with photo errors: {error_detail}")

            return PublishResult(
                success=True,
                external_post_id=str(post_id) if post_id else None,
                external_post_url=post_url,
                error=error_detail,  # ошибки фото видны в UI
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
                    'group_id': str(group_id).lstrip('-'),
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

    def _upload_photo(
        self,
        access_token: str,
        group_id: str,
        photo_url: str,
        api_version: str,
    ) -> Optional[str]:
        """Загружает фото по URL на стену сообщества VK.

        Строго по документации VK API:
        1. photos.getWallUploadServer → upload_url
        2. POST photo на upload_url → server, photo, hash
        3. photos.saveWallPhoto → photo object
        4. Возвращает "photo{owner_id}_{photo_id}"
        """
        # === ШАГ 0: Скачиваем и конвертируем в JPEG ===
        photo_data = _download_and_convert_to_jpeg(photo_url)
        if not photo_data:
            return None
        jpeg_bytes, filename = photo_data

        try:
            # === ШАГ 1: photos.getWallUploadServer ===
            resp = requests.get(
                f'{VK_API_BASE}/photos.getWallUploadServer',
                params={
                    'access_token': access_token,
                    'group_id': group_id,  # положительное число
                    'v': api_version,
                },
                timeout=10,
            )
            srv = resp.json()
            logger.debug(f"getWallUploadServer response: {srv}")

            if 'error' in srv:
                logger.error(f"VK getWallUploadServer error: {srv['error']}")
                return None

            upload_url = srv.get('response', {}).get('upload_url')
            if not upload_url:
                logger.error(f"VK getWallUploadServer: no upload_url")
                return None

            # === ШАГ 2: POST фото на upload_url ===
            # Поле "photo", файл с расширением .jpg, content-type image/jpeg
            upload_resp = requests.post(
                upload_url,
                files={'photo': (filename, jpeg_bytes, 'image/jpeg')},
                timeout=30,
            )
            upload_data = upload_resp.json()
            logger.debug(f"VK upload response: server={upload_data.get('server')}, "
                         f"photo_len={len(upload_data.get('photo', ''))}, "
                         f"hash={upload_data.get('hash', '')[:16]}...")

            # VK возвращает photo как JSON-строку. Пустое = ошибка
            photo_field = upload_data.get('photo', '')
            if not photo_field or photo_field in ('[]', ''):
                logger.error(f"VK upload: empty photo field. Full response: {upload_data}")
                return None

            # === ШАГ 3: photos.saveWallPhoto ===
            save_resp = requests.post(
                f'{VK_API_BASE}/photos.saveWallPhoto',
                data={
                    'access_token': access_token,
                    'group_id': group_id,  # положительное число
                    'server': upload_data.get('server', ''),
                    'photo': photo_field,
                    'hash': upload_data.get('hash', ''),
                    'v': api_version,
                },
                timeout=10,
            )
            save_data = save_resp.json()
            logger.debug(f"saveWallPhoto response: {save_data}")

            if 'error' in save_data:
                logger.error(f"VK saveWallPhoto error: {save_data['error']}")
                return None

            photos = save_data.get('response', [])
            if not photos:
                logger.error(f"VK saveWallPhoto: empty response array")
                return None

            # === ШАГ 4: Формируем attachment string ===
            photo_obj = photos[0]
            owner_id = photo_obj['owner_id']  # отрицательный для группы
            photo_id = photo_obj['id']
            attachment = f"photo{owner_id}_{photo_id}"
            return attachment

        except Exception as e:
            logger.error(f"VK photo upload exception: {e}", exc_info=True)
            return None
