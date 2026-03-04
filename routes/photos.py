# -*- coding: utf-8 -*-
"""
Маршруты для раздачи фотографий поставщиков.

Включает:
- Безопасную раздачу кэшированных фото по хэшу
- Прокси-маршрут для фото товаров из каталога поставщика (SupplierProduct)
- Публичный маршрут для раздачи фото в WB (без авторизации, с подписанным токеном)
"""
import json
import re
import hmac
import hashlib
import logging
from pathlib import Path
from io import BytesIO

from flask import send_file, abort, Response, url_for
from flask_login import login_required

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


def _sign_photo_token(secret_key: str, sp_id: int, photo_idx: int) -> str:
    """Подписывает параметры фото HMAC-токеном."""
    msg = f'{sp_id}:{photo_idx}'
    sig = hmac.new(secret_key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]
    return sig


def generate_public_photo_url(supplier_product_id: int, photo_idx: int) -> str:
    """
    Генерирует публичный URL для фото товара поставщика.
    Используется для отображения в превью WB и для загрузки фото в WB.
    """
    from flask import current_app
    sig = _sign_photo_token(current_app.config['SECRET_KEY'], supplier_product_id, photo_idx)
    return url_for('serve_public_photo', sp=supplier_product_id, idx=photo_idx, sig=sig, _external=True)


def generate_public_photo_urls(imported_product) -> list:
    """
    Генерирует список публичных URL для всех фото ImportedProduct.
    Если есть связь с SupplierProduct — использует её.
    """
    from flask import current_app
    import json as _json

    photo_urls = []
    if imported_product.photo_urls:
        try:
            photo_urls = _json.loads(imported_product.photo_urls)
        except Exception:
            return []

    if not photo_urls:
        return []

    secret = current_app.config['SECRET_KEY']

    # Предпочитаем supplier_product_id для эффективного кэширования
    sp_id = imported_product.supplier_product_id
    if not sp_id:
        return []

    result = []
    for idx in range(len(photo_urls)):
        sig = _sign_photo_token(secret, sp_id, idx)
        result.append(url_for('serve_public_photo', sp=sp_id, idx=idx, sig=sig, _external=True))

    return result


def register_photo_routes(app):
    """Регистрирует маршруты раздачи фото в приложении Flask"""

    # ==========================================================================
    # Раздача кэшированного фото по хэшу (перенесено из seller_platform.py)
    # ==========================================================================

    @app.route('/photos/supplier/<supplier_type>/<external_id>/<photo_hash>')
    @login_required
    def serve_supplier_photo(supplier_type, external_id, photo_hash):
        """
        Безопасная раздача кэшированных фото поставщика.
        Только авторизованные пользователи. Не раскрывает оригинальный URL поставщика.
        """
        # Валидация параметров — только безопасные символы
        if not re.match(r'^[a-zA-Z0-9_-]+$', supplier_type):
            abort(404)
        if not re.match(r'^[a-zA-Z0-9_-]+$', external_id):
            abort(404)
        if not re.match(r'^[a-f0-9]+$', photo_hash):
            abort(404)

        cache_base = BASE_DIR / 'data' / 'photo_cache'
        photo_path = cache_base / supplier_type / external_id / f"{photo_hash}.jpg"

        # Path traversal защита
        try:
            photo_path.resolve().relative_to(cache_base.resolve())
        except ValueError:
            abort(404)

        if not photo_path.exists():
            abort(404)

        response = send_file(photo_path, mimetype='image/jpeg', conditional=True)
        response.cache_control.max_age = 86400
        response.cache_control.public = False
        response.cache_control.private = True
        return response

    # ==========================================================================
    # Прокси для фото товаров из каталога поставщика (SupplierProduct)
    # ==========================================================================

    @app.route('/api/photos/supplier-product/<int:supplier_product_id>/<int:photo_idx>')
    @login_required
    def serve_supplier_product_photo(supplier_product_id, photo_idx):
        """
        Прокси для фото товаров из каталога поставщика.
        Отдаёт из кэша или скачивает с сервера поставщика.
        """
        import requests as _requests
        from PIL import Image as _Image
        from models import SupplierProduct
        from services.photo_cache import get_photo_cache

        product = SupplierProduct.query.get_or_404(supplier_product_id)

        if not product.photo_urls_json:
            abort(404)

        try:
            photos = json.loads(product.photo_urls_json)
        except (json.JSONDecodeError, TypeError):
            abort(404)

        if photo_idx < 0 or photo_idx >= len(photos):
            abort(404)

        ph = photos[photo_idx]

        # Определяем URL — поддерживаем и dict, и строку
        if isinstance(ph, dict):
            url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
        elif isinstance(ph, str):
            url = ph
        else:
            abort(404)

        if not url:
            abort(404)

        supplier_type = product.supplier.code if product.supplier else 'unknown'
        external_id = product.external_id or ''
        cache = get_photo_cache()

        # Если уже закэшировано — отдаём из кэша
        if cache.is_cached(supplier_type, external_id, url):
            cache_path = cache.get_cache_path(supplier_type, external_id, url)
            response = send_file(cache_path, mimetype='image/jpeg', conditional=True)
            response.cache_control.max_age = 86400
            response.cache_control.private = True
            return response

        # Скачиваем с поставщика
        auth_cookies = _get_supplier_auth_cookies(product.supplier)

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/*,*/*;q=0.8',
        }
        if 'sexoptovik.ru' in url:
            headers['Referer'] = 'https://sexoptovik.ru/admin/'

        # Подготовим fallback URLs
        fallbacks = []
        if isinstance(ph, dict):
            if ph.get('blur') and ph['blur'] != url:
                fallbacks.append(ph['blur'])
            if ph.get('original') and ph['original'] != url:
                fallbacks.append(ph['original'])

        for current_url in [url] + fallbacks:
            try:
                resp = _requests.get(
                    current_url, headers=headers, cookies=auth_cookies,
                    timeout=15, allow_redirects=True
                )
                resp.raise_for_status()

                content_type = resp.headers.get('Content-Type', '')
                if not content_type.startswith('image/') and len(resp.content) < 1024:
                    continue

                img = _Image.open(BytesIO(resp.content))
                output = BytesIO()
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(output, format='JPEG', quality=95)
                image_bytes = output.getvalue()

                # Сохраняем в кэш
                cache.save_to_cache(supplier_type, external_id, url, image_bytes)

                response = Response(image_bytes, mimetype='image/jpeg')
                response.cache_control.max_age = 86400
                response.cache_control.private = True
                return response

            except Exception as e:
                logger.debug(f"[PhotoProxy] Failed {current_url[:60]}: {e}")
                continue

        # Возвращаем placeholder вместо 404
        return _generate_placeholder_image()

    # ==========================================================================
    # Прокси для фото ImportedProduct (через связь с SupplierProduct)
    # ==========================================================================

    @app.route('/api/photos/imported-product/<int:product_id>/<int:photo_idx>')
    @login_required
    def serve_imported_product_photo(product_id, photo_idx):
        """
        Прокси для фото импортированных товаров продавца.
        Если есть связь с SupplierProduct — переиспользуем его фото.
        Иначе пытаемся отдать из photo_urls самого ImportedProduct.
        """
        from flask import redirect, url_for as _url_for
        from models import ImportedProduct

        product = ImportedProduct.query.get_or_404(product_id)

        # Если есть связь с SupplierProduct — делегируем
        if product.supplier_product_id:
            return redirect(
                _url_for('serve_supplier_product_photo',
                         supplier_product_id=product.supplier_product_id,
                         photo_idx=photo_idx)
            )

        # Иначе пробуем photo_urls самого ImportedProduct
        if not product.photo_urls:
            abort(404)

        try:
            photos = json.loads(product.photo_urls)
        except (json.JSONDecodeError, TypeError):
            abort(404)

        if photo_idx < 0 or photo_idx >= len(photos):
            abort(404)

        url = photos[photo_idx] if isinstance(photos[photo_idx], str) else None
        if not url:
            abort(404)

        # Прямой прокси для URL
        import requests as _requests
        try:
            resp = _requests.get(url, timeout=15, allow_redirects=True,
                                 headers={'User-Agent': 'Mozilla/5.0'})
            resp.raise_for_status()
            response = Response(resp.content, mimetype=resp.headers.get('Content-Type', 'image/jpeg'))
            response.cache_control.max_age = 86400
            response.cache_control.private = True
            return response
        except Exception:
            return _generate_placeholder_image()

    # ==========================================================================
    # API управления скачиванием фото
    # ==========================================================================

    @app.route('/api/photos/download-all/<int:supplier_id>', methods=['POST'])
    @login_required
    def api_photos_download_all(supplier_id):
        """
        Запускает массовое фоновое скачивание всех фото поставщика.
        Фото, которые уже есть в кэше, пропускаются.
        """
        from services.photo_cache import bulk_download_supplier_photos
        try:
            result = bulk_download_supplier_photos(supplier_id)
            return {
                'success': True,
                'total_photos': result['total_photos'],
                'already_cached': result['already_cached'],
                'queued': result['queued'],
                'errors': result['errors']
            }
        except Exception as e:
            logger.error(f"Ошибка запуска массового скачивания фото: {e}")
            return {'success': False, 'error': str(e)}, 500

    @app.route('/api/photos/download-status/<int:supplier_id>')
    @login_required
    def api_photos_download_status(supplier_id):
        """
        Возвращает прогресс скачивания фото для поставщика.
        """
        from services.photo_cache import get_photo_cache
        try:
            cache = get_photo_cache()
            progress = cache.get_download_progress(supplier_id)
            return {
                'success': True,
                **progress
            }
        except Exception as e:
            logger.error(f"Ошибка получения прогресса: {e}")
            return {'success': False, 'error': str(e)}, 500

    @app.route('/api/photos/cache-stats')
    @login_required
    def api_photos_cache_stats():
        """
        Общая статистика кэша фотографий.
        """
        from services.photo_cache import get_photo_cache
        try:
            cache = get_photo_cache()
            stats = cache.get_stats()
            return {
                'success': True,
                **stats
            }
        except Exception as e:
            logger.error(f"Ошибка получения статистики: {e}")
            return {'success': False, 'error': str(e)}, 500

    # ==========================================================================
    # Публичный маршрут для раздачи фото (для WB и превью без авторизации)
    # ==========================================================================

    @app.route('/photos/public/<int:sp>/<int:idx>.jpg')
    def serve_public_photo(sp, idx):
        """
        Публичный маршрут для раздачи фото по supplier_product_id + idx.
        НЕ требует авторизации — для WB и внешних сервисов.
        Защищён HMAC-подписью в query parameter sig.
        URL: /photos/public/{supplier_product_id}/{photo_idx}.jpg?sig=HMAC
        """
        from flask import request as _req
        import requests as _requests
        from PIL import Image as _Image
        from services.photo_cache import get_photo_cache

        # Проверяем подпись
        sig = _req.args.get('sig', '')
        expected = _sign_photo_token(app.config['SECRET_KEY'], sp, idx)
        if not hmac.compare_digest(sig, expected):
            abort(403)

        from models import SupplierProduct
        product = SupplierProduct.query.get(sp)
        if not product or not product.photo_urls_json:
            abort(404)

        try:
            photos = json.loads(product.photo_urls_json)
        except (json.JSONDecodeError, TypeError):
            abort(404)

        if idx < 0 or idx >= len(photos):
            abort(404)

        ph = photos[idx]
        supplier_type = product.supplier.code if product.supplier else 'unknown'
        external_id = product.external_id or ''

        # Определяем URL
        if isinstance(ph, dict):
            url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
        elif isinstance(ph, str):
            url = ph
        else:
            abort(404)

        if not url:
            abort(404)

        cache = get_photo_cache()

        # Из кэша
        if cache.is_cached(supplier_type, external_id, url):
            cache_path = cache.get_cache_path(supplier_type, external_id, url)
            response = send_file(cache_path, mimetype='image/jpeg', conditional=True)
            response.cache_control.max_age = 86400
            response.cache_control.public = True
            return response

        # Скачиваем с поставщика
        auth_cookies = _get_supplier_auth_cookies(product.supplier)

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/*,*/*;q=0.8',
        }
        if 'sexoptovik.ru' in url:
            headers['Referer'] = 'https://sexoptovik.ru/admin/'

        fallbacks = []
        if isinstance(ph, dict):
            if ph.get('blur') and ph['blur'] != url:
                fallbacks.append(ph['blur'])
            if ph.get('original') and ph['original'] != url:
                fallbacks.append(ph['original'])

        for current_url in [url] + fallbacks:
            try:
                resp = _requests.get(
                    current_url, headers=headers, cookies=auth_cookies,
                    timeout=15, allow_redirects=True
                )
                resp.raise_for_status()

                content_type = resp.headers.get('Content-Type', '')
                if not content_type.startswith('image/') and len(resp.content) < 1024:
                    continue

                img = _Image.open(BytesIO(resp.content))
                output = BytesIO()
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(output, format='JPEG', quality=95)
                image_bytes = output.getvalue()

                cache.save_to_cache(supplier_type, external_id, url, image_bytes)

                response = Response(image_bytes, mimetype='image/jpeg')
                response.cache_control.max_age = 86400
                response.cache_control.public = True
                return response

            except Exception as e:
                logger.debug(f"[PublicPhoto] Failed {current_url[:60]}: {e}")
                continue

        return _generate_placeholder_image()


def _get_supplier_auth_cookies(supplier) -> dict:
    """Получает cookies авторизации для поставщика (если требуется)"""
    if not supplier:
        return {}

    if supplier.code == 'sexoptovik' and supplier.auth_login and supplier.auth_password:
        try:
            import requests as _requests
            session = _requests.Session()
            login_url = 'https://sexoptovik.ru/admin/login'
            resp = session.post(login_url, data={
                'login': supplier.auth_login,
                'password': supplier.auth_password,
            }, timeout=10, allow_redirects=False)
            if resp.status_code in (200, 302):
                return dict(session.cookies)
        except Exception as e:
            logger.debug(f"[PhotoProxy] Auth failed for {supplier.code}: {e}")

    return {}


def _generate_placeholder_image():
    """Генерирует placeholder изображение (серый квадрат с иконкой)"""
    from PIL import Image as _Image, ImageDraw as _ImageDraw

    img = _Image.new('RGB', (200, 200), '#f3f4f6')
    draw = _ImageDraw.Draw(img)
    # Рисуем простую рамку с крестиком
    draw.line([(80, 80), (120, 120)], fill='#d1d5db', width=2)
    draw.line([(120, 80), (80, 120)], fill='#d1d5db', width=2)
    draw.rectangle([(60, 60), (140, 140)], outline='#d1d5db', width=1)

    output = BytesIO()
    img.save(output, format='JPEG', quality=80)
    output.seek(0)

    response = Response(output.getvalue(), mimetype='image/jpeg')
    response.cache_control.max_age = 300
    response.cache_control.private = True
    return response
