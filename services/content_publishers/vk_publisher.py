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
    "api_version": "5.199",     # (опционально)
    "user_token": "vk1.a.yyy..."  # Пользовательский токен (для загрузки фото)
                                   # Групповой токен НЕ поддерживает photos.getWallUploadServer
                                   # Получить: https://vk.cc/1mYRMQ (scope: photos,wall,offline)
}
"""
import io
import logging
import time
from typing import Optional

import requests
from PIL import Image

from models import ContentItem, SocialAccount
from services.content_publishers.base_publisher import BasePublisher, PublishResult

logger = logging.getLogger(__name__)

VK_API_BASE = 'https://api.vk.com/method'
VK_API_VERSION = '5.199'


_BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)


def _read_local_supplier_photo(photo_url: str) -> Optional[tuple[bytes, str]]:
    """Если URL указывает на /photos/public/ или /photos/imported/ — пробуем из кэша поставщика."""
    import re
    # /photos/public/{sp_id}/{idx}.jpg?sig=...
    m = re.search(r'/photos/public/(\d+)/(\d+)\.jpg', photo_url)
    if not m:
        return None
    try:
        from models import SupplierProduct
        import json as _json
        from services.photo_cache import get_photo_cache

        sp_id = int(m.group(1))
        idx = int(m.group(2))
        product = SupplierProduct.query.get(sp_id)
        if not product or not product.photo_urls_json:
            return None

        photos = _json.loads(product.photo_urls_json)
        if idx < 0 or idx >= len(photos):
            return None

        ph = photos[idx]
        url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur') if isinstance(ph, dict) else ph if isinstance(ph, str) else None
        if not url:
            return None

        supplier_type = product.supplier.code if product.supplier else 'unknown'
        external_id = product.external_id or ''
        cache = get_photo_cache()

        if cache.is_cached(supplier_type, external_id, url):
            cache_path = cache.get_cache_path(supplier_type, external_id, url)
            import os
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 512:
                with open(cache_path, 'rb') as f:
                    data = f.read()
                logger.info(f"Read local supplier photo: {cache_path} ({len(data)}B)")
                return data, 'photo.jpg'
    except Exception as e:
        logger.debug(f"Failed to read local supplier photo: {e}")
    return None


def _read_local_content_photo(photo_url: str) -> Optional[tuple[bytes, str]]:
    """Если URL указывает на наш /content-photos/ — читаем файл с диска напрямую."""
    import re
    # Матчим /content-photos/{nm_id}/{index}.jpg в URL
    m = re.search(r'/content-photos/(\d+)/(\d+)\.jpg', photo_url)
    if not m:
        return None

    try:
        from services.content_photo_cache import get_cached_photo_path
        nm_id = int(m.group(1))
        index = int(m.group(2))
        path = get_cached_photo_path(nm_id, index)
        if path.exists() and path.stat().st_size > 512:
            jpeg_bytes = path.read_bytes()
            logger.info(f"Read local content photo: {path} ({len(jpeg_bytes)}B)")
            return jpeg_bytes, 'photo.jpg'
    except Exception as e:
        logger.warning(f"Failed to read local content photo: {e}")
    return None


def _download_and_convert_to_jpeg(photo_url: str) -> Optional[tuple[bytes, str]]:
    """Скачивает фото по URL и конвертирует в JPEG.

    Если URL указывает на наш /content-photos/ — читает с диска (без HTTP).

    Returns:
        (jpeg_bytes, filename) или None при ошибке
    """
    # Приоритет: локальные файлы — читаем с диска напрямую
    local = _read_local_content_photo(photo_url)
    if local:
        return local

    # Также пробуем /photos/public/ — локальный supplier кэш
    if '/photos/public/' in photo_url or '/photos/imported/' in photo_url:
        local_supplier = _read_local_supplier_photo(photo_url)
        if local_supplier:
            return local_supplier

    # Скачиваем по HTTP (с одним ретраем)
    raw = None
    content_type = ''
    headers = {
        'User-Agent': _BROWSER_UA,
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Referer': 'https://www.wildberries.ru/',
    }
    for attempt in range(2):
        try:
            resp = requests.get(photo_url, timeout=15, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"Photo download HTTP {resp.status_code} (attempt {attempt + 1}): {photo_url}")
                if attempt == 0:
                    continue
                return None

            raw = resp.content
            content_type = resp.headers.get('Content-Type', '')
            break
        except Exception as e:
            logger.error(f"Photo download failed (attempt {attempt + 1}): {e} URL: {photo_url[:100]}")
            if attempt == 0:
                continue
            return None

    if not raw:
        return None

    if len(raw) < 512:
        logger.warning(f"Photo too small ({len(raw)}B): {photo_url}")
        return None

    logger.info(f"Photo downloaded: {len(raw)}B, content-type={content_type} from {photo_url[:80]}")

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
        # user_token нужен для загрузки фото (photos.getWallUploadServer не работает с group token)
        user_token = creds.get('user_token', '')
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

        # Относительные URL → абсолютные (для скачивания с нашего сервера)
        try:
            from flask import current_app
            public_base = current_app.config.get('PUBLIC_BASE_URL', '').rstrip('/')
            if public_base:
                media_urls = [
                    f'{public_base}{u}' if u.startswith('/') else u
                    for u in media_urls
                ]
        except RuntimeError:
            pass  # Нет app context

        if media_urls and not user_token:
            logger.warning(f"VK publish: no user_token in credentials — photo upload may fail "
                           f"(group tokens can't use photos.getWallUploadServer). "
                           f"Add user_token to VK account credentials.")

        logger.info(f"VK publish item={item.id}: group_id={group_id}, media_urls={len(media_urls)}, "
                    f"has_user_token={bool(user_token)}")
        for i, url in enumerate(media_urls[:5]):
            logger.info(f"  photo[{i}]: {url[:120]}")

        try:
            # Загружаем фото через VK Upload API
            attachments = []
            photo_errors = []

            for i, url in enumerate(media_urls[:10]):
                if i > 0:
                    time.sleep(1.0)  # VK rate limit + запас на обработку
                # Для фото используем user_token (group token не поддерживает photos.getWallUploadServer)
                photo_token = user_token or access_token

                # До 2 попыток на каждое фото (ретрай при vk_upload_empty_photo)
                attachment, error_reason = None, None
                for attempt in range(2):
                    if attempt > 0:
                        logger.info(f"  photo[{i}] retry attempt {attempt + 1} after 3s...")
                        time.sleep(3)
                    attachment, error_reason = self._upload_photo(photo_token, group_id, url, api_version)
                    if attachment:
                        break
                    # Ретраим только при пустом фото (серверная проблема VK)
                    if error_reason and 'vk_upload_empty_photo' not in error_reason:
                        break

                if attachment:
                    attachments.append(attachment)
                    logger.info(f"  photo[{i}] OK: {attachment}")
                else:
                    short_url = url.split('/')[-1] if '/' in url else url[:40]
                    error_info = f"{short_url}({error_reason})"
                    photo_errors.append(error_info)
                    logger.warning(f"  photo[{i}] FAILED: {error_reason} URL: {url[:80]}")

            if media_urls and not attachments:
                error_summary = '; '.join(photo_errors[:5])
                logger.error(f"VK publish: ALL {len(media_urls)} photos failed to upload: {error_summary}")
                return PublishResult(
                    success=False,
                    error=f"Все {len(media_urls)} фото не загружены в VK: {error_summary}"
                )

            # Даём VK время обработать загруженные фото перед wall.post
            if attachments:
                time.sleep(3)

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
                error_detail = f"Фото не загружены ({len(photo_errors)} из {len(media_urls)}): {'; '.join(photo_errors[:5])}"
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
    ) -> tuple[Optional[str], Optional[str]]:
        """Загружает фото по URL на стену сообщества VK.

        Returns:
            (attachment_string, None) при успехе
            (None, error_reason) при ошибке
        """
        # === ШАГ 0: Скачиваем и конвертируем в JPEG ===
        photo_data = _download_and_convert_to_jpeg(photo_url)
        if not photo_data:
            return None, "download_failed"
        jpeg_bytes, filename = photo_data
        logger.info(f"VK upload: downloaded {len(jpeg_bytes)}B from {photo_url[:80]}")

        try:
            # === ШАГ 1: photos.getWallUploadServer ===
            resp = requests.get(
                f'{VK_API_BASE}/photos.getWallUploadServer',
                params={
                    'access_token': access_token,
                    'group_id': group_id,
                    'v': api_version,
                },
                timeout=10,
            )
            srv = resp.json()
            logger.debug(f"getWallUploadServer response: {srv}")

            if 'error' in srv:
                err = srv['error']
                reason = f"getUploadServer error {err.get('error_code')}: {err.get('error_msg', '')}"
                logger.error(f"VK {reason}")
                return None, reason

            upload_url = srv.get('response', {}).get('upload_url')
            if not upload_url:
                logger.error(f"VK getWallUploadServer: no upload_url")
                return None, "no_upload_url"

            # === ШАГ 2: POST фото на upload_url (с ретраем при empty photo) ===
            photo_field = None
            upload_data = None
            for upload_attempt in range(2):
                if upload_attempt > 0:
                    # Получаем новый upload_url перед ретраем
                    logger.info(f"VK upload retry: requesting new upload_url (attempt {upload_attempt + 1})")
                    time.sleep(2)
                    retry_resp = requests.get(
                        f'{VK_API_BASE}/photos.getWallUploadServer',
                        params={
                            'access_token': access_token,
                            'group_id': group_id,
                            'v': api_version,
                        },
                        timeout=10,
                    )
                    retry_srv = retry_resp.json()
                    if 'error' in retry_srv:
                        break
                    upload_url = retry_srv.get('response', {}).get('upload_url') or upload_url

                upload_resp = requests.post(
                    upload_url,
                    files={'photo': (filename, jpeg_bytes, 'image/jpeg')},
                    timeout=30,
                )
                upload_data = upload_resp.json()
                logger.info(f"VK upload response (attempt {upload_attempt + 1}): server={upload_data.get('server')}, "
                            f"photo_len={len(upload_data.get('photo', ''))}, "
                            f"hash={upload_data.get('hash', '')[:16]}...")

                photo_field = upload_data.get('photo', '')
                if photo_field and photo_field not in ('[]', ''):
                    break  # Успешно

                logger.warning(f"VK upload: empty photo field (attempt {upload_attempt + 1}). Response: {upload_data}")

            # После ретраев всё ещё пусто
            if not photo_field or photo_field in ('[]', ''):
                logger.error(f"VK upload: empty photo field after retries. Full response: {upload_data}")
                return None, f"vk_upload_empty_photo: {str(upload_data)[:200]}"

            # === ШАГ 3: photos.saveWallPhoto ===
            save_resp = requests.post(
                f'{VK_API_BASE}/photos.saveWallPhoto',
                data={
                    'access_token': access_token,
                    'group_id': group_id,
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
                err = save_data['error']
                reason = f"saveWallPhoto error {err.get('error_code')}: {err.get('error_msg', '')}"
                logger.error(f"VK {reason}")
                return None, reason

            photos = save_data.get('response', [])
            if not photos:
                logger.error(f"VK saveWallPhoto: empty response array")
                return None, "saveWallPhoto_empty_response"

            # === ШАГ 4: Формируем attachment string ===
            photo_obj = photos[0]
            owner_id = photo_obj['owner_id']
            photo_id = photo_obj['id']
            attachment = f"photo{owner_id}_{photo_id}"
            return attachment, None

        except requests.exceptions.Timeout as e:
            logger.error(f"VK photo upload timeout: {e}")
            return None, f"timeout: {e}"
        except requests.exceptions.ConnectionError as e:
            logger.error(f"VK photo upload connection error: {e}")
            return None, f"connection_error: {str(e)[:100]}"
        except Exception as e:
            logger.error(f"VK photo upload exception: {e}", exc_info=True)
            return None, f"exception: {str(e)[:100]}"
